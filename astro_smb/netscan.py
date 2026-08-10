"""局域网发现:找本网段上**真的** SMB 设备。

**判据是 SMB 协商成功,不是 TCP 端口开着。** 这条是真机纪律:实测有路由器
会对**整个网段**的 445 SYN 秒回 ACK(1ms 就"连上"),按 TCP 判会把 254 个
地址报成两百多台,而真设备只有三四台。

## 为什么在核心库

这些原语原本住在 `astro_smb_app/views/scan.py`(视图模型层),因为最早只有
扫描页用。后来 CLI 也要自动发现 —— 而核心库**不能反向依赖**共享层
(`test_core_library_never_imports_the_gui` 守着),于是要么复制一份,
要么搬下来。复制出去的那份迟早有一份被改回"只看 TCP"。

分界线是这样划的:**探测网络是核心能力,把结果排版成显示行是视图模型。**
所以这里返回的是 `Device`(纯数据),`views/scan.device_row()` 才负责措辞
(「疑似 ASIAIR」/「可能是 PC/NAS」)与配色。

## 为什么需要它

**设备是 DHCP 的,写死一个 IP 对新用户永远是错的。** 早期版本里
`DEFAULT_HOST` 写死了开发机上那台设备的地址,换台机器之后每条命令都对着
一个不存在的地址等 15 秒超时。没有记录时正确的默认是"去找",不是"猜"。
"""
from __future__ import annotations

import concurrent.futures as cf
import ipaddress
import socket

from astro_smb.i18n import gettext as _
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

#: 一个 /24 里要探的地址数(.1 ~ .254)
HOSTS = 254
#: 并发探测数。64 条时全网段约 6 秒;再高收益很小,还会把弱路由器打懵。
POOL = 64


@dataclass
class Device:
    """一台**完成了 SMB 协商**的设备。纯数据,不带任何显示措辞。"""

    ip: str
    name: str = ""                  # SMB 服务器名
    hostname: str = ""              # 反向 DNS
    shares: list[str] = field(default_factory=list)
    rtt_ms: float | None = None

    @property
    def is_asiair(self) -> bool:
        """疑似 ASIAIR。共享名里有 Images,或名字里有 ASIAIR。

        判据在这里而不在视图层:它是**设备识别**,不是排版。CLI 与两套前端
        都要用同一条,分叉的话同一台设备在不同入口被认成不同东西。
        """
        return (any("Images" in s for s in self.shares)
                or "ASIAIR" in (self.name or "").upper()
                or "ASIAIR" in (self.hostname or "").upper())

    @property
    def label(self) -> str:
        return self.hostname or self.name or self.ip


def probe(ip: str, port: int = 445, timeout: float = 0.4) -> tuple[bool, float | None]:
    """TCP 445 通不通 + 往返毫秒。**通了不代表是 SMB 设备**(见模块说明)。"""
    import time

    t0 = time.monotonic()
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True, (time.monotonic() - t0) * 1000.0
    except OSError:
        return False, None


def identify(ip: str, timeout: float = 1.5) -> tuple[str, list[str]] | None:
    """真去协商一次 SMB。返回 ``(服务器名, 匿名可见的共享)``;不是 SMB 就 None。

    **匿名登录失败不影响判定** —— 能协商就说明那头是 SMB,共享列不出来只是
    需要认证。
    """
    from impacket.smbconnection import SMBConnection

    conn = None
    try:
        conn = SMBConnection(ip, ip, sess_port=445, timeout=timeout)
        name = ""
        try:
            name = conn.getServerName() or ""
        except Exception:                       # noqa: BLE001
            pass
        shares: list[str] = []
        try:
            conn.login("", "")
            for s in conn.listShares():
                nm = s["shi1_netname"][:-1] if s["shi1_netname"] else ""
                if nm and not nm.endswith("$"):
                    shares.append(nm)
        except Exception:                       # noqa: BLE001
            pass
        return name, shares
    except Exception:                           # noqa: BLE001
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:                   # noqa: BLE001
                pass


