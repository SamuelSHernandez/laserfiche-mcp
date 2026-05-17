# AUDIT_DESIGN.md — laserfiche-mcp Pass 3: Tool Design

Read-only audit of the v1.5 codebase against the principle that **tool
design is prompt engineering for an agent with finite context, no
memory, and a tendency to invent plausible parameters.** Every name,
description, parameter, default, and return field is a prompt fragment.

Source of truth for `PLAN_DESIGN.md`. Every claim cites `file:line`.

---

## 1a. Naming audit

Pass 3's authoritative convention: `laserfiche_{resource}_{verb}` —
resource segment first, then verb (e.g., `laserfiche_entry_get`,
`laserfiche_field_set`). The Laserfiche prefix prevents collisions
when the agent has other MCP servers (GitHub, Slack, SQL) loaded
alongside; the resource-first structure lets the model select within
the server by resource.

### Current state

**0 of 32 tools follow `laserfiche_{resource}_{verb}`.** All current
names are verb-first with no namespace prefix. The MCP framework
prepends `mcp__laserfiche__` at the wire level, but the registered
tool names themselves are bare.

| Current name | Follows convention? | Resource | Verb | Proposed name |
|---|---|---|---|---|
| `search_entries` | N | entry | search | `laserfiche_entry_search` |
| `search_by_name` | N | entry | search_by_name | `laserfiche_entry_search_by_name` |
| `search_natural` | N | entry | search_natural | `laserfiche_entry_search_natural` |
| `list_folder` | N | folder | list | `laserfiche_folder_list` |
| `get_entry` | N | entry | get | `laserfiche_entry_get` |
| `get_entry_by_path` | N | entry | get_by_path | `laserfiche_entry_get_by_path` |
| `get_field_values` | N | field_values | get | `laserfiche_field_values_get` |
| `get_document_text` | N | document | get_text | `laserfiche_document_get_text` |
| `get_document_edoc` | N | document | get_edoc (mixed-mode) | split — see 1g below |
| `list_repositories` | N | repository | list | `laserfiche_repository_list` |
| `list_field_definitions` | N | field_definition | list | `laserfiche_field_definition_list` |
| `list_tag_definitions` | N | tag_definition | list | `laserfiche_tag_definition_list` |
| `list_template_definitions` | N | template_definition | list | `laserfiche_template_definition_list` |
| `list_link_definitions` | N | link_definition | list | `laserfiche_link_definition_list` |
| `get_audit_reasons` | N | audit_reason | list (read-only enumeration) | `laserfiche_audit_reason_list` |
| `get_task_status` | N | task | get_status | folds into `laserfiche_task_wait` (timeout=0) |
| `wait_for_task` | N | task | wait | `laserfiche_task_wait` |
| `set_fields` + `merge_fields` | N | field | set / merge | collapsed → `laserfiche_field_update` (mode enum) |
| `set_tags` + `merge_tags` | N | tag | set / merge | collapsed → `laserfiche_tag_update` (add/remove) |
| `set_links` | N | link | set | `laserfiche_link_update` (mode enum) |
| `assign_template` + `remove_template` | N | template | assign / remove | collapsed → `laserfiche_template_assign` (template_name=None clears) |
| `create_folder` | N | folder | create | `laserfiche_folder_create` |
| `copy_entry` | N | entry | copy | `laserfiche_entry_copy` |
| `import_document` | N | document | import | `laserfiche_document_import` |
| `rename_entry` (preview + execute) | N | entry | rename | split → `laserfiche_entry_rename_preview` + `laserfiche_entry_rename_execute` |
| `move_entry` (preview + execute) | N | entry | move | split → `laserfiche_entry_move_preview` + `laserfiche_entry_move_execute` |
| `delete_entry` (preview + execute) | N | entry | delete | split → `laserfiche_entry_delete_preview` + `laserfiche_entry_delete_execute` |
| `delete_edoc` (preview + execute) | N | document_edoc | delete | split → `laserfiche_document_edoc_delete_preview` + `_execute` |
| `delete_pages` (preview + execute) | N | document_pages | delete | split → `laserfiche_document_pages_delete_preview` + `_execute` |

