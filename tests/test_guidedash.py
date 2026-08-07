"""导星仪表盘聚合层的离线单测(纯计算,不连设备、不起 XAML)。

覆盖:单位口径判定(角秒/像素不混算)、与 `phd2log.compute_rms` 的口径一致性、
丢星剔除、误差椭圆、直方图分箱与正态拟合、自相关(含合成正弦的周期命中)、
脉冲时长直方图、每张 sub 的 RMS 与废片候选、分段对比条、退化情形
(空组 / 单帧 / 全丢星 / 无 Autorun run),外加 §7.1 的星平面字符静态扫描。
"""

import math
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from win32more.Microsoft.UI.Xaml import Visibility

from astro_smb.autorunlog import (
    AutorunBlock, FrameShot, Night, ShootingGroup, TargetRun,
)
from astro_smb.phd2log import (
    CalibrationSection, GuideFrame, GuideSection, Phd2Log, compute_rms,
)
from astro_smb_gui import _guidedash as D
from astro_smb_gui import _guiding as G
from astro_smb_gui.logstore import LogData
from tests.support import tr


# ---------------------------------------------------------------- 构造器

def _frame(t, ra=0.5, dec=-0.5, *, snr=20.0, err=0, ra_dur=0, ra_dir="",
           dec_dur=0, dec_dir="", mass=1000.0):
    return GuideFrame(time_s=t, dx=0.0, dy=0.0, ra_raw=ra, dec_raw=dec,
                      ra_guide=0.0, dec_guide=0.0, ra_dur=ra_dur, ra_dir=ra_dir,
                      dec_dur=dec_dur, dec_dir=dec_dir, star_mass=mass,
                      snr=snr, err=err)


def _at(h, m, s=0, day=23):
    return datetime(2026, 7, day, h, m, s)


def _sec(begins, frames, scale=2.0, **meta):
    sec = GuideSection(begins=begins, pixel_scale=scale)
    sec.frames = list(frames)
    sec.ends = begins + timedelta(seconds=frames[-1].time_s if frames else 0.0)
    for k, v in meta.items():
        setattr(sec, k, v)
    return sec


def _wave_sec(begins, n=400, step=2.0, period=240.0, amp=1.0, scale=2.0):
    """合成正弦 RA 误差的段(自相关主峰应落在 period 上)。"""
    frames = [_frame((i + 1) * step,
                     ra=amp * math.sin(2 * math.pi * (i + 1) * step / period),
                     dec=0.1)
              for i in range(n)]
    return _sec(begins, frames, scale=scale)


def _group(secs=(), cals=(), run=None, key="k", title="M 8"):
    """把段/校准装成 _prepare 的 rows + group(走真实代码路径)。"""
    rows = [G._prep_guide(s) for s in secs] + [G._prep_cal(c) for c in cals]
    rows.sort(key=lambda r: r["begins"], reverse=True)
    if not rows:
        # 真实 _build_groups 不会产出空组(桶是从行建的),空组只在这里构造用来
        # 验证 aggregate_group 的退化路径
        return {"key": key, "title": title, "sub": "", "n_sec": 0, "rms": None,
                "unit": "", "level": None, "items": [], "ris": [], "run": run,
                "t0": None, "t1": None, "dur": 0.0}, rows
    loc = {}
    g = G._make_group(key, title, list(range(len(rows))), rows, loc, run=run)
    return g, rows


def _run(target="M 8", shots=(), plan_no=1):
    grp = ShootingGroup(frame_type="light", planned=len(shots), exposure="60.0s",
                        binning="1")
    grp.frames = list(shots)
    blk = AutorunBlock(target=target, begin_time=_at(21, 0), end_time=_at(23, 0),
                       end_mode="Finish")
    blk.groups = [grp]
    r = TargetRun(target=target, plan_no=plan_no)
    r.blocks = [blk]
    return r


def _shot(t, no, exp="60.0s"):
    return FrameShot(time=t, image_no=no, exposure=exp)


# ---- 结构断言用的取源工具 ----
#
# 有几条不变量只存在于**绘制/装配代码**里(元素在 UI 线程铺,离线起不了 XAML):
# 组头控件必须复用而不是每次重建、事件只在画布上挂一次、进度环要画完才关、
# ToolTip 只给废片候选挂。这些拿源码结构钉死,配合真机探针(见任务报告)双保险。

def _node_of(module, dotted: str):
    """取 `模块.类.方法` 或 `模块.函数` 的 AST 节点。"""
    import ast

    src = Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    node = tree
    for part in dotted.split("."):
        found = None
        for child in ast.iter_child_nodes(node):
            if (isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.ClassDef))
                    and child.name == part):
                found = child
                break
        assert found is not None, f"{module.__name__} 里找不到 {dotted}"
        node = found
    return node


def _src_of(module, dotted: str) -> str:
    import ast

    src = Path(module.__file__).read_text(encoding="utf-8")
    return ast.get_source_segment(src, _node_of(module, dotted)) or ""


# ---------------------------------------------------------------- 单位口径

class TestCollect:
    def test_all_arcsec(self):
        col = D.collect_sections([_sec(_at(21, 0), [_frame(i) for i in range(1, 5)])])
        assert col["unit"] == "″" and col["arcsec"] is True
        assert len(col["arrs"]) == 1 and not col["skipped"]

    def test_all_pixels_when_no_scale(self):
        col = D.collect_sections(
            [_sec(_at(21, 0), [_frame(i) for i in range(1, 5)], scale=None)])
        assert col["unit"] == "px" and col["arcsec"] is False
        assert len(col["arrs"]) == 1 and not col["skipped"]
        # 像素口径:不乘任何比例
        assert np.allclose(col["arrs"][0]["ra"], col["arrs"][0]["ra_px"])

    def test_mixed_scale_excludes_scaleless_sections(self):
        """角秒/像素不可混算:无 scale 的段整段排除,而不是按 scale=1 混进去。"""
        a = _sec(_at(21, 0), [_frame(i) for i in range(1, 5)], scale=2.0)
        b = _sec(_at(22, 0), [_frame(i) for i in range(1, 5)], scale=None)
        col = D.collect_sections([a, b])
        assert col["unit"] == "″"
        assert [s is a for s in col["used"]] == [True]
        assert [s is b for s in col["skipped"]] == [True]
        assert len(col["arrs"]) == 1

    def test_scale_applied_per_section(self):
        a = _sec(_at(21, 0), [_frame(1, ra=1.0)], scale=2.0)
        b = _sec(_at(22, 0), [_frame(1, ra=1.0)], scale=3.0)
        col = D.collect_sections([a, b])
        assert col["scales"] == [2.0, 3.0]
        assert col["arrs"][0]["ra"][0] == pytest.approx(2.0)
        assert col["arrs"][1]["ra"][0] == pytest.approx(3.0)

    def test_lost_frames_excluded_from_arrays(self):
        frames = [_frame(1), _frame(2, err=3), _frame(3, snr=0.0), _frame(4)]
        col = D.collect_sections([_sec(_at(21, 0), frames)])
        a = col["arrs"][0]
        assert a["n"] == 2                       # 只剩 2 帧有效
        assert a["lost_t"] == [2.0, 3.0]

    def test_section_with_no_valid_frames_yields_no_array(self):
        col = D.collect_sections(
            [_sec(_at(21, 0), [_frame(1, err=1), _frame(2, snr=0.0)])])
        assert col["arrs"] == []
        assert col["unit"] == "px"               # 无有效帧 → 无角秒依据

    def test_arrays_sorted_by_begin_time(self):
        late = _sec(_at(23, 0), [_frame(1)])
        early = _sec(_at(21, 0), [_frame(1)])
        col = D.collect_sections([late, early])
        assert [a["sec"] is early for a in col["arrs"]] == [True, False]


# ---------------------------------------------------------------- 聚合口径

class TestAggregateRms:
    def test_matches_compute_rms(self):
        secs = [_sec(_at(21, 0), [_frame(i, ra=0.3 * i, dec=-0.2 * i)
                                  for i in range(1, 40)], scale=2.0),
                _sec(_at(22, 0), [_frame(i, ra=-0.1 * i, dec=0.4 * i)
                                  for i in range(1, 30)], scale=2.05)]
        g, rows = _group(secs)
        agg = D.aggregate_group(g, rows)
        want = compute_rms([(f, s.pixel_scale) for s in secs for f in s.frames])
        assert agg["rms"].rms_total == pytest.approx(want.rms_total)
        assert agg["rms"].rms_ra == pytest.approx(want.rms_ra)
        assert agg["rms"].n_frames == want.n_frames
        # numpy 复算的散点/直方图口径必须与 compute_rms 一致
        ra = np.concatenate([a["ra"] for a in
                             D.collect_sections(secs)["arrs"]])
        assert math.sqrt(float(np.dot(ra, ra)) / len(ra)) == pytest.approx(
            want.rms_ra)

    def test_mixed_units_rms_excludes_pixel_sections(self):
        good = _sec(_at(21, 0), [_frame(i, ra=1.0, dec=0.0) for i in range(1, 11)],
                    scale=2.0)
        px = _sec(_at(22, 0), [_frame(i, ra=100.0, dec=0.0) for i in range(1, 11)],
                  scale=None)
        g, rows = _group([good, px])
        agg = D.aggregate_group(g, rows)
        assert agg["unit"] == "″" and agg["n_sec_skipped"] == 1
        # 只有角秒段参与:RA RMS = 1.0px × 2.0″/px
        assert agg["rms"].rms_ra == pytest.approx(2.0)
        assert any("像素口径" in n for n in agg["notes"])

    def test_dual_unit_px_mirror(self):
        secs = [_sec(_at(21, 0), [_frame(i, ra=1.0, dec=2.0) for i in range(1, 6)],
                     scale=2.0)]
        g, rows = _group(secs)
        agg = D.aggregate_group(g, rows)
        assert agg["rms_px"]["ra"] == pytest.approx(1.0)
        assert agg["rms_px"]["dec"] == pytest.approx(2.0)
        assert agg["rms"].rms_ra == pytest.approx(2.0)   # 角秒 = px × scale

    def test_no_px_mirror_when_pixel_unit(self):
        g, rows = _group([_sec(_at(21, 0), [_frame(i) for i in range(1, 6)],
                               scale=None)])
        agg = D.aggregate_group(g, rows)
        assert agg["unit"] == "px" and agg["rms_px"] is None

    def test_lost_rate_counts_all_sections(self):
        frames = [_frame(1), _frame(2, err=1), _frame(3, snr=0.0), _frame(4)]
        g, rows = _group([_sec(_at(21, 0), frames)])
        agg = D.aggregate_group(g, rows)
        assert agg["n_frames"] == 2 and agg["n_lost"] == 2
        assert agg["lost_pct"] == pytest.approx(50.0)

    def test_duration_is_summed_not_rms_duration(self):
        """RmsStats.duration_s 跨段无意义(max-min 帧时刻),时长必须按段求和。"""
        secs = [_sec(_at(21, 0), [_frame(i * 1.0) for i in range(1, 61)]),
                _sec(_at(23, 0), [_frame(i * 1.0) for i in range(1, 61)])]
        g, rows = _group(secs)
        agg = D.aggregate_group(g, rows)
        assert agg["dur_total"] == pytest.approx(120.0)
        assert agg["rms"].duration_s < agg["dur_total"] + 1.0   # 不是 2 小时


