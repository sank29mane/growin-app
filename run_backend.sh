#!/bin/bash
# SOTA 2026: Growin App Backend Runner
# Ensures correct environment and import paths

export PYTHONPATH=$PYTHONPATH:$(pwd)/backend
echo "🚀 Starting Growin Backend with PYTHONPATH=$PYTHONPATH"

# Check if .env exists
if [ -f .env ]; then
    export $(grep -v '^#' .env | xargs)
fi

# Run the server using uv
uv run python3 backend/server.py
