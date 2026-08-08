"""打一个解开就能跑的分发包(**不签名**)。

    uv run --extra qt python scripts/package.py
    uv run --extra qt python scripts/package.py --smoke     # 打完真启动一次

产物在 `dist/astro-smb-tool/`,目标机不用装 Python、不用装 Qt。

## 只能原生打,**没有交叉编译这回事**

PyInstaller 把**当前正在跑的那个解释器**连同它已装好的原生扩展一起塞进包里。
所以:

* Windows 上打不出 Linux 包,反之亦然;
* mac 的 x86_64 与 arm64 也**各打各的** —— 想要一个 universal2 单包,得让
  链路上每一个原生轮子都是 universal2,而 numpy / pillow / cffi 都只发
  分架构的轮子(2026-08 实测)。PySide6 自己倒是 universal2。

结论:三平台四个架构靠 **CI 上四台原生机器**出,不靠交叉编译。
对应关系在 `.github/workflows/release.yml`。

## 打完要真的启动一次

**包能打出来不等于包能用。** 少一个随包资源、少一个 Qt 插件,启动才炸,
而那时"构建成功"这四个字已经绿了。`--smoke` 用 `--seconds` 起一个真窗口,
几秒后自己退;无头机器上靠 `QT_QPA_PLATFORM=offscreen` 也能跑。
"""
from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "packaging" / "astro-smb-tool.spec"
OUT = ROOT / "dist" / "astro-smb-tool"


def rid() -> str:
    """当前平台+架构的标签。**arch 要看真的那个** —— mac 上 x64 与 arm64
    的产物不能互换,而在 Rosetta 下跑的 Python 会自称 x86_64,那也是对的:
    它打出来的确实是 x86_64 的包。"""
    machine = platform.machine().lower()
    arm = machine in ("arm64", "aarch64")
    if sys.platform == "win32":
        return "win-arm64" if arm else "win-x64"
    if sys.platform == "darwin":
        return "osx-arm64" if arm else "osx-x64"
    return "linux-arm64" if arm else "linux-x64"


def run(argv: list[str]) -> None:
    print("$", " ".join(argv), flush=True)
    subprocess.run(argv, check=True, cwd=ROOT)


def build() -> Path:
    for d in (ROOT / "dist" / "astro-smb-tool", ROOT / "build"):
        shutil.rmtree(d, ignore_errors=True)
    run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--distpath", str(ROOT / "dist"),
         "--workpath", str(ROOT / "build"),
         str(SPEC)])
    return OUT


def exe_path() -> Path:
    return OUT / ("astro-smb-tool.exe" if sys.platform == "win32"
                  else "astro-smb-tool")


def smoke() -> int:
    """真启动一次:造一个空设备目录,起窗口,几秒后自退。

    **用本地目录当设备**,不连网络 —— CI 上没有 ASIAIR,而"界面起不起得来"
    和"连不连得上设备"是两件事,这里只验前者。
    """
    exe = exe_path()
    if not exe.is_file():
        print(f"没有可执行文件: {exe}", file=sys.stderr)
        return 2

    env = dict(os.environ)
    # 无头机器上没有 X11/Wayland,建 QWidget 会直接 abort(不是异常,是进程
    # 没了)。离屏平台插件是 Qt 自带的。有显示时不覆盖,那样才是真窗口。
    if sys.platform.startswith("linux") and not (
            env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        env.setdefault("QT_QPA_PLATFORM", "offscreen")

    # 先自检。**顺序是有讲究的**:窗口起得来说明不了随包资源在不在 ——
    # 翻译缺了只是界面永远中文,天球资产缺了只是那一页空白,两样都不报错、
    # 都不影响启动。反过来自检过了窗口起不来,也还是坏的。所以两条都要。
    print("$", exe, "--selftest", flush=True)
    if subprocess.run([str(exe), "--selftest"], env=env).returncode != 0:
        return 1

    with tempfile.TemporaryDirectory(prefix="astro-smb-smoke-") as tmp:
        dev = Path(tmp) / "EMMC Images"
        (dev / "log").mkdir(parents=True)
        (dev / "Autorun").mkdir()
        argv = [str(exe), "--host", str(dev), "--seconds", "4"]
        print("$", " ".join(argv), flush=True)
        p = subprocess.run(argv, env=env, cwd=tmp)
    if p.returncode != 0:
        print(f"起来了但退出码是 {p.returncode}", file=sys.stderr)
        return p.returncode
    print("冒烟通过:自检过了,窗口起得来,四秒后自己退了")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="打分发包(不签名)")
    ap.add_argument("--smoke", action="store_true",
                    help="打完真启动一次(**强烈建议**,尤其在 CI 上)")
    ap.add_argument("--smoke-only", action="store_true",
                    help="不打,只对现有产物跑一次冒烟")
    args = ap.parse_args(argv)

    if not args.smoke_only:
        if not SPEC.is_file():
            print(f"没有规格文件: {SPEC}", file=sys.stderr)
            return 2
        build()

    exe = exe_path()
    size = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    print(f"\n目标平台: {rid()}")
    print(f"产物:     {OUT}  ({size / 1048576:.0f} MB)")
    print(f"启动:     {exe}")

    if args.smoke or args.smoke_only:
        return smoke()
    print("\n**没做冒烟测试。** 打出来不等于跑得起来,加 --smoke。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
