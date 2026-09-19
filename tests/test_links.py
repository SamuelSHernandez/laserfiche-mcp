"""Tests for ``links.py`` — web-client viewer URL construction."""

from __future__ import annotations

import pytest

from laserfiche_mcp.config import Settings
from laserfiche_mcp.links import (
    attach_web_url,
    attach_web_urls,
    build_web_url,
    validate_web_client_url_template,
    web_url_for,
)


def _settings(**overrides: object) -> Settings:
    return Settings(  # type: ignore[call-arg]
        deployment_mode="self_hosted",
        repo_api_url="https://lf.example.test/LFRepositoryAPI",
        repository_id="demo",
        username="svc",
        password="secret",
        **overrides,
    )


# --- validate_web_client_url_template ----------------------------------------


def test_validate_rejects_non_http_scheme() -> None:
    with pytest.raises(ValueError, match="http"):
        validate_web_client_url_template("ftp://lf.example.com/{entry_id}", field_name="X")


def test_validate_requires_entry_id_placeholder() -> None:
    with pytest.raises(ValueError, match="entry_id"):
        validate_web_client_url_template("https://lf.example.com/view", field_name="X")


def test_validate_rejects_unknown_placeholder() -> None:
    with pytest.raises(ValueError, match="entry_name"):
        validate_web_client_url_template(
            "https://lf.example.com/{entry_id}/{entry_name}", field_name="X"
        )


def test_validate_accepts_entry_id_and_repo_id() -> None:
    validate_web_client_url_template(
        "https://lf.example.com/DocView.aspx?repo={repo_id}&id={entry_id}",
        field_name="X",
    )


# --- Settings integration -----------------------------------------------------


def test_settings_rejects_bad_web_client_template(lf_env: dict[str, str]) -> None:
    with pytest.raises(ValueError, match="LF_WEB_CLIENT_URL_TEMPLATE"):
        _settings(web_client_url_template="not-a-url")


def test_settings_accepts_unset_templates(lf_env: dict[str, str]) -> None:
    settings = _settings()
    assert settings.web_client_url_template is None
    assert settings.web_client_folder_url_template is None


# --- build_web_url -------------------------------------------------------------


def test_build_web_url_substitutes_placeholders() -> None:
    url = build_web_url(
        "https://lf.example.com/DocView.aspx?repo={repo_id}&id={entry_id}",
        entry_id=4711,
        repo_id="Main Repo",
    )
    assert url == "https://lf.example.com/DocView.aspx?repo=Main%20Repo&id=4711"


def test_build_web_url_handles_missing_repo_id() -> None:
    url = build_web_url("https://lf.example.com/view/{entry_id}", entry_id=1, repo_id=None)
    assert url == "https://lf.example.com/view/1"


# --- web_url_for ---------------------------------------------------------------


def test_web_url_for_document_uses_document_template(lf_env: dict[str, str]) -> None:
    settings = _settings(
        web_client_url_template="https://lf.example.com/doc/{entry_id}",
        web_client_folder_url_template="https://lf.example.com/folder/{entry_id}",
    )
    assert web_url_for("Document", 5, settings) == "https://lf.example.com/doc/5"


def test_web_url_for_folder_uses_folder_template(lf_env: dict[str, str]) -> None:
    settings = _settings(
        web_client_url_template="https://lf.example.com/doc/{entry_id}",
        web_client_folder_url_template="https://lf.example.com/folder/{entry_id}",
    )
    assert web_url_for("Folder", 5, settings) == "https://lf.example.com/folder/5"
    assert web_url_for("RecordSeries", 5, settings) == "https://lf.example.com/folder/5"


def test_web_url_for_folder_without_folder_template_returns_none(lf_env: dict[str, str]) -> None:
    settings = _settings(web_client_url_template="https://lf.example.com/doc/{entry_id}")
    assert web_url_for("Folder", 5, settings) is None


def test_web_url_for_shortcut_returns_none(lf_env: dict[str, str]) -> None:
    settings = _settings(
        web_client_url_template="https://lf.example.com/doc/{entry_id}",
        web_client_folder_url_template="https://lf.example.com/folder/{entry_id}",
    )
    assert web_url_for("Shortcut", 5, settings) is None


def test_web_url_for_returns_none_when_unconfigured(lf_env: dict[str, str]) -> None:
    settings = _settings()
    assert web_url_for("Document", 5, settings) is None


# --- attach_web_url / attach_web_urls ------------------------------------------


def test_attach_web_url_adds_key_when_configured(lf_env: dict[str, str]) -> None:
    settings = _settings(web_client_url_template="https://lf.example.com/doc/{entry_id}")
    entry = {"id": 7, "entry_type": "Document", "name": "x.pdf"}
    attach_web_url(entry, settings=settings)
    assert entry["web_url"] == "https://lf.example.com/doc/7"


def test_attach_web_url_omits_key_when_unconfigured(lf_env: dict[str, str]) -> None:
    settings = _settings()
    entry = {"id": 7, "entry_type": "Document", "name": "x.pdf"}
    attach_web_url(entry, settings=settings)
    assert "web_url" not in entry


def test_attach_web_url_uses_custom_id_key(lf_env: dict[str, str]) -> None:
    settings = _settings(web_client_url_template="https://lf.example.com/doc/{entry_id}")
    entry = {"entry_id": 9, "entry_type": "Document"}
    attach_web_url(entry, settings=settings, id_key="entry_id")
    assert entry["web_url"] == "https://lf.example.com/doc/9"


def test_attach_web_urls_applies_to_each_entry(lf_env: dict[str, str]) -> None:
    settings = _settings(web_client_url_template="https://lf.example.com/doc/{entry_id}")
    entries = [
        {"id": 1, "entry_type": "Document"},
        {"id": 2, "entry_type": "Folder"},
    ]
    attach_web_urls(entries, settings=settings)
    assert entries[0]["web_url"] == "https://lf.example.com/doc/1"
    assert "web_url" not in entries[1]
