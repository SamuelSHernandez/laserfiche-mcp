"""Tests for cli.py — argument parsing, --diagnose probes, main() entrypoint.

The MCP server itself isn't exercised here; we test the CLI layer that
wraps it (option parsing, config-error formatting, diagnostic probe
results, exit-code behavior). Anything that would normally call
``mcp.run()`` is short-circuited via a monkeypatch so tests stay fast and
hermetic.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from pytest_httpx import HTTPXMock

from laserfiche_mcp import cli, server
from laserfiche_mcp.client import LaserficheClient
from laserfiche_mcp.config import Settings
from laserfiche_mcp.errors import LaserficheError
from tests.conftest import _BASE, _StubAuth

# --- _parse_args -------------------------------------------------------------


def test_cli_parse_args_defaults() -> None:
    args = cli._parse_args([])
    assert args.help is False
    assert args.version is False
    assert args.diagnose is False
    assert args.verbose == 0
    assert args.quiet is False
    assert args.config is None


def test_cli_parse_args_help_flag() -> None:
    args = cli._parse_args(["--help"])
    assert args.help is True
    short = cli._parse_args(["-h"])
    assert short.help is True


def test_cli_parse_args_version_flag() -> None:
    args = cli._parse_args(["--version"])
    assert args.version is True
    short = cli._parse_args(["-V"])
    assert short.version is True


def test_cli_parse_args_diagnose_flag() -> None:
    args = cli._parse_args(["--diagnose"])
    assert args.diagnose is True


def test_cli_parse_args_verbose_counts() -> None:
    args = cli._parse_args(["-v"])
    assert args.verbose == 1
    args2 = cli._parse_args(["-vv"])
    assert args2.verbose == 2


def test_cli_parse_args_verbose_and_quiet_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        cli._parse_args(["--verbose", "--quiet"])


def test_cli_parse_args_config_path() -> None:
    args = cli._parse_args(["--config", ".env.custom"])
    assert args.config == ".env.custom"


# --- _resolve_log_level ------------------------------------------------------


def test_cli_resolve_log_level_prefers_verbose(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = server._get_settings()
    monkeypatch.setattr(settings, "log_level", "INFO")
    args = cli._parse_args(["-v"])
    assert cli._resolve_log_level(settings, args) == "DEBUG"


def test_cli_resolve_log_level_prefers_quiet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = server._get_settings()
    monkeypatch.setattr(settings, "log_level", "INFO")
    args = cli._parse_args(["--quiet"])
    assert cli._resolve_log_level(settings, args) == "WARNING"


def test_cli_resolve_log_level_falls_back_to_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = server._get_settings()
    monkeypatch.setattr(settings, "log_level", "WARNING")
    args = cli._parse_args([])
    assert cli._resolve_log_level(settings, args) == "WARNING"


# --- _format_config_error ----------------------------------------------------


def test_format_config_error_with_plain_exception() -> None:
    """Non-ValidationError exceptions fall through to a one-line bullet."""
    out = cli._format_config_error(ValueError("LF_USERNAME is required"))
    assert "configuration is missing or invalid." in out
    assert "  - LF_USERNAME is required" in out
    assert "Quick start:" in out


def test_format_config_error_strips_pydantic_value_error_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pydantic prefixes value_error.* messages with 'Value error, '; strip it."""
    # Trigger a real Settings ValidationError by clearing a required env var.
    monkeypatch.delenv("LF_REPO_API_URL", raising=False)
    monkeypatch.delenv("LF_REPOSITORY_ID", raising=False)
    monkeypatch.delenv("LF_USERNAME", raising=False)
    monkeypatch.delenv("LF_PASSWORD", raising=False)
    server._reset_settings_for_tests()
    try:
        Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        out = cli._format_config_error(exc)
    else:
        pytest.fail("Settings() should have raised with required env cleared")

    # The 'Value error, ' prefix must not appear in the formatted output.
    assert "Value error, " not in out
    assert "configuration is missing or invalid." in out
    # And at least one bullet must list the actual problem.
    assert "\n  - " in out


# --- ProbeResult / _run_probe / _probe_optional_endpoints --------------------


def test_probe_result_display_status_ok() -> None:
    assert cli.ProbeResult(label="x", ok=True).display_status() == "OK"


def test_probe_result_display_status_known_failure() -> None:
    assert (
        cli.ProbeResult(label="x", ok=False, status_code=404).display_status()
        == "unavailable (HTTP 404)"
    )


def test_probe_result_display_status_unknown_failure() -> None:
    assert (
        cli.ProbeResult(label="x", ok=False, status_code=None).display_status()
        == "unavailable (HTTP ?)"
    )


@pytest.mark.asyncio
async def test_run_probe_success() -> None:
    async def ok() -> int:
        return 1

    result = await cli._run_probe("hello", ok())
    assert result.ok is True
    assert result.label == "hello"


