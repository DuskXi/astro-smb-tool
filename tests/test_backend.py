"""存储后端抽象 / 本地磁盘后端 / ZWO 卡识别 / 设备记录兼容性的离线单测。

全部用 tmp_path 造假目录树,不碰真设备、不碰真 devices.json
(涉及落盘的用例一律 monkeypatch LOCALAPPDATA)。
"""

from __future__ import annotations

import threading

import pytest

from astro_smb.backend import (
    LocalBackend,
    StorageBackend,
    is_local,
    missing_methods,
)
from astro_smb.client import AstroSmbClient, SmbClientError, TransferCancelled
from tests.support import tr

# 一张"像 ZWO 卡"的假盘:8 个特征目录 + 1 个非特征目录 + 3 个系统垃圾
_ZWO_LAYOUT = {
    "Autorun/Light": ["Light_M 31_60.0s_Bin1_20260726-213000.fit"],
    "Autorun/Flat": ["Flat_1.0s_Bin1_20260726-060000.fit"],
    "Autorun/Bias": [],
    "Plan/Light/M 31": ["Light_M 31_60.0s_Bin1_20260726-214000.fit"],
    "Preview/M 31": [],
    "Live": [],
    "Video": [],
    "Stacked/DSO": [],
    "GuidingDarkLibrary": [],
    "log": ["Autorun_Log_2026-07-26_213000.txt",
            "PHD2_GuideLog_2026-07-26_213000.txt"],
    "batch_stack_tmp": [],
    "System Volume Information": ["WPSettings.dat"],
    ".fseventsd": [],
    ".Spotlight-V100": [],
}


@pytest.fixture()
def card(tmp_path):
    """造一张假 ZWO 卡,返回根路径。文件内容 = 名字重复填充,便于按偏移校验。"""
    root = tmp_path / "card"
    root.mkdir()
    for rel, files in _ZWO_LAYOUT.items():
        d = root / rel
        d.mkdir(parents=True, exist_ok=True)
        for fn in files:
            # 每个文件都恰好 600 字节(名字重复填充),方便按总量对账
            (d / fn).write_bytes((fn.encode("utf-8") * 100)[:600])
    return root


@pytest.fixture()
def backend(card):
    b = LocalBackend(card, label="ASIAIR")
    b.connect()
    return b


@pytest.fixture()
def share(backend):
    return backend.list_shares()[0].name


class TestProtocol:
    """契约:SMB 客户端与本地后端必须都满足 StorageBackend。"""

    def test_smb_client_satisfies(self):
        c = AstroSmbClient(host="0.0.0.0")
        assert missing_methods(c) == []
        assert isinstance(c, StorageBackend)

    def test_local_backend_satisfies(self, backend):
        assert missing_methods(backend) == []
        assert isinstance(backend, StorageBackend)

    def test_is_local_flag(self, backend):
        assert is_local(backend) is True
        assert is_local(AstroSmbClient(host="0.0.0.0")) is False


class TestMakeBackend:
    """上层按设备记录建后端的唯一分支点。"""

    def test_smb_default(self):
        from astro_smb.backend import make_backend

        b = make_backend(host="192.0.2.225", timeout=3)
        assert isinstance(b, AstroSmbClient) and b.timeout == 3
        assert not is_local(b)

    def test_unknown_kind_falls_back_to_smb(self):
        """未知 kind 退回 SMB。**顺带确认参数真的传下去了** ——
        只断言类型的话,一个忽略 host 的实现照样能过。"""
        from astro_smb.backend import make_backend

        be = make_backend("ftp", host="1.2.3.4")
        assert isinstance(be, AstroSmbClient)
        assert be.host == "1.2.3.4"

    def test_local_from_path(self, card):
        from astro_smb.backend import make_backend

        b = make_backend("local", host=str(card), path=str(card), label="ASIAIR")
        assert is_local(b) and b.label == "ASIAIR"
        assert b.list_shares()[0].name == "ASIAIR"

    def test_local_falls_back_to_host(self, card):
        from astro_smb.backend import make_backend

        assert make_backend("local", host=str(card)).root == card

    def test_local_ignores_smb_only_kwargs(self, card):
        """同一份 kwargs 要能喂给两种设备,本地不认识的键直接忽略而不是 TypeError。"""
        from astro_smb.backend import make_backend

        b = make_backend("local", path=str(card), username="", port=445,
                         chunk_size=1 << 20)
        assert b.chunk_size == 1 << 20

    def test_missing_target_raises(self):
        from astro_smb.backend import make_backend

        with pytest.raises(SmbClientError):
            make_backend("local")
        with pytest.raises(SmbClientError):
            make_backend("smb", host="  ")


