"""metacache(SQLite 元数据缓存)与其两处接入(FITS 头 / 日志解析)的离线单测。

全部不连设备:SMB 侧用假 client;真机样例日志存在于 .tmp/ 时追加对账测试。
"""
from __future__ import annotations
import sqlite3

import os
import shutil
import threading
import time

import pytest

from astro_smb_gui import logstore, metacache
from astro_smb_gui.preview import read_fits_header
from astro_smb.autorunlog import AutorunLog, parse_autorun_log
from astro_smb.client import RemoteEntry
from astro_smb.fitshdr import BLOCK

TMP = os.path.join(os.path.dirname(__file__), "..", ".tmp")


def _reset_default_cache() -> None:
    """把进程级默认实例还原成"未初始化",避免测试用的 tmp 库泄漏到后续用例。"""
    metacache.close()
    metacache._default = None


@pytest.fixture()
def mc(tmp_path):
    """每个测试一个独立库文件。"""
    c = metacache.MetaCache(tmp_path / "meta.db")
    yield c
    c.close()


# ---------------------------------------------------------------- 基本读写

class TestBasics:
    def test_miss_then_hit(self, mc):
        assert mc.get("k", "dev", "a") is None
        mc.put("k", "dev", "a", {"n": 1, "s": "中文"})
        assert mc.get("k", "dev", "a") == {"n": 1, "s": "中文"}

    def test_overwrite(self, mc):
        mc.put("k", "dev", "a", {"n": 1})
        mc.put("k", "dev", "a", {"n": 2})
        assert mc.get("k", "dev", "a") == {"n": 2}
        assert mc.stats() == {"k": 1}

    def test_backend_isolated(self, mc):
        """换设备天然隔离:同 kind 同 key,不同 backend 互不串味。"""
        mc.put("k", "devA", "a", {"n": 1})
        mc.put("k", "devB", "a", {"n": 2})
        assert mc.get("k", "devA", "a") == {"n": 1}
        assert mc.get("k", "devB", "a") == {"n": 2}

    def test_stats_by_kind(self, mc):
        mc.put("k1", "d", "a", {})
        mc.put("k1", "d", "b", {})
        mc.put("k2", "d", "a", {})
        assert mc.stats() == {"k1": 2, "k2": 1}

    def test_unserializable_payload_is_silent(self, mc):
        mc.put("k", "d", "a", {"bad": object()})     # 不能抛
        assert mc.get("k", "d", "a") is None


# ---------------------------------------------------------------- 失效机制

class TestInvalidation:
    def test_src_size_change(self, mc):
        mc.put("k", "d", "a", {"n": 1}, src_size=100, src_mtime=5.0)
        assert mc.get("k", "d", "a", src_size=100, src_mtime=5.0) == {"n": 1}
        assert mc.get("k", "d", "a", src_size=101, src_mtime=5.0) is None

    def test_src_mtime_change(self, mc):
        mc.put("k", "d", "a", {"n": 1}, src_size=100, src_mtime=5.0)
        assert mc.get("k", "d", "a", src_size=100, src_mtime=6.0) is None

    def test_stale_row_is_dropped(self, mc):
        """失配的行顺手删掉,库不会一直长。"""
        mc.put("k", "d", "a", {"n": 1}, src_size=100, src_mtime=5.0)
        mc.get("k", "d", "a", src_size=999, src_mtime=5.0)
        assert mc.stats() == {}

    def test_no_fingerprint_means_no_check(self, mc):
        """调用方不给源指纹 = 不校验(内容寻址型 key 用)。"""
        mc.put("k", "d", "a", {"n": 1}, src_size=100, src_mtime=5.0)
        assert mc.get("k", "d", "a") == {"n": 1}

    def test_ttl_expiry(self, mc):
        mc.put("k", "d", "a", {"n": 1})
        assert mc.get("k", "d", "a", ttl=60.0) == {"n": 1}
        time.sleep(0.02)
        assert mc.get("k", "d", "a", ttl=0.01) is None
        assert mc.stats() == {}          # 过期行同样被清掉

    def test_invalidate_dimensions(self, mc):
        for kind in ("k1", "k2"):
            for backend in ("d1", "d2"):
                for key in ("a", "b"):
                    mc.put(kind, backend, key, {})
        assert mc.invalidate(kind="k1", backend="d1", key="a") == 1
        assert mc.invalidate(kind="k1", backend="d1") == 1
        assert mc.invalidate(kind="k1") == 2
        assert mc.stats() == {"k2": 4}
        assert mc.invalidate() == 4
        assert mc.stats() == {}

    def test_prune(self, mc):
        for i in range(5):
            mc.put("k", "d", str(i), {"i": i})
        assert mc.prune("k", max_rows=2) == 3
        assert mc.stats() == {"k": 2}
        assert mc.prune("k", max_age_s=-1) == 2
        assert mc.stats() == {}

    def test_vacuum_if_large_evicts_half(self, mc):
        for i in range(40):
            mc.put("k", "d", str(i), {"pad": "x" * 200})
        mc.vacuum_if_large(max_mb=0)     # 阈值 0 ⇒ 一定触发
        assert mc.stats()["k"] == 20

    def test_vacuum_noop_when_small(self, mc):
        mc.put("k", "d", "a", {})
        mc.vacuum_if_large(max_mb=64)
        assert mc.stats() == {"k": 1}


