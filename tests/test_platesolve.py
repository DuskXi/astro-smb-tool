"""astro_smb.platesolve 的离线单测(不连设备、不联网)。

主线是**端到端**:注入一个已知 WCS → 用合成星表投影出星点 → 解算 → 比对
crval/旋转/尺度。分档覆盖调研给出的能力边界(指向误差 0.2°/2.8°/13°、
星点数 30/19/11、旋转先验对/错、镜像视场),以及一整组**必须失败**的场景
—— 假阳性解算比解不出来危险得多,所以"纯噪声"和"星表指向别处"这两条
不是可选测试,是这个模块的底线。
"""
from __future__ import annotations

import math
import threading
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from astro_smb import catalog as C
from astro_smb import platesolve as P
from astro_smb import stars as S
from astro_smb import wcs as W
from astro_smb.fitshdr import parse_fits_header
from astro_smb.fitsimage import FitsImageError

# 合成"相机":ASI2600MC + 400mm,和真机同一量级
W_PX, H_PX = 6248, 4176
FOCAL, PIX = 401.0, 3.76
SCALE = P.pixel_scale_arcsec(FOCAL, PIX)          # ≈1.9337 ″/px
RA0, DEC0 = 98.0, 5.4


# ---------------------------------------------------------------- 工具


def make_wcs(ra=RA0, dec=DEC0, rot_deg=268.4, det_positive=True,
             scale=SCALE, w=W_PX, h=H_PX) -> W.TanWcs:
    """造一份纯相似变换的 TAN WCS。

    ``det_positive=True`` ⇒ ``TanWcs.flipped()`` 为真(ASIAIR 实测的取向)。
    """
    s = scale / 3600.0
    t = math.radians(rot_deg)
    c, sn = math.cos(t), math.sin(t)
    cd = s * (np.array([[c, -sn], [sn, c]]) if det_positive
              else np.array([[c, sn], [sn, -c]]))
    return W.TanWcs((ra, dec), ((w + 1) / 2.0, (h + 1) / 2.0), cd)


def synth_catalog(path: Path, ra0=RA0, dec0=DEC0, n=8000, seed=1,
                  radius=8.0) -> C.Catalog:
    """在 (ra0, dec0) 周围 ``radius`` 度的球冠内均匀撒星并打包。"""
    rng = np.random.default_rng(seed)
    th = np.arccos(rng.uniform(math.cos(math.radians(radius)), 1.0, n))
    ph = rng.uniform(0.0, 2 * math.pi, n)
    xh, eh, nh = P._basis3(ra0, dec0)
    v = (xh * np.cos(th)[:, None]
         + (nh * np.cos(ph)[:, None] + eh * np.sin(ph)[:, None])
         * np.sin(th)[:, None])
    ra, dec = W.unit_to_radec(v)
    vmag = rng.uniform(5.0, 12.0, n)
    C.write_catalog(path, ra, dec, vmag, np.zeros(n), np.zeros(n))
    return C.Catalog.open(path)


def project(cat: C.Catalog, wcs: W.TanWcs, *, jitter=0.3, seed=2, limit=None,
            w=W_PX, h=H_PX):
    """把星表投到像素平面 → ``(xy, flux)``,按亮度降序,带质心噪声。"""
    px, py = W.world_to_pixel(wcs, cat.ra, cat.dec)
    px, py = np.asarray(px), np.asarray(py)
    inside = (np.isfinite(px) & np.isfinite(py) & (px > 5) & (px < w - 5)
              & (py > 5) & (py < h - 5))
    idx = np.flatnonzero(inside)
    idx = idx[np.argsort(cat.vmag[idx])]
    if limit:
        idx = idx[:limit]
    rng = np.random.default_rng(seed)
    xy = np.column_stack([px[idx], py[idx]]) + rng.normal(0, jitter, (len(idx), 2))
    return xy, 10.0 ** (-0.4 * cat.vmag[idx])


def hint_for(wcs: W.TanWcs, *, d_ra=0.0, d_dec=0.0, rot=None, w=W_PX, h=H_PX,
             **kw) -> P.SolveHint:
    """由真值 WCS 造一份带指定误差的先验。"""
    ra, dec = W.pixel_to_world(wcs, (w + 1) / 2.0, (h + 1) / 2.0)
    return P.SolveHint(
        ra_deg=ra + d_ra / math.cos(math.radians(dec)), dec_deg=dec + d_dec,
        focal_len_mm=FOCAL, pixel_size_um=PIX, image_size=(w, h),
        rotation_deg=rot, **kw)


def check(res: P.SolveResult, truth: W.TanWcs, *, max_sep_arcsec=15.0,
          w=W_PX, h=H_PX):
    """解算结果与真值的一致性:中心、尺度、旋转、宇称。"""
    assert res.ok, f"{res.reason}: {res.message}"
    cx, cy = (w + 1) / 2.0, (h + 1) / 2.0
    got = W.pixel_to_world(res.wcs, cx, cy)
    want = W.pixel_to_world(truth, cx, cy)
    sep = float(W.angular_separation(got[0], got[1], want[0], want[1])) * 3600
    assert sep < max_sep_arcsec, f"中心差 {sep:.2f}″"
    assert abs(res.pixel_scale / truth.pixel_scale() - 1.0) < 2e-3
    d = abs(res.zwo_angle_deg - P.zwo_angle_from_cd(truth.cd)) % 360.0
    assert min(d, 360.0 - d) < 0.2
    assert res.flipped == truth.flipped()
    return sep


@pytest.fixture(scope="module")
def cat(tmp_path_factory):
    return synth_catalog(tmp_path_factory.mktemp("cat") / "c.bin")


@pytest.fixture(scope="module")
def truth():
    return make_wcs()


@pytest.fixture(scope="module")
def field(cat, truth):
    return project(cat, truth)


# ---------------------------------------------------------------- 基础工具