class TestEnumeration:
    def test_single_share_named_by_label(self, backend):
        shares = backend.list_shares()
        assert len(shares) == 1
        s = shares[0]
        assert s.name == "ASIAIR" and s.is_disk and not s.is_hidden

    def test_default_label_from_dir_name(self, card):
        assert LocalBackend(card).list_shares()[0].name == "card"

    def test_listdir_root_sorted_like_smb(self, backend, share):
        entries = backend.listdir(share)
        names = [e.name for e in entries]
        assert all(e.is_dir for e in entries)          # 该层全是目录
        assert names == sorted(names, key=str.lower)   # 与 SMB 侧同一套排序
        assert "Autorun" in names
        # 系统垃圾目录**不过滤**:文件管理器应如实显示,过滤是识别逻辑的事
        assert "System Volume Information" in names

    def test_listdir_dirs_before_files(self, backend, share, card):
        (card / "Stacked" / "aaa.txt").write_bytes(b"x")
        assert [e.name for e in backend.listdir(share, "Stacked")] == \
            ["DSO", "aaa.txt"]

    def test_listdir_paths_use_backslash(self, backend, share):
        entries = backend.listdir(share, "Autorun")
        assert {e.path for e in entries} == {
            "Autorun\\Bias", "Autorun\\Flat", "Autorun\\Light"}

    def test_listdir_accepts_forward_slash(self, backend, share):
        a = backend.listdir(share, "Autorun/Light")
        b = backend.listdir(share, "Autorun\\Light")
        assert [e.name for e in a] == [e.name for e in b] and len(a) == 1

    def test_listdir_file_sizes_and_flags(self, backend, share):
        (e,) = backend.listdir(share, "Autorun\\Light")
        assert not e.is_dir and e.size == 600 and e.mtime > 0
        assert e.attr_text()[0] == "-"          # 不是目录

    def test_listdir_missing_path(self, backend, share):
        with pytest.raises(SmbClientError, match="路径不存在"):
            backend.listdir(share, "Nope")

    def test_stat_root_pseudo_entry(self, backend, share):
        e = backend.stat(share, "")
        assert e.is_dir and e.name == share and e.path == ""

    def test_stat_file_and_exists(self, backend, share):
        p = "Autorun\\Light\\Light_M 31_60.0s_Bin1_20260726-213000.fit"
        e = backend.stat(share, p)
        assert e.size == 600 and e.path == p and not e.is_dir
        assert backend.exists(share, p)
        assert not backend.exists(share, "Autorun\\Light\\nope.fit")

    def test_unknown_share_rejected(self, backend):
        with pytest.raises(SmbClientError, match="共享不存在"):
            backend.listdir("EMMC Images", "")

    def test_connect_reports_missing_root(self, tmp_path):
        with pytest.raises(SmbClientError, match="不存在"):
            LocalBackend(tmp_path / "ejected").connect()


class TestPartialRead:
    """FITS 头部分读取靠 read_bytes:必须 seek+read,而不是整文件读完再切。"""

    PATH = "Autorun\\Light\\Light_M 31_60.0s_Bin1_20260726-213000.fit"

    def test_read_head(self, backend, share, card):
        raw = (card / "Autorun" / "Light" /
               "Light_M 31_60.0s_Bin1_20260726-213000.fit").read_bytes()
        assert backend.read_bytes(share, self.PATH, 0, 16) == raw[:16]

    def test_read_at_offset(self, backend, share, card):
        raw = (card / "Autorun" / "Light" /
               "Light_M 31_60.0s_Bin1_20260726-213000.fit").read_bytes()
        assert backend.read_bytes(share, self.PATH, 128, 64) == raw[128:192]

    def test_read_past_eof_returns_short(self, backend, share):
        assert backend.read_bytes(share, self.PATH, 590, 4096) == \
            backend.read_bytes(share, self.PATH, 590, 10)

    def test_read_zero_size(self, backend, share):
        assert backend.read_bytes(share, self.PATH, 0, 0) == b""

    def test_read_directory_fails(self, backend, share):
        with pytest.raises(SmbClientError):
            backend.read_bytes(share, "Autorun", 0, 16)


