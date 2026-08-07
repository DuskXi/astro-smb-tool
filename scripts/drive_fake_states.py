"""用**假数据**把那几条"离线镜像触发不到"的渲染分支逼出来并截图。

**这证明什么、不证明什么** —— 写在这里,免得截图被当成真机验证:

- 证明:界面拿到那种状态时**画得对** —— 分区/徽章/措辞/进度条形状/★ 置顶。
- **不证明**:真机上那个状态**会不会被触发**。watcher 的帧 mtime 心跳阈值、
  慢链路下进度回调的频率、路由器对整网段 445 的假 ACK ——
  这些只有连上真设备才算数。

也就是说:这几张图能挡住"画错了",挡不住"永远画不出来"。
两类缺陷都真实存在过(传输页「排队」分区就是画对了但判据错,永远空),
所以两种验都得做,不能互相顶替。

用法::

    uv run --with pyside6 python scripts/drive_fake_states.py [输出目录]

不给输出目录就落在 ``docs/evidence/qt/``,文件名前缀 ``fake-``。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: 拿离线镜像当"设备",纯粹是为了让外壳有东西可连;这几条分支都不读它
DEVICE = ROOT / ".tmp" / "device" / "EMMC Images"

os.environ.setdefault("ASTRO_SMB_QT_TITLE_TAG", "FAKE")
sys.argv = ["astro_smb_qt"]

from PySide6.QtCore import QTimer                       # noqa: E402
from PySide6.QtWidgets import QApplication              # noqa: E402

from astro_smb_qt import theme                          # noqa: E402
from astro_smb_qt.shell import Shell                    # noqa: E402

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "evidence" / "qt"

app = QApplication.instance() or QApplication([])
theme.apply(app)
win = Shell(host=str(DEVICE), page="browse")
win.resize(1760, 1100)
win.move(0, 0)
win.show()

steps: list = []


def shot(name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    win.grab().save(str(OUT / f"fake-{name}.png"))
    print("saved", name, flush=True)


def step_watch():
    """2.16 「正在拍摄」横幅 —— 真机靠帧 mtime 心跳,这里直接喂状态。"""
    win.select_page("records")
    win.watch.emit({
        "running": True, "target": "NGC 7293", "kind": "light",
        "seq": 42, "exposure_s": 300.0, "age_s": 37.0,
        "share": "EMMC Images", "path": "Plan\\Light\\NGC 7293",
    })
    QTimer.singleShot(1200, lambda: (shot("2.16-watch-banner"), nxt()))


def step_transfers():
    """9.1 三个分区各有任务 —— 本地拷贝瞬时完成,真机上看不到排队。"""
    from astro_smb_app import transfers as X

    win.select_page("transfers")
    jobs = []
    for label, status, done in (("排队的.fit", X.QUEUED, 0),
                                ("正在传的.fit", X.RUNNING, 3_000_000),
                                ("完成的.fit", X.DONE_S, 9_000_000)):
        j = X.TransferJob(kind="download", label=label, total=9_000_000,
                          done=done)
        j.status = status
        j.phase = (X.PH_QUEUE if status == X.QUEUED else
                   X.PH_TRANSFER if status == X.RUNNING else X.PH_DONE)
        if status == X.RUNNING:
            j.speed = 6_200_000.0
            j.parallel, j.n_chunks = 8, 64
            j.blocks = [2] * 20 + [1] * 8 + [0] * 36
        jobs.append(j)
    win.transfers.jobs.clear()
    win.transfers.jobs.extend(jobs)
    win.page("transfers").refresh()
    QTimer.singleShot(1200, lambda: (shot("9.1-three-sections"), nxt()))


def step_download_bar():
    """5.1 确定式下载进度条(带 MB)—— 本地镜像瞬时完成,截不到中间态。"""
    win.select_page("fits")
    page = win.page("fits")
    page.state.show_content()
    page.prog_card.setVisible(True)
    page._on_progress(("bytes", 18_500_000, 49_770_000))
    QTimer.singleShot(1000, lambda: (shot("5.1-download-progress"), nxt()))


def step_scan():
    """8.3 疑似 ASIAIR 置顶标 ★ —— 开发这台机器所在的网里没有 ASIAIR。"""
    from astro_smb_app.views import scan as sv

    win.select_page("scan")
    page = win.page("scan")
    rows = [
        sv.device_row("192.0.2.31", "DUSK-N100", ["Public", "Backup"], 3.2),
        sv.device_row("192.0.2.227", "ASIAIR",
                      ["EMMC Images", "TF Images", "Udisk Images"], 4.8),
    ]
    page.rows = sv.sort_rows(rows)
    page._render_rows()
    QTimer.singleShot(1000, lambda: (shot("8.3-asiair-starred"), nxt()))


def step_devices():
    """7.4/7.5 存活探测措辞与连接态 —— 记录里那台不在线,真机上探不到。"""
    win.select_page("devices")
    page = win.page("devices")
    page._rtt = {"192.0.2.227": 4.8}
    page.reload()
    QTimer.singleShot(1400, lambda: (shot("7.4-7.5-devices"), nxt()))


def nxt():
    if steps:
        QTimer.singleShot(600, steps.pop(0))
    else:
        QTimer.singleShot(500, app.quit)


steps.extend([step_transfers, step_download_bar, step_scan, step_devices])
# 头一段留给外壳把镜像扫完,不然切页会撞上正在跑的首扫
QTimer.singleShot(14000, step_watch)
QTimer.singleShot(90000, app.quit)          # 保险:再怎么卡也得自己关
app.exec()
print("done")
