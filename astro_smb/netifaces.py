"""本机网卡的 IPv4 地址**连同真实前缀长度**。纯 ctypes,不加依赖。

## 为什么不能只看地址

`socket.getaddrinfo(gethostname())` 只给地址,给不了掩码 —— 于是全项目
一直默认"本机在一个 /24 里"。**很多人的网不是 /24**:家用路由器给 /24,
而办公室、宿舍、带 AP 的实验室常见 /22 甚至 /16。默认 /24 的后果是
扫描页把设备所在的那一段**扫漏了**,而界面上看起来是"这段里没有设备"。

## 拿不到就退回去,不要报错

三个平台各一条路(Windows 的 `GetAdaptersAddresses`、POSIX 的
`getifaddrs`),任何一条不通都返回空列表,由调用方退回"按地址猜 /24"。
**网卡枚举失败不该让扫描页打不开。**

## `sockaddr` 的坑

Linux 的 `sockaddr` 是 ``{uint16 sa_family; char sa_data[14]}``,而
macOS / BSD 是 ``{uint8 sa_len; uint8 sa_family; char sa_data[14]}`` ——
**family 的偏移不一样**。IPv4 地址本身在两边都落在偏移 4,所以只有读
family 那一步要分平台。读错的表现不是崩,是把 IPv6 地址当成 IPv4 解出
一个荒唐的网段。
"""
from __future__ import annotations

import ctypes
import ipaddress
import socket
import sys

__all__ = ["local_networks"]

#: BSD 系(macOS 也算)的 `sockaddr` 头一个字节是长度,family 在第二个
_BSD_SOCKADDR = sys.platform.startswith(("darwin", "freebsd", "openbsd",
                                         "netbsd"))


def local_networks() -> list[str]:
    # publish-scan: ok(文档串里的 CIDR 例子,不是真实地址)
    """本机每块网卡的 IPv4 网络,形如 ``192.168.1.0/24``。

    去掉回环与链路本地(APIPA);顺序按枚举顺序,去重。拿不到返回 ``[]``。
    """
    try:
        if sys.platform == "win32":
            raw = _windows()
        else:
            raw = _getifaddrs()
    except Exception:                       # noqa: BLE001 - 见模块文档
        return []

    out: list[str] = []
    for addr, prefix in raw:
        try:
            net = ipaddress.ip_network(f"{addr}/{prefix}", strict=False)
        except ValueError:
            continue
        if net.is_loopback or net.is_link_local:
            continue
        text = str(net)
        if text not in out:
            out.append(text)
    return out


# ---------------------------------------------------------------- POSIX

class _IfAddrs(ctypes.Structure):
    pass


_IfAddrs._fields_ = [
    ("ifa_next", ctypes.POINTER(_IfAddrs)),
    ("ifa_name", ctypes.c_char_p),
    ("ifa_flags", ctypes.c_uint),
    ("ifa_addr", ctypes.c_void_p),
    ("ifa_netmask", ctypes.c_void_p),
    ("ifa_dstaddr", ctypes.c_void_p),
    ("ifa_data", ctypes.c_void_p),
]


def _sa_family(ptr: int) -> int:
    if not ptr:
        return -1
    if _BSD_SOCKADDR:
        return ctypes.cast(ptr, ctypes.POINTER(ctypes.c_uint8))[1]
    return ctypes.cast(ptr, ctypes.POINTER(ctypes.c_uint16))[0]


def _sa_ipv4(ptr: int) -> str:
    """`sockaddr_in` 里的地址。**两个平台都在偏移 4**。"""
    buf = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_uint8 * 8)).contents
    return ".".join(str(b) for b in buf[4:8])


def _getifaddrs() -> list[tuple[str, int]]:
    libc = ctypes.CDLL(None, use_errno=True)
    head = ctypes.POINTER(_IfAddrs)()
    if libc.getifaddrs(ctypes.byref(head)) != 0:
        return []
    out: list[tuple[str, int]] = []
    try:
        node = head
        while node:
            cur = node.contents
            if (_sa_family(cur.ifa_addr) == socket.AF_INET
                    and _sa_family(cur.ifa_netmask) == socket.AF_INET):
                addr = _sa_ipv4(cur.ifa_addr)
                mask = _sa_ipv4(cur.ifa_netmask)
                try:
                    prefix = ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
                except ValueError:
                    prefix = 24
                out.append((addr, prefix))
            node = cur.ifa_next
    finally:
        libc.freeifaddrs(head)
    return out


