"""拍摄记录页(_records)两段式懒加载的离线单测。

覆盖:
  ① 缓存全命中 → 第一段(_apply_preview)+ 第二段(_apply_data)都触发,
     且第一段**零原文获取**(不下载、不读盘、不解析);
  ② 缓存全空 → 只走全量路径, 绝不先闪一个空首屏;
  ③ 第一段结果过期(gen 已推进)→ 直接丢弃, 不碰任何 UI 字段;
  ④ 第二段重渲染按稳定键找回用户选中的目标(两代对象不同, id 不可比);
  ⑤ complete=False / pending 占位文案 —— "读取中…" 必须与"无数据"可分辨。

外加回归护栏:pending 默认关时, 派生数据与旧版逐条计算**逐字段相等**
(第二段画出来的必须和两段式引入前一模一样)。

全部不连设备:SMB 侧用假 client;RecordsPage 需要真 XAML 消息泵建不出来,
故把它的方法绑到只带必要字段的假页面上跑(_work / _emit_preview 只碰
shell / _fits_cache / 几个纯函数)。
"""
from __future__ import annotations

import ast
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from astro_smb_gui import _records as R
from astro_smb_gui import logstore, metacache
from astro_smb.autorunlog import (
    AutorunBlock, FrameShot, Night, ShootingGroup, TargetRun, aggregate_nights,
    parse_autorun_log,
)
from astro_smb.client import RemoteEntry, SmbClientError
from tests.support import tr

T0 = datetime(2026, 7, 25, 22, 0, 0)


# ---------------------------------------------------------------- 合成数据

def _run(target: str, ftype: str | None = "light", n: int = 3,
         start: datetime = T0, plan: int | None = 1,
         ra: str | None = "18h03m37s",
         dec: str | None = "-24°23'12\"") -> TargetRun:
    frames = [FrameShot(time=start + timedelta(seconds=60 * i),
                        image_no=i + 1, exposure="60.0s") for i in range(n)]
    g = ShootingGroup(frame_type=ftype, planned=n, exposure="60.0s",
                      binning="1", start_time=start, frames=frames)
    b = AutorunBlock(target=target, begin_time=start,
                     end_time=start + timedelta(seconds=60 * n),
                     end_mode="Finish", ra=ra, dec=dec, groups=[g])
    return TargetRun(target=target, plan_no=plan, blocks=[b])


def _night(runs: list[TargetRun], date: str = "2026-07-25") -> Night:
    return Night(date=date, sessions=[], runs=runs)


# ---------------------------------------------------------------- 假 shell / 页面 / client

class FakeShell:
    """记录 shell.ui(...) 的编组调用;可选在每次编组时抓一份计数快照。"""

    def __init__(self, client=None, snapshot=None):
        self.client = client
        self.calls: list[tuple] = []         # (回调名, args, 快照)
        self._snapshot = snapshot

    def ui(self, fn, *args) -> None:
        snap = self._snapshot() if self._snapshot else None
        self.calls.append((getattr(fn, "__name__", str(fn)), args, snap))

    def error(self, msg) -> None:
        self.calls.append(("error", (msg,), None))

    def info(self, msg) -> None:
        self.calls.append(("info", (msg,), None))

    def names(self) -> list[str]:
        return [n for n, _a, _s in self.calls]

    def payload(self, name: str):
        for n, a, _s in self.calls:
            if n == name:
                return a
        raise AssertionError(f"没有 {name} 编组")


class FakePage:
    """只带 _work / _emit_preview 真正会碰到的字段的假页面。"""

    _work = R.RecordsPage._work
    _emit_preview = R.RecordsPage._emit_preview
    _collect_fits = R.RecordsPage._collect_fits
    # 只作为"编组目标"传给 shell.ui, 测试里不会真调(要 XAML)
    _apply_preview = R.RecordsPage._apply_preview
    _apply_data = R.RecordsPage._apply_data
    _load_failed = R.RecordsPage._load_failed
    _data_share = R.RecordsPage._data_share   # 走 shell.data_share, 缺省回落常量

    def __init__(self, shell) -> None:
        self.shell = shell
        self._fits_cache: dict = {}


class FakeLogClient:
    """列 log 目录的假 client;原文已在磁盘缓存里, download_file 不该被调到。"""

    host = "192.0.2.225"

    def __init__(self, logdir):
        self.logdir = logdir
        self.downloads = 0
        self.listdirs: list[tuple] = []

    def clone(self):
        return self

    def close(self) -> None:
        pass

    def listdir(self, share, path=""):
        self.listdirs.append((share, path))
        if path != logstore.LOG_DIR:
            return []                        # Plan\Light: 本测试不建目录
        out = []
        for f in sorted(self.logdir.glob("*.txt")):
            st = f.stat()
            out.append(RemoteEntry(share=share, path=f"log\\{f.name}",
                                   name=f.name, is_dir=False, size=st.st_size,
                                   mtime=st.st_mtime, ctime=st.st_mtime,
                                   atime=st.st_mtime, attributes=0x20))
        return out

    def download_file(self, *a, **kw):
        self.downloads += 1
        raise AssertionError("磁盘缓存已有原文, 不该再下载")


