"""FITS 影像像素层:解码 / 超像素去马赛克 / 自动拉伸(纯 numpy,不依赖 astropy)。

分成四段,每段都可以单独使用、单独测试:

1. **几何**(:func:`geometry_from_header`)—— 从 :class:`~astro_smb.fitshdr.FitsHeader`
   推出宽高/平面数/位深/BSCALE-BZERO/数据区偏移,以及 **实际 Bayer 相位**。
   相位是这里最容易错的一环:``XBAYROFF``/``YBAYROFF`` 会平移相位,
   而 ``ROWORDER`` 决定的行序翻转 **也会改变相位**(偶数高度时上下两行互换)。
   顺序固定为「先应用 offset,再应用翻转」,由单测钉死。
2. **解码**(:func:`decode_pixels`)—— FITS 是 **big-endian**;BITPIX 16 是**有符号**,
   ASIAIR/ZWO 写 ``BZERO=32768`` 把它当无符号 16 位用,必须还原成 0..65535。
3. **去马赛克**(:func:`debayer_superpixel`)—— 超像素:2×2 CFA → 1 个 RGB 像素。
   无插值伪影、快、内存友好(6248×4176 → 3124×2088)。**在整数域上做**,
   避免 26M 像素先转 float32 吃掉 100MB+。
4. **拉伸**(:func:`stretch`)—— 三选一:PixInsight 口径的 STF/MTF 自动拉伸、
   asinh、朴素百分位。整数输入走 **LUT**(65536 项查表),全分辨率一次约几十毫秒,
   参数滑杆才能做到实时。

坐标与轴序约定:对外一律 ``(H, W)`` / ``(H, W, C)``(numpy 惯例,行在前),
FITS 里的 ``NAXIS1`` 是宽、``NAXIS2`` 是高、``NAXIS3`` 是平面数。
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from astro_smb.fitshdr import FitsHeader
from astro_smb.i18n import gettext as _

__all__ = [
    "FitsImageError", "FitsGeometry", "LinearImage", "ChannelStats",
    "StretchParams", "UnitScale",
    "BAYER_PATTERNS", "SHADOWS_CLIPPING", "TARGET_BACKGROUND",
    "normalize_bayer", "bayer_shift", "bayer_after_vflip", "bayer_after_hflip",
    "bayer_positions", "roworder_needs_flip",
    "geometry_from_header", "decode_pixels", "debayer_superpixel",
    "mtf", "madn", "stf_stats", "apply_stf", "asinh_stretch",
    "percentile_stretch", "compute_stats", "stretch", "transfer_lut",
    "unit_scale_for", "to_unit", "sample_unit",
    "load_linear", "histogram_u16", "histogram_unit",
    "linear_hist_u16", "hist_after_from_lut",
]


class FitsImageError(Exception):
    """FITS 影像无法解码/不受支持(头不完整、位深不认识、数据区截断等)。"""


# ---------------------------------------------------------------- 常量

BAYER_PATTERNS = ("RGGB", "BGGR", "GRBG", "GBRG")

# FITS BITPIX → numpy dtype(**big-endian**,FITS 标准就是网络字节序)
_DTYPES: dict[int, str] = {
    8: ">u1", 16: ">i2", 32: ">i4", 64: ">i8", -32: ">f4", -64: ">f8",
}

# PixInsight ScreenTransferFunction 的默认值(口径照抄,别自创)
SHADOWS_CLIPPING = -2.80        # 阴影裁切,单位 = MADN 的倍数(sigma)
TARGET_BACKGROUND = 0.25        # 目标背景亮度

DEFAULT_SAMPLES = 200_000       # 统计抽样上限(全量中位数在 6.5M 元素上要 0.3s)


# ---------------------------------------------------------------- Bayer 相位

def normalize_bayer(value: str | None) -> str | None:
    """``BAYERPAT`` 卡片值 → 规范化的四字母相位;不是 RGB Bayer 的返回 None。

    真机里见过带尾随空格、小写的写法('rggb  ');CYYM/CYGM 等非 RGB 滤镜阵列
    这里一律不支持(返回 None,走单色路径),不要瞎猜。
    """
    if not value:
        return None
    v = str(value).strip().strip("'\"").strip().upper()
    return v if v in BAYER_PATTERNS else None


def bayer_shift(pattern: str, dx: int, dy: int) -> str:
    """把 CFA 原点平移 (dx, dy) 后的相位。

    相位字符串按 ``[p0 p1 / p2 p3]`` 排布:``p0`` 是像素 (row0, col0) 的颜色。
    平移后的 (r, c) 取原来的 ((r+dy)%2, (c+dx)%2)。
    """
    p = _check_pattern(pattern)
    dx, dy = int(dx) % 2, int(dy) % 2
    out = []
    for r in (0, 1):
        for c in (0, 1):
            out.append(p[((r + dy) % 2) * 2 + ((c + dx) % 2)])
    return "".join(out)


def bayer_after_vflip(pattern: str, height: int) -> str:
    """上下翻转 ``height`` 行之后的实际相位。

    新的第 r 行 = 旧的第 (H-1-r) 行。**H 为偶数**时两行的奇偶性互换,
    等价于 ``bayer_shift(p, 0, 1)``;H 为奇数时奇偶性不变,相位原样保留。
    这一条是「翻转会改变 Bayer 相位」的全部内容,漏掉它会让整幅图红蓝互换。
    """
    return bayer_shift(pattern, 0, 1) if int(height) % 2 == 0 else _check_pattern(pattern)


def bayer_after_hflip(pattern: str, width: int) -> str:
    """左右翻转 ``width`` 列之后的实际相位(与 :func:`bayer_after_vflip` 同理)。"""
    return bayer_shift(pattern, 1, 0) if int(width) % 2 == 0 else _check_pattern(pattern)


def bayer_positions(pattern: str) -> dict[str, tuple[tuple[int, int], ...]]:
    """相位 → {颜色: ((行, 列), ...)},行列都是 2×2 单元内的 0/1 偏移。"""
    p = _check_pattern(pattern)
    out: dict[str, list[tuple[int, int]]] = {"R": [], "G": [], "B": []}
    for i, ch in enumerate(p):
        out[ch].append((i // 2, i % 2))
    return {k: tuple(v) for k, v in out.items()}


def _check_pattern(pattern: str) -> str:
    p = (pattern or "").strip().upper()
    if p not in BAYER_PATTERNS:
        raise FitsImageError(_("不支持的 Bayer 相位: {pattern!r}").format(pattern=pattern))
    return p


def roworder_needs_flip(roworder: str | None) -> bool:
    """``ROWORDER`` 卡片值 → 显示时是否需要上下翻转。

    FITS 标准的行序是 **自底向上**(文件里第一行 = 图像最下面一行),所以
    默认(缺卡)必须翻转才能正着显示;ASIAIR/ZWO 写 ``'TOP-DOWN'`` 时不翻。
    值有各种写法('BOTTOM-UP' / 'bottom_up' / 带引号),一律先规范化;
    **认不出来的值按标准处理(翻转)**,不要当成 TOP-DOWN。
    """
    if not roworder:
        return True
    v = str(roworder).strip().strip("'\"").strip().upper().replace("_", "-").replace(" ", "-")
    return not v.startswith("TOP")


# ---------------------------------------------------------------- 几何

@dataclass(frozen=True)
class FitsGeometry:
    """主 HDU 影像的几何与数值刻度。"""

    width: int
    height: int
    planes: int                 # NAXIS3(缺省 1)
    bitpix: int
    bscale: float
    bzero: float
    data_offset: int            # 数据区起始字节(= 头部 2880 对齐后的长度)
    data_bytes: int             # 数据区字节数(不含尾部补齐)
    flip_vertical: bool         # 由 ROWORDER 决定
    bayer_raw: str | None       # 头里写的相位(规范化后)
    bayer_effective: str | None  # 应用 offset + 翻转之后的实际相位
    bayer_offset: tuple[int, int] = (0, 0)   # (XBAYROFF, YBAYROFF)

    @property
    def is_color_cube(self) -> bool:
        """NAXIS3 ≥ 3:已经是 RGB 立方体,不需要也不能再去马赛克。"""
        return self.planes >= 3

    @property
    def shape(self) -> tuple[int, int]:
        return self.height, self.width

    @property
    def dtype(self) -> str:
        return _DTYPES[self.bitpix]


def _fnum(hdr: FitsHeader, key: str, default: float) -> float:
    try:
        v = hdr.get(key)
        return default if v in (None, "") else float(v)
    except (TypeError, ValueError):
        return default


def geometry_from_header(hdr: FitsHeader) -> FitsGeometry:
    """从 FITS 头推出几何;不完整/不支持一律抛 :class:`FitsImageError`。"""
    if not hdr.complete or hdr.header_bytes <= 0:
        raise FitsImageError(_("FITS 头不完整(没读到 END 卡),无法定位数据区"))
    shape = hdr.naxis
    if len(shape) < 2:
        raise FitsImageError(_("不是二维影像(NAXIS={0})").format(len(shape)))
    if len(shape) > 3:
        raise FitsImageError(_("不支持 {0} 维数据立方体").format(len(shape)))
    bitpix = hdr.bitpix
    if bitpix not in _DTYPES:
        raise FitsImageError(_("不支持的 BITPIX: {bitpix}").format(bitpix=bitpix))
    width, height = int(shape[0]), int(shape[1])
    planes = int(shape[2]) if len(shape) >= 3 else 1
    if width <= 0 or height <= 0 or planes <= 0:
        raise FitsImageError(_("影像尺寸非法: {width}×{height}×{planes}").format(
            width=width, height=height, planes=planes))

    bscale = _fnum(hdr, "BSCALE", 1.0)
    bzero = _fnum(hdr, "BZERO", 0.0)
    flip = roworder_needs_flip(hdr.get("ROWORDER"))

    raw_pat = normalize_bayer(hdr.get("BAYERPAT"))
    xoff = int(_fnum(hdr, "XBAYROFF", 0.0))
    yoff = int(_fnum(hdr, "YBAYROFF", 0.0))
    eff = None
    if raw_pat is not None and planes == 1:
        # 顺序不能反:先按 XBAYROFF/YBAYROFF 平移原点,再算行序翻转的影响
        eff = bayer_shift(raw_pat, xoff, yoff)
        if flip:
            eff = bayer_after_vflip(eff, height)

    return FitsGeometry(
        width=width, height=height, planes=planes, bitpix=bitpix,
        bscale=bscale, bzero=bzero,
        data_offset=int(hdr.header_bytes),
        data_bytes=(abs(bitpix) // 8) * width * height * planes,
        flip_vertical=flip, bayer_raw=raw_pat, bayer_effective=eff,
        bayer_offset=(xoff, yoff),
    )


# ---------------------------------------------------------------- 解码

def decode_pixels(src: str | os.PathLike | bytes | bytearray,
                  geom: FitsGeometry, *, cancel=None) -> np.ndarray:
    """读出主 HDU 像素并还原成物理值。

    ``src`` 可以是文件路径(走 ``np.fromfile``,不整份读进内存)或字节串。
    返回 ``(H, W)``(单平面)或 ``(H, W, planes)``(立方体),已应用:

    * BSCALE / BZERO —— **BITPIX 16 + BZERO 32768 + BSCALE 1** 是 ASIAIR 主路径,
      直接还原成 ``uint16``(0..65535),不进 float,省一半内存;
    * ``ROWORDER`` 决定的上下翻转。

    数据区不足(文件截断)抛 :class:`FitsImageError`。

    **内存**:主路径(BITPIX 16 + BZERO 32768,从**文件**读)全程零整份复制 ——
    字节序就地交换 + view,翻转按行块就地交换。52MB 的像素数组以前要多付
    两份整份复制(实测峰值 +149MB),对 50MB 的 ASIAIR 原图很致命
    (渲染线程还抓着上一张时打开第二张会直接翻倍)。
    """
    if cancel is not None and cancel.is_set():
        raise FitsImageError(_("已取消"))
    count = geom.width * geom.height * geom.planes
    dt = np.dtype(geom.dtype)
    if isinstance(src, (bytes, bytearray, memoryview)):
        need = geom.data_offset + count * dt.itemsize
        if len(src) < need:
            raise FitsImageError(
                _("数据区截断:需要 {need:,} 字节,实际只有 {0:,} 字节").format(len(src), need=need))
        raw = np.frombuffer(src, dtype=dt, count=count, offset=geom.data_offset)
        owned = False       # 缓冲区是调用方的(bytearray 可写!),绝不能就地改
    else:
        path = Path(src)
        raw = np.fromfile(path, dtype=dt, count=count, offset=geom.data_offset)
        if raw.size < count:
            raise FitsImageError(
                _("数据区截断:需要 {count:,} 个采样,实际只读到 {size:,} 个").format(
                    count=count, size=raw.size))
        owned = True
    if cancel is not None and cancel.is_set():
        raise FitsImageError(_("已取消"))

    # _apply_scale 要么返回新分配的数组,要么(仅 owned 时)返回 raw 的 view;
    # 两种情况下结果都只有本函数持有,后续可以放心就地翻转
    arr = _apply_scale(raw, geom, inplace=owned)

    if geom.planes > 1:
        # FITS 轴序是 (NAXIS3, NAXIS2, NAXIS1) = (平面, 行, 列)
        arr = arr.reshape(geom.planes, geom.height, geom.width)
        arr = np.moveaxis(arr, 0, -1)
    else:
        arr = arr.reshape(geom.height, geom.width)

    if geom.flip_vertical:
        # 负步长视图后续切片会很慢(去马赛克要按 2 步跨行),这里落成连续内存
        arr = _flip_vertical(arr)
    return arr


def _flip_vertical(arr: np.ndarray) -> np.ndarray:
    """上下翻转并保证结果连续。

    二维连续可写数组走**按行块就地交换**(临时只有 64 行,约 800KB),
    否则回落 ``np.ascontiguousarray(arr[::-1])``(整份复制)。
    """
    if not (arr.ndim == 2 and arr.flags.c_contiguous and arr.flags.writeable):
        return np.ascontiguousarray(arr[::-1])
    h = arr.shape[0]
    step = 64
    for i in range(0, h // 2, step):
        n = min(step, h // 2 - i)
        tmp = arr[h - i - n:h - i][::-1].copy()      # 下块倒序,先存起来
        arr[h - i - n:h - i] = arr[i:i + n][::-1]    # 上块倒序写到下块
        arr[i:i + n] = tmp
    return arr


def _apply_scale(raw: np.ndarray, geom: FitsGeometry, *,
                 inplace: bool = False) -> np.ndarray:
    """应用 BSCALE/BZERO,尽量留在整数域。

    ``inplace=True`` 表示 ``raw`` 的缓冲区归调用方独占(``np.fromfile`` 读出来的),
    主路径可以就地交换字节序再 view 成 uint16,省掉一整份 52MB 的复制。
    """
    bs, bz = geom.bscale, geom.bzero
    if geom.bitpix == 16 and bs == 1.0 and bz == 32768.0:
        # 有符号 int16 + 32768 → uint16:XOR 0x8000 就是模 2^16 的 +32768,
        # 无需 int32 中转(26M 像素能省 100MB 临时内存)。
        # byteswap(inplace) + view(uint16) 与 astype(uint16) **逐元素等价**
        # ([-32768,-1,0,1,32767] → [0,32767,32768,32769,65535],单测钉死),
        # 但完全不分配新数组。
        if inplace and raw.flags.writeable:
            out = raw.byteswap(inplace=True).view(np.uint16)
        else:
            out = raw.astype(np.uint16)
        out ^= np.uint16(0x8000)
        return out
    if geom.bitpix == 8 and bs == 1.0 and bz == 0.0:
        return raw.astype(np.uint8)
    if geom.bitpix == 32 and bs == 1.0 and bz == 2147483648.0:
        out = raw.astype(np.uint32)
        out ^= np.uint32(0x80000000)
        return out
    out = raw.astype(np.float32)
    if bs != 1.0:
        out *= np.float32(bs)
    if bz != 0.0:
        out += np.float32(bz)
    return out


# ---------------------------------------------------------------- 去马赛克

def debayer_superpixel(arr: np.ndarray, pattern: str) -> np.ndarray:
    """超像素去马赛克:每个 2×2 CFA 单元 → 1 个 RGB 像素。

    以 RGGB 为例::

        R = a[0::2, 0::2]
        G = (a[0::2, 1::2] + a[1::2, 0::2]) / 2
        B = a[1::2, 1::2]

    输出 ``(H//2, W//2, 3)``,**dtype 与输入一致**(整数进整数出)。
    奇数尺寸自动裁掉最后一行/列。两个绿位求平均时整数要**加宽**再除,
    否则两个 65535 相加会溢出成 65534//2(真机会看到绿通道整体压暗)。
    加宽的宽度必须看输入位宽:uint16 用 uint32 够,但 **uint32 输入
    (BITPIX 32 + BZERO 2147483648)再用 uint32 就会回绕**
    (两个 4000000000 平均出 1852516352),必须上 uint64。
    """
    a = np.asarray(arr)
    if a.ndim != 2:
        raise FitsImageError(_("超像素去马赛克需要二维 CFA 阵列,收到 {ndim} 维").format(ndim=a.ndim))
    pos = bayer_positions(pattern)
    h, w = a.shape[0] // 2 * 2, a.shape[1] // 2 * 2
    if h < 2 or w < 2:
        raise FitsImageError(_("影像太小,无法去马赛克: {shape}").format(shape=a.shape))
    a = a[:h, :w]

    def plane(rc: tuple[int, int]) -> np.ndarray:
        r, c = rc
        return a[r::2, c::2]

    r_plane = plane(pos["R"][0])
    b_plane = plane(pos["B"][0])
    g1, g2 = (plane(p) for p in pos["G"])
    if np.issubdtype(a.dtype, np.integer):
        if np.issubdtype(a.dtype, np.signedinteger):
            acc = np.dtype(np.int64)
        else:
            acc = np.dtype(np.uint32) if a.dtype.itemsize <= 2 else np.dtype(np.uint64)
        g_plane = ((g1.astype(acc) + g2.astype(acc)) // 2).astype(a.dtype)
    else:
        g_plane = ((g1.astype(np.float32) + g2.astype(np.float32)) * 0.5).astype(a.dtype)

    out = np.empty((h // 2, w // 2, 3), dtype=a.dtype)
    out[:, :, 0] = r_plane
    out[:, :, 1] = g_plane
    out[:, :, 2] = b_plane
    return out


# ---------------------------------------------------------------- 归一化

@dataclass(frozen=True)
class UnitScale:
    """物理值 → [0, 1] 的仿射变换:``unit = (x - offset) * scale``。

    整数影像用固定量程(uint16 → 1/65535),这样同一张图在不同抽样/不同
    分辨率下的统计口径完全一致;浮点影像才按实际范围拉平。
    """

    offset: float = 0.0
    scale: float = 1.0

    @property
    def is_u16(self) -> bool:
        return self.offset == 0.0 and abs(self.scale - 1.0 / 65535.0) < 1e-12


def unit_scale_for(arr: np.ndarray) -> UnitScale:
    a = np.asarray(arr)
    if a.dtype == np.uint8:
        return UnitScale(0.0, 1.0 / 255.0)
    if a.dtype == np.uint16:
        return UnitScale(0.0, 1.0 / 65535.0)
    if a.dtype == np.uint32:
        return UnitScale(0.0, 1.0 / 4294967295.0)
    flat = a.reshape(-1)
    if flat.size > DEFAULT_SAMPLES:
        flat = flat[::max(1, flat.size // DEFAULT_SAMPLES)]
    flat = flat[np.isfinite(flat)]
    if flat.size == 0:
        return UnitScale(0.0, 1.0)
    lo, hi = float(flat.min()), float(flat.max())
    if lo >= -1e-6 and hi <= 1.0 + 1e-6:
        return UnitScale(0.0, 1.0)          # 已经是 0..1
    if hi <= lo:
        return UnitScale(lo, 1.0)
    return UnitScale(lo, 1.0 / (hi - lo))


def to_unit(arr: np.ndarray, us: UnitScale | None = None) -> np.ndarray:
    """转成 float32 的 [0, 1] 数组(会复制一份,注意内存)。

    **NaN / ±Inf 会被抹平**(NaN→0、+Inf→1、-Inf→0):浮点 FITS
    (BITPIX -32/-64,Siril/DSS/定标叠加的输出)带 NaN 很常见,而
    ``np.clip`` 保留 NaN、后续 ``(nan*bins).astype(int32)`` 得 INT_MIN,
    ``np.bincount`` 直接抛「must have no negative elements」—— 整张图打不开。
    整数输入不可能有 NaN,跳过这步保住主路径的速度。
    """
    a = np.asarray(arr)
    us = us or unit_scale_for(a)
    out = a.astype(np.float32)
    if us.offset != 0.0:
        out -= np.float32(us.offset)
    if us.scale != 1.0:
        out *= np.float32(us.scale)
    if np.issubdtype(a.dtype, np.inexact):
        np.nan_to_num(out, copy=False, nan=0.0, posinf=1.0, neginf=0.0)
    np.clip(out, 0.0, 1.0, out=out)
    return out


def sample_unit(arr: np.ndarray, us: UnitScale | None = None,
                max_samples: int = DEFAULT_SAMPLES) -> np.ndarray:
    """抽样成 ``(N, C)`` 的 float32 归一化样本,给统计用(确定性等距抽样)。

    不用随机抽样:同一张图两次打开必须给出**逐位相同**的拉伸,否则用户会
    看到"参数没动画面却变了"。
    """
    a = np.asarray(arr)
    if a.ndim == 2:
        a = a[:, :, None]
    nch = a.shape[-1]
    flat = a.reshape(-1, nch)
    if flat.shape[0] > max_samples:
        flat = flat[::max(1, flat.shape[0] // max_samples)]
    return to_unit(flat, us or unit_scale_for(arr))


# ---------------------------------------------------------------- 拉伸数学

def mtf(m: float, x) -> np.ndarray:
    """PixInsight 的 Midtones Transfer Function::

        MTF(m, x) = ((m - 1)·x) / ((2m - 1)·x - m)

    性质(单测钉死):``MTF(m, 0) = 0``、``MTF(m, 1) = 1``、``MTF(0.5, x) = x``、
    ``MTF(m, 0.5) = 1 - m``,在 [0, 1] 上单调递增。
    ``m`` 落到 0/1 之外时公式退化,这里按极限值处理并保证输出仍在 [0, 1]。
    """
    xa = np.clip(np.asarray(x, dtype=np.float32), 0.0, 1.0)
    m = float(m)
    if not np.isfinite(m):
        return xa
    if m <= 0.0:
        return np.where(xa > 0.0, np.float32(1.0), np.float32(0.0)).astype(np.float32)
    if m >= 1.0:
        return np.where(xa >= 1.0, np.float32(1.0), np.float32(0.0)).astype(np.float32)
    # 在 x ∈ [0,1]、m ∈ (0,1) 上分母恒为负,不会除零
    den = (2.0 * m - 1.0) * xa - m
    out = ((m - 1.0) * xa) / den
    return np.clip(out, 0.0, 1.0).astype(np.float32)


def _finite(x) -> np.ndarray:
    """展平成 float32 一维,并**剔除 NaN/Inf**(只在真有非有限值时才复制)。"""
    a = np.asarray(x, dtype=np.float32).reshape(-1)
    if a.size and not bool(np.isfinite(a).all()):
        a = a[np.isfinite(a)]
    return a


def _moments(x) -> tuple[float, float]:
    """(median, MADN),NaN/Inf 已剔除;空/全非有限返回 (0, 0)。"""
    a = _finite(x)
    if a.size == 0:
        return 0.0, 0.0
    med = float(np.median(a))
    return med, float(1.4826 * np.median(np.abs(a - np.float32(med))))


def madn(x) -> float:
    """归一化中位数绝对偏差 MADN = 1.4826 × median(|x - median(x)|)。

    1.4826 是让 MADN 在正态分布上等于标准差的常数。常量图返回 0。
    NaN/Inf 先剔除(浮点 FITS 里一个 NaN 就会让 median 变成 NaN,
    进而 c0/m2 全废、整张图渲染成黑)。
    """
    return _moments(x)[1]


@dataclass
class ChannelStats:
    """单通道的拉伸统计(全部在归一化 [0,1] 域)。"""

    median: float = 0.0
    madn: float = 0.0
    c0: float = 0.0             # STF 阴影裁切点
    m2: float = 0.5             # STF 中间调
    lo: float = 0.0             # 百分位下限
    hi: float = 1.0             # 百分位上限


def stf_stats(x, *, shadows_clipping: float = SHADOWS_CLIPPING,
              target_background: float = TARGET_BACKGROUND,
              linked: bool = False,
              max_samples: int | None = DEFAULT_SAMPLES) -> list[ChannelStats]:
    """PixInsight 口径的 STF 统计。

    每通道::

        m    = median(x)
        MADN = 1.4826 · median(|x - m|)
        c0   = clamp(m + shadows_clipping · MADN, 0, 1)
        m2   = MTF(target_background, m - c0)

    ``linked=True`` 时三通道用**同一组** c0/m2(保持原始色彩比例),``False`` 时
    各算各的(等价于自动白平衡)。``max_samples=None`` 用全量数据。

    **链接模式的口径**(PixInsight 的 linked STF):**先逐通道**算 median/MADN,
    **再把两个量各自平均**,而不是把三通道像素汇成一池再算。汇池是错的 ——
    池化后的 MADN 量到的是**通道间的背景偏移**而不是噪声:R/G/B 背景
    1200/1800/900 ADU、σ≈40 时,汇池 MADN 比真噪声大了十几倍,c0 被 clamp 到 0
    (等于根本不裁背景)、m2 大一个数量级,画面发灰欠拉伸(真机踩过)。
    """
    a = np.asarray(x, dtype=np.float32)
    if a.ndim == 1:
        a = a[:, None]
    elif a.ndim == 2:
        a = a[:, :, None]
    nch = a.shape[-1]
    flat = a.reshape(-1, nch)
    if max_samples and flat.shape[0] > max_samples:
        flat = flat[::max(1, flat.shape[0] // max_samples)]

    per = [_moments(flat[:, c]) for c in range(nch)]
    if linked:
        med = sum(m for m, _ in per) / nch
        mad = sum(d for _, d in per) / nch
        moments = [(med, mad)] * nch
    else:
        moments = per

    out: list[ChannelStats] = []
    for med, mad in moments:
        c0 = float(np.clip(med + shadows_clipping * mad, 0.0, 1.0))
        m2 = float(mtf(target_background, med - c0))
        out.append(ChannelStats(median=med, madn=mad, c0=c0, m2=m2, lo=c0, hi=1.0))
    return out


def apply_stf(x, cs: ChannelStats) -> np.ndarray:
    """``out = MTF(m2, clamp((x - c0) / (1 - c0), 0, 1))``。"""
    a = np.asarray(x, dtype=np.float32)
    den = 1.0 - cs.c0
    if den <= 1e-9:
        den = 1e-9
    y = np.clip((a - np.float32(cs.c0)) / np.float32(den), 0.0, 1.0)
    return mtf(cs.m2, y)


def asinh_stretch(x, a: float, black: float = 0.0) -> np.ndarray:
    """``y = asinh(a·x') / asinh(a)``,其中 ``x' = (x - black) / (1 - black)``。

    ``black`` 是**黑场**(用 STF 那套 ``median + sc·MADN`` 估出来的 c0)。
    不减黑场的裸 asinh 在真实天文数据上基本没用:天光背景带着几百到几千 ADU
    的基座, asinh 的陡峭段全被基座吃掉,画面发灰、目标不突出(真机实测反馈)。
    先把基座压掉再上曲线,才能和 STF/Siril 的观感对齐。``black=0`` 时退化成
    原来的纯逐点函数。
    """
    xa = np.asarray(x, dtype=np.float32)
    b = float(black)
    if np.isfinite(b) and b > 0.0:
        den = max(1.0 - b, 1e-9)
        xa = (xa - np.float32(b)) / np.float32(den)
    xa = np.clip(xa, 0.0, 1.0)
    a = float(a)
    if not np.isfinite(a) or a <= 0.0:
        return xa
    return (np.arcsinh(a * xa) / np.arcsinh(a)).astype(np.float32)


def percentile_stretch(x, lo_pct: float = 0.2, hi_pct: float = 99.8) -> np.ndarray:
    """朴素百分位线性拉伸(现有预览用的那套,作为第三种模式保留)。"""
    a = np.asarray(x, dtype=np.float32)
    lo_pct, hi_pct = sorted((float(lo_pct), float(hi_pct)))
    lo, hi = (float(v) for v in np.percentile(a, (lo_pct, hi_pct)))
    return _linear_map(a, lo, hi)


def _pcts(x, lo_pct: float, hi_pct: float) -> tuple[float, float]:
    """百分位对(NaN/Inf 已剔除;空样本回落 0~1)。"""
    a = _finite(x)
    if a.size == 0:
        return 0.0, 1.0
    lo, hi = (float(v) for v in np.percentile(a, (lo_pct, hi_pct)))
    return lo, hi


def _linear_map(a: np.ndarray, lo: float, hi: float) -> np.ndarray:
    if not np.isfinite(lo):
        lo = 0.0
    if not np.isfinite(hi) or hi <= lo:
        hi = lo + 1e-6
    return np.clip((a - np.float32(lo)) / np.float32(hi - lo), 0.0, 1.0).astype(np.float32)


# ---------------------------------------------------------------- 拉伸参数

@dataclass(frozen=True)
class StretchParams:
    """一次拉伸的全部可调参数。``mode`` ∈ {stf, asinh, percentile}。"""

    mode: str = "stf"
    shadows_clipping: float = SHADOWS_CLIPPING
    target_background: float = TARGET_BACKGROUND
    linked: bool = False
    asinh_a: float = 100.0
    lo_pct: float = 0.2
    hi_pct: float = 99.8

    def fingerprint(self) -> str:
        """只把**当前模式真正用到的**字段进指纹。

        这样"在 STF 模式下拖 asinh 滑杆"不会让磁盘缓存整片失效
        (也不会触发一次无意义的全图重渲染)。
        """
        if self.mode == "asinh":
            # asinh 现在先减黑场(复用 STF 的 c0), 所以 shadows_clipping 与
            # linked 也会改变结果 —— 不进指纹的话切"通道链接"时缓存键不变,
            # 画面不刷新。
            key = ("asinh", round(float(self.asinh_a), 4),
                   round(float(self.shadows_clipping), 4), bool(self.linked))
        elif self.mode == "percentile":
            key = ("pct", round(float(self.lo_pct), 4), round(float(self.hi_pct), 4),
                   bool(self.linked))
        else:
            key = ("stf", round(float(self.shadows_clipping), 4),
                   round(float(self.target_background), 4), bool(self.linked))
        return hashlib.sha1(repr(key).encode("ascii")).hexdigest()[:12]


def compute_stats(sample: np.ndarray, params: StretchParams) -> list[ChannelStats]:
    """从归一化样本 ``(N, C)`` 一次算齐所有模式要用的统计量。

    STF 的 c0/m2 与百分位的 lo/hi 都很便宜(样本只有 20 万点),一起算好,
    切换模式时不用重扫数据;直方图上的标记线也直接取这里的值。
    """
    a = np.asarray(sample, dtype=np.float32)
    if a.ndim == 1:
        a = a[:, None]
    # (N, C) 抽样矩阵送进 stf_stats 会被当成 (H, W) 单色图(它按 numpy 惯例
    # 把二维当影像),三个通道就被合并成一个 —— 不链接模式会静默失效
    # (真机探针里表现为三通道统计完全相同)。补一根长度 1 的"宽"轴消歧。
    stats = stf_stats(a[:, None, :], shadows_clipping=params.shadows_clipping,
                      target_background=params.target_background,
                      linked=params.linked, max_samples=None)
    lo_pct, hi_pct = sorted((float(params.lo_pct), float(params.hi_pct)))
    if params.linked:
        pairs = [_pcts(a, lo_pct, hi_pct)] * len(stats)
    else:
        pairs = [_pcts(a[:, c], lo_pct, hi_pct) for c in range(len(stats))]
    for cs, (lo, hi) in zip(stats, pairs):
        cs.lo, cs.hi = lo, hi
    return stats


def _transfer(params: StretchParams, cs: ChannelStats):
    """返回作用在归一化数组上的传递函数。"""
    if params.mode == "asinh":
        # 黑场复用 STF 的 c0(compute_stats 已经算好, 不额外扫数据);
        # 链接/不链接开关因此对 asinh 也有意义了(不链接 = 逐通道减各自黑场
        # = 顺带白平衡), 所以 UI 上不该再把该开关灰掉。
        return lambda u: asinh_stretch(u, params.asinh_a, cs.c0)
    if params.mode == "percentile":
        return lambda u: _linear_map(np.asarray(u, dtype=np.float32), cs.lo, cs.hi)
    return lambda u: apply_stf(u, cs)


def transfer_lut(params: StretchParams, cs: ChannelStats,
                 n: int = 65536) -> np.ndarray:
    """把传递函数在 ``n`` 个量化格点上算成 uint8 查表。

    单独暴露出来是为了让调用方能**由 LUT 反推拉伸后的直方图**
    (见 :func:`hist_after_from_lut`)—— 对 stf/asinh/percentile 三种模式都成立,
    因为它们全都是逐点单变量函数。
    """
    if n < 2:
        raise FitsImageError(_("LUT 格点数至少 2,收到 {n}").format(n=n))
    grid = np.arange(n, dtype=np.float32) * np.float32(1.0 / (n - 1))
    fn = _transfer(params, cs)
    return np.clip(fn(grid) * 255.0 + 0.5, 0.0, 255.0).astype(np.uint8)


def stretch(rgb: np.ndarray, params: StretchParams, *,
            unit: UnitScale | None = None,
            stats: list[ChannelStats] | None = None,
            mono_out: bool = False
            ) -> tuple[np.ndarray, list[ChannelStats]]:
    """线性阵列 → 可显示的 8bit 位图。

    ``rgb`` 可以是 ``(H, W)`` / ``(H, W, 1)`` / ``(H, W, 3)``;单通道默认
    广播成灰度 RGB,返回 ``(uint8 (H, W, 3), 三个通道的统计)``。
    ``mono_out=True`` 且输入只有一个通道时**直接返回 ``(H, W)`` 单通道** ——
    单色相机(ASI1600MM/2600MM/6200MM 不写 BAYERPAT)整幅 3 倍的位图
    (6248×4176 时 78MB → 26MB)、3 倍的落盘时间纯属白烧,调用方按
    ``ndim`` 选 PIL 的 ``"L"``/``"RGB"`` 模式即可。

    单通道输入即便不开 ``mono_out``,也**只算一遍 LUT/传递函数**再复制到
    另外两个平面(以前同一条 LUT 要算三遍)。

    **整数输入走 LUT**:先把传递函数在 0..65535(或 0..255)上算一遍,
    再 ``lut[arr]`` 一次查表出结果 —— 3124×2088×3 约 50ms,滑杆才拖得动。
    浮点输入没有有限量化格点,只能整幅算。
    """
    a = np.asarray(rgb)
    if a.ndim == 2:
        a = a[:, :, None]
    if a.ndim != 3 or a.shape[2] not in (1, 3):
        raise FitsImageError(_("不支持的显示阵列形状: {shape}").format(shape=a.shape))
    us = unit or unit_scale_for(a)
    if stats is None:
        stats = compute_stats(sample_unit(a, us), params)
    if len(stats) < a.shape[2]:
        stats = list(stats) + [stats[-1]] * (a.shape[2] - len(stats))

    h, w, nch = a.shape
    lut_n = 0
    if a.dtype == np.uint16 and us.is_u16:
        lut_n = 65536
    elif a.dtype == np.uint8 and abs(us.scale - 1.0 / 255.0) < 1e-12 and us.offset == 0.0:
        lut_n = 256

    def band(src_c: int) -> np.ndarray:
        cs = stats[src_c]
        if lut_n:
            return transfer_lut(params, cs, lut_n)[a[:, :, src_c]]
        u = to_unit(a[:, :, src_c], us)
        return np.clip(_transfer(params, cs)(u) * 255.0 + 0.5,
                       0.0, 255.0).astype(np.uint8)

    stats3 = [stats[min(c, nch - 1)] for c in range(3)]
    if nch == 1:
        gray = band(0)
        if mono_out:
            return gray, stats3
        out = np.empty((h, w, 3), dtype=np.uint8)
        out[:, :, 0] = out[:, :, 1] = out[:, :, 2] = gray
        return out, stats3

    out = np.empty((h, w, 3), dtype=np.uint8)
    for c in range(3):
        out[:, :, c] = band(c)
    return out, stats3


# ---------------------------------------------------------------- 直方图

def histogram_u16(a: np.ndarray, bins: int = 256) -> np.ndarray:
    """uint16 阵列的直方图,``np.bincount`` 一遍扫出来再按等宽分组。

    与 ``np.histogram(a, bins=bins, range=(0, 65536))`` 结果一致,但快一个量级
    (6.5M 元素:bincount 约 5ms,histogram 约 200ms)。``bins`` 必须整除 65536。
    """
    arr = np.asarray(a)
    if arr.dtype != np.uint16:
        arr = arr.astype(np.uint16)
    if bins <= 0 or 65536 % bins:
        raise FitsImageError(_("bins 必须整除 65536,收到 {bins}").format(bins=bins))
    counts = np.bincount(arr.reshape(-1), minlength=65536)
    return counts.reshape(bins, 65536 // bins).sum(axis=1)


def histogram_unit(x: np.ndarray, bins: int = 256) -> np.ndarray:
    """归一化 [0,1] 数组的直方图(量化到 bins 后 bincount)。

    **NaN/Inf 先剔除**:``np.clip`` 保留 NaN,``(nan*bins).astype(int32)``
    得 INT_MIN,``np.bincount`` 会抛「must have no negative elements」——
    浮点 FITS 带一个 NaN 就能让整张图打不开(且错误信息毫无指向性)。
    """
    a = _finite(x)
    if a.size == 0:
        return np.zeros(bins, dtype=np.intp)
    a = np.clip(a, 0.0, 1.0)        # 不能 out=a:_finite 全有限时返回的是入参视图
    idx = np.minimum((a * bins).astype(np.int32), bins - 1)
    return np.bincount(idx, minlength=bins)


def linear_hist_u16(a: np.ndarray, *, chunk_rows: int = 512) -> np.ndarray:
    """uint16 平面的 **65536 格**直方图(分块累加)。

    整幅一次 ``np.bincount`` 会把 uint16 提升成 intp:6.5M 像素 = 52MB 临时,
    26M 像素 = 209MB。按行分块累加把瞬时分配压到十几 MB,结果逐位相同。

    这张表是「拉伸后直方图不再碰全分辨率像素」的基础:每次调参只要
    ``hist_after_from_lut(transfer_lut(...), 这张表)`` 就够(256 项运算)。
    """
    arr = np.asarray(a)
    if arr.ndim == 3 and arr.shape[2] == 1:
        arr = arr[:, :, 0]
    if arr.dtype != np.uint16:
        arr = arr.astype(np.uint16)
    out = np.zeros(65536, dtype=np.int64)
    if arr.ndim != 2:
        return out + np.bincount(arr.reshape(-1), minlength=65536)
    rows = max(1, int(chunk_rows))
    for i in range(0, arr.shape[0], rows):
        block = np.ascontiguousarray(arr[i:i + rows]).reshape(-1)
        out += np.bincount(block, minlength=65536)
    return out


def hist_after_from_lut(lut: np.ndarray, linear_hist: np.ndarray,
                        bins: int = 256) -> np.ndarray:
    """由「线性直方图 + LUT」推出**拉伸后**的直方图,不碰任何全分辨率像素。

    LUT 把每个线性格点映到一个 0..255 输出级,所以输出级的计数就是所有映到它的
    线性格点的计数之和 —— ``bincount(lut, weights=linear_hist)``,256 项运算。
    直接在 uint8 位图上 bincount 要先把非连续的 ``rgb8[:,:,c]`` 复制一份、
    再提升成 intp(单色 26M 像素实测 261ms、峰值 +209MB),而结果**完全一样**。
    """
    l = np.asarray(lut)
    lh = np.asarray(linear_hist, dtype=np.float64)
    if l.shape[0] != lh.shape[0]:
        raise FitsImageError(
            _("LUT 与线性直方图长度不一致: {0} vs {1}").format(l.shape[0], lh.shape[0]))
    return np.bincount(l.reshape(-1), weights=lh,
                       minlength=bins).astype(np.int64)


# ---------------------------------------------------------------- 顶层装配

@dataclass
class LinearImage:
    """一张 FITS 的线性像素数据(拉伸之前),连同统计用的抽样。"""

    geom: FitsGeometry
    raw: np.ndarray                 # 解码后未去马赛克的阵列(像素读数用原始 ADU)
    rgb: np.ndarray                 # 显示用 (H, W, 1|3) 线性阵列
    unit: UnitScale
    sample: np.ndarray              # (N, C) 归一化抽样
    debayered: bool = False
    header: FitsHeader | None = field(default=None, repr=False)

    @property
    def height(self) -> int:
        return int(self.rgb.shape[0])

    @property
    def width(self) -> int:
        return int(self.rgb.shape[1])

    @property
    def channels(self) -> int:
        return int(self.rgb.shape[2])

    @property
    def is_color(self) -> bool:
        return self.channels >= 3

    def raw_at(self, x: int, y: int) -> tuple[int, int]:
        """显示坐标 (x, y) → 原始阵列坐标(超像素时 ×2)。"""
        if self.debayered:
            return x * 2, y * 2
        return x, y


def load_linear(src: str | os.PathLike | bytes | bytearray,
                hdr: FitsHeader, *, cancel=None,
                debayer: bool = True) -> LinearImage:
    """读文件 → 解码 → (可选)去马赛克 → 算统计抽样,一步到位。"""
    geom = geometry_from_header(hdr)
    raw = decode_pixels(src, geom, cancel=cancel)
    if cancel is not None and cancel.is_set():
        raise FitsImageError(_("已取消"))

    debayered = False
    if debayer and geom.bayer_effective and raw.ndim == 2:
        rgb = debayer_superpixel(raw, geom.bayer_effective)
        debayered = True
    elif raw.ndim == 3:
        rgb = raw if raw.shape[2] == 3 else raw[:, :, :1]
    else:
        rgb = raw[:, :, None]

    us = unit_scale_for(rgb)
    sample = sample_unit(rgb, us)
    return LinearImage(geom=geom, raw=raw, rgb=rgb, unit=us, sample=sample,
                       debayered=debayered, header=hdr)