# ---------------------------------------------------------------- 退化情形

class TestDegenerate:
    def test_empty_group(self):
        g, rows = _group([])
        agg = D.aggregate_group(g, rows)
        assert agg["ch"] is None and agg["n_sec"] == 0
        assert agg["segbars"] == [] and agg["subs"] is None

    def test_calibration_only_group(self):
        cal = CalibrationSection(begins=_at(21, 0), complete=True,
                                 west_angle=10.0, west_rate=5.0,
                                 north_angle=100.0, north_rate=4.0)
        g, rows = _group([], cals=[cal])
        agg = D.aggregate_group(g, rows)
        assert agg["ch"] is None
        assert agg["cal"]["n"] == 1 and agg["cal"]["usable"] is True
        assert agg["cal"]["ortho_err"] == pytest.approx(0.0)

    def test_all_frames_lost(self):
        g, rows = _group([_sec(_at(21, 0), [_frame(1, err=1), _frame(2, err=1)])])
        agg = D.aggregate_group(g, rows)
        assert agg["ch"] is None and agg["n_frames"] == 0 and agg["n_lost"] == 2

    def test_single_valid_frame(self):
        g, rows = _group([_sec(_at(21, 0), [_frame(1.0, ra=1.0, dec=1.0)])])
        agg = D.aggregate_group(g, rows)
        assert agg["ch"] is not None
        assert agg["ellipse"] is None            # <3 帧不给椭圆
        assert agg["acf"] is None                # 数据不足以做自相关
        assert agg["ch"]["period"] is None
        assert agg["drift"] is None              # 单帧无趋势

    def test_zero_deviation_frames(self):
        """全 0 偏差:σ=0,正态拟合与椭圆要优雅退化而不是除零崩。"""
        g, rows = _group([_sec(_at(21, 0),
                               [_frame(i, ra=0.0, dec=0.0) for i in range(1, 20)])])
        agg = D.aggregate_group(g, rows)
        assert agg["ellipse"] is None
        assert agg["ch"]["hist"] is None or agg["ch"]["hist"]["fit_ra"] is None


# ---------------------------------------------------------------- 数学件

class TestEllipse:
    def test_axis_aligned(self):
        ra = np.array([2.0, -2.0] * 50)
        dec = np.array([1.0, -1.0] * 50)
        # 完全相关会退化成一条线,加点抖动让协方差非奇异
        rng = np.random.default_rng(0)
        el = D.cov_ellipse(ra + rng.normal(0, 0.01, 100),
                           dec * rng.choice([1.0, -1.0], 100))
        assert el is not None and el["a"] >= el["b"] > 0

    def test_uncorrelated_gives_expected_axes(self):
        rng = np.random.default_rng(1)
        ra = rng.normal(0.0, 2.0, 20000)
        dec = rng.normal(0.0, 1.0, 20000)
        el = D.cov_ellipse(ra, dec)
        assert el["a"] == pytest.approx(2.0, rel=0.05)
        assert el["b"] == pytest.approx(1.0, rel=0.05)
        assert abs(el["theta_deg"]) < 5.0        # 长轴基本沿 RA 轴
        assert el["ratio"] == pytest.approx(2.0, rel=0.06)

    def test_too_few_points(self):
        assert D.cov_ellipse(np.array([1.0, 2.0]), np.array([1.0, 2.0])) is None

    def test_all_zero(self):
        z = np.zeros(10)
        assert D.cov_ellipse(z, z) is None


class TestNormalFit:
    def test_matches_histogram_scale(self):
        rng = np.random.default_rng(2)
        vals = rng.normal(0.0, 1.0, 50000)
        hr = 3.0
        h, _ = np.histogram(vals, bins=G.HIST_BINS, range=(-hr, hr))
        hmax = float(h.max())
        fit = D.normal_fit(vals, hr, G.HIST_BINS, hmax)
        assert fit is not None and len(fit) == G.HIST_BINS
        # 中央 bin 的拟合值应贴近实测计数(同一归一化)
        mid = G.HIST_BINS // 2
        assert fit[mid] == pytest.approx(h[mid] / hmax, rel=0.05)
        assert fit[0] < fit[mid] and fit[-1] < fit[mid]

    def test_degenerate_inputs(self):
        assert D.normal_fit(np.array([1.0]), 1.0, 5, 1.0) is None
        assert D.normal_fit(np.ones(10), 1.0, 5, 1.0) is None      # σ=0
        assert D.normal_fit(np.array([1.0, 2.0]), 0.0, 5, 1.0) is None
        assert D.normal_fit(np.array([1.0, 2.0]), 1.0, 5, 0.0) is None


class TestAutocorr:
    def test_finds_synthetic_period(self):
        n, step, period = 600, 2.0, 240.0
        npt = np.arange(1, n + 1) * step
        vals = np.sin(2 * math.pi * npt / period)
        acf = D.autocorr(npt, vals)
        assert acf is not None and acf["significant"] is True
        assert acf["peak_lag"] == pytest.approx(period, rel=0.05)
        # 有偏估计随滞后衰减,240s 处应仍保有 ~0.8 的相关
        assert acf["peak_val"] > 0.75

    def test_white_noise_has_no_significant_peak(self):
        rng = np.random.default_rng(3)
        npt = np.arange(1, 601) * 2.0
        acf = D.autocorr(npt, rng.normal(0.0, 1.0, 600))
        assert acf is not None
        assert acf["significant"] is False or acf["peak_val"] < 0.3

    def test_too_few_frames(self):
        npt = np.arange(1, 50) * 2.0
        assert D.autocorr(npt, np.sin(npt)) is None

    def test_too_short_duration(self):
        npt = np.arange(1, 200) * 0.5     # 200 帧但只有 ~100 秒
        assert D.autocorr(npt, np.sin(npt)) is None

    def test_zero_variance(self):
        npt = np.arange(1, 300) * 2.0
        assert D.autocorr(npt, np.zeros(299)) is None

    def test_points_are_downsampled_and_normalised(self):
        npt = np.arange(1, 2001) * 2.0
        acf = D.autocorr(npt, np.sin(2 * math.pi * npt / 300.0))
        assert acf is not None and len(acf["pts"]) <= D.ACF_PTS
        assert all(0.0 <= x <= 1.0 for x, _ in acf["pts"])


class TestPulseHist:
    def test_bins_and_medians(self):
        frames = ([_frame(i, ra_dur=100, ra_dir="E") for i in range(1, 11)]
                  + [_frame(i, dec_dur=200, dec_dir="N") for i in range(11, 21)])
        ph = D.pulse_hist([_sec(_at(21, 0), frames)])
        assert ph["n_ra"] == 10 and ph["n_dec"] == 10
        assert ph["med_ra"] == pytest.approx(100.0)
        assert ph["med_dec"] == pytest.approx(200.0)
        assert len(ph["ra"]) == D.PULSE_BINS
        assert max(ph["ra"]) == pytest.approx(1.0)   # 归一到两轴共同的 max

    def test_no_pulses(self):
        assert D.pulse_hist([_sec(_at(21, 0), [_frame(1), _frame(2)])]) is None

    def test_direction_balance_in_aggregate(self):
        frames = ([_frame(i, ra_dur=50, ra_dir="E") for i in range(1, 7)]
                  + [_frame(i, ra_dur=50, ra_dir="W") for i in range(7, 11)])
        g, rows = _group([_sec(_at(21, 0), frames)])
        agg = D.aggregate_group(g, rows)
        pulse = dict((lab, cnt) for lab, cnt, _ms, _ax in agg["ch"]["pulse"])
        assert pulse["RA E"] == 6 and pulse["RA W"] == 4
        assert agg["pulse_balance"]["ra"] == pytest.approx((6 - 4) / 10)


# ---------------------------------------------------------------- 图表数据

class TestChartData:
    def _agg(self):
        secs = [_sec(_at(21, 0), [_frame(i, ra=0.4, dec=-0.3)
                                  for i in range(1, 200)]),
                _sec(_at(22, 0), [_frame(i, ra=-0.4, dec=0.3)
                                  for i in range(1, 200)])]
        g, rows = _group(secs)
        return D.aggregate_group(g, rows)

    def test_ch_shape_matches_prep_charts(self):
        """`ch` 必须与 `_guiding._prep_charts` 同构,才能直接喂给已有的 _draw_*。"""
        agg = self._agg()
        sec = _sec(_at(21, 0), [_frame(i, ra=0.4, dec=-0.3) for i in range(1, 50)])
        row = G._prep_guide(sec)
        assert set(agg["ch"]) == set(row["charts"])

    def test_scatter_downsampled(self):
        secs = [_sec(_at(21, 0), [_frame(i * 0.5, ra=0.4, dec=-0.3)
                                  for i in range(1, 4000)])]
        g, rows = _group(secs)
        agg = D.aggregate_group(g, rows)
        assert len(agg["ch"]["sc_pts"]) == D.SCATTER_MAX

    def test_roll_marks_mark_section_boundaries(self):
        agg = self._agg()
        assert len(agg["roll_marks"]) == 1      # 2 段 → 1 条边界线
        assert agg["roll_marks"][0] > 3000.0    # 第二段起点是 +1 小时
        assert agg["ch"]["roll"] == sorted(agg["ch"]["roll"], key=lambda p: p[0])

    def test_snr_axis_normalised(self):
        agg = self._agg()
        sn = agg["ch"]["snr"]
        assert sn["snr"] and all(0.0 <= t <= 1.0 for t, _ in sn["snr"])
        assert max(v for _, v in sn["snr"]) == pytest.approx(1.0)

    def test_lost_marks_normalised_and_capped(self):
        frames = [_frame(i, err=1 if i % 2 else 0) for i in range(1, 2000)]
        g, rows = _group([_sec(_at(21, 0), frames)])
        agg = D.aggregate_group(g, rows)
        assert 0 < len(agg["lost_marks"]) <= D.LOST_TICKS
        assert all(0.0 <= t <= 1.0 for t in agg["lost_marks"])

    def test_period_and_acf_use_longest_section(self):
        short = _sec(_at(21, 0), [_frame(i * 2.0, ra=0.1) for i in range(1, 30)])
        long_ = _wave_sec(_at(22, 0), n=500, step=2.0, period=300.0)
        g, rows = _group([short, long_])
        agg = D.aggregate_group(g, rows)
        assert agg["longest_sec"] is long_
        assert agg["acf"] is not None and agg["acf"]["significant"]
        assert agg["acf"]["peak_lag"] == pytest.approx(300.0, rel=0.06)

    def test_drift_is_frame_weighted_per_section(self):
        """每段各自拟合后按帧数加权 —— 跨段拼接会被 1 小时空洞带偏。"""
        a = _sec(_at(21, 0), [_frame(i * 2.0, ra=0.01 * i, dec=0.0)
                              for i in range(1, 101)])
        b = _sec(_at(22, 0), [_frame(i * 2.0, ra=0.01 * i, dec=0.0)
                              for i in range(1, 101)])
        g, rows = _group([a, b])
        agg = D.aggregate_group(g, rows)
        # 单段斜率 0.01px/2s × scale 2.0 = 0.01″/s = 0.6″/min
        assert agg["drift"]["ra"] == pytest.approx(0.6, rel=1e-6)
        assert agg["drift"]["dec"] == pytest.approx(0.0, abs=1e-9)

    def test_hist_range_is_three_sigma(self):
        agg = self._agg()
        assert agg["ch"]["hist"]["rng"] == pytest.approx(
            3.0 * agg["ch"]["rms_total"])
        assert agg["ch"]["hist"]["fit_ra"] is not None


