"""拍摄记录页的**视图模型**:Autorun/PHD2 日志 → 夜次、目标、统计卡、时间线。

老 UI 里这一层就有约 45 个纯函数、1000 多行,全部在工作线程里把 dataclass 树
算成普通 dict —— 页面类只负责把 dict 摆成控件。抽出来之后新前端消费同一份,
"第 N 张"、"已完成/已暂停/被截断"、"间隔 6 分钟"这些措辞两边永远一致。

几条从老 UI 原样继承、**改动前务必先读**的判读约定:

- **夜次按正午分界**,晨间平场归前一夜(见 `astro_smb.autorunlog.aggregate_nights`)。
- **裸 `Exposure 2.0s`(无 image#)是 AutoCenter 曝光,不算帧。**
- 甘特条的命中测试是**纯几何反算**(`timeline_hit_bar`),不挂逐条事件 ——
  老 UI 是被 win32more 的事件泄漏逼的,但这个分法本身就该保留。
- 天球图:北上**东左**,r = R·(90-alt)/90;**整图必须同一时刻**,
  各点用各自拍摄时刻会与底图错位(真机踩过"M 8 不在银心")。
- bias/dark-only 的 run 坐标是停机位,**不上天球**(`_sky_relevant`)。
"""
from __future__ import annotations

import math
import re
from datetime import datetime, timedelta

from astro_smb import astro
from astro_smb.autorunlog import (
    FILTER_UNKNOWN,
    AutorunBlock,
    Night,
    TargetRun,
    parse_exposure_seconds,
)
from astro_smb.guidecheck import POLAR_COND_DEGENERATE
from astro_smb.naming import parse_image_name
from astro_smb.phd2log import RmsStats, guide_coverage, rms_for_interval
from astro_smb.util import human_size
from astro_smb.i18n import N_, gettext as _
from astro_smb_app.logstore import guide_summary_for_run, section_begins

# 环的候选满量程(角分)。取"刚好装得下"的那一档,免得 1′ 的误差和 30′ 的
# 误差画出来一模一样。
POLAR_FULL_SCALES = (1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0)
# 注:实际共享名要走 shell.data_share —— 本地卡的共享名是卷标,不是这个。
# 这里只留作 shell 不可用时的兜底缺省。
PLAN_SHARE = "EMMC Images"
PLAN_LIGHT_DIR = "Plan\\Light"
# 事件时间线: 相邻条目间隔超过该秒数时插入"间隙"分隔条目(反馈#4)
TIMELINE_GAP_S = 180.0
# 巡天底图渲染像素尺寸: Canvas 逻辑尺寸 340 × 1.25(典型 DPI 缩放)取整
SKY_RENDER_PX = int(340 * 1.25)
# 整夜时间轴: run 顺序循环取色(与空间页 treemap 调色一致的低饱和色)
_TL_PALETTE = [
    (0x4F, 0x8A, 0xC7), (0x6A, 0xB0, 0x4F), (0xC7, 0x8A, 0x4F),
    (0xB0, 0x4F, 0x8A), (0x4F, 0xC7, 0xB0), (0x8A, 0x6A, 0xC7),
]
# 无 end_time 且无帧的块在时间轴上的最小显示时长(秒)
_TL_MIN_BLOCK_S = 60.0
# 统计卡帧型展示顺序; 隐式组(frame_type=None)归入 unknown
_FRAME_TYPE_ORDER = ("light", "dark", "bias", "flat", "unknown")
_FRAME_TYPE_LABEL = {"unknown": N_("未知型")}
# 详情徽章上的帧型中文名(unknown = 无 Shooting 行的逐帧变曝光组)
_FRAME_TYPE_CN = {"light": N_("亮场"), "dark": N_("暗场"), "bias": N_("偏置"),
                  "flat": N_("平场"), "unknown": N_("变曝光")}
# 目标列表时间轨道: 相邻 run 间隔超过该秒数时插入"间隙"细行(反馈#3)
TARGET_GAP_S = 900.0
# 时间轨道列: 总宽 = 时刻文字列 + 间距 + 竖线/圆点列
# ("HH:MM" 在 Consolas 11 上约 30px, 留到 35px 才不会被右对齐裁掉)
TRACK_W = 48.0
TRACK_RAIL_W = 10.0
TRACK_GAP = 3.0
# 轨道几何: 上连接线 0~DOT_TOP, 圆点 DOT_TOP~DOT_TOP+9, 下连接线自 DOT_BOT 起拉伸
# (对齐内容列首行文字中心: 内容上边距 6 + 半行高 ≈ 15.5)
_DOT_TOP = 11.0
_DOT_BOT = 20.0
# 分组视图里 run/间隙行的左缩进
GROUP_INDENT = 18.0
# 状态级别 → 列表行标记字符(圆点用同级别的画刷着色)
_LEVEL_MARK = {"ok": "✓", "warn": "⏸", "err": "✗"}
# 详情徽章配色 (底色 RGB, 字色 RGB) —— 浅底深字, 深浅主题下均可读
# (与浏览页详情卡片同一套口径, 见 _browser._badge_brushes)
_BADGE_COLORS = {
    "ok":      ((0xDD, 0xEF, 0xDD), (0x1B, 0x5E, 0x20)),   # 已完成: 绿
    "warn":    ((0xFB, 0xEA, 0xC5), (0x7A, 0x52, 0x00)),   # 已暂停: 琥珀
    "err":     ((0xF8, 0xDD, 0xDD), (0x8E, 0x1F, 0x1F)),   # 被截断: 红
    "plan":    ((0xDF, 0xE9, 0xEC), (0x24, 0x50, 0x60)),   # 计划号: 青灰
    "light":   ((0xDD, 0xEF, 0xDD), (0x1B, 0x5E, 0x20)),   # 亮场: 绿
    "bias":    ((0xE9, 0xE9, 0xE9), (0x45, 0x45, 0x45)),   # 偏置: 灰
    "dark":    ((0xD3, 0xD3, 0xDC), (0x2A, 0x2A, 0x38)),   # 暗场: 深灰
    "flat":    ((0xD9, 0xE7, 0xF8), (0x0D, 0x47, 0xA1)),   # 平场: 蓝
    "filter":  ((0xE4, 0xDD, 0xF2), (0x4A, 0x33, 0x82)),   # 滤镜: 紫
    "info":    ((0xE6, 0xE6, 0xE6), (0x50, 0x50, 0x50)),   # 中性
}
# 甘特图的排版常量。**绘制与命中反算必须共用这一组**, 一旦走偏就会点错目标
# —— 横条不再逐个挂 Tapped(见 RecordsPage._draw_timeline 的说明), 画布上
# 只有一个 Tapped, 命中哪一条全靠下面的纯函数算, 所以单测直接钉死它。
TL_BAR_Y, TL_BAR_H = 6.0, 14.0          # 目标块横条
TL_GUIDE_Y, TL_GUIDE_H = 26.0, 5.0      # 导星覆盖细条
TL_TICK_Y = 36.0                        # 小时刻度线底 / 刻度标签顶
TL_BAR_MIN_W = 2.0                      # 极短块也要看得见的最小宽度
TL_LABEL_MIN_W = 46.0                   # 条内写目标名所需的最小宽度
TL_HIT_PAD_Y = 4.0                      # 命中带上下各放宽(细条不好点)
TL_HIT_PAD_X = 3.0                      # 命中带左右各放宽(2px 宽的块几乎点不中)
_XML_ESCAPE = {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}
# Guide 事件原文关键词 → 紧凑短语(按序匹配, 真机样例归纳)
#: 日志原文片段 → (显示短语, 严重程度)
#:
#: **严重程度写在表里,不要事后去显示文本里找关键词。** 原来的写法是
#: ``any("丢星" in s or "失败" in s for s, _ in segs)`` —— 拿**翻译过的显示
#: 文本**反推语义。那样一改文案(或者一做 i18n)判断就静默失效:导星丢星的
#: 那一段不再标警告,而界面上什么异常都没有。
_GUIDE_PHRASES = (
    ("select guide star failed", N_("选星失败"), "warn"),
    ("reselect", N_("重选星"), "info"),
    ("lost", N_("丢星"), "warn"),
    ("settle done", N_("稳定完成"), "info"),
    ("settle", N_("稳定中"), "info"),
    ("start guiding", N_("开始"), "info"),
    ("stop guiding", N_("停止"), "info"),
    ("stop looping", N_("停止循环"), "info"),
    ("calibrate success", N_("校准成功"), "info"),
    ("calibrat", N_("校准"), "info"),
    ("dither", N_("抖动"), "info"),
)


