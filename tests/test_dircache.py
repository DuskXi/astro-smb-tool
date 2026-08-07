"""目录索引缓存层(astro_smb_gui.dircache)的离线单测 —— 不连任何设备。

覆盖:序列化往返 / 逐字段完整性 / 按设备与共享隔离 / 版本常量升级即失效 /
三种粒度的失效 API(含 LIKE 通配符转义)/ ASTRO_SMB_GUI_NOCACHE 旁路 /
空目录与超大目录 / 占用树紧凑编码(path 由父路径推出)/ TTL /
"乐观显示 + 后台对账"发现不一致时的行为。
"""
from __future__ import annotations

import os
import time

import pytest

from astro_smb_gui import dircache, metacache
from astro_smb.backend import LocalBackend
from astro_smb.client import RemoteEntry, SmbClientError, TreeNode
from tests.support import tr

HOST_A = "192.0.2.225"
HOST_B = "192.0.2.228"
SHARE = "EMMC Images"


def _reset_default_cache() -> None:
    """把进程级默认实例还原成"未初始化",避免 tmp 库泄漏到后续用例。"""
    metacache.close()
    metacache._default = None


@pytest.fixture()
def cachedb(tmp_path, monkeypatch):
    """把 metacache 的**模块级默认实例**指向本用例专属的库文件。"""
    monkeypatch.delenv("ASTRO_SMB_GUI_NOCACHE", raising=False)
    mc = metacache.use_path(tmp_path / "meta.db")
    yield mc
    _reset_default_cache()


def _entry(name: str, *, path: str | None = None, share: str = SHARE,
           is_dir: bool = False, size: int = 1234, mtime: float = 1_700_000_000.5,
           ctime: float = 1_699_000_000.25, atime: float = 1_701_000_000.75,
           attributes: int = 0x20) -> RemoteEntry:
    return RemoteEntry(
        share=share, path=path if path is not None else name, name=name,
        is_dir=is_dir, size=size, mtime=mtime, ctime=ctime, atime=atime,
        attributes=attributes)


def _sample() -> list[RemoteEntry]:
    return [
        _entry("Autorun", path="Autorun", is_dir=True, size=0,
               attributes=0x10, mtime=1_700_000_100.0),
        _entry("中文 目录", path="中文 目录", is_dir=True, size=0, attributes=0x10),
        _entry("Light_M 8_300.0s_Bin1_20260701-221530_-10.1C_0001.fit",
               path="Light_M 8_300.0s_Bin1_20260701-221530_-10.1C_0001.fit",
               size=52_168_320, attributes=0x20),
    ]


class _FakeDirClient:
    """只实现 listdir/clone/close 的假客户端,统计 listdir 次数。"""

    def __init__(self, layout: dict, host: str = HOST_A):
        self.host = host
        self.layout = layout          # 规范化 path → list[RemoteEntry]
        self.listdirs: list[str] = []

    def clone(self):
        c = _FakeDirClient(self.layout, self.host)
        c.listdirs = self.listdirs    # 共享计数器,便于断言总往返数
        return c

    def connect(self):
        pass

    def close(self):
        pass

    def listdir(self, share: str, path: str = ""):
        self.listdirs.append(path)
        try:
            return list(self.layout[path])
        except KeyError:
            raise SmbClientError(f"列目录 {share}/{path} 失败: 路径不存在")


# ------------------------------------------------------------------ 键 / 设备维度

class TestKeys:
    def test_backend_of_client(self):
        assert dircache.backend_of(_FakeDirClient({}, HOST_B)) == HOST_B

    def test_backend_of_plain_host_string(self):
        assert dircache.backend_of(HOST_A) == HOST_A

    def test_backend_of_none_is_empty(self):
        assert dircache.backend_of(None) == ""

    def test_dir_key_normalizes_separators(self):
        # 正斜杠 / 反斜杠 / 多余分隔符必须收敛到同一个键,否则同一个目录会存两条
        assert (dircache.dir_key(SHARE, "Autorun/Light")
                == dircache.dir_key(SHARE, "Autorun\\Light")
                == dircache.dir_key(SHARE, "//Autorun//Light//")
                == f"{SHARE}|Autorun\\Light")

    def test_dir_key_share_root(self):
        assert dircache.dir_key(SHARE, "") == f"{SHARE}|"

    def test_dir_key_resolves_dotdot(self):
        assert dircache.dir_key(SHARE, "a/b/../c") == f"{SHARE}|a\\c"


