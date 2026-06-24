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


def _today_bounds():
    s = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    e = datetime.utcnow().replace(hour=23, minute=59, second=59, microsecond=999000)
    return s, e


def daily(db: Session) -> List[dict]:
    s, e = _today_bounds()
    rows = (
        db.query(Attendance.timestamp)
        .filter(Attendance.timestamp >= s, Attendance.timestamp <= e, Attendance.type == "check-in")
        .all()
    )
    by_hour: Dict[int, int] = {}
    for (ts,) in rows:
        by_hour[ts.hour] = by_hour.get(ts.hour, 0) + 1

    buckets = []
    for h in range(24):
        period = "AM" if h < 12 else "PM"
        display = 12 if h == 0 else (h - 12 if h > 12 else h)
        buckets.append({"hour": h, "count": by_hour.get(h, 0), "label": f"{display}:00 {period}"})
    return buckets


def _daily_buckets(db: Session, days: int) -> List[dict]:
    s = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1)
    rows = (
        db.query(Attendance.timestamp)
        .filter(Attendance.timestamp >= s, Attendance.type == "check-in")
        .all()
    )
    by_date: Dict[str, int] = {}
    for (ts,) in rows:
        key = ts.date().isoformat()
        by_date[key] = by_date.get(key, 0) + 1

    buckets = []
    for i in range(days - 1, -1, -1):
        d = datetime.utcnow().date() - timedelta(days=i)
        key = d.isoformat()
        label = d.strftime("%b %d").replace(" 0", " ")
        buckets.append({"date": key, "count": by_date.get(key, 0), "label": label})
    return buckets


def weekly(db: Session) -> List[dict]:
    return _daily_buckets(db, 7)


def monthly(db: Session) -> List[dict]:
    return _daily_buckets(db, 30)


def present_today(db: Session, station: Optional[str] = None) -> dict:
    s, _ = _today_bounds()
    if station:
        total = db.query(func.count(Person.id)).filter(Person.station == station).scalar() or 0
    else:
        total = db.query(func.count(Person.id)).scalar() or 0

    pq = (
        db.query(func.count(func.distinct(Attendance.person_id)))
        .filter(Attendance.timestamp >= s, Attendance.type == "check-in")
    )
    if station:
        pq = pq.join(Person, Attendance.person).filter(Person.station == station)
    present = pq.scalar() or 0
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
    start = datetime(year, month, 1)
    end = (
        datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
    ) - timedelta(milliseconds=1)
    days_in_month = (end.date() - start.date()).days + 1

    rows = (
        db.query(Attendance.timestamp, Attendance.person_id)
        .filter(Attendance.timestamp >= start, Attendance.timestamp <= end, Attendance.type == "check-in")
        .all()
    )
    by_date: Dict[str, set] = {}
    for ts, pid in rows:
        key = ts.date().isoformat()
        by_date.setdefault(key, set()).add(pid)

    result = []
    for d in range(1, days_in_month + 1):
        key = f"{year:04d}-{month:02d}-{d:02d}"
        result.append({"date": key, "count": len(by_date.get(key, ()))})
    return result


def _holiday_dates(db: Session, from_: datetime, to: datetime) -> Set[str]:
    rows = (
        db.query(Holiday.date)
        .filter(Holiday.date >= from_.date().isoformat(), Holiday.date <= to.date().isoformat())
        .all()
    )
    return {r[0] for r in rows}


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
    """Scheduled minutes = end − start. No automatic break deductions."""
    if not start or not end:
        return 0
    sh, sm = map(int, start.split(":"))
    eh, em = map(int, end.split(":"))
    return max(0, (eh * 60 + em) - (sh * 60 + sm))


