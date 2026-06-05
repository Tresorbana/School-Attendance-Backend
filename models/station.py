from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String

from database import StructuredBase


class Station(StructuredBase):
    __tablename__ = "stations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(150), unique=True, nullable=False)
    code = Column(String(30), unique=True, nullable=True)
    address = Column(String(300), nullable=True)
    admin_username = Column(String(100), unique=True, nullable=True)
    admin_password = Column(String(200), nullable=True)
    admin_full_name = Column(String(150), nullable=True)
    active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "code": self.code,
            "address": self.address,
            "adminUsername": self.admin_username,
            "adminFullName": self.admin_full_name,
            "active": self.active,
            "createdAt": self.created_at,
        }
