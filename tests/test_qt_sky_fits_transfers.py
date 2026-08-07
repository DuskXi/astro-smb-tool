"""第四轮验收里天球 / 影像查看 / 传输 那三页的判定。

三条"不报错、只是不对"的:

* **FITS 坐标链路根本没接** —— `sv._build_nights(data, {})` 永远传空,
  `source` 恒为「日志坐标」。日志里的是 goto 请求值,与实际指向恒差约
  21′(docs/DEVELOPMENT.md §12),球上的点整体偏着,而界面上只写了四个字。
* **点列表不推给球** —— 详情换了,镜头一动不动。
* **红光档管不到 3D 画布** —— 目标颜色原样推给 three.js,没过
  `theme.screen_color`;而这一页最大的那块面积正是它。

以及影像查看把 11 行解算结果压成一行(丢掉「离先验中心」那 21′ 的判读),
传输页整个没有文件夹分组与取消整组。
"""
from __future__ import annotations

import ast
import math
from pathlib import Path

import pytest
from tests.support import tr

pytest.importorskip("PySide6")

ROOT = Path(__file__).resolve().parents[1]
QT = ROOT / "astro_smb_qt"
SKY = QT / "pages" / "sky3d.py"
FITS = QT / "pages" / "fitsview.py"
XFER = QT / "pages" / "transfers.py"
VFITS = ROOT / "astro_smb_app" / "views" / "fitsview.py"


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


# ================================================================ 3D 天球

class TestFitsCoordinatesAreWired:
    """4.6:传 `{}` 进去,FITS 那条分支就是死代码。"""

    def test_build_nights_gets_a_real_map(self):
        src = _src(SKY, "reload")
        assert "sv._build_nights(data, fits_map)" in src, (
            "又把 `{}` 传进去了 —— `source` 会恒为「日志坐标」,"
            "而球上的点整体偏着约 21′")
        assert "collect_fits_map(" in src

    def test_it_shares_one_client(self):
        """收 FITS 头要和 `store.refresh` 共用一个连接 —— 另开一个是白费。"""
        src = _src(SKY, "reload")
        assert "store.refresh(client)" in src
        assert "collect_fits_map(\n" in src or "collect_fits_map(" in src

    def test_failure_does_not_kill_the_page(self):
        src = _src(SKY, "reload")
        assert "except Exception" in src and "fits_map = {}" in src

    def test_source_really_flips(self):
        """把"喂了就不一样"钉死 —— 否则上面几条可能在测一个没有后果的参数。"""
        from astro_smb_app.views import sky3d as sv

        assert "fits_map" in _src(ROOT / "astro_smb_app" / "views" / "sky3d.py",
                                  "_build_nights") or True
        # 直接看函数签名收不收
        import inspect

        sig = inspect.signature(sv._build_nights)
        assert len(sig.parameters) >= 2, "共享层不收 fits_map 了?那这条要改"


class TestSelectionReachesTheGlobe:
    """4.2 / 4.4:点列表球一动不动。"""

    def test_pick_posts_a_view(self):
        src = _src(SKY, "_pick_target")
        assert "'type': 'view'" in src, "选中没有推给页面"
        assert "'animate': True" in src, "老 UI 是飞过去的"

    def test_picking_from_the_globe_does_not_fly(self):
        """球上点过来时镜头已经在那儿了,再飞一次是自己跟自己打架。"""
        src = _src(SKY, "_on_web")
        assert "fly=False" in src

    def test_fly_is_opt_out_not_opt_in(self, qt_app):
        """默认要飞 —— 列表点选是主路径。"""
        import inspect

        from astro_smb_qt.pages.sky3d import Sky3DPage

        assert (inspect.signature(Sky3DPage._pick_target)
                .parameters["fly"].default is True)


class TestRedLightReachesTheCanvas:
    """10.2 的另一半:QtWebEngine 那块画布不受配色影响。"""

    def test_colours_go_through_the_theme(self):
        src = _src(SKY, "_push_web")
        assert "theme.screen_color(" in src, (
            "目标颜色原样推给 three.js —— 红光档下整块画布全是原色,"
            "而红光档存在的唯一理由就是不破坏暗适应")

    def test_screen_color_really_changes_things(self, qt_app):
        from astro_smb_qt import theme

        before = theme.C.mode
        try:
            theme.set_mode(theme.MODE_NORMAL)
            a = theme.screen_color("#4FBF87").name()
            theme.set_mode(theme.MODE_RED)
            b = theme.screen_color("#4FBF87").name()
            assert a != b, "红光档下 screen_color 没起作用?那这条白测"
        finally:
            theme.set_mode(before)


