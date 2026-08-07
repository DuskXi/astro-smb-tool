"""导星质量逆推验证(guidecheck)的离线单测。

重点在**能不能自证正确**:这个模块将来要用来反证 PHD2,自己不准整条链就塌了。
所以极轴反解一律用"注入已知偏差 → 反解 → 比对真值"的往返验证,
而不是拿几个数字凑一凑看着像。
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import numpy as np
import pytest

from astro_smb import guidecheck as G

LAT = 31.0
T0 = datetime(2026, 7, 25, 22, 0, 0)


def _t(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


# ---------------------------------------------------------------- 正演模型

class TestForwardModel:
    def test_zero_error_means_zero_drift(self):
        for ha, dec in ((-60.0, 30.0), (0.0, -20.0), (45.0, 70.0)):
            dra, ddec = G.drift_rates(ha, dec, LAT, G.PolarError(0.0, 0.0))
            assert abs(dra) < 1e-9 and abs(ddec) < 1e-9

    def test_drift_scales_linearly_for_small_angles(self):
        """小角下近似线性 —— 但正演是**精确**的,所以有真实的二阶偏离。
        容差按总量给(小分量上的相对误差会被放大),1% 足够 fit_polar_error 用。"""
        a = G.drift_rates(30.0, 25.0, LAT, G.PolarError(1 / 60, 0.0))
        b = G.drift_rates(30.0, 25.0, LAT, G.PolarError(3 / 60, 0.0))
        scale = math.hypot(*a)
        assert abs(b[0] - 3 * a[0]) < 0.01 * 3 * scale
        assert abs(b[1] - 3 * a[1]) < 0.01 * 3 * scale

    def test_sign_flips_with_error_sign(self):
        """反号近似反向;二阶交叉项让它不完全对称,容差同样按总量给。"""
        p = G.drift_rates(20.0, 30.0, LAT, G.PolarError(2 / 60, 1 / 60))
        m = G.drift_rates(20.0, 30.0, LAT, G.PolarError(-2 / 60, -1 / 60))
        scale = math.hypot(*p)
        assert abs(p[0] + m[0]) < 0.01 * scale
        assert abs(p[1] + m[1]) < 0.01 * scale

    def test_nonlinearity_is_real_not_a_bug(self):
        """把二阶偏离钉下来:正演不是线性近似,将来谁"修"成完全线性反而是错的。"""
        a = G.drift_rates(30.0, 25.0, LAT, G.PolarError(1 / 60, 0.0))
        b = G.drift_rates(30.0, 25.0, LAT, G.PolarError(3 / 60, 0.0))
        ratio = b[0] / a[0]
        assert ratio != pytest.approx(3.0, abs=1e-6)
        assert 2.9 < ratio < 3.1

    def test_drift_grows_with_time(self):
        pe = G.PolarError(4 / 60, 0.0)
        d5 = G.simulate_track(15.0, 25.0, LAT, pe, 5.0)
        d20 = G.simulate_track(15.0, 25.0, LAT, pe, 20.0)
        assert math.hypot(*d20) > 3.5 * math.hypot(*d5)

    def test_southern_hemisphere_runs(self):
        """南半球(纬度为负)不能炸,也不能返回 0。"""
        dra, ddec = G.drift_rates(20.0, -40.0, -33.0, G.PolarError(3 / 60, 0.0))
        assert math.hypot(dra, ddec) > 0.01

    def test_target_at_pole_degrades_safely(self):
        assert G.simulate_track(0.0, 90.0, LAT, G.PolarError(1 / 60, 0.0), 10.0) \
            == (0.0, 0.0)


# ---------------------------------------------------------------- 极轴反解

class TestPolarInversion:
    @pytest.mark.parametrize("az_arcmin,alt_arcmin", [
        (5.0, 3.0), (-8.0, 2.0), (0.5, -0.5), (12.0, -7.0), (0.0, 4.0),
    ])
    def test_round_trip(self, az_arcmin, alt_arcmin):
        truth = G.PolarError(az_arcmin / 60.0, alt_arcmin / 60.0)
        samples = [(ha, dec) + G.drift_rates(ha, dec, LAT, truth)
                   for ha, dec in ((-45.0, 20.0), (0.0, -25.0), (55.0, 45.0))]
        pe, rms, cond = G.fit_polar_error(samples, LAT)
        assert pe.az * 60 == pytest.approx(az_arcmin, abs=0.05)
        assert pe.alt * 60 == pytest.approx(alt_arcmin, abs=0.05)
        assert rms < 0.05 and cond < 10.0

    def test_single_target_off_equator_is_solvable(self):
        """比经典漂移法强:同时用 RA+DEC,一个目标就够(远离天赤道时)。"""
        truth = G.PolarError(6 / 60, -4 / 60)
        s = [(-40.0, 35.0) + G.drift_rates(-40.0, 35.0, LAT, truth)]
        pe, _rms, cond = G.fit_polar_error(s, LAT)
        assert cond < 10.0
        assert pe.total_arcmin == pytest.approx(truth.total_arcmin, abs=0.1)

    def test_near_equator_is_degenerate(self):
        """真正的简并在赤纬≈0 —— 条件数必须把它标出来。"""
        truth = G.PolarError(5 / 60, 3 / 60)
        s = [(10.0, 0.0) + G.drift_rates(10.0, 0.0, LAT, truth)]
        _pe, _rms, cond = G.fit_polar_error(s, LAT)
        assert cond > 30.0, "赤道附近必须被标记为不可分解"

    def test_classic_dec_only_single_target_is_singular(self):
        """对照:教科书漂移法只看 DEC,单目标恒奇异 —— 这正是它要测两个位置的原因。"""
        a = np.array([[G.drift_rates(0.0, 0.0, LAT, G.PolarError(1, 0))[1],
                       G.drift_rates(0.0, 0.0, LAT, G.PolarError(0, 1))[1]]])
        sv = np.linalg.svd(a, compute_uv=False)
        assert sv.size == 1 or sv[-1] < 1e-12

    def test_absurd_result_is_refused(self):
        with pytest.raises(ValueError):
            G.fit_polar_error([(0.0, 40.0, 500.0, 500.0)], LAT)

    def test_empty_samples_refused(self):
        with pytest.raises(ValueError):
            G.fit_polar_error([], LAT)

    def test_direction_convention(self):
        assert G.PolarError(0.0, 1.0).direction_deg == pytest.approx(0.0)
        assert G.PolarError(1.0, 0.0).direction_deg == pytest.approx(90.0)


# ---------------------------------------------------------------- 抖动

DITHER_LOG = """\
Guiding Begins at 2026-07-25 22:00:00
Pixel scale = 2.05 arc-sec/px, Binning = 1, Focal length = 403 mm
1,10.0,"Mount",0,0,0,0,0,0,0,,0,,0,0,1000,20,0
INFO: DITHER by 3.000, -2.000
2,70.0,"Mount",0,0,0,0,0,0,0,,0,,0,0,1000,20,0
INFO: DITHER by -1.000, 4.000
3,130.0,"Mount",0,0,0,0,0,0,0,,0,,0,0,1000,20,0
Guiding Ends at 2026-07-25 22:05:00
"""


class TestDither:
    def test_extracts_events_with_times(self):
        evs = G.dither_from_log_text(DITHER_LOG)
        assert len(evs) == 2
        assert (evs[0].dx, evs[0].dy) == (3.0, -2.0)
        assert (evs[1].dx, evs[1].dy) == (-1.0, 4.0)
        assert evs[0].time == T0 + timedelta(seconds=10)
        assert evs[1].time == T0 + timedelta(seconds=70)
        assert evs[0].pixel_scale == pytest.approx(2.05)

    def test_no_dither_lines(self):
        assert G.dither_from_log_text("Guiding Begins at 2026-07-25 22:00:00\n") == []

    def test_arcsec_conversion(self):
        ev = G.DitherEvent(T0, 2.0, -3.0)
        assert ev.arcsec(2.05) == pytest.approx((4.1, -6.15))

    def test_each_section_keeps_its_own_pixel_scale(self):
        text = (
            DITHER_LOG
            + "\nGuiding Begins at 2026-07-25 23:00:00\n"
            + "Pixel scale = 1.50 arc-sec/px, Binning = 1, Focal length = 550 mm\n"
            + '1,5.0,"Mount",0,0,0,0,0,0,0,,0,,0,0,1000,20,0\n'
            + "INFO: DITHER by 2.000, 0.000\n")
        evs = G.dither_from_log_text(text)
        assert [ev.pixel_scale for ev in evs] == pytest.approx(
            [2.05, 2.05, 1.50])
        assert evs[-1].arcsec() == pytest.approx((3.0, 0.0))

    def test_dither_is_subtracted_not_excluded(self):
        """抖动要被**减掉**,不能当成漂移 —— 这是用户明确要求的口径。"""
        times = [_t(i * 10.0) for i in range(6)]
        # 目标完全没漂,但第 3 帧之后有一次 10px 的指令抖动
        ra = [100.0] * 6
        dec = [20.0] * 6
        scale = 2.0
        shift = 10.0 * scale / 3600.0           # 度
        for i in range(3, 6):
            dec[i] += shift
        evs = [G.DitherEvent(times[3] - timedelta(seconds=1), 0.0, 10.0)]
        raw = G.fit_center_drift(times, ra, dec)
        fixed = G.fit_center_drift(times, ra, dec, dither=evs, pixel_scale=scale)
        assert abs(raw.dec_rate) > 0.1, "不扣抖动时会被误当成漂移"
        assert abs(fixed.dec_rate) < 1e-6, "扣掉之后应该判定为没有漂移"
        assert fixed.dither_removed is True


# ---------------------------------------------------------------- 漂移与场旋

class TestDrift:
    def test_pure_dec_drift(self):
        times = [_t(i * 5.0) for i in range(7)]
        dec = [20.0 + i * 5.0 / 3600.0 for i in range(7)]     # 1″/分
        fit = G.fit_center_drift(times, [100.0] * 7, dec)
        assert fit.dec_rate == pytest.approx(1.0, abs=1e-6)
        assert fit.ra_rate == pytest.approx(0.0, abs=1e-9)
        assert fit.significant

    def test_ra_drift_includes_cos_dec(self):
        """RA 分量必须是**大圆**距离(乘 cos(dec))—— 这是最常见的错。"""
        dec0 = 60.0
        times = [_t(i * 5.0) for i in range(5)]
        ra = [100.0 + i * (10.0 / 3600.0) for i in range(5)]   # 每步 10″(坐标差)
        fit = G.fit_center_drift(times, ra, [dec0] * 5)
        expect = (10.0 * math.cos(math.radians(dec0))) / 5.0
        assert fit.ra_rate == pytest.approx(expect, rel=1e-6)

    def test_ra_wrap_at_zero(self):
        times = [_t(i * 5.0) for i in range(4)]
        ra = [359.99, 359.995, 0.0, 0.005]        # 跨 0h
        fit = G.fit_center_drift(times, ra, [10.0] * 4)
        assert fit.total_arcsec < 100.0, "跨 0h 不能被算成 360° 的跳变"

    def test_single_frame_and_empty(self):
        assert G.fit_center_drift([_t(0)], [1.0], [2.0]).n == 1
        assert G.fit_center_drift([], [], []).total_rate == 0.0

    def test_unsorted_input_is_sorted(self):
        times = [_t(10.0), _t(0.0), _t(5.0)]
        dec = [20.0 + 10.0 / 3600.0, 20.0, 20.0 + 5.0 / 3600.0]
        assert G.fit_center_drift(times, [1.0] * 3, dec).dec_rate == \
            pytest.approx(1.0, abs=1e-6)


class TestRotation:
    def test_linear_rate(self):
        times = [_t(i * 30.0) for i in range(5)]
        pa = [10.0 + i * 0.25 for i in range(5)]      # 0.5°/小时
        fit = G.fit_position_angle(times, pa)
        assert fit.rate_deg_per_hour == pytest.approx(0.5, abs=1e-6)

    def test_unwraps_across_360(self):
        times = [_t(i * 30.0) for i in range(4)]
        pa = [359.0, 359.5, 0.0, 0.5]
        fit = G.fit_position_angle(times, pa)
        assert abs(fit.total_deg) < 5.0, "缠绕必须解开,不能算成 -359°"

    def test_meridian_flip_is_not_field_rotation(self):
        fit = G.fit_position_angle(
            [_t(i * 10.0) for i in range(5)],
            [20.0, 20.1, 199.9, 200.0, 200.1])
        assert fit.meridian_flip
        assert math.isnan(fit.rate_deg_per_hour)

    def test_degenerate(self):
        assert G.fit_position_angle([_t(0)], [10.0]).rate_deg_per_hour == 0.0


# ---------------------------------------------------------------- 星点通道

class TestSampling:
    @pytest.mark.parametrize("fwhm,level", [
        (1.2, "under"), (2.0, "marginal"), (3.5, "ok"), (0.0, "unknown"),
    ])
    def test_levels(self, fwhm, level):
        assert G.sampling_quality(fwhm, 2.0)[0] == level

    def test_missing_scale(self):
        assert G.sampling_quality(3.0, 0.0)[0] == "unknown"


class TestFwhmBudget:
    def test_decomposition_is_quadrature(self):
        out = G.fwhm_budget(3.0, 0.5)
        g = 2.3548 * 0.5
        assert out["consistent"]
        assert out["seeing"] == pytest.approx(math.sqrt(9.0 - g * g))
        assert out["guiding_share"] == pytest.approx(g * g / 9.0)

    def test_inconsistent_input_is_flagged_not_forced(self):
        """导星项超过总宽 ⇒ 明确说不自洽,不能凑出负的视宁度。"""
        out = G.fwhm_budget(1.0, 2.0)
        assert out["consistent"] is False and "reason" in out
        assert "seeing" not in out

    def test_optics_term(self):
        a = G.fwhm_budget(4.0, 0.3)
        b = G.fwhm_budget(4.0, 0.3, optics_fwhm_arcsec=2.0)
        assert b["seeing"] < a["seeing"]

    def test_missing_total(self):
        assert G.fwhm_budget(0.0, 0.5)["consistent"] is False


# ---------------------------------------------------------------- 交叉判读

def _frames(n, *, drift_arcsec_per_min=0.0, rms=0.4, pa_rate=0.0):
    out = []
    for i in range(n):
        t0 = _t(i * 5.0)
        out.append(G.FrameEvidence(
            t0=t0, t1=t0 + timedelta(minutes=4),
            center_ra=100.0,
            center_dec=20.0 + (i * 5.0 * drift_arcsec_per_min) / 3600.0,
            pa_deg=30.0 + i * 5.0 / 60.0 * pa_rate,
            guide_rms_arcsec=rms, guide_coverage=1.0,
            fwhm_px=3.0, fwhm_arcsec=3.0,
            ellipticity=0.10, n_stars=120))
    return out


class TestCrossValidate:
    def test_good_night(self):
        r = G.cross_validate(_frames(12), pixel_scale_main=1.0)
        assert r.verdict == "good" and r.confidence == "high"

    def test_pretty_curve_but_target_walks(self):
        """本模块存在的理由:①稳 + ③漂。"""
        r = G.cross_validate(_frames(12, drift_arcsec_per_min=1.2, rms=0.45))
        assert r.verdict == "drift"
        assert "导星曲线漂亮" in r.headline

    def test_oag_changes_the_diagnosis(self):
        base = _frames(12, drift_arcsec_per_min=1.2, rms=0.45)
        oag = G.cross_validate(base, is_oag=True)
        sep = G.cross_validate(base, is_oag=False)
        assert "极轴" in oag.headline and "挠曲" in sep.headline

    def test_overguiding(self):
        r = G.cross_validate(
            _frames(12, drift_arcsec_per_min=0.0, rms=2.2),
            pixel_scale_main=1.0)
        assert r.verdict == "overguide"
        assert "星点仍圆" in r.headline

    def test_missing_guide_is_unknown_not_good(self):
        frames = _frames(6)
        for frame in frames:
            frame.guide_rms_arcsec = None
        r = G.cross_validate(frames, pixel_scale_main=1.0)
        assert r.verdict == "unknown"
        assert "缺少同期导星日志" in r.headline

    def test_too_few_solves_says_so(self):
        r = G.cross_validate(_frames(1))
        assert r.verdict == "unknown" and r.confidence == "low"

    def test_rotation_reported(self):
        r = G.cross_validate(_frames(12, pa_rate=0.8))
        assert r.rotation is not None
        assert any("场旋" in f for f in r.findings)

    def test_meridian_flip_suppresses_rotation_verdict(self):
        frames = _frames(6)
        for i, frame in enumerate(frames):
            frame.pa_deg = 20.0 + i * 0.1 + (180.0 if i >= 3 else 0.0)
        r = G.cross_validate(frames)
        assert r.rotation is not None and r.rotation.meridian_flip
        assert any("中天翻转" in finding for finding in r.findings)
        assert not any(finding.startswith("场旋 ") for finding in r.findings)

    def test_no_astral_chars_in_output(self):
        """§7.1:UI 会直接显示这些字符串,星平面字符会被吞掉末尾一个字。"""
        r = G.cross_validate(_frames(12, drift_arcsec_per_min=1.2))
        for s in [r.headline] + r.findings:
            assert not [c for c in s if ord(c) > 0xFFFF], s


class TestDriftGeometryHasSingleSource:
    """帧中心漂移的几何只能有**一套**。

    此前 `guidecheck.fit_center_drift` 用 `Δra·cos δ` 小角近似,
    `wcsapps.drift` 用切平面(gnomonic)投影 —— 同一个物理量两种口径,
    而更完备的那套(还带零阶保持扣 dither、分离原始值与扣除量、给出扣掉线性
    项后的残差)**一直没有调用方**。现在统一走 `wcsapps._project_tangent`。
    """

    def test_uses_the_shared_tangent_projection(self):
        import inspect
        src = inspect.getsource(G.fit_center_drift)
        assert "_project_tangent" in src
        assert "cos" not in src.split('"""')[-1], "不该再有 cos δ 小角近似"

    def test_agrees_with_cos_dec_for_small_offsets(self):
        """小偏移下两种口径应当一致 —— 换几何不能悄悄改变既有结论。"""
        dec0 = 60.0
        times = [_t(i * 5.0) for i in range(5)]
        ra = [100.0 + i * (10.0 / 3600.0) for i in range(5)]
        fit = G.fit_center_drift(times, ra, [dec0] * 5)
        expect = (10.0 * math.cos(math.radians(dec0))) / 5.0
        assert fit.ra_rate == pytest.approx(expect, rel=1e-4)

    def test_wide_scatter_is_refused_not_approximated(self):
        """中心散布超过 90°:不是同一个目标,漂移没有意义 —— 不给数字。"""
        times = [_t(0.0), _t(10.0)]
        fit = G.fit_center_drift(times, [0.0, 200.0], [0.0, 0.0])
        assert fit.n == 2 and fit.total_rate == 0.0

    def test_wcsapps_drift_now_has_a_caller(self):
        """`wcsapps.drift` 与本函数共用同一套投影核心,不再是无主代码。"""
        import inspect
        from astro_smb import wcsapps
        assert "_project_tangent" in inspect.getsource(wcsapps.drift)


