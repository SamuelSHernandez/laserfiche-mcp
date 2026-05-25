# PLAN_DESIGN.md — laserfiche-mcp Pass 3: Tool Design

Source: `AUDIT_DESIGN.md`. Companions: `PLAN.md` (surface),
`PLAN_ERRORS.md` (error model).

Target release: v2.0.0.

This file holds the **canonical per-tool spec** for the v2 surface:
final names, descriptions written for an LLM reader, input schemas
with `Field(description=...)` and `examples=[...]`, output schemas,
limit/pagination policy, `response_format` policy, composability
hints.

---

## 2a. Final naming scheme

### Convention

`laserfiche_{resource}_{verb}` — resource segment first, verb last.
Snake-case throughout.

### Rules

1. Every tool starts with `laserfiche_`. No exceptions.
2. Resource segments are **singular** (`entry`, not `entries`).
3. Compound resources are **snake_case** (`field_definition`,
   `document_edoc`, `document_pages`).
4. Verbs are intent-oriented, not HTTP/REST verbs:
   - **Read intent:** `get` (one), `list` (bounded set), `search`
     (unbounded), `wait` (poll).
   - **Write intent:** `create`, `update` (delta or replace), `assign`
     (one-time apply), `delete` (irreversible), `copy`, `rename`,
     `move`, `import`.
   - **Two-phase verbs:** `_preview` and `_execute` suffixes on
     destructive ops.
5. Avoid REST-style names (`entry.get`, `entries_GET`, etc.).

### Complete rename map

| Old name (v1.5) | New name (v2.0) |
|---|---|
| `search_entries` | `laserfiche_entry_search` |
| `search_by_name` | `laserfiche_entry_search_by_name` |
| `search_natural` | `laserfiche_entry_search_natural` |
| `list_folder` | `laserfiche_folder_list` |
| `get_entry` | `laserfiche_entry_get` |
| `get_entry_by_path` | `laserfiche_entry_get_by_path` |
| `get_field_values` | `laserfiche_field_values_get` |
| `get_document_text` | `laserfiche_document_get_text` |
| `get_document_edoc(mode="info")` | `laserfiche_document_get_info` |
| `get_document_edoc(mode="bytes")` | `laserfiche_document_get_bytes` |
| `get_document_edoc(mode="text")` | `laserfiche_document_get_extracted_text` |
| `list_repositories` | `laserfiche_repository_list` |
| `list_field_definitions` | `laserfiche_field_definition_list` |
| (new) | `laserfiche_template_field_list` |
| `list_tag_definitions` | `laserfiche_tag_definition_list` |
| `list_template_definitions` | `laserfiche_template_definition_list` |
| `list_link_definitions` | `laserfiche_link_definition_list` |
| `get_audit_reasons` | `laserfiche_audit_reason_list` |
| `get_task_status` + `wait_for_task` | `laserfiche_task_wait` |
| `set_fields` + `merge_fields` | `laserfiche_field_update` |
| `set_tags` + `merge_tags` | `laserfiche_tag_update` |
| `set_links` | `laserfiche_link_update` |
| `assign_template` + `remove_template` | `laserfiche_template_assign` |
| `create_folder` | `laserfiche_folder_create` |
| `copy_entry` | `laserfiche_entry_copy` |
| `import_document` | `laserfiche_document_import` |
| `rename_entry` | `laserfiche_entry_rename_preview` + `laserfiche_entry_rename_execute` |
| `move_entry` | `laserfiche_entry_move_preview` + `laserfiche_entry_move_execute` |
| `delete_entry` | `laserfiche_entry_delete_preview` + `laserfiche_entry_delete_execute` |
| `delete_edoc` | `laserfiche_document_edoc_delete_preview` + `laserfiche_document_edoc_delete_execute` |
| `delete_pages` | `laserfiche_document_pages_delete_preview` + `laserfiche_document_pages_delete_execute` |

---

## 2b. Per-tool full design

For brevity, this section gives **full specs for representative tools
covering every pattern** (single-resource get, paginated search,
update with mode enum, preview+execute pair, multi-mode-split read).
The remaining tools follow the patterns by mechanical extension; the
execution phase produces the complete docstrings + Field specs by
applying the same templates.

### Pattern A — Single-resource get (`laserfiche_entry_get`)

