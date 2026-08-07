"""astro_smb.wcs 的离线单测:合成 WCS + 合成星对,不碰网络也不碰真机文件。

重点钉死四件板解算里最容易错、错了还"看着像能跑"的事:

1. **坐标约定** —— crpix 是 FITS 1-based;numpy 数组行序是翻转的。
   :func:`array_to_fits_xy` 一旦写反,解算会稳定地收敛到一个**镜像**的错解。
2. **过极点 / RA 跨 0°** —— 教科书闭式在 ρ→0 和极点处要加特例;本实现走
   单位向量三元组,单测把这些边界全走一遍。
3. **CRPIX 必须与 CD 一起解** —— 假定一个 CRPIX 只解 CD,残差会大到几十上百
   像素(``TestCrpixMustBeSolvedJointly``)。
4. **sigma 剔除用 MAD 而不是 std** —— 误匹配残差几十像素,std 会被它自己顶高。

真机对账(不在单测里,靠探针跑过一次)见任务报告:M 31 核心的预测像素与实测
最亮块相距 13 px、镜像假设差 641 px;fit_tan 对真机 SIP 的残差复现了侦察报告
独立量出的畸变地板(NGC2237 0.19~0.28 / M31 0.69~0.71 / NGC1499 1.34~1.36 /
M16 1.33~1.70 px)。
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from astro_smb import wcs as W
from astro_smb.fitshdr import parse_fits_header

# ----------------------------------------------------------------- 合成工具

FRAME_W, FRAME_H = 6248, 4176
CENTER = ((FRAME_W + 1) / 2.0, (FRAME_H + 1) / 2.0)


def make_wcs(ra: float = 83.6, dec: float = 22.0, scale: float = 1.93,
             rot: float = 0.0, flip: bool = False, crpix=CENTER) -> W.TanWcs:
    """造一份 TAN WCS。``scale`` 角秒/像素,``rot`` 为 +y 轴位置角(北起东向)。"""
    s = scale / 3600.0
    r = math.radians(rot)
    if flip:
        cd = s * np.array([[math.cos(r), math.sin(r)],
                           [-math.sin(r), math.cos(r)]])
    else:
        cd = s * np.array([[-math.cos(r), math.sin(r)],
                           [math.sin(r), math.cos(r)]])
    return W.TanWcs((ra, dec), crpix, cd)


def frame_pixels(n: int = 400, seed: int = 7, margin: int = 60):
    """帧内随机像素坐标 ``(n, 2)``(FITS 1-based)。"""
    rng = np.random.default_rng(seed)
    x = rng.uniform(margin, FRAME_W - margin, n)
    y = rng.uniform(margin, FRAME_H - margin, n)
    return np.column_stack([x, y])


def pairs_from(wcs: W.TanWcs, xy: np.ndarray) -> np.ndarray:
    """由真值 WCS 生成对应的天球坐标 ``(n, 2)``。"""
    ra, dec = W.pixel_to_world(wcs, xy[:, 0], xy[:, 1])
    return np.column_stack([ra, dec])


def fits_header_bytes(extra: dict[str, object]) -> bytes:
    """造一份只有头的合成 FITS 字节流(供 from_fits_cards 吃 FitsHeader)。"""
    cards = [("SIMPLE", True), ("BITPIX", 16), ("NAXIS", 2),
             ("NAXIS1", FRAME_W), ("NAXIS2", FRAME_H)]
    cards += list(extra.items())
    out = bytearray()
    for key, value in cards:
        if isinstance(value, bool):
            v = "T" if value else "F"
        elif isinstance(value, str):
            v = "'%s'" % value.ljust(8)
        else:
            v = str(value)
        out += f"{key:<8}= {v:>20}".ljust(80)[:80].encode("ascii")
    out += b"END".ljust(80)
    while len(out) % 2880:
        out += b" " * 80
    return bytes(out)


# ------------------------------------------------------------- 球面基础工具


class TestSphericalHelpers:
    def test_radec_to_unit_known(self):
        assert np.allclose(W.radec_to_unit(0.0, 0.0), [1, 0, 0], atol=1e-15)
        assert np.allclose(W.radec_to_unit(90.0, 0.0), [0, 1, 0], atol=1e-15)
        assert np.allclose(W.radec_to_unit(0.0, 90.0), [0, 0, 1], atol=1e-15)
        assert np.allclose(W.radec_to_unit(123.0, -90.0), [0, 0, -1], atol=1e-15)

    def test_unit_roundtrip(self):
        rng = np.random.default_rng(1)
        ra = rng.uniform(0, 360, 500)
        dec = np.degrees(np.arcsin(rng.uniform(-1, 1, 500)))
        back_ra, back_dec = W.unit_to_radec(W.radec_to_unit(ra, dec))
        assert np.max(np.abs(back_dec - dec)) < 1e-12
        assert np.max(np.abs((back_ra - ra + 180) % 360 - 180)) < 1e-12

    def test_unit_to_radec_ignores_norm(self):
        """不要求归一化 —— 反投影直接喂未归一化向量。"""
        ra, dec = W.unit_to_radec(np.array([3.0, 0.0, 3.0]))
        assert ra == pytest.approx(0.0)
        assert dec == pytest.approx(45.0)

    def test_unit_to_radec_bad_shape(self):
        with pytest.raises(W.WcsError):
            W.unit_to_radec(np.zeros((4, 2)))

    def test_angular_separation_known(self):
        assert W.angular_separation(0, 0, 0, 0) == pytest.approx(0.0)
        assert W.angular_separation(0, 0, 90, 0) == pytest.approx(90.0)
        assert W.angular_separation(0, 0, 180, 0) == pytest.approx(180.0)
        assert W.angular_separation(0, 90, 0, -90) == pytest.approx(180.0)
        assert W.angular_separation(10, 30, 10, 40) == pytest.approx(10.0)

    def test_angular_separation_crosses_ra_zero(self):
        assert W.angular_separation(359.5, 0, 0.5, 0) == pytest.approx(1.0)

    def test_angular_separation_tiny(self):
        """1 mas 量级也不掉精度(Vincenty 形式的意义所在)。"""
        mas = 1.0 / 3600.0 / 1000.0
        assert W.angular_separation(0, 0, mas, 0) == pytest.approx(mas, rel=1e-9)

    def test_angular_separation_vectorized(self):
        ra = np.array([0.0, 10.0, 20.0])
        sep = W.angular_separation(ra, 0.0, ra + 1.0, 0.0)
        assert sep.shape == (3,)
        assert np.allclose(sep, 1.0)


# ------------------------------------------------------- 数组 ↔ FITS 像素坐标


class TestPixelConvention:
    def test_array_to_fits_corners(self):
        h = FRAME_H
        assert W.array_to_fits_xy(0, 0, h) == (1.0, float(h))        # 顶行
        assert W.array_to_fits_xy(0, h - 1, h) == (1.0, 1.0)          # 底行
        assert W.array_to_fits_xy(5, 2, h) == (6.0, float(h - 2))

    def test_roundtrip(self):
        col, row = 137.25, 891.5
        x, y = W.array_to_fits_xy(col, row, FRAME_H)
        back_c, back_r = W.fits_to_array_xy(x, y, FRAME_H)
        assert back_c == pytest.approx(col)
        assert back_r == pytest.approx(row)

    def test_vectorized(self):
        cols = np.arange(5.0)
        rows = np.arange(5.0)
        x, y = W.array_to_fits_xy(cols, rows, FRAME_H)
        assert x.shape == (5,) and y.shape == (5,)
        assert np.allclose(x, cols + 1)
        assert np.allclose(y, FRAME_H - rows)


# ------------------------------------------------------------------ 投影


class TestProjectionRoundTrip:
    def _grid(self):
        gx, gy = np.meshgrid(np.linspace(1, FRAME_W, 30),
                             np.linspace(1, FRAME_H, 30))
        return gx.ravel(), gy.ravel()

    def _check(self, wcs, tol_px=1e-8):
        gx, gy = self._grid()
        ra, dec = W.pixel_to_world(wcs, gx, gy)
        bx, by = W.world_to_pixel(wcs, ra, dec)
        assert np.max(np.abs(bx - gx)) < tol_px
        assert np.max(np.abs(by - gy)) < tol_px

    def test_typical_field(self):
        self._check(make_wcs())

    def test_near_north_pole(self):
        self._check(make_wcs(dec=89.99))

    def test_exact_north_pole(self):
        self._check(make_wcs(dec=90.0))

    def test_exact_south_pole(self):
        self._check(make_wcs(dec=-90.0))

    def test_near_south_pole(self):
        self._check(make_wcs(dec=-89.99))

    def test_ra_zero_crossing(self):
        self._check(make_wcs(ra=0.0))

    def test_ra_just_below_360(self):
        self._check(make_wcs(ra=359.95))

    def test_wide_field(self):
        """5° 级视场(短焦)也精确。"""
        self._check(make_wcs(scale=10.0))

    def test_rotated_and_flipped(self):
        self._check(make_wcs(rot=137.0, flip=True))

    def test_world_pixel_world_roundtrip(self):
        wcs = make_wcs(ra=0.05, dec=-31.0, rot=42.0)
        rng = np.random.default_rng(3)
        ra0 = (wcs.crval[0] + rng.uniform(-1.5, 1.5, 300)) % 360.0
        dec0 = wcs.crval[1] + rng.uniform(-1.0, 1.0, 300)
        x, y = W.world_to_pixel(wcs, ra0, dec0)
        ra1, dec1 = W.pixel_to_world(wcs, x, y)
        assert np.max(W.angular_separation(ra0, dec0, ra1, dec1)) < 1e-11

    def test_crval_maps_to_crpix(self):
        wcs = make_wcs(ra=275.27, dec=-14.04, rot=269.3, flip=True)
        x, y = W.world_to_pixel(wcs, wcs.crval[0], wcs.crval[1])
        assert x == pytest.approx(wcs.crpix[0], abs=1e-9)
        assert y == pytest.approx(wcs.crpix[1], abs=1e-9)

    def test_crpix_maps_to_crval(self):
        wcs = make_wcs(ra=275.27, dec=-14.04)
        ra, dec = W.pixel_to_world(wcs, wcs.crpix[0], wcs.crpix[1])
        assert W.angular_separation(ra, dec, *wcs.crval) < 1e-12


class TestProjectionEdges:
    def test_back_hemisphere_is_nan(self):
        """切平面背面无法投影,必须给 NaN 而不是一个看似合理的大数。"""
        wcs = make_wcs(ra=0.0, dec=0.0)
        x, y = W.world_to_pixel(wcs, 180.0, 0.0)
        assert math.isnan(x) and math.isnan(y)

    def test_exactly_ninety_degrees_is_nan(self):
        wcs = make_wcs(ra=0.0, dec=0.0)
        x, y = W.world_to_pixel(wcs, 90.0, 0.0)
        assert math.isnan(x) and math.isnan(y)

    def test_mixed_valid_and_back(self):
        wcs = make_wcs(ra=0.0, dec=0.0)
        ra = np.array([0.0, 0.5, 180.0, 179.0])
        x, y = W.world_to_pixel(wcs, ra, 0.0)
        assert np.isfinite(x[:2]).all()
        assert np.isnan(x[2:]).all() and np.isnan(y[2:]).all()

    def test_scalar_in_scalar_out(self):
        wcs = make_wcs()
        x, y = W.world_to_pixel(wcs, wcs.crval[0], wcs.crval[1])
        assert isinstance(x, float) and isinstance(y, float)
        ra, dec = W.pixel_to_world(wcs, 100.0, 200.0)
        assert isinstance(ra, float) and isinstance(dec, float)

    def test_array_shapes_preserved(self):
        wcs = make_wcs()
        xs = np.linspace(1, FRAME_W, 12).reshape(3, 4)
        ys = np.linspace(1, FRAME_H, 12).reshape(3, 4)
        ra, dec = W.pixel_to_world(wcs, xs, ys)
        assert ra.shape == (3, 4) and dec.shape == (3, 4)
        bx, by = W.world_to_pixel(wcs, ra, dec)
        assert bx.shape == (3, 4) and by.shape == (3, 4)

    def test_broadcasting(self):
        wcs = make_wcs()
        ra, dec = W.pixel_to_world(wcs, np.array([1.0, 2.0, 3.0]), 100.0)
        assert ra.shape == (3,)

    def test_analytic_offsets(self):
        """1"/px、北上东左:+3600 px 沿 y = 正北 1°(含 gnomonic 的 atan 修正)。"""
        wcs = W.TanWcs((0.0, 0.0), (0.0, 0.0),
                       np.array([[-1 / 3600.0, 0.0], [0.0, 1 / 3600.0]]))
        expect = math.degrees(math.atan(math.radians(1.0)))
        ra, dec = W.pixel_to_world(wcs, 0.0, 3600.0)
        assert dec == pytest.approx(expect, abs=1e-12)
        assert ra == pytest.approx(0.0, abs=1e-12)
        # +x 是西(赤经减小)
        ra2, dec2 = W.pixel_to_world(wcs, 3600.0, 0.0)
        assert ra2 == pytest.approx(360.0 - expect, abs=1e-12)
        assert dec2 == pytest.approx(0.0, abs=1e-12)


