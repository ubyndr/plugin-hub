#!/usr/bin/env bash
# Set up the package for local development and run every offline check CI runs.
#
#   ./dev.sh              tests, lint, release tooling, catalogue validation
#   ./dev.sh -k sync      any pytest arguments pass straight through
set -euo pipefail
cd "$(dirname "$0")"

uv venv -q --allow-existing
uv pip install -q -e "packages/plugin-hub[test]"
uv run --no-sync pytest packages/plugin-hub "$@"
# The release tooling, against throwaway git repos.
uv run --no-sync pytest tests -q
uvx ruff check packages/ scripts/ tests/
if command -v claude >/dev/null; then
  claude plugin validate .
fi
