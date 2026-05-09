"""Configuration loaded from environment variables.

Supports two deployment modes:
  - "self_hosted": Repository API Server (on-premise)
  - "cloud":      api.laserfiche.com (Laserfiche Cloud)

Self-hosted is the v1 focus; cloud config fields are reserved for v2.
"""

from __future__ import annotations

from enum import Enum

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DeploymentMode(str, Enum):
    SELF_HOSTED = "self_hosted"
    CLOUD = "cloud"


class AuthMode(str, Enum):
    BASIC = "basic"            # username + password (self-hosted)
    OAUTH = "oauth"            # LFDS OAuth (self-hosted) or cloud OAuth
    API_KEY = "api_key"        # cloud service principal


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
    repo_api_url: str | None = Field(
        default=None,
        description="Base URL of Repository API Server, e.g. https://lf.example.com/LFRepositoryAPI",
    )
    repository_id: str | None = Field(
        default=None,
        description="Repository name or ID.",
    )

    # --- Cloud (v2, reserved) ---
    cloud_region: str | None = Field(
        default=None,
        description="Cloud region: 'us', 'ca', 'eu'. Used in v2.",
    )

    # --- Auth ---
    auth_mode: AuthMode = Field(default=AuthMode.BASIC)
    username: str | None = None
    password: str | None = None
    client_id: str | None = None
    client_secret: str | None = None
    api_key: str | None = None

    # --- Safety ---
    read_only: bool = Field(
        default=True,
        description="When true, write tools are not registered. Default true for safety.",
    )
    max_results_default: int = Field(
        default=25,
        description="Default page size for list/search tools.",
    )
    max_results_ceiling: int = Field(
        default=200,
        description="Hard cap on page size regardless of caller-requested value.",
    )
    request_timeout_seconds: float = Field(default=30.0)

    # --- Validation ---
    @model_validator(mode="after")
    def _validate_required_for_mode(self) -> Settings:
        if self.deployment_mode is DeploymentMode.SELF_HOSTED:
            missing = [
                name for name, value in {
                    "LF_REPO_API_URL": self.repo_api_url,
                    "LF_REPOSITORY_ID": self.repository_id,
                }.items() if not value
            ]
            if missing:
                raise ValueError(
                    f"Missing required environment variables for self-hosted mode: "
                    f"{', '.join(missing)}"
                )

            if self.auth_mode is AuthMode.BASIC and not (self.username and self.password):
                raise ValueError(
                    "auth_mode=basic requires LF_USERNAME and LF_PASSWORD."
                )

        if self.deployment_mode is DeploymentMode.CLOUD:
            raise NotImplementedError(
                "Cloud deployment mode is reserved for v2. "
                "Set LF_DEPLOYMENT_MODE=self_hosted for now."
            )

        return self
