"""Central registry of every MCP tool in the package.

The ``@register`` decorator records a ``ToolSpec`` for each tool function
*without* calling ``mcp.tool()`` — that happens later in ``server.py``,
which iterates the registry and applies registration gates
(``LF_READ_ONLY``, the ``LF_WRITE_TOOLS_ALLOWED`` allowlist).

This indirection lets a single declarative annotation per tool replace
three separate places the old code had to be kept in sync:

  * the ``_WRITE_TOOLS`` tuple in ``server.py``
  * the ``_V2_RENAME_MAP`` dict mapping legacy → v2 names
  * the ``__all__`` re-export list in ``server.py``

Adding a new tool now means: write the function in a ``tools/*.py``
module, decorate it with ``@register(v2_name=..., is_write=...)``,
and import the module from ``server.py``. Nothing else changes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

# A tool is an async function returning a JSON-serializable dict.
ToolFn = Callable[..., Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ToolSpec:
    """Everything ``server.py`` needs to register one tool with FastMCP."""

    fn: ToolFn
    legacy_name: str
    """v1.x name preserved as a deprecation shim through v2.x."""
    v2_name: str
    """``laserfiche_{resource}_{verb}`` name introduced in v2.0."""
    is_write: bool
    """If true, registration is gated behind ``LF_READ_ONLY=false`` and the
    ``LF_WRITE_TOOLS_ALLOWED`` allowlist."""


_REGISTRY: list[ToolSpec] = []


def register(
    *,
    v2_name: str,
    is_write: bool = False,
) -> Callable[[ToolFn], ToolFn]:
    """Record this tool's metadata in the package-level registry.

    The decorator returns the function unmodified — it does NOT call
    ``mcp.tool()``. That happens later in ``server.py`` after settings
    are loaded so write tools can be conditionally registered.

    Usage::

        @register(v2_name="laserfiche_entry_search")
        async def search_entries(query: str, ...) -> dict[str, Any]:
            ...

        @register(v2_name="laserfiche_entry_delete", is_write=True)
        async def delete_entry(entry_id: int, ...) -> dict[str, Any]:
            ...
    """

    def wrap(fn: ToolFn) -> ToolFn:
        _REGISTRY.append(
            ToolSpec(
                fn=fn,
                legacy_name=fn.__name__,
                v2_name=v2_name,
                is_write=is_write,
            )
        )
        return fn

    return wrap


def all_tools() -> list[ToolSpec]:
    """Return every registered tool spec, in registration order.

    Returns a fresh list so callers can't mutate the registry by
    accident. Order is insertion order — currently determined by the
    order tool modules are imported from ``server.py``.
    """
    return list(_REGISTRY)


def reads() -> list[ToolSpec]:
    """Convenience: the subset of tools that aren't gated by ``LF_READ_ONLY``."""
    return [s for s in _REGISTRY if not s.is_write]


def writes() -> list[ToolSpec]:
    """Convenience: the subset of tools that ARE gated by ``LF_READ_ONLY``."""
    return [s for s in _REGISTRY if s.is_write]


def v2_rename_map() -> dict[str, str]:
    """Generate the legacy → v2 name map from the registry.

    Provided for back-compat with tests/external code that introspected
    the old ``server._V2_RENAME_MAP`` dict directly.
    """
    return {s.legacy_name: s.v2_name for s in _REGISTRY}
