# Security Policy

## Supported versions

Only the latest minor release receives security fixes.

| Version  | Supported          |
| -------- | ------------------ |
| 0.2.x    | :white_check_mark: |
| < 0.2    | :x: (yanked from PyPI) |

## Reporting a vulnerability

**Please do not file public issues for security vulnerabilities.**

The preferred channel is GitHub's private vulnerability reporting. Click the
**"Report a vulnerability"** button on the
[Security tab](https://github.com/SamuelSHernandez/laserfiche-mcp/security)
of this repository.

If you can't use GitHub's reporting flow, email
**samuelhernandezyepez@gmail.com** with `[laserfiche-mcp security]` in the
subject line.

### Response targets

- **Acknowledgement**: within 7 days of report.
- **Fix or mitigation**: a patched release within 30 days for confirmed,
  in-scope vulnerabilities. Coordinated disclosure timeline negotiated for
  anything that doesn't fit that window.

### In scope

- Credential handling and exposure (e.g. ways `LF_PASSWORD` could leak via
  logs, tracebacks, or error messages).
- Bypass of the `LF_READ_ONLY` safety flag.
- SSRF or URL-injection risks in how `LF_REPO_API_URL` is composed into
  request URLs.
- Authentication bypass against the Laserfiche `/Token` endpoint (e.g.
  token reuse across users, refresh handling).
- Dependency vulnerabilities that materially affect this package's security
  posture.

### Out of scope

- Vulnerabilities in the upstream **Laserfiche Repository API** itself —
  please report those to Laserfiche directly.
- Misconfigurations that depend on the user explicitly disabling safety
  controls (e.g. `LF_VERIFY_SSL=false`, `LF_READ_ONLY=false`).
- Issues in third-party MCP clients (Claude Desktop, MCP Inspector, etc.).
- Findings against `.env.example` placeholder values.
