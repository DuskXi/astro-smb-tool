"""WCS 应用层:板解算拿到 WCS **之后**能算出来的东西(纯 numpy)。

:mod:`astro_smb.wcs` 负责"一张图 ↔ 天球"的几何,:mod:`astro_smb.platesolve`
负责"把 WCS 解出来"。本模块负责第三件事:**一堆 WCS 摆在一起能回答什么问题**。

八件事,按依赖顺序:

1. :func:`footprint` —— 一张图的视场足迹(天球多边形)。
2. :func:`footprint_area_deg2` —— 球面多边形面积。
3. :func:`coverage` —— 一组足迹的并集覆盖 / 公共交集 / 缺口 / 覆盖张数图。
4. :func:`pointing_error` —— 解出的中心离命令位置差多少(goto 精度)。
5. :func:`field_rotation` —— 位置角随时间的漂移速率(**极轴误差的直接观测量**)。
6. :func:`drift` —— 帧中心随时间的漂移(可扣掉已知 dither)。
7. :func:`stack_alignment` —— 各帧对齐到参考帧需要多少平移/旋转/缩放。
8. :func:`sky_grid` / :func:`blind_hint_grid` —— 盲解兜底的**搜索计划**
   (只产计划,解算是 :mod:`~astro_smb.platesolve` 的事)。

约定(**看清楚再用**)
--------------------

* **角度单位写在名字里**:``*_deg`` 度、``*_arcsec`` 角秒、``*_deg2`` 平方度、
  ``*_px`` 像素、``*_s`` 秒、``*_hours`` 小时。没后缀的 ``ra`` / ``dec`` 一律是度。
* **像素坐标一律 FITS 1-based**(和 :mod:`astro_smb.wcs` 一致);图幅中心是
  ``((W+1)/2, (H+1)/2)``,外边界是 ``0.5`` 与 ``N+0.5``。
* **"RA 分量"永远是东向大圆分量(已乘 cos δ)**,不是 ΔRA 本身 —— 否则赤纬高的
  目标数字会凭空放大 1/cos δ 倍。
* 时刻可以给 ``float``(unix 秒或任何统一起点的秒)或 ``datetime``;
  :func:`field_rotation` / :func:`drift` 内部**会按时刻重新排序**。

gnomonic 的两个性质(本模块反复用到,别当成近似)
------------------------------------------------

* **大圆在 gnomonic 平面上是直线**。图幅四条边在自己的切平面里是直线 ⇒ 对应天球上
  的四段大圆 ⇒ 投到**任何**别的 gnomonic 平面上**还是直线**。所以
  :func:`coverage` 把所有足迹投到同一个公共切平面后,多边形判内外用直线段的
  crossing number 是**精确**的,不是"小视场近似"。
* 面积元 ``dΩ = dξ·dη / (1 + ξ² + η²)^{3/2}``(ξ/η 是切平面坐标 = 角距的正切)。
  :func:`coverage` 的每个格子都按这个权重折算成平方度,所以大视场边缘不会高估。

退化输入
--------

空序列 / 单张 / 时刻全相同 / 共线 —— **都不抛异常**,返回 ``n=0`` 或速率为 ``nan``
的结果对象(UI 里"这一夜只解出 1 张"太常见了,不该炸)。只有**输入本身有毛病**
(形状不对、时刻个数对不上、参数超范围、足迹散布到半个天球)才抛
:class:`WcsAppsError`。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime
from typing import NamedTuple

import numpy as np

from .platesolve import SolveHint
from .wcs import (TanWcs, angular_separation, pixel_to_world, radec_to_unit,
                  unit_to_radec, world_to_pixel)
from astro_smb.i18n import gettext as _

__all__ = [
    "WcsAppsError",
    "Footprint", "footprint", "footprint_area_deg2",
    "Coverage", "coverage",
    "PointingError", "pointing_error",
    "FieldRotation", "field_rotation",
    "Drift", "drift",
    "FrameAlignment", "StackAlignment", "stack_alignment",
    "sky_grid", "blind_hint_grid",
]

_DEG2_PER_SR = (180.0 / math.pi) ** 2
_TWO_PI = 2.0 * math.pi
_FOUR_PI = 4.0 * math.pi


class WcsAppsError(ValueError):
    """输入非法:形状不对、时刻个数对不上、参数超范围、足迹散布过大。

    **区别于退化输入** —— 空序列/单张/共线这些是正常业务情形,返回带 ``nan``
    的结果对象,不抛异常。
    """


# ------------------------------------------------------------------ 小工具


def _wrap180(d):
    """角度差折叠到 ``[-180, 180)``。向量化。"""
    return (np.asarray(d, dtype=np.float64) + 180.0) % 360.0 - 180.0


def _as_seconds(t) -> float:
    """时刻 → 秒(float)。接受 ``datetime`` 或数值。"""
    if isinstance(t, datetime):
        return float(t.timestamp())
    try:
        v = float(t)
    except (TypeError, ValueError) as exc:
        raise WcsAppsError(_("时刻必须是数值秒或 datetime,给的是 %r") % (t,)) from exc
    if not math.isfinite(v):
        raise WcsAppsError(_("时刻含 NaN/Inf"))
    return v


def _tangent_basis(ra_deg: float, dec_deg: float):
    """切点处的正交基 ``(x̂ 指向切点, ê 东, n̂ 北)``。极点处依然良定义。

    与 :mod:`astro_smb.wcs` 内部用的是同一套基(单测
    ``TestTangentPlaneMatchesWcs`` 用 :func:`~astro_smb.wcs.world_to_pixel`
    交叉钉死),这里另写一份只是为了不依赖别人的私有函数。
    """
    ra = math.radians(float(ra_deg))
    dec = math.radians(float(dec_deg))
    ca, sa = math.cos(ra), math.sin(ra)
    cd, sd = math.cos(dec), math.sin(dec)
    return (np.array([cd * ca, cd * sa, sd]),
            np.array([-sa, ca, 0.0]),
            np.array([-sd * ca, -sd * sa, cd]))


#: 切平面正面判据(与 :mod:`astro_smb.wcs` 的 ``_MIN_COS`` 同一量级)
_MIN_COS = 1e-12


def _project_tangent(center, ra, dec):
    """(ra, dec) → 以 ``center`` 为切点的 gnomonic 坐标 ``(ξ, η)``,**度**。

    第三个返回值 ``ok`` 标记该点是否在切平面正面(夹角 < 90°);背面的
    ξ/η 是垃圾值,调用方必须自己挡掉。
    """
    u = radec_to_unit(ra, dec)
    x_hat, e_hat, n_hat = _tangent_basis(center[0], center[1])
    d = u @ x_hat
    ok = d > _MIN_COS
    with np.errstate(divide="ignore", invalid="ignore"):
        xi = np.degrees((u @ e_hat) / d)
        eta = np.degrees((u @ n_hat) / d)
    return xi, eta, ok


def _deproject_tangent(center, xi_deg, eta_deg):
    """gnomonic ``(ξ, η)``(度)→ (ra, dec) 度。:func:`_project_tangent` 的逆。"""
    x_hat, e_hat, n_hat = _tangent_basis(center[0], center[1])
    xi = np.radians(np.asarray(xi_deg, dtype=np.float64))
    eta = np.radians(np.asarray(eta_deg, dtype=np.float64))
    v = x_hat + xi[..., None] * e_hat + eta[..., None] * n_hat
    return unit_to_radec(v)


def _spherical_mean(ra, dec) -> tuple[float, float]:
    """一组天球坐标的方向平均。散布到整个天球(合矢量≈0)时抛错。"""
    u = radec_to_unit(ra, dec).reshape(-1, 3)
    m = u.mean(axis=0)
    norm = float(np.linalg.norm(m))
    if norm < 1e-6:
        raise WcsAppsError(_("这些方向散布在整个天球, 无法确定公共切点"))
    r, d = unit_to_radec(m / norm)
    return float(r), float(d)


def _linfit(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """一元线性拟合 ``y = k·x + b``。样本 < 2 或 x 全相同时返回 ``(nan, nan)``。"""
    if x.size < 2:
        return float("nan"), float("nan")
    xm = float(x.mean())
    dx = x - xm
    sxx = float(dx @ dx)
    if sxx <= 0.0:
        return float("nan"), float("nan")
    k = float(dx @ (y - y.mean()) / sxx)
    return k, float(y.mean() - k * xm)


# ------------------------------------------------------------------ 1. 足迹


@dataclass(frozen=True, eq=False)
class Footprint:
    """一张图在天球上的足迹多边形。

    :param radec: ``(n, 2)`` 顶点,按边界顺序,**首尾不重复**(闭合是隐含的;
        要画图用 :meth:`closed_radec`)。像素平面上从 ``(0.5, 0.5)`` 出发逆时针。
    :param unwrapped_ra: ``(n,)`` **连续化**的赤经 —— 逐边取最短方向累加,
        因此可能 ``< 0`` 或 ``≥ 360``。跨 0h 的足迹直接拿它画就不会横穿整幅图。
    :param wraps_ra0: 多边形是否跨越 RA=0h(绘图方据此决定要不要拆成两段)。
    :param pole: ``+1`` 含北天极 / ``-1`` 含南天极 / ``0`` 都不含。含极点时
        "赤经范围"没有意义(整圈都在里面),别再按 RA 裁图。
    :param wcs: 来源 WCS(:func:`footprint` 建的都有);有它时 :meth:`contains`
        走**像素判据**,精确到边界的 0.5 像素。

    ``eq=False``:字段里有 ndarray,自动 ``__eq__`` 会返回数组导致真值歧义。
    """

    radec: np.ndarray
    unwrapped_ra: np.ndarray
    wraps_ra0: bool
    pole: int
    width: int
    height: int
    center: tuple[float, float]
    wcs: TanWcs | None = None

    @property
    def n_vertices(self) -> int:
        return int(self.radec.shape[0])

    def closed_radec(self) -> np.ndarray:
        """``(n+1, 2)``:把第一个顶点补到末尾,可直接连线画闭合多边形。"""
        if self.radec.size == 0:
            return self.radec
        return np.vstack([self.radec, self.radec[:1]])

    def closed_unwrapped(self) -> np.ndarray:
        """``(n+1, 2)``:同 :meth:`closed_radec`,但赤经用连续化的值。"""
        if self.radec.size == 0:
            return self.radec
        ra = np.concatenate([self.unwrapped_ra, self.unwrapped_ra[:1]])
        dec = np.concatenate([self.radec[:, 1], self.radec[:1, 1]])
        return np.column_stack([ra, dec])

    def area_deg2(self) -> float:
        """足迹面积(平方度),见 :func:`footprint_area_deg2`。"""
        return footprint_area_deg2(self)

    def ra_span_deg(self) -> tuple[float, float]:
        """连续化赤经的 ``(min, max)``。含极点时没有意义,返回 ``(0, 360)``。"""
        if self.pole:
            return 0.0, 360.0
        if self.radec.size == 0:
            return float("nan"), float("nan")
        return float(self.unwrapped_ra.min()), float(self.unwrapped_ra.max())

    def dec_span_deg(self) -> tuple[float, float]:
        """赤纬范围 ``(min, max)``。

        含极点时把极点本身算进去(顶点只是边界,极点在**内部**)。
        """
        if self.radec.size == 0:
            return float("nan"), float("nan")
        lo = float(self.radec[:, 1].min())
        hi = float(self.radec[:, 1].max())
        if self.pole > 0:
            hi = 90.0
        elif self.pole < 0:
            lo = -90.0
        return lo, hi

    def contains(self, ra, dec):
        """点是否在视场内。标量进 bool 出,数组进 bool 数组出。

        有 :attr:`wcs` 时走**像素判据**(投回像素看落不落在
        ``[0.5, W+0.5] × [0.5, H+0.5]``),这是精确判据;
        没有时退到公共切平面上的 crossing number(边是大圆 ⇒ 平面上是直线,
        同样精确,只是切点选在足迹中心)。
        """
        ra_a = np.asarray(ra, dtype=np.float64)
        dec_a = np.asarray(dec, dtype=np.float64)
        scalar = ra_a.ndim == 0 and dec_a.ndim == 0
        if self.wcs is not None and self.width > 0 and self.height > 0:
            x, y = world_to_pixel(self.wcs, ra_a, dec_a)
            with np.errstate(invalid="ignore"):
                inside = ((x >= 0.5) & (x <= self.width + 0.5)
                          & (y >= 0.5) & (y <= self.height + 0.5))
        else:
            vx, vy, vok = _project_tangent(self.center, self.radec[:, 0],
                                           self.radec[:, 1])
            if not bool(np.all(vok)):
                raise WcsAppsError(_("足迹顶点落在切平面背面, 多边形无法判内外"))
            qx, qy, qok = _project_tangent(self.center, ra_a, dec_a)
            inside = _points_in_polygon(qx, qy, vx, vy) & qok
        inside = np.asarray(inside, dtype=bool)
        return bool(inside) if scalar else inside


def _points_in_polygon(px, py, vx, vy) -> np.ndarray:
    """平面 crossing number(射线法)。顶点数很小,只在顶点上循环,查询点向量化。

    边界上的点归属由浮点比较决定(不保证),覆盖统计里这点误差远小于一个格子。
    """
    px = np.asarray(px, dtype=np.float64)
    py = np.asarray(py, dtype=np.float64)
    inside = np.zeros(np.broadcast(px, py).shape, dtype=bool)
    n = len(vx)
    j = n - 1
    for i in range(n):
        yi, yj = float(vy[i]), float(vy[j])
        xi, xj = float(vx[i]), float(vx[j])
        straddle = (yi > py) != (yj > py)
        if yj != yi:
            with np.errstate(invalid="ignore", divide="ignore"):
                x_cross = xi + (py - yi) * (xj - xi) / (yj - yi)
            inside ^= straddle & (px < x_cross)
        j = i
    return inside


def footprint(wcs: TanWcs, width: int, height: int,
              n_per_edge: int = 1) -> Footprint:
    """图幅四角(必要时加边上的采样点)的天球多边形。

    :param wcs: 该帧的 WCS。
    :param width: FITS ``NAXIS1``(像素)。
    :param height: FITS ``NAXIS2``。
    :param n_per_edge: **每条边贡献几个顶点**。``1`` = 只要四角(默认);
        ``k`` 会在每条边上等距取 ``k`` 个点(含起点、不含终点),共 ``4k`` 个顶点。

        四角足够用吗?—— 边是大圆,而大圆在**任何** gnomonic 平面上都是直线,
        所以判内外/求交完全不需要加密。加密只在**画图**时有用:等距柱状、
        Aitoff 这类非 gnomonic 投影下,四角连直线会和真正的边差出来,视场越大
        差得越多。60° 视场用 ``n_per_edge=8`` 足够肉眼看不出。

    :returns: :class:`Footprint`。顶点顺序 = 像素平面
        ``(0.5,0.5) → (W+0.5,0.5) → (W+0.5,H+0.5) → (0.5,H+0.5)``。

    跨 0h 与极点都处理:``wraps_ra0`` 标记跨 0h(绘图方据此拆两段),
    ``pole`` 标记把天极圈在里面(此时 RA 范围没有意义)。
    """
    if not isinstance(wcs, TanWcs):
        raise WcsAppsError(_("footprint 需要 TanWcs"))
    w, h = _check_size(width, height)
    k = int(n_per_edge)
    if k < 1:
        raise WcsAppsError(_("n_per_edge 至少为 1"))

    corners = [(0.5, 0.5), (w + 0.5, 0.5), (w + 0.5, h + 0.5), (0.5, h + 0.5)]
    frac = np.arange(k, dtype=np.float64) / float(k)
    xs, ys = [], []
    for i in range(4):
        ax, ay = corners[i]
        bx, by = corners[(i + 1) % 4]
        xs.append(ax + (bx - ax) * frac)
        ys.append(ay + (by - ay) * frac)
    px = np.concatenate(xs)
    py = np.concatenate(ys)

    ra, dec = pixel_to_world(wcs, px, py)
    poly = np.column_stack([np.asarray(ra, dtype=np.float64),
                            np.asarray(dec, dtype=np.float64)])

    pole = _pole_inside(wcs, w, h)
    unwrapped, wraps = _unwrap_ra(poly[:, 0])
    if pole:
        wraps = False
    cra, cdec = pixel_to_world(wcs, (w + 1.0) / 2.0, (h + 1.0) / 2.0)
    return Footprint(radec=poly, unwrapped_ra=unwrapped, wraps_ra0=wraps,
                     pole=pole, width=w, height=h,
                     center=(float(cra), float(cdec)), wcs=wcs)


def _check_size(width, height) -> tuple[int, int]:
    try:
        w = int(width)
        h = int(height)
    except (TypeError, ValueError) as exc:
        raise WcsAppsError(_("图幅尺寸必须是整数")) from exc
    if w < 1 or h < 1:
        raise WcsAppsError(_("图幅尺寸必须 ≥ 1(给的是 %r × %r)") % (width, height))
    return w, h


def _pole_inside(wcs: TanWcs, w: int, h: int) -> int:
    """天极是否落在图幅内。用**像素判据**,精确。"""
    for sign, dec in ((1, 90.0), (-1, -90.0)):
        x, y = world_to_pixel(wcs, 0.0, dec)
        if (math.isfinite(x) and math.isfinite(y)
                and 0.5 <= x <= w + 0.5 and 0.5 <= y <= h + 0.5):
            return sign
    return 0


def _unwrap_ra(ra: np.ndarray) -> tuple[np.ndarray, bool]:
    """赤经序列连续化 + "是否跨 0h" 判定。

    首点取 ``[0, 360)``,之后逐边按最短方向累加。跨 0h ⟺ 连续化后越出
    ``[0, 360)``(闭合边回到首点,所以只看 min/max 就够)。
    """
    a = np.asarray(ra, dtype=np.float64)
    if a.size == 0:
        return a.copy(), False
    d = _wrap180(np.diff(a))
    out = np.empty_like(a)
    out[0] = a[0] % 360.0
    if a.size > 1:
        out[1:] = out[0] + np.cumsum(d)
    wraps = bool(out.max() >= 360.0 or out.min() < 0.0)
    return out, wraps


# ----------------------------------------------------------- 2. 球面多边形面积


def _as_polygon(poly) -> np.ndarray:
    """:class:`Footprint` 或 ``(n, 2)`` 数组 → ``(n, 2)`` 顶点(去掉重复的闭合点)。"""
    if isinstance(poly, Footprint):
        v = poly.radec
    else:
        v = np.asarray(poly, dtype=np.float64)
    if v.ndim != 2 or v.shape[1] != 2:
        raise WcsAppsError(_("多边形必须是 (n, 2) 的 (ra, dec) 数组"))
    if not np.all(np.isfinite(v)):
        raise WcsAppsError(_("多边形顶点含 NaN/Inf"))
    if len(v) >= 2 and abs(_wrap180(v[0, 0] - v[-1, 0])) < 1e-12 \
            and abs(v[0, 1] - v[-1, 1]) < 1e-12:
        v = v[:-1]
    return np.ascontiguousarray(v, dtype=np.float64)


def _polygon_excess_sr(v: np.ndarray) -> tuple[float, int]:
    """球面多边形的**有向**面积(球面度)与绕极圈数。

    用 Bevis & Cambareri 的半角正切式逐边累加(边是大圆,不是恒向线)::

        E_i = 2·atan2( tan(Δλ/2)·(tan φ_i/2 + tan φ_{i+1}/2),
                       1 + tan(φ_i/2)·tan(φ_{i+1}/2) )

    ``tan(±45°) = ±1``,极点本身当顶点也不会炸。绕极圈数
    ``k = round(ΣΔλ / 2π)``,有向面积 ``= (ΣE + 2πk) mod 4π``
    —— 这个 ``2πk`` 修正就是"多边形把极点圈在里面"时的那一项。
    """
    n = len(v)
    if n < 3:
        return 0.0, 0
    lon = np.radians(v[:, 0])
    lat = np.radians(v[:, 1])
    lon2 = np.roll(lon, -1)
    lat2 = np.roll(lat, -1)
    dlon = np.radians(_wrap180(np.degrees(lon2 - lon)))
    t1 = np.tan(lat / 2.0)
    t2 = np.tan(lat2 / 2.0)
    e = 2.0 * np.arctan2(np.tan(dlon / 2.0) * (t1 + t2), 1.0 + t1 * t2)
    winding = int(round(float(dlon.sum()) / _TWO_PI))
    signed = (float(e.sum()) + _TWO_PI * winding) % _FOUR_PI
    return signed, winding


def footprint_area_deg2(poly) -> float:
    """球面多边形面积,**平方度**。

    :param poly: :class:`Footprint`,或 ``(n, 2)`` 的 ``(ra, dec)`` 顶点数组
        (首尾重复的闭合点会自动去掉)。顶点顺序随便(顺/逆时针都行)。
    :returns: 面积。顶点少于 3 个返回 ``0.0``。

    **返回的是较小的那一侧**。闭曲线把球面分成两块,哪块算"里面"取决于绕行方向;
    视场足迹永远远小于半球,取小的那块就一定是你要的那块(含极点的小圆帽也对)。
    真要算超过半球的区域,自己用 ``4π - S`` 换算。

    精度:走球面剩余量的闭式,**不是**平面近似 —— 八分之一球面(5156.62 deg²)
    与极冠 ``2π(1-cos r)`` 都能到 1e-9 相对误差(单测钉死)。
    """
    v = _as_polygon(poly)
    signed, _w = _polygon_excess_sr(v)
    area = min(signed, _FOUR_PI - signed)
    return float(max(0.0, area) * _DEG2_PER_SR)


# --------------------------------------------------------- 输入正规化(共用)


def _wcs_of(item) -> TanWcs | None:
    """一条输入里的 WCS。

    接受 :class:`~astro_smb.wcs.TanWcs` 本身,或**任何带 ``.wcs`` 属性**的对象
    —— :class:`~astro_smb.platesolve.SolveResult` 是,GUI 侧自己包一层的行对象
    也是(鸭子类型,本模块不 import 那些类)。没解出来(``wcs is None``)返回
    ``None``,由调用方决定跳过还是报错。
    """
    if isinstance(item, TanWcs):
        return item
    if hasattr(item, "wcs"):
        w = item.wcs
        if w is None or isinstance(w, TanWcs):
            return w
        raise WcsAppsError(_("对象的 .wcs 不是 TanWcs"))
    return None


def _size_of(item, width, height) -> tuple[int, int] | None:
    """一条输入的图幅尺寸 ``(宽, 高)``:先看 ``hint.image_size``,再看参数。"""
    hint = getattr(item, "hint", None)
    size = getattr(hint, "image_size", None) if hint is not None else None
    if size:
        try:
            return _check_size(size[0], size[1])
        except (WcsAppsError, IndexError, TypeError):
            pass
    if width is not None and height is not None:
        return _check_size(width, height)
    return None


# ------------------------------------------------------------------ 3. 覆盖


@dataclass(frozen=True, eq=False)
class Coverage:
    """一组足迹的并集覆盖 / 公共交集 / 缺口 / **覆盖张数图**。

    所有面积都是**平方度**,已按 gnomonic 面积元 ``1/(1+ξ²+η²)^{3/2}`` 逐格
    加权,不是"格数 × 平面格面积"。

    :param counts: ``(ny, nx)`` **覆盖张数图** —— 每个格点被几张图盖到。
        "这块天区只被 3 张覆盖到"就是拿它画的。行 = η(北为大),列 = ξ(东为大)。
    :param cell_area_deg2: ``(ny, nx)`` 每格的球面面积,与 ``counts`` 同形状。
    :param depth_area_deg2: ``(n_frames+1,)`` 按覆盖张数分档的面积;
        ``[0]`` 是格网内没被任何图盖到的面积,``[n]`` 就是公共交集。
    :param max_gap_deg2: **最大缺口** —— 被拍摄区域包围、内部却没盖到的连通块里
        最大的那块。并集外面的空白不算(那是画布边缘,不是漏拍)。
    :param center / xi_deg / eta_deg: 公共 gnomonic 切点与格心坐标轴(度),
        配 :meth:`cell_radec` 可以把 ``counts`` 摆回天球。
    """

    n_frames: int
    center: tuple[float, float]
    xi_deg: np.ndarray
    eta_deg: np.ndarray
    cell_deg: float
    counts: np.ndarray
    cell_area_deg2: np.ndarray
    union_area_deg2: float
    common_area_deg2: float
    depth_area_deg2: np.ndarray
    max_gap_deg2: float
    gap_area_deg2: float
    n_gaps: int
    frame_area_deg2: np.ndarray

    # -------- 派生量

    @property
    def common_fraction(self) -> float:
        """公共交集占并集的比例(1.0 = 每张都盖住同一块天区,完全没跑位)。"""
        if self.union_area_deg2 <= 0.0:
            return float("nan")
        return self.common_area_deg2 / self.union_area_deg2

    @property
    def gap_fraction(self) -> float:
        """内部缺口面积占并集的比例。"""
        if self.union_area_deg2 <= 0.0:
            return float("nan")
        return self.gap_area_deg2 / self.union_area_deg2

    def cell_radec(self):
        """格心的 ``(ra, dec)``,各 ``(ny, nx)``。用时才算(格网可能不小)。"""
        if self.counts.size == 0:
            e = np.zeros((0, 0))
            return e, e
        gx, gy = np.meshgrid(self.xi_deg, self.eta_deg)
        return _deproject_tangent(self.center, gx, gy)

    def depth_at(self, ra, dec):
        """某天球位置的覆盖张数(最近格点查表)。格网外或切平面背面返回 ``-1``。"""
        ra_a = np.asarray(ra, dtype=np.float64)
        dec_a = np.asarray(dec, dtype=np.float64)
        scalar = ra_a.ndim == 0 and dec_a.ndim == 0
        shape = np.broadcast(ra_a, dec_a).shape
        if self.counts.size == 0:
            out = np.full(shape, -1, dtype=np.int32)
            return int(out) if scalar else out
        xi, eta, ok = _project_tangent(self.center, ra_a, dec_a)
        with np.errstate(invalid="ignore"):
            ix = np.rint((np.asarray(xi) - self.xi_deg[0]) / self.cell_deg)
            iy = np.rint((np.asarray(eta) - self.eta_deg[0]) / self.cell_deg)
        good = (np.asarray(ok) & np.isfinite(ix) & np.isfinite(iy))
        ixi = np.where(good, ix, 0).astype(np.int64)
        iyi = np.where(good, iy, 0).astype(np.int64)
        good &= ((ixi >= 0) & (ixi < len(self.xi_deg))
                 & (iyi >= 0) & (iyi < len(self.eta_deg)))
        vals = self.counts[np.clip(iyi, 0, len(self.eta_deg) - 1),
                           np.clip(ixi, 0, len(self.xi_deg) - 1)]
        out = np.where(good, vals, -1).astype(np.int32)
        return int(out) if scalar else out


def _as_footprint(item, width, height) -> Footprint:
    """把一条输入正规化成 :class:`Footprint`。

    接受 :class:`Footprint`、:class:`~astro_smb.wcs.TanWcs`、任何带 ``.wcs``
    的对象(:class:`~astro_smb.platesolve.SolveResult` 就是),或 ``(n, 2)``
    的天球多边形。图幅尺寸优先取对象自带的 ``hint.image_size``,再取参数。
    """
    if isinstance(item, Footprint):
        return item
    w = _wcs_of(item)
    if w is not None:
        size = _size_of(item, width, height)
        if size is None:
            raise WcsAppsError(_("这条输入没带图幅尺寸, 请给 width/height"))
        return footprint(w, size[0], size[1])
    if isinstance(item, TanWcs) or hasattr(item, "wcs"):
        raise WcsAppsError(_("这条输入没有可用的 WCS"))
    v = _as_polygon(item)
    if len(v) < 3:
        raise WcsAppsError(_("天球多边形至少要 3 个顶点"))
    unwrapped, wraps = _unwrap_ra(v[:, 0])
    _signed, winding = _polygon_excess_sr(v)
    pole = 0
    if winding:
        pole = 1 if float(np.mean(v[:, 1])) >= 0.0 else -1
    return Footprint(radec=v, unwrapped_ra=unwrapped,
                     wraps_ra0=bool(wraps and not pole), pole=pole,
                     width=0, height=0,
                     center=_spherical_mean(v[:, 0], v[:, 1]), wcs=None)


def _empty_coverage() -> Coverage:
    return Coverage(n_frames=0, center=(float("nan"), float("nan")),
                    xi_deg=np.zeros(0), eta_deg=np.zeros(0),
                    cell_deg=float("nan"),
                    counts=np.zeros((0, 0), dtype=np.int32),
                    cell_area_deg2=np.zeros((0, 0)),
                    union_area_deg2=0.0, common_area_deg2=0.0,
                    depth_area_deg2=np.zeros(1), max_gap_deg2=0.0,
                    gap_area_deg2=0.0, n_gaps=0, frame_area_deg2=np.zeros(0))


def coverage(footprints, *, width=None, height=None, grid: int = 256,
             pad_cells: int = 2) -> Coverage:
    """一组足迹的**并集覆盖**与**缺口**。

    :param footprints: 一批 :class:`Footprint` / :class:`~astro_smb.wcs.TanWcs`
        / 解算结果(带 ``.wcs``)/ ``(n,2)`` 天球多边形,混着给也行。典型用法是
        同一目标的一整晚 sub。
    :param width / height: 给裸 ``TanWcs`` 用的图幅尺寸(对象自带
        ``hint.image_size`` 时不用给)。
    :param grid: 长边方向的格子数(默认 256 ⇒ 约 6.6 万格)。面积精度约是
        **周长 × 格宽**量级,要更准就调大(代价是 O(格数 × 帧数))。
    :param pad_cells: 格网四周留几格空白 —— 缺口的洪水填充要从边缘起步,
        贴边的足迹会让"外面"无路可走,留白是必须的。
    :returns: :class:`Coverage`;**空输入返回 ``n_frames=0`` 的空结果**,不抛异常。

    做法:把所有足迹投到**公共 gnomonic 切平面**(切点 = 所有顶点的方向平均),
    平面上打规则格网,逐帧用精确判据(有 WCS 就用像素判据)数每格被盖几次。
    大圆在 gnomonic 平面上是直线 ⇒ 这不是小视场近似。每格面积按
    ``1/(1+ξ²+η²)^{3/2}`` 折算回球面。

    **缺口的定义**:``counts == 0`` 且**不与格网边缘连通**的 4-连通块。并集外面
    的大片空白与边缘连通,不算缺口 —— 只有被拍摄区域包围的"洞"才是漏拍。
    """
    items = list(footprints) if footprints is not None else []
    if not items:
        return _empty_coverage()
    g = int(grid)
    if g < 8 or g > 4096:
        raise WcsAppsError(_("grid 必须在 8~4096 之间(给的是 %r)") % (grid,))
    pad = max(0, int(pad_cells))

    fps = [_as_footprint(it, width, height) for it in items]
    all_ra = np.concatenate([f.radec[:, 0] for f in fps])
    all_dec = np.concatenate([f.radec[:, 1] for f in fps])
    center = _spherical_mean(all_ra, all_dec)

    vx, vy, ok = _project_tangent(center, all_ra, all_dec)
    if not bool(np.all(ok)):
        raise WcsAppsError(_("这些足迹散布超过 90°, 无法投到同一个切平面"))

    xi_lo, xi_hi = float(vx.min()), float(vx.max())
    eta_lo, eta_hi = float(vy.min()), float(vy.max())
    span = max(xi_hi - xi_lo, eta_hi - eta_lo)
    if not math.isfinite(span) or span <= 0.0:
        raise WcsAppsError(_("足迹退化成一个点, 无法建覆盖格网"))
    cell = span / float(g)
    nx = int(math.ceil((xi_hi - xi_lo) / cell)) + 2 * pad + 1
    ny = int(math.ceil((eta_hi - eta_lo) / cell)) + 2 * pad + 1
    xi_axis = xi_lo - pad * cell + np.arange(nx) * cell
    eta_axis = eta_lo - pad * cell + np.arange(ny) * cell

    gx, gy = np.meshgrid(xi_axis, eta_axis)
    ra_g, dec_g = _deproject_tangent(center, gx, gy)

    counts = np.zeros((ny, nx), dtype=np.int32)
    frame_area = np.empty(len(fps), dtype=np.float64)
    for i, f in enumerate(fps):
        counts += np.asarray(f.contains(ra_g, dec_g), dtype=np.int32)
        frame_area[i] = f.area_deg2()

    r2 = np.radians(gx) ** 2 + np.radians(gy) ** 2
    cell_area = (cell * cell) / np.power(1.0 + r2, 1.5)

    n = len(fps)
    depth_area = np.zeros(n + 1, dtype=np.float64)
    for k in range(n + 1):
        depth_area[k] = float(cell_area[counts == k].sum())
    union = float(cell_area[counts >= 1].sum())

    empty = counts == 0
    holes = empty & ~_flood_from_border(empty)
    sizes = _component_areas(holes, cell_area)
    return Coverage(
        n_frames=n, center=center, xi_deg=xi_axis, eta_deg=eta_axis,
        cell_deg=cell, counts=counts, cell_area_deg2=cell_area,
        union_area_deg2=union, common_area_deg2=float(depth_area[n]),
        depth_area_deg2=depth_area,
        max_gap_deg2=float(sizes[0]) if sizes else 0.0,
        gap_area_deg2=float(sum(sizes)), n_gaps=len(sizes),
        frame_area_deg2=frame_area)


def _flood_from_border(mask: np.ndarray) -> np.ndarray:
    """``mask`` 里与格网边缘 4-连通的那部分(numpy 迭代扩张,不引 scipy)。

    迭代轮数 = 该连通块离边缘的最大测地深度;并集外面的空白是贴着边缘的一圈,
    深度很浅,实测几十轮就收敛。上限 ``ny+nx+2`` 是纯保险(单调扩张必然收敛)。
    """
    if mask.size == 0:
        return mask.copy()
    out = np.zeros_like(mask)
    out[0, :] = mask[0, :]
    out[-1, :] = mask[-1, :]
    out[:, 0] = mask[:, 0]
    out[:, -1] = mask[:, -1]
    for _i in range(mask.shape[0] + mask.shape[1] + 2):
        nxt = out.copy()
        nxt[1:, :] |= out[:-1, :]
        nxt[:-1, :] |= out[1:, :]
        nxt[:, 1:] |= out[:, :-1]
        nxt[:, :-1] |= out[:, 1:]
        nxt &= mask
        if np.array_equal(nxt, out):
            break
        out = nxt
    return out


def _component_areas(mask: np.ndarray, cell_area: np.ndarray) -> list[float]:
    """``mask`` 各 4-连通块的面积(平方度),**降序**。洞通常很小,显式栈够快。"""
    if mask.size == 0 or not bool(mask.any()):
        return []
    ny, nx = mask.shape
    flat = mask.copy().reshape(-1)
    area = cell_area.reshape(-1)
    out: list[float] = []
    for seed in np.flatnonzero(flat):
        seed = int(seed)
        if not flat[seed]:
            continue
        stack = [seed]
        flat[seed] = False
        total = 0.0
        while stack:
            p = stack.pop()
            total += float(area[p])
            r, c = divmod(p, nx)
            if r > 0 and flat[p - nx]:
                flat[p - nx] = False
                stack.append(p - nx)
            if r < ny - 1 and flat[p + nx]:
                flat[p + nx] = False
                stack.append(p + nx)
            if c > 0 and flat[p - 1]:
                flat[p - 1] = False
                stack.append(p - 1)
            if c < nx - 1 and flat[p + 1]:
                flat[p + 1] = False
                stack.append(p + 1)
        out.append(total)
    out.sort(reverse=True)
    return out


# -------------------------------------------------------------- 4. 指向误差


class PointingError(NamedTuple):
    """指向误差三元组。可以直接当 ``(总, RA, DEC)`` 解包。

    :param total_arcsec: **总偏差** = 精确大圆角距(角秒),恒 ≥ 0。
    :param ra_arcsec: 东向分量 = ``ΔRA × cos(平均赤纬) × 3600``。正 = 解出的
        位置偏**东**(赤经更大)。
    :param dec_arcsec: 北向分量 = ``ΔDEC × 3600``。正 = 偏**北**。

    ``hypot(ra, dec)`` 与 ``total`` 只在二阶意义上相等(1° 偏差时差 ~0.07″);
    要严格的总量就用 ``total_arcsec``,它是球面上量出来的。
    """

    total_arcsec: float
    ra_arcsec: float
    dec_arcsec: float


def pointing_error(solved_center, requested_radec) -> PointingError:
    """解算出的视场中心 vs 命令位置 —— goto 精度。

    :param solved_center: ``(ra, dec)`` 度、``(n, 2)`` 数组,或任何带 ``.center``
        的对象(:class:`~astro_smb.platesolve.SolveResult` 就是,它的
        :attr:`~astro_smb.platesolve.SolveResult.center` 已经是**图幅中心**
        而不是 CRVAL)。
    :param requested_radec: 命令位置。来自日志的 goto 坐标,或 FITS 头的
        ``RA``/``DEC``(ASIAIR 写的是十进制度;``OBJCTRA``/``OBJCTDEC`` 实测不存在)。
    :returns: :class:`PointingError`。标量进标量出,``(n,2)`` 进数组出。

    赤经跨 0h 自动处理(差值折到 ``[-180, 180)``);两个位置都在极点附近时
    ``cos(平均赤纬) → 0``,RA 分量会塌成 0,此时只有 ``total_arcsec`` 有意义。
    """
    a = _center_pair(solved_center, "solved_center")
    b = _center_pair(requested_radec, "requested_radec")
    scalar = a.ndim == 1 and b.ndim == 1
    a2 = np.atleast_2d(a)
    b2 = np.atleast_2d(b)
    if a2.shape[0] != b2.shape[0] and a2.shape[0] != 1 and b2.shape[0] != 1:
        raise WcsAppsError(_("两组坐标个数对不上(%d vs %d)")
                           % (a2.shape[0], b2.shape[0]))
    ra_s, dec_s = a2[:, 0], a2[:, 1]
    ra_r, dec_r = b2[:, 0], b2[:, 1]
    total = np.asarray(angular_separation(ra_r, dec_r, ra_s, dec_s)) * 3600.0
    cosd = np.cos(np.radians((dec_s + dec_r) / 2.0))
    d_ra = _wrap180(ra_s - ra_r) * cosd * 3600.0
    d_dec = (dec_s - dec_r) * 3600.0
    if scalar:
        return PointingError(float(total[0]), float(d_ra[0]), float(d_dec[0]))
    return PointingError(np.asarray(total, dtype=np.float64),
                         np.asarray(d_ra, dtype=np.float64),
                         np.asarray(d_dec, dtype=np.float64))


def _center_pair(item, what: str) -> np.ndarray:
    """``(ra, dec)`` / ``(n,2)`` / 带 ``.center`` 的对象 → ndarray。"""
    if hasattr(item, "center") and not isinstance(item, (tuple, list, np.ndarray)):
        c = item.center
        if c is None:
            raise WcsAppsError(_("%s 没有可用的中心坐标(解算失败?)") % what)
        item = c
    v = np.asarray(item, dtype=np.float64)
    if v.shape == (2,):
        return v
    if v.ndim == 2 and v.shape[1] == 2:
        return v
    raise WcsAppsError(_("%s 必须是 (ra, dec) 或 (n, 2) 数组") % what)


# ----------------------------------------------------- 时间序列输入正规化


def _split_time_item(item):
    """``(t, obj)`` 或 ``obj`` → ``(t|None, obj)``。"""
    if isinstance(item, (tuple, list)) and len(item) == 2:
        second = item[1]
        if isinstance(second, TanWcs) or hasattr(second, "wcs"):
            return item[0], second
    return None, item


def _series(items, times):
    """时间序列输入正规化。

    :returns: ``(t_s, wcs_list, objs, n_skipped)``。``t_s`` 是**升序**的秒数
        (稳定排序,同时刻保持原序);``.wcs is None`` 的条目(解算失败)被跳过,
        个数记在 ``n_skipped``。
    """
    items = list(items) if items is not None else []
    raw_t: list = []
    objs: list = []
    for it in items:
        t, obj = _split_time_item(it)
        raw_t.append(t)
        objs.append(obj)
    if times is not None:
        tl = list(times)
        if len(tl) != len(objs):
            raise WcsAppsError(_("时刻个数(%d)与结果个数(%d)对不上")
                               % (len(tl), len(objs)))
        raw_t = tl
    keep_t, keep_w, keep_o = [], [], []
    skipped = 0
    for t, obj in zip(raw_t, objs):
        w = _wcs_of(obj)
        if w is None:
            skipped += 1
            continue
        if t is None:
            raise WcsAppsError(
                _("缺少时刻:要么传 times=,要么把每条写成 (时刻, 结果) 二元组"))
        keep_t.append(_as_seconds(t))
        keep_w.append(w)
        keep_o.append(obj)
    if not keep_t:
        return np.zeros(0), [], [], skipped
    t_arr = np.asarray(keep_t, dtype=np.float64)
    order = np.argsort(t_arr, kind="stable")
    return (t_arr[order], [keep_w[i] for i in order],
            [keep_o[i] for i in order], skipped)


# -------------------------------------------------------------- 5. 场旋


@dataclass(frozen=True, eq=False)
class FieldRotation:
    """位置角随时间的变化 —— **极轴误差的直接观测量**。

    赤道仪跟踪得再准,极轴一歪,视场就会绕着导星星缓慢转;这个转速跟曝光时长
    一乘就是边缘星点被拉长的量。跟"帧中心漂移"(:func:`drift`)是两件独立的事:
    漂移可以靠导星压住,场旋压不住。

    :param angles_deg: **解缠后**的位置角序列(:meth:`TanWcs.rotation_deg`),
        连续、可超出 ``[0, 360)`` —— 相邻两帧转过 180° 以上就解不对,采样别太稀。
    :param rate_deg_per_hour: 线性拟合斜率(度/小时)。样本 < 2 或时刻全相同
        时是 ``nan``。
    :param resid_deg: 各帧对拟合直线的残差(度);``rms_deg`` / ``max_dev_deg``
        是它的统计。残差大 = 转速本身在变(过中天前后就会),别只看斜率。
    """

    n: int
    t0: float
    times_s: np.ndarray
    angles_deg: np.ndarray
    rate_deg_per_hour: float
    intercept_deg: float
    resid_deg: np.ndarray
    rms_deg: float
    max_dev_deg: float
    span_hours: float
    total_deg: float
    n_skipped: int
    meridian_flip: bool = False

    @property
    def ok(self) -> bool:
        """拟合是否成立(≥2 帧且时刻不全相同)。"""
        return (not self.meridian_flip and self.n >= 2
                and math.isfinite(self.rate_deg_per_hour))

    @property
    def rate_arcsec_per_min(self) -> float:
        """转速换成角秒/分钟(和导星那边的口径对得上)。"""
        return self.rate_deg_per_hour * 3600.0 / 60.0


def field_rotation(results, times=None) -> FieldRotation:
    """一组按时间排列的解算结果 → 位置角序列 + 旋转速率 + 残差。

    :param results: 每条是 ``(时刻, 结果)`` 二元组,或结果本身(此时用 ``times=``
        传时刻)。"结果"可以是 :class:`~astro_smb.wcs.TanWcs` 或任何带 ``.wcs``
        的对象;``.wcs is None`` 的(没解出来)自动跳过并计入 ``n_skipped``。
    :param times: 与 ``results`` 等长的时刻(秒或 ``datetime``)。
    :returns: :class:`FieldRotation`。**内部会按时刻升序重排**,给的顺序乱了也没事。

    角度取 :meth:`~astro_smb.wcs.TanWcs.rotation_deg`(图像 +y 轴的位置角),
    **不是** ZWO 约定角 —— 后者差个 180° 加镜像,做差时会多绕一圈。
    一组里混着镜像与非镜像的帧本身就没法叠,这里也不会替你纠正。

    空输入 / 单张 / 时刻全相同都不抛异常,返回 ``ok == False`` 的结果。
    """
    t, wl, _objs, skipped = _series(results, times)
    n = len(wl)
    if n == 0:
        return FieldRotation(0, float("nan"), np.zeros(0), np.zeros(0),
                             float("nan"), float("nan"), np.zeros(0),
                             float("nan"), float("nan"), 0.0, 0.0, skipped)
    ang = np.array([w.rotation_deg() for w in wl], dtype=np.float64)
    # GEM 中天翻转会让相机位置角瞬间跳约 180°。这不是连续场旋，
    # 若硬交给 unwrap/直线拟合，会制造一个非常大的假极轴误差。
    jumps = np.abs((np.diff(ang) + 180.0) % 360.0 - 180.0)
    meridian_flip = bool(np.any(jumps > 150.0))
    ang = np.degrees(np.unwrap(np.radians(ang)))
    t0 = float(t[0])
    rel = t - t0
    hours = rel / 3600.0
    k, b = ((float("nan"), float("nan")) if meridian_flip
            else _linfit(hours, ang))
    if math.isfinite(k):
        resid = ang - (k * hours + b)
    else:
        resid = np.zeros(n)
    return FieldRotation(
        n=n, t0=t0, times_s=rel, angles_deg=ang,
        rate_deg_per_hour=k, intercept_deg=b, resid_deg=resid,
        rms_deg=float(np.sqrt(np.mean(resid ** 2))) if n else float("nan"),
        max_dev_deg=float(np.max(np.abs(resid))) if n else float("nan"),
        span_hours=float(hours[-1] - hours[0]),
        total_deg=float(ang[-1] - ang[0]), n_skipped=skipped,
        meridian_flip=meridian_flip)


# -------------------------------------------------------------- 6. 中心漂移


@dataclass(frozen=True, eq=False)
class Drift:
    """帧中心随时间的漂移。

    :param ra_arcsec / dec_arcsec: 相对参考帧的偏移(角秒),**已扣掉
        ``subtract`` 给的已知 dither**。RA 分量是**东向大圆分量**(含 cos δ)。
    :param raw_ra_arcsec / raw_dec_arcsec: 没扣 dither 的原始偏移。
    :param dither_ra_arcsec / dither_dec_arcsec: 实际扣掉的量(零阶保持后的值),
        方便复核 dither 表对没对上帧。
    :param rate_arcsec_per_min: 合成漂移速率的模;``rate_pa_deg`` 是它的方向
        (位置角,北起东向)。
    :param resid_*: 扣掉线性漂移后的残差 —— **这才是"抖"**(导星抽风、阵风、
        dither 没扣干净),线性那部分只是极轴/大气折射的稳定项。
    """

    n: int
    t0: float
    ref: int
    times_s: np.ndarray
    ra_arcsec: np.ndarray
    dec_arcsec: np.ndarray
    raw_ra_arcsec: np.ndarray
    raw_dec_arcsec: np.ndarray
    dither_ra_arcsec: np.ndarray
    dither_dec_arcsec: np.ndarray
    rate_ra_arcsec_per_min: float
    rate_dec_arcsec_per_min: float
    rate_arcsec_per_min: float
    rate_pa_deg: float
    total_arcsec: float
    max_excursion_arcsec: float
    resid_ra_arcsec: np.ndarray
    resid_dec_arcsec: np.ndarray
    rms_resid_arcsec: float
    max_resid_arcsec: float
    span_hours: float
    centers: np.ndarray
    n_skipped: int

    @property
    def ok(self) -> bool:
        return self.n >= 2 and math.isfinite(self.rate_arcsec_per_min)


def drift(results, times=None, *, width=None, height=None, ref: int = 0,
          subtract=None) -> Drift:
    """帧中心随时间的漂移:总位移、速率、RA/DEC 分解、线性拟合残差。

    :param results: 同 :func:`field_rotation`。
    :param times: 同 :func:`field_rotation`。
    :param width / height: 裸 ``TanWcs`` 的图幅尺寸(用来取**图幅中心**;
        带 ``hint.image_size`` 的对象不用给)。两者都没有时退而用 ``CRVAL``
        —— 用 ``crpix_guess=图幅中心`` 拟合出来的 WCS 里两者本就相等。
    :param ref: 参考帧下标(**按时刻排序后**的下标,默认 0 = 最早那张)。
    :param subtract: 已知的指令抖动(dither),一组 ``(时刻, dRA, dDEC)``,
        角秒,``dRA`` 同样是**东向分量**。按**零阶保持**扣除:每帧扣掉时刻
        ``≤`` 它的最后一条(dither 是阶跃,不是渐变);第一条之前的帧扣 0。
    :returns: :class:`Drift`。空输入/单张不抛异常(速率为 ``nan``)。

    偏移量在**参考帧中心的切平面**上量(gnomonic),几角分以内与真实角距的
    差别在 1e-6 相对量级 —— 漂移分析用绰绰有余。
    """
    t, wl, objs, skipped = _series(results, times)
    n = len(wl)
    if n == 0:
        z = np.zeros(0)
        return Drift(0, float("nan"), int(ref), z, z, z, z, z, z, z,
                     float("nan"), float("nan"), float("nan"), float("nan"),
                     float("nan"), float("nan"), z, z, float("nan"),
                     float("nan"), 0.0, np.zeros((0, 2)), skipped)
    if not 0 <= int(ref) < n:
        raise WcsAppsError(_("ref 下标 %r 超出范围(可用 0~%d)") % (ref, n - 1))
    ref = int(ref)

    centers = np.array([_frame_center(w, o, width, height)
                        for w, o in zip(wl, objs)], dtype=np.float64)
    base = (float(centers[ref, 0]), float(centers[ref, 1]))
    xi, eta, ok = _project_tangent(base, centers[:, 0], centers[:, 1])
    if not bool(np.all(ok)):
        raise WcsAppsError(_("有帧的中心离参考帧超过 90°, 不是同一个目标吧?"))
    raw_ra = np.asarray(xi) * 3600.0
    raw_dec = np.asarray(eta) * 3600.0

    d_ra, d_dec = _hold_offsets(subtract, t)
    ra = raw_ra - d_ra
    dec = raw_dec - d_dec

    t0 = float(t[0])
    rel = t - t0
    minutes = rel / 60.0
    k_ra, b_ra = _linfit(minutes, ra)
    k_dec, b_dec = _linfit(minutes, dec)
    if math.isfinite(k_ra) and math.isfinite(k_dec):
        res_ra = ra - (k_ra * minutes + b_ra)
        res_dec = dec - (k_dec * minutes + b_dec)
        speed = float(math.hypot(k_ra, k_dec))
        pa = float(math.degrees(math.atan2(k_ra, k_dec)) % 360.0)
    else:
        res_ra = np.zeros(n)
        res_dec = np.zeros(n)
        speed = float("nan")
        pa = float("nan")
    radius = np.hypot(ra, dec)
    res_r = np.hypot(res_ra, res_dec)
    return Drift(
        n=n, t0=t0, ref=ref, times_s=rel, ra_arcsec=ra, dec_arcsec=dec,
        raw_ra_arcsec=raw_ra, raw_dec_arcsec=raw_dec,
        dither_ra_arcsec=d_ra, dither_dec_arcsec=d_dec,
        rate_ra_arcsec_per_min=k_ra, rate_dec_arcsec_per_min=k_dec,
        rate_arcsec_per_min=speed, rate_pa_deg=pa,
        total_arcsec=float(math.hypot(ra[-1] - ra[0], dec[-1] - dec[0])),
        max_excursion_arcsec=float(radius.max()),
        resid_ra_arcsec=res_ra, resid_dec_arcsec=res_dec,
        rms_resid_arcsec=float(np.sqrt(np.mean(res_r ** 2))),
        max_resid_arcsec=float(res_r.max()),
        span_hours=float(rel[-1] - rel[0]) / 3600.0,
        centers=centers, n_skipped=skipped)


def _frame_center(w: TanWcs, obj, width, height) -> tuple[float, float]:
    """帧的**图幅中心**天球坐标;拿不到尺寸就退回 CRVAL。"""
    size = _size_of(obj, width, height)
    if size is None:
        return float(w.crval[0]), float(w.crval[1])
    ra, dec = pixel_to_world(w, (size[0] + 1.0) / 2.0, (size[1] + 1.0) / 2.0)
    return float(ra), float(dec)


def _hold_offsets(subtract, t: np.ndarray):
    """``(时刻, dRA, dDEC)`` 表 → 每帧生效的偏移(零阶保持)。"""
    n = len(t)
    if subtract is None:
        return np.zeros(n), np.zeros(n)
    rows = list(subtract)
    if not rows:
        return np.zeros(n), np.zeros(n)
    st, sra, sdec = [], [], []
    for row in rows:
        if len(row) != 3:
            raise WcsAppsError(_("subtract 的每条必须是 (时刻, dRA, dDEC)"))
        st.append(_as_seconds(row[0]))
        sra.append(float(row[1]))
        sdec.append(float(row[2]))
    st = np.asarray(st, dtype=np.float64)
    order = np.argsort(st, kind="stable")
    st = st[order]
    sra = np.asarray(sra, dtype=np.float64)[order]
    sdec = np.asarray(sdec, dtype=np.float64)[order]
    idx = np.searchsorted(st, t, side="right") - 1
    valid = idx >= 0
    safe = np.clip(idx, 0, len(st) - 1)
    return (np.where(valid, sra[safe], 0.0), np.where(valid, sdec[safe], 0.0))


# ------------------------------------------------------------ 7. 叠加对齐


@dataclass(frozen=True, eq=False)
class FrameAlignment:
    """单帧相对参考帧的对齐参数(:class:`StackAlignment` 的一行)。"""

    index: int
    dx_px: float
    dy_px: float
    shift_px: float
    rotation_deg: float
    scale: float
    rms_px: float
    max_px: float
    overlap_frac: float
    parity_mismatch: bool


@dataclass(frozen=True, eq=False)
class StackAlignment:
    """一组帧对齐到参考帧要做什么变换 —— "这一组能不能直接叠"。

    所有量都是 **frame i → 参考帧** 的方向(叠加时就是这么用的:把每张的像素
    搬到参考帧的格子里)。

    :param dx_px / dy_px: 该帧**图幅中心**落在参考帧里的位置减去参考帧自己的
        中心;正 dx = 内容偏向参考帧的 +x。旋转按图幅中心算,所以平移和旋转
        是解耦的。
    :param rotation_deg: 需要**逆时针**转多少度(像素平面,+x 向 +y)。
    :param scale: 需要缩放多少倍(≈1 表示焦距没变)。
    :param rms_px / max_px: 用"平移+旋转+等比缩放"这一个相似变换去套整幅图后的
        残差。**这才是"能不能直接叠"的判据** —— 残差大说明视场之间还有别的形变
        (投影非线性、镜头畸变差异),得上更高阶的配准。
    :param overlap_frac: **参考帧**有多大比例落在该帧里(0~1)。用两个视场四边形
        在公共切平面上的**精确凸多边形求交**算(大圆在 gnomonic 平面上是直线,
        所以裁剪是精确的),不是撒点数比例 —— 撒 17×17 的话小于半格(约 3%)的
        损失根本看不出来(真踩过:焦距差 2% 时重叠报成 1.00)。
    :param parity_mismatch: 宇称不同(一张镜像一张不镜像)。此时相似变换根本
        套不上,必须先翻转一次,``rms_px`` 会很大。
    """

    ref: int
    index: np.ndarray
    dx_px: np.ndarray
    dy_px: np.ndarray
    shift_px: np.ndarray
    rotation_deg: np.ndarray
    scale: np.ndarray
    rms_px: np.ndarray
    max_px: np.ndarray
    overlap_frac: np.ndarray
    parity_mismatch: np.ndarray
    n_skipped: int

    @property
    def n(self) -> int:
        return int(self.index.size)

    def usable(self, *, min_overlap: float = 0.5, max_rms_px: float = 2.0):
        """哪些帧"可以直接叠":重叠够大、相似变换套得上、宇称一致。

        阈值是**调用方的策略**,所以做成参数而不是写死在结果里。
        """
        return ((self.overlap_frac >= float(min_overlap))
                & (self.rms_px <= float(max_rms_px))
                & ~self.parity_mismatch)

    def rows(self) -> list[FrameAlignment]:
        """逐帧的 :class:`FrameAlignment`(UI 里按行显示用)。"""
        return [FrameAlignment(int(self.index[i]), float(self.dx_px[i]),
                               float(self.dy_px[i]), float(self.shift_px[i]),
                               float(self.rotation_deg[i]), float(self.scale[i]),
                               float(self.rms_px[i]), float(self.max_px[i]),
                               float(self.overlap_frac[i]),
                               bool(self.parity_mismatch[i]))
                for i in range(self.n)]


def stack_alignment(results, ref: int = 0, *, width=None, height=None,
                    samples: int = 17) -> StackAlignment:
    """以某张为参考,其余各张要平移/旋转/缩放多少才能对齐。

    .. note::
       **当前没有产品调用方,为任务 #33(导星质量逆推验证)预留。**

       #33 要回答的是"导星曲线漂亮但目标在主镜里走没走"。本函数给的
       ``rotation_deg`` / ``scale`` / ``overlap_frac`` / ``parity_mismatch``
       正是那条证据链需要的:场旋(极轴误差的直接观测量)、中天翻转、
       以及"这一组还能不能直接叠"。接线时机是 #33 把三通道证据装配起来的时候
       —— 它现在只用了 :func:`drift` 与 :func:`field_rotation`。

       留着不删的理由:算法与 135 条单测都在,重写一遍不划算;但**不允许它
       一直无主** —— 若 #33 最终没用到,应当删除而不是继续挂着。

    :param results: 一组 :class:`~astro_smb.wcs.TanWcs` 或带 ``.wcs`` 的结果
        (**不需要时刻**,也不重排序)。``.wcs is None`` 的跳过,原始下标记在
        :attr:`StackAlignment.index` 里。
    :param ref: 参考帧在**输入序列**里的下标。该帧必须有 WCS,否则抛异常。
    :param width / height: 裸 ``TanWcs`` 的图幅尺寸。
    :param samples: 每轴采样点数(默认 17 ⇒ 289 点)。相似变换只有 4 个自由度,
        采样多是为了让残差与重叠比例估得准。
    :returns: :class:`StackAlignment`;空输入返回 ``n == 0`` 的结果。

    怎么算的:把该帧自己的像素网格经**天球**打到参考帧的像素里(两次精确投影,
    不做小角近似),再最小二乘拟一个**非反射**相似变换(复数形式的 Procrustes:
    ``q ≈ a·p + b``,``|a|`` 是缩放、``arg a`` 是旋转)。残差就是这个模型套不上
    的部分。宇称不同的帧非反射相似变换套不上是**故意的** —— 那种情况本来就得
    先翻转,残差大正是要告诉你这件事。
    """
    items = list(results) if results is not None else []
    keep_w, keep_o, keep_i = [], [], []
    skipped = 0
    for i, it in enumerate(items):
        w = _wcs_of(it)
        if w is None:
            skipped += 1
            continue
        keep_w.append(w)
        keep_o.append(it)
        keep_i.append(i)
    n = len(keep_w)
    if n == 0:
        z = np.zeros(0)
        return StackAlignment(int(ref), np.zeros(0, dtype=np.int64), z, z, z, z,
                              z, z, z, z, np.zeros(0, dtype=bool), skipped)
    if int(ref) not in keep_i:
        raise WcsAppsError(_("参考帧下标 %r 没有可用的 WCS") % (ref,))
    s = int(samples)
    if s < 2:
        raise WcsAppsError(_("samples 至少为 2"))
    r = keep_i.index(int(ref))
    w_ref = keep_w[r]
    size_ref = _size_of(keep_o[r], width, height)
    if size_ref is None:
        raise WcsAppsError(_("参考帧没带图幅尺寸, 请给 width/height"))
    wr, hr = size_ref
    cx_ref, cy_ref = (wr + 1.0) / 2.0, (hr + 1.0) / 2.0
    flip_ref = w_ref.flipped()

    fp_ref = footprint(w_ref, wr, hr)
    plane_center = fp_ref.center
    ref_xy = _to_plane(plane_center, fp_ref)
    ref_area = fp_ref.area_deg2()

    out = {k: np.zeros(n) for k in ("dx", "dy", "sh", "rot", "sc", "rms", "mx", "ov")}
    parity = np.zeros(n, dtype=bool)
    for i, (w, o) in enumerate(zip(keep_w, keep_o)):
        size = _size_of(o, width, height)
        if size is None:
            raise WcsAppsError(_("第 %d 帧没带图幅尺寸, 请给 width/height") % keep_i[i])
        wi, hi = size
        parity[i] = bool(w.flipped() != flip_ref)

        gx, gy = _pixel_grid(wi, hi, s)
        ra, dec = pixel_to_world(w, gx, gy)
        qx, qy = world_to_pixel(w_ref, ra, dec)
        qx = np.asarray(qx).ravel()
        qy = np.asarray(qy).ravel()
        good = np.isfinite(qx) & np.isfinite(qy)
        if int(good.sum()) < 3:
            out["rot"][i] = out["sc"][i] = float("nan")
            out["dx"][i] = out["dy"][i] = out["sh"][i] = float("nan")
            out["rms"][i] = out["mx"][i] = float("inf")
            continue
        p = gx.ravel()[good] + 1j * gy.ravel()[good]
        q = qx[good] + 1j * qy[good]
        a, b, resid = _fit_similarity(p, q)
        out["rot"][i] = float(math.degrees(np.angle(a)))
        out["sc"][i] = float(abs(a))
        out["rms"][i] = float(np.sqrt(np.mean(resid ** 2)))
        out["mx"][i] = float(resid.max())

        ra_c, dec_c = pixel_to_world(w, (wi + 1.0) / 2.0, (hi + 1.0) / 2.0)
        xr, yr = world_to_pixel(w_ref, ra_c, dec_c)
        dx = float(xr) - cx_ref
        dy = float(yr) - cy_ref
        out["dx"][i], out["dy"][i] = dx, dy
        out["sh"][i] = math.hypot(dx, dy) if math.isfinite(dx + dy) else float("nan")
        out["ov"][i] = _overlap_fraction(plane_center, ref_xy, ref_area,
                                         footprint(w, wi, hi))
    return StackAlignment(
        ref=int(ref), index=np.asarray(keep_i, dtype=np.int64),
        dx_px=out["dx"], dy_px=out["dy"], shift_px=out["sh"],
        rotation_deg=_wrap180(out["rot"]), scale=out["sc"],
        rms_px=out["rms"], max_px=out["mx"], overlap_frac=out["ov"],
        parity_mismatch=parity, n_skipped=skipped)


def _pixel_grid(w: int, h: int, s: int):
    """图幅内 ``s×s`` 等分**格心**的采样点(FITS 1-based),供相似变换拟合用。

    取格心而不是取到 ``0.5`` / ``N+0.5`` 两条外边界:边界点经"像素→天球→像素"
    往返后会落在 ``0.4999999…``,任何"落在框内吗"的判据都会在那儿抖;格心离边界
    半格,不碰这类浮点边界问题,而且"按面积等分"也是拟合权重该有的分布。
    """
    xs = 0.5 + (np.arange(s, dtype=np.float64) + 0.5) * (float(w) / s)
    ys = 0.5 + (np.arange(s, dtype=np.float64) + 0.5) * (float(h) / s)
    return np.meshgrid(xs, ys)


def _to_plane(center, fp: Footprint) -> np.ndarray | None:
    """足迹顶点 → 公共切平面坐标 ``(n, 2)``;有顶点在背面则返回 ``None``。"""
    xi, eta, ok = _project_tangent(center, fp.radec[:, 0], fp.radec[:, 1])
    if not bool(np.all(ok)):
        return None
    return np.column_stack([np.asarray(xi), np.asarray(eta)])


def _overlap_fraction(center, ref_xy, ref_area: float, other: Footprint) -> float:
    """参考帧有多大比例落在 ``other`` 里 —— **精确**的凸多边形求交。"""
    if ref_xy is None or ref_area <= 0.0:
        return float("nan")
    clip = _to_plane(center, other)
    if clip is None:
        return float("nan")
    inter = _clip_convex(ref_xy, clip)
    if len(inter) < 3:
        return 0.0
    ra, dec = _deproject_tangent(center, inter[:, 0], inter[:, 1])
    area = footprint_area_deg2(np.column_stack([np.asarray(ra), np.asarray(dec)]))
    return float(min(1.0, area / ref_area))


def _signed_area_2d(poly: np.ndarray) -> float:
    """平面多边形的有向面积(鞋带公式),用来统一绕向。"""
    x, y = poly[:, 0], poly[:, 1]
    return 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def _clip_convex(subject: np.ndarray, clip: np.ndarray) -> np.ndarray:
    """Sutherland–Hodgman:用**凸**多边形 ``clip`` 裁 ``subject``(平面坐标)。

    视场四边形在 gnomonic 平面上是直边凸四边形(投影是射影变换,直线还是直线),
    所以这一步是精确的,不是近似。
    """
    if _signed_area_2d(clip) < 0.0:
        clip = clip[::-1]
    out = subject
    n = len(clip)
    for i in range(n):
        if len(out) == 0:
            return np.zeros((0, 2))
        a, b = clip[i], clip[(i + 1) % n]
        ex, ey = b[0] - a[0], b[1] - a[1]
        side = ex * (out[:, 1] - a[1]) - ey * (out[:, 0] - a[0])
        keep = side >= 0.0
        nxt = []
        m = len(out)
        for j in range(m):
            k = (j + 1) % m
            if keep[j]:
                nxt.append(out[j])
            if keep[j] != keep[k]:
                d = side[j] - side[k]
                if d != 0.0:
                    nxt.append(out[j] + (out[k] - out[j]) * (side[j] / d))
        out = np.array(nxt, dtype=np.float64) if nxt else np.zeros((0, 2))
    return out


def _fit_similarity(p: np.ndarray, q: np.ndarray):
    """复数最小二乘拟合 ``q ≈ a·p + b``(非反射相似变换)。

    ``|a|`` = 缩放,``arg a`` = 逆时针旋转角。返回 ``(a, b, |残差|)``。
    源点全重合时退化成纯平移(``a = 1``)。
    """
    pm = p.mean()
    qm = q.mean()
    u = p - pm
    v = q - qm
    den = float(np.vdot(u, u).real)
    a = complex(np.vdot(u, v) / den) if den > 0.0 else 1.0 + 0.0j
    b = qm - a * pm
    return a, b, np.abs(a * p + b - q)


# ---------------------------------------------------------- 8. 盲解搜索计划


#: 环间距 / 环内弧长相对搜索半径的倍数。取 √2 的道理:一个格点最坏落在"半个环距
#: + 半个弧距"的角上,距离 = √((√2r/2)² + (√2r/2)²) = r,刚好被覆盖。
_GRID_STEP_FACTOR = math.sqrt(2.0)


def sky_grid(radius_deg: float, *, dec_range=(-90.0, 90.0), near=None
             ) -> np.ndarray:
    """全天(或指定赤纬带)的**覆盖网格**中心,``(n, 2)`` 的 ``(ra, dec)`` 度。

    :param radius_deg: 每个中心的覆盖半径(度,``0 < r ≤ 90``)。保证
        **赤纬带内任意一点到最近中心的角距 ≤ radius_deg**(单测拿 20 万个随机
        点钉死)。
    :param dec_range: ``(下限, 上限)`` 赤纬(度)。只想搜地平线以上的天区就传
        它 —— 31°N 的站点看不到 dec < -59°,砍掉能省一半以上的格点。
        位于边界上的点也保证被覆盖(带外的环只要还管着带内的点就会保留)。
    :param near: 给了 ``(ra, dec)`` 就按到它的角距**升序**排;不给按环序
        (赤纬从南到北、环内赤经递增)。
    :returns: ``(n, 2)`` 中心坐标。

    布局:等赤纬环 + 环内等赤经。环距 ``√2·r``、环内弧长 ``≤ √2·r``
    (弧长按该环负责的赤纬带里 **cos δ 最大**那一边算,偏保守);两极各放一个点,
    相邻环错开半格。点数约 ``41253 / (2 r²)``:``r=2°`` 约 5 千,``r=5°`` 约 830。
    """
    r = float(radius_deg)
    if not math.isfinite(r) or r <= 0.0 or r > 90.0:
        raise WcsAppsError(_("radius_deg 必须在 (0, 90] 内(给的是 %r)") % (radius_deg,))
    lo, hi = float(dec_range[0]), float(dec_range[1])
    if not (math.isfinite(lo) and math.isfinite(hi)) or lo > hi:
        raise WcsAppsError(_("dec_range 非法:%r") % (dec_range,))
    lo = max(-90.0, lo)
    hi = min(90.0, hi)

    step = _GRID_STEP_FACTOR * r
    n_ring = max(1, int(math.ceil(180.0 / step)))
    d_step = 180.0 / n_ring
    rows = []
    for j in range(n_ring + 1):
        dec = -90.0 + j * d_step
        band_lo = max(-90.0, dec - d_step / 2.0)
        band_hi = min(90.0, dec + d_step / 2.0)
        if band_hi < lo or band_lo > hi:
            continue                      # 这个环管的赤纬带整个在要求之外
        if abs(dec) >= 90.0 - 1e-9:
            rows.append(np.array([[0.0, math.copysign(90.0, dec)]]))
            continue
        # 该环负责的带里 cos δ 最大的那一边(带跨过赤道就是 1)
        cos_max = 1.0 if band_lo <= 0.0 <= band_hi else \
            math.cos(math.radians(min(abs(band_lo), abs(band_hi))))
        n_ra = max(1, int(math.ceil(360.0 * cos_max / step)))
        ra = (np.arange(n_ra, dtype=np.float64) + 0.5 * (j % 2)) * (360.0 / n_ra)
        rows.append(np.column_stack([ra % 360.0, np.full(n_ra, dec)]))
    out = np.vstack(rows) if rows else np.zeros((0, 2))
    if near is not None:
        n0 = np.asarray(near, dtype=np.float64).reshape(2)
        sep = np.asarray(angular_separation(n0[0], n0[1], out[:, 0], out[:, 1]))
        out = out[np.argsort(sep, kind="stable")]
    return out


def blind_hint_grid(pixel_scale: float, image_size=None, *, radius_deg=None,
                    base_hint: SolveHint | None = None,
                    dec_range=(-90.0, 90.0), near=None,
                    **overrides) -> list[SolveHint]:
    """没有任何指向先验时的**盲解搜索计划**:一组覆盖全天的
    :class:`~astro_smb.platesolve.SolveHint`,交给上层顺序去试。

    :param pixel_scale: 像素尺度(角秒/像素)。盲解必须知道尺度 —— 尺度也未知的
        真·全盲解本模块不做(得靠多尺度四元组索引,那是另一套东西)。
    :param image_size: ``(宽, 高)`` 全分辨率像素。给了就用**视场外接圆半径**
        当搜索半径。
    :param radius_deg: 直接指定每个 hint 的搜索半径(度),覆盖上面的推算。
        调大 ⇒ 格点少、每次搜得慢;调小 ⇒ 格点多、每次快。
    :param base_hint: 其余字段(``scale_tol`` / ``flipped`` / ``epoch`` /
        ``rotation_deg`` …)的模板,逐条 ``replace`` 出来。
    :param dec_range: 只搜这个赤纬带(见 :func:`sky_grid`)。
    :param near: 有粗略猜测(上一张图的解、赤道仪报的位置)就传进来,
        列表会按离它的远近排序 —— **顺序才是盲解快慢的关键**。
    :param overrides: 直接盖到每个 hint 上的字段。
    :returns: ``list[SolveHint]``,每条带 ``ra_deg`` / ``dec_deg`` /
        ``radius_deg`` / ``pixel_scale``,``source`` 写成 ``"盲解网格 i/N"``。

    **本函数不跑解算**,只产计划;解算是 :func:`astro_smb.platesolve.solve`
    的事,上层自己按顺序喂、命中即停(记得设 ``time_budget``)。

    点数提醒:半径 2° 约 5 千个 hint。真要全盲,先用 ``dec_range`` 砍掉
    地平线以下、再用 ``near`` 排序,基本都能在前几十个里命中。
    """
    s = float(pixel_scale)
    if not math.isfinite(s) or s <= 0.0:
        raise WcsAppsError(_("pixel_scale 必须是正数(角秒/像素)"))
    size = None
    if image_size is not None:
        size = _check_size(image_size[0], image_size[1])
    if radius_deg is None:
        if size is None:
            raise WcsAppsError(_("要么给 image_size 让我推视场, 要么直接给 radius_deg"))
        radius_deg = 0.5 * math.hypot(size[0], size[1]) * s / 3600.0
    r = float(radius_deg)
    centers = sky_grid(r, dec_range=dec_range, near=near)

    base = base_hint if base_hint is not None else SolveHint()
    total = len(centers)
    out: list[SolveHint] = []
    for i, (ra, dec) in enumerate(centers):
        kw = dict(ra_deg=float(ra), dec_deg=float(dec), pixel_scale=s,
                  radius_deg=r, source=_("盲解网格 %d/%d") % (i + 1, total))
        if size is not None:
            kw["image_size"] = size
        kw.update(overrides)
        out.append(replace(base, **kw))
    return out