# ---------------------------------------------------------------- 并发 / 健壮性

class TestConcurrency:
    def test_parallel_put_get(self, mc):
        """多工作线程并发读写同一个库(真实场景:预览线程 + 懒加载线程 + 记录页)。"""
        errors: list[BaseException] = []

        def worker(n: int) -> None:
            try:
                for i in range(60):
                    mc.put("conc", "d", f"{n}-{i}", {"n": n, "i": i})
                    assert mc.get("conc", "d", f"{n}-{i}") == {"n": n, "i": i}
            except BaseException as ex:      # noqa: BLE001
                errors.append(ex)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert mc.stats() == {"conc": 8 * 60}
        assert mc.errors == 0

    def test_corrupt_db_self_heals(self, tmp_path):
        path = tmp_path / "meta.db"
        c = metacache.MetaCache(path)
        c.put("k", "d", "a", {"n": 1})
        c.close()
        for suffix in ("-wal", "-shm"):     # 只留主库文件,再写成垃圾
            p = tmp_path / ("meta.db" + suffix)
            if p.exists():
                p.unlink()
        path.write_bytes(b"definitely not a sqlite database" * 200)

        c2 = metacache.MetaCache(path)
        assert c2.get("k", "d", "a") is None          # 降级为未命中,不抛
        c2.put("k", "d", "a", {"n": 2})               # 自愈后可继续用
        assert c2.get("k", "d", "a") == {"n": 2}
        assert c2.stats() == {"k": 1}
        c2.close()

    def test_unwritable_path_degrades_silently(self, tmp_path):
        """库建不出来(路径是个目录)时全面降级为"无缓存",绝不能抛。"""
        d = tmp_path / "adir"
        d.mkdir()
        c = metacache.MetaCache(d)
        assert c.get("k", "d", "a") is None
        c.put("k", "d", "a", {"n": 1})
        assert c.get("k", "d", "a") is None
        assert c.stats() == {}
        c.invalidate()
        c.vacuum_if_large(0)
        c.close()


# ---------------------------------------------------------------- dataclass 编解码

