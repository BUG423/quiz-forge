#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
mkdir -p outputs

# 如果传入了当前前台任务 PID，先等待它结束；随后从进度文件断点续跑。
existing_pid="${1:-}"
if [[ -n "$existing_pid" ]]; then
  while kill -0 "$existing_pid" 2>/dev/null; do
    sleep 15
  done
fi

export HF_HUB_OFFLINE=1
export HF_HUB_DISABLE_TELEMETRY=1
export TRANSFORMERS_OFFLINE=1
export ASR_CPU_THREADS="${ASR_CPU_THREADS:-16}"

exec .venv/bin/python scripts/transcribe_data.py >> outputs/transcription.log 2>&1
