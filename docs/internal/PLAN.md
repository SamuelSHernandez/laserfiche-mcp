# PLAN.md — laserfiche-mcp Pass 1: Workflow & Surface

Source: `AUDIT.md`. Companions: `PLAN_DESIGN.md` (per-tool schemas
and naming) and `PLAN_ERRORS.md` (error model and observability).

Target release: **v2.0.0**. Breaking change with deprecation shims at
the old tool names for one minor version (removed in v2.1).

---

## 2a. Target tool set

**36 tools.** Full per-tool input/output schemas with Pydantic
`Field(description=..., examples=[...])` are specified in
`PLAN_DESIGN.md` section 2b. This file holds the surface composition,
the rationale for each tool's presence, and the migration order.

### Reads (19 tools)

| Tool | Replaces / origin | Purpose (one sentence) |
|---|---|---|
| `laserfiche_entry_search` | `search_entries` | Run a raw Laserfiche query and return paginated matches. |
| `laserfiche_entry_search_by_name` | `search_by_name` | Wildcard name search with optional folder scope; thin wrapper for ergonomics. |
| `laserfiche_entry_search_natural` | `search_natural` | Two-mode LLM-guided search: ask for grammar+templates (Mode A), then run with auto-repair (Mode B). |
| `laserfiche_folder_list` | `list_folder` | Page through a folder's immediate children by ID. |
| `laserfiche_entry_get` | `get_entry` | Fetch metadata for one entry by ID. |
| `laserfiche_entry_get_by_path` | `get_entry_by_path` | Resolve a backslash-delimited path to an entry. |
| `laserfiche_field_values_get` | `get_field_values` | Read template field values currently on an entry. |
| `laserfiche_document_get_text` | `get_document_text` | v2-only server-side extracted text. |
| `laserfiche_document_get_info` | `get_document_edoc(mode="info")` split | Lightweight edoc metadata (size, content-type). |
| `laserfiche_document_get_bytes` | `get_document_edoc(mode="bytes")` split | Base64-encoded edoc payload (capped). |
| `laserfiche_document_get_extracted_text` | `get_document_edoc(mode="text")` split | Client-extracted text via pypdf for PDFs or direct decode for text/*. |
| `laserfiche_repository_list` | `list_repositories` | List repos this account can reach (with single-item fallback on disabled endpoint). |
| `laserfiche_field_definition_list` | `list_field_definitions` | Every field definition in the repository. |
| `laserfiche_template_field_list` | **new** | Field definitions scoped to one template (closes workflow F gap). |
| `laserfiche_tag_definition_list` | `list_tag_definitions` | Every tag definition. |
| `laserfiche_template_definition_list` | `list_template_definitions` | Every template definition. |
| `laserfiche_link_definition_list` | `list_link_definitions` | Every entry-link type definition. |
| `laserfiche_audit_reason_list` | `get_audit_reasons` | Audit-reason codes (renamed from `get_*` since the response is a listing). |
| `laserfiche_task_wait` | `wait_for_task` + `get_task_status` | Poll an async operation; `timeout_seconds=0` returns immediately (collapses status-check). |

### Writes (17 tools)

| Tool | Replaces / origin | Purpose |
|---|---|---|
| `laserfiche_field_update` | `set_fields` + `merge_fields` | Update template fields. `mode: Literal["merge","replace"]` encodes the destructive choice explicitly. |
| `laserfiche_tag_update` | `set_tags` + `merge_tags` | Add/remove tags on an entry. |
| `laserfiche_link_update` | `set_links` | Update entry-link list. `mode: Literal["merge","replace"]`. |
| `laserfiche_template_assign` | `assign_template` + `remove_template` | Assign a template (or clear with `template_name=None`). |
| `laserfiche_folder_create` | `create_folder` | Create a child folder. |
| `laserfiche_entry_copy` | `copy_entry` | Async copy via the CopyAsync endpoint. |
| `laserfiche_document_import` | `import_document` | Upload a local file as a new document. |
| `laserfiche_entry_rename_preview` | `rename_entry` (preview branch) | Return the would-be path and an HMAC-signed confirmation token. |
| `laserfiche_entry_rename_execute` | `rename_entry` (execute branch) | Apply the rename using the token from the preview. |
| `laserfiche_entry_move_preview` | `move_entry` (preview branch) | Return source/destination paths and a token. |
| `laserfiche_entry_move_execute` | `move_entry` (execute branch) | Apply the move using the token. |
| `laserfiche_entry_delete_preview` | `delete_entry` (preview branch) | Return child-count, batch-cap status, audit-reason requirement, and a token. |
| `laserfiche_entry_delete_execute` | `delete_entry` (execute branch) | Queue the async delete using the token. |
| `laserfiche_document_edoc_delete_preview` | `delete_edoc` (preview branch) | Return entry info and a token. |
| `laserfiche_document_edoc_delete_execute` | `delete_edoc` (execute branch) | Wipe the document binary using the token. |
| `laserfiche_document_pages_delete_preview` | `delete_pages` (preview branch) | Return entry info, validated page range, and a token. |
| `laserfiche_document_pages_delete_execute` | `delete_pages` (execute branch) | Delete the specified pages using the token. |

### Surface delta

- 32 current tools → 36 v2 tools.
- **Collapses (-4):** set/merge pairs (-2), `remove_template` into
  `template_assign` (-1), `get_task_status` into `task_wait` (-1).
- **Splits (+5):** preview/execute pairs add 5 new entry points
  (rename, move, delete_entry, delete_edoc, delete_pages each go from
  1 mode-multiplexed function to 2 single-purpose tools).
- **Splits (+3):** `get_document_edoc` mode-multiplexer becomes three
  tools (`document_get_info`, `document_get_bytes`,
  `document_get_extracted_text`).
- **New tool (+1):** `laserfiche_template_field_list`.
- **Removed mode-multiplexed shell (-1):** `get_document_edoc`
  removed (its three modes become three tools).

Net: 32 − 4 + 5 + 3 + 1 − 1 = **36**.

---

## 2b. Deletions and collapses

### Collapses (functionality preserved via parameter encoding)

| Removed | Kept | Encoding |
|---|---|---|
| `set_fields` + `merge_fields` | `laserfiche_field_update` | `mode: Literal["merge","replace"]="merge"` |
| `set_tags` + `merge_tags` | `laserfiche_tag_update` | `add: list[str]=[]`, `remove: list[str]=[]` (additive delta; `replace` semantics dropped — agents who want overwrite call with `add=<new full list>, remove=<diff>` after a `tag_definition_list` lookup) |
| `assign_template` + `remove_template` | `laserfiche_template_assign` | `template_name: str | None` — `None` clears |
| `get_task_status` + `wait_for_task` | `laserfiche_task_wait` | `timeout_seconds: int = 60`; `0` returns immediately with current status |

### Splits (composability over multiplexing)

| Removed | Replaced by |
|---|---|
| `rename_entry` (single function, preview/execute discriminated by `confirmation_token`) | `laserfiche_entry_rename_preview` + `laserfiche_entry_rename_execute` |
| `move_entry` | `laserfiche_entry_move_preview` + `laserfiche_entry_move_execute` |
| `delete_entry` | `laserfiche_entry_delete_preview` + `laserfiche_entry_delete_execute` |
| `delete_edoc` | `laserfiche_document_edoc_delete_preview` + `laserfiche_document_edoc_delete_execute` |
| `delete_pages` | `laserfiche_document_pages_delete_preview` + `laserfiche_document_pages_delete_execute` |
| `get_document_edoc` (mode-multiplexed) | `laserfiche_document_get_info` + `laserfiche_document_get_bytes` + `laserfiche_document_get_extracted_text` |

### New tools

| New | Why |
|---|---|
| `laserfiche_template_field_list` | Workflow F gap (`AUDIT.md` 1b). Closes a 3-call chain into a 1-call lookup for "what fields does this template need." |

### Tools removed outright

None. API coverage is preserved.

---

## 2c. Migration order

The order interleaves all three passes per the user-approved
cross-pass dependency order (Pass 1 surface → Pass 3 design → Pass 2
errors). Within each step, work runs smallest-safe-diff-first.

### Step 1 — Pass 1 security: close defense-in-depth gaps
**Files:** `permissions.py`, `client.py`, new `validation.py` helper.
- Reject `..` path-traversal segments in `permissions.py:path_allowed`.
- Add `name_allowed(name: str) -> tuple[bool, reason]` for entry-name
  validation (no `\`, no `/`, no NULL bytes, length 1-128).
- Add `validate_page_range(range_str: str) -> tuple[bool, reason]`
  regex check.
- Add cached lookup helpers on `LaserficheClient`:
  - `cached_field_definitions(ttl=300)` returns a dict keyed by name.
  - `cached_template_definitions(ttl=300)`.
  - `cached_tag_definitions(ttl=300)`.
  - `cached_link_definitions(ttl=300)`.
- Wire these helpers into the new validation pre-flights at every
  write tool, returning structured errors with the appropriate
  `subkind` (PLAN_ERRORS section 2b lists each).
**Compatibility:** purely additive. Existing tools continue to work.
Tests must pass.

### Step 2 — Pass 1 surface: new atomic tools + projection
**Files:** `server.py`, `client.py`, `config.py`, `models.py`.
- Add `laserfiche_template_field_list(template_name)` (the new
  atomic tool).
- Add `include_fields: list[str] | None = None` to
  `laserfiche_folder_list`, `laserfiche_entry_get`,
  `laserfiche_entry_get_by_path`, `laserfiche_field_values_get`, and
  all three `laserfiche_entry_search*` variants. Default `None`
  preserves existing payload shape.
- Add `summary_only: bool = False` to
  `laserfiche_field_definition_list`,
  `laserfiche_tag_definition_list`,
  `laserfiche_template_definition_list`,
  `laserfiche_link_definition_list`. Default `False` preserves
  payload.
- Add `response_format: Literal["concise","detailed"]="concise"` to
  the heavy reads. "Concise" definition per tool documented in
  `PLAN_DESIGN.md` section 2b.
- Add `total_estimate: int | None` companion to every `next_link`
  response.
**Compatibility:** all additions default to existing behavior.

### Step 3 — Pass 1 collapses: write-tool consolidation
**Files:** `server.py`.
- Implement `laserfiche_field_update`, `laserfiche_tag_update`,
  `laserfiche_link_update`, `laserfiche_template_assign`,
  `laserfiche_task_wait` ALONGSIDE the old `set_fields`/etc. tools.
  The old tools become thin shims calling the new implementations
  (so collapses don't ship dead code).
- Old tools log a one-time deprecation warning on first call.

### Step 4 — Pass 1 preview/execute splits
**Files:** `server.py`.
- Implement the 10 split tools (`*_preview` + `*_execute` pairs for
  rename, move, delete_entry, delete_edoc, delete_pages) ALONGSIDE
  the old single-function tools.
- Old single-function tools become thin shims that dispatch by
  `confirmation_token` presence.

### Step 5 — Pass 3 rename (Phase 3 of Pass 3)
**Files:** `server.py`, every tool registration.
- Rename every tool to `laserfiche_{resource}_{verb}`. Add the old
  name as a deprecation alias.
- Per Pass 3 PLAN_DESIGN.md section 2f, deprecation shims:
  - Emit a `DeprecationWarning` to logs (not to agent response).
  - Forward call to the new tool name.
  - Removed in v2.1.

### Step 6 — Pass 3 design: descriptions, schemas, return shapes
**Files:** `server.py`, `models.py`.
- Rewrite the bottom-3 descriptions to score 5/5 on the rubric.
- Move parameter descriptions from docstrings into `Field(...,
  description=...)`.
- Add `examples=[...]` on non-obvious parameters.
- Replace `dict[str, list[Any]]` (fields) and `list[dict[str, Any]]`
  (links) with pydantic models (`FieldUpdate`, `EntryLink`).
- Pydantic-wrap the five raw-OData passthroughs.

### Step 7 — Pass 2: error model + observability
**Files:** new `errors.py`, new `observability.py`, every tool.
- Implement `ToolError` and `ToolErrorKind` per `PLAN_ERRORS.md`
  section 2a.
- Implement structured-logging decorator per `PLAN_ERRORS.md` 2c.
- Implement redaction helper per `PLAN_ERRORS.md` 2d.
- Extract Laserfiche `traceId` and surface as `upstream_trace_id` on
  every error response.
- Wire `request_id` generation per `PLAN_ERRORS.md` 2e.

### Step 8 — Final: tests, docs, version bump, ship
- Full unit + mock-integration test pass.
- Integration test pass against GC IPRS sandbox (with
  `LF_INTEGRATION_TEST=1`).
- Update `README.md`, `CHANGELOG.md`, `docs/error-contract.md`,
  `docs/getting-started.md`, `TODO.md`.
- Bump `pyproject.toml` and `__init__.py` to `2.0.0`.
- Commit, tag `v2.0.0`, push to GitHub.
- Build wheel + sdist with `uv build`.
- Publish to PyPI.
- Create GitHub Release with notes from CHANGELOG.

After each step: `uv run ruff check`, `uv run pytest -q`, summarize
diff, wait for "continue."

---

## 2d. Risks and open questions

### Risk 1 — Deprecation shim impact on FastMCP wire format

Old tool names registered as shims still appear in FastMCP's tool
list. Wire-level names become `mcp__laserfiche__search_entries` (old)
AND `mcp__laserfiche__laserfiche_entry_search` (new). The model sees
both in tool selection. To prevent double-counting toward context
budget, the shim should be hidden from the schema where possible —
or marked deprecated in the tool description so the LLM prefers the
new name.

**Resolution to verify in execution:** check FastMCP's API for
hiding/deprecating individual tools without unregistering them. If
not available, document the trade-off and rely on the deprecated
docstring to steer the LLM.

### Risk 2 — `tag_update`'s replace-mode semantics

The collapse drops the "OVERWRITE all tags" affordance from
`set_tags`. Agents wanting that semantic must compose
`tag_definition_list` → diff → `tag_update(add=<diff>, remove=<rest>)`.
This is a real ergonomic loss for the "I want exactly these tags"
use case.

**Resolution:** if the audit's integration testing finds the diff
chain unwieldy, add `mode: Literal["merge","replace"]` to
`tag_update` consistent with `field_update` and `link_update`.

### Risk 3 — Preview-execute split makes confirmation-token replay
risk more visible

Splitting the destructive tools into preview + execute makes the
HMAC token contract explicit: an agent could call `entry_delete_preview`
then NEVER call `entry_delete_execute`, leaving an open token until
TTL expiry. The current single-tool design has the same property,
but the split makes it surface-visible. Operationally identical.

**Resolution:** documented in PLAN_DESIGN's per-tool descriptions —
"tokens expire in 5 minutes; calling preview alone is safe and has
no side effects."

### Risk 4 — Cached schema lookup TTL trade-off

Field / template / tag definitions rarely change but they CAN change
(e.g., an admin adds a new template). A 5-minute cache TTL means
agents could see stale validation errors for 5 minutes after a
schema change.

**Resolution:** 5-minute TTL by default; expose
`LF_SCHEMA_CACHE_TTL_SECONDS` env var; document the trade-off.

### Open question 1 — Should `laserfiche_template_field_list`
absorb `laserfiche_field_definition_list`?

`field_definition_list` returns ALL fields in the repo; the new
`template_field_list` returns fields scoped to one template. Both
have legitimate use cases:
- `field_definition_list` for "what fields exist anywhere"
- `template_field_list` for workflow F (what does this template need)

Keep both. The new tool doesn't replace the old; it provides a more
focused entry point.

### Open question 2 — Should there be a
`laserfiche_template_required_field_list` for the "what required
fields does this template have" sub-case?

Workflow F (per AUDIT 1b) really wants "required fields for this
template". Two options:
1. `template_field_list(template_name)` returns ALL fields with
   `is_required` flag; agent filters.
2. `template_field_list(template_name, required_only: bool = False)`
   parameter.

**Resolution:** ship option 2. Single tool with optional filter;
LLM doesn't have to compose a filter.

---

## 2e. Test strategy

Every new and changed tool gets coverage across three test surfaces.

### Unit tests (mocks via `pytest-httpx`)

- Each collapsed tool (`field_update`, `tag_update`, `link_update`,
  `template_assign`, `task_wait`) gets happy-path tests for each
  `mode` value plus the collapsed paths the original tools had.
- Each preview/execute pair gets independent tests for preview output
  shape, execute token validation, and the
  `invalid_confirmation_token` slug.
- Each three-way document-get split gets per-mode happy-path tests.
- `laserfiche_template_field_list` gets:
  - Empty template (no fields)
  - Template with all required fields
  - Template with mixed required/optional
  - Unknown template name → `invalid_template_name` slug
- Cached lookup helpers: TTL expiry, cache hit, cache miss, refresh.
- Pre-flight validations: tests for every new slug
  (`invalid_field_name`, `invalid_template_name`, `invalid_tag_name`,
  `expected_folder_got_document`, `invalid_field_value`,
  `invalid_name`, `invalid_page_range`).
- Path-fence `..` rejection: tests for normalized and un-normalized
  `..` segments, mixed slash variants.

### Integration tests (`LF_INTEGRATION_TEST=1` against GC IPRS sandbox)

Extend `tests/test_integration.py`:

- **Surface coverage:** call every renamed tool at least once to
  verify the rename map.
- **Deprecation shims:** call an old name (e.g., `search_entries`),
  assert it returns the same structured result and logs a
  deprecation warning.
- **Workflow F:** call `template_field_list` against a real
  Missionary Document template; verify required fields are flagged.
- **Pre-flight validations against real schema:** unknown template
  name returns `invalid_template_name` with the valid template list;
  unknown field name returns `invalid_field_name`.
- **Path-fence `..`:** attempt to write to a `..`-containing path;
  verify `path_not_allowed` fires before the API call.
- **Preview/execute split E2E:** preview → use token → execute, for
  rename, move, delete_entry, delete_edoc.
- **Adversarial inputs:** zero-length names, very long names,
  control-char names, paths with NUL bytes.

### CLI tests (`--diagnose`)

- After Step 8, `uv run laserfiche-mcp --diagnose` against the GC
  IPRS sandbox reports:
  - All 36 v2 tools registered (or 32 + 5 split + 1 new − 4 collapse
    + 0 deletion = 36, less 11 old-name shims if hidden).
  - Auth succeeds.
  - All read endpoints probe-pass.
  - Write-mode safety config reflects PLAN_ERRORS' new env vars
    (e.g., `LF_VALIDATE_FIELD_TYPES`, `LF_VALIDATE_NAMES`).

### Live Claude Code smoke

End-to-end against the GC IPRS sandbox with the new MCP version:

1. "Find a recent Missionary Document and tell me who it's about."
2. "Set the Status field on entry X to 'Approved'." (Verifies
   `field_update` with mode default = "merge".)
3. "Delete the test folder Y from yesterday." (Verifies the
   preview/execute split end-to-end.)
4. "What templates are available?" → "What fields does the
   Missionary Document template need?" (Verifies the new
   `template_field_list` tool.)

---

## End of PLAN.md

Companions:
- `PLAN_ERRORS.md` — error model, per-tool error mapping, logging
  schema, redaction plan.
- `PLAN_DESIGN.md` — per-tool full specs (descriptions, input
  schemas, output schemas, response_format policies, deprecation
  shim policy).
