"""
SAMS local relay — runs on the Windows client PC as a Windows service.

Responsibilities:
  1. Serve the Next.js static export at http://localhost:RELAY_PORT/
  2. Proxy /api/* → VPS backend (so the browser uses a single origin)
  3. Expose ws://localhost:RELAY_PORT/fingerprint for scanner events
  4. Read fingerprint scans from FingerprintBridge.exe and forward to VPS

Environment (set by installer in .env):
  VPS_URL     = http(s)://your-vps-ip-or-domain   (default: http://localhost:8000)
  STATIC_DIR  = path to Next.js out/ directory     (default: ./frontend)
  RELAY_PORT  = local port to listen on            (default: 8001)
"""
import asyncio
import base64
import io
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Set

# When frozen by PyInstaller, __file__ lives inside _internal/. The .env and
# frontend/ sit next to the .exe, so anchor on sys.executable instead.
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent

from dotenv import load_dotenv
load_dotenv(APP_DIR / ".env")

import httpx
from fastapi import FastAPI, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from fastapi.staticfiles import StaticFiles
from PIL import Image

from scanner.bridge_client import scanner_bridge

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("relay")

VPS_URL    = os.environ.get("VPS_URL",    "http://localhost:8000").rstrip("/")
STATIC_DIR = os.environ.get("STATIC_DIR", str(APP_DIR / "frontend"))
RELAY_PORT = int(os.environ.get("RELAY_PORT", "8001"))

# ── Broadcast hub ──────────────────────────────────────────────────────────────

class Hub:
    def __init__(self) -> None:
        self._clients: Set[WebSocket] = set()
        self.enrollment_clients: Set[WebSocket] = set()
        self.next_mode: str = "auto"
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


# ── Image helpers ──────────────────────────────────────────────────────────────

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


# ── VPS identify call ──────────────────────────────────────────────────────────

async def _identify_via_vps(png_bytes: bytes, mode: str = "auto") -> dict:
    headers = {"Content-Type": "application/json"}
    if _client_token:
        headers["Authorization"] = f"Bearer {_client_token}"
    payload = {
        "image": base64.b64encode(png_bytes).decode("ascii"),
        "mode": mode,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(f"{VPS_URL}/api/identify", json=payload, headers=headers)
        r.raise_for_status()
        return r.json()


# ── Scanner event handler ──────────────────────────────────────────────────────

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
    mode = hub.next_mode
    hub.next_mode = "auto"
    await hub.broadcast({"type": "processing"})
    try:
        result = await _identify_via_vps(png, mode)
        await hub.broadcast({"type": "attendance_result", **result})
        if result.get("emergencyAlert"):
            await hub.broadcast({
                "type": "emergency_alert",
                "personName": result.get("name"),
                "personId": result.get("person_id"),
                "message": f"{result.get('name')} triggered an emergency checkout.",
            })
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


# ── App lifecycle ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_app: FastAPI):
    loop = asyncio.get_running_loop()
    scanner_bridge.start(loop)
    pump = asyncio.create_task(_event_pump())
    logger.info("SAMS Relay ready — port %d, VPS=%s", RELAY_PORT, VPS_URL)
    if STATIC_DIR and Path(STATIC_DIR).is_dir():
        logger.info("Serving frontend from: %s", STATIC_DIR)
    else:
        logger.warning("STATIC_DIR not found (%s) — frontend not served", STATIC_DIR)
    try:
        yield
    finally:
        scanner_bridge.stop()
        pump.cancel()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Health ─────────────────────────────────────────────────────────────────────

@app.get("/relay-health")
def health() -> dict:
    return {"ok": True, "status": scanner_bridge.status, "vps": VPS_URL}


# ── API reverse proxy → VPS ────────────────────────────────────────────────────

_HOP_BY_HOP = {
    "connection", "keep-alive", "transfer-encoding",
    "te", "trailer", "upgrade", "proxy-authorization",
    "proxy-authenticate", "content-encoding", "content-length",
}


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy_api(path: str, request: Request) -> Response:
    url = f"{VPS_URL}/api/{path}"
    headers = {k: v for k, v in request.headers.items() if k.lower() not in {"host"}}
    body = await request.body()
    # 5s connect timeout → fail fast when the VPS is unreachable, so the user
    # sees "no internet" instead of waiting a full minute.
    # 60s read timeout preserves headroom for the matcher on the VPS side.
    timeout = httpx.Timeout(60.0, connect=5.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            r = await client.request(
                method=request.method,
                url=url,
                headers=headers,
                content=body,
                params=dict(request.query_params),
            )
        resp_headers = {k: v for k, v in r.headers.items() if k.lower() not in _HOP_BY_HOP}
        return Response(content=r.content, status_code=r.status_code, headers=resp_headers)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        return Response(
            content=json.dumps({"detail": "No internet connection. Please check your network and try again."}),
            status_code=503,
            media_type="application/json",
        )
    except httpx.ReadTimeout:
        return Response(
            content=json.dumps({"detail": "The server is taking too long to respond. Please try again."}),
            status_code=504,
            media_type="application/json",
        )
    except Exception as exc:
        logger.error("Proxy error for %s: %s", path, exc)
        return Response(
            content=json.dumps({"detail": "Something went wrong. Please try again."}),
            status_code=502,
            media_type="application/json",
        )


# ── Fingerprint WebSocket ──────────────────────────────────────────────────────

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
            data = msg.get("data") or {}

            if event == "arm_enrollment":
                hub.enrollment_clients.add(ws)
                await hub.send_one(ws, {"type": "enrollment_armed"})
            elif event == "cancel_enrollment":
                hub.enrollment_clients.discard(ws)
            elif event == "set_mode":
                mode = data.get("mode")
                valid_modes = {"auto", "break-start", "break-end", "check-out", "emergency-checkout"}
                if mode in valid_modes:
                    hub.next_mode = mode
                    await hub.broadcast({"type": "mode_set", "mode": mode})
    except WebSocketDisconnect:
        pass
    finally:
        await hub.remove(ws)


# ── Serve Next.js static export ────────────────────────────────────────────────
# Mounted last so explicit routes above take priority.

if STATIC_DIR and Path(STATIC_DIR).is_dir():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="frontend")


# ── Entry point (NSSM runs this directly via uvicorn) ─────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=RELAY_PORT, reload=False)
