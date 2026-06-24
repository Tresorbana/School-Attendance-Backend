"""Report endpoints. Export routes require a ?token= query param for browser downloads."""
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from database import get_structured_db
from services import reports as svc
from services.auth import decode_token, require_any_admin

router = APIRouter(prefix="/reports", tags=["reports"])


def _require_download_token(token: Optional[str] = Query(default=None)) -> dict:
    """
    Validates a JWT passed as ?token= query param.
    Used by export endpoints because browser <a href> downloads cannot send Bearer headers.
    """
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentication required")
    return decode_token(token)


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
    effective = user.get("station") if user.get("role") == "station-admin" else station
    return svc.present_today(db, effective)


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
    effective = user.get("station") if user.get("role") == "station-admin" else station
    return svc.monthly_working_hours(db, year, month, effective, personId)


@router.get("/export")
def export_csv(
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = None,
    station: Optional[str] = Query(default=None, max_length=150),
    user: dict = Depends(_require_download_token),
    db: Session = Depends(get_structured_db),
):
    effective = user.get("station") if user.get("role") == "station-admin" else station
    csv_data = svc.export_csv(db, from_, to, effective)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    return Response(
        content=csv_data,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="sams-attendance-{today}.csv"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/export-monthly")
def export_monthly_csv(
    year: int,
    month: int,
    station: Optional[str] = Query(default=None, max_length=150),
    user: dict = Depends(_require_download_token),
    db: Session = Depends(get_structured_db),
):
    effective = user.get("station") if user.get("role") == "station-admin" else station
    csv_data = svc.export_monthly_csv(db, year, month, effective)
    month_str = f"{year:04d}-{month:02d}"
    return Response(
        content=csv_data,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="sams-working-hours-{month_str}.csv"',
            "Cache-Control": "no-store",
        },
    )
