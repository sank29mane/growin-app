#!/bin/bash
# Growin macOS App Runner
# Starts both backend and opens Xcode

echo "🚀 Growin - AI Trading Assistant for macOS"
echo "==========================================="
echo ""

# Check if backend is already running
if lsof -ti:8002 > /dev/null 2>&1; then
    echo "✅ Backend already running on port 8002"
else
    echo "📦 Starting backend server..."
    osascript -e 'tell application "Terminal" to do script "cd \"'"$(dirname "$0")"'\" && ./start_backend.sh"'
    echo "⏳ Waiting for backend to start..."
    sleep 3
fi

# Check backend health
if curl -s http://127.0.0.1:8002/health > /dev/null 2>&1; then
    echo "✅ Backend is healthy"
else
    echo "⚠️  Backend may still be starting..."
fi

echo ""
echo "📱 Opening Xcode project..."
open "$(dirname "$0")/Growin/Growin.xcodeproj"

echo ""
echo "=========================================="
echo "🎉 Setup Complete!"
echo ""
echo "Next steps:"
echo "  1. In Xcode, select 'Growin' scheme"
echo "  2. Press ⌘R to run the app"
echo "  3. Select your preferred LLM in Settings"
echo ""
echo "Tips:"
echo "  • Use Ollama or MLX for local AI (no API key needed)"
echo "  • For portfolio data, add Trading 212 API key in Settings"
echo "=========================================="
