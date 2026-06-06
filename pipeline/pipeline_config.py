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

    # Adaptive 3-tier decision (default ON). Probe is confirmed when ANY
    # of the tiers fires. Designed to confirm honest users in 1 try while
    # still rejecting impostors (whose scores cluster ~3–10 with no clear
    # winner). To revert to strict-70 mode set USE_ADAPTIVE_THRESHOLD=False
    # — then the legacy MATCH_THRESHOLD_CONFIRM gate below is used.
    "USE_ADAPTIVE_THRESHOLD": True,

    # ── Tier 1 — Absolute strong (single-finger high score) ─────────────
    "MATCH_TIER1_STRONG": 50,

    # ── Tier 2 — Clear winner over runner-up ────────────────────────────
    "MATCH_TIER2_MIN":   15,   # absolute floor
    "MATCH_TIER2_RATIO": 2.0,  # best / 2nd
    "MATCH_TIER2_GAP":   10,   # best − 2nd

    # ── Tier 3 — Dominant leader (modest absolute but no real competition) ─
    "MATCH_TIER3_MIN":   12,
    "MATCH_TIER3_RATIO": 3.0,

    # Legacy strict-mode gate (only used when USE_ADAPTIVE_THRESHOLD=False)
    "MATCH_THRESHOLD_CONFIRM": 70,
    "MATCH_THRESHOLD_WARN": 10,
    "MATCH_GAP_MIN_ABS": 15.0,
    "MATCH_GAP_MIN_RATIO": 1.5,
    "MATCH_QUALITY_SCALE": True,
    "MATCH_QUALITY_FLOOR": 0.35,
    "MATCH_QUALITY_PENALTY": 5.0,

    # ── Multi-template (per-person raw templates) ───────────────────────
    # Probe is checked against raw templates whenever composite scores
    # above MULTI_TEMPLATE_MIN_COMPOSITE (kept low so we always exploit
    # the per-scan templates for ambiguous cases). MAX_RAW_TEMPLATES
    # bounds identify time.
    "USE_MULTI_TEMPLATE": True,
    "MULTI_TEMPLATE_STRATEGY": "max",
    "MULTI_TEMPLATE_MIN_COMPOSITE": 3,
    "MAX_RAW_TEMPLATES": 4,

    # ── Speed / parallelism ─────────────────────────────────────────────
    "MATCH_PARALLEL": True,              # parallel BFS across persons
    "MATCH_PARALLEL_WORKERS": 4,         # tuned for 4-core scanners
    "MATCH_EARLY_EXIT_SCORE": 65.0,      # raised from 55 — fewer false early exits

    # ── Logging ─────────────────────────────────────────────────────────
    "LOG_ENABLED": True,
    "LOG_FILE": "fingerprint_log.txt",
}