class TestDataclassCodec:
    def test_roundtrip_equal(self):
        log = parse_autorun_log(SMALL_AUTORUN, source="x.txt")
        back = metacache.dc_decode(AutorunLog, metacache.dc_encode(log))
        assert back == log                       # dataclass 逐字段严格相等

    def test_roundtrip_through_json(self, mc):
        import json
        log = parse_autorun_log(SMALL_AUTORUN, source="x.txt")
        payload = json.loads(json.dumps(metacache.dc_encode(log)))
        assert metacache.dc_decode(AutorunLog, payload) == log

    def test_schema_sig_stable_and_sensitive(self):
        from dataclasses import dataclass

        sig = metacache.dc_schema_sig(AutorunLog)
        assert sig == metacache.dc_schema_sig(AutorunLog)
        assert len(sig) == 12

        @dataclass
        class A:
            x: int

        @dataclass
        class B:
            x: int
            y: str

        assert metacache.dc_schema_sig(A) != metacache.dc_schema_sig(B)

    def test_decode_tolerates_missing_field(self):
        """payload 缺可选字段时用默认值,不炸。"""
        log = metacache.dc_decode(AutorunLog, {"source": "s.txt"})
        assert log.source == "s.txt" and log.sessions == []


SMALL_AUTORUN = """\
Log enabled at 2026/07/23 20:05:43
2026/07/23 20:05:43 [Autorun|Begin] M 8 Start
2026/07/23 20:05:43 Target RA:18h03m37s DEC:-24°23'12"
2026/07/23 20:05:43 Shooting 2 bias frames, exposure 1.0ms Bin1
2026/07/23 20:05:43 Exposure 1.0ms image 1#
2026/07/23 20:05:44 Exposure 1.0ms image 2#
2026/07/23 20:05:45 [AutoFocus|Begin] exposure 2.0s temperature 21.5℃
2026/07/23 20:06:10 Auto focus succeeded, the focused position is 12345
2026/07/23 20:06:10 [AutoFocus|End] Success
2026/07/23 20:10:46 [Autorun|End] Finish Autorun
Log disabled at 2026/07/23 20:11:00
"""


# ---------------------------------------------------------------- 接入 1:FITS 头

def _fits_blob() -> bytes:
    def card(k: str, v) -> str:
        return (k.ljust(8) + "= " + str(v).ljust(70))[:80]

    cards = [card("SIMPLE", "T"), card("BITPIX", 16), card("NAXIS", 2),
             card("NAXIS1", 6248), card("NAXIS2", 4176),
             card("EXPTIME", 180.0), card("GAIN", 100),
             card("OBJECT", "'M 8     '"), "END".ljust(80)]
    return "".join(cards).ljust(BLOCK).encode("ascii")


class CountingFitsClient:
    """只实现 read_bytes 的假 client,统计 SMB 往返次数。"""

    def __init__(self, blob: bytes, host: str = "192.0.2.225") -> None:
        self.blob = blob
        self.host = host
        self.reads = 0

    def read_bytes(self, share, path, offset, size):
        self.reads += 1
        return self.blob[offset:offset + size]


def _fit_entry(size: int, mtime: float = 1700000000.5) -> RemoteEntry:
    return RemoteEntry(share="EMMC Images", path="Autorun\\Bias\\a.fit",
                       name="a.fit", is_dir=False, size=size, mtime=mtime,
                       ctime=0.0, atime=0.0, attributes=0x20)


