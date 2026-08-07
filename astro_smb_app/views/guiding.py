"""导星分析页的**视图模型**:PHD2 段/校准 → 图表数据(纯 Python 列表)。

老 UI 的注释写得很清楚:"全部在工作线程算好,UI 线程只画" —— 也就是说这一层
本来就是纯的,只是住在页面模块里。抽出来之后新前端与老 UI 画的是同一份数据。

**降采样一律在这里做完。** 一段导星有几万帧,而屏幕上一条折线最多画
``MAX_POINTS`` 个点。老 UI 那边降采样是为了省 WinRT 调用;新前端是为了别把
几万个浮点塞进 JSON —— 目的不同,做法一样,而且都必须在**生产侧**做:
发过界再让前端丢弃,那几万个数字已经序列化过一遍了。

分桶保峰(``_bucket_peak``)不是普通抽稀:每桶取 |值| 最大的那一帧。
导星曲线上最该看见的就是尖峰,均匀抽稀会把它们抹平。
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta

import numpy as np

from astro_smb.phd2log import (
    CalibrationSection,
    GuideSection,
    compute_rms,
    section_rms,
)
from astro_smb.i18n import N_, gettext as _

MAX_POINTS = 1200        # 逐点折线降采样上限(RA/DEC 各自独立)
MAX_LOST_TICKS = 600     # 丢星刻度线上限(过多时抽稀)
# 绘图边距:左侧留刻度文字,底部留丢星刻度 + 时间标签
ML, MR, MT, MB = 46.0, 10.0, 10.0, 22.0

# 时间窗选项(与 guiding.xaml 的 WindowCombo 顺序一致)
# 表里 `N_()` 只标记不翻,取用时才 `_()` —— 模块级求值一次,直接 `_()`
# 会把翻译冻在 import 那一刻的语言上(下面 `_PIER_CN` 同理)
WINDOW_CHOICES: list[tuple[str, float | None]] = [
    (N_("全段"), None), (N_("60 分钟"), 3600.0), (N_("30 分钟"), 1800.0),
    (N_("10 分钟"), 600.0), (N_("5 分钟"), 300.0),
]
ENV_FRAMES_PER_PX = 2.0  # 窗口内帧数 > 2×像素宽 → 包络视图
RMS30_FRAMES = 30        # 包络主线:滑动 RMS 帧窗
ROLL_WIN_S = 60.0        # 滚动 RMS 图:60 秒时间窗
ROLL_SAMPLES = 240       # 滚动 RMS 曲线采样点上限
SCATTER_MAX = 800        # 散点图降采样上限(≤1500,兼顾 XAML 元素开销)
HIST_BINS = 21           # 直方图 bin 数
PERIOD_MIN_S = 30.0      # 周期图候选周期下限(秒)
PERIOD_MAX_S = 1200.0    # 周期图候选周期上限(秒)
PERIOD_MIN_FRAMES = 120  # 周期图最少有效帧(不足显示"数据不足")
PERIOD_MAX_PTS = 300     # 周期图折线点数上限(分桶保峰)
SNR_MAX_PTS = 400        # SNR/星质量曲线降采样上限(均匀抽稀)
DRIFT_DEC_WARN = 0.5     # DEC 漂移预警阈值(″/min,超过提示检查极轴)
BAR_GOOD, BAR_WARN = 0.8, 1.5   # 逐段 RMS 柱颜色阈值(″):绿 / 琥珀 / 红
BAR_M = 6.0              # 逐段 RMS 总览的左右边距(绘制与命中反算共用同一个值)
MIN_RANK_FRAMES = 30     # 汇总"最佳/最差段"参评的最少有效帧(排除 settle 碎段)
CHART_W, CHART_H = 220.0, 150.0   # 统计小图画布尺寸(与 xaml 固定值一致)
# X 轴时间刻度候选步长(秒),取使刻度数 ≤8 的最小值
_TICK_STEPS = (60, 120, 300, 600, 900, 1800, 3600, 7200, 14400, 28800)

# ---- 段列表分组门槛 ----
MAIN_MIN_FRAMES = 100     # 主段判据之一:本段记录帧数(含丢星,与列表显示同源)
MAIN_MIN_DUR = 300.0      # 主段判据之二:时长 ≥5 分钟
FRAG_MIN_CLUSTER = 2      # 至少这么多**连续**碎段才值得折叠成一条摘要
CAL_SPAN_S = 300.0        # 校准段没有结束时刻,按这么长的区间参与重叠判定
CAL_NEAR_S = 1200.0       # 校准落在目标块开始前 20 分钟内,算作属于该目标
OTHER_KEY = "\x00other"   # 「其它」组的键(用不可能与目标键冲突的前缀)

_PIER_CN = {"West": N_("西垂"), "East": N_("东垂")}

# 徽章配色(浅底 + 深字,浅/深主题下均可读)——排版语言照抄浏览页详情卡。
# **单一出处**:仪表盘(_guidedash)反过来 import 本模块,两个右侧视图的
# 胶囊必须完全同色,否则切来切去像两个软件。
BADGE_RGB = {
    "good": ((0xDD, 0xEF, 0xDD), (0x1B, 0x5E, 0x20)),
    "warn": ((0xFB, 0xEA, 0xC5), (0x7A, 0x52, 0x00)),
    "bad": ((0xF8, 0xD7, 0xD7), (0xA3, 0x1D, 0x1D)),
    "info": ((0xD9, 0xE7, 0xF8), (0x0D, 0x47, 0xA1)),
    "neutral": ((0xE6, 0xE6, 0xE6), (0x50, 0x50, 0x50)),
}

# 右侧分析区的两个视图(_set_view 的取值)
VIEW_SEGMENT = "segment"
VIEW_DASH = "dash"


def overview_hit_bar(x: float, n_bars: int, w: float = CHART_W) -> int | None:
    """逐段 RMS 总览:画布 x 坐标 → 柱下标(没命中返回 None)。

    与 `GuidingPage._draw_overview` 的排版共用 `BAR_M` 与同一个槽宽公式。
    之所以抽成纯函数,是因为整块画布只挂**一个** Tapped(见 `_wire`),命中柱
    必须靠几何反算,而几何一旦和绘制走偏就会点错段 —— 单测直接钉死这个函数。
    画布宽由调用方传入(绘制时存进 `self._ov_w`),免得两边各写一份常量。

    命中区是整条**槽**而不是柱本身:柱最窄只有 2px,逐根挂事件时几乎点不中;
    按槽反算等于整列都算命中。y 不参与判定(这张画布上只有这一张图)。
    """
    if n_bars <= 0:
        return None
    span = float(w) - 2 * BAR_M
    if span <= 0 or not (BAR_M <= x <= float(w) - BAR_M):
        return None
    k = int((x - BAR_M) / (span / n_bars))
    return min(k, n_bars - 1)       # 右边界那一点归最后一柱


# ---------------------------------------------------------------- 数据侧预计算
# 以下函数均为纯计算(不碰 XAML),在刷新工作线程里执行。

def _p995(vals: list[float]) -> float:
    """|偏差| 的 P99.5(把个别尖峰截掉,避免量程被压扁)。"""
    if not vals:
        return 0.0
    s = sorted(vals)
    i = min(len(s) - 1, int(round(0.995 * (len(s) - 1))))
    return s[i]


def _bucket_peak(pts: list[tuple[float, float]], cap: int) -> list[tuple[float, float]]:
    """>cap 时分桶,每桶取 |值| 最大的点(保尖峰;曲线/频谱降采样共用)。"""
    n = len(pts)
    if n <= cap:
        return pts
    out: list[tuple[float, float]] = []
    for b in range(cap):
        lo = b * n // cap
        hi = max(lo + 1, (b + 1) * n // cap)
        out.append(max(pts[lo:hi], key=lambda p: abs(p[1])))
    return out


def _downsample(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """>MAX_POINTS 时分桶,每桶取 |值| 最大的那帧(保尖峰)。"""
    return _bucket_peak(pts, MAX_POINTS)


def _sliding_rms(vals: np.ndarray, w: int) -> np.ndarray:
    """逐帧的尾窗滑动 RMS(窗口 w 帧,前缀和 O(n))。"""
    sq = vals * vals
    c = np.concatenate(([0.0], np.cumsum(sq)))
    idx = np.arange(len(vals))
    lo = np.maximum(0, idx - (w - 1))
    return np.sqrt(np.maximum(0.0, (c[idx + 1] - c[lo]) / (idx - lo + 1)))


def _fmt(v: float | None, suffix: str, nd: int = 2) -> str:
    return f"{v:.{nd}f}{suffix}" if v is not None else "—"


def _fmt_hours(seconds: float) -> str:
    if seconds < 3600:
        return _("{0:.0f} 分钟").format(seconds / 60)
    return _("{0:.1f} 小时").format(seconds / 3600)


def _prep_period(npt: np.ndarray, npra: np.ndarray) -> dict | None:
    """RA 周期图数据:不等间隔帧 → 均匀重采样 → rFFT 幅值谱(找蜗杆周期误差)。

    帧不等间隔,先按中位帧间隔线性插值重采样到均匀网格,减均值后 rFFT,
    幅值谱换算到周期轴,只取 30s~1200s 区间(且周期 ≤ 时长/2,保证至少
    2 个完整周期)。返回 log-x 归一化坐标点(0~1)+ 峰值周期 + 刻度位置;
    数据不足(帧数 <120 或时长不足 2 个最短候选周期)返回 None。
    DEC 无周期意义,不做。7000 帧量级 rFFT 毫秒级,工作线程内完成。
    """
    n = len(npt)
    dur = float(npt[-1] - npt[0])
    if n < PERIOD_MIN_FRAMES or dur < 2.0 * PERIOD_MIN_S:
        return None
    dt = float(np.median(np.diff(npt)))
    if dt <= 0:
        return None
    m = int(dur / dt) + 1
    if m < 8 or m > 500_000:            # 防御:异常帧间隔不做谱
        return None
    grid = npt[0] + np.arange(m) * dt
    y = np.interp(grid, npt, npra)
    y = y - y.mean()
    amp = np.abs(np.fft.rfft(y))
    freq = np.fft.rfftfreq(m, dt)
    pmax = min(PERIOD_MAX_S, dur / 2.0)     # 周期上限:至少容 2 个完整周期
    if pmax <= PERIOD_MIN_S:
        return None
    sel = (freq >= 1.0 / pmax) & (freq <= 1.0 / PERIOD_MIN_S)
    if int(sel.sum()) < 4:
        return None
    p = (1.0 / freq[sel])[::-1]             # 周期升序(freq 升序 → 反转)
    a = amp[sel][::-1]
    am = float(a.max())
    if am <= 0:
        return None
    a = a / am
    lg0, lg1 = math.log(PERIOD_MIN_S), math.log(pmax)
    xn = (np.log(p) - lg0) / (lg1 - lg0)    # 对数周期轴归一化 0~1
    k = int(np.argmax(a))
    pts = _bucket_peak(list(zip(xn.tolist(), a.tolist())), PERIOD_MAX_PTS)
    ticks = [((math.log(tv) - lg0) / (lg1 - lg0), f"{tv}s")
             for tv in (60, 120, 300, 600) if PERIOD_MIN_S <= tv <= pmax]
    return {"pts": pts, "peak_p": float(p[k]), "peak_x": float(xn[k]),
            "ticks": ticks}


def _prep_charts(sec: GuideSection, npt: np.ndarray, npra: np.ndarray,
                 npdec: np.ndarray, rms, unit: str) -> dict:
    """统计图表数据(散点/直方图/滚动 RMS/脉冲/漂移/SNR/周期图),
    全部在工作线程算好,UI 线程只画。"""
    n = len(npt)
    rms_total = float(rms.rms_total) if rms is not None and rms.n_frames > 0 else 0.0
    absmax = float(np.percentile(np.abs(np.concatenate((npra, npdec))), 99.5))

    # a. RA/DEC 散点(降采样均匀抽稀)
    if n > SCATTER_MAX:
        si = np.linspace(0, n - 1, SCATTER_MAX).astype(np.int64)
    else:
        si = np.arange(n)
    sc_pts = list(zip(npra[si].tolist(), npdec[si].tolist()))
    sc_rng = max(2.6 * rms_total, 1e-6) if rms_total > 0 else max(absmax, 1e-6)

    # b. 偏差直方图(范围 ±3×RMS)
    hr = 3.0 * rms_total if rms_total > 0 else absmax
    hist = None
    if hr > 0:
        # 丢弃变量不用 `_`:那个名字归 gettext(见 guidedash 里同一处的说明)
        h_ra, _edges = np.histogram(npra, bins=HIST_BINS, range=(-hr, hr))
        h_dec, _edges = np.histogram(npdec, bins=HIST_BINS, range=(-hr, hr))
        hmax = float(max(int(h_ra.max()), int(h_dec.max()), 1))
        hist = {"ra": (h_ra / hmax).tolist(), "dec": (h_dec / hmax).tolist(),
                "rng": hr}

    # c. 滚动总 RMS(60 秒中心窗,前缀和 + searchsorted)
    roll: list[tuple[float, float]] = []
    roll_max = 0.0
    if n >= 2:
        ri = np.unique(np.linspace(0, n - 1, min(n, ROLL_SAMPLES)).astype(np.int64))
        sq = npra * npra + npdec * npdec
        c = np.concatenate(([0.0], np.cumsum(sq)))
        lo = np.searchsorted(npt, npt[ri] - ROLL_WIN_S / 2, "left")
        hi = np.searchsorted(npt, npt[ri] + ROLL_WIN_S / 2, "right")
        vals = np.sqrt(np.maximum(0.0, (c[hi] - c[lo]) / np.maximum(1, hi - lo)))
        roll = list(zip(npt[ri].tolist(), vals.tolist()))
        roll_max = float(vals.max())

    # d. 修正脉冲统计(RA E/W、DEC N/S:次数 + 累计 ms)
    cnt = {"E": 0, "W": 0, "N": 0, "S": 0}
    tot = {"E": 0, "W": 0, "N": 0, "S": 0}
    for f in sec.frames:
        if f.ra_dur > 0 and f.ra_dir in ("E", "W"):
            cnt[f.ra_dir] += 1
            tot[f.ra_dir] += f.ra_dur
        if f.dec_dur > 0 and f.dec_dir in ("N", "S"):
            cnt[f.dec_dir] += 1
            tot[f.dec_dir] += f.dec_dur
    pulse = [("RA E", cnt["E"], tot["E"], "ra"), ("RA W", cnt["W"], tot["W"], "ra"),
             ("DEC N", cnt["N"], tot["N"], "dec"), ("DEC S", cnt["S"], tot["S"], "dec")]

    # e. 漂移速率(RA/DEC 原始偏差一阶线性拟合,斜率换算到 单位/分钟)
    drift = None
    tspan = float(npt[-1] - npt[0])
    if tspan > 1e-9:
        drift = {"ra": float(np.polyfit(npt, npra, 1)[0]) * 60.0,
                 "dec": float(np.polyfit(npt, npdec, 1)[0]) * 60.0}

    # f. SNR / 星质量曲线(视宁/透明度代理):双 Y 各自归一到 max,均匀抽稀
    snr_chart = None
    valid = [f for f in sec.frames if not f.lost]   # 与 npt 同序同长(同一过滤)
    if len(valid) == n and tspan > 1e-9:
        snr_a = np.asarray([f.snr for f in valid], dtype=np.float64)
        mass_a = np.asarray([f.star_mass for f in valid], dtype=np.float64)
        si2 = np.unique(np.linspace(0, n - 1, min(n, SNR_MAX_PTS)).astype(np.int64))
        tn = ((npt - npt[0]) / tspan)[si2]          # 时间轴归一化 0~1(全段)
        smax, mmax = float(snr_a.max()), float(mass_a.max())
        snr_chart = {
            "snr": list(zip(tn.tolist(), (snr_a[si2] / smax).tolist()))
                   if smax > 0 else [],
            "mass": list(zip(tn.tolist(), (mass_a[si2] / mmax).tolist()))
                    if mmax > 0 else [],
            "mean": float(snr_a.mean()), "std": float(snr_a.std()),
        }

    # g. RA 周期图(重采样 + rFFT,数据不足为 None)
    period = _prep_period(npt, npra)

    return {"unit": unit, "rms_total": rms_total, "sc_pts": sc_pts, "sc_rng": sc_rng,
            "hist": hist, "roll": roll, "roll_max": roll_max, "pulse": pulse,
            "drift": drift, "snr": snr_chart, "period": period}


def _prep_guide(sec: GuideSection) -> dict:
    """导星段 → 显示行 + 完整帧数组 + 图表数据(全部在工作线程算好)。"""
    scale = sec.pixel_scale if sec.pixel_scale is not None else 1.0
    unit = "″" if sec.pixel_scale is not None else "px"
    valid = [f for f in sec.frames if not f.lost]
    lost_ts = [f.time_s for f in sec.frames if f.lost]
    rms = section_rms(sec)

    npt = npra = npdec = rms30_ra = rms30_dec = None
    charts = None
    peak = 0.0
    if len(valid) >= 2:
        npt = np.asarray([f.time_s for f in valid], dtype=np.float64)
        npra = np.asarray([f.ra_raw for f in valid], dtype=np.float64) * scale
        npdec = np.asarray([f.dec_raw for f in valid], dtype=np.float64) * scale
        peak = float(np.percentile(np.abs(np.concatenate((npra, npdec))), 99.5))
        rms30_ra = _sliding_rms(npra, RMS30_FRAMES)
        rms30_dec = _sliding_rms(npdec, RMS30_FRAMES)
        charts = _prep_charts(sec, npt, npra, npdec, rms, unit)
    elif valid:
        f0 = valid[0]
        peak = max(abs(f0.ra_raw * scale), abs(f0.dec_raw * scale))
    rng = max(1.0, 1.2 * peak)

    dur = sec.duration_s
    main = _("{begins:%m-%d %H:%M:%S} · {0:.1f} 分钟 · {1} 帧").format(
        dur / 60, len(sec.frames), begins=sec.begins)
    level = None
    if rms is None or rms.n_frames == 0:
        n_lost = rms.n_lost if rms is not None else 0
        sub = _("无有效帧") + (_("(丢星 {n_lost})").format(n_lost=n_lost) if n_lost else "")
        stats = _("无有效帧 — 本段没有可统计的导星数据")
        # **键要恒定存在。** 有时有有时没有的话,页面那边 `.get()` 不报错,
        # 只是那一块静默消失 —— 这个仓库为这类事故立过一条专门的门禁。
        stat_rows = [(_("状态"), _("无有效帧"), "bad"),
                     (_("丢星"), str(n_lost), "bad" if n_lost else None)]
        rms_chip = ""       # 同理:键恒定存在,值可以是空
    else:
        u = "″" if rms.in_arcsec else "px"
        rms_chip = f"{rms.rms_total:.2f}{u}"
        level = _rms_level(rms.rms_total, u)
        # 行首「●」是 RMS 语义色圆点(BMP 字符;emoji 会让 HSTRING 末尾少字,§7.1)
        sub = (_("● RMS {rms_total:.2f}{u} (RA {rms_ra:.2f} / DEC {rms_dec:.2f}) · 丢星 {n_lost}").format(
            rms_total=rms.rms_total, u=u, rms_ra=rms.rms_ra, rms_dec=rms.rms_dec, n_lost=rms.n_lost))
        parts = [
            f"RMS RA {rms.rms_ra:.2f}{u}",
            f"DEC {rms.rms_dec:.2f}{u}",
            f"Total {rms.rms_total:.2f}{u}",
            _("峰值 RA {peak_ra:.1f}{u}/DEC {peak_dec:.1f}{u}").format(
                peak_ra=rms.peak_ra, u=u, peak_dec=rms.peak_dec),
            _("{0} 帧(丢星 {n_lost})").format(len(sec.frames), n_lost=rms.n_lost),
        ]
        if sec.exposure_ms is not None:
            parts.append(_("曝光 {exposure_ms}ms").format(exposure_ms=sec.exposure_ms))
        if sec.pixel_scale is not None:
            parts.append(_("比例 {pixel_scale:.2f}″/px").format(pixel_scale=sec.pixel_scale))
        if sec.dec_deg is not None:
            parts.append(_("赤纬 {dec_deg:.1f}°").format(dec_deg=sec.dec_deg))
        if sec.pier_side:
            parts.append(_PIER_CN.get(sec.pier_side, sec.pier_side))
        stats = " · ".join(parts)
        # **同一份数据的结构化版本。** 拼成一行 `·` 串在老 UI 那种宽卡片里
        # 还能看,换到窄一点的右栏就是一堵墙 —— 而这几个数是要**逐个对比**的
        # (这一段 RA 大还是 DEC 大?峰值和均值差多少?)。
        # 两份都由这里产出,页面按自己的宽度挑一份用,不许自己去 split 那个串。
        stat_rows = [
            (_("RMS 合计"), f"{rms.rms_total:.2f}{u}", level),
            ("RMS RA", f"{rms.rms_ra:.2f}{u}", None),
            ("RMS DEC", f"{rms.rms_dec:.2f}{u}", None),
            (_("峰值 RA"), f"{rms.peak_ra:.1f}{u}", None),
            (_("峰值 DEC"), f"{rms.peak_dec:.1f}{u}", None),
            (_("帧数"), _("{0}(丢星 {n_lost})").format(len(sec.frames), n_lost=rms.n_lost),
             "bad" if rms.n_lost else None),
        ]
        if sec.exposure_ms is not None:
            stat_rows.append((_("导星曝光"), f"{sec.exposure_ms} ms", None))
        if sec.pixel_scale is not None:
            stat_rows.append((_("像元比例"), f"{sec.pixel_scale:.2f}″/px", None))
        if sec.dec_deg is not None:
            stat_rows.append((_("赤纬"), f"{sec.dec_deg:.1f}°", None))
        if sec.pier_side:
            stat_rows.append(("Pier side",
                              _PIER_CN.get(sec.pier_side, sec.pier_side), None))

    end = sec.end_time_effective
    title = (_("导星段 {begins:%Y-%m-%d %H:%M:%S} — {end:%H:%M:%S} · {0:.1f} 分钟").format(
        dur / 60, begins=sec.begins, end=end))
    # 主段 / 碎段:碎段(settle 抖动、重选星的短尝试)在列表里会被折叠成一条摘要
    main_seg = len(sec.frames) >= MAIN_MIN_FRAMES or dur >= MAIN_MIN_DUR
    return {
        "kind": "guide", "begins": sec.begins, "end": end,
        "duration": dur, "main": main, "sub": sub, "stats": stats,
        "stat_rows": stat_rows,
        # 卡片右上角胶囊要的那个数**单独给一个字段**。页面原来是在 stat_rows
        # 里按标签 `== "RMS 合计"` 找 —— 那个标签是显示文本,会被翻译,
        # 一翻胶囊就永远空着(不报错,只是少一块)。
        # **两支都要给值**:第一版只在有效帧那一支算,无有效帧的段直接
        # UnboundLocalError —— 键恒定存在这条规矩对它同样适用。
        "rms_chip": rms_chip,
        "title": title, "unit": unit, "rng": rng,
        "lost": lost_ts, "rms": rms, "level": level, "main_seg": main_seg,
        # 原始段对象:仪表盘(_guidedash)做组内聚合时要逐帧重算,行里只有
        # 降采样后的展示数据不够用。纯数据对象,跨线程传递安全。
        "sec": sec,
        # 完整帧数组(numpy,窗口切片用)+ 30 帧滑动 RMS(包络主线)
        "npt": npt, "npra": npra, "npdec": npdec,
        "rms30ra": rms30_ra, "rms30dec": rms30_dec,
        "charts": charts,
    }


def _prep_cal(cal: CalibrationSection) -> dict:
    """校准段 → 时间线低调行 + 右侧信息文本(选中时只显示文字,不画曲线)。"""
    if cal.complete:
        bit = ""
        if cal.west_angle is not None or cal.west_rate is not None:
            bit = f"West {_fmt(cal.west_angle, '°', 1)}/{_fmt(cal.west_rate, 'px/s')}"
        elif cal.north_angle is not None or cal.north_rate is not None:
            bit = f"North {_fmt(cal.north_angle, '°', 1)}/{_fmt(cal.north_rate, 'px/s')}"
        result = _("成功") + (f" {bit}" if bit else "")
    else:
        result = _("失败(星丢失×{star_lost})").format(star_lost=cal.star_lost)
    main = _("{begins:%m-%d %H:%M:%S} · 校准 · {result}").format(
        begins=cal.begins, result=result)

    lines = [_("开始时间:{begins:%Y-%m-%d %H:%M:%S}").format(begins=cal.begins),
             _("结果:成功") if cal.complete else _("结果:失败(星丢失 ×{star_lost})").format(
                 star_lost=cal.star_lost)]
    if cal.west_angle is not None or cal.west_rate is not None:
        lines.append(_("West:角度 {0} · 速率 {1}").format(
            _fmt(cal.west_angle, '°', 1), _fmt(cal.west_rate, ' px/s')))
    if cal.north_angle is not None or cal.north_rate is not None:
        lines.append(_("North:角度 {0} · 速率 {1}").format(
            _fmt(cal.north_angle, '°', 1), _fmt(cal.north_rate, ' px/s')))
    if cal.mount:
        lines.append(_("赤道仪:{mount}").format(mount=cal.mount))
    if cal.pixel_scale is not None:
        lines.append(_("像元比例:{pixel_scale:.2f}″/px").format(pixel_scale=cal.pixel_scale))
    if cal.steps:
        lines.append(_("校准步数:{0}").format(len(cal.steps)))
    title = _("校准 {begins:%Y-%m-%d %H:%M:%S} · ").format(
        begins=cal.begins) + (_("成功") if cal.complete else _("失败"))
    # end/duration/level 补齐成与导星行同构,分组代码不必到处判 kind
    return {
        "kind": "cal", "begins": cal.begins, "end": cal.begins, "duration": 0.0,
        "main": main, "sub": None, "level": None, "main_seg": True,
        "cal_fail": not cal.complete, "cal": cal,
        "title": title, "cal_text": "\n".join(lines),
    }


def _summary_text(rows: list[dict], total_dur: float, all_pairs: list,
                  s_ok: int, s_fail: int, n_cal: int, n_cal_fail: int) -> str | None:
    """全部导星段合并的汇总文本(工作线程执行)。无段返回 None。"""
    overall = compute_rms(all_pairs)
    if overall is None:
        return None
    u = "″" if overall.in_arcsec else "px"
    lines = [_("汇总(全部导星段):时长 {0} · 有效 {n_frames} 帧 · 丢星 {n_lost}").format(
        _fmt_hours(total_dur), n_frames=overall.n_frames, n_lost=overall.n_lost)]
    if overall.n_frames > 0:
        lines.append(_("整体 RMS {rms_total:.2f}{u}(RA {rms_ra:.2f} / DEC {rms_dec:.2f})").format(
            rms_total=overall.rms_total, u=u, rms_ra=overall.rms_ra, rms_dec=overall.rms_dec))
    # 最佳/最差段(单位一致的段之间比较;优先只看 ≥30 有效帧的段,
    # 避免几帧的 settle 碎段以离谱 RMS 霸榜"最差")
    pool = [r for r in rows
            if r["kind"] == "guide" and r.get("rms") is not None
            and r["rms"].n_frames > 0
            and r["rms"].in_arcsec == overall.in_arcsec]
    solid = [r for r in pool if r["rms"].n_frames >= MIN_RANK_FRAMES]
    cand = [(r["rms"].rms_total, r["begins"]) for r in (solid or pool)]
    if cand:
        bv, bt = min(cand)
        s = _("最佳段 {bt:%m-%d %H:%M}({bv:.2f}{u})").format(bt=bt, bv=bv, u=u)
        if len(cand) > 1:
            wv, wt = max(cand)
            s += _(" · 最差段 {wt:%m-%d %H:%M}({wv:.2f}{u})").format(wt=wt, wv=wv, u=u)
        lines.append(s)
    bits = []
    n_settle = s_ok + s_fail
    if n_settle:
        bits.append(_("Settle 成功 {s_ok}/{n_settle}({0:.0f}%)").format(
            100.0 * s_ok / n_settle, s_ok=s_ok, n_settle=n_settle))
    else:
        bits.append(_("Settle 无记录"))
    if n_cal:
        bits.append(_("校准 {n_cal} 次").format(n_cal=n_cal)
                    + (_("({n_cal_fail} 失败)").format(
                        n_cal_fail=n_cal_fail) if n_cal_fail else _("(全部成功)")))
    lines.append(" · ".join(bits))
    return "\n".join(lines)


# ---------------------------------------------------------------- 段列表分组
# 以下全部是纯计算(不碰 XAML),随 _prepare 一起在刷新工作线程里执行。

def _rms_level(value: float | None, unit: str) -> str | None:
    """RMS → 语义级别(好/警告/差)。px 口径没有质量阈值语义,返回 None。"""
    if value is None or unit != "″":
        return None
    return "good" if value < BAR_GOOD else ("warn" if value < BAR_WARN else "bad")


def _merge_rms(stats: list) -> tuple[float | None, str, int, int]:
    """把若干段的 RmsStats 合并成一个 → (RMS, 单位, 有效帧数, 丢星数)。

    **按帧数平方加权**(合并 RMS 的正确口径,与 _records 的整夜汇总一致);
    角秒与像素不可混算,优先角秒口径,全无角秒时才退回像素。
    """
    sq_a = sq_p = 0.0
    n_a = n_p = lost = 0
    for rms in stats:
        if rms is None:
            continue
        lost += rms.n_lost
        if rms.n_frames <= 0:
            continue
        if rms.in_arcsec:
            sq_a += rms.rms_total ** 2 * rms.n_frames
            n_a += rms.n_frames
        else:
            sq_p += rms.rms_total ** 2 * rms.n_frames
            n_p += rms.n_frames
    if n_a:
        return math.sqrt(sq_a / n_a), "″", n_a, lost
    if n_p:
        return math.sqrt(sq_p / n_p), "px", n_p, lost
    return None, "", 0, lost


def _target_blocks(data) -> list[tuple[datetime, datetime, str, str]]:
    """Autorun 目标块 → [(起, 止, 分组键, 目标名)]。

    用**块级**区间而不是 TargetRun 整段:Pause/恢复分裂出的 run,整段区间会
    横跨其他目标的块,整段匹配会把别人的导星段抢过来(logstore._run_covering
    同款教训)。块缺 end_time(会话截断)时用末帧结束时刻兜底。
    """
    out: list[tuple[datetime, datetime, str, str]] = []
    for night in getattr(data, "nights", []) or []:
        for run in night.runs:
            key = f"{night.date}|{run.plan_no}|{run.target}"
            for b in run.blocks:
                end = b.end_time
                if end is None:
                    frames = b.all_frames()
                    end = frames[-1].end_time if frames else b.begin_time
                out.append((b.begin_time, max(end, b.begin_time), key, run.target))
    return out


def _target_runs(data) -> dict:
    """分组键 → Autorun `TargetRun`(与 `_target_blocks` 的键构造保持一致)。

    仪表盘的「每张 sub 期间导星 RMS」需要该目标的实拍帧;组键唯一对应一个 run。
    """
    out: dict = {}
    for night in getattr(data, "nights", []) or []:
        for run in night.runs:
            out[f"{night.date}|{run.plan_no}|{run.target}"] = run
    return out


def _overlap_s(a0: datetime, a1: datetime, b0: datetime, b1: datetime) -> float:
    lo, hi = max(a0, b0), min(a1, b1)
    return max(0.0, (hi - lo).total_seconds())


def _assign_guide(t0: datetime, t1: datetime, blocks: list) -> str | None:
    """导星段归组:重叠最长的目标块胜出;毫无重叠返回 None(进「其它」)。"""
    best_key, best = None, 0.0
    for b0, b1, key, _name in blocks:
        ov = _overlap_s(t0, t1, b0, b1)
        if ov > best:
            best, best_key = ov, key
    return best_key


def _assign_cal(ts: datetime, blocks: list) -> str | None:
    """校准段归组:先按 [begins, +5min] 求重叠;不中再找紧随其后开始的目标块
    (校准通常发生在目标块开始前的准备阶段)。"""
    key = _assign_guide(ts, ts + timedelta(seconds=CAL_SPAN_S), blocks)
    if key is not None:
        return key
    best_key, best_gap = None, None
    for b0, _b1, k, _name in blocks:
        gap = (b0 - ts).total_seconds()
        if 0.0 <= gap <= CAL_NEAR_S and (best_gap is None or gap < best_gap):
            best_gap, best_key = gap, k
    return best_key


def _make_group(key: str, title: str, ris: list[int], rows: list[dict],
                loc: dict, run=None) -> dict:
    """一组的显示数据 + 组内条目(连续碎段折叠成摘要)。ris 已按 rows 顺序(倒序)。

    `run` 是该组对应的 Autorun `TargetRun`(无匹配时 None),仪表盘用它把
    每张 sub 的曝光区间与导星帧求交(纯数据对象,跨线程传递安全)。
    """
    guides = [rows[i] for i in ris if rows[i]["kind"] == "guide"]
    cals = [rows[i] for i in ris if rows[i]["kind"] == "cal"]
    t0 = min(rows[i]["begins"] for i in ris)
    t1 = max([r["end"] for r in guides] + [r["begins"] for r in cals] + [t0])
    dur = sum(r["duration"] for r in guides)
    rms_v, unit, _n_frames, lost = _merge_rms([r.get("rms") for r in guides])

    items: list[dict] = []
    buf: list[int] = []

    def flush() -> None:
        """把攒着的连续碎段收口:够多就折叠成摘要,不够就原样逐条展开。"""
        if not buf:
            return
        if len(buf) < FRAG_MIN_CLUSTER:
            for i in buf:
                items.append({"type": "row", "ri": i})
                loc[i] = (key, None)
            buf.clear()
            return
        frs = [rows[i] for i in buf]
        fdur = sum(r["duration"] for r in frs)
        fv, funit, _fn, _flost = _merge_rms([r.get("rms") for r in frs])
        text = _("{0} 段短尝试 · 共 {1:.1f} 分钟").format(len(buf), fdur / 60)
        text += (_(" · 平均 RMS {fv:.2f}{funit}").format(
            fv=fv, funit=funit) if fv is not None else _(" · 无有效帧"))
        fkey = f"{key}#f{buf[0]}"
        items.append({"type": "frag", "key": fkey, "ris": list(buf),
                      "text": text, "level": _rms_level(fv, funit)})
        for i in buf:
            loc[i] = (key, fkey)
        buf.clear()

    for i in ris:
        r = rows[i]
        if r["kind"] == "guide" and not r.get("main_seg"):
            buf.append(i)       # 碎段先攒着,遇到主段/校准段或收尾时再决定
            continue
        flush()
        items.append({"type": "row", "ri": i})
        loc[i] = (key, None)
    flush()

    bits = [f"{t0:%m-%d %H:%M} — {t1:%H:%M}"]
    if dur > 0:
        bits.append(_("导星 {0}").format(_fmt_hours(dur)))
    if lost:
        bits.append(_("丢星 {lost} 帧").format(lost=lost))
    if cals:
        n_fail = sum(1 for r in cals if r.get("cal_fail"))
        bits.append(_("校准 {0} 次").format(len(cals))
                    + (_("({n_fail} 失败)").format(n_fail=n_fail) if n_fail else ""))
    return {"key": key, "title": title, "sub": " · ".join(bits),
            "n_sec": len(guides), "rms": rms_v, "unit": unit,
            "level": _rms_level(rms_v, unit), "items": items,
            # 仪表盘用:本组全部数据行索引(段行 + 校准行)与对应的拍摄 run
            "ris": list(ris), "run": run,
            "t0": t0, "t1": t1, "dur": dur}


def _build_groups(rows: list[dict], data) -> tuple[list[dict], dict]:
    """rows(倒序)→ (分组列表, 数据行索引 → (组键, 碎段簇键|None))。"""
    blocks = _target_blocks(data)
    names = {k: n for _a, _b, k, n in blocks}
    runs = _target_runs(data)
    first: dict[str, datetime] = {}
    for b0, _b1, k, _n in blocks:
        if k not in first or b0 < first[k]:
            first[k] = b0

    buckets: dict[str, list[int]] = {}
    for i, r in enumerate(rows):
        if r["kind"] == "guide":
            key = _assign_guide(r["begins"], r["end"], blocks)
        else:
            key = _assign_cal(r["begins"], blocks)
        buckets.setdefault(key or OTHER_KEY, []).append(i)

    keys = [k for k in buckets if k != OTHER_KEY]
    keys.sort(key=lambda k: first.get(k, datetime.min), reverse=True)  # 最新在前
    if OTHER_KEY in buckets:
        keys.append(OTHER_KEY)      # 「其它」永远垫底
    loc: dict[int, tuple] = {}
    groups = [_make_group(k, names.get(k, _("其它(未匹配到拍摄目标)")),
                          buckets[k], rows, loc, run=runs.get(k)) for k in keys]
    return groups, loc


def _prepare(data) -> dict:
    """LogData → {rows(按开始时间倒序), groups, loc, status, summary, overview}。
    纯计算,工作线程执行。"""
    rows: list[dict] = []
    n_sec = n_cal = n_fail = 0
    total_dur = 0.0
    settle_ok = settle_fail = 0
    all_pairs: list = []
    for log in data.phd2_logs:
        for sec in log.guide_sections:
            rows.append(_prep_guide(sec))
            n_sec += 1
            total_dur += sec.duration_s
            all_pairs.extend((f, sec.pixel_scale) for f in sec.frames)
            for ev in sec.settles:
                if ev.kind == "complete":
                    settle_ok += 1
                elif ev.kind == "failed":
                    settle_fail += 1
        for cal in log.calibrations:
            rows.append(_prep_cal(cal))
            n_cal += 1
            if not cal.complete:
                n_fail += 1
    summary = _summary_text(rows, total_dur, all_pairs,
                            settle_ok, settle_fail, n_cal, n_fail)
    rows.sort(key=lambda r: r["begins"], reverse=True)   # 最新在前
    # 逐段 RMS 柱状总览(整夜视角,不随选中段变):柱按时间升序,记录排序后
    # 的行索引供点击跳转选中。px 口径段与角秒段混在时只画角秒段(不可比)。
    gi = [(i, r) for i, r in enumerate(rows)
          if r["kind"] == "guide" and r.get("rms") is not None
          and r["rms"].n_frames > 0]
    any_as = any(r["rms"].in_arcsec for _, r in gi)
    pool = [(i, r) for i, r in gi if r["rms"].in_arcsec] if any_as else gi
    pool.sort(key=lambda t: t[1]["begins"])
    overview = None
    if pool:
        overview = {"bars": [(i, float(r["rms"].rms_total)) for i, r in pool],
                    "unit": "″" if any_as else "px",
                    "mixed": any_as and len(pool) < len(gi)}
    # 分组结构(按拍摄目标)——同样在工作线程算好,UI 线程只负责摆控件
    groups, loc = _build_groups(rows, data)
    status = (_("{0} 个导星日志 · {n_sec} 个导星段 · {n_cal} 次校准").format(len(data.phd2_logs), n_sec=n_sec, n_cal=n_cal) + (_("({n_fail} 失败)").format(
        n_fail=n_fail) if n_fail else ""))
    if data.errors:
        status += _(" · {0} 个文件读取失败").format(len(data.errors))
    return {"rows": rows, "groups": groups, "loc": loc,
            "status": status, "summary": summary, "overview": overview}


# ---------------------------------------------------------------- 页面
