# AUDIT_ERRORS.md — laserfiche-mcp Pass 2: Error Handling & Observability

Read-only audit of the v1.5 codebase against the principle that
**error handling is prompt engineering** — agents reason better over
typed return shapes with named kinds and suggested actions than over
exception strings.

Source of truth for `PLAN_ERRORS.md`. Every claim cites `file:line`.

> **Status note (post-v2.0 sanitization pass):** the logging-leak
> findings in section 1d below have been remediated at minimum
> severity (auth.py token-URL emission removed; client retry warnings
> log `request.url.path` only — host stripped). The structured
> redaction helper / per-tool-call `request_id` work described in
> section 1f is still deferred to a v2.x follow-up. File:line
> references throughout this document refer to the **v1.5** codebase
> the audit was performed against; lines have since shifted.

---

## 1a. Current error surface

| Tool | What reaches the agent today | Leaks internals? |
|---|---|---|
| All tools (server errors) | `_classify_lf_error()` returns `{mode:"error", operation, error:<slug>, status_code, server_error_code, server_message, reason, ...extras}` (`server.py:1276-1359`) | **N** — `_lf_error_detail` extracts only `errorCode`, `title`, `message` from ProblemDetails (`server.py:1294-1296`); raw URLs/hostnames/credentials never reach the agent through this path. |
| All write tools (pre-server guards) | Structured `{mode:"error", error:<slug>, ...}` for `path_not_allowed`, `exceeds_batch_cap`, `audit_reason_required`, `invalid_confirmation_token`, `missing_required_fields`, `tool_not_allowed`, `page_range_required` | **N** — these are local guards; data is structured by construction. |
| `list_repositories` | On endpoint failure, returns `{mode:"fallback", operation, warning, server_error:<classified>, value:[configured repo]}` (`server.py:946-957`) | **N** — error dict is nested under `server_error` key. |
| All tools (pydantic-level config errors) | Validation errors from `Settings()` at startup are formatted in `_format_config_error()` and printed to stderr (`server.py:2935-2966`); never reach the agent at runtime. | **N** — startup-only path. |
| `_require_writes_enabled()` | Still raises `RuntimeError` when a write tool is invoked while `LF_READ_ONLY=true` (`server.py:937-948`). Documented as intentional — this is a config error, not a server error. | **Borderline** — the exception propagates to FastMCP and surfaces as "Error executing tool ..." to the agent, which is exactly the bad UX the rest of v1.4–v1.5 fixed. |

**Verdict:** Response-surface leakage is **clean**. Every tool returns a
structured dict on failure with no exception escaping (except the
`_require_writes_enabled` config-error path, which is a known gap).

---

## 1b. Error taxonomy gaps vs the canonical 5 kinds

Pass 2's canonical kinds: `not_found`, `permission_denied`,
`rate_limited`, `invalid_input`, `upstream_unavailable`.

Current taxonomy: 14 slugs total — 7 server-classified and 7
pre-server guards.

### Mapping

| Current slug | Source | file:line | Canonical kind |
|---|---|---|---|
| `auth_failed` | 401, 403, LF 9010, LF 9528 | `server.py:1301-1315` | `permission_denied` |
| `required_field_missing` | LF 9039, LF 9066 | `server.py:1308-1311` | `invalid_input` |
| `not_found` | HTTP 404 | `server.py:1321-1322` | `not_found` ✓ |
| `method_not_allowed` | HTTP 405 | `server.py:1324-1330` | `upstream_unavailable` (build config) |
| `unsupported_media_type` | HTTP 415 | `server.py:1332-1338` | `invalid_input` (MCP wire bug) |
| `rate_limited` | HTTP 429 | `server.py:1340-1342` | `rate_limited` ✓ |
| `server_error` | HTTP 5xx + fallback | `server.py:1344` | `upstream_unavailable` ✓ |
| `path_not_allowed` | `permissions.py:path_allowed` | `server.py:1392-1425` | `permission_denied` |
| `exceeds_batch_cap` | folder-delete probe | `server.py:2570-2602` | `invalid_input` |
| `audit_reason_required` | `LF_REQUIRE_AUDIT_REASON=true` | `server.py:2603-2615` | `invalid_input` |
| `invalid_confirmation_token` | HMAC verify failure | `server.py:1369-1374` | `invalid_input` |
| `missing_required_fields` | `_validate_required_fields` | `server.py:1755-1810` | `invalid_input` |
| `tool_not_allowed` | `LF_WRITE_TOOLS_ALLOWED` filter | (registration-time gate; runtime check at server.py around 2858) | `permission_denied` |
| `page_range_required` | client-side empty-range refusal | `server.py:2780-2792` | `invalid_input` |

