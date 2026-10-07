#!/usr/bin/env bash
set -euo pipefail

export PATH="/usr/local/bin:/usr/local/cargo/bin:${HOME}/.local/bin:${PATH}"
export CI=true
export GROWIN_ANALYTICS_ENABLED=false
export POSTHOG_DISABLED=1
export ANALYTICS_OPT_OUT=true
export DO_NOT_TRACK=1
export PH_OPT_OUT=1
export PYTHONPATH=/workspace/backend

PORT=8002
HOST=0.0.0.0

if command -v lsof >/dev/null 2>&1 && lsof -i:"${PORT}" >/dev/null 2>&1; then
  lsof -ti:"${PORT}" | xargs -r kill -9 2>/dev/null || true
  sleep 1
fi

cd /workspace/backend
exec uv run python -m uvicorn server:app --host "${HOST}" --port "${PORT}" --loop uvloop
