"""Tests for ``laserfiche_mcp.observability``: redact, tool_logger, JSON formatter.

The decorator's contract is "one log line per call, with a propagated
request_id that the error classifier picks up via ContextVar." These tests
exercise both halves: the redaction helper directly, and the decorator in
combination with ``errors.classify_lf_error``.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import pytest

from laserfiche_mcp.errors import LaserficheError, classify_lf_error
from laserfiche_mcp.observability import (
    REDACTED,
    REDACTED_HOST,
    REDACTED_REPO,
    JsonLogFormatter,
    configure_logging,
    get_request_id,
    get_request_id_or_new,
    redact,
    tool_logger,
)

# ---------------------------------------------------------------------------
# redact()
# ---------------------------------------------------------------------------


class TestRedact:
    def test_replaces_top_level_password(self) -> None:
        assert redact({"password": "hunter2"}) == {"password": REDACTED}

    def test_case_insensitive_key_match(self) -> None:
        assert redact({"Password": "x", "API_KEY": "y"}) == {
            "Password": REDACTED,
            "API_KEY": REDACTED,
        }

    def test_recurses_into_nested_dict(self) -> None:
        result = redact({"creds": {"password": "x", "user": "alice"}})
        assert result == {"creds": {"password": REDACTED, "user": "alice"}}

    def test_recurses_into_list_of_dicts(self) -> None:
        result = redact([{"password": "a"}, {"token": "b"}])
        assert result == [{"password": REDACTED}, {"token": REDACTED}]

    def test_recurses_into_tuple_of_dicts(self) -> None:
        result = redact(({"password": "a"}, {"token": "b"}))
        assert result == ({"password": REDACTED}, {"token": REDACTED})

    def test_non_redacted_keys_pass_through(self) -> None:
        result = redact({"entry_id": 42, "query": "x", "mode": "info"})
        assert result == {"entry_id": 42, "query": "x", "mode": "info"}

    def test_confirmation_token_is_redacted(self) -> None:
        assert redact({"confirmation_token": "abc.def.ghi"}) == {"confirmation_token": REDACTED}

    def test_input_not_mutated(self) -> None:
        original = {"password": "x", "nested": {"token": "y"}}
        snapshot = {"password": "x", "nested": {"token": "y"}}
        redact(original)
        assert original == snapshot
        assert original["nested"]["token"] == "y"

    def test_host_substring_replaced_in_string_value(self) -> None:
        out = redact(
            "GET https://lf.internal.example/LFRepositoryAPI/v1/Repositories/MYREPO/Entries/1",
            host="lf.internal.example",
            repo_id="MYREPO",
        )
        assert "lf.internal.example" not in out
        assert "MYREPO" not in out
        assert REDACTED_HOST in out
        assert REDACTED_REPO in out

    def test_repo_id_replacement_respects_boundaries(self) -> None:
        # "MYREPO" inside the path gets replaced; "MYREPOTHER" (longer
        # token starting with MYREPO) does NOT, because the segment
        # boundary check sees MYREPOTHER as a different identifier.
        out = redact("/v1/Repositories/MYREPO/Entries", repo_id="MYREPO")
        assert out == f"/v1/Repositories/{REDACTED_REPO}/Entries"

        out = redact("MYREPOTHER", repo_id="MYREPO")
        assert out == "MYREPOTHER"

    def test_host_redaction_recurses_into_dict_values(self) -> None:
        out = redact(
            {"url": "https://lf.internal/api"},
            host="lf.internal",
        )
        assert out == {"url": f"https://{REDACTED_HOST}/api"}

    def test_no_host_or_repo_means_string_unchanged(self) -> None:
        assert redact("some string") == "some string"

    def test_scalars_pass_through(self) -> None:
        assert redact(42) == 42
        assert redact(True) is True
        assert redact(None) is None
        assert redact(b"bytes") == b"bytes"


# ---------------------------------------------------------------------------
# tool_logger decorator
# ---------------------------------------------------------------------------


class _CaptureHandler(logging.Handler):
    """Test handler that stashes records for inspection."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def captured_logs() -> Any:
    """Capture log records emitted at the ``laserfiche_mcp.tools`` logger."""
    handler = _CaptureHandler()
    tools_logger = logging.getLogger("laserfiche_mcp.tools")
    prior_level = tools_logger.level
    prior_propagate = tools_logger.propagate
    tools_logger.addHandler(handler)
    tools_logger.setLevel(logging.DEBUG)
    tools_logger.propagate = False
    try:
        yield handler
    finally:
        tools_logger.removeHandler(handler)
        tools_logger.setLevel(prior_level)
        tools_logger.propagate = prior_propagate


