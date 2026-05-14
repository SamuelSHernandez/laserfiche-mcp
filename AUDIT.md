# AUDIT.md — laserfiche-mcp Pass 1: Workflow & Surface

Read-only audit of the v1.5 codebase against the four design principles:
(1) don't port the API, (2) security lives in the host, (3) design for
finite context, (4) design for hallucinated inputs.

Source of truth for the corresponding `PLAN.md`. Every claim cites
`file:line`.

---

## 1a. Tool inventory

32 tools total — 17 reads (always registered) and 15 writes (registered
only when `LF_READ_ONLY=false`).

| # | Name | Purpose | Params | Return | Client call(s) | LOC | file:line |
|---|------|---------|--------|--------|-----------------|-----|-----------|
| 1 | `search_entries` | Run a raw Laserfiche query | `query`, `max_results?` | `SearchResults` dict | `client.search_entries` | 9 | `server.py:119` |
| 2 | `search_by_name` | Wildcard name search, optional folder scope | `name_pattern`, `in_folder_path?`, `max_results?` | `SearchResults` dict | `client.search_entries` | 14 | `server.py:166` |
| 3 | `search_natural` | Two-mode LLM-guided search with auto-repair | `question`, `lf_query?`, `folder_path?`, `max_results=50`, `fuzzy=True` | `SearchNaturalResponse` dict | `client.search_entries` + sampler | 186 | `server.py:318` |
| 4 | `list_folder` | Page through folder children by ID | `folder_id`, `max_results?`, `skip=0` | `SearchResults` dict | `client.list_folder` | 10 | `server.py:501` |
| 5 | `get_entry` | Fetch metadata for one entry | `entry_id` | `EntryDetail` dict | `client.get_entry` | 5 | `server.py:544` |
| 6 | `get_entry_by_path` | Resolve a path to an entry | `full_path` | `EntryDetail` dict | `client.get_entry_by_path` | 5 | `server.py:575` |
| 7 | `get_field_values` | Read template fields on an entry | `entry_id` | `{entry_id, values:[...]}` | `client.get_field_values` | 9 | `server.py:608` |
| 8 | `get_document_text` | Server-extracted text (v2-only) | `entry_id`, `max_chars=50_000` | `{entry_id, text, char_count, truncated}` | `client.export_entry` | 14 | `server.py:641` |
| 9 | `get_document_edoc` | Inspect / download / extract a document | `entry_id`, `mode∈{info,bytes,text}`, `max_bytes?`, `text_char_limit=50_000` | mode-dependent | `client.export_entry_with_meta` + pypdf | 161 | `server.py:749` |
| 10 | `list_repositories` | Enumerate accessible repos | (none) | OData dict OR `mode:fallback` | `client.list_repositories` | 24 | `server.py:910` |
| 11 | `list_field_definitions` | Every field in the repo | `max_results?`, `skip=0` | OData dict | `client.list_field_definitions` | 9 | `server.py:964` |
| 12 | `list_tag_definitions` | Every tag definition | `max_results?`, `skip=0` | OData dict | `client.list_tag_definitions` | 9 | `server.py:1002` |
| 13 | `list_template_definitions` | Every template | `template_name?`, `max_results?`, `skip=0` | OData dict | `client.list_template_definitions` | 10 | `server.py:1034` |
| 14 | `list_link_definitions` | Every entry-link type | `max_results?`, `skip=0` | OData dict | `client.list_link_definitions` | 8 | `server.py:1071` |
| 15 | `get_audit_reasons` | Audit-reason codes for the account | (none) | dict grouped by op | `client.get_audit_reasons` | 5 | `server.py:1104` |
| 16 | `get_task_status` | Synchronous status check | `operation_token` | task payload dict | `client.get_task_status` | 8 | `server.py:1128` |
| 17 | `wait_for_task` | Poll until terminal status | `operation_token`, `timeout_seconds=60`, `poll_interval_seconds=1.0` | task payload + `timed_out` | `client.get_task_status` (loop) | 48 | `server.py:1165` |
| 18 | `set_fields` | OVERWRITE all field values | `entry_id`, `fields` | server field listing | `client.put_fields` | 9 | `server.py:1503` |
| 19 | `merge_fields` | Update specific fields, preserve rest | `entry_id`, `updates` | `{mode:executed, fields_updated, fields_preserved, result}` | `client.get_field_values` + `client.put_fields` | 63 | `server.py:1546` |
| 20 | `set_tags` | OVERWRITE tags | `entry_id`, `tags` | server tag listing | `client.put_tags` | 9 | `server.py:1610` |
| 21 | `merge_tags` | Add/remove specific tags | `entry_id`, `add?`, `remove?` | `{mode:executed, added, removed, final_tags, result}` | `client.get_tags` + `client.put_tags` | 61 | `server.py:1649` |
| 22 | `set_links` | OVERWRITE entry links | `entry_id`, `links` | server link listing | `client.put_links` | 9 | `server.py:1713` |
| 23 | `assign_template` | Assign template (with validator pre-flight) | `entry_id`, `template_name`, `fields?` | server entry dict | `_validate_required_fields` + `client.assign_template` | 63 | `server.py:1822` |
| 24 | `remove_template` | Clear template assignment | `entry_id` | server entry dict | `client.remove_template` | 9 | `server.py:1887` |
| 25 | `create_folder` | Create child folder | `parent_id`, `name`, `template_name?`, `fields?`, `auto_rename=False` | server entry dict | `client.create_child_entry` | 20 | `server.py:1920` |
| 26 | `copy_entry` | Async copy via CopyAsync | `source_id`, `parent_id`, `name`, `auto_rename=False` | `{token}` | `client.copy_entry_async` | 16 | `server.py:1981` |
| 27 | `import_document` | Multipart upload of a local file | `parent_id`, `name`, `file_path`, `template_name?`, `fields?`, `tags?`, `content_type?`, `auto_rename=False` | server import payload | local I/O + `client.import_document` | 118 | `server.py:2038` |
| 28 | `rename_entry` | Two-step preview→token→execute rename | `entry_id`, `new_name`, `confirmation_token?` | preview OR executed dict | `client.get_entry` + `confirmation.create_token` + `client.patch_entry` | 122 | `server.py:2161` |
| 29 | `move_entry` | Two-step move (with optional rename) | `entry_id`, `new_parent_id`, `confirmation_token?`, `new_name?` | preview OR executed dict | `client.get_entry` ×2 + token + `client.patch_entry` | 124 | `server.py:2284` |
| 30 | `delete_entry` | Two-step delete with cap+audit gates | `entry_id`, `confirmation_token?`, `audit_reason_id?`, `comment?`, `force_large_delete=False` | preview OR executed dict | `client.get_entry` + `list_folder` probe + token + `client.delete_entry` | 218 | `server.py:2413` |
| 31 | `delete_edoc` | Two-step wipe document binary | `entry_id`, `confirmation_token?` | preview OR executed | `client.get_entry` + token + `client.delete_edoc` | 88 | `server.py:2632` |
| 32 | `delete_pages` | Two-step delete specific pages | `entry_id`, `page_range`, `confirmation_token?` | preview OR executed | `client.get_entry` + token + `client.delete_pages` | 112 | `server.py:2721` |