def resolve_hostname(ip: str) -> str:
    """反向 DNS。**只对已确认的 SMB 设备做** —— 254 次会很慢。"""
    try:
        return socket.gethostbyaddr(ip)[0]
    except OSError:
        return ""


def subnet_of(ip: str) -> str | None:
    """`192.0.2.25` → `192.0.2`;**不是 IPv4 返回 None**。

    搬过来时**一个字节没改**。第一版顺手"改进"成返回空串,于是
    `test_features.TestScanSubnet` 当场红了 —— 搬迁就该是搬迁,
    连返回 None 还是空串这种细节都不能顺手动:调用点分不清
    "解析不出来"和"空网段",而那种混淆是静默的。
    """
    parts = ip.strip().split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        return ".".join(parts[:3])
    return None


def valid_subnet(text: str) -> str:
    """规范化 `/24` 网段前缀。**不合法返回空串**。

    与 `subnet_of` 的 None 不同是**有意的**:这一支的调用方是输入框校验,
    "空串 = 不合法"直接能当假值用。同样是原样搬过来的。
    """
    prefix = (text or "").strip().rstrip(".")
    parts = prefix.split(".")
    if len(parts) != 3 or not all(p.isdigit() and 0 <= int(p) <= 255
                                  for p in parts):
        return ""
    return prefix


#: 一次扫描最多探多少个地址。**这不是性能调优,是防手滑。**
#: 打一个 `/16` 进去是 65534 次探测 —— 按 64 并发、每个 0.4 秒算要七分钟,
#: 而界面上只会显示"正在探测",看起来就是卡死。超过就截断并如实说。
MAX_HOSTS = 4096


def parse_target(text: str) -> ipaddress.IPv4Network | None:
    """把用户输入变成一个网络。**认不出返回 None。**

    收四种写法,前两种是历史形式(等价于 /24):

    * ``192.168.1``      三段前缀
    * ``192.168.1.``     带尾点
    * ``192.0.2.0/24`` CIDR,**前缀长度随便**
    * ``192.0.2.5/22`` 主机位不为零也行(按 `strict=False` 归到网络地址)

    加 CIDR 是因为**很多人的网不是 /24**:办公室、宿舍、带 AP 的实验室
    常见 /22 甚至 /16。原来只认三段前缀,那些人的设备所在段根本扫不到,
    而界面上看起来是"这段里没有设备"。
    """
    s = (text or "").strip()
    if not s:
        return None
    if "/" in s:
        try:
            return ipaddress.ip_network(s, strict=False)     # type: ignore[return-value]
        except ValueError:
            return None
    s = s.rstrip(".")
    parts = s.split(".")
    if len(parts) == 3 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        s = f"{s}.0/24"
    elif len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        s = f"{s}/24"                       # 给了整地址就当它所在的 /24
    else:
        return None
    try:
        net = ipaddress.ip_network(s, strict=False)
    except ValueError:
        return None
    return net if isinstance(net, ipaddress.IPv4Network) else None


def target_hosts(net: ipaddress.IPv4Network, limit: int | None = None) -> list[str]:
    """网络里要探的地址。**截断到 `limit`**(默认 :data:`MAX_HOSTS`)。

    `/31`、`/32` 上 `hosts()` 的语义是特例(没有"可用主机"的概念),
    这里直接把网络地址本身算进去 —— 用户填 `/32` 就是想探那一个。
    """
    cap = MAX_HOSTS if limit is None else limit
    if net.prefixlen >= 31:
        return [str(a) for a in net][:cap]
    out: list[str] = []
    for ip in net.hosts():
        out.append(str(ip))
        if len(out) >= cap:
            break
    return out


