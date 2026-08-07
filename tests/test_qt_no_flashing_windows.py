"""无父控件不许 `setVisible(True)` —— 那会在屏幕上闪出一堆小窗。

用户报的:"现在有部分操作会迅速弹一堆在一个位置的小框框又秒级消失",
附图是几个带最小化/关闭按钮的小窗框。

**Qt 里无父控件就是顶层窗口。** 构造期做
`self.setVisible(bool(text))` 的件,在被 `addWidget` 收进布局之前是没有父的,
那一下 `show()` 就让它以一个真窗口的形态闪一帧。详情面板一次要建七八个
徽章/胶囊,于是闪成一片。

加进布局之后控件本来就可见,压根不需要显式 show;真正需要的只有"隐藏",
而 `setVisible(False)` 对无父控件是安全的。`W.show_if` 就是这条规则。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

ROOT = Path(__file__).resolve().parents[1]
QT = ROOT / "astro_smb_qt"


def _sources():
    for p in sorted(QT.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        yield p


class TestNoUnsafeShow:

    def test_nothing_shows_a_possibly_parentless_widget(self):
        """整个包里的"可能显示"一律走 `show_if`。

        **判据走 `ast`,不走文本。** 按子串查会匹配到解释这条规则的文档字符串
        与注释本身 —— 这一轮已经栽过七次同一种。语法树里只有真正的调用。

        `setVisible(False)` 对无父控件是安全的,所以只放过写死 `False` 的。
        唯一的例外是 `show_if` 自己(它就是那个守卫)。
        """
        import ast

        offenders = []
        for p in _sources():
            tree = ast.parse(p.read_text(encoding="utf-8"))
            inside_guard = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name == "show_if":
                    inside_guard.update(
                        id(n) for n in ast.walk(node) if isinstance(n, ast.Call))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "setVisible"
                        and node.args):
                    continue
                if id(node) in inside_guard:
                    continue
                arg = node.args[0]
                if isinstance(arg, ast.Constant) and arg.value is False:
                    continue
                offenders.append(f"{p.name}:{node.lineno}")
        assert not offenders, (
            f"这些地方直接 setVisible 了,可能对无父控件 show 出一个小窗:"
            f"{offenders} —— 改用 `W.show_if`")

    def test_show_if_exists_and_guards(self):
        from astro_smb_qt import widgets as W

        assert callable(W.show_if)
        src = (QT / "widgets.py").read_text(encoding="utf-8")
        at = src.index("def show_if")
        body = src[at:src.index("\n\ndef ", at)]
        assert "w.parent() is not None" in body, (
            "`show_if` 没判父控件 —— 那它和直接 setVisible 没区别")


class TestBehaviour:

    @pytest.fixture(scope="class")
    def qt_app(self):
        from PySide6.QtWidgets import QApplication

        from astro_smb_qt import theme

        inst = QApplication.instance() or QApplication([])
        theme.apply(inst)
        return inst

    def test_parentless_chip_is_not_shown(self, qt_app):
        from astro_smb_qt import widgets as W

        chip = W.StatusChip("亮场", "good")
        assert chip.parent() is None
        assert not chip.isVisible(), (
            "无父的胶囊被 show 了 —— 它会以一个带标题栏的小窗闪一帧")

    def test_it_becomes_visible_once_laid_out(self, qt_app):
        from PySide6.QtWidgets import QWidget

        from astro_smb_qt import widgets as W

        host = QWidget()
        row = W.hbox(host)
        chip = W.StatusChip("亮场", "good")
        row.addWidget(chip)
        host.show()
        assert chip.isVisible(), "收进布局之后反而不见了"
        host.hide()

    def test_empty_chip_stays_hidden(self, qt_app):
        from PySide6.QtWidgets import QWidget

        from astro_smb_qt import widgets as W

        host = QWidget()
        row = W.hbox(host)
        chip = W.StatusChip("", None)
        row.addWidget(chip)
        host.show()
        assert not chip.isVisible(), "空胶囊会留一个没有内容的小圆角块"
        host.hide()

    def test_set_toggles_after_reparenting(self, qt_app):
        from PySide6.QtWidgets import QWidget

        from astro_smb_qt import widgets as W

        host = QWidget()
        row = W.hbox(host)
        chip = W.StatusChip("", None)
        row.addWidget(chip)
        host.show()
        chip.set("3/3", "ok")
        assert chip.isVisible(), "有父之后 set() 应当能把它显出来"
        chip.set("", None)
        assert not chip.isVisible()
        host.hide()

    def test_metric_row_note_does_not_flash(self, qt_app):
        from astro_smb_qt import widgets as W

        row = W.MetricRow("键", "值", note="注")
        assert row.parent() is None
        assert not row.isVisible()