class TestFitsHeaderCache:
    @pytest.fixture(autouse=True)
    def _isolated_db(self, tmp_path):
        metacache.use_path(tmp_path / "meta.db")
        yield
        _reset_default_cache()

    def test_second_read_costs_zero_smb(self):
        blob = _fits_blob()
        c = CountingFitsClient(blob)
        e = _fit_entry(len(blob))
        h1 = read_fits_header(c, e)
        assert c.reads == 1 and h1.complete and h1.cards["OBJECT"] == "M 8"
        h2 = read_fits_header(c, e)
        assert c.reads == 1                     # 命中缓存,零往返
        assert (h2.cards, h2.order, h2.complete, h2.header_bytes) == \
               (h1.cards, h1.order, h1.complete, h1.header_bytes)
        assert h2.naxis == (6248, 4176) and h2.bitpix == 16

    def test_mtime_change_rereads(self):
        blob = _fits_blob()
        c = CountingFitsClient(blob)
        read_fits_header(c, _fit_entry(len(blob), mtime=1.0))
        read_fits_header(c, _fit_entry(len(blob), mtime=2.0))
        assert c.reads == 2

    def test_use_cache_false_always_reads(self):
        blob = _fits_blob()
        c = CountingFitsClient(blob)
        e = _fit_entry(len(blob))
        read_fits_header(c, e)
        read_fits_header(c, e, use_cache=False)
        assert c.reads == 2

    def test_backend_isolation(self):
        blob = _fits_blob()
        c = CountingFitsClient(blob)
        e = _fit_entry(len(blob))
        read_fits_header(c, e, backend_id="dev-A")
        read_fits_header(c, e, backend_id="dev-B")
        assert c.reads == 2
        read_fits_header(c, e, backend_id="dev-A")
        assert c.reads == 2

    def test_cache_failure_does_not_break_read(self, monkeypatch):
        """缓存层整体挂掉时读头必须照常(只是退化成每次都走 SMB)。"""
        def boom(*a, **kw):
            raise RuntimeError("缓存炸了")

        monkeypatch.setattr(metacache, "get", boom)
        monkeypatch.setattr(metacache, "put", boom)
        blob = _fits_blob()
        c = CountingFitsClient(blob)
        e = _fit_entry(len(blob))
        assert read_fits_header(c, e).cards["OBJECT"] == "M 8"
        assert read_fits_header(c, e).cards["OBJECT"] == "M 8"
        assert c.reads == 2

    def test_incomplete_header_not_cached(self):
        """没读到 END 的头是截断/非 FITS,缓存下来会以讹传讹。"""
        blob = b"SIMPLE  = " + b" " * 70 + b"\x00" * 100
        c = CountingFitsClient(blob)
        e = _fit_entry(len(blob))
        read_fits_header(c, e)
        n = c.reads
        read_fits_header(c, e)
        assert c.reads > n
        assert metacache.stats().get("fitshdr", 0) == 0


# ---------------------------------------------------------------- 接入 2:日志

class FakeLogClient:
    """列 log 目录的假 client;文件已在"磁盘缓存"里,download_file 不该被调到。"""

    host = "192.0.2.225"

    def __init__(self, logdir):
        self.logdir = logdir
        self.downloads = 0

    def listdir(self, share, path=""):
        out = []
        for f in sorted(self.logdir.glob("*.txt")):
            st = f.stat()
            out.append(RemoteEntry(share=share, path=f"log\\{f.name}", name=f.name,
                                   is_dir=False, size=st.st_size, mtime=st.st_mtime,
                                   ctime=st.st_mtime, atime=st.st_mtime,
                                   attributes=0x20))
        return out

    def download_file(self, *a, **kw):
        self.downloads += 1
        raise AssertionError("磁盘缓存已有原文, 不该再下载")


@pytest.fixture()
def logenv(tmp_path, monkeypatch):
    """把 logstore 的原文缓存目录与 metacache 库都指到 tmp_path。"""
    logdir = tmp_path / "logs"
    logdir.mkdir()
    monkeypatch.setattr(logstore, "logs_cache_dir", lambda host="": logdir)
    metacache.use_path(tmp_path / "meta.db")
    yield logdir
    _reset_default_cache()


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


