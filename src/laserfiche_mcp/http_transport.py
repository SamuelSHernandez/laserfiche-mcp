"""Streamable HTTP transport for remote / web MCP clients.

The default stdio transport (``laserfiche-mcp`` with no flags) serves local
clients that spawn the server as a subprocess — Claude Desktop, Claude Code,
Cursor, Gemini CLI. Web and cloud clients (claude.ai custom connectors,
ChatGPT connectors) cannot spawn a local process; they connect to a URL. This
module serves the same FastMCP instance over Streamable HTTP so those clients
can reach it.

Security posture:
  * Binds to ``127.0.0.1`` by default — not reachable off the machine. Exposing
    it to a network is an explicit opt-in via ``LF_HTTP_HOST``.
  * Optional static bearer token (``LF_HTTP_AUTH_TOKEN``). When set, every
    request must carry ``Authorization: Bearer <token>`` or gets 401.
  * Binding to a non-loopback host *without* a token logs a loud warning: an
    unauthenticated Laserfiche bridge on a routable interface is a data-exposure
    risk. This server speaks plain HTTP — TLS is expected to be terminated by a
    reverse proxy in front of it.
"""

from __future__ import annotations

import hmac
import logging
from typing import TYPE_CHECKING

from .config import Settings

if TYPE_CHECKING:
    from starlette.applications import Starlette
    from starlette.middleware.base import BaseHTTPMiddleware

logger = logging.getLogger("laserfiche_mcp")

# Hosts that are only reachable from the local machine. Binding to any of these
# means an omitted auth token is a convenience, not a network exposure.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def is_loopback(host: str) -> bool:
    """True when ``host`` is only reachable from the local machine."""
    return host in _LOOPBACK_HOSTS


def _build_auth_middleware(token: str) -> type[BaseHTTPMiddleware]:
    """Return a Starlette middleware class enforcing a static bearer token.

    The comparison is constant-time so a caller can't recover the token by
    timing 401 responses.
    """
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    class BearerTokenMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):  # type: ignore[no-untyped-def]
            header = request.headers.get("authorization", "")
            scheme, _, presented = header.partition(" ")
            if scheme.lower() != "bearer" or not hmac.compare_digest(presented, token):
                return JSONResponse(
                    {
                        "error": "unauthorized",
                        "detail": "Missing or invalid bearer token.",
                    },
                    status_code=401,
                )
            return await call_next(request)

    return BearerTokenMiddleware


def build_http_app(settings: Settings) -> Starlette:
    """Configure the FastMCP instance and return the Streamable HTTP ASGI app.

    Separated from :func:`run_http` so the wiring (host/port/path binding, auth
    guard, exposure warning) is testable without starting a real server.
    """
    # Lazy: importing server triggers tool registration against the shared
    # FastMCP instance. Keeping it out of module scope mirrors cli.py.
    from .server import mcp

    mcp.settings.host = settings.http_host
    mcp.settings.port = settings.http_port
    mcp.settings.streamable_http_path = settings.http_path

    # Auth precedence: OAuth (per-user) > static bearer token > none.
    if settings.oauth_enabled:
        auth_mode = _configure_oauth(mcp, settings)
    elif settings.http_auth_token is not None:
        token = settings.http_auth_token.get_secret_value()
        mcp.settings.auth = None  # ensure FastMCP's own auth wiring stays off
        app = mcp.streamable_http_app()
        app.add_middleware(_build_auth_middleware(token))
        _log_serving(settings, "static bearer token")
        return app
    else:
        auth_mode = "none"
        mcp.settings.auth = None
        if not is_loopback(settings.http_host):
            logger.warning(
                "laserfiche-mcp is binding to %s (not loopback) with NO "
                "authentication (no LF_HTTP_OAUTH_ISSUER, no LF_HTTP_AUTH_TOKEN). "
                "The repository bridge is reachable by anything that can route to "
                "this host. Configure OAuth (or at least LF_HTTP_AUTH_TOKEN) and "
                "terminate TLS at a reverse proxy before exposing it to a network.",
                settings.http_host,
            )

    app = mcp.streamable_http_app()
    _log_serving(settings, auth_mode)
    return app


def _configure_oauth(mcp: object, settings: Settings) -> str:
    """Attach the OAuth Resource Server verifier + metadata to the FastMCP app.

    FastMCP reads ``settings.auth`` and ``_token_verifier`` when it builds the
    Streamable HTTP app, so injecting them here (rather than at construction)
    lets the shared singleton stay auth-free until --http with OAuth is used.
    """
    from mcp.server.auth.settings import AuthSettings

    from .oauth import build_token_verifier

    verifier = build_token_verifier(settings)
    # AuthSettings coerces str -> AnyHttpUrl at runtime; the ignores are just for
    # the annotated-URL parameter types.
    mcp.settings.auth = AuthSettings(  # type: ignore[attr-defined]
        issuer_url=str(settings.http_oauth_issuer),  # type: ignore[arg-type]
        resource_server_url=str(settings.http_public_url),  # type: ignore[arg-type]
        required_scopes=settings.oauth_required_scopes or None,
    )
    # Both must be set together — FastMCP only validates this pairing at
    # construction, which we bypass by injecting post-hoc.
    mcp._token_verifier = verifier  # type: ignore[attr-defined]
    return f"OAuth (issuer {settings.http_oauth_issuer})"


def _log_serving(settings: Settings, auth_mode: str) -> None:
    logger.info(
        "laserfiche-mcp Streamable HTTP on http://%s:%d%s (auth: %s)",
        settings.http_host,
        settings.http_port,
        settings.http_path,
        auth_mode,
    )


def run_http(settings: Settings) -> None:  # pragma: no cover - blocking I/O
    """Serve the MCP over Streamable HTTP. Blocks until interrupted."""
    import uvicorn

    app = build_http_app(settings)
    try:
        uvicorn.run(
            app,
            host=settings.http_host,
            port=settings.http_port,
            log_level=settings.log_level.lower(),
        )
    except KeyboardInterrupt:
        logger.info("laserfiche-mcp stopped.")