# ------------------------------------------------- 真机回归(2026-07-29 NGC 7293)

# ASIAIR 192.0.2.227,EMMC Images/Plan/Light/NGC 7293,13x300s 连续子帧,
# 全部板解算成功(n_match 111~144,残差约 0.5 px)。同夜 PHD2 日志 0 次抖动、
# 0 丢星、覆盖 97.8%,且 raw 误差在整个成像窗口内的**系统漂移只有 0.011″**
# —— 也就是说导星把那颗星摁得死死的,而主镜视场自己走掉了 14″。
# 这一组是本模块的地面真值:它曾经被判成 "good / high",那正是要修的 bug。
_REAL = [
    (22, 51, 45, 337.41205, -20.83976, 179.0819),
    (23, 1, 20, 337.41358, -20.83971, 179.0949),
    (23, 6, 21, 337.41385, -20.83995, 179.0994),
    (23, 11, 22, 337.41412, -20.84006, 179.1011),
    (23, 16, 23, 337.41439, -20.84013, 179.1055),
    (23, 21, 24, 337.41465, -20.84018, 179.1100),
    (23, 26, 25, 337.41489, -20.84029, 179.1134),
    (23, 31, 26, 337.41507, -20.84032, 179.1158),
    (23, 36, 27, 337.41526, -20.84043, 179.1227),
    (23, 41, 28, 337.41554, -20.84051, 179.1218),
    (23, 46, 29, 337.41571, -20.84058, 179.1259),
    (23, 51, 30, 337.41593, -20.84062, 179.1308),
    (23, 56, 31, 337.41612, -20.84067, 179.1315),
]
REAL_SCALE = 1.925          # 主镜 ASI2600MC + 403mm
REAL_SIZE = (6248, 4176)
REAL_LAT, REAL_HA = 31.0, -38.08


