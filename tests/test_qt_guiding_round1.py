"""导星页第一轮独立验收判"不过"的四条 + 记在案的那批。

两条是这个项目的招牌病:

* **3.8 偏差散点从来没画过。** 代码写的是 `ch.get("scatter")`,而共享层
  给的是平铺的 `sc_pts`/`sc_rng`/`rms_total` —— 那个键**从来不存在**,
  条件永远 False,`_Scatter` 这个类一次都没被实例化。不报错、不违反契约,
  只是八张图变成七张。
* **3.3 切档之后颜色不重算。** 和 10.2 是同一条,但**上一版的闸门没抓到**:
  它只查"谁用了 `OpsCanvas`",而导星页把 `tone_color()` 的结果烤进
  `DataTable` 的 cell 字典里。实测切红光档,段列表还是绿色字 ——
  红光档存在的唯一理由就是不破坏暗适应。判据因此放宽成"谁取过主题色"。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

ROOT = Path(__file__).resolve().parents[1]
QT = ROOT / "astro_smb_qt"
PAGE = QT / "pages" / "guiding.py"
MODELS = QT / "models.py"


@pytest.fixture(scope="module")
def qt_app():
    from PySide6.QtWidgets import QApplication

    from astro_smb_qt import theme

    inst = QApplication.instance() or QApplication([])
    theme.apply(inst)
    return inst


@pytest.fixture(scope="module")
def prepared():
    """真样例日志跑一遍 `_prepare` —— 结构断言挡不住"键名读错"。"""
    from astro_smb.autorunlog import aggregate_nights, parse_autorun_log
    from astro_smb.phd2log import parse_phd2_log
    from astro_smb_app.views import guiding as gv

    from tests.test_qt_models import AUTORUN, _LogData, _phd2_text

    log = parse_autorun_log(AUTORUN, "Autorun_Log_2026-07-29_222414.txt")
    data = _LogData(aggregate_nights([log]), [parse_phd2_log(_phd2_text())],
                    [log])
    return data, gv._prepare(data)


def _fn(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name} 里没有 {name}")


def _src(path: Path, name: str) -> str:
    node = _fn(path, name)
    body = node.body[1:] if (node.body and isinstance(node.body[0], ast.Expr)
                             and isinstance(node.body[0].value, ast.Constant)
                             ) else node.body
    return "\n".join(ast.unparse(n) for n in body)


class TestScatterIsActuallyDrawn:
    """3.8:那个键从来不存在,`_Scatter` 一次都没画过。"""

    def test_the_flat_keys_are_what_the_shared_layer_gives(self, prepared):
        """先把事实钉死:共享层给的是 `sc_pts`,**没有** `scatter`。"""
        from astro_smb_qt import models

        _data, prep = prepared
        row = prep["rows"][models.default_guide_row(prep["rows"])]
        ch = models.chart_payload(row, window_index=0, pos=0, width=760.0)
        charts = ch.get("charts") or {}
        assert "sc_pts" in charts, "共享层的键名变了,这条断言要跟着改"
        assert "scatter" not in charts, (
            "共享层真给了 `scatter`?那原来的代码就没错,这个文件白写")

    def test_the_page_reads_the_flat_key(self):
        src = _src(PAGE, "_small_charts")
        assert "ch.get('sc_pts')" in src, "又去读那个不存在的 `scatter` 了"
        assert "ch.get('scatter')" not in src

    def test_eight_charts_on_real_data(self, qt_app, prepared):
        """**行为验证**:老 UI 八张,这边也要八张。"""
        from astro_smb_qt import models
        from astro_smb_qt.pages.guiding import _small_charts
        from astro_smb_qt.shell import Shell

        data, prep = prepared
        row = prep["rows"][models.default_guide_row(prep["rows"])]
        ch = models.chart_payload(row, window_index=0, pos=0, width=760.0)
        small = _small_charts(ch.get("charts") or {}, ch.get("unit") or "″")
        page = Shell().page("guiding")
        page.data, page.prep = data, prep
        page.selected = models.default_guide_row(prep["rows"])
        total = len(small) + (1 if page._overview_chart() is not None else 0)
        assert total == 8, f"只有 {total} 张图(老 UI 是 8 张)"

    def test_scatter_gets_its_range_and_rms(self, qt_app, prepared):
        """**参考圆要靠 rng/rms 才画得出来。** 只验标题里有"散点"的话,
        把这两个参数删掉照样绿 —— 图上就只剩一团点,读不出 1×/2×RMS。"""
        from astro_smb_qt import models
        from astro_smb_qt.pages.guiding import _Scatter, _small_charts

        _data, prep = prepared
        row = prep["rows"][models.default_guide_row(prep["rows"])]
        ch = models.chart_payload(row, window_index=0, pos=0, width=760.0)
        charts = ch.get("charts") or {}
        sc = None
        for card in _small_charts(charts, "″"):
            found = card.findChildren(_Scatter)
            if found:
                sc = found[0]
        assert sc is not None, "散点画布没建出来"
        assert sc._sc.get("rng") == charts.get("sc_rng"), "量程没传进去"
        assert sc._sc.get("rms") == charts.get("rms_total"), "RMS 没传进去"

    def test_scatter_is_among_them(self, qt_app, prepared):
        from PySide6.QtWidgets import QLabel

        from astro_smb_qt import models
        from astro_smb_qt.pages.guiding import _small_charts

        _data, prep = prepared
        row = prep["rows"][models.default_guide_row(prep["rows"])]
        ch = models.chart_payload(row, window_index=0, pos=0, width=760.0)
        titles = []
        for card in _small_charts(ch.get("charts") or {}, "″"):
            titles += [lb.text() for lb in card.findChildren(QLabel)]
        assert any("散点" in t for t in titles), f"散点还是没画:{titles}"


class TestOverviewChart:
    """3.8 的另一半:逐段 RMS 总览,共享层算好了但没人消费。

    **样例日志只有一段导星** —— 总览只有一根柱,而"没跳"和"跳到了唯一
    那根"分不出来(反向验证里这三条全活了)。所以下面一律用**多根柱**的
    合成 overview,并且断言"从别处跳过来",不是断言最终值。
    """

    #: 三根柱,对应数据行下标 0 / 4 / 7
    FAKE = {"bars": [(0, 0.5), (4, 1.9), (7, 3.4)], "unit": "″", "mixed": False}

    def _page(self, qt_app, prepared, selected=0):
        from astro_smb_qt.shell import Shell

        data, prep = prepared
        page = Shell().page("guiding")
        page.data = data
        page.prep = dict(prep, overview=self.FAKE)
        page.selected = selected
        page._render = lambda: None
        return page

    def test_shared_layer_provides_it(self, prepared):
        _data, prep = prepared
        assert prep.get("overview"), "共享层没给 overview,这条断言失效"

    def test_the_page_consumes_it(self, qt_app, prepared):
        page = self._page(qt_app, prepared)
        assert page._overview_chart() is not None, "总览没画出来"

    def test_it_is_wired_into_the_chart_grid(self, qt_app, prepared):
        """**接线也要验。** 直接调 `_overview_chart()` 是绕过接线的 ——
        把 `_render_charts` 里那三行删掉,只调函数的断言照样绿。"""
        src = _src(PAGE, "_render_segment")
        assert "self._overview_chart()" in src, "总览没接进图表区"
        assert "small.append(ovw)" in src

    def test_clicking_a_bar_jumps(self, qt_app, prepared):
        """**命中靠几何反算**,柱最窄 2px,逐根挂事件几乎点不中。"""
        from astro_smb_app.views import guiding as gv

        page = self._page(qt_app, prepared, selected=0)
        n = len(self.FAKE["bars"])
        span = float(gv.CHART_W) - 2 * gv.BAR_M
        # 打在**最后**一根柱的槽中央 —— 起点选 0,所以"没动"会留在 0
        x = gv.BAR_M + (n - 0.5) * (span / n)
        page._pick_overview(x, n)
        assert page.selected == 7, f"点最后一根柱没跳过去(还在 {page.selected})"

    def test_the_middle_bar_maps_to_the_middle_segment(self, qt_app, prepared):
        """反算要**逐槽**对得上,不是"总是第一根"或"总是最后一根"。"""
        from astro_smb_app.views import guiding as gv

        page = self._page(qt_app, prepared, selected=0)
        n = len(self.FAKE["bars"])
        span = float(gv.CHART_W) - 2 * gv.BAR_M
        page._pick_overview(gv.BAR_M + 1.5 * (span / n), n)
        assert page.selected == 4, f"中间那根柱跳错了段:{page.selected}"

    def test_out_of_range_click_does_nothing(self, qt_app, prepared):
        page = self._page(qt_app, prepared, selected=4)
        page._pick_overview(-5.0, len(self.FAKE["bars"]))
        assert page.selected == 4

    def test_levels_come_from_the_shared_layer(self):
        """在这里重写一遍阈值,等于让同一个数字在两处显示成两种好坏。"""
        src = (PAGE).read_text(encoding="utf-8")
        at = src.index("class _Overview")
        body = src[at:src.index("\n\nclass ", at + 10)]
        assert "gv._rms_level(" in body, "分档阈值不是从共享层取的"


class TestThemeSwitch:
    """3.3:上一版闸门只查 `OpsCanvas`,漏掉了两整页。"""

    def test_guiding_implements_on_theme(self):
        from astro_smb_qt.pages.guiding import GuidingPage

        assert "on_theme" in vars(GuidingPage)

    def test_sky3d_implements_it_too(self):
        from astro_smb_qt.pages.sky3d import Sky3DPage

        assert "on_theme" in vars(Sky3DPage)

    def test_it_rerenders(self, qt_app, prepared):
        from astro_smb_qt.shell import Shell

        data, prep = prepared
        page = Shell().page("guiding")
        page.data, page.prep = data, prep
        hits = []
        page._render = lambda: hits.append(1)
        type(page).on_theme(page)
        assert hits, "切档没有重生成段列表"

    def test_it_skips_before_data(self, qt_app):
        from astro_smb_qt.shell import Shell

        page = Shell().page("guiding")
        page.prep = {}
        page._render = lambda: (_ for _ in ()).throw(AssertionError("不该跑"))
        type(page).on_theme(page)


class TestGroupHeaderBadges:
    """3.2:组头少了段数与合并 RMS。"""

    def test_shared_layer_gives_them(self, prepared):
        _data, prep = prepared
        g = (prep.get("groups") or [])[0]
        assert "n_sec" in g and "rms" in g and "unit" in g

    def test_rows_carry_them(self, qt_app, prepared):
        from astro_smb_qt import models

        _data, prep = prepared
        rows = models.guiding_rows(prep, set(), set())
        heads = [r["title"] for r in rows if r.get("group")]
        assert heads, "一个组头都没有"
        assert any("段]" in h for h in heads), f"组头没有段数:{heads}"
        assert any("RMS" in h for h in heads), (
            f"组头没有合并 RMS —— 那是这一行最值钱的数:{heads}")

    def test_collapse_and_expand_all(self, qt_app, prepared):
        from astro_smb_qt.shell import Shell

        data, prep = prepared
        page = Shell().page("guiding")
        page.data, page.prep = data, prep
        page._render = lambda: None
        page._expand_all()
        assert len(page.expanded) == len(prep["groups"]), "全部展开没生效"
        page._collapse_all()
        assert not page.expanded and not page.frag_open, "全部折叠没生效"

    def test_group_note_exists(self):
        src = _src(PAGE, "_left")
        assert "group_note" in src and "全部折叠" in src and "全部展开" in src


class TestCalibrationRow:
    """3.11:选中校准行时卡头不换,一行"校准失败"旁边挂着别人的 RMS。"""

    def test_title_and_chip_are_updated(self):
        src = _src(PAGE, "_render_segment")
        head = src[:src.index("self.win_combo.setEnabled(True)")]
        assert "set_title" in head and "set_chip" in head, (
            "校准分支提前 return,卡头还留着上一段的标题与 RMS 胶囊")

    def test_it_reads_the_real_field(self):
        """字段叫 `cal_fail`(共享层 `_cal_row` 给的),不是 `cal_ok` ——
        读错名字不会报错,只会让每一行校准都显示成"成功"。"""
        src = _src(PAGE, "_render_segment")
        assert "row.get('cal_fail')" in src
        assert "cal_ok" not in src

    def test_window_controls_are_disabled(self):
        """不置灰的话滑一下什么也不动,像坏了。"""
        src = _src(PAGE, "_render_segment")
        head = src[:src.index("self.win_combo.setEnabled(True)")]
        assert "self.win_combo.setEnabled(False)" in head
        assert "self.slider.setEnabled(False)" in head

    def test_empty_segment_clears_the_header_too(self):
        """同一条路径的另一半:画不出曲线的段也不能留着上一段的卡头。"""
        src = _src(PAGE, "_render_segment")
        at = src.index("这一段画不出曲线")
        assert "set_chip('', None)" in src[:at]


class TestChartPolish:

    def test_pulse_puts_the_text_above_the_bar(self):
        """条从 x=48 起、数字右对齐到 w-4 —— 满格条正好压住数字
        (实测 "RA W" 显示成「…5·156950ms」)。字条分行就不可能压。"""
        src = (PAGE).read_text(encoding="utf-8")
        at = src.index("class _Pulse")
        body = src[at:src.index("\n\nclass ", at + 10)]
        assert "align_right=True" not in body, "数字又右对齐到画布边了"
        assert "次 ·" in body, "还在写原始毫秒 —— 读起来要自己除 1000"

    def test_time_ticks_land_on_clock_boundaries(self):
        """从段起点按步长推会给出 01:58 / 02:28 这种读不出规律的标签,
        而这张图的用处正是"几点开始变差"。"""
        src = _src(PAGE, "_time_ticks")
        assert "int(epoch + t0) // step" in src, "刻度不是按钟点对齐的"

    def test_y_labels_are_outside_the_plot(self):
        """刻度画在 x=2 会压着曲线,跳转高亮那种整幅铺满时尤其难认。"""
        src = (PAGE).read_text(encoding="utf-8")
        assert "Y_AXIS_W" in src
        curve = src[src.index("class _Curve"):src.index("class _Overview")]
        assert "Y_AXIS_W + (t - t0)" in curve, "曲线没有给刻度栏让位"

    def test_histogram_shows_its_range(self):
        """没有量程读不出这堆柱子是宽是窄。"""
        src = _src(PAGE, "_small_charts")
        assert "±{rng:.1f}{unit}" in src

    def test_rolling_rms_shows_its_peak(self):
        src = _src(PAGE, "_small_charts")
        assert "roll_max" in src

    def test_drift_explanation_is_permanent(self):
        """数值为 0 时这张图就是两条竖线加两个 +0.00,没有解释看不懂。"""
        src = _src(PAGE, "_small_charts")
        assert "极轴误差" in src


class TestJumpResetsTheWindow:
    def test_show_range_goes_back_to_full(self, qt_app, prepared):
        from astro_smb_qt.shell import Shell

        data, prep = prepared
        page = Shell().page("guiding")
        page.data, page.prep = data, prep
        page.window_index, page.pos = 3, 50
        page._render = lambda: None
        page._locate = lambda *_a: None
        page.show_range(0.0, 1.0, "x")
        assert page.window_index == 0, (
            "跳过来还留着上一次的 5 分钟窗 —— 那个窗很可能整个落在高亮区间外")
        assert page.pos == 0
