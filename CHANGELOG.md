# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.1] - 2026-05-09

### Changed
- `Development Status` classifier bumped from Alpha to Beta to reflect the verified-against-reference state of the package.
- `pyproject.toml`: added `Typing :: Typed`, `Topic :: Communications`, and `Topic :: Software Development :: Libraries :: Python Modules` classifiers; declared Python 3.13 support (CI tests it); expanded `[project.urls]` to include Repository, Documentation, and Changelog.

### Added
- `py.typed` marker file (PEP 561) so downstream type checkers recognize the package as typed.
- `CHANGELOG.md`, `SECURITY.md`, `CONTRIBUTING.md`.
- Status badges in README (PyPI version, Python versions, CI, license, MCP).
- `.github/dependabot.yml` for weekly grouped dependency updates.
- `.github/workflows/release.yml` — auto-publish to PyPI on `v*` tag via OIDC trusted publishing.
- `smithery.yaml` — manifest for one-click install on Smithery.

## [0.2.0] - 2026-05-08

### Changed (breaking)
- **Auth flow**: HTTP Basic auth replaced with the password-grant token exchange Laserfiche actually requires. The server now POSTs username/password to `/v2/{repository_id}/Token` and uses the returned bearer for subsequent calls. `LF_AUTH_MODE=basic` renamed to `LF_AUTH_MODE=password`.
- **Endpoint paths** corrected against the official `Laserfiche/lf-repository-api-client-java` reference:
  - `list_folder`: `/Entries/{id}/Children` → `/Entries/{id}/Folder/Children`.
  - `search_entries`: `GET /Entries/SearchEntries?...` → `POST /SimpleSearches` with JSON body `{"searchCommand": "..."}`.
  - Document download: `GET /Entries/{id}/Edoc` → `POST /Entries/{id}/Export` with body `{"part": "Edoc" | "Text" | "Image"}`.
- `get_document_text` now requests Laserfiche-extracted text rather than raw bytes.

### Added
- `get_document_edoc` tool returning metadata only (size + hint), so the model never gets raw binary in its context window.
- `search_by_name` convenience tool that constructs `{LF:Name=...}` queries safely.
- `OAuthClientCredentialsStrategy` for LFDS-compatible OAuth providers.
- `LF_VERIFY_SSL` knob with a runtime warning when disabled.
- Retry on transient 5xx/429/connection errors with exponential backoff (`LF_RETRY_ATTEMPTS`, default 3).
- `LF_LOG_LEVEL` env var.
- `SecretStr` for `password`, `client_secret`, and `api_key` so credentials don't leak via `repr` or tracebacks.
- `_pick(raw, *keys)` helper that distinguishes missing keys from explicitly false/zero/empty values; regression-tested.
- 43 tests across config, models, auth, client (incl. retry/4xx-no-retry), and server helpers.

### Yanked
- v0.1.0 was yanked from PyPI on 2026-05-09 because the auth flow and several endpoint paths were incorrect against any real Laserfiche server.

## [0.1.0] - 2026-05-08

Initial public release. **Yanked** — see v0.2.0 for the correct auth flow and endpoint paths.

[Unreleased]: https://github.com/SamuelSHernandez/laserfiche-mcp/compare/v0.2.1...HEAD
[0.2.1]: https://github.com/SamuelSHernandez/laserfiche-mcp/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/SamuelSHernandez/laserfiche-mcp/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/SamuelSHernandez/laserfiche-mcp/releases/tag/v0.1.0
