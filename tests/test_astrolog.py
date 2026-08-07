"""autorunlog / phd2log / astro / naming 四个天文模块的离线单测。

合成样例覆盖格式陷阱; .tmp/ 下的真机日志存在时追加对账测试。
"""
from __future__ import annotations

import math
import os
from datetime import datetime

import pytest

from astro_smb import astro, naming
from astro_smb.autorunlog import (
    FILTER_UNKNOWN, aggregate_nights, night_key, parse_autorun_log,
    parse_exposure_seconds,
)
from astro_smb.phd2log import (
    GuideFrame, compute_rms, parse_phd2_log, rms_for_interval, section_rms,
)

TMP = os.path.join(os.path.dirname(__file__), "..", ".tmp")
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------- autorunlog

MINI_LOG = """\
Log enabled at 2026/07/23 20:05:43
2026/07/23 20:05:43 [Autorun|Begin] M 8 Start
2026/07/23 20:05:43 Target RA:10h16m13s DEC:+89°0'0"
2026/07/23 20:05:43 Shooting 2 bias frames, exposure 1.0ms Bin1
2026/07/23 20:05:43 Exposure 1.0ms image 1#
2026/07/23 20:05:44 Exposure 1.0ms image 2#
2026/07/23 20:05:45 Shooting 1 dark frames, exposure 300.0s Bin1
2026/07/23 20:05:45 Exposure 300.0s image 3#
2026/07/23 20:10:46 [Autorun|End] Finish Autorun
Log disabled at 2026/07/23 20:11:00
Log enabled at 2026/07/23 21:15:36
2026/07/23 21:15:36 Plan 1 Start
2026/07/23 21:15:36 [Autorun|Begin] NGC 6334 Start
2026/07/23 21:15:47 [AutoCenter|Begin] Auto-Center 1#
2026/07/23 21:15:47 Mount slews to target position: RA:17h22m35s DEC:-36°7'40"
2026/07/23 21:16:53 Exposure 2.0s
2026/07/23 21:16:56 Plate Solve
2026/07/23 21:16:57 Solve succeeded: RA:17h21m33s DEC:-36°7'11" Angle = 274.806, Star number = 31
2026/07/23 21:16:58 [AutoCenter|End] Too far from center, distance = 9%(0.20988°)
2026/07/23 21:16:59 Stop Autorun Manually
2026/07/23 21:17:00 [Autorun|End] Pause Autorun
2026/07/23 21:17:00 Pause Plan 1
Log disabled at 2026/07/23 21:17:01
Log enabled at 2026/07/23 22:00:00
2026/07/23 22:00:00 Plan 1 Start
2026/07/23 22:00:00 [Autorun|Begin] NGC 6334 Start
2026/07/23 22:00:05 Shooting 3 light frames, exposure 180.0s Bin1
2026/07/23 22:00:06 [AutoFocus|Begin] Run AF before Autorun start, exposure 2.0s Bin1, temperature 37.2℃
2026/07/23 22:05:00 Auto focus succeeded, the focused position is 13140
2026/07/23 22:05:00 [AutoFocus|End] Auto focus succeeded
2026/07/23 22:05:10 [Guide] Start Guiding
2026/07/23 22:05:20 Exposure 180.0s image 1#
2026/07/23 22:08:21 Exposure 180.0s image 2#
2026/07/23 22:11:22 [Autorun|End] Finish Autorun
2026/07/23 22:11:22 Plan 1 Finish
2026/07/23 22:11:23 Shutdown ASIAIR
Log disabled at 2026/07/23 22:11:24
"""


class TestAutorunParse:
    def setup_method(self):
        self.log = parse_autorun_log(MINI_LOG, source="mini.txt")

    def test_sessions_and_blocks(self):
        assert len(self.log.sessions) == 3
        s1, s2, s3 = self.log.sessions
        assert s1.plan_no is None and len(s1.blocks) == 1
        assert s2.plan_no == 1 and s2.plan_end == "Pause"
        assert s3.plan_no == 1 and s3.plan_end == "Finish" and s3.shutdown

    def test_cross_group_numbering(self):
        b = self.log.sessions[0].blocks[0]
        assert b.target == "M 8"
        assert [g.frame_type for g in b.groups] == ["bias", "dark"]
        assert b.groups[0].frames[0].image_no == 1
        assert b.groups[1].frames[0].image_no == 3  # 跨组连续编号
        assert b.total_frames == 3
        assert b.end_mode == "Finish"

    def test_bare_exposure_not_counted(self):
        b = self.log.sessions[1].blocks[0]
        assert b.total_frames == 0          # 'Exposure 2.0s' 不算实拍帧
        assert b.manual_stop and b.end_mode == "Pause"
        ac = b.autocenter[0]
        assert ac.solve_stars == 31 and ac.solve_angle == pytest.approx(274.806)
        assert "Too far" in (b.autocenter_final or "")

    def test_autofocus_and_frames(self):
        b = self.log.sessions[2].blocks[0]
        af = b.autofocus[0]
        assert af.success and af.focused_position == 13140
        assert af.temperature == pytest.approx(37.2)
        frames = b.all_frames()
        assert len(frames) == 2
        assert frames[0].exposure_s == pytest.approx(180.0)
        assert frames[0].end_time == datetime(2026, 7, 23, 22, 8, 20)

    def test_aggregate_merges_plan_runs(self):
        nights = aggregate_nights([self.log])
        assert len(nights) == 1
        n = nights[0]
        assert n.date == "2026-07-23"
        # M 8(无 plan) 与 NGC 6334(plan 1, 两个块合并) → 2 个 TargetRun
        assert len(n.runs) == 2
        m8, ngc = n.runs
        assert m8.target == "M 8" and m8.plan_no is None
        assert ngc.target == "NGC 6334" and ngc.plan_no == 1
        assert ngc.attempts == 2 and ngc.finished
        assert ngc.total_frames == 2
        assert ngc.type_stats()["light"] == (3, 2)  # 计划3 实拍2
        span = ngc.frame_span()
        assert span is not None
        assert span[0] == datetime(2026, 7, 23, 22, 5, 20)

    def test_night_key_noon_boundary(self):
        assert night_key(datetime(2026, 7, 24, 2, 30)) == "2026-07-23"
        assert night_key(datetime(2026, 7, 24, 13, 0)) == "2026-07-24"

    def test_exposure_seconds(self):
        assert parse_exposure_seconds("180.0s") == pytest.approx(180.0)
        assert parse_exposure_seconds("1.0ms") == pytest.approx(0.001)
        assert parse_exposure_seconds("auto") is None
        assert parse_exposure_seconds(None) is None


