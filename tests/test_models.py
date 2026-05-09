"""Tests for model.from_api translation logic."""

from __future__ import annotations

from laserfiche_mcp.models import (
    EntryDetail,
    EntrySummary,
    EntryType,
    FieldValue,
    SearchResults,
    _pick,
)


def test_pick_distinguishes_missing_from_falsy() -> None:
    assert _pick({"x": 0}, "x", default=99) == 0
    assert _pick({"x": False}, "x", default=99) is False
    assert _pick({"x": ""}, "x", default="default") == ""
    assert _pick({}, "x", default=99) == 99


def test_pick_returns_first_present_key() -> None:
    assert _pick({"id": 1, "Id": 2}, "id", "Id") == 1
    assert _pick({"Id": 2}, "id", "Id") == 2
    assert _pick({}, "id", "Id", default=0) == 0


def test_entry_summary_camelcase() -> None:
    summary = EntrySummary.from_api({
        "id": 42,
        "name": "Smith,John",
        "entryType": "Folder",
        "parentId": 1,
        "fullPath": "\\Imports\\Smith,John",
    })
    assert summary.id == 42
    assert summary.entry_type is EntryType.FOLDER
    assert summary.parent_id == 1


def test_entry_summary_pascalcase() -> None:
    summary = EntrySummary.from_api({
        "Id": 42,
        "Name": "Smith,John",
        "EntryType": "Document",
    })
    assert summary.id == 42
    assert summary.entry_type is EntryType.DOCUMENT


def test_entry_summary_unknown_type_coerced() -> None:
    summary = EntrySummary.from_api({
        "id": 1, "name": "x", "entryType": "ExoticType",
    })
    assert summary.entry_type is EntryType.UNKNOWN


def test_entry_summary_handles_zero_parent_id() -> None:
    """Regression: a falsy `or` chain would silently drop parent_id=0."""
    summary = EntrySummary.from_api({
        "id": 1, "name": "x", "entryType": "Folder", "parentId": 0,
    })
    assert summary.parent_id == 0


def test_field_value_list_from_api() -> None:
    fields = FieldValue.list_from_api({
        "value": [
            {"fieldName": "Status", "values": ["Approved"], "isMultiValue": False},
            {"FieldName": "Tags", "Values": ["a", "b"], "IsMultiValue": True},
        ]
    })
    assert len(fields) == 2
    assert fields[0].field_name == "Status"
    assert fields[1].is_multi_value is True
    assert fields[1].values == ["a", "b"]


def test_entry_detail_with_fields() -> None:
    fields = [FieldValue(field_name="Status", values=["Approved"])]
    detail = EntryDetail.from_api(
        {
            "id": 7, "name": "doc.pdf", "entryType": "Document",
            "templateName": "Application", "isElectronicDocument": True,
            "pageCount": 5, "extension": "pdf",
        },
        fields=fields,
    )
    assert detail.template_name == "Application"
    assert detail.is_electronic_document is True
    assert detail.page_count == 5
    assert detail.fields == fields


def test_entry_detail_explicit_false_preserved() -> None:
    """Regression: `or` chain would drop is_electronic_document=False."""
    detail = EntryDetail.from_api({
        "id": 7, "name": "f", "entryType": "Folder",
        "isElectronicDocument": False,
    })
    assert detail.is_electronic_document is False


def test_search_results_pagination_metadata() -> None:
    results = SearchResults.from_api({
        "value": [{"id": 1, "name": "a", "entryType": "Document"}],
        "@odata.count": 100,
        "@odata.nextLink": "https://lf.test/page2",
    })
    assert len(results.entries) == 1
    assert results.total_count == 100
    assert results.next_link == "https://lf.test/page2"
