<!-- mcp-name: io.github.SamuelSHernandez/laserfiche-mcp -->

# laserfiche-mcp

[![PyPI version](https://img.shields.io/pypi/v/laserfiche-mcp.svg)](https://pypi.org/project/laserfiche-mcp/)
[![Python versions](https://img.shields.io/pypi/pyversions/laserfiche-mcp.svg)](https://pypi.org/project/laserfiche-mcp/)
[![CI](https://github.com/SamuelSHernandez/laserfiche-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/SamuelSHernandez/laserfiche-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-Model%20Context%20Protocol-1f6feb.svg)](https://modelcontextprotocol.io)

> **Community project — not affiliated with or endorsed by Laserfiche.**

A [Model Context Protocol](https://modelcontextprotocol.io) server that lets
Claude (Desktop, Code, or any MCP client) search, read, and — when you opt
in — write documents in a self-hosted
[Laserfiche](https://www.laserfiche.com) repository. The same binary is
also a full command-line client (`ls`, `get`, `cat`, `search`, `manifest`,
`dedupe`, ...) for the deterministic work that needs no model at all.

Current release **v2.3.0** — read and write tools for self-hosted Repository
API v1 and v2, a one-click Claude Desktop extension, an optional remote HTTP
transport with per-user OAuth for web clients, and a full CLI (`ls`, `get`,
`cat`, `search`, `manifest`, `dedupe`, ...) for the deterministic work that
needs no model at all. Read-only by default; write tools register only with
`LF_READ_ONLY=false` and are guarded by path fences and a two-step,
parameter-bound confirmation flow. See the [CHANGELOG](CHANGELOG.md) for
per-release notes and the [roadmap](#roadmap) for what's next.

## Quick start

```bash
uv tool install laserfiche-mcp   # or run ad hoc: uvx laserfiche-mcp
laserfiche-mcp setup             # wizard: server URL + account, then verifies the connection
laserfiche-mcp ls 1              # you're in — list the repository root
```

Then wire it into your MCP client — see
[Claude Desktop](#connect-to-claude-desktop) and
[Claude Code](#connect-to-claude-code) below. Prefer environment variables
over the wizard? See [Configure](#configure). Want a no-terminal install
instead? See the [Claude Desktop extension](#for-everyone--the-claude-desktop-extension).

## What you can do with it

Once connected, Claude can:

**Read** (always available):

- Search the repository with native Laserfiche search syntax, by name pattern, or via the LLM-friendly `search_natural` flow (asks the server for templates first, then runs with automatic 400 repair)
- List the contents of any folder, look up an entry by ID or path, read all template field values, list field/tag/template/link definitions and audit reasons
- Inspect document metadata, fetch the raw edoc as base64, or extract text locally (PDF, DOCX, PPTX, XLSX, EML, HTML, RTF, `text/*`) — all via `get_document_edoc(..., mode=...)`

**Write** (opt-in via `LF_READ_ONLY=false`):

- Create folders, import documents, copy entries (async), rename and move entries
- Set, merge, and clear fields, tags, and links on an entry
- Assign and remove templates — with optional client-side validation of repository-required fields before the API call
- Delete entries (folders cascade), edocs, and specific page ranges — all with a two-step preview→confirm-token flow, HMAC-signed and bound to operation + entry + the operation's own parameters, expiring after 5 minutes

**Operate safely** — every write checks the entry's path against
`LF_WRITE_PATHS_ALLOW` / `LF_WRITE_PATHS_DENY`, folder deletes refuse
unless `force_large_delete=true` when child count exceeds
`LF_DELETE_FOLDER_MAX_DESCENDANTS`, and `LF_WRITE_TOOLS_ALLOWED` can
scope a deployment to e.g. metadata-only writes.

## Install

Two ways to run it, depending on who you are.

### For everyone — the Claude Desktop extension

Chat with your Laserfiche repository from Claude Desktop — no terminal, no config files.

**1. Download**

[**Download the extension**](https://github.com/SamuelSHernandez/laserfiche-mcp/releases/latest/download/laserfiche-mcp.mcpb) (always the newest version), or browse the [latest release](https://github.com/SamuelSHernandez/laserfiche-mcp/releases/latest). You'll need [Claude Desktop](https://claude.ai/download) installed first.

**2. Double-click & connect**

Double-click the file, click **Install**, and fill in the short form that appears:

| Field | What to enter |
|---|---|
| Repository API URL | Your Laserfiche server address, e.g. `https://your-server/LFRepositoryAPI` |
| Repository name | The repository you pick when signing in to Laserfiche Web Access |
| Username | A Laserfiche account that can read the repository |
| Password | That account's password — stored safely in your computer's keychain |

Not sure what goes where? Ask whoever runs Laserfiche at your organization — it takes them a minute.

**3. Ask**

Open a chat and try:

- *"Find every invoice from March in the Accounting folder."*
- *"What's in the Onboarding folder? Summarize the newest document."*
- *"Search for contracts mentioning Acme and list them with dates."*

> [!NOTE]
> Claude can **look, but never change or delete** — the extension is read-only by default, and your password lives in your operating system's keychain, not a text file.

Full walkthrough for end users and team rollouts: [docs/desktop-extension.md](docs/desktop-extension.md).

### For developers — the Python package

```bash
uvx laserfiche-mcp            # run directly, no install
pip install laserfiche-mcp    # or add it to your environment
```

Requires Python 3.10+ and a reachable Laserfiche **Repository API Server**
(self-hosted) with a service account that can read it, plus any MCP client
(Claude Desktop, Claude Code, MCP Inspector). For local development:

```bash
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

**Optional write-mode variables** (fences and allowlists default off; the delete batch cap and required-field validation default on — see the [Safety model](#safety-model) section for context):

| Variable                            | Default | Purpose                                                                          |
| ----------------------------------- | ------- | -------------------------------------------------------------------------------- |
| `LF_READ_ONLY`                      | `true`  | Set `false` to register the write tools                                          |
| `LF_WRITE_PATHS_ALLOW`              | unset   | Comma-separated path prefixes where writes are permitted (case-insensitive)      |
| `LF_WRITE_PATHS_DENY`               | unset   | Comma-separated path prefixes where writes are refused (deny wins over allow)    |
| `LF_WRITE_TOOLS_ALLOWED`            | unset   | Comma-separated write-tool names to scope what registers; e.g. metadata-only     |
| `LF_DELETE_FOLDER_MAX_DESCENDANTS`  | `50`    | Refuse folder deletes above this immediate-child count unless `force_large_delete=true` |
| `LF_REQUIRE_AUDIT_REASON`           | `false` | When `true`, `delete_entry` refuses to execute without `audit_reason_id`         |
| `LF_VALIDATE_REQUIRED_FIELDS`       | `true`  | Validate the target template's own required fields (fields with a `defaultValue` are skipped) client-side before `assign_template` PUTs |
| `LF_VALIDATE_NAMES`                 | `true`  | Pre-flight field / tag / template / link-type names against cached schema definitions; returns `invalid_*_name` instead of an opaque 400 |
| `LF_SCHEMA_CACHE_TTL_SECONDS`       | `300`   | Cache window for the schema-definition lookups that back `LF_VALIDATE_NAMES` and `LF_VALIDATE_REQUIRED_FIELDS`. Set to `0` to disable caching. |
| `LF_IMPORT_MAX_BYTES`               | `25 MB` | Client-side cap on `import_document` payload size                                |
| `LF_IMPORT_SOURCE_DIRS`             | unset   | Comma-separated local directories `import_document` may read `file_path` from (symlinks resolved before comparison). Unset: any path the MCP process can read is accepted. |
| `LF_EDOC_MAX_BYTES`                 | `25 MB` | Cap on `get_document_edoc` downloads in `bytes`/`text` modes                     |
| `LF_SEARCH_TIMEOUT_SECONDS`         | `60`    | How long `search_content` waits for an async search before abandoning it         |
| `LF_SEARCH_POLL_INTERVAL_SECONDS`   | `1`     | Delay between `search_content` status polls; backs off toward 2s on long searches |
| `LF_SEARCH_CONTEXT_HITS_MAX`        | `10`    | Hard cap on matched passages returned per entry by `search_content`              |
| `LF_LEGACY_TOOL_NAMES`              | `true`  | Also register the v1.x verb-first aliases. Set `false` to roughly halve the per-request tool catalog (the v3.0 behavior, available today) |
| `LF_CONFIRMATION_SECRET`            | unset   | Optional secret the destructive-op confirmation tokens are signed with. Unset: random per-process key, so a **restart invalidates pending preview tokens** (the safer single-instance default). Set it to keep tokens valid across restarts / across instances sharing the secret. Treat like a password. |
| `LF_LOG_FORMAT`                     | `text`  | `json` emits one JSON object per log line (for jq / Datadog / Splunk)            |

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

## Remote HTTP (web clients)

The default transport is **stdio** — for local clients that launch the server
as a subprocess (Claude Desktop, Claude Code, Cursor, Gemini CLI). Web and
cloud clients (**claude.ai custom connectors**, **ChatGPT connectors**) can't
spawn a local process; they connect to a URL. The same server can serve those
clients over **Streamable HTTP**:

```bash
laserfiche-mcp --http                 # binds 127.0.0.1:8000, path /mcp
laserfiche-mcp --http --port 9000     # override port for this run
```

Configuration (all optional, `LF_*` env like everything else):

| Variable | Default | Purpose |
|---|---|---|
| `LF_HTTP_HOST` | `127.0.0.1` | Bind interface. Loopback by default — not reachable off the machine. |
| `LF_HTTP_PORT` | `8000` | Listen port. |
| `LF_HTTP_PATH` | `/mcp` | Endpoint path; clients connect to `http(s)://host:port/mcp`. |
| `LF_HTTP_AUTH_TOKEN` | *(unset)* | Static shared bearer token. Simplest auth; ignored when OAuth is on. |
| `LF_HTTP_OAUTH_ISSUER` | *(unset)* | Turns on **per-user OAuth** — verifies each caller's token against this authorization server. |

The `--http` server chooses its auth by precedence: **OAuth** (if
`LF_HTTP_OAUTH_ISSUER` is set) → **static token** (if `LF_HTTP_AUTH_TOKEN`) →
**none** (loopback only). OAuth is the multi-user path for claude.ai / ChatGPT;
see [Per-user OAuth](#per-user-oauth) below.

Verify locally with the Inspector (point it at the URL, not the command):

```bash
laserfiche-mcp --http &
npx @modelcontextprotocol/inspector   # then connect to http://127.0.0.1:8000/mcp
```

### Per-user OAuth

For a multi-user connector, run the server as an **OAuth 2.1 Resource Server** —
each user signs in through your existing identity provider (LFDS, Microsoft
Entra, Okta, Auth0, Google) and the server verifies their token:

```bash
pip install 'laserfiche-mcp[oauth]'

LF_HTTP_OAUTH_ISSUER="https://login.microsoftonline.com/<tenant>/v2.0" \
LF_HTTP_PUBLIC_URL="https://lf.example.com/mcp" \
LF_HTTP_OAUTH_AUDIENCE="api://laserfiche-mcp" \
LF_HTTP_OAUTH_REQUIRED_SCOPES="laserfiche.read" \
  laserfiche-mcp --http --host 0.0.0.0
```

The server then serves protected-resource metadata (RFC 9728), so claude.ai /
ChatGPT discover your authorization server, run the `authorization_code` + PKCE
flow, and present a bearer token that this server verifies (signature via JWKS,
plus `aud` / `iss` / `exp` / scopes). This is authentication **at the edge** —
verified requests still reach Laserfiche via the shared service account, so the
Laserfiche audit trail shows that account, not the end user. Full details,
including IdP registration and the security checklist, are in
[docs/remote-http.md](docs/remote-http.md).

### Connecting a web client

claude.ai and ChatGPT connectors need a **public HTTPS URL**. In practice that
means putting this server behind a reverse proxy (or a tunnel like `cloudflared`
/ `ngrok` for a spike) and adding the resulting `https://…/mcp` URL as a custom
connector in the client's settings.

> [!WARNING]
> **Read this before exposing `--http` to a network.**
> - Configure auth: OAuth (`LF_HTTP_OAUTH_ISSUER`) for multi-user, or at least a
>   long random `LF_HTTP_AUTH_TOKEN`. Binding off-loopback with neither logs a
>   warning and leaves your repository reachable unauthenticated.
> - Terminate **TLS at a reverse proxy** in front of this server (it speaks plain HTTP).
> - Your Laserfiche server is self-hosted behind a firewall; a public connector
>   needs a deliberate **network path in** to it (VPN / DMZ / tunnel).
> - See [docs/remote-http.md](docs/remote-http.md) for the full deployment and
>   security checklist.

## Command line (no model, no tokens)

Everything the MCP tools do against the repository, the same binary does from
a shell — same code path, no LLM in the loop. Use it for the work that has one
correct answer: inventories, downloads, dedupe, grepping a contract.

```bash
laserfiche-mcp setup                           # one-time: save connection details
laserfiche-mcp ls 1                            # list a folder (alias: list)
laserfiche-mcp ls '\HR\Leases'                 # ...or by path
laserfiche-mcp get 4821 --to ./lease.pdf       # stream a document to disk
laserfiche-mcp cat 4821                        # extracted text on stdout
laserfiche-mcp cat 4821 --pages 4-9            # just those pages
laserfiche-mcp find 4821 'unpaid balance'      # grep one document
laserfiche-mcp search 'unpaid balance'         # grep the whole repository
laserfiche-mcp manifest 1 --out inventory.csv  # walk a tree, write CSV
laserfiche-mcp dedupe 1                        # byte-identical documents
laserfiche-mcp diff 4821 4822                  # compare two entries (alias: compare)
```

Office-friendly synonyms work everywhere: `list`, `read` (cat), `download`
(get), `compare` (diff), `duplicates` (dedupe). On Windows, quote paths
with double quotes: `laserfiche-mcp list "\HR\Leases"`.

`ENTRY` and `FOLDER` accept a numeric entry ID or a repository path. Every
command takes `--json`, so the same invocation backs a shell pipeline:

```bash
laserfiche-mcp search 'termination' --json | jq -r '.results[].name'
laserfiche-mcp manifest 1 --json | jq '.by_extension'
laserfiche-mcp manifest 1 --out tree.jsonl --format jsonl && jq -r 'select(.extension=="pdf").name' tree.jsonl
```

Conventions that make these scriptable:

- Results go to **stdout**; progress and warnings go to **stderr**.
- Exit codes are meaningful: `0` success, `1` the operation failed or found
  nothing, `2` bad usage. `laserfiche-mcp find 4821 'indemnify' >/dev/null &&
  echo present` does what you would expect.
- Nothing here writes to the repository. The subcommands are read-only by
  construction and never register the write tools.

### What each command saves you

| Command | Instead of | Why it is cheaper |
| --- | --- | --- |
| `get --to` | a base64 blob in a tool result | Streamed to disk; the bytes never enter a context window, and never sit in RAM either |
| `cat` / `find` | reading a whole document into the model | Extraction and matching happen locally; you get the passage, not the file |
| `search` | opening each hit to see what it says | Laserfiche's own OCR index returns the matched passages |
| `manifest` | one `list_folder` call per subfolder | One walk, one CSV, a summary you can read at a glance |
| `dedupe` | downloading everything to compare | Sizes are probed first; only size collisions are ever downloaded |
| `diff` | eyeballing two records side by side | A set comparison with one correct answer |

### Document formats

`cat`, `find`, and the MCP's `mode="text"` read these with **no extra
dependencies**: PDF, DOCX, PPTX, EML, HTML, RTF, and anything `text/*`
(including CSV, JSON, XML, Markdown).

Two more need the optional extra:

```bash
pip install 'laserfiche-mcp[office]'   # adds .xlsx and Outlook .msg
```

Legacy binary `.doc` / `.xls` / `.ppt` are not supported — correct extraction
needs an external converter such as LibreOffice, and bundling that would break
the "pip install and it works" promise. Re-save as OOXML, or read Laserfiche's
own indexed text with `search`.

Scanned images have no text layer at all. `cat` says so explicitly rather than
returning an empty string, and points you at `search`, which reads the OCR
index where that text actually lives.

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
| `search_content`             | `laserfiche_entry_search_content`      | Full-text search that returns the **matched passages** — page number plus excerpt, straight from the OCR index |
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
bound to `(operation, entry_id, entry_name)` **and the operation's own
parameters** (`page_range` for `delete_pages`, `new_name` for
`rename_entry`, destination + name for `move_entry`) — executing with
different arguments than were previewed fails verification. They expire
after 5 minutes,
and are invalidated by server restart (unless `LF_CONFIRMATION_SECRET`
is set — see [Configure](#configure)).

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

### Using `search_content`

The other search tools answer *which* entries matched. `search_content`
answers *what they say*, by returning the matched passages themselves —
page number, surrounding excerpt, and the exact substring that matched —
pulled from Laserfiche's full-text index, which is where OCR output for
scanned documents lives.

```
search_content(query="unpaid balance", folder_path="\Leases")
→ { "results": [
      { "entry_id": 7, "name": "lease-4821.pdf", "hit_count": 6,
        "hits": [ { "page": 4,
                    "text": "…tenant owes an unpaid balance of $2,400 as of March…",
                    "match": "unpaid balance" } ] } ] }
```

That answers most "what does this document say about X" questions without
downloading anything. Reach for `get_document_edoc(mode="text")` only when
an excerpt points you at the right document and you need more of it.

A bare phrase is wrapped into `{LF:Basic~="..."}` for you. Pass a query
starting with `{` to use raw syntax instead — e.g. `option="D"` to search
only OCR'd document text, or `option="DFANLT"` to permit leading and
trailing wildcards.

Two knobs control cost: `hits_for_top` (how many results get their
passages fetched — one extra request each, default 5) and `hits_per_entry`
(passages per result, default 3, capped by `LF_SEARCH_CONTEXT_HITS_MAX`).

This is the asynchronous `/Searches` flow rather than `SimpleSearches`,
which is what makes context hits available at all — they're keyed by a
search token that only the async flow produces. Laserfiche caps concurrent
searches per session (two, on v1), so the tool runs the whole
create→poll→read→close cycle inside one call and always releases the token,
including on timeout and failure. Older builds without the `/Searches`
endpoints get a structured `async_search_unavailable` error pointing at
`search_entries` as the fallback.

### `get_document_edoc` modes

On v1 servers the Laserfiche `Text` export endpoint doesn't exist, so
`get_document_text` cannot return anything. `get_document_edoc` gained a
`mode` parameter as the workaround:

| Mode      | Use it when                                                |
| --------- | ---------------------------------------------------------- |
| `info`    | You only need metadata (size, content-type). Default.      |
| `bytes`   | You want the raw file as base64 — capped at `LF_EDOC_MAX_BYTES` (25 MB by default; override per-call with `max_bytes`). |
| `text`    | You want extracted text. PDF, DOCX, PPTX, XLSX, EML, HTML, RTF and `text/*` are handled (format detected from content-type + entry name — octet-stream PDFs work). OCR is not attempted; for scans use `search_content`, which reads the OCR index. |

All tool descriptions are written to read like prompts — they tell the
model when to use the tool, valid input shapes, and what kind of follow-up
is expected. See [`src/laserfiche_mcp/server.py`](src/laserfiche_mcp/server.py).

## Troubleshooting

**Start with one command:**

```bash
laserfiche-mcp diagnose
```

It authenticates, probes every endpoint your Laserfiche build exposes, and
prints an OK/unavailable table plus your write-mode and logging config.
Failures are classified — it will tell you whether the server was
unreachable (URL/VPN/TLS, *not* your password), whether the other
`LF_API_VERSION` would work (it probes both and names the right one), or
whether the credentials themselves were rejected.

No config yet, or config in doubt? Run the wizard:

```bash
laserfiche-mcp setup
```

It asks for the server URL, repository, and service account, saves them to
`~/.laserfiche-mcp/.env` (`%USERPROFILE%\.laserfiche-mcp\.env` on
Windows), and ends with a diagnose run. Every later CLI invocation finds
that file automatically when nothing else is configured.

Windows notes:

- In **PowerShell/cmd**, quote repository paths with double quotes:
  `laserfiche-mcp list "\HR\Leases"`. Avoid a trailing backslash before
  the closing quote — it escapes the quote.
- Claude Desktop logs live at `%APPDATA%\Claude\logs\` (macOS:
  `~/Library/Logs/Claude/`). Look for `mcp-server-laserfiche.log`.

Common misconfigurations:

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| diagnose says UNREACHABLE | Wrong `LF_REPO_API_URL`, VPN down, self-signed cert | Fix the URL; for internal certs set `LF_VERIFY_SSL=false` (dev only) |
| diagnose suggests the other version | `LF_API_VERSION` mismatch | Set the version it names |
| HTTP 401, or LF error 9528 ("LFDS unreachable") | Bad credentials — 9528's wording is misleading | Re-run `laserfiche-mcp setup` or fix `LF_USERNAME`/`LF_PASSWORD` |

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

Wondering **what Claude actually sees and where your document content goes**?
See [Data handling & privacy](docs/data-handling.md) — it covers the data flow,
what leaves the machine, and how to scope a service account so sensitive folders
are never exposed. The rest of this section is about the **write-mode** guards.

Writes are off by default. When you enable them (`LF_READ_ONLY=false`),
the following guards are available — all independent, all opt-in
except as noted:

- **Path-prefix fences** (`LF_WRITE_PATHS_ALLOW`, `LF_WRITE_PATHS_DENY`) — every write checks the entry's `fullPath` (or the parent's for creates) against the configured prefixes. Case-insensitive, deny wins over allow, both `\` and `/` accepted. `move_entry` fences on BOTH source and destination paths so a token from an allowed source can't be replayed to land in a denied folder. Strongest single fence — recommended for any non-trivial deployment.
- **Local import source fence** (`LF_IMPORT_SOURCE_DIRS`, default unset) — `import_document` reads `file_path` off the MCP process's own filesystem; with this set, the resolved (symlinks included) path must fall inside one of the configured directories or the tool refuses with `source_path_not_allowed`. This is the source-side counterpart to the path-prefix fences above, which only cover the repository destination.
- **Tool-level allowlist** (`LF_WRITE_TOOLS_ALLOWED`) — restrict which write tools register at all. Example: `merge_fields,merge_tags,assign_template` for a metadata-only deployment that can't create or delete anything.
- **Folder-delete batch cap** (`LF_DELETE_FOLDER_MAX_DESCENDANTS`, default 50) — `delete_entry` on a folder with more immediate children refuses unless `force_large_delete=true` is passed alongside the confirmation token. The preview surfaces `exceeds_batch_cap: true` so the LLM can explain the size before re-calling.
- **Audit-reason requirement** (`LF_REQUIRE_AUDIT_REASON`, default false) — when true, `delete_entry` refuses without an `audit_reason_id`. Use `get_audit_reasons` to enumerate valid IDs.
- **Required-field validation** (`LF_VALIDATE_REQUIRED_FIELDS`, default **true**) — `assign_template` lists `FieldDefinitions`, finds `isRequired: true` fields, checks them against what's on the entry and what's in the caller's `fields=`, and returns a structured `missing_required_fields` error before the PUT — instead of the server's opaque `Multistatus response. [9039]`.
- **Two-step confirmation tokens** (always on for destructive ops) — `rename_entry`, `move_entry`, `delete_entry`, `delete_edoc`, `delete_pages` return a preview + HMAC-signed token on first call; execute on second call. Tokens bind to `(operation, entry_id, entry_name)` plus the operation's execute-relevant parameters (`page_range`, `new_name`, destination), so the execute leg cannot silently swap in different arguments than the user confirmed; they expire after 5 minutes. By default the signing key is random per-process, so a restart invalidates pending tokens; set `LF_CONFIRMATION_SECRET` to derive a stable key instead (tokens survive restarts and verify across instances sharing the secret — for multi-instance deployments).

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

- **Server-side audit logging** — sidecar file with rotation, capturing
  every write tool call with the authenticated user, target entry, and
  outcome.
- **Cloud** — Laserfiche Cloud support (`signin.laserfiche.com`
  JWT-signed `client_credentials` flow plus the `api.laserfiche.com`
  v2-only endpoint surface).
- **v3.0** — Remove the verb-first deprecation aliases (`get_entry`,
  `set_fields`, ...). Only the `laserfiche_{resource}_{verb}` names
  remain.
- **Stateless MCP (spec 2026-07-28)** — the protocol core is now
  stateless: the initialize handshake and session IDs are gone, and
  cross-call state must live in server-minted handles passed as ordinary
  tool arguments. This server is already shaped for that world — the
  HMAC-signed `confirmation_token` is exactly such a handle (set
  `LF_CONFIRMATION_SECRET` so every instance can verify every instance's
  tokens), and the async-search token never crosses a call boundary. The
  remaining review item for a remote/multi-instance deployment is that
  the per-process lifespan client and schema caches become per-instance.
  The cacheable `tools/list` in the new spec also raises the value of a
  small catalog (`LF_LEGACY_TOOL_NAMES=false`).
- **Beyond** — Workflow trigger tools, MCP resource links for edocs, and
  per-viewer table summaries for spreadsheet entries.

## Development

```bash
uv sync --extra dev
uv run pytest                  # mocked HTTP, enforces 85% coverage baseline
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
- Text extraction for more document formats (`ops/extract.py`)

This is a community project, **not** affiliated with or endorsed by
Laserfiche.

## License

Released under the [MIT License](LICENSE). Copyright (c) 2026 Samuel S. Hernandez.
