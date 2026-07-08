"""Entry point for the Claude Desktop extension (``.mcpb``) bundle.

The console script declared in ``pyproject.toml`` (``laserfiche-mcp``) is the
normal way to launch the server, but the ``uv`` server type in a Desktop
Extension runs a *file*, not a console script. ``server.py`` itself can't be
run as a loose script because it uses package-relative imports, so this thin
wrapper puts ``src/`` on the path and calls the same ``main()``.

``uv run --directory <bundle> mcp_entry.py`` (see ``manifest.json``) syncs the
project's dependencies from ``pyproject.toml`` on first launch, then invokes
this.
"""

from __future__ import annotations

import os
import sys

# Make ``import laserfiche_mcp`` resolve whether or not uv has installed the
# project itself — deps are always synced, but the src-layout package may be
# import-only. Harmless if the package is already importable.
_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from laserfiche_mcp.server import main  # noqa: E402

if __name__ == "__main__":
    main()
