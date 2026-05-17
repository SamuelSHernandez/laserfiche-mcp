# Error contract

Every tool in `laserfiche-mcp` returns a structured dict on failure rather
than raising. This is deliberate: when an MCP tool raises an exception,
most clients surface a one-line `"Error executing tool X: ..."` string to
the LLM, which is hard to act on. A structured response lets the LLM
branch on a stable slug, surface a human-readable reason to the user, and
take a different action without parsing prose.

This document is a reference for that contract — the shape, the slugs,
when each slug is emitted, and what a caller should typically do in
response.

## Shape

```jsonc
{
  "mode": "error",                          // always literal "error"
  "operation": "laserfiche_entry_delete",   // the tool that produced this
  "kind": "not_found",                      // one of 5 canonical ToolErrorKind values
  "error": "not_found",                     // stable machine-readable subkind
  "status_code": 404,                       // HTTP status from the server (nullable)
  "server_error_code": null,                // Laserfiche-specific errorCode (nullable)
  "server_message": null,                   // server's title/message field (nullable)
  "reason": "Server returned 404 — ...",    // human-readable hint
  "request_id": "9f2c…",                    // UUID4 unique to this tool invocation
  "upstream_trace_id": null,                // W3C trace ID from the LF ProblemDetails (nullable)
  "entry_id": 999                           // optional, present when relevant
}
```

Additional fields are appended for specific operations — e.g.
`create_folder` includes `parent_id` and `name`, `delete_pages` includes
`page_range`, `assign_template` failures include `template_name`. These
are documented in the per-tool docstrings and shouldn't be relied on
positionally; always key by name.

> **Stability.** `mode`, `operation`, `kind`, `error`, `status_code`,
> `reason`, `request_id` are guaranteed across releases.
> `server_error_code`, `server_message`, and `upstream_trace_id` reflect
> upstream Laserfiche behavior and may be `null` depending on what the
> server returned. Per-operation extras (e.g. `entry_id`) are documented
> per tool and may evolve. The `kind` and `error` taxonomies below are
> the primary stable surface.

## Kinds (`kind`)

`kind` is one of five canonical `ToolErrorKind` values. LLM callers
branch on `kind` for category-level decisions (retry vs ask user vs
abort); they branch on `error` (the subkind) for the actionable
specifics.

| Kind                    | Meaning                                                                                                                       |
| ----------------------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `not_found`             | The named entry, path, or endpoint doesn't exist. Verify the target with the user.                                            |
| `permission_denied`     | The credentials, ACLs, or local fence config refused the operation. Don't retry without changing config or asking the user.   |
| `rate_limited`          | The server told the caller to slow down. Back off and retry; the client also retries 429 internally up to `LF_RETRY_ATTEMPTS`.|
| `invalid_input`         | The request itself is malformed or fails a local pre-flight. Fix the input and re-call; never retry the exact same payload.   |
| `upstream_unavailable`  | The Laserfiche server returned 5xx, 405, or an otherwise opaque failure. Retry once, then surface to the user.                |

The subkind → kind mapping lives in `_SUBKIND_TO_KIND` in
`src/laserfiche_mcp/errors.py` and is exposed via the public helper
`laserfiche_mcp.errors.kind_for_subkind(subkind)`. v1.5 callers that
branched on `error` continue to work; new code can branch on `kind`
for category-level handling.

## Subkinds (`error`) from server responses

These subkinds are produced by `classify_lf_error()` in
`src/laserfiche_mcp/errors.py` when the Repository API returns a
non-2xx response. Direct unit tests cover every slug.