### Gap analysis

- **The 14 slugs DO map cleanly to the 5 canonical kinds.** Eight of
  the 14 collapse into `invalid_input`. Three into `permission_denied`.
  Two into `upstream_unavailable`. The remaining two (`not_found`,
  `rate_limited`) are canonical kinds themselves.
- **No tools are missing structured error paths.** Every tool either
  returns a `{mode:"error", ...}` dict via `_classify_lf_error` or
  raises only `RuntimeError` from the `_require_writes_enabled` guard.
- **The slugs carry actionable signal that the canonical kinds erase.**
  `exceeds_batch_cap` tells the agent "pass `force_large_delete=true`
  on the next call." Collapsing it into generic `invalid_input` loses
  that. Resolution: per user's approved decision, surface BOTH —
  `kind` (canonical 5) and `subkind` (current slugs).

### Slugs that aren't subkinds — they're operations

`audit_reason_required` and `missing_required_fields` are operations
that produce extra payload fields (`audit_reason_required` returns the
list of valid reasons; `missing_required_fields` returns the field
metadata for each missing one). Keep these as subkinds AND keep their
extra-payload contracts intact.

---

## 1c. Suggested-action audit

For every error response that suggests a follow-up, does it (a) name
a real tool, (b) refer to an action the agent can take from the tool
surface alone, (c) actually unblock the next turn?

| Source | Slug | Suggestion text (paraphrased) | Real tool? | Actionable? |
|---|---|---|---|---|
| `server.py:1308-1311` | `required_field_missing` | "Call list_field_definitions and supply isRequired=true fields via the tool's `fields` parameter." | ✓ `list_field_definitions` exists | ✓ |
| `server.py:1314-1320` | `auth_failed` | "Confirm the service account has rights on the target path." | ✗ no tool; config/human action | ⚠ informational only — appropriate |
| `server.py:1324-1330` | `method_not_allowed` | "Usually an MCP routing bug or a build that doesn't expose the endpoint." | ✗ no tool | ⚠ informational — the agent can't fix this |
| `server.py:1332-1338` | `unsupported_media_type` | "Usually a wire-format bug in the MCP." | ✗ no tool | ⚠ informational |
| `server.py:1340-1342` | `rate_limited` | "Slow down and retry after a delay." | ✗ no tool, but agent can comply | ✓ retryable signal |
| `server.py:1372-1374` (in `_invalid_token_response`) | `invalid_confirmation_token` | "Re-run the same tool without confirmation_token to get a fresh preview and a new token." | ✓ same tool | ✓ |
| `server.py:2453-2458` | `exceeds_batch_cap` | "Pass `force_large_delete=true` alongside the confirmation token on the execute leg." | ✓ same tool with extra param | ✓ |
| `server.py:2607-2614` | `audit_reason_required` | "Use `get_audit_reasons` to enumerate valid IDs, then re-call with `audit_reason_id`." | ✓ `get_audit_reasons` exists | ✓ |
| `_validate_required_fields` response (`server.py:1789-1808`) | `missing_required_fields` | "Call assign_template again with `fields=` including each of these names. Disable this check with LF_VALIDATE_REQUIRED_FIELDS=false." | ✓ same tool | ✓ |

**Verdict:** All five tool-referencing suggestions name real tools and
are actionable. The four informational suggestions
(`auth_failed`, `method_not_allowed`, `unsupported_media_type`,
`rate_limited`) appropriately point at non-agent-actionable causes —
this is correct (an agent can't fix a credential or a wire-format
bug; it can comply with rate limiting).

### Gap to close

No structured `suggested_action` field exists today — the suggestion
text is folded into `reason`. PLAN_ERRORS will extract it into a
dedicated `suggested_action: str | None` field on `ToolError`.

---

## 1d. Leakage review (logging path, NOT response path)

Response-path leakage is clean (see 1a). Logging-path leakage is the
real audit finding.

### Confirmed leakage (REMEDIATED — see Status note at top)