# ---------------------------------------------------------------- phd2log

MINI_PHD2 = """\
PHD2 version, Log version 2.5. Log enabled at 2026-07-23 21:24:45

Calibration Begins at 2026-07-23 21:27:37
Camera = ZWO ASI220MM Mini
Exposure = 500 ms
Pixel scale = 2.06 arc-sec/px, Binning = 1, Focal length = 400 mm
Direction,Step,dx,dy,x,y,Dist
West,1,-1.028,10.889,363.797,883.468,10.937
West,2,-2.100,21.500,362.700,894.100,21.602
West calibration complete. Angle = 95.3 deg, Rate = 6.407 px/sec, Parity = N/A
North calibration complete. Angle = 9.7 deg, Rate = 7.602 px/sec, Parity = Even
Calibration complete, mount = OnStep Electronics.

Guiding Begins at 2026-07-23 22:10:28
Pixel scale = 2.00 arc-sec/px, Binning = 1, Focal length = 400 mm
Exposure = 1500 ms
Camera = ZWO ASI220MM Mini, gain = 68, full size = 1920 x 1080
Mount = OnStep Electronics,  connected, guiding enabled
Dec = -24.4 deg, Hour angle = -0.50 hr, Pier side = West, Rotator pos = Unknown
Frame,Time,mount,dx,dy,RARawDistance,DECRawDistance,RAGuideDistance,DECGuideDistance,RADuration,RADirection,DECDuration,DECDirection,XStep,YStep,StarMass,SNR,ErrorCode
1,1.500,"Mount",0.500,0.400,0.300,0.400,0.000,0.000,0,,0,,,,857,20.53,0
2,3.000,"Mount",-0.400,0.300,-0.400,0.300,0.000,0.000,120,W,0,,,,860,21.00,0
3,4.500,"Mount",0.000,0.000,0.000,0.000,0.000,0.000,0,,0,,,,0,0.00,7
INFO: SETTLING STATE CHANGE, Settling complete
Guiding Ends at 2026-07-23 22:11:00

Guiding Ends at 2026-07-23 22:20:00

Log closed at 2026-07-24 02:46:50
"""


class TestPhd2Parse:
    def setup_method(self):
        self.log = parse_phd2_log(MINI_PHD2, source="mini_phd2.txt")

    def test_structure(self):
        assert self.log.enabled_at == datetime(2026, 7, 23, 21, 24, 45)
        assert self.log.closed_at == datetime(2026, 7, 24, 2, 46, 50)
        assert len(self.log.calibrations) == 1
        assert len(self.log.guide_sections) == 1   # 孤立 Ends 不产生新段

    def test_calibration(self):
        cal = self.log.calibrations[0]
        assert cal.complete and cal.mount == "OnStep Electronics"
        assert len(cal.steps) == 2
        assert cal.west_angle == pytest.approx(95.3)
        assert cal.north_rate == pytest.approx(7.602)

    def test_guide_section_meta(self):
        gs = self.log.guide_sections[0]
        assert gs.pixel_scale == pytest.approx(2.00)
        assert gs.exposure_ms == 1500
        assert gs.dec_deg == pytest.approx(-24.4)
        assert gs.hour_angle_hr == pytest.approx(-0.50)
        assert gs.pier_side == "West"
        assert gs.ends == datetime(2026, 7, 23, 22, 11, 0)
        assert len(gs.frames) == 3
        assert gs.frames[1].ra_dur == 120 and gs.frames[1].ra_dir == "W"
        assert gs.frames[2].lost                 # ErrorCode=7
        assert [s.kind for s in gs.settles] == ["complete"]

    def test_rms_excludes_lost(self):
        gs = self.log.guide_sections[0]
        st = section_rms(gs)
        assert st is not None
        assert st.n_frames == 2 and st.n_lost == 1
        # RMS_RA = sqrt((0.3^2+0.4^2)/2)*2.0
        assert st.rms_ra == pytest.approx(
            math.sqrt((0.09 + 0.16) / 2) * 2.0, rel=1e-6)
        assert st.rms_total == pytest.approx(
            math.hypot(st.rms_ra, st.rms_dec), rel=1e-9)
        assert st.in_arcsec

    def test_interval_intersection(self):
        # 只取第 2 帧(绝对 22:10:31)附近
        st = rms_for_interval(
            [self.log],
            datetime(2026, 7, 23, 22, 10, 30, 500000),
            datetime(2026, 7, 23, 22, 10, 32))
        assert st is not None and st.n_frames == 1
        # 完全在段外 → None
        assert rms_for_interval(
            [self.log],
            datetime(2026, 7, 23, 23, 0, 0),
            datetime(2026, 7, 23, 23, 10, 0)) is None


