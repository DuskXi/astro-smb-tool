"""Tycho-2 星表:自研紧凑打包格式 + 锥形查询 + 下载器(纯 numpy/标准库)。

板解算需要一份**本地可离线查询**的星表。现成方案都被否决过:UCAC4 8.27 GB、
2MASS 39.7 GB、Hipparcos 密度太低(2000mm 视场里不到 1 颗)、BSC5 位置只到角秒级、
astrometry.net 索引文件 2.31 GB 且 4100/5200 系列没有 license 声明、
Gaia 是 CC BY-NC 3.0 IGO **明确禁止商用**。剩下唯一合适的是 **Tycho-2**:
2,539,913 条,99% 完备到 V=11.0,位置精度 60 mas(400mm 焦距下仅 0.03 像素)。

原始 CDS 数据 158.9 MiB(gz)/ 501.4 MiB(解压),对一个桌面客户端太重。
本模块定义一个 **14 字节/星** 的打包格式,整表 **35.6 MB**:

===========  =========  ==============================================
字段         类型       含义
===========  =========  ==============================================
``ra``       int32      赤经,**微度**(0 ~ 359 999 999)
``dec``      int32      赤纬,**微度**(-90 000 000 ~ +90 000 000)
``vmag``     uint16     V 星等,**毫星等 + 偏移**(见 :data:`VMAG_OFFSET`)
``pmra``     int16      自行 pmRA*cos(dec),mas/yr
``pmde``     int16      自行 pmDE,mas/yr
===========  =========  ==============================================

量化误差 1 微度 = **3.6 mas**,远小于 Tycho-2 自身 60 mas 的位置精度。
自行**必须存**:J2000 到 2026 已 26 年,2000mm 焦距下累计 1~3 像素。

四段职责,可以分别使用、分别测试:

1. **格式**(:func:`pack_header` / :func:`unpack_header` / :func:`encode_records` /
   :func:`decode_*`)—— 64 字节文件头 + 紧凑记录区。**版本不兼容一律抛
   :class:`CatalogError`,绝不"尽力解析"** —— 错位解析出来的星表会让板解算
   静默地全盘失败,比直接报错难查一百倍。
2. **查询**(:class:`Catalog`)—— 打包文件**按 dec 升序**存放(文件头 flag 承诺,
   :meth:`Catalog.verify_sorted` 复核)。锥形查询 = ``np.searchsorted`` 取 dec 带
   + float32 单位向量点积粗筛 + float64 精确角距复筛。**不用 HEALPix** ——
   难点从来不是 ang2pix 而是 query_disc。整表 254 万星实测:半径 0.5° 约
   0.06 ms、1° 约 0.12 ms、2.5° 约 0.45 ms(2.5° 锥内平均 1200 颗),
   比板解算的需求快两个量级。
3. **下载**(:func:`ensure_catalog`)—— 缓存在
   ``%LOCALAPPDATA%/AstroSmbTool/catalog/``,原子落盘,**绝不信 HTTP 200**:
   下完必须校验 magic/版本/条目数/文件长度/sha256,任一不符即丢弃。
   证书兜底链与 skymap 同源:urllib(补装系统根证书)失败 → ``curl.exe``。
4. **构建** —— 见 :mod:`astro_smb.catalog_build`(从 CDS 原始 ``tyc2.dat`` 生成)。

坐标约定:ra/dec 一律 **度**,ICRS/J2000,与 :mod:`astro_smb.astro` 一致。
``epoch`` 一律 **儒略年**(2000.0 = J2000),用 :func:`jyear_from_unix` 换算。
"""

from __future__ import annotations

from astro_smb import paths
from astro_smb.i18n import gettext as _
import hashlib
import math
import os
import ssl
import struct
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import numpy as np

__all__ = [
    "CatalogError",
    "MAGIC", "FORMAT_VERSION", "HEADER_BYTES", "RECORD_BYTES", "RECORD_DTYPE",
    "VMAG_OFFSET", "MICRO", "PM_SCALE", "FLAG_SORTED_DEC", "J2000_EPOCH",
    "CatalogHeader", "Catalog",
    "pack_header", "unpack_header",
    "encode_records", "decode_ra", "decode_dec", "decode_vmag",
    "write_catalog", "write_records", "read_catalog_header",
    "validate_catalog_file",
    "angular_separation", "unit_vectors", "apply_proper_motion",
    "jyear_from_unix",
    "catalog_dir", "catalog_path", "catalog_available", "ensure_catalog",
    "CATALOG_URL", "CATALOG_SHA256", "CATALOG_COUNT", "CATALOG_FILENAME",
    "VERIFIED_CATALOG_SHA256", "VERIFIED_CATALOG_COUNT",
]


class CatalogError(Exception):
    """星表文件不存在/损坏/版本不兼容,或下载校验失败。"""


# ---------------------------------------------------------------- 格式常量

MAGIC = b"ASTARCAT"          # 8 字节,固定
FORMAT_VERSION = 1           # 结构变更必须 +1(读侧对不上直接报错)
HEADER_BYTES = 64
RECORD_BYTES = 14

#: 记录区 numpy 结构化 dtype。**小端、不对齐**(itemsize 恰好 14)。
RECORD_DTYPE = np.dtype([
    ("ra", "<i4"),      # 微度
    ("dec", "<i4"),     # 微度
    ("vmag", "<u2"),    # 毫星等 + VMAG_OFFSET
    ("pmra", "<i2"),    # mas/yr(已含 cos(dec) 因子)
    ("pmde", "<i2"),    # mas/yr
], align=False)

MICRO = 1_000_000.0         # 度 → 微度
PM_SCALE = 3_600_000.0      # mas → 度(1 度 = 3.6e6 mas)

#: V 星等偏移(毫星等无符号存储):stored = round((V + 2.0) * 1000)。
#: 覆盖 V ∈ [-2.0, +63.5];Tycho-2 主表实际范围约 1.9 ~ 15.2,余量充足。
VMAG_OFFSET = 2.0

