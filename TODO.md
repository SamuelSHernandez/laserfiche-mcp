# TODO

Working notes for `laserfiche-mcp`. Not a public roadmap — see
[`CHANGELOG.md`](CHANGELOG.md) for what's shipped and the README's
**Roadmap** section for what's promised to users.

Items are grouped by category and rough priority. Move items to
CHANGELOG once they ship.

---

## v2.0 follow-ups (deferred from the three-pass refactor)

Pass 1 / Pass 2 / Pass 3 produced six artifacts at the repo root
(`AUDIT.md`, `AUDIT_ERRORS.md`, `AUDIT_DESIGN.md`, `PLAN.md`,
`PLAN_ERRORS.md`, `PLAN_DESIGN.md`). v2.0 shipped the highest-impact
half; the items below are the explicit deferrals.

### Write-tool collapses (PLAN.md step 3)
- `laserfiche_field_update(updates, mode)` combining `set_fields` +
  `merge_fields` with `Literal["merge","replace"]` enum.
- `laserfiche_tag_update(add, remove)` combining `set_tags` + `merge_tags`.
- `laserfiche_link_update(links, mode)` (only `set_links` exists today;
  add the merge variant).
- `laserfiche_template_assign(template_name=None)` collapsing
  `assign_template` + `remove_template` (None clears).
- `laserfiche_task_wait(timeout_seconds=0)` collapsing
  `wait_for_task` + `get_task_status` (timeout=0 returns immediately).

### Preview/execute splits (PLAN.md step 4)
Split each of the 5 destructive multiplex tools into two
single-purpose tools:
- `laserfiche_entry_rename_preview` / `..._execute`
- `laserfiche_entry_move_preview` / `..._execute`
- `laserfiche_entry_delete_preview` / `..._execute`
- `laserfiche_document_edoc_delete_preview` / `..._execute`
- `laserfiche_document_pages_delete_preview` / `..._execute`

### Description polish (PLAN_DESIGN.md step 6)
- Move parameter descriptions from docstrings into
  `Field(description=...)` so they reach the JSON schema the LLM sees.
- Add `examples=[...]` on non-obvious params (queries, paths, field-
  update structures).
- Replace `dict[str, list[Any]]` (fields) and `list[dict[str, Any]]`
  (links) with pydantic models (`FieldUpdate`, `EntryLink`).
- Pydantic-wrap the five raw-OData passthroughs (definition lists +
  `get_task_status`) into typed responses with snake_case fields.
- Add `response_format: Literal["concise","detailed"]` to heavy
  reads.
- Add `include_fields: list[str] | None` projection on entry-returning
  reads.
- Add `total_estimate: int | None` companion to `next_link` cursor
  responses.

### Structured logging + redaction (PLAN_ERRORS.md step 7)
- Per-tool-call decorator emitting one JSON event:
  `{ts, tool, args_redacted, duration_ms, outcome, request_id,
   upstream_trace_id, error_kind?, error_subkind?}`.
- `LF_LOG_FORMAT: Literal["text","json"]="text"` env var.
- Single `redact()` helper for credentials / hostnames / repo IDs
  wired into:
  - The decorator above
  - The retry-warning logs at `client.py:159-170`
  - The auth-flow logs at `auth.py:81, 139`
- `request_id` propagated via ContextVar so nested code can read it
  without threading it through every signature.

### Old-name removal (v3.0)
Every v1.x tool name (`get_entry`, `set_fields`, etc.) remains
registered as a deprecation alias through v2.x. Remove in v3.0.

---

## Bugs (from v1.4.2 live testing) — RESOLVED in v1.5.0

- ~~Bug 7 — `list_repositories` rejects list-shape responses.~~ Fixed:
  client normalizes both shapes to `{"value": [...]}`. See
  `test_client.py::test_list_repositories_normalizes_bare_list_response`.
- ~~Bug 8 — typed-return tools break the structured error contract at
  runtime.~~ Fixed by refactoring all 8 affected tools to return
  `dict[str, Any]` uniformly (`.model_dump()` on success, structured
  error dict on failure). Integration tests in
  `tests/test_integration.py` exercise the FastMCP runtime path so
  this gap can't reopen silently.

---

## Cloud AI parity (positioning, not capability)

