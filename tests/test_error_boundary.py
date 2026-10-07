"""The error-boundary contract: no tool leaks a raw exception or a secret.

Parametrized over EVERY registered tool and a range of failure types, so a new
tool that forgets to handle a failure fails here instead of reaching a user as
``Error executing tool <name>: <raw text>``.
"""

from __future__ import annotations

import inspect
import typing
from collections.abc import AsyncIterator
from typing import Any, get_args, get_origin

import httpx
import pytest

import laserfiche_mcp.client._core as core
from laserfiche_mcp import _app, errors, server
from laserfiche_mcp.auth import AuthStrategy
from laserfiche_mcp.client import LaserficheClient
from laserfiche_mcp.errors import LaserficheError
from laserfiche_mcp.observability import tool_logger
from laserfiche_mcp.safety import safe_tool, scrub_secrets
from laserfiche_mcp.tools._registry import all_tools

CANONICAL_KINDS = {
    "not_found",
    "permission_denied",
    "rate_limited",
    "invalid_input",
    "upstream_unavailable",
}
_REQ = httpx.Request("GET", "https://lf.example.test/x")


class _NoAuth(AuthStrategy):
    async def apply(self, request: httpx.Request) -> None:
        return None


def _sample(annotation: Any) -> Any:
    """A plausible argument for a required parameter, from its annotation."""
    origin = get_origin(annotation)
    if origin is typing.Annotated:
        return _sample(get_args(annotation)[0])
    if origin is typing.Literal:
        return get_args(annotation)[0]
    if origin in (typing.Union, getattr(__import__("types"), "UnionType", None)):
        non_none = [a for a in get_args(annotation) if a is not type(None)]
        return _sample(non_none[0]) if non_none else None
    if origin in (list, typing.List):  # noqa: UP006
        return []
    if origin in (dict, typing.Dict):  # noqa: UP006
        return {}
    if annotation is bool:
        return False
    if annotation is int:
        return 1
    if annotation is float:
        return 1.0
    if annotation is str:
        return "x"
    return "x"


def _required_kwargs(fn: Any) -> dict[str, Any]:
    hints = typing.get_type_hints(fn, include_extras=True)
    kwargs: dict[str, Any] = {}
    for name, param in inspect.signature(fn).parameters.items():
        if param.default is inspect.Parameter.empty:
            kwargs[name] = _sample(hints.get(name, str))
    return kwargs


FAILURES: dict[str, Any] = {
    "WriteError": lambda: httpx.WriteError("boom", request=_REQ),
    "DecodingError": lambda: httpx.DecodingError("bad gzip", request=_REQ),
    "TooManyRedirects": lambda: httpx.TooManyRedirects("loop", request=_REQ),
    "UnsupportedProtocol": lambda: httpx.UnsupportedProtocol("scheme", request=_REQ),
    "ProxyError": lambda: httpx.ProxyError("proxy", request=_REQ),
    "LocalProtocolError": lambda: httpx.LocalProtocolError("proto", request=_REQ),
    "ReadTimeout": lambda: httpx.ReadTimeout("slow", request=_REQ),
    "ConnectError": lambda: httpx.ConnectError("refused", request=_REQ),
    "OSError": lambda: OSError("disk /secret/path"),
    "ValueError": lambda: ValueError("odd"),
    "KeyError": lambda: KeyError("surprise"),
}


@pytest.fixture
async def failing_client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    """A real client whose transport raises whatever ``state['make']`` builds."""
    monkeypatch.setattr(core, "_retry_delay", lambda *a, **k: 0.0)
    settings = server._get_settings()
    monkeypatch.setattr(settings, "read_only", False)
    state: dict[str, Any] = {"make": lambda: ValueError("unset")}

    def handler(request: httpx.Request) -> httpx.Response:
        raise state["make"]()

    # Not `async with`: __aenter__ would build a real TLS-verifying httpx client
    # (a ~1s SSL-context load) only for us to replace it with the mock below.
    client = LaserficheClient(settings, _NoAuth())
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(_app, "get_client", lambda: client)
    try:
        yield state
    finally:
        await client._http.aclose()


def _assert_contract(result: Any, tool: str, failure: str) -> None:
    where = f"{tool} under {failure}"
    # A tool may still succeed or return its own non-error shape for some inputs
    # (e.g. a local validator rejects first); what it must never do is raise.
    if isinstance(result, dict) and result.get("mode") == "error":
        assert result.get("kind") in CANONICAL_KINDS, f"{where}: kind={result.get('kind')!r}"
        assert result.get("error"), f"{where}: no subkind"
        assert result.get("request_id"), f"{where}: no request_id"
    # Raw exception text must never be the whole story.
    text = repr(result)
    assert "Traceback" not in text, where