# ------------------------------------------------------------------ 序列化往返

class TestSerialization:
    def test_roundtrip_every_field(self):
        """逐字段对比 —— 只比 name 会漏掉 mtime/attributes 之类的类型漂移。"""
        src = _sample()
        got = dircache.decode_entries(dircache.encode_entries(src))
        assert got is not None and len(got) == len(src)
        names = [f.name for f in RemoteEntry.__dataclass_fields__.values()]
        assert len(names) == 9
        for a, b in zip(src, got):
            for f in names:
                assert getattr(a, f) == getattr(b, f), f
                assert type(getattr(a, f)) is type(getattr(b, f)), f

    def test_roundtrip_equality(self):
        src = _sample()
        assert dircache.decode_entries(dircache.encode_entries(src)) == src

    def test_int_mtime_comes_back_as_float(self):
        """JSON 把 0.0 写成 0;回来必须仍是 float,不然与新读的 entry 比不相等。"""
        e = _entry("a.fit", mtime=0.0, ctime=0.0, atime=0.0)
        got = dircache.decode_entries(dircache.encode_entries([e]))
        assert got is not None
        assert isinstance(got[0].mtime, float) and got[0].mtime == 0.0

    def test_empty_dir_roundtrip(self):
        payload = dircache.encode_entries([])
        assert dircache.decode_entries(payload) == []

    def test_large_dir_roundtrip(self):
        big = [_entry(f"f{i:05d}.fit", size=i, mtime=1_700_000_000.0 + i)
               for i in range(5000)]
        got = dircache.decode_entries(dircache.encode_entries(big))
        assert got == big

    def test_decode_rejects_non_dict(self):
        assert dircache.decode_entries(None) is None
        assert dircache.decode_entries([1, 2, 3]) is None

    def test_decode_rejects_missing_rows(self):
        assert dircache.decode_entries({"fields": list(dircache._ENTRY_FIELDS)}) is None

    def test_decode_rejects_field_set_change(self):
        """字段名/顺序对不上 → 当未命中重读,绝不拼出半截对象。"""
        payload = dircache.encode_entries(_sample())
        payload["fields"] = payload["fields"][:-1]
        assert dircache.decode_entries(payload) is None

    def test_decode_rejects_short_row(self):
        payload = dircache.encode_entries(_sample())
        payload["rows"][0] = payload["rows"][0][:-1]
        assert dircache.decode_entries(payload) is None


# ------------------------------------------------------------------ 读写 / 隔离

