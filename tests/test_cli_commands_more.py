"""More CLI subcommand tests: error paths, ``--json`` output, hostile names.

Split from ``test_cli_commands.py`` to keep each file focused: that one
covers the happy paths and output conventions, this one covers the error
humanization, the top-level ``run()`` exception handling, the ``--json``
contract on every command, and entry names local filesystems reject.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pytest_httpx import HTTPXMock

from laserfiche_mcp import cli, cli_commands
from laserfiche_mcp.client import LaserficheClient
from laserfiche_mcp.config import Settings
from laserfiche_mcp.errors import LaserficheError
from tests.client.conftest import _build_client
from tests.conftest import _BASE, SAMPLE_PDF_BYTES, SAMPLE_PDF_TEXT

_EDOC = f"{_BASE}/Entries/42/Laserfiche.Repository.Document/edoc"


@pytest.fixture
def args_for():
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


# --- error humanization ------------------------------------------------------


def test_humanize_unreachable_points_at_setup() -> None:
    msg = cli_commands._humanize_error(LaserficheError("connect timeout"))
    assert "could not reach" in msg
    assert "setup" in msg


def test_humanize_auth_failure_points_at_setup() -> None:
    exc = LaserficheError("x", status_code=401, detail={"title": "Unauthorized"})
    assert "rejected the login" in cli_commands._humanize_error(exc)


def test_humanize_not_found_includes_the_server_title() -> None:
    exc = LaserficheError("x", status_code=404, detail={"title": "Entry not found."})
    msg = cli_commands._humanize_error(exc)
    assert "not found" in msg
    assert "Entry not found." in msg


def test_humanize_other_status_with_title() -> None:
    exc = LaserficheError("x", status_code=500, detail={"title": "Internal error"})
    assert cli_commands._humanize_error(exc) == "HTTP 500: Internal error"


def test_humanize_other_status_without_title() -> None:
    exc = LaserficheError("teapot says no", status_code=418, detail="teapot says no")
    assert cli_commands._humanize_error(exc).startswith("HTTP 418:")


# --- run(): top-level exception handling -------------------------------------


def test_run_prints_one_line_for_laserfiche_errors(
    monkeypatch: pytest.MonkeyPatch, args_for, capsys
) -> None:
    async def boom(_settings, _args):
        raise LaserficheError("nope", status_code=404, detail={"title": "gone"})

    monkeypatch.setattr(cli_commands, "_dispatch", boom)
    rc = cli_commands.run(Settings(), args_for(["ls", "1"]))  # type: ignore[call-arg]

    assert rc == cli_commands.EXIT_FAILED
    err = capsys.readouterr().err
    assert "laserfiche-mcp ls" in err
    assert "Traceback" not in err


def test_run_prints_one_line_for_local_file_errors(
    monkeypatch: pytest.MonkeyPatch, args_for, capsys
) -> None:
    """Permission denied / disk full / bad path must not dump a traceback."""

    async def boom(_settings, _args):
        raise OSError(13, "Permission denied", "out.pdf")

    monkeypatch.setattr(cli_commands, "_dispatch", boom)
    rc = cli_commands.run(Settings(), args_for(["get", "42"]))  # type: ignore[call-arg]

    assert rc == cli_commands.EXIT_FAILED
    err = capsys.readouterr().err
    assert "local file error" in err
    assert "Traceback" not in err


def test_run_handles_ctrl_c(monkeypatch: pytest.MonkeyPatch, args_for, capsys) -> None:
    async def boom(_settings, _args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_commands, "_dispatch", boom)
    rc = cli_commands.run(Settings(), args_for(["ls", "1"]))  # type: ignore[call-arg]

    assert rc == cli_commands.EXIT_FAILED
    assert "interrupted" in capsys.readouterr().err


# --- resolve_entry_id: unresolvable path -------------------------------------


@pytest.mark.asyncio
async def test_path_without_an_id_is_a_clean_error(
    httpx_mock: HTTPXMock, client: LaserficheClient
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/ByPath?fullPath=%5CHR",
        json={"name": "HR"},  # no id field at all
    )

    with pytest.raises(LaserficheError, match="did not resolve"):
        await cli_commands.resolve_entry_id(client, "\\HR")


# --- Windows-hostile entry names ---------------------------------------------


@pytest.mark.asyncio
async def test_get_sanitizes_entry_names_with_invalid_characters(
    httpx_mock: HTTPXMock, client: LaserficheClient, args_for, tmp_path: Path
) -> None:
    """Laserfiche allows ':' and '?' in entry names; local filesystems don't."""
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/42", json=_entry(name="Report: Q1?.pdf")
    )
    httpx_mock.add_response(method="GET", url=_EDOC, content=b"bytes")

    code = await cli_commands.cmd_get(client, args_for(["get", "42", "--to", str(tmp_path)]))

    assert code == 0
    assert (tmp_path / "Report_ Q1_.pdf").exists()