def polar_plot_scale(total_arcmin: float) -> float:
    """挑一个刚好装得下的满量程(角分)。"""
    want = max(float(total_arcmin) * 1.25, 0.5)
    for s in POLAR_FULL_SCALES:
        if s >= want:
            return s
    return POLAR_FULL_SCALES[-1]


def polar_plot_geometry(polar, size: float = 132.0):
    """极轴误差 → 示意图的纯几何(不碰任何 XAML,可离线单测)。

    返回 ``dict``:``center``/``radius``/``full``(满量程角分)/``rings``
    (半径列表)/``marker``(实际极轴落点)/``labels``。

    **方位约定与本项目其他天球图一致:北上、东左**(见 skyview.radar_xy 与
    records 天球图)。所以方位分量向东为正时,点子往**左**跑;高度分量为正
    (极轴抬高了)时点子往**上**跑。四个边标注写死在图上,不靠读者记约定 ——
    极轴调整方向记反了就白折腾一晚上。
    """
    cx = cy = float(size) / 2.0
    r = cx - 14.0
    full = polar_plot_scale(polar.total_arcmin if polar is not None else 0.0)
    rings = [r * f for f in (0.25, 0.5, 0.75, 1.0)]
    if polar is None:
        marker = None
    else:
        # az/alt 是**度**,换成角分再按满量程归一
        fx = (polar.az * 60.0) / full
        fy = (polar.alt * 60.0) / full
        # 落点可能超出满量程(极端情况),夹到圆内免得画到画布外
        m = math.hypot(fx, fy)
        if m > 1.0:
            fx, fy = fx / m, fy / m
        marker = (cx - fx * r, cy - fy * r)     # 东在左 ⇒ x 取负
    return {
        "center": (cx, cy), "radius": r, "full": full,
        "rings": rings, "marker": marker,
        "labels": ((_("偏高"), cx, cy - r - 11.0), (_("偏低"), cx, cy + r + 2.0),
                   (_("偏东"), cx - r - 12.0, cy - 6.0),
                   (_("偏西"), cx + r + 1.0, cy - 6.0)),
    }


def polar_advice(polar, *, cond: float = float("inf"),
                 falsifiable: bool = False) -> str:
    """一句人话:极轴偏了多少、该往哪调、这个数字可不可信。

    **调整方向与误差方向相反** —— 误差是"极轴偏东了"就得往西拧。这一句写死在
    文案里,不让用户自己反推符号;方向记反了就白折腾一晚上。
    """
    if polar is None:
        return ""
    if cond > POLAR_COND_DEGENERATE:
        return (_("极轴偏差约 {total_arcmin:.1f}′,但条件数 {cond:.0f},方位与高度分量在观测上几乎简并 —— 只能看总量,换个赤纬远离 0° 的目标即可分解").format(
            
            total_arcmin=polar.total_arcmin, cond=cond))
    ns = _("下调") if polar.alt > 0 else _("上调")
    ew = _("向西") if polar.az > 0 else _("向东")
    tail = "" if falsifiable else _(";单目标恰定,这个数字推翻不了,只能当量级参考")
    return (_("把极轴{ew}拧 {0:.2f}′、{ns} {1:.2f}′{tail}").format(
        abs(polar.az) * 60, abs(polar.alt) * 60, ew=ew, ns=ns, tail=tail))


# 注:实际共享名要走 shell.data_share —— 本地卡的共享名是卷标,不是这个。
# 这里只留作 shell 不可用时的兜底缺省。


def _sky_relevant(run: TargetRun) -> bool:
    """该 run 是否值得画上天球:纯偏置/暗场会话的日志坐标是停机位(实测
    DEC+89° 之类),画出来只会误导;亮场/平场/未知帧型(变曝光序列)保留。"""
    types = [g.frame_type for b in run.blocks for g in b.groups]
    if not types:
        return True     # 无帧(失败尝试)仍显示计划位置
    return not all(t in ("bias", "dark") for t in types)

# 事件时间线: 相邻条目间隔超过该秒数时插入"间隙"分隔条目(反馈#4)

# 巡天底图渲染像素尺寸: Canvas 逻辑尺寸 340 × 1.25(典型 DPI 缩放)取整

# 整夜时间轴: run 顺序循环取色(与空间页 treemap 调色一致的低饱和色)
# 无 end_time 且无帧的块在时间轴上的最小显示时长(秒)

# 统计卡帧型展示顺序; 隐式组(frame_type=None)归入 unknown
# 详情徽章上的帧型中文名(unknown = 无 Shooting 行的逐帧变曝光组)

# 目标列表时间轨道: 相邻 run 间隔超过该秒数时插入"间隙"细行(反馈#3)
# 时间轨道列: 总宽 = 时刻文字列 + 间距 + 竖线/圆点列
# ("HH:MM" 在 Consolas 11 上约 30px, 留到 35px 才不会被右对齐裁掉)
# 轨道几何: 上连接线 0~DOT_TOP, 圆点 DOT_TOP~DOT_TOP+9, 下连接线自 DOT_BOT 起拉伸
# (对齐内容列首行文字中心: 内容上边距 6 + 半行高 ≈ 15.5)
# 分组视图里 run/间隙行的左缩进

# 状态级别 → 列表行标记字符(圆点用同级别的画刷着色)

# 详情徽章配色 (底色 RGB, 字色 RGB) —— 浅底深字, 深浅主题下均可读
# (与浏览页详情卡片同一套口径, 见 _browser._badge_brushes)


def _fmt_lon(lon: float) -> str:
    return f"{abs(lon):.1f}°{'E' if lon >= 0 else 'W'}"


def _fmt_lat(lat: float) -> str:
    return f"{abs(lat):.1f}°{'N' if lat >= 0 else 'S'}"


