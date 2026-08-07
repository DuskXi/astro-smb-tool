"""板解算(plate solving):**有初始猜测**的约束解算,纯 numpy/标准库。

给一张图上提取到的星点(:mod:`astro_smb.stars`)、一份本地星表
(:mod:`astro_smb.catalog`)和一份粗略先验(指向 + 焦距 + 像元),解出这张图的
WCS(:class:`astro_smb.wcs.TanWcs`)。**不做盲解算** —— 没有指向先验直接返回
``ok=False``,理由 :data:`REASON_NO_HINT`。

为什么是"成对距离播种 + 批量内点投票",而不是三角形/quad 几何哈希
--------------------------------------------------------------------

几何哈希(astrometry.net 的 quad)的全部价值是**尺度不变性** —— 它假设你不知道
像素尺度。而我们知道:FITS 头里有 ``FOCALLEN`` 和 ``XPIXSZ``,
``206.2648 × 像元µm / 焦距mm`` 就是角秒/像素。**尺度已知时,两颗星就唯一确定
一个相似变换**(平移 2 + 旋转 1 + 尺度 1 = 4 个自由度,两颗星给 4 个方程),
根本用不到第三颗、第四颗星。代价是候选数量大,靠**向量化批量投票**压下去。

顺带说一句为什么不用 astrometry.net 的索引文件:2000mm 焦距的最小可用集
**2.31 GB**,比原始 Tycho-2 贵 65 倍,而且 4100/5200 系列没有 license 声明。

算法(:func:`solve`)
~~~~~~~~~~~~~~~~~~~~

1. **先验 → 几何**:像素尺度、视场半径、视场面积(:class:`SolveHint`)。
2. **分级搜索半径** 1° → 5° → 15°。实测指向误差是**双峰**的:light 帧的
   赤道仪指向 vs 实际中心 90% 分位 1.0°、最大 1.5°,但 AutoCenter 刚 GOTO 完的
   定位帧里出现过 2.81° 和 13.07° 的粗差。半径大到一个锥装不下时**切片成多个
   tile**,每个 tile 是一次独立的小半径解算(总工作量 ∝ R²·r²,tile 小才划算)。
3. **星表候选数自适应**:``n_cat ≈ n_image × 锥面积/视场面积 × margin``。
   **写死一个数是错的** —— 实测写死 150 在 2000mm 上只有 4/10 成功,自适应 10/10。
4. **播种**:图上最亮 ``n_seed`` 颗两两配对,按**角距**在(排序好的)星表星对上
   ``searchsorted`` 取一段;每个 (图星对, 表星对) × 2 种对应 × 2 种宇称 = 一个
   候选相似变换 ``c``(复数:``z_sky = c·z_img`` 或 ``c·conj(z_img)``)。
5. **两级剪枝(全向量化)**:候选的 ZWO 旋转角是否落在先验 ±tol 内(有先验时
   实测提速 16 倍);候选反推的视场中心是否落在搜索半径内。
6. **两级投票**:先用最亮 ``n_probe`` 颗探针星做廉价筛(把图星经候选变换投到
   tile 切平面,查星表网格);过关的才拿全部 ``n_image`` 颗做完整投票。
7. **接受判据**:内点数 ≥ ``min_matches`` **且** 假阳率(Poisson 尾 + 已试候选数
   的 Bonferroni)足够低。**假阳性解算比解不出来危险得多** —— 一个错的 WCS 会
   悄悄污染下游所有对账。
8. **精拟合**:用 :func:`astro_smb.wcs.fit_tan` + sigma 剔除迭代;每轮用当前解
   重新查表、重新配对(能配上的星越来越多)。

坐标约定(**看这里,不要猜**)
-------------------------------

:func:`solve` 的输入和 :attr:`SolveResult.wcs` 一律是 **FITS 像素约定**:
1-based、存储序、y 自底向上。:mod:`astro_smb.stars` 给的是 0-based 数组坐标
(y 向下),换算走 :func:`fits_xy_from_stars`(内部用
:func:`astro_smb.wcs.array_to_fits_xy`,并处理超像素的 2× 缩放)。
传 :class:`~astro_smb.stars.StarList` 给 :func:`solve` 时会自动换算,前提是
``hint.image_size`` 给了**全分辨率**尺寸(据此反推缩放倍数)。

**结果永远是全分辨率坐标系下的 WCS**,可以直接写回 FITS 头、直接和 ASIAIR
自己解算回写的那份 ``CRVAL/CD`` 比对。

已知边界
~~~~~~~~

* 视场半径 ≳ 5° 时,"以某颗星为切点的线性 CD"这个播种模型的射影残差开始变大
  (量级 ρ³/3 弧度),投票容差已按 ρ² 自适应放宽,但再大的视场(鱼眼/全天相机)
  本模块不保证。
* **不解 SIP 畸变**。真机标定:纯 TAN 的残差地板本身就有 1.3~1.7 px
  (见 :mod:`astro_smb.wcs` 的表),所以**判成功看内点数,不卡死 RMS**。
* :meth:`SolveHint.from_header` **刻意不读头里已有的 CRVAL/CD** —— 只用赤道仪
  报的 ``RA``/``DEC``。这样解算结果才是一条**独立**的证据链,可以拿去和 ZWO
  自己的解算对账(实测 ASIAIR 的 light 帧头里就带 ``RA---TAN-SIP`` 全套)。
"""

from __future__ import annotations

import math
import os
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import stars as _stars
from .astro import dec_str_to_deg, ra_str_to_deg
from .catalog import Catalog, catalog_path, jyear_from_unix
from .fitshdr import FitsHeader, header_read_hint, parse_fits_header
from .fitsimage import (FitsGeometry, FitsImageError, decode_pixels,
                        geometry_from_header)
from .naming import parse_image_name
from .wcs import (TanWcs, WcsError, angular_separation, array_to_fits_xy,
                  fit_tan, fit_tan_sigma_clip, pixel_to_world, to_fits_cards,
                  unit_to_radec, world_to_pixel)
from astro_smb.i18n import N_, gettext as _

__all__ = [
    "SolveError", "SolveHint", "SolveResult",
    "REASON_OK", "REASON_NO_HINT", "REASON_FEW_STARS", "REASON_NO_CATALOG",
    "REASON_NO_MATCH", "REASON_BAD_FIT", "REASON_TIMEOUT", "REASON_TEXT",
    "DEFAULT_RADII",
    "pixel_scale_arcsec", "zwo_angle_from_cd", "fits_xy_from_stars",
    "solve", "solve_file", "default_catalog", "close_default_catalog",
]


class SolveError(ValueError):
    """调用参数非法。

    **注意区别**:"解不出来"不是异常,是 ``ok=False`` 的 :class:`SolveResult`。
    只有输入本身有问题(星点数组形状不对、星表打不开、先验自相矛盾)才抛这个。
    """


# ------------------------------------------------------------------ 失败分类

REASON_OK = ""
#: 没有指向先验(ra/dec 缺失)或没有尺度先验(焦距/像元缺失)—— 本模块不做盲解算
REASON_NO_HINT = "no_hint"
#: 图上星点太少
REASON_FEW_STARS = "few_stars"
#: 星表在该天区覆盖不足(锥内星数不够)
REASON_NO_CATALOG = "no_catalog"
#: 试遍所有候选也找不到一致的变换
REASON_NO_MATCH = "no_match"
#: 找到了一致变换但精拟合失败 / 残差过大 / 内点塌缩
REASON_BAD_FIT = "bad_fit"
#: 超过时间预算
REASON_TIMEOUT = "timeout"

#: 理由键 → **显示文本的 msgid**。表在模块级、import 时求值一次,所以这里
#: 只能用 `N_()`(只标记不翻);真正翻译在取用的地方(见 `SolveResult.summary`)。
#: 直接写 `_()` 会把翻译冻在 import 那一刻的语言上,之后切语言这几句永远不变。
REASON_TEXT = {
    REASON_OK: N_("解算成功"),
    REASON_NO_HINT: N_("缺少指向或尺度先验"),
    REASON_FEW_STARS: N_("图上星点太少"),
    REASON_NO_CATALOG: N_("星表在该天区覆盖不足"),
    REASON_NO_MATCH: N_("找不到一致的变换"),
    REASON_BAD_FIT: N_("拟合残差过大"),
    REASON_TIMEOUT: N_("超过时间预算"),
}

#: 分级搜索半径(度)。实测指向误差双峰:正常 ≤1.6°,粗差 2.8°/13.1°
DEFAULT_RADII = (1.0, 5.0, 15.0)

_LOG10 = math.log(10.0)
#: 206264.806…″/rad —— 焦距/像元换算像素尺度用
_ARCSEC_PER_RAD = 180.0 * 3600.0 / math.pi


# ------------------------------------------------------------------ 小工具


def pixel_scale_arcsec(focal_len_mm: float, pixel_size_um: float,
                       binning: int = 1) -> float:
    """焦距(mm)+ 像元(µm)→ 像素尺度(角秒/像素)。

    ``binning`` 只在 ``pixel_size_um`` 是**未合并**的传感器像元时才给。
    ASIAIR 头里的 ``XPIXSZ`` 实测**已经含 binning**,所以
    :meth:`SolveHint.from_header` 传的是 1。
    """
    f = float(focal_len_mm)
    p = float(pixel_size_um)
    b = max(1, int(binning))
    if not (math.isfinite(f) and math.isfinite(p)) or f <= 0.0 or p <= 0.0:
        raise SolveError(_("焦距/像元非法: focal={focal_len_mm} pixel={pixel_size_um}").format(
            focal_len_mm=focal_len_mm, pixel_size_um=pixel_size_um))
    return _ARCSEC_PER_RAD * (p * 1e-3 * b) / f


def zwo_angle_from_cd(cd) -> float:
    """CD 矩阵 → **ZWO/ASIAIR 约定**的旋转角(度,0~360)。

    ``(degrees(atan2(CD2_1, CD1_1)) + 180) mod 360`` —— 146 帧真机对账得出:
    与文件名 ``<N>deg`` 之差中位 0.31°、最大 0.77°(残差来源是文件名记的是
    **上一次**解算的角度)。与 :meth:`astro_smb.wcs.TanWcs.rotation_deg`
    **不是同一个量**,别混用。
    """
    m = np.asarray(cd, dtype=np.float64).reshape(2, 2)
    return (math.degrees(math.atan2(m[1, 0], m[0, 0])) + 180.0) % 360.0


def _angle_diff(a, b):
    """角度差的绝对值(度),折叠到 [0, 180]。向量化。"""
    d = np.abs(np.asarray(a, dtype=np.float64) - float(b)) % 360.0
    return np.minimum(d, 360.0 - d)


