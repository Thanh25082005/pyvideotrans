#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -x .aligner-venv/bin/python ]; then
  uv venv .aligner-venv --python 3.12
fi

# RTX 50-series cần wheel CUDA 12.8.
UV_NO_CONFIG=1 uv pip install --python .aligner-venv/bin/python \
  torch==2.7.1 torchaudio==2.7.1 \
  --index-url https://download.pytorch.org/whl/cu128
UV_NO_CONFIG=1 uv pip install --python .aligner-venv/bin/python qwen-asr==0.0.6
# qwen-asr 0.0.6 cần Transformers 4.57.6. Repo cha dùng Transformers 5.x,
# vì vậy khóa lại trong venv biệt lập này.
UV_NO_CONFIG=1 uv pip install --python .aligner-venv/bin/python --no-deps \
  transformers==4.57.6 'huggingface-hub>=0.34,<1.0'

echo "Đã cài Qwen3 Forced Aligner runtime. Model sẽ tải ở lần load đầu tiên."