class TestPathEscape:
    """路径安全:任何越出 root 的写法都必须报错,而不是被静默折叠成合法路径。"""

    @pytest.mark.parametrize("bad", [
        "..", "..\\..", "../../etc", "Autorun\\..\\..\\secret",
        "Autorun/../../secret",
    ])
    def test_dotdot_rejected(self, backend, share, bad):
        with pytest.raises(SmbClientError, match="越界"):
            backend.listdir(share, bad)

    @pytest.mark.parametrize("bad", ["C:\\Windows", "Autorun\\x:y"])
    def test_drive_letter_rejected(self, backend, share, bad):
        with pytest.raises(SmbClientError, match="非法路径片段"):
            backend.listdir(share, bad)

    def test_escape_does_not_read_outside(self, backend, share, tmp_path):
        (tmp_path / "outside.txt").write_bytes(b"secret")
        with pytest.raises(SmbClientError):
            backend.read_bytes(share, "..\\outside.txt", 0, 16)

    def test_stat_escape_rejected(self, backend, share):
        with pytest.raises(SmbClientError, match="越界"):
            backend.stat(share, "..\\..\\anything")

    def test_stat_bare_dotdot_is_not_root(self, backend, share):
        # 曾经的坑:先 normalize 会把 '..' 折成 ''(= 共享根),逃逸企图就看不见了
        with pytest.raises(SmbClientError, match="越界"):
            backend.stat(share, "..")

    @pytest.mark.parametrize("name,args", [
        ("makedirs", ("..\\evil",)),
        ("mkdir", ("..\\evil",)),
        ("remove", ("..\\evil.txt",)),
        ("rmdir", ("..\\evil",)),
        ("dir_tree", ("..",)),
        ("dir_stat", ("..",)),
        ("scan_children", ("..",)),
        ("count_children", ("..",)),
        ("exists", ("..",)),
    ])
    def test_write_and_scan_ops_reject_escape(self, backend, share, name, args):
        """写操作/扫描一律先校验再规范化 —— 否则 '..' 会被静默折叠后放行。"""
        fn = getattr(backend, name)
        if name in ("exists", "count_children"):
            # 这两个的契约是"失败返回假值"而不是抛异常,只要不越权访问即可
            assert fn(share, *args) in (False, None)
            return
        with pytest.raises(SmbClientError, match="越界"):
            fn(share, *args)

    def test_rename_rejects_escape_on_both_sides(self, backend, share):
        with pytest.raises(SmbClientError, match="越界"):
            backend.rename(share, "..\\a", "b")
        with pytest.raises(SmbClientError, match="越界"):
            backend.rename(share, "Live", "..\\b")

    def test_walk_rejects_escape(self, backend, share):
        with pytest.raises(SmbClientError, match="越界"):
            list(backend.walk(share, ".."))


class TestTraversal:
    def test_walk_covers_tree(self, backend, share):
        seen = {p for p, _d, _f in backend.walk(share)}
        assert "" in seen and "Autorun\\Light" in seen and "Plan\\Light\\M 31" in seen

    def test_walk_max_depth(self, backend, share):
        seen = {p for p, _d, _f in backend.walk(share, max_depth=1)}
        assert "Autorun" in seen and "Autorun\\Light" not in seen

    def test_walk_cancel_raises(self, backend, share):
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(TransferCancelled):
            list(backend.walk(share, cancel=cancel))

    def test_dir_tree_aggregates_bottom_up(self, backend, share):
        root = backend.dir_tree(share, "Autorun")
        # Light(600) + Flat(600) + Bias(0)
        assert root.size == 1200 and root.file_count == 2
        assert not root.partial and root.error_count == 0
        assert [c.name for c in root.children][:2] == ["Flat", "Light"] or \
               [c.name for c in root.children][:2] == ["Light", "Flat"]
        assert root.children[-1].name == "Bias" and root.children[-1].size == 0

    def test_dir_tree_whole_card(self, backend, share):
        root = backend.dir_tree(share)
        # 6 个文件:Light 1 + Flat 1 + Plan 1 + log 2 + System Volume Information 1
        assert root.file_count == 6
        assert root.size == 6 * 600

    def test_dir_stat(self, backend, share):
        st = backend.dir_stat(share, "Autorun")
        assert st.file_count == 2 and st.total_size == 1200
        assert st.dir_count == 3 and not st.partial

    def test_dir_stat_cancel_marks_partial(self, backend, share):
        cancel = threading.Event()
        cancel.set()
        st = backend.dir_stat(share, "Autorun", cancel=cancel)
        assert st.partial

    def test_scan_children_sorted_desc(self, backend, share):
        rows = backend.scan_children(share, "Autorun")
        assert [r[0].name for r in rows][-1] == "Bias"      # 0 字节排最后
        assert dict((r[0].name, r[1]) for r in rows)["Light"] == 600

    def test_count_children(self, backend, share):
        assert backend.count_children(share, "Autorun") == (3, 0)
        assert backend.count_children(share, "Autorun\\Light") == (0, 1)
        assert backend.count_children(share, "Nope") is None

    def test_find_pattern_case_insensitive(self, backend, share):
        hits = list(backend.find(share, "", "*.FIT"))
        assert len(hits) == 3 and all(h.name.endswith(".fit") for h in hits)

    def test_find_min_size_and_limit(self, backend, share):
        assert list(backend.find(share, "", "*.fit", min_size=10_000)) == []
        assert len(list(backend.find(share, "", "*", limit=2))) == 2

    def test_find_chinese_and_space_names(self, backend, share, card):
        (card / "Plan" / "Light" / "M 31" / "星云 测试.fit").write_bytes(b"x" * 5)
        hits = [h.name for h in backend.find(share, "Plan", "星云*")]
        assert hits == ["星云 测试.fit"]