def _basis3(ra_deg, dec_deg):
    """(ra, dec) → 正交基 ``(x̂ 指向, ê 东, n̂ 北)``,每个 ``(..., 3)``。"""
    ra = np.radians(np.asarray(ra_deg, dtype=np.float64))
    dec = np.radians(np.asarray(dec_deg, dtype=np.float64))
    ca, sa = np.cos(ra), np.sin(ra)
    cd, sd = np.cos(dec), np.sin(dec)
    z = np.zeros_like(ca)
    x_hat = np.stack([cd * ca, cd * sa, sd], axis=-1)
    e_hat = np.stack([-sa, ca, z], axis=-1)
    n_hat = np.stack([-sd * ca, -sd * sa, cd], axis=-1)
    return x_hat, e_hat, n_hat


def _tangent_offset(xh, eh, nh, a, b) -> np.ndarray:
    """以 ``a`` 为切点看 ``b`` 的 gnomonic 偏移,复数 ``ξ + iη``(**度**)。

    ``xh/eh/nh`` 是全部星的正交基,``a``/``b`` 是下标数组。
    """
    vb = xh[b]
    d = np.einsum("ij,ij->i", vb, xh[a])
    xi = np.degrees(np.einsum("ij,ij->i", vb, eh[a]) / d)
    eta = np.degrees(np.einsum("ij,ij->i", vb, nh[a]) / d)
    return xi + 1j * eta


#: 探针筛过关后最多再做多少个完整投票(稠密天区的兜底,正常只有个位数)
_MAX_FULL_VOTE = 256


def _adaptive_catalog_count(n_image: int, cone_r_deg: float,
                            field_area_deg2: float, margin: float,
                            lo: int, hi: int) -> int:
    """锥内取多少颗星表星参与解算 —— **按锥/视场面积比自适应,不许写死**。

    设计不变量:``n_cat × 视场面积 / 锥面积 = n_image × margin``,也就是
    **落在视场里的星表候选数与焦距无关**,恒为"图上用的星数 × 安全系数"。

    写死一个数在长焦上必挂:2000mm 的视场只有 0.30 deg²,而搜索锥有 3.2 deg²
    —— 写死 150 颗最亮的摊进视场只剩 14 颗,播种要的那对星大概率不在里面
    (实测 4/10 成功);自适应之后 10/10。400mm 视场 7.5 deg² 时同样写死 150
    还剩 22 颗,所以"写死"这个 bug 在短焦上根本暴露不出来。
    """
    ratio = _cone_area_deg2(cone_r_deg) / max(float(field_area_deg2), 1e-9)
    want = float(n_image) * ratio * float(margin)
    return int(min(float(hi), max(float(lo), want)))


def _cone_area_deg2(radius_deg: float) -> float:
    """球冠面积(平方度)。小半径下退化为 π r²,大半径下不会高估。"""
    r = math.radians(max(0.0, min(180.0, float(radius_deg))))
    return 2.0 * math.pi * (1.0 - math.cos(r)) * (180.0 / math.pi) ** 2


def _log10_poisson_tail(n: int, mu: float) -> float:
    """``log10 P(X ≥ n)``,X ~ Poisson(mu)。上界估计,宁可偏保守(偏大)。

    首项 ``e^-µ µⁿ/n!`` 乘上比值小于 ``µ/(n+1)`` 的几何级数上界。
    ``µ ≥ n`` 时尾部根本不小,直接返回 0(= 完全不显著)。
    """
    n = int(n)
    mu = float(mu)
    if n <= 0:
        return 0.0
    if not math.isfinite(mu) or mu <= 0.0:
        return -300.0
    if mu >= n:
        return 0.0
    log_first = -mu + n * math.log(mu) - math.lgamma(n + 1.0)
    bound = log_first - math.log1p(-mu / (n + 1.0))
    return max(-300.0, bound / _LOG10)


def _check_cancel(cancel) -> None:
    if cancel is not None and cancel.is_set():
        raise InterruptedError(_("解算已取消"))


# ------------------------------------------------------------------ 点网格


#: 单个格子里最多扫描前多少个点(退化输入的兜底,正常密度下 max_count = 1)
_MAX_PER_CELL = 64


class _PointGrid:
    """均匀网格上的"半径内最近点"批量查询,**精确**(与暴力最近邻逐条一致)。

    单元格边长取 ``max(2r, span/1024)``:2r 保证一次查询只需看 **2×2** 个格子
    (查询点 ±r 的区间宽 2r ≤ 格宽,最多跨两格);``span/1024`` 的下限保证格子
    总数不会被极端跨度炸掉。

    格子里存的是**变长桶**(按格号排序 + start/count 索引),不是"一格一个点"。
    一格一个点看着够用(格边 = 2×容差,点又稀),但实测 200 个点撒在 50×50 格
    上就已经开始丢:被覆盖掉的那颗永远配不上。丢配对在这里不是"少一点精度",
    而是**内点数下降 → 过不了接受判据 → 整张图解不出来**,所以必须精确。
    """

    __slots__ = ("x", "y", "cell", "x0", "y0", "nx", "ny",
                 "order", "start", "count", "depth")

    def __init__(self, x, y, radius: float):
        self.x = np.ascontiguousarray(x, dtype=np.float64)
        self.y = np.ascontiguousarray(y, dtype=np.float64)
        n = self.x.size
        if self.y.size != n:
            raise SolveError(_("网格 x/y 长度不一致"))
        r = float(radius)
        if not math.isfinite(r) or r <= 0.0:
            raise SolveError(_("网格半径非法: {radius}").format(radius=radius))
        if n == 0:
            self.cell = 2.0 * r
            self.x0 = self.y0 = 0.0
            self.nx = self.ny = 1
            self.order = np.zeros(0, dtype=np.int32)
            self.start = np.zeros(1, dtype=np.int64)
            self.count = np.zeros(1, dtype=np.int32)
            self.depth = 0
            return
        span = max(float(self.x.max() - self.x.min()),
                   float(self.y.max() - self.y.min()), 1e-12)
        self.cell = max(2.0 * r, span / 1024.0)
        self.x0 = float(self.x.min()) - self.cell
        self.y0 = float(self.y.min()) - self.cell
        self.nx = int((float(self.x.max()) - self.x0) / self.cell) + 2
        self.ny = int((float(self.y.max()) - self.y0) / self.cell) + 2
        gx = ((self.x - self.x0) / self.cell).astype(np.int64)
        gy = ((self.y - self.y0) / self.cell).astype(np.int64)
        cid = gy * self.nx + gx
        ncell = self.nx * self.ny
        self.order = np.argsort(cid, kind="stable").astype(np.int32)
        cnt = np.bincount(cid, minlength=ncell)
        self.count = cnt.astype(np.int32)
        self.start = np.concatenate([[0], np.cumsum(cnt)[:-1]])
        self.depth = int(min(cnt.max(), _MAX_PER_CELL))

    def query(self, qx, qy, radius: float):
        """批量查询 → ``(idx, d2)``,``idx`` 为 -1 表示半径内没有点。

        ``qx``/``qy`` 任意同形状数组(可含 NaN/Inf,一律判为未命中)。
        """
        q_x = np.asarray(qx, dtype=np.float64)
        q_y = np.asarray(qy, dtype=np.float64)
        r = float(radius)
        best = np.full(q_x.shape, -1, dtype=np.int32)
        best_d2 = np.full(q_x.shape, r * r, dtype=np.float64)
        if self.x.size == 0:
            return best, best_d2
        with np.errstate(invalid="ignore"):
            ok = np.isfinite(q_x) & np.isfinite(q_y)
            lo_x = np.where(ok, (q_x - r - self.x0) / self.cell, -1.0)
            hi_x = np.where(ok, (q_x + r - self.x0) / self.cell, -1.0)
            lo_y = np.where(ok, (q_y - r - self.y0) / self.cell, -1.0)
            hi_y = np.where(ok, (q_y + r - self.y0) / self.cell, -1.0)
        ix = (np.floor(lo_x).astype(np.int64), np.floor(hi_x).astype(np.int64))
        iy = (np.floor(lo_y).astype(np.int64), np.floor(hi_y).astype(np.int64))
        for gx in ix:
            in_x = ok & (gx >= 0) & (gx < self.nx)
            gxc = np.clip(gx, 0, self.nx - 1)
            for gy in iy:
                m = in_x & (gy >= 0) & (gy < self.ny)
                if not m.any():
                    continue
                cell = np.where(m, gy.clip(0, self.ny - 1) * self.nx + gxc, 0)
                s = self.start[cell]
                c = self.count[cell]
                for k in range(self.depth):
                    live = m & (c > k)
                    if not live.any():
                        break
                    pi = self.order[np.where(live, s + k, 0)]
                    dx = q_x - self.x[pi]
                    dy = q_y - self.y[pi]
                    d2 = dx * dx + dy * dy
                    take = live & (d2 < best_d2)
                    best = np.where(take, pi.astype(np.int32), best)
                    best_d2 = np.where(take, d2, best_d2)
        return best, best_d2


# ------------------------------------------------------------------ 先验


