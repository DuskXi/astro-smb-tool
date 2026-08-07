"""新功能的离线单测:卷容量、扩展名排序/分类、扫描网段、传输冲突、并行下载 .part 原子性。"""

from pathlib import Path
import time

import pytest

from astro_smb.client import (
    AstroSmbClient, RemoteEntry, VolumeInfo, DirStat, SmbClientError,
)
from tests.support import tr


def _mk(name, is_dir=False, size=0, path=None):
    return RemoteEntry(share="EMMC Images", path=path or name, name=name,
                       is_dir=is_dir, size=size, mtime=0, ctime=0, atime=0,
                       attributes=0x10 if is_dir else 0x20)


class TestDirTree:
    """dir_tree 的聚合逻辑(monkeypatch walk, 不连设备)。"""

    def test_bottom_up_aggregation(self, monkeypatch):
        client = AstroSmbClient(host="0.0.0.0")
        layout = {
            "top": ([_mk("A", is_dir=True, path="top\\A"),
                     _mk("B", is_dir=True, path="top\\B")],
                    [_mk("r.txt", size=10, path="top\\r.txt")]),
            "top\\A": ([_mk("C", is_dir=True, path="top\\A\\C")],
                       [_mk("a1.fit", size=100, path="top\\A\\a1.fit")]),
            "top\\A\\C": ([], [_mk("c1.fit", size=7, path="top\\A\\C\\c1.fit")]),
            "top\\B": ([], [_mk("b1.fit", size=50, path="top\\B\\b1.fit"),
                            _mk("b2.fit", size=5, path="top\\B\\b2.fit")]),
        }

        def fake_walk(share, top="", max_depth=None, on_error=None,
                      depth_first=False, cancel=None):
            order = ["top", "top\\A", "top\\B", "top\\A\\C"]  # BFS 序
            for p in order:
                dirs, files = layout[p]
                yield p, dirs, files

        monkeypatch.setattr(client, "walk", fake_walk)
        root = client.dir_tree("EMMC Images", "top")
        assert root.size == 172 and root.file_count == 5
        # 子节点按大小降序: A(107) > B(55) > r.txt(10)
        assert [c.name for c in root.children] == ["A", "B", "r.txt"]
        a = root.children[0]
        assert a.size == 107 and a.file_count == 2
        assert a.children[0].name == "a1.fit"       # 100 > C(7)
        assert a.children[1].size == 7 and a.children[1].is_dir


class TestVolumeInfo:
    def test_used_percent(self):
        vi = VolumeInfo(total=100, free=25)
        assert vi.used == 75
        assert vi.percent == 75.0

    def test_zero_total(self):
        vi = VolumeInfo(total=0, free=0)
        assert vi.percent == 0.0
        assert vi.used == 0


class TestSortAndCategory:
    def test_ext_category(self):
        from astro_smb_gui._common import ext_category
        assert ext_category(_mk("a.fit")) == tr("图像")
        assert ext_category(_mk("a_thn.jpg")) == tr("缩略图/图片")
        assert ext_category(_mk("log.txt")) == tr("文本/日志")
        assert ext_category(_mk("d", is_dir=True)) == tr("文件夹")
        # 这一条是**按扩展名现拼的**(进不了词表),只有"…文件"那半截可翻
        assert ext_category(_mk("x.xyz")) == tr("{ext} 文件", ext="XYZ")
        assert ext_category(_mk("noext")) == tr("无扩展名")

    def test_sort_by_extension_clusters(self):
        from astro_smb_gui._common import sorted_entries
        entries = [_mk("b.jpg"), _mk("a.fit"), _mk("c.fit"), _mk("d.jpg")]
        # idx 6 = 扩展名/类型
        out = sorted_entries(entries, 6)
        exts = [e.name.split(".")[1] for e in out]
        # 同扩展名聚在一起
        assert exts == ["fit", "fit", "jpg", "jpg"]

    def test_dirs_before_files(self):
        from astro_smb_gui._common import sorted_entries
        entries = [_mk("z.fit"), _mk("adir", is_dir=True)]
        out = sorted_entries(entries, 0)
        assert out[0].is_dir
        assert not out[1].is_dir

    def test_unique_local(self, tmp_path):
        from astro_smb_gui._common import unique_local
        used = set()
        p1 = unique_local(tmp_path, "M31.fit", used)
        assert p1.name == "M31.fit"
        # 内存里已占用 -> 改名
        p2 = unique_local(tmp_path, "M31.fit", used)
        assert p2.name == "M31 (1).fit"
        # 磁盘已存在 -> 也改名
        (tmp_path / "x.fit").write_bytes(b"x")
        used2 = set()
        p3 = unique_local(tmp_path, "x.fit", used2)
        assert p3.name == "x (1).fit"


