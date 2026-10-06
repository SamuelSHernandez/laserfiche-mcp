# Remote HTTP transport — deployment & security

The default `laserfiche-mcp` transport is **stdio**: a local client launches the
server as a subprocess and talks to it over stdin/stdout. That covers every
local MCP client — Claude Desktop, Claude Code, Cursor, VS Code, Zed, Gemini
CLI — and keeps credentials on the user's machine.

**Web and cloud clients can't spawn a process.** claude.ai custom connectors and
ChatGPT connectors connect to a URL. To serve them, run the same server over
**Streamable HTTP**:

```bash
laserfiche-mcp --http
```

This document covers running that mode safely. If you only need local clients,
you don't need any of this — use the stdio setup in the main README.

## What `--http` does

- Serves the identical tool set over the MCP **Streamable HTTP** transport.
- Binds `LF_HTTP_HOST:LF_HTTP_PORT` (default `127.0.0.1:8000`) at path
  `LF_HTTP_PATH` (default `/mcp`).
- Optionally enforces a static bearer token (`LF_HTTP_AUTH_TOKEN`) on every
  request, returning `401 {"error":"unauthorized"}` when it's missing or wrong
  (constant-time compared).
- Speaks **plain HTTP**. TLS is expected to be terminated by a reverse proxy in
  front of it.

## Authentication modes

The `--http` server picks its auth mode by precedence: **OAuth** (per-user) if
`LF_HTTP_OAUTH_ISSUER` is set, else **static token** if `LF_HTTP_AUTH_TOKEN` is
set, else **none** (only acceptable on loopback).

| Mode | When | What it does |
|---|---|---|
| **OAuth (per-user)** | `LF_HTTP_OAUTH_ISSUER` set | Verifies each caller's bearer token against an external authorization server. Real per-user authentication at the edge. See [OAuth Resource Server](#oauth-resource-server-per-user-auth) below. |
| **Static token** | `LF_HTTP_AUTH_TOKEN` set | One shared secret checked on every request. Fine for a single-tenant deployment or a spike. |
| **None** | neither set | No auth. Loopback only — a warning is logged if bound off-loopback. |

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `LF_HTTP_HOST` | `127.0.0.1` | Loopback by default. `0.0.0.0` exposes it to the network — opt in deliberately. |
| `LF_HTTP_PORT` | `8000` | 1–65535. |
| `LF_HTTP_PATH` | `/mcp` | Must start with `/`. |
| `LF_HTTP_AUTH_TOKEN` | *(unset)* | Static bearer token. Ignored when OAuth is enabled. |
| `LF_HTTP_OAUTH_ISSUER` | *(unset)* | Authorization-server issuer URL. Setting it turns on OAuth Resource Server mode. |
| `LF_HTTP_PUBLIC_URL` | *(unset)* | Public HTTPS URL incl. path (e.g. `https://lf.example.com/mcp`). Required with OAuth. |
| `LF_HTTP_OAUTH_AUDIENCE` | *(= public URL)* | Expected `aud` claim — this server's identifier in the IdP. |
| `LF_HTTP_OAUTH_JWKS_URL` | *(discovered)* | Explicit JWKS URL; else discovered from the issuer's OpenID config. |
| `LF_HTTP_OAUTH_REQUIRED_SCOPES` | *(none)* | Comma/space-separated scopes a token must carry. |
| `LF_HTTP_OAUTH_ALGORITHMS` | `RS256` | Allowed JWT algorithms. Asymmetric only — HMAC/`none` are rejected. |

CLI overrides for a single run: `--host`, `--port`. All the usual `LF_*`
repository/auth settings still apply — the HTTP layer sits in front of the same
Laserfiche client.

## OAuth Resource Server (per-user auth)

Install the extra and point the server at your authorization server:

```bash
pip install 'laserfiche-mcp[oauth]'

LF_REPO_API_URL=... LF_REPOSITORY_ID=... LF_USERNAME=... LF_PASSWORD=... \
LF_HTTP_OAUTH_ISSUER="https://login.microsoftonline.com/<tenant>/v2.0" \
LF_HTTP_PUBLIC_URL="https://lf.example.com/mcp" \
LF_HTTP_OAUTH_AUDIENCE="api://laserfiche-mcp" \
LF_HTTP_OAUTH_REQUIRED_SCOPES="laserfiche.read" \
  laserfiche-mcp --http --host 0.0.0.0
```

