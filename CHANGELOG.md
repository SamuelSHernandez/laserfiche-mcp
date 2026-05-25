# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [2.1.0] - 2026-05-24

Public-release readiness pass. Closes the deferrals from PLAN.md /
PLAN_ERRORS.md / PLAN_DESIGN.md that v2.0.0 punted on, plus a
sanitization sweep that removes accidental customer-specific
identifiers from source, tests, and internal docs.

No breaking changes — every new tool registers as a v2.x peer
alongside the existing multiplex / set+merge / assign+remove pairs.
v3.0 (eventual) is when the original verb-first names go away;
v2.x is additive only.

### Added — observability (PLAN_ERRORS.md step 7)
- **`observability.py` module** with three primitives:
  - **`redact(obj, *, host=None, repo_id=None)`** — single redaction
    helper. Replaces values of credential-like keys (`password`,
    `token`, `api_key`, `confirmation_token`, etc.) with
    `"<redacted>"`, recursing into dicts/lists/tuples. Strings have
    the configured host and repo-id substrings rewritten to
    `"<repo_host>"` / `"<repo_id>"` (whole-segment match for repo-id).
  - **`request_id` ContextVar** + `get_request_id()` /
    `get_request_id_or_new()`. The decorator below sets the var on
    entry; `errors.classify_lf_error` reads it so the per-tool-call
    log line and the agent-visible error response carry the same ID.
  - **`tool_logger` decorator** — emits exactly one structured event
    per tool call: `{tool, request_id, outcome, duration_ms, args
    (redacted), error_kind?, error_subkind?, upstream_trace_id?}`.
    Idempotent (double-wrapping is a no-op), so it's safe to apply
    once at registration and reuse the wrapped function under both
    the legacy and v2 names.
  - **`JsonLogFormatter` + `configure_logging(level, format_)`** —
    one JSON object per log line when `LF_LOG_FORMAT=json`. Plain
    text format remains the default. Per-tool-call events hoist to
    a top-level `event` key for easy `jq` filtering.
- **`LF_LOG_FORMAT` env var** (`text` | `json`, default `text`).
  Surfaced in `--diagnose` output alongside the existing log-level
  and write-mode reports.
- Retry warnings in `client/_core.py` now log the **full URL** with
  host + repo-id substituted — strictly more diagnostic value than
  the prior path-only redaction, with the same safety guarantee.

### Added — preview/execute splits (PLAN.md step 4)
Ten new tools, two per destructive operation, registered alongside
the existing multiplex tools as v2.x peers (no breaking change):
- `rename_entry_preview` / `rename_entry_execute`
- `move_entry_preview` / `move_entry_execute`
- `delete_entry_preview` / `delete_entry_execute`
- `delete_edoc_preview` / `delete_edoc_execute`
- `delete_pages_preview` / `delete_pages_execute`

Each split refuses the wrong call shape with a structured error:
`..._preview` returns `preview_does_not_accept_token` if the agent
passes a token; `..._execute` returns `execute_requires_token` if
the agent omits it (or passes the empty string). v2 names follow
`laserfiche_{resource}_{verb}_{preview|execute}` per the existing
naming map.

### Added — rich parameter descriptions in JSON schema (PLAN_DESIGN.md step 6)
Every tool parameter with a non-obvious shape now carries
`Annotated[type, Field(description=..., examples=[...])]` annotations.
The MCP catalog the LLM sees gains:

- Rich descriptions on params like `query` (Laserfiche search syntax),
  `full_path` (backslash format), `name_pattern` (wildcard rules),
  `page_range` (refused-when-empty rationale), `fields` / `updates`
  (multi-value list shape with empty-list-to-clear semantics),
  `links` (target/linkType dict shape), `file_path` (MCP-server
  filesystem caveat), `confirmation_token` (TTL + binding).
- Concrete `examples` arrays so the model can copy proven shapes
  instead of guessing — Laserfiche query clauses, sample paths,
  field-update dicts, page ranges.
- Min/max constraints (`ge`, `le`, `min_length`, `max_length`) on
  numeric and string params where they exist server-side or
  client-side.

Tools touched: every read tool (search/list/get), every write tool
(set/merge/create/copy/import/rename/move/delete/template), the 5
v2.x collapses (field_update etc.), and the 10 preview/execute
splits. No runtime behavior change — purely schema enrichment.

### Added — collapsed write tools (PLAN.md step 3)
Five new tools that subsume the existing `set_*` / `merge_*` /
`assign_*` / `remove_*` pairs so the LLM only has to learn one
name + a `mode` argument:
- **`field_update(entry_id, updates, mode="merge")`** — wraps
  `merge_fields` (default, safer) and `set_fields` (`mode="replace"`).
- **`tag_update(entry_id, add=None, remove=None, replace=None)`** —
  wraps `merge_tags` (when `replace` is omitted) and `set_tags`
  (when `replace=...` is supplied, even `replace=[]` to clear).
  Refuses `conflicting_modes` if both `replace` and `add`/`remove`
  are passed.
