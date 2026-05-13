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
  "operation": "delete_entry",              // the tool that produced this
  "error": "not_found",                     // stable machine-readable slug
  "status_code": 404,                       // HTTP status from the server (nullable)
  "server_error_code": null,                // Laserfiche-specific errorCode (nullable)
  "server_message": null,                   // server's title/message field (nullable)
  "reason": "Server returned 404 — ...",    // human-readable hint
  "entry_id": 999                           // optional, present when relevant
}
```

Additional fields are appended for specific operations — e.g.
`create_folder` includes `parent_id` and `name`, `delete_pages` includes
`page_range`, `assign_template` failures include `template_name`. These
are documented in the per-tool docstrings and shouldn't be relied on
positionally; always key by name.

> **Stability.** `mode`, `operation`, `error`, `status_code`, `reason`
> are guaranteed across releases. `server_error_code` and
> `server_message` reflect upstream Laserfiche behavior and may shift
> with their builds. Per-operation extras (e.g. `entry_id`) are
> documented per tool and may evolve. The slug taxonomy below is the
> primary stable surface.

## Slug taxonomy

| Slug                       | Triggers                                                                       | Typical LLM response                                                                |
| -------------------------- | ------------------------------------------------------------------------------ | ----------------------------------------------------------------------------------- |
| `auth_failed`              | HTTP 401/403, LF errorCode 9010, or LF 9528 (misleadingly worded but usually creds) | Tell the user the credentials or permissions are wrong. Don't retry without input. |
| `required_field_missing`   | LF errorCode 9039/9066 from the server, OR the `LF_VALIDATE_REQUIRED_FIELDS` preflight on `assign_template` | Read the `missing` and `field_details` keys (when set by the preflight) and ask the user for values. |
| `not_found`                | HTTP 404                                                                       | Verify the entry ID / path with the user before retrying.                           |
| `method_not_allowed`       | HTTP 405                                                                       | Usually an MCP bug — surface to the user and stop. Worth a bug report.              |
| `unsupported_media_type`   | HTTP 415                                                                       | Usually an MCP wire-format bug — same as above.                                     |
| `rate_limited`             | HTTP 429                                                                       | Back off and retry after a delay; the client also retries 429 with exponential backoff up to `LF_RETRY_ATTEMPTS`. |
| `server_error`             | HTTP 5xx or any unrecognized failure                                           | Retry once, then surface to the user.                                               |

The slug-to-status mapping lives in `_classify_lf_error()` in
`src/laserfiche_mcp/server.py`. Direct unit tests cover every slug.

## Tool-specific pre-server errors

Some tools have additional `mode: "error"` shapes that fire **before**
hitting the server — these are local guards, not classified by the
generic mapper:

| Slug                          | Tool(s)                                                            | When                                                                                   |
| ----------------------------- | ------------------------------------------------------------------ | -------------------------------------------------------------------------------------- |
| `path_not_allowed`            | every write tool                                                   | Target (or, for `move_entry`, destination) path falls outside `LF_WRITE_PATHS_ALLOW` / inside `LF_WRITE_PATHS_DENY`. |
| `exceeds_batch_cap`           | `delete_entry`                                                     | Folder has more immediate children than `LF_DELETE_FOLDER_MAX_DESCENDANTS`. Pass `force_large_delete=true` to override. |
| `audit_reason_required`       | `delete_entry`                                                     | `LF_REQUIRE_AUDIT_REASON=true` and the caller didn't supply `audit_reason_id`.        |
| `invalid_confirmation_token`  | `rename_entry`, `move_entry`, `delete_entry`, `delete_edoc`, `delete_pages` | Token is expired, malformed, or bound to a different `(operation, entry_id, entry_name)` tuple. |
| `missing_required_fields`    | `assign_template`                                                   | `LF_VALIDATE_REQUIRED_FIELDS=true` and one or more `isRequired` fields are unset and not supplied via `fields=`. Response includes `missing` (names) and `field_details` (full metadata). |
| `tool_not_allowed`            | every write tool                                                   | Tool name isn't in `LF_WRITE_TOOLS_ALLOWED`. (Belt-and-suspenders to the registration-time gate.) |
| `page_range_required`         | `delete_pages`                                                     | Empty `page_range` (the API treats empty as "delete all pages" — too easy to fat-finger). |

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
from laserfiche_mcp import server
from laserfiche_mcp.client import LaserficheError

def test_my_tool_handles_missing_entry():
    exc = LaserficheError("test", status_code=404, detail={})
    result = server._classify_lf_error("my_tool", exc, entry_id=42)
    assert result["mode"] == "error"
    assert result["error"] == "not_found"
    assert result["entry_id"] == 42
```

End-to-end tests can call the tool with `httpx_mock` returning the
target status and assert the same shape — the existing
`test_classify_lf_error_*` tests in `tests/test_server.py` are the
reference.

## Backwards-incompatible note

Prior to v1.4, tools raised `RuntimeError(f"Failed to ...: {exc}")` on
LaserficheError. Existing integrations that wrap tool calls in
`try/except RuntimeError` will see the exception path go away — tools
now return the structured error instead. Adapt by checking
`result.get("mode") == "error"` and branching on `result["error"]`.

The one place that still raises is `_require_writes_enabled()`, which
fires when a write tool is invoked while `LF_READ_ONLY=true`. That's a
configuration error, not a server error, and propagates as a
`RuntimeError` so it surfaces loudly during setup.
