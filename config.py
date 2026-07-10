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
    # Set TRUST_PROXY=true when the backend sits behind a trusted reverse proxy (e.g., Nginx).
    # This makes the rate limiter read X-Forwarded-For for the real client IP.
    TRUST_PROXY: bool = os.environ.get("TRUST_PROXY", "false").lower() == "true"

    # ── School identity ──────────────────────────────────────────────────
    SCHOOL_NAME: str = os.environ.get("SCHOOL_NAME", "SAMS")

    # ── Auth ─────────────────────────────────────────────────────────────
    ADMIN_USERNAME: str = os.environ.get("ADMIN_USERNAME", "admin")
    ADMIN_PASSWORD: str = os.environ.get("ADMIN_PASSWORD", "admin")
    ADMIN_FULL_NAME: str = os.environ.get("ADMIN_FULL_NAME", "Administrator")
    JWT_SECRET: str = os.environ.get("JWT_SECRET", "change-me-in-prod-please")
    JWT_EXPIRES_HOURS: int = int(os.environ.get("JWT_EXPIRES_HOURS", "8"))

    # ── Databases ────────────────────────────────────────────────────────
    DATABASE_URL: str = os.environ.get(
        "DATABASE_URL",
        "postgresql+psycopg2://sams:sams_secret@localhost:5432/sams",
    )
    TEMPLATES_DB_URL: str = os.environ.get(
        "TEMPLATES_DB_URL",
        f"sqlite:///{_DEFAULT_SQLITE.as_posix()}",
    )
    DATABASE_SSLMODE: str = os.environ.get("DATABASE_SSLMODE", "require")

    # ── Attendance rules ──────────────────────────────────────────────────
    CHECKIN_COOLDOWN_MINUTES: int = int(os.environ.get("CHECKIN_COOLDOWN_MINUTES", "5"))
    LATE_GRACE_MINUTES: int = int(os.environ.get("LATE_GRACE_MINUTES", "5"))
    EARLY_DEPARTURE_GRACE_MINUTES: int = int(os.environ.get("EARLY_DEPARTURE_GRACE_MINUTES", "5"))

    # ── Static files (installer mode) ────────────────────────────────────
    STATIC_DIR: str | None = os.environ.get("STATIC_DIR") or None


settings = Settings()
