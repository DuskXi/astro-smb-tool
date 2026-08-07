"""浏览页:选中语义与详情面板 —— 用户逐条报上来的那几个。

四条全是**静默**的:不崩溃、不报错,只是少一块、点了没反应、或者被截掉。

* 勾选模式打开后界面毫无变化(Qt 的 MultiSelection 不画任何东西)
* 不开勾选模式时 ctrl / shift 全失效(选择模式给成了 SingleSelection)
* 高度角那条 0°–90° 量条被丢掉(共享层元组第 6 项没取)
* 文件名右边被截掉(QLabel 只在空格处断行,而 ASIAIR 的名字只有一个空格)
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QAbstractItemView as AIV  # noqa: E402

from astro_smb_qt import theme, widgets as W  # noqa: E402
from astro_smb_qt.pages import browser as bp  # noqa: E402


@pytest.fixture(scope="module")
def qt_app():
    from PySide6.QtWidgets import QApplication

    inst = QApplication.instance() or QApplication([])
    theme.apply(inst)
    return inst


class TestSelectionModes:
    """老 UI 是 `Multiple`(带框)/ `Extended`(ctrl+shift)两档。"""

    def test_multi_table_defaults_to_extended(self, qt_app):
        t = W.DataTable(("*",), multi=True)
        assert t.selectionMode() == AIV.ExtendedSelection, (
            "支持多选的表默认必须是 Extended —— 否则启动后 ctrl/shift 不管用,"
            "要先把勾选模式开了再关才恢复")

    def test_check_mode_on_is_multi_selection(self, qt_app):
        t = W.DataTable(("*",), multi=True)
        t.set_check_mode(True)
        assert t.selectionMode() == AIV.MultiSelection, (
            "勾选模式下应当单击即勾选,不用按住 ctrl")

    def test_check_mode_off_keeps_ctrl_shift(self, qt_app):
        t = W.DataTable(("*",), multi=True)
        t.set_check_mode(True)
        t.set_check_mode(False)
        assert t.selectionMode() == AIV.ExtendedSelection, (
            "关掉勾选模式**不等于**只能选一个 —— 用户报的就是这条")

    def test_check_mode_flag_drives_the_box(self, qt_app):
        """框由 `_CellDelegate` 按这个标志画;标志不动的话框永远不出现。"""
        t = W.DataTable(("*",), multi=True)
        assert t._check_mode is False
        t.set_check_mode(True)
        assert t._check_mode is True

    def test_checked_keys_reported_in_both_multi_modes(self, qt_app):
        """「下载所选(n)」的计数在 ctrl 多选下也要动,不能只在勾选模式里动。"""
        seen: list[list[str]] = []
        t = W.DataTable(("*",), multi=True)
        t.keys_checked.connect(lambda ks: seen.append(list(ks)))
        t.set_rows([{"key": "a", "cells": [{"text": "1"}]},
                    {"key": "b", "cells": [{"text": "2"}]}])
        t.selectAll()
        assert seen and seen[-1] == ["a", "b"], seen

    def test_delegate_draws_the_box(self, qt_app):
        """真画一遍,确认不炸 —— 画框那段用了 option.state 与主题色。"""
        t = W.DataTable(("*",), multi=True)
        t.set_rows([{"key": "a", "cells": [{"text": "一"}]}])
        t.set_check_mode(True)
        t.selectAll()
        t.resize(320, 80)
        assert not t.grab().isNull()


class TestBreakableFilename:
    """文件名不许被截掉 —— 序号和角度正好在被截的那一段。"""

    NAME = "Light_IC 4603_300.0s_Bin1_4C_20260725-202016_276deg_0001.fit"

    def test_inserts_break_opportunities(self):
        out = W.breakable(self.NAME)
        assert "​" in out, "没有插入换行机会,长名字还是会被截"
        # 可见文本一个字都不能变
        assert out.replace("​", "") == self.NAME

    def test_breaks_after_separators(self):
        out = W.breakable("a_b-c.d")
        assert out == "a_​b-​c.​d", out

    def test_a_name_without_separators_is_untouched(self):
        assert W.breakable("abc") == "abc"


class TestDetailRowsKeepTheGauge:
    """共享层给的元组第 6 项是量条,不能在展平时丢掉。"""

    GROUPS = [(
        "", "目标",
        [("坐标", "16h26m51s", "", True),
         ("高度角", "35.5°", "(偏低)", False, "warn", ("altbar", 35.5)),
         ("方位", "182° (南)")],
    )]

    def test_altbar_survives(self):
        rows = bp._flat_rows(self.GROUPS)
        alt = next(r for r in rows if r["key"] == "高度角")
        assert alt["bar"] == ("altbar", 35.5), (
            "高度角的量条被丢了 —— 老 UI 有,它才是'35° 算高还是低'的判据")

    def test_tone_and_mono_survive(self):
        rows = bp._flat_rows(self.GROUPS)
        by = {r["key"]: r for r in rows}
        assert by["高度角"]["tone"] == "warn"
        assert by["坐标"]["mono"] is True, "坐标要等宽,逐位比对 RA/DEC 是常做的事"

    def test_rows_without_extras_still_work(self):
        rows = bp._flat_rows(self.GROUPS)
        assert next(r for r in rows if r["key"] == "方位")["bar"] is None

    def test_value_joins_note(self):
        rows = bp._flat_rows(self.GROUPS)
        assert next(r for r in rows
                    if r["key"] == "高度角")["value"] == "35.5° (偏低)"


class TestBadgesAreColoured:
    """一排徽章全用强调色,等于把'这是什么帧'这条信息抹掉。"""

    def test_light_and_flat_differ(self):
        assert bp._badge_tone("light") != bp._badge_tone("flat")

    def test_night_kind_is_recognised(self):
        assert bp._badge_tone("night:2026-07-25") is not None

    def test_unknown_kind_is_neutral(self):
        assert bp._badge_tone("wat") is None


class TestGauge:
    def test_clamps(self, qt_app):
        assert W.Gauge(-3.0)._frac == 0.0
        assert W.Gauge(9.0)._frac == 1.0

    def test_paints(self, qt_app):
        g = W.Gauge(0.4, tone="warn", lo="0°", hi="90°")
        g.resize(120, 20)
        assert not g.grab().isNull()


class TestGroupHeaderGlyphsAreCrossPlatform:
    """共享层的 glyph 是 Segoe MDL2 私用区码位,macOS/Linux 上是方框。"""

    def test_all_glyphs_are_bmp(self):
        for key, glyph in W.SEGOE_GLYPHS.items():
            for ch in glyph:
                assert ord(ch) < 0xE000 or ord(ch) > 0xF8FF, (
                    f"U+{ord(key[0]):04X} 换出来还是私用区码位 {ch!r} ——"
                    " 换平台就是一个方框")
                assert ord(ch) <= 0xFFFF, (
                    f"U+{ord(key[0]):04X} 换出来是星平面字符 ——"
                    " 这个仓库为它踩过长度截断的坑")

    def test_every_shared_layer_glyph_can_be_translated(self):
        """**查的是码位不是组名。**

        原来这条断言的是 ``"目标" in GROUP_GLYPHS`` —— 而组名是共享层给的
        **显示文本**,一做 i18n 就全都对不上,七个组的图标一起退回一个点,
        不报错也不违反任何契约。码位与语言无关,所以现在按码位查。
        """
        import ast
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "astro_smb_app" / "views"
        missing = []
        for p in sorted(root.glob("*.py")):
            tree = ast.parse(p.read_text(encoding="utf-8"))
            for node in tree.body:
                if not (isinstance(node, ast.Assign)
                        and isinstance(node.targets[0], ast.Name)
                        and node.targets[0].id.lstrip("_").startswith("GRP")
                        and isinstance(node.value, ast.Constant)):
                    continue
                if node.value.value not in W.SEGOE_GLYPHS:
                    missing.append(f"{p.name}:{node.targets[0].id}")
        assert missing == [], f"这些分组图标没有跨平台替身: {missing}"
