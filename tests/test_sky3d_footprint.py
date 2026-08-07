"""3D 天球「实际视场足迹」的离线单测(轨道B)。

只测**纯逻辑**:足迹环的几何、sub 抽样、WCS 缓存 payload 往返、覆盖统计、
位置角漂移、wcsapps 适配层、以及 Python↔JS 的消息契约(靠静态扫两个文件)。
WebView2 / XAML 控件要真消息泵,不在单测覆盖。

类名前缀统一用 ``TestFoot*`` —— 与 tests/test_sky3d.py 里已有的
``TestSkyRelevant/TestBuildNights/TestFormatting/TestWebhostAssets`` 不重名
(同名类会互相覆盖,那会让既有测试成片消失)。
"""
from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest

from astro_smb import wcs as W
from tests.support import tr

WIDTH, HEIGHT = 6248, 4176          # ASI2600MC Pro 全幅
SCALE_AS = 1.24                     # 角秒/像素(约 2.15° × 1.44° 视场)


def _wcs(ra: float, dec: float, *, scale_as: float = SCALE_AS,
         rot_deg: float = 0.0, width: int = WIDTH, height: int = HEIGHT,
         flip: bool = True) -> W.TanWcs:
    """造一份 TanWcs。

    刻意让参数直接对上 :class:`~astro_smb.wcs.TanWcs` 的派生量,免得测试断言
    还要在脑子里换一道算:``pixel_scale() == scale_as``、
    ``rotation_deg() == rot_deg``、``flipped() == flip``
    (``flip=True`` 就是 ASIAIR light 帧实测的镜像宇称,det > 0)。
    """
    s = scale_as / 3600.0
    r = math.radians(rot_deg)
    if flip:        # det = +s²
        cd = np.array([[s * math.cos(r), s * math.sin(r)],
                       [-s * math.sin(r), s * math.cos(r)]], dtype=np.float64)
    else:           # det = -s²
        cd = np.array([[-s * math.cos(r), s * math.sin(r)],
                       [s * math.sin(r), s * math.cos(r)]], dtype=np.float64)
    return W.TanWcs((ra, dec), ((width + 1) / 2.0, (height + 1) / 2.0), cd)


def test_wcs_helper_matches_its_own_promises():
    """测试辅助函数自身的自检 —— 它错了下面几十条断言都会跟着错。"""
    w = _wcs(270.0, -24.0, scale_as=1.24, rot_deg=37.0, flip=True)
    assert w.pixel_scale() == pytest.approx(1.24, rel=1e-9)
    assert w.rotation_deg() == pytest.approx(37.0, abs=1e-9)
    assert w.flipped() is True
    assert _wcs(270.0, -24.0, rot_deg=37.0, flip=False).flipped() is False
    assert _wcs(270.0, -24.0, rot_deg=37.0,
                flip=False).rotation_deg() == pytest.approx(37.0, abs=1e-9)


def _unit(ra_deg: float, dec_deg: float) -> np.ndarray:
    r, d = math.radians(ra_deg), math.radians(dec_deg)
    return np.array([math.cos(d) * math.cos(r),
                     math.cos(d) * math.sin(r),
                     math.sin(d)], dtype=np.float64)