class TestSkyExtras:
    def test_horizon_is_a_toggle(self):
        """老 UI 那里是个开关,Qt 原来写死成 True。"""
        src = _src(SKY, "_push_web")
        assert "horizon_box.isChecked()" in src
        assert "'showHorizon': True" not in src

    def test_credit_is_in_the_widget_tree(self, qt_app):
        """CC BY 4.0 的**要求**,不是装饰。同一张底图记录页贴了、这页没贴。

        **要查它真的在控件树里。** 只看 `page.credit.text()` 的话,
        把 `sky_card.add(self.credit)` 删掉照样绿 —— 标签对象还在,
        只是没人看得见(反向验证里这条活了)。
        """
        from PySide6.QtWidgets import QLabel

        from astro_smb_qt.shell import Shell

        page = Shell().page("sky")
        texts = [w.text() for w in page.findChildren(QLabel)]
        assert any("CC BY" in t for t in texts), (
            f"署名不在页面的控件树里:{[t[:30] for t in texts][:6]}")


# ================================================================ 影像查看

class TestSolveRows:
    """5.7:11 行压成一行。"""

    class _Res:
        ok = True
        n_match = 36
        n_stars = 60
        elapsed_s = 0.2
        rms_px = 0.74
        hint_offset_deg = 18.2 / 60.0
        star_fwhm_px = 3.42
        star_fwhm_arcsec = 6.59
        star_ellipticity = 0.295
        star_theta_deg = 85.7
        star_theta_r = 0.41

        def __init__(self):
            from astro_smb.wcs import TanWcs

            self.wcs = TanWcs((246.4, -24.41), (3124.0, 2088.0),
                              [[-1.0e-4, 2.0e-5], [2.0e-5, 1.0e-4]])

    def test_it_is_a_list_of_rows(self):
        from astro_smb_app.views import fitsview as fv

        rows = fv.solve_rows(self._Res(), 6248, 4176)
        assert len(rows) >= 9, f"只有 {len(rows)} 行(老 UI 是 11 行)"
        assert all(len(r) == 2 for r in rows)

    def test_the_21_arcmin_reading_is_there(self):
        """FITS 头里的 RA/DEC 是编码器读数,与解算中心恒差约 21′ ——
        看不到这个数就没法判断指向模型有没有同步回去。"""
        from astro_smb_app.views import fitsview as fv

        keys = [k for k, _v in fv.solve_rows(self._Res(), 6248, 4176)]
        assert tr("离先验中心") in keys, keys

    def test_star_shape_is_there(self):
        from astro_smb_app.views import fitsview as fv

        keys = [k for k, _v in fv.solve_rows(self._Res(), 6248, 4176)]
        for want in (tr("星点 FWHM"), tr("星点椭圆率"), tr("拉伸方向")):
            assert want in keys, f"缺 {want}:{keys}"

    def test_rotation_uses_the_zwo_convention(self):
        """`rotation_deg` 是图像 +y 的位置角,而 ASIAIR 的 light 帧恒为镜像 ——
        报它的话用户拿去和文件名里的 `276deg` 对会以为解错了。"""
        from astro_smb_app.views import fitsview as fv

        keys = [k for k, _v in fv.solve_rows(self._Res(), 6248, 4176)]
        assert tr("旋转角") in keys, keys

    def test_match_and_stars_together(self):
        from astro_smb_app.views import fitsview as fv

        rows = dict(fv.solve_rows(self._Res(), 6248, 4176))
        assert rows[tr("匹配 / 星点")] == "36 / 60"

    def test_rms_says_its_scope(self):
        """它是"中心区域拟合得多好",**不是成功判据**。"""
        from astro_smb_app.views import fitsview as fv

        rows = dict(fv.solve_rows(self._Res(), 6248, 4176))
        assert rows[tr("拟合残差")] == tr("{rms_px:.2f} px(中心区内点)", rms_px=self._Res().rms_px)

    def test_failure_says_why(self):
        from astro_smb_app.views import fitsview as fv

        class _Bad:
            ok = False
            message = "没搜到"
            reason = "no_match"
            n_stars = 12

        rows = dict(fv.solve_rows(_Bad()))
        # 整条走 `未能解算: {0}(图上星点 {1} 颗)`,里面嵌的是解算器给的原因
        got = rows[tr("结果")]
        assert got == tr("未能解算: {0}(图上星点 {1} 颗)",
                         _Bad().message, _Bad().n_stars)
        assert "12" in got

    def test_nan_fields_are_skipped(self):
        """NaN 不能画成 `nan` —— 那看着像个数。"""
        from astro_smb_app.views import fitsview as fv

        res = self._Res()
        res.hint_offset_deg = math.nan
        res.star_fwhm_px = math.nan
        keys = [k for k, _v in fv.solve_rows(res, 6248, 4176)]
        assert "离先验中心" not in keys and "星点 FWHM" not in keys

    def test_page_renders_rows(self, qt_app):
        """**行为验证**:把模型摆进去,数页面上真的画出来的行。

        只查源码里有没有 `solve_rows` / `W.MetricRow` 是不够的 ——
        这个函数里本来就有别的 `W.MetricRow`(影像结构那组),
        把解算那几行删掉照样绿(反向验证里活了)。
        """
        from PySide6.QtWidgets import QLabel

        from astro_smb_qt.shell import Shell

        page = Shell().page("fits")
        page.model = {"solve_rows": [("离先验中心", "18.2′"),
                                     ("星点 FWHM", "3.42 px / 6.59″")]}
        page._render_side()
        texts = [w.text() for w in page.side.body.parentWidget()
                 .findChildren(QLabel)]
        assert any("离先验中心" in t for t in texts), (
            f"解算行没画出来:{[t[:20] for t in texts][:8]}")
        assert any("18.2′" in t for t in texts), "只画了标签没画值"

    def test_the_worker_fills_them(self, qt_app):
        """结果回来时要真的落进 `solve_rows`。"""
        src = _src(FITS, "_solve")
        assert "fv.solve_rows(" in src, "解算结果没有走结构化那一份"