@dataclass(frozen=True)
class SolveHint:
    """解算先验。至少要有**指向**(ra/dec)和**尺度**(焦距+像元,或直接给尺度)。

    :param ra_deg: 视场中心赤经(度)。ASIAIR 头里的 ``RA`` 就是十进制度。
    :param dec_deg: 视场中心赤纬(度)。
    :param focal_len_mm: 焦距。ASIAIR 的 ``FOCALLEN`` 是**上次解算回写的值**,
        同一晚同一镜子会在 401/402/403 之间跳 —— 当先验够用,别当常数。
    :param pixel_size_um: 像元尺寸。ASIAIR 的 ``XPIXSZ`` **已含 binning**。
    :param binning: 仅当 ``pixel_size_um`` 是未合并像元时才 >1。
    :param image_size: ``(宽, 高)``,**全分辨率** FITS 像素。
    :param rotation_deg: 旋转角先验,**ZWO 约定**(见 :func:`zwo_angle_from_cd`)
        —— 因为两个现成来源(文件名 ``<N>deg``、日志 ``Angle = ...``)都是这个
        约定。给了实测提速 16 倍。
    :param rotation_tol_deg: 旋转先验的容差(±)。
    :param pixel_scale: 直接给角秒/像素;给了就不用焦距/像元。
    :param scale_tol: 尺度先验的相对容差,决定星对距离配对的窗口宽度。
        ASIAIR 回写的焦距准到 0.3%,用户手填的标称焦距可能差 5%。
    :param flipped: 是否镜像(``det(CD) > 0``)。ASIAIR light 帧实测**恒为 True**。
        ``None`` = 两种宇称都试(慢一倍)。
    :param epoch: 观测历元(儒略年),用于星表自行外推。J2000 到 2026 已 26 年,
        2000mm 下累计 1~3 像素,**该外推就外推**。
    :param radius_deg: 覆盖分级搜索半径的第一级;``None`` 用 :data:`DEFAULT_RADII`。
    :param source: 先验从哪儿来的(纯记录,便于排查)。
    """

    ra_deg: float | None = None
    dec_deg: float | None = None
    focal_len_mm: float | None = None
    pixel_size_um: float | None = None
    binning: int = 1
    image_size: tuple[int, int] | None = None
    rotation_deg: float | None = None
    rotation_tol_deg: float = 10.0
    pixel_scale: float | None = None
    scale_tol: float = 0.02
    flipped: bool | None = None
    epoch: float | None = None
    radius_deg: float | None = None
    source: str = ""

    # -------- 派生量

    @property
    def has_pointing(self) -> bool:
        return (self.ra_deg is not None and self.dec_deg is not None
                and math.isfinite(float(self.ra_deg))
                and math.isfinite(float(self.dec_deg)))

    def pixel_scale_arcsec(self) -> float | None:
        """角秒/像素;算不出来返回 ``None``。"""
        if self.pixel_scale is not None:
            v = float(self.pixel_scale)
            return v if math.isfinite(v) and v > 0.0 else None
        if self.focal_len_mm is None or self.pixel_size_um is None:
            return None
        try:
            return pixel_scale_arcsec(self.focal_len_mm, self.pixel_size_um,
                                      self.binning)
        except SolveError:
            return None

    def field_radius_deg(self) -> float | None:
        """视场外接圆半径(度)= 半对角线 × 像素尺度。"""
        s = self.pixel_scale_arcsec()
        if s is None or not self.image_size:
            return None
        w, h = float(self.image_size[0]), float(self.image_size[1])
        if w <= 0 or h <= 0:
            return None
        return 0.5 * math.hypot(w, h) * s / 3600.0

    def field_area_deg2(self) -> float | None:
        """视场面积(平方度)。星表候选数自适应就靠它。"""
        s = self.pixel_scale_arcsec()
        if s is None or not self.image_size:
            return None
        w, h = float(self.image_size[0]), float(self.image_size[1])
        return w * h * (s / 3600.0) ** 2

    # -------- 装配

    @classmethod
    def from_header(cls, hdr: FitsHeader, name: str | None = None,
                    **overrides) -> "SolveHint":
        """从 FITS 头(+ 可选文件名)装配先验。

        取用的卡片(真机 237 条头统计的存在率):``RA``/``DEC``(233,**十进制度**,
        ``OBJCTRA``/``OBJCTDEC`` 实测**根本不存在**)、``FOCALLEN``(233)、
        ``XPIXSZ``(233)、``NAXIS1/2``、``DATE-OBS``(233,**UTC**)、
        ``ROTATOR``(180,与文件名 ``<N>deg`` 同源)。

        ``name`` 给了就用 :func:`astro_smb.naming.parse_image_name` 取
        ``angle_deg`` 当旋转先验(优先于 ``ROTATOR``)。注意 SD 卡方言的文件名
        现在解析不了(没有 ``deg`` 字段),拿不到就退回 ``ROTATOR``。

        **不读头里已有的 ``CRVAL``/``CD``** —— 见模块 docstring 的说明。
        ``overrides`` 直接覆盖任何字段。
        """
        get = hdr.get

        def num(key):
            v = get(key)
            if v is None:
                return None
            try:
                f = float(str(v).strip())
            except (TypeError, ValueError):
                return None
            return f if math.isfinite(f) else None

        ra = num("RA")
        if ra is None:
            ra = ra_str_to_deg(get("RA")) or ra_str_to_deg(get("OBJCTRA"))
        dec = num("DEC")
        if dec is None:
            dec = dec_str_to_deg(get("DEC")) or dec_str_to_deg(get("OBJCTDEC"))

        pix = num("XPIXSZ")
        if pix is None:
            pix = num("YPIXSZ")
        size = None
        nx, ny = num("NAXIS1"), num("NAXIS2")
        if nx and ny:
            size = (int(nx), int(ny))

        rot = None
        if name:
            parsed = parse_image_name(os.path.basename(str(name)))
            if parsed is not None and parsed.angle_deg is not None:
                rot = float(parsed.angle_deg)
        if rot is None:
            rot = num("ROTATOR")

        kw = dict(
            ra_deg=ra, dec_deg=dec, focal_len_mm=num("FOCALLEN"),
            pixel_size_um=pix, binning=1, image_size=size,
            rotation_deg=rot, epoch=_epoch_from_header(hdr),
            source=str(name) if name else _("FITS 头"),
        )
        kw.update(overrides)
        return cls(**kw)


def _epoch_from_header(hdr: FitsHeader) -> float | None:
    """``DATE-OBS`` → 儒略年。

    ``DATE-OBS`` 是 **UTC**(实测 ``2025-11-04T16:56:49.794842``),
    **绝不能**走 :func:`astro_smb.astro.unix_from_local`(那个按本机时区算,
    会差一个时区)。
    """
    raw = hdr.get("DATE-OBS")
    if not raw:
        return None
    txt = str(raw).strip().rstrip("Zz")
    try:
        dt = datetime.fromisoformat(txt)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return jyear_from_unix(dt.timestamp())


# ------------------------------------------------------------------ 结果


@dataclass
class SolveResult:
    """一次解算的结果。``ok=False`` 时 :attr:`reason` 给出明确分类。

    关于 :attr:`rms_px` 的一个**必须知道的口径问题**:它统计的是**配对容差之内
    留下来的内点**。精拟合的容差会随迭代收紧到 ``4×RMS``,所以画面边缘那些被
    镜头畸变推开 4~6 px 的星根本不在统计里 —— 真机上本模块报 0.4~1.1 px,而
    同一张图和 ZWO 带 SIP 的解在全幅上的差异中位有 1.3~2.5 px。
    **RMS 是"中心区域拟合得多好",不是"这张图的畸变有多大",更不是成功判据**
    (判成功看 :attr:`n_match` 和 :attr:`log_fap`)。
    """

    ok: bool
    wcs: TanWcs | None = None
    reason: str = REASON_NO_MATCH
    message: str = ""
    n_match: int = 0
    rms_px: float = float("nan")
    log_fap: float = 0.0
    elapsed_s: float = 0.0
    level: int = -1                     # 命中的分级(0 = 第一级搜索半径)
    radius_deg: float = float("nan")    # 命中那一级的搜索半径
    n_stars: int = 0                    # 参与解算的图上星点数
    n_catalog: int = 0                  # 命中那一级用到的星表候选数
    candidates: int = 0                 # 累计试过多少候选变换
    tiles: int = 0                      # 累计试过多少个天区 tile
    match_radius_px: float = float("nan")
    hint_offset_deg: float = float("nan")   # 解出的中心离先验中心多远
    # 提星阶段的整帧形状统计。保留在解算结果里，供 FITS 查看器和导星质量
    # 交叉验证复用；否则 solve_file 返回后原始 StarList 已被释放。
    star_fwhm_px: float = float("nan")
    star_fwhm_arcsec: float = float("nan")
    star_ellipticity: float = float("nan")
    star_theta_deg: float = float("nan")
    star_theta_r: float = float("nan")
    matched_xy: np.ndarray | None = field(default=None, repr=False)
    matched_radec: np.ndarray | None = field(default=None, repr=False)
    hint: SolveHint | None = field(default=None, repr=False)

    # -------- 派生量(都从 wcs 现算,免得和 wcs 不同步)

    @property
    def center(self) -> tuple[float, float] | None:
        """图幅中心的天球坐标 ``(ra, dec)``,度。"""
        if self.wcs is None or not self.hint or not self.hint.image_size:
            return None
        w, h = self.hint.image_size
        ra, dec = pixel_to_world(self.wcs, (w + 1.0) / 2.0, (h + 1.0) / 2.0)
        return float(ra), float(dec)

    @property
    def pixel_scale(self) -> float | None:
        """角秒/像素。"""
        return None if self.wcs is None else self.wcs.pixel_scale()

    @property
    def rotation_deg(self) -> float | None:
        """图像 +y 轴的位置角(:meth:`astro_smb.wcs.TanWcs.rotation_deg`)。"""
        return None if self.wcs is None else self.wcs.rotation_deg()

    @property
    def zwo_angle_deg(self) -> float | None:
        """**ZWO 约定**的旋转角 —— 直接和日志 ``Angle = ...`` / 文件名对账。"""
        return None if self.wcs is None else zwo_angle_from_cd(self.wcs.cd)

    @property
    def flipped(self) -> bool | None:
        return None if self.wcs is None else self.wcs.flipped()

    def fits_cards(self) -> dict[str, str]:
        """解算结果 → 标准 FITS 关键字(不含 SIP)。未解出时返回空 dict。"""
        return {} if self.wcs is None else to_fits_cards(self.wcs)

    def __str__(self) -> str:
        if not self.ok or self.wcs is None:
            return _("解算失败({reason}): {message}").format(
                reason=_(REASON_TEXT.get(self.reason, self.reason)),
                message=self.message)
        c = self.center
        pos = "" if c is None else _(" 中心 {0:.5f}, {1:.5f}").format(c[0], c[1])
        return (_('解算成功:{n_match} 颗内点, RMS {rms_px:.2f} px, {0:.4f}"/px, ZWO 角 {zwo_angle_deg:.2f}°{pos} ({1:.0f} ms)').format(
            self.wcs.pixel_scale(), self.elapsed_s * 1000, n_match=self.n_match, rms_px=self.rms_px, zwo_angle_deg=self.zwo_angle_deg, pos=pos))


def _fail(reason: str, message: str, **kw) -> SolveResult:
    return SolveResult(ok=False, reason=reason, message=message, **kw)


# ------------------------------------------------------------------ 坐标换算


def fits_xy_from_stars(stars, height_full: int, *, binning: int = 1,
                       row_offset: float = 0.0) -> np.ndarray:
    """:class:`~astro_smb.stars.StarList`(或 ``(N,2)`` 数组坐标)→ FITS 像素。

    :param stars: :class:`~astro_smb.stars.StarList`,或 ``(N, 2)`` 的
        ``(x=列, y=行)`` **0-based 数组坐标**(y 向下,:mod:`astro_smb.stars`
        的输出约定)。
    :param height_full: **全分辨率** FITS ``NAXIS2``。
    :param binning: 提星平面相对全分辨率的缩小倍数(OSC 超像素平面 = 2)。
        超像素 ``(c, r)`` 的中心对应全分辨率的 ``(2c+0.5, 2r+0.5)``。
    :param row_offset: 提星平面在整幅里的行偏移(分带读取时用),单位是
        **提星平面的行**。
    :returns: ``(N, 2)`` FITS 像素坐标(1-based,y 自底向上)。
    """
    if isinstance(stars, _stars.StarList):
        cols = np.asarray(stars.x, dtype=np.float64)
        rows = np.asarray(stars.y, dtype=np.float64)
    else:
        a = np.asarray(stars, dtype=np.float64)
        if a.ndim != 2 or a.shape[1] != 2:
            raise SolveError(_("星点坐标必须是 (N, 2) 数组"))
        cols, rows = a[:, 0], a[:, 1]
    b = max(1, int(binning))
    half = (b - 1) * 0.5
    col_full = b * cols + half
    row_full = b * (rows + float(row_offset)) + half
    x, y = array_to_fits_xy(col_full, row_full, int(height_full))
    return np.column_stack([np.asarray(x, dtype=np.float64),
                            np.asarray(y, dtype=np.float64)])


