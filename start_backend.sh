#!/bin/bash
cd "$(dirname "$0")/backend"

# PID file to track server
PID_FILE=".server.pid"

# Create venv if not exists
if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

# Activate venv
source .venv/bin/activate

# Install dependencies
echo "Installing dependencies..."
.venv/bin/pip install -r requirements.txt -q
.venv/bin/pip install . 2>/dev/null || echo "Could not install current project as package, relying on manual deps"

# Kill existing server gracefully
if [ -f "$PID_FILE" ]; then
    PID=$(cat "$PID_FILE")
    if ps -p $PID > /dev/null; then
        echo "Stopping existing server (PID: $PID)..."
        kill $PID
        sleep 2
    fi
    rm "$PID_FILE"
fi

# Force kill port 8002 if still occupied
lsof -ti:8002 | xargs kill -9 2>/dev/null && echo "Force cleaned port 8002"

# Wait for port to be free
echo "Waiting for port 8002 to clear..."
while lsof -i:8002 >/dev/null; do
    sleep 1
done

# Run Server
echo ""
echo "🚀 Starting Growin Backend Server (Mac Native)"
echo "📍 Server: http://127.0.0.1:8002"
echo "🧠 Configuration: M4 Pro Optimized (MLX)"
echo ""

# Start in background to save PID
.venv/bin/python -m uvicorn server:app --reload --host 127.0.0.1 --port 8002 &
NEW_PID=$!
echo $NEW_PID > "$PID_FILE"

# Wait for server to come online
echo "Waiting for server health check..."
MAX_RETRIES=30
COUNT=0
URL="http://127.0.0.1:8002/health"

while [ $COUNT -lt $MAX_RETRIES ]; do
    if curl -s $URL > /dev/null; then
        echo "✅ Backend is ONLINE!"
        break
    fi
    sleep 1
    let COUNT=COUNT+1
    echo -n "."
done

if [ $COUNT -eq $MAX_RETRIES ]; then
    echo "❌ Server failed to start within 30 seconds."
    exit 1
fi

wait $NEW_PID
