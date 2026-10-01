"""Core transport for :class:`LaserficheClient` — auth, retry, request helpers.

Holds the per-instance state (``_settings``, ``_auth``, ``_http``, schema
caches) and the request primitives (``_send``, ``_request_json``,
``_request_bytes``, ``_request_bytes_with_meta``, ``_repo_path``). The
resource mixins in this package extend ``_CoreClient`` so their methods
can call those primitives directly.
"""

from __future__ import annotations

import asyncio
import hashlib
import json as jsonlib
import logging
import random
import warnings
from pathlib import Path
from typing import Any, TypeVar, cast
from urllib.parse import urljoin

import httpx

from ..auth import AuthStrategy
from ..config import ApiVersion, Settings
from ..errors import LaserficheError
from ..observability import redact

logger = logging.getLogger("laserfiche_mcp.client")

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}

# A v2 ``POST /Entries/{id}/Export`` can answer with a small JSON pointer
# (``{"@odata.context": ..., "value": "<download url>"}``) instead of the file
# body — seen for entries with no electronic document. The pointer is tiny;
# anything bigger is a real JSON document and must not be treated as one.
_POINTER_MAX_BYTES = 8192
_POINTER_KEYS = frozenset({"@odata.context", "value"})


def _is_json_content_type(content_type: str | None) -> bool:
    if not content_type:
        return False
    mime = content_type.split(";")[0].strip().lower()
    return mime == "application/json" or mime.endswith("+json")


def _extract_download_pointer(content: bytes, content_type: str | None) -> str | None:
    """Return the Download URL if ``content`` is a v2 Export pointer, else None.

    Deliberately strict (JSON content-type, small, an object whose only keys
    are ``@odata.context``/``value``, ``value`` a non-empty string) so a
    genuine JSON edoc is never mistaken for a pointer.
    """
    if not _is_json_content_type(content_type) or len(content) > _POINTER_MAX_BYTES:
        return None
    try:
        data = jsonlib.loads(content)
    except ValueError:
        return None
    if not isinstance(data, dict) or "value" not in data or not set(data) <= _POINTER_KEYS:
        return None
    value = data["value"]
    return value if isinstance(value, str) and value.strip() else None


# Ceiling on a single retry's delay, regardless of attempt count — without
# it, LF_RETRY_ATTEMPTS=10's uncapped exponential backoff (2^0 + 2^1 + ...
# + 2^9 seconds) can turn one failing call into a ~17-minute wait.
_MAX_RETRY_DELAY_SECONDS = 30.0


def _retry_delay(attempt: int, *, retry_after: str | None = None) -> float:
    """Backoff delay for retry ``attempt`` (0-indexed).

    Honors a server-supplied ``Retry-After`` (seconds form only — an
    HTTP-date value is rare enough here, and stale enough by the time it'd
    matter, that falling back to the capped exponential default is fine)
    over the exponential default. Otherwise: capped exponential backoff
    with jitter (50%-100% of the capped value), so many concurrent callers
    retrying the same transient failure don't all wake up and hammer the
    server in lockstep.
    """
    if retry_after is not None:
        try:
            return min(float(retry_after), _MAX_RETRY_DELAY_SECONDS)
        except ValueError:
            pass
    base = min(float(2**attempt), _MAX_RETRY_DELAY_SECONDS)
    return base * (0.5 + random.random() / 2)


_CacheValueT = TypeVar("_CacheValueT")
# Bound to ``_CoreClient`` so subclasses' ``__aenter__`` reports the concrete
# subclass type (e.g. ``LaserficheClient``) instead of ``_CoreClient``.
_SelfCore = TypeVar("_SelfCore", bound="_CoreClient")


def build_repo_path(
    base_url: str,
    repository_id: str,
    suffix: str,
    api_version: ApiVersion = ApiVersion.V1,
) -> str:
    """Construct a /{api_version}/Repositories/{repo}/{suffix} URL.

    Pulled out of ``_CoreClient`` so it's directly unit-testable.
    """
    if not base_url.endswith("/"):
        base_url += "/"
    suffix = suffix.lstrip("/")
    return urljoin(base_url, f"{api_version.value}/Repositories/{repository_id}/{suffix}")


