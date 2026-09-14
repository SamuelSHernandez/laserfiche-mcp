"""Tests for cli.py — argument parsing, --diagnose probes, main() entrypoint.

The MCP server itself isn't exercised here; we test the CLI layer that
wraps it (option parsing, config-error formatting, diagnostic probe
results, exit-code behavior). Anything that would normally call
``mcp.run()`` is short-circuited via a monkeypatch so tests stay fast and
hermetic.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from pytest_httpx import HTTPXMock

from laserfiche_mcp import cli, server
from laserfiche_mcp.client import LaserficheClient
from laserfiche_mcp.config import Settings
from laserfiche_mcp.errors import LaserficheError
from tests.conftest import _BASE, _BASE_V2, _StubAuth

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


def test_cli_parse_args_http_defaults() -> None:
    args = cli._parse_args([])
    assert args.http is False
    assert args.host is None
    assert args.port is None


def test_cli_parse_args_http_with_overrides() -> None:
    args = cli._parse_args(["--http", "--host", "0.0.0.0", "--port", "9443"])
    assert args.http is True
    assert args.host == "0.0.0.0"
    assert args.port == 9443


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


def test_main_http_flag_routes_to_run_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """--http calls run_http (not mcp.run) with CLI host/port overrides applied."""
    monkeypatch.setattr(
        "sys.argv",
        ["laserfiche-mcp", "--http", "--host", "0.0.0.0", "--port", "9443"],
    )
    from laserfiche_mcp import http_transport

    captured: list[tuple[str, int]] = []

    def _stub_run_http(settings: Any) -> None:
        captured.append((settings.http_host, settings.http_port))

    monkeypatch.setattr(http_transport, "run_http", _stub_run_http)

    class _StubMCP:
        def run(self) -> None:  # pragma: no cover - must not be called
            raise AssertionError("stdio mcp.run() should not run in --http mode")

    monkeypatch.setattr(server, "mcp", _StubMCP())
    cli.main(lambda: None)
    assert captured == [("0.0.0.0", 9443)]


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


# --- subcommand parsing ------------------------------------------------------


def test_bare_invocation_still_means_serve() -> None:
    """Every existing MCP client config launches this binary with no args."""
    args = cli._parse_args([])
    assert getattr(args, "command", None) is None
    assert args.diagnose is False


def test_diagnose_flag_and_subcommand_both_parse() -> None:
    assert cli._parse_args(["--diagnose"]).diagnose is True
    assert cli._parse_args(["diagnose"]).command == "diagnose"


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["ls", "1"], "ls"),
        (["get", "42"], "get"),
        (["cat", "42"], "cat"),
        (["find", "42", "needle"], "find"),
        (["search", "unpaid balance"], "search"),
        (["manifest", "1"], "manifest"),
        (["dedupe", "1"], "dedupe"),
        (["diff", "1", "2"], "diff"),
        (["serve"], "serve"),
    ],
)
def test_every_subcommand_parses(argv: list[str], expected: str) -> None:
    assert cli._parse_args(argv).command == expected


def test_global_verbose_survives_a_following_subcommand() -> None:
    """Without SUPPRESS defaults the subparser would reset this to 0."""
    args = cli._parse_args(["-v", "find", "42", "needle"])
    assert args.verbose == 1
    assert args.command == "find"


def test_config_before_subcommand_is_preserved() -> None:
    args = cli._parse_args(["--config", ".env.custom", "ls", "1"])
    assert args.config == ".env.custom"


def test_json_flag_defaults_false_and_sets_true() -> None:
    assert cli._parse_args(["ls", "1"]).json is False
    assert cli._parse_args(["ls", "1", "--json"]).json is True


def test_subcommand_specific_options_parse() -> None:
    args = cli._parse_args(["find", "42", "x", "--regex", "--context", "80", "--limit", "5"])
    assert (args.regex, args.context, args.limit) == (True, 80, 5)


def test_manifest_format_choice_is_validated() -> None:
    assert cli._parse_args(["manifest", "1", "--format", "jsonl"]).format == "jsonl"
    with pytest.raises(SystemExit):
        cli._parse_args(["manifest", "1", "--format", "xml"])


def test_entry_reference_accepts_a_path() -> None:
    path = r"\HR\Leases\a.pdf"
    assert cli._parse_args(["cat", path]).entry == path


def test_help_text_lists_the_repository_commands() -> None:
    for name in ("ls", "get", "cat", "find", "search", "manifest", "dedupe", "diff"):
        assert f"  {name} " in cli._HELP_TEXT


# --- diagnose failure classification -----------------------------------------


@pytest.mark.asyncio
async def test_run_diagnose_unreachable_does_not_blame_credentials(
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Wrong URL / DNS / firewall must not read as a password problem."""
    monkeypatch.setattr(cli, "build_auth_strategy", lambda _settings: _StubAuth())
    monkeypatch.setenv("LF_RETRY_ATTEMPTS", "0")
    settings = Settings()  # type: ignore[call-arg]
    import httpx

    httpx_mock.add_exception(httpx.ConnectError("connection refused"))

    rc = await cli._run_diagnose(settings)

    assert rc == 1
    out = capsys.readouterr().out
    assert "UNREACHABLE" in out
    assert "not a credentials problem" in out
    assert "LF_PASSWORD" not in out