def _real_frames(guide_rms=0.79):
    out = []
    for hh, mm, ss, ra, dec, pa in _REAL:
        t1 = datetime(2026, 7, 29, hh, mm, ss)      # 文件名时刻 = 曝光结束
        out.append(G.FrameEvidence(
            t0=t1 - timedelta(seconds=300), t1=t1,
            center_ra=ra, center_dec=dec, pa_deg=pa,
            guide_rms_arcsec=guide_rms, guide_coverage=0.978,
            fwhm_px=2.61, fwhm_arcsec=5.01, ellipticity=0.25,
            theta_r=0.54, n_stars=130))
    return out


def _real_check(**kw):
    kw.setdefault("pixel_scale_main", REAL_SCALE)
    kw.setdefault("image_size", REAL_SIZE)
    kw.setdefault("lat_deg", REAL_LAT)
    kw.setdefault("ha_deg", REAL_HA)
    kw.setdefault("is_oag", True)
    return G.cross_validate(_real_frames(), **kw)


class TestRealNightRegression:
    def test_slow_long_drift_is_not_good(self):
        """**这条就是 bug 本身**:0.19″/分不过裸速率阈值,但跑了 65 分钟。

        旧代码判 "good / high confidence",而同一份 findings 里就写着累计 14″。
        自相矛盾的输出比不给结论更糟 —— 用户会据此认为设备没问题。
        """
        r = _real_check()
        assert r.verdict == "drift", r.headline
        assert r.drift.total_rate < G.DRIFT_SIGNIFICANT, (
            "前提:这个速率确实低于裸阈值,否则这条测试就测不到那个 bug 了")

    def test_walk_is_reported_in_pixels(self):
        """伤害的单位是**主镜像素**,不是角秒/分钟。"""
        r = _real_check()
        walk_px = r.drift.total_arcsec / REAL_SCALE
        assert 6.5 < walk_px < 8.0
        assert any("主镜像素" in f for f in r.findings)

    def test_rotation_survives_the_gate(self):
        """0.044°/小时 曾被 >0.05°/小时 的硬阈值整条压掉,而它信噪比 24。"""
        r = _real_check()
        assert r.rotation is not None
        assert abs(r.rotation.rate_deg_per_hour) < G.ROT_FALLBACK_DEG_PER_HOUR, (
            "前提:实测速率确实低于旧阈值")
        assert abs(r.rotation.total_deg) > G.ROT_SNR * r.rotation.resid_deg
        assert any("画幅角落" in f for f in r.findings)

    def test_two_chains_contradict_each_other(self):
        """#33 的核心产出:漂移反解的极轴误差解释不了实测的场旋。

        漂移只支持约 2′,而实测场旋要 11′ 才够,还反号。两条链本该由同一个
        极轴误差同时决定 —— 对不上就说明现场至少还有第二个机制。
        """
        r = _real_check()
        assert r.polar is not None and r.polar_cond <= 30.0
        assert r.polar_consistent is False
        assert any("互相矛盾" in f for f in r.findings)

    def test_headline_does_not_blame_polar_when_chains_disagree(self):
        """对质否掉极轴之后,结论行不能再让用户去拧极轴螺丝。"""
        r = _real_check()
        assert "极轴" not in r.headline or "解释不了" in r.headline
        assert "OAG" in r.headline or "组件" in r.headline

    def test_guiding_itself_was_genuinely_good(self):
        """PHD2 没说谎:它把导星星点摁住了,只是看不见主镜里发生了什么。"""
        r = _real_check()
        assert any("导星器报告 RMS 0.79" in f for f in r.findings)
        assert "导星曲线漂亮" in r.headline


