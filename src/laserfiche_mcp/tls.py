"""TLS trust configuration for every outbound connection to Laserfiche.

By default httpx trusts only the public certificate authorities in its bundled
``certifi`` list. A self-hosted Laserfiche server is usually fronted by a
certificate from the organisation's *internal* CA, which that bundle does not
contain — so the only option used to be ``LF_VERIFY_SSL=false``, which turns
checking off completely and leaves the connection open to impersonation. This
module gives operators the safe alternatives:

* ``LF_USE_SYSTEM_CA=true`` — trust the operating system's certificate store
  (Windows certificate store, macOS keychain, the distro's CA directory), which
  already holds the internal CA on a managed machine.
* ``LF_CA_BUNDLE=<path>`` — additionally trust the CA certificate(s) in a PEM file.
  Combines with the setting above or with the built-in public CAs.

Precedence: ``LF_VERIFY_SSL=false`` disables verification outright (and warns);
otherwise a verifying context is built from the choices above.
"""

from __future__ import annotations

import ssl
from functools import lru_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings


@lru_cache(maxsize=8)
def _build_context(use_system_ca: bool, ca_bundle: str | None) -> ssl.SSLContext:
    if use_system_ca:
        ctx = ssl.create_default_context()  # OS trust store
    else:
        import certifi  # noqa: PLC0415 — httpx dependency; keeps the public-CA default

        ctx = ssl.create_default_context(cafile=certifi.where())
    if ca_bundle:
        ctx.load_verify_locations(cafile=ca_bundle)
    return ctx


def tls_verify(settings: Settings) -> bool | ssl.SSLContext:
    """The value to pass as httpx's ``verify=`` for these settings.

    ``False`` when verification is explicitly disabled, ``True`` for the plain
    default (httpx's own public-CA bundle), otherwise a verifying
    :class:`ssl.SSLContext` that includes the requested extra trust.
    """
    if not settings.verify_ssl:
        return False
    if not settings.use_system_ca and not settings.ca_bundle:
        return True
    return _build_context(settings.use_system_ca, settings.ca_bundle)


def validate_secure_url(uri: object, *, source: str, what: str) -> str:
    """Return ``uri`` if it is acceptable for a security-critical endpoint, else raise ValueError.

    Used for URLs that decide who is trusted (OAuth issuer, JWKS location): they must
    be ``https://`` with a host and no embedded credentials, so nobody on the network
    path can impersonate the identity provider or substitute signing keys. Plain
    ``http://`` is accepted only for loopback hosts (local testing).
    """
    from urllib.parse import urlsplit  # noqa: PLC0415

    if not isinstance(uri, str) or not uri.strip():
        raise ValueError(f"{source} did not provide a usable {what}")
    parts = urlsplit(uri.strip())
    host = parts.hostname or ""
    if not host:
        raise ValueError(f"{source} {what} has no host: {uri!r}")
    if parts.username or parts.password:
        raise ValueError(f"{source} {what} must not embed credentials")
    loopback = host in ("127.0.0.1", "::1", "localhost")
    if parts.scheme != "https" and not (parts.scheme == "http" and loopback):
        raise ValueError(f"{source} {what} must be https:// (got {parts.scheme or 'no'} scheme)")
    return uri.strip()


def certificate_hint(error_text: str) -> str:
    """Actionable advice when a connection failed on certificate verification, else "".

    Appended to network errors so a TLS trust problem doesn't read as "server down".
    """
    lowered = error_text.lower()
    if "hostname mismatch" in lowered or "ip address mismatch" in lowered:
        return (
            " The server's certificate is not valid for the host name in the URL — use the "
            "name on the certificate (often the full host.domain form) in LF_REPO_API_URL."
        )
    if "certificate_verify_failed" in lowered or "certificate verify failed" in lowered:
        return (
            " The server's certificate is not trusted by this machine's configuration. If it "
            "comes from your organisation's internal CA, set LF_USE_SYSTEM_CA=true (or "
            "LF_CA_BUNDLE=<path to the CA .pem>). Do not use LF_VERIFY_SSL=false — it turns "
            "certificate checking off entirely."
        )
    return ""


def describe_tls(settings: Settings) -> str:
    """One-line human summary for ``diagnose``."""
    if not settings.verify_ssl:
        return "verification DISABLED (LF_VERIFY_SSL=false) — insecure"
    parts = ["operating-system trust store" if settings.use_system_ca else "built-in public CAs"]
    if settings.ca_bundle:
        parts.append(f"+ {settings.ca_bundle}")
    return "verifying against " + " ".join(parts)
