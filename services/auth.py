"""Authentication: env admin + users table; JWT issuance/verification.

Roles:
  admin      — full access (create users, approve leave, view reports, etc.)
  attendance — operate scanner, enroll people, view records, submit leave
"""
import hashlib
import hmac
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from config import settings
from database import get_structured_db, StructuredSession
from models.user import User

JWT_ALGO = "HS256"


# ── Password hashing ───────────────────────────────────────────────────


def hash_password(plain: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", plain.encode("utf-8"), salt, 260_000)
    return f"pbkdf2$260000${salt.hex()}${dk.hex()}"


def verify_password(plain: str, stored: str) -> bool:
    if not stored:
        return False
    if not stored.startswith("pbkdf2$"):
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


# ── Login ──────────────────────────────────────────────────────────────


def login(username: str, password: str, db: Session) -> dict:
    u_lower = username.strip().lower()

    # 1. Env admin (always first — works even if DB is empty)
    if settings.ADMIN_USERNAME and u_lower == settings.ADMIN_USERNAME.strip().lower():
        if verify_password(password, settings.ADMIN_PASSWORD):
            if not settings.ADMIN_PASSWORD.startswith("pbkdf2$"):
                hashed = hash_password(password)
                _update_env_key("ADMIN_PASSWORD", hashed)
                settings.ADMIN_PASSWORD = hashed
            return {
                "username": settings.ADMIN_USERNAME,
                "full_name": settings.ADMIN_FULL_NAME,
                "role": "admin",
            }

    # 2. Users table
    user: Optional[User] = (
        db.query(User)
        .filter(User.username == username.strip(), User.is_active.is_(True))
        .first()
    )
    if user and verify_password(password, user.password_hash):
        return {
            "username": user.username,
            "full_name": user.full_name,
            "role": user.role,
        }

    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect username or password.")


# ── Token ──────────────────────────────────────────────────────────────


def create_token(user: dict) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": user["username"],
        "role": user["role"],
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
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required")
    return decode_token(authorization[7:].strip())


def require_admin(user: dict = Depends(current_user)) -> dict:
    if user.get("role") != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Admin access required")
    return user


def require_any_staff(user: dict = Depends(current_user)) -> dict:
    """Allow admin and attendance roles."""
    if user.get("role") not in ("admin", "attendance"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Staff access required")
    return user


# ── Password change ────────────────────────────────────────────────────


def change_password(current_password: str, new_password: str, user: dict, db: Session) -> None:
    username = user.get("sub") or user.get("username")
    role = user.get("role")

    # Env admin
    if settings.ADMIN_USERNAME and username and username.lower() == settings.ADMIN_USERNAME.lower():
        if role != "admin":
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Forbidden")
        if not verify_password(current_password, settings.ADMIN_PASSWORD):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is incorrect.")
        hashed = hash_password(new_password)
        _update_env_key("ADMIN_PASSWORD", hashed)
        settings.ADMIN_PASSWORD = hashed
        return

    # Users table
    db_user = db.query(User).filter(User.username == username).first()
    if not db_user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Account not found.")
    if not verify_password(current_password, db_user.password_hash):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is incorrect.")
    db_user.password_hash = hash_password(new_password)
    db.commit()


# ── Seeding ────────────────────────────────────────────────────────────


def seed_admin_if_needed() -> None:
    """Called at startup: ensure at least one admin row exists in the users table."""
    import logging
    log = logging.getLogger("auth")
    db = StructuredSession()
    try:
        count = db.query(User).filter(User.role == "admin").count()
        if count == 0 and settings.ADMIN_USERNAME:
            log.info("No admin user in DB — env admin '%s' is primary.", settings.ADMIN_USERNAME)
    except Exception as exc:
        log.warning("Could not check users table at startup: %s", exc)
    finally:
        db.close()


# ── .env helper ────────────────────────────────────────────────────────


def _update_env_key(key: str, value: str) -> None:
    import re
    from pathlib import Path

    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        env_path = Path(".env")
    if not env_path.exists():
        return

    content = env_path.read_text(encoding="utf-8")
    pattern = re.compile(rf"^{re.escape(key)}=.*$", re.MULTILINE)
    replacement = f"{key}={value}"

    if pattern.search(content):
        new_content = pattern.sub(replacement, content)
    else:
        new_content = content.rstrip("\n") + f"\n{replacement}\n"

    env_path.write_text(new_content, encoding="utf-8")
