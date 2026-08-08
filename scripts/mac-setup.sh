#!/usr/bin/env bash
# macOS 上把环境备好。跑一次就够。
#
#   ./scripts/mac-setup.sh
#
# 只装依赖、跑一遍测试。不签名、不打包 —— 打包见 scripts/package.py。
#
# **不需要 Xcode,不需要 .NET,不需要自己装 Python。** uv 会照
# `.python-version` 自己拉 3.13。
set -euo pipefail

cd "$(dirname "$0")/.."

say() { printf '\033[1;36m==> %s\033[0m\n' "$*"; }
die() { printf '\033[1;31m!! %s\033[0m\n' "$*" >&2; exit 1; }

say "检查前置工具"
command -v uv >/dev/null 2>&1 || die \
  "缺 uv。装:curl -LsSf https://astral.sh/uv/install.sh | sh(装完重开终端)"

ARCH="$(uname -m)"
case "$ARCH" in
  x86_64) RID=osx-x64 ;;
  arm64)  RID=osx-arm64 ;;
  *)      die "不认识的架构 $ARCH" ;;
esac
say "架构 $ARCH → $RID · uv $(uv --version | awk '{print $2}')"

# `--extra qt` 装 PySide6(约 200 MB,第一次慢)。**它不在必装依赖里** ——
# 只用命令行的人不该被拖去下这么多。dev 组里也有一份,所以裸 `uv sync`
# 同样够用;这里写全是为了让这条命令单独看也说得通。
say "同步 Python 环境(第一次要下 PySide6,约 200 MB)"
uv sync --extra qt

# win32more 带着 `sys_platform == 'win32'` 标记,mac 上会被跳过 —— 那是
# 预期的。少了它 `astro_smb_gui`(Windows 原生的那套界面)import 不了,
# `tests/conftest.py` 会把相关模块整块跳过,而不是让整轮变红。
say "跑一遍离线单测"
uv run --extra qt pytest -q

say "完成。启动:  ./scripts/mac-run.sh"
say "打包:      uv run --extra qt python scripts/package.py --smoke   ($RID)"
