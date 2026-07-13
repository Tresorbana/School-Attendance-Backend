"""Reports: hourly/daily/calendar/role/working-hours, CSV export."""
from datetime import date, datetime, timedelta
from io import StringIO
from typing import Dict, List, Optional, Set

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from config import settings
from models.attendance import Attendance
from models.holiday import Holiday
from models.person import Person
from services.attendance import get_daily_sessions
from services.timezone import (
    kigali_date_key,
    kigali_day_bounds_utc,
    kigali_month_bounds_utc,
    to_kigali,
    today_kigali,
)


def _today_bounds():
    return kigali_day_bounds_utc(today_kigali())


def daily(db: Session) -> List[dict]:
    s, e = _today_bounds()
    rows = (
        db.query(Attendance.timestamp)
        .filter(Attendance.timestamp >= s, Attendance.timestamp <= e, Attendance.type == "check-in")
        .all()
    )
    by_hour: Dict[int, int] = {}
    for (ts,) in rows:
        local_hour = to_kigali(ts).hour
        by_hour[local_hour] = by_hour.get(local_hour, 0) + 1

    buckets = []
    for h in range(24):
        period = "AM" if h < 12 else "PM"
        display = 12 if h == 0 else (h - 12 if h > 12 else h)
        buckets.append({"hour": h, "count": by_hour.get(h, 0), "label": f"{display}:00 {period}"})
    return buckets


def _daily_buckets(db: Session, days: int) -> List[dict]:
    today = today_kigali()
    start_local = today - timedelta(days=days - 1)
    s, _ = kigali_day_bounds_utc(start_local)
    rows = (
        db.query(Attendance.timestamp)
        .filter(Attendance.timestamp >= s, Attendance.type == "check-in")
        .all()
    )
    by_date: Dict[str, int] = {}
    for (ts,) in rows:
        key = kigali_date_key(ts)
        by_date[key] = by_date.get(key, 0) + 1

    buckets = []
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        key = d.isoformat()
        label = d.strftime("%b %d").replace(" 0", " ")
        buckets.append({"date": key, "count": by_date.get(key, 0), "label": label})
    return buckets


def weekly(db: Session) -> List[dict]:
    return _daily_buckets(db, 7)


def monthly(db: Session) -> List[dict]:
    return _daily_buckets(db, 30)


def present_today(db: Session) -> dict:
    s, _ = _today_bounds()
    total = db.query(func.count(Person.id)).scalar() or 0
    present = (
        db.query(func.count(func.distinct(Attendance.person_id)))
        .filter(Attendance.timestamp >= s, Attendance.type == "check-in")
        .scalar() or 0
    )
    return {"present": present, "total": total, "absent": max(0, total - present)}


def by_role(db: Session) -> List[dict]:
    s, _ = _today_bounds()
    rows = (
        db.query(Person.role, func.count(func.distinct(Attendance.person_id)).label("count"))
        .join(Attendance, Attendance.person_id == Person.id)
        .filter(Attendance.timestamp >= s, Attendance.type == "check-in")
        .group_by(Person.role)
        .order_by(func.count(func.distinct(Attendance.person_id)).desc())
        .all()
    )
    return [{"role": r[0] or "Unknown", "count": int(r[1])} for r in rows]


def calendar_month(db: Session, year: int, month: int) -> List[dict]:
    start, end = kigali_month_bounds_utc(year, month)
    last_day = (
        date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    ) - timedelta(days=1)
    days_in_month = last_day.day

    rows = (
        db.query(Attendance.timestamp, Attendance.person_id)
        .filter(Attendance.timestamp >= start, Attendance.timestamp <= end, Attendance.type == "check-in")
        .all()
    )
    by_date: Dict[str, set] = {}
    for ts, pid in rows:
        key = kigali_date_key(ts)
        by_date.setdefault(key, set()).add(pid)

    result = []
    for d in range(1, days_in_month + 1):
        key = f"{year:04d}-{month:02d}-{d:02d}"
        result.append({"date": key, "count": len(by_date.get(key, ()))})
    return result


def _holiday_dates(db: Session, from_: datetime, to: datetime) -> Set[str]:
    start = to_kigali(from_).date().isoformat()
    end = to_kigali(to).date().isoformat()
    rows = (
        db.query(Holiday.date)
        .filter(Holiday.date >= start, Holiday.date <= end)
        .all()
    )
    return {r[0] for r in rows}