# -------------------------------------------------- 审查修复的回归测试

RESUME_LOG = """\
Log enabled at 2026/07/23 21:00:00
2026/07/23 21:00:00 Plan 1 Start
2026/07/23 21:00:00 [Autorun|Begin] X 1 Start
2026/07/23 21:00:05 Shooting 10 light frames, exposure 180.0s Bin1
2026/07/23 21:00:10 Exposure 180.0s image 1#
2026/07/23 21:03:15 Stop Autorun Manually
2026/07/23 21:03:16 [Autorun|End] Pause Autorun
2026/07/23 21:03:16 Pause Plan 1
Log disabled at 2026/07/23 21:03:17
Log enabled at 2026/07/23 21:10:00
2026/07/23 21:10:00 Plan 1 Start
2026/07/23 21:10:00 [Autorun|Begin] X 1 Start
2026/07/23 21:10:05 Shooting 10 light frames, exposure 180.0s Bin1
2026/07/23 21:10:10 Exposure 180.0s image 1#
2026/07/23 21:13:11 Exposure 180.0s image 2#
2026/07/23 21:16:12 [Autorun|End] Finish Autorun
2026/07/23 21:16:12 Plan 1 Finish
Log disabled at 2026/07/23 21:16:13
"""


class TestReviewFixes:
    def test_planned_not_double_counted_across_resumed_blocks(self):
        """Pause/恢复的块会重新宣告同一份 Shooting 计划,归并后不得累加。"""
        log = parse_autorun_log(RESUME_LOG, source="resume.txt")
        night = aggregate_nights([log])[0]
        run = night.runs[0]
        assert run.attempts == 2
        assert run.type_stats()["light"] == (10, 3)     # 计划 10, 实拍 1+2

    def test_headless_session_sorted_by_block_time(self):
        """缺 'Log enabled' 头的会话按首块时间排序,归并后区间不倒挂。"""
        early = parse_autorun_log(
            "Log enabled at 2026/07/23 21:00:00\n"
            "2026/07/23 21:00:00 [Autorun|Begin] X 1 Start\n"
            "2026/07/23 21:00:10 Exposure 180.0s image 1#\n"
            "2026/07/23 21:03:11 [Autorun|End] Finish Autorun\n"
            "Log disabled at 2026/07/23 21:03:12\n", source="a.txt")
        headless = parse_autorun_log(     # 文件头损坏:无 Log enabled 行
            "2026/07/23 23:00:00 [Autorun|Begin] X 1 Start\n"
            "2026/07/23 23:00:10 Exposure 180.0s image 1#\n"
            "2026/07/23 23:03:11 [Autorun|End] Finish Autorun\n", source="b.txt")
        assert headless.sessions[0].enabled_at is None
        night = aggregate_nights([headless, early])[0]
        run = night.runs[0]
        assert run.begin_time < run.end_time            # 不倒挂
        t0, t1 = run.frame_span()
        assert t0 < t1

    def test_settle_after_guiding_ends_backfilled(self):
        """真机顺序:'Guiding Ends' 之后的 settle failed 要回填到刚结束的段。"""
        text = (
            "PHD2 version, Log version 2.5. Log enabled at 2026-07-23 21:00:00\n"
            "Guiding Begins at 2026-07-23 21:00:10\n"
            "Pixel scale = 2.00 arc-sec/px, Binning = 1, Focal length = 400 mm\n"
            "Frame,Time,mount,dx,dy,RARawDistance,DECRawDistance,RAGuideDistance,"
            "DECGuideDistance,RADuration,RADirection,DECDuration,DECDirection,"
            "XStep,YStep,StarMass,SNR,ErrorCode\n"
            "INFO: SETTLING STATE CHANGE, Settling started\n"
            '1,1.000,"Mount",0.1,0.1,0.1,0.1,0,0,0,,0,,,,100,10.0,0\n'
            "Guiding Ends at 2026-07-23 21:00:20\n"
            "INFO: SETTLING STATE CHANGE, Settling failed\n"
            "Calibration Begins at 2026-07-23 21:01:00\n"
            "INFO: SETTLING STATE CHANGE, Settling failed\n")
        log = parse_phd2_log(text)
        sec = log.guide_sections[0]
        kinds = [s.kind for s in sec.settles]
        assert kinds == ["started", "failed"]           # Ends 后的 failed 已回填
        assert sec.settles[-1].time == sec.ends
        # Calibration Begins 之后的 settle 不再回填(不属于任何导星段)
        assert len(sec.settles) == 2

    def test_frame_filter_attribution(self):
        """帧滤镜由最近一次 Filter change 推得,首次 change 前为 None。"""
        text = (
            "Log enabled at 2026/07/23 19:15:09\n"
            "2026/07/23 19:15:09 [Autorun|Begin] GAIA X Start\n"
            "2026/07/23 19:15:15 Exposure 1.7s image 1#\n"
            "2026/07/23 19:15:17 Filter change, 1 change to 4C\n"
            "2026/07/23 19:15:30 Exposure 5.8s image 2#\n"
            "2026/07/23 19:15:37 Filter change, 4C change to Dul\n"
            "2026/07/23 19:15:58 Exposure 15.0s image 3#\n"
            "2026/07/23 19:16:15 [Autorun|End] Finish Autorun\n"
            "Log disabled at 2026/07/23 19:16:16\n")
        log = parse_autorun_log(text)
        frames = log.sessions[0].blocks[0].all_frames()
        assert [f.filter for f in frames] == [None, "4C", "Dul"]
        night = aggregate_nights([log])[0]
        integ = night.runs[0].integration_by_filter()
        assert integ[FILTER_UNKNOWN] == pytest.approx(1.7)
        assert integ["4C"] == pytest.approx(5.8)
        assert integ["Dul"] == pytest.approx(15.0)

    def test_rms_mixed_scale_excludes_unscaled(self):
        """混合有/无 pixel_scale 时剔除无 scale 帧,不得把像素当角秒混算。"""
        def frame(ra_raw):
            return GuideFrame(time_s=1.0, dx=0, dy=0, ra_raw=ra_raw, dec_raw=0,
                              ra_guide=0, dec_guide=0, ra_dur=0, ra_dir="",
                              dec_dur=0, dec_dir="", star_mass=100, snr=10, err=0)
        st = compute_rms([(frame(1.0), 2.0), (frame(1.0), None)])
        assert st is not None
        assert st.n_frames == 1                         # 无 scale 帧被剔除
        assert st.rms_ra == pytest.approx(2.0)
        assert st.in_arcsec
        # 全部无 scale 仍按像素输出
        st2 = compute_rms([(frame(1.0), None)])
        assert st2 is not None and not st2.in_arcsec
        assert st2.rms_ra == pytest.approx(1.0)


