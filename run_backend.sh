#!/bin/bash
# Growin App backend runner.
# Runs backend/server.py inside the backend uv project (backend/pyproject.toml,
# backend/.venv), from any working directory.
set -euo pipefail

# Work from the repo root so relative paths below resolve, whatever the cwd was.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# Load .env if present. This sources the file as shell, so it must be a trusted,
# operator-owned file. Values are exported but never printed.
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

# Append backend to PYTHONPATH without leaving an empty entry (empty means cwd).
export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}$ROOT/backend"
echo "Starting Growin Backend with PYTHONPATH=$PYTHONPATH"

exec uv run --project backend python3 backend/server.py
