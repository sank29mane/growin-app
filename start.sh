#!/bin/bash
# 🚀 canonical growin startup script (uv powered)

PROJECT_ROOT="$(dirname "$0")"
cd "$PROJECT_ROOT"

# === 1. configuration ===
PORT=8002
HOST="127.0.0.1"
PID_FILE=".server.pid"
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${YELLOW}--- Growin System Startup (UV) ---${NC}"

# === 2. system checks (macOS) ===
if [[ "$OSTYPE" == "darwin"* ]]; then
    if ! brew list libomp > /dev/null 2>&1; then
        echo -e "${YELLOW}Installing libomp (XGBoost req)...${NC}"
        brew install libomp
    fi
fi

# === 3. cleanup ===
if lsof -i:$PORT >/dev/null; then
    echo -e "${YELLOW}Cleaning up port $PORT...${NC}"
    lsof -ti:$PORT | xargs kill -9 2>/dev/null
    sleep 1
fi

# === 4. telemetry opt-out ===
export POSTHOG_DISABLED=1
export ANALYTICS_OPT_OUT=true
export DO_NOT_TRACK=1
export PH_OPT_OUT=1
export POSTHOG_OFFLINE=1
export POSTHOG_BATCH_MAX_RETRIES=0

# === 5. start server via uv ===
echo -e "${GREEN}Starting Uvicorn Server via UV...${NC}"
cd backend

# Run in background, streaming output to console AND file
uv run python -m uvicorn server:app --reload --host $HOST --port $PORT --loop uvloop 2>&1 | tee ../startup.log &
NEW_PID=$!
echo $NEW_PID > "../$PID_FILE"

# === 6. health check ===
echo -n "Waiting for health check"
MAX_RETRIES=30
COUNT=0
URL="http://$HOST:$PORT/health"

while [ $COUNT -lt $MAX_RETRIES ]; do
    if curl -s $URL > /dev/null; then
        echo ""
        echo -e "${GREEN}✅ SYSTEM ONLINE${NC} - PID: $NEW_PID"
        echo -e "   Dashboard: http://$HOST:$PORT"
        echo -e "   Logs:      startup.log"
        exit 0
    fi
    echo -n "."
    sleep 1
    let COUNT=COUNT+1
done

echo ""
echo -e "${RED}❌ Server failed to start. Check startup.log${NC}"
# Kill the failed process to avoid zombies
kill $NEW_PID 2>/dev/null
exit 1
