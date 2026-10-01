# Tools

> Tool names below are shown in their original verb-first form
> (`get_entry`, `set_fields`, ...) for readability. In v2.0 every tool is
> *also* registered under the `laserfiche_{resource}_{verb}` form
> (`laserfiche_entry_get`, `laserfiche_field_set`, ...). Both names
> resolve to the same function. The `laserfiche_*` names are the
> recommended path; the old names remain as deprecation aliases through
> v2.x and will be removed in v3.0. The authoritative mapping lives in
> `_V2_RENAME_MAP` in [`src/laserfiche_mcp/server.py`](../src/laserfiche_mcp/server.py).

## Reads (always registered)

| Tool                         | v2 name                                | Purpose                                                                 |
| ---------------------------- | -------------------------------------- | ----------------------------------------------------------------------- |
| `search_entries`             | `laserfiche_entry_search`              | Run a raw Laserfiche search query, e.g. `{LF:Name="*.pdf"}`             |
| `search_by_name`             | `laserfiche_entry_search_by_name`      | Convenience wrapper: name pattern + optional folder scope               |
| `search_natural`             | `laserfiche_entry_search_natural`      | Two-mode guided search: ask for grammar+templates, then run with auto-repair on 400 |
| `search_content`             | `laserfiche_entry_search_content`      | Full-text search that returns the **matched passages** — page number plus excerpt, straight from the OCR index |
| `list_folder`                | `laserfiche_folder_list`               | List children of a folder by ID                                          |
| `get_entry`                  | `laserfiche_entry_get`                 | Fetch metadata for one entry by ID                                       |
| `get_entry_by_path`          | `laserfiche_entry_get_by_path`         | Resolve a full path to an entry                                          |
| `get_field_values`           | `laserfiche_field_values_get`          | Read all template fields assigned to an entry                            |
| `get_document_text`          | `laserfiche_document_get_text`         | Server-side extracted text (v2 only; v1 use `get_document_edoc(mode="text")`) |
| `get_document_edoc`          | `laserfiche_document_get_edoc`         | Inspect edoc (`info`), download bytes (`bytes`), or extract text (`text`) |
| `compare_entries`            | `laserfiche_entry_compare`             | Diff two entries' metadata and template fields — is this a re-scanned duplicate or a real difference? |
| `find_duplicate_documents`   | `laserfiche_document_find_duplicates`  | Scan a folder tree for byte-identical documents (size-then-hash, downloads almost nothing on a mostly-distinct tree) |
| `list_repositories`          | `laserfiche_repository_list`           | List repos for this account; falls back to the configured repo if endpoint disabled |
| `list_field_definitions`     | `laserfiche_field_definition_list`     | Enumerate all field definitions; pass `summary_only=true` for a `{count, names}` shape |
| `list_tag_definitions`       | `laserfiche_tag_definition_list`       | Enumerate tag definitions; supports `summary_only`                       |
| `list_template_definitions`  | `laserfiche_template_definition_list`  | Enumerate template definitions; supports `summary_only`                  |
| `list_link_definitions`      | `laserfiche_link_definition_list`      | Enumerate entry-link type definitions; supports `summary_only`           |
| `get_template_fields`        | `laserfiche_template_field_list`       | Atomic "what fields does this template need" lookup; pass `required_only=true` to filter to mandatory fields. Replaces the three-call chain (`list_template_definitions` → `list_field_definitions` → manual filter). |
| `get_audit_reasons`          | `laserfiche_audit_reason_list`         | Audit reasons available to the authenticated user (for delete/export)    |
| `get_task_status`            | `laserfiche_task_get_status`           | Poll the status of an async operation (delete, copy)                     |
| `wait_for_task`              | `laserfiche_task_wait`                 | Block until an async operation reaches a terminal state                  |
| `task_wait_or_poll`          | `laserfiche_task_update`               | Combines the two above: `timeout_seconds=0` polls once, `>0` blocks. Read-only despite the `_update` v2 name — it never mutates anything. |

## Writes (registered only when `LF_READ_ONLY=false`)