FLAG_SORTED_DEC = 1 << 0    # 记录区按 dec 升序(锥形查询的前提)

J2000_EPOCH = 2000.0

# 文件头:magic(8) 版本(2) 记录长(2) 条目数(4) 历元(8) 生成时刻(8) flags(4)
# 来源(24) + 4 字节保留 = 64 字节
_HEADER_STRUCT = struct.Struct("<8sHHIddI24s4x")
assert _HEADER_STRUCT.size == HEADER_BYTES

# 锥形粗筛的 dec 带外扩余量(度)。带边界本身已经按微度 floor/ceil 取整,
# 这里再留 3.6" 属于双保险。
_EDGE_EPS_DEG = 1e-3

# 粗筛阈值在 **cos 域** 上的松弛量。**这一条修过一个真实 bug,别去掉**:
#
#   float32 在 1.0 附近的分辨率是 1.19e-7。半径小于约 0.03°(1.7')时,
#   cos(半径) = 1 - 1.5e-10 会被 np.float32() 舍入成**恰好 1.0**,
#   而 float32 点积因舍入几乎不可能恰好等于 1.0 ⇒ **整片真成员被静默排除**,
#   cone() 对小半径返回空集。实测:r=2e-5° 查一颗表内确实存在的星,
#   暴力最近邻 0.0001",cone 却返回 0 条。
#
# 松弛 2e-6 同时覆盖两个极端:靠近 1.0 时 (1 - 2e-6) 在 float32 里可分辨;
# 大半径时对应的角度过冲仅约 0.001°(且随后被 float64 精筛剔除,纯属浪费一点算力)。
_COS_SLACK = 2e-6


@dataclass(frozen=True)
class CatalogHeader:
    """打包文件的 64 字节文件头。"""

    version: int
    record_bytes: int
    count: int
    epoch: float            # 存储位置所在历元(儒略年),Tycho-2 为 2000.0
    built_unix: float       # 生成时刻(unix 秒)
    flags: int
    source: str             # 数据来源短名,如 "Tycho-2"

    @property
    def sorted_by_dec(self) -> bool:
        return bool(self.flags & FLAG_SORTED_DEC)

    @property
    def data_bytes(self) -> int:
        return self.count * self.record_bytes

    @property
    def file_bytes(self) -> int:
        return HEADER_BYTES + self.data_bytes


def pack_header(hdr: CatalogHeader) -> bytes:
    """CatalogHeader → 64 字节。"""
    if hdr.count < 0 or hdr.count > 0xFFFFFFFF:
        raise CatalogError(_("条目数超出范围: {count}").format(count=hdr.count))
    src = hdr.source.encode("utf-8", errors="replace")[:24]
    return _HEADER_STRUCT.pack(
        MAGIC, int(hdr.version), int(hdr.record_bytes), int(hdr.count),
        float(hdr.epoch), float(hdr.built_unix), int(hdr.flags), src)


def unpack_header(data: bytes) -> CatalogHeader:
    """64 字节 → CatalogHeader。

    magic / 版本 / 记录长度任一不符都**直接抛错**,不做兼容性猜测:
    用错位的偏移解析星表,只会让板解算无声地全盘失败。
    """
    if len(data) < HEADER_BYTES:
        raise CatalogError(
            _("星表文件头不完整: 需要 {HEADER_BYTES} 字节, 实际 {0}").format(
                len(data), HEADER_BYTES=HEADER_BYTES))
    (magic, version, rec_bytes, count, epoch,
     built, flags, src) = _HEADER_STRUCT.unpack(data[:HEADER_BYTES])
    if magic != MAGIC:
        raise CatalogError(
            _("星表 magic 不匹配: 期望 {MAGIC!r}, 实际 {magic!r} — 不是本格式的文件").format(
                MAGIC=MAGIC, magic=magic))
    if version != FORMAT_VERSION:
        raise CatalogError(
            _("星表格式版本不兼容: 文件为 v{version}, 本程序支持 v{FORMAT_VERSION} — 请重新下载星表").format(
                version=version, FORMAT_VERSION=FORMAT_VERSION))
    if rec_bytes != RECORD_BYTES:
        raise CatalogError(
            _("星表记录长度不匹配: 文件为 {rec_bytes} 字节, 期望 {RECORD_BYTES}").format(
                rec_bytes=rec_bytes, RECORD_BYTES=RECORD_BYTES))
    return CatalogHeader(
        version=version, record_bytes=rec_bytes, count=count,
        epoch=epoch, built_unix=built, flags=flags,
        source=src.rstrip(b"\x00").decode("utf-8", errors="replace"))


# ---------------------------------------------------------------- 编解码

def encode_records(ra_deg, dec_deg, vmag, pmra, pmde) -> np.ndarray:
    """物理量 → :data:`RECORD_DTYPE` 结构化数组(量化 + 截断保护)。

    - ``ra_deg`` 先取模到 [0, 360),再量化;359.9999999 这类值不会溢出成 360。
    - ``dec_deg`` 钳到 [-90, 90]。
    - ``vmag`` 钳到 uint16 可表示区间(见 :data:`VMAG_OFFSET`)。
    - ``pmra``/``pmde`` 钳到 int16(Tycho-2 实际最大约 ±10 300 mas/yr,余量充足)。

    NaN 一律按"缺失"处理:位置 NaN 抛错(没有位置的星没有意义),
    自行 NaN 记为 0,星等 NaN 记为可表示的最暗值。
    """
    ra = np.asarray(ra_deg, dtype=np.float64).ravel()
    dec = np.asarray(dec_deg, dtype=np.float64).ravel()
    vm = np.asarray(vmag, dtype=np.float64).ravel()
    pa = np.asarray(pmra, dtype=np.float64).ravel()
    pd = np.asarray(pmde, dtype=np.float64).ravel()
    n = ra.size
    if not (dec.size == vm.size == pa.size == pd.size == n):
        raise CatalogError(_("encode_records: 各字段长度不一致"))
    if np.isnan(ra).any() or np.isnan(dec).any():
        raise CatalogError(_("encode_records: 位置含 NaN"))

    rec = np.zeros(n, dtype=RECORD_DTYPE)
    # 先取模再四舍五入,再对 360e6 取模兜住 359.9999995 → 360000000 的进位
    ra_micro = np.round(np.mod(ra, 360.0) * MICRO)
    rec["ra"] = np.mod(ra_micro, 360.0 * MICRO).astype(np.int64)
    rec["dec"] = np.round(np.clip(dec, -90.0, 90.0) * MICRO).astype(np.int64)

    vm = np.where(np.isnan(vm), (65535.0 / 1000.0) - VMAG_OFFSET, vm)
    rec["vmag"] = np.clip(
        np.round((vm + VMAG_OFFSET) * 1000.0), 0.0, 65535.0).astype(np.int64)

    for name, src in (("pmra", pa), ("pmde", pd)):
        v = np.where(np.isnan(src), 0.0, src)
        rec[name] = np.clip(np.round(v), -32768.0, 32767.0).astype(np.int64)
    return rec


