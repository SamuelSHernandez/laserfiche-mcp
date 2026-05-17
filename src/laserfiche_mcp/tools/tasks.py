"""Tools for tracking long-running server-side operations (delete folder, copy, import)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .. import _app
from ..errors import LaserficheError, classify_lf_error
from ._registry import register


@register(v2_name="laserfiche_task_get_status")
async def get_task_status(operation_token: str) -> dict[str, Any]:
    """Look up the status of an async operation by its token.

    The async tools (``delete_entry``, ``copy_entry``, sometimes
    ``import_document``) return an ``operation_token`` instead of the
    final result — call this to check whether the operation finished.
    For "wait until done" semantics, use ``wait_for_task`` instead so
    you don't have to write a polling loop.

    Args:
        operation_token: The string token returned by the originating
            async tool.

    Returns: Server's task payload — ``operationToken``,
    ``operationType``, ``percentComplete``, ``status`` (one of
    ``NotStarted``, ``InProgress``, ``Completed``, ``Failed``,
    ``Canceled``), ``redirectUri`` (set when the op produced a new
    entry, e.g. after a copy), ``entryId`` (the resulting entry's ID
    when applicable), ``errors`` (list — empty on success), and
    timestamps.

    On failure: returns ``{"mode": "error", "error": <slug>,
    "operation_token": <str>, ...}``. Common slugs: ``not_found``
    (token unknown — usually expired or from a different server
    instance), ``auth_failed``.
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
    operation_token: str,
    timeout_seconds: int = 60,
    poll_interval_seconds: float = 1.0,
) -> dict[str, Any]:
    """Block until an async operation reaches a terminal state.

    Preferred over manual polling with ``get_task_status``. Returns
    quickly when the op is fast; otherwise polls at ``poll_interval_seconds``
    until ``Completed``, ``Failed``, or ``Canceled`` — or until
    ``timeout_seconds`` is reached, in which case the last observed
    status is returned with ``timed_out=true`` so the caller can decide
    whether to keep waiting.

    Args:
        operation_token: Token from the originating async tool.
        timeout_seconds: Maximum time to wait (default 60). Set higher
            for large folder deletes or large copies.
        poll_interval_seconds: Delay between status checks (default 1.0).
            Bounded below at 0.1s.

    Returns: Same payload as ``get_task_status``, with an added
    ``timed_out`` boolean indicating whether the wait ended on timeout.

    On failure: if a poll call fails mid-wait, returns
    ``{"mode": "error", "error": <slug>, "operation_token": <str>, ...}``.
    Common slugs: ``not_found`` (token invalidated by server restart),
    ``auth_failed``.
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
