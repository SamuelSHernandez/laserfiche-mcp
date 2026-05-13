"""Confirmation tokens for destructive operations.

The MCP protocol is stateless — there's no built-in way to require a human
"are you sure?" between a tool call and its execution. We approximate this
by making destructive tools two-step:

1. First call (preview): the tool returns a structured preview of what will
   happen plus a short-lived signed ``confirmation_token``. Nothing mutates.
2. Second call (execute): caller passes the token back. The server verifies
   the token is bound to the same operation + entry and hasn't expired,
   then executes.

The token is signed with a per-process HMAC secret generated at startup,
so it cannot be forged externally. The secret is intentionally NOT
persisted across restarts — losing pending confirmations across a server
bounce is the right safety default.

Bindings carried in the token:
    operation       — e.g. "delete_entry", "rename_entry". A token issued
                      for one operation never validates against another.
    entry_id        — must match on execute.
    entry_name_hash — sha256(entry_name)[:16]. If the entry was renamed
                      between preview and execute (or a different entry now
                      sits at this id), the token is rejected.
    expiry          — unix seconds. Default 5 min, enough for the LLM to
                      surface the preview to the user and the user to
                      respond, but short enough that a forgotten token
                      can't sit indefinitely waiting to be replayed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
from typing import Final

DEFAULT_TTL_SECONDS: Final[int] = 300

# Per-process secret, regenerated on every server start.
_SERVER_SECRET: Final[bytes] = secrets.token_bytes(32)


def _entry_name_hash(entry_name: str) -> str:
    return hashlib.sha256(entry_name.encode("utf-8")).hexdigest()[:16]


def _sign(payload: str) -> str:
    return hmac.new(
        _SERVER_SECRET, payload.encode("ascii"), hashlib.sha256
    ).hexdigest()[:32]


def create_token(
    operation: str,
    entry_id: int,
    entry_name: str,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> str:
    """Issue a confirmation token bound to (operation, entry_id, entry_name).

    The returned token is opaque to the caller — they just pass it back on
    the execute call. It encodes binding + expiry + HMAC signature.
    """
    expiry = int(time.time()) + ttl_seconds
    name_hash = _entry_name_hash(entry_name)
    payload = f"{operation}:{entry_id}:{name_hash}:{expiry}"
    sig = _sign(payload)
    raw = f"{payload}:{sig}".encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def verify_token(
    token: str,
    operation: str,
    entry_id: int,
    entry_name: str,
) -> tuple[bool, str | None]:
    """Validate a confirmation token.

    Returns ``(ok, reason)``. If ``ok`` is False, ``reason`` is a
    human-readable explanation suitable for surfacing back to the LLM.
    """
    try:
        padded = token + "=" * (-len(token) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii")
    except (ValueError, UnicodeDecodeError):
        return False, "Token is not valid base64."

    parts = decoded.split(":")
    if len(parts) != 5:
        return False, "Token is structurally invalid."

    tok_op, tok_id_str, tok_name_hash, tok_exp_str, tok_sig = parts
    try:
        tok_id = int(tok_id_str)
        tok_exp = int(tok_exp_str)
    except ValueError:
        return False, "Token contains non-integer fields."

    payload = f"{tok_op}:{tok_id}:{tok_name_hash}:{tok_exp}"
    expected = _sign(payload)
    if not hmac.compare_digest(expected, tok_sig):
        return (
            False,
            "Token signature does not match (forged, tampered, or from a "
            "prior server instance — server restarts invalidate pending tokens).",
        )

    if tok_op != operation:
        return False, (
            f"Token was issued for operation {tok_op!r}, not {operation!r}."
        )
    if tok_id != entry_id:
        return False, (
            f"Token was issued for entry {tok_id}, not {entry_id}."
        )
    if tok_name_hash != _entry_name_hash(entry_name):
        return False, (
            "Entry name no longer matches the token's binding — the entry "
            "may have been renamed, or a different entry now sits at this "
            "id. Re-run with dry_run=true to get a fresh preview."
        )

    if time.time() >= tok_exp:
        return False, (
            "Token expired. Re-run with dry_run=true to get a fresh preview "
            f"and token (TTL is {DEFAULT_TTL_SECONDS} seconds)."
        )

    return True, None