- **`link_update(entry_id, links, mode="replace")`** — wraps
  `set_links`. `mode="merge"` reads current links, unions, and PUTs
  (the server has no delta endpoint; this is GET-then-PUT).
- **`template_assign_or_remove(entry_id, template_name=None, fields=None)`**
  — wraps `assign_template` (when `template_name` is set) and
  `remove_template` (when `None`). Refuses `fields=` on the remove
  path with `fields_ignored_on_remove`.
- **`task_wait_or_poll(operation_token, timeout_seconds=60)`** —
  wraps `wait_for_task` (default) and `get_task_status`
  (`timeout_seconds=0`).

Original tool names remain registered as peers.

### Changed — request_id is now per-call, not per-error
`errors.classify_lf_error` previously generated a fresh UUID4 on
every invocation, so two errors in the same tool call would get
different `request_id`s. The classifier now reads from the
ContextVar set by `tool_logger`, falling back to a fresh UUID only
when called outside any tool-logger context. Net effect: a tool call
that emits one log line and surfaces one error response shows the
same `request_id` on both — an operator can pivot from log to error
or vice versa with a single grep.

### Added — contributor experience
- **`.gitattributes`** enforcing LF line endings on every text file.
  Stops Windows clones from failing `ruff format --check` locally
  with 58 unrelated "would reformat" entries — the most common
  blocker for first-time contributors on Windows.
- **`smithery.yaml`** now exposes every runtime env var the server
  understands: `LF_API_VERSION`, `LF_VALIDATE_NAMES`,
  `LF_SCHEMA_CACHE_TTL_SECONDS`, `LF_MAX_PAGE_SIZE`,
  `LF_EDOC_MAX_BYTES`, plus the full write-mode safety block
  (`LF_WRITE_PATHS_ALLOW/DENY`, `LF_WRITE_TOOLS_ALLOWED`,
  `LF_DELETE_FOLDER_MAX_DESCENDANTS`, `LF_REQUIRE_AUDIT_REASON`,
  `LF_VALIDATE_REQUIRED_FIELDS`, `LF_IMPORT_MAX_BYTES`). One-click
  Smithery installs can now configure write mode end-to-end.

### Changed — public-release sanitization
- **Log redaction in auth and retry paths.** `auth.py` `_refresh()` log
  lines no longer include the token URL (operator already configured
  the host; DEBUG-level URL emission added needless deployment-context
  to log aggregators). Client retry warnings in `client/_core.py` now
  log only `request.url.path` instead of the full URL — the host (and
  any embedded credentials in unusual configs) no longer reach WARNING
  output. Path retains repo-id + endpoint so retries are still
  diagnosable.
- **Example template names** in docstrings, search grammar, tutorial,
  and tests changed from domain-specific (`Missionary Application`,
  `Missionary Document`) to neutral (`Loan Application`,
  `Personnel Document`). No behavior change; LLM-facing tool docs
  and `getting-started.md` show generic examples.
- **Test fixture repo ID** in `tests/client/test_definitions.py`
  replaced (`IPRS` → `MAIN`); related comment generalized.
- **`docs/internal/` worked notes** sanitized: example hostnames
  (`gc-its-dm-repo`) → `lf.example.com`, deployment references
  (`GC IPRS sandbox`, `IPRS server`) → generic (`v1 test repository`,
  `test v1 server`). Documents remain accurate; the customer-specific
  identifiers are removed.
- **`docs/internal/AUDIT_ERRORS.md`** logging-leak findings annotated
  as remediated with pointers to the new locations.
- **`CONTRIBUTING.md`** adds a Windows line-ending note and lists
  `ruff format --check` alongside the other CI commands.

## [2.0.0] - 2026-05-14

This is a major release that reshapes the public surface based on a
three-pass architectural audit (`AUDIT.md`, `AUDIT_ERRORS.md`,
`AUDIT_DESIGN.md`) and the corresponding plans (`PLAN.md`,
`PLAN_ERRORS.md`, `PLAN_DESIGN.md`) — all checked in for reference.
The audits applied four design principles: don't port the API,
security lives in the host, design for finite context, design for
hallucinated inputs.

### Added — Pass 1 security defense-in-depth
Closes gaps identified in `AUDIT.md` section 1c (Principle 2):

- **Path traversal rejected.** `permissions.path_allowed` now rejects
  any path containing a `..` segment regardless of allow/deny config.