class TestLogstoreCache:
    def _seed(self, logdir):
        (logdir / "Autorun_Log_2026-07-23_200543.txt").write_text(
            SMALL_AUTORUN, encoding="utf-8")
        (logdir / "PHD2_GuideLog_2026-07-23_200500.txt").write_text(
            SMALL_PHD2, encoding="utf-8")
        return FakeLogClient(logdir)

    def test_refresh_identical_cold_and_warm(self, logenv):
        c = self._seed(logenv)
        cold = logstore.LogStore().refresh(c)
        warm = logstore.LogStore().refresh(c)      # 新实例 ⇒ 内存缓存空
        assert c.downloads == 0
        assert cold.autorun_logs == warm.autorun_logs   # metacache 往返等价
        assert cold.phd2_sections == warm.phd2_sections
        assert [n.date for n in cold.nights] == [n.date for n in warm.nights]
        assert metacache.stats().get(logstore.AUTORUN_KIND) == 1
        assert metacache.stats().get(logstore.PHD2SUM_KIND) == 1

    def test_section_summary_has_no_frames(self, logenv):
        """逐帧数组绝不进库。"""
        c = self._seed(logenv)
        data = logstore.LogStore().refresh(c)
        (sec,) = data.phd2_sections
        assert "frames" not in sec
        assert sec["n_frames"] == 3 and sec["n_lost"] == 1
        assert sec["pixel_scale"] == 2.06 and sec["hour_angle_hr"] == 0.5
        rms = logstore.section_rms_stats(sec)
        assert rms is not None and rms.n_frames == 2 and rms.n_lost == 1
        assert logstore.section_begins(sec).minute == 5

    def test_summaries_uses_cache_only(self, logenv):
        c = self._seed(logenv)
        full = logstore.LogStore().refresh(c)
        # 缓存已热:严格只用缓存也能出完整结果,且完全不碰原文
        sm = logstore.LogStore().summaries(c, parse_missing=False)
        assert sm.complete
        assert sm.phd2_sections == full.phd2_sections
        assert [n.date for n in sm.nights] == [n.date for n in full.nights]
        assert sm.lon_estimate == full.lon_estimate

    def test_summaries_incomplete_without_cache(self, logenv):
        c = self._seed(logenv)
        sm = logstore.LogStore().summaries(c, parse_missing=False)
        assert not sm.complete and sm.nights == [] and sm.phd2_sections == []

    def test_source_change_invalidates(self, logenv):
        c = self._seed(logenv)
        logstore.LogStore().refresh(c)
        p = logenv / "Autorun_Log_2026-07-23_200543.txt"
        p.write_text(SMALL_AUTORUN.replace("M 8", "NGC 6334"), encoding="utf-8")
        os.utime(p, (time.time() + 30, time.time() + 30))
        data = logstore.LogStore().refresh(c)
        targets = {r.target for n in data.nights for r in n.runs}
        assert targets == {"NGC 6334"}

    def test_schema_sig_guards_stale_payload(self, logenv):
        """结构指纹拼在 kind 里 ⇒ dataclass 改字段后旧 payload 自动全未命中。"""
        c = self._seed(logenv)
        logstore.LogStore().refresh(c)
        assert metacache.stats().get(logstore.AUTORUN_KIND) == 1
        assert metacache.get("autorunlog/deadbeef0000", c.host,
                             "Autorun_Log_2026-07-23_200543.txt") is None

    def test_guide_summary_cached_result_matches(self, logenv):
        c = self._seed(logenv)
        data = logstore.LogStore().refresh(c)
        runs = [r for n in data.nights for r in n.runs]
        assert runs
        cold = [logstore.guide_summary_for_run(r, data.phd2_logs) for r in runs]
        warm = [logstore.guide_summary_for_run(r, data.phd2_logs) for r in runs]
        raw = [logstore.guide_summary_for_run(r, data.phd2_logs, use_cache=False)
               for r in runs]
        assert cold == warm == raw

    def test_guide_summary_key_follows_content(self, logenv):
        """导星日志一变,内容寻址的 key 就变 —— 不可能读到脏数据。"""
        c = self._seed(logenv)
        data = logstore.LogStore().refresh(c)
        fp1 = logstore._phd2_fingerprint(data.phd2_logs)
        data.phd2_logs[0].guide_sections[0].frames.pop()
        assert logstore._phd2_fingerprint(data.phd2_logs) != fp1

    def test_cache_failure_does_not_break_refresh(self, logenv, monkeypatch):
        """缓存层整体挂掉时功能必须照常。"""
        c = self._seed(logenv)
        baseline = logstore.LogStore().refresh(c)

        def boom(*a, **kw):
            raise RuntimeError("缓存炸了")

        monkeypatch.setattr(metacache, "get", boom)
        monkeypatch.setattr(metacache, "put", boom)
        with pytest.raises(RuntimeError):
            metacache.get("x", "y", "z")            # 确认打桩生效
        data = logstore.LogStore().refresh(c)
        assert data.autorun_logs == baseline.autorun_logs


