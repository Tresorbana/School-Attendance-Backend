"""
WebSocket gateway that replaces the NestJS FingerprintGateway.

Browser clients connect to /ws/fingerprint and receive:
  { type: 'scanner_status', status: 'ready'|'scanning'|'disconnected'|'error' }
  { type: 'processing' }
  { type: 'attendance_result', matched, name, ..., pipeline: 'python' }
  { type: 'enrollment_capture', template: <base64 png> }
  { type: 'enrollment_armed' }
  { type: 'mode_set', mode }

Client → server messages (JSON):
  { event: 'fingerprint_sample', data: { template: <base64 png> } }
  { event: 'arm_enrollment' }
  { event: 'cancel_enrollment' }
  { event: 'set_mode', data: { mode } }
"""
import asyncio
import base64
import io
import json
import logging
from typing import Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from PIL import Image

from scanner.bridge_client import scanner_bridge
from services.recognition import identify_and_record

logger = logging.getLogger("ws.fingerprint")

router = APIRouter()


class _Hub:
    def __init__(self) -> None:
        self.clients: Set[WebSocket] = set()
        self.enrollment_clients: Set[WebSocket] = set()
        self.next_mode: str = "auto"
        self._lock = asyncio.Lock()

    async def add(self, ws: WebSocket) -> None:
        async with self._lock:
            self.clients.add(ws)

    async def remove(self, ws: WebSocket) -> None:
        async with self._lock:
            self.clients.discard(ws)
            self.enrollment_clients.discard(ws)

    async def broadcast(self, payload: dict) -> None:
        msg = json.dumps(payload, default=str)
        async with self._lock:
            dead = []
            for c in self.clients:
                try:
                    await c.send_text(msg)
                except Exception:
                    dead.append(c)
            for c in dead:
                self.clients.discard(c)
                self.enrollment_clients.discard(c)


hub = _Hub()


def _raw_gray_to_png(width: int, height: int, data: bytes) -> bytes:
    img = Image.frombytes("L", (width, height), data)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _compressed_to_png(data: bytes) -> bytes | None:
    """Convert compressed scanner output (PNG/JPEG2000) to PNG. Returns None if unsupported."""
    try:
        img = Image.open(io.BytesIO(data))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:
        return None


async def _handle_scan_event(evt: dict) -> None:
    """Convert a scanner event to PNG, send to enrollment listeners, run identify."""
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
            logger.warning("Unsupported compressed format comprAlg=%s", height)
            await hub.broadcast({"type": "attendance_result", "matched": False, "error": "unsupported_compression"})
            return
    else:
        await hub.broadcast({"type": "attendance_result", "matched": False, "error": "bir_parse_failed"})
        return

    png_b64 = base64.b64encode(png).decode("ascii")
    await hub.broadcast({"type": "processing"})

    # Identify + record
    try:
        mode = hub.next_mode
        hub.next_mode = "auto"
        result = await asyncio.to_thread(identify_and_record, png, mode)
        await hub.broadcast({"type": "attendance_result", **result})
    except Exception as exc:
        logger.exception("Recognition failed: %s", exc)

    # Deliver to any armed enrollment client
    if hub.enrollment_clients:
        msg = json.dumps({"type": "enrollment_capture", "template": png_b64})
        for c in list(hub.enrollment_clients):
            try:
                await c.send_text(msg)
            except Exception:
                pass
        hub.enrollment_clients.clear()


async def _scanner_event_pump() -> None:
    """Bridge → WS broadcast forever. Started in lifespan."""
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


@router.websocket("/fingerprint")
async def fingerprint_ws(ws: WebSocket) -> None:
    await ws.accept()
    await hub.add(ws)
    await ws.send_text(json.dumps({"type": "scanner_status", "status": scanner_bridge.status}))
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
                    await hub.broadcast({"type": "attendance_result", "matched": False, "error": "non_png_rejected"})
                    continue
                png = base64.b64decode(tpl)
                await hub.broadcast({"type": "processing"})
                mode = hub.next_mode
                hub.next_mode = "auto"
                result = await asyncio.to_thread(identify_and_record, png, mode)
                await hub.broadcast({"type": "attendance_result", **result})

            elif event == "arm_enrollment":
                hub.enrollment_clients.add(ws)
                await ws.send_text(json.dumps({"type": "enrollment_armed"}))

            elif event == "cancel_enrollment":
                hub.enrollment_clients.discard(ws)

            elif event == "set_mode":
                mode = data.get("mode")
                if mode in {"auto", "break-start", "break-end", "check-out"}:
                    hub.next_mode = mode
                    await hub.broadcast({"type": "mode_set", "mode": mode})

    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.warning("WS error: %s", exc)
    finally:
        await hub.remove(ws)
