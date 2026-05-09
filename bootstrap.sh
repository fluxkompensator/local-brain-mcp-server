#!/usr/bin/env bash
##############################################################
# Created: 2026-05-09
# Author:  void
# Purpose: One-shot bootstrap from a fresh clone — creates a
#          .venv with the right Python, installs the runtime
#          deps, and pulls Chromium for Playwright (needed only
#          if you ever pass --browser).
##############################################################
set -euo pipefail

if ! command -v uv >/dev/null 2>&1; then
  echo "uv not found. Install it first: https://docs.astral.sh/uv/getting-started/installation/" >&2
  exit 1
fi

uv venv --python 3.12
# shellcheck disable=SC1091
source .venv/bin/activate
uv pip install "mcp[cli]" crawl4ai chromadb \
    langchain-text-splitters sentence-transformers
playwright install chromium

cat <<'EOF'

✓ bootstrap done.

To activate this venv later:
  source .venv/bin/activate          # bash / zsh
  source .venv/bin/activate.fish     # fish

First crawl (replace URL + brain name):
  python ingest.py https://docs.example.com/ --depth 6 --max-pages 800 --brain example-docs

Register the MCP server with Claude Code (one-time, user scope):
  claude mcp add localbrain --scope user -- "$PWD/.venv/bin/python" "$PWD/server.py"
EOF