def _pairs(ring: list[float]) -> list[tuple[float, float]]:
    return [(ring[2 * i], ring[2 * i + 1]) for i in range(len(ring) // 2)]


def _entry(name: str, *, mtime: float = 0.0, size: int = 52_000_000,
           is_dir: bool = False, share: str = "EMMC Images",
           folder: str = "Plan\\Light\\M 8") -> SimpleNamespace:
    return SimpleNamespace(name=name, is_dir=is_dir, size=size, mtime=mtime,
                           share=share, path=f"{folder}\\{name}")


def _light(ts: datetime, seq: int = 1, angle: int = 123) -> str:
    return (f"Light_M 8_180.0s_Bin1_4C_{ts.strftime('%Y%m%d-%H%M%S')}"
            f"_{angle}deg_{seq:04d}.fit")


# ================================================================ 足迹环几何


class TestFootRing:
    def test_point_count_and_closure(self):
        from astro_smb_gui._sky3d import FOOT_EDGE_STEPS, _footprint_ring

        ring = _footprint_ring(_wcs(270.0, -24.0), WIDTH, HEIGHT)
        assert len(ring) == 2 * 4 * FOOT_EDGE_STEPS
        # 首点不等于末点:环是**隐式闭合**的(JS 用 LineLoop / 取模的三角扇),
        # 显式重复一个点会多画一条零长边
        assert (ring[0], ring[1]) != (ring[-2], ring[-1])

    def test_points_lie_on_image_outer_boundary(self):
        """环点必须正好落在 [0.5, W+0.5] × [0.5, H+0.5] 的**外边界**上。

        用 0/W 而不是 0.5/W+0.5 会让足迹整体缩掉半个像素 —— 数值上小,
        但说明作者没搞清 FITS 的 1-based 像素中心约定,后面必然连累别处。

        容差 0.05 px:环点的 ra/dec 只保留 5 位小数(0.036″,为了压 JSON 体积),
        换算回来就是 0.03 px 量级的往返误差。半像素的错位是它的 10 倍以上,
        照样会被这条抓住。
        """
        from astro_smb_gui._sky3d import _footprint_ring

        tol = 0.05
        w = _wcs(270.0, -24.0)
        ring = _footprint_ring(w, WIDTH, HEIGHT)
        ra = np.array(ring[0::2])
        dec = np.array(ring[1::2])
        x, y = W.world_to_pixel(w, ra, dec)
        on_x = np.isclose(x, 0.5, atol=tol) | np.isclose(x, WIDTH + 0.5, atol=tol)
        on_y = np.isclose(y, 0.5, atol=tol) | np.isclose(y, HEIGHT + 0.5, atol=tol)
        assert np.all(on_x | on_y)
        assert x.min() == pytest.approx(0.5, abs=tol)
        assert x.max() == pytest.approx(WIDTH + 0.5, abs=tol)
        assert y.min() == pytest.approx(0.5, abs=tol)
        assert y.max() == pytest.approx(HEIGHT + 0.5, abs=tol)

    @pytest.mark.parametrize("ra,dec,scale", [
        (270.0, -24.0, SCALE_AS),       # 常规
        (45.0, 89.4, SCALE_AS),         # 近北天极
        (45.0, 89.8, 4.0),              # 更近极点 + 更大视场
        (0.05, 5.0, SCALE_AS),          # 跨 RA=0
    ])
    def test_edge_samples_lie_on_the_great_circle(self, ra, dec, scale):
        """一条边上的中间取样点必须落在两个角点所定的**大圆**上。

        这是"像素空间等分 == 大圆细分"这条设计的核心断言。若有人把实现"简化"成
        在 (ra, dec) 上线性插值,常规视场就偏 16″、赤纬 89.4° 处偏 3468″ —— 本用例
        会立刻变红(见 test_naive_radec_lerp_would_be_wrong 的对照)。
        """
        from astro_smb_gui._sky3d import FOOT_EDGE_STEPS, _footprint_ring

        n = FOOT_EDGE_STEPS
        pts = _pairs(_footprint_ring(_wcs(ra, dec, scale_as=scale), WIDTH, HEIGHT))
        for edge in range(4):
            a = _unit(*pts[edge * n])
            b = _unit(*pts[((edge + 1) * n) % len(pts)])
            normal = np.cross(a, b)
            normal /= np.linalg.norm(normal)
            for k in range(1, n):
                u = _unit(*pts[edge * n + k])
                off_arcsec = math.degrees(abs(float(np.dot(u, normal)))) * 3600.0
                assert off_arcsec < 0.1

    def test_naive_radec_lerp_would_be_wrong(self):
        """对照实验:证明上一条断言不是空转的。

        近极点时"在 (ra, dec) 上线性插值"离真实边界差着近 1°。这条用例把那个数量级
        钉死,免得将来有人觉得"细分随便怎么插值都行"。
        """
        from astro_smb_gui._sky3d import FOOT_EDGE_STEPS, _footprint_ring

        n = FOOT_EDGE_STEPS
        pts = _pairs(_footprint_ring(_wcs(45.0, 89.4), WIDTH, HEIGHT))
        worst = 0.0
        for k in range(1, n):
            f = k / n
            ra_l = pts[0][0] + (pts[n][0] - pts[0][0]) * f
            dec_l = pts[0][1] + (pts[n][1] - pts[0][1]) * f
            sep = math.degrees(math.acos(
                max(-1.0, min(1.0, float(np.dot(_unit(ra_l, dec_l),
                                                _unit(*pts[k])))))))
            worst = max(worst, sep * 3600.0)
        assert worst > 100.0

    def test_ra_wrap_is_left_raw(self):
        """跨 RA=0 时环里同时有 ~0° 和 ~360° 的值,**不许**被 unwrap 成 -0.1 或 360.1。

        JS 侧靠 dir(ra, dec) 逐点转单位向量,原样的 359.9/0.1 完全正确;真正会炸的是
        有人"顺手"把 RA 排序/连续化。
        """
        from astro_smb_gui._sky3d import _footprint_ring

        ring = _footprint_ring(_wcs(0.05, 5.0), WIDTH, HEIGHT)
        ra = ring[0::2]
        assert all(0.0 <= v < 360.0 for v in ra)
        assert min(ra) < 1.5 and max(ra) > 358.5

    def test_segments_stay_short(self):
        """相邻取样点之间 < 90°:弦的走向永不含糊(> 180° 会绕反)。"""
        from astro_smb_gui._sky3d import _footprint_ring

        pts = _pairs(_footprint_ring(_wcs(45.0, 89.8, scale_as=4.0),
                                     WIDTH, HEIGHT))
        for i, p in enumerate(pts):
            q = pts[(i + 1) % len(pts)]
            cos = float(np.dot(_unit(*p), _unit(*q)))
            assert math.degrees(math.acos(max(-1.0, min(1.0, cos)))) < 90.0

    def test_dec_never_leaves_range(self):
        from astro_smb_gui._sky3d import _footprint_ring

        ring = _footprint_ring(_wcs(45.0, 89.9, scale_as=4.0), WIDTH, HEIGHT)
        assert all(-90.0 <= v <= 90.0 for v in ring[1::2])

    def test_frame_center_matches_crval_when_crpix_centered(self):
        from astro_smb_gui._sky3d import _frame_center

        ra, dec = _frame_center(_wcs(270.75, -24.38), WIDTH, HEIGHT)
        assert ra == pytest.approx(270.75, abs=1e-6)
        assert dec == pytest.approx(-24.38, abs=1e-6)


# ================================================================ sub 抽样


class TestFootPickSubs:
    def test_filters_other_nights(self):
        """``Plan\\Light\\<目标>`` 是跨夜累积的 —— 不按时刻窗口过滤就会把
        昨夜的足迹画到今夜上(真机上这个目录动辄上千张)。"""
        from astro_smb_gui._sky3d import _pick_subs

        night = datetime(2026, 7, 25, 22, 0, 0)
        prev = datetime(2026, 7, 20, 22, 0, 0)
        entries = [_entry(_light(night + timedelta(minutes=3 * i), i + 1))
                   for i in range(4)]
        entries += [_entry(_light(prev + timedelta(minutes=3 * i), i + 1))
                    for i in range(5)]
        got = _pick_subs(entries, night.timestamp(),
                         (night + timedelta(minutes=10)).timestamp())
        assert len(got) == 4
        assert all("20260725" in e.name for e in got)

    def test_skips_thumbnails_dirs_and_other_types(self):
        from astro_smb_gui._sky3d import _pick_subs

        t = datetime(2026, 7, 25, 22, 0, 0)
        good = _light(t, 1)
        entries = [
            _entry(good),
            _entry(good.replace(".fit", "_thn.jpg")),      # 缩略图不是 sub
            _entry("notes.txt"),
            _entry("subdir", is_dir=True),
        ]
        got = _pick_subs(entries, t.timestamp(), t.timestamp())
        assert [e.name for e in got] == [good]

    def test_thinning_keeps_first_and_last(self):
        """抽稀取首尾 + 均匀间隔 —— 取"前 N 张"会把整夜漂移信息全丢掉。"""
        from astro_smb_gui._sky3d import _pick_subs

        t0 = datetime(2026, 7, 25, 21, 0, 0)
        entries = [_entry(_light(t0 + timedelta(minutes=3 * i), i + 1))
                   for i in range(60)]
        got = _pick_subs(entries, t0.timestamp(),
                         (t0 + timedelta(minutes=3 * 59)).timestamp(), limit=6)
        assert len(got) == 6
        assert got[0].name == entries[0].name
        assert got[-1].name == entries[-1].name

    def test_sorted_by_time_not_by_name(self):
        from astro_smb_gui._sky3d import _pick_subs

        t0 = datetime(2026, 7, 25, 21, 0, 0)
        entries = [_entry(_light(t0 + timedelta(minutes=10), 2)),
                   _entry(_light(t0, 1))]
        got = _pick_subs(entries, t0.timestamp(),
                         (t0 + timedelta(minutes=20)).timestamp())
        assert [e.name for e in got] == [entries[1].name, entries[0].name]

    def test_entry_time_prefers_filename_then_mtime(self):
        from astro_smb_gui._sky3d import _entry_time

        t = datetime(2026, 7, 25, 22, 30, 15)
        assert _entry_time(_entry(_light(t), mtime=1.0)) == pytest.approx(
            t.timestamp())
        assert _entry_time(_entry("weird.fit", mtime=1234.5)) == pytest.approx(
            1234.5)


# ================================================================ 缓存 payload


class TestFootWcsPayload:
    def test_round_trip(self):
        from astro_smb_gui._sky3d import _wcs_from_payload, _wcs_to_payload

        w = _wcs(270.75, -24.38, rot_deg=17.0)
        got = _wcs_from_payload(_wcs_to_payload(w, WIDTH, HEIGHT, src="hdr"))
        assert got is not None
        back, width, height = got
        assert (width, height) == (WIDTH, HEIGHT)
        assert back.crval == pytest.approx(w.crval)
        assert back.crpix == pytest.approx(w.crpix)
        assert np.allclose(back.cd, w.cd)
        assert back.flipped() == w.flipped()

    def test_version_bump_invalidates(self):
        """payload 结构改了就必须 +FOOT_CACHE_V,否则旧库里的行会被当新结构读。"""
        from astro_smb_gui._sky3d import _wcs_from_payload, _wcs_to_payload

        p = _wcs_to_payload(_wcs(10.0, 10.0), WIDTH, HEIGHT)
        p["v"] = p["v"] + 1
        assert _wcs_from_payload(p) is None

    def test_failure_payload_is_not_a_wcs(self):
        from astro_smb_gui._sky3d import FOOT_CACHE_V, _wcs_from_payload

        assert _wcs_from_payload(
            {"v": FOOT_CACHE_V, "ok": False, "reason": "星点太少"}) is None

    @pytest.mark.parametrize("mutate", [
        lambda p: p.update(cd=[0.0, 0.0, 0.0, 0.0]),     # 退化 CD
        lambda p: p.update(cd=[1.0, 2.0]),               # 长度不对
        lambda p: p.update(w=0),
        lambda p: p.pop("crval"),
        lambda p: p.update(crval=["x", "y"]),
    ])
    def test_corrupt_payload_is_a_miss_not_a_crash(self, mutate):
        from astro_smb_gui._sky3d import _wcs_from_payload, _wcs_to_payload

        p = _wcs_to_payload(_wcs(10.0, 10.0), WIDTH, HEIGHT)
        mutate(p)
        assert _wcs_from_payload(p) is None

    def test_payload_is_json_round_trippable(self):
        """metacache 存的是 JSON —— payload 里混进 ndarray/tuple 会静默写不进去。"""
        import json

        from astro_smb_gui._sky3d import _wcs_from_payload, _wcs_to_payload

        p = _wcs_to_payload(_wcs(270.0, -24.0), WIDTH, HEIGHT, src="solve",
                            nmatch=42, rms=0.61)
        assert _wcs_from_payload(json.loads(json.dumps(p))) is not None


# ================================================================ 覆盖统计


class TestFootCoverageRemoved:
    """`_coverage_stats` 已删,本类原有的 6 条用例随之失去测试对象。

    它测的是 `_sky3d` 自带的那套网格覆盖估算 —— 与 `astro_smb.wcsapps.coverage`
    并存、喂同一个 UI 栏位。两套口径不同(格数 × 平面格面积 vs gnomonic 面积元
    逐格加权),靠人工纪律维持一致,离中心几度外就分道扬镳。
    覆盖算法的正经测试在 `tests/test_wcsapps.py`(135 条)。
    新契约由 `TestCoverageHasSingleSource` 守着。
    """



class TestFootRotDrift:
    def test_linear_slope(self):
        from astro_smb_gui._sky3d import _rot_drift

        assert _rot_drift([0, 3600, 7200], [10.0, 10.5, 11.0]) == pytest.approx(0.5)

    def test_wrap_around_360_is_unwrapped(self):
        """359.0 → 359.8 → 0.6 是 +0.8°/h,不是 -179°/h。

        不 unwrap 的话这一栏会给出天文数字,而位置角刚好最常在 0/360 附近晃。
        """
        from astro_smb_gui._sky3d import _rot_drift

        assert _rot_drift([0, 3600, 7200],
                          [359.0, 359.8, 0.6]) == pytest.approx(0.8, abs=1e-6)

    def test_meridian_flip_gives_no_false_rate(self):
        from astro_smb_gui._sky3d import _meridian_flip, _rot_drift

        rots = [20.0, 20.1, 199.9, 200.0]
        assert _meridian_flip(rots)
        assert _rot_drift([0, 600, 1200, 1800], rots) is None

    def test_short_span_gives_no_verdict(self):
        from astro_smb_gui._sky3d import _rot_drift

        assert _rot_drift([0.0, 60.0], [10.0, 12.0]) is None

    def test_single_sample(self):
        from astro_smb_gui._sky3d import _rot_drift

        assert _rot_drift([0.0], [10.0]) is None

    def test_nan_samples_are_dropped(self):
        from astro_smb_gui._sky3d import _rot_drift

        got = _rot_drift([0, 3600, 7200, 10800],
                         [10.0, float("nan"), 11.0, 11.5])
        assert got == pytest.approx(0.5, abs=0.02)


class TestFootGuideQuality:
    @staticmethod
    def _run():
        frame = SimpleNamespace(exposure_s=180.0)
        return SimpleNamespace(
            target="M 8", ra="18h03m00s", dec="-24°23'00\"",
            plan_no=1, begin_time=datetime(2026, 7, 25, 22, 0),
            end_time=datetime(2026, 7, 25, 23, 0),
            all_frames=lambda: [frame, frame],
            frame_span=lambda: (
                datetime(2026, 7, 25, 22, 5),
                datetime(2026, 7, 25, 22, 55)),
        )

    def test_record_run_builds_independent_quality_target(self):
        from astro_smb_gui._sky3d import _quality_target_for_run

        run = self._run()
        target = _quality_target_for_run(run)
        assert target["name"] == "M 8"
        assert target["runs"] == [run]
        assert target["frames"] == 2 and target["exposure"] == 360.0
        assert target["ts0"] == datetime(2026, 7, 25, 22, 5).timestamp()
        assert target["ts1"] == datetime(2026, 7, 25, 22, 55).timestamp()

    def test_records_request_does_not_require_3d_toggle(self, monkeypatch):
        from astro_smb_gui._sky3d import Sky3DPage

        started = []

        class FakeThread:
            def __init__(self, **kw):
                started.append(kw)

            def start(self):
                started[-1]["started"] = True

        states = []
        page = Sky3DPage.__new__(Sky3DPage)
        page._quality_cancel = {}
        page._lat, page._lon = 30.0, 120.0
        page.shell = SimpleNamespace(
            client=object(),
            logstore=SimpleNamespace(data=SimpleNamespace(phd2_logs=[])),
            set_guide_quality_state=lambda *a, **k: states.append((a, k)),
        )
        monkeypatch.setattr("astro_smb_gui._sky3d.threading.Thread", FakeThread)
        run = self._run()
        assert page.request_guide_quality(run)
        assert id(run) in page._quality_cancel
        assert started and started[0]["name"] == "records-guide-quality"
        assert started[0]["started"]
        # 独立入口不读取 FootprintToggle / _night_idx / _targets。
        assert states and states[-1][1]["busy"] is True
        assert page.cancel_guide_quality(run)
        assert page._quality_cancel[id(run)].is_set()

    def test_quality_uses_plate_solves_even_without_phd2(self):
        from astro_smb_gui._sky3d import _quality_for

        base = datetime(2026, 7, 25, 22, 0).timestamp()
        foots = [
            {"ts": base + i * 600, "ra": 100.0, "dec": 20.0,
             "rot": 30.0, "scale": 1.0, "focal": 400.0,
             "star_fwhm_px": 3.0, "star_fwhm_arcsec": 3.0,
             "star_ellipticity": 0.1, "star_theta_deg": 0.0,
             "star_theta_r": 0.1, "nmatch": 80}
            for i in range(3)
        ]
        quality = _quality_for(
            foots, {"frames": 3, "exposure": 540.0, "runs": []},
            [], [], 30.0, 120.0)
        assert quality.verdict == "unknown"
        assert "缺少同期导星日志" in quality.headline
        assert any("主镜星点" in line for line in quality.findings)


# ================================================================ 汇总/文案


class TestFootAggregate:
    def _payload(self, **kw):
        from astro_smb_gui._sky3d import _wcs_to_payload

        return _wcs_to_payload(_wcs(kw.pop("ra", 270.0), kw.pop("dec", -24.0),
                                    rot_deg=kw.pop("rot", 0.0)),
                               WIDTH, HEIGHT, **kw)

    def test_build_foot_shape(self):
        from astro_smb_gui._sky3d import _build_foot

        t = {"name": "M 8", "color": "#7FD88F"}
        when = datetime(2026, 7, 25, 22, 5, 0)
        ent = _entry(_light(when))
        foot = _build_foot(t, ent, self._payload(src="hdr", sip=True))
        assert foot is not None
        assert foot["id"] == f"{ent.share}|{ent.path}"
        assert foot["target"] == "M 8" and foot["color"] == "#7FD88F"
        assert foot["ts"] == pytest.approx(when.timestamp())
        assert len(foot["ring"]) >= 24 and foot["sip"] is True
        assert foot["scale"] == pytest.approx(SCALE_AS, rel=1e-6)
        assert foot["flip"] is True
        assert foot["fov"][0] == pytest.approx(2.15, abs=0.05)

    def test_build_foot_rejects_bad_payload(self):
        from astro_smb_gui._sky3d import FOOT_CACHE_V, _build_foot

        assert _build_foot({"name": "X"}, _entry("a.fit"),
                           {"v": FOOT_CACHE_V, "ok": False}) is None

    def test_build_foot_payload_has_no_unserialisable_leftovers(self):
        """JS 侧只吃 id/target/color/label/ring —— 这几项必须是纯 JSON 类型。"""
        import json

        from astro_smb_gui._sky3d import _build_foot

        foot = _build_foot({"name": "M 8", "color": "#7FD88F"},
                           _entry(_light(datetime(2026, 7, 25, 22, 0, 0))),
                           self._payload(src="hdr"))
        json.dumps({"id": foot["id"], "target": foot["target"],
                    "color": foot["color"], "label": foot["file"],
                    "ring": foot["ring"]})

    def test_cover_for_counts_sources_and_pointing_error(self):
        from astro_smb_gui._sky3d import _build_foot, _cover_for

        t = {"name": "M 8", "color": "#7FD88F",
             "log_ra": 270.0, "log_dec": -24.0}
        base = datetime(2026, 7, 25, 21, 0, 0)
        foots = []
        # 实际中心比日志 goto 偏了 0.1° —— 指向误差就该是这个数
        for i in range(4):
            ent = _entry(_light(base + timedelta(hours=i)), mtime=0.0)
            src = "hdr" if i < 3 else "solve"
            extra = {"src": src, "sip": src == "hdr"}
            if src == "solve":
                extra.update(nmatch=51, rms=0.62)
            foots.append(_build_foot(
                t, ent, self._payload(ra=270.1, dec=-24.0, rot=i * 0.25, **extra)))
        cov = _cover_for(foots, t)
        assert cov["n"] == 4 and cov["n_hdr"] == 3 and cov["n_solved"] == 1
        assert cov["n_sip"] == 3
        assert cov["point_err"] == pytest.approx(0.1 * math.cos(math.radians(24.0)),
                                                 rel=0.02)
        assert cov["rms_med"] == pytest.approx(0.62)
        assert cov["drift"] == pytest.approx(0.25, abs=0.01)
        assert cov["span_h"] == pytest.approx(3.0, abs=0.01)
        assert cov["common"] > 0.9

    def test_cover_for_without_log_coords(self):
        from astro_smb_gui._sky3d import _build_foot, _cover_for

        t = {"name": "M 8", "color": "#7FD88F"}
        foot = _build_foot(t, _entry(_light(datetime(2026, 7, 25, 22, 0, 0))),
                           self._payload(src="hdr"))
        cov = _cover_for([foot], t)
        assert cov["point_err"] is None
        assert cov["drift"] is None

    def test_foot_note_mentions_every_shortfall(self):
        from astro_smb_gui._sky3d import _foot_note

        note = _foot_note(3, 10, need_cat=4, pending=2, failed=1, cat_ok=False)
        assert "3/10" in note
        assert "星表" in note and "4" in note
        assert "排队" in note and "刷新" in note
        assert "失败" in note

    def test_foot_note_clean_run(self):
        from astro_smb_gui._sky3d import _foot_note

        note = _foot_note(8, 8, 0, 0, 0, True)
        assert note == tr("实际视场: {n_ok}/{total} 张", n_ok=8, total=8)

    def test_foot_note_explains_total_miss(self):
        from astro_smb_gui._sky3d import _foot_note

        assert "没有 WCS" in _foot_note(0, 5, 0, 0, 0, True)


class TestFootFormatting:
    def test_area(self):
        from astro_smb_gui._sky3d import _fmt_area

        assert _fmt_area(3.1) == tr("{deg2:.2f} 平方度", deg2=3.1)
        assert _fmt_area(0.25) == tr("{0:.0f} 平方角分", 0.25 * 3600.0)

    def test_sep_switches_units(self):
        from astro_smb_gui._sky3d import _fmt_sep

        assert _fmt_sep(2.5) == "2.50°"
        assert _fmt_sep(0.5).endswith("′")
        assert _fmt_sep(0.001).endswith("″")

    def test_drift_keeps_sign(self):
        from astro_smb_gui._sky3d import _fmt_drift

        assert _fmt_drift(0.25).startswith("+")
        assert _fmt_drift(-0.25).startswith("-")

    def test_new_glyphs_are_bmp(self):
        """§7.1:非 BMP 字符会让 HSTRING 末尾少一个字 —— 图标只能用私用区码位。"""
        from astro_smb_gui import _sky3d

        for g in (_sky3d.GLYPH_COVER, _sky3d.GLYPH_SUB):
            assert len(g) == 1 and ord(g) <= 0xFFFF

    def test_no_astral_plane_chars_in_page_strings(self):
        """整个模块的字符串字面量里都不许出现代理对(emoji 等)。"""
        import ast
        from pathlib import Path

        import astro_smb_gui._sky3d as mod

        tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                assert all(ord(ch) <= 0xFFFF for ch in node.value), node.value


# ================================================================ wcsapps 适配


class TestFootWcsappsAdapter:
    """适配层针对**轨道A 的真实 API**(``astro_smb.wcsapps``)。

    真模块在(生产路径)与不在/抛异常(降级路径)都要测:那两条分支决定了这张卡
    是"给数"还是"崩掉整个工作线程"。
    """

    def _foots(self, n=3, ra=270.1, dec=-24.0):
        from astro_smb_gui._sky3d import _build_foot, _wcs_to_payload

        base = datetime(2026, 7, 25, 21, 0, 0)
        out = []
        for i in range(n):
            payload = _wcs_to_payload(_wcs(ra, dec, rot_deg=i * 0.25),
                                      WIDTH, HEIGHT, src="hdr")
            out.append(_build_foot({"name": "M 8", "color": "#7FD88F"},
                                   _entry(_light(base + timedelta(hours=i))),
                                   payload))
        return out

    def _target(self):
        return {"name": "M 8", "log_ra": 270.0, "log_dec": -24.0}

    @pytest.fixture
    def fake_wcsapps(self, monkeypatch):
        """把一个假的 astro_smb.wcsapps 塞进 sys.modules(测降级分支用)。"""
        def _make(**attrs):
            mod = ModuleType("astro_smb.wcsapps")
            for k, v in attrs.items():
                setattr(mod, k, v)
            monkeypatch.setitem(sys.modules, "astro_smb.wcsapps", mod)
            import astro_smb
            monkeypatch.setattr(astro_smb, "wcsapps", mod, raising=False)
            return mod
        return _make

    def test_real_module_is_used_and_agrees_with_the_local_fallback(self):
        """真 wcsapps 在场时必须走它,且两条路径的口径要**对得上**。

        UI 上"公共交集/单帧留存"是同一栏,来源不同却换个定义的话用户没法比较。
        这条同时钉住"优先用 wcsapps"和"两边口径一致"。
        """
        pytest.importorskip("astro_smb.wcsapps")
        from astro_smb_gui._sky3d import _cover_for, _local_cover

        # **契约已变**:原来这里拿 _local_cover 的数字与 wcsapps 对拍,
        # 前提是"两套算法都算覆盖、且应当吻合"。那正是要消除的东西 ——
        # 现在覆盖只有 wcsapps 一个来源,_local_cover 只在它失败时把这几项
        # 标成"不可用"。
        foots = self._foots(4)
        got = _cover_for(foots, self._target())
        assert got["source"] == "wcsapps"
        assert got["area"] > 0 and got["common"] is not None
        local = _local_cover(foots, self._target())
        assert local["source"] == "不可用" and local["area"] is None
        # 不依赖 wcsapps 的那几项两边都该给
        assert local["span_h"] == pytest.approx(got["span_h"], abs=1e-6)
        assert local["point_err"] == pytest.approx(got["point_err"], rel=0.05)

    def test_real_module_reports_gaps_field(self):
        pytest.importorskip("astro_smb.wcsapps")
        from astro_smb_gui._sky3d import _wcsapps_cover

        got = _wcsapps_cover(self._foots(3), self._target())
        assert got is not None
        assert isinstance(got["n_gaps"], int)
        assert got["n"] == 3

    def test_absent_module_degrades_to_none(self, monkeypatch):
        """模拟轨道A 还没落地(本页曾经就是在这个状态下开发的)。

        注意光把 ``sys.modules`` 设成 None 不够:``from astro_smb import wcsapps``
        会先走父包的属性,导入过一次后那个属性一直在 —— 必须连它一起摘掉。
        """
        import astro_smb

        from astro_smb_gui._sky3d import _wcsapps_cover

        monkeypatch.delattr(astro_smb, "wcsapps", raising=False)
        monkeypatch.setitem(sys.modules, "astro_smb.wcsapps", None)
        assert _wcsapps_cover(self._foots(2), self._target()) is None

    def test_raising_coverage_degrades(self, fake_wcsapps):
        from astro_smb_gui._sky3d import _wcsapps_cover

        def coverage(items, **kw):
            raise RuntimeError("足迹散布超过 90°")

        fake_wcsapps(coverage=coverage)
        assert _wcsapps_cover(self._foots(2), self._target()) is None

    def test_renamed_api_degrades(self, fake_wcsapps):
        """签名/命名再变(轨道A 仍在动)也只该降级,不该炸掉工作线程。"""
        from astro_smb_gui._sky3d import _wcsapps_cover

        fake_wcsapps(something_else=lambda *a, **k: None)
        assert _wcsapps_cover(self._foots(2), self._target()) is None

    def test_pointing_and_rotation_are_optional_extras(self, fake_wcsapps):
        """coverage 成功但 pointing_error / field_rotation 挂了,不该整卡回退。"""
        from astro_smb_gui._sky3d import _wcsapps_cover

        cov = SimpleNamespace(n_frames=2, union_area_deg2=6.0,
                              common_area_deg2=3.0,
                              frame_area_deg2=np.array([4.0, 4.0]),
                              n_gaps=0, max_gap_deg2=0.0)

        def boom(*a, **k):
            raise RuntimeError("还没写完")

        fake_wcsapps(coverage=lambda items, **kw: cov,
                     pointing_error=boom, field_rotation=boom)
        got = _wcsapps_cover(self._foots(2), self._target())
        assert got["source"] == "wcsapps"
        assert got["common"] == pytest.approx(0.5)
        assert got["keep"] == pytest.approx(0.75)
        assert got["point_err"] is None and got["drift"] is None

    def test_cover_for_falls_back_when_wcsapps_unusable(self, fake_wcsapps):
        from astro_smb_gui._sky3d import _cover_for

        fake_wcsapps(coverage=lambda *a, **k: (_ for _ in ()).throw(
            RuntimeError("nope")))
        cov = _cover_for(self._foots(3), self._target())
        # **契约已变**:wcsapps 不可用时不再换一套算法凑数字,而是明说算不出来。
        assert cov["source"] == "不可用"
        assert cov["area"] is None and cov["n"] == 3
        assert cov["span_h"] > 0, "跨度不依赖 wcsapps,照常给"

    def test_short_span_gives_no_drift_on_either_path(self, fake_wcsapps):
        """"跨度 < 5 分钟不给漂移结论"这条规矩两边都要守。"""
        from astro_smb_gui._sky3d import _build_foot, _cover_for, _wcs_to_payload

        base = datetime(2026, 7, 25, 21, 0, 0)
        foots = [_build_foot({"name": "M 8", "color": "#7FD88F"},
                             _entry(_light(base + timedelta(seconds=60 * i))),
                             _wcs_to_payload(_wcs(270.0, -24.0, rot_deg=i * 0.5),
                                             WIDTH, HEIGHT, src="hdr"))
                 for i in range(3)]
        assert _cover_for(foots, self._target())["drift"] is None


# ================================================================ 工作线程编排


class _Harness:
    """驱动 ``Sky3DPage._foot_work`` 的最小替身 —— 不建任何 XAML、不连 SMB。

    ``_foot_work`` 是这个功能里最容易出错的一段(缓存/头/解算三条路径 × 预算 ×
    取消 × 连续失败熔断),但它以前只能靠真机跑。这里把它当**未绑定函数**调用
    (``Sky3DPage._foot_work(harness, ...)``),self 上需要什么就补什么。
    """

    def __init__(self, listing, *, cat_ok=True, header=None, solver=None,
                 cached=None):
        self._listing = listing         # {目标名: [entry, ...]},值为异常则抛
        self._cat_ok = cat_ok
        self._header = header or (lambda ent: None)
        self._solver = solver or (lambda ent, cancel: None)
        self._cached = cached or (lambda ent: None)
        self.applied = None
        self.failed = None
        self.progress = []
        self.solve_calls = []
        self.closed = False
        harness = self

        class _Client:
            host = "192.0.2.225"

            def clone(self):
                return self

            def close(self):
                harness.closed = True

            def listdir(self, share, path):
                name = path.rsplit("\\", 1)[-1]
                got = harness._listing.get(name, [])
                if isinstance(got, Exception):
                    raise got
                return got

        self.client = _Client()
        self.shell = SimpleNamespace(ui=lambda fn, *a: fn(*a))

    # -- 被 _foot_work 调用的钩子
    def _catalog_ok(self):
        return self._cat_ok

    def _cached_wcs(self, host, ent):
        return self._cached(ent)

    def _header_wcs(self, clone, host, ent):
        return self._header(ent)

    def _solve_wcs(self, clone, host, ent, cancel):
        self.solve_calls.append(ent.name)
        return self._solver(ent, cancel)

    def _foot_progress(self, gen, done, total, text):
        self.progress.append((done, total, text))

    def _foot_failed(self, gen, message):
        self.failed = message

    def _apply_footprints(self, gen, foots, cover, note,
                          need_cat=0, cat_ok=True):
        self.applied = SimpleNamespace(foots=foots, cover=cover, note=note,
                                       need_cat=need_cat, cat_ok=cat_ok)

    def run(self, targets, cancel=None):
        import threading

        from astro_smb_gui._sky3d import Sky3DPage

        Sky3DPage._foot_work(self, 1, cancel or threading.Event(),
                             "EMMC Images", targets, self.client)
        return self


def _target(name="M 8", color="#7FD88F", t0=None, hours=3.0):
    t0 = t0 or datetime(2026, 7, 25, 21, 0, 0)
    return {"name": name, "color": color, "log_ra": 270.0, "log_dec": -24.0,
            "ts0": t0.timestamp(), "ts1": (t0 + timedelta(hours=hours)).timestamp()}


def _subs(n, t0=None, folder="Plan\\Light\\M 8"):
    t0 = t0 or datetime(2026, 7, 25, 21, 0, 0)
    return [_entry(_light(t0 + timedelta(minutes=20 * i), i + 1), folder=folder)
            for i in range(n)]


def _hdr_payload(ent):
    from astro_smb_gui._sky3d import _wcs_to_payload

    return _wcs_to_payload(_wcs(270.0, -24.0), WIDTH, HEIGHT,
                           src="hdr", sip=True)


class TestFootWorker:
    def test_happy_path_header_wcs_only(self):
        """真机主路径:ASIAIR light 帧头里就带 WCS,一次板解算都不该发生。"""
        h = _Harness({"M 8": _subs(3), "NGC 7293": _subs(2, folder="x")},
                     header=_hdr_payload).run(
            [_target("M 8"), _target("NGC 7293")])
        assert h.failed is None
        assert h.solve_calls == []
        assert len(h.applied.foots) == 5
        assert h.applied.note == tr("实际视场: {n_ok}/{total} 张", n_ok=5, total=5)
        assert set(h.applied.cover) == {"M 8", "NGC 7293"}
        assert h.applied.cover["M 8"]["n_hdr"] == 3
        assert h.closed is True

    def test_cache_hit_skips_header_read(self):
        from astro_smb_gui._sky3d import _wcs_to_payload

        def boom(ent):
            raise AssertionError("命中缓存后不该再读 FITS 头")

        h = _Harness({"M 8": _subs(3)}, header=boom,
                     cached=lambda ent: _wcs_to_payload(
                         _wcs(270.0, -24.0), WIDTH, HEIGHT, src="hdr")).run(
            [_target()])
        assert len(h.applied.foots) == 3

    def test_no_catalog_degrades_gracefully(self):
        """星表缺失:不解算、不报错,照常回报"需要星表"让 UI 给下载入口。"""
        h = _Harness({"M 8": _subs(4)}, cat_ok=False).run([_target()])
        assert h.failed is None
        assert h.solve_calls == []
        assert h.applied.foots == []
        assert h.applied.need_cat == 4 and h.applied.cat_ok is False
        assert "星表" in h.applied.note

    def test_solve_budget_is_capped_per_run(self):
        from astro_smb_gui._sky3d import MAX_SOLVE_PER_RUN

        n = MAX_SOLVE_PER_RUN + 3
        h = _Harness({"M 8": _subs(n)},
                     solver=lambda ent, cancel: _hdr_payload(ent)).run(
            [_target(hours=6.0)])
        assert len(h.solve_calls) == MAX_SOLVE_PER_RUN
        assert len(h.applied.foots) == MAX_SOLVE_PER_RUN
        assert "排队" in h.applied.note

    def test_cancel_during_solve_is_not_reported_as_error(self):
        """「停止解算」是经 TransferCancelled(SmbClientError 的子类)从下载里
        抛出来的,会先被"这张失败"的分支接住 —— 让整轮**干净退出**(不出结果、
        不弹错)的是循环顶部与循环之后那两句 ``cancel.is_set()``。
        """
        import threading

        from astro_smb.client import TransferCancelled

        cancel = threading.Event()

        def solver(ent, ev):
            ev.set()
            raise TransferCancelled("传输已取消")

        h = _Harness({"M 8": _subs(6)}, solver=solver).run([_target()], cancel)
        assert h.failed is None
        assert h.applied is None            # 取消不产出结果
        assert h.closed is True             # 但连接一定要还回去

    def test_missing_target_dir_does_not_kill_the_batch(self):
        from astro_smb.client import SmbClientError

        h = _Harness({"M 8": SmbClientError("目录不存在"),
                      "NGC 7293": _subs(2, folder="y")},
                     header=_hdr_payload).run(
            [_target("M 8"), _target("NGC 7293")])
        assert h.failed is None
        assert len(h.applied.foots) == 2
        assert set(h.applied.cover) == {"NGC 7293"}

    def test_repeated_header_failures_trip_the_breaker(self):
        from astro_smb.client import SmbClientError

        def boom(ent):
            raise SmbClientError("连接已断开")

        h = _Harness({"M 8": _subs(10)}, header=boom).run([_target(hours=6.0)])
        assert h.failed == "连接已断开"
        assert h.applied is None

    def test_download_breaker_is_not_reset_by_header_reads(self):
        """下载熔断必须有**自己**的计数器。

        读头(几 KB)几乎总是成功,若两条路径共用一个 fails 并在读头成功时清零,
        "连着下不动 50MB 原图"就永远攒不到阈值 —— 用户要干等满
        MAX_SOLVE_PER_RUN 次超时。这条把两个计数器分开的事实钉死。
        """
        from astro_smb.client import SmbClientError

        def solver(ent, cancel):
            raise SmbClientError("读取超时")

        h = _Harness({"M 8": _subs(12)}, solver=solver).run([_target(hours=6.0)])
        assert h.failed == "读取超时"
        assert len(h.solve_calls) == 3          # 熔断阈值, 不是 MAX_SOLVE_PER_RUN
        assert h.applied is None

    def test_no_subs_at_all(self):
        h = _Harness({"M 8": []}).run([_target()])
        assert h.applied is not None
        assert h.applied.foots == [] and h.applied.need_cat == 0
        assert "Plan/Light" in h.applied.note

    def test_failed_solves_are_counted_not_dropped_silently(self):
        from astro_smb_gui._sky3d import FOOT_CACHE_V

        h = _Harness({"M 8": _subs(3)},
                     solver=lambda ent, cancel: {"v": FOOT_CACHE_V, "ok": False,
                                                 "reason": "星点太少"}).run(
            [_target()])
        assert h.applied.foots == []
        assert "失败" in h.applied.note


# ================================================================ Py↔JS 契约


class TestFootPageContract:
    """Python 侧发什么、JS 侧收什么,靠静态扫两个文件对上。

    这两边分处不同语言、不同进程,任何一边改了消息名另一边都不会报错 ——
    只会"开关打开但天上什么都没有"。真机上排查这种静默失配非常费时。
    """

    def _js(self) -> str:
        from astro_smb_gui import webhost

        return (webhost.PKG_WEB_DIR / "sky3d.js").read_text(encoding="utf-8")

    def _py(self) -> str:
        from pathlib import Path

        import astro_smb_gui._sky3d as mod

        return Path(mod.__file__).read_text(encoding="utf-8")

    @pytest.mark.parametrize("kind", ["footprints", "footSelect", "options"])
    def test_js_handles_host_messages(self, kind):
        assert f"case '{kind}':" in self._js()

    def test_python_posts_footprints_message(self):
        py = self._py()
        assert '"type": "footprints"' in py
        assert '"footprints": on' in py       # options 里的显隐开关

    def test_js_sends_footprint_pick(self):
        assert "type: 'footprint'" in self._js()

    def test_python_handles_footprint_pick(self):
        assert 'kind == "footprint"' in self._py()

    def test_js_uses_additive_blending_for_overlap(self):
        """重叠处变亮是本功能的核心表达 —— 换成普通 alpha 混合就没了。"""
        assert "AdditiveBlending" in self._js()

    def test_js_footprint_legend_id_matches_css(self):
        from astro_smb_gui import webhost

        css = (webhost.PKG_WEB_DIR / "sky3d.css").read_text(encoding="utf-8")
        assert "foot-legend" in self._js() and "#foot-legend" in css

    def test_js_does_not_unwrap_ra(self):
        """JS 侧对 ring 只做 dir(ra, dec);任何排序/unwrap 都会毁掉跨 RA=0 的环。"""
        js = self._js()
        assert "ringPoints" in js
        body = js[js.index("function ringPoints"):js.index("function buildFootprint")]
        assert "sort" not in body and "unwrap" not in body

    def test_xaml_has_the_new_controls(self):
        """_find_controls 里 FindName 拿不到就是 None.as_() 崩在构造函数里。"""
        from pathlib import Path

        import astro_smb_gui._sky3d as mod

        xaml = Path(mod.XAML_PATH).read_text(encoding="utf-8")
        for name in ("FootprintToggle", "FootPanel", "FootStatus", "FootBar",
                     "FootCancelBtn", "CatalogPanel", "CatalogText",
                     "CatalogBar", "CatalogBtn", "CoverPanel", "SubPanel",
                     "TimeRangeText"):
            assert f'x:Name="{name}"' in xaml
        assert 'x:Name="QualityPanel"' not in xaml


class TestCoverageHasSingleSource:
    """覆盖类的量只能有**一个**算法。

    此前 `_sky3d._coverage_stats`(格数 × 平面格面积)与 `wcsapps.coverage`
    (gnomonic 面积元 1/(1+ξ²+η²)^1.5 逐格加权)并存,喂同一个 UI 栏位,
    靠人工纪律维持口径一致 —— 离中心几度外就会分道扬镳。同一栏在不同机器上
    悄悄换定义,用户没法比较。
    """

    def test_duplicate_implementation_is_gone(self):
        import astro_smb_gui._sky3d as S3
        assert not hasattr(S3, "_coverage_stats"), (
            "_coverage_stats 是与 wcsapps.coverage 并存的第二套覆盖算法,应已删除")

    def test_local_cover_refuses_to_invent_coverage_numbers(self):
        import astro_smb_gui._sky3d as S3
        foots = [{"wcs": None, "w": 100, "h": 80, "ts": 1000.0, "rot": 10.0,
                  "ra": 10.0, "dec": 20.0},
                 {"wcs": None, "w": 100, "h": 80, "ts": 4600.0, "rot": 11.0,
                  "ra": 10.0, "dec": 20.0}]
        cov = S3._local_cover(foots, {})
        assert cov["source"] == "不可用"
        for key in ("area", "single", "common_area", "common", "keep",
                    "n_gaps", "max_gap"):
            assert cov[key] is None, f"{key} 不该被兜底算法凑出数字"

    def test_wcsapps_independent_fields_still_computed(self):
        """跨度/场旋/中天翻转不依赖 wcsapps,降级时照常给。"""
        import astro_smb_gui._sky3d as S3
        foots = [{"wcs": None, "w": 100, "h": 80, "ts": 1000.0, "rot": 10.0,
                  "ra": 10.0, "dec": 20.0},
                 {"wcs": None, "w": 100, "h": 80, "ts": 8200.0, "rot": 12.0,
                  "ra": 10.0, "dec": 20.0}]
        cov = S3._local_cover(foots, {})
        assert cov["span_h"] == pytest.approx(2.0)
        assert "meridian_flip" in cov and "drift" in cov

    def test_ui_says_unavailable_instead_of_hiding_rows(self):
        import astro_smb_gui._sky3d as S3
        import inspect
        text = inspect.getsource(S3)
        # 文案外面裹着 `_()`(i18n)—— 查的仍然是**这一行**,
        # 不是"整份源码里出现过这几个字"
        assert '_("覆盖统计"), _("不可用")' in text, (
            "算不出来要明说,不能让那几行静默消失 —— 用户分不清"
            "\"没算出来\"和\"没什么可说\"")
        # 判"算没算出来"用的必须是**身份**不是显示文本:`cov["source"]` 会
        # 被翻译,拿它比中文一翻就永远走"算出来了"那一支。
        assert 'cov.get("source") == _COV_NA' in text, (
            "还在拿翻译过的显示文本判断覆盖统计可不可用")
