"""Tools for tracking long-running server-side operations (delete folder, copy, import)."""

from __future__ import annotations

import asyncio
import time
from typing import Annotated, Any

from pydantic import Field

from .. import _app
from ..errors import LaserficheError, classify_lf_error
from ._registry import register

_OPERATION_TOKEN = Annotated[
    str,
    Field(
        description=(
            "Operation token returned by an async tool (delete_entry, "
            "copy_entry, sometimes import_document)."
        ),
        min_length=1,
    ),
]


@register(v2_name="laserfiche_task_get_status")
async def get_task_status(operation_token: _OPERATION_TOKEN) -> dict[str, Any]:
    """Look up the status of an async operation by its token.

    Async tools (``delete_entry``, ``copy_entry``, sometimes
    ``import_document``) return an ``operation_token``; call this to check
    progress, or ``wait_for_task`` for wait-until-done semantics.

    Returns the server's task payload (``status`` of NotStarted/InProgress/
    Completed/Failed/Canceled, ``percentComplete``, ``entryId`` when a new
    entry resulted, ``errors``). On failure returns ``{"mode": "error",
    "error": <slug>}`` (``not_found`` = token expired or wrong server).
    """
    try:
        raw = await _app.get_client().get_task_status(operation_token)
    except LaserficheError as exc:
        return classify_lf_error(
            "get_task_status",
            exc,
            extra={"operation_token": operation_token},
        )
    return raw


@register(v2_name="laserfiche_task_wait")
async def wait_for_task(
    operation_token: _OPERATION_TOKEN,
    timeout_seconds: Annotated[
        int,
        Field(
            default=60,
            description="Maximum wait; on deadline the last status returns with timed_out=true.",
            ge=1,
            le=3600,
        ),
    ] = 60,
    poll_interval_seconds: Annotated[
        float,
        Field(
            default=1.0,
            description="Delay between status checks. Bounded below at 0.1s.",
            ge=0.1,
            le=60.0,
        ),
    ] = 1.0,
) -> dict[str, Any]:
    """Block until an async operation reaches a terminal state.

    Preferred over manual polling. Returns the same payload as
    ``get_task_status`` plus ``timed_out`` — true when ``timeout_seconds``
    elapsed first, so the caller can decide whether to keep waiting.
    On a failed poll returns ``{"mode": "error", "error": <slug>}``.
    """
    deadline = time.monotonic() + max(1, timeout_seconds)
    last: dict[str, Any] = {}
    while True:
        try:
            last = await _app.get_client().get_task_status(operation_token)
        except LaserficheError as exc:
            return classify_lf_error(
                "wait_for_task",
                exc,
                extra={"operation_token": operation_token},
            )
        status = (last.get("status") or last.get("Status") or "").lower()
        if status in {"completed", "failed", "canceled"}:
            return {**last, "timed_out": False}
        if time.monotonic() >= deadline:
            return {**last, "timed_out": True}
        await asyncio.sleep(max(0.1, poll_interval_seconds))