# One failure per distinct code path keeps the sweep (every tool x each) fast:
# generic httpx error, timeout (retry path), connect error, local I/O, and an
# arbitrary bug. The remaining FAILURES are covered on a few tools below.
SWEEP = ("WriteError", "ReadTimeout", "ConnectError", "OSError", "KeyError")


@pytest.mark.parametrize("failure", SWEEP)
@pytest.mark.parametrize("spec", all_tools(), ids=lambda s: s.legacy_name)
async def test_no_tool_leaks_a_raw_exception(
    spec: Any, failure: str, failing_client: dict[str, Any]
) -> None:
    failing_client["make"] = FAILURES[failure]
    wrapped = tool_logger(safe_tool(spec.fn))

    result = await wrapped(**_required_kwargs(spec.fn))  # must not raise

    _assert_contract(result, spec.legacy_name, failure)


async def test_unknown_exception_becomes_internal_error_without_leaking_its_text() -> None:
    async def tool(entry_id: int) -> dict[str, Any]:
        raise RuntimeError("password=hunter2 in /home/user/secret")

    tool.__name__ = "explodes"
    result = await tool_logger(safe_tool(tool))(entry_id=7)

    assert result["mode"] == "error"
    assert result["error"] == "internal_error"
    assert result["kind"] == "upstream_unavailable"
    assert result["entry_id"] == 7
    assert result["request_id"]
    assert "hunter2" not in repr(result) and "/home/user" not in repr(result)
    assert "RuntimeError" in result["reason"]  # the type, not the message


async def test_cancellation_and_exit_are_not_swallowed() -> None:
    import asyncio

    async def cancelled() -> dict[str, Any]:
        raise asyncio.CancelledError

    async def exiting() -> dict[str, Any]:
        raise SystemExit(3)

    with pytest.raises(asyncio.CancelledError):
        await safe_tool(cancelled)()
    with pytest.raises(SystemExit):
        await safe_tool(exiting)()


async def test_writes_disabled_is_a_structured_permission_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server._get_settings(), "read_only", True)
    result = await tool_logger(safe_tool(server.delete_entry))(entry_id=1)
    assert result["error"] == "writes_disabled"
    assert result["kind"] == "permission_denied"


async def test_secrets_are_scrubbed_from_error_responses(
    monkeypatch: pytest.MonkeyPatch, failing_client: dict[str, Any]
) -> None:
    password = server._get_settings().password.get_secret_value()  # type: ignore[union-attr]
    failing_client["make"] = lambda: OSError(f"cannot open C:/x?password={password}")

    async def tool() -> dict[str, Any]:
        return {
            "mode": "error",
            "error": "x",
            "kind": "invalid_input",
            "reason": f"leak {password}",
        }

    result = await safe_tool(tool)()
    assert password not in repr(result)
    assert "<redacted>" in result["reason"]


def test_scrub_secrets_ignores_short_values_and_nested_structures() -> None:
    payload = {"a": ["xx-longsecret-yy", ("t", "longsecret")], "n": 5}
    scrubbed = scrub_secrets(payload, ["longsecret", "ab"])
    assert scrubbed == {"a": ["xx-<redacted>-yy", ("t", "<redacted>")], "n": 5}
    assert scrub_secrets("hello", []) == "hello"


def test_every_new_subkind_is_mapped_to_a_canonical_kind() -> None:
    for subkind in (
        "network_error",
        "local_io_error",
        "internal_error",
        "outcome_unknown",
        "writes_disabled",
    ):
        assert errors.kind_for_subkind(subkind) in CANONICAL_KINDS


async def test_ambiguous_write_failure_is_classified_outcome_unknown(
    failing_client: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"id": 2, "name": "P", "entryType": "Folder"})
        raise httpx.ReadTimeout("reply lost", request=request)

    client = _app.get_client()
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[attr-defined]

    result = await tool_logger(safe_tool(server.copy_entry))(source_id=1, parent_id=2, name="x")

    assert result["error"] == "outcome_unknown"
    assert result["kind"] == "upstream_unavailable"
    assert "state" in result["reason"]


def test_outcome_unknown_flag_survives_classification() -> None:
    exc = LaserficheError("lost", outcome_unknown=True)
    assert errors.classify_lf_error("op", exc)["error"] == "outcome_unknown"
    assert errors.classify_lf_error("op", LaserficheError("x", status_code=404))["error"] == (
        "not_found"
    )


@pytest.mark.parametrize("failure", sorted(set(FAILURES) - set(SWEEP)))
@pytest.mark.parametrize("tool", ["get_entry", "list_folder", "delete_entry", "get_document_edoc"])
async def test_remaining_failure_types_on_representative_tools(
    tool: str, failure: str, failing_client: dict[str, Any]
) -> None:
    failing_client["make"] = FAILURES[failure]
    fn = getattr(server, tool)
    result = await tool_logger(safe_tool(fn))(**_required_kwargs(fn))
    _assert_contract(result, tool, failure)