class TestGetPut:
    def test_miss_then_hit(self, cachedb):
        assert dircache.get(HOST_A, SHARE, "Autorun") is None
        dircache.put(HOST_A, SHARE, "Autorun", _sample())
        assert dircache.get(HOST_A, SHARE, "Autorun") == _sample()

    def test_backend_isolation_by_device(self, cachedb):
        """换设备天然隔离:同一个共享名在两台设备上内容完全不同。"""
        dircache.put(HOST_A, SHARE, "Autorun", _sample())
        assert dircache.get(HOST_B, SHARE, "Autorun") is None
        dircache.put(HOST_B, SHARE, "Autorun", [])
        assert dircache.get(HOST_B, SHARE, "Autorun") == []
        assert dircache.get(HOST_A, SHARE, "Autorun") == _sample()

    def test_share_isolation(self, cachedb):
        dircache.put(HOST_A, SHARE, "", _sample())
        assert dircache.get(HOST_A, "TF Images", "") is None

    def test_path_normalization_shares_one_row(self, cachedb):
        dircache.put(HOST_A, SHARE, "Autorun/Light", _sample())
        assert dircache.get(HOST_A, SHARE, "Autorun\\Light") == _sample()

    def test_put_overwrites(self, cachedb):
        dircache.put(HOST_A, SHARE, "d", _sample())
        dircache.put(HOST_A, SHARE, "d", [])
        assert dircache.get(HOST_A, SHARE, "d") == []

    def test_get_with_age_reports_seconds(self, cachedb):
        dircache.put(HOST_A, SHARE, "d", _sample())
        got = dircache.get_with_age(HOST_A, SHARE, "d")
        assert got is not None
        entries, age = got
        assert entries == _sample()
        assert 0.0 <= age < 5.0

    def test_ttl_expires(self, cachedb):
        dircache.put(HOST_A, SHARE, "d", _sample())
        time.sleep(0.01)
        assert dircache.get(HOST_A, SHARE, "d", ttl=0.0) is None
        # 过期行被 metacache 就地删掉,再读也没有
        assert dircache.get(HOST_A, SHARE, "d") is None

    def test_use_cache_false_never_reads(self, cachedb):
        dircache.put(HOST_A, SHARE, "d", _sample())
        assert dircache.get(HOST_A, SHARE, "d", use_cache=False) is None

    def test_put_async_lands(self, cachedb):
        t = dircache.put_async(HOST_A, SHARE, "d", _sample())
        assert t is not None
        t.join(timeout=10)
        assert dircache.get(HOST_A, SHARE, "d") == _sample()

    def test_broken_cache_does_not_raise(self, cachedb, monkeypatch):
        """缓存挂了只能降级成"没有缓存",绝不能把异常抛给浏览页。"""
        def boom(*a, **kw):
            raise RuntimeError("库炸了")
        monkeypatch.setattr(metacache, "get", boom)
        monkeypatch.setattr(metacache, "put", boom)
        dircache.put(HOST_A, SHARE, "d", _sample())     # 不抛
        assert dircache.get(HOST_A, SHARE, "d") is None

    def test_accepts_client_object_as_backend(self, cachedb):
        client = _FakeDirClient({}, HOST_B)
        dircache.put(client, SHARE, "d", _sample())
        assert dircache.get(HOST_B, SHARE, "d") == _sample()


# ------------------------------------------------------------------ 版本 / 指纹

class TestVersioning:
    def test_kind_carries_version_and_schema_sig(self):
        assert dircache.DIR_KIND.startswith(f"dirlist/{dircache.DIR_CACHE_VER}/")
        assert metacache.dc_schema_sig(RemoteEntry) in dircache.DIR_KIND
        assert dircache.TREE_KIND.startswith(f"dirtree/{dircache.TREE_CACHE_VER}/")
        assert metacache.dc_schema_sig(TreeNode) in dircache.TREE_KIND
        assert not dircache.DIR_KIND.startswith("dirlist/1/nosig")

    def test_version_bump_invalidates_dirlist(self, cachedb, monkeypatch):
        dircache.put(HOST_A, SHARE, "d", _sample())
        assert dircache.get(HOST_A, SHARE, "d") is not None
        monkeypatch.setattr(dircache, "DIR_KIND", "dirlist/2/" + "x" * 12)
        assert dircache.get(HOST_A, SHARE, "d") is None

    def test_version_bump_invalidates_tree(self, cachedb, monkeypatch):
        tree = TreeNode(name="root", path="", is_dir=True)
        dircache.put_tree(HOST_A, SHARE, "", tree)
        assert dircache.get_tree(HOST_A, SHARE, "") is not None
        monkeypatch.setattr(dircache, "TREE_KIND", "dirtree/2/" + "x" * 12)
        assert dircache.get_tree(HOST_A, SHARE, "") is None

    def test_schema_change_changes_kind(self):
        """字段一变指纹就变 —— 旧 payload 自然全部未命中。"""
        import dataclasses

        @dataclasses.dataclass(frozen=True)
        class RemoteEntryV2:            # 假装 RemoteEntry 多了一个字段
            share: str
            path: str
            name: str
            is_dir: bool
            size: int
            mtime: float
            ctime: float
            atime: float
            attributes: int
            owner: str
        assert (metacache.dc_schema_sig(RemoteEntryV2)
                != metacache.dc_schema_sig(RemoteEntry))


