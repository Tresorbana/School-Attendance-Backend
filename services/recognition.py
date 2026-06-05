"""
Recognition coordinator — collapses what NestJS's RecognitionService used to do:
  run /identify → check cooldown → resolve next action → record attendance
"""
import logging
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from config import settings
from database import StructuredSession, TemplatesSession
from models.person import Person
from pipeline.match import identify as pipeline_identify
from services import attendance as att_svc
from services.template_cache import template_cache

logger = logging.getLogger("recognition")


def identify_and_record(
    image_bytes: bytes,
    mode: str = "auto",
) -> dict:
    """
    Run the SourceAFIS pipeline on the image, find the best match, then record
    attendance with the appropriate type. Returns a dict the frontend can render.
    """
    enrolled = template_cache.get()
    py = pipeline_identify(image_bytes, enrolled)

    if py.get("flag") == "low_confidence":
        logger.warning(
            "Low-confidence person_id=%s score=%s — not recording",
            py.get("person_id"), py.get("confidence_score"),
        )
        return {"matched": False, "flag": "low_confidence", "pipeline": "python"}

    if not py.get("matched") or not py.get("person_id"):
        return {"matched": False, "flag": py.get("flag"), "pipeline": "python"}

    try:
        person_id = int(py["person_id"])
    except (TypeError, ValueError):
        return {"matched": False, "error": "invalid_person_id"}

    score = py.get("confidence_score", 0) / 100.0  # store as 0–1

    db: Session = StructuredSession()
    try:
        person: Optional[Person] = db.query(Person).filter(Person.id == person_id).first()
        if not person:
            logger.warning("identify matched id=%s but person not in DB", person_id)
            return {"matched": False, "pipeline": "python"}

        # Cooldown — same record as last if within window
        last = att_svc.get_last_for_person(db, person.id)
        if last:
            elapsed_ms = (
                (datetime.utcnow() - last.timestamp).total_seconds() * 1000
                if last.timestamp
                else 9_999_999
            )
            if elapsed_ms < settings.CHECKIN_COOLDOWN_MINUTES * 60_000:
                return {
                    "matched": True,
                    "name": person.name,
                    "score": score,
                    "person_id": person.id,
                    "action": last.type,
                    "cooldown": True,
                    "pipeline": "python",
                    "mode": mode,
                }

        # Resolve action via stage machine
        stage = att_svc.get_current_stage(db, person.id)
        resolved = att_svc.resolve_action(stage, mode)
        if "error" in resolved:
            logger.warning(
                'Mode "%s" invalid for person_id=%s (stage=%s): %s',
                mode, person.id, stage, resolved["error"],
            )
            return {
                "matched": True,
                "error": resolved["error"],
                "name": person.name,
                "score": score,
                "person_id": person.id,
                "pipeline": "python",
                "mode": mode,
            }

        att_svc.record(db, person.id, score, resolved["action"])
        return {
            "matched": True,
            "name": person.name,
            "score": score,
            "person_id": person.id,
            "action": resolved["action"],
            "cooldown": False,
            "pipeline": "python",
            "mode": mode,
        }
    finally:
        db.close()