def monthly_working_hours(
    db: Session,
    year: int,
    month: int,
    station: Optional[str] = None,
    person_id: Optional[int] = None,
) -> List[dict]:
    from_ = datetime(year, month, 1)
    to = (
        datetime(year + 1, 1, 1) if month == 12 else datetime(year, month + 1, 1)
    ) - timedelta(milliseconds=1)

    holidays = _holiday_dates(db, from_, to)
    sessions = get_daily_sessions(db, from_, to, station, person_id, holidays)
    required_days = _count_working_days(year, month, holidays)

    late_grace = settings.LATE_GRACE_MINUTES
    early_grace = settings.EARLY_DEPARTURE_GRACE_MINUTES

    by_person: Dict[int, List[dict]] = {}
    for s in sessions:
        by_person.setdefault(s["person_id"], []).append(s)

    rows = []
    for _pid, person_sessions in by_person.items():
        first = person_sessions[0]
        sched_min = _schedule_net_minutes(first["scheduleStart"], first["scheduleEnd"])
        present = len(person_sessions)
        total_worked = sum((s["workedMinutes"] or 0) for s in person_sessions)
        # Per-day overtime: days where worked > daily schedule
        total_overtime = sum((s.get("overtimeMinutes") or 0) for s in person_sessions)
        required_min = sched_min * required_days
        # deficitMinutes > 0 → they owe time; overtimeMinutes is monthly net excess
        net = total_worked - required_min
        deficit_min = max(0, -net)
        monthly_overtime_min = max(0, net)

        # Late: delay > grace period (positive delay = late arrival)
        late_days = sum(
            1 for s in person_sessions
            if s.get("delayMinutes") is not None and s["delayMinutes"] > late_grace
        )
        total_delay = sum(
            s["delayMinutes"]
            for s in person_sessions
            if s.get("delayMinutes") is not None and s["delayMinutes"] > late_grace
        )

        # Early departure: positive early_dep > grace → left before schedule end
        early_days = sum(
            1 for s in person_sessions
            if s.get("earlyDepartureMinutes") is not None and s["earlyDepartureMinutes"] > early_grace
        )

        missed_checkout_days = sum(1 for s in person_sessions if s.get("missedCheckout"))

        rows.append({
            "person_id": first["person_id"],
            "employee_id": first["employee_id"],
            "name": first["name"],
            "role": first["role"],
            "station": first["station"],
            "scheduleStart": first["scheduleStart"],
            "scheduleEnd": first["scheduleEnd"],
            "requiredDays": required_days,
            "presentDays": present,
            "absentDays": max(0, required_days - present),
            "totalWorkedMinutes": total_worked,
            "requiredMinutes": required_min,
            "deficitMinutes": deficit_min,          # shortfall (positive = owes time)
            "overtimeMinutes": monthly_overtime_min, # net excess for the month
            "totalDailyOvertimeMinutes": total_overtime,  # sum of per-day overtime
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
    station: Optional[str] = None,
) -> str:
    q = (
        db.query(Attendance)
        .options(joinedload(Attendance.person))
        .order_by(Attendance.timestamp.asc())
    )
    if from_:
        q = q.filter(Attendance.timestamp >= datetime.fromisoformat(from_))
    if to:
        q = q.filter(Attendance.timestamp < datetime.fromisoformat(to) + timedelta(days=1))
    if station:
        q = q.join(Person, Attendance.person).filter(Person.station == station)
    records = q.all()

    buf = StringIO()
    generated = datetime.utcnow().isoformat()
    period = f"{from_ or 'all time'} to {to or 'today'}" if (from_ or to) else "all records"
    buf.write('"Staff Attendance Management System (SAMS) — Attendance Report"\r\n')
    buf.write(f'"Generated","{generated}"\r\n')
    buf.write(f'"Period","{period}"\r\n')
    if station:
        buf.write(f'"Station","{_csv_escape(station)}"\r\n')
    buf.write(f'"Total records","{len(records)}"\r\n\r\n')
    buf.write("Date,Employee ID,Employee Name,Role,Station,Type,Time\r\n")
    for r in records:
        ts = r.timestamp
        p = r.person
        buf.write(
            f'{ts.strftime("%d/%m/%Y") if ts else ""},'
            f'"{_csv_escape(p.employee_id if p else "")}",'
            f'"{_csv_escape(p.name if p else "")}",'
            f'"{_csv_escape(p.role if p else "")}",'
            f'"{_csv_escape(p.station if p else "")}",'
            f'{r.type},'
            f'{ts.strftime("%I:%M %p") if ts else ""}\r\n'
        )
    return buf.getvalue()


def export_monthly_csv(db: Session, year: int, month: int, station: Optional[str] = None) -> str:
    rows = monthly_working_hours(db, year, month, station)
    buf = StringIO()
    month_name = datetime(year, month, 1).strftime("%B %Y")
    buf.write('"Staff Attendance Management System (SAMS)"\r\n')
    buf.write(f'"Monthly Working Hours Report — {month_name}"\r\n')
    buf.write(f'"Station","{station or "All Stations"}"\r\n')
    buf.write(f'"Generated","{datetime.utcnow().isoformat()}"\r\n\r\n')
    buf.write(
        "Employee ID,Name,Role,Station,Schedule,"
        "Required Days,Present Days,Absent Days,"
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
            f'"{_csv_escape(r["station"] or "")}",'
            f'"{sched}",'
            f'{r["requiredDays"]},{r["presentDays"]},{r["absentDays"]},'
            f'{r["requiredMinutes"]/60:.1f},{r["totalWorkedMinutes"]/60:.1f},'
            f'{r["deficitMinutes"]/60:.1f},{r["overtimeMinutes"]/60:.1f},'
            f'{r["lateDays"]},{r["totalDelayMinutes"]},'
            f'{r["earlyDepartureDays"]},{r["missedCheckoutDays"]}\r\n'
        )
    return buf.getvalue()
