"""Diff two entries' metadata.

"What's different between these two records?" is a set comparison over two
dicts. Shipping both records into a context window so a model can eyeball
them is strictly worse than computing it: more expensive, and occasionally
wrong about which of forty fields actually differ.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..models import FieldValue

# Entry attributes worth comparing. Timestamps and IDs are excluded by
# default — two copies of the same document differing only in creation time
# is noise, not a finding.
COMPARED_ATTRIBUTES = ("name", "entryType", "templateName", "extension", "pageCount")


def _pick(raw: dict[str, Any], key: str) -> Any:
    """Read a key in either camelCase or PascalCase, as the API varies."""
    if key in raw:
        return raw[key]
    pascal = key[0].upper() + key[1:]
    return raw.get(pascal)


@dataclass
class Difference:
    """One attribute or field that differs between the two entries."""

    kind: str
    """``"attribute"`` for entry metadata, ``"field"`` for a template field."""
    name: str
    left: Any
    right: Any


@dataclass
class ComparisonResult:
    left_id: int
    right_id: int
    differences: list[Difference] = field(default_factory=list)
    same: list[str] = field(default_factory=list)
    only_left_fields: list[str] = field(default_factory=list)
    only_right_fields: list[str] = field(default_factory=list)

    @property
    def identical(self) -> bool:
        return not (self.differences or self.only_left_fields or self.only_right_fields)


def _field_map(raw: dict[str, Any]) -> dict[str, list[Any]]:
    """Flatten a field-values payload into ``{field_name: [values]}``."""
    out: dict[str, list[Any]] = {}
    for value in FieldValue.list_from_api(raw):
        out[value.field_name] = list(value.values)
    return out


def compare_entries(
    left_entry: dict[str, Any],
    right_entry: dict[str, Any],
    *,
    left_fields: dict[str, Any] | None = None,
    right_fields: dict[str, Any] | None = None,
) -> ComparisonResult:
    """Compare two entries' attributes and template fields.

    ``left_fields`` / ``right_fields`` are raw ``get_field_values`` payloads;
    omit them to compare attributes only.

    A field present on one entry and absent on the other is reported
    separately from a field present on both with different values — "you
    forgot to fill this in" and "these disagree" are different problems.
    """
    result = ComparisonResult(
        left_id=int(_pick(left_entry, "id") or 0),
        right_id=int(_pick(right_entry, "id") or 0),
    )

    for attribute in COMPARED_ATTRIBUTES:
        left_value = _pick(left_entry, attribute)
        right_value = _pick(right_entry, attribute)
        if left_value is None and right_value is None:
            continue
        if left_value == right_value:
            result.same.append(attribute)
        else:
            result.differences.append(
                Difference(kind="attribute", name=attribute, left=left_value, right=right_value)
            )

    if left_fields is None and right_fields is None:
        return result

    left_map = _field_map(left_fields or {})
    right_map = _field_map(right_fields or {})

    for name in sorted(set(left_map) | set(right_map)):
        if name not in right_map:
            result.only_left_fields.append(name)
        elif name not in left_map:
            result.only_right_fields.append(name)
        elif left_map[name] == right_map[name]:
            result.same.append(name)
        else:
            result.differences.append(
                Difference(kind="field", name=name, left=left_map[name], right=right_map[name])
            )

    return result