class TestScanSubnet:
    def test_subnet_of(self):
        from astro_smb_gui._scan import _subnet_of
        assert _subnet_of("192.0.2.225") == "192.0.2"
        # publish-scan: ok(要的就是"第一段不是 192" —— 换成文档网段就和上一行同形了)
        assert _subnet_of("10.0.0.5") == "10.0.0"
        assert _subnet_of("not-an-ip") is None
        assert _subnet_of("1.2.3") is None

    def test_local_subnets_prefers_private(self):
        from astro_smb_gui._scan import _local_subnets
        subs = _local_subnets()
        assert isinstance(subs, list) and subs
        # 基准测试网段 198.18/APIPA 不应排在私有网段前面
        # (只验证返回非空且第一个不是 127./169.254)
        assert not subs[0].startswith(("127.", "169.254"))


class TestConflictPolicy:
    def test_rename_policy(self, tmp_path):
        from astro_smb_gui.transfers import TransferManager, TransferJob, CONFLICT_RENAME
        tm = TransferManager(client_factory=lambda: None, on_update=lambda j: None,
                             conflict=CONFLICT_RENAME)
        target = tmp_path / "a.fit"
        target.write_bytes(b"x")
        job = TransferJob(kind="download", label="a.fit")
        resolved = tm._resolve_conflict(target, job)
        assert resolved is not None
        assert resolved.name == "a (1).fit"
        tm.shutdown()

    def test_skip_policy(self, tmp_path):
        from astro_smb_gui.transfers import TransferManager, TransferJob, CONFLICT_SKIP, SKIPPED
        tm = TransferManager(client_factory=lambda: None, on_update=lambda j: None,
                             conflict=CONFLICT_SKIP)
        target = tmp_path / "a.fit"
        target.write_bytes(b"x")
        job = TransferJob(kind="download", label="a.fit")
        resolved = tm._resolve_conflict(target, job)
        assert resolved is None
        assert job.status == SKIPPED
        tm.shutdown()

    def test_retryable_detection(self):
        from astro_smb_gui.transfers import TransferManager
        from astro_smb.client import SmbClientError
        assert TransferManager._is_retryable(SmbClientError("连接 X 中断: reset"))
        # **判结构化标志,不搜消息文本。** 拿翻译过的消息去搜翻译过的关键词,
        # 中文下碰巧成立,换语言就不成立 —— 而症状只是"连接错误不再重试"。
        assert TransferManager._is_retryable(
            SmbClientError("随便什么消息", retryable=True))
        assert not TransferManager._is_retryable(SmbClientError("目标已存在"))
        # ASCII 兜底(没经过 `_run` 的那些路径)仍然管用
        assert TransferManager._is_retryable(SmbClientError("socket timeout"))
        assert not TransferManager._is_retryable(SmbClientError("访问被拒绝"))


class _StubRangeClient:
    """模拟 AstroSmbClient 的分块下载接口(仅 ParallelDownloader 用到的方法)。"""

    def __init__(self, data: bytes, fail: bool = False):
        self._data = data
        self._fail = fail

    def connect(self):
        pass

    def close(self):
        pass

    def reconnect(self):
        pass

    def download_range(self, share, path, offset, length, fh, cancel=None, on_bytes=None):
        if self._fail:
            # 措辞避开 _is_conn_error 的关键字,让块级重试立即放弃
            raise SmbClientError("访问被拒绝(模拟)")
        fh.seek(offset)
        fh.write(self._data[offset:offset + length])
        if on_bytes:
            on_bytes(length)