def _fmt_integration(seconds: float) -> str:
    """总曝光积分时间: 5430 → '1h 30m'; 90 → '1m 30s'; 12 → '12s'。"""
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {sec:02d}s"
    return f"{sec}s"


def _fmt_range(t0, t1) -> str:
    if t1.date() == t0.date():
        return f"{t0:%Y-%m-%d %H:%M:%S} ~ {t1:%H:%M:%S}"
    return f"{t0:%Y-%m-%d %H:%M:%S} ~ {t1:%m-%d %H:%M:%S}"


# ---------------------------------------------------------------- FITS 头信息(反馈#2#3, 纯计算)

def _fits_num(hdr, key: str) -> float | None:
    """FITS 卡片数值化;缺失/非数值返回 None。"""
    v = hdr.get(key)
    if v is None:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _fits_info(hdr) -> dict:
    """从 FITS 头提取实测坐标与设备信息(值缺失的键不出现)。

    RA/DEC 是设备写入的实测指向(度, 角秒级), 比日志里的 slew 坐标
    (goto 请求值)更准; XPIXSZ 已含 Bin 系数, 算像元比例不要再乘 Bin。
    """
    info: dict = {}
    ra, dec = _fits_num(hdr, "RA"), _fits_num(hdr, "DEC")
    if ra is not None and dec is not None:
        info["ra_deg"] = ra % 360.0
        info["dec_deg"] = max(-90.0, min(90.0, dec))
    for key, name in (("INSTRUME", "camera"), ("TELESCOP", "telescope"),
                      ("FILTER", "filter")):
        v = hdr.get(key)
        if v and v.strip():
            info[name] = v.strip()
    for key, name in (("GAIN", "gain"), ("CCD-TEMP", "ccd_temp"),
                      ("FOCALLEN", "focallen"), ("XPIXSZ", "xpixsz"),
                      ("EXPTIME", "exptime")):
        v = _fits_num(hdr, key)
        if v is not None:
            info[name] = v
    return info


def _pixel_scale(info: dict) -> float | None:
    """像元比例(″/px) = 206.265 × XPIXSZ(µm) / FOCALLEN(mm)。"""
    px, fl = info.get("xpixsz"), info.get("focallen")
    if not px or not fl:
        return None
    return 206.265 * px / fl


# ---------------------------------------------------------------- 夜次统计/时间轴(纯计算, 工作线程调用)

def _block_end(b: AutorunBlock):
    """块的显示结束时刻: end_time 缺失(截断)时用末帧结束/块开始兜底。"""
    if b.end_time is not None:
        return b.end_time
    frames = b.all_frames()
    if frames:
        return frames[-1].end_time
    return b.begin_time


def _night_window(night: Night):
    """夜次时间窗 (t0, t1): 会话 enabled/disabled 优先, 块起止兜底;
    无法确定或区间为空返回 None。"""
    starts = []
    ends = []
    if night.begin_time is not None:
        starts.append(night.begin_time)
    if night.end_time is not None:
        ends.append(night.end_time)
    for r in night.runs:
        for b in r.blocks:
            starts.append(b.begin_time)
            ends.append(_block_end(b))
    if not starts or not ends:
        return None
    t0, t1 = min(starts), max(ends)
    if t1 <= t0:
        return None
    return t0, t1


def _night_summary(night: Night, guide_map: dict, fits_map: dict, *,
                   guide_pending: bool = False,
                   fits_pending: bool = False) -> tuple[str, str]:
    """夜次统计汇总卡内容(左列文本, 右列文本)。纯计算, 不碰 UI。

    ``guide_pending`` / ``fits_pending`` = 懒加载第一段, 对应数据还在后台补;
    此时"导星"/"设备"行显示 **读取中…** 而不是"无数据"(两者必须可分辨)。
    """
    filt: dict[str, float] = {}         # 滤镜 → 亮场积分秒
    type_frames: dict[str, int] = {}    # 帧型 → 实拍帧数
    light_frames = 0
    all_secs = 0.0                      # 全部帧(含校准帧)曝光合计
    for r in night.runs:
        for k, v in r.integration_by_filter().items():
            filt[k] = filt.get(k, 0.0) + v
        for b in r.blocks:
            for g in b.groups:
                key = g.frame_type or "unknown"
                type_frames[key] = type_frames.get(key, 0) + g.actual
                if g.frame_type in ("light", None):
                    light_frames += g.actual
                for f in g.frames:
                    all_secs += f.exposure_s
    light_secs = sum(filt.values())

    n_runs = len(night.runs)
    n_done = sum(1 for r in night.runs if r.finished)

    acs = [a for r in night.runs for b in r.blocks for a in b.autocenter]
    ac_fail = sum(1 for a in acs if not a.ok)
    afs = [af for r in night.runs for b in r.blocks for af in b.autofocus]
    temps = [af.temperature for af in afs if af.temperature is not None]

    # 整夜导星: 逐 run 的 RMS 按帧数平方加权合并(合并 RMS 的正确口径);
    # 角秒/像素不可混合, 优先角秒口径, 全无角秒时退回像素口径
    sq_arc = sq_px = 0.0
    n_arc = n_px = 0
    lost = 0
    for r in night.runs:
        rms, _cov = guide_map.get(id(r), (None, 0.0))
        if rms is None:
            continue
        lost += rms.n_lost
        if rms.n_frames <= 0:
            continue
        if rms.in_arcsec:
            sq_arc += rms.rms_total ** 2 * rms.n_frames
            n_arc += rms.n_frames
        else:
            sq_px += rms.rms_total ** 2 * rms.n_frames
            n_px += rms.n_frames
    if n_arc:
        guide_txt = (_("平均 RMS {0:.2f}″(帧数加权)· 丢星 {lost} 帧").format(
            math.sqrt(sq_arc / n_arc), lost=lost))
    elif n_px:
        guide_txt = (_("平均 RMS {0:.2f}px(帧数加权)· 丢星 {lost} 帧").format(
            math.sqrt(sq_px / n_px), lost=lost))
    elif lost:
        guide_txt = _("区间内全部丢星 · 丢星 {lost} 帧").format(lost=lost)
    else:
        # 第一段只有夜次没有逐帧导星 ⇒ 明确写"读取中", 不能报"无数据"
        guide_txt = _("读取中…") if guide_pending else _("无数据")

    # 时间利用率 = Σ帧曝光 ÷ 夜次时长
    util_txt = "—"
    window = _night_window(night)
    if window is not None and all_secs > 0:
        night_s = (window[1] - window[0]).total_seconds()
        if night_s > 0:
            util_txt = (_("{0:.0f}%(曝光 {1} / 夜长 {2})").format(
                all_secs / night_s * 100.0, _fmt_integration(all_secs), _fmt_integration(night_s)))

    filt_parts = [f"{k or _('未知')} {_fmt_integration(v)}"
                  for k, v in sorted(filt.items(), key=lambda kv: -kv[1])
                  if v > 0]
    type_parts = [f"{_(_FRAME_TYPE_LABEL.get(k, k))} {type_frames[k]}"
                  for k in _FRAME_TYPE_ORDER if type_frames.get(k)]
    type_parts += [f"{k} {v}" for k, v in type_frames.items()
                   if k not in _FRAME_TYPE_ORDER and v]     # 未知帧型兜底

    left = "\n".join([
        _("亮场积分: {0} · {light_frames} 帧").format(
            _fmt_integration(light_secs), light_frames=light_frames),
        _("按滤镜: ") + (" · ".join(filt_parts) if filt_parts else _("无亮场")),
        _("帧型: ") + (" · ".join(type_parts) if type_parts else _("无帧")),
        _("目标: {n_runs} 个 · 完成 {n_done} 个").format(n_runs=n_runs, n_done=n_done),
    ])
    af_txt = _("{0} 次").format(len(afs))
    if temps:
        lo, hi = min(temps), max(temps)
        af_txt += (_(" · 温度 {lo:g}~{hi:g}℃").format(lo=lo, hi=hi) if lo != hi else _(" · 温度 {lo:g}℃").format(
            
            lo=lo))
    right_lines = [
        _("时间利用率: {util_txt}").format(util_txt=util_txt),
        _("AutoCenter: {0} 次 · 失败 {ac_fail} 次").format(len(acs), ac_fail=ac_fail),
        f"AutoFocus: {af_txt}",
        _("导星: {guide_txt}").format(guide_txt=guide_txt),
    ]
    # 设备行(反馈#2): 取该夜任一有 FITS 头的 run(同夜设备不会变)
    for r in night.runs:
        info = fits_map.get(id(r))
        if info and (info.get("camera") or info.get("telescope")):
            parts = [p for p in (info.get("camera"), info.get("telescope")) if p]
            if info.get("focallen"):
                parts.append(f"{info['focallen']:g}mm")
            right_lines.append(_("设备: ") + " · ".join(parts))
            break
    else:
        if fits_pending:                    # FITS 头还在后台读
            right_lines.append(_("设备: 读取中…"))
    return left, "\n".join(right_lines)


