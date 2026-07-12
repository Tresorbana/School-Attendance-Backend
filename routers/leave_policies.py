"""Leave policies keyed by Person.role (annual leave day quotas)."""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from database import get_structured_db
from models.leave_policy import DEFAULT_ANNUAL_DAYS, LeavePolicy
from models.person import PERSON_ROLES
from services.auth import require_admin, require_any_staff

router = APIRouter(prefix="/leave-policies", tags=["leave-policies"])


class UpsertPolicyDto(BaseModel):
    role: str = Field(..., min_length=1, max_length=100)
    annualLeaveDays: int = Field(..., ge=0, le=365)

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        v = v.strip()
        if v not in PERSON_ROLES:
            raise ValueError(f"Role must be one of: {', '.join(sorted(PERSON_ROLES))}")
        return v


def _ensure_defaults(db: Session) -> None:
    existing = {p.role for p in db.query(LeavePolicy).all()}
    added = False
    for role in PERSON_ROLES:
        if role not in existing:
            db.add(LeavePolicy(role=role, annual_leave_days=DEFAULT_ANNUAL_DAYS.get(role, 20)))
            added = True
    if added:
        db.commit()


def get_policy_for_role(db: Session, role: str) -> int:
    row: Optional[LeavePolicy] = db.query(LeavePolicy).filter(LeavePolicy.role == role).first()
    if row:
        return row.annual_leave_days
    return DEFAULT_ANNUAL_DAYS.get(role, 20)


@router.get("", response_model=List[dict])
def list_policies(
    user: dict = Depends(require_any_staff),
    db: Session = Depends(get_structured_db),
):
    _ensure_defaults(db)
    return [
        p.to_public()
        for p in db.query(LeavePolicy).order_by(LeavePolicy.role.asc()).all()
    ]


@router.put("")
def upsert_policy(
    dto: UpsertPolicyDto,
    user: dict = Depends(require_admin),
    db: Session = Depends(get_structured_db),
):
    row = db.query(LeavePolicy).filter(LeavePolicy.role == dto.role).first()
    if row:
        row.annual_leave_days = dto.annualLeaveDays
    else:
        row = LeavePolicy(role=dto.role, annual_leave_days=dto.annualLeaveDays)
        db.add(row)
    db.commit()
    db.refresh(row)
    return row.to_public()
