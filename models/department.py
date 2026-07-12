from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from database import StructuredBase


class Department(StructuredBase):
    __tablename__ = "departments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(150), unique=True, nullable=False, index=True)
    description = Column(String(500), nullable=True)
    supervisor_person_id = Column(
        Integer,
        ForeignKey("people.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    min_headcount_present = Column(Integer, nullable=False, default=1)
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    supervisor = relationship("Person", foreign_keys=[supervisor_person_id], lazy="joined")

    def to_public(self) -> dict:
        sup = self.supervisor
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "supervisorPersonId": self.supervisor_person_id,
            "supervisorName": sup.name if sup else None,
            "supervisorEmail": getattr(sup, "email", None) if sup else None,
            "minHeadcountPresent": self.min_headcount_present,
            "active": self.active,
            "createdAt": self.created_at,
        }
