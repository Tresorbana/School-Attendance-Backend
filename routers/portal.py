"""Portal endpoints — scoped to the logged-in user (employee/supervisor)."""
from datetime import date, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from database import get_structured_db
from models.attendance import Attendance
from models.department import Department
from models.leave_request import LeaveRequest
from models.person import Person
from models.user import User
from services.attendance import get_daily_sessions
from services.auth import current_user, require_portal_user, require_supervisor
from services.leave_balance import compute_balance
from services.timezone import (
    kigali_date_key,
    kigali_day_bounds_utc,
    kigali_month_bounds_utc,
    to_kigali,
    today_kigali,
)

router = APIRouter(prefix="/portal", tags=["portal"])


# ── Identity resolution ────────────────────────────────────────────────────────

def _resolve_person(db: Session, user: dict) -> Person:
    """Locate the Person row for the current portal user."""
    identifier = user.get("email") or user.get("sub")
    if not identifier:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "No identity in token")

    # 1. Direct email match on Person.
    person = db.query(Person).filter(Person.email == identifier).first()
    if person:
        return person

    # 2. Person.user_id → User.username / email
    db_user = (
        db.query(User)
        .filter((User.username == identifier) | (User.email == identifier))
        .first()
    )
    if db_user:
        person = db.query(Person).filter(Person.user_id == db_user.id).first()
        if person:
            return person

    raise HTTPException(
        status.HTTP_404_NOT_FOUND,
        "No employee record linked to this account. Ask an admin to link your profile.",
    )


# ── Me ────────────────────────────────────────────────────────────────────────


@router.get("/me")
def me(
    user: dict = Depends(require_portal_user),
    db: Session = Depends(get_structured_db),
):
    person = _resolve_person(db, user)
    dept = None
    if person.department_id:
        dept_row = (
            db.query(Department).filter(Department.id == person.department_id).first()
        )
        if dept_row:
            dept = dept_row.to_public()

    is_supervisor = (
        db.query(Department)
        .filter(Department.supervisor_person_id == person.id)
        .first()
        is not None
    )

    return {
        **person.to_public(),
        "department": dept,
        "isSupervisor": is_supervisor,
        "role": user.get("role"),
        "mustChangePassword": bool(
            db.query(User).filter(User.id == person.user_id).first().must_change_password
            if person.user_id else False
        ),
    }


@router.get("/me/balance")
def my_balance(
    year: Optional[int] = Query(default=None),
    user: dict = Depends(require_portal_user),
    db: Session = Depends(get_structured_db),
):
    person = _resolve_person(db, user)
    return compute_balance(db, person, year)


@router.get("/me/attendance")
def my_attendance(
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = Query(default=None),
    user: dict = Depends(require_portal_user),
    db: Session = Depends(get_structured_db),
):
    person = _resolve_person(db, user)

    today = today_kigali()
    end = date.fromisoformat(to) if to else today
    start = date.fromisoformat(from_) if from_ else end - timedelta(days=29)
    start_utc, _ = kigali_day_bounds_utc(start)
    _, end_utc = kigali_day_bounds_utc(end)

    rows = (
        db.query(Attendance)
        .filter(
            Attendance.person_id == person.id,
            Attendance.timestamp >= start_utc,
            Attendance.timestamp <= end_utc,
        )
        .order_by(Attendance.timestamp.desc())
        .limit(500)
        .all()
    )
    return [
        {
            "id": r.id,
            "type": r.type,
            "timestamp": r.timestamp,
            "localTime": to_kigali(r.timestamp).isoformat(),
            "isEmergency": r.is_emergency,
        }
        for r in rows
    ]


@router.get("/me/leave")
def my_leave(
    status_: Optional[str] = Query(default=None, alias="status"),
    user: dict = Depends(require_portal_user),
    db: Session = Depends(get_structured_db),
):
    person = _resolve_person(db, user)
    q = (
        db.query(LeaveRequest)
        .filter(LeaveRequest.person_id == person.id)
        .order_by(LeaveRequest.created_at.desc())
    )
    if status_:
        q = q.filter(LeaveRequest.status == status_)
    return [r.to_public() for r in q.all()]


