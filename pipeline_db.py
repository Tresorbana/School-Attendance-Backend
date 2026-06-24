"""Repository helpers for fingerprint templates (TemplatesBase)."""
import pickle
from datetime import datetime
from typing import List, Optional

from sqlalchemy.orm import Session

from models.fingerprint_template import FingerprintRecord


def save_template(
    db: Session,
    person_id: str,
    template_bytes: bytes,
    raw_templates: List[bytes],
    station_id: Optional[int] = None,
) -> None:
    raw_data = pickle.dumps(raw_templates)
    existing = db.query(FingerprintRecord).filter(FingerprintRecord.person_id == person_id).first()
    if existing:
        existing.template_bytes = template_bytes
        existing.raw_templates_data = raw_data
        existing.station_id = station_id
        existing.updated_at = datetime.utcnow()
    else:
        db.add(FingerprintRecord(
            person_id=person_id,
            station_id=station_id,
            template_bytes=template_bytes,
            raw_templates_data=raw_data,
            updated_at=datetime.utcnow(),
        ))
    db.commit()


def delete_template(db: Session, person_id: str, station_id: Optional[int] = None) -> bool:
    q = db.query(FingerprintRecord).filter(FingerprintRecord.person_id == person_id)
    if station_id is not None:
        q = q.filter(FingerprintRecord.station_id == station_id)
    rec = q.first()
    if rec is None:
        return False
    db.delete(rec)
    db.commit()
    return True


def load_all_enrolled(db: Session, station_id: Optional[int] = None) -> list:
    """Return [(person_id, composite_template, [raw_templates])] filtered by station."""
    from pipeline.minutiae import FingerprintTemplate  # noqa: F401

    q = db.query(FingerprintRecord)
    if station_id is not None:
        q = q.filter(FingerprintRecord.station_id == station_id)
    records = q.all()

    result = []
    for rec in records:
        if rec.template_bytes is None:
            continue
        try:
            composite = pickle.loads(rec.template_bytes)
            composite._ensure_edges()
        except Exception:
            continue
        raw_list: list = []
        if rec.raw_templates_data:
            try:
                stored = pickle.loads(rec.raw_templates_data)
                for item in stored:
                    t = pickle.loads(item) if isinstance(item, bytes) else item
                    t._ensure_edges()
                    raw_list.append(t)
            except Exception:
                pass
        result.append((rec.person_id, composite, raw_list))
    return result
