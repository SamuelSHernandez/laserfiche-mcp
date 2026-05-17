"""Shared helper for the per-mixin client-package tests.

The constants ``_BASE_V1``, ``_BASE_V2``, ``_BASE`` and the ``_StubAuth``
class come from ``tests/conftest.py`` (one level up). This conftest only
adds ``_build_client``, used by every test in this directory.
"""

from __future__ import annotations

from laserfiche_mcp.client import LaserficheClient
from laserfiche_mcp.config import Settings
from tests.conftest import _StubAuth


def _build_client(settings: Settings) -> LaserficheClient:
    """Construct a ``LaserficheClient`` with the stub auth used by client tests."""
    return LaserficheClient(settings, _StubAuth())
