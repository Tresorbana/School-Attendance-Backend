from datetime import datetime

from sqlalchemy import Column, DateTime, LargeBinary, String

from database import TemplatesBase


class FingerprintRecord(TemplatesBase):
    """Stores composite + individual scan SourceAFIS templates per person."""
    __tablename__ = "fingerprint_templates_py"

    person_id = Column(String, primary_key=True, index=True)
    template_bytes = Column(LargeBinary, nullable=True)
    raw_templates_data = Column(LargeBinary, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