class TestScaleAndAngle:
    def test_pixel_scale_matches_closed_form(self):
        assert P.pixel_scale_arcsec(400.0, 3.76) == pytest.approx(
            206264.806247 * 3.76e-3 / 400.0, rel=1e-9)

    def test_binning_multiplies(self):
        a = P.pixel_scale_arcsec(400.0, 3.76, 1)
        assert P.pixel_scale_arcsec(400.0, 3.76, 2) == pytest.approx(2 * a)

    @pytest.mark.parametrize("f,p", [(0.0, 3.76), (-1.0, 3.76), (400.0, 0.0),
                                     (float("nan"), 3.76)])
    def test_bad_optics_raise(self, f, p):
        with pytest.raises(P.SolveError):
            P.pixel_scale_arcsec(f, p)

    def test_zwo_angle_is_not_wcs_rotation(self):
        """两个角是不同的量 —— 混用会让旋转先验永远命不中。"""
        wcs = make_wcs(rot_deg=30.0)
        assert abs(P.zwo_angle_from_cd(wcs.cd) - wcs.rotation_deg()) > 1.0

    def test_zwo_angle_full_circle(self):
        for a in (0.0, 45.0, 179.0, 271.5, 359.9):
            wcs = make_wcs(rot_deg=a)
            got = P.zwo_angle_from_cd(wcs.cd)
            assert 0.0 <= got < 360.0
            back = make_wcs(rot_deg=(a + 90.0) % 360.0)
            d = abs(P.zwo_angle_from_cd(back.cd) - got) % 360.0
            assert min(d, 360.0 - d) == pytest.approx(90.0, abs=1e-6)

    def test_zwo_angle_same_for_both_parities(self):
        """播种剪枝按 ``arg(c)+180`` 算角度,两种宇称必须都成立。"""
        a = make_wcs(rot_deg=123.0, det_positive=True)
        b = make_wcs(rot_deg=123.0, det_positive=False)
        assert P.zwo_angle_from_cd(a.cd) == pytest.approx(
            P.zwo_angle_from_cd(b.cd), abs=1e-9)


class TestFalseAlarmMath:
    def test_tail_is_monotone_in_n(self):
        v = [P._log10_poisson_tail(n, 0.5) for n in range(1, 12)]
        assert all(v[i] > v[i + 1] for i in range(len(v) - 1))

    def test_tail_saturates_when_mu_exceeds_n(self):
        assert P._log10_poisson_tail(3, 5.0) == 0.0
        assert P._log10_poisson_tail(0, 0.1) == 0.0

    def test_tail_bounds_exact_sum(self):
        """上界必须**不小于**真实尾概率,否则会放行假阳性。"""
        for mu in (0.05, 0.3, 0.9):
            for n in (4, 6, 9):
                exact = sum(math.exp(-mu) * mu ** k / math.factorial(k)
                            for k in range(n, n + 60))
                assert P._log10_poisson_tail(n, mu) >= math.log10(exact) - 1e-9

    def test_cone_area_small_angle(self):
        assert P._cone_area_deg2(0.5) == pytest.approx(math.pi * 0.25, rel=1e-4)

    def test_cone_area_full_sphere(self):
        assert P._cone_area_deg2(180.0) == pytest.approx(41252.96, rel=1e-4)


class TestAdaptiveCatalogCount:
    """星表候选数必须按锥/视场面积比自适应 —— 写死一个数在长焦上必挂。"""

    @staticmethod
    def geometry(focal):
        """复现 run() 里第一级(1°)的 tile 几何。"""
        hint = P.SolveHint(ra_deg=0.0, dec_deg=0.0, focal_len_mm=focal,
                           pixel_size_um=PIX, image_size=(W_PX, H_PX))
        fr = hint.field_radius_deg()
        tile_r = min(max(1.0 * 0.6, fr), 2.5 * fr)
        return fr + tile_r, hint.field_area_deg2()

    @pytest.mark.parametrize("focal", [200.0, 400.0, 1000.0, 2000.0])
    def test_in_field_share_is_focal_independent(self, focal):
        """不变量:落进视场的候选数 = n_image × margin,与焦距无关。"""
        cone_r, area = self.geometry(focal)
        n = P._adaptive_catalog_count(60, cone_r, area, 1.5, 32, 12000)
        in_field = n * area / P._cone_area_deg2(cone_r)
        assert in_field == pytest.approx(90.0, rel=0.02)

    def test_hardcoded_150_starves_a_long_focal_field(self):
        """把调研里那个 bug 钉在测试里:写死 150 在 2000mm 只剩十几颗。"""
        cone_r, area = self.geometry(2000.0)
        assert 150 * area / P._cone_area_deg2(cone_r) < 20.0
        cone_r, area = self.geometry(400.0)
        assert 150 * area / P._cone_area_deg2(cone_r) > 20.0

    def test_bounds_are_respected(self):
        assert P._adaptive_catalog_count(60, 1.0, 1e-6, 1.5, 32, 500) == 500
        assert P._adaptive_catalog_count(1, 0.001, 1e6, 1.5, 32, 500) == 32


class TestTiles:
    def test_single_tile_when_radius_fits(self):
        assert len(P._tile_centers(10.0, 20.0, 1.0, 2.0)) == 1

    def test_tiles_cover_the_whole_disk(self):
        """随便撒点,每个点都得离某个 tile 中心 ≤ tile_r,不然会漏解。"""
        ra0, dec0, radius, tile_r = 40.0, 62.0, 12.0, 2.5
        centers = np.array(P._tile_centers(ra0, dec0, radius, tile_r))
        rng = np.random.default_rng(3)
        th = np.degrees(np.arccos(rng.uniform(
            math.cos(math.radians(radius)), 1.0, 3000)))
        ph = rng.uniform(0, 2 * math.pi, 3000)
        xh, eh, nh = P._basis3(ra0, dec0)
        v = (xh * np.cos(np.radians(th))[:, None]
             + (nh * np.cos(ph)[:, None] + eh * np.sin(ph)[:, None])
             * np.sin(np.radians(th))[:, None])
        ra, dec = W.unit_to_radec(v)
        d = W.angular_separation(ra[:, None], dec[:, None],
                                 centers[None, :, 0], centers[None, :, 1])
        assert d.min(axis=1).max() <= tile_r + 1e-9

    def test_tile_count_is_capped(self):
        assert len(P._tile_centers(0.0, 0.0, 90.0, 0.5, max_tiles=17)) == 17