```python
@mcp.tool()
async def laserfiche_entry_get(
    entry_id: Annotated[int, Field(
        description=(
            "Integer entry ID. Get this from "
            "laserfiche_entry_search, laserfiche_folder_list, or "
            "laserfiche_entry_get_by_path."
        ),
        examples=[84493, 1],
        gt=0,
    )],
    include_fields: Annotated[list[str] | None, Field(
        default=None,
        description=(
            "If provided, response is projected to only these "
            "field names (in addition to id, which is always "
            "included). Use to slim large responses when you "
            "only need a few attributes."
        ),
        examples=[["id", "name", "entry_type"]],
    )] = None,
    response_format: Annotated[Literal["concise", "detailed"], Field(
        default="concise",
        description=(
            "concise (default): id, name, entry_type, full_path, "
            "template_name. Use for tool chaining where you'll "
            "follow up with another get. "
            "detailed: also creator, creation_time, "
            "last_modified_time, page_count, "
            "is_electronic_document, extension. Use when you need "
            "to show the user metadata. Concise ~150 bytes; "
            "detailed ~500 bytes per entry."
        ),
    )] = "concise",
) -> dict[str, Any] | ToolError:
    """Fetch metadata for one entry by ID.

    Use this once you have an entry ID (from search, list, or path
    resolution) and need its full metadata. Cheap call (~150 bytes
    concise, ~500 bytes detailed). Chains naturally with
    laserfiche_field_values_get (for template fields) or
    laserfiche_document_get_text (for the body of a document entry).

    Do NOT use:
    - To enumerate folder contents — use laserfiche_folder_list.
    - To verify a path exists — use laserfiche_entry_get_by_path.
    - To read a document's content — use laserfiche_document_get_text
      (v2 servers) or laserfiche_document_get_extracted_text (v1).

    Returns: {id, name, entry_type, full_path, template_name,
    creator?, creation_time?, last_modified_time?, page_count?,
    is_electronic_document?, extension?}. Optional fields populated
    only when response_format="detailed".

    On failure: ToolError with kind=not_found if the entry doesn't
    exist, kind=permission_denied if the service account lacks
    read access. See docs/error-contract.md for the full slug
    taxonomy.
    """
```

### Pattern B — Paginated search (`laserfiche_entry_search`)

```python
@mcp.tool()
async def laserfiche_entry_search(
    query: Annotated[str, Field(
        description=(
            "Laserfiche search query. Quote string values with "
            "double quotes; escape inner quotes by doubling them. "
            "If you don't know the syntax, use "
            "laserfiche_entry_search_natural for LLM-guided "
            "construction."
        ),
        examples=[
            '{LF:Name="Onboarding*"}',
            '{[Loan Application]:[Last Name]="Smith"}',
            '{LF:Name="*.pdf"} & {[Application]:[Status]="Approved"}',
        ],
    )],
    limit: Annotated[int, Field(
        default=20,
        description=(
            "Page size. Hard cap LF_MAX_RESULTS_CEILING (200). "
            "Default 20 is sized for typical context budgets; "
            "raise only when you genuinely need more in one call."
        ),
        ge=1, le=200,
    )] = 20,
    cursor: Annotated[str | None, Field(
        default=None,
        description=(
            "Opaque continuation token from a previous response's "
            "next_link. Use to page through large result sets."
        ),
    )] = None,
    response_format: Annotated[Literal["concise", "detailed"], Field(
        default="concise",
        description=(
            "concise (default): id, name, entry_type, full_path. "
            "~150 bytes per result. "
            "detailed: also creation_time, last_modified_time, "
            "parent_id, template_name. ~400 bytes per result."
        ),
    )] = "concise",
) -> dict[str, Any] | ToolError:
    """Run a Laserfiche query and return matching entries.

    Use when you know how to express the search in Laserfiche query
    syntax. If you don't, use laserfiche_entry_search_natural first
    — it returns the available templates and grammar so you can
    construct a valid query.

    For a simple name-pattern lookup, laserfiche_entry_search_by_name
    is the cheaper option.

    Returns: {entries: [...], total_estimate: int | null, next_link:
    str | null}. total_estimate is the server's row-count hint when
    available; null when the server didn't return one. next_link is
    the opaque cursor to pass to a follow-up call's `cursor` param.

    On failure: ToolError with kind=invalid_input/subkind=
    bad_query_syntax if the query is malformed (try
    laserfiche_entry_search_natural to construct a valid one), or
    kind=upstream_unavailable/subkind=server_error if the
    SimpleSearches endpoint is unstable on this build.
    """
```

