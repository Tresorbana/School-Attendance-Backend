"""Attendance logic: recording, stage machine, daily sessions for reports."""
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from config import settings
from models.attendance import Attendance
from models.person import Person

AttendanceType = str  # 'check-in' | 'check-out' | 'break-start' | 'break-end'
AttendanceStage = str  # 'checked-out' | 'checked-in' | 'on-break'


# ── Core recording ─────────────────────────────────────────────────────

def record(db: Session, person_id: int, confidence: float, type_: AttendanceType = "check-in") -> Attendance:
    entry = Attendance(
        person_id=person_id,
        confidence=confidence,
        type=type_,
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
        "checked-in": "check-out",
        "on-break": "break-end",
    }[stage]


def resolve_action(stage: AttendanceStage, mode: str) -> dict:
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
            "station": p.station if p else None,
            "timestamp": r.timestamp,
            "confidence": r.confidence,
            "type": r.type,
        })
    return out


def get_recent(db: Session, limit: int = 20, station: Optional[str] = None) -> List[dict]:
    q = (
        db.query(Attendance)
        .options(joinedload(Attendance.person))
        .order_by(Attendance.timestamp.desc())
        .limit(limit)
    )
    if station:
        q = q.join(Person, Attendance.person).filter(Person.station == station)
    return _format(q.all())


def get_all(
    db: Session,
    from_: Optional[str] = None,
    to: Optional[str] = None,
    search: Optional[str] = None,
    person_id: Optional[int] = None,
    station: Optional[str] = None,
    type_: Optional[str] = None,
) -> List[dict]:
    q = (
        db.query(Attendance)
        .options(joinedload(Attendance.person))
        .order_by(Attendance.timestamp.desc())
        .limit(500)
    )
    if from_:
        q = q.filter(Attendance.timestamp >= datetime.fromisoformat(from_))
    if to:
        q = q.filter(Attendance.timestamp < datetime.fromisoformat(to) + timedelta(days=1))
    if person_id:
        q = q.filter(Attendance.person_id == person_id)
    if station:
        q = q.join(Person, Attendance.person).filter(Person.station == station)
    if type_:
        q = q.filter(Attendance.type == type_)
    if search and search.strip():
        q = q.join(Person, Attendance.person).filter(
            Person.name.ilike(f"%{search.strip()[:100]}%")
        )
    return _format(q.all())


def get_stats(db: Session, station: Optional[str] = None) -> dict:
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

    people_q = db.query(func.count(Person.id))
    if station:
        people_q = people_q.filter(Person.station == station)
    total_people = people_q.scalar() or 0

    today_q = (
        db.query(func.count(func.distinct(Attendance.person_id)))
        .filter(Attendance.timestamp >= today_start, Attendance.type == "check-in")
    )
    if station:
        today_q = today_q.join(Person, Attendance.person).filter(Person.station == station)
    today_count = today_q.scalar() or 0

    last_q = (
        db.query(Attendance)
        .options(joinedload(Attendance.person))
        .filter(Attendance.type == "check-in")
        .order_by(Attendance.timestamp.desc())
    )
    if station:
        last_q = last_q.join(Person, Attendance.person).filter(Person.station == station)
    last = last_q.first()

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
    """'HH:MM' → minutes since midnight."""
    h, m = map(int, t.split(":"))
    return h * 60 + m


def _schedule_net_minutes(start: str, end: str) -> int:
    """Scheduled working minutes = end − start. No automatic break deductions."""
    return max(0, _time_to_min(end) - _time_to_min(start))


def get_daily_sessions(
    db: Session,
    from_: datetime,
    to: datetime,
    station: Optional[str] = None,
    person_id: Optional[int] = None,
    holiday_dates: Optional[Set[str]] = None,
) -> List[dict]:
    holiday_dates = holiday_dates or set()
    q = (
        db.query(Attendance)
        .options(joinedload(Attendance.person))
        .filter(Attendance.timestamp >= from_, Attendance.timestamp <= to)
        .order_by(Attendance.timestamp.asc())
    )
    if station:
        q = q.join(Person, Attendance.person).filter(Person.station == station)
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
            if d.weekday() >= 5:           # skip Sat/Sun
                continue
            if date_key in holiday_dates:
                continue

            recs_sorted = sorted(recs, key=lambda x: x.timestamp)
            first_in = next((x for x in recs_sorted if x.type == "check-in"), None)
            last_out = next((x for x in reversed(recs_sorted) if x.type == "check-out"), None)

            check_in_time = _to_hhmm(first_in.timestamp) if first_in else None
            check_out_time = _to_hhmm(last_out.timestamp) if last_out else None

            # ── Break minutes: pair break-start / break-end events ───────
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
            # Unclosed break → auto-close at checkout
            if open_break is not None and last_out is not None and last_out.timestamp > open_break:
                break_minutes += int(
                    round((last_out.timestamp - open_break).total_seconds() / 60)
                )

            # ── Worked minutes: raw span minus actual tracked breaks ──────
            # Previously the code deducted 60 min for shifts > 4 h with no break
            # data. Removed — break tracking is now explicit.
            missed_checkout = first_in is not None and last_out is None
            worked_minutes: Optional[int] = None
            if first_in and last_out and last_out.timestamp > first_in.timestamp:
                raw = int(
                    round((last_out.timestamp - first_in.timestamp).total_seconds() / 60)
                )
                worked_minutes = max(0, raw - break_minutes)

            # ── Schedule deviation ────────────────────────────────────────
            person = recs_sorted[0].person
            sched_start = person.schedule_start if person else None
            sched_end = person.schedule_end if person else None

            # delay_minutes > 0  → arrived LATE  (positive = late)
            delay_minutes: Optional[int] = None
            if sched_start and check_in_time:
                delay_minutes = _time_to_min(check_in_time) - _time_to_min(sched_start)

            # early_departure_minutes > 0  → left EARLY before schedule end
            early_dep_minutes: Optional[int] = None
            if sched_end and check_out_time:
                early_dep_minutes = _time_to_min(sched_end) - _time_to_min(check_out_time)

            # overtime_minutes > 0  → worked beyond scheduled hours that day
            overtime_minutes: Optional[int] = None
            if worked_minutes is not None and sched_start and sched_end:
                sched_net = _schedule_net_minutes(sched_start, sched_end)
                overtime_minutes = max(0, worked_minutes - sched_net)

            sessions.append({
                "person_id": person.id if person else recs_sorted[0].person_id,
                "employee_id": person.employee_id if person else None,
                "name": person.name if person else "Unknown",
                "role": person.role if person else "Unknown",
                "station": person.station if person else None,
                "date": date_key,
                "checkIn": check_in_time,
                "checkOut": check_out_time,
                "workedMinutes": worked_minutes,
                "breakMinutes": break_minutes,
                "delayMinutes": delay_minutes,
                "earlyDepartureMinutes": early_dep_minutes,
                "overtimeMinutes": overtime_minutes,
                "missedCheckout": missed_checkout,
                "scheduleStart": sched_start,
                "scheduleEnd": sched_end,
            })

    sessions.sort(key=lambda s: (s["date"], s["name"]))
    return sessions