# ------------------------------------------------------------- TanWcs 派生量


class TestTanWcsDerived:
    def test_pixel_scale(self):
        assert make_wcs(scale=1.93).pixel_scale() == pytest.approx(1.93)
        assert make_wcs(scale=1.93, rot=57.0).pixel_scale() == pytest.approx(1.93)

    def test_pixel_scale_xy_asymmetric(self):
        cd = np.array([[-2.0 / 3600.0, 0.0], [0.0, 3.0 / 3600.0]])
        wcs = W.TanWcs((10.0, 20.0), (1.0, 1.0), cd)
        sx, sy = wcs.pixel_scale_xy()
        assert sx == pytest.approx(2.0)
        assert sy == pytest.approx(3.0)
        assert wcs.pixel_scale() == pytest.approx(math.sqrt(6.0))

    def test_rotation_zero_is_north_up_east_left(self):
        wcs = make_wcs(rot=0.0)
        assert wcs.rotation_deg() == pytest.approx(0.0, abs=1e-9)
        # +y 确实朝北
        ra, dec = W.pixel_to_world(wcs, wcs.crpix[0], wcs.crpix[1] + 500)
        assert dec > wcs.crval[1]
        # +x 确实朝西(赤经减小)
        ra2, _ = W.pixel_to_world(wcs, wcs.crpix[0] + 500, wcs.crpix[1])
        assert ((ra2 - wcs.crval[0] + 180) % 360) - 180 < 0

    @pytest.mark.parametrize("rot", [0.0, 30.0, 88.5, 180.0, 268.4, 359.5])
    def test_rotation_recovered(self, rot):
        assert make_wcs(rot=rot).rotation_deg() == pytest.approx(rot, abs=1e-9)

    @pytest.mark.parametrize("rot", [0.0, 30.0, 268.4])
    def test_rotation_recovered_flipped(self, rot):
        assert make_wcs(rot=rot, flip=True).rotation_deg() == pytest.approx(rot, abs=1e-9)

    def test_flipped_sign_convention(self):
        """常规取向(北上东左)det < 0;镜像 det > 0。"""
        assert make_wcs(flip=False).flipped() is False
        assert make_wcs(flip=False).det() < 0
        assert make_wcs(flip=True).flipped() is True
        assert make_wcs(flip=True).det() > 0

    def test_cd_inv(self):
        wcs = make_wcs(rot=33.0)
        assert np.allclose(wcs.cd @ wcs.cd_inv(), np.eye(2), atol=1e-15)

    def test_fov(self):
        wcs = make_wcs(scale=1.93)
        fw, fh = wcs.fov_deg(FRAME_W, FRAME_H)
        nominal_w = FRAME_W * 1.93 / 3600.0
        nominal_h = FRAME_H * 1.93 / 3600.0
        assert fw == pytest.approx(nominal_w, rel=2e-3)
        assert fh == pytest.approx(nominal_h, rel=2e-3)
        # gnomonic:切平面上等距对应的角距略小于标称
        assert fw < nominal_w and fh < nominal_h

    def test_fov_rotation_invariant(self):
        a = make_wcs(rot=0.0).fov_deg(FRAME_W, FRAME_H)
        b = make_wcs(rot=90.0).fov_deg(FRAME_W, FRAME_H)
        assert a[0] == pytest.approx(b[0], rel=1e-9)
        assert a[1] == pytest.approx(b[1], rel=1e-9)

    def test_repr_is_readable(self):
        text = repr(make_wcs())
        assert "TanWcs" in text and "scale=" in text


