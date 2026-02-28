#!/bin/bash
# 🚀 Growin - High-Performance Startup Script

PORT=8002
HOST="127.0.0.1"
PID_FILE=".server.pid"
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${YELLOW}🚀 Launching Growin Ecosystem...${NC}"

# === 1. Atomic Port Cleanup ===
if lsof -i:$PORT >/dev/null 2>&1; then
    lsof -ti:$PORT | xargs kill -9 2>/dev/null
fi

# === 2. Open Xcode (Parallel) ===
if [ -d "Growin/Growin.xcodeproj" ]; then
    open "Growin/Growin.xcodeproj" &
fi

# === 3. Env Optimization ===
export POSTHOG_DISABLED=1
export ANALYTICS_OPT_OUT=true
export DO_NOT_TRACK=1
export PH_OPT_OUT=1

# === 4. Fast Backend Start ===
cd backend || exit 1
# Using nohup and redirecting to log to ensure it persists correctly
uv run python -m uvicorn server:app --host $HOST --port $PORT --loop uvloop > ../startup.log 2>&1 &
echo $! > "../$PID_FILE"

# === 5. High-Frequency Health Check (0.2s interval) ===
echo -n "Waiting for backend"
URL="http://$HOST:$PORT/health"
for i in {1..50}; do
    if curl -s "$URL" > /dev/null; then
        echo -e "\n${GREEN}✅ BACKEND ONLINE${NC}"
        exit 0
    fi
    echo -n "."
    sleep 0.2
done

echo -e "\n${RED}❌ Timeout: Check startup.log for errors.${NC}"
exit 1