### Thin-wrapper inventory

Tools that make exactly one client call with no aggregation, no
validation, no transformation beyond clamping or escaping:

`get_entry`, `get_entry_by_path`, `get_field_values`,
`list_repositories`, `list_field_definitions`, `list_tag_definitions`,
`list_template_definitions`, `list_link_definitions`,
`get_audit_reasons`, `get_task_status`, `set_fields`, `set_tags`,
`set_links`, `remove_template`.

**14 of 32 tools are thin proxies.** Per the user's composability
directive, thin wrappers are not automatic deletion candidates — small
atomic tools enable agent composition. The audit focuses on
*redundancy* (multiple tools serving identical purposes) and
*opportunities to add atomic tools that close workflow gaps*, not on
headcount reduction.

---

## 1b. Workflow mapping

Eight intent-level workflows representative of real Laserfiche use,
mapped onto the current tool set.

| Workflow | Tool chain | Length | Pain points |
|---|---|---|---|
| A. Find a doc by topic, read its contents | `search_natural` → `get_entry` → `get_document_edoc(mode="text")` | 3 calls | Agent must know v1-vs-v2 quirks; on v1 the read path is `get_document_edoc(mode="text")`, on v2 it's `get_document_text`. |
| B. Read an entry's template metadata | `get_entry` → `get_field_values` | 2 calls | Clean. |
| C. Classify and set metadata | `assign_template` → `merge_fields` | 2–3 calls | Agent must reason about `set_fields` (destructive) vs `merge_fields` (safe). |
| D. Import a doc with template + fields | `import_document` | 1 call | Atomic. Clean. |
| E. Rename + move to archive | `rename_entry` then `move_entry`, OR `move_entry(new_name=...)` | 1–2 calls | Two-step token flow per op; appropriate safety, but each op has its preview/execute multiplexed through one tool. |
| F. Discover template's required fields, then assign | `list_template_definitions` → `list_field_definitions` (manually filter `isRequired`) → `assign_template` | 3–4 calls | `list_field_definitions` is a fire hose; no `get_template_fields(name)` shortcut. |
| G. Audited delete | `get_audit_reasons` → `delete_entry` (preview) → `delete_entry` (execute, with token + audit_reason_id) | 3 calls | Audit reasons aren't surfaced inside the preview response itself; the agent has to know to call `get_audit_reasons` first. |
| H. Async copy a subtree | `copy_entry` → `wait_for_task` | 2 calls | Clean. |

