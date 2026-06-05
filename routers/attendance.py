from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from database import get_structured_db
from services import attendance as svc

router = APIRouter(tags=["attendance"])


@router.get("/attendance/recent")
def recent(station: Optional[str] = None, db: Session = Depends(get_structured_db)):
    return svc.get_recent(db, 20, station)


@router.get("/attendance")
def all_records(
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = None,
    search: Optional[str] = None,
    personId: Optional[int] = None,
    station: Optional[str] = None,
    type: Optional[str] = None,
    db: Session = Depends(get_structured_db),
):
    return svc.get_all(db, from_, to, search, personId, station, type)


@router.get("/stats")
def stats(station: Optional[str] = None, db: Session = Depends(get_structured_db)):
    return svc.get_stats(db, station)
