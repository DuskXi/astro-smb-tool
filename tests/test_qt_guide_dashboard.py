"""导星仪表盘:**一组段合起来**看是什么样。

逐段看得到"这一段抖了",看不到"这一晚这个目标整体如何" —— 而后者才是
"要不要重拍 / 要不要调极轴"的依据。老 UI 里它是组头右侧那颗按钮。

这一份从冻结的 `astro_smb_gui/_guidedash.py` **复制**了纯计算部分到
`astro_smb_app/views/guidedash.py`(老 UI 一个字节没碰),判读阈值仍然
复用 `views.guiding` 的那几个 —— 没有第二份阈值。

抽的时候被架构门禁抓到两条,都记在断言里:
① 抽过来的代码里带着 `from astro_smb_gui.preview import ...` —— 共享层
   反向依赖前端,而那个前端还是冻结的;
② 顺手抽了个 `_corner()`(纯 WinUI 的 `CornerRadius`),`import` 时不报错,
   **调用到才 NameError**。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from tests.support import tr

pytest.importorskip("PySide6")

ROOT = Path(__file__).resolve().parents[1]
QT = ROOT / "astro_smb_qt"
PAGE = QT / "pages" / "guiding.py"
GD = ROOT / "astro_smb_app" / "views" / "guidedash.py"


@pytest.fixture(scope="module")
def qt_app():
    from PySide6.QtWidgets import QApplication

    from astro_smb_qt import theme

    inst = QApplication.instance() or QApplication([])
    theme.apply(inst)
    return inst


@pytest.fixture(scope="module")
def prepared():
    from astro_smb.autorunlog import aggregate_nights, parse_autorun_log
    from astro_smb.phd2log import parse_phd2_log
    from astro_smb_app.views import guiding as gv

    from tests.test_qt_models import AUTORUN, _LogData, _phd2_text

    log = parse_autorun_log(AUTORUN, "Autorun_Log_2026-07-29_222414.txt")
    data = _LogData(aggregate_nights([log]), [parse_phd2_log(_phd2_text())],
                    [log])
    return data, gv._prepare(data)


@pytest.fixture(scope="module")
def agg(prepared):
    from astro_smb_app.views import guidedash as gd

    data, prep = prepared
    return gd.aggregate_group(prep["groups"][0], prep["rows"], data)


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


class TestExtractionIsClean:
    """**原来这里有一条 `test_the_frozen_ui_is_untouched`,已经删掉。**

    它跑的是 ``git diff --name-only HEAD -- astro_smb_gui``,也就是
    **工作区与 HEAD 的差异** —— 提交前红、提交后绿,而两次的**内容一模一样**。
    那不是一个不变量,是"这次会话别碰老 UI"。2026-08-03 冻结改成
    「同步提醒」门禁(改可以,但要重算基线 + 在 `frontend.md` 记一笔)之后,
    它对每一次合法改动都会误报。

    它想守的事由 `tests/test_legacy_ui_freeze.py` 守着,而且守得更对:
    比的是**内容哈希与记录基线**,并且带一个显式的逃生口。
    """

    def test_no_reverse_dependency_on_the_frontend(self):
        """抽过来的代码里带着 `from astro_smb_gui.preview import ...` ——
        共享层反向依赖前端,而那个前端还是冻结的。"""
        # **走 AST 查真 import。** 剥 `#` 注释不够 —— 模块 docstring 里
        # 写着"从 `astro_smb_gui/_guidedash.py` 抽出来的",那句话本身会
        # 让按子串查的断言永远红(这一轮在"匹配到文档字符串"上栽过多次)。
        tree = ast.parse(GD.read_text(encoding="utf-8"))
        bad = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                bad += [a.name for a in node.names
                        if a.name.startswith("astro_smb_gui")]
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").startswith("astro_smb_gui"):
                    bad.append(node.module)
        assert not bad, f"共享层 import 了老 UI: {bad}"

    def test_no_winui_leftovers(self):
        """`_corner()` 返回 `CornerRadius` —— 纯 WinUI 的类型。
        import 时不报错,**调用到才 NameError**。"""
        src = GD.read_text(encoding="utf-8")
        for name in ("CornerRadius", "SolidColorBrush", "XamlReader",
                     "Visibility", "FontWeights"):
            assert name not in src, f"抽过来的代码里还留着 WinUI 的 {name}"

    def test_thresholds_still_come_from_the_shared_guiding_module(self):
        """判读阈值不许在这里再写一份 —— 同一个数字在两处显示成两种好坏
        是这个项目最贵的一类 bug。"""
        src = GD.read_text(encoding="utf-8")
        assert "from astro_smb_app.views import guiding as G" in src
        assert "G._rms_level(" in src or "G._bucket_peak(" in src


class TestAggregation:

    def test_it_produces_the_headline_numbers(self, agg):
        for key in ("unit", "n_sec", "n_frames", "lost_pct", "ellipse",
                    "acf", "pulse_balance", "drift", "meta", "ch"):
            assert key in agg, f"聚合结果缺 {key}"

    def test_rms_ellipse_is_computed(self, agg):
        """椭圆轴比是"各向同性抖动 vs 单轴在跑"的唯一直接读数。"""
        el = agg.get("ellipse")
        assert el and el["a"] >= el["b"] > 0

    def test_dashboard_text_is_complete(self, agg):
        from astro_smb_app.views import guidedash as gd

        text = gd.dashboard_text(agg)
        for word in ("导星仪表盘", "RMS", "椭圆", "每张 sub"):
            assert word in text, f"复制出来的信息里没有「{word}」"


class TestSummaryModel:
    """`_summary` 的**数据规则**抽出来了,控件怎么摆各前端自己决定。"""

    def test_shape(self, agg):
        from astro_smb_app.views import guidedash as gd

        m = gd.summary_model(agg)
        assert set(m) == {"badges", "title", "sub", "pills", "groups"}

    def test_groups_match_the_old_ui(self, agg):
        from astro_smb_app.views import guidedash as gd

        names = [name for _g, name, _rows in gd.summary_model(agg)["groups"]]
        assert names == [tr(n) for n in ("导星质量", "帧统计", "趋势与平衡",
                                        "导星光学", "赤道仪 / 几何", "校准",
                                        "与拍摄联动")], names

    def test_kv_rows_are_five_tuples(self, agg):
        """``(标签, 值, 副注, 等宽?, 语义色)`` —— 少一项前端就得猜。"""
        from astro_smb_app.views import guidedash as gd

        for _g, _name, rows in gd.summary_model(agg)["groups"]:
            for row in rows:
                assert len(row) == 5, row

    def test_lost_rate_is_toned(self, agg):
        """丢星率要分档上色 —— 一个光秃秃的百分数看不出好坏。"""
        from astro_smb_app.views import guidedash as gd

        m = gd.summary_model(agg)
        tones = {t for _txt, t in m["badges"]}
        assert tones - {"neutral"}, f"所有徽章都是中性色:{m['badges']}"


class TestPageIntegration:

    def _page(self, qt_app, prepared, agg=None):
        from astro_smb_qt import models
        from astro_smb_qt.shell import Shell

        data, prep = prepared
        page = Shell().page("guiding")
        page.data, page.prep = data, prep
        page.selected = models.default_guide_row(prep["rows"])
        if agg is not None:
            page._dash_cache[prep["groups"][0]["key"]] = agg
        return page

    def test_expanded_group_offers_the_entry(self, qt_app, prepared):
        """入口只在**展开时**出现 —— 收起来的组不该多一行。"""
        from astro_smb_qt import models

        _data, prep = prepared
        gk = prep["groups"][0]["key"]
        closed = models.guiding_rows(prep, set(), set())
        assert not any(r["key"].startswith("d:") for r in closed), (
            "组是收起来的,却多出一行仪表盘")
        opened = models.guiding_rows(prep, {gk}, set())
        assert any(r["key"] == f"d:{gk}" for r in opened), "展开了也没有入口"

    def test_picking_it_switches_the_view(self, qt_app, prepared):
        page = self._page(qt_app, prepared)
        page._render = lambda: None
        gk = prepared[1]["groups"][0]["key"]
        page._pick(f"d:{gk}")
        assert page.dash_key == gk

    def test_picking_a_segment_leaves_the_dashboard(self, qt_app, prepared,
                                                    agg):
        """点回段行要退出仪表盘 —— 否则右栏永远停在聚合视图上。

        **组 key 必须是真的。** 第一版随手写了 `"whatever"`,而
        `_render_dash` 有一条"组找不到就退回段视图"的兜底 —— 那条兜底
        把 `dash_key` 也清了,于是删掉真正那行复位照样绿(反向验证抓到)。
        """
        page = self._page(qt_app, prepared, agg)
        page._render = lambda: None
        page.dash_key = prepared[1]["groups"][0]["key"]
        page._pick("r:0")
        assert page.dash_key is None, "点段行没有退出仪表盘"

    def test_it_renders(self, qt_app, prepared, agg):
        from PySide6.QtWidgets import QLabel

        page = self._page(qt_app, prepared, agg)
        page.dash_key = prepared[1]["groups"][0]["key"]
        page._render()
        texts = [w.text() for w in page.scroll.body.parentWidget()
                 .findChildren(QLabel)]
        for want in ("椭圆", "帧统计", "与拍摄联动"):
            assert any(want in t for t in texts), f"仪表盘里没有「{want}」:{texts[:6]}"

    def test_empty_groups_leave_no_header(self, qt_app, prepared, agg):
        """空分区连标题都不留 —— 一个空标题看着像"没读到"。"""
        src = _src(PAGE, "_render_dash")
        assert "if not rows:" in src and "continue" in src

    def test_back_button_exists(self, qt_app, prepared, agg):
        from PySide6.QtWidgets import QPushButton

        page = self._page(qt_app, prepared, agg)
        page.dash_key = prepared[1]["groups"][0]["key"]
        page._render()
        btns = [b.text() for b in page.scroll.body.parentWidget()
                .findChildren(QPushButton)]
        assert any("段视图" in b for b in btns), btns
        assert any("复制全部信息" in b for b in btns), btns

    def test_window_controls_are_disabled(self):
        """仪表盘是整组聚合,时间窗对它没有意义 —— 不置灰就是"滑了没反应"。"""
        src = _src(PAGE, "_render_dash")
        assert "self.win_combo.setEnabled(False)" in src
        assert "self.slider.setEnabled(False)" in src

    def test_the_heavy_work_is_off_the_gui_thread(self):
        """几万帧的 numpy 聚合,放 GUI 线程会整页卡住好几秒。"""
        src = _src(PAGE, "_start_dash")
        assert "self.bg.run(" in src

    def test_it_is_not_generation_guarded(self):
        """聚合要好几秒,期间任何重画都会 bump 世代 —— 带 gen 的话结果
        回来必被当成"迟到的"整份丢掉,表现是转了半天什么都没有。"""
        src = _src(PAGE, "_start_dash")
        assert "gen=" not in src

    def test_results_are_cached_per_group(self):
        """同一组来回切不该每次都重算。"""
        src = _src(PAGE, "_start_dash")
        assert "_dash_cache[k] = agg" in src
        assert "self._dash_cache.get(key)" in _src(PAGE, "_render_dash")

    def test_a_stale_result_does_not_repaint(self):
        """聚合回来时用户可能已经切到别的组了。"""
        src = _src(PAGE, "_start_dash")
        assert "if self.dash_key == k:" in src