### Pattern C — Update with mode enum (`laserfiche_field_update`)

```python
class FieldUpdate(BaseModel):
    """One field-value update for laserfiche_field_update."""
    field_name: str = Field(
        description="The exact field name from laserfiche_field_definition_list.",
        examples=["Status", "Last Name", "Hire Date"],
    )
    values: list[str] = Field(
        description=(
            "Values for the field. Single-value fields take one "
            "element; multi-value fields take many. Empty list "
            "clears the field (mode=merge) or omits it (mode=replace)."
        ),
        examples=[["Approved"], ["Smith"], ["2024-01-15"]],
    )


@mcp.tool()
async def laserfiche_field_update(
    entry_id: Annotated[int, Field(
        description="Integer entry ID to update.",
        examples=[84493],
        gt=0,
    )],
    updates: Annotated[list[FieldUpdate], Field(
        description=(
            "List of field updates. Each entry pairs a field_name "
            "(must exist in the repository's field definitions) "
            "with values."
        ),
    )],
    mode: Annotated[Literal["merge", "replace"], Field(
        default="merge",
        description=(
            "merge (default, SAFE): GET-then-PUT helper. Reads "
            "the entry's current field values, layers updates on "
            "top, PUTs the union. Fields not in updates are "
            "preserved. "
            "replace (DESTRUCTIVE): OVERWRITE all fields. Fields "
            "on the entry that are NOT in updates are DELETED "
            "(independent fields) or reset to empty (templated "
            "fields). Use only when you want that explicit "
            "blow-away semantic."
        ),
    )] = "merge",
) -> dict[str, Any] | ToolError:
    """Set or update template fields on an entry.

    Default mode is "merge" (safe). Use "replace" only when you
    explicitly want to clear all other fields on the entry.

    Field names are validated against laserfiche_field_definition_list
    (cached); unknown field names return kind=invalid_input/subkind=
    invalid_field_name with the valid field names included.

    Field types are validated against the field definitions; passing
    a string for a ShortInteger field returns kind=invalid_input/
    subkind=invalid_field_value.

    Returns: {mode: "executed", entry_id, fields_updated: list[str],
    fields_preserved: list[str] (merge mode only), fields_cleared:
    list[str] (replace mode only), result: ...}.

    On failure: structured ToolError. See PLAN_ERRORS.md section 2b
    for the (kind, subkind) mapping.
    """
```

### Pattern D — Preview+execute pair (`laserfiche_entry_delete_*`)

