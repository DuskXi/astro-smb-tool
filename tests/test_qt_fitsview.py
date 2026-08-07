"""影像查看页 —— 用户列的第 10 条(「3D天球/影像查看没做」)的前一半。

这一页的形状由**一条设备事实**决定:一张 light 帧 49.77 MB、SMB 单流约
6 MiB/s,打开一张要等十来秒。所以进度条必须是**确定式并写出 MB 数** ——
只给一个转圈,用户分不清是在下载、卡住了、还是快好了。

另外钉住一条抽取纪律:判读那几支帮手原来住在 `astro_smb_app/ui/app.py`
(Uno 前端的私有模块,跟协议/子进程缠在一起,别的前端 import 不了)。
第三套前端要用它们,只能重写一份 —— 而重写迟早在某次"顺手调阈值"时分叉,
分叉之后两页给出不同判读、谁都不知道哪个对。已下沉到 `views.fitsview`。
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "astro_smb_qt" / "pages" / "fitsview.py").read_text(
    encoding="utf-8")


def _body(src: str, name: str) -> str:
    at = src.index(f"def {name}")
    end = src.find("\n    def ", at + 10)
    return src[at:end if end > 0 else len(src)]


class TestHelpersLiveInTheSharedLayer:
    """三套前端一份判读。"""

    @pytest.mark.parametrize("name", [
        "fits_astro", "fits_structure", "solve_text", "fits_badges", "fmt_exp",
    ])
    def test_public_in_views(self, name: str):
        from astro_smb_app.views import fitsview as fv

        assert callable(getattr(fv, name, None)), (
            f"{name} 不在共享层 —— 第三套前端只能重写一份,判读口径会分叉")

    def test_the_page_does_not_reimplement_the_judgement(self):
        """页面只负责摆,不许把判读再写一遍。

        (原来这条盯的是 Uno 那边的转发 shim;Uno 已删,而性质对 Qt 一样成立。)

        **查公式,不查"Pickering"这个词。** 页面的模块文档里正大光明写着
        "气量用的是 Pickering (2002)……这一页只负责摆" —— 查词会把**解释规则的
        那句话本身**判成违规(第一版就是这么红的)。查系数才是查实现。
        """
        import ast

        path = ROOT / "astro_smb_qt" / "pages" / "fitsview.py"
        src = path.read_text(encoding="utf-8")
        assert "fv.fits_astro(" in src, "页面没走共享判读"

        # Pickering (2002) 的系数:1/sin(h + 244/(165 + 47·h^1.1))
        tree = ast.parse(src)
        nums = {n.value for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, (int, float))
                and not isinstance(n.value, bool)}
        copied = {244.0, 165.0, 47.0} & nums
        assert not copied, f"气量公式的系数出现在页面里,判读被复制过去了: {copied}"

    def test_the_page_uses_the_shared_ones(self):
        for name in ("fv.fits_astro(", "fv.fits_structure(", "fv.solve_rows("):
            assert name in SRC, f"页面没走共享层的 {name}"


class TestProgressIsDeterminate:
    """一张 50MB 的图等十来秒 —— 只给转圈等于什么都没说。"""

    def test_progress_reports_bytes(self):
        body = _body(SRC, "_on_progress")
        assert "MB" in body, "进度条不写 MB —— 用户不知道还要等多久"
        assert "setRange(0, 1000)" in body, "不是确定式进度条"

    def test_cumulative_not_delta(self):
        """`progress(已完成, 总量)` 是累计值。当增量累加会冲到 200%。"""
        body = _body(SRC, "reload")
        assert "int(done), int(total)" in body, (
            "进度回调的两个值被改过 —— 那两个 int 长得一模一样,"
            "当成增量累加不报错,只是进度条冲到 200% 然后卡住")

    def test_decode_phase_switches_to_indeterminate(self):
        """解码没有百分比,硬把进度条停在 100% 会让人以为卡住了。"""
        body = _body(SRC, "_on_progress")
        assert "setRange(0, 0)" in body

    def test_zero_total_does_not_divide(self):
        body = _body(SRC, "_on_progress")
        assert "total <= 0" in body, "总量为 0 时会除零"


class TestEverythingHeavyIsOffTheGuiThread:
    """6248×4176 的解码 + 百分位拉伸在 GUI 线程上就是几秒白屏。"""

    def test_load_runs_in_the_executor(self):
        body = _body(SRC, "reload")
        assert "self.bg.run(" in body
        assert "load_linear" in body and "def work(report)" in body, (
            "解码没在工作线程里")

    def test_solve_runs_in_the_executor(self):
        body = _body(SRC, "_solve")
        assert "self.bg.run(" in body

    def test_generation_guard(self):
        body = _body(SRC, "reload")
        assert "self.bg.bump()" in body, (
            "换一张图时上一张的结果没被作废 —— 会看到上一张的判读")


class TestSidepanelHasEverything:
    """老 UI 右栏四块:直方图 / 判读卡 / 影像结构 / 原始头。"""

    @pytest.mark.parametrize("token", [
        "直方图", "影像结构", "板解算", "完整 FITS 头",
    ])
    def test_block_present(self, token: str):
        body = _body(SRC, "_render_side")
        assert token in body, f"右栏少了「{token}」"

    def test_raw_header_is_a_dialog(self):
        """一张 light 帧七十多张卡片,摊在面板里会把判读整个顶出可视区。"""
        assert "TextDialog" in _body(SRC, "_show_header")

    def test_histogram_overlays_channels(self):
        """多通道**半透明叠画** —— 合成单条会让重叠处不再叠色,
        而叠色正是这张图的读法(哪个通道饱和了)。

        件住在 `widgets.py`:它要构造 QColor 调透明度,而页面一律不许造颜色
        (自己造的会绕过红光映射;`test_page_does_not_construct_colors` 盯着)。
        """
        w = (ROOT / "astro_smb_qt" / "widgets.py").read_text(encoding="utf-8")
        at = w.index("class MultiHistogram")
        body = w[at:w.index("\n\nclass ", at + 10)]
        assert "setAlpha" in body, "通道不透明,叠不出颜色"
        assert "self._hist[:3]" in body, "只画了一个通道"

    def test_page_uses_the_shared_widget(self):
        assert "W.MultiHistogram(" in SRC, "页面自己画了一份直方图"


class TestModeSwitch:
    def test_three_modes(self):
        # 这一句要 import 页面模块 —— 它顶层就 `from PySide6 import …`。
        # 本文件其余断言只读源码文本,所以没有文件级的守卫;缺了这一句,
        # `ASTRO_SMB_NO_QT=1` 那条路上它是 **error 不是 skip**。
        pytest.importorskip("PySide6")
        from astro_smb_qt.pages import fitsview as page
        from astro_smb_app.views import fitsview as fv

        assert [k for k, _t in page.MODES] == list(fv._MODES), (
            "拉伸模式与共享层对不上 —— 下拉选第二项会拉出第三种")

    def test_switching_reloads(self):
        body = _body(SRC, "_set_mode")
        assert "self.reload()" in body, "换了模式不重新拉伸 —— 点了没反应"

    def test_same_mode_does_not_reload(self):
        body = _body(SRC, "_set_mode")
        assert "if idx == self.mode" in body, (
            "选同一项也重下 50MB")


class TestRegistered:
    def test_page_is_wired_up(self):
        pytest.importorskip("PySide6")
        from astro_smb_qt.pages import PAGE_CLASSES
        from astro_smb_qt.pages.fitsview import FitsViewPage

        assert PAGE_CLASSES["fits"] is FitsViewPage, (
            "还指向占位页 —— 写完了忘了注册 = 永远打不开的死模块")

    def test_open_selects_the_page(self):
        body = _body(SRC, "open")
        assert 'select_page("fits")' in body, (
            "从浏览页跳过来不切页,用户以为点了没反应")