These eight workflows do not cap the surface. The LLM will compose
the tools in ways we don't anticipate (e.g., dedup-and-archive across
folders, rotate a field value across a cohort of entries, etc.). The
audit is observational, not prescriptive about workflow coverage.

---

## 1c. Principle violations

### Principle 1 — Don't port the API verbatim

The current surface is at 32 tools. The user's directive: keep API
coverage, but collapse genuine redundancies. The violations below are
the redundancies and the gaps that force unnecessary chaining.

- **Duplicate set/merge pairs for the same write target.**
  - `set_fields` (`server.py:1503`) and `merge_fields` (`server.py:1546`)
    both operate on the same `/Entries/{id}/fields` endpoint with
    different semantics (overwrite vs. merge). Forces the agent to
    reason about safety semantics on every call.
  - `set_tags` (`server.py:1610`) and `merge_tags` (`server.py:1649`)
    have the same pattern.
- **`remove_template`** (`server.py:1887`) is the degenerate case of
  `assign_template(template_name=None)`. Two tools where one with a
  nullable parameter would do.
- **`get_task_status`** (`server.py:1128`) is the no-wait variant of
  `wait_for_task` (`server.py:1165`). One tool with `timeout=0`
  semantics would cover both.
- **No scoped variant of `list_field_definitions`.** Workflow F (see
  1b) requires the agent to fetch every field definition in the repo
  (potentially 500+) and filter client-side for `isRequired==true` on
  a single template. A `get_template_fields(template_name)` tool
  would close this gap.

### Principle 2 — Security lives in the host

The MCP must defend itself; never assume the protocol enforces consent.
The codebase has a strong base (SecretStr, no path-in-URL composition,
quote-escaping in search queries, path-fence framework), but several
defense-in-depth gaps:

- **Path-fence does not reject `..` traversal.** `permissions.py:20-45`
  normalizes paths (lowercase, slash-unification) but does not strip
  or refuse `..` segments. The check is prefix-based with a boundary,
  so `\Sandbox\..\Secret` normalized to `\sandbox\..\secret` matches
  an `\sandbox` allow prefix. Server-side ACL is the real fence, but
  defense-in-depth is the principle.
