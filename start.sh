#!/bin/bash
set -e
cd "$(dirname "$0")"

# Load .env only when running locally (Coolify injects env vars directly)
if [ -f ".env" ]; then
    set -a
    . ./.env
    set +a
fi

echo "[outboundai] Starting..."
echo "[outboundai] LiveKit: ${LIVEKIT_URL}"
echo "[outboundai] Model:   ${GEMINI_MODEL:-gemini-3.8-live}"
echo "[outboundai] SIP:     ${SIP_PROVIDER:-twilio}"

echo "[outboundai] Starting FastAPI server on port 8000..."
uvicorn server:app --host 0.0.0.0 --port 8000 &
SERVER_PID=$!
MCP_PID=""
if [ -n "${AGENT_API_KEY}" ]; then
    echo "[outboundai] Starting MCP on 127.0.0.1:${MCP_PORT:-8766} (API key required)..."
    python mcp_server.py &
    MCP_PID=$!
else
    echo "[outboundai] MCP not started. Set AGENT_API_KEY in .env to open it."
fi
cleanup() {
    echo "[outboundai] Shutting down..."
    kill $SERVER_PID 2>/dev/null || true
    if [ -n "$MCP_PID" ]; then
        kill $MCP_PID 2>/dev/null || true
    fi
    exit 0
}
trap cleanup SIGTERM SIGINT

sleep 2

echo "[outboundai] Starting LiveKit agent worker..."
python agent.py start

cleanup