def decode_ra(ra_micro) -> np.ndarray:
    """微度 → 度(float64)。"""
    return np.asarray(ra_micro, dtype=np.float64) / MICRO


def decode_dec(dec_micro) -> np.ndarray:
    """微度 → 度(float64)。"""
    return np.asarray(dec_micro, dtype=np.float64) / MICRO


def decode_vmag(vmag_milli) -> np.ndarray:
    """毫星等(带偏移)→ 星等(float64)。"""
    return np.asarray(vmag_milli, dtype=np.float64) / 1000.0 - VMAG_OFFSET


# ---------------------------------------------------------------- 球面数学

def unit_vectors(ra_deg, dec_deg, dtype=np.float64) -> np.ndarray:
    """(ra, dec) 度 → (N, 3) 单位向量。x 指向春分点, z 指向北天极。"""
    ra = np.radians(np.asarray(ra_deg, dtype=np.float64))
    dec = np.radians(np.asarray(dec_deg, dtype=np.float64))
    cd = np.cos(dec)
    out = np.empty(ra.shape + (3,), dtype=dtype)
    out[..., 0] = cd * np.cos(ra)
    out[..., 1] = cd * np.sin(ra)
    out[..., 2] = np.sin(dec)
    return out


def angular_separation(ra1, dec1, ra2, dec2) -> np.ndarray:
    """球面角距(度),haversine 形式 —— 小角度下不会像 acos 那样掉精度。

    支持广播:标量中心 vs 数组、数组 vs 数组都可以。
    """
    ra1 = np.radians(np.asarray(ra1, dtype=np.float64))
    dec1 = np.radians(np.asarray(dec1, dtype=np.float64))
    ra2 = np.radians(np.asarray(ra2, dtype=np.float64))
    dec2 = np.radians(np.asarray(dec2, dtype=np.float64))
    sdd = np.sin((dec2 - dec1) * 0.5)
    sdr = np.sin((ra2 - ra1) * 0.5)
    h = sdd * sdd + np.cos(dec1) * np.cos(dec2) * sdr * sdr
    return np.degrees(2.0 * np.arcsin(np.sqrt(np.clip(h, 0.0, 1.0))))


def apply_proper_motion(ra_deg, dec_deg, pmra, pmde,
                        dt_years: float) -> tuple[np.ndarray, np.ndarray]:
    """一阶自行外推。``pmra`` 已含 cos(dec) 因子(Tycho-2 的 pmRA* 约定)。

    ``dt_years`` 是相对存储历元的儒略年差。数十年的基线上位移 < 0.1°,
    一阶线性足够(Tycho-2 自身位置精度 60 mas);极区 cos(dec) → 0 会放大
    赤经增量,这里对 cos(dec) 设下限、并把 dec 钳回 [-90, 90],
    保证不会产生 NaN/inf 污染后续的单位向量。
    """
    ra = np.asarray(ra_deg, dtype=np.float64)
    dec = np.asarray(dec_deg, dtype=np.float64)
    if dt_years == 0.0:
        return ra.copy(), dec.copy()
    d_dec = np.asarray(pmde, dtype=np.float64) * dt_years / PM_SCALE
    cosd = np.maximum(np.cos(np.radians(dec)), 1e-6)
    d_ra = np.asarray(pmra, dtype=np.float64) * dt_years / PM_SCALE / cosd
    return np.mod(ra + d_ra, 360.0), np.clip(dec + d_dec, -90.0, 90.0)


def jyear_from_unix(unix_ts: float) -> float:
    """unix 秒 → 儒略年(2000.0 = J2000.0)。与 astro.gmst_deg 的儒略日一致。"""
    jd = unix_ts / 86400.0 + 2440587.5
    return 2000.0 + (jd - 2451545.0) / 365.25


# ---------------------------------------------------------------- 写文件

def write_catalog(path, ra_deg, dec_deg, vmag, pmra, pmde, *,
                  source: str = "Tycho-2",
                  epoch: float = J2000_EPOCH,
                  built_unix: float | None = None,
                  presorted: bool = False) -> CatalogHeader:
    """量化 + **按 dec 升序排序** + 原子落盘。返回写出的文件头。

    物理量入口;已经量化好的记录用 :func:`write_records`。
    """
    rec = encode_records(ra_deg, dec_deg, vmag, pmra, pmde)
    return write_records(path, rec, source=source, epoch=epoch,
                         built_unix=built_unix, presorted=presorted)


