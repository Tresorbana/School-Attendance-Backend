"""In-memory cache of pre-deserialized enrolled templates for fast /identify."""
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
        self._data: list | None = None
        self._loaded_at: float = 0.0
        self._file_version: int = -1

    def _file_v(self) -> int:
        try:
            with open(_VERSION_PATH) as fh:
                return int(fh.read().strip())
        except Exception:
            return 0

    def get(self) -> list:
        with self._lock:
            now = time.monotonic()
            file_v = self._file_v()
            if (
                self._data is None
                or (now - self._loaded_at) > self.TTL
                or file_v != self._file_version
            ):
                last_exc: Exception | None = None
                for attempt in range(2):
                    db = TemplatesSession()
                    try:
                        self._data = load_all_enrolled(db)
                        last_exc = None
                        break
                    except (OperationalError, DBAPIError) as exc:
                        last_exc = exc
                        try:
                            db.close()
                        except Exception:
                            pass
                        templates_engine.dispose()
                        continue
                    finally:
                        try:
                            db.close()
                        except Exception:
                            pass
                if last_exc is not None:
                    raise last_exc
                self._loaded_at = now
                self._file_version = file_v
            return self._data

    def invalidate(self) -> None:
        with self._lock:
            v = self._file_v() + 1
            try:
                with open(_VERSION_PATH, "w") as fh:
                    fh.write(str(v))
            except Exception:
                pass
            self._data = None
            self._loaded_at = 0.0
            self._file_version = v


template_cache = TemplateCache()