class TestTanWcsValidation:
    def test_singular_cd(self):
        with pytest.raises(W.WcsError):
            W.TanWcs((0.0, 0.0), (1.0, 1.0), np.array([[1.0, 2.0], [2.0, 4.0]]))

    def test_zero_cd(self):
        with pytest.raises(W.WcsError):
            W.TanWcs((0.0, 0.0), (1.0, 1.0), np.zeros((2, 2)))

    def test_bad_cd_shape(self):
        with pytest.raises(W.WcsError):
            W.TanWcs((0.0, 0.0), (1.0, 1.0), np.array([1.0, 2.0, 3.0]))

    def test_nan_cd(self):
        cd = np.array([[1.0, 0.0], [0.0, np.nan]])
        with pytest.raises(W.WcsError):
            W.TanWcs((0.0, 0.0), (1.0, 1.0), cd)

    def test_nan_crval(self):
        with pytest.raises(W.WcsError):
            make_wcs(ra=float("nan"))

    def test_dec_out_of_range(self):
        with pytest.raises(W.WcsError):
            make_wcs(dec=91.0)

    def test_ra_normalized(self):
        assert make_wcs(ra=-10.0).crval[0] == pytest.approx(350.0)
        assert make_wcs(ra=370.0).crval[0] == pytest.approx(10.0)

    def test_crval_not_a_pair(self):
        with pytest.raises(W.WcsError):
            W.TanWcs((1.0,), (1.0, 1.0), np.eye(2))


