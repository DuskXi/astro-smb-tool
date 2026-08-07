#!/usr/bin/env bash
# 在 macOS 上启动 Astro SMB Tool 的跨平台界面。
#
#   ./scripts/mac-run.sh                        # 读 devices.json 自己连
#   ./scripts/mac-run.sh --host 192.0.2.227   # 指定设备
#   ./scripts/mac-run.sh --page records         # 直达某一页
#
# 所有参数原样透给 astro-smb-tool-ui,见 `--help`。
set -euo pipefail

cd "$(dirname "$0")/.."

FRONTEND="frontend/src/AstroSmbTool.Uno/AstroSmbTool.Uno/bin/Debug/net9.0-desktop/AstroSmbTool.Uno"
if [ ! -x "$FRONTEND" ] && [ ! -f "$FRONTEND.dll" ]; then
  printf '\033[1;31m!! 还没构建前端。先跑 ./scripts/mac-setup.sh\033[0m\n' >&2
  exit 2
fi

# Python 是主进程:它拿一个临时端口、拉起 C# 渲染子进程、崩了负责重启。
# 所以这里只需要启动 Python 那一侧。
exec uv run astro-smb-tool-ui "$@"
