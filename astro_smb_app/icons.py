"""应用图标的定位。

图标要在三个地方出现,而**三个地方拿它的方式不一样**:

* PyInstaller —— 构建时读 `packaging/icon.ico` / `.icns`,不经过这里;
* Qt 窗口 —— 运行时读随包的 PNG(这个模块);
* wheel —— 那几张 PNG 住在 `astro_smb_app/icons/`,所以 `pip install`
  之后也在。放 `packaging/` 里就不会被打进 wheel。

**给 `QIcon` 喂多档,别喂一张大的。** Qt 会挑最接近的那一档;只给 256 的话
任务栏那个 16px 是缩出来的,糊。
"""
from __future__ import annotations

from pathlib import Path

#: 与 `scripts/make_icons.py` 里的 `RUNTIME_SIZES` 对齐
SIZES = (16, 32, 64, 128, 256)


def icon_dir() -> Path:
    """图标目录。冻结后走 `bundle`,开发时就在包里。"""
    from astro_smb_app import bundle

    dev = Path(__file__).resolve().parent / "icons"
    return bundle.data_file("astro_smb_app", "icons",
                            package_relative=dev) or dev


def icon_files() -> list[Path]:
    """存在的那几档,从小到大。一个都没有时返回空列表。"""
    d = icon_dir()
    return [p for s in SIZES if (p := d / f"app-{s}.png").is_file()]
