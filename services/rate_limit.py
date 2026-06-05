"""In-memory IP-keyed rate limiter mirroring NestJS RateLimitGuard."""
import threading
import time
from typing import Dict

from fastapi import HTTPException, Request, status


class _Bucket:
    __slots__ = ("count", "reset_at")

    def __init__(self, count: int, reset_at: float) -> None:
        self.count = count
        self.reset_at = reset_at


class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: float) -> None:
        self.max = max_requests
        self.window = window_seconds
        self._buckets: Dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def __call__(self, request: Request) -> None:
        ip = request.client.host if request.client else "unknown"
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            ip = forwarded.split(",")[0].strip()

        now = time.monotonic()
        with self._lock:
            entry = self._buckets.get(ip)
            if entry is None or now > entry.reset_at:
                self._buckets[ip] = _Bucket(1, now + self.window)
                return
            entry.count += 1
            if entry.count > self.max:
                retry_after = int(entry.reset_at - now) + 1
                raise HTTPException(
                    status.HTTP_429_TOO_MANY_REQUESTS,
                    f"Too many requests. Try again in {retry_after} seconds.",
                )


login_limiter = RateLimiter(max_requests=5, window_seconds=60)
api_limiter = RateLimiter(max_requests=30, window_seconds=60)