def describe_target(text: str) -> str:
    """给界面用的一句话:这次要扫多大、会不会被截断。"""
    net = parse_target(text)
    if net is None:
        return ""
    ips = target_hosts(net)
    total = net.num_addresses if net.prefixlen >= 31 else max(
        net.num_addresses - 2, 0)
    if len(ips) < total:
        return _("{net} 共 {total} 个地址,只探前 {n} 个").format(
            net=net.with_prefixlen, total=total, n=len(ips))
    return _("{net} · {n} 个地址").format(net=net.with_prefixlen, n=len(ips))


def local_networks() -> list[str]:
    """本机每块网卡的网络,**带真实前缀长度**(``192.0.2.0/24``)。

    拿不到(平台不支持 / 枚举失败)就退回 `local_subnets()` 按地址猜的 /24 ——
    猜错的后果是扫漏,不是崩。
    """
    from astro_smb import netifaces

    nets = netifaces.local_networks()
    if nets:
        return nets
    return [f"{s}.0/24" for s in local_subnets()]


def local_subnets() -> list[str]:
    """本机所在的那些 /24(只有三段,历史形式)。

    **新代码用 :func:`local_networks`** —— 这一支按地址猜 /24,而很多人的网
    不是 /24。留着是因为老 UI 与几处调用点还按三段前缀在用。
    """
    out: list[str] = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None,
                                       socket.AF_INET):
            sub = subnet_of(info[4][0]) or ""
            if sub and sub not in out and not sub.startswith("127."):
                out.append(sub)
    except OSError:
        pass
    if not out:
        # 拿不到主机名解析时的兜底:开一个 UDP "连接"看本地端点是哪个网卡
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("192.0.2.1", 9))     # RFC 5737 文档网段,不会真发包
                sub = subnet_of(s.getsockname()[0]) or ""
                if sub:
                    out.append(sub)
        except OSError:
            pass
    return out


def discover(subnet: str, *,
             on_progress: Callable[[int, int, list[Device]], Any] | None = None,
             cancel: Callable[[], bool] | None = None,
             hosts: int | None = None, pool: int = POOL) -> list[Device]:
    """扫一个 /24,返回完成了 SMB 协商的设备(疑似 ASIAIR 排在前面)。

    ``on_progress(done, total, so_far)`` 每探完一个地址调一次 —— 界面要能
    边扫边出结果,不能等六秒憋一屏。``cancel()`` 返回 True 时尽快收手。
    """
    net = parse_target(subnet)
    if net is None:
        return []
    found: list[Device] = []
    done = 0

    def one(ip: str) -> Device | None:
        if cancel is not None and cancel():
            return None
        ok, rtt = probe(ip)
        if not ok:
            return None
        ident = identify(ip)                    # ← 判据:SMB 协商成功
        if ident is None:
            return None
        name, shares = ident
        return Device(ip=ip, name=name, hostname=resolve_hostname(ip),
                      shares=shares, rtt_ms=rtt)

    # `hosts` 是**上限**不是数量:给了三段前缀就是 254,给了 /22 就是 1022。
    # 老调用方传 `hosts=8` 来只探八个,那条语义保留。
    ips = target_hosts(net, limit=hosts)
    total = len(ips)
    with cf.ThreadPoolExecutor(max_workers=pool) as ex:
        for res in ex.map(one, ips):
            done += 1
            if res is not None:
                found.append(res)
            if on_progress is not None:
                on_progress(done, total, sort_devices(list(found)))
            if cancel is not None and cancel():
                break
    return sort_devices(found)


def preferred_subnets() -> list[str]:
    """本机的 /24,**按"最可能是真局域网"排序**。

    实测一台开着 VPN 的开发机能报出五个网段(`198.18.x` 是某些 VPN 的
    保留段、`100.64+` 是运营商级 NAT、`172.29.x`/`192.168.240.x` 是虚拟网卡)。
    一股脑全扫就是 5×254 = 1270 次探测、三十秒起 —— 而 ASIAIR 几乎总在
    那个"看起来最像家用网"的段里。

    排序只是**先后**,不是过滤:排在后面的照样会扫,只是等前面的没找到。
    """
    ranked: list[tuple[int, str]] = []
    for sub in local_subnets():
        a = int(sub.split(".")[0])
        b = int(sub.split(".")[1])
        if sub.startswith("192.168."):
            score = 0                       # 最常见的家用段
        elif a == 10:
            score = 1
        elif a == 172 and 16 <= b <= 31:
            score = 2
        elif a == 100 and 64 <= b <= 127:
            score = 8                       # 运营商级 NAT,几乎不会是自家设备
        elif a == 198 and b in (18, 19):
            score = 9                       # 基准测试保留段,常被 VPN 占用
        else:
            score = 5
        ranked.append((score, sub))
    return [sub for _score, sub in sorted(ranked)]



