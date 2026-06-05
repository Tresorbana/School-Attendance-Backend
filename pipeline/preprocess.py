import cv2
import numpy as np
from skimage.morphology import skeletonize as sk_skeletonize
from .pipeline_config import PIPELINE_CONFIG


def preprocess(image_bytes: bytes, config: dict = PIPELINE_CONFIG):
    """
    Preprocess raw fingerprint image bytes into a clean ridge image.
    Returns np.ndarray on success or dict with 'error' key on failure.
    """
    try:
        nparr = np.frombuffer(image_bytes, np.uint8)
        # Force grayscale decode: handles 8-bit indexed/palette PNGs from DPFP SDK
        # as well as standard grayscale/RGB images.
        img = cv2.imdecode(nparr, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return {"error": "Failed to decode image bytes"}

        # TOGGLE: USE_GRAYSCALE (image is already grayscale from IMREAD_GRAYSCALE)
        if config.get("USE_GRAYSCALE", True):
            pass  # already grayscale

        # Ensure uint8 grayscale at this point
        if img.dtype != np.uint8:
            img = np.clip(img, 0, 255).astype(np.uint8)

        # TOGGLE: USE_CLAHE
        if config.get("USE_CLAHE", True):
            clahe = cv2.createCLAHE(
                clipLimit=float(config.get("CLAHE_CLIP_LIMIT", 2.0)),
                tileGridSize=tuple(config.get("CLAHE_TILE_SIZE", (8, 8))),
            )
            img = clahe.apply(img)

        # TOGGLE: USE_GABOR
        if config.get("USE_GABOR", True):
            n_orient = int(config.get("GABOR_ORIENTATIONS", 8))
            responses = []
            for i in range(n_orient):
                theta = i * np.pi / n_orient  # 0 to pi (exclusive), evenly spaced
                kernel = cv2.getGaborKernel(
                    (21, 21), sigma=4.0, theta=theta,
                    lambd=10.0, gamma=0.5, psi=0.0, ktype=cv2.CV_32F,
                )
                resp = cv2.filter2D(img.astype(np.float32), cv2.CV_32F, kernel)
                responses.append(np.abs(resp))
            # Take per-pixel maximum response across all orientations
            gabor_max = np.max(np.stack(responses, axis=0), axis=0)
            # Rescale to uint8 preserving relative values
            g_min, g_max = gabor_max.min(), gabor_max.max()
            if g_max > g_min:
                img = ((gabor_max - g_min) / (g_max - g_min) * 255).astype(np.uint8)
            else:
                img = gabor_max.astype(np.uint8)

        # TOGGLE: USE_NORMALIZATION
        if config.get("USE_NORMALIZATION", True):
            img_f = img.astype(np.float64)
            mean = np.mean(img_f)
            std = np.std(img_f)
            if std > 0:
                img_f = (img_f - mean) / std
            i_min, i_max = img_f.min(), img_f.max()
            if i_max > i_min:
                img_f = (img_f - i_min) / (i_max - i_min) * 255.0
            img = np.clip(img_f, 0, 255).astype(np.uint8)

        # TOGGLE: USE_BINARIZE
        if config.get("USE_BINARIZE", True):
            img = cv2.adaptiveThreshold(
                img, 255,
                cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY,
                blockSize=11, C=2,
            )

        # TOGGLE: USE_SKELETONIZE
        if config.get("USE_SKELETONIZE", True):
            binary = img > 127
            skeleton = sk_skeletonize(binary)
            img = (skeleton * 255).astype(np.uint8)

        return img

    except Exception as exc:
        return {"error": f"preprocess failed: {exc}"}


def preprocess_for_template(image_bytes: bytes, config: dict = PIPELINE_CONFIG):
    """
    Ridge-enhanced grayscale for SourceAFIS-style minutiae extraction.
    Pipeline: decode → CLAHE → Gabor max-response (no binarize, no skeleton).
    The Gabor step boosts ridge contrast so FingerprintTemplate's internal
    threshold+skeletonize finds cleaner minutiae with fewer spur artifacts.
    Returns np.ndarray (uint8 grayscale, ridges bright on dark) or {'error': str}.
    """
    try:
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return {"error": "Failed to decode image bytes"}
        if img.dtype != np.uint8:
            img = np.clip(img, 0, 255).astype(np.uint8)

        if config.get("USE_CLAHE", True):
            clahe = cv2.createCLAHE(
                clipLimit=float(config.get("CLAHE_CLIP_LIMIT", 2.0)),
                tileGridSize=tuple(config.get("CLAHE_TILE_SIZE", (8, 8))),
            )
            img = clahe.apply(img)

        # Gabor multi-orientation max — boosts ridge contrast in any direction.
        # We DON'T binarize/skeletonize here — FingerprintTemplate does that
        # internally on the enhanced grayscale, producing a cleaner skeleton
        # than thresholding raw CLAHE.
        if config.get("USE_GABOR", True):
            n_orient = int(config.get("GABOR_ORIENTATIONS", 8))
            responses = []
            for i in range(n_orient):
                theta = i * np.pi / n_orient
                kernel = cv2.getGaborKernel(
                    (21, 21), sigma=4.0, theta=theta,
                    lambd=10.0, gamma=0.5, psi=0.0, ktype=cv2.CV_32F,
                )
                resp = cv2.filter2D(img.astype(np.float32), cv2.CV_32F, kernel)
                responses.append(np.abs(resp))
            gabor_max = np.max(np.stack(responses, axis=0), axis=0)
            g_min, g_max = gabor_max.min(), gabor_max.max()
            if g_max > g_min:
                img = ((gabor_max - g_min) / (g_max - g_min) * 255).astype(np.uint8)
            else:
                img = gabor_max.astype(np.uint8)

        return img
    except Exception as exc:
        return {"error": f"preprocess_for_template failed: {exc}"}


if __name__ == "__main__":
    import sys

    path = sys.argv[1] if len(sys.argv) > 1 else None
    if path is None:
        print("Usage: python preprocess.py <image_path>")
        sys.exit(1)
    with open(path, "rb") as f:
        data = f.read()
    result = preprocess(data)
    if isinstance(result, dict):
        print("ERROR:", result)
    else:
        print(f"OK â€” output shape: {result.shape}, dtype: {result.dtype}")
