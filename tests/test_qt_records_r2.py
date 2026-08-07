"""拍摄记录页第二轮复验抓出来的四条。

**核心那条又是"共享层支持的字段,前端没传"** —— 而且是修过一次之后,
从一条没堵上的路径重新冒出来的。

点时间轴彩条走 `_render()`(带 ``fits_map`` 与 ``site``),点左侧目标列表却
自己拼了一份**不带这两个字段**的模型、还只重画详情。于是同一个目标,
从列表点进去比从时间轴点进去少了「实测坐标」「设备」两行、少一个滤镜徽章,
天球上的高亮环与时刻**干脆不动**。不报错,只是从**最常用**的那条路进去
看到的是残的。

所以这里不测"参数补上了没有" —— 测的是**两个入口走不走同一段代码**。
补参数还能再漏第三次,合并入口才没有地方分叉。
"""
from __future__ import annotations

import ast
import math
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

ROOT = Path(__file__).resolve().parents[1]
RECORDS = ROOT / "astro_smb_qt" / "pages" / "records.py"


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    from PySide6.QtWidgets import QApplication

    from astro_smb_qt import theme

    inst = QApplication.instance() or QApplication([])
    theme.apply(inst)
    return inst


def _page():
    from astro_smb_qt.shell import Shell

    return Shell().page("records")


def _fn_src(path: Path, name: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            body = node.body[1:] if (node.body
                                     and isinstance(node.body[0], ast.Expr)
                                     and isinstance(node.body[0].value,
                                                    ast.Constant)
                                     ) else node.body
            return "\n".join(ast.unparse(n) for n in body)
    raise AssertionError(f"{path.name} 里没有 {name}")


class TestBothPicksGoThroughOnePath:

    def _spy(self, page):
        hits = []
        page._render = lambda: hits.append("full")
        page._render_detail = lambda _m: hits.append("partial")
        page.runs.select_key = lambda _k: None
        return hits

    def test_list_click_does_a_full_render(self):
        """点列表**必须走整页重渲染**。

        只重画详情就是那个缺陷本身:详情用的模型没有 ``fits_map``/``site``,
        天球也不会跟着换。
        """
        page = _page()
        hits = self._spy(page)
        page._pick_run("3")
        assert hits == ["full"], f"点列表走的是 {hits}"
        assert page.selected == 3

    def test_timeline_click_does_the_same(self):
        page = _page()
        hits = self._spy(page)
        page._pick_span("3")
        assert hits == ["full"], f"点时间轴走的是 {hits}"

    def test_the_two_are_indistinguishable(self):
        """两条路留下的状态要一模一样 —— 这才是"同一件事"的定义。"""
        page = _page()
        seen = []
        for pick in (page._pick_run, page._pick_span):
            hits = self._spy(page)
            page.selected = 0
            pick("5")
            seen.append((page.selected, tuple(hits)))
        assert seen[0] == seen[1], seen

    def test_group_headers_are_still_ignored(self):
        """``g:``/``x:`` 是组头与间隙,**不是目标**,点了不该换详情。"""
        page = _page()
        hits = self._spy(page)
        page.selected = 7
        page._pick_run("g:1")
        page._pick_span("x:2")
        assert hits == [], hits
        assert page.selected == 7

    def test_the_full_render_really_carries_the_fields(self):
        """整页重渲染本身得带着那两个字段,否则合并入口也没意义。"""
        src = _fn_src(RECORDS, "_render")
        assert "fits_map=self.fits_map" in src
        assert "site=self._site()" in src


class TestZoomOverlayHighlight:

    def test_it_passes_the_selection(self):
        """放大层不传 ``selected`` 的话所有点长得一样 —— 页面上明明有个
        高亮环,点「放大」反而找不到"我看的是哪一个",而放大正是为了看清它。"""
        src = _fn_src(RECORDS, "_on_slide")
        assert "selected=self.page.selected" in src

    def test_sky_payload_actually_takes_it(self):
        """顺带钉住共享层真的收这个参数 —— 只测调用方写了没有,
        改个参数名两边一起坏还是绿的。"""
        import inspect

        from astro_smb_qt import models

        assert "selected" in inspect.signature(models.sky_payload).parameters


class TestNightComboWidth:
    """和 3D 天球页**同一个坑**,第二个页面又踩了一次。"""

    def test_helper_lives_in_the_shared_layer(self):
        from astro_smb_qt import widgets as W

        assert callable(W.fit_combo)

    def test_records_combo_fits_its_items(self):
        from PySide6.QtGui import QFont

        from astro_smb_qt import widgets as W

        app = __import__("PySide6.QtWidgets", fromlist=["QApplication"]) \
            .QApplication.instance()
        old = app.font()
        try:
            for pt in (9, 12, 16):
                app.setFont(QFont(old.family(), pt))
                page = _page()
                labels = ["2026-07-23 · 12 目标 · 1200 帧",
                          "2026-07-29 · 2 目标 · 59 帧"]
                page.night_combo.clear()
                page.night_combo.addItems(labels)
                W.fit_combo(page.night_combo, labels)
                fm = page.night_combo.fontMetrics()
                need = max(fm.horizontalAdvance(s) for s in labels)
                assert page.night_combo.minimumWidth() >= need, (pt, need)
        finally:
            app.setFont(old)

    def test_it_is_refit_after_items_land(self):
        """构造时按样例预留过宽度,填完真实夜次**必须再算一次** ——
        不然更长的项(目标两位数、帧数四位数)照样被截。"""
        src = RECORDS.read_text(encoding="utf-8")
        at = src.index("self.night_combo.addItems(")
        assert "fit_combo" in src[at:at + 200]


class TestRedThemeSeverityColours:

    def _dist(self, a, b):
        return math.dist((a.red(), a.green(), a.blue()),
                         (b.red(), b.green(), b.blue()))

    def test_accent_and_bad_are_distinguishable_everywhere(self):
        """事件时间线上"某步开始"(info→ACCENT)与"失败"(err→BAD)
        必须一眼分得开。红光档曾经只差 22 个 RGB 单位,靠图形形状才能辨。"""
        from PySide6.QtGui import QColor

        from astro_smb_qt import theme

        before = theme.C.mode
        try:
            for m in theme.MODES:
                theme.set_mode(m)
                d = self._dist(QColor(theme.Q.ACCENT), QColor(theme.Q.BAD))
                assert d >= 60, f"{m} 档 ACCENT 与 BAD 只差 {d:.0f}"
        finally:
            theme.set_mode(before)

    def test_red_theme_separates_by_lightness(self):
        """红光档**不能靠色相**分开(整档只有红,那是它的意义所在),
        只能靠明度 —— 所以这里单独钉一条。"""
        from PySide6.QtGui import QColor

        from astro_smb_qt import theme

        before = theme.C.mode
        try:
            theme.set_mode(theme.MODE_RED)
            a, b = QColor(theme.Q.ACCENT), QColor(theme.Q.BAD)
            lum = [0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
                   for c in (a, b)]
            assert abs(lum[0] - lum[1]) >= 25, lum
            # 仍然要是红的:红光档不许混进蓝绿
            for c in (a, b):
                assert c.red() > c.green() and c.red() > c.blue()
        finally:
            theme.set_mode(before)