def write_records(path, rec: np.ndarray, *,
                  source: str = "Tycho-2",
                  epoch: float = J2000_EPOCH,
                  built_unix: float | None = None,
                  presorted: bool = False) -> CatalogHeader:
    """写已量化的 :data:`RECORD_DTYPE` 记录(构建脚本分片累积后调用)。

    排序是锥形查询的前提(文件头 :data:`FLAG_SORTED_DEC` 承诺)。
    ``presorted=True`` 时跳过排序,仅在调用方已保证有序时使用 —— 会做一次校验。
    """
    dest = Path(path)
    if rec.dtype != RECORD_DTYPE:
        raise CatalogError(_("write_records: 记录 dtype 不是 RECORD_DTYPE"))
    if presorted:
        if rec.size and not bool(np.all(np.diff(rec["dec"]) >= 0)):
            raise CatalogError(_("presorted=True 但记录并非按 dec 升序"))
    else:
        rec = rec[np.argsort(rec["dec"], kind="stable")]

    hdr = CatalogHeader(
        version=FORMAT_VERSION, record_bytes=RECORD_BYTES, count=int(rec.size),
        epoch=float(epoch),
        built_unix=float(time.time() if built_unix is None else built_unix),
        flags=FLAG_SORTED_DEC, source=source)

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    try:
        with open(tmp, "wb") as fh:
            fh.write(pack_header(hdr))
            fh.write(rec.tobytes())
        os.replace(tmp, dest)
    except BaseException:
        _unlink_quiet(tmp)      # 失败不留半截文件(§7.5 的教训)
        raise
    return hdr


def _unlink_quiet(p: Path) -> None:
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass


def read_catalog_header(path) -> CatalogHeader:
    """只读文件头(不加载记录区)。"""
    try:
        with open(path, "rb") as fh:
            data = fh.read(HEADER_BYTES)
    except OSError as ex:
        raise CatalogError(_("无法读取星表文件: {ex}").format(ex=ex)) from ex
    return unpack_header(data)


def validate_catalog_file(path, *, expect_count: int | None = None,
                          expect_sha256: str | None = None) -> CatalogHeader:
    """完整性校验:magic/版本/记录长/文件长度/条目数/sha256。

    **下载器绝不能只看 HTTP 200** —— 星表服务端静默截断是实测发生过的
    (ESA TAP 硬上限 300 万行、VizieR 大查询从字段中间断掉、CDN 对不存在
    的路径也回 200)。这里任一项不符都抛 :class:`CatalogError`。
    """
    p = Path(path)
    hdr = read_catalog_header(p)
    try:
        size = p.stat().st_size
    except OSError as ex:
        raise CatalogError(_("无法读取星表文件: {ex}").format(ex=ex)) from ex
    if size != hdr.file_bytes:
        raise CatalogError(
            _("星表文件长度不符: 文件头声明 {count} 条(共 {file_bytes} 字节), 实际 {size} 字节 — 疑似下载被截断").format(
                count=hdr.count, file_bytes=hdr.file_bytes, size=size))
    if not hdr.sorted_by_dec:
        raise CatalogError(_("星表未按 dec 排序(文件头 flag 缺失),锥形查询无法工作"))
    if expect_count is not None and hdr.count != expect_count:
        raise CatalogError(
            _("星表条目数不符: 期望 {expect_count}, 实际 {count}").format(
                expect_count=expect_count, count=hdr.count))
    if expect_sha256:
        got = _sha256_file(p)
        if got.lower() != expect_sha256.lower():
            raise CatalogError(
                _("星表 sha256 不符: 期望 {expect_sha256}, 实际 {got}").format(
                    expect_sha256=expect_sha256, got=got))
    return hdr


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- 查询