@pytest.mark.asyncio
async def test_cat_survives_an_entry_name_with_invalid_characters(
    httpx_mock: HTTPXMock, client: LaserficheClient, args_for, capsys
) -> None:
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/Entries/42", json=_entry(name="lease: signed.pdf")
    )
    httpx_mock.add_response(method="GET", url=_EDOC, content=SAMPLE_PDF_BYTES)

    code = await cli_commands.cmd_cat(client, args_for(["cat", "42"]))

    assert code == 0
    assert SAMPLE_PDF_TEXT in capsys.readouterr().out


# --- the --json contract on every command ------------------------------------


@pytest.mark.asyncio
async def test_get_json_reports_path_size_and_hash(
    httpx_mock: HTTPXMock, client: LaserficheClient, args_for, capsys, tmp_path: Path
) -> None:
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/42", json=_entry())
    httpx_mock.add_response(method="GET", url=_EDOC, content=b"PDF BYTES")
    dest = tmp_path / "saved.pdf"

    code = await cli_commands.cmd_get(client, args_for(["get", "42", "--to", str(dest), "--json"]))

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["entry_id"] == 42
    assert payload["byte_size"] == len(b"PDF BYTES")
    assert len(payload["sha256"]) == 64


@pytest.mark.asyncio
async def test_cat_json_carries_text_and_backend(
    httpx_mock: HTTPXMock, client: LaserficheClient, args_for, capsys
) -> None:
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/42", json=_entry())
    httpx_mock.add_response(method="GET", url=_EDOC, content=SAMPLE_PDF_BYTES)

    code = await cli_commands.cmd_cat(client, args_for(["cat", "42", "--json"]))

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert SAMPLE_PDF_TEXT in payload["text"]
    assert payload["backend"] == "pypdf"
    assert payload["page_count"] == 1


@pytest.mark.asyncio
async def test_cat_max_chars_truncates_and_says_so(
    httpx_mock: HTTPXMock, client: LaserficheClient, args_for, capsys
) -> None:
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/42", json=_entry())
    httpx_mock.add_response(method="GET", url=_EDOC, content=SAMPLE_PDF_BYTES)

    code = await cli_commands.cmd_cat(client, args_for(["cat", "42", "--max-chars", "5", "--json"]))

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["char_count"] == 5
    assert any("truncated" in w.lower() for w in payload["warnings"])


@pytest.mark.asyncio
async def test_cat_pages_selects_within_a_pdf(
    httpx_mock: HTTPXMock, client: LaserficheClient, args_for, capsys
) -> None:
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/42", json=_entry())
    httpx_mock.add_response(method="GET", url=_EDOC, content=SAMPLE_PDF_BYTES)

    code = await cli_commands.cmd_cat(client, args_for(["cat", "42", "--pages", "1"]))

    assert code == 0
    assert SAMPLE_PDF_TEXT in capsys.readouterr().out


@pytest.mark.asyncio
async def test_cat_rejects_pages_beyond_the_document(
    httpx_mock: HTTPXMock, client: LaserficheClient, args_for, capsys
) -> None:
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/42", json=_entry())
    httpx_mock.add_response(method="GET", url=_EDOC, content=SAMPLE_PDF_BYTES)

    code = await cli_commands.cmd_cat(client, args_for(["cat", "42", "--pages", "99"]))

    assert code == cli_commands.EXIT_USAGE
    assert "page(s)" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_cat_rejects_a_malformed_page_spec(
    httpx_mock: HTTPXMock, client: LaserficheClient, args_for, capsys
) -> None:
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/42", json=_entry())
    httpx_mock.add_response(method="GET", url=_EDOC, content=SAMPLE_PDF_BYTES)

    code = await cli_commands.cmd_cat(client, args_for(["cat", "42", "--pages", "nine"]))

    assert code == cli_commands.EXIT_USAGE


