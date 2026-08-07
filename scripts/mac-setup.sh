#!/usr/bin/env bash
# Intel macOS (x86_64) 上把环境备好。跑一次就够。
#
# 只装依赖、只构建,不做签名也不打包 —— 目标是"能用命令跑起来"。
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$(pwd)"

say() { printf '\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m!! %s\033[0m\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- 前置检查
say "检查前置工具"
command -v uv >/dev/null 2>&1 || die "缺 uv。装:curl -LsSf https://astral.sh/uv/install.sh | sh"
command -v dotnet >/dev/null 2>&1 || die "缺 dotnet。装 .NET SDK 9:https://dotnet.microsoft.com/download"

DOTNET_MAJOR="$(dotnet --version | cut -d. -f1)"
[ "$DOTNET_MAJOR" -ge 9 ] || die "需要 .NET SDK 9 或更新,当前 $(dotnet --version)"

ARCH="$(uname -m)"
say "架构 $ARCH · dotnet $(dotnet --version)"
if [ "$ARCH" != "x86_64" ]; then
  printf '\033[1;33m注意:这台不是 Intel。脚本照样能跑(RID 会用 osx-arm64),\n'
  printf '     但本项目目前只在 x86_64 上验证过。\033[0m\n'
fi

# ---------------------------------------------------------------- Python 侧
say "同步 Python 环境(uv sync)"
# win32more 在 pyproject 里带着 sys_platform == 'win32' 标记,mac 上会被跳过。
# 少了它 `astro_smb_gui`(老 WinUI 界面)import 不了 —— 那是预期的,
# mac 上跑的是新的 Uno 前端,老 UI 本来就只在 Windows 服役。
uv sync

say "跑一遍不连设备的单测"
uv run pytest tests/ -q -x --ignore=tests/test_legacy_ui_freeze.py

# ---------------------------------------------------------------- C# 侧
say "还原并构建 Uno 渲染器"
# Uno 的单项目模板要 workload;没装就装上(需要 sudo)。
if ! dotnet workload list 2>/dev/null | grep -qi 'wasm-tools\|uno'; then
  say "安装 Uno workload(需要 sudo)"
  dotnet workload install wasm-tools || true
fi

# 先把不依赖 Uno 的协议库与它的测试跑通 —— 那一层出问题,前端一定也起不来
dotnet test frontend/tests/AstroSmbTool.Protocol.Tests/AstroSmbTool.Protocol.Tests.csproj \
  -v q --nologo

dotnet build frontend/src/AstroSmbTool.Uno/AstroSmbTool.Uno/AstroSmbTool.Uno.csproj \
  -v q --nologo

say "完成。启动:  ./scripts/mac-run.sh   (或 ./scripts/mac-run.sh --host 192.0.2.227)"