def _spans_from_sections(sections: list[dict]) -> list[tuple]:
    """导星**段摘要** → (起, 讫) 区间列表。

    整夜时间轴的绿色导星覆盖条只需段级起止, 不碰逐帧 —— 于是懒加载第一段
    (只有 ``LogSummary.phd2_sections``, 没有 ``Phd2Log``)也能画出这条。
    """
    out: list[tuple] = []
    for sec in sections or ():
        t0 = section_begins(sec)
        try:
            t1 = datetime.fromisoformat(sec.get("end_eff"))
        except (TypeError, ValueError):
            t1 = None
        if t0 is not None and t1 is not None:
            out.append((t0, t1))
    return out


def _night_timeline(night: Night, phd2_logs: list,
                    spans: list[tuple] | None = None) -> dict | None:
    """整夜时间轴数据(纯计算): 归一化的目标块横条/导星覆盖/小时刻度。

    bars: (f0, f1, 色号, 透明度, 目标名, 提示文本, run 引用);
    guides: (f0, f1); ticks: (f, 'HH:MM')。无可画内容返回 None。

    ``spans`` 给定时用它作导星区间来源(懒加载第一段的段摘要), 否则从
    ``phd2_logs`` 的逐帧段取 —— 两条路径产出的 guides 完全一致。
    """
    window = _night_window(night)
    if window is None:
        return None
    t0, t1 = window
    span = (t1 - t0).total_seconds()

    bars = []
    for ri, run in enumerate(night.runs):
        for b in run.blocks:
            b0 = b.begin_time
            b1 = max(_block_end(b), b0 + timedelta(seconds=_TL_MIN_BLOCK_S))
            f0 = max(0.0, (b0 - t0).total_seconds() / span)
            f1 = min(1.0, (b1 - t0).total_seconds() / span)
            if f1 <= f0:
                continue
            finished = b.end_mode == "Finish"
            # tooltip 用真实结束时刻(b1 只是保证可见宽度的绘制下限)
            real_end = _block_end(b)
            tip = (_('{target}\n{b0:%H:%M:%S} ~ {real_end:%H:%M:%S} · {total_frames} 帧').format(
                
                target=run.target, b0=b0, real_end=real_end, total_frames=b.total_frames))
            if not finished:
                tip += " · " + (_("暂停") if b.end_mode == "Pause" else _("截断"))
            bars.append((f0, f1, ri % len(_TL_PALETTE),
                         1.0 if finished else 0.6, run.target, tip, run))
    if not bars:
        return None

    if spans is None:
        spans = [(sec.begins, sec.end_time_effective)
                 for log in phd2_logs for sec in log.guide_sections]
    guides = []
    for s0, e0 in spans:
        s = max(t0, s0)
        e = min(t1, e0)
        if e > s:
            guides.append(((s - t0).total_seconds() / span,
                           (e - t0).total_seconds() / span))

    hours = span / 3600.0
    step = 1 if hours <= 9 else (2 if hours <= 18 else 4)
    ticks = []
    tick = t0.replace(minute=0, second=0, microsecond=0)
    if tick < t0:
        tick += timedelta(hours=1)
    while tick <= t1:
        if tick.hour % step == 0:
            ticks.append(((tick - t0).total_seconds() / span, f"{tick:%H:%M}"))
        tick += timedelta(hours=1)
    return {"bars": bars, "guides": guides, "ticks": ticks}


# ------------------------------------------------ 整夜时间轴: 排版几何 + 命中反算

# 甘特图的排版常量。**绘制与命中反算必须共用这一组**, 一旦走偏就会点错目标
# —— 横条不再逐个挂 Tapped(见 RecordsPage._draw_timeline 的说明), 画布上
# 只有一个 Tapped, 命中哪一条全靠下面的纯函数算, 所以单测直接钉死它。


def timeline_bar_px(f0: float, f1: float, w: float) -> tuple[float, float]:
    """归一化区间 → 横条的 (左边界, 宽度) 像素。绘制与命中反算共用。"""
    return f0 * w, max(TL_BAR_MIN_W, (f1 - f0) * w)


def timeline_hit_bar(x: float, y: float, spans, w: float) -> int | None:
    """时间轴画布坐标 → 命中的横条序号(没命中返回 None)。

    ``spans`` 是与绘制同序的 (f0, f1) 归一化区间列表, ``w`` 必须是**这次画的**
    那个画布宽度(画的时候存进 ``_tl_w``)—— 画布宽随窗口变, 用错宽度就会
    整体错位。

    命中带比"逐根 Rectangle 挂事件"时更宽容: 纵向连刻度线上沿一起算, 横向
    左右各放宽 ``TL_HIT_PAD_X``(短块原本只有 2px 宽, 几乎点不中)。多个候选
    时优先真正落在条上的, 其次取中心更近的, 再其次取**后画的**(z 序在上)。
    """
    if w <= 0:
        return None
    if not (TL_BAR_Y - TL_HIT_PAD_Y <= y <= TL_BAR_Y + TL_BAR_H + TL_HIT_PAD_Y):
        return None
    best = None
    for i, (f0, f1) in enumerate(spans):
        x0, bw = timeline_bar_px(f0, f1, w)
        if not (x0 - TL_HIT_PAD_X <= x <= x0 + bw + TL_HIT_PAD_X):
            continue
        exact = 1 if x0 <= x <= x0 + bw else 0
        cand = (exact, -abs(x - (x0 + bw / 2.0)), i)
        if best is None or cand > best:
            best = cand
    return None if best is None else best[2]