class TestWalkIsJudgedByAbsolutePositionOnly:
    @staticmethod
    def _slow(rate=0.19, n=13):
        return G.fit_center_drift(
            [_t(i * 5.0) for i in range(n)], [100.0] * n,
            [20.0 + i * 5.0 * rate / 3600.0 for i in range(n)])

    def test_accumulated_walk_triggers_without_rate(self):
        fit = self._slow()
        assert not fit.significant, "裸速率判据在这里就是漏报的"
        ok, why = G.drift_severity(fit, pixel_scale=1.925)
        assert ok and any("主镜像素" in w for w in why)

    def test_same_rate_is_harmless_on_a_coarse_scale(self):
        """同样的角秒速率,像素尺度不同伤害就不同 —— 判据必须跟着尺度走。"""
        fit = self._slow()
        assert G.drift_severity(fit, pixel_scale=1.925)[0]
        assert not G.drift_severity(fit, pixel_scale=20.0)[0]

    def test_smear_does_not_decide_whether_the_target_walked(self):
        """**契约已变**(用户口径):"走没走"只看板解算的绝对位置累计位移。

        曝光内涂抹说的是"这一张糊不糊",走说的是"整组跑没跑掉" —— 两件事。
        早先把涂抹也当成"走"的触发条件,是把两件事混成了一件。
        """
        fit = self._slow()
        # 没有像素尺度、位移也没到角秒退路阈值 ⇒ 无论曝光多长都不算"走"
        short = G.fit_center_drift(
            [_t(i * 1.0) for i in range(6)], [100.0] * 6,
            [20.0 + i * 1.0 * 0.19 / 3600.0 for i in range(6)])
        assert not G.drift_severity(short, exposure_s=600.0, guide_rms=0.1)[0]
        # 但涂抹本身照样算得出来,只是不参与判定
        assert G.exposure_smear(fit, 300.0) == pytest.approx(
            fit.total_rate * 5.0, rel=1e-9)
        assert G.exposure_smear(fit, 0.0) == 0.0

    def test_falls_back_to_accumulated_arcsec_not_rate(self):
        """**契约已变**:没有像素尺度时退回**累计角秒**,而不是退回速率。

        退回速率会让判定重新对"跑了多久"变瞎 —— 那正是最初的 bug。
        """
        ok, why = G.drift_severity(self._slow(rate=1.2))
        assert ok and any("累计位移" in w and "像素" in w for w in why)
        assert not any("兜底" in w or "速率" in w for w in why)

    def test_rotation_noise_is_not_reported(self):
        """抖动大的序列不能把噪声报成场旋。"""
        pas = [30.0 + (0.4 if i % 2 else -0.4) for i in range(12)]
        rot = G.fit_position_angle([_t(i * 5.0) for i in range(12)], pas)
        assert not G.rotation_severity(
            rot, pixel_scale=1.0, image_size=(6000, 4000))[0]


