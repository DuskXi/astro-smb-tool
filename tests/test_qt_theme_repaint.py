"""切配色之后,**显示列表必须重新生成**(清单 10.2)。

`OpsCanvas` 吃的是一串 ``{"fill": "#4FBF87"}`` —— 颜色在**生成 op 的那一刻**
就烤进去了。切档时外壳会 `theme.apply()` 刷 QSS、`restyle()` 重抛样式,
但那两样管不到显示列表:天球、甘特、treemap 会原封不动留在上一档的配色里。

**不报错、不崩溃**,只是颜色不对。红光档尤其致命 —— 那一档存在的唯一理由
就是不破坏暗适应,而一张深绿的甘特条直接把这个理由作废。

这一条**只能行为验证**。查源码没用:`_on_theme` 里写着 `self.update()`
看起来就很像"已经重画了",而 `update()` 只是把同一串旧颜色再画一遍。
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")


@pytest.fixture(scope="module")
def qt_app():
    from PySide6.QtWidgets import QApplication

    from astro_smb_qt import theme

    inst = QApplication.instance() or QApplication([])
    theme.apply(inst)
    return inst


class TestTheStaleness:
    """先把"会变陈"这件事本身钉死 —— 否则下面几条可能在测一个不存在的问题。"""

    def test_ops_bake_the_colour_in(self, qt_app):
        from astro_smb_qt import theme, widgets as W

        before = theme.C.mode
        try:
            theme.set_mode(theme.MODE_NORMAL)
            canvas = W.OpsCanvas(120, 60)
            canvas.set_ops([{"op": "rect", "x": 0, "y": 0, "w": 8, "h": 8,
                             "fill": theme.C.OK}])
            baked = canvas._ops[0]["fill"]
            theme.set_mode(theme.MODE_RED)
            assert theme.C.OK != baked, "两档的 OK 色一样 —— 这条没在测东西"
            assert canvas._ops[0]["fill"] == baked, (
                "显示列表会自己跟着主题变?那这一整个文件都可以删了")
        finally:
            theme.set_mode(before)


class TestContract:

    def test_base_page_has_the_hook(self):
        from astro_smb_qt.pages.base import Page

        assert hasattr(Page, "on_theme"), "Page 没有 on_theme 契约"

    def test_theme_signal_calls_it(self, qt_app):
        """信号→钩子这条线要真的通。"""
        from astro_smb_qt.pages.base import Page

        called = []

        class _P(Page):
            def on_theme(self) -> None:
                called.append(1)

        class _Shell:
            pass

        page = _P.__new__(_P)
        from PySide6.QtWidgets import QWidget
        QWidget.__init__(page)
        page._on_theme("red")
        assert called, "切档没有调用 on_theme"

    def test_a_failing_hook_does_not_break_switching(self, qt_app):
        """某一页重画失败,不该把整个切档动作带崩 ——
        那会表现成"点了配色按钮没反应"。"""
        from PySide6.QtWidgets import QWidget

        from astro_smb_qt.pages.base import Page

        class _P(Page):
            def on_theme(self) -> None:
                raise RuntimeError("故意炸")

        page = _P.__new__(_P)
        QWidget.__init__(page)
        page._on_theme("red")          # 不抛就算过


class TestPagesReRender:
    """三页各验一次。这三页是仅有的显示列表消费者。"""

    def _pages_that_bake_colours(self):
        """**凡是在页面代码里取过主题色的页**,都要实现 `on_theme`。

        上一版这里只查 `OpsCanvas(` —— 范围太窄,**漏掉了两整页**:
        导星页把 `theme.tone_color()` 的结果烤进 `DataTable` 的 cell 字典,
        3D 天球页烤进自己的绘制状态,两页都没有 `OpsCanvas`。
        独立验收实测:运行时切到红光档,导星页的段列表**还是绿色字** ——
        而红光档存在的唯一理由就是不破坏暗适应。

        判据放宽成"取过 `theme.C.` / `theme.Q.` / `theme.tone_color(`":
        在 `paintEvent` 里现取的页面其实不需要重生成,但多重画一次几乎
        没有代价,而漏掉一页的代价是一整页颜色不对、且没人会发现。
        """
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "astro_smb_qt" / "pages"
        out = []
        for p in sorted(root.glob("*.py")):
            if p.stem == "base":
                continue
            src = p.read_text(encoding="utf-8")
            if ("theme.C." in src or "theme.Q." in src
                    or "theme.tone_color(" in src or "OpsCanvas(" in src):
                out.append(p)
        return out

    def test_every_colour_baking_page_implements_it(self):
        """**按"谁取过主题色"反查**,不是我记得哪几页。

        新加一页而忘了 `on_theme`,这条会立刻红 —— 而界面上的表现
        只是"那一页切档之后颜色不对",没人会注意到。
        """
        import importlib

        pages = self._pages_that_bake_colours()
        assert len(pages) >= 4, f"只扫到 {len(pages)} 页 —— 判据可能失效了"
        missing = []
        for path in pages:
            mod = importlib.import_module(f"astro_smb_qt.pages.{path.stem}")
            for name in dir(mod):
                obj = getattr(mod, name)
                if (isinstance(obj, type) and name.endswith("Page")
                        and obj.__module__ == mod.__name__):
                    if "on_theme" not in vars(obj):
                        missing.append(f"{path.stem}.{name}")
        assert not missing, (
            f"这些页取了主题色却没实现 on_theme: {missing} —— "
            f"切档之后它们会留在上一档的颜色里(红光档尤其致命)")

    def test_records_rebuilds_its_canvases(self, qt_app):
        from astro_smb_qt.pages import records as R

        page = R.RecordsPage.__new__(R.RecordsPage)
        page.model = {"spans": [{"f0": 0.0, "f1": 0.5, "fill": (1, 2, 3),
                                 "key": "k"}]}
        hits = []
        page._render = lambda: hits.append(1)
        R.RecordsPage.on_theme(page)
        assert hits, "拍摄记录页切档没有重生成显示列表"

    def test_records_skips_when_there_is_no_data(self, qt_app):
        """还没读日志时不许硬跑 `_render` —— 那会拿 None 去算,整页报错。"""
        from astro_smb_qt.pages import records as R

        page = R.RecordsPage.__new__(R.RecordsPage)
        page.model = {}
        page._render = lambda: (_ for _ in ()).throw(AssertionError("不该跑"))
        R.RecordsPage.on_theme(page)

    def test_space_rebuilds_the_treemap(self, qt_app):
        from astro_smb_qt.pages import space as S

        page = S.SpacePage.__new__(S.SpacePage)
        page.root = object()
        hits = []
        page._render = lambda: hits.append(1)
        S.SpacePage.on_theme(page)
        assert hits, "空间分析页切档没有重画 treemap"

    def test_space_skips_before_the_scan(self, qt_app):
        from astro_smb_qt.pages import space as S

        page = S.SpacePage.__new__(S.SpacePage)
        page.root = None
        page._render = lambda: (_ for _ in ()).throw(AssertionError("不该跑"))
        S.SpacePage.on_theme(page)

    def test_browser_repaints_the_radar_without_refetching(self, qt_app):
        """详情重铺一遍就够,**不要再发一轮预览请求** —— 那是白跑的网络 I/O。"""
        from astro_smb_qt.pages import browser as B

        page = B.BrowserPage.__new__(B.BrowserPage)
        page._last_detail = {"name": "x"}
        got = []
        page._render_detail = lambda m: got.append(m)
        page._on_pick = lambda *_a: (_ for _ in ()).throw(
            AssertionError("切个配色不该重新拉预览"))
        B.BrowserPage.on_theme(page)
        assert got == [{"name": "x"}]

    def test_browser_skips_when_nothing_is_selected(self, qt_app):
        from astro_smb_qt.pages import browser as B

        page = B.BrowserPage.__new__(B.BrowserPage)
        page._last_detail = {}
        page._render_detail = lambda m: (_ for _ in ()).throw(
            AssertionError("没选中却铺了详情"))
        B.BrowserPage.on_theme(page)


class TestEndToEnd:
    """整页跑一遍:切档之后画布里的颜色**真的**变了。"""

    def test_a_canvas_actually_changes_colour(self, qt_app):
        from astro_smb_qt import theme, widgets as W
        from astro_smb_qt.pages.base import Page

        before = theme.C.mode

        class _P(Page):
            def __init__(self):
                from PySide6.QtWidgets import QWidget
                QWidget.__init__(self)
                self.canvas = W.OpsCanvas(120, 60, self)
                self._paint()

            def _paint(self) -> None:
                self.canvas.set_ops([{"op": "rect", "x": 0, "y": 0,
                                      "w": 8, "h": 8, "fill": theme.C.OK}])

            def on_theme(self) -> None:
                self._paint()

        try:
            theme.set_mode(theme.MODE_NORMAL)
            page = _P()
            first = page.canvas._ops[0]["fill"]
            theme.set_mode(theme.MODE_RED)
            page._on_theme(theme.MODE_RED)
            assert page.canvas._ops[0]["fill"] != first, (
                "切到红光档之后画布还是常规档的颜色")
            assert page.canvas._ops[0]["fill"] == theme.C.OK
        finally:
            theme.set_mode(before)
