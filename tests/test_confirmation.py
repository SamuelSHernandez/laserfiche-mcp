"""Tests for the confirmation-token module used by destructive tools."""

from __future__ import annotations

import time

import pytest

from laserfiche_mcp import confirmation


def test_create_and_verify_roundtrip() -> None:
    token = confirmation.create_token("delete_entry", 42, "Foo")
    ok, reason = confirmation.verify_token(token, "delete_entry", 42, "Foo")
    assert ok is True
    assert reason is None


def test_token_is_opaque_base64url_safe() -> None:
    token = confirmation.create_token("delete_entry", 1, "x")
    # Base64url tokens are URL-safe; no padding required for input.
    assert "/" not in token
    assert "+" not in token
    assert "=" not in token


def test_verify_rejects_wrong_operation() -> None:
    token = confirmation.create_token("delete_entry", 42, "Foo")
    ok, reason = confirmation.verify_token(token, "rename_entry", 42, "Foo")
    assert ok is False
    assert reason is not None
    assert "delete_entry" in reason
    assert "rename_entry" in reason


def test_verify_rejects_wrong_entry_id() -> None:
    token = confirmation.create_token("delete_entry", 42, "Foo")
    ok, reason = confirmation.verify_token(token, "delete_entry", 43, "Foo")
    assert ok is False
    assert reason is not None
    assert "42" in reason
    assert "43" in reason


def test_verify_rejects_wrong_entry_name() -> None:
    token = confirmation.create_token("delete_entry", 42, "Foo")
    ok, reason = confirmation.verify_token(token, "delete_entry", 42, "Bar")
    assert ok is False
    assert reason is not None
    assert "renamed" in reason.lower() or "no longer matches" in reason.lower()


def test_verify_rejects_garbage_token() -> None:
    ok, reason = confirmation.verify_token("not-a-real-token", "delete_entry", 1, "x")
    assert ok is False
    assert reason is not None


def test_verify_rejects_tampered_token() -> None:
    token = confirmation.create_token("delete_entry", 42, "Foo")
    # Flip a character mid-token to corrupt the signature.
    mid = len(token) // 2
    tampered = token[:mid] + ("A" if token[mid] != "A" else "B") + token[mid + 1 :]
    ok, reason = confirmation.verify_token(tampered, "delete_entry", 42, "Foo")
    assert ok is False
    assert reason is not None


def test_verify_rejects_expired_token(monkeypatch: pytest.MonkeyPatch) -> None:
    token = confirmation.create_token(
        "delete_entry", 42, "Foo", ttl_seconds=1,
    )
    # Fast-forward past the expiry.
    real_time = time.time
    monkeypatch.setattr(
        confirmation.time, "time", lambda: real_time() + 10,
    )
    ok, reason = confirmation.verify_token(token, "delete_entry", 42, "Foo")
    assert ok is False
    assert reason is not None
    assert "expir" in reason.lower()


def test_tokens_differ_per_entry() -> None:
    a = confirmation.create_token("delete_entry", 1, "Foo")
    b = confirmation.create_token("delete_entry", 2, "Foo")
    assert a != b


def test_tokens_differ_per_operation() -> None:
    a = confirmation.create_token("delete_entry", 1, "Foo")
    b = confirmation.create_token("rename_entry", 1, "Foo")
    assert a != b


def test_token_rejected_when_structurally_short() -> None:
    # Valid base64 but the decoded payload doesn't have the right parts.
    import base64
    bad = base64.urlsafe_b64encode(b"only:three:parts").decode().rstrip("=")
    ok, reason = confirmation.verify_token(bad, "delete_entry", 1, "x")
    assert ok is False
    assert reason is not None
    assert "structurally" in reason.lower()


def test_token_rejected_with_non_integer_fields() -> None:
    import base64
    bad = base64.urlsafe_b64encode(
        b"delete_entry:notanint:abc:123:sig"
    ).decode().rstrip("=")
    ok, reason = confirmation.verify_token(bad, "delete_entry", 1, "x")
    assert ok is False
    assert reason is not None
    assert "integer" in reason.lower()
