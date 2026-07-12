import base64

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_structured_db
from services.auth import change_password, create_token, current_user, login
from services.biometric_login import biometric_login
from services.rate_limit import login_limiter

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginDto(BaseModel):
    username: str = Field(..., min_length=1, max_length=100)
    password: str = Field(..., min_length=1, max_length=200)


class ChangePasswordDto(BaseModel):
    currentPassword: str = Field(..., min_length=1)
    newPassword: str = Field(..., min_length=8)


@router.post("/login")
def login_route(
    dto: LoginDto,
    db: Session = Depends(get_structured_db),
    _=Depends(login_limiter),
):
    user = login(dto.username, dto.password, db)
    token = create_token(user)
    return {**user, "token": token}


@router.patch("/change-password", status_code=204)
def change_password_route(
    dto: ChangePasswordDto,
    user: dict = Depends(current_user),
    db: Session = Depends(get_structured_db),
):
    change_password(dto.currentPassword, dto.newPassword, user, db)


class BiometricLoginDto(BaseModel):
    image: str = Field(..., description="Base64-encoded fingerprint capture (PNG/BMP)")


@router.post("/biometric-login")
def biometric_login_route(
    dto: BiometricLoginDto,
    _=Depends(login_limiter),
):
    """Sign in via a fingerprint scan captured by the desktop bridge.

    The web portal cannot use this endpoint (browsers can't reach the scanner);
    it is intended for the desktop admin/scanner .exe only.
    """
    if not dto.image:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Missing image")
    try:
        image_bytes = base64.b64decode(dto.image, validate=False)
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid base64 image")
    return biometric_login(image_bytes)
