PIPELINE_CONFIG = {
    # ── Preprocessing ──────────────────────────────────────────────────
    "USE_GRAYSCALE": True,
    "USE_CLAHE": True,
    "CLAHE_CLIP_LIMIT": 2.0,
    "CLAHE_TILE_SIZE": (8, 8),
    "USE_GABOR": True,
    "GABOR_ORIENTATIONS": 8,
    "USE_NORMALIZATION": True,
    "USE_BINARIZE": True,
    "USE_SKELETONIZE": True,

    # ── Quality gate ────────────────────────────────────────────────────
    "USE_QUALITY_GATE": True,
    "QUALITY_MIN_CLARITY": 0.18,         # raised: blank scans were sneaking past 0.05
    "QUALITY_MIN_COVERAGE": 0.18,        # raised: tiny finger touches must not enroll
    "QUALITY_MIN_MINUTIAE_PROBE": 14,    # probe must yield at least this many minutiae

    # ── Coverage-driven enrollment ──────────────────────────────────────
    # Field-tuned 2026-06-05 — coverage is now measured from the RIDGE MASK
    # of the preprocessed image (real finger contact area), not from
    # minutiae locations. The old metric capped at 25–30% because minutiae
    # cluster centrally. With the ridge metric, a flat press easily reaches
    # 60–80%, so the target reflects "most of the sensor was touched."
    "ENROLLMENT_SCAN_COUNT": 5,
    "USE_COVERAGE_ENROLLMENT": True,
    "COVERAGE_GRID_SIZE": 4,             # 4×4 = 16 cells
    "COVERAGE_TARGET_REGIONS": 0.65,     # 11/16 cells of real ridge contact
    "COVERAGE_MIN_MINUTIAE_TOTAL": 80,   # raised: with broader contact we expect more minutiae
    "COVERAGE_MIN_SCANS": 4,             # ≥4 raw_templates for robust matching
    "COVERAGE_MAX_SCANS": 12,
    "COVERAGE_STALL_WINDOW": 4,
    "COVERAGE_HINT_DEAD_ZONE": 0.08,

    # ── Alignment / mosaicking ──────────────────────────────────────────
    "USE_ICP_ALIGNMENT": True,
    "ICP_MAX_ITERATIONS": 50,
    "ICP_CONVERGENCE_THRESH": 1.5,
    "USE_TPS_CORRECTION": True,
    "USE_MOSAICKING": True,
    "MOSAIC_WEIGHT_BY_CLARITY": True,

    # ── Matching backend ────────────────────────────────────────────────
    # Priority order (first True wins):
    #   1. USE_NBIS_MATCHER     — NIST bozorth3 (biometric-grade; requires binaries)
    #   2. USE_EMBEDDING_MATCHER — DINOv2 cosine (no fine-tune; not biometric-grade)
    #   3. (fallback)            — homebrew minutiae BFS matcher
    "USE_NBIS_MATCHER": True,
    "USE_EMBEDDING_MATCHER": False,

    # Legacy minutiae backend (used only when above are False/unavailable)
    "USE_SOURCEAFIS": True,

    # Calibrated for bozorth3 score distribution (NIST recommendation):
    #   Genuine same-finger:    40–200+
    #   Different fingers:      0–15
    # Threshold 40 is NIST's documented FMR ≤ 0.01% point. 50 is safer.
    "MATCH_THRESHOLD_CONFIRM": 40,
    "MATCH_THRESHOLD_WARN": 25,
    "MATCH_GAP_MIN_ABS": 10.0,
    "MATCH_GAP_MIN_RATIO": 1.5,
    "MATCH_QUALITY_SCALE": True,
    "MATCH_QUALITY_FLOOR": 0.35,
    "MATCH_QUALITY_PENALTY": 5.0,

    # ── Multi-template (per-person raw templates) ───────────────────────
    # Probe is checked against raw templates when composite is in the
    # ambiguous range. Raw templates are capped at MAX_RAW_TEMPLATES per
    # person at enrollment time so identify stays under ~2s.
    "USE_MULTI_TEMPLATE": True,
    "MULTI_TEMPLATE_STRATEGY": "max",
    "MULTI_TEMPLATE_MIN_COMPOSITE": 15,
    "MAX_RAW_TEMPLATES": 4,              # cap to keep match time bounded

    # ── Speed / parallelism ─────────────────────────────────────────────
    "MATCH_PARALLEL": True,              # parallel BFS across persons
    "MATCH_PARALLEL_WORKERS": 4,         # tuned for 4-core scanners
    "MATCH_EARLY_EXIT_SCORE": 65.0,      # raised from 55 — fewer false early exits

    # ── Logging ─────────────────────────────────────────────────────────
    "LOG_ENABLED": True,
    "LOG_FILE": "fingerprint_log.txt",
}
