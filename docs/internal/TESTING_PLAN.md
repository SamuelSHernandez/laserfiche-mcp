# Pre-release verification plan — Cloud auth + destructive-scope gate + mcp floor fix

Working notes, not a public doc. Covers the three uncommitted changes from
the 2026-09 session: Cloud service-app auth (`LF_AUTH_MODE=api_key`, BETA),
the `LF_HTTP_OAUTH_DESTRUCTIVE_SCOPE` gate, and the `mcp` dependency-floor
fix (`>=1.2.0` → `>=1.22.0`).

**Why this doc exists:** before telling clients this is done, we want to be
as sure as possible it won't break anything — split into what we can
actually verify (self-hosted, our own environment) versus what we
structurally cannot (Laserfiche Cloud — no tenant available). Draft, to be
refined together. Nothing below has been executed yet.

Status legend: `[ ]` not started · `[~]` planned, not run · `[x]` done

---

## Track 1 — Cloud auth: maximum confidence without a live tenant

The one thing we can't do is a real network round-trip against
`signin.laserfiche.com` / `api.laserfiche.com`. Everything below is aimed
at closing every *other* gap, so the BETA label is honest but as narrow as
possible.

- [x] **Protocol contract test (mocked transport).** `tests/test_auth.py`
  now exercises `CloudServiceAppStrategy.apply()`/`_refresh()` end-to-end
  against a mocked `signin.{domain}/oauth/token` response (`httpx_mock`),
  including caching, refresh-on-expiry, the optional `scope` param, and a
  rejected-credentials 401 wrapped as `LaserficheError` — not just JWT
  construction in isolation anymore. This catches wiring bugs (header
  name, content-type, response parsing, expiry math, the `+30s` refresh
  skew) that JWT-only tests couldn't. Still open, in ascending order of how
  much they'd actually close the gap: a real ASGI/HTTP-stub server
  exercising actual wire serialization (header casing, content-type
  negotiation) rather than pytest-httpx's request matching; then a real
  network round-trip against a live Cloud tenant.
- [ ] **Line-by-line diff against the reference client**, beyond the JWT
  claims already checked: token refresh timing, error-response handling,
  and the regional-domain variant (`api.eu.laserfiche.com`) against
  `Laserfiche/lf-api-client-core-dotnet`'s `TokenClient`.
- [ ] **Check for a published OpenAPI/Swagger spec** for the Cloud token
  endpoint and Repository API v2 surface; diff request/response
  assumptions against it if one exists.
- [ ] **Search Laserfiche's GitHub issues / developer community** for
  reported gotchas in the service-app flow (clock-skew tolerance,
  required scopes, kid-header quirks) not reflected in the reference
  library.
- [ ] **Ask about a trial/sandbox Cloud tenant.** Not assumed available —
  if one exists even temporarily, this is the only item that actually
  closes the gap rather than narrowing it.

**Ceiling, even after all of the above:** "verified as far as anyone can
without hitting the real service." Docs/README keep saying exactly that —
BETA stays BETA until someone with real Cloud access confirms it.

---

## Track 2 — Everything else: closing gaps in what's already automated

- [ ] **OAuth destructive-scope gate — integration-level test.** Current
  tests (`tests/tools/test_writes_delete_entry.py`) monkeypatch
  `_helpers.get_access_token` directly, bypassing FastMCP's real OAuth
  middleware entirely. Add a test using the existing `TestClient` /
  `--http` harness (see `tests/test_http_transport.py`) with a
  fake-but-real `TokenVerifier`, so the scope check is exercised through
  the actual ASGI request path. Doesn't touch Laserfiche at all — pure
  in-process test, safe to run anytime.
- [ ] **Fresh-clone install test** for the `mcp` floor fix: clone into a
  scratch dir, `uv sync` from a clean state, confirm it resolves a
  working `mcp` version and `laserfiche_mcp.server` imports + the server
  boots. Closes the loop beyond the ad hoc isolated-venv bisection done
  this session.
- [x] Full regression suite — 794 passed / 12 skipped, run twice
  independently (once against the real repo venv, once against an
  isolated venv pinned to `mcp==1.22.0`).
- [x] Traced that both new features are no-ops for
  `LF_AUTH_MODE=password` / stdio / no `LF_HTTP_OAUTH_ISSUER` — the
  config your production actually runs.

---

## Track 3 — Live testing in your own environment (GC IPRS, self-hosted)

This is the one that actually matters for "will this embarrass me in
front of a client." Everything here is self-hosted / password-auth /
stdio — Cloud stays out of scope, it's not testable in this environment.

### 1. Baseline capture (before touching anything)
- [ ] Run a fixed set of known-good read/write smoke flows against the
  **current, unmodified** production code. Record results (entry IDs,
  expected field values, expected search hit counts) as the comparison
  baseline. Candidate flows — reuse what's already known-good from prior
  sessions (the GC IPRS self-hosted connection; entry 81469, a December
  2025 PAF, as a canonical demo doc) plus whatever your normal day-to-day
  tool calls look like.

### 2. Cutover mechanics
- [ ] Confirm the MCP server still runs from local source
  (`uv run --directory ... laserfiche-mcp`) — new code goes live on next
  reconnect/restart, no separate deploy step.
- [ ] Decide: test directly on `main` (it's local-only, trivially
  revertible via `git stash`/checkout) or cut a throwaway branch first.

### 3. Regression pass (proves the new features didn't disturb anything)
- [ ] Re-run the Step 1 baseline flows post-cutover. Confirm identical
  results — same entries, same field values, same search hits, same
  error shapes on the known error cases (9528/9010 misleading-code
  behavior, etc.).
- [ ] Spot-check the existing write-mode safety guards still behave
  identically if write mode is exercised: confirmation-token
  preview→execute flow, path fences, `LF_DELETE_FOLDER_MAX_DESCENDANTS`.

### 4. New-feature-specific checks
- **Destructive-scope gate:** only meaningful once/if `--http` + OAuth is
  actually deployed. If that's not imminent, defer — it's already covered
  by Track 2's in-process test and doesn't touch real Laserfiche data
  either way, so there's no urgency to validate it against production.
- **Cloud auth:** not testable here at all — stays Track 1 only.

### 5. Rollback plan
- [ ] Since everything is uncommitted/local, rollback is `git stash` or
  checkout of the prior commit. No release/tag/PyPI step is involved
  until we're ready to ship, so rollback risk during testing is low.

### 6. Test-data safety
- [ ] Any write/delete-preview testing uses a designated non-production
  folder/entry — never real records. Consistent with the project's
  existing destructive-safety principle: delete/rename previews are safe
  to run anywhere (read-only), but *execute* legs only run against
  scratch data.

---

## Open questions to refine together

- What's the actual set of "known-good" smoke flows for Step 1's
  baseline? Worth listing explicitly rather than improvising in the
  moment.
- Is `--http` + OAuth deployment on the near-term roadmap, or purely
  hypothetical for now? Determines whether Track 3 §4's destructive-scope
  live check is worth scheduling at all.
- Any interest in pursuing a Cloud trial tenant, or is that a dead end
  worth not spending time chasing?