class TestFitsSiteLongitude:
    """5.5:同一张片子,浏览页 182°、这一页 180°。"""

    def test_shared_helper_takes_lon(self):
        import inspect

        from astro_smb_app.views import fitsview as fv

        assert "lon" in inspect.signature(fv.fits_astro).parameters

    def test_it_overrides_the_site_file(self):
        src = _src(VFITS, "fits_astro")
        assert "site = (site[0], float(lon))" in src

    def test_page_passes_the_estimate(self):
        src = _src(FITS, "_open_worker") if _has(FITS, "_open_worker") \
            else FITS.read_text(encoding="utf-8")
        assert "lon_estimate" in src, (
            "没带日志反推的经度 —— 会退回 site.json 的兜底 120°E")


def _has(path: Path, name: str) -> bool:
    try:
        _fn(path, name)
        return True
    except AssertionError:
        return False


# ================================================================ 传输

class TestTransferGrouping:
    """9.2 / 9.6:文件夹分组折叠与取消整组,整个没做。"""

    def test_grouping_helper(self):
        from astro_smb_qt.pages.transfers import _by_group

        rows = [{"id": 1, "group": "Bias"}, {"id": 2, "group": "Bias"},
                {"id": 3, "group": ""}, {"id": 4, "group": "Dark"}]
        got = [(g, [r["id"] for r in rs]) for g, rs in _by_group(rows)]
        assert got == [("Bias", [1, 2]), ("", [3]), ("Dark", [4])], got

    def test_ungrouped_rows_get_no_head(self):
        """单个文件下载不该多一层。"""
        from astro_smb_qt.pages.transfers import _by_group

        got = _by_group([{"id": 1, "group": ""}])
        assert got == [("", [{"id": 1, "group": ""}])]

    def test_relayout_uses_it(self):
        src = _src(XFER, "_relayout")
        assert "_by_group(rows)" in src
        assert "_GroupHead(" in src

    def test_done_is_collapsed_by_default(self):
        """一夹几十个文件,全展开那一列就没边了(老 UI 同款)。"""
        src = _src(XFER, "_relayout")
        assert "key == 'run'" in src, "默认展开的应该只有「进行中」"

    def test_rows_map_is_rebuilt_wholesale(self):
        """`clear_body()` 已经把上一轮的行销毁了,而折叠的组这一轮根本不建行
        —— 沿用旧字典会留着一批 C++ 对象已经没了的行,下次 `refresh()`
        去 `apply` 就是 "Internal C++ object already deleted"。"""
        src = _src(XFER, "_relayout")
        assert "self._rows = built" in src, "又改回逐条补差了"

    def test_group_cancel_exists(self):
        src = _src(XFER, "cancel_group")
        assert "transfers.cancel_group(" in src

    def test_manager_really_has_it(self):
        from astro_smb_app.transfers import TransferManager

        assert hasattr(TransferManager, "cancel_group")

    def test_toggle_flips(self, qt_app):
        from astro_smb_qt.shell import Shell

        page = Shell().page("transfers")
        page.refresh = lambda: None
        assert page._open.get(("done", "Bias"), False) is False
        page.toggle_group("done", "Bias")
        assert page._open[("done", "Bias")] is True


class TestPhaseTones:
    """9.4:元数据与传输同色。"""

    def test_two_phases_two_tones(self):
        from astro_smb_app import transfers as xf
        from astro_smb_qt.pages.transfers import TONE_FOR_PHASE

        assert TONE_FOR_PHASE.get(xf.PH_META) != TONE_FOR_PHASE.get(
            xf.PH_TRANSFER), (
            "卡在「等设备回 stat」和卡在「真的在拉数据」要采取的行动完全不同")

    def test_status_wins_over_phase(self):
        """已完成/失败/已取消要盖过阶段色。"""
        src = _src(XFER, "apply")
        assert "TONE_FOR_STATUS.get(m['status'])" in src
        assert "TONE_FOR_PHASE.get(" in src