@pytest.mark.asyncio
async def test_run_diagnose_404_probes_the_other_api_version(
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Wrong LF_API_VERSION: diagnose should name the version that works."""
    monkeypatch.setattr(cli, "build_auth_strategy", lambda _settings: _StubAuth())
    settings = Settings()  # type: ignore[call-arg]
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE}/FieldDefinitions?%24top=1&%24skip=0",
        status_code=404,
        json={"title": "Not Found"},
    )
    httpx_mock.add_response(
        method="GET",
        url=f"{_BASE_V2}/FieldDefinitions?%24top=1&%24skip=0",
        json={"value": []},
    )

    rc = await cli._run_diagnose(settings)

    assert rc == 1
    out = capsys.readouterr().out
    assert "LF_API_VERSION=v2" in out


@pytest.mark.asyncio
async def test_run_diagnose_401_still_points_at_credentials(
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
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
    assert "LF_USERNAME / LF_PASSWORD" in out
    assert "9528" in out  # the misleading LF error code is called out


# --- setup wizard -------------------------------------------------------------


def test_setup_writes_user_env_and_runs_diagnose(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    user_env = tmp_path / ".laserfiche-mcp" / ".env"
    monkeypatch.setattr(cli, "USER_ENV_PATH", user_env)

    answers = iter(
        [
            "https://lf.example.test/LFRepositoryAPI",  # url
            "demo",  # repo
            "svc-account",  # username
            "",  # api version -> default v1
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    import getpass

    monkeypatch.setattr(getpass, "getpass", lambda _prompt: "s3cret")

    async def fake_diagnose(settings):
        assert settings.repository_id == "demo"
        return 0

    monkeypatch.setattr(cli, "_run_diagnose", fake_diagnose)

    rc = cli._run_setup()

    assert rc == 0
    written = user_env.read_text(encoding="utf-8")
    assert "LF_REPO_API_URL=https://lf.example.test/LFRepositoryAPI" in written
    assert "LF_PASSWORD=s3cret" in written
    assert "LF_API_VERSION=v1" in written
    out = capsys.readouterr().out
    assert "You're connected" in out


def test_setup_cancelled_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    user_env = tmp_path / ".laserfiche-mcp" / ".env"
    monkeypatch.setattr(cli, "USER_ENV_PATH", user_env)

    def interrupt(_prompt):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", interrupt)

    rc = cli._run_setup()

    assert rc == 1
    assert not user_env.exists()
    assert "nothing was written" in capsys.readouterr().out


def test_user_env_fallback_only_when_nothing_else_configured(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    user_env = tmp_path / ".laserfiche-mcp" / ".env"
    user_env.parent.mkdir(parents=True)
    user_env.write_text("LF_REPOSITORY_ID=from-user-level\n", encoding="utf-8")
    monkeypatch.setattr(cli, "USER_ENV_PATH", user_env)
    monkeypatch.chdir(tmp_path)  # no ./.env here

    # Case 1: explicit env var present -> user-level file must NOT load.
    monkeypatch.setenv("LF_REPO_API_URL", "https://explicit.example.test/api")
    monkeypatch.delenv("LF_REPOSITORY_ID", raising=False)
    cli._load_user_env_if_unconfigured()
    assert os.environ.get("LF_REPOSITORY_ID") != "from-user-level"

    # Case 2: nothing configured -> it loads.
    monkeypatch.delenv("LF_REPO_API_URL", raising=False)
    cli._load_user_env_if_unconfigured()
    assert os.environ.get("LF_REPOSITORY_ID") == "from-user-level"


# --- command aliases ----------------------------------------------------------


@pytest.mark.parametrize(
    ("alias", "canonical"),
    [
        ("list", "ls"),
        ("download", "get"),
        ("read", "cat"),
        ("compare", "diff"),
        ("duplicates", "dedupe"),
    ],
)
def test_office_friendly_aliases_parse(alias: str, canonical: str) -> None:
    from laserfiche_mcp.cli_commands import COMMAND_ALIASES

    argv = {
        "ls": [alias, "1"],
        "get": [alias, "1"],
        "cat": [alias, "1"],
        "diff": [alias, "1", "2"],
        "dedupe": [alias, "1"],
    }[canonical]
    args = cli._parse_args(argv)
    assert args.command == alias
    assert COMMAND_ALIASES[alias] == canonical


def test_setup_subcommand_parses() -> None:
    assert cli._parse_args(["setup"]).command == "setup"


# --- diagnose speed: probes must not inherit the retry policy ----------------


@pytest.mark.asyncio
async def test_diagnose_probes_with_zero_retries(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """diagnose exists to give a fast verdict; with the configured backoff
    (up to 10 retries) an unreachable server would stall it for minutes."""
    captured: dict[str, Any] = {}

    class _RecordingClient:
        def __init__(self, settings: Settings, auth: Any) -> None:
            captured["settings"] = settings

        async def __aenter__(self) -> _RecordingClient:
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def list_field_definitions(self, max_results: int = 1) -> None:
            raise LaserficheError("connect timeout")  # no status: unreachable

    monkeypatch.setenv("LF_RETRY_ATTEMPTS", "5")
    monkeypatch.setattr(cli, "LaserficheClient", _RecordingClient)
    monkeypatch.setattr(cli, "build_auth_strategy", lambda _s: object())

    settings = Settings()  # type: ignore[call-arg]
    assert settings.retry_attempts == 5

    rc = await cli._run_diagnose(settings)

    assert rc == 1
    assert captured["settings"].retry_attempts == 0
    assert "UNREACHABLE" in capsys.readouterr().out


# --- setup wizard hardening ---------------------------------------------------


def test_url_problem_flags_bare_hostname_and_paths() -> None:
    assert cli._url_problem("lf.example.org") is not None
    assert cli._url_problem(r"\\server\share") is not None
    assert cli._url_problem("ftp://lf.example.org") is not None
    assert cli._url_problem("https://") is not None
    assert cli._url_problem("https://lf.example.org/LFRepositoryAPI") is None
    assert cli._url_problem("http://10.0.0.5/LFRepositoryAPI") is None


def test_env_quote_passes_simple_values_through() -> None:
    assert cli._env_quote("v1") == "v1"
    assert cli._env_quote("https://lf.example.org/LFRepositoryAPI") == (
        "https://lf.example.org/LFRepositoryAPI"
    )


def test_env_quote_round_trips_awkward_passwords(tmp_path: Path) -> None:
    """A password with spaces, '#', quotes, or backslashes must survive the
    write-then-parse cycle python-dotenv (and pydantic-settings) apply."""
    from dotenv import dotenv_values

    awkward = 'p4$s back\\slash#1 "quoted"'
    env_file = tmp_path / ".env"
    env_file.write_text(f"LF_PASSWORD={cli._env_quote(awkward)}\n", encoding="utf-8")

    assert dotenv_values(env_file)["LF_PASSWORD"] == awkward


def test_setup_declines_to_overwrite_existing_config(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    user_env = tmp_path / ".laserfiche-mcp" / ".env"
    user_env.parent.mkdir(parents=True)
    user_env.write_text("LF_REPOSITORY_ID=keep-me\n", encoding="utf-8")
    monkeypatch.setattr(cli, "USER_ENV_PATH", user_env)
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")

    rc = cli._run_setup()

    assert rc == 0
    assert user_env.read_text(encoding="utf-8") == "LF_REPOSITORY_ID=keep-me\n"
    assert "nothing was changed" in capsys.readouterr().out


def test_setup_overwrites_after_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    user_env = tmp_path / ".laserfiche-mcp" / ".env"
    user_env.parent.mkdir(parents=True)
    user_env.write_text("LF_REPOSITORY_ID=old\n", encoding="utf-8")
    monkeypatch.setattr(cli, "USER_ENV_PATH", user_env)

    answers = iter(
        [
            "y",  # overwrite?
            "https://lf.example.test/LFRepositoryAPI",
            "demo",
            "svc-account",
            "",  # api version -> default
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    import getpass

    monkeypatch.setattr(getpass, "getpass", lambda _prompt: "s3cret")

    async def fake_diagnose(_settings: Settings) -> int:
        return 0

    monkeypatch.setattr(cli, "_run_diagnose", fake_diagnose)

    rc = cli._run_setup()

    assert rc == 0
    assert "LF_REPOSITORY_ID=demo" in user_env.read_text(encoding="utf-8")


def test_setup_reprompts_on_invalid_url(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    user_env = tmp_path / ".laserfiche-mcp" / ".env"
    monkeypatch.setattr(cli, "USER_ENV_PATH", user_env)

    answers = iter(
        [
            "lf.example.test",  # bare hostname — rejected, re-prompted
            "https://lf.example.test/LFRepositoryAPI",
            "demo",
            "svc-account",
            "",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    import getpass

    monkeypatch.setattr(getpass, "getpass", lambda _prompt: "s3cret")

    async def fake_diagnose(_settings: Settings) -> int:
        return 0

    monkeypatch.setattr(cli, "_run_diagnose", fake_diagnose)

    rc = cli._run_setup()

    assert rc == 0
    out = capsys.readouterr().out
    assert "http://" in out  # the objection names the expected scheme
    assert "LF_REPO_API_URL=https://lf.example.test/LFRepositoryAPI" in user_env.read_text(
        encoding="utf-8"
    )


def test_setup_quotes_awkward_password_in_env_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from dotenv import dotenv_values

    user_env = tmp_path / ".laserfiche-mcp" / ".env"
    monkeypatch.setattr(cli, "USER_ENV_PATH", user_env)

    answers = iter(
        [
            "https://lf.example.test/LFRepositoryAPI",
            "demo",
            "svc-account",
            "",
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    import getpass

    monkeypatch.setattr(getpass, "getpass", lambda _prompt: "pa ss#word")

    async def fake_diagnose(_settings: Settings) -> int:
        return 0

    monkeypatch.setattr(cli, "_run_diagnose", fake_diagnose)

    rc = cli._run_setup()

    assert rc == 0
    assert dotenv_values(user_env)["LF_PASSWORD"] == "pa ss#word"


def test_setup_insists_on_a_nonempty_password(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    user_env = tmp_path / ".laserfiche-mcp" / ".env"
    monkeypatch.setattr(cli, "USER_ENV_PATH", user_env)

    answers = iter(
        [
            "https://lf.example.test/LFRepositoryAPI",
            "demo",
            "svc-account",
            "",  # api version default
        ]
    )
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    import getpass

    passwords = iter(["", "   ", "finally-a-password"])
    monkeypatch.setattr(getpass, "getpass", lambda _prompt: next(passwords))

    async def fake_diagnose(_settings: Settings) -> int:
        return 0

    monkeypatch.setattr(cli, "_run_diagnose", fake_diagnose)

    rc = cli._run_setup()

    assert rc == 0
    assert "A value is required" in capsys.readouterr().out
    assert "finally-a-password" in user_env.read_text(encoding="utf-8")
