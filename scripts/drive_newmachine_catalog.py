"""**模拟一台新机器**,把 Qt 的星表流程从头走一遍并截图。

新机器 = 应用数据目录是空的。Windows 上那个目录由 `LOCALAPPDATA` 决定
(`astro_smb/paths.py`),所以把它指到一个临时目录就够了 —— 不用真找一台机器。

要验的四件事(用户报的是"星表没有自动下载"):

1. 没星表时点「板解算」,**说不说得清**(未就绪 / 从哪儿取 / 多大);
2. 「下载星表」按钮**在不在**;
3. 点了**下不下得来**(2026-08-03 之前这里第一次进度回调就 TypeError);
4. 下完**接不接着把刚才那次解算跑完**。

用法:``uv run --with pyside6 python drive_catalog_newmachine.py``
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIRROR = ROOT / ".tmp" / "device" / "EMMC Images"
FIT = ("Plan/Light/M 8/"
       "Light_M 8_180.0s_Bin1_4C_20260723-221336_2deg_0001.fit")
OUT = Path(__file__).resolve().parents[1] / "docs" / "evidence" / "qt"

# ---- 新机器:空的应用数据目录,且没有任何指向现成星表的环境变量
FRESH = Path(tempfile.mkdtemp(prefix="newmachine-"))
os.environ["LOCALAPPDATA"] = str(FRESH)
os.environ["XDG_DATA_HOME"] = str(FRESH / "data")
os.environ["XDG_CACHE_HOME"] = str(FRESH / "cache")
os.environ["USERPROFILE"] = str(FRESH)
os.environ.pop("ASTRO_SMB_CATALOG_PATH", None)
os.environ.setdefault("ASTRO_SMB_QT_TITLE_TAG", "NEWMACHINE")
sys.argv = ["astro_smb_qt"]

sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QTimer                       # noqa: E402
from PySide6.QtWidgets import QApplication              # noqa: E402

from astro_smb import catalog                           # noqa: E402
from astro_smb_qt import theme                          # noqa: E402
from astro_smb_qt.shell import Shell                    # noqa: E402

print(f"新机器数据目录: {FRESH}")
print(f"星表路径:      {catalog.catalog_path()}")
print(f"星表就绪?      {catalog.catalog_available()}   (期望 False)")
assert not catalog.catalog_available(), "这台'新机器'上已经有星表了,验不出东西"

app = QApplication.instance() or QApplication([])
theme.apply(app)
win = Shell(host=str(MIRROR), page="fits")
win.resize(1760, 1100)
win.move(0, 0)
win.show()

page = win.page("fits")
log: list[str] = []
t0 = time.time()


def shot(name: str) -> None:
    win.grab().save(str(OUT / f"newmachine-cat-{name}.png"))
    print(f"[{time.time() - t0:5.1f}s] 截图 {name}", flush=True)


def rows_text() -> str:
    return " | ".join(f"{a}={b}" for a, b in
                      (page.model.get("solve_rows") or []))


def step_open():
    """打开那张真片。"""
    win.open_fits("EMMC Images", FIT.replace("/", "\\"))
    QTimer.singleShot(6000, step_solve)


def step_solve():
    """点「板解算」—— 没星表,应该给出说明 + 下载按钮。"""
    print(f"[{time.time() - t0:5.1f}s] 点板解算", flush=True)
    page._solve()
    QTimer.singleShot(1500, check_offer)


def check_offer():
    offer = bool(page.model.get("catalog_offer"))
    btn = page.catalog_btn is not None and page.catalog_btn.isVisible()
    txt = rows_text()
    print(f"  catalog_offer={offer}  按钮在={btn}")
    print(f"  说明: {txt[:200]}")
    log.append(f"1/2 说明与按钮: offer={offer} btn={btn}")
    shot("1-offer")
    if not (offer and btn):
        print("!! 没给出下载入口 —— 用户报的现象复现了")
        QTimer.singleShot(500, app.quit)
        return
    QTimer.singleShot(500, step_download)


def step_download():
    print(f"[{time.time() - t0:5.1f}s] 点「下载星表」", flush=True)
    page._download_catalog()
    QTimer.singleShot(4000, watch)


_last = [""]


def watch():
    msg = str(page.model.get("solve") or "")
    if msg != _last[0]:
        _last[0] = msg
        print(f"[{time.time() - t0:5.1f}s] {msg[:90]}", flush=True)
    if "获取星表" in msg and "newmachine-cat-2-progress.png" not in log:
        shot("2-progress")
        log.append("newmachine-cat-2-progress.png")
    done = catalog.catalog_available()
    if done and "获取星表" not in msg:
        print(f"[{time.time() - t0:5.1f}s] 星表就绪,等自动接着解算…", flush=True)
        QTimer.singleShot(8000, finish)
        return
    if time.time() - t0 > 300:
        print("!! 超时 300s")
        shot("timeout")
        QTimer.singleShot(500, app.quit)
        return
    QTimer.singleShot(1000, watch)


def finish():
    shot("3-after")
    p = catalog.catalog_path()
    size = p.stat().st_size if p.is_file() else 0
    print(f"\n星表: {p}  {size / 1e6:.1f} MB")
    print(f"解算结果行数: {len(page.model.get('solve_rows') or [])}")
    print(f"结果: {rows_text()[:400]}")
    QTimer.singleShot(500, app.quit)


QTimer.singleShot(3000, step_open)
QTimer.singleShot(400_000, app.quit)      # 保险
app.exec()
print("done")
