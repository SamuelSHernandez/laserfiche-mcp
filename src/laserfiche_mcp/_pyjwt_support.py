"""Shared lazy-import guard for the optional PyJWT dependency.

Leaf module: both :mod:`auth` (Cloud service-app JWT assertions) and
:mod:`oauth` (Resource Server token verification) need PyJWT and neither
should import it from the other just to share this guard — that backwards
dependency also meant a Cloud-auth failure blamed the unrelated ``--http``
OAuth Resource Server feature in its error message.
"""

from __future__ import annotations

from typing import Any

_INSTALL_HINT = "Install the extra: pip install 'laserfiche-mcp[oauth]'"


def require_pyjwt(feature: str) -> Any:
    """Import PyJWT lazily, raising a feature-specific message if it's missing."""
    try:
        import jwt  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RuntimeError(f"{feature} needs PyJWT. {_INSTALL_HINT}") from exc
    return jwt
