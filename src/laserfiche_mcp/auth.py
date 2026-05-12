"""Authentication strategies for Laserfiche.

Self-hosted Laserfiche Repository API does NOT support HTTP Basic auth.
The supported flows are:

* **Password grant** — POST username/password to ``/v2/Repositories/{repository_id}/Token``,
  receive a bearer token (``expires_in`` ~= 900s), use it on subsequent calls.
  Implemented as :class:`PasswordGrantStrategy`. This is the default.

* **OAuth (cloud, client_credentials with JWT assertion)** — uses
  ``signin.laserfiche.com/oauth/token`` with a JWT signed by the access key
  as the Bearer credential. Significantly more setup; reserved for v2.

References:
  https://developer.laserfiche.com/docs/api/server/authentication/
  https://developer.laserfiche.com/docs/api/authentication/guide_oauth-service/
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod

import httpx
from pydantic import SecretStr

from .config import AuthMode, Settings

logger = logging.getLogger("laserfiche_mcp.auth")


class AuthStrategy(ABC):
    """Adds whatever auth header(s) a request needs."""

    @abstractmethod
    async def apply(self, request: httpx.Request) -> None: ...


class PasswordGrantStrategy(AuthStrategy):
    """Self-hosted Laserfiche password-grant token exchange.

    On first call, POSTs ``grant_type=password&username=...&password=...``
    (form-encoded) to ``{base_url}/v2/Repositories/{repository_id}/Token``
    and caches the bearer token. Refreshes ~30 seconds before ``expires_in``.
    """

    def __init__(
        self,
        base_url: str,
        repository_id: str,
        username: str,
        password: SecretStr,
        verify_ssl: bool = True,
        timeout_seconds: float = 30.0,
    ) -> None:
        if not base_url.endswith("/"):
            base_url += "/"
        # Self-hosted LFRepositoryAPI v2 token endpoint sits at
        # /v2/Repositories/{repositoryId}/Token, consistent with every other
        # v2 path in this client. (The /v2/{repo}/Token shape without
        # /Repositories/ that some Laserfiche Cloud docs show returns 404 on
        # self-hosted servers.)
        self._token_url = f"{base_url}v2/Repositories/{repository_id}/Token"
        self._username = username
        self._password = password
        self._verify_ssl = verify_ssl
        self._timeout_seconds = timeout_seconds
        self._access_token: str | None = None
        self._expires_at: float = 0.0

    async def apply(self, request: httpx.Request) -> None:
        if not self._access_token or time.time() >= self._expires_at - 30:
            await self._refresh()
        request.headers["Authorization"] = f"Bearer {self._access_token}"

    async def _refresh(self) -> None:
        logger.debug("Exchanging password for bearer token at %s", self._token_url)
        async with httpx.AsyncClient(
            verify=self._verify_ssl,
            timeout=self._timeout_seconds,
        ) as client:
            resp = await client.post(
                self._token_url,
                data={
                    "grant_type": "password",
                    "username": self._username,
                    "password": self._password.get_secret_value(),
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            resp.raise_for_status()
            payload = resp.json()
            self._access_token = payload["access_token"]
            # Default 900s if expires_in is absent.
            self._expires_at = time.time() + payload.get("expires_in", 900)


class OAuthClientCredentialsStrategy(AuthStrategy):
    """OAuth 2.0 client_credentials grant with simple client_secret.

    NOTE: Laserfiche **Cloud** uses a different flow — it requires a JWT
    signed by an access key as the Bearer credential, not a plain
    ``client_secret`` in the request body. That flow is not implemented in
    v1; see ``signin.laserfiche.com`` docs for the JWT structure.

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

    async def apply(self, request: httpx.Request) -> None:
        if not self._access_token or time.time() >= self._expires_at - 30:
            await self._refresh()
        request.headers["Authorization"] = f"Bearer {self._access_token}"

    async def _refresh(self) -> None:
        logger.debug("Refreshing OAuth access token from %s", self._token_url)
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
            resp = await client.post(self._token_url, data=data)
            resp.raise_for_status()
            payload = resp.json()
            self._access_token = payload["access_token"]
            self._expires_at = time.time() + payload.get("expires_in", 3600)


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
            verify_ssl=settings.verify_ssl,
            timeout_seconds=settings.request_timeout_seconds,
        )

    if settings.auth_mode is AuthMode.OAUTH:
        assert (
            settings.oauth_token_url
            and settings.client_id
            and settings.client_secret
        )
        return OAuthClientCredentialsStrategy(
            token_url=str(settings.oauth_token_url),
            client_id=settings.client_id,
            client_secret=settings.client_secret,
            scope=settings.oauth_scope,
            verify_ssl=settings.verify_ssl,
            timeout_seconds=settings.request_timeout_seconds,
        )

    # Settings validation rejects API_KEY before we get here.
    raise NotImplementedError(f"Unsupported auth mode: {settings.auth_mode}")
