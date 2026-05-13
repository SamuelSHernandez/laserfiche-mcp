"""Regenerate the binary PDF fixtures used by the text-extraction tests.

Run from the repo root:

    uv run python tests/fixtures/_generate.py

This is committed (not executed at test time) so reviewers can verify how
the fixtures were produced. The fixtures themselves are also committed —
running this script should be a no-op against ``git diff`` unless one of
the inputs below changes.
"""

from __future__ import annotations

import io
from pathlib import Path

import pypdf

FIXTURE_DIR = Path(__file__).parent

# Text intentionally short and ASCII-only so it round-trips through
# pypdf's content-stream parser without surprises.
SAMPLE_TEXT = "Hello laserfiche-mcp test fixture."


def _escape_pdf_string(value: str) -> bytes:
    """Escape characters that have special meaning inside a PDF literal string."""
    escaped = (
        value.replace("\\", r"\\")
        .replace("(", r"\(")
        .replace(")", r"\)")
    )
    return escaped.encode("ascii")


def build_text_pdf(text: str) -> bytes:
    """Build a single-page PDF whose content stream renders ``text``.

    Hand-rolled rather than written through reportlab/fpdf to avoid pulling
    a heavy dev-only dependency. Uses only the PDF features pypdf's text
    extractor reliably recovers: Type 1 font, BT/ET text object, ``Tj``.
    """
    content_body = (
        b"BT\n/F1 18 Tf\n72 720 Td\n("
        + _escape_pdf_string(text)
        + b") Tj\nET\n"
    )

    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Count 1 /Kids [3 0 R] >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"
        ),
        (
            b"<< /Length " + str(len(content_body)).encode("ascii") + b" >>\n"
            b"stream\n" + content_body + b"endstream"
        ),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for idx, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{idx} 0 obj\n".encode("ascii") + body + b"\nendobj\n"

    xref_offset = len(out)
    out += b"xref\n"
    out += f"0 {len(objects) + 1}\n".encode("ascii")
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode("ascii")

    out += b"trailer\n"
    out += f"<< /Size {len(objects) + 1} /Root 1 0 R >>\n".encode("ascii")
    out += b"startxref\n"
    out += f"{xref_offset}\n".encode("ascii")
    out += b"%%EOF\n"

    return bytes(out)


def encrypt_pdf(source: bytes, *, user_password: str) -> bytes:
    """Re-encode ``source`` through pypdf with a user password set.

    pypdf's writer rewrites the PDF so the encryption envelope is whatever
    the installed pypdf version emits — keeping this generator in sync with
    the runtime dependency means encrypted-PDF detection in our tests
    tracks pypdf's behavior automatically.
    """
    reader = pypdf.PdfReader(io.BytesIO(source))
    writer = pypdf.PdfWriter(clone_from=reader)
    writer.encrypt(user_password=user_password)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def main() -> None:
    text_pdf = build_text_pdf(SAMPLE_TEXT)
    (FIXTURE_DIR / "sample_text.pdf").write_bytes(text_pdf)

    encrypted = encrypt_pdf(text_pdf, user_password="fixture-password")
    (FIXTURE_DIR / "sample_encrypted.pdf").write_bytes(encrypted)

    # Sanity-check round-trip so a broken generator can't ship a broken fixture.
    reader = pypdf.PdfReader(io.BytesIO(text_pdf))
    extracted = "".join(p.extract_text() or "" for p in reader.pages)
    if SAMPLE_TEXT not in extracted:
        raise SystemExit(
            "Generated sample_text.pdf did not round-trip through pypdf "
            f"extraction. Got: {extracted!r}"
        )

    encrypted_reader = pypdf.PdfReader(io.BytesIO(encrypted))
    if not encrypted_reader.is_encrypted:
        raise SystemExit("Generated sample_encrypted.pdf is not actually encrypted.")

    print(
        f"Wrote {FIXTURE_DIR / 'sample_text.pdf'} "
        f"({len(text_pdf)} bytes) and "
        f"{FIXTURE_DIR / 'sample_encrypted.pdf'} ({len(encrypted)} bytes)."
    )


if __name__ == "__main__":
    main()
