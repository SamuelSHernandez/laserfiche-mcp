"""Tests for ``laserfiche_mcp.errors`` — exception hierarchy + classifier.

``classify_lf_error`` is the central error contract for every tool wrapper,
so each subkind has direct coverage here in addition to the transitive
coverage that the per-tool tests provide. ``kind_for_subkind`` and
``lf_error_detail`` are the supporting helpers; both are exercised here.
"""

from __future__ import annotations

from laserfiche_mcp import errors
from laserfiche_mcp.errors import LaserficheError


def _err(status: int | None, detail: object | None = None) -> LaserficheError:
    return LaserficheError("test error", status_code=status, detail=detail)


# --- classify_lf_error: per-slug mapping ------------------------------------


def test_classify_lf_error_401_is_auth_failed() -> None:
    r = errors.classify_lf_error("get_entry", _err(401))
    assert r["error"] == "auth_failed"
    assert r["status_code"] == 401


def test_classify_lf_error_403_is_auth_failed() -> None:
    r = errors.classify_lf_error("get_entry", _err(403))
    assert r["error"] == "auth_failed"


def test_classify_lf_error_lf_code_9010_is_auth_failed() -> None:
    # LF-specific code overrides HTTP status interpretation: 9010 means
    # invalid credentials even on a 400.
    r = errors.classify_lf_error("get_entry", _err(400, {"errorCode": 9010}))
    assert r["error"] == "auth_failed"
    assert r["server_error_code"] == 9010


def test_classify_lf_error_lf_code_9528_treated_as_auth_failed() -> None:
    # 9528 is misleadingly worded ('LFDS unreachable') but most often
    # means bad creds; the reason text should reflect that.
    r = errors.classify_lf_error("get_entry", _err(400, {"errorCode": 9528}))
    assert r["error"] == "auth_failed"
    assert "9528" in r["reason"]


def test_classify_lf_error_9066_is_required_field_missing() -> None:
    r = errors.classify_lf_error("assign_template", _err(400, {"errorCode": 9066}))
    assert r["error"] == "required_field_missing"


def test_classify_lf_error_9039_is_required_field_missing() -> None:
    r = errors.classify_lf_error("assign_template", _err(400, {"errorCode": 9039}))
    assert r["error"] == "required_field_missing"


def test_classify_lf_error_404_is_not_found() -> None:
    r = errors.classify_lf_error("get_entry", _err(404))
    assert r["error"] == "not_found"


def test_classify_lf_error_405_is_method_not_allowed() -> None:
    r = errors.classify_lf_error("delete_entry", _err(405))
    assert r["error"] == "method_not_allowed"


def test_classify_lf_error_415_is_unsupported_media_type() -> None:
    r = errors.classify_lf_error("delete_entry", _err(415))
    assert r["error"] == "unsupported_media_type"


def test_classify_lf_error_429_is_rate_limited() -> None:
    r = errors.classify_lf_error("get_entry", _err(429))
    assert r["error"] == "rate_limited"


def test_classify_lf_error_500_is_server_error() -> None:
    r = errors.classify_lf_error("get_entry", _err(500))
    assert r["error"] == "server_error"


def test_classify_lf_error_502_is_server_error() -> None:
    r = errors.classify_lf_error("get_entry", _err(502))
    assert r["error"] == "server_error"


def test_classify_lf_error_unknown_status_falls_back_to_server_error() -> None:
    # Network error before HTTP status: detail=None, status_code=None.
    r = errors.classify_lf_error("get_entry", _err(None))
    assert r["error"] == "server_error"


def test_classify_lf_error_includes_entry_id_when_supplied() -> None:
    r = errors.classify_lf_error("get_entry", _err(404), entry_id=42)
    assert r["entry_id"] == 42


def test_classify_lf_error_extra_dict_merged_into_response() -> None:
    r = errors.classify_lf_error(
        "copy_entry",
        _err(404),
        extra={"parent_id": 100, "name": "X"},
    )
    assert r["parent_id"] == 100
    assert r["name"] == "X"


def test_classify_lf_error_extracts_title_from_problem_details() -> None:
    r = errors.classify_lf_error(
        "get_entry",
        _err(400, {"errorCode": 216, "title": "Bad parameter"}),
    )
    assert r["server_message"] == "Bad parameter"


# --- lf_error_detail: ProblemDetails unwrapping -----------------------------


def test_lf_error_detail_handles_nested_error_wrapper() -> None:
    # The Edoc DELETE routes return `{error: {code, message}}` instead of
    # the usual flat ProblemDetails. Detail extractor should merge the
    # inner dict so callers see a uniform shape.
    exc = _err(405, {"error": {"code": "UnsupportedApiVersion", "message": "no DELETE"}})
    detail = errors.lf_error_detail(exc)
    assert detail["code"] == "UnsupportedApiVersion"
    assert detail["message"] == "no DELETE"


def test_lf_error_detail_returns_empty_for_non_dict_detail() -> None:
    # Plaintext bodies leave detail as a string; helper should yield {}.
    exc = _err(500, "Internal Server Error")
    assert errors.lf_error_detail(exc) == {}


# --- v2.0 error model: kind + subkind + request_id + upstream_trace_id ------


def test_classify_lf_error_includes_canonical_kind() -> None:
    r = errors.classify_lf_error("get_entry", _err(404))
    assert r["kind"] == "not_found"
    assert r["error"] == "not_found"  # subkind


def test_classify_lf_error_includes_request_id() -> None:
    import uuid as _uuid

    r = errors.classify_lf_error("get_entry", _err(404))
    # Validate it's a parseable UUID4 string.
    parsed = _uuid.UUID(r["request_id"])
    assert parsed.version == 4


def test_classify_lf_error_surfaces_upstream_trace_id() -> None:
    exc = _err(
        400,
        {
            "errorCode": 216,
            "title": "Bad request",
            "traceId": "00-92647f26a3ce0eefba0bb9b5b8b7997c-86f93f712586c4d7-00",
        },
    )
    r = errors.classify_lf_error("get_entry", exc)
    assert r["upstream_trace_id"] == "00-92647f26a3ce0eefba0bb9b5b8b7997c-86f93f712586c4d7-00"


def test_classify_lf_error_trace_id_null_when_absent() -> None:
    r = errors.classify_lf_error("get_entry", _err(404))
    assert r["upstream_trace_id"] is None


# --- kind_for_subkind: canonical 5-kind taxonomy ----------------------------


def test_kind_for_subkind_maps_all_invalid_input_slugs() -> None:
    """Spot-check the subkind→kind mapping."""
    invalid_input_subkinds = [
        "required_field_missing",
        "missing_required_fields",
        "invalid_confirmation_token",
        "exceeds_batch_cap",
        "audit_reason_required",
        "invalid_page_range",
        "invalid_name",
        "invalid_field_name",
        "invalid_template_name",
        "invalid_tag_name",
        "invalid_link_type",
    ]
    for sub in invalid_input_subkinds:
        assert errors.kind_for_subkind(sub) == "invalid_input", sub


def test_kind_for_subkind_permission_denied_slugs() -> None:
    for sub in ("auth_failed", "path_not_allowed", "tool_not_allowed"):
        assert errors.kind_for_subkind(sub) == "permission_denied", sub


def test_kind_for_subkind_unknown_falls_back_to_upstream_unavailable() -> None:
    assert errors.kind_for_subkind("not_a_real_subkind") == "upstream_unavailable"
