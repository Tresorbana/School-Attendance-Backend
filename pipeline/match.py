import time
from concurrent.futures import ThreadPoolExecutor
from typing import List, Tuple

import cv2
import numpy as np

from .logger import log_event
from .pipeline_config import PIPELINE_CONFIG
from .preprocess import preprocess, preprocess_for_template
from .quality import check_quality


def _score_one_person(
    probe_matcher,
    composite_template,
    raw_template_list,
    use_multi: bool,
    strategy: str,
    multi_min: float,
    threshold_confirm: float,
) -> float:
    """Score a probe matcher against a single enrolled person's templates."""
    try:
        best = float(probe_matcher.match(composite_template))
    except Exception:
        best = 0.0

    # Only check raw templates when composite score is in the ambiguous range
    if use_multi and multi_min <= best < threshold_confirm:
        for raw_tpl in raw_template_list:
            try:
                s = float(probe_matcher.match(raw_tpl))
                if strategy == "max":
                    if s > best:
                        best = s
                else:
                    # avg-of-top strategy uses running mean
                    best = (best + s) / 2.0
            except Exception:
                pass
    return best


def identify(
    image_bytes: bytes,
    enrolled: list,   # List[Tuple[str, FingerprintTemplate, List[FingerprintTemplate]]]
    config: dict = PIPELINE_CONFIG,
) -> dict:
    """
    1:N fingerprint identification with hallucination guards.

    Decision pipeline:
      1. Image quality gate (clarity â‰¥ MATCH_QUALITY_FLOOR else poor_scan_quality)
      2. Probe minutiae count gate (â‰¥ QUALITY_MIN_MINUTIAE_PROBE)
      3. Parallel BFS scoring across all enrolled persons
      4. Triple gate on best person:
            a. score â‰¥ effective_confirm (raised by low clarity)
            b. score âˆ’ second-best â‰¥ MATCH_GAP_MIN_ABS
            c. score / second-best â‰¥ MATCH_GAP_MIN_RATIO
         All three must pass â†’ confirmed.  Any failure that still clears the
         warn threshold â†’ low_confidence (NOT recorded).
    """
    t_start = time.perf_counter()
    timings: dict = {}
    steps_applied: list = []

    try:
        # â”€â”€ 1. Decode + quality gate â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        t0 = time.perf_counter()

        raw_arr = np.frombuffer(image_bytes, np.uint8)
        raw_img = cv2.imdecode(raw_arr, cv2.IMREAD_GRAYSCALE)
        if raw_img is None:
            return {
                "matched": False, "person_id": None, "confidence_score": 0.0,
                "flag": "poor_scan_quality", "all_scores": {},
                "error": "Failed to decode image bytes",
            }

        quality = check_quality(raw_img, config)
        clarity = float(quality.get("clarity_score", 0.0))
        clarity_floor = float(config.get("MATCH_QUALITY_FLOOR", 0.45))

        if not quality.get("acceptable", True) or clarity < clarity_floor:
            return {
                "matched": False, "person_id": None, "confidence_score": 0.0,
                "flag": "poor_scan_quality", "all_scores": {},
                "quality": quality,
            }

        # NBIS / DINOv2 want a real fingerprint image, not the heavy
        # Gabor-enhanced output of preprocess_for_template. NBIS's mindtct
        # in particular returns zero minutiae on Gabor-stylized inputs.
        if config.get("USE_NBIS_MATCHER", False):
            # mindtct gets raw grayscale; nbis.py applies its own CLAHE.
            probe_img = raw_img
        else:
            probe_img = preprocess_for_template(image_bytes, config)
            if isinstance(probe_img, dict):
                return {
                    "matched": False, "person_id": None, "confidence_score": 0.0,
                    "flag": "poor_scan_quality", "all_scores": {},
                    "error": probe_img.get("error"),
                }

        timings["step_preprocess"] = time.perf_counter() - t0

        # â”€â”€ 2. Probe template + minutiae count gate â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        t0 = time.perf_counter()

        if not config.get("USE_SOURCEAFIS", True):
            return {
                "matched": False, "person_id": None, "confidence_score": 0.0,
                "flag": "no_match", "all_scores": {},
                "error": "USE_SOURCEAFIS disabled",
            }

        try:
            if config.get("USE_NBIS_MATCHER", False):
                from .nbis import NbisMatcher as _Matcher
                from .nbis import NbisTemplate as _Template
            elif config.get("USE_EMBEDDING_MATCHER", False):
                from .embedding import EmbeddingMatcher as _Matcher
                from .embedding import EmbeddingTemplate as _Template
            else:
                from .minutiae import FingerprintMatcher as _Matcher
                from .minutiae import FingerprintTemplate as _Template
            probe_template = _Template(probe_img)
            probe_matcher = _Matcher(probe_template)
        except Exception as exc:
            print(f"[identify] FAILED to build probe template: {exc}")
            return {
                "matched": False, "person_id": None, "confidence_score": 0.0,
                "flag": "no_match", "all_scores": {},
                "error": f"probe template failed: {exc}",
            }

        min_minutiae = int(config.get("QUALITY_MIN_MINUTIAE_PROBE", 14))
        probe_count = len(probe_template.minutiae)
        if probe_count < min_minutiae:
            return {
                "matched": False, "person_id": None, "confidence_score": 0.0,
                "flag": "poor_scan_quality", "all_scores": {},
                "quality": {**quality, "probe_minutiae": probe_count},
            }

        timings["step_probe_template"] = time.perf_counter() - t0

        # â”€â”€ 3. Parallel 1:N scoring â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        t0 = time.perf_counter()

        threshold_confirm = float(config.get("MATCH_THRESHOLD_CONFIRM", 45))
        use_multi = bool(config.get("USE_MULTI_TEMPLATE", True))
        strategy = config.get("MULTI_TEMPLATE_STRATEGY", "max")
        multi_min = float(config.get("MULTI_TEMPLATE_MIN_COMPOSITE", 18))
        use_parallel = bool(config.get("MATCH_PARALLEL", True))
        n_workers = int(config.get("MATCH_PARALLEL_WORKERS", 4))

        final_scores: dict = {}

        if use_parallel and len(enrolled) > 1:
            # Parallel BFS across persons â€” each person is independent
            def _job(item):
                pid, comp, raws = item
                return pid, _score_one_person(
                    probe_matcher, comp, raws,
                    use_multi, strategy, multi_min, threshold_confirm,
                )

            with ThreadPoolExecutor(max_workers=n_workers) as pool:
                for pid, score in pool.map(_job, enrolled):
                    final_scores[pid] = score
        else:
            for pid, comp, raws in enrolled:
                final_scores[pid] = _score_one_person(
                    probe_matcher, comp, raws,
                    use_multi, strategy, multi_min, threshold_confirm,
                )

        steps_applied.append("SOURCEAFIS_1N_PARALLEL" if use_parallel else "SOURCEAFIS_1N")
        if use_multi:
            steps_applied.append("MULTI_TEMPLATE")
        timings["step_match"] = time.perf_counter() - t0

        # â”€â”€ 4. Hallucination guards: gap-to-2nd, quality scaling â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        if not final_scores:
            best_person, best_score, second_score = None, 0.0, 0.0
        else:
            sorted_scores = sorted(final_scores.items(), key=lambda kv: kv[1], reverse=True)
            best_person, best_score = sorted_scores[0]
            second_score = sorted_scores[1][1] if len(sorted_scores) > 1 else 0.0

        # Quality-scaled threshold: low clarity â†’ much higher bar
        if config.get("MATCH_QUALITY_SCALE", True):
            penalty = float(config.get("MATCH_QUALITY_PENALTY", 25.0))
            # at clarity=1.0 â†’ 0 penalty; at clarity=floor â†’ full penalty
            normalized = max(0.0, min(1.0, (1.0 - clarity) / max(1e-6, 1.0 - clarity_floor)))
            effective_confirm = threshold_confirm + normalized * penalty
        else:
            effective_confirm = threshold_confirm

        gap_abs = float(config.get("MATCH_GAP_MIN_ABS", 12.0))
        gap_ratio = float(config.get("MATCH_GAP_MIN_RATIO", 1.35))
        threshold_warn = float(config.get("MATCH_THRESHOLD_WARN", 28))

        diff = best_score - second_score
        ratio = best_score / second_score if second_score > 1e-6 else float("inf")
        gap_ok = diff >= gap_abs and ratio >= gap_ratio

        # ── Decision logic ─────────────────────────────────────────────────
        # ADAPTIVE mode: pass on any of three tiers — absolute strong,
        # clear winner, or dominant leader. Catches genuine matches with
        # modest absolute scores when they clearly beat the runner-up.
        # Impostor scores cluster near each other so gap rules block them.
        if config.get("USE_ADAPTIVE_THRESHOLD", True):
            t1_strong  = float(config.get("MATCH_TIER1_STRONG", 50))
            t2_min     = float(config.get("MATCH_TIER2_MIN", 15))
            t2_ratio   = float(config.get("MATCH_TIER2_RATIO", 2.0))
            t2_gap     = float(config.get("MATCH_TIER2_GAP", 10))
            t3_min     = float(config.get("MATCH_TIER3_MIN", 12))
            t3_ratio   = float(config.get("MATCH_TIER3_RATIO", 3.0))

            tier_hit = None
            if best_score >= t1_strong:
                tier_hit = "T1"
            elif (best_score >= t2_min
                  and ratio >= t2_ratio
                  and diff >= t2_gap):
                tier_hit = "T2"
            elif (best_score >= t3_min
                  and (second_score < 1e-6 or ratio >= t3_ratio)):
                tier_hit = "T3"

            if tier_hit is not None:
                flag = "confirmed"
            elif best_score >= threshold_warn:
                flag = "low_confidence"
                best_person = None
            else:
                flag = "no_match"
                best_person = None
        else:
            # STRICT legacy mode (used when USE_ADAPTIVE_THRESHOLD=False).
            if best_score >= effective_confirm and gap_ok:
                flag = "confirmed"
                tier_hit = "STRICT"
            elif best_score >= threshold_warn:
                flag = "low_confidence"
                best_person = None
                tier_hit = None
            else:
                flag = "no_match"
                best_person = None
                tier_hit = None
            best_person = None

        timings["total"] = time.perf_counter() - t_start

        # Concise console summary for live debugging
        print(
            f"[identify] preprocess={timings.get('step_preprocess', 0):.3f}s "
            f"probe={timings.get('step_probe_template', 0):.3f}s "
            f"match={timings.get('step_match', 0):.3f}s "
            f"total={timings['total']:.3f}s "
            f"enrolled={len(enrolled)} clarity={clarity:.2f} "
            f"best={best_score:.1f} 2nd={second_score:.1f} gap={diff:.1f}/{ratio:.2f}x "
            f"effective_thresh={effective_confirm:.1f} flag={flag}"
            f"{(' tier=' + tier_hit) if tier_hit else ''}"
        )

        result = {
            "matched": flag == "confirmed",
            "person_id": best_person,
            "confidence_score": best_score,
            "second_score": second_score,
            "effective_threshold": effective_confirm,
            "clarity": clarity,
            "flag": flag,
            "all_scores": final_scores,
        }

        log_event("identify", {
            "step_timings": timings,
            "scores": final_scores,
            "flags": {
                "flag": flag, "clarity": clarity, "diff": diff, "ratio": ratio,
                "effective_threshold": effective_confirm,
            },
            "steps_applied": steps_applied,
        }, config)

        return result

    except Exception as exc:
        log_event("identify", {
            "step_timings": timings, "scores": {},
            "flags": {"error": str(exc)}, "steps_applied": steps_applied,
        }, config)
        return {
            "matched": False, "person_id": None, "confidence_score": 0.0,
            "flag": "no_match", "all_scores": {},
            "error": f"identify pipeline failed: {exc}",
        }


if __name__ == "__main__":
    import pickle
    import sys

    if len(sys.argv) < 2:
        print("Usage: python match.py <probe_image> [enrolled_template_pickle ...]")
        sys.exit(1)

    with open(sys.argv[1], "rb") as f:
        probe_bytes = f.read()

    from .minutiae import FingerprintTemplate

    enrolled_list: List[Tuple[str, object, list]] = []
    for path in sys.argv[2:]:
        with open(path, "rb") as f:
            data = pickle.load(f)
            person_id, comp, raws = data
            if isinstance(comp, bytes):
                comp = FingerprintTemplate(pickle.loads(comp))
            if raws and isinstance(raws[0], bytes):
                raws = [pickle.loads(b) for b in raws]
            enrolled_list.append((person_id, comp, raws))

    print(identify(probe_bytes, enrolled_list))
