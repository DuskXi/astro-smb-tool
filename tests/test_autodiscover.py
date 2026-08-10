"""局域网自动发现 —— **不许假设别人的设备地址**。

设备是 DHCP 的。硬编码一个默认 IP 对新用户永远是错的:早期版本里
`astro_smb/cli.py` 的 `DEFAULT_HOST` 写死了开发机上那台设备的地址,
换台机器装上之后**每条命令都对着一个不存在的地址等 15 秒超时**。

正确的默认不是"猜",也不是"报错让人自己填",而是**去找**。

三条纪律:

* **判据是 SMB 协商成功,不是 TCP 端口开着** —— 有路由器会对整个网段的
  445 秒回 ACK,按 TCP 判会把 254 个地址报成两百多台;
* **只有无歧义才自动连** —— 两台疑似 ASIAIR 时替人选一台,选错了他看到的
  是别人的片子,而界面上不会说"我替你选了";
* **扫描只有一份实现**(`astro_smb.netscan`)。复制出去的那份迟早有一份被
  改回只看 TCP。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from astro_smb import netscan as N          # noqa: E402


def _dev(ip: str, *, asiair: bool = False, name: str = "") -> N.Device:
    return N.Device(ip=ip, name=name,
                    shares=["EMMC Images"] if asiair else ["Public"])


class TestNobodyHardcodesAnAddress:

    def test_no_default_host_constant_anywhere(self):
        bad = []
        for pkg in ("astro_smb", "astro_smb_app", "astro_smb_qt", "astro_smb_gui"):
            for p in sorted((ROOT / pkg).rglob("*.py")):
                for i, line in enumerate(
                        p.read_text(encoding="utf-8").splitlines(), 1):
                    code = line.split("#")[0]      # 注释里提实测地址是可以的
                    if re.search(r"DEFAULT_HOST\s*=\s*[\"']", code):
                        bad.append(f"{p.relative_to(ROOT).as_posix()}:{i}")
        assert not bad, f"又出现了硬编码默认地址: {bad}"

    def test_cli_discovers_instead_of_giving_up(self):
        src = (ROOT / "astro_smb" / "cli.py").read_text(encoding="utf-8")
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "main")
        body = "\n".join(ast.unparse(b) for b in fn.body)
        assert "_autodiscover()" in body, (
            "没给 -H 时直接退出了 —— 正确的默认是去找,不是让人自己填")

    def test_qt_starts_scanning_by_itself(self):
        src = (ROOT / "astro_smb_qt" / "shell.py").read_text(encoding="utf-8")
        assert "autoscan" in src, (
            "一台设备都没记过时只是跳到扫描页 —— 用户还不知道要找什么,"
            "停在那里等他点「开始扫描」等于没有自动发现")


class TestTheJudgementIsSmbNotTcp:

    def test_only_one_implementation(self):
        """扫描循环搬到核心库了,前端与 CLI 都走它。"""
        qt = (ROOT / "astro_smb_qt" / "pages" / "scan.py").read_text(
            encoding="utf-8")
        assert "ThreadPoolExecutor" not in qt, (
            "扫描页又自己写了一遍扫描循环 —— 判据会分叉")
        cli = (ROOT / "astro_smb" / "cli.py").read_text(encoding="utf-8")
        assert "from astro_smb.netscan import" in cli

    def test_core_does_not_reach_into_the_app_layer(self):
        """核心库不能反向依赖共享层 —— 这正是原语要搬下来的原因。

        **查真 import,不查子串。** 第一版写的是 `"astro_smb_app" not in src`,
        命中的是模块文档里"这些原语原本住在 `astro_smb_app/views/scan.py`"
        那句话 —— 也就是**说明为什么搬走的那句话本身**让断言变红。
        这一轮在"匹配到注释/文档串"上栽过好几次了。
        """
        src = (ROOT / "astro_smb" / "netscan.py").read_text(encoding="utf-8")
        bad = []
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                bad += [a.name for a in node.names
                        if a.name.startswith("astro_smb_app")]
            elif isinstance(node, ast.ImportFrom):
                if (node.module or "").startswith("astro_smb_app"):
                    bad.append(node.module)
        assert not bad, f"核心库 import 了共享层: {bad}"

    def test_a_port_that_answers_but_does_not_negotiate_is_dropped(self,
                                                                   monkeypatch):
        """路由器对整网段秒回 ACK 的那种:端口通,但协商不出来。"""
        monkeypatch.setattr(N, "probe", lambda ip, **kw: (True, 1.0))
        monkeypatch.setattr(N, "identify", lambda ip, **kw: None)
        assert N.discover("192.0.2", hosts=8, pool=4) == []

    def test_a_real_device_is_kept(self, monkeypatch):
        monkeypatch.setattr(N, "probe", lambda ip, **kw: (True, 2.0))
        monkeypatch.setattr(N, "resolve_hostname", lambda ip: "")
        monkeypatch.setattr(
            N, "identify",
            lambda ip, **kw: ("ASIAIR", ["EMMC Images"])
            if ip.endswith(".3") else None)
        got = N.discover("192.0.2", hosts=8, pool=4)
        assert [d.ip for d in got] == ["192.0.2.3"]
        assert got[0].is_asiair is True


class TestItOnlyAutoConnectsWhenUnambiguous:

    def test_exactly_one_asiair_is_picked(self):
        got = N.pick_one([_dev("192.0.2.3", asiair=True), _dev("192.0.2.4")])
        assert got is not None and got.ip == "192.0.2.3"

    def test_two_asiairs_are_not_picked(self):
        """**这条是重点。** 选错了他操作的是别人的片子,而界面不会说。"""
        assert N.pick_one([_dev("192.0.2.3", asiair=True),
                           _dev("192.0.2.5", asiair=True)]) is None

    def test_no_asiair_is_not_picked(self):
        assert N.pick_one([_dev("192.0.2.4")]) is None

    def test_nothing_found_is_not_picked(self):
        assert N.pick_one([]) is None


class TestItDoesNotScanEveryVirtualAdapter:
    """一台开着 VPN 的开发机实测报出**五个**网段 —— 全扫就是 1270 次探测。"""

    def test_home_subnets_come_first(self, monkeypatch):
        monkeypatch.setattr(N, "local_subnets",
                            lambda: ["198.18.0", "100.65.0", "192.168.1",
                                     "172.29.16", "192.168.240"])
        got = N.preferred_subnets()
        assert got[0].startswith("192.168."), got
        assert got[-1] == "198.18.0", got        # VPN 保留段排最后
        assert set(got) == {"198.18.0", "100.65.0", "192.168.1",
                            "172.29.16", "192.168.240"}, "排序不是过滤"

    def test_it_stops_once_an_asiair_turns_up(self, monkeypatch):
        seen: list[str] = []

        def fake(sub, **kw):
            seen.append(sub)
            return ([_dev("192.0.2.3", asiair=True)]
                    if sub == "192.0.2.0/24" else [])

        monkeypatch.setattr(N, "local_networks",
                            lambda: ["192.0.2.0/24", "198.51.100.0/24"])
        monkeypatch.setattr(N, "discover", fake)
        N.discover_all()
        assert seen == ["192.0.2.0/24"], f"找到了还继续扫: {seen}"

    def test_it_keeps_going_when_nothing_is_found(self, monkeypatch):
        seen: list[str] = []
        monkeypatch.setattr(N, "local_networks",
                            lambda: ["192.0.2.0/24", "198.51.100.0/24"])
        monkeypatch.setattr(N, "discover",
                            lambda sub, **kw: seen.append(sub) or [])
        N.discover_all()
        assert seen == ["192.0.2.0/24", "198.51.100.0/24"]

    def test_the_scan_page_asks_for_the_full_list(self):
        """扫描页要的是完整清单,不能提前收手。"""
        src = (ROOT / "astro_smb" / "netscan.py").read_text(encoding="utf-8")
        assert "stop_on_asiair" in src


class TestProgressIsReported:
    """全网段约 6 秒 —— 边扫边出结果,不能等六秒憋一屏。"""

    def test_on_progress_fires_for_every_address(self, monkeypatch):
        monkeypatch.setattr(N, "probe", lambda ip, **kw: (False, None))
        seen = []
        N.discover("192.0.2", hosts=5, pool=2,
                   on_progress=lambda d, t, rows: seen.append((d, t)))
        assert seen == [(1, 5), (2, 5), (3, 5), (4, 5), (5, 5)]

    def test_cancel_stops_early(self, monkeypatch):
        monkeypatch.setattr(N, "probe", lambda ip, **kw: (False, None))
        seen = []
        N.discover("192.0.2", hosts=50, pool=2,
                   on_progress=lambda d, t, rows: seen.append(d),
                   cancel=lambda: len(seen) >= 3)
        assert len(seen) < 50, "cancel 没起作用,整段扫完了"


class TestTheViewLayerStillWorks:
    """原语搬走了,但视图层按老名字转出 —— 调用点一个字节没改。"""

    def test_the_old_private_names_still_resolve(self):
        from astro_smb_app.views import scan as sv

        assert sv._subnet_of("192.0.2.225") == "192.0.2"
        assert sv.valid_subnet("nope") == ""
        assert callable(sv._probe) and callable(sv._identify)

    def test_rows_still_carry_the_display_fields(self):
        from astro_smb_app import discover as D

        row = D.to_rows([_dev("192.0.2.3", asiair=True, name="ASIAIR")])[0]
        for field in ("ip", "title", "asiair", "shares", "sub"):
            assert field in row, field


class TestItScansTheWholeNetworkNotAGuessed24:
    """**很多人的网不是 /24。**

    以前全项目都假设"本机在一个 /24 里"(`socket.getaddrinfo` 只给地址,
    给不了掩码)。网卡是 /22 的机器上,按 /24 扫**只覆盖了四分之一** ——
    而界面上看起来是「这一段里没有设备」,不是「我只扫了一部分」。
    """

    def test_cidr_is_accepted(self):
        assert N.parse_target("192.0.2.0/22") is not None
        assert len(N.target_hosts(N.parse_target("192.0.2.0/22"))) == 1022

    def test_the_old_three_octet_form_still_works(self):
        """老写法等价于 /24 —— 界面上、设备记录里到处是它。"""
        net = N.parse_target("192.168.1")
        assert net is not None and net.prefixlen == 24
        assert len(N.target_hosts(net)) == 254

    def test_a_host_address_with_a_prefix_snaps_to_its_network(self):
        """用户多半直接抄自己那台机的地址,不会先算网络地址。"""
        assert str(N.parse_target("192.0.2.5/22")) == "192.0.0.0/22"

    def test_garbage_is_rejected_not_guessed(self):
        for bad in ("", "nope", "192.168", "192.0.2.0/33", "999.1.1.1/24"):
            assert N.parse_target(bad) is None, bad

    def test_a_huge_range_is_capped_and_says_so(self):
        """`/16` 是 65534 次探测 —— 按 64 并发算七分钟,界面上只显示
        「正在探测」,看起来就是卡死。截断,并**如实说**截了。"""
        net = N.parse_target("198.18.0.0/16")
        assert len(N.target_hosts(net)) == N.MAX_HOSTS
        line = N.describe_target("198.18.0.0/16")
        assert "65534" in line and str(N.MAX_HOSTS) in line

    def test_discover_reports_the_real_total(self, monkeypatch):
        """进度条的分母得是**这次真要探的个数**,不是写死的 254。"""
        monkeypatch.setattr(N, "probe", lambda ip, **kw: (False, None))
        seen = []
        N.discover("192.0.2.0/22", pool=8,
                   on_progress=lambda d, t, rows: seen.append(t),
                   cancel=lambda: len(seen) >= 5)
        assert seen and set(seen) == {1022}, seen

    def test_tunnel_endpoints_rank_last(self, monkeypatch):
        """`/30`、`/32` 是 VPN 隧道端点,里面不可能有 ASIAIR ——
        而它们恰好因为地址少而扫得飞快,不压后就会排到前面去。"""
        # 打分按 RFC1918 前缀分档,输入必须是真的私网段,换成文档网段
        # 这条就测不到东西了。
        home = "192.168.2.0/24"   # publish-scan: ok(判据要的就是真私网段)
        nets = ["198.18.0.0/30", "100.65.0.8/32", home, "172.29.16.0/20"]
        monkeypatch.setattr(N, "local_networks", lambda: nets)
        got = N.preferred_networks()
        assert got[0] == home, got
        assert got[-2:] == ["100.65.0.8/32", "198.18.0.0/30"], got

    def test_local_networks_falls_back_when_enumeration_fails(self, monkeypatch):
        """枚举网卡失败(平台不支持 / 权限)不该让扫描页打不开 ——
        退回按地址猜的 /24,猜错的后果是扫漏,不是崩。"""
        from astro_smb import netifaces

        monkeypatch.setattr(netifaces, "local_networks", lambda: [])
        monkeypatch.setattr(N, "local_subnets", lambda: ["192.0.2"])
        assert N.local_networks() == ["192.0.2.0/24"]
