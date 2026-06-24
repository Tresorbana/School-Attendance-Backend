"""People CRUD + enrollment. Security: JWT required; station-admins scoped to their station."""
import logging
import re
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from database import get_structured_db, get_templates_db
from models.person import Person
from models import fingerprint_template  # noqa: F401 ensure model registered
from services.auth import require_any_admin, require_super_admin

logger = logging.getLogger("people")

router = APIRouter(tags=["people"])

VALID_ROLES = {"Employee", "Manager", "Director", "Contractor", "Intern", "Admin"}
_TIME_RE = re.compile(r"^\d{2}:\d{2}$")
_EMP_ID_RE = re.compile(r"^[A-Za-z0-9\-_]{1,50}$")


class EnrollDto(BaseModel):
    employeeId: Optional[str] = Field(default=None, max_length=50)
    name: str = Field(..., min_length=2, max_length=200)
    role: str = Field(default="Employee", max_length=50)
    station: Optional[str] = Field(default=None, max_length=150)
    scheduleStart: Optional[str] = None
    scheduleEnd: Optional[str] = None
    fingerprintTemplate: str = ""
    fingerprintTemplates: Optional[List[str]] = None

    @field_validator("name")
    @classmethod
    def clean_name(cls, v: str) -> str:
        return v.strip()

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        v = v.strip()
        if v not in VALID_ROLES:
            raise ValueError(f"Role must be one of: {', '.join(sorted(VALID_ROLES))}")
        return v

    @field_validator("employeeId")
    @classmethod
    def validate_emp_id(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        v = v.strip()
        if v and not _EMP_ID_RE.match(v):
            raise ValueError("Employee ID may only contain letters, digits, hyphens and underscores")
        return v

    @field_validator("scheduleStart", "scheduleEnd")
    @classmethod
    def validate_time(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if v and not _TIME_RE.match(v):
            raise ValueError("Time must be in HH:MM format")
        return v or None


class UpdatePersonDto(BaseModel):
    employeeId: Optional[str] = Field(default=None, max_length=50)
    name: Optional[str] = Field(default=None, min_length=2, max_length=200)
    role: Optional[str] = Field(default=None, max_length=50)
    station: Optional[str] = Field(default=None, max_length=150)
    scheduleStart: Optional[str] = None
    scheduleEnd: Optional[str] = None

    @field_validator("name")
    @classmethod
    def clean_name(cls, v: Optional[str]) -> Optional[str]:
        return v.strip() if v else v

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if v and v not in VALID_ROLES:
            raise ValueError(f"Role must be one of: {', '.join(sorted(VALID_ROLES))}")
        return v

    @field_validator("scheduleStart", "scheduleEnd")
    @classmethod
    def validate_time(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if v and not _TIME_RE.match(v):
            raise ValueError("Time must be in HH:MM format")
        return v or None


def _duplicate_check(name: str, station: Optional[str], employee_id: Optional[str], db: Session) -> Optional[dict]:
    if employee_id and employee_id.strip():
        existing = db.query(Person).filter(Person.employee_id == employee_id.strip()).first()
        if existing:
            return {
                "isDuplicate": True,
                "reason": "employee_id",
                "existingPerson": existing.to_public(),
            }

    station_val = station.strip() if station and station.strip() else None
    name_match = (
        db.query(Person)
        .filter(Person.name.ilike(name.strip()))
        .filter(Person.station == station_val)
        .first()
    )
    if name_match:
        return {
            "isDuplicate": True,
            "reason": "name",
            "existingPerson": name_match.to_public(),
        }

    return {"isDuplicate": False}


@router.get("/people")
def list_people(
    station: Optional[str] = Query(default=None, max_length=150),
    user: dict = Depends(require_any_admin),
    db: Session = Depends(get_structured_db),
):
    q = db.query(Person).order_by(Person.name.asc())
    if user.get("role") == "station-admin":
        q = q.filter(Person.station == user.get("station"))
    elif station:
        q = q.filter(Person.station == station)
    return [p.to_public() for p in q.all()]


@router.get("/people/stations")
def list_stations_from_people(
    user: dict = Depends(require_any_admin),
    db: Session = Depends(get_structured_db),
):
    """Distinct stations for filter dropdowns."""
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
    user: dict = Depends(require_any_admin),
    db: Session = Depends(get_structured_db),
):
    person = db.query(Person).filter(Person.id == person_id).first()
    if not person:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Person not found")

    # IDOR guard: station-admins can only edit people in their station
    if user.get("role") == "station-admin" and person.station != user.get("station"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Access denied")

    if dto.employeeId is not None and dto.employeeId and dto.employeeId.strip():
        conflict = (
            db.query(Person)
            .filter(Person.employee_id == dto.employeeId.strip(), Person.id != person_id)
            .first()
        )
        if conflict:
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
        # Station-admins cannot reassign people to another station
        if user.get("role") != "station-admin":
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
    user: dict = Depends(require_super_admin),
    db: Session = Depends(get_structured_db),
    templates_db: Session = Depends(get_templates_db),
):
    person = db.query(Person).filter(Person.id == person_id).first()
    if not person:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Person not found")

    # Resolve station_id before deleting the person record
    station_id: Optional[int] = None
    if person.station:
        from models.station import Station
        st = db.query(Station).filter(Station.name == person.station).first()
        if st:
            station_id = st.id

    db.delete(person)
    db.commit()
    from pipeline_db import delete_template
    delete_template(templates_db, str(person_id), station_id)


@router.post("/enroll/check-duplicate")
def check_duplicate(
    dto: EnrollDto,
    user: dict = Depends(require_any_admin),
    db: Session = Depends(get_structured_db),
):
    effective_station = user.get("station") if user.get("role") == "station-admin" else dto.station
    return _duplicate_check(dto.name, effective_station, dto.employeeId, db)


@router.post("/enroll", status_code=status.HTTP_201_CREATED)
def enroll(
    dto: EnrollDto,
    background: BackgroundTasks,
    user: dict = Depends(require_any_admin),
    db: Session = Depends(get_structured_db),
):
    # Station-admin: force station to their own station, ignore whatever was sent
    effective_station = user.get("station") if user.get("role") == "station-admin" else dto.station

    dup = _duplicate_check(dto.name, effective_station, dto.employeeId, db)
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
        station=effective_station.strip() if effective_station else None,
        schedule_start=dto.scheduleStart or None,
        schedule_end=dto.scheduleEnd or None,
        fingerprint_template=None,
    )
    db.add(person)
    db.commit()
    db.refresh(person)

    images: List[str] = []
    if dto.fingerprintTemplates:
        images = dto.fingerprintTemplates
    elif dto.fingerprintTemplate and dto.fingerprintTemplate != "pending":
        images = [dto.fingerprintTemplate]

    # Resolve the station's numeric ID for fingerprint template tagging
    station_id: Optional[int] = None
    if effective_station:
        from models.station import Station
        st = db.query(Station).filter(Station.name == effective_station).first()
        if st:
            station_id = st.id

    if images:
        background.add_task(_enroll_fingerprint, person.id, images, station_id)

    return {"id": person.id, "name": person.name}


def _enroll_fingerprint(person_id: int, images_b64: List[str], station_id: Optional[int] = None) -> None:
    import base64
    from database import TemplatesSession
    from pipeline.enroll import enroll as run_enroll
    from pipeline_db import save_template

    try:
        images = [base64.b64decode(b) for b in images_b64]
        result = run_enroll(images)
        if result.get("error") or not result.get("template_bytes"):
            logger.warning("Background enroll failed person_id=%s err=%s", person_id, result.get("error"))
            return

        db = TemplatesSession()
        try:
            save_template(db, str(person_id), result["template_bytes"], result.get("raw_templates", []), station_id)
            logger.info("Enrolled person_id=%s station_id=%s steps=%s", person_id, station_id, ",".join(result.get("steps_applied", [])))
        finally:
            db.close()
        from services.template_cache import template_cache
        template_cache.invalidate()
    except Exception as exc:
        logger.exception("Background enroll exception person_id=%s: %s", person_id, exc)