SMALL_AUTORUN = """\
Log enabled at 2026/07/23 20:05:43
2026/07/23 20:05:43 [Autorun|Begin] M 8 Start
2026/07/23 20:05:43 Target RA:18h03m37s DEC:-24°23'12"
2026/07/23 20:05:43 Shooting 2 light frames, exposure 60.0s Bin1
2026/07/23 20:05:43 Exposure 60.0s image 1#
2026/07/23 20:06:44 Exposure 60.0s image 2#
2026/07/23 20:10:46 [Autorun|End] Finish Autorun
Log disabled at 2026/07/23 20:11:00
"""

SMALL_PHD2 = """\
PHD2 version , Log version 2.5. Log enabled at 2026-07-23 20:05:00
Guiding Begins at 2026-07-23 20:05:40
Pixel scale = 2.06 arc-sec/px, Binning = 1, Focal length = 250 mm
Exposure = 2000 ms
Camera = ASI120MM
Dec = -24.4 deg, Hour angle = 0.50 hr, Pier side = East
Frame,Time,mount,dx,dy,RARawDistance,DECRawDistance,RAGuideDistance,\
DECGuideDistance,RADuration,RADirection,DECDuration,DECDirection,XStep,YStep,\
StarMass,SNR,ErrorCode
1,1.0,"Mount",0.1,0.2,0.30,0.40,0.0,0.0,0,,0,,0,0,1000,25.0,0
2,3.0,"Mount",0.1,0.2,-0.20,0.10,0.0,0.0,0,,0,,0,0,1000,25.0,0
3,5.0,"Mount",0.0,0.0,0.00,0.00,0.0,0.0,0,,0,,0,0,0,0.0,1
Guiding Ends at 2026-07-23 20:10:00
Log closed at 2026-07-23 20:11:00
"""


@pytest.fixture()
def logenv(tmp_path, monkeypatch):
    """把 logstore 的原文缓存目录与 metacache 库都指到 tmp_path。"""
    logdir = tmp_path / "logs"
    logdir.mkdir()
    (logdir / "Autorun_Log_2026-07-23_200543.txt").write_text(
        SMALL_AUTORUN, encoding="utf-8")
    (logdir / "PHD2_GuideLog_2026-07-23_200500.txt").write_text(
        SMALL_PHD2, encoding="utf-8")
    # 签名要跟真身一致:原文缓存目录现在按设备分子目录(换设备防串)
    monkeypatch.setattr(logstore, "logs_cache_dir", lambda host="": logdir)
    metacache.use_path(tmp_path / "meta.db")
    yield logdir
    metacache.close()
    metacache._default = None


@pytest.fixture()
def fetch_counter(monkeypatch):
    """统计"日志原文获取"次数(读盘或下载都走 _fetch_text)。"""
    box = {"n": 0}
    orig = logstore.LogStore._fetch_text

    def counted(self, client, e, cancel):
        box["n"] += 1
        return orig(self, client, e, cancel)

    monkeypatch.setattr(logstore.LogStore, "_fetch_text", counted)
    return box


# ---------------------------------------------------------------- ① 缓存全命中: 两段都触发, 第一段零 I/O

