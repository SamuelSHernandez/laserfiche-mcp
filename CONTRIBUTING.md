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
uv run mypy src
```

CI runs the same three commands across Python 3.10–3.13 on every push and
pull request.

## Where help is most welcome

- **Endpoint corrections** for older Repository API Server versions (v0.2
  targets V2; older V1 paths sometimes differ).
- **Laserfiche Cloud client** — needs the JWT-signed `client_credentials`
  assertion flow that `signin.laserfiche.com` requires.
- **Write tools** (`update_field_values`, `move_entry`, rename) gated
  behind `LF_READ_ONLY=false`.
- **Async-search support** for result sets larger than the SimpleSearches
  endpoint can return synchronously.

## PR expectations

- Tests for new behavior. Mocked HTTP via `pytest-httpx` is the established
  pattern — see `tests/test_client.py`.
- Match the convention in [`models.py`](src/laserfiche_mcp/models.py) when
  adding endpoints: each model has a `from_api(raw)` classmethod that
  tolerates camelCase + PascalCase keys via the `_pick` helper.
- Tool descriptions read like prompts — see existing tools in
  [`server.py`](src/laserfiche_mcp/server.py) for the tone.
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