# ------------------------------------------------------------------ 拟合


class TestFitTan:
    def test_exact_recovery_with_crpix_guess(self):
        truth = make_wcs(ra=98.0017, dec=5.401, rot=268.43, flip=True)
        xy = frame_pixels()
        fit, rms, resid = W.fit_tan(xy, pairs_from(truth, xy), crpix_guess=CENTER)
        assert rms < 1e-9
        assert resid.shape == (len(xy), 2)
        assert fit.crpix == pytest.approx(CENTER)
        assert W.angular_separation(*fit.crval, *truth.crval) * 3600.0 < 1e-6
        assert np.max(np.abs(fit.cd - truth.cd)) < 1e-14

    def test_recovery_without_crpix_guess(self):
        """不给 CRPIX 先验时参数化不同(切点 = 星的方向平均),但天球映射必须等价。

        切点落在离图幅中心几十像素的地方,射影 vs 仿射的差异带来约 0.01 px
        的参数化残差 —— 远低于质心精度,可以忽略;见 fit_tan docstring 的表。
        """
        truth = make_wcs(ra=10.457, dec=40.613, rot=88.5, flip=True)
        xy = frame_pixels()
        fit, rms, _ = W.fit_tan(xy, pairs_from(truth, xy))
        assert rms < 0.02
        probe = frame_pixels(n=50, seed=99)
        ra_a, dec_a = W.pixel_to_world(fit, probe[:, 0], probe[:, 1])
        ra_b, dec_b = W.pixel_to_world(truth, probe[:, 0], probe[:, 1])
        assert np.max(W.angular_separation(ra_a, dec_a, ra_b, dec_b)) * 3600 < 0.1

    def test_three_pairs_is_exact(self):
        truth = make_wcs()
        xy = np.array([[100.0, 200.0], [5000.0, 400.0], [3000.0, 3900.0]])
        fit, rms, _ = W.fit_tan(xy, pairs_from(truth, xy))
        assert rms < 1e-8

    def test_noise_residual_matches_injection(self):
        """每轴注入 0.5 px 噪声 ⇒ 二维 RMS ≈ 0.5·√2 ≈ 0.71 px。"""
        truth = make_wcs()
        xy = frame_pixels(n=300, seed=11)
        radec = pairs_from(truth, xy)
        rng = np.random.default_rng(2024)
        noisy = xy + rng.normal(0.0, 0.5, xy.shape)
        fit, rms, _ = W.fit_tan(noisy, radec, crpix_guess=CENTER)
        assert 0.60 < rms < 0.85
        assert W.angular_separation(*fit.crval, *truth.crval) * 3600.0 < 0.5
        assert fit.pixel_scale() == pytest.approx(truth.pixel_scale(), rel=1e-3)

    def test_fit_across_ra_zero(self):
        truth = make_wcs(ra=0.2, dec=12.0, rot=15.0)
        xy = frame_pixels()
        fit, rms, _ = W.fit_tan(xy, pairs_from(truth, xy), crpix_guess=CENTER)
        assert rms < 1e-9
        assert W.angular_separation(*fit.crval, *truth.crval) * 3600.0 < 1e-6

    def test_fit_near_pole(self):
        truth = make_wcs(ra=120.0, dec=88.5, rot=200.0)
        xy = frame_pixels()
        fit, rms, _ = W.fit_tan(xy, pairs_from(truth, xy), crpix_guess=CENTER)
        assert rms < 1e-9
        assert W.angular_separation(*fit.crval, *truth.crval) * 3600.0 < 1e-6

    def test_far_tangent_point_costs_precision(self):
        """切点必须落在视场里 —— 这是 TAN **参数化**的硬限制,不是数值误差。

        绕 A 点与绕 B 点的 gnomonic 之间差一个射影变换(不是仿射),切点越远,
        "线性 CD"越装不下这个场。加多少星都不会变小,所以调用方传 crpix_guess
        时应当传图幅中心。
        """
        truth = make_wcs()
        xy = frame_pixels()
        radec = pairs_from(truth, xy)

        _c, rms_center, _r = W.fit_tan(xy, radec, crpix_guess=CENTER)
        fit_far, rms_far, _r2 = W.fit_tan(xy, radec, crpix_guess=(-500.0, 9000.0))

        assert rms_center < 1e-9            # 切点在中心:精确
        assert 0.5 < rms_far < 5.0          # 离中心约 4.2°:实测 1.85 px
        assert fit_far.crpix == pytest.approx((-500.0, 9000.0))  # 仍然钉死了 CRPIX

    def test_residuals_sign_is_predicted_minus_observed(self):
        truth = make_wcs()
        xy = frame_pixels(n=20)
        radec = pairs_from(truth, xy)
        shifted = W.TanWcs(truth.crval,
                           (truth.crpix[0] + 3.0, truth.crpix[1] - 2.0), truth.cd)
        resid = W.residuals(shifted, xy, radec)
        assert np.allclose(resid[:, 0], 3.0)
        assert np.allclose(resid[:, 1], -2.0)

    def test_rms_px(self):
        assert W.rms_px(np.array([[3.0, 4.0]])) == pytest.approx(5.0)
        assert W.rms_px(np.zeros((0, 2))) == 0.0


