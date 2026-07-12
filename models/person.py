from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from database import StructuredBase

PERSON_ROLES = {"Teacher", "Staff", "Administrator", "Guard", "Contractor"}


class Person(StructuredBase):
    __tablename__ = "people"

    id = Column(Integer, primary_key=True, autoincrement=True)
    employee_id = Column(String(50), unique=True, nullable=True, index=True)
    name = Column(String(255), nullable=False)
    email = Column(String(255), unique=True, nullable=True, index=True)
    role = Column(String(100), nullable=False, default="Staff")
    department = Column(String(150), nullable=True)    # legacy free-text (kept in sync with Department.name)
    department_id = Column(
        Integer,
        ForeignKey("departments.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
        index=True,
    )
    schedule_start = Column(String(10), nullable=True)
    schedule_end = Column(String(10), nullable=True)
    fingerprint_template = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    department_ref = relationship(
        "Department",
        foreign_keys=[department_id],
        lazy="joined",
        post_update=True,
    )
    user = relationship("User", foreign_keys=[user_id], lazy="joined")

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "employeeId": self.employee_id,
            "name": self.name,
            "email": self.email,
            "role": self.role,
            "department": self.department,
            "departmentId": self.department_id,
            "userId": self.user_id,
            "scheduleStart": self.schedule_start,
            "scheduleEnd": self.schedule_end,
            "createdAt": self.created_at,
        }
