"""Admin notifications — emergency alerts and system events."""
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from database import get_structured_db
from models.notification import Notification
from services.auth import require_admin, require_any_staff

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=List[dict])
def list_notifications(
    user: dict = Depends(require_admin),
    db: Session = Depends(get_structured_db),
):
    notes = (
        db.query(Notification)
        .order_by(Notification.created_at.desc())
        .limit(100)
        .all()
    )
    return [n.to_public() for n in notes]


@router.get("/unread-count")
def unread_count(
    user: dict = Depends(require_any_staff),
    db: Session = Depends(get_structured_db),
):
    count = (
        db.query(Notification)
        .filter(Notification.is_read.is_(False))
        .count()
    )
    return {"count": count}


@router.patch("/{notification_id}/read")
def mark_read(
    notification_id: int,
    user: dict = Depends(require_admin),
    db: Session = Depends(get_structured_db),
):
    note = db.query(Notification).filter(Notification.id == notification_id).first()
    if not note:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Notification not found")
    note.is_read = True
    db.commit()
    return {"ok": True}


@router.patch("/mark-all-read")
def mark_all_read(
    user: dict = Depends(require_admin),
    db: Session = Depends(get_structured_db),
):
    db.query(Notification).filter(Notification.is_read.is_(False)).update({"is_read": True})
    db.commit()
    return {"ok": True}
