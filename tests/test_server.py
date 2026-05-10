"""Tests for server module helpers and tool registration."""

from __future__ import annotations

import pytest

from laserfiche_mcp import server
from laserfiche_mcp.config import Settings


@pytest.fixture(autouse=True)
def _reset_settings(lf_env: dict[str, str]) -> None:
    server._reset_settings_for_tests()


def test_clamp_max_results_uses_default_when_none() -> None:
    settings = server._get_settings()
    assert server._clamp_max_results(None) == settings.max_results_default


def test_clamp_max_results_floors_at_one() -> None:
    assert server._clamp_max_results(0) == 1
    assert server._clamp_max_results(-5) == 1


def test_clamp_max_results_caps_at_ceiling() -> None:
    settings = server._get_settings()
    assert server._clamp_max_results(99_999) == settings.max_results_ceiling


def test_clamp_max_results_passes_through_in_range() -> None:
    assert server._clamp_max_results(10) == 10


@pytest.mark.asyncio
async def test_all_tools_registered() -> None:
    tools = await server.mcp.list_tools()
    names = {t.name for t in tools}
    assert names == {
        "search_entries",
        "search_by_name",
        "list_folder",
        "get_entry",
        "get_entry_by_path",
        "get_field_values",
        "get_document_text",
        "get_document_edoc",
    }


# --- main() entrypoint behaviors --------------------------------------------


def test_main_help_prints_and_exits_zero(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.argv", ["laserfiche-mcp", "--help"])
    server.main()
    out = capsys.readouterr().out
    assert "laserfiche-mcp" in out
    assert "Configuration" in out


def test_main_version_prints_version(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sys.argv", ["laserfiche-mcp", "--version"])
    server.main()
    out = capsys.readouterr().out.strip()
    assert out.startswith("laserfiche-mcp ")


def test_main_missing_config_prints_friendly_error(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Strip everything; reset cached settings so Settings() actually re-runs.
    for var in ("LF_REPO_API_URL", "LF_REPOSITORY_ID", "LF_USERNAME", "LF_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr("sys.argv", ["laserfiche-mcp"])
    server._reset_settings_for_tests()

    with pytest.raises(SystemExit) as exit_info:
        server.main()
    assert exit_info.value.code == 2

    err = capsys.readouterr().err
    assert "configuration is missing" in err
    assert "LF_REPO_API_URL" in err
    # No Python traceback — just the friendly message
    assert "Traceback" not in err


def test_format_config_error_strips_value_error_prefix() -> None:
    """Pydantic prefixes value_error messages with 'Value error, '; we strip it."""
    settings_cls = Settings
    try:
        settings_cls()  # type: ignore[call-arg]
    except Exception as exc:
        msg = server._format_config_error(exc)
        assert "Value error, " not in msg
        assert "configuration" in msg