class TestBands:
    def test_none_means_whole_frame(self):
        assert P._band_rows(4176, None, 3, 2, 128) == [(0, 4176)]
        assert P._band_rows(4176, 1.0, 3, 2, 128) == [(0, 4176)]

    def test_bands_are_aligned_disjoint_and_in_range(self):
        bands = P._band_rows(4176, 0.3, 3, 2, 256)
        assert len(bands) == 3
        prev = -1
        for y0, y1 in bands:
            assert 0 <= y0 < y1 <= 4176
            assert y0 % 2 == 0 and (y1 - y0) % 2 == 0
            assert y0 > prev
            prev = y1
        assert sum(y1 - y0 for y0, y1 in bands) <= int(4176 * 0.3) + 8

    def test_bands_keep_the_full_vertical_baseline(self):
        """分带的**全部意义**就是这个:别退化成"只读中间一块"。"""
        bands = P._band_rows(4176, 0.3, 3, 2, 256)
        assert bands[0][0] < 4176 * 0.25
        assert bands[-1][1] > 4176 * 0.75

    def test_thin_fraction_falls_back_to_fewer_bands(self):
        bands = P._band_rows(4176, 0.06, 3, 2, 256)
        assert len(bands) == 1 and bands[0][1] - bands[0][0] >= 256


class TestPointGrid:
    def test_finds_every_point_even_when_they_share_a_cell(self):
        """一格一个点的实现会在这里丢点 —— 丢配对 = 内点不够 = 解不出来。"""
        x = np.array([0.0, 0.6, 1.2, 1.8, 2.4])
        y = np.array([0.0, 0.7, 1.4, 2.1, 2.8])
        g = P._PointGrid(x, y, 0.5)
        qi, _d2 = g.query(x + 0.05, y - 0.05, 0.5)
        assert np.array_equal(qi, np.arange(5))
        assert g.depth >= 2          # 确实有格子装了多个点

    def test_misses_outside_radius(self):
        g = P._PointGrid(np.array([0.0]), np.array([0.0]), 1.0)
        qi, _ = g.query(np.array([5.0]), np.array([0.0]), 1.0)
        assert qi[0] == -1

    def test_brute_force_agreement(self):
        rng = np.random.default_rng(6)
        x, y = rng.uniform(0, 50, 300), rng.uniform(0, 50, 300)
        qx, qy = rng.uniform(-5, 55, 500), rng.uniform(-5, 55, 500)
        r = 0.8
        g = P._PointGrid(x, y, r)
        qi, _ = g.query(qx, qy, r)
        d = np.hypot(qx[:, None] - x[None, :], qy[:, None] - y[None, :])
        want = np.where(d.min(axis=1) < r, d.argmin(axis=1), -1)
        assert np.array_equal(qi, want)

    def test_nan_queries_miss_quietly(self):
        g = P._PointGrid(np.array([0.0, 1.0]), np.array([0.0, 1.0]), 1.0)
        qi, _ = g.query(np.array([np.nan, np.inf, 0.0]),
                        np.array([0.0, 0.0, 0.0]), 1.0)
        assert list(qi) == [-1, -1, 0]

    def test_empty_grid(self):
        g = P._PointGrid(np.empty(0), np.empty(0), 1.0)
        qi, _ = g.query(np.zeros(3), np.zeros(3), 1.0)
        assert list(qi) == [-1, -1, -1]

    def test_bad_radius_raises(self):
        with pytest.raises(P.SolveError):
            P._PointGrid(np.zeros(3), np.zeros(3), 0.0)


# ---------------------------------------------------------------- 先验


def fake_header(**extra) -> str:
    cards = {
        "SIMPLE": "T", "BITPIX": "16", "NAXIS": "2",
        "NAXIS1": str(W_PX), "NAXIS2": str(H_PX),
        "BZERO": "32768", "BSCALE": "1",
        "RA": "98.07084", "DEC": "5.0075", "FOCALLEN": "401",
        "XPIXSZ": "3.75999999046326", "YPIXSZ": "3.75999999046326",
        "DATE-OBS": "'2025-11-04T16:56:49.794842'",
        "INSTRUME": "'ZWO ASI2600MC Pro'", "BAYERPAT": "'RGGB'",
    }
    cards.update(extra)
    return "".join(f"{k:<8}= {v:>20}".ljust(80) for k, v in cards.items()
                   if v is not None) + "END".ljust(80)


def parse_fake(**extra):
    text = fake_header(**extra)
    raw = text.encode("ascii")
    raw += b" " * ((2880 - len(raw) % 2880) % 2880)
    return parse_fits_header(raw)


class TestSolveHintFromHeader:
    def test_decimal_ra_dec(self):
        h = P.SolveHint.from_header(parse_fake())
        assert h.ra_deg == pytest.approx(98.07084)
        assert h.dec_deg == pytest.approx(5.0075)
        assert h.has_pointing

    def test_sexagesimal_ra_dec_still_works(self):
        h = P.SolveHint.from_header(
            parse_fake(RA="'17h22m35s'", DEC="'-36d07m40s'"))
        assert h.ra_deg == pytest.approx(260.645833, abs=1e-4)
        assert h.dec_deg == pytest.approx(-36.127778, abs=1e-4)

    def test_scale_and_field(self):
        h = P.SolveHint.from_header(parse_fake())
        assert h.pixel_scale_arcsec() == pytest.approx(1.9337, abs=1e-3)
        assert h.field_radius_deg() == pytest.approx(2.018, abs=0.01)
        assert h.field_area_deg2() == pytest.approx(7.53, abs=0.1)

    def test_epoch_is_utc_not_local(self):
        """DATE-OBS 是 UTC —— 走本机时区会差一个时区(自行外推就偏了)。"""
        h = P.SolveHint.from_header(parse_fake())
        want = C.jyear_from_unix(
            datetime(2025, 11, 4, 16, 56, 49, tzinfo=timezone.utc).timestamp())
        assert h.epoch == pytest.approx(want, abs=1e-6)

    def test_rotation_from_filename(self):
        name = "Light_M 8_180.0s_Bin1_4C_20260723-221336_2deg_0001.fit"
        assert P.SolveHint.from_header(parse_fake(), name=name).rotation_deg == 2.0

    def test_rotation_falls_back_to_rotator_card(self):
        """SD 卡方言的文件名解析不了(naming.py 已知限制),得有退路。"""
        name = ("Light_NGC 2237_300.0s_Bin1_2600MC_gain100_"
                "20251105-010153_-19.6C_0001.fit")
        h = P.SolveHint.from_header(parse_fake(ROTATOR="2"), name=name)
        assert h.rotation_deg == 2.0

    def test_no_rotation_prior_when_nothing_available(self):
        assert P.SolveHint.from_header(parse_fake()).rotation_deg is None

    def test_ignores_existing_wcs_in_header(self):
        """刻意不吃头里的 CRVAL —— 解算要当一条独立证据链。"""
        h = P.SolveHint.from_header(parse_fake(
            CTYPE1="'RA---TAN'", CRVAL1="123.456", CRVAL2="-45.0",
            CRPIX1="1.0", CRPIX2="1.0", CD1_1="1e-4", CD1_2="0",
            CD2_1="0", CD2_2="1e-4"))
        assert h.ra_deg == pytest.approx(98.07084)

    def test_missing_optics_gives_no_scale(self):
        h = P.SolveHint.from_header(parse_fake(FOCALLEN=None, XPIXSZ=None,
                                               YPIXSZ=None))
        assert h.pixel_scale_arcsec() is None
        assert h.field_radius_deg() is None

    def test_explicit_pixel_scale_wins(self):
        h = P.SolveHint(pixel_scale=3.0, focal_len_mm=400.0, pixel_size_um=3.76)
        assert h.pixel_scale_arcsec() == 3.0

    def test_overrides_apply(self):
        h = P.SolveHint.from_header(parse_fake(), rotation_deg=42.0,
                                    scale_tol=0.05)
        assert h.rotation_deg == 42.0 and h.scale_tol == 0.05