- **Entry-name validation.** New `permissions.name_allowed()` rejects
  names with `\`, `/`, NULL bytes, control characters, or length
  outside 1-128. Wired into `create_folder`, `import_document`,
  `copy_entry`, `rename_entry`, `move_entry` with structured
  `invalid_name` errors.
- **Page-range syntax validation.** New `permissions.validate_page_range()`
  catches malformed-but-non-empty `page_range` inputs before the API
  call (`invalid_page_range`).
- **Cached schema-definition lookups** on `LaserficheClient`:
  `cached_field_definitions`, `cached_tag_definitions`,
  `cached_template_definitions`, `cached_link_definitions`. TTL via
  new `LF_SCHEMA_CACHE_TTL_SECONDS` env var (default 300s).
- **Client-side pre-flight** for unknown field / tag / template /
  link-type names on every write that takes them. Returns structured
  `invalid_field_name`, `invalid_tag_name`, `invalid_template_name`,
  `invalid_link_type` errors listing valid values, instead of the
  server's opaque 400. Gated on `LF_VALIDATE_NAMES` (default true).

### Added — Pass 1 atomic tool
- **`get_template_fields(template_name, required_only=False)`** —
  the missing "what fields does this template need" lookup that
  used to require chaining three calls (`list_template_definitions`
  → `list_field_definitions` → manual filter). Returns the template's
  field list with each field's type, constraints, and required flag
  inlined. `required_only=True` filters to mandatory fields.
  Surfaces `invalid_template_name` errors with the valid names list.

### Added — Pass 1 token-budget affordances
- **`summary_only: bool = False`** on `list_field_definitions`,
  `list_tag_definitions`, `list_template_definitions`,
  `list_link_definitions`. When true, the tool returns
  `{count, names: list[str]}` instead of the full definition payload.
  Cuts a 30-50 KB listing down to a tiny shape for "what's
  available?" workflows. Backwards-compatible default preserves the
  existing payload.

### Changed — Pass 3 tool naming
Per `PLAN_DESIGN.md` section 2a: every tool is now registered under
its v2 name in `laserfiche_{resource}_{verb}` form (e.g.,
`laserfiche_entry_get`, `laserfiche_field_set`, `laserfiche_folder_create`).
The old verb-first names (`get_entry`, `set_fields`, `create_folder`,
etc.) remain registered as deprecation aliases for v2.x; they will be
removed in v3.0.

The `_V2_RENAME_MAP` constant in `src/laserfiche_mcp/server.py` is
the authoritative mapping. Both names point at the same function
implementation; behavior is identical.

### Changed — Pass 2 structured error contract
Every tool's `mode: "error"` response now carries three new
top-level fields:

- **`kind`**: one of 5 canonical `ToolErrorKind` values
  (`not_found`, `permission_denied`, `rate_limited`, `invalid_input`,
  `upstream_unavailable`). LLMs branch on this for category-level
  decisions (retry vs ask user vs abort).
- **`request_id`**: a UUID4 unique to this tool invocation. Operators
  pivot from this into MCP-side logs.
- **`upstream_trace_id`**: the Laserfiche server's W3C trace ID from
  the ProblemDetails response when present. Pivots into the LF
  server's own logs for cross-system correlation.

The existing `error` field is preserved and now reads as the
"subkind" — a more specific slug than `kind`. The full subkind → kind
mapping is in `_SUBKIND_TO_KIND` and exposed via the public helper
`kind_for_subkind(subkind)`. v1.5 callers that branched on `error`
continue to work; new code can branch on `kind` for category-level
handling.

### Deferred to v2.x follow-ups
Recorded in `TODO.md` for incremental ship after v2.0:

- **Write-tool collapses** (set/merge field/tag pairs into
  `update_fields(mode)`, etc.) — current tools still work; the
  collapses were prepared in `PLAN.md` but the existing surface
  remains coherent.
- **Preview/execute splits** of the 5 destructive tools into 10
  single-purpose tools — the current single-tool design with
  confirmation-token discrimination still works.
- **Description polish** — per `PLAN_DESIGN.md` section 2b, move
  parameter descriptions into `Field(description=...)`, add
  `examples=[...]`, replace `dict[str, list[Any]]` field-update
  shapes with pydantic models. Current docstrings remain detailed
  but don't yet flow into the MCP JSON schema's parameter docs.
- **Structured JSON logging** with `LF_LOG_FORMAT=json` and a
  per-tool-call decorator emitting `{ts, tool, args_redacted,
  duration_ms, outcome, request_id, upstream_trace_id, ...}`.
- **Single `redact()` helper** wired into the two retry-warning log
  lines and the new logging decorator.

### Notes for upgraders
- **JSON wire shape**: success-path responses are unchanged for every
  tool. Error responses gain `kind`, `request_id`, `upstream_trace_id`
  alongside the existing fields. Callers parsing the existing fields
  continue to work; the new fields are optional context.
- **Tool names**: v2.0 keeps both old and new names registered. Update
  to the `laserfiche_*` names at your convenience; deprecation
  warnings are not emitted yet but old names will be removed in v3.0.
- **Test count**: 310 unit tests, coverage 85.43%.

## [1.5.0] - 2026-05-13

### Fixed
- **Bug 7 — `list_repositories` response normalization.** Some LF builds
  return a bare JSON array (`[{repoId: "ASTR", ...}, ...]`) for the
  `/Repositories` endpoint instead of an OData envelope. The client now
  normalizes to `{"value": [...]}` on both shapes so callers always
  receive the same key. Test coverage in `test_client.py` exercises both
  response variants plus the empty-body case.
- **Bug 8 — Typed-return tools broke the structured error contract at
  runtime.** Eight tools (`get_entry`, `get_entry_by_path`,
  `get_field_values`, `list_folder`, `search_entries`, `search_by_name`,
  `search_natural`, `get_document_text`) were annotated with pydantic
  model returns; FastMCP's runtime validation rejected the `mode: "error"`
  dict on the failure path and surfaced a pydantic ValidationError to
  the client instead of the structured slug. Tool wrappers now return
  `dict[str, Any]` uniformly — `.model_dump()` on success, structured
  error dict on failure. Pydantic models stay for internal parsing and
  validation. The success-path JSON shape is unchanged. The unit tests
  passed in v1.4.x because they called functions directly, bypassing the
  FastMCP layer; v1.5 also adds real-server integration coverage that
  goes through the runtime path.

### Changed
- `get_document_text` returns `{"entry_id", "text", "char_count",
  "truncated"}` on success instead of a bare string. Easier for callers
  to detect truncation programmatically. The previous "[truncated, N
  chars omitted]" inline marker is gone; check `result["truncated"]`.
- `get_field_values` returns `{"entry_id", "values": [...]}` on success
  instead of a bare list. Matches the envelope shape used by other
  read tools.

### Added — CLI options
- `--diagnose` — probes the configured server and prints a
  deployment-fitness report (authentication, every read endpoint,
  SimpleSearches behavior, write-mode safety guards). Useful for new
  adopters figuring out what their LF build actually supports without
  having to start the MCP. Exits 0 on auth success, 1 on auth failure.
- `--verbose` / `-v` and `--quiet` / `-q` — override `LF_LOG_LEVEL` to
  DEBUG or WARNING for ad-hoc debugging.
- `--config PATH` — load environment from a specific `.env` file
  instead of the default `$CWD/.env` discovery.
- CLI arg parsing migrated to `argparse` (was hand-rolled). `--help` and
  `--version` behavior unchanged. New CLI parser tests cover defaults,
  mutually exclusive verbose/quiet, and log-level resolution.

### Added — Integration tests
- `tests/test_integration.py` expanded with real-server smoke tests
  (still gated behind `LF_INTEGRATION_TEST=1`):
  - Structured-error contract through FastMCP for typed-return tools
    (closes the test gap that hid Bug 8): `get_entry`,
    `get_entry_by_path`, `list_folder`, `get_field_values` on a
    nonexistent ID return a clean `{mode: "error", error: <slug>}` dict.
  - `list_repositories` envelope-shape verification (closes Bug 7).
  - Path-fence enforcement: `move_entry` to a denied destination returns
    `path_not_allowed` before the API call.
  - `assign_template` required-field validator: opt-in via
    `LF_INTEGRATION_SANDBOX_PARENT_ID` + optional
    `LF_INTEGRATION_TEMPLATE_NAME`.
  - `delete_entry` preview's child-count accuracy regression
    (page-bound `$count` worked around in v1.4 — verifies the cap+1
    probe matches `list_folder`'s actual entry count).

### Notes for upgraders
- The success-path **JSON shape** is identical for every tool except
  `get_document_text` and `get_field_values` (see Changed above).
- Python callers importing tool functions directly will see
  `dict[str, Any]` instead of pydantic models. Pydantic models
  (`EntryDetail`, `FieldValue`, `SearchResults`, `SearchNaturalResponse`)
  remain in `laserfiche_mcp.models` for direct use.

## [1.4.2] - 2026-05-13

### Fixed
- Lint cleanups CI surfaced after the v1.4.1 commit (six findings: nested
  `if` collapse, unused intermediate variable, `==` chain → `in (...)`,
  over-long docstring line, unused test import, import sort order). No
  functional change. v1.4.1 was not published to PyPI; v1.4.2 ships in
  its place.

## [1.4.1] - 2026-05-13

### Fixed
- `--version` flag and the `laserfiche_mcp.__version__` constant were
  out of sync with the package metadata in v1.4.0 — they reported
  ``1.3.0`` while ``pyproject.toml`` (and the PyPI release) correctly
  declared ``1.4.0``. The package functionality was unaffected; only
  the version-reporting paths lied. Both now report ``1.4.1``.

## [1.4.0] - 2026-05-13

This release is the first to validate the write surface against a real
self-hosted LFRepositoryAPI v1 server. The v1.2 and v1.3 wire formats were
inferred from the OpenAPI spec and Java reference client; some did not
survive contact with a live build. v1.4 fixes the broken endpoints, and
makes errors machine-readable end-to-end so the next live-server test
catches issues without an LLM stuck on `Error executing tool ...`.

### Fixed — v1 wire format
Any deployment running against a v1 Repository API Server was previously
broken on the affected tools. v1.2 unit tests passed only because the
httpx mocks didn't validate request shape; v1.4 tests do.

- **`create_folder`** — v1 route is `POST /Entries/{p}/Laserfiche.Repository.Folder/children`, not the bare `/Folder` path. The latter is interpreted as an entry-name suffix and dispatched to the document-import handler, which then 400s on the missing multipart `file` part.
- **`set_fields` / `merge_fields`** — v1 PUT body is a flat `{FieldName: FieldToUpdate}` dict, not wrapped in a `"fields"` key. Wrapping it produced a server-side `NullReferenceException`.
- **`delete_entry`** — DELETE requests now always serialize an empty JSON body (`{}`) when there's no audit info, so httpx attaches `Content-Type: application/json`. v1 servers return HTTP 415 when the header is missing.
- **`copy_entry`** — Routed via the new `copy_entry_async` client method to `POST /Entries/{p}/Laserfiche.Repository.Folder/CopyAsync` with `{name, sourceId, volumeName?}`. The previous synchronous create-children route rejects `entryType: "Document"` (its enum is `Folder | Shortcut`). Copy is async — poll the returned `operationToken` via `get_task_status` / `wait_for_task`.
- **`delete_edoc`** — v1 route is `DELETE /Entries/{id}/Laserfiche.Repository.Document/edoc` (OData type segment); plus the same `Content-Type` fix as `delete_entry`.
- **`delete_pages`** — v1 route is `DELETE /Entries/{id}/Laserfiche.Repository.Document/pages?pageRange=...`; same `Content-Type` fix.

### Fixed — folder-delete preview child count
The delete-folder preview's `immediate_child_count` is now accurate. On
this v1 server the OData `$count` query parameter is page-bound (returns
the page size, not the total, when combined with `$top`). The probe now
fetches `cap + 1` children and uses `len(items)`: an exact count when the
folder is at or under the cap, a definitive "exceeds" signal at `cap + 1`.

### Fixed — `_user_fields_to_values` position indexing
Multi-value field updates now emit 1-indexed `position` values per the
swagger spec (`ValueToUpdate.position: 1-indexed for multi value field`).
Single-value fields ignore the value either way, so this only affects
multi-value writes.

### Added — structured error contract
Every tool now returns a stable error shape on failure instead of raising
`RuntimeError` (which surfaced as an opaque `"Error executing tool ..."`
string in MCP clients).

```json
{
  "mode": "error",
  "operation": "delete_entry",
  "error": "not_found",
  "status_code": 404,
  "server_error_code": null,
  "server_message": null,
  "reason": "Server returned 404 — the entry, path, or endpoint does not exist.",
  "entry_id": 999
}
```

The `error` field is a short machine-readable slug LLM callers can branch
on. Mapping is centralized in `_classify_lf_error()`:

| Slug                      | Triggers                                           |
| ------------------------- | -------------------------------------------------- |
| `auth_failed`             | HTTP 401/403, LF errorCode 9010, or LF 9528 ('LFDS unreachable', misleadingly worded — usually also bad creds) |
| `required_field_missing`  | LF errorCode 9039/9066 |
| `not_found`               | HTTP 404 |
| `method_not_allowed`      | HTTP 405 (usually an MCP routing bug) |
| `unsupported_media_type`  | HTTP 415 (usually a wire-format bug — missing Content-Type) |
| `rate_limited`            | HTTP 429 |
| `server_error`            | HTTP 5xx or unrecognized failures |

`LaserficheError` now carries a `detail` attribute (parsed response body
if JSON, raw text if plaintext), so callers no longer scrape the message
string. The `_lf_error_detail` helper merges the nested `{error: {...}}`
shape some routes use (notably the Edoc DELETEs) into a flat
ProblemDetails view.

### Added — required-field validation
`LF_VALIDATE_REQUIRED_FIELDS` (default `true`) makes `assign_template`
preflight repository-wide required-field constraints client-side. Before
the PUT, it lists `FieldDefinitions`, checks each `isRequired: true`
field against what's on the entry and what's in the caller's `fields=`,
and returns a `missing_required_fields` error listing the names, list
values, and defaults — instead of letting the server reject with the
opaque `Multistatus response. [9039]` message. Set to `false` on builds
with no required fields to save the extra reads.

### Added — `list_repositories` fallback
Some self-hosted builds disable the `/Repositories` endpoint. When that
happens, `list_repositories` now returns `mode: "fallback"` with the
configured `LF_REPOSITORY_ID` as a single-item list and the underlying
server error attached — so LLM callers get usable data and know it's
partial.

### Changed
- `_fetch_entry_or_raise` → `_fetch_entry_for_op` returning `(entry, error)`. Write tools propagate the structured error instead of raising.
- `LaserficheClient.delete_entry` and `delete_edoc` / `delete_pages` always send a JSON body so the `Content-Type` header is attached (see Fixed above).
- `LaserficheClient.list_folder` gained `include_count: bool = False` for callers who genuinely want the server's `@odata.count` — the delete-preview probe no longer uses it (see Fixed above).

### Tests
- 242 unit tests, coverage 88.65% (above the 85% floor).
- New direct unit tests for `_classify_lf_error` (one per slug), `_lf_error_detail`, validator edge cases (all-set / partial-caller-fields / read-failure fall-through), and write-tool fetch-failure surfacing structured errors.
- Existing `pytest.raises(RuntimeError)` error-path tests updated to assert the structured `mode: error` response.

### Not in v1.4 (still deferred)
- Server-side audit logging (sidecar file + rotation). Tracked for v1.5.
- Full async `/Searches` flow, ContextHits, Attributes.

## [1.3.0] - 2026-05-13

### Added — configuration-driven write guards
Builds on v1.2 by adding operator-configurable safety layers. All default to "off" — existing deployments upgrade with zero behavior change.

- **Path scope fences** — `LF_WRITE_PATHS_ALLOW` and `LF_WRITE_PATHS_DENY` (comma-separated path prefixes). When set, every write tool checks the target entry's `fullPath` (or, for creates, the parent's path) against the lists. Case-insensitive; backslashes and forward slashes both accepted; deny wins over allow. `move_entry` fences on BOTH source and destination paths so a token from an allowed source can't be replayed to land in a denied folder. Implemented in the new `permissions` module.
- **Folder-delete batch cap** — `LF_DELETE_FOLDER_MAX_DESCENDANTS` (default 50). When `delete_entry` targets a folder with more immediate children than the cap, the execute call refuses unless `force_large_delete=true` is also passed. The preview shows `exceeds_batch_cap: true` and includes the cap in `next_step` so the LLM can surface the size to the user before re-calling.
- **Tool-level allowlist** — `LF_WRITE_TOOLS_ALLOWED` (comma-separated tool names). When set, only listed write tools register at startup. Defense-in-depth: write tools also runtime-check their own name against the allowlist, so direct invocation (e.g., from tests) is also gated. Lets operators ship metadata-only (`merge_fields,merge_tags,assign_template`) or create-only (`create_folder,import_document,copy_entry`) deployments.
- **Audit-reason requirement** — `LF_REQUIRE_AUDIT_REASON` (bool, default false). When true, `delete_entry` refuses to execute without an `audit_reason_id`. Use `get_audit_reasons` to enumerate valid IDs for the authenticated user.

### Changed
- `delete_entry` gains a `force_large_delete: bool = False` parameter. Required only when the targeted folder exceeds `LF_DELETE_FOLDER_MAX_DESCENDANTS`. The confirmation token from preview is NOT bound to this flag — the cap check is a separate, independent gate.
- `delete_entry` preview response gains `exceeds_batch_cap`, `batch_cap`, and `audit_reason_required` fields so the LLM can tell the user up front what extra arguments execute will need.
- `move_entry` always fetches the destination parent (used to be preview-only) to enforce the destination-path fence on both preview and execute paths. Eliminates a token-replay vector where the LLM could swap `new_parent_id` between calls.

### Added — supporting infrastructure
- `permissions` module — pure-function path matching (case-insensitive prefix with boundary check) and tool-allowlist parsing. Importable independent of FastMCP, so it can be unit-tested standalone.
- `_check_write_permission` / `_check_write_for_entry` / `_check_write_for_parent` helpers in `server.py` centralize the guard plumbing so every write tool runs the same checks.

### Tests
- New `tests/test_permissions.py` covers the path-match boundary rules, case insensitivity, slash normalization, allow/deny precedence, and tool allowlist parsing (16 tests).
- `tests/test_server.py` extended with 10 guard tests: path deny/allow refusal, parent-path check for creates, move-entry destination fence, batch cap refusal + force override, audit-reason requirement, runtime tool allowlist, registration-time tool allowlist. 216 tests total; coverage 89% (above the 85% floor).

### Not in v1.3 (deferred)
- Server-side audit logging (sidecar file, rotation). Scope-creep risk; deserves its own release. Tracked for v1.4.
- The async `/Searches` flow, ContextHits, Attributes — still deferred since SimpleSearches covers current needs.

## [1.2.0] - 2026-05-13

### Added — write tools
Writes are off by default. Set `LF_READ_ONLY=false` to register them; otherwise the tool catalog the LLM sees is unchanged from v1.1.

- **Metadata writes**: `set_fields`, `merge_fields`, `set_tags`, `merge_tags`, `set_links`, `assign_template`, `remove_template`. `merge_fields` and `merge_tags` are GET-then-PUT helpers that preserve values not mentioned in the call — the raw `set_*` tools follow the API's overwrite semantics and delete anything not in the payload, which is a footgun for LLM-driven workflows.
- **Entry creation / copy / import**: `create_folder`, `copy_entry`, `import_document`. `import_document` reads bytes from a local file path, guesses content-type from the name, and enforces a client-side cap (`LF_IMPORT_MAX_BYTES`, default 25 MB; API caps at 100 MB).
- **Destructive ops with two-step confirmation**: `rename_entry`, `move_entry`, `delete_entry`, `delete_edoc`, `delete_pages`. First call returns a structured preview + a short-lived HMAC-signed `confirmation_token`; second call passes the token back to execute. Tokens are bound to `(operation, entry_id, entry_name)` and expire after 5 minutes. Server restarts invalidate all pending tokens (per-process secret). `delete_entry` previews include the immediate child count for folders; `delete_pages` refuses an empty `page_range` (API treats empty as "delete all").

### Added — supporting reads
- `list_repositories`, `list_field_definitions`, `list_tag_definitions`, `list_template_definitions`, `list_link_definitions`, `get_audit_reasons` (needed before supplying `audit_reason_id` to `delete_entry`), `get_task_status`, `wait_for_task` (polling wrapper for the async deletes/copies). These register unconditionally.

### Added — supporting infrastructure
- `confirmation` module — HMAC-signed token issuance and verification, scoped to a per-process secret.
- `LaserficheClient` write methods: `patch_entry`, `delete_entry`, `create_child_entry`, `import_document` (multipart), `put_fields`, `put_tags`, `put_links`, `assign_template`, `remove_template`, `delete_edoc`, `delete_pages`, plus reads `get_tags`, `get_links`, `list_repositories`, `list_field_definitions`, `list_tag_definitions`, `list_template_definitions`, `list_link_definitions`, `get_audit_reasons`, `get_task_status`. `delete_pages` refuses empty `page_range` at the client layer too.
- `LF_IMPORT_MAX_BYTES` env var (default 25 MB) — client-side cap for `import_document`.

### Changed
- `LF_READ_ONLY` now has behavior. `true` (still the default) registers only read tools, exactly matching the v1.1 catalog. `false` additionally registers the 15 write tools. Existing deployments upgrade with zero change unless they explicitly opt in.
- Startup log line reports the number of write tools registered.

### Tests
- New `tests/test_confirmation.py` covers token roundtrip, tamper detection, expiry, and operation/entry/name binding.
- `tests/test_client.py` and `tests/test_server.py` extended with happy-path + error-path coverage for every new endpoint and tool. 188 unit tests; coverage 89% (above the 85% floor).

### Not in v1.2 (deferred)
- Full async `/Searches` flow (SimpleSearches covers current needs).
- ContextHits, Attributes, deprecated ServerSession routes.
- Richer guardrails — path scope fences, batch caps, tool-level allowlists, audit-reason enforcement, per-tool dry-run defaults beyond destructive ops. Phase 2 work.

## [1.1.0] - 2026-05-13

### Added
- LFRepositoryAPI v1 support alongside v2 (`LF_API_VERSION`, default `v1`). Most current on-prem installations expose the v1 routing surface (`/v1/` paths, lowercase `fields`/`children`, OData entity-type segments, no unified `/Export`); v2-only routing was rejecting every call. Auth's password-grant token endpoint, `build_repo_path`, `list_folder`, `get_field_values`, and `export_entry` now all route on the configured version. *(Shipped as the prior commit on this branch.)*
- `search_natural` tool — two-mode guided search. Mode A (no `lf_query`) samples up to ten entries from `folder_path`, returns the templates and field names actually present, the Laserfiche search-syntax grammar reference, and 2–3 candidate query strings for the host LLM to pick from or refine. Mode B (`lf_query` set) executes the query and, on HTTP 400, performs up to two automatic repairs (escape unescaped `"` inside `="..."` values, then wildcard-wrap bare `Name=` values when `fuzzy=True`) before returning a structured error with every attempt visible. Pagination surfaces `pagination_unknown=true` when the result count hits the cap but the server returned no continuation link.
- `get_document_edoc(mode=...)` — adds `bytes` (base64 + content-type, capped by `LF_EDOC_MAX_BYTES`, default 25 MB) and `text` (server-side extraction; PDFs via `pypdf`, `text/*` decoded directly, anything else returns a structured "use mode=bytes" error) alongside the original `info` mode. Gives v1 servers a path to document text now that `get_document_text` has no endpoint to call.
- `LF_MAX_PAGE_SIZE` env var (default 100) — dedicated `search_natural` page-size cap, separate from `LF_MAX_RESULTS_CEILING`. Some self-hosted SimpleSearches implementations reject `$top` above an internal limit, so this defaults lower.
- `LF_EDOC_MAX_BYTES` env var (default 25 MB) — caps edoc downloads when `mode` is `bytes` or `text`. Override per-call via the `max_bytes` argument.
- `LaserficheClient.export_entry_with_meta()` — returns `(bytes, content_type)` so callers can branch on document type instead of trusting the entry's extension.
- `pypdf` runtime dependency for server-side PDF text extraction.