# ------------------------------------------------------------------ 失效 API

class TestInvalidate:
    def _seed(self):
        for host in (HOST_A, HOST_B):
            for share in (SHARE, "TF Images"):
                for path in ("", "Autorun", "Autorun\\Bias", "Plan\\Light"):
                    dircache.put(host, share, path, _sample())

    def test_invalidate_single_dir(self, cachedb):
        self._seed()
        dircache.invalidate(HOST_A, SHARE, "Autorun", subtree=False, trees=False)
        assert dircache.get(HOST_A, SHARE, "Autorun") is None
        assert dircache.get(HOST_A, SHARE, "Autorun\\Bias") is not None
        assert dircache.get(HOST_B, SHARE, "Autorun") is not None

    def test_invalidate_subtree(self, cachedb):
        self._seed()
        dircache.invalidate(HOST_A, SHARE, "Autorun", subtree=True, trees=False)
        assert dircache.get(HOST_A, SHARE, "Autorun") is None
        assert dircache.get(HOST_A, SHARE, "Autorun\\Bias") is None
        assert dircache.get(HOST_A, SHARE, "Plan\\Light") is not None
        assert dircache.get(HOST_B, SHARE, "Autorun\\Bias") is not None

    def test_invalidate_whole_share(self, cachedb):
        self._seed()
        dircache.invalidate(HOST_A, SHARE)
        for p in ("", "Autorun", "Autorun\\Bias", "Plan\\Light"):
            assert dircache.get(HOST_A, SHARE, p) is None
        assert dircache.get(HOST_A, "TF Images", "Autorun") is not None
        assert dircache.get(HOST_B, SHARE, "Autorun") is not None

    def test_invalidate_whole_device(self, cachedb):
        self._seed()
        dircache.invalidate(HOST_A)
        assert dircache.get(HOST_A, SHARE, "Autorun") is None
        assert dircache.get(HOST_A, "TF Images", "Autorun") is None
        assert dircache.get(HOST_B, SHARE, "Autorun") is not None

    def test_invalidate_everything(self, cachedb):
        self._seed()
        dircache.invalidate()
        assert dircache.get(HOST_A, SHARE, "Autorun") is None
        assert dircache.get(HOST_B, "TF Images", "Plan\\Light") is None

    def test_invalidate_root_subtree_clears_share(self, cachedb):
        self._seed()
        dircache.invalidate(HOST_A, SHARE, "", subtree=True, trees=False)
        for p in ("", "Autorun", "Autorun\\Bias"):
            assert dircache.get(HOST_A, SHARE, p) is None

    def test_invalidate_escapes_like_underscore(self, cachedb):
        """``_`` 是 LIKE 的单字符通配 —— 不转义会误删邻居目录的缓存。"""
        dircache.put(HOST_A, SHARE, "a_b\\c", _sample())
        dircache.put(HOST_A, SHARE, "axb\\c", _sample())
        dircache.invalidate(HOST_A, SHARE, "a_b", subtree=True, trees=False)
        assert dircache.get(HOST_A, SHARE, "a_b\\c") is None
        assert dircache.get(HOST_A, SHARE, "axb\\c") is not None

    def test_invalidate_escapes_like_percent(self, cachedb):
        """``%`` 同理(共享名/目录名里出现完全合法)。"""
        dircache.put(HOST_A, "a%b", "x", _sample())
        dircache.put(HOST_A, "aQQb", "x", _sample())
        dircache.invalidate(HOST_A, "a%b")
        assert dircache.get(HOST_A, "a%b", "x") is None
        assert dircache.get(HOST_A, "aQQb", "x") is not None

    def test_invalidate_clears_trees_for_whole_share(self, cachedb):
        """占用树里祖先的数字是后代聚合来的:任何一处改动都按共享整体清。"""
        tree = TreeNode(name="root", path="", is_dir=True)
        dircache.put_tree(HOST_A, SHARE, "", tree)
        dircache.put_tree(HOST_A, SHARE, "Autorun", tree)
        dircache.put_tree(HOST_B, SHARE, "", tree)
        dircache.invalidate(HOST_A, SHARE, "Autorun\\Bias\\deep")
        assert dircache.get_tree(HOST_A, SHARE, "") is None
        assert dircache.get_tree(HOST_A, SHARE, "Autorun") is None
        assert dircache.get_tree(HOST_B, SHARE, "") is not None

    def test_invalidate_trees_false_keeps_trees(self, cachedb):
        tree = TreeNode(name="root", path="", is_dir=True)
        dircache.put_tree(HOST_A, SHARE, "", tree)
        dircache.invalidate(HOST_A, SHARE, "Autorun", trees=False)
        assert dircache.get_tree(HOST_A, SHARE, "") is not None