class Catalog:
    """已打包星表的只读视图 + 锥形查询。

    ``ra_micro`` / ``dec_micro`` / ``vmag_milli`` / ``pmra`` / ``pmde`` 是
    **零拷贝视图**(pmra/pmde 单位已是 mas/yr);``ra`` / ``dec`` / ``vmag``
    是按需物化并缓存的 float64 数组(2.5M 星各约 20 MB,用到才算)。

    线程安全:构造后只读,多线程共享安全;惰性缓存用锁保护,避免重复计算。
    """

    def __init__(self, header: CatalogHeader, records: np.ndarray,
                 path: Path | None = None):
        if records.dtype != RECORD_DTYPE:
            raise CatalogError(_("记录区 dtype 不是 RECORD_DTYPE"))
        if records.size != header.count:
            raise CatalogError(
                _("记录数与文件头不符: {size} vs {count}").format(
                    size=records.size, count=header.count))
        self.header = header
        self.records = records
        self.path = path
        # RLock 而非 Lock:惰性属性之间将来若互相引用(如 xyz_t 改用 self.ra)
        # 用 Lock 会**静默死锁**;RLock 让这类改动只是多算一次而不是挂死
        self._lock = threading.RLock()
        self._ra: np.ndarray | None = None
        self._dec: np.ndarray | None = None
        self._vmag: np.ndarray | None = None
        self._xyz_t: np.ndarray | None = None
        self._dec_key: np.ndarray | None = None
        self._max_pm: float | None = None

    # -------------------------------------------------- 构造 / 生命周期

    @classmethod
    def open(cls, path=None, *, mmap: bool = False) -> Catalog:
        """打开打包文件。

        默认**整份读入内存**(35.6 MB) —— Windows 上 memmap 会占住文件句柄,
        重新下载时 ``os.replace`` 会撞 WinError 32。确有大内存压力时传
        ``mmap=True``,记得用完 :meth:`close`。
        """
        p = Path(path) if path is not None else catalog_path()
        hdr = validate_catalog_file(p)
        if mmap:
            rec = np.memmap(p, dtype=RECORD_DTYPE, mode="r",
                            offset=HEADER_BYTES, shape=(hdr.count,))
        else:
            try:
                with open(p, "rb") as fh:
                    fh.seek(HEADER_BYTES)
                    buf = fh.read(hdr.data_bytes)
            except OSError as ex:
                raise CatalogError(_("无法读取星表记录区: {ex}").format(ex=ex)) from ex
            if len(buf) != hdr.data_bytes:
                raise CatalogError(
                    _("星表记录区截断: 读到 {0}/{data_bytes} 字节").format(
                        len(buf), data_bytes=hdr.data_bytes))
            rec = np.frombuffer(buf, dtype=RECORD_DTYPE, count=hdr.count)
        return cls(hdr, rec, path=p)

    def close(self) -> None:
        """释放 memmap(整份读入模式下是空操作)。"""
        rec = self.records
        if isinstance(rec, np.memmap):
            try:
                rec._mmap.close()       # type: ignore[attr-defined]
            except (AttributeError, ValueError, OSError):
                pass

    def __enter__(self) -> Catalog:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __len__(self) -> int:
        return int(self.records.size)

    def __repr__(self) -> str:
        return (f"<Catalog {self.header.source} n={len(self)} "
                f"epoch={self.header.epoch:.1f}>")

    # -------------------------------------------------- 字段

    @property
    def ra_micro(self) -> np.ndarray:
        return self.records["ra"]

    @property
    def dec_micro(self) -> np.ndarray:
        return self.records["dec"]

    @property
    def vmag_milli(self) -> np.ndarray:
        return self.records["vmag"]

    @property
    def pmra(self) -> np.ndarray:
        """自行 pmRA*cos(dec),mas/yr(int16 零拷贝视图)。"""
        return self.records["pmra"]

    @property
    def pmde(self) -> np.ndarray:
        """自行 pmDE,mas/yr(int16 零拷贝视图)。"""
        return self.records["pmde"]

    @property
    def ra(self) -> np.ndarray:
        with self._lock:
            if self._ra is None:
                self._ra = decode_ra(self.records["ra"])
            return self._ra

    @property
    def dec(self) -> np.ndarray:
        with self._lock:
            if self._dec is None:
                self._dec = decode_dec(self.records["dec"])
            return self._dec

    @property
    def vmag(self) -> np.ndarray:
        with self._lock:
            if self._vmag is None:
                self._vmag = decode_vmag(self.records["vmag"])
            return self._vmag

    @property
    def xyz_t(self) -> np.ndarray:
        """**(3, N)** float32 单位向量(行 = x/y/z),惰性构建并缓存(约 30 MB)。

        刻意转置成 SoA:``xyz_t[0, i0:i1]`` 是**连续**内存,而 (N,3) 布局下
        ``xyz[i0:i1, 0]`` 步长为 12 字节 —— 实测点积慢 2.3 倍。
        分块直接写最终数组，避免整表 float64 角度、(N,3) 向量与转置副本同时
        存活；254 万条真表的临时内存由约 170 MB 降到十余 MB。
        """
        with self._lock:
            if self._xyz_t is None:
                n = self.records.size
                out = np.empty((3, n), dtype=np.float32)
                scale = math.pi / (180.0 * MICRO)
                step = 1 << 18
                for i0 in range(0, n, step):
                    i1 = min(n, i0 + step)
                    ra = self.records["ra"][i0:i1].astype(np.float64)
                    dec = self.records["dec"][i0:i1].astype(np.float64)
                    ra *= scale
                    dec *= scale
                    cos_dec = np.cos(dec)
                    out[0, i0:i1] = cos_dec * np.cos(ra)
                    out[1, i0:i1] = cos_dec * np.sin(ra)
                    out[2, i0:i1] = np.sin(dec)
                self._xyz_t = out
            return self._xyz_t

    @property
    def dec_key(self) -> np.ndarray:
        """dec 微度的**连续** int32 副本(约 10 MB),供 searchsorted 使用。

        ``records["dec"]`` 是步长 14 的视图,``np.searchsorted`` 会先把整列
        复制成连续数组 —— 每次查询多花 ~1.8 ms。这里一次性缓存。
        """
        with self._lock:
            if self._dec_key is None:
                self._dec_key = np.ascontiguousarray(self.records["dec"])
            return self._dec_key

    @property
    def max_pm_mas(self) -> float:
        """全表自行分量绝对值的最大值(mas/yr),用于自行外推时放宽搜索带。"""
        with self._lock:
            if self._max_pm is None:
                if self.records.size == 0:
                    self._max_pm = 0.0
                else:
                    self._max_pm = float(max(
                        np.abs(self.records["pmra"].astype(np.int32)).max(),
                        np.abs(self.records["pmde"].astype(np.int32)).max()))
            return self._max_pm

    def verify_sorted(self) -> bool:
        """复核"按 dec 升序"这条不变量(全表比较,2.5M 条约几毫秒)。"""
        if self.records.size < 2:
            return True
        return bool(np.all(np.diff(self.records["dec"].astype(np.int64)) >= 0))

    # -------------------------------------------------- 位置

    def positions_at(self, epoch: float | None = None,
                     idx=None) -> tuple[np.ndarray, np.ndarray]:
        """取(可选子集的)位置,``epoch`` 给定时做自行外推。返回 (ra, dec) 度。"""
        if idx is None:
            ra = self.ra
            dec = self.dec
            pmra = self.records["pmra"]
            pmde = self.records["pmde"]
        else:
            idx = np.asarray(idx, dtype=np.int64)
            ra = decode_ra(self.records["ra"][idx])
            dec = decode_dec(self.records["dec"][idx])
            pmra = self.records["pmra"][idx]
            pmde = self.records["pmde"][idx]
        if epoch is None:
            return np.asarray(ra, dtype=np.float64).copy(), \
                np.asarray(dec, dtype=np.float64).copy()
        return apply_proper_motion(ra, dec, pmra, pmde,
                                   float(epoch) - self.header.epoch)

    # -------------------------------------------------- 锥形查询

    def dec_band(self, dec_lo: float, dec_hi: float) -> tuple[int, int]:
        """按 dec 升序不变量取 [dec_lo, dec_hi] 对应的记录区间 [i0, i1)。

        微度边界向外取整(floor/ceil),保证**不会漏掉**边界上的星。

        两个性能陷阱都在这里(实测,勿"简化"回去):

        - 搜索键**必须先钳位再转成 int32**。``np.searchsorted(int32数组,
          Python int)`` 会把**整张 2.5M 表提升成 int64** 再查 —— 实测
          3.6 ms,而 dtype 对齐的标量只要 **1 µs**(慢 3600 倍)。
        - 被搜索的数组用 :attr:`dec_key`(连续副本),不是 records 的步长视图。

        钳位到 ±90.000001° 不改变结果:所有记录的 dec 都在此区间内。
        """
        lim = int(90.0 * MICRO) + 1
        lo = max(-lim, min(lim, int(math.floor(dec_lo * MICRO))))
        hi = max(-lim, min(lim, int(math.ceil(dec_hi * MICRO))))
        d = self.dec_key
        i0 = int(np.searchsorted(d, np.int32(lo), side="left"))
        i1 = int(np.searchsorted(d, np.int32(hi), side="right"))
        return i0, max(i0, i1)

    def cone(self, ra_deg: float, dec_deg: float, radius_deg: float, *,
             max_mag: float | None = None,
             epoch: float | None = None,
             limit: int | None = None) -> np.ndarray:
        """锥形查询 → **升序的 int64 索引数组**(``limit`` 时按星等升序)。

        三级流水:

        1. ``np.searchsorted`` 在 dec 升序数组上取带(自行外推时按全表最大自行
           把带放宽,保证不漏);
        2. 带内用 **float32 单位向量点积**粗筛(阈值放宽 :data:`_EDGE_EPS_DEG`,
           吸收 float32 在 cos(θ) 上的 ~1e-7 误差);
        3. 候选集(通常几千条)用 **float64 haversine 角距**精确复筛
           —— 结果与暴力全表比对逐条一致。

        :param max_mag: 只保留 V ≤ max_mag 的星(None 不限)。
        :param epoch: 儒略年;给定时先做自行外推再判距离。
        :param limit: 只保留最亮的 N 颗(视场面积自适应取候选数用)。
        """
        n = self.records.size
        empty = np.empty(0, dtype=np.int64)
        if n == 0 or radius_deg < 0:
            return empty
        radius_deg = float(radius_deg)
        dt = 0.0 if epoch is None else float(epoch) - self.header.epoch
        # 自行在 dt 年内的最大位移(度);两个分量都取最大值再乘 √2 是保守上界
        pm_pad = (math.sqrt(2.0) * self.max_pm_mas * abs(dt) / PM_SCALE
                  if dt else 0.0)

        if radius_deg + pm_pad >= 180.0:
            cand = np.arange(n, dtype=np.int64)
        else:
            pad = radius_deg + pm_pad + _EDGE_EPS_DEG
            i0, i1 = self.dec_band(dec_deg - pad, dec_deg + pad)
            if i0 >= i1:
                return empty
            cx, cy, cz = unit_vectors(ra_deg, dec_deg, dtype=np.float64)
            xt = self.xyz_t
            dot = (xt[0, i0:i1] * np.float32(cx)
                   + xt[1, i0:i1] * np.float32(cy)
                   + xt[2, i0:i1] * np.float32(cz))
            # 阈值在 cos 域上放宽 _COS_SLACK 再降到 float32 —— 粗筛只负责
            # "绝不漏",多收进来的由下面的 float64 精筛剔除
            thr = math.cos(math.radians(min(180.0, pad))) - _COS_SLACK
            cand = i0 + np.flatnonzero(dot >= np.float32(thr)).astype(np.int64)
            if cand.size == 0:
                return empty

        # 精确复筛(float64);自行外推后位置才是判据
        ra_c, dec_c = self.positions_at(epoch, cand)
        sep = angular_separation(ra_deg, dec_deg, ra_c, dec_c)
        keep = sep <= radius_deg
        if max_mag is not None:
            keep &= decode_vmag(self.records["vmag"][cand]) <= float(max_mag)
        out = cand[keep]
        if limit is not None and out.size > int(limit):
            k = int(limit)
            if k <= 0:
                return empty
            mags = self.records["vmag"][out]
            pick = np.argpartition(mags, k - 1)[:k]
            out = np.sort(out[pick])
        return out