def _approved_leave_dates(db: Session, from_: datetime, to: datetime) -> Dict[int, Set[str]]:
    """Build a map of person_id → set of leave dates for approved leaves in the range."""
    from models.leave_request import LeaveRequest
    start = to_kigali(from_).date().isoformat()
    end = to_kigali(to).date().isoformat()
    reqs = (
        db.query(LeaveRequest)
        .filter(
            LeaveRequest.status == "approved",
            LeaveRequest.to_date >= start,
            LeaveRequest.from_date <= end,
        )
        .all()
    )
    result: Dict[int, Set[str]] = {}
    for req in reqs:
        start = date.fromisoformat(req.from_date)
        end = date.fromisoformat(req.to_date)
        cur = start
        while cur <= end:
            result.setdefault(req.person_id, set()).add(cur.isoformat())
            cur += timedelta(days=1)
    return result


def _count_working_days(year: int, month: int, holidays: Set[str]) -> int:
    last = (
        date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    ) - timedelta(days=1)
    count = 0
    for d in range(1, last.day + 1):
        dt = date(year, month, d)
        if dt.weekday() >= 5:
            continue
        if dt.isoformat() in holidays:
            continue
        count += 1
    return count


def _schedule_net_minutes(start: Optional[str], end: Optional[str]) -> int:
    if not start or not end:
        return 0
    sh, sm = map(int, start.split(":"))
    eh, em = map(int, end.split(":"))
    return max(0, (eh * 60 + em) - (sh * 60 + sm))


def daily_detail(db: Session, target: date) -> dict:
    """One row per employee for the given calendar day (Kigali time)."""
    from_utc, to_utc = kigali_day_bounds_utc(target)
    holidays = _holiday_dates(db, from_utc, to_utc)
    leave_dates = _approved_leave_dates(db, from_utc, to_utc)
    sessions_list = get_daily_sessions(db, from_utc, to_utc, None, holidays, leave_dates)
    by_person = {s["person_id"]: s for s in sessions_list}

    is_weekend = target.weekday() >= 5
    is_holiday = target.isoformat() in holidays
    late_grace = settings.LATE_GRACE_MINUTES
    early_grace = settings.EARLY_DEPARTURE_GRACE_MINUTES

    people = db.query(Person).order_by(Person.name.asc()).all()
    rows = []
    summary = {
        "total": len(people),
        "present": 0,
        "absent": 0,
        "onLeave": 0,
        "late": 0,
        "missingCheckout": 0,
        "workingDay": (not is_weekend) and (not is_holiday),
    }

    for person in people:
        session = by_person.get(person.id)
        on_leave = target.isoformat() in leave_dates.get(person.id, set())

        if is_weekend:
            status_key = "weekend"
        elif is_holiday:
            status_key = "holiday"
        elif on_leave:
            status_key = "on-leave"
            summary["onLeave"] += 1
        elif session and session.get("checkIn"):
            status_key = "present"
            summary["present"] += 1
            if session.get("delayMinutes") and session["delayMinutes"] > late_grace:
                summary["late"] += 1
            if session.get("missedCheckout"):
                summary["missingCheckout"] += 1
        else:
            status_key = "absent"
            summary["absent"] += 1

        rows.append({
            "personId": person.id,
            "employeeId": person.employee_id,
            "name": person.name,
            "email": person.email,
            "role": person.role,
            "department": person.department,
            "departmentId": person.department_id,
            "scheduleStart": person.schedule_start,
            "scheduleEnd": person.schedule_end,
            "status": status_key,
            "checkIn": session.get("checkIn") if session else None,
            "checkOut": session.get("checkOut") if session else None,
            "workedMinutes": session.get("workedMinutes") if session else None,
            "breakMinutes": session.get("breakMinutes", 0) if session else 0,
            "delayMinutes": session.get("delayMinutes") if session else None,
            "earlyDepartureMinutes": session.get("earlyDepartureMinutes") if session else None,
            "overtimeMinutes": session.get("overtimeMinutes") if session else None,
            "missedCheckout": bool(session.get("missedCheckout")) if session else False,
            "emergencyCheckout": bool(session.get("emergencyCheckout")) if session else False,
            "isLate": bool(session and session.get("delayMinutes") and session["delayMinutes"] > late_grace),
            "isEarlyLeave": bool(
                session and session.get("earlyDepartureMinutes")
                and session["earlyDepartureMinutes"] > early_grace
            ),
        })

    return {
        "date": target.isoformat(),
        "isWeekend": is_weekend,
        "isHoliday": is_holiday,
        "summary": summary,
        "rows": rows,
    }