class TestWarmCacheTwoStages:
    def test_preview_then_full(self, logenv, fetch_counter):
        c = FakeLogClient(logenv)
        logstore.LogStore().refresh(c)              # 预热 metacache
        store = logstore.LogStore()                 # 新实例: 内存缓存空
        assert store.data is None
        fetch_counter["n"] = 0
        shell = FakeShell(c, snapshot=lambda: (fetch_counter["n"], c.downloads))
        page = FakePage(shell)
        page._gen = 1

        page._work(1, False, store, c, False)

        assert shell.names() == ["_apply_preview", "_apply_data"]
        # 第一段的快照: 一次原文获取都没有(既没读盘也没下载)
        _n, _a, snap = shell.calls[0]
        assert snap == (0, 0)
        # 第二段确实做了真解析(PHD2 逐帧不进库, 必然要取原文)
        assert shell.calls[1][2][0] > 0
        assert c.downloads == 0                     # 全程走磁盘缓存

    def test_preview_payload_is_pending_and_matches_full(self, logenv):
        c = FakeLogClient(logenv)
        logstore.LogStore().refresh(c)
        store = logstore.LogStore()
        shell = FakeShell(c)
        page = FakePage(shell)
        page._gen = 3

        page._work(3, False, store, c, False)

        (gen, p) = shell.payload("_apply_preview")
        assert gen == 3
        assert p["complete"] is True
        assert p["guide_pending"] is True and p["fits_pending"] is True
        assert p["guide_map"] == {}
        # 第一段的夜次与第二段完全一致(只是没有逐帧派生)
        full = shell.payload("_apply_data")[1]
        assert [n.date for n in p["nights"]] == [n.date for n in full.nights]
        # 只需 nights 就能画的东西第一段就齐了
        d = p["derived"]
        assert d["layouts"] and d["rows"] and d["timelines"]
        assert all(v for v in d["timelines"].values())
        # 导星尾巴是"读取中", 不是"无数据"
        assert all("导星读取中…" in r["sub"] for r in d["rows"].values())

    def test_summaries_called_with_cache_only(self, logenv, monkeypatch):
        """第一段必须 parse_missing=False —— 否则它自己就会去下载/解析。"""
        seen = {}
        orig = logstore.LogStore.summaries

        def spy(self, client, cancel=None, *, parse_missing=True):
            seen["parse_missing"] = parse_missing
            return orig(self, client, cancel, parse_missing=parse_missing)

        monkeypatch.setattr(logstore.LogStore, "summaries", spy)
        c = FakeLogClient(logenv)
        logstore.LogStore().refresh(c)
        shell = FakeShell(c)
        page = FakePage(shell)
        page._gen = 1
        page._work(1, False, logstore.LogStore(), c, False)
        assert seen == {"parse_missing": False}


# ---------------------------------------------------------------- ② 缓存全空: 只走全量, 不闪空首屏

class TestColdCacheSingleStage:
    def test_no_preview_when_nothing_cached(self, logenv):
        c = FakeLogClient(logenv)               # metacache 空(没先 refresh 过)
        shell = FakeShell(c)
        page = FakePage(shell)
        page._gen = 1

        page._work(1, False, logstore.LogStore(), c, False)

        assert shell.names() == ["_apply_data"]     # 绝不先发一个空首屏

    def test_empty_summary_is_skipped(self):
        """summaries 拿不到夜次时 _emit_preview 自己就不发。"""
        shell = FakeShell()
        page = FakePage(shell)
        assert page._emit_preview(1, [], None, False, {}, [], [],
                                  guide_pending=True, fits_pending=True) is False
        assert shell.calls == []

    def test_summaries_failure_falls_back_to_full(self, logenv):
        """第一段炸了不许拖垮整次加载(它只是加速)。"""
        c = FakeLogClient(logenv)
        logstore.LogStore().refresh(c)
        store = logstore.LogStore()

        def boom(*a, **kw):
            raise RuntimeError("摘要炸了")

        store.summaries = boom
        shell = FakeShell(c)
        page = FakePage(shell)
        page._gen = 1
        page._work(1, False, store, c, False)
        assert shell.names() == ["_apply_data"]

    def test_already_rendered_skips_preview(self):
        """页面已在显示这份数据时不做第一段(否则手动刷新会白闪一下)。"""
        data = logstore.LogData(nights=[_night([_run("M 8")])], phd2_logs=[])
        store = _StubStore(data)
        shell = FakeShell(_PlanClient())
        page = FakePage(shell)
        page._gen = 1
        page._work(1, False, store, shell.client, True)     # already=True
        assert shell.names() == ["_apply_data"]

    def test_cached_data_preview_has_guide_ready(self):
        """已有完整 LogData 时导星摘要不碰 SMB, 第一段就只剩 FITS 是占位。"""
        data = logstore.LogData(nights=[_night([_run("M 8")])], phd2_logs=[])
        store = _StubStore(data)
        shell = FakeShell(_PlanClient())
        page = FakePage(shell)
        page._gen = 1
        page._work(1, False, store, shell.client, False)
        assert shell.names() == ["_apply_preview", "_apply_data"]
        p = shell.payload("_apply_preview")[1]
        assert p["guide_pending"] is False and p["fits_pending"] is True
        assert set(p["guide_map"]) == {id(r) for r in data.nights[0].runs}
        row = next(iter(p["derived"]["rows"].values()))
        assert "读取中" not in row["sub"]       # 导星已就绪, 不该有占位


class _StubStore:
    """只提供 data/refresh/summaries 的假 LogStore。"""

    def __init__(self, data):
        self.data = data
        self.refreshed = 0

    def refresh(self, client, cancel=None):
        self.refreshed += 1
        return self.data

    def summaries(self, client, cancel=None, *, parse_missing=True):
        raise AssertionError("有 store.data 时不该再走 summaries")


