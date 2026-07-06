"""In-memory cache of pre-deserialized enrolled templates (all school staff)."""
import os
import tempfile
import threading
import time

from sqlalchemy.exc import DBAPIError, OperationalError

from database import TemplatesSession, templates_engine
from pipeline_db import load_all_enrolled

_VERSION_PATH = os.path.join(tempfile.gettempdir(), "fp_cache_v")


class TemplateCache:
    TTL = 60.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: list = []
        self._loaded_at: float = 0.0
        self._file_v: int = 0

    def _read_file_v(self) -> int:
        try:
            with open(_VERSION_PATH) as fh:
                return int(fh.read().strip())
        except Exception:
            return 0

    def get(self) -> list:
        with self._lock:
            now = time.monotonic()
            file_v = self._read_file_v()
            if (now - self._loaded_at) > self.TTL or file_v != self._file_v:
                self._data = self._load()
                self._loaded_at = now
                self._file_v = file_v
            return self._data

    def _load(self) -> list:
        last_exc: Exception | None = None
        for _ in range(2):
            db = TemplatesSession()
            try:
                return load_all_enrolled(db, station_id=None)
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
            v = self._read_file_v() + 1
            try:
                with open(_VERSION_PATH, "w") as fh:
                    fh.write(str(v))
            except Exception:
                pass
            self._data = []
            self._loaded_at = 0.0


template_cache = TemplateCache()