# ---------------------------------------------------------------- 下载器

#: 打包文件名(含格式版本 —— 版本升级时不会与旧缓存混淆)
CATALOG_FILENAME = f"tycho2_v{FORMAT_VERSION}.bin"

#: 从 CDS 现建时要下的原始分片总量(估计值,用来把"第几个分片"折算成字节)。
#:
#: `download_parts` 只报"第 i 个 / 共 n 个",而 `ensure_catalog` 的
#: `progress(done, total)` 被前端当**字节**渲染成 MB —— 中间得有个尺子。
#: 这里宁可给个稳定的估计值,也不去逐个 HEAD 问大小(20 次往返换一个
#: 进度条的分母,不值)。
UPSTREAM_BYTES = 159 * 1024 * 1024

#: 打包镜像地址。**留空是正常状态,不是待办**。
#:
#: 空的时候 :func:`ensure_catalog` 走 :func:`_build_from_upstream` ——
#: 从 CDS I/259(Tycho-2 的权威发布方)取原始分片在本机构建。那条路没有
#: 再分发的许可顾虑,数据也最新;镜像只是"少下 123 MB"的优化。
#:
#: 曾经这里为空会让 :func:`ensure_catalog` **直接抛错**,于是 GUI 上的
#: 「下载星表」是一条死路 —— 用户拿不到星表就永远用不了板解算,而这跟解算
#: 算法毫无关系,纯粹是分发资产没发布造成的接线缺口。
#: 要挂镜像时:URL、sha256、条目数**三者必须一起填**(见下面的 VERIFIED_*),
#: 只填 URL 等于放弃完整性校验。
DEFAULT_CATALOG_URL = ""

#: 2026-07-27 从 CDS I/259 完整构建、并在真机上对过账的 v1
#: 产物校验基准。Release URL 尚未发布，不能把它冒充下载默认值；发布时把
#: 这两个 VERIFIED 值同步填进下面的 DEFAULT_* 即可。
VERIFIED_CATALOG_SHA256 = (
    "2ffd83498a03d85f1e01684aafdc0158173139d8afafb80e5997fe143f44c06c")
VERIFIED_CATALOG_COUNT = 2_539_913

#: 发布产物的 sha256 与条目数(URL 为空期间保持空，便于自建/测试星表)。
DEFAULT_CATALOG_SHA256 = ""
DEFAULT_CATALOG_COUNT = 0


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


