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

    # 1. Super-admin (from env) — constant-time compare for password
    if settings.ADMIN_USERNAME and u_lower == settings.ADMIN_USERNAME.strip().lower():
        if hmac.compare_digest(password, settings.ADMIN_PASSWORD):
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
    if not x_admin_key or x_admin_key != settings.ADMIN_PASSWORD:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or invalid X-Admin-Key")
