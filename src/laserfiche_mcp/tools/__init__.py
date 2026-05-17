"""@mcp.tool() registrations grouped by resource.

Importing a submodule registers its tools as a side effect of the
``@mcp.tool()`` decorator firing at import time. ``server.py`` imports
the read-side submodules unconditionally; the write-side submodules
are imported by ``_register_write_tools()`` in ``server.py`` only when
``LF_READ_ONLY=false`` and after the write tool-allowlist has been
consulted.
"""
