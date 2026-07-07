"""
Connect to the FingerprintBridge Windows scheduled task / exe via named pipe
\\.\pipe\AttendAIFingerprint and stream events to an asyncio.Queue.

Mirrors the NestJS FingerprintScannerService behaviour:
  - try the named pipe first
  - if not connected, run `schtasks /run /tn AttendAIFingerprintBridge`
  - if still nothing, spawn bridge/FingerprintBridge.exe directly
  - emit dict events: {"type": "status" | "scan" | "quality" | "error", ...}
"""
import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
from base64 import b64decode
from pathlib import Path
from typing import Optional

logger = logging.getLogger("scanner")

SERVICE_PIPE = r"\\.\pipe\AttendAIFingerprint"
SCHEDULED_TASK = "AttendAIFingerprintBridge"

# In a PyInstaller one-folder bundle sys.executable is the .exe itself;
# __file__ resolves to an internal extraction temp dir and is unreliable.
_HERE = (
    Path(sys.executable).parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent.parent
)
_BRIDGE_EXE_CANDIDATES = [
    _HERE / "bridge" / "FingerprintBridge.exe",
]


def _find_bridge_exe() -> Optional[Path]:
    for p in _BRIDGE_EXE_CANDIDATES:
        if p.exists():
            return p
    return None


class ScannerBridge:
    """Thread-based pipe reader; pushes events into a thread-safe queue."""

    def __init__(self) -> None:
        self._queue: "asyncio.Queue[dict]" = asyncio.Queue()
        self._stop = threading.Event()
        self._status = "disconnected"
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    @property
    def status(self) -> str:
        return self._status

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._loop = loop
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="ScannerBridge")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    async def next_event(self) -> dict:
        return await self._queue.get()

    # ── Internal ────────────────────────────────────────────────────────

    def _emit(self, event: dict) -> None:
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._queue.put(event), self._loop)

    def _set_status(self, s: str) -> None:
        if self._status != s:
            self._status = s
            self._emit({"type": "status", "status": s})

    def _run(self) -> None:
        if sys.platform != "win32":
            logger.info("Non-Windows platform — fingerprint bridge disabled.")
            self._set_status("disconnected")
            return
        delay = 2.0
        while not self._stop.is_set():
            try:
                if self._try_pipe():
                    delay = 2.0
                else:
                    self._trigger_task()
                    _wait(3.0, self._stop)
                    if self._try_pipe():
                        delay = 2.0
                    else:
                        self._spawn_exe_blocking()
            except Exception as exc:
                logger.exception("Scanner bridge loop error: %s", exc)
            self._set_status("disconnected")
            _wait(min(delay, 30.0), self._stop)
            delay = min(delay * 2, 30.0)

    def _try_pipe(self) -> bool:
        try:
            f = open(SERVICE_PIPE, "rb", buffering=0)
        except FileNotFoundError:
            return False
        except OSError as exc:
            logger.debug("Pipe connect failed: %s", exc)
            return False
        logger.info("Connected to fingerprint service via named pipe")
        self._set_status("ready")
        self._read_lines(f)
        return True

    def _trigger_task(self) -> None:
        logger.info("Triggering fingerprint bridge scheduled task…")
        try:
            subprocess.run(
                ["schtasks", "/run", "/tn", SCHEDULED_TASK],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                shell=False,
            )
        except Exception as exc:
            logger.debug("schtasks /run failed: %s", exc)

    def _kill_lingering(self) -> None:
        """A previous backend session may have left FingerprintBridge.exe
        holding the U.are.U device. New spawn would fail with DPFP_INIT_ERROR
        until we release it."""
        try:
            subprocess.run(
                ["taskkill", "/f", "/im", "FingerprintBridge.exe"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                shell=False,
            )
        except Exception:
            pass

    def _spawn_exe_blocking(self) -> None:
        exe = _find_bridge_exe()
        if not exe:
            logger.warning("FingerprintBridge.exe not found; install the bridge scheduled task.")
            _wait(10.0, self._stop)
            return
        # Release the scanner from any zombie bridge before claiming it.
        self._kill_lingering()
        logger.info("Starting fingerprint bridge exe: %s", exe)
        try:
            proc = subprocess.Popen(
                [str(exe)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                bufsize=0,
            )
        except Exception as exc:
            logger.error("Failed to spawn bridge: %s", exc)
            return
        self._set_status("ready")
        try:
            self._read_lines(proc.stdout)
        finally:
            try:
                proc.kill()
            except Exception:
                pass

    def _read_lines(self, stream) -> None:
        try:
            for raw in iter(stream.readline, b""):
                if self._stop.is_set():
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("Bridge non-JSON: %s", line)
                    continue
                self._handle_msg(msg)
        except Exception as exc:
            logger.warning("Bridge stream error: %s", exc)
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def _handle_msg(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "status":
            self._set_status("ready" if msg.get("status") == "ready" else "disconnected")
        elif t == "scan":
            self._set_status("scanning")
            try:
                data = b64decode(msg.get("data", ""))
            except Exception:
                data = b""
            self._emit({
                "type": "scan",
                "width": int(msg.get("width", 0)),
                "height": int(msg.get("height", 0)),
                "data": data,
            })
            self._set_status("ready")
        elif t == "quality":
            self._emit({"type": "quality", "reject": msg.get("reject")})
        elif t == "error":
            logger.error("Bridge error [%s]: %s", msg.get("code"), msg.get("message"))
            self._emit({"type": "bridge_error", "code": msg.get("code"), "message": msg.get("message")})


def _wait(seconds: float, stop: threading.Event) -> None:
    """Sleep up to N seconds, but return early when stop is set."""
    deadline = asyncio_now() + seconds
    while not stop.is_set() and asyncio_now() < deadline:
        stop.wait(0.2)


def asyncio_now() -> float:
    import time
    return time.monotonic()


scanner_bridge = ScannerBridge()
