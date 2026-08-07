"""2.14 的尾巴:**巡天底图的下载确认流程**,在"新机器"(空缓存)上走一遍。

清单里 2.14 一直挂着"下载确认流程没测到,底图已缓存" —— 也就是说验收时
本机早就有那 8 MB 的图,勾上就直接显示了,**问不问、下不下得来都没验过**。

和星表那条是同一个机制(先问再下),所以一起办。

要验三件事:

1. 缓存空的时候勾「巡天底图」,**弹不弹确认框**(以及框里写没写清多大、署名);
2. 点确认之后**下不下得来**;
3. 下完**底图有没有真的画到天球上**(不是只把开关点亮)。

用法:``uv run --with pyside6 python drive_skybg_newmachine.py``
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIRROR = ROOT / ".tmp" / "device" / "EMMC Images"
OUT = Path(__file__).resolve().parents[1] / "docs" / "evidence" / "qt"

FRESH = Path(tempfile.mkdtemp(prefix="newmachine-sky-"))
os.environ["LOCALAPPDATA"] = str(FRESH)
os.environ["XDG_DATA_HOME"] = str(FRESH / "data")
os.environ["XDG_CACHE_HOME"] = str(FRESH / "cache")
os.environ["USERPROFILE"] = str(FRESH)
os.environ.setdefault("ASTRO_SMB_QT_TITLE_TAG", "NEWMACHINE")
sys.argv = ["astro_smb_qt"]
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QTimer                       # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

from astro_smb_app import skymap                        # noqa: E402
from astro_smb_qt import theme                          # noqa: E402
from astro_smb_qt.shell import Shell                    # noqa: E402

print(f"新机器缓存: {FRESH}")
print(f"底图就绪?  {skymap.survey_available()}   (期望 False)")
assert not skymap.survey_available(), "这台'新机器'上已经有底图了,验不出东西"

app = QApplication.instance() or QApplication([])
theme.apply(app)
win = Shell(host=str(MIRROR), page="records")
win.resize(1760, 1100)
win.move(0, 0)
win.show()

page = win.page("records")
t0 = time.time()
seen_dialog = [False]


def shot(name: str) -> None:
    win.grab().save(str(OUT / f"newmachine-sky-{name}.png"))
    print(f"[{time.time() - t0:5.1f}s] 截图 {name}", flush=True)


def grab_dialog():
    """确认框是模态的:主窗口被挡住时把**整屏那个框**单独截下来。"""
    for w in app.topLevelWidgets():
        if isinstance(w, (QMessageBox, QDialog)) and w.isVisible():
            w.grab().save(str(OUT / "newmachine-sky-1-confirm.png"))
            txt = w.text() if isinstance(w, QMessageBox) else ""
            print(f"[{time.time() - t0:5.1f}s] 确认框: {txt[:160]!r}", flush=True)
            seen_dialog[0] = True
            # 点「是」
            if isinstance(w, QMessageBox):
                for b in w.buttons():
                    if w.buttonRole(b) in (QMessageBox.ButtonRole.YesRole,
                                           QMessageBox.ButtonRole.AcceptRole):
                        b.click()
                        return True
            w.accept()
            return True
    return False


def step_check():
    print(f"[{time.time() - t0:5.1f}s] 勾「巡天底图」", flush=True)
    QTimer.singleShot(600, poll_dialog)
    page.bg_box.setChecked(True)          # 触发 _set_sky_bg → 模态确认框


def poll_dialog():
    if grab_dialog():
        QTimer.singleShot(2000, watch)
    elif time.time() - t0 < 40:
        QTimer.singleShot(300, poll_dialog)
    else:
        print("!! 一直没等到确认框")
        QTimer.singleShot(200, app.quit)


def watch():
    if skymap.survey_available():
        print(f"[{time.time() - t0:5.1f}s] 底图下载完成", flush=True)
        QTimer.singleShot(6000, finish)
        return
    if time.time() - t0 > 240:
        print("!! 底图下载超时")
        shot("timeout")
        QTimer.singleShot(200, app.quit)
        return
    QTimer.singleShot(1000, watch)


def finish():
    shot("2-after")
    p = skymap.survey_path()
    print(f"\n确认框出现过: {seen_dialog[0]}")
    print(f"底图: {p}  {p.stat().st_size / 1e6:.1f} MB" if p.is_file()
          else f"底图: {p} 不存在")
    print(f"页面 sky_bg 开关: {page.sky_bg}")
    print(f"署名可见: {page.sky_credit.isVisible()} —— "
          f"{page.sky_credit.text()[:90]!r}")
    QTimer.singleShot(300, app.quit)


QTimer.singleShot(12000, step_check)      # 等日志解析完、天球先画出来
QTimer.singleShot(300_000, app.quit)
app.exec()
print("done")