class TestFitTanDegenerate:
    def test_too_few_pairs(self):
        with pytest.raises(W.WcsError):
            W.fit_tan(np.zeros((2, 2)), np.zeros((2, 2)))

    def test_collinear_points(self):
        truth = make_wcs()
        xy = np.column_stack([np.linspace(100, 6000, 40),
                              np.linspace(100, 4000, 40)])
        with pytest.raises(W.WcsError, match="退化"):
            W.fit_tan(xy, pairs_from(truth, xy))

    def test_all_points_identical(self):
        truth = make_wcs()
        xy = np.tile(np.array([[3000.0, 2000.0]]), (10, 1))
        with pytest.raises(W.WcsError):
            W.fit_tan(xy, pairs_from(truth, xy))

    def test_length_mismatch(self):
        with pytest.raises(W.WcsError, match="不一致"):
            W.fit_tan(np.zeros((5, 2)), np.zeros((4, 2)))

    def test_nan_input(self):
        xy = frame_pixels(n=10)
        radec = pairs_from(make_wcs(), xy)
        xy[3, 1] = np.nan
        with pytest.raises(W.WcsError, match="NaN"):
            W.fit_tan(xy, radec)

    def test_bad_shape(self):
        with pytest.raises(W.WcsError):
            W.fit_tan(np.zeros((5, 3)), np.zeros((5, 3)))

    def test_star_on_back_hemisphere(self):
        truth = make_wcs(ra=0.0, dec=0.0)
        xy = frame_pixels(n=20)
        radec = pairs_from(truth, xy)
        radec[0] = (180.0, 0.0)
        with pytest.raises(W.WcsError, match="背面"):
            W.fit_tan(xy, radec)

    def test_nan_crpix_guess(self):
        xy = frame_pixels(n=10)
        radec = pairs_from(make_wcs(), xy)
        with pytest.raises(W.WcsError):
            W.fit_tan(xy, radec, crpix_guess=(np.nan, 0.0))


