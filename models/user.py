from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String

from database import StructuredBase


class User(StructuredBase):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(100), unique=True, nullable=False, index=True)
    full_name = Column(String(200), nullable=False)
    password_hash = Column(String(400), nullable=False)
    role = Column(String(20), nullable=False, default="attendance")  # "admin" | "attendance"
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "fullName": self.full_name,
            "role": self.role,
            "isActive": self.is_active,
            "createdAt": self.created_at,
        }
