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
        logger.info("Structured DB schema ensured.")
    except Exception as exc:
        logger.warning(
            "Structured DB not reachable on startup (%s) — continuing. "
            "Requests will reconnect on demand; Neon may be cold-starting.",
            exc.__class__.__name__,
        )


# ── Lightweight schema migrations ──────────────────────────────────────────────
#
# SQLAlchemy's `create_all` creates missing tables but never alters existing ones.
# For a few small columns added over time it's cheaper to run idempotent
# ALTER TABLE ADD COLUMN IF NOT EXISTS statements than to pull in Alembic.
# Every new column goes through here so schema drift doesn't accumulate.

_STRUCTURED_MIGRATIONS: list[str] = [
    "ALTER TABLE people ADD COLUMN IF NOT EXISTS email VARCHAR(255)",
    "ALTER TABLE people ADD COLUMN IF NOT EXISTS department_id INTEGER",
    "ALTER TABLE people ADD COLUMN IF NOT EXISTS user_id INTEGER",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_people_email ON people (email)",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_people_user_id ON people (user_id)",
    "CREATE INDEX IF NOT EXISTS ix_people_department_id ON people (department_id)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS email VARCHAR(255)",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS must_change_password BOOLEAN NOT NULL DEFAULT FALSE",
    "CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email ON users (email)",
]


def _apply_structured_migrations(engine) -> None:
    import logging
    import sqlalchemy as _sa

    log = logging.getLogger("database")
    with engine.connect() as conn:
        for stmt in _STRUCTURED_MIGRATIONS:
            try:
                conn.execute(_sa.text(stmt))
                conn.commit()
            except Exception as exc:
                # Log-and-continue: a failure on one ALTER shouldn't block the app.
                log.warning("Migration skipped (%s): %s", exc.__class__.__name__, stmt)
                conn.rollback()
