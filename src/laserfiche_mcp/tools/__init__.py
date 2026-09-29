"""MCP tool implementations grouped by resource.

Every submodule (read and write alike) is imported unconditionally by
``server.py`` at module load — that's what populates the ``tools/_registry``
metadata (``@register(v2_name=..., is_write=...)``) each tool function
carries, decoupled from whether it actually registers with FastMCP.
Registration itself is a separate, later step: ``server._register_read_tools()``
runs at import time for every non-write tool; ``server._register_write_tools()``
runs from ``main()``, after settings are loaded, and only calls
``mcp.tool(...)`` for write tools when ``LF_READ_ONLY=false`` (honoring
``LF_WRITE_TOOLS_ALLOWED`` too). So importing this package always defines
every tool function; whether ``LF_READ_ONLY=true`` actually reaches the
model is decided afterward, in ``server.py``, not by which modules get
imported.
"""
