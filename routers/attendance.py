from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from database import get_structured_db
from services import attendance as svc
from services.auth import require_any_admin

router = APIRouter(tags=["attendance"])


@router.get("/attendance/recent")
def recent(
    station: Optional[str] = Query(default=None, max_length=150),
    user: dict = Depends(require_any_admin),
    db: Session = Depends(get_structured_db),
):
    effective_station = user.get("station") if user.get("role") == "station-admin" else station
    return svc.get_recent(db, 20, effective_station)


@router.get("/attendance")
def all_records(
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = None,
    search: Optional[str] = Query(default=None, max_length=100),
    personId: Optional[int] = None,
    station: Optional[str] = Query(default=None, max_length=150),
    type: Optional[str] = Query(default=None, max_length=20),
    user: dict = Depends(require_any_admin),
    db: Session = Depends(get_structured_db),
):
    effective_station = user.get("station") if user.get("role") == "station-admin" else station
    return svc.get_all(db, from_, to, search, personId, effective_station, type)


@router.get("/stats")
def stats(
    station: Optional[str] = Query(default=None, max_length=150),
    user: dict = Depends(require_any_admin),
    db: Session = Depends(get_structured_db),
):
    effective_station = user.get("station") if user.get("role") == "station-admin" else station
    return svc.get_stats(db, effective_station)
