# laserfiche-mcp

[![PyPI version](https://img.shields.io/pypi/v/laserfiche-mcp.svg)](https://pypi.org/project/laserfiche-mcp/)
[![Python versions](https://img.shields.io/pypi/pyversions/laserfiche-mcp.svg)](https://pypi.org/project/laserfiche-mcp/)
[![CI](https://github.com/SamuelSHernandez/laserfiche-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/SamuelSHernandez/laserfiche-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-Model%20Context%20Protocol-1f6feb.svg)](https://modelcontextprotocol.io)

> **Community project — not affiliated with or endorsed by Laserfiche.**

A [Model Context Protocol](https://modelcontextprotocol.io) server that lets
Claude (Desktop, Code, or any MCP client) search and read documents in a
[Laserfiche](https://www.laserfiche.com) repository.

> **Current release: v2.0.0** — read AND write tools for self-hosted
> Repository API v1 and v2, reshaped per a three-pass architectural
> audit. Every tool is now registered under a `laserfiche_{resource}_{verb}`
> name (e.g. `laserfiche_entry_get`, `laserfiche_field_set`); the
> original verb-first names (`get_entry`, `set_fields`, ...) remain
> registered as deprecation aliases through v2.x and will be removed in
> v3.0. Error responses gain top-level `kind` (one of five canonical
> `ToolErrorKind` values), `request_id`, and `upstream_trace_id`
> fields. Defense-in-depth additions: entry-name validation, page-range
> validation, path-traversal rejection, cached client-side pre-flight of
> field/tag/template/link-type names (`LF_VALIDATE_NAMES`), plus a new
> atomic `get_template_fields` lookup and `summary_only` on the
> definition-list tools. Write tools still gate behind `LF_READ_ONLY=false`
> with path-prefix fences, batch caps for folder deletes, two-step
> confirmation tokens, and a tool-level allowlist. See
> [CHANGELOG](CHANGELOG.md) for the full per-release notes. Cloud
> (JWT-signed `client_credentials`) is still on the roadmap.

## What you can do with it

Once connected, Claude can:

**Read** (always available):

- Search the repository with native Laserfiche search syntax, by name pattern, or via the LLM-friendly `search_natural` flow (asks the server for templates first, then runs with automatic 400 repair)
- List the contents of any folder, look up an entry by ID or path, read all template field values, list field/tag/template/link definitions and audit reasons
- Inspect document metadata, fetch the raw edoc as base64, or extract text server-side (PDF via pypdf) — all via `get_document_edoc(..., mode=...)`

**Write** (opt-in via `LF_READ_ONLY=false`):

- Create folders, import documents, copy entries (async), rename and move entries
- Set, merge, and clear fields, tags, and links on an entry
- Assign and remove templates — with optional client-side validation of repository-required fields before the API call
- Delete entries (folders cascade), edocs, and specific page ranges — all with a two-step preview→confirm-token flow, HMAC-signed and bound to operation + entry, expiring after 5 minutes

**Operate safely** — every write checks the entry's path against
`LF_WRITE_PATHS_ALLOW` / `LF_WRITE_PATHS_DENY`, folder deletes refuse
unless `force_large_delete=true` when child count exceeds
`LF_DELETE_FOLDER_MAX_DESCENDANTS`, and `LF_WRITE_TOOLS_ALLOWED` can
scope a deployment to e.g. metadata-only writes.

## Requirements

- A reachable Laserfiche **Repository API Server** (self-hosted) and a service account that can read it
- Python 3.10+ (the install path below uses [`uv`](https://docs.astral.sh/uv/) so you don't have to think about this)
- An MCP-capable client (Claude Desktop, Claude Code, MCP Inspector, etc.)

## Install

Pick whichever fits your workflow:

```bash
# Run directly without cloning
uvx laserfiche-mcp

# Or clone for development
git clone https://github.com/SamuelSHernandez/laserfiche-mcp
cd laserfiche-mcp
uv sync --extra dev
```

## Configure

Copy the example file and fill in your repository details:

```bash
cp .env.example .env
$EDITOR .env
```

Minimum required variables for self-hosted password-grant auth:

| Variable             | Example                                       |
| -------------------- | --------------------------------------------- |
| `LF_REPO_API_URL`    | `https://lf.example.com/LFRepositoryAPI`      |
| `LF_REPOSITORY_ID`   | `my-repo`                                     |
| `LF_API_VERSION`     | `v1` (default) or `v2` — see below            |
| `LF_USERNAME`        | `service-account`                             |
| `LF_PASSWORD`        | (your service account password)               |
| `LF_AUTH_MODE`       | `password`                                    |
| `LF_READ_ONLY`       | `true` (default — see Writes section below)   |

**Optional write-mode variables** (all default off; see the [Safety model](#safety-model) section for context):

| Variable                            | Default | Purpose                                                                          |
| ----------------------------------- | ------- | -------------------------------------------------------------------------------- |
| `LF_READ_ONLY`                      | `true`  | Set `false` to register the write tools                                          |
| `LF_WRITE_PATHS_ALLOW`              | unset   | Comma-separated path prefixes where writes are permitted (case-insensitive)      |
| `LF_WRITE_PATHS_DENY`               | unset   | Comma-separated path prefixes where writes are refused (deny wins over allow)    |
| `LF_WRITE_TOOLS_ALLOWED`            | unset   | Comma-separated write-tool names to scope what registers; e.g. metadata-only     |
| `LF_DELETE_FOLDER_MAX_DESCENDANTS`  | `50`    | Refuse folder deletes above this immediate-child count unless `force_large_delete=true` |
| `LF_REQUIRE_AUDIT_REASON`           | `false` | When `true`, `delete_entry` refuses to execute without `audit_reason_id`         |
| `LF_VALIDATE_REQUIRED_FIELDS`       | `true`  | Validate repo-wide required fields client-side before `assign_template` PUTs     |
| `LF_VALIDATE_NAMES`                 | `true`  | Pre-flight field / tag / template / link-type names against cached schema definitions; returns `invalid_*_name` instead of an opaque 400 |
| `LF_SCHEMA_CACHE_TTL_SECONDS`       | `300`   | Cache window for the schema-definition lookups that back `LF_VALIDATE_NAMES` and `LF_VALIDATE_REQUIRED_FIELDS`. Set to `0` to disable caching. |
| `LF_IMPORT_MAX_BYTES`               | `25 MB` | Client-side cap on `import_document` payload size                                |
| `LF_EDOC_MAX_BYTES`                 | `25 MB` | Cap on `get_document_edoc` downloads in `bytes`/`text` modes                     |

See [`.env.example`](.env.example) for the full list including OAuth
config, pagination limits, request timeout, retry attempts, and SSL
verification.

> **API version note:** LFRepositoryAPI ships with different routing
> surfaces across builds. Older self-hosted installs expose `/v1/...`
> paths; newer ones expose `/v2/...`. Probe your server with:
>
> ```
> curl {LF_REPO_API_URL}/v1/Repositories
> curl {LF_REPO_API_URL}/v2/Repositories
> ```
>
> Whichever returns a `200` with a JSON repo list is your version.
> If the wrong value is set, every call fails with
> `400 UnsupportedApiVersion`. The default is `v1` because that is what
> most current on-prem installations expose.

> **Auth note:** Laserfiche self-hosted does not accept HTTP Basic auth.
> The server exchanges your username/password for a bearer token at
> `POST /{api_version}/Repositories/{repository_id}/Token` on first
> request and refreshes it automatically before expiry. The same flow
> works on both v1 and v2.

## Connect to Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`
(macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "laserfiche": {
      "command": "uvx",
      "args": ["laserfiche-mcp"],
      "env": {
        "LF_REPO_API_URL": "https://lf.example.com/LFRepositoryAPI",
        "LF_REPOSITORY_ID": "my-repo",
        "LF_API_VERSION": "v1",
        "LF_USERNAME": "service-account",
        "LF_PASSWORD": "replace-me",
        "LF_AUTH_MODE": "password",
        "LF_READ_ONLY": "true"
      }
    }
  }
}
```

Restart Claude Desktop. The Laserfiche tools will appear in the tool picker.

## Connect to Claude Code

```bash
claude mcp add laserfiche -- uvx laserfiche-mcp
```

(Pass env vars via `--env LF_REPO_API_URL=...` flags or set them in your
shell before running Claude Code.)

## Test it locally with the MCP Inspector

```bash
npx @modelcontextprotocol/inspector uvx laserfiche-mcp
```

This opens a UI where you can call each tool directly and watch the
JSON-RPC traffic — useful for verifying endpoint shapes against your
specific Repository API Server version before wiring it into Claude.

## Tools

> Tool names below are shown in their original verb-first form
> (`get_entry`, `set_fields`, ...) for readability. In v2.0 every tool is
> *also* registered under the `laserfiche_{resource}_{verb}` form
> (`laserfiche_entry_get`, `laserfiche_field_set`, ...). Both names
> resolve to the same function. The `laserfiche_*` names are the
> recommended path; the old names remain as deprecation aliases through
> v2.x and will be removed in v3.0. The authoritative mapping lives in
> `_V2_RENAME_MAP` in [`src/laserfiche_mcp/server.py`](src/laserfiche_mcp/server.py).

### Reads (always registered)

| Tool                         | v2 name                                | Purpose                                                                 |
| ---------------------------- | -------------------------------------- | ----------------------------------------------------------------------- |
| `search_entries`             | `laserfiche_entry_search`              | Run a raw Laserfiche search query, e.g. `{LF:Name="*.pdf"}`             |
| `search_by_name`             | `laserfiche_entry_search_by_name`      | Convenience wrapper: name pattern + optional folder scope               |
| `search_natural`             | `laserfiche_entry_search_natural`      | Two-mode guided search: ask for grammar+templates, then run with auto-repair on 400 |
| `list_folder`                | `laserfiche_folder_list`               | List children of a folder by ID                                          |
| `get_entry`                  | `laserfiche_entry_get`                 | Fetch metadata for one entry by ID                                       |
| `get_entry_by_path`          | `laserfiche_entry_get_by_path`         | Resolve a full path to an entry                                          |
| `get_field_values`           | `laserfiche_field_values_get`          | Read all template fields assigned to an entry                            |
| `get_document_text`          | `laserfiche_document_get_text`         | Server-side extracted text (v2 only; v1 use `get_document_edoc(mode="text")`) |
| `get_document_edoc`          | `laserfiche_document_get_edoc`         | Inspect edoc (`info`), download bytes (`bytes`), or extract text (`text`) |
| `list_repositories`          | `laserfiche_repository_list`           | List repos for this account; falls back to the configured repo if endpoint disabled |
| `list_field_definitions`     | `laserfiche_field_definition_list`     | Enumerate all field definitions; pass `summary_only=true` for a `{count, names}` shape |
| `list_tag_definitions`       | `laserfiche_tag_definition_list`       | Enumerate tag definitions; supports `summary_only`                       |
| `list_template_definitions`  | `laserfiche_template_definition_list`  | Enumerate template definitions; supports `summary_only`                  |
| `list_link_definitions`      | `laserfiche_link_definition_list`      | Enumerate entry-link type definitions; supports `summary_only`           |
| `get_template_fields`        | `laserfiche_template_field_list`       | Atomic "what fields does this template need" lookup; pass `required_only=true` to filter to mandatory fields. Replaces the three-call chain (`list_template_definitions` → `list_field_definitions` → manual filter). |
| `get_audit_reasons`          | `laserfiche_audit_reason_list`         | Audit reasons available to the authenticated user (for delete/export)    |
| `get_task_status`            | `laserfiche_task_get_status`           | Poll the status of an async operation (delete, copy)                     |
| `wait_for_task`              | `laserfiche_task_wait`                 | Block until an async operation reaches a terminal state                  |

### Writes (registered only when `LF_READ_ONLY=false`)

| Tool                | v2 name                              | Purpose                                                                          | Two-step token? |
| ------------------- | ------------------------------------ | -------------------------------------------------------------------------------- | --------------- |
| `set_fields`        | `laserfiche_field_set`               | OVERWRITE all field values on an entry (fields not in the body are deleted)      | —               |
| `merge_fields`      | `laserfiche_field_merge`             | GET-then-PUT helper: update specific fields, preserve the rest                   | —               |
| `set_tags`          | `laserfiche_tag_set`                 | OVERWRITE all tags on an entry                                                   | —               |
| `merge_tags`        | `laserfiche_tag_merge`               | Add/remove specific tags without touching others                                 | —               |
| `set_links`         | `laserfiche_link_set`                | OVERWRITE all entry links                                                        | —               |
| `assign_template`   | `laserfiche_template_assign`         | Assign a template, optionally with initial field values (preflight-validated)    | —               |
| `remove_template`   | `laserfiche_template_remove`         | Clear the template assignment                                                    | —               |
| `create_folder`     | `laserfiche_folder_create`           | Create a child folder under a parent                                             | —               |
| `import_document`   | `laserfiche_document_import`         | Multipart upload from a local file path; capped by `LF_IMPORT_MAX_BYTES`         | —               |
| `copy_entry`        | `laserfiche_entry_copy`              | Async copy via `CopyAsync`; returns an operation token to poll                   | —               |
| `rename_entry`      | `laserfiche_entry_rename`            | Rename an entry — preview shows old/new path, then re-call with the token        | yes             |
| `move_entry`        | `laserfiche_entry_move`              | Move (optionally rename) — fence applies to both source AND destination paths    | yes             |
| `delete_entry`      | `laserfiche_entry_delete`            | Delete an entry (folders cascade); preview shows child count + batch-cap status  | yes             |
| `delete_edoc`       | `laserfiche_document_edoc_delete`    | Wipe the electronic-document content; entry + metadata remain                    | yes             |
| `delete_pages`      | `laserfiche_document_pages_delete`   | Delete specific page ranges; refuses empty `page_range` (would mean "delete all") | yes             |

Tools with **two-step token** return a preview + HMAC-signed
`confirmation_token` on first call. Surface the preview to the user; on
go-ahead, re-call with the same arguments plus the token. Tokens are
bound to `(operation, entry_id, entry_name)`, expire after 5 minutes,
and are invalidated by server restart.

### Using `search_natural`

`search_entries` requires hand-written Laserfiche query syntax. If the
server rejects the query the only feedback the LLM gets is a generic HTTP
400 — there's nothing actionable to retry against. `search_natural` is the
LLM-friendly path:

1. **First call** — pass the user's question and (optionally) a
   `folder_path` to scope the answer; leave `lf_query` unset.
   The tool samples up to ten entries from that folder, returns
   the templates and field names it found, the Laserfiche search grammar
   reference, and 2–3 candidate query strings the LLM can choose from or
   refine.
2. **Second call** — same `question`, plus the chosen `lf_query`.
   On HTTP 400, the tool tries up to two automatic repairs (escape
   unescaped quotes inside values, then wildcard-wrap bare `Name=`
   values if `fuzzy=True`) before returning a structured error with all
   attempts visible so the LLM can author a fresh query.

The page-size cap for `search_natural` is the dedicated `LF_MAX_PAGE_SIZE`
env var (default 100) — some self-hosted SimpleSearches implementations
reject `$top` values above an internal limit, so this defaults lower than
the list/folder cap.

### `get_document_edoc` modes

On v1 servers the Laserfiche `Text` export endpoint doesn't exist, so
`get_document_text` cannot return anything. `get_document_edoc` gained a
`mode` parameter as the workaround:

| Mode      | Use it when                                                |
| --------- | ---------------------------------------------------------- |
| `info`    | You only need metadata (size, content-type). Default.      |
| `bytes`   | You want the raw file as base64 — capped at `LF_EDOC_MAX_BYTES` (25 MB by default; override per-call with `max_bytes`). |
| `text`    | You want extracted text. PDFs go through `pypdf` server-side; `text/*` is decoded directly; anything else returns a structured "use mode=bytes" error. OCR is not attempted. |

All tool descriptions are written to read like prompts — they tell the
model when to use the tool, valid input shapes, and what kind of follow-up
is expected. See [`src/laserfiche_mcp/server.py`](src/laserfiche_mcp/server.py).

## Errors

Every tool returns a stable dict on failure instead of raising — so the
LLM gets actionable, structured data instead of `Error executing tool ...`.

```json
{
  "mode": "error",
  "operation": "laserfiche_entry_delete",
  "kind": "not_found",
  "error": "not_found",
  "status_code": 404,
  "server_error_code": null,
  "server_message": null,
  "reason": "Server returned 404 — the entry, path, or endpoint does not exist.",
  "request_id": "9f2c…",
  "upstream_trace_id": null,
  "entry_id": 999
}
```

`kind` is one of five canonical `ToolErrorKind` values — LLMs branch on
this for category-level decisions (retry vs ask user vs abort):

| Kind                    | Meaning                                                                                  |
| ----------------------- | ---------------------------------------------------------------------------------------- |
| `not_found`             | The named entry, path, or endpoint doesn't exist. Verify with the user.                  |
| `permission_denied`     | Credentials, ACLs, or local fence config refused the operation.                          |
| `rate_limited`          | The server told the caller to slow down. Back off and retry.                             |
| `invalid_input`         | The request is malformed or fails a local pre-flight. Fix and re-call.                   |
| `upstream_unavailable`  | LF returned 5xx, 405, or an opaque failure. Retry once, then surface.                    |

`error` is the more-specific subkind. Server-mapped subkinds:

| Subkind                   | Triggers                                                                     |
| ------------------------- | ---------------------------------------------------------------------------- |
| `auth_failed`             | HTTP 401/403, LF errorCode 9010, or LF 9528 ("LFDS unreachable" — usually creds too) |
| `required_field_missing`  | LF errorCode 9039/9066                                                       |
| `not_found`               | HTTP 404                                                                     |
| `method_not_allowed`      | HTTP 405 — usually an MCP routing bug                                        |
| `unsupported_media_type`  | HTTP 415 — usually a wire-format bug (missing `Content-Type`)                |
| `rate_limited`            | HTTP 429                                                                     |
| `server_error`            | HTTP 5xx or unrecognized failure                                             |

Tools also have pre-server `mode: error` shapes (`path_not_allowed`,
`path_traversal_blocked`, `exceeds_batch_cap`,
`invalid_confirmation_token`, `missing_required_fields`,
`page_range_required`, `invalid_page_range`, `invalid_name`,
`invalid_field_name`, `invalid_tag_name`, `invalid_template_name`,
`invalid_link_type`, `file_not_found`, `size_exceeds_cap`,
`tool_not_allowed`). `list_repositories` returns `mode: fallback`
instead of erroring when the server doesn't expose the endpoint — see
the docstring for the response shape.

See [`docs/error-contract.md`](docs/error-contract.md) for the full
taxonomy, per-tool triggers, and the kind ↔ subkind mapping.

## Safety model

Writes are off by default. When you enable them (`LF_READ_ONLY=false`),
the following guards are available — all independent, all opt-in
except as noted:

- **Path-prefix fences** (`LF_WRITE_PATHS_ALLOW`, `LF_WRITE_PATHS_DENY`) — every write checks the entry's `fullPath` (or the parent's for creates) against the configured prefixes. Case-insensitive, deny wins over allow, both `\` and `/` accepted. `move_entry` fences on BOTH source and destination paths so a token from an allowed source can't be replayed to land in a denied folder. Strongest single fence — recommended for any non-trivial deployment.
- **Tool-level allowlist** (`LF_WRITE_TOOLS_ALLOWED`) — restrict which write tools register at all. Example: `merge_fields,merge_tags,assign_template` for a metadata-only deployment that can't create or delete anything.
- **Folder-delete batch cap** (`LF_DELETE_FOLDER_MAX_DESCENDANTS`, default 50) — `delete_entry` on a folder with more immediate children refuses unless `force_large_delete=true` is passed alongside the confirmation token. The preview surfaces `exceeds_batch_cap: true` so the LLM can explain the size before re-calling.
- **Audit-reason requirement** (`LF_REQUIRE_AUDIT_REASON`, default false) — when true, `delete_entry` refuses without an `audit_reason_id`. Use `get_audit_reasons` to enumerate valid IDs.
- **Required-field validation** (`LF_VALIDATE_REQUIRED_FIELDS`, default **true**) — `assign_template` lists `FieldDefinitions`, finds `isRequired: true` fields, checks them against what's on the entry and what's in the caller's `fields=`, and returns a structured `missing_required_fields` error before the PUT — instead of the server's opaque `Multistatus response. [9039]`.
- **Two-step confirmation tokens** (always on for destructive ops) — `rename_entry`, `move_entry`, `delete_entry`, `delete_edoc`, `delete_pages` return a preview + HMAC-signed token on first call; execute on second call. Tokens bind to `(operation, entry_id, entry_name)`, expire after 5 minutes, invalidate on server restart.

### Recommended starting config for write mode

```jsonc
"env": {
  "LF_READ_ONLY": "false",
  "LF_WRITE_PATHS_ALLOW": "\\Sandbox\\mcp-test",        // scope to a sandbox first
  "LF_WRITE_TOOLS_ALLOWED": "create_folder,import_document,merge_fields,merge_tags,assign_template,delete_entry",
  "LF_DELETE_FOLDER_MAX_DESCENDANTS": "10",
  "LF_REQUIRE_AUDIT_REASON": "false"                    // turn on once you have a workflow
}
```

Pre-create the sandbox folder by hand in the Laserfiche web client; the
fence needs an existing parent to read its `fullPath`. Once
smoke-tested, broaden the tool list — path scope is still the strongest
fence regardless of which tools are registered.

## Roadmap

- **v2.x follow-ups** (deferred from the v2.0 audit) — write-tool
  collapses (`field_update(mode)`, `tag_update(add, remove)`,
  `link_update(mode)`), preview/execute splits of the 5 destructive
  tools, parameter-description polish for the JSON schema the LLM sees,
  structured JSON logging (`LF_LOG_FORMAT=json`) with a `redact()`
  helper. Working notes in [`docs/internal/TODO.md`](docs/internal/TODO.md).
- **Server-side audit logging** — sidecar file with rotation, capturing
  every write tool call with the authenticated user, target entry, and
  outcome.
- **Cloud** — Laserfiche Cloud support (`signin.laserfiche.com`
  JWT-signed `client_credentials` flow plus the `api.laserfiche.com`
  v2-only endpoint surface).
- **v3.0** — Remove the verb-first deprecation aliases (`get_entry`,
  `set_fields`, ...). Only the `laserfiche_{resource}_{verb}` names
  remain.
- **Beyond** — Workflow trigger tools, async `/Searches` flow for large
  result sets, server-side text extraction for Office documents.

## Development

```bash
uv sync --extra dev
uv run pytest                  # mocked HTTP, enforces 80% coverage baseline
uv run ruff check src tests
uv run mypy src
```

Tests use `pytest-httpx` to mock the Repository API and committed
fixture PDFs to exercise the text-extraction paths — they don't require a
real Laserfiche server.

### Opt-in integration tests

```bash
LF_INTEGRATION_TEST=1 uv run pytest tests/test_integration.py
```

Reads the same `LF_*` env vars the server uses at runtime. Optional
overrides:

- `LF_INTEGRATION_FOLDER_PATH` — folder used in the `search_natural` Mode A
  test (defaults to repository root)
- `LF_INTEGRATION_PDF_ENTRY_ID` — known PDF entry; if unset, edoc tests skip
- `LF_INTEGRATION_SAFE_QUERY` — a query expected to return results on your
  repo (defaults to `{LF:Name="*"}`)

Use this before tagging a release if you have a reachable repository — it
catches issues that mocked HTTP can't surface (server-side query syntax
quirks, real PDF extraction, transport-level rejections).

## Contributing

Issues and PRs welcome — particularly:

- Endpoint corrections for Repository API Server builds the v1 / v2 wire format hasn't been validated against
- Laserfiche Cloud client + JWT-signed `client_credentials` assertion flow
- Server-side audit logging for write-mode deployments (sidecar file + rotation)
- Structured JSON logging + per-tool-call redaction (`LF_LOG_FORMAT=json`)
- Async `/Searches` flow for very large result sets

This is a community project, **not** affiliated with or endorsed by
Laserfiche.

## License

Released under the [MIT License](LICENSE). Copyright (c) 2026 Samuel S. Hernandez.
