"""
Direct fingerprint pipeline endpoints. These mirror the old fingerprint-pipeline
FastAPI app so the enrollment UI keeps working unchanged.

Routes:
  POST /coverage              accumulate one scan against a session
  POST /coverage/{sid}/reset  cancel a session
  POST /scan-quality          quick acceptability check
  POST /identify              1:N match (also used by the WebSocket path)
  POST /enroll                build composite template + save
  DELETE /person/{pid}        clear a person's stored template
  GET/PATCH /pipeline-config  runtime config tweaks
"""
import base64
import threading
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, Body, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_templates_db
from pipeline.coverage import CoverageState, accumulate_scan, new_state
from pipeline.enroll import enroll as pipeline_enroll
from pipeline.pipeline_config import PIPELINE_CONFIG
from pipeline.quality import check_quality
from pipeline_db import delete_template, save_template
from services.auth import require_admin
from services.template_cache import template_cache

router = APIRouter(tags=["fingerprint"])

# Runtime-mutable config (PATCH /pipeline-config writes here)
_runtime_config: dict = dict(PIPELINE_CONFIG)


# ── Coverage sessions ──────────────────────────────────────────────────


class _CoverageSessions:
    TTL = 600.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: Dict[str, Dict[str, Any]] = {}

    def get_or_create(self, sid: str) -> CoverageState:
        with self._lock:
            self._gc()
            entry = self._sessions.get(sid)
            if entry is None:
                state = new_state(_runtime_config)
                self._sessions[sid] = {"state": state, "touched": time.monotonic()}
                return state
            entry["touched"] = time.monotonic()
            return entry["state"]

    def reset(self, sid: str) -> None:
        with self._lock:
            self._sessions.pop(sid, None)

    def _gc(self) -> None:
        now = time.monotonic()
        to_del = [k for k, v in self._sessions.items() if now - v["touched"] > self.TTL]
        for k in to_del:
            self._sessions.pop(k, None)


_sessions = _CoverageSessions()


# ── DTOs ────────────────────────────────────────────────────────────────


class IdentifyDto(BaseModel):
    image: str  # base64 PNG
    mode: str = "auto"


class CoverageDto(BaseModel):
    sessionId: str
    image: str


class CoverageResetDto(BaseModel):
    sessionId: str


class EnrollDto(BaseModel):
    person_id: str
    images: list[str]


class QualityDto(BaseModel):
    image: str


# ── Routes ──────────────────────────────────────────────────────────────


@router.post("/coverage")
def coverage(dto: CoverageDto):
    if not dto.sessionId or not dto.image:
        return {"accepted": False, "error": "missing_fields"}
    state = _sessions.get_or_create(dto.sessionId)
    img_bytes = base64.b64decode(dto.image)
    res = accumulate_scan(state, img_bytes, _runtime_config)
    res["state"] = state.to_dict(_runtime_config)
    return res


@router.post("/coverage/reset")
def coverage_reset(dto: CoverageResetDto):
    if not dto.sessionId:
        return {"ok": False}
    _sessions.reset(dto.sessionId)
    return {"ok": True}


@router.post("/scan-quality")
def scan_quality(dto: QualityDto):
    import cv2
    import numpy as np
    if not dto.image:
        return {"acceptable": False, "error": "no_image"}
    arr = np.frombuffer(base64.b64decode(dto.image), np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return {"acceptable": False, "clarity_score": 0, "coverage": 0, "reason": "decode_error"}
    return check_quality(img, _runtime_config)


@router.post("/identify")
def identify_route(dto: IdentifyDto):
    """HTTP identify — used by the relay and direct frontend calls."""
    from services.recognition import identify_and_record
    img = base64.b64decode(dto.image)
    return identify_and_record(img, dto.mode)


@router.post("/enroll")
def enroll_route(dto: EnrollDto, db: Session = Depends(get_templates_db)):
    """Run the pipeline + persist the composite template."""
    images = [base64.b64decode(b) for b in dto.images]
    result = pipeline_enroll(images, _runtime_config)
    template_stored = False
    if result.get("template_bytes"):
        save_template(db, dto.person_id, result["template_bytes"], result.get("raw_templates", []))
        template_stored = True
        template_cache.invalidate()
    return {
        "success": template_stored,
        "person_id": dto.person_id,
        "quality_scores": result.get("quality_scores", []),
        "steps_applied": result.get("steps_applied", []),
        "template_stored": template_stored,
        "error": result.get("error"),
    }


@router.delete("/person/{person_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_person_template(person_id: str, db: Session = Depends(get_templates_db)):
    delete_template(db, person_id)
    template_cache.invalidate()


@router.get("/pipeline-config", dependencies=[Depends(require_admin)])
def get_pipeline_config():
    return _runtime_config


@router.patch("/pipeline-config", dependencies=[Depends(require_admin)])
def patch_pipeline_config(patch: dict = Body(...)):
    _runtime_config.update(patch)
    return _runtime_config