@pytest.mark.asyncio
async def test_tool_logger_emits_one_ok_event_on_success(captured_logs: Any) -> None:
    @tool_logger
    async def my_tool(entry_id: int) -> dict[str, Any]:
        return {"mode": "ok", "value": entry_id}

    result = await my_tool(entry_id=42)

    assert result == {"mode": "ok", "value": 42}
    events = [r.lf_event for r in captured_logs.records if hasattr(r, "lf_event")]
    assert len(events) == 1
    ev = events[0]
    assert ev["tool"] == "my_tool"
    assert ev["outcome"] == "ok"
    assert ev["duration_ms"] >= 0
    assert ev["args"] == {"entry_id": 42}
    assert "request_id" in ev
    # UUID4 shape
    assert re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
        ev["request_id"],
    )


@pytest.mark.asyncio
async def test_tool_logger_emits_error_event_with_kind_and_subkind(
    captured_logs: Any,
) -> None:
    @tool_logger
    async def failing_tool() -> dict[str, Any]:
        return {
            "mode": "error",
            "kind": "not_found",
            "error": "not_found",
            "upstream_trace_id": "trace-xyz",
        }

    result = await failing_tool()
    assert result["mode"] == "error"

    events = [r.lf_event for r in captured_logs.records if hasattr(r, "lf_event")]
    assert len(events) == 1
    ev = events[0]
    assert ev["outcome"] == "error"
    assert ev["error_kind"] == "not_found"
    assert ev["error_subkind"] == "not_found"
    assert ev["upstream_trace_id"] == "trace-xyz"


@pytest.mark.asyncio
async def test_tool_logger_redacts_credential_kwargs(captured_logs: Any) -> None:
    @tool_logger
    async def importer(file_path: str, password: str) -> dict[str, Any]:
        return {"mode": "ok"}

    await importer(file_path="/tmp/x", password="hunter2")

    ev = next(r.lf_event for r in captured_logs.records if hasattr(r, "lf_event"))
    assert ev["args"]["file_path"] == "/tmp/x"
    assert ev["args"]["password"] == REDACTED


@pytest.mark.asyncio
async def test_tool_logger_propagates_request_id_via_contextvar(
    captured_logs: Any,
) -> None:
    captured: dict[str, Any] = {}

    @tool_logger
    async def reads_contextvar() -> dict[str, Any]:
        captured["rid_inside"] = get_request_id()
        return {"mode": "ok"}

    await reads_contextvar()
    ev = next(r.lf_event for r in captured_logs.records if hasattr(r, "lf_event"))
    assert captured["rid_inside"] is not None
    assert captured["rid_inside"] == ev["request_id"]


@pytest.mark.asyncio
async def test_tool_logger_backfills_request_id_into_error_dict(
    captured_logs: Any,
) -> None:
    """If a pre-server guard returns an error dict without request_id, the
    decorator should backfill it so the agent sees the same ID we logged."""

    @tool_logger
    async def naive_guard() -> dict[str, Any]:
        # Deliberately omits request_id, as a pre-server guard might.
        return {"mode": "error", "kind": "invalid_input", "error": "bad_input"}

    result = await naive_guard()
    ev = next(r.lf_event for r in captured_logs.records if hasattr(r, "lf_event"))
    assert result["request_id"] == ev["request_id"]


