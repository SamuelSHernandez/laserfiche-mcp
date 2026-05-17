"""Tests for ``tools/writes_create_copy_import.py`` — create_folder, copy_entry, import_document."""

from __future__ import annotations

from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from laserfiche_mcp import server
from laserfiche_mcp.client import LaserficheClient
from tests.conftest import _BASE

# --- happy paths -------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_folder_happy_path(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/100",
        json={"id": 100, "name": "Parent", "entryType": "Folder"},
    )
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/Entries/100/Laserfiche.Repository.Folder/children?autoRename=false",
        json={"id": 500, "name": "New", "entryType": "Folder"},
    )
    await server.create_folder(100, "New")


@pytest.mark.asyncio
async def test_copy_entry_happy_path(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/100",
        json={"id": 100, "name": "Parent", "entryType": "Folder"},
    )
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/Entries/100/Laserfiche.Repository.Folder/CopyAsync?autoRename=true",
        status_code=201,
        json={"token": "op-copy-1"},
    )
    result = await server.copy_entry(42, 100, "Copy", auto_rename=True)
    assert result["token"] == "op-copy-1"


@pytest.mark.asyncio
async def test_import_document_file_not_found(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/100",
        json={"id": 100, "name": "Parent", "entryType": "Folder"},
    )
    missing = tmp_path / "missing.txt"
    result = await server.import_document(100, "x.txt", str(missing))
    assert result["mode"] == "error"
    assert result["error"] == "file_not_found"


@pytest.mark.asyncio
async def test_import_document_size_cap(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
    tmp_path: Path,
) -> None:
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(settings, "import_max_bytes", 10)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/100",
        json={"id": 100, "name": "Parent", "entryType": "Folder"},
    )
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * 100)
    result = await server.import_document(100, "big.bin", str(big))
    assert result["mode"] == "error"
    assert result["error"] == "size_exceeds_cap"


@pytest.mark.asyncio
async def test_import_document_happy_path_with_metadata(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    f = tmp_path / "doc.txt"
    f.write_bytes(b"hello")
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/100",
        json={"id": 100, "name": "Parent", "entryType": "Folder"},
    )
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/Entries/100/doc.txt?autoRename=false",
        status_code=201,
        json={"entryCreate": {"entryId": 500}},
    )
    result = await server.import_document(
        100,
        "doc.txt",
        str(f),
        template_name="Doc",
        fields={"Note": ["hello"]},
        tags=["new"],
    )
    assert result.get("entryCreate", {}).get("entryId") == 500


# --- path-fence on parent ---------------------------------------------------


@pytest.mark.asyncio
async def test_create_folder_checks_parent_path(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """create_folder fences on parent's path since the new folder doesn't exist yet."""
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(settings, "write_paths_deny", "\\Production")
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/100",
        json={
            "id": 100,
            "name": "Production",
            "entryType": "Folder",
            "fullPath": "\\Production",
        },
    )
    result = await server.create_folder(100, "NewSub")
    assert result["mode"] == "error"
    assert result["error"] == "path_not_allowed"


# --- classify_lf_error wrap on upstream failures ----------------------------


@pytest.mark.asyncio
async def test_copy_entry_classifies_upstream_error(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/100",
        json={"id": 100, "name": "Dest", "entryType": "Folder", "fullPath": "\\Dest"},
    )
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/Entries/100/Laserfiche.Repository.Folder/CopyAsync?autoRename=false",
        status_code=500,
    )
    result = await server.copy_entry(42, 100, "NewName")
    assert result["mode"] == "error"
    assert result["error"] == "server_error"


@pytest.mark.asyncio
async def test_create_folder_classifies_upstream_error(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/100",
        json={"id": 100, "name": "Parent", "entryType": "Folder", "fullPath": "\\P"},
    )
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/Entries/100/Laserfiche.Repository.Folder/children?autoRename=false",
        status_code=500,
    )
    result = await server.create_folder(100, "NewFolder")
    assert result["mode"] == "error"
    assert result["error"] == "server_error"


@pytest.mark.asyncio
async def test_create_folder_with_template_validates_then_creates(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """create_folder with a template runs the template+field validators on the happy path."""
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(settings, "validate_names", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/100",
        json={"id": 100, "name": "Parent", "entryType": "Folder", "fullPath": "\\P"},
    )
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/Entries/100/Laserfiche.Repository.Folder/children?autoRename=false",
        json={"id": 999, "name": "NewFolder", "templateName": "Personnel"},
    )
    result = await server.create_folder(
        100,
        "NewFolder",
        template_name="Personnel",
        fields={"Name": ["v"]},
    )
    assert result["id"] == 999


@pytest.mark.asyncio
async def test_import_document_missing_file_returns_structured_error(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
) -> None:
    """import_document with a missing local file returns file_not_found before any HTTP call."""
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/100",
        json={"id": 100, "name": "Parent", "entryType": "Folder", "fullPath": "\\P"},
    )
    result = await server.import_document(100, "x.pdf", "/nonexistent/path/x.pdf")
    assert result["mode"] == "error"
    assert result["error"] == "file_not_found"


@pytest.mark.asyncio
async def test_import_document_size_exceeds_cap(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
    tmp_path: Path,
) -> None:
    """import_document refuses files larger than LF_IMPORT_MAX_BYTES."""
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    monkeypatch.setattr(settings, "import_max_bytes", 10)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/100",
        json={"id": 100, "name": "Parent", "entryType": "Folder", "fullPath": "\\P"},
    )
    big_file = tmp_path / "big.bin"
    big_file.write_bytes(b"x" * 100)
    result = await server.import_document(100, "big.bin", str(big_file))
    assert result["mode"] == "error"
    assert result["error"] == "size_exceeds_cap"
    assert result["byte_size"] == 100


@pytest.mark.asyncio
async def test_import_document_classifies_upstream_error(
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    patched_client: LaserficheClient,
    tmp_path: Path,
) -> None:
    """An upstream 500 from the import POST flows through classify_lf_error."""
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/100",
        json={"id": 100, "name": "Parent", "entryType": "Folder", "fullPath": "\\P"},
    )
    src = tmp_path / "doc.txt"
    src.write_bytes(b"hello")
    httpx_mock.add_response(
        method="POST",
        url=f"{_BASE}/Entries/100/doc.txt?autoRename=false",
        status_code=500,
    )
    result = await server.import_document(100, "doc.txt", str(src))
    assert result["mode"] == "error"
    assert result["error"] == "server_error"


# --- name validator rejections ----------------------------------------------


@pytest.mark.asyncio
async def test_create_folder_rejects_invalid_name(
    monkeypatch: pytest.MonkeyPatch,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    result = await server.create_folder(parent_id=100, name="bad/name")
    assert result["mode"] == "error"
    assert result["error"] == "invalid_name"
    assert result["parent_id"] == 100


@pytest.mark.asyncio
async def test_copy_entry_rejects_invalid_name(
    monkeypatch: pytest.MonkeyPatch,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    result = await server.copy_entry(source_id=42, parent_id=100, name="")
    assert result["mode"] == "error"
    assert result["error"] == "invalid_name"


@pytest.mark.asyncio
async def test_import_document_rejects_invalid_name(
    monkeypatch: pytest.MonkeyPatch,
    patched_client: LaserficheClient,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", False)
    result = await server.import_document(
        parent_id=100,
        name="bad/file.txt",
        file_path="x",
    )
    assert result["mode"] == "error"
    assert result["error"] == "invalid_name"