class TestRotationForwardModel:
    """场旋正演 —— 补上此前缺失的一环:场旋只被测过,从没被预测过。"""

    def test_perfect_polar_means_zero_rotation(self):
        """第一版把参考系搞混,零误差竟给出 5.15°/小时的假场旋。"""
        for ha, dec in ((-45.0, 20.0), (0.0, -25.0), (60.0, 55.0)):
            assert G.rotation_rate(ha, dec, LAT, G.PolarError(0.0, 0.0)) == 0.0

    def test_near_pole_with_perfect_polar_is_still_zero(self):
        """同一个 bug 在天极附近给出的是恒星速率 15.04°/小时。"""
        assert abs(G.rotation_rate(0.0, 89.99, LAT, G.PolarError(0.0, 0.0))) < 1e-9

    def test_scales_linearly_and_flips_sign(self):
        a = G.rotation_rate(-45.0, 20.0, LAT, G.PolarError(1 / 60, 0.0))
        b = G.rotation_rate(-45.0, 20.0, LAT, G.PolarError(3 / 60, 0.0))
        assert a != 0.0
        assert b / a == pytest.approx(3.0, abs=0.01)
        assert G.rotation_rate(-45.0, 20.0, LAT, G.PolarError(-1 / 60, 0.0)) \
            == pytest.approx(-a, rel=1e-3)

    def test_rate_is_roughly_duration_independent(self):
        pe = G.PolarError(2 / 60, 1 / 60)
        short = G.rotation_rate(-45.0, 20.0, LAT, pe, 10.0)
        long_ = G.rotation_rate(-45.0, 20.0, LAT, pe, 120.0)
        assert long_ == pytest.approx(short, rel=0.05)

    def test_consistent_pair_is_recognised(self):
        """正演自洽性:拿正演的漂移与正演的场旋喂进去,必须判为自洽。"""
        truth = G.PolarError(25 / 60, -15 / 60)
        ha, dec = -40.0, 35.0
        rate = G.rotation_rate(ha, dec, LAT, truth)
        dra, ddec = G.drift_rates(ha, dec, LAT, truth)
        base = datetime(2026, 7, 29, 22, 0, 0)
        frames = []
        for i in range(12):
            mins = i * 15.0
            t1 = base + timedelta(minutes=mins)
            frames.append(G.FrameEvidence(
                t0=t1 - timedelta(seconds=60), t1=t1,
                center_ra=100.0 + dra * mins / 3600.0 / math.cos(math.radians(dec)),
                center_dec=dec + ddec * mins / 3600.0,
                pa_deg=30.0 + rate * mins / 60.0,
                guide_rms_arcsec=0.5, guide_coverage=1.0,
                fwhm_px=3.0, fwhm_arcsec=3.0, ellipticity=0.1, n_stars=100))
        r = G.cross_validate(frames, pixel_scale_main=1.0,
                             image_size=(6000, 4000), lat_deg=LAT, ha_deg=ha)
        assert r.polar_consistent is True, r.findings
        assert any("自洽" in f for f in r.findings)

    def test_matches_an_independent_derivation(self):
        """**外部校验**:换一条完全不同的路子算同一个量,量级必须吻合。

        另一条路子:残余转动矩阵 ``M = R_bad . R_true^-1`` 的转动向量沿**视线**
        的分量就是场旋(导星消掉的是垂直分量,也就是平移)。它走矩阵对数,
        与 :func:`simulate_rotation` 的"标杆位置角"构造毫无共同代码。

        两者**符号相反**是约定问题不是错误:位置角从北向东量,对应绕 -û 的
        转动;这条推导取的是 +û 分量。正因为符号如此依赖约定,两链对质才
        坚持只比量级(见 :func:`cross_validate`)。
        """
        def los_component(ha, dec, lat, pe, minutes):
            u = G._eq_to_hz(ha, dec, lat)
            tp = G._hz_vec(lat, 0.0 if lat >= 0 else 180.0)
            bad = G._rot(np.array([0.0, 0.0, 1.0]), pe.az) @ (
                G._rot(np.array([0.0, 1.0, 0.0]), -pe.alt) @ tp)
            ang = G.SIDEREAL_DEG_PER_S * minutes * 60.0
            m = G._rot(bad, -ang) @ G._rot(tp, -ang).T
            w = np.array([m[2, 1] - m[1, 2], m[0, 2] - m[2, 0],
                          m[1, 0] - m[0, 1]]) / 2.0
            nw = np.linalg.norm(w)
            if nw < 1e-15:
                return 0.0
            th = math.asin(min(1.0, nw))
            return math.degrees(th * float(
                np.dot(w / nw, G._rot(tp, -ang) @ u)))

        cases = [(-40.0, 35.0, 31.0, G.PolarError(25 / 60, -15 / 60)),
                 (-38.0, -20.84, 31.0, G.PolarError(-1.61 / 60, -1.47 / 60)),
                 (55.0, 60.0, 45.0, G.PolarError(10 / 60, 4 / 60)),
                 (0.0, -30.0, -33.0, G.PolarError(-8 / 60, 6 / 60))]
        for ha, dec, lat, pe in cases:
            mine = G.simulate_rotation(ha, dec, lat, pe, 60.0)
            other = los_component(ha, dec, lat, pe, 60.0)
            assert abs(mine) == pytest.approx(abs(other), rel=1e-6), (ha, dec)
            assert abs(mine) > 1e-6, "这些算例必须给出非零场旋,否则测不出什么"

    def test_sign_is_not_used_as_evidence(self):
        """宇称陷阱的护栏:把实测场旋整体反号,对质结论**不能**改变。

        ASIAIR 的 light 帧恒为镜像,``rotation_deg`` 的旋向因此与天球物理旋向
        相反。若拿符号当证据,同一台设备会凭空多出一个"反号"的假分歧。
        """
        truth = G.PolarError(25 / 60, -15 / 60)
        ha, dec = -40.0, 35.0
        rate = G.rotation_rate(ha, dec, LAT, truth)
        dra, ddec = G.drift_rates(ha, dec, LAT, truth)
        base = datetime(2026, 7, 29, 22, 0, 0)

        def build(sign):
            out = []
            for i in range(12):
                mins = i * 15.0
                t1 = base + timedelta(minutes=mins)
                out.append(G.FrameEvidence(
                    t0=t1 - timedelta(seconds=60), t1=t1,
                    center_ra=100.0 + dra * mins / 3600.0 / math.cos(
                        math.radians(dec)),
                    center_dec=dec + ddec * mins / 3600.0,
                    pa_deg=30.0 + sign * rate * mins / 60.0,
                    guide_rms_arcsec=0.5, guide_coverage=1.0,
                    fwhm_px=3.0, fwhm_arcsec=3.0, ellipticity=0.1, n_stars=100))
            return out

        kw = dict(pixel_scale_main=1.0, image_size=(6000, 4000),
                  lat_deg=LAT, ha_deg=ha)
        a = G.cross_validate(build(+1.0), **kw)
        b = G.cross_validate(build(-1.0), **kw)
        assert a.polar_consistent is True and b.polar_consistent is True


