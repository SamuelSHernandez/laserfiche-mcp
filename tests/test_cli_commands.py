"""End-to-end tests for the ``laserfiche-mcp`` subcommands.

These drive the real command functions against mocked HTTP, so they cover the
whole path a user gets: argument parsing, the ops call, and what lands on
stdout. Output shape is part of the contract here — people pipe these
commands into other tools.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from laserfiche_mcp import cli, cli_commands
from laserfiche_mcp.client import LaserficheClient
from laserfiche_mcp.config import Settings
from tests.client.conftest import _build_client
from tests.conftest import _BASE, SAMPLE_PDF_BYTES, SAMPLE_PDF_TEXT

_EDOC = f"{_BASE}/Entries/42/Laserfiche.Repository.Document/edoc"


@pytest.fixture
def args_for():
    """Parse a real argv, so tests exercise the actual parser defaults."""

    def build(argv: list[str]):
        return cli._parse_args(argv)

    return build


@pytest.fixture
async def client(lf_env: dict[str, str]):
    settings = Settings()  # type: ignore[call-arg]
    async with _build_client(settings) as opened:
        yield opened


def _entry(entry_id: int = 42, name: str = "lease.pdf", **extra):
    return {"id": entry_id, "name": name, "entryType": "Document", **extra}


# --- resolve_entry_id -------------------------------------------------------


@pytest.mark.asyncio
async def test_numeric_reference_needs_no_lookup(
    httpx_mock: HTTPXMock, client: LaserficheClient
) -> None:
    assert await cli_commands.resolve_entry_id(client, "42") == 42
    assert httpx_mock.get_requests() == []


@pytest.mark.asyncio
async def test_path_reference_resolves_via_bypath(
    httpx_mock: HTTPXMock, client: LaserficheClient
) -> None:
    """Paths are how people actually refer to documents."""
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/ByPath?fullPath=%5CHR%5CLeases",
        json={"id": 99, "name": "Leases", "entryType": "Folder"},
    )

    assert await cli_commands.resolve_entry_id(client, "\\HR\\Leases") == 99


# --- ls ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ls_prints_id_and_name_per_row(
    httpx_mock: HTTPXMock, client: LaserficheClient, args_for, capsys
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/1/Laserfiche.Repository.Folder/children?%24top=100&%24skip=0&%24count=true",
        json={"value": [_entry(7, "a.pdf"), {"id": 8, "name": "HR", "entryType": "Folder"}]},
    )

    code = await cli_commands.cmd_ls(client, args_for(["ls", "1"]))

    out = capsys.readouterr().out
    assert code == 0
    assert "7  a.pdf" in out
    assert "d " in out  # folders are marked


@pytest.mark.asyncio
async def test_ls_json_is_parseable(
    httpx_mock: HTTPXMock, client: LaserficheClient, args_for, capsys
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/1/Laserfiche.Repository.Folder/children?%24top=100&%24skip=0&%24count=true",
        json={"value": [_entry(7, "a.pdf")], "@odata.count": 1},
    )

    await cli_commands.cmd_ls(client, args_for(["ls", "1", "--json"]))

    payload = json.loads(capsys.readouterr().out)
    assert payload["entries"][0]["id"] == 7
    assert payload["total_count"] == 1


# --- get --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_writes_the_file_and_reports_its_hash(
    httpx_mock: HTTPXMock, client: LaserficheClient, args_for, capsys, tmp_path: Path
) -> None:
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/42", json=_entry())
    httpx_mock.add_response(method="GET", url=_EDOC, content=b"PDF BYTES")
    dest = tmp_path / "saved.pdf"

    code = await cli_commands.cmd_get(client, args_for(["get", "42", "--to", str(dest)]))

    assert code == 0
    assert dest.read_bytes() == b"PDF BYTES"
    assert "sha256" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_get_refuses_to_overwrite_without_force(
    httpx_mock: HTTPXMock, client: LaserficheClient, args_for, tmp_path: Path
) -> None:
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/42", json=_entry())
    dest = tmp_path / "existing.pdf"
    dest.write_bytes(b"do not clobber")

    code = await cli_commands.cmd_get(client, args_for(["get", "42", "--to", str(dest)]))

    assert code == cli_commands.EXIT_USAGE
    assert dest.read_bytes() == b"do not clobber"


@pytest.mark.asyncio
async def test_get_overwrites_with_force(
    httpx_mock: HTTPXMock, client: LaserficheClient, args_for, tmp_path: Path
) -> None:
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/42", json=_entry())
    httpx_mock.add_response(method="GET", url=_EDOC, content=b"new bytes")
    dest = tmp_path / "existing.pdf"
    dest.write_bytes(b"old")

    code = await cli_commands.cmd_get(client, args_for(["get", "42", "--to", str(dest), "--force"]))

    assert code == 0
    assert dest.read_bytes() == b"new bytes"


@pytest.mark.asyncio
async def test_get_into_a_directory_uses_the_entry_name(
    httpx_mock: HTTPXMock, client: LaserficheClient, args_for, tmp_path: Path
) -> None:
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/42", json=_entry())
    httpx_mock.add_response(method="GET", url=_EDOC, content=b"bytes")

    await cli_commands.cmd_get(client, args_for(["get", "42", "--to", str(tmp_path)]))

    assert (tmp_path / "lease.pdf").exists()


# --- cat --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cat_prints_extracted_text(
    httpx_mock: HTTPXMock, client: LaserficheClient, args_for, capsys
) -> None:
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/42", json=_entry())
    httpx_mock.add_response(method="GET", url=_EDOC, content=SAMPLE_PDF_BYTES)

    code = await cli_commands.cmd_cat(client, args_for(["cat", "42"]))

    assert code == 0
    assert SAMPLE_PDF_TEXT in capsys.readouterr().out


@pytest.mark.asyncio
async def test_cat_reports_an_unsupported_format_without_a_traceback(
    httpx_mock: HTTPXMock, client: LaserficheClient, args_for, capsys
) -> None:
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/42", json=_entry(name="scan.tiff"))
    httpx_mock.add_response(method="GET", url=_EDOC, content=b"II*\x00")

    code = await cli_commands.cmd_cat(client, args_for(["cat", "42"]))

    assert code == cli_commands.EXIT_FAILED
    assert "unsupported_format" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_cat_rejects_pages_on_an_unpaginated_document(
    httpx_mock: HTTPXMock, client: LaserficheClient, args_for, capsys
) -> None:
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/42", json=_entry(name="notes.txt"))
    httpx_mock.add_response(method="GET", url=_EDOC, content=b"plain text")

    code = await cli_commands.cmd_cat(client, args_for(["cat", "42", "--pages", "2"]))

    assert code == cli_commands.EXIT_USAGE
    assert "paginated" in capsys.readouterr().err


# --- find -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_prints_matches_with_location(
    httpx_mock: HTTPXMock, client: LaserficheClient, args_for, capsys
) -> None:
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/42", json=_entry())
    httpx_mock.add_response(method="GET", url=_EDOC, content=SAMPLE_PDF_BYTES)

    word = SAMPLE_PDF_TEXT.split()[0]
    code = await cli_commands.cmd_find(client, args_for(["find", "42", word]))

    out = capsys.readouterr().out
    assert code == 0
    assert word.lower() in out.lower()
    assert "match" in out


@pytest.mark.asyncio
async def test_find_with_no_match_exits_nonzero(
    httpx_mock: HTTPXMock, client: LaserficheClient, args_for, capsys
) -> None:
    """Exit status is what makes this usable in a shell conditional."""
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/42", json=_entry())
    httpx_mock.add_response(method="GET", url=_EDOC, content=SAMPLE_PDF_BYTES)

    code = await cli_commands.cmd_find(
        client, args_for(["find", "42", "definitely-not-present-anywhere"])
    )

    assert code == cli_commands.EXIT_FAILED
    assert "No match" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_find_rejects_an_invalid_regex_as_usage(
    httpx_mock: HTTPXMock, client: LaserficheClient, args_for, capsys
) -> None:
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/42", json=_entry())
    httpx_mock.add_response(method="GET", url=_EDOC, content=SAMPLE_PDF_BYTES)

    code = await cli_commands.cmd_find(client, args_for(["find", "42", "(unclosed", "--regex"]))

    assert code == cli_commands.EXIT_USAGE
    assert "invalid regex" in capsys.readouterr().err


# --- diff -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_diff_reports_differing_attributes(
    httpx_mock: HTTPXMock, client: LaserficheClient, args_for, capsys
) -> None:
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/1", json=_entry(1, "a.pdf"))
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/2", json=_entry(2, "b.pdf"))

    code = await cli_commands.cmd_diff(client, args_for(["diff", "1", "2", "--no-fields"]))

    out = capsys.readouterr().out
    assert code == cli_commands.EXIT_FAILED  # differences found
    assert "a.pdf" in out and "b.pdf" in out


@pytest.mark.asyncio
async def test_diff_of_identical_entries_exits_zero(
    httpx_mock: HTTPXMock, client: LaserficheClient, args_for, capsys
) -> None:
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/1", json=_entry(1, "same.pdf"))
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/2", json=_entry(2, "same.pdf"))

    code = await cli_commands.cmd_diff(client, args_for(["diff", "1", "2", "--no-fields"]))

    assert code == 0
    assert "match on every compared attribute" in capsys.readouterr().out


# --- manifest ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_manifest_writes_csv_and_summarizes(
    httpx_mock: HTTPXMock, client: LaserficheClient, args_for, capsys, tmp_path: Path
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/1/Laserfiche.Repository.Folder/children?%24top=100&%24skip=0",
        json={"value": [_entry(7, "a.pdf", extension="pdf")]},
    )
    dest = tmp_path / "inventory.csv"

    code = await cli_commands.cmd_manifest(
        client, args_for(["manifest", "1", "--out", str(dest), "--no-recursive"])
    )

    assert code == 0
    assert "a.pdf" in dest.read_text(encoding="utf-8-sig")
    assert "Entries      1" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_manifest_without_out_says_nothing_was_written(
    httpx_mock: HTTPXMock, client: LaserficheClient, args_for, capsys
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/1/Laserfiche.Repository.Folder/children?%24top=100&%24skip=0",
        json={"value": []},
    )

    await cli_commands.cmd_manifest(client, args_for(["manifest", "1", "--no-recursive"]))

    assert "nothing was written" in capsys.readouterr().err


# --- shared helpers ---------------------------------------------------------


def test_human_bytes_scales_units() -> None:
    assert cli_commands.human_bytes(512) == "512 B"
    assert cli_commands.human_bytes(2048) == "2.0 KB"
    assert cli_commands.human_bytes(None) == "?"


def test_emit_json_serializes_dataclasses(capsys) -> None:
    from laserfiche_mcp.ops.manifest import ManifestRow

    cli_commands.emit_json(
        {"row": ManifestRow(entry_id=1, name="x", entry_type="Document", parent_id=None, depth=1)}
    )

    assert json.loads(capsys.readouterr().out)["row"]["entry_id"] == 1


def test_warn_writes_to_stderr_so_stdout_stays_pipeable(capsys) -> None:
    cli_commands.warn("progress")

    captured = capsys.readouterr()
    assert captured.err.strip() == "progress"
    assert captured.out == ""


# --- search -----------------------------------------------------------------

_TOKEN = "srch-cli"


def _mock_search(httpx_mock: HTTPXMock, results: list[dict], hits: dict | None = None) -> None:
    httpx_mock.add_response(
        method="POST", url=f"{_BASE}/Searches", status_code=202, json={"token": _TOKEN}
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Searches/{_TOKEN}",
        json={"status": "Completed", "percentComplete": 100, "errors": []},
    )
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Searches/{_TOKEN}/Results", json={"value": results}
    )
    if hits is not None:
        httpx_mock.add_response(
            method="GET",
            url=f"{_BASE}/Searches/{_TOKEN}/Results/1/ContextHits",
            json=hits,
        )
    httpx_mock.add_response(method="DELETE", url=f"{_BASE}/Searches/{_TOKEN}")


@pytest.mark.asyncio
async def test_search_prints_entries_with_their_passages(
    httpx_mock: HTTPXMock, client: LaserficheClient, args_for, capsys
) -> None:
    _mock_search(
        httpx_mock,
        [{"id": 7, "name": "lease.pdf", "entryType": "Document", "rowNumber": 1}],
        hits={
            "value": [
                {
                    "pageNumber": 4,
                    "hitType": "PageContent",
                    "context": "tenant owes an unpaid balance of $2,400",
                    "highlight1Offset": 15,
                    "highlight1Length": 14,
                }
            ]
        },
    )

    code = await cli_commands.cmd_search(client, args_for(["search", "unpaid balance"]))

    out = capsys.readouterr().out
    assert code == 0
    assert "lease.pdf" in out
    assert "p4" in out
    assert "unpaid balance" in out


@pytest.mark.asyncio
async def test_search_releases_the_token_even_from_the_cli(
    httpx_mock: HTTPXMock, client: LaserficheClient, args_for
) -> None:
    """The two-active-searches cap applies to CLI runs too."""
    _mock_search(httpx_mock, [])

    await cli_commands.cmd_search(client, args_for(["search", "x"]))

    assert any(r.method == "DELETE" for r in httpx_mock.get_requests())


@pytest.mark.asyncio
async def test_search_with_no_results_exits_nonzero(
    httpx_mock: HTTPXMock, client: LaserficheClient, args_for, capsys
) -> None:
    _mock_search(httpx_mock, [])

    code = await cli_commands.cmd_search(client, args_for(["search", "nothing"]))

    assert code == cli_commands.EXIT_FAILED
    assert "No documents matched" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_search_reports_a_build_without_the_endpoint(
    httpx_mock: HTTPXMock, client: LaserficheClient, args_for, capsys
) -> None:
    httpx_mock.add_response(
        method="POST", url=f"{_BASE}/Searches", status_code=404, json={"title": "Not Found"}
    )

    code = await cli_commands.cmd_search(client, args_for(["search", "x"]))

    assert code == cli_commands.EXIT_FAILED
    assert "no asynchronous /Searches endpoints" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_search_folder_scope_reaches_the_search_command(
    httpx_mock: HTTPXMock, client: LaserficheClient, args_for
) -> None:
    _mock_search(httpx_mock, [])

    await cli_commands.cmd_search(client, args_for(["search", "x", "--folder", r"\HR"]))

    body = json.loads(httpx_mock.get_requests()[0].read())
    assert "LookIn" in body["searchCommand"]


# --- dedupe -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_dedupe_groups_identical_documents(
    httpx_mock: HTTPXMock, client: LaserficheClient, args_for, capsys
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/1/Laserfiche.Repository.Folder/children?%24top=100&%24skip=0",
        json={"value": [_entry(7, "a.pdf"), _entry(8, "b.pdf")]},
    )
    body = b"identical bytes"
    for entry_id in (7, 8):
        url = f"{_BASE}/Entries/{entry_id}/Laserfiche.Repository.Document/edoc"
        # One size probe plus one download per document.
        httpx_mock.add_response(
            method="GET", url=url, content=body, headers={"content-length": str(len(body))}
        )
        httpx_mock.add_response(
            method="GET", url=url, content=body, headers={"content-length": str(len(body))}
        )

    code = await cli_commands.cmd_dedupe(client, args_for(["dedupe", "1", "--no-recursive"]))

    out = capsys.readouterr().out
    assert code == 0
    assert "x2" in out
    assert "1 duplicate group(s)" in out


@pytest.mark.asyncio
async def test_dedupe_reports_when_nothing_needed_downloading(
    httpx_mock: HTTPXMock, client: LaserficheClient, args_for, capsys
) -> None:
    """Distinct sizes mean no bytes move at all — the headline efficiency claim."""
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/1/Laserfiche.Repository.Folder/children?%24top=100&%24skip=0",
        json={"value": [_entry(7, "a.pdf"), _entry(8, "b.pdf")]},
    )
    for entry_id, size in ((7, 10), (8, 20)):
        httpx_mock.add_response(
            method="GET",
            url=f"{_BASE}/Entries/{entry_id}/Laserfiche.Repository.Document/edoc",
            content=b"x" * size,
            headers={"content-length": str(size)},
        )

    await cli_commands.cmd_dedupe(client, args_for(["dedupe", "1", "--no-recursive"]))

    out = capsys.readouterr().out
    assert "0 duplicate group(s)" in out
    assert "downloaded 0" in out
