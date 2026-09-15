#!/usr/bin/env bash
set -euo pipefail

base_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.work/lo-render" && pwd)"
office_root="$base_dir/root"
export LD_LIBRARY_PATH="$office_root/usr/lib/libreoffice/program:$office_root/usr/lib/x86_64-linux-gnu"
export URE_BOOTSTRAP="vnd.sun.star.pathname:/usr/lib/libreoffice/program/fundamentalrc"
task_tmp="${SOFFICE_TASK_TMPDIR:-$base_dir/tmp}"
mkdir -p "$task_tmp"
export PROOT_TMP_DIR="$task_tmp"
export TMPDIR="$task_tmp"
export XDG_CACHE_HOME="$base_dir/cache"
export SAL_USE_VCLPLUGIN="svp"

exec "$office_root/usr/bin/proot" \
  -b "$office_root/usr/lib/libreoffice:/usr/lib/libreoffice" \
  -b "$office_root/usr/share/libreoffice:/usr/share/libreoffice" \
  -b "$office_root/etc/libreoffice:/etc/libreoffice" \
  -b "$office_root/usr/share/liblangtag:/usr/share/liblangtag" \
  "$office_root/usr/lib/libreoffice/program/soffice.bin" "$@"
