from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from database import get_structured_db
from services import attendance as svc
from services.auth import require_any_staff

router = APIRouter(tags=["attendance"])


@router.get("/attendance/recent")
def recent(
    user: dict = Depends(require_any_staff),
    db: Session = Depends(get_structured_db),
):
    return svc.get_recent(db, 20)


@router.get("/attendance")
def all_records(
    from_: Optional[str] = Query(default=None, alias="from"),
    to: Optional[str] = None,
    search: Optional[str] = Query(default=None, max_length=100),
    personId: Optional[int] = None,
    type: Optional[str] = Query(default=None, max_length=20),
    user: dict = Depends(require_any_staff),
    db: Session = Depends(get_structured_db),
):
    return svc.get_all(db, from_, to, search, personId, type)


@router.get("/stats")
def stats(
    user: dict = Depends(require_any_staff),
    db: Session = Depends(get_structured_db),
):
    return svc.get_stats(db)
