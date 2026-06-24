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

    # ── Station identity ─────────────────────────────────────────────────
    # Set STATION_ID in each station's .env so this backend instance only
    # loads and matches fingerprints for its own station's employees.
    # Leave unset on the central/super-admin instance.
    STATION_ID: int | None = (
        int(os.environ["STATION_ID"])
        if os.environ.get("STATION_ID", "").strip().isdigit()
        else None
    )

    # ── Auth ─────────────────────────────────────────────────────────────
    ADMIN_USERNAME: str = os.environ.get("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD: str = os.environ.get("ADMIN_PASSWORD", "admin")
    ADMIN_FULL_NAME: str = os.environ.get("ADMIN_FULL_NAME", "Administrator")
    JWT_SECRET: str = os.environ.get("JWT_SECRET", "change-me-in-prod-please")
    JWT_EXPIRES_HOURS: int = int(os.environ.get("JWT_EXPIRES_HOURS", "8"))

    # ── Databases ────────────────────────────────────────────────────────
    DATABASE_URL: str = os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg2://attendai:attendai_secret@localhost:5432/attendai",
    )
    TEMPLATES_DB_URL: str = os.environ.get(
        "TEMPLATES_DB_URL",
        f"sqlite:///{_DEFAULT_SQLITE.as_posix()}",
    )
    DATABASE_SSLMODE: str = os.environ.get("DATABASE_SSLMODE", "require")

    # ── Attendance rules ──────────────────────────────────────────────────
    CHECKIN_COOLDOWN_MINUTES: int = int(os.environ.get("CHECKIN_COOLDOWN_MINUTES", "5"))
    # Grace periods before flagging lateness / early departure (minutes)
    LATE_GRACE_MINUTES: int = int(os.environ.get("LATE_GRACE_MINUTES", "5"))
    EARLY_DEPARTURE_GRACE_MINUTES: int = int(os.environ.get("EARLY_DEPARTURE_GRACE_MINUTES", "5"))

    # ── Static files (installer mode) ────────────────────────────────────
    # When STATIC_DIR is set the backend serves the Next.js static export
    # at the root URL — no separate Node.js server needed in the packaged exe.
    STATIC_DIR: str | None = os.environ.get("STATIC_DIR") or None


settings = Settings()
