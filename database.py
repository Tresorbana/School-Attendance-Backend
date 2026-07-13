"""
Two SQLAlchemy engines:
  - structured_engine: people, attendance, stations, holidays (Neon Postgres)
  - templates_engine:  fingerprint template binaries (local SQLite by default)

Same pool-hygiene rules apply to the structured engine as the templates one:
pool_pre_ping, short pool_recycle, TCP keepalives, post-connect statement_timeout
that works through Neon's pooler (no startup options).
"""
import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from config import settings


class StructuredBase(DeclarativeBase):
    """Tables stored in the main (Postgres) database."""
    pass


class TemplatesBase(DeclarativeBase):
    """Tables stored in the local (SQLite) templates DB."""
    pass


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _make_engine(url: str):
    if _is_sqlite(url):
        return create_engine(url, connect_args={"check_same_thread": False, "timeout": 5})

    eng = create_engine(
        url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        pool_recycle=180,
        connect_args={
            "keepalives": 1,
            "keepalives_idle": 30,
            "keepalives_interval": 10,
            "keepalives_count": 5,
            # Neon free-tier scales to zero — first connect can take 5–15s
            # while the compute wakes up. 30s gives headroom without hanging.
            "connect_timeout": 30,
            "sslmode": settings.DATABASE_SSLMODE,
        },
    )

    @event.listens_for(eng, "connect")
    def _set_stmt_timeout(dbapi_conn, _record):
        try:
            with dbapi_conn.cursor() as cur:
                cur.execute("SET statement_timeout = 8000")
            dbapi_conn.commit()
        except Exception:
            pass

    return eng


structured_engine = _make_engine(settings.DATABASE_URL)
templates_engine = _make_engine(settings.TEMPLATES_DB_URL)

StructuredSession = sessionmaker(bind=structured_engine, autocommit=False, autoflush=False)
TemplatesSession = sessionmaker(bind=templates_engine, autocommit=False, autoflush=False)


def get_structured_db():
    db = StructuredSession()
    try:
        yield db
    finally:
        db.close()


def get_templates_db():
    db = TemplatesSession()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """
    Ensure tables exist. The templates DB is local SQLite and must succeed.
    The structured DB is remote (Neon) — its tables already exist from the
    legacy NestJS schema, so a connect failure here is non-fatal: requests
    will retry on demand and surface clear errors if the DB is truly down.
    """
    import logging
    logger = logging.getLogger("database")
    from models import (  # noqa: F401
        person, attendance, holiday, fingerprint_template, user,
        leave_request, notification, department, leave_policy, station,
    )

    # Local SQLite — must succeed (it's a file on the same machine).
    TemplatesBase.metadata.create_all(templates_engine)

    # Ensure fingerprint template table has station_id for backward compat with existing data.
    try:
        import sqlalchemy as _sa
        with templates_engine.connect() as _conn:
            _cols = [row[1] for row in _conn.execute(_sa.text("PRAGMA table_info(fingerprint_templates_py)"))]
            if "station_id" not in _cols:
                _conn.execute(_sa.text("ALTER TABLE fingerprint_templates_py ADD COLUMN station_id INTEGER"))
                _conn.commit()
    except Exception:
        pass

    # Remote Postgres — tolerate cold-start / transient failures.
    try:
        StructuredBase.metadata.create_all(structured_engine)
        _apply_structured_migrations(structured_engine)
        _backfill_portal_users()
        _backfill_supervisor_roles()
        logger.info("Structured DB schema ensured.")
    except Exception as exc:
        logger.warning(
            "Structured DB not reachable on startup (%s) — continuing. "
            "Requests will reconnect on demand; Neon may be cold-starting.",
            exc.__class__.__name__,
        )


def _backfill_portal_users() -> None:
    """Create User rows for people who have an email but no linked portal account.

    Fixes any records where an admin added an email through Staff Roster
    before the PATCH endpoint was wired to auto-create User rows.
    """
    import logging
    from models.person import Person
    from models.user import User

    log = logging.getLogger("database")
    session = StructuredSession()
    try:
        broken = (
            session.query(Person)
            .filter(Person.email.isnot(None), Person.user_id.is_(None))
            .all()
        )
        if not broken:
            return

        # Import lazily to avoid circular imports at module load.
        from services.auth import hash_password
        default_hash = hash_password("Password123!")
        created = 0
        for person in broken:
            email = (person.email or "").lower()
            if not email:
                continue
            # Skip if a User with this email already exists (shouldn't happen but be safe).
            existing = session.query(User).filter(User.email == email).first()
            if existing:
                person.user_id = existing.id
                created += 1
                continue

            username = email
            suffix = 2
            while session.query(User).filter(User.username == username).first():
                username = f"{email}-{suffix}"
                suffix += 1

            portal_user = User(
                username=username,
                email=email,
                full_name=person.name,
                password_hash=default_hash,
                role="employee",
                is_active=True,
                must_change_password=True,
            )
            session.add(portal_user)
            session.flush()
            person.user_id = portal_user.id
            created += 1

        if created:
            session.commit()
            log.info("Portal-user backfill created %d account(s).", created)
    except Exception as exc:
        log.warning("Portal-user backfill skipped (%s).", exc.__class__.__name__)
        session.rollback()
    finally:
        session.close()