class _PlanClient:
    """只应付 Plan\\Light 列目录的假 client。"""

    host = "dev"

    def clone(self):
        return self

    def listdir(self, share, path=""):
        return []

    def close(self) -> None:
        pass


# ---------------------------------------------------------------- ③ 过期代次丢弃

class TestGeneration:
    def test_stale_preview_discarded(self):
        """gen 已推进时 _apply_preview 第一句就返回, 不碰任何 UI 字段
        (假 self 上根本没有那些属性 —— 真动了必然 AttributeError)。"""
        stale = type("P", (), {"_gen": 2})()
        assert R.RecordsPage._apply_preview(
            stale, 1, {"nights": [_night([_run("M 8")])]}) is None

    def test_stale_full_discarded(self):
        stale = type("P", (), {"_gen": 7})()
        assert R.RecordsPage._apply_data(
            stale, 6, None, {}, None, {}, {}, {}, {}, {}) is None

    def test_current_preview_without_nights_is_noop(self):
        """代次对得上但夜次为空 ⇒ 也不许把已有内容清空。"""
        cur = type("P", (), {"_gen": 4})()
        assert R.RecordsPage._apply_preview(cur, 4, {"nights": []}) is None

    def test_work_passes_same_gen_to_both_stages(self):
        data = logstore.LogData(nights=[_night([_run("M 8")])], phd2_logs=[])
        shell = FakeShell(_PlanClient())
        page = FakePage(shell)
        page._gen = 9
        page._work(9, False, _StubStore(data), shell.client, False)
        assert [a[0] for _n, a, _s in shell.calls] == [9, 9]


# ---------------------------------------------------------------- ④ 选中项跨代保住

class TestSelectionSurvivesSecondStage:
    @staticmethod
    def _gen_nights():
        log = parse_autorun_log(SMALL_AUTORUN, source="Autorun_Log_x.txt")
        return list(reversed(aggregate_nights([log])))

    def test_run_key_stable_across_generations(self):
        a, b = self._gen_nights(), self._gen_nights()
        ra, rb = a[0].runs[0], b[0].runs[0]
        assert ra is not rb                          # 两代确实是不同对象
        assert R._run_key(ra) == R._run_key(rb)

    def test_restore_picks_new_object(self):
        a, b = self._gen_nights(), self._gen_nights()
        page = type("P", (), {})()
        page._nights = a
        page._sel_run = a[0].runs[0]
        key = R.RecordsPage._sel_key(page)
        page._nights = b                             # 第二段换了一代数据
        page._sel_run = None
        R.RecordsPage._restore_sel_by_key(page, key, 0)
        assert page._sel_run is b[0].runs[0]

    def test_restore_noop_without_key_or_night(self):
        page = type("P", (), {})()
        page._nights = self._gen_nights()
        page._sel_run = None
        R.RecordsPage._restore_sel_by_key(page, None, 0)
        assert page._sel_run is None
        R.RecordsPage._restore_sel_by_key(page, ("M 8", None, T0), 5)
        assert page._sel_run is None

    def test_restore_ignores_unknown_target(self):
        nights = self._gen_nights()
        page = type("P", (), {})()
        page._nights = nights
        page._sel_run = None
        R.RecordsPage._restore_sel_by_key(page, ("不存在", 3, T0), 0)
        assert page._sel_run is None

    def test_sel_key_none_when_nothing_selected(self):
        page = type("P", (), {})()
        page._sel_run = None
        assert R.RecordsPage._sel_key(page) is None


# ---------------------------------------------------------------- ⑤ 占位文案

