"""设备管理页(astro_smb_gui._devices)的离线单测。

只测**纯数据层**:设备记录读写/迁移、卷信息格式化、两类卡片的数据组装、
"已拔出"判定、空状态、手动添加的路径校验,外加静态 BMP-only 扫描。
不建任何 XAML 控件(那需要 XAML 消息泵),只 import 模块本身
—— win32more 是延迟绑定,import 不触发 DLL 加载(与 test_fitsimage 同做法)。

页面对象本身也能测:用 ``object.__new__(DevicesPage)`` 跳过 ``__init__``
(它要 XamlReader),手工塞上假控件 —— 这样线程编排/懒渲染/缓存这些
**不碰 XAML 的逻辑**照样能钉死(见 :class:`_FakePage` 工厂)。
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from astro_smb_gui import _devices as D
from astro_smb_gui import devices, volumes
from tests.support import tr

NOW = 1_800_000_000.0


def _smb_rec(**kw) -> dict:
    base = {"host": "192.0.2.225", "kind": "smb", "path": "",
            "name": "ASIAIR", "os": "Samba 4.9.5-Debian",
            "dialect": "SMB 3.1.1", "shares": 3,
            "first_seen": NOW - 86400 * 30, "last_ok": NOW - 600}
    base.update(kw)
    return base


def _local_rec(**kw) -> dict:
    base = {"host": "E:\\", "kind": "local", "path": "E:\\", "name": "ASIAIR",
            "os": "ZWO 卡 · 命中 8 项", "dialect": "本地磁盘", "shares": 1,
            "first_seen": NOW - 86400, "last_ok": NOW - 60}
    base.update(kw)
    return base



class _Widget:
    """假控件:属性随便赋值,记录被写过哪些值(不碰 WinRT)。"""

    def __init__(self, **kw):
        self.Text = ""
        self.IsEnabled = True
        self.IsActive = False
        self.Visibility = None
        self.Foreground = None
        self.Width = 0.0
        self.Fill = None
        self.writes: list[tuple] = []
        for k, v in kw.items():
            object.__setattr__(self, k, v)

    def __setattr__(self, name, value):
        if name != "writes" and "writes" in self.__dict__:
            self.writes.append((name, value))
        object.__setattr__(self, name, value)


class _FakeShell:
    """页面用到的那一小撮 shell 接口(``ui`` 同步执行,便于单测断言)。"""

    def __init__(self, **kw):
        self.hb: dict = {}
        self._hb_host = ""
        self._dev_rtt: dict = {}
        self.errors: list[str] = []
        self.infos: list[str] = []
        self.done = threading.Event()
        for k, v in kw.items():
            setattr(self, k, v)

    def ui(self, fn, *args):
        fn(*args)
        self.done.set()

    def error(self, text):
        self.errors.append(text)

    def info(self, text, action_text="", on_action=None):
        self.infos.append(text)


def _fake_page(shell=None, **kw):
    """不跑 ``__init__`` 造一个 DevicesPage(XAML 全换成假控件)。"""
    page = object.__new__(D.DevicesPage)
    page.shell = shell or _FakeShell()
    page.root = _Widget(Parent=None)
    for name in ("refresh_ring", "status_text", "add_box", "add_btn", "add_hint",
                 "record_head", "empty_card", "go_scan_btn", "card_host",
                 "vol_head", "vol_host", "refresh_btn"):
        setattr(page, name, _Widget())
    page._cards = {}
    page._laid = {}
    page._state = {"rtt": {}, "local": {}, "vols": [], "ts": 0.0}
    page._gen = 0
    page._refreshing = False
    page._last_try = 0.0
    page._stop = threading.Event()
    page._shown = True
    page._visible = True
    page._parent_ok = False
    page._tick_thread = None
    page._recs_cache = None
    page._recs_at = 0.0
    page._status_summary = ""
    page._status_shown = ""
    page._adding = False
    page._tone = {k: object() for k in ("good", "warn", "bad", "dim")}
    page._dot = dict(page._tone)
    page.laid: dict = {}                # slot → 最近一次铺进去的卡片模型
    page.refreshes: list = []           # _start_refresh 被叫了几次
    page._lay_out = lambda slot, host, models: page.laid.__setitem__(slot, models)
    page._start_refresh = lambda: page.refreshes.append(1)
    for k, v in kw.items():
        setattr(page, k, v)
    return page


def _pairs(model: dict, group_name: str) -> list[tuple]:
    for _glyph, name, pairs in model["groups"]:
        if name == group_name:
            return pairs
    return []


def _kv(model: dict, group_name: str) -> dict:
    return {p[0]: p[1] for p in _pairs(model, group_name)}


# ---------------------------------------------------------------- 时间格式化

class TestRelTime:
    def test_never_when_missing(self):
        assert D.rel_time(0) == tr("从未")
        assert D.rel_time(None) == tr("从未")
        assert D.rel_time("坏数据") == tr("从未")

    def test_scales(self):
        assert D.rel_time(NOW - 5, NOW) == tr("刚刚")
        assert D.rel_time(NOW - 700, NOW) == tr("{0} 分钟前", 11)
        assert D.rel_time(NOW - 7200, NOW) == tr("{0} 小时前", 2)
        assert D.rel_time(NOW - 86400 * 3, NOW) == tr("{0} 天前", 3)
        assert D.rel_time(NOW - 86400 * 60, NOW) == tr("{0} 个月前", 2)
        assert D.rel_time(NOW - 86400 * 400, NOW) == tr("{0} 年前", 1)

    def test_clock_skew_is_not_negative(self):
        # 时钟回拨:记录时间比"现在"还晚,不能显示 "-3 分钟前"
        assert D.rel_time(NOW + 300, NOW) == tr("刚刚")

    def test_abs_time_blank_when_missing(self):
        assert D.abs_time(0) == "" and D.abs_time(None) == ""
        assert D.abs_time(NOW).startswith("20")


# ---------------------------------------------------------------- 容量格式化

class TestCapacity:
    def test_percent_and_tone(self):
        assert D.usage_percent(100, 25) == 75.0
        assert D.usage_percent(0, 0) == 0.0
        assert D.usage_tone(10.0) == "good"
        assert D.usage_tone(75.0) == "warn"
        assert D.usage_tone(95.0) == "bad"

    def test_pairs_have_bar_gadget(self):
        pairs = D.capacity_pairs(1 << 30, 1 << 28)     # 1GB 总 / 256MB 空
        assert [p[0] for p in pairs] == [tr("已用"), tr("可用"), tr("总量")]
        used = pairs[0]
        assert used[5] == ("usagebar", pytest.approx(75.0))
        assert used[4] == "warn"                       # 75% → 需留意
        assert "占 75%" in used[2]

    def test_zero_total_gives_placeholder_not_crash(self):
        pairs = D.capacity_pairs(0, 0)
        assert len(pairs) == 1 and pairs[0][1] == "—"

    def test_free_larger_than_total_is_clamped(self):
        assert D.usage_percent(100, 999) == 0.0


# ---------------------------------------------------------------- ZWO 特征

class TestZwoPairs:
    def test_unknown_when_none(self):
        pairs = D.zwo_pairs(None)
        assert pairs[0][1] == tr("未检测") and pairs[0][4] == "dim"

    def test_no_hits(self):
        pairs = D.zwo_pairs([])
        assert pairs[0][1] == tr("{n} / {total} 项", n=0,
                             total=len(volumes.ZWO_DIRS))
        assert pairs[0][4] == "dim" and len(pairs) == 1

    def test_below_threshold_warns(self):
        pairs = D.zwo_pairs(["Autorun", "Plan"])
        assert pairs[0][4] == "warn" and str(volumes.MIN_HITS) in pairs[0][2]
        assert pairs[1][1] == "Autorun / Plan"

    def test_full_hit_is_good_and_ordered(self):
        hits = ["Plan", "log", "Autorun"]
        pairs = D.zwo_pairs(hits)
        assert pairs[0][4] == "good"
        # 按 ZWO_DIRS 的官方顺序展示,不是 scandir 的随机顺序
        assert pairs[1][1] == "Autorun / Plan / log"


# ---------------------------------------------------------------- 状态措辞

class TestStatusWording:
    """docs/DEVELOPMENT.md §2:只有当前连接(真 SMB 心跳)那台才配说"在线"。"""

    def test_connected_alive_says_online(self):
        hb = {"host": "h", "alive": True, "rtt_ms": 12.3}
        assert D.smb_status("h", connected=True, hb=hb, rtt={}) == \
            (tr("● 在线 {ms:.0f} ms", ms=12), "good")

    def test_connected_dead_says_offline(self):
        hb = {"host": "h", "alive": False}
        assert D.smb_status("h", connected=True, hb=hb, rtt={}) == (tr("● 离线"), "dim")

    def test_probed_only_says_port_reachable(self):
        text, tone = D.smb_status("h", connected=False, hb={}, rtt={"h": 5.0})
        assert text == tr("● 端口可达 {ms:.0f} ms", ms=5) and tone == "good"
        # 路由器假 ACK:绝不能说"在线"。**比的是它没走那一条消息**,
        # 不是"文字里没有这两个字" —— 后者一翻译就永远成立。
        assert text != tr("● 在线 {ms:.0f} ms", ms=5)

    def test_probe_failed_is_offline(self):
        assert D.smb_status("h", connected=False, hb={}, rtt={"h": None}) == \
            (tr("● 离线"), "dim")

    def test_unprobed(self):
        assert D.smb_status("h", connected=False, hb={}, rtt={}) == (tr("○ 未探测"), "dim")

    def test_local_uses_plugged_wording_not_offline(self):
        assert D.local_status(True) == (tr("● 已插入"), "good")
        assert D.local_status(False) == (tr("● 已拔出"), "dim")
        assert D.local_status(None) == (tr("○ 未检测"), "dim")
        for text, _tone in (D.local_status(True), D.local_status(False),
                            D.local_status(None)):
            assert "离线" not in text and "在线" not in text


# ---------------------------------------------------------------- SMB 卡片

class TestSmbCard:
    def test_fields(self):
        m = D.smb_card(_smb_rec(), connected=False, hb={}, rtt={}, now=NOW)
        assert m["kind"] == devices.KIND_SMB and m["host"] == "192.0.2.225"
        assert m["title"] == "ASIAIR" and m["sub"] == "192.0.2.225"
        dev = _kv(m, tr("设备"))
        assert dev[tr("地址")] == "192.0.2.225"
        assert dev[tr("系统")] == "Samba 4.9.5-Debian"      # 记录里的 os 终于有 UI 出口
        assert dev[tr("协议")] == "SMB 3.1.1" and dev[tr("共享数")] == tr("{shares} 个", shares=3)
        assert _kv(m, tr("记录"))[tr("最近连接")] == tr("{0} 分钟前", 10)

    def test_unknown_shares_not_shown_as_zero(self):
        assert _kv(D.smb_card(_smb_rec(shares=None), now=NOW), tr("设备"))[tr("共享数")] \
            == tr("未知")

    def test_badges(self):
        m = D.smb_card(_smb_rec(), connected=True,
                       hb={"host": "192.0.2.225", "alive": True, "rtt_ms": 9.0},
                       now=NOW)
        styles = [s for _t, s in m["badges"]]
        assert "conn" in styles and "smb" in styles and "zwo" in styles

    def test_connected_device_cannot_be_forgotten(self):
        m = D.smb_card(_smb_rec(), connected=True,
                       hb={"host": "192.0.2.225", "alive": True}, now=NOW)
        assert m["forget"][0] is False and m["forget"][2]
        assert m["connect"][1] == tr("重新连接")

    def test_heartbeat_counters_surface_when_connected(self):
        hb = {"host": "192.0.2.225", "alive": True, "rtt_ms": 8.0,
              "checks": 42, "fails": 2}
        m = D.smb_card(_smb_rec(), connected=True, hb=hb, now=NOW)
        # 心跳计数在 live(单个 TextBlock),**不在 groups**(见 TestHeartbeatCost)
        assert "心跳 42 次" in m["live"] and "失败 2 次" in m["live"]
        assert "心跳" not in _kv(m, tr("记录"))

    def test_empty_record_does_not_crash(self):
        m = D.smb_card({"host": "1.2.3.4"}, now=NOW)
        assert m["title"] == "1.2.3.4"
        assert _kv(m, tr("设备"))[tr("协议")] == "—"
        assert _kv(m, tr("记录"))[tr("最近连接")] == tr("从未")


# ---------------------------------------------------------------- 本地卡片

class TestLocalCard:
    def _facts(self, **kw):
        f = {"root": "E:\\", "present": True, "hits": list(volumes.ZWO_DIRS),
             "label": "ASIAIR", "drive": "E:", "fs": "exFAT",
             "kind_text": "可移动磁盘", "total": 64 << 30, "free": 16 << 30,
             "volume_root": "E:\\"}
        f.update(kw)
        return f

    def test_present_card(self):
        m = D.local_card(_local_rec(), facts=self._facts(), now=NOW)
        assert m["kind"] == devices.KIND_LOCAL and m["title"] == "ASIAIR (E:)"
        assert m["status"] == (tr("● 已插入"), "good")
        disk = _kv(m, tr("磁盘"))
        assert disk[tr("路径")] == "E:\\" and disk[tr("卷标")] == "ASIAIR"
        # `kind_text` 是**这条用例自己喂进去的**(上面的 fake),卡片只原样透传 ——
        # 所以这里比的是那个字面量,不是 `tr(...)`。拿 tr 去比就是在测翻译,
        # 而不是在测"卡片有没有把它显示出来"。
        assert disk[tr("文件系统")] == "exFAT"
        assert disk[tr("磁盘类型")] == "可移动磁盘"
        assert _kv(m, tr("容量"))[tr("总量")] == "64.00 GB"
        assert _kv(m, tr("ZWO 特征"))[tr("特征目录")] == tr("{n} / {total} 项", n=8, total=8)
        assert m["connect"][0] is True and m["open"][0] is True

    def test_unplugged_card(self):
        m = D.local_card(_local_rec(),
                         facts=self._facts(present=False, hits=None), now=NOW)
        assert m["status"] == (tr("● 已拔出"), "dim")
        assert (tr("已拔出"), "gone") in m["badges"]
        assert _kv(m, tr("容量"))[tr("容量")] == "—"      # 拔了就别显示上次的容量
        assert m["connect"][0] is False and m["open"][0] is False
        assert m["connect"][2]                     # 有解释性提示

    def test_never_refreshed_is_unknown_not_unplugged(self):
        m = D.local_card(_local_rec(), facts=None, now=NOW)
        assert m["status"] == (tr("○ 未检测"), "dim")
        assert _kv(m, tr("ZWO 特征"))[tr("特征目录")] == tr("未检测")

    def test_unknown_volume_does_not_invent_drive_or_capacity(self):
        """卡拔了/认不出所在卷时不能编造盘符与容量(标题也别塞整条路径)。"""
        facts = D.local_facts("D:\\ASIAIR", None, present=False, hits=None)
        assert facts["drive"] == "" and facts["total"] == 0
        m = D.local_card(_local_rec(host="D:\\ASIAIR", path="D:\\ASIAIR",
                                    name="旧卡"), facts=facts, now=NOW)
        assert m["title"] == "旧卡 (ASIAIR)"
        assert "D:\\ASIAIR" not in m["title"]
        assert _kv(m, tr("磁盘"))[tr("文件系统")] == "—"

    def test_local_facts_from_volume_info(self):
        vol = volumes.VolumeInfo(path=Path("E:\\"), label="ASIAIR",
                                 kind=volumes.KIND_REMOVABLE,
                                 total=100, free=40, fs="exFAT")
        f = D.local_facts("E:\\", vol, present=True, hits=["Autorun"])
        assert f["drive"] == "E:" and f["fs"] == "exFAT"
        # 这条**真的**走了 `VolumeInfo.kind_text`(不是上面那条喂进去的假数据),
        # 所以拿它自己的输出比 —— 那一支已经包了 `_()`,换语言两边一起变。
        assert f["kind_text"] == vol.kind_text
        assert f["volume_root"] == "E:\\"

    def test_subdirectory_shows_owning_volume(self):
        rec = _local_rec(host="D:\\ASIAIR", path="D:\\ASIAIR")
        facts = self._facts(root="D:\\ASIAIR", drive="D:", volume_root="D:\\",
                            label="DATA", hits=["Autorun", "Plan", "log"])
        m = D.local_card(rec, facts=facts, now=NOW)
        assert _kv(m, tr("磁盘"))[tr("所在卷")] == "D:\\"
        assert _kv(m, tr("ZWO 特征"))[tr("特征目录")] == tr("{n} / {total} 项", n=3, total=8)
        # 子目录不能拿所在盘的卷标当自己的名字(会张冠李戴)
        assert m["title"] == "ASIAIR (D:)"

    def test_volume_root_uses_label(self):
        m = D.local_card(_local_rec(), facts=self._facts(), now=NOW)
        assert m["title"] == "ASIAIR (E:)"

    def test_legacy_record_without_path_still_resolves_root(self):
        rec = devices._normalize({"host": "E:\\", "kind": "local"})
        m = D.local_card(rec, facts=self._facts(), now=NOW)
        assert _kv(m, tr("磁盘"))[tr("路径")] == "E:\\"


# ---------------------------------------------------------------- 未记录的卷

class TestVolumeCard:
    def _vol(self, **kw):
        kw.setdefault("path", Path("F:\\"))
        kw.setdefault("label", "CARD")
        kw.setdefault("kind", volumes.KIND_REMOVABLE)
        kw.setdefault("total", 32 << 30)
        kw.setdefault("free", 8 << 30)
        kw.setdefault("fs", "exFAT")
        return volumes.VolumeInfo(**kw)

    def test_facts_from_volume_info(self):
        f = D.volume_facts(self._vol(), ["Autorun", "Plan", "log"], ["junk"])
        assert f["drive"] == "F:" and f["fs"] == "exFAT"
        assert f["kind"] == volumes.KIND_REMOVABLE and f["others_n"] == 1

    def test_card_marks_probable_zwo(self):
        m = D.volume_card(D.volume_facts(self._vol(),
                                         ["Autorun", "Plan", "log"]))
        assert m["key"].startswith("vol:") and m["add"][0] is True
        assert any(s == "zwo" for _t, s in m["badges"])
        assert m["status"] == (tr("已用 {pct:.0f}%", pct=75), "warn")

    def test_offer_filter(self):
        removable = D.volume_facts(self._vol())
        fixed = D.volume_facts(self._vol(path=Path("C:\\"),
                                         kind=volumes.KIND_FIXED))
        fixed_with_hits = D.volume_facts(
            self._vol(path=Path("C:\\"), kind=volumes.KIND_FIXED), ["Autorun"])
        assert D.should_offer_volume(removable) is True
        assert D.should_offer_volume(fixed) is False
        assert D.should_offer_volume(fixed_with_hits) is True

    def test_volume_info_exposes_filesystem(self):
        v = self._vol(fs="")
        assert v.fs_text == "—"
        assert self._vol().fs_text == "exFAT"


# ---------------------------------------------------------------- 手动添加

class TestManualInput:
    def test_empty(self):
        r = D.parse_manual_input("  ")
        assert not r["ok"] and "本地文件夹" in r["error"]

    def test_plain_ip(self):
        r = D.parse_manual_input("192.0.2.225")
        assert r["ok"] and r["kind"] == devices.KIND_SMB
        assert r["host"] == "192.0.2.225"

    def test_hostname_and_quotes_stripped(self):
        assert D.parse_manual_input('"asiair-plus.local"')["host"] == \
            "asiair-plus.local"

    def test_unc_path_reduced_to_host(self):
        r = D.parse_manual_input("\\\\192.0.2.225\\EMMC Images")
        assert r["ok"] and r["host"] == "192.0.2.225"

    def test_smb_url_reduced_to_host(self):
        r = D.parse_manual_input("smb://192.0.2.99/EMMC%20Images/log")
        assert r["ok"] and r["host"] == "192.0.2.99"

    def test_port_rejected_with_explanation(self):
        r = D.parse_manual_input("192.0.2.225:445")
        assert not r["ok"] and "445" in r["error"]

    def test_garbage_rejected(self):
        assert not D.parse_manual_input("我的 ASIAIR 盒子")["ok"]
        assert not D.parse_manual_input("a" * 300)["ok"]

    def test_existing_local_dir(self, tmp_path):
        r = D.parse_manual_input(str(tmp_path))
        assert r["ok"] and r["kind"] == devices.KIND_LOCAL
        assert r["path"] == str(tmp_path)

    def test_trailing_separator_trimmed(self, tmp_path):
        r = D.parse_manual_input(str(tmp_path) + "\\")
        assert r["ok"] and r["path"] == str(tmp_path)

    def test_drive_letter_gets_root_slash(self):
        r = D.parse_manual_input("E:", isdir=lambda p: p == "E:\\")
        assert r["ok"] and r["host"] == "E:\\" and r["path"] == "E:\\"

    def test_missing_local_dir_rejected(self):
        r = D.parse_manual_input("E:\\", isdir=lambda p: False)
        assert not r["ok"] and "不存在" in r["error"]

    def test_duplicate_is_info_not_error(self, tmp_path):
        r = D.parse_manual_input("192.0.2.225", existing=["192.0.2.225"])
        assert not r["ok"] and r.get("dup") is True
        r2 = D.parse_manual_input(str(tmp_path), existing=[str(tmp_path)])
        assert not r2["ok"] and r2.get("dup") is True

    def test_relative_dir_not_mistaken_for_local(self, tmp_path, monkeypatch):
        """当前目录下恰好有个同名文件夹时,手输的主机名不能被当成本地路径。"""
        (tmp_path / "nas").mkdir()
        monkeypatch.chdir(tmp_path)
        r = D.parse_manual_input("nas")
        assert r["ok"] and r["kind"] == devices.KIND_SMB


# ---------------------------------------------------------------- 记录 / 空状态

class TestRecordsAndEmptyState:
    def test_sorted_puts_local_first(self):
        recs = [_smb_rec(host="a"), _local_rec(host="E:\\"), _smb_rec(host="b")]
        assert [r["host"] for r in D.sorted_records(recs)] == ["E:\\", "a", "b"]

    def test_empty_records_yield_no_cards(self):
        assert D.sorted_records([]) == []

    def test_migration_old_json_renders_both_cards(self, tmp_path, monkeypatch):
        """旧版 devices.json(没有 kind/path)读进来后两类卡片都能组装。"""
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        target = devices.devices_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps([
            {"host": "192.0.2.225", "name": "ASIAIR", "os": "Samba",
             "dialect": "SMB 3.1.1", "shares": 3, "last_ok": NOW - 100},
            {"host": "E:\\", "kind": "local", "name": "CARD",
             "dialect": "本地磁盘", "shares": 1, "last_ok": NOW - 50},
        ]), encoding="utf-8")
        recs = D.sorted_records(devices.load())
        assert [devices.is_local(r) for r in recs] == [True, False]
        local = D.local_card(recs[0], facts=None, now=NOW)
        smb = D.smb_card(recs[1], now=NOW)
        assert local["title"] and smb["title"]
        assert _kv(local, tr("磁盘"))[tr("路径")] == "E:\\"      # path 缺失 → 回填 host

    def test_remember_then_forget_roundtrip(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        devices.remember("192.0.2.7", kind=devices.KIND_SMB)
        assert [r["host"] for r in devices.load()] == ["192.0.2.7"]
        devices.forget("192.0.2.7")
        assert devices.load() == []

    def test_manual_add_record_keeps_kind(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        card = tmp_path / "card"
        card.mkdir()
        res = D.parse_manual_input(str(card))
        devices.remember(res["host"], kind=res["kind"], path=res["path"],
                         dialect="本地磁盘", shares=1)
        (rec,) = devices.load()
        assert devices.is_local(rec) and devices.local_root(rec) == str(card)


# ============================================================ 对抗审查确认项
# 以下七组对应设备页第一轮对抗审查确认的 7 条缺陷。每组都验证过"把修复回退掉
# 测试会红" —— 否则很容易写出删掉修复照样过的空转测试。

# ---------------------------------------------------------------- [高] 1
# _add 在 UI 线程调 parse_manual_input,其中 os.path.isdir 对不可达 UNC
# 阻塞 42 秒,把 XAML 消息泵和手摇 asyncio 循环一起冻住。

class TestAddNeverTouchesFilesystemOnUiThread:
    def test_forward_slash_unc_is_smb_not_a_local_path(self):
        """``//192.0.2.225/EMMC Images``(ASIAIR 场景下最自然的粘贴)必须
        当成 SMB 地址认掉 —— 走本地分支就会撞上 os.path.isdir 的长超时。"""
        def boom(_p):
            raise AssertionError("正斜杠 UNC 不该去碰文件系统")

        r = D.parse_manual_input("//192.0.2.225/EMMC Images", isdir=boom)
        assert r["ok"] and r["kind"] == devices.KIND_SMB
        assert r["host"] == "192.0.2.225"

    def test_add_does_no_filesystem_work_on_calling_thread(self, tmp_path,
                                                           monkeypatch):
        """UI 线程那一半只做纯字符串判断:文件系统调用必须发生在别的线程。"""
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
        card = tmp_path / "card"
        card.mkdir()
        ui_thread = threading.get_ident()
        touched: list[str] = []
        real_isdir = os.path.isdir

        def spy_isdir(p):
            if threading.get_ident() == ui_thread:
                raise AssertionError(f"UI 线程碰了文件系统: {p}")
            touched.append(str(p))
            return real_isdir(p)

        monkeypatch.setattr(os.path, "isdir", spy_isdir)
        monkeypatch.setattr(volumes, "list_volumes", lambda: [])
        shell = _FakeShell()
        page = _fake_page(shell, _shown=False)
        page._add(str(card))                    # 不许在这里抛
        assert shell.done.wait(20), "添加线程没回来"
        assert touched, "文件系统检查根本没发生(检查测试本身)"
        (rec,) = devices.load()
        assert devices.local_root(rec) == str(card)

    def test_add_is_reentrancy_guarded_and_shows_progress(self):
        page = _fake_page()
        started: list[str] = []
        page._add_worker = lambda raw: started.append(raw)
        monkey = threading.Thread
        try:
            # 直接替 threading.Thread 太糙,这里只验证第二次调用被挡住:
            page._adding = True
            page._add("192.0.2.225")
            assert page.add_hint.Text == ""      # 被挡住,连提示都没改
        finally:
            assert monkey is threading.Thread
        page._adding = False
        page.add_hint.Text = ""
        page._add("")                            # 空输入:纯字符串判断,不起线程
        assert page._adding is False
        assert page.shell.errors and "本地文件夹" in page.shell.errors[0]

    def test_astral_input_is_sanitized_before_echo(self):
        assert D.bmp_safe("A\U0001F600B") == "AB"
        assert D.bmp_safe("x" * 80, 10) == "x" * 10 + "…"


# ---------------------------------------------------------------- [高] 2
# 心跳次数塞进 groups → 每 4s 整张 KV 表推倒重建(实测 67 ms,与在哪一页无关)。

class TestHeartbeatCost:
    def _hb(self, checks):
        return {"host": "192.0.2.225", "alive": True, "rtt_ms": 8.0,
                "checks": checks, "fails": 0}

    def test_heartbeat_tick_does_not_change_groups(self):
        a = D.smb_card(_smb_rec(), connected=True, hb=self._hb(42), now=NOW)
        b = D.smb_card(_smb_rec(), connected=True, hb=self._hb(43), now=NOW)
        assert a["groups"] == b["groups"], "心跳变化不该动 groups(会触发整表重建)"
        assert a["live"] != b["live"]

    def test_apply_card_only_touches_the_live_line(self):
        """只有 live 变了:_fill_groups / _render_badges 一次都不能被调用。"""
        page = _fake_page()
        rebuilt: list[str] = []
        page._fill_groups = lambda *a: rebuilt.append("kv")
        page._render_badges = lambda *a: rebuilt.append("badges")
        a = D.smb_card(_smb_rec(), connected=True, hb=self._hb(42), now=NOW)
        b = D.smb_card(_smb_rec(), connected=True, hb=self._hb(43), now=NOW)
        card = {"root": _Widget(), "dot": _Widget(), "title": _Widget(),
                "sub": _Widget(), "status": _Widget(), "age": _Widget(),
                "live": _Widget(), "badges": _Widget(), "kv": _Widget(),
                "actions": _Widget(), "buttons": {}, "rows": [], "shape": None,
                "model": a}
        page._apply_card(card, b)
        assert rebuilt == [], f"心跳不该重建 {rebuilt}"
        assert card["live"].Text == b["live"]
        assert card["title"].writes == [] and card["status"].writes == []

    def test_same_shape_kv_updates_in_place(self):
        """结构签名相同(只有数值变)→ 走原地改文字,不重建控件树。"""
        g1 = [(D._GRP_CAPACITY, "容量", D.capacity_pairs(100, 50))]
        g2 = [(D._GRP_CAPACITY, "容量", D.capacity_pairs(100, 40))]
        assert D._groups_shape(g1) == D._groups_shape(g2)
        # 行数变化 / 占位行必须触发重建
        assert D._groups_shape(g1) != D._groups_shape(
            [(D._GRP_CAPACITY, "容量", D.capacity_pairs(0, 0))])

    def test_in_place_update_writes_new_values(self):
        page = _fake_page()
        rows = [{"val": _Widget(), "aux": _Widget(), "fill": _Widget(),
                 "last": ("已用", "50 B", "占 50%", False, "good",
                          ("usagebar", 50.0))}]
        page._update_rows(rows, [("已用", "60 B", "占 60%", False, "warn",
                                  ("usagebar", 60.0))])
        assert rows[0]["val"].Text == "60 B"
        assert rows[0]["aux"].Text == "占 60%"
        assert rows[0]["fill"].Width == pytest.approx(D._BAR_W * 0.6)


# ---------------------------------------------------------------- [中] 3
# 手动添加调 devices.remember 无条件刷 last_ok → 从没连上的设备显示"刚刚连过",
# 还会抢走下次启动的默认设备。

class TestLastOkOnlyOnRealConnect:
    def test_manual_record_is_never_connected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        devices.remember("192.0.2.522", kind=devices.KIND_SMB, connected=False)
        (rec,) = devices.load()
        assert rec["last_ok"] == 0.0
        assert D.rel_time(rec["last_ok"], NOW) == tr("从未")
        assert devices.last_host() is None       # 不许当下次启动的默认设备

    def test_real_connect_still_stamps_last_ok(self, tmp_path, monkeypatch):
        """默认 connected=True:_window._connect_to 的现有语义不能变。"""
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        devices.remember("192.0.2.225", name="ASIAIR", shares=3)
        (rec,) = devices.load()
        assert rec["last_ok"] > 0
        assert devices.last_host() == "192.0.2.225"

    def test_typo_does_not_steal_the_default_device(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        devices.remember("192.0.2.225")                       # 真连上过
        devices.remember("192.0.2.522", connected=False)      # 打错了才添加的
        assert devices.last_host() == "192.0.2.225"
        assert [r["host"] for r in devices.load()][0] == "192.0.2.522"  # 仍在列表里

    def test_metadata_update_keeps_existing_last_ok(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        devices.remember("192.0.2.225")
        was = devices.load()[0]["last_ok"]
        time.sleep(0.01)
        devices.remember("192.0.2.225", name="ASIAIR", connected=False)
        rec = devices.load()[0]
        assert rec["last_ok"] == was and rec["name"] == "ASIAIR"

    def test_add_worker_records_as_not_connected(self, tmp_path, monkeypatch):
        """页面这一侧真的传了 connected=False(整条链路,不只是 devices.py)。"""
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
        monkeypatch.setattr(volumes, "list_volumes", lambda: [])
        page = _fake_page(_shown=False)
        page._add_worker("192.0.2.522")
        (rec,) = devices.load()
        assert rec["host"] == "192.0.2.522" and rec["last_ok"] == 0.0


# ---------------------------------------------------------------- [中] 4
# 卡片显示的是手动刷新时的快照,却被每 4s 的心跳带着重画,看上去像实时数据。

class TestSnapshotHonesty:
    def test_note_says_when_it_was_collected(self):
        assert D.snapshot_note(0) == tr("未采集")
        assert D.snapshot_note(NOW - 5, NOW) == ""          # 新鲜:不加噪声
        assert D.snapshot_note(NOW - 600, NOW) == tr("采集于 {0}", tr("{0} 分钟前", 10))

    def test_local_card_labels_stale_capacity(self):
        facts = {"present": True, "total": 100, "free": 50, "drive": "E:"}
        stale = D.local_card(_local_rec(), facts=facts, snap_ts=NOW - 600,
                             now=NOW)
        fresh = D.local_card(_local_rec(), facts=facts, snap_ts=NOW - 5, now=NOW)
        assert stale["age"] == tr("采集于 {0}", tr("{0} 分钟前", 10)) and fresh["age"] == ""

    def test_live_probed_smb_card_is_not_marked_stale(self):
        m = D.smb_card(_smb_rec(), rtt={"192.0.2.225": 5.0},
                       fresh={"192.0.2.225"}, snap_ts=NOW - 600, now=NOW)
        assert m["age"] == "" and m["status"][0] == tr("● 端口可达 {ms:.0f} ms", ms=5)
        old = D.smb_card(_smb_rec(), rtt={"192.0.2.225": 5.0},
                         snap_ts=NOW - 600, now=NOW)
        assert old["age"] == tr("采集于 {0}", tr("{0} 分钟前", 10))

    def test_tick_refreshes_while_visible_and_stale(self):
        page = _fake_page(_parent_ok=True)
        page.root.Parent = object()                 # 还挂在树上 = 可见
        page._state["ts"] = time.time() - 3600
        page._tick()
        assert page.refreshes, "可见且快照过期时必须真去重采"

    def test_tick_does_not_refresh_when_hidden(self):
        page = _fake_page(_parent_ok=True)
        page.root.Parent = None                     # 被切走了
        page._state["ts"] = time.time() - 3600
        page._tick()
        assert page.refreshes == []

    def test_tick_backs_off_after_a_try(self):
        page = _fake_page(_parent_ok=True)
        page.root.Parent = object()
        page._state["ts"] = time.time() - 3600
        page._last_try = time.monotonic()           # 刚试过(可能失败了)
        page._tick()
        assert page.refreshes == []                 # 不许每 5s 猛敲


# ---------------------------------------------------------------- [低] 5
# __init__ 就渲染全部卡片,而 DevicesPage 在 win.Activate() 之前构造。

class TestLazyRender:
    def test_no_cards_before_first_show(self):
        page = _fake_page(_shown=False)
        page.refresh_records()
        assert page.laid == {}, "首次 on_show 之前不该建任何卡片"

    def test_first_show_renders(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        devices.remember("192.0.2.225", name="ASIAIR")
        page = _fake_page(_shown=False)
        page._start_ticker = lambda: None
        page.on_show()
        assert page._shown is True
        assert [m["host"] for m in page.laid["dev"]] == ["192.0.2.225"]

    def test_init_does_not_render(self):
        import inspect

        src = inspect.getsource(D.DevicesPage.__init__)
        assert "refresh_records" not in src, "构造函数不该渲染(冷启动 +823ms)"


# ---------------------------------------------------------------- [低] 6
# 本地设备 host 按字节比较:E:\ 与 e:\ 被当成两台设备。

class TestHostKeyIsNormalized:
    def test_case_and_separator_insensitive_for_local(self):
        k = devices.host_key("E:\\")
        assert devices.host_key("e:\\") == k
        assert devices.host_key("e:/") == k
        assert devices.host_key("E:\\ASIAIR\\") == devices.host_key("e:/asiair")

    def test_smb_hostnames_are_case_insensitive(self):
        assert devices.same_host("ASIAIR-Plus", "asiair-plus")
        assert not devices.same_host("192.0.2.225", "192.0.2.226")

    def test_local_path_shape_matches_common(self):
        """devices._looks_local 与 _common.looks_like_local_path 不许漂移。"""
        from astro_smb_gui._common import looks_like_local_path

        for s in ("E:\\", "e:/", "/media/card", "192.0.2.225", "asiair",
                  "", "  ", "C:", "\\\\host\\share", "//host/share"):
            assert devices._looks_local(s) == looks_like_local_path(s), s

    def test_same_card_is_not_recorded_twice(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        devices.remember("E:\\", name="ASIAIR", kind=devices.KIND_LOCAL,
                         path="E:\\", connected=False)
        devices.remember("e:\\", name="手输的", kind=devices.KIND_LOCAL,
                         path="e:\\", connected=False)
        recs = devices.load()
        assert len(recs) == 1 and recs[0]["name"] == "手输的"

    def test_existing_json_with_both_cases_collapses(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        target = devices.devices_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps([
            {"host": "E:\\", "kind": "local", "path": "E:\\", "last_ok": NOW},
            {"host": "e:\\", "kind": "local", "path": "e:\\", "last_ok": NOW - 9},
        ]), encoding="utf-8")
        recs = devices.load()
        assert len(recs) == 1 and recs[0]["host"] == "E:\\"   # 留排序靠前的

    def test_manual_add_detects_case_variant_duplicate(self):
        r = D.parse_manual_input("e:\\", existing=["E:\\"],
                                 isdir=lambda p: True)
        assert not r["ok"] and r.get("dup") is True
        assert "E:\\" in r["error"]          # 提示里显示记录中真实的那条
        r2 = D.parse_manual_input("ASIAIR", existing=["asiair"])
        assert not r2["ok"] and r2.get("dup") is True

    def test_forget_matches_normalized(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        devices.remember("E:\\", kind=devices.KIND_LOCAL, path="E:\\")
        devices.forget("e:/")
        assert devices.load() == []


# ---------------------------------------------------------------- [低] 7
# 页面只读自己的 _state['rtt'],从不读外壳每 20s 一轮的探测结果。

class TestPageUsesShellProbe:
    def test_shell_rtt_prefers_public_accessor(self):
        shell = _FakeShell(_dev_rtt={"a": 1.0})
        page = _fake_page(shell)
        assert page._shell_rtt() == {"a": 1.0}          # 还没接线:退回私有字段
        shell.dev_rtt = lambda: {"b": 2.0}
        assert page._shell_rtt() == {"b": 2.0}          # 接线后走公开访问器

    def test_smb_badge_follows_shell_probe(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        devices.remember("192.0.2.225", name="ASIAIR")
        shell = _FakeShell(_dev_rtt={"192.0.2.225": 7.0})
        page = _fake_page(shell)
        page.refresh_records()
        (m,) = page.laid["dev"]
        assert m["status"] == (tr("● 端口可达 {ms:.0f} ms", ms=7), "good")   # 不再停在"○ 未探测"

    def test_local_card_follows_shell_unplug(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        devices.remember("E:\\", name="ASIAIR", kind=devices.KIND_LOCAL,
                         path="E:\\")
        shell = _FakeShell(_dev_rtt={"E:\\": None})     # 外壳:卡拔了
        page = _fake_page(shell)
        page._state["local"]["E:\\"] = {"present": True, "total": 100,
                                        "free": 50, "hits": ["Autorun"]}
        page._state["ts"] = time.time()
        page.refresh_records()
        (m,) = page.laid["dev"]
        assert m["status"] == (tr("● 已拔出"), "dim")
        assert _kv(m, tr("容量"))[tr("容量")] == "—"           # 别再挂着上次的容量

    def test_records_are_cached_between_heartbeats(self, tmp_path, monkeypatch):
        """心跳每 4s 走一遍渲染 —— 不该每次都去读 devices.json。"""
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        devices.remember("192.0.2.225")
        calls: list[int] = []
        real_load = devices.load
        monkeypatch.setattr(devices, "load",
                            lambda: (calls.append(1), real_load())[1])
        page = _fake_page()
        page.refresh_records()
        page.refresh_records()
        page.refresh_records()
        assert len(calls) == 1

    def test_cache_is_dropped_when_records_change(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        page = _fake_page()
        page.refresh_records()
        assert page.laid["dev"] == []
        devices.remember("192.0.2.225", name="ASIAIR")
        page.on_connected([])               # 连接成功:缓存必须作废
        assert [m["host"] for m in page.laid["dev"]] == ["192.0.2.225"]


# ---------------------------------------------------------------- 静态扫描

class TestStaticSource:
    """win32more 按码点数给 HSTRING 长度,星平面字符会让字符串末尾少一个字符
    (真机现象 'Plan'→'Pla')。UI 文案里一律不许出现代理对。"""

    FILES = ("astro_smb_gui/_devices.py", "astro_smb_gui/devicespage.xaml",
             "astro_smb_gui/volumes.py")

    def test_no_astral_characters(self):
        root = Path(__file__).resolve().parents[1]
        for rel in self.FILES:
            text = (root / rel).read_text(encoding="utf-8")
            bad = {c for c in text if ord(c) > 0xFFFF}
            assert not bad, f"{rel} 含星平面字符: {[hex(ord(c)) for c in bad]}"

    def test_card_template_is_well_formed_and_named(self):
        import xml.etree.ElementTree as ET

        root = ET.fromstring(D._CARD_XAML)
        x_ns = "{http://schemas.microsoft.com/winfx/2006/xaml}Name"
        names = {el.get(x_ns) for el in root.iter() if el.get(x_ns)}
        # Live/Age 是高频字段的专用槽位:它们必须存在,否则心跳只能塞进 KV 表
        assert {"Dot", "Title", "Sub", "Status", "Age", "Live", "Badges", "KV",
                "Actions"} <= names

    def test_page_xaml_has_required_names(self):
        import xml.etree.ElementTree as ET

        path = Path(__file__).resolve().parents[1] / "astro_smb_gui/devicespage.xaml"
        root = ET.fromstring(path.read_text(encoding="utf-8"))
        x_ns = "{http://schemas.microsoft.com/winfx/2006/xaml}Name"
        names = {el.get(x_ns) for el in root.iter() if el.get(x_ns)}
        assert {"RefreshBtn", "RefreshRing", "StatusText", "AddBox", "AddBtn",
                "AddHint", "RecordHead", "EmptyCard", "GoScanBtn", "CardHost",
                "VolHead", "VolHost"} <= names

    def test_page_exposes_shell_page_interface(self):
        for name in ("on_show", "on_connected", "on_close", "refresh_records"):
            assert callable(getattr(D.DevicesPage, name))


# ---------------------------------------- 主控接线:启动默认设备的本地卡兜底

class TestStartupHostFallback:
    """自动发现的本地卡传 connected=False(它没"连接成功"过),于是
    devices.last_host() 会返回 None。若不兜底,首次插卡启动会直接跳去扫描页 ——
    而扫描根本找不到手里这张卡。兜底必须只认**自动发现的本地卡**:
    手输的地址(可能打错)绝不能靠这条路抢走默认设备。
    """

    @staticmethod
    def _startup(monkeypatch, records):
        from astro_smb_gui import _window
        monkeypatch.setattr(_window.devices, "load", lambda: list(records))
        monkeypatch.setattr(
            _window.devices, "last_host",
            lambda: next((r["host"] for r in records
                          if float(r.get("last_ok") or 0) > 0), None))
        return _window.App._startup_host()

    def test_prefers_really_connected_device(self, monkeypatch):
        got = self._startup(monkeypatch, [
            {"host": "E:\\", "kind": "local", "last_ok": 0, "first_seen": 200.0},
            {"host": "192.0.2.228", "kind": "smb", "last_ok": 100.0,
             "first_seen": 100.0},
        ])
        assert got == "192.0.2.228"

    def test_falls_back_to_autodiscovered_local_card(self, monkeypatch):
        got = self._startup(monkeypatch, [
            {"host": "E:\\", "kind": "local", "last_ok": 0, "first_seen": 200.0},
        ])
        assert got == "E:\\", "插上卡就该自动连它"

    def test_typed_smb_address_never_wins_by_fallback(self, monkeypatch):
        """手输但从未连上的 SMB 地址(可能是打错的)不该成为启动默认。"""
        got = self._startup(monkeypatch, [
            {"host": "192.0.2.522", "kind": "smb", "last_ok": 0,
             "first_seen": 999.0},
        ])
        assert got == ""

    def test_newest_local_card_wins(self, monkeypatch):
        got = self._startup(monkeypatch, [
            {"host": "E:\\", "kind": "local", "last_ok": 0, "first_seen": 100.0},
            {"host": "F:\\", "kind": "local", "last_ok": 0, "first_seen": 300.0},
        ])
        assert got == "F:\\"

    def test_empty_and_broken_records(self, monkeypatch):
        assert self._startup(monkeypatch, []) == ""
        assert self._startup(monkeypatch, [{"kind": "local"}]) == ""