def _backfill_supervisor_roles() -> None:
    """Promote existing department supervisors from `employee` to `supervisor`.

    Fixes the pre-existing state where an admin assigned a Department's
    supervisor but the linked User.role stayed at `employee`, causing 403s
    on /api/portal/team*.
    """
    import logging
    from models.department import Department
    from models.person import Person
    from models.user import User

    log = logging.getLogger("database")
    session = StructuredSession()
    try:
        rows = (
            session.query(User)
            .join(Person, Person.user_id == User.id)
            .join(Department, Department.supervisor_person_id == Person.id)
            .filter(User.role == "employee")
            .distinct()
            .all()
        )
        if not rows:
            return
        for u in rows:
            u.role = "supervisor"
        session.commit()
        log.info("Supervisor-role backfill promoted %d account(s).", len(rows))
    except Exception as exc:
        log.warning("Supervisor-role backfill skipped (%s).", exc.__class__.__name__)
        session.rollback()
    finally:
        session.close()


# ── Lightweight schema migrations ──────────────────────────────────────────────
#
# SQLAlchemy's `create_all` creates missing tables but never alters existing ones.
# Rather than pulling in Alembic for a handful of ADD COLUMN statements, we
# check `PRAGMA table_info` / `information_schema.columns` and run a plain
# `ALTER TABLE ADD COLUMN` only when the column is missing. This keeps the
# migration idempotent on both SQLite and Postgres — SQLite doesn't support
# `ADD COLUMN IF NOT EXISTS`, which is what silently broke the previous
# revision on local dev DBs.

# (table, column, definition) — definition must be a plain-SQL column decl.
_COLUMN_MIGRATIONS: list[tuple[str, str, str]] = [
    ("people", "email", "VARCHAR(255)"),
    ("people", "department_id", "INTEGER"),
    ("people", "user_id", "INTEGER"),
    ("users", "email", "VARCHAR(255)"),
    ("users", "must_change_password", "BOOLEAN NOT NULL DEFAULT 0"),
]

# Indexes are portable — SQLite and Postgres both accept "CREATE ... IF NOT EXISTS".
_INDEX_MIGRATIONS: list[str] = [
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_people_email ON people (email)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_people_user_id ON people (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_people_department_id ON people (department_id)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email ON users (email)",
]


def _existing_columns(conn, dialect: str, table: str) -> set[str]:
    import sqlalchemy as _sa
    if dialect == "sqlite":
        rows = conn.execute(_sa.text(f"PRAGMA table_info({table})")).fetchall()
        return {row[1] for row in rows}
    # Postgres and others: rely on information_schema.
    rows = conn.execute(
        _sa.text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = :t"
        ),
        {"t": table},
    ).fetchall()
    return {row[0] for row in rows}


def _apply_structured_migrations(engine) -> None:
    import logging
    import sqlalchemy as _sa

    log = logging.getLogger("database")
    dialect = engine.dialect.name  # 'sqlite' | 'postgresql' | ...

    with engine.connect() as conn:
        # Add columns that are missing.
        cache: dict[str, set[str]] = {}
        for table, column, decl in _COLUMN_MIGRATIONS:
            try:
                if table not in cache:
                    cache[table] = _existing_columns(conn, dialect, table)
                if column in cache[table]:
                    continue
                stmt = f"ALTER TABLE {table} ADD COLUMN {column} {decl}"
                conn.execute(_sa.text(stmt))
                conn.commit()
                cache[table].add(column)
                log.info("Added column %s.%s", table, column)
            except Exception as exc:
                log.warning(
                    "Migration skipped (%s): ADD %s.%s — %s",
                    exc.__class__.__name__, table, column, exc,
                )
                conn.rollback()

        # Ensure supporting indexes.
        for stmt in _INDEX_MIGRATIONS:
            try:
                conn.execute(_sa.text(stmt))
                conn.commit()
            except Exception as exc:
                log.warning("Index skipped (%s): %s", exc.__class__.__name__, stmt)
                conn.rollback()
