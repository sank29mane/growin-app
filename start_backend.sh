#!/bin/bash
cd "$(dirname "$0")/backend"

# === Configuration ===
PID_FILE=".server.pid"
PORT=8002
HOST="127.0.0.1"

# === Colors ===
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${YELLOW}🚀 Initializing Growin Backend (UV Powered)...${NC}"

# 1. Host Check (Mac Optimization)
if [[ "$OSTYPE" == "darwin"* ]]; then
    if ! brew list libomp > /dev/null 2>&1; then
        echo -e "${YELLOW}Installing libomp (XGBoost req)...${NC}"
        brew install libomp
    fi
fi

# 2. Port Cleanup
if lsof -i:$PORT >/dev/null; then
    echo -e "${YELLOW}Freeing port $PORT...${NC}"
    lsof -ti:$PORT | xargs kill -9 2>/dev/null
    sleep 1
fi

# 3. Start Server
echo -e "${GREEN}Starting Uvicorn Server via UV...${NC}"

# Use uv run to handle venv and deps
uv run python -m uvicorn server:app --reload --host $HOST --port $PORT --loop uvloop > server.log 2>&1 &
NEW_PID=$!
echo $NEW_PID > "$PID_FILE"

# 4. Health Check Loop
echo -n "Waiting for health check"
MAX_RETRIES=30
COUNT=0
URL="http://$HOST:$PORT/health"

while [ $COUNT -lt $MAX_RETRIES ]; do
    if curl -s $URL > /dev/null; then
        echo ""
        echo -e "${GREEN}✅ SYSTEM ONLINE${NC} - PID: $NEW_PID"
        echo -e "   Dashboard: http://$HOST:$PORT"
        echo -e "   Logs:      backend/server.log"
        exit 0
    fi
    echo -n "."
    sleep 1
    let COUNT=COUNT+1
done

echo ""
echo -e "${RED}❌ Server failed to start. Check backend/server.log${NC}"
exit 1

