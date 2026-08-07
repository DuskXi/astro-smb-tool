"""轻量天文计算(纯标准库, 精度面向可视化, 非天体测量级)。

- ASIAIR 日志坐标字符串解析: '17h22m35s' / '-36°7'40"'
- 儒略日 / 格林尼治恒星时(GMST) / 本地恒星时(LST)
- 赤道坐标 → 地平坐标(alt/az), 供天球图绘制
- 站点经度反推: LST = RA + HA, 已知 UTC 时刻的 GMST 即得经度
  (HA 来自 PHD2 段头 'Hour angle = ... hr', RA 用同时刻拍摄目标的 RA 近似)

时间约定: 日志/文件名里的时间是设备本地时间(naive datetime)。本模块用
``datetime.timestamp()`` 把 naive 当作**本机时区**换算 UTC —— 前提是运行本
客户端的 PC 与 ASIAIR 处于同一时区(实际部署即如此)。
"""
from __future__ import annotations

import math
import re
from datetime import datetime

_RE_RA = re.compile(r"^\s*(\d+)h\s*(\d+)m\s*([\d.]+)s?\s*$")
_RE_DEC = re.compile(r"^\s*([+-]?)(\d+)[°d]\s*(\d+)['m]\s*([\d.]+)[\"s]?\s*$")


def ra_str_to_deg(s: str | None) -> float | None:
    """'17h22m35s' → 度(0~360)。解析失败返回 None。"""
    if not s:
        return None
    m = _RE_RA.match(s)
    if not m:
        return None
    h = int(m.group(1)) + int(m.group(2)) / 60.0 + float(m.group(3)) / 3600.0
    return (h % 24.0) * 15.0


def dec_str_to_deg(s: str | None) -> float | None:
    """'-36°7'40"' → 度(-90~+90)。解析失败返回 None。"""
    if not s:
        return None
    m = _RE_DEC.match(s)
    if not m:
        return None
    v = int(m.group(2)) + int(m.group(3)) / 60.0 + float(m.group(4)) / 3600.0
    if m.group(1) == "-":
        v = -v
    return max(-90.0, min(90.0, v))


def format_ra(deg: float) -> str:
    h = (deg / 15.0) % 24.0
    hh = int(h)
    mm = int((h - hh) * 60)
    ss = round(((h - hh) * 60 - mm) * 60)
    if ss == 60:
        ss = 0
        mm += 1
    if mm == 60:
        mm = 0
        hh = (hh + 1) % 24
    return f"{hh:02d}h{mm:02d}m{ss:02d}s"


def format_dec(deg: float) -> str:
    sign = "-" if deg < 0 else "+"
    v = abs(deg)
    dd = int(v)
    mm = int((v - dd) * 60)
    ss = round(((v - dd) * 60 - mm) * 60)
    if ss == 60:
        ss = 0
        mm += 1
    if mm == 60:
        mm = 0
        dd += 1
    return f"{sign}{dd:02d}°{mm:02d}'{ss:02d}\""


def unix_from_local(dt: datetime) -> float:
    """naive 本地时间 → unix 秒(按本机时区)。"""
    return dt.timestamp()


def gmst_deg(unix_ts: float) -> float:
    """格林尼治平恒星时(度)。IAU 简化式, 误差 << 1', 可视化足够。"""
    jd = unix_ts / 86400.0 + 2440587.5
    t = jd - 2451545.0
    g = 280.46061837 + 360.98564736629 * t
    return g % 360.0


def lst_deg(unix_ts: float, lon_deg: float) -> float:
    """本地恒星时(度)。lon_deg 东经为正。"""
    return (gmst_deg(unix_ts) + lon_deg) % 360.0


def altaz(ra_deg: float, dec_deg: float,
          lat_deg: float, lon_deg: float,
          unix_ts: float) -> tuple[float, float]:
    """赤道坐标 → 地平坐标。返回 (alt 高度角, az 方位角, 均为度)。

    az 从正北起顺时针(N=0, E=90, S=180, W=270)。
    """
    ha = math.radians((lst_deg(unix_ts, lon_deg) - ra_deg) % 360.0)
    dec = math.radians(dec_deg)
    lat = math.radians(lat_deg)
    sin_alt = (math.sin(dec) * math.sin(lat)
               + math.cos(dec) * math.cos(lat) * math.cos(ha))
    alt = math.asin(max(-1.0, min(1.0, sin_alt)))
    az = math.atan2(
        -math.sin(ha) * math.cos(dec),
        math.sin(dec) * math.cos(lat)
        - math.cos(dec) * math.sin(lat) * math.cos(ha))
    return math.degrees(alt), math.degrees(az) % 360.0


