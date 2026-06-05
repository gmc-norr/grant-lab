"""Vision-token estimation per Anthropic's published formula:
tokens = (width * height) / 750, with the longest side capped at 1568 px
(images are downscaled before the count). Source:
https://docs.anthropic.com/en/docs/build-with-claude/vision#calculate-image-costs
"""

import io

import pytest
from PIL import Image

from call_manager.proposal_assist.attachments.tokens import (
    estimate_image_tokens,
    estimate_image_tokens_from_bytes,
)


def _make_image_bytes(width: int, height: int, fmt: str = "PNG") -> bytes:
    img = Image.new("RGB", (width, height), color=(128, 128, 128))
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


def test_estimate_small_image():
    # 200 x 300 = 60_000 px / 750 = 80 tokens
    assert estimate_image_tokens(200, 300) == 80


def test_estimate_rounds_up():
    # 100 x 100 = 10_000 px / 750 = 13.33 -> 14
    assert estimate_image_tokens(100, 100) == 14


def test_estimate_caps_long_side_at_1568():
    # 3000 x 2000 -> scaled to 1568 x 1045 -> 1_638_960 / 750 = ~2186 tokens
    tokens = estimate_image_tokens(3000, 2000)
    # Expect ~2186; allow ±5 for integer rounding of the scale.
    assert 2180 <= tokens <= 2200


def test_estimate_from_bytes_png():
    data = _make_image_bytes(640, 480, fmt="PNG")
    tokens = estimate_image_tokens_from_bytes(data)
    # 640 * 480 = 307_200 / 750 = 410 (rounded up)
    assert tokens == 410


def test_estimate_from_bytes_rejects_non_image():
    with pytest.raises(ValueError):
        estimate_image_tokens_from_bytes(b"not an image")


def test_estimate_from_bytes_rejects_decompression_bomb(monkeypatch):
    """A crafted image declaring an enormous pixel count must be refused
    (Pillow raises DecompressionBombError) rather than allocating GBs."""
    from PIL import Image

    from call_manager.proposal_assist.attachments import tokens as tokens_mod

    # The module must set a finite MAX_IMAGE_PIXELS cap.
    assert Image.MAX_IMAGE_PIXELS is not None

    # Build an image whose pixel count exceeds the module's cap. Use a
    # temporarily-lowered cap so the test image stays tiny/fast.
    monkeypatch.setattr(tokens_mod.Image, "MAX_IMAGE_PIXELS", 10)
    data = _make_image_bytes(100, 100, fmt="PNG")  # 10_000 px > 10
    with pytest.raises(ValueError):
        estimate_image_tokens_from_bytes(data)