| Subkind                    | Triggers                                                                       | Kind                    | Typical LLM response                                                                |
| -------------------------- | ------------------------------------------------------------------------------ | ----------------------- | ----------------------------------------------------------------------------------- |
| `auth_failed`              | HTTP 401/403, LF errorCode 9010, or LF 9528 (misleadingly worded but usually creds) | `permission_denied`     | Tell the user the credentials or permissions are wrong. Don't retry without input. |
| `required_field_missing`   | LF errorCode 9039/9066 from the server                                         | `invalid_input`         | Read the `missing` and `field_details` keys (when set by the preflight) and ask the user for values. |
| `not_found`                | HTTP 404                                                                       | `not_found`             | Verify the entry ID / path with the user before retrying.                           |
| `method_not_allowed`       | HTTP 405                                                                       | `upstream_unavailable`  | Usually an MCP bug — surface to the user and stop. Worth a bug report.              |
| `unsupported_media_type`   | HTTP 415                                                                       | `invalid_input`         | Usually an MCP wire-format bug — same as above.                                     |
| `rate_limited`             | HTTP 429                                                                       | `rate_limited`          | Back off and retry after a delay; the client also retries 429 with exponential backoff up to `LF_RETRY_ATTEMPTS`. |
| `server_error`             | HTTP 5xx or any unrecognized failure                                           | `upstream_unavailable`  | Retry once, then surface to the user.                                               |

## Tool-specific pre-server errors

Some tools have additional `mode: "error"` shapes that fire **before**
hitting the server — these are local guards, not classified by the
generic mapper. They still carry the same envelope (`kind`, `error`,
`request_id`, ...):

| Subkind                       | Tool(s)                                                            | When                                                                                   | Kind                    |
| ----------------------------- | ------------------------------------------------------------------ | -------------------------------------------------------------------------------------- | ----------------------- |
| `path_not_allowed`            | every write tool                                                   | Target (or, for `move_entry`, destination) path falls outside `LF_WRITE_PATHS_ALLOW` / inside `LF_WRITE_PATHS_DENY`. | `permission_denied`     |
| `path_traversal_blocked`      | every write tool                                                   | Path contains a `..` segment. Rejected regardless of allow/deny config.                | `permission_denied`     |
| `exceeds_batch_cap`           | `delete_entry`                                                     | Folder has more immediate children than `LF_DELETE_FOLDER_MAX_DESCENDANTS`. Pass `force_large_delete=true` to override. | `invalid_input`         |
| `audit_reason_required`       | `delete_entry`                                                     | `LF_REQUIRE_AUDIT_REASON=true` and the caller didn't supply `audit_reason_id`.        | `invalid_input`         |
| `invalid_confirmation_token`  | `rename_entry`, `move_entry`, `delete_entry`, `delete_edoc`, `delete_pages` | Token is expired, malformed, or bound to a different `(operation, entry_id, entry_name)` tuple. | `invalid_input`         |
| `missing_required_fields`     | `assign_template`                                                  | `LF_VALIDATE_REQUIRED_FIELDS=true` and one or more `isRequired` fields are unset and not supplied via `fields=`. Response includes `missing` (names) and `field_details` (full metadata). | `invalid_input`         |
| `tool_not_allowed`            | every write tool                                                   | Tool name isn't in `LF_WRITE_TOOLS_ALLOWED`. (Belt-and-suspenders to the registration-time gate.) | `permission_denied`     |
| `page_range_required`         | `delete_pages`                                                     | Empty `page_range` (the API treats empty as "delete all pages" — too easy to fat-finger). | `invalid_input`         |
| `invalid_page_range`          | `delete_pages`                                                     | Page-range string is non-empty but malformed (e.g. `"1-"`, `"abc"`, `"3-2"`).         | `invalid_input`         |
| `invalid_name`                | `create_folder`, `import_document`, `copy_entry`, `rename_entry`, `move_entry` | Entry name contains `\`, `/`, NULL bytes, control characters, or is outside length 1–128. | `invalid_input`         |
| `invalid_field_name`          | every metadata write that takes fields                             | `LF_VALIDATE_NAMES=true` and a referenced field name isn't in the cached `FieldDefinitions`. Response lists valid names. | `invalid_input`         |
| `invalid_field_value`         | every metadata write that takes fields                             | A field value fails the constraint validator (e.g. type, choice list).                | `invalid_input`         |
| `invalid_tag_name`            | `set_tags`, `merge_tags`                                           | `LF_VALIDATE_NAMES=true` and a tag name isn't in the cached `TagDefinitions`.         | `invalid_input`         |
| `invalid_template_name`       | `assign_template`, `get_template_fields`                           | `LF_VALIDATE_NAMES=true` and the template name isn't in the cached `TemplateDefinitions`. Response lists valid names. | `invalid_input`         |
| `invalid_link_type`           | `set_links`                                                        | `LF_VALIDATE_NAMES=true` and a link-type name isn't in the cached `LinkDefinitions`.  | `invalid_input`         |
| `file_not_found`              | `import_document`                                                  | The local `file_path` doesn't exist or isn't readable.                                | `invalid_input`         |
| `size_exceeds_cap`            | `import_document`, `get_document_edoc`                             | Payload exceeds `LF_IMPORT_MAX_BYTES` / `LF_EDOC_MAX_BYTES`.                          | `invalid_input`         |
| `expected_folder_got_document`| `create_folder`, `import_document`, `copy_entry` (when targeting a parent) | The parent_id resolved to a document, not a folder.                          | `invalid_input`         |
| `bad_query_syntax`            | `search_natural`                                                   | The two automatic repairs (quote-escape, name wildcarding) didn't produce a query the server would accept. Response surfaces every attempt. | `invalid_input`         |
| `endpoint_disabled`           | reads against builds that disable specific endpoints               | Endpoint returned an explicit "not available" response (see `list_repositories` for the `mode: "fallback"` variant). | `upstream_unavailable`  |

## Special: `list_repositories` fallback

When the `/Repositories` endpoint isn't exposed by the server (some
builds disable it), `list_repositories` does **not** return `mode:
"error"` — instead it returns `mode: "fallback"` with the configured
`LF_REPOSITORY_ID` as a one-item list:

```jsonc
{
  "mode": "fallback",
  "operation": "list_repositories",
  "warning": "Server's /Repositories endpoint returned an error (status=400). ...",
  "server_error": { /* classified error dict per the slug taxonomy above */ },
  "value": [{ "repoId": "my-repo", "displayName": null, "is_configured": true }]
}
```

This is so the LLM still gets actionable data (one valid repo it can
operate against) instead of a hard failure. Branch on `mode == "fallback"`
if you need to distinguish partial from full results.

## Asserting against the contract in tests

```python
from laserfiche_mcp.errors import LaserficheError, classify_lf_error

