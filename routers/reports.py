from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session

from database import get_structured_db
from services import reports as svc

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get("/daily")
def daily(db: Session = Depends(get_structured_db)):
    return svc.daily(db)


@router.get("/weekly")
def weekly(db: Session = Depends(get_structured_db)):
    return svc.weekly(db)


@router.get("/monthly")
def monthly(db: Session = Depends(get_structured_db)):
    return svc.monthly(db)


@router.get("/calendar")
def calendar(year: int, month: int, db: Session = Depends(get_structured_db)):
    return svc.calendar_month(db, year, month)


@router.get("/present-today")
def present_today(station: Optional[str] = None, db: Session = Depends(get_structured_db)):
    return svc.present_today(db, station)


@router.get("/by-role")
def by_role(db: Session = Depends(get_structured_db)):
    return svc.by_role(db)


@router.get("/working-hours")
def working_hours(
    year: int,
    month: int,
    station: Optional[str] = None,
    personId: Optional[int] = None,
    db: Session = Depends(get_structured_db),
):
    return svc.monthly_working_hours(db, year, month, station, personId)


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
    station: Optional[str] = None,
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