- **Untyped `fields: dict[str, list[Any]]`** parameter on
  `set_fields` (`server.py:1505`), `merge_fields` (`server.py:1548`),
  `assign_template` (`server.py:1825`), `create_folder`
  (`server.py:1924`), and `import_document` (`server.py:2043`). No
  client-side validation against the repository's declared field
  types — an LLM can supply a string for a `ShortInteger` field and
  receive an opaque 400.
- **`page_range`** in `delete_pages` (`server.py:2723`) is documented
  as a CSV of integers and ranges (`"1-3,5"`) but is not validated
  before being sent to the server. Malformed input fails server-side
  with a generic 400.
- **Entry-name validation absent.** `rename_entry.new_name`
  (`server.py:2204`), `create_folder.name` (`server.py:1936`),
  `copy_entry.name`, and `import_document.name` document "no
  backslashes" but enforce nothing.
- **Tag, template, and field name pre-flight absent.** `set_tags`
  (`server.py:1610`), `assign_template` (`server.py:1822`),
  `set_fields` (`server.py:1505`): if the agent hallucinates a tag,
  template, or field name, the server returns an opaque 400. A cached
  lookup against `list_*_definitions` would catch this client-side
  and return a structured slug listing valid names.
- **Entry-type validation absent.** `list_folder` (`server.py:501`)
  and `move_entry.new_parent_id` (`server.py:2284`) accept any
  integer; passing a Document ID fails server-side with no client
  pre-check.

### Principle 3 — Design for finite context

Tool descriptions, parameter names, error messages, and return
payloads are all prompt engineering. A 50 KB JSON dump from a search
endpoint is a bug.

- **`get_document_edoc(mode="bytes")` worst case ~33 MB.** Cap is
  `LF_EDOC_MAX_BYTES=25 MB` (`config.py:165`); base64 encoding
  expands that 1.33× → ~33 MB of text in a single tool response,
  enough to fill an entire model context window.
  (`server.py:803-849`).
- **`list_field_definitions` returns 30–50 KB unscoped.** Default
  `max_results=25` clamped by `LF_MAX_RESULTS_CEILING=200`
  (`config.py:153`). On a repo with hundreds of fields, agents tend
  to crank the page size to "see everything" and burn context.
  (`server.py:964`).
- **No `response_format` parameter anywhere.** No tool offers
  `Literal["concise","detailed"]` to trade response richness for
  token cost. Every read returns the full record set.
- **No field-projection affordance.** `list_folder` and `get_entry`
  always return the full entry shape (id, name, type, parent_id,
  full_path, template_name, page_count, timestamps, ...). No
  `include_fields=["id","name"]` parameter to slim a list response.
- **`search_entries` HTTP 400 is opaque.** When the SimpleSearches
  endpoint rejects a query, the agent gets a generic `server_error`
  slug with the raw upstream message but no hint that the query
  syntax was wrong (`server.py:155-159`). `search_natural` is the
  fallback but the agent might not know to use it.
- **Long docstring on `move_entry`.** ~400 lines of prose
  (`server.py:2284-2310`). Hard to scan when fitting in context.

### Principle 4 — Design for hallucinated inputs

Parameter names should be unambiguous, enums should be enums,
required vs optional should be obvious.

- **`template_name`, tag names, and field names are free-text
  strings.** No `Literal` enum (these aren't finite at design time —
  templates vary by repo — so the right fix is client-side validation,
  not enum typing). `server.py:1825, 1610, 1505`.
- **`folder_id` and `new_parent_id` accept any `int`.** `list_folder`
  (`server.py:501`), `move_entry` (`server.py:2284`). Passing a
  Document ID 400s server-side; no client check.
- **`fields: dict[str, list[Any]]`** is too loose. No pydantic shape
  for the inner update record.
