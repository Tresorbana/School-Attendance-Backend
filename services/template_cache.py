"""In-memory cache of pre-deserialized enrolled templates, keyed by station_id."""
import os
import tempfile
import threading
import time
from typing import Optional

from sqlalchemy.exc import DBAPIError, OperationalError

from database import TemplatesSession, templates_engine
from pipeline_db import load_all_enrolled

_VERSION_PATH = os.path.join(tempfile.gettempdir(), "fp_cache_v")


class TemplateCache:
    TTL = 60.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Keyed by station_id (None = all templates for super-admin contexts)
        self._buckets: dict[Optional[int], tuple[list, float, int]] = {}

    def _file_v(self) -> int:
        try:
            with open(_VERSION_PATH) as fh:
                return int(fh.read().strip())
        except Exception:
            return 0

    def get(self, station_id: Optional[int] = None) -> list:
        with self._lock:
            now = time.monotonic()
            file_v = self._file_v()
            bucket = self._buckets.get(station_id)

            if bucket is None or (now - bucket[1]) > self.TTL or file_v != bucket[2]:
                data = self._load(station_id)
                self._buckets[station_id] = (data, now, file_v)
                return data

            return bucket[0]

    def _load(self, station_id: Optional[int]) -> list:
        last_exc: Exception | None = None
        for _ in range(2):
            db = TemplatesSession()
            try:
                return load_all_enrolled(db, station_id)
            except (OperationalError, DBAPIError) as exc:
                last_exc = exc
                try:
                    db.close()
                except Exception:
                    pass
                templates_engine.dispose()
            finally:
                try:
                    db.close()
                except Exception:
                    pass
        if last_exc is not None:
            raise last_exc
        return []

    def invalidate(self) -> None:
        with self._lock:
            v = self._file_v() + 1
            try:
                with open(_VERSION_PATH, "w") as fh:
                    fh.write(str(v))
            except Exception:
                pass
            self._buckets.clear()


template_cache = TemplateCache()
