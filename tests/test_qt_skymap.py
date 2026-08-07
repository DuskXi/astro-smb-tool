"""全天位置的巡天底图 —— 用户报"全天位置没有巡天图了"。

老 UI 有:开关 → 下载(约 8 MB,**要用户点头**)→ 按站点与时刻重投影成
alt-az 圆盘 → 叠在天球圈**下面**。这一套的价值不在好看:银河一铺开,
"这一夜的目标都挤在银道面上"或者"这个目标其实在光害最重的方向"一眼就有。

两条几何铁律单独钉:

* **直径 = 2×地平线半径**,不是画布宽。拉伸到整张画布的话星点与银河错位,
  而错位在一张星图上几乎看不出来(老 UI 真机踩过"M 8 不在银心")。
* **底图与点用同一个时刻**。各用各的时刻是同一个错位的另一种来法。
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "astro_smb_qt" / "pages" / "records.py").read_text(
    encoding="utf-8")


class TestSkyPayloadCarriesTheTimestamp:
    """重投影要的是 unix 时刻,而模型原来只给了一句格式化字符串。"""

    def test_models_return_ts(self):
        from astro_smb_qt import models
        import inspect

        src = inspect.getsource(models.sky_payload)
        assert '"ts"' in src, (
            "sky_payload 不回传 ts —— 底图只能自己另取一个时刻,必然与点错位")

    def test_page_uses_that_ts(self):
        at = SRC.index("def _refresh_sky_bg")
        body = SRC[at:SRC.index("\n    def ", at + 10)]
        assert 'sky.get("ts")' in body, (
            "底图没用天球那一份时刻 —— 星点会与银河错位")


class TestGeometry:

    def test_background_is_the_horizon_disk(self):
        at = SRC.index("def _refresh_sky_bg")
        body = SRC[at:SRC.index("\n    def ", at + 10)]
        assert "SKY_MARGIN" in body, (
            "底图矩形没扣掉 margin —— 直径不等于地平圈直径,点会错位")
        assert "2 * r" in body, "底图直径不是 2×半径"

    def test_canvas_can_take_a_background(self):
        pytest.importorskip("PySide6")
        from PySide6.QtWidgets import QApplication

        from astro_smb_qt import theme, widgets as W

        app = QApplication.instance() or QApplication([])
        theme.apply(app)
        c = W.OpsCanvas(200, 200)
        assert hasattr(c, "set_background")
        c.set_background("", None)      # 空路径 = 关掉,不该炸
        c.resize(200, 200)
        assert not c.grab().isNull()

    def test_background_is_painted_under_the_ops(self):
        w = (ROOT / "astro_smb_qt" / "widgets.py").read_text(encoding="utf-8")
        at = w.index("    def paint(self, p: QPainter, w: float, h: float) -> None:\n"
                     "        if self._bg is not None")
        body = w[at:at + 900]
        bg = body.index("drawPixmap")
        ops = body.index("for op in self._ops")
        assert bg < ops, "底图画在图元之后 —— 会把地平圈和目标点整个盖掉"


class TestDownloadNeedsConsent:
    """8 MB 不能不问就下。"""

    def test_asks_before_downloading(self):
        at = SRC.index("def _set_sky_bg")
        body = SRC[at:SRC.index("\n    def ", at + 10)]
        assert "survey_available()" in body, "没判断有没有下过就直接用"
        # **要的是那个控制流形状,不是"文本里出现过 confirm"。**
        # 第一版断言 `"self.confirm(" in body`,而把守卫改成
        # `if False and self.confirm(...)` 之后那个字符串还在,变异照样绿。
        guard = body.index("if not self.confirm(")
        dl = body.index("self._download_survey()")
        assert guard < dl, "下载没被确认守卫挡在后面"

    def test_declining_unchecks_the_box(self):
        at = SRC.index("def _set_sky_bg")
        body = SRC[at:SRC.index("\n    def ", at + 10)]
        assert "setChecked(False)" in body, (
            "用户点了取消,开关却还勾着 —— 下次点它反而变成关闭")

    def test_confirm_defaults_to_cancel(self):
        base = (ROOT / "astro_smb_qt" / "pages" / "base.py").read_text(
            encoding="utf-8")
        at = base.index("def confirm")
        body = base[at:base.index("\n    def ", at + 10)]
        # 按钮改成了自建的中文按钮(老 UI 是「删除 / 取消」而不是英文 Yes/No),
        # 所以默认项现在指向那个 cancel 按钮对象。
        assert "setDefaultButton(cancel)" in body, (
            "确认框默认按钮不是「取消」—— 一路回车会把下载/删除点掉")
        assert "QMessageBox.RejectRole" in body, "取消按钮没登记成 Reject 角色"


class TestCreditIsShown:
    """CC BY 4.0 **要求**署名,那不是装饰。"""

    def test_credit_label_exists(self):
        assert "sky_credit" in SRC

    def test_credit_uses_the_shared_constant(self):
        at = SRC.index("def _refresh_sky_bg")
        body = SRC[at:SRC.index("\n    def ", at + 10)]
        assert "skymap.SURVEY_CREDIT" in body, (
            "署名自己拼了一份 —— 改一处就会漏另一处")

    def test_credit_hidden_when_off(self):
        at = SRC.index("def _set_sky_bg")
        body = SRC[at:SRC.index("\n    def ", at + 10)]
        assert "setVisible(False)" in body


class TestReprojectionIsOffTheGuiThread:
    """一次重投影约 0.36s。跑在 GUI 线程上就是可见的卡顿。"""

    def test_uses_the_page_executor(self):
        at = SRC.index("def _refresh_sky_bg")
        body = SRC[at:SRC.index("\n    def ", at + 10)]
        assert "self.bg.run(" in body, "重投影跑在 GUI 线程上了"

    def test_download_too(self):
        at = SRC.index("def _download_survey")
        body = SRC[at:SRC.index("\n    def ", at + 10)]
        assert "self.bg.run(" in body, "8 MB 下载跑在 GUI 线程上了"
