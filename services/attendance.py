"""Attendance logic: recording, stage machine, daily sessions for reports."""
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from config import settings
from models.attendance import Attendance
from models.person import Person

AttendanceType = str   # 'check-in' | 'check-out' | 'break-start' | 'break-end'
AttendanceStage = str  # 'checked-out' | 'checked-in' | 'on-break'

# 4-scan labels for the school context
SCAN_LABELS = {
    "check-in":    "Morning arrival",
    "break-start": "Break started",
    "break-end":   "Break returned",
    "check-out":   "End of day",
}


# ── Core recording ─────────────────────────────────────────────────────

def record(
    db: Session,
    person_id: int,
    confidence: float,
    type_: AttendanceType = "check-in",
    is_emergency: bool = False,
    notes: Optional[str] = None,
) -> Attendance:
    entry = Attendance(
        person_id=person_id,
        confidence=confidence,
        type=type_,
        is_emergency=is_emergency,
        notes=notes,
        timestamp=datetime.utcnow(),
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry


def get_last_for_person(db: Session, person_id: int) -> Optional[Attendance]:
    return (
        db.query(Attendance)
        .filter(Attendance.person_id == person_id)
        .order_by(Attendance.timestamp.desc())
        .first()
    )


def get_current_stage(db: Session, person_id: int) -> AttendanceStage:
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    todays = (
        db.query(Attendance)
        .filter(Attendance.person_id == person_id, Attendance.timestamp >= today_start)
        .order_by(Attendance.timestamp.desc())
        .limit(20)
        .all()
    )
    if not todays:
        return "checked-out"
    latest = todays[0]
    if latest.type == "check-out":
        return "checked-out"
    if latest.type == "break-start":
        return "on-break"
    return "checked-in"


def next_action(stage: AttendanceStage) -> AttendanceType:
    return {
        "checked-out": "check-in",
        "checked-in":  "check-out",
        "on-break":    "break-end",
    }[stage]


def resolve_action(stage: AttendanceStage, mode: str) -> dict:
    """
    Resolve what attendance action to record given the current stage and requested mode.

    Modes:
      auto              — follow the default 4-scan sequence
      break-start       — explicitly go on break (scan 2)
      break-end         — explicitly return from break (scan 3)
      check-out         — end of day (scan 4)
      emergency-checkout — going home early; records check-out + sets is_emergency=True
    """
    if mode == "auto":
        return {"action": next_action(stage)}

    if mode == "break-start":
        if stage == "checked-out":
            return {"error": "not_checked_in"}
        if stage == "on-break":
            return {"error": "already_on_break"}
        return {"action": "break-start"}

    if mode == "break-end":
        if stage != "on-break":
            return {"error": "not_on_break"}
        return {"action": "break-end"}

    if mode == "check-out":
        if stage == "checked-out":
            return {"error": "not_checked_in"}
        if stage == "on-break":
            return {"error": "still_on_break"}
        return {"action": "check-out"}

    if mode == "emergency-checkout":
        if stage == "checked-out":
            return {"error": "not_checked_in"}
        # Allow emergency checkout even if on break — it's an emergency
        return {"action": "check-out", "is_emergency": True}

    return {"error": "unknown_mode"}


# ── Query helpers ──────────────────────────────────────────────────────

def _format(records: List[Attendance]) -> List[dict]:
    out = []
    for r in records:
        p = r.person
        out.append({
            "id": r.id,
            "person_id": r.person_id,
            "employee_id": p.employee_id if p else None,
            "name": p.name if p else "Unknown",
            "role": p.role if p else "Unknown",
            "department": p.department if p else None,
            "timestamp": r.timestamp,
            "confidence": r.confidence,
            "type": r.type,
            "isEmergency": r.is_emergency,
            "notes": r.notes,
        })
    return out


def get_recent(db: Session, limit: int = 20) -> List[dict]:
    q = (
        db.query(Attendance)
        .options(joinedload(Attendance.person))
        .order_by(Attendance.timestamp.desc())
        .limit(limit)
    )
    return _format(q.all())


_VALID_TYPES = {"check-in", "check-out", "break-start", "break-end"}


def _parse_date_param(value: str) -> Optional[datetime]:
    """Parse a YYYY-MM-DD query parameter; returns None on invalid input (never 500s)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.strip()[:10])
    except ValueError:
        return None


def get_all(
    db: Session,
    from_: Optional[str] = None,
    to: Optional[str] = None,
    search: Optional[str] = None,
    person_id: Optional[int] = None,
    type_: Optional[str] = None,
) -> List[dict]:
    q = (
        db.query(Attendance)
        .options(joinedload(Attendance.person))
        .order_by(Attendance.timestamp.desc())
        .limit(500)
    )
    if from_:
        dt = _parse_date_param(from_)
        if dt:
            q = q.filter(Attendance.timestamp >= dt)
    if to:
        dt = _parse_date_param(to)
        if dt:
            q = q.filter(Attendance.timestamp < dt + timedelta(days=1))
    if person_id:
        q = q.filter(Attendance.person_id == person_id)
    if type_ and type_ in _VALID_TYPES:
        q = q.filter(Attendance.type == type_)
    if search and search.strip():
        q = q.join(Person, Attendance.person).filter(
            Person.name.ilike(f"%{search.strip()[:100]}%")
        )
    return _format(q.all())


def get_stats(db: Session) -> dict:
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    total_people = db.query(func.count(Person.id)).scalar() or 0

    today_count = (
        db.query(func.count(func.distinct(Attendance.person_id)))
        .filter(Attendance.timestamp >= today_start, Attendance.type == "check-in")
        .scalar() or 0
    )

    last = (
        db.query(Attendance)
        .options(joinedload(Attendance.person))
        .filter(Attendance.type == "check-in")
        .order_by(Attendance.timestamp.desc())
        .first()
    )

    attendance_rate = round((today_count / total_people) * 100) if total_people > 0 else 0
    return {
        "totalPeople": total_people,
        "todayCount": today_count,
        "attendanceRate": attendance_rate,
        "lastCheckIn": last.timestamp if last else None,
    }


# ── Daily session builder (for reports) ────────────────────────────────

def _to_hhmm(d: datetime) -> str:
    return d.strftime("%H:%M")


def _time_to_min(t: str) -> int:
    h, m = map(int, t.split(":"))
    return h * 60 + m


def _schedule_net_minutes(start: str, end: str) -> int:
    return max(0, _time_to_min(end) - _time_to_min(start))


def get_daily_sessions(
    db: Session,
    from_: datetime,
    to: datetime,
    person_id: Optional[int] = None,
    holiday_dates: Optional[Set[str]] = None,
    approved_leave_dates: Optional[Dict[int, Set[str]]] = None,
) -> List[dict]:
    holiday_dates = holiday_dates or set()
    approved_leave_dates = approved_leave_dates or {}

    q = (
        db.query(Attendance)
        .options(joinedload(Attendance.person))
        .filter(Attendance.timestamp >= from_, Attendance.timestamp <= to)
        .order_by(Attendance.timestamp.asc())
    )
    if person_id:
        q = q.filter(Attendance.person_id == person_id)

    records = q.all()

    grouped: Dict[int, Dict[str, List[Attendance]]] = {}
    for r in records:
        date_key = r.timestamp.strftime("%Y-%m-%d")
        grouped.setdefault(r.person_id, {}).setdefault(date_key, []).append(r)

    sessions: List[dict] = []
    for _pid, by_date in grouped.items():
        for date_key, recs in by_date.items():
            d = datetime.strptime(date_key, "%Y-%m-%d")
            if d.weekday() >= 5:
                continue
            if date_key in holiday_dates:
                continue
            # Skip if person has approved leave for this date
            person_leave = approved_leave_dates.get(_pid, set())
            if date_key in person_leave:
                continue

            recs_sorted = sorted(recs, key=lambda x: x.timestamp)
            first_in = next((x for x in recs_sorted if x.type == "check-in"), None)
            last_out = next((x for x in reversed(recs_sorted) if x.type == "check-out"), None)

            check_in_time = _to_hhmm(first_in.timestamp) if first_in else None
            check_out_time = _to_hhmm(last_out.timestamp) if last_out else None

            # Was this an emergency checkout?
            emergency_checkout = last_out is not None and last_out.is_emergency

            # Break minutes: pair break-start / break-end events
            break_minutes = 0
            open_break: Optional[datetime] = None
            for r in recs_sorted:
                if r.type == "break-start":
                    open_break = r.timestamp
                elif r.type == "break-end" and open_break is not None:
                    break_minutes += max(
                        0, int(round((r.timestamp - open_break).total_seconds() / 60))
                    )
                    open_break = None
            if open_break is not None and last_out is not None and last_out.timestamp > open_break:
                break_minutes += int(
                    round((last_out.timestamp - open_break).total_seconds() / 60)
                )

            missed_checkout = first_in is not None and last_out is None
            worked_minutes: Optional[int] = None
            if first_in and last_out and last_out.timestamp > first_in.timestamp:
                raw = int(
                    round((last_out.timestamp - first_in.timestamp).total_seconds() / 60)
                )
                worked_minutes = max(0, raw - break_minutes)

            person = recs_sorted[0].person
            sched_start = person.schedule_start if person else None
            sched_end = person.schedule_end if person else None

            delay_minutes: Optional[int] = None
            if sched_start and check_in_time:
                delay_minutes = _time_to_min(check_in_time) - _time_to_min(sched_start)

            early_dep_minutes: Optional[int] = None
            if sched_end and check_out_time:
                early_dep_minutes = _time_to_min(sched_end) - _time_to_min(check_out_time)

            overtime_minutes: Optional[int] = None
            if worked_minutes is not None and sched_start and sched_end:
                sched_net = _schedule_net_minutes(sched_start, sched_end)
                overtime_minutes = max(0, worked_minutes - sched_net)

            sessions.append({
                "person_id": person.id if person else recs_sorted[0].person_id,
                "employee_id": person.employee_id if person else None,
                "name": person.name if person else "Unknown",
                "role": person.role if person else "Unknown",
                "department": person.department if person else None,
                "date": date_key,
                "checkIn": check_in_time,
                "checkOut": check_out_time,
                "workedMinutes": worked_minutes,
                "breakMinutes": break_minutes,
                "delayMinutes": delay_minutes,
                "earlyDepartureMinutes": early_dep_minutes,
                "overtimeMinutes": overtime_minutes,
                "missedCheckout": missed_checkout,
                "emergencyCheckout": emergency_checkout,
                "scheduleStart": sched_start,
                "scheduleEnd": sched_end,
            })

    sessions.sort(key=lambda s: (s["date"], s["name"]))
    return sessions
