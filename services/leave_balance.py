"""Leave balance + department coverage computation."""
from datetime import date
from typing import Dict, List, Optional, Set

from sqlalchemy.orm import Session

from models.holiday import Holiday
from models.leave_policy import DEFAULT_ANNUAL_DAYS, LeavePolicy
from models.leave_request import LeaveRequest
from models.person import Person
from services.timezone import today_kigali
from services.working_days import leave_days_taken, working_day_iso_dates


def holidays_for_year(db: Session, year: int) -> Set[str]:
    rows = (
        db.query(Holiday.date)
        .filter(Holiday.date >= f"{year}-01-01", Holiday.date <= f"{year}-12-31")
        .all()
    )
    return {r[0] for r in rows}


def annual_allowance(db: Session, role: str) -> int:
    row: Optional[LeavePolicy] = db.query(LeavePolicy).filter(LeavePolicy.role == role).first()
    if row:
        return row.annual_leave_days
    return DEFAULT_ANNUAL_DAYS.get(role, 20)


def used_days_in_year(
    db: Session,
    person_id: int,
    year: int,
    holiday_dates: Optional[Set[str]] = None,
    include_pending: bool = False,
    exclude_request_id: Optional[int] = None,
) -> int:
    year_start = f"{year}-01-01"
    year_end = f"{year}-12-31"
    statuses = ["approved"]
    if include_pending:
        statuses.append("pending")

    q = (
        db.query(LeaveRequest)
        .filter(
            LeaveRequest.person_id == person_id,
            LeaveRequest.status.in_(statuses),
            LeaveRequest.to_date >= year_start,
            LeaveRequest.from_date <= year_end,
        )
    )
    if exclude_request_id is not None:
        q = q.filter(LeaveRequest.id != exclude_request_id)

    holidays = holiday_dates if holiday_dates is not None else holidays_for_year(db, year)
    total = 0
    for req in q.all():
        clamped_from = max(req.from_date, year_start)
        clamped_to = min(req.to_date, year_end)
        if clamped_from > clamped_to:
            continue
        total += leave_days_taken(clamped_from, clamped_to, holidays)
    return total


def compute_balance(db: Session, person: Person, year: Optional[int] = None) -> dict:
    y = year or today_kigali().year
    allowance = annual_allowance(db, person.role)
    holidays = holidays_for_year(db, y)
    used = used_days_in_year(db, person.id, y, holiday_dates=holidays)
    pending = used_days_in_year(
        db, person.id, y, holiday_dates=holidays, include_pending=True
    ) - used
    remaining = max(0, allowance - used - pending)
    return {
        "personId": person.id,
        "role": person.role,
        "year": y,
        "allowance": allowance,
        "used": used,
        "pending": pending,
        "remaining": remaining,
    }


def department_coverage(
    db: Session,
    department_id: Optional[int],
    from_iso: str,
    to_iso: str,
    exclude_request_id: Optional[int] = None,
) -> dict:
    """
    For a proposed leave range, compute how many people in the department are
    already on approved leave for at least one day inside the range.
    """
    if department_id is None:
        return {
            "departmentId": None,
            "departmentSize": 0,
            "overlappingAbsent": 0,
            "warning": None,
        }

    members = db.query(Person).filter(Person.department_id == department_id).all()
    total_members = len(members)
    if total_members == 0:
        return {
            "departmentId": department_id,
            "departmentSize": 0,
            "overlappingAbsent": 0,
            "warning": None,
        }

    year = int(from_iso[:4])
    holidays = holidays_for_year(db, year)
    range_dates = set(
        working_day_iso_dates(
            date.fromisoformat(from_iso),
            date.fromisoformat(to_iso),
            holidays,
        )
    )

    overlapping = 0
    absentees: List[str] = []
    for member in members:
        q = db.query(LeaveRequest).filter(
            LeaveRequest.person_id == member.id,
            LeaveRequest.status.in_(["approved", "pending"]),
            LeaveRequest.to_date >= from_iso,
            LeaveRequest.from_date <= to_iso,
        )
        if exclude_request_id is not None:
            q = q.filter(LeaveRequest.id != exclude_request_id)
        conflicts = q.all()
        member_covered_dates: Set[str] = set()
        for req in conflicts:
            member_covered_dates.update(
                working_day_iso_dates(
                    date.fromisoformat(req.from_date),
                    date.fromisoformat(req.to_date),
                    holidays,
                )
            )
        if member_covered_dates & range_dates:
            overlapping += 1
            absentees.append(member.name)

    warning = None
    from models.department import Department
    dept = db.query(Department).filter(Department.id == department_id).first()
    min_required = dept.min_headcount_present if dept else 1
    remaining_present = total_members - overlapping
    if remaining_present <= 0 and total_members > 0:
        warning = (
            f"All {total_members} member(s) of this department would be absent."
        )
    elif remaining_present < min_required:
        warning = (
            f"Only {remaining_present} of {total_members} member(s) would be present — "
            f"below the minimum of {min_required} required for this department."
        )

    return {
        "departmentId": department_id,
        "departmentSize": total_members,
        "overlappingAbsent": overlapping,
        "absentees": absentees,
        "minRequired": min_required,
        "warning": warning,
    }


def coverage_for_person(
    db: Session,
    person: Person,
    from_iso: str,
    to_iso: str,
    exclude_request_id: Optional[int] = None,
) -> dict:
    return department_coverage(
        db,
        person.department_id,
        from_iso,
        to_iso,
        exclude_request_id=exclude_request_id,
    )
