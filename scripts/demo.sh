#!/usr/bin/env bash
# One command for the Loom demo:
#   reset the DB (keeps pre-warmed LLM caches for the simulated org), start the demo MCP
#   server, the backend (serves the UI), and the sensor on THIS machine, then replay the
#   simulated colleagues so the UI fills up live.
#
#   scripts/demo.sh            # replay colleagues over ~30s
#   scripts/demo.sh --fast     # load colleagues instantly
#   scripts/demo.sh --no-seed  # only your own machine
#
# Needs: OPENROUTER_API_KEY (else rule-based cards), optional DEMO_OWNER_NAME / LANGDOCK_API_KEY.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT=$(pwd)
PORT=${PORT:-8787}
MODE=${1:-replay}

if [ ! -x .venv/bin/python ]; then
  echo "Setting up Python venv…"; python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt
fi
PY=$ROOT/.venv/bin/python
if [ ! -f ui/dist/index.html ]; then
  echo "Building UI…"; (cd ui && npm install --no-audit --no-fund && npx vite build)
fi
[ -z "${OPENROUTER_API_KEY:-}" ] && echo "⚠️  OPENROUTER_API_KEY not set: cards stay rule-based (no LLM)."
export PYTHONPATH=$ROOT BACKEND_URL=http://127.0.0.1:$PORT DEMO_OWNER_NAME=${DEMO_OWNER_NAME:-$(id -un)}

mkdir -p data/logs
pids=()
cleanup() { echo; echo "Stopping…"; kill "${pids[@]}" 2>/dev/null || true; wait 2>/dev/null || true; }
trap cleanup EXIT INT TERM

$PY scripts/demo_mcp_server.py > data/logs/demo_mcp.log 2>&1 & pids+=($!)
$PY -m uvicorn backend.app:app --host 127.0.0.1 --port "$PORT" --log-level warning --timeout-graceful-shutdown 1 \
  > data/logs/backend.log 2>&1 & pids+=($!)
for i in $(seq 1 60); do curl -sf "http://127.0.0.1:$PORT/api/health" >/dev/null && break; sleep 0.5; done
curl -sf -X POST "http://127.0.0.1:$PORT/api/admin/reset" >/dev/null
echo "Backend + UI: http://127.0.0.1:$PORT (loading the embedding model…)"
for i in $(seq 1 120); do  # replaying while torch loads can stall requests
  curl -sf "http://127.0.0.1:$PORT/api/stats" | grep -q '"embedder_ready": *true' && break; sleep 1
done

$PY -m sensor.main > data/logs/sensor.log 2>&1 & pids+=($!)
echo "Sensor running on this machine (log: data/logs/sensor.log)"

case "$MODE" in
  --fast) $PY scripts/seed.py --backend "http://127.0.0.1:$PORT" ;;
  --no-seed) ;;
  *) (sleep 2; $PY scripts/seed.py --backend "http://127.0.0.1:$PORT" --replay > data/logs/seed.log 2>&1) & pids+=($!)
     echo "Replaying 6 colleagues + identity logs (~30s)…" ;;
esac

cat <<MSG

──────────────────────────────────────────────────────────────────────────
 On camera: add this to Cursor → Settings → MCP (or ~/.cursor/mcp.json):

   "inbox-digest": { "url": "http://127.0.0.1:8931/mcp",
                     "headers": { "Authorization": "Bearer demo" } }

 Watch it appear in the inventory, join "Customer email summarizer", then
 Duplicates → Preview one Langdock agent → Approve → Create in Langdock.
 Ctrl-C stops everything.
──────────────────────────────────────────────────────────────────────────
MSG
(command -v open >/dev/null && open "http://127.0.0.1:$PORT") || true
wait