| Tool                | v2 name                              | Purpose                                                                          | Two-step token? |
| ------------------- | ------------------------------------ | -------------------------------------------------------------------------------- | --------------- |
| `set_fields`        | `laserfiche_field_set`               | OVERWRITE all field values on an entry (fields not in the body are deleted)      | —               |
| `merge_fields`      | `laserfiche_field_merge`             | GET-then-PUT helper: update specific fields, preserve the rest                   | —               |
| `field_update`      | `laserfiche_field_update`            | Collapses the two above: `mode="merge"` (default) or `mode="replace"`            | —               |
| `set_tags`          | `laserfiche_tag_set`                 | OVERWRITE all tags on an entry                                                   | —               |
| `merge_tags`        | `laserfiche_tag_merge`               | Add/remove specific tags without touching others                                 | —               |
| `tag_update`        | `laserfiche_tag_update`              | Collapses the two above: pass `add`/`remove` (merge) or `replace` (overwrite)    | —               |
| `set_links`         | `laserfiche_link_set`                | OVERWRITE all entry links                                                        | —               |
| `link_update`       | `laserfiche_link_update`             | Wraps `set_links`; `mode="merge"` does a GET-then-PUT union instead of overwrite | —               |
| `assign_template`   | `laserfiche_template_assign`         | Assign a template, optionally with initial field values (preflight-validated)    | —               |
| `remove_template`   | `laserfiche_template_remove`         | Clear the template assignment                                                    | —               |
| `template_assign_or_remove` | `laserfiche_template_update`  | Collapses the two above: `template_name=<name>` assigns, `template_name=None` clears | —           |
| `create_folder`     | `laserfiche_folder_create`           | Create a child folder under a parent                                             | —               |
| `import_document`   | `laserfiche_document_import`         | Multipart upload from a local file path; capped by `LF_IMPORT_MAX_BYTES`         | —               |
| `copy_entry`        | `laserfiche_entry_copy`              | Async copy via `CopyAsync`; returns an operation token to poll                   | —               |
| `rename_entry`      | `laserfiche_entry_rename`            | Rename an entry — preview shows old/new path, then re-call with the token        | yes             |
| `move_entry`        | `laserfiche_entry_move`              | Move (optionally rename) — fence applies to both source AND destination paths    | yes             |
| `delete_entry`      | `laserfiche_entry_delete`            | Delete an entry (folders cascade); preview shows child count + batch-cap status  | yes             |
| `delete_edoc`       | `laserfiche_document_edoc_delete`    | Wipe the electronic-document content; entry + metadata remain                    | yes             |
| `delete_pages`      | `laserfiche_document_pages_delete`   | Delete specific page ranges; refuses empty `page_range` (would mean "delete all") | yes             |

Tools with **two-step token** return a preview + HMAC-signed
`confirmation_token` on first call. Surface the preview to the user; on
go-ahead, re-call with the same arguments plus the token. Tokens are
bound to `(operation, entry_id, entry_name)` **and the operation's own
parameters** (`page_range` for `delete_pages`, `new_name` for
`rename_entry`, destination + name for `move_entry`) — executing with
different arguments than were previewed fails verification. They expire
after 5 minutes,
and are invalidated by server restart (unless `LF_CONFIRMATION_SECRET`
is set — see [Configuration](configuration.md)).

Each of the 5 two-step tools above is *also* registered as a
`..._preview` / `..._execute` pair — same behavior, split into two
single-purpose tool calls instead of one tool branching on whether
`confirmation_token` is set. Use whichever style your client's model
handles more reliably — both call the identical underlying logic.

| Tool                    | v2 name                                     |
| ------------------------ | -------------------------------------------- |
| `rename_entry_preview` / `rename_entry_execute`     | `laserfiche_entry_rename_preview` / `laserfiche_entry_rename_execute`     |
| `move_entry_preview` / `move_entry_execute`         | `laserfiche_entry_move_preview` / `laserfiche_entry_move_execute`         |
| `delete_entry_preview` / `delete_entry_execute`     | `laserfiche_entry_delete_preview` / `laserfiche_entry_delete_execute`     |
| `delete_edoc_preview` / `delete_edoc_execute`       | `laserfiche_document_edoc_delete_preview` / `laserfiche_document_edoc_delete_execute` |
| `delete_pages_preview` / `delete_pages_execute`     | `laserfiche_document_pages_delete_preview` / `laserfiche_document_pages_delete_execute` |