class TestCrpixMustBeSolvedJointly:
    """钉死调研结论:CD 与 CRPIX 偏移必须**一起**解。

    模拟真实场景 —— 指向先验(赤道仪报的 RA/DEC)有约 0.9° 误差(真机实测
    中位 0.53°、最大 1.5°)。若拿这个先验当 CRVAL、拿图幅中心当 CRPIX、
    只用 2 参数解 CD,指向误差会整个折进 CD 里。
    """

    @staticmethod
    def _cd_only(xy, radec, crval, crpix):
        """只解 CD(强制过原点,没有截距项)—— 反面教材。"""
        xieta = W._standard_coords(crval, radec)
        design = xy - np.asarray(crpix, dtype=np.float64)
        sol, *_ = np.linalg.lstsq(design, xieta, rcond=None)
        return np.array([[sol[0, 0], sol[1, 0]], [sol[0, 1], sol[1, 1]]])

    def test_joint_beats_cd_only(self):
        truth = make_wcs(ra=98.0017, dec=5.401, rot=268.43, flip=True)
        xy = frame_pixels(n=200, seed=5)
        radec = pairs_from(truth, xy)

        # 指向先验偏 0.9°(典型真机值)
        prior = (truth.crval[0] + 0.9 / math.cos(math.radians(truth.crval[1])),
                 truth.crval[1])

        joint, joint_rms, _ = W.fit_tan(xy, radec, crpix_guess=CENTER)
        cd_only = self._cd_only(xy, radec, prior, CENTER)
        bad = W.TanWcs(prior, CENTER, cd_only)
        bad_rms = W.rms_px(W.residuals(bad, xy, radec))

        assert joint_rms < 1e-9
        assert bad_rms > 100.0                      # 实测上千像素
        # 只解 CD 时连尺度都被带歪
        assert abs(bad.pixel_scale() / truth.pixel_scale() - 1.0) > 1e-3
        assert joint.pixel_scale() == pytest.approx(truth.pixel_scale(), rel=1e-12)

    def test_joint_fit_absorbs_pointing_error(self):
        """联合解不需要任何指向先验就能把 CRVAL 找回来。"""
        truth = make_wcs(ra=275.27, dec=-14.04, rot=269.3, flip=True)
        xy = frame_pixels(n=120, seed=6)
        fit, rms, _ = W.fit_tan(xy, pairs_from(truth, xy), crpix_guess=CENTER)
        assert rms < 1e-9
        assert W.angular_separation(*fit.crval, *truth.crval) * 3600.0 < 1e-6