@pytest.mark.asyncio
async def test_tool_logger_request_id_propagates_to_error_classifier(
    captured_logs: Any,
) -> None:
    """The whole point: classify_lf_error reads from the ContextVar so the
    agent-visible error_response.request_id matches the per-call log line."""

    @tool_logger
    async def server_error_tool() -> dict[str, Any]:
        exc = LaserficheError("boom", status_code=500)
        return classify_lf_error("server_error_tool", exc)

    result = await server_error_tool()
    ev = next(r.lf_event for r in captured_logs.records if hasattr(r, "lf_event"))
    assert result["request_id"] == ev["request_id"]


@pytest.mark.asyncio
async def test_tool_logger_emits_exception_event_and_reraises(
    captured_logs: Any,
) -> None:
    @tool_logger
    async def raises() -> dict[str, Any]:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await raises()

    ev = next(r.lf_event for r in captured_logs.records if hasattr(r, "lf_event"))
    assert ev["outcome"] == "exception"
    assert ev["error_kind"] == "upstream_unavailable"
    assert ev["error_subkind"] == "uncaught_exception"
    assert "boom" in ev["error_message"]


@pytest.mark.asyncio
async def test_tool_logger_clears_contextvar_after_call() -> None:
    """ContextVar must reset between calls so request_ids don't leak across boundaries."""
    assert get_request_id() is None

    @tool_logger
    async def t() -> dict[str, Any]:
        assert get_request_id() is not None
        return {"mode": "ok"}

    await t()
    assert get_request_id() is None


@pytest.mark.asyncio
async def test_tool_logger_is_idempotent() -> None:
    """Wrapping twice must not double-log."""

    @tool_logger
    async def t() -> dict[str, Any]:
        return {"mode": "ok"}

    again = tool_logger(t)
    # The second application returns the same object (already wrapped).
    assert again is t


def test_get_request_id_or_new_returns_fresh_uuid_outside_context() -> None:
    """Outside any tool_logger, the fallback must still produce a non-null UUID."""
    a = get_request_id_or_new()
    b = get_request_id_or_new()
    assert a != b
    for s in (a, b):
        assert re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}",
            s,
        )


# ---------------------------------------------------------------------------
# JSON formatter + configure_logging
# ---------------------------------------------------------------------------


def test_json_formatter_emits_one_json_object_per_record() -> None:
    record = logging.LogRecord(
        name="laserfiche_mcp.tools",
        level=logging.INFO,
        pathname="test.py",
        lineno=1,
        msg="tool_call test ok",
        args=(),
        exc_info=None,
    )
    out = JsonLogFormatter().format(record)
    payload = json.loads(out)
    assert payload["level"] == "INFO"
    assert payload["logger"] == "laserfiche_mcp.tools"
    assert payload["message"] == "tool_call test ok"
    assert "ts" in payload


def test_json_formatter_hoists_lf_event_extra() -> None:
    record = logging.LogRecord(
        name="laserfiche_mcp.tools",
        level=logging.INFO,
        pathname="test.py",
        lineno=1,
        msg="msg",
        args=(),
        exc_info=None,
    )
    record.lf_event = {  # type: ignore[attr-defined]
        "tool": "x",
        "request_id": "rid-1",
        "outcome": "ok",
    }
    payload = json.loads(JsonLogFormatter().format(record))
    assert payload["event"] == {"tool": "x", "request_id": "rid-1", "outcome": "ok"}


def test_configure_logging_json_installs_json_formatter() -> None:
    configure_logging(level="DEBUG", format_="json")
    root = logging.getLogger()
    # Single handler with the JSON formatter.
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, JsonLogFormatter)
    # Restore default for any subsequent tests.
    configure_logging(level="INFO", format_="text")


def test_configure_logging_text_installs_text_formatter() -> None:
    configure_logging(level="INFO", format_="text")
    root = logging.getLogger()
    assert len(root.handlers) == 1
    fmt = root.handlers[0].formatter
    assert fmt is not None and not isinstance(fmt, JsonLogFormatter)


def test_configure_logging_replaces_existing_handlers() -> None:
    """Re-invocation must not cascade duplicate handlers."""
    configure_logging(level="INFO", format_="text")
    configure_logging(level="INFO", format_="json")
    configure_logging(level="INFO", format_="text")
    root = logging.getLogger()
    assert len(root.handlers) == 1
