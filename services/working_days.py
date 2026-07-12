"""Working-day math shared by leave balance, coverage flags and reports."""
from datetime import date, timedelta
from typing import Iterable, Set


def working_days(from_date: date, to_date: date, holiday_dates: Set[str]) -> int:
    """Number of weekdays (Mon–Fri) between the two dates inclusive, minus holidays."""
    if from_date > to_date:
        return 0
    count = 0
    cur = from_date
    while cur <= to_date:
        if cur.weekday() < 5 and cur.isoformat() not in holiday_dates:
            count += 1
        cur += timedelta(days=1)
    return count


def working_days_in_month(year: int, month: int, holiday_dates: Set[str]) -> int:
    first = date(year, month, 1)
    last = (
        date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    ) - timedelta(days=1)
    return working_days(first, last, holiday_dates)


def working_days_in_year(year: int, holiday_dates: Set[str]) -> int:
    return working_days(date(year, 1, 1), date(year, 12, 31), holiday_dates)


def leave_days_taken(
    from_iso: str,
    to_iso: str,
    holiday_dates: Set[str],
) -> int:
    """Working days between two YYYY-MM-DD strings inclusive, minus weekends and holidays."""
    a = date.fromisoformat(from_iso)
    b = date.fromisoformat(to_iso)
    return working_days(a, b, holiday_dates)


def working_day_iso_dates(
    from_date: date,
    to_date: date,
    holiday_dates: Set[str],
) -> Iterable[str]:
    cur = from_date
    while cur <= to_date:
        if cur.weekday() < 5 and cur.isoformat() not in holiday_dates:
            yield cur.isoformat()
        cur += timedelta(days=1)