class _CoreClient:
    """Transport core. Not used directly — composed into ``LaserficheClient``."""

    def __init__(self, settings: Settings, auth: AuthStrategy) -> None:
        self._settings = settings
        self._auth = auth
        self._base_url = str(settings.repo_api_url) if settings.repo_api_url else ""
        self._repository_id = settings.repository_id or ""
        self._api_version = settings.api_version
        self._http: httpx.AsyncClient | None = None

        # Schema-definition caches for client-side pre-flight validation.
        # Each cache stores (value, expiry_monotonic). TTL is taken from
        # settings.schema_cache_ttl_seconds at lookup time so the env var
        # can be tuned without recreating the client.
        self._field_def_cache: tuple[dict[str, Any], float] | None = None
        self._tag_def_cache: tuple[dict[str, Any], float] | None = None
        self._template_def_cache: tuple[dict[str, Any], float] | None = None
        # Keyed by linkTypeId (int), unlike the other caches which are keyed
        # by name. See ``_DefinitionsMixin.cached_link_definitions``.
        self._link_def_cache: tuple[dict[int, dict[str, Any]], float] | None = None

        if not settings.verify_ssl:
            warnings.warn(
                "TLS certificate verification is DISABLED (LF_VERIFY_SSL=false). "
                "This is insecure outside of trusted internal dev environments.",
                stacklevel=2,
            )

    async def __aenter__(self: _SelfCore) -> _SelfCore:
        self._http = httpx.AsyncClient(
            timeout=self._settings.request_timeout_seconds,
            verify=self._settings.verify_ssl,
            headers={"Accept": "application/json"},
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # --- Path + transport --------------------------------------------------

    def _repo_path(self, suffix: str) -> str:
        return build_repo_path(self._base_url, self._repository_id, suffix, self._api_version)

    def _redact_url(self, url: httpx.URL) -> str:
        """Return ``url`` with the configured host and repo-id replaced.

        Used by retry warnings so the WARNING-level log output doesn't carry
        a deployment fingerprint (hostname + repository ID) into shared log
        aggregators. The path and query string survive untouched so
        operators can still see which endpoint was retried.
        """
        host = self._settings.repo_api_url.host if self._settings.repo_api_url else None
        return cast(str, redact(str(url), host=host, repo_id=self._repository_id or None))

    async def _send(self, request: httpx.Request) -> httpx.Response:
        """Apply auth, send, and retry on transient failures."""
        if self._http is None:
            raise RuntimeError("LaserficheClient must be used as an async context manager.")

        attempts = max(1, self._settings.retry_attempts + 1)
        last_exc: Exception | None = None

        for attempt in range(attempts):
            try:
                await self._auth.apply(request)
                response = await self._http.send(request)
            # TimeoutException is a sibling of ConnectError under TransportError,
            # not a subclass — without it a cold-starting server's first request
            # failed outright instead of being retried.
            except (
                httpx.ConnectError,
                httpx.ReadError,
                httpx.RemoteProtocolError,
                httpx.TimeoutException,
            ) as exc:
                last_exc = exc
                if attempt + 1 >= attempts:
                    break
                delay = _retry_delay(attempt)
                logger.warning(
                    "Network error on %s %s (attempt %d/%d): %s; retrying in %.1fs",
                    request.method,
                    self._redact_url(request.url),
                    attempt + 1,
                    attempts,
                    exc,
                    delay,
                )
                await asyncio.sleep(delay)
                continue

            if response.status_code in _RETRYABLE_STATUS and attempt + 1 < attempts:
                delay = _retry_delay(attempt, retry_after=response.headers.get("retry-after"))
                logger.warning(
                    "Retryable status %d on %s %s (attempt %d/%d); retrying in %.1fs",
                    response.status_code,
                    request.method,
                    self._redact_url(request.url),
                    attempt + 1,
                    attempts,
                    delay,
                )
                await asyncio.sleep(delay)
                continue

            return response

        raise LaserficheError(
            f"Network error after {attempts} attempt(s): {last_exc}",
        ) from last_exc

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._http is None:
            raise RuntimeError("LaserficheClient must be used as an async context manager.")

        request = self._http.build_request(method, url, params=params, json=json)
        response = await self._send(request)

        if response.status_code >= 400:
            try:
                detail = response.json()
            except ValueError:
                detail = response.text
            raise LaserficheError(
                f"Laserfiche API error {response.status_code}: {detail}",
                status_code=response.status_code,
                detail=detail,
            )

        if not response.content:
            return {}
        try:
            return cast(dict[str, Any], response.json())
        except ValueError as exc:
            raise LaserficheError(
                f"Laserfiche API returned a 2xx response for {method} {url} with an "
                f"unparseable body: {exc!r}"
            ) from exc

    async def _request_bytes(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> bytes:
        content, _ = await self._request_bytes_with_meta(method, url, json=json)
        return content

    async def _request_bytes_with_meta(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        follow_download_pointer: bool = False,
    ) -> tuple[bytes, str | None]:
        """Like ``_request_bytes`` but also surfaces the response Content-Type.

        Needed by edoc modes that branch on document type (PDF vs text vs
        binary) instead of trusting the file extension on the entry.

        With ``follow_download_pointer``, a v2 Export pointer response is
        followed with one authenticated GET and the real body is returned.
        """
        if self._http is None:
            raise RuntimeError("LaserficheClient must be used as an async context manager.")

        request = self._http.build_request(method, url, json=json)
        response = await self._send(request)
        if response.status_code >= 400:
            try:
                detail = response.json()
            except ValueError:
                detail = response.text
            raise LaserficheError(
                f"Laserfiche API error {response.status_code}: {detail}",
                status_code=response.status_code,
                detail=detail,
            )
        if 300 <= response.status_code < 400:
            raise self._redirect_error(response)
        content_type = response.headers.get("content-type")
        if follow_download_pointer:
            pointer = _extract_download_pointer(response.content, content_type)
            if pointer is not None:
                target = self._resolve_download_url(pointer, request.url)
                followed = await self._send(self._download_request(target))
                if followed.status_code >= 400:
                    try:
                        detail = followed.json()
                    except ValueError:
                        detail = followed.text
                    raise LaserficheError(
                        f"Laserfiche API error {followed.status_code}: {detail}",
                        status_code=followed.status_code,
                        detail=detail,
                    )
                if 300 <= followed.status_code < 400:
                    raise self._redirect_error(followed)
                return followed.content, followed.headers.get("content-type")
        return response.content, content_type

    def _redirect_error(self, response: httpx.Response) -> LaserficheError:
        """A 3xx on a download is never a body — fail loudly instead of returning b""."""
        location = response.headers.get("location", "<none>")
        return LaserficheError(
            f"Laserfiche returned an unexpected redirect ({response.status_code}) for "
            f"{response.request.method} {self._redact_url(response.request.url)} "
            f"-> {location}. Downloads are not redirected; check the configured "
            "LF_REPO_API_URL (http vs https, trailing path, or a proxy login page).",
            status_code=response.status_code,
        )

    def _resolve_download_url(self, pointer: str, request_url: httpx.URL | str) -> httpx.URL:
        """Turn a pointer's ``value`` into the URL we will actually fetch.

        Only the path and query are taken from the server's URL; scheme, host
        and port always come from the configured API URL. That survives a
        server advertising its internal hostname behind a reverse proxy, and
        guarantees the Authorization header is never sent to a host the
        operator did not configure.
        """
        joined = httpx.URL(urljoin(str(request_url), pointer.strip()))
        if joined.scheme not in ("http", "https"):
            raise LaserficheError(
                f"Laserfiche Export returned a download pointer with an unsupported "
                f"scheme {joined.scheme!r}."
            )
        base = httpx.URL(self._base_url) if self._base_url else httpx.URL(str(request_url))
        # An empty query must be omitted entirely, or httpx emits a dangling "?".
        return base.copy_with(path=joined.path, query=joined.query or None, fragment=None)

    def _download_request(self, url: httpx.URL) -> httpx.Request:
        assert self._http is not None
        # The client default is ``Accept: application/json``; the Download
        # endpoint serves file bytes, so accept anything.
        return self._http.build_request("GET", url, headers={"Accept": "*/*"})

    async def _open_stream(self, request: httpx.Request, *, label: str) -> httpx.Response:
        """Send ``request`` streamed with auth, raising on HTTP errors.

        Returns an open response; the caller must close it.
        """
        assert self._http is not None
        await self._auth.apply(request)
        try:
            response = await self._http.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise LaserficheError(
                f"Network error {label} {request.method} {self._redact_url(request.url)}: {exc!r}"
            ) from exc
        if response.status_code >= 400:
            try:
                # Error bodies are small — read this one so the message is useful.
                await response.aread()
                try:
                    detail: object = response.json()
                except ValueError:
                    detail = response.text
            finally:
                await response.aclose()
            raise LaserficheError(
                f"Laserfiche API error {response.status_code}: {detail}",
                status_code=response.status_code,
                detail=detail,
            )
        if 300 <= response.status_code < 400:
            error = self._redirect_error(response)
            await response.aclose()
            raise error
        return response

    async def _open_export_stream(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None,
        label: str,
        follow_download_pointer: bool,
    ) -> httpx.Response:
        """Open a streamed response, transparently following a v2 download pointer.

        Returns an open, successful response positioned at the real file body.
        When the first response might be a pointer (small JSON), its body is
        peeked and a pointer is followed with one authenticated GET. Anything
        else — including a genuine small JSON document — is replayed by
        re-issuing the (read-only) request, so no body bytes are lost.
        """
        assert self._http is not None
        request = self._http.build_request(method, url, json=json)
        response = await self._open_stream(request, label=label)
        if not follow_download_pointer or not _is_json_content_type(
            response.headers.get("content-type")
        ):
            return response

        declared = response.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > _POINTER_MAX_BYTES:
            return response  # too big to be a pointer — a genuine JSON edoc

        pointer: str | None = None
        try:
            buffered = bytearray()
            async for chunk in response.aiter_bytes():
                buffered.extend(chunk)
                if len(buffered) > _POINTER_MAX_BYTES:
                    break
            else:
                pointer = _extract_download_pointer(
                    bytes(buffered), response.headers.get("content-type")
                )
        except httpx.HTTPError as exc:
            raise LaserficheError(
                f"Network error {label} {request.method} "
                f"{self._redact_url(request.url)}: connection failed while reading "
                f"the response body: {exc!r}"
            ) from exc
        finally:
            await response.aclose()

        if pointer is None:
            replay = self._http.build_request(method, url, json=json)
            return await self._open_stream(replay, label=label)

        target = self._resolve_download_url(pointer, request.url)
        return await self._open_stream(self._download_request(target), label=label)

    async def _request_meta_only(
        self,
        method: str,
        url: str,
        *,
        json: dict[str, Any] | None = None,
        follow_download_pointer: bool = False,
    ) -> tuple[int | None, str | None]:
        """Open a response, read its headers, and close without buffering the body.

        Returns ``(content_length, content_type)``; ``content_length`` is
        ``None`` when the server answers with chunked encoding and omits the
        header. Used by ``mode='info'`` so probing the size of a 400 MB edoc
        costs a request round-trip instead of a 400 MB transfer.

        This deliberately bypasses ``_send``: streaming responses can't be
        replayed through that retry loop, and a metadata probe is cheap
        enough to retry at the caller's level.
        """
        if self._http is None:
            raise RuntimeError("LaserficheClient must be used as an async context manager.")

        response = await self._open_export_stream(
            method,
            url,
            json=json,
            label="probing",
            follow_download_pointer=follow_download_pointer,
        )
        try:
            raw_length = response.headers.get("content-length")
            length = int(raw_length) if raw_length is not None and raw_length.isdigit() else None
            if length is None:
                # No Content-Length (chunked, or an empty reply that omits it —
                # what a real v1 server sends for a scanned entry with no
                # edoc). Peek at the first bytes so "empty" is reported as 0
                # rather than "unknown"; a non-empty body stays None.
                try:
                    async for chunk in response.aiter_bytes():
                        if chunk:
                            break
                    else:
                        length = 0
                except httpx.HTTPError as exc:
                    raise LaserficheError(
                        f"Network error probing {method} "
                        f"{self._redact_url(httpx.URL(url))}: connection failed while "
                        f"reading the response body: {exc!r}"
                    ) from exc
            return length, response.headers.get("content-type")
        finally:
            await response.aclose()

    # --- Cache helper (used by _DefinitionsMixin) -------------------------

    def _cache_alive(
        self,
        entry: tuple[_CacheValueT, float] | None,
    ) -> _CacheValueT | None:
        """Return the cached value if not expired, else None."""
        import time

        if entry is None:
            return None
        value, expiry = entry
        if time.monotonic() >= expiry:
            return None
        return value

    async def _request_stream_to_file(
        self,
        method: str,
        url: str,
        dest: Path,
        *,
        json: dict[str, Any] | None = None,
        max_bytes: int | None = None,
        chunk_size: int = 65536,
        follow_download_pointer: bool = False,
    ) -> tuple[int, str | None, str]:
        """Stream a response body straight to disk. Returns (bytes, type, sha256).

        The point is that the body never lands in memory — a 400 MB edoc costs
        one 64 KB buffer, not 400 MB of RSS, and never passes through a tool
        result. ``_request_bytes_with_meta`` buffers the whole response and is
        the wrong primitive for anything large.

        Writes to a ``.part`` sibling and renames on success, so a failed or
        aborted transfer never leaves a truncated file that looks complete.

        ``max_bytes`` is enforced twice: once against Content-Length before a
        single byte is written, and again against the running total for servers
        that stream without declaring a length. Exceeding it raises
        ``LaserficheError`` with ``size_exceeds_cap`` in the message and
        deletes the partial file.

        Like ``_request_meta_only``, this bypasses ``_send``'s retry loop —
        a partially-consumed stream can't be replayed.
        """
        if self._http is None:
            raise RuntimeError("LaserficheClient must be used as an async context manager.")

        response = await self._open_export_stream(
            method,
            url,
            json=json,
            label="streaming",
            follow_download_pointer=follow_download_pointer,
        )

        partial = dest.with_name(dest.name + ".part")
        digest = hashlib.sha256()
        written = 0

        try:
            content_type = response.headers.get("content-type")
            declared = response.headers.get("content-length")
            if (
                max_bytes is not None
                and declared is not None
                and declared.isdigit()
                and int(declared) > max_bytes
            ):
                raise LaserficheError(
                    f"size_exceeds_cap: edoc is {int(declared)} bytes, over the "
                    f"{max_bytes}-byte cap. Raise max_bytes to download it."
                )

            dest.parent.mkdir(parents=True, exist_ok=True)
            with partial.open("wb") as handle:
                async for chunk in response.aiter_bytes(chunk_size):
                    written += len(chunk)
                    if max_bytes is not None and written > max_bytes:
                        raise LaserficheError(
                            f"size_exceeds_cap: transfer passed the {max_bytes}-byte "
                            "cap (server declared no Content-Length). Raise max_bytes "
                            "to download it."
                        )
                    digest.update(chunk)
                    handle.write(chunk)
        except httpx.HTTPError as exc:
            # The connection dropped or the body was cut short mid-download
            # (e.g. RemoteProtocolError on a Content-Length mismatch). Callers
            # only guard LaserficheError, so don't let httpx's types escape.
            partial.unlink(missing_ok=True)
            raise LaserficheError(
                f"Network error streaming {method} {self._redact_url(httpx.URL(url))}: "
                f"connection failed mid-download: {exc!r}"
            ) from exc
        except OSError as exc:
            # Local disk failure (AV lock, full/locked scratch dir) — distinct
            # from the network/HTTP failures above, but callers only guard
            # against LaserficheError, so an uncaught OSError here escapes
            # the structured error contract entirely.
            partial.unlink(missing_ok=True)
            raise LaserficheError(
                f"Local disk error writing {method} {self._redact_url(httpx.URL(url))} "
                f"to {dest}: {exc!r}"
            ) from exc
        except BaseException:
            partial.unlink(missing_ok=True)
            raise
        finally:
            await response.aclose()

        partial.replace(dest)
        return written, content_type, digest.hexdigest()