# ---------------------------------------------------------------- Windows

_AF_UNSPEC = 0
#: 只要单播地址,跳过所有我们用不上的
_GAA_FLAGS = 0x0010 | 0x0020 | 0x0080     # SKIP_ANYCAST|SKIP_MULTICAST|SKIP_FRIENDLY
_IF_OPER_UP = 1


class _SockaddrIn(ctypes.Structure):
    _fields_ = [("sin_family", ctypes.c_ushort), ("sin_port", ctypes.c_ushort),
                ("sin_addr", ctypes.c_uint8 * 4), ("sin_zero", ctypes.c_uint8 * 8)]


class _SocketAddress(ctypes.Structure):
    _fields_ = [("lpSockaddr", ctypes.POINTER(_SockaddrIn)),
                ("iSockaddrLength", ctypes.c_int)]


class _UnicastAddress(ctypes.Structure):
    pass


_UnicastAddress._fields_ = [
    ("Length", ctypes.c_ulong), ("Flags", ctypes.c_ulong),
    ("Next", ctypes.POINTER(_UnicastAddress)),
    ("Address", _SocketAddress),
    ("PrefixOrigin", ctypes.c_int), ("SuffixOrigin", ctypes.c_int),
    ("DadState", ctypes.c_int),
    ("ValidLifetime", ctypes.c_ulong), ("PreferredLifetime", ctypes.c_ulong),
    ("LeaseLifetime", ctypes.c_ulong),
    # **这一个字节就是前缀长度**,`IP_ADAPTER_UNICAST_ADDRESS_LH` 才有
    ("OnLinkPrefixLength", ctypes.c_uint8),
]


class _Adapter(ctypes.Structure):
    pass


_Adapter._fields_ = [
    ("Length", ctypes.c_ulong), ("IfIndex", ctypes.c_ulong),
    ("Next", ctypes.POINTER(_Adapter)),
    ("AdapterName", ctypes.c_char_p),
    ("FirstUnicastAddress", ctypes.POINTER(_UnicastAddress)),
    ("FirstAnycastAddress", ctypes.c_void_p),
    ("FirstMulticastAddress", ctypes.c_void_p),
    ("FirstDnsServerAddress", ctypes.c_void_p),
    ("DnsSuffix", ctypes.c_wchar_p),
    ("Description", ctypes.c_wchar_p),
    ("FriendlyName", ctypes.c_wchar_p),
    ("PhysicalAddress", ctypes.c_uint8 * 8),
    ("PhysicalAddressLength", ctypes.c_ulong),
    ("Flags", ctypes.c_ulong), ("Mtu", ctypes.c_ulong),
    ("IfType", ctypes.c_ulong), ("OperStatus", ctypes.c_int),
]


def _windows() -> list[tuple[str, int]]:
    iphlpapi = ctypes.WinDLL("Iphlpapi.dll")     # noqa: F821 - 只在 Windows 走
    size = ctypes.c_ulong(15 * 1024)
    for _ in range(3):
        buf = ctypes.create_string_buffer(size.value)
        rc = iphlpapi.GetAdaptersAddresses(
            _AF_UNSPEC, _GAA_FLAGS, None, buf, ctypes.byref(size))
        if rc == 0:
            break
        if rc != 111:                            # ERROR_BUFFER_OVERFLOW
            return []
    else:
        return []

    out: list[tuple[str, int]] = []
    node = ctypes.cast(buf, ctypes.POINTER(_Adapter))
    while node:
        cur = node.contents
        # **只要在用的网卡。** 断开的以太网口、没连的 Wi-Fi 也会被列出来,
        # 而它们的地址是陈的 —— 扫那种段纯属浪费半分钟。
        if cur.OperStatus == _IF_OPER_UP:
            ua = cur.FirstUnicastAddress
            while ua:
                a = ua.contents
                sa = a.Address.lpSockaddr
                if sa and sa.contents.sin_family == socket.AF_INET:
                    addr = ".".join(str(b) for b in sa.contents.sin_addr)
                    out.append((addr, int(a.OnLinkPrefixLength)))
                ua = a.Next
        node = cur.Next
    return out