@pytest.mark.asyncio
async def test_run_probe_captures_laserfiche_error() -> None:
    async def fail() -> int:
        raise LaserficheError("boom", status_code=503)

    result = await cli._run_probe("hello", fail())
    assert result.ok is False
    assert result.status_code == 503


@pytest.mark.asyncio
async def test_probe_optional_endpoints_runs_every_probe(httpx_mock: HTTPXMock) -> None:
    """Each of the 8 optional endpoints is awaited; results come back in order."""
    settings = Settings()  # type: ignore[call-arg]
    # Three respond OK, the rest fail with various statuses — the probe must
    # not abort on any individual failure.
    httpx_mock.add_response(
        method="GET", url="https://lf.example.test/LFRepositoryAPI/v1/Repositories", json={}
    )
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/FieldDefinitions?%24top=1&%24skip=0", json={"value": []}
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/TemplateDefinitions?%24top=1&%24skip=0",
        json={"value": []},
    )
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/TagDefinitions?%24top=1&%24skip=0", status_code=404
    )
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/LinkDefinitions?%24top=1&%24skip=0", status_code=404
    )
    httpx_mock.add_response(method="GET", url=f"{_BASE}/AuditReasons", status_code=403)
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/1/Laserfiche.Repository.Folder/children?%24top=1&%24skip=0",
        status_code=404,
    )
    httpx_mock.add_response(method="POST", url=f"{_BASE}/SimpleSearches", status_code=500)

    async with LaserficheClient(settings, _StubAuth()) as client:
        results = await cli._probe_optional_endpoints(client)

    assert len(results) == 8
    assert [r.label for r in results] == [
        "List repositories",
        "Field definitions",
        "Template definitions",
        "Tag definitions",
        "Link definitions",
        "Audit reasons",
        "Root folder children",
        'SimpleSearches ({LF:Name="*"})',
    ]
    # First three succeeded; last five surface their HTTP status codes.
    assert [r.ok for r in results] == [True, True, True, False, False, False, False, False]
    assert results[3].status_code == 404
    assert results[5].status_code == 403
    assert results[7].status_code == 500


# --- _print_write_mode_report ------------------------------------------------


def test_print_write_mode_report_read_only(capsys: pytest.CaptureFixture[str]) -> None:
    settings = Settings()  # type: ignore[call-arg]
    cli._print_write_mode_report(settings)
    out = capsys.readouterr().out
    assert "Write mode:" in out
    assert "LF_READ_ONLY" in out
    # In read-only mode, write-specific config rows are suppressed.
    assert "Delete batch cap" not in out


