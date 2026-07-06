"""Leave request management. Any authenticated staff can create; admin can approve/reject."""
from datetime import date as _date, datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from database import get_structured_db
from models.leave_request import LeaveRequest
from models.person import Person
from services.auth import require_admin, require_any_staff

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


@router.get("", response_model=List[dict])
def list_leave_requests(
    personId: Optional[int] = Query(default=None),
    status_: Optional[str] = Query(default=None, alias="status", max_length=20),
    user: dict = Depends(require_any_staff),
    db: Session = Depends(get_structured_db),
):
    q = db.query(LeaveRequest).order_by(LeaveRequest.created_at.desc())
    if personId:
        q = q.filter(LeaveRequest.person_id == personId)
    if status_:
        q = q.filter(LeaveRequest.status == status_)
    return [r.to_public() for r in q.all()]


@router.post("", status_code=status.HTTP_201_CREATED)
def create_leave_request(
    dto: CreateLeaveDto,
    user: dict = Depends(require_any_staff),
    db: Session = Depends(get_structured_db),
):
    person = db.query(Person).filter(Person.id == dto.personId).first()
    if not person:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Person not found")

    if dto.fromDate > dto.toDate:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "fromDate cannot be after toDate")

    # Check for overlapping pending/approved leaves
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
            f"An overlapping leave request already exists for this period ({overlap.from_date} – {overlap.to_date}).",
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
    return req.to_public()


@router.patch("/{leave_id}/review")
def review_leave_request(
    leave_id: int,
    dto: ReviewLeaveDto,
    user: dict = Depends(require_admin),
    db: Session = Depends(get_structured_db),
):
    req = db.query(LeaveRequest).filter(LeaveRequest.id == leave_id).first()
    if not req:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Leave request not found")
    if req.status != "pending":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Leave request is already {req.status}.")

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
    user: dict = Depends(require_admin),
    db: Session = Depends(get_structured_db),
):
    req = db.query(LeaveRequest).filter(LeaveRequest.id == leave_id).first()
    if not req:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Leave request not found")
    db.delete(req)
    db.commit()
