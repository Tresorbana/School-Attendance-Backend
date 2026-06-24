"""
WebSocket gateway — replaces NestJS FingerprintGateway.

Clients connect to /fingerprint?token=<jwt>.
The hub is station-aware: each client is tagged with its stationId from the JWT,
and attendance results are only broadcast to clients of the same station.
Scanner events are processed with the STATION_ID from config (set per-deployment).
"""
import asyncio
import base64
import io
import json
import logging
from typing import Dict, Optional, Set

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from PIL import Image

from config import settings
from scanner.bridge_client import scanner_bridge
from services.auth import decode_token
from services.recognition import identify_and_record

logger = logging.getLogger("ws.fingerprint")

router = APIRouter()


# ── Station-aware hub ──────────────────────────────────────────────────

class _Hub:
    def __init__(self) -> None:
        self._clients: Dict[WebSocket, Optional[int]] = {}  # ws → station_id
        self.enrollment_clients: Set[WebSocket] = set()
        self.next_mode: str = "auto"
        self._lock = asyncio.Lock()

    async def add(self, ws: WebSocket, station_id: Optional[int]) -> None:
        async with self._lock:
            self._clients[ws] = station_id

    async def remove(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.pop(ws, None)
            self.enrollment_clients.discard(ws)

    async def broadcast(self, payload: dict, station_id: Optional[int] = None) -> None:
        """Send to all clients of station_id (or all clients if station_id is None)."""
        msg = json.dumps(payload, default=str)
        async with self._lock:
            dead = []
            for ws, sid in list(self._clients.items()):
                if station_id is None or sid is None or sid == station_id:
                    try:
                        await ws.send_text(msg)
                    except Exception:
                        dead.append(ws)
            for ws in dead:
                self._clients.pop(ws, None)
                self.enrollment_clients.discard(ws)

    async def send_one(self, ws: WebSocket, payload: dict) -> None:
        try:
            await ws.send_text(json.dumps(payload, default=str))
        except Exception:
            pass

    @property
    def clients(self) -> Dict[WebSocket, Optional[int]]:
        return self._clients


hub = _Hub()


# ── Image helpers ──────────────────────────────────────────────────────

def _raw_gray_to_png(width: int, height: int, data: bytes) -> bytes:
    img = Image.frombytes("L", (width, height), data)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _compressed_to_png(data: bytes) -> bytes | None:
    try:
        img = Image.open(io.BytesIO(data))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


# ── Scan handler ───────────────────────────────────────────────────────

async def _handle_scan_event(evt: dict) -> None:
    width = evt.get("width", 0)
    height = evt.get("height", 0)
    data = evt.get("data", b"")
    png: bytes | None

    if width > 0 and height > 0:
        try:
            png = _raw_gray_to_png(width, height, data)
        except Exception as exc:
            logger.error("PNG encoding failed: %s", exc)
            await hub.broadcast({"type": "attendance_result", "matched": False, "error": "image_encoding_failed"})
            return
    elif width == -1:
        png = _compressed_to_png(data)
        if not png:
            logger.warning("Unsupported compressed format")
            await hub.broadcast({"type": "attendance_result", "matched": False, "error": "unsupported_compression"})
            return
    else:
        await hub.broadcast({"type": "attendance_result", "matched": False, "error": "bir_parse_failed"})
        return

    png_b64 = base64.b64encode(png).decode("ascii")
    station_id = settings.STATION_ID
    await hub.broadcast({"type": "processing"}, station_id)

    try:
        mode = hub.next_mode
        hub.next_mode = "auto"
        result = await asyncio.to_thread(identify_and_record, png, mode, station_id)
        await hub.broadcast({"type": "attendance_result", **result}, station_id)
    except Exception as exc:
        logger.exception("Recognition failed: %s", exc)

    if hub.enrollment_clients:
        msg = json.dumps({"type": "enrollment_capture", "template": png_b64})
        for c in list(hub.enrollment_clients):
            try:
                await c.send_text(msg)
            except Exception:
                pass
        hub.enrollment_clients.clear()


async def _scanner_event_pump() -> None:
    while True:
        evt = await scanner_bridge.next_event()
        t = evt.get("type")
        if t == "status":
            await hub.broadcast({"type": "scanner_status", "status": evt.get("status")})
        elif t == "scan":
            await _handle_scan_event(evt)
        elif t == "quality":
            await hub.broadcast({"type": "quality", "reject": evt.get("reject")})
        elif t == "bridge_error":
            await hub.broadcast({"type": "bridge_error", "code": evt.get("code"), "message": evt.get("message")})


# ── WebSocket endpoint ─────────────────────────────────────────────────

@router.websocket("/fingerprint")
async def fingerprint_ws(ws: WebSocket, token: str = Query(default="")) -> None:
    # Resolve station identity from JWT (optional — unauthenticated connections
    # fall back to the server-configured STATION_ID)
    client_station_id: Optional[int] = settings.STATION_ID
    if token:
        try:
            payload = decode_token(token)
            jwt_station = payload.get("stationId")
            if jwt_station is not None:
                # Super-admins (stationId=None) see all, station-admins scoped to theirs
                client_station_id = int(jwt_station) if jwt_station else None
        except Exception:
            # Invalid token — close with 4001 (policy violation)
            await ws.close(code=4001)
            return

    await ws.accept()
    await hub.add(ws, client_station_id)
    await hub.send_one(ws, {"type": "scanner_status", "status": scanner_bridge.status})

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue
            event = msg.get("event")
            data = msg.get("data") or {}

            if event == "fingerprint_sample":
                tpl = (data.get("template") or "").strip()
                if not tpl.startswith("iVBO"):
                    await hub.send_one(ws, {"type": "attendance_result", "matched": False, "error": "non_png_rejected"})
                    continue
                png = base64.b64decode(tpl)
                await hub.send_one(ws, {"type": "processing"})
                mode = hub.next_mode
                hub.next_mode = "auto"
                result = await asyncio.to_thread(identify_and_record, png, mode, client_station_id)
                await hub.send_one(ws, {"type": "attendance_result", **result})

            elif event == "arm_enrollment":
                hub.enrollment_clients.add(ws)
                await hub.send_one(ws, {"type": "enrollment_armed"})

            elif event == "cancel_enrollment":
                hub.enrollment_clients.discard(ws)

            elif event == "set_mode":
                mode = data.get("mode")
                if mode in {"auto", "break-start", "break-end", "check-out"}:
                    hub.next_mode = mode
                    await hub.broadcast({"type": "mode_set", "mode": mode}, client_station_id)

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("WS error: %s", exc)
    finally:
        await hub.remove(ws)
