"""TLS trust options: internal-CA support without turning verification off.

Uses a throwaway CA and a real local HTTPS server, so the trust and hostname
behavior is exercised end to end rather than mocked.
"""

from __future__ import annotations

import datetime
import http.server
import json
import ssl
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from laserfiche_mcp import tls
from laserfiche_mcp.auth import AuthStrategy
from laserfiche_mcp.client import LaserficheClient
from laserfiche_mcp.config import Settings


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _write(path: Path, data: bytes) -> str:
    path.write_bytes(data)
    return str(path)


@dataclass
class _Pki:
    ca_pem: str  # path to the CA certificate
    server_port: int


@pytest.fixture(scope="module")
def pki(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Pki]:
    d = tmp_path_factory.mktemp("pki")
    now = datetime.datetime.now(datetime.timezone.utc)
    day = datetime.timedelta(days=1)

    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(_name("Test Internal CA"))
        .issuer_name(_name("Test Internal CA"))
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - day)
        .not_valid_after(now + 30 * day)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                key_cert_sign=True,
                crl_sign=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    srv_key = ec.generate_private_key(ec.SECP256R1())
    srv_cert = (
        x509.CertificateBuilder()
        .subject_name(_name("localhost"))
        .issuer_name(ca_cert.subject)
        .public_key(srv_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - day)
        .not_valid_after(now + 30 * day)
        # Valid for the NAME "localhost" only — NOT for the bare IP 127.0.0.1, which
        # mirrors the real-world "cert is for the FQDN, config uses the short name" trap.
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(srv_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_path = _write(d / "ca.pem", ca_cert.public_bytes(serialization.Encoding.PEM))
    cert_path = _write(d / "srv.pem", srv_cert.public_bytes(serialization.Encoding.PEM))
    key_path = _write(
        d / "srv.key",
        srv_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = json.dumps({"id": 1, "name": "ok", "entryType": "Document"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return None

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert_path, key_path)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield _Pki(ca_pem=ca_path, server_port=server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "repo_api_url": "https://localhost:1/LFRepositoryAPI",
        "repository_id": "r",
        "username": "u",
        "password": "p",
    }
    return Settings(_env_file=None, **{**base, **overrides})  # type: ignore[arg-type]


async def _get(verify: bool | ssl.SSLContext, host: str, port: int) -> httpx.Response:
    async with httpx.AsyncClient(verify=verify, timeout=10) as client:
        return await client.get(f"https://{host}:{port}/")


# --- trust behaviour against a real TLS server --------------------------------


async def test_default_trust_rejects_an_internal_ca(pki: _Pki) -> None:
    verify = tls.tls_verify(_settings())
    assert verify is True
    with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
        await _get(verify, "localhost", pki.server_port)


async def test_ca_bundle_trusts_the_internal_ca(pki: _Pki) -> None:
    verify = tls.tls_verify(_settings(ca_bundle=pki.ca_pem))
    assert isinstance(verify, ssl.SSLContext)
    resp = await _get(verify, "localhost", pki.server_port)
    assert resp.status_code == 200


async def test_hostname_is_still_checked_even_with_the_ca_trusted(pki: _Pki) -> None:
    """Trusting the CA must not weaken hostname verification: connecting by an
    address the certificate wasn't issued for still fails."""
    verify = tls.tls_verify(_settings(ca_bundle=pki.ca_pem))
    with pytest.raises(httpx.ConnectError, match="(?i)hostname|IP address mismatch"):
        await _get(verify, "127.0.0.1", pki.server_port)


async def test_system_store_alone_does_not_trust_an_unknown_ca(pki: _Pki) -> None:
    verify = tls.tls_verify(_settings(use_system_ca=True))
    assert isinstance(verify, ssl.SSLContext)
    with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
        await _get(verify, "localhost", pki.server_port)


async def test_system_store_plus_bundle_works(pki: _Pki) -> None:
    verify = tls.tls_verify(_settings(use_system_ca=True, ca_bundle=pki.ca_pem))
    assert (await _get(verify, "localhost", pki.server_port)).status_code == 200


async def test_disabling_verification_still_works_but_is_the_insecure_path(pki: _Pki) -> None:
    verify = tls.tls_verify(_settings(verify_ssl=False))
    assert verify is False
    assert (await _get(verify, "127.0.0.1", pki.server_port)).status_code == 200


class _NoAuth(AuthStrategy):
    async def apply(self, request: httpx.Request) -> None:
        return None


async def test_the_real_client_uses_the_configured_trust(pki: _Pki) -> None:
    """End to end through LaserficheClient: with LF_CA_BUNDLE it reaches an
    internal-CA HTTPS server; without it, it refuses."""
    url = f"https://localhost:{pki.server_port}/LFRepositoryAPI"

    async with LaserficheClient(
        _settings(repo_api_url=url, ca_bundle=pki.ca_pem, retry_attempts=0), _NoAuth()
    ) as client:
        assert (await client.get_entry(1))["name"] == "ok"

    from laserfiche_mcp.errors import LaserficheError

    async with LaserficheClient(_settings(repo_api_url=url, retry_attempts=0), _NoAuth()) as bare:
        with pytest.raises(LaserficheError, match="(?i)certificate|network"):
            await bare.get_entry(1)


# --- configuration validation -------------------------------------------------


def test_missing_ca_bundle_file_is_a_clear_startup_error(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="LF_CA_BUNDLE"):
        _settings(ca_bundle=str(tmp_path / "nope.pem"))


def test_require_https_refuses_plain_http_on_a_real_host() -> None:
    with pytest.raises(ValueError, match="LF_REQUIRE_HTTPS"):
        _settings(repo_api_url="http://lf.internal/LFRepositoryAPI", require_https=True)


def test_require_https_allows_https_and_loopback_http() -> None:
    _settings(repo_api_url="https://lf.internal/LFRepositoryAPI", require_https=True)
    _settings(repo_api_url="http://localhost:8080/LFRepositoryAPI", require_https=True)
    _settings(repo_api_url="http://127.0.0.1/LFRepositoryAPI", require_https=True)


def test_require_https_also_covers_the_oauth_token_url() -> None:
    with pytest.raises(ValueError, match="LF_OAUTH_TOKEN_URL"):
        _settings(
            auth_mode="oauth",
            oauth_token_url="http://idp.internal/token",
            client_id="c",
            client_secret="s",
            require_https=True,
        )


def test_plain_http_is_still_allowed_by_default() -> None:
    assert _settings(repo_api_url="http://lf.internal/LFRepositoryAPI").require_https is False


def test_describe_tls_summaries(tmp_path: Path) -> None:
    assert "public CAs" in tls.describe_tls(_settings())
    assert "operating-system" in tls.describe_tls(_settings(use_system_ca=True))
    assert "DISABLED" in tls.describe_tls(_settings(verify_ssl=False))
    bundle = tmp_path / "ca.pem"
    bundle.write_text("x")
    # content isn't parsed by describe_tls, only by tls_verify
    assert str(bundle) in tls.describe_tls(_settings(ca_bundle=str(bundle)))


def test_certificate_hint_points_at_the_right_fix() -> None:
    trust = tls.certificate_hint("[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer")
    assert "LF_USE_SYSTEM_CA" in trust and "LF_VERIFY_SSL=false" in trust
    assert "do not use" in trust.lower()
    name = tls.certificate_hint("Hostname mismatch, certificate is not valid for 'x'")
    assert "host name" in name and "LF_REPO_API_URL" in name
    assert tls.certificate_hint("connection refused") == ""


async def test_untrusted_certificate_error_tells_the_operator_what_to_set(pki: _Pki) -> None:
    from laserfiche_mcp.errors import LaserficheError

    url = f"https://localhost:{pki.server_port}/LFRepositoryAPI"
    async with LaserficheClient(_settings(repo_api_url=url, retry_attempts=0), _NoAuth()) as c:
        with pytest.raises(LaserficheError) as info:
            await c.get_entry(1)
    assert "LF_USE_SYSTEM_CA" in str(info.value)
