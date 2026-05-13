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

> **Current release: v1.1.0** — read-only, self-hosted Repository API v1 and v2.
> Endpoint paths and auth flow are validated against the official
> [`Laserfiche/lf-repository-api-client-java`](https://github.com/Laserfiche/lf-repository-api-client-java)
> client. Cloud (JWT-signed client_credentials) and write tools are still
> on the roadmap.

## What you can do with it

Once connected, Claude can:

- Search the repository with native Laserfiche search syntax (or by name pattern via the convenience tool)
- Ask the repository for guidance and then run a natural-language-derived search with automatic 400 repair (`search_natural`, see below)
- List the contents of any folder
- Look up an entry by ID or full path
- Read all template field values on an entry
- Read a document's Laserfiche-extracted text (v2 servers) or extract text client-side from the raw edoc (v1 servers, PDF via pypdf)
- Inspect document metadata, fetch the raw edoc as base64, or get server-side extracted text — all via `get_document_edoc(..., mode=...)`

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
| `LF_READ_ONLY`       | `true` (default — write tools are not yet implemented) |

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

| Tool                 | Purpose                                                                 |
| -------------------- | ----------------------------------------------------------------------- |
| `search_entries`     | Run a raw Laserfiche search query, e.g. `{LF:Name="*.pdf"}`             |
| `search_by_name`     | Convenience wrapper: name pattern + optional folder scope               |
| `search_natural`     | Two-mode guided search: ask for grammar+templates, then run with auto-repair on 400 |
| `list_folder`        | List children of a folder by ID                                          |
| `get_entry`          | Fetch metadata for one entry by ID                                       |
| `get_entry_by_path`  | Resolve a full path (e.g. `\Imports\2024\Smith,John`) to an entry        |
| `get_field_values`   | Read all template fields assigned to an entry                            |
| `get_document_text`  | Download a document's Laserfiche-extracted text (v2 only; v1 should use `get_document_edoc(mode="text")`) |
| `get_document_edoc`  | Inspect an edoc (`mode="info"`), download as base64 (`mode="bytes"`), or extract text server-side (`mode="text"`, PDF via pypdf) |

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

## Roadmap

- **Next** — Write tools (`update_field_values`, `move_entry`, rename) gated behind `LF_READ_ONLY=false`.
- **Cloud** — Laserfiche Cloud support (`signin.laserfiche.com` JWT-signed `client_credentials` flow).
- **Beyond** — Workflow trigger tools, batch field updates, async search for large result sets, server-side text extraction for Office documents.

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

- Endpoint corrections for older Repository API Server versions (v0.2.0 targets V2)
- Laserfiche Cloud client + JWT-signed `client_credentials` assertion flow
- Write tools (`update_field_values`, `move_entry`) and async-search support

This is a community project, **not** affiliated with or endorsed by
Laserfiche.

## License

Released under the [MIT License](LICENSE). Copyright (c) 2026 Samuel S. Hernandez.
