"""Leave request management.

- Employees / staff can create their own requests.
- Supervisors approve/reject requests for people in their own department.
- Admins can approve/reject any request.
"""
from datetime import date as _date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from database import get_structured_db
from models.leave_request import LeaveRequest
from models.person import Person
from services.auth import require_any_staff, current_user
from services.leave_balance import (
    compute_balance,
    coverage_for_person,
    holidays_for_year,
)
from services.working_days import leave_days_taken

router = APIRouter(prefix="/leave", tags=["leave"])

LEAVE_TYPES = {"sick", "vacation", "personal"}


class CreateLeaveDto(BaseModel):
    personId: int
    leaveType: str = Field(default="sick")
    fromDate: str
    toDate: str
    reason: Optional[str] = Field(default=None, max_length=1000)

    @field_validator("leaveType")
    @classmethod
    def validate_type(cls, v: str) -> str:
        if v not in LEAVE_TYPES:
            raise ValueError(f"leaveType must be one of: {', '.join(sorted(LEAVE_TYPES))}")
        return v

    @field_validator("fromDate", "toDate")
    @classmethod
    def validate_date(cls, v: str) -> str:
        v = v.strip()
        try:
            _date.fromisoformat(v)
        except ValueError:
            raise ValueError("Date must be a valid calendar date in YYYY-MM-DD format")
        return v


class ReviewLeaveDto(BaseModel):
    status: str
    adminNotes: Optional[str] = Field(default=None, max_length=500)

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        if v not in ("approved", "rejected"):
            raise ValueError("Status must be 'approved' or 'rejected'")
        return v


# ── Helpers ────────────────────────────────────────────────────────────────────


def _lookup_current_person(db: Session, user: dict) -> Optional[Person]:
    username = user.get("sub") or user.get("username")
    if not username:
        return None
    # Prefer email-based lookup (portal users log in by email) then username.
    return (
        db.query(Person).filter(Person.email == username).first()
        or db.query(Person).filter(Person.name == user.get("full_name")).first()
    )


def _supervisor_can_review(db: Session, user: dict, person: Person) -> bool:
    """A supervisor may only review requests from people in their own department."""
    if person is None:
        return False
    from models.department import Department

    # We identify the supervisor by matching the current user's email to a
    # Person row, then checking Department.supervisor_person_id.
    sup_person = _lookup_current_person(db, user)
    if not sup_person:
        return False
    dept = (
        db.query(Department)
        .filter(Department.supervisor_person_id == sup_person.id)
        .first()
    )
    if not dept:
        return False
    return person.department_id == dept.id


def _can_review(db: Session, user: dict, target_person: Person) -> bool:
    role = user.get("role")
    if role == "admin":
        return True
    if role == "supervisor":
        return _supervisor_can_review(db, user, target_person)
    return False


# ── Routes ─────────────────────────────────────────────────────────────────────


@router.get("", response_model=List[dict])
def list_leave_requests(
    personId: Optional[int] = Query(default=None),
    status_: Optional[str] = Query(default=None, alias="status", max_length=20),
    departmentId: Optional[int] = Query(default=None),
    user: dict = Depends(require_any_staff),
    db: Session = Depends(get_structured_db),
):
    q = db.query(LeaveRequest).order_by(LeaveRequest.created_at.desc())
    if personId:
        q = q.filter(LeaveRequest.person_id == personId)
    if status_:
        q = q.filter(LeaveRequest.status == status_)
    if departmentId:
        q = q.join(Person, LeaveRequest.person_id == Person.id).filter(
            Person.department_id == departmentId
        )
    return [r.to_public() for r in q.all()]


@router.get("/balance/{person_id}")
def leave_balance(
    person_id: int,
    year: Optional[int] = Query(default=None),
    user: dict = Depends(require_any_staff),
    db: Session = Depends(get_structured_db),
):
    person = db.query(Person).filter(Person.id == person_id).first()
    if not person:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Person not found")
    return compute_balance(db, person, year)