class TestVolumeAndInfo:
    def test_volume_info(self, backend, share):
        vi = backend.volume_info(share)
        assert vi is not None and vi.total > 0 and vi.free >= 0
        assert 0.0 <= vi.percent <= 100.0

    def test_volume_info_bad_share(self, backend):
        assert backend.volume_info("nope") is None

    def test_server_info_shape(self, backend):
        info = backend.server_info()
        assert info["dialect"] == tr("本地磁盘")
        assert info["server_name"] == "ASIAIR"
        assert info["host"] == backend.host

    def test_echo_and_ping(self, backend):
        assert backend.echo() == 0.0 and backend.ping_tcp() == 0.0

    def test_clone_is_independent_same_config(self, backend):
        c = backend.clone()
        assert c is not backend and c.root == backend.root and c.label == backend.label


class TestTransfer:
    SRC = "Autorun\\Light\\Light_M 31_60.0s_Bin1_20260726-213000.fit"

    def test_download_file_atomic(self, backend, share, tmp_path, card):
        dst = tmp_path / "out" / "a.fit"
        backend.download_file(share, self.SRC, dst)
        assert dst.read_bytes() == (card / "Autorun" / "Light" /
                                    "Light_M 31_60.0s_Bin1_20260726-213000.fit"
                                    ).read_bytes()
        assert not dst.with_name("a.fit.part").exists()

    def test_download_progress_monotonic(self, backend, share, tmp_path):
        seen = []
        backend.download_file(share, self.SRC, tmp_path / "a.fit",
                              progress=lambda d, t: seen.append((d, t)))
        assert seen[0][0] == 0 and seen[-1] == (600, 600)
        assert [d for d, _ in seen] == sorted(d for d, _ in seen)

    def test_download_cancel_keeps_part_not_target(self, backend, share, tmp_path):
        backend.chunk_size = 1 << 16
        cancel = threading.Event()
        cancel.set()
        dst = tmp_path / "a.fit"
        with pytest.raises(TransferCancelled):
            backend.download_file(share, self.SRC, dst, cancel=cancel)
        assert not dst.exists()                 # 最终路径上绝不留半成品

    def test_download_resume_from_part(self, backend, share, tmp_path, card):
        dst = tmp_path / "a.fit"
        raw = (card / "Autorun" / "Light" /
               "Light_M 31_60.0s_Bin1_20260726-213000.fit").read_bytes()
        dst.with_name("a.fit.part").write_bytes(raw[:200])
        seen = []
        backend.download_file(share, self.SRC, dst, resume=True,
                              progress=lambda d, t: seen.append(d))
        assert dst.read_bytes() == raw
        assert seen[0] == 200                   # 只补了差量

    def test_download_resume_skips_complete_target(self, backend, share, tmp_path, card):
        raw = (card / "Autorun" / "Light" /
               "Light_M 31_60.0s_Bin1_20260726-213000.fit").read_bytes()
        dst = tmp_path / "a.fit"
        dst.write_bytes(raw)
        backend.download_file(share, self.SRC, dst, resume=True)
        assert dst.read_bytes() == raw

    def test_download_fresh_discards_stale_part(self, backend, share, tmp_path, card):
        dst = tmp_path / "a.fit"
        dst.with_name("a.fit.part").write_bytes(b"GARBAGE" * 10)
        backend.download_file(share, self.SRC, dst)     # resume=False
        assert dst.read_bytes() == (card / "Autorun" / "Light" /
                                    "Light_M 31_60.0s_Bin1_20260726-213000.fit"
                                    ).read_bytes()

    def test_download_dir(self, backend, share, tmp_path):
        n = backend.download_dir(share, "Autorun", tmp_path / "dl")
        assert n == 2
        assert (tmp_path / "dl" / "Autorun" / "Light").is_dir()
        assert (tmp_path / "dl" / "Autorun" / "Bias").is_dir()

    def test_download_range_matches_source(self, backend, share, tmp_path, card):
        raw = (card / "Autorun" / "Light" /
               "Light_M 31_60.0s_Bin1_20260726-213000.fit").read_bytes()
        target = tmp_path / "chunked.bin"
        with open(target, "wb") as fh:
            fh.truncate(600)
        got = []
        with open(target, "r+b") as fh:
            backend.download_range(share, self.SRC, 100, 200, fh,
                                   on_bytes=got.append)
        assert target.read_bytes()[100:300] == raw[100:300]
        assert sum(got) == 200

    def test_parallel_downloader_over_local_backend(self, backend, share,
                                                    card, tmp_path):
        """ParallelDownloader 只依赖 connect/close/download_range —— 本地后端也应能跑。"""
        from astro_smb.parallel import ParallelDownloader

        big = card / "Live" / "big.bin"
        payload = bytes(range(256)) * 10240      # 2.5 MiB,跨多个 1MiB 块
        big.write_bytes(payload)
        dst = tmp_path / "big.bin"
        res = ParallelDownloader(backend.clone, workers=3).download(
            share, "Live\\big.bin", dst, len(payload))
        assert dst.read_bytes() == payload
        assert res.n_chunks >= 2 and not dst.with_name("big.bin.part").exists()

    def test_upload_file_and_overwrite(self, backend, share, tmp_path):
        src = tmp_path / "up.fit"
        src.write_bytes(b"A" * 300)
        rel = backend.upload_file(src, share, "Plan\\Light\\M 31\\up.fit")
        assert rel == "Plan\\Light\\M 31\\up.fit"
        assert backend.stat(share, rel).size == 300
        src.write_bytes(b"B" * 10)
        backend.upload_file(src, share, rel)
        assert backend.read_bytes(share, rel, 0, 99) == b"B" * 10

    def test_upload_creates_parents(self, backend, share, tmp_path):
        src = tmp_path / "x.txt"
        src.write_bytes(b"hello")
        backend.upload_file(src, share, "Video\\新建\\deep\\x.txt")
        assert backend.exists(share, "Video\\新建\\deep\\x.txt")

    def test_upload_cancel_leaves_no_part(self, backend, share, tmp_path, card):
        src = tmp_path / "x.txt"
        src.write_bytes(b"hello")
        cancel = threading.Event()
        cancel.set()
        with pytest.raises(TransferCancelled):
            backend.upload_file(src, share, "Video\\x.txt", cancel=cancel)
        assert not (card / "Video" / "x.txt.part").exists()
        assert not (card / "Video" / "x.txt").exists()

    def test_upload_escape_rejected(self, backend, share, tmp_path):
        src = tmp_path / "x.txt"
        src.write_bytes(b"hello")
        with pytest.raises(SmbClientError, match="越界"):
            backend.upload_file(src, share, "..\\evil.txt")

    def test_upload_dir(self, backend, share, tmp_path):
        d = tmp_path / "batch"
        (d / "sub").mkdir(parents=True)
        (d / "a.txt").write_bytes(b"a")
        (d / "sub" / "b.txt").write_bytes(b"b")
        assert backend.upload_dir(d, share, "Video") == 2
        assert backend.exists(share, "Video\\batch\\sub\\b.txt")