### Changed
- `get_document_text` docstring now points v1 users to `get_document_edoc(mode="text")`.
- Startup log message no longer warns that `LF_READ_ONLY` "has no effect yet" — that warning was misleading. The flag remains reserved for future write tools; the startup line just records the configured value.

### Tests
- New PDF fixtures committed at `tests/fixtures/sample_text.pdf` and `tests/fixtures/sample_encrypted.pdf` (with a regeneration script at `tests/fixtures/_generate.py`). Text-extraction tests now assert specific extracted content rather than just response shape — earlier blank-page PDFs would have let a pypdf regression ship silently. New tests cover the encrypted-PDF, malformed-PDF, mixed-case content-type, and char-limit truncation paths.
- New opt-in integration target at `tests/test_integration.py`, marked with `pytest.mark.integration` and gated behind `LF_INTEGRATION_TEST=1`. Reads the same `LF_*` config the server uses at runtime; covers `search_natural` Mode A guidance, Mode B structured-outcome contract, and `get_document_edoc(mode="info"|"text")` against a real PDF entry (configurable via `LF_INTEGRATION_FOLDER_PATH` and `LF_INTEGRATION_PDF_ENTRY_ID`).
- `pytest-cov` added as a dev dep with a baseline of 80% (current measured 85% branch coverage; threshold leaves a small regression buffer).