def preferred_networks() -> list[str]:
    """本机的网络(带前缀长度),**按"最可能是真局域网"排序**。

    与 :func:`preferred_subnets` 同一套打分,外加一条:**只有一两个地址的
    网段排到最后**。实测一台开着 VPN 的机器会报出 `198.18.0.0/30` 与
    `100.65.0.8/32` —— 那是隧道端点,里面不可能有 ASIAIR,而它们恰好
    因为"地址少"扫得飞快,一不留神就排到了前面。
    """
    ranked: list[tuple[int, int, str]] = []
    for cidr in local_networks():
        net = parse_target(cidr)
        if net is None:
            continue
        octs = str(net.network_address).split(".")
        a, b = int(octs[0]), int(octs[1])
        if octs[0] == "192" and octs[1] == "168":
            score = 0                       # 最常见的家用段
        elif a == 10:
            score = 1
        elif a == 172 and 16 <= b <= 31:
            score = 2
        elif a == 100 and 64 <= b <= 127:
            score = 8                       # 运营商级 NAT,几乎不会是自家设备
        elif a == 198 and b in (18, 19):
            score = 9                       # 基准测试保留段,常被 VPN 占用
        else:
            score = 5
        if net.prefixlen >= 30:
            score += 10                     # 点对点隧道,不是局域网
        # 同分时先扫小的:/24 比 /16 快得多,而设备多半就在那个 /24 里
        ranked.append((score, -net.prefixlen, net.with_prefixlen))
    return [c for _s, _p, c in sorted(ranked)]


def discover_all(subnets: Sequence[str] | None = None, *,
                 stop_on_asiair: bool = True, **kw) -> list[Device]:
    """本机所在的每个 /24 都扫一遍(按 `preferred_subnets` 的先后)。

    ``stop_on_asiair``:某一段里找到了疑似 ASIAIR 就不再往下扫。**这不是
    抄近路** —— 多网卡机器上后面那几段是 VPN / 虚拟网卡,继续扫只是多花
    二十几秒去确认那里什么也没有。要完整清单的场合(扫描页)传 False。
    """
    out: list[Device] = []
    # **按网络扫,不是按猜出来的 /24 扫。** 网卡是 /22 的机器上,
    # 按 /24 扫只覆盖了四分之一 —— 而界面上看起来是「这里没有设备」。
    for sub in (subnets if subnets is not None
                else preferred_networks()):
        got = discover(sub, **kw)
        out += got
        if stop_on_asiair and any(d.is_asiair for d in got):
            break
    return sort_devices(out)


def sort_devices(items: Sequence[Device]) -> list[Device]:
    """疑似 ASIAIR 置顶,其余按地址末段。"""
    def key(d: Device):
        try:
            last = int(d.ip.rsplit(".", 1)[-1])
        except ValueError:
            last = 999
        return (0 if d.is_asiair else 1, last)

    return sorted(items, key=key)


def pick_one(items: Sequence[Device]) -> Device | None:
    """能不能**无歧义地**挑一台自动连上。

    只有"恰好一台疑似 ASIAIR"才返回它。**两台就不挑** —— 替用户在两台设备
    之间做选择,一旦选错,他看到的是别人的片子,而界面上不会说"我替你选了"。
    """
    hits = [d for d in items if d.is_asiair]
    return hits[0] if len(hits) == 1 else None