# ---------------------------------------------------------------- 批量绘图片段



def scale_alpha(hex_argb: str, alpha: float) -> str:
    """把元素级 ``Opacity`` 折进颜色的 A 通道。

    纯色矩形上两者结果完全一致(Opacity 就是对整个元素的 alpha 乘法), 而
    `_common.rect_fragment` 只吃颜色 —— 折进去才能在批量路径里保住甘特条
    "半透明 = 暂停/截断"的既有语义。
    """
    if alpha >= 0.999:
        return hex_argb
    try:
        a = int(hex_argb[1:3], 16)
    except (ValueError, IndexError):
        return hex_argb
    a = max(0, min(255, int(round(a * max(0.0, alpha)))))
    return f"#{a:02X}{hex_argb[3:]}"


# ---------------------------------------------------------------- 事件时间线(反馈#4, 纯计算, 工作线程调用)

def _fmt_exp_compact(exposure: str | None) -> str | None:
    """曝光原文('300.0s'/'1.0ms') → 紧凑显示('300s'/'1ms'); auto/None → None。"""
    if not exposure or exposure == "auto":
        return None
    v = parse_exposure_seconds(exposure)
    if v is None:
        return exposure
    if v >= 1.0:
        return f"{v:g}s"
    return f"{v * 1000.0:g}ms"


def _ac_short(result: str | None) -> str:
    """AutoCenter 单次结果 → 紧凑短语。真机样例: 'The target is centered' /
    'Mount slews failed' / 'Too far from center, distance = 585%(13.07°)' /
    'Plate Solve failed, Star number = 8' / 'Exposure failed'。"""
    if not result:
        return _("无结果")
    low = result.lower()
    if "centered" in low:
        return _("居中")
    m = re.search(r"([\d.]+)\s*%", result)
    if m:
        return _("偏差{0}%").format(m.group(1))
    if "slew" in low:
        return _("GoTo失败")
    if "solve" in low:
        return _("解析失败")
    if "exposure" in low:
        return _("曝光失败")
    if "fail" in low:
        return _("失败")
    return result if len(result) <= 12 else result[:12] + "…"


# Guide 事件原文关键词 → 紧凑短语(按序匹配, 真机样例归纳)


def _guide_phrase(event: str) -> tuple[str, str]:
    """导星事件原文 → (显示短语, 严重程度)。

    严重程度来自 `_GUIDE_PHRASES`,**不是从显示文本里猜的** —— 见那张表的注释。
    """
    low = event.lower()
    for k, v, level in _GUIDE_PHRASES:
        if k in low:
            return _(v), level          # 表里存的是 msgid,到这一步才翻
    return (event if len(event) <= 10 else event[:10] + "…"), "info"


def _guide_short(event: str) -> str:
    return _guide_phrase(event)[0]


