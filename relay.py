"""
Local fingerprint scanner relay.

Runs on the Windows PC. Reads events from FingerprintBridge.exe (via named
pipe / spawned child) and exposes them to the browser at
ws://localhost:8001/fingerprint. Captured fingerprint images are uploaded
to the VPS backend for identification; results stream back over the WS.

Usage:
  set VPS_URL=http://102.202.208.254
  py -m uvicorn relay:app --host 127.0.0.1 --port 8001
"""
import asyncio
import base64
import io
import json
import logging
import os
from contextlib import asynccontextmanager
from typing import Set

import httpx
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from PIL import Image

from scanner.bridge_client import scanner_bridge

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("relay")

VPS_URL = os.environ.get("VPS_URL", "http://102.202.208.254").rstrip("/")


class Hub:
    def __init__(self) -> None:
        self._clients: Set[WebSocket] = set()
        self.enrollment_clients: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def add(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.add(ws)

    async def remove(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)
            self.enrollment_clients.discard(ws)

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


hub = Hub()
_client_token = ""


def _raw_to_png(width: int, height: int, data: bytes) -> bytes:
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


async def _identify_via_vps(png_bytes: bytes) -> dict:
    headers = {"Content-Type": "application/json"}
    if _client_token:
        headers["Authorization"] = f"Bearer {_client_token}"
    payload = {"image": base64.b64encode(png_bytes).decode("ascii")}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{VPS_URL}/api/identify", json=payload, headers=headers)
        r.raise_for_status()
        return r.json()


async def _handle_scan(evt: dict) -> None:
    width = evt.get("width", 0)
    height = evt.get("height", 0)
    data = evt.get("data", b"")

    if width > 0 and height > 0:
        try:
            png = _raw_to_png(width, height, data)
        except Exception as exc:
            logger.error("PNG encode failed: %s", exc)
            await hub.broadcast({"type": "attendance_result", "matched": False, "error": "image_encoding_failed"})
            return
    elif width == -1:
        png = _compressed_to_png(data)
        if not png:
            await hub.broadcast({"type": "attendance_result", "matched": False, "error": "unsupported_compression"})
            return
    else:
        await hub.broadcast({"type": "attendance_result", "matched": False, "error": "bir_parse_failed"})
        return

    # If any client is armed for enrollment, deliver the PNG directly.
    if hub.enrollment_clients:
        png_b64 = base64.b64encode(png).decode("ascii")
        msg = json.dumps({"type": "enrollment_capture", "template": png_b64})
        for c in list(hub.enrollment_clients):
            try:
                await c.send_text(msg)
            except Exception:
                pass
        hub.enrollment_clients.clear()
        return

    # Otherwise: identify via VPS.
    await hub.broadcast({"type": "processing"})
    try:
        result = await _identify_via_vps(png)
        await hub.broadcast({"type": "attendance_result", **result})
    except Exception as exc:
        logger.exception("Identify failed: %s", exc)
        await hub.broadcast({"type": "attendance_result", "matched": False, "error": "pipeline_unavailable"})


async def _event_pump() -> None:
    while True:
        evt = await scanner_bridge.next_event()
        t = evt.get("type")
        if t == "status":
            await hub.broadcast({"type": "scanner_status", "status": evt.get("status")})
        elif t == "scan":
            await _handle_scan(evt)
        elif t == "quality":
            await hub.broadcast({"type": "quality", "reject": evt.get("reject")})
        elif t == "bridge_error":
            await hub.broadcast({"type": "bridge_error", "code": evt.get("code"), "message": evt.get("message")})


@asynccontextmanager
async def lifespan(_app: FastAPI):
    loop = asyncio.get_running_loop()
    scanner_bridge.start(loop)
    pump = asyncio.create_task(_event_pump())
    logger.info("Scanner relay ready on port 8001, VPS=%s", VPS_URL)
    try:
        yield
    finally:
        scanner_bridge.stop()
        pump.cancel()


app = FastAPI(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health() -> dict:
    return {"ok": True, "status": scanner_bridge.status, "vps": VPS_URL}


@app.websocket("/fingerprint")
async def fingerprint_ws(ws: WebSocket, token: str = Query(default="")) -> None:
    global _client_token
    if token:
        _client_token = token

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
            if event == "arm_enrollment":
                hub.enrollment_clients.add(ws)
                await hub.send_one(ws, {"type": "enrollment_armed"})
            elif event == "cancel_enrollment":
                hub.enrollment_clients.discard(ws)
    except WebSocketDisconnect:
        pass
    finally:
        await hub.remove(ws)