### Fixed
- N/A (v1.1.0 is purely additive; existing tools keep their contracts.)

## [0.2.1] - 2026-05-10

### Changed
- `Development Status` classifier bumped from Alpha to Beta to reflect the verified-against-reference state of the package.
- `pyproject.toml`: added `Typing :: Typed`, `Topic :: Communications`, and `Topic :: Software Development :: Libraries :: Python Modules` classifiers; declared Python 3.13 support (CI tests it); expanded `[project.urls]` to include Repository, Documentation, and Changelog.
- `main()` now prints a clean, actionable error and exits with code 2 when LF_* env vars are missing — no Python traceback. Ctrl-C exits cleanly without a traceback as well.

### Added
- `--help` and `--version` CLI flags so first-time users running `uvx laserfiche-mcp` directly can discover the package without needing env config.
- `py.typed` marker file (PEP 561) so downstream type checkers recognize the package as typed.
- `CHANGELOG.md`, `SECURITY.md`, `CONTRIBUTING.md`.
- Status badges in README (PyPI version, Python versions, CI, license, MCP).
- `.github/dependabot.yml` for weekly grouped dependency updates.
- `.github/workflows/release.yml` — auto-publish to PyPI on `v*` tag via OIDC trusted publishing.
- `smithery.yaml` — manifest for one-click install on Smithery.