class TestSigmaClip:
    def _setup(self, n=120, n_bad=5, noise=0.3, seed=42):
        truth = make_wcs()
        xy = frame_pixels(n=n, seed=seed)
        radec = pairs_from(truth, xy)
        rng = np.random.default_rng(seed + 1)
        obs = xy + rng.normal(0.0, noise, xy.shape)
        bad_idx = np.arange(n_bad) * 7 + 3
        obs[bad_idx] += rng.choice([-1.0, 1.0], (n_bad, 2)) * rng.uniform(
            40.0, 120.0, (n_bad, 2))
        return truth, obs, radec, bad_idx

    def test_removes_outliers(self):
        truth, obs, radec, bad_idx = self._setup()
        fit, rms, resid, keep = W.fit_tan_sigma_clip(obs, radec, crpix_guess=CENTER)
        assert not keep[bad_idx].any()          # 离群全被剔
        assert keep.sum() >= len(obs) - 15      # 好点基本都留着
        assert rms < 0.7                        # 回到噪声水平
        assert resid.shape == (len(obs), 2)     # 残差是全部点
        assert W.angular_separation(*fit.crval, *truth.crval) * 3600.0 < 1.0

    def test_outliers_would_wreck_plain_fit(self):
        """对照组:不剔除时 RMS 被离群点顶爆(体现 sigma_clip 的价值)。"""
        _truth, obs, radec, _bad = self._setup()
        _fit, rms, _r = W.fit_tan(obs, radec, crpix_guess=CENTER)
        assert rms > 5.0

    def test_keeps_everything_when_clean(self):
        truth = make_wcs()
        xy = frame_pixels(n=80, seed=17)
        radec = pairs_from(truth, xy)
        _fit, rms, _resid, keep = W.fit_tan_sigma_clip(xy, radec, crpix_guess=CENTER)
        assert keep.all()
        assert rms < 1e-9

    def test_noise_free_not_over_clipped(self):
        """无噪合成数据残差只有 1e-12 量级,MAD≈0 —— 阈值下限保证不误剔。

        没有下限的话 ``median + 3·1.4826·MAD`` 会退化成 ``≈ median``,
        把一半的点当离群剔掉。
        """
        truth = make_wcs(rot=45.0)
        xy = frame_pixels(n=60, seed=31)
        _fit, _rms, _resid, keep = W.fit_tan_sigma_clip(
            xy, pairs_from(truth, xy), crpix_guess=CENTER)
        assert keep.all()

    def test_stops_before_dropping_below_min_keep(self):
        truth = make_wcs()
        xy = frame_pixels(n=8, seed=23)
        radec = pairs_from(truth, xy)
        obs = xy.copy()
        obs[0] += 300.0
        _fit, _rms, _resid, keep = W.fit_tan_sigma_clip(
            obs, radec, crpix_guess=CENTER, min_keep=6)
        assert keep.sum() >= 6

    def test_too_few_pairs(self):
        with pytest.raises(W.WcsError):
            W.fit_tan_sigma_clip(np.zeros((2, 2)), np.zeros((2, 2)))


# ------------------------------------------------------------ FITS 卡片互转