# ------------------------------------------------------------------ NOCACHE 旁路

class TestNoCacheBypass:
    def test_reads_bypassed(self, cachedb, monkeypatch):
        dircache.put(HOST_A, SHARE, "d", _sample())
        assert dircache.get(HOST_A, SHARE, "d") is not None
        monkeypatch.setenv("ASTRO_SMB_GUI_NOCACHE", "1")
        assert metacache.bypass_reads() is True
        assert dircache.get(HOST_A, SHARE, "d") is None
        assert dircache.get_with_age(HOST_A, SHARE, "d") is None

    def test_tree_reads_bypassed(self, cachedb, monkeypatch):
        dircache.put_tree(HOST_A, SHARE, "", TreeNode(name="r", path="", is_dir=True))
        assert dircache.get_tree(HOST_A, SHARE, "") is not None
        monkeypatch.setenv("ASTRO_SMB_GUI_NOCACHE", "1")
        assert dircache.get_tree(HOST_A, SHARE, "") is None

    def test_writes_still_happen(self, cachedb, monkeypatch):
        """旁路只作用于**读** —— 写照旧,这样关掉开关后立刻就是热的。"""
        monkeypatch.setenv("ASTRO_SMB_GUI_NOCACHE", "1")
        dircache.put(HOST_A, SHARE, "d", _sample())
        monkeypatch.delenv("ASTRO_SMB_GUI_NOCACHE")
        assert dircache.get(HOST_A, SHARE, "d") == _sample()

    def test_zero_and_empty_do_not_bypass(self, cachedb, monkeypatch):
        dircache.put(HOST_A, SHARE, "d", _sample())
        for v in ("", "0"):
            monkeypatch.setenv("ASTRO_SMB_GUI_NOCACHE", v)
            assert dircache.get(HOST_A, SHARE, "d") is not None


# ------------------------------------------------------------------ 对账原语

class TestReconcilePrimitives:
    def test_same_true_for_identical(self):
        assert dircache.same(_sample(), _sample()) is True

    def test_same_false_on_size_change(self):
        """只比 name 的实现会在这里放过一个被覆盖重传的文件。"""
        a = _sample()
        b = _sample()
        b[2] = RemoteEntry(**{**b[2].__dict__, "size": 999})
        assert dircache.same(a, b) is False

    def test_same_false_on_mtime_change(self):
        a = _sample()
        b = _sample()
        b[0] = RemoteEntry(**{**b[0].__dict__, "mtime": 1.0})
        assert dircache.same(a, b) is False

    def test_same_false_on_none(self):
        assert dircache.same(None, _sample()) is False
        assert dircache.same(_sample(), None) is False

    def test_diff_summary_counts(self):
        old = _sample()
        new = list(old[:2]) + [_entry("new.fit", path="new.fit")]
        added, removed, changed = dircache.diff_summary(old, new)
        assert (added, removed, changed) == (1, 1, 0)

    def test_diff_summary_changed(self):
        old = _sample()
        new = list(old)
        new[1] = RemoteEntry(**{**new[1].__dict__, "mtime": 42.0})
        assert dircache.diff_summary(old, new) == (0, 0, 1)

    def test_age_text_buckets(self):
        assert dircache.age_text(0) == tr("刚刚")
        assert dircache.age_text(44) == tr("刚刚")
        assert dircache.age_text(120) == tr("{0} 分钟前", 2)
        assert dircache.age_text(7200) == tr("{0} 小时前", 2)
        assert dircache.age_text(3 * 86400) == tr("{0} 天前", 3)

    def test_age_text_is_bmp_only(self):
        """UI 文案禁止星平面字符(win32more 的 HSTRING 长度坑,见 docs/DEVELOPMENT.md)。"""
        for s in (dircache.age_text(0), dircache.age_text(120),
                  dircache.age_text(7200), dircache.age_text(999999)):
            assert all(ord(ch) < 0x10000 for ch in s)