| file:line (v1.5) | Log line | Leaks | Status |
|---|---|---|---|
| `auth.py:81` | `logger.debug("Exchanging password for bearer token at %s", self._token_url)` | Token-exchange URL including hostname AND repository ID. DEBUG level. | ✅ FIXED: URL removed from log message in `auth.py` (post-2.0 sanitization). |
| `auth.py:139` | `logger.debug("Refreshing OAuth access token from %s", self._token_url)` | OAuth token URL. DEBUG level. | ✅ FIXED: URL removed. |
| `client.py:159-161` | `logger.warning("Network error on %s %s (attempt %d/%d): %s; retrying in %ds", request.method, request.url, ...)` | **Full request URL** including hostname, repo ID, entry IDs in path. WARNING level — surfaces in default logging config. | ✅ FIXED: now logs `request.url.path` (host stripped). Endpoint path retained for diagnosability. |
| `client.py:168-170` | `logger.warning("Retryable status %d on %s %s (attempt %d/%d); retrying in %ds", response.status_code, request.method, request.url, ...)` | Same as above. WARNING. | ✅ FIXED: same treatment. |
| `server.py:2973-2977` | `print(f"Target: {settings.repo_api_url}{settings.repository_id} (API {settings.api_version.value})")` then auth info | Repo URL, repo ID, auth mode, username. **stdout via print()**, not logger. Only on `--diagnose` flag — explicit operator action. | ➖ Intentional. Operator-invoked diagnostic output. |

### Not leaking

- `Authorization` header is set by `_auth.apply(request)` (`client.py:152`)
  immediately before send; never logged.
- `Settings.password` is a `SecretStr` (`config.py:79-87`); only
  `.get_secret_value()` reaches the wire (`auth.py:91`).
- Error responses to the agent never include hostnames; only the
  upstream `title`/`message` ProblemDetails text.

### Severity (pre-remediation)

**Medium.** Repository IDs and full request URLs at WARNING level
got logged on every retry attempt. For a self-hosted Laserfiche
deployment where the hostname and repo ID are part of the customer's
internal infrastructure inventory, those values leaking into shared
log aggregators (Splunk, Datadog, etc.) was a real concern. Not a
secret leak (no credentials in any log line), but a deployment-context
leak.

**Post-remediation:** the host is no longer emitted by any logger
call in the codebase. The path component (which includes the repo ID
segment) is still emitted at WARNING on retries — that's the
minimum information needed to diagnose which endpoint was retried.
Operators who want stricter redaction can lower the log level or
filter on logger name `laserfiche_mcp.client`. Full URL redaction
via a central `redact()` helper plus per-tool-call `request_id` is
still a v2.x follow-up.

---

## 1e. Logging inventory

### Every `logger.*` call in the codebase

| file:line | Level | Format style | What's logged |
|---|---|---|---|
| `auth.py:81` | DEBUG | structured-arg (`%s`) | Token-exchange URL |
| `auth.py:139` | DEBUG | structured-arg | OAuth token URL |
| `client.py:159-161` | WARNING | structured-arg | Network error + retry |
| `client.py:168-170` | WARNING | structured-arg | Retryable status + retry |
| `client.py:172` (logger setup) | — | — | Logger name = `laserfiche_mcp.client` |
| `server.py:46` | — | — | Logger name = `laserfiche_mcp` (root) |
| `server.py:3079-3081` | INFO | structured-arg | Startup banner with `read_only` flag and write-tool count |
| `server.py:3087` | INFO | f-string | `"laserfiche-mcp stopped."` (no data) |

### Non-`logger` output

| file:line | Channel | Format |
|---|---|---|
| `server.py:2941-2956` | stderr via `print()` | Config-error friendly message (only at startup) |
| `server.py:2960-2966` | stderr via `print()` | NotImplementedError friendly message (startup only) |
| `server.py:2973-3034` | stdout via `print()` | `--diagnose` output (deployment-fitness report) |

### Findings

1. **No per-tool-call logging.** Not a single tool emits a structured
   event when invoked. `wait_for_task` is the only place that
   captures elapsed time (`server.py:1195-1211`), and even that doesn't
   reach a log line. **This is the main observability gap.**
2. **Format style is consistent** (everywhere uses `%s`/`%d`
   placeholder-arg format — the safe pattern for structured logging
   adapters). No f-strings in actual logger calls (only in stdout
   prints).
3. **No JSON log format.** Default `logging.basicConfig(level=...)` at
   `server.py:3083` produces human-readable lines, not JSON. Operators
   piping to `jq` or aggregating in Datadog need to parse free-form
   strings.
4. **Log level controlled via `LF_LOG_LEVEL`** (`config.py:175-179`)
   and overridable by `--verbose` / `--quiet` CLI flags. Good
   precedent — no change.

### Remediations (carry into PLAN_ERRORS.md)

