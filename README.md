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

> **Current release: v0.2.0** — read-only, self-hosted Repository API V2.
> Endpoint paths and auth flow are validated against the official
> [`Laserfiche/lf-repository-api-client-java`](https://github.com/Laserfiche/lf-repository-api-client-java)
> client. Cloud (JWT-signed client_credentials) and write tools are scoped
> for v2 / v1.1.

## What you can do with it

Once connected, Claude can:

- Search the repository with native Laserfiche search syntax (or by name pattern via the convenience tool)
- List the contents of any folder
- Look up an entry by ID or full path
- Read all template field values on an entry
- Read a document's Laserfiche-extracted text
- Inspect document metadata (size of the raw Edoc, without dumping bytes into the model)

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
| `LF_USERNAME`        | `service-account`                             |
| `LF_PASSWORD`        | (your service account password)               |
| `LF_AUTH_MODE`       | `password`                                    |
| `LF_READ_ONLY`       | `true` (default — write tools are not yet implemented) |

See [`.env.example`](.env.example) for the full list including OAuth
config, pagination limits, request timeout, retry attempts, and SSL
verification.

> **Auth note:** Laserfiche self-hosted does not accept HTTP Basic auth.
> The server exchanges your username/password for a bearer token at
> `POST /v2/{repository_id}/Token` on first request and refreshes it
> automatically before expiry.

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
| `list_folder`        | List children of a folder by ID                                          |
| `get_entry`          | Fetch metadata for one entry by ID                                       |
| `get_entry_by_path`  | Resolve a full path (e.g. `\Imports\2024\Smith,John`) to an entry        |
| `get_field_values`   | Read all template fields assigned to an entry                            |
| `get_document_text`  | Download a document's Laserfiche-extracted text (truncated by default)   |
| `get_document_edoc`  | Inspect raw electronic document metadata (size + hint, never raw bytes) |

All tool descriptions are written to read like prompts — they tell the
model when to use the tool, valid input shapes, and what kind of follow-up
is expected. See [`src/laserfiche_mcp/server.py`](src/laserfiche_mcp/server.py).

## Roadmap

- **v1.1** — Write tools (`update_field_values`, `move_entry`, rename) gated behind `LF_READ_ONLY=false`.
- **v2** — Laserfiche Cloud support (`signin.laserfiche.com` JWT-signed `client_credentials` flow).
- **Beyond** — Workflow trigger tools, batch field updates, advanced search builders, async search for large result sets.

## Development

```bash
uv sync --extra dev
uv run pytest                  # smoke tests against mocked HTTP
uv run ruff check src tests
uv run mypy src
```

Tests use `pytest-httpx` to mock the Repository API; they don't require a
real Laserfiche server. For integration testing against a real repository,
use the MCP Inspector pointed at `uv run laserfiche-mcp` with a populated
`.env`.

## Contributing

Issues and PRs welcome — particularly:

- Endpoint corrections for older Repository API Server versions (v0.2.0 targets V2)
- Laserfiche Cloud client + JWT-signed `client_credentials` assertion flow
- Write tools (`update_field_values`, `move_entry`) and async-search support

This is a community project, **not** affiliated with or endorsed by
Laserfiche.

## License

Released under the [MIT License](LICENSE). Copyright (c) 2026 Samuel S. Hernandez.
