"""verify_token must be total: it can be handed anything and never raises."""

from __future__ import annotations

import base64

from hypothesis import given, settings
from hypothesis import strategies as st

from laserfiche_mcp import confirmation


@settings(max_examples=300, deadline=None)
@given(token=st.text(max_size=300))
def test_arbitrary_text_never_raises_and_never_verifies(token: str) -> None:
    ok, reason = confirmation.verify_token(token, "delete_entry", 1, "name")
    assert ok is False
    assert isinstance(reason, str) and reason


@settings(max_examples=200, deadline=None)
@given(raw=st.binary(max_size=200))
def test_arbitrary_bytes_encoded_as_a_token_never_raise(raw: bytes) -> None:
    token = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    ok, _ = confirmation.verify_token(token, "delete_entry", 1, "name")
    assert ok is False


@settings(max_examples=200, deadline=None)
@given(
    parts=st.lists(
        st.text(alphabet=st.characters(codec="ascii"), max_size=30), min_size=0, max_size=9
    )
)
def test_structurally_plausible_tokens_never_raise(parts: list[str]) -> None:
    token = base64.urlsafe_b64encode(":".join(parts).encode("ascii")).decode("ascii")
    ok, _ = confirmation.verify_token(token, "delete_entry", 1, "name")
    assert ok is False


def test_non_string_tokens_fail_closed() -> None:
    for bad in (None, 123, b"bytes", ["x"], {"a": 1}):
        ok, reason = confirmation.verify_token(bad, "delete_entry", 1, "name")  # type: ignore[arg-type]
        assert ok is False and reason


def test_a_genuine_token_still_verifies_and_is_still_bound() -> None:
    token = confirmation.create_token("delete_entry", 5, "Doc", params={"page_range": "1-2"})
    assert confirmation.verify_token(token, "delete_entry", 5, "Doc", params={"page_range": "1-2"})[
        0
    ]
    assert not confirmation.verify_token(
        token, "delete_entry", 6, "Doc", params={"page_range": "1-2"}
    )[0]
    assert not confirmation.verify_token(
        token, "delete_entry", 5, "Doc", params={"page_range": "1-9"}
    )[0]


def test_internal_failure_during_verification_fails_closed(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    token = confirmation.create_token("delete_entry", 5, "Doc")

    def boom(payload: str) -> str:
        raise RuntimeError("signing backend exploded")

    monkeypatch.setattr(confirmation, "_sign", boom)
    ok, reason = confirmation.verify_token(token, "delete_entry", 5, "Doc")
    assert ok is False and "fresh preview" in (reason or "")