class TestMutations:
    def test_makedirs_idempotent(self, backend, share):
        backend.makedirs(share, "Plan\\Light\\NGC 7293")
        backend.makedirs(share, "Plan\\Light\\NGC 7293")
        assert backend.exists(share, "Plan\\Light\\NGC 7293")

    def test_rename_and_collision(self, backend, share):
        backend.rename(share, "Live", "Live2")
        assert backend.exists(share, "Live2") and not backend.exists(share, "Live")
        with pytest.raises(SmbClientError, match="目标已存在"):
            backend.rename(share, "Live2", "Video")

    def test_remove_file(self, backend, share):
        p = "log\\Autorun_Log_2026-07-26_213000.txt"
        backend.remove(share, p)
        assert not backend.exists(share, p)

    def test_remove_directory_refused(self, backend, share):
        with pytest.raises(SmbClientError, match="目标是目录"):
            backend.remove(share, "Autorun")

    def test_remove_root_refused(self, backend, share):
        with pytest.raises(SmbClientError):
            backend.remove(share, "")

    def test_rmdir_non_empty_refused(self, backend, share):
        with pytest.raises(SmbClientError, match="非空"):
            backend.rmdir(share, "Autorun")

    def test_rmdir_recursive(self, backend, share):
        backend.rmdir(share, "Autorun", recursive=True)
        assert not backend.exists(share, "Autorun")