@router.get("/me/analytics")
def my_analytics(
    year: Optional[int] = Query(default=None),
    month: Optional[int] = Query(default=None),
    user: dict = Depends(require_portal_user),
    db: Session = Depends(get_structured_db),
):
    person = _resolve_person(db, user)
    today = today_kigali()
    y = year or today.year
    m = month or today.month
    from_utc, to_utc = kigali_month_bounds_utc(y, m)

    from services.reports import _holiday_dates, _approved_leave_dates
    holidays = _holiday_dates(db, from_utc, to_utc)
    leave_dates = _approved_leave_dates(db, from_utc, to_utc)
    sessions = get_daily_sessions(db, from_utc, to_utc, person.id, holidays, leave_dates)

    worked = sum((s["workedMinutes"] or 0) for s in sessions)
    overtime = sum((s.get("overtimeMinutes") or 0) for s in sessions)
    late_days = sum(1 for s in sessions if (s.get("delayMinutes") or 0) > 5)
    early_leave = sum(1 for s in sessions if (s.get("earlyDepartureMinutes") or 0) > 5)
    missed = sum(1 for s in sessions if s.get("missedCheckout"))

    return {
        "year": y,
        "month": m,
        "sessions": sessions,
        "summary": {
            "presentDays": len(sessions),
            "workedMinutes": worked,
            "overtimeMinutes": overtime,
            "lateDays": late_days,
            "earlyLeaveDays": early_leave,
            "missedCheckoutDays": missed,
        },
    }


# ── Team (supervisor scope) ────────────────────────────────────────────────────


def _supervised_department(db: Session, user: dict) -> Department:
    person = _resolve_person(db, user)
    dept = (
        db.query(Department)
        .filter(Department.supervisor_person_id == person.id)
        .first()
    )
    if not dept:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "You are not the supervisor of any department.",
        )
    return dept


@router.get("/team")
def my_team(
    user: dict = Depends(require_supervisor),
    db: Session = Depends(get_structured_db),
):
    dept = _supervised_department(db, user)
    members = (
        db.query(Person)
        .filter(Person.department_id == dept.id)
        .order_by(Person.name.asc())
        .all()
    )
    today = today_kigali()
    from_utc, to_utc = kigali_day_bounds_utc(today)
    events = (
        db.query(Attendance)
        .filter(
            Attendance.person_id.in_([m.id for m in members]),
            Attendance.timestamp >= from_utc,
            Attendance.timestamp <= to_utc,
        )
        .all()
    )
    present_ids = {e.person_id for e in events if e.type == "check-in"}

    return {
        "department": dept.to_public(),
        "members": [
            {
                **m.to_public(),
                "presentToday": m.id in present_ids,
            }
            for m in members
        ],
    }


@router.get("/team/leave")
def team_leave(
    status_: Optional[str] = Query(default=None, alias="status"),
    user: dict = Depends(require_supervisor),
    db: Session = Depends(get_structured_db),
):
    dept = _supervised_department(db, user)
    q = (
        db.query(LeaveRequest)
        .join(Person, LeaveRequest.person_id == Person.id)
        .filter(Person.department_id == dept.id)
        .order_by(LeaveRequest.created_at.desc())
    )
    if status_:
        q = q.filter(LeaveRequest.status == status_)
    return [r.to_public() for r in q.all()]


# ── Profile self-service ───────────────────────────────────────────────────────


from pydantic import BaseModel, Field


class UpdateMyProfileDto(BaseModel):
    name: Optional[str] = Field(default=None, min_length=2, max_length=200)
    scheduleStart: Optional[str] = None
    scheduleEnd: Optional[str] = None


@router.patch("/me")
def update_my_profile(
    dto: UpdateMyProfileDto,
    user: dict = Depends(require_portal_user),
    db: Session = Depends(get_structured_db),
):
    person = _resolve_person(db, user)
    if dto.name is not None:
        person.name = dto.name.strip()
        if person.user_id:
            linked = db.query(User).filter(User.id == person.user_id).first()
            if linked:
                linked.full_name = person.name
    # Employees can't change their schedule; only admins can.
    if user.get("role") == "admin":
        if dto.scheduleStart is not None:
            person.schedule_start = dto.scheduleStart or None
        if dto.scheduleEnd is not None:
            person.schedule_end = dto.scheduleEnd or None
    db.commit()
    db.refresh(person)
    return person.to_public()
