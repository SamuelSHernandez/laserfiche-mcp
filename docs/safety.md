# Safety model

Wondering **what Claude actually sees and where your document content goes**?
See [Data handling & privacy](data-handling.md) — it covers the data flow,
what leaves the machine, and how to scope a service account so sensitive folders
are never exposed. The rest of this page is about the **write-mode** guards.

Writes are off by default. When you enable them (`LF_READ_ONLY=false`),
the following guards are available — all independent, all opt-in
except as noted:

- **Path-prefix fences** (`LF_WRITE_PATHS_ALLOW`, `LF_WRITE_PATHS_DENY`) — every write checks the entry's `fullPath` (or the parent's for creates) against the configured prefixes. Case-insensitive, deny wins over allow, both `\` and `/` accepted. `move_entry` fences on BOTH source and destination paths so a token from an allowed source can't be replayed to land in a denied folder. Strongest single fence — recommended for any non-trivial deployment.
- **Local import source fence** (`LF_IMPORT_SOURCE_DIRS`, default unset) — `import_document` reads `file_path` off the MCP process's own filesystem; with this set, the resolved (symlinks included) path must fall inside one of the configured directories or the tool refuses with `source_path_not_allowed`. This is the source-side counterpart to the path-prefix fences above, which only cover the repository destination.
  - **Unset is deprecated.** Today it still works (nothing breaks), but a startup warning is logged when writes are on, and a future major release is planned to refuse imports until a folder is configured. To accept any path *deliberately*, set `LF_IMPORT_SOURCE_DIRS=*` — that is explicit, and silences the warning.
  - **Always-on blocklist.** Regardless of this setting (even with `*`), `import_document` refuses well-known credential files: anything inside `.ssh`, `.aws`, `.gnupg`, `.azure`, `.kube`; files named `.env`/`.env.*` (not `.env.example`), `.netrc`, `.git-credentials`, `.pgpass`, `.npmrc`, `.pypirc`, `.claude.json`, `id_rsa`/`id_ed25519`/…; private-key suffixes (`.key`, `.pfx`, `.p12`, `.ppk`, `.kdbx`, `.jks`); and a few OS credential stores. It is a safety net, not a boundary — a fence directory is the boundary.
  - The file is read from the exact resolved path that passed the check, and each import logs that path.
- **Tool-level allowlist** (`LF_WRITE_TOOLS_ALLOWED`) — restrict which write tools register at all. Example: `merge_fields,merge_tags,assign_template` for a metadata-only deployment that can't create or delete anything.
- **Folder-delete batch cap** (`LF_DELETE_FOLDER_MAX_DESCENDANTS`, default 50) — `delete_entry` on a folder with more immediate children refuses unless `force_large_delete=true` is passed alongside the confirmation token. The preview surfaces `exceeds_batch_cap: true` so the LLM can explain the size before re-calling. If the child-count probe itself fails (transient error), the delete fails CLOSED — `child_count_probe_failed: true` on the preview, and execute refuses with `child_count_probe_failed` regardless of `force_large_delete` — rather than assuming an unknown count is safe.
- **Audit-reason requirement** (`LF_REQUIRE_AUDIT_REASON`, default false) — when true, `delete_entry` refuses without an `audit_reason_id`. Use `get_audit_reasons` to enumerate valid IDs.
- **Required-field validation** (`LF_VALIDATE_REQUIRED_FIELDS`, default **true**) — `assign_template` lists `FieldDefinitions`, finds `isRequired: true` fields, checks them against what's on the entry and what's in the caller's `fields=`, and returns a structured `missing_required_fields` error before the PUT — instead of the server's opaque `Multistatus response. [9039]`.
- **Two-step confirmation tokens** (always on for destructive ops) — `rename_entry`, `move_entry`, `delete_entry`, `delete_edoc`, `delete_pages` return a preview + HMAC-signed token on first call; execute on second call. Tokens bind to `(operation, entry_id, entry_name)` plus the operation's execute-relevant parameters (`page_range`, `new_name`, destination), so the execute leg cannot silently swap in different arguments than the user confirmed; they expire after 5 minutes. By default the signing key is random per-process, so a restart invalidates pending tokens; set `LF_CONFIRMATION_SECRET` to derive a stable key instead (tokens survive restarts and verify across instances sharing the secret — for multi-instance deployments). This two-step dance is a prompt-level convention, not an enforced one — nothing stops an autonomous caller from confirming its own preview. `LF_HTTP_OAUTH_DESTRUCTIVE_SCOPE` (see [Remote HTTP](remote-http.md)) is the actual enforcement point for unattended callers.
- **Destructive-op OAuth scope** (`LF_HTTP_OAUTH_DESTRUCTIVE_SCOPE`, OAuth mode only, opt-in) — executing `delete_entry` / `delete_edoc` / `delete_pages` requires this scope on the caller's token; previews don't. Lets one deployment serve both a human (whose token carries the scope) and an unattended agent (whose `client_credentials` token doesn't) — see [Unattended agents](remote-http.md#unattended-agents). `move_entry` / `rename_entry` are reversible and not gated by this.

### What the confirmation token is — and is not

The two-step token is **friction for the model, not a human approval step.**
Nothing in the protocol forces a person to look at the preview: the tool
descriptions tell the model to show it to the user and wait, and a well-behaved
client does, but a model that is confused, rushed, or steered by a malicious
document (prompt injection) can call the preview and the execute back to back in
one turn. The token proves the execute call matches a preview the server issued
in the last five minutes; it does not prove a human saw or agreed to it.

What it *does* guarantee is what the preview was about. A token is bound to:

| Binding | Effect |
|---|---|
| operation + entry id + entry name | can't be used for a different operation or entry, or after a rename |
| the operation's parameters (`page_range`, `new_name`, destination, …) | "preview pages 1–2, execute 1–9999" fails |
| **the caller** (OAuth user, else client id) | one identity's preview can't authorize another's call — an agent's preview can't be executed by a human's token or vice versa; each must preview as themselves |
| **the entry's state** (last-modified time) | if the entry changed after the preview, the user confirmed something that no longer exists — the call is refused and must be re-previewed |
| five-minute expiry | a forgotten token can't wait indefinitely |

Under stdio or a static bearer token there is exactly one principal, so the caller
binding is empty there; it applies wherever the server can tell callers apart.

**Why tokens are not single-use.** A ledger of used tokens would need server-side
state (per process, so useless across instances sharing a signing key) and would
add a failure mode (a legitimate retry after a transient error is refused). It
isn't needed for the property it would buy, which is "the same authorization can't
be spent twice":

- the token is bound to the entry's state, and *executing the operation changes the
  entry* — so the token that authorized it no longer matches afterwards, and a
  replay is refused before anything reaches Laserfiche. That holds for delete,
  rename, move, delete-edoc and delete-pages alike (and pages are additionally
  bound to the page count, since page numbers shift);
- replay needs the token *and* the same caller, who could equally run a fresh
  preview-and-execute — the replay grants no authority the caller didn't already
  have, and the OAuth destructive scope is re-checked on every execute;
- anything that does change an entry between preview and execute invalidates the
  token, which also closes the preview-to-execute time-of-check gap.

What this does not cover: a token authorizes exactly the one previewed change and
nothing more, but it does not make a human approve anything, and a retry after an
ambiguous failure needs a fresh preview (the safe outcome). Do not treat it as
your only safeguard for anything you cannot undo. The controls that bound the
damage are enforced by the server regardless of what the model does:

- keep `LF_READ_ONLY=true` unless you need writes;
- scope writes with `LF_WRITE_PATHS_ALLOW` / `LF_WRITE_PATHS_DENY` and
  `LF_WRITE_TOOLS_ALLOWED` (omit the delete tools if you don't need them);
- set `LF_IMPORT_SOURCE_DIRS` (a startup warning is logged if writes are on and
  it is unset);
- use `LF_HTTP_OAUTH_DESTRUCTIVE_SCOPE` (OAuth mode) so a human's own credential
  is required to execute deletes;
- rely on the Laserfiche account's own permissions — the repository ACLs are the
  real fence. Note the audit log records the service account, not the person
  chatting with Claude, unless you use `oauth_passthrough`.

### How errors are contained

Failures are handled in layers, so a problem in one layer can't reach the model as
a raw exception or a leaked secret:

1. **Transport** (`client/_core.py`): every `httpx` failure becomes a
   `LaserficheError`. Reads and idempotent requests retry transient failures;
   writes are *not* blindly replayed after an ambiguous failure — they report
   `outcome_unknown`.
2. **Tool**: each tool returns precise structured errors for what it expects
   (`not_found`, `path_not_allowed`, ...).
3. **Boundary** (`safety.py`, wraps every tool): anything else becomes a structured
   `internal_error` / `network_error` / `local_io_error` with a `request_id`; the
   traceback is logged server-side only; configured secrets are scrubbed from the
   response. Cancellation and shutdown are never swallowed.
4. **Confirmation tokens** (`confirmation.py`): verification is total — a malformed
   or hostile token is simply "not valid", never an exception.

A test runs every registered tool against a range of failure types and asserts
none raises, so a new tool that forgets a case fails CI. Adding a tool needs nothing
extra: register it with `@register` and the boundary applies automatically.

### Recommended starting config for write mode

```jsonc
"env": {
  "LF_READ_ONLY": "false",
  "LF_WRITE_PATHS_ALLOW": "\\Sandbox\\mcp-test",        // scope to a sandbox first
  "LF_WRITE_TOOLS_ALLOWED": "create_folder,import_document,merge_fields,merge_tags,assign_template,delete_entry",
  "LF_DELETE_FOLDER_MAX_DESCENDANTS": "10",
  "LF_REQUIRE_AUDIT_REASON": "false"                    // turn on once you have a workflow
}
```

Pre-create the sandbox folder by hand in the Laserfiche web client; the
fence needs an existing parent to read its `fullPath`. Once
smoke-tested, broaden the tool list — path scope is still the strongest
fence regardless of which tools are registered.
