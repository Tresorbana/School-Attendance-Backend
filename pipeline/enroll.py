import pickle
import time
from typing import List

import cv2
import numpy as np
from scipy.interpolate import RBFInterpolator
from scipy.spatial import KDTree

from .logger import log_event
from .pipeline_config import PIPELINE_CONFIG
from .preprocess import preprocess, preprocess_for_template
from .quality import check_quality


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _create_template(img: np.ndarray, config: dict = None):
    """Return pickled template bytes — backend chosen by config (NBIS/embedding/minutiae)."""
    cfg = config or PIPELINE_CONFIG
    if len(img.shape) == 3:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if cfg.get("USE_NBIS_MATCHER", False):
        from .nbis import NbisTemplate
        return pickle.dumps(NbisTemplate(img))
    if cfg.get("USE_EMBEDDING_MATCHER", False):
        from .embedding import EmbeddingTemplate
        return pickle.dumps(EmbeddingTemplate(img))
    from .minutiae import FingerprintTemplate
    return pickle.dumps(FingerprintTemplate(img))


def _get_point_cloud(template_bytes: bytes, fallback_image: np.ndarray = None) -> np.ndarray:
    """
    Extract (x, y) minutiae coords from a pickled FingerprintTemplate.
    Falls back to ORB keypoints when template has no minutiae.
    Returns shape (N, 2) float64 array; may be empty.
    """
    try:
        template = pickle.loads(template_bytes)
        if hasattr(template, "minutiae") and template.minutiae:
            pts = np.array([[m.x, m.y] for m in template.minutiae], dtype=np.float64)
            if len(pts) >= 3:
                return pts
    except Exception:
        pass

    # Fall back to keypoint detection on the preprocessed image
    if fallback_image is not None:
        try:
            detector = cv2.ORB_create(nfeatures=150)
            kps = detector.detect(fallback_image, None)
            if kps:
                return np.array([[kp.pt[0], kp.pt[1]] for kp in kps], dtype=np.float64)
        except Exception:
            pass

    return np.empty((0, 2), dtype=np.float64)


def _icp_align(ref_pts: np.ndarray, src_pts: np.ndarray, config: dict):
    """
    Iterative Closest Point rigid alignment.
    Aligns src_pts onto ref_pts.
    Returns (R: 2Ã—2, t: (2,), final_mean_dist: float).
    """
    max_iter = int(config.get("ICP_MAX_ITERATIONS", 50))
    conv_thresh = float(config.get("ICP_CONVERGENCE_THRESH", 1.5))
    max_pair_dist = 20.0

    R_total = np.eye(2, dtype=np.float64)
    t_total = np.zeros(2, dtype=np.float64)
    current = src_pts.copy()
    tree = KDTree(ref_pts)
    last_mean_dist = float("inf")

    for _ in range(max_iter):
        dists, indices = tree.query(current)
        mask = dists < max_pair_dist
        if mask.sum() < 3:
            break

        last_mean_dist = float(dists[mask].mean())
        if last_mean_dist < conv_thresh:
            break

        pts_src = current[mask]
        pts_ref = ref_pts[indices[mask]]

        c_src = pts_src.mean(axis=0)
        c_ref = pts_ref.mean(axis=0)

        H = (pts_src - c_src).T @ (pts_ref - c_ref)
        U, _, Vt = np.linalg.svd(H)

        R_iter = Vt.T @ U.T
        if np.linalg.det(R_iter) < 0:
            Vt[-1, :] *= -1
            R_iter = Vt.T @ U.T

        t_iter = c_ref - R_iter @ c_src

        current = (R_iter @ current.T).T + t_iter
        R_total = R_iter @ R_total
        t_total = R_iter @ t_total + t_iter

    return R_total, t_total, last_mean_dist


def _warp_affine(image: np.ndarray, R: np.ndarray, t: np.ndarray) -> np.ndarray:
    M = np.column_stack([R, t]).astype(np.float32)
    h, w = image.shape[:2]
    return cv2.warpAffine(image, M, (w, h), flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_REPLICATE)


