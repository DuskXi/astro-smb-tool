"""事件时间线必须是**结构化**的,不是一串 `·` 拼起来的文本行(清单 2.10)。

老 UI 那侧一条事件长这样:

    23:41:02  ●  目标块 #1 结束 · 完成
    ~23:52:10 │  RA 17h22m  DEC -36°07
              │  ▓▓▓▓▓▓▓░░░  28/30

时刻一列、状态色标记一列、卡片一列;标记按 `level` 上色、按 `kind` 分方旗
与圆点,Shooting 组还带一条"实拍/计划"的迷你进度条。

Qt 这边原来把这几样拼成一行灰字。**不报错、不崩溃**,只是"哪一步出了事"
要逐行读文字才知道 —— 和导星页 3.9(段统计卡被拼成一行 `·` 串)同一个病。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

ROOT = Path(__file__).resolve().parents[1]
QT = ROOT / "astro_smb_qt"


@pytest.fixture(scope="module")
def qt_app():
    from PySide6.QtWidgets import QApplication

    from astro_smb_qt import theme

    inst = QApplication.instance() or QApplication([])
    theme.apply(inst)
    return inst


def _fn(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name} 里没有 {name}")


def _synth():
    """合成的一夜(与 `test_qt_models` 同一份样例日志)。

    行为断言必须跑真数据 —— 只查源码的话,"键还在但值永远是空串"这种
    改动会安静地通过(反向验证抓到过一次)。
    """
    from tests.test_qt_models import AUTORUN, _LogData, _phd2_text
    from astro_smb.autorunlog import aggregate_nights, parse_autorun_log
    from astro_smb.phd2log import parse_phd2_log

    log = parse_autorun_log(AUTORUN, "Autorun_Log_2026-07-29_222414.txt")
    return _LogData(aggregate_nights([log]), [parse_phd2_log(_phd2_text())],
                    [log])


def _src(path: Path, name: str) -> str:
    """函数体源码。**`ast.unparse` 把字符串常量一律吐成单引号** ——
    下面的断言因此统一用单引号写。"""
    node = _fn(path, name)
    body = node.body[1:] if (node.body and isinstance(node.body[0], ast.Expr)
                             and isinstance(node.body[0].value, ast.Constant)
                             ) else node.body
    return "\n".join(ast.unparse(n) for n in body)


class TestModelKeepsTheStructure:

    def test_run_detail_does_not_flatten(self):
        src = _src(QT / "models.py", "run_detail")
        assert "' · '.join" not in src, (
            "又把时刻/标题/副标题在模型层拼成一行了 —— 那一拼,"
            "level(状态色)、kind(方旗/圆点)、progress(进度条)三样全丢")

    def test_every_field_survives(self):
        """四个字段各查一次。少任何一个,界面上就少一样东西。"""
        src = _src(QT / "models.py", "run_detail")
        for key in ("'kind'", "'level'", "'when'", "'progress'"):
            assert key in src, f"事件条目没带 {key}"

    def test_progress_is_passed_through_untouched(self):
        """进度是 `(实拍, 计划)` 二元组,别在模型层就算成百分比 ——
        `28/30` 这个原始读数本身要显示出来。"""
        src = _src(QT / "models.py", "run_detail")
        assert "item.get('progress')" in src, "进度没有原样带过来"

    def test_the_clock_column_actually_has_times(self):
        """**行为验证,不是"源码里出现过 `when` 这个键"。**

        反向验证抓到过:把 `"when"` 的值改成空串,上面那条按键名查的断言
        照样绿 —— 键还在,只是永远是空的。时刻列于是整列空白,而"这一步
        发生在几点"正是时间线存在的理由。
        """
        import re

        from astro_smb_qt import models

        d = models.records_model(_synth())["detail"]
        whens = [str(ev.get("when") or "") for ev in d["events"]]
        assert whens, "一条事件都没有 —— 这条断言没在测任何东西"
        good = [w for w in whens if re.fullmatch(r"\d{2}:\d{2}:\d{2}", w)]
        assert len(good) >= len(whens) // 2, (
            f"时刻列基本是空的 —— 前几条: {whens[:5]}")


class TestPageRendersRows:

    def test_page_uses_the_structured_widgets(self):
        src = _src(QT / "pages" / "records.py", "_render_detail")
        assert "W.TimelineRow(" in src, "事件还是用普通 label 铺的"
        assert "W.TimelineGap(" in src, (
            "间隙没有单独画 —— 那会让「空了 6 分钟」看起来像又发生了一件事")

    def test_first_and_last_are_marked(self):
        """首尾行要告诉控件自己是首/尾,否则时间线两端各挂一截断头。"""
        src = _src(QT / "pages" / "records.py", "_render_detail")
        assert "first=" in src and "last=" in src


class TestRowVisuals:

    def _row(self, *, first=False, last=False, **over):
        from astro_smb_qt import widgets as W

        item = {"kind": "block", "level": "ok", "when": "23:41:02",
                "when2": "~23:52:10", "title": "目标块 #1 结束 · 完成",
                "subtitle": "RA 17h22m  DEC -36°07", "progress": (28, 30)}
        item.update(over)
        return W.TimelineRow(item, first=first, last=last)

    def test_three_columns(self, qt_app):
        """时刻 / 轨道 / 卡片。少一列就退化成"带缩进的一行字"。"""
        row = self._row()
        assert row.layout().count() == 3, (
            f"不是三列布局(实际 {row.layout().count()} 个)")

    def test_time_column_is_fixed_width(self, qt_app):
        """时刻列不定宽的话,每行的卡片起点都不一样,整列会呈锯齿。"""
        from astro_smb_qt import widgets as W

        # **行必须先绑到一个名字上。** 写成 `self._row().layout()...` 的话
        # 那个无父的行是个临时对象,取完子控件就被回收,底下的 C++ 对象跟着
        # 析构 —— 报的是 "Internal C++ object already deleted",看着像 Qt 的
        # 毛病,其实是测试自己写漏了引用。
        row = self._row()
        t = row.layout().itemAt(0).widget()
        assert W.TimelineRow.T_COL in (t.width(), t.minimumWidth()), (
            "时刻列没有定宽")

    def test_end_time_is_shown(self, qt_app):
        """带跨度的条目要显示 `~结束时刻` —— 否则一张覆盖十分钟的
        AutoFocus 卡看起来跟一个瞬时事件一样。"""
        row = self._row()
        t = row.layout().itemAt(0).widget()
        assert "23:52:10" in t.text(), f"结束时刻没显示: {t.text()!r}"

    def test_progress_bar_appears_only_when_given(self, qt_app):
        from astro_smb_qt import widgets as W

        with_bar, without = self._row(), self._row(progress=None)
        assert with_bar.findChildren(W.Gauge), "给了 progress 却没有进度条"
        assert not without.findChildren(W.Gauge), "没给 progress 也画了一根条"

    def test_progress_shows_the_raw_counts(self, qt_app):
        from PySide6.QtWidgets import QLabel

        row = self._row()
        texts = [c.text() for c in row.findChildren(QLabel)]
        assert any("28/30" in t for t in texts), (
            f"只画了条没写数字 —— 「28/30」是「还差两张」的唯一读法: {texts}")

    def test_title_and_subtitle_both_render(self, qt_app):
        from PySide6.QtWidgets import QLabel

        row = self._row()
        texts = [c.text() for c in row.findChildren(QLabel)]
        assert any("目标块 #1 结束" in t for t in texts), texts
        assert any("RA 17h22m" in t for t in texts), (
            f"副标题没画 —— RA/DEC 就是靠它显示的: {texts}")

    def test_level_reaches_the_rail(self, qt_app):
        """行要把 level 交给轨道去上色,而不是自己吞掉。"""
        assert self._row(level="err").rail_geometry()[0] == "err"

    def test_kind_reaches_the_rail(self, qt_app):
        assert self._row(kind="guide").rail_geometry()[1] == "guide"

    def test_first_last_reach_the_rail(self, qt_app):
        assert self._row(first=True).rail_geometry()[2] is True
        assert self._row(last=True).rail_geometry()[3] is True

    def test_three_levels_are_three_colours(self, qt_app):
        """全一个色 = 状态信息没了。"""
        from astro_smb_qt import theme

        names = {theme.tone_color(t).name() for t in ("ok", "warn", "err")}
        assert len(names) == 3, f"ok/warn/err 取到了同一个颜色: {names}"


class TestRailDrawing:

    def _rail_body(self) -> str:
        src = (QT / "widgets.py").read_text(encoding="utf-8")
        at = src.index("class _RailStrip")
        return src[at:src.index("\n\nclass ", at + 10)]

    def test_block_and_dot_differ(self):
        """目标块边界画方旗、其余画圆点 —— 判据要真的读 `kind`。"""
        body = self._rail_body()
        assert "addRoundedRect" in body and "addEllipse" in body, (
            "方旗和圆点没有分开画")
        assert "kind ==" in body, "根本没读 kind"

    def test_rail_skips_the_stub_at_both_ends(self):
        body = self._rail_body()
        assert "if not first:" in body and "if not last:" in body, (
            "首尾行照画连接线 —— 时间线两端各挂一截断头")

    def test_marker_colour_comes_from_the_theme(self):
        """自己造颜色会绕过红光模式的映射。"""
        body = self._rail_body()
        assert "theme.tone_color(" in body, "标记颜色不是从主题取的"

    def test_it_actually_paints(self, qt_app):
        """**真的画一遍。**

        上面几条都是读源码的 —— `paintEvent` 里少 import 一个 `QPointF`
        会抛 `NameError`,而 Qt 把绘制异常吞掉(控件只是白着),这些断言
        一条都不会红。三档配色各画一次,顺带覆盖红光模式的颜色映射。
        """
        from astro_smb_qt import theme, widgets as W

        before = theme.C.mode
        try:
            for mode in theme.MODES:
                theme.set_mode(mode)
                for kind, first, last in (("block", True, False),
                                          ("guide", False, False),
                                          ("gap2", False, True)):
                    row = W.TimelineRow(
                        {"kind": kind, "level": "warn", "when": "01:02:03",
                         "title": "画一下", "subtitle": "副标题"},
                        first=first, last=last)
                    row.resize(420, 64)
                    img = row.grab().toImage()
                    assert not img.isNull(), f"{mode}/{kind} 画出来是空的"
        finally:
            theme.set_mode(before)

    def test_the_marker_is_really_drawn(self, qt_app):
        """轨道那一列不能是一片背景色 —— 标记和竖线得真的落在像素上。"""
        from astro_smb_qt import widgets as W

        row = W.TimelineRow(
            {"kind": "block", "level": "err", "when": "01:02:03",
             "title": "画一下"}, first=False, last=False)
        row.resize(420, 64)
        rail = row.layout().itemAt(1).widget()
        img = rail.grab().toImage()
        colours = {img.pixel(x, y)
                   for x in range(img.width()) for y in range(img.height())}
        assert len(colours) > 1, "轨道列只有一种颜色 —— 标记/竖线根本没画上"


class TestGapRow:

    def test_gap_has_no_card_and_no_bar(self, qt_app):
        """间隙是分隔,不是事件 —— 不给卡片、不给进度条。"""
        from astro_smb_qt import widgets as W

        gap = W.TimelineGap("空了 6 分 40 秒")
        assert not gap.findChildren(W.Gauge)
        assert not [c for c in gap.findChildren(object)
                    if getattr(c, "objectName", lambda: "")()
                    == "TimelineCard"]

    def test_gap_text_survives(self, qt_app):
        from PySide6.QtWidgets import QLabel

        from astro_smb_qt import widgets as W

        gap = W.TimelineGap("空了 6 分 40 秒")
        texts = [c.text() for c in gap.findChildren(QLabel)]
        assert any("6 分 40 秒" in t for t in texts), texts


class TestStyleExists:
    """图元有了 ≠ 样式表认得它。QSS 少一条规则不会报错,只是没底色。"""

    def test_every_mode_styles_them(self, qt_app):
        """三档配色都要有 —— 只在常规档加规则的话,切到白天就没底色了。"""
        from astro_smb_qt import theme

        before = theme.C.mode
        try:
            for mode in theme.MODES:
                theme.set_mode(mode)
                qss = theme.stylesheet()
                # **选择器要连着后面那个 `{` 一起查。** 只查 `#TimelineCard`
                # 的话,把它改名成 `#TimelineCardXX` 照样绿 —— 前缀是子串。
                # (这个坑在别处已经栽过一次:`_probe_alive` ⊂
                # `_probe_alive_disabled`。)
                assert "#TimelineCard {" in qss, f"{mode} 档缺 TimelineCard 底色"
                assert 'QLabel[role="strong"] {' in qss, f"{mode} 档缺 strong 字重"
        finally:
            theme.set_mode(before)