# ---------------------------------------------------------------- 真机样例对账

_SAMPLES = [f for f in (os.listdir(TMP) if os.path.isdir(TMP) else [])
            if f.startswith("Autorun_Log_") and f.endswith(".txt")
            and "_CHN" not in f]


@pytest.mark.skipif(not _SAMPLES, reason="需要 .tmp/ 下的真机样例日志")
def test_real_autorun_logs_roundtrip_exactly(tmp_path):
    """真机日志整棵树 JSON 往返必须逐字段相等 —— 这是敢把解析产物进库的前提。"""
    for name in _SAMPLES:
        with open(os.path.join(TMP, name), encoding="utf-8-sig",
                  errors="replace") as fh:
            log = parse_autorun_log(fh.read(), source=name)
        assert metacache.dc_decode(AutorunLog, metacache.dc_encode(log)) == log


@pytest.mark.skipif(not _SAMPLES, reason="需要 .tmp/ 下的真机样例日志")
def test_real_logs_refresh_cold_equals_warm(tmp_path, monkeypatch):
    logdir = tmp_path / "logs"
    logdir.mkdir()
    for f in os.listdir(TMP):
        if f.endswith(".txt") and "_CHN" not in f:
            shutil.copy2(os.path.join(TMP, f), logdir / f)
    monkeypatch.setattr(logstore, "logs_cache_dir", lambda host="": logdir)
    metacache.use_path(tmp_path / "meta.db")
    try:
        c = FakeLogClient(logdir)
        cold = logstore.LogStore().refresh(c)
        warm = logstore.LogStore().refresh(c)
        assert cold.autorun_logs == warm.autorun_logs
        assert cold.phd2_sections == warm.phd2_sections
        assert cold.lon_estimate == warm.lon_estimate
        assert c.downloads == 0
    finally:
        _reset_default_cache()


# ------------------------------------------------ 真机反馈:无缓存启动模式

class TestNoCacheEnv:
    """ASTRO_SMB_GUI_NOCACHE=1:在**不删除用户缓存目录**的前提下复现冷启动路径。
    删缓存既麻烦又会破坏真实使用数据(用户真机调试反馈)。"""

    def test_bypass_off_by_default(self, monkeypatch):
        monkeypatch.delenv("ASTRO_SMB_GUI_NOCACHE", raising=False)
        assert metacache.bypass_reads() is False

    @pytest.mark.parametrize("val,want", [
        ("1", True), ("true", True), ("yes", True),
        ("0", False), ("", False), ("   ", False),
    ])
    def test_bypass_parsing(self, monkeypatch, val, want):
        monkeypatch.setenv("ASTRO_SMB_GUI_NOCACHE", val)
        assert metacache.bypass_reads() is want

    def test_get_returns_none_when_bypassed(self, monkeypatch, tmp_path):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        monkeypatch.delenv("ASTRO_SMB_GUI_NOCACHE", raising=False)
        metacache.put("k", "dev", "key1", {"v": 1})
        assert metacache.get("k", "dev", "key1") == {"v": 1}
        monkeypatch.setenv("ASTRO_SMB_GUI_NOCACHE", "1")
        assert metacache.get("k", "dev", "key1") is None, "绕过时必须当未命中"

    def test_put_still_writes_when_bypassed(self, monkeypatch, tmp_path):
        """只绕过**读**:写照旧,这样一次无缓存跑完之后缓存是热的,
        下一次不带该变量启动就能直接对比冷/热差异。"""
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        monkeypatch.setenv("ASTRO_SMB_GUI_NOCACHE", "1")
        metacache.put("k", "dev", "key2", {"v": 2})
        monkeypatch.delenv("ASTRO_SMB_GUI_NOCACHE")
        assert metacache.get("k", "dev", "key2") == {"v": 2}