class TestZwoSignature:
    def test_card_hits_all_features(self, card):
        from astro_smb_gui import volumes

        score, hits = volumes.zwo_signature(card)
        assert score == 8
        assert "Autorun" in hits and "GuidingDarkLibrary" in hits

    def test_junk_dirs_ignored(self, card):
        from astro_smb_gui import volumes

        _hits, others = volumes.scan_root(card)
        # System Volume Information / .fseventsd / .Spotlight-V100 都不算"杂物"
        assert others == ["batch_stack_tmp"]

    def test_ordinary_disk_scores_zero(self, tmp_path):
        from astro_smb_gui import volumes

        for d in ("Windows", "Program Files", "Users", "temp"):
            (tmp_path / d).mkdir()
        score, hits = volumes.zwo_signature(tmp_path)
        assert score == 0 and hits == []

    def test_missing_root_is_silent(self, tmp_path):
        from astro_smb_gui import volumes

        assert volumes.zwo_signature(tmp_path / "gone") == (0, [])
        assert volumes.scan_root(tmp_path / "gone") == ([], [])

    def test_same_named_file_does_not_count(self, tmp_path):
        from astro_smb_gui import volumes

        for n in ("Autorun", "Plan", "log"):
            (tmp_path / n).write_bytes(b"x")     # 是文件不是目录
        assert volumes.zwo_signature(tmp_path)[0] == 0

    def test_describe(self, card):
        from astro_smb_gui import volumes

        text = volumes.describe_zwo(card)
        # **整条是一个 msgid**(`ZWO 卡 · 命中 {0} 项({1}{tail})`),
        # 所以从同一个 msgid 拼出期望值 —— 比前缀在中文下碰巧成立,
        # 换语言整条被一起翻掉就不成立了。
        # 展示的三个是**按 ZWO_DIRS 的官方顺序**挑的,不是 scandir 的顺序
        hits, _others = volumes.scan_root(card)
        order = {d.casefold(): i for i, d in enumerate(volumes.ZWO_DIRS)}
        shown = sorted(hits, key=lambda h: order.get(h.casefold(), 99))[:3]
        assert text == tr("ZWO 卡 · 命中 {0} 项({1}{tail})", len(hits),
                          "/".join(shown),
                          tail="…" if len(hits) > len(shown) else "")
        assert volumes.describe_zwo(card / "batch_stack_tmp") == ""


class TestAutodetect:
    def test_detects_card(self, card):
        from astro_smb_gui import volumes

        vol = volumes.VolumeInfo(path=card, label="ASIAIR",
                                 kind=volumes.KIND_REMOVABLE,
                                 total=1000, free=400)
        assert volumes.autodetect_zwo([vol]) == [card]

    def test_accepts_plain_paths(self, card):
        from astro_smb_gui import volumes

        assert volumes.autodetect_zwo([card]) == [card]

    def test_rejects_cluttered_disk(self, tmp_path):
        """3 个同名目录 + 一堆杂物的大硬盘不能被当成 ASIAIR 卡。"""
        from astro_smb_gui import volumes

        for d in ("Autorun", "Plan", "log", "Downloads", "Music", "Games",
                  "Projects", "Backup"):
            (tmp_path / d).mkdir()
        assert volumes.zwo_signature(tmp_path)[0] == 3       # 分数够
        assert volumes.autodetect_zwo([tmp_path]) == []      # 但杂物太多

    def test_rejects_too_few_hits(self, tmp_path):
        from astro_smb_gui import volumes

        for d in ("Autorun", "Plan"):
            (tmp_path / d).mkdir()
        assert volumes.autodetect_zwo([tmp_path]) == []

    def test_bad_entries_do_not_crash(self):
        from astro_smb_gui import volumes

        assert volumes.autodetect_zwo([None, 123, object()]) == []
        assert volumes.autodetect_zwo(None) == []