def _stars_input(stars, flux, hint: SolveHint):
    """把 :func:`solve` 的星点入参归一成 ``(xy_fits, flux_or_None)``。"""
    if isinstance(stars, _stars.StarList):
        if not hint.image_size:
            raise SolveError(
                _("传 StarList 时 hint.image_size 必须给全分辨率尺寸,否则无法推断提星平面的缩放倍数"))
        w_full = int(hint.image_size[0])
        h_full = int(hint.image_size[1])
        h_plane, w_plane = stars.shape
        if w_plane <= 0 or h_plane <= 0:
            raise SolveError(_("StarList.shape 非法"))
        b = int(round(w_full / float(w_plane)))
        if b < 1 or abs(w_full - b * w_plane) > b or abs(h_full - b * h_plane) > b:
            raise SolveError(
                _("提星平面 {w_plane}×{h_plane} 与全分辨率 {w_full}×{h_full} 不是整数倍关系,请自己用 fits_xy_from_stars 换算后再传").format(
                    w_plane=w_plane, h_plane=h_plane, w_full=w_full, h_full=h_full))
        xy = fits_xy_from_stars(stars, h_full, binning=b)
        return xy, np.asarray(stars.flux, dtype=np.float64)
    a = np.asarray(stars, dtype=np.float64)
    if a.ndim != 2 or a.shape[1] != 2:
        raise SolveError(_("星点坐标必须是 (N, 2) 数组(FITS 像素约定)"))
    f = None if flux is None else np.asarray(flux, dtype=np.float64)
    if f is not None and f.size != a.shape[0]:
        raise SolveError(_("flux 长度与星点数不一致"))
    return a, f


# ------------------------------------------------------------------ 星表句柄

_default_cat: Catalog | None = None
_default_cat_lock = threading.Lock()


def default_catalog() -> Catalog:
    """进程内共享的默认星表(懒加载 + 缓存)。

    35.6 MB 的表每次解算都重新读一遍太亏(GUI 会逐帧解算)。
    :class:`~astro_smb.catalog.Catalog` 构造后只读,多线程共享安全。
    """
    global _default_cat
    with _default_cat_lock:
        if _default_cat is None:
            _default_cat = Catalog.open(catalog_path())
        return _default_cat


def close_default_catalog() -> None:
    """释放缓存的默认星表(单测/换表时用)。"""
    global _default_cat
    with _default_cat_lock:
        if _default_cat is not None:
            _default_cat.close()
            _default_cat = None


def _as_catalog(cat) -> Catalog:
    if cat is None:
        return default_catalog()
    if isinstance(cat, Catalog):
        return cat
    if isinstance(cat, (str, os.PathLike, Path)):
        return Catalog.open(cat)
    raise SolveError(_("catalog 必须是 Catalog 实例、路径或 None"))


# ------------------------------------------------------------------ tile


def _tile_centers(ra0: float, dec0: float, radius: float, tile_r: float,
                  max_tiles: int = 200) -> list[tuple[float, float]]:
    """把半径 ``radius`` 的搜索盘铺成若干半径 ``tile_r`` 的 tile 中心。

    环间距取 ``1.2·tile_r``,环上点距同量级 —— 相邻 tile 圆有重叠,盘内任意点
    到最近 tile 中心都 ≤ ``tile_r``(不留缝)。
    """
    if radius <= tile_r:
        return [(float(ra0) % 360.0, float(dec0))]
    x_hat, e_hat, n_hat = _basis3(ra0, dec0)
    out = [(float(ra0) % 360.0, float(dec0))]
    step = 1.2 * tile_r
    k = 1
    while True:
        d = k * step
        if d > radius + 0.2 * tile_r:
            break
        m = max(6, int(round(2.0 * math.pi * d / step)))
        pa = 2.0 * math.pi * np.arange(m) / m
        sd, cdd = math.sin(math.radians(d)), math.cos(math.radians(d))
        v = (x_hat * cdd
             + (n_hat * np.cos(pa)[:, None] + e_hat * np.sin(pa)[:, None]) * sd)
        ra, dec = unit_to_radec(v)
        out.extend(zip(np.asarray(ra).tolist(), np.asarray(dec).tolist()))
        if len(out) >= max_tiles:
            break
        k += 1
    return out[:max_tiles]


# ------------------------------------------------------------------ 求解器


