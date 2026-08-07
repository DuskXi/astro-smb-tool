"""导星质量分析:**从拍摄结果倒推导星好不好**(记录页那块卡)。

上一轮验收把它记成"整块功能缺失"。它不是一个控件,是一整条链路 ——
抽样 sub → 提星 + 板解算 → 与同期 PHD2 交叉判读 → 极轴反解。

那条链路原来只长在**冻结的**老 UI(`astro_smb_gui/_sky3d.py`)里。
冻结政策不许改它,所以这里的做法是把链路搬进 `astro_smb_app/guidequality.py`,
老 UI 一个字节不碰。**判读本身仍在 `astro_smb.guidecheck`** ——
搬的只是编排,阈值和公式一个都没复制。

两条口径写进断言,因为它们错了不会报错、只会给出**看起来很正常的错结论**:

* 单目标极轴反解是**恰定**的(2 方程 2 未知),残差恒为机器零 —— 推翻不了
  任何东西。夜次级联合反解才让残差有意义。
* 靶图的方位约定是**北上、东左**,几何只能来自共享层的
  `polar_plot_geometry`;在页面里重算一遍等于埋一个"图是镜像的"的雷,
  而镜像之后看起来完全正常(变异测试当年就抓到过天球投影的这一条)。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

ROOT = Path(__file__).resolve().parents[1]
QT = ROOT / "astro_smb_qt"
PAGE = QT / "pages" / "records.py"
GQ = ROOT / "astro_smb_app" / "guidequality.py"


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


def _src(path: Path, name: str) -> str:
    node = _fn(path, name)
    body = node.body[1:] if (node.body and isinstance(node.body[0], ast.Expr)
                             and isinstance(node.body[0].value, ast.Constant)
                             ) else node.body
    return "\n".join(ast.unparse(n) for n in body)


class TestTheFrozenUiIsUntouched:
    """搬迁的前提:老 UI 一个字节都不碰。"""

    def test_the_chain_lives_in_the_shared_layer_now(self):
        from astro_smb_app import guidequality as gq

        for name in ("pick_subs", "cached_wcs", "solve_wcs", "build_row",
                     "dither_events", "quality_for", "night_polar", "analyze"):
            assert hasattr(gq, name), f"共享层缺 {name}"

    def test_the_page_does_not_import_the_old_ui(self):
        src = PAGE.read_text(encoding="utf-8")
        assert "astro_smb_gui" not in src, (
            "Qt 页面 import 了老 UI —— 那是冻结模块,而且它带一整套 win32more")


class TestJudgementStaysInTheCoreLibrary:
    """搬的是编排,**阈值与公式一个都不许复制**。"""

    def test_cross_validate_comes_from_guidecheck(self):
        src = _src(GQ, "quality_for")
        assert "cross_validate(" in src
        assert "from astro_smb.guidecheck import" in GQ.read_text(
            encoding="utf-8")

    def test_polar_solution_comes_from_guidecheck(self):
        src = _src(GQ, "night_polar")
        assert "polar_from_runs(" in src and "fit_center_drift(" in src


class TestPickSubs:
    class _E:
        def __init__(self, name, ts, is_dir=False):
            self.name = name
            self.mtime = ts
            self.is_dir = is_dir
            self.share = "s"
            self.path = name
            self.size = 1

    def _subs(self, n, t0=1000.0, step=300.0):
        """**名字故意不按 ASIAIR 的语法拼。**

        `_entry_time` 优先用**文件名时间戳**(与日志同源),拼成真名字的话
        这里给的 mtime 就完全不起作用 —— 第一版的固件正是这么写的,
        窗口过滤把所有帧都滤掉了,看着像 `pick_subs` 坏了。
        文件名优先这条单独一个用例验(见下)。
        """
        return [self._E(f"sub{i:04d}.fit", t0 + i * step) for i in range(n)]

    def test_filename_timestamp_beats_mtime(self):
        """文件名时间是**曝光结束的设备本地时刻**,与日志同源;
        mtime 会被拷贝/同步改掉。"""
        from astro_smb_app import guidequality as gq

        e = self._E("Light_M 8_300.0s_Bin1_20260730-011500_0001.fit", 5.0)
        got = gq._entry_time(e)
        assert got > 1e9, f"退回 mtime 了: {got}"

    def test_unparseable_name_falls_back_to_mtime(self):
        from astro_smb_app import guidequality as gq

        assert gq._entry_time(self._E("random.fit", 4242.0)) == 4242.0

    def test_window_filters_other_nights(self):
        """`Plan/Light/<目标>/` 是**跨夜累积**的 —— 不按窗口过滤会把昨夜的
        证据混进今夜。"""
        from astro_smb_app import guidequality as gq

        subs = self._subs(6, t0=1000.0)
        subs += [self._E("old.fit", 10.0), self._E("future.fit", 999999.0)]
        got = gq.pick_subs(subs, 1000.0, 1000.0 + 5 * 300.0)
        assert all(e.name not in ("old.fit", "future.fit") for e in got)
        assert len(got) == 6

    def test_thinning_keeps_both_ends(self):
        """抽稀取**首尾 + 均匀间隔**:只取前 N 张正好把整夜的漂移信息丢光。"""
        from astro_smb_app import guidequality as gq

        subs = self._subs(40)
        got = gq.pick_subs(subs, 1000.0, 1000.0 + 39 * 300.0, limit=5)
        assert len(got) == 5
        assert got[0] is subs[0], "没取到第一张"
        assert got[-1] is subs[-1], "没取到最后一张 —— 漂移信息全丢"

    def test_thumbnails_are_skipped(self):
        from astro_smb_app import guidequality as gq

        subs = self._subs(3) + [self._E("x_thn.jpg", 1100.0)]
        got = gq.pick_subs(subs, 1000.0, 2000.0)
        assert all(not e.name.endswith("_thn.jpg") for e in got)

    def test_directories_are_skipped(self):
        from astro_smb_app import guidequality as gq

        subs = self._subs(2) + [self._E("sub", 1100.0, is_dir=True)]
        assert len(gq.pick_subs(subs, 1000.0, 2000.0)) == 2


class TestWcsPayloadRoundTrip:
    def _wcs(self):
        from astro_smb.wcs import TanWcs

        return TanWcs((10.0, 20.0), (100.0, 80.0),
                      [[-1e-4, 0.0], [0.0, 1e-4]])

    def test_round_trip(self):
        from astro_smb_app import guidequality as gq

        got = gq.wcs_from_payload(gq.wcs_to_payload(self._wcs(), 200, 160))
        assert got is not None
        w, width, height = got
        assert (width, height) == (200, 160)
        assert abs(w.crval[0] - 10.0) < 1e-9

    def test_version_mismatch_is_a_miss(self):
        """payload 版本变了要**整份当未命中**,而不是喂进缺字段的对象。"""
        from astro_smb_app import guidequality as gq

        d = gq.wcs_to_payload(self._wcs(), 200, 160)
        d["v"] = gq.WCS_CACHE_V + 1
        assert gq.wcs_from_payload(d) is None

    def test_failed_payload_is_a_miss(self):
        from astro_smb_app import guidequality as gq

        assert gq.wcs_from_payload({"v": gq.WCS_CACHE_V, "ok": False}) is None

    def test_it_shares_the_cache_with_the_3d_page(self):
        """同一张 sub 解算一次就够 —— 一次要拉 50MB 原图。
        种类名与版本必须和老 UI 那侧对得上,否则两边互相看不见。"""
        import re

        from astro_smb_app import guidequality as gq

        old = (ROOT / "astro_smb_gui" / "_sky3d.py").read_text(encoding="utf-8")
        kind = re.search(r'FOOT_KIND = "([^"]+)"', old).group(1)
        ver = int(re.search(r"FOOT_CACHE_V = (\d+)", old).group(1))
        assert gq.WCS_KIND == kind, f"缓存种类名对不上: {gq.WCS_KIND} vs {kind}"
        assert gq.WCS_CACHE_V == ver, f"payload 版本对不上: {gq.WCS_CACHE_V} vs {ver}"


class TestBuildRow:
    def test_it_does_not_compute_a_footprint_ring(self):
        """足迹环是 3D 天球画视场框要的,质量判读用不上 ——
        每张算一圈边界采样是白花的时间。"""
        src = _src(GQ, "build_row")
        assert "ring" not in src

    def test_bad_payload_gives_none(self):
        from astro_smb_app import guidequality as gq

        class _E:
            name = "x.fit"
            share = "s"
            path = "p"
            mtime = 1.0

        assert gq.build_row(_E(), {"v": 0}) is None


class TestOrchestration:
    def test_it_gives_a_human_reason_for_each_failure(self):
        """"没结论"有三种,用户要采取的行动完全不同:
        缺星表 → 去下星表;帧数不够 → 换个目标;证据不足 → 多拍几张。"""
        src = _src(GQ, "analyze")
        for word in ("两张", "星表", "证据不足"):
            assert word in src, f"失败原因里没有「{word}」这一种"

    def test_it_reports_progress(self):
        """动辄几十秒,没有进度用户会以为卡死了。"""
        src = _src(GQ, "analyze")
        assert "on_progress" in src and "正在分析" in src

    def test_it_honours_cancel(self):
        src = _src(GQ, "analyze")
        assert src.count("cancel.is_set()") >= 2, (
            "取消只在循环外查了一次 —— 一张 sub 要解算好几秒,"
            "点了停止得等一整张")

    def test_it_solves_even_when_the_header_has_wcs(self):
        """**主镜 FWHM/椭率/方向只有本机提星才拿得到**,而那正是
        "从拍摄结果倒推"的关键证据;头里的 WCS 只给位置。"""
        src = _src(GQ, "analyze")
        assert "star_fwhm_px" in src and "has_shape" in src

    def test_solving_is_serialised(self):
        """一次要拉 50MB 原图 + 吃满 CPU,两个同时跑只会互相拖慢。"""
        assert "_solve_lock" in GQ.read_text(encoding="utf-8")
        assert "with _solve_lock:" in _src(GQ, "solve_wcs")

    def test_target_exposure_comes_from_the_real_attribute(self):
        """`TargetRun` 上**没有** `total_exposure_s` —— 写它的话
        `getattr` 默认值一兜,平均曝光恒为 0,配不上曝光的帧会退化成
        0.1 秒的窗口,而且不报错。"""
        src = _src(GQ, "_target_of")
        assert "integration_by_filter()" in src
        assert "total_exposure_s" not in src


class TestPolarIsHonestAboutItself:

    def test_single_target_is_not_falsifiable(self):
        """单目标恰定 ⇒ 残差恒为机器零。这条告白必须留在文案里。"""
        src = _src(GQ, "night_polar")
        assert "len(samples) < 2" in src, "一个目标也敢给联合反解"

    def test_page_marks_falsifiability(self):
        """**判据要读结构化字段,不许去 findings 里搜「恰定」两个字。**

        这条原来断言的是 ``"恰定" in src`` —— 也就是把**那个反模式本身**
        钉成了正确行为。`findings` 是给用户看的人话,会被翻译;拿它当判据
        一翻就静默变成"这个极轴数字可信",而它恰恰是推翻不了的那种。
        判据现在走 `CrossCheck.polar_falsifiable`。
        """
        src = _src(PAGE, "_render_polar")      # ast.unparse:没有注释也没有文档串
        assert "polar_falsifiable" in src, "没读结构化字段"
        assert "findings" not in src, "还在拿 findings 的文本反推可证伪性"

    def test_advice_comes_from_the_shared_layer(self):
        """"该往哪拧"的符号在共享层写死 —— 方向记反了要白折腾一晚上。"""
        src = _src(PAGE, "_render_polar")
        assert "rv.polar_advice(" in src

    def test_geometry_comes_from_the_shared_layer(self):
        """方位约定(北上东左)只存在于共享层那一处。"""
        src = _src(PAGE, "_render_polar")
        assert "rv.polar_plot_geometry(" in src

    def test_no_polar_means_no_plot(self, qt_app):
        """空着一个画好的靶环比不画更容易被误读成"极轴没问题"。"""
        src = _src(PAGE, "_render_polar")
        assert "if polar is None:" in src and "return" in src

    def test_the_marker_is_on_the_east_left_side(self, qt_app):
        """**方位向东为正时点子往左跑。** 整张图镜像之后看起来完全正常 ——
        变异测试当年就是在天球投影的东西方向上抓到同款。"""
        from astro_smb_app.views import records as rv

        class _P:
            az = 0.02       # 度,向东
            alt = 0.0
            total_arcmin = 1.2

        geo = rv.polar_plot_geometry(_P(), 132.0)
        cx = geo["center"][0]
        assert geo["marker"][0] < cx, "偏东画到了右边 —— 图是镜像的"

    def test_higher_pole_goes_up(self, qt_app):
        from astro_smb_app.views import records as rv

        class _P:
            az = 0.0
            alt = 0.02      # 极轴抬高了
            total_arcmin = 1.2

        geo = rv.polar_plot_geometry(_P(), 132.0)
        assert geo["marker"][1] < geo["center"][1], "偏高画到了下边"


class TestCardRendering:

    def _quality(self):
        from astro_smb.guidecheck import polar_from_runs

        chk = polar_from_runs([(-30.0, 20.0, 0.010, -0.004),
                               (40.0, -10.0, -0.006, 0.009)], 30.0)

        class _Q:
            verdict = "drift"
            confidence = "medium"
            headline = "主镜有明显漂移"
            findings = ["证据 A", "证据 B"]
            polar = chk.polar
            polar_cond = chk.cond

        return _Q()

    def _page(self, qt_app):
        from astro_smb_qt.shell import Shell

        return Shell().page("records")

    def test_card_appears_before_any_analysis(self, qt_app):
        """没分析过也要有卡 + 「开始分析」按钮,否则用户根本找不到入口。"""
        from PySide6.QtWidgets import QLabel, QPushButton

        page = self._page(qt_app)
        page.detail.clear()
        page._render_quality(page.detail.body, {"target": "M 8"})
        texts = [w.text() for w in page.detail.body.parentWidget()
                 .findChildren(QLabel)]
        assert any("尚未分析" in t for t in texts), texts
        btns = [b.text() for b in page.detail.body.parentWidget()
                .findChildren(QPushButton)]
        assert any("开始分析" in b for b in btns), btns

    def test_findings_are_listed(self, qt_app):
        from PySide6.QtWidgets import QLabel

        page = self._page(qt_app)
        page._quality["M 8"] = self._quality()
        page.detail.clear()
        page._render_quality(page.detail.body, {"target": "M 8"})
        texts = [w.text() for w in page.detail.body.parentWidget()
                 .findChildren(QLabel)]
        assert any("主镜有明显漂移" in t for t in texts), texts
        assert any("证据 A" in t for t in texts), "findings 没列出来"
        assert any("可信度" in t for t in texts)

    def test_verdict_drives_the_colour(self, qt_app):
        """漂移=红、过冲=琥珀、好=绿。全一个色 = 结论那一层信息没了。"""
        src = _src(PAGE, "_render_quality")
        assert "'good': 'ok'" in src and "'drift': 'bad'" in src
        assert "'overguide': 'warn'" in src

    def test_button_toggles_to_stop_while_busy(self, qt_app):
        from PySide6.QtWidgets import QPushButton

        page = self._page(qt_app)
        page._quality_state["M 8"] = {"busy": True, "text": "正在分析 1/8"}
        page.detail.clear()
        page._render_quality(page.detail.body, {"target": "M 8"})
        btns = [b.text() for b in page.detail.body.parentWidget()
                .findChildren(QPushButton)]
        assert any("停止分析" in b for b in btns), btns

    def test_error_text_is_shown_in_bad_tone(self, qt_app):
        from PySide6.QtWidgets import QLabel

        page = self._page(qt_app)
        page._quality_state["M 8"] = {
            "busy": False, "text": "缺少可用星表", "error": True}
        page.detail.clear()
        page._render_quality(page.detail.body, {"target": "M 8"})
        texts = [w.text() for w in page.detail.body.parentWidget()
                 .findChildren(QLabel)]
        assert any("缺少可用星表" in t for t in texts), texts

    def test_the_long_job_is_not_generation_guarded(self):
        """**不能给 gen。** 这一趟动辄几十秒,期间任何一次重画都会 bump
        世代,带 gen 的话结果回来必被当成"迟到的"整份丢掉 ——
        表现是转了半天什么都没有,而且不报错。"""
        src = _src(PAGE, "_start_quality")
        assert "gen=" not in src, "给了 gen —— 结果会被自己的世代守卫丢掉"

    def test_results_are_keyed_by_target_name(self):
        """按**目标名**做键,不是 `id(run)` —— 刷新一次日志对象全换,
        用对象身份做键等于每次刷新都白算。"""
        src = _src(PAGE, "_start_quality")
        assert "getattr(run, 'target'" in src
        assert "id(run)" not in src

    def test_stop_actually_cancels(self):
        src = _src(PAGE, "_stop_quality")
        assert ".cancel()" in src
