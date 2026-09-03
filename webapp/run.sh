#!/usr/bin/env bash
# Khởi động Video Dubbing Studio. Lần đầu chạy sẽ tự tạo venv và cài dependency.
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8199}"
HOST="${HOST:-127.0.0.1}"

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

exec .venv/bin/python server.py --host "$HOST" --port "$PORT" "$@"
