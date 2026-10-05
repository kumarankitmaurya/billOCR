"""Measure how straight a bill photo is, and say so. Do not rewrite it.

A tilted page is how a rate from one row ends up on another: the model follows
what looks like a horizontal line, which on a tilted page is partly the row
above. One such error was found in this shop's own book — ANIKA stored at
1792, the rate belonging to the row above it.

This module deliberately does NOT correct the image, because correcting it was
measured and made things worse. Deskewing a bill tilted 6 degrees and sending
the result returned 5 of 8 rows where the untouched tilted photo returned all
8; so did every other preprocessing variant tried. Flattening the lighting was
the main culprit — it pushed 35.6% of the page to near-white against 1.5% in
the original, erasing the faintest rows. Losing three rows silently is a worse
outcome than one misread rate, which at least the amount check can catch.

What is kept is the measurement, which was validated against known rotations
and recovers them to within 0.1 degrees. A tilted photo can be flagged for the
shopkeeper to retake, which fixes the cause instead of fighting the symptom.
"""

import io
import logging

import numpy as np
from PIL import Image, ImageFilter, ImageOps

logger = logging.getLogger(__name__)

# Only small angles are searched. A page photographed on a counter is off by a
# few degrees; beyond this it is a sideways photo, which is a different problem.
MAX_SKEW_DEGREES = 12.0
_COARSE_STEP = 1.0
_FINE_STEP = 0.2
# Below this, tilt is not worth mentioning — the model reads it fine.
SKEW_WORTH_FLAGGING = 2.0
# The search runs on a small copy; the angle applies to the full-size image.
_ANALYSIS_MAX_SIDE = 1000


def _ink(image: Image.Image) -> np.ndarray:
    """A small, binary view of the image where 1 is ink.

    Thresholded against each pixel's own neighbourhood rather than a global
    value, so a page bright on one side and shadowed on the other still shows
    text on both. Used only for measurement — this never reaches the model.
    """
    grey = ImageOps.grayscale(image)
    grey.thumbnail((_ANALYSIS_MAX_SIDE, _ANALYSIS_MAX_SIDE))
    background = grey.filter(ImageFilter.GaussianBlur(radius=max(2, grey.width // 50)))
    flattened = np.asarray(grey, dtype=np.float32) - np.asarray(background, dtype=np.float32)
    return (flattened < -12).astype(np.float32)


def _row_sharpness(ink: np.ndarray) -> float:
    """How strongly the image separates into text rows.

    Summing ink along each row peaks at text and dips in the gaps between
    rows. The variance of that profile is highest when the rows are level,
    which is the whole basis of the search below.
    """
    return float(np.var(np.diff(ink.sum(axis=1))))


def find_skew(image: Image.Image) -> float:
    """Degrees the page is rotated by. Coarse sweep, then fine around the best.

    A full fine sweep costs 120 rotations for the same answer.
    """
    ink = Image.fromarray((_ink(image) * 255).astype(np.uint8))

    def score(angle: float) -> float:
        rotated = ink if angle == 0 else ink.rotate(angle, resample=Image.BILINEAR, fillcolor=0)
        return _row_sharpness(np.asarray(rotated, dtype=np.float32) / 255.0)

    coarse = np.arange(-MAX_SKEW_DEGREES, MAX_SKEW_DEGREES + _COARSE_STEP, _COARSE_STEP)
    best = max(coarse, key=score)
    fine = np.arange(best - _COARSE_STEP, best + _COARSE_STEP + _FINE_STEP, _FINE_STEP)
    return float(max(fine, key=score))


def skew_flags(image_bytes: bytes) -> list[str]:
    """Warn when a photo is tilted enough to risk a misread row.

    Never raises: a photo this cannot measure produces no warning rather than
    a failed scan.
    """
    try:
        image = ImageOps.exif_transpose(Image.open(io.BytesIO(image_bytes))).convert("RGB")
        angle = find_skew(image)
    except Exception:
        logger.exception("could not measure skew")
        return []

    logger.info("skew measured at %+.1f degrees", angle)
    if abs(angle) < SKEW_WORTH_FLAGGING:
        return []
    return [
        f"the photo is tilted about {abs(angle):.0f}° — a tilted page is the usual "
        "cause of a price landing on the wrong row, so check the lines below, and "
        "retake the photo square to the page if anything looks off"
    ]
