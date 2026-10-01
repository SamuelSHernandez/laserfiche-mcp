# Command line

Everything the MCP tools do against the repository, the same binary does from
a shell — same code path, no LLM in the loop. Use it for the work that has one
correct answer: inventories, downloads, dedupe, grepping a contract.

```bash
laserfiche-mcp setup                           # one-time: save connection details
laserfiche-mcp ls 1                            # list a folder (alias: list)
laserfiche-mcp ls '\HR\Leases'                 # ...or by path
laserfiche-mcp get 4821 --to ./lease.pdf       # stream a document to disk
laserfiche-mcp cat 4821                        # extracted text on stdout
laserfiche-mcp cat 4821 --pages 4-9            # just those pages
laserfiche-mcp find 4821 'unpaid balance'      # grep one document
laserfiche-mcp search 'unpaid balance'         # grep the whole repository
laserfiche-mcp manifest 1 --out inventory.csv  # walk a tree, write CSV
laserfiche-mcp dedupe 1                        # byte-identical documents
laserfiche-mcp diff 4821 4822                  # compare two entries (alias: compare)
```

Office-friendly synonyms work everywhere: `list`, `read` (cat), `download`
(get), `compare` (diff), `duplicates` (dedupe). On Windows, quote paths
with double quotes: `laserfiche-mcp list "\HR\Leases"`.

`ENTRY` and `FOLDER` accept a numeric entry ID or a repository path. Every
command takes `--json`, so the same invocation backs a shell pipeline:

```bash
laserfiche-mcp search 'termination' --json | jq -r '.results[].name'
laserfiche-mcp manifest 1 --json | jq '.by_extension'
laserfiche-mcp manifest 1 --out tree.jsonl --format jsonl && jq -r 'select(.extension=="pdf").name' tree.jsonl
```

Conventions that make these scriptable:

- Results go to **stdout**; progress and warnings go to **stderr**.
- Exit codes are meaningful: `0` success, `1` the operation failed or found
  nothing, `2` bad usage. `laserfiche-mcp find 4821 'indemnify' >/dev/null &&
  echo present` does what you would expect.
- Nothing here writes to the repository. The subcommands are read-only by
  construction and never register the write tools.

## What each command saves you

| Command | Instead of | Why it is cheaper |
| --- | --- | --- |
| `get --to` | a base64 blob in a tool result | Streamed to disk; the bytes never enter a context window, and never sit in RAM either |
| `cat` / `find` | reading a whole document into the model | Extraction and matching happen locally; you get the passage, not the file |
| `search` | opening each hit to see what it says | Laserfiche's own OCR index returns the matched passages |
| `manifest` | one `list_folder` call per subfolder | One walk, one CSV, a summary you can read at a glance |
| `dedupe` | downloading everything to compare | Sizes are probed first; only size collisions are ever downloaded |
| `diff` | eyeballing two records side by side | A set comparison with one correct answer |

## Document formats

`cat`, `find`, and the MCP's `mode="text"` read these with **no extra
dependencies**: PDF, DOCX, PPTX, EML, HTML, RTF, and anything `text/*`
(including CSV, JSON, XML, Markdown).

Two more need the optional extra:

```bash
pip install 'laserfiche-mcp[office]'   # adds .xlsx and Outlook .msg
```

Legacy binary `.doc` / `.xls` / `.ppt` are not supported — correct extraction
needs an external converter such as LibreOffice, and bundling that would break
the "pip install and it works" promise. Re-save as OOXML, or read Laserfiche's
own indexed text with `search`.

Scanned images have no text layer at all. `cat` says so explicitly rather than
returning an empty string, and points you at `search`, which reads the OCR
index where that text actually lives.
