"""
WebSocket gateway for fingerprint scanner events.

Clients connect to /fingerprint?token=<jwt>.
All clients receive all events — single school context, no station filtering.
Emergency checkout is signalled by mode='emergency-checkout' and triggers
an admin notification broadcast.
"""
import asyncio
import base64
import io
import json
import logging
from typing import Set

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from PIL import Image

from scanner.bridge_client import scanner_bridge
from services.auth import decode_token
from services.recognition import identify_and_record

logger = logging.getLogger("ws.fingerprint")

router = APIRouter()


# ── Broadcast hub ──────────────────────────────────────────────────────

class _Hub:
    def __init__(self) -> None:
        self._clients: Set[WebSocket] = set()
        self.enrollment_clients: Set[WebSocket] = set()
        self.login_clients: Set[WebSocket] = set()
        self.next_mode: str = "auto"
        self._lock = asyncio.Lock()

    async def add(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.add(ws)

    async def remove(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)
            self.enrollment_clients.discard(ws)
            self.login_clients.discard(ws)

    async def broadcast(self, payload: dict) -> None:
        msg = json.dumps(payload, default=str)
        async with self._lock:
            dead = []
            for ws in list(self._clients):
                try:
                    await ws.send_text(msg)
                except Exception:
                    dead.append(ws)
            for ws in dead:
                self._clients.discard(ws)
                self.enrollment_clients.discard(ws)

    async def send_one(self, ws: WebSocket, payload: dict) -> None:
        try:
            await ws.send_text(json.dumps(payload, default=str))
        except Exception:
            pass


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

    # If someone is armed for biometric login, hand them the raw image and
    # DO NOT record attendance — otherwise a login attempt would clock people in.
    if hub.login_clients:
        msg = json.dumps({"type": "login_capture", "image": png_b64})
        for c in list(hub.login_clients):
            try:
                await c.send_text(msg)
            except Exception:
                pass
        hub.login_clients.clear()
        return

    await hub.broadcast({"type": "processing"})

    try:
        mode = hub.next_mode
        hub.next_mode = "auto"
        result = await asyncio.to_thread(identify_and_record, png, mode)
        await hub.broadcast({"type": "attendance_result", **result})

        # Broadcast emergency alert separately so admin UI can highlight it
        if result.get("emergencyAlert"):
            await hub.broadcast({
                "type": "emergency_alert",
                "personName": result.get("name"),
                "personId": result.get("person_id"),
                "message": f"{result.get('name')} triggered an emergency checkout.",
            })
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
    if token:
        try:
            decode_token(token)
        except Exception:
            await ws.close(code=4001)
            return

    await ws.accept()
    await hub.add(ws)
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
                result = await asyncio.to_thread(identify_and_record, png, mode)
                await hub.send_one(ws, {"type": "attendance_result", **result})
                if result.get("emergencyAlert"):
                    await hub.broadcast({
                        "type": "emergency_alert",
                        "personName": result.get("name"),
                        "personId": result.get("person_id"),
                        "message": f"{result.get('name')} triggered an emergency checkout.",
                    })

            elif event == "arm_enrollment":
                hub.enrollment_clients.add(ws)
                await hub.send_one(ws, {"type": "enrollment_armed"})

            elif event == "cancel_enrollment":
                hub.enrollment_clients.discard(ws)

            elif event == "arm_login":
                hub.login_clients.add(ws)
                await hub.send_one(ws, {"type": "login_armed"})

            elif event == "cancel_login":
                hub.login_clients.discard(ws)

            elif event == "set_mode":
                mode = data.get("mode")
                valid_modes = {"auto", "break-start", "break-end", "check-out", "emergency-checkout"}
                if mode in valid_modes:
                    hub.next_mode = mode
                    await hub.broadcast({"type": "mode_set", "mode": mode})

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("WS error: %s", exc)
    finally:
        await hub.remove(ws)