### Resource taxonomy (singular forms used in names)

`entry`, `folder`, `document`, `field`, `field_values`,
`field_definition`, `tag`, `tag_definition`, `template`,
`template_definition`, `link`, `link_definition`, `repository`,
`audit_reason`, `task`, `document_edoc`, `document_pages`. Singular
for individual ops, qualified compound (e.g., `field_definition`,
`document_edoc`) where the resource has sub-aspects.

### Critical finding

**Every tool needs renaming.** This is a mechanical refactor over 32
existing entry points, plus 5 splits adding new entry points. Total
final tool count: **36 tools post-refactor**.

---

## 1b. Search-first review

Pass 3's principle: default shape is `search_entries(query, limit=20)`.
`list_*` only when the result set is bounded-by-construction (e.g., a
fixed enum of ~12 items).

| Tool | Shape | Bounded? | Verdict |
|---|---|---|---|
| `search_entries` | search | n/a (paginated) | ✓ correct |
| `search_by_name` | search | n/a | ✓ correct |
| `search_natural` | dual-mode (guidance + paginated search) | n/a | ✓ correct |
| `list_folder` | list (paginated by `max_results`+`skip`) | n/a | ✓ correct — bounded per-call by pagination |
| `list_repositories` | list | **YES** — usually 1–5 repos per server | ✓ correct (bounded by construction) |
| `list_field_definitions` | list | **partial** — bounded by repo schema, typically 50–500 fields | ⚠ borderline — at the top end (~500 fields), each ~50 B summary = ~25 KB. Worst case 50 KB. Not a context bomb but no `summary_only` mode exists. |
| `list_tag_definitions` | list | **YES** — 0–50 tags typically | ✓ correct |
| `list_template_definitions` | list | **YES** — 5–50 templates typically | ✓ correct |
| `list_link_definitions` | list | **YES** — 2–20 link types | ✓ correct |
| `get_audit_reasons` | list (grouped by op) | **YES** — usually <20 reasons total | ✓ correct |

### Verdict

**Search-first principle is well-respected.** All four `list_*_definitions`
tools enumerate sets that ARE bounded by construction (repository
schema). None of them are context bombs in normal operation
(`list_field_definitions` is the closest to the line at ~50 KB worst
case — see `AUDIT.md` 1f).

**The actual context bomb is `get_document_edoc(mode="bytes")` — ~33 MB.**
This is a single-document bomb, not a list-shape problem. Addressed in
PLAN_DESIGN's `response_format` and docstring guidance.

---

## 1c. Description audit

Rubric (5 components, score 0–5):
1. One-sentence "what it does" at the top.
2. "When to use" — concrete signals.
3. "When NOT to use" — disambiguate from neighboring tools.
4. Return shape — what the agent gets back.
5. Constraints/limits — caps, gotchas, server quirks.

### Bottom 3 (lowest-scoring docstrings)

#### `list_repositories` — 2/5 (`server.py:910-938`)

Score breakdown:
- ✓ (1) Has clear "List the repositories this account can reach"
- ✓ (2) Has "Useful for confirming which repository the server is pointed at"
- ✗ (3) No "when NOT to use" — agent might call this to enumerate all
  entries in a repo (very different use case)
- ✓ (4) Has return shape (including fallback mode)
- ✗ (5) No mention of: how many repos to expect, that the endpoint
  is sometimes disabled (mentioned in passing in fallback shape but
  not stated as a constraint)

Rewrite direction: explicitly state "Do NOT use to enumerate entries
within a repository — use `laserfiche_entry_search` or
`laserfiche_folder_list` for that. This tool answers 'which repos
does my account have' only."

#### `get_audit_reasons` — 2/5 (`server.py:1104-1126`)