# ------------------------------------------------------------------ 端到端:乐观显示 + 后台对账

class TestOptimisticFlow:
    """用假 client 走一遍浏览页的两段式流程(不起 GUI)。"""

    LAYOUT_V1 = None    # 在用例里构造

    def _layout(self, extra=False):
        root = [_entry("Autorun", path="Autorun", is_dir=True, size=0,
                       attributes=0x10)]
        bias = [_entry("a.fit", path="Autorun\\a.fit", size=100)]
        if extra:
            bias.append(_entry("b.fit", path="Autorun\\b.fit", size=200))
        return {"": root, "Autorun": bias}

    def test_first_visit_costs_one_listdir_and_fills_cache(self, cachedb):
        c = _FakeDirClient(self._layout())
        entries = c.listdir(SHARE, "Autorun")
        dircache.put(c, SHARE, "Autorun", entries)
        assert c.listdirs == ["Autorun"]
        assert dircache.get(c, SHARE, "Autorun") == entries

    def test_second_visit_costs_zero_listdir(self, cachedb):
        c = _FakeDirClient(self._layout())
        dircache.put(c, SHARE, "Autorun", c.listdir(SHARE, "Autorun"))
        c.listdirs.clear()
        assert dircache.get(c, SHARE, "Autorun") is not None
        assert c.listdirs == []          # 秒开:一次网络往返都没有

    def test_reconcile_agrees_keeps_view(self, cachedb):
        c = _FakeDirClient(self._layout())
        cached = c.listdir(SHARE, "Autorun")
        dircache.put(c, SHARE, "Autorun", cached)
        fresh = c.clone().listdir(SHARE, "Autorun")      # 后台对账
        assert dircache.same(cached, fresh) is True

    def test_reconcile_detects_new_file_and_refreshes_cache(self, cachedb):
        c = _FakeDirClient(self._layout())
        dircache.put(c, SHARE, "Autorun", c.listdir(SHARE, "Autorun"))
        cached = dircache.get(c, SHARE, "Autorun")
        # 设备上又拍了一张 —— 对账线程拿到的是新内容
        c.layout = self._layout(extra=True)
        fresh = c.clone().listdir(SHARE, "Autorun")
        assert dircache.same(cached, fresh) is False
        assert dircache.diff_summary(cached, fresh) == (1, 0, 0)
        dircache.put(c, SHARE, "Autorun", fresh)         # 对账后回写
        assert dircache.get(c, SHARE, "Autorun") == fresh

    def test_invalidate_after_delete_forces_network(self, cachedb):
        c = _FakeDirClient(self._layout(extra=True))
        dircache.put(c, SHARE, "Autorun", c.listdir(SHARE, "Autorun"))
        dircache.invalidate(c.host, SHARE, "Autorun", subtree=True)
        assert dircache.get(c, SHARE, "Autorun") is None

    def test_failed_listdir_does_not_poison_cache(self, cachedb):
        c = _FakeDirClient(self._layout())
        with pytest.raises(SmbClientError):
            c.listdir(SHARE, "不存在")
        assert dircache.get(c, SHARE, "不存在") is None


# ------------------------------------------------------------------ 占用树

