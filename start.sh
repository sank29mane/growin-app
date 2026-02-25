#!/bin/bash
# 🚀 Growin - AI Trading Assistant Startup Script (Consolidated)
# This script starts the backend server and opens the Xcode project.

PROJECT_ROOT="$(dirname "$0")"
cd "$PROJECT_ROOT"

# === 1. Configuration ===
PORT=8002
HOST="127.0.0.1"
PID_FILE=".server.pid"
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

# Parse flags
OPEN_XCODE=true
while [[ "$#" -gt 0 ]]; do
    case $1 in
        --headless|-h) OPEN_XCODE=false ;;
        --help) echo "Usage: ./start.sh [--headless|-h]"; exit 0 ;;
        *) echo "Unknown parameter passed: $1"; exit 1 ;;
    esac
    shift
done

echo -e "${YELLOW}🚀 Initializing Growin System...${NC}"
echo "==========================================="

# === 2. System Checks (macOS Optimization) ===
if [[ "$OSTYPE" == "darwin"* ]]; then
    if ! brew list libomp > /dev/null 2>&1; then
        echo -e "${YELLOW}Installing libomp (XGBoost requirement)...${NC}"
        brew install libomp
    fi
fi

# === 3. Port Cleanup ===
if lsof -i:$PORT >/dev/null; then
    echo -e "${YELLOW}Cleaning up port $PORT...${NC}"
    lsof -ti:$PORT | xargs kill -9 2>/dev/null
    sleep 1
fi

# === 4. Telemetry Opt-out (Privacy First) ===
export POSTHOG_DISABLED=1
export ANALYTICS_OPT_OUT=true
export DO_NOT_TRACK=1
export PH_OPT_OUT=1
export POSTHOG_OFFLINE=1
export POSTHOG_BATCH_MAX_RETRIES=0

# === 5. Start Backend Server via UV ===
echo -e "${GREEN}Starting Backend Server via UV...${NC}"

# Ensure we're in the right directory for the backend
if [ -d "backend" ]; then
    cd backend
else
    echo -e "${RED}❌ Error: 'backend' directory not found.${NC}"
    exit 1
fi

# Run uvicorn in background, streaming output to startup.log
uv run python -m uvicorn server:app --reload --host $HOST --port $PORT --loop uvloop 2>&1 | tee ../startup.log &
NEW_PID=$!
echo $NEW_PID > "../$PID_FILE"

# === 6. Health Check Loop ===
echo -n "Waiting for health check"
MAX_RETRIES=30
COUNT=0
URL="http://$HOST:$PORT/health"

while [ $COUNT -lt $MAX_RETRIES ]; do
    if curl -s $URL > /dev/null; then
        echo ""
        echo -e "${GREEN}✅ BACKEND ONLINE${NC} - PID: $NEW_PID"
        echo -e "   Dashboard: http://$HOST:$PORT"
        echo -e "   Logs:      startup.log"
        break
    fi
    echo -n "."
    sleep 1
    let COUNT=COUNT+1
    
    # If we hit max retries, fail
    if [ $COUNT -eq $MAX_RETRIES ]; then
        echo ""
        echo -e "${RED}❌ Server failed to start. Check startup.log${NC}"
        kill $NEW_PID 2>/dev/null
        exit 1
    fi
done

# === 7. Open Xcode Project ===
if [ "$OPEN_XCODE" = true ]; then
    echo ""
    echo -e "${GREEN}📱 Opening Xcode project...${NC}"
    cd "$PROJECT_ROOT"
    if [ -d "Growin/Growin.xcodeproj" ]; then
        open "Growin/Growin.xcodeproj"
    else
        echo -e "${YELLOW}⚠️  Warning: Xcode project not found at Growin/Growin.xcodeproj${NC}"
    fi
fi

echo ""
echo "=========================================="
echo -e "${GREEN}🎉 Setup Complete!${NC}"
echo ""
if [ "$OPEN_XCODE" = true ]; then
    echo "Next steps:"
    echo "  1. In Xcode, select 'Growin' scheme"
    echo "  2. Press ⌘R to run the app"
    echo "  3. Select your preferred LLM in Settings"
fi
echo ""
echo "Tips:"
echo "  • Logs are being written to startup.log"
echo "  • Use ./start.sh --headless to start only the backend"
echo "=========================================="
