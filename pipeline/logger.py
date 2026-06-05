import json
import os
from datetime import datetime
from .pipeline_config import PIPELINE_CONFIG


def log_event(event_type: str, data: dict, config: dict = PIPELINE_CONFIG) -> None:
    # TOGGLE: LOG_ENABLED
    if not config.get("LOG_ENABLED", True):
        return

    try:
        log_file = config.get("LOG_FILE", "fingerprint_log.txt")
        entry = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "event_type": event_type,
            "step_timings": data.get("step_timings", {}),
            "scores": data.get("scores", {}),
            "flags": data.get("flags", {}),
            "steps_applied": data.get("steps_applied", []),
        }
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # logging must never crash the caller
