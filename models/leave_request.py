from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from database import StructuredBase


class LeaveRequest(StructuredBase):
    __tablename__ = "leave_requests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    person_id = Column(Integer, ForeignKey("people.id", ondelete="CASCADE"), nullable=False, index=True)
    leave_type = Column(String(20), nullable=False, default="sick")   # sick | vacation | personal
    from_date = Column(String(10), nullable=False)                     # YYYY-MM-DD
    to_date = Column(String(10), nullable=False)                       # YYYY-MM-DD
    reason = Column(Text, nullable=True)
    status = Column(String(20), nullable=False, default="pending")     # pending | approved | rejected
    reviewed_by = Column(String(100), nullable=True)                   # username of admin who reviewed
    reviewed_at = Column(DateTime, nullable=True)
    admin_notes = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    person = relationship("Person", lazy="joined")

    def to_public(self) -> dict:
        p = self.person
        return {
            "id": self.id,
            "personId": self.person_id,
            "personName": p.name if p else "Unknown",
            "personRole": p.role if p else "Unknown",
            "leaveType": self.leave_type,
            "fromDate": self.from_date,
            "toDate": self.to_date,
            "reason": self.reason,
            "status": self.status,
            "reviewedBy": self.reviewed_by,
            "reviewedAt": self.reviewed_at,
            "adminNotes": self.admin_notes,
            "createdAt": self.created_at,
        }