#: 运行时生效值(环境变量优先)。模块级读取一次即可 —— 这些不会在进程内变。
CATALOG_URL = _env("ASTRO_SMB_CATALOG_URL") or DEFAULT_CATALOG_URL
CATALOG_SHA256 = _env("ASTRO_SMB_CATALOG_SHA256") or DEFAULT_CATALOG_SHA256
CATALOG_COUNT = int(_env("ASTRO_SMB_CATALOG_COUNT") or DEFAULT_CATALOG_COUNT or 0)


def catalog_dir() -> Path:
    """星表缓存目录(``ASTRO_SMB_CATALOG_DIR`` 可覆盖,单测用)。"""
    override = _env("ASTRO_SMB_CATALOG_DIR")
    base = Path(override) if override else paths.cache_root() / "catalog"
    base.mkdir(parents=True, exist_ok=True)
    return base


def catalog_path() -> Path:
    """本地打包星表路径。``ASTRO_SMB_CATALOG_PATH`` 直接指定文件时优先。"""
    override = _env("ASTRO_SMB_CATALOG_PATH")
    if override:
        return Path(override)
    return catalog_dir() / CATALOG_FILENAME


def catalog_available() -> bool:
    """本地是否已有一份**校验通过**的星表(不只是"文件存在")。"""
    try:
        validate_catalog_file(catalog_path(),
                              expect_count=CATALOG_COUNT or None)
        return True
    except (CatalogError, OSError):
        return False


def ensure_catalog(progress=None, cancel: threading.Event | None = None,
                   *, url: str | None = None,
                   expect_sha256: str | None = None,
                   expect_count: int | None = None) -> Path:
    """确保本地有一份可用星表,必要时下载。返回文件路径。

    - ``ASTRO_SMB_CATALOG_PATH`` 指向的文件**只校验不下载**(离线/自建旁路);
    - 已有文件先校验,通过就直接返回;
    - 下载走 ``.part`` 原子落盘,**校验通过才 os.replace**;
    - 证书兜底链:urllib(补装 Windows 系统根证书)→ ``curl.exe``(Schannel)。

    ``progress(done, total)`` 在下载过程中被调用(total 可能为 0)。
    """
    dest = catalog_path()
    want_sha = expect_sha256 if expect_sha256 is not None else CATALOG_SHA256
    want_n = expect_count if expect_count is not None else (CATALOG_COUNT or None)

    if dest.is_file():
        try:
            validate_catalog_file(dest, expect_count=want_n,
                                  expect_sha256=want_sha or None)
            return dest
        except CatalogError:
            if _env("ASTRO_SMB_CATALOG_PATH"):
                raise       # 用户显式指定的文件坏了,不该被悄悄删掉重下
            _unlink_quiet(dest)     # 损坏缓存:清掉重下,别让它永远卡住

    if _env("ASTRO_SMB_CATALOG_PATH"):
        raise CatalogError(
            _("ASTRO_SMB_CATALOG_PATH 指向的星表不存在: {dest} — 请用 `python -m astro_smb.catalog_build` 构建").format(
                dest=dest))

    src = (url if url is not None else CATALOG_URL).strip()
    if not src:
        # **没有打包镜像时,直接从上游 CDS 取原始数据自己构建。**
        #
        # 原来这里直接抛错,于是 GUI 上的"下载星表"是一条死路:用户拿不到星表,
        # 也就永远用不了板解算 —— 而这跟解算算法一点关系都没有,纯粹是分发资产
        # 没发布造成的接线缺口。
        #
        # 打包镜像(35.6 MB)只是"省流量"的优化;**CDS I/259 才是权威上游**,
        # 而且没有再分发的许可顾虑。代价是下 159 MB 原始分片 + 几秒构建,
        # 一次性的事。所以这条不是兜底凑合,是正经的默认路径。
        return _build_from_upstream(dest, progress=progress, cancel=cancel,
                                    expect_count=want_n)

    tmp = dest.with_name(dest.name + ".part")
    try:
        try:
            _download_urllib(src, tmp, progress, cancel)
        except ssl.SSLCertVerificationError:
            # uv 独立构建的 Python 在 Windows 不挂系统证书库、OpenSSL 不做 AIA
            # 补链;curl.exe 走 Schannel,证书链完整(与 skymap 同一条兜底链)
            _download_curl(src, tmp, progress, cancel)
        validate_catalog_file(tmp, expect_count=want_n,
                              expect_sha256=want_sha or None)
        os.replace(tmp, dest)
        return dest
    except BaseException:
        _unlink_quiet(tmp)
        raise


