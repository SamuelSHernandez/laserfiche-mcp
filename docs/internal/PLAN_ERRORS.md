# PLAN_ERRORS.md — laserfiche-mcp Pass 2: Error Handling & Observability

Source: `AUDIT_ERRORS.md`. Companions: `PLAN.md` (surface) and
`PLAN_DESIGN.md` (naming/schemas).

Target release: v2.0.0.

---

## 2a. Error model

A single pydantic model `ToolError` returned by every tool on failure
(replacing the current ad-hoc `{mode: "error", ...}` dict). Closed set
of 5 canonical kinds; subkind preserves the actionable signal of the
current 14-slug taxonomy.

### `ToolErrorKind` enum

```python
class ToolErrorKind(str, Enum):
    NOT_FOUND = "not_found"
    PERMISSION_DENIED = "permission_denied"
    RATE_LIMITED = "rate_limited"
    INVALID_INPUT = "invalid_input"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
```

The five canonical kinds are stable forever. Adding a sixth requires
a major version bump and a justification (per Pass 2 principle 2).

### `ToolError` model

```python
class ToolError(BaseModel):
    """Structured error returned by every tool on failure.

    Replaces exceptions and ad-hoc error dicts. Agents branch on
    `kind` for category-level decisions and on `subkind` for
    specific remediation cues. `suggested_action` names a concrete
    next step where applicable.
    """

    mode: Literal["error"] = "error"
    operation: str = Field(
        description="The tool name that produced this error."
    )
    kind: ToolErrorKind = Field(
        description=(
            "Closed-set error category. Branch on this for "
            "category-level decisions (retry vs ask user vs abort)."
        ),
    )
    subkind: str = Field(
        description=(
            "Specific subcategory carrying actionable signal. "
            "Stable but additions allowed in minor versions. "
            "Examples: exceeds_batch_cap, invalid_confirmation_token, "
            "missing_required_fields."
        ),
    )
    message: str = Field(
        description=(
            "Human-readable description of what went wrong. "
            "Written for an LLM reader. Never contains stack traces, "
            "URLs, hostnames, or credentials."
        ),
    )
    suggested_action: str | None = Field(
        default=None,
        description=(
            "Concrete next step the agent can take. Names a real "
            "tool when applicable. Null when no tool action will "
            "help (e.g., bad credentials require operator)."
        ),
    )
    retry_after_seconds: int | None = Field(
        default=None,
        description=(
            "Hint for rate-limited responses. Null otherwise. "
            "Agents should sleep this long before retrying the "
            "same operation."
        ),
    )
    request_id: str = Field(
        description=(
            "UUID4 unique to this tool invocation. Pivots into "
            "structured logs. Operators reading log aggregation "
            "can search this id."
        ),
    )
    upstream_trace_id: str | None = Field(
        default=None,
        description=(
            "W3C trace ID from the Laserfiche server's "
            "ProblemDetails response, when available. Pivots into "
            "the Laserfiche server's own logs."
        ),
    )

    # Tool-specific context: per-tool extras like `entry_id`,
    # `template_name`, `missing` (list of field names), etc. Each
    # tool's ToolError documents which extras it includes.
    model_config = ConfigDict(extra="allow")
```

### Return type contract

Every tool's signature changes to `-> dict[str, Any] | ToolError`.
On success: the existing success dict. On failure: a `ToolError`
serialized to dict by FastMCP. The union is necessary because
pydantic models and plain dicts both serialize cleanly through
FastMCP's runtime validation.

**MCP wire-format note:** FastMCP serializes `ToolError` to its
`model_dump()` JSON. The LLM sees a JSON object with `mode: "error"`
at the top level, just like today — but now with stable `kind` and
`subkind` fields. The existing migration from v1.4 (typed-return
tools returning dict[str, Any]) sets up the pattern; this just adds
the typed validation layer.

---

## 2b. Per-tool error mapping

For every tool in the v2 surface (per `PLAN.md` section 2a), the
table below specifies which `kind` + `subkind` it produces under each
upstream failure path and what `suggested_action` (if any) is
attached.

### Reads