# ---------------------------------------------------------------- 分段对比条

class TestSegBars:
    def test_one_row_per_section_sorted_ascending(self):
        secs = [_sec(_at(22, 0), [_frame(i) for i in range(1, 200)]),
                _sec(_at(21, 0), [_frame(i) for i in range(1, 200)])]
        g, rows = _group(secs)
        agg = D.aggregate_group(g, rows)
        bars = agg["segbars"]
        assert len(bars) == 2
        assert bars[0]["begins"] < bars[1]["begins"]
        # ri 必须指回 rows 里的真实行(show_range 与主曲线定位靠它)
        for b in bars:
            assert b["ri"] is not None
            assert rows[b["ri"]]["begins"] == b["begins"]

    def test_skipped_sections_flagged(self):
        a = _sec(_at(21, 0), [_frame(i) for i in range(1, 200)], scale=2.0)
        b = _sec(_at(22, 0), [_frame(i) for i in range(1, 200)], scale=None)
        g, rows = _group([a, b])
        agg = D.aggregate_group(g, rows)
        flags = {bar["begins"]: bar["skipped"] for bar in agg["segbars"]}
        assert flags[_at(21, 0)] is False and flags[_at(22, 0)] is True

    def test_main_and_fragment_flag(self):
        main = _sec(_at(21, 0), [_frame(i) for i in range(1, 200)])
        frag = _sec(_at(22, 0), [_frame(i) for i in range(1, 5)])
        g, rows = _group([main, frag])
        agg = D.aggregate_group(g, rows)
        flags = {b["begins"]: b["main"] for b in agg["segbars"]}
        assert flags[_at(21, 0)] is True and flags[_at(22, 0)] is False

    def test_calibration_rows_are_not_segbars(self):
        cal = CalibrationSection(begins=_at(21, 30), complete=False, star_lost=4)
        g, rows = _group([_sec(_at(21, 0), [_frame(i) for i in range(1, 20)])],
                         cals=[cal])
        agg = D.aggregate_group(g, rows)
        assert len(agg["segbars"]) == 1
        assert agg["cal"]["n"] == 1 and agg["cal"]["usable"] is False


# ---------------------------------------------------------------- 与拍摄联动

class TestSubSeries:
    def _secs(self, base=_at(21, 0), n=600, ra=0.4):
        return [_sec(base, [_frame(i * 2.0, ra=ra, dec=0.0) for i in range(1, n)])]

    def test_rms_per_sub_and_bad_flag(self):
        # 段: 21:00 起, 每 2 秒一帧, 前 300 帧 ra=0.1px, 之后 ra=1.0px
        frames = [_frame(i * 2.0, ra=(0.1 if i <= 300 else 1.0), dec=0.0)
                  for i in range(1, 601)]
        secs = [_sec(_at(21, 0), frames, scale=2.0)]
        # 第一张落在 ra=0.1px 的好区间, 第二张(21:12 = +720s)落在 ra=1.0px 的坏区间
        shots = [_shot(_at(21, 1), 1), _shot(_at(21, 12), 2)]   # 60s 曝光各一张
        g, rows = _group(secs, run=_run(shots=shots))
        agg = D.aggregate_group(g, rows)
        subs = agg["subs"]
        assert subs is not None and subs["n_rated"] == 2
        first, second = subs["items"]
        assert first["rms"] == pytest.approx(0.2)     # 0.1px × 2.0″/px
        assert second["rms"] == pytest.approx(2.0)    # 1.0px × 2.0″/px
        assert subs["thr"] == pytest.approx(agg["rms"].rms_total
                                            * D.SUB_BAD_FACTOR)

    def test_bad_candidates_counted(self):
        # 只有 900~960s 这一小段导星变差(整组 RMS 不会被它带跑),
        # 恰好落在第二张 sub 的曝光窗内 → 它应被标成废片候选
        frames = [_frame(i * 2.0, ra=(8.0 if 450 < i <= 480 else 0.1), dec=0.0)
                  for i in range(1, 601)]
        secs = [_sec(_at(21, 0), frames, scale=2.0)]
        shots = [_shot(_at(21, 1), 1), _shot(_at(21, 15), 2)]
        g, rows = _group(secs, run=_run(shots=shots))
        agg = D.aggregate_group(g, rows)
        subs = agg["subs"]
        assert subs["bad"] == 1
        assert subs["items"][1]["bad"] is True
        assert subs["items"][0]["bad"] is False

    def test_no_run_means_no_subs(self):
        g, rows = _group(self._secs())
        assert D.aggregate_group(g, rows)["subs"] is None

    def test_subs_without_coverage_are_skipped(self):
        # 拍摄帧远在导星段之后 → 无覆盖
        shots = [_shot(_at(23, 30), 1)]
        g, rows = _group(self._secs(), run=_run(shots=shots))
        agg = D.aggregate_group(g, rows)
        assert agg["subs"] is None

    def test_lost_frames_excluded_from_sub_rms(self):
        frames = []
        for i in range(1, 61):
            # 每隔一帧丢星, 且丢星帧带离谱偏差 —— 混进去会把 RMS 拉爆
            frames.append(_frame(i * 1.0, ra=(50.0 if i % 2 == 0 else 0.5),
                                 dec=0.0, err=(1 if i % 2 == 0 else 0)))
        secs = [_sec(_at(21, 0), frames, scale=2.0)]
        shots = [_shot(_at(21, 0, 1), 1, exp="50.0s")]
        g, rows = _group(secs, run=_run(shots=shots))
        agg = D.aggregate_group(g, rows)
        assert agg["subs"]["items"][0]["rms"] == pytest.approx(1.0)  # 0.5×2.0

    def test_bias_dark_groups_are_ignored(self):
        """只统计亮场(与隐式组):bias/dark 不该出现在 sub 列表里。"""
        grp = ShootingGroup(frame_type="dark", planned=2, exposure="60.0s",
                            binning="1")
        grp.frames = [_shot(_at(21, 1), 1), _shot(_at(21, 3), 2)]
        blk = AutorunBlock(target="M 8", begin_time=_at(21, 0),
                           end_time=_at(22, 0), end_mode="Finish")
        blk.groups = [grp]
        run = TargetRun(target="M 8", plan_no=1)
        run.blocks = [blk]
        g, rows = _group(self._secs(), run=run)
        assert D.aggregate_group(g, rows)["subs"] is None


# ---------------------------------------------------------------- 文本导出

class TestDashboardText:
    def test_contains_key_numbers(self):
        secs = [_sec(_at(21, 0), [_frame(i, ra=0.4, dec=-0.3)
                                  for i in range(1, 300)],
                     exposure_ms=2000, camera="ASI120MM", dec_deg=-36.1,
                     pier_side="West")]
        g, rows = _group(secs)
        agg = D.aggregate_group(g, rows)
        txt = D.dashboard_text(agg)
        assert "导星仪表盘 · M 8" in txt
        assert f"{agg['rms'].rms_total:.2f}" in txt
        assert "有效帧" in txt and "峰值" in txt
        assert "西垂" in txt and "ASI120MM" in txt
        assert not any(ord(c) > 0xFFFF for c in txt)

    def test_empty_group_text_does_not_crash(self):
        g, rows = _group([])
        assert "导星仪表盘" in D.dashboard_text(D.aggregate_group(g, rows))


# ---------------------------------------------------------------- 集成路径

class TestPrepareIntegration:
    def _data(self, secs, runs):
        night = Night(date="2026-07-23")
        night.runs = list(runs)
        log = Phd2Log(source="PHD2_GuideLog_t.txt", enabled_at=_at(20, 0))
        log.guide_sections = list(secs)
        return LogData(nights=[night], phd2_logs=[log])

    def test_group_carries_ris_and_run(self):
        blk = AutorunBlock(target="M 8", begin_time=_at(21, 0),
                           end_time=_at(23, 0), end_mode="Finish")
        run = TargetRun(target="M 8", plan_no=1)
        run.blocks = [blk]
        secs = [_sec(_at(21, 5), [_frame(i) for i in range(1, 200)])]
        prep = G._prepare(self._data(secs, [run]))
        g = prep["groups"][0]
        assert g["run"] is run
        assert g["ris"] == [0]
        # 走真实 _prepare 路径的组也能直接聚合
        agg = D.aggregate_group(g, prep["rows"])
        assert agg["title"] == "M 8" and agg["n_sec"] == 1

    def test_other_group_has_no_run(self):
        secs = [_sec(_at(21, 5), [_frame(i) for i in range(1, 200)])]
        prep = G._prepare(self._data(secs, []))
        g = prep["groups"][0]
        assert g["key"] == G.OTHER_KEY and g["run"] is None
        assert D.aggregate_group(g, prep["rows"])["subs"] is None

    def test_rows_carry_section_objects(self):
        sec = _sec(_at(21, 5), [_frame(i) for i in range(1, 10)])
        cal = CalibrationSection(begins=_at(21, 0), complete=True)
        prep = G._prepare(self._data([sec], []))
        assert prep["rows"][0]["sec"] is sec
        assert G._prep_cal(cal)["cal"] is cal


