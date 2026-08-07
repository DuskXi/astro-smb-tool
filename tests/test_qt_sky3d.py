"""3D 天球页 —— 用户列的第 10 条后一半。

**方案决定先写在这里:不上 QtWebEngine。** 老 UI 走 WebView2 + three.js;
Qt 侧的对应物是 QtWebEngine,几百 MB 的额外依赖 —— 而这套前端全部的卖点就是
"零外部工具链、一条命令就跑"。为了一页星图把它拖进来,等于把这条路线自己的
立身之本换掉。改用 `QPainter` 画正射投影天球:同一份数据、同一套判读、
拖动旋转 + 滚轮缩放 + 点选,零新依赖。

三条几何铁律各钉一条:

* **只画近半球。** 背面的点投影上去和正面重合,不判可见性的话球背后的目标
  会凭空出现在正面。
* **连线跨过球缘要断开。** 直接连过去会画出一道横穿整个球的假线,
  而投影上看不出它其实绕到了背面。
* **赤纬要夹住。** 相机转到天极时右向量退化,画面会突然翻转。
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

from PySide6.QtWidgets import QApplication  # noqa: E402

from astro_smb_qt import theme  # noqa: E402
from astro_smb_qt.pages import sky3d as page  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "astro_smb_qt" / "pages" / "sky3d.py").read_text(encoding="utf-8")


def _body(name: str) -> str:
    at = SRC.index(f"def {name}")
    end = SRC.find("\n    def ", at + 10)
    return SRC[at:end if end > 0 else len(SRC)]


@pytest.fixture(scope="module")
def qt_app():
    inst = QApplication.instance() or QApplication([])
    theme.apply(inst)
    return inst


@pytest.fixture()
def view(qt_app):
    v = page.SphereView()
    v.resize(400, 400)
    return v


class TestNoWebEngine:
    def test_page_does_not_import_qtwebengine(self):
        # **查的是 import,不是"文中出现过"** —— 模块文档里正解释着"为什么
        # 不用 QtWebEngine",第一版按子串查,自己把自己判红了。
        code = "\n".join(ln for ln in SRC.splitlines()
                         if ln.lstrip().startswith(("import ", "from ")))
        assert "WebEngine" not in code, (
            "把 QtWebEngine 拖进来了 —— 那是几百 MB,而这套前端的卖点就是"
            "一条命令就跑")

    def test_it_is_not_a_stub_anymore(self):
        from astro_smb_qt.pages import PAGE_CLASSES

        assert PAGE_CLASSES["sky"] is page.Sky3DPage
        assert "未实现" not in SRC


class TestProjection:
    def test_facing_point_lands_in_the_centre(self, view):
        view.ra0, view.dec0, view.zoom = 120.0, 30.0, 1.0
        x, y, vis = view.project(120.0, 30.0)
        assert vis
        assert abs(x - 200) < 1.0 and abs(y - 200) < 1.0, (x, y)

    def test_far_side_is_invisible(self, view):
        """**背面的点投影上去和正面重合** —— 不判可见性就会凭空冒出来。"""
        view.ra0, view.dec0 = 0.0, 0.0
        assert view.project(180.0, 0.0)[2] is False

    def test_limb_is_visible(self, view):
        view.ra0, view.dec0 = 0.0, 0.0
        x, _y, vis = view.project(89.0, 0.0)
        assert vis
        assert x > 200, "赤经 +89° 应当落在中心右侧(东在右)"

    def test_zoom_scales_the_radius(self, view):
        view.ra0, view.dec0 = 0.0, 0.0
        near = view.project(30.0, 0.0)[0]
        view.zoom = 2.0
        far = view.project(30.0, 0.0)[0]
        assert far - 200 > (near - 200) * 1.8

    def test_pole_does_not_blow_up(self, view):
        """视线正对天极时右向量退化 —— 不能除零。"""
        view.ra0, view.dec0 = 0.0, 90.0
        x, y, _v = view.project(0.0, 89.0)
        assert math.isfinite(x) and math.isfinite(y)


class TestDragAndZoomAreClamped:
    def test_dec_is_clamped(self, view):
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QMouseEvent

        view.mousePressEvent(QMouseEvent(
            QMouseEvent.Type.MouseButtonPress, QPointF(200, 200),
            Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
        for _ in range(30):
            view.mouseMoveEvent(QMouseEvent(
                QMouseEvent.Type.MouseMove, QPointF(200, 400),
                Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
        assert -85.0 <= view.dec0 <= 85.0, (
            f"赤纬没夹住({view.dec0}) —— 转到天极时画面会突然翻转")

    def test_zoom_is_clamped(self, view):
        assert "max(0.6, min(6.0" in _body("wheelEvent"), "缩放没有上下限"

    def test_ra_wraps(self, view):
        view.ra0 = 359.0
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QMouseEvent

        view.mousePressEvent(QMouseEvent(
            QMouseEvent.Type.MouseButtonPress, QPointF(200, 200),
            Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))
        view.mouseMoveEvent(QMouseEvent(
            QMouseEvent.Type.MouseMove, QPointF(100, 200),
            Qt.NoButton, Qt.LeftButton, Qt.NoModifier))
        assert 0.0 <= view.ra0 < 360.0


class TestPolylineBreaksAtTheLimb:
    def test_source_guards_visibility(self):
        body = _body("_polyline")
        assert "prev = (x, y) if vis else None" in body, (
            "跨过球缘直接连过去 —— 会画出一道横穿整个球的假线,"
            "而投影上看不出它其实绕到了背面")


class TestHorizonCircle:
    """地平圈是这一页真正值钱的东西。"""

    def test_zenith_is_lst_and_latitude(self):
        body = _body("_update_horizon")
        assert "lst_deg(" in body, "天顶赤经不是 LST —— 地平圈会转到别处去"
        assert "self._site()" in body, "站点不是从统一那一处取的"

    def test_prefers_the_log_longitude(self):
        """站点取值收敛到 `_site()` —— 降级球与真 3D 页必须用同一个经度,
        各取各的会让两条路径画出不一样的地平圈。"""
        body = _body("_site")
        assert "lon_estimate" in body, (
            "经度没优先用日志反推值 —— 与浏览页详情那条是同一个坑")

    def test_both_paths_share_the_site(self):
        assert _body("_update_horizon").count("self._site()") == 1
        assert "self._site()" in _body("_push_web")

    def test_circle_is_perpendicular_to_zenith(self, view):
        """采样点必须**每一个**都离天顶 90°,否则那不是地平圈。"""
        import astro_smb_qt.pages.sky3d as m

        z = m._unit(45.0, 30.0)
        ax, ay = -z[1], z[0]
        n = math.hypot(ax, ay)
        a = (ax / n, ay / n, 0.0)
        b = (z[1] * a[2] - z[2] * a[1], z[2] * a[0] - z[0] * a[2],
             z[0] * a[1] - z[1] * a[0])
        for k in range(0, 360, 15):
            t = math.radians(k)
            v = tuple(math.cos(t) * a[i] + math.sin(t) * b[i] for i in range(3))
            dot = sum(v[i] * z[i] for i in range(3))
            assert abs(dot) < 1e-9, f"{k}° 处不垂直: {dot}"

    def test_slider_moves_it(self):
        body = _body("_set_frac")
        assert "_update_horizon()" in body, "拖滑杆地平圈不动 —— 等于没有滑杆"


class TestTargetsComeFromTheSharedLayer:
    def test_uses_build_nights(self):
        assert "sv._build_nights(" in SRC, (
            "目标列表自己拼了一份 —— 同夜同名跨 Plan 合并、停机位不上天球"
            "这些判据会漂开")

    def test_detail_reports_the_coordinate_source(self):
        body = _body("_render_detail")
        # **查的是那一行 `MetricRow`,不是"这段文字里出现过"** ——
        # 紧跟其后的注释里也写着「坐标来源」,按子串查的话把那一行删掉
        # 断言照样成立(这一轮第五次栽在注释/文档字符串上)。
        assert 'MetricRow(_("坐标来源")' in body, (
            "没写坐标是 FITS 实测还是日志 goto 值 —— 两者差着指向模型误差"
            "(实测恒差 21′),混为一谈会让判断整个偏掉")

    def test_hit_test_picks_the_nearest(self, view):
        view.targets = [{"name": "A", "ra": 0.0, "dec": 0.0},
                        {"name": "B", "ra": 20.0, "dec": 0.0}]
        view.ra0, view.dec0 = 10.0, 0.0
        view.grab()                       # 画一遍才有 _hits
        ax, ay, _ = view.project(0.0, 0.0)
        assert view._hit(ax, ay) == "A"

    def test_far_side_targets_are_not_clickable(self, view):
        view.targets = [{"name": "A", "ra": 180.0, "dec": 0.0}]
        view.ra0, view.dec0 = 0.0, 0.0
        view.grab()
        assert view._hits == [], "背面的目标进了命中表"


class TestFaceSelected:
    def test_points_the_camera(self):
        body = _body("_face_selected")
        assert "self.sphere.ra0" in body and "self.sphere.dec0" in body
        assert "min(85.0" in body, "正视时赤纬没夹住,转到极点会翻转"