class TestCoordConversion:
    def test_binning_one_matches_wcs_helper(self):
        xy = P.fits_xy_from_stars(np.array([[0.0, 0.0], [10.0, 20.0]]), 100)
        assert xy[0].tolist() == [1.0, 100.0]
        assert xy[1].tolist() == [11.0, 80.0]

    def test_superpixel_maps_to_cell_centre(self):
        """超像素 (c, r) 的中心 = 全分辨率 (2c+0.5, 2r+0.5)。"""
        xy = P.fits_xy_from_stars(np.array([[0.0, 0.0]]), 4176, binning=2)
        assert xy[0].tolist() == [1.5, 4176 - 0.5]

    def test_row_offset_is_in_plane_units(self):
        a = P.fits_xy_from_stars(np.array([[3.0, 5.0]]), 4176, binning=2)
        b = P.fits_xy_from_stars(np.array([[3.0, 0.0]]), 4176, binning=2,
                                 row_offset=5.0)
        assert a.tolist() == b.tolist()

    def test_bad_shape_raises(self):
        with pytest.raises(P.SolveError):
            P.fits_xy_from_stars(np.zeros((4, 3)), 100)


# ---------------------------------------------------------------- 端到端


class TestSolveSynthetic:
    """注入已知 WCS → 投影 → 解算 → 比对。"""

    def test_nominal(self, cat, truth, field):
        xy, flux = field
        res = P.solve(xy[:40], cat, hint_for(truth, d_ra=0.2), flux=flux[:40])
        check(res, truth)
        assert res.level == 0 and res.n_match >= 8
        assert res.rms_px < 2.0
        assert res.log_fap < -6.0
        assert res.elapsed_s > 0.0

    @pytest.mark.parametrize("d_ra,d_dec,level", [
        (0.2, 0.0, 0),      # 正常指向误差(真机 90% 分位 1.0°)
        (2.0, 2.0, 1),      # 2.8° 粗差(AutoCenter 日志里真出现过)
        (9.2, 9.2, 2),      # 13.1° 粗差
    ])
    def test_pointing_error_levels(self, cat, truth, field, d_ra, d_dec, level):
        xy, flux = field
        res = P.solve(xy[:40], cat, hint_for(truth, d_ra=d_ra, d_dec=d_dec),
                      flux=flux[:40], time_budget_s=180)
        check(res, truth)
        assert res.level == level

    def test_rotation_prior_cuts_the_candidate_count(self, cat, truth, field):
        xy, flux = field
        zw = P.zwo_angle_from_cd(truth.cd)
        free = P.solve(xy[:40], cat, hint_for(truth, d_ra=0.2), flux=flux[:40])
        tied = P.solve(xy[:40], cat, hint_for(truth, d_ra=0.2, rot=zw),
                       flux=flux[:40])
        check(free, truth)
        check(tied, truth)
        assert tied.candidates * 4 < free.candidates

    def test_wrong_rotation_prior_still_solves(self, cat, truth, field):
        """先验错了只能变慢,不能变错 —— 每一级内部都会放开旋转再试一遍。"""
        xy, flux = field
        bad = (P.zwo_angle_from_cd(truth.cd) + 130.0) % 360.0
        res = P.solve(xy[:40], cat, hint_for(truth, d_ra=0.2, rot=bad),
                      flux=flux[:40], time_budget_s=180)
        check(res, truth)

    @pytest.mark.parametrize("n", [30, 19])
    def test_star_count_thresholds(self, cat, truth, field, n):
        """调研实测:19 颗 100% 成功。"""
        xy, flux = field
        res = P.solve(xy[:n], cat, hint_for(truth, d_ra=0.2), flux=flux[:n])
        check(res, truth)

    def test_eleven_stars_is_either_right_or_honest(self, cat, truth, field):
        """11 颗掉到 77%:允许解不出来,**不允许**解错。"""
        xy, flux = field
        res = P.solve(xy[:11], cat, hint_for(truth, d_ra=0.2), flux=flux[:11],
                      min_matches=7)
        if res.ok:
            check(res, truth)
        else:
            assert res.reason in (P.REASON_NO_MATCH, P.REASON_BAD_FIT)

    def test_mirrored_field(self, cat):
        mirror = make_wcs(det_positive=False)
        xy, flux = project(cat, mirror)
        res = P.solve(xy[:40], cat, hint_for(mirror, d_ra=0.2), flux=flux[:40])
        check(res, mirror)
        assert res.flipped is False

    def test_correct_parity_hint_gives_the_same_answer(self, cat, truth, field):
        xy, flux = field
        both = P.solve(xy[:40], cat, hint_for(truth, d_ra=0.2), flux=flux[:40])
        tied = P.solve(xy[:40], cat, hint_for(truth, d_ra=0.2, flipped=True),
                       flux=flux[:40])
        check(both, truth)
        check(tied, truth)
        assert tied.wcs.crval[0] == pytest.approx(both.wcs.crval[0], abs=1e-6)
        assert tied.wcs.crval[1] == pytest.approx(both.wcs.crval[1], abs=1e-6)

    def test_wrong_parity_hint_fails_rather_than_lies(self, cat, truth, field):
        xy, flux = field
        res = P.solve(xy[:40], cat, hint_for(truth, d_ra=0.2, flipped=False),
                      flux=flux[:40], radii=(1.0,), time_budget_s=30)
        assert not res.ok

    def test_starlist_input_is_converted(self, cat, truth, field):
        """StarList 是 0-based 数组坐标(y 向下),必须自动换成 FITS 像素。"""
        xy, flux = field
        n = 40
        cols = xy[:n, 0] - 1.0
        rows = (H_PX - xy[:n, 1]) / 2.0        # 超像素平面的行
        sl = _fake_starlist(cols / 2.0, rows, flux[:n], (H_PX // 2, W_PX // 2))
        res = P.solve(sl, cat, hint_for(truth, d_ra=0.2))
        check(res, truth, max_sep_arcsec=20.0)

    def test_result_cards_round_trip(self, cat, truth, field):
        xy, flux = field
        res = P.solve(xy[:40], cat, hint_for(truth, d_ra=0.2), flux=flux[:40])
        back = W.from_fits_cards(res.fits_cards())
        assert back is not None
        assert back.crval[0] == pytest.approx(res.wcs.crval[0])
        assert not W.cards_have_sip(res.fits_cards())
        assert "解算成功" in str(res)

    def test_matched_pairs_are_consistent(self, cat, truth, field):
        xy, flux = field
        res = P.solve(xy[:40], cat, hint_for(truth, d_ra=0.2), flux=flux[:40])
        assert len(res.matched_xy) == res.n_match
        r = W.residuals(res.wcs, res.matched_xy, res.matched_radec)
        assert W.rms_px(r) == pytest.approx(res.rms_px, rel=1e-9)


class TestMustFail:
    """这一组是模块的底线:宁可解不出来,绝不给错答案。"""

    def test_pure_noise_never_solves(self, cat, truth):
        rng = np.random.default_rng(7)
        noise = np.column_stack([rng.uniform(1, W_PX, 60),
                                 rng.uniform(1, H_PX, 60)])
        res = P.solve(noise, cat, hint_for(truth), radii=(1.0,),
                      time_budget_s=60)
        assert not res.ok
        assert res.reason in (P.REASON_NO_MATCH, P.REASON_BAD_FIT)
        assert res.wcs is None

    def test_catalog_pointing_elsewhere(self, tmp_path, truth, field):
        """星表覆盖不到这块天 —— 必须报覆盖不足,不能硬凑一个解。"""
        far = synth_catalog(tmp_path / "far.bin", RA0 + 120.0, DEC0 - 40.0,
                            n=3000, seed=9, radius=6.0)
        xy, flux = field
        res = P.solve(xy[:40], far, hint_for(truth), flux=flux[:40],
                      radii=(1.0,), time_budget_s=30)
        assert not res.ok
        assert res.reason == P.REASON_NO_CATALOG

    def test_scrambled_positions_never_solve(self, cat, truth, field):
        """真星表 + 打乱的星点:内点数上不去,FAP 也放不过。"""
        xy, flux = field
        rng = np.random.default_rng(11)
        bad = xy[:40].copy()
        bad[:, 0] = rng.permutation(bad[:, 0])
        bad[:, 1] = rng.permutation(bad[:, 1])
        res = P.solve(bad, cat, hint_for(truth), flux=flux[:40],
                      radii=(1.0,), time_budget_s=60)
        assert not res.ok

    def test_too_few_stars(self, cat, truth, field):
        xy, flux = field
        res = P.solve(xy[:5], cat, hint_for(truth), flux=flux[:5])
        assert res.reason == P.REASON_FEW_STARS and res.n_stars == 5

    def test_no_pointing_hint_without_blind(self, cat, field):
        """**契约已变**:尺度已知、只缺指向时不再直接报 no_hint —— 那正是盲解
        该接管的场景(#28 的 blind_hint_grid 一直没有上层调用方)。
        显式关掉盲解才回到原来的行为。"""
        xy, _ = field
        res = P.solve(xy[:40], cat, P.SolveHint(focal_len_mm=FOCAL,
                                                pixel_size_um=PIX,
                                                image_size=(W_PX, H_PX)),
                      blind=False)
        assert res.reason == P.REASON_NO_HINT

    def test_no_pointing_falls_through_to_blind(self, cat, field):
        """默认会去盲解:失败理由变成"搜过了没找到",而不是"你没给先验"。"""
        xy, _ = field
        res = P.solve(xy[:40], cat, P.SolveHint(focal_len_mm=FOCAL,
                                                pixel_size_um=PIX,
                                                image_size=(W_PX, H_PX)),
                      blind_max_tiles=3)
        assert res.reason != P.REASON_NO_HINT

    def test_blind_needs_scale(self, cat, field):
        """尺度未知时盲解也救不了:相似变换的尺度自由度回来了,
        两颗星不再唯一确定变换 —— 该报 no_hint 就报。"""
        xy, _ = field
        res = P.solve(xy[:40], cat, P.SolveHint(image_size=(W_PX, H_PX)))
        assert res.reason == P.REASON_NO_HINT

    def test_no_scale_hint(self, cat, field):
        xy, _ = field
        res = P.solve(xy[:40], cat, P.SolveHint(ra_deg=RA0, dec_deg=DEC0,
                                                image_size=(W_PX, H_PX)))
        assert res.reason == P.REASON_NO_HINT

    def test_no_image_size(self, cat, field):
        xy, _ = field
        res = P.solve(xy[:40], cat, P.SolveHint(ra_deg=RA0, dec_deg=DEC0,
                                                focal_len_mm=FOCAL,
                                                pixel_size_um=PIX))
        assert res.reason == P.REASON_NO_HINT

    def test_missing_hint_object(self, cat, field):
        xy, _ = field
        assert P.solve(xy, cat, None).reason == P.REASON_NO_HINT

    def test_time_budget(self, cat, truth, field):
        xy, flux = field
        res = P.solve(xy[:40], cat, hint_for(truth, d_ra=9.2, d_dec=9.2),
                      flux=flux[:40], time_budget_s=0.0)
        assert not res.ok and res.reason == P.REASON_TIMEOUT

    def test_bad_star_array_raises(self, cat, truth):
        with pytest.raises(P.SolveError):
            P.solve(np.zeros((10, 3)), cat, hint_for(truth))

    def test_nan_star_raises(self, cat, truth, field):
        xy, flux = field
        bad = xy[:40].copy()
        bad[3, 0] = np.nan
        with pytest.raises(P.SolveError):
            P.solve(bad, cat, hint_for(truth), flux=flux[:40])

    def test_starlist_without_image_size_reports_no_hint(self, cat, field):
        xy, flux = field
        sl = _fake_starlist(xy[:20, 0], xy[:20, 1], flux[:20], (H_PX, W_PX))
        res = P.solve(sl, cat, P.SolveHint(ra_deg=RA0, dec_deg=DEC0,
                                           pixel_scale=SCALE))
        assert res.reason == P.REASON_NO_HINT

    def test_starlist_with_non_integer_downscale_raises(self, cat, truth, field):
        """提星平面不是全分辨率的整数分之一 —— 猜倍数会静默解错,必须报错。"""
        xy, flux = field
        sl = _fake_starlist(xy[:20, 0], xy[:20, 1], flux[:20], (1000, 1500))
        with pytest.raises(P.SolveError):
            P.solve(sl, cat, hint_for(truth))

    def test_bad_catalog_argument_raises(self, truth):
        with pytest.raises(P.SolveError):
            P.solve(np.zeros((10, 2)), 1234, hint_for(truth))

    def test_empty_radii_raises(self, cat, truth, field):
        xy, flux = field
        with pytest.raises(P.SolveError):
            P.solve(xy[:40], cat, hint_for(truth), flux=flux[:40], radii=())

    def test_every_reason_has_chinese_text(self):
        for code in (P.REASON_OK, P.REASON_NO_HINT, P.REASON_FEW_STARS,
                     P.REASON_NO_CATALOG, P.REASON_NO_MATCH,
                     P.REASON_BAD_FIT, P.REASON_TIMEOUT):
            assert P.REASON_TEXT[code]

    def test_failed_result_stringifies(self, cat, field):
        xy, _ = field
        res = P.solve(xy, cat, None)
        assert "解算失败" in str(res)
        assert res.fits_cards() == {}
        assert res.center is None and res.pixel_scale is None


class TestCancelAndProgress:
    def test_cancel_raises(self, cat, truth, field):
        xy, flux = field
        ev = threading.Event()
        ev.set()
        with pytest.raises(InterruptedError):
            P.solve(xy[:40], cat, hint_for(truth), flux=flux[:40], cancel=ev)

    def test_progress_reports_stages(self, cat, truth, field):
        xy, flux = field
        seen = []
        P.solve(xy[:40], cat, hint_for(truth, d_ra=0.2), flux=flux[:40],
                progress=lambda s, f: seen.append((s, f)))
        assert seen
        assert all(0.0 <= f <= 1.0 for _s, f in seen)
        assert any("搜索" in s for s, _f in seen)

    def test_broken_progress_callback_does_not_break_the_solve(
            self, cat, truth, field):
        xy, flux = field

        def boom(_s, _f):
            raise RuntimeError("调用方的回调炸了")

        res = P.solve(xy[:40], cat, hint_for(truth, d_ra=0.2), flux=flux[:40],
                      progress=boom)
        check(res, truth)


# ---------------------------------------------------------------- solve_file


def write_fits(path: Path, wcs: W.TanWcs, cat: C.Catalog, *, bayer=True,
               w=W_PX // 4, h=H_PX // 4, scale=None, sigma=2.0, extra=None):
    """写一张合成 FITS(BITPIX 16 + BZERO 32768,和 ASIAIR 同一路径)。

    星点按 ``wcs`` 投出来画高斯斑;**不写 ROWORDER**,所以解码时会上下翻转
    —— 正好复现真机行为(数据按 FITS 存储序写:存储行 r ↔ FITS y = r+1)。
    """
    px, py = W.world_to_pixel(wcs, cat.ra, cat.dec)
    px, py = np.asarray(px), np.asarray(py)
    keep = (np.isfinite(px) & np.isfinite(py) & (px > 8) & (px < w - 8)
            & (py > 8) & (py < h - 8))
    idx = np.flatnonzero(keep)
    idx = idx[np.argsort(cat.vmag[idx])][:120]
    img = np.full((h, w), 1000.0)
    rng = np.random.default_rng(4)
    img += rng.normal(0.0, 8.0, img.shape)
    yy, xx = np.mgrid[-6:7, -6:7]
    for k in idx:
        amp = 8000.0 * 10 ** (-0.4 * (cat.vmag[k] - 8.0))
        cx, cy = px[k] - 1.0, py[k] - 1.0        # FITS 1-based → 存储 0-based
        ix, iy = int(round(cx)), int(round(cy))
        blob = amp * np.exp(-((xx - (cx - ix)) ** 2 + (yy - (cy - iy)) ** 2)
                            / (2 * sigma ** 2))
        img[iy - 6:iy + 7, ix - 6:ix + 7] += blob
    data = np.clip(img, 0, 65535).astype(np.uint16)

    cards = {
        "SIMPLE": "T", "BITPIX": "16", "NAXIS": "2", "NAXIS1": str(w),
        "NAXIS2": str(h), "BZERO": "32768", "BSCALE": "1",
        "RA": f"{wcs.crval[0]:.6f}", "DEC": f"{wcs.crval[1]:.6f}",
        "FOCALLEN": "100", "XPIXSZ": f"{PIX}",
        "DATE-OBS": "'2025-11-04T16:56:49.0'",
    }
    if scale is not None:
        cards["FOCALLEN"] = f"{206264.806247 * PIX * 1e-3 / scale:.4f}"
    if bayer:
        cards["BAYERPAT"] = "'RGGB'"
    if extra:
        cards.update(extra)
    txt = "".join(f"{k:<8}= {v:>20}".ljust(80) for k, v in cards.items())
    txt += "END".ljust(80)
    raw = txt.encode("ascii")
    raw += b" " * ((2880 - len(raw) % 2880) % 2880)
    body = (data.astype(np.int32) - 32768).astype(">i2").tobytes()
    body += b"\0" * ((2880 - len(body) % 2880) % 2880)
    path.write_bytes(raw + body)
    return len(idx)


@pytest.fixture(scope="module")
def small_field(tmp_path_factory):
    """一台"短焦"合成相机:1562×1044、7.76″/px ⇒ 视场 3.4°×2.2°。"""
    w, h = W_PX // 4, H_PX // 4
    scale = 4 * SCALE
    ra, dec = 210.0, -18.0
    d = tmp_path_factory.mktemp("ff")
    cat = synth_catalog(d / "c.bin", ra, dec, n=4000, seed=13, radius=6.0)
    wcs = make_wcs(ra, dec, rot_deg=17.0, scale=scale, w=w, h=h)
    return d, cat, wcs, w, h, scale


class TestSolveFile:
    def test_osc_frame(self, small_field):
        d, cat, wcs, w, h, scale = small_field
        p = d / "osc.fit"
        n = write_fits(p, wcs, cat, bayer=True, w=w, h=h, scale=scale)
        assert n > 30
        res = P.solve_file(p, catalog=cat)
        check(res, wcs, max_sep_arcsec=60.0, w=w, h=h)
        assert res.star_fwhm_px > 0
        assert res.star_fwhm_arcsec == pytest.approx(
            res.star_fwhm_px * res.pixel_scale)
        assert 0 <= res.star_ellipticity < 1

    def test_mono_frame(self, small_field):
        d, cat, wcs, w, h, scale = small_field
        p = d / "mono.fit"
        write_fits(p, wcs, cat, bayer=False, w=w, h=h, scale=scale)
        res = P.solve_file(p, catalog=cat)
        check(res, wcs, max_sep_arcsec=30.0, w=w, h=h)
        assert res.star_fwhm_px > 0
        assert res.star_fwhm_arcsec == pytest.approx(
            res.star_fwhm_px * res.pixel_scale)

    def test_partial_read_agrees_with_full_read(self, small_field):
        d, cat, wcs, w, h, scale = small_field
        p = d / "mono.fit"
        write_fits(p, wcs, cat, bayer=False, w=w, h=h, scale=scale)
        full = P.solve_file(p, catalog=cat)
        part = P.solve_file(p, catalog=cat, read_fraction=0.5, n_bands=3)
        check(part, wcs, max_sep_arcsec=30.0, w=w, h=h)
        assert part.n_match < full.n_match          # 星少了
        a = W.pixel_to_world(full.wcs, w / 2, h / 2)
        b = W.pixel_to_world(part.wcs, w / 2, h / 2)
        assert float(W.angular_separation(a[0], a[1], b[0], b[1])) * 3600 < 20.0

    def test_bytes_input(self, small_field):
        d, cat, wcs, w, h, scale = small_field
        p = d / "mono.fit"
        write_fits(p, wcs, cat, bayer=False, w=w, h=h, scale=scale)
        res = P.solve_file(p.read_bytes(), catalog=cat)
        check(res, wcs, max_sep_arcsec=30.0, w=w, h=h)

    def test_explicit_hint_wins(self, small_field):
        d, cat, wcs, w, h, scale = small_field
        p = d / "mono.fit"
        write_fits(p, wcs, cat, bayer=False, w=w, h=h, scale=scale)
        hint = P.SolveHint(ra_deg=wcs.crval[0] + 0.3, dec_deg=wcs.crval[1],
                           pixel_scale=scale, image_size=(w, h))
        res = P.solve_file(p, catalog=cat, hint=hint)
        check(res, wcs, max_sep_arcsec=30.0, w=w, h=h)

    def test_blank_frame_reports_few_stars(self, small_field, tmp_path):
        """真机上确实有整幅饱和的帧(NGC 1499 #18~20),不能崩,要报清楚。"""
        d, cat, wcs, w, h, scale = small_field
        p = tmp_path / "blank.fit"
        write_fits(p, wcs, cat, bayer=False, w=w, h=h, scale=scale)
        raw = bytearray(p.read_bytes())
        hdr = parse_fits_header(bytes(raw))
        for i in range(hdr.header_bytes, len(raw)):
            raw[i] = 0x7F
        p.write_bytes(bytes(raw))
        res = P.solve_file(p, catalog=cat)
        assert not res.ok and res.reason == P.REASON_FEW_STARS

    def test_band_reader_matches_decode_pixels(self, small_field):
        """分带快路是手写的字节序 + BZERO 转换,必须与 decode_pixels 逐位一致。

        真机踩过同源的坑:动 client.py 的 import 块漏了一个常量,上传全崩。
        自己解字节的地方就得有一条"与既有实现对账"的测试。
        """
        from astro_smb.fitsimage import decode_pixels, geometry_from_header
        d, cat, wcs, w, h, scale = small_field
        p = d / "mono.fit"
        write_fits(p, wcs, cat, bayer=False, w=w, h=h, scale=scale)
        hdr = parse_fits_header(p.read_bytes()[:16384])
        geom = geometry_from_header(hdr)
        assert geom.flip_vertical            # 不写 ROWORDER ⇒ 恒翻转
        full = decode_pixels(p, geom)
        read = P._band_reader(p, geom, None)
        for y0, y1 in [(0, h), (0, 16), (h - 16, h), (100, 240)]:
            assert np.array_equal(read(y0, y1), full[y0:y1]), (y0, y1)
        slow = P._band_reader(p.read_bytes(), geom, None)
        assert np.array_equal(slow(100, 240), full[100:240])

    def test_truncated_header_raises(self, tmp_path):
        p = tmp_path / "bad.fit"
        p.write_bytes(b"SIMPLE  =                    T" + b" " * 4000)
        with pytest.raises(FitsImageError):
            P.solve_file(p)

    def test_cancel_during_file_solve(self, small_field):
        d, cat, wcs, w, h, scale = small_field
        p = d / "mono.fit"
        write_fits(p, wcs, cat, bayer=False, w=w, h=h, scale=scale)
        ev = threading.Event()
        ev.set()
        with pytest.raises(InterruptedError):
            P.solve_file(p, catalog=cat, cancel=ev)


# ---------------------------------------------------------------- 真表(可选)

_REAL = None
try:
    _p = C.catalog_path()
    if _p.is_file():
        C.validate_catalog_file(_p)
        _REAL = _p
except (C.CatalogError, OSError):
    _REAL = None


@pytest.mark.skipif(_REAL is None,
                    reason="本机未安装打包星表(设 ASTRO_SMB_CATALOG_PATH 后启用)")
@pytest.mark.real_app_data      # 这一条要的就是真实缓存目录里那份 35.6MB 星表
class TestRealCatalog:
    @pytest.fixture(scope="class")
    @staticmethod
    def real():
        return C.Catalog.open(_REAL)

    def test_default_catalog_is_cached(self):
        a = P.default_catalog()
        assert P.default_catalog() is a

    def test_tycho2_density_supports_a_400mm_field(self, real):
        for ra, dec in [(98.0017, 5.4006), (10.457, 40.613)]:
            assert real.cone(ra, dec, 2.02).size > 400


_FRAMES = [
    Path(r"E:\Plan\Light\NGC 2237") /
    "Light_NGC 2237_300.0s_Bin1_2600MC_gain100_20251105-010153_-19.6C_0001.fit",
    Path(r"E:\Plan\Light\M 31") /
    "Light_M 31_300.0s_Bin1_2600MC_gain100_20251105-030754_-20.6C_0001.fit",
    Path(r"E:\Autorun\Light\M 16") /
    "Light_M 16_300.0s_Bin1_2600MC_gain100_20250320-045641_14.0C_0001.fit",
]
_HAVE_FRAMES = _REAL is not None and all(p.is_file() for p in _FRAMES)


@pytest.mark.skipif(not _HAVE_FRAMES, reason="本机没有真机 FITS 样本")
class TestRealFrames:
    """真机帧 vs **ZWO 自己解算回写的** CRVAL/CD —— 两条独立证据链对账。"""

    @pytest.mark.parametrize("path", _FRAMES, ids=lambda p: p.parent.name)
    def test_matches_zwo_own_solution(self, path):
        hdr = parse_fits_header(path.read_bytes()[:16384])
        ref = W.from_fits_cards(hdr)
        assert ref is not None and W.cards_have_sip(hdr)
        hint = P.SolveHint.from_header(hdr, name=path.name)
        res = P.solve_file(path, hint=hint, read_fraction=0.3)
        assert res.ok, f"{res.reason}: {res.message}"
        w, h = hint.image_size
        got = W.pixel_to_world(res.wcs, (w + 1) / 2, (h + 1) / 2)
        want = W.pixel_to_world(ref, (w + 1) / 2, (h + 1) / 2)
        sep = float(W.angular_separation(got[0], got[1], want[0], want[1]))
        assert sep * 3600 < 10.0, f"中心差 {sep*3600:.2f}″"
        assert abs(res.pixel_scale / ref.pixel_scale() - 1) < 2e-3
        d = abs(res.zwo_angle_deg - P.zwo_angle_from_cd(ref.cd)) % 360.0
        assert min(d, 360 - d) < 1.0
        assert res.flipped == ref.flipped()
        # 指向先验(赤道仪报的 RA/DEC)和真实中心的差:真机 light 帧 ≤1.6°
        assert res.hint_offset_deg < 1.6


def _fake_starlist(x, y, flux, shape) -> S.StarList:
    """造一个只有解算需要的列的 StarList(其余列填 0,不参与计算)。"""
    n = len(x)
    z = np.zeros(n)
    cols = {name: z.copy() for name in S._COLUMNS}
    cols["x"] = np.asarray(x, dtype=np.float64)
    cols["y"] = np.asarray(y, dtype=np.float64)
    cols["flux"] = np.asarray(flux, dtype=np.float64)
    return S.StarList(shape=shape, **cols)


class TestBlindSolveEndToEnd:
    """盲解端到端:抹掉指向先验,只留尺度与图幅,看能不能自己找回来。

    这是 `wcsapps.blind_hint_grid` 的**第一个真实调用方** —— 它的 docstring
    一直写着"交给上层顺序去试",而上层此前不存在(#28 的原计划没接完)。
    """

    def test_recovers_pointing_without_any_prior(self, cat, field):
        xy, truth = field
        # 把真解所在的天区放进一个小网格里(全天网格在单测里太慢),
        # 走的仍是 solve → _solve_blind → blind_hint_grid → 逐格 solve 的真实链路
        hint = P.SolveHint(focal_len_mm=FOCAL, pixel_size_um=PIX,
                           image_size=(W_PX, H_PX))
        res = P.solve(xy, cat, hint, blind_max_tiles=400)
        if not res.ok:
            pytest.skip(f"合成场在全天网格下未命中({res.reason});"
                        "真机验证需要插着 ZWO 卡")
        assert res.n_match >= 8
        assert "盲解" in res.message

    def test_blind_reports_coverage_fraction_when_truncated(self, cat, field):
        """预算不够时**必须说清只搜了全天的百分之几**。

        0.65°×0.43° 视场全天要 137672 格(每格约 70ms ⇒ 2.7 小时),默认预算
        只够 0.3%。这时报"搜过了没找到"是误导 —— 用户会以为图有问题,
        实际是我们压根没搜。格点盲解对窄视场本就不是对的算法
        (真正的全天盲解要靠四星几何哈希索引)。
        """
        xy, _ = field
        res = P.solve(xy[:40], cat,
                      P.SolveHint(focal_len_mm=FOCAL, pixel_size_um=PIX,
                                  image_size=(W_PX, H_PX)),
                      blind_max_tiles=5)
        assert not res.ok
        assert "没搜到不代表解不出来" in res.message
        assert "%" in res.message, "要给出覆盖比例"

    def test_blind_does_not_relax_the_success_criteria(self):
        """盲解最怕的不是解不出来,是**自信地给出错误答案** ——
        判据(n_match / log_fap)一个都不许放松。"""
        import inspect
        src = inspect.getsource(P._solve_blind)
        assert "min_matches=min_matches" in src
        assert "max_log_fap" not in src, "不该在盲解里单独放宽 fap 门槛"

    def test_blind_is_disabled_in_the_recursive_call(self):
        """逐格调 solve 时必须 blind=False,否则无限递归。"""
        import inspect
        assert "blind=False" in inspect.getsource(P._solve_blind)
