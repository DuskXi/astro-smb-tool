"""传输页的分块方块图。

用户报"传输貌似没有小格子了"。查下来**格子本身是好的** —— 是本地设备
(卡直插 / 离线镜像)**刻意不走分块并发**:分块的全部价值是掩盖 SMB 的单流
RTT 瓶颈(实测单流 6 MiB/s、8 并发才 9.6),本地盘顺序读就有 1.48 GB/s,
开 8 个句柄同时 seek+write 只会打乱磁盘调度。老 UI 在本地设备上同样没有格子。

所以这里钉两件事:
① 有分块时格子真的画得出来(离线,不需要设备);
② **没有格子时界面要说明为什么** —— 什么都不说的话用户只能怀疑是坏了。
"""
from __future__ import annotations

import pytest

from astro_smb_app.views import transfers as tv


class _Job:
    """够 `row_model` 用的最小桩。"""

    def __init__(self, **kw):
        self.job_id = 1
        self.label = "Light_0001.fit"
        self.group = ""
        self.status = "传输中"
        self.phase = "传输"
        self.total = 1000
        self.done = 500
        self.speed = 0.0
        self.blocks: list[int] = []
        self.n_chunks = 0
        self.parallel = 0
        self.error = ""
        self.local_device = False
        self.__dict__.update(kw)


class TestBlocksSurvive:

    def test_states_reach_the_row(self):
        job = _Job(blocks=[2, 2, 1, 0, 0, 0], n_chunks=6)
        assert tv.row_model(job)["blocks"] == [2, 2, 1, 0, 0, 0]

    def test_downsampled_but_not_emptied(self):
        """块数很多时要降采样,但**不能降没**。"""
        job = _Job(blocks=[1] * 4000, n_chunks=4000)
        got = tv.row_model(job)["blocks"]
        assert got, "降采样把格子降没了"
        assert len(got) <= tv.MAX_BLOCKS

    def test_no_blocks_is_empty_not_none(self):
        assert tv.row_model(_Job())["blocks"] == []


class TestExplainWhyThereAreNoBlocks:

    def test_local_device_says_so(self):
        m = tv.row_model(_Job(local_device=True))
        assert m["no_blocks_why"], (
            "本地设备没有格子却什么都不说 —— 用户只会以为坏了")
        assert "本地" in m["no_blocks_why"]

    def test_remote_device_says_nothing(self):
        """SMB 设备没有格子是别的原因(文件太小/单线程),别乱解释。"""
        assert tv.row_model(_Job(local_device=False))["no_blocks_why"] == ""

    def test_the_page_actually_shows_it(self):
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1] / "astro_smb_qt" / "pages"
               / "transfers.py").read_text(encoding="utf-8")
        assert "no_blocks_why" in src, "页面没读这个字段,等于没做"


class TestBlockMapPaints:

    def test_paints_without_crashing(self):
        pytest.importorskip("PySide6")
        from PySide6.QtWidgets import QApplication

        from astro_smb_qt import theme, widgets as W

        app = QApplication.instance() or QApplication([])
        theme.apply(app)
        bm = W.BlockMap()
        bm.set_states([0, 1, 2] * 20)
        bm.resize(300, bm.height())
        assert not bm.grab().isNull()

    def test_height_grows_with_rows(self):
        pytest.importorskip("PySide6")
        from PySide6.QtWidgets import QApplication

        from astro_smb_qt import theme, widgets as W

        app = QApplication.instance() or QApplication([])
        theme.apply(app)
        bm = W.BlockMap()
        bm.set_states([0] * 8)
        one = bm.height()
        bm.set_states([0] * (tv.BLOCK_COLS * 3))
        assert bm.height() > one, "格子多到要换行时高度没跟着长 —— 会被裁掉"
