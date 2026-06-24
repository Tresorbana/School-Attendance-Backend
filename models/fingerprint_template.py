from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, LargeBinary, String

from database import TemplatesBase


class FingerprintRecord(TemplatesBase):
    """Composite + individual scan templates per person, scoped to a station."""
    __tablename__ = "fingerprint_templates_py"

    person_id = Column(String, primary_key=True, index=True)
    station_id = Column(Integer, nullable=True, index=True)   # which station owns this record
    template_bytes = Column(LargeBinary, nullable=True)
    raw_templates_data = Column(LargeBinary, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
