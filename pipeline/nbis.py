"""
NBIS (NIST Biometric Image Software) matcher backend.

Wraps the mindtct + bozorth3 command-line binaries via subprocess. mindtct
extracts minutiae from a fingerprint image into a `.xyt` text file; bozorth3
takes two `.xyt` files and prints a match score to stdout. NIST has used
this matcher in operational AFIS deployments for decades; score distribution:

  Genuine same-finger:  40–200+ (often 80+)
  Different fingers:    0–15

NIST's recommended decision threshold is 40; many deployments use 50 to be
strict. Score is monotonic and unbounded — there is no noise floor.

Setup:
  Place mindtct.exe and bozorth3.exe under  backend/bridge/nbis/
  (Override with the NBIS_PATH environment variable.)

Public API (matches embedding.py / minutiae.py):
  template = NbisTemplate(grayscale_image)        # np.ndarray uint8
  matcher  = NbisMatcher(probe_template)
  score    = matcher.match(candidate_template)    # float 0–200+
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

logger = logging.getLogger("nbis")

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
_NBIS_SEARCH_DIRS = [
    Path(os.environ["NBIS_PATH"]) if os.environ.get("NBIS_PATH") else None,
    _BACKEND / "bridge" / "nbis",
    _BACKEND / "nbis",
]


def _exe_name(name: str) -> str:
    return f"{name}.exe" if sys.platform == "win32" else name


_lock = threading.Lock()
_mindtct: Optional[Path] = None
_bozorth3: Optional[Path] = None


def _find_binaries() -> tuple[Optional[Path], Optional[Path]]:
    mindtct = bozorth3 = None
    for d in _NBIS_SEARCH_DIRS:
        if d is None or not d.exists():
            continue
        m = d / _exe_name("mindtct")
        b = d / _exe_name("bozorth3")
        if m.exists() and not mindtct:
            mindtct = m
        if b.exists() and not bozorth3:
            bozorth3 = b
    return mindtct, bozorth3


def _ensure_binaries() -> None:
    global _mindtct, _bozorth3
    if _mindtct and _bozorth3:
        return
    with _lock:
        if _mindtct and _bozorth3:
            return
        m, b = _find_binaries()
        if not m or not b:
            searched = ", ".join(str(d) for d in _NBIS_SEARCH_DIRS if d is not None)
            raise RuntimeError(
                f"NBIS binaries not found. Looked in: {searched}\n"
                "Download mindtct + bozorth3 from NIST NBIS and place them in "
                "backend/bridge/nbis/ — see backend/bridge/nbis/README.md."
            )
        _mindtct, _bozorth3 = m, b
        logger.info("NBIS ready — mindtct=%s bozorth3=%s", m, b)


def is_available() -> bool:
    """Check whether NBIS binaries are present without raising."""
    m, b = _find_binaries()
    return bool(m and b)


# ── PGM helpers ─────────────────────────────────────────────────────────


def _save_pgm(img: np.ndarray, path: Path) -> None:
    """Write a uint8 grayscale numpy array to PGM (binary P5)."""
    if img.dtype != np.uint8:
        img = np.clip(img, 0, 255).astype(np.uint8)
    if len(img.shape) == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    h, w = img.shape[:2]
    with open(path, "wb") as f:
        f.write(f"P5\n{w} {h}\n255\n".encode("ascii"))
        f.write(img.tobytes())


# ── mindtct: image -> minutiae .xyt ─────────────────────────────────────


def _mindtct_extract(image: np.ndarray) -> bytes:
    """Run mindtct, return the resulting .xyt file content as bytes."""
    _ensure_binaries()
    with tempfile.TemporaryDirectory(prefix="nbis_mt_") as tmp:
        tmp_path = Path(tmp)
        pgm_path = tmp_path / "input.pgm"
        out_root = tmp_path / "out"
        _save_pgm(image, pgm_path)
        try:
            subprocess.run(
                [str(_mindtct), str(pgm_path), str(out_root)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except subprocess.TimeoutExpired:
            logger.warning("mindtct timed out")
            return b""
        except Exception as exc:
            logger.warning("mindtct failed: %s", exc)
            return b""
        xyt = tmp_path / "out.xyt"
        if not xyt.exists():
            return b""
        return xyt.read_bytes()


# ── bozorth3: two .xyt files -> score ───────────────────────────────────


def _bozorth3_match(probe_xyt: bytes, gallery_xyt: bytes) -> float:
    """Run bozorth3 on two .xyt blobs, return the integer score it prints."""
    if not probe_xyt or not gallery_xyt:
        return 0.0
    _ensure_binaries()
    with tempfile.TemporaryDirectory(prefix="nbis_bz_") as tmp:
        tmp_path = Path(tmp)
        probe = tmp_path / "p.xyt"
        gallery = tmp_path / "g.xyt"
        probe.write_bytes(probe_xyt)
        gallery.write_bytes(gallery_xyt)
        try:
            result = subprocess.run(
                [str(_bozorth3), str(probe), str(gallery)],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except subprocess.TimeoutExpired:
            return 0.0
        except Exception as exc:
            logger.warning("bozorth3 failed: %s", exc)
            return 0.0
        out = result.stdout.decode("ascii", "ignore").strip()
        try:
            return float(out.split()[0])
        except (ValueError, IndexError):
            return 0.0


# ── Public template + matcher classes ───────────────────────────────────


class NbisTemplate:
    """Holds an NBIS .xyt minutiae file as bytes."""

    def __init__(self, image: Optional[np.ndarray] = None) -> None:
        self.xyt: bytes = b""
        # Compat shim: probe-count gate in match.py reads len(minutiae)
        self.minutiae: list = []
        if image is not None:
            self.xyt = _mindtct_extract(image)
            if self.xyt:
                # Each line after the header is one minutia "x y theta quality"
                lines = self.xyt.decode("ascii", "ignore").splitlines()
                self.minutiae = [None] * max(0, len(lines))

    def _ensure_edges(self) -> None:
        """No-op for API parity with the minutiae backend."""
        return


class NbisMatcher:
    """bozorth3-backed matcher. Score is the raw bozorth3 integer."""

    def __init__(self, probe: NbisTemplate) -> None:
        if not probe.xyt:
            raise ValueError("Probe NbisTemplate has no minutiae (.xyt empty)")
        self._probe_xyt = probe.xyt

    def match(self, candidate: NbisTemplate) -> float:
        return _bozorth3_match(self._probe_xyt, candidate.xyt)
