from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from database import StructuredBase


class Attendance(StructuredBase):
    __tablename__ = "attendance"

    id = Column(Integer, primary_key=True, autoincrement=True)
    person_id = Column(Integer, ForeignKey("people.id", ondelete="CASCADE"), nullable=False, index=True)
    confidence = Column(Float, nullable=False)
    timestamp = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False, index=True)
    type = Column(String(20), default="check-in", nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    person = relationship("Person", lazy="joined")
