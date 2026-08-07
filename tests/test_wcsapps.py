"""astro_smb.wcsapps 的离线单测:合成 WCS + 已知答案,不碰网络也不碰真机文件。

思路是**每个量都反推得出**,而不是"跑一遍看着像":

* 面积:八分之一球面(5156.62 deg²)、球冠 ``2π(1-cos r)``、球面三角形剩余量 —— 都有闭式。
* 覆盖:用 4 条矩形拼一个正方形环,中间的洞面积是算得出来的 1 deg²。
* 场旋/漂移:先给定速率造样本,再看拟合能不能把这个速率原样解回来。
* dither:同一批样本**扣与不扣**对比 —— 不扣的残差必须大一个量级,
  否则 ``subtract`` 就是空转的。
* 盲解网格:不是抽样看看,而是**构造最坏点**(带边缘 × 相邻中心的中赤经)
  去卡"任意天球点都落在某个 hint 的搜索半径内"。

坐标约定在 :mod:`astro_smb.wcs` 的 docstring 里,这里只复述一条最容易错的:
像素是 **FITS 1-based**,图幅中心 = ``((W+1)/2, (H+1)/2)``,外边界 0.5 与 N+0.5。
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from astro_smb import wcsapps as A
from astro_smb.platesolve import SolveHint
from astro_smb.wcs import (TanWcs, angular_separation, pixel_to_world,
                            radec_to_unit, world_to_pixel)
from tests.support import tr

# ----------------------------------------------------------------- 合成工具

FRAME_W, FRAME_H = 6248, 4176
CENTER_PX = ((FRAME_W + 1) / 2.0, (FRAME_H + 1) / 2.0)
SCALE = 1.93          # 角秒/像素,ASI2600MC @ 400mm
FOV_W = FRAME_W * SCALE / 3600.0      # 约 3.35°
FOV_H = FRAME_H * SCALE / 3600.0      # 约 2.24°


def make_wcs(ra: float = 83.6, dec: float = 22.0, scale: float = SCALE,
             rot: float = 0.0, flip: bool = True, crpix=CENTER_PX,
             width: int = FRAME_W, height: int = FRAME_H) -> TanWcs:
    """造一份 TAN WCS。``flip=True`` 是 ASIAIR light 帧的实测取向(det > 0)。"""
    s = scale / 3600.0
    r = math.radians(rot)
    if flip:
        cd = s * np.array([[math.cos(r), math.sin(r)],
                           [-math.sin(r), math.cos(r)]])
    else:
        cd = s * np.array([[-math.cos(r), math.sin(r)],
                           [math.sin(r), math.cos(r)]])
    if crpix is None:
        crpix = ((width + 1) / 2.0, (height + 1) / 2.0)
    return TanWcs((ra, dec), crpix, cd)


def arcsec_ra(d_arcsec: float, dec: float) -> float:
    """东向 ``d_arcsec`` 角秒对应多少度赤经(除掉 cos δ)。"""
    return d_arcsec / 3600.0 / math.cos(math.radians(dec))


def fake_result(wcs, size=(FRAME_W, FRAME_H), center=None):
    """冒充 SolveResult:只要有 ``.wcs`` / ``.hint`` / ``.center`` 就够(鸭子类型)。"""
    ns = SimpleNamespace(wcs=wcs, hint=SolveHint(image_size=size))
    if center is not None:
        ns.center = center
    return ns


def lonlat_rect(ra0, ra1, dec0, dec1, n=2) -> np.ndarray:
    """(ra, dec) 矩形的顶点多边形(每边 n 段,逆时针)。"""
    def edge(a, b, c, d):
        f = np.linspace(0.0, 1.0, n + 1)[:-1]
        return np.column_stack([a + (c - a) * f, b + (d - b) * f])
    return np.vstack([edge(ra0, dec0, ra1, dec0), edge(ra1, dec0, ra1, dec1),
                      edge(ra1, dec1, ra0, dec1), edge(ra0, dec1, ra0, dec0)])


def worst_gap_deg(grid: np.ndarray, pts: np.ndarray) -> float:
    """``pts`` 里每个点到最近格点的角距的最大值(度)。"""
    gu = radec_to_unit(grid[:, 0], grid[:, 1])
    pu = radec_to_unit(pts[:, 0], pts[:, 1])
    best = np.full(len(pts), -2.0)
    for k in range(0, len(gu), 512):
        best = np.maximum(best, (pu @ gu[k:k + 512].T).max(axis=1))
    return float(np.degrees(np.arccos(np.clip(best, -1.0, 1.0))).max())


def worst_case_points(grid: np.ndarray) -> np.ndarray:
    """网格的**解析最坏点**:每个环负责的赤纬带上下沿 × 相邻中心的中赤经。

    随机撒点永远打不中真正的最坏角落(实测比解析值低 2%),覆盖完备性这种
    "必须永真"的性质只能这么卡。
    """
    decs = np.unique(np.round(grid[:, 1], 9))
    d_step = float(decs[1] - decs[0]) if len(decs) > 1 else 180.0
    out = []
    for dc in decs:
        ras = np.sort(grid[np.abs(grid[:, 1] - dc) < 1e-9][:, 0])
        if len(ras) > 1:
            mids = (ras + np.roll(ras, -1)) / 2.0
            mids[-1] = ((ras[-1] + ras[0] + 360.0) / 2.0) % 360.0
        else:
            mids = np.array([(ras[0] + 180.0) % 360.0])
        for edge in (dc - d_step / 2.0, dc, dc + d_step / 2.0):
            e = min(90.0, max(-90.0, edge))
            for r in (mids, ras):
                out.append(np.column_stack([r, np.full(len(r), e)]))
    return np.vstack(out)


# ============================================================ 切平面基础


class TestTangentPlane:
    """本模块自带的 gnomonic 投影必须和 astro_smb.wcs 的那套完全一致。"""

    def test_projection_matches_world_to_pixel(self):
        # 以 CRVAL 为切点投影,再除以像素尺度,应当回到像素偏移
        w = make_wcs(120.0, -35.0)
        ra, dec = pixel_to_world(w, np.array([100.0, 3000.0, 6000.0]),
                                 np.array([200.0, 2000.0, 4000.0]))
        xi, eta, ok = A._project_tangent(w.crval, ra, dec)
        assert bool(np.all(ok))
        inv = w.cd_inv()
        dx = inv[0, 0] * xi + inv[0, 1] * eta + w.crpix[0]
        dy = inv[1, 0] * xi + inv[1, 1] * eta + w.crpix[1]
        assert dx == pytest.approx([100.0, 3000.0, 6000.0], abs=1e-8)
        assert dy == pytest.approx([200.0, 2000.0, 4000.0], abs=1e-8)

    def test_project_deproject_roundtrip(self):
        center = (359.7, 88.0)          # 贴着极点又跨 0h,两个坑一起踩
        ra = np.array([0.3, 180.0, 359.9, 10.0])
        dec = np.array([87.0, 89.5, 88.2, 85.0])
        xi, eta, ok = A._project_tangent(center, ra, dec)
        assert bool(np.all(ok))
        ra2, dec2 = A._deproject_tangent(center, xi, eta)
        assert np.max(np.asarray(angular_separation(ra, dec, ra2, dec2))) < 1e-11

    def test_backside_is_flagged(self):
        _xi, _eta, ok = A._project_tangent((0.0, 0.0), [90.001, 89.0], [0.0, 0.0])
        assert list(np.asarray(ok)) == [False, True]


# ================================================================ 1. 足迹


class TestFootprint:

    def test_default_is_four_corners(self):
        fp = A.footprint(make_wcs(), FRAME_W, FRAME_H)
        assert fp.n_vertices == 4
        assert fp.radec.shape == (4, 2)

    def test_corners_map_back_to_frame_boundary(self):
        w = make_wcs()
        fp = A.footprint(w, FRAME_W, FRAME_H)
        x, y = world_to_pixel(w, fp.radec[:, 0], fp.radec[:, 1])
        assert np.asarray(x) == pytest.approx(
            [0.5, FRAME_W + 0.5, FRAME_W + 0.5, 0.5], abs=1e-6)
        assert np.asarray(y) == pytest.approx(
            [0.5, 0.5, FRAME_H + 0.5, FRAME_H + 0.5], abs=1e-6)

    def test_n_per_edge_multiplies_vertices(self):
        fp = A.footprint(make_wcs(), FRAME_W, FRAME_H, n_per_edge=3)
        assert fp.n_vertices == 12

    def test_edge_samples_lie_on_the_boundary(self):
        w = make_wcs()
        fp = A.footprint(w, FRAME_W, FRAME_H, n_per_edge=4)
        x, y = world_to_pixel(w, fp.radec[:, 0], fp.radec[:, 1])
        on_edge = (np.isclose(x, 0.5) | np.isclose(x, FRAME_W + 0.5)
                   | np.isclose(y, 0.5) | np.isclose(y, FRAME_H + 0.5))
        assert bool(np.all(on_edge))

    def test_closed_radec_repeats_first_vertex(self):
        fp = A.footprint(make_wcs(), FRAME_W, FRAME_H)
        c = fp.closed_radec()
        assert c.shape == (5, 2)
        assert c[0] == pytest.approx(c[-1])

    def test_closed_unwrapped_uses_continuous_ra(self):
        fp = A.footprint(make_wcs(0.2, 10.0), FRAME_W, FRAME_H)
        c = fp.closed_unwrapped()
        assert c.shape == (5, 2)
        # 连续化之后相邻点的赤经差都是小量,不会有 360 的跳
        assert float(np.abs(np.diff(c[:, 0])).max()) < 10.0

    def test_no_wrap_flag_for_ordinary_field(self):
        fp = A.footprint(make_wcs(83.6, 22.0), FRAME_W, FRAME_H)
        assert fp.wraps_ra0 is False
        assert fp.pole == 0

    def test_wrap_flag_across_ra_zero(self):
        fp = A.footprint(make_wcs(0.2, 10.0), FRAME_W, FRAME_H)
        assert fp.wraps_ra0 is True
        lo, hi = fp.ra_span_deg()
        assert lo < 360.0 < hi          # 连续化后跨过 360,可以直接画
        assert hi - lo == pytest.approx(FOV_W / math.cos(math.radians(10.0)),
                                        rel=0.05)

    def test_unwrapped_ra_is_continuous(self):
        fp = A.footprint(make_wcs(359.9, -5.0), FRAME_W, FRAME_H)
        assert float(np.abs(np.diff(fp.unwrapped_ra)).max()) < 10.0
        assert fp.unwrapped_ra[0] == pytest.approx(fp.radec[0, 0] % 360.0)

    def test_north_pole_inside(self):
        fp = A.footprint(make_wcs(0.0, 89.9), FRAME_W, FRAME_H)
        assert fp.pole == 1
        assert fp.wraps_ra0 is False        # 含极点时"跨 0h"没有意义
        assert fp.ra_span_deg() == (0.0, 360.0)
        assert fp.dec_span_deg()[1] == 90.0

    def test_south_pole_inside(self):
        fp = A.footprint(make_wcs(180.0, -89.9), FRAME_W, FRAME_H)
        assert fp.pole == -1
        assert fp.dec_span_deg()[0] == -90.0

    def test_pole_just_outside_is_not_flagged(self):
        # 极点落在图幅外(距中心 > 半高)就不该报含极点
        fp = A.footprint(make_wcs(0.0, 90.0 - FOV_H), FRAME_W, FRAME_H)
        assert fp.pole == 0

    def test_contains_center_and_rejects_far_field(self):
        fp = A.footprint(make_wcs(83.6, 22.0), FRAME_W, FRAME_H)
        assert fp.contains(83.6, 22.0) is True
        assert fp.contains(83.6 + 5.0, 22.0) is False

    def test_contains_is_vectorized(self):
        fp = A.footprint(make_wcs(83.6, 22.0), FRAME_W, FRAME_H)
        got = fp.contains(np.array([83.6, 83.6, 100.0]),
                          np.array([22.0, 22.0 + FOV_H, 22.0]))
        assert list(got) == [True, False, False]

    def test_contains_edge_is_inside(self):
        w = make_wcs()
        fp = A.footprint(w, FRAME_W, FRAME_H)
        ra, dec = pixel_to_world(w, 1.0, 1.0)          # 第一个像素的中心
        assert fp.contains(ra, dec) is True

    def test_polygon_fallback_contains_matches_pixel_test(self):
        """没有 WCS 时走切平面 crossing number,结果必须和像素判据一致。"""
        w = make_wcs(83.6, 22.0)
        fp = A.footprint(w, FRAME_W, FRAME_H, n_per_edge=1)
        bare = A._as_footprint(fp.radec, None, None)
        rng = np.random.default_rng(3)
        ra = 83.6 + rng.uniform(-3.0, 3.0, 500)
        dec = 22.0 + rng.uniform(-2.0, 2.0, 500)
        assert np.array_equal(fp.contains(ra, dec), bare.contains(ra, dec))

    def test_bad_size_raises(self):
        with pytest.raises(A.WcsAppsError):
            A.footprint(make_wcs(), 0, FRAME_H)

    def test_bad_n_per_edge_raises(self):
        with pytest.raises(A.WcsAppsError):
            A.footprint(make_wcs(), FRAME_W, FRAME_H, n_per_edge=0)

    def test_non_wcs_raises(self):
        with pytest.raises(A.WcsAppsError):
            A.footprint("不是 WCS", FRAME_W, FRAME_H)


# =========================================================== 2. 球面面积


class TestFootprintArea:

    def test_octant_of_the_sphere(self):
        """三个直角的球面三角形 = 1/8 球面。整球 41252.96 deg²。"""
        got = A.footprint_area_deg2([[0.0, 0.0], [90.0, 0.0], [0.0, 90.0]])
        assert got == pytest.approx(41252.96124941928 / 8.0, rel=1e-12)

    def test_spherical_triangle_excess(self):
        """顶点在北极 + 赤道上两点:剩余量 = 顶角(弧度),与球面三角学一致。"""
        for theta in (30.0, 120.0):
            got = A.footprint_area_deg2(
                [[0.0, 90.0], [0.0, 0.0], [theta, 0.0]])
            expect = math.radians(theta) * (180.0 / math.pi) ** 2
            assert got == pytest.approx(expect, rel=1e-12)

    def test_polar_cap_converges_to_closed_form(self):
        """极冠面积 2π(1-cos r);多边形边数越多越逼近(差的是离散化,不是公式)。"""
        r = 5.0
        exact = 2 * math.pi * (1 - math.cos(math.radians(r))) * (180 / math.pi) ** 2
        errs = []
        for n in (360, 3600):
            lon = np.linspace(0.0, 360.0, n, endpoint=False)
            poly = np.column_stack([lon, np.full(n, 90.0 - r)])
            errs.append(abs(A.footprint_area_deg2(poly) - exact) / exact)
        assert errs[0] < 1e-4
        assert errs[1] < 1e-6
        assert errs[1] < errs[0] / 50.0

    def test_south_cap_same_as_north(self):
        n = 720
        lon = np.linspace(0.0, 360.0, n, endpoint=False)
        north = np.column_stack([lon, np.full(n, 60.0)])
        south = np.column_stack([lon, np.full(n, -60.0)])
        assert A.footprint_area_deg2(north) == pytest.approx(
            A.footprint_area_deg2(south), rel=1e-12)

    def test_orientation_does_not_matter(self):
        poly = lonlat_rect(10.0, 12.0, -1.0, 1.0)
        assert A.footprint_area_deg2(poly) == pytest.approx(
            A.footprint_area_deg2(poly[::-1]), rel=1e-12)

    def test_equatorial_rectangle_matches_integral(self):
        """赤道带矩形面积 = Δα × (sin δ₂ - sin δ₁),换算成平方度。"""
        poly = lonlat_rect(10.0, 13.0, -0.5, 0.5, n=64)
        expect = 3.0 * (math.sin(math.radians(0.5)) - math.sin(math.radians(-0.5))) \
            * (180.0 / math.pi)
        assert A.footprint_area_deg2(poly) == pytest.approx(expect, rel=2e-4)

    def test_frame_area_close_to_fov_product(self):
        w = make_wcs()
        area = A.footprint(w, FRAME_W, FRAME_H).area_deg2()
        fw, fh = w.fov_deg(FRAME_W, FRAME_H)
        assert area == pytest.approx(fw * fh, rel=2e-3)

    def test_area_is_invariant_near_the_pole(self):
        """同一台设备指哪儿视场面积都一样 —— 极点附近也不能例外。"""
        a = A.footprint(make_wcs(83.6, 22.0), FRAME_W, FRAME_H).area_deg2()
        b = A.footprint(make_wcs(0.0, 89.9), FRAME_W, FRAME_H).area_deg2()
        assert b == pytest.approx(a, rel=1e-9)

    def test_closing_vertex_is_ignored(self):
        poly = lonlat_rect(10.0, 12.0, -1.0, 1.0)
        closed = np.vstack([poly, poly[:1]])
        assert A.footprint_area_deg2(closed) == pytest.approx(
            A.footprint_area_deg2(poly), rel=1e-12)

    def test_degenerate_polygon_is_zero(self):
        assert A.footprint_area_deg2(np.zeros((2, 2))) == 0.0
        assert A.footprint_area_deg2(np.zeros((0, 2))) == 0.0

    def test_nan_raises(self):
        with pytest.raises(A.WcsAppsError):
            A.footprint_area_deg2([[0.0, 0.0], [1.0, float("nan")], [1.0, 1.0]])

    def test_bad_shape_raises(self):
        with pytest.raises(A.WcsAppsError):
            A.footprint_area_deg2([1.0, 2.0, 3.0])


# ================================================================ 3. 覆盖


class TestCoverage:

    def test_empty_input_is_not_an_error(self):
        c = A.coverage([])
        assert c.n_frames == 0
        assert c.union_area_deg2 == 0.0
        assert c.counts.size == 0
        assert math.isnan(c.common_fraction)

    def test_single_frame_matches_its_own_area(self):
        """格网面积随 1/grid 收敛到闭式面积。

        误差是**系统性**的:视场在切平面上是个直多边形,边落在格心之间的哪一侧
        由几何决定,不会像随机采样那样自己抵消 —— 所以四条边各差半格,
        相对误差约 2/grid(grid=128 实测 0.9%,grid=768 实测 0.09%)。
        """
        w = make_wcs()
        truth = A.footprint(w, FRAME_W, FRAME_H).area_deg2()
        coarse = A.coverage([w], width=FRAME_W, height=FRAME_H, grid=128)
        fine = A.coverage([w], width=FRAME_W, height=FRAME_H, grid=768)
        e_coarse = abs(coarse.union_area_deg2 - truth) / truth
        e_fine = abs(fine.union_area_deg2 - truth) / truth
        assert e_coarse < 0.02
        assert e_fine < 0.003
        assert e_fine < e_coarse
        assert fine.common_area_deg2 == pytest.approx(fine.union_area_deg2)
        assert fine.common_fraction == pytest.approx(1.0)
        assert fine.n_gaps == 0

    def test_identical_frames_stack_to_full_depth(self):
        w = make_wcs()
        c = A.coverage([w, w, w], width=FRAME_W, height=FRAME_H, grid=128)
        assert int(c.counts.max()) == 3
        assert c.common_area_deg2 == pytest.approx(c.union_area_deg2)

    def test_dither_shrinks_the_common_area(self):
        frames = [make_wcs(83.6 + arcsec_ra(dx, 22.0), 22.0 + dy / 3600.0)
                  for dx, dy in ((0, 0), (120, 0), (0, 120), (120, 120))]
        c = A.coverage(frames, width=FRAME_W, height=FRAME_H, grid=256)
        assert c.common_area_deg2 < c.union_area_deg2
        assert 0.9 < c.common_fraction < 1.0
        # 并集比单张大,但大不过单张 + 抖动带
        one = A.footprint(frames[0], FRAME_W, FRAME_H).area_deg2()
        assert one < c.union_area_deg2 < one * 1.05

    def test_depth_area_partitions_the_grid(self):
        frames = [make_wcs(83.6 + arcsec_ra(dx, 22.0), 22.0)
                  for dx in (0, 600, 1200)]
        c = A.coverage(frames, width=FRAME_W, height=FRAME_H, grid=192)
        assert float(c.depth_area_deg2[1:].sum()) == pytest.approx(
            c.union_area_deg2, rel=1e-12)
        assert float(c.depth_area_deg2.sum()) == pytest.approx(
            float(c.cell_area_deg2.sum()), rel=1e-12)
        assert c.common_area_deg2 == pytest.approx(c.depth_area_deg2[3])

    def test_known_hole_area(self):
        """4 条矩形拼出一个正方形环,中间的洞是精确的 1 deg²。"""
        ra0, dec0 = 10.0, 0.0
        bars = [lonlat_rect(ra0 - 1.5, ra0 + 1.5, dec0 + 0.5, dec0 + 1.5, n=16),
                lonlat_rect(ra0 - 1.5, ra0 + 1.5, dec0 - 1.5, dec0 - 0.5, n=16),
                lonlat_rect(ra0 - 1.5, ra0 - 0.5, dec0 - 0.5, dec0 + 0.5, n=16),
                lonlat_rect(ra0 + 0.5, ra0 + 1.5, dec0 - 0.5, dec0 + 0.5, n=16)]
        c = A.coverage(bars, grid=512)
        hole = 1.0 * (math.sin(math.radians(0.5)) - math.sin(math.radians(-0.5))) \
            * (180.0 / math.pi)
        assert c.n_gaps == 1
        assert c.max_gap_deg2 == pytest.approx(hole, rel=0.05)
        assert c.gap_area_deg2 == pytest.approx(c.max_gap_deg2)
        assert c.gap_fraction == pytest.approx(hole / c.union_area_deg2, rel=0.06)

    def test_compact_union_has_no_gap(self):
        frames = [make_wcs(83.6 + arcsec_ra(dx, 22.0), 22.0)
                  for dx in (0, 1800, 3600)]
        c = A.coverage(frames, width=FRAME_W, height=FRAME_H, grid=256)
        assert c.n_gaps == 0
        assert c.max_gap_deg2 == 0.0

    def test_outside_blank_is_not_counted_as_gap(self):
        """两块互不相连的天区之间的空白与画布边缘连通,不算"漏拍"。"""
        frames = [make_wcs(83.6, 22.0), make_wcs(83.6 + 8.0, 22.0)]
        c = A.coverage(frames, width=FRAME_W, height=FRAME_H, grid=192)
        assert c.n_gaps == 0
        assert c.common_area_deg2 == 0.0

    def test_mixed_input_types(self):
        w = make_wcs()
        items = [w, A.footprint(w, FRAME_W, FRAME_H), fake_result(w),
                 A.footprint(w, FRAME_W, FRAME_H).radec]
        c = A.coverage(items, width=FRAME_W, height=FRAME_H, grid=96)
        assert c.n_frames == 4
        assert int(c.counts.max()) == 4
        assert c.frame_area_deg2 == pytest.approx(
            [c.frame_area_deg2[0]] * 4, rel=1e-9)

    def test_result_uses_hint_image_size(self):
        c = A.coverage([fake_result(make_wcs())], grid=96)
        assert c.n_frames == 1
        assert c.union_area_deg2 > 0.0

    def test_missing_size_raises(self):
        with pytest.raises(A.WcsAppsError):
            A.coverage([make_wcs()], grid=64)

    def test_bad_grid_raises(self):
        with pytest.raises(A.WcsAppsError):
            A.coverage([make_wcs()], width=FRAME_W, height=FRAME_H, grid=4)

    def test_frames_spread_over_the_sky_raises(self):
        frames = [make_wcs(0.0, 0.0), make_wcs(180.0, 0.0)]
        with pytest.raises(A.WcsAppsError):
            A.coverage(frames, width=FRAME_W, height=FRAME_H, grid=64)

    def test_cell_area_uses_the_gnomonic_jacobian(self):
        """格子面积不是常数:离切点越远压缩越多(这就是那个 (1+r²)^-1.5)。"""
        frames = [make_wcs(83.6, 22.0), make_wcs(83.6 + 3.0, 22.0)]
        c = A.coverage(frames, width=FRAME_W, height=FRAME_H, grid=128)
        flat = c.cell_deg ** 2
        assert float(c.cell_area_deg2.max()) <= flat * (1.0 + 1e-12)
        assert float(c.cell_area_deg2.min()) < flat

    def test_depth_at_reads_the_map(self):
        frames = [make_wcs(83.6, 22.0), make_wcs(83.6, 22.0)]
        c = A.coverage(frames, width=FRAME_W, height=FRAME_H, grid=128)
        assert c.depth_at(83.6, 22.0) == 2
        assert c.depth_at(120.0, -40.0) == -1
        got = c.depth_at(np.array([83.6, 120.0]), np.array([22.0, -40.0]))
        assert list(got) == [2, -1]

    def test_cell_radec_shape_matches_counts(self):
        c = A.coverage([make_wcs()], width=FRAME_W, height=FRAME_H, grid=64)
        ra, dec = c.cell_radec()
        assert ra.shape == c.counts.shape == dec.shape
        assert A.coverage([]).cell_radec()[0].size == 0


# ========================================================== 4. 指向误差


class TestPointingError:

    def test_known_offset(self):
        dec = 22.0
        solved = (83.6 + arcsec_ra(10.0, dec), dec + 5.0 / 3600.0)
        err = A.pointing_error(solved, (83.6, dec))
        assert err.ra_arcsec == pytest.approx(10.0, rel=1e-4)
        assert err.dec_arcsec == pytest.approx(5.0, rel=1e-9)
        assert err.total_arcsec == pytest.approx(math.hypot(10.0, 5.0), rel=1e-4)

    def test_unpacks_as_a_triple(self):
        total, dra, ddec = A.pointing_error((10.0, 0.0), (10.0, 0.0))
        assert (total, dra, ddec) == (0.0, 0.0, 0.0)

    def test_ra_wrap_across_zero(self):
        err = A.pointing_error((0.05, 60.0), (359.95, 60.0))
        assert err.ra_arcsec == pytest.approx(0.1 * 3600.0 * 0.5, rel=1e-4)
        assert err.total_arcsec == pytest.approx(abs(err.ra_arcsec), rel=1e-4)

    def test_sign_convention_east_and_north_positive(self):
        err = A.pointing_error((83.7, 22.1), (83.6, 22.0))
        assert err.ra_arcsec > 0.0 and err.dec_arcsec > 0.0
        back = A.pointing_error((83.6, 22.0), (83.7, 22.1))
        assert back.ra_arcsec < 0.0 and back.dec_arcsec < 0.0
        assert back.total_arcsec == pytest.approx(err.total_arcsec)

    def test_total_is_the_great_circle_distance(self):
        err = A.pointing_error((100.0, -20.0), (101.0, -19.0))
        expect = angular_separation(100.0, -20.0, 101.0, -19.0) * 3600.0
        assert err.total_arcsec == pytest.approx(expect, rel=1e-12)

    def test_cos_dec_is_applied(self):
        """同样 0.1° 的赤经差,赤纬 60° 处的实际偏差只有赤道的一半。"""
        eq = A.pointing_error((10.1, 0.0), (10.0, 0.0))
        hi = A.pointing_error((10.1, 60.0), (10.0, 60.0))
        assert hi.ra_arcsec == pytest.approx(eq.ra_arcsec * 0.5, rel=1e-3)

    def test_vectorized(self):
        solved = np.array([[83.6, 22.0], [83.7, 22.0]])
        req = np.array([[83.6, 22.0], [83.6, 22.0]])
        err = A.pointing_error(solved, req)
        assert err.total_arcsec.shape == (2,)
        assert err.total_arcsec[0] == 0.0
        assert err.ra_arcsec[1] > 0.0

    def test_accepts_result_with_center(self):
        r = fake_result(make_wcs(), center=(83.6, 22.0))
        assert A.pointing_error(r, (83.6, 22.0)).total_arcsec == 0.0

    def test_result_without_center_raises(self):
        r = SimpleNamespace(center=None)
        with pytest.raises(A.WcsAppsError):
            A.pointing_error(r, (83.6, 22.0))

    def test_bad_shape_raises(self):
        with pytest.raises(A.WcsAppsError):
            A.pointing_error((1.0, 2.0, 3.0), (1.0, 2.0))

    def test_length_mismatch_raises(self):
        with pytest.raises(A.WcsAppsError):
            A.pointing_error(np.zeros((3, 2)), np.zeros((2, 2)))


# ================================================================ 5. 场旋


def rotation_series(rate_deg_per_hour: float, n: int = 12, dt: float = 600.0,
                    start: float = 10.0, t0: float = 0.0):
    return [(t0 + i * dt,
             make_wcs(rot=(start + rate_deg_per_hour * (i * dt / 3600.0)) % 360.0))
            for i in range(n)]


class TestFieldRotation:

    def test_recovers_a_known_rate(self):
        fr = A.field_rotation(rotation_series(0.5))
        assert fr.ok
        assert fr.rate_deg_per_hour == pytest.approx(0.5, rel=1e-9)
        assert fr.rms_deg < 1e-9
        assert fr.n == 12 and fr.n_skipped == 0

    def test_span_and_total(self):
        fr = A.field_rotation(rotation_series(0.5))
        assert fr.span_hours == pytest.approx(11 * 600.0 / 3600.0)
        assert fr.total_deg == pytest.approx(0.5 * fr.span_hours, rel=1e-9)

    def test_rate_in_arcsec_per_minute(self):
        fr = A.field_rotation(rotation_series(0.6))
        assert fr.rate_arcsec_per_min == pytest.approx(0.6 * 3600.0 / 60.0)

    def test_unwraps_across_360(self):
        """起点 359.6°、正向转过 0h —— 不解缠的话斜率会是 -几十度/小时。"""
        fr = A.field_rotation(rotation_series(0.5, start=359.6))
        assert fr.rate_deg_per_hour == pytest.approx(0.5, rel=1e-6)
        assert float(fr.angles_deg.min()) < 360.0 < float(fr.angles_deg.max())
        assert float(np.abs(np.diff(fr.angles_deg)).max()) < 1.0   # 没有 360 跳变

    def test_negative_rate(self):
        fr = A.field_rotation(rotation_series(-0.8))
        assert fr.rate_deg_per_hour == pytest.approx(-0.8, rel=1e-9)

    def test_meridian_flip_is_not_field_rotation(self):
        samples = [
            (0.0, make_wcs(rot=20.0)),
            (600.0, make_wcs(rot=20.1)),
            (1200.0, make_wcs(rot=199.9)),
            (1800.0, make_wcs(rot=200.0)),
        ]
        fr = A.field_rotation(samples)
        assert fr.meridian_flip
        assert not fr.ok
        assert math.isnan(fr.rate_deg_per_hour)

    def test_curvature_shows_up_in_residual(self):
        samples = [(i * 600.0, make_wcs(rot=10.0 + 0.3 * (i * 600.0 / 3600.0) ** 2))
                   for i in range(12)]
        fr = A.field_rotation(samples)
        assert fr.rms_deg > 0.05
        assert fr.max_dev_deg > fr.rms_deg

    def test_input_order_does_not_matter(self):
        s = rotation_series(0.5)
        shuffled = [s[i] for i in (5, 0, 9, 2, 11, 1, 7, 3, 10, 4, 8, 6)]
        a = A.field_rotation(s)
        b = A.field_rotation(shuffled)
        assert b.rate_deg_per_hour == pytest.approx(a.rate_deg_per_hour)
        assert b.times_s == pytest.approx(a.times_s)

    def test_separate_times_argument(self):
        s = rotation_series(0.4)
        fr = A.field_rotation([w for _t, w in s], times=[t for t, _w in s])
        assert fr.rate_deg_per_hour == pytest.approx(0.4, rel=1e-9)

    def test_datetime_times(self):
        base = datetime(2026, 7, 26, 22, 0, tzinfo=timezone.utc)
        s = [(base + timedelta(minutes=10 * i), make_wcs(rot=10.0 + 0.5 * i / 6.0))
             for i in range(10)]
        assert A.field_rotation(s).rate_deg_per_hour == pytest.approx(0.5, rel=1e-6)

    def test_skips_failed_solves(self):
        s = [(0.0, fake_result(make_wcs(rot=10.0))),
             (600.0, SimpleNamespace(wcs=None)),
             (1200.0, fake_result(make_wcs(rot=10.0 + 0.5 / 3.0)))]
        fr = A.field_rotation(s)
        assert fr.n == 2 and fr.n_skipped == 1
        assert fr.rate_deg_per_hour == pytest.approx(0.5, rel=1e-6)

    def test_empty_and_single(self):
        assert A.field_rotation([]).n == 0
        assert A.field_rotation([]).ok is False
        one = A.field_rotation([(0.0, make_wcs())])
        assert one.n == 1 and one.ok is False
        assert math.isnan(one.rate_deg_per_hour)

    def test_identical_timestamps_do_not_blow_up(self):
        fr = A.field_rotation([(5.0, make_wcs(rot=1.0)), (5.0, make_wcs(rot=2.0))])
        assert fr.ok is False and math.isnan(fr.rate_deg_per_hour)

    def test_missing_time_raises(self):
        with pytest.raises(A.WcsAppsError):
            A.field_rotation([make_wcs(), make_wcs()])

    def test_times_length_mismatch_raises(self):
        with pytest.raises(A.WcsAppsError):
            A.field_rotation([make_wcs(), make_wcs()], times=[0.0])

    def test_bad_time_type_raises(self):
        with pytest.raises(A.WcsAppsError):
            A.field_rotation([make_wcs()], times=["昨天"])


# ============================================================== 6. 漂移


def drift_series(rate_arcsec_per_min: float, n: int = 10, dt: float = 60.0,
                 dec: float = 22.0, extra=None):
    out = []
    for i in range(n):
        t = i * dt
        east = rate_arcsec_per_min * (t / 60.0) + (extra(i) if extra else 0.0)
        out.append((t, fake_result(make_wcs(83.6 + arcsec_ra(east, dec), dec))))
    return out


class TestDrift:

    def test_recovers_a_known_rate(self):
        d = A.drift(drift_series(1.2))
        assert d.ok
        assert d.rate_ra_arcsec_per_min == pytest.approx(1.2, rel=1e-6)
        assert abs(d.rate_dec_arcsec_per_min) < 1e-4
        assert d.rate_arcsec_per_min == pytest.approx(1.2, rel=1e-6)
        assert d.rms_resid_arcsec < 1e-3

    def test_position_angle_of_a_pure_east_drift(self):
        d = A.drift(drift_series(1.2))
        assert d.rate_pa_deg == pytest.approx(90.0, abs=0.01)

    def test_pure_north_drift(self):
        s = [(i * 60.0, fake_result(make_wcs(83.6, 22.0 + i * 2.0 / 3600.0)))
             for i in range(8)]
        d = A.drift(s)
        assert d.rate_dec_arcsec_per_min == pytest.approx(2.0, rel=1e-6)
        assert d.rate_pa_deg == pytest.approx(0.0, abs=0.01)

    def test_total_and_max_excursion(self):
        d = A.drift(drift_series(1.2))
        assert d.total_arcsec == pytest.approx(1.2 * 9.0, rel=1e-5)
        assert d.max_excursion_arcsec == pytest.approx(1.2 * 9.0, rel=1e-5)

    def test_ra_component_includes_cos_dec(self):
        """同样的赤经步长,在 dec=60 的东向位移只有 dec=0 的一半。"""
        a = A.drift([(i * 60.0, fake_result(make_wcs(10.0 + i * 0.01, 0.0)))
                     for i in range(5)])
        b = A.drift([(i * 60.0, fake_result(make_wcs(10.0 + i * 0.01, 60.0)))
                     for i in range(5)])
        assert b.rate_ra_arcsec_per_min == pytest.approx(
            a.rate_ra_arcsec_per_min * 0.5, rel=1e-3)

    def test_dither_subtraction_changes_the_answer(self):
        """扣 dither 前后必须**明显不同** —— 否则 subtract 是空转的。"""
        s = drift_series(1.2, extra=lambda i: 20.0 if i >= 5 else 0.0)
        raw = A.drift(s)
        fixed = A.drift(s, subtract=[(300.0, 20.0, 0.0)])
        assert raw.rate_ra_arcsec_per_min > 3.0        # 被 dither 顶高
        assert raw.rms_resid_arcsec > 3.0
        assert fixed.rate_ra_arcsec_per_min == pytest.approx(1.2, rel=1e-4)
        assert fixed.rms_resid_arcsec < 1e-2
        assert fixed.raw_ra_arcsec == pytest.approx(raw.ra_arcsec)

    def test_dither_is_zero_order_hold(self):
        s = drift_series(0.0, n=6, extra=lambda i: 10.0 if i >= 3 else 0.0)
        d = A.drift(s, subtract=[(150.0, 10.0, 0.0)])
        assert list(d.dither_ra_arcsec) == [0.0, 0.0, 0.0, 10.0, 10.0, 10.0]
        assert float(np.abs(d.ra_arcsec).max()) < 1e-6

    def test_dither_before_first_entry_is_zero(self):
        s = drift_series(0.0, n=4)
        d = A.drift(s, subtract=[(1e9, 33.0, 44.0)])
        assert float(np.abs(d.dither_ra_arcsec).max()) == 0.0

    def test_dither_two_axis(self):
        s = drift_series(0.0, n=4)
        d = A.drift(s, subtract=[(-1.0, 5.0, -7.0)])
        assert d.ra_arcsec == pytest.approx(np.full(4, -5.0), abs=1e-6)
        assert d.dec_arcsec == pytest.approx(np.full(4, 7.0), abs=1e-6)

    def test_reference_frame_selection(self):
        d0 = A.drift(drift_series(1.2), ref=0)
        d9 = A.drift(drift_series(1.2), ref=9)
        assert d0.ra_arcsec[0] == pytest.approx(0.0, abs=1e-9)
        assert d9.ra_arcsec[-1] == pytest.approx(0.0, abs=1e-9)
        assert d9.rate_ra_arcsec_per_min == pytest.approx(
            d0.rate_ra_arcsec_per_min, rel=1e-6)

    def test_bad_ref_raises(self):
        with pytest.raises(A.WcsAppsError):
            A.drift(drift_series(1.0), ref=99)

    def test_center_comes_from_the_frame_not_crval(self):
        """CRPIX 不在图幅中心时,漂移必须按**图幅中心**算。"""
        off = make_wcs(83.6, 22.0, crpix=(1.0, 1.0))
        ctr = A.drift([(0.0, off), (60.0, off)], width=FRAME_W,
                      height=FRAME_H).centers[0]
        expect = pixel_to_world(off, CENTER_PX[0], CENTER_PX[1])
        assert ctr == pytest.approx(expect, abs=1e-9)
        assert abs(ctr[0] - off.crval[0]) > 1e-3

    def test_falls_back_to_crval_without_size(self):
        w = make_wcs(83.6, 22.0, crpix=(1.0, 1.0))
        d = A.drift([(0.0, w), (60.0, w)])
        assert d.centers[0] == pytest.approx(np.asarray(w.crval), abs=1e-12)

    def test_empty_and_single(self):
        assert A.drift([]).n == 0
        one = A.drift(drift_series(1.0, n=1))
        assert one.n == 1 and one.ok is False

    def test_subtract_row_shape_raises(self):
        with pytest.raises(A.WcsAppsError):
            A.drift(drift_series(1.0), subtract=[(0.0, 1.0)])

    def test_far_apart_frames_raise(self):
        s = [(0.0, fake_result(make_wcs(0.0, 0.0))),
             (60.0, fake_result(make_wcs(120.0, 0.0)))]
        with pytest.raises(A.WcsAppsError):
            A.drift(s)


# ========================================================== 7. 叠加对齐


class TestStackAlignment:

    def test_identity(self):
        w = make_wcs()
        sa = A.stack_alignment([w, w], width=FRAME_W, height=FRAME_H)
        assert sa.n == 2
        assert sa.dx_px == pytest.approx([0.0, 0.0], abs=1e-6)
        assert sa.dy_px == pytest.approx([0.0, 0.0], abs=1e-6)
        assert sa.rotation_deg == pytest.approx([0.0, 0.0], abs=1e-9)
        assert sa.scale == pytest.approx([1.0, 1.0], rel=1e-12)
        assert float(sa.rms_px.max()) < 1e-6

    def test_overlap_of_a_frame_with_itself_is_one(self):
        """采样点取格心而不是外边界 —— 取边界的话往返浮点会把重叠报成 0.88。"""
        w = make_wcs()
        sa = A.stack_alignment([w], width=FRAME_W, height=FRAME_H)
        assert sa.overlap_frac[0] == pytest.approx(1.0)

    def test_pure_shift(self):
        shift_px = 100.0
        w0 = make_wcs(83.6, 22.0)
        ra, dec = pixel_to_world(w0, CENTER_PX[0] + shift_px, CENTER_PX[1])
        w1 = make_wcs(ra, dec)
        sa = A.stack_alignment([w0, w1], width=FRAME_W, height=FRAME_H)
        assert sa.dx_px[1] == pytest.approx(shift_px, rel=2e-3)
        assert abs(sa.dy_px[1]) < 1.0
        assert sa.shift_px[1] == pytest.approx(abs(sa.dx_px[1]), rel=1e-6)
        assert abs(sa.rotation_deg[1]) < 0.05
        assert sa.rms_px[1] < 0.2
        assert sa.overlap_frac[1] == pytest.approx(
            1.0 - shift_px / FRAME_W, rel=0.02)

    def test_pure_rotation(self):
        sa = A.stack_alignment([make_wcs(rot=0.0), make_wcs(rot=3.0)],
                               width=FRAME_W, height=FRAME_H)
        assert abs(sa.rotation_deg[1]) == pytest.approx(3.0, rel=1e-6)
        assert sa.shift_px[1] < 1e-6
        assert sa.scale[1] == pytest.approx(1.0, rel=1e-9)
        assert sa.rms_px[1] < 1e-6
        # 转 3° 之后四个角转出框外,重叠掉一点点(精确裁剪实测 0.9730)
        assert sa.overlap_frac[1] == pytest.approx(0.9730, abs=2e-3)

    def test_scale_change(self):
        """尺度变了 ⇒ scale 认得出来;重叠比例是**参考帧**有多少落在该帧里,
        所以视场变大的那张对参考帧是全包(1.0),反过来才按 1/k² 掉。"""
        small, big = make_wcs(scale=SCALE), make_wcs(scale=SCALE * 1.02)
        sa = A.stack_alignment([small, big], width=FRAME_W, height=FRAME_H)
        assert sa.scale[1] == pytest.approx(1.02, rel=1e-4)
        assert sa.overlap_frac[1] == pytest.approx(1.0)
        rev = A.stack_alignment([big, small], width=FRAME_W, height=FRAME_H)
        assert rev.scale[1] == pytest.approx(1.0 / 1.02, rel=1e-4)
        # 精确求交:和解析值 1/1.02² 只差 2.5e-5(撒点法差 4%,根本看不出来)
        assert rev.overlap_frac[1] == pytest.approx(1.0 / 1.02 ** 2, rel=1e-3)

    def test_overlap_is_exact_not_sampled(self):
        """半幅平移 ⇒ 重叠约 1/2;完全错开 ⇒ 0。"""
        w0 = make_wcs()
        ra, dec = pixel_to_world(w0, CENTER_PX[0] + FRAME_W / 2.0, CENTER_PX[1])
        half = A.stack_alignment([w0, make_wcs(ra, dec)],
                                 width=FRAME_W, height=FRAME_H)
        assert half.overlap_frac[1] == pytest.approx(0.5, abs=5e-3)
        ra2, dec2 = pixel_to_world(w0, CENTER_PX[0] + FRAME_W * 1.5, CENTER_PX[1])
        away = A.stack_alignment([w0, make_wcs(ra2, dec2)],
                                 width=FRAME_W, height=FRAME_H)
        assert away.overlap_frac[1] == 0.0

    def test_overlap_agrees_with_the_coverage_grid(self):
        """拿 coverage 的格点计数当**独立实现**交叉验证多边形裁剪。"""
        w0 = make_wcs()
        ra, dec = pixel_to_world(w0, CENTER_PX[0] + 1500.0, CENTER_PX[1] + 700.0)
        w1 = make_wcs(ra, dec, rot=2.0)
        sa = A.stack_alignment([w0, w1], width=FRAME_W, height=FRAME_H)
        cov = A.coverage([w0, w1], width=FRAME_W, height=FRAME_H, grid=768)
        by_grid = cov.depth_area_deg2[2] / cov.frame_area_deg2[0]
        assert sa.overlap_frac[1] == pytest.approx(by_grid, rel=5e-3)

    def test_parity_mismatch_is_flagged_and_unfittable(self):
        sa = A.stack_alignment([make_wcs(flip=True), make_wcs(flip=False)],
                               width=FRAME_W, height=FRAME_H)
        assert list(sa.parity_mismatch) == [False, True]
        assert sa.rms_px[1] > 100.0          # 非反射相似变换根本套不上
        assert list(sa.usable()) == [True, False]

    def test_usable_thresholds_are_caller_policy(self):
        w1 = make_wcs(83.6 + arcsec_ra(FOV_W * 3600.0 * 0.8, 22.0), 22.0)
        sa = A.stack_alignment([make_wcs(), w1], width=FRAME_W, height=FRAME_H)
        assert bool(sa.usable(min_overlap=0.5)[1]) is False
        assert bool(sa.usable(min_overlap=0.05)[1]) is True

    def test_reference_can_be_any_index(self):
        frames = [make_wcs(rot=0.0), make_wcs(rot=2.0), make_wcs(rot=5.0)]
        sa = A.stack_alignment(frames, ref=1, width=FRAME_W, height=FRAME_H)
        assert sa.ref == 1
        assert sa.rotation_deg[1] == pytest.approx(0.0, abs=1e-9)
        assert abs(sa.rotation_deg[0]) == pytest.approx(2.0, rel=1e-6)
        assert abs(sa.rotation_deg[2]) == pytest.approx(3.0, rel=1e-6)

    def test_skipped_frames_keep_original_indices(self):
        frames = [fake_result(make_wcs()), SimpleNamespace(wcs=None),
                  fake_result(make_wcs(rot=1.0))]
        sa = A.stack_alignment(frames)
        assert list(sa.index) == [0, 2]
        assert sa.n_skipped == 1
        assert sa.n == 2

    def test_rows_view(self):
        sa = A.stack_alignment([make_wcs(), make_wcs(rot=1.0)],
                               width=FRAME_W, height=FRAME_H)
        rows = sa.rows()
        assert len(rows) == 2
        assert rows[0].index == 0
        assert isinstance(rows[1].parity_mismatch, bool)

    def test_empty_input(self):
        sa = A.stack_alignment([])
        assert sa.n == 0 and sa.index.size == 0
        assert sa.usable().size == 0

    def test_missing_size_raises(self):
        with pytest.raises(A.WcsAppsError):
            A.stack_alignment([make_wcs(), make_wcs()])

    def test_ref_without_wcs_raises(self):
        with pytest.raises(A.WcsAppsError):
            A.stack_alignment([SimpleNamespace(wcs=None), fake_result(make_wcs())],
                              ref=0)

    def test_bad_samples_raises(self):
        with pytest.raises(A.WcsAppsError):
            A.stack_alignment([make_wcs()], width=FRAME_W, height=FRAME_H,
                              samples=1)


# ======================================================== 8. 盲解搜索计划


class TestSkyGrid:

    @pytest.mark.parametrize("radius", [5.0, 15.0, 30.0])
    def test_covers_every_sky_point(self, radius):
        """完备性:**解析最坏点**到最近中心的角距不许超过搜索半径。"""
        g = A.sky_grid(radius)
        assert worst_gap_deg(g, worst_case_points(g)) <= radius

    def test_covers_every_sky_point_small_radius(self):
        g = A.sky_grid(2.0)
        rng = np.random.default_rng(11)
        z = rng.uniform(-1.0, 1.0, 4000)
        pts = np.column_stack([rng.uniform(0.0, 360.0, 4000),
                               np.degrees(np.arcsin(z))])
        assert worst_gap_deg(g, pts) <= 2.0

    def test_dec_band_is_still_complete(self):
        lo, hi = -59.0, 90.0
        g = A.sky_grid(3.0, dec_range=(lo, hi))
        cand = worst_case_points(g)
        cand = cand[(cand[:, 1] >= lo) & (cand[:, 1] <= hi)]
        assert worst_gap_deg(g, cand) <= 3.0

    def test_dec_band_edge_is_covered(self):
        lo, hi = 20.0, 25.0
        g = A.sky_grid(4.0, dec_range=(lo, hi))
        edge = np.column_stack([np.linspace(0.0, 360.0, 720), np.full(720, lo)])
        assert worst_gap_deg(g, edge) <= 4.0

    def test_dec_band_reduces_the_count(self):
        assert len(A.sky_grid(5.0, dec_range=(-59.0, 90.0))) < len(A.sky_grid(5.0))

    def test_both_poles_are_present(self):
        g = A.sky_grid(5.0)
        assert float(g[:, 1].min()) == -90.0
        assert float(g[:, 1].max()) == 90.0
        assert int(np.sum(np.abs(g[:, 1]) >= 90.0 - 1e-9)) == 2   # 极点不重复

    def test_count_scales_like_inverse_radius_squared(self):
        n2 = len(A.sky_grid(2.0))
        n4 = len(A.sky_grid(4.0))
        assert 3.0 < n2 / n4 < 5.0

    @pytest.mark.parametrize("radius", [2.0, 5.0, 15.0])
    def test_grid_is_not_wastefully_dense(self, radius):
        """点数不能远超"半径 r 的圆盘铺满天球"的理论下限。

        环内赤经步长忘了按 cos δ 收缩的话,靠近天极的环会挤出一堆多余的点
        (r=5 时 904 → 1377),完备性照样成立、只是白搭 50% 的解算时间 ——
        所以这条必须单独卡,完备性测试是抓不到的。
        """
        ideal = 41252.96 / (math.pi * radius ** 2)
        assert len(A.sky_grid(radius)) < 2.2 * ideal

    def test_near_sorts_by_distance(self):
        near = (83.6, 22.0)
        g = A.sky_grid(5.0, near=near)
        sep = np.asarray(angular_separation(near[0], near[1], g[:, 0], g[:, 1]))
        assert bool(np.all(np.diff(sep) >= -1e-9))
        assert sep[0] < 5.0

    def test_all_centers_are_in_range(self):
        g = A.sky_grid(7.0)
        assert bool(np.all((g[:, 0] >= 0.0) & (g[:, 0] < 360.0)))
        assert bool(np.all(np.abs(g[:, 1]) <= 90.0))

    def test_invalid_radius_raises(self):
        for bad in (0.0, -1.0, 91.0, float("nan")):
            with pytest.raises(A.WcsAppsError):
                A.sky_grid(bad)

    def test_invalid_dec_range_raises(self):
        with pytest.raises(A.WcsAppsError):
            A.sky_grid(5.0, dec_range=(30.0, 10.0))


class TestBlindHintGrid:

    def test_radius_comes_from_the_field(self):
        hints = A.blind_hint_grid(SCALE, (FRAME_W, FRAME_H))
        expect = 0.5 * math.hypot(FRAME_W, FRAME_H) * SCALE / 3600.0
        assert hints[0].radius_deg == pytest.approx(expect)
        assert len(hints) == len(A.sky_grid(expect))

    def test_hints_are_solvable_shaped(self):
        hints = A.blind_hint_grid(SCALE, (FRAME_W, FRAME_H))
        h = hints[0]
        assert isinstance(h, SolveHint)
        assert h.has_pointing
        assert h.pixel_scale_arcsec() == pytest.approx(SCALE)
        assert h.image_size == (FRAME_W, FRAME_H)
        assert h.field_radius_deg() == pytest.approx(h.radius_deg)
        # 整条是一个 msgid,所以比整条(总数从 hints 长度来)
        assert h.source == tr("盲解网格 %d/%d") % (1, len(hints))

    def test_every_sky_point_falls_inside_some_hint(self):
        """完备性的业务表述:任意天球点都在某个 hint 的搜索半径内。"""
        hints = A.blind_hint_grid(SCALE, (FRAME_W, FRAME_H), radius_deg=8.0)
        g = np.array([[h.ra_deg, h.dec_deg] for h in hints])
        assert worst_gap_deg(g, worst_case_points(g)) <= 8.0

    def test_explicit_radius_overrides(self):
        hints = A.blind_hint_grid(SCALE, (FRAME_W, FRAME_H), radius_deg=10.0)
        assert hints[0].radius_deg == 10.0
        assert len(hints) == len(A.sky_grid(10.0))

    def test_base_hint_fields_are_kept(self):
        base = SolveHint(scale_tol=0.05, flipped=True, epoch=2026.5,
                         rotation_deg=137.0, rotation_tol_deg=5.0)
        h = A.blind_hint_grid(SCALE, (FRAME_W, FRAME_H), base_hint=base)[0]
        assert (h.scale_tol, h.flipped, h.epoch) == (0.05, True, 2026.5)
        assert (h.rotation_deg, h.rotation_tol_deg) == (137.0, 5.0)
        assert h.ra_deg is not None            # 但指向被网格覆盖了

    def test_overrides_win(self):
        h = A.blind_hint_grid(SCALE, (FRAME_W, FRAME_H), scale_tol=0.2,
                              flipped=False)[0]
        assert h.scale_tol == 0.2 and h.flipped is False

    def test_dec_range_and_near_are_forwarded(self):
        near = (83.6, 22.0)
        hints = A.blind_hint_grid(SCALE, (FRAME_W, FRAME_H), radius_deg=6.0,
                                  dec_range=(-30.0, 90.0), near=near)
        assert all(h.dec_deg >= -40.0 for h in hints)
        first = angular_separation(near[0], near[1], hints[0].ra_deg,
                                   hints[0].dec_deg)
        last = angular_separation(near[0], near[1], hints[-1].ra_deg,
                                  hints[-1].dec_deg)
        assert first < last
        assert hints[0].source == tr("盲解网格 %d/%d") % (1, len(hints))

    def test_without_size_or_radius_raises(self):
        with pytest.raises(A.WcsAppsError):
            A.blind_hint_grid(SCALE)

    def test_radius_only_is_enough(self):
        hints = A.blind_hint_grid(SCALE, radius_deg=20.0)
        assert hints[0].image_size is None
        assert hints[0].radius_deg == 20.0

    def test_bad_pixel_scale_raises(self):
        for bad in (0.0, -1.0, float("nan")):
            with pytest.raises(A.WcsAppsError):
                A.blind_hint_grid(bad, (FRAME_W, FRAME_H))

    def test_bad_image_size_raises(self):
        with pytest.raises(A.WcsAppsError):
            A.blind_hint_grid(SCALE, (0, FRAME_H))