```python
@mcp.tool()
async def laserfiche_entry_delete_preview(
    entry_id: Annotated[int, Field(
        description="Entry to be deleted (folder cascade-deletes its subtree).",
        examples=[84490],
        gt=0,
    )],
) -> dict[str, Any] | ToolError:
    """Preview a delete and return an HMAC-signed confirmation token.

    Always call this BEFORE laserfiche_entry_delete_execute. The
    preview shows the user what's about to be deleted and returns a
    token bound to (operation, entry_id, entry_name) with a 5-minute
    TTL.

    For folders, the preview includes immediate_child_count when
    that count is within LF_DELETE_FOLDER_MAX_DESCENDANTS (default 50);
    when over the cap, exceeds_batch_cap=true and the agent must
    pass force_large_delete=true to the execute call.

    For documents, the preview includes the entry's path and type.

    Do NOT call this with a confirmation_token — that's the execute
    tool's job. Calling preview alone is safe; no side effects on
    the repository.

    Returns: {mode: "preview", operation: "entry_delete", entry_id,
    entry_name, entry_type, full_path, immediate_child_count:
    int | null, exceeds_batch_cap: bool, batch_cap: int,
    audit_reason_required: bool, available_audit_reasons: list?,
    warning: str, confirmation_token: str, ttl_seconds: int,
    next_step: str}.

    On failure: ToolError with kind=not_found (entry doesn't exist),
    kind=permission_denied (subkind=path_not_allowed or
    path_traversal_blocked when the entry sits outside
    LF_WRITE_PATHS_ALLOW).
    """


@mcp.tool()
async def laserfiche_entry_delete_execute(
    entry_id: Annotated[int, Field(
        description="Must match the entry_id from the preview that issued the token.",
        examples=[84490],
        gt=0,
    )],
    confirmation_token: Annotated[str, Field(
        description=(
            "Token from laserfiche_entry_delete_preview. HMAC-signed "
            "and bound to (operation, entry_id, entry_name). Cannot "
            "be reused for a different entry. Expires after 5 minutes."
        ),
    )],
    audit_reason_id: Annotated[int | None, Field(
        default=None,
        description=(
            "Required when LF_REQUIRE_AUDIT_REASON=true or the "
            "preview's audit_reason_required field is true. Use IDs "
            "from laserfiche_audit_reason_list."
        ),
    )] = None,
    comment: Annotated[str | None, Field(
        default=None,
        description="Optional free-text comment recorded alongside the audit reason.",
    )] = None,
    force_large_delete: Annotated[bool, Field(
        default=False,
        description=(
            "Required (must be true) when the preview's "
            "exceeds_batch_cap field is true. Defense against "
            "accidentally deleting a folder with thousands of "
            "descendants."
        ),
    )] = False,
) -> dict[str, Any] | ToolError:
    """Execute a delete using the token from laserfiche_entry_delete_preview.

    The delete is async on the server side. The returned
    operation_token can be polled with laserfiche_task_wait.

    Folders cascade-delete their entire subtree. Irreversible without
    a Laserfiche backup or recycle-bin restore.

    Returns: {mode: "executed", operation: "entry_delete",
    entry_id, entry_name, operation_token: str, next_step: "Call
    laserfiche_task_wait(operation_token) to confirm completion."}.

    On failure: structured ToolError. Common subkinds:
    invalid_confirmation_token (token expired or for a different
    entry — re-run preview), exceeds_batch_cap (folder too large —
    pass force_large_delete=true), audit_reason_required (operator
    policy — call laserfiche_audit_reason_list first), path_not_allowed.
    """
```

### Pattern E — Multi-mode-split read (`laserfiche_document_get_*`)

The three replacements for `get_document_edoc(mode=...)`. Each is a
single-purpose tool with clear cost/return semantics.

- `laserfiche_document_get_info(entry_id)` — returns size, content_type,
  hint. ~200 bytes. Safe to call on any document as a first probe.
- `laserfiche_document_get_bytes(entry_id, max_bytes?)` — returns
  base64-encoded edoc + content_type + byte_size. Worst case ~33 MB
  (24 MB cap × 1.33 base64 expansion). The docstring explicitly
  warns: "**Context warning**: A 25 MB document yields ~33 MB of
  base64 text in the response. Prefer laserfiche_document_get_extracted_text
  for text-only workflows."
