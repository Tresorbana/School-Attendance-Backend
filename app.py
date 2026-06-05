"""
SAMS / AttendAI backend — single Python service replacing NestJS + the
old fingerprint-pipeline. Listens on port 8000 with /api/* prefix so the
existing frontend works unchanged.
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware

from config import settings
from database import init_db
from routers import auth, attendance, fingerprint, fingerprint_ws, holidays, people, reports, stations
from scanner.bridge_client import scanner_bridge

logging.basicConfig(
    level=logging.INFO if settings.NODE_ENV == "production" else logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("app")


def _log_matcher_status() -> None:
    """Print which matcher backend is active so we never wonder again."""
    from pipeline.pipeline_config import PIPELINE_CONFIG
    if PIPELINE_CONFIG.get("USE_NBIS_MATCHER"):
        from pipeline import nbis
        if nbis.is_available():
            logger.info("Matcher: NBIS (bozorth3) — biometric-grade")
        else:
            logger.error(
                "Matcher: NBIS REQUESTED but binaries missing. "
                "Place mindtct + bozorth3 in backend/bridge/nbis/ — "
                "see backend/bridge/nbis/README.md. Falling back to embedding matcher."
            )
    elif PIPELINE_CONFIG.get("USE_EMBEDDING_MATCHER"):
        logger.info("Matcher: DINOv2 embedding (no fine-tune)")
    else:
        logger.info("Matcher: minutiae BFS (homebrew)")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    _log_matcher_status()
    loop = asyncio.get_running_loop()
    scanner_bridge.start(loop)
    pump = asyncio.create_task(fingerprint_ws._scanner_event_pump())
    logger.info("SAMS backend ready on port %d [%s]", settings.PORT, settings.NODE_ENV)
    try:
        yield
    finally:
        scanner_bridge.stop()
        pump.cancel()


app = FastAPI(
    title="SAMS — Indongozi SACCO Attendance API",
    description="Staff Attendance Management System (Python backend)",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/api/docs" if settings.NODE_ENV != "production" else None,
    redoc_url=None,
)

app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.CORS_ORIGIN] if settings.CORS_ORIGIN != "*" else ["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Admin-Key"],
)

# All HTTP routes live under /api/* to match the NestJS prefix the frontend uses.
api_prefix = "/api"
app.include_router(auth.router, prefix=api_prefix)
app.include_router(people.router, prefix=api_prefix)
app.include_router(stations.router, prefix=api_prefix)
app.include_router(holidays.router, prefix=api_prefix)
app.include_router(attendance.router, prefix=api_prefix)
app.include_router(reports.router, prefix=api_prefix)
app.include_router(fingerprint.router, prefix=api_prefix)

# WebSocket gateway lives at /ws/fingerprint (no /api prefix).
app.include_router(fingerprint_ws.router)


@app.get("/api/health")
def health():
    return {"status": "ok", "scanner": scanner_bridge.status}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host=settings.HOST, port=settings.PORT, reload=False)
