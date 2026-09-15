#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

model_name="sherpa-onnx-sense-voice-funasr-nano-int8-2025-12-17"
model_dir="models/$model_name"
archive_dir="models/downloads"
archive="$archive_dir/funasr-nano-int8.tar.bz2"
expected_sha256="257936ea9a64cbe33200274e6367fc26d373ff6ca58b996b15108ffd6b9f6148"
url="https://modelscope.cn/models/ZhaoChaoqun/sherpa-onnx-asr-models/resolve/master/$model_name.tar.bz2"

if [[ -f "$model_dir/model.int8.onnx" && -f "$model_dir/tokens.txt" ]]; then
  echo "模型已存在：$model_dir"
  exit 0
fi

mkdir -p "$archive_dir"
curl -L --fail --retry 3 --connect-timeout 20 -o "$archive" "$url"
echo "$expected_sha256  $archive" | sha256sum --check --strict
tar -xjf "$archive" -C models

if [[ ! -f "$model_dir/model.int8.onnx" || ! -f "$model_dir/tokens.txt" ]]; then
  echo "模型解压后不完整：$model_dir" >&2
  exit 1
fi

echo "模型准备完成：$model_dir"
