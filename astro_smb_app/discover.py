"""共享层的自动发现:**扫描在核心库**,这里只把结果排版成显示行。

分界线:探测网络是核心能力(`astro_smb.netscan`),把结果变成带措辞与配色
的行是视图模型(`views.scan.device_row`)。CLI 只要前者,两套前端要后者。
"""
from __future__ import annotations

from collections.abc import Sequence

from astro_smb.netscan import (Device, discover as discover_devices,
                               discover_all as discover_all_devices, pick_one)
from astro_smb_app.views import scan as sv

__all__ = ["Device", "pick_one", "discover", "discover_all", "to_rows"]


def to_rows(items: Sequence[Device]) -> list[dict]:
    return [sv.device_row(d.ip, d.name, d.shares, d.rtt_ms, d.hostname)
            for d in items]


def discover(subnet: str, **kw) -> list[dict]:
    """扫一个 /24,返回**显示行**。``on_progress`` 收到的也是行。"""
    op = kw.pop("on_progress", None)
    if op is not None:
        kw["on_progress"] = lambda d, t, found: op(d, t, to_rows(found))
    return to_rows(discover_devices(subnet, **kw))


def discover_all(subnets: Sequence[str] | None = None, **kw) -> list[dict]:
    op = kw.pop("on_progress", None)
    if op is not None:
        kw["on_progress"] = lambda d, t, found: op(d, t, to_rows(found))
    return to_rows(discover_all_devices(subnets, **kw))