In this mode the server is an **OAuth 2.1 Resource Server**:

1. It serves protected-resource metadata (RFC 9728) at
   `/.well-known/oauth-protected-resource<path>`, advertising your authorization
   server. Unauthenticated requests get `401` with a `WWW-Authenticate` header
   pointing there.
2. The client (claude.ai / ChatGPT) discovers your authorization server, runs
   the standard `authorization_code` + PKCE flow, and presents the resulting
   bearer token.
3. This server verifies the token's signature (against the issuer's JWKS),
   `aud`, `iss`, `exp`, and any required scopes. Valid → the request proceeds;
   anything else → `401`.

**By default this is authentication, not delegation.** A verified user is
allowed to use the connector; the Laserfiche calls themselves still run as the
configured service account (`LF_USERNAME` / OAuth `client_credentials`).
Laserfiche's own audit trail therefore shows the service account, not the end
user. See **True on-behalf-of** below for the opt-in that changes this.

You must register the web client in your IdP: allow claude.ai's redirect URI (or
enable dynamic client registration), and expose a scope matching
`LF_HTTP_OAUTH_REQUIRED_SCOPES` / an audience matching `LF_HTTP_OAUTH_AUDIENCE`.

### True on-behalf-of (`LF_AUTH_MODE=oauth_passthrough`) — beta

Add `LF_AUTH_MODE=oauth_passthrough` on top of the OAuth Resource Server setup
above, and this server stops using a shared service account entirely: every
Laserfiche call reuses the calling user's own already-verified bearer token.
No `LF_USERNAME`/`LF_PASSWORD` or other static credential is needed or used.

```bash
LF_REPO_API_URL=... LF_REPOSITORY_ID=... LF_AUTH_MODE=oauth_passthrough \
LF_HTTP_OAUTH_ISSUER="https://your-lfds.example.com" \
LF_HTTP_PUBLIC_URL="https://lf.example.com/mcp" \
LF_HTTP_OAUTH_AUDIENCE="api://laserfiche-mcp" \
  laserfiche-mcp --http --host 0.0.0.0
```

**Whether this actually grants per-user Laserfiche access depends entirely on
your identity provider:**

- If `LF_HTTP_OAUTH_ISSUER` is Laserfiche's own **LFDS**, and the tokens it
  issues carry an audience/resource already valid for the Repository API, the
  forwarded token works as-is — Laserfiche's own audit trail shows the calling
  user, and its own ACLs apply per-user. This is genuine on-behalf-of.
- If `LF_HTTP_OAUTH_ISSUER` points at a *different* IdP (Entra, Okta, Auth0,
  Google) — the common case when it's only gating who may use the connector —
  that token authenticates fine at the MCP server but Laserfiche will reject
  it outright on the first real call. Passthrough mode cannot invent
  Laserfiche permissions the caller's token was never granted; use the
  service-account modes instead in that setup.

There is deliberately no fallback to the service account when a passthrough
call fails — a silent fallback would defeat the point (every call would
quietly run as the shared account again after all), so a rejected or missing
token surfaces as a clear tool error instead. **Never verified against a live
LFDS tenant** — if you try it, please open an issue with what worked or broke,
including whether your LFDS token's audience needed a token-exchange step
first.

### Reverse proxies and the `Host` header

The server rejects requests whose `Host` header isn't a loopback name or the
host of `LF_HTTP_PUBLIC_URL` (`421 Invalid Host header`) — this is MCP's
DNS-rebinding protection. Set `LF_HTTP_PUBLIC_URL` to the exact public URL
clients use and make sure your proxy forwards the original `Host` header
(nginx: `proxy_set_header Host $host;`).

## Unattended agents

