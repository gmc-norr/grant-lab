"""Vision-token estimation for Claude image inputs.

Anthropic's published formula: tokens ≈ (width * height) / 750, with the
longest side downscaled to 1568 px before the count. We use this to pre-
populate ChatTurn.tokens_in at submit time so the daily-token cap reflects
queued vision cost (closes the residual gap from B6-residuals-2 where
text-only submits left the cap reactive).

Source: https://docs.anthropic.com/en/docs/build-with-claude/vision
"""

from __future__ import annotations

import io
import math

from PIL import Image, UnidentifiedImageError
from PIL.Image import DecompressionBombError

_MAX_LONG_SIDE = 1568
_TOKENS_PER_PIXEL_DENOMINATOR = 750

# Decompression-bomb guard: cap the pixel count Pillow will accept before
# it raises DecompressionBombError. Pillow's library default is ~178 Mpx;
# we set a finite, explicit cap here so a crafted image (tiny on disk,
# enormous in pixels) can't drive a multi-GB allocation when we probe
# dimensions. 64 Mpx (~8000x8000) is far larger than any legitimate
# attachment but still bounded.
Image.MAX_IMAGE_PIXELS = 64 * 1024 * 1024


def estimate_image_tokens(width: int, height: int) -> int:
    """Estimate input tokens for an image of the given pixel dimensions.

    If the longest side exceeds 1568 px, scale proportionally first
    (Anthropic downscales internally before counting tokens)."""
    if width <= 0 or height <= 0:
        raise ValueError(f"invalid dimensions: {width}x{height}")
    long_side = max(width, height)
    if long_side > _MAX_LONG_SIDE:
        scale = _MAX_LONG_SIDE / long_side
        width = int(width * scale)
        height = int(height * scale)
    return math.ceil((width * height) / _TOKENS_PER_PIXEL_DENOMINATOR)


def estimate_image_tokens_from_bytes(data: bytes) -> int:
    """Probe image dimensions and return the token estimate.

    Raises ValueError if the bytes are not a recognized image format."""
    try:
        with Image.open(io.BytesIO(data)) as img:
            # Pillow raises DecompressionBombError during open() for images
            # whose declared pixel count is well over MAX_IMAGE_PIXELS; we
            # also re-check explicitly below to catch the warning-band case.
            width, height = img.size
            if width * height > Image.MAX_IMAGE_PIXELS:
                raise DecompressionBombError(
                    f"image pixel count {width * height} exceeds cap {Image.MAX_IMAGE_PIXELS}"
                )
    except DecompressionBombError as e:
        raise ValueError(f"image too large (decompression-bomb guard): {e}") from e
    except UnidentifiedImageError as e:
        raise ValueError(f"unrecognized image data: {e}") from e
    return estimate_image_tokens(width, height)