def _tps_warp(ref_image: np.ndarray, warp_image: np.ndarray,
              src_pts: np.ndarray, tgt_pts: np.ndarray) -> np.ndarray:
    """
    Apply TPS elastic correction: warp warp_image so that its tgt_pts align
    to the reference src_pts positions.
    src_pts: minutiae in reference space (output grid coordinates)
    tgt_pts: matched minutiae in warp_image space (source sample coordinates)
    """
    if len(src_pts) < 4 or len(tgt_pts) < 4:
        return warp_image  # not enough control points

    try:
        interp_x = RBFInterpolator(src_pts, tgt_pts[:, 0], kernel="thin_plate_spline")
        interp_y = RBFInterpolator(src_pts, tgt_pts[:, 1], kernel="thin_plate_spline")

        h, w = ref_image.shape[:2]
        gx, gy = np.meshgrid(np.arange(w, dtype=np.float64),
                              np.arange(h, dtype=np.float64))
        grid_pts = np.column_stack([gx.ravel(), gy.ravel()])  # (h*w, 2)

        map_x = interp_x(grid_pts).reshape(h, w).astype(np.float32)
        map_y = interp_y(grid_pts).reshape(h, w).astype(np.float32)

        return cv2.remap(warp_image, map_x, map_y, cv2.INTER_LINEAR,
                         borderMode=cv2.BORDER_REPLICATE)
    except Exception:
        return warp_image