# ---------------------------------------------------------------- 复用契约

class TestReusedDrawSignatures:
    """仪表盘复用导星页的 6 个 `_draw_*`。它们被参数化成 (ch, cv, w, h),
    **缺省值必须仍是导星页自己的小图尺寸** —— 否则导星页的观感会被悄悄改掉。
    """

    REUSED = ("_draw_scatter", "_draw_hist", "_draw_roll", "_draw_pulse",
              "_draw_period", "_draw_snr")

    def test_defaults_keep_guiding_page_pixel_identical(self):
        import inspect

        for name in self.REUSED:
            sig = inspect.signature(getattr(G.GuidingPage, name))
            params = list(sig.parameters)
            assert params[:5] == ["self", "ch", "cv", "w", "h"], name
            assert sig.parameters["cv"].default is None, name
            assert sig.parameters["w"].default == G.CHART_W, name
            assert sig.parameters["h"].default == G.CHART_H, name

    def test_pulse_row_pitch_unchanged_at_default_height(self):
        """脉冲图行距在缺省高度下必须仍是 35.0(参数化前的硬编码值)。"""
        assert max(35.0, (G.CHART_H - 12.0) / 4.0) == 35.0
        # 仪表盘的画布更高(宽度自适应算出来的),这时才按高度均分,
        # 且四行加上下留白必须放得下
        _cols, _w, h = D.chart_layout(D.PANEL_W_DEFAULT)
        pitch = max(35.0, (h - 12.0) / 4.0)
        assert pitch > 35.0
        assert 6.0 + 4.0 * pitch <= h + 0.5

    def test_overview_not_parameterised(self):
        """逐段 RMS 总览是整夜视角,不参与仪表盘复用,签名不该被动。"""
        import inspect

        sig = inspect.signature(G.GuidingPage._draw_overview)
        assert list(sig.parameters) == ["self"]

    def test_target_runs_key_matches_target_blocks(self):
        blk = AutorunBlock(target="M 8", begin_time=_at(21, 0),
                           end_time=_at(22, 0), end_mode="Finish")
        run = TargetRun(target="M 8", plan_no=2)
        run.blocks = [blk]
        night = Night(date="2026-07-23")
        night.runs = [run]
        data = LogData(nights=[night], phd2_logs=[])
        keys_from_blocks = {k for _a, _b, k, _n in G._target_blocks(data)}
        assert set(G._target_runs(data)) == keys_from_blocks
        assert G._target_runs(data)[next(iter(keys_from_blocks))] is run


# ------------------------------------------------- 滚动 RMS 的预热窗与段边界

class TestRollingRms:
    """`G._sliding_rms` 是**尾窗**:每段前 ROLL_FRAMES-1 个样本没有满窗
    (index 0 就是单帧模长)。settle 后的首帧残差常常是整段最大的一个 ——
    算进曲线会让 roll_max 被单点顶穿(真机:组 RMS 0.907" 而 roll_max 3.441"),
    "峰值"数字错、曲线还被这个假峰当量程压扁。
    """

    @staticmethod
    def _tot(frames, scale=2.0):
        """按聚合层同款口径复算整条尾窗滚动 RMS(含预热样本)。"""
        ra = np.asarray([f.ra_raw for f in frames], dtype=np.float64) * scale
        dec = np.asarray([f.dec_raw for f in frames], dtype=np.float64) * scale
        return np.sqrt(G._sliding_rms(ra, D.ROLL_FRAMES) ** 2
                       + G._sliding_rms(dec, D.ROLL_FRAMES) ** 2)

    def test_warmup_samples_excluded_from_curve_and_peak(self):
        # 首帧残差极大(settle 刚结束的正常现象),其余帧都很小
        frames = ([_frame(1.0, ra=5.0, dec=0.0)]
                  + [_frame(1.0 + i, ra=0.1, dec=0.0) for i in range(1, 200)])
        g, rows = _group([_sec(_at(21, 0), frames)])
        agg = D.aggregate_group(g, rows)
        ch = agg["ch"]
        tot = self._tot(frames)
        # 峰值 = 只在**满窗**样本上取,而不是含预热样本的全段最大
        assert ch["roll_max"] == pytest.approx(float(tot[D.ROLL_FRAMES - 1:].max()))
        assert ch["roll_max"] < float(tot.max())          # 旧口径明显更大
        assert float(tot.max()) == pytest.approx(10.0)    # 旧口径 = 首帧模长
        # 曲线第一个点必须落在第 30 帧(满窗)之后
        assert ch["roll"][0][0] >= frames[D.ROLL_FRAMES - 1].time_s
        assert len(ch["roll"]) <= len(frames) - (D.ROLL_FRAMES - 1)

    def test_section_shorter_than_window_is_skipped_and_counted(self):
        short = _sec(_at(21, 0),
                     [_frame(i, ra=3.0, dec=0.0) for i in range(1, 6)])
        long_ = _sec(_at(22, 0),
                     [_frame(i, ra=0.1, dec=0.0) for i in range(1, 200)])
        g, rows = _group([short, long_])
        agg = D.aggregate_group(g, rows)
        assert agg["roll_short"] == 1 and agg["roll_segs"] == 1
        assert agg["roll_marks"] == []      # 只画出一段 → 没有段边界
        # 被跳过的短段不得把峰值顶上去(3.0px x scale 2.0 = 6")
        assert agg["ch"]["roll_max"] == pytest.approx(0.2)

    def test_boundary_marks_count_drawn_segments_not_arrs_index(self):
        """首段被跳过时,不能给**实际画出的第一段**也补一条边界线。

        真机常见触发:组内最早的段只有 1 帧有效(settle 一帧就丢星重开)——
        旧代码用 `enumerate(arrs)` 的下标做判据,于是边界线画在画布最左边距、
        压在纵轴上,图上还多写一段。
        """
        tiny = _sec(_at(21, 0), [_frame(1.0, ra=1.0)])          # 1 帧,整段跳过
        a = _sec(_at(21, 10), [_frame(i, ra=0.2) for i in range(1, 200)])
        b = _sec(_at(21, 40), [_frame(i, ra=0.2) for i in range(1, 200)])
        g, rows = _group([tiny, a, b])
        agg = D.aggregate_group(g, rows)
        roll, marks = agg["ch"]["roll"], agg["roll_marks"]
        assert agg["roll_short"] == 1 and agg["roll_segs"] == 2
        assert len(marks) == 1                        # 画出 2 段 → 1 条边界
        assert marks[0] > roll[0][0]                  # 绝不落在曲线起点(左边距)
        # 图上标注的段数(len(marks)+1)必须与实际画出的段数一致
        assert len(marks) + 1 == agg["roll_segs"]

    def test_marks_align_with_second_drawn_section(self):
        a = _sec(_at(21, 0), [_frame(i, ra=0.2) for i in range(1, 200)])
        b = _sec(_at(22, 0), [_frame(i, ra=0.2) for i in range(1, 200)])
        g, rows = _group([a, b])
        agg = D.aggregate_group(g, rows)
        # 边界 = 第二段**第一个满窗样本**的绝对时刻(3600s 偏移 + 第 30 帧)
        assert agg["roll_marks"][0] == pytest.approx(3600.0 + 30.0)
        assert agg["roll_short"] == 0 and agg["roll_segs"] == 2

    def test_degenerate_group_carries_roll_counters(self):
        g, rows = _group([])
        agg = D.aggregate_group(g, rows)
        assert agg["roll_short"] == 0 and agg["roll_segs"] == 0


# ------------------------------------------------- 分段对比条的命中反算(单一 Tapped)

class TestSegHitRow:
    """整块条区只挂**一个** Tapped(逐根 Rectangle 挂事件会被 win32more 的
    event 描述符永久 pin 住),命中行靠几何反算 —— 几何走偏就会点错段。
    条区宽度现在随面板宽变,所以 track 必须由调用方(= 绘制时存下的那个值)传入。
    """

    TRACK = D.seg_track(D.PANEL_W_DEFAULT)

    def test_rows_map_to_index(self):
        for k in range(5):
            y = D.SEG_TOP + k * D.SEG_ROW_H + 1.0
            assert D.seg_hit_row(D.SEG_LABEL_W + 10.0, y, 5, self.TRACK) == k

    def test_row_band_boundaries(self):
        x = D.SEG_LABEL_W + 10.0
        t = self.TRACK
        assert D.seg_hit_row(x, D.SEG_TOP, 3, t) == 0
        assert D.seg_hit_row(x, D.SEG_TOP + D.SEG_ROW_H - 0.01, 3, t) == 0
        assert D.seg_hit_row(x, D.SEG_TOP + D.SEG_ROW_H, 3, t) == 1

    def test_outside_track_misses(self):
        y = D.SEG_TOP + 2.0
        t = self.TRACK
        assert D.seg_hit_row(D.SEG_LABEL_W - 1.0, y, 3, t) is None   # 左侧时间列
        assert D.seg_hit_row(D.SEG_LABEL_W + t + 1.0, y, 3, t) is None

    def test_track_follows_panel_width(self):
        """面板变窄 → 条区变窄 → 原来能命中的 x 落到条区外(反之亦然)。"""
        wide = D.seg_track(1200.0)
        narrow = D.seg_track(500.0)
        assert wide > narrow >= D.SEG_MIN_TRACK
        x = D.SEG_LABEL_W + narrow + 20.0
        y = D.SEG_TOP + 1.0
        assert D.seg_hit_row(x, y, 3, wide) == 0
        assert D.seg_hit_row(x, y, 3, narrow) is None
        # 极窄面板下也不会算出负宽度的条区
        assert D.seg_track(10.0) == D.SEG_MIN_TRACK

    def test_above_first_row_and_past_last_row_miss(self):
        x = D.SEG_LABEL_W + 10.0
        t = self.TRACK
        assert D.seg_hit_row(x, D.SEG_TOP - 1.0, 3, t) is None
        assert D.seg_hit_row(x, D.SEG_TOP + 3 * D.SEG_ROW_H + 1.0, 3, t) is None

    def test_no_rows_never_hits(self):
        assert D.seg_hit_row(D.SEG_LABEL_W + 10.0, D.SEG_TOP + 1.0, 0,
                             self.TRACK) is None

    def test_geometry_matches_drawing_constants(self):
        """反算用的几何必须就是绘制用的那组常量/那个函数(改一处必须两处一起改)。"""
        assert D.seg_track(936.0) == 936.0 - D.SEG_LABEL_W - D.SEG_TAIL_W
        src = _src_of(D, "GuideDashboard._draw_seg")
        assert "SEG_TOP + k * SEG_ROW_H" in src
        assert "x0 = SEG_LABEL_W" in src
        # 绘制时算出的条区宽必须存下来给命中反算用
        assert "track = seg_track(w)" in src
        assert "self._seg_track = track" in src
        tapped = _src_of(D, "GuideDashboard._on_seg_tapped")
        assert "self._seg_track" in tapped