The `--http` transport doesn't distinguish a human's chat client from an
autonomous agent's tool-calling loop — same OAuth flow, same tools. What
should differ is what each is *allowed* to do unsupervised. Register a
separate OAuth client for your agent (`client_credentials` grant, no human
present to click through `authorization_code`), and withhold whichever scope
you assign to `LF_HTTP_OAUTH_DESTRUCTIVE_SCOPE` from that client's grant.
The agent can still call `delete_entry`/`delete_edoc`/`delete_pages` to get
a **preview** — useful for it to reason about and surface to a human — but
executing refuses with `destructive_scope_required` until a human's own
token (which does carry the scope) makes the actual call. `move_entry` and
`rename_entry` are unaffected by this gate: they're reversible, so an
unattended agent can execute those on its own.

```bash
LF_HTTP_OAUTH_DESTRUCTIVE_SCOPE="laserfiche.destructive"   # only humans get this scope
```

This is on top of, not instead of, scoping the agent's own deployment with
`LF_WRITE_TOOLS_ALLOWED` / `LF_READ_ONLY` — see [Safety model](safety.md).

## Local verification

```bash
# Terminal 1
LF_REPO_API_URL=... LF_REPOSITORY_ID=... LF_USERNAME=... LF_PASSWORD=... \
  laserfiche-mcp --http

# Terminal 2 — MCP Inspector, connect to http://127.0.0.1:8000/mcp
npx @modelcontextprotocol/inspector
```

Or drive the handshake by hand:

```bash
curl -sS -X POST http://127.0.0.1:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize",
       "params":{"protocolVersion":"2025-06-18","capabilities":{},
                 "clientInfo":{"name":"curl","version":"0"}}}'
```

With `LF_HTTP_AUTH_TOKEN` set, add `-H "Authorization: Bearer <token>"` — without
it the same request returns `401`.

## Connecting claude.ai / ChatGPT

Both need a **public HTTPS URL**. Two common paths:

1. **Spike / demo** — tunnel loopback to a temporary public URL:
   ```bash
   laserfiche-mcp --http --port 8000 &
   cloudflared tunnel --url http://127.0.0.1:8000    # or: ngrok http 8000
   ```
   Add the resulting `https://…/mcp` as a custom connector in the client's
   settings. Set `LF_HTTP_AUTH_TOKEN` even for a spike — the tunnel is public.

2. **Real deployment** — run the server on a host that can reach your Laserfiche
   server, behind a reverse proxy (nginx / Caddy / cloud LB) that terminates TLS
   and forwards to `127.0.0.1:8000`. Point the connector at `https://your-host/mcp`.

## Security checklist before exposing to a network

- If writes are enabled, set `LF_IMPORT_SOURCE_DIRS`; unset, `import_document` can read any file the server process can.

- [ ] An auth mode is configured: OAuth (`LF_HTTP_OAUTH_ISSUER`) for multi-user,
      or at least a long random `LF_HTTP_AUTH_TOKEN` for single-tenant.
- [ ] TLS is terminated by a reverse proxy; the server itself stays on loopback
      and the proxy forwards to it (don't bind `0.0.0.0` with plain HTTP on the
      open internet).
- [ ] The host has a controlled **network path** to the self-hosted Laserfiche
      server (VPN / DMZ / private network) — you are bridging an on-prem system
      to a cloud client.
- [ ] `LF_READ_ONLY=true` unless write access is genuinely required (see the
      Safety model in the README).
- [ ] Logs are monitored — every tool call is logged with a request id and
      redacted args (`LF_LOG_FORMAT=json` for structured forwarding).

## Known limitations

- **Edge auth by default, not on-behalf-of.** OAuth mode authenticates the
  *user at the connector*, but Laserfiche operations still run as the shared
  service account by default, so Laserfiche's audit trail shows that account
  rather than the end user. `LF_AUTH_MODE=oauth_passthrough` (see above) opts
  into real on-behalf-of, but it's beta and its success depends on whether
  your LFDS/IdP issues tokens whose audience is already valid against the
  Repository API — some tenants will need a token-exchange step this server
  doesn't implement yet.
- **No built-in rate limiting.** Put it behind a proxy that provides this if
  exposed.
- **Stateful sessions.** The Streamable HTTP transport keeps per-session state;
  a single long-lived process is assumed (no horizontal scaling without a shared
  session/event store).
