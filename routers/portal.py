"""Portal endpoints — scoped to the logged-in user (employee/supervisor)."""
from datetime import date, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from datetime import datetime
from io import StringIO

from fastapi.responses import Response

from database import get_structured_db
from models.attendance import Attendance
from models.department import Department
from models.leave_request import LeaveRequest
from models.person import Person
from models.user import User
from services.attendance import get_daily_sessions
from services.auth import (
    current_user, decode_token, require_portal_user, require_supervisor,
)
from services.leave_balance import compute_balance, department_coverage
from services.reports import daily_detail, monthly_working_hours
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


# ── Supervisor report ─────────────────────────────────────────────────────────


def _reviewer_identifiers(user: dict, person: Person) -> set[str]:
    """All strings that could appear in LeaveRequest.reviewed_by for this user."""
    ids: set[str] = set()
    if user.get("sub"):
        ids.add(user["sub"])
    if user.get("email"):
        ids.add(user["email"])
    if person.email:
        ids.add(person.email)
    if person.user_id:
        # Include the linked User.username too.
        # (Could be email or a stem — reviewed_by stores whichever the JWT held.)
        pass
    return {i for i in ids if i}


def _build_team_report(db: Session, dept: Department, user: dict, year: int, month: int) -> dict:
    """Monthly working-hours + leave activity scoped to the supervisor's department."""
    from_utc, to_utc = kigali_month_bounds_utc(year, month)
    rows = monthly_working_hours(db, year, month, department_id=dept.id)

    supervisor_person = _resolve_person(db, user)
    my_ids = _reviewer_identifiers(user, supervisor_person)

    # All leave requests for this dept overlapping the month (any status).
    leaves = (
        db.query(LeaveRequest)
        .join(Person, LeaveRequest.person_id == Person.id)
        .filter(
            Person.department_id == dept.id,
            LeaveRequest.to_date >= to_kigali(from_utc).date().isoformat(),
            LeaveRequest.from_date <= to_kigali(to_utc).date().isoformat(),
        )
        .order_by(LeaveRequest.created_at.desc())
        .all()
    )

    reviewed_by_me = [l for l in leaves if l.reviewed_by in my_ids]
    approved_by_me = [l for l in reviewed_by_me if l.status == "approved"]
    rejected_by_me = [l for l in reviewed_by_me if l.status == "rejected"]
    pending_dept   = [l for l in leaves if l.status == "pending"]

    total_worked_min = sum(r.get("totalWorkedMinutes", 0) for r in rows)
    total_overtime_min = sum(r.get("overtimeMinutes", 0) for r in rows)
    total_late_days = sum(r.get("lateDays", 0) for r in rows)
    total_absent_days = sum(r.get("absentDays", 0) for r in rows)
    total_missed_checkouts = sum(r.get("missedCheckoutDays", 0) for r in rows)

    return {
        "department": dept.to_public(),
        "period": {"year": year, "month": month},
        "supervisor": {
            "personId": supervisor_person.id,
            "name": supervisor_person.name,
            "email": supervisor_person.email,
        },
        "summary": {
            "teamSize": len(rows),
            "totalWorkedMinutes": total_worked_min,
            "totalOvertimeMinutes": total_overtime_min,
            "totalAbsentDays": total_absent_days,
            "totalLateDays": total_late_days,
            "totalMissedCheckouts": total_missed_checkouts,
            "leaveApprovedByMe": len(approved_by_me),
            "leaveRejectedByMe": len(rejected_by_me),
            "leaveStillPending": len(pending_dept),
        },
        "members": rows,
        "leaveActivity": {
            "approvedByMe": [l.to_public() for l in approved_by_me],
            "rejectedByMe": [l.to_public() for l in rejected_by_me],
            "pending": [l.to_public() for l in pending_dept],
        },
    }


@router.get("/team/daily")
def team_daily(
    date_: Optional[str] = Query(default=None, alias="date"),
    user: dict = Depends(require_supervisor),
    db: Session = Depends(get_structured_db),
):
    """Per-employee daily snapshot for the supervisor's department only."""
    dept = _supervised_department(db, user)

    if date_:
        try:
            target = date.fromisoformat(date_.strip()[:10])
        except ValueError:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "date must be YYYY-MM-DD")
    else:
        target = today_kigali()

    # Reuse the admin daily-detail engine, then filter to this dept's members.
    full = daily_detail(db, target)
    member_ids = {
        p.id for p in db.query(Person).filter(Person.department_id == dept.id).all()
    }
    rows = [r for r in full["rows"] if r["personId"] in member_ids]

    late_grace = 5
    summary = {
        "total": len(rows),
        "present": sum(1 for r in rows if r["status"] == "present"),
        "absent": sum(1 for r in rows if r["status"] == "absent"),
        "onLeave": sum(1 for r in rows if r["status"] == "on-leave"),
        "late": sum(1 for r in rows if r.get("isLate")),
        "missingCheckout": sum(1 for r in rows if r.get("missedCheckout")),
        "workingDay": full["summary"]["workingDay"],
    }

    return {
        "date": full["date"],
        "isWeekend": full["isWeekend"],
        "isHoliday": full["isHoliday"],
        "department": dept.to_public(),
        "summary": summary,
        "rows": rows,
        "minRequired": dept.min_headcount_present,
        "understaffed": summary["present"] < dept.min_headcount_present
                        and summary["workingDay"],
    }


