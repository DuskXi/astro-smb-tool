"""导星仪表盘:**一组段合起来**看是什么样。

导星页左边那列是按段列的,而一个目标常常被拆成十几段(settle、换目标、
中途停一下)。逐段看得到"这一段抖了",看不到"这一晚这个目标整体如何" ——
仪表盘回答的就是后者:RMS 椭圆(是各向同性抖动还是单轴在跑)、
周期误差主峰与自相关(蜗杆周期)、脉冲方向配比(平衡还是极轴)、
逐段对比、以及**每张 sub 期间的导星 RMS**(把导星数据和拍摄结果对上)。

这一份是从老 UI 的 `astro_smb_gui/_guidedash.py` **抽出来的纯计算部分**。
老 UI 是冻结的,所以是**复制**而不是搬走;判读阈值仍然复用
`astro_smb_app.views.guiding` 的那几个(`_rms_level` / `_bucket_peak` /
`_prep_period` / `_sliding_rms` …),没有第二份阈值。

全部是纯函数,**必须在工作线程调用**(几万帧的 numpy 运算),
返回纯数据,画图的事情交给各前端。
"""
from __future__ import annotations

import math
from datetime import datetime

import numpy as np

from astro_smb.phd2log import compute_rms
from astro_smb.i18n import gettext as _
from astro_smb_app.views import guiding as G

CHART_GAP = 10.0        # 图表卡之间的间距(= 网格每格右侧留白)
CHART_BORDER = 2.0      # 卡片 Border 左右各 1px
CHART_TITLE_H = 18.0    # 卡片标题行(11px 字 + 2px 间距)
CHART_MIN_W = 250.0     # 单张小图最小宽:再窄坐标轴文字就开始互相压
CHART_MAX_W = 340.0     # 单张小图最大宽:超宽窗口下别把图拉成横条
CHART_MAX_COLS = 4      # 每行最多几列(再多单图信息密度反而下降)
CHART_ASPECT = 0.63     # 小图 高/宽
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
SCATTER_MAX = 400       # 散点上限(>此值均匀抽稀)。300x190 画布上 2px 点,400 已接近
ROLL_FRAMES = 30        # 滚动 RMS 帧窗(需求明确:30 帧)
ROLL_PTS = 480          # 滚动 RMS 曲线总采样点预算(按段帧数分配)
SNR_PTS = 400           # SNR/星质量曲线采样点预算
LOST_TICKS = 80         # 丢星刻度上限。绘图区只有 288px 宽,画 300 根竖线就是一块
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

SUB_BAD_FACTOR = 1.5    # 废片候选阈值 = 组 RMS × 该系数
LOST_GOOD, LOST_WARN = 2.0, 8.0   # 丢星率语义阈值(%)


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
        from astro_smb_app.preview import read_fits_header
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
        # **别拿 `_` 当丢弃变量。** 函数里只要有一处 `_ = …`,`_` 在整个
        # 函数里就是局部名,前面那几行的 `_("…")` 会 UnboundLocalError ——
        # 而它只在走到这条路径的那一组上炸(真机 11 组里就第 10 组)。
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
        bits.append(_(G._PIER_CN.get(m["pier_side"], m["pier_side"])))
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
        out.append(_("每张 sub 导星 RMS: {0}/{1} 张有覆盖 · 平均 {2:.2f}{u}"
                    " · 最差 {3:.2f}{u} · 废片候选 {4} 张").format(
            subs['n_rated'], subs['n_shots'], subs['mean'], subs['max'],
            subs['bad'], u=u))
    for n in agg.get("notes") or []:
        out.append(_("注: {n}").format(n=n))
    return "\n".join(out)


# ============================================================ 视图层


def summary_model(agg: dict) -> dict:
    """聚合结果 → **摆得出来的结构**:徽章 / 标题 / 副标题 / 胶囊 / 分区键值。

    这一段原来长在老 UI 的 `_summary` 里,和 WinUI 的控件调用缠在一起。
    抽出来的是**数据规则**(哪些进徽章、什么阈值算警告、分几组),
    控件怎么摆由各前端自己决定 —— 两边各写一份规则的话,同一个数字
    在两套界面上会显示成两种好坏。

    键值元组是 ``(标签, 值, 副注, 等宽?, 语义色)``。
    """
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

    title = agg["title"] or _("(未匹配到拍摄目标)")
    t0, t1 = agg.get("t0"), agg.get("t1")
    sub = []
    if t0 and t1:
        sub.append(f"{t0:%Y-%m-%d %H:%M} — {t1:%H:%M}")
    sub.append(_("导星 {0}").format(G._fmt_hours(agg['dur_total'])))
    sub.append(_("有效 {0} 帧").format(agg['n_frames']))
    sub_text = " · ".join(sub)

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
                   _(G._PIER_CN.get(m["pier_side"], m["pier_side"])),
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

    groups = [
        ("", _("导星质量"), q),
        ("", _("帧统计"), fr),
        ("", _("趋势与平衡"), dr),
        ("", _("导星光学"), op),
        ("", _("赤道仪 / 几何"), ge),
        ("", _("校准"), cl),
        ("", _("与拍摄联动"), sh),
    ]
    return {"badges": badges, "title": title, "sub": sub_text,
            "pills": pills, "groups": groups}
