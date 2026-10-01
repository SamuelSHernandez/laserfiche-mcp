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
- **Tool-level allowlist** (`LF_WRITE_TOOLS_ALLOWED`) — restrict which write tools register at all. Example: `merge_fields,merge_tags,assign_template` for a metadata-only deployment that can't create or delete anything.
- **Folder-delete batch cap** (`LF_DELETE_FOLDER_MAX_DESCENDANTS`, default 50) — `delete_entry` on a folder with more immediate children refuses unless `force_large_delete=true` is passed alongside the confirmation token. The preview surfaces `exceeds_batch_cap: true` so the LLM can explain the size before re-calling. If the child-count probe itself fails (transient error), the delete fails CLOSED — `child_count_probe_failed: true` on the preview, and execute refuses with `child_count_probe_failed` regardless of `force_large_delete` — rather than assuming an unknown count is safe.
- **Audit-reason requirement** (`LF_REQUIRE_AUDIT_REASON`, default false) — when true, `delete_entry` refuses without an `audit_reason_id`. Use `get_audit_reasons` to enumerate valid IDs.
- **Required-field validation** (`LF_VALIDATE_REQUIRED_FIELDS`, default **true**) — `assign_template` lists `FieldDefinitions`, finds `isRequired: true` fields, checks them against what's on the entry and what's in the caller's `fields=`, and returns a structured `missing_required_fields` error before the PUT — instead of the server's opaque `Multistatus response. [9039]`.
- **Two-step confirmation tokens** (always on for destructive ops) — `rename_entry`, `move_entry`, `delete_entry`, `delete_edoc`, `delete_pages` return a preview + HMAC-signed token on first call; execute on second call. Tokens bind to `(operation, entry_id, entry_name)` plus the operation's execute-relevant parameters (`page_range`, `new_name`, destination), so the execute leg cannot silently swap in different arguments than the user confirmed; they expire after 5 minutes. By default the signing key is random per-process, so a restart invalidates pending tokens; set `LF_CONFIRMATION_SECRET` to derive a stable key instead (tokens survive restarts and verify across instances sharing the secret — for multi-instance deployments). This two-step dance is a prompt-level convention, not an enforced one — nothing stops an autonomous caller from confirming its own preview. `LF_HTTP_OAUTH_DESTRUCTIVE_SCOPE` (see [Remote HTTP](remote-http.md)) is the actual enforcement point for unattended callers.
- **Destructive-op OAuth scope** (`LF_HTTP_OAUTH_DESTRUCTIVE_SCOPE`, OAuth mode only, opt-in) — executing `delete_entry` / `delete_edoc` / `delete_pages` requires this scope on the caller's token; previews don't. Lets one deployment serve both a human (whose token carries the scope) and an unattended agent (whose `client_credentials` token doesn't) — see [Unattended agents](remote-http.md#unattended-agents). `move_entry` / `rename_entry` are reversible and not gated by this.

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
