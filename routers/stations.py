from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_structured_db
from models.station import Station
from services.auth import require_admin_key

router = APIRouter(prefix="/stations", tags=["stations"])


class CreateStationDto(BaseModel):
    name: str
    code: Optional[str] = None
    address: Optional[str] = None
    adminUsername: Optional[str] = None
    adminPassword: Optional[str] = None
    adminFullName: Optional[str] = None


class UpdateStationDto(BaseModel):
    name: Optional[str] = None
    code: Optional[str] = None
    address: Optional[str] = None
    adminUsername: Optional[str] = None
    adminPassword: Optional[str] = None
    adminFullName: Optional[str] = None


@router.get("")
def list_stations(db: Session = Depends(get_structured_db)):
    return [s.to_public() for s in db.query(Station).order_by(Station.name.asc()).all()]


@router.get("/names")
def list_active_names(db: Session = Depends(get_structured_db)):
    rows = (
        db.query(Station.name)
        .filter(Station.active.is_(True))
        .order_by(Station.name.asc())
        .all()
    )
    return [r[0] for r in rows]


@router.get("/{station_id}")
def get_one(station_id: int, db: Session = Depends(get_structured_db)):
    s = db.query(Station).filter(Station.id == station_id).first()
    if not s:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Station {station_id} not found")
    return s.to_public()


@router.post("", status_code=status.HTTP_201_CREATED)
def create(dto: CreateStationDto, db: Session = Depends(get_structured_db)):
    existing = db.query(Station).filter(Station.name == dto.name.strip()).first()
    if existing:
        raise HTTPException(status.HTTP_409_CONFLICT, f'Station "{dto.name}" already exists')
    s = Station(
        name=dto.name.strip(),
        code=(dto.code.strip() if dto.code else None),
        address=(dto.address.strip() if dto.address else None),
        admin_username=(dto.adminUsername.strip() if dto.adminUsername else None),
        admin_password=dto.adminPassword or None,
        admin_full_name=(dto.adminFullName.strip() if dto.adminFullName else None),
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    return s.to_public()


@router.patch("/{station_id}")
def update(station_id: int, dto: UpdateStationDto, db: Session = Depends(get_structured_db)):
    s = db.query(Station).filter(Station.id == station_id).first()
    if not s:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Station {station_id} not found")
    if dto.name is not None:
        s.name = dto.name.strip()
    if dto.code is not None:
        s.code = dto.code.strip() or None
    if dto.address is not None:
        s.address = dto.address.strip() or None
    if dto.adminUsername is not None:
        s.admin_username = dto.adminUsername.strip() or None
    if dto.adminPassword is not None:
        s.admin_password = dto.adminPassword or None
    if dto.adminFullName is not None:
        s.admin_full_name = dto.adminFullName.strip() or None
    db.commit()
    db.refresh(s)
    return s.to_public()


@router.delete("/{station_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove(station_id: int, db: Session = Depends(get_structured_db)):
    s = db.query(Station).filter(Station.id == station_id).first()
    if not s:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Station {station_id} not found")
    db.delete(s)
    db.commit()


@router.post("/seed-demo", dependencies=[Depends(require_admin_key)])
def seed_demo(db: Session = Depends(get_structured_db)):
    branches = [
        {"name": "Nyamasheke HQ", "code": "HQ", "address": "Nyamasheke, Western Province"},
        {"name": "Bushekeri", "code": "BSK", "address": "Bushekeri, Nyamasheke"},
        {"name": "Bushenge", "code": "BSG", "address": "Bushenge, Nyamasheke"},
        {"name": "Cyato", "code": "CYT", "address": "Cyato, Nyamasheke"},
        {"name": "Gihombo", "code": "GHB", "address": "Gihombo, Nyamasheke"},
        {"name": "Kagano", "code": "KGN", "address": "Kagano, Nyamasheke"},
        {"name": "Kanjongo", "code": "KJG", "address": "Kanjongo, Nyamasheke"},
        {"name": "Karambi", "code": "KRB", "address": "Karambi, Nyamasheke"},
        {"name": "Karengera", "code": "KRG", "address": "Karengera, Nyamasheke"},
        {"name": "Kirimbi", "code": "KRM", "address": "Kirimbi, Nyamasheke"},
        {"name": "Macuba", "code": "MCB", "address": "Macuba, Nyamasheke"},
        {"name": "Mahembe", "code": "MHB", "address": "Mahembe, Nyamasheke"},
        {"name": "Nyabitekeri", "code": "NBT", "address": "Nyabitekeri, Nyamasheke"},
        {"name": "Rangiro", "code": "RNG", "address": "Rangiro, Nyamasheke"},
        {"name": "Ruharambuga", "code": "RHR", "address": "Ruharambuga, Nyamasheke"},
        {"name": "Shangi", "code": "SHG", "address": "Shangi, Nyamasheke"},
    ]
    created, skipped = [], []
    for b in branches:
        existing = db.query(Station).filter(Station.name == b["name"]).first()
        if existing:
            skipped.append(b["name"])
            continue
        code_lower = b["code"].lower()
        s = Station(
            name=b["name"],
            code=b["code"],
            address=b["address"],
            admin_username=f"{code_lower}_admin",
            admin_password=f"Indongozi@{b['code'].upper()}",
            admin_full_name=f"{b['name']} Admin",
        )
        db.add(s)
        db.flush()
        created.append(s.to_public())
    db.commit()
    return {"created": created, "skipped": skipped}
