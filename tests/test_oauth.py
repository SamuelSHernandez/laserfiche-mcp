"""Tests for oauth.py — the OAuth Resource Server JWT verifier.

Uses a real RSA keypair and real PyJWT signing/decoding so the security-critical
claim checks (signature, audience, issuer, expiry, scopes) are exercised end to
end. Only the JWKS *fetch* is stubbed — the verifier's PyJWKClient is replaced
with one that returns our public key, so no network is touched.
"""

from __future__ import annotations

import time

import pytest

# The oauth extra (pyjwt[crypto]) is in the dev deps; skip cleanly if absent.
jwt = pytest.importorskip("jwt")
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

from laserfiche_mcp import oauth  # noqa: E402
from laserfiche_mcp.config import Settings  # noqa: E402

_ISSUER = "https://idp.example.com"
_AUDIENCE = "https://lf.example.com/mcp"


@pytest.fixture(scope="module")
def _keypair() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


class _FakeSigningKey:
    def __init__(self, key: object) -> None:
        self.key = key


class _FakeJWKClient:
    """Stands in for PyJWKClient — returns the test public key, no network."""

    def __init__(self, public_key: object) -> None:
        self._public_key = public_key

    def get_signing_key_from_jwt(self, token: str) -> _FakeSigningKey:
        return _FakeSigningKey(self._public_key)


def _make_verifier(
    keypair: rsa.RSAPrivateKey,
    *,
    issuer: str = _ISSUER,
    audience: str = _AUDIENCE,
) -> oauth.JwtTokenVerifier:
    v = oauth.JwtTokenVerifier(
        issuer=issuer,
        audience=audience,
        algorithms=["RS256"],
        jwks_url="https://idp.example.com/jwks",
    )
    # Inject the fake key client so _client() short-circuits discovery/fetch.
    v._jwk_client = _FakeJWKClient(keypair.public_key())  # type: ignore[assignment]
    return v


def _sign(keypair: rsa.RSAPrivateKey, **overrides: object) -> str:
    now = int(time.time())
    claims: dict[str, object] = {
        "iss": _ISSUER,
        "aud": _AUDIENCE,
        "sub": "user-123",
        "iat": now,
        "exp": now + 3600,
        "scope": "laserfiche.read",
    }
    claims.update(overrides)
    return jwt.encode(claims, keypair, algorithm="RS256")


# --- _extract_scopes ---------------------------------------------------------


def test_extract_scopes_from_scope_string() -> None:
    assert oauth._extract_scopes({"scope": "a b c"}) == ["a", "b", "c"]


def test_extract_scopes_from_scp_list() -> None:
    assert oauth._extract_scopes({"scp": ["a", "b"]}) == ["a", "b"]


def test_extract_scopes_empty() -> None:
    assert oauth._extract_scopes({}) == []


# --- verify_token: happy path ------------------------------------------------


async def test_verify_valid_token(_keypair: rsa.RSAPrivateKey) -> None:
    verifier = _make_verifier(_keypair)
    token = _sign(_keypair)
    result = await verifier.verify_token(token)
    assert result is not None
    assert result.client_id == "user-123"
    assert result.scopes == ["laserfiche.read"]
    assert result.resource == _AUDIENCE
    assert result.expires_at is not None


async def test_verify_uses_scp_claim(_keypair: rsa.RSAPrivateKey) -> None:
    verifier = _make_verifier(_keypair)
    token = _sign(_keypair, scope=None, scp=["laserfiche.write"])
    result = await verifier.verify_token(token)
    assert result is not None
    assert result.scopes == ["laserfiche.write"]


async def test_verify_issuer_trailing_slash_tolerated(_keypair: rsa.RSAPrivateKey) -> None:
    # Verifier configured with trailing slash; token iss has none.
    verifier = _make_verifier(_keypair, issuer=_ISSUER + "/")
    token = _sign(_keypair, iss=_ISSUER)
    assert await verifier.verify_token(token) is not None


# --- verify_token: rejection paths (each returns None => 401) ----------------


async def test_verify_rejects_wrong_audience(_keypair: rsa.RSAPrivateKey) -> None:
    verifier = _make_verifier(_keypair)
    token = _sign(_keypair, aud="https://someone-else.example.com")
    assert await verifier.verify_token(token) is None


async def test_verify_rejects_wrong_issuer(_keypair: rsa.RSAPrivateKey) -> None:
    verifier = _make_verifier(_keypair)
    token = _sign(_keypair, iss="https://evil.example.com")
    assert await verifier.verify_token(token) is None


async def test_verify_rejects_expired(_keypair: rsa.RSAPrivateKey) -> None:
    verifier = _make_verifier(_keypair)
    now = int(time.time())
    token = _sign(_keypair, iat=now - 7200, exp=now - 3600)
    assert await verifier.verify_token(token) is None


async def test_verify_rejects_missing_exp(_keypair: rsa.RSAPrivateKey) -> None:
    verifier = _make_verifier(_keypair)
    # exp is required; drop it.
    now = int(time.time())
    token = jwt.encode(
        {"iss": _ISSUER, "aud": _AUDIENCE, "sub": "u", "iat": now},
        _keypair,
        algorithm="RS256",
    )
    assert await verifier.verify_token(token) is None


async def test_verify_rejects_bad_signature(_keypair: rsa.RSAPrivateKey) -> None:
    # Sign with a DIFFERENT key than the verifier trusts.
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    verifier = _make_verifier(_keypair)
    token = _sign(other)
    assert await verifier.verify_token(token) is None


async def test_verify_rejects_garbage(_keypair: rsa.RSAPrivateKey) -> None:
    verifier = _make_verifier(_keypair)
    assert await verifier.verify_token("not-a-jwt") is None


# --- build_token_verifier ----------------------------------------------------


def test_build_token_verifier_from_settings(
    lf_env: dict[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LF_HTTP_OAUTH_ISSUER", _ISSUER)
    monkeypatch.setenv("LF_HTTP_PUBLIC_URL", _AUDIENCE)
    settings = Settings()  # type: ignore[call-arg]
    verifier = oauth.build_token_verifier(settings)
    assert isinstance(verifier, oauth.JwtTokenVerifier)
    assert verifier._audience == _AUDIENCE
    assert verifier._issuer == _ISSUER  # trailing slash normalized off
    assert verifier._algorithms == ["RS256"]