## [0.2.0] - 2026-05-08

### Changed (breaking)
- **Auth flow**: HTTP Basic auth replaced with the password-grant token exchange Laserfiche actually requires. The server now POSTs username/password to `/v2/{repository_id}/Token` and uses the returned bearer for subsequent calls. `LF_AUTH_MODE=basic` renamed to `LF_AUTH_MODE=password`.
- **Endpoint paths** corrected against the official `Laserfiche/lf-repository-api-client-java` reference:
  - `list_folder`: `/Entries/{id}/Children` → `/Entries/{id}/Folder/Children`.
  - `search_entries`: `GET /Entries/SearchEntries?...` → `POST /SimpleSearches` with JSON body `{"searchCommand": "..."}`.
  - Document download: `GET /Entries/{id}/Edoc` → `POST /Entries/{id}/Export` with body `{"part": "Edoc" | "Text" | "Image"}`.
- `get_document_text` now requests Laserfiche-extracted text rather than raw bytes.

### Added
- `get_document_edoc` tool returning metadata only (size + hint), so the model never gets raw binary in its context window.
- `search_by_name` convenience tool that constructs `{LF:Name=...}` queries safely.
- `OAuthClientCredentialsStrategy` for LFDS-compatible OAuth providers.
- `LF_VERIFY_SSL` knob with a runtime warning when disabled.
- Retry on transient 5xx/429/connection errors with exponential backoff (`LF_RETRY_ATTEMPTS`, default 3).
- `LF_LOG_LEVEL` env var.
- `SecretStr` for `password`, `client_secret`, and `api_key` so credentials don't leak via `repr` or tracebacks.
- `_pick(raw, *keys)` helper that distinguishes missing keys from explicitly false/zero/empty values; regression-tested.
- 43 tests across config, models, auth, client (incl. retry/4xx-no-retry), and server helpers.