class TestParallelPartFile:
    """并行下载必须先写 .part 再原子改名:失败/取消绝不能在最终路径留下
    大小==全长但内容有空洞的文件(会被 download_file(resume=True) 误判已完成)。"""

    TOTAL = (2 << 20) + 12345  # 跨 3 个 1MiB 块,末块非整块

    def _data(self):
        import string
        raw = string.ascii_letters.encode()
        return (raw * (self.TOTAL // len(raw) + 1))[:self.TOTAL]

    def _pd(self, data, fail=False):
        from astro_smb.parallel import ParallelDownloader
        return ParallelDownloader(lambda: _StubRangeClient(data, fail=fail), workers=2)

    def test_success_atomic_replace(self, tmp_path):
        data = self._data()
        target = tmp_path / "big.fit"
        target.write_bytes(b"stale")  # 覆盖模式:旧文件在成功前应保持原样
        res = self._pd(data).download("S", "big.fit", target, self.TOTAL)
        assert target.read_bytes() == data
        assert not (tmp_path / "big.fit.part").exists()
        assert res.n_chunks >= 2

    def test_failure_leaves_no_final_file(self, tmp_path):
        target = tmp_path / "big.fit"
        with pytest.raises(SmbClientError):
            self._pd(self._data(), fail=True).download("S", "big.fit", target, self.TOTAL)
        assert not target.exists()
        assert not (tmp_path / "big.fit.part").exists()

    def test_failure_preserves_preexisting_target(self, tmp_path):
        # 旧行为会把目标文件 truncate 成全长空洞文件;现在失败不得触碰它
        target = tmp_path / "big.fit"
        target.write_bytes(b"old-content")
        with pytest.raises(SmbClientError):
            self._pd(self._data(), fail=True).download("S", "big.fit", target, self.TOTAL)
        assert target.read_bytes() == b"old-content"

    def test_zero_byte_file(self, tmp_path):
        target = tmp_path / "empty.txt"
        res = self._pd(b"").download("S", "empty.txt", target, 0)
        assert target.exists() and target.stat().st_size == 0
        assert not (tmp_path / "empty.txt.part").exists()
        assert res.n_chunks == 0


# ------------------------------------------ B组:本地设备的传输/文案差异

class TestLocalDeviceTransfers:
    """本地卡(直插)不该走分块并发:分块的全部价值是掩盖 SMB 的单流 RTT 瓶颈
    (实测单流 6 MiB/s、8 并发才 9.6),而本地盘顺序读就有 1.48 GB/s。"""

    class _Backend:
        def __init__(self, local: bool):
            self.is_local = local
            self.host = "E:\\" if local else "192.0.2.228"

        def clone(self):
            return self

        def connect(self):
            pass

        def close(self):
            pass

        def download_file(self, *a, **kw):
            pass

    @staticmethod
    def _mgr(local: bool):
        from astro_smb_gui import transfers as T
        return T.TransferManager(
            client_factory=lambda: TestLocalDeviceTransfers._Backend(local),
            on_update=lambda job: None), T

    def test_local_backend_marked_on_job(self):
        mgr, T = self._mgr(True)
        job = T.TransferJob(kind="download", label="x")
        mgr._bind_factory(job)
        assert job.local_device is True and job.host == "E:\\"

    def test_smb_backend_not_marked(self):
        mgr, T = self._mgr(False)
        job = T.TransferJob(kind="download", label="x")
        mgr._bind_factory(job)
        assert job.local_device is False

    def test_is_local_backend_is_duck_typed(self):
        from astro_smb_gui import transfers as T
        assert T._is_local_backend(object()) is False      # 缺属性不炸
        assert T._is_local_backend(self._Backend(True)) is True

    def test_local_download_does_not_use_parallel(self, tmp_path):
        """真正的行为断言:大文件 + 本地后端 ⇒ job.parallel 保持 False。"""
        from astro_smb_gui import transfers as T
        mgr, _ = self._mgr(True)
        job = mgr.submit_download("asiair", "a\big.fit", tmp_path / "big.fit",
                                  "big.fit", T.PARALLEL_THRESHOLD * 4)
        for _ in range(200):
            if job.finished:
                break
            time.sleep(0.01)
        assert job.parallel is False, "本地盘不该走分块并发"


class TestLocalDeviceWording:
    """本地设备只有插着和拔了两种状态。说"在线 0 ms / 离线"既没信息量又误导
    —— 用户看到"离线"会去查网络,而实际上是卡被拔了。"""

    def test_looks_like_local_path(self):
        from astro_smb_gui._common import looks_like_local_path as f
        assert f("E:\\") and f("E:/") and f("/media/zwo") and f("\\?\Volume{x}")
        assert not f("192.0.2.228") and not f("astro-smb-tool.local") and not f("")

    def test_helper_is_shared_not_duplicated(self):
        """扫描页不能反向 import _window(循环),所以实现必须在 _common 里。"""
        from astro_smb_gui import _common, _scan
        assert _scan.looks_like_local_path is _common.looks_like_local_path


# -------------------------------------- 审查确认【高】:陈旧 size → 静默截断

class TestParallelIgnoresStaleTotal:
    """并行下载器不能信调用方传进来的 total。

    背景:目录索引进缓存后,列表里的 RemoteEntry.size 可能是**任意旧**的
    (上次访问时那一帧还在写、或几天前的索引)。而 client.download_file 一直
    自己 stat、只有并行这条路信外部值 —— 这个不对称是静默截断源:偏小时
    .part 被 truncate 到那个大小、块只覆盖 [0,total)、然后 os.replace 成最终
    文件,**文件名与完整帧一模一样,不抛任何异常**。
    审查用 LocalBackend 实测复现:真实 5 MiB 传入 total=2 MiB → 落地 2097152 字节。
    """

    @staticmethod
    def _dev(tmp_path, payload: bytes):
        from astro_smb.backend import LocalBackend
        root = tmp_path / "dev"
        root.mkdir()
        (root / "big.fit").write_bytes(payload)
        b = LocalBackend(str(root), label="t")
        b.connect()
        return b

    def test_stale_small_total_does_not_truncate(self, tmp_path):
        from astro_smb.parallel import ParallelDownloader
        payload = bytes(range(256)) * 8192            # 2 MiB
        dev = self._dev(tmp_path, payload)
        out = tmp_path / "out.fit"
        res = ParallelDownloader(lambda: dev.clone(), workers=2).download(
            dev.share_name, "big.fit", out, total=len(payload) // 4)   # 陈旧值偏小
        assert out.read_bytes() == payload, "必须按服务器真实大小下全"
        assert res.total == len(payload)

    def test_stale_large_total_also_corrected(self, tmp_path):
        from astro_smb.parallel import ParallelDownloader
        payload = b"Z" * (512 * 1024)
        dev = self._dev(tmp_path, payload)
        out = tmp_path / "out.fit"
        res = ParallelDownloader(lambda: dev.clone(), workers=2).download(
            dev.share_name, "big.fit", out, total=len(payload) * 4)    # 陈旧值偏大
        assert out.read_bytes() == payload
        assert res.total == len(payload)

    def test_correct_total_unchanged(self, tmp_path):
        from astro_smb.parallel import ParallelDownloader
        payload = b"A" * (300 * 1024)
        dev = self._dev(tmp_path, payload)
        out = tmp_path / "out.fit"
        res = ParallelDownloader(lambda: dev.clone(), workers=2).download(
            dev.share_name, "big.fit", out, total=len(payload))
        assert out.read_bytes() == payload and res.total == len(payload)

    def test_stat_failure_falls_back_to_caller_total(self, tmp_path):
        """校正是咨询性的:stat 拿不到就用调用方的值,绝不能让下载整个失败。"""
        from astro_smb.parallel import ParallelDownloader
        payload = b"Q" * 4096
        dev = self._dev(tmp_path, payload)

        class _NoStat:
            def __getattr__(self, k):
                if k == "stat":
                    raise AttributeError("stat")
                return getattr(dev.clone(), k)

        out = tmp_path / "out.fit"
        ParallelDownloader(_NoStat, workers=1).download(
            dev.share_name, "big.fit", out, total=len(payload))
        assert out.read_bytes() == payload


class TestParallelDownloadIsActuallyReachable:
    """分块并发**必须真的会被走到**。

    变异测试把 `PARALLEL_THRESHOLD` 从 16 MiB 抬到 16 TiB —— 一条测试都没红。
    抬上去等于**关掉分块并发**:实测那是 +57% 的提速(单流 6 MiB/s、8 并发
    9.6 MiB/s),而 ASIAIR 的一张 .fit 是 49.77 MB。改坏了不报错、不崩溃,
    只是每次下载都慢一半 —— 没有测试的话谁也不会发现。
    """

    def test_a_real_asiair_frame_crosses_the_threshold(self):
        from astro_smb_app.transfers import PARALLEL_THRESHOLD

        one_fit = 49_770_000          # 真机实测:ASI2600MC Pro 单张亮场
        assert PARALLEL_THRESHOLD < one_fit, (
            f"阈值 {PARALLEL_THRESHOLD} 已经大过一张 .fit —— 分块并发形同关闭")

    def test_a_thumbnail_stays_sequential(self):
        """反面:18 KB 的 _thn.jpg 不该开 8 条连接。"""
        from astro_smb_app.transfers import PARALLEL_THRESHOLD

        assert PARALLEL_THRESHOLD > 18_000

    def test_local_devices_never_go_parallel(self):
        """本地盘顺序读就有 1.48 GB/s;开 8 个句柄同时 seek 只会打乱调度。"""
        import inspect

        from astro_smb_app import transfers

        src = inspect.getsource(transfers.TransferManager)
        assert "not job.local_device" in src, \
            "本地设备的例外没了 —— 插卡时会白开 8 个句柄"
