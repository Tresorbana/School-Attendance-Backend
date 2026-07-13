"""Leave policies keyed by Person.role (annual leave day quotas).

Roles are freely definable — the built-in `PERSON_ROLES` set is used only to
seed the initial policy list. Admins can add, edit or delete any role name
they want from the Leave Policies page.
"""
import re
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from database import get_structured_db
from models.leave_policy import DEFAULT_ANNUAL_DAYS, LeavePolicy
from models.person import PERSON_ROLES, Person
from services.auth import require_admin, require_any_staff

router = APIRouter(prefix="/leave-policies", tags=["leave-policies"])

_ROLE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 _\-/&]{0,49}$")


def _clean_role(v: str) -> str:
    v = (v or "").strip()
    if not v:
        raise ValueError("Role name is required")
    if not _ROLE_RE.match(v):
        raise ValueError(
            "Role must start with a letter and use only letters, numbers, "
            "spaces or - _ / & (max 50 chars)"
        )
    return v


class UpsertPolicyDto(BaseModel):
    role: str = Field(..., min_length=1, max_length=100)
    annualLeaveDays: int = Field(..., ge=0, le=365)

    @field_validator("role")
    @classmethod
    def validate_role(cls, v: str) -> str:
        return _clean_role(v)


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
    rows = db.query(LeavePolicy).order_by(LeavePolicy.role.asc()).all()
    # Attach the current headcount per role so the UI can safely block deletes.
    from sqlalchemy import func
    counts = dict(
        db.query(Person.role, func.count(Person.id))
        .group_by(Person.role)
        .all()
    )
    return [
        {**p.to_public(), "peopleCount": int(counts.get(p.role, 0))}
        for p in rows
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


@router.delete("/{role}", status_code=status.HTTP_204_NO_CONTENT)
def delete_policy(
    role: str,
    user: dict = Depends(require_admin),
    db: Session = Depends(get_structured_db),
):
    row = db.query(LeavePolicy).filter(LeavePolicy.role == role).first()
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"No leave policy for role '{role}'")

    # Refuse to delete if any staff still use this role — otherwise their
    # leave-balance calc would silently fall back to the default and the
    # roster page would keep showing an unmanaged role.
    in_use = db.query(Person).filter(Person.role == role).count()
    if in_use > 0:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{in_use} staff member(s) are still assigned to '{role}'. "
            f"Move them to a different role before deleting this policy.",
        )

    db.delete(row)
    db.commit()
