from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text

from database import StructuredBase


class Person(StructuredBase):
    __tablename__ = "people"

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(String(50), unique=True, nullable=True, index=True)
    name = Column(String(255), nullable=False)
    role = Column(String(100), nullable=False, default="Employee")
    station = Column(String(150), nullable=True)
    schedule_start = Column(String(10), nullable=True)
    schedule_end = Column(String(10), nullable=True)
    fingerprint_template = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "employeeId": self.employee_id,
            "name": self.name,
            "role": self.role,
            "station": self.station,
            "scheduleStart": self.schedule_start,
            "scheduleEnd": self.schedule_end,
            "createdAt": self.created_at,
        }
