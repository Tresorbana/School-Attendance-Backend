"""Centralized settings loaded from .env."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

_BACKEND_DIR = Path(__file__).resolve().parent
_DEFAULT_SQLITE = _BACKEND_DIR / "fingerprint_templates.db"


class Settings:
    # ── Server ──────────────────────────────────────────────────────────
    PORT: int = int(os.environ.get("PORT", "8000"))
    HOST: str = os.environ.get("HOST", "0.0.0.0")
    CORS_ORIGIN: str = os.environ.get("CORS_ORIGIN", "*")
    NODE_ENV: str = os.environ.get("NODE_ENV", "development")

    # ── Auth ────────────────────────────────────────────────────────────
    ADMIN_USERNAME: str = os.environ.get("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD: str = os.environ.get("ADMIN_PASSWORD", "admin")
    ADMIN_FULL_NAME: str = os.environ.get("ADMIN_FULL_NAME", "Administrator")
    JWT_SECRET: str = os.environ.get("JWT_SECRET", "change-me-in-prod-please")
    JWT_EXPIRES_HOURS: int = int(os.environ.get("JWT_EXPIRES_HOURS", "12"))

    # ── Databases ───────────────────────────────────────────────────────
    # Structured data (people, attendance, stations, holidays) — Neon Postgres.
    DATABASE_URL: str = os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg2://attendai:attendai_secret@localhost:5432/attendai",
    )
    # Fingerprint template binaries — local SQLite by default. Override with
    # PYTHON_DATABASE_URL to use Postgres for the templates too.
    TEMPLATES_DB_URL: str = os.environ.get(
        "PYTHON_DATABASE_URL",
        f"sqlite:///{_DEFAULT_SQLITE.as_posix()}",
    )
    DATABASE_SSLMODE: str = os.environ.get("DATABASE_SSLMODE", "require")

    # ── Attendance / recognition ────────────────────────────────────────
    CHECKIN_COOLDOWN_MINUTES: int = int(os.environ.get("CHECKIN_COOLDOWN_MINUTES", "5"))


settings = Settings()
