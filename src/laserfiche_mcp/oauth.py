"""OAuth 2.1 Resource Server token verification for the --http transport.

When ``LF_HTTP_OAUTH_ISSUER`` is set, the HTTP server verifies per-user bearer
tokens minted by an external authorization server (LFDS, Entra, Okta, Auth0,
Google) instead of accepting a single shared secret. This module implements the
:class:`~mcp.server.auth.provider.TokenVerifier` the MCP SDK plugs into its
bearer-auth middleware.

Design:
  * JWT access tokens, verified against the issuer's published JWKS (discovered
    from ``{issuer}/.well-known/openid-configuration`` unless an explicit JWKS
    URL is given). Signature verification uses PyJWT — we do not hand-roll it.
  * Asymmetric algorithms only (enforced in config): a Resource Server holds
    public keys, so HMAC / ``none`` are never acceptable.
  * The ``aud`` (audience) check is the anti-replay control: a token minted for
    a different service must not authenticate here.
  * ``verify_token`` returns ``None`` on *any* failure — a bad token becomes a
    clean 401, never a 500. Reasons are logged at debug so misconfig is
    diagnosable without leaking token contents.

This is authentication at the connector edge. Verified requests still reach
Laserfiche through the configured service account — see docs/remote-http.md.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import httpx
from mcp.server.auth.provider import AccessToken, TokenVerifier

from .config import Settings

if TYPE_CHECKING:
    from jwt import PyJWKClient

logger = logging.getLogger("laserfiche_mcp")

_INSTALL_HINT = (
    "OAuth Resource Server mode needs PyJWT. Install the extra: pip install 'laserfiche-mcp[oauth]'"
)


def _require_pyjwt() -> Any:
    """Import PyJWT lazily, raising a clear message if the extra is missing."""
    try:
        import jwt  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RuntimeError(_INSTALL_HINT) from exc
    return jwt


def _extract_scopes(claims: dict[str, Any]) -> list[str]:
    """Pull scopes from either the ``scope`` string or the ``scp`` claim.

    Different authorization servers disagree: OAuth's ``scope`` is a
    space-delimited string; Microsoft Entra uses ``scp`` (string or list).
    """
    raw = claims.get("scope") or claims.get("scp") or []
    if isinstance(raw, str):
        return raw.split()
    if isinstance(raw, list):
        return [str(s) for s in raw]
    return []


class JwtTokenVerifier(TokenVerifier):
    """Verifies JWT access tokens against an issuer's JWKS."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        algorithms: list[str],
        jwks_url: str | None = None,
    ) -> None:
        # Normalize trailing slash so the manual issuer check below is robust to
        # HttpUrl adding one when the token's `iss` doesn't have it.
        self._issuer = issuer.rstrip("/")
        self._audience = audience
        self._algorithms = algorithms
        self._explicit_jwks_url = jwks_url
        self._jwk_client: PyJWKClient | None = None

    async def _jwks_url(self) -> str:
        """Resolve the JWKS URL — explicit, else via OpenID discovery."""
        if self._explicit_jwks_url:
            return self._explicit_jwks_url
        discovery = f"{self._issuer}/.well-known/openid-configuration"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(discovery)
            resp.raise_for_status()
            jwks_uri = resp.json()["jwks_uri"]
        return str(jwks_uri)

    async def _client(self) -> PyJWKClient:
        """Lazily build (and cache) the PyJWKClient that fetches signing keys."""
        if self._jwk_client is None:
            jwt = _require_pyjwt()
            url = await self._jwks_url()
            # PyJWKClient caches keys and handles rotation on cache miss.
            self._jwk_client = jwt.PyJWKClient(url, cache_keys=True)
        return self._jwk_client

    async def verify_token(self, token: str) -> AccessToken | None:
        jwt = _require_pyjwt()
        try:
            client = await self._client()
            # Signing-key fetch and decode are synchronous (urllib inside PyJWT);
            # keep them off the event loop.
            signing_key = await asyncio.to_thread(client.get_signing_key_from_jwt, token)
            claims: dict[str, Any] = await asyncio.to_thread(
                jwt.decode,
                token,
                signing_key.key,
                algorithms=self._algorithms,
                audience=self._audience,
                options={"require": ["exp", "iat"], "verify_iss": False},
            )
        except Exception as exc:  # noqa: BLE001 - any failure => not authenticated
            logger.debug("token verification failed: %s", exc)
            return None

        # Manual issuer check, trailing-slash tolerant.
        token_iss = str(claims.get("iss", "")).rstrip("/")
        if token_iss != self._issuer:
            logger.debug("token issuer mismatch: %r != %r", token_iss, self._issuer)
            return None

        client_id = claims.get("client_id") or claims.get("azp") or claims.get("sub") or "unknown"
        expires_at = claims.get("exp")
        return AccessToken(
            token=token,
            client_id=str(client_id),
            scopes=_extract_scopes(claims),
            expires_at=int(expires_at) if expires_at is not None else None,
            resource=self._audience,
        )


def build_token_verifier(settings: Settings) -> TokenVerifier:
    """Construct the JWT verifier from settings. Assumes OAuth is enabled."""
    issuer = str(settings.http_oauth_issuer)
    audience = settings.oauth_effective_audience
    if audience is None:  # pragma: no cover - guarded by config validation
        raise RuntimeError("OAuth audience could not be resolved (no LF_HTTP_PUBLIC_URL).")
    jwks_url = str(settings.http_oauth_jwks_url) if settings.http_oauth_jwks_url else None
    return JwtTokenVerifier(
        issuer=issuer,
        audience=audience,
        algorithms=settings.oauth_algorithms,
        jwks_url=jwks_url,
    )
