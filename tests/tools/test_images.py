"""Tests for ``tools/images.py`` — get_document_image, incl. the cost gate."""

from __future__ import annotations

import base64
import io
import json
from typing import Any

import pytest
from mcp.types import ImageContent, TextContent
from PIL import Image
from pytest_httpx import HTTPXMock

from laserfiche_mcp import server
from laserfiche_mcp.client import LaserficheClient
from laserfiche_mcp.ops import images as ops_images
from tests.conftest import _BASE

_EDOC = f"{_BASE}/Entries/42/Laserfiche.Repository.Document/edoc"


def _png(size: tuple[int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=(10, 120, 200)).save(buf, format="PNG")
    return buf.getvalue()


def _serve(httpx_mock: HTTPXMock, data: bytes, ctype: str = "image/png") -> None:
    httpx_mock.add_response(
        method="GET", url=_EDOC, content=data, headers={"content-type": ctype}, is_reusable=True
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/42",
        json={
            "id": 42,
            "name": "Sample.png",
            "entryType": "Document",
            "fullPath": "\\Pics\\Sample.png",
        },
        is_reusable=True,
    )


def _split(result: Any) -> tuple[ImageContent, dict[str, Any]]:
    assert isinstance(result, list)
    image, text = result
    assert isinstance(image, ImageContent)
    assert isinstance(text, TextContent)
    return image, json.loads(text.text)


@pytest.mark.asyncio
async def test_small_image_is_returned_with_metadata(
    httpx_mock: HTTPXMock, patched_client: LaserficheClient
) -> None:
    data = _png((200, 100))
    _serve(httpx_mock, data)

    image, meta = _split(await server.get_document_image(entry_id=42))

    assert base64.b64decode(image.data) == data
    assert image.mimeType == "image/png"
    assert meta["name"] == "Sample.png"
    assert meta["width"] == 200 and meta["height"] == 100
    assert meta["estimated_tokens"] == ops_images.estimate_tokens(200, 100)
    assert "untrusted" in meta["notice"].lower()


@pytest.mark.asyncio
async def test_expensive_image_returns_cost_warning_and_no_image(
    httpx_mock: HTTPXMock, patched_client: LaserficheClient
) -> None:
    _serve(httpx_mock, _png((1500, 1000)))

    result = await server.get_document_image(entry_id=42)

    assert isinstance(result, dict)
    assert result["mode"] == "cost_warning"
    assert result["estimated_tokens"] > result["warn_threshold_tokens"]
    assert "tell the user" in result["next_step"].lower()
    assert "acknowledge_cost=true" in result["next_step"]
    assert "data" not in result  # the image itself was not sent
    # v1 test server has no Text export, so OCR availability is unknown, not False.
    assert result["ocr_text_available"] is None


@pytest.mark.asyncio
async def test_acknowledged_cost_returns_the_image(
    httpx_mock: HTTPXMock, patched_client: LaserficheClient
) -> None:
    _serve(httpx_mock, _png((1500, 1000)))

    image, meta = _split(await server.get_document_image(entry_id=42, acknowledge_cost=True))

    assert image.mimeType == "image/png"
    assert meta["estimated_tokens"] > server._get_settings().image_warn_tokens


@pytest.mark.asyncio
async def test_threshold_zero_gates_every_image(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, patched_client: LaserficheClient
) -> None:
    monkeypatch.setattr(server._get_settings(), "image_warn_tokens", 0)
    _serve(httpx_mock, _png((50, 50)))

    result = await server.get_document_image(entry_id=42)

    assert isinstance(result, dict) and result["mode"] == "cost_warning"


@pytest.mark.asyncio
async def test_large_image_is_downscaled_and_estimate_reflects_final_size(
    httpx_mock: HTTPXMock, patched_client: LaserficheClient
) -> None:
    _serve(httpx_mock, _png((4000, 3000)))

    image, meta = _split(
        await server.get_document_image(entry_id=42, acknowledge_cost=True, max_edge=400)
    )

    assert max(meta["width"], meta["height"]) <= 400
    assert meta["downscaled_or_converted"] is True
    assert meta["original_width"] == 4000
    assert meta["estimated_tokens"] == ops_images.estimate_tokens(meta["width"], meta["height"])
    assert image.mimeType in ("image/png", "image/jpeg")


@pytest.mark.asyncio
@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
async def test_non_image_document_is_rejected(
    httpx_mock: HTTPXMock, patched_client: LaserficheClient
) -> None:
    _serve(httpx_mock, b"%PDF-1.7 definitely not a picture", ctype="application/pdf")

    result = await server.get_document_image(entry_id=42)

    assert result["mode"] == "error"
    assert result["error"] == "unsupported_image_format"
    assert result["kind"] == "invalid_input"
    assert result["operation"] == "get_document_image"


@pytest.mark.asyncio
async def test_entry_without_electronic_document(
    httpx_mock: HTTPXMock, patched_client: LaserficheClient
) -> None:
    httpx_mock.add_response(
        method="GET", url=_EDOC, content=b"", headers={"content-length": "0"}, is_reusable=True
    )

    result = await server.get_document_image(entry_id=42)

    assert result["mode"] == "error"
    assert result["operation"] == "get_document_image"
    assert result["error"] == "no_electronic_document"
    # Explains the page-image situation and the way out, not a generic scan hint.
    assert "page images" in result["message"]
    assert "v2" in result["hint"] and "import_document" in result["hint"]


@pytest.mark.asyncio
@pytest.mark.httpx_mock(assert_all_responses_were_requested=False)
async def test_image_over_download_cap_without_pillow_is_refused(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock, patched_client: LaserficheClient
) -> None:
    monkeypatch.setattr(ops_images, "pillow_available", lambda: False)
    monkeypatch.setattr(server._get_settings(), "image_max_bytes", 500)
    _serve(httpx_mock, _png((300, 300)))

    result = await server.get_document_image(entry_id=42)

    assert result["error"] == "image_too_large"
    assert "laserfiche-mcp[images]" in result["message"]


@pytest.mark.asyncio
async def test_not_found_is_classified(
    httpx_mock: HTTPXMock, patched_client: LaserficheClient
) -> None:
    httpx_mock.add_response(method="GET", url=_EDOC, status_code=404, json={"title": "nope"})

    result = await server.get_document_image(entry_id=42)

    assert result["mode"] == "error"
    assert result["error"] == "not_found"


@pytest.mark.asyncio
async def test_tool_is_registered_and_returns_image_content_through_fastmcp(
    httpx_mock: HTTPXMock, patched_client: LaserficheClient
) -> None:
    """End to end through FastMCP's own conversion: the model must receive a real
    image content block, not a stringified list."""
    _serve(httpx_mock, _png((120, 80)))

    blocks = await server.mcp.call_tool("laserfiche_document_get_image", {"entry_id": 42})

    # A plain list of content blocks: NOT a (blocks, structured) tuple. FastMCP infers
    # structured output from the return annotation differently on Python 3.10 vs
    # 3.11+, which on 3.10 sent the image twice; the tool pins structured_output=False.
    assert isinstance(blocks, list)
    kinds = [b.type for b in blocks]
    assert kinds == ["image", "text"]


async def test_image_tool_declares_no_output_schema_on_any_python_version() -> None:
    tools = {t.name: t for t in await server.mcp.list_tools()}
    assert tools["laserfiche_document_get_image"].outputSchema is None
