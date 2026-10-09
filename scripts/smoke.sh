#!/usr/bin/env bash
# Start the server, generate a report over HTTP, check the result, stop the server.
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${PORT:-8799}"; DATA="$(mktemp -d)"
export PYTHONPATH="${PYTHONPATH:-src}" DIVA_DATA_DIR="$DATA"
python3 -m diva_atelier serve --port "$PORT" --data-dir "$DATA" >/dev/null 2>&1 &
PID=$!; trap 'kill $PID 2>/dev/null || true; rm -rf "$DATA"' EXIT
for _ in $(seq 1 40); do curl -sf "localhost:$PORT/api/health" >/dev/null && break; sleep 0.25; done
curl -sf "localhost:$PORT/api/health" >/dev/null || { echo "server did not start"; exit 1; }
BODY=$(printf '{"variables": %s, "author": "smoke"}' "$(cat examples/bridal_vars.json)")
STATUS=$(curl -sf -X POST "localhost:$PORT/api/configs/bridal/reports" -H 'Content-Type: application/json' -d "$BODY" \
  | python3 -c 'import sys,json; print(json.load(sys.stdin)["status"])')
[ "$STATUS" = "ready_for_review" ] || { echo "unexpected report status: $STATUS"; exit 1; }
echo "smoke ok: report $STATUS"
