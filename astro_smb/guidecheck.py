"""导星质量**逆推验证**:用拍摄数据反证导星,而不是只看导星器自己的报告。

为什么需要这个模块
------------------
PHD2 的 RMS 只反映"**导星镜里那颗星**被摁得多稳"。它对主镜里实际发生了什么
一无所知 —— 导星曲线漂亮但目标在主镜画面里慢慢走掉,是天文摄影最隐蔽的失败
模式(差分挠曲、极轴误差、镜筒沉降都会这样)。

有了逐帧的**绝对天坐标**(板解算)与**星点形状**(星点提取)之后,就有了三条
**互相独立**的证据链:

==========  ==================================  ==============================
通道        测的是                              来源
==========  ==================================  ==============================
① 导星日志  导星器**以为**自己纠正了多少        :mod:`astro_smb.phd2log`
② 星点形状  该次**曝光期间**的实际抖动,含方向  :mod:`astro_smb.stars`
③ 板解算    帧间**绝对漂移**与**场旋**          :mod:`astro_smb.platesolve`
==========  ==================================  ==============================

**价值全在三者的分歧里**:

- ① 稳 + ③ 漂  ⇒ 主镜/导星镜差分挠曲,或极轴误差
- ① 抖 + ② 圆  ⇒ 导星镜的视宁度噪声,实际无害(很可能在过度纠正)
- ② 沿固定方位角拉长  ⇒ 直接读出误差轴向

极轴误差:为什么用数值正演而不是查公式
--------------------------------------
漂移法的闭式公式版本极多、符号约定互相打架(方位角零点、南北半球、时角正负),
记错一个符号就会**自信地给出错误结论** —— 而这个模块的全部价值就在于它比
PHD2 更可信,自己不准整条链就塌了。

所以这里**不抄公式**:直接建精确的正演模型 —— 把赤道仪的极轴按 (方位误差,
高度误差) 偏一点,让它绕这根**错误的轴**以恒星速率转,算出目标视位置随时间怎么
跑。全是旋转矩阵,没有近似,也没有需要记住的约定。反演就是对这个正演做最小
二乘。单测里注入已知偏差再反解,能回收到 0.1 角分以内。

坐标与单位约定(全模块统一)
--------------------------
- 角度一律**度**,时间一律**秒**,漂移速率一律**角秒/分钟**。
- RA 方向的量一律是**大圆距离**(已乘 ``cos(dec)``)—— 这是最容易错的一处。
- 时角 H:**向西为正**(天体过中天后 H>0)。
- 方位角 A:**北 0°、东 90°**(与 :mod:`astro_smb.astro` 一致)。
- 位置角 PA:与 :meth:`astro_smb.wcs.TanWcs.rotation_deg` 同一口径。

本模块是**纯计算**:只依赖 numpy 与标准库,不碰 SMB、不碰 GUI、不做 I/O。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np
from astro_smb.i18n import gettext as _

__all__ = [
    "SIDEREAL_DEG_PER_S",
    "DitherEvent",
    "dither_from_log_text",
    "PolarError",
    "simulate_track",
    "drift_rates",
    "fit_polar_error",
    "PolarCheck",
    "polar_from_runs",
    "DriftFit",
    "fit_center_drift",
    "RotationFit",
    "fit_position_angle",
    "drift_severity",
    "exposure_smear",
    "rotation_severity",
    "fwhm_budget",
    "sampling_quality",
    "FrameEvidence",
    "CrossCheck",
    "cross_validate",
]

# 恒星日 86164.0905 秒转 360° ⇒ 度/秒
SIDEREAL_DEG_PER_S = 360.0 / 86164.0905

# 采样判据(角秒/像素相对 FWHM):低于此值属欠采样,星点形状测不出导星误差
UNDERSAMPLED_RATIO = 1.6        # FWHM(像素) 小于 1.6 就当欠采样
WELL_SAMPLED_RATIO = 2.5        # 达到 2.5 像素以上认为采样充分

# 判读阈值
#
# 这里**刻意不用裸角秒速率当主判据**。真机(2026-07-29 NGC 7293,13x300s)打脸过:
# 漂移 0.19″/分低于 DRIFT_SIGNIFICANT,于是判成"good/高置信",而那一夜实际
# 跑了 65 分钟、累计 14″ = 7.3 主镜像素,场旋 3′。裸速率对**跑了多久**和
# **什么像素尺度**都是瞎的 —— 同样 0.30″/分,403mm 下是 0.16 像素/分,
# 2000mm 下就是 0.77 像素/分,伤害差 5 倍。所以判据一律换成"后果的单位"。
# **"目标走没走"只看一件事:逐帧板解算的绝对位置整段累计挪了多少。**
# 速率、曝光内涂抹、导星 RMS 都是另外的问题,不参与这个判定。
DRIFT_WALK_PX = 3.0             # 整段累计位移达到这么多主镜像素就算"目标在走"
DRIFT_WALK_ARCSEC = 10.0        # 拿不到像素尺度时的退路(角秒);换算不出伤害,只求不静默漏报
DRIFT_SIGNIFICANT = 0.30        # 角秒/分钟:仅供 DriftFit.significant 这个历史属性用
DRIFT_SMEAR_FRAC = 0.5          # 涂抹观察的措辞阈值(单帧内漂移 / 导星 RMS),不参与判定
ROT_CORNER_PX = 2.0             # 场旋在画幅角落造成的位移(主镜像素)
ROT_SNR = 3.0                   # 场旋线性趋势至少要是拟合残差的这个倍数才算真信号
ROT_FALLBACK_DEG_PER_HOUR = 0.05  # 没给画幅尺寸时的退路阈值(旧的任意值,仅为不静默漏报)
RMS_RATIO_SUSPECT = 2.0         # 实测抖动 / PHD2 报告 超过这个倍数就存疑
POLAR_COND_DEGENERATE = 30.0    # 极轴反解条件数超过它就只报总量,不报方位/高度分解
POLAR_RESID_OK = 0.05           # 角秒/分钟:联合反解残差低于它才算"单一极轴误差解释得通"


# ---------------------------------------------------------------- 抖动(dither)

@dataclass(frozen=True)
class DitherEvent:
    """一次指令抖动。``dx``/``dy`` 是 PHD2 报的**导星相机像素**偏移。"""

    time: datetime
    dx: float
    dy: float
    pixel_scale: float | None = None

    def arcsec(self, pixel_scale: float = 0.0) -> tuple[float, float]:
        """换算成角秒(需要该段的 pixel scale,同一文件内会变,按段取)。"""
        scale = self.pixel_scale or pixel_scale
        return self.dx * scale, self.dy * scale


# PHD2 写的是 `INFO: DITHER by 1.234, -2.345` 之类;宽松匹配大小写与分隔
_RE_DITHER = re.compile(
    r"^INFO:\s*DITHER\b[^-\d]*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)",
    re.IGNORECASE)
_RE_BEGINS = re.compile(
    r"^Guiding Begins at\s+(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2}):(\d{2})")
_RE_FRAME_T = re.compile(r"^(\d+),\s*([\d.]+),")
_RE_SCALE = re.compile(r"^Pixel scale = ([\d.]+) arc-sec/px", re.IGNORECASE)


def dither_from_log_text(text: str) -> list[DitherEvent]:
    """从 PHD2 日志原文里抽出抖动事件。

    **为什么不改 phd2log**:那边的解析结果进了 metacache,加字段要动版本常量、
    让用户已有缓存整体失效;而抖动只有本模块用,单独扫一遍原文更划算
    (一份日志几百 KB,扫描是毫秒级)。

    ``INFO:`` 行**自身不带时刻**,所以取"同段内最近一帧"的绝对时刻 ——
    与 :mod:`astro_smb.phd2log` 给 ``SettleEvent`` 定时刻的做法一致。
    抖动后紧跟 settle,几秒的定时误差不影响"把这一跳从漂移里扣掉"。
    """
    out: list[DitherEvent] = []
    begins: datetime | None = None
    last_t: datetime | None = None
    pixel_scale: float | None = None
    for line in text.splitlines():
        if (m := _RE_BEGINS.match(line)):
            begins = datetime(*(int(g) for g in m.groups()))  # type: ignore[arg-type]
            last_t = begins
            pixel_scale = None
            continue
        if (m := _RE_SCALE.match(line.strip())):
            pixel_scale = float(m.group(1))
            continue
        if (m := _RE_FRAME_T.match(line)):
            if begins is not None:
                last_t = begins + timedelta(seconds=float(m.group(2)))
            continue
        if (m := _RE_DITHER.match(line.strip())):
            if last_t is not None:
                out.append(DitherEvent(last_t, float(m.group(1)),
                                       float(m.group(2)), pixel_scale))
    return out


def _dither_cumulative(events, when, pixel_scale: float) -> tuple[float, float]:
    """``when`` 时刻为止累计的指令抖动(角秒,RA/DEC 两轴)。

    抖动是**指令**位移:它让目标在画面上挪开是**有意为之**,不能算进"漂移"。
    """
    dx = dy = 0.0
    for ev in events:
        if ev.time <= when:
            ex, ey = ev.arcsec(pixel_scale)
            dx += ex
            dy += ey
    return dx, dy


# ---------------------------------------------------------------- 极轴误差

@dataclass(frozen=True)
class PolarError:
    """极轴指向偏差(小角,度)。

    :param az: **方位**分量 —— 极轴偏东为正。
    :param alt: **高度**分量 —— 极轴偏高(仰角过大)为正。
    """

    az: float
    alt: float

    @property
    def total_arcmin(self) -> float:
        return math.hypot(self.az, self.alt) * 60.0

    @property
    def direction_deg(self) -> float:
        """偏差方向(度):0=偏高,90=偏东。"""
        return math.degrees(math.atan2(self.az, self.alt)) % 360.0


def _rot(axis: np.ndarray, ang_deg: float) -> np.ndarray:
    """绕单位轴转 ``ang_deg`` 度的旋转矩阵(罗德里格斯公式,精确)。"""
    a = math.radians(ang_deg)
    x, y, z = axis / np.linalg.norm(axis)
    c, s, t = math.cos(a), math.sin(a), 1.0 - math.cos(a)
    return np.array([
        [t * x * x + c, t * x * y - s * z, t * x * z + s * y],
        [t * x * y + s * z, t * y * y + c, t * y * z - s * x],
        [t * x * z - s * y, t * y * z + s * x, t * z * z + c],
    ])


def _hz_vec(alt_deg: float, az_deg: float) -> np.ndarray:
    """地平坐标 → 单位向量(x 北,y 东,z 天顶)。"""
    a, z = math.radians(alt_deg), math.radians(az_deg)
    return np.array([math.cos(a) * math.cos(z), math.cos(a) * math.sin(z),
                     math.sin(a)])


def _eq_to_hz(ha_deg: float, dec_deg: float, lat_deg: float) -> np.ndarray:
    """时角/赤纬 → 地平单位向量(北 0 东 90,与 astro.altaz 同约定)。"""
    h, d, p = (math.radians(v) for v in (ha_deg, dec_deg, lat_deg))
    sin_alt = math.sin(d) * math.sin(p) + math.cos(d) * math.cos(p) * math.cos(h)
    sin_alt = max(-1.0, min(1.0, sin_alt))
    alt = math.asin(sin_alt)
    cos_alt = math.cos(alt)
    if abs(cos_alt) < 1e-12:
        return _hz_vec(math.degrees(alt), 0.0)
    cos_az = (math.sin(d) - math.sin(alt) * math.sin(p)) / (cos_alt * math.cos(p))
    az = math.acos(max(-1.0, min(1.0, cos_az)))
    if math.sin(h) > 0:                 # 时角为正(过中天)⇒ 目标在西边
        az = 2.0 * math.pi - az
    return _hz_vec(math.degrees(alt), math.degrees(az))


def simulate_track(ha_deg: float, dec_deg: float, lat_deg: float,
                   polar: PolarError, minutes: float) -> tuple[float, float]:
    """**精确正演**:极轴偏了 ``polar`` 的赤道仪跟踪 ``minutes`` 分钟后,
    目标在天上跑了多远。返回 ``(沿 RA 的大圆位移, DEC 位移)``,单位角秒。

    做法:目标向量绕**错误的极轴**反向转 ω·t(这就是"跟踪"),再与它绕**正确
    的极轴**转同样角度(即理想跟踪下它该在哪)相比。两者之差就是漂移。
    全程只有旋转矩阵,不含任何近似或需要记住的符号约定。
    """
    v = _eq_to_hz(ha_deg, dec_deg, lat_deg)
    true_pole = _hz_vec(lat_deg, 0.0 if lat_deg >= 0 else 180.0)
    # 偏差:先绕"东"轴抬高/压低,再绕天顶轴转方位
    east = np.array([0.0, 1.0, 0.0])
    bad = _rot(np.array([0.0, 0.0, 1.0]), polar.az) @ (
        _rot(east, -polar.alt) @ true_pole)
    ang = SIDEREAL_DEG_PER_S * minutes * 60.0
    ideal = _rot(true_pole, -ang) @ v
    actual = _rot(bad, -ang) @ v
    # 在 ideal 处建局部切平面基:north = 指向天极的分量,east = pole × ideal
    n = np.cross(true_pole, ideal)
    ne = np.linalg.norm(n)
    # 目标贴着天极时"沿 RA 的位移"没有意义(RA 在极点未定义),局部切平面基也病态。
    # **阈值不能取机器精度**:_eq_to_hz 里 acos 在辐角接近 1 时精度只到 ~1e-8 rad,
    # 用 1e-12 根本兜不住,会拿一个数值噪声当东向基,算出几角秒的假漂移(实测 2.25″)。
    # 1e-6 rad ≈ 0.2″ —— 比任何真实指向都近,同时远高于 acos 的噪声。
    if ne < 1e-6:
        return 0.0, 0.0
    e_hat = n / ne
    n_hat = np.cross(ideal, e_hat)
    d = actual - ideal
    arcsec = 3600.0 * math.degrees(1.0)
    return float(np.dot(d, e_hat) * arcsec), float(np.dot(d, n_hat) * arcsec)


def drift_rates(ha_deg: float, dec_deg: float, lat_deg: float,
                polar: PolarError, dt_min: float = 10.0) -> tuple[float, float]:
    """正演的**漂移速率**(角秒/分钟),(RA 向, DEC 向)。"""
    dra, ddec = simulate_track(ha_deg, dec_deg, lat_deg, polar, dt_min)
    return dra / dt_min, ddec / dt_min


@dataclass(frozen=True)
class PolarCheck:
    """极轴反解的结论 —— 比裸 ``(PolarError, rms, cond)`` 多两件必须说的事。"""

    polar: PolarError
    rms: float                  # 拟合残差,角秒/分钟
    cond: float                 # 设计矩阵条件数
    n_samples: int              # 参与拟合的(目标, 时段)数
    exactly_determined: bool    # True = 方程数正好等于未知数,残差没有意义
    degenerate: bool            # True = 条件数过大,方位/高度分不开

    @property
    def falsifiable(self) -> bool:
        """这个结论**能不能被数据推翻**。

        单个目标给 2 个方程解 2 个未知数,残差恒等于机器零 —— 无论模型对不对
        它都"完美拟合"。实测过:给单目标的漂移里掺入 0.5″/分的非极轴分量,
        反解出 9.63′(真值 5.83′,错 65%),残差依然是 4e-16。
        **所以单目标的极轴数字是不可证伪的,只能当量级参考。**
        """
        return not self.exactly_determined

    @property
    def explained(self) -> bool | None:
        """单一极轴误差能否解释全部观测;不可证伪时返回 None。"""
        if self.exactly_determined:
            return None
        return self.rms <= POLAR_RESID_OK


def polar_from_runs(runs, lat_deg: float, *,
                    max_arcmin: float = 120.0) -> PolarCheck:
    """多个(目标, 时段)的漂移 → 极轴误差,**并给出它是否可证伪**。

    :param runs: 若干 ``(ha_deg, dec_deg, dra_rate, ddec_rate)``,与
        :func:`fit_polar_error` 同一口径(角秒/分钟,``dra_rate`` 已乘 cos dec)。

    为什么值得单独有这个函数::func:`fit_polar_error` 只给数字,不说"这个数字
    可不可信"。而**样本数决定了结论的性质**——

    - 1 组:恰定,残差恒为 0,模型错了也看不出来 → 只能当量级参考;
    - >=2 组(且时角/赤纬拉得开):超定,残差立刻能证伪"单一极轴误差"这个模型。

    真机上这不是理论问题:2026-07-29 那一夜的单目标反解给出 2.2′ 且残差为 0,
    而独立测到的场旋要 11′ 才够 —— 单目标那条链**自己发现不了**这个矛盾。
    """
    samples = list(runs)
    pe, rms, cond = fit_polar_error(samples, lat_deg, max_arcmin=max_arcmin)
    return PolarCheck(polar=pe, rms=rms, cond=cond, n_samples=len(samples),
                      exactly_determined=len(samples) < 2,
                      degenerate=cond > POLAR_COND_DEGENERATE)


def simulate_rotation(ha_deg: float, dec_deg: float, lat_deg: float,
                      polar: PolarError, minutes: float) -> float:
    """**精确正演**:极轴偏了 ``polar`` 时,跟踪 ``minutes`` 分钟后画面转了几度。

    这是 :func:`simulate_track` 的姊妹件,补上了本模块此前缺失的一环 ——
    场旋此前只被**测量**,从来没有被**预测**过,于是"漂移"和"场旋"这两条
    本该互相独立的证据链**从未真正对质**,而对质正是本模块存在的理由。

    做法(仍然只有旋转矩阵,不抄任何闭式公式):

    1. 取目标向量 ``u`` 和它正北方向一根极短的"标杆" ``v``;
    2. **星场**绕正确极轴转 ω·t(周日运动),**相机**被赤道仪绕错误极轴转
       同样角度 —— 两者从此不再重合;
    3. 导星把镜筒推回那颗星上(把相机指向送回星场里的目标位置,取最小旋转),
       标杆跟着走 —— 导星摁得住平移,摁不住剩下的转动;
    4. 在目标处的同一切平面里量两根标杆的位置角之差,那就是场旋。

    与 :func:`simulate_track` 一样全程在**地平系**里算,且同样以"绕正确极轴的
    转动"作为理想参照 —— 所以极轴无误差时两根标杆恒重合,场旋严格为 0
    (这一条是本函数的第一条单测:早期版本漏掉星场那一步,零误差竟给出
    5.15°/小时、贴天极给出恒星速率 15.04°/小时,全是参考系搞混的假信号)。

    .. warning::
       **返回值的符号是"天球上的物理旋向",不能直接和实测的位置角趋势比符号。**

       实测那一侧是 :attr:`~astro_smb.wcs.TanWcs.rotation_deg`(图像 +y 的天球
       位置角),而**镜像画幅会把旋向整个反过来** —— ASIAIR 写出的 light 帧
       实测恒为镜像(:attr:`~astro_smb.wcs.TanWcs.flipped`)。所以
       :func:`cross_validate` 的两链对质**只比量级不比符号**;真要比符号,
       必须先按该帧的宇称把测量值翻正。
    """
    u = _eq_to_hz(ha_deg, dec_deg, lat_deg)
    true_pole = _hz_vec(lat_deg, 0.0 if lat_deg >= 0 else 180.0)
    east = np.array([0.0, 1.0, 0.0])
    bad = _rot(np.array([0.0, 0.0, 1.0]), polar.az) @ (
        _rot(east, -polar.alt) @ true_pole)

    def basis(at):
        n = np.cross(true_pole, at)
        ne = np.linalg.norm(n)
        if ne < 1e-6:               # 贴天极:位置角无定义(阈值同 simulate_track)
            return None, None
        e_hat = n / ne
        return e_hat, np.cross(at, e_hat)

    e0, n0 = basis(u)
    if e0 is None:
        return 0.0
    eps = math.radians(1.0 / 60.0)          # 1 角分的标杆,足够短也远离数值噪声
    v = u * math.cos(eps) + n0 * math.sin(eps)

    ang = SIDEREAL_DEG_PER_S * minutes * 60.0
    sky = _rot(true_pole, -ang)         # 星场:周日运动
    cam = _rot(bad, -ang)               # 相机:赤道仪绕错误的极轴转
    u_sky, v_sky = sky @ u, sky @ v
    u_cam, v_cam = cam @ u, cam @ v
    # 导星把相机指向推回那颗星:绕 (u_cam × u_sky) 的最小旋转
    axis = np.cross(u_cam, u_sky)
    na = np.linalg.norm(axis)
    if na > 1e-15:
        v_cam = _rot(axis, math.degrees(
            math.atan2(na, float(np.dot(u_cam, u_sky))))) @ v_cam
    e1, n1 = basis(u_sky)
    if e1 is None:
        return 0.0

    def pa(vec):
        d = vec - u_sky * float(np.dot(vec, u_sky))
        return math.atan2(float(np.dot(d, e1)), float(np.dot(d, n1)))

    return math.degrees((pa(v_cam) - pa(v_sky) + math.pi) % (2 * math.pi) - math.pi)


def rotation_rate(ha_deg: float, dec_deg: float, lat_deg: float,
                  polar: PolarError, dt_min: float = 30.0) -> float:
    """正演的**场旋速率**,度/小时。"""
    return simulate_rotation(ha_deg, dec_deg, lat_deg, polar, dt_min) * 60.0 / dt_min


def fit_polar_error(samples, lat_deg: float, *,
                    max_arcmin: float = 120.0) -> tuple[PolarError, float, float]:
    """从实测漂移反解极轴误差。

    :param samples: 若干 ``(ha_deg, dec_deg, dra_rate, ddec_rate)``,
        速率单位**角秒/分钟**,``dra_rate`` 必须是**大圆**分量(已乘 cos(dec))。
    :returns: ``(PolarError, 残差 RMS(角秒/分), 条件数)``。

    正演对小角是**线性**的,所以取两个单位基(方位 1°、高度 1°)各正演一次,
    组成 2×N 设计矩阵做最小二乘 —— 不需要迭代,也不会陷局部极小。

    **比经典漂移法更强**:教科书的漂移法只看 DEC 漂移,单个目标恒为奇异
    (实测条件数 ``inf``),所以必须测两个位置。这里**同时用 RA 与 DEC 两个
    分量**,一个目标就给出两个方程 —— 实测在赤纬 ±20° 以外条件数只有 1.0~3.4,
    单目标即可分解。

    **真正的简并在赤纬 ≈ 0**(天赤道附近两个误差分量的漂移特征几乎平行):
    纬度 31°N 的扫描里,dec=0 处条件数 83~1112,其余位置 1.0~3.4。
    所以**条件数是必须检查的输出**,不是可选项 —— 简并时拟合照样会给出
    "看起来很确定"的错误分解。经验阈值 >30 就只报总量、不报方位/高度分解。
    """
    rows: list[list[float]] = []
    obs: list[float] = []
    unit_az = PolarError(1.0, 0.0)
    unit_alt = PolarError(0.0, 1.0)
    for ha, dec, dra, ddec in samples:
        a_ra, a_dec = drift_rates(ha, dec, lat_deg, unit_az)
        h_ra, h_dec = drift_rates(ha, dec, lat_deg, unit_alt)
        rows.append([a_ra, h_ra])
        obs.append(float(dra))
        rows.append([a_dec, h_dec])
        obs.append(float(ddec))
    if len(rows) < 2:
        raise ValueError(_("至少需要一个样本(两行观测)才能拟合极轴误差"))
    a = np.asarray(rows, dtype=float)
    b = np.asarray(obs, dtype=float)
    sol, *_rest = np.linalg.lstsq(a, b, rcond=None)
    sv = np.linalg.svd(a, compute_uv=False)
    cond = float(sv[0] / sv[-1]) if sv[-1] > 1e-15 else float("inf")
    resid = a @ sol - b
    rms = float(np.sqrt(np.mean(resid ** 2))) if resid.size else 0.0
    pe = PolarError(float(sol[0]), float(sol[1]))
    if pe.total_arcmin > max_arcmin:     # 明显超出小角近似,拒绝给数
        raise ValueError(_("反解出的极轴误差 {total_arcmin:.0f}′ 超出可信范围").format(
            total_arcmin=pe.total_arcmin))
    return pe, rms, cond


# ---------------------------------------------------------------- 漂移与场旋

@dataclass(frozen=True)
class DriftFit:
    """帧中心随时间的漂移(线性拟合)。速率单位角秒/分钟。"""

    ra_rate: float                  # 大圆分量(已乘 cos(dec))
    dec_rate: float
    total_rate: float
    span_min: float
    total_arcsec: float             # 整段累计位移
    resid_arcsec: float             # 拟合残差 RMS
    n: int
    dither_removed: bool = False

    @property
    def significant(self) -> bool:
        """**只是兜底**:不知道像素尺度/曝光时长时的裸速率判据。

        真正的判读走 :func:`drift_severity` —— 裸速率对"跑了多久"和"什么像素
        尺度"都是瞎的。真机踩过:0.19″/分 在这里不显著,但那一夜跑了 65 分钟,
        累计 14″ = 7.3 主镜像素,整组片子的边缘都白拍了。
        """
        return self.total_rate >= DRIFT_SIGNIFICANT


def drift_severity(fit: DriftFit, *, pixel_scale: float = 0.0,
                   exposure_s: float = 0.0,
                   guide_rms: float = 0.0) -> tuple[bool, list[str]]:
    """目标**走没走** —— 只看逐帧板解算给出的绝对位置累计走了多远。

    "走没走"是天文摄影里的通行概念,判据就一条:**把每张图解算出来的绝对天
    坐标连起来,整段一共挪了多少**。不掺任何别的量 —— 速率、曝光内涂抹、
    导星 RMS、星点形状都是**另外的问题**,它们各自有各自的结论,不参与
    "目标走没走"这个判定。

    (早先这里把"单帧内涂抹 ≥ 半个导星 RMS"和"裸速率超阈值"也算进触发条件,
    是把三件事混成了一件。涂抹说的是"这一张糊不糊",走说的是"整组跑没跑掉",
    两者可以各自独立成立。已按用户口径改正。)

    单位取**主镜像素**:同样的角秒位移,403mm 下 7 个像素、2000mm 下 35 个,
    伤害完全不同。拿不到像素尺度时退回角秒,并在措辞里说明没换算成像素。

    ``exposure_s`` / ``guide_rms`` 仍然收下,但**只用来附一句涂抹的观察**,
    不影响返回的判定 —— 见 :func:`exposure_smear`。
    """
    reasons: list[str] = []
    if pixel_scale > 0:
        walk_px = fit.total_arcsec / pixel_scale
        if walk_px >= DRIFT_WALK_PX:
            reasons.append(
                _("整段累计位移 {total_arcsec:.0f}″ = {walk_px:.1f} 主镜像素(≥{DRIFT_WALK_PX:.0f} 像素即认定目标在走)").format(
                    
                    total_arcsec=fit.total_arcsec, walk_px=walk_px, DRIFT_WALK_PX=DRIFT_WALK_PX))
    elif fit.total_arcsec >= DRIFT_WALK_ARCSEC:
        reasons.append(
            _("整段累计位移 {total_arcsec:.0f}″(≥{DRIFT_WALK_ARCSEC:.0f}″即认定目标在走);调用方没给像素尺度,无法换算成主镜像素").format(
                
                total_arcsec=fit.total_arcsec, DRIFT_WALK_ARCSEC=DRIFT_WALK_ARCSEC))
    return bool(reasons), reasons


def exposure_smear(fit: DriftFit, exposure_s: float) -> float:
    """一次曝光期间漂移把星点拉长了多少(角秒)。

    这是**和"走没走"无关的另一个问题**:走说的是整组跑没跑掉,涂抹说的是
    单张糊不糊。两者可以各自独立成立 —— 慢而长的漂移能把整组跑掉却几乎不
    涂抹单张;短促的大漂移则相反。
    """
    if exposure_s <= 0:
        return 0.0
    return fit.total_rate * (exposure_s / 60.0)


def rotation_severity(rot: "RotationFit", *, pixel_scale: float = 0.0,
                      image_size=None) -> tuple[bool, list[str]]:
    """场旋要不要紧 —— 看它在**画幅角落**推了多少像素。

    场旋是绕画幅中心转的:中心不动,角落位移最大 = 半对角 × 转角。所以
    "多少度每小时"根本不是伤害的单位,**角落像素**才是。真机踩过:
    0.044°/小时 曾被 ``> 0.05°/小时`` 的硬阈值整条压掉,而它在 APS-C 画幅
    角落上是 3.2 像素,且线性趋势的信噪比达 24。

    还要求趋势本身**统计上是真的**(整段转角 ≥ :data:`ROT_SNR` × 拟合残差),
    否则抖动大的序列会把噪声报成场旋。中天翻转时直接不给结论。
    """
    reasons: list[str] = []
    if rot.meridian_flip or rot.n < 3 or not math.isfinite(rot.total_deg):
        return False, reasons
    # 趋势必须统计上是真的。``resid_deg`` 可能恰好是 0(完美线性的合成序列),
    # 那时这一关无从判起,直接放行交给后面的量级判据 —— 但**绝不能拿它当分母**。
    if rot.resid_deg > 0 and abs(rot.total_deg) < ROT_SNR * rot.resid_deg:
        return False, reasons
    snr = (abs(rot.total_deg) / rot.resid_deg) if rot.resid_deg > 0 else float("inf")
    head = (_("场旋 {rate_deg_per_hour:+.3f}°/小时(整段 {0:+.1f}′)").format(
        rot.total_deg * 60, rate_deg_per_hour=rot.rate_deg_per_hour))
    if pixel_scale > 0 and image_size:
        w, h = float(image_size[0]), float(image_size[1])
        corner_px = math.hypot(w, h) / 2.0 * abs(math.radians(rot.total_deg))
        if corner_px >= ROT_CORNER_PX:
            reasons.append(
                _("{head},画幅角落被推了 {corner_px:.1f} 像素 —— 这是极轴误差的直接观测量,与导星好坏无关").format(
                    head=head, corner_px=corner_px))
            return True, reasons
        return False, reasons
    # 没有画幅尺寸就换算不出角落像素,只能退回**旧的那个任意速率阈值**。
    # 保留它是为了"信息不全时别静默漏报",不是因为它有道理 —— 调用方给全
    # pixel_scale + image_size 才是正确用法。
    if abs(rot.rate_deg_per_hour) <= ROT_FALLBACK_DEG_PER_HOUR:
        return False, reasons
    reasons.append(
        _("{head},信噪 {snr:.0f} —— 极轴误差的直接观测量;调用方没给画幅尺寸,无法换算成角落像素").format(
            head=head, snr=snr))
    return True, reasons


def fit_center_drift(times, ra_deg, dec_deg, *, dither=None,
                     pixel_scale: float = 0.0) -> DriftFit:
    """一组按时间排序的帧中心 → 漂移速率。

    ``dither`` 给了就先把**累计指令抖动**从观测里扣掉再拟合 —— 否则整夜的
    抖动会被当成漂移(这正是"抖动不作为前提排除,而是减掉"的实现)。
    扣除需要导星相机的 ``pixel_scale``(角秒/像素)。

    **偏移量的几何走 :func:`astro_smb.wcsapps._project_tangent`**(在参考帧中心的
    切平面上量,gnomonic),不再用 ``Δra·cos δ`` 的小角近似。这两处曾各有一套
    实现:``wcsapps.drift`` 用切平面、本函数用 cos δ,同一个物理量两种口径,
    而 ``wcsapps.drift`` 那套更完备却一直没有调用方。取更准的那套,
    近似那套删掉。几角分以内两者差别在 1e-6 相对量级,但跨度大时切平面才是对的。
    """
    t = np.asarray([_ts(x) for x in times], dtype=float)
    ra = np.asarray(ra_deg, dtype=float)
    dec = np.asarray(dec_deg, dtype=float)
    if t.size < 2:
        return DriftFit(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, int(t.size))
    order = np.argsort(t)
    t, ra, dec = t[order], ra[order], dec[order]
    tm = (t - t[0]) / 60.0                      # 分钟
    from astro_smb.wcsapps import _project_tangent

    xi, eta, ok = _project_tangent((float(ra[0]), float(dec[0])), ra, dec)
    if not bool(np.all(ok)):
        # 帧中心散布超过 90°:不是同一个目标,漂移这个量没有意义
        return DriftFit(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, int(t.size))
    dra = np.asarray(xi, dtype=float) * 3600.0      # 东向大圆分量(角秒)
    ddec = np.asarray(eta, dtype=float) * 3600.0
    removed = False
    if dither and (pixel_scale > 0
                   or any(getattr(ev, "pixel_scale", None) for ev in dither)):
        sub = np.array([_dither_cumulative(dither, times[i], pixel_scale)
                        for i in order])
        dra = dra - (sub[:, 0] - sub[0, 0])
        ddec = ddec - (sub[:, 1] - sub[0, 1])
        removed = True
    ra_rate, ra_res = _lin_rate(tm, dra)
    dec_rate, dec_res = _lin_rate(tm, ddec)
    span = float(tm[-1] - tm[0])
    total = math.hypot(float(dra[-1]), float(ddec[-1]))
    return DriftFit(ra_rate, dec_rate, math.hypot(ra_rate, dec_rate),
                    span, total, math.hypot(ra_res, dec_res), int(t.size),
                    removed)


@dataclass(frozen=True)
class RotationFit:
    """位置角随时间的漂移 —— 极轴误差的**直接观测量**。"""

    rate_deg_per_hour: float
    span_min: float
    total_deg: float
    resid_deg: float
    n: int
    meridian_flip: bool = False


def fit_position_angle(times, pa_deg) -> RotationFit:
    """位置角序列 → 旋转速率。自动解 ±180°/360° 的缠绕。"""
    t = np.asarray([_ts(x) for x in times], dtype=float)
    pa = np.asarray(pa_deg, dtype=float)
    if t.size < 2:
        return RotationFit(0.0, 0.0, 0.0, 0.0, int(t.size))
    order = np.argsort(t)
    t, pa = t[order], pa[order]
    jumps = np.abs((np.diff(pa) + 180.0) % 360.0 - 180.0)
    meridian_flip = bool(np.any(jumps > 150.0))
    pa = np.degrees(np.unwrap(np.radians(pa)))
    th = (t - t[0]) / 3600.0
    rate, res = ((float("nan"), float("nan")) if meridian_flip
                 else _lin_rate(th, pa - pa[0]))
    return RotationFit(rate, float((t[-1] - t[0]) / 60.0),
                       float(pa[-1] - pa[0]), res, int(t.size),
                       meridian_flip)


def _lin_rate(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    """过原点不强制的一次拟合 → (斜率, 残差 RMS)。"""
    if x.size < 2 or float(np.ptp(x)) <= 0:
        return 0.0, 0.0
    k, b = np.polyfit(x, y, 1)
    resid = y - (k * x + b)
    return float(k), float(np.sqrt(np.mean(resid ** 2)))


def _ts(v) -> float:
    return v.timestamp() if isinstance(v, datetime) else float(v)


# ---------------------------------------------------------------- 星点形状通道

def sampling_quality(fwhm_px: float, pixel_scale: float) -> tuple[str, str]:
    """采样是否足以让**星点形状**反映导星误差。返回 (等级, 说明)。

    像素尺度远大于星像时,星点被采样成几个方块,FWHM/偏心率量化误差极大 ——
    此时星点通道**不可信**,结论只能靠板解算。宁可标"测不了",也不给一个
    看着很确定的数字。
    """
    if not (fwhm_px and fwhm_px > 0) or not (pixel_scale and pixel_scale > 0):
        return "unknown", _("缺少 FWHM 或像素尺度,无法判断采样")
    if fwhm_px < UNDERSAMPLED_RATIO:
        return "under", (_("欠采样(FWHM 仅 {fwhm_px:.1f} px):星点形状测不出导星误差,本项结论仅供参考").format(
            fwhm_px=fwhm_px))
    if fwhm_px < WELL_SAMPLED_RATIO:
        return "marginal", _("采样勉强(FWHM {fwhm_px:.1f} px):星点通道精度有限").format(fwhm_px=fwhm_px)
    return "ok", _("采样充分(FWHM {fwhm_px:.1f} px)").format(fwhm_px=fwhm_px)


def fwhm_budget(fwhm_total_arcsec: float, guiding_rms_arcsec: float,
                optics_fwhm_arcsec: float = 0.0) -> dict:
    """把星像宽度分解成 视宁度 / 光学 / 导星 三项。

    各项近似**正交**,按平方和合成::

        FWHM_total² ≈ FWHM_seeing² + FWHM_optics² + (k·RMS_guide)²

    导星抖动的 RMS 折算成 FWHM 要乘 ``k = 2.3548``(高斯 σ→FWHM);
    余下的算作视宁度。若导星项已经超过总宽,说明输入不自洽(多半是
    RMS 与 FWHM 不是同一段时间),此时**明确返回 ``consistent=False``**,
    不要硬凑出一个负的视宁度。
    """
    tot = float(fwhm_total_arcsec or 0.0)
    guide = 2.3548 * float(guiding_rms_arcsec or 0.0)
    opt = float(optics_fwhm_arcsec or 0.0)
    known = guide ** 2 + opt ** 2
    if tot <= 0:
        return {"consistent": False, "reason": _("总 FWHM 缺失")}
    if known >= tot ** 2:
        return {"consistent": False, "total": tot, "guiding": guide,
                "optics": opt,
                "reason": _("导星与光学项之和已超过实测星像宽度 —— 多半是 RMS 与该帧不是同一时段,或像素尺度用错了")}
    seeing = math.sqrt(tot ** 2 - known)
    return {"consistent": True, "total": tot, "seeing": seeing,
            "guiding": guide, "optics": opt,
            "guiding_share": (guide ** 2) / (tot ** 2)}


# ---------------------------------------------------------------- 交叉判读

@dataclass
class FrameEvidence:
    """一张 sub 的三通道证据(缺哪条就留 None)。"""

    t0: datetime                        # 曝光开始
    t1: datetime                        # 曝光结束
    # ② 星点形状
    fwhm_px: float | None = None
    fwhm_arcsec: float | None = None
    ellipticity: float | None = None
    theta_deg: float | None = None
    theta_r: float | None = None
    n_stars: int = 0
    # ③ 板解算
    center_ra: float | None = None
    center_dec: float | None = None
    pa_deg: float | None = None
    # ① 导星日志(该曝光区间内)
    guide_rms_arcsec: float | None = None
    guide_coverage: float = 0.0


@dataclass
class CrossCheck:
    """交叉判读结论。``findings`` 是给用户看的人话,按重要性排序。"""

    verdict: str                        # good | drift | rotation | overguide | unknown
    headline: str
    findings: list[str] = field(default_factory=list)
    drift: DriftFit | None = None
    rotation: RotationFit | None = None
    polar: PolarError | None = None
    polar_cond: float = float("inf")
    #: 极轴反解**推翻得了吗**。单目标是恰定解(2 方程 2 未知),残差恒为机器零,
    #: 模型错了也照样"完美拟合" —— 那种数字只能当量级参考。
    #: **界面别再去 findings 里搜「恰定」两个字来反推这件事**:那是拿会被翻译的
    #: 显示文本当判据,一改文案(或一做 i18n)就静默变成"这个数可信"。
    polar_falsifiable: bool = False
    #: 不可证伪时附在 `findings` 里的那句告白**原文**。夜次级联合反解会把它
    #: 换成真正有信息量的结论,而"换掉哪一条"原来是靠 ``"恰定" not in f``
    #: 搜出来的 —— 一翻译就搜不到,那句误导性的告白会**和新结论并排留着**。
    #: 存原文按相等剔除,与语言无关。
    polar_exact_note: str = ""
    # 漂移反解出的极轴误差,能不能解释**独立测到的场旋**。
    # None = 没做这项对质(缺台址/时角,或场旋不显著)。
    # False 时**别去调极轴** —— 现场至少还有第二个机制。
    polar_consistent: bool | None = None
    confidence: str = "low"             # low | medium | high
    is_oag: bool | None = None


def cross_validate(frames, *, pixel_scale_main: float = 0.0,
                   guide_pixel_scale: float = 0.0, dither=None,
                   is_oag: bool | None = None,
                   lat_deg: float | None = None,
                   ha_deg: float | None = None,
                   image_size=None) -> CrossCheck:
    """三通道交叉判读。

    这是本模块的出口:把"PHD2 说什么""星点说什么""板解算说什么"摆在一起,
    输出**分歧**而不是又一个 RMS 数字。

    ``is_oag`` 会改变判读:同轴导星天然消除主镜/导星镜差分挠曲,所以
    "① 稳 + ③ 漂"在 OAG 上更可能是**极轴误差/场旋**而不是挠曲 ——
    这一条会直接写进结论措辞。

    ``pixel_scale_main`` 与 ``image_size`` 不是可有可无的装饰:漂移和场旋的
    判据都以**主镜像素**为单位(见 :func:`drift_severity` /
    :func:`rotation_severity`)。不给就只能退回裸速率兜底,漏报风险显著变高。
    """
    solved = [f for f in frames
              if f.center_ra is not None and f.center_dec is not None]
    out = CrossCheck(verdict="unknown", headline=_("证据不足,无法判读"))
    out.is_oag = is_oag
    if len(solved) < 2:
        out.findings.append(
            _("只有 {0} 张解算成功,至少需要 2 张才能测漂移").format(len(solved)))
        return out

    mid = [f.t0 + (f.t1 - f.t0) / 2 for f in solved]
    out.drift = fit_center_drift(mid, [f.center_ra for f in solved],
                                 [f.center_dec for f in solved],
                                 dither=dither, pixel_scale=guide_pixel_scale)
    pas = [(m, f.pa_deg) for m, f in zip(mid, solved) if f.pa_deg is not None]
    if len(pas) >= 2:
        out.rotation = fit_position_angle([p[0] for p in pas],
                                          [p[1] for p in pas])

    guided = [f.guide_rms_arcsec for f in solved if f.guide_rms_arcsec]
    guide_rms = float(np.median(guided)) if guided else None
    exposures = [(f.t1 - f.t0).total_seconds() for f in solved
                 if f.t1 is not None and f.t0 is not None]
    exposure_s = float(np.median(exposures)) if exposures else 0.0
    drifting, drift_why = drift_severity(
        out.drift, pixel_scale=pixel_scale_main, exposure_s=exposure_s,
        guide_rms=guide_rms or 0.0)
    rotating, rotation_why = (
        rotation_severity(out.rotation, pixel_scale=pixel_scale_main,
                          image_size=image_size)
        if out.rotation is not None else (False, []))
    shaped = [f for f in solved
              if f.fwhm_px is not None and f.fwhm_px > 0
              and f.ellipticity is not None
              and math.isfinite(float(f.ellipticity))]
    shape_level = "unknown"
    shape_round = shape_long = False
    if shaped:
        fwhm_px = float(np.median([f.fwhm_px for f in shaped]))
        shape_level, sample_note = sampling_quality(fwhm_px, pixel_scale_main)
        ellipticity = float(np.median([f.ellipticity for f in shaped]))
        fwhm_as_values = [
            f.fwhm_arcsec for f in shaped
            if f.fwhm_arcsec is not None
            and math.isfinite(float(f.fwhm_arcsec))
        ]
        fwhm_as = (float(np.median(fwhm_as_values))
                   if fwhm_as_values else
                   fwhm_px * pixel_scale_main if pixel_scale_main > 0 else None)
        out.findings.append(
            _("主镜星点 FWHM {fwhm_px:.2f} px").format(fwhm_px=fwhm_px)
            + (f" / {fwhm_as:.2f}″" if fwhm_as is not None else "")
            + _(",椭圆率 {ellipticity:.2f}({0}/{1} 张)").format(
                len(shaped), len(solved), ellipticity=ellipticity))
        out.findings.append(sample_note)
        if shape_level != "under":
            shape_round = ellipticity <= 0.15
            shape_long = ellipticity >= 0.22
        if guide_rms is not None and fwhm_as is not None:
            budget = fwhm_budget(fwhm_as, guide_rms)
            if budget["consistent"]:
                out.findings.append(
                    _("FWHM 平方预算中导星项约占 {0:.0f}%").format(100.0 * budget['guiding_share']))
            else:
                out.findings.append(_("FWHM 与同期导星 RMS 不自洽:") + budget["reason"])
    else:
        out.findings.append(_("缺少主镜星点形状统计,本次只能交叉核对导星与板解算"))

    if guide_rms is not None:
        out.findings.append(
            _("导星器报告 RMS {guide_rms:.2f}″(中位,覆盖 {0}/{1} 张)").format(
                len(guided), len(solved), guide_rms=guide_rms))
    out.findings.append(
        _("主镜实测漂移 {total_rate:.2f}″/分(整段累计 {total_arcsec:.0f}″,{span_min:.0f} 分钟)").format(
            total_rate=out.drift.total_rate, total_arcsec=out.drift.total_arcsec, span_min=out.drift.span_min)
        + (_("(已扣除指令抖动)") if out.drift.dither_removed else ""))
    out.findings.extend(drift_why)
    # 涂抹是**另一个**问题,单独报,不参与"走没走"
    if exposure_s > 0 and guide_rms:
        smear = exposure_smear(out.drift, exposure_s)
        if smear >= DRIFT_SMEAR_FRAC * guide_rms:
            out.findings.append(
                _("另:单帧曝光内漂移 {smear:.2f}″,达导星 RMS({guide_rms:.2f}″)的{0:.0%},这一张的星点也被它拉长了(与'目标走没走'是两件事)").format(
                    
                    smear / guide_rms, smear=smear, guide_rms=guide_rms))
    if out.rotation is not None and out.rotation.meridian_flip:
        out.findings.append(
            _("位置角出现约 180° 跳变,疑似中天翻转;已停止场旋拟合,不给极轴结论"))
    else:
        out.findings.extend(rotation_why)

    # 极轴反解**不再被漂移门控**:场旋是极轴误差的独立观测量,导星把漂移压掉了
    # 也照样转。真机踩过:漂移不过阈值 ⇒ 连极轴诊断一起消失,而那一夜场旋信噪 24。
    dec_med = float(np.median([f.center_dec for f in solved]))
    if lat_deg is not None and ha_deg is not None and (drifting or rotating):
        try:
            pc = polar_from_runs(
                [(ha_deg, dec_med,
                  out.drift.ra_rate, out.drift.dec_rate)], lat_deg)
            pe, cond = pc.polar, pc.cond
            out.polar, out.polar_cond = pe, cond
            out.polar_falsifiable = bool(pc.falsifiable)
            if not pc.falsifiable:
                # 本函数只判读**一个**目标时段,所以极轴反解恒为恰定 ——
                # 残差必然是机器零,模型错了也照样"完美拟合"。真机验证过:
                # 掺 0.5″/分的非极轴分量进去,反解错 65% 而残差仍是 4e-16。
                # 想要可证伪的极轴数字,得用 polar_from_runs 喂**多个**目标。
                out.polar_exact_note = _("注意:只有一个目标时段,极轴反解是**恰定**的 —— 残差恒为 0 是构造使然,不代表拟合得好,模型错了也看不出来。要得到可证伪的极轴结论,请用 polar_from_runs 同时喂入同一夜的多个目标(时角/赤纬拉得开)")
                out.findings.append(out.polar_exact_note)
            if cond > POLAR_COND_DEGENERATE:
                out.findings.append(
                    _("由漂移反解极轴偏差约 {total_arcmin:.0f}′,但该目标靠近天赤道,方位与高度分量在观测上几乎简并——只能当量级参考;换一个赤纬远离 0° 的目标即可分解").format(
                        
                        total_arcmin=pe.total_arcmin))
            else:
                out.findings.append(
                    _("由漂移反解极轴偏差 {total_arcmin:.1f}′,方向 {direction_deg:.0f}°").format(
                        total_arcmin=pe.total_arcmin, direction_deg=pe.direction_deg))
        except ValueError as ex:
            out.findings.append(_("极轴反解未给出结果: {ex}").format(ex=ex))

    # ---- 两条独立链的**对质**:这才是本模块存在的理由
    #
    # 漂移和场旋若都源自同一个极轴误差,它们必须互相支持 —— 一个极轴误差
    # 同时决定了漂移速率**和**场旋速率。此前两者各报各的,用户会自然以为
    # 是同一回事;真机(2026-07-29)恰恰不是:漂移只支持 2.1′,而实测场旋要
    # 11′ 才够,还反号。差这么多就说明**至少还有第二个机制**在起作用。
    if (out.polar is not None and out.rotation is not None and rotating
            and lat_deg is not None and ha_deg is not None
            and out.polar_cond <= POLAR_COND_DEGENERATE):
        pred = rotation_rate(ha_deg, dec_med, lat_deg, out.polar)
        meas = out.rotation.rate_deg_per_hour
        # **只比量级,不比符号**:实测那一侧是图像 +y 的位置角,镜像画幅
        # (ASIAIR light 帧恒为镜像)会把旋向整个翻过来,而正演给的是天球上的
        # 物理旋向。拿符号当证据会凭空造出一个"反号"的假分歧 —— 差点就这么报了。
        ratio = (abs(meas) / abs(pred)) if abs(pred) > 1e-9 else float("inf")
        out.polar_consistent = 0.5 <= ratio <= 2.0
        if out.polar_consistent:
            out.findings.append(
                _("两条独立链自洽:漂移反解的 {total_arcmin:.1f}′ 极轴误差预测场旋 {0:.3f}°/小时,实测 {1:.3f}°/小时(只比量级,符号受画幅宇称影响不作数)—— 可以放心按极轴误差去调").format(
                    
                    abs(pred), abs(meas), total_arcmin=out.polar.total_arcmin))
        else:
            out.findings.append(
                _("**两条独立链互相矛盾**:漂移反解的 {total_arcmin:.1f}′ 极轴误差只预测出场旋 {0:.3f}°/小时,实测却是 {1:.3f}°/小时").format(
                    
                    abs(pred), abs(meas), total_arcmin=out.polar.total_arcmin)
                + (_("(差 {ratio:.1f} 倍)").format(
                    ratio=ratio) if math.isfinite(ratio) else "")
                + _(" —— 单一极轴误差解释不了这两个观测,至少还有第二个机制(相机/OAG 组件相对光轴转动、调焦座沉降等),光调极轴不会解决问题"))

    if drifting and guide_rms is not None and guide_rms < 1.5:
        out.verdict = "drift"
        if out.polar_consistent is False:
            # 对质已经否掉了"单一极轴误差"这个解释,结论行就不能再指向极轴 ——
            # 让用户去拧极轴螺丝而问题依旧,比不给结论更糟。
            cause = (_("但漂移与场旋对不上,单一极轴误差解释不了 —— 先查相机/OAG 组件与调焦座是否在转、在沉"))
        else:
            cause = (_("同轴导星(OAG)已排除主镜/导星镜差分挠曲,首要嫌疑是极轴误差与场旋") if is_oag else
                     _("首要嫌疑是主镜/导星镜差分挠曲,其次是极轴误差"))
        out.headline = (_("导星曲线漂亮({guide_rms:.2f}″)但目标在走({total_arcsec:.0f}″)—— {cause}").format(
            
            guide_rms=guide_rms, total_arcsec=out.drift.total_arcsec, cause=cause))
    elif drifting:
        out.verdict = "drift"
        out.headline = _("目标在走({total_arcsec:.0f}″),且导星本身也不稳").format(
            total_arcsec=out.drift.total_arcsec)
    elif rotating:
        # 只转不漂:导星把平移压住了,但场旋压不住 —— 这仍然是极轴问题,
        # 绝不能因为"中心没走"就判 good(角落照样被抹)。
        out.verdict = "rotation"
        out.headline = (
            _("画幅中心没走,但整场在转({0:+.1f}′)—— 导星只能摁住平移,摁不住场旋;这是极轴误差,越靠画幅边缘越糊").format(
                out.rotation.total_deg * 60))
    elif guide_rms is not None and guide_rms > 1.5:
        out.verdict = "overguide"
        if shape_round:
            out.headline = (
                _("导星器报 {guide_rms:.2f}″,但主镜星点仍圆且目标没走 —— 多半是导星镜的视宁度噪声,可能在过度纠正").format(
                    guide_rms=guide_rms))
        elif shape_long:
            out.headline = (
                _("导星器报 {guide_rms:.2f}″,主镜星点也被拉长,但没有累计漂移 —— 导星抖动正在直接损伤成像").format(
                    guide_rms=guide_rms))
        else:
            out.headline = (
                _("导星器报 {guide_rms:.2f}″ 但目标没有累计漂移 —— 星点形状证据不足,只能提示可能过度纠正").format(
                    guide_rms=guide_rms))
    else:
        if guide_rms is None:
            out.verdict = "unknown"
            out.headline = _("板解算没有发现累计漂移,但缺少同期导星日志")
        else:
            out.verdict = "good"
            out.headline = _("导星器报告与主镜实测一致,没有发现隐藏漂移")

    # 置信度既看**样本量**也看**到齐了几条证据链**。本模块的全部前提是"价值在
    # 三者的分歧里",少一条链还报 high,等于把两条链的巧合当成三条链的共识。
    n_ok = sum(1 for f in solved if f.pa_deg is not None)
    channels = sum((guide_rms is not None, bool(shaped), n_ok >= 2))
    out.confidence = ("high" if len(solved) >= 10 and n_ok >= 10 else
                      "medium" if len(solved) >= 5 else "low")
    if channels < 3 and out.confidence == "high":
        out.confidence = "medium"
        missing = [name for name, have in
                   ((_("导星日志"), guide_rms is not None),
                    (_("星点形状"), bool(shaped)),
                    (_("位置角"), n_ok >= 2)) if not have]
        out.findings.append(
            _("只有 {channels}/3 条证据链到齐(缺 {0}),置信度降为 medium").format(
                _("、").join(missing), channels=channels))
    return out
