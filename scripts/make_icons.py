"""从 `packaging/icon.svg` 生成所有平台要的图标。

    uv run --extra qt python scripts/make_icons.py

**只在改了 SVG 之后跑。** 产物是提交进仓库的 —— 打包时不能依赖一个要
QtSvg 才跑得起来的生成步骤(那会把 QtSvg 拖进发行包,而界面本身用不到它)。

## 出什么、给谁用

===========================================  ==========================
`packaging/icon.ico`                          PyInstaller(Windows)
`packaging/icon.icns`                         PyInstaller(macOS)
`astro_smb_app/icons/app-{16..256}.png`       **运行时**:Qt 的窗口图标
===========================================  ==========================

运行时那几张放在 `astro_smb_app/` 里面而不是 `packaging/`,是因为它们要
**随 wheel 走** —— `pip install` 之后也得有图标,而 `packaging/` 不是包。

## 为什么多给几档而不是只给一张大的

Qt 的 `QIcon` 会自己挑最接近的那一档再缩放,而**缩放出来的小图标是糊的**。
16 和 32 这两档在任务栏、Finder 侧栏、alt-tab 里天天见,值得单独渲一张。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SVG = ROOT / "packaging" / "icon.svg"

#: Windows 的 `.ico` 内嵌这几档。**16 一定要有** —— 少了它资源管理器会拿
#: 32 的缩,糊得很明显。
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)
#: macOS 的 `.icns`。Pillow 认这几个尺寸。
ICNS_SIZES = (16, 32, 64, 128, 256, 512, 1024)
#: 运行时给 Qt 的。多档是为了让 `QIcon` 有得挑,不用自己缩。
RUNTIME_SIZES = (16, 32, 64, 128, 256)
RUNTIME_DIR = ROOT / "astro_smb_app" / "icons"


def render(size: int, tmp: Path):
    """SVG → `PIL.Image`(RGBA)。用 Qt 渲,它的抗锯齿在小尺寸上明显更好。

    **中间过一趟磁盘,不走内存缓冲。** 第一版是
    ``QBuffer(QByteArray())`` —— 那个临时 `QByteArray` 当场被 Python 回收,
    而 QBuffer 还指着它,于是整个进程**段错误**(退出码 139,一个字都不说)。
    经典的 PySide6 生命周期坑:Qt 对象持有的是裸指针,不给 Python 增引用。
    """
    from PIL import Image
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(0)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setRenderHint(QPainter.SmoothPixmapTransform)
    renderer = QSvgRenderer(str(SVG))
    if not renderer.isValid():
        raise SystemExit(f"SVG 读不了: {SVG}")
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()

    out = tmp / f"{size}.png"
    if not img.save(str(out), "PNG"):
        raise SystemExit(f"{size}px 写不出来")
    with Image.open(out) as im:
        return im.convert("RGBA")


def main() -> int:
    if not SVG.is_file():
        print(f"没有 {SVG}", file=sys.stderr)
        return 2

    from PySide6.QtGui import QGuiApplication

    # QImage/QPainter 要有 QGuiApplication。离屏 —— 这是个命令行脚本。
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QGuiApplication.instance() or QGuiApplication([])

    import tempfile

    biggest = max(*ICO_SIZES, *ICNS_SIZES, *RUNTIME_SIZES)
    with tempfile.TemporaryDirectory(prefix="astro-icons-") as td:
        cache = {s: render(s, Path(td)) for s in sorted(
            {*ICO_SIZES, *ICNS_SIZES, *RUNTIME_SIZES})}

        ico = ROOT / "packaging" / "icon.ico"
        cache[biggest].save(ico, format="ICO",
                            sizes=[(s, s) for s in ICO_SIZES])
        print(f"{ico.relative_to(ROOT)}  ({', '.join(map(str, ICO_SIZES))})")

        icns = ROOT / "packaging" / "icon.icns"
        cache[1024].save(icns, format="ICNS",
                         sizes=[(s, s) for s in ICNS_SIZES])
        print(f"{icns.relative_to(ROOT)}  ({', '.join(map(str, ICNS_SIZES))})")

        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        for s in RUNTIME_SIZES:
            out = RUNTIME_DIR / f"app-{s}.png"
            cache[s].save(out, format="PNG")
            print(f"{out.relative_to(ROOT)}")

    # **别 `del app`。** QGuiApplication 提前析构会把还活着的 Qt 对象带走 ——
    # 又是一次段错误。让它跟着进程走。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
