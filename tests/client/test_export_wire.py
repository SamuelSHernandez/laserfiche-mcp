"""Export handling over a *real socket*, not mocks.

A small local HTTP server emulates the v2 behaviours a mock cannot: chunked
transfer, gzip, redirects, bodies cut short, dropped connections, and a
Download endpoint that refuses requests lacking credentials. The goal is that
every one of them either yields the right bytes or a ``LaserficheError`` —
never a raw httpx exception, a silent empty "success", or a leaked credential.
"""

from __future__ import annotations

import gzip
import json
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from laserfiche_mcp.client import LaserficheError
from laserfiche_mcp.config import Settings
from tests.client.conftest import _build_client

_API = "/LFRepositoryAPI/v2/Repositories/demo"
_BODY = b"OCR text of a scanned page.\n" * 50
_JSON_CT = "application/json;odata.metadata=minimal"

Handler = Callable[[BaseHTTPRequestHandler], None]


class _Server:
    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Handler] = {}
        self.seen: list[tuple[str, str, dict[str, str]]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_: object) -> None:  # silence
                pass

            def _dispatch(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                if length:
                    self.rfile.read(length)
                path = self.path.split("?")[0]
                outer.seen.append(
                    (self.command, self.path, {k.lower(): v for k, v in self.headers.items()})
                )
                handler = outer.routes.get((self.command, path))
                if handler is None:
                    self.send_response(404)
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return
                handler(self)

            do_GET = do_POST = _dispatch  # noqa: N815

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _send(h: BaseHTTPRequestHandler, body: bytes, ct: str, status: int = 200, **hdr: str) -> None:
    h.send_response(status)
    h.send_header("content-type", ct)
    h.send_header("content-length", str(len(body)))
    for k, v in hdr.items():
        h.send_header(k.replace("_", "-"), v)
    h.end_headers()
    h.wfile.write(body)


def _chunked(h: BaseHTTPRequestHandler, body: bytes, ct: str) -> None:
    h.send_response(200)
    h.send_header("content-type", ct)
    h.send_header("transfer-encoding", "chunked")
    h.end_headers()
    mid = max(1, len(body) // 2)
    for part in (body[:mid], body[mid:]):
        h.wfile.write(f"{len(part):x}\r\n".encode() + part + b"\r\n")
    h.wfile.write(b"0\r\n\r\n")


def _pointer_to(srv: _Server, path: str, *, advertise: str | None = None) -> bytes:
    host = advertise or srv.base
    return json.dumps(
        {"@odata.context": f"{host}/$metadata#String", "value": f"{host}{path}"}
    ).encode()


@pytest.fixture
def srv() -> Iterator[_Server]:
    server = _Server()
    yield server
    server.stop()


@pytest.fixture
def settings(srv: _Server, lf_env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> Settings:
    monkeypatch.setenv("LF_REPO_API_URL", f"{srv.base}/LFRepositoryAPI/")
    monkeypatch.setenv("LF_API_VERSION", "v2")
    monkeypatch.setenv("LF_RETRY_ATTEMPTS", "0")
    return Settings()  # type: ignore[call-arg]


def _export(srv: _Server, handler: Handler) -> None:
    srv.routes[("POST", f"{_API}/Entries/42/Export")] = handler


def _download(srv: _Server, handler: Handler, name: str = "tok") -> str:
    path = f"{_API}/Download/{name}"
    srv.routes[("GET", path)] = handler
    return path


async def _all_three(settings: Settings, tmp_path: Path) -> list[object]:
    """Run buffered, streamed and probe paths; return what each produced."""
    out: list[object] = []
    async with _build_client(settings) as c:
        for name, call in (
            ("buffered", lambda: c.export_entry(42, part="Text")),
            ("probe", lambda: c.export_entry_meta_only(42, part="Text")),
            ("file", lambda: c.export_entry_to_file(42, tmp_path / "o.bin", part="Text")),
        ):
            try:
                out.append(await call())
            except LaserficheError as exc:
                out.append(exc)
            except Exception as exc:  # noqa: BLE001
                pytest.fail(f"{name}: raw {type(exc).__name__} escaped: {exc!r}")
    return out


# --- the happy path, on the wire --------------------------------------------


@pytest.mark.asyncio
async def test_pointer_followed_over_real_http_with_credentials(
    srv: _Server, settings: Settings, tmp_path: Path
) -> None:
    path = _download(
        srv,
        lambda h: (
            _send(h, _BODY, "text/plain")
            if h.headers.get("authorization")
            else _send(h, b"no", "text/plain", 401)
        ),
    )
    _export(srv, lambda h: _send(h, _pointer_to(srv, path), _JSON_CT))

    buffered, probe, to_file = await _all_three(settings, tmp_path)

    assert buffered == _BODY
    assert probe == (len(_BODY), "text/plain")
    assert to_file[0] == len(_BODY)  # type: ignore[index]
    assert (tmp_path / "o.bin").read_bytes() == _BODY
    downloads = [s for s in srv.seen if s[1].startswith(f"{_API}/Download/")]
    assert downloads and all(d[2].get("authorization") == "Bearer test-token" for d in downloads)


@pytest.mark.asyncio
async def test_chunked_pointer_and_chunked_download(
    srv: _Server, settings: Settings, tmp_path: Path
) -> None:
    path = _download(srv, lambda h: _chunked(h, _BODY, "text/plain"))
    _export(srv, lambda h: _chunked(h, _pointer_to(srv, path), _JSON_CT))

    buffered, probe, to_file = await _all_three(settings, tmp_path)

    assert buffered == _BODY
    assert probe == (None, "text/plain")  # no Content-Length declared — handled, not crashed
    assert (tmp_path / "o.bin").read_bytes() == _BODY


@pytest.mark.asyncio
async def test_gzip_encoded_pointer_and_download(
    srv: _Server, settings: Settings, tmp_path: Path
) -> None:
    path = _download(
        srv, lambda h: _send(h, gzip.compress(_BODY), "text/plain", content_encoding="gzip")
    )
    _export(
        srv,
        lambda h: _send(
            h, gzip.compress(_pointer_to(srv, path)), _JSON_CT, content_encoding="gzip"
        ),
    )

    buffered, _, _ = await _all_three(settings, tmp_path)

    assert buffered == _BODY
    assert (tmp_path / "o.bin").read_bytes() == _BODY


@pytest.mark.asyncio
async def test_pointer_advertising_internal_hostname_still_works_on_the_wire(
    srv: _Server, settings: Settings, tmp_path: Path
) -> None:
    path = _download(srv, lambda h: _send(h, _BODY, "text/plain"))
    _export(
        srv,
        lambda h: _send(
            h, _pointer_to(srv, path, advertise="http://lf-internal.invalid:9"), _JSON_CT
        ),
    )

    buffered, _, _ = await _all_three(settings, tmp_path)

    assert buffered == _BODY  # would hang/err if it tried the advertised host


# --- failure modes: every one must be a LaserficheError ---------------------


@pytest.mark.asyncio
async def test_download_without_credentials_is_refused_not_silently_empty(
    srv: _Server, settings: Settings, tmp_path: Path
) -> None:
    path = _download(srv, lambda h: _send(h, b"unauthorized", "text/plain", 401))
    _export(srv, lambda h: _send(h, _pointer_to(srv, path), _JSON_CT))

    results = await _all_three(settings, tmp_path)

    assert all(isinstance(r, LaserficheError) and r.status_code == 401 for r in results), results
    assert not (tmp_path / "o.bin").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 307])
async def test_redirect_from_download_is_an_error_not_an_empty_body(
    srv: _Server, settings: Settings, tmp_path: Path, status: int
) -> None:
    path = _download(srv, lambda h: _send(h, b"", "text/html", status, location="/login"))
    _export(srv, lambda h: _send(h, _pointer_to(srv, path), _JSON_CT))

    results = await _all_three(settings, tmp_path)

    assert all(isinstance(r, LaserficheError) and "redirect" in str(r) for r in results), results
    assert not (tmp_path / "o.bin").exists()


@pytest.mark.asyncio
async def test_redirect_from_export_itself_is_an_error(
    srv: _Server, settings: Settings, tmp_path: Path
) -> None:
    """Typical of http->https or a proxy login page: used to return b'' silently."""
    _export(srv, lambda h: _send(h, b"", "text/html", 302, location="https://sso.invalid/"))

    results = await _all_three(settings, tmp_path)

    assert all(isinstance(r, LaserficheError) and "redirect" in str(r) for r in results), results


@pytest.mark.asyncio
async def test_download_truncated_mid_body_is_a_laserfiche_error(
    srv: _Server, settings: Settings, tmp_path: Path
) -> None:
    def short(h: BaseHTTPRequestHandler) -> None:
        h.send_response(200)
        h.send_header("content-type", "text/plain")
        h.send_header("content-length", str(len(_BODY) * 10))  # promise far more
        h.end_headers()
        h.wfile.write(_BODY)
        h.wfile.flush()
        h.close_connection = True  # then hang up

    path = _download(srv, short)
    _export(srv, lambda h: _send(h, _pointer_to(srv, path), _JSON_CT))

    buffered, _probe, to_file = await _all_three(settings, tmp_path)

    assert isinstance(buffered, LaserficheError)
    assert isinstance(to_file, LaserficheError)
    assert not (tmp_path / "o.bin").exists()
    assert not (tmp_path / "o.bin.part").exists()


@pytest.mark.asyncio
async def test_export_connection_dropped_mid_pointer_is_a_laserfiche_error(
    srv: _Server, settings: Settings, tmp_path: Path
) -> None:
    def drop(h: BaseHTTPRequestHandler) -> None:
        h.send_response(200)
        h.send_header("content-type", _JSON_CT)
        h.send_header("content-length", "120")
        h.end_headers()
        h.wfile.write(b'{"value": "http://')  # cut short
        h.wfile.flush()
        h.close_connection = True

    _export(srv, drop)

    results = await _all_three(settings, tmp_path)

    assert all(isinstance(r, LaserficheError) for r in results), results


@pytest.mark.asyncio
async def test_download_server_error_is_a_laserfiche_error(
    srv: _Server, settings: Settings, tmp_path: Path
) -> None:
    path = _download(srv, lambda h: _send(h, b'{"title":"boom"}', "application/json", 500))
    _export(srv, lambda h: _send(h, _pointer_to(srv, path), _JSON_CT))

    results = await _all_three(settings, tmp_path)

    assert all(isinstance(r, LaserficheError) and r.status_code == 500 for r in results), results


@pytest.mark.asyncio
async def test_download_connection_refused_is_a_laserfiche_error(
    srv: _Server, settings: Settings, tmp_path: Path
) -> None:
    # Pointer path has no route on the server -> 404, not a hang.
    _export(srv, lambda h: _send(h, _pointer_to(srv, f"{_API}/Download/missing"), _JSON_CT))

    results = await _all_three(settings, tmp_path)

    assert all(isinstance(r, LaserficheError) and r.status_code == 404 for r in results), results


@pytest.mark.asyncio
async def test_pointer_to_non_http_scheme_never_leaves_the_machine(
    srv: _Server, settings: Settings, tmp_path: Path
) -> None:
    _export(
        srv,
        lambda h: _send(h, json.dumps({"value": "file:///C:/Windows/win.ini"}).encode(), _JSON_CT),
    )

    results = await _all_three(settings, tmp_path)

    assert all(
        isinstance(r, LaserficheError) and "unsupported scheme" in str(r) for r in results
    ), results


# --- empty bodies: the "scanned doc, no edoc" signature on a real v1 server --


@pytest.mark.asyncio
async def test_empty_200_body_is_returned_as_empty_and_probe_reports_zero(
    srv: _Server, settings: Settings, tmp_path: Path
) -> None:
    """Client layer stays faithful (empty is empty); the tool layer turns it into
    a structured ``no_electronic_document`` error — see tests/tools."""

    def empty(h: BaseHTTPRequestHandler) -> None:
        h.send_response(200)
        h.send_header("content-length", "0")
        h.end_headers()

    _export(srv, empty)

    buffered, probe, to_file = await _all_three(settings, tmp_path)

    assert buffered == b""
    assert probe == (0, None)
    assert to_file[0] == 0  # type: ignore[index]