# ------------------------------- 审查确认【高】:暂时性失败不能删掉用户的缓存库

class _ExecProxy:
    """包住真连接,只在 execute 上注入异常(Connection.execute 是只读属性,
    monkeypatch 不上去)。"""

    def __init__(self, conn, exc=None, fail=True):
        self._conn, self._exc, self.fail = conn, exc, fail

    def execute(self, *a, **kw):
        if self.fail and self._exc is not None:
            raise self._exc
        return self._conn.execute(*a, **kw)

    def __getattr__(self, k):
        return getattr(self._conn, k)


class TestOperationalErrorNeverWipes:
    """`_connect` 早就把 OperationalError 单列并注释「绝不能因此删掉用户的
    缓存库」,但四个操作路径都写的是 `except sqlite3.DatabaseError: _rebuild()`,
    而 OperationalError ⊂ DatabaseError,_rebuild = 删 meta.db/-wal/-shm。

    审查实测:双实例撞锁时 put() 阻塞 10.9s 后确实进了 _rebuild —— 那次库没被
    删纯属另一进程还开着句柄、Windows unlink 失败被吞掉。
    """

    @staticmethod
    def _cache(tmp_path):
        from astro_smb_gui.metacache import MetaCache
        mc = MetaCache(tmp_path / "m.db")
        mc.put("k", "dev", "a", {"v": 1})
        assert mc.get("k", "dev", "a") == {"v": 1}
        return mc

    def test_operational_error_is_subclass_of_database_error(self):
        """这条不变量是整个 bug 的前提,钉死它免得将来误判。"""
        assert issubclass(sqlite3.OperationalError, sqlite3.DatabaseError)

    def test_locked_db_does_not_wipe(self, tmp_path, monkeypatch):
        mc = self._cache(tmp_path)
        wiped = {"n": 0}
        monkeypatch.setattr(mc, "_wipe",
                            lambda: wiped.__setitem__("n", wiped["n"] + 1))
        mc._conn = _ExecProxy(mc._conn, sqlite3.OperationalError("database is locked"))
        mc.put("k", "dev", "b", {"v": 2})       # 不许炸
        assert mc.get("k", "dev", "b") is None  # 降级为"没有缓存"
        assert wiped["n"] == 0, "暂时性失败绝不能删库"

    def test_data_survives_transient_failure(self, tmp_path):
        """撞锁之后恢复正常,原有数据必须还在。"""
        mc = self._cache(tmp_path)
        proxy = _ExecProxy(mc._conn, sqlite3.OperationalError("database is locked"))
        mc._conn = proxy
        assert mc.get("k", "dev", "a") is None
        proxy.fail = False
        assert mc.get("k", "dev", "a") == {"v": 1}, "库被删了就取不回来了"

    def test_real_corruption_still_rebuilds(self, tmp_path, monkeypatch):
        """真损坏(DatabaseError 但不是 OperationalError)仍要自愈。"""
        mc = self._cache(tmp_path)
        rebuilt = {"n": 0}

        def fake_rebuild():
            rebuilt["n"] += 1
            return None

        monkeypatch.setattr(mc, "_rebuild", fake_rebuild)
        mc._conn = _ExecProxy(mc._conn, sqlite3.DatabaseError("file is not a database"))
        mc.get("k", "dev", "a")
        assert rebuilt["n"] == 1, "真损坏必须还能自愈"

    def test_invalidate_paths_also_guarded(self, tmp_path, monkeypatch):
        mc = self._cache(tmp_path)
        wiped = {"n": 0}
        monkeypatch.setattr(mc, "_wipe",
                            lambda: wiped.__setitem__("n", wiped["n"] + 1))
        mc._conn = _ExecProxy(mc._conn, sqlite3.OperationalError("database is locked"))
        assert mc.invalidate("k") == 0
        assert wiped["n"] == 0