The MCP already provides the substrate for every feature Laserfiche
markets at <https://www.laserfiche.com/products/ai/>. What's missing is
the adopter-facing layer that makes it obvious. Highest leverage first:

### "vs Laserfiche Cloud AI" comparison + recipes
- README section mapping each marketed feature (Smart Chat,
  Document Summarization, Smart Fields, Intelligent Tagging &
  Classification, AI Agents) to the concrete tool sequence that
  delivers it on this MCP.
- `docs/recipes/` directory, one page per use case:
  - `smart-chat.md` — natural-language Q&A against a folder. Tools:
    `search_natural` → `get_entry` → `get_document_edoc(mode="text")` →
    `get_field_values`.
  - `summarize-document.md` — single-doc summarization. Tools:
    `get_document_edoc(mode="text")` and pypdf extraction notes.
  - `smart-fields-extraction.md` — auto-populate template fields from
    new docs. Tools: `import_document` → `get_document_edoc(mode="text")`
    → `assign_template` (with validator) → `merge_fields`. Include a
    canonical invoice-extraction example.
  - `intelligent-classification.md` — auto-tag and template on entry.
    Tools: `list_tag_definitions` + `merge_tags` + `assign_template`.
  - `redundancy-scan.md` — the "AI Agents" example. Tools: walk via
    `list_folder`, inspect via `get_entry`/`get_field_values`,
    surface candidates via `delete_entry` previews (no execute).
  - `hr-onboarding.md` — Laserfiche's stock example. Tools:
    `create_folder` tree, `assign_template` per entry, suggested field
    defaults.
- Each recipe = a copy-pasteable prompt the user gives Claude, plus
  the tool sequence that fires under the hood.

### Folder-watcher example
- Companion script (or sibling repo) using `watchdog` that fires
  Claude via the API when a doc lands in a configured folder. Closes
  the "automatic, without human intervention" gap that Smart Fields
  advertises.
- Three-page doc: setup, prompt template, error handling. Lives at
  `docs/recipes/auto-classify-on-entry.md`.

### `batch_apply` tool
- New MCP tool: takes a folder ID + a prompt template + an optional
  template-assign spec; iterates entries, calls Claude per entry,
  writes fields back. Single tool call replaces an n-call loop for
  the "scan and classify" workflow.
- Path-fence and audit-reason guards apply per-entry. Honor
  `LF_DELETE_FOLDER_MAX_DESCENDANTS` for safety.
- Returns a per-entry results array so the LLM can summarize what
  changed.

### Cross-link recipes from tool docstrings
- When `get_document_edoc` is the right tool, the docstring should
  mention `docs/recipes/smart-chat.md` so an LLM picking it up has
  the workflow context.
- Light touch — one line per relevant tool.

---

## Future capability (real engineering, longer timelines)

### Server-side audit logging
Already mentioned as deferred in the v1.4 CHANGELOG. Sidecar log
file with rotation, captures every write tool call with the
authenticated user, target entry, and outcome. Belongs in v1.5.

### Laserfiche Cloud (`signin.laserfiche.com` JWT client_credentials)
The on-prem story is solid as of v1.4. Cloud auth is a separate flow:
JWT-signed assertion → access token, plus `api.laserfiche.com` v2-only
endpoints. Reserved on the roadmap; not blocking on-prem adopters.

### Async `/Searches` flow
SimpleSearches has a server-side row cap. The async flow handles very
large result sets via paged operation tokens. Not blocking — most
users hit `search_natural` which already pages within the cap.

### Per-user OAuth-on-behalf-of (ACL)
Cloud claims "respecting organizational access controls and
redactions." Ours inherits the LF service account's permissions —
correct, but only as fine-grained as that account. Per-user ACL flow
would mean OAuth-on-behalf-of (the requesting human's identity flows
to LF). Real engineering, longer term.

### Redaction-aware reads
If the LF server has redaction layers configured on documents, the
edoc + text paths currently bypass them. Add a flag (default true)
that respects redactions and surfaces an indicator in the response.
Stretch — only matters for orgs that actually use redactions.

---

## Operational

### Configure PyPI trusted publisher (one-time)
The release workflow is set up but the PyPI side isn't trusting it
yet. Go to
<https://pypi.org/manage/project/laserfiche-mcp/settings/publishing/>
and add a GitHub publisher:
- Owner: `SamuelSHernandez`
- Repository: `laserfiche-mcp`
- Workflow: `release.yml`
- Environment: `pypi`