@router.get("/team/report")
def team_report(
    year: Optional[int] = Query(default=None),
    month: Optional[int] = Query(default=None),
    user: dict = Depends(require_supervisor),
    db: Session = Depends(get_structured_db),
):
    """Monthly team report scoped to the supervisor's department.

    Includes per-member attendance rollup + which leaves this supervisor has
    approved or rejected during the month.
    """
    dept = _supervised_department(db, user)
    today = today_kigali()
    y = year or today.year
    m = month or today.month
    return _build_team_report(db, dept, user, y, m)


def _csv_escape(v) -> str:
    return ("" if v is None else str(v)).replace('"', '""')


def _team_report_csv(report: dict) -> str:
    buf = StringIO()
    dept = report["department"]
    period = report["period"]
    summary = report["summary"]
    month_name = datetime(period["year"], period["month"], 1).strftime("%B %Y")

    buf.write('"SAMS — Supervisor Report"\r\n')
    buf.write(f'"Department","{_csv_escape(dept["name"])}"\r\n')
    buf.write(f'"Supervisor","{_csv_escape(report["supervisor"]["name"])}"\r\n')
    buf.write(f'"Period","{month_name}"\r\n')
    buf.write(f'"Generated","{datetime.utcnow().isoformat()}"\r\n\r\n')

    # ── Summary
    buf.write('"Summary"\r\n')
    buf.write(f'"Team size",{summary["teamSize"]}\r\n')
    buf.write(f'"Total hours worked",{summary["totalWorkedMinutes"] / 60:.1f}\r\n')
    buf.write(f'"Total overtime (hrs)",{summary["totalOvertimeMinutes"] / 60:.1f}\r\n')
    buf.write(f'"Total absent days",{summary["totalAbsentDays"]}\r\n')
    buf.write(f'"Total late days",{summary["totalLateDays"]}\r\n')
    buf.write(f'"Missed clock-outs",{summary["totalMissedCheckouts"]}\r\n')
    buf.write(f'"Leaves I approved",{summary["leaveApprovedByMe"]}\r\n')
    buf.write(f'"Leaves I rejected",{summary["leaveRejectedByMe"]}\r\n')
    buf.write(f'"Leaves still pending",{summary["leaveStillPending"]}\r\n\r\n')

    # ── Members
    buf.write('"Team members"\r\n')
    buf.write(
        "Employee ID,Name,Role,Required days,Present days,Absent days,"
        "Leave days,Worked hrs,Overtime hrs,Late days,Missed clock-outs\r\n"
    )
    for m in report["members"]:
        buf.write(
            f'"{_csv_escape(m.get("employee_id") or "")}",'
            f'"{_csv_escape(m["name"])}",'
            f'"{_csv_escape(m["role"])}",'
            f'{m["requiredDays"]},{m["presentDays"]},{m["absentDays"]},'
            f'{m.get("leaveDays", 0)},'
            f'{m["totalWorkedMinutes"] / 60:.1f},'
            f'{m["overtimeMinutes"] / 60:.1f},'
            f'{m["lateDays"]},{m["missedCheckoutDays"]}\r\n'
        )

    # ── Leaves I reviewed
    def _write_leave_block(title: str, items: list) -> None:
        buf.write(f'\r\n"{title}"\r\n')
        if not items:
            buf.write('"None"\r\n')
            return
        buf.write("Staff,Type,From,To,Reason,Reviewed at,Notes\r\n")
        for l in items:
            buf.write(
                f'"{_csv_escape(l["personName"])}",'
                f'{l["leaveType"]},'
                f'{l["fromDate"]},{l["toDate"]},'
                f'"{_csv_escape(l.get("reason") or "")}",'
                f'{l.get("reviewedAt") or ""},'
                f'"{_csv_escape(l.get("adminNotes") or "")}"\r\n'
            )

    _write_leave_block("Leaves I approved this month", report["leaveActivity"]["approvedByMe"])
    _write_leave_block("Leaves I rejected this month", report["leaveActivity"]["rejectedByMe"])
    _write_leave_block("Leaves still pending my review", report["leaveActivity"]["pending"])

    return buf.getvalue()


def _require_download_token(token: Optional[str] = Query(default=None)) -> dict:
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required")
    return decode_token(token)


@router.get("/team/report/export")
def export_team_report(
    year: Optional[int] = Query(default=None),
    month: Optional[int] = Query(default=None),
    user: dict = Depends(_require_download_token),
    db: Session = Depends(get_structured_db),
):
    """CSV variant of the supervisor report. Uses ?token= for browser downloads."""
    # Re-check the token bearer's supervisor status.
    if user.get("role") not in ("admin", "supervisor"):
        # Fallback: DB check.
        person = _resolve_person(db, user)
        supervises = (
            db.query(Department)
            .filter(Department.supervisor_person_id == person.id)
            .first()
        )
        if not supervises:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Supervisor access required")

    dept = _supervised_department(db, user)
    today = today_kigali()
    y = year or today.year
    m = month or today.month
    report = _build_team_report(db, dept, user, y, m)
    csv_data = _team_report_csv(report)
    fname = f"sams-team-report-{dept.name.replace(' ', '_')}-{y:04d}-{m:02d}.csv"
    return Response(
        content=csv_data,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{fname}"',
            "Cache-Control": "no-store",
        },
    )


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
