"""Departments: first-class entity with a supervisor pointer."""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_structured_db
from models.department import Department
from models.person import Person
from models.user import User
from services.auth import require_admin, require_any_staff

router = APIRouter(prefix="/departments", tags=["departments"])


def _sync_supervisor_role(
    db: Session,
    old_person_id: Optional[int],
    new_person_id: Optional[int],
    exclude_dept_id: Optional[int],
) -> None:
    """Keep User.role in sync with department supervisor assignments.

    - Promote the newly-assigned supervisor's User row from `employee` to `supervisor`.
    - Demote the previous supervisor's User row back to `employee` if they no
      longer supervise any other department.

    We never touch `admin` or `attendance` roles — those are unrelated.
    """
    if old_person_id == new_person_id:
        return

    def _still_supervises(person_id: int) -> bool:
        q = db.query(Department).filter(Department.supervisor_person_id == person_id)
        if exclude_dept_id is not None:
            q = q.filter(Department.id != exclude_dept_id)
        return q.first() is not None

    if old_person_id:
        prev = db.query(Person).filter(Person.id == old_person_id).first()
        if prev and prev.user_id and not _still_supervises(old_person_id):
            linked = db.query(User).filter(User.id == prev.user_id).first()
            if linked and linked.role == "supervisor":
                linked.role = "employee"

    if new_person_id:
        nxt = db.query(Person).filter(Person.id == new_person_id).first()
        if nxt and nxt.user_id:
            linked = db.query(User).filter(User.id == nxt.user_id).first()
            if linked and linked.role == "employee":
                linked.role = "supervisor"


class CreateDepartmentDto(BaseModel):
    name: str = Field(..., min_length=1, max_length=150)
    description: Optional[str] = Field(default=None, max_length=500)
    supervisorPersonId: Optional[int] = None
    minHeadcountPresent: int = Field(default=1, ge=0, le=1000)


class UpdateDepartmentDto(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=150)
    description: Optional[str] = Field(default=None, max_length=500)
    supervisorPersonId: Optional[int] = None
    minHeadcountPresent: Optional[int] = Field(default=None, ge=0, le=1000)
    active: Optional[bool] = None


def _validate_supervisor(db: Session, person_id: Optional[int]) -> None:
    if person_id is None:
        return
    person = db.query(Person).filter(Person.id == person_id).first()
    if not person:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Supervisor person not found.")


@router.get("", response_model=List[dict])
def list_departments(
    user: dict = Depends(require_any_staff),
    db: Session = Depends(get_structured_db),
):
    return [d.to_public() for d in db.query(Department).order_by(Department.name.asc()).all()]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_department(
    dto: CreateDepartmentDto,
    user: dict = Depends(require_admin),
    db: Session = Depends(get_structured_db),
):
    name = dto.name.strip()
    if db.query(Department).filter(Department.name.ilike(name)).first():
        raise HTTPException(status.HTTP_409_CONFLICT, f'Department "{name}" already exists.')

    _validate_supervisor(db, dto.supervisorPersonId)

    dept = Department(
        name=name,
        description=(dto.description or "").strip() or None,
        supervisor_person_id=dto.supervisorPersonId,
        min_headcount_present=dto.minHeadcountPresent,
        active=True,
    )
    db.add(dept)
    db.flush()
    _sync_supervisor_role(db, None, dto.supervisorPersonId, exclude_dept_id=dept.id)
    db.commit()
    db.refresh(dept)
    return dept.to_public()


@router.patch("/{dept_id}")
def update_department(
    dept_id: int,
    dto: UpdateDepartmentDto,
    user: dict = Depends(require_admin),
    db: Session = Depends(get_structured_db),
):
    dept = db.query(Department).filter(Department.id == dept_id).first()
    if not dept:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Department not found")

    if dto.name is not None:
        new_name = dto.name.strip()
        if new_name.lower() != (dept.name or "").lower():
            conflict = (
                db.query(Department)
                .filter(Department.name.ilike(new_name), Department.id != dept_id)
                .first()
            )
            if conflict:
                raise HTTPException(status.HTTP_409_CONFLICT, f'Department "{new_name}" already exists.')
        # Keep Person.department string in sync.
        db.query(Person).filter(Person.department_id == dept_id).update(
            {Person.department: new_name}, synchronize_session=False
        )
        dept.name = new_name

    if dto.description is not None:
        dept.description = dto.description.strip() or None

    if dto.supervisorPersonId is not None:
        _validate_supervisor(db, dto.supervisorPersonId)
        old_supervisor_id = dept.supervisor_person_id
        dept.supervisor_person_id = dto.supervisorPersonId
        _sync_supervisor_role(
            db, old_supervisor_id, dto.supervisorPersonId, exclude_dept_id=dept.id,
        )

    if dto.minHeadcountPresent is not None:
        dept.min_headcount_present = dto.minHeadcountPresent

    if dto.active is not None:
        dept.active = dto.active

    db.commit()
    db.refresh(dept)
    return dept.to_public()


@router.delete("/{dept_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_department(
    dept_id: int,
    user: dict = Depends(require_admin),
    db: Session = Depends(get_structured_db),
):
    dept = db.query(Department).filter(Department.id == dept_id).first()
    if not dept:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Department not found")

    # If this department had a supervisor, they may no longer supervise anything —
    # demote them back to employee if that's the case.
    _sync_supervisor_role(db, dept.supervisor_person_id, None, exclude_dept_id=dept_id)

    # Unlink employees rather than cascading.
    db.query(Person).filter(Person.department_id == dept_id).update(
        {Person.department_id: None}, synchronize_session=False
    )
    db.delete(dept)
    db.commit()


@router.get("/{dept_id}/members")
def department_members(
    dept_id: int,
    user: dict = Depends(require_any_staff),
    db: Session = Depends(get_structured_db),
):
    dept = db.query(Department).filter(Department.id == dept_id).first()
    if not dept:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Department not found")

    rows = (
        db.query(Person)
        .filter(Person.department_id == dept_id)
        .order_by(Person.name.asc())
        .all()
    )
    return {
        "department": dept.to_public(),
        "members": [p.to_public() for p in rows],
    }
