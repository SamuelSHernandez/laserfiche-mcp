# laserfiche-mcp

A [Model Context Protocol](https://modelcontextprotocol.io) server that lets
Claude (Desktop, Code, or any MCP client) search and read documents in a
[Laserfiche](https://www.laserfiche.com) repository.

> **v1 status:** read-only, self-hosted Repository API Server.
> Cloud and write tools are scoped for v2 / v1.1.

## What you can do with it

Once connected, Claude can:

- Search the repository with native Laserfiche search syntax
- List the contents of any folder
- Look up an entry by ID or full path
- Read all template field values on an entry
- Download an electronic document and read its text

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

Minimum required variables for self-hosted basic auth:

| Variable             | Example                                       |
| -------------------- | --------------------------------------------- |
| `LF_REPO_API_URL`    | `https://lf.example.com/LFRepositoryAPI`      |
| `LF_REPOSITORY_ID`   | `my-repo`                                     |
| `LF_USERNAME`        | `service-account`                             |
| `LF_PASSWORD`        | (your service account password)               |
| `LF_AUTH_MODE`       | `basic`                                       |
| `LF_READ_ONLY`       | `true` (default — write tools are not yet implemented) |

See [`.env.example`](.env.example) for the full list including pagination
limits, request timeout, and reserved OAuth/cloud fields.

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
        "LF_AUTH_MODE": "basic",
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

| Tool                 | Purpose                                                              |
| -------------------- | -------------------------------------------------------------------- |
| `search_entries`     | Run a Laserfiche search query, e.g. `{LF:Name="*.pdf"}`              |
| `list_folder`        | List children of a folder by ID                                       |
| `get_entry`          | Fetch metadata for one entry by ID                                    |
| `get_entry_by_path`  | Resolve a full path (e.g. `\Imports\2024\Smith,John`) to an entry     |
| `get_field_values`   | Read all template fields assigned to an entry                         |
| `get_document_text`  | Download an electronic document's content as text (truncated by default) |

All tool descriptions are written to read like prompts — they tell the
model when to use the tool, valid input shapes, and what kind of follow-up
is expected. See [`src/laserfiche_mcp/server.py`](src/laserfiche_mcp/server.py).

## Roadmap

- **v1.1** — Write tools (`update_field_values`, `move_entry`) gated behind `LF_READ_ONLY=false`; OAuth/LFDS auth.
- **v2** — Laserfiche Cloud support.
- **Beyond** — Workflow trigger tools, batch field updates, advanced search builders.

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

- Endpoint corrections for older Repository API Server versions
- LFDS OAuth token discovery in `auth.py`
- Cloud (`api.laserfiche.com`) client implementation

This is a community project, **not** affiliated with or endorsed by
Laserfiche.

## License

Released under the [MIT License](LICENSE). Copyright (c) 2026 Samuel S. Hernandez.
