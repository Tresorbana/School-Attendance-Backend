from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String

from database import StructuredBase


# Roles:
#   admin      — full access
#   supervisor — approves leave for their department
#   attendance — scanner operator (station attendant)
#   employee   — portal-only self-service user
USER_ROLES = {"admin", "supervisor", "attendance", "employee"}


class User(StructuredBase):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(100), unique=True, nullable=False, index=True)
    email = Column(String(255), unique=True, nullable=True, index=True)
    full_name = Column(String(200), nullable=False)
    password_hash = Column(String(400), nullable=False)
    role = Column(String(20), nullable=False, default="employee")
    is_active = Column(Boolean, default=True, nullable=False)
    must_change_password = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "username": self.username,
            "email": self.email,
            "fullName": self.full_name,
            "role": self.role,
            "isActive": self.is_active,
            "mustChangePassword": self.must_change_password,
            "createdAt": self.created_at,
        }
