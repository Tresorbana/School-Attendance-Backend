"""Authentication: password check against env + stations table, JWT issuance/verification."""
import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from config import settings
from database import get_structured_db
from models.station import Station

JWT_ALGO = "HS256"


def hash_password(plain: str) -> str:
    """Hash a password using PBKDF2-SHA256 with a random 16-byte salt."""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, 260_000)
    return f"pbkdf2$260000${salt.hex()}${dk.hex()}"


def verify_password(plain: str, stored: str) -> bool:
    """Verify a password against a stored hash (or legacy plain text)."""
    if not stored:
        return False
    if not stored.startswith("pbkdf2$"):
        # Legacy plain-text — constant-time comparison to prevent timing attacks
        return hmac.compare_digest(plain, stored)
    parts = stored.split("$")
    if len(parts) != 4:
        return False
    _, iters_str, salt_hex, dk_hex = parts
    try:
        salt = bytes.fromhex(salt_hex)
        dk = bytes.fromhex(dk_hex)
        new_dk = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, int(iters_str))
        return hmac.compare_digest(new_dk, dk)
    except Exception:
        return False


def login(username: str, password: str, db: Session) -> dict:
    """Validate credentials and return an AuthUser dict. Raises 401 on failure."""
    u_lower = username.strip().lower()

    # 1. Super-admin — supports both hashed (pbkdf2$...) and legacy plain-text passwords.
    #    On the first successful login with a plain-text password the hash is written
    #    back to .env automatically so the password is never left in the clear.
    if settings.ADMIN_USERNAME and u_lower == settings.ADMIN_USERNAME.strip().lower():
        if verify_password(password, settings.ADMIN_PASSWORD):
            # Auto-upgrade plain-text to hash on first use
            if not settings.ADMIN_PASSWORD.startswith("pbkdf2$"):
                hashed = hash_password(password)
                _update_env_key("ADMIN_PASSWORD", hashed)
                settings.ADMIN_PASSWORD = hashed
            return {
                "username": settings.ADMIN_USERNAME,
                "full_name": settings.ADMIN_FULL_NAME,
                "role": "super-admin",
                "station": None,
                "stationId": None,
            }

    # 2. Station-admin
    station: Optional[Station] = (
        db.query(Station)
        .filter(Station.admin_username == username.strip(), Station.active.is_(True))
        .first()
    )
    if station and verify_password(password, station.admin_password or ""):
        return {
            "username": station.admin_username,
            "full_name": station.admin_full_name or station.name,
            "role": "station-admin",
            "station": station.name,
            "stationId": station.id,
        }

    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect username or password.")


def create_token(user: dict) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user["username"],
        "role": user["role"],
        "station": user["station"],
        "stationId": user["stationId"],
        "full_name": user["full_name"],
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=settings.JWT_EXPIRES_HOURS)).timestamp()),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=JWT_ALGO)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.JWT_SECRET, algorithms=[JWT_ALGO])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Session expired — please sign in again.")
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Invalid token: {exc}")


# ── FastAPI dependencies ────────────────────────────────────────────────


def current_user(authorization: str = Header(default="")) -> dict:
    """Require a valid Bearer JWT; return the decoded payload."""
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required")
    token = authorization[7:].strip()
    return decode_token(token)


def require_super_admin(user: dict = Depends(current_user)) -> dict:
    if user.get("role") != "super-admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Super-admin access required")
    return user


def require_any_admin(user: dict = Depends(current_user)) -> dict:
    """Allow both super-admin and station-admin."""
    if user.get("role") not in ("super-admin", "station-admin"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin access required")
    return user


def require_admin_key(x_admin_key: str = Header(default="", alias="X-Admin-Key")) -> None:
    """Legacy X-Admin-Key guard used on seed endpoints."""
    if not x_admin_key or not verify_password(x_admin_key, settings.ADMIN_PASSWORD):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or invalid X-Admin-Key")


def change_password(current_password: str, new_password: str, user: dict, db: Session) -> None:
    """
    Change password for the currently logged-in admin.
    - Super-admin: updates ADMIN_PASSWORD in the .env file + live settings.
    - Station-admin: updates hashed password in the stations table.
    Raises 400 on wrong current password.
    """
    role = user.get("role")

    if role == "super-admin":
        if not verify_password(current_password, settings.ADMIN_PASSWORD):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is incorrect.")
        hashed = hash_password(new_password)
        _update_env_key("ADMIN_PASSWORD", hashed)
        settings.ADMIN_PASSWORD = hashed

    elif role == "station-admin":
        station = (
            db.query(Station)
            .filter(Station.admin_username == user.get("username"))
            .first()
        )
        if not station:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Station admin account not found.")
        if not verify_password(current_password, station.admin_password or ""):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is incorrect.")
        station.admin_password = hash_password(new_password)
        db.commit()

    else:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only admins can change passwords.")


def _update_env_key(key: str, value: str) -> None:
    """Update a single KEY=value line in the .env file, or append it if missing."""
    import re
    from pathlib import Path

    # Locate .env: next to this file's package root (backend/)
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        env_path = Path(".env")
    if not env_path.exists():
        return  # Nothing to update — settings are env-only (e.g. Docker)

    content = env_path.read_text(encoding="utf-8")
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    replacement = f"{key}={value}"

    if pattern.search(content):
        new_content = pattern.sub(replacement, content)
    else:
        new_content = content.rstrip("\n") + f"\n{replacement}\n"

    env_path.write_text(new_content, encoding="utf-8")
