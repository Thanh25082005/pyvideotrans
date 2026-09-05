#!/usr/bin/env bash
# Khởi động Video Dubbing Studio. Lần đầu chạy sẽ tự tạo venv và cài dependency.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8199}"
HOST="${HOST:-127.0.0.1}"
ALIGNER_PORT="${ALIGNER_PORT:-8200}"
ALIGNER_ENABLED="${ALIGNER_ENABLED:-1}"

if ! command -v ffmpeg >/dev/null; then
  echo "Thiếu ffmpeg. Cài bằng: sudo apt install ffmpeg" >&2
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "Tạo môi trường ảo..."
  if command -v uv >/dev/null; then
    uv venv .venv --python 3.12
    uv pip install --python .venv/bin/python -r requirements.txt
  else
    python3 -m venv .venv
    .venv/bin/pip install -q --upgrade pip
    .venv/bin/pip install -q -r requirements.txt
  fi
fi

aligner_pid=""
if [ "$ALIGNER_ENABLED" = "1" ]; then
  if [ -x .aligner-venv/bin/python ]; then
    .aligner-venv/bin/python aligner_service.py --host 127.0.0.1 --port "$ALIGNER_PORT" &
    aligner_pid=$!
  else
    echo "Qwen aligner chưa được cài. Chạy ./install_aligner.sh; webapp sẽ fallback về VAD." >&2
  fi
fi

cleanup() {
  if [ -n "$aligner_pid" ]; then
    kill "$aligner_pid" 2>/dev/null || true
  fi
}
trap cleanup EXIT INT TERM

.venv/bin/python server.py --host "$HOST" --port "$PORT" "$@"