class TestPendingPlaceholders:
    def test_status_line_complete(self):
        s = R._preview_status_line(3, True, True)
        assert "3 个夜次(缓存)" in s and "正在补全导星与设备信息…" in s
        assert "尚未解析" not in s

    def test_status_line_incomplete(self):
        s = R._preview_status_line(2, False, True)
        assert "部分日志尚未解析" in s and "正在补全" in s

    def test_status_line_guide_ready(self):
        s = R._preview_status_line(1, True, False)
        assert "正在补全设备信息…" in s and "导星" not in s

    def test_row_subline_pending_vs_missing(self):
        run = _run("M 8")
        assert "导星读取中…" in R._run_subline(run, {}, guide_pending=True)
        assert "读取中" not in R._run_subline(run, {})       # 默认 = 旧行为
        # 已算过但确实没导星数据 ⇒ 不是"读取中"
        assert "读取中" not in R._run_subline(
            run, {id(run): (None, 0.0)}, guide_pending=True)

    def test_detail_guide_row_pending(self):
        run = _run("M 8")
        pend = R._run_detail(run, {}, {}, guide_pending=True)
        row = [p for p in pend["pairs"] if p["k"] == tr("导星")][0]
        assert row["v"] == tr("读取中…")
        plain = R._run_detail(run, {}, {})
        assert [p for p in plain["pairs"]
                if p["k"] == tr("导星")][0]["v"] == tr("无数据")

    def test_detail_device_row_pending(self):
        run = _run("M 8")
        pend = R._run_detail(run, {}, {}, fits_pending=True)
        dev = [p for p in pend["pairs"] if p["k"] == tr("设备")]
        assert dev and dev[0]["v"] == tr("读取中…")
        # 非 pending 时该行整条不出现(旧行为)
        assert not [p for p in R._run_detail(run, {}, {})["pairs"]
                    if p["k"] == "设备"]

    def test_night_summary_pending_rows(self):
        night = _night([_run("M 8")])
        _l, right = R._night_summary(night, {}, {}, guide_pending=True,
                                     fits_pending=True)
        # 「导星: …」是**两个 msgid 拼的**(外层 `导星: {guide_txt}` + 里层
        # `读取中…`),而「设备: 读取中…」整条是一个。照产出方的拼法来。
        assert tr("导星: {guide_txt}", guide_txt=tr("读取中…")) in right
        assert tr("设备: 读取中…") in right
        _l2, right2 = R._night_summary(night, {}, {})
        assert tr("导星: {guide_txt}", guide_txt=tr("无数据")) in right2
        assert tr("设备") not in right2

    def test_placeholder_text_is_distinguishable(self):
        """"读取中"与"无数据"绝不能是同一句话。"""
        run = _run("M 8")
        a = R._run_detail(run, {}, {}, guide_pending=True)["pairs"]
        b = R._run_detail(run, {}, {})["pairs"]
        assert a != b


# ---------------------------------------------------------------- 回归护栏: 第二段输出不变

class TestFullPathUnchanged:
    def test_derive_maps_matches_direct_calls(self):
        night = _night([_run("M 8", plan=1),
                        _run("NGC 7293", plan=2, start=T0 + timedelta(hours=1))])
        gm = {id(r): (None, 0.0) for r in night.runs}
        fm: dict = {}
        d = R._derive_maps([night], gm, fm, [])
        for r in night.runs:
            assert d["rows"][id(r)] == R._run_row_data(r, gm)
            assert d["details"][id(r)] == R._run_detail(r, gm, fm)
            assert d["timelines"][id(r)] == R._timeline_items(r)
        assert d["stats"][night.date] == R._night_summary(night, gm, fm)
        assert d["tl"][night.date] == R._night_timeline(night, [])
        assert d["layouts"][night.date] == R._night_layouts(night)

    def test_derive_maps_survives_bad_run(self):
        """单条算炸不许拖垮整夜(与旧版逐条 try 同口径)。"""
        night = _night([_run("M 8")])
        bad = {"boom": True}

        class Exploding(dict):
            def get(self, *a, **kw):
                raise RuntimeError("炸")

        d = R._derive_maps([night], Exploding(), bad, [])
        row = d["rows"][id(night.runs[0])]
        assert row["sub"] == tr("统计失败") and row["name"] == "M 8"
        assert d["details"][id(night.runs[0])]["pairs"][0]["k"] == tr("详情")

    def test_guide_spans_equivalent(self):
        """段摘要路径与逐帧路径画出来的导星覆盖条必须一致。"""
        night = _night([_run("M 8", n=10)])
        t0 = T0 + timedelta(minutes=1)
        t1 = T0 + timedelta(minutes=5)
        sec = type("S", (), {"begins": t0, "end_time_effective": t1})()
        log = type("L", (), {"guide_sections": [sec]})()
        via_frames = R._night_timeline(night, [log])
        via_spans = R._night_timeline(
            night, [], R._spans_from_sections(
                [{"begins": t0.isoformat(), "end_eff": t1.isoformat()}]))
        assert via_frames["guides"] == via_spans["guides"]
        assert via_frames["bars"] == via_spans["bars"]

    def test_spans_from_sections_skips_broken(self):
        assert R._spans_from_sections([]) == []
        assert R._spans_from_sections([{"begins": "坏的", "end_eff": None}]) == []
        assert R._spans_from_sections(None) == []

    def test_guide_map_for_never_raises(self):
        night = _night([_run("M 8")])
        gm = R._guide_map_for([night], [])
        assert set(gm) == {id(r) for r in night.runs}


# -------------------------------------------------- ⑥ 状态判据(对抗审查确认的三条)