def test_print_write_mode_report_writes_enabled(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("LF_READ_ONLY", "false")
    server._reset_settings_for_tests()
    settings = Settings()  # type: ignore[call-arg]
    cli._print_write_mode_report(settings)
    out = capsys.readouterr().out
    # Write rows now appear.
    assert "Write paths allow" in out
    assert "Delete batch cap" in out
    assert "Audit reason required" in out


# --- _run_diagnose -----------------------------------------------------------


@pytest.mark.asyncio
async def test_run_diagnose_success_path(
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Auth OK + every probe responds → exit code 0 and a full report on stdout."""
    monkeypatch.setattr(cli, "build_auth_strategy", lambda _settings: _StubAuth())
    settings = Settings()  # type: ignore[call-arg]
    # Auth probe + 8 optional probes — register all as OK responses.
    for _ in range(2):  # auth probe also calls list_field_definitions
        httpx_mock.add_response(
            method="GET",
            url=f"{_BASE}/FieldDefinitions?%24top=1&%24skip=0",
            json={"value": []},
        )
    httpx_mock.add_response(
        method="GET",
        url="https://lf.example.test/LFRepositoryAPI/v1/Repositories",
        json={},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/TemplateDefinitions?%24top=1&%24skip=0",
        json={"value": []},
    )
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/TagDefinitions?%24top=1&%24skip=0", json={"value": []}
    )
    httpx_mock.add_response(
        method="GET", url=f"{_BASE}/LinkDefinitions?%24top=1&%24skip=0", json={"value": []}
    )
    httpx_mock.add_response(method="GET", url=f"{_BASE}/AuditReasons", json={})
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/Entries/1/Laserfiche.Repository.Folder/children?%24top=1&%24skip=0",
        json={"value": []},
    )
    httpx_mock.add_response(method="POST", url=f"{_BASE}/SimpleSearches", json={"value": []})

    rc = await cli._run_diagnose(settings)
    assert rc == 0
    out = capsys.readouterr().out
    assert "server diagnostic" in out
    assert "Authentication" in out and "OK" in out
    assert "Endpoint probes:" in out
    assert "Done." in out


@pytest.mark.asyncio
async def test_run_diagnose_auth_failure_exits_1(
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Auth probe failure short-circuits with exit code 1 and a help message."""
    monkeypatch.setattr(cli, "build_auth_strategy", lambda _settings: _StubAuth())
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/FieldDefinitions?%24top=1&%24skip=0",
        status_code=401,
    )

    rc = await cli._run_diagnose(settings)
    assert rc == 1
    out = capsys.readouterr().out
    assert "Authentication" in out and "FAIL" in out
    assert "Check LF_USERNAME / LF_PASSWORD" in out


# --- _load_config_file -------------------------------------------------------


def test_load_config_file_missing_exits_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        cli._load_config_file("/definitely/not/a/real/path/.env")
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "--config file not found" in err


def test_load_config_file_happy_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real .env file is loaded into the process environment."""
    env_file = tmp_path / "test.env"
    env_file.write_text("LF_TEST_FROM_DOTENV=value\n")
    monkeypatch.delenv("LF_TEST_FROM_DOTENV", raising=False)
    cli._load_config_file(str(env_file))
    import os

    assert os.environ.get("LF_TEST_FROM_DOTENV") == "value"


# --- main() entrypoint -------------------------------------------------------


def test_main_help_prints_help_and_returns(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("sys.argv", ["laserfiche-mcp", "--help"])
    called: list[str] = []
    cli.main(lambda: called.append("registered"))
    out = capsys.readouterr().out
    assert "Model Context Protocol server for Laserfiche" in out
    # Writes were NOT registered — main returned at --help.
    assert called == []


def test_main_version_prints_version_and_returns(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr("sys.argv", ["laserfiche-mcp", "--version"])
    cli.main(lambda: None)
    out = capsys.readouterr().out
    assert "laserfiche-mcp " in out
    assert any(ch.isdigit() for ch in out), "version line should contain a version number"


def test_main_config_error_exits_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A missing required env var → friendly error to stderr + exit 2."""
    monkeypatch.delenv("LF_REPO_API_URL", raising=False)
    monkeypatch.delenv("LF_REPOSITORY_ID", raising=False)
    monkeypatch.delenv("LF_USERNAME", raising=False)
    monkeypatch.delenv("LF_PASSWORD", raising=False)
    server._reset_settings_for_tests()
    monkeypatch.setattr("sys.argv", ["laserfiche-mcp"])

    with pytest.raises(SystemExit) as exc:
        cli.main(lambda: None)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "configuration is missing or invalid." in err


def test_main_not_implemented_error_exits_2(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Cloud-mode / api_key validators raise NotImplementedError; main handles it."""

    def _raise_not_implemented() -> Any:
        raise NotImplementedError("cloud mode not supported yet")

    monkeypatch.setattr("sys.argv", ["laserfiche-mcp"])
    monkeypatch.setattr(cli, "get_settings", _raise_not_implemented)

    with pytest.raises(SystemExit) as exc:
        cli.main(lambda: None)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "cloud mode not supported yet" in err


def test_main_diagnose_exits_with_diagnose_return_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--diagnose drives _run_diagnose and exits with its return code."""

    async def _stub_diagnose(_settings: Settings) -> int:
        return 7  # arbitrary nonzero sentinel

    monkeypatch.setattr("sys.argv", ["laserfiche-mcp", "--diagnose"])
    monkeypatch.setattr(cli, "_run_diagnose", _stub_diagnose)
    called: list[str] = []
    with pytest.raises(SystemExit) as exc:
        cli.main(lambda: called.append("registered"))
    assert exc.value.code == 7
    # Writes are NOT registered on the diagnose path.
    assert called == []


def test_main_config_flag_loads_dotenv_then_starts(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """--config PATH loads the file, then main proceeds to register + run."""
    env_file = tmp_path / ".env"
    env_file.write_text("LF_TEST_VAR=from_config\n")
    monkeypatch.setattr("sys.argv", ["laserfiche-mcp", "--config", str(env_file)])
    # Stub out everything that would run a real server.
    stub_mcp_run_called: list[bool] = []

    class _StubMCP:
        def run(self) -> None:
            stub_mcp_run_called.append(True)

    monkeypatch.setattr(server, "mcp", _StubMCP())
    register_called: list[str] = []
    cli.main(lambda: register_called.append("registered"))
    assert register_called == ["registered"]
    assert stub_mcp_run_called == [True]


def test_main_keyboard_interrupt_is_swallowed(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Ctrl-C on mcp.run() exits cleanly without a traceback dump."""
    monkeypatch.setattr("sys.argv", ["laserfiche-mcp"])

    class _StubMCP:
        def run(self) -> None:
            raise KeyboardInterrupt

    monkeypatch.setattr(server, "mcp", _StubMCP())
    cli.main(lambda: None)  # Should NOT raise.