@router.get("/coverage-preview")
def coverage_preview(
    personId: int = Query(...),
    fromDate: str = Query(...),
    toDate: str = Query(...),
    excludeRequestId: Optional[int] = Query(default=None),
    user: dict = Depends(require_any_staff),
    db: Session = Depends(get_structured_db),
):
    person = db.query(Person).filter(Person.id == personId).first()
    if not person:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Person not found")
    return coverage_for_person(
        db, person, fromDate, toDate, exclude_request_id=excludeRequestId
    )


@router.post("", status_code=status.HTTP_201_CREATED)
def create_leave_request(
    dto: CreateLeaveDto,
    user: dict = Depends(current_user),
    db: Session = Depends(get_structured_db),
):
    person = db.query(Person).filter(Person.id == dto.personId).first()
    if not person:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Person not found")

    # Employees can only submit for themselves.
    role = user.get("role")
    if role == "employee":
        me = _lookup_current_person(db, user)
        if not me or me.id != dto.personId:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "You can only submit leave requests for yourself.",
            )
    elif role not in ("admin", "supervisor", "attendance"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed")

    if dto.fromDate > dto.toDate:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "fromDate cannot be after toDate")

    # Reject overlapping non-rejected leaves for the same person.
    overlap = (
        db.query(LeaveRequest)
        .filter(
            LeaveRequest.person_id == dto.personId,
            LeaveRequest.status != "rejected",
            LeaveRequest.to_date >= dto.fromDate,
            LeaveRequest.from_date <= dto.toDate,
        )
        .first()
    )
    if overlap:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"An overlapping leave request already exists for this period "
            f"({overlap.from_date} – {overlap.to_date}).",
        )

    # Enforce annual balance (weekend/holiday-aware).
    year = int(dto.fromDate[:4])
    holidays = holidays_for_year(db, year)
    requested = leave_days_taken(dto.fromDate, dto.toDate, holidays)
    if requested <= 0:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "Selected range contains no working days (weekends/holidays only).",
        )
    balance = compute_balance(db, person, year)
    if requested > balance["remaining"]:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Requested {requested} working day(s) but only "
            f"{balance['remaining']} day(s) remain in {year} "
            f"(allowance {balance['allowance']}, used {balance['used']}, "
            f"pending {balance['pending']}).",
        )

    req = LeaveRequest(
        person_id=dto.personId,
        leave_type=dto.leaveType,
        from_date=dto.fromDate,
        to_date=dto.toDate,
        reason=dto.reason,
        status="pending",
    )
    db.add(req)
    db.commit()
    db.refresh(req)

    coverage = coverage_for_person(db, person, dto.fromDate, dto.toDate, exclude_request_id=req.id)
    return {
        **req.to_public(),
        "workingDaysRequested": requested,
        "coverage": coverage,
    }


@router.patch("/{leave_id}/review")
def review_leave_request(
    leave_id: int,
    dto: ReviewLeaveDto,
    user: dict = Depends(current_user),
    db: Session = Depends(get_structured_db),
):
    req = db.query(LeaveRequest).filter(LeaveRequest.id == leave_id).first()
    if not req:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Leave request not found")
    if req.status != "pending":
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Leave request is already {req.status}.",
        )

    person = db.query(Person).filter(Person.id == req.person_id).first()
    if not _can_review(db, user, person):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Only an admin or the department supervisor can review this request.",
        )

    req.status = dto.status
    req.reviewed_by = user.get("sub") or user.get("username")
    req.reviewed_at = datetime.utcnow()
    req.admin_notes = dto.adminNotes
    db.commit()
    db.refresh(req)
    return req.to_public()


@router.delete("/{leave_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_leave_request(
    leave_id: int,
    user: dict = Depends(current_user),
    db: Session = Depends(get_structured_db),
):
    req = db.query(LeaveRequest).filter(LeaveRequest.id == leave_id).first()
    if not req:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Leave request not found")

    role = user.get("role")
    if role == "admin":
        pass
    elif role == "employee":
        me = _lookup_current_person(db, user)
        if not me or me.id != req.person_id or req.status != "pending":
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "You can only delete your own pending requests.",
            )
    else:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Not allowed")

    db.delete(req)
    db.commit()
