"""独立验收在浏览页抓到的缺陷 —— 一条一个门禁。

验收报告(2026-08-02,`docs/qt-final.md` §1)判定浏览页**不通过**。这一份钉住
已修的那几条,免得再漂回去。

最严重的一条值得单说:**冷启动时方位角用的是 `site.json` 的兜底经度 120°**,
而真值是日志反推的 121.4°。界面上是 180°(正确值 182°)—— 不报错、不崩溃,
就是一个**看起来很正常的错数字**。切一趟拍摄记录页再回来它又变对了,
因为那一页会去读日志。这个目标正好在中天附近,所以高度角在一位小数上看不
出差别;换一个远离子午线的目标,同样的经度误差会一并挪动高度角,进而影响
气量和 good/warn/bad 语义色。
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BROWSER = (ROOT / "astro_smb_qt" / "pages" / "browser.py").read_text(
    encoding="utf-8")
SHELL = (ROOT / "astro_smb_qt" / "shell.py").read_text(encoding="utf-8")


def _body(src: str, name: str) -> str:
    at = src.index(f"def {name}")
    end = src.find("\n    def ", at + 10)
    return src[at:end if end > 0 else len(src)]


class TestLongitudeComesFromTheLogs:
    """经度不是可选项:方位角与迷你雷达都用它。"""

    def test_shell_warms_the_logs_on_connect(self):
        # **要的是连接那条路上真的调了它**,不是"文件里有这个方法"。
        # 第一版断言 `"_warm_logs" in SHELL` —— 把调用点删掉之后定义还在,
        # 变异照样绿。(这已经是这一轮第四次"匹配到定义而不是调用点"。)
        connect = _body(SHELL, "_on_connected")
        assert "self._warm_logs()" in connect, (
            "连上之后没有预热日志 —— 浏览页会一直用兜底经度算方位角")
        body = _body(SHELL, "_warm_logs")
        assert "logstore.refresh" in body
        assert "logs_ready.emit" in body, "读完没有广播,已渲染的详情不会重算"

    def test_warm_up_is_off_the_gui_thread(self):
        body = _body(SHELL, "_warm_logs")
        assert ".run(" in body, "日志下载/解析跑在 GUI 线程上了"

    def test_warm_up_uses_its_own_executor(self):
        """页面各自的 `Bg` 会因换页/换目录 bump 世代而作废这一趟预热。"""
        body = _body(SHELL, "_warm_logs")
        assert "_logbg" in body, "预热用了页面的执行器,换个目录就被作废"

    def test_browser_listens(self):
        assert "logs_ready.connect" in BROWSER, (
            "浏览页不接 logs_ready —— 已经渲染出来的详情会一直显示错数字")

    def test_browser_rerenders_the_current_file(self):
        body = _body(BROWSER, "_on_logs_ready")
        assert "_on_pick" in body, "接了信号却不重算详情"

    def test_detail_prefers_the_estimate(self):
        body = _body(BROWSER, "_detail_model")
        assert "lon_estimate" in body, (
            "详情没有优先用日志反推的经度 —— 纬度只能靠用户设,而经度可以由"
            "PHD2 段头时角 + 同时刻目标 RA 反推,那个值比用户随手填的准")


class TestNoStaleStateAfterNavigating:
    """换目录后「下载所选(1)」还亮着,而那个键在新目录里不存在 —— 点了没反应。"""

    def test_entries_reset_selection(self):
        body = _body(BROWSER, "_apply_entries")
        for token in ("self.selected = \"\"", "clearSelection()",
                      "_on_checked([])"):
            assert token in body, f"换目录没清 {token} —— 计数会是脏的"

    def test_entries_reset_the_detail(self):
        body = _body(BROWSER, "_apply_entries")
        assert "_render_detail(None)" in body, (
            "换目录后详情面板还停在上一个目录那个文件上,像是进错了目录")

    def test_reset_happens_before_rendering(self):
        body = _body(BROWSER, "_apply_entries")
        assert body.index("_render_detail(None)") < body.index("_render_rows()")


class TestClickingTheCurrentShareStillNavigates:
    """`currentTextChanged` 在"点的就是当前项"时不发 —— 点了毫无反应。"""

    def test_item_clicked_is_wired(self):
        assert "share_list.itemClicked.connect" in BROWSER, (
            "点已选中的共享不会回到共享根(老 UI 会)")


class TestRadarLabelsAreNotClipped:
    """「东」被左边界静默切成两个像素 —— 记录页修过同一个坑。"""

    def test_margin_is_declared(self):
        assert "RADAR_MARGIN" in BROWSER

    def test_frame_and_points_share_it(self):
        body = _body(BROWSER, "_radar")
        assert body.count("margin=RADAR_MARGIN") == 2, (
            "圈和点用的 margin 不一致 —— 点会偏出圈")

    def test_margin_is_big_enough(self):
        import re

        m = re.search(r"RADAR_MARGIN = ([\d.]+)", BROWSER)
        assert m and float(m.group(1)) >= 16.0, (
            "margin 太小,方位标注会被边界切掉(记录页那条注释记的就是这个)")


@pytest.mark.skipif(not (ROOT / ".tmp" / "device" / "EMMC Images").is_dir(),
                    reason="没有离线镜像")
class TestAzimuthMatchesTheOldUi:
    """端到端:同一张 light 帧,方位角必须是日志经度算出来的那个值。"""

    def test_estimate_moves_the_azimuth(self):
        """先证明这条**验得到** —— 两个经度确实给出不同的方位角。

        不先证一下的话,下面那条可能只是在一份"怎么算都一样"的数据上空转。
        """
        from astro_smb.astro import altaz

        # publish-scan: ok(编造的目标坐标 + 北纬 30,不是任何人的观测地)
        ra, dec, lat = 246.7125, -24.515556, 30.0
        ts = 1784981715.875817
        a120 = altaz(ra, dec, lat, 120.0, ts)[1]
        a121 = altaz(ra, dec, lat, 121.4, ts)[1]
        assert round(a120) != round(a121), (
            f"两个经度给出同一个方位角({a120:.2f} vs {a121:.2f}),这条验不到")

    def test_detail_uses_the_estimate(self):
        """`site_latlon(site, lon_estimate)` 必须让反推值**盖过** site.json。"""
        from astro_smb_app.views import browser as bv

        got = bv.site_latlon({"lat": 30.0, "lon": 120.0}, 121.4)
        assert got == (30.0, 121.4), got

    def test_connecting_really_fires_logs_ready(self):
        """**行为测试,不是查源码。**

        上面那几条是源码检查,它们区分不了"调用了"和"写在一个永远不执行的
        分支里"(`if False: self._warm_logs()` 就能骗过去)。这条真起一个
        外壳、真连镜像、真等信号 —— 慢几秒,但这是全页最贵的那个缺陷。
        """
        pytest.importorskip("PySide6")
        from PySide6.QtCore import QEventLoop, QTimer
        from PySide6.QtWidgets import QApplication

        from astro_smb_qt import theme
        from astro_smb_qt.shell import Shell

        app = QApplication.instance() or QApplication([])
        theme.apply(app)
        host = str(ROOT / ".tmp" / "device" / "EMMC Images")
        sh = Shell(host=host, page="browse")
        try:
            loop = QEventLoop()
            fired: list[int] = []
            sh.logs_ready.connect(lambda: (fired.append(1), loop.quit()))
            QTimer.singleShot(25000, loop.quit)      # 保险,不要挂死
            loop.exec()
            assert fired, "连上之后 logs_ready 一直没来 —— 日志没被预热"
            lon = getattr(sh.logstore.data, "lon_estimate", None)
            assert lon is not None, "日志读完了却没反推出经度"
            assert 100.0 < float(lon) < 140.0, f"反推经度不合理: {lon}"
        finally:
            sh.close()
