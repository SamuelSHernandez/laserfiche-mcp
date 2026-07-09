# Laserfiche Assistant — the Claude Desktop extension

Search and read your Laserfiche documents by chatting with Claude. One download, one form — no terminal, no JSON.

## For everyone — install in 3 steps

### 1. Download

You need two things:

- [Claude Desktop](https://claude.ai/download) — the free Claude app for Mac or Windows
- The extension file: [`laserfiche-mcp-2.1.0.mcpb`](https://github.com/SamuelSHernandez/laserfiche-mcp/releases/download/v2.1.0/laserfiche-mcp-2.1.0.mcpb) from the [v2.1.0 release page](https://github.com/SamuelSHernandez/laserfiche-mcp/releases/tag/v2.1.0)

### 2. Double-click & connect

Double-click the downloaded file. Claude Desktop opens an install dialog — click **Install**. A settings form appears. Fill it in:

| Field | What to enter |
|---|---|
| Repository API URL | Your Laserfiche server address, e.g. `https://your-server/LFRepositoryAPI` |
| Repository name | The repository you choose when signing in to Laserfiche Web Access |
| Service account username | A Laserfiche account that can read the repository |
| Service account password | That account's password — kept in your computer's keychain, never a text file |

Leave the other options as they are. If you're not sure about any value, ask whoever manages Laserfiche at your organization.

Click **Save**, and you're connected. It's your repository and your credentials — nothing is shared with anyone else.

> [!NOTE]
> **The first start takes a few seconds.** The extension downloads what it needs to run the first time, so make sure you're online. After that, it starts instantly.

> [!TIP]
> **If the extension shows an error when it starts**, your computer is missing a small helper tool called `uv`. On a Mac, open Terminal and run `brew install uv` — or use the installer at [docs.astral.sh/uv](https://docs.astral.sh/uv/). Then restart Claude Desktop.

### 3. Ask

Open a chat and talk to your repository in plain language.

## What you can ask

- *"Find every invoice from March in the Accounting folder."*
- *"What's in the HR/Onboarding folder? Summarize the newest document."*
- *"Search for contracts that mention Acme and list them with their dates."*
- *"Read the text of the policy document called 'Remote Work' and give me the key points."*

Claude searches, reads, and summarizes — you just ask.

## Is it safe?

- **Claude can look, never touch.** The extension is read-only by default. Claude cannot change, move, or delete anything in your repository unless an administrator deliberately turns that on.
- **Your password stays on your computer.** It's stored in your operating system's keychain — the same protected place your other app passwords live — not in a text file.
- **It runs locally.** The extension runs on your machine and talks directly to your Laserfiche server. There's no middleman service.
- **Your repository, your credentials.** Every person installs their own copy and connects with their own account. Nothing is pooled or shared.

This is a community project under the MIT license, not affiliated with or endorsed by Laserfiche.

## For IT & advanced users

The `.mcpb` is packaging only — it wraps the same `laserfiche-mcp` server the CLI runs, so every tool, guard, and error contract is identical.

### Form fields → environment variables

Each form field maps to the same `LF_*` environment variable the CLI path uses (`config.py`). Everything not in the form falls back to `config.py` defaults.

| Form field | Environment variable | Default |
|---|---|---|
| Repository API URL | `LF_REPO_API_URL` | — (base URL, no trailing `/v1` path) |
| Repository name | `LF_REPOSITORY_ID` | — |
| Service account username | `LF_USERNAME` | — |
| Service account password | `LF_PASSWORD` (OS keychain) | — |
| API version | `LF_API_VERSION` | `v1` |
| Read-only mode | `LF_READ_ONLY` | on (`true`) |
| Verify TLS certificate | `LF_VERIFY_SSL` | on (`true`) |

### Runtime mechanics

Claude Desktop launches the server via the `uv` runtime, which it provides. If the extension errors on start, the fix is to install `uv` yourself (`brew install uv`, or the installer at [docs.astral.sh/uv](https://docs.astral.sh/uv/)). First launch resolves the Python dependencies from PyPI, so it needs network access and takes a few extra seconds; later starts use the cached environment.

### Authentication

The form covers the self-hosted **password grant** (`LF_AUTH_MODE=password`) — the server exchanges the username/password for a bearer token and refreshes it automatically. If your environment uses **OAuth-based Laserfiche auth**, use the classic config path (`claude_desktop_config.json` / `.env`) with the `LF_*` OAuth variables instead of the extension form.

### Alternative installs

```bash
pip install laserfiche-mcp    # classic install
uvx laserfiche-mcp            # run directly, no install
```

For web clients (claude.ai custom connectors, ChatGPT) the same server runs over Streamable HTTP — `laserfiche-mcp --http` — with optional per-user OAuth. Deployment and security details live in [docs/remote-http.md](remote-http.md); don't expose `--http` to a network without reading it.

### Rebuilding the `.mcpb` from source

The build files (`manifest.json`, `mcp_entry.py`, `.mcpbignore`) live in the repo. With Node available:

```bash
./scripts/build-extension.sh
```

The script validates `manifest.json` and packs the project into `dist/` with the `@anthropic-ai/mcpb` CLI. Attach the resulting `.mcpb` to a GitHub release. End users don't need Node — only the build does.

### Limitations

- **Password auth only in the form.** OAuth environments use the classic config path (above).
- **Fine-grained write controls aren't in the form.** The form has a **Read-only** toggle (on by default); turning it off enables the write tools with their *default* guardrails. The finer controls — path fences, write-tool allowlists, audit-reason requirements, delete caps — are only configurable via `LF_*` environment variables in the classic config path. Read the [Safety model](../README.md#safety-model) before turning read-only off.
- **First launch needs internet** to fetch dependencies. Fully offline machines should use a pre-provisioned environment instead.