class _Solver:
    """一次 :func:`solve` 调用的全部状态。拆成类只是为了别传十几个参数。"""

    def __init__(self, xy, flux, cat: Catalog, hint: SolveHint, cfg: dict):
        self.cat = cat
        self.hint = hint
        self.cfg = cfg
        self.cancel = cfg["cancel"]
        self.progress = cfg["progress"]
        self.t0 = time.perf_counter()
        self.deadline = self.t0 + float(cfg["time_budget_s"])
        self.candidates = 0
        self.tiles = 0
        self.best_hits = 0

        # 按亮度降序(没给 flux 就认为调用方已排好)
        if flux is not None and flux.size:
            order = np.argsort(-flux, kind="stable")
            xy = xy[order]
        self.xy_all = np.ascontiguousarray(xy, dtype=np.float64)
        self.n_image = min(int(cfg["n_image"]), len(self.xy_all))
        self.xy = self.xy_all[:self.n_image]
        self.z = self.xy[:, 0] + 1j * self.xy[:, 1]

        w, h = hint.image_size            # 调用方已保证非空
        self.width, self.height = float(w), float(h)
        self.center_px = complex((self.width + 1.0) / 2.0, (self.height + 1.0) / 2.0)
        self.diag = math.hypot(self.width, self.height)
        self.scale = hint.pixel_scale_arcsec()
        self.scale_deg = self.scale / 3600.0
        self.field_r = hint.field_radius_deg()
        self.field_area = hint.field_area_deg2()
        self.epoch = hint.epoch

        # 投票容差:基准值 + 播种模型的射影残差项(ρ³/3 弧度,折算成像素)
        tol = cfg["match_radius_px"]
        if tol is None:
            tol = min(30.0, max(3.0, 0.002 * self.diag))
        rho = math.radians(self.field_r)
        self.tol_px = float(max(tol, 0.35 * rho * rho * self.diag))
        self.tol_deg = self.tol_px * self.scale_deg

        self.scale_tol = float(cfg["scale_tol"] if cfg["scale_tol"] is not None
                               else hint.scale_tol)
        self.rot_tol = float(cfg["rotation_tol_deg"]
                             if cfg["rotation_tol_deg"] is not None
                             else hint.rotation_tol_deg)

    # -------------------------------------------------- 基础设施

    def _tick(self, stage: str, frac: float) -> None:
        _check_cancel(self.cancel)
        if self.progress is not None:
            try:
                self.progress(stage, float(min(1.0, max(0.0, frac))))
            except Exception:      # 回调是调用方的事,别让它搞崩解算
                pass

    def _out_of_time(self) -> bool:
        return time.perf_counter() > self.deadline

    # -------------------------------------------------- 图上星对

    def _image_pairs(self, n_seed: int):
        """最亮 ``n_seed`` 颗两两配对 → ``(ia, ib, 角距°)``,按亮度排序。

        角距用**精确的** gnomonic 关系算(不是 ``像素距 × 尺度``):
        视场半径 ρ 处切平面拉伸 1/cos²ρ,2° 视场只差 0.04% 无所谓,
        但换个 10° 视场的镜头就有 1%,直接把星对配对窗口顶穿。
        """
        n = min(int(n_seed), self.n_image)
        if n < 2:
            return (np.empty(0, np.int64),) * 2 + (np.empty(0),)
        ia, ib = np.triu_indices(n, k=1)
        dz = self.z[:n]
        xi = np.radians((dz.real - self.center_px.real) * self.scale_deg)
        eta = np.radians((dz.imag - self.center_px.imag) * self.scale_deg)
        norm = np.sqrt(1.0 + xi * xi + eta * eta)
        cos_t = ((1.0 + xi[ia] * xi[ib] + eta[ia] * eta[ib])
                 / (norm[ia] * norm[ib]))
        d = np.degrees(np.arccos(np.clip(cos_t, -1.0, 1.0)))
        lo = self.cfg["min_pair_frac"] * 2.0 * self.field_r
        keep = d >= lo
        if keep.sum() < 3:              # 星点挤在一起时放开下限,总比没有强
            keep = d > 0.0
        ia, ib, d = ia[keep], ib[keep], d[keep]
        # 排序按"较暗的那颗"优先:(0,1) (0,2) (1,2) (0,3) (1,3) (2,3) …
        # 也就是先把最亮的那几颗互相配完再往下扩。按 ia 排会把 19 个星对全押在
        # 第 0 颗上 —— 它要是热点/饱和斑/星表里没有,这 19 对就全废了。
        order = np.argsort(ib.astype(np.int64) * n + ia, kind="stable")
        return ia[order], ib[order], d[order]

    # -------------------------------------------------- 星表 tile

    def _load_tile(self, ra_c: float, dec_c: float, cone_r: float, n_cat: int):
        """查一个 tile 的星表候选,并预算好后面反复要用的量。

        返回 ``None`` 表示这个 tile 的星表覆盖不足。
        """
        idx = self.cat.cone(ra_c, dec_c, cone_r, epoch=self.epoch,
                            limit=int(n_cat))
        if idx.size < self.cfg["min_matches"]:
            return None
        ra, dec = self.cat.positions_at(self.epoch, idx)
        # 按星等升序 —— 候选排序时"越亮越可能是真对应"要用到名次
        mag = self.cat.vmag_milli[idx]
        order = np.argsort(mag, kind="stable")
        ra, dec = ra[order], dec[order]
        x_hat, e_hat, n_hat = _basis3(ra, dec)
        t_x, t_e, t_n = _basis3(ra_c, dec_c)
        # 切平面**背面**的星要先扔掉:cone_r 正常 ≤ 3.5×视场半径,远不到 90°,
        # 但真让一颗 90° 外的星混进来,除出来的 ±inf 会顺着 min/max 把整个网格
        # 的坐标原点污染成 inf,那时候整个 tile 一颗都配不上、而且不报错
        d = x_hat @ t_x
        front = d > 1e-6
        if int(front.sum()) < self.cfg["min_matches"]:
            return None
        if not bool(front.all()):
            ra, dec = ra[front], dec[front]
            x_hat, e_hat, n_hat = x_hat[front], e_hat[front], n_hat[front]
            d = d[front]
        # tile 切平面上的星表位置(度) —— 探针/投票都在这个平面上查网格
        cat_X = np.degrees((x_hat @ t_e) / d)
        cat_Y = np.degrees((x_hat @ t_n) / d)
        return {
            "ra": ra, "dec": dec, "n": int(ra.size),
            "xh": np.ascontiguousarray(x_hat),
            "eh": np.ascontiguousarray(e_hat),
            "nh": np.ascontiguousarray(n_hat),
            "tx": t_x, "te": t_e, "tn": t_n,
            "grid": _PointGrid(cat_X, cat_Y, self.tol_deg),
            "cone_r": float(cone_r), "center": (float(ra_c), float(dec_c)),
        }

    @staticmethod
    def _catalog_pairs(tile: dict, d_lo: float, d_hi: float, max_pairs: int):
        """tile 内所有角距落在 ``[d_lo, d_hi]`` 的星表星对,**按角距升序**。

        分块算 Gram 矩阵(不materialize N×N),用弦长比较避开 arccos 的开销;
        只在最后对留下来的对算一次真角距。
        """
        xh = tile["xh"]
        n = xh.shape[0]
        # 弦长 = 2 sin(θ/2) ⇒ 点积阈值(升序距离 ⇔ 降序点积)
        hi_dot = math.cos(math.radians(d_lo))
        lo_dot = math.cos(math.radians(d_hi))
        pa_l, pb_l, dot_l = [], [], []
        total = 0
        step = max(1, int(4_000_000 / max(1, n)))
        for i0 in range(0, n, step):
            i1 = min(n, i0 + step)
            g = xh[i0:i1] @ xh.T
            # 只取上三角(i < j),避免重复与自配对
            rows = np.arange(i0, i1)[:, None]
            cols = np.arange(n)[None, :]
            m = (cols > rows) & (g <= hi_dot) & (g >= lo_dot)
            if not m.any():
                continue
            ri, ci = np.nonzero(m)
            pa_l.append((ri + i0).astype(np.int32))
            pb_l.append(ci.astype(np.int32))
            dot_l.append(g[ri, ci])
            total += ri.size
            if total > max_pairs:
                break
        if not pa_l:
            return (np.empty(0, np.int32), np.empty(0, np.int32),
                    np.empty(0, np.float64))
        pa = np.concatenate(pa_l)
        pb = np.concatenate(pb_l)
        d = np.degrees(np.arccos(np.clip(np.concatenate(dot_l), -1.0, 1.0)))
        order = np.argsort(d, kind="stable")
        return pa[order], pb[order], d[order]

    # -------------------------------------------------- 投票

    def _vote(self, tile: dict, cand: dict, probe: np.ndarray):
        """把 ``probe`` 指定的图上星经每个候选变换投到 tile 切平面并查表。

        :returns: ``(hits, idx)``。``hits`` 是 ``(C,)`` 命中数;
            ``idx`` 是 ``(C, P)`` 命中的星表下标(-1 = 未命中)。
        """
        zi = self.z[cand["i"]]
        c = cand["c"]
        p = cand["p"]
        zp = self.z[probe]
        dz = zp[None, :] - zi[:, None]
        if cand["conj"]:
            dz = np.conj(dz)
        w = c[:, None] * dz
        xi = np.radians(w.real)
        eta = np.radians(w.imag)
        xh, eh, nh = tile["xh"], tile["eh"], tile["nh"]
        tx, te, tn = tile["tx"], tile["te"], tile["tn"]
        # v = x̂_p + ξ·ê_p + η·n̂_p(不必归一化:gnomonic 对 v 的模长不敏感)
        acc_d = np.zeros(xi.shape, dtype=np.float64)
        acc_e = np.zeros(xi.shape, dtype=np.float64)
        acc_n = np.zeros(xi.shape, dtype=np.float64)
        for k in range(3):
            vk = xh[p, k][:, None] + xi * eh[p, k][:, None] + eta * nh[p, k][:, None]
            acc_d += vk * tx[k]
            acc_e += vk * te[k]
            acc_n += vk * tn[k]
        with np.errstate(divide="ignore", invalid="ignore"):
            big_x = np.degrees(acc_e / acc_d)
            big_y = np.degrees(acc_n / acc_d)
        big_x = np.where(acc_d > 1e-9, big_x, np.nan)
        big_y = np.where(acc_d > 1e-9, big_y, np.nan)
        idx, _d2 = tile["grid"].query(big_x, big_y, self.tol_deg)
        return (idx >= 0).sum(axis=1), idx

    # -------------------------------------------------- 一个 tile 的尝试

    def _try_tile(self, tile: dict, tile_r: float, ia, ib, dimg):
        """在一个 tile 上跑完整的播种 + 剪枝 + 投票。命中返回内点对,否则 None。"""
        n_cat = tile["n"]
        if not len(ia):
            return None
        d_lo = float(dimg.min()) * (1.0 - self.scale_tol)
        d_hi = float(dimg.max()) * (1.0 + self.scale_tol)
        pa, pb, dcat = self._catalog_pairs(tile, d_lo, d_hi,
                                           self.cfg["max_catalog_pairs"])
        if pa.size == 0:
            return None
        self._tick(_("播种"), 0.0)

        # 星表星对的切平面偏移,**两个方向各算一遍**。
        # 千万别用 "反向 = -正向" 抄近路:两个切点的北方向差一个子午线收敛角
        # (≈ Δra·sin dec,2° 视场 @ dec 40° 就有 1.7°),那点角度误差外推到
        # 3700 px 的杠杆上就是 100 px —— 直接把投票全打飞。
        xh, eh, nh = tile["xh"], tile["eh"], tile["nh"]
        pair_z = _tangent_offset(xh, eh, nh, pa, pb)     # 以 pa 为切点看 pb
        pair_z_rev = _tangent_offset(xh, eh, nh, pb, pa)  # 以 pb 为切点看 pa

        probe = np.arange(min(int(self.cfg["n_probe"]), self.n_image))
        all_probe = np.arange(self.n_image)
        # conj=False ⇔ CD 行列式 > 0 ⇔ TanWcs.flipped() 为 True(ASIAIR 实测恒真)
        conj_modes = ([False, True] if self.hint.flipped is None
                      else [not bool(self.hint.flipped)])
        rot_hint = self.hint.rotation_deg if self.cfg["use_rotation"] else None

        budget = int(self.cfg["batch_candidates"])
        k0 = 0
        n_pairs = len(ia)
        while k0 < n_pairs:
            if self._out_of_time():
                return None
            # 攒够一批图星对再算,摊薄 numpy 的每次调用开销
            lo = np.searchsorted(dcat, dimg[k0:] * (1.0 - self.scale_tol), "left")
            hi = np.searchsorted(dcat, dimg[k0:] * (1.0 + self.scale_tol), "right")
            cnt = np.maximum(hi - lo, 0)
            take = int(np.searchsorted(np.cumsum(cnt) * 2 * len(conj_modes),
                                       budget, "right")) + 1
            take = max(1, min(take, n_pairs - k0))
            k1 = k0 + take
            self._tick(_("播种"), k0 / float(n_pairs))
            hit = self._try_pairs(tile, tile_r, ia[k0:k1], ib[k0:k1],
                                  lo[:take], cnt[:take], pa, pb,
                                  pair_z, pair_z_rev,
                                  probe, all_probe, conj_modes, rot_hint)
            if hit is not None:
                return hit
            k0 = k1
        return None

    def _try_pairs(self, tile, tile_r, ia, ib, lo, cnt, pa, pb,
                   pair_z, pair_z_rev, probe, all_probe, conj_modes, rot_hint):
        """一批图星对的候选:展开 → 剪枝 → 两级投票。"""
        total = int(cnt.sum())
        if total == 0:
            return None
        # 变长展开:每个图星对 k 取 dcat[lo[k] : lo[k]+cnt[k]]
        off = np.concatenate([[0], np.cumsum(cnt)])
        seed_k = np.repeat(np.arange(len(cnt), dtype=np.int64), cnt)
        seed_j = (np.arange(total, dtype=np.int64)
                  - np.repeat(off[:-1] - lo.astype(np.int64), cnt))

        # 两种对应关系:(i↔pa, j↔pb) 与 (i↔pb, j↔pa)
        img_i = np.concatenate([ia[seed_k], ib[seed_k]])
        img_j = np.concatenate([ib[seed_k], ia[seed_k]])
        cat_p = np.concatenate([pa[seed_j], pb[seed_j]])
        z_sky = np.concatenate([pair_z[seed_j], pair_z_rev[seed_j]])

        z_img = self.z[img_j] - self.z[img_i]
        for conj in conj_modes:
            c = z_sky / (np.conj(z_img) if conj else z_img)
            keep = np.isfinite(c)
            # ① 旋转先验(ZWO 约定的角度就是 arg(c) + 180°,两种宇称都成立)
            if rot_hint is not None:
                zwo = (np.degrees(np.angle(c)) + 180.0) % 360.0
                keep &= _angle_diff(zwo, rot_hint) <= self.rot_tol
            if not keep.any():
                continue
            ci, pi = c[keep], cat_p[keep]
            ii, jj = img_i[keep], img_j[keep]
            # ② 反推的视场中心必须落在这个 tile 的搜索半径内
            dz = self.center_px - self.z[ii]
            w = ci * (np.conj(dz) if conj else dz)
            xi = np.radians(w.real)
            eta = np.radians(w.imag)
            xh, eh, nh = tile["xh"], tile["eh"], tile["nh"]
            tx = tile["tx"]
            v0 = xh[pi, 0] + xi * eh[pi, 0] + eta * nh[pi, 0]
            v1 = xh[pi, 1] + xi * eh[pi, 1] + eta * nh[pi, 1]
            v2 = xh[pi, 2] + xi * eh[pi, 2] + eta * nh[pi, 2]
            cos_off = ((v0 * tx[0] + v1 * tx[1] + v2 * tx[2])
                       / np.sqrt(v0 * v0 + v1 * v1 + v2 * v2))
            lim = math.cos(math.radians(min(180.0, tile_r + 0.1 * self.field_r)))
            near = cos_off >= lim
            if not near.any():
                continue
            cand = {"c": ci[near], "p": pi[near], "i": ii[near],
                    "j": jj[near], "conj": conj}
            got = self._score(tile, cand, probe, all_probe)
            if got is not None:
                return got
        return None

    def _score(self, tile, cand, probe, all_probe):
        """两级投票:探针筛 → 完整投票 → 假阳率判据。"""
        n = int(cand["c"].size)
        if n == 0:
            return None
        chunk = max(1, int(self.cfg["vote_chunk"] // max(1, probe.size)))
        n_probe = probe.size
        min_probe = int(self.cfg["min_probe_hits"])
        for s0 in range(0, n, chunk):
            if self._out_of_time():
                return None
            _check_cancel(self.cancel)
            s1 = min(n, s0 + chunk)
            # 计数放在这里而不是剪枝之后:假阳率的 Bonferroni 分母应当是
            # **真正投过票的**变换数。放在剪枝处会把批次里因提前命中而根本没
            # 评估过的候选也算进去,分母虚高 ⇒ 判据被批大小左右
            self.candidates += s1 - s0
            sub = {k: (v[s0:s1] if isinstance(v, np.ndarray) else v)
                   for k, v in cand.items()}
            hits, _idx = self._vote(tile, sub, probe)
            # 星对自身那两颗**必然**命中(i 精确落在 p 上,j 近似落在 q 上),
            # 不算证据 —— 它们在探针集里时要减掉,否则 min_probe_hits 形同虚设
            trivial = ((sub["i"] < n_probe).astype(np.int64)
                       + (sub["j"] < n_probe).astype(np.int64))
            good = np.flatnonzero(hits - trivial >= min_probe)
            if good.size == 0:
                continue
            if good.size > _MAX_FULL_VOTE:      # 极端稠密天区的兜底
                good = good[np.argsort(-(hits - trivial)[good])[:_MAX_FULL_VOTE]]
            self.best_hits = max(self.best_hits, int(hits[good].max()))
            # 完整投票(候选很少了,直接一把算完)
            full = {k: (v[good] if isinstance(v, np.ndarray) else v)
                    for k, v in sub.items()}
            fh, fidx = self._vote(tile, full, all_probe)
            order = np.argsort(-fh)
            for t in order[:8]:
                nm = int(fh[t])
                self.best_hits = max(self.best_hits, nm)
                if nm < self.cfg["min_matches"]:
                    break
                if self._log_fap(nm, tile) > self.cfg["max_log_fap"]:
                    continue
                idx = fidx[t]
                sel = np.flatnonzero(idx >= 0)
                return {
                    "xy": self.xy[all_probe[sel]],
                    "cat": idx[sel].astype(np.int64),
                    "n_match": nm,
                    "log_fap": self._log_fap(nm, tile),
                }
        return None

    def _log_fap(self, n_match: int, tile: dict) -> float:
        """log10(假阳率),含已试候选数的 Bonferroni 修正。

        随机变换下,一颗图星落在某颗星表星 ``tol`` 内的概率 ≈
        ``n_cat·π·tol²/锥面积``;安全系数 ``fap_safety`` 兜住银道面附近的密度
        不均匀。**宁可保守** —— 假阳性解算会悄悄污染下游所有对账。
        """
        area = _cone_area_deg2(tile["cone_r"])
        if area <= 0.0:
            return 0.0
        p = tile["n"] * math.pi * self.tol_deg ** 2 / area
        mu = self.n_image * p * float(self.cfg["fap_safety"])
        return (_log10_poisson_tail(n_match, mu)
                + math.log10(max(1.0, float(self.candidates))))

    # -------------------------------------------------- 精拟合

    def _refine(self, seed: dict, tile: dict):
        """由种子内点做 TAN 拟合,再反复"重查表 → 重配对 → 重拟合"。"""
        xy = np.asarray(seed["xy"], dtype=np.float64)
        radec = np.column_stack([tile["ra"][seed["cat"]], tile["dec"][seed["cat"]]])
        # 同一颗星表星被两颗图星认领时只留最近的那对
        xy, radec = _dedupe_pairs(xy, radec, seed["cat"])
        if len(xy) < 4:
            return None, _("种子内点去重后不足 4 对")
        guess = (self.center_px.real, self.center_px.imag)
        # crpix_guess **必须**是图幅中心:切点离视场越远,"TAN + 线性 CD"
        # 这个参数化本身就装不下这个场,残差凭空长出来(wcs.fit_tan 有实测表)
        try:
            wcs, rms, _res, keep0 = fit_tan_sigma_clip(
                xy, radec, crpix_guess=guess, sigma=3.0, min_keep=4)
            xy, radec = xy[keep0], radec[keep0]
        except WcsError:
            try:
                wcs, rms, _r = fit_tan(xy, radec, guess)
            except WcsError as ex:
                return None, _("初拟合失败: {ex}").format(ex=ex)

        best = (wcs, rms, xy, radec)
        n_want = int(self.cfg["refine_catalog"])
        for it in range(int(self.cfg["refine_iters"])):
            _check_cancel(self.cancel)
            self._tick(_("精拟合"), (it + 1) / float(self.cfg["refine_iters"]))
            ra_c, dec_c = pixel_to_world(wcs, guess[0], guess[1])
            idx = self.cat.cone(float(ra_c), float(dec_c),
                                self.field_r * 1.05, epoch=self.epoch,
                                limit=n_want)
            if idx.size < 4:
                break
            cra, cdec = self.cat.positions_at(self.epoch, idx)
            px, py = world_to_pixel(wcs, cra, cdec)
            px = np.asarray(px, dtype=np.float64)
            py = np.asarray(py, dtype=np.float64)
            tol = max(2.0, min(self.tol_px, 4.0 * max(rms, 0.5)))
            inside = (np.isfinite(px) & np.isfinite(py)
                      & (px >= 0.5) & (px <= self.width + 0.5)
                      & (py >= 0.5) & (py <= self.height + 0.5))
            if inside.sum() < 4:
                break
            grid = _PointGrid(self.xy_all[:, 0], self.xy_all[:, 1], tol)
            mi, md2 = grid.query(px[inside], py[inside], tol)
            hit = mi >= 0
            if int(hit.sum()) < 4:
                break
            xy2 = self.xy_all[mi[hit]]
            radec2 = np.column_stack([cra[inside][hit], cdec[inside][hit]])
            xy2, radec2 = _dedupe_pairs(xy2, radec2, mi[hit], md2[hit])
            if len(xy2) < 4:
                break
            try:
                wcs2, rms2, _res, keep = fit_tan_sigma_clip(
                    xy2, radec2, crpix_guess=guess, sigma=3.0)
            except WcsError:
                break
            if int(keep.sum()) < 4:
                break
            wcs, rms = wcs2, rms2
            best = (wcs2, rms2, xy2[keep], radec2[keep])
        return best, ""

    # -------------------------------------------------- 主循环

    def run(self) -> SolveResult:
        n_stars = len(self.xy_all)
        base = dict(n_stars=n_stars, hint=self.hint,
                    match_radius_px=self.tol_px)
        radii = self.cfg["radii"]
        reason = REASON_NO_MATCH
        msg = _("试遍所有候选也没有一致的变换")
        saw_catalog = False

        # 旋转先验在**每一级内部**先试再放开 —— 反过来(先把三级都用错的先验
        # 跑一遍)会让一个填错的 <N>deg 把最贵的 15° 那级白跑一次
        rot_modes = ([True, False] if (self.hint.rotation_deg is not None)
                     else [False])
        for level, radius in enumerate(radii):
            n_seed = max(8, int(self.cfg["n_seed"] * (1.0 - 0.25 * level)))
            ia, ib, dimg = self._image_pairs(n_seed)
            tile_r = min(max(radius * 0.6, self.field_r), 2.5 * self.field_r)
            centers = _tile_centers(self.hint.ra_deg, self.hint.dec_deg,
                                    radius, tile_r, int(self.cfg["max_tiles"]))
            cone_r = tile_r + self.field_r
            n_cat = self._n_cat(cone_r)
            for use_rot in rot_modes:
                self.cfg["use_rotation"] = use_rot
                for ti, (ra_c, dec_c) in enumerate(centers):
                    if self._out_of_time():
                        return _fail(
                            REASON_TIMEOUT,
                            _("超过时间预算 {0:.0f}s").format(self.cfg['time_budget_s']),
                            elapsed_s=time.perf_counter() - self.t0,
                            candidates=self.candidates, tiles=self.tiles,
                            n_match=self.best_hits, **base)
                    self._tick(_("搜索 {radius:g}°").format(
                        radius=radius), ti / float(len(centers)))
                    tile = self._load_tile(ra_c, dec_c, cone_r, n_cat)
                    if tile is None:
                        continue
                    saw_catalog = True
                    self.tiles += 1
                    seed = self._try_tile(tile, tile_r, ia, ib, dimg)
                    if seed is None:
                        continue
                    got, why = self._refine(seed, tile)
                    if got is None:
                        reason, msg = REASON_BAD_FIT, why
                        continue
                    wcs, rms, mxy, mradec = got
                    cap = self.cfg["max_rms_px"]
                    if cap is None:
                        cap = max(4.0, 0.001 * self.diag)
                    if not math.isfinite(rms) or rms > cap:
                        reason = REASON_BAD_FIT
                        msg = _("残差 RMS {rms:.2f} px 超过上限 {cap:.2f} px").format(
                            rms=rms, cap=cap)
                        continue
                    if len(mxy) < self.cfg["min_matches"]:
                        reason = REASON_BAD_FIT
                        msg = _("精拟合后内点只剩 {0} 对").format(len(mxy))
                        continue
                    return self._success(wcs, rms, mxy, mradec, seed,
                                         level, radius, tile, base)
        if not saw_catalog:
            reason = REASON_NO_CATALOG
            msg = _("星表在搜索范围内的星数不足,换更亮的星等上限或检查星表是否完整")
        return _fail(reason, msg,
                     elapsed_s=time.perf_counter() - self.t0,
                     candidates=self.candidates, tiles=self.tiles,
                     n_match=self.best_hits, **base)

    def _n_cat(self, cone_r: float) -> int:
        return _adaptive_catalog_count(
            self.n_image, cone_r, self.field_area,
            float(self.cfg["cat_margin"]), int(self.cfg["min_matches"]) * 4,
            int(self.cfg["max_catalog"]))

    def _success(self, wcs, rms, mxy, mradec, seed, level, radius, tile, base):
        ra_c, dec_c = pixel_to_world(wcs, self.center_px.real,
                                     self.center_px.imag)
        off = float(angular_separation(self.hint.ra_deg, self.hint.dec_deg,
                                       ra_c, dec_c))
        return SolveResult(
            ok=True, wcs=wcs, reason=REASON_OK,
            message=_("内点 {0} 对,残差 {rms:.2f} px").format(len(mxy), rms=rms),
            n_match=len(mxy), rms_px=float(rms),
            log_fap=float(seed["log_fap"]),
            elapsed_s=time.perf_counter() - self.t0,
            level=level, radius_deg=float(radius),
            n_catalog=int(tile["n"]), candidates=self.candidates,
            tiles=self.tiles, hint_offset_deg=off,
            matched_xy=mxy, matched_radec=mradec, **base)


def _dedupe_pairs(xy: np.ndarray, radec: np.ndarray, key: np.ndarray,
                  d2: np.ndarray | None = None):
    """同一个 ``key``(星表下标 / 图星下标)只保留一对。

    给了 ``d2`` 就保留**最近**的那一对(星表里的密近双星会让两颗表星抢同一颗
    图星,留错了等于给拟合塞进一个 1 个容差量级的系统偏差);没有距离信息时
    按出现顺序保留第一个。
    """
    k = np.asarray(key)
    if d2 is None:
        order = np.arange(k.size)
    else:
        order = np.argsort(np.asarray(d2), kind="stable")
    _u, first = np.unique(k[order], return_index=True)
    sel = np.sort(order[first])
    return xy[sel], radec[sel]


# ------------------------------------------------------------------ 公开入口


def _solve_blind(stars, catalog, hint: SolveHint, *, flux=None,
                 min_matches: int = 8, cancel=None, progress=None,
                 max_tiles: int = 400,
                 tuning: dict | None = None) -> SolveResult:
    """没有指向先验时的盲解:按视场铺一张覆盖全天的网格,逐格当先验去试。

    **尺度必须已知**(焦距+像元,或直接给 pixel_scale)—— 否则相似变换的尺度
    自由度回来了,两颗星不再唯一确定变换,成对播种那套的前提就没了。

    搜索计划来自 :func:`astro_smb.wcsapps.blind_hint_grid`(它保证任意天球点都
    落在某个格心的搜索半径内)。这里只负责**顺序试**并把首个可信解返回 ——
    那个函数的 docstring 一直写着"交给上层顺序去试",而上层此前不存在。

    判成功仍看 :attr:`SolveResult.n_match` 与 :attr:`SolveResult.log_fap`:
    盲解最怕的不是解不出来,是**自信地给出错误答案**,所以判据一个都不放松。
    """
    from astro_smb import wcsapps

    scale = hint.pixel_scale_arcsec()
    plan = wcsapps.blind_hint_grid(scale, hint.image_size, base_hint=hint)
    full = len(plan)
    truncated = full > max_tiles
    if truncated:
        plan = plan[:max_tiles]
    total = len(plan)
    frac = total / full if full else 1.0

    # **覆盖率必须写进失败消息**。格点盲解对窄视场本就不是对的算法:
    # 0.65°×0.43° 的视场全天要 137672 格,按每格约 70ms 是 2.7 小时。
    # 默认预算只够搜 0.3% —— 这时候报"搜过了没找到"是**误导**,
    # 用户会以为图有问题,实际是我们压根没搜。
    # (真正的全天盲解要靠四星几何哈希做索引,astrometry.net 就是为此;
    #  本项目刻意没走那条路,因为正常流程总有指向先验。)
    if truncated:
        note = (_('盲解:只搜了 {total}/{full} 个天区(全天的 {frac:.1%})就用完预算 —— **没搜到不代表解不出来**。这个视场({scale:.2f}"/px)全天格点搜索要约 {0:.0f} 分钟;请给个大致指向,或放宽 blind_max_tiles').format(
            full * 0.07 / 60, total=total, full=full, frac=frac, scale=scale))
    else:
        note = _("盲解:全天 {total} 个天区都没找到一致变换").format(total=total)
    best = _fail(REASON_NO_MATCH, note, hint=hint)
    for i, tile in enumerate(plan):
        _check_cancel(cancel)
        if progress:
            progress(_("盲解 {0}/{total}").format(
                i + 1, total=total), (i + 1) / max(1, total))
        res = solve(stars, catalog, tile, flux=flux, min_matches=min_matches,
                    cancel=cancel, blind=False, **(tuning or {}))
        if res.ok:
            res.message = (_("盲解:试到第 {0}/{total} 个天区命中").format(i + 1, total=total)
                           + (f"({res.message})" if res.message else ""))
            return res
        if res.n_match > best.n_match:
            best = res
    best.n_stars = best.n_stars or 0
    return best


def solve(stars, catalog=None, hint: SolveHint | None = None, *,
          flux=None,
          n_seed: int = 20, n_image: int = 60, n_probe: int = 10,
          min_probe_hits: int = 2,
          cat_margin: float = 1.5, max_catalog: int = 12000,
          match_radius_px: float | None = None,
          min_matches: int = 8, max_log_fap: float = -6.0,
          max_rms_px: float | None = None,
          radii=DEFAULT_RADII,
          scale_tol: float | None = None,
          rotation_tol_deg: float | None = None,
          min_pair_frac: float = 0.25,
          refine_iters: int = 3, refine_catalog: int = 600,
          fap_safety: float = 3.0,
          max_tiles: int = 200, max_catalog_pairs: int = 4_000_000,
          batch_candidates: int = 200_000, vote_chunk: int = 500_000,
          time_budget_s: float = 60.0,
          blind: bool = True,
          blind_max_tiles: int = 400,
          cancel: threading.Event | None = None,
          progress=None) -> SolveResult:
    """约束板解算:星点 + 星表 + 先验 → :class:`SolveResult`。

    :param stars: :class:`~astro_smb.stars.StarList`(自动按
        ``hint.image_size`` 反推缩放并换算坐标),或 ``(N, 2)`` 的
        **FITS 像素坐标**(1-based、y 自底向上,见模块 docstring)。
    :param catalog: :class:`~astro_smb.catalog.Catalog` / 路径 / ``None``
        (用 :func:`default_catalog`)。
    :param hint: 先验;至少要有指向和尺度,以及 ``image_size``。
    :param flux: ``stars`` 是裸数组时的亮度(用于排序);不给就认为已按亮度降序。

    :param n_seed: 参与**播种**的最亮星数(星对数 = n_seed²/2)。搜索半径越大
        会自动调小(候选爆炸)。
    :param n_image: 参与**投票**的最亮星数。
    :param n_probe: 廉价探针筛用几颗星。这一级把每个候选的代价从 ``n_image``
        降到 ``n_probe``,是整个算法能跑到毫秒级的关键。
    :param min_probe_hits: 探针筛的门槛(**已扣掉星对自身的平凡命中**)。
    :param cat_margin: 星表候选数的安全系数(图上最亮的星未必是星表最亮的)。
    :param max_catalog: 星表候选数上限(内存/时间兜底)。
    :param match_radius_px: 投票配对半径(全分辨率像素)。``None`` 自适应:
        ``clip(0.002×对角线, 3, 30)``,再按视场半径的射影残差项放宽。
    :param min_matches: 接受一个解至少要多少内点。
    :param max_log_fap: 接受阈值:``log10(假阳率) ≤ 该值``(已含候选数的
        Bonferroni)。**调大 = 更容易出假阳性解,慎动**。
    :param max_rms_px: 精拟合残差上限;``None`` 用 ``max(4, 0.001×对角线)``。
        真机纯 TAN 的畸变地板就有 1.3~1.7 px,别卡太紧。
    :param radii: 分级搜索半径(度)。
    :param min_pair_frac: 播种星对的最短基线(视场直径的比例)。太短的基线
        外推到全场会把误差放大 ``对角线/基线`` 倍。
    :param time_budget_s: 总时间预算,超了返回 :data:`REASON_TIMEOUT`。
    :param cancel: ``threading.Event``;置位时抛 :class:`InterruptedError`
        (与 :mod:`astro_smb.stars` 的约定一致)。
    :param progress: ``progress(stage: str, frac: float)``,frac ∈ [0, 1]。

    :raises SolveError: 入参本身非法(形状/类型/星表打不开)。
    :raises InterruptedError: ``cancel`` 置位。
    """
    if hint is None:
        return _fail(REASON_NO_HINT, _("没有给先验(SolveHint)"))
    if not hint.has_pointing:
        # **尺度已知但不知道指向 ⇒ 盲解**:按视场大小铺一张覆盖全天的搜索网格,
        # 逐个当作指向先验去试。尺度未知则无从铺网格(相似变换的尺度自由度回来了,
        # 两颗星不再唯一确定变换),那才是真的没救。
        if blind and hint.pixel_scale_arcsec() is not None and hint.image_size:
            return _solve_blind(
                stars, catalog, hint, flux=flux, min_matches=min_matches,
                cancel=cancel, progress=progress, max_tiles=blind_max_tiles,
                tuning=dict(
                    n_seed=n_seed, n_image=n_image, n_probe=n_probe,
                    min_probe_hits=min_probe_hits, cat_margin=cat_margin,
                    max_catalog=max_catalog, match_radius_px=match_radius_px,
                    max_log_fap=max_log_fap, max_rms_px=max_rms_px,
                    scale_tol=scale_tol, rotation_tol_deg=rotation_tol_deg,
                    min_pair_frac=min_pair_frac, refine_iters=refine_iters,
                    refine_catalog=refine_catalog, fap_safety=fap_safety,
                    max_tiles=max_tiles, max_catalog_pairs=max_catalog_pairs,
                    batch_candidates=batch_candidates, vote_chunk=vote_chunk,
                    time_budget_s=time_budget_s))
        return _fail(REASON_NO_HINT, _("先验里没有指向(ra/dec)"), hint=hint)
    if hint.pixel_scale_arcsec() is None:
        return _fail(REASON_NO_HINT,
                     _("先验里没有尺度(焦距 + 像元,或直接给 pixel_scale)"),
                     hint=hint)
    if not hint.image_size or hint.field_radius_deg() is None:
        return _fail(REASON_NO_HINT, _("先验里没有(或没有合法的)图幅尺寸 image_size"),
                     hint=hint)

    xy, fl = _stars_input(stars, flux, hint)
    n_stars = len(xy)
    need = max(4, int(min_matches))
    if n_stars < need:
        # 星点数 < min_matches 时**不可能**攒够内点,早点报明确的理由,
        # 别让调用方拿到一个含糊的"找不到一致变换"
        return _fail(REASON_FEW_STARS,
                     _("只提到 {n_stars} 颗星,至少需要 {need} 颗").format(n_stars=n_stars, need=need),
                     n_stars=n_stars, hint=hint)
    if not np.all(np.isfinite(xy)):
        raise SolveError(_("星点坐标含 NaN/Inf"))

    cat = _as_catalog(catalog)
    radii = tuple(float(r) for r in radii)
    if not radii:
        raise SolveError(_("radii 不能为空"))
    if hint.radius_deg is not None:
        radii = (float(hint.radius_deg),) + tuple(
            r for r in radii if r > float(hint.radius_deg))

    cfg = dict(
        n_seed=int(n_seed), n_image=int(n_image), n_probe=int(n_probe),
        min_probe_hits=int(min_probe_hits),
        cat_margin=float(cat_margin), max_catalog=int(max_catalog),
        match_radius_px=match_radius_px, min_matches=int(min_matches),
        max_log_fap=float(max_log_fap), max_rms_px=max_rms_px,
        radii=radii, scale_tol=scale_tol, rotation_tol_deg=rotation_tol_deg,
        min_pair_frac=float(min_pair_frac),
        refine_iters=int(refine_iters), refine_catalog=int(refine_catalog),
        fap_safety=float(fap_safety), max_tiles=int(max_tiles),
        max_catalog_pairs=int(max_catalog_pairs),
        batch_candidates=int(batch_candidates), vote_chunk=int(vote_chunk),
        time_budget_s=float(time_budget_s), cancel=cancel, progress=progress,
        use_rotation=False,
    )
    return _Solver(xy, fl, cat, hint, cfg).run()


# ------------------------------------------------------------------ 文件入口


#: 一次 SMB/磁盘往返就把 FITS 头读全(真机有 WCS 的头 8640 B、没有的 5760 B)
HEADER_PROBE_BYTES = 16384


def _read_header(src) -> tuple[FitsHeader, bytes | None]:
    if isinstance(src, (bytes, bytearray, memoryview)):
        buf = bytes(src)
        return parse_fits_header(buf), buf
    path = Path(src)
    with open(path, "rb") as fh:
        probe = fh.read(HEADER_PROBE_BYTES)
        hdr = parse_fits_header(probe)
        need = header_read_hint(probe)
        while need > 0 and not hdr.complete:
            probe += fh.read(need)
            hdr = parse_fits_header(probe)
            nxt = header_read_hint(probe)
            if nxt >= need:                 # 不再增长 ⇒ 文件就这么大
                break
            need = nxt
    return hdr, None


def _band_rows(height: int, read_fraction: float | None, n_bands: int,
               unit: int, min_band: int) -> list[tuple[int, int]]:
    """把要读的行分成若干**均匀撒开**的带 → ``[(y0, y1), ...]``(显示行序)。

    为什么是"多条带"而不是"中间一块":板解算的 CD 精度取决于星点在**两个方向
    上的基线**。只读中间 1/3 会把 y 方向基线砍掉 2/3,旋转角和 y 尺度的误差
    直接放大 3 倍。分散成 3 条带能保住全幅基线,读的字节数一样。

    ``unit`` 是行对齐粒度(OSC 超像素 = 2);``min_band`` 是单带最少行数
    (背景估计要至少两块高,否则整条带的背景全靠外推)。
    """
    h = int(height)
    if not read_fraction or read_fraction >= 1.0:
        return [(0, h)]
    frac = max(0.02, min(1.0, float(read_fraction)))
    nb = max(1, int(n_bands))
    rows = int(h * frac)
    while nb > 1 and rows // nb < min_band:
        nb -= 1
    band = max(min_band, rows // nb)
    band = (band // unit) * unit
    if band <= 0 or band * nb >= h:
        return [(0, h)]
    out = []
    gap = (h - band * nb) // max(1, nb)
    y = gap // 2
    for _i in range(nb):
        y0 = (min(max(0, y), h - band) // unit) * unit
        out.append((y0, y0 + band))
        y += band + gap
    return out


def _band_reader(src, geom: FitsGeometry, cancel):
    """返回 ``read(y0, y1) -> (H', W)`` —— 按**显示行**区间取像素。

    只有 ASIAIR 主路径(BITPIX 16 + BZERO 32768 + BSCALE 1 + 单平面 + 路径输入)
    能真正少读字节:``np.fromfile(offset=…)`` 直接定位到那几行。其余情况
    (字节输入、浮点数据、彩色立方体)退回 :func:`decode_pixels` **读一次整幅**
    再切片 —— 分带的收益本来就是网络/磁盘字节数,为了边角格式手写字节序转换
    不划算,而按带反复整幅解码更是灾难。
    """
    fast = (not isinstance(src, (bytes, bytearray, memoryview))
            and geom.planes == 1 and geom.bitpix == 16
            and geom.bscale == 1.0 and geom.bzero == 32768.0)
    if not fast:
        cache: list = []

        def read_slow(y0: int, y1: int):
            if not cache:
                cache.append(decode_pixels(src, geom, cancel=cancel))
            return cache[0][y0:y1]
        return read_slow

    h, w = geom.height, geom.width

    def read_fast(y0: int, y1: int):
        if cancel is not None and cancel.is_set():
            raise InterruptedError(_("解算已取消"))
        # 显示行 [y0, y1) ↔ 存储行 [h-y1, h-y0)(ASIAIR 从不写 ROWORDER ⇒ 恒翻转)
        r0, r1 = (h - y1, h - y0) if geom.flip_vertical else (y0, y1)
        want = (r1 - r0) * w
        raw = np.fromfile(Path(src), dtype=np.dtype(">u2"), count=want,
                          offset=geom.data_offset + r0 * w * 2)
        if raw.size != want:
            raise FitsImageError(
                _("数据区不足:想读 {want} 个像素,只有 {size}").format(want=want, size=raw.size))
        # fromfile 返回独占数组:原地换成本机字节序并复用同一块内存，避免
        # astype + 异或各复制整条带（大图分带时曾把峰值内存放大到约 3 倍）。
        raw.byteswap(inplace=True)
        out = raw.view(np.uint16).reshape(r1 - r0, w)
        # int16 + BZERO 32768 ⇒ 无符号解释下就是异或最高位(等价、零分支)
        out ^= np.uint16(0x8000)
        return out[::-1] if geom.flip_vertical else out
    return read_fast


def solve_file(src, *, catalog=None, hint: SolveHint | None = None,
               name: str | None = None,
               read_fraction: float | None = None, n_bands: int = 3,
               threshold: float = 5.0, max_stars: int = 400,
               detect_kw: dict | None = None,
               cancel: threading.Event | None = None,
               progress=None, **solve_kw) -> SolveResult:
    """从 FITS 文件(或字节)一路解到 WCS:读头 → 读像素 → 提星 → 解算。

    :param src: 文件路径,或整份 FITS 字节。
    :param hint: 不给就用 :meth:`SolveHint.from_header` 从头 + 文件名装配。
    :param name: 文件名(取旋转先验用);``src`` 是路径时默认取它的 basename。
    :param read_fraction: **只读一部分像素**(0~1)。``None`` = 整幅。
    :param n_bands: 分几条带读(见 :func:`_band_rows` 的说明)。

    只读部分数据的取舍
    ~~~~~~~~~~~~~~~~~~

    解算只需要几十颗星,而一张 ASIAIR 原图 52 MB —— 走 SMB 是 9 秒。
    本函数支持只读**若干条均匀撒开的行带**:

    * 星点数按比例减少(``read_fraction=0.3`` ⇒ 约 30% 的星)。实测 19 颗
      100% 成功、11 颗掉到 77%,所以只在原本星点富余时才值得开;
    * **几何基线不损失** —— 这正是分带而不是"读中间一块"的原因;
    * 带边界会被 ``drop_edge`` 吃掉几颗星(每条带上下各一行);
    * 背景是逐带估的,天光梯度跨带不连续无所谓(反正逐块估);
    * 只对 ASIAIR 主路径(BITPIX 16 / BZERO 32768 / 单平面 / 路径输入)真正
      少读字节,其他格式会退回整幅读取(结果一样,只是没省 I/O)。

    :raises SolveError: 入参非法。
    :raises FitsImageError: FITS 头/数据区有问题。
    :raises InterruptedError: ``cancel`` 置位。
    """
    def tick(stage, frac):
        if cancel is not None and cancel.is_set():
            raise InterruptedError(_("解算已取消"))
        if progress is not None:
            try:
                progress(stage, float(frac))
            except Exception:
                pass

    tick(_("读取文件头"), 0.0)
    hdr, buf = _read_header(src)
    if not hdr.complete:
        raise FitsImageError(_("FITS 头不完整(没读到 END 卡)"))
    geom = geometry_from_header(hdr)
    if name is None and not isinstance(src, (bytes, bytearray, memoryview)):
        name = os.path.basename(str(src))
    if hint is None:
        hint = SolveHint.from_header(hdr, name=name)
    if not hint.image_size:
        hint = replace(hint, image_size=(geom.width, geom.height))

    osc = geom.planes == 1 and geom.bayer_effective is not None
    binning = 2 if osc else 1
    unit = 2 if osc else 1
    min_band = 256 if osc else 128
    bands = _band_rows(geom.height, read_fraction, n_bands, unit, min_band)
    source = buf if buf is not None else src
    read = _band_reader(source, geom, cancel)

    kw = dict(threshold=float(threshold), max_stars=int(max_stars),
              pixel_scale=None, cancel=cancel)
    if detect_kw:
        kw.update(detect_kw)
    scale = hint.pixel_scale_arcsec()
    if scale is not None and kw.get("pixel_scale") is None:
        kw["pixel_scale"] = scale * binning
    if geom.planes == 3 and "channel" not in kw:
        kw["channel"] = 1                       # 彩色立方体取绿通道

    xy_l, flux_l = [], []
    fwhm_l, ellipticity_l, theta_l = [], [], []
    for bi, (y0, y1) in enumerate(bands):
        tick(_("提取星点"), bi / float(len(bands)))
        plane = read(y0, y1)
        if osc:
            plane = _stars.green_superpixel(plane, geom.bayer_effective)
            row_off = y0 // 2
        else:
            row_off = y0
        found = _stars.detect_stars(plane, **kw)
        if len(found):
            xy_l.append(fits_xy_from_stars(found, geom.height, binning=binning,
                                           row_offset=row_off))
            flux_l.append(np.asarray(found.flux, dtype=np.float64))
            # OSC 的 found 坐标在 2x2 绿超像素平面；FWHM 要还原成全分辨率
            # 像素，角秒值才可与 SolveResult.pixel_scale 对上。
            fwhm_l.append(np.asarray(found.fwhm, dtype=np.float64) * binning)
            ellipticity_l.append(
                np.asarray(found.ellipticity, dtype=np.float64))
            theta_l.append(np.asarray(found.theta, dtype=np.float64))
        del plane, found

    if not xy_l:
        return _fail(REASON_FEW_STARS, _("整幅图上没提到任何星点"), hint=hint)
    xy = np.concatenate(xy_l)
    fl = np.concatenate(flux_l)
    if xy.shape[0] > max_stars:
        pick = np.argsort(-fl, kind="stable")[:max_stars]
        xy, fl = xy[pick], fl[pick]
    tick(_("解算"), 0.0)
    result = solve(xy, catalog, hint, flux=fl, cancel=cancel,
                   progress=progress, **solve_kw)
    fw = np.concatenate(fwhm_l)
    el = np.concatenate(ellipticity_l)
    th = np.concatenate(theta_l)
    good_fw = fw[np.isfinite(fw) & (fw > 0)]
    result.star_fwhm_px = (float(np.median(good_fw)) if good_fw.size
                           else float("nan"))
    result.star_ellipticity = (float(np.median(el[np.isfinite(el)]))
                               if np.any(np.isfinite(el)) else float("nan"))
    good_th = th[np.isfinite(th)]
    if good_th.size:
        doubled = np.radians(2.0 * good_th)
        cs = float(np.mean(np.cos(doubled)))
        sn = float(np.mean(np.sin(doubled)))
        result.star_theta_deg = (
            math.degrees(0.5 * math.atan2(sn, cs)) % 180.0)
        result.star_theta_r = math.hypot(cs, sn)
    # 成功时优先用解算出的实际尺度；失败结果没有 WCS 才退回头部先验。
    scale_full = result.pixel_scale or hint.pixel_scale_arcsec()
    if scale_full is not None and math.isfinite(result.star_fwhm_px):
        result.star_fwhm_arcsec = result.star_fwhm_px * scale_full
    return result
