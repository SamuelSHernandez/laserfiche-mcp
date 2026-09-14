"""Deterministic operations shared by the CLI and the MCP tools.

Nothing in this package talks to an LLM or shapes a tool response. Each
module is plain compute over bytes already on disk, or a bounded traversal
of the repository — the kind of work that should never cost anyone tokens.

The split matters: ``tools/`` wraps these for the model and pays the context
cost of describing results; ``cli_commands.py`` wraps the same functions for
a human or a shell script and pays nothing. Logic lives here so the two can
never drift.
"""

from __future__ import annotations
