"""Effectiveness evals for laserfiche-mcp.

L2 — deterministic replay: drive the live MCP tool surface from a JSON
corpus and assert postconditions. No LLM in the loop, runs in CI.

L3 (future) — Claude-driven task eval. Same corpus, model picks the path.

Read ``README.md`` in this directory for the strategy doc.
"""