# ---------------------------------------------------------------- astro

class TestAstro:
    def test_parse_ra_dec(self):
        assert astro.ra_str_to_deg("17h22m35s") == pytest.approx(
            (17 + 22 / 60 + 35 / 3600) * 15, rel=1e-9)
        assert astro.dec_str_to_deg("-36°7'40\"") == pytest.approx(
            -(36 + 7 / 60 + 40 / 3600), rel=1e-9)
        assert astro.dec_str_to_deg("+89°0'0\"") == pytest.approx(89.0)
        assert astro.ra_str_to_deg("bogus") is None
        assert astro.dec_str_to_deg(None) is None

    def test_format_roundtrip(self):
        ra = astro.ra_str_to_deg("18h19m35s")
        assert astro.format_ra(ra) == "18h19m35s"
        dec = astro.dec_str_to_deg("-12°12'16\"")
        assert astro.format_dec(dec) == "-12°12'16\""

    def test_altaz_zenith(self):
        # 恒星时=RA 时(HA=0), dec=纬度 → 天顶
        lat, lon = 31.0, 118.0
        ts = astro.unix_from_local(datetime(2026, 7, 23, 22, 0, 0))
        ra = astro.lst_deg(ts, lon)
        alt, _az = astro.altaz(ra, lat, lat, lon, ts)
        assert alt == pytest.approx(90.0, abs=1e-6)

    def test_altaz_pole(self):
        # 北天极高度 = 纬度, 方位 = 正北
        lat, lon = 31.0, 118.0
        ts = astro.unix_from_local(datetime(2026, 7, 23, 22, 0, 0))
        alt, az = astro.altaz(0.0, 90.0, lat, lon, ts)
        assert alt == pytest.approx(lat, abs=1e-6)
        assert min(az, 360 - az) == pytest.approx(0.0, abs=1e-6)

    def test_estimate_longitude_roundtrip(self):
        lat, lon = 31.0, 118.0
        when = datetime(2026, 7, 23, 23, 44, 44)
        ts = astro.unix_from_local(when)
        ra = astro.ra_str_to_deg("18h19m35s")
        ha_hours = ((astro.lst_deg(ts, lon) - ra) % 360.0) / 15.0
        if ha_hours > 12:
            ha_hours -= 24
        est = astro.estimate_longitude(ra, ha_hours, when)
        assert est == pytest.approx(lon, abs=1e-6)

    def test_hours_visible(self):
        assert astro.hours_visible(89.0, 31.0) == 24.0    # 恒显
        assert astro.hours_visible(-80.0, 31.0) == 0.0    # 恒隐
        assert astro.hours_visible(0.0, 0.0) == pytest.approx(12.0, abs=0.01)

    def test_altaz_radec_roundtrip(self):
        lat, lon = 30.0, 120.0
        ts = astro.unix_from_local(datetime(2026, 7, 23, 22, 0, 0))
        for ra, dec in ((10.0, -30.0), (180.0, 45.0), (271.3, -24.4),
                        (359.0, 0.0), (100.0, 88.0)):
            alt, az = astro.altaz(ra, dec, lat, lon, ts)
            ra2, dec2 = astro.radec_from_altaz(alt, az, lat, lon, ts)
            assert ra2 == pytest.approx(ra, abs=1e-6)
            assert dec2 == pytest.approx(dec, abs=1e-6)

    def test_galactic_known_values(self):
        # 人马座 A*(银心): l≈359.944, b≈-0.046
        l, b = astro.galactic_from_radec(266.41683, -29.00781)
        assert min(l, 360 - l) == pytest.approx(0.056, abs=0.05)
        assert b == pytest.approx(-0.046, abs=0.05)
        # 北银极方向
        _, b2 = astro.galactic_from_radec(192.85948, 27.12825)
        assert b2 == pytest.approx(90.0, abs=1e-6)
        # 互逆
        ra, dec = astro.radec_from_galactic(l, b)
        assert ra == pytest.approx(266.41683, abs=1e-6)
        assert dec == pytest.approx(-29.00781, abs=1e-6)