def daily_detail_csv(db: Session, target: date) -> str:
    report = daily_detail(db, target)
    buf = StringIO()
    buf.write('"SAMS — Staff Attendance Management"\r\n')
    buf.write(f'"Daily Report — {target.isoformat()}"\r\n')
    buf.write(f'"Generated","{to_kigali(datetime.utcnow()).isoformat()}"\r\n\r\n')
    buf.write(
        "Employee ID,Name,Role,Department,Status,"
        "Check-in,Break start,Break end,Check-out,"
        "Worked (hrs),Overtime (hrs),Late (min)\r\n"
    )
    # get_daily_sessions doesn't emit break-start / break-end times separately;
    # re-query the raw events for that.
    from_utc, to_utc = kigali_day_bounds_utc(target)
    events = (
        db.query(Attendance)
        .options(joinedload(Attendance.person))
        .filter(Attendance.timestamp >= from_utc, Attendance.timestamp <= to_utc)
        .all()
    )
    break_by_person: Dict[int, Dict[str, str]] = {}
    for ev in events:
        if ev.type not in ("break-start", "break-end"):
            continue
        break_by_person.setdefault(ev.person_id, {})
        # Keep the first break-start and the last break-end.
        if ev.type == "break-start" and "start" not in break_by_person[ev.person_id]:
            break_by_person[ev.person_id]["start"] = to_kigali(ev.timestamp).strftime("%H:%M")
        elif ev.type == "break-end":
            break_by_person[ev.person_id]["end"] = to_kigali(ev.timestamp).strftime("%H:%M")

    for row in report["rows"]:
        bp = break_by_person.get(row["personId"], {})
        worked = (row["workedMinutes"] or 0) / 60
        overtime = (row["overtimeMinutes"] or 0) / 60
        late_min = row["delayMinutes"] or 0
        buf.write(
            f'"{_csv_escape(row["employeeId"] or "")}",'
            f'"{_csv_escape(row["name"])}",'
            f'"{_csv_escape(row["role"])}",'
            f'"{_csv_escape(row["department"] or "")}",'
            f'{row["status"]},'
            f'{row["checkIn"] or ""},'
            f'{bp.get("start", "")},'
            f'{bp.get("end", "")},'
            f'{row["checkOut"] or ""},'
            f'{worked:.2f},'
            f'{overtime:.2f},'
            f'{late_min}\r\n'
        )
    return buf.getvalue()


def monthly_working_hours(
    db: Session,
    year: int,
    month: int,
    person_id: Optional[int] = None,
    department_id: Optional[int] = None,
) -> List[dict]:
    from_, to = kigali_month_bounds_utc(year, month)

    holidays = _holiday_dates(db, from_, to)
    leave_dates = _approved_leave_dates(db, from_, to)
    sessions = get_daily_sessions(db, from_, to, person_id, holidays, leave_dates)
    required_days = _count_working_days(year, month, holidays)

    late_grace = settings.LATE_GRACE_MINUTES
    early_grace = settings.EARLY_DEPARTURE_GRACE_MINUTES

    # Group sessions by person for aggregate math.
    by_person: Dict[int, List[dict]] = {}
    for s in sessions:
        by_person.setdefault(s["person_id"], []).append(s)

    # IMPORTANT: enumerate every Person, not just those with attendance rows.
    # Previously, people with zero scans this month never appeared — hence the
    # "3 in the DB but only 1 in report" bug reported by admins.
    people_q = db.query(Person).order_by(Person.name.asc())
    if person_id is not None:
        people_q = people_q.filter(Person.id == person_id)
    if department_id is not None:
        people_q = people_q.filter(Person.department_id == department_id)
    people = people_q.all()

    rows = []
    for person in people:
        person_sessions = by_person.get(person.id, [])
        sched_min = _schedule_net_minutes(person.schedule_start, person.schedule_end)

        person_leave_days = len(leave_dates.get(person.id, set()))
        effective_required_days = max(0, required_days - person_leave_days)

        present = len(person_sessions)
        total_worked = sum((s["workedMinutes"] or 0) for s in person_sessions)
        total_overtime = sum((s.get("overtimeMinutes") or 0) for s in person_sessions)
        required_min = sched_min * effective_required_days
        net = total_worked - required_min
        deficit_min = max(0, -net)
        monthly_overtime_min = max(0, net)

        late_days = sum(
            1 for s in person_sessions
            if s.get("delayMinutes") is not None and s["delayMinutes"] > late_grace
        )
        total_delay = sum(
            s["delayMinutes"]
            for s in person_sessions
            if s.get("delayMinutes") is not None and s["delayMinutes"] > late_grace
        )

        early_days = sum(
            1 for s in person_sessions
            if s.get("earlyDepartureMinutes") is not None and s["earlyDepartureMinutes"] > early_grace
        )

        missed_checkout_days = sum(1 for s in person_sessions if s.get("missedCheckout"))

        rows.append({
            "person_id": person.id,
            "employee_id": person.employee_id,
            "name": person.name,
            "role": person.role,
            "department": person.department,
            "departmentId": person.department_id,
            "scheduleStart": person.schedule_start,
            "scheduleEnd": person.schedule_end,
            "requiredDays": effective_required_days,
            "leaveDays": person_leave_days,
            "presentDays": present,
            "absentDays": max(0, effective_required_days - present),
            "totalWorkedMinutes": total_worked,
            "requiredMinutes": required_min,
            "deficitMinutes": deficit_min,
            "overtimeMinutes": monthly_overtime_min,
            "totalDailyOvertimeMinutes": total_overtime,
            "lateDays": late_days,
            "totalDelayMinutes": total_delay,
            "earlyDepartureDays": early_days,
            "missedCheckoutDays": missed_checkout_days,
            "sessions": person_sessions,
        })

    rows.sort(key=lambda r: r["name"])
    return rows


