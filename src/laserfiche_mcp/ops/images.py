"""Image helpers: format/size sniffing, token estimate, optional downscaling.

Sniffing is stdlib-only (PNG, JPEG, GIF, WebP headers) so the base install can
size an image — and estimate what returning it to the model will cost —
without any imaging dependency. Downscaling and conversion of other formats
(BMP, TIFF, ...) need Pillow, an optional extra: ``pip install
'laserfiche-mcp[images]'``.
"""

from __future__ import annotations

import io
import math
import struct
from dataclasses import dataclass

# Claude's vision pipeline resizes anything whose long edge exceeds ~1568px (or
# ~1.15 megapixels) before tokenizing, and charges roughly width*height/750
# tokens. These constants mirror that so the estimate reflects what the model
# is actually billed for, not the stored resolution.
API_MAX_EDGE = 1568
API_MAX_PIXELS = 1_150_000
TOKEN_DIVISOR = 750

# Refuse to decode anything declaring more pixels than this (decompression-bomb
# guard; a 100-megapixel image is already far beyond any scanned page).
MAX_DECODE_PIXELS = 100_000_000

_SOF_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


class ImageError(Exception):
    """An image can't be prepared for the model. ``slug`` is the error subkind."""

    def __init__(self, slug: str, message: str) -> None:
        super().__init__(message)
        self.slug = slug
        self.message = message


@dataclass(frozen=True)
class ImageInfo:
    mime: str
    width: int
    height: int


def pillow_available() -> bool:
    try:
        import PIL  # noqa: F401, PLC0415
    except ImportError:
        return False
    return True


def sniff_image(data: bytes) -> ImageInfo | None:
    """Identify PNG/JPEG/GIF/WebP from magic bytes and read the pixel size.

    Returns ``None`` for anything else (or a truncated header). Never trusts
    the entry's extension or the server's Content-Type.
    """
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return ImageInfo("image/png", width, height)
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        width, height = struct.unpack("<HH", data[6:10])
        return ImageInfo("image/gif", width, height)
    if data[:2] == b"\xff\xd8":
        return _sniff_jpeg(data)
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return _sniff_webp(data)
    return None


def _sniff_jpeg(data: bytes) -> ImageInfo | None:
    pos = 2
    n = len(data)
    while pos + 4 <= n:
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        if marker == 0xFF:  # fill byte
            pos += 1
            continue
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:  # standalone markers
            pos += 2
            continue
        if marker in _SOF_MARKERS:
            if pos + 9 > n:
                return None
            height, width = struct.unpack(">HH", data[pos + 5 : pos + 9])
            return ImageInfo("image/jpeg", width, height)
        (length,) = struct.unpack(">H", data[pos + 2 : pos + 4])
        pos += 2 + max(length, 2)
    return None


def _sniff_webp(data: bytes) -> ImageInfo | None:
    kind = data[12:16]
    if kind == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
        width, height = struct.unpack("<HH", data[26:30])
        return ImageInfo("image/webp", width & 0x3FFF, height & 0x3FFF)
    if kind == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
        (bits,) = struct.unpack("<I", data[21:25])
        return ImageInfo("image/webp", (bits & 0x3FFF) + 1, ((bits >> 14) & 0x3FFF) + 1)
    if kind == b"VP8X" and len(data) >= 30:
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return ImageInfo("image/webp", width, height)
    return None


def estimate_tokens(width: int, height: int) -> int:
    """Approximate input tokens Claude charges for an image of this size."""
    if width <= 0 or height <= 0:
        return 0
    scale = min(1.0, API_MAX_EDGE / max(width, height))
    pixels = width * height * scale * scale
    if pixels > API_MAX_PIXELS:
        pixels = API_MAX_PIXELS
    return math.ceil(pixels / TOKEN_DIVISOR)


@dataclass(frozen=True)
class PreparedImage:
    data: bytes
    mime: str
    width: int
    height: int
    original_bytes: int
    original_width: int
    original_height: int
    converted: bool  # resized and/or re-encoded


def prepare_image(data: bytes, *, max_edge: int, max_bytes: int) -> PreparedImage:
    """Return bytes the model can consume: supported format, within the caps.

    Without Pillow the image is passed through untouched (and refused if it is
    over ``max_bytes`` or not PNG/JPEG/GIF/WebP). With Pillow it is downscaled
    to ``max_edge`` and re-encoded when it is too big, and BMP/TIFF/etc. are
    converted.
    """
    info = sniff_image(data)
    if info is not None and info.width * info.height > MAX_DECODE_PIXELS:
        raise ImageError(
            "image_too_large",
            f"Image is {info.width}x{info.height} pixels, over the "
            f"{MAX_DECODE_PIXELS}-pixel safety limit.",
        )

    needs_work = (
        info is None
        or len(data) > max_bytes
        or (info is not None and max(info.width, info.height) > max_edge)
    )
    if not needs_work:
        assert info is not None
        return PreparedImage(
            data, info.mime, info.width, info.height, len(data), info.width, info.height, False
        )

    if not pillow_available():
        if info is None:
            raise ImageError(
                "unsupported_image_format",
                "Not a PNG, JPEG, GIF or WebP file. Install the optional extra "
                "(pip install 'laserfiche-mcp[images]') to convert other formats.",
            )
        if len(data) > max_bytes:
            raise ImageError(
                "image_too_large",
                f"Image is {len(data)} bytes, over the {max_bytes}-byte limit. Install "
                "the optional extra (pip install 'laserfiche-mcp[images]') to downscale "
                "large images automatically, or raise LF_IMAGE_MAX_BYTES.",
            )
        # Long edge too big but bytes fine: the API downsizes it itself, so
        # pass it through — the token estimate already accounts for that.
        return PreparedImage(
            data, info.mime, info.width, info.height, len(data), info.width, info.height, False
        )

    return _convert_with_pillow(data, info, max_edge=max_edge, max_bytes=max_bytes)


def _convert_with_pillow(
    data: bytes, info: ImageInfo | None, *, max_edge: int, max_bytes: int
) -> PreparedImage:
    from PIL import Image, ImageOps, UnidentifiedImageError  # noqa: PLC0415

    Image.MAX_IMAGE_PIXELS = MAX_DECODE_PIXELS
    try:
        with Image.open(io.BytesIO(data)) as opened:
            opened.load()
            image = ImageOps.exif_transpose(opened) or opened
            original_w, original_h = (
                (info.width, info.height) if info else (image.width, image.height)
            )
            has_alpha = image.mode in ("RGBA", "LA") or "transparency" in image.info
            edge = max_edge
            for _ in range(6):
                work = image.copy()
                work.thumbnail((edge, edge), Image.Resampling.LANCZOS)
                out = io.BytesIO()
                if has_alpha:
                    work.convert("RGBA").save(out, format="PNG", optimize=True)
                    mime = "image/png"
                else:
                    work.convert("RGB").save(out, format="JPEG", quality=85, optimize=True)
                    mime = "image/jpeg"
                if out.tell() <= max_bytes:
                    return PreparedImage(
                        out.getvalue(),
                        mime,
                        work.width,
                        work.height,
                        len(data),
                        original_w,
                        original_h,
                        True,
                    )
                edge = int(edge * 0.75)
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ImageError(
            "unsupported_image_format", f"Could not decode this file as an image: {exc}"
        ) from exc
    raise ImageError(
        "image_too_large",
        f"Could not shrink the image under {max_bytes} bytes; raise LF_IMAGE_MAX_BYTES.",
    )