## Using `search_natural`

`search_entries` requires hand-written Laserfiche query syntax. If the
server rejects the query the only feedback the LLM gets is a generic HTTP
400 — there's nothing actionable to retry against. `search_natural` is the
LLM-friendly path:

1. **First call** — pass the user's question and (optionally) a
   `folder_path` to scope the answer; leave `lf_query` unset.
   The tool samples up to ten entries from that folder, returns
   the templates and field names it found, the Laserfiche search grammar
   reference, and 2–3 candidate query strings the LLM can choose from or
   refine.
2. **Second call** — same `question`, plus the chosen `lf_query`.
   On HTTP 400, the tool tries up to two automatic repairs (escape
   unescaped quotes inside values, then wildcard-wrap bare `Name=`
   values if `fuzzy=True`) before returning a structured error with all
   attempts visible so the LLM can author a fresh query.

The page-size cap for `search_natural` is the dedicated `LF_MAX_PAGE_SIZE`
env var (default 100) — some self-hosted SimpleSearches implementations
reject `$top` values above an internal limit, so this defaults lower than
the list/folder cap.

## Using `search_content`

The other search tools answer *which* entries matched. `search_content`
answers *what they say*, by returning the matched passages themselves —
page number, surrounding excerpt, and the exact substring that matched —
pulled from Laserfiche's full-text index, which is where OCR output for
scanned documents lives.

```
search_content(query="unpaid balance", folder_path="\Leases")
→ { "results": [
      { "entry_id": 7, "name": "lease-4821.pdf", "hit_count": 6,
        "hits": [ { "page": 4,
                    "text": "…tenant owes an unpaid balance of $2,400 as of March…",
                    "match": "unpaid balance" } ] } ] }
```

That answers most "what does this document say about X" questions without
downloading anything. Reach for `get_document_edoc(mode="text")` only when
an excerpt points you at the right document and you need more of it.

A bare phrase is wrapped into `{LF:Basic~="..."}` for you. Pass a query
starting with `{` to use raw syntax instead — e.g. `option="D"` to search
only OCR'd document text, or `option="DFANLT"` to permit leading and
trailing wildcards.

Two knobs control cost: `hits_for_top` (how many results get their
passages fetched — one extra request each, default 5) and `hits_per_entry`
(passages per result, default 3, capped by `LF_SEARCH_CONTEXT_HITS_MAX`).

This is the asynchronous `/Searches` flow rather than `SimpleSearches`,
which is what makes context hits available at all — they're keyed by a
search token that only the async flow produces. Laserfiche caps concurrent
searches per session (two, on v1), so the tool runs the whole
create→poll→read→close cycle inside one call and always releases the token,
including on timeout and failure. Older builds without the `/Searches`
endpoints get a structured `async_search_unavailable` error pointing at
`search_entries` as the fallback.

## `get_document_edoc` modes

On v1 servers the Laserfiche `Text` export endpoint doesn't exist, so
`get_document_text` cannot return anything. `get_document_edoc` gained a
`mode` parameter as the workaround:

| Mode      | Use it when                                                |
| --------- | ---------------------------------------------------------- |
| `info`    | You only need metadata (size, content-type). Default.      |
| `bytes`   | You want the raw file as base64 — capped at `LF_EDOC_MAX_BYTES` (25 MB by default; override per-call with `max_bytes`). |
| `text`    | You want extracted text. PDF, DOCX, PPTX, XLSX, EML, HTML, RTF and `text/*` are handled (format detected from content-type + entry name — octet-stream PDFs work). OCR is not attempted; for scans use `search_content`, which reads the OCR index. |

All tool descriptions are written to read like prompts — they tell the
model when to use the tool, valid input shapes, and what kind of follow-up
is expected. See [`src/laserfiche_mcp/server.py`](../src/laserfiche_mcp/server.py).
