from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String

from database import StructuredBase


DEFAULT_ANNUAL_DAYS = {
    "Teacher": 20,
    "Staff": 18,
    "Administrator": 20,
    "Guard": 15,
    "Contractor": 10,
}


class LeavePolicy(StructuredBase):
    __tablename__ = "leave_policies"

    id = Column(Integer, primary_key=True, autoincrement=True)
    role = Column(String(100), unique=True, nullable=False, index=True)
    annual_leave_days = Column(Integer, nullable=False, default=20)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    def to_public(self) -> dict:
        return {
            "id": self.id,
            "role": self.role,
            "annualLeaveDays": self.annual_leave_days,
            "updatedAt": self.updated_at,
        }