class TestFitsCards:
    def test_to_cards_keys(self):
        cards = W.to_fits_cards(make_wcs())
        assert cards["CTYPE1"] == "RA---TAN"
        assert cards["CTYPE2"] == "DEC--TAN"
        assert set(("CRVAL1", "CRVAL2", "CRPIX1", "CRPIX2",
                    "CD1_1", "CD1_2", "CD2_1", "CD2_2")) <= set(cards)
        assert all(isinstance(v, str) for v in cards.values())

    def test_roundtrip_is_lossless(self):
        wcs = make_wcs(ra=98.0016933341, dec=5.40057063, rot=268.428, flip=True)
        back = W.from_fits_cards(W.to_fits_cards(wcs))
        assert back is not None
        assert back.crval == wcs.crval
        assert back.crpix == wcs.crpix
        assert np.array_equal(back.cd, wcs.cd)

    def test_roundtrip_through_fits_header(self):
        wcs = make_wcs(ra=10.457, dec=40.613, rot=88.538, flip=True)
        hdr = parse_fits_header(fits_header_bytes(W.to_fits_cards(wcs)))
        back = W.from_fits_cards(hdr)
        assert back is not None
        assert W.angular_separation(*back.crval, *wcs.crval) < 1e-12
        assert np.allclose(back.cd, wcs.cd, rtol=0, atol=1e-18)

    def test_accepts_sip_ctype_and_ignores_distortion(self):
        """ASIAIR 写的就是 RA---TAN-SIP,必须能读,但只取线性部分。"""
        cards = dict(W.to_fits_cards(make_wcs()))
        cards["CTYPE1"] = "RA---TAN-SIP"
        cards["CTYPE2"] = "DEC--TAN-SIP"
        cards.update({"A_ORDER": "2", "A_0_2": "1e-6", "B_ORDER": "2"})
        wcs = W.from_fits_cards(cards)
        assert wcs is not None
        assert W.cards_have_sip(cards) is True

    def test_cards_have_sip_false(self):
        assert W.cards_have_sip(W.to_fits_cards(make_wcs())) is False

    def test_cards_have_sip_detects_order_keys(self):
        assert W.cards_have_sip({"AP_ORDER": "2"}) is True

    def test_lowercase_keys_accepted(self):
        cards = {k.lower(): v for k, v in W.to_fits_cards(make_wcs()).items()}
        assert W.from_fits_cards(cards) is not None

    def test_pc_plus_cdelt(self):
        cards = {
            "CTYPE1": "RA---TAN", "CTYPE2": "DEC--TAN",
            "CRVAL1": "83.6", "CRVAL2": "22.0",
            "CRPIX1": "100.0", "CRPIX2": "200.0",
            "CDELT1": "-0.001", "CDELT2": "0.001",
            "PC1_1": "1.0", "PC1_2": "0.0", "PC2_1": "0.0", "PC2_2": "1.0",
        }
        wcs = W.from_fits_cards(cards)
        assert wcs is not None
        assert np.allclose(wcs.cd, [[-0.001, 0.0], [0.0, 0.001]])
        assert wcs.rotation_deg() == pytest.approx(0.0)

    def test_cdelt_plus_crota2(self):
        """CROTA2 转的是**坐标轴**不是图像:+30° 读回来 rotation_deg 是 330°。

        标准式(Greisen & Calabretta 2002 Paper II)是
        ``CD1_2 = -CDELT2·sin ρ``、``CD2_2 = CDELT2·cos ρ``,
        于是 ``rotation_deg = atan2(CD1_2, CD2_2) = -ρ``。
        这个符号是"读别人写的老头文件"时最容易搞反的地方,故单独钉死。
        """
        cards = {
            "CTYPE1": "RA---TAN", "CTYPE2": "DEC--TAN",
            "CRVAL1": "83.6", "CRVAL2": "22.0",
            "CRPIX1": "100.0", "CRPIX2": "200.0",
            "CDELT1": "-0.001", "CDELT2": "0.001", "CROTA2": "30.0",
        }
        wcs = W.from_fits_cards(cards)
        assert wcs is not None
        assert wcs.pixel_scale() == pytest.approx(3.6)
        assert wcs.rotation_deg() == pytest.approx(330.0, abs=1e-9)
        assert wcs.flipped() is False
        assert wcs.cd[0, 1] == pytest.approx(-0.001 * math.sin(math.radians(30)))

    def test_cdelt_without_crota_defaults_to_zero(self):
        cards = {
            "CTYPE1": "RA---TAN", "CTYPE2": "DEC--TAN",
            "CRVAL1": "0", "CRVAL2": "0", "CRPIX1": "1", "CRPIX2": "1",
            "CDELT1": "-0.001", "CDELT2": "0.001",
        }
        wcs = W.from_fits_cards(cards)
        assert wcs is not None and wcs.rotation_deg() == pytest.approx(0.0)

    @pytest.mark.parametrize("drop", ["CTYPE1", "CTYPE2", "CRVAL1", "CRPIX2"])
    def test_missing_key_returns_none(self, drop):
        cards = dict(W.to_fits_cards(make_wcs()))
        cards.pop(drop)
        assert W.from_fits_cards(cards) is None

    def test_non_tan_projection_returns_none(self):
        cards = dict(W.to_fits_cards(make_wcs()))
        cards["CTYPE1"] = "RA---SIN"
        cards["CTYPE2"] = "DEC--SIN"
        assert W.from_fits_cards(cards) is None

    def test_missing_linear_terms_returns_none(self):
        cards = dict(W.to_fits_cards(make_wcs()))
        for k in ("CD1_1", "CD1_2", "CD2_1", "CD2_2"):
            cards.pop(k)
        assert W.from_fits_cards(cards) is None

    def test_unparsable_value_returns_none(self):
        cards = dict(W.to_fits_cards(make_wcs()))
        cards["CRVAL1"] = "not-a-number"
        assert W.from_fits_cards(cards) is None

    def test_singular_cd_in_cards_returns_none(self):
        cards = dict(W.to_fits_cards(make_wcs()))
        cards.update({"CD1_1": "1.0", "CD1_2": "2.0",
                      "CD2_1": "2.0", "CD2_2": "4.0"})
        assert W.from_fits_cards(cards) is None

    def test_empty_header_returns_none(self):
        assert W.from_fits_cards({}) is None

    def test_bad_source_type(self):
        with pytest.raises(W.WcsError):
            W.from_fits_cards(42)

    def test_fit_then_write_then_read(self):
        """端到端:拟合 → 写卡片 → 读回,天球映射必须一致。"""
        truth = make_wcs(ra=275.27, dec=-14.04, rot=269.3, flip=True)
        xy = frame_pixels(n=150, seed=8)
        fit, _rms, _resid = W.fit_tan(xy, pairs_from(truth, xy), crpix_guess=CENTER)
        back = W.from_fits_cards(W.to_fits_cards(fit))
        assert back is not None
        ra_a, dec_a = W.pixel_to_world(back, xy[:, 0], xy[:, 1])
        ra_b, dec_b = W.pixel_to_world(truth, xy[:, 0], xy[:, 1])
        assert np.max(W.angular_separation(ra_a, dec_a, ra_b, dec_b)) * 3600 < 1e-6
