from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_structured_db
from services.auth import change_password, create_token, current_user, login
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
