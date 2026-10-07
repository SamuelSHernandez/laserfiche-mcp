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
import ssl
from typing import TYPE_CHECKING, Any

import httpx
from mcp.server.auth.provider import AccessToken, TokenVerifier

from ._pyjwt_support import require_pyjwt
from .config import Settings
from .tls import tls_verify, validate_secure_url

if TYPE_CHECKING:
    from jwt import PyJWKClient

logger = logging.getLogger("laserfiche_mcp")


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


def validate_jwks_uri(uri: object, *, source: str) -> str:
    """Return ``uri`` if it is safe to fetch signing keys from, else raise ValueError.

    The JWKS URL decides which public keys are trusted to sign access tokens, so a
    discovery document (or config) that points it at plain HTTP, embeds
    credentials, or isn't a URL at all must not be followed. The host may
    legitimately differ from the issuer's (e.g. Google), so that is not enforced.
    """
    return validate_secure_url(uri, source=source, what="jwks_uri")


class JwtTokenVerifier(TokenVerifier):
    """Verifies JWT access tokens against an issuer's JWKS."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        algorithms: list[str],
        jwks_url: str | None = None,
        verify: bool | ssl.SSLContext = True,
    ) -> None:
        # Normalize trailing slash so the manual issuer check below is robust to
        # HttpUrl adding one when the token's `iss` doesn't have it.
        self._issuer = issuer.rstrip("/")
        self._audience = audience
        self._algorithms = algorithms
        self._explicit_jwks_url = jwks_url
        self._jwk_client: PyJWKClient | None = None
        # Trust for reaching the identity provider. Never False: LF_VERIFY_SSL=false is
        # about the Laserfiche server and must not also weaken verification of the IdP.
        self._verify: bool | ssl.SSLContext = verify if verify is not False else True

    async def _jwks_url(self) -> str:
        """Resolve the JWKS URL — explicit, else via OpenID discovery."""
        if self._explicit_jwks_url:
            return validate_jwks_uri(self._explicit_jwks_url, source="LF_HTTP_OAUTH_JWKS_URL")
        discovery = f"{self._issuer}/.well-known/openid-configuration"
        async with httpx.AsyncClient(timeout=10.0, verify=self._verify) as client:
            resp = await client.get(discovery)
            resp.raise_for_status()
            jwks_uri = resp.json().get("jwks_uri")
        try:
            return validate_jwks_uri(jwks_uri, source=f"OpenID discovery at {discovery}")
        except ValueError as exc:
            logger.warning("Refusing to fetch signing keys: %s", exc)
            raise

    async def _client(self) -> PyJWKClient:
        """Lazily build (and cache) the PyJWKClient that fetches signing keys."""
        if self._jwk_client is None:
            jwt = require_pyjwt("OAuth Resource Server mode")
            url = await self._jwks_url()
            # PyJWKClient caches keys and handles rotation on cache miss.
            ssl_context = self._verify if isinstance(self._verify, ssl.SSLContext) else None
            self._jwk_client = jwt.PyJWKClient(url, cache_keys=True, ssl_context=ssl_context)
        return self._jwk_client

    async def verify_token(self, token: str) -> AccessToken | None:
        jwt = require_pyjwt("OAuth Resource Server mode")
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
        extra: dict[str, Any] = {}
        # Newer SDKs carry the end user separately from the client application.
        # Confirmation tokens bind to it, so one user's preview can't authorize
        # another user's call even when both come through the same OAuth client.
        if "subject" in AccessToken.model_fields and claims.get("sub") is not None:
            extra["subject"] = str(claims["sub"])
        return AccessToken(
            token=token,
            client_id=str(client_id),
            scopes=_extract_scopes(claims),
            expires_at=int(expires_at) if expires_at is not None else None,
            resource=self._audience,
            **extra,
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
        verify=tls_verify(settings),
    )
