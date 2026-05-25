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

- **Endpoint corrections** for Repository API Server builds the v1 / v2
  wire format hasn't been validated against. The current wire format is
  exercised against a live v1 server; v2-build divergences are still
  possible.
- **Laserfiche Cloud client** — needs the JWT-signed `client_credentials`
  assertion flow that `signin.laserfiche.com` requires, plus the
  `api.laserfiche.com` v2-only endpoint surface.
- **v2.x follow-ups** deferred from the v2.0 audit (see
  [`docs/internal/TODO.md`](docs/internal/TODO.md)): write-tool collapses
  (`field_update(mode)`, `tag_update(add, remove)`, ...), preview/execute
  splits of the 5 destructive tools, structured JSON logging
  (`LF_LOG_FORMAT=json`) with a `redact()` helper, and parameter-
  description polish so docs flow into the JSON schema the LLM sees.
- **Server-side audit logging** for write-mode deployments (sidecar
  file + rotation).
- **Async-search support** for result sets larger than the
  SimpleSearches endpoint can return synchronously.

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
