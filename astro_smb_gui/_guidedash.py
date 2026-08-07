"""导星仪表盘:某个拍摄目标组内**全部导星段聚合**的分析视图。

入口:导星页段列表的组头右侧「仪表盘」按钮(`_guiding.GuidingPage._on_dash_click`)。
形态:导星页**右侧分析区的第二个视图**(`guidedash.xaml` 挂进 `DashHost`),
与段视图(大折线图 + 该段统计)互斥切换,左侧段列表全程可见可点。
所以这里**没有**遮罩背景 / Esc / 关闭按钮那套模态包袱:点左侧任意段、
或标题行的「◀ 段视图」按钮就切回去;可见性由页面的 `_set_view` 统一管,
本类只管"画/不画"(`show()` / `hide()`)。

**尺寸自适应**:右侧面板比原来的整页遮罩窄且会随窗口变,所以画布尺寸不再硬编码 ——
`chart_layout(可用宽)` 现算列数与单图宽高,8 张小图交给 `VariableSizedWrapGrid`
按格排(布局层自己换行,不需要重画),2 张通栏图按可用宽画。正文横向滚动被
关死:一是用户明确不要横滚条,二是只有横向受约束正文才拿得到有限宽度。
宽度真的变了才防抖重画(`_on_body_size` → `_resize_later`)。

分层(与本仓库其它页一致):

* **纯计算层**(本文件上半,模块级函数,`aggregate_group` 为入口)——在工作线程
  执行,只吃 `_guiding._prepare` 产出的数据行与 `LogData`,不碰任何 XAML;
* **视图层** `GuideDashboard` —— UI 线程只画。图表按组键做**进程内内存缓存**,
  随 `LogData` 换代整体作废(不进 metacache,本任务不改变任何被缓存的解析行为)。

统计口径(与 `astro_smb.phd2log` 严格一致,改动前务必看清):

* 丢星判据一律 `GuideFrame.lost`(ErrorCode!=0 或 SNR<=0),RMS 统计必须剔除;
* pixel scale **按段取用**(同一文件内会变)。组内只要有一个段带 scale,就按角秒
  口径,**无 scale 的整段排除**而不是混算 —— 与 `compute_rms` 的规则同源;
  权威 RMS 直接调 `compute_rms(全组 (帧, 段 scale) 对)`,本模块的 numpy 数组只是
  它的等价复算(单测钉死两者一致);
* `RmsStats.duration_s` 跨段无意义(它是 max-min 帧时刻),一律不用,时长按段求和;
* 滚动 RMS / 漂移拟合 / 周期图 / 自相关**不得跨段拼接**(段间有大空洞会让
  中位帧间隔与线性趋势失真):滚动 RMS 逐段算后放到同一条绝对时间轴上并画出
  段边界竖线;漂移按段拟合后按帧数加权;周期图/自相关只取组内**最长的那一段**。

复用:6 张小图(散点 / 直方图 / 滚动 RMS / 脉冲配比 / 周期图 / SNR)直接调
`_guiding` 里已有的 `_draw_*`(已参数化 canvas 与宽高,缺省值与参数化前像素级
等价),本文件只在其上叠加 RMS 椭圆 / 正态拟合 / 丢星刻度 / 段边界四层;
另有 4 张本页独有的图(脉冲时长直方图 / 自相关 / 分段对比条 / 每张 sub 的 RMS)。
"""

from __future__ import annotations

import asyncio
import math
import threading
from datetime import datetime
from pathlib import Path

import numpy as np

from win32more import asyncui
from win32more.Microsoft.UI.Text import FontWeights
from win32more.Microsoft.UI.Xaml import (
    CornerRadius,
    FrameworkElement,
    GridLength,
    GridUnitType,
    TextWrapping,
    Thickness,
    VerticalAlignment,
    Visibility,
)
from win32more.Microsoft.UI.Xaml.Controls import (
    Border,
    Button,
    Canvas,
    ColumnDefinition,
    FontIcon,
    Grid,
    Orientation,
    ProgressRing,
    RowDefinition,
    ScrollBarVisibility,
    ScrollMode,
    ScrollViewer,
    StackPanel,
    TextBlock,
    ToolTipService,
    VariableSizedWrapGrid,
)
from win32more.Microsoft.UI.Xaml.Markup import XamlReader
from win32more.Microsoft.UI.Xaml.Media import FontFamily, RotateTransform, SolidColorBrush
from win32more.Microsoft.UI.Xaml.Shapes import Ellipse, Rectangle
from win32more.Windows.ApplicationModel.DataTransfer import Clipboard, DataPackage
from win32more.Windows.UI import Color

from astro_smb.phd2log import compute_rms
from astro_smb.i18n import gettext as _

from astro_smb_gui import _guiding as G
from astro_smb_gui._xamli18n import load_text as _xaml_text

XAML_PATH = Path(__file__).with_name("guidedash.xaml")

# ---- 画布尺寸:全部按面板可用宽现算(xaml 里不再有固定 Width) ----
CHART_GAP = 10.0        # 图表卡之间的间距(= 网格每格右侧留白)
CHART_BORDER = 2.0      # 卡片 Border 左右各 1px
CHART_TITLE_H = 18.0    # 卡片标题行(11px 字 + 2px 间距)
CHART_MIN_W = 250.0     # 单张小图最小宽:再窄坐标轴文字就开始互相压
CHART_MAX_W = 340.0     # 单张小图最大宽:超宽窗口下别把图拉成横条
CHART_MAX_COLS = 4      # 每行最多几列(再多单图信息密度反而下降)
CHART_ASPECT = 0.63     # 小图 高/宽
# 排版余量:`VariableSizedWrapGrid` 在 列数×ItemWidth **正好等于**可用宽时会少排
# 一列(真机实测:可用宽 811.2,ItemWidth 270.0 排 3 列、270.4 就掉到 2 列,
# 而 3×270.4 = 811.2 —— 边界上它不认)。所以先从可用宽里扣掉一点再分。
CHART_SLACK = 6.0
PANEL_W_DEFAULT = 780.0  # 还没量到面板宽度时的缺省(1400 逻辑宽窗口右侧区约 819)
SCROLL_PAD = 8.0        # DashScroll 的右侧内边距(= guidedash.xaml 的 Padding)
WIDE_MIN = 300.0        # 通栏图最小宽
RESIZE_TOLERANCE = 12.0  # 宽度变化小于这么多不重画(布局抖动不该触发重排)
RESIZE_DEBOUNCE_S = 0.25  # 拖窗口边框期间只在停下来之后重画一次

SEG_ROW_H = 22.0                   # 分段对比条行高
SEG_TOP = 8.0                      # 分段对比条第一行的 y
SEG_LABEL_W = 100.0                # 左侧时间标签列宽
SEG_TAIL_W = 176.0                 # 右侧说明文字列宽
SEG_MIN_TRACK = 80.0               # 条区最小宽(极窄面板下的兜底)
SEG_MAX_ROWS = 40                  # 超过就只画前 N 段并注明(避免画布过高)
SUB_H = 180.0

# ---- 聚合门槛 / 降采样预算 ----
SCATTER_MAX = 400       # 散点上限(>此值均匀抽稀)。300x190 画布上 2px 点,400 已接近
                        # 视觉饱和(导星页自己的小图也才 800);每个点都是一个 XAML 元素,
                        # 元素数是打开耗时的主项,不能按"数据越全越好"来定
ROLL_FRAMES = 30        # 滚动 RMS 帧窗(需求明确:30 帧)
ROLL_PTS = 480          # 滚动 RMS 曲线总采样点预算(按段帧数分配)
SNR_PTS = 400           # SNR/星质量曲线采样点预算
LOST_TICKS = 80         # 丢星刻度上限。绘图区只有 288px 宽,画 300 根竖线就是一块
                        # 实心色带(信息量为零),80 根仍能看出"丢星密集在哪一段"
PULSE_BINS = 16         # 脉冲时长直方图 bin 数
ACF_MIN_FRAMES = 120    # 自相关最少有效帧
ACF_MIN_DUR = 180.0     # 自相关最短时长(秒)
ACF_MIN_LAG_S = 20.0    # 主峰搜索起始滞后(躲开 0 附近的自身衰减)
ACF_MAX_LAG_S = 1200.0  # 自相关滞后上限
ACF_PEAK_MIN = 0.15     # 主峰显著性门槛(低于此视为"无明显周期")
ACF_PTS = 260           # 自相关曲线点数上限
ACF_LABEL_MIN_GAP = 34.0  # 自相关滞后标签之间的最小像素间距(太近就只画线不画字)
SUB_MIN_FRAMES = 3      # 一张 sub 至少要覆盖这么多导星帧才给 RMS
SUB_BAD_FACTOR = 1.5    # 废片候选阈值 = 组 RMS × 该系数
SUB_MAX_BARS = 120      # sub 柱状图最多画这么多柱(分桶取最差)。936px 通栏上 120 根
                        # 已是每根 7.8px,再密就分辨不出单张;柱数直接决定打开耗时
LOST_GOOD, LOST_WARN = 2.0, 8.0   # 丢星率语义阈值(%)

# 分区图标(Segoe Fluent Icons 私用区,均为 BMP;与浏览页详情卡同族)
_GRP_QUALITY = ""   # Diagnostic
_GRP_FRAMES = ""    # Page
_GRP_DRIFT = ""     # MapDirections
_GRP_OPTICS = ""    # View
_GRP_CAMERA = ""    # Camera
_GRP_CAL = ""       # Settings
_GRP_SHOT = ""      # FavoriteStarFill

# 数值语义色(中间调:浅/深主题下都可读,不带底色)——与浏览页详情卡同表
_TONE_RGB = {
    "good": (0x3F, 0xA9, 0x55),
    "warn": (0xD0, 0x8A, 0x00),
    "bad": (0xD9, 0x4A, 0x4A),
    "dim": (0x8A, 0x8A, 0x8A),
}
# 徽章配色在 `_guiding.BADGE_RGB`(段视图标题行也要用同一份):右侧两个视图
# 的胶囊必须完全同色,配色表放在下层模块才只有一处出处。胶囊控件本身直接
# 复用 `GuidingPage._chip`。


def _brush(r: int, g: int, b: int, a: int = 255) -> SolidColorBrush:
    c = Color()
    c.A, c.R, c.G, c.B = a, r, g, b
    return SolidColorBrush(c)


