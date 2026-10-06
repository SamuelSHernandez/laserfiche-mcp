"""Retry policy: ambiguous failures must not replay non-idempotent writes."""

from __future__ import annotations

import httpx
import pytest

import laserfiche_mcp.client._core as core
from laserfiche_mcp.auth import AuthStrategy
from laserfiche_mcp.client import LaserficheClient
from laserfiche_mcp.config import Settings
from laserfiche_mcp.errors import LaserficheError


class _NoAuth(AuthStrategy):
    async def apply(self, request: httpx.Request) -> None:
        return None


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        repo_api_url="https://lf.example.com/LFRepositoryAPI",
        repository_id="r",
        username="u",
        password="p",
        retry_attempts=3,
    )


async def _client(handler) -> LaserficheClient:  # type: ignore[no-untyped-def]
    client = LaserficheClient(_settings(), _NoAuth())
    await client.__aenter__()
    client._http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers={"Accept": "application/json"}
    )
    return client


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(core, "_retry_delay", lambda *a, **k: 0.0)


async def test_post_not_replayed_after_read_timeout() -> None:
    calls: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req.method)
        raise httpx.ReadTimeout("reply lost", request=req)

    client = await _client(handler)
    with pytest.raises(LaserficheError, match="outcome unknown"):
        await client.copy_entry_async(5, source_id=9, name="x", auto_rename=True)
    assert calls == ["POST"]


async def test_post_not_replayed_after_503() -> None:
    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(503, json={"title": "busy"})

    client = await _client(handler)
    with pytest.raises(LaserficheError):
        await client.copy_entry_async(5, source_id=9, name="x")
    assert len(calls) == 1


async def test_post_retried_on_connect_error_and_429() -> None:
    seq = iter(["connect", 429, "ok"])

    def handler(req: httpx.Request) -> httpx.Response:
        step = next(seq)
        if step == "connect":
            raise httpx.ConnectError("refused", request=req)
        if step == 429:
            return httpx.Response(429, json={})
        return httpx.Response(201, json={"ok": True})

    client = await _client(handler)
    assert await client.copy_entry_async(5, source_id=9, name="x") == {"ok": True}


async def test_get_still_retried_on_timeout() -> None:
    seq = iter(["timeout", "ok"])

    def handler(req: httpx.Request) -> httpx.Response:
        if next(seq) == "timeout":
            raise httpx.ReadTimeout("slow", request=req)
        return httpx.Response(200, json={"id": 1})

    client = await _client(handler)
    assert await client.get_entry(1) == {"id": 1}


async def test_read_only_post_search_still_retried() -> None:
    seq = iter(["timeout", "ok"])

    def handler(req: httpx.Request) -> httpx.Response:
        if next(seq) == "timeout":
            raise httpx.ReadTimeout("slow", request=req)
        return httpx.Response(200, json={"value": []})

    client = await _client(handler)
    assert await client.search_entries("x", max_results=5) == {"value": []}


async def test_schema_cache_not_shared_between_callers_in_passthrough() -> None:
    settings = _settings().model_copy(
        update={
            "auth_mode": core.AuthMode.OAUTH_PASSTHROUGH,
            "http_oauth_issuer": "https://idp.example.com",
        }
    )
    hits: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        hits.append(1)
        return httpx.Response(200, json={"value": [{"name": f"Field{len(hits)}"}]})

    client = LaserficheClient(settings, _NoAuth())
    await client.__aenter__()
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    first = await client.cached_field_definitions()
    second = await client.cached_field_definitions()
    assert first != second  # second caller re-fetched instead of reading A's cache
    assert len(hits) == 2


async def test_schema_cache_still_used_for_service_account() -> None:
    hits: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        hits.append(1)
        return httpx.Response(200, json={"value": [{"name": "F"}]})

    client = LaserficheClient(_settings(), _NoAuth())
    await client.__aenter__()
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    await client.cached_field_definitions()
    await client.cached_field_definitions()
    assert len(hits) == 1


async def test_edoc_download_aborts_at_cap_without_content_length() -> None:
    from laserfiche_mcp.client import EdocTooLarge

    def handler(req: httpx.Request) -> httpx.Response:
        async def body():  # type: ignore[no-untyped-def]
            for _ in range(1000):  # chunked: no Content-Length declared
                yield b"x" * 1024

        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=body())

    client = await _client(handler)
    with pytest.raises(EdocTooLarge) as info:
        await client.export_entry_with_meta(7, part="Edoc", max_bytes=4096)
    assert info.value.observed < 1024 * 1000  # stopped early, not fully buffered


async def test_capped_download_retries_transient_failure_then_succeeds() -> None:
    seq = iter(["timeout", 503, "ok"])

    def handler(req: httpx.Request) -> httpx.Response:
        step = next(seq)
        if step == "timeout":
            raise httpx.ReadTimeout("slow", request=req)
        if step == 503:
            return httpx.Response(503, json={})
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"pdf")

    client = await _client(handler)
    body, ctype = await client.export_entry_with_meta(7, part="Edoc", max_bytes=1000)
    assert (body, ctype) == (b"pdf", "application/pdf")


async def test_capped_download_does_not_retry_over_cap_or_4xx() -> None:
    from laserfiche_mcp.client import EdocTooLarge

    calls: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(404, json={"title": "nope"})

    client = await _client(handler)
    with pytest.raises(LaserficheError) as info:
        await client.export_entry_with_meta(7, part="Edoc", max_bytes=1000)
    assert not isinstance(info.value, EdocTooLarge)
    assert len(calls) == 1


async def test_capped_download_v1_and_pointer_paths() -> None:
    # v1 GET path honours the cap
    def v1_handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "application/pdf"}, content=b"x" * 50)

    from laserfiche_mcp.client import EdocTooLarge

    client = await _client(v1_handler)
    assert (await client.export_entry_with_meta(7, part="Edoc", max_bytes=100))[0] == b"x" * 50
    with pytest.raises(EdocTooLarge):
        await client.export_entry_with_meta(7, part="Edoc", max_bytes=10)