def test_my_tool_handles_missing_entry():
    exc = LaserficheError("test", status_code=404, detail={})
    result = classify_lf_error("my_tool", exc, entry_id=42)
    assert result["mode"] == "error"
    assert result["kind"] == "not_found"
    assert result["error"] == "not_found"
    assert result["entry_id"] == 42
    assert result["request_id"]  # always set
```

End-to-end tests can call the tool with `httpx_mock` returning the
target status and assert the same shape — the existing
`test_classify_lf_error_*` tests in `tests/test_errors.py` are the
reference.

## Notes for upgraders

- **Pre-v1.4**: tools raised `RuntimeError(f"Failed to ...: {exc}")` on
  `LaserficheError`. Existing integrations that wrap tool calls in
  `try/except RuntimeError` should switch to checking
  `result.get("mode") == "error"` and branching on `result["error"]`
  (or `result["kind"]`).
- **v1.5 → v2.0**: success-path JSON is unchanged. Error responses
  gained `kind`, `request_id`, and `upstream_trace_id` alongside the
  existing fields — callers parsing only the existing fields continue
  to work; the new fields are optional context.

The one place that still raises is `_require_writes_enabled()`, which
fires when a write tool is invoked while `LF_READ_ONLY=true`. That's a
configuration error, not a server error, and propagates as a
`RuntimeError` so it surfaces loudly during setup.