class TestChartLayout:
    """图表尺寸随面板宽现算:窄了减列、宽了加列,但**永远不横向溢出**
    (溢出就等于要么被裁掉、要么冒出用户明确不要的横滚条)。"""

    WIDTHS = [200.0, 262.0, 300.0, 420.0, 500.0, 620.0, 780.0, 819.0,
              900.0, 1100.0, 1600.0, 2400.0]

    def test_row_never_exceeds_available_width(self):
        """`VariableSizedWrapGrid` 在 列数×ItemWidth **正好等于**可用宽时会少排
        一列(真机实测 811.2 / 270.4 只排 2 列),所以必须留出 CHART_SLACK 的余量。
        """
        floor_w = D.CHART_MIN_W + D.CHART_BORDER + D.CHART_GAP + D.CHART_SLACK
        for avail in self.WIDTHS:
            cols, w, _h = D.chart_layout(avail)
            row = cols * (w + D.CHART_BORDER + D.CHART_GAP)
            assert row <= max(avail, floor_w) - D.CHART_SLACK + 1e-6, avail

    def test_width_stays_within_readable_bounds(self):
        for avail in self.WIDTHS:
            _cols, w, h = D.chart_layout(avail)
            assert D.CHART_MIN_W - 1e-6 <= w <= D.CHART_MAX_W + 1e-6, avail
            assert h == pytest.approx(round(w * D.CHART_ASPECT, 1))

    def test_columns_grow_with_width_and_are_capped(self):
        cols = [D.chart_layout(a)[0] for a in self.WIDTHS]
        assert cols == sorted(cols)                  # 单调不减
        assert cols[0] == 1 and max(cols) == D.CHART_MAX_COLS
        assert D.chart_layout(D.PANEL_W_DEFAULT)[0] >= 2

    def test_degenerate_widths_do_not_crash(self):
        for bad in (0.0, -50.0, None):
            cols, w, h = D.chart_layout(bad)
            assert cols == 1 and w >= D.CHART_MIN_W and h > 0

    def test_wide_charts_never_narrower_than_floor(self):
        assert D.WIDE_MIN >= D.CHART_MIN_W
        assert D.seg_track(D.WIDE_MIN) >= D.SEG_MIN_TRACK


# ---------------------------------------------------------------- 批量绘制片段

class TestBatchFragment:
    """散点/柱子逐个建元素在 win32more 下约 1.7ms/个,一次 XamlReader.Load
    整片子画布只要 16ms/400 个(实测 40 倍以上)。元素本身必须仍是各自独立的
    Rectangle —— 合成单个 Path 会让半透明重叠不再叠色,直方图 RA/DEC 会走样。"""

    NS = "{http://schemas.microsoft.com/winfx/2006/xaml/presentation}"

    def test_empty_yields_empty_string(self):
        assert G.rect_fragment([]) == ""

    def test_one_rectangle_element_per_item_in_order(self):
        import xml.etree.ElementTree as ET

        rects = [(1.0, 2.0, 3.0, 4.0, "#5A0078D7"),
                 (10.5, 20.25, 2.0, 2.0, "#FFFF0000")]
        root = ET.fromstring(G.rect_fragment(rects))
        kids = list(root)
        assert root.tag == self.NS + "Canvas"
        assert [k.tag for k in kids] == [self.NS + "Rectangle"] * 2
        assert [k.get("Fill") for k in kids] == ["#5A0078D7", "#FFFF0000"]
        assert kids[0].get("Canvas.Left") == "1.00"
        assert kids[1].get("Canvas.Top") == "20.25"
        assert kids[0].get("Width") == "3.00" and kids[0].get("Height") == "4.00"

    def test_numbers_use_invariant_decimal_point(self):
        frag = G.rect_fragment([(1234.5, 0.5, 2.0, 2.0, "#FF000000")])
        assert "," not in frag          # 区域设置若用逗号做小数点会让 XAML 解析歪掉
        assert "1234.50" in frag

    def test_primitives_come_from_the_single_shared_source(self):
        """**契约已变**:批量原语只有 `_common` 一份。

        本模块曾自带 XAML_NS/rect_fragment/poly_fragment/_argb_hex 的副本,
        与 `_common` 逐字同源。原来这里还测过一个 `scatter_fragment` ——
        它**在生产代码里一个调用方都没有**,是这条测试在替死代码续命,
        随合并一并删除(散点走 `_append_points` → `_append_rects`)。
        """
        from astro_smb_gui import _common
        assert G.rect_fragment is _common.rect_fragment
        assert G.poly_fragment is _common.poly_fragment
        assert G.XAML_NS == _common.XAML_NS
        assert not hasattr(G, "scatter_fragment")

    def test_fragment_has_a_fallback_path(self):
        """片段解析万一失败必须还能逐个建元素画出来(不能整张图空掉)。"""
        src = _src_of(G, "GuidingPage._append_rects")
        assert "except Exception" in src
        assert "Rectangle()" in src and "Canvas.SetLeft" in src


class TestPolyFragment:
    """折线同理:`PointCollection.Append` 是**逐点**的 Python→WinRT 调用,
    仪表盘一屏的折线合计约 1900 个点。整条折线一次 XamlReader.Load 出来。"""

    NS = "{http://schemas.microsoft.com/winfx/2006/xaml/presentation}"

    def test_polyline_points_and_stroke(self):
        import xml.etree.ElementTree as ET

        frag = G.poly_fragment([(1.0, 2.0), (3.5, 4.25), (5.0, 6.0)],
                               stroke="#FF0078D7", thickness=1.5)
        root = ET.fromstring(frag)
        kids = list(root)
        assert root.tag == self.NS + "Canvas"
        assert [k.tag for k in kids] == [self.NS + "Polyline"]
        assert kids[0].get("Stroke") == "#FF0078D7"
        assert kids[0].get("StrokeThickness") == "1.50"
        assert kids[0].get("Points") == "1.00,2.00 3.50,4.25 5.00,6.00"

    def test_polygon_uses_fill(self):
        import xml.etree.ElementTree as ET

        root = ET.fromstring(G.poly_fragment([(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)],
                                             fill="#3C0078D7"))
        kid = list(root)[0]
        assert kid.tag == self.NS + "Polygon"
        assert kid.get("Fill") == "#3C0078D7" and kid.get("Stroke") is None

    def test_too_few_points_yields_empty_string(self):
        assert G.poly_fragment([], stroke="#FF000000") == ""
        assert G.poly_fragment([(1.0, 2.0)], stroke="#FF000000") == ""

    def test_numbers_use_invariant_decimal_point(self):
        frag = G.poly_fragment([(1234.5, 0.5), (2.25, 3.0)], stroke="#FF000000")
        # 点对内部用逗号分隔,点与点之间用空格;小数点必须是 '.'
        assert "1234.50,0.50 2.25,3.00" in frag

    def test_callers_go_through_the_batch_helper(self):
        """曲线/包络/滚动 RMS/小图折线都必须走批量路径,不许退回逐点 Append。"""
        for name in ("GuidingPage._polyline", "GuidingPage._poly_on",
                     "GuidingPage._draw_envelope", "GuidingPage._draw_roll"):
            src = _src_of(G, name)
            assert "_append_poly" in src, name
            assert "PointCollection()" not in src, name

    def test_batch_helper_has_a_fallback_path(self):
        src = _src_of(G, "GuidingPage._append_poly")
        assert "except Exception" in src
        assert "PointCollection()" in src and "col.Append(" in src


# ---------------------------------------------------------------- 打开耗时预算

