from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_structured_db
from services.auth import change_password, create_token, login, require_any_admin
from services.rate_limit import login_limiter

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginDto(BaseModel):
    username: str
    password: str


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
    user: dict = Depends(require_any_admin),
    db: Session = Depends(get_structured_db),
):
    change_password(dto.currentPassword, dto.newPassword, user, db)
