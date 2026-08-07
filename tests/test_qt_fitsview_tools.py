"""影像查看的工具链 + 星点叠加 + 整夹下载入队 —— 验收文件最后三块。

**为什么这些不是"锦上添花"。** 一张 6248×4176 的片子被定死缩到 820px 宽,
星点是圆是扁根本看不出来 —— 而那是判断导星好坏的直接证据(整个导星质量
分析卡就建立在星点形状上)。缩放、1:1、像素读数,是"能不能看"的问题。

拉伸参数同理:三档拉伸各有自己的旋钮,不给的话只能看默认参数下的样子;
而"背景压死了还是烧顶了"要拖着看才知道。

整夹下载那条更直白:浏览页原来的提示语指向"到传输页发起",而**传输页
根本没有这个入口** —— 一句指向不存在功能的提示。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from tests.support import tr

pytest.importorskip("PySide6")

ROOT = Path(__file__).resolve().parents[1]
QT = ROOT / "astro_smb_qt"
FITS = QT / "pages" / "fitsview.py"
BROWSE = QT / "pages" / "browser.py"
WIDGETS = QT / "widgets.py"


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


class TestZoomView:
    """缩放 / 平移 / 像素坐标反查。"""

    def _view(self, qt_app, w=400, h=300, iw=1000, ih=800):
        from PySide6.QtGui import QPixmap

        from astro_smb_qt import widgets as W

        v = W.ZoomView()
        v.resize(w, h)
        pix = QPixmap(iw, ih)
        pix.fill()
        v._src = pix
        v.fit()
        return v

    def test_fit_shows_the_whole_image(self, qt_app):
        v = self._view(qt_app)
        assert v.zoom == pytest.approx(min(400 / 1000, 300 / 800), rel=1e-6)

    def test_actual_size_is_one_to_one(self, qt_app):
        v = self._view(qt_app)
        v.actual_size()
        assert v.zoom == pytest.approx(1.0)

    def test_zoom_is_clamped(self, qt_app):
        from astro_smb_qt import widgets as W

        v = self._view(qt_app)
        v.set_zoom(1e9)
        assert v.zoom == pytest.approx(W.ZoomView.MAX_ZOOM)
        v.set_zoom(1e-9)
        assert v.zoom == pytest.approx(W.ZoomView.MIN_ZOOM)

    def test_anchor_stays_put(self, qt_app):
        """**定点缩放**:滚轮放大时鼠标底下那一点要不动 ——
        不然想看的地方越缩越远。"""
        v = self._view(qt_app)
        anchor = (137.0, 91.0)
        before = v.to_image(*anchor)
        v.set_zoom(v.zoom * 3.0, anchor)
        after = v.to_image(*anchor)
        assert abs(after[0] - before[0]) <= 1, (before, after)
        assert abs(after[1] - before[1]) <= 1, (before, after)

    def test_coordinates_round_trip(self, qt_app):
        """`to_image` 必须和 `paintEvent` 用同一对 zoom/offset ——
        各写一份的表现是"读数指向别的像素",而那看不出来。"""
        v = self._view(qt_app)
        v.set_zoom(2.0, (0.0, 0.0))
        ix, iy = 123, 45
        sx = v._ox + ix * v.zoom
        sy = v._oy + iy * v.zoom
        assert v.to_image(sx + 0.5, sy + 0.5) == (ix, iy)

    def test_outside_gives_minus_one(self, qt_app):
        v = self._view(qt_app)
        assert v.to_image(-50.0, -50.0) == (-1, -1)

    def test_stars_are_optional(self, qt_app):
        v = self._view(qt_app)
        assert not v._show_stars
        v.set_stars([(10.0, 20.0)], show=True)
        assert v._show_stars
        v.show_stars(False)
        assert not v._show_stars

    def test_it_takes_what_the_real_caller_gives(self, qt_app):
        """**真实调用方给的是 numpy 数组,不是 list。**

        这条是补票:原来的用例喂 Python 列表,而板解算返回的
        `matched_xy` 是 numpy 数组 —— `points or ()` 对元素数 >1 的数组
        直接抛 `ValueError: truth value ... is ambiguous`。
        解算成功几乎总能匹配到几十颗星,所以那是**必现**,不是边角。

        独立验收才发现:界面上按钮恢复可点(看着像解算完了),
        面板却永远冻在「正在解算…」—— 因为异常正抛在两者之间。
        **测试喂的形状不是调用方的形状,等于没测。**
        """
        np = pytest.importorskip("numpy")

        v = self._view(qt_app)
        v.set_stars(np.array([[10.0, 20.0], [30.0, 40.0], [50.0, 60.0]]),
                    show=True)
        assert len(v._stars) == 3
        assert v._show_stars

    def test_empty_numpy_is_not_stars(self, qt_app):
        np = pytest.importorskip("numpy")

        v = self._view(qt_app)
        v.set_stars(np.empty((0, 2)), show=True)
        assert v._stars == []
        assert not v._show_stars

    def test_none_is_fine(self, qt_app):
        v = self._view(qt_app)
        v.set_stars(None, show=True)
        assert v._stars == []

    def test_no_stars_means_no_overlay(self, qt_app):
        """没有星点时勾选也不该"打开一个空图层"。"""
        v = self._view(qt_app)
        v.set_stars([], show=True)
        assert not v._show_stars

    def test_it_paints(self, qt_app):
        """真画一遍 —— `paintEvent` 里少个名字会被 Qt 吞掉,只是白着。"""
        v = self._view(qt_app)
        v.set_stars([(100.0, 100.0), (200.0, 300.0)])
        img = v.grab().toImage()
        assert not img.isNull()

    def test_nearest_when_magnified(self):
        """放到 1:1 以上要用近邻 —— 平滑插值会把单个像素糊成一团,
        而放这么大就是为了看单个星点的形状。"""
        src = WIDGETS.read_text(encoding="utf-8")
        at = src.index("class ZoomView")
        body = src[at:src.index("\n\ndef _red_tint", at)]
        assert "SmoothPixmapTransform, self._zoom < 1.0" in body


class TestStretchKnobs:
    """每档拉伸的参数滑杆 —— 老 UI 有,这边原来一个都没有。"""

    def test_each_mode_shows_only_its_own(self, qt_app):
        """在 STF 档下拖 asinh 强度既不改画面也不失效缓存
        (`fingerprint` 按档分字段),摆着只会让人以为它坏了。"""
        from astro_smb_qt.shell import Shell

        page = Shell().page("fits")

        def visible():
            return {k for k, (h, *_r) in page._knobs.items() if not h.isHidden()}

        assert visible() == {"shadows_clipping", "target_background"}
        page._set_mode(1)
        assert visible() == {"shadows_clipping", "asinh_a"}
        page._set_mode(2)
        assert visible() == {"lo_pct", "hi_pct"}

    def test_knobs_match_the_dataclass(self):
        """滑杆的键必须是 `StretchParams` 上真有的字段 ——
        写错一个名字的话 `dataclasses.replace` 会抛,而那是在拖动时才炸。"""
        from astro_smb.fitsimage import StretchParams
        from astro_smb_qt.pages.fitsview import _SLIDERS

        fields = set(StretchParams.__dataclass_fields__)
        assert set(_SLIDERS) <= fields, set(_SLIDERS) - fields

    def test_dragging_updates_params(self, qt_app):
        from astro_smb_qt.shell import Shell

        page = Shell().page("fits")
        page._restretch = lambda: None
        page._set_knob("target_background", 40)
        assert page._params.target_background == pytest.approx(0.40)

    def test_link_toggle(self, qt_app):
        from astro_smb_qt.shell import Shell

        page = Shell().page("fits")
        page._restretch = lambda: None
        page._set_linked(True)
        assert page._params.linked is True

    def test_restretch_does_not_redownload(self):
        """**只重拉伸。** 重下一次 50 MB 是十几秒,拖滑杆时完全不可接受。"""
        src = _src(FITS, "_restretch")
        assert "download_file" not in src and "with_client" not in src
        assert "stretch(img.rgb" in src

    def test_mode_change_restretches(self, qt_app):
        """**行为验证。** 只查 `"self._restretch()" in src` 挡不住
        `if False:` —— 调用那行文本还在(反向验证里这条活了)。"""
        from astro_smb_qt.shell import Shell

        page = Shell().page("fits")
        page._img = object()
        hits = []
        page._restretch = lambda: hits.append(1)
        page.reload = lambda: hits.append("reload")
        page._set_mode(1)
        assert hits == [1], f"换档没有重拉伸(而是 {hits})"

    def test_mode_change_without_an_image_reloads(self, qt_app):
        """还没打开过图时只能走完整加载 —— 没有可重拉伸的东西。"""
        from astro_smb_qt.shell import Shell

        page = Shell().page("fits")
        page._img = None
        page.path = "a.fit"
        hits = []
        page.reload = lambda: hits.append("reload")
        page._restretch = lambda: hits.append("restretch")
        page._set_mode(2)
        assert hits == ["reload"], hits

    def test_the_linear_image_is_kept(self):
        src = _src(FITS, "_apply")
        assert "self._img = m.get('img')" in src, (
            "线性图没留下来 —— 那重拉伸就只能重下")


class TestZoomToolbarAndReadout:

    def test_buttons_exist(self, qt_app):
        from PySide6.QtWidgets import QPushButton

        from astro_smb_qt.shell import Shell

        page = Shell().page("fits")
        labels = {b.text() for b in page.findChildren(QPushButton)}
        for want in (tr("适应窗口"), "1:1", "−", "+", tr("另存为 PNG"),
                     tr("重新加载"), tr("在浏览页中显示")):
            assert want in labels, f"缺按钮「{want}」:{sorted(labels)}"

    def test_readout_reports_raw_values(self, qt_app):
        """**报原始线性值**,不是拉伸后的显示值 ——
        拉伸是为了看得见,判读要看原始数。"""
        import numpy as np

        from astro_smb_qt.shell import Shell

        page = Shell().page("fits")

        class _Img:
            rgb = np.arange(12, dtype=np.float32).reshape(3, 4)
            unit = "ADU"

        page._img = _Img()
        page._on_hover(2, 1)
        txt = page.pix_label.text()
        assert "(2, 1)" in txt and "6" in txt, txt
        assert "ADU" in txt

    def test_readout_clears_off_image(self, qt_app):
        from astro_smb_qt.shell import Shell

        page = Shell().page("fits")
        page._on_hover(-1, -1)
        assert "鼠标" in page.pix_label.text()

    def test_zoom_label_follows(self, qt_app):
        from astro_smb_qt.shell import Shell

        page = Shell().page("fits")
        page._on_zoom(2.5)
        assert page.zoom_label.text() == "250%"


class TestHistogramToggleAndCopy:

    def test_both_histograms_are_computed(self):
        src = _src(FITS, "reload")
        assert "'hist': _histogram(img)" in src
        assert "'hist_after': _histogram_u8(rgb8)" in src, (
            "只有拉伸前那份 —— 看不出「显示得对不对」")

    def test_u8_histogram_is_normalised(self):
        import numpy as np

        from astro_smb_qt.pages.fitsview import _histogram_u8

        got = _histogram_u8(np.full((10, 10), 128, dtype=np.uint8))
        assert got and max(got[0]) == pytest.approx(1.0)

    def test_toggle_flips(self, qt_app):
        from astro_smb_qt.shell import Shell

        page = Shell().page("fits")
        page._render_side = lambda: None
        before = page._show_before
        page._toggle_hist()
        assert page._show_before is not before

    def test_copy_all_includes_every_section(self, qt_app):
        from PySide6.QtWidgets import QApplication

        from astro_smb_qt.shell import Shell

        page = Shell().page("fits")
        page.share, page.path = "EMMC Images", "Plan\\Light\\a.fit"
        page.model = {"astro": {"title": "IC 4603", "sub": "亮场",
                                "rows": [("高度角", "35°", "", None)]},
                      "structure": [("宽", "6248")],
                      "solve_rows": [("离先验中心", "18.2′")]}
        page._copy_all()
        text = QApplication.clipboard().text()
        for want in ("IC 4603", "高度角", "宽", "离先验中心"):
            assert want in text, f"复制出来的没有「{want}」:{text}"


class TestStarOverlay:
    """5.9:把匹配上的星标在图上。"""

    def test_solve_feeds_the_view(self):
        src = _src(FITS, "_solve")
        assert "matched_xy" in src, "解算结果里的星点没喂给视图"
        assert "star_box.setEnabled(True)" in src

    def test_checkbox_starts_disabled(self, qt_app):
        """没解算过就没有星点 —— 勾了也没东西可画。"""
        from astro_smb_qt.shell import Shell

        page = Shell().page("fits")
        assert not page.star_box.isEnabled()


class TestFolderDownload:
    """§9 的根:浏览页的提示语指向一个不存在的功能。"""

    def test_the_dead_hint_is_gone(self):
        src = BROWSE.read_text(encoding="utf-8")
        assert "到传输页发起" not in src, (
            "提示还指着传输页,而那里没有整夹入队的入口")

    def test_directory_download_expands(self):
        src = _src(BROWSE, "_download")
        assert "_download_dir(entry)" in src

    def test_it_walks_on_a_worker(self):
        """展开要走一遍 `walk`,几百次 listdir —— 放 GUI 线程会整窗冻住。"""
        src = _src(BROWSE, "_download_dir")
        assert "self.bg.run(" in src and "client.walk(" in src

    def test_each_file_carries_the_group(self):
        """`group=<目录名>` 是传输页分组折叠的依据。"""
        src = _src(BROWSE, "_download_dir")
        assert "group=name" in src

    def test_it_submits_per_file(self):
        """**不是 `submit_download_dir`。** 整夹一个任务的话拿不到文件内
        分块并发,也没有逐文件的进度与方块图 —— 老 UI 实测按文件展开之后
        整夹 18 MB/s。"""
        src = _src(BROWSE, "_download_dir")
        assert "submit_download(" in src
        assert "submit_download_dir" not in src

    def test_empty_folder_says_so(self):
        src = _src(BROWSE, "_download_dir")
        assert "没有文件" in src, "空文件夹默默什么都不做,像是点了没反应"

    def test_behaviour_on_the_sample_mirror(self, qt_app):
        """真数据走一遍:一个目录 → 一批带同一个组名的任务。"""
        import os

        mirror = ROOT / ".tmp" / "device" / "EMMC Images"
        if not (mirror / "Autorun" / "Bias").is_dir():
            pytest.skip("没有 .tmp 镜像")
        from astro_smb.backend import make_backend
        from astro_smb_qt.shell import Shell

        sh = Shell()
        root = os.path.abspath(str(mirror))
        sh.client_factory = lambda: (lambda b: (b.connect(), b)[1])(
            make_backend(kind="local", host="", path=root))
        page = sh.page("browse")

        class _E:
            share = "EMMC Images"
            path = "Autorun\\Bias"
            name = "Bias"
            is_dir = True
            size = 0

        page.bg.run = lambda work, **kw: kw["on_done"](work())
        sh.select_page = lambda t: None
        page._download_dir(_E())
        jobs = list(sh.transfers.jobs)
        assert len(jobs) > 5, f"只入队了 {len(jobs)} 个"
        assert {j.group for j in jobs} == {"Bias"}


# ================================================================ 最后四条

class TestSolveSurvivesTheOverlay:
    """**主结果不能被可选装饰拖下水。**

    原来的顺序是"先铺星点叠加、再写解算结果"。星点那步一抛异常,
    十一行结果整个丢掉 —— 而按钮已经恢复可点,界面看着像解算完了,
    面板却永远冻在「正在解算…」。独立验收实测 100% 复现。
    """

    def test_results_are_written_before_the_overlay(self):
        src = _src(FITS, "_solve")
        at_rows = src.index("'solve_rows'")
        at_star = src.index("set_stars")
        assert at_rows < at_star, "星点叠加排在写结果之前 —— 它一抛异常结果就没了"

    def test_the_overlay_is_guarded(self):
        src = _src(FITS, "_solve")
        at = src.index("set_stars")
        seg = src[max(0, at - 400):at]
        assert "try:" in seg, "星点叠加没有兜底"

    def test_geometry_reaches_the_solver(self):
        """`solve_rows` 要靠它算视场那一行。模型里没有 `geom` 的话
        传进去的是 0×0 —— 不报错,只是十一行悄悄变十行。"""
        src = _src(FITS, "reload")
        assert "'geom': geom" in src


class TestFootprints:
    """4.7:实际视场框。

    **只用已经解算过的 WCS。** 足迹是"顺带看看",不值得为它去解算几十张
    50 MB 的图 —— 真要解算走导星质量分析那条路,两边**共用同一份缓存**,
    解过的这里立刻就有。
    """

    def test_ring_is_in_the_shared_layer(self):
        from astro_smb_app import guidequality as gq

        assert callable(gq.footprint_ring)
        assert callable(gq.collect_footprints)

    def test_ring_is_a_closed_flat_list(self):
        from astro_smb.wcs import TanWcs
        from astro_smb_app import guidequality as gq

        w = TanWcs((10.0, 20.0), (100.0, 80.0), [[-1e-4, 0.0], [0.0, 1e-4]])
        ring = gq.footprint_ring(w, 200, 160, steps=4)
        assert len(ring) == 4 * 4 * 2, len(ring)
        assert all(isinstance(v, float) for v in ring)

    def test_it_walks_the_boundary_once(self):
        """**逐段步长必须均匀。** 四条边任意一条走反,环就变成"蝴蝶结" ——
        对角线那两段会突然变成边长的 1.4 倍,而画出来是两个三角形不是一个框。
        长度检查对这种错**一点感觉都没有**(点数一个不差)。
        """
        import math

        from astro_smb.wcs import TanWcs
        from astro_smb_app import guidequality as gq

        w = TanWcs((10.0, 20.0), (100.0, 80.0), [[-1e-4, 0.0], [0.0, 1e-4]])
        ring = gq.footprint_ring(w, 200, 160, steps=6)
        pts = list(zip(ring[::2], ring[1::2]))
        steps = []
        for (a0, d0), (a1, d1) in zip(pts, pts[1:] + pts[:1]):
            dx = (a1 - a0) * math.cos(math.radians((d0 + d1) / 2))
            steps.append(math.hypot(dx, d1 - d0))
        assert min(steps) > 0, "有两个点重合 —— 角点被采了两次"
        assert max(steps) < 2 * sorted(steps)[len(steps) // 2], (
            f"步长不均匀,环大概走岔了: {steps}")

    def test_ra_is_allowed_to_wrap(self):
        """**RA 允许从 359.x 跳到 0.x** —— 调用方不许去 unwrap 它。
        在 (ra, dec) 上线性插值实测常规 2° 视场就偏 16″。"""
        src = (ROOT / "astro_smb_app" / "guidequality.py").read_text(
            encoding="utf-8")
        at = src.index("def footprint_ring")
        body = src[at:src.index("\ndef ", at + 10)]
        assert "% 360.0" in body
        assert "unwrap" in body.lower()

    def test_collect_only_uses_cached_wcs(self):
        """不去解算 —— 那是几十张 50 MB。"""
        src = (ROOT / "astro_smb_app" / "guidequality.py").read_text(
            encoding="utf-8")
        at = src.index("def collect_footprints")
        body = src[at:src.index("\ndef ", at + 10)]
        assert "cached_wcs(" in body
        assert "solve_wcs(" not in body, "足迹去解算了 —— 那会卡几分钟"

    def test_it_survives_a_missing_target_dir(self):
        from astro_smb_app import guidequality as gq

        class _C:
            host = "h"

            def listdir(self, *_a):
                raise OSError("没有这个目录")

        assert gq.collect_footprints(
            _C(), [{"name": "M 8", "ts0": 0, "ts1": 1}]) == []

    def test_page_has_a_toggle(self, qt_app):
        from astro_smb_qt.shell import Shell

        page = Shell().page("sky")
        assert page.foot_box is not None

    def test_page_pushes_the_right_message(self):
        src = _src(ROOT / "astro_smb_qt" / "pages" / "sky3d.py", "_push_foot")
        assert "'type': 'footprints'" in src

    def test_empty_result_says_why(self):
        """没有解算过的 sub 时要说清楚,不能默默什么都不发生。"""
        src = _src(ROOT / "astro_smb_qt" / "pages" / "sky3d.py", "_toggle_foot")
        assert "还没有解算过" in src

    def test_success_clears_the_reading_banner(self):
        """**读完了也要把横幅换掉。**

        原来只有"没解算过"那条分支改横幅,于是成功读到足迹之后,
        "正在读取实际视场…"一直挂着 —— 功能是好的,看着像卡死。
        独立验收轮把这条抓出来了。
        """
        src = _src(ROOT / "astro_smb_qt" / "pages" / "sky3d.py", "_toggle_foot")
        at = src.index("if not feet:")
        assert "else:" in src[at:], "成功分支压根没碰横幅"
        assert "实际视场" in src[at:]

    def test_night_combo_can_show_its_longest_item(self, qt_app):
        """夜次项带着目标数与帧数,收起状态下必须**看得全**。

        150px 只够放到"… 2 目标 ·",帧数被截在框外 —— 而帧数正是加它的
        理由。宽度不能拍脑袋写死,要按最长项算。

        **量 `minimumWidth`,不要量 `sizeHint`。** QComboBox 的 sizeHint
        本来就是按内容算的,不管你把下限设成多少它都够宽 —— 那条断言
        改回 150px 照样绿(反向验证里活了一轮)。真正决定收起状态会不会
        被截的是**下限**:布局在挤的时候给的就是它。

        **而且要换着字号验。** 只在当前字体下量一次,写死一个"够用"的
        数字就能蒙混过去 —— 全量测试里别的用例把字体调大之后,这条真的
        挂了一次(写死的 210px 在 9pt 下要 288px 才够)。宽度必须跟着字走。

        **走真实的填充路径,别只信构造时那一下。** 构造时按一条样例文字
        预留了宽度;要是填完真实夜次不再重算,样例之外的更长项(目标多、
        帧数四位数)照样会被截 —— 而只拿样例文字去断言,这个缺陷测不出来
        (反向验证里活了一轮)。
        """
        from PySide6.QtGui import QFont

        from astro_smb_qt.shell import Shell

        # 比构造时那条样例长得多:目标数两位、帧数四位
        nights = [{"date": "2026-07-23", "ts0": 1.7e9, "ts1": 1.7e9 + 3600,
                   "targets": [{"frames": 400, "name": f"T{i}",
                                "ra": 338.0, "dec": -20.0, "color": "#8ab",
                                "source": "日志坐标", "exposure": 1200.0,
                                "ts0": 1.7e9, "ts1": 1.7e9 + 300,
                                "plans": []}
                               for i in range(12)]}]
        old = qt_app.font()
        try:
            for pt in (9, 12, 16):
                qt_app.setFont(QFont(old.family(), pt))
                page = Shell().page("sky")
                page._start_web = lambda *_a, **_k: None
                page._apply((None, nights, None))
                cb = page.night_combo
                need = max(cb.fontMetrics().horizontalAdvance(cb.itemText(i))
                           for i in range(cb.count()))
                assert cb.minimumWidth() >= need, (pt, cb.minimumWidth(), need)
        finally:
            qt_app.setFont(old)


class TestBandLabelContrast:
    """清单外:白天档 treemap 标题带上的标签近黑压在中蓝底上。"""

    def test_every_theme_has_a_light_band_colour(self):
        from astro_smb_qt import theme

        before = theme.C.mode
        try:
            for m in theme.MODES:
                theme.set_mode(m)
                c = theme.Q.ON_BAND
                lum = (0.299 * c.red() + 0.587 * c.green()
                       + 0.114 * c.blue())
                assert lum > 180, f"{m} 档的 ON_BAND 不够浅: {c.name()}"
        finally:
            theme.set_mode(before)

    def test_on_accent_would_have_been_wrong(self):
        """**不能拿 `ON_ACCENT` 顶替** —— 它在深色档里是近黑,
        换过去会把现在好好的两档弄坏。"""
        from astro_smb_qt import theme

        before = theme.C.mode
        try:
            theme.set_mode(theme.MODE_NORMAL)
            c = theme.Q.ON_ACCENT
            lum = 0.299 * c.red() + 0.587 * c.green() + 0.114 * c.blue()
            assert lum < 100, "ON_ACCENT 在深色档也变浅了?那这条要重写"
        finally:
            theme.set_mode(before)

    def test_page_uses_it_for_band_labels_only(self):
        src = _src(ROOT / "astro_smb_qt" / "pages" / "space.py", "_render")
        assert "theme.C.ON_BAND if band else theme.C.TEXT" in src
        assert "band = weight == 'semibold'" in src


class TestScanProgressBar:
    def test_bar_exists_and_is_ranged(self, qt_app):
        from astro_smb_qt.pages.scan import HOSTS
        from astro_smb_qt.shell import Shell

        page = Shell().page("scan")
        assert page.bar.maximum() == HOSTS

    def test_it_is_hidden_until_scanning(self, qt_app):
        from astro_smb_qt.shell import Shell

        page = Shell().page("scan")
        assert page.bar.isHidden()

    def test_progress_moves_it(self, qt_app):
        from astro_smb_qt.shell import Shell

        page = Shell().page("scan")
        page._render_rows = lambda: None
        page._on_progress((42, []))
        assert page.bar.value() == 42
        assert not page.bar.isHidden()

    def test_done_hides_it(self, qt_app):
        from astro_smb_qt.shell import Shell

        page = Shell().page("scan")
        page._render_rows = lambda: None
        page._on_progress((42, []))
        page._on_done([])
        assert page.bar.isHidden(), "扫完了进度条还挂着"
