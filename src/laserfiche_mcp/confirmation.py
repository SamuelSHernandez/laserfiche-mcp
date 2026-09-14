"""Confirmation tokens for destructive operations.

The MCP protocol is stateless — there's no built-in way to require a human
"are you sure?" between a tool call and its execution. We approximate this
by making destructive tools two-step:

1. First call (preview): the tool returns a structured preview of what will
   happen plus a short-lived signed ``confirmation_token``. Nothing mutates.
2. Second call (execute): caller passes the token back. The server verifies
   the token is bound to the same operation + entry and hasn't expired,
   then executes.

The token is signed with an HMAC secret. By default the secret is
generated per-process at startup, so it cannot be forged externally and
is intentionally NOT persisted across restarts — losing pending
confirmations across a server bounce is the right safety default for a
single-instance stdio deployment.

Set ``LF_CONFIRMATION_SECRET`` to derive a stable signing key from an
operator-supplied secret instead. Tokens then survive server restarts,
and any instance sharing the secret can verify any instance's tokens —
the multi-instance / stateless-MCP deployment shape, where a preview and
its execute call may land on different processes. The trade-off is that
a leaked secret allows token forgery, so treat it like a password.

Bindings carried in the token:
    operation       — e.g. "delete_entry", "rename_entry". A token issued
                      for one operation never validates against another.
    entry_id        — must match on execute.
    entry_name_hash — sha256(entry_name)[:16]. If the entry was renamed
                      between preview and execute (or a different entry now
                      sits at this id), the token is rejected.
    params          — per-parameter hashes of the operation's execute-
                      relevant arguments (page_range for delete_pages,
                      new_name for rename_entry, new_parent_id + new_name
                      for move_entry). The user confirms the *previewed*
                      operation; without this binding an execute call could
                      swap in different parameters ("preview pages 1-2,
                      execute 1-9999") and the token would still verify.
    expiry          — unix seconds. Default 5 min, enough for the LLM to
                      surface the preview to the user and the user to
                      respond, but short enough that a forgotten token
                      can't sit indefinitely waiting to be replayed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from collections.abc import Mapping
from typing import Final

DEFAULT_TTL_SECONDS: Final[int] = 300

# Per-process fallback secret, regenerated on every server start. Used only
# when LF_CONFIRMATION_SECRET is not configured.
_SERVER_SECRET: Final[bytes] = secrets.token_bytes(32)

# Domain-separation label so the derived key can never collide with another
# consumer hashing the same operator secret for a different purpose.
_KDF_LABEL: Final[bytes] = b"laserfiche-mcp.confirmation-token.v1:"


def _signing_key() -> bytes:
    """Resolve the HMAC key: derived from LF_CONFIRMATION_SECRET, else per-process.

    The env var is checked first (matching how ``LF_LEGACY_TOOL_NAMES`` is
    read); when the process was configured through a ``.env`` file that only
    pydantic-settings parsed, the value is picked up from ``Settings``
    instead. Resolved on every call — a few microseconds of SHA-256 — so a
    secret injected or rotated at runtime takes effect immediately and tests
    need no cache reset.
    """
    secret = os.environ.get("LF_CONFIRMATION_SECRET") or None
    if secret is None:
        try:
            from ._app import get_settings  # noqa: PLC0415 — avoid import cycle at load

            configured = get_settings().confirmation_secret
            secret = configured.get_secret_value() if configured else None
        except Exception:  # noqa: BLE001 — no valid Settings (e.g. unit tests)
            secret = None
    if secret:
        return hashlib.sha256(_KDF_LABEL + secret.encode("utf-8")).digest()
    return _SERVER_SECRET


def _entry_name_hash(entry_name: str) -> str:
    return hashlib.sha256(entry_name.encode("utf-8")).hexdigest()[:16]


def _encode_params(params: Mapping[str, object] | None) -> str:
    """Encode operation parameters as sorted per-key hashes.

    ``"-"`` when there are no parameters to bind. Otherwise a comma-joined
    ``key=sha256(str(value))[:12]`` list, sorted by key, so verify can
    name the specific parameter that drifted between preview and execute.
    Values are stringified (``None`` included) — binding is about equality,
    not structure.
    """
    if not params:
        return "-"
    parts = [
        f"{key}={hashlib.sha256(str(value).encode('utf-8')).hexdigest()[:12]}"
        for key, value in sorted(params.items())
    ]
    return ",".join(parts)


def _drifted_param_names(token_seg: str, expected_seg: str) -> list[str]:
    """Name the parameters whose hashes differ between two encoded segments."""

    def parse(seg: str) -> dict[str, str]:
        if seg == "-":
            return {}
        out: dict[str, str] = {}
        for chunk in seg.split(","):
            key, _, value = chunk.partition("=")
            out[key] = value
        return out

    tok, exp = parse(token_seg), parse(expected_seg)
    return sorted(k for k in (set(tok) | set(exp)) if tok.get(k) != exp.get(k))


def _sign(payload: str) -> str:
    return hmac.new(_signing_key(), payload.encode("ascii"), hashlib.sha256).hexdigest()[:32]


def create_token(
    operation: str,
    entry_id: int,
    entry_name: str,
    *,
    params: Mapping[str, object] | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> str:
    """Issue a confirmation token bound to (operation, entry_id, entry_name, params).

    ``params`` are the execute-relevant arguments of the operation (what
    the user actually confirmed in the preview) — e.g. the page_range of a
    delete_pages, the new_name of a rename. Pass the same mapping on
    verify; a drifted value fails validation naming the parameter.

    The returned token is opaque to the caller — they just pass it back on
    the execute call. It encodes binding + expiry + HMAC signature.
    """
    expiry = int(time.time()) + ttl_seconds
    name_hash = _entry_name_hash(entry_name)
    params_seg = _encode_params(params)
    payload = f"{operation}:{entry_id}:{name_hash}:{params_seg}:{expiry}"
    sig = _sign(payload)
    raw = f"{payload}:{sig}".encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def verify_token(
    token: str,
    operation: str,
    entry_id: int,
    entry_name: str,
    *,
    params: Mapping[str, object] | None = None,
) -> tuple[bool, str | None]:
    """Validate a confirmation token.

    ``params`` must be the same execute-relevant arguments the preview
    bound (see :func:`create_token`); a drifted value fails with a reason
    naming the parameter.

    Returns ``(ok, reason)``. If ``ok`` is False, ``reason`` is a
    human-readable explanation suitable for surfacing back to the LLM.
    """
    try:
        padded = token + "=" * (-len(token) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("ascii")
    except (ValueError, UnicodeDecodeError):
        return False, "Token is not valid base64."

    parts = decoded.split(":")
    if len(parts) != 6:
        return False, "Token is structurally invalid."

    tok_op, tok_id_str, tok_name_hash, tok_params_seg, tok_exp_str, tok_sig = parts
    try:
        tok_id = int(tok_id_str)
        tok_exp = int(tok_exp_str)
    except ValueError:
        return False, "Token contains non-integer fields."

    payload = f"{tok_op}:{tok_id}:{tok_name_hash}:{tok_params_seg}:{tok_exp}"
    expected = _sign(payload)
    if not hmac.compare_digest(expected, tok_sig):
        return (
            False,
            "Token signature does not match (forged, tampered, or from a "
            "prior server instance — unless LF_CONFIRMATION_SECRET is set, "
            "server restarts invalidate pending tokens).",
        )

    if tok_op != operation:
        return False, (f"Token was issued for operation {tok_op!r}, not {operation!r}.")
    if tok_id != entry_id:
        return False, (f"Token was issued for entry {tok_id}, not {entry_id}.")
    if tok_name_hash != _entry_name_hash(entry_name):
        return False, (
            "Entry name no longer matches the token's binding — the entry "
            "may have been renamed, or a different entry now sits at this "
            "id. Call the tool again without confirmation_token to get a "
            "fresh preview."
        )

    expected_params_seg = _encode_params(params)
    if tok_params_seg != expected_params_seg:
        drifted = _drifted_param_names(tok_params_seg, expected_params_seg)
        which = ", ".join(drifted) if drifted else "operation parameters"
        return False, (
            f"Token parameter binding mismatch: {which} differ(s) from the "
            "previewed call. The user confirmed the preview, not these "
            "arguments. Call the tool again without confirmation_token to "
            "get a fresh preview."
        )

    if time.time() >= tok_exp:
        return False, (
            "Token expired. Call the tool again without confirmation_token "
            f"to get a fresh preview and token (TTL is {DEFAULT_TTL_SECONDS} "
            "seconds)."
        )

    return True, None
