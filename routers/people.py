"""People CRUD + enrollment. Matches NestJS shapes."""
import logging
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_structured_db, get_templates_db
from models.person import Person
from models import fingerprint_template  # noqa: F401 ensure model registered

logger = logging.getLogger("people")

router = APIRouter(tags=["people"])


# ── DTOs ────────────────────────────────────────────────────────────────


class EnrollDto(BaseModel):
    employeeId: Optional[str] = None
    name: str
    role: str = "Employee"
    station: Optional[str] = None
    scheduleStart: Optional[str] = None
    scheduleEnd: Optional[str] = None
    fingerprintTemplate: str = ""
    fingerprintTemplates: Optional[List[str]] = None


class UpdatePersonDto(BaseModel):
    employeeId: Optional[str] = None
    name: Optional[str] = None
    role: Optional[str] = None
    station: Optional[str] = None
    scheduleStart: Optional[str] = None
    scheduleEnd: Optional[str] = None


# ── Helpers ─────────────────────────────────────────────────────────────


def _duplicate_check(dto: EnrollDto, db: Session) -> Optional[dict]:
    if dto.employeeId and dto.employeeId.strip():
        existing = db.query(Person).filter(Person.employee_id == dto.employeeId.strip()).first()
        if existing:
            return {
                "isDuplicate": True,
                "reason": "employee_id",
                "existingPerson": existing.to_public(),
            }

    name_match = (
        db.query(Person)
        .filter(Person.name.ilike(dto.name.strip()))
        .filter(Person.station == (dto.station.strip() if dto.station and dto.station.strip() else None))
        .first()
    )
    if name_match:
        return {
            "isDuplicate": True,
            "reason": "name",
            "existingPerson": name_match.to_public(),
        }

    return {"isDuplicate": False}


# ── /people ─────────────────────────────────────────────────────────────


@router.get("/people")
def list_people(
    station: Optional[str] = None,
    db: Session = Depends(get_structured_db),
):
    q = db.query(Person).order_by(Person.name.asc())
    if station:
        q = q.filter(Person.station == station)
    return [p.to_public() for p in q.all()]


@router.get("/people/stations")
def list_stations_from_people(db: Session = Depends(get_structured_db)):
    """Distinct stations the frontend uses for filters."""
    rows = (
        db.query(Person.station)
        .filter(Person.station.isnot(None))
        .distinct()
        .order_by(Person.station.asc())
        .all()
    )
    return [r[0] for r in rows]


@router.patch("/people/{person_id}")
def update_person(
    person_id: int,
    dto: UpdatePersonDto,
    db: Session = Depends(get_structured_db),
):
    person = db.query(Person).filter(Person.id == person_id).first()
    if not person:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Person with id {person_id} not found")

    if dto.employeeId is not None and dto.employeeId.strip():
        conflict = db.query(Person).filter(Person.employee_id == dto.employeeId.strip()).first()
        if conflict and conflict.id != person_id:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f'Employee ID "{dto.employeeId}" is already assigned to {conflict.name}',
            )

    if dto.employeeId is not None:
        person.employee_id = dto.employeeId.strip() or None
    if dto.name is not None:
        person.name = dto.name.strip()
    if dto.role is not None:
        person.role = dto.role.strip()
    if dto.station is not None:
        person.station = dto.station.strip() or None
    if dto.scheduleStart is not None:
        person.schedule_start = dto.scheduleStart or None
    if dto.scheduleEnd is not None:
        person.schedule_end = dto.scheduleEnd or None

    db.commit()
    db.refresh(person)
    return person.to_public()


@router.delete("/people/{person_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_person(
    person_id: int,
    db: Session = Depends(get_structured_db),
    templates_db: Session = Depends(get_templates_db),
):
    person = db.query(Person).filter(Person.id == person_id).first()
    if not person:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Person with id {person_id} not found")
    db.delete(person)
    db.commit()

    # Cascade-delete the fingerprint template
    from pipeline_db import delete_template
    delete_template(templates_db, str(person_id))


# ── /enroll ─────────────────────────────────────────────────────────────


@router.post("/enroll/check-duplicate")
def check_duplicate(dto: EnrollDto, db: Session = Depends(get_structured_db)):
    return _duplicate_check(dto, db)


@router.post("/enroll", status_code=status.HTTP_201_CREATED)
def enroll(
    dto: EnrollDto,
    background: BackgroundTasks,
    db: Session = Depends(get_structured_db),
):
    dup = _duplicate_check(dto, db)
    if dup["isDuplicate"]:
        reason = dup.get("reason")
        existing = dup.get("existingPerson", {})
        if reason == "employee_id":
            msg = f'Employee ID "{existing.get("employeeId")}" is already registered to {existing.get("name")}.'
        elif reason == "name":
            msg = f'An employee named "{existing.get("name")}" already exists at this station.'
        else:
            msg = "Duplicate employee detected."
        raise HTTPException(status.HTTP_409_CONFLICT, msg)

    person = Person(
        employee_id=dto.employeeId.strip() if dto.employeeId else None,
        name=dto.name.strip(),
        role=(dto.role or "Employee").strip(),
        station=dto.station.strip() if dto.station else None,
        schedule_start=dto.scheduleStart or None,
        schedule_end=dto.scheduleEnd or None,
        fingerprint_template=None,
    )
    db.add(person)
    db.commit()
    db.refresh(person)

    # Run the Python fingerprint enrollment in the background — it's heavy.
    images: List[str] = []
    if dto.fingerprintTemplates:
        images = dto.fingerprintTemplates
    elif dto.fingerprintTemplate and dto.fingerprintTemplate != "pending":
        images = [dto.fingerprintTemplate]

    if images:
        background.add_task(_enroll_fingerprint, person.id, images)

    return {"id": person.id, "name": person.name}


def _enroll_fingerprint(person_id: int, images_b64: List[str]) -> None:
    """Build a SourceAFIS template from the captured scans and persist it."""
    import base64
    from database import TemplatesSession
    from pipeline.enroll import enroll as run_enroll
    from pipeline_db import save_template

    try:
        images = [base64.b64decode(b) for b in images_b64]
        result = run_enroll(images)
        if result.get("error") or not result.get("template_bytes"):
            logger.warning(
                "Background enroll failed person_id=%s err=%s",
                person_id, result.get("error"),
            )
            return

        db = TemplatesSession()
        try:
            save_template(
                db,
                str(person_id),
                result["template_bytes"],
                result.get("raw_templates", []),
            )
            logger.info(
                "Enrolled person_id=%s steps=%s",
                person_id, ",".join(result.get("steps_applied", [])),
            )
        finally:
            db.close()
    except Exception as exc:
        logger.exception("Background enroll exception person_id=%s: %s", person_id, exc)