class TestListVolumes:
    def test_returns_list_without_raising(self):
        from astro_smb_gui import volumes

        vols = volumes.list_volumes()
        assert isinstance(vols, list)
        for v in vols:
            assert v.kind in (volumes.KIND_FIXED, volumes.KIND_REMOVABLE,
                              volumes.KIND_NETWORK)
            assert v.total >= 0 and v.free >= 0
            assert v.display and v.kind_text

    @pytest.mark.skipif(__import__("os").name != "nt", reason="仅 Windows")
    def test_windows_lists_system_drive(self):
        from astro_smb_gui import volumes

        drives = {v.drive.upper() for v in volumes.list_volumes()}
        assert "C:" in drives


class TestDeviceRecords:
    """新老 devices.json 兼容 + 本地设备摘要。"""

    def test_old_record_defaults_to_smb(self):
        from astro_smb_gui import devices

        rec = devices._normalize({"host": "192.0.2.225", "name": "ASIAIR",
                                  "dialect": "SMB 3.1.1", "shares": 3})
        assert rec["kind"] == devices.KIND_SMB and rec["path"] == ""
        assert not devices.is_local(rec)
        assert devices.summary(rec) == "  ·  ".join(
            ["ASIAIR", "SMB 3.1.1", tr("{0} 共享", 3)])

    def test_unknown_kind_falls_back(self):
        from astro_smb_gui import devices

        assert devices._normalize({"host": "h", "kind": "ftp"})["kind"] == \
            devices.KIND_SMB

    def test_local_record_summary(self):
        from astro_smb_gui import devices

        rec = devices._normalize({"host": "E:\\", "kind": "local",
                                  "path": "E:\\", "name": "ASIAIR",
                                  "dialect": "本地磁盘", "shares": 1})
        assert devices.is_local(rec) and devices.local_root(rec) == "E:\\"
        assert devices.summary(rec) == "  ·  ".join([tr("本地磁盘"), "ASIAIR"])

    def test_local_record_without_path_falls_back_to_host(self):
        from astro_smb_gui import devices

        rec = devices._normalize({"host": "E:\\", "kind": "local"})
        assert devices.local_root(rec) == "E:\\"

    def test_remember_roundtrip(self, tmp_path, monkeypatch):
        from astro_smb_gui import devices

        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        devices.remember("192.0.2.225", name="ASIAIR", dialect="SMB 3.1.1",
                         shares=3)
        devices.remember("E:\\", name="ASIAIR", dialect="本地磁盘", shares=1,
                         kind=devices.KIND_LOCAL, path="E:\\")
        recs = devices.load()
        assert len(recs) == 2
        by_host = {r["host"]: r for r in recs}
        assert by_host["E:\\"]["kind"] == devices.KIND_LOCAL
        assert by_host["E:\\"]["path"] == "E:\\"
        assert by_host["192.0.2.225"]["kind"] == devices.KIND_SMB
        assert devices.last_device()["host"] == "E:\\"      # 最近成功的在最前

    def test_remember_rejects_bad_kind(self, tmp_path, monkeypatch):
        from astro_smb_gui import devices

        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        devices.remember("h", kind="nonsense")
        assert devices.load()[0]["kind"] == devices.KIND_SMB

    def test_legacy_json_file_loads(self, tmp_path, monkeypatch):
        import json

        from astro_smb_gui import devices

        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        target = devices.devices_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps([
            {"host": "192.0.2.225", "name": "ASIAIR", "os": "Samba",
             "dialect": "SMB 3.1.1", "shares": 3,
             "first_seen": 1.0, "last_ok": 2.0},
        ]), encoding="utf-8")
        (rec,) = devices.load()
        assert rec["kind"] == devices.KIND_SMB and rec["path"] == ""
        assert rec["name"] == "ASIAIR"


# ------------------------------------------------ D组审查确认项(Phase-A 零审查代码)

