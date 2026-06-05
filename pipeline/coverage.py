"""
Coverage-driven enrollment helper.

Replaces fixed-N enrollment with an adaptive loop that keeps capturing until
the fingerprint pattern is sufficiently covered.  We divide the sensor area
into a GÃ—G grid and track which cells are "captured" â€” a cell counts as
captured when it contains enough minutiae across the accumulated scans.

Per response the caller (UI) receives:
  - per-cell coverage mask (for animated UI)
  - global completion percentage
  - directional hint pointing at the LARGEST uncovered region
  - stall flag when recent scans add nothing new (user is pressing same spot)
"""

from __future__ import annotations

import base64
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, List, Optional, Tuple

import cv2
import numpy as np

from .pipeline_config import PIPELINE_CONFIG
from .preprocess import preprocess
from .quality import check_quality


@dataclass
class CoverageState:
    """Per-session state tracked across multiple /coverage calls."""
    grid_size: int
    cells: np.ndarray              # GÃ—G int â€” count of unique minutiae per cell
    total_minutiae: int            # accumulated unique minutiae
    scans_accepted: int            # number of scans that passed quality gate
    scans_attempted: int           # total scans regardless of acceptance
    image_h: int                   # canonical image dims (first scan)
    image_w: int
    recent_new_cells: Deque[int] = field(default_factory=lambda: deque(maxlen=8))

    def to_dict(self, config: dict = PIPELINE_CONFIG) -> dict:
        target = float(config.get("COVERAGE_TARGET_REGIONS", 0.55))
        min_total = int(config.get("COVERAGE_MIN_MINUTIAE_TOTAL", 70))
        min_scans = int(config.get("COVERAGE_MIN_SCANS", 3))
        max_scans = int(config.get("COVERAGE_MAX_SCANS", 10))
        stall_window = int(config.get("COVERAGE_STALL_WINDOW", 3))

        n_cells = self.grid_size * self.grid_size
        covered_cells = int(np.count_nonzero(self.cells))
        coverage_ratio = covered_cells / float(n_cells)

        # Completion criteria â€” ALL must pass:
        complete = (
            coverage_ratio >= target
            and self.total_minutiae >= min_total
            and self.scans_accepted >= min_scans
        )
        ceiling_hit = self.scans_attempted >= max_scans

        # Stall detection: last N scans added zero new cells
        stalled = False
        if len(self.recent_new_cells) >= stall_window:
            tail = list(self.recent_new_cells)[-stall_window:]
            stalled = all(v == 0 for v in tail) and not complete

        hint = _hint_for_state(self, config)

        return {
            "grid_size": self.grid_size,
            "cells": self.cells.tolist(),
            "covered_cells": covered_cells,
            "total_cells": n_cells,
            "coverage_ratio": round(coverage_ratio, 4),
            "total_minutiae": self.total_minutiae,
            "scans_accepted": self.scans_accepted,
            "scans_attempted": self.scans_attempted,
            "target_coverage": target,
            "target_minutiae": min_total,
            "complete": complete,
            "ceiling_hit": ceiling_hit,
            "should_continue": not complete and not ceiling_hit,
            "stalled": stalled,
            "hint": hint,                       # human-readable guidance string
            "hint_direction": hint["direction"] if hint else None,
        }


def new_state(config: dict = PIPELINE_CONFIG) -> CoverageState:
    g = int(config.get("COVERAGE_GRID_SIZE", 5))
    return CoverageState(
        grid_size=g,
        cells=np.zeros((g, g), dtype=np.int32),
        total_minutiae=0,
        scans_accepted=0,
        scans_attempted=0,
        image_h=0,
        image_w=0,
    )


# ---------------------------------------------------------------------------
# Directional hint
# ---------------------------------------------------------------------------

_DIRECTION_MESSAGES = {
    "top-left":     "Tilt the finger toward the top-left of the sensor",
    "top":          "Press more of the upper part of the finger",
    "top-right":    "Tilt the finger toward the top-right of the sensor",
    "left":         "Roll the finger toward the left edge",
    "center":       "Press the centre of the finger firmly",
    "right":        "Roll the finger toward the right edge",
    "bottom-left":  "Tilt the finger toward the bottom-left of the sensor",
    "bottom":       "Press more of the lower part of the finger",
    "bottom-right": "Tilt the finger toward the bottom-right of the sensor",
}


def _hint_for_state(state: CoverageState, config: dict) -> Optional[dict]:
    """
    Compute a directional hint pointing at the LARGEST uncovered region.

    Returns:
        { "direction": "top-left" | "center" | â€¦, "message": str } | None
    """
    mask = state.cells == 0
    if not mask.any():
        return None

    g = state.grid_size
    ys, xs = np.where(mask)
    cy = float(ys.mean())
    cx = float(xs.mean())

    center_idx = (g - 1) / 2.0
    dead_zone = float(config.get("COVERAGE_HINT_DEAD_ZONE", 0.18)) * g

    dy = cy - center_idx
    dx = cx - center_idx

    vertical = "bottom" if dy > dead_zone else ("top" if dy < -dead_zone else "")
    horizontal = "right" if dx > dead_zone else ("left" if dx < -dead_zone else "")

    if not vertical and not horizontal:
        direction = "center"
    else:
        direction = "-".join(filter(None, [vertical, horizontal]))

    return {
        "direction": direction,
        "message": _DIRECTION_MESSAGES.get(direction, "Reposition the finger and try again"),
    }


