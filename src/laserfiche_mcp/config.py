"""Configuration loaded from environment variables.

Supports two deployment modes:
  - "self_hosted": Repository API Server (on-premise)
  - "cloud":      api.laserfiche.com (Laserfiche Cloud)

Self-hosted is the v1 focus; cloud config is reserved for v2.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field, HttpUrl, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DeploymentMode(str, Enum):
    SELF_HOSTED = "self_hosted"
    CLOUD = "cloud"


class AuthMode(str, Enum):
    # username + password → /{api_version}/Repositories/{repo}/Token bearer (self-hosted, default)
    PASSWORD = "password"
    OAUTH = "oauth"            # client_credentials grant (LFDS or compatible)
    API_KEY = "api_key"        # cloud service principal with JWT (reserved for v2)


class ApiVersion(str, Enum):
    # Older self-hosted LFRepositoryAPI builds (e.g. installations pinned to the
    # v1 API surface). Uses /v1/ paths, lowercase `fields`/`children` segments,
    # and OData entity-type segments (Laserfiche.Repository.Folder, .Document)
    # in URLs. No unified /Export endpoint — only raw edoc bytes.
    V1 = "v1"
    # Newer self-hosted LFRepositoryAPI builds. Uses /v2/ paths, PascalCase
    # `Fields`/`Children` segments, and a unified /Export endpoint that can
    # return Edoc, Text (extracted), or Image parts.
    V2 = "v2"


class Settings(BaseSettings):
    """Runtime configuration. All fields read from LF_* env vars."""

    model_config = SettingsConfigDict(
        env_prefix="LF_",
        env_file=".env",
        extra="ignore",
    )

    # --- Deployment ---
    deployment_mode: DeploymentMode = Field(
        default=DeploymentMode.SELF_HOSTED,
        description="Which Laserfiche API to target.",
    )

    # --- Self-hosted ---
    repo_api_url: HttpUrl | None = Field(
        default=None,
        description="Base URL of Repository API Server, e.g. https://lf.example.com/LFRepositoryAPI",
    )
    repository_id: str | None = Field(
        default=None,
        description="Repository name or ID.",
    )
    api_version: ApiVersion = Field(
        default=ApiVersion.V1,
        description="LFRepositoryAPI version exposed by your server: 'v1' for "
        "older self-hosted builds, 'v2' for newer ones. Probe by GETting "
        "`{LF_REPO_API_URL}/v1/Repositories` vs `/v2/Repositories` — whichever "
        "returns 200 is your version. Defaults to v1 because that is what most "
        "current on-prem installations expose.",
    )

    # --- Auth ---
    auth_mode: AuthMode = Field(default=AuthMode.PASSWORD)
    username: str | None = None
    password: SecretStr | None = None

    # OAuth (client_credentials grant)
    oauth_token_url: HttpUrl | None = None
    oauth_scope: str | None = None
    client_id: str | None = None
    client_secret: SecretStr | None = None

    # Cloud (reserved for v2)
    api_key: SecretStr | None = None

    # --- Network ---
    verify_ssl: bool = Field(
        default=True,
        description="Verify the server's TLS certificate. Set false only for "
        "self-signed cert dev environments — emits a warning when disabled.",
    )
    request_timeout_seconds: float = Field(default=30.0, gt=0)
    retry_attempts: int = Field(
        default=3, ge=0, le=10,
        description="Number of times to retry transient failures (5xx, 429, "
        "connection errors) with exponential backoff.",
    )

    # --- Behavior ---
    read_only: bool = Field(
        default=True,
        description="When true, write tools are not registered. Default true for safety.",
    )
    write_paths_allow: str | None = Field(
        default=None,
        description="Comma-separated repository path prefixes; when set, "
        "writes are permitted ONLY on entries whose fullPath starts with one "
        "of these prefixes (case-insensitive). Backslashes are normalized. "
        "Example: '\\\\Imports\\\\Pending,\\\\Archive\\\\WIP'. Combine with "
        "LF_WRITE_PATHS_DENY for an allow-then-deny pattern.",
    )
    write_paths_deny: str | None = Field(
        default=None,
        description="Comma-separated repository path prefixes; writes are "
        "REFUSED on any entry whose fullPath starts with one of these "
        "prefixes. Deny wins over allow.",
    )
    delete_folder_max_descendants: int = Field(
        default=50,
        ge=0,
        description="Maximum number of immediate children a folder may have "
        "before delete_entry refuses to execute without force_large_delete=true. "
        "Set to 0 to require force_large_delete on every folder delete.",
    )
    write_tools_allowed: str | None = Field(
        default=None,
        description="Comma-separated write-tool names; when set, only these "
        "write tools register (assuming LF_READ_ONLY=false). Useful for "
        "scope-limited deployments — e.g. 'merge_fields,merge_tags,"
        "assign_template' for a metadata-only setup.",
    )
    require_audit_reason: bool = Field(
        default=False,
        description="When true, delete_entry refuses to execute without an "
        "audit_reason_id. Use get_audit_reasons to enumerate valid IDs.",
    )
    validate_required_fields: bool = Field(
        default=True,
        description="When true, assign_template (and related entry-creation "
        "tools assigning a template) validates required-field constraints "
        "client-side and returns a structured error listing missing fields "
        "before the API call. Disable on builds where the extra reads are "
        "wasted (e.g. repositories with no required fields).",
    )
    schema_cache_ttl_seconds: int = Field(
        default=300,
        ge=0,
        description="Cache window for schema-definition lookups "
        "(field/tag/template/link definitions) used by client-side "
        "pre-flight validation. 0 disables caching (lookups hit the "
        "server on every call). On schemas that change frequently, "
        "lower this; on stable schemas, raise it.",
    )
    validate_names: bool = Field(
        default=True,
        description="When true, write tools pre-flight tag / template / "
        "field / link-type names against the repository's cached schema "
        "definitions before the API call, returning structured "
        "invalid_*_name errors instead of letting the server reject "
        "with an opaque 400. Disable on repos with high schema churn "
        "or to skip the extra reads.",
    )
    max_results_default: int = Field(
        default=25,
        ge=1, le=500,
        description="Default page size for list/search tools.",
    )
    max_results_ceiling: int = Field(
        default=200,
        ge=1, le=1000,
        description="Hard cap on page size regardless of caller-requested value.",
    )
    max_page_size: int = Field(
        default=100,
        ge=1, le=1000,
        description="Hard cap on page size for search_natural specifically. "
        "Some self-hosted servers reject SimpleSearches $top values above "
        "their internal limit; this defaults lower than max_results_ceiling.",
    )
    edoc_max_bytes: int = Field(
        default=25_000_000,
        ge=1,
        description="Maximum edoc size in bytes that get_document_edoc will "
        "fetch when mode is 'bytes' or 'text'. Larger entries return a "
        "structured cap error instead of being downloaded.",
    )
    import_max_bytes: int = Field(
        default=25_000_000,
        ge=1,
        description="Maximum file size in bytes accepted by import_document. "
        "The API itself enforces a 100 MB cap; this client-side cap defaults "
        "lower to keep upload latency predictable. Override per-call by "
        "raising this env var.",
    )
    log_level: str = Field(
        default="INFO",
        description="Python logging level for the server (DEBUG, INFO, WARNING, ERROR).",
    )

    # --- Validation ---
    @model_validator(mode="after")
    def _validate(self) -> Settings:
        if self.deployment_mode is DeploymentMode.CLOUD:
            raise NotImplementedError(
                "Cloud deployment mode is reserved for v2. "
                "Set LF_DEPLOYMENT_MODE=self_hosted for now."
            )

        missing: list[str] = []
        if not self.repo_api_url:
            missing.append("LF_REPO_API_URL")
        if not self.repository_id:
            missing.append("LF_REPOSITORY_ID")

        if self.auth_mode is AuthMode.PASSWORD:
            if not self.username:
                missing.append("LF_USERNAME")
            if not self.password:
                missing.append("LF_PASSWORD")
        elif self.auth_mode is AuthMode.OAUTH:
            if not self.oauth_token_url:
                missing.append("LF_OAUTH_TOKEN_URL")
            if not self.client_id:
                missing.append("LF_CLIENT_ID")
            if not self.client_secret:
                missing.append("LF_CLIENT_SECRET")
        elif self.auth_mode is AuthMode.API_KEY:
            raise NotImplementedError(
                "api_key auth (cloud JWT-signed assertion) is reserved for v2. "
                "Use LF_AUTH_MODE=password or oauth."
            )

        if missing:
            raise ValueError(
                "Missing required environment variables: "
                + ", ".join(missing)
                + ". See .env.example for the full list."
            )

        if self.max_results_default > self.max_results_ceiling:
            raise ValueError(
                "LF_MAX_RESULTS_DEFAULT must be <= LF_MAX_RESULTS_CEILING "
                f"(got {self.max_results_default} > {self.max_results_ceiling})."
            )

        valid_levels = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if self.log_level.upper() not in valid_levels:
            raise ValueError(
                f"LF_LOG_LEVEL must be one of {sorted(valid_levels)}, "
                f"got {self.log_level!r}."
            )

        return self
