"""3D 天球页(_sky3d)与 WebView2 宿主(webhost)的离线单测。

只测**纯逻辑**:夜次/目标聚合、坐标口径、语义色阈值、资产目录语义。
WebView2 控件本身要真 XAML 消息泵,不在单测覆盖(见 scratchpad 的探针脚本)。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from astro_smb.autorunlog import (
    AutorunBlock, FrameShot, Night, ShootingGroup, TargetRun,
)
from tests.support import tr

T0 = datetime(2026, 7, 25, 22, 0, 0)


def _run(target: str, ra: str | None, dec: str | None, ftype: str | None,
         n: int, start: datetime, plan: int | None = None) -> TargetRun:
    frames = [FrameShot(time=start + timedelta(seconds=60 * i),
                        image_no=i + 1, exposure="60.0s") for i in range(n)]
    g = ShootingGroup(frame_type=ftype, planned=n, exposure="60.0s",
                      binning="1", start_time=start, frames=frames)
    b = AutorunBlock(target=target, begin_time=start,
                     end_time=start + timedelta(seconds=60 * n),
                     ra=ra, dec=dec, groups=[g])
    return TargetRun(target=target, plan_no=plan, blocks=[b])


def _data(runs: list[TargetRun], date: str = "2026-07-25") -> SimpleNamespace:
    return SimpleNamespace(nights=[Night(date=date, sessions=[], runs=runs)])


class TestSkyRelevant:
    def test_bias_dark_only_excluded(self):
        from astro_smb_gui._sky3d import _sky_relevant

        run = _run("Dark", "0h0m0s", "+89°0'0\"", "dark", 3, T0)
        assert not _sky_relevant(run)

    def test_light_included(self):
        from astro_smb_gui._sky3d import _sky_relevant

        assert _sky_relevant(_run("M 8", "18h3m0s", "-24°23'0\"", "light", 3, T0))

    def test_no_frames_included(self):
        """失败的尝试(无 Shooting 行)仍显示计划位置。"""
        from astro_smb_gui._sky3d import _sky_relevant

        run = TargetRun(target="X", plan_no=None,
                        blocks=[AutorunBlock(target="X", begin_time=T0)])
        assert _sky_relevant(run)


class TestBuildNights:
    def test_log_coords_fallback(self):
        from astro_smb_app.views import sky3d as sv
        from astro_smb_gui._sky3d import _build_nights

        nights = _build_nights(_data([
            _run("M 8", "18h03m00s", "-24°23'00\"", "light", 5, T0)]), {})
        assert len(nights) == 1
        (t,) = nights[0]["targets"]
        assert t["name"] == "M 8" and t["source_key"] == sv.SRC_LOG
        assert t["ra"] == pytest.approx(270.75, abs=0.01)
        assert t["dec"] == pytest.approx(-24.383, abs=0.01)
        assert t["frames"] == 5 and t["exposure"] == pytest.approx(300.0)
        assert nights[0]["frames"] == 5

    def test_fits_coords_win(self):
        """FITS 实测(角秒级)优先于日志 slew 坐标。

        **断言的是 `source_key` 不是 `source`** —— 后者是显示文本,会被翻译;
        而"FITS 坐标有没有盖住 goto 请求值"是**判读**,与语言无关。
        (那两者恒差约 21′,判错了天球上的点会静默退回请求值。)
        """
        from astro_smb_app.views import sky3d as sv
        from astro_smb_gui._sky3d import _build_nights

        run = _run("M 8", "18h03m00s", "-24°23'00\"", "light", 2, T0)
        data = _data([run])
        nights = _build_nights(data, {id(run): (271.5, -24.0)})
        (t,) = nights[0]["targets"]
        assert t["source_key"] == sv.SRC_FITS
        assert t["ra"] == pytest.approx(271.5) and t["dec"] == pytest.approx(-24.0)

    def test_bias_run_dropped_and_colors_cycle(self):
        from astro_smb_gui._sky3d import TARGET_COLORS, _build_nights

        runs = [
            _run("M 8", "18h03m00s", "-24°23'00\"", "light", 2, T0),
            _run("Dark", "0h00m00s", "+89°00'00\"", "dark", 2,
                 T0 + timedelta(hours=1)),
            _run("NGC 7293", "22h29m00s", "-20°50'00\"", "light", 2,
                 T0 + timedelta(hours=2)),
        ]
        nights = _build_nights(_data(runs), {})
        names = [t["name"] for t in nights[0]["targets"]]
        assert names == ["M 8", "NGC 7293"]        # 暗场不上天球
        assert nights[0]["targets"][0]["color"] == TARGET_COLORS[0]
        assert nights[0]["targets"][1]["color"] == TARGET_COLORS[1]

    def test_same_target_merged_across_runs(self):
        """同夜同名目标(跨 Plan / 被 Pause 分裂)合并成一条。"""
        from astro_smb_gui._sky3d import _build_nights

        runs = [_run("M 8", "18h03m00s", "-24°23'00\"", "light", 3, T0, plan=1),
                _run("M 8", "18h03m00s", "-24°23'00\"", "light", 4,
                     T0 + timedelta(hours=2), plan=2)]
        nights = _build_nights(_data(runs), {})
        (t,) = nights[0]["targets"]
        assert t["frames"] == 7
        assert t["plans"] == [1, 2]
        assert t["t0"] == T0
        assert t["t1"] >= T0 + timedelta(hours=2)

    def test_missing_coords_skipped(self):
        from astro_smb_gui._sky3d import _build_nights

        assert _build_nights(_data([_run("X", None, None, "light", 2, T0)]),
                             {}) == []

    def test_span_is_monotonic(self):
        from astro_smb_gui._sky3d import _build_nights

        nights = _build_nights(_data([
            _run("M 8", "18h03m00s", "-24°23'00\"", "light", 1, T0)]), {})
        n = nights[0]
        assert n["ts1"] - n["ts0"] >= 600      # 极短夜次也要给得出可拖动量程

    def test_time_window_tracks_selected_target_actual_span(self):
        from astro_smb_gui._sky3d import _build_nights, _time_window_for

        runs = [
            _run("M 8", "18h03m00s", "-24°23'00\"", "light", 3, T0),
            _run("M 31", "00h42m44s", "+41°16'00\"", "light", 4,
                 T0 + timedelta(hours=3)),
        ]
        night = _build_nights(_data(runs), {})[0]
        target = next(t for t in night["targets"] if t["name"] == "M 31")
        assert _time_window_for(night) == (night["ts0"], night["ts1"])
        assert _time_window_for(night, target) == (
            target["t0"].timestamp(), target["t1"].timestamp())
        assert _time_window_for(night, target) != _time_window_for(night)


class TestFormatting:
    def test_alt_tone_thresholds(self):
        from astro_smb_gui._sky3d import _alt_tone

        assert _alt_tone(None) == "dim"
        assert _alt_tone(-0.1) == "dim"
        assert _alt_tone(0.0) == "warn"
        assert _alt_tone(29.9) == "warn"
        assert _alt_tone(30.0) == "good"

    def test_az_name(self):
        from astro_smb_gui._sky3d import _az_name

        assert _az_name(0.0) == tr("北")
        assert _az_name(359.9) == tr("北")
        assert _az_name(90.0) == tr("东")
        assert _az_name(180.0) == tr("南")
        assert _az_name(270.0) == tr("西")
        assert _az_name(225.0) == tr("西南")

    def test_fmt_dur(self):
        from astro_smb_gui._sky3d import _fmt_dur

        assert _fmt_dur(45) == "45 s"
        assert _fmt_dur(600) == "10 min"
        assert _fmt_dur(7200) == "2.0 h"

    def test_fits_coords(self):
        from astro_smb_gui._sky3d import _fits_coords

        assert _fits_coords({"RA": "271.5", "DEC": "-24.2"}) == (271.5, -24.2)
        assert _fits_coords({"RA": "370.0", "DEC": "-24.2"})[0] == pytest.approx(10.0)
        assert _fits_coords({"RA": "10.0"}) is None
        assert _fits_coords({"RA": "abc", "DEC": "1"}) is None

    def test_glyphs_are_bmp(self):
        """§7.1: 非 BMP 字符会让 HSTRING 末尾少字符, 图标只能用私用区码位。"""
        from astro_smb_gui import _sky3d

        for g in (_sky3d.GLYPH_TARGET, _sky3d.GLYPH_DETAIL):
            assert len(g) == 1 and ord(g) <= 0xFFFF


class TestWebhostAssets:
    def test_three_ready_needs_reasonable_size(self, tmp_path, monkeypatch):
        from astro_smb_gui import webhost

        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        assert not webhost.three_ready()
        webhost.three_path().write_bytes(b"x" * 1024)      # 太小 = 错误页
        assert not webhost.three_ready()
        webhost.three_path().write_bytes(b"x" * (webhost.THREE_MIN_BYTES + 1))
        assert webhost.three_ready()

    def test_survey_url_only_when_present(self, tmp_path, monkeypatch):
        from astro_smb_gui import webhost

        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        assert webhost.survey_asset_url() is None
        (webhost.web_cache_dir() / webhost.SURVEY_ASSET).write_bytes(
            b"x" * (2 << 20))
        assert webhost.survey_asset_url() == (
            f"{webhost.ASSET_ORIGIN}/{webhost.SURVEY_ASSET}")

    def test_copy_static_refreshes_page_files(self, tmp_path, monkeypatch):
        """每次启动都要把包内 html/js 覆盖到缓存目录(便于迭代)。"""
        from astro_smb_gui import webhost

        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        dest = webhost.web_cache_dir()
        stale = dest / "sky3d.js"
        stale.write_text("stale", encoding="utf-8")
        names = webhost._copy_static(dest)
        assert {"sky3d.html", "sky3d.js", "sky3d.css"} <= set(names)
        assert stale.read_text(encoding="utf-8") != "stale"

    def test_page_assets_exist_and_import_three(self):
        """ES module 的相对 import 名必须与 THREE_NAME 一致, 否则页面白屏。"""
        from astro_smb_gui import webhost

        js = (webhost.PKG_WEB_DIR / "sky3d.js").read_text(encoding="utf-8")
        html = (webhost.PKG_WEB_DIR / "sky3d.html").read_text(encoding="utf-8")
        assert f"./{webhost.THREE_NAME}" in js
        assert 'type="module"' in html and "./sky3d.js" in html

    def test_user_data_folder_env(self, tmp_path, monkeypatch):
        from astro_smb_gui import webhost

        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        monkeypatch.delenv("WEBVIEW2_USER_DATA_FOLDER", raising=False)
        webhost.prepare_user_data_folder()
        import os
        folder = os.environ["WEBVIEW2_USER_DATA_FOLDER"]
        assert str(tmp_path) in folder and os.path.isdir(folder)
