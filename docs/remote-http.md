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

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `LF_HTTP_HOST` | `127.0.0.1` | Loopback by default. `0.0.0.0` exposes it to the network — opt in deliberately. |
| `LF_HTTP_PORT` | `8000` | 1–65535. |
| `LF_HTTP_PATH` | `/mcp` | Must start with `/`. |
| `LF_HTTP_AUTH_TOKEN` | *(unset)* | Bearer token. Unset = unauthenticated (only acceptable on loopback). |

CLI overrides for a single run: `--host`, `--port`. All the usual `LF_*`
repository/auth settings still apply — the HTTP layer sits in front of the same
Laserfiche client.

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

- [ ] `LF_HTTP_AUTH_TOKEN` is set to a long random value.
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

- **Single shared secret, not per-user auth.** `LF_HTTP_AUTH_TOKEN` is one token
  for all callers. There is no per-user OAuth / identity mapping yet — every
  request authenticates as the one configured Laserfiche service account.
  Full OAuth (FastMCP supports a `token_verifier` / auth provider) is the next
  step for a multi-user production connector.
- **No built-in rate limiting.** Put it behind a proxy that provides this if
  exposed.
- **Stateful sessions.** The Streamable HTTP transport keeps per-session state;
  a single long-lived process is assumed (no horizontal scaling without a shared
  session/event store).
