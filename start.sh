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
echo "[outboundai] Model:   ${GEMINI_MODEL:-gemini-3.1-flash-live-preview}"
echo "[outboundai] SIP:     ${SIP_PROVIDER:-twilio}"

echo "[outboundai] Starting FastAPI server on port 8000..."
uvicorn server:app --host 0.0.0.0 --port 8000 &
SERVER_PID=$!

sleep 2

echo "[outboundai] Starting LiveKit agent worker..."
python agent.py start

kill $SERVER_PID 2>/dev/null || true
