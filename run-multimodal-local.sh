#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

export HF_HUB_OFFLINE=1
export HF_HUB_DISABLE_TELEMETRY=1
export TRANSFORMERS_OFFLINE=1

exec .venv/bin/python scripts/multimodal_review.py "$@"