def _local_gabor_variance(img: np.ndarray, window: int = 15) -> np.ndarray:
    """Per-pixel weight = local Gabor response variance in windowÃ—window kernel."""
    kernel = cv2.getGaborKernel(
        (21, 21), sigma=4.0, theta=np.pi / 2,
        lambd=10.0, gamma=0.5, psi=0.0, ktype=cv2.CV_32F,
    )
    response = np.abs(cv2.filter2D(img.astype(np.float32), cv2.CV_32F, kernel))
    blur = (window, window)
    local_mean = cv2.blur(response, blur)
    local_mean_sq = cv2.blur(response ** 2, blur)
    local_var = np.maximum(local_mean_sq - local_mean ** 2, 0.0)
    return local_var


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def enroll(images_bytes: List[bytes], config: dict = PIPELINE_CONFIG) -> dict:
    """
    Build SourceAFIS templates from CLAHE-enhanced grayscale scans.

    Rewritten 2026-06-05:
      - Each accepted scan -> one FingerprintTemplate from CLAHE'd grayscale.
      - Composite = template of the highest-clarity scan (best single capture).
      - raw_templates = templates from every accepted scan; matching uses
        max(score across all of them) so probes that hit a different region
        still find an overlap.
      - ICP/TPS/mosaic dropped: they operated on skeletons and produced a
        skeleton composite that broke FingerprintTemplate's internal ridge
        detection. The helper functions are kept in this module for future
        experiments, but the public path no longer calls them.

    Returns:
        {
          "template_bytes": bytes | None,
          "raw_templates": list[bytes],
          "quality_scores": list[dict],
          "steps_applied": list[str],
          "error": str | None,
        }
    """
    t_start = time.perf_counter()
    timings: dict = {}
    steps_applied: list = []

    try:
        t0 = time.perf_counter()
        gray_scans: list = []        # (clahe_gray, quality_dict) tuples
        quality_scores: list = []

        for raw_bytes in images_bytes:
            raw_arr = np.frombuffer(raw_bytes, np.uint8)
            raw_img = cv2.imdecode(raw_arr, cv2.IMREAD_GRAYSCALE)
            if raw_img is None:
                quality_scores.append({
                    "acceptable": False, "clarity_score": 0.0, "coverage": 0.0,
                    "reason": "decode_error", "error": "Failed to decode image bytes",
                })
                continue

            q = check_quality(raw_img, config)
            quality_scores.append({k: v for k, v in q.items() if k != "image"})
            if not q.get("acceptable", False):
                continue

            gray = preprocess_for_template(raw_bytes, config)
            if isinstance(gray, dict):
                continue
            gray_scans.append((gray, q))

        timings["step_preprocess"] = time.perf_counter() - t0

        if len(gray_scans) < 2:
            log_event("enroll", {"step_timings": timings, "scores": {},
                                  "flags": {"error": "insufficient_quality_scans"},
                                  "steps_applied": steps_applied}, config)
            return {
                "template_bytes": None,
                "raw_templates": [],
                "quality_scores": quality_scores,
                "steps_applied": steps_applied,
                "error": "insufficient_quality_scans",
            }

        if not config.get("USE_SOURCEAFIS", True):
            return {
                "template_bytes": None,
                "raw_templates": [],
                "quality_scores": quality_scores,
                "steps_applied": steps_applied,
                "error": "USE_SOURCEAFIS is disabled; cannot produce template",
            }

        # Build one template per accepted scan.
        t0 = time.perf_counter()
        raw_templates: list = []
        per_scan_clarity: list = []
        for gray, q in gray_scans:
            try:
                raw_templates.append(_create_template(gray, config))
                per_scan_clarity.append(float(q.get("clarity_score", 0.0)))
            except Exception:
                pass

        if not raw_templates:
            return {
                "template_bytes": None,
                "raw_templates": [],
                "quality_scores": quality_scores,
                "steps_applied": steps_applied,
                "error": "SourceAFIS extracted no usable templates",
            }

        # Composite = the highest-clarity scan's template. SourceAFIS does its
        # own internal ridge detection per image; the multi-template scoring
        # in match.py compares the probe against raw templates, so we don't
        # need an averaged composite.
        order = sorted(range(len(raw_templates)),
                       key=lambda i: per_scan_clarity[i], reverse=True)
        max_raw = int(config.get("MAX_RAW_TEMPLATES", 4))
        # Keep only the top-N highest-clarity raw templates. With dozens of
        # presses during coverage enrollment, scoring against all of them on
        # every identify costs >10s. Top 4 captures the useful variety.
        keep = order[:max_raw]
        raw_templates = [raw_templates[i] for i in keep]
        per_scan_clarity = [per_scan_clarity[i] for i in keep]
        template_bytes = raw_templates[0]    # already highest-clarity after sort
        steps_applied.append("SOURCEAFIS_GRAYSCALE")

        timings["step_template"] = time.perf_counter() - t0
        timings["total"] = time.perf_counter() - t_start

        log_event("enroll", {
            "step_timings": timings,
            "scores": {"n_scans_used": len(raw_templates),
                       "best_clarity": per_scan_clarity[0] if per_scan_clarity else 0.0},
            "flags": {"error": None},
            "steps_applied": steps_applied,
        }, config)

        return {
            "template_bytes": template_bytes,
            "raw_templates": raw_templates,
            "quality_scores": quality_scores,
            "steps_applied": steps_applied,
            "error": None,
        }

    except Exception as exc:
        log_event("enroll", {
            "step_timings": timings, "scores": {},
            "flags": {"error": str(exc)}, "steps_applied": steps_applied,
        }, config)
        return {
            "template_bytes": None,
            "raw_templates": [],
            "quality_scores": [],
            "steps_applied": steps_applied,
            "error": f"enroll pipeline failed: {exc}",
        }


if __name__ == "__main__":
    import sys

    paths = sys.argv[1:]
    if not paths:
        print("Usage: python enroll.py <img1> <img2> ... <img5>")
        sys.exit(1)

    images = []
    for p in paths:
        with open(p, "rb") as f:
            images.append(f.read())

    result = enroll(images)
    print("steps_applied:", result["steps_applied"])
    print("quality_scores:", result["quality_scores"])
    print("error:", result["error"])
    print("template_bytes length:", len(result["template_bytes"]) if result["template_bytes"] else 0)
    print("raw_templates count:", len(result["raw_templates"]))
