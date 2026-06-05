import cv2
import numpy as np
from .pipeline_config import PIPELINE_CONFIG


def check_quality(image: np.ndarray, config: dict = PIPELINE_CONFIG) -> dict:
    """
    Assess image quality before accepting a fingerprint scan.
    Returns:
        { "acceptable": bool, "clarity_score": float, "coverage": float, "reason": str }
    reason is one of: "ok" | "low_clarity" | "low_coverage" | "gate_disabled"

    clarity_score: normalised pixel std-dev (std / 60).
      Blank/uniform image â†’ std â‰ˆ 2  â†’ score â‰ˆ 0.03
      Real fingerprint    â†’ std â‰ˆ 40â€“70 â†’ score â‰ˆ 0.67â€“1.0
    """
    try:
        # TOGGLE: USE_QUALITY_GATE
        if not config.get("USE_QUALITY_GATE", True):
            return {
                "acceptable": True,
                "clarity_score": 1.0,
                "coverage": 1.0,
                "reason": "gate_disabled",
            }

        if isinstance(image, dict):
            return {"acceptable": False, "clarity_score": 0.0, "coverage": 0.0,
                    "reason": "low_clarity", "error": "received error dict instead of image"}

        gray = image
        if len(gray.shape) == 3:
            gray = cv2.cvtColor(gray, cv2.COLOR_BGR2GRAY)
        if gray.dtype != np.uint8:
            gray = np.clip(gray, 0, 255).astype(np.uint8)

        # Clarity: normalised global standard deviation.
        # Gabor-variance fails on fingerprints where ridges cover the full sensor
        # (uniform Gabor response â†’ low variance â†’ falsely poor score).
        # Std-dev reliably separates blank images (std â‰ˆ 2) from real scans (std â‰ˆ 40+).
        clarity_score = float(min(gray.std() / 60.0, 1.0))

        # Coverage: fraction of non-background pixels via Otsu threshold.
        _, fg_mask = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        coverage = float(np.count_nonzero(fg_mask) / fg_mask.size)

        min_clarity = float(config.get("QUALITY_MIN_CLARITY", 0.05))
        min_coverage = float(config.get("QUALITY_MIN_COVERAGE", 0.1))

        if clarity_score < min_clarity:
            return {
                "acceptable": False,
                "clarity_score": clarity_score,
                "coverage": coverage,
                "reason": "low_clarity",
            }
        if coverage < min_coverage:
            return {
                "acceptable": False,
                "clarity_score": clarity_score,
                "coverage": coverage,
                "reason": "low_coverage",
            }
        return {
            "acceptable": True,
            "clarity_score": clarity_score,
            "coverage": coverage,
            "reason": "ok",
        }

    except Exception as exc:
        return {
            "acceptable": False,
            "clarity_score": 0.0,
            "coverage": 0.0,
            "reason": "low_clarity",
            "error": f"check_quality failed: {exc}",
        }


if __name__ == "__main__":
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else None
    if path is None:
        print("Usage: python quality.py <image_path>")
        sys.exit(1)
    with open(path, "rb") as f:
        data = f.read()
    nparr = np.frombuffer(data, np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_GRAYSCALE)
    result = check_quality(img)
    print(result)