# ── CSV exports ────────────────────────────────────────────────────────

def _csv_escape(value) -> str:
    s = "" if value is None else str(value)
    return s.replace('"', '""')


def export_csv(
    db: Session,
    from_: Optional[str] = None,
    to: Optional[str] = None,
) -> str:
    q = (
        db.query(Attendance)
        .options(joinedload(Attendance.person))
        .order_by(Attendance.timestamp.asc())
    )
    if from_:
        start_local = date.fromisoformat(from_)
        start_utc, _ = kigali_day_bounds_utc(start_local)
        q = q.filter(Attendance.timestamp >= start_utc)
    if to:
        end_local = date.fromisoformat(to)
        _, end_utc = kigali_day_bounds_utc(end_local)
        q = q.filter(Attendance.timestamp <= end_utc)
    records = q.all()

    buf = StringIO()
    generated = to_kigali(datetime.utcnow()).isoformat()
    period = f"{from_ or 'all time'} to {to or 'today'}" if (from_ or to) else "all records"
    buf.write('"SAMS — Staff Attendance Report"\r\n')
    buf.write(f'"Generated","{generated}"\r\n')
    buf.write(f'"Period","{period}"\r\n')
    buf.write(f'"Total records","{len(records)}"\r\n\r\n')
    buf.write("Date,Employee ID,Name,Role,Department,Type,Time,Emergency\r\n")
    for r in records:
        ts_local = to_kigali(r.timestamp) if r.timestamp else None
        p = r.person
        buf.write(
            f'{ts_local.strftime("%d/%m/%Y") if ts_local else ""},'
            f'"{_csv_escape(p.employee_id if p else "")}",'
            f'"{_csv_escape(p.name if p else "")}",'
            f'"{_csv_escape(p.role if p else "")}",'
            f'"{_csv_escape(p.department if p else "")}",'
            f'{r.type},'
            f'{ts_local.strftime("%I:%M %p") if ts_local else ""},'
            f'{"Yes" if r.is_emergency else ""}\r\n'
        )
    return buf.getvalue()


def export_monthly_csv(db: Session, year: int, month: int) -> str:
    rows = monthly_working_hours(db, year, month)
    buf = StringIO()
    month_name = datetime(year, month, 1).strftime("%B %Y")
    buf.write('"SAMS — Staff Attendance Management"\r\n')
    buf.write(f'"Monthly Working Hours Report — {month_name}"\r\n')
    buf.write(f'"Generated","{to_kigali(datetime.utcnow()).isoformat()}"\r\n\r\n')
    buf.write(
        "Employee ID,Name,Role,Department,Schedule,"
        "Required Days,Leave Days,Present Days,Absent Days,"
        "Required Hours,Worked Hours,Deficit Hours,Overtime Hours,"
        "Late Days,Total Delay (min),Early Departure Days,Missed Checkout Days\r\n"
    )
    for r in rows:
        sched = (
            f'{r["scheduleStart"]}-{r["scheduleEnd"]}'
            if r["scheduleStart"] and r["scheduleEnd"]
            else "N/A"
        )
        buf.write(
            f'"{_csv_escape(r["employee_id"] or "")}",'
            f'"{_csv_escape(r["name"])}",'
            f'"{_csv_escape(r["role"])}",'
            f'"{_csv_escape(r.get("department") or "")}",'
            f'"{sched}",'
            f'{r["requiredDays"]},{r.get("leaveDays", 0)},{r["presentDays"]},{r["absentDays"]},'
            f'{r["requiredMinutes"]/60:.1f},{r["totalWorkedMinutes"]/60:.1f},'
            f'{r["deficitMinutes"]/60:.1f},{r["overtimeMinutes"]/60:.1f},'
            f'{r["lateDays"]},{r["totalDelayMinutes"]},'
            f'{r["earlyDepartureDays"]},{r["missedCheckoutDays"]}\r\n'
        )
    return buf.getvalue()