def _build_from_upstream(dest: Path, *, progress=None,
                         cancel: threading.Event | None = None,
                         expect_count: int | None = None) -> Path:
    """从 CDS I/259 下原始分片并就地构建打包星表(原子落盘)。

    进度分两段:下载占 0~85%,构建占 85~100% —— 下载是 159 MB 的大头,
    构建实测只要 4 秒左右,按字节数分段比按步骤数更贴合观感。

    临时文件放在**目标同目录**的 ``.build`` 子目录:同盘才能 ``os.replace``
    原子改名(跨盘会退化成复制,失败时可能留下半截文件 —— §7.5 踩过)。
    构建成功后清掉分片(159 MB 不留着占地方);失败也清,不留半成品。
    """
    from astro_smb import catalog_build as CB

    work = dest.parent / ".build"
    work.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")

    # **只许往前走。** 下载段现在有两个来源在报:`dl_bytes` 每 0.25 秒一次
    # 真字节数(细),`dl_progress` 每个分片完成时一次(粗)。两者对同一段
    # 进度的估计不一样,谁后到就听谁的话,进度条会**往回跳** —— 实测:
    # 下了 1 MB(0.5%)之后第一个分片完成,`dl_progress(0, 20)` 报 0,
    # 条子当场缩回起点。用户看到的是"下着下着又从头开始了"。
    #
    # 夹在这里而不是在两个回调里各写一遍:来源以后还可能加,而"不许倒退"
    # 是这条进度**本身**的性质。
    _high = [0]

    def report(done: int) -> None:
        if done <= _high[0]:
            return
        _high[0] = done
        if progress:
            progress(done, UPSTREAM_BYTES)

    def dl_progress(done: int, total: int, name: str = "",
                    cached: bool = False) -> None:
        """`download_parts` 的进度 → `ensure_catalog` 的进度。

        **两处签名对不上过。** `download_parts` 调的是
        ``progress(i+1, len(parts), name, cached)`` —— **四个**参数,
        而这里原来只收两个,于是**第一次回调就 TypeError**:
        `ensure_catalog` 直接抛出去,星表**在任何前端上都下不下来**。
        用户报的"新机器上星表没有自动下载",根子在这里 ——
        Qt 少一个"先问再下"的入口只是让它更难看懂。

        **单位也对不上过。** 调用方给的是"第几个分片 / 共几个",
        而 `ensure_catalog` 的 `progress(done, total)` 被两套前端都当成
        **字节**在渲染(`{done/(1<<20):.0f} MB`)—— 直接透传的话界面上
        写的是"1/20 MB"。所以这里按分片比例折算成字节估计值。
        """
        if cancel is not None and cancel.is_set():
            raise CatalogError(_("已取消"))
        frac = (float(done) / float(total)) if total else 0.0
        report(int(UPSTREAM_BYTES * 0.85 * frac))

    def dl_bytes(done: int) -> None:
        """下载**过程中**的字节数 → `ensure_catalog` 的进度。

        `dl_progress` 一个分片才响一次:20 下、每下跳 8 MB,而且第一下要等
        第一个分片整个下完 —— 在那之前界面上一个数字都没有。这条是每 0.25
        秒一次的真字节数。

        **上限卡在 84%**,把最后那一格留给 `download_parts` 返回之后的那次
        回报。`UPSTREAM_BYTES` 只是估计值,真实总量可能略大;不卡的话进度条
        会先冲到 85% 再退回来。
        """
        if cancel is not None and cancel.is_set():
            raise CatalogError(_("已取消"))
        frac = min(float(done) / float(UPSTREAM_BYTES), 0.99)
        report(int(UPSTREAM_BYTES * 0.84 * frac))

    try:
        parts = CB.download_parts(work, progress=dl_progress,
                                  on_bytes=dl_bytes)
        if cancel is not None and cancel.is_set():
            raise CatalogError(_("已取消"))
        report(int(UPSTREAM_BYTES * 0.85))
        hdr = CB.build(work, tmp)
        if expect_count and hdr.count != expect_count:
            raise CatalogError(
                _("构建出的星表条目数 {count} 与预期 {expect_count} 不符").format(
                    count=hdr.count, expect_count=expect_count))
        validate_catalog_file(tmp, expect_count=expect_count or None)
        os.replace(tmp, dest)
        # 收尾也用同一把尺子(字节),别在最后一下把单位换掉 ——
        # 前端渲染的是 MB,忽然跳成 "0/0 MB" 只会看着像出错了
        report(UPSTREAM_BYTES)
        return dest
    except BaseException:
        _unlink_quiet(tmp)
        raise
    finally:
        # 原始分片是中间产物,159 MB,构建完就没用了
        try:
            for p in work.glob("tyc2.dat*"):
                _unlink_quiet(p)
            work.rmdir()
        except OSError:
            pass


def _ssl_context() -> ssl.SSLContext:
    """从 Windows ROOT/CA 存储补装根证书(纯标准库,不引入 certifi)。"""
    ctx = ssl.create_default_context()
    try:
        for store in ("ROOT", "CA"):
            for cert, enc, _trust in ssl.enum_certificates(store):
                if enc == "x509_asn":
                    try:
                        ctx.load_verify_locations(cadata=cert)
                    except ssl.SSLError:
                        pass
    except (AttributeError, OSError, PermissionError):
        pass
    return ctx


def _download_urllib(url: str, tmp: Path, progress, cancel) -> None:
    req = urllib.request.Request(
        url, headers={"User-Agent": "astro-smb-tool/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=60,
                                    context=_ssl_context()) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            with open(tmp, "wb") as fh:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise InterruptedError(_("星表下载已取消"))
                    chunk = resp.read(1 << 18)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if progress is not None:
                        progress(done, total)
            if total > 0 and done != total:
                # http.client 对提前 EOF 静默短读, 必须自查长度
                raise CatalogError(_("星表下载不完整: {done}/{total} 字节").format(
                    done=done, total=total))
    except urllib.error.URLError as ex:
        if isinstance(ex.reason, ssl.SSLCertVerificationError):
            raise ex.reason from ex
        raise CatalogError(_("星表下载失败: {ex}").format(ex=ex)) from ex


def _download_curl(url: str, tmp: Path, progress, cancel) -> None:
    """用系统 curl 兜底下载，同时保持取消与字节进度可用。"""
    import subprocess
    if cancel is not None and cancel.is_set():
        raise InterruptedError(_("星表下载已取消"))
    try:
        proc = subprocess.Popen(
            paths.curl_argv("-sSL", "--max-time", "900", "-o", str(tmp), url),
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=paths.NO_WINDOW)
    except OSError as ex:
        raise CatalogError(_("curl 下载星表失败: {ex}").format(ex=ex)) from ex
    try:
        last_done = -1
        while proc.poll() is None:
            if cancel is not None and cancel.is_set():
                proc.kill()
                proc.communicate()
                raise InterruptedError(_("星表下载已取消"))
            try:
                done = tmp.stat().st_size
            except OSError:
                done = 0
            if progress is not None and done != last_done:
                progress(done, 0)
                last_done = done
            time.sleep(0.2)
        _stdout, stderr = proc.communicate()
    except BaseException:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
        raise
    if proc.returncode != 0:
        err = (stderr or b"").decode("utf-8", errors="replace").strip()
        raise CatalogError(_("curl 下载星表失败: {0}").format(err or proc.returncode))
    try:
        done = tmp.stat().st_size
    except OSError:
        done = 0
    if progress is not None and done != last_done:
        progress(done, 0)
