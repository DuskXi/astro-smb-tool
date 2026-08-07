"""扫描设备页的**视图模型**:局域网探测与结果判读。

**判据只认 SMB 协商,不认 TCP。** 用户那台路由器(RT-BE88U)会对**整个网段**的
445 SYN 秒回 ACK(1ms 就"连上"),但只有真机能完成 SMB 协商 —— 只看 TCP 的话
254 个 IP 会误报 200 多台。这条是真机踩出来的,别改回去。

时延着色的阈值(<30ms 绿 / <100ms 琥珀 / 否则红)与老 UI 一致,但这里返回
**语义名**而不是画刷 —— 颜色是渲染器的事。
"""
from __future__ import annotations

import socket
import time
from astro_smb.i18n import gettext as _

# **原语搬到核心库了**(`astro_smb.netscan`)。CLI 也要自动发现,而核心库
# 不能反向依赖共享层 —— 复制一份的话,"以 SMB 协商为准"这条真机纪律迟早
# 有一份被改回只看 TCP。这里按老名字转出去,调用点一个字节没改。
from astro_smb.netscan import (          # noqa: E402
    identify as _identify,
    local_subnets as _local_subnets,
    probe as _probe,
    resolve_hostname as _resolve_hostname,
    subnet_of as _subnet_of,
    valid_subnet,
)



def latency_tone(rtt_ms: float | None) -> str | None:
    """时延 → 语义色名。老 UI 那边直接返回 SolidColorBrush,那是 UI 的事。"""
    if rtt_ms is None:
        return None
    if rtt_ms < 30:
        return "ok"
    if rtt_ms < 100:
        return "warn"
    return "error"














def device_row(ip: str, name: str, shares, rtt_ms: float | None,
               hostname: str = "") -> dict:
    """一台被确认的 SMB 设备 → 一行显示数据。

    疑似 ASIAIR(共享名含 "Images" 或设备名含 ASIAIR)置顶并标星 —— 这台设备
    才是用户来这一页的目的,埋在一堆 NAS 里等于没扫。
    """
    shares = list(shares or ())
    is_asiair = (any("Images" in s for s in shares)
                 or "ASIAIR" in (name or "").upper()
                 or "ASIAIR" in (hostname or "").upper())
    # **判读要写出来**:这一页的目的就是把 ASIAIR 从一堆 NAS 里认出来。
    # 只列共享名的话,"这台不是 ASIAIR"要用户自己看出来 —— 而这正是
    # 老 UI 那句「可能是 PC/NAS,非 ASIAIR」在做的事。
    bits = [_("疑似 ASIAIR") if is_asiair else _("SMB 设备(可能是 PC/NAS,非 ASIAIR)")]
    if shares:
        bits.append(_("{0} 共享: ").format(len(shares)) + _("、").join(shares[:3])
                    + ("…" if len(shares) > 3 else ""))
    else:
        bits.append(_("无匿名共享(可能需要认证)"))
    return {
        "key": ip,
        "ip": ip,
        "title": hostname or name or ip,
        "sub": " · ".join(bits),
        "rtt": f"{rtt_ms:.0f} ms" if rtt_ms is not None else "—",
        "tone": latency_tone(rtt_ms),
        "asiair": is_asiair,
        "shares": shares,
    }


def sort_rows(rows: list[dict]) -> list[dict]:
    """疑似 ASIAIR 置顶,其余按 IP 末段。"""
    def key(r):
        try:
            last = int(r["ip"].rsplit(".", 1)[-1])
        except (ValueError, IndexError):
            last = 999
        return (0 if r.get("asiair") else 1, last)

    return sorted(rows, key=key)
