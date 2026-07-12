"""Africa/Kigali timezone helpers.

All attendance-day math (day bounds, month buckets, "today", etc.) must anchor
on Kigali local time (UTC+2, no DST) so late-evening scans don't spill into the
next report day. Timestamps stay stored as UTC in the DB; this module only
handles conversion at the read boundary.
"""
from datetime import date, datetime, time, timedelta, timezone
from typing import Tuple

try:
    from zoneinfo import ZoneInfo
    _KIGALI = ZoneInfo("Africa/Kigali")
except Exception:
    # Windows fallback — Kigali is fixed UTC+2 year-round (no DST).
    _KIGALI = timezone(timedelta(hours=2), name="Africa/Kigali")

KIGALI_TZ = _KIGALI


def now_kigali() -> datetime:
    return datetime.now(_KIGALI)


def today_kigali() -> date:
    return now_kigali().date()


def to_kigali(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_KIGALI)


def kigali_day_bounds_utc(d: date) -> Tuple[datetime, datetime]:
    """Return the [start, end] UTC datetimes bracketing a Kigali calendar day."""
    start_local = datetime.combine(d, time(0, 0, 0), tzinfo=_KIGALI)
    end_local = datetime.combine(d, time(23, 59, 59, 999_000), tzinfo=_KIGALI)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def kigali_month_bounds_utc(year: int, month: int) -> Tuple[datetime, datetime]:
    first = date(year, month, 1)
    last = (
        date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    ) - timedelta(days=1)
    start, _ = kigali_day_bounds_utc(first)
    _, end = kigali_day_bounds_utc(last)
    return start, end


def kigali_date_key(dt: datetime) -> str:
    """YYYY-MM-DD in Kigali local time for a stored (UTC or naive-UTC) datetime."""
    return to_kigali(dt).date().isoformat()