def chart_layout(avail: float) -> tuple[int, float, float]:
    """面板可用宽 → (每行列数, 单图画布宽, 单图画布高)。纯函数,单测钉死。

    列数按"最小可用宽"取整,再把剩余宽度(扣掉 `CHART_SLACK`)**均分**给这几列
    (格宽含右侧 gap),所以一行的总宽稳稳小于可用宽 —— 既不留下一大条空白,
    也绝不会溢出(溢出 = 要么被裁、要么冒出用户明确不要的横滚条)。
    单图宽有上下限:太窄坐标文字互相压,太宽就成了没信息量的横条。
    """
    cell = CHART_MIN_W + CHART_BORDER + CHART_GAP
    avail = max(cell + CHART_SLACK, float(avail or 0.0)) - CHART_SLACK
    cols = max(1, min(CHART_MAX_COLS, int(avail // cell)))
    w = min(CHART_MAX_W, avail / cols - CHART_GAP - CHART_BORDER)
    w = math.floor(w * 10.0) / 10.0     # 只向下取:四舍五入可能让整行多出零点几像素
    return cols, w, round(w * CHART_ASPECT, 1)


def seg_track(w: float) -> float:
    """分段对比条的条区宽度(总宽减去左侧时间列与右侧说明列)。"""
    return max(SEG_MIN_TRACK, float(w) - SEG_LABEL_W - SEG_TAIL_W)


def seg_hit_row(x: float, y: float, n_rows: int, track: float) -> int | None:
    """分段对比条:画布坐标 → 行号(没命中返回 None)。

    与 `GuideDashboard._draw_seg` 的排版共用同一组 SEG_* 常量与同一个
    `seg_track()`。之所以抽成纯函数,是因为整块条区只挂**一个** Tapped
    (见 `_on_seg_tapped`),命中行必须靠几何反算,而几何一旦和绘制走偏就会
    点错段 —— 单测直接钉死这个函数。**条区宽度现在随面板变**,所以必须由
    调用方把绘制时用的 track 传进来(绘制时存进 `self._seg_track`)。

    命中区比逐根 Rectangle 挂事件时**更宽容**:整行 22px 高、整条轨道宽都算命中
    (逐根挂时只有 11px 高、且短段的条只有几像素宽,几乎点不中)。
    """
    if n_rows <= 0 or y < SEG_TOP:
        return None
    if not (SEG_LABEL_W <= x <= SEG_LABEL_W + track):
        return None
    k = int((y - SEG_TOP) // SEG_ROW_H)
    return k if 0 <= k < n_rows else None


def _corner(r: float) -> CornerRadius:
    cr = CornerRadius()
    cr.TopLeft = cr.TopRight = cr.BottomRight = cr.BottomLeft = r
    return cr


# ============================================================ 纯计算层
# 以下全部不碰 XAML,在 GuideDashboard 的工作线程里执行。

def _sec_arrays(sec, scale: float) -> dict | None:
    """一个导星段的有效帧数组(丢星帧已剔除)。无有效帧返回 None。"""
    valid = [f for f in sec.frames if not f.lost]
    if not valid:
        return None
    ra_px = np.asarray([f.ra_raw for f in valid], dtype=np.float64)
    dec_px = np.asarray([f.dec_raw for f in valid], dtype=np.float64)
    return {
        "sec": sec,
        "npt": np.asarray([f.time_s for f in valid], dtype=np.float64),
        "ra": ra_px * scale, "dec": dec_px * scale,
        "ra_px": ra_px, "dec_px": dec_px,
        "snr": np.asarray([f.snr for f in valid], dtype=np.float64),
        "mass": np.asarray([f.star_mass for f in valid], dtype=np.float64),
        "n": len(valid),
        "lost_t": [f.time_s for f in sec.frames if f.lost],
    }


def collect_sections(secs: list) -> dict:
    """组内导星段 → 单位口径 + 参与聚合的逐段数组。

    口径规则与 `phd2log.compute_rms` 同源:**有效帧**里只要有一帧带 pixel scale,
    就按角秒口径,无 scale 的段整段排除(角秒/像素绝不混算);全无 scale 才按像素。
    """
    with_valid = [s for s in secs if any(not f.lost for f in s.frames)]
    any_scale = any(s.pixel_scale is not None for s in with_valid)
    unit = "″" if any_scale else "px"
    used, skipped = [], []
    for s in secs:
        if any_scale and s.pixel_scale is None:
            skipped.append(s)
        else:
            used.append(s)
    arrs = []
    for s in used:
        scale = s.pixel_scale if (any_scale and s.pixel_scale is not None) else 1.0
        a = _sec_arrays(s, scale)
        if a is not None:
            arrs.append(a)
    arrs.sort(key=lambda a: a["sec"].begins)
    scales = sorted({s.pixel_scale for s in used if s.pixel_scale is not None})
    return {"unit": unit, "arcsec": any_scale, "used": used, "skipped": skipped,
            "arrs": arrs, "scales": scales}


def cov_ellipse(ra: np.ndarray, dec: np.ndarray) -> dict | None:
    """RA/DEC 偏差的 1σ 误差椭圆(以 0 为中心,手写 2×2 对称阵特征分解)。

    返回半长轴 a、半短轴 b(与输入同单位)与长轴相对 +RA 轴的角度(度,逆时针)。
    """
    n = len(ra)
    if n < 3 or len(dec) != n:
        return None
    cxx = float(np.dot(ra, ra) / n)
    cyy = float(np.dot(dec, dec) / n)
    cxy = float(np.dot(ra, dec) / n)
    tr = cxx + cyy
    if tr <= 0.0:
        return None
    d = math.sqrt(max(0.0, tr * tr / 4.0 - (cxx * cyy - cxy * cxy)))
    l1, l2 = tr / 2.0 + d, tr / 2.0 - d
    a = math.sqrt(max(l1, 0.0))
    b = math.sqrt(max(l2, 0.0))
    theta = 0.5 * math.atan2(2.0 * cxy, cxx - cyy)
    return {"a": a, "b": b, "theta_deg": math.degrees(theta),
            "ratio": (a / b) if b > 1e-12 else float("inf")}


def normal_fit(vals: np.ndarray, rng: float, bins: int, hmax: float) -> list | None:
    """直方图上的正态拟合曲线(与 bin 计数同一归一化,便于直接叠画)。"""
    n = len(vals)
    if n < 2 or rng <= 0 or hmax <= 0:
        return None
    mu = float(vals.mean())
    sd = float(vals.std())
    if sd <= 0:
        return None
    bw = 2.0 * rng / bins
    centers = -rng + bw * (np.arange(bins) + 0.5)
    pdf = np.exp(-0.5 * ((centers - mu) / sd) ** 2) / (sd * math.sqrt(2.0 * math.pi))
    return (n * bw * pdf / hmax).tolist()


def autocorr(npt: np.ndarray, vals: np.ndarray) -> dict | None:
    """RA 误差的自相关(周期误差 PE 的粗指纹,纯 numpy)。

    不等间隔帧先按中位帧间隔线性重采样到均匀网格,去均值后用 FFT 算循环自相关
    (补零到 ≥2N 使循环相关等于线性相关),再归一到 lag=0。主峰 = 滞后
    ≥20s 的局部极大里最大的那个;峰值 <0.15 视为"无明显周期"。
    数据不足(<120 有效帧 或 时长 <3 分钟 或 帧间隔异常)返回 None。
    """
    n = len(npt)
    if n < ACF_MIN_FRAMES or len(vals) != n:
        return None
    dur = float(npt[-1] - npt[0])
    if dur < ACF_MIN_DUR:
        return None
    dt = float(np.median(np.diff(npt)))
    if dt <= 0:
        return None
    m = int(dur / dt) + 1
    if m < 32 or m > 200_000:
        return None
    grid = npt[0] + np.arange(m) * dt
    y = np.interp(grid, npt, vals)
    y = y - y.mean()
    denom = float(np.dot(y, y))
    if denom <= 0:
        return None
    nfft = 1 << (2 * m - 1).bit_length()
    spec = np.fft.rfft(y, nfft)
    ac = np.fft.irfft(spec * np.conj(spec), nfft)[:m] / denom
    max_lag = min(ACF_MAX_LAG_S, dur / 2.0)
    keep = max(4, min(int(max_lag / dt) + 1, m))
    lags = np.arange(keep) * dt
    ac = ac[:keep]
    peak_lag = peak_val = None
    lo = max(1, int(math.ceil(ACF_MIN_LAG_S / dt)))
    if keep - 1 > lo:
        idx = np.arange(lo, keep - 1)
        if len(idx):
            cur, left, right = ac[idx], ac[idx - 1], ac[idx + 1]
            mask = (cur > left) & (cur >= right)
            if bool(mask.any()):
                cand = idx[mask]
                best = int(cand[int(np.argmax(ac[cand]))])
                peak_lag, peak_val = float(lags[best]), float(ac[best])
    span = float(lags[-1]) or 1.0
    pts = G._bucket_peak(list(zip((lags / span).tolist(), ac.tolist())), ACF_PTS)
    return {"pts": pts, "max_lag": float(lags[-1]), "dt": dt,
            "peak_lag": peak_lag, "peak_val": peak_val,
            "significant": peak_val is not None and peak_val >= ACF_PEAK_MIN}


def pulse_hist(secs: list) -> dict | None:
    """RA/DEC 修正脉冲时长直方图(毫秒;含全部帧,与丢星无关)。"""
    ra_d = [float(f.ra_dur) for s in secs for f in s.frames if f.ra_dur > 0]
    dec_d = [float(f.dec_dur) for s in secs for f in s.frames if f.dec_dur > 0]
    if not ra_d and not dec_d:
        return None
    allv = np.asarray(ra_d + dec_d, dtype=np.float64)
    hi = max(float(np.percentile(allv, 99.0)), 1.0)

    def _h(vals: list) -> np.ndarray:
        if not vals:
            return np.zeros(PULSE_BINS, dtype=np.float64)
        h, _edges = np.histogram(np.asarray(vals, dtype=np.float64),
                            bins=PULSE_BINS, range=(0.0, hi))
        return h.astype(np.float64)

    h_ra, h_dec = _h(ra_d), _h(dec_d)
    hmax = max(float(h_ra.max()), float(h_dec.max()), 1.0)
    return {"ra": (h_ra / hmax).tolist(), "dec": (h_dec / hmax).tolist(),
            "hi": hi, "n_ra": len(ra_d), "n_dec": len(dec_d),
            "med_ra": float(np.median(ra_d)) if ra_d else None,
            "med_dec": float(np.median(dec_d)) if dec_d else None}


def sub_series(run, arrs: list, origin: datetime, group_rms: float | None) -> dict | None:
    """每张 sub(亮场曝光)期间的导星 RMS + 废片候选。

    用组内有效帧的**绝对时刻**建全局排序数组 + 前缀平方和,逐张 sub 二分切片,
    RMS = sqrt(mean(ra²)+mean(dec²)) —— 与 `compute_rms` 同式(帧集合已按
    同一口径过滤)。覆盖不足 3 帧的 sub 跳过(给不出有意义的统计)。
    """
    if run is None or not arrs:
        return None
    shots = []
    for b in getattr(run, "blocks", []) or []:
        for g in b.groups:
            if g.frame_type in ("light", None):
                shots.extend(g.frames)
    if not shots:
        return None
    shots.sort(key=lambda f: f.time)

    xs = np.concatenate([(a["sec"].begins - origin).total_seconds() + a["npt"]
                         for a in arrs])
    ra = np.concatenate([a["ra"] for a in arrs])
    dec = np.concatenate([a["dec"] for a in arrs])
    order = np.argsort(xs, kind="stable")
    xs, ra, dec = xs[order], ra[order], dec[order]
    c = np.concatenate(([0.0], np.cumsum(ra * ra + dec * dec)))

    thr = group_rms * SUB_BAD_FACTOR if (group_rms and group_rms > 0) else None
    items = []
    for f in shots:
        a0 = (f.time - origin).total_seconds()
        a1 = (f.end_time - origin).total_seconds()
        i0 = int(np.searchsorted(xs, a0, "left"))
        i1 = int(np.searchsorted(xs, a1, "right"))
        n = i1 - i0
        if n < SUB_MIN_FRAMES:
            continue
        v = math.sqrt(max(0.0, float(c[i1] - c[i0]) / n))
        items.append({"t": f.time, "no": f.image_no, "exp": f.exposure_s,
                      "n": n, "rms": v, "bad": thr is not None and v > thr})
    if not items:
        return None
    vals = [it["rms"] for it in items]
    return {"items": items, "n_shots": len(shots), "n_rated": len(items),
            "thr": thr, "bad": sum(1 for it in items if it["bad"]),
            "mean": float(np.mean(vals)), "max": float(max(vals)),
            "min": float(min(vals))}


def _first_varying(secs: list, attr: str) -> tuple:
    """段头元数据取值:返回 (第一个非 None 值, 是否各段不一致)。"""
    vals = [getattr(s, attr, None) for s in secs]
    seen = [v for v in vals if v is not None]
    if not seen:
        return None, False
    return seen[0], len(set(seen)) > 1


def _cal_summary(cals: list) -> dict | None:
    """组内校准段汇总(取最后一次成功的校准作为"当前可用"的那次)。"""
    if not cals:
        return None
    ok = [c for c in cals if c.complete]
    last = ok[-1] if ok else cals[-1]
    ortho = None
    if last.west_angle is not None and last.north_angle is not None:
        diff = abs(last.west_angle - last.north_angle) % 360.0
        if diff > 180.0:
            diff = 360.0 - diff
        ortho = diff - 90.0
    return {"n": len(cals), "n_fail": sum(1 for c in cals if not c.complete),
            "usable": bool(ok), "begins": last.begins,
            "west_angle": last.west_angle, "west_rate": last.west_rate,
            "north_angle": last.north_angle, "north_rate": last.north_rate,
            "ortho_err": ortho, "star_lost": last.star_lost,
            "pixel_scale": last.pixel_scale, "mount": last.mount}


# ---------------------------------------------------------------- OAG 识别

# 注:实际共享名要走 shell.data_share —— 本地卡的共享名是卷标,不是这个。
# 这里只留作 shell 不可用时的兜底缺省。
PLAN_SHARE = "EMMC Images"
PLAN_LIGHT_DIR = "Plan\\Light"
OAG_TOLERANCE = 0.05        # 焦距比落在 1±5% 内即判"很可能同轴"


def oag_verdict(guide_focal_mm, main_focal_mm) -> str:
    """由两个焦距推断是不是 OAG(离轴导星)。返回徽章文案,拿不准返回空串。

    判据:导星光路与主光路焦距**相同**就说明它们是同一套光学 —— 那只能是
    OAG(或分光),不可能是独立导星镜。容差取 ±5%:两个数都是反推来的
    (导星焦距来自 PHD2 段头,主镜来自 FITS FOCALLEN),各自有误差。

    **拿不准就不显示**:任一焦距缺失、非正数,一律返回空串,绝不猜。
    文案也留余地("可能是"),因为理论上存在两台焦距碰巧相同的独立镜。

    这条对导星质量分析有实质影响:OAG 天然消除主镜/导星镜差分挠曲,
    所以"PHD2 报得漂亮但目标在主镜里漂移"这个失败模式在 OAG 上概率很低,
    极轴误差与场旋才是主要嫌疑。
    """
    try:
        g = float(guide_focal_mm or 0.0)
        m = float(main_focal_mm or 0.0)
    except (TypeError, ValueError):
        return ""
    if g <= 0.0 or m <= 0.0:
        return ""
    ratio = g / m
    if abs(ratio - 1.0) <= OAG_TOLERANCE:
        return _("可能是 OAG 导星")
    return ""


def main_focal_for(client, share: str, target: str,
                   cache: dict) -> float | None:
    """取该目标首帧 FITS 的 FOCALLEN(主镜焦距,mm)。**工作线程调用**。

    走 preview.read_fits_header —— 它已经接了 metacache(按 size+mtime 做源指纹),
    所以每个目标最多一次 SMB 往返、重启后也不用重读。任何失败都返回 None:
    这只是一个锦上添花的徽章,不值得让整个聚合失败。
    """
    if not client or not target:
        return None
    if target in cache:
        return cache[target]
    cache[target] = None                    # 先占位, 失败也不重试
    try:
        from astro_smb_gui.preview import read_fits_header
        entries = client.listdir(share or PLAN_SHARE,
                                 PLAN_LIGHT_DIR + "\\" + target)
        first = next((e for e in entries if not e.is_dir
                      and e.name.lower().endswith((".fit", ".fits", ".fts"))), None)
        if first is None:
            return None
        hdr = read_fits_header(client, first)
        raw = (hdr.cards or {}).get("FOCALLEN")
        if raw is None:
            return None
        val = float(str(raw).strip())
        cache[target] = val if val > 0 else None
        return cache[target]
    except Exception:
        return None


def aggregate_group(group: dict, rows: list, data=None) -> dict:
    """组 → 仪表盘全部数据(纯计算,工作线程执行)。

    `group` 是 `_guiding._make_group` 的产物(带 `ris`/`run`),`rows` 是
    `_guiding._prepare` 的数据行列表。返回的 dict 里 `ch` 与 `_guiding._prep_charts`
    同构,可直接喂给已参数化的 `_draw_*`。
    """
    ris = list(group.get("ris") or [])
    grows = [rows[i] for i in ris if 0 <= i < len(rows)]
    guide_rows = [r for r in grows if r["kind"] == "guide"]
    cal_rows = [r for r in grows if r["kind"] == "cal"]
    secs = [r["sec"] for r in guide_rows if r.get("sec") is not None]
    cals = [r["cal"] for r in cal_rows if r.get("cal") is not None]
    cals.sort(key=lambda c: c.begins)

    col = collect_sections(secs)
    unit = col["unit"]
    arrs = col["arrs"]
    notes: list[str] = []
    if col["skipped"]:
        notes.append(_("{0} 段无 pixel scale(像素口径),已从角秒统计中排除 —— 角秒与像素不可混算").format(
            len(col['skipped'])))
    if len(col["scales"]) > 1:
        notes.append(_("组内 pixel scale 不一致(") +
                     " / ".join(f"{v:.2f}" for v in col["scales"]) +
                     _(" ″/px),各段按各自比例折算"))

    # 权威 RMS:直接走 phd2log 的 compute_rms(混合口径规则由它保证)
    rms = compute_rms([(f, s.pixel_scale) for s in secs for f in s.frames])
    rms_total = float(rms.rms_total) if (rms and rms.n_frames > 0) else 0.0

    dur_total = sum(float(s.duration_s) for s in secs)
    dur_used = sum(float(a["sec"].duration_s) for a in arrs)
    n_valid = int(sum(a["n"] for a in arrs))
    n_lost_all = int(rms.n_lost) if rms is not None else 0
    n_frames_all = sum(len(s.frames) for s in secs)

    agg: dict = {
        "key": group.get("key"), "title": group.get("title") or "",
        "unit": unit, "arcsec": col["arcsec"],
        "n_sec": len(secs), "n_sec_used": len(arrs),
        "n_sec_skipped": len(col["skipped"]),
        "n_main": sum(1 for r in guide_rows if r.get("main_seg")),
        "t0": group.get("t0"), "t1": group.get("t1"),
        "dur_total": dur_total, "dur_used": dur_used,
        "n_frames": n_valid, "n_frames_all": n_frames_all, "n_lost": n_lost_all,
        "lost_pct": (100.0 * n_lost_all / n_frames_all) if n_frames_all else 0.0,
        "rms": rms, "rms_px": None, "scales": col["scales"],
        "ellipse": None, "drift": None, "acf": None, "pulse_hist": None,
        "subs": None, "cal": _cal_summary(cals), "notes": notes,
        "segbars": [], "ch": None, "level": G._rms_level(rms_total, unit),
        "meta": {}, "roll_marks": [], "lost_marks": [],
        "roll_short": 0, "roll_segs": 0,
    }
    # 段头元数据(各段可能不同,标注是否不一致)
    for attr in ("exposure_ms", "camera", "mount", "dec_deg", "hour_angle_hr",
                 "pier_side", "binning", "focal_len", "pixel_scale"):
        v, varies = _first_varying(col["used"] or secs, attr)
        agg["meta"][attr] = v
        agg["meta"][attr + "_varies"] = varies

    # 分段对比条(整组的段,按时间升序;被排除的段也列出但标灰)
    skipped_ids = {id(s) for s in col["skipped"]}
    ri_of = {id(rows[i]): i for i in ris if 0 <= i < len(rows)}
    bars = []
    for r in sorted(guide_rows, key=lambda x: x["begins"]):
        sec = r.get("sec")
        srms = r.get("rms")
        val = float(srms.rms_total) if (srms and srms.n_frames > 0) else None
        u = ("″" if srms.in_arcsec else "px") if srms else ""
        bars.append({
            "ri": ri_of.get(id(r)),
            "begins": r["begins"], "end": r["end"], "dur": float(r["duration"]),
            "rms": val, "unit": u, "level": G._rms_level(val, u),
            "n_frames": int(srms.n_frames) if srms else 0,
            "n_lost": int(srms.n_lost) if srms else 0,
            "main": bool(r.get("main_seg")),
            "skipped": sec is not None and id(sec) in skipped_ids,
        })
    agg["segbars"] = bars

    if not arrs:
        agg["ch"] = None
        return agg

    origin = arrs[0]["sec"].begins
    agg["origin"] = origin
    ra_all = np.concatenate([a["ra"] for a in arrs])
    dec_all = np.concatenate([a["dec"] for a in arrs])
    ra_px_all = np.concatenate([a["ra_px"] for a in arrs])
    dec_px_all = np.concatenate([a["dec_px"] for a in arrs])

    # 像素口径(双单位显示用;与角秒是同一批帧,只是不乘 scale)
    if col["arcsec"]:
        n = len(ra_px_all)
        r_ra = math.sqrt(float(np.dot(ra_px_all, ra_px_all)) / n)
        r_dec = math.sqrt(float(np.dot(dec_px_all, dec_px_all)) / n)
        agg["rms_px"] = {"ra": r_ra, "dec": r_dec,
                         "total": math.sqrt(r_ra ** 2 + r_dec ** 2),
                         "peak_ra": float(np.abs(ra_px_all).max()),
                         "peak_dec": float(np.abs(dec_px_all).max())}
    agg["ellipse"] = cov_ellipse(ra_all, dec_all)

    # 漂移:按段线性拟合后按帧数加权(跨段拼接会被段间空洞带偏)
    wsum = dra = ddec = 0.0
    for a in arrs:
        if a["n"] < 3 or float(a["npt"][-1] - a["npt"][0]) <= 1e-9:
            continue
        w = float(a["n"])
        dra += w * float(np.polyfit(a["npt"], a["ra"], 1)[0]) * 60.0
        ddec += w * float(np.polyfit(a["npt"], a["dec"], 1)[0]) * 60.0
        wsum += w
    drift = {"ra": dra / wsum, "dec": ddec / wsum} if wsum > 0 else None
    agg["drift"] = drift

    absmax = float(np.percentile(np.abs(np.concatenate((ra_all, dec_all))), 99.5))

    # a. 散点(均匀抽稀)
    ntot = len(ra_all)
    si = (np.linspace(0, ntot - 1, SCATTER_MAX).astype(np.int64)
          if ntot > SCATTER_MAX else np.arange(ntot))
    sc_pts = list(zip(ra_all[si].tolist(), dec_all[si].tolist()))
    sc_rng = max(2.6 * rms_total, 1e-6) if rms_total > 0 else max(absmax, 1e-6)

    # b. 直方图 + 正态拟合(范围 ±3×RMS)
    hr = 3.0 * rms_total if rms_total > 0 else absmax
    hist = None
    if hr > 0:
        h_ra, _edges = np.histogram(ra_all, bins=G.HIST_BINS, range=(-hr, hr))
        h_dec, _edges = np.histogram(dec_all, bins=G.HIST_BINS, range=(-hr, hr))
        hmax = float(max(int(h_ra.max()), int(h_dec.max()), 1))
        hist = {"ra": (h_ra / hmax).tolist(), "dec": (h_dec / hmax).tolist(),
                "rng": hr, "hmax": hmax,
                "fit_ra": normal_fit(ra_all, hr, G.HIST_BINS, hmax),
                "fit_dec": normal_fit(dec_all, hr, G.HIST_BINS, hmax),
                "sd_ra": float(ra_all.std()), "sd_dec": float(dec_all.std())}

    # c. 滚动 RMS(30 帧窗,**逐段**算后放到同一条绝对时间轴上;段边界画竖线)
    #
    # `G._sliding_rms` 是**尾窗**:每段前 ROLL_FRAMES-1 个样本没有满窗(index 0 就是
    # 单帧模长)。settle 之后的首帧残差常常是整段最大的一个,把这些预热样本算进来
    # 会让 roll_max 被**单点**顶穿(真机:组 RMS 0.907" 而 roll_max 3.441",正好等于
    # 首帧模长),于是"峰值"数字错、曲线又被这个假峰当量程压扁到图高的两成。
    # 故逐段丢弃未满窗的前 ROLL_FRAMES-1 个样本;不足一整窗的段整段不画,只记个数。
    warm = max(0, ROLL_FRAMES - 1)
    roll: list[tuple[float, float]] = []
    marks: list[float] = []
    roll_max = 0.0
    n_short = 0
    tot_n = max(1, sum(max(0, a["n"] - warm) for a in arrs))
    drawn = 0                   # **已画出**的段数 —— 不能拿 arrs 下标当判据
    for a in arrs:
        n_eff = a["n"] - warm
        if n_eff < 1:
            n_short += 1
            continue
        off = (a["sec"].begins - origin).total_seconds()
        tot = np.sqrt(G._sliding_rms(a["ra"], ROLL_FRAMES) ** 2
                      + G._sliding_rms(a["dec"], ROLL_FRAMES) ** 2)[warm:]
        npt = a["npt"][warm:]
        budget = max(2, int(ROLL_PTS * n_eff / tot_n))
        idx = np.unique(np.linspace(0, n_eff - 1,
                                    min(n_eff, budget)).astype(np.int64))
        roll.extend(zip((off + npt[idx]).tolist(), tot[idx].tolist()))
        roll_max = max(roll_max, float(tot.max()))
        # 边界竖线只在**前面已经画出过**至少一段时才加。用 arrs 下标做判据的话,
        # 首段被跳过时会给实际画出的第一段也补一条线(压在纵轴上)、段数还多算一。
        if drawn:
            marks.append(off + float(npt[0]))
        drawn += 1
    roll.sort(key=lambda p: p[0])
    agg["roll_marks"] = marks
    agg["roll_short"] = n_short     # 不足一整窗、整段没画的段数
    agg["roll_segs"] = drawn        # 实际画出的段数(= len(marks) + 1,除非一段都没有)

    # d. 修正脉冲方向配比(全组累计)
    cnt = {"E": 0, "W": 0, "N": 0, "S": 0}
    tot_ms = {"E": 0, "W": 0, "N": 0, "S": 0}
    for s in col["used"]:
        for f in s.frames:
            if f.ra_dur > 0 and f.ra_dir in ("E", "W"):
                cnt[f.ra_dir] += 1
                tot_ms[f.ra_dir] += f.ra_dur
            if f.dec_dur > 0 and f.dec_dir in ("N", "S"):
                cnt[f.dec_dir] += 1
                tot_ms[f.dec_dir] += f.dec_dur
    pulse = [("RA E", cnt["E"], tot_ms["E"], "ra"),
             ("RA W", cnt["W"], tot_ms["W"], "ra"),
             ("DEC N", cnt["N"], tot_ms["N"], "dec"),
             ("DEC S", cnt["S"], tot_ms["S"], "dec")]
    agg["pulse_hist"] = pulse_hist(col["used"])
    agg["pulse_balance"] = {
        "ra": (cnt["E"] - cnt["W"]) / max(1, cnt["E"] + cnt["W"]),
        "dec": (cnt["N"] - cnt["S"]) / max(1, cnt["N"] + cnt["S"]),
    }

    # e. SNR / 星质量(同一条绝对时间轴,归一到 0~1;丢星帧另存红刻度位置)
    span_t = 0.0
    for a in arrs:
        off = (a["sec"].begins - origin).total_seconds()
        span_t = max(span_t, off + float(a["npt"][-1]))
    span_t = max(span_t, 1e-9)
    snr_pairs: list[tuple[float, float]] = []
    mass_pairs: list[tuple[float, float]] = []
    snr_vals: list[float] = []
    lost_marks: list[float] = []
    for a in arrs:
        off = (a["sec"].begins - origin).total_seconds()
        budget = max(2, int(SNR_PTS * a["n"] / tot_n))
        idx = np.unique(np.linspace(0, a["n"] - 1,
                                    min(a["n"], budget)).astype(np.int64))
        tn = ((off + a["npt"][idx]) / span_t).tolist()
        snr_pairs.extend(zip(tn, a["snr"][idx].tolist()))
        mass_pairs.extend(zip(tn, a["mass"][idx].tolist()))
        snr_vals.extend(a["snr"].tolist())
        lost_marks.extend(((off + t) / span_t) for t in a["lost_t"])
    snr_pairs.sort(key=lambda p: p[0])
    mass_pairs.sort(key=lambda p: p[0])
    smax = max((v for _, v in snr_pairs), default=0.0)
    mmax = max((v for _, v in mass_pairs), default=0.0)
    snr_arr = np.asarray(snr_vals, dtype=np.float64) if snr_vals else None
    snr_chart = {
        "snr": [(t, v / smax) for t, v in snr_pairs] if smax > 0 else [],
        "mass": [(t, v / mmax) for t, v in mass_pairs] if mmax > 0 else [],
        "mean": float(snr_arr.mean()) if snr_arr is not None else 0.0,
        "std": float(snr_arr.std()) if snr_arr is not None else 0.0,
    }
    lost_marks.sort()
    # 抽稀到刻度上限(向上取整,否则 999/300 会给出 333 根)
    stride = max(1, -(-len(lost_marks) // LOST_TICKS))
    agg["lost_marks"] = lost_marks[::stride]

    # f/g. 周期图与自相关:只取**最长的那一段**(跨段拼接会让重采样失真)
    longest = max(arrs, key=lambda a: a["n"])
    agg["longest_sec"] = longest["sec"]
    period = G._prep_period(longest["npt"], longest["ra"])
    agg["acf"] = autocorr(longest["npt"], longest["ra"])

    agg["ch"] = {"unit": unit, "rms_total": rms_total, "sc_pts": sc_pts,
                 "sc_rng": sc_rng, "hist": hist, "roll": roll,
                 "roll_max": roll_max, "pulse": pulse, "drift": drift,
                 "snr": snr_chart, "period": period}

    agg["subs"] = sub_series(group.get("run"), arrs, origin,
                             rms_total if rms_total > 0 else None)
    return agg


# ---------------------------------------------------------------- 文本导出

def _fmt(v, suffix: str = "", nd: int = 2) -> str:
    return f"{v:.{nd}f}{suffix}" if v is not None else "—"


def dashboard_text(agg: dict) -> str:
    """「复制全部信息」的纯文本(与卡片同源:同一份 agg 出两种呈现)。"""
    u = agg["unit"]
    out = [_("导星仪表盘 · {0}").format(agg['title'])]
    t0, t1 = agg.get("t0"), agg.get("t1")
    if t0 and t1:
        out.append(_("时段: {t0:%Y-%m-%d %H:%M} — {t1:%H:%M}").format(t0=t0, t1=t1))
    out.append(_("导星段: {0} 段(主段 {1}) · 总导星时长 {2}").format(
        agg['n_sec'], agg['n_main'], G._fmt_hours(agg['dur_total'])))
    out.append(_("有效帧 {0} · 丢星 {1}({2:.1f}%)").format(
        agg['n_frames'], agg['n_lost'], agg['lost_pct']))
    rms = agg.get("rms")
    if rms is not None and rms.n_frames > 0:
        line = (f"RMS Total {rms.rms_total:.2f}{u}"
                f"(RA {rms.rms_ra:.2f} / DEC {rms.rms_dec:.2f})")
        px = agg.get("rms_px")
        if px:
            line += (f" = {px['total']:.3f} px"
                     f"(RA {px['ra']:.3f} / DEC {px['dec']:.3f})")
        out.append(line)
        out.append(_("峰值 RA {peak_ra:.2f}{u} · DEC {peak_dec:.2f}{u}").format(
            peak_ra=rms.peak_ra, u=u, peak_dec=rms.peak_dec))
    el = agg.get("ellipse")
    if el:
        out.append(_("RMS 椭圆: 长轴 {0:.2f}{u} · 短轴 {1:.2f}{u} · 方位 {2:+.0f}°").format(
            el['a'], el['b'], el['theta_deg'], u=u))
    d = agg.get("drift")
    if d:
        out.append(_("漂移(帧数加权): RA {0:+.2f}{u}/min · DEC {1:+.2f}{u}/min").format(
            d['ra'], d['dec'], u=u))
    m = agg["meta"]
    bits = []
    if m.get("pixel_scale") is not None:
        bits.append(_("比例 {0:.2f}″/px").format(m['pixel_scale']))
    if m.get("exposure_ms") is not None:
        bits.append(_("曝光 {0}ms").format(m['exposure_ms']))
    if m.get("camera"):
        bits.append(_("导星相机 {0}").format(m['camera']))
    if m.get("dec_deg") is not None:
        bits.append(_("赤纬 {0:.1f}°").format(m['dec_deg']))
    if m.get("pier_side"):
        bits.append(G._PIER_CN.get(m["pier_side"], m["pier_side"]))
    if bits:
        out.append(" · ".join(bits))
    cal = agg.get("cal")
    if cal:
        out.append(_("校准 {0} 次").format(cal['n'])
                   + (_("({0} 失败)").format(cal['n_fail']) if cal["n_fail"] else "")
                   + (_(" · 有可用校准") if cal["usable"] else _(" · 无成功校准")))
        if cal.get("ortho_err") is not None:
            out.append(_("校准正交误差 {0:+.1f}°").format(cal['ortho_err']))
    acf = agg.get("acf")
    if acf:
        out.append(_("RA 自相关主峰: ")
                   + (_("{0:.0f}s(相关 {1:.2f})").format(acf['peak_lag'], acf['peak_val'])
                      if acf.get("significant") else _("无明显周期")))
    subs = agg.get("subs")
    if subs:
        out.append(_("每张 sub 导星 RMS: {0}/{1} 张有覆盖 · 平均 {2:.2f}{u} · 最差 {3:.2f}{u} · 废片候选 {4} 张").format(
            subs['n_rated'], subs['n_shots'], subs['mean'], subs['max'], subs['bad'], u=u))
    for n in agg.get("notes") or []:
        out.append(_("注: {n}").format(n=n))
    return "\n".join(out)


# ============================================================ 视图层

class GuideDashboard:
    """导星仪表盘面板。生命周期由 `_guiding.GuidingPage` 持有。

    UI 线程构造(懒建于第一次点「仪表盘」),面板挂进导星页右侧的 `DashHost`;
    **可见性归页面管**(`GuidingPage._set_view`),本类只负责画与不画。
    聚合在工作线程算,结果经 `shell.ui(...)` 编组回来画;单飞用代次计数器,
    过期结果丢弃。
    """

    def __init__(self, page):
        self.page = page
        self.shell = page.shell
        self.root = XamlReader.Load(_xaml_text(XAML_PATH)).as_(FrameworkElement)

        self._live = False                    # 面板当前在显(= 页面处于仪表盘视图)
        self._gen = 0
        self._group: dict | None = None
        self._agg: dict | None = None
        self._cache: dict[str, dict] = {}     # 组键 → agg(进程内,随数据换代作废)
        self._cache_src = None                # _cache 对应的 LogData
        self._seg_rows: list[dict] = []       # 当前画在分段对比条上的段(命中反算用)
        self._seg_track = seg_track(PANEL_W_DEFAULT)   # 画分段条时的条区宽(命中反算用)
        self._panel_w = PANEL_W_DEFAULT       # 正文可用宽(SizeChanged 校正)
        # show() 刚量的宽在收到本轮首个 SizeChanged 之前是权威 ——
        # 面板 Collapsed 期间 body.ActualWidth 是上一次布局的旧值
        self._panel_w_fresh = False
        self._focal_cache: dict[str, float | None] = {}   # 目标 → 主镜 FOCALLEN
        self._size_gen = 0                    # 宽度防抖代次(拖窗口只重画最后一次)

        # 画刷建一次复用(渲染函数里绝不新建 SolidColorBrush)
        self._tone = {k: _brush(*rgb) for k, rgb in _TONE_RGB.items()}
        self._divider = _brush(0x80, 0x80, 0x80, 0x3C)
        self._pill_bg = _brush(0x80, 0x80, 0x80, 0x28)
        self._mono = FontFamily("Consolas")
        self._b_ra = _brush(0, 120, 215)
        self._b_dec = _brush(240, 140, 0)
        self._b_ra_dim = _brush(0, 120, 215, 110)
        self._b_dec_dim = _brush(240, 140, 0, 110)
        self._b_grid = _brush(128, 128, 128, 60)
        self._b_axis = _brush(128, 128, 128, 200)
        self._b_lost = _brush(0xE5, 0x73, 0x73)
        self._b_good = _brush(0x4C, 0xAF, 0x50)
        self._b_warn = _brush(0xFF, 0xB3, 0x00)
        self._b_dim = _brush(0x9E, 0x9E, 0x9E, 200)
        self._b_ell = _brush(0x9C, 0x27, 0xB0, 220)   # RMS 椭圆紫
        self._b_fit = _brush(0x66, 0x66, 0x66, 220)   # 正态拟合灰
        self._level_brush = {"good": self._b_good, "warn": self._b_warn,
                             "bad": self._b_lost}

        self._find()
        self._wire()
        self._attach()

    # ---------- 装配 ----------

    def _find(self) -> None:
        f = self.root.FindName
        self.title = f("DashTitle").as_(TextBlock)
        self.subtitle = f("DashSubTitle").as_(TextBlock)
        self.ring = f("DashRing").as_(ProgressRing)
        self.copy_btn = f("DashCopyBtn").as_(Button)
        self.back_btn = f("DashBackBtn").as_(Button)
        self.scroll = f("DashScroll").as_(ScrollViewer)
        self.body = f("DashBody").as_(StackPanel)
        self.wrap = f("DashWrap").as_(VariableSizedWrapGrid)
        self.badges = f("DashBadges").as_(StackPanel)
        self.target_text = f("DashTargetText").as_(TextBlock)
        self.sub_text = f("DashSubText").as_(TextBlock)
        self.pills = f("DashPills").as_(StackPanel)
        self.kv = f("DashKv").as_(Grid)
        self.notes = f("DashNotes").as_(TextBlock)
        self.charts = f("DashCharts").as_(StackPanel)
        self.cv_scatter = f("DashScatterCanvas").as_(Canvas)
        self.cv_hist = f("DashHistCanvas").as_(Canvas)
        self.cv_roll = f("DashRollCanvas").as_(Canvas)
        self.cv_pulse = f("DashPulseCanvas").as_(Canvas)
        self.cv_pulse_hist = f("DashPulseHistCanvas").as_(Canvas)
        self.cv_snr = f("DashSnrCanvas").as_(Canvas)
        self.cv_acf = f("DashAcfCanvas").as_(Canvas)
        self.cv_period = f("DashPeriodCanvas").as_(Canvas)
        self.cv_seg = f("DashSegCanvas").as_(Canvas)
        self.sub_card = f("DashSubCard").as_(StackPanel)
        self.cv_sub = f("DashSubCanvas").as_(Canvas)
        self.empty = f("DashEmpty").as_(TextBlock)

    def _wire(self) -> None:
        self.back_btn.Click += self._on_back
        self.copy_btn.Click += self._on_copy
        # 分段对比条:**整块画布只挂一个** Tapped。win32more 的 event 描述符会把
        # 实例存进类级 _event_setters 且从不移除(clear()/-= 只清 _callbacks),
        # 逐根 Rectangle 挂事件 = 每开一次仪表盘永久滞留一批 Rectangle + 闭包。
        # 画布是 xaml 里的固定控件,这里只注册一次,命中行靠 seg_hit_row 反算。
        self.cv_seg.Tapped += self._on_seg_tapped
        # 正文宽度(= 面板可用宽)变化 → 防抖后按新宽度重排图表。同理只挂一次。
        self.body.SizeChanged += self._on_body_size

    def _attach(self) -> None:
        """把面板挂进导星页右侧的视图宿主(可见性由页面的 _set_view 管)。"""
        self.page.dash_host.as_(Grid).Children.Append(self.root)

    # ---------- 生命周期 ----------

    def invalidate(self) -> None:
        """数据换代:聚合缓存整体作废,在显的面板退回段视图。

        组键里带的是**旧一代的行索引**(`_make_group` 的 `ris`),留着只会画出
        错位的东西;而 `_apply_data` 紧接着就会重建列表并选中一个默认段,
        退回段视图正好接上 —— 这也是"数据一换就能立刻看到新内容"的最短路径。
        重新点一次组头的「仪表盘」就按新一代数据重算。
        """
        self._cache.clear()
        self._cache_src = None
        self._agg = None
        self._gen += 1
        if self._live:
            self.page.show_segment_view()      # 内部会回调本类的 hide()

    def hide(self) -> None:
        """面板切走:停掉在途聚合 + 清空画布。

        **不动 Visibility** —— 那是 `GuidingPage._set_view` 的职责(两个视图
        叠在同一格,由页面统一切,免得两边各改一半对不上)。
        """
        if not self._live:
            return
        self._live = False
        self._gen += 1                  # 在途聚合结果作废
        self._size_gen += 1             # 在途的防抖重画也作废
        self._clear_canvases()

    def _on_back(self, sender, e) -> None:
        self.page.show_segment_view()

    def _clear_canvases(self) -> None:
        self._seg_rows = []     # 条已清掉,残留的命中表不能再被 Tapped 用上
        for cv in (self.cv_scatter, self.cv_hist, self.cv_roll, self.cv_pulse,
                   self.cv_pulse_hist, self.cv_snr, self.cv_acf, self.cv_period,
                   self.cv_seg, self.cv_sub):
            try:
                cv.Children.Clear()
            except Exception:
                pass

    def show(self, group: dict, rows: list, src, panel_w: float = 0.0) -> None:
        """在右侧画出某组的仪表盘(UI 线程调用)。聚合命中缓存则同步渲染。

        `panel_w` 由页面在**切视图之前**量好传进来:切到仪表盘的那一帧面板还
        没被布局过,`self.body.ActualWidth` 是 0,直接用会先按缺省宽画一遍再
        被 SizeChanged 纠正(白画一次;聚合命中缓存时是同步渲染,更是必然撞上)。
        页面给的是**整块右侧区**的宽,正文还要减去 ScrollViewer 的右侧内边距 ——
        差这 8px 就足以让一行少排一列(见 CHART_SLACK 那段实测)。
        """
        self._group = group
        self._live = True
        if panel_w and panel_w > 1.0:
            self._panel_w = float(panel_w) - SCROLL_PAD
            self._panel_w_fresh = True
        self._sync_hscroll()
        self.title.Text = _("导星仪表盘 · {0}").format(group.get('title') or '')
        self.subtitle.Text = group.get("sub") or ""
        try:
            self.scroll.ChangeView(None, 0.0, None)
        except Exception:
            pass
        if self._cache_src is not src:
            self._cache.clear()
            self._cache_src = src
        self._gen += 1
        gen = self._gen
        key = group.get("key") or ""
        agg = self._cache.get(key)
        if agg is not None:
            self._apply(gen, key, agg)
            return
        self._busy(True)
        self._show_placeholder(_("正在聚合本组导星数据…"))

        def work():
            try:
                a = aggregate_group(group, rows, src)
                # 主镜焦距(用于 OAG 判断)。**必须在工作线程里取**:要发 SMB。
                # 拿不到就算了 —— 这只是个锦上添花的徽章,不值得让聚合失败。
                run = group.get("run")
                target = getattr(run, "target", "") if run is not None else ""
                if target:
                    try:
                        cli = self.shell.client.clone()
                    except Exception:
                        cli = None
                    if cli is not None:
                        try:
                            a["main_focal"] = main_focal_for(
                                cli, getattr(self.shell, "data_share", ""),
                                target, self._focal_cache)
                        finally:
                            try:
                                cli.close()
                            except Exception:
                                pass
                self.shell.ui(self._apply, gen, key, a)
            except Exception as ex:     # 工作线程异常不许静默(§11)
                self.shell.ui(self._failed, gen, f"{type(ex).__name__}: {ex}")

        threading.Thread(target=work, daemon=True, name="guidedash-agg").start()

    # ---------- 宽度自适应(防抖重排) ----------

    def _on_body_size(self, sender, e) -> None:
        """正文可用宽变了(窗口缩放 / 导航面板折叠)→ 防抖后按新宽度重排。

        重排就是重跑一遍 `_draw_charts`(几百毫秒),拖窗口边框期间每帧都跑
        会卡死;所以带一个容差(布局抖动不算)+ 一个代次防抖:只有最后一次
        变化真正落到重画上。UI 线程的 asyncio 循环拿不到时(探针/极端时序)
        退回立即重画,画总比不画好。
        """
        try:
            w = float(e.NewSize.Width)
        except Exception:
            return
        self._panel_w_fresh = False     # 拿到真实布局宽, 之后以实测为准
        if w <= 1.0 or abs(w - self._panel_w) < RESIZE_TOLERANCE:
            self._sync_hscroll()
            return
        self._panel_w = w
        self._sync_hscroll()
        if not self._live or self._agg is None:
            return                      # 没在显就只记宽度,下次 show 时用得上
        self._size_gen += 1
        gen = self._size_gen
        try:
            asyncui.create_task(self._resize_later(gen))
        except Exception:
            self._redraw_charts(gen)

    async def _resize_later(self, gen: int) -> None:
        await asyncio.sleep(RESIZE_DEBOUNCE_S)
        self._redraw_charts(gen)

    def _redraw_charts(self, gen: int) -> None:
        if gen != self._size_gen or not self._live:
            return
        agg = self._agg
        if agg is None or agg.get("ch") is None:
            return
        try:
            self._draw_charts(agg)
        except Exception as ex:         # 事件/协程里的异常会被吞,必须落地
            self.shell.error(_("仪表盘按新宽度重排失败: {__name__}: {ex}").format(
                __name__=type(ex).__name__, ex=ex))

    def _wide_width(self) -> float:
        """通栏图(分段对比 / sub RMS)的画布宽 = 正文可用宽。

        **`show(panel_w=…)` 刚给的值优先于 `body.ActualWidth`**:面板隐藏期间
        (Collapsed)ActualWidth 保留的是上一次布局的**旧宽**。真机实测:窗口从
        1400 缩到 886 后再开仪表盘,body.ActualWidth 仍是 811.2,于是把一整屏
        811px 宽、3 列的图表画进 310px 的视口里,要等 SizeChanged 防抖 0.25s
        之后才纠正 —— 用户会看到一次明显的错版闪跳。
        页面在切视图**之前**量的那个值才是当下真实的,收到本轮第一次
        SizeChanged 之前一律以它为准。
        """
        if not self._panel_w_fresh:
            try:
                w = float(self.body.ActualWidth or 0.0)
            except Exception:
                w = 0.0
            if w > 1.0:
                self._panel_w = w
        return max(WIDE_MIN, self._panel_w)

    def _sync_hscroll(self) -> None:
        """正文最小需求宽真的塞不下时,才允许横向滚动。

        原来横滚被无条件 Disabled,而通栏图仍按 `WIDE_MIN` 画 —— 视口窄于
        300px 时超出的部分被**静默裁掉,且用户没有任何办法看到**
        (审查实测:窗口 800 → 视口 210,画布仍是 300;截图里小图右边框消失、
        周期图「600s」刻度被切成「60」)。注意 `HorizontalScrollMode=Disabled`
        时 `ScrollableWidth` 恒为 0,所以"实测 hscroll=0 ⇒ 没裁切"是**无效推断**
        —— 这条差点让问题溜过去。
        """
        try:
            avail = float(self.body.ActualWidth or 0.0) or self._panel_w
            need = max(WIDE_MIN, CHART_MIN_W + CHART_BORDER + CHART_GAP)
            on = need > avail + 1.0
            self.scroll.HorizontalScrollMode = (
                ScrollMode.Enabled if on else ScrollMode.Disabled)
            self.scroll.HorizontalScrollBarVisibility = (
                ScrollBarVisibility.Auto if on else ScrollBarVisibility.Disabled)
        except Exception:
            pass

    def _busy(self, on: bool) -> None:
        self.ring.IsActive = on
        self.copy_btn.IsEnabled = not on

    def _show_placeholder(self, text: str) -> None:
        """收起图表并显示占位文字。

        **汇总卡也必须一起清掉** —— 它的徽章/目标名/RMS/分区 KV/注记都是
        DashBody 的直接子元素,不在 charts 里。组间直切时只换标题不清汇总卡,
        用户会看到「B 组的标题 + A 组的 RMS 和废片阈值」,一直持续到工作线程
        回来。在分析页里读错组的数字比卡顿危险得多;面板化之后左侧列表全程
        可见,组间直切是常规操作,撞上的概率比遮罩时代高得多。
        """
        self.charts.Visibility = Visibility.Collapsed
        self._clear_summary()
        self.empty.Visibility = Visibility.Visible
        self.empty.Text = text

    def _clear_summary(self) -> None:
        """把汇总卡恢复成"什么都还不知道"的状态(幂等,不抛)。"""
        for panel in (self.badges, self.pills):
            try:
                panel.Children.Clear()
                panel.Visibility = Visibility.Collapsed
            except Exception:
                pass
        try:
            self.kv.Children.Clear()
            self.kv.RowDefinitions.Clear()
        except Exception:
            pass
        try:
            # 目标名保留(标题已经换过了, 留着让用户知道正在聚合的是哪一组),
            # 但一切**数字**必须清空
            self.target_text.Text = (self._group or {}).get("title") or ""
            self.sub_text.Text = ""
            self.notes.Visibility = Visibility.Collapsed
        except Exception:
            pass

    def _failed(self, gen: int, msg: str) -> None:
        if gen != self._gen:
            return
        self._busy(False)
        self._show_placeholder(_("聚合失败:{msg}").format(msg=msg))
        self.shell.error(_("导星仪表盘聚合失败: {msg}").format(msg=msg))

    def _apply(self, gen: int, key: str, agg: dict) -> None:
        if gen != self._gen or not self._live:
            return
        self._cache[key] = agg
        self._agg = agg
        try:
            # 进度环必须**画完才关**:_render 在 UI 线程铺元素,期间界面是冻的,
            # 提前关掉等于在冻结开始前先撤掉"还在忙"的唯一提示
            self._render(agg)
        except Exception as ex:
            self._show_placeholder(_("渲染失败:{__name__}: {ex}").format(
                __name__=type(ex).__name__, ex=ex))
            self.shell.error(_("导星仪表盘渲染失败: {__name__}: {ex}").format(
                __name__=type(ex).__name__, ex=ex))
        finally:
            self._busy(False)

    def _on_copy(self, sender, e) -> None:
        if self._agg is None:
            return
        try:
            pack = DataPackage()
            pack.SetText(dashboard_text(self._agg))
            Clipboard.SetContent(pack)
            try:
                Clipboard.Flush()   # 让内容在 DataPackage 释放后仍留在剪贴板
            except Exception:
                pass
            self.shell.info(_("仪表盘信息已复制到剪贴板"))
        except Exception as ex:
            self.shell.error(_("复制仪表盘信息失败: {ex}").format(ex=ex))

    def _on_seg_click(self, bar: dict) -> None:
        """点分段对比条 → 在段视图的主曲线上定位该段。

        不用自己收面板:`page.show_range` 本来就会强制切回段视图
        (定位结果画在大曲线上,不切回去什么也看不见)。
        """
        try:
            label = f"{self._group.get('title') if self._group else ''}"
            self.page.show_range(bar["begins"], bar["end"], label)
        except Exception as ex:
            self.shell.error(_("定位导星段失败: {__name__}: {ex}").format(
                __name__=type(ex).__name__, ex=ex))

    # ---------- 排版原语(设计语言照抄浏览页详情卡:卡片 + 分区标题 + KV 行) ----------
    # 本仓库惯例是各页各持一份同风格私有 helper(_records/_space/_monitor 皆然),
    # 不跨页 import、不抽公共库。

    def _add_row(self, grid: Grid) -> None:
        rd = RowDefinition()
        rd.Height = GridLength(Value=1.0, GridUnitType=GridUnitType.Auto)
        grid.RowDefinitions.Append(rd)

    def _add_pairs(self, grid: Grid, row: int, pairs: list) -> int:
        """(标签, 值[, 副注[, 等宽[, 语义色]]]) 逐行填进两列 Grid,返回下一行号。

        标签淡色;值可选中复制;副注以淡色小字随主值横排;等宽=True 用 Consolas;
        语义色只染主值(标签保持淡色)。
        """
        for item in pairs:
            k, v = item[0], item[1]
            note = item[2] if len(item) > 2 else ""
            mono = item[3] if len(item) > 3 else False
            tone = item[4] if len(item) > 4 else None
            self._add_row(grid)
            lab = TextBlock()
            lab.Text = k
            lab.FontSize = 12
            lab.Opacity = 0.55
            grid.Children.Append(lab)
            Grid.SetRow(lab, row)
            Grid.SetColumn(lab, 0)
            val = TextBlock()
            val.Text = v
            val.FontSize = 12
            val.TextWrapping = TextWrapping.Wrap
            val.IsTextSelectionEnabled = True
            val.VerticalAlignment = VerticalAlignment.Center
            if mono:
                val.FontFamily = self._mono
            brush = self._tone.get(tone) if tone else None
            if brush is not None:
                val.Foreground = brush
                val.FontWeight = FontWeights.SemiBold
            if note:
                panel = StackPanel()
                panel.Orientation = Orientation.Horizontal
                panel.Spacing = 6
                panel.VerticalAlignment = VerticalAlignment.Center
                panel.Children.Append(val)
                aux = TextBlock()
                aux.Text = note
                aux.FontSize = 11
                aux.Opacity = 0.55
                aux.TextWrapping = TextWrapping.Wrap
                aux.VerticalAlignment = VerticalAlignment.Center
                panel.Children.Append(aux)
                grid.Children.Append(panel)
                Grid.SetRow(panel, row)
                Grid.SetColumn(panel, 1)
            else:
                grid.Children.Append(val)
                Grid.SetRow(val, row)
                Grid.SetColumn(val, 1)
            row += 1
        return row

    def _add_group_header(self, grid: Grid, row: int, glyph: str, name: str,
                          first: bool = False) -> int:
        """分区小标题:图标 + 组名(淡色小字)+ 一条细分隔线,横跨两列。"""
        self._add_row(grid)
        head = Grid()
        for width, unit in ((1.0, GridUnitType.Auto), (1.0, GridUnitType.Auto),
                            (1.0, GridUnitType.Star)):
            c = ColumnDefinition()
            c.Width = GridLength(Value=width, GridUnitType=unit)
            head.ColumnDefinitions.Append(c)
        head.Margin = Thickness(Left=0, Top=(1 if first else 9), Right=0, Bottom=1)
        icon = FontIcon()
        icon.Glyph = glyph
        icon.FontSize = 11
        icon.Opacity = 0.55
        icon.VerticalAlignment = VerticalAlignment.Center
        head.Children.Append(icon)
        Grid.SetColumn(icon, 0)
        lab = TextBlock()
        lab.Text = name
        lab.FontSize = 11
        lab.Opacity = 0.55
        lab.Margin = Thickness(Left=6, Top=0, Right=0, Bottom=0)
        lab.VerticalAlignment = VerticalAlignment.Center
        head.Children.Append(lab)
        Grid.SetColumn(lab, 1)
        line = Border()
        line.Height = 1
        line.Background = self._divider
        line.VerticalAlignment = VerticalAlignment.Center
        line.Margin = Thickness(Left=8, Top=0, Right=0, Bottom=0)
        head.Children.Append(line)
        Grid.SetColumn(line, 2)
        grid.Children.Append(head)
        Grid.SetRow(head, row)
        Grid.SetColumn(head, 0)
        Grid.SetColumnSpan(head, 2)
        return row + 1

    def _fill_groups(self, grid: Grid, groups: list) -> None:
        """清空后按 (图标, 组名, 键值对) 分区填充;空组自动跳过。"""
        grid.RowDefinitions.Clear()
        grid.Children.Clear()
        row = 0
        for glyph, name, pairs in groups:
            if not pairs:
                continue
            row = self._add_group_header(grid, row, glyph, name, first=(row == 0))
            row = self._add_pairs(grid, row, pairs)

    def _render_badges(self, badges: list) -> None:
        """徽章行:圆角小胶囊(浅色底深色字)。

        控件直接复用 `GuidingPage._chip` —— 段视图标题行用的是同一个函数,
        两个右侧视图的胶囊因此不可能走样。
        """
        self.badges.Children.Clear()
        for text, style in badges:
            self.badges.Children.Append(self.page._chip(text, style))
        self.badges.Visibility = (Visibility.Visible if badges
                                  else Visibility.Collapsed)

    def _render_pills(self, pills: list) -> None:
        """参数胶囊行:中性底色小 pill,语义色只染文字。"""
        self.pills.Children.Clear()
        for item in pills:
            text = item[0]
            tip = item[1] if len(item) > 1 else ""
            tone = item[2] if len(item) > 2 else None
            pill = Border()
            pill.CornerRadius = _corner(4.0)
            pill.Background = self._pill_bg
            pill.Padding = Thickness(Left=7, Top=1, Right=7, Bottom=2)
            tb = TextBlock()
            tb.Text = text
            tb.FontSize = 12
            tb.FontWeight = FontWeights.SemiBold
            brush = self._tone.get(tone) if tone else None
            if brush is not None:
                tb.Foreground = brush
            pill.Child = tb
            if tip:
                ToolTipService.SetToolTip(pill, tip)
            self.pills.Children.Append(pill)
        self.pills.Visibility = (Visibility.Visible if pills
                                 else Visibility.Collapsed)

    # ---------- 渲染(UI 线程;数据已在工作线程算好,一次性铺完不再重绘) ----------

    def _render(self, agg: dict) -> None:
        self._summary(agg)
        notes = agg.get("notes") or []
        self.notes.Text = " · ".join(notes)
        self.notes.Visibility = (Visibility.Visible if notes
                                 else Visibility.Collapsed)
        if agg.get("ch") is None:
            self._clear_canvases()
            self.charts.Visibility = Visibility.Collapsed
            self.empty.Visibility = Visibility.Visible
            self.empty.Text = (_("本组没有可统计的导星帧(只有校准记录,或全部帧都是丢星帧)"))
            return
        self.empty.Visibility = Visibility.Collapsed
        self.charts.Visibility = Visibility.Visible
        self._draw_charts(agg)

    def _summary(self, agg: dict) -> None:
        u = agg["unit"]
        rms = agg.get("rms")
        has_rms = rms is not None and rms.n_frames > 0
        lost_pct = agg["lost_pct"]
        lost_tone = ("good" if lost_pct < LOST_GOOD
                     else "warn" if lost_pct < LOST_WARN else "bad")

        badges = [(_("{0} 段导星").format(agg['n_sec']), "neutral")]
        if has_rms:
            badges.append((f"RMS {rms.rms_total:.2f}{u}",
                           agg.get("level") or "info"))
        badges.append((_("丢星 {lost_pct:.1f}%").format(lost_pct=lost_pct), lost_tone))
        cal = agg.get("cal")
        if cal is None:
            badges.append((_("无校准记录"), "neutral"))
        elif cal["usable"]:
            badges.append((_("校准可用({0} 次)").format(cal['n']), "good"))
        else:
            badges.append((_("校准全部失败({0} 次)").format(cal['n']), "bad"))
        if agg["n_sec_skipped"]:
            badges.append((_("{0} 段像素口径已排除").format(agg['n_sec_skipped']), "warn"))
        self._render_badges(badges)

        self.target_text.Text = agg["title"] or _("(未匹配到拍摄目标)")
        t0, t1 = agg.get("t0"), agg.get("t1")
        sub = []
        if t0 and t1:
            sub.append(f"{t0:%Y-%m-%d %H:%M} — {t1:%H:%M}")
        sub.append(_("导星 {0}").format(G._fmt_hours(agg['dur_total'])))
        sub.append(_("有效 {0} 帧").format(agg['n_frames']))
        self.sub_text.Text = " · ".join(sub)

        m = agg["meta"]
        pills = []
        if m.get("exposure_ms") is not None:
            pills.append((_("曝光 {0}ms").format(m['exposure_ms']), _("导星相机曝光时长")))
        if m.get("pixel_scale") is not None:
            pills.append((f"{m['pixel_scale']:.2f}″/px", _("导星像元比例")))
        if m.get("binning") is not None:
            pills.append((f"Bin{m['binning']}", _("导星相机 binning")))
        if m.get("camera"):
            pills.append((m["camera"], _("导星相机")))
        # 导星光路与主光路焦距相同 ⇒ 只能是同一套光学 = OAG(或分光)。
        # 拿不准就不显示(oag_verdict 会返回空串), 绝不猜。
        oag = oag_verdict(m.get("focal_len"), agg.get("main_focal"))
        if oag:
            pills.append((oag, _("导星焦距与主镜 FOCALLEN 相同({0:.0f} mm),推断为同轴导星").format(
                m.get('focal_len'))))
        self._render_pills(pills)

        # ---- 分区 KV ----
        q = []
        if has_rms:
            px = agg.get("rms_px")
            note = (f"= {px['total']:.3f} px" if px else "")
            q.append(("Total RMS", f"{rms.rms_total:.2f}{u}", note, True,
                      agg.get("level")))
            q.append(("RA RMS", f"{rms.rms_ra:.2f}{u}",
                      f"= {px['ra']:.3f} px" if px else "", True, None))
            q.append(("DEC RMS", f"{rms.rms_dec:.2f}{u}",
                      f"= {px['dec']:.3f} px" if px else "", True, None))
            q.append((_("峰值误差"),
                      f"RA {rms.peak_ra:.2f}{u} / DEC {rms.peak_dec:.2f}{u}",
                      "", True, None))
        else:
            q.append(("RMS", _("无有效帧"), "", False, "dim"))
        el = agg.get("ellipse")
        if el:
            ratio = ("—" if math.isinf(el["ratio"]) else f"{el['ratio']:.2f}")
            q.append((_("RMS 椭圆"),
                      f"{el['a']:.2f} × {el['b']:.2f}{u}",
                      _("轴比 {ratio} · 方位 {0:+.0f}°").format(
                          el['theta_deg'], ratio=ratio), True, None))
        acf = agg.get("acf")
        if acf is not None:
            if acf.get("significant"):
                q.append((_("周期误差主峰"), f"{acf['peak_lag']:.0f} s",
                          _("自相关 {0:.2f}").format(acf['peak_val']), False, "warn"))
            else:
                q.append((_("周期误差主峰"), _("无明显周期"), "", False, "dim"))

        fr = [
            (_("有效帧 / 总帧"), f"{agg['n_frames']} / {agg['n_frames_all']}", "",
             False, None),
            (_("丢星帧"), f"{agg['n_lost']}", f"({lost_pct:.1f}%)", False, lost_tone),
            (_("总导星时长"), G._fmt_hours(agg["dur_total"]),
             (_("参与统计 {0}").format(G._fmt_hours(agg['dur_used']))
              if agg["n_sec_skipped"] else ""), False, None),
            (_("段数"), _("{0} 段").format(agg['n_sec']),
             _("主段 {0} · 短尝试 {1}").format(agg['n_main'], agg['n_sec'] - agg['n_main']),
             False, None),
        ]

        dr = []
        d = agg.get("drift")
        if d:
            warn = u == "″" and abs(d["dec"]) > G.DRIFT_DEC_WARN
            dr.append((_("RA 漂移"), f"{d['ra']:+.2f}{u}/min", "", True, None))
            dr.append((_("DEC 漂移"), f"{d['dec']:+.2f}{u}/min",
                       _("(建议检查极轴)") if warn else "", True,
                       "warn" if warn else None))
        bal = agg.get("pulse_balance")
        if bal:
            dr.append((_("脉冲方向配比"),
                       f"RA {bal['ra']:+.2f} · DEC {bal['dec']:+.2f}",
                       _("(+ 偏 E/N,− 偏 W/S)"), True, None))

        op = []
        if m.get("pixel_scale") is not None:
            op.append((_("像元比例"), f"{m['pixel_scale']:.2f}″/px",
                       _("(各段不一致)") if m.get("pixel_scale_varies") else "",
                       False, None))
        if m.get("focal_len") is not None:
            op.append((_("导星镜焦距"), f"{m['focal_len']:.0f} mm", "", False, None))
        if m.get("camera"):
            op.append((_("导星相机"), m["camera"], "", False, None))
        if m.get("binning") is not None:
            op.append(("Binning", f"Bin{m['binning']}", "", False, None))

        ge = []
        if m.get("mount"):
            ge.append((_("赤道仪"), m["mount"], "", False, None))
        if m.get("dec_deg") is not None:
            ge.append((_("赤纬"), f"{m['dec_deg']:.2f}°",
                       _("(各段不一致)") if m.get("dec_deg_varies") else "",
                       True, None))
        if m.get("hour_angle_hr") is not None:
            ge.append((_("时角"), f"{m['hour_angle_hr']:+.2f} hr", "", True, None))
        if m.get("pier_side"):
            ge.append(("Pier side",
                       G._PIER_CN.get(m["pier_side"], m["pier_side"]),
                       _("(各段不一致)") if m.get("pier_side_varies") else "",
                       False, None))

        cl = []
        if cal:
            cl.append((_("校准次数"), _("{0} 次").format(cal['n']),
                       _("{0} 次失败").format(cal['n_fail']) if cal["n_fail"] else _("全部成功"),
                       False, "bad" if cal["n_fail"] else "good"))
            cl.append((_("可用校准"), _("有") if cal["usable"] else _("无"), "", False,
                       "good" if cal["usable"] else "bad"))
            cl.append((_("最近一次"), f"{cal['begins']:%m-%d %H:%M:%S}", "", False, None))
            if cal.get("west_angle") is not None:
                cl.append(("West", f"{cal['west_angle']:.1f}°",
                           _fmt(cal.get("west_rate"), " px/s", 4), True, None))
            if cal.get("north_angle") is not None:
                cl.append(("North", f"{cal['north_angle']:.1f}°",
                           _fmt(cal.get("north_rate"), " px/s", 4), True, None))
            if cal.get("ortho_err") is not None:
                bad = abs(cal["ortho_err"]) > 5.0
                cl.append((_("正交误差"), f"{cal['ortho_err']:+.1f}°",
                           _("(RA/DEC 轴不正交,>5° 需重新校准)") if bad else "",
                           True, "warn" if bad else "good"))

        sh = []
        subs = agg.get("subs")
        if subs:
            sh.append((_("有覆盖的 sub"),
                       _("{0} / {1} 张").format(
                           subs['n_rated'], subs['n_shots']), "", False, None))
            sh.append((_("sub 平均 RMS"), f"{subs['mean']:.2f}{u}",
                       _("最好 {0:.2f} · 最差 {1:.2f}").format(subs['min'], subs['max']),
                       True, None))
            if subs.get("thr"):
                sh.append((_("废片候选"), _("{0} 张").format(subs['bad']),
                           _("(阈值 {0:.2f}{u} = 组 RMS × {SUB_BAD_FACTOR:g})").format(
                               subs['thr'], u=u, SUB_BAD_FACTOR=SUB_BAD_FACTOR), False,
                           "bad" if subs["bad"] else "good"))

        self._fill_groups(self.kv, [
            (_GRP_QUALITY, _("导星质量"), q),
            (_GRP_FRAMES, _("帧统计"), fr),
            (_GRP_DRIFT, _("趋势与平衡"), dr),
            (_GRP_OPTICS, _("导星光学"), op),
            (_GRP_CAMERA, _("赤道仪 / 几何"), ge),
            (_GRP_CAL, _("校准"), cl),
            (_GRP_SHOT, _("与拍摄联动"), sh),
        ])

    # ---------- 图表 ----------

    def _size_charts(self, w: float, h: float) -> None:
        """把算出来的单图尺寸落到 8 张小图画布与外层网格的格宽/格高上。

        格宽 = 画布宽 + 边框 + 间距;格高再加上卡片标题行。网格自己按格宽换行,
        所以窗口变宽变窄不需要重画,只有**画布尺寸本身**变了才要(见 _on_body_size)。
        """
        for cv in (self.cv_scatter, self.cv_hist, self.cv_roll, self.cv_pulse,
                   self.cv_pulse_hist, self.cv_snr, self.cv_acf, self.cv_period):
            cv.Width, cv.Height = w, h
        self.wrap.ItemWidth = w + CHART_BORDER + CHART_GAP
        self.wrap.ItemHeight = h + CHART_BORDER + CHART_TITLE_H + CHART_GAP

    def _draw_charts(self, agg: dict) -> None:
        """6 张复用导星页画法的小图 + 4 张本页独有的图。

        尺寸按面板可用宽现算(`chart_layout`),所以同一份数据在宽窗口下是
        3~4 列大图、窄窗口下是 1~2 列小图,**永远不横向溢出**。
        复用的 `_draw_*` 自己会先 `Children.Clear()`,叠加层在其后追加;
        整个面板一次性铺完(没有滑杆/交互重绘,所以不存在闪烁问题)。
        """
        ch = agg["ch"]
        p = self.page
        _cols, w, h = chart_layout(self._wide_width())
        self._size_charts(w, h)
        p._draw_scatter(ch, self.cv_scatter, w, h)
        self._overlay_ellipse(agg, ch, w, h)
        p._draw_hist(ch, self.cv_hist, w, h)
        self._overlay_fit(ch, w, h)
        p._draw_roll(ch, self.cv_roll, w, h)
        self._overlay_roll_marks(agg, ch, w, h)
        p._draw_pulse(ch, self.cv_pulse, w, h)
        self._draw_pulse_hist(agg, w, h)
        p._draw_snr(ch, self.cv_snr, w, h)
        self._overlay_lost(agg, w, h)
        self._draw_acf(agg, w, h)
        p._draw_period(ch, self.cv_period, w, h)
        self._annotate_period(agg, w, h)
        self._draw_seg(agg)
        self._draw_subs(agg)

    def _overlay_ellipse(self, agg: dict, ch: dict, w: float, h: float) -> None:
        """散点上叠 1σ RMS 椭圆(几何与 `_draw_scatter` 的映射保持一致)。"""
        el = agg.get("ellipse")
        if not el or el["a"] <= 0:
            return
        cv = self.cv_scatter
        cx, cy = w / 2.0, h / 2.0
        rng = max(ch["sc_rng"], 1e-9)
        s = (min(w, h) / 2.0 - 8.0) / rng
        ra, rb = el["a"] * s, el["b"] * s
        if ra < 2.0 or ra > min(w, h) / 2.0:
            return
        e = Ellipse()
        e.Width, e.Height = 2.0 * ra, 2.0 * max(rb, 0.5)
        e.Stroke = self._b_ell
        e.StrokeThickness = 1.2
        rot = RotateTransform()
        # 数据系逆时针 θ → 屏幕系(y 向下)顺时针,故取负
        rot.Angle = -el["theta_deg"]
        rot.CenterX, rot.CenterY = ra, max(rb, 0.5)
        e.RenderTransform = rot
        Canvas.SetLeft(e, cx - ra)
        Canvas.SetTop(e, cy - max(rb, 0.5))
        cv.Children.Append(e)
        cap = (_("椭圆(轴比 —)") if math.isinf(el["ratio"])
               else _("椭圆轴比 {0:.2f}").format(el['ratio']))
        self.page._text_on(cv, cap, w - 92.0, 2.0,
                           brush=self._b_ell, opacity=None)

    def _overlay_fit(self, ch: dict, w: float, h: float) -> None:
        """直方图上叠正态拟合曲线(x 取 bin 中心,与柱同一归一化)。"""
        hist = ch.get("hist")
        if not hist:
            return
        cv = self.cv_hist
        m = 6.0
        base, top = h - 18.0, 8.0
        bw = (w - 2 * m) / G.HIST_BINS
        for key, brush in (("fit_ra", self._b_ra), ("fit_dec", self._b_dec)):
            fit = hist.get(key)
            if not fit:
                continue
            pts = [(m + (i + 0.5) * bw, base - min(v, 1.05) * (base - top))
                   for i, v in enumerate(fit)]
            self.page._poly_on(cv, pts, brush, 1.2)
        sd_ra, sd_dec = hist.get("sd_ra"), hist.get("sd_dec")
        if sd_ra is not None and sd_dec is not None:
            self.page._text_on(
                cv, f"σ RA {sd_ra:.2f} · DEC {sd_dec:.2f}{ch['unit']}",
                6.0, 0.0)

    def _overlay_roll_marks(self, agg: dict, ch: dict, w: float, h: float) -> None:
        """滚动 RMS 上叠段边界竖线(跨段之间的连线不是真实数据,必须标出来)。

        `roll_marks` 与**实际画出的段**严格对应(聚合层用 drawn 计数,未画出的段
        不产生边界线),所以段数就是 `len(marks) + 1`;整段不足一窗而没画的段
        另行标注,免得用户以为那段数据丢了。
        """
        roll = ch.get("roll") or []
        marks = agg.get("roll_marks") or []
        short = int(agg.get("roll_short") or 0)
        cv = self.cv_roll
        if len(roll) >= 2:
            m = 6.0
            base, top = h - 18.0, 10.0
            rt0, rt1 = roll[0][0], roll[-1][0]
            span = max(rt1 - rt0, 1e-9)
            for t in marks:
                x = m + (t - rt0) / span * (w - 2 * m)
                if m <= x <= w - m:
                    self.page._line_on(cv, x, top, x, base, self._b_dim, 1.0)
            if marks:
                self.page._text_on(cv, _("{0} 段").format(len(marks) + 1), w - 44.0, 0.0)
        if short:
            # 左下角(_draw_roll 的"峰值"在左上角,不打架)
            self.page._text_on(cv, _("{short} 段不足 {ROLL_FRAMES} 帧窗, 未画").format(
                short=short, ROLL_FRAMES=ROLL_FRAMES),
                               6.0, h - 15.0)

    def _overlay_lost(self, agg: dict, w: float, h: float) -> None:
        """SNR 曲线底部叠丢星帧红刻度(云过境/丢星密集时一眼可见)。"""
        marks = agg.get("lost_marks") or []
        if not marks:
            return
        cv = self.cv_snr
        m = 6.0
        base = h - 18.0
        for t in marks:
            x = m + max(0.0, min(1.0, t)) * (w - 2 * m)
            self.page._line_on(cv, x, base - 7.0, x, base, self._b_lost, 1.2)

    def _annotate_period(self, agg: dict, w: float, h: float) -> None:
        """周期图/自相关只取最长的一段,得说清楚用的是哪一段。

        写在**自相关卡的右上角**:这条注解对自相关与周期图同时成立,而周期图
        的左下角是滞后刻度标签(窄画布上一定撞车,真机截图复现过)。
        自相关卡左上角是"主峰"文字,右上角空着。
        """
        sec = agg.get("longest_sec")
        if sec is None:
            return
        self.page._text_on(self.cv_acf, _("最长段 {begins:%m-%d %H:%M}").format(
            begins=sec.begins),
                           max(6.0, w - 92.0), 0.0)

    def _draw_pulse_hist(self, agg: dict, w: float, h: float) -> None:
        """修正脉冲时长直方图(RA/DEC 叠加半透明;能看出回差与最小脉冲)。"""
        cv = self.cv_pulse_hist
        cv.Children.Clear()
        ph = agg.get("pulse_hist")
        if not ph:
            self.page._text_on(cv, _("本组无修正脉冲"), w / 2 - 42.0, h / 2 - 8.0)
            return
        m = 6.0
        base, top = h - 18.0, 14.0
        bw = (w - 2 * m) / PULSE_BINS
        # 与直方图同款:RA/DEC 半透明叠画,顺序即 z 序;整批一次铺完(见 G.rect_fragment)
        bars = []
        for i in range(PULSE_BINS):
            x = m + i * bw
            for key, brush in (("ra", self._b_ra_dim), ("dec", self._b_dec_dim)):
                v = ph[key][i]
                if v <= 0:
                    continue
                bh = v * (base - top)
                bars.append((x + 0.5, base - bh, max(1.0, bw - 1.0), bh, brush))
        self.page._append_rects(cv, bars)
        self.page._line_on(cv, m, base, w - m, base, self._b_axis)
        self.page._text_on(cv, "RA", 6.0, 0.0, brush=self._b_ra, opacity=None)
        self.page._text_on(cv, "DEC", 30.0, 0.0, brush=self._b_dec, opacity=None)
        med = []
        if ph.get("med_ra") is not None:
            med.append(_("中位 RA {0:.0f}ms").format(ph['med_ra']))
        if ph.get("med_dec") is not None:
            med.append(f"DEC {ph['med_dec']:.0f}ms")
        if med:
            self.page._text_on(cv, " · ".join(med), 6.0, h - 15.0)
        self.page._text_on(cv, f"0~{ph['hi']:.0f}ms", w - 66.0, h - 15.0)

    def _draw_acf(self, agg: dict, w: float, h: float) -> None:
        """RA 误差自相关:x=滞后(线性),标主峰;数据不足时优雅退化。"""
        cv = self.cv_acf
        cv.Children.Clear()
        acf = agg.get("acf")
        if acf is None:
            self.page._text_on(cv, _("数据不足以做自相关分析"),
                               w / 2 - 68.0, h / 2 - 16.0)
            self.page._text_on(cv, _("(需 ≥{ACF_MIN_FRAMES} 帧且时长 ≥3 分钟)").format(
                ACF_MIN_FRAMES=ACF_MIN_FRAMES),
                               w / 2 - 78.0, h / 2 + 2.0)
            return
        m = 6.0
        base, top = h - 18.0, 14.0
        mid = (base + top) / 2.0          # ACF 值域 -1~1,0 在正中
        self.page._line_on(cv, m, mid, w - m, mid, self._b_axis)
        # 滞后刻度(60/120/300/600s,超出量程的不画)。x 是**线性**轴:窄画布上
        # 60s 与 120s 会挤到一起(真机截图上糊成一团),标签太近就只画竖线不画字。
        span = max(acf["max_lag"], 1e-9)
        last_lbl = -1e9
        for tv in (60, 120, 300, 600, 900):
            if tv > span:
                continue
            x = m + tv / span * (w - 2 * m)
            self.page._line_on(cv, x, top, x, base, self._b_grid)
            if x - last_lbl < ACF_LABEL_MIN_GAP:
                continue
            last_lbl = x
            self.page._text_on(cv, f"{tv}s", x - 10.0, h - 15.0)
        pts = [(m + xn * (w - 2 * m), mid - max(-1.0, min(1.0, v)) * (mid - top))
               for xn, v in acf["pts"]]
        self.page._poly_on(cv, pts, self._b_ra)
        if acf.get("peak_lag") is not None:
            xpk = m + (acf["peak_lag"] / span) * (w - 2 * m)
            good = acf.get("significant")
            self.page._line_on(cv, xpk, top, xpk, base,
                               self._b_warn if good else self._b_dim, 1.0)
            txt = (_("主峰 ~{0:.0f}s(r={1:.2f})").format(acf['peak_lag'], acf['peak_val'])
                   if good else _("最强 ~{0:.0f}s · 不显著").format(acf['peak_lag']))
            self.page._text_on(cv, txt, 6.0, 0.0,
                               brush=self._b_warn if good else None,
                               opacity=None if good else 0.7)
        else:
            self.page._text_on(cv, _("无明显周期"), 6.0, 0.0)

    def _draw_seg(self, agg: dict) -> None:
        """分段对比条:一段一行,条长 = 时长,颜色 = 该段 RMS,点击定位主曲线。"""
        cv = self.cv_seg
        cv.Children.Clear()
        self._seg_rows = []
        bars = agg.get("segbars") or []
        w = self._wide_width()
        cv.Width = w
        # 条区宽随面板变 —— 命中反算必须用**这次画的**那个宽度(见 seg_hit_row)
        track = seg_track(w)
        self._seg_track = track
        if not bars:
            cv.Height = 60.0
            self.page._text_on(cv, _("本组没有导星段"), 10.0, 20.0)
            return
        shown = bars[:SEG_MAX_ROWS]
        self._seg_rows = shown          # 命中反算按下标查这份表(与画出的行同序)
        cv.Height = 12.0 + len(shown) * SEG_ROW_H + (18.0 if len(shown) < len(bars) else 6.0)
        x0 = SEG_LABEL_W
        dmax = max((b["dur"] for b in shown), default=1.0) or 1.0
        for k, b in enumerate(shown):
            y = SEG_TOP + k * SEG_ROW_H
            self.page._text_on(
                cv, f"{b['begins']:%m-%d %H:%M:%S}", 6.0, y,
                opacity=0.5 if b["skipped"] else 0.75)
            bw = max(2.0, track * b["dur"] / dmax)
            rect = Rectangle()
            rect.Width, rect.Height = bw, 11.0
            rect.RadiusX = rect.RadiusY = 2.0
            if b["skipped"]:
                rect.Fill = self._b_dim
            else:
                rect.Fill = self._level_brush.get(b["level"], self._b_dim)
            ToolTipService.SetToolTip(
                rect,
                _('{0:%Y-%m-%d %H:%M:%S} — {1:%H:%M:%S}\n时长 {2:.1f} 分钟 · 有效 {3} 帧 · 丢星 {4}\n点击定位到主曲线').format(
                    b['begins'], b['end'], b['dur'] / 60, b['n_frames'], b['n_lost']))
            # 不给单根条挂 Tapped(见 _wire:事件在画布上只挂一次)
            Canvas.SetLeft(rect, x0)
            Canvas.SetTop(rect, y + 2.0)
            cv.Children.Append(rect)
            bits = _("{0:.1f} 分钟").format(b['dur'] / 60)
            if b["rms"] is not None:
                bits += f" · RMS {b['rms']:.2f}{b['unit']}"
            else:
                bits += _(" · 无有效帧")
            if b["skipped"]:
                bits += _(" · 像素口径(未计入)")
            elif not b["main"]:
                bits += _(" · 短尝试")
            self.page._text_on(cv, bits, x0 + track + 8.0, y,
                               opacity=0.5 if b["skipped"] else 0.8)
        if len(shown) < len(bars):
            self.page._text_on(
                cv, _("另有 {0} 段未列出(按时间取前 {SEG_MAX_ROWS} 段)").format(
                    len(bars) - len(shown), SEG_MAX_ROWS=SEG_MAX_ROWS), 6.0, SEG_TOP + len(shown) * SEG_ROW_H + 2.0)

    def _on_seg_tapped(self, sender, e) -> None:
        """分段对比条的唯一 Tapped(画布级,`_wire` 里注册一次)。

        条是 Canvas 的子元素,Tapped 会冒泡到画布;坐标必须在处理器内**同步**取出
        (事件参数不能跨帧持有,与 _space 的画布命中同款)。
        """
        rows = self._seg_rows
        if not rows:
            return
        try:
            p = e.GetPosition(self.cv_seg)
            k = seg_hit_row(float(p.X), float(p.Y), len(rows), self._seg_track)
        except Exception:
            return
        if k is None:
            return
        self._on_seg_click(rows[k])

    def _draw_subs(self, agg: dict) -> None:
        """每张 sub 曝光期间的导星 RMS 柱状图 + 废片候选阈值线。

        拿不到同夜拍摄帧(组未匹配到 Autorun run / 无亮场 / 无覆盖)时整卡隐藏。
        """
        cv = self.cv_sub
        cv.Children.Clear()
        subs = agg.get("subs")
        if not subs or not subs["items"]:
            self.sub_card.Visibility = Visibility.Collapsed
            return
        self.sub_card.Visibility = Visibility.Visible
        w, h = self._wide_width(), SUB_H
        cv.Width, cv.Height = w, h
        items = subs["items"]
        # >SUB_MAX_BARS 时分桶取该桶最差的一张(保住废片尖峰)
        n = len(items)
        if n > SUB_MAX_BARS:
            picked = []
            for k in range(SUB_MAX_BARS):
                lo = k * n // SUB_MAX_BARS
                hi = max(lo + 1, (k + 1) * n // SUB_MAX_BARS)
                picked.append(max(items[lo:hi], key=lambda it: it["rms"]))
            items = picked
        u = agg["unit"]
        m = 8.0
        base, top = h - 22.0, 16.0
        vmax = max(max(it["rms"] for it in items),
                   subs["thr"] or 0.0, 1e-6) * 1.08
        slot = (w - 2 * m) / len(items)
        bw = max(1.0, min(14.0, slot - 1.0))
        # 普通柱整批一次铺完(见 G.rect_fragment);**废片候选**逐个建 —— 只有它们
        # 需要 ToolTip,而 ToolTip 单价约是一个 Rectangle 的两倍(真机实测),
        # 给全部 120 根挂会把打开耗时翻一番,而用户要点开看的本来就只有这几根红柱。
        plain = []
        for k, it in enumerate(items):
            bh = max(1.5, it["rms"] / vmax * (base - top))
            x, y = m + k * slot + (slot - bw) / 2.0, base - bh
            if it["bad"]:
                bar = Rectangle()
                bar.Width, bar.Height = bw, bh
                bar.Fill = self._b_lost
                ToolTipService.SetToolTip(
                    bar, _('#{0} · {1:%m-%d %H:%M:%S} · 曝光 {2:.0f}s\nRMS {3:.2f}{u}({4} 导星帧)· 废片候选').format(
                        it['no'], it['t'], it['exp'], it['rms'], it['n'], u=u))
                Canvas.SetLeft(bar, x)
                Canvas.SetTop(bar, y)
                cv.Children.Append(bar)
                continue
            if u == "″":
                brush = (self._b_good if it["rms"] < G.BAR_GOOD
                         else self._b_warn if it["rms"] < G.BAR_WARN
                         else self._b_lost)
            else:
                brush = self._b_good
            plain.append((x, y, bw, bh, brush))
        self.page._append_rects(cv, plain)
        if subs["thr"]:
            y = base - min(subs["thr"] / vmax, 1.0) * (base - top)
            self.page._line_on(cv, m, y, w - m, y, self._b_lost, 1.0)
            self.page._text_on(cv, _("废片阈值 {0:.2f}{u}").format(subs['thr'], u=u),
                               w - 130.0, y - 14.0, brush=self._b_lost,
                               opacity=None)
        self.page._line_on(cv, m, base, w - m, base, self._b_axis)
        first, last = subs["items"][0]["t"], subs["items"][-1]["t"]
        self.page._text_on(cv, f"{first:%m-%d %H:%M}", m, h - 16.0)
        self.page._text_on(cv, f"{last:%m-%d %H:%M}", w - 74.0, h - 16.0)
        cap = (_("{0}/{1} 张有导星覆盖 · 平均 {2:.2f}{u} · 最差 {3:.2f}{u} · 废片候选 {4} 张").format(
            subs['n_rated'], subs['n_shots'], subs['mean'], subs['max'], subs['bad'], u=u))
        if len(items) < len(subs["items"]):
            cap += _("(柱已降采样到 {0} 根,每根取桶内最差)").format(len(items))
        self.page._text_on(cv, cap, m, 0.0)
