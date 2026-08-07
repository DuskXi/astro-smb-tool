"""TAN(gnomonic)投影与 WCS 拟合(纯 numpy,不依赖 astropy)。

板解算的几何底座。三件事:

1. **投影**(:func:`world_to_pixel` / :func:`pixel_to_world`)—— FITS 标准的
   ``RA---TAN`` / ``DEC--TAN`` 切平面投影,全向量化,过极点与 RA 跨 0° 都正确。
   实现走**单位向量三元组**而不是教科书上的球面三角闭式:
   在 CRVAL 处建正交基 ``(x̂ 指向中心, ê 东, n̂ 北)``,则

       正投影: d = u·x̂, ξ = (u·ê)/d, η = (u·n̂)/d
       反投影: v = x̂ + ξ·ê + η·n̂    (无需归一化, atan2 自洽)

   这样**没有 ρ→0 的除零特例、没有极点特例**,往返误差 ~1e-13 度。
   ``(ξ, η)`` 就是 FITS 的中间世界坐标(intermediate world coordinates),
   它们以**度**表示时等于 ``degrees(tan(角距))·方向余弦`` —— 换句话说,把
   头里的 ``CDi_j``(度/像素)乘上像素偏移得到的就是 ``(ξ, η)`` 的度值,
   再 :func:`math.radians` 一下才是真正的切平面坐标(正切值)。

2. **拟合**(:func:`fit_tan` / :func:`fit_tan_sigma_clip`)—— 由匹配好的
   「像素坐标 ↔ 天球坐标」星对最小二乘反解 WCS。用**两次 3 参数 lstsq**,
   **CRPIX 偏移必须和 CD 一起解**(见 :func:`fit_tan` 的说明与
   ``tests/test_wcs.py::TestCrpixMustBeSolvedJointly``:假定一个错的 CRPIX
   只解 CD,残差会大到几十像素)。

3. **互转**(:func:`to_fits_cards` / :func:`from_fits_cards`)—— 解算结果能写回
   FITS 头,别人写好的 WCS(含 ASIAIR 自己解算回写的那份)也能读回来。

坐标约定(**极易搞错,改代码前先读**)
--------------------------------------

* ``crpix`` / :func:`world_to_pixel` 返回的 ``(x, y)`` 一律是 **FITS 约定:
  1-based、存储序**。第一个像素的中心是 ``(1.0, 1.0)``,它的左下边界是
  ``(0.5, 0.5)``;``y`` 沿 FITS ``NAXIS2`` 自底向上增长。
* :func:`astro_smb.fitsimage.decode_pixels` 返回的 numpy 数组是 **0-based、
  行在前、且已按 ROWORDER 上下翻转**(ASIAIR 从不写 ROWORDER ⇒ 恒翻转)。
  两套坐标的换算封装在 :func:`array_to_fits_xy` / :func:`fits_to_array_xy`,
  **提星后一律先过这两个函数再进投影**,别手写 ``H - row``。
* 超像素(2×2 CFA → 1 像素)平面上的 ``(c, r)`` 对应全分辨率数组的
  ``(2c + 0.5, 2r + 0.5)``,再套上面的换算即得 FITS 坐标。

畸变(SIP):本模块**只做线性 CD**,不解 SIP
--------------------------------------------

判据是残差::func:`fit_tan` 返回的 ``rms``(像素)如果**显著大于星点质心精度**
(实测超像素平面上质心精度约 0.1~0.3 px),差值就是畸变地板,提示"可能需要畸变项"。
真机标定值(ASIAIR + ASI2600MC,把每帧 SIP 位移场扣掉最佳仿射后的残余):

===============  ==============  ==============
目标              残差 RMS(px)   残差 MAX(px)
===============  ==============  ==============
NGC 2237          0.12 – 0.28     0.35 – 0.81
M 31              0.52 – 0.71     1.67 – 2.55
NGC 1499          1.33 – 1.41     4.83 – 5.15
M 16              1.27 – 1.70     4.45 – 6.41
===============  ==============  ==============

即**纯 TAN 在这套设备上的残差地板本身就有 1.3~1.7 px**(边缘 5~6 px)。
所以判解算成功**要看内点数,不要卡死 RMS 阈值**;RMS 只用来判"要不要上畸变项"。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from astro_smb.i18n import gettext as _

__all__ = [
    "WcsError", "TanWcs",
    "radec_to_unit", "unit_to_radec", "angular_separation",
    "array_to_fits_xy", "fits_to_array_xy",
    "world_to_pixel", "pixel_to_world",
    "fit_tan", "fit_tan_sigma_clip", "residuals", "rms_px",
    "to_fits_cards", "from_fits_cards", "cards_have_sip",
]


class WcsError(ValueError):
    """WCS 参数非法,或拟合无法进行(点太少/共线/重复/含 NaN/在切平面背面)。"""


# ------------------------------------------------------------ 球面基础工具


def radec_to_unit(ra_deg, dec_deg) -> np.ndarray:
    """(ra, dec) 度 → 单位向量,返回 ``(..., 3)``(x 指向春分点, z 指向北天极)。"""
    ra = np.radians(np.asarray(ra_deg, dtype=np.float64))
    dec = np.radians(np.asarray(dec_deg, dtype=np.float64))
    ra, dec = np.broadcast_arrays(ra, dec)
    cos_d = np.cos(dec)
    return np.stack([cos_d * np.cos(ra), cos_d * np.sin(ra), np.sin(dec)], axis=-1)


def unit_to_radec(vec) -> tuple[np.ndarray, np.ndarray]:
    """``(..., 3)`` 向量 → (ra, dec) 度。**不要求已归一化**(全用 atan2)。"""
    v = np.asarray(vec, dtype=np.float64)
    if v.shape[-1] != 3:
        raise WcsError(_("向量最后一维必须是 3"))
    ra = np.degrees(np.arctan2(v[..., 1], v[..., 0])) % 360.0
    dec = np.degrees(np.arctan2(v[..., 2], np.hypot(v[..., 0], v[..., 1])))
    return ra, dec


def angular_separation(ra1, dec1, ra2, dec2):
    """球面角距(度)。Vincenty 形式 —— 小角度和 180° 附近都不掉精度。

    标量进标量出,数组进数组出(广播)。
    """
    a1 = np.radians(np.asarray(ra1, dtype=np.float64))
    d1 = np.radians(np.asarray(dec1, dtype=np.float64))
    a2 = np.radians(np.asarray(ra2, dtype=np.float64))
    d2 = np.radians(np.asarray(dec2, dtype=np.float64))
    scalar = all(x.ndim == 0 for x in (a1, d1, a2, d2))
    dlon = a2 - a1
    sin_d, cos_d = np.sin(dlon), np.cos(dlon)
    cos1, sin1 = np.cos(d1), np.sin(d1)
    cos2, sin2 = np.cos(d2), np.sin(d2)
    num = np.hypot(cos2 * sin_d, cos1 * sin2 - sin1 * cos2 * cos_d)
    den = sin1 * sin2 + cos1 * cos2 * cos_d
    sep = np.degrees(np.arctan2(num, den))
    return float(sep) if scalar else sep


# ------------------------------------------------------- 数组 ↔ FITS 像素坐标


def array_to_fits_xy(col, row, height: int):
    """numpy 数组坐标(0-based, 已翻转的显示序)→ FITS 像素坐标(1-based)。

    ``height`` 是 FITS ``NAXIS2``(= 数组行数)。约定见模块 docstring::

        fits_x = col + 1
        fits_y = height - row

    校验:``row=0``(数组第一行 = 图像顶部)→ ``y = height``;
    ``row = height-1``(底部)→ ``y = 1``。
    """
    c = np.asarray(col, dtype=np.float64)
    r = np.asarray(row, dtype=np.float64)
    scalar = c.ndim == 0 and r.ndim == 0
    x = c + 1.0
    y = float(height) - r
    return (float(x), float(y)) if scalar else (x, y)


def fits_to_array_xy(x, y, height: int):
    """:func:`array_to_fits_xy` 的逆:FITS 像素坐标 → numpy 数组 (col, row)。"""
    xa = np.asarray(x, dtype=np.float64)
    ya = np.asarray(y, dtype=np.float64)
    scalar = xa.ndim == 0 and ya.ndim == 0
    col = xa - 1.0
    row = float(height) - ya
    return (float(col), float(row)) if scalar else (col, row)


# ------------------------------------------------------------------ TanWcs


def _inv2(m: np.ndarray) -> np.ndarray:
    """2×2 逆矩阵(手写,免走 lapack)。调用方保证行列式非零。"""
    det = m[0, 0] * m[1, 1] - m[0, 1] * m[1, 0]
    return np.array([[m[1, 1], -m[0, 1]], [-m[1, 0], m[0, 0]]],
                    dtype=np.float64) / det


@dataclass(frozen=True, eq=False)
class TanWcs:
    """一份 TAN(gnomonic)WCS。

    :param crval: 参考点天球坐标 ``(ra, dec)``,**度**。
    :param crpix: 参考点像素坐标 ``(x, y)``,**FITS 约定:1-based、存储序**
        (第一个像素中心 = ``(1.0, 1.0)``)。见模块 docstring 的坐标约定一节。
    :param cd: 2×2 矩阵 ``[[CD1_1, CD1_2], [CD2_1, CD2_2]]``,**度/像素**。
        把像素偏移 ``(x - crpix1, y - crpix2)`` 映到中间世界坐标 ``(ξ, η)``
        —— ξ 朝**东**(赤经增大)、η 朝**北**。

    ``eq=False``:字段里有 ndarray,自动生成的 ``__eq__`` 会返回数组导致真值歧义;
    需要比较请用 :func:`angular_separation` / ``np.allclose`` 逐项比。
    """

    crval: tuple[float, float]
    crpix: tuple[float, float]
    cd: np.ndarray

    def __post_init__(self) -> None:
        try:
            cd = np.array(self.cd, dtype=np.float64).reshape(2, 2)
        except (ValueError, TypeError) as exc:
            raise WcsError(_("cd 必须是 2×2 数值矩阵")) from exc
        if not np.all(np.isfinite(cd)):
            raise WcsError(_("cd 含 NaN/Inf"))
        det = cd[0, 0] * cd[1, 1] - cd[0, 1] * cd[1, 0]
        if abs(det) < 1e-30:
            raise WcsError(_("cd 行列式为 0(退化的线性变换)"))
        try:
            ra = float(self.crval[0])
            dec = float(self.crval[1])
            px = float(self.crpix[0])
            py = float(self.crpix[1])
        except (TypeError, ValueError, IndexError) as exc:
            raise WcsError(_("crval / crpix 必须是两个数")) from exc
        if not all(math.isfinite(v) for v in (ra, dec, px, py)):
            raise WcsError(_("crval / crpix 含 NaN/Inf"))
        if not -90.0 <= dec <= 90.0:
            raise WcsError(_("crval 的赤纬必须在 [-90, 90] 内"))
        object.__setattr__(self, "crval", (ra % 360.0, dec))
        object.__setattr__(self, "crpix", (px, py))
        object.__setattr__(self, "cd", cd)

    # -------- 派生量

    def cd_inv(self) -> np.ndarray:
        """``cd`` 的逆(中间世界坐标 → 像素偏移)。"""
        return _inv2(self.cd)

    def det(self) -> float:
        """``cd`` 的行列式(度²/像素²)。符号即宇称,见 :meth:`flipped`。"""
        cd = self.cd
        return float(cd[0, 0] * cd[1, 1] - cd[0, 1] * cd[1, 0])

    def pixel_scale(self) -> float:
        """像素尺度(**角秒/像素**),取两轴几何平均 ``sqrt(|det|)``。

        两轴不等时用 :meth:`pixel_scale_xy`。
        """
        return math.sqrt(abs(self.det())) * 3600.0

    def pixel_scale_xy(self) -> tuple[float, float]:
        """两条像素轴各自的尺度(角秒/像素)= CD 的列范数。"""
        cd = self.cd
        sx = math.hypot(cd[0, 0], cd[1, 0]) * 3600.0
        sy = math.hypot(cd[0, 1], cd[1, 1]) * 3600.0
        return sx, sy

    def rotation_deg(self) -> float:
        """图像 **+y 轴**的位置角(度,0~360,**从北起向东量**)。

        即 ``atan2(CD1_2, CD2_2)``:北上东左的常规取向为 0°。
        与 ZWO 日志里的 ``Angle`` 不是同一个量 —— 实测那个约等于
        ``(degrees(atan2(CD2_1, CD1_1)) + 180) mod 360``。
        """
        cd = self.cd
        return math.degrees(math.atan2(cd[0, 1], cd[1, 1])) % 360.0

    def flipped(self) -> bool:
        """是否**镜像**(天球宇称翻转)。

        常规(北上、东在左)取向的 CD 行列式为**负**;``det > 0`` 即镜像。
        ASIAIR 写出的 light 帧实测恒为镜像(det > 0)。
        """
        return self.det() > 0.0

    def fov_deg(self, width: int, height: int) -> tuple[float, float]:
        """给定图幅像素尺寸,返回视场 ``(宽, 高)``,度。

        量的是两条中线两端(外边界 0.5 与 N+0.5)之间的**大圆角距**,
        因此已包含 gnomonic 在边缘的拉伸。
        """
        cx = (float(width) + 1.0) / 2.0
        cy = (float(height) + 1.0) / 2.0
        ra_l, dec_l = pixel_to_world(self, 0.5, cy)
        ra_r, dec_r = pixel_to_world(self, float(width) + 0.5, cy)
        ra_b, dec_b = pixel_to_world(self, cx, 0.5)
        ra_t, dec_t = pixel_to_world(self, cx, float(height) + 0.5)
        return (float(angular_separation(ra_l, dec_l, ra_r, dec_r)),
                float(angular_separation(ra_b, dec_b, ra_t, dec_t)))

    def __repr__(self) -> str:  # pragma: no cover - 仅调试可读性
        return ("TanWcs(crval=(%.7f, %.7f), crpix=(%.3f, %.3f), "
                "scale=%.4f\"/px, rot=%.3f°, flipped=%s)"
                % (self.crval[0], self.crval[1], self.crpix[0], self.crpix[1],
                   self.pixel_scale(), self.rotation_deg(), self.flipped()))


# ------------------------------------------------------------------ 投影


def _basis(crval: tuple[float, float]) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """CRVAL 处的正交基 (x̂ 指向中心, ê 东, n̂ 北)。极点处依然良定义。"""
    ra0 = math.radians(crval[0])
    dec0 = math.radians(crval[1])
    ca, sa = math.cos(ra0), math.sin(ra0)
    cd_, sd = math.cos(dec0), math.sin(dec0)
    x_hat = np.array([cd_ * ca, cd_ * sa, sd])
    e_hat = np.array([-sa, ca, 0.0])
    n_hat = np.array([-sd * ca, -sd * sa, cd_])
    return x_hat, e_hat, n_hat


# 切平面正面的判据下限:``cos(夹角) > _MIN_COS``。
# 取 0 会让"恰好 90°"的点由浮点噪声决定死活(实测 cos 算出来是 ±6e-17,
# 正号时投出 1e16 像素这种看着像数的垃圾值)。1e-12 对应约 1e12 度的投影值,
# 真实视场离它有十几个数量级,不会误伤。
_MIN_COS = 1e-12


def _project(crval: tuple[float, float], ra, dec):
    """(ra, dec) → 中间世界坐标 ``(x_deg, y_deg)`` 与 ``ok``(是否在切平面正面)。"""
    u = radec_to_unit(ra, dec)
    x_hat, e_hat, n_hat = _basis(crval)
    d = u @ x_hat
    ok = d > _MIN_COS
    with np.errstate(divide="ignore", invalid="ignore"):
        xi = (u @ e_hat) / d
        eta = (u @ n_hat) / d
    return np.degrees(xi), np.degrees(eta), ok


def world_to_pixel(wcs: TanWcs, ra, dec):
    """天球坐标 → 像素坐标(**FITS 1-based**)。向量化,标量进标量出。

    切平面**背面**(与 CRVAL 夹角 ≥ 90°)的点无法投影,返回 ``NaN``。
    RA 跨 0°、过极点都正确(实现走单位向量,没有分支)。
    """
    ra_a = np.asarray(ra, dtype=np.float64)
    dec_a = np.asarray(dec, dtype=np.float64)
    scalar = ra_a.ndim == 0 and dec_a.ndim == 0
    x_deg, y_deg, ok = _project(wcs.crval, ra_a, dec_a)
    inv = wcs.cd_inv()
    dx = inv[0, 0] * x_deg + inv[0, 1] * y_deg
    dy = inv[1, 0] * x_deg + inv[1, 1] * y_deg
    x = np.where(ok, dx + wcs.crpix[0], np.nan)
    y = np.where(ok, dy + wcs.crpix[1], np.nan)
    return (float(x), float(y)) if scalar else (x, y)


def pixel_to_world(wcs: TanWcs, x, y):
    """像素坐标(**FITS 1-based**)→ 天球坐标 (ra, dec) 度。向量化。"""
    xa = np.asarray(x, dtype=np.float64)
    ya = np.asarray(y, dtype=np.float64)
    scalar = xa.ndim == 0 and ya.ndim == 0
    xa, ya = np.broadcast_arrays(xa, ya)
    dx = xa - wcs.crpix[0]
    dy = ya - wcs.crpix[1]
    cd = wcs.cd
    xi = np.radians(cd[0, 0] * dx + cd[0, 1] * dy)
    eta = np.radians(cd[1, 0] * dx + cd[1, 1] * dy)
    x_hat, e_hat, n_hat = _basis(wcs.crval)
    v = (x_hat + xi[..., None] * e_hat + eta[..., None] * n_hat)
    ra, dec = unit_to_radec(v)
    return (float(ra), float(dec)) if scalar else (ra, dec)


# ------------------------------------------------------------------ 拟合


def _prepare_pairs(xy, radec) -> tuple[np.ndarray, np.ndarray]:
    a = np.asarray(xy, dtype=np.float64)
    b = np.asarray(radec, dtype=np.float64)
    if a.ndim != 2 or a.shape[1] != 2:
        raise WcsError(_("xy 必须是 (n, 2) 数组"))
    if b.ndim != 2 or b.shape[1] != 2:
        raise WcsError(_("radec 必须是 (n, 2) 数组"))
    if a.shape[0] != b.shape[0]:
        raise WcsError(_("xy 与 radec 的星数不一致(%d vs %d)")
                       % (a.shape[0], b.shape[0]))
    if not np.all(np.isfinite(a)) or not np.all(np.isfinite(b)):
        raise WcsError(_("匹配星对里含 NaN/Inf"))
    return a, b


def _spherical_mean(radec: np.ndarray) -> tuple[float, float]:
    """一组天球坐标的方向平均(用作初始切点,保证切平面近似最好)。"""
    u = radec_to_unit(radec[:, 0], radec[:, 1])
    m = u.mean(axis=0)
    norm = float(np.linalg.norm(m))
    if norm < 1e-9:
        raise WcsError(_("匹配星对散布在整个天球, 无法确定切点"))
    ra, dec = unit_to_radec(m / norm)
    return float(ra), float(dec)


def _lstsq_affine(xy: np.ndarray, xieta: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """解 ``ξ = CD·(p - crpix)`` 的等价仿射形式 ``ξ = CD·p + c``。

    两次 3 参数最小二乘(共用同一个设计矩阵)。设计矩阵先做中心化 + 归一化
    以改善条件数,这样秩/奇异值判据才有意义。返回 ``(cd, c)``。
    """
    ctr = xy.mean(axis=0)
    d = xy - ctr
    span = float(np.sqrt(np.mean(d[:, 0] ** 2 + d[:, 1] ** 2)))
    if span < 1e-12:
        raise WcsError(_("匹配点全部重合, 无法确定线性变换"))
    u = d / span
    design = np.column_stack([u[:, 0], u[:, 1], np.ones(len(u))])
    sol, _res, rank, sv = np.linalg.lstsq(design, xieta, rcond=None)
    if rank < 3 or sv[-1] <= 1e-8 * sv[0]:
        raise WcsError(_("匹配点退化(共线或重复), 无法唯一确定线性变换"))
    cd = np.array([[sol[0, 0], sol[1, 0]],
                   [sol[0, 1], sol[1, 1]]], dtype=np.float64) / span
    c = np.array([sol[2, 0], sol[2, 1]], dtype=np.float64) - cd @ ctr
    return cd, c


def _standard_coords(crval: tuple[float, float], radec: np.ndarray) -> np.ndarray:
    x_deg, y_deg, ok = _project(crval, radec[:, 0], radec[:, 1])
    if not bool(np.all(ok)):
        raise WcsError(_("有匹配星落在切平面背面(与视场中心夹角 ≥ 90°)"))
    return np.column_stack([x_deg, y_deg])


def fit_tan(xy, radec, crpix_guess=None, *, max_iter: int = 8,
            tol: float = 1e-10) -> tuple[TanWcs, float, np.ndarray]:
    """由匹配星对最小二乘拟合 TAN WCS。

    :param xy: ``(n, 2)`` 像素坐标,**FITS 1-based**(见模块 docstring)。
    :param radec: ``(n, 2)`` 对应的天球坐标(度),**须与 xy 一一配对**。
    :param crpix_guess: 可选的 ``(x, y)``。给了就把 CRPIX **钉死**在这个像素上
        (通常传图幅中心),CRVAL 迭代成"该像素对应的天球坐标"——写回 FITS 头时
        这样最规整。不给则 CRVAL 取匹配星的方向平均、CRPIX 由拟合反解。

        **切点必须落在视场里**。绕 A 点的 gnomonic 与绕 B 点的 gnomonic 之间差一个
        **射影**变换,不是仿射 —— 切点离视场越远,"TAN + 线性 CD"这个参数化本身
        就装不下这个场,残差凭空长出来。6248×4176 @ 1.93"/px 实测:

        ====================  ==========
        crpix_guess 离中心      拟合 RMS
        ====================  ==========
        0 px(图幅中心)        1e-11 px
        不给(方向平均)        0.011 px
        3620 px(≈1.9°)        1.00 px
        7804 px(≈4.2°)        1.85 px
        ====================  ==========

        —— 这不是数值误差,是参数化误差,加多少星都不会变小。
    :returns: ``(wcs, rms, resid)``。``rms`` 是像素残差的二维均方根
        ``sqrt(mean(dx² + dy²))``;``resid`` 是 ``(n, 2)`` 的 ``预测 - 观测``。

    **CRPIX 必须与 CD 一起解**:模型写成 ``ξ = CD·p + c`` 后用 3 参数
    lstsq(``[x, y, 1]``),再由 ``crpix = -CD⁻¹·c`` 反解。若图省事假定一个
    CRPIX(比如图幅中心)只解 2 参数的 CD,指向先验的那点误差会整个折进 CD,
    实测残差会大到几十像素(单测 ``TestCrpixMustBeSolvedJointly`` 钉死)。

    **不解 SIP 畸变**。``rms`` 显著大于星点质心精度(超像素平面实测 0.1~0.3 px)
    时,超出的部分就是畸变地板,提示"可能需要畸变项";模块 docstring 有真机标定表。
    """
    a, b = _prepare_pairs(xy, radec)
    if len(a) < 3:
        raise WcsError(_("至少需要 3 对匹配星才能拟合 TAN WCS(给了 %d 对)") % len(a))

    crval = _spherical_mean(b)
    guess = None
    if crpix_guess is not None:
        guess = (float(crpix_guess[0]), float(crpix_guess[1]))
        if not all(math.isfinite(v) for v in guess):
            raise WcsError(_("crpix_guess 含 NaN/Inf"))
        # 迭代:把切点挪到 crpix_guess 所对应的天球位置(二次收敛,几轮即可)
        for _i in range(max_iter):
            cd, c = _lstsq_affine(a, _standard_coords(crval, b))
            crpix_fit = -(_inv2(cd) @ c)
            probe = TanWcs(crval, (float(crpix_fit[0]), float(crpix_fit[1])), cd)
            new_ra, new_dec = pixel_to_world(probe, guess[0], guess[1])
            moved = angular_separation(crval[0], crval[1], new_ra, new_dec)
            crval = (float(new_ra), float(new_dec))
            if moved < tol:
                break

    cd, c = _lstsq_affine(a, _standard_coords(crval, b))
    if guess is None:
        crpix_fit = -(_inv2(cd) @ c)
        crpix = (float(crpix_fit[0]), float(crpix_fit[1]))
    else:
        crpix = guess
    wcs = TanWcs(crval, crpix, cd)
    resid = residuals(wcs, a, b)
    return wcs, rms_px(resid), resid


def residuals(wcs: TanWcs, xy, radec) -> np.ndarray:
    """每对匹配星的像素残差 ``(n, 2)``,定义为 **预测 - 观测**。

    预测 = 把 ``radec`` 用 ``wcs`` 正投影得到的像素坐标。
    """
    a, b = _prepare_pairs(xy, radec)
    px, py = world_to_pixel(wcs, b[:, 0], b[:, 1])
    return np.column_stack([np.asarray(px) - a[:, 0], np.asarray(py) - a[:, 1]])


def rms_px(resid) -> float:
    """由 ``(n, 2)`` 残差算二维均方根(像素)。空输入返回 0。"""
    r = np.asarray(resid, dtype=np.float64)
    if r.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(r[:, 0] ** 2 + r[:, 1] ** 2)))


def _clip_threshold(radius: np.ndarray, sigma: float) -> float:
    """稳健离群阈值:``median + sigma × 1.4826 × MAD``,带绝对下限。

    下限 1e-6 px 是为了无噪合成数据 —— 那时残差只有 1e-12 量级且 MAD≈0,
    没有下限会把一半的点当成"离群"剔掉。
    """
    med = float(np.median(radius))
    mad = float(np.median(np.abs(radius - med)))
    return max(med + sigma * 1.4826 * mad, 1e-6)


def fit_tan_sigma_clip(xy, radec, *, crpix_guess=None, sigma: float = 3.0,
                       max_iter: int = 5, min_keep: int = 6
                       ) -> tuple[TanWcs, float, np.ndarray, np.ndarray]:
    """带 sigma 剔除的迭代拟合:剔掉离群星对后重拟合。

    :returns: ``(wcs, rms, resid, keep)``。``rms`` 只统计**内点**;
        ``resid`` 是**全部** ``n`` 对的 ``(n, 2)`` 残差(被剔的也在里面,方便复查);
        ``keep`` 是 ``(n,)`` 布尔内点掩码。

    阈值用 MAD 稳健尺度而不是 std —— 误匹配的残差往往是几十像素,
    std 会被它自己顶高到剔不掉。剩余内点少于 ``min_keep`` 时停止剔除
    (宁可保留可疑点也不要拟合到一个欠定的解上)。
    """
    a, b = _prepare_pairs(xy, radec)
    n = len(a)
    if n < 3:
        raise WcsError(_("至少需要 3 对匹配星才能拟合 TAN WCS(给了 %d 对)") % n)
    keep = np.ones(n, dtype=bool)
    for _i in range(max_iter):
        wcs, _rms, _r = fit_tan(a[keep], b[keep], crpix_guess)
        resid = residuals(wcs, a, b)
        radius = np.hypot(resid[:, 0], resid[:, 1])
        thr = _clip_threshold(radius[keep], sigma)
        new_keep = radius <= thr
        if int(new_keep.sum()) < max(min_keep, 3):
            break
        if np.array_equal(new_keep, keep):
            break
        keep = new_keep
    wcs, _rms, _r = fit_tan(a[keep], b[keep], crpix_guess)
    resid = residuals(wcs, a, b)
    return wcs, rms_px(resid[keep]), resid, keep


# ------------------------------------------------------------ FITS 卡片互转


def _fmt(v: float) -> str:
    """浮点 → FITS 卡片值字符串。用 repr 保证 float 往返无损。"""
    return repr(float(v))


def to_fits_cards(wcs: TanWcs) -> dict[str, str]:
    """WCS → 标准 FITS 关键字(值都是字符串,可直接并进
    :class:`~astro_smb.fitshdr.FitsHeader` 的 ``cards``)。

    写的是 CD 矩阵形式(不写 CDELT/CROTA2 这套弃用约定),不含 SIP。
    """
    return {
        "CTYPE1": "RA---TAN",
        "CTYPE2": "DEC--TAN",
        "CUNIT1": "deg",
        "CUNIT2": "deg",
        "EQUINOX": "2000.0",
        "RADESYS": "ICRS",
        "CRVAL1": _fmt(wcs.crval[0]),
        "CRVAL2": _fmt(wcs.crval[1]),
        "CRPIX1": _fmt(wcs.crpix[0]),
        "CRPIX2": _fmt(wcs.crpix[1]),
        "CD1_1": _fmt(wcs.cd[0, 0]),
        "CD1_2": _fmt(wcs.cd[0, 1]),
        "CD2_1": _fmt(wcs.cd[1, 0]),
        "CD2_2": _fmt(wcs.cd[1, 1]),
    }


def _as_cards(src) -> dict[str, str]:
    """接受 dict 或任何带 ``.cards`` 的对象(如 FitsHeader);键统一大写。"""
    if hasattr(src, "cards"):
        src = src.cards
    try:
        items = dict(src).items()
    except (TypeError, ValueError) as exc:
        raise WcsError(_("需要 dict 或带 .cards 的对象")) from exc
    return {str(k).strip().upper(): v for k, v in items}


def _num(cards: dict[str, str], key: str) -> float | None:
    v = cards.get(key)
    if v is None:
        return None
    try:
        f = float(str(v).strip())
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def cards_have_sip(cards) -> bool:
    """头里是否带 SIP 畸变项(CTYPE 带 ``-SIP`` 后缀,或有 ``A_ORDER``/``B_ORDER``)。

    ASIAIR 自己解算回写的 light 帧实测**都是** ``RA---TAN-SIP`` + 二阶系数;
    :func:`from_fits_cards` 只取其中的线性部分。
    """
    c = _as_cards(cards)
    for key in ("CTYPE1", "CTYPE2"):
        if str(c.get(key, "")).strip().upper().endswith("-SIP"):
            return True
    return any(k in c for k in ("A_ORDER", "B_ORDER", "AP_ORDER", "BP_ORDER"))


def from_fits_cards(cards) -> TanWcs | None:
    """标准 FITS 关键字 → :class:`TanWcs`;没有可用的 TAN WCS 时返回 ``None``。

    接受三种线性项写法(按优先级):``CDi_j`` > ``PCi_j`` + ``CDELTi`` >
    ``CDELTi`` + ``CROTA2``(弃用约定)。``RA---TAN-SIP`` 也接受,**只取线性部分**
    (用 :func:`cards_have_sip` 判断是否丢弃了畸变项)。

    ``CROTA2`` 用 Greisen & Calabretta 2002 Paper II 的标准式::

        CD1_1 =  CDELT1·cos ρ    CD1_2 = -CDELT2·sin ρ
        CD2_1 =  CDELT1·sin ρ    CD2_2 =  CDELT2·cos ρ

    注意它转的是**坐标轴**不是图像:常规取向下 ``CROTA2 = +ρ`` 读回来的
    :meth:`TanWcs.rotation_deg` 是 ``-ρ``(即 ``360-ρ``)。这是标准如此,
    不是 bug —— 单测 ``test_cdelt_plus_crota2`` 钉死了这个符号。
    """
    c = _as_cards(cards)
    t1 = str(c.get("CTYPE1", "")).strip().upper()
    t2 = str(c.get("CTYPE2", "")).strip().upper()
    if t1 not in ("RA---TAN", "RA---TAN-SIP"):
        return None
    if t2 not in ("DEC--TAN", "DEC--TAN-SIP"):
        return None

    crval1, crval2 = _num(c, "CRVAL1"), _num(c, "CRVAL2")
    crpix1, crpix2 = _num(c, "CRPIX1"), _num(c, "CRPIX2")
    if None in (crval1, crval2, crpix1, crpix2):
        return None

    cd_vals = [_num(c, k) for k in ("CD1_1", "CD1_2", "CD2_1", "CD2_2")]
    if all(v is not None for v in cd_vals):
        cd = np.array(cd_vals, dtype=np.float64).reshape(2, 2)
    else:
        d1, d2 = _num(c, "CDELT1"), _num(c, "CDELT2")
        if d1 is None or d2 is None:
            return None
        pc = [_num(c, k) for k in ("PC1_1", "PC1_2", "PC2_1", "PC2_2")]
        if all(v is not None for v in pc):
            m = np.array(pc, dtype=np.float64).reshape(2, 2)
            cd = np.array([[d1, d1], [d2, d2]], dtype=np.float64) * m
        else:
            rot = _num(c, "CROTA2") or 0.0
            cr, sr = math.cos(math.radians(rot)), math.sin(math.radians(rot))
            cd = np.array([[d1 * cr, -d2 * sr],
                           [d1 * sr, d2 * cr]], dtype=np.float64)
    try:
        return TanWcs((crval1, crval2), (crpix1, crpix2), cd)
    except WcsError:
        return None
