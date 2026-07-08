# The one-click Desktop Extension

*For administrators who want the value of `laserfiche-mcp` without touching a
terminal, a JSON config file, or a single environment variable.*

The server also ships as a **Claude Desktop Extension** — a single `.mcpb` file
an admin double-clicks to install. Instead of hand-editing
`claude_desktop_config.json`, they fill in a native settings form (Repository
URL, service account, password) and click **Save**. The password is stored in
the operating system's keychain rather than a plaintext file, and the server
starts read-only by default.

This is *packaging only* — it wraps the exact same server the CLI path runs, so
every safety guard, tool, and error contract is unchanged.

## What the administrator does

1. Download `laserfiche-mcp.mcpb` (from your GitHub Releases, an email, a shared
   drive — no app store, no signing).
2. Double-click it, or in Claude Desktop go to **Settings → Extensions →
   Install extension**.
3. Fill in the form:
   - **Repository API URL** — e.g. `https://lf.example.com/LFRepositoryAPI`
   - **Repository name** — the one from the Web Access login
   - **Service account username** and **password** (kept in the OS keychain)
   - **Read-only mode** — leave on (recommended)
   - **API version** — leave `v1` unless the server is a newer v2 build
4. Click **Save**. Claude Desktop launches the server on its bundled `uv`
   runtime — the user needs no Python, no `uv`, and no Node installed.

That's it. Open a chat and ask something like *"search the Imports/2024 folder
for entries named Onboarding\* and summarize what you find."*

## Building the bundle (maintainer)

You need Node (for the `mcpb` CLI). The bundle itself does **not** — end users
run it on Claude Desktop's bundled `uv`.

```bash
./scripts/build-extension.sh
```

This validates `manifest.json`, then packs the project into
`dist/laserfiche-mcp.mcpb`. Attach that file to a GitHub Release.

Under the hood the script runs:

```bash
npx @anthropic-ai/mcpb validate manifest.json
npx @anthropic-ai/mcpb pack . dist/laserfiche-mcp.mcpb
```

`.mcpbignore` keeps the bundle to just the source `uv` needs (`src/`,
`pyproject.toml`, `uv.lock`, `mcp_entry.py`, `README.md`, `LICENSE`); tests,
docs, caches, and the virtualenv are excluded.

## How it maps to the CLI config

The settings form is defined by `user_config` in `manifest.json`; each field is
passed to the server as the same `LF_*` environment variable the CLI uses
(`config.py`). The form intentionally exposes only the fields most admins need —
everything else (pagination limits, retry, timeouts, the write-mode fences)
falls back to the defaults in `config.py`. A power user can still add any `LF_*`
override the classic way.

| Form field                | Environment variable   |
|---------------------------|------------------------|
| Repository API URL        | `LF_REPO_API_URL`      |
| Repository name           | `LF_REPOSITORY_ID`     |
| Service account username  | `LF_USERNAME`          |
| Service account password  | `LF_PASSWORD` (keychain) |
| API version               | `LF_API_VERSION`       |
| Read-only mode            | `LF_READ_ONLY`         |
| Verify TLS certificate    | `LF_VERIFY_SSL`        |

`LF_DEPLOYMENT_MODE=self_hosted` and `LF_AUTH_MODE=password` are set for you.

## Limitations & notes

- **Password (username/password) auth only.** The form covers the default
  self-hosted password grant. If your environment uses **LFDS OAuth**, use the
  classic `claude_desktop_config.json` / `.env` path documented in
  [getting-started.md](getting-started.md) — set `LF_AUTH_MODE=oauth` with the
  `LF_CLIENT_ID` / `LF_CLIENT_SECRET` / `LF_OAUTH_TOKEN_URL` trio.
- **First launch fetches dependencies.** `uv` resolves the pure-Python deps
  (`mcp`, `httpx`, `pydantic`, `pypdf`) from PyPI on first run, so the first
  start needs network access. Fully offline laptops should use a pre-provisioned
  environment instead.
- **Enabling writes** is possible by turning off *Read-only mode*, but the path
  fences and tool allowlist that make writes safe are not (yet) surfaced in the
  form — configure those via `LF_*` env if you need a scoped write deployment.
  Read the Safety model in the [README](../README.md#safety-model) first.