- Wire a tool-invocation decorator at `_register_write_tools` and the
  `@mcp.tool()` registration sites that emits one JSON event per call:
  `{ts, tool, args (redacted), duration_ms, outcome, request_id, upstream_trace_id, error_kind?, error_subkind?}`.
- Add `LF_LOG_FORMAT: Literal["text","json"]="text"` env var. When
  set to `json`, format every log line as one JSON object per line.
- Redact request URLs in `client.py:159-170` retry warnings —
  replace `request.url` with `request.method + " " + suffix_after_host`.

---

## 1f. Redaction surface

### Current state

- **No single redaction helper exists.** Redaction is implicit and
  relies on:
  - `SecretStr` for passwords / API keys / client_secret (correct).
  - Developer discipline to NEVER pass `password` etc. to `logger.*`
    calls. No automated guard.
- **Where credentials could leak (worst case):**
  - If a future tool wrote `logger.debug("settings=%s", settings)`
    on a settings object with `model_dump()` — pydantic v2
    `SecretStr` redacts in `model_dump()`, so this is actually safe
    BY DESIGN of pydantic, but only if the developer uses
    `model_dump()`. Calling `repr(settings)` or `dict(settings)`
    on a non-pydantic structure could leak.
  - Tool arguments. If a future write tool accepts a credential-like
    parameter (which none currently do, but adopters could fork),
    nothing prevents logging it.
- **Where hostnames could leak (current):**
  - The two retry warnings in `client.py` (see 1d).
  - The diagnose printout (operator-invoked; tolerable).

### Findings

1. **No allow-list / deny-list of redacted fields.** A central
   redaction helper that, given a dict of tool args, returns a
   redacted copy (replacing values for keys matching
   `password|secret|token|api_key|authorization|cookie|x-api-key` with
   `<redacted>`) does not exist.
2. **No automatic redaction of URLs containing the configured
   `LF_REPO_API_URL` or `LF_REPOSITORY_ID`** in log lines.

### Remediation

A single `redact(obj)` helper in a new module (`observability.py`) used
by:
- The new per-tool-call logging decorator (input args).
- The two retry-warning log lines in `client.py`.

Deny-list keys (case-insensitive):
`password, secret, client_secret, api_key, token, authorization,
cookie, x-api-key, lf_password, lf_client_secret, lf_api_key`.

Plus URL-rewriting: replace any hostname matching
`Settings.repo_api_url`'s host with `<repo>`; replace any path segment
matching `Settings.repository_id` with `<repo_id>`.

---

## 1g. Correlation review

### Upstream trace IDs ARE available — we just don't surface them

Laserfiche's ProblemDetails responses include a `traceId` field for
every API error. Confirmed from live-test outputs in the v1.4
testing session, e.g.:

```
{'type': 'invalidRequest', 'title': '...', 'status': 400, ...,
 'traceId': '00-92647f26a3ce0eefba0bb9b5b8b7997c-86f93f712586c4d7-00'}
```

That trace ID is W3C-format and corresponds to the Laserfiche
server's request log. An operator triaging a failed agent call can
take that ID and find the exact upstream request in their server logs.

### Current handling

- `LaserficheError` carries `detail: object` (`client.py:67-83`) which
  contains the full ProblemDetails dict.
- `_lf_error_detail(exc)` (`server.py:1265-1273`) extracts `errorCode`,
  `title`, and `message` but **does NOT extract `traceId`**.
- Result: `traceId` is in memory at the moment of error classification
  but is dropped before the error response reaches the agent.

### No request_id generation

The MCP server doesn't generate a per-call request_id. Operators have
no way to correlate "agent reported this error" with "server log line
at this timestamp."

### Remediation

1. **Generate a per-tool-call `request_id`** (UUID4) at the start of
   every tool invocation. Plumb through the tool-call decorator and
   include in every log line for that call.
2. **Extract `traceId` from `LaserficheError.detail`** in
   `_lf_error_detail` and surface it as `upstream_trace_id` on every
   `ToolError` response.
3. **Correlate** in the log line: both `request_id` (our id) and
   `upstream_trace_id` (Laserfiche's id) so an operator can pivot
   either way.

---

## End of Pass 2 audit

Decisions deferred to `PLAN_ERRORS.md`: full `ToolError` /
`ToolErrorKind` model, the per-tool error-mapping table, the
structured-logging schema, the redaction helper interface and rules,
request_id propagation, migration order.

Sibling audits:
- Pass 1 (workflow & surface) → `AUDIT.md`
- Pass 3 (tool design — naming, schemas, descriptions) → `AUDIT_DESIGN.md`
