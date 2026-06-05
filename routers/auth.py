from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_structured_db
from services.auth import create_token, login
from services.rate_limit import login_limiter

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginDto(BaseModel):
    username: str
    password: str


@router.post("/login")
def login_route(
    dto: LoginDto,
    db: Session = Depends(get_structured_db),
    _=Depends(login_limiter),
):
    user = login(dto.username, dto.password, db)
    token = create_token(user)
    return {**user, "token": token}
