"""FastMCP server exposing Laserfiche Repository operations as tools.

This module is intentionally thin: the FastMCP instance, the per-request
lifespan, and the shared accessors all live in ``_app.py``; the tools
themselves live in the ``tools/`` subpackage; each tool is annotated with
``@register(v2_name=..., is_write=...)`` so the metadata is on the
function definition rather than spread across maps and tuples here.

The job of *this* file is:

1. Import every ``tools/*`` module so each ``@register`` runs and the
   tool registry is populated.
2. ``_register_read_tools()`` (called at module load) and
   ``_register_write_tools()`` (called from ``main()`` after settings
   are loaded) apply ``mcp.tool(name=...)`` for both the legacy name
   and the v2 ``laserfiche_{resource}_{verb}`` alias, with
   ``LF_READ_ONLY`` and ``LF_WRITE_TOOLS_ALLOWED`` gating on writes.
3. Re-export each tool function and a few legacy helpers under the
   ``server.*`` namespace so callers (and the test suite) keep working.
4. Provide the ``main()`` entrypoint declared in pyproject.toml; the
   body lives in ``cli.py``.
"""

from __future__ import annotations

from . import permissions
from ._app import (
    clamp_max_results,
    clamp_search_page_size,
    get_client,
    get_settings,
    mcp,
    reset_settings_for_tests,
)
from .cli import (
    _format_config_error,
    _parse_args,
    _resolve_log_level,
)
from .cli import (
    main as _cli_main,
)
from .observability import tool_logger

# Import every tool module so each ``@register`` fires and the registry
# is populated before _register_read_tools() runs below.
from .tools import (  # noqa: F401
    definitions,
    documents,
    natural_search,
    preview_execute_splits,
    reads,
    tasks,
    write_collapses,
    writes_create_copy_import,
    writes_delete_edoc_pages,
    writes_delete_entry,
    writes_fields_tags_links,
    writes_move_rename,
    writes_templates,
)
from .tools._registry import ToolSpec, all_tools, v2_rename_map, writes
from .tools._registry import reads as _read_specs

# Back-compat re-exports for tests and external callers that import these
# helpers directly. New code should import from ``_app`` / ``tools._support``.
_get_settings = get_settings
_reset_settings_for_tests = reset_settings_for_tests
_client = get_client
_clamp_max_results = clamp_max_results
_clamp_search_page_size = clamp_search_page_size


def _register_one(spec: ToolSpec) -> None:
    """Register a single tool under both its legacy and v2 names.

    Both registrations point at the same function — the v2 name is the
    recommended path, the legacy name is a deprecation shim through v2.x.

    The function is wrapped with ``tool_logger`` so every call (regardless
    of which name the agent used) emits one structured log event with a
    UUID4 ``request_id`` propagated via ContextVar to ``classify_lf_error``.
    The decorator is idempotent, so applying it once and registering the
    same wrapped function under both names gives one log line per call.
    """
    wrapped = tool_logger(spec.fn)
    mcp.tool(name=spec.legacy_name)(wrapped)
    mcp.tool(name=spec.v2_name)(wrapped)


def _register_read_tools() -> None:
    """Register every non-write tool with FastMCP.

    Called at module load so reads are visible in the MCP catalog as
    soon as anything imports ``laserfiche_mcp.server``.
    """
    for spec in _read_specs():
        _register_one(spec)


def _register_write_tools() -> None:
    """Register write tools if and only if ``LF_READ_ONLY=false``.

    Called from ``main()`` after settings have been validated, so the
    tool catalog the LLM sees reflects the configured permission level.
    Also honors ``LF_WRITE_TOOLS_ALLOWED`` — if set, only tools named
    in that comma-separated env var are registered. Lets operators
    ship a metadata-only or create-only deployment.
    """
    settings = get_settings()
    if settings.read_only:
        return
    allowed = permissions.parse_tool_allowlist(settings.write_tools_allowed)
    for spec in writes():
        if allowed is not None and spec.legacy_name not in allowed:
            continue
        _register_one(spec)


# Register reads now so anything importing this module sees them.
_register_read_tools()


# --- Back-compat shims for tests / external callers --------------------------
# Older code (and tests) reference ``server._V2_RENAME_MAP`` and
# ``server._WRITE_TOOLS`` directly. Both are derivable from the registry now.

_V2_RENAME_MAP: dict[str, str] = v2_rename_map()
_WRITE_TOOLS: tuple = tuple(s.fn for s in writes())


# Bind every registered tool function as a module-level attribute so
# ``server.search_entries(...)``, ``server.delete_entry(...)``, etc. keep
# working. The alternative — typing out 33 explicit imports + an
# ``__all__`` listing — drifts the moment a new tool is added.
_module_globals = globals()
for _spec in all_tools():
    _module_globals[_spec.legacy_name] = _spec.fn
del _spec, _module_globals

__all__ = [
    "mcp",
    "main",
    # Helpers re-exported for tests / external callers
    "_get_settings",
    "_reset_settings_for_tests",
    "_client",
    "_clamp_max_results",
    "_clamp_search_page_size",
    "_format_config_error",
    "_parse_args",
    "_resolve_log_level",
    "_register_write_tools",
    "_register_read_tools",
    "_WRITE_TOOLS",
    "_V2_RENAME_MAP",
    # Tool functions (registered above in the for-loop)
    *[s.legacy_name for s in all_tools()],
]


def main() -> None:
    """Console-script entrypoint registered in pyproject.toml."""
    _cli_main(_register_write_tools)


if __name__ == "__main__":
    main()
