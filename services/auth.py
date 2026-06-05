"""Authentication: password check against env + stations table, JWT issuance/verification."""
from datetime import datetime, timedelta, timezone
from typing import Optional

import jwt
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from config import settings
from database import get_structured_db
from models.station import Station

JWT_ALGO = "HS256"


def login(username: str, password: str, db: Session) -> dict:
    """
    Validate credentials and return an AuthUser dict matching the NestJS shape.
    Raises HTTPException 401 on failure.
    """
    u_lower = username.strip().lower()

    # 1. Super-admin (from env)
    if (
        settings.ADMIN_USERNAME
        and u_lower == settings.ADMIN_USERNAME.strip().lower()
        and password == settings.ADMIN_PASSWORD
    ):
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
    if station and station.admin_password == password:
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
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"Invalid token: {exc}")


# ── FastAPI dependencies ────────────────────────────────────────────────


def current_user(authorization: str = Header(default="")) -> dict:
    """Require a valid Bearer JWT; return the decoded payload."""
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing Bearer token")
    token = authorization[7:].strip()
    return decode_token(token)


def require_super_admin(user: dict = Depends(current_user)) -> dict:
    if user.get("role") != "super-admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Super-admin required")
    return user


def require_admin_key(x_admin_key: str = Header(default="", alias="X-Admin-Key")) -> None:
    """Match the NestJS X-Admin-Key guard used on seed endpoints."""
    if not x_admin_key or x_admin_key != settings.ADMIN_PASSWORD:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or invalid X-Admin-Key")
