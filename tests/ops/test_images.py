"""Tests for ``ops/images.py`` — sniffing, token estimate, downscaling."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from laserfiche_mcp.ops import images as ops_images


def _encode(fmt: str, size: tuple[int, int] = (40, 20), mode: str = "RGB", **kw: object) -> bytes:
    buf = io.BytesIO()
    Image.new(mode, size, color=(200, 30, 30) if mode == "RGB" else (200, 30, 30, 128)).save(
        buf, format=fmt, **kw
    )
    return buf.getvalue()


@pytest.mark.parametrize(
    ("fmt", "mime"),
    [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("GIF", "image/gif"), ("WEBP", "image/webp")],
)
def test_sniff_reads_format_and_size(fmt: str, mime: str) -> None:
    info = ops_images.sniff_image(_encode(fmt, (40, 20)))
    assert info is not None
    assert (info.mime, info.width, info.height) == (mime, 40, 20)


def test_sniff_webp_lossless_and_extended() -> None:
    lossless = ops_images.sniff_image(_encode("WEBP", (33, 17), lossless=True))
    assert lossless is not None
    assert (lossless.width, lossless.height) == (33, 17)
    extended = ops_images.sniff_image(_encode("WEBP", (50, 30), mode="RGBA"))
    assert extended is not None
    assert (extended.width, extended.height) == (50, 30)


def test_sniff_rejects_non_images_and_truncation() -> None:
    assert ops_images.sniff_image(b"hello world, not an image") is None
    assert ops_images.sniff_image(b"") is None
    assert ops_images.sniff_image(_encode("PNG")[:10]) is None
    assert ops_images.sniff_image(_encode("JPEG")[:12]) is None


def test_sniff_bmp_is_not_natively_supported() -> None:
    assert ops_images.sniff_image(_encode("BMP")) is None


def test_estimate_tokens() -> None:
    assert ops_images.estimate_tokens(1000, 1000) == 1334
    assert ops_images.estimate_tokens(0, 100) == 0
    # Huge images are resized by the API before billing, so cost plateaus.
    assert ops_images.estimate_tokens(8000, 6000) == ops_images.estimate_tokens(4000, 3000)
    assert ops_images.estimate_tokens(8000, 6000) <= 1600
    # Small images are cheap.
    assert ops_images.estimate_tokens(100, 100) < 20


def test_prepare_passes_small_supported_image_through_untouched() -> None:
    data = _encode("PNG", (64, 64))
    out = ops_images.prepare_image(data, max_edge=1568, max_bytes=5_000_000)
    assert out.data == data
    assert out.converted is False
    assert (out.width, out.height) == (64, 64)


def test_prepare_downscales_large_image_with_pillow() -> None:
    data = _encode("JPEG", (3000, 2000))
    out = ops_images.prepare_image(data, max_edge=800, max_bytes=5_000_000)
    assert out.converted is True
    assert max(out.width, out.height) <= 800
    assert (out.original_width, out.original_height) == (3000, 2000)
    decoded = Image.open(io.BytesIO(out.data))
    assert decoded.size == (out.width, out.height)


def test_prepare_converts_bmp_with_pillow() -> None:
    out = ops_images.prepare_image(_encode("BMP", (30, 30)), max_edge=1568, max_bytes=5_000_000)
    assert out.mime in ("image/jpeg", "image/png")
    assert out.converted is True


def test_prepare_keeps_alpha_as_png() -> None:
    data = _encode("TIFF", (30, 30), mode="RGBA")
    out = ops_images.prepare_image(data, max_edge=1568, max_bytes=5_000_000)
    assert out.mime == "image/png"


def test_prepare_shrinks_to_fit_byte_cap() -> None:
    noisy = io.BytesIO()
    Image.effect_noise((1500, 1500), 80).convert("RGB").save(noisy, format="PNG")
    data = noisy.getvalue()
    cap = len(data) // 6
    out = ops_images.prepare_image(data, max_edge=1568, max_bytes=cap)
    assert len(out.data) <= cap


def test_prepare_rejects_garbage_with_pillow() -> None:
    with pytest.raises(ops_images.ImageError) as info:
        ops_images.prepare_image(b"definitely not an image", max_edge=1568, max_bytes=5_000_000)
    assert info.value.slug == "unsupported_image_format"


def test_prepare_decompression_bomb_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ops_images, "MAX_DECODE_PIXELS", 100)
    with pytest.raises(ops_images.ImageError) as info:
        ops_images.prepare_image(_encode("PNG", (40, 20)), max_edge=1568, max_bytes=5_000_000)
    assert info.value.slug == "image_too_large"


# --- without Pillow ----------------------------------------------------------


@pytest.fixture
def no_pillow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ops_images, "pillow_available", lambda: False)


def test_no_pillow_small_supported_image_still_works(no_pillow: None) -> None:
    data = _encode("PNG")
    assert ops_images.prepare_image(data, max_edge=1568, max_bytes=5_000_000).data == data


def test_no_pillow_refuses_other_formats_with_install_hint(no_pillow: None) -> None:
    with pytest.raises(ops_images.ImageError) as info:
        ops_images.prepare_image(_encode("BMP"), max_edge=1568, max_bytes=5_000_000)
    assert info.value.slug == "unsupported_image_format"
    assert "laserfiche-mcp[images]" in info.value.message


def test_no_pillow_refuses_oversized_bytes(no_pillow: None) -> None:
    with pytest.raises(ops_images.ImageError) as info:
        ops_images.prepare_image(_encode("PNG", (200, 200)), max_edge=1568, max_bytes=50)
    assert info.value.slug == "image_too_large"


def test_no_pillow_wide_but_light_image_passes_through(no_pillow: None) -> None:
    data = _encode("PNG", (3000, 10))
    out = ops_images.prepare_image(data, max_edge=1568, max_bytes=5_000_000)
    assert out.data == data
    assert out.converted is False
