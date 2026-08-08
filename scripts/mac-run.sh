#!/usr/bin/env bash
# 在 macOS 上启动 Qt 界面。
#
#   ./scripts/mac-run.sh                                  # 不给地址就自动扫本网段
#   ./scripts/mac-run.sh --host 192.0.2.227               # 指定设备
#   ./scripts/mac-run.sh --host "/Volumes/ASIAIR"         # 存储卡直插
#   ./scripts/mac-run.sh --page records                   # 直达某一页
#
# 参数原样透给 `astro-smb-tool-qt`,见 `--help`。
set -euo pipefail

cd "$(dirname "$0")/.."

command -v uv >/dev/null 2>&1 || {
  printf '\033[1;31m!! 缺 uv。先跑 ./scripts/mac-setup.sh\033[0m\n' >&2
  exit 2
}

# `--extra qt` 是幂等的:装过了就直接起,没装会先补上。
exec uv run --extra qt astro-smb-tool-qt "$@"
