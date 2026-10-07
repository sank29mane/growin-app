#!/usr/bin/env bash
set -euo pipefail

# uv must be on default PATH (login shells do not load ~/.bashrc).
if ! command -v uv >/dev/null 2>&1; then
  curl -fsSL https://astral.sh/uv/install.sh | sh
fi
if [ -x "${HOME}/.local/bin/uv" ] && [ ! -e /usr/local/bin/uv ]; then
  sudo ln -sf "${HOME}/.local/bin/uv" /usr/local/bin/uv
fi

export PATH="/usr/local/bin:/usr/local/cargo/bin:${PATH}"

cd /workspace
uv python install 3.11
uv sync --project backend --all-groups