Score breakdown:
- ✓ (1) Clear: "Return the audit-reason codes the authenticated user is allowed to supply"
- ✓ (2) "Use before `delete_entry` ... when LF_REQUIRE_AUDIT_REASON=true"
- ✗ (3) No "when NOT to use" — when LF_REQUIRE_AUDIT_REASON=false, this is unnecessary; the agent will burn a call learning that
- ✓ (4) Return shape documented (grouped dict)
- ✗ (5) No mention of empty-response cases or that this is a pre-flight check, not a primary tool

Rewrite direction: "Do NOT call unless `LF_REQUIRE_AUDIT_REASON=true`
or your operator policy requires audited deletes. When this is unset,
`laserfiche_entry_delete_preview` won't require an audit_reason_id."

#### `list_field_definitions` — 2/5 (`server.py:964-999`)

Score breakdown:
- ✓ (1) "List every field definition in the repository"
- ✓ (2) "Use before authoring a field-based search query or preparing a field update"
- ✗ (3) No "when NOT to use" — fails to disambiguate from "find entries containing field X" (which is `laserfiche_entry_search`)
- ⚠ (4) "Server's raw OData listing" — not very informative
- ⚠ (5) Pagination mentioned but no size guidance ("expect 50–500 fields on a typical repo")

Rewrite direction: "Do NOT use to find entries with a specific field
value — use `laserfiche_entry_search` with `{[Template]:[Field]=value}`
syntax for that. This is schema-inspection, not data-retrieval. On a
typical repo expect 50–500 fields; use `summary_only=true` if you
just need names."

### Overall description quality (32 tools, summary)

- **Top quartile (4-5/5):** `search_natural`, `delete_entry`,
  `assign_template`, `move_entry`, `get_document_edoc`,
  `import_document`. Long, multi-section docstrings with all 5
  components covered.
- **Middle (3/5):** Most tool docstrings — typically miss "when NOT to
  use" and constraints, otherwise solid.
- **Bottom (2/5):** The three listed above.

Per `AUDIT.md` 1c (Principle 3), `move_entry`'s docstring is too LONG
(400+ lines) to scan quickly. Length is a separate axis from
completeness; a tool can score 5/5 on the rubric and still be too
verbose.

---

## 1d. Return shape audit

Pass 3's principle: agents reason better over human-readable fields
than over raw IDs. Co-locate IDs with names. `{"template_id": 7,
"field_values": {"4": "John"}}` forces another tool call to be useful;
`{"template": "Personnel", "template_id": 7, "fields": {"Applicant Name": "John"}}`
doesn't.

### Strong examples

- **`EntrySummary`** (`models.py:51-72`) — `id`, `name`, `entry_type`
  (string enum: Folder/Document), `parent_id`, `full_path`,
  `creation_time` (ISO 8601), `last_modified_time` (ISO 8601).
  Human-readable + structured.
- **`FieldValue`** (`models.py:75-90`) — `field_name`, `values`,
  `field_type`, `is_multi_value`, `is_required`. Field name AND type
  co-located. No opaque `field_id` only.
- **`EntryDetail`** (`models.py:93-122`) — adds `template_name`,
  `extension`, `is_electronic_document` to `EntrySummary`. All names
  resolved, not just IDs.
- **`SearchResults`** (`models.py:124-142`) — `entries`,
  `total_count`, `next_link`. Cursor in `next_link`. (Missing
  `total_estimate` — see 1e.)

### Weak / raw passthroughs

