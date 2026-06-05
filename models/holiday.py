from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String

from database import StructuredBase


class Holiday(StructuredBase):
    __tablename__ = "holidays"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(200), nullable=False)
    date = Column(String(10), unique=True, nullable=False)
    confirmed = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "date": self.date,
            "confirmed": self.confirmed,
            "createdAt": self.created_at,
        }