def estimate_longitude(ra_deg: float, ha_hours: float,
                       when_local: datetime) -> float:
    """由 目标 RA + 该时刻的时角 HA 反推站点经度(度, 东经为正, [-180,180])。

    原理: LST = RA + HA; lon = LST - GMST。
    HA 取自 PHD2 导星段头(导星视场≈拍摄目标, RA 差引入的经度误差 <~1°)。
    """
    lst = (ra_deg + ha_hours * 15.0) % 360.0
    lon = (lst - gmst_deg(unix_from_local(when_local))) % 360.0
    if lon > 180.0:
        lon -= 360.0
    return lon


def radec_from_altaz(alt_deg: float, az_deg: float,
                     lat_deg: float, lon_deg: float,
                     unix_ts: float) -> tuple[float, float]:
    """altaz() 的逆变换:地平坐标 → 赤道坐标 (ra, dec, 度)。

    与 altaz() 同一旋转的镜像形式,单测做双向往返校验。
    """
    alt = math.radians(alt_deg)
    az = math.radians(az_deg)
    lat = math.radians(lat_deg)
    sin_dec = (math.sin(alt) * math.sin(lat)
               + math.cos(alt) * math.cos(lat) * math.cos(az))
    dec = math.asin(max(-1.0, min(1.0, sin_dec)))
    ha = math.atan2(
        -math.sin(az) * math.cos(alt),
        math.sin(alt) * math.cos(lat)
        - math.cos(alt) * math.sin(lat) * math.cos(az))
    ra = (lst_deg(unix_ts, lon_deg) - math.degrees(ha)) % 360.0
    return ra, math.degrees(dec)


# 银道坐标(J2000/IAU 1958 定义):北银极与银经零点常数
_GAL_NGP_RA = math.radians(192.85948)   # 北银极赤经
_GAL_NGP_DEC = math.radians(27.12825)   # 北银极赤纬
_GAL_LON_NCP = math.radians(122.93192)  # 北天极的银经


def galactic_from_radec(ra_deg: float, dec_deg: float) -> tuple[float, float]:
    """赤道 J2000 → 银道 (l, b, 度)。校验值:人马座 A* → l≈359.94, b≈-0.05。"""
    ra = math.radians(ra_deg)
    dec = math.radians(dec_deg)
    sin_b = (math.sin(dec) * math.sin(_GAL_NGP_DEC)
             + math.cos(dec) * math.cos(_GAL_NGP_DEC)
             * math.cos(ra - _GAL_NGP_RA))
    b = math.asin(max(-1.0, min(1.0, sin_b)))
    l = _GAL_LON_NCP - math.atan2(
        math.cos(dec) * math.sin(ra - _GAL_NGP_RA),
        math.sin(dec) * math.cos(_GAL_NGP_DEC)
        - math.cos(dec) * math.sin(_GAL_NGP_DEC) * math.cos(ra - _GAL_NGP_RA))
    return math.degrees(l) % 360.0, math.degrees(b)


def radec_from_galactic(l_deg: float, b_deg: float) -> tuple[float, float]:
    """银道 → 赤道 J2000 (ra, dec, 度)。与 galactic_from_radec 互逆。"""
    l = math.radians(l_deg)
    b = math.radians(b_deg)
    sin_dec = (math.sin(b) * math.sin(_GAL_NGP_DEC)
               + math.cos(b) * math.cos(_GAL_NGP_DEC)
               * math.cos(_GAL_LON_NCP - l))
    dec = math.asin(max(-1.0, min(1.0, sin_dec)))
    ra = _GAL_NGP_RA + math.atan2(
        math.cos(b) * math.sin(_GAL_LON_NCP - l),
        math.sin(b) * math.cos(_GAL_NGP_DEC)
        - math.cos(b) * math.sin(_GAL_NGP_DEC) * math.cos(_GAL_LON_NCP - l))
    return math.degrees(ra) % 360.0, math.degrees(dec)


def hours_visible(dec_deg: float, lat_deg: float,
                  min_alt_deg: float = 0.0) -> float | None:
    """目标每天高于 min_alt 的小时数; 恒显返回 24, 恒隐返回 0, 极端返回 None 不会发生。"""
    dec = math.radians(dec_deg)
    lat = math.radians(lat_deg)
    h0 = math.radians(min_alt_deg)
    cos_h = ((math.sin(h0) - math.sin(lat) * math.sin(dec))
             / max(1e-9, math.cos(lat) * math.cos(dec)))
    if cos_h <= -1.0:
        return 24.0
    if cos_h >= 1.0:
        return 0.0
    return math.degrees(math.acos(cos_h)) / 15.0 * 2.0
