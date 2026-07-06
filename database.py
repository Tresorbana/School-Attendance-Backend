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
    from models import person, attendance, holiday, fingerprint_template, user, leave_request, notification  # noqa: F401

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
        logger.info("Structured DB schema ensured.")
    except Exception as exc:
        logger.warning(
            "Structured DB not reachable on startup (%s) — continuing. "
            "Requests will reconnect on demand; Neon may be cold-starting.",
            exc.__class__.__name__,
        )
