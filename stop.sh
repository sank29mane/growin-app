#!/bin/bash
# 🛑 Growin - Fast Purge Script

PORT=8002
PID_FILE=".server.pid"
YELLOW='\033[1;33m'
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${YELLOW}Stopping Growin...${NC}"

# 1. Kill everything on the port (Most reliable)
if lsof -i:$PORT >/dev/null 2>&1; then
    echo -e "Purging port $PORT..."
    lsof -ti:$PORT | xargs kill -9 2>/dev/null
    echo -e "${GREEN}✅ Port cleared.${NC}"
fi

# 2. Cleanup PID file
if [ -f "$PID_FILE" ]; then
    rm "$PID_FILE"
fi

# 3. Final verification
if ! lsof -i:$PORT >/dev/null 2>&1; then
    echo -e "${GREEN}✅ All backend processes terminated.${NC}"
else
    echo -e "${RED}❌ Warning: Port $PORT still active. Manual check required.${NC}"
fi
