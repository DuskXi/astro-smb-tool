"""从线性天文影像中提取星点(source extraction),纯 numpy 向量化实现。

本模块只做「像素 → 星表」这一步,不涉及天球坐标。输出坐标是**影像阵列坐标**:
``x`` = 列、``y`` = 行,都是 0-based,**像素中心取整数值**,``y`` 向下增大。
换算到 FITS 存储序(1-based、``ROWORDER`` 决定的行序翻转)是调用方的事,
本模块一个字都不猜。

管线(四段,每段可以单独用、单独测)::

    estimate_background()   分块中值 + 二阶差分 MAD → 背景面 / 噪声面(双线性插值)
    detect_stars()          减背景 → k·σ 阈值 → 连通域 → 矩 → 形状 → 过滤
    filter_stars()          触边 / 饱和 / 过小 / 过扁 / 热像素 / 低信噪
    brightest()             按流量取前 n 颗(解算只要几十颗)

数值全部向量化:逐像素的活儿(阈值、行程提取、矩累加)一次 numpy 调用搞定,
唯一的 Python 循环跑在**行程(run)图**上 —— 一张 6248×4176 的真机帧
上大约一两万条行程,不是几千万个像素。

单位与约定
----------
* ``flux`` / ``peak`` / ``background`` / ``noise`` 全是**原始 ADU**
  (输入是什么单位,输出就是什么单位;本模块不做归一化)。
* ``fwhm`` / ``sigma_major`` / ``sigma_minor`` 单位是**像素**,
  已扣掉像素积分自带的 1/12 方差(见 :data:`PIXEL_VARIANCE`)。
  传了 ``pixel_scale``(角秒/像素)之后 :attr:`StarList.fwhm_arcsec` 直接给角秒。
* ``theta`` 是长轴方位角,**从 +x 轴转向 +y 轴**,单位度,取值 ``[0, 180)``。
  注意 ``y`` 在阵列里向下,所以显示到屏幕上看是顺时针。
* ``ellipticity`` = ``1 - b/a``;``eccentricity`` = ``sqrt(1 - (b/a)²)``。
  两个都给,因为文献里"偏心率"这个词两种都有人用,别再猜了。

**星点的形状统计(FWHM / 偏心率 / 方位角)是独立的证据链**,不是解算的副产品:
同一夜里 FWHM 的时间序列 = 视宁度 + 对焦漂移,偏心率的方向一致性
(:meth:`StarList.stats` 里的 ``theta_r``)= 跟踪/导星误差的直接指纹。
所以这些字段必须齐全、单位必须标注,不能因为"解算用不上"就省掉。

OSC(Bayer 彩色相机)的处理策略
--------------------------------
ASIAIR 上的 ASI2600MC Pro 是 RGGB 的 OSC 相机。**不要在原始 CFA 平面上直接做
局部极大 / 连通域**:3×3 邻域跨越了不同颜色的滤镜,R/G/B 灵敏度差本身就制造
大量伪极大(实测同一帧原始 CFA 出 21471 个"峰",超像素绿平面只有 3728 个)。
要在全分辨率上提星,邻域必须同色(步长 2),而同色采样的空间频率跟超像素
完全一样 —— 拿不回 1.93″/px,白折腾。

正确做法:先 2×2 超像素、在**绿平面**上提星(:func:`green_superpixel`):

* 绿平面是两个 G 位的平均,噪声比单个绿位低 √2(真机实测 MADN 91.9 vs 120.1);
* 两个 G 永远在 2×2 单元的对角上,平均值落在单元**几何中心**;而 R / B 平面
  各自带半个全分辨率像素的对角偏移,拿它们提星会引入系统性半像素偏置;
* 分辨率减半**不会**欠采样:ASI2600MC + 400mm 实测星像 FWHM 在超像素平面上
  仍有 2.8~5.7 px,质心细化的采样充分。

**像素尺度**:超像素平面 = 原始 CFA 的 **2 倍**。ASI2600MC Pro(3.76 µm)+
400mm 实测原始 1.923″/px、超像素 **3.85″/px**。单色相机(ASI2600MM 等)不写
``BAYERPAT``,直接在全分辨率平面上跑,``pixel_scale`` 用原始尺度。

已知取舍
--------
* **不做去混叠(deblending)**:两颗星的等照度轮廓连在一起时算作一个团块。
  密集星场里这会吃掉一部分星,但对板解算(只要几十颗最亮的)和形状统计
  (中位数)都不致命。真要区分,应该在这一层之上做。
* ``flux`` 是**等照度流量**(阈值以上连通像素之和),不是全流量;
  ``flux_aper`` 是细化时固定孔径内的和(含负噪声,零偏无偏)。
* 二阶矩在阈值处被截断,对暗星会**低估** σ。细化路径(``refine=True``)用
  固定圆孔径 + 高斯孔径改正把这个偏差压到 1% 以内;不细化时请自行按
  ``U = ln(peak_sub / (threshold·noise))``、``f(U) = (1-(1+U)e^-U)/(1-e^-U)``、
  ``σ² ← σ²/f(U)`` 改正。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
from astro_smb.i18n import gettext as _

__all__ = [
    "FWHM_PER_SIGMA", "PIXEL_VARIANCE",
    "Background", "StarList",
    "estimate_background", "detect_stars", "filter_stars", "brightest",
    "green_superpixel",
]

#: FWHM = FWHM_PER_SIGMA × σ(高斯)
FWHM_PER_SIGMA = 2.0 * math.sqrt(2.0 * math.log(2.0))   # 2.35482004503...

#: 像素积分本身的方差(方波在一维上的方差 = 1/12)。
#: 探测器测到的是 PSF ⊛ 像素方波,所以二阶矩里天然多出 1/12,
#: 要还原"真实 PSF 的 σ"必须减掉它。σ=1.2px 时这一项占 5.8% 的方差、2.9% 的 σ。
PIXEL_VARIANCE = 1.0 / 12.0

_MAD_TO_SIGMA = 1.4826          # 正态分布下 σ = 1.4826 × MAD

# 2×2 单元里两个绿位的 (行, 列) 偏移
_GREEN_POS = {
    "RGGB": ((0, 1), (1, 0)),
    "BGGR": ((0, 1), (1, 0)),
    "GRBG": ((0, 0), (1, 1)),
    "GBRG": ((0, 0), (1, 1)),
}


# ---------------------------------------------------------------- 通用小工具

def _check_cancel(cancel) -> None:
    if cancel is not None and cancel.is_set():
        raise InterruptedError(_("已取消"))


def _as_plane(img, channel: int | None) -> np.ndarray:
    """把输入规整成二维平面;``(H, W, C)`` 按 ``channel`` 取一层。

    ``channel=None`` 时三通道取 **1(绿)**,其余取 0 —— 绿通道信噪最好,
    而且对 OSC 超像素图来说它就是几何居中的那一层。
    """
    a = np.asarray(img)
    if a.ndim == 3:
        if a.shape[2] == 1:
            a = a[:, :, 0]
        else:
            c = 1 if channel is None and a.shape[2] == 3 else (channel or 0)
            if not 0 <= c < a.shape[2]:
                raise ValueError(_("channel 越界: {c} 不在 [0, {0})").format(a.shape[2], c=c))
            a = a[:, :, c]
    if a.ndim != 2:
        raise ValueError(_("星点提取需要二维平面,收到 {ndim} 维 {shape}").format(
            ndim=a.ndim, shape=a.shape))
    if a.shape[0] < 3 or a.shape[1] < 3:
        raise ValueError(_("影像太小: {shape}").format(shape=a.shape))
    return a


def green_superpixel(raw: np.ndarray, pattern: str) -> np.ndarray:
    """原始 CFA 阵列 → 2×2 超像素**绿平面**(两个绿位取平均)。

    ``pattern`` 是**实际生效**的 Bayer 相位(已经把 ``XBAYROFF``/``YBAYROFF``
    和 ``ROWORDER`` 翻转算进去的那个,``fitsimage.FitsGeometry.bayer_effective``)。
    输出尺寸 ``(H//2, W//2)``,**dtype 与输入一致**(整数进整数出;整数相加
    先加宽再除,不然两个 65535 会回绕成 65534//2)。

    **饱和的注意事项**:一个饱和位 + 一个不饱和位平均之后就掉到满量程以下,
    :func:`detect_stars` 的饱和判定会漏掉这种星。要严格判定请自己在全分辨率
    平面上做 ``raw >= 满量程`` 的掩膜。
    """
    a = np.asarray(raw)
    if a.ndim != 2:
        raise ValueError(_("超像素需要二维 CFA 阵列,收到 {ndim} 维").format(ndim=a.ndim))
    pat = (pattern or "").strip().upper()
    if pat not in _GREEN_POS:
        raise ValueError(_("不支持的 Bayer 相位: {pattern!r}").format(pattern=pattern))
    h, w = a.shape[0] // 2 * 2, a.shape[1] // 2 * 2
    if h < 2 or w < 2:
        raise ValueError(_("影像太小,无法做超像素: {shape}").format(shape=a.shape))
    a = a[:h, :w]
    (r1, c1), (r2, c2) = _GREEN_POS[pat]
    g1, g2 = a[r1::2, c1::2], a[r2::2, c2::2]
    if np.issubdtype(a.dtype, np.integer):
        if np.issubdtype(a.dtype, np.signedinteger):
            acc = np.int64
        else:
            acc = np.uint32 if a.dtype.itemsize <= 2 else np.uint64
        return ((g1.astype(acc) + g2.astype(acc)) // 2).astype(a.dtype)
    return ((g1.astype(np.float64) + g2.astype(np.float64)) * 0.5).astype(a.dtype)


# ---------------------------------------------------------------- 背景

def _axis_weights(centers: np.ndarray, n: int
                  ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """一维双线性插值权重:像素 0..n-1 落在哪两个块心之间、权重多少。

    最外一圈块心**之外**的像素(每边半个块宽)必须**线性外推**,不能钳成端点值。
    真机踩过:一条 4000 ADU / 800 px 的天光梯度上,钳死会让最外半块的背景残差
    达到 ±180 ADU(≈ 15σ),整整一圈刷出 250 个假星 —— 而全图内部只有 1 个。
    外推只跨半个块、误差是二阶的,安全得多。权重仍钳在 ``[-1, 2]`` 兜底,
    免得畸形网格把外推放飞。
    """
    c = np.asarray(centers, dtype=np.float64)
    if c.size == 0:
        raise ValueError(_("背景网格为空"))
    if c.size == 1:
        z = np.zeros(n, dtype=np.int32)
        return z, z, np.zeros(n, dtype=np.float32)
    p = np.arange(n, dtype=np.float64)
    i1 = np.clip(np.searchsorted(c, p, side="left"), 1, c.size - 1).astype(np.int32)
    i0 = (i1 - 1).astype(np.int32)
    w = (p - c[i0]) / (c[i1] - c[i0])
    np.clip(w, -1.0, 2.0, out=w)
    return i0, i1, w.astype(np.float32)


def _lerp_axis(grid: np.ndarray, i0: np.ndarray, i1: np.ndarray,
               w: np.ndarray) -> np.ndarray:
    """沿最后一维把 ``(ny, nx)`` 的粗网格插到 ``(ny, n)``。"""
    g0 = grid[:, i0]
    return (g0 + (grid[:, i1] - g0) * w).astype(np.float32)


@dataclass
class Background:
    """分块估计出来的背景面 / 噪声面(只存粗网格,按需插值)。

    只保留 ``(ny, nx)`` 的粗网格 + 一张沿 x 已插好的 ``(ny, W)`` 中间表,
    **不materialize 全分辨率的背景面**:6248×4176 的 float32 背景面要 104 MB,
    再加一张噪声面就 208 MB,对一个"顺手打开一张图看看"的客户端太贵了。
    要整面就显式调 :meth:`plane` / :meth:`rms_plane`,自己认这个内存。
    """

    back: np.ndarray                    # (ny, nx) float32,各块背景中位数
    rms: np.ndarray                     # (ny, nx) float32,各块噪声 σ
    box: tuple[int, int]                # 实际块尺寸 (bh, bw),像素
    shape: tuple[int, int]              # 原图 (H, W)
    yc: np.ndarray = field(repr=False)  # (ny,) 块心行坐标
    xc: np.ndarray = field(repr=False)  # (nx,) 块心列坐标
    rms_floor: float = 0.0              # 噪声下限(边缘外推可能把 σ 推到负数)

    def __post_init__(self) -> None:
        h, w = int(self.shape[0]), int(self.shape[1])
        self.shape = (h, w)
        self.rms_floor = max(float(self.rms_floor), 0.0)
        self._yi0, self._yi1, self._wy = _axis_weights(self.yc, h)
        xi0, xi1, wx = _axis_weights(self.xc, w)
        self._bx = _lerp_axis(self.back, xi0, xi1, wx)   # (ny, W)
        self._rx = _lerp_axis(self.rms, xi0, xi1, wx)    # (ny, W)

    # ---- 取值 ----

    def rows(self, y0: int, y1: int) -> tuple[np.ndarray, np.ndarray]:
        """第 ``[y0, y1)`` 行的 ``(背景, 噪声)``,各 ``(y1-y0, W)`` float32。"""
        i0 = self._yi0[y0:y1]
        i1 = self._yi1[y0:y1]
        w = self._wy[y0:y1][:, None]
        b0, r0 = self._bx[i0], self._rx[i0]
        rms = r0 + (self._rx[i1] - r0) * w
        np.maximum(rms, np.float32(self.rms_floor), out=rms)
        return b0 + (self._bx[i1] - b0) * w, rms

    def _at(self, table: np.ndarray, ys, xs) -> np.ndarray:
        ys = np.asarray(ys)
        xs = np.asarray(xs)
        i0, i1 = self._yi0[ys], self._yi1[ys]
        w = self._wy[ys]
        v0 = table[i0, xs]
        return v0 + (table[i1, xs] - v0) * w

    def back_at(self, ys, xs) -> np.ndarray:
        """任意整数像素坐标处的背景(``ys``/``xs`` 支持广播)。"""
        return self._at(self._bx, ys, xs)

    def rms_at(self, ys, xs) -> np.ndarray:
        """任意整数像素坐标处的噪声 σ(已应用 :attr:`rms_floor`)。"""
        return np.maximum(self._at(self._rx, ys, xs), np.float32(self.rms_floor))

    def plane(self) -> np.ndarray:
        """完整的 ``(H, W)`` float32 背景面(**会吃 4 字节/像素**)。"""
        return self.rows(0, self.shape[0])[0]

    def rms_plane(self) -> np.ndarray:
        """完整的 ``(H, W)`` float32 噪声面。"""
        return self.rows(0, self.shape[0])[1]

    @property
    def global_back(self) -> float:
        return float(np.median(self.back))

    @property
    def global_rms(self) -> float:
        return float(np.median(self.rms))

    @property
    def grid_shape(self) -> tuple[int, int]:
        return (int(self.back.shape[0]), int(self.back.shape[1]))


def estimate_background(img, box: int = 64, *, channel: int | None = None,
                        rms_method: str = "diff", rms_floor_frac: float = 0.1,
                        cancel=None) -> Background:
    """分块中值背景 + 分块噪声估计,双线性插值成全图的背景/噪声面。

    天光梯度(光污染、月光、渐晕)必须先扣掉,否则同一个绝对阈值会让一边全是
    假星、另一边一颗都出不来 —— 这是"检出率随位置塌陷"最常见的原因。

    ``box`` 是**目标**块边长;实际块尺寸取 ``H // (H // box)`` 这样的整除值,
    把余数摊进各块(而不是留一条细长的边角块),剩下不到 ``ny`` 行居中丢弃,
    这几行的背景由最外一圈块心钳出来。要盖住天光梯度而**不**吃掉星:块边长
    应当远大于星像(≥ 10×FWHM)、又远小于梯度尺度。64 px 在 ASIAIR 超像素平面
    (3124×2088)上大约 48×32 块,实测既跟得上梯度也不会被星拉偏。

    背景一律取块内**中位数**:块里 1~5% 的像素是星,对中位数没有影响;而线性
    梯度的中位数正好等于块心处的值,与后面的双线性插值天然自洽。
    **不做 sigma-clipping 迭代** —— 实测收益抵不上多一遍 26M 像素的 partition。

    ``rms_method``
        ``"diff"``(默认)—— 用水平方向的**二阶差分** ``a[i-1] - 2a[i] + a[i+1]``
        的 MAD 估噪声:``σ = 1.4826·median(|Δ²|)/√6``。这是本函数唯一一处不那么
        "教科书"的地方,但它是必须的:**块内 MAD 量到的是块内的梯度范围,不是噪声**。
        实测一条 4000 ADU / 800 px 的天光梯度(每块内落差 320 ADU、真实噪声 σ=12)
        会让块内 MAD 给出 σ≈136 —— 阈值虚高 11 倍,暗星整片消失。
        二阶差分把**任意线性斜坡精确消掉**(合成对照:斜率 30 ADU/px、真值 σ=15
        时,块内 MAD 给 711、一阶差分给 31.6、二阶差分给 15.0),而且比块内 MAD
        还快(少一次 partition)。中位数保证 5% 的像素是星也不影响。
        ``"mad"`` —— 传统的块内 MAD。只在**噪声像素间相关**时才该用它
        (插值去马赛克、重采样过的图会让差分低估 σ);ASIAIR 的超像素平面
        没有插值,用 ``"diff"``。
    ``rms_floor_frac``
        给噪声面兜个下限(全局中位数的这个比例):某个块整块落在饱和核 / 死区里
        时估出来的 σ 会是 0,阈值 ``k·σ`` 退化成 0,一点数值抖动就能刷出一大片
        "星"。传 0 关掉。

    抛 :class:`ValueError`(输入不是二维 / 太小 / box 或 rms_method 非法),
    ``cancel`` 触发时抛 :class:`InterruptedError`。
    """
    a = _as_plane(img, channel)
    h, w = a.shape
    box = int(box)
    if box < 2:
        raise ValueError(_("box 至少 2 像素,收到 {box}").format(box=box))
    if rms_method not in ("diff", "mad"):
        raise ValueError(_("rms_method 只能是 'diff' 或 'mad',收到 {rms_method!r}").format(
            rms_method=rms_method))

    ny = max(1, h // box)
    nx = max(1, w // box)
    bh, bw = h // ny, w // nx
    oy, ox = (h - ny * bh) // 2, (w - nx * bw) // 2
    yc = oy + (np.arange(ny) + 0.5) * bh - 0.5
    xc = ox + (np.arange(nx) + 0.5) * bw - 0.5

    back = np.empty((ny, nx), dtype=np.float32)
    rms = np.empty((ny, nx), dtype=np.float32)

    use_diff = rms_method == "diff" and bw >= 3
    # 一次最多摊开约 400 万个 float32(16 MB),按"块行"分批,峰值内存与图无关
    per_row = max(1, nx * bh * bw)
    jstep = max(1, 4_000_000 // per_row)
    for j0 in range(0, ny, jstep):
        _check_cancel(cancel)
        j1 = min(ny, j0 + jstep)
        sub = a[oy + j0 * bh: oy + j1 * bh, ox: ox + nx * bw]
        blk = np.asarray(sub, dtype=np.float32).reshape(j1 - j0, bh, nx, bw)
        # (块行, 块列, 行, 列) → (块数, 行, 列);ascontiguousarray 之后是本函数
        # 独占的副本,后面可以放心就地改
        blk = np.ascontiguousarray(blk.transpose(0, 2, 1, 3)).reshape(-1, bh, bw)
        flat = blk.reshape(blk.shape[0], -1)
        med = np.median(flat, axis=1)
        if use_diff:
            # 二阶差分:线性斜坡被精确消掉,噪声方差放大到 6σ²
            d = np.diff(blk, axis=2, n=2)
            np.abs(d, out=d)
            sig = ((_MAD_TO_SIGMA / math.sqrt(6.0))
                   * np.median(d.reshape(d.shape[0], -1), axis=1))
            del d
        else:
            flat -= med[:, None]
            np.abs(flat, out=flat)
            sig = _MAD_TO_SIGMA * np.median(flat, axis=1)
        back[j0:j1] = med.reshape(j1 - j0, nx)
        rms[j0:j1] = sig.reshape(j1 - j0, nx)
        del blk, flat

    np.nan_to_num(back, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
    np.nan_to_num(rms, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
    pos = rms[rms > 0]
    # 先落成 float32 再存:网格是 float32,若下限留在 float64 精度上,
    # rms.min() 会比 rms_floor 小一个 ULP —— 调用方拿它做断言必踩
    if pos.size and rms_floor_frac > 0:
        floor = float(np.float32(
            float(np.median(pos)) * float(rms_floor_frac)))
    elif rms_floor_frac > 0 and float(np.ptp(back)) > 0.0:
        # 有梯度但完全平滑/量化后的图会让所有二阶差分都为 0。仍给一个与
        # 信号尺度相称的数值下限，避免插值舍入误差在 threshold×0 下变成
        # 海量“星点”。真正的常量图没有残差，继续保留 σ=0 的既有语义。
        signal = max(1.0, float(np.max(np.abs(back), initial=0.0)))
        floor = float(np.float32(
            signal * np.finfo(np.float32).eps * 8.0))
    else:
        floor = 0.0
    if floor > 0:
        np.maximum(rms, np.float32(floor), out=rms)
    return Background(back=back, rms=rms, box=(bh, bw), shape=(h, w),
                      yc=yc, xc=xc, rms_floor=floor)


# ---------------------------------------------------------------- 连通域

def _extract_runs(mask: np.ndarray, y0: int, width: int
                  ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """布尔掩膜 → 逐行的行程(run)三元组 ``(row, col_start, col_end)``。

    ``col_end`` 是**开区间**端点。左右各补一列 False 之后展平,行程就绝不会跨行,
    一次 ``diff`` + ``flatnonzero`` 全部找出来 —— 没有 Python 循环。
    """
    m2 = np.zeros((mask.shape[0], width + 2), dtype=bool)
    m2[:, 1:-1] = mask
    flat = m2.reshape(-1).view(np.int8)
    d = np.diff(flat)
    st = np.flatnonzero(d == 1) + 1
    en = np.flatnonzero(d == -1) + 1
    stride = width + 2
    rows = (st // stride).astype(np.int32) + np.int32(y0)
    ca = (st % stride).astype(np.int32) - 1
    cb = (en % stride).astype(np.int32) - 1
    return rows, ca, cb


def _run_pairs(rows: np.ndarray, ca: np.ndarray, cb: np.ndarray, width: int
               ) -> tuple[np.ndarray, np.ndarray]:
    """相邻两行之间 8-邻接的行程对 ``(u, v)``(``u`` 在上,``v`` 在下)。

    行程按 ``(row, col)`` 升序,于是 ``key = row·stride + col`` **全局严格递增**
    (``stride = W + 2`` 保证跨行不会撞上),下一行里的候选就是一段连续下标,
    两次 ``searchsorted`` 就能取到 —— 不用逐行 Python 循环。

    8-邻接判据:行程 ``i = [a_i, b_i)`` 与下一行的 ``j = [a_j, b_j)`` 相接
    ⟺ ``a_j <= b_i`` 且 ``b_j >= a_i``(两端各放宽 1 个像素就是对角相连)。
    """
    n = rows.size
    if n == 0:
        return np.empty(0, dtype=np.intp), np.empty(0, dtype=np.intp)
    stride = np.int64(width + 2)
    r = rows.astype(np.int64)
    key_a = r * stride + ca
    key_b = r * stride + cb
    nxt = (r + 1) * stride
    lo = np.searchsorted(key_b, nxt + ca, side="left")     # 第一个 b_j >= a_i
    hi = np.searchsorted(key_a, nxt + cb, side="right")    # 第一个 a_j > b_i
    cnt = np.maximum(hi - lo, 0)
    total = int(cnt.sum())
    if total == 0:
        return np.empty(0, dtype=np.intp), np.empty(0, dtype=np.intp)
    u = np.repeat(np.arange(n, dtype=np.intp), cnt)
    base = np.repeat(lo, cnt)
    offs = np.arange(total, dtype=np.intp) - np.repeat(np.cumsum(cnt) - cnt, cnt)
    return u, (base + offs).astype(np.intp)


def _label_runs(rows: np.ndarray, ca: np.ndarray, cb: np.ndarray,
                width: int) -> tuple[np.ndarray, int]:
    """行程 → 连通分量标号(0..k-1),8-连通。

    并查集跑在**行程**上而不是像素上:真机 6248×4176 的帧上行程只有一两万条,
    对应上千万个像素 —— 这就是"自己写连通域也能快"的全部秘密。
    路径折半 + 按下标合并,总代价线性。
    """
    n = rows.size
    if n == 0:
        return np.empty(0, dtype=np.intp), 0
    u, v = _run_pairs(rows, ca, cb, width)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for x, y in zip(u.tolist(), v.tolist()):
        rx, ry = find(x), find(y)
        if rx != ry:
            if rx < ry:
                parent[ry] = rx
            else:
                parent[rx] = ry

    root = np.fromiter((find(i) for i in range(n)), dtype=np.intp, count=n)
    _uniq, lab = np.unique(root, return_inverse=True)
    lab = np.asarray(lab, dtype=np.intp).reshape(-1)
    return lab, int(lab.max()) + 1


def _expand_runs(rows: np.ndarray, ca: np.ndarray, cb: np.ndarray,
                 lab: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """行程展开成逐像素的 ``(标号, x, y)``,顺序与 ``mask`` 的行主序完全一致。"""
    lens = (cb - ca).astype(np.intp)
    total = int(lens.sum())
    idx = np.arange(total, dtype=np.intp)
    starts = np.repeat(np.cumsum(lens) - lens, lens)
    px = np.repeat(ca.astype(np.int32), lens) + (idx - starts).astype(np.int32)
    py = np.repeat(rows.astype(np.int32), lens)
    return np.repeat(lab, lens), px, py


# ---------------------------------------------------------------- 星表

_COLUMNS = (
    "x", "y", "flux", "flux_aper", "peak", "peak_sub", "background", "noise",
    "snr", "peak_snr", "npix", "fwhm", "sigma_major", "sigma_minor",
    "theta", "ellipticity", "eccentricity",
    "saturated", "edge", "refined", "xmin", "xmax", "ymin", "ymax",
)


@dataclass
class StarList:
    """一张图上提取到的全部星点,**列存**(每个字段一条 numpy 数组)。

    绝不做成"每颗星一个 Python 对象":一张 ASIAIR 全幅上可能有几千颗,
    逐星对象既慢又吃内存,而下游(板解算、形状统计)要的全是整列运算。
    切片一律走 :meth:`select` / :meth:`brightest`,它们会把所有列一起切。
    """

    x: np.ndarray               # 质心列坐标(0-based,像素中心为整数)
    y: np.ndarray               # 质心行坐标
    flux: np.ndarray            # 等照度流量(阈值以上连通像素的背景扣除和,ADU)
    flux_aper: np.ndarray       # 固定圆孔径内的背景扣除和(ADU;未细化时同 flux)
    peak: np.ndarray            # 团块内最大**原始**像素值(ADU,未扣背景)
    peak_sub: np.ndarray        # 团块内最大扣背景值(ADU)
    background: np.ndarray      # 质心处的局部背景(ADU)
    noise: np.ndarray           # 质心处的局部噪声 σ(ADU)
    snr: np.ndarray             # flux / (noise·√npix)
    peak_snr: np.ndarray        # peak_sub / noise
    npix: np.ndarray            # 团块像素数(int32)
    fwhm: np.ndarray            # 2.3548·√((σx²+σy²)/2),像素
    sigma_major: np.ndarray     # 长轴 σ,像素
    sigma_minor: np.ndarray     # 短轴 σ,像素
    theta: np.ndarray           # 长轴方位角,度,+x 转向 +y,[0, 180)
    ellipticity: np.ndarray     # 1 - b/a
    eccentricity: np.ndarray    # √(1 - (b/a)²)
    saturated: np.ndarray       # 峰值接近满量程(bool)
    edge: np.ndarray            # 团块触及画面边缘(bool)
    refined: np.ndarray         # 孔径细化成功(bool);False 表示回落到等照度矩
    xmin: np.ndarray            # 团块外接框(int32,闭区间)
    xmax: np.ndarray
    ymin: np.ndarray
    ymax: np.ndarray

    shape: tuple[int, int] = (0, 0)         # 源影像 (H, W)
    threshold: float = 0.0                  # 检测阈值(单位 = 局部 σ 的倍数)
    box: tuple[int, int] = (0, 0)           # 背景块尺寸
    pixel_scale: float | None = None        # 角秒/像素(超像素平面记得×2)
    n_blobs: int = 0                        # 过滤之前的团块总数
    rejects: dict[str, int] = field(default_factory=dict)   # 各条过滤规则剔掉几颗

    # ---- 容器协议 ----

    def __len__(self) -> int:
        return int(self.x.size)

    def __bool__(self) -> bool:
        return self.x.size > 0

    def select(self, idx) -> "StarList":
        """按布尔掩膜 / 下标数组切一份新的 :class:`StarList`(所有列一起切)。"""
        kw = {name: np.asarray(getattr(self, name))[idx] for name in _COLUMNS}
        return StarList(shape=self.shape, threshold=self.threshold, box=self.box,
                        pixel_scale=self.pixel_scale, n_blobs=self.n_blobs,
                        rejects=dict(self.rejects), **kw)

    def sorted_by_flux(self) -> "StarList":
        """按等照度流量降序排列。"""
        return self.select(np.argsort(-np.asarray(self.flux), kind="stable"))

    def brightest(self, n: int) -> "StarList":
        """按流量取最亮的 ``n`` 颗(不足就全给,顺序按流量降序)。"""
        n = int(n)
        if n <= 0:
            return self.select(np.zeros(0, dtype=np.intp))
        if n >= len(self):
            return self.sorted_by_flux()
        f = np.asarray(self.flux)
        # argpartition 只做一次 O(N) 的划分,几千颗星里挑几十颗不用整体排序
        cut = np.argpartition(-f, n - 1)[:n]
        return self.select(cut[np.argsort(-f[cut], kind="stable")])

    # ---- 派生 ----

    @property
    def positions(self) -> np.ndarray:
        """``(N, 2)`` 的 ``(x, y)`` 阵列坐标。"""
        return np.stack([np.asarray(self.x), np.asarray(self.y)], axis=1)

    @property
    def fwhm_arcsec(self) -> np.ndarray | None:
        """FWHM 的角秒值;没给 ``pixel_scale`` 时返回 None。"""
        if self.pixel_scale is None:
            return None
        return np.asarray(self.fwhm) * float(self.pixel_scale)

    def stats(self) -> dict[str, float]:
        """整张图的形状/信噪汇总 —— 帧间比较用的就是这一份。

        ``theta_median`` 是**轴向**中位方向(θ 与 θ+180° 等价,所以在 2θ 上取
        环形平均);``theta_r`` 是方向集中度 ∈ [0, 1]:0 = 长轴指向完全随机
        (正常的圆星场),接近 1 = 全场星点朝同一个方向拉长
        —— 那就是跟踪/导星在出问题,而不是视宁度变差。
        """
        n = len(self)
        out: dict[str, float] = {"n": float(n)}
        if n == 0:
            return out
        fw = np.asarray(self.fwhm, dtype=np.float64)
        good = fw[np.isfinite(fw) & (fw > 0)]
        out["fwhm_median"] = float(np.median(good)) if good.size else float("nan")
        out["fwhm_mad"] = (float(_MAD_TO_SIGMA * np.median(np.abs(good - np.median(good))))
                           if good.size else float("nan"))
        if self.pixel_scale is not None and good.size:
            out["fwhm_arcsec_median"] = out["fwhm_median"] * float(self.pixel_scale)
        out["ellipticity_median"] = float(np.median(np.asarray(self.ellipticity)))
        out["eccentricity_median"] = float(np.median(np.asarray(self.eccentricity)))
        out["flux_median"] = float(np.median(np.asarray(self.flux)))
        out["snr_median"] = float(np.median(np.asarray(self.snr)))
        out["background_median"] = float(np.median(np.asarray(self.background)))
        out["noise_median"] = float(np.median(np.asarray(self.noise)))
        t2 = np.radians(2.0 * np.asarray(self.theta, dtype=np.float64))
        cs, sn = float(np.mean(np.cos(t2))), float(np.mean(np.sin(t2)))
        out["theta_median"] = float(math.degrees(0.5 * math.atan2(sn, cs)) % 180.0)
        out["theta_r"] = float(math.hypot(cs, sn))
        return out


def _empty_stars(shape, threshold, box, pixel_scale, n_blobs=0,
                 rejects=None) -> StarList:
    z = np.zeros(0, dtype=np.float32)
    zi = np.zeros(0, dtype=np.int32)
    zb = np.zeros(0, dtype=bool)
    kw: dict[str, np.ndarray] = {}
    for name in _COLUMNS:
        if name in ("npix", "xmin", "xmax", "ymin", "ymax"):
            kw[name] = zi
        elif name in ("saturated", "edge", "refined"):
            kw[name] = zb
        else:
            kw[name] = z
    return StarList(shape=shape, threshold=threshold, box=box,
                    pixel_scale=pixel_scale, n_blobs=n_blobs,
                    rejects=dict(rejects or {}), **kw)


# ---------------------------------------------------------------- 形状

def _shape_from_moments(mxx: np.ndarray, myy: np.ndarray, mxy: np.ndarray):
    """二阶中心矩 → (fwhm, σ_major, σ_minor, θ度, ellipticity, eccentricity)。

    先减掉像素积分自带的 1/12 方差(见 :data:`PIXEL_VARIANCE`),再对协方差矩阵
    求特征值。判别式用 ``hypot`` 算,避免大团块上 ``(mxx-myy)²`` 溢出/失精。
    """
    cxx = np.maximum(mxx - PIXEL_VARIANCE, 0.0)
    cyy = np.maximum(myy - PIXEL_VARIANCE, 0.0)
    cxy = mxy
    half = 0.5 * (cxx + cyy)
    disc = np.hypot(0.5 * (cxx - cyy), cxy)
    lam1 = np.maximum(half + disc, 0.0)
    lam2 = np.maximum(half - disc, 0.0)
    a = np.sqrt(lam1)
    b = np.sqrt(lam2)
    fwhm = FWHM_PER_SIGMA * np.sqrt(np.maximum(half, 0.0))
    theta = np.degrees(0.5 * np.arctan2(2.0 * cxy, cxx - cyy)) % 180.0
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(a > 0, b / np.maximum(a, 1e-12), 1.0)
    ratio = np.clip(np.nan_to_num(ratio, nan=1.0), 0.0, 1.0)
    ell = 1.0 - ratio
    ecc = np.sqrt(np.maximum(1.0 - ratio * ratio, 0.0))
    return (fwhm.astype(np.float32), a.astype(np.float32), b.astype(np.float32),
            theta.astype(np.float32), ell.astype(np.float32), ecc.astype(np.float32))


def _aperture_factor(u: np.ndarray) -> np.ndarray:
    """圆孔径截断对二阶矩的缩水系数 ``f(U) = (1-(1+U)e^-U)/(1-e^-U)``。

    对一个 σ 的高斯、半径 R 的圆孔径,``U = R²/(2σ²)``,则孔径内的
    ``⟨r²⟩ = 2σ²·f(U)``。R = 3.5σ 时 f = 0.987,改正之后 σ 的偏差 < 1%;
    不改正就是 1.3% 的系统性低估,再叠上暗星的阈值截断就藏不住了。
    """
    u = np.maximum(np.asarray(u, dtype=np.float64), 1e-6)
    e = np.exp(-u)
    num = 1.0 - (1.0 + u) * e
    den = 1.0 - e
    f = np.where(den > 1e-12, num / np.maximum(den, 1e-12), 1.0)
    return np.clip(f, 0.05, 1.0)


def _refine_batch(a: np.ndarray, bg: Background, cx, cy, mxx, myy, mxy,
                  scale: float, iters: int, max_radius: float):
    """一批星的孔径细化(见 :func:`_refine_moments`)。"""
    h, w = a.shape
    flux_aper = np.zeros(cx.size, dtype=np.float64)
    ok = np.zeros(cx.size, dtype=bool)
    for _i in range(max(1, int(iters))):
        sig2 = np.maximum(0.5 * (mxx + myy), 0.25)
        rad = np.clip(np.ceil(scale * np.sqrt(sig2)), 2.0, float(max_radius))
        half = int(rad.max())
        off = np.arange(-half, half + 1, dtype=np.int32)
        cy0 = np.rint(cy).astype(np.int32)
        cx0 = np.rint(cx).astype(np.int32)
        # 索引数组保持 (N, ws, 1) / (N, 1, ws),靠广播现算 (N, ws, ws) —— 否则
        # 光是 intp 下标就要两份 (N, ws, ws)×8 字节
        iy = cy0[:, None, None] + off[None, :, None]
        ix = cx0[:, None, None] + off[None, None, :]
        inb = ((iy >= 0) & (iy < h)) & ((ix >= 0) & (ix < w))
        iyc = np.clip(iy, 0, h - 1)
        ixc = np.clip(ix, 0, w - 1)
        val = a[iyc, ixc].astype(np.float32)
        val -= bg.back_at(iyc, ixc).astype(np.float32)
        dy = off[None, :, None].astype(np.float32)
        dx = off[None, None, :].astype(np.float32)
        rr = dy * dy + dx * dx
        keep = inb & (rr <= (rad * rad).astype(np.float32)[:, None, None])
        wgt = np.where(keep, val, np.float32(0.0))
        del val, keep, inb, iy, ix, iyc, ixc

        ax = (1, 2)
        sw = wgt.sum(axis=ax, dtype=np.float64)
        good = sw > 0
        if not good.any():
            break
        sdx = (wgt * dx).sum(axis=ax, dtype=np.float64)
        sdy = (wgt * dy).sum(axis=ax, dtype=np.float64)
        sxx = (wgt * (dx * dx)).sum(axis=ax, dtype=np.float64)
        syy = (wgt * (dy * dy)).sum(axis=ax, dtype=np.float64)
        sxy = (wgt * (dx * dy)).sum(axis=ax, dtype=np.float64)
        del wgt
        inv = np.where(good, 1.0 / np.where(good, sw, 1.0), 0.0)
        mx, my = sdx * inv, sdy * inv
        nxx = sxx * inv - mx * mx
        nyy = syy * inv - my * my
        nxy = sxy * inv - mx * my
        # 孔径截断改正(高斯假设):R 固定 ⇒ U 只跟量到的 σ 有关
        f = _aperture_factor((rad * rad) / (2.0 * np.maximum(0.5 * (nxx + nyy), 1e-6)))
        nxx, nyy, nxy = nxx / f, nyy / f, nxy / f
        fine = (good & np.isfinite(nxx) & np.isfinite(nyy) & np.isfinite(nxy)
                & (nxx > 0) & (nyy > 0)
                & (np.abs(mx) <= half) & (np.abs(my) <= half))
        cx = np.where(fine, cx0 + mx, cx)
        cy = np.where(fine, cy0 + my, cy)
        mxx = np.where(fine, nxx, mxx)
        myy = np.where(fine, nyy, myy)
        mxy = np.where(fine, nxy, mxy)
        flux_aper = np.where(fine, sw, flux_aper)
        ok = fine
    return cx, cy, mxx, myy, mxy, flux_aper, ok


def _refine_moments(a: np.ndarray, bg: Background, cx, cy, mxx, myy, mxy, *,
                    scale: float = 3.5, iters: int = 2, max_radius: float = 16.0,
                    where: np.ndarray | None = None, budget: int = 2_000_000,
                    cancel=None):
    """固定圆孔径里重新量质心与二阶矩(**不**做非负裁剪)。

    等照度矩只统计阈值以上的像素,对暗星会明显低估 σ(峰值只有阈值 10 倍时
    低 14%)。这里改用一个跟着星走的**固定圆孔径**(R = ``scale``×σ),孔径内
    所有像素都参与 —— 关键是权重**不能**把负值裁掉:背景噪声零均值,不裁只会
    增加估计的方差、不产生偏差;一裁就变成正偏差,暗星的 σ 会被噪声吹大。
    孔径截断本身用 :func:`_aperture_factor` 做解析改正。

    ``where`` 给出要细化的星(其余原样返回);按 ``budget``(每批的窗口像素总数)
    分批处理,峰值内存与星数无关 —— 低阈值下噪声团块可能上十万个。

    返回 ``(cx, cy, mxx, myy, mxy, flux_aper, ok)``,``ok=False`` 的星
    (孔径流量 ≤ 0,多半是噪声或紧邻亮星的翼)由调用方回落到等照度值。
    """
    cx = np.array(cx, dtype=np.float64, copy=True)
    cy = np.array(cy, dtype=np.float64, copy=True)
    mxx = np.array(mxx, dtype=np.float64, copy=True)
    myy = np.array(myy, dtype=np.float64, copy=True)
    mxy = np.array(mxy, dtype=np.float64, copy=True)
    flux_aper = np.zeros(cx.size, dtype=np.float64)
    ok = np.zeros(cx.size, dtype=bool)

    sel = (np.flatnonzero(where) if where is not None
           else np.arange(cx.size, dtype=np.intp))
    if sel.size == 0:
        return cx, cy, mxx, myy, mxy, flux_aper, ok
    ws = (2 * int(np.clip(math.ceil(scale * 4.0), 2, max_radius)) + 1) ** 2
    step = max(1, int(budget) // max(1, ws))
    for i in range(0, sel.size, step):
        _check_cancel(cancel)
        s = sel[i:i + step]
        rcx, rcy, rxx, ryy, rxy, rfa, rok = _refine_batch(
            a, bg, cx[s], cy[s], mxx[s], myy[s], mxy[s], scale, iters, max_radius)
        cx[s] = np.where(rok, rcx, cx[s])
        cy[s] = np.where(rok, rcy, cy[s])
        mxx[s] = np.where(rok, rxx, mxx[s])
        myy[s] = np.where(rok, ryy, myy[s])
        mxy[s] = np.where(rok, rxy, mxy[s])
        flux_aper[s] = np.where(rok, rfa, flux_aper[s])
        ok[s] = rok
    return cx, cy, mxx, myy, mxy, flux_aper, ok


# ---------------------------------------------------------------- 检测

def _saturation_level(a: np.ndarray, saturation: float | None) -> float:
    """满量程。整数 dtype 直接按位宽取;浮点没法猜,返回 inf(不标饱和)。"""
    if saturation is not None:
        return float(saturation)
    if np.issubdtype(a.dtype, np.unsignedinteger):
        return float(np.iinfo(a.dtype).max)
    if np.issubdtype(a.dtype, np.signedinteger):
        return float(np.iinfo(a.dtype).max)
    return float("inf")


def detect_stars(img, *, background: Background | None = None, box: int = 64,
                 threshold: float = 5.0, channel: int | None = None,
                 rms_method: str = "diff",
                 min_pixels: int = 3, max_pixels: int | None = None,
                 max_eccentricity: float | None = 0.8,
                 elong_ratio: float | None = 0.65,
                 min_snr: float = 0.0,
                 saturation: float | None = None, sat_frac: float = 0.98,
                 edge_margin: int = 1, drop_edge: bool = True,
                 drop_saturated: bool = True,
                 reject_hot: bool = True, hot_ratio: float = 0.5,
                 refine: bool = True, aperture_scale: float = 3.5,
                 apply_filters: bool = True, max_stars: int | None = None,
                 pixel_scale: float | None = None,
                 chunk_rows: int = 512, max_runs: int = 2_000_000,
                 max_threshold_pixels: int | None = 4_000_000,
                 cancel=None) -> StarList:
    """从线性影像里提取星点 → :class:`StarList`(默认按流量降序)。

    流程:估背景 → 逐块减背景并按 ``threshold``×局部σ 取阈 → 行程编码 →
    8-连通标号 → 流量加权矩 → 固定孔径细化 → 过滤。

    重要参数
    ~~~~~~~~
    ``threshold``
        检测阈值,单位是**局部**噪声 σ 的倍数(默认 5)。因为背景和噪声都是
        逐块估的,同一个值在梯度两端是等效的。
    ``refine``
        是否做固定圆孔径的二次测量(默认开)。关掉会快一点,但暗星的 FWHM
        会系统性偏小,质心也略差。
    ``apply_filters``
        关掉就返回**全部**团块并保留 ``saturated``/``edge``/``npix`` 等标志,
        方便自己定过滤规则(单测就这么用)。
    ``max_stars``
        只留最亮的 N 颗。板解算只需要几十颗,早点截断能省掉后面所有开销。
    ``pixel_scale``
        角秒/像素,只是塞进 :class:`StarList` 供 :attr:`~StarList.fwhm_arcsec`
        用,不参与任何计算。**OSC 超像素平面记得填原始尺度的 2 倍。**
    ``max_threshold_pixels``
        阈值以上像素的安全上限。正常星场远低于默认 400 万；平场、全饱和或
        噪声估计退化时尽早中止，避免随后展开连通域时占用数百 MB。传 ``None``
        可显式关闭，但不建议在 GUI 路径中这样做。

    内存:全程按 ``chunk_rows`` 行分块,峰值与图的高度无关
    (512 行 × 6248 列 × float32 ≈ 12 MB/块,同时活着三四块)。

    抛 :class:`ValueError`(输入非法 / 背景尺寸对不上 / 阈值过低导致行程爆炸),
    ``cancel`` 触发时抛 :class:`InterruptedError`。
    """
    a = _as_plane(img, channel)
    h, w = a.shape
    threshold = float(threshold)
    if not np.isfinite(threshold) or threshold < 0:
        raise ValueError(_("threshold 必须是非负有限数,收到 {threshold}").format(threshold=threshold))

    bg = background if background is not None else estimate_background(
        a, box, rms_method=rms_method, cancel=cancel)
    if tuple(bg.shape) != (h, w):
        raise ValueError(_("背景尺寸 {0} 与影像 {1} 不一致").format(tuple(bg.shape), (h, w)))
    meta = dict(shape=(h, w), threshold=threshold, box=bg.box,
                pixel_scale=pixel_scale)

    # ---- 逐块取阈 + 行程编码 ----
    rows_l, ca_l, cb_l, det_l, raw_l = [], [], [], [], []
    n_runs = 0
    n_threshold = 0
    if max_threshold_pixels is not None:
        max_threshold_pixels = int(max_threshold_pixels)
        if max_threshold_pixels <= 0:
            raise ValueError(_("max_threshold_pixels 必须为正数或 None"))
    step = max(1, int(chunk_rows))
    for y0 in range(0, h, step):
        _check_cancel(cancel)
        y1 = min(h, y0 + step)
        chunk = np.asarray(a[y0:y1], dtype=np.float32)
        bk, rm = bg.rows(y0, y1)
        det = chunk - bk
        mask = det > (rm * np.float32(threshold))
        n_threshold += int(np.count_nonzero(mask))
        if (max_threshold_pixels is not None
                and n_threshold > max_threshold_pixels):
            raise ValueError(
                _("阈值过低或背景估计失败:阈值以上像素超过 {max_threshold_pixels:,}。请提高 threshold,或检查影像是否几乎全是信号(平场/全饱和)。").format(
                    max_threshold_pixels=max_threshold_pixels))
        if not mask.any():
            continue
        r, c0, c1 = _extract_runs(mask, y0, w)
        n_runs += r.size
        if n_runs > max_runs:
            raise ValueError(
                _("阈值过低或背景估计失败:行程数超过 {max_runs:,}。请提高 threshold,或检查影像是否几乎全是信号(平场/全饱和)。").format(
                    max_runs=max_runs))
        rows_l.append(r)
        ca_l.append(c0)
        cb_l.append(c1)
        det_l.append(det[mask])
        raw_l.append(chunk[mask])
        del chunk, bk, rm, det, mask

    if not rows_l:
        return _empty_stars(**meta)

    rows = np.concatenate(rows_l)
    ca = np.concatenate(ca_l)
    cb = np.concatenate(cb_l)
    vals = np.concatenate(det_l).astype(np.float64)
    raws = np.concatenate(raw_l).astype(np.float64)
    del rows_l, ca_l, cb_l, det_l, raw_l
    _check_cancel(cancel)

    # ---- 连通域 ----
    lab, k = _label_runs(rows, ca, cb, w)
    if k == 0:
        return _empty_stars(**meta)
    pix_lab, px, py = _expand_runs(rows, ca, cb, lab)
    pxf = px.astype(np.float64)
    pyf = py.astype(np.float64)
    _check_cancel(cancel)

    # ---- 一阶/二阶矩(全部 bincount,一次扫完) ----
    sw = np.bincount(pix_lab, weights=vals, minlength=k)
    sw_safe = np.where(sw > 0, sw, 1.0)
    cx = np.bincount(pix_lab, weights=vals * pxf, minlength=k) / sw_safe
    cy = np.bincount(pix_lab, weights=vals * pyf, minlength=k) / sw_safe
    mxx = np.bincount(pix_lab, weights=vals * pxf * pxf, minlength=k) / sw_safe - cx * cx
    myy = np.bincount(pix_lab, weights=vals * pyf * pyf, minlength=k) / sw_safe - cy * cy
    mxy = np.bincount(pix_lab, weights=vals * pxf * pyf, minlength=k) / sw_safe - cx * cy
    npix = np.bincount(pix_lab, minlength=k).astype(np.int32)

    peak_sub = np.zeros(k, dtype=np.float64)
    np.maximum.at(peak_sub, pix_lab, vals)
    peak_raw = np.zeros(k, dtype=np.float64)
    np.maximum.at(peak_raw, pix_lab, raws)

    xmin = np.full(k, w, dtype=np.int32)
    xmax = np.full(k, -1, dtype=np.int32)
    ymin = np.full(k, h, dtype=np.int32)
    ymax = np.full(k, -1, dtype=np.int32)
    np.minimum.at(xmin, lab, ca)
    np.maximum.at(xmax, lab, cb - 1)
    np.minimum.at(ymin, lab, rows)
    np.maximum.at(ymax, lab, rows)
    del pix_lab, px, py, pxf, pyf, vals, raws
    _check_cancel(cancel)

    flux_iso = sw.copy()
    mxx = np.maximum(mxx, 0.0)
    myy = np.maximum(myy, 0.0)
    flux_aper = flux_iso.copy()
    refined = np.zeros(k, dtype=bool)
    if refine and k:
        # 1 像素的团块没有可细化的形状(孔径里全是噪声),跳过省一大笔:
        # 低阈值下噪声单像素团块能占绝大多数
        rcx, rcy, rxx, ryy, rxy, faper, ok = _refine_moments(
            a, bg, cx, cy, mxx, myy, mxy, scale=aperture_scale,
            where=(npix >= 2), cancel=cancel)
        cx, cy = np.where(ok, rcx, cx), np.where(ok, rcy, cy)
        mxx, myy, mxy = (np.where(ok, rxx, mxx), np.where(ok, ryy, myy),
                         np.where(ok, rxy, mxy))
        flux_aper = np.where(ok, faper, flux_aper)
        refined = ok

    fwhm, smaj, smin, theta, ell, ecc = _shape_from_moments(mxx, myy, mxy)

    iy = np.clip(np.rint(cy), 0, h - 1).astype(np.int32)
    ix = np.clip(np.rint(cx), 0, w - 1).astype(np.int32)
    back_at = bg.back_at(iy, ix).astype(np.float64)
    rms_at = np.maximum(bg.rms_at(iy, ix).astype(np.float64), 1e-9)

    sat_level = _saturation_level(np.asarray(a), saturation)
    m = int(max(0, edge_margin))
    stars = StarList(
        x=cx.astype(np.float32), y=cy.astype(np.float32),
        flux=flux_iso.astype(np.float32), flux_aper=flux_aper.astype(np.float32),
        peak=peak_raw.astype(np.float32), peak_sub=peak_sub.astype(np.float32),
        background=back_at.astype(np.float32), noise=rms_at.astype(np.float32),
        snr=(flux_iso / (rms_at * np.sqrt(np.maximum(npix, 1)))).astype(np.float32),
        peak_snr=(peak_sub / rms_at).astype(np.float32),
        npix=npix, fwhm=fwhm, sigma_major=smaj, sigma_minor=smin,
        theta=theta, ellipticity=ell, eccentricity=ecc,
        saturated=(peak_raw >= sat_frac * sat_level),
        edge=((xmin <= m) | (ymin <= m) | (xmax >= w - 1 - m) | (ymax >= h - 1 - m)),
        refined=refined, xmin=xmin, xmax=xmax, ymin=ymin, ymax=ymax,
        n_blobs=k, **meta)

    if apply_filters:
        stars = filter_stars(
            stars, min_pixels=min_pixels, max_pixels=max_pixels,
            max_eccentricity=max_eccentricity, elong_ratio=elong_ratio,
            min_snr=min_snr, drop_edge=drop_edge, drop_saturated=drop_saturated,
            reject_hot=reject_hot, hot_ratio=hot_ratio)
    stars = stars.sorted_by_flux()
    if max_stars is not None:
        stars = stars.brightest(int(max_stars))
    return stars


def filter_stars(stars: StarList, *, min_pixels: int = 3,
                 max_pixels: int | None = None,
                 max_eccentricity: float | None = 0.8,
                 elong_ratio: float | None = 0.65,
                 min_snr: float = 0.0,
                 drop_edge: bool = True, drop_saturated: bool = True,
                 reject_hot: bool = True, hot_ratio: float = 0.5) -> StarList:
    """按各条规则剔除不可信的团块;剔除数量记进 :attr:`StarList.rejects`。

    规则(**按顺序判定**,一颗星只记进第一条命中的规则,不重复计数):

    ``too_small``
        像素数 < ``min_pixels``(默认 3)。单像素亮点、读出噪声尖峰。
    ``too_big``
        像素数 > ``max_pixels``(默认不限)。星云核、卫星迹、连成一片的密集星团。
        密集/有星云的场建议设个上限;注意**离焦帧的甜甜圈星像本身就很大**
        (真机 NGC 2237 那一夜离焦,单颗星 300+ 像素),别一刀切掉真星。
    ``edge``
        团块触到画面边缘 —— 流量被截断,质心必然偏,还会污染孔径细化。
    ``saturated``
        峰值接近满量程。饱和星的核是平顶,质心和 FWHM 都不可信;ADC 削顶之后的
        形状统计更是完全没有意义。
    ``elongated``
        轴比 ``b/a`` 低于门限。门限取**两者中更宽松的那个**:
        (a) 绝对门限 ``max_eccentricity``(默认 0.8,对应 b/a = 0.6);
        (b) 同图中位轴比的 ``elong_ratio`` 倍(默认 0.65)。

        (b) 这条自适应项是被真机数据逼出来的:**整场星点被跟踪误差拉长时,
        固定阈值会把星全吃光**。M 16 那一帧实测中位偏心率 0.927
        (:meth:`StarList.stats` 的 ``theta_r`` = 0.64,方向高度一致 = 典型拖线),
        固定 0.8 会剔掉 **95% 的星,包括全部亮星**;自适应之后门限自动放宽到
        ecc≈0.97,拖线的星保住了,而真正的卫星迹 / 宇宙线径迹(b/a < 0.1)照样出局。
        反过来,画面正常时 (b) 算出来比 (a) 松不了多少,绝对门限继续起作用。
        ``elong_ratio=None`` 关掉自适应,只用绝对门限。
    ``hot``
        FWHM < ``hot_ratio`` × 同图 FWHM 中位数(默认 0.5)。热像素 / 宇宙线击中
        的特征就是"比真星锐得多":真星的宽度由大气 + 光学决定,全场一致;
        单像素事件没有 PSF,FWHM 接近 0。少于 5 颗星时不启用(中位数不可信)。
    ``low_snr``
        等照度信噪比 < ``min_snr``(默认 0,关闭)。
    """
    n = len(stars)
    if n == 0:
        return stars
    reason = np.zeros(n, dtype=np.int8)     # 0 = 保留
    names = ["too_small", "too_big", "edge", "saturated",
             "elongated", "hot", "low_snr"]

    def mark(code: int, cond: np.ndarray) -> None:
        np.putmask(reason, (reason == 0) & cond, np.int8(code))

    npix = np.asarray(stars.npix)
    mark(1, npix < int(min_pixels))
    if max_pixels is not None:
        mark(2, npix > int(max_pixels))
    if drop_edge:
        mark(3, np.asarray(stars.edge))
    if drop_saturated:
        mark(4, np.asarray(stars.saturated))
    if max_eccentricity is not None:
        ecc = np.clip(np.asarray(stars.eccentricity, dtype=np.float64), 0.0, 1.0)
        ratio = np.sqrt(np.maximum(1.0 - ecc * ecc, 0.0))        # b/a
        thr = math.sqrt(max(1.0 - float(max_eccentricity) ** 2, 0.0))
        if elong_ratio is not None:
            alive = (reason == 0) & np.isfinite(ratio)
            if int(alive.sum()) >= 5:
                thr = min(thr, float(elong_ratio) * float(np.median(ratio[alive])))
        mark(5, ~np.isfinite(np.asarray(stars.eccentricity)) | (ratio < thr))
    fwhm = np.asarray(stars.fwhm, dtype=np.float64)
    if reject_hot:
        alive = (reason == 0) & np.isfinite(fwhm) & (fwhm > 0)
        if int(alive.sum()) >= 5:
            med = float(np.median(fwhm[alive]))
            if med > 0:
                mark(6, ~np.isfinite(fwhm) | (fwhm < float(hot_ratio) * med))
    if min_snr > 0:
        mark(7, np.asarray(stars.snr) < float(min_snr))

    out = stars.select(reason == 0)
    counts = dict(stars.rejects)
    for code, name in enumerate(names, start=1):
        c = int((reason == code).sum())
        if c:
            counts[name] = counts.get(name, 0) + c
    out.rejects = counts
    return out


def brightest(stars: StarList, n: int) -> StarList:
    """按流量取最亮的 ``n`` 颗(:meth:`StarList.brightest` 的函数形式)。"""
    return stars.brightest(n)