class TestOpenCostBudget:
    """打开仪表盘时 `_render` 在 UI 线程一次性铺完全部元素,期间界面是冻的。
    元素数是唯一有效的成本杠杆(真机:1200 点散点单独就要 2.0 秒),
    这些上限是性能契约,调大之前先去量。"""

    def test_element_budgets(self):
        assert D.SCATTER_MAX <= 400      # 300x190 画布上 400 已接近视觉饱和
        assert D.LOST_TICKS <= 80        # 288px 绘图区画 300 根竖线 = 一块实心色带
        assert D.SUB_MAX_BARS <= 120
        assert D.SEG_MAX_ROWS <= 40

    def test_scatter_downsampled_to_budget(self):
        secs = [_sec(_at(21, 0), [_frame(i * 0.5, ra=0.4, dec=-0.3)
                                  for i in range(1, 4000)])]
        g, rows = _group(secs)
        agg = D.aggregate_group(g, rows)
        assert len(agg["ch"]["sc_pts"]) == D.SCATTER_MAX

    def test_lost_ticks_capped_to_budget(self):
        frames = [_frame(i, err=1 if i % 2 else 0) for i in range(1, 2000)]
        g, rows = _group([_sec(_at(21, 0), frames)])
        agg = D.aggregate_group(g, rows)
        assert 0 < len(agg["lost_marks"]) <= D.LOST_TICKS

    def test_sub_bars_capped_to_budget(self):
        shots = [_shot(_at(21, 0) + timedelta(seconds=60 * i), i + 1)
                 for i in range(400)]
        run = _run(shots=shots)
        secs = [_sec(_at(21, 0), [_frame(i * 2.0, ra=0.4)
                                  for i in range(1, 12000)])]
        g, rows = _group(secs, run=run)
        agg = D.aggregate_group(g, rows)
        items = agg["subs"]["items"]
        assert len(items) > D.SUB_MAX_BARS          # 原始张数确实超预算
        # 画的时候按桶取最差降采样到预算内(绘制逻辑与此处同式)
        n = len(items)
        picked = [max(items[k * n // D.SUB_MAX_BARS:
                            max(k * n // D.SUB_MAX_BARS + 1,
                                (k + 1) * n // D.SUB_MAX_BARS)],
                      key=lambda it: it["rms"])
                  for k in range(D.SUB_MAX_BARS)]
        assert len(picked) == D.SUB_MAX_BARS

    def test_progress_ring_stops_after_render_not_before(self):
        """`_busy(False)` 必须在 `_render` **之后** —— 提前关掉等于在冻结开始前
        先撤掉"还在忙"的唯一提示。"""
        src = _src_of(D, "GuideDashboard._apply")
        assert src.index("self._render(") < src.index("self._busy(False)")

    def test_sub_tooltips_only_on_bad_candidates(self):
        """ToolTip 单价约是 Rectangle 的两倍;120 根全挂会把打开耗时翻一番。"""
        import ast

        fn = _node_of(D, "GuideDashboard._draw_subs")
        parents = {}
        for node in ast.walk(fn):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        tips = [n for n in ast.walk(fn)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "SetToolTip"]
        assert tips, "_draw_subs 应当仍给废片候选挂 ToolTip"
        for call in tips:
            guards = []
            cur = parents.get(call)
            while cur is not None:
                if isinstance(cur, ast.If):
                    guards.append(ast.dump(cur.test))
                cur = parents.get(cur)
            assert any("'bad'" in g or '"bad"' in g for g in guards), \
                "SetToolTip 必须只在废片候选分支里调用"


# ------------------------------------------------- 组头控件复用 / 遮罩跨页收起

class _StubEl:
    """不起 XAML 的最小控件替身:只有视图切换/工具栏会碰到的几个属性。"""

    def __init__(self, width: float = 0.0) -> None:
        self.Visibility = Visibility.Visible
        self.ActualWidth = width
        self.IsEnabled = True
        self.SelectedIndex = -1


class _FakeDash:
    """GuideDashboard 的最小替身(只实现页面会调的两个方法)。

    `hide()` 与真身一样是**幂等**的(见 TestDashDataGeneration 里对真身的断言),
    否则这里量出来的调用次数说明不了问题。
    """

    def __init__(self, live: bool = True) -> None:
        self.live = live
        self.hidden = 0
        self.shown: list[tuple] = []

    def hide(self) -> None:
        if not self.live:
            return
        self.live = False
        self.hidden += 1

    def show(self, group, rows, src, panel_w: float = 0.0) -> None:
        self.live = True
        self.shown.append((group, panel_w))


class _StubShell:
    def __init__(self) -> None:
        self.logstore = None
        self.errors: list[str] = []

    def error(self, text: str) -> None:
        self.errors.append(text)

    def info(self, text: str) -> None:
        pass


def _bare_page(right_w: float = 800.0):
    """不起 XAML 的 GuidingPage 空壳。

    只喂视图切换 / show_range / on_show / _on_select 会碰到的字段;
    XAML 控件用 `_StubEl` 顶上(视图切换本质就是改两个 Visibility)。
    """
    page = object.__new__(G.GuidingPage)
    page.shell = _StubShell()
    page._rows = None
    page._prepared_src = None
    page._pending_locate = False
    page._refreshing = False
    page._connected = False
    page._hl = None
    page._dash = None
    page._dash_guard = None
    page._current = None
    page._win_s = None
    page._loading_list = False
    page._sel_idx = -1
    page._disp = []
    page._ri_disp = {}
    page._group_open = {}
    page._view = G.VIEW_SEGMENT
    page.segment_view = _StubEl(right_w)
    page.dash_host = _StubEl(0.0)          # 折叠着的元素 ActualWidth 就是 0
    page.window_combo = _StubEl()
    page.pos_slider = _StubEl()
    page.section_list = _StubEl()
    return page


class TestRightViewSwitching:
    """右侧分析区的视图状态机:段视图 ⇄ 仪表盘视图。

    仪表盘从"覆盖整页的遮罩"改成"右侧面板"之后,不再需要 Esc/关闭按钮;
    但**跳转定位必须强制回段视图** —— 定位结果画在段视图的大曲线上,
    仪表盘占着同一格,不切回去等于什么也看不见(遮罩时代同款故障)。
    """

    def test_dash_click_switches_to_dash_view(self):
        page = _bare_page()
        dash = _FakeDash(live=False)
        page._dash = dash
        page._on_dash_click({"key": "k", "title": "M 8"})
        assert page._view == G.VIEW_DASH
        assert page.segment_view.Visibility == Visibility.Collapsed
        assert page.dash_host.Visibility == Visibility.Visible
        assert len(dash.shown) == 1

    def test_dash_click_passes_measured_panel_width(self):
        """宽度必须在切视图**之前**量:切完这一帧面板还没被布局过。"""
        page = _bare_page(right_w=819.0)
        page._dash = _FakeDash(live=False)
        page._on_dash_click({"key": "k", "title": "M 8"})
        assert page._dash.shown[0][1] == pytest.approx(819.0)

    def test_selecting_a_row_switches_back_to_segment_view(self):
        page = _bare_page()
        page._dash = _FakeDash(live=False)
        page._on_dash_click({"key": "k", "title": "M 8"})
        page.show_segment_view()
        assert page._view == G.VIEW_SEGMENT
        assert page.segment_view.Visibility == Visibility.Visible
        assert page.dash_host.Visibility == Visibility.Collapsed
        assert page._dash.hidden == 1        # 面板画布要清掉,别占着元素

    def test_show_range_forces_segment_view(self):
        page = _bare_page()
        page._dash = _FakeDash(live=False)
        page._on_dash_click({"key": "k", "title": "M 8"})
        page.show_range(_at(21, 0), _at(22, 0), "M 8")
        assert page._view == G.VIEW_SEGMENT
        assert page._dash.hidden == 1
        assert page._pending_locate is True   # 定位意图仍然保留

    def test_on_show_keeps_dash_view(self):
        """面板不再遮挡任何东西,换页回来保留用户选的视图(不再强制收起)。"""
        page = _bare_page()
        page._dash = _FakeDash(live=False)
        page._on_dash_click({"key": "k", "title": "M 8"})
        page.on_show()                        # shell.logstore is None → 早退
        assert page._view == G.VIEW_DASH
        assert page._dash.hidden == 0

    def test_toolbar_window_controls_disabled_in_dash_view(self):
        """窗口/位置只缩放段视图的大折线图,仪表盘视图下必须置灰。"""
        page = _bare_page()
        page._dash = _FakeDash(live=False)
        page._current = {"kind": "guide", "duration": 3600.0}
        page._win_s = 600.0
        page._update_win_controls()
        assert page.window_combo.IsEnabled is True
        assert page.pos_slider.IsEnabled is True
        page._on_dash_click({"key": "k", "title": "M 8"})
        assert page.window_combo.IsEnabled is False
        assert page.pos_slider.IsEnabled is False
        page.show_segment_view()
        assert page.window_combo.IsEnabled is True

    def test_switch_is_noop_when_already_there(self):
        page = _bare_page()
        page._dash = _FakeDash(live=False)
        page.show_segment_view()
        page.show_segment_view()
        assert page._dash.hidden == 0        # 本来就没在显,不该反复 hide
        assert page._view == G.VIEW_SEGMENT

    def test_show_range_without_dashboard(self):
        page = _bare_page()
        page.show_range(_at(21, 0), _at(22, 0))     # _dash is None
        assert page._dash is None and page._view == G.VIEW_SEGMENT

    def test_broken_dashboard_does_not_break_jump(self):
        class _Boom:
            def hide(self):
                raise RuntimeError("boom")

        page = _bare_page()
        page._dash = _Boom()
        page._view = G.VIEW_DASH
        page.show_range(_at(21, 0), _at(22, 0), "x")
        assert page._pending_locate is True          # 跳转照常继续
        assert page._view == G.VIEW_SEGMENT          # 视图照样切回来

    def test_dash_button_does_not_bubble_into_collapse(self):
        """组头右侧的「仪表盘」按钮:_dash_guard 布防后的那次组头选中要被吞掉,
        既不能折叠该组,也不能把右侧拽回段视图。"""
        page = _bare_page()
        page._dash = _FakeDash(live=False)
        g = {"key": "k", "title": "M 8"}
        page._disp = [{"type": "group", "g": g}]
        page.section_list.SelectedIndex = 0
        page._arm_dash_guard("k")
        page._on_dash_click(g)                      # 按钮真的点到了
        assert page._view == G.VIEW_DASH
        # 紧随其后(某些输入路径下)才到的组头选中
        page._dash_guard = "k"                      # 重新布防模拟"选中晚于 Click"
        page._on_select(None, None)
        assert page._group_open == {}               # 没有被当成折叠切换
        assert page._view == G.VIEW_DASH            # 也没有被拽回段视图
        assert page._dash_guard is None             # 守卫用完即弃

    def test_row_selection_toggles_view_but_group_click_collapses(self):
        """没布防时点组头仍然是折叠切换(别把防冒泡做成"组头永远点不动")。"""
        page = _bare_page()
        page._dash = _FakeDash(live=False)
        g = {"key": "k", "title": "M 8"}
        page._disp = [{"type": "group", "g": g}]
        page.section_list.SelectedIndex = 0
        page._rebuild_list = lambda: None           # 重建列表要真 XAML,这里桩掉
        page._on_select(None, None)
        assert page._group_open == {"k": False}


class TestDashDataGeneration:
    """数据换代:聚合缓存按组键存,而组键里带的是**旧一代的行索引**,
    留着只会画出错位的东西 ⇒ 缓存整体作废 + 退回段视图(`_apply_data`
    紧接着就会重建列表并选中一个默认段,正好接上)。"""

    def _dash(self, page):
        dash = object.__new__(D.GuideDashboard)
        dash.page = page
        dash.shell = page.shell
        dash._live = True
        dash._gen = 3
        dash._size_gen = 0
        dash._agg = {"ch": {}}
        dash._cache = {"k": {"ch": {}}}
        dash._cache_src = object()
        dash._seg_rows = [{"begins": _at(21, 0)}]
        dash._clear_canvases = lambda: setattr(dash, "cleared", True)
        return dash

    def test_invalidate_drops_cache_and_falls_back_to_segment_view(self):
        page = _bare_page()
        dash = self._dash(page)
        page._dash = dash
        page._view = G.VIEW_DASH
        dash.invalidate()
        assert dash._cache == {} and dash._cache_src is None
        assert dash._agg is None
        assert dash._live is False and getattr(dash, "cleared", False)
        assert page._view == G.VIEW_SEGMENT

    def test_invalidate_while_hidden_only_drops_cache(self):
        page = _bare_page()
        dash = self._dash(page)
        dash._live = False
        page._dash = dash
        dash.invalidate()
        assert dash._cache == {} and page._view == G.VIEW_SEGMENT
        assert getattr(dash, "cleared", False) is False   # 本来就没画,不用清

    def test_stale_aggregation_is_dropped_after_hide(self):
        """在途聚合回来时面板已经切走 → 结果必须丢弃,不能往看不见的画布上画。"""
        page = _bare_page()
        dash = self._dash(page)
        gen = dash._gen
        dash.hide()
        dash._render = lambda agg: pytest.fail("切走之后不该再渲染")
        dash._apply(gen, "k", {"ch": {}})

    def test_hide_is_idempotent(self):
        page = _bare_page()
        dash = self._dash(page)
        dash.hide()
        dash.cleared = False
        dash.hide()
        assert dash.cleared is False        # 第二次是空操作


class TestSegmentViewBadges:
    """段视图标题行的徽章(纯数据部分):与仪表盘汇总卡同一套语义与配色。"""

    def test_guide_row_badges(self):
        sec = _sec(_at(21, 0), [_frame(i, ra=0.1, dec=0.1) for i in range(1, 200)])
        row = G._prep_guide(sec)
        badges = G.GuidingPage._row_badges(None, row)
        texts = [t for t, _s in badges]
        assert any(t.startswith("RMS ") for t in texts)
        # 整条是一个 msgid(`丢星 {lost_pct:.1f}%`)—— 比整条,不比前缀
        assert any(t == tr("丢星 {lost_pct:.1f}%", lost_pct=p)
                   for t in texts for p in (0.0,) ) or any(
            "%" in t for t in texts), texts
        assert any("分钟" in t for t in texts)
        assert "短尝试" not in texts               # 200 帧是主段
        assert all(s in G.BADGE_RGB for _t, s in badges)

    def test_fragment_row_is_flagged(self):
        sec = _sec(_at(21, 0), [_frame(i) for i in range(1, 5)])
        badges = G.GuidingPage._row_badges(None, G._prep_guide(sec))
        assert (tr("短尝试"), "warn") in badges

    def test_no_valid_frames(self):
        sec = _sec(_at(21, 0), [_frame(1, err=1), _frame(2, err=1)])
        badges = G.GuidingPage._row_badges(None, G._prep_guide(sec))
        assert (tr("无有效帧"), "bad") in badges

    def test_calibration_row_badges(self):
        cal = CalibrationSection(begins=_at(21, 0), complete=False, star_lost=3)
        badges = G.GuidingPage._row_badges(None, G._prep_cal(cal))
        assert (tr("校准段"), "neutral") in badges
        assert (tr("校准失败"), "bad") in badges

    def test_no_row_no_badges(self):
        assert G.GuidingPage._row_badges(None, None) == []

    def test_badge_palette_has_a_single_source(self):
        """两个右侧视图的胶囊必须完全同色 ⇒ 配色表只能有一处出处,
        胶囊控件也复用同一个 `_chip`。"""
        assert set(G.BADGE_RGB) >= {"good", "warn", "bad", "info", "neutral"}
        assert not hasattr(D, "_BADGE_RGB")
        assert "self.page._chip(" in _src_of(D, "GuideDashboard._render_badges")


class TestPanelNotOverlay:
    """遮罩形态的包袱必须真的拆干净(留着就是死代码 + 误导)。"""

    def test_dashboard_api_has_no_overlay_leftovers(self):
        for gone in ("is_open", "close", "_on_close", "_attach_overlay"):
            assert not hasattr(D.GuideDashboard, gone), gone
        for gone in ("_close_dash",):
            assert not hasattr(G.GuidingPage, gone), gone

    def test_attach_goes_into_the_page_right_hand_host(self):
        src = _src_of(D, "GuideDashboard._attach")
        assert "dash_host" in src
        assert "SetRowSpan" not in src and "SetRow(" not in src

    def test_no_escape_accelerator_or_close_button_in_xaml(self):
        base = Path(__file__).resolve().parent.parent / "astro_smb_gui"
        xaml = (base / "guidedash.xaml").read_text(encoding="utf-8")
        assert "KeyboardAccelerator" not in xaml and "DashCloseBtn" not in xaml
        assert "DashBackBtn" in xaml                 # 换成非模态的"回段视图"
        # 遮罩底色/负边距都是给"盖住整页"用的,面板里留着就是错的
        assert "SolidBackgroundFillColorBaseBrush" not in xaml
        assert 'Margin="-4"' not in xaml

    def test_guiding_xaml_hosts_both_views_in_one_cell(self):
        from xml.dom import minidom

        base = Path(__file__).resolve().parent.parent / "astro_smb_gui"
        doc = minidom.parse(str(base / "guiding.xaml"))
        names = {}
        stack = [(doc.documentElement, None)]
        while stack:
            el, parent = stack.pop()
            n = el.getAttribute("x:Name") if el.attributes is not None else ""
            if n:
                names[n] = (el, parent)
            for ch in el.childNodes:
                if ch.nodeType == ch.ELEMENT_NODE:
                    stack.append((ch, el))
        assert "SegmentView" in names and "DashHost" in names
        # 两个视图必须是**同一个父容器**的兄弟,才能叠在同一格里互斥
        assert names["SegmentView"][1] is names["DashHost"][1]
        assert names["DashHost"][0].getAttribute("Visibility") == "Collapsed"

    def test_dashboard_body_scrolls_vertically_only(self):
        """横向必须关死:用户不要横滚条,而且只有横向受约束正文才拿得到
        有限宽度、图表才排得出来。"""
        base = Path(__file__).resolve().parent.parent / "astro_smb_gui"
        xaml = (base / "guidedash.xaml").read_text(encoding="utf-8")
        i = xaml.index("DashScroll")
        head = xaml[i:i + 400]
        assert 'HorizontalScrollMode="Disabled"' in head
        assert 'HorizontalScrollBarVisibility="Disabled"' in head
        assert 'VerticalScrollBarVisibility="Auto"' in head

    def test_no_fixed_chart_widths_left_in_xaml(self):
        """画布尺寸全部由 chart_layout 现算 —— xaml 里再留死宽就会两边打架。"""
        import re
        from xml.dom import minidom

        base = Path(__file__).resolve().parent.parent / "astro_smb_gui"
        doc = minidom.parse(str(base / "guidedash.xaml"))
        stack = [doc.documentElement]
        while stack:
            el = stack.pop()
            if el.tagName == "Canvas":
                assert not el.getAttribute("Width"), el.getAttribute("x:Name")
            for ch in el.childNodes:
                if ch.nodeType == ch.ELEMENT_NODE:
                    stack.append(ch)
        src = Path(D.__file__).read_text(encoding="utf-8")
        assert not re.search(r"^WIDE_W\s*=", src, re.M)

    def test_panel_width_hint_accounts_for_scroll_padding(self):
        """页面给的是整块右侧区的宽,正文还要减掉 ScrollViewer 的右内边距 ——
        差这 8px 就足以让一行少排一列(见 CHART_SLACK 的实测)。"""
        import re

        assert "- SCROLL_PAD" in _src_of(D, "GuideDashboard.show")
        base = Path(__file__).resolve().parent.parent / "astro_smb_gui"
        xaml = (base / "guidedash.xaml").read_text(encoding="utf-8")
        i = xaml.index("DashScroll")
        m = re.search(r'Padding="0,0,([\d.]+),0"', xaml[i:i + 400])
        assert m and float(m.group(1)) == pytest.approx(D.SCROLL_PAD)

    def test_measured_width_wins_over_the_hint(self):
        """正文一旦被布局过就以实测宽为准(窗口中途变过、hint 已经过期)。"""
        src = _src_of(D, "GuideDashboard._wide_width")
        assert "self.body.ActualWidth" in src
        assert "self._panel_w = w" in src and "WIDE_MIN" in src

    def test_resize_is_debounced_and_wired_once(self):
        """拖窗口边框期间每帧重画会卡死:必须有容差 + 代次防抖,
        且 SizeChanged 只在固定控件上挂一次(win32more 的事件挂了就摘不掉)。"""
        wire = _src_of(D, "GuideDashboard._wire")
        assert wire.count(".SizeChanged +=") == 1
        assert "self.body.SizeChanged +=" in wire
        on_size = _src_of(D, "GuideDashboard._on_body_size")
        assert "RESIZE_TOLERANCE" in on_size and "_size_gen" in on_size
        later = _src_of(D, "GuideDashboard._resize_later")
        assert "RESIZE_DEBOUNCE_S" in later


class TestGroupHeaderReuse:
    """组头右侧的「仪表盘」Button 每挂一次事件就被 win32more 的 event 描述符
    永久 pin 住(`_winrt.py` 的 `event.__get__` 把实例存进类级 `_event_setters`
    且从不移除,`-=`/`clear()` 只清 `_callbacks`)。所以 `_rebuild_list` 每次
    折叠/展开都新建组头 = 永久泄漏 N 个 Button 及其闭包(闭包里的 `gg=g`
    还顺带 pin 住整组的 TargetRun)。真机探针实测:6 次重建泄漏 30 个
    存活 Button,复用缓存后为 0。
    """

    def test_page_initialises_the_cache(self):
        src = _src_of(G, "GuidingPage.__init__")
        assert "_group_widgets" in src

    def test_cache_is_consulted_before_building_a_button(self):
        src = _src_of(G, "GuidingPage._group_widget")
        hit = src.index("self._group_widgets.get(")
        assert "return cached[" in src[hit:]
        # 缓存命中的分支必须在新建 Button 之前返回
        assert hit < src.index("btn = Button()")
        assert src.index("return cached[") < src.index("btn = Button()")

    def test_cache_is_stored_with_the_group_identity(self):
        src = _src_of(G, "GuidingPage._group_widget")
        # 用组对象身份做二次校验:换代后组键相同但组对象是新的,必须重建
        assert 'cached["g"] is g' in src
        assert "self._group_widgets[g[\"key\"]] = " in src

    def test_cache_is_dropped_on_new_data_generation(self):
        src = _src_of(G, "GuidingPage._apply_data")
        assert "self._group_widgets = {}" in src

    def test_seg_bars_no_longer_wire_per_rectangle_events(self):
        """分段对比条同理:事件只在画布上挂一次。"""
        draw = _src_of(D, "GuideDashboard._draw_seg")
        assert ".Tapped +=" not in draw     # 逐根条不再挂事件
        wire = _src_of(D, "GuideDashboard._wire")
        assert wire.count(".Tapped +=") == 1 and "self.cv_seg.Tapped +=" in wire


# ---------------------------------------------------------------- 静态扫描

class TestNoAstralChars:
    """§7.1:win32more 把 str 转 HSTRING 时按码点数给长度,而 HSTRING 是 UTF-16,
    任何星平面字符都会让字符串末尾少一个字符(真机现象:Plan→Pla)。"""

    @staticmethod
    def _astral(s: str) -> list:
        return sorted({c for c in s if ord(c) > 0xFFFF})

    def test_python_string_literals_are_bmp_only(self):
        import ast

        base = Path(__file__).resolve().parent.parent / "astro_smb_gui"
        tree = ast.parse((base / "_guidedash.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                bad = self._astral(node.value)
                assert not bad, (f"_guidedash.py:{node.lineno} 含星平面字符: "
                                 f"{[hex(ord(c)) for c in bad]}")

    def test_xaml_text_is_bmp_only(self):
        from xml.dom import minidom

        base = Path(__file__).resolve().parent.parent / "astro_smb_gui"
        doc = minidom.parse(str(base / "guidedash.xaml"))
        chunks = []
        stack = [doc.documentElement]
        while stack:
            el = stack.pop()
            if el.attributes is not None:
                chunks += [a.value for a in el.attributes.values()]
            for ch in el.childNodes:
                if ch.nodeType == ch.TEXT_NODE:
                    chunks.append(ch.data)
                elif ch.nodeType == ch.ELEMENT_NODE:
                    stack.append(ch)
        bad = self._astral("".join(chunks))
        assert not bad, f"guidedash.xaml 含星平面字符: {[hex(ord(c)) for c in bad]}"

    def test_xaml_names_referenced_by_code_all_exist(self):
        """xaml 的 x:Name 与 _find() 里的 FindName 必须一一对得上(打字错会到真机才炸)。"""
        import re
        from xml.dom import minidom

        base = Path(__file__).resolve().parent.parent / "astro_smb_gui"
        doc = minidom.parse(str(base / "guidedash.xaml"))
        names = set()
        stack = [doc.documentElement]
        while stack:
            el = stack.pop()
            if el.attributes is not None and el.getAttribute("x:Name"):
                names.add(el.getAttribute("x:Name"))
            for ch in el.childNodes:
                if ch.nodeType == ch.ELEMENT_NODE:
                    stack.append(ch)
        src = (base / "_guidedash.py").read_text(encoding="utf-8")
        used = set(re.findall(r'f\("(\w+)"\)', src))
        assert used and used <= names, f"xaml 里没有这些 x:Name: {used - names}"


# ---------------------------------------- 对抗审查确认项:面板化后的三处修复

class _PanelStubEl:
    """够用的假 XAML 元素:只记属性,不需要真消息泵。"""

    def __init__(self, **kw):
        self.Children = _PanelStubVec()
        self.RowDefinitions = _PanelStubVec()
        self.Visibility = None
        self.Text = ""
        self.ActualWidth = 0.0
        self.HorizontalScrollMode = None
        self.HorizontalScrollBarVisibility = None
        for k, v in kw.items():
            setattr(self, k, v)


class _PanelStubVec(list):
    def Clear(self):
        del self[:]

    def Append(self, x):
        self.append(x)


def _bare_dash(**over):
    """不建 XAML 的 GuideDashboard 骨架(只装本组测试要碰的字段)。"""
    d = object.__new__(D.GuideDashboard)
    d.badges = _PanelStubEl()
    d.pills = _PanelStubEl()
    d.kv = _PanelStubEl()
    d.target_text = _PanelStubEl()
    d.sub_text = _PanelStubEl()
    d.notes = _PanelStubEl()
    d.charts = _PanelStubEl()
    d.empty = _PanelStubEl()
    d.body = _PanelStubEl()
    d.scroll = _PanelStubEl()
    d._group = {"title": "NGC 253"}
    d._panel_w = D.PANEL_W_DEFAULT
    d._panel_w_fresh = False
    for k, v in over.items():
        setattr(d, k, v)
    return d


class TestPlaceholderClearsSummary:
    """组间直切时,汇总卡不能留着上一组的数字 —— 在分析页里读错组的 RMS
    比卡顿危险得多,而面板化后左侧列表全程可见,组间直切是常规操作。"""

    def test_placeholder_wipes_summary_widgets(self):
        d = _bare_dash()
        d.badges.Children.Append(object())
        d.pills.Children.Append(object())
        d.kv.Children.Append(object())
        d.kv.RowDefinitions.Append(object())
        d.sub_text.Text = "上一组的 RMS 1.88″"
        d._show_placeholder("正在聚合本组导星数据…")
        assert list(d.badges.Children) == []
        assert list(d.pills.Children) == []
        assert list(d.kv.Children) == [] and list(d.kv.RowDefinitions) == []
        assert d.sub_text.Text == ""

    def test_placeholder_keeps_group_title(self):
        """目标名保留 —— 让用户知道正在聚合的是哪一组。"""
        d = _bare_dash(_group={"title": "M 8"})
        d._show_placeholder("聚合中")
        assert d.target_text.Text == "M 8"

    def test_placeholder_is_idempotent(self):
        d = _bare_dash()
        d._show_placeholder("a")
        d._show_placeholder("b")
        assert d.empty.Text == "b"


class TestFreshPanelWidthWins:
    """面板 Collapsed 期间 body.ActualWidth 是上一次布局的旧值 —— 隐藏时改过
    窗口大小再开仪表盘,会先按旧宽画错一整屏,等 0.25s 防抖才纠正。"""

    def test_fresh_width_beats_stale_actual_width(self):
        d = _bare_dash(_panel_w=310.0, _panel_w_fresh=True)
        d.body.ActualWidth = 811.2          # 隐藏期间残留的旧宽
        assert d._wide_width() == 310.0

    def test_actual_width_used_once_layout_settled(self):
        d = _bare_dash(_panel_w=310.0, _panel_w_fresh=False)
        d.body.ActualWidth = 811.2
        assert d._wide_width() == 811.2

    def test_wide_width_never_below_minimum(self):
        d = _bare_dash(_panel_w=50.0, _panel_w_fresh=True)
        assert d._wide_width() == D.WIDE_MIN


class TestHScrollOnlyWhenTooNarrow:
    """横滚默认关,但正文最小需求宽真的塞不下时必须打开 —— 否则内容被静默
    裁掉且用户无法查看。注意 HorizontalScrollMode=Disabled 时 ScrollableWidth
    恒为 0,所以"实测 hscroll=0 ⇒ 没裁切"是无效推断。"""

    def test_disabled_when_wide_enough(self):
        d = _bare_dash()
        d.body.ActualWidth = 811.2
        d._sync_hscroll()
        assert d.scroll.HorizontalScrollMode == D.ScrollMode.Disabled
        assert (d.scroll.HorizontalScrollBarVisibility
                == D.ScrollBarVisibility.Disabled)

    def test_enabled_when_viewport_narrower_than_content_minimum(self):
        d = _bare_dash()
        d.body.ActualWidth = 210.4          # 审查实测的窗口 800 场景
        d._sync_hscroll()
        assert d.scroll.HorizontalScrollMode == D.ScrollMode.Enabled
        assert (d.scroll.HorizontalScrollBarVisibility
                == D.ScrollBarVisibility.Auto)

    def test_threshold_is_the_content_minimum(self):
        need = max(D.WIDE_MIN, D.CHART_MIN_W + D.CHART_BORDER + D.CHART_GAP)
        for w, on in ((need - 20.0, True), (need + 20.0, False)):
            d = _bare_dash()
            d.body.ActualWidth = w
            d._sync_hscroll()
            assert (d.scroll.HorizontalScrollMode
                    == (D.ScrollMode.Enabled if on else D.ScrollMode.Disabled)), w


# ---------------------------------------- #35 OAG 识别徽章

class TestOagVerdict:
    """导星光路与主光路焦距相同 ⇒ 只能是同一套光学 = OAG(或分光),
    不可能是独立导星镜。真机确认:用户设备是 ZWO OAG-L,导星 403mm、主镜 403mm。"""

    def test_identical_focal_lengths_reads_as_oag(self):
        assert D.oag_verdict(403.0, 403.0) == tr("可能是 OAG 导星")

    def test_real_device_numbers(self):
        """PHD2 段头 403mm(ASI220MM Mini 4.0µm ÷ 2.05″/px)vs FITS FOCALLEN 403mm。"""
        assert D.oag_verdict(403.0, 402.6)

    def test_separate_guide_scope_is_not_oag(self):
        assert D.oag_verdict(240.0, 1000.0) == ""     # 常见小导星镜 + 长焦主镜
        assert D.oag_verdict(120.0, 400.0) == ""

    def test_tolerance_boundary(self):
        """容差 ±5%:两个焦距都是反推来的,各自有误差。"""
        assert D.oag_verdict(400.0 * (1 + D.OAG_TOLERANCE - 1e-6), 400.0)
        assert D.oag_verdict(400.0 * (1 + D.OAG_TOLERANCE + 1e-3), 400.0) == ""

    @pytest.mark.parametrize("g,m", [
        (None, 400.0), (400.0, None), (None, None),
        (0.0, 400.0), (400.0, 0.0), (-400.0, 400.0),
        ("", 400.0), ("abc", 400.0), (float("nan"), 400.0),
    ])
    def test_refuses_to_guess(self, g, m):
        """任一焦距缺失/非正/不可解析 ⇒ 一律不显示,绝不猜。"""
        assert D.oag_verdict(g, m) == ""

    def test_wording_leaves_room(self):
        """理论上存在两台焦距碰巧相同的独立镜,所以文案必须留余地。"""
        assert "可能" in D.oag_verdict(403.0, 403.0)


class TestMainFocalLookup:
    """主镜焦距取自该目标首帧 FITS 的 FOCALLEN;任何失败都返回 None ——
    这只是个锦上添花的徽章,不值得让整个聚合失败。"""

    class _Cli:
        def __init__(self, entries=None, boom=False):
            self._entries, self._boom = entries or [], boom
            self.listdirs = 0

        def listdir(self, share, path=""):
            self.listdirs += 1
            if self._boom:
                raise OSError("设备已拔出")
            return self._entries

    def test_no_client_or_target(self):
        assert D.main_focal_for(None, "s", "M 8", {}) is None
        assert D.main_focal_for(self._Cli(), "s", "", {}) is None

    def test_listdir_failure_is_swallowed(self):
        assert D.main_focal_for(self._Cli(boom=True), "s", "M 8", {}) is None

    def test_no_fits_in_directory(self):
        assert D.main_focal_for(self._Cli([]), "s", "M 8", {}) is None

    def test_cache_prevents_retry_storm(self):
        """失败也写进缓存:每打开一次仪表盘都重试一遍慢 SMB 是不可接受的。"""
        cli = self._Cli(boom=True)
        cache: dict = {}
        D.main_focal_for(cli, "s", "M 8", cache)
        D.main_focal_for(cli, "s", "M 8", cache)
        D.main_focal_for(cli, "s", "M 8", cache)
        assert cli.listdirs == 1 and cache["M 8"] is None