- **`links: list[dict[str, Any]]`** on `set_links` (`server.py:1713`)
  documents `{"targetId": int, "linkTypeId": int}` shape in prose;
  not enforced.
- **`content_type` on `import_document` is free-text** (`server.py:2043`).
  Could be a `Literal` over known MIME types.
- **Positive example to emulate:** `mode: Literal["info","bytes","text"]`
  on `get_document_edoc` (`server.py:752`).

---

## 1d. Thin-wrapper / redundancy candidates

Per the composability directive: thin wrappers are NOT auto-deletion
candidates. The candidates below are *redundancies* where two or more
tools serve the same purpose, or *gaps* where a new atomic tool would
close a workflow.

| Action | Tool(s) involved | Resolution |
|---|---|---|
| Collapse | `set_fields` + `merge_fields` | One tool `update_fields(updates, mode: Literal["merge","replace"]="merge")` |
| Collapse | `set_tags` + `merge_tags` | One tool `update_tags(add: list[str]=[], remove: list[str]=[])` |
| Collapse | `assign_template` + `remove_template` | One tool: `assign_template(template_name=None)` clears |
| Collapse | `get_task_status` + `wait_for_task` | One tool `wait_for_task(timeout=0)` for no-wait, otherwise polls |
| Add new | (no current tool) | `get_template_fields(template_name)` — scoped variant of `list_field_definitions` filtered to one template |
| Add new | (no current tool) | `list_template_required_fields(template_name)` — even more scoped: only `isRequired==true` for that template (optional, may fold into `get_template_fields` with a flag) |

Pass 3 will split the 5 preview/execute multiplexers into 10 separate
tools as a composability win (see `AUDIT_DESIGN.md` section 1g).

**Net surface change:** -4 collapses, +1 new tool (`get_template_fields`),
+5 net from preview/execute splits = **~36 tools** post-refactor. The
goal is composability, not headcount; the count grows slightly while
every tool does one thing.

---

## 1e. Security surface

### Strong (no action required)

- **Credentials are wrapped in `SecretStr`** (`auth.py:53, 119`) and
  `.get_secret_value()` is called only at token-exchange time
  (`auth.py:91, 147`), never in logs.
- **Token refresh ~30s before expiry** (`auth.py:76, 134`). No 401
  retry loop; cached token is reused until refresh.
- **Base URL constructed via `urljoin()`** (`client.py:86-101`). No
  agent-supplied parameter ever reaches the hostname portion of a URL.
  `LF_REPO_API_URL` is operator-controlled config, not agent input.
- **Search-query quote-escaping** (`server.py:195, 198`): inner
  double-quotes are escaped before being placed inside `="..."` value
  spans.
- **Confirmation tokens** for destructive ops (`confirmation.py`):
  HMAC-signed, bound to `(operation, entry_id, entry_name)`, 5-min
  TTL, server-restart invalidates all tokens.

### Gaps to close

1. **`..` traversal in path-fence** (`permissions.py:20-45`). Reject
   any path containing a `..` segment after normalization.
