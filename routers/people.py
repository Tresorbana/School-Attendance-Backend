"""People CRUD + enrollment. Admin: full access. Attendance: can enroll and list."""
import logging
import re
from typing import List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from database import get_structured_db, get_templates_db
from models.person import Person, PERSON_ROLES
from models import fingerprint_template  # noqa: F401 ensure model registered
from services.auth import require_any_staff, require_admin

logger = logging.getLogger("people")

router = APIRouter(tags=["people"])

_TIME_RE = re.compile(r"^\d{2}:\d{2}$")
_EMP_ID_RE = re.compile(r"^[A-Za-z0-9\-_]{1,50}$")


class EnrollDto(BaseModel):
    employeeId: Optional[str] = Field(default=None, max_length=50)
    name: str = Field(..., min_length=2, max_length=200)
    role: str = Field(default="Staff", max_length=50)
    department: Optional[str] = Field(default=None, max_length=150)
    scheduleStart: Optional[str] = None
    scheduleEnd: Optional[str] = None
    fingerprintTemplate: str = ""
    fingerprintTemplates: Optional[List[str]] = None

    @field_validator("fingerprintTemplates")
    @classmethod
    def cap_templates(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        if v is not None and len(v) > 12:
            raise ValueError("fingerprintTemplates may contain at most 12 scans")
        return v

    @field_validator("name")
    @classmethod
    def clean_name(cls, v: str) -> str:
        return v.strip()

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        v = v.strip()
        if v not in PERSON_ROLES:
            raise ValueError(f"Role must be one of: {', '.join(sorted(PERSON_ROLES))}")
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
    department: Optional[str] = Field(default=None, max_length=150)
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
        if v and v not in PERSON_ROLES:
            raise ValueError(f"Role must be one of: {', '.join(sorted(PERSON_ROLES))}")
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


def _duplicate_check(name: str, employee_id: Optional[str], db: Session) -> Optional[dict]:
    if employee_id and employee_id.strip():
        existing = db.query(Person).filter(Person.employee_id == employee_id.strip()).first()
        if existing:
            return {
                "isDuplicate": True,
                "reason": "employee_id",
                "existingPerson": existing.to_public(),
            }

    name_match = (
        db.query(Person)
        .filter(Person.name.ilike(name.strip()))
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
    department: Optional[str] = Query(default=None, max_length=150),
    user: dict = Depends(require_any_staff),
    db: Session = Depends(get_structured_db),
):
    q = db.query(Person).order_by(Person.name.asc())
    if department:
        q = q.filter(Person.department == department)
    return [p.to_public() for p in q.all()]


@router.get("/people/departments")
def list_departments(
    user: dict = Depends(require_any_staff),
    db: Session = Depends(get_structured_db),
):
    rows = (
        db.query(Person.department)
        .filter(Person.department.isnot(None))
        .distinct()
        .order_by(Person.department.asc())
        .all()
    )
    return [r[0] for r in rows]


@router.patch("/people/{person_id}")
def update_person(
    person_id: int,
    dto: UpdatePersonDto,
    user: dict = Depends(require_admin),
    db: Session = Depends(get_structured_db),
):
    person = db.query(Person).filter(Person.id == person_id).first()
    if not person:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Person not found")

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
    if dto.department is not None:
        person.department = dto.department.strip() or None
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
    user: dict = Depends(require_admin),
    db: Session = Depends(get_structured_db),
    templates_db: Session = Depends(get_templates_db),
):
    person = db.query(Person).filter(Person.id == person_id).first()
    if not person:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Person not found")

    db.delete(person)
    db.commit()

    from pipeline_db import delete_template
    delete_template(templates_db, str(person_id))


@router.post("/enroll/check-duplicate")
def check_duplicate(
    dto: EnrollDto,
    user: dict = Depends(require_any_staff),
    db: Session = Depends(get_structured_db),
):
    return _duplicate_check(dto.name, dto.employeeId, db)


@router.post("/enroll", status_code=status.HTTP_201_CREATED)
def enroll(
    dto: EnrollDto,
    background: BackgroundTasks,
    user: dict = Depends(require_any_staff),
    db: Session = Depends(get_structured_db),
):
    dup = _duplicate_check(dto.name, dto.employeeId, db)
    if dup["isDuplicate"]:
        reason = dup.get("reason")
        existing = dup.get("existingPerson", {})
        if reason == "employee_id":
            msg = f'Employee ID "{existing.get("employeeId")}" is already registered to {existing.get("name")}.'
        elif reason == "name":
            msg = f'A person named "{existing.get("name")}" is already enrolled in the system.'
        else:
            msg = "Duplicate person detected."
        raise HTTPException(status.HTTP_409_CONFLICT, msg)

    person = Person(
        employee_id=dto.employeeId.strip() if dto.employeeId else None,
        name=dto.name.strip(),
        role=(dto.role or "Staff").strip(),
        department=dto.department.strip() if dto.department else None,
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

    if images:
        background.add_task(_enroll_fingerprint, person.id, images)

    return {"id": person.id, "name": person.name}


def _enroll_fingerprint(person_id: int, images_b64: List[str]) -> None:
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
            save_template(db, str(person_id), result["template_bytes"], result.get("raw_templates", []))
            logger.info("Enrolled person_id=%s steps=%s", person_id, ",".join(result.get("steps_applied", [])))
        finally:
            db.close()

        from services.template_cache import template_cache
        template_cache.invalidate()
    except Exception as exc:
        logger.exception("Background enroll exception person_id=%s: %s", person_id, exc)
