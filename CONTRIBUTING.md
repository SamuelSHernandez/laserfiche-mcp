# Contributing

Thanks for your interest. This is a small project; most contributions land
in one or two PRs without much process.

## Development setup

```bash
git clone https://github.com/SamuelSHernandez/laserfiche-mcp
cd laserfiche-mcp
uv sync --extra dev
```

## Tests, lint, type-check

Every PR should leave these clean:

```bash
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
uv run mypy src
```

CI runs the same commands across Python 3.10–3.13 on every push and
pull request.

### Opt-in integration tests

The default suite mocks the Repository API. If you have a reachable
repository, also run the integration tests before tagging a release — they
catch what mocks can't (server-side query quirks, real PDF extraction,
transport-level rejections):

```bash
LF_INTEGRATION_TEST=1 uv run pytest tests/test_integration.py
```

They read the same `LF_*` variables the server uses. Optional overrides:

- `LF_INTEGRATION_FOLDER_PATH` — folder for the `search_natural` test (default: repository root)
- `LF_INTEGRATION_PDF_ENTRY_ID` — a known PDF entry; if unset, the edoc tests skip
- `LF_INTEGRATION_SAFE_QUERY` — a query that returns results on your repo (default: `{LF:Name="*"}`)

### Windows line-ending note

The repo's [`.gitattributes`](.gitattributes) normalizes all text
files to LF. `ruff format --check` is configured `line-ending = "lf"`
and will flag every file as "would reformat" if your working tree
has CRLF endings.

If you cloned the repo *before* `.gitattributes` was added (or if your
clone shows CRLF on disk despite the attribute), run once:

```bash
git rm --cached -r .
git reset --hard
```

That re-checks-out every tracked file with the attributes applied. No
content change — just line endings.

## Where help is most welcome

- **Real-server confirmation.** Several features are implemented and
  unit-tested but have never run against the real thing:
  - Laserfiche **Cloud** auth (`LF_AUTH_MODE=api_key`) against a live tenant.
  - True on-behalf-of auth (`LF_AUTH_MODE=oauth_passthrough`, see
    [`docs/remote-http.md`](docs/remote-http.md)) against a live LFDS tenant.
  - **Repository API v2** servers — the v2 wire format (for example the
    Export download pointer) is covered by mocks and a fake server only.
- **Endpoint corrections** for Repository API Server builds the v1 / v2
  wire format hasn't been validated against.
- **Server-side audit logging** for write-mode deployments (sidecar file +
  rotation).
- **Text extraction** for more document formats (`ops/extract.py`).
- Open follow-ups are tracked in [`docs/internal/TODO.md`](docs/internal/TODO.md).

## PR expectations

- Tests for new behavior. Mocked HTTP via `pytest-httpx` is the established
  pattern — see `tests/test_client.py`.
- Match the convention in [`models.py`](src/laserfiche_mcp/models.py) when
  adding endpoints: each model has a `from_api(raw)` classmethod that
  tolerates camelCase + PascalCase keys via the `_pick` helper.
- Tool descriptions read like prompts — see existing tools in
  [`src/laserfiche_mcp/tools/`](src/laserfiche_mcp/tools/) (e.g.
  [`reads.py`](src/laserfiche_mcp/tools/reads.py),
  [`documents.py`](src/laserfiche_mcp/tools/documents.py)) for the tone.
  Each tool docstring should have Args, Returns, and On failure sections.
- Update [`CHANGELOG.md`](CHANGELOG.md) under `[Unreleased]`.

## Commit messages

Conventional-ish (`fix:`, `feat:`, `docs:`, `chore:`, `ci:`) — not strictly
enforced, but it keeps `git log` scannable.

## Reporting bugs

Use [GitHub Issues](https://github.com/SamuelSHernandez/laserfiche-mcp/issues).
Include: Laserfiche server version, repository deployment (self-hosted vs
cloud), the tool that failed, and the response payload (with credentials
redacted).

## Reporting security issues

**Don't file public issues** — see [SECURITY.md](SECURITY.md) for the
private disclosure process.

## License

By contributing, you agree your contributions are licensed under the same
MIT license as the project.