- **`list_field_definitions`** (`server.py:993-999`) returns the
  server's raw OData with `value: [{id, name, displayName, fieldType,
  ...}, ...]`. Mixed case (`fieldType` not `field_type`), no pydantic
  validation. Same pattern in `list_tag_definitions` (`server.py:1025-1031`),
  `list_template_definitions` (`server.py:1061-1068`),
  `list_link_definitions` (`server.py:1095-1101`).
- **`list_repositories`** (`server.py:910-961`) returns server's
  shape with `value: [{repoId, repoName, ...}]` plus a fallback shape
  on the error path. Mixed case (`repoId`).
- **`get_task_status`** (`server.py:1128-1162`) returns the server's
  raw task payload with PascalCase fields (`OperationToken`,
  `PercentComplete`, `Status`, `RedirectUri`, `EntryId`, ...). No
  pydantic normalization.
- **`get_audit_reasons`** (`server.py:1104-1126`) returns the
  server's grouped dict — keys are PascalCase operation names
  (`deleteEntry`, `exportDocument`).
- **Bool name clarity:** `wait_for_task` returns `timed_out: bool`
  (`server.py:1187-1211`). Clear. But check: `is_electronic_document`,
  `is_required`, `is_multi_value` follow the `is_*` convention — good.
- **Non-ISO timestamps** — pydantic models normalize to ISO, but raw
  OData passthroughs preserve whatever the server sends. On the GC
  IPRS server the raw responses use ISO 8601 (`2026-05-13T15:23:45.789Z`),
  but we shouldn't rely on that for builds with different config.

### Verdict

- **Pydantic-modeled return shapes are good.** Co-locate well, ISO
  timestamps, enum-typed fields.
- **Five raw-OData passthrough tools are weak.** PLAN_DESIGN will
  wrap them in pydantic models with consistent snake_case + ISO.

---

## 1e. Token budget audit

| Tool | Typical | Worst case | `limit`? | Enforced ceiling? | `response_format`? | Pagination |
|---|---|---|---|---|---|---|
| `search_entries` | ~5 KB | ~100 KB | `max_results=25` | `LF_MAX_RESULTS_CEILING=200` (`config.py:153`) | **No** | `next_link` cursor |
| `search_by_name` | ~5 KB | ~100 KB | `max_results` | yes | **No** | `next_link` |
| `search_natural` (results) | ~5 KB | ~400 KB | `max_results=50` (clamped to `LF_MAX_PAGE_SIZE=100`) | `LF_MAX_PAGE_SIZE` (`config.py:158`) | **No** | `next_link` |
| `list_folder` | ~5 KB | ~500 KB at top of ceiling | `max_results=25` | `LF_MAX_RESULTS_CEILING=200` | **No** | `skip` offset |
| `get_entry` / `get_entry_by_path` | ~1 KB | ~2 KB | n/a | n/a | **No** | n/a |
| `get_field_values` | ~2 KB | ~10 KB (entry with 50 fields) | n/a | n/a | **No** | n/a |
| `get_document_text` | up to 50 KB | 50 KB (capped) | `max_chars=50_000` | enforced | **No** | n/a |
| `get_document_edoc` info | ~200 B | ~200 B | n/a | n/a | n/a | n/a |
| `get_document_edoc` bytes | ~25 MB | **~33 MB** (base64) | `max_bytes` override | `LF_EDOC_MAX_BYTES=25 MB` (`config.py:165`) | **No** | n/a |
| `get_document_edoc` text | up to 50 KB | 50 KB (capped) | `text_char_limit=50_000` | enforced | **No** | n/a |
| `list_repositories` | ~500 B | ~2 KB | n/a | n/a | **No** | n/a |
| `list_field_definitions` | ~5 KB | ~50 KB | `max_results=25` | `LF_MAX_RESULTS_CEILING` | **No** | `skip` |
| `list_template_definitions` | ~3 KB | ~20 KB | `max_results` | yes | **No** | `skip` |
| `list_tag_definitions` | ~1 KB | ~10 KB | `max_results` | yes | **No** | `skip` |
| `list_link_definitions` | ~1 KB | ~10 KB | `max_results` | yes | **No** | `skip` |
| `get_audit_reasons` | ~1 KB | ~5 KB | n/a | n/a | **No** | n/a |
| `get_task_status` / `wait_for_task` | ~500 B | ~2 KB | n/a | n/a | **No** | n/a |
| All write tools (success) | ~1 KB | ~5 KB | n/a | n/a | **No** | n/a |
| Preview responses (5 destructive ops) | ~1 KB | ~2 KB | n/a | n/a | **No** | n/a |

### Top 3 context bombs

1. **`get_document_edoc(mode="bytes")` — 33 MB worst case.** Single
   tool response can fill a 200K-token context. Cap enforced, but no
   below-cap warning to the agent.
2. **`list_folder(max_results=200)` — 500 KB.** Full entry metadata
   per item. No `include_fields` projection.
3. **`search_natural(max_results=100)` — 400 KB.** Same shape issue.

### `response_format` parameter is missing everywhere

Zero tools offer `Literal["concise", "detailed"]` to let the agent
trade richness for tokens. The principle prescribes it for every tool
returning variable-length data.

### Pagination shape

- `search_entries`, `search_by_name`, `search_natural`, `list_folder`
  all use `next_link` (OData cursor). ✓ correct shape.
- `list_*_definitions` use `skip` + `max_results` (page-number style).
  Per principle, **prefer opaque continuation cursors over page
  numbers** — but for bounded sets (5–50 items typically) this is
  acceptable. Most callers will fetch all in one call.
- **No `total_estimate` companion** to `next_link` anywhere — the
  agent can't know "is there a lot more or just a tiny bit more?"
  before paging.

### Remediation (carry into PLAN_DESIGN.md)

- Add `response_format: Literal["concise","detailed"]="concise"` to
  `search`, `search_by_name`, `search_natural`, `list_folder`,
  `entry_get`, `entry_get_by_path`, `field_values_get`,
  `document_get_text`, the four `*_definition_list` tools, and the
  task-status tool.
- "Concise" definition for each tool:
  - For entry-returning tools: `id`, `name`, `entry_type`, `full_path`.
  - For field-list tools: `name`, `field_type`, `is_required`.
  - For document-text tools: trimmed to a smaller default.
- Add `total_estimate: int | None` to every paginated response.
- Add `include_fields: list[str] | None = None` to entry-list/get
  tools as a finer-grained projection.
- Document the 33 MB ceiling on `document_get_bytes` (post-split)
  prominently; recommend `document_get_text` for summarization.

---

## 1f. Schema audit

### Positive example (the convention we want to spread)

`get_document_edoc.mode` (`server.py:752`):

```python
mode: Literal["info", "bytes", "text"] = "info"
```

Enum at the type level. Pydantic-friendly. Schema-aware. The LLM
picks from the enum, not from prose. This is the pattern.

### Weak patterns

1. **`fields: dict[str, list[Any]]`** on `set_fields` (`server.py:1505`),
   `merge_fields` (`server.py:1548`), `assign_template`
   (`server.py:1825`), `create_folder` (`server.py:1924`),
   `import_document` (`server.py:2043`). The agent sees only
   `dict[str, list[Any]]` in the schema; the structure is documented
   in prose only. No pydantic model for the inner update.
   - Remediation: define `FieldUpdate(field_name: str, values:
     list[str | int | bool])` model.
2. **`links: list[dict[str, Any]]`** on `set_links` (`server.py:1713`).
   Schema documents `{"targetId": int, "linkTypeId": int}` in prose.
   - Remediation: `EntryLink(target_id: int, link_type_id: int,
     direction: Literal["source","target"]?)` model.
3. **`page_range: str`** on `delete_pages` (`server.py:2723`).
   Documented format (`"1,2,3"`, `"1-3,5"`) is enforced server-side
   only.
   - Remediation: Pydantic `validator` with the regex from
     `AUDIT.md` 1e or a wrapper type `PageRangeSpec`.
4. **`tags: list[str]`** on `set_tags` (`server.py:1610`). Tags must
   exist as repo definitions (`list_tag_definitions`); not validated
   client-side.
   - Remediation: pre-flight against cached tag definitions.
5. **`template_name: str`** on `assign_template` (`server.py:1825`),
   `create_folder.template_name` (`server.py:1924`),
   `import_document.template_name` (`server.py:2043`). Free text.
   - Remediation: pre-flight against cached template definitions.
6. **`content_type: str | None`** on `import_document` (`server.py:2043`).
   Defaults to auto-detect. Could be a `Literal` over known MIME types
   for the most common cases (PDF, plain text, common Office docs).
7. **`folder_id: int`** on `list_folder` (`server.py:501`) and
   `new_parent_id: int` on `move_entry` (`server.py:2284`) accept any
   int. Passing a Document ID fails server-side. No pre-flight type
   check.
   - Remediation: pre-flight entry-type check before the API call.

### Field descriptions and examples

- **Field descriptions exist** in tool docstrings for nearly every
  parameter. ✓
- **`examples=[...]` is NOT used** anywhere. Pydantic supports
  `Field(..., examples=["{LF:Name=\"Onboarding*\"}"])`. Adding these
  helps the LLM generate well-formed calls on first try.
- **No `description=` on `Field()` declarations** — descriptions live
  only in the docstring. FastMCP can read either, but `Field(...,
  description=...)` shows up in the JSON-schema MCP exposes to clients,
  which is what the LLM actually sees on tool selection.

### Remediation (carry into PLAN_DESIGN.md)

- Move parameter descriptions from docstrings into `Field(...,
  description=...)` so they reach the MCP schema layer (the LLM's
  decision surface).
- Add `examples=[...]` on every parameter where the shape is non-obvious
  (search queries, paths, field-update structures).
- Replace bare `str` params with `Literal[...]` where finite, or
  pydantic-validated types (`PageRangeSpec`, `FieldUpdate`, `EntryLink`)
  where structured.
- Add cached pre-flight validation for `template_name`, tag names,
  field names, and entry-type checks.

---

## 1g. Composability audit

Pass 3's principle: tools that do more than one thing collapse the
agent's planning surface. Resist conditional behavior based on input
shape.

### Violations

#### Preview/execute multiplexing (5 tools)

Each of these tools handles **two distinct operations** —
preview and execute — through a single function. The discrimination
is via `confirmation_token=None` (preview) vs. set (execute):

- `rename_entry` (`server.py:2161-2282`)
- `move_entry` (`server.py:2284-2410`)
- `delete_entry` (`server.py:2413-2629`)
- `delete_edoc` (`server.py:2632-2719`)
- `delete_pages` (`server.py:2721-2832`)

**Composability violation:** the agent reads one tool description but
the tool has two execution paths with different return shapes. The
LLM has to reason about which mode it's invoking.

**Resolution (per user-approved decision):** split each into
preview + execute. 5 tools become 10. Each does one thing.
Confirmation token contract stays the same — preview returns it,
execute requires it.

#### Mode-multiplexing on `get_document_edoc` (`server.py:749-905`)

One tool handles three semantically-different operations via
`mode: Literal["info","bytes","text"]`:
- `info`: metadata only (~200 B response)
- `bytes`: base64 of binary content (up to ~33 MB)
- `text`: extracted text via pypdf (~50 KB)

**Composability tension:** the `mode` parameter IS a `Literal` enum
(positive — the LLM picks from a closed set), but each mode has a
different response shape, different cost profile, and different
failure modes. Three semantically distinct operations under one tool
name.

**Resolution:** split into `laserfiche_document_get_info`,
`laserfiche_document_get_bytes`, `laserfiche_document_get_extracted_text`.
Three small composable tools beat one mode-multiplexed mega-tool.

#### Dual-mode `search_natural` (`server.py:318-499`)

Same shape: `lf_query=None` → guidance mode (returns templates and
grammar); `lf_query="..."` → results mode. **However**, this is
INTENTIONAL and good — Mode A is the LLM-facing affordance that
makes Mode B usable. The two modes share state (`question`,
`folder_path`) and the dual-mode contract is documented carefully.

**Verdict:** Keep `search_natural` as-is. The dual-mode design here
is principled (guidance-then-execute is a meta-pattern, not a
multiplex), and the response model `SearchNaturalResponse` carries
mode-discriminated fields.

### Collapses approved per user direction (carry from `AUDIT.md` 1d)

These are NOT composability violations — they're genuine redundancy
(two tools, same target, different behavior selector):

- `set_fields` + `merge_fields` → `laserfiche_field_update(mode: Literal["merge","replace"])`
- `set_tags` + `merge_tags` → `laserfiche_tag_update(add, remove)`
- `assign_template` + `remove_template` → `laserfiche_template_assign(template_name=None)`
- `get_task_status` + `wait_for_task` → `laserfiche_task_wait(timeout=0)`

These collapses ADD a parameter that encodes intent (mode enum,
optional name, timeout=0) rather than splitting an existing tool.
The principle is "compose, don't multiplex" — these aren't multiplexed
with different return shapes; they're variations on one operation.

---

## 1h. Cross-pass consistency

Pass 1 proposed `lf_*` prefix with verb-first ordering. Pass 3
specifies `laserfiche_{resource}_{verb}` with resource-first ordering.
**Pass 3 is authoritative per the user's explicit direction.**

### Reconciled decisions

- **Prefix:** `laserfiche_` (not `lf_`). Self-documenting; matches MCP
  ecosystem conventions seen in claude.ai's Figma/Gmail/Drive
  servers.
- **Order:** resource first, verb last (e.g., `laserfiche_entry_get`,
  not `laserfiche_get_entry`). Groups related tools alphabetically
  for the LLM's tool list scan.
- **Resource segments are singular** (`entry`, `field`, `tag`,
  `template`, `link`, `folder`, `document`, `repository`,
  `audit_reason`, `task`).
- **Compound resources** use snake_case (`field_definition`,
  `field_values`, `document_edoc`, `document_pages`).
- **Verb segments** are short and intent-aware (`get`, `list`,
  `search`, `create`, `update`, `delete`, `assign`, `copy`, `rename`,
  `move`, `import`, `wait`, `preview`, `execute`).

### Pass 1 decisions held under Pass 3 lens

- Collapses (1d in AUDIT.md): held.
- Security validations (Principle 2): held.
- Context-cost remediations (Principle 3): held + add
  `response_format` per Pass 3.
- Hallucinated-input validations (Principle 4): held + use
  `Literal[...]` everywhere per Pass 3.

### Final tool list (36 tools, post-refactor)

**Reads (19):**
- `laserfiche_entry_search`, `laserfiche_entry_search_by_name`, `laserfiche_entry_search_natural`
- `laserfiche_folder_list`
- `laserfiche_entry_get`, `laserfiche_entry_get_by_path`
- `laserfiche_field_values_get`
- `laserfiche_document_get_text`
- `laserfiche_document_get_info`, `laserfiche_document_get_bytes`, `laserfiche_document_get_extracted_text` *(from `get_document_edoc` split)*
- `laserfiche_repository_list`
- `laserfiche_field_definition_list`, `laserfiche_tag_definition_list`, `laserfiche_template_definition_list`, `laserfiche_link_definition_list`
- `laserfiche_template_field_list` *(new — scoped variant of field-definition listing)*
- `laserfiche_audit_reason_list`
- `laserfiche_task_wait` *(collapses `get_task_status` + `wait_for_task`)*

**Writes (17):**
- `laserfiche_field_update` *(collapses `set_fields` + `merge_fields`)*
- `laserfiche_tag_update` *(collapses `set_tags` + `merge_tags`)*
- `laserfiche_link_update` *(replaces `set_links`)*
- `laserfiche_template_assign` *(collapses `assign_template` + `remove_template`)*
- `laserfiche_folder_create`
- `laserfiche_entry_copy`
- `laserfiche_document_import`
- `laserfiche_entry_rename_preview`, `laserfiche_entry_rename_execute`
- `laserfiche_entry_move_preview`, `laserfiche_entry_move_execute`
- `laserfiche_entry_delete_preview`, `laserfiche_entry_delete_execute`
- `laserfiche_document_edoc_delete_preview`, `laserfiche_document_edoc_delete_execute`
- `laserfiche_document_pages_delete_preview`, `laserfiche_document_pages_delete_execute`

---

## End of Pass 3 audit

Decisions deferred to `PLAN_DESIGN.md`: complete per-tool spec (full
`Field(...)` schemas with `description` + `examples` + `Literal`),
final rename map with deprecation-shim policy, projection-mode
contracts, migration order, breaking-change notes for v2.0.0.

Sibling audits:
- Pass 1 (workflow & surface) → `AUDIT.md`
- Pass 2 (errors & observability) → `AUDIT_ERRORS.md`
