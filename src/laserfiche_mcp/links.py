"""Laserfiche web-client viewer links.

Builds a clickable URL into the Laserfiche web client for an entry, from an
operator-configured template (``LF_WEB_CLIENT_URL_TEMPLATE`` /
``LF_WEB_CLIENT_FOLDER_URL_TEMPLATE``). The Repository API host/path this
server otherwise talks to is not the web client's host/path, and the URL
scheme itself differs across Laserfiche products and versions — so the
template is required, opt-in configuration, never derived from
``LF_REPO_API_URL``.

A returned link confers no access by itself: opening it still requires the
viewer's own Laserfiche web-client login and is subject to the repository's
entry-level ACLs, independent of this server's service-account credentials.
"""

from __future__ import annotations

import string
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from .models import EntryType

if TYPE_CHECKING:
    from .config import Settings

_ALLOWED_PLACEHOLDERS = {"entry_id", "repo_id"}

# Entry types the web client has a dedicated viewer page for. Shortcuts are
# excluded — target resolution is web-client-specific and not worth guessing.
_DOCUMENT_TYPES = {EntryType.DOCUMENT.value}
_FOLDER_TYPES = {EntryType.FOLDER.value, EntryType.RECORD_SERIES.value}


def validate_web_client_url_template(template: str, *, field_name: str) -> None:
    """Raise ``ValueError`` if ``template`` isn't a usable URL template.

    Requires an http(s) URL containing ``{entry_id}``; the only other
    allowed format placeholder is ``{repo_id}``.
    """
    if not template.startswith(("http://", "https://")):
        raise ValueError(f"{field_name} must start with http:// or https://, got {template!r}.")

    for _, field, _, _ in string.Formatter().parse(template):
        if field is not None and field not in _ALLOWED_PLACEHOLDERS:
            raise ValueError(
                f"{field_name} has unsupported placeholder {{{field}}}. "
                f"Allowed placeholders: {', '.join(sorted(_ALLOWED_PLACEHOLDERS))}."
            )

    if "{entry_id}" not in template:
        raise ValueError(f"{field_name} must contain {{entry_id}}.")


def build_web_url(template: str, *, entry_id: int, repo_id: str | None) -> str:
    """Render ``template`` for a specific entry. Assumes ``template`` is valid."""
    return template.format(entry_id=entry_id, repo_id=quote(repo_id, safe="") if repo_id else "")


def web_url_for(
    entry_type: EntryType | str | None,
    entry_id: int,
    settings: Settings,
) -> str | None:
    """Return a web-client viewer URL for ``entry_id``, or ``None``.

    ``None`` when no template is configured for ``entry_type``'s category,
    or ``entry_type`` isn't a Document, Folder, or RecordSeries (Shortcut,
    Unknown).
    """
    value = entry_type.value if isinstance(entry_type, EntryType) else entry_type
    if value in _DOCUMENT_TYPES:
        template = settings.web_client_url_template
    elif value in _FOLDER_TYPES:
        template = settings.web_client_folder_url_template
    else:
        return None

    if not template:
        return None

    return build_web_url(template, entry_id=entry_id, repo_id=settings.repository_id)


def attach_web_url(entry: dict[str, Any], *, settings: Settings, id_key: str = "id") -> None:
    """Add a ``web_url`` key to ``entry`` in place, if configured and applicable.

    ``entry`` is a plain dict already produced by ``model_dump()`` — never
    attach to a Pydantic model or before it's dumped, since the entry models
    don't declare a ``web_url`` field and would silently drop it.
    """
    entry_id = entry.get(id_key)
    entry_type = entry.get("entry_type")
    if entry_id is None or entry_type is None:
        return
    url = web_url_for(entry_type, entry_id, settings)
    if url is not None:
        entry["web_url"] = url


def attach_web_urls(
    entries: list[dict[str, Any]],
    *,
    settings: Settings,
    id_key: str = "id",
) -> None:
    """Add a ``web_url`` key to each dict in ``entries`` in place, where applicable."""
    for entry in entries:
        attach_web_url(entry, settings=settings, id_key=id_key)