class TestTreeCache:
    def _tree(self) -> TreeNode:
        f1 = TreeNode(name="a.fit", path="Plan\\Light\\a.fit", is_dir=False,
                      size=100, file_count=1)
        f2 = TreeNode(name="b.fit", path="Plan\\Light\\b.fit", is_dir=False,
                      size=50, file_count=1)
        light = TreeNode(name="Light", path="Plan\\Light", is_dir=True,
                         size=150, file_count=2, children=[f1, f2])
        root = TreeNode(name="Plan", path="Plan", is_dir=True,
                        size=150, file_count=2, children=[light])
        return root

    def test_roundtrip_structure_and_paths(self):
        """path 不入库,由父路径 + name 推出 —— 必须逐节点核对推得对不对。"""
        got = dircache.decode_tree(dircache.encode_tree(self._tree()))
        assert got is not None
        assert (got.name, got.path, got.size, got.file_count) == ("Plan", "Plan", 150, 2)
        light = got.children[0]
        assert (light.name, light.path, light.is_dir) == ("Light", "Plan\\Light", True)
        assert [c.path for c in light.children] == ["Plan\\Light\\a.fit",
                                                    "Plan\\Light\\b.fit"]
        assert [c.size for c in light.children] == [100, 50]
        assert all(c.file_count == 1 for c in light.children)

    def test_roundtrip_at_share_root(self):
        root = TreeNode(name=SHARE, path="", is_dir=True, size=10, file_count=1,
                        children=[TreeNode(name="x.fit", path="x.fit",
                                           is_dir=False, size=10, file_count=1)])
        got = dircache.decode_tree(dircache.encode_tree(root))
        assert got is not None and got.path == ""
        assert got.children[0].path == "x.fit"

    def test_partial_tree_not_encoded(self):
        t = self._tree()
        t.partial = True
        t.error_count = 3
        assert dircache.encode_tree(t) is None

    def test_partial_tree_not_stored(self, cachedb):
        t = self._tree()
        t.partial = True
        assert dircache.put_tree(HOST_A, SHARE, "Plan", t) is False
        assert dircache.get_tree(HOST_A, SHARE, "Plan") is None

    def test_oversize_tree_not_stored(self, cachedb, monkeypatch):
        monkeypatch.setattr(dircache, "TREE_MAX_NODES", 3)
        assert dircache.put_tree(HOST_A, SHARE, "Plan", self._tree()) is False

    def test_tree_get_with_age(self, cachedb):
        dircache.put_tree(HOST_A, SHARE, "Plan", self._tree())
        got = dircache.get_tree(HOST_A, SHARE, "Plan")
        assert got is not None
        tree, age = got
        assert tree.size == 150 and 0.0 <= age < 5.0

    def test_tree_ttl_expires(self, cachedb):
        dircache.put_tree(HOST_A, SHARE, "Plan", self._tree())
        time.sleep(0.01)
        assert dircache.get_tree(HOST_A, SHARE, "Plan", ttl=0.0) is None

    def test_tree_device_isolation(self, cachedb):
        dircache.put_tree(HOST_A, SHARE, "Plan", self._tree())
        assert dircache.get_tree(HOST_B, SHARE, "Plan") is None

    def test_decode_tree_rejects_garbage(self):
        assert dircache.decode_tree(None) is None
        assert dircache.decode_tree({}) is None
        assert dircache.decode_tree({"root": ["x", 0, 1]}) is None   # 根必须是目录
        assert dircache.decode_tree({"root": "nope"}) is None

    def test_tree_use_cache_false(self, cachedb):
        dircache.put_tree(HOST_A, SHARE, "Plan", self._tree())
        assert dircache.get_tree(HOST_A, SHARE, "Plan", use_cache=False) is None


# ------------------------------------------------------------------ 与真实后端对账

