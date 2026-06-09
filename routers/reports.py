from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from database import get_structured_db
from services import reports as svc
from services.auth import require_any_admin

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/daily")
def daily(user: dict = Depends(require_any_admin), db: Session = Depends(get_structured_db)):
    return svc.daily(db)


@router.get("/weekly")
def weekly(user: dict = Depends(require_any_admin), db: Session = Depends(get_structured_db)):
    return svc.weekly(db)


@router.get("/monthly")
def monthly(user: dict = Depends(require_any_admin), db: Session = Depends(get_structured_db)):
    return svc.monthly(db)


@router.get("/calendar")
def calendar(
    year: int,
    month: int,
    user: dict = Depends(require_any_admin),
    db: Session = Depends(get_structured_db),
):
    return svc.calendar_month(db, year, month)


@router.get("/present-today")
def present_today(
    station: Optional[str] = Query(default=None, max_length=150),
    user: dict = Depends(require_any_admin),
    db: Session = Depends(get_structured_db),
):
    effective_station = user.get("station") if user.get("role") == "station-admin" else station
    return svc.present_today(db, effective_station)


@router.get("/by-role")
def by_role(user: dict = Depends(require_any_admin), db: Session = Depends(get_structured_db)):
    return svc.by_role(db)


@router.get("/working-hours")
def working_hours(
    year: int,
    month: int,
    station: Optional[str] = Query(default=None, max_length=150),
    personId: Optional[int] = None,
    user: dict = Depends(require_any_admin),
    db: Session = Depends(get_structured_db),
):
    effective_station = user.get("station") if user.get("role") == "station-admin" else station
    return svc.monthly_working_hours(db, year, month, effective_station, personId)


# Export endpoints intentionally unauthenticated — browser file downloads cannot send Bearer tokens.
# Restrict access at the network/firewall level in production.

@router.get("/export")
def export_csv(
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = None,
    db: Session = Depends(get_structured_db),
):
    csv = svc.export_csv(db, from_, to)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    return Response(
        content=csv,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="sams-attendance-{today}.csv"',
            "Cache-Control": "no-cache",
        },
    )


@router.get("/export-monthly")
def export_monthly_csv(
    year: int,
    month: int,
    station: Optional[str] = Query(default=None, max_length=150),
    db: Session = Depends(get_structured_db),
):
    csv = svc.export_monthly_csv(db, year, month, station)
    month_str = f"{year:04d}-{month:02d}"
    return Response(
        content=csv,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="sams-working-hours-{month_str}.csv"',
            "Cache-Control": "no-cache",
        },
    )