- `laserfiche_document_get_extracted_text(entry_id, max_chars=50_000)` —
  for PDFs: pypdf-extracted text. For text/*: direct UTF-8 decode.
  For other content types: returns a structured error with
  suggested_action pointing at `_get_bytes`. Returns:
  `{text, char_count, truncated, pages_total?, pages_extracted?}`.

### Remaining tools — pattern application

Each remaining tool follows one of the five patterns above:

- **Pattern A (single-resource get):** `laserfiche_entry_get_by_path`,
  `laserfiche_field_values_get`, `laserfiche_document_get_text`,
  `laserfiche_document_get_info`, `laserfiche_repository_list`,
  `laserfiche_audit_reason_list`.
- **Pattern B (paginated search/list):** `laserfiche_entry_search_by_name`,
  `laserfiche_entry_search_natural`, `laserfiche_folder_list`,
  `laserfiche_field_definition_list`, `laserfiche_tag_definition_list`,
  `laserfiche_template_definition_list`, `laserfiche_link_definition_list`,
  `laserfiche_template_field_list`.
- **Pattern C (update with mode/delta):** `laserfiche_field_update`,
  `laserfiche_tag_update`, `laserfiche_link_update`,
  `laserfiche_template_assign`.
- **Pattern D (preview+execute):** `laserfiche_entry_rename_preview`/`_execute`,
  `laserfiche_entry_move_preview`/`_execute`,
  `laserfiche_entry_delete_preview`/`_execute`,
  `laserfiche_document_edoc_delete_preview`/`_execute`,
  `laserfiche_document_pages_delete_preview`/`_execute`.
- **One-shot atomic writes (not preview/execute, but single-call):**
  `laserfiche_folder_create`, `laserfiche_entry_copy`,
  `laserfiche_document_import`.
- **Polling:** `laserfiche_task_wait`.

Full per-tool docstrings + Field specs land in `server.py` during
execution Step 5 (per `PLAN.md` 2c). Each follows the appropriate
pattern's template.

---

## 2c. List-to-search conversions

Per `AUDIT_DESIGN.md` 1b, all current list-shaped tools are correctly
bounded by construction. No list-to-search conversions needed in
v2.0. Specifically:

- **All four `list_*_definitions`** stay list-shaped — schema sets
  are bounded (50–500 fields, 0–50 tags, 5–50 templates, 5–20 links).
- **`laserfiche_repository_list`** stays list-shaped — typically 1–5
  repos per server.
- **`laserfiche_audit_reason_list`** stays list-shaped — typically
  fewer than 20 reasons.
- **`laserfiche_template_field_list`** (new) is list-shaped — fields
  per template are bounded (typically 5–30).

Search-shaped tools (`laserfiche_entry_search*`, `laserfiche_folder_list`)
already use pagination cursors via `next_link`. Stay as-is, gain
`response_format` and `total_estimate` per PLAN.md 2c step 2.

---

## 2d. Field-naming rules

Apply uniformly across every return shape:

1. **snake_case** field names in JSON output (no camelCase, no
   PascalCase). Even when the upstream OData uses camelCase,
   pydantic models normalize.
2. **ISO 8601 with `Z` suffix** for timestamps. UTC.
3. **`_id` suffix** for IDs that have a human-readable companion in
   the same response (`template_name` + `template_id`, `field_name` +
   `field_id`).
4. **`is_*` prefix** for booleans whose meaning isn't obvious from
   the resource name (`is_required`, `is_multi_value`,
   `is_electronic_document`, `is_record_folder`). NOT `*_flag`, NOT
   bare adjectives.
5. **`has_*` prefix** for booleans about existence (`has_children`,
   `has_more_values`).
6. **Enum-typed string fields** use the canonical enum value names
   (`entry_type: "Folder" | "Document" | "Shortcut"` — server's
   PascalCase preserved because that's how Laserfiche serializes;
   the model accepts these via `Literal` types).
7. **List fields** end in plural or the natural plural form
   (`entries`, `values`, `missing`).
8. **Cursor fields** use the OData convention: `next_link` (when the
   server returns one) or `cursor` (when we synthesize). Avoid
   `page_token` (could imply page-number semantics).
9. **Sizes** use byte counts in `byte_size`, character counts in
   `char_count`.
10. **Optional fields** are explicitly `T | None` in pydantic models;
    omitted from responses when None (pydantic `model_dump(exclude_none=True)`)
    to reduce token count.

---

## 2e. Migration order

Per `PLAN.md` section 2c. This file's contribution is Steps 5–6
(rename + design polish). Restated:

### Step 5 — Rename (mechanical, large diff)
1. Add the new tool name as a SECOND `@mcp.tool()` registration
   alongside the old one. Both names point to the same function
   body.
2. The old name emits a one-time deprecation warning to the structured
   log (NOT to the agent's response — that would pollute the success
   path).
3. Old names removed in v2.1.

### Step 6 — Design polish (per tool, in batches)
1. Move parameter descriptions from docstrings into `Field(...,
   description=...)`. FastMCP exposes the Field description in the
   JSON schema the LLM sees on tool selection — this is the real
   change in the LLM's decision surface.
2. Add `examples=[...]` on every parameter where the shape is
   non-obvious (queries, paths, field-update structures).
3. Replace bare `str` with `Literal[...]` where the value space is
   finite (`mode`, `response_format`).
4. Replace bare `dict[str, list[Any]]` (fields) with
   `list[FieldUpdate]` pydantic model.
5. Replace bare `list[dict[str, Any]]` (links) with `list[EntryLink]`
   pydantic model.
6. Pydantic-wrap the five raw-OData passthroughs
   (`*_definition_list` + `task_wait`).
7. Rewrite the bottom-3 docstrings (`list_repositories`,
   `get_audit_reasons`, `list_field_definitions`) to score 5/5 on the
   rubric.
8. Audit all docstrings against the rubric; fix gaps.

Each batch runs `ruff check` + `pytest -q` and waits for "continue."

---

## 2f. Backward compatibility

Renames are breaking. The user has signaled willingness to ship v2.0
as a breaking release with deprecation shims for one minor version.

### Deprecation shim policy

Every old name registers as a SECOND `@mcp.tool()` pointing at the
same implementation. On first call:
1. Log a `WARNING`-level structured event:
   `{event: "deprecation_warning", old_name, new_name, request_id}`.
2. Tool returns the same shape as the new name (no behavior change).

Old names removed in v2.1.

### What's preserved across the v1.5 → v2.0 break

- Wire-level success-response JSON shape for tools NOT in the split
  set. `laserfiche_entry_get`'s success payload is identical to
  v1.5's `get_entry`.
- The `mode: "error"` top-level key on failure responses. Agents
  branching on `result.get("mode") == "error"` continue to work.
- Confirmation-token contract for destructive ops. Tokens remain
  HMAC-signed and bound to (operation, entry_id, entry_name).

### What breaks

- Error response shape: `error` (slug field, e.g. `"not_found"`)
  becomes `kind` (`"not_found"`) + `subkind` (`"not_found"` for
  HTTP 404 case). Agents branching on `result["error"]` need to read
  `result["subkind"]` instead. Deprecation shim CANNOT preserve
  this; the v1.5 error shape is replaced.
- Tool names: every name changes. The shim makes this soft.
- `set_fields` / `set_tags` removed in favor of
  `field_update(mode="replace")` / `tag_update(...)`. Behavior
  preserved via mode.
- `get_document_edoc(mode=...)` removed in favor of three split
  tools. The shim CAN preserve this by dispatching to the new
  split based on the `mode` argument.
- Single-function preview/execute tools (`delete_entry`, etc.)
  removed. Shim preserves by dispatching to `_preview` or `_execute`
  based on `confirmation_token` presence.

### Documented breaking changes in CHANGELOG.md

Will be enumerated explicitly in the v2.0 CHANGELOG entry per the
"Notes for upgraders" section style we used in v1.4.

---

## 2g. Risks and open questions

### Risk 1 — FastMCP and `Annotated[..., Field(...)]`

The plan relies on `Annotated[T, Field(description=..., examples=...)]`
to surface parameter descriptions in the JSON schema FastMCP exposes
to MCP clients. FastMCP/MCP-SDK's behavior with this pattern needs
verification.

**Resolution:** in Step 6, register one tool with `Annotated[Field]`
parameters, inspect the MCP schema (`mcp.list_tools()` then check
`tool.inputSchema`), confirm the descriptions and examples appear.
If they don't, fall back to keeping descriptions in docstrings
(which FastMCP does propagate) but add `examples` via pydantic
JSON schema customization.

### Risk 2 — Cached schema lookup invalidation

Field/tag/template definitions are cached for 5 minutes (per `PLAN.md`
2d Risk 4). If an operator adds a new template, the validation will
falsely reject `assign_template` against the new template name for up
to 5 minutes.

**Resolution:** `LF_SCHEMA_CACHE_TTL_SECONDS` env var (default 300);
operators on actively-changing schemas can set lower. Documented in
README.

### Risk 3 — `response_format="concise"` losing data the LLM needs

If "concise" omits `template_name` and the LLM needs to know whether
an entry has a template to decide whether to call
`laserfiche_field_values_get`, the LLM will call `_get` again with
`response_format="detailed"`. Two round trips instead of one.

**Resolution:** the concise field set is the result of weighing
per-tool common workflows. For `laserfiche_entry_get`, concise
includes `template_name` precisely because of that workflow.
Concise definitions per tool are documented in the docstring's "what
each format returns" subsection.

### Risk 4 — Pydantic-wrapping raw OData breaks adopters parsing old shape

The five `list_*_definitions` tools currently return raw OData
(`{value: [{fieldType, isRequired, ...}]}`). v2.0 pydantic-wraps
them and snake_cases the fields. Adopters parsing the old shape
break.

**Resolution:** deprecation shim CAN preserve old shape if needed,
but the cost is shipping two shapes for one tool. Cleaner: document
the v2.0 shape change explicitly in CHANGELOG; the deprecation shim
of the OLD tool name returns the v1.5 raw OData shape (matching the
old name's contract); the NEW tool name returns the pydantic-normalized
shape.

### Open question 1 — `response_format="detailed"` token cost

The audit estimates concise vs detailed ratios per tool. These are
estimates — actual ratios depend on the repo's data. Should we
expose per-call token-cost estimates in the response?

**Resolution:** NO. Adds noise. Operators concerned about token
budget can measure via the structured logs (which already capture
response size proxies).

### Open question 2 — Plural vs singular resource segments

The plan uses singular resource segments per principle (`entry`,
not `entries`). But the verbs naturally suggest plural for list
operations: `laserfiche_entries_list` reads more naturally than
`laserfiche_entry_list`.

**Resolution:** decided per principle — stay singular. The
consistency is more valuable than the local readability win. Tools
that return lists make that explicit via the `_list` verb suffix
anyway.

---

## 2h. Test strategy

### Description-quality smoke test (NEW)

Per Pass 3's principle 3 ("Iterative refinement of these strings is
how server quality improves"), add a smoke test that gives a separate
LLM agent a stated task and the v2 tool descriptions, and asks it
to pick the right tool. If the agent picks wrong, the description
needs more disambiguation.

`tests/test_tool_picking.py` (new):

```python
@pytest.mark.parametrize("task, expected_tool", [
    ("Summarize this PDF document", "laserfiche_document_get_extracted_text"),
    ("What's the status on entry 42?", "laserfiche_field_values_get"),
    ("Find all docs named Onboarding*", "laserfiche_entry_search_by_name"),
    ("I have an entry ID, give me its name", "laserfiche_entry_get"),
    ("List subfolders under \\Sandbox", "laserfiche_folder_list"),
    ("Delete folder 42", "laserfiche_entry_delete_preview"),
    ("Apply that token to actually delete", "laserfiche_entry_delete_execute"),
])
async def test_description_disambiguates_tool_choice(task, expected_tool):
    """Smoke test: the tool description for `expected_tool` should
    make it the obvious choice for `task`. Failing this means the
    description needs more 'when to use' / 'when NOT to use' detail."""
    ...
```

Implemented via a small offline LLM call (e.g., haiku-4.5) given just
the tool list + descriptions. Run manually before each release; not
in CI (cost).

### Per-tool unit tests

Every tool gets:
- Happy path with the default `response_format`.
- Each `mode` / `response_format` value tested.
- A malformed-input test that exercises the schema validation
  (asserts `kind=invalid_input`).
- A limit-ceiling test (ask for 1000, get 200).

### Pagination tests

For paginated tools:
- First page returns `next_link`.
- Following the `next_link` cursor returns the next page.
- Last page has `next_link=None`.
- `total_estimate` matches actual total when server provides one.

### Deprecation-shim tests

For every shim:
- Call old name; assert success.
- Assert one deprecation log event was emitted with the new name.
- Assert wire-shape matches v1.5 (where preservable) or matches the
  documented v2 shape (where not).

### Integration tests against the test v1 repository

Per `PLAN.md` 2e:
- Full surface coverage (one call per tool).
- Workflow F E2E (`template_field_list` against Personnel Document).
- Pre-flight validation E2E (unknown field, unknown template,
  unknown tag, wrong entry_type).
- Path-traversal block E2E.
- Preview/execute split E2E for all 5 destructive pairs.

### Naming smoke test

`tests/test_naming.py` (new):

```python
def test_every_tool_follows_naming_convention():
    """Every registered tool name starts with 'laserfiche_' and
    follows the {resource}_{verb} pattern."""
    tools = mcp.list_tools()  # or whatever the FastMCP API exposes
    for tool in tools:
        if tool.name in DEPRECATED_OLD_NAMES:
            continue  # shims are exempt
        assert tool.name.startswith("laserfiche_"), (
            f"Tool {tool.name} missing laserfiche_ prefix."
        )
        # Resource segment validation
        parts = tool.name.split("_")[1:]
        assert parts[0] in KNOWN_RESOURCES, (
            f"Tool {tool.name} has unknown resource segment {parts[0]}"
        )
```

`KNOWN_RESOURCES` set: `entry, folder, document, field, field_values,
field_definition, tag, tag_definition, template, template_field,
template_definition, link, link_definition, repository, audit_reason,
task, document_edoc, document_pages`.

---

## End of PLAN_DESIGN.md

Companions:
- `PLAN.md` — surface composition, collapses/splits, migration order.
- `PLAN_ERRORS.md` — error model, per-tool error mapping, logging,
  redaction.

When all three pass executions complete, this file's per-tool
patterns are the source of truth for the actual docstrings shipped
in `server.py`.