class TestAgainstLocalBackend:
    """用 LocalBackend(纯本地目录)产出真实的 listdir / dir_tree 结果做往返。"""

    @pytest.fixture()
    def card(self, tmp_path):
        root = tmp_path / "card"
        for d in ("Autorun/Bias", "Autorun/Flat", "Plan/Light/M 8", "中文 目录"):
            (root / d).mkdir(parents=True, exist_ok=True)
        for i, rel in enumerate([
            "Autorun/Bias/Bias_1.0s_Bin1_20260701-201530_0001.fit",
            "Autorun/Flat/Flat_2.0s_Bin1_20260701-071530_0001.fit",
            "Plan/Light/M 8/Light_M 8_300.0s_Bin1_20260701-221530_0001.fit",
            "中文 目录/说明.txt",
        ]):
            (root / rel).write_bytes(b"x" * (600 + i))
        return root

    def test_listdir_roundtrip_field_exact(self, card):
        b = LocalBackend(card, label="ASIAIR")
        share = b.list_shares()[0].name
        for path in ("", "Autorun", "Autorun\\Bias", "Plan\\Light\\M 8", "中文 目录"):
            src = b.listdir(share, path)
            got = dircache.decode_entries(dircache.encode_entries(src))
            assert got == src, path

    def test_listdir_cached_hit_matches_backend(self, cachedb, card):
        b = LocalBackend(card, label="ASIAIR")
        share = b.list_shares()[0].name
        dircache.put(b, share, "Autorun", b.listdir(share, "Autorun"))
        assert dircache.get(b, share, "Autorun") == b.listdir(share, "Autorun")
        assert dircache.backend_of(b) == b.host

    def test_dir_tree_roundtrip(self, cachedb, card):
        b = LocalBackend(card, label="ASIAIR")
        share = b.list_shares()[0].name
        tree = b.dir_tree(share, "")
        assert dircache.put_tree(b, share, "", tree) is True
        got = dircache.get_tree(b, share, "")
        assert got is not None
        back, _age = got

        def walk(n):
            yield n
            for c in n.children:
                yield from walk(c)

        a = {(x.path, x.is_dir, x.size, x.file_count) for x in walk(tree)}
        c = {(x.path, x.is_dir, x.size, x.file_count) for x in walk(back)}
        assert a == c
        assert back.size == tree.size > 0

    def test_dir_tree_nested_root_paths(self, cachedb, card):
        b = LocalBackend(card, label="ASIAIR")
        share = b.list_shares()[0].name
        tree = b.dir_tree(share, "Plan\\Light")
        dircache.put_tree(b, share, "Plan\\Light", tree)
        got = dircache.get_tree(b, share, "Plan\\Light")
        assert got is not None
        assert got[0].path == "Plan\\Light"
        assert got[0].children[0].path == "Plan\\Light\\M 8"


# ------------------------------------------------------------------ metacache 前缀失效

class TestMetacachePrefix:
    def test_prefix_delete_scoped_to_kind(self, tmp_path):
        mc = metacache.MetaCache(tmp_path / "m.db")
        try:
            mc.put("k1", "dev", "S|a\\b", {"x": 1})
            mc.put("k2", "dev", "S|a\\b", {"x": 1})
            assert mc.invalidate_prefix("k1", "dev", "S|a\\") == 1
            assert mc.get("k1", "dev", "S|a\\b") is None
            assert mc.get("k2", "dev", "S|a\\b") is not None
        finally:
            mc.close()

    def test_prefix_delete_all_backends(self, tmp_path):
        mc = metacache.MetaCache(tmp_path / "m.db")
        try:
            mc.put("k", "d1", "S|a", {"x": 1})
            mc.put("k", "d2", "S|a", {"x": 1})
            assert mc.invalidate_prefix("k", None, "S|") == 2
        finally:
            mc.close()

    def test_prefix_empty_clears_kind(self, tmp_path):
        mc = metacache.MetaCache(tmp_path / "m.db")
        try:
            mc.put("k", "d", "anything", {"x": 1})
            assert mc.invalidate_prefix("k", "d", "") == 1
        finally:
            mc.close()


def test_env_is_clean():
    """本模块不得给后续测试留下 NOCACHE 开关。"""
    assert os.environ.get("ASTRO_SMB_GUI_NOCACHE", "") in ("", "0")
