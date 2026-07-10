"""
SAMS — Staff Attendance Management System backend.
Listens on port 8000 with /api/* prefix.
"""
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from config import settings
from database import init_db
from routers import (
    auth, attendance, fingerprint, fingerprint_ws,
    holidays, leave, notifications, people, reports, users,
)
from scanner.bridge_client import scanner_bridge

logging.basicConfig(
    level=logging.INFO if settings.NODE_ENV == "production" else logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("app")

_MAX_BODY_BYTES = 20 * 1024 * 1024  # 20 MB — enough for fingerprint images


def _log_matcher_status() -> None:
    from pipeline.pipeline_config import PIPELINE_CONFIG
    if PIPELINE_CONFIG.get("USE_NBIS_MATCHER"):
        from pipeline import nbis
        if nbis.is_available():
            logger.info("Matcher: NBIS (bozorth3) — biometric-grade")
        else:
            logger.error(
                "Matcher: NBIS requested but binaries missing. "
                "Place mindtct + bozorth3 in backend/bridge/nbis/. Falling back."
            )
    elif PIPELINE_CONFIG.get("USE_EMBEDDING_MATCHER"):
        logger.info("Matcher: DINOv2 embedding")
    else:
        logger.info("Matcher: minutiae BFS (homebrew)")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    _log_matcher_status()
    logger.info("School: %s", settings.SCHOOL_NAME)
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
    title="SAMS — Staff Attendance Management System",
    description="Staff Attendance Management System API",
    version="3.0.0",
    lifespan=lifespan,
    docs_url="/api/docs" if settings.NODE_ENV != "production" else None,
    redoc_url=None,
)

# ── Request body size limit ────────────────────────────────────────────
@app.middleware("http")
async def limit_body_size(request: Request, call_next) -> Response:
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > _MAX_BODY_BYTES:
        return Response("Request body too large", status_code=413)
    return await call_next(request)


# ── Security headers ───────────────────────────────────────────────────
@app.middleware("http")
async def security_headers(request: Request, call_next) -> Response:
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if settings.NODE_ENV == "production":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src 'self' https://fonts.gstatic.com; "
            "img-src 'self' data:; "
            "connect-src 'self' wss:; "
            "frame-ancestors 'none';"
        )
    return response


# ── CORS ────────────────────────────────────────────────────────────────
if settings.NODE_ENV == "production" and settings.CORS_ORIGIN == "*":
    logger.warning("CORS_ORIGIN is '*' in production — set it to your frontend domain in .env")

app.add_middleware(GZipMiddleware, minimum_size=1024)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.CORS_ORIGIN] if settings.CORS_ORIGIN != "*" else ["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
    expose_headers=["Content-Disposition"],
)

# ── Routes ──────────────────────────────────────────────────────────────
api_prefix = "/api"
app.include_router(auth.router, prefix=api_prefix)
app.include_router(users.router, prefix=api_prefix)
app.include_router(people.router, prefix=api_prefix)
app.include_router(leave.router, prefix=api_prefix)
app.include_router(notifications.router, prefix=api_prefix)
app.include_router(holidays.router, prefix=api_prefix)
app.include_router(attendance.router, prefix=api_prefix)
app.include_router(reports.router, prefix=api_prefix)
app.include_router(fingerprint.router, prefix=api_prefix)
app.include_router(fingerprint_ws.router)


@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "school": settings.SCHOOL_NAME,
        "scanner": scanner_bridge.status,
    }


# ── Serve Next.js static files (installer / packaged exe mode) ────────────
if settings.STATIC_DIR:
    import os
    if os.path.isdir(settings.STATIC_DIR):
        app.mount("/", StaticFiles(directory=settings.STATIC_DIR, html=True), name="frontend")
        logger.info("Serving frontend static files from: %s", settings.STATIC_DIR)
    else:
        logger.warning("STATIC_DIR set but not found: %s — frontend not served", settings.STATIC_DIR)


if __name__ == "__main__":
    import sys
    if len(sys.argv) == 3 and sys.argv[1] == "--hash-password":
        from services.auth import hash_password
        print(hash_password(sys.argv[2]), end="")
        sys.exit(0)

    import uvicorn
    uvicorn.run("app:app", host=settings.HOST, port=settings.PORT, reload=False)