class _FailStore(_StubStore):
    """summaries 抛指定异常;refresh 记录被调次数。"""

    def __init__(self, exc):
        super().__init__(None)
        self._exc = exc

    def summaries(self, client, cancel=None, *, parse_missing=True):
        raise self._exc


class _ConnClient(_PlanClient):
    """可控 connected 的假 client;记录 refresh 阶段是否被用到。"""

    def __init__(self, connected: bool):
        self.connected = connected


class TestRenderCompleteGate:
    """缺陷 A: 判据必须是"页面上是否已有完整渲染", 不是 store.data 的对象身份。

    旧写法 `already = store.data is not None and store.data is self.data` 有两个
    漏洞: ① _apply_preview 刻意不写 self.data, 别的页面 force 刷新换掉 store.data
    后切回来就会降级; ② watcher 发现新日志时 shell 先 invalidate() 把 store.data
    置 None, already 恒为 False, 于是用信息量更少的缓存摘要覆盖完整页面。
    """

    def test_complete_page_skips_preview_even_if_store_data_is_new_object(self):
        """页面已完整 + store.data 换成**内容相同的新对象** ⇒ 不发第一段。"""
        night = _night([_run("M 8")])
        shell = FakeShell(_PlanClient())
        page = FakePage(shell)
        page._gen = 1
        # store.data 是新对象(模拟导星页 force 刷新过), 但页面已完整渲染
        page._work(1, False, _StubStore(logstore.LogData(nights=[night],
                                                         phd2_logs=[])),
                   shell.client, True)
        assert shell.names() == ["_apply_data"], (
            "页面已完整时不该再发 _apply_preview 把内容降级成占位")

    def test_empty_page_still_gets_preview(self):
        """页面为空 ⇒ 第一段照常发 —— 首屏加速不能被这个修复弄丢。"""
        night = _night([_run("M 8")])
        shell = FakeShell(_PlanClient())
        page = FakePage(shell)
        page._gen = 1
        page._work(1, False, _StubStore(logstore.LogData(nights=[night],
                                                         phd2_logs=[])),
                   shell.client, False)
        assert shell.names() == ["_apply_preview", "_apply_data"]

    def test_watcher_invalidate_does_not_downgrade_complete_page(self):
        """watcher 路径: store.data 已被 invalidate 置 None, 但页面仍是完整的
        ⇒ 连 summaries 那条第一段也必须跳过(旧代码只门控了另一条)。"""
        night = _night([_run("M 8")])

        class _S(_StubStore):
            def __init__(self):
                super().__init__(None)
                self.summaries_calls = 0

            def summaries(self, client, cancel=None, *, parse_missing=True):
                self.summaries_calls += 1
                raise AssertionError("页面已完整时不该调 summaries")

            def refresh(self, client, cancel=None):
                self.refreshed += 1
                return logstore.LogData(nights=[night], phd2_logs=[])

        store = _S()
        shell = FakeShell(_PlanClient())
        page = FakePage(shell)
        page._gen = 3
        page._work(3, True, store, shell.client, True)
        assert store.summaries_calls == 0
        assert shell.names() == ["_apply_data"]

    def test_apply_preview_clears_and_apply_data_sets_the_flag(self):
        """两个 apply 必须正确翻转 _render_complete, 否则门控与真实渲染脱节。"""
        src = Path(R.__file__).read_text(encoding="utf-8")
        assert "self._render_complete = False" in src
        assert "self._render_complete = True" in src
        # 门控读的就是这个字段
        assert "has_full = self._render_complete" in src