# ---------------------------------------------------------------------------
# Scan accumulation
# ---------------------------------------------------------------------------

def accumulate_scan(
    state: CoverageState,
    image_bytes: bytes,
    config: dict = PIPELINE_CONFIG,
) -> dict:
    """Update coverage state with a new scan. Aggregate state mutated in place."""
    state.scans_attempted += 1

    def _push_progress(new_cells: int) -> None:
        """Record new-cell delta in the rolling window for stall detection."""
        state.recent_new_cells.append(new_cells)

    raw_arr = np.frombuffer(image_bytes, np.uint8)
    raw_img = cv2.imdecode(raw_arr, cv2.IMREAD_GRAYSCALE)
    if raw_img is None:
        return {
            "accepted": False, "reason": "decode_error",
            "clarity_score": 0.0, "coverage": 0.0,
            "minutiae_count": 0, "new_cells_covered": 0,
        }

    q = check_quality(raw_img, config)
    if not q.get("acceptable", False):
        return {
            "accepted": False, "reason": q.get("reason", "low_clarity"),
            "clarity_score": q.get("clarity_score", 0.0),
            "coverage": q.get("coverage", 0.0),
            "minutiae_count": 0, "new_cells_covered": 0,
        }

    pre = preprocess(image_bytes, config)
    if isinstance(pre, dict):
        return {
            "accepted": False, "reason": "preprocess_error",
            "clarity_score": q.get("clarity_score", 0.0),
            "coverage": q.get("coverage", 0.0),
            "minutiae_count": 0, "new_cells_covered": 0,
        }

    if state.image_h == 0:
        state.image_h, state.image_w = pre.shape[:2]

    try:
        from .minutiae import FingerprintTemplate
        tpl = FingerprintTemplate(pre)
    except Exception:
        return {
            "accepted": False, "reason": "template_error",
            "clarity_score": q.get("clarity_score", 0.0),
            "coverage": q.get("coverage", 0.0),
            "minutiae_count": 0, "new_cells_covered": 0,
        }

    minutiae = tpl.minutiae
    min_minutiae_per_scan = max(8, int(config.get("QUALITY_MIN_MINUTIAE_PROBE", 14)) // 2)
    if len(minutiae) < min_minutiae_per_scan:
        return {
            "accepted": False, "reason": "few_minutiae",
            "clarity_score": q.get("clarity_score", 0.0),
            "coverage": q.get("coverage", 0.0),
            "minutiae_count": len(minutiae), "new_cells_covered": 0,
        }

    # Coverage is computed from the RIDGE MASK of the preprocessed image,
    # not from minutiae locations. Minutiae cluster centrally (detector bias
    # + border margin), so a minutiae-grid never reaches >30% even when the
    # whole sensor is touched. The preprocessed skeleton/binary directly
    # tells us where the finger actually contacted the sensor.
    g = state.grid_size
    h, w = pre.shape[:2]
    cell_h, cell_w = max(1, h // g), max(1, w // g)

    # `pre` is the preprocessed image — either skeletonized (0/1) or
    # grayscale-enhanced. Threshold to a binary mask, then count per cell.
    if pre.dtype != np.uint8:
        mask_src = pre.astype(np.uint8)
    else:
        mask_src = pre
    # Any "dark" / "ridge" pixel counts as ridge content. SourceAFIS
    # preprocessing emits values in a wide range; >0 is enough since
    # background is normalized to 0.
    ridge_mask = (mask_src > 0).astype(np.uint8)

    # Each cell counts as covered once its ridge density crosses a
    # small fraction of the cell area — robust against speckle.
    cell_area = max(1, cell_h * cell_w)
    min_ridge_pixels = max(50, int(cell_area * 0.05))  # 5% of cell, floor 50px

    before = (state.cells > 0).astype(np.int8)
    for gy in range(g):
        for gx in range(g):
            y0, y1 = gy * cell_h, (gy + 1) * cell_h if gy < g - 1 else h
            x0, x1 = gx * cell_w, (gx + 1) * cell_w if gx < g - 1 else w
            density = int(ridge_mask[y0:y1, x0:x1].sum())
            if density >= min_ridge_pixels:
                state.cells[gy, gx] = max(state.cells[gy, gx], density)

    after = (state.cells > 0).astype(np.int8)
    new_cells = int((after - before).sum())

    state.total_minutiae += len(minutiae)
    state.scans_accepted += 1
    _push_progress(new_cells)

    return {
        "accepted": True, "reason": "ok",
        "clarity_score": q.get("clarity_score", 0.0),
        "coverage": q.get("coverage", 0.0),
        "minutiae_count": len(minutiae), "new_cells_covered": new_cells,
    }


def decode_b64_image(b64: str) -> bytes:
    return base64.b64decode(b64)
