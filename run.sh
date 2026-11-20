#!/usr/bin/env bash
# Human Emulator Mission Control launcher: local model + server + dashboard, no API keys.
set -euo pipefail
cd "$(dirname "$0")"

MODEL="${CASE_MODEL:-qwen3.5:2b-q4_K_M}"
PORT="${CASE_PORT:-8765}"
PYTHON="${PYTHON:-python3}"
OLLAMA_URL="http://127.0.0.1:11434"
STARTED_OLLAMA=""

cleanup() {
  if [ -n "$STARTED_OLLAMA" ]; then kill "$STARTED_OLLAMA" 2>/dev/null || true; fi
}
trap cleanup EXIT

if ! command -v ollama >/dev/null 2>&1; then
  echo "Ollama is required: brew install ollama" >&2
  exit 1
fi

if curl -sf "http://127.0.0.1:$PORT/api/status" >/dev/null 2>&1; then
  echo "• Human Emulator is already running → http://127.0.0.1:$PORT"
  [ -z "${CASE_NO_BROWSER:-}" ] && open "http://127.0.0.1:$PORT/"
  exit 0
fi

if ! curl -sf "$OLLAMA_URL/api/version" >/dev/null; then
  echo "• Starting the on-device model server (Ollama)…"
  OLLAMA_FLASH_ATTENTION=1 OLLAMA_KV_CACHE_TYPE=q8_0 OLLAMA_KEEP_ALIVE=30m ollama serve >"${TMPDIR:-/tmp}/case-ollama.log" 2>&1 &
  STARTED_OLLAMA=$!
  for _ in $(seq 1 40); do
    curl -sf "$OLLAMA_URL/api/version" >/dev/null && break
    sleep 0.5
  done
fi

if ! ollama list | awk 'NR>1 {print $1}' | grep -qx "$MODEL"; then
  echo "• Downloading $MODEL once (needs internet this one time)…"
  ollama pull "$MODEL"
fi

if [ ! -x .venv/bin/python ]; then
  echo "• Creating Python environment…"
  "$PYTHON" -m venv .venv
  .venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt
fi

echo "• Checking macOS permissions…"
.venv/bin/python preflight.py || true

if [ -z "${CASE_NO_BROWSER:-}" ]; then
  ( sleep 2 && open "http://127.0.0.1:$PORT/" ) &
fi

echo "• Human Emulator Mission Control → http://127.0.0.1:$PORT  (Ctrl+C to stop)"
CASE_MODEL="$MODEL" CASE_PORT="$PORT" .venv/bin/python -m uvicorn server:app --host 127.0.0.1 --port "$PORT" --no-access-log
