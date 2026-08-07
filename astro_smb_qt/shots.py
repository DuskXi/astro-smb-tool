r"""截图/演练脚本(开发用,不进产品路径)。

**两条纪律,都是这个仓库真机踩出来的:**

1. **窗口按标题标签认,不按进程名。** 用户常开着自己的实例 —— 按进程名匹配
   一定会误抓,截图里是他点开的目录、他勾的文件。所以这里起的实例一律带
   ``ASTRO_SMB_QT_TITLE_TAG=AGENT``,而留给用户手动体验的那个用别的标签。
2. **自带超时自关。** 没有自关的探针在迭代验证里每几分钟泄漏一个进程,
   几轮就吃掉几个 GB。这里每一次都走 ``--seconds``。

用法::

    uv run --with pyside6 python -m astro_smb_qt.shots --host "<设备或本地目录>"
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path
from astro_smb.i18n import gettext as _

#: 只截自己这一个实例
AGENT_TAG = "AGENT"

#: (文件名, 启动页, 停留秒数, 额外参数)
SHOTS = [
    ("01-browse", "browse", 20, ["--auto"]),
    ("02-transfers", "transfers", 16, ["--auto"]),
    ("03-devices", "devices", 10, []),
    ("04-scan", "scan", 10, ["--auto"]),
    ("05-records", "records", 22, []),
    ("06-guiding", "guiding", 26, []),
    ("07-space", "space", 40, ["--auto"]),
    ("08-fits", "fits", 6, []),
    ("09-sky", "sky", 6, []),
    ("10-browse-red", "browse", 20, ["--auto", "--theme", "red"]),
]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m astro_smb_qt.shots")
    ap.add_argument("--host", required=True)
    ap.add_argument("--out", default=".tmp/shots")
    ap.add_argument("--only", default="", help=_("只跑名字里含这个串的那几张"))
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, ASTRO_SMB_QT_TITLE_TAG=AGENT_TAG,
               PYTHONIOENCODING="utf-8")

    rc = 0
    for name, page, seconds, extra in SHOTS:
        if args.only and args.only not in name:
            continue
        png = out / f"{name}.png"
        cmd = [sys.executable, "-m", "astro_smb_qt", "--host", args.host,
               "--page", page, "--seconds", str(seconds), "--shot", str(png),
               *extra]
        print(f"[shots] {name} … ", end="", flush=True)
        # 子进程的中文输出必须显式按 utf-8 解 —— Windows 上 text=True 默认
        # 走 GBK,读到中文日志会在 reader 线程里抛 UnicodeDecodeError。
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=seconds + 90)
        ok = png.is_file()
        print("ok" if ok else _('失败\n{stdout}\n{stderr}').format(
            stdout=proc.stdout, stderr=proc.stderr))
        rc |= 0 if ok else 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
