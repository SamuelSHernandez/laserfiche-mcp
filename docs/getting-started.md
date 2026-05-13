# Connecting Claude to a Laserfiche repository

*Getting started · ~7 min read*

The Model Context Protocol — MCP — is Anthropic's attempt to standardize the
way language models reach into the systems we already use. Instead of bolting
a bespoke integration onto every assistant, you write one server that exposes
a set of tools, and any MCP-aware client can call them. Claude Desktop, Cursor,
Zed, and a growing list of others already speak the protocol. The promise is
unfussy: the model becomes useful inside the boundaries of your own data.

For records and document management, the implication is interesting.
Repositories like Laserfiche hold the unglamorous source of truth at most
organizations — executed contracts, invoice approvals, HR files, the audit
trail behind business processes that have run quietly for twenty years. None
of that lives in the model's training data, and most of it shouldn't. But the
question of *"where did we land on the renewal terms for the Acme account"* is
one a good assistant should be able to answer, if you let it look.

This guide walks through getting `laserfiche-mcp` running on your machine and
connected to Claude Desktop. By the end of it, you'll be able to ask Claude
to find an entry, retrieve its metadata, and summarize what it found —
without writing any glue code. The tutorial assumes you've used a terminal
and edited a JSON config file before. It does not assume you've used MCP
before.

## What you'll need

Four things, and not much else.