Once configured, every `git push origin vX.Y.Z` auto-publishes to PyPI
via OIDC — no tokens, no manual `uv publish`. The release-workflow
failures on v1.4.0/v1.4.1/v1.4.2 were all this missing config.

### v1.4.2 PyPI propagation check
Direct version URL confirms v1.4.2 is uploaded, but the aggregate JSON
+ simple index were CDN-cached behind v1.4.0 at last check. Probe again
after a few hours:
```
curl https://pypi.org/pypi/laserfiche-mcp/json | python -c "import sys,json; print(json.load(sys.stdin)['info']['version'])"
```
Should return `1.4.2`.

### v1.4.1 GitHub release page
Tag `v1.4.1` exists on origin but no Release page was created (the
publish was blocked, so it's effectively superseded by v1.4.2).
Either:
- Create a stub release: `gh release create v1.4.1 --title "v1.4.1 — unreleased, superseded by v1.4.2" --notes "Tagged but not published. v1.4.2 is the released equivalent."`
- Or delete the tag locally and from origin, since it points to a
  commit with failing CI.

### Verify uvx install round-trip
Once PyPI propagation completes:
```
uvx --refresh laserfiche-mcp --version    # should print 1.4.2
```
Currently `--version` still prints `1.3.0` from the stale v1.4.0
artifact.

---

## Maintenance ideas (lower priority)

- Move from `uv tool` install hints in the README to `uvx` for the
  simpler quickstart path.
- ~~Expand `tests/test_integration.py` with real-server smoke tests~~ —
  done in v1.5.0. Future expansions worth considering: `copy_entry`
  async polling round-trip, `rename_entry` preview→execute end-to-end,
  `merge_fields` preserve-other-fields verification.
- ~~Consider a `--diagnose` CLI flag that probes the configured server~~
  — shipped in v1.5.0. Future: `--diagnose --json` for machine-readable
  output, `--diagnose --write-mode` to also test write-mode guards
  against a sandbox folder.

## How to find more bugs going forward

A short playbook for keeping this MCP robust as it gets exercised
against more LF builds and use cases:

1. **Property-based tests for the parser-like layers.** `search.py`'s
   query repair and `permissions.py`'s path matching both have
   well-defined input/output contracts. `hypothesis` can generate
   thousands of test inputs and surface edge cases (Unicode in paths,
   nested quotes, weird wildcards). Add `hypothesis` to dev deps and
   write property tests for `repair_escape_quotes`,
   `repair_wildcard_name`, and `_matches_prefix`.
2. **Real-server compatibility matrix.** Run `--diagnose` against
   different LF build versions and record the results in
   `docs/compatibility.md`. Patterns will emerge — which endpoints are
   reliable, which builds need workarounds. Each new pattern is a test
   case for the mock layer plus a doc note for adopters.
3. **Adversarial integration tests.** Add tests that send the MCP
   syntactically-valid-but-semantically-wrong inputs: paths with
   `..`, names with control chars, oversize field values, deeply
   nested folder structures. The structured-error contract should
   absorb all of these without raising.
4. **Type-check at build time.** `mypy src --strict` is currently
   advisory; consider making it a CI gate. Many Bug-8-class issues
   show up as `# type: ignore` comments before they hit production —
   if the CI rejects new `# type: ignore` additions, you catch them
   in the PR.
5. **Mutation testing.** `mutmut` flips operators, removes lines, etc.
   and reports which mutations survive your test suite — surviving
   mutations mean the tests don't actually cover that code path.
   Overkill for routine work; useful for an annual "is our test suite
   strong" audit.
6. **Fuzzing the JSON-RPC surface.** Run the MCP under
   [`mcp-fuzzer`](https://github.com/microsoft/mcp-fuzzer) or similar
   to throw malformed tool calls at it. Should reveal missing input
   validation at the FastMCP boundary.
7. **Lint pass on every PR.** v1.4.x shipped six lint findings that
   CI caught only after the tag. `pre-commit` (or just a `make check`
   target in docs) lets contributors lint before pushing.
8. **A `--strict-mode` flag for tools.** Optional setting that
   enforces stricter shapes on responses (e.g. assert
   `@odata.context` is present on every read). Helps surface server-
   side variability without crashing day-to-day workflows.
