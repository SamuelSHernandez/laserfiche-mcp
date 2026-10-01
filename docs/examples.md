# Examples

Copy-and-paste starting points. Entry IDs, paths and names below are made up —
swap in your own.

- [Things to ask Claude](#things-to-ask-claude)
- [Searching](#searching)
- [Command-line recipes](#command-line-recipes)
- [Making changes safely](#making-changes-safely)
- [Config snippets](#config-snippets)

## Things to ask Claude

Plain English works. The tool Claude will reach for is in parentheses.

**Find things**

- *"What's in the Onboarding folder?"* (`list_folder`)
- *"Find every PDF with 'Acme' in the name under \Contracts."* (`search_by_name`)
- *"Find contracts that mention an auto-renewal clause."* (`search_content`)

**Read things**

- *"Summarize entry 4821."* (`get_document_edoc`, `mode="text"`)
- *"Show me pages 4–9 of the Henderson lease."* (`get_document_edoc` with `pages`)
- *"What template and field values does entry 4821 have?"* (`get_entry`, `get_field_values`)

**Compare and clean up**

- *"Are entries 4821 and 4822 the same document, or a real difference?"* (`compare_entries`)
- *"Find duplicate documents under \Accounting\2023."* (`find_duplicate_documents`)

**Change things** (needs `LF_READ_ONLY=false` — see [Making changes safely](#making-changes-safely))

- *"Create a folder called 'Q4 Reports' under \Finance."* (`create_folder`)
- *"Set Status to Approved on entry 4821."* (`field_update`)
- *"Move entry 4821 into the Archive folder."* (`move_entry`)

## Searching

Which tool to use:

| You want | Use | Example |
|---|---|---|
| Passages from inside documents (works on scans, via OCR) | `search_content` | `search_content(query="unpaid balance", folder_path="\Leases")` |
| Files by name | `search_by_name` | `search_by_name(name_pattern="*.pdf", in_folder_path="\Contracts")` |
| A precise Laserfiche query | `search_entries` | `search_entries(...)` with `{LF:Name="*.pdf"}` |
| Claude to write the query for you | `search_natural` | Claude asks the server for templates first, then runs and auto-repairs the query |

`search_content` returns the page and surrounding text of each match, so Claude
can usually answer without opening the document:

```json
{ "results": [
  { "entry_id": 7, "name": "lease-4821.pdf", "hit_count": 6,
    "hits": [ { "page": 4,
                "text": "…tenant owes an unpaid balance of $2,400 as of March…",
                "match": "unpaid balance" } ] } ] }
```

A plain phrase is searched as-is. Start a query with `{` to use raw Laserfiche
syntax, e.g. `{LF:Basic~="unpaid balance"}`.

**Scanned documents** have no text layer to extract, so
`get_document_edoc(mode="text")` can't read them. Use `search_content` — it
reads Laserfiche's OCR index.

More: [Tools reference](tools.md).

## Command-line recipes

No AI involved; these run straight against your repository. Every command
accepts `--json`, and `ENTRY` / `FOLDER` can be an ID or a path.

```bash
# Look around
laserfiche-mcp ls 1                            # repository root
laserfiche-mcp ls '\HR\Leases'                 # by path (Windows: use "double quotes")

# Get a document, or just its text
laserfiche-mcp get 4821 --to ./lease.pdf       # download
laserfiche-mcp cat 4821                        # text to the terminal
laserfiche-mcp cat 4821 --pages 4-9            # just those pages
laserfiche-mcp cat 4821 > lease.txt            # save the text

# Search
laserfiche-mcp search 'unpaid balance'         # whole repository, with excerpts
laserfiche-mcp find 4821 'indemnify'           # inside one document

# Inventory and clean-up
laserfiche-mcp manifest 1 --out inventory.csv  # CSV of a whole tree
laserfiche-mcp dedupe '\Accounting\2023'       # byte-identical documents
laserfiche-mcp diff 4821 4822                  # what differs between two entries
```

Use it in scripts. Results go to stdout, messages to stderr, and the exit code
is `0` = success, `1` = failed or nothing found, `2` = bad usage:

```bash
# names of every hit
laserfiche-mcp search 'termination' --json | jq -r '.results[].name'

# count documents by file type
laserfiche-mcp manifest 1 --json | jq '.by_extension'

# act only if a document mentions something
laserfiche-mcp find 4821 'indemnify' >/dev/null && echo "has indemnity clause"

# one JSON object per line, filtered
laserfiche-mcp manifest 1 --out tree.jsonl --format jsonl
jq -r 'select(.extension=="pdf").name' tree.jsonl
```

More: [Command line reference](cli.md).

## Making changes safely

Writes are off by default. A cautious way to turn them on is to start in a
sandbox folder you created by hand in the Laserfiche web client (the fence
needs an existing folder), with only the tools you need — see
[config snippets](#config-snippets) below.

**Deletes, renames and moves take two steps.** Claude first gets a *preview*
and a short-lived token; nothing changes until it repeats the call with that
token:

```text
delete_entry(entry_id=4821)
→ preview: "\Sandbox\Old Drafts" (folder, 12 items) + confirmation_token

  Claude shows you the preview and asks.

delete_entry(entry_id=4821, confirmation_token="…")
→ deleted
```

The token only works for the exact operation that was previewed (same entry,
same arguments) and expires after 5 minutes.

**Changing field values** — `field_update` merges by default, leaving other
fields alone:

```text
field_update(entry_id=4821, updates={"Status": ["Approved"]})
field_update(entry_id=4821, updates={"Note": []})            # clears one field
field_update(entry_id=4821, updates={...}, mode="replace")   # clears every field not listed
```

All guardrails: [Safety model](safety.md).

## Config snippets

These go in the `env` block of your MCP client config (see
[Connect to Claude](../README.md#connect-to-claude)). Add them to the
connection settings (`LF_REPO_API_URL`, `LF_REPOSITORY_ID`, credentials).

**Read-only** (the default — nothing to set)

```json
"LF_READ_ONLY": "true"
```

**Writes, fenced to a sandbox**

```json
"LF_READ_ONLY": "false",
"LF_WRITE_PATHS_ALLOW": "\\Sandbox\\mcp-test",
"LF_WRITE_TOOLS_ALLOWED": "create_folder,import_document,merge_fields,merge_tags,assign_template,delete_entry",
"LF_DELETE_FOLDER_MAX_DESCENDANTS": "10"
```

**Metadata edits only** (can't create, move or delete anything)

```json
"LF_READ_ONLY": "false",
"LF_WRITE_TOOLS_ALLOWED": "merge_fields,merge_tags,assign_template"
```

**Links back to the Laserfiche web client in search results** — see the
`LF_WEB_CLIENT_URL_TEMPLATE` setting in [Configuration](configuration.md).

**Web clients (claude.ai, ChatGPT)** — see [Remote HTTP](remote-http.md).