- **Python 3.10 or newer.** The server is published on PyPI. If you already
  have [`uv`](https://github.com/astral-sh/uv) installed, that's all you need
  — `uvx` will fetch and run the server without a global install.
- **A reachable Laserfiche Repository API Server endpoint.** Self-hosted only
  in v1 — URLs shaped like `https://lf.example.com/LFRepositoryAPI`.
  Laserfiche Cloud is on the v2 roadmap, not yet supported.
- **A service account with read access to the repository.** Username +
  password is the default auth path; OAuth via Laserfiche Directory Server
  (LFDS) is supported if your environment is set up for it.
- **[Claude Desktop](https://claude.ai/download),** or another MCP-capable
  client. The configuration below is written for Claude Desktop; if you're on
  Claude Code or another client, the principles transfer cleanly and there's
  a one-liner for Claude Code further down.

## Install the server

The fastest path is `uvx`, which runs the server without installing it
globally. Claude Desktop will invoke it on your behalf — you don't need to
run it yourself first, but it's worth a sanity check:

```bash
uvx laserfiche-mcp --help
```

That should print a brief CLI help screen. If you see a Python traceback
complaining about the Python version, your `python` is older than 3.10 —
update or let `uv` manage a newer one for you.

If you'd rather install permanently, `pip install laserfiche-mcp` works
identically. The published wheel lives on PyPI.

## Configure access

The server reads its configuration from environment variables. The minimum
set for the password-grant auth path:

```bash
LF_DEPLOYMENT_MODE=self_hosted
LF_REPO_API_URL=https://lf.example.com/LFRepositoryAPI
LF_REPOSITORY_ID=my-repo
LF_AUTH_MODE=password
LF_USERNAME=service-account
LF_PASSWORD=replace-me
```

`LF_REPO_API_URL` is the base URL of your Repository API Server with no
trailing path — the server appends `/v2/{repository}/...` on each call.
`LF_REPOSITORY_ID` is the repository name or ID, matching what you'd type
into the Web Access client login dropdown.

The auth flow itself: the server exchanges your username and password for a
bearer token at `/v2/{repository_id}/Token` on the first call, then reuses
the token for subsequent requests in the same session. No client-side state
to manage.

If your environment uses LFDS OAuth instead of the password grant, set
`LF_AUTH_MODE=oauth` and provide `LF_OAUTH_TOKEN_URL`, `LF_CLIENT_ID`, and
`LF_CLIENT_SECRET` instead of the username/password pair. See `.env.example`
in the repo for the complete option list — including `LF_VERIFY_SSL` for
dev environments with self-signed certs, `LF_REQUEST_TIMEOUT_SECONDS`,
`LF_RETRY_ATTEMPTS`, and the pagination limits below.

> **Heads up.** The env block ends up in your Claude Desktop config, which
> is unencrypted on disk. For anything beyond a personal sandbox, point the
> server at a secrets manager or your system keychain rather than pasting
> plaintext credentials into the JSON.

## Connect it to Claude Desktop

Open Claude Desktop's config file. On macOS it lives at
`~/Library/Application Support/Claude/claude_desktop_config.json`; on
Windows, at `%APPDATA%\Claude\claude_desktop_config.json`. If it doesn't
exist yet, create it.

Add an `mcpServers` entry:

```json
{
  "mcpServers": {
    "laserfiche": {
      "command": "uvx",
      "args": ["laserfiche-mcp"],
      "env": {
        "LF_DEPLOYMENT_MODE": "self_hosted",
        "LF_REPO_API_URL": "https://lf.example.com/LFRepositoryAPI",
        "LF_REPOSITORY_ID": "my-repo",
        "LF_AUTH_MODE": "password",
        "LF_USERNAME": "service-account",
        "LF_PASSWORD": "replace-me",
        "LF_READ_ONLY": "true"
      }
    }
  }
}
```

Save the file and quit Claude Desktop fully (Cmd-Q, not just closing the
window). When you reopen it, the server is registered.

You can confirm it loaded by opening the tools panel in the message
composer. If `laserfiche` appears in the list with the eight read tools
beneath it, you're connected. If it doesn't — or shows up red — Claude
Desktop writes its MCP logs to
`~/Library/Logs/Claude/mcp-server-laserfiche.log` on macOS, and that's the
first place to look.

For Claude Code instead of Desktop, the equivalent one-liner is:

```bash
claude mcp add laserfiche -- uvx laserfiche-mcp \
  -e LF_REPO_API_URL=https://lf.example.com/LFRepositoryAPI \
  -e LF_REPOSITORY_ID=my-repo \
  -e LF_USERNAME=service-account \
  -e LF_PASSWORD=replace-me
```

## Test it without Claude first

Before you wire the server to a real client, the
[MCP Inspector](https://github.com/modelcontextprotocol/inspector) is the
fastest way to verify the tool surface end-to-end:

```bash
npx @modelcontextprotocol/inspector uvx laserfiche-mcp
```

Open the URL it prints, click through to the Tools tab, and try
`search_entries` with a query like `{LF:Name="*"}`. If something's wrong
with auth or endpoint paths, you'll see it in the response panel without
having to interpret it through a model's summary.

## Your first query

Open a new chat in Claude Desktop and try a question that touches the
repository. Don't be precious — natural language is fine.

> *Search the `Imports/2024` folder for any entries named `Onboarding*`
> and summarize what you find.*

Claude will plan the work, ask you to approve a call to `search_entries`,
and then — once you click *Allow* — hand the request to the server. The
server constructs the Laserfiche search command, POSTs it to
`/SimpleSearches`, returns the matching entries, and Claude summarizes
the result in prose.

The first time you watch this happen, the satisfying part is what's *not*
there: no plugin to maintain, no separate UI, no copy-pasting into a chat
window. The repository is just another surface Claude can read against,
with the same care you'd give any other read-only access.

> *The model only knows what your tools tell it. Designing the surface —
> what gets exposed, what defaults look reasonable, what stays hidden —
> is most of the work.*

The current tool surface is deliberately small and entirely read-only:

| Tool | Purpose |
|------|---------|
| `search_entries` | Run a Laserfiche search query and return matching entries |
| `search_by_name` | Convenience wrapper that constructs a `{LF:Name=...}` query safely |
| `list_folder` | List the immediate children of a folder by ID |
| `get_entry` | Fetch metadata for a single entry by ID |
| `get_entry_by_path` | Resolve a full repository path (e.g. `\Imports\2024\...`) to an entry |
| `get_field_values` | Read all template field values on an entry |
| `get_document_edoc` | Return electronic-document metadata (size, hint) without the bytes |
| `get_document_text` | Download Laserfiche-extracted text from an electronic document |

There is no general-purpose `write` or `delete` in v1. That's
intentional — records are governed for a reason, and the right way to let
a model mutate them is through deliberate, scoped tools that already have
approvals wired in. Write operations are on the v1.1 roadmap behind
`LF_READ_ONLY=false`.

## Search syntax in 60 seconds

Tools that take a `query` parameter use Laserfiche's native search syntax.
A few common patterns:

```text
{LF:Name="Onboarding*"}                                   # name match
{[Missionary Application]:[Last Name]="Smith"}            # template field
{LF:LookIn="\Imports\2024"}                               # scope to a folder
{LF:Name="*.pdf"} & {[Application]:[Status]="Approved"}   # combined
```

Full reference: [Laserfiche search syntax docs](https://doc.laserfiche.com/laserfiche.documentation/english/clientHelp/Default.htm#cshid=Search%20Syntax).

If you'd rather not learn the syntax to start, use `search_by_name` — it
constructs the right `{LF:Name=...}` query from a plain string and stays
out of your way.

## Where to go next

Three directions, depending on what you're building toward.

### Tighten the result envelope

`LF_MAX_RESULTS_DEFAULT` (default `25`) controls how many results each tool
returns by default; `LF_MAX_RESULTS_CEILING` (default `200`) caps the
absolute maximum no matter what the model requests. If a search routinely
hits the ceiling, narrow the query rather than raising the cap — a model
with 200 entries to reason about is a model that's wasting tokens.

### Going beyond local

Running on your laptop is a fine way to evaluate the integration; it is
not how you'd serve a team. The natural next step is hosting the server
behind a forward-auth proxy that maps end-user identity to Laserfiche
service accounts, so a team can share access without each person managing
their own credentials. There's also a `smithery.yaml` in the repo for
one-click install via [Smithery](https://smithery.ai/).

### Audit and observability

The server emits structured logs for every tool call via the `LF_LOG_LEVEL`
env var (default `INFO`). Pipe those into whatever your audit pipeline
expects. Records governance does not get to have a black box at the edge.

If you build something with this, I'd genuinely like to see it.
[Open an issue](https://github.com/SamuelSHernandez/laserfiche-mcp/issues),
or write to me directly.

### A note on writes

This tutorial leaves `LF_READ_ONLY=true`, which is also the package
default. When you're ready to let Claude create, modify, or delete
entries, flip `LF_READ_ONLY` to `false` and read the **Safety model**
section in the [README](../README.md#safety-model) before you do.
Path-prefix fences (`LF_WRITE_PATHS_ALLOW`), batch caps on folder
deletes, and two-step confirmation tokens on destructive operations are
all available and recommended. The [`docs/error-contract.md`](error-contract.md)
reference documents the structured error responses every tool returns
on failure, so an LLM can branch on the slug instead of parsing prose.

## Further reading

1. [The Model Context Protocol specification](https://modelcontextprotocol.io)
   — the authoritative source for how clients and servers negotiate.
2. [Laserfiche Repository API Server documentation](https://doc.laserfiche.com/)
   — every endpoint the server calls under the hood is documented here.
3. [`.env.example`](../.env.example) — the complete list of configuration
   knobs, including OAuth, SSL verification, retry behavior, pagination
   ceilings, and the write-mode safety guards.
4. [`docs/error-contract.md`](error-contract.md) — the stable
   `mode: "error"` response shape every tool returns on failure, plus
   the full slug taxonomy.
5. [`CHANGELOG.md`](../CHANGELOG.md) — what changed between versions and
   why. v0.1.0 was yanked from PyPI for an incorrect auth flow; v0.2.x is
   the first version verified against a real Laserfiche server. v1.4.0
   is the first to validate the write surface against a live server.