| Tool | Upstream failure | `kind` | `subkind` | `suggested_action` |
|---|---|---|---|---|
| `laserfiche_entry_search` | 400 (bad query syntax) | invalid_input | `bad_query_syntax` | "Use laserfiche_entry_search_natural for LLM-guided query construction." |
| All reads | HTTP 401/403 OR LF 9010 OR LF 9528 | permission_denied | `auth_failed` | null (operator must fix creds) |
| All reads | HTTP 404 | not_found | `not_found` | null (verify the id/path) |
| All reads | HTTP 429 | rate_limited | `rate_limited` | null; `retry_after_seconds` set from `Retry-After` header if present |
| All reads | HTTP 5xx | upstream_unavailable | `server_error` | "Retry once after a short delay." |
| All reads | HTTP 405 (build doesn't expose endpoint) | upstream_unavailable | `method_not_allowed` | null (build limitation) |
| `laserfiche_document_get_text` (v1) | client refuses (v2-only) | upstream_unavailable | `method_not_allowed` | "Use laserfiche_document_get_extracted_text instead — fetches the raw edoc and extracts client-side." |
| `laserfiche_folder_list` | folder_id refers to a Document | invalid_input | `expected_folder_got_document` | "Pass a folder ID. Use laserfiche_entry_get to verify entry_type before listing." |
| `laserfiche_repository_list` | endpoint disabled on this build | upstream_unavailable | `endpoint_disabled` | "Fall back to the configured LF_REPOSITORY_ID." (NOTE: returns `mode: "fallback"` instead of `mode: "error"` for this specific case — see PLAN_DESIGN.md for the rationale.) |

### Writes (all writes ALSO inherit the reads' generic failures)

| Tool | Upstream failure | `kind` | `subkind` | `suggested_action` |
|---|---|---|---|---|
| All writes (pre-server) | path outside `LF_WRITE_PATHS_ALLOW` | permission_denied | `path_not_allowed` | "The path is outside the allowlist configured in LF_WRITE_PATHS_ALLOW. Either operate inside an allowed prefix or have your operator add this path." |
| All writes (pre-server) | path contains `..` (new in v2) | permission_denied | `path_traversal_blocked` | "Path-traversal segments ('..') are rejected. Use absolute paths." |
| All writes (pre-server) | tool name not in `LF_WRITE_TOOLS_ALLOWED` | permission_denied | `tool_not_allowed` | null (operator policy) |
| `laserfiche_field_update`, `laserfiche_template_assign`, `laserfiche_folder_create`, `laserfiche_document_import` | LF 9039 OR 9066 (required fields missing server-side) OR validator pre-flight catches it | invalid_input | `missing_required_fields` | "Call laserfiche_template_field_list(template_name, required_only=true) and include each missing field in the `fields=` parameter." Response extras: `missing: list[str]`, `field_details: list[FieldDef]`. |
| `laserfiche_field_update`, `laserfiche_template_assign` (new) | field type mismatch (pre-flight catches it) | invalid_input | `invalid_field_value` | "Field 'X' expects type Y; received Z. Call laserfiche_field_definition_list to see field constraints." |
| `laserfiche_field_update`, `laserfiche_template_assign` (new) | unknown field name | invalid_input | `invalid_field_name` | "Field 'X' is not defined in this repository. Call laserfiche_field_definition_list to see available fields." Extras: `valid_field_names: list[str]` (sample of 20). |
| `laserfiche_tag_update` (new) | unknown tag name | invalid_input | `invalid_tag_name` | "Tag 'X' is not defined. Call laserfiche_tag_definition_list to see available tags." Extras: `valid_tag_names: list[str]`. |
| `laserfiche_template_assign` (new) | unknown template name | invalid_input | `invalid_template_name` | "Template 'X' is not defined. Call laserfiche_template_definition_list to see available templates (case-sensitive)." Extras: `valid_template_names: list[str]`. |
| `laserfiche_link_update` (new) | unknown link type id | invalid_input | `invalid_link_type` | "linkTypeId not defined. Call laserfiche_link_definition_list to see available link types." |
| `laserfiche_entry_rename_*`, `laserfiche_folder_create`, `laserfiche_document_import`, `laserfiche_entry_copy` (new) | name contains `\`, `/`, NUL, or wrong length | invalid_input | `invalid_name` | "Entry names cannot contain backslashes, forward slashes, or null bytes; length must be 1-128." |
| `laserfiche_entry_move_execute`, `laserfiche_folder_create`, `laserfiche_document_import`, `laserfiche_entry_copy` (new) | `parent_id`/`new_parent_id` refers to a Document | invalid_input | `expected_folder_got_document` | "Pass a folder ID. Use laserfiche_entry_get to verify entry_type." |
| All `*_execute` tools | confirmation_token expired/tampered/wrong-entry | invalid_input | `invalid_confirmation_token` | "Re-run the corresponding *_preview tool to get a fresh token. Tokens expire after 5 minutes and are bound to the entry." |
| `laserfiche_entry_delete_execute` | folder exceeds batch cap | invalid_input | `exceeds_batch_cap` | "Pass force_large_delete=true on this execute call. The folder has >LF_DELETE_FOLDER_MAX_DESCENDANTS immediate children." Extras: `immediate_child_count` (when known), `batch_cap`. |
| `laserfiche_entry_delete_execute` | `LF_REQUIRE_AUDIT_REASON=true` and no audit_reason_id supplied | invalid_input | `audit_reason_required` | "Pass audit_reason_id from laserfiche_audit_reason_list. The operator policy requires audit trails on deletes." Extras: `available_audit_reasons: list[AuditReason]`. |
| `laserfiche_document_pages_delete_*` (new) | page_range syntax invalid | invalid_input | `invalid_page_range` | "page_range must match '1', '1-3', '1,2,5', '1-3,5,7-9'. Cannot be empty." |
| `laserfiche_document_import` | file_path not found on MCP server | invalid_input | `file_not_found` | "The MCP server cannot read the file. Verify the path is correct and accessible to the server process." |
| `laserfiche_document_import` | size > `LF_IMPORT_MAX_BYTES` | invalid_input | `size_exceeds_cap` | "File exceeds LF_IMPORT_MAX_BYTES cap. Increase the env var or use a smaller file." Extras: `file_size`, `cap`. |

### Subkind taxonomy (closed at v2.0; additions in minor versions)

```
auth_failed
bad_query_syntax
endpoint_disabled
exceeds_batch_cap
expected_folder_got_document
file_not_found
invalid_confirmation_token
invalid_field_name
invalid_field_value
invalid_link_type
invalid_name
invalid_page_range
invalid_tag_name
invalid_template_name
method_not_allowed
missing_required_fields
not_found
path_not_allowed
path_traversal_blocked
rate_limited
server_error
size_exceeds_cap
tool_not_allowed
audit_reason_required
```

24 subkinds covering every error path in the v2 surface. Each maps to
exactly one of the 5 canonical `kind` values.

---

## 2c. Logging schema

Single structured JSON log line per tool invocation, emitted by a
decorator wired at the tool-registration layer. Schema:

```json
{
  "ts": "2026-05-14T16:32:01.234Z",
  "tool": "laserfiche_entry_get",
  "args": {"entry_id": 84493},
  "duration_ms": 142,
  "outcome": "success",
  "request_id": "5e1b3c2a-0e0a-4f8c-9b6a-2c5e3d4f6a7b",
  "upstream_trace_id": null
}
```

On failure:

```json
{
  "ts": "2026-05-14T16:32:01.234Z",
  "tool": "laserfiche_entry_get",
  "args": {"entry_id": 999999999},
  "duration_ms": 89,
  "outcome": "error",
  "request_id": "5e1b3c2a-0e0a-4f8c-9b6a-2c5e3d4f6a7b",
  "upstream_trace_id": "00-92647f26a3ce0eefba0bb9b5b8b7997c-86f93f712586c4d7-00",
  "error_kind": "not_found",
  "error_subkind": "not_found"
}
```

### Field reference

| Field | Type | Required | Notes |
|---|---|---|---|
| `ts` | string (ISO 8601, UTC, ms precision) | yes | `datetime.now(UTC).isoformat(timespec="milliseconds")`. |
| `tool` | string | yes | Final tool name (`laserfiche_entry_get`, not the deprecated alias). |
| `args` | object | yes | Tool arguments after redaction (see 2d). Empty object `{}` when tool has no args. |
| `duration_ms` | integer | yes | Wall-clock milliseconds from tool entry to return. |
| `outcome` | string | yes | One of `"success"`, `"error"`, `"fallback"` (for `repository_list`). |
| `request_id` | string (UUID4) | yes | Generated at tool entry. Surfaced in `ToolError.request_id` on failure. |
| `upstream_trace_id` | string OR null | yes | Extracted from `LaserficheError.detail.traceId` when present. |
| `error_kind` | string | only on `outcome="error"` | One of the 5 `ToolErrorKind` values. |
| `error_subkind` | string | only on `outcome="error"` | From the subkind taxonomy in 2b. |

### Log level policy

| Outcome | Level |
|---|---|
| `success` | INFO |
| `fallback` | WARNING |
| `error` with `kind` in (`rate_limited`, `upstream_unavailable`) | WARNING |
| `error` with `kind` in (`not_found`, `permission_denied`, `invalid_input`) | INFO (these are agent-correctable; not a server problem) |

Unhandled exceptions (which v2 should never produce on the tool
surface, but `_require_writes_enabled` raises one for config errors)
go to ERROR.

### Implementation

New module `src/laserfiche_mcp/observability.py`. Decorator:

```python
def tool_logger(fn):
    """Wrap a tool to emit one structured log event per call."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        request_id = str(uuid.uuid4())
        start = time.monotonic()
        try:
            result = await fn(*args, **kwargs)
            duration_ms = int((time.monotonic() - start) * 1000)
            _log_event(fn.__name__, kwargs, duration_ms, result, request_id)
            return result
        except Exception as exc:
            duration_ms = int((time.monotonic() - start) * 1000)
            _log_event(
                fn.__name__, kwargs, duration_ms,
                {"mode": "error", "kind": "upstream_unavailable",
                 "subkind": "uncaught_exception"},
                request_id,
            )
            raise
    return wrapper
```

Wired at `@mcp.tool()` registration: the existing decorator chain
becomes `@mcp.tool()` → `@tool_logger` → tool function. The
decorator generates the `request_id` and surfaces it on every error
return (the tool function receives it via a ContextVar or as a
hidden first argument; cleanest is ContextVar).

### Format selector

New env var `LF_LOG_FORMAT: Literal["text","json"] = "text"`
(default preserves backward compat). When `"json"`, swap the default
formatter for one that emits one JSON object per line.

---

## 2d. Redaction plan

Single redaction helper in `observability.py`:

```python
_REDACT_KEYS = {
    "password", "secret", "client_secret", "api_key", "token",
    "authorization", "cookie", "x-api-key", "lf_password",
    "lf_client_secret", "lf_api_key", "confirmation_token",
}

def redact(obj: Any, *, host_pattern: re.Pattern | None = None,
           repo_id: str | None = None) -> Any:
    """Return a redacted copy of obj for safe logging.

    - Replaces values of any key matching _REDACT_KEYS (case-insensitive)
      with the string '<redacted>'.
    - Recurses into nested dicts and lists.
    - Replaces any substring of `Settings.repo_api_url` host with
      '<repo_host>' and any substring matching `repo_id` with '<repo_id>'
      in string values (URLs in retry warnings).
    - Does NOT mutate the input.
    """
```

### Wired at three sites

1. **Per-tool logging decorator (2c above):** redacts `kwargs` before
   logging.
2. **Retry-warning log lines** in `client.py:159-170`: replace
   `request.url` argument with `redact(str(request.url), host_pattern=..., repo_id=...)`.
3. **Auth log lines** in `auth.py:81, 139`: replace `self._token_url`
   with the redacted form.

### Confirmation tokens

HMAC-signed tokens passed to `*_execute` tools are not credentials,
but they're verifier secrets. Adding `confirmation_token` to the
redaction deny-list is conservative.

### Tests for redaction

In `tests/test_observability.py`:

- Dict containing `{"password": "x"}` → `{"password": "<redacted>"}`.
- Nested: `{"creds": {"password": "x"}}` → `{"creds": {"password": "<redacted>"}}`.
- Case-insensitive: `{"Password": "x"}` → redacted.
- URL with repo host: `"http://lf.example.com/...myrepo..."` → host and repo-id substrings replaced.
- Confirmation token: redacted.
- Non-sensitive args (`entry_id`, `query`, `mode`): pass through unchanged.

---

## 2e. Request ID and correlation

### Per-call `request_id`

Generated as UUID4 at tool entry by the `tool_logger` decorator.
Stored in a `ContextVar` so any code in the call stack can access it
without threading it through every function signature.

Surfaced in:
- The structured log line (2c).
- Every `ToolError.request_id` (mandatory field).

NOT surfaced on success responses (would add noise to most calls).
Agents that want correlation on success can grep logs by `tool +
args` — operators almost always need correlation on failure, not
success.

### Upstream `trace_id`

Extracted in `_lf_error_detail` (existing function at
`server.py:1265-1273`). Add:

```python
detail.setdefault("traceId", inner.get("traceId") if inner else None)
```

Then in `_classify_lf_error`, surface as `upstream_trace_id` on the
returned `ToolError`. Also pass to the log line so an operator can
pivot either direction (our request_id → our logs; upstream trace_id
→ LF server logs).

### W3C trace context propagation (NOT in v2.0)

We do NOT inject a `traceparent` header into outbound requests in
v2.0. That'd require integration with the host's tracing stack and
is out of scope for an MCP server. Tracked as future work.

---

## 2f. Migration order

This is Pass 2's contribution to the consolidated migration order
in `PLAN.md` section 2c. Repeated here for clarity.

### Step A — error model alongside existing
1. Add `errors.py` with `ToolError`, `ToolErrorKind`, subkind
   taxonomy.
2. Add `observability.py` with `redact()`, `tool_logger`, and the
   structured log emitter.
3. Wire `tool_logger` at every `@mcp.tool()` registration. Tools
   continue returning their current shapes — the decorator just
   adds the log line.

**Compat:** structured logs start emitting; tool return shapes
unchanged. Tests should pass.

### Step B — migrate `_classify_lf_error` to emit `ToolError`
1. Update `_classify_lf_error` to produce `ToolError` instances
   instead of `dict[str, Any]`.
2. Extract `upstream_trace_id` from `LaserficheError.detail`.
3. Wire `request_id` from the ContextVar.
4. Map every current slug → `(kind, subkind)` per 2b's table.

**Compat:** `ToolError` serializes to a dict with the same top-level
shape (`{mode: "error", operation, error_kind, error_subkind, message,
suggested_action, request_id, upstream_trace_id, ...extras}`).
Agents that branched on `error` (the current slug field) need
migration; the v1 → v2 shim should normalize.

### Step C — migrate pre-server guard error returns
1. Update `_check_write_permission`, `_check_write_for_entry`,
   `_check_write_for_parent`, `_validate_required_fields`,
   `_invalid_token_response`, the batch-cap check, the
   audit-reason check, and `page_range_required` to return
   `ToolError` instances.

### Step D — wire redaction into existing log lines
1. Replace `request.url` in the two retry warnings
   (`client.py:159-170`) with `redact(str(request.url), ...)`.
2. Replace `self._token_url` in `auth.py:81, 139` with redacted
   variants.

### Step E — `--diagnose` reports observability state
1. Extend `--diagnose` output with: `LF_LOG_FORMAT` setting,
   redaction-helper availability, per-tool-logging decorator wired
   (Y/N).

### Step F — tests + docs
1. `tests/test_errors.py` — `ToolError` shape, subkind taxonomy
   coverage.
2. `tests/test_observability.py` — `redact()` cases, `tool_logger`
   decorator emits one event per call, on success and on error.
3. `docs/error-contract.md` updated to document the new `kind`/`subkind`
   shape, the 5 canonical kinds, the 24-subkind closed set, and the
   `suggested_action` contract.
4. `README.md` "Errors" section updated.

---

## 2g. Risks and open questions

### Risk 1 — FastMCP serialization of union return types

`-> dict[str, Any] | ToolError`. FastMCP's runtime validation handled
typed pydantic returns poorly in v1.4 (the Bug 8 we fixed). Need to
verify that returning a `ToolError` from a tool annotated `-> dict |
ToolError` serializes cleanly.

**Resolution:** verify in Step A. Test: register a stub tool that
returns a `ToolError`; call it via MCP; assert the wire payload has
the expected shape. If FastMCP rejects union returns, fall back to
returning `ToolError.model_dump()` and keep the annotation as `->
dict[str, Any]`.

### Risk 2 — ContextVar propagation across async hops

`request_id` lives in a `ContextVar`. `asyncio` propagates
ContextVars across `await` boundaries by default, but some libraries
(notably httpx in some configurations) may not. If a tool's `await
client.something()` doesn't propagate, the request_id won't be
available when an error fires deep in the client.

**Resolution:** test explicitly with the v1 test repository under
adversarial network conditions (mock 500 from middleware).

### Risk 3 — `retry_after_seconds` source

The LF server may or may not include a `Retry-After` header on 429
responses. The v1 builds we've tested don't always.

**Resolution:** if `Retry-After` is present, parse it. If not, set
`retry_after_seconds=None` and rely on the agent's general
back-off strategy. Document the gap in the `rate_limited`
description.

### Risk 4 — `suggested_action` referring to a tool that doesn't exist
in a future build

If we deprecate a tool the suggestion mentions, the suggestion is now
broken. E.g., suggesting `laserfiche_field_definition_list` when
that tool is later removed.

**Resolution:** the v2.0 surface is the source of truth. Any tool
mentioned in a suggested_action MUST exist in v2.0. Pin all
suggested_action references in a single helper
(`_suggested_action(kind, subkind)`) so we can audit them in one
place. Add a test that grep's the codebase for all
suggested_action strings and verifies each tool name they mention is
in the registered tool list.

### Open question 1 — `request_id` on success responses?

Should every success response include `request_id` so an agent can
report "this worked but produced unexpected output, please
investigate"? Current decision (above): NO, to reduce noise. But if
operators find themselves frequently asking "which tool call
produced this output?" we should add it.

**Resolution:** ship without; revisit in v2.1 based on operator
feedback.

### Open question 2 — Should `tool_logger` log on tool entry too?

Currently emits one event per call (at completion). For long-running
tools (`laserfiche_entry_copy` polling, `laserfiche_task_wait`),
operators might want a "started" event.

**Resolution:** v2.0 ships completion-only. Add `started` events in
v2.1 if needed; structured JSON is forward-compatible.

---

## 2h. Test strategy

### `kind`/`subkind` coverage (unit, mocked httpx)

For every `(kind, subkind)` pair in 2b's mapping table:
1. Set up the upstream-failure scenario via `pytest-httpx`.
2. Call the appropriate tool.
3. Assert the returned `ToolError` has the expected `kind` and
   `subkind`.
4. Assert `suggested_action` is the expected string (verified
   character-for-character against the `_suggested_action` helper).
5. Assert `request_id` is a valid UUID4.

Result: 24 tests, one per subkind.

### Redaction tests (unit)

In `tests/test_observability.py`:
- Each `_REDACT_KEYS` member triggers redaction.
- Case-insensitive matching.
- Nested dict/list recursion.
- URL substring rewriting for repo host and repo id.
- Non-sensitive args pass through.
- Pydantic SecretStr values render as `<redacted>` (not
  `SecretStr('***')`).

### Decorator tests (unit)

In `tests/test_observability.py`:
- Wraps an async function; emits one log event on success.
- Emits one log event on `ToolError` return (outcome=error).
- Emits ERROR-level event on uncaught exception.
- `request_id` is unique per call.
- `request_id` propagates to nested code via ContextVar.

### Integration tests (`LF_INTEGRATION_TEST=1`)

Extend `tests/test_integration.py`:
- **Trace ID extraction:** call `laserfiche_entry_get(999999999)` (a
  nonexistent ID); assert the returned `ToolError.upstream_trace_id`
  is a valid W3C trace ID string from the live server.
- **Permission_denied paths:** trigger `path_not_allowed` and
  `path_traversal_blocked` against a real sandbox.
- **No leakage:** assert the returned `ToolError.message` does NOT
  contain the configured `LF_REPO_API_URL`'s host or `LF_REPOSITORY_ID`.

### Log-line fault injection

Smoke test in `tests/test_observability.py`:
1. Configure `LF_LOG_FORMAT=json`.
2. Capture stderr.
3. Call a stub tool that succeeds.
4. Parse the captured line as JSON.
5. Assert all required schema fields per 2c are present.
6. Run again with a tool that returns a `ToolError`; assert
   `outcome=error`, `error_kind`, `error_subkind` are set.

### Suggested-action validator

In `tests/test_errors.py`:
1. Walk every `(kind, subkind)` in the helper.
2. Get its `suggested_action`.
3. Extract tool names from the suggestion text (regex
   `laserfiche_\w+`).
4. Assert each tool name is in the v2 tool registry.

Prevents suggestion-action references from going stale.

---

## End of PLAN_ERRORS.md

Companions:
- `PLAN.md` — surface composition, collapses, splits.
- `PLAN_DESIGN.md` — per-tool full specs.
