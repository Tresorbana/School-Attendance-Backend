from datetime import date, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from database import get_structured_db
from models.holiday import Holiday
from services.auth import require_admin_key, require_any_admin, require_super_admin

router = APIRouter(prefix="/holidays", tags=["holidays"])


class CreateHolidayDto(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    date: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")
    confirmed: bool = False


class UpdateHolidayDto(BaseModel):
    name: Optional[str] = Field(default=None, max_length=200)
    date: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    confirmed: Optional[bool] = None


@router.get("")
def list_all(
    year: Optional[int] = None,
    user: dict = Depends(require_any_admin),
    db: Session = Depends(get_structured_db),
):
    if year is not None:
        if year < 2000 or year > 2100:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid year")
        q = (
            db.query(Holiday)
            .filter(Holiday.date >= f"{year}-01-01", Holiday.date <= f"{year}-12-31")
            .order_by(Holiday.date.asc())
        )
    else:
        q = db.query(Holiday).order_by(Holiday.date.asc())
    return [h.to_public() for h in q.all()]


@router.post("", status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_super_admin)])
def create(dto: CreateHolidayDto, db: Session = Depends(get_structured_db)):
    h = Holiday(name=dto.name.strip(), date=dto.date, confirmed=dto.confirmed)
    db.add(h)
    db.commit()
    db.refresh(h)
    return h.to_public()


@router.patch("/{holiday_id}", dependencies=[Depends(require_super_admin)])
def update(holiday_id: int, dto: UpdateHolidayDto, db: Session = Depends(get_structured_db)):
    h = db.query(Holiday).filter(Holiday.id == holiday_id).first()
    if not h:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Holiday {holiday_id} not found")
    if dto.name is not None:
        h.name = dto.name.strip()
    if dto.date is not None:
        h.date = dto.date
    if dto.confirmed is not None:
        h.confirmed = dto.confirmed
    db.commit()
    db.refresh(h)
    return h.to_public()


@router.delete("/{holiday_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_super_admin)])
def remove(holiday_id: int, db: Session = Depends(get_structured_db)):
    h = db.query(Holiday).filter(Holiday.id == holiday_id).first()
    if not h:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Holiday {holiday_id} not found")
    db.delete(h)
    db.commit()


# ── Seed ────────────────────────────────────────────────────────────────


def _compute_easter(year: int) -> date:
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    L = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * L) // 451
    month = (h + L - 7 * m + 114) // 31
    day = ((h + L - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def _first_friday_of_august(year: int) -> str:
    d = date(year, 8, 1)
    while d.weekday() != 4:
        d += timedelta(days=1)
    return d.isoformat()


def _observed_date(year: int, month: int, day: int) -> str:
    d = date(year, month, day)
    wd = d.weekday()
    if wd == 5:
        d += timedelta(days=2)
    elif wd == 6:
        d += timedelta(days=1)
    return d.isoformat()


def _islamic_dates(year: int) -> dict:
    BASE_YEAR = 2026
    BASE_FITR = date(2026, 3, 20)
    BASE_ADHA = date(2026, 5, 27)
    DAYS_PER_HIJRI_YEAR = 354.367
    offset = round((year - BASE_YEAR) * DAYS_PER_HIJRI_YEAR)
    return {
        "eidFitr": (BASE_FITR + timedelta(days=offset)).isoformat(),
        "eidAdha": (BASE_ADHA + timedelta(days=offset)).isoformat(),
    }


@router.post("/seed/{year}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_admin_key)])
def seed_defaults(year: int, db: Session = Depends(get_structured_db)):
    if year < 2000 or year > 2100:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid year")
    existing = (
        db.query(Holiday)
        .filter(Holiday.date >= f"{year}-01-01", Holiday.date <= f"{year}-12-31")
        .count()
    )
    if existing > 0:
        return

    easter = _compute_easter(year)
    islamic = _islamic_dates(year)

    defaults = [
        ("New Year's Day", f"{year}-01-01", True),
        ("Day after New Year's Day", f"{year}-01-02", True),
        ("National Heroes' Day", _observed_date(year, 2, 1), True),
        ("Eid al-Fitr (End of Ramadan)", islamic["eidFitr"], False),
        ("Good Friday", (easter - timedelta(days=2)).isoformat(), True),
        ("Easter Monday", (easter + timedelta(days=1)).isoformat(), True),
        ("Genocide Against the Tutsi Memorial Day", f"{year}-04-07", True),
        ("Labour Day (Worker's Day)", f"{year}-05-01", True),
        ("Eid al-Adha (Feast of Sacrifice)", islamic["eidAdha"], False),
        ("Independence Day", f"{year}-07-01", True),
        ("Liberation Day (Kwibohora)", f"{year}-07-04", True),
        ("Umuganura Day (National Thanksgiving)", _first_friday_of_august(year), True),
        ("Assumption Day", f"{year}-08-15", True),
        ("Christmas Day", f"{year}-12-25", True),
        ("Boxing Day", f"{year}-12-26", True),
    ]
    for name, d, confirmed in defaults:
        try:
            db.add(Holiday(name=name, date=d, confirmed=confirmed))
            db.flush()
        except Exception:
            db.rollback()
    db.commit()
