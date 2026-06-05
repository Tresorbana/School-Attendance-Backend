"""
Repository helpers for fingerprint templates (TemplatesBase).

Kept separate from models/ to mirror the structure of the old fingerprint-pipeline
and minimize churn when the matching cache reaches into it.
"""
import pickle
from datetime import datetime
from typing import List

from sqlalchemy.orm import Session

from models.fingerprint_template import FingerprintRecord


def save_template(
    db: Session,
    person_id: str,
    template_bytes: bytes,
    raw_templates: List[bytes],
) -> None:
    raw_data = pickle.dumps(raw_templates)
    existing = db.get(FingerprintRecord, person_id)
    if existing:
        existing.template_bytes = template_bytes
        existing.raw_templates_data = raw_data
        existing.updated_at = datetime.utcnow()
    else:
        db.add(FingerprintRecord(
            person_id=person_id,
            template_bytes=template_bytes,
            raw_templates_data=raw_data,
            updated_at=datetime.utcnow(),
        ))
    db.commit()


def delete_template(db: Session, person_id: str) -> bool:
    rec = db.get(FingerprintRecord, person_id)
    if rec is None:
        return False
    db.delete(rec)
    db.commit()
    return True


def load_all_enrolled(db: Session) -> list:
    """Return [(person_id, composite_template, [raw_templates])] for all rows."""
    from pipeline.minutiae import FingerprintTemplate  # noqa: F401  registered

    records = db.query(FingerprintRecord).all()
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
