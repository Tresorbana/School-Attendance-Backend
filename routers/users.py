"""User management — admin only. Users are system login accounts (not people/staff)."""
import re
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from database import get_structured_db
from models.user import USER_ROLES, User
from services.auth import hash_password, require_admin

router = APIRouter(prefix="/users", tags=["users"])

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-\.@]{3,100}$")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def _clean_email(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    v = v.strip().lower()
    if not v:
        return None
    if not _EMAIL_RE.match(v):
        raise ValueError("Invalid email address")
    return v


class CreateUserDto(BaseModel):
    username: str = Field(..., min_length=3, max_length=100)
    email: Optional[str] = Field(default=None, max_length=255)
    fullName: str = Field(..., min_length=2, max_length=200)
    password: str = Field(..., min_length=8, max_length=200)
    role: str = Field(default="attendance")
    mustChangePassword: bool = False

    @field_validator("username")
    @classmethod
    def validate_username(cls, v: str) -> str:
        v = v.strip()
        if not _USERNAME_RE.match(v):
            raise ValueError("Username may only contain letters, digits, underscores, hyphens, dots and @")
        return v

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: Optional[str]) -> Optional[str]:
        return _clean_email(v)

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        if v not in USER_ROLES:
            raise ValueError(f"Role must be one of: {', '.join(sorted(USER_ROLES))}")
        return v


class UpdateUserDto(BaseModel):
    fullName: Optional[str] = Field(default=None, min_length=2, max_length=200)
    email: Optional[str] = Field(default=None, max_length=255)
    password: Optional[str] = Field(default=None, min_length=8, max_length=200)
    role: Optional[str] = None
    isActive: Optional[bool] = None
    mustChangePassword: Optional[bool] = None

    @field_validator("email")
    @classmethod
    def validate_email(cls, v: Optional[str]) -> Optional[str]:
        return _clean_email(v)

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in USER_ROLES:
            raise ValueError(f"Role must be one of: {', '.join(sorted(USER_ROLES))}")
        return v


@router.get("", response_model=List[dict])
def list_users(
    user: dict = Depends(require_admin),
    db: Session = Depends(get_structured_db),
):
    return [u.to_public() for u in db.query(User).order_by(User.username.asc()).all()]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_user(
    dto: CreateUserDto,
    user: dict = Depends(require_admin),
    db: Session = Depends(get_structured_db),
):
    conflict = db.query(User).filter(User.username == dto.username).first()
    if conflict:
        raise HTTPException(status.HTTP_409_CONFLICT, f'Username "{dto.username}" is already taken.')

    email = dto.email
    if email and db.query(User).filter(User.email == email).first():
        raise HTTPException(status.HTTP_409_CONFLICT, f'Email "{email}" is already in use.')

    new_user = User(
        username=dto.username,
        email=email,
        full_name=dto.fullName.strip(),
        password_hash=hash_password(dto.password),
        role=dto.role,
        is_active=True,
        must_change_password=dto.mustChangePassword,
    )
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    return new_user.to_public()


def _count_active_admins(db: Session) -> int:
    return db.query(User).filter(User.role == "admin", User.is_active.is_(True)).count()


@router.patch("/{user_id}")
def update_user(
    user_id: int,
    dto: UpdateUserDto,
    caller: dict = Depends(require_admin),
    db: Session = Depends(get_structured_db),
):
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    # Prevent demoting or deactivating the last DB admin.
    # (The env admin is always available as a fallback, but guard DB admins too.)
    if target.role == "admin" and target.is_active:
        would_demote = dto.role is not None and dto.role != "admin"
        would_deactivate = dto.isActive is False
        if (would_demote or would_deactivate) and _count_active_admins(db) <= 1:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "Cannot demote or deactivate the last admin account.",
            )

    if dto.fullName is not None:
        target.full_name = dto.fullName.strip()
    if dto.email is not None:
        new_email = dto.email
        if new_email != (target.email or ""):
            conflict = (
                db.query(User)
                .filter(User.email == new_email, User.id != user_id)
                .first()
            )
            if conflict:
                raise HTTPException(status.HTTP_409_CONFLICT, f'Email "{new_email}" is already in use.')
            target.email = new_email
    if dto.password is not None:
        target.password_hash = hash_password(dto.password)
        target.must_change_password = False
    if dto.role is not None:
        target.role = dto.role
    if dto.isActive is not None:
        target.is_active = dto.isActive
    if dto.mustChangePassword is not None:
        target.must_change_password = dto.mustChangePassword

    db.commit()
    db.refresh(target)
    return target.to_public()


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: int,
    caller: dict = Depends(require_admin),
    db: Session = Depends(get_structured_db),
):
    target = db.query(User).filter(User.id == user_id).first()
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "User not found")

    if target.username == caller.get("sub"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "You cannot delete your own account.")

    # Prevent deleting the last active DB admin
    if target.role == "admin" and target.is_active and _count_active_admins(db) <= 1:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Cannot delete the last admin account.",
        )

    db.delete(target)
    db.commit()