class TestSkymapReproject:
    """合成底图全链路校验:已知银道坐标的亮块经重投影后落在预期地平位置。"""

    def _make_synthetic(self, tmp_path, marks):
        """720×360 黑图,按底图约定(l 向左增,银心居中)画白色亮块。"""
        import numpy as np
        from PIL import Image
        w, h = 720, 360
        img = np.zeros((h, w, 3), dtype=np.uint8)
        for l, b in marks:
            l_signed = ((l + 180.0) % 360.0) - 180.0
            x = int(w / 2.0 - l_signed * w / 360.0) % w
            y = min(h - 1, max(0, int(h / 2.0 - b * h / 180.0)))
            img[max(0, y - 2):y + 3, max(0, x - 2):x + 3] = 255
        p = tmp_path / "synthetic.jpg"
        Image.fromarray(img).save(p, quality=95)
        return p

    def test_zenith_and_east_marks(self, tmp_path, monkeypatch):
        import numpy as np
        from PIL import Image
        from astro_smb_gui import skymap

        monkeypatch.setattr(skymap, "skymap_dir", lambda: tmp_path)
        lat, lon = 30.0, 120.0
        ts = astro.unix_from_local(datetime(2026, 7, 23, 22, 0, 0))
        # 亮块1: 当前天顶;亮块2: 正东 45°;亮块3: **正北 45°**
        # ——正北探针钉住南北方向:az→180°-az 的镜像回归在纯东西向采样点上
        # 是不动点,只有南北不对称的标记才能暴露(审查实证)
        marks = []
        for alt, az in ((90.0, 0.0), (45.0, 90.0), (45.0, 0.0)):
            ra, dec = astro.radec_from_altaz(alt, az, lat, lon, ts)
            marks.append(astro.galactic_from_radec(ra, dec))
        src = self._make_synthetic(tmp_path, marks)

        size = 400
        out = skymap.render_altaz(lat, lon, ts, size=size,
                                  src_path=src, dim=1.0)
        arr = np.asarray(Image.open(out).convert("L")).astype(float)
        c = (size - 1) / 2.0
        r_px = size / 2.0

        def brightest_near(x, y, win=12):
            x0, y0 = int(x), int(y)
            patch = arr[max(0, y0 - win):y0 + win, max(0, x0 - win):x0 + win]
            return patch.max()

        # 天顶亮块 → 圆盘中心;正东 45° → 中心左侧半半径处(东=左)
        assert brightest_near(c, c) > 180
        assert brightest_near(c - r_px * 0.5, c) > 180
        # 正北 45° → 中心上方半半径处(北=上);南侧对应位置必须是黑的
        assert brightest_near(c, c - r_px * 0.5) > 180
        assert brightest_near(c, c + r_px * 0.5) < 40
        # 对照:标记之外的位置应是黑的(西侧同半径)
        assert brightest_near(c + r_px * 0.5, c) < 40
        # 盘外透明
        rgba = np.asarray(Image.open(out))
        assert rgba[2, 2, 3] == 0 and rgba[size // 2, 4, 3] == 255


# ---------------------------------------------------------------- naming

class TestNaming:
    def test_light_full(self):
        n = naming.parse_image_name(
            "Light_M 8_180.0s_Bin1_4C_20260723-221336_2deg_0001.fit")
        assert n is not None
        assert n.kind == "Light" and n.target == "M 8"
        assert n.exposure_s == pytest.approx(180.0)
        assert n.binning == 1 and n.filter == "4C"
        assert n.time == datetime(2026, 7, 23, 22, 13, 36)
        assert n.angle_deg == 2 and n.seq == 1
        assert not n.thumb and n.ext == "fit"

    def test_thumb(self):
        n = naming.parse_image_name(
            "Light_IC 4603_300.0s_Bin1_4C_20260725-202016_276deg_0001_thn.jpg")
        assert n is not None and n.thumb and n.ext == "jpg"
        assert n.target == "IC 4603" and n.seq == 1

    def test_calibration_no_target(self):
        n = naming.parse_image_name(
            "Bias_1.0ms_Bin1_4C_20260723-200544_91deg_0001.fit")
        assert n is not None
        assert n.kind == "Bias" and n.target is None
        assert n.exposure_s == pytest.approx(0.001)
        assert n.angle_deg == 91

    def test_flat_no_deg(self):
        n = naming.parse_image_name(
            "Flat_1.7s_Bin1_1_20260723-191517_0001.fit")
        assert n is not None
        assert n.filter == "1" and n.angle_deg is None and n.seq == 1

    def test_preview_no_filter_no_seq(self):
        n = naming.parse_image_name("Preview_1.0ms_Bin1_20260721-171405.fit")
        assert n is not None
        assert n.kind == "Preview" and n.filter is None
        assert n.seq is None and n.angle_deg is None

    def test_preview_with_target(self):
        n = naming.parse_image_name(
            "Preview_M 24_2.0s_Bin2_4C_20260723-210051_91deg.fit")
        assert n is not None
        assert n.target == "M 24" and n.binning == 2 and n.seq is None

    def test_non_image(self):
        assert naming.parse_image_name("Autorun_Log_2026-07-23.txt") is None
        assert naming.parse_image_name("random.fit") is None


# ---------------------------------------------------------------- 真机样例对账

@pytest.mark.skipif(
    not os.path.isfile(os.path.join(TMP, "Autorun_Log_2026-07-23_212227.txt")),
    reason="真机样例日志不存在")
class TestRealAutorunLogs:
    def test_full_night_plan(self):
        with open(os.path.join(TMP, "Autorun_Log_2026-07-23_212227.txt"),
                  encoding="utf-8-sig") as fh:
            log = parse_autorun_log(fh.read(), source="212227")
        assert len(log.sessions) == 7
        last = log.sessions[-1]
        assert last.plan_no == 1 and last.plan_end == "Finish" and last.shutdown
        m8, ngc = last.blocks
        assert m8.target == "M 8" and m8.total_frames == 30
        assert ngc.target == "NGC 6604" and ngc.total_frames == 60
        assert ngc.autofocus[0].focused_position == 13127
        # 全文件无未识别行
        assert all(not s.unparsed_lines for s in log.sessions)

    def test_aggregate_real(self):
        logs = []
        for name in ("Autorun_Log_2026-07-23_191509.txt",
                     "Autorun_Log_2026-07-23_212227.txt",
                     "Autorun_Log_2026-07-24_084214.txt"):
            with open(os.path.join(TMP, name), encoding="utf-8-sig") as fh:
                logs.append(parse_autorun_log(fh.read(), source=name))
        nights = aggregate_nights(logs)
        # 07-24 08:42 的晨间平场按正午分界归入 07-23 观测夜
        assert [n.date for n in nights] == ["2026-07-23"]
        n0 = nights[0]
        theta = next(r for r in n0.runs if r.target == "Theta Ceti")
        assert theta.type_stats()["flat"] == (20, 20)
        # NGC 6334 的 8 个分裂块归并为一个 run
        ngc6334 = next(r for r in n0.runs if r.target == "NGC 6334")
        assert ngc6334.attempts == 8 and not ngc6334.finished
        assert ngc6334.plan_no == 1
        ngc6604 = next(r for r in n0.runs if r.target == "NGC 6604")
        assert ngc6604.total_frames == 60 and ngc6604.finished


@pytest.mark.skipif(
    not os.path.isfile(os.path.join(TMP, "PHD2_GuideLog_2026-07-23_212445.txt")),
    reason="真机 PHD2 日志不存在")
class TestRealPhd2Logs:
    def test_big_log(self):
        with open(os.path.join(TMP, "PHD2_GuideLog_2026-07-23_212445.txt"),
                  encoding="utf-8") as fh:
            log = parse_phd2_log(fh.read(), source="212445")
        assert len(log.guide_sections) == 11
        assert len(log.calibrations) == 4
        assert sum(1 for c in log.calibrations if not c.complete) == 1
        assert sum(len(s.frames) for s in log.guide_sections) == 11027
        # 最长段 RMS 与探索阶段实测一致(RA≈0.57" DEC≈0.40" Total≈0.69")
        main = max(log.guide_sections, key=lambda s: len(s.frames))
        assert len(main.frames) == 7246
        st = section_rms(main)
        assert st is not None
        assert st.rms_ra == pytest.approx(0.57, abs=0.03)
        assert st.rms_dec == pytest.approx(0.40, abs=0.03)
        assert st.rms_total == pytest.approx(0.69, abs=0.04)

    def test_truncated_log(self):
        with open(os.path.join(TMP, "PHD2_GuideLog_2026-07-23_194934.txt"),
                  encoding="utf-8") as fh:
            log = parse_phd2_log(fh.read(), source="194934")
        assert len(log.guide_sections) == 7
        assert log.closed_at is None            # 文件截断
        last = log.guide_sections[-1]
        assert last.ends is None and len(last.frames) == 548
        assert last.end_time_effective > last.begins






class TestGuidingOverviewUsesTheRightRms:
    """整体 RMS 必须**按帧数平方加权**,不是各段 RMS 的简单平均。

    真机上 123 段里有一段 22.96″ 的短尝试(几帧就失败了)。简单平均把整体拉到
    1.89″,而加权口径是 0.92″ —— 那是"导星很差"和"导星正常"的区别,是判读错误
    而不是显示误差。老 UI 用的一直是加权口径。
    """

    def _sections(self):
        """两段:一段长而稳,一段极短且极差。"""
        class _R:
            def __init__(self, total, n, arcsec=True, lost=0):
                self.rms_total, self.n_frames = total, n
                self.in_arcsec, self.n_lost = arcsec, lost

        return [_R(0.80, 8000), _R(23.0, 4)]

    def test_a_short_awful_section_does_not_dominate(self):
        from astro_smb_app.views import guiding as gv

        merged, unit, n, _lost = gv._merge_rms(self._sections())
        plain = sum(r.rms_total for r in self._sections()) / 2
        assert unit == "″" and n == 8004
        assert merged < 1.0, f"加权口径应接近 0.8,得到 {merged}"
        assert plain > 10.0, "简单平均确实会被短段带偏 —— 这正是不能用它的原因"














class TestTheProjectionInvariants:
    """天球投影的三条不变量:**北上、东左、r = R·(90-alt)/90**。

    变异测试把 `cx - r·sin(az)` 改成 `cx + r·sin(az)`(东西翻转),
    **整个测试套没有一条发现** —— 而那会让每个目标都画在天空的反侧。
    docs/DEVELOPMENT.md 写着"改投影必须三处同步",却没有一条测试钉住它是哪一侧。
    """

    def test_north_is_up(self):
        from astro_smb_app.views.skychart import radar_xy

        x, y = radar_xy(45.0, 0.0, 100.0, 100.0, 90.0)
        assert abs(x - 100.0) < 1e-6, "正北不该有东西向偏移"
        assert y < 100.0, "北在上 → y 小于圆心"

    def test_east_is_on_the_left(self):
        """**东左**是仰视图的惯例(你抬头看天,东在左手边)。

        搞反了整张图就是镜像的,而它看起来完全正常 —— 只有拿真实的
        目标位置对照才会发现。
        """
        from astro_smb_app.views.skychart import radar_xy

        x, _y = radar_xy(45.0, 90.0, 100.0, 100.0, 90.0)
        assert x < 100.0, "东在左 → x 小于圆心"

    def test_west_is_on_the_right(self):
        from astro_smb_app.views.skychart import radar_xy

        x, _y = radar_xy(45.0, 270.0, 100.0, 100.0, 90.0)
        assert x > 100.0

    def test_south_is_down(self):
        from astro_smb_app.views.skychart import radar_xy

        _x, y = radar_xy(45.0, 180.0, 100.0, 100.0, 90.0)
        assert y > 100.0

    def test_zenith_is_the_centre_and_horizon_is_the_rim(self):
        """r = R·(90-alt)/90:天顶在圆心,地平在圆周。"""
        from astro_smb_app.views.skychart import radar_xy

        import math

        cx = cy = 100.0
        r = 90.0
        zx, zy = radar_xy(90.0, 123.0, cx, cy, r)      # 天顶,方位无所谓
        assert math.hypot(zx - cx, zy - cy) < 1e-6
        hx, hy = radar_xy(0.0, 45.0, cx, cy, r)        # 地平
        assert abs(math.hypot(hx - cx, hy - cy) - r) < 1e-6

    def test_below_the_horizon_stays_inside_the_canvas(self):
        """地平线下的点要画出来(那多半是站点纬度设错了),但不能跑出画布。"""
        from astro_smb_app.views.skychart import radar_xy

        import math

        cx = cy = 100.0
        r = 90.0
        for alt in (-1.0, -5.0, -30.0, -90.0):
            x, y = radar_xy(alt, 0.0, cx, cy, r)
            assert math.hypot(x - cx, y - cy) <= r * (95.0 / 90.0) + 1e-6, alt

    def test_the_two_pages_use_the_same_projection(self):
        """浏览页的迷你雷达与记录页的大图必须同一份公式。

        docs/DEVELOPMENT.md:"改投影必须三处同步"。B11 把它收成一份,这条钉住它别再分叉。
        (Uno 删除后转指 Qt —— 守的性质没变:**两页都不许自己再算一遍投影**。
        投影镜像了看起来完全正常,这正是变异测试抓到过的那一类。)
        """
        from pathlib import Path

        root = Path(__file__).resolve().parents[1] / "astro_smb_qt" / "pages"
        for name in ("browser.py", "records.py"):
            src = (root / name).read_text(encoding="utf-8")
            assert "skychart" in src, f"{name} 里有人又自己算了一遍投影"


class TestEnvelopeThresholdIsReachable:
    """密集导星段**必须真的切到包络视图**。

    变异测试把 `ENV_FRAMES_PER_PX` 从 2.0 抬到 2000 —— 一条测试都没红。
    抬上去之后 7543 帧又会画成逐帧折线,也就是一团噪声:不报错、不崩溃,
    只是那张图重新变得读不了。老 UI 的判据一直是"> 2 帧/像素就切包络"。
    """

    def test_a_real_section_crosses_the_threshold(self):
        from astro_smb_app.views.guiding import ENV_FRAMES_PER_PX

        frames = 7543          # 真机:07-30 那段 125.8 分钟
        width = 900.0          # 曲线画布宽(pages/guiding.CURVE_W)
        assert frames > ENV_FRAMES_PER_PX * width, (
            f"阈值 {ENV_FRAMES_PER_PX} 帧/像素太高 —— 密集段不会切包络,"
            "曲线又会变成一团噪声")

    def test_a_short_section_stays_a_plain_polyline(self):
        """反面:几百帧的段直接画折线更清楚,不该套包络。"""
        from astro_smb_app.views.guiding import ENV_FRAMES_PER_PX

        assert 468 < ENV_FRAMES_PER_PX * 900.0      # 真机:22:46 那段 468 帧



class TestRunningJudgement:
    """"正在拍摄"的判据:**最新帧 mtime 距今 < 曝光时长 + 容差**。

    变异测试把 `IDLE_GRACE_S` 从 600 秒压到 6 秒 —— 一条测试都没红。
    压下去之后每次换目标、每次自动对焦都会被判成"停机"(真机实测那些间隙
    是 6~7 分钟),状态栏会在拍摄途中反复闪回空闲。

    这条判据是真机试出来的,而且**不能改成回读日志** —— Autorun 日志是会话
    结束时一次性写盘的,运行中设备上根本看不到。
    """

    def test_the_grace_covers_a_target_change(self):
        from astro_smb_app.watcher import IDLE_GRACE_S

        # 真机实测:换目标 + 自动对焦的间隙 6~7 分钟
        assert IDLE_GRACE_S >= 7 * 60, (
            f"容差 {IDLE_GRACE_S}s 盖不住换目标的间隙 —— 会误报停机")

    def test_the_grace_is_not_so_long_that_idle_looks_busy(self):
        """反面:容差太长的话,收工两小时了还说"正在拍摄"。"""
        from astro_smb_app.watcher import IDLE_GRACE_S

        assert IDLE_GRACE_S <= 30 * 60

    def test_a_long_exposure_extends_the_window(self):
        """判据是**曝光 + 容差**,不是固定值 —— 300s 的 sub 之间本来就隔得远。"""
        from astro_smb_app.watcher import IDLE_GRACE_S

        for exposure in (1.0, 60.0, 300.0):
            threshold = exposure + IDLE_GRACE_S
            assert threshold > exposure, "阈值必须随曝光变长"
            # 刚拍完一张:一定还算在拍
            assert exposure * 0.5 < threshold

    def test_it_never_reads_the_log_to_decide(self):
        """**判据不能回读日志。** Autorun 日志会话结束才写盘,运行中看不到 ——
        真机实证,注释里写着"勿优化回读日志"。"""
        import inspect

        from astro_smb_app import watcher

        src = inspect.getsource(watcher.RunWatcher._poll_once) \
            if hasattr(watcher.RunWatcher, "_poll_once") else \
            inspect.getsource(watcher)
        # 判"正在拍摄"的那段不该去解析 Autorun_Log
        running_part = src.split("running=")[0]
        assert "parse_autorun_log" not in running_part


class TestImageNameIsAnchored:
    """文件名解析必须**从头锚定**,不能在串里到处搜。

    变异测试把 `_RE_IMAGE.match` 换成 `.search` —— 一条测试都没红。
    换成搜索之后,任何**包含**一个合法影像名的串都会被解析成功:
    完整路径、带前缀的备份名、甚至一句日志。而这个函数的返回值会被直接
    当成"这是第几张、什么目标、哪一夜",错一次就是整行副行和夜次徽章都错。
    """

    def test_a_full_path_is_not_an_image_name(self):
        """**这条是那个变异**:`.search` 会从路径里挖出文件名部分。"""
        from astro_smb.naming import parse_image_name

        name = "Light_M 8_300.0s_Bin1_20260723-210000_0001.fit"
        assert parse_image_name(name) is not None       # 光名字:认
        assert parse_image_name("EMMC Images/Plan/Light/" + name) is None
        assert parse_image_name("C:" + chr(92) + "down" + chr(92) + name) is None

    def test_a_prefixed_name_is_not_an_image_name(self):
        from astro_smb.naming import parse_image_name

        name = "Light_M 8_300.0s_Bin1_20260723-210000_0001.fit"
        for prefix in ("copy of ", "old_", "(1) ", "~$"):
            assert parse_image_name(prefix + name) is None, prefix

    def test_a_log_line_containing_a_name_is_not_an_image_name(self):
        from astro_smb.naming import parse_image_name

        line = ("2026-07-23 21:00:01 saved "
                "Light_M 8_300.0s_Bin1_20260723-210000_0001.fit ok")
        assert parse_image_name(line) is None

    def test_real_asiair_names_still_parse(self):
        """反面:真机上的各种写法都得认,否则"锚定"就成了"什么都不认"。"""
        from astro_smb.naming import parse_image_name

        samples = [
            "Light_M 8_300.0s_Bin1_20260723-210000_0001.fit",
            "Bias_1.0ms_Bin1_4C_20260723-200544_91deg_0001.fit",
            "Bias_1.0ms_Bin1_4C_20260723-200544_91deg_0001_thn.jpg",
            "Flat_10.0ms_Bin1_Dul_20260723-193012_0003.fit",
        ]
        for name in samples:
            got = parse_image_name(name)
            assert got is not None, name
            assert got.time is not None, name


class TestKeyOrderIsDeterministic:
    """**返回给界面的 dict,键序不许跟着进程走。**

    `TargetRun.type_stats()` 原来是 ``{k: … for k in planned.keys() | actual.keys()}``
    —— 集合并,而 Python 的字符串 `hash()` 每个进程都不一样。于是拍摄记录页
    那句"已完成 · dark 5/5 · bias 30/30"的帧型顺序**每次启动都在变**。
    不报错、不崩溃,只是看着别扭 —— 同一个病这个仓库犯过第二次
    (上一次是 treemap 的 `hash(ext_category(node))`)。

    这条测试**必须开子进程**:同一个进程里 `PYTHONHASHSEED` 是固定的,
    在进程内怎么调都看不出问题来。
    """

    #: 直接用本文件顶上那份 MINI_LOG —— 它的 Shooting 行是**真机语法**
    #: (``Shooting 2 bias frames, exposure 1.0ms Bin1``)。自己另编一份很容易
    #: 写成解析不出帧型的样子,于是所有键并成一个 `unknown`,这条测试就退化成
    #: "一个元素的列表当然稳定" —— 什么都没测到。**第一版就是这么写的。**
    LOG = MINI_LOG

    def _order_in_a_fresh_process(self, seed: str) -> list:
        import json
        import os
        import subprocess
        import sys

        code = (
            "import json,sys;"
            "from astro_smb.autorunlog import aggregate_nights,parse_autorun_log;"
            "log=parse_autorun_log(sys.stdin.read());"
            "run=aggregate_nights([log])[0].runs[0];"
            "print(json.dumps(list(run.type_stats())))")
        env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONIOENCODING": "utf-8"}
        # **`text=True` 不够,要显式 `encoding`** —— 否则 stdin 按本机
        # 代码页(Windows 上是 cp936)编码,日志里的 `°`/`℃` 直接炸。
        out = subprocess.run([sys.executable, "-c", code], input=self.LOG,
                             capture_output=True, text=True, env=env,
                             encoding="utf-8", cwd=ROOT)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)

    def test_the_order_survives_hash_randomisation(self):
        orders = [self._order_in_a_fresh_process(s)
                  for s in ("0", "1", "12345", "999")]
        assert len(set(map(tuple, orders))) == 1, (
            f"不同 PYTHONHASHSEED 下键序不一样: {orders}")
        # 而且是**日志里出现的顺序**,不是随便某个稳定顺序
        assert orders[0] == ["bias", "dark"], orders[0]
