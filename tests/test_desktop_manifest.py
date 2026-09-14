"""Tests for ``manifest.json`` (the Claude Desktop extension manifest).

Guards against the tool list in ``manifest.json["tools"]`` drifting from
the actual registered tool names — a stale name there is silently wrong
(the Desktop extension's tool-consent screen just shows a name nothing
ever calls) with no test anywhere else to catch it.
"""

from __future__ import annotations

import json
from pathlib import Path

from laserfiche_mcp import server

_MANIFEST_PATH = Path(__file__).parent.parent / "manifest.json"


def _load_manifest_tool_names() -> list[str]:
    data = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
    return [t["name"] for t in data["tools"]]


def test_manifest_tools_are_real_registered_names() -> None:
    """Every name in manifest.json's tools list must match a real v2 (or
    legacy) tool name — catches the exact stale-name drift the v2.3.0
    audit flagged (laserfiche_search, laserfiche_document_edoc_get)."""
    known = {s.v2_name for s in server.all_tools()} | {s.legacy_name for s in server.all_tools()}
    manifest_names = _load_manifest_tool_names()
    assert manifest_names, "manifest.json tools list must not be empty"
    unknown = [n for n in manifest_names if n not in known]
    assert not unknown, f"manifest.json lists tool name(s) that don't exist: {unknown}"


def test_manifest_tools_are_all_reads() -> None:
    """manifest.json documents the read-only default experience — every
    listed tool must be a non-write tool (writes need LF_READ_ONLY=false
    explicitly, which the Desktop extension's default config doesn't set)."""
    write_names = {s.v2_name for s in server.all_tools() if s.is_write} | {
        s.legacy_name for s in server.all_tools() if s.is_write
    }
    manifest_names = _load_manifest_tool_names()
    assert not (set(manifest_names) & write_names)