class TestWalkDoesNotFollowReparsePoints:
    """[高] walk 跟随目录联接/符号链接 → 无限递归。

    审查实测:用 mklink /J 建一个指回根的联接后,一个"1 文件 10 字节"的目录
    被 dir_stat 报成 65 文件 / 650 字节 / 130 目录,且只有靠 max_depth 或
    cancel 才停得下来 —— download_dir 会一直复制到填满磁盘。
    dir_stat / dir_tree / find / download_dir / rmdir(recursive) 全走 walk。
    """

    @staticmethod
    def _backend(root):
        from astro_smb.backend import LocalBackend
        b = LocalBackend(str(root), label="t")
        b.connect()
        return b

    def test_reparse_dir_is_listed_but_not_descended(self, tmp_path):
        from astro_smb.backend import ATTR_DIRECTORY, ATTR_REPARSE_POINT, RemoteEntry
        b = self._backend(tmp_path)
        (tmp_path / "real").mkdir()
        (tmp_path / "real" / "a.txt").write_text("x")
        seen = []
        real_listdir = b.listdir

        def fake_listdir(share, path=""):
            seen.append(path)
            if path == "":
                e = RemoteEntry(share=share, path="loop", name="loop", is_dir=True,
                                size=0, mtime=0.0, ctime=0.0, atime=0.0,
                                attributes=ATTR_DIRECTORY | ATTR_REPARSE_POINT)
                return list(real_listdir(share, path)) + [e]
            return real_listdir(share, path)

        b.listdir = fake_listdir
        list(b.walk(b.share_name, ""))
        assert "loop" not in seen, "walk 绝不能下降进重解析点"
        assert "real" in seen, "普通目录照常下降"

    def test_reparse_attr_constant(self):
        from astro_smb import backend as B
        assert B.ATTR_REPARSE_POINT == 0x400        # FILE_ATTRIBUTE_REPARSE_POINT

    def test_posix_symlink_gets_reparse_bit(self, tmp_path):
        """POSIX 没有 st_file_attributes,符号链接要自己补上重解析位。"""
        from astro_smb.backend import ATTR_REPARSE_POINT, _attrs_of

        class _FakeLink:
            def is_symlink(self):
                return True

        attrs = _attrs_of(None, "link", True, _FakeLink())
        assert attrs & ATTR_REPARSE_POINT

    def test_plain_dir_has_no_reparse_bit(self, tmp_path):
        from astro_smb.backend import ATTR_REPARSE_POINT, _attrs_of

        class _NotLink:
            def is_symlink(self):
                return False

        assert not (_attrs_of(None, "d", True, _NotLink()) & ATTR_REPARSE_POINT)


class TestResumeRejectsOversizedPart:
    """[中] 比源还大的 .part 被原样落盘:最终文件大小对不上、内容还是旧的,
    函数却正常返回。SMB 侧一直是 `if start > total: start = 0`,本地侧要对齐。"""

    def test_oversized_part_is_discarded(self, tmp_path):
        from astro_smb.backend import LocalBackend
        src_root = tmp_path / "dev"
        src_root.mkdir()
        (src_root / "f.bin").write_bytes(b"NEW12")          # 5 字节
        b = LocalBackend(str(src_root), label="t")
        b.connect()
        out = tmp_path / "out" / "f.bin"
        out.parent.mkdir()
        out.with_name(out.name + ".part").write_bytes(b"OLDOLDOLDOLDOLD1")  # 16 字节
        b.download_file(b.share_name, "f.bin", out, resume=True)
        assert out.read_bytes() == b"NEW12", "超长 .part 必须丢弃重下"

    def test_valid_partial_still_resumes(self, tmp_path):
        from astro_smb.backend import LocalBackend
        src_root = tmp_path / "dev"
        src_root.mkdir()
        (src_root / "f.bin").write_bytes(b"ABCDEFGH")
        b = LocalBackend(str(src_root), label="t")
        b.connect()
        out = tmp_path / "out" / "f.bin"
        out.parent.mkdir()
        out.with_name(out.name + ".part").write_bytes(b"ABC")
        b.download_file(b.share_name, "f.bin", out, resume=True)
        assert out.read_bytes() == b"ABCDEFGH"


class TestLocalLivenessIsReal:
    """[中] 卡拔掉后心跳仍显示"在线 0ms":echo/ping_tcp 恒返回 0.0。
    shell 的心跳循环只调 echo(),从不调 connect(),所以判定必须在 echo 里做。"""

    def test_echo_ok_while_media_present(self, tmp_path):
        from astro_smb.backend import LocalBackend
        b = LocalBackend(str(tmp_path), label="t")
        b.connect()
        assert b.echo() == 0.0 and b.ping_tcp() == 0.0

    def test_echo_raises_after_media_removed(self, tmp_path):
        import shutil
        from astro_smb.backend import LocalBackend, SmbClientError
        root = tmp_path / "card"
        root.mkdir()
        b = LocalBackend(str(root), label="t")
        b.connect()
        shutil.rmtree(root)                     # 模拟拔卡
        with pytest.raises(SmbClientError):
            b.echo()
        assert b.ping_tcp() is None, "拔出后不可达,不能再报 0 ms"
