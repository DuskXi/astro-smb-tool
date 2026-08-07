"""空间分析页的交互 —— 用户报"有图,但是没有交互"。

图和联动其实都在,**缺的是反馈**:点一个块只是把右边树里那一行选上,
treemap 本身毫无变化,于是看起来"点了没反应"。另外老 UI 的 treemap 还有
双击下钻和悬停提示,这边都没有。

一条几何铁律单独钉死:**命中反查要倒着找**。嵌套 treemap 里子块画在父块
之上,正着找永远命中最外层那个大块 —— 表现为"点哪个子目录都选中根目录"。
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from astro_smb_qt import theme, widgets as W  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    inst = QApplication.instance() or QApplication([])
    theme.apply(inst)
    return inst


def _canvas(qt_app):
    c = W.OpsCanvas(200, 200)
    # 父块整张,子块在里面 —— 顺序就是"后画的在上面"
    c.set_ops([], [[0, 0, 200, 200, "root"],
                   [10, 10, 90, 90, "root/a"],
                   [100, 10, 190, 90, "root/b"]])
    return c


class TestHitLookupIsTopmostFirst:

    def test_child_wins_over_parent(self, qt_app):
        c = _canvas(qt_app)
        assert c.key_at(50, 50) == "root/a", (
            "命中反查是正着找的 —— 嵌套 treemap 里点哪个子块都会选中根块")

    def test_other_child(self, qt_app):
        assert _canvas(qt_app).key_at(150, 50) == "root/b"

    def test_parent_where_no_child(self, qt_app):
        assert _canvas(qt_app).key_at(50, 150) == "root"

    def test_outside_is_empty(self, qt_app):
        assert _canvas(qt_app).key_at(-5, -5) == ""


class TestCanvasEmitsTheThreeInteractions:

    def test_signals_exist(self, qt_app):
        c = _canvas(qt_app)
        for name in ("hit", "hit_activated", "hit_hovered"):
            assert hasattr(c, name), f"画布没有 {name} 信号"

    def test_hover_only_fires_on_change(self, qt_app):
        """每个鼠标移动事件都发一次的话,上层每帧重画一整张 treemap。"""
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QMouseEvent

        c = _canvas(qt_app)
        seen: list[str] = []
        c.hit_hovered.connect(seen.append)

        def move(x, y):
            ev = QMouseEvent(QMouseEvent.Type.MouseMove, QPointF(x, y),
                             Qt.NoButton, Qt.NoButton, Qt.NoModifier)
            c.mouseMoveEvent(ev)

        move(50, 50)
        move(55, 55)          # 还在同一个块里 —— 不该再发
        move(150, 50)
        assert seen == ["root/a", "root/b"], seen

    def test_leaving_clears(self, qt_app):
        from PySide6.QtCore import QEvent, QPointF, Qt
        from PySide6.QtGui import QMouseEvent

        c = _canvas(qt_app)
        seen: list[str] = []
        c.mouseMoveEvent(QMouseEvent(QMouseEvent.Type.MouseMove,
                                     QPointF(50, 50), Qt.NoButton,
                                     Qt.NoButton, Qt.NoModifier))
        c.hit_hovered.connect(seen.append)
        c.leaveEvent(QEvent(QEvent.Type.Leave))
        assert seen == [""], "指针离开时没清掉悬停高亮"

    def test_mouse_tracking_is_on(self, qt_app):
        """不开 tracking 的话只有按住鼠标才会收到 move —— 悬停等于没做。"""
        assert _canvas(qt_app).hasMouseTracking()


class TestSpacePageDrawsFeedback:
    """选中/悬停要进显示列表,否则"点了没反应"。"""

    def _page_src(self) -> str:
        from pathlib import Path

        return (Path(__file__).resolve().parents[1] / "astro_smb_qt" / "pages"
                / "space.py").read_text(encoding="utf-8")

    def test_marks_are_appended_after_the_blocks(self):
        src = self._page_src()
        assert "ops + self._marks()" in src, (
            "高亮没有画在显示列表最后 —— 会被后面的块盖掉")

    def test_selection_and_hover_both_marked(self):
        src = self._page_src()
        at = src.index("def _marks")
        body = src[at:src.index("\n    def ", at + 10)]
        assert "self._hover" in body and "self.selected" in body

    def test_double_click_drills(self):
        src = self._page_src()
        assert "hit_activated.connect(self._drill)" in src, (
            "treemap 上双击不能下钻 —— 老 UI 可以")

    def test_hover_is_wired(self):
        src = self._page_src()
        assert "hit_hovered.connect(self._on_hover)" in src

    def test_click_repaints(self):
        """选中之后不重画,高亮框永远不出现。"""
        src = self._page_src()
        at = src.index("def _pick_block")
        body = src[at:src.index("\n    def ", at + 10)]
        assert "_repaint_marks" in body, "点了不重画,高亮框出不来"
