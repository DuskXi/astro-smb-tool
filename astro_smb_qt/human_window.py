r"""重启**留给人手动体验**的那个窗口(开发用,不进产品路径)。

两条纪律:

1. **按标题标签认窗口,不按进程名。** 用户常开着自己的实例 —— 按进程名匹配
   一定会误抓(docs/DEVELOPMENT.md §7.10 记着这条真机教训)。这里只杀标题里带
   ``[人类体验窗口]`` 的那些,永远不碰 ``[AGENT]``。
2. **人的那个不带 ``--seconds``**(他要能一直开着);自己的实验窗口才要带,
   否则每跑一次泄漏一个进程。

用法::

    uv run --with pyside6 python -m astro_smb_qt.human_window --host "<设备或本地目录>"
    uv run --with pyside6 python -m astro_smb_qt.human_window --stop
"""
from __future__ import annotations

import argparse
import os
import pathlib
import subprocess
from astro_smb.i18n import gettext as _

TAG = "人类体验窗口"


#: 按**命令行**认自己人:命令行里带这个环境变量的进程就是本工具起的。
#: 光看进程名会误抓(用户可能开着别的 python),而 ``taskkill /FI WINDOWTITLE``
#: 在中文标题上直接报"无法识别的筛选器"(实测),所以走 PowerShell 读 CIM。
_PS_STOP = r"""
$hits = Get-CimInstance Win32_Process -Filter "Name like '%python%'" |
  Where-Object {{ $_.CommandLine -like '*astro_smb_qt*' }}
$killed = 0
foreach ($p in $hits) {{
  $proc = Get-Process -Id $p.ProcessId -ErrorAction SilentlyContinue
  if ($null -ne $proc -and $proc.MainWindowTitle -like '*[{tag}]*') {{
    Stop-Process -Id $p.ProcessId -Force
    Write-Output ("killed " + $p.ProcessId + " " + $proc.MainWindowTitle)
    $killed = $killed + 1
  }}
}}
if ($killed -eq 0) {{ Write-Output "没有找到旧的[{tag}]" }}
"""


def _stop() -> int:
    """按**窗口标题标签**关掉旧的那个 —— 绝不碰 ``[AGENT]`` 那些。"""
    r = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         _PS_STOP.format(tag=TAG)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    print((r.stdout or r.stderr).strip() or _("(没有输出)"))
    return 0


def _start(host: str) -> int:
    """拉起新窗口。

    **必须走 ``uv run --with pyside6``,不能直接用 ``sys.executable``。**
    PySide6 是靠 ``--with`` 临时注入的,那个环境是 uv 的临时目录 ——
    本进程一退出它就可能被回收,detach 出去的子进程会连解释器都找不着
    (实测:窗口起来几秒后无声无息地没了)。让 uv 自己管住那个进程的生命周期。
    """
    env = dict(os.environ, ASTRO_SMB_QT_TITLE_TAG=TAG, PYTHONIOENCODING="utf-8")
    cmd = ["uv", "run", "--with", "pyside6", "python", "-m", "astro_smb_qt"]
    if host:
        cmd += ["--host", host]
    # **不给 --seconds** —— 人的窗口要一直开着
    proc = subprocess.Popen(
        cmd, env=env, cwd=str(pathlib.Path(__file__).resolve().parents[1]),
        creationflags=(getattr(subprocess, "DETACHED_PROCESS", 0)
                       | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)),
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)
    print(_("已拉起人类体验窗口 pid={pid} 标题=Astro SMB Tool (Qt) [{TAG}]").format(
        pid=proc.pid, TAG=TAG))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m astro_smb_qt.human_window")
    ap.add_argument("--host", default="")
    ap.add_argument("--stop", action="store_true", help=_("只关掉,不重开"))
    args = ap.parse_args(argv)
    _stop()
    return 0 if args.stop else _start(args.host)


if __name__ == "__main__":
    raise SystemExit(main())