### Yanked
- v0.1.0 was yanked from PyPI on 2026-05-09 because the auth flow and several endpoint paths were incorrect against any real Laserfiche server.

## [0.1.0] - 2026-05-08

Initial public release. **Yanked** — see v0.2.0 for the correct auth flow and endpoint paths.

[Unreleased]: https://github.com/SamuelSHernandez/laserfiche-mcp/compare/v2.1.0...HEAD
[2.1.0]: https://github.com/SamuelSHernandez/laserfiche-mcp/compare/v2.0.0...v2.1.0
[2.0.0]: https://github.com/SamuelSHernandez/laserfiche-mcp/compare/v1.5.0...v2.0.0
[1.5.0]: https://github.com/SamuelSHernandez/laserfiche-mcp/compare/v1.4.2...v1.5.0
[1.4.2]: https://github.com/SamuelSHernandez/laserfiche-mcp/compare/v1.4.1...v1.4.2
[1.4.1]: https://github.com/SamuelSHernandez/laserfiche-mcp/compare/v1.4.0...v1.4.1
[1.4.0]: https://github.com/SamuelSHernandez/laserfiche-mcp/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/SamuelSHernandez/laserfiche-mcp/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/SamuelSHernandez/laserfiche-mcp/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/SamuelSHernandez/laserfiche-mcp/compare/v0.2.1...v1.1.0
[0.2.1]: https://github.com/SamuelSHernandez/laserfiche-mcp/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/SamuelSHernandez/laserfiche-mcp/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/SamuelSHernandez/laserfiche-mcp/releases/tag/v0.1.0
