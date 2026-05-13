# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.1.0] - 2026-05-13

### Added
- LFRepositoryAPI v1 support alongside v2 (`LF_API_VERSION`, default `v1`). Most current on-prem installations expose the v1 routing surface (`/v1/` paths, lowercase `fields`/`children`, OData entity-type segments, no unified `/Export`); v2-only routing was rejecting every call. Auth's password-grant token endpoint, `build_repo_path`, `list_folder`, `get_field_values`, and `export_entry` now all route on the configured version. *(Shipped as the prior commit on this branch.)*
- `search_natural` tool — two-mode guided search. Mode A (no `lf_query`) samples up to ten entries from `folder_path`, returns the templates and field names actually present, the Laserfiche search-syntax grammar reference, and 2–3 candidate query strings for the host LLM to pick from or refine. Mode B (`lf_query` set) executes the query and, on HTTP 400, performs up to two automatic repairs (escape unescaped `"` inside `="..."` values, then wildcard-wrap bare `Name=` values when `fuzzy=True`) before returning a structured error with every attempt visible. Pagination surfaces `pagination_unknown=true` when the result count hits the cap but the server returned no continuation link.
- `get_document_edoc(mode=...)` — adds `bytes` (base64 + content-type, capped by `LF_EDOC_MAX_BYTES`, default 25 MB) and `text` (server-side extraction; PDFs via `pypdf`, `text/*` decoded directly, anything else returns a structured "use mode=bytes" error) alongside the original `info` mode. Gives v1 servers a path to document text now that `get_document_text` has no endpoint to call.
- `LF_MAX_PAGE_SIZE` env var (default 100) — dedicated `search_natural` page-size cap, separate from `LF_MAX_RESULTS_CEILING`. Some self-hosted SimpleSearches implementations reject `$top` above an internal limit, so this defaults lower.
- `LF_EDOC_MAX_BYTES` env var (default 25 MB) — caps edoc downloads when `mode` is `bytes` or `text`. Override per-call via the `max_bytes` argument.
- `LaserficheClient.export_entry_with_meta()` — returns `(bytes, content_type)` so callers can branch on document type instead of trusting the entry's extension.
- `pypdf` runtime dependency for server-side PDF text extraction.

### Changed
- `get_document_text` docstring now points v1 users to `get_document_edoc(mode="text")`.
- Startup log message no longer warns that `LF_READ_ONLY` "has no effect yet" — that warning was misleading. The flag remains reserved for future write tools; the startup line just records the configured value.

### Tests
- New PDF fixtures committed at `tests/fixtures/sample_text.pdf` and `tests/fixtures/sample_encrypted.pdf` (with a regeneration script at `tests/fixtures/_generate.py`). Text-extraction tests now assert specific extracted content rather than just response shape — earlier blank-page PDFs would have let a pypdf regression ship silently. New tests cover the encrypted-PDF, malformed-PDF, mixed-case content-type, and char-limit truncation paths.
- New opt-in integration target at `tests/test_integration.py`, marked with `pytest.mark.integration` and gated behind `LF_INTEGRATION_TEST=1`. Reads the same `LF_*` config the server uses at runtime; covers `search_natural` Mode A guidance, Mode B structured-outcome contract, and `get_document_edoc(mode="info"|"text")` against a real PDF entry (configurable via `LF_INTEGRATION_FOLDER_PATH` and `LF_INTEGRATION_PDF_ENTRY_ID`).
- `pytest-cov` added as a dev dep with a baseline of 80% (current measured 85% branch coverage; threshold leaves a small regression buffer).

### Fixed
- N/A (v1.1.0 is purely additive; existing tools keep their contracts.)

## [0.2.1] - 2026-05-10

### Changed
- `Development Status` classifier bumped from Alpha to Beta to reflect the verified-against-reference state of the package.
- `pyproject.toml`: added `Typing :: Typed`, `Topic :: Communications`, and `Topic :: Software Development :: Libraries :: Python Modules` classifiers; declared Python 3.13 support (CI tests it); expanded `[project.urls]` to include Repository, Documentation, and Changelog.
- `main()` now prints a clean, actionable error and exits with code 2 when LF_* env vars are missing — no Python traceback. Ctrl-C exits cleanly without a traceback as well.

### Added
- `--help` and `--version` CLI flags so first-time users running `uvx laserfiche-mcp` directly can discover the package without needing env config.
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

[Unreleased]: https://github.com/SamuelSHernandez/laserfiche-mcp/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/SamuelSHernandez/laserfiche-mcp/compare/v0.2.1...v1.1.0
[0.2.1]: https://github.com/SamuelSHernandez/laserfiche-mcp/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/SamuelSHernandez/laserfiche-mcp/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/SamuelSHernandez/laserfiche-mcp/releases/tag/v0.1.0