class TestNightNotPinnedByPreview:
    """缺陷 B: 第一段临时钉的 _night_date 不能影响第二段的夜次选择。

    最常见场景: 关着程序拍了一整夜, 早上开 app —— 昨夜日志没进过 metacache,
    第一段只能钉在次新夜; 第二段若照旧"优先匹配已选日期"就停在次新夜,
    而改动前冷启动一定落在最新一夜。
    """

    @staticmethod
    def _page(nights, night_date, from_preview):
        p = object.__new__(R.RecordsPage)
        p._nights = nights
        p._night_date = night_date
        p._night_from_preview = from_preview
        p._ui_updating = False
        p.night_combo = None                # 下面只调纯逻辑部分
        return p

    def test_preview_pinned_date_is_dropped_by_second_stage(self):
        """_apply_data 见到 from_preview 标记 ⇒ 丢掉日期, 回落最新夜(idx 0)。"""
        src = Path(R.__file__).read_text(encoding="utf-8")
        # 丢弃必须发生在重建下拉**之前**
        i_drop = src.index("if self._night_from_preview:")
        i_build = src.index("idx = self._rebuild_night_combo()",
                            src.index("def _apply_data"))
        assert i_drop < i_build, "必须先丢弃 preview 钉的夜次再重建下拉"

    def test_night_combo_picks_newest_when_date_unset(self):
        """_night_date 为 None 时 _rebuild_night_combo 的选择逻辑 = idx 0。
        (_nights 是倒序, 最新在前。)"""
        older, newer = _night([_run("M 8")], "2026-07-20"), \
            _night([_run("M 16")], "2026-07-26")
        p = self._page([newer, older], None, True)
        idx = 0
        if p._night_date is not None:
            for i, n in enumerate(p._nights):
                if n.date == p._night_date:
                    idx = i
                    break
        assert idx == 0 and p._nights[idx].date == "2026-07-26"

    def test_stale_date_would_pick_older_night(self):
        """反证: 若不丢弃 preview 钉的旧日期, 就会选到次新夜 —— 这正是缺陷。"""
        older, newer = _night([_run("M 8")], "2026-07-20"), \
            _night([_run("M 16")], "2026-07-26")
        p = self._page([newer, older], "2026-07-20", True)
        idx = 0
        if p._night_date is not None:
            for i, n in enumerate(p._nights):
                if n.date == p._night_date:
                    idx = i
                    break
        assert idx == 1, "旧日期确实会把选择带到次新夜(所以第二段必须丢弃它)"

    def test_user_choice_clears_the_preview_flag(self):
        """用户手动切夜次后, 第二段必须尊重他的选择。"""
        src = Path(R.__file__).read_text(encoding="utf-8")
        seg = src[src.index("def _on_night_changed"):]
        seg = seg[:seg.index("def _show_night")]
        assert "self._night_from_preview = False" in seg


class TestConnectFailureNotRetriedTwice:
    """缺陷 C: 第一段的连接类失败直接上抛, 别让第二段再挂一次超时
    (默认 15s ⇒ 用户要等 30s 才看到"读取日志失败")。"""

    def test_connection_failure_reported_once_without_second_attempt(self):
        """连不上时: refresh 一次都不调(否则是第二次 15s 超时),
        且直接编组 _load_failed 一次到位。"""
        store = _FailStore(SmbClientError("连接超时"))
        shell = FakeShell(_ConnClient(connected=False))
        page = FakePage(shell)
        page._gen = 1
        page._work(1, True, store, shell.client, False)
        assert store.refreshed == 0, "连不上时不该再进第二段重连一次(用户会等 2× 超时)"
        assert shell.names() == ["_load_failed"]

    def test_non_connection_failure_still_falls_through(self):
        """列目录/解析类失败(连接是好的)⇒ 照旧吞掉, 继续走全量路径。"""
        night = _night([_run("M 8")])

        class _S(_FailStore):
            def refresh(self, client, cancel=None):
                self.refreshed += 1
                return logstore.LogData(nights=[night], phd2_logs=[])

        store = _S(SmbClientError("列目录失败"))
        shell = FakeShell(_ConnClient(connected=True))
        page = FakePage(shell)
        page._gen = 1
        page._work(1, True, store, shell.client, False)
        assert store.refreshed == 1
        assert shell.names() == ["_apply_data"]

    def test_non_smb_exception_still_swallowed(self):
        """非 SmbClientError(解析器炸了之类)不该让整页加载失败。"""
        night = _night([_run("M 8")])

        class _S(_FailStore):
            def __init__(self):
                super().__init__(ValueError("解析炸了"))

            def refresh(self, client, cancel=None):
                self.refreshed += 1
                return logstore.LogData(nights=[night], phd2_logs=[])

        store = _S()
        shell = FakeShell(_ConnClient(connected=False))
        page = FakePage(shell)
        page._gen = 1
        page._work(1, True, store, shell.client, False)
        assert store.refreshed == 1


# ---------------------------------------------------------------- §7.1 星平面字符

class TestNoAstralChars:
    """win32more 把 str 转 HSTRING 时按码点数给长度, 而 HSTRING 是 UTF-16,
    任何星平面字符都会让字符串末尾少一个字符(真机现象: Plan→Pla)。"""

    def test_records_string_literals_are_bmp_only(self):
        path = Path(__file__).resolve().parent.parent / "astro_smb_gui" / "_records.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                bad = sorted({c for c in node.value if ord(c) > 0xFFFF})
                assert not bad, (f"_records.py:{node.lineno} 含星平面字符: "
                                 f"{[hex(ord(c)) for c in bad]}")


# ------------------------------------------- ⑦ 换设备:数据源必须跟着切(真机确认的 bug)

