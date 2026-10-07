<!-- mcp-name: io.github.SamuelSHernandez/laserfiche-mcp -->

# laserfiche-mcp

[![PyPI version](https://img.shields.io/pypi/v/laserfiche-mcp.svg)](https://pypi.org/project/laserfiche-mcp/)
[![Python versions](https://img.shields.io/pypi/pyversions/laserfiche-mcp.svg)](https://pypi.org/project/laserfiche-mcp/)
[![CI](https://github.com/SamuelSHernandez/laserfiche-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/SamuelSHernandez/laserfiche-mcp/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![MCP](https://img.shields.io/badge/MCP-Model%20Context%20Protocol-1f6feb.svg)](https://modelcontextprotocol.io)

> **Community project — not affiliated with or endorsed by Laserfiche.**

Let Claude (Desktop, Code, or any [MCP](https://modelcontextprotocol.io)
client) search and read documents in a self-hosted
[Laserfiche](https://www.laserfiche.com) repository — and, if you opt in,
change them. The same program is also a command-line tool (`ls`, `cat`,
`search`, `dedupe`, ...) for work that needs no AI at all.

**Read-only by default.** Writes are off until you set `LF_READ_ONLY=false`,
and destructive actions always need a second confirmation step.
Current release: **v2.5.0** — see the [CHANGELOG](CHANGELOG.md).

## Quick start

```bash
uv tool install laserfiche-mcp   # or run ad hoc: uvx laserfiche-mcp
laserfiche-mcp setup             # asks for your server + account, then tests the connection
laserfiche-mcp ls 1              # list the repository root
```

Then [connect it to Claude](#connect-to-claude). Prefer no terminal? Use the
[Claude Desktop extension](#claude-desktop-extension). New to MCP? Read the
[step-by-step guide](docs/getting-started.md) (~7 min). Want ready-made
prompts and commands? See [examples](docs/examples.md).

## What it can do

Once connected, you can ask Claude things like:

- *"Which leases mention an unpaid balance, and what do they say?"*
- *"Summarize the newest document in the Onboarding folder."*
- *"Are these two scans the same record, or is something different?"*
- *"Find duplicate invoices under 2023 and tell me how much space they take."*
- *"Set Status to Approved on these five entries."*

Behind that, it can:

**Find things**
- Search with native Laserfiche queries or by name pattern — or let Claude
  draft the query from your repository's own templates and field names, with
  automatic repair if Laserfiche rejects it.
- **Search inside documents, including scans.** Full-text search returns the
  matching *passages* (page number plus surrounding text) straight from
  Laserfiche's OCR index, so most "what does it say about X?" questions are
  answered without downloading anything.
- **Look at images.** Claude can view picture files in the repository (PNG, JPEG,
  GIF, WebP) to describe, label and tag them against your own tag and template
  standard. Images cost tokens, so you're warned of the estimated cost and asked
  first; for scans of text it tries Laserfiche's OCR before looking at the picture.
  Optional `laserfiche-mcp[images]` adds downscaling.

**Read things**
- Browse folders, look up entries by ID or path, and read metadata, template
  field values, and your repository's field / tag / template / link definitions.
- Download files, or extract text — PDF, Word, PowerPoint, email, HTML, RTF and
  plain text (Excel and Outlook `.msg` need `pip install 'laserfiche-mcp[office]'`)
  — with page ranges for long documents.

**Analyze**
- Compare two entries' metadata and fields, and find byte-identical duplicates
  (only files whose sizes match are ever downloaded). From the command line you
  can also inventory a whole folder tree to CSV.

**Make changes** (opt-in)
- Create folders, import documents, copy, rename and move entries.
- Edit fields, tags and links (merge or replace), and assign or clear templates
  — with required-field checks before Laserfiche sees the request.
- Delete entries, files, or page ranges. Deletes, renames and moves always
  show a preview first; folders can be fenced, tools allow-listed, and bulk
  deletes capped. See [Safety](#safety).

**Run where you need it**
- Locally in Claude Desktop or Claude Code, as a one-click Desktop extension, or
  as an HTTP server for claude.ai and ChatGPT with per-user sign-in.
- Self-hosted Laserfiche (Repository API v1 and v2); Cloud support is in beta.
- As a **command-line tool** with no AI involved — the same operations from a
  shell, with `--json` output for scripts. See [docs/cli.md](docs/cli.md).

Every tool, with details: [docs/tools.md](docs/tools.md).

## Install

### Claude Desktop extension

No terminal, no config files.

1. [Download the extension](https://github.com/SamuelSHernandez/laserfiche-mcp/releases/latest/download/laserfiche-mcp.mcpb) (needs [Claude Desktop](https://claude.ai/download)).
2. Double-click it, click **Install**, and fill in the form:

   | Field | What to enter |
   |---|---|
   | Repository API URL | Your server address, e.g. `https://your-server/LFRepositoryAPI` |
   | Repository name | The repository you pick when signing in to Laserfiche Web Access |
   | Username / Password | An account that can read the repository (the password goes in your OS keychain) |

   Not sure what to enter? Ask whoever runs Laserfiche at your organization.
3. Ask Claude something, like *"Find every invoice from March in the Accounting folder."*

More detail: [docs/desktop-extension.md](docs/desktop-extension.md).

### Python package

```bash
uvx laserfiche-mcp            # run directly, no install
pip install laserfiche-mcp    # or add it to your environment
```

Needs Python 3.10+ and a reachable Laserfiche Repository API Server with an
account that can read it.

## Configure

Run `laserfiche-mcp setup`, or set these environment variables:

| Variable | Example | Notes |
|---|---|---|
| `LF_REPO_API_URL` | `https://lf.example.com/LFRepositoryAPI` | Your Repository API Server |
| `LF_REPOSITORY_ID` | `my-repo` | |
| `LF_API_VERSION` | `v1` (default) or `v2` | Wrong value → `400 UnsupportedApiVersion`. Probe with `curl {LF_REPO_API_URL}/v1/Repositories` and `/v2/Repositories`; the one that returns a repo list is yours. |
| `LF_USERNAME` / `LF_PASSWORD` | | A service account |
| `LF_AUTH_MODE` | `password` | |
| `LF_READ_ONLY` | `true` (default) | `false` turns on the write tools |

Everything else — write guardrails, size limits, search tuning, web-client
links, logging — is in [docs/configuration.md](docs/configuration.md).

> [!WARNING]
> Laserfiche **Cloud** support is **beta** and has never been verified against
> a live tenant. If you have Cloud access, please
> [open an issue](https://github.com/SamuelSHernandez/laserfiche-mcp/issues)
> with what worked or broke. Details in
> [docs/configuration.md](docs/configuration.md).

## Connect to Claude

**Claude Code**

```bash
claude mcp add laserfiche -- uvx laserfiche-mcp
```

Pass settings with `--env LF_REPO_API_URL=...` flags, or export them in your shell first.

**Claude Desktop** — add this to `claude_desktop_config.json`
(`%APPDATA%\Claude\` on Windows, `~/Library/Application Support/Claude/` on macOS), then restart:

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

**Try a tool without Claude** using the MCP Inspector:
`npx @modelcontextprotocol/inspector uvx laserfiche-mcp`

## Web clients (claude.ai, ChatGPT)

By default the server talks over stdio to a local client. Web connectors can't
launch a local program, so run it as an HTTP server instead:

```bash
laserfiche-mcp --http          # serves http://127.0.0.1:8000/mcp
```

For anything beyond your own machine you need TLS, authentication (per-user
OAuth or a shared token) and a network path to your Laserfiche server. Setup
and the security checklist: [docs/remote-http.md](docs/remote-http.md).

## Command line

```bash
laserfiche-mcp ls '\HR\Leases'                 # list a folder (by path or entry ID)
laserfiche-mcp get 4821 --to ./lease.pdf       # download a document
laserfiche-mcp cat 4821 --pages 4-9            # print its text
laserfiche-mcp search 'unpaid balance'         # full-text search with excerpts
laserfiche-mcp manifest 1 --out inventory.csv  # inventory a folder tree
laserfiche-mcp dedupe 1                        # find byte-identical documents
```

Every command takes `--json`. More recipes in [examples](docs/examples.md);
the full reference is [docs/cli.md](docs/cli.md).

## Safety

- Writes are **off by default**.
- When on, you can fence them to specific folders (`LF_WRITE_PATHS_ALLOW`),
  limit which write tools exist (`LF_WRITE_TOOLS_ALLOWED`), and cap folder
  deletes.
- `delete`, `rename` and `move` return a **preview** and a short-lived token;
  nothing changes until the call is repeated with that token.
- Start in a sandbox folder: see the
  [recommended write-mode config](docs/safety.md#recommended-starting-config-for-write-mode).

Where your document content goes: [Data handling & privacy](docs/data-handling.md).
All guardrails: [docs/safety.md](docs/safety.md).

## Troubleshooting

Start with one command — it tests your login, probes your server, and tells
you what's wrong:

```bash
laserfiche-mcp diagnose
```

| Symptom | Likely cause | Fix |
|---|---|---|
| `diagnose` says UNREACHABLE | Wrong URL, VPN down, or an `https://` server whose certificate comes from your internal CA | Fix the URL; for an internal CA set `LF_USE_SYSTEM_CA=true` (or `LF_CA_BUNDLE=<pem>`) — not `LF_VERIFY_SSL=false`, which switches checking off |
| `CERTIFICATE_VERIFY_FAILED ... hostname mismatch` | The URL's host isn't a name on the certificate (e.g. short name vs. the full `host.domain` name) | Use the host name on the certificate in `LF_REPO_API_URL` |
| `diagnose` suggests the other API version | `LF_API_VERSION` mismatch | Use the version it names |
| HTTP 401, or Laserfiche error 9528 | Bad credentials (9528's "LFDS unreachable" wording is misleading) | Re-run `laserfiche-mcp setup` |
| A setting seems ignored | A misspelled `LF_*` name — unknown names are silently skipped | `diagnose` lists any `LF_*` variable it doesn't recognize |

On Windows, put repository paths in double quotes: `laserfiche-mcp list "\HR\Leases"`.
Claude Desktop logs: `%APPDATA%\Claude\logs\` (macOS: `~/Library/Logs/Claude/`).

When a tool fails it returns a structured error (not a crash) that tells the
model whether to retry, ask you, or give up. See
[docs/error-contract.md](docs/error-contract.md).

## Documentation

| | |
|---|---|
| [Getting started](docs/getting-started.md) | Slower walkthrough for people new to MCP |
| [Examples](docs/examples.md) | Prompts, commands and config you can copy |
| [Configuration](docs/configuration.md) | Every `LF_*` setting |
| [Tools](docs/tools.md) | All tools and how to use them |
| [Command line](docs/cli.md) | CLI conventions, formats and cost savings |
| [Safety model](docs/safety.md) | Write guardrails and confirmation tokens |
| [Data handling & privacy](docs/data-handling.md) | What Claude sees and where data goes |
| [Remote HTTP](docs/remote-http.md) | Web clients, OAuth, per-user access |
| [Error contract](docs/error-contract.md) | Error kinds and codes |

## Roadmap

- **Cloud verification** — needs someone with a Laserfiche Cloud tenant to confirm the auth flow end to end.
- **Per-user access (beta)** — `LF_AUTH_MODE=oauth_passthrough` runs Laserfiche calls as each user instead of a shared account; needs confirming against a live LFDS tenant ([details](docs/remote-http.md)).
- **Audit logging** — a server-side log of every write call.
- **v3.0** — remove the old verb-first tool names (`get_entry`, ...); only `laserfiche_{resource}_{verb}` remain.
- **Later** — Workflow trigger tools; stateless-MCP review for multi-instance deployments.

## Contributing

Issues and PRs welcome. Run the tests with:

```bash
uv sync --extra dev
uv run pytest && uv run ruff check src tests && uv run mypy src
```

See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, where help is most wanted,
and the opt-in integration tests. This is a community project, **not**
affiliated with or endorsed by Laserfiche.

## License

Released under the [MIT License](LICENSE). Copyright (c) 2026 Samuel S. Hernandez.