@pytest.mark.asyncio
async def test_find_json_reports_totals_and_matches(
    httpx_mock: HTTPXMock, client: LaserficheClient, args_for, capsys
) -> None:
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/42", json=_entry())
    httpx_mock.add_response(method="GET", url=_EDOC, content=SAMPLE_PDF_BYTES)

    word = SAMPLE_PDF_TEXT.split()[0]
    code = await cli_commands.cmd_find(client, args_for(["find", "42", word, "--json"]))

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_matches"] >= 1
    assert payload["matches"][0]["page"] == 1
    assert word.lower() in payload["matches"][0]["context"].lower()


@pytest.mark.asyncio
async def test_find_reports_extraction_failure_cleanly(
    httpx_mock: HTTPXMock, client: LaserficheClient, args_for, capsys
) -> None:
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/42", json=_entry(name="scan.tiff"))
    httpx_mock.add_response(method="GET", url=_EDOC, content=b"II*")

    code = await cli_commands.cmd_find(client, args_for(["find", "42", "anything"]))

    assert code == cli_commands.EXIT_FAILED
    assert "unsupported_format" in capsys.readouterr().err


_TOKEN = "srch-cli-more"


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
async def test_search_json_is_parseable(
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
                    "context": "an unpaid balance of 2,400 dollars",
                    "highlight1Offset": 3,
                    "highlight1Length": 14,
                }
            ]
        },
    )

    code = await cli_commands.cmd_search(client, args_for(["search", "unpaid", "--json"]))

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["results"][0]["name"] == "lease.pdf"
    assert payload["results"][0]["hits"][0]["page"] == 4


@pytest.mark.asyncio
async def test_dedupe_json_lists_groups(
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
        httpx_mock.add_response(
            method="GET", url=url, content=body, headers={"content-length": str(len(body))}
        )
        httpx_mock.add_response(
            method="GET", url=url, content=body, headers={"content-length": str(len(body))}
        )

    code = await cli_commands.cmd_dedupe(
        client, args_for(["dedupe", "1", "--no-recursive", "--json"])
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["groups"]) == 1
    assert payload["documents_hashed"] == 2


@pytest.mark.asyncio
async def test_manifest_json_without_out_includes_the_rows(
    httpx_mock: HTTPXMock, client: LaserficheClient, args_for, capsys
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/1/Laserfiche.Repository.Folder/children?%24top=100&%24skip=0",
        json={"value": [_entry(7, "a.pdf")]},
    )

    code = await cli_commands.cmd_manifest(client, args_for(["manifest", "1", "--json"]))

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total"] == 1
    assert payload["rows"][0]["entry_id"] == 7


@pytest.mark.asyncio
async def test_manifest_writes_jsonl_when_asked(
    httpx_mock: HTTPXMock, client: LaserficheClient, args_for, capsys, tmp_path: Path
) -> None:
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/1/Laserfiche.Repository.Folder/children?%24top=100&%24skip=0",
        json={"value": [_entry(7, "a.pdf")]},
    )
    out_file = tmp_path / "tree.jsonl"

    code = await cli_commands.cmd_manifest(
        client, args_for(["manifest", "1", "--out", str(out_file), "--format", "jsonl"])
    )

    assert code == 0
    lines = out_file.read_text(encoding="utf-8").strip().splitlines()
    assert json.loads(lines[0])["entry_id"] == 7


@pytest.mark.asyncio
async def test_diff_json_carries_the_differences(
    httpx_mock: HTTPXMock, client: LaserficheClient, args_for, capsys
) -> None:
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/1", json=_entry(1, "a.pdf"))
    httpx_mock.add_response(method="GET", url=f"{_BASE}/Entries/2", json=_entry(2, "b.pdf"))

    code = await cli_commands.cmd_diff(
        client, args_for(["diff", "1", "2", "--no-fields", "--json"])
    )

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    names = [d["name"] for d in payload["differences"]]
    assert "name" in names
