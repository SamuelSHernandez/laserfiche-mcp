"""Authentication strategies for Laserfiche.

Self-hosted Laserfiche Repository API does NOT support HTTP Basic auth.
The supported flows are:

* **Password grant** — POST username/password to ``/v2/Repositories/{repository_id}/Token``,
  receive a bearer token (``expires_in`` ~= 900s), use it on subsequent calls.
  Implemented as :class:`PasswordGrantStrategy`. This is the default.

* **OAuth (LFDS or a compatible provider, plain client_credentials)** — a
  standard ``client_id``/``client_secret`` grant. Implemented as
  :class:`OAuthClientCredentialsStrategy`.

* **Cloud service app (client_credentials with a JWT assertion)** — Laserfiche
  Cloud's own flow: a short-lived ES256-signed JWT (minted from an "access
  key" exported from the Developer Console) is presented as the Bearer
  credential on a request to ``https://signin.{domain}/oauth/token``, which
  returns a longer-lived access token used as a normal Bearer credential
  against ``https://api.{domain}/repository/v2/...``. Implemented as
  :class:`CloudServiceAppStrategy`. Built and unit-tested against the
  documented flow and Laserfiche's own open-source client library
  (``Laserfiche/lf-api-client-core-dotnet``) — **never verified against a
  live Cloud tenant**; treat as beta until someone with Cloud access
  confirms it end-to-end.

* **OAuth passthrough (delegated / on-behalf-of)** — reuses the calling MCP
  client's own already-verified bearer token as the credential for outbound
  Laserfiche calls, instead of a single shared service account. Implemented
  as :class:`PassthroughTokenStrategy`. Only meaningful under ``--http`` with
  OAuth Resource Server mode (``LF_HTTP_OAUTH_ISSUER``) enabled — see that
  class's docstring for the audience caveat that determines whether this
  actually works against a given tenant. **Never verified against a live
  LFDS tenant**; treat as beta.

References:
  https://developer.laserfiche.com/docs/api/server/authentication/
  https://developer.laserfiche.com/docs/api/authentication/guide_oauth-service/
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from abc import ABC, abstractmethod
from typing import Any

import httpx
from mcp.server.auth.middleware.auth_context import get_access_token
from pydantic import SecretStr

from ._pyjwt_support import require_pyjwt
from .config import ApiVersion, AuthMode, Settings
from .errors import LaserficheError

logger = logging.getLogger("laserfiche_mcp.auth")

_CLOUD_ASSERTION_AUDIENCE = "laserfiche.com"
_CLOUD_ASSERTION_TTL_SECONDS = 1800  # 30 min — matches the official .NET client's default.


async def _post_token(
    client: httpx.AsyncClient,
    url: str,
    data: dict[str, str],
    *,
    grant: str,
    default_expires_in: float = 900.0,
    extra_headers: dict[str, str] | None = None,
) -> tuple[str, float]:
    """POST a token request and return ``(access_token, expires_in)``.

    Every failure mode is normalized to :class:`LaserficheError`. Without
    this, a slow or unreachable token endpoint raised a bare
    ``httpx.TimeoutException`` / ``HTTPStatusError`` / ``KeyError`` out of
    ``AuthStrategy.apply``, which is called *inside* every tool's
    ``try: ... except LaserficheError`` block. Those escaped the structured
    error contract entirely and surfaced to the model as an empty
    ``Error executing tool <name>:`` with nothing actionable in it.
    """
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if extra_headers:
        headers.update(extra_headers)
    try:
        resp = await client.post(url, data=data, headers=headers)
    except httpx.TimeoutException as exc:
        raise LaserficheError(
            f"Timed out contacting the Laserfiche token endpoint during the "
            f"{grant} grant: {exc!r}. The server may be cold-starting; retry, "
            f"or raise LF_REQUEST_TIMEOUT_SECONDS."
        ) from exc
    except httpx.HTTPError as exc:
        raise LaserficheError(
            f"Could not reach the Laserfiche token endpoint for the "
            f"{grant} grant: {exc!r}. Check LF_REPO_API_URL and network "
            f"reachability."
        ) from exc

    if resp.status_code >= 400:
        try:
            detail: object = resp.json()
        except ValueError:
            detail = resp.text
        raise LaserficheError(
            f"Laserfiche token endpoint rejected the {grant} grant ({resp.status_code}): {detail}",
            status_code=resp.status_code,
            detail=detail,
        )

    try:
        payload = resp.json()
        return payload["access_token"], float(payload.get("expires_in", default_expires_in))
    except (ValueError, KeyError, TypeError) as exc:
        raise LaserficheError(
            f"Laserfiche token endpoint returned an unexpected payload for "
            f"the {grant} grant (no usable access_token): {exc!r}"
        ) from exc


class AuthStrategy(ABC):
    """Adds whatever auth header(s) a request needs."""

    @abstractmethod
    async def apply(self, request: httpx.Request) -> None: ...


class PasswordGrantStrategy(AuthStrategy):
    """Self-hosted Laserfiche password-grant token exchange.

    On first call, POSTs ``grant_type=password&username=...&password=...``
    (form-encoded) to ``{base_url}/{api_version}/Repositories/{repository_id}/Token``
    and caches the bearer token. Refreshes ~30 seconds before ``expires_in``.
    """

    def __init__(
        self,
        base_url: str,
        repository_id: str,
        username: str,
        password: SecretStr,
        api_version: ApiVersion = ApiVersion.V1,
        verify_ssl: bool = True,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not base_url.endswith("/"):
            base_url += "/"
        # Both v1 and v2 expose the token endpoint at
        # /{version}/Repositories/{repositoryId}/Token with the same
        # form-encoded grant_type=password body. (The /{version}/{repo}/Token
        # shape without /Repositories/ that some Cloud docs show returns 404
        # on self-hosted servers regardless of version.)
        self._token_url = f"{base_url}{api_version.value}/Repositories/{repository_id}/Token"
        self._username = username
        self._password = password
        self._verify_ssl = verify_ssl
        self._timeout_seconds = timeout_seconds
        self._access_token: str | None = None
        self._expires_at: float = 0.0
        self._refresh_lock = asyncio.Lock()

    async def apply(self, request: httpx.Request) -> None:
        if not self._access_token or time.time() >= self._expires_at - 30:
            async with self._refresh_lock:
                # Re-check: another concurrent call may have refreshed while we
                # waited for the lock — avoids a redundant token exchange.
                if not self._access_token or time.time() >= self._expires_at - 30:
                    await self._refresh()
        request.headers["Authorization"] = f"Bearer {self._access_token}"

    async def _refresh(self) -> None:
        # URL deliberately omitted (operator already configured the host;
        # logging it at DEBUG just adds noise to log aggregators). If a
        # future addition needs to log a URL here, route it through
        # ``redact(url, host=..., repo_id=...)`` to keep the deployment
        # context out of WARNING-or-lower output.
        logger.debug("Exchanging password for bearer token (password grant)")
        async with httpx.AsyncClient(
            verify=self._verify_ssl,
            timeout=self._timeout_seconds,
        ) as client:
            token, expires_in = await _post_token(
                client,
                self._token_url,
                {
                    "grant_type": "password",
                    "username": self._username,
                    "password": self._password.get_secret_value(),
                },
                grant="password",
            )
            self._access_token = token
            # Default 900s if expires_in is absent.
            self._expires_at = time.time() + expires_in


class OAuthClientCredentialsStrategy(AuthStrategy):
    """OAuth 2.0 client_credentials grant with simple client_secret.

    NOTE: Laserfiche **Cloud** uses a different flow — see
    :class:`CloudServiceAppStrategy` below.

    This class works for OAuth providers that accept plain client_credentials
    (e.g., LFDS configured with a custom service app, or any standards-
    compliant OAuth server). Refreshes ~30 seconds before ``expires_in``.
    """

    def __init__(
        self,
        token_url: str,
        client_id: str,
        client_secret: SecretStr,
        scope: str | None = None,
        verify_ssl: bool = True,
        timeout_seconds: float = 30.0,
    ) -> None:
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._scope = scope
        self._verify_ssl = verify_ssl
        self._timeout_seconds = timeout_seconds
        self._access_token: str | None = None
        self._expires_at: float = 0.0
        self._refresh_lock = asyncio.Lock()

    async def apply(self, request: httpx.Request) -> None:
        if not self._access_token or time.time() >= self._expires_at - 30:
            async with self._refresh_lock:
                if not self._access_token or time.time() >= self._expires_at - 30:
                    await self._refresh()
        request.headers["Authorization"] = f"Bearer {self._access_token}"

    async def _refresh(self) -> None:
        logger.debug("Refreshing OAuth access token (client_credentials)")
        async with httpx.AsyncClient(
            verify=self._verify_ssl,
            timeout=self._timeout_seconds,
        ) as client:
            data = {
                "grant_type": "client_credentials",
                "client_id": self._client_id,
                "client_secret": self._client_secret.get_secret_value(),
            }
            if self._scope:
                data["scope"] = self._scope
            token, expires_in = await _post_token(
                client,
                self._token_url,
                data,
                grant="client_credentials",
                default_expires_in=3600.0,
            )
            self._access_token = token
            self._expires_at = time.time() + expires_in


def _decode_cloud_access_key(access_key_b64: str) -> dict[str, Any]:
    """Decode the base64-encoded 'access key' JSON blob exported from the
    Laserfiche Developer Console: ``{customerId, domain, clientId, jwk}``.

    Raises ``ValueError`` with a clear message on malformed input — this
    runs once at server startup (building the auth strategy), so failing
    fast here beats a confusing error on the first real request.
    """
    try:
        raw = base64.b64decode(access_key_b64, validate=True)
        data: dict[str, Any] = json.loads(raw)
    except Exception as exc:
        raise ValueError(
            "LF_CLOUD_ACCESS_KEY is not a valid base64-encoded access key "
            "JSON blob. Re-export it from the Laserfiche Developer Console "
            f"(App Configuration > Authentication). Details: {exc!r}"
        ) from exc

    missing = [k for k in ("clientId", "domain", "jwk") if not data.get(k)]
    if missing:
        raise ValueError(f"LF_CLOUD_ACCESS_KEY is missing required field(s): {', '.join(missing)}.")

    jwk = data.get("jwk")
    if not isinstance(jwk, dict):
        raise ValueError("LF_CLOUD_ACCESS_KEY's 'jwk' field must be a JSON object.")
    jwk_missing = [k for k in ("crv", "x", "y", "d") if not jwk.get(k)]
    if jwk_missing:
        raise ValueError(
            "LF_CLOUD_ACCESS_KEY's 'jwk' field is missing required EC private-key "
            f"component(s): {', '.join(jwk_missing)}. Re-export the access key from "
            "the Laserfiche Developer Console (App Configuration > Authentication) "
            "— it must contain the private key ('d'), not just the public point."
        )
    if jwk.get("crv") != "P-256":
        raise ValueError(
            f"LF_CLOUD_ACCESS_KEY's 'jwk.crv' is {jwk.get('crv')!r}, expected 'P-256' "
            "(the assertion is signed with ES256)."
        )
    return data


class CloudServiceAppStrategy(AuthStrategy):
    """Laserfiche Cloud OAuth service-app flow (client_credentials + JWT assertion).

    Mirrors Laserfiche's own client libraries (see
    ``Laserfiche/lf-api-client-core-dotnet``'s ``JwtUtils``/``TokenClient``):

    1. Mint a short-lived (30 min) JWT signed with ES256, using the EC
       private key embedded in the access key's ``jwk``. Claims:
       ``client_id`` (from the access key), ``client_secret`` (the separate
       service principal key), ``aud="laserfiche.com"``, ``iat``/``nbf``/``exp``.
       The JWT header carries the key's ``kid``.
    2. POST that JWT as the *Bearer credential* (not a request-body field —
       this is Laserfiche's own bespoke shape, not RFC 7523 JWT-bearer) to
       ``https://signin.{domain}/oauth/token`` with
       ``grant_type=client_credentials`` (+ optional ``scope``).
    3. Cache the returned access token and use it as a normal Bearer
       credential against ``https://api.{domain}/repository/v2/...`` —
       which is exactly the URL shape ``LF_REPO_API_URL`` + v2 already
       builds, so no client/routing changes are needed for Cloud.

    Built and unit-tested against the documented flow and the official
    client library above; **never verified against a live Cloud tenant.**
    """

    def __init__(
        self,
        access_key_b64: str,
        service_principal_key: SecretStr,
        scope: str | None = None,
        verify_ssl: bool = True,
        timeout_seconds: float = 30.0,
    ) -> None:
        access_key = _decode_cloud_access_key(access_key_b64)
        self._client_id: str = access_key["clientId"]
        self._domain: str = access_key["domain"]
        self._jwk: dict[str, Any] = access_key["jwk"]
        self._service_principal_key = service_principal_key
        self._scope = scope
        self._verify_ssl = verify_ssl
        self._timeout_seconds = timeout_seconds
        self._token_url = f"https://signin.{self._domain}/oauth/token"
        self._access_token: str | None = None
        self._expires_at: float = 0.0
        self._refresh_lock = asyncio.Lock()

    async def apply(self, request: httpx.Request) -> None:
        if not self._access_token or time.time() >= self._expires_at - 30:
            async with self._refresh_lock:
                if not self._access_token or time.time() >= self._expires_at - 30:
                    await self._refresh()
        request.headers["Authorization"] = f"Bearer {self._access_token}"

    def _build_assertion_jwt(self) -> str:
        """Mint the client_credentials assertion JWT.

        ``_decode_cloud_access_key`` already validates the jwk's shape at
        startup, but this still wraps key construction / signing so a
        latent issue (e.g. a jwk that decodes but isn't accepted by the
        crypto backend) raises :class:`LaserficheError` instead of an
        uncaught exception escaping the structured error contract on
        every single tool call.
        """
        try:
            jwt = require_pyjwt("Cloud service-app auth (LF_AUTH_MODE=api_key)")
            from jwt.algorithms import ECAlgorithm  # noqa: PLC0415

            private_key = ECAlgorithm(ECAlgorithm.SHA256).from_jwk(json.dumps(self._jwk))
            now = int(time.time())
            claims = {
                "client_id": self._client_id,
                "client_secret": self._service_principal_key.get_secret_value(),
                "aud": _CLOUD_ASSERTION_AUDIENCE,
                "iat": now,
                "nbf": now,
                "exp": now + _CLOUD_ASSERTION_TTL_SECONDS,
            }
            headers = {"kid": self._jwk["kid"]} if self._jwk.get("kid") else None
            return str(jwt.encode(claims, private_key, algorithm="ES256", headers=headers))
        except Exception as exc:
            raise LaserficheError(
                "Could not build the Cloud service-app assertion JWT from "
                f"LF_CLOUD_ACCESS_KEY's jwk: {exc!r}. Re-export the access key "
                "from the Laserfiche Developer Console (App Configuration > "
                "Authentication)."
            ) from exc

    async def _refresh(self) -> None:
        logger.debug("Requesting Cloud access token (service-app client_credentials)")
        assertion = self._build_assertion_jwt()
        async with httpx.AsyncClient(
            verify=self._verify_ssl,
            timeout=self._timeout_seconds,
        ) as client:
            data = {"grant_type": "client_credentials"}
            if self._scope:
                data["scope"] = self._scope
            token, expires_in = await _post_token(
                client,
                self._token_url,
                data,
                grant="cloud_client_credentials",
                default_expires_in=3600.0,
                extra_headers={"Authorization": f"Bearer {assertion}"},
            )
            self._access_token = token
            self._expires_at = time.time() + expires_in


class PassthroughTokenStrategy(AuthStrategy):
    """Reuses the calling MCP client's own verified bearer token for outbound
    Laserfiche Repository API calls, instead of one shared service account.

    Requires ``--http`` running in OAuth Resource Server mode
    (``LF_HTTP_OAUTH_ISSUER``, see ``oauth.py``). That mode already verifies
    each caller's token (signature, audience, issuer, expiry) before a tool
    call runs, and ``mcp.server.fastmcp`` wires ``AuthContextMiddleware`` into
    the Streamable HTTP app whenever a ``TokenVerifier`` is configured — see
    ``http_transport.py:_configure_oauth`` — so the verified
    :class:`~mcp.server.auth.provider.AccessToken` for the request currently
    being handled is always available from ``get_access_token()``, scoped per
    request via Python's ``contextvars`` (concurrent callers don't cross
    contaminate). This class is intentionally stateless: unlike the other
    strategies above it caches nothing, because the right token to use
    changes on every call depending on who's currently calling.

    **Whether this produces genuine per-user Laserfiche access, or just a
    401 on the first real call, depends entirely on the authorization server
    behind LF_HTTP_OAUTH_ISSUER:**

    * If it's Laserfiche's own LFDS, and the tokens it issues carry an
      audience/resource already valid for the Repository API, the token
      forwarded here works as-is — this is genuine delegation: Laserfiche's
      own audit trail shows the calling user, and its own ACLs apply.
    * If LF_HTTP_OAUTH_ISSUER points at a *different* identity provider
      (Entra, Okta, Auth0, Google) — the common case when it's just
      authenticating who may use the MCP connector — that token authenticates
      fine *here* but Laserfiche will reject it outright: passthrough mode
      cannot invent Laserfiche permissions the caller's token was never
      granted. In that case, use the shared-service-account modes above
      instead.

    There is no fallback to a service account on failure by design — a
    silent fallback would defeat the point (every call would quietly run as
    the shared account again), so a missing/rejected token surfaces as a
    clear error instead.
    """

    async def apply(self, request: httpx.Request) -> None:
        token = get_access_token()
        if token is None:
            raise LaserficheError(
                "LF_AUTH_MODE=oauth_passthrough has no verified caller token "
                "to use for this request. This mode only works for calls "
                "made under `--http` with LF_HTTP_OAUTH_ISSUER configured "
                "(OAuth Resource Server mode) — stdio and unauthenticated "
                "--http requests have no per-caller token to pass through."
            )
        request.headers["Authorization"] = f"Bearer {token.token}"


def build_auth_strategy(settings: Settings) -> AuthStrategy:
    """Factory: pick the right strategy for the configured auth_mode."""
    if settings.auth_mode is AuthMode.PASSWORD:
        # Validated upstream: repo_api_url, repository_id, username, password all present.
        assert (
            settings.repo_api_url
            and settings.repository_id
            and settings.username
            and settings.password
        )
        return PasswordGrantStrategy(
            base_url=str(settings.repo_api_url),
            repository_id=settings.repository_id,
            username=settings.username,
            password=settings.password,
            api_version=settings.api_version,
            verify_ssl=settings.verify_ssl,
            timeout_seconds=settings.request_timeout_seconds,
        )

    if settings.auth_mode is AuthMode.OAUTH:
        assert settings.oauth_token_url and settings.client_id and settings.client_secret
        return OAuthClientCredentialsStrategy(
            token_url=str(settings.oauth_token_url),
            client_id=settings.client_id,
            client_secret=settings.client_secret,
            scope=settings.oauth_scope,
            verify_ssl=settings.verify_ssl,
            timeout_seconds=settings.request_timeout_seconds,
        )

    if settings.auth_mode is AuthMode.API_KEY:
        # Validated upstream: cloud_access_key and cloud_service_principal_key present.
        assert settings.cloud_access_key and settings.cloud_service_principal_key
        return CloudServiceAppStrategy(
            access_key_b64=settings.cloud_access_key.get_secret_value(),
            service_principal_key=settings.cloud_service_principal_key,
            scope=settings.oauth_scope,
            verify_ssl=settings.verify_ssl,
            timeout_seconds=settings.request_timeout_seconds,
        )

    if settings.auth_mode is AuthMode.OAUTH_PASSTHROUGH:
        # Validated upstream: http_oauth_issuer is set (config._validate).
        return PassthroughTokenStrategy()

    raise NotImplementedError(f"Unsupported auth mode: {settings.auth_mode}")  # pragma: no cover