2. **Entry-name backslash enforcement.** `rename_entry`,
   `create_folder`, `import_document`, `copy_entry`: validate `name` /
   `new_name` contains no `\` or `/`. Structured `invalid_name` slug
   before the API call.
3. **`page_range` regex validation** in `delete_pages`. Client-side
   regex `^(\d+(-\d+)?)(,\d+(-\d+)?)*$` before sending.
4. **Field-type pre-flight** in `set_fields`/`merge_fields`/
   `assign_template`/`create_folder`/`import_document`. Cached
   `list_field_definitions` lookup; reject obvious mismatches with
   `invalid_field_value` slug listing the field's declared type.
5. **Field-name / tag-name / template-name pre-flight.** Cached
   lookups; reject unknown names with structured slugs that include
   the valid names list.
6. **Entry-type validation** for tools that take a folder ID
   (`list_folder.folder_id`, `move_entry.new_parent_id`,
   `create_folder.parent_id`, `import_document.parent_id`,
   `copy_entry.parent_id`). Pre-fetch entry; reject with
   `expected_folder_got_document` if mismatch.

### Other notes

- **`server_message` in error responses** is parsed from upstream
  ProblemDetails `title`/`message` (`server.py:1296, 1352`) and CAN
  contain entry names. Documented as business-safe; no internal
  hostnames or credentials reach the agent through this channel.
- **Path-fence is intentionally case-insensitive** (matches Laserfiche
  semantics). Document explicitly. No change needed.

---

## 1f. Context-cost review

Estimated worst-case payload size per tool (in KB), plus pagination /
projection / summarization affordances. Calculated from default and
ceiling values in `config.py`.

| Tool | Typical | Worst case | Pagination | Field projection | Summary mode |
|---|---|---|---|---|---|
| `get_document_edoc(mode="bytes")` | ~25 KB info | **~33,000 KB** | `max_bytes` override | — | — |
| `list_folder(max_results=200)` | ~5 KB | ~500 KB | `max_results` + `skip` | **missing** | **missing** |
| `search_natural` | ~5 KB | ~400 KB | `max_results` (cap 100) | **missing** | **missing** |
| `list_field_definitions` | ~5 KB | ~50 KB | `max_results` + `skip` | **missing** | **missing** |
| `list_template_definitions` | ~3 KB | ~20 KB | `max_results` + `skip` | **missing** | **missing** |
| `list_tag_definitions` | ~1 KB | ~10 KB | `max_results` + `skip` | **missing** | **missing** |
| `list_link_definitions` | ~1 KB | ~10 KB | `max_results` + `skip` | **missing** | **missing** |
| `get_document_text` | ~50 KB | ~50 KB (capped) | — | — | — |
| `get_field_values` | ~2 KB | ~10 KB | — | **missing** | — |
| `get_entry` | ~1 KB | ~2 KB | — | **missing** | — |
| `get_entry_by_path` | ~1 KB | ~2 KB | — | **missing** | — |
| `list_repositories` | ~500 B | ~2 KB | — | — | — |
| `get_audit_reasons` | ~1 KB | ~5 KB | — | — | — |
| `get_task_status` / `wait_for_task` | ~500 B | ~2 KB | — | — | — |
| `search_entries`, `search_by_name` | ~5 KB | ~100 KB | `max_results` + `next_link` | **missing** | **missing** |

### Top 3 context hogs

1. **`get_document_edoc(mode="bytes")` — ~33 MB worst case.** The
   max_bytes cap is enforced (`server.py:827`) but no warning fires
   below the cap. Documentation should steer agents toward `mode="text"`
   or `mode="info"` for large documents.
2. **`list_folder(max_results=200)` — ~500 KB.** Always returns full
   entry metadata. Adding `include_fields=["id","name","entry_type"]`
   would slim typical browse calls by 80%.
3. **`search_natural` / `search_entries` — ~400 KB / ~100 KB.** Same
   structural issue: full entry metadata in each result, no projection.

### Remediations (carry into PLAN.md)

- Add `include_fields: list[str] | None = None` to `list_folder`,
  `get_entry`, `search_entries`, `search_by_name`, `search_natural`,
  `get_field_values`. When set, response is projected to the named
  fields only (with id always preserved).
- Add `summary_only: bool = False` to the four
  `list_*_definitions`. When true, response is
  `{count: int, names: list[str]}` instead of full definitions.
- Document the `get_document_edoc(mode="bytes")` 33 MB ceiling
  prominently in the tool docstring; recommend `mode="text"` for
  summarization workflows.

---

## End of Pass 1 audit

Decisions deferred to `PLAN.md`: final tool surface (with collapses
and new tools), parameter schemas, migration order, test strategy,
breaking-change policy for the v2.0.0 release.

Sibling audits:
- Pass 2 (errors & observability) → `AUDIT_ERRORS.md`
- Pass 3 (tool design — naming, schemas, descriptions) → `AUDIT_DESIGN.md`
