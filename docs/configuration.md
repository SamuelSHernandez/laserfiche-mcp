# Configuration

Everything is configured with `LF_*` environment variables (or the `laserfiche-mcp setup` wizard, which writes them to a file for you).

Copy the example file and fill in your repository details:

```bash
cp .env.example .env
$EDITOR .env
```

Minimum required variables for self-hosted password-grant auth:

| Variable             | Example                                       |
| -------------------- | --------------------------------------------- |
| `LF_REPO_API_URL`    | `https://lf.example.com/LFRepositoryAPI`      |
| `LF_REPOSITORY_ID`   | `my-repo`                                     |
| `LF_API_VERSION`     | `v1` (default) or `v2` — see below            |
| `LF_USERNAME`        | `service-account`                             |
| `LF_PASSWORD`        | (your service account password)               |
| `LF_AUTH_MODE`       | `password`                                    |
| `LF_READ_ONLY`       | `true` (default — see Writes section below)   |

**Optional — web-client links.** Unset by default, so search/read tools
never emit `web_url`. Cannot be derived from `LF_REPO_API_URL` — copy it by
hand from your own Laserfiche web client (open a document, copy the browser
URL, substitute the entry ID with `{entry_id}`). See `.env.example` for
worked examples. A returned link grants no access by itself: opening it
still requires the *viewer's own* Laserfiche web-client login and is
subject to the repository's entry-level ACLs — useful for staff who have
Laserfiche accounts, useless to an end user who doesn't.

| Variable                            | Default | Purpose                                                                          |
| ------------------------------------ | ------- | -------------------------------------------------------------------------------- |
| `LF_WEB_CLIENT_URL_TEMPLATE`         | unset   | URL template for a Document viewer link, e.g. `https://lf.example.com/Laserfiche/DocView.aspx?repo={repo_id}&id={entry_id}` |
| `LF_WEB_CLIENT_FOLDER_URL_TEMPLATE`  | unset   | Same, for Folder/RecordSeries entries — most web clients browse on a different page than they view a document on |

**Optional write-mode variables** (fences and allowlists default off; the delete batch cap and required-field validation default on — see the [Safety model](safety.md) for context):

| Variable                            | Default | Purpose                                                                          |
| ----------------------------------- | ------- | -------------------------------------------------------------------------------- |
| `LF_READ_ONLY`                      | `true`  | Set `false` to register the write tools                                          |
| `LF_WRITE_PATHS_ALLOW`              | unset   | Comma-separated path prefixes where writes are permitted (case-insensitive)      |
| `LF_WRITE_PATHS_DENY`               | unset   | Comma-separated path prefixes where writes are refused (deny wins over allow)    |
| `LF_WRITE_TOOLS_ALLOWED`            | unset   | Comma-separated write-tool names to scope what registers; e.g. metadata-only     |
| `LF_DELETE_FOLDER_MAX_DESCENDANTS`  | `50`    | Refuse folder deletes above this immediate-child count unless `force_large_delete=true` |
| `LF_REQUIRE_AUDIT_REASON`           | `false` | When `true`, `delete_entry` refuses to execute without `audit_reason_id`         |
| `LF_VALIDATE_REQUIRED_FIELDS`       | `true`  | Validate the target template's own required fields (fields with a `defaultValue` are skipped) client-side before `assign_template` PUTs |
| `LF_VALIDATE_NAMES`                 | `true`  | Pre-flight field / tag / template / link-type names against cached schema definitions; returns `invalid_*_name` instead of an opaque 400 |
| `LF_SCHEMA_CACHE_TTL_SECONDS`       | `300`   | Cache window for the schema-definition lookups that back `LF_VALIDATE_NAMES` and `LF_VALIDATE_REQUIRED_FIELDS`. Set to `0` to disable caching. |
| `LF_IMPORT_MAX_BYTES`               | `25 MB` | Client-side cap on `import_document` payload size                                |
| `LF_IMPORT_SOURCE_DIRS`             | unset   | Comma-separated local directories `import_document` may read `file_path` from (symlinks resolved before comparison). Unset: any path the MCP process can read is accepted. |
| `LF_EDOC_MAX_BYTES`                 | `25 MB` | Cap on `get_document_edoc` downloads in `bytes`/`text` modes                     |
| `LF_SEARCH_TIMEOUT_SECONDS`         | `60`    | How long `search_content` waits for an async search before abandoning it         |
| `LF_SEARCH_POLL_INTERVAL_SECONDS`   | `1`     | Delay between `search_content` status polls; backs off toward 2s on long searches |
| `LF_SEARCH_CONTEXT_HITS_MAX`        | `10`    | Hard cap on matched passages returned per entry by `search_content`              |
| `LF_LEGACY_TOOL_NAMES`              | `false` | Set `true` to also register the v1.x verb-first aliases alongside the v2 names. Default changed to `false` in v2.3.0 — registering both roughly doubles the per-request tool catalog; set `true` if you still call tools by their legacy names |
| `LF_CONFIRMATION_SECRET`            | unset   | Optional secret the destructive-op confirmation tokens are signed with. Unset: random per-process key, so a **restart invalidates pending preview tokens** (the safer single-instance default). Set it to keep tokens valid across restarts / across instances sharing the secret. Treat like a password. |
| `LF_LOG_FORMAT`                     | `text`  | `json` emits one JSON object per log line (for jq / Datadog / Splunk)            |

See [`.env.example`](../.env.example) for the full list including OAuth
config, pagination limits, request timeout, retry attempts, and SSL
verification.

> **API version note:** LFRepositoryAPI ships with different routing
> surfaces across builds. Older self-hosted installs expose `/v1/...`
> paths; newer ones expose `/v2/...`. Probe your server with:
>
> ```
> curl {LF_REPO_API_URL}/v1/Repositories
> curl {LF_REPO_API_URL}/v2/Repositories
> ```
>
> Whichever returns a `200` with a JSON repo list is your version.
> If the wrong value is set, every call fails with
> `400 UnsupportedApiVersion`. The default is `v1` because that is what
> most current on-prem installations expose.

> **Auth note:** Laserfiche self-hosted does not accept HTTP Basic auth.
> The server exchanges your username/password for a bearer token at
> `POST /{api_version}/Repositories/{repository_id}/Token` on first
> request and refreshes it automatically before expiry. The same flow
> works on both v1 and v2.

> [!WARNING]
> **Laserfiche Cloud (`LF_DEPLOYMENT_MODE=cloud` / `LF_AUTH_MODE=api_key`) is
> BETA — never verified against a live Cloud tenant.** It's implemented
> against Laserfiche's documented service-app flow and cross-checked
> against Laserfiche's own open-source client library
> ([`lf-api-client-core-dotnet`](https://github.com/Laserfiche/lf-api-client-core-dotnet)) —
> the JWT assertion this server builds is unit-tested to be byte-for-byte
> compatible with that library's own test vector, so the *cryptography* is
> right. What's unverified is the network round-trip: whether
> `signin.laserfiche.com` actually accepts that assertion and whether
> `api.laserfiche.com/repository/v2/...` behaves identically to self-hosted
> v2 for every endpoint this server wraps. Set `LF_REPO_API_URL` to
> `https://api.laserfiche.com/repository` (or your tenant's region, e.g.
> `https://api.eu.laserfiche.com/repository`), `LF_API_VERSION=v2`,
> `LF_CLOUD_ACCESS_KEY` (the base64 access-key blob from the Developer
> Console) and `LF_CLOUD_SERVICE_PRINCIPAL_KEY` (from Account
> Administration) — see `.env.example`. If you have Cloud access and try
> this, please open an issue with what worked or broke.
