#!/usr/bin/env bash
# Build the Claude Desktop Extension (.mcpb) bundle.
#
# Produces dist/laserfiche-mcp.mcpb — a single file an administrator can
# double-click to install into Claude Desktop (macOS/Windows). No signing
# required. See docs/desktop-extension.md for the install walkthrough.
#
# Requires Node (for `npx @anthropic-ai/mcpb`). The .mcpb itself runs on the
# `uv` runtime Claude Desktop ships, so end users need neither Node nor a
# preinstalled Python.
set -euo pipefail

cd "$(dirname "$0")/.."

MCPB=(npx --yes @anthropic-ai/mcpb)

# `validate` in the currently-published CLI (2.1.2) predates the `uv` server
# type used by Anthropic's own file-manager-python example, so it may reject a
# spec-current manifest. Treat it as advisory — `pack` is the authoritative
# build step.
echo "==> Validating manifest.json (advisory)"
"${MCPB[@]}" validate manifest.json || \
  echo "    note: validator is behind the spec; continuing (see docs/desktop-extension.md)"

echo "==> Packing bundle"
mkdir -p dist
"${MCPB[@]}" pack . dist/laserfiche-mcp.mcpb

echo
echo "Built dist/laserfiche-mcp.mcpb"
echo "Install: open Claude Desktop → Settings → Extensions → Install extension,"
echo "or just double-click the .mcpb file."