def _fmt_gap(seconds: float) -> str:
    m = int(round(seconds / 60.0))
    if m >= 60:
        return _("间隙 {0} 小时 {1:02d} 分钟").format(m // 60, m % 60)
    return _("间隙 {m} 分钟").format(m=m)


def _ac_card(attempts: list) -> dict:
    """AutoCenter 整组归并为一张卡: 逐次结果归纳成紧凑串。"""
    n = len(attempts)
    ok = attempts[-1].ok
    t1 = attempts[-1].end_time or attempts[-1].begin_time
    seq = " → ".join(f"#{a.attempt_no} {_ac_short(a.result)}" for a in attempts)
    return {"kind": "ac", "t0": attempts[0].begin_time, "t1": t1,
            "title": _("自动居中 · {n} 次尝试 · {0}").format(_("成功") if ok else _("失败"), n=n),
            "subtitle": seq, "level": "ok" if ok else "err"}


def _af_card(af) -> dict:
    if af.manual_cancel:
        title, level = _("自动对焦 · 手动取消"), "warn"
    elif af.success and af.focused_position is not None:
        title = _("自动对焦 · 成功@{focused_position}").format(
            focused_position=af.focused_position)
        if af.temperature is not None:
            title += f" · {af.temperature:g}℃"
        level = "ok"
    elif af.success:
        title, level = _("自动对焦 · 成功"), "ok"
    elif af.success is None:
        title, level = _("自动对焦 · 未记录结果"), "info"
    else:
        title, level = _("自动对焦 · 失败"), "err"
    return {"kind": "af", "t0": af.begin_time, "t1": af.end_time,
            "title": title, "subtitle": "", "level": level}


def _group_card(g) -> dict | None:
    """Shooting 组 → 一张卡(带实拍/计划迷你进度条)。无起点且无帧的组跳过。"""
    if g.start_time is None and not g.frames:
        return None
    t0 = g.start_time or g.frames[0].time
    title = _("拍摄 {0} ×{actual}").format(g.frame_type or _("变曝光"), actual=g.actual)
    exp = _fmt_exp_compact(g.exposure)
    if exp:
        title += f" · {exp}"
    elif g.exposure == "auto":
        title += _(" · 自动曝光")
    total = sum(f.exposure_s for f in g.frames)
    if total > 0:
        title += _(" · 计 {0}").format(_fmt_integration(total))
    sub = []
    if g.frames:
        sub.append(_("首帧 {time:%H:%M:%S} · 末帧 {end_time:%H:%M:%S}").format(
            time=g.frames[0].time, end_time=g.frames[-1].end_time))
    if g.binning:
        sub.append(g.binning)
    if g.planned:
        level = "ok" if g.actual >= g.planned else ("info" if g.actual else "warn")
    else:
        level = "info"
    item = {"kind": "group", "t0": t0,
            "t1": g.frames[-1].end_time if g.frames else None,
            "title": title, "subtitle": " · ".join(sub), "level": level}
    if g.planned:
        item["progress"] = (g.actual, g.planned)
    return item


def _guide_card(events: list) -> dict:
    """连续 Guide 事件段 → 一张归并卡: 相邻同短语合并计数。"""
    n = len(events)
    t0, t1 = events[0].time, events[-1].time
    segs: list[list] = []
    for ge in events:
        s, level = _guide_phrase(ge.event)
        if segs and segs[-1][0] == s:
            segs[-1][1] += 1
        else:
            segs.append([s, 1, level])
    parts = [f"{s}×{c}" if c > 1 else s for s, c, _lv in segs]
    if len(parts) > 6:
        parts = parts[:6] + ["…"]
    # **按语义判,不在显示文本里找关键词** —— 见 `_GUIDE_PHRASES` 的注释
    warn = any(lv == "warn" for _s, _c, lv in segs)
    sub = _("{n} 个事件").format(n=n)
    if n > 1:
        sub += f" · {t0:%H:%M:%S} ~ {t1:%H:%M:%S}"
    return {"kind": "guide", "t0": t0, "t1": t1 if t1 != t0 else None,
            "title": _("导星: ") + " → ".join(parts), "subtitle": sub,
            "level": "warn" if warn else "info"}


def _timeline_items(run: TargetRun) -> list[dict]:
    """把 run 的块/AutoCenter/AutoFocus/Shooting/Guide 事件加工成结构化
    时间线条目(纯计算, 工作线程调用; 归并后通常 10~30 条)。

    每条: {kind, t0, t1|None, title, subtitle, level(ok|warn|err|info)
           [, progress=(actual, planned)]}; kind='gap' 为间隙分隔条目。
    """
    raw: list[dict] = []
    for bi, b in enumerate(run.blocks, 1):
        sub = f"RA {b.ra}  DEC {b.dec or '?'}" if b.ra else ""
        raw.append({"kind": "block", "t0": b.begin_time, "t1": None,
                    "title": _("目标块 #{bi} 开始").format(bi=bi), "subtitle": sub,
                    "level": "info"})
        if b.end_time is not None:
            mode = {"Finish": _("完成"), "Pause": _("暂停")}.get(
                b.end_mode, b.end_mode or _("截断"))
            title = _("目标块 #{bi} 结束 · {mode}").format(bi=bi, mode=mode)
            if b.manual_stop:
                title += _("(手动)")
            raw.append({"kind": "block", "t0": b.end_time, "t1": None,
                        "title": title, "subtitle": "",
                        "level": "ok" if b.end_mode == "Finish" else "warn"})
        if b.autocenter:
            raw.append(_ac_card(b.autocenter))
        for af in b.autofocus:
            raw.append(_af_card(af))
        for g in b.groups:
            item = _group_card(g)
            if item is not None:
                raw.append(item)
        for ge in b.guide_events:
            raw.append({"kind": "guide_raw", "t0": ge.time, "ev": ge})
    raw.sort(key=lambda it: it["t0"])

    # 连续 Guide 事件段归并为一张卡(与旧版折叠逻辑同界定: 排序后相邻)
    items: list[dict] = []
    i = 0
    while i < len(raw):
        if raw[i]["kind"] != "guide_raw":
            items.append(raw[i])
            i += 1
            continue
        j = i
        while j < len(raw) and raw[j]["kind"] == "guide_raw":
            j += 1
        items.append(_guide_card([raw[k]["ev"] for k in range(i, j)]))
        i = j

    # 相邻条目间隔超过 TIMELINE_GAP_S → 插入"间隙"分隔条目;
    # 间隔基准取"迄今最晚结束时刻"(长卡片可能覆盖后续条目的开始)
    out: list[dict] = []
    prev_end = None
    for it in items:
        if prev_end is not None:
            gap = (it["t0"] - prev_end).total_seconds()
            if gap > TIMELINE_GAP_S:
                out.append({"kind": "gap", "t0": prev_end, "t1": None,
                            "title": _fmt_gap(gap), "subtitle": "",
                            "level": "info"})
        end = it["t1"] or it["t0"]
        prev_end = end if prev_end is None else max(prev_end, end)
        out.append(it)
    return out


# ---------------------------------------------------------------- 目标行/列表布局/详情(反馈#3, 纯计算, 工作线程调用)

def _run_level(run: TargetRun) -> str:
    """目标行状态级别: 零帧=err(红) / 已完成=ok(绿) / 其余=warn(琥珀)。"""
    if run.total_frames == 0:
        return "err"
    return "ok" if run.finished else "warn"


def _end_state(run: TargetRun) -> tuple[str, str]:
    """结束状态的**单一真源**: (状态词, 级别 ok|warn|err)。

    仅凭图标分不清"完成/暂停/截断", 故给出明确文字。口径 = **最后一个块的
    最终状态**(Pause 把同一 Plan 分裂成多块, 中途那些块不改结论);
    详情徽章 / 列表行副行 / 详情 KV「结束方式」三处共用此函数, 否则同一个
    run 会出现"被截断"徽章配"暂停"结束方式这种自相矛盾的说法。
    "中途曾暂停几次"属于过程信息, 由调用方放进 KV 副注, 不改主值口径。
    """
    if run.finished:                        # finished = 任一块 end_mode==Finish
        return _("已完成"), "ok"
    last = run.blocks[-1] if run.blocks else None
    mode = last.end_mode if last is not None else None
    if mode == "Pause":
        return _("已暂停"), "warn"
    if mode is None:                        # 会话截断(日志里没有结束行)
        return _("被截断"), "err"
    return _("其它({mode})").format(mode=mode), "warn"           # 未知原文原样示人, 不硬套语义


def _run_subline(run: TargetRun, guide_map: dict, *,
                 guide_pending: bool = False) -> str:
    parts = [_end_state(run)[0]]
    for k, (planned, actual) in run.type_stats().items():
        parts.append(f"{k} {actual}/{planned}" if planned else f"{k} {actual}")
    sub = " · ".join(parts) if len(parts) > 1 else parts[0] + _(" · 无帧")
    if run.attempts > 1:
        sub += _(" · {attempts} 次尝试").format(attempts=run.attempts)
    rms, cov = guide_map.get(id(run), (None, 0.0))
    if rms is not None and rms.n_frames > 0:
        u = "″" if rms.in_arcsec else "px"
        sub += _(" · RMS {rms_total:.2f}{u} · 覆盖{0:.0f}%").format(
            cov * 100, rms_total=rms.rms_total, u=u)
    elif guide_pending and id(run) not in guide_map:
        # 第一段: RMS 尾巴尚未算出。"暂缺"与"确实没导星"必须可分辨
        sub += _(" · 导星读取中…")
    return sub


def _run_row_data(run: TargetRun, guide_map: dict, *,
                  guide_pending: bool = False) -> dict:
    """目标行的全部显示字段(纯字符串/级别键, UI 侧只做赋值)。"""
    level = _run_level(run)
    return {
        "time": f"{run.begin_time:%H:%M}",
        "level": level,
        "mark": _LEVEL_MARK.get(level, "·"),
        "name": run.target,
        "plan": (_("计划{plan_no}").format(
            plan_no=run.plan_no) if run.plan_no is not None else _("单目标")),
        "sub": _run_subline(run, guide_map, guide_pending=guide_pending),
    }


def _run_key(run: TargetRun) -> tuple:
    """跨数据代**稳定**的 run 标识。

    两段式加载 / 两次刷新各自跑一遍 ``aggregate_nights``, 产出的是**不同
    对象** —— ``id(run)`` 只在同一代内有效, 恢复选中项必须用这个键。
    (目标名 + 计划号 + 起始时刻在同一夜内唯一。)
    """
    return (run.target, run.plan_no, run.begin_time)


def _runs_with_gaps(runs: list[TargetRun], indent: float,
                    group: str | None) -> list[dict]:
    """按时间顺序铺开 run,相邻间隔 > TARGET_GAP_S 处插入"间隙"条目。

    间隔基准取"迄今最晚结束时刻"(Pause 分裂的 run 整段区间可能互相覆盖)。
    """
    items: list[dict] = []
    prev_end = None
    for r in runs:
        if prev_end is not None:
            gap = (r.begin_time - prev_end).total_seconds()
            if gap > TARGET_GAP_S:
                items.append({"kind": "gap", "text": _fmt_gap(gap),
                              "indent": indent, "group": group})
        items.append({"kind": "run", "run": r, "indent": indent, "group": group})
        end = r.end_time or r.begin_time
        prev_end = end if prev_end is None else max(prev_end, end)
    return items


def _group_header(plan_no: int | None, runs: list[TargetRun]) -> dict:
    """Plan 组头条目: 计划号 + 起止时刻 + 目标数 + 总帧数 + 结果。"""
    t0 = min(r.begin_time for r in runs)
    t1 = max((r.end_time or r.begin_time) for r in runs)
    n_done = sum(1 for r in runs if r.finished)
    frames = sum(r.total_frames for r in runs)
    result = _("全部完成") if n_done == len(runs) else _("完成 {n_done}/{0}").format(
        len(runs), n_done=n_done)
    return {
        "kind": "group",
        "key": ("solo" if plan_no is None else f"p{plan_no}"),
        "title": (_("单目标拍摄") if plan_no is None else _("计划 {plan_no}").format(
            plan_no=plan_no)),
        "sub": (_("{t0:%H:%M} ~ {t1:%H:%M} · {0} 目标 · {frames} 帧 · {result}").format(
            len(runs), t0=t0, t1=t1, frames=frames, result=result)),
    }


def _night_layouts(night: Night) -> dict:
    """当前夜次的两套列表布局(纯数据): 平铺 flat / 按 Plan 分组 grouped。

    两套都预先算好, 「合并计划」开关切换时 UI 只换一套条目重画,
    不做任何统计。裸 Autorun(plan_no=None)归入「单目标拍摄」组。
    """
    flat = _runs_with_gaps(list(night.runs), 0.0, None)

    groups: dict[int | None, list[TargetRun]] = {}
    for r in night.runs:
        groups.setdefault(r.plan_no, []).append(r)
    ordered = sorted(groups.items(),
                     key=lambda kv: min(r.begin_time for r in kv[1]))
    grouped: list[dict] = []
    for plan_no, runs in ordered:
        rs = sorted(runs, key=lambda r: r.begin_time)
        head = _group_header(plan_no, rs)
        grouped.append(head)
        grouped.extend(_runs_with_gaps(rs, GROUP_INDENT, head["key"]))
    return {"flat": flat, "grouped": grouped}


def _run_detail(run: TargetRun, guide_map: dict, fits_map: dict, *,
                guide_pending: bool = False,
                fits_pending: bool = False) -> dict:
    """目标详情的全部渲染数据(纯数据: 徽章 + 两列 KV 条目)。

    KV 条目字段: k=标签, v=数值, note=淡色副注, mono=等宽,
    level=数值着色级别(ok/warn/err), bar=(占比 0~1, 级别) 迷你进度条。

    ``*_pending`` = 懒加载第一段, 对应数据后台补齐中 → 该行写"读取中…"。
    """
    info = fits_map.get(id(run)) or {}
    ts = run.type_stats()

    # ---- 徽章行 ----
    state, state_lv = _end_state(run)       # 徽章/副行/KV 同一真源
    badges: list[tuple[str, str]] = [
        (state, state_lv),
        (_("计划 {plan_no}").format(
            plan_no=run.plan_no) if run.plan_no is not None else _("单目标"), "plan"),
    ]
    keys = [k for k in _FRAME_TYPE_ORDER if k in ts]
    keys += [k for k in ts if k not in _FRAME_TYPE_ORDER]
    for key in keys:
        planned, actual = ts[key]
        if not planned and not actual:
            continue
        style = key if key in _BADGE_COLORS else "info"
        badges.append((f"{_(_FRAME_TYPE_CN.get(key, key))} {actual}", style))
    # 滤镜: 优先用帧上的滤镜轮槽位(按积分时间降序, 主力滤镜在前),
    # 日志里推不出时退回 FITS 头的 FILTER
    filts = [k for k, _v in sorted(run.integration_by_filter().items(),
                                   key=lambda kv: -kv[1])
             if k and k != FILTER_UNKNOWN]
    if not filts and info.get("filter"):
        filts = [info["filter"]]
    for name in filts[:3]:
        badges.append((_("滤镜 {name}").format(name=name), "filter"))
    if run.attempts > 1:
        badges.append((_("{attempts} 次尝试").format(attempts=run.attempts), "info"))

    # ---- KV 表 ----
    pairs: list[dict] = []
    planned_total = sum(p for p, _a in ts.values())
    if planned_total:
        frac = min(1.0, run.total_frames / planned_total)
        lv = ("ok" if run.total_frames >= planned_total
              else ("warn" if run.total_frames else "err"))
        pairs.append({"k": _("帧数"), "v": f"{run.total_frames} / {planned_total}",
                      "bar": (frac, lv), "note": f"{frac * 100:.0f}%"})
    else:
        pairs.append({"k": _("帧数"), "v": _("{total_frames} 帧").format(
            total_frames=run.total_frames),
                      "note": _("无计划数")})
    pairs.append({"k": _("积分时间"),
                  "v": _fmt_integration(
                      sum(f.exposure_s for f in run.all_frames()))})
    t1 = run.end_time or run.begin_time
    pairs.append({"k": _("时间范围"), "v": _fmt_range(run.begin_time, t1),
                  "mono": True})

    # 结束方式与徽章共用 _end_state(以最后一个块为准); 中途暂停/手动停止
    # 属过程信息, 只进副注 —— 让主值改口径会和徽章打架(分裂 run 上尤其明显)
    end, end_lv = _end_state(run)
    notes = []
    n_pause = sum(1 for b in run.blocks if b.end_mode == "Pause")
    if n_pause:
        notes.append(_("曾暂停 {n_pause} 次").format(n_pause=n_pause))
    if any(b.manual_stop for b in run.blocks):
        notes.append(_("手动停止"))
    pairs.append({"k": _("结束方式"), "v": end, "level": end_lv,
                  "note": " · ".join(notes)})

    acs = [a for b in run.blocks for a in b.autocenter]
    if acs:
        fail = sum(1 for a in acs if not a.ok)
        pairs.append({"k": "AutoCenter",
                      "v": _("{0} 次 · 最终 {1}").format(len(acs), _ac_short(acs[-1].result)),
                      "note": (_("失败 {fail} 次").format(fail=fail) if fail else ""),
                      "level": ("warn" if fail else None)})
    else:
        pairs.append({"k": "AutoCenter", "v": _("无")})

    afs = [af for b in run.blocks for af in b.autofocus]
    if afs:
        poss = [af.focused_position for af in afs
                if af.focused_position is not None]
        temps = [af.temperature for af in afs if af.temperature is not None]
        bad = sum(1 for af in afs if af.manual_cancel or af.success is False)
        v = _("{0} 次").format(len(afs))
        if poss:
            v += (_(" · 位置 {0}").format(poss[-1]) if len(set(poss)) == 1
                  else _(" · 位置 {0}~{1}").format(min(poss), max(poss)))
        note_parts = []
        if temps:
            lo, hi = min(temps), max(temps)
            note_parts.append(f"{lo:g}℃" if lo == hi else f"{lo:g}~{hi:g}℃")
        if bad:
            note_parts.append(_("失败 {bad} 次").format(bad=bad))
        pairs.append({"k": "AutoFocus", "v": v, "note": " · ".join(note_parts),
                      "level": ("warn" if bad else None)})
    else:
        pairs.append({"k": "AutoFocus", "v": _("无")})

    rms, cov = guide_map.get(id(run), (None, 0.0))
    if rms is not None and rms.n_frames > 0:
        u = "″" if rms.in_arcsec else "px"
        # 角秒口径才谈得上好坏阈值; 像素口径不着色(不同像元比例不可比)
        lv = None
        if rms.in_arcsec:
            lv = ("ok" if rms.rms_total < 0.8
                  else ("warn" if rms.rms_total < 1.5 else "err"))
        pairs.append({"k": _("导星 RMS"), "v": _("总 {rms_total:.2f}{u}").format(
            rms_total=rms.rms_total, u=u),
                      "level": lv,
                      "note": (f"RA {rms.rms_ra:.2f}{u}"
                               f" · DEC {rms.rms_dec:.2f}{u}")})
        pairs.append({"k": _("峰值"),
                      "v": (f"RA {rms.peak_ra:.2f}{u}"
                            f" · DEC {rms.peak_dec:.2f}{u}")})
        pairs.append({"k": _("丢星"), "v": _("{n_lost} 帧").format(n_lost=rms.n_lost),
                      "note": _("有效 {n_frames} 帧").format(n_frames=rms.n_frames),
                      "level": ("warn" if rms.n_lost else None)})
        clv = "ok" if cov >= 0.9 else ("warn" if cov >= 0.6 else "err")
        pairs.append({"k": _("覆盖率"), "v": f"{cov * 100:.0f}%",
                      "bar": (cov, clv)})
    elif rms is not None:
        pairs.append({"k": _("导星"), "v": _("区间内全部 {n_lost} 帧丢星").format(n_lost=rms.n_lost),
                      "level": "err"})
    elif guide_pending and id(run) not in guide_map:
        pairs.append({"k": _("导星"), "v": _("读取中…"),
                      "note": _("正在解析导星日志")})
    else:
        pairs.append({"k": _("导星"), "v": _("无数据")})

    if "ra_deg" in info:
        pairs.append({"k": _("实测坐标"),
                      "v": (f"RA {astro.format_ra(info['ra_deg'])}"
                            f"   DEC {astro.format_dec(info['dec_deg'])}"),
                      "mono": True, "note": "FITS"})
    dev = []
    if info.get("camera"):
        dev.append(info["camera"])
    if info.get("gain") is not None:
        dev.append(_("增益 {0:g}").format(info['gain']))
    if info.get("ccd_temp") is not None:
        dev.append(f"{info['ccd_temp']:g}℃")
    if info.get("focallen"):
        dev.append(_("焦距 {0:g}mm").format(info['focallen']))
    scale = _pixel_scale(info)
    if scale is not None:
        dev.append(f"{scale:.2f}″/px")
    if dev:
        pairs.append({"k": _("设备"), "v": " · ".join(dev)})
    elif fits_pending and not info:
        # 实测坐标/设备都出自同一份 FITS 头, 一行占位说清在补什么即可
        pairs.append({"k": _("设备"), "v": _("读取中…"),
                      "note": _("正在读取影像 FITS 头")})

    coord = (f"RA {run.ra or '?'}   DEC {run.dec or '?'}"
             if (run.ra or run.dec) else _("RA/DEC 未记录"))
    return {"badges": badges, "pairs": pairs, "coord": coord}


# ---------------------------------------------------------------- 派生渲染数据(两段共用, 纯计算, 工作线程调用)

def _guide_map_for(nights: list, phd2_logs: list) -> dict[int, tuple]:
    """逐 run 的导星摘要 id(run) → (RmsStats|None, 覆盖率)。

    需要 PHD2 **逐帧**(只有 ``LogData.phd2_logs`` 有), 故懒加载第一段走
    ``summaries()`` 时算不出来 —— 那种情况传空 dict + guide_pending=True。
    """
    out: dict[int, tuple] = {}
    for night in nights:
        for run in night.runs:
            try:
                out[id(run)] = guide_summary_for_run(run, phd2_logs)
            except Exception:
                out[id(run)] = (None, 0.0)
    return out


def _derive_maps(nights: list, guide_map: dict, fits_map: dict,
                 phd2_logs: list, spans: list[tuple] | None = None, *,
                 guide_pending: bool = False,
                 fits_pending: bool = False) -> dict:
    """把夜次派生成 UI 渲染所需的**全部纯数据**(工作线程调用, UI 零计算)。

    两段式加载的两段共用本函数:第一段传空 guide_map/fits_map + pending 标志
    (占位文案), 第二段传齐全的映射 + pending=False(最终结果)。

    返回 {"rows", "details", "timelines", "stats", "tl", "layouts"};
    单条计算失败不影响其余条目(逐条兜底, 与旧版逐条 try 口径一致)。
    """
    rows: dict[int, dict] = {}
    details: dict[int, dict] = {}
    timelines: dict[int, list] = {}
    stats: dict[str, tuple[str, str]] = {}
    tls: dict[str, dict | None] = {}
    layouts: dict[str, dict] = {}
    for night in nights:
        for run in night.runs:
            try:
                rows[id(run)] = _run_row_data(run, guide_map,
                                              guide_pending=guide_pending)
            except Exception:
                rows[id(run)] = {
                    "time": "", "level": "info", "mark": "·",
                    "name": run.target, "plan": "", "sub": _("统计失败")}
            try:
                details[id(run)] = _run_detail(run, guide_map, fits_map,
                                               guide_pending=guide_pending,
                                               fits_pending=fits_pending)
            except Exception as ex:
                details[id(run)] = {
                    "badges": [], "coord": "",
                    "pairs": [{"k": _("详情"), "v": _("计算失败: {ex}").format(ex=ex),
                               "level": "err"}]}
            try:
                timelines[id(run)] = _timeline_items(run)
            except Exception:
                timelines[id(run)] = []
        try:
            stats[night.date] = _night_summary(night, guide_map, fits_map,
                                               guide_pending=guide_pending,
                                               fits_pending=fits_pending)
        except Exception:
            stats[night.date] = (_("统计计算失败"), "")
        try:
            tls[night.date] = _night_timeline(night, phd2_logs, spans)
        except Exception:
            tls[night.date] = None
        try:
            layouts[night.date] = _night_layouts(night)
        except Exception:
            layouts[night.date] = {"flat": [], "grouped": []}
    return {"rows": rows, "details": details, "timelines": timelines,
            "stats": stats, "tl": tls, "layouts": layouts}


def _preview_status_line(n_nights: int, complete: bool,
                         guide_pending: bool) -> str:
    """第一段(缓存首屏)的状态栏文案:说清**已出什么**与**还在补什么**。"""
    head = _("{n_nights} 个夜次(缓存)").format(n_nights=n_nights)
    if not complete:
        head = _("{n_nights} 个夜次(缓存, 部分日志尚未解析)").format(n_nights=n_nights)
    tail = _("正在补全导星与设备信息…") if guide_pending else _("正在补全设备信息…")
    return head + " · " + tail


# `frame_type_cn` 曾经在这里 —— 新前端手拼详情徽章时加的第二套帧型中文表。
# 详情改回复用 `_run_detail` 之后它没有调用者了,一并删掉:同一个 sentinel
# 两套说法("变曝光" / "未知")本来就是分叉的开始。帧型中文的唯一真源是
# 上面的 `_FRAME_TYPE_CN`,两套前端共用。
