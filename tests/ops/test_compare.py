"""Tests for ``ops/compare.py`` — entry metadata and field diffing."""

from __future__ import annotations

from typing import Any

from laserfiche_mcp.ops.compare import compare_entries


def _entry(entry_id: int, **attrs: Any) -> dict[str, Any]:
    return {"id": entry_id, "entryType": "Document", **attrs}


def _fields(**named: list[Any]) -> dict[str, Any]:
    return {
        "value": [
            {"fieldName": name, "values": values, "fieldType": "String"}
            for name, values in named.items()
        ]
    }


def test_identical_entries_report_no_differences() -> None:
    left = _entry(1, name="a.pdf", templateName="Invoice")
    right = _entry(2, name="a.pdf", templateName="Invoice")

    result = compare_entries(left, right)

    assert result.identical is True
    assert "name" in result.same


def test_differing_attribute_is_reported_with_both_values() -> None:
    result = compare_entries(_entry(1, name="a.pdf"), _entry(2, name="b.pdf"))

    assert result.identical is False
    difference = result.differences[0]
    assert difference.kind == "attribute"
    assert (difference.left, difference.right) == ("a.pdf", "b.pdf")


def test_pascal_case_payloads_compare_the_same_as_camel_case() -> None:
    """The API has been inconsistent across versions; a diff must not
    report every field as missing because of casing."""
    left = {"Id": 1, "Name": "a.pdf", "EntryType": "Document"}
    right = {"id": 2, "name": "a.pdf", "entryType": "Document"}

    result = compare_entries(left, right)

    assert result.identical is True


def test_field_values_that_disagree_are_reported_as_field_differences() -> None:
    result = compare_entries(
        _entry(1),
        _entry(2),
        left_fields=_fields(Status=["Approved"]),
        right_fields=_fields(Status=["Pending"]),
    )

    difference = next(d for d in result.differences if d.kind == "field")
    assert difference.name == "Status"
    assert (difference.left, difference.right) == (["Approved"], ["Pending"])


def test_a_field_present_on_only_one_side_is_reported_separately() -> None:
    """'You forgot to fill this in' is a different problem from 'these disagree'."""
    result = compare_entries(
        _entry(1),
        _entry(2),
        left_fields=_fields(Status=["Approved"], Reviewer=["Sam"]),
        right_fields=_fields(Status=["Approved"]),
    )

    assert result.only_left_fields == ["Reviewer"]
    assert result.only_right_fields == []
    assert result.differences == []
    assert result.identical is False


def test_matching_fields_land_in_same_not_differences() -> None:
    result = compare_entries(
        _entry(1),
        _entry(2),
        left_fields=_fields(Status=["Approved"]),
        right_fields=_fields(Status=["Approved"]),
    )

    assert "Status" in result.same
    assert result.identical is True


def test_omitting_field_payloads_compares_attributes_only() -> None:
    result = compare_entries(_entry(1, name="same.pdf"), _entry(2, name="same.pdf"))

    assert result.only_left_fields == []
    assert result.identical is True


def test_attributes_absent_on_both_sides_are_ignored() -> None:
    """Two entries with no template shouldn't report templateName as 'same'."""
    result = compare_entries(_entry(1), _entry(2))

    assert "templateName" not in result.same
    assert "extension" not in result.same


def test_result_carries_both_entry_ids() -> None:
    result = compare_entries(_entry(11), _entry(22))

    assert (result.left_id, result.right_id) == (11, 22)
