from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text

from database import StructuredBase


class Notification(StructuredBase):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    type = Column(String(50), nullable=False)          # "emergency_checkout" | "leave_request"
    message = Column(Text, nullable=False)
    person_id = Column(Integer, nullable=True)
    person_name = Column(String(255), nullable=True)
    attendance_id = Column(Integer, nullable=True)
    is_read = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "message": self.message,
            "personId": self.person_id,
            "personName": self.person_name,
            "attendanceId": self.attendance_id,
            "isRead": self.is_read,
            "createdAt": self.created_at,
        }
