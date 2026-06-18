"""Tests for the PNG/JPEG auto-downsample preflight in ``extract.py``.

Bedrock caps per-image base64 payload at 5 MB; raw images above ~3.75 MB
risk crossing it. The preflight in
``aegis.funders.extract._downsample_if_oversized`` resizes iteratively
until the encoded result fits the soft cap (4.5 MB). These tests pin
the size contract; the 2026-06-18 funder pass hit this when Logic
Advance's 6.5 MB guideline PNG crashed the extract silently.
"""

from __future__ import annotations

import io
import random

from PIL import Image

from aegis.funders.extract import (
    _BEDROCK_IMAGE_BYTES_SOFT_CAP,
    _downsample_if_oversized,
)


def _random_png(side: int, *, seed: int = 42) -> bytes:
    """Render a deterministic, high-entropy PNG that defeats deflate.

    Seeded ``random.randbytes`` produces pseudo-random RGB data — PNG's
    deflate compressor can't shrink it materially, so byte size scales
    with pixel count. ``seed`` keeps the fixture identical across runs
    (testing.md determinism contract). Not crypto — just reproducible
    noise for a size-contract test fixture.
    """
    rng = random.Random(seed)  # noqa: S311 — fixture determinism, not crypto
    rgb_data = rng.randbytes(side * side * 3)
    image = Image.frombytes("RGB", (side, side), rgb_data)
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=False)
    return buf.getvalue()


def _random_jpeg(side: int, *, seed: int = 42) -> bytes:
    rng = random.Random(seed)  # noqa: S311 — fixture determinism, not crypto
    rgb_data = rng.randbytes(side * side * 3)
    image = Image.frombytes("RGB", (side, side), rgb_data)
    buf = io.BytesIO()
    image.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def test_under_soft_cap_passes_through_unchanged() -> None:
    """Inputs already small enough are returned identity-equal."""
    small = b"\x89PNG\r\n\x1a\n" + b"\x00" * 1024
    out = _downsample_if_oversized(small)
    assert out is small


def test_oversized_png_shrinks_below_soft_cap() -> None:
    """The headline contract: any image above the cap comes back below it."""
    # 2000x2000 random RGB is ~12 MB raw and PNG can't compress it. Two
    # 0.7x passes drop it to ~3 MB — well under the cap.
    oversize = _random_png(2000)
    assert len(oversize) > _BEDROCK_IMAGE_BYTES_SOFT_CAP, (
        f"fixture {len(oversize)} bytes must exceed cap {_BEDROCK_IMAGE_BYTES_SOFT_CAP}"
    )
    out = _downsample_if_oversized(oversize)
    assert len(out) <= _BEDROCK_IMAGE_BYTES_SOFT_CAP, (
        f"downsample left {len(out)} bytes (cap {_BEDROCK_IMAGE_BYTES_SOFT_CAP})"
    )


def test_downsampled_output_remains_valid_png() -> None:
    """Pillow can re-open the downsampled bytes — no silent corruption."""
    oversize = _random_png(2000)
    out = _downsample_if_oversized(oversize)
    reopened = Image.open(io.BytesIO(out))
    reopened.load()
    assert reopened.format == "PNG"
    assert reopened.size[0] < 2000
    assert reopened.size[1] < 2000


def test_downsample_preserves_jpeg_format() -> None:
    """JPEG input → JPEG output (PNG would inflate the bytes again)."""
    oversize = _random_jpeg(2400)
    assert len(oversize) > _BEDROCK_IMAGE_BYTES_SOFT_CAP, (
        f"jpeg fixture {len(oversize)} bytes must exceed cap {_BEDROCK_IMAGE_BYTES_SOFT_CAP}"
    )
    out = _downsample_if_oversized(oversize)
    reopened = Image.open(io.BytesIO(out))
    assert reopened.format == "JPEG"
    assert len(out) <= _BEDROCK_IMAGE_BYTES_SOFT_CAP