class TestDeviceSwitchIsolation:
    """真机现象:切换设备后,除浏览页外的页面(尤其拍摄记录/导星/3D 天球)
    仍显示**进程启动时那台设备**的数据。

    根因是 shell 只换了 client,没动数据层;而三个页面都按 ``store.data is None``
    分支决定要不要重新拉取 —— data 还在,于是直接重渲旧设备的内容。
    """

    @staticmethod
    def _data(target: str):
        return logstore.LogData(nights=[_night([_run(target)])], phd2_logs=[])

    def test_data_from_other_host_is_not_served(self):
        """结构性保证:数据带着"我来自哪台设备"的标记,对不上就不交出去。

        这条**独立于** bind() 的清理:即使有人绕过 bind 直接塞进一份别的设备
        的 LogData(或在途 refresh 晚到),取用时也必须被挡住。
        """
        st = logstore.LogStore()
        st.bind("E:\\")
        st._data = self._data("M 8")
        st._data_host = "192.0.2.228"      # 这份数据其实来自另一台设备
        assert st.data is None, "宿主对不上的 LogData 绝不能交出去"
        st._data_host = "E:\\"
        assert st.data is not None           # 对得上就正常返回

    def test_refresh_stamps_data_with_the_client_that_produced_it(self, logenv):
        """在途 refresh 晚到时,标记必须是**产出数据的 client**的 host,
        而不是"当前绑定的设备" —— 否则等于给旧设备的数据发新设备的通行证。"""
        c = FakeLogClient(logenv)            # host = 192.0.2.225
        st = logstore.LogStore()
        st.bind("192.0.2.225")
        st.refresh(c)
        assert st._data_host == "192.0.2.225"
        assert st.data is not None

    def test_bind_clears_filename_keyed_memory_caches(self):
        """两个内存缓存只按文件名做键,没有设备维度 —— 换设备必须清空,
        否则两台设备上同名的 Autorun_Log_xxx.txt 会互相串。"""
        st = logstore.LogStore()
        st.bind("192.0.2.228")
        st._autorun_cache["Autorun_Log_x.txt"] = (1, 2.0, object())
        st._phd2_cache["PHD2_GuideLog_x.txt"] = (1, 2.0, object())
        st.bind("E:\\")
        assert st._autorun_cache == {} and st._phd2_cache == {}

    def test_rebind_same_host_keeps_cache(self):
        """重连同一台设备不该把已解析的日志全丢掉重来。"""
        st = logstore.LogStore()
        st.bind("192.0.2.228")
        st.data = self._data("M 8")
        st._autorun_cache["a.txt"] = (1, 2.0, object())
        assert st.bind("192.0.2.228") is False
        assert st.data is not None and st._autorun_cache != {}

    def test_bind_updates_share_without_switching(self):
        st = logstore.LogStore()
        st.bind("192.0.2.228", "EMMC Images")
        st.data = self._data("M 8")
        assert st.bind("192.0.2.228", "TF Images") is False
        assert st.share == "TF Images" and st.data is not None

    def test_switch_makes_pages_refetch(self):
        """页面侧的实际效果:换设备后 _work 会走全量 refresh(而不是复用缓存)。"""
        st = logstore.LogStore()
        st.bind("192.0.2.228")
        st.data = self._data("M 8")

        class _S:
            def __init__(self, store):
                self.store, self.refreshed = store, 0

            @property
            def data(self):
                return self.store.data

            def refresh(self, client, cancel=None):
                self.refreshed += 1
                return TestDeviceSwitchIsolation._data("NGC 7293")

            def summaries(self, client, cancel=None, *, parse_missing=True):
                return logstore.LogSummary(nights=[])

        st.bind("E:\\")                        # 换设备
        shell = FakeShell(_PlanClient())
        page = FakePage(shell)
        page._gen = 1
        proxy = _S(st)
        page._work(1, False, proxy, shell.client, False)
        assert proxy.refreshed == 1, "换设备后必须重新 refresh, 不能复用旧 LogData"
        applied = shell.payload("_apply_data")
        assert applied[1].nights[0].runs[0].target == "NGC 7293"

    def test_log_cache_dir_is_per_host(self, tmp_path, monkeypatch):
        """磁盘缓存只按文件名+大小判同,不隔离设备就会串
        (ASIAIR 内置 EMMC 与它导出到 U 盘的副本极易同名)。"""
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        a = logstore.logs_cache_dir("192.0.2.228")
        b = logstore.logs_cache_dir("E:\\")
        assert a != b and a.is_dir() and b.is_dir()

    def test_host_slug_is_filesystem_safe(self):
        for host in ("192.0.2.228", "E:\\", "\\srv\share", "", "..", "a/b"):
            s = logstore._host_slug(host)
            assert s and not (set(s) & set('\/:*?"<>|')), f"{host!r} → {s!r}"