class TestConfidenceCountsChannels:
    def test_missing_shape_channel_degrades_confidence(self):
        """三条链缺一条还报 high,等于把两条链的巧合当三条链的共识。"""
        frames = _real_frames()
        for f in frames:
            f.fwhm_px = None
            f.ellipticity = None
        r = G.cross_validate(frames, pixel_scale_main=REAL_SCALE,
                             image_size=REAL_SIZE)
        assert r.confidence == "medium"
        assert any("证据链到齐" in f for f in r.findings)

    def test_all_three_channels_keeps_high(self):
        assert _real_check().confidence == "high"


class TestPolarFalsifiability:
    """单目标极轴反解**不可证伪** —— 这比"不够精确"严重得多。"""

    TRUTH = G.PolarError(5 / 60, 3 / 60)

    def _sample(self, ha, dec, bias=(0.0, 0.0)):
        dra, ddec = G.drift_rates(ha, dec, LAT, self.TRUTH)
        return (ha, dec, dra + bias[0], ddec + bias[1])

    def test_single_run_residual_is_zero_by_construction(self):
        """2 个方程解 2 个未知数,残差必然是机器零 —— 与拟合好坏无关。"""
        pc = G.polar_from_runs([self._sample(-40.0, 35.0)], LAT)
        assert pc.exactly_determined and not pc.falsifiable
        assert pc.rms < 1e-12
        assert pc.explained is None, "不可证伪时不能给出'解释得通'的结论"

    def test_single_run_hides_a_wrong_model(self):
        """掺入非极轴分量(挠曲):反解大错,残差却依旧是 0。"""
        pc = G.polar_from_runs([self._sample(-40.0, 35.0, bias=(0.5, -0.3))], LAT)
        assert pc.rms < 1e-12, "残差还是 0"
        err = abs(pc.polar.total_arcmin - self.TRUTH.total_arcmin)
        assert err > 1.0, "而结论已经错得很离谱了"
        assert not pc.falsifiable

    def test_multiple_runs_expose_the_wrong_model(self):
        """同样的污染,多目标立刻把它顶出来。"""
        runs = [self._sample(ha, dec, bias=(0.5, -0.3))
                for ha, dec in ((-40.0, 35.0), (10.0, -20.0),
                                (60.0, 5.0), (-70.0, 55.0))]
        pc = G.polar_from_runs(runs, LAT)
        assert pc.falsifiable and pc.n_samples == 4
        assert pc.rms > G.POLAR_RESID_OK
        assert pc.explained is False

    def test_multiple_clean_runs_are_recognised_as_explained(self):
        runs = [self._sample(ha, dec)
                for ha, dec in ((-40.0, 35.0), (10.0, -20.0), (60.0, 5.0))]
        pc = G.polar_from_runs(runs, LAT)
        assert pc.falsifiable and pc.explained is True
        assert pc.polar.total_arcmin == pytest.approx(
            self.TRUTH.total_arcmin, abs=0.05)

    def test_cross_validate_admits_it_is_not_falsifiable(self):
        """单时段判读必须**明说**这个极轴数字推翻不了。"""
        r = _real_check()
        assert r.polar is not None
        assert any("恰定" in f and "构造使然" in f for f in r.findings)

    def test_real_night_two_targets_agree(self):
        """真机 2026-07-30:两个目标各自反解,以及联合反解,必须一致。

        NGC 253 与 NGC 7293 同夜、时角差 7.7°、赤纬差 4.5°,各 7 帧板解算。
        单独 1.48′ / 1.43′,联合 1.45′ 且残差只占观测漂移的 3% ——
        **漂移这条链自洽**,单一极轴误差解释得通。
        (而同期场旋要 10′ 才够,那是另一个机制,见 TestRealNightRegression。)
        """
        runs = [(-29.42, -25.29, 0.156, -0.051),      # NGC 253  01:30-03:01
                (-21.72, -20.84, 0.126, -0.012)]      # NGC 7293 23:59-00:59
        each = [G.polar_from_runs([r], REAL_LAT).polar.total_arcmin for r in runs]
        assert each == pytest.approx([1.48, 1.43], abs=0.1)
        joint = G.polar_from_runs(runs, REAL_LAT)
        assert joint.falsifiable and joint.explained is True
        assert joint.polar.total_arcmin == pytest.approx(1.45, abs=0.1)
        assert joint.rms < 0.02

    def test_measured_rotation_needs_far_more_polar_error(self):
        """真机的场旋不可能来自极轴:要 10′ 以上,而漂移只支持 1.45′。"""
        joint = G.polar_from_runs(
            [(-29.42, -25.29, 0.156, -0.051),
             (-21.72, -20.84, 0.126, -0.012)], REAL_LAT)
        for ha, dec, measured in ((-29.42, -25.29, 0.0440),
                                  (-21.72, -20.84, 0.0353)):
            pred = abs(G.rotation_rate(ha, dec, REAL_LAT, joint.polar))
            assert measured / pred > 5.0, (ha, dec, pred)
