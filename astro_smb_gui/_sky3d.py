"""3D 天球页:WebView2 + three.js(GPU)渲染的可自由旋转天球。

**为什么另起一页而不是改拍摄记录页的天球图**:那张图是 CPU 逐像素重投影
(skymap.py),换一个时刻就要重算整幅位图,拖动必然卡。这里把渲染交给
WebView2 里的 WebGL —— 拖动只改相机矩阵,底图纹理一次上传常驻显存。

分工
----
- ``webhost.py``:资产准备(three.js 下载缓存 + 页面文件覆盖)与 WebView2 宿主。
- ``web/sky3d.js``:三维场景(银道底图球 + 赤道网格 + 目标标记 + 地平线)。
- 本文件:数据(夜次/目标/坐标/站点)、控件、消息编排。

线程模型(§6.2 铁律)
--------------------
- 资产准备(可能下载 1.3MB three.js)在 ``sky3d-assets`` 工作线程;
- 日志读取 + FITS 头实测坐标在 ``sky3d-load`` 工作线程(各持 ``client.clone()``),
  算好纯数据后经 ``shell.ui`` 编组;
- UI 线程只做控件赋值与 ``host.post``(小 JSON 字符串),**零 SMB / 零磁盘 /
  零重计算**;时刻滑杆每档只做 目标数×一次 altaz 三角运算。

坐标口径与拍摄记录页一致:FITS 头实测 RA/DEC 优先(角秒级),取不到才回退
日志里的 slew 坐标(goto 请求值);纯偏置/暗场 run 的坐标是停机位,不上天球。
"""
from __future__ import annotations

import asyncio
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from win32more import asyncui
from win32more.Microsoft.UI.Text import FontWeights
from win32more.Microsoft.UI.Xaml import (
    CornerRadius,
    FrameworkElement,
    GridLength,
    GridUnitType,
    TextTrimming,
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
    ComboBox,
    ComboBoxItem,
    FontIcon,
    Grid,
    Orientation,
    ProgressBar,
    ProgressRing,
    RowDefinition,
    Slider,
    StackPanel,
    TextBlock,
    TextBox,
    ToggleSwitch,
)
from win32more.Microsoft.UI.Xaml.Markup import XamlReader
from win32more.Microsoft.UI.Xaml.Media import FontFamily, SolidColorBrush
from win32more.Microsoft.UI.Xaml.Shapes import Rectangle
from win32more.Windows.UI import Color

from astro_smb import astro
from astro_smb.client import SmbClientError
from astro_smb.i18n import N_, gettext as _
from astro_smb_gui import webhost
from astro_smb_gui.logstore import load_site, save_site
from astro_smb_gui.preview import (
    cache_dir,
    cache_key,
    clear_cache,
    download_cached,
    read_fits_header,
)

#: 详情行的**身份**。产出与消费都在本文件里,但那几处比较原来比的是
#: 显示文本 —— 一翻译,"高度"行就不再被记下引用,时刻滑杆拖动时它不再
#: 就地更新(而且不报错)。元组里存 msgid,`lab.Text = _(k)` 时才翻。
_ROW_ALT = N_("高度")
_ROW_AZ = N_("方位")
#: 覆盖统计拿不到 wcsapps 时的 source 值(同样是身份,不是文案)
_COV_NA = N_("不可用")

XAML_PATH = Path(__file__).with_name("sky3d.xaml")

# 注:实际共享名要走 shell.data_share —— 本地卡的共享名是卷标,不是这个。
# 这里只留作 shell 不可用时的兜底缺省。
PLAN_SHARE = "EMMC Images"
PLAN_LIGHT_DIR = "Plan\\Light"

PAGE_FILE = "sky3d.html"

# 署名:底图 CC BY 4.0 必须常显(没贴图时就不提底图);three.js MIT 一并注明
CREDIT = N_("底图: ESO/S. Brunier — GigaGalaxy Zoom (CC BY 4.0) · 渲染: three.js r160 (MIT)")
CREDIT_NO_SURVEY = N_("渲染: three.js r160 (MIT)")

# 目标配色与「夜次 → 目标」的归并逻辑已下沉到共享层,两套前端消费同一份。
# 见 docs/architecture/frontend.md 的逃生口变更表。
from astro_smb_app.views.sky3d import (      # noqa: E402
    TARGET_COLORS,
    _as_float,
    _build_nights,
    _fits_coords,
)

# 语义色(与浏览页详情卡片同一套)
_TONE_RGB = {
    "good": (0x3F, 0xA9, 0x55),
    "warn": (0xD0, 0x8A, 0x00),
    "bad": (0xD9, 0x4A, 0x4A),
    "dim": (0x8A, 0x8A, 0x8A),
}

# 徽章配色(浅底深字,两主题下均可读)
_BADGE_RGB = {
    "night": ((0xD7, 0xE8, 0xFA), (0x0D, 0x47, 0xA1)),
    "count": ((0xDC, 0xEF, 0xDC), (0x1B, 0x5E, 0x20)),
    "gpu": ((0xE4, 0xDD, 0xF2), (0x4A, 0x33, 0x82)),
    "warn": ((0xFB, 0xE7, 0xC6), (0x7A, 0x52, 0x00)),
}

# 时刻滑杆档位数(整夜均分;1000 档在 10 小时的夜上约 36 秒/档)
SLIDER_STEPS = 1000
# 时刻变化小于该秒数不重发给页面(滑杆一次拖动会触发上百次事件)
POST_BUCKET_S = 20.0
# 页面握手超时:超过这么久还没收到 ready 就换成可重试的明确提示
READY_TIMEOUT_S = 12.0

# 分区小标题图标(Segoe MDL2 私用区码位, **都是 BMP** —— 见 §7.1 emoji 截断坑)
GLYPH_TARGET = ""     # Location
GLYPH_DETAIL = ""     # Info


def _brush(r: int, g: int, b: int, a: int = 255) -> SolidColorBrush:
    c = Color()
    c.A, c.R, c.G, c.B = a, r, g, b
    return SolidColorBrush(c)


def _corner(r: float) -> CornerRadius:
    cr = CornerRadius()
    cr.TopLeft = cr.TopRight = cr.BottomLeft = cr.BottomRight = r
    return cr


def _hex_brush(text: str) -> SolidColorBrush:
    """'#RRGGBB' → 画刷(解析失败给中性灰)。"""
    try:
        v = text.lstrip("#")
        return _brush(int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16))
    except (ValueError, IndexError):
        return _brush(0x9E, 0x9E, 0x9E)


def _alt_tone(alt: float | None) -> str:
    """高度角 → 语义色键:地平线下=灰,<30°=琥珀(大气消光重),否则绿。"""
    if alt is None:
        return "dim"
    if alt < 0:
        return "dim"
    if alt < 30:
        return "warn"
    return "good"


def _az_name(az: float) -> str:
    names = [_("北"), _("东北"), _("东"), _("东南"), _("南"), _("西南"), _("西"), _("西北")]
    return names[int((az % 360.0) / 45.0 + 0.5) % 8]


def _fmt_dur(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} h"
    if seconds >= 60:
        return f"{seconds / 60:.0f} min"
    return f"{seconds:.0f} s"


def _fmt_area(deg2: float) -> str:
    """平方度 / 平方角分(小视场用平方度会全是 0.0x,读不出差别)。"""
    if deg2 >= 1.0:
        return _("{deg2:.2f} 平方度").format(deg2=deg2)
    return _("{0:.0f} 平方角分").format(deg2 * 3600.0)


def _fmt_sep(deg: float) -> str:
    """角距:度 / 角分 / 角秒自动换档(′″ 都是 BMP 字符)。"""
    if deg >= 1.0:
        return f"{deg:.2f}°"
    if deg >= 1.0 / 60.0:
        return f"{deg * 60.0:.1f}′"
    return f"{deg * 3600.0:.0f}″"


def _fmt_drift(v: float) -> str:
    return f"{v:+.3f} °/h" if abs(v) < 1.0 else f"{v:+.2f} °/h"


def _fmt_lat(v: float) -> str:
    return f"{abs(v):.2f}°{'N' if v >= 0 else 'S'}"


def _fmt_lon(v: float) -> str:
    return f"{abs(v):.2f}°{'E' if v >= 0 else 'W'}"


from astro_smb_app.views.records import _sky_relevant  # noqa: E402,F401
from astro_smb_gui._xamli18n import load_text as _xaml_text


def _time_window_for(night: dict, target: dict | None = None
                     ) -> tuple[float, float]:
    """顶部时刻轴范围；选中目标时只覆盖它实际拍摄的首尾时刻。"""
    if target is None:
        lo, hi = float(night["ts0"]), float(night["ts1"])
    else:
        lo, hi = float(target["ts0"]), float(target["ts1"])
    if hi <= lo:
        hi = lo + 1.0
    return lo, hi


def _quality_target_for_run(run) -> dict:
    """单条拍摄记录 → 可直接交给足迹/质量后台的目标快照。"""
    ra = astro.ra_str_to_deg(run.ra)
    dec = astro.dec_str_to_deg(run.dec)
    if ra is None or dec is None:
        raise ValueError(_("该拍摄记录没有可用的目标坐标"))
    blocks = list(getattr(run, "blocks", []) or [])
    if blocks:
        frames = [
            frame for block in blocks for group in block.groups
            if group.frame_type in ("light", None) for frame in group.frames
        ]
    else:
        frames = run.all_frames()
    if not frames:
        raise ValueError(_("该记录没有亮场拍摄结果，无法倒推导星质量"))
    span = run.frame_span()
    t0 = span[0] if span else run.begin_time
    t1 = span[1] if span else (run.end_time or run.begin_time)
    return {
        "name": run.target, "ra": ra, "dec": dec,
        "log_ra": ra, "log_dec": dec, "source": _("日志坐标"),
        "frames": len(frames),
        "exposure": sum(float(f.exposure_s) for f in frames),
        "t0": t0, "t1": t1, "ts0": t0.timestamp(), "ts1": t1.timestamp(),
        "plans": ([run.plan_no] if run.plan_no is not None else []),
        "runs": [run], "color": TARGET_COLORS[0],
    }


def _row_key(t: dict) -> tuple:
    """目标行的内容指纹:名字 + 一切会写进行内文字的字段。

    只用于**控件复用**(避开 win32more 事件永久泄漏)。刻意不含 alt/方位这类
    随时刻变化的量 —— 它们由 _refresh_alt 原地更新,不需要重建控件。
    """
    return (t.get("name"), t.get("sub"), t.get("frames"), t.get("exposure"),
            t.get("ra"), t.get("dec"), t.get("type"))


# ==================================================================== 足迹
#
# "足迹" = 一张 sub **真实覆盖**的天区四边形(而不是日志里那个 goto 请求点)。
# 数据来源两条,按开销从低到高:
#   ① FITS 头里已有的 WCS 卡片(ASIAIR 自己解算后回写,实测是 RA---TAN-SIP)
#      —— 只读几 KB 头,零额外开销;
#   ② 本机板解算(astro_smb.platesolve)—— 要把整张 50MB 原图拉下来,
#      一张几秒,**必须在工作线程且可取消**。
# 两条都进 metacache(源指纹 = size+mtime),重开秒出。


FOOT_KIND = "sky3dwcs"          # metacache 数据种类名
FOOT_CACHE_V = 3                # payload 版本(改结构必须 +1,否则读到旧结构)
FOOT_EDGE_STEPS = 6             # 图幅每条边的细分段数(见 _footprint_ring)
MAX_SUBS_PER_TARGET = 12        # 每个目标最多画几张(全画既慢又糊成一片)
MAX_SOLVE_PER_RUN = 8           # 单轮最多板解算几张(每张要拉 50MB)
FOOT_FAIL_TTL_S = 7 * 86400     # 失败结果的缓存寿命(星表/算法更新后该重试)

# 足迹相关的图标(Segoe MDL2 私用区码位,**都是 BMP** —— 见 §7.1 截断坑)
GLYPH_COVER = ""          # ViewAll
GLYPH_SUB = ""            # Photo


def _footprint_ring(w, width: int, height: int,
                    steps: int = FOOT_EDGE_STEPS) -> list[float]:
    """图幅边界 → 球面闭合环,扁平 ``[ra0, dec0, ra1, dec1, ...]``(度)。

    **为什么在像素空间等分就等于"按大圆细分边"**:TAN(gnomonic)把大圆映成切平面
    上的直线,而"像素 → 切平面 (ξ, η)"是 CD 矩阵这个**线性**映射 —— 所以像素平面上
    一条直边上的每一点都落在**同一条大圆**上。沿四条边在像素空间等分取样、再逐点
    ``pixel_to_world``,拿到的就是大圆上的精确采样点。

    跨 RA=0 与近极点为什么不用特判:这里**从不对角度做算术**,JS 侧把每个
    ``(ra, dec)`` 独立变成单位向量。真正会出事的是"在 (ra, dec) 上线性插值"那种写法
    —— 实测(见 tests)常规 2° 视场就偏 16″,赤纬 89.4° 时偏到 3468″(将近 1°)。
    **本函数的返回值里 RA 是允许从 359.x 跳到 0.x 的,调用方不许去 unwrap 它。**

    还要细分的理由(诚实交代):相机就在天球球心,从球心看时**弦和大圆弧投影成同一条
    屏幕直线**,所以就渲染效果而言只给 4 个角点也够。细分是廉价保险(12 张 0.7 ms):
    ① 每段控制在 90° 以内,弦的走向永不含糊;② 将来若加"从球外看"的视角,4 个角点
    连出来的平面四边形会明显切进球里,细分后误差按段长平方衰减。

    图幅在 FITS 约定下占 ``[0.5, width+0.5] × [0.5, height+0.5]``(像素中心为整数,
    外边界在半像素处),这里量的正是外边界。
    """
    import numpy as np
    from astro_smb import wcs as _wcs

    n = max(1, int(steps))
    x0, y0 = 0.5, 0.5
    x1, y1 = float(width) + 0.5, float(height) + 0.5
    t = np.arange(n, dtype=np.float64) / n          # 0 .. 1-1/n(不含终点,免重复角点)
    # 逆时针一圈:下边 → 右边 → 上边 → 左边
    xs = np.concatenate([x0 + (x1 - x0) * t, np.full(n, x1),
                         x1 - (x1 - x0) * t, np.full(n, x0)])
    ys = np.concatenate([np.full(n, y0), y0 + (y1 - y0) * t,
                         np.full(n, y1), y1 - (y1 - y0) * t])
    ra, dec = _wcs.pixel_to_world(w, xs, ys)
    out: list[float] = []
    for a, d in zip(np.atleast_1d(ra), np.atleast_1d(dec)):
        out.append(round(float(a) % 360.0, 5))
        out.append(round(float(d), 5))
    return out


def _frame_center(w, width: int, height: int) -> tuple[float, float]:
    """图幅几何中心的天球坐标(度)。"""
    from astro_smb import wcs as _wcs

    ra, dec = _wcs.pixel_to_world(w, (float(width) + 1.0) / 2.0,
                                  (float(height) + 1.0) / 2.0)
    return float(ra) % 360.0, float(dec)


def _entry_time(entry) -> float:
    """一张 sub 的时刻(unix 秒)。

    优先用**文件名时间戳**(设备本地时间的曝光结束时刻,与日志同源);解析不出
    才退回 mtime。日志里的夜次时段也是设备本地时间,两者对得上的前提是
    PC 与 ASIAIR 同时区(§4.6 已有的同一条假设)。
    """
    from astro_smb.naming import parse_image_name

    parsed = parse_image_name(entry.name)
    if parsed is not None and parsed.time is not None:
        try:
            return parsed.time.timestamp()
        except (OverflowError, OSError, ValueError):
            pass
    return float(entry.mtime or 0.0)


def _pick_subs(entries, ts0: float, ts1: float,
               limit: int = MAX_SUBS_PER_TARGET) -> list:
    """从目标目录里挑出该夜的 sub,并**均匀抽稀**到 limit 张。

    ``Plan\\Light\\<目标>\\`` 是**跨夜累积**的(同一个目标拍了三晚就三晚的帧都在
    里面),所以必须按时刻窗口过滤,否则昨夜的足迹会画到今夜上。

    抽稀取**首尾 + 均匀间隔**而不是"前 N 张":足迹的价值一半在看整夜的漂移,
    只取开头几张正好把漂移信息全丢掉。
    """
    lo, hi = min(ts0, ts1) - 120.0, max(ts0, ts1) + 120.0
    cand = []
    for e in entries:
        if e.is_dir:
            continue
        low = e.name.lower()
        if low.endswith("_thn.jpg") or not low.endswith((".fit", ".fits", ".fts")):
            continue
        ts = _entry_time(e)
        if lo <= ts <= hi:
            cand.append((ts, e))
    cand.sort(key=lambda p: p[0])
    n = len(cand)
    if limit <= 1:
        return [cand[0][1]] if cand else []
    if n <= limit:
        return [e for _ts, e in cand]
    idx = [round(i * (n - 1) / (limit - 1)) for i in range(limit)]
    seen: set[int] = set()
    out = []
    for i in idx:
        if i not in seen:
            seen.add(i)
            out.append(cand[i][1])
    return out


def _wcs_to_payload(w, width: int, height: int, **extra) -> dict:
    """TanWcs + 图幅尺寸 → 可 JSON 化的 metacache payload。"""
    cd = w.cd
    d = {"v": FOOT_CACHE_V, "ok": True,
         "crval": [float(w.crval[0]), float(w.crval[1])],
         "crpix": [float(w.crpix[0]), float(w.crpix[1])],
         "cd": [float(cd[0][0]), float(cd[0][1]),
                float(cd[1][0]), float(cd[1][1])],
         "w": int(width), "h": int(height)}
    d.update(extra)
    return d


def _wcs_from_payload(d: dict):
    """payload → ``(TanWcs, width, height)``;结构不对/版本不符返回 ``None``。"""
    from astro_smb import wcs as _wcs

    if not isinstance(d, dict) or d.get("v") != FOOT_CACHE_V or not d.get("ok"):
        return None
    try:
        crval = (float(d["crval"][0]), float(d["crval"][1]))
        crpix = (float(d["crpix"][0]), float(d["crpix"][1]))
        cd = [float(v) for v in d["cd"]]
        width, height = int(d["w"]), int(d["h"])
    except (KeyError, TypeError, ValueError, IndexError):
        return None
    if width <= 0 or height <= 0 or len(cd) != 4:
        return None
    try:
        return _wcs.TanWcs(crval, crpix, [[cd[0], cd[1]], [cd[2], cd[3]]]), width, height
    except Exception:       # WcsError 等:坏缓存当未命中
        return None


def _rot_drift(times, rots) -> float | None:
    """位置角漂移速率(度/小时);样本不足或时间跨度太短返回 ``None``。

    先 unwrap 再最小二乘 —— 角度过 360° 会把斜率算成天文数字(0.1°/h 变
    -3600°/h)。跨度 < 5 分钟时不给结论:那点跨度上解算噪声会被除成假的大漂移。
    """
    import numpy as np

    t = np.asarray(list(times), dtype=np.float64)
    r = np.asarray(list(rots), dtype=np.float64)
    good = np.isfinite(t) & np.isfinite(r)
    t, r = t[good], r[good]
    if t.size < 2:
        return None
    span = float(t.max() - t.min())
    if span < 300.0:
        return None
    if _meridian_flip(r):
        return None
    r = np.degrees(np.unwrap(np.radians(r)))
    a = np.vstack([t / 3600.0, np.ones_like(t)]).T
    try:
        slope = np.linalg.lstsq(a, r, rcond=None)[0][0]
    except np.linalg.LinAlgError:
        return None
    return float(slope) if np.isfinite(slope) else None


def _meridian_flip(rots) -> bool:
    """位置角序列里是否有约 180° 的单步跳变(GEM 中天翻转)。"""
    import numpy as np

    r = np.asarray(list(rots), dtype=np.float64)
    r = r[np.isfinite(r)]
    if r.size < 2:
        return False
    jumps = np.abs((np.diff(r) + 180.0) % 360.0 - 180.0)
    return bool(np.any(jumps > 150.0))


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, float(v)))


#: 传给 wcsapps 的鸭子类型:它的 ``_wcs_of`` 认 ``.wcs``,``_size_of`` 认
#: ``.hint.image_size`` —— 这样每帧可以带**各自**的图幅尺寸(比全局
#: ``width=/height=`` 更严谨),而且不用 import 它的 Footprint 类。
class _WcsItem:
    __slots__ = ("wcs", "hint", "center")

    def __init__(self, w, width: int, height: int, ra: float, dec: float):
        from types import SimpleNamespace

        self.wcs = w
        self.hint = SimpleNamespace(image_size=(int(width), int(height)))
        self.center = (float(ra), float(dec))


def _wcsapps_cover(foots: list[dict], target: dict) -> dict | None:
    """用轨道A 的 ``astro_smb.wcsapps`` 算覆盖 / 指向误差 / 场旋。

    它比本页的兜底实现准:面积按 gnomonic 面积元逐格加权(不是"格数×平面格面积"),
    还能识别被拍摄区域**包围**的缺口。任何一步不顺(模块不在、签名又变了、
    足迹散布超过 90° 抛异常)都返回 ``None``,此时 :func:`_local_cover` 会把
    覆盖类的量标成"不可用"(**不换一套算法凑数**),但跨度/场旋/指向误差照常给。

    实际调用到的签名(供主控对齐)::

        wcsapps.coverage(items) -> Coverage
            # items 为鸭子类型: .wcs (TanWcs) + .hint.image_size (w, h)
            # 用到 .union_area_deg2 / .common_area_deg2 / .frame_area_deg2
            #      / .n_gaps / .max_gap_deg2
        wcsapps.pointing_error(solved_centers_n2, (log_ra, log_dec))
            -> PointingError(total_arcsec=...)       # 角秒
        wcsapps.field_rotation(wcs_list, times=unix_seconds)
            -> FieldRotation(.ok / .rate_deg_per_hour / .rms_deg / .span_hours)
    """
    import numpy as np

    try:
        from astro_smb import wcsapps
    except Exception:
        return None
    items = [f for f in foots if f.get("wcs") is not None]
    if not items:
        return None
    try:
        cov = wcsapps.coverage([_WcsItem(f["wcs"], f["w"], f["h"],
                                         f["ra"], f["dec"]) for f in items])
        union = float(cov.union_area_deg2)
        common_area = float(cov.common_area_deg2)
        frame = float(np.median(np.asarray(cov.frame_area_deg2,
                                           dtype=np.float64)))
        out = {
            "n": int(cov.n_frames),
            "area": union,
            "single": frame,
            "common_area": common_area,
            "common": _clamp01(common_area / union) if union > 0 else 0.0,
            "keep": _clamp01(common_area / frame) if frame > 0 else 0.0,
            "n_gaps": int(cov.n_gaps),
            "max_gap": float(cov.max_gap_deg2),
            "source": "wcsapps",
        }
    except Exception:
        return None

    # 指向误差与场旋是**加分项** —— 单独失败不该把整张卡打回兜底实现
    out["point_err"] = _wcsapps_point_err(wcsapps, items, target)
    (out["drift"], out["drift_rms"], out["span_h"],
     out["meridian_flip"]) = _wcsapps_rotation(wcsapps, items)
    return out


def _wcsapps_point_err(wcsapps, items: list[dict], target: dict) -> float | None:
    """解算中心 vs 日志 goto 的角距中位数(**度**;wcsapps 给的是角秒)。"""
    import numpy as np

    lra, ldec = target.get("log_ra"), target.get("log_dec")
    if lra is None or ldec is None:
        return None
    try:
        centers = np.array([[f["ra"], f["dec"]] for f in items],
                           dtype=np.float64)
        err = wcsapps.pointing_error(centers, (float(lra), float(ldec)))
        tot = np.atleast_1d(np.asarray(err.total_arcsec, dtype=np.float64))
        tot = tot[np.isfinite(tot)]
        return float(np.median(tot)) / 3600.0 if tot.size else None
    except Exception:
        return None


def _wcsapps_rotation(wcsapps, items: list[dict]):
    """``(度/小时,残差,跨度,中天翻转)``;算不出来不给漂移结论。"""
    import math

    try:
        rot = wcsapps.field_rotation([f["wcs"] for f in items],
                                     times=[f["ts"] for f in items])
        span_h = float(rot.span_hours)
        flipped = bool(getattr(rot, "meridian_flip", False))
        if flipped:
            return None, None, max(0.0, span_h), True
        # 与 _rot_drift 同一条规矩:跨度 < 5 分钟不给结论(噪声会被除成假漂移)
        if not rot.ok or span_h * 3600.0 < 300.0:
            return (None, None,
                    max(0.0, span_h if math.isfinite(span_h) else 0.0), False)
        rate = float(rot.rate_deg_per_hour)
        rms = float(rot.rms_deg)
        return (rate if math.isfinite(rate) else None,
                rms if math.isfinite(rms) else None, span_h, False)
    except Exception:
        return None, None, 0.0, False


def _build_foot(t: dict, ent, payload: dict) -> dict | None:
    """(目标, 目录项, WCS payload) → 一条足迹记录(工作线程调用)。"""
    res = _wcs_from_payload(payload)
    if res is None:
        return None
    w, width, height = res
    try:
        ring = _footprint_ring(w, width, height)
        ra, dec = _frame_center(w, width, height)
        fov = w.fov_deg(width, height)
    except Exception:       # 退化的 CD / 极点附近的数值意外:宁可少画一张
        return None
    zwo = None
    try:
        from astro_smb.platesolve import zwo_angle_from_cd

        zwo = float(zwo_angle_from_cd(w.cd))
    except Exception:
        pass
    return {"id": f"{ent.share}|{ent.path}", "target": t.get("name", ""),
            "color": t.get("color", TARGET_COLORS[0]),
            "file": ent.name, "share": ent.share, "path": ent.path,
            "ts": _entry_time(ent), "ring": ring,
            "ra": ra, "dec": dec, "w": width, "h": height,
            "rot": float(w.rotation_deg()), "zwo": zwo,
            "scale": float(w.pixel_scale()), "fov": fov,
            "flip": bool(w.flipped()),
            "src": payload.get("src", ""), "sip": bool(payload.get("sip")),
            "nmatch": payload.get("nmatch"), "rms": payload.get("rms"),
            "focal": payload.get("focal"),
            "star_fwhm_px": payload.get("star_fwhm_px"),
            "star_fwhm_arcsec": payload.get("star_fwhm_arcsec"),
            "star_ellipticity": payload.get("star_ellipticity"),
            "star_theta_deg": payload.get("star_theta_deg"),
            "star_theta_r": payload.get("star_theta_r"),
            "wcs": w}


def _cover_for(foots: list[dict], target: dict) -> dict:
    """一个目标的「覆盖」卡数据(工作线程调用:网格统计是重活)。

    优先走轨道A 的 ``wcsapps``,不可用时回退本页估算。**两条路径产出同一组键、
    同一套口径** —— 同一栏在不同机器上换个定义,用户没法比较。
    """
    cov = _wcsapps_cover(foots, target)
    if cov is None:
        cov = _local_cover(foots, target)
    cov = dict(cov)
    cov["n"] = cov.get("n") or len(foots)
    cov["n_solved"] = sum(1 for f in foots if f.get("src") == "solve")
    cov["n_hdr"] = sum(1 for f in foots if f.get("src") == "hdr")
    cov["n_sip"] = sum(1 for f in foots if f.get("sip"))
    rms = sorted(float(f["rms"]) for f in foots
                 if isinstance(f.get("rms"), (int, float))
                 and f["rms"] == f["rms"])        # 顺手滤掉 NaN
    cov["rms_med"] = rms[len(rms) // 2] if rms else None
    return cov


def _dither_events(host: str, phd2_logs) -> list:
    """从日志磁盘缓存读取 dither 指令；只在足迹工作线程调用。"""
    from astro_smb_gui.logstore import logs_cache_dir
    from astro_smb.guidecheck import dither_from_log_text

    out = []
    for log in phd2_logs:
        source = getattr(log, "source", "")
        if not source:
            continue
        try:
            text = (logs_cache_dir(host) / source).read_text(
                encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        out.extend(dither_from_log_text(text))
    out.sort(key=lambda e: e.time)
    return out


def _night_polar(by_target: dict[str, list[dict]], lat_deg: float,
                 lon_deg: float):
    """**当夜所有目标联合**反解极轴误差。

    单个目标只给 2 个方程解 2 个未知数 —— 残差恒为机器零,模型错了也照样
    "完美拟合"(掺入非极轴分量实测反解错 65%,残差仍是 4e-16)。所以按目标
    分别算出来的那个极轴数字**推翻不了**,只能当量级参考。

    把同一夜各目标的漂移速率凑成多组样本联合反解,残差才第一次有意义:
    残差小 = 单一极轴误差解释得通;残差大 = 现场还有别的机制。

    真机(2026-07-30,NGC 253 + NGC 7293)实测:各自 1.48′/1.43′,联合 1.45′,
    残差只占观测漂移的 3%。返回 ``(PolarCheck, 参与的目标名列表)``;
    可用样本不足 2 组时返回 ``(None, [])``。
    """
    import numpy as np
    from astro_smb import astro
    from astro_smb.guidecheck import fit_center_drift, polar_from_runs

    samples, names = [], []
    for name, items in sorted(by_target.items()):
        rows = sorted(items, key=lambda f: f["ts"])
        if len(rows) < 3:               # 少于 3 张拟合不出可信的漂移速率
            continue
        times = [datetime.fromtimestamp(float(r["ts"])) for r in rows]
        ra = np.array([float(r["ra"]) for r in rows])
        dec = np.array([float(r["dec"]) for r in rows])
        fit = fit_center_drift(times, ra, dec)
        mid_ts = 0.5 * (float(rows[0]["ts"]) + float(rows[-1]["ts"]))
        mid_ra = float(np.median(ra))
        ha = ((astro.lst_deg(mid_ts, lon_deg) - mid_ra + 180.0) % 360.0) - 180.0
        samples.append((ha, float(np.median(dec)), fit.ra_rate, fit.dec_rate))
        names.append(name)
    if len(samples) < 2:
        return None, []
    try:
        return polar_from_runs(samples, lat_deg), names
    except ValueError:                  # 反解出的量超出小角近似,不给数
        return None, []


def _apply_night_polar(qualities: dict, check, names: list[str]) -> None:
    """把夜次级的联合反解结果盖到各目标的判读上。

    联合结果**严格优于**单目标那个恰定解,所以直接替换 ``polar``/``polar_cond``,
    并把"恰定、看不出对错"那条告白换成真正有信息量的结论。
    """
    if check is None:
        return
    note = (_("本夜 {0} 个目标({1})联合反解极轴偏差 {total_arcmin:.2f}′,残差 {rms:.3f}″/分").format(
        len(names), _("、").join(names), total_arcmin=check.polar.total_arcmin, rms=check.rms))
    if check.degenerate:
        note += _("(条件数 {cond:.0f},方位/高度分不开,只能看总量)").format(cond=check.cond)
    elif check.explained:
        note += _(" —— 残差很小,单一极轴误差解释得通这几个目标的漂移")
    else:
        note += (_(" —— **残差偏大**,单一极轴误差解释不了这几个目标的漂移,现场还有别的机制(挠曲、组件转动等)"))
    for quality in qualities.values():
        if quality is None:
            continue
        quality.polar, quality.polar_cond = check.polar, check.cond
        # 联合反解跨了多个目标 ⇒ 有残差可看 ⇒ 这个数字终于推翻得了
        quality.polar_falsifiable = True
        # 按相等剔除,不搜关键词(见 guidequality 里同一处的说明)
        # **别叫 note** —— 这个函数里 `note` 装的是上面刚拼好的"新结论",
        # 同名会把它盖掉,于是剔除了告白又把告白原样加回来。
        stale = getattr(quality, "polar_exact_note", "")
        quality.findings = [f for f in quality.findings
                            if not stale or f != stale]
        quality.findings.append(note)


def _quality_for(foots: list[dict], target: dict, phd2_logs, dither,
                 lat_deg: float, lon_deg: float):
    """足迹 + 同期 PHD2 + 星点形状 → guidecheck 三证据交叉判读。"""
    import math
    import numpy as np
    from astro_smb.guidecheck import FrameEvidence, cross_validate
    from astro_smb.phd2log import guide_coverage, rms_for_interval

    if len(foots) < 2:
        return None
    ordered_foots = sorted(foots, key=lambda f: f["ts"])
    shots = [shot for run in target.get("runs", [])
             for shot in run.all_frames()]
    evidences = []
    for foot in ordered_foots:
        end = datetime.fromtimestamp(float(foot["ts"]))
        shot = min(shots, key=lambda s: abs((s.end_time - end).total_seconds()),
                   default=None)
        if shot is not None:
            tolerance = max(120.0, shot.exposure_s + 60.0)
            if abs((shot.end_time - end).total_seconds()) > tolerance:
                shot = None
        if shot is None:
            avg = float(target.get("exposure") or 0.0) / max(
                1, int(target.get("frames") or 1))
            t1 = end
            t0 = end - timedelta(seconds=max(0.1, avg))
        else:
            t0, t1 = shot.time, shot.end_time
        stats = rms_for_interval(phd2_logs, t0, t1)
        guide_rms = (stats.rms_total if stats is not None and stats.in_arcsec
                     else None)
        def finite(name):
            value = foot.get(name)
            return (float(value) if isinstance(value, (int, float))
                    and math.isfinite(float(value)) else None)
        evidences.append(FrameEvidence(
            t0=t0, t1=t1,
            fwhm_px=finite("star_fwhm_px"),
            fwhm_arcsec=finite("star_fwhm_arcsec"),
            ellipticity=finite("star_ellipticity"),
            theta_deg=finite("star_theta_deg"),
            theta_r=finite("star_theta_r"),
            n_stars=int(foot.get("nmatch") or 0),
            center_ra=float(foot["ra"]), center_dec=float(foot["dec"]),
            pa_deg=float(foot["rot"]),
            guide_rms_arcsec=guide_rms,
            guide_coverage=guide_coverage(phd2_logs, t0, t1)))
    scales = [float(f["scale"]) for f in foots
              if isinstance(f.get("scale"), (int, float))
              and float(f["scale"]) > 0]
    main_scale = float(np.median(scales)) if scales else 0.0
    main_focal = [float(f["focal"]) for f in foots
                  if isinstance(f.get("focal"), (int, float))
                  and float(f["focal"]) > 0]
    guide_focal = [
        float(sec.focal_len) for log in phd2_logs for sec in log.guide_sections
        if sec.focal_len is not None and sec.focal_len > 0
        and sec.end_time_effective >= evidences[0].t0
        and sec.begins <= evidences[-1].t1
    ]
    is_oag = None
    if main_focal and guide_focal:
        mf = float(np.median(main_focal))
        gf = float(np.median(guide_focal))
        is_oag = abs(mf - gf) / mf <= 0.05
    mid_ts = 0.5 * (ordered_foots[0]["ts"] + ordered_foots[-1]["ts"])
    mid_ra = float(np.median([f["ra"] for f in foots]))
    ha = ((astro.lst_deg(mid_ts, lon_deg) - mid_ra + 180.0) % 360.0) - 180.0
    return cross_validate(
        evidences, pixel_scale_main=main_scale, dither=dither,
        is_oag=is_oag, lat_deg=lat_deg, ha_deg=ha)


def _local_cover(foots: list[dict], target: dict) -> dict:
    """`wcsapps` 算不出覆盖时的降级结果。

    **覆盖面积一律不给** —— 这里曾经有一套自己的网格估算(`_coverage_stats`),
    与 `wcsapps.coverage` 并存、喂同一个 UI 栏位,靠人工纪律维持口径一致:
    它按"格数 × 平面格面积"算,而 wcsapps 按 gnomonic 面积元
    ``1/(1+ξ²+η²)^1.5`` 逐格加权,离中心几度外就会分道扬镳。同一栏在不同机器上
    悄悄换定义,用户根本没法比较 —— 宁可显示"不可用"。

    跨度 / 场旋 / 中天翻转 / 指向误差**不依赖 wcsapps**,照常算。
    """
    ts = [f["ts"] for f in foots]
    cov: dict = {
        "source": _COV_NA,
        "n": len(foots),
        # 覆盖类的量只有 wcsapps 能给,拿不到就是拿不到
        "area": None, "single": None, "common_area": None,
        "common": None, "keep": None, "n_gaps": None, "max_gap": None,
    }
    cov["span_h"] = (max(ts) - min(ts)) / 3600.0 if len(ts) > 1 else 0.0
    cov["drift"] = _rot_drift(ts, [f["rot"] for f in foots])
    cov["drift_rms"] = None
    cov["meridian_flip"] = _meridian_flip([f["rot"] for f in foots])
    # 指向误差:解算/实测中心 vs 日志里的 goto 请求值。两者本就不该相等 ——
    # 这一栏量的正是"望远镜实际停在哪儿离你要去的地方多远"。
    err = None
    lra, ldec = target.get("log_ra"), target.get("log_dec")
    if lra is not None and ldec is not None and foots:
        try:
            import numpy as np
            from astro_smb.wcs import angular_separation

            sep = angular_separation(
                np.array([f["ra"] for f in foots], dtype=np.float64),
                np.array([f["dec"] for f in foots], dtype=np.float64),
                float(lra), float(ldec))
            sep = np.atleast_1d(np.asarray(sep, dtype=np.float64))
            sep = sep[np.isfinite(sep)]
            if sep.size:
                err = float(np.median(sep))
        except Exception:
            err = None
    cov["point_err"] = err
    return cov


def _foot_note(n_ok: int, total: int, need_cat: int, pending: int,
               failed: int, cat_ok: bool) -> str:
    """一行说明这轮解出了多少、还差多少、为什么差(工作线程调用,纯字符串)。"""
    parts = [_("实际视场: {n_ok}/{total} 张").format(n_ok=n_ok, total=total)]
    if need_cat:
        parts.append(_("{need_cat} 张需要星表才能解算").format(need_cat=need_cat))
    if pending:
        parts.append(_("{pending} 张排队中(再点「刷新」继续解算)").format(pending=pending))
    if failed:
        parts.append(_("{failed} 张解算失败").format(failed=failed))
    if n_ok == 0 and not need_cat and not failed:
        parts.append(_("这些原图头里没有 WCS"))
    return "  ·  ".join(parts)


class Sky3DPage:
    """契约与其它页一致:``__init__(shell)`` / ``root`` / ``on_show()`` /
    ``on_connected(shares)`` / ``on_close()``。"""

    def __init__(self, shell) -> None:
        self.shell = shell
        self.root = XamlReader.Load(_xaml_text(XAML_PATH)).as_(FrameworkElement)
        self._find_controls()

        # 复用画刷/字体(每次新建 SolidColorBrush 很浪费)
        self._tone = {k: _brush(*v) for k, v in _TONE_RGB.items()}
        self._badge = {k: (_brush(*bg), _brush(*fg))
                       for k, (bg, fg) in _BADGE_RGB.items()}
        self._pill_bg = _brush(0x80, 0x80, 0x80, 0x28)
        self._divider = _brush(0x80, 0x80, 0x80, 0x3C)
        self._track_bg = _brush(0x80, 0x80, 0x80, 0x38)
        self._row_hover = _brush(0x80, 0x80, 0x80, 0x28)
        self._row_sel = _brush(0x4F, 0x8A, 0xC7, 0x3C)
        self._transparent = _brush(0, 0, 0, 0)
        self._mono = FontFamily("Consolas")

        # WebView2 宿主:element 可能为 None(控件都建不出来时走降级面板)
        self.host = webhost.WebHost(PAGE_FILE,
                                    on_message=self._on_web_message,
                                    on_error=self._on_web_error)
        if self.host.element is not None:
            self.view_host.Children.InsertAt(0, self.host.element)

        # 状态
        self._assets_dir: Path | None = None
        self._booting = False
        self._boot_done = False
        self._page_ready = False    # 收到过页面的 ready 握手
        self._ready_gen = 0         # 握手看门狗代次(重试后旧的作废)
        self._gen = 0
        self._loading = False
        self._nights: list[dict] = []
        self._night_idx = -1
        self._targets: list[dict] = []
        self._rows: dict[str, dict] = {}
        self._sel_name: str | None = None
        self._hover_name: str | None = None
        # 详情卡里随时刻变化的两行(引用持久化, 拖滑杆只改文本不重建)
        self._detail_alt: TextBlock | None = None
        self._detail_alt_note: TextBlock | None = None
        self._detail_az: TextBlock | None = None
        self._fits_cache: dict[tuple, tuple[float, float] | None] = {}
        self._combo_sync = False
        self._slider_sync = False
        self._ts: float | None = None
        self._time_ts0: float | None = None
        self._time_ts1: float | None = None
        self._last_post_ts: float | None = None
        self._last_js_error = ""
        self._survey_ok = False

        # 足迹(实际视场)状态
        self._foot_gen = 0
        self._foot_cancel: threading.Event | None = None
        self._foot_busy = False
        self._foots: list[dict] = []
        self._foot_by_id: dict[str, dict] = {}
        self._foot_by_target: dict[str, list[dict]] = {}
        self._cover: dict[str, dict] = {}
        self._sel_foot: str | None = None
        self._foot_note = ""
        self._cat_busy = False
        self._cat_cancel: threading.Event | None = None
        # 拍摄记录页可独立发起质量倒推；每个 run 单飞，关闭应用统一取消。
        self._quality_cancel: dict[int, threading.Event] = {}
        # FITS 缓存下载/板解算会被 3D 足迹与记录页同时调用，必须串行，避免
        # 两个 worker 写同一个本地缓存文件与 metacache 键。
        self._solve_lock = threading.Lock()

        site = load_site()
        self._lat = float(site.get("lat", 30.0))
        self._lon = float(site.get("lon", 120.0))
        self._lon_auto = bool(site.get("lon_auto", True))
        self._lon_estimate: float | None = None

        self._wire()
        self.lat_box.Text = f"{self._lat:.4f}"
        self.lon_text.Text = _fmt_lon(self._lon)
        self._update_credit()

    # ---------- 控件 ----------

    def _find_controls(self) -> None:
        f = self.root.FindName
        self.refresh_btn = f("RefreshBtn").as_(Button)
        self.refresh_ring = f("RefreshRing").as_(ProgressRing)
        self.night_combo = f("NightCombo").as_(ComboBox)
        self.time_slider = f("TimeSlider").as_(Slider)
        self.time_text = f("TimeText").as_(TextBlock)
        self.time_range_text = f("TimeRangeText").as_(TextBlock)
        self.horizon_toggle = f("HorizonToggle").as_(ToggleSwitch)
        self.reset_btn = f("ResetBtn").as_(Button)
        self.view_host = f("ViewHost").as_(Grid)
        self.overlay = f("OverlayPanel").as_(StackPanel)
        self.load_ring = f("LoadRing").as_(ProgressRing)
        self.load_text = f("LoadText").as_(TextBlock)
        self.load_bar = f("LoadBar").as_(ProgressBar)
        self.retry_btn = f("RetryBtn").as_(Button)
        self.badge_row = f("BadgeRow").as_(StackPanel)
        self.card_title = f("CardTitle").as_(TextBlock)
        self.card_sub = f("CardSub").as_(TextBlock)
        self.pill_row = f("PillRow").as_(StackPanel)
        self.target_panel = f("TargetPanel").as_(StackPanel)
        self.detail_grid = f("DetailGrid").as_(Grid)
        self.foot_toggle = f("FootprintToggle").as_(ToggleSwitch)
        self.foot_panel = f("FootPanel").as_(StackPanel)
        self.foot_status = f("FootStatus").as_(TextBlock)
        self.foot_bar = f("FootBar").as_(ProgressBar)
        self.foot_cancel_btn = f("FootCancelBtn").as_(Button)
        self.catalog_panel = f("CatalogPanel").as_(StackPanel)
        self.catalog_text = f("CatalogText").as_(TextBlock)
        self.catalog_bar = f("CatalogBar").as_(ProgressBar)
        self.catalog_btn = f("CatalogBtn").as_(Button)
        self.cover_panel = f("CoverPanel").as_(StackPanel)
        self.sub_panel = f("SubPanel").as_(StackPanel)
        self.lat_box = f("LatBox").as_(TextBox)
        self.lon_text = f("LonText").as_(TextBlock)
        self.site_apply_btn = f("SiteApplyBtn").as_(Button)
        self.status_text = f("StatusText").as_(TextBlock)
        self.credit_text = f("CreditText").as_(TextBlock)

    def _wire(self) -> None:
        self.refresh_btn.Click += self._on_refresh
        self.retry_btn.Click += self._on_retry
        self.reset_btn.Click += self._on_reset_view
        self.night_combo.SelectionChanged += self._on_night_changed
        self.time_slider.ValueChanged += self._on_time_changed
        self.horizon_toggle.Toggled += self._on_horizon_toggled
        self.site_apply_btn.Click += self._on_site_apply
        # 这几个都是 XAML 里的固定控件,**只在这里挂一次**
        # (§ win32more 事件永久泄漏:重建循环里挂事件会一直堆积)
        self.foot_toggle.Toggled += self._on_foot_toggled
        self.foot_cancel_btn.Click += self._on_foot_cancel
        self.catalog_btn.Click += self._on_catalog_download

    # ---------- 页面生命周期 ----------

    def _data_share(self) -> str:
        """存放 log/ 与 Plan/Light/ 的共享名。

        SMB 是 "EMMC Images",**本地卡是卷标** —— 硬编码常量在本地卡上
        必然一无所获,而且是静默退化(listdir 抛错被吞成空集合),
        表现为"实测坐标/设备信息莫名其妙全没了"(审查实证)。
        """
        return getattr(self.shell, "data_share", "") or PLAN_SHARE

    def on_show(self) -> None:
        self._ensure_boot()
        self._sync_survey()
        if not self._nights and not self._loading:
            self._start_load(force=False)

    def on_connected(self, shares) -> None:
        self._start_load(force=False)

    def on_new_logs(self) -> None:
        """watcher 发现新日志(会话刚结束):当前可见就立刻重载。"""
        self._start_load(force=True)

    def on_close(self) -> None:
        self._cancel_footprints()
        if self._cat_cancel is not None:
            self._cat_cancel.set()
        for cancel in list(self._quality_cancel.values()):
            cancel.set()
        try:
            self.host.close()
        except Exception:
            pass

    # ---------- 资产 / WebView2 引导 ----------

    def _ensure_boot(self) -> None:
        if self.host.element is None:
            self._show_overlay(
                _('3D 天球不可用:{failure}\n(拍摄记录页的 2D 天球图不受影响)').format(
                    failure=self.host.failure), retry=False)
            return
        if self._boot_done or self._booting:
            return
        self._booting = True
        self._show_overlay(_("正在准备 3D 天球 …"), ring=True)
        if not webhost.three_ready():
            self.load_bar.Visibility = Visibility.Visible
            self.load_bar.Value = 0
        threading.Thread(target=self._boot_work, daemon=True,
                         name="sky3d-assets").start()

    def _boot_work(self) -> None:
        """工作线程:复制页面资产 + 首次下载 three.js。异常必须落 shell.error。"""
        try:
            d = webhost.ensure_assets(
                progress=lambda stage, done, total:
                    self.shell.ui(self._boot_progress, stage, done, total))
            self.shell.ui(self._boot_ready, d)
        except Exception as ex:
            msg = _("准备 3D 天球资源失败: {__name__}: {ex}").format(
                __name__=type(ex).__name__, ex=ex)
            self.shell.ui(self.shell.error, msg)
            self.shell.ui(self._show_overlay, msg, False, True)
            self.shell.ui(self._boot_reset)

    def _boot_reset(self) -> None:
        self._booting = False

    def _boot_progress(self, stage: str, done: int, total: int) -> None:
        self.load_text.Text = stage
        if total > 0:
            self.load_bar.Visibility = Visibility.Visible
            self.load_bar.Maximum = 100
            self.load_bar.Value = min(100.0, done * 100.0 / total)
        elif done > 0:
            self.load_text.Text = f"{stage}({done / 1024:.0f} KB)"

    def _boot_ready(self, assets_dir: Path) -> None:
        self._assets_dir = assets_dir
        self.load_bar.Visibility = Visibility.Collapsed
        self.load_text.Text = _("正在启动 WebView2 …")
        asyncui.create_task(self._navigate())

    async def _navigate(self) -> None:
        ok = False
        try:
            ok = await self.host.ensure_ready(self._assets_dir)
        except Exception as ex:                 # async 处理器的异常会被吞
            self.host.failure = f"{type(ex).__name__}: {ex}"
        self._booting = False
        if not ok:
            msg = self.host.failure or _("WebView2 初始化失败")
            self._show_overlay(
                _('3D 天球不可用:{msg}\n(拍摄记录页的 2D 天球图不受影响)').format(msg=msg), retry=True)
            self.shell.error(msg)
            return
        self._boot_done = True
        # 页面 JS 起来后会回 {"type":"ready"},那时才推数据(见 _on_web_message)。
        # **但 ready 可能永远不来**:WebGL 不可用时 sky3d.js 直接 throw;
        # three.module.js 404 时 ES module 加载失败连 window.onerror 都不触发
        # (资源错误不冒泡)。没有超时的话遮罩就永久停在"正在启动 WebView2 …",
        # 用户既看不到图也看不到原因(审查实证)。
        self._ready_gen += 1
        asyncui.create_task(self._ready_watchdog(self._ready_gen))

    async def _ready_watchdog(self, gen: int) -> None:
        """等页面握手;超时就把遮罩换成可重试的明确提示。"""
        await asyncio.sleep(READY_TIMEOUT_S)
        if gen != self._ready_gen or self._page_ready:
            return
        self._show_overlay(
            _('3D 天球:页面已加载但迟迟没有就绪。\n常见原因是该机器不支持 WebGL,或 three.js 资产损坏。\n(拍摄记录页的 2D 天球图不受影响)'), retry=True)

    def _show_overlay(self, text: str, ring: bool = False,
                      retry: bool = False) -> None:
        self.load_text.Text = text
        self.load_ring.IsActive = ring
        self.load_bar.Visibility = Visibility.Collapsed
        self.retry_btn.Visibility = (Visibility.Visible if retry
                                     else Visibility.Collapsed)
        self.overlay.Visibility = Visibility.Visible

    def _hide_overlay(self) -> None:
        self.load_ring.IsActive = False
        self.overlay.Visibility = Visibility.Collapsed

    def _on_retry(self, sender, e) -> None:
        self._page_ready = False
        self._boot_done = False
        self._booting = False
        self._ensure_boot()

    # ---------- 与页面通信 ----------

    def _on_web_error(self, message: str) -> None:
        self.shell.error(_("3D 天球: {message}").format(message=message))

    def _on_web_message(self, msg: dict) -> None:
        """WebMessageReceived 已在 UI 线程;这里只做轻量更新。"""
        kind = msg.get("type")
        if kind == "ready":
            self._page_ready = True
            self._hide_overlay()
            self._push_all()
        elif kind == "hover":
            self._set_hover(msg.get("name"))
        elif kind == "pick":
            name = msg.get("name")
            if name:
                self._select_target(name, fly=False)
        elif kind == "footprint":
            fid = msg.get("id")
            if fid:
                self._on_foot_picked(str(fid))
        elif kind == "view":
            self._update_view_status(msg)
        elif kind == "survey":
            self._survey_ok = bool(msg.get("ok"))
            self._update_credit()
            if not self._survey_ok:
                self.status_text.Text = (
                    _("巡天底图加载失败(文件可能损坏)— 当前只显示网格与标记;可在「拍摄记录」页重新开启「巡天底图」下载"))
        elif kind == "error":
            text = str(msg.get("message") or "")
            if text and text != self._last_js_error:
                self._last_js_error = text
                self.shell.error(_("3D 天球页面脚本错误: {text}").format(text=text))

    def _push_all(self) -> None:
        """页面就绪 / 数据变化后把整套状态推给页面。"""
        self._push_survey()
        self._push_targets()
        self._push_footprints()
        self._push_site()
        self._push_initial_view()

    def _push_survey(self) -> None:
        url = webhost.survey_asset_url(self._assets_dir)
        self.host.post({"type": "init", "survey": url})
        self._survey_ok = bool(url)
        self._update_credit()
        if not url:
            self.status_text.Text = (
                _("未启用巡天底图 — 只显示网格与标记;在「拍摄记录」页打开「巡天底图」下载后回到本页即可自动贴图"))

    def _update_credit(self) -> None:
        self.credit_text.Text = _(CREDIT if self._survey_ok
                                  else CREDIT_NO_SURVEY)

    def _sync_survey(self) -> None:
        """底图可能是刚在拍摄记录页下载的:工作线程同步进资产目录后重推。"""
        if self._survey_ok or not self._boot_done:
            return

        def work():
            try:
                url = webhost.refresh_survey(self._assets_dir)
            except Exception:
                url = None
            if url:
                self.shell.ui(self._push_survey)

        threading.Thread(target=work, daemon=True, name="sky3d-survey").start()

    def _push_targets(self) -> None:
        items = [{"name": t["name"], "ra": t["ra"], "dec": t["dec"],
                  "color": t["color"]} for t in self._targets]
        self.host.post({"type": "targets", "items": items})

    def _push_site(self) -> None:
        if self._ts is None:
            self.host.post({"type": "site", "lat": None, "lst": None,
                            "showHorizon": False})
            return
        self.host.post({"type": "site",
                        "lat": self._lat,
                        "lst": astro.lst_deg(self._ts, self._lon),
                        "showHorizon": bool(self.horizon_toggle.IsOn)})
        self._last_post_ts = self._ts

    def _update_view_status(self, msg: dict) -> None:
        try:
            ra = float(msg.get("ra", 0.0))
            dec = float(msg.get("dec", 0.0))
            fov = float(msg.get("fov", 0.0))
        except (TypeError, ValueError):
            return
        self.status_text.Text = (
            _("视场中心 {0} {1}  ·  视场 {fov:.0f}°  ·  拖动旋转 / 滚轮缩放 / 双击回正").format(
                astro.format_ra(ra), astro.format_dec(dec), fov=fov))

    # ---------- 数据加载 ----------

    def _on_refresh(self, sender, e) -> None:
        self._start_load(force=True)

    def _start_load(self, force: bool) -> None:
        if self._loading:
            return
        store = getattr(self.shell, "logstore", None)
        if store is None:
            return
        self._gen += 1
        gen = self._gen
        self._loading = True
        self.refresh_ring.IsActive = True
        self.status_text.Text = _("正在读取拍摄日志 …")
        base_client = self.shell.client
        threading.Thread(target=self._load_work,
                         args=(gen, force, store, base_client),
                         daemon=True, name="sky3d-load").start()

    def _load_work(self, gen: int, force: bool, store, base_client) -> None:
        """工作线程:日志聚合 + 逐目标 FITS 实测坐标 → 纯数据夜次列表。"""
        clone = None
        try:
            data = store.data
            if force or data is None:
                clone = base_client.clone()
                data = store.refresh(clone)

            coords: dict[int, tuple[float, float]] = {}
            try:
                if clone is None:
                    clone = base_client.clone()
                plan_dirs = {e.name for e in
                             clone.listdir(self._data_share(), PLAN_LIGHT_DIR)
                             if e.is_dir}
            except SmbClientError:
                plan_dirs = set()   # 不致命:退回日志坐标
            if plan_dirs:
                coords = self._collect_fits(clone, data, plan_dirs)

            nights = _build_nights(data, coords)
            self.shell.ui(self._apply_data, gen, nights,
                          data.lon_estimate, len(data.errors))
        except SmbClientError as ex:
            self.shell.ui(self._load_failed, gen, str(ex))
        except Exception as ex:     # 工作线程异常不许静默
            self.shell.ui(self._load_failed, gen, f"{type(ex).__name__}: {ex}")
        finally:
            if clone is not None:
                try:
                    clone.close()
                except Exception:
                    pass

    def _collect_fits(self, clone, data,
                      plan_dirs: set[str]) -> dict[int, tuple[float, float]]:
        """工作线程:逐目标读首帧 FITS 头拿实测指向(每目标几 KB)。

        与拍摄记录页同款口径:同名目标只 listdir 一次,头信息按
        (share, path, size, mtime) 缓存;连续 3 次失败视为连接问题放弃本轮。
        """
        out: dict[int, tuple[float, float]] = {}
        first_fit: dict[str, object] = {}
        fails = 0
        for night in data.nights:
            for run in night.runs:
                if fails >= 3:
                    return out
                if run.target not in plan_dirs:
                    continue
                if run.target not in first_fit:
                    try:
                        entries = clone.listdir(
                            self._data_share(), PLAN_LIGHT_DIR + "\\" + run.target)
                        first_fit[run.target] = next(
                            (e for e in entries if not e.is_dir
                             and e.name.lower().endswith(
                                 (".fit", ".fits", ".fts"))), None)
                        fails = 0
                    except SmbClientError:
                        first_fit[run.target] = None
                        fails += 1
                        continue
                ent = first_fit[run.target]
                if ent is None:
                    continue
                key = (ent.share, ent.path, ent.size, ent.mtime)
                if key in self._fits_cache:
                    hit = self._fits_cache[key]
                else:
                    try:
                        hit = _fits_coords(read_fits_header(clone, ent))
                    except SmbClientError:
                        fails += 1
                        continue
                    fails = 0
                    self._fits_cache[key] = hit
                if hit is not None:
                    out[id(run)] = hit
        return out

    def _load_failed(self, gen: int, message: str) -> None:
        if gen != self._gen:
            return
        self._loading = False
        self.refresh_ring.IsActive = False
        self.status_text.Text = _("日志读取失败: {message}").format(message=message)
        self.shell.error(_("3D 天球: 日志读取失败 — {message}").format(message=message))

    def _apply_data(self, gen: int, nights: list[dict],
                    lon_estimate: float | None, n_errors: int) -> None:
        if gen != self._gen:
            return
        self._loading = False
        self.refresh_ring.IsActive = False
        self._nights = nights
        self._lon_estimate = lon_estimate
        if self._lon_auto and lon_estimate is not None:
            self._lon = lon_estimate
        self.lon_text.Text = (
            _fmt_lon(self._lon)
            + (_("(日志推算)") if self._lon_auto and lon_estimate is not None
               else _("(手动)")))

        self._combo_sync = True
        try:
            self.night_combo.Items.Clear()
            for n in nights:
                # 项内容必须是**纯字符串**(§7.1 富内容项选中后编辑框空白)
                it = ComboBoxItem()
                it.Content = (_("{0} · {1} 目标 · {2} 帧").format(
                    n['date'], len(n['targets']), n['frames']))
                self.night_combo.Items.Append(it)
        finally:
            self._combo_sync = False

        if not nights:
            self.card_title.Text = _("没有可上天球的目标")
            self.card_sub.Text = (_("日志里没有带坐标的亮场目标(纯偏置/暗场的坐标是停机位,不显示)"))
            self.status_text.Text = _("无数据")
            self._targets = []
            self._render_targets()
            self._push_targets()
            return
        self.night_combo.SelectedIndex = len(nights) - 1   # 默认最近一夜
        if n_errors:
            self.status_text.Text = _("有 {n_errors} 个日志读取/解析失败(已跳过)").format(
                n_errors=n_errors)

    # ---------- 夜次 / 时刻 ----------

    def _on_night_changed(self, sender, args) -> None:
        if self._combo_sync:
            return
        idx = self.night_combo.SelectedIndex
        if 0 <= idx < len(self._nights):
            self._select_night(idx)

    def _select_night(self, idx: int) -> None:
        self._night_idx = idx
        night = self._nights[idx]
        self._targets = night["targets"]
        self._sel_name = None
        self._hover_name = None
        # 换夜 = 换一整套 sub:先把旧足迹清干净再重算,别让上一夜的四边形留在天上
        self._cancel_footprints()
        self._foots = []
        self._foot_by_id = {}
        self._foot_by_target = {}
        self._cover = {}
        self._sel_foot = None
        self._foot_note = ""
        self._push_footprints()
        self._render_sub(None)

        self._set_time_window(None, center=True)
        self._last_post_ts = None

        self._render_header(night)
        self._render_targets()
        self._update_time_label()
        self._push_targets()
        self._push_site()
        self._push_initial_view()
        best = self._best_target()
        if best is not None:
            self._select_target(best["name"], fly=False)
        self._start_footprints()        # 开关没开时它自己会立刻返回

    def _push_initial_view(self) -> None:
        """初始视角对准当前最高的目标(直接回天顶往往一个目标都看不到);
        「回正视角」按钮才是回天顶。

        **两条路径都走这里**:数据先到还是页面先 ready 是竞态的 —— 页面
        ready 时 ``_push_all`` 若发 reset,就会把排队里的"对准目标"覆盖成天顶
        (真机复现过:同样的代码两次启动视角不同)。
        """
        best = self._best_target()
        if best is None:
            self.host.post({"type": "reset"})
            return
        self.host.post({"type": "view", "ra": best["ra"], "dec": best["dec"],
                        "fov": 72, "animate": False})

    def _best_target(self) -> dict | None:
        """当前时刻高度最高的目标(没有站点/时刻时取第一个)。"""
        if not self._targets:
            return None
        if self._ts is None:
            return self._targets[0]
        return max(self._targets, key=lambda t: self._alt_of(t) or -90.0)

    def _on_time_changed(self, sender, args) -> None:
        if self._slider_sync or self._night_idx < 0:
            return
        if self._time_ts0 is None or self._time_ts1 is None:
            return
        frac = max(0.0, min(1.0, self.time_slider.Value / SLIDER_STEPS))
        self._ts = self._time_ts0 + (self._time_ts1 - self._time_ts0) * frac
        self._update_time_label()
        self._refresh_alt()          # 纯三角运算, 目标数量级 ~10
        if (self._last_post_ts is None
                or abs(self._ts - self._last_post_ts) >= POST_BUCKET_S):
            self._push_site()

    def _update_time_label(self) -> None:
        if self._ts is None:
            self.time_text.Text = "—"
            return
        self.time_text.Text = datetime.fromtimestamp(self._ts).strftime(
            "%m-%d %H:%M")

    def _set_time_window(self, target: dict | None, *, center: bool) -> None:
        """切换顶部滑杆的数据域，并把当前时刻夹进新的实际拍摄区间。"""
        if self._night_idx < 0:
            return
        night = self._nights[self._night_idx]
        lo, hi = _time_window_for(night, target)
        if center or self._ts is None:
            ts = 0.5 * (lo + hi)
        else:
            ts = min(hi, max(lo, self._ts))
        frac = (ts - lo) / (hi - lo)
        self._slider_sync = True
        try:
            self.time_slider.Minimum = 0
            self.time_slider.Maximum = SLIDER_STEPS
            self.time_slider.Value = frac * SLIDER_STEPS
        finally:
            self._slider_sync = False
        self._time_ts0, self._time_ts1, self._ts = lo, hi, ts
        start = datetime.fromtimestamp(lo).strftime("%H:%M")
        end = datetime.fromtimestamp(hi).strftime("%H:%M")
        self.time_range_text.Text = (
            _("{0} · 拍摄 {start}–{end}").format(
                target['name'], start=start, end=end) if target is not None
            else _("整夜 {start}–{end}").format(start=start, end=end))
        self._update_time_label()
        self._refresh_alt()
        self._last_post_ts = None
        self._push_site()

    def _on_horizon_toggled(self, sender, e) -> None:
        self._push_site()
        self._refresh_alt()

    def _on_reset_view(self, sender, e) -> None:
        self.host.post({"type": "reset"})

    def _on_site_apply(self, sender, e) -> None:
        try:
            lat = float(self.lat_box.Text.strip())
        except ValueError:
            self.shell.error(_("纬度请填 -90~90 的数字(北纬为正)"))
            return
        if not -90.0 <= lat <= 90.0:
            self.shell.error(_("纬度超出范围(-90~90)"))
            return
        self._lat = lat
        save_site(lat, self._lon, self._lon_auto)
        self.lat_box.Text = f"{lat:.4f}"
        self._push_site()
        self._refresh_alt()
        self.shell.info(_("站点已保存: {0} / {1}").format(_fmt_lat(lat), _fmt_lon(self._lon)))

    # ---------- 拍摄记录触发的质量倒推 ----------

    def request_guide_quality(self, run) -> bool:
        """从拍摄记录页启动同一套 FITS/PHD2 三证据分析。UI 线程调用。"""
        key = id(run)
        if key in self._quality_cancel:
            return False
        try:
            target = _quality_target_for_run(run)
        except (ValueError, OSError) as ex:
            self.shell.set_guide_quality_state(
                run, busy=False, text=str(ex), error=True)
            return False
        base_client = getattr(self.shell, "client", None)
        if base_client is None:
            self.shell.set_guide_quality_state(
                run, busy=False, text=_("尚未连接设备"), error=True)
            return False
        data = getattr(getattr(self.shell, "logstore", None), "data", None)
        phd2_logs = list(getattr(data, "phd2_logs", []) or [])
        cancel = threading.Event()
        self._quality_cancel[key] = cancel
        self.shell.set_guide_quality_state(
            run, busy=True, text=_("正在查找该目标的原始 FITS …"))
        threading.Thread(
            target=self._quality_work,
            args=(key, cancel, run, target, base_client, phd2_logs,
                  self._lat, self._lon),
            daemon=True, name="records-guide-quality").start()
        return True

    def cancel_guide_quality(self, run) -> bool:
        """请求停止记录页发起的分析；下载/解算均共享同一个 cancel event。"""
        cancel = self._quality_cancel.get(id(run))
        if cancel is None:
            return False
        cancel.set()
        self.shell.set_guide_quality_state(
            run, busy=True, text=_("正在停止分析 …"))
        return True

    def _quality_work(self, key: int, cancel: threading.Event, run,
                      target: dict, base_client, phd2_logs,
                      lat_deg: float, lon_deg: float) -> None:
        """工作线程：抽样 sub → 确保星点形状 → 与同期 PHD2 交叉判读。"""
        clone = None
        try:
            clone = base_client.clone()
            host = getattr(base_client, "host", "") or ""
            entries = clone.listdir(
                self._data_share(), PLAN_LIGHT_DIR + "\\" + target["name"])
            subs = _pick_subs(entries, target["ts0"], target["ts1"])
            if len(subs) < 2:
                raise ValueError(_("至少需要两张该时段的原始 FITS 才能倒推质量"))
            cat_ok = self._catalog_ok()
            foots: list[dict] = []
            total = min(len(subs), MAX_SOLVE_PER_RUN)
            for i, ent in enumerate(subs[:total]):
                if cancel.is_set():
                    raise InterruptedError(_("导星质量分析已取消"))
                self.shell.ui(
                    self.shell.set_guide_quality_state, run, True,
                    _("正在分析拍摄结果 {0}/{total}: {name}").format(
                        i + 1, total=total, name=ent.name))
                payload = self._cached_wcs(host, ent)
                has_shape = (isinstance(payload, dict) and payload.get("ok")
                             and payload.get("star_fwhm_px") is not None)
                if not has_shape:
                    if not cat_ok:
                        continue
                    # 即使 FITS 头自带 WCS，也要本机提星/解算一次才能得到主镜
                    # FWHM、椭率和方向；这是“基于拍摄结果倒推”的关键证据。
                    payload = self._solve_wcs(clone, host, ent, cancel)
                if isinstance(payload, dict) and payload.get("ok"):
                    foot = _build_foot(target, ent, payload)
                    if foot is not None:
                        foots.append(foot)
            if cancel.is_set():
                raise InterruptedError(_("导星质量分析已取消"))
            if len(foots) < 2:
                if not cat_ok:
                    raise ValueError(
                        _("缺少可用星表，无法从原始 FITS 提取主镜星点证据"))
                raise ValueError(_("成功分析的 FITS 少于两张，证据不足"))
            dither = _dither_events(host, phd2_logs)
            quality = _quality_for(
                foots, target, phd2_logs, dither, lat_deg, lon_deg)
            if quality is None:
                raise ValueError(_("可用拍摄结果不足，无法形成导星质量结论"))
            self.shell.ui(self.shell.set_guide_quality, run, quality)
        except InterruptedError:
            self.shell.ui(
                self.shell.set_guide_quality_state, run, False,
                _("分析已取消"), False)
        except (SmbClientError, ValueError, OSError) as ex:
            self.shell.ui(
                self.shell.set_guide_quality_state, run, False, str(ex), True)
        except Exception as ex:
            self.shell.ui(
                self.shell.set_guide_quality_state, run, False,
                f"{type(ex).__name__}: {ex}", True)
        finally:
            if clone is not None:
                try:
                    clone.close()
                except Exception:
                    pass
            self._quality_cancel.pop(key, None)

    # ---------- 足迹(实际视场) ----------

    def _on_foot_toggled(self, sender, e) -> None:
        on = bool(self.foot_toggle.IsOn)
        self.host.post({"type": "options", "footprints": on})
        self.foot_panel.Visibility = Visibility.Visible if on else Visibility.Collapsed
        if not on:
            self._cancel_footprints()
            self.catalog_panel.Visibility = Visibility.Collapsed
            self._render_cover()
            self._clear_sub_selection()
            return
        if self._foots:
            self._render_cover()
            self._foot_note_text()
        else:
            self._start_footprints()

    def _on_foot_cancel(self, sender, e) -> None:
        self._cancel_footprints()
        self.foot_status.Text = _("已停止解算(已解出的仍然显示)")
        self.foot_bar.Visibility = Visibility.Collapsed
        self.foot_cancel_btn.Visibility = Visibility.Collapsed

    def _cancel_footprints(self) -> None:
        # 使已经排进 DispatcherQueue 的 progress/apply 回调全部过期。只置
        # cancel event 不够:当前那张结束后仍会无条件排一次进度更新。
        self._foot_gen += 1
        if self._foot_cancel is not None:
            self._foot_cancel.set()
        self._foot_busy = False

    def _start_footprints(self) -> None:
        """解算/读 WCS 是重活 —— 只在开关打开且有夜次时才跑,且随时可中断。"""
        if not self.foot_toggle.IsOn or self._night_idx < 0 or not self._targets:
            return
        store_client = getattr(self.shell, "client", None)
        if store_client is None:
            return
        self._cancel_footprints()
        self._foot_gen += 1
        gen = self._foot_gen
        cancel = self._foot_cancel = threading.Event()
        self._foot_busy = True
        self.foot_panel.Visibility = Visibility.Visible
        self.foot_status.Text = _("正在查找该夜的原图 …")
        self.foot_bar.Visibility = Visibility.Visible
        self.foot_bar.Maximum = 100
        self.foot_bar.Value = 0
        self.foot_cancel_btn.Visibility = Visibility.Visible
        targets = [dict(t) for t in self._targets]      # 快照:工作线程不碰 UI 状态
        data = getattr(getattr(self.shell, "logstore", None), "data", None)
        phd2_logs = list(getattr(data, "phd2_logs", []) or [])
        threading.Thread(target=self._foot_work, daemon=True, name="sky3d-wcs",
                         args=(gen, cancel, self._data_share(), targets,
                               store_client, phd2_logs, self._lat,
                               self._lon)).start()

    def _foot_work(self, gen: int, cancel: threading.Event, share: str,
                   targets: list[dict], base_client, phd2_logs=(),
                   lat_deg: float = 30.0, lon_deg: float = 120.0) -> None:
        """工作线程:逐 sub 拿 WCS(缓存 → FITS 头 → 板解算)→ 足迹 + 覆盖统计。"""
        clone = None
        try:
            clone = base_client.clone()
            host = getattr(base_client, "host", "") or ""

            jobs: list[tuple[dict, object]] = []
            for t in targets:
                if cancel.is_set():
                    return
                try:
                    entries = clone.listdir(share, PLAN_LIGHT_DIR + "\\" + t["name"])
                except SmbClientError:
                    continue        # 目标目录不在(本地卡/换设备):跳过,不致命
                for ent in _pick_subs(entries, t["ts0"], t["ts1"]):
                    jobs.append((t, ent))
            total = len(jobs)
            if not total:
                self.shell.ui(self._apply_footprints, gen, [], {},
                              _("没有在 Plan/Light 下找到该夜的原图 —— 只显示日志坐标的目标点"))
                return

            cat_ok = self._catalog_ok()
            foots: list[dict] = []
            solved = need_cat = pending = failed = 0
            # **两个独立的熔断计数器**。共用一个会被互相冲掉:读头成功就
            # `fails = 0`,而读头(几 KB)几乎总是成功,于是"连着下不动 50MB
            # 原图"永远攒不到阈值,用户要干等满 MAX_SOLVE_PER_RUN 次超时。
            hdr_fails = solve_fails = 0
            for i, (t, ent) in enumerate(jobs):
                # 循环顶部 + 循环之后各有一次 cancel 判断,取消语义靠这两处:
                # 下载/解算被打断时抛的是 TransferCancelled(SmbClientError 的
                # 子类),会先被下面按"这张失败"接住,再由这两处**干净退出**
                # (不出结果、不弹错)。
                if cancel.is_set():
                    return
                payload = self._cached_wcs(host, ent)
                if payload is None:
                    try:
                        payload = self._header_wcs(clone, host, ent)
                    except SmbClientError as ex:
                        hdr_fails += 1
                        if hdr_fails >= 4:  # 连着失败多半是连接问题, 别硬刷
                            self.shell.ui(self._foot_failed, gen, str(ex))
                            return
                        continue
                    hdr_fails = 0
                if payload is None:
                    if not cat_ok:
                        need_cat += 1
                    elif solved >= MAX_SOLVE_PER_RUN:
                        pending += 1
                    else:
                        self.shell.ui(self._foot_progress, gen, i, total,
                                      _("正在解算 {name} …").format(name=ent.name))
                        solved += 1
                        try:
                            payload = self._solve_wcs(clone, host, ent, cancel)
                        except SmbClientError as ex:
                            # 一张拉不下来不该毁掉整批;但每次重试都是 50MB,
                            # 阈值比读头那条更紧
                            solve_fails += 1
                            failed += 1
                            if solve_fails >= 3:
                                self.shell.ui(self._foot_failed, gen, str(ex))
                                return
                            self.shell.ui(self._foot_progress, gen, i + 1,
                                          total, "")
                            continue
                        solve_fails = 0
                if payload is not None and payload.get("ok"):
                    foot = _build_foot(t, ent, payload)
                    if foot is not None:
                        foots.append(foot)
                elif payload is not None:
                    failed += 1
                self.shell.ui(self._foot_progress, gen, i + 1, total, "")

            if cancel.is_set():
                return
            cover: dict[str, dict] = {}
            by_target: dict[str, list[dict]] = {}
            for f in foots:
                by_target.setdefault(f["target"], []).append(f)
            tmap = {t["name"]: t for t in targets}
            dither = _dither_events(host, phd2_logs)
            for name, items in by_target.items():
                target = tmap.get(name, {})
                cover[name] = _cover_for(items, target)
                cover[name]["quality"] = _quality_for(
                    items, target, phd2_logs, dither, lat_deg, lon_deg)
            # 极轴反解要**跨目标**才可证伪:单目标恒为恰定解,残差没有意义
            check, names = _night_polar(by_target, lat_deg, lon_deg)
            _apply_night_polar({n: cover[n].get("quality") for n in cover},
                               check, names)
            self.shell.ui(self._apply_footprints, gen, foots, cover,
                          _foot_note(len(foots), total, need_cat, pending,
                                     failed, cat_ok),
                          need_cat, cat_ok)
        except InterruptedError:
            return                      # 解算被取消:不是错误
        except SmbClientError as ex:
            self.shell.ui(self._foot_failed, gen, str(ex))
        except Exception as ex:         # 工作线程异常不许静默
            self.shell.ui(self._foot_failed, gen, f"{type(ex).__name__}: {ex}")
        finally:
            if clone is not None:
                try:
                    clone.close()
                except Exception:
                    pass

    # -------- 单张 sub 的 WCS 三条路径

    def _catalog_ok(self) -> bool:
        """星表是否可用(工作线程调用:要读磁盘校验)。"""
        try:
            from astro_smb import catalog

            return bool(catalog.catalog_available())
        except Exception:
            return False

    def _cached_wcs(self, host: str, ent) -> dict | None:
        """metacache 命中(源指纹 = size+mtime)。失败结果也缓存,但会过期。"""
        from astro_smb_gui import metacache

        key = f"{ent.share}|{ent.path}"
        try:
            hit = metacache.get(FOOT_KIND, host, key,
                                src_size=ent.size, src_mtime=ent.mtime)
        except Exception:
            return None
        if not isinstance(hit, dict) or hit.get("v") != FOOT_CACHE_V:
            return None
        if hit.get("ok"):
            return hit
        # 失败结果只在 TTL 内复用 —— 星表刚下好 / 算法改了就该再试一次,
        # 否则一次失败会永久钉死这张 sub(缓存的本意是省时间,不是记仇)
        try:
            age = time.time() - float(hit.get("ts", 0.0))
        except (TypeError, ValueError):
            return None
        return hit if age < FOOT_FAIL_TTL_S else None

    def _header_wcs(self, clone, host: str, ent) -> dict | None:
        """零成本路径:FITS 头里已有的 WCS 卡片(ASIAIR 解算后会回写)。

        头里是 ``RA---TAN-SIP`` 时 ``from_fits_cards`` **只取线性部分**,畸变项
        被丢掉 —— 足迹边缘因此可能差一两个像素,对"拍到哪块天区"完全够用,
        但要在 UI 上说明白。
        """
        from astro_smb_gui import metacache
        from astro_smb import wcs as _wcs

        hdr = read_fits_header(clone, ent)
        w = _wcs.from_fits_cards(hdr)
        if w is None:
            return None
        try:
            width = int(float(hdr.get("NAXIS1")))
            height = int(float(hdr.get("NAXIS2")))
        except (TypeError, ValueError):
            return None
        if width <= 0 or height <= 0:
            return None
        payload = _wcs_to_payload(w, width, height, src="hdr",
                                  sip=bool(_wcs.cards_have_sip(hdr)),
                                  focal=_as_float(hdr.get("FOCALLEN")),
                                  ts=time.time())
        try:
            metacache.put(FOOT_KIND, host, f"{ent.share}|{ent.path}", payload,
                          src_size=ent.size, src_mtime=ent.mtime)
        except Exception:
            pass
        return payload

    def _solve_wcs(self, clone, host: str, ent,
                   cancel: threading.Event) -> dict | None:
        """串行化本地 FITS 缓存写入与板解算，供 3D/记录页共同调用。"""
        with self._solve_lock:
            return self._solve_wcs_locked(clone, host, ent, cancel)

    def _solve_wcs_locked(self, clone, host: str, ent,
                          cancel: threading.Event) -> dict | None:
        """兜底路径:把原图拉到本地缓存后本机板解算(一张几秒)。

        缓存文件名与 FITS 查看器/预览用**同一套 cache_key**,所以这 50MB 只会
        下一次、三个组件共用。成功与失败都写 metacache(失败带 TTL)。
        """
        from astro_smb_gui import metacache
        from astro_smb import platesolve

        dest = cache_dir() / f"{cache_key(host, ent)}.fit"
        key = f"{ent.share}|{ent.path}"
        try:
            # GUI 会常开一整夜;只在应用启动时裁一次无法守住 500MB 上限。
            # 下载前裁且仅在本文件尚不存在时裁,不会删掉马上要读的 dest。
            if not dest.exists():
                try:
                    clear_cache(drop_dragout=False)
                except OSError:
                    pass
            download_cached(clone, ent.share, ent.path, dest, cancel=cancel)
            res = platesolve.solve_file(str(dest), name=ent.name, cancel=cancel)
        except InterruptedError:
            raise
        except SmbClientError:
            raise
        except Exception as ex:
            payload = {"v": FOOT_CACHE_V, "ok": False, "ts": time.time(),
                       "reason": f"{type(ex).__name__}: {ex}"}
            self._put_wcs(metacache, host, key, ent, payload)
            return payload
        if not res.ok or res.wcs is None:
            payload = {"v": FOOT_CACHE_V, "ok": False, "ts": time.time(),
                       "reason": res.message or res.reason}
            self._put_wcs(metacache, host, key, ent, payload)
            return payload
        size = (res.hint.image_size if res.hint is not None else None) or (0, 0)
        if int(size[0]) <= 0 or int(size[1]) <= 0:
            # 没有图幅尺寸就画不出四边形(只知道 WCS 不知道边界在哪)。
            # 别把这种 payload 写进缓存 —— 它会被 _wcs_from_payload 当坏数据
            # 静默丢掉,然后每次重开都白解算一遍。
            payload = {"v": FOOT_CACHE_V, "ok": False, "ts": time.time(),
                       "reason": _("解算成功但拿不到图幅尺寸(NAXIS 缺失)")}
            self._put_wcs(metacache, host, key, ent, payload)
            return payload
        payload = _wcs_to_payload(res.wcs, int(size[0]), int(size[1]),
                                  src="solve", sip=False,
                                  nmatch=int(res.n_match),
                                  rms=float(res.rms_px),
                                  logfap=float(res.log_fap),
                                  focal=(res.hint.focal_len_mm
                                         if res.hint is not None else None),
                                  star_fwhm_px=float(res.star_fwhm_px),
                                  star_fwhm_arcsec=float(res.star_fwhm_arcsec),
                                  star_ellipticity=float(
                                      res.star_ellipticity),
                                  star_theta_deg=float(res.star_theta_deg),
                                  star_theta_r=float(res.star_theta_r),
                                  ts=time.time())
        self._put_wcs(metacache, host, key, ent, payload)
        return payload

    @staticmethod
    def _put_wcs(metacache, host: str, key: str, ent, payload: dict) -> None:
        try:
            metacache.put(FOOT_KIND, host, key, payload,
                          src_size=ent.size, src_mtime=ent.mtime)
        except Exception:
            pass

    # -------- 足迹结果回 UI

    def _foot_progress(self, gen: int, done: int, total: int, text: str) -> None:
        if gen != self._foot_gen:
            return
        self.foot_bar.Visibility = Visibility.Visible
        self.foot_bar.Maximum = 100
        self.foot_bar.Value = (0.0 if total <= 0
                               else min(100.0, done * 100.0 / total))
        self.foot_status.Text = text or _("正在读取实际视场 {done}/{total} …").format(
            done=done, total=total)

    def _foot_failed(self, gen: int, message: str) -> None:
        if gen != self._foot_gen:
            return
        self._foot_busy = False
        self.foot_bar.Visibility = Visibility.Collapsed
        self.foot_cancel_btn.Visibility = Visibility.Collapsed
        self.foot_status.Text = _("实际视场读取失败: {message}").format(message=message)
        self.shell.error(_("3D 天球: 实际视场读取失败 — {message}").format(message=message))

    def _apply_footprints(self, gen: int, foots: list[dict],
                          cover: dict[str, dict], note: str,
                          need_cat: int = 0, cat_ok: bool = True) -> None:
        if gen != self._foot_gen:
            return
        self._foot_busy = False
        self._foots = foots
        self._cover = cover
        self._foot_by_id = {f["id"]: f for f in foots}
        self._foot_by_target = {}
        for f in foots:
            self._foot_by_target.setdefault(f["target"], []).append(f)
        self._sel_foot = None
        self._foot_note = note
        self.foot_bar.Visibility = Visibility.Collapsed
        self.foot_cancel_btn.Visibility = Visibility.Collapsed
        self._foot_note_text()
        # 优雅降级:头里没 WCS 又没星表 —— 目标点照常显示,只是画不出实际视场
        if need_cat and not cat_ok:
            self._show_catalog_panel(
                _("有 {need_cat} 张原图的 FITS 头里没有 WCS,需要星表才能在本机板解算出它们的实际视场。目标点不受影响,照常显示。").format(
                    
                    need_cat=need_cat))
        elif not self._cat_busy:
            self.catalog_panel.Visibility = Visibility.Collapsed
        self._push_footprints()
        self._render_cover()
        # 三证据诊断属于拍摄记录语境；3D 页只产出主镜/WCS 证据并共享，
        # 由记录页按具体 TargetRun 展示，避免把导星结论塞进空间浏览工具。
        for target in self._targets:
            quality = (cover.get(target["name"]) or {}).get("quality")
            if quality is None:
                continue
            for run in target.get("runs", []):
                try:
                    self.shell.set_guide_quality(run, quality)
                except Exception:
                    pass
        self._render_sub(None)

    def _foot_note_text(self) -> None:
        self.foot_status.Text = self._foot_note or (
            _("实际视场: {0} 张").format(len(self._foots)))

    def _push_footprints(self) -> None:
        """只推 JS 需要的字段(TanWcs 之类不可序列化的留在 Python 侧)。"""
        items = [{"id": f["id"], "target": f["target"], "color": f["color"],
                  "label": f["file"], "ring": f["ring"]} for f in self._foots]
        self.host.post({"type": "footprints", "items": items,
                        "show": bool(self.foot_toggle.IsOn)})

    def _on_foot_picked(self, fid: str) -> None:
        foot = self._foot_by_id.get(fid)
        if foot is None:
            return
        # 先记下选中的 sub 再切目标 —— _select_target 会把"不属于当前目标的
        # sub 选中态"清掉,顺序反了这一下就把刚点的那张清没了
        self._sel_foot = fid
        if foot["target"] != self._sel_name:
            self._select_target(foot["target"], fly=False)
        self._render_sub(foot)

    def _clear_sub_selection(self) -> None:
        """取消 sub 选中态(同时通知页面撤掉高亮)。"""
        if self._sel_foot is not None:
            self._sel_foot = None
            self.host.post({"type": "footSelect", "id": None})
        self._render_sub(None)

    # -------- 星表缺失时的降级入口

    def _show_catalog_panel(self, text: str) -> None:
        self.catalog_text.Text = text
        self.catalog_panel.Visibility = Visibility.Visible
        self.catalog_btn.IsEnabled = not self._cat_busy

    def _on_catalog_download(self, sender, e) -> None:
        if self._cat_busy:
            return
        self._cat_busy = True
        self._cat_cancel = threading.Event()
        self.catalog_btn.IsEnabled = False
        self.catalog_bar.Visibility = Visibility.Visible
        self.catalog_bar.Value = 0
        self.catalog_text.Text = _("正在下载星表 …")
        threading.Thread(target=self._catalog_work, daemon=True,
                         name="sky3d-catalog",
                         args=(self._cat_cancel,)).start()

    def _catalog_work(self, cancel: threading.Event) -> None:
        try:
            from astro_smb import catalog

            catalog.ensure_catalog(
                progress=lambda done, total:
                    self.shell.ui(self._catalog_progress, done, total),
                cancel=cancel)
            self.shell.ui(self._catalog_done, True, "")
        except Exception as ex:
            self.shell.ui(self._catalog_done, False, f"{type(ex).__name__}: {ex}")

    def _catalog_progress(self, done: int, total: int) -> None:
        self.catalog_bar.Visibility = Visibility.Visible
        if total > 0:
            self.catalog_bar.Maximum = 100
            self.catalog_bar.Value = min(100.0, done * 100.0 / total)
            self.catalog_text.Text = (
                _("正在下载星表 {0:.0f}/{1:.0f} MB").format(done / (1 << 20), total / (1 << 20)))
        else:
            self.catalog_text.Text = _("正在下载星表 {0:.0f} MB").format(done / (1 << 20))

    def _catalog_done(self, ok: bool, message: str) -> None:
        self._cat_busy = False
        self.catalog_bar.Visibility = Visibility.Collapsed
        self.catalog_btn.IsEnabled = True
        if ok:
            self.catalog_panel.Visibility = Visibility.Collapsed
            self.shell.info(_("星表已就绪 — 正在重新计算实际视场"))
            self._start_footprints()
        else:
            self.catalog_text.Text = _("星表下载失败: {message}").format(message=message)
            self.shell.error(_("3D 天球: 星表下载失败 — {message}").format(message=message))

    # ---------- 卡片渲染 ----------

    def _render_header(self, night: dict) -> None:
        self.card_title.Text = _("{0} 观测夜").format(night['date'])
        t0 = datetime.fromtimestamp(night["ts0"]).strftime("%H:%M")
        t1 = datetime.fromtimestamp(night["ts1"]).strftime("%H:%M")
        self.card_sub.Text = (
            _("{t0} – {t1}  ·  拖动时刻滑杆看目标在天上怎么走;点目标行或天球标记可飞过去").format(t0=t0, t1=t1))
        self._fill_badges([
            (night["date"], "night"),
            (_("{0} 目标").format(len(night['targets'])), "count"),
            (_("GPU 渲染"), "gpu"),
        ])
        self._fill_pills([
            (_("{0} 帧").format(night['frames']), _("本夜总帧数"), None),
            (_("积分 {0}").format(_fmt_dur(night['exposure'])), _("曝光时间合计"), None),
            (f"{_fmt_lat(self._lat)} {_fmt_lon(self._lon)}", _("站点"), None),
        ])

    def _fill_badges(self, badges: list[tuple[str, str]]) -> None:
        self.badge_row.Children.Clear()
        for text, style in badges:
            bg, fg = self._badge.get(style, self._badge["count"])
            chip = Border()
            chip.CornerRadius = _corner(9.0)
            chip.Background = bg
            chip.Padding = Thickness(Left=8, Top=1, Right=8, Bottom=2)
            tb = TextBlock()
            tb.Text = text
            tb.FontSize = 11
            tb.FontWeight = FontWeights.SemiBold
            tb.Foreground = fg
            chip.Child = tb
            self.badge_row.Children.Append(chip)
        self.badge_row.Visibility = (Visibility.Visible if badges
                                     else Visibility.Collapsed)

    def _fill_pills(self, pills: list[tuple]) -> None:
        self.pill_row.Children.Clear()
        for text, _tip, tone in pills:
            pill = Border()
            pill.CornerRadius = _corner(4.0)
            pill.Background = self._pill_bg
            pill.Padding = Thickness(Left=7, Top=1, Right=7, Bottom=2)
            tb = TextBlock()
            tb.Text = text
            tb.FontSize = 12
            tb.FontWeight = FontWeights.SemiBold
            if tone:
                tb.Foreground = self._tone.get(tone, self._tone["dim"])
            pill.Child = tb
            self.pill_row.Children.Append(pill)
        self.pill_row.Visibility = (Visibility.Visible if pills
                                    else Visibility.Collapsed)

    def _render_targets(self) -> None:
        """目标分区:一次搭好行控件,之后时刻变化只原地改字段(不重建)。

        **行控件按目标名复用**:每行挂 3 个事件(Tapped/PointerEntered/PointerExited),
        而 win32more 的 event 描述符把实例存进类级 `_event_setters[id(instance)]`
        且**永不删除**(`-=` 与 `clear()` 只清 `_callbacks`)。审查在真 XAML app 里
        实测:8 行 × 4 轮换夜 → 条目数 8→16→24→32,`Children.Clear()` + gc 之后
        仍是 32,每条还通过 `_instance` 强引用整行子树。复用后每个目标只注册一次。
        """
        self.target_panel.Children.Clear()
        if not self._targets:
            self._rows = {}
            self._row_cache = {}
            self._fill_detail([])
            return
        self.target_panel.Children.Append(
            self._section_header(GLYPH_TARGET, _("目标(点击飞向)"), first=True))
        cache = getattr(self, "_row_cache", None)
        if cache is None:
            cache = self._row_cache = {}
        self._rows = {}
        for t in self._targets:
            # 键里带**内容指纹**而不只是目标名:换夜后同名目标的帧数/曝光会变,
            # 只按名字复用会显示上一夜的文字。内容没变才复用(那正是重复渲染
            # 造成泄漏的场景);内容真变了就重建一次,泄漏被限制在
            # "出现过多少种不同内容",而不是"重渲染了多少次"。
            key = _row_key(t)
            hit = cache.get(key)
            if hit is not None:
                self._rows[t["name"]] = hit["parts"]
                self.target_panel.Children.Append(hit["border"])
                continue
            border = self._target_row(t)
            cache[key] = {"border": border, "parts": self._rows[t["name"]]}
            self.target_panel.Children.Append(border)
        self._fill_detail(self._detail_pairs())
        self._refresh_alt()

    def _section_header(self, glyph: str, name: str, first: bool = False) -> Grid:
        """分区小标题:图标 + 淡色小字 + 细分隔线(与浏览页详情同风格)。"""
        head = Grid()
        for unit in (GridUnitType.Auto, GridUnitType.Auto, GridUnitType.Star):
            c = ColumnDefinition()
            c.Width = GridLength(Value=1.0, GridUnitType=unit)
            head.ColumnDefinitions.Append(c)
        head.Margin = Thickness(Left=0, Top=(1 if first else 9), Right=0, Bottom=3)

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
        return head

    def _target_row(self, t: dict) -> Border:
        row = Border()
        row.CornerRadius = _corner(4.0)
        row.Padding = Thickness(Left=6, Top=4, Right=6, Bottom=4)
        row.Background = self._transparent

        outer = StackPanel()
        outer.Spacing = 1

        top = Grid()
        top.ColumnSpacing = 6
        for unit in (GridUnitType.Auto, GridUnitType.Star,
                     GridUnitType.Auto, GridUnitType.Auto):
            c = ColumnDefinition()
            c.Width = GridLength(Value=1.0, GridUnitType=unit)
            top.ColumnDefinitions.Append(c)

        dot = Rectangle()
        dot.Width = dot.Height = 9.0
        dot.RadiusX = dot.RadiusY = 4.5
        dot.Fill = _hex_brush(t["color"])
        dot.VerticalAlignment = VerticalAlignment.Center
        top.Children.Append(dot)
        Grid.SetColumn(dot, 0)

        name = TextBlock()
        name.Text = t["name"]
        name.FontSize = 13
        name.FontWeight = FontWeights.SemiBold
        name.TextTrimming = TextTrimming.CharacterEllipsis
        name.VerticalAlignment = VerticalAlignment.Center
        top.Children.Append(name)
        Grid.SetColumn(name, 1)

        alt = TextBlock()
        alt.Text = "—"
        alt.FontSize = 12
        alt.FontWeight = FontWeights.SemiBold
        alt.FontFamily = self._mono
        alt.VerticalAlignment = VerticalAlignment.Center
        top.Children.Append(alt)
        Grid.SetColumn(alt, 2)

        bar, fill = self._alt_bar()
        top.Children.Append(bar)
        Grid.SetColumn(bar, 3)
        outer.Children.Append(top)

        sub = TextBlock()
        sub.Text = (_("{0} 帧 · {1} · {2}").format(
            t['frames'], _fmt_dur(t['exposure']), t['source']))
        sub.FontSize = 11
        sub.Opacity = 0.6
        sub.Margin = Thickness(Left=15, Top=0, Right=0, Bottom=0)
        sub.TextTrimming = TextTrimming.CharacterEllipsis
        outer.Children.Append(sub)

        row.Child = outer
        row.Tapped += (lambda s, e, n=t["name"]: self._select_target(n, fly=True))
        row.PointerEntered += (lambda s, e, n=t["name"]: self._set_hover(n))
        row.PointerExited += (lambda s, e: self._set_hover(None))
        self._rows[t["name"]] = {"border": row, "alt": alt, "fill": fill,
                                 "bar": bar}
        return row

    def _alt_bar(self) -> tuple[Canvas, Rectangle]:
        """迷你高度条(60×8):底槽 + 语义色填充(0~90°)。"""
        w, h = 60.0, 8.0
        canvas = Canvas()
        canvas.Width, canvas.Height = w, h
        canvas.VerticalAlignment = VerticalAlignment.Center
        track = Rectangle()
        track.Width, track.Height = w, h
        track.RadiusX = track.RadiusY = 4.0
        track.Fill = self._track_bg
        canvas.Children.Append(track)
        Canvas.SetLeft(track, 0.0)
        Canvas.SetTop(track, 0.0)
        fill = Rectangle()
        fill.Width, fill.Height = 0.0, h
        fill.RadiusX = fill.RadiusY = 4.0
        fill.Fill = self._tone["dim"]
        canvas.Children.Append(fill)
        Canvas.SetLeft(fill, 0.0)
        Canvas.SetTop(fill, 0.0)
        return canvas, fill

    def _refresh_alt(self) -> None:
        """按当前时刻更新每行的高度角文本/条(原地改,不重建控件)。"""
        for t in self._targets:
            row = self._rows.get(t["name"])
            if row is None:
                continue
            alt = self._alt_of(t)
            tone = _alt_tone(alt)
            if alt is None:
                row["alt"].Text = "—"
                row["fill"].Width = 0.0
            else:
                row["alt"].Text = f"{alt:5.1f}°"
                row["fill"].Width = max(0.0, min(1.0, alt / 90.0)) * 60.0
            row["alt"].Foreground = self._tone[tone]
            row["fill"].Fill = self._tone[tone]
            row["bar"].Opacity = 0.4 if tone == "dim" else 1.0
        self._refresh_detail_alt()

    def _refresh_detail_alt(self) -> None:
        """详情卡里的高度/方位就地改文本 —— 拖时刻滑杆时**不重建**键值行。"""
        name = self._hover_name or self._sel_name
        t = next((x for x in self._targets if x["name"] == name), None)
        if t is None or self._detail_alt is None:
            return
        aa = self._altaz_of(t)
        if aa is None:
            return
        alt, az = aa
        tone = _alt_tone(alt)
        self._detail_alt.Text = f"{alt:.1f}°"
        self._detail_alt.Foreground = self._tone[tone]
        if self._detail_alt_note is not None:
            self._detail_alt_note.Text = _("地平线下") if alt < 0 else ""
        if self._detail_az is not None:
            self._detail_az.Text = f"{_az_name(az)} {az:.0f}°"

    def _alt_of(self, t: dict) -> float | None:
        if self._ts is None:
            return None
        alt, _az = astro.altaz(t["ra"], t["dec"], self._lat, self._lon, self._ts)
        return alt

    def _altaz_of(self, t: dict) -> tuple[float, float] | None:
        if self._ts is None:
            return None
        return astro.altaz(t["ra"], t["dec"], self._lat, self._lon, self._ts)

    # ---------- 选中 / 悬停 ----------

    def _set_hover(self, name: str | None) -> None:
        if name == self._hover_name:
            return
        self._hover_name = name
        for key, row in self._rows.items():
            if key == self._sel_name:
                row["border"].Background = self._row_sel
            elif key == name:
                row["border"].Background = self._row_hover
            else:
                row["border"].Background = self._transparent
        self._fill_detail(self._detail_pairs())

    def _select_target(self, name: str, fly: bool) -> None:
        changed = name != self._sel_name
        self._sel_name = name
        self._set_hover(None)
        for key, row in self._rows.items():
            row["border"].Background = (self._row_sel if key == name
                                        else self._transparent)
        t = next((x for x in self._targets if x["name"] == name), None)
        if t is not None and fly:
            self.host.post({"type": "view", "ra": t["ra"], "dec": t["dec"],
                            "fov": 34, "animate": True})
        if t is not None and changed:
            self._set_time_window(t, center=True)
        self._fill_detail(self._detail_pairs())
        # 换目标后原先选中的那张 sub 属于**别的**目标, 留着右栏就自相矛盾
        sel = self._foot_by_id.get(self._sel_foot or "")
        if sel is not None and sel.get("target") != name:
            self._clear_sub_selection()
        self._render_cover()

    def _detail_pairs(self) -> list[tuple]:
        """当前关注目标(悬停优先于选中)的键值行,纯数据。"""
        name = self._hover_name or self._sel_name
        t = next((x for x in self._targets if x["name"] == name), None)
        if t is None:
            return []
        pairs: list[tuple] = [
            (_("目标"), t["name"], "", False, None),
            (_("赤经"), astro.format_ra(t["ra"]), t["source"], True, None),
            (_("赤纬"), astro.format_dec(t["dec"]), "", True, None),
        ]
        aa = self._altaz_of(t)
        if aa is not None:
            alt, az = aa
            tone = _alt_tone(alt)
            note = _("地平线下") if alt < 0 else ""
            pairs.append((_ROW_ALT, f"{alt:.1f}°", note, False, tone))
            pairs.append((_ROW_AZ, f"{_az_name(az)} {az:.0f}°", "", False, None))
        pairs.append((_("帧数"), f"{t['frames']}", "", False, None))
        pairs.append((_("积分"), _fmt_dur(t["exposure"]), "", False, None))
        pairs.append((_("时段"),
                      f"{t['t0'].strftime('%H:%M')} – {t['t1'].strftime('%H:%M')}",
                      "", False, None))
        if t["plans"]:
            pairs.append((_("计划"),
                          _("、").join(f"Plan {p}" for p in t["plans"]),
                          "", False, None))
        return pairs

    def _fill_detail(self, pairs: list[tuple]) -> None:
        """两列键值:标签淡色,数值可选中;坐标用等宽;语义色只染数值。"""
        self.detail_grid.RowDefinitions.Clear()
        self.detail_grid.Children.Clear()
        self._detail_alt = None
        self._detail_alt_note = None
        self._detail_az = None
        if not pairs:
            return
        head = self._section_header(GLYPH_DETAIL, _("目标详情"), first=True)
        self._add_row(self.detail_grid)
        self.detail_grid.Children.Append(head)
        Grid.SetRow(head, 0)
        Grid.SetColumn(head, 0)
        Grid.SetColumnSpan(head, 2)
        row = 1
        for k, v, note, mono, tone in pairs:
            self._add_row(self.detail_grid)
            lab = TextBlock()
            lab.Text = _(k)              # 元组里存的是 msgid
            lab.FontSize = 12
            lab.Opacity = 0.55
            self.detail_grid.Children.Append(lab)
            Grid.SetRow(lab, row)
            Grid.SetColumn(lab, 0)

            holder = StackPanel()
            holder.Orientation = Orientation.Horizontal
            holder.Spacing = 6
            val = TextBlock()
            val.Text = v
            val.FontSize = 12
            val.TextWrapping = TextWrapping.Wrap
            val.IsTextSelectionEnabled = True
            val.VerticalAlignment = VerticalAlignment.Center
            if mono:
                val.FontFamily = self._mono
            if tone:
                val.Foreground = self._tone.get(tone, self._tone["dim"])
            holder.Children.Append(val)
            if note or k == _ROW_ALT:   # 高度行的副注要留位, 之后可能变"地平线下"
                sub = TextBlock()
                sub.Text = note
                sub.FontSize = 11
                sub.Opacity = 0.55
                sub.VerticalAlignment = VerticalAlignment.Center
                holder.Children.Append(sub)
            else:
                sub = None
            if k == _ROW_ALT:       # 记下引用, 时刻变化时就地改(见 _refresh_detail_alt)
                self._detail_alt, self._detail_alt_note = val, sub
            elif k == _ROW_AZ:
                self._detail_az = val
            self.detail_grid.Children.Append(holder)
            Grid.SetRow(holder, row)
            Grid.SetColumn(holder, 1)
            row += 1

    def _add_row(self, grid: Grid) -> None:
        r = RowDefinition()
        r.Height = GridLength(Value=1.0, GridUnitType=GridUnitType.Auto)
        grid.RowDefinitions.Append(r)

    # ---------- 覆盖卡 / sub 详情卡 ----------

    def _fill_kv(self, panel: StackPanel, glyph: str, title: str,
                 pairs: list[tuple]) -> None:
        """把一组键值填进某个 StackPanel(分区小标题 + 两列 Grid)。

        这里建的控件**一律不挂事件** —— 它们每次选中都会重建,挂上去就是
        §「win32more 事件永久泄漏」那条铁律里说的只增不删。要交互请走天球上的
        拾取(容器级单一事件)。
        """
        panel.Children.Clear()
        if not pairs:
            panel.Visibility = Visibility.Collapsed
            return
        panel.Visibility = Visibility.Visible
        panel.Children.Append(self._section_header(glyph, title))

        grid = Grid()
        grid.ColumnSpacing = 10
        grid.RowSpacing = 3
        for unit in (GridUnitType.Auto, GridUnitType.Star):
            c = ColumnDefinition()
            c.Width = GridLength(Value=1.0, GridUnitType=unit)
            grid.ColumnDefinitions.Append(c)
        for row, (k, v, note, mono, tone) in enumerate(pairs):
            self._add_row(grid)
            lab = TextBlock()
            lab.Text = _(k)              # 元组里存的是 msgid
            lab.FontSize = 12
            lab.Opacity = 0.55
            grid.Children.Append(lab)
            Grid.SetRow(lab, row)
            Grid.SetColumn(lab, 0)

            holder = StackPanel()
            holder.Spacing = 0
            val = TextBlock()
            val.Text = v
            val.FontSize = 12
            val.TextWrapping = TextWrapping.Wrap
            val.IsTextSelectionEnabled = True
            if mono:
                val.FontFamily = self._mono
            if tone:
                val.Foreground = self._tone.get(tone, self._tone["dim"])
            holder.Children.Append(val)
            if note:
                sub = TextBlock()
                sub.Text = note
                sub.FontSize = 11
                sub.Opacity = 0.55
                sub.TextWrapping = TextWrapping.Wrap
                holder.Children.Append(sub)
            grid.Children.Append(holder)
            Grid.SetRow(holder, row)
            Grid.SetColumn(holder, 1)
        panel.Children.Append(grid)

    def _render_cover(self) -> None:
        """当前选中目标的「覆盖」卡。"""
        name = self._sel_name
        if not self.foot_toggle.IsOn or not name:
            self.cover_panel.Children.Clear()
            self.cover_panel.Visibility = Visibility.Collapsed
            return
        foots = self._foot_by_target.get(name) or []
        cov = self._cover.get(name)
        title = _("覆盖 · {name}").format(name=name)
        if not foots or not cov:
            state = _("计算中 …") if self._foot_busy else _("不可用")
            note = (_("正在逐 sub 读取 WCS") if self._foot_busy
                    else _("这个目标没有解出任何一张 sub 的 WCS"))
            self._fill_kv(self.cover_panel, GLYPH_COVER, title,
                          [(_("状态"), state, note, False, "dim")])
            return

        n = int(cov.get("n") or len(foots))
        n_hdr, n_solved = int(cov.get("n_hdr") or 0), int(cov.get("n_solved") or 0)
        pairs: list[tuple] = [
            (_("张数"), _("{n} 张").format(n=n),
             _("头 WCS {n_hdr} · 解算 {n_solved}").format(
                 n_hdr=n_hdr, n_solved=n_solved), False, None),
        ]
        if cov.get("source") == _COV_NA:
            # 覆盖类的量算不出来时**明说**,而不是让那几行静默消失 ——
            # 用户分不清"没算出来"和"没什么可说"。见 `_local_cover`。
            pairs.append((_("覆盖统计"), _("不可用"),
                          _("足迹散布过大或 wcsapps 不可用;跨度/场旋照常给"),
                          False, "warn"))
        area = cov.get("area")
        if isinstance(area, (int, float)) and area > 0:
            single = cov.get("single") or 0.0
            extra = (_("单帧 {0}").format(_fmt_area(single)) if single > 0 else "")
            pairs.append((_("覆盖面积"), _fmt_area(float(area)), extra, False, None))
        common = cov.get("common")
        if isinstance(common, (int, float)):
            frac = max(0.0, min(1.0, float(common)))
            tone = "good" if frac >= 0.8 else ("warn" if frac >= 0.5 else "bad")
            pairs.append((_("公共交集"), f"{frac * 100:.0f}%",
                          _("N 张都盖到的部分占并集"), False, tone))
        keep = cov.get("keep")
        if isinstance(keep, (int, float)):
            frac = max(0.0, min(1.0, float(keep)))
            tone = "good" if frac >= 0.9 else ("warn" if frac >= 0.7 else "bad")
            pairs.append((_("单帧留存"), f"{frac * 100:.0f}%",
                          _("叠加后一张 sub 还剩多少能用"), False, tone))
        gaps = cov.get("n_gaps")
        if isinstance(gaps, int) and gaps > 0:
            pairs.append((_("内部缺口"), _("{gaps} 处").format(gaps=gaps),
                          _("最大 {0} — 被拍摄区域包围却没盖到").format(
                              _fmt_area(float(cov.get('max_gap') or 0.0))), False, "warn"))
        err = cov.get("point_err")
        if isinstance(err, (int, float)):
            tone = "good" if err < 0.05 else ("warn" if err < 0.25 else "bad")
            pairs.append((_("指向误差"), _fmt_sep(float(err)),
                          _("实测中心 vs 日志 goto"), False, tone))
        drift = cov.get("drift")
        if cov.get("meridian_flip"):
            pairs.append((_("位置角漂移"), "—",
                          _("疑似中天翻转(约 180° 跳变),已停止拟合,不给极轴结论"),
                          False, "warn"))
        elif isinstance(drift, (int, float)):
            span = float(cov.get("span_h") or 0.0)
            tone = "good" if abs(drift) < 0.1 else ("warn" if abs(drift) < 0.5
                                                    else "bad")
            note = _("跨度 {span:.1f} h 线性拟合").format(span=span)
            drms = cov.get("drift_rms")
            if isinstance(drms, (int, float)):
                # 残差大 = 转速本身在变(过中天前后就会),只看斜率会误判
                note += _(",残差 {0:.3f}°").format(float(drms))
            pairs.append((_("位置角漂移"), _fmt_drift(float(drift)), note,
                          False, tone))
        elif len(foots) > 1:
            pairs.append((_("位置角漂移"), "—", _("时间跨度太短,不给结论"), False, "dim"))
        rms = cov.get("rms_med")
        if isinstance(rms, (int, float)):
            # §契约:rms_px 只统计配对容差内的内点,是"中心区域拟合得多好",
            # 不是这张图的畸变有多大,更不是成功判据 —— UI 上必须这么说
            pairs.append((_("解算残差"), _("{0:.2f} px 中位").format(float(rms)),
                          _("中心区域的拟合残差,不代表全幅畸变"), True, None))
        n_sip = int(cov.get("n_sip") or 0)
        if n_sip:
            pairs.append((_("畸变项"), _("{n_sip} 张头里带 SIP").format(n_sip=n_sip),
                          _("足迹只用了线性部分"), False, "dim"))
        pairs.append((N_("统计来源"), _(str(cov.get("source") or N_("本页估算"))),
                      "", False, "dim"))
        self._fill_kv(self.cover_panel, GLYPH_COVER, title, pairs)

    def _render_sub(self, foot: dict | None) -> None:
        """选中的单张 sub 详情(点天球上的足迹触发)。"""
        if foot is None:
            self.sub_panel.Children.Clear()
            self.sub_panel.Visibility = Visibility.Collapsed
            return
        src = foot.get("src")
        if src == "hdr":
            src_text = _("FITS 头自带 WCS")
            src_note = _("ASIAIR 解算后回写") + (_("(含 SIP,只取线性部分)")
                                              if foot.get("sip") else "")
        else:
            src_text = _("本机板解算")
            src_note = "astro_smb.platesolve"
        fov = foot.get("fov") or (0.0, 0.0)
        pairs: list[tuple] = [
            (_("文件"), str(foot.get("file") or ""), "", False, None),
            (_("时刻"), datetime.fromtimestamp(foot["ts"]).strftime("%m-%d %H:%M:%S"),
             _("曝光结束时刻"), True, None),
            (_("中心"), f"{astro.format_ra(foot['ra'])} {astro.format_dec(foot['dec'])}",
             "", True, None),
            (_("视场"), f"{fov[0]:.2f}° × {fov[1]:.2f}°",
             f"{foot['w']}×{foot['h']} px", False, None),
            (_("像素尺度"), f"{foot['scale']:.3f} ″/px", "", True, None),
            (_("旋转角"), f"{foot['rot']:.2f}°",
             _("图像 +y 轴的位置角"), True, None),
        ]
        if isinstance(foot.get("zwo"), (int, float)):
            pairs.append((_("ZWO 角"), f"{float(foot['zwo']):.2f}°",
                          _("与日志 Angle= / 文件名同一约定"), True, None))
        pairs.append((_("宇称"), _("镜像") if foot.get("flip") else _("常规"), "", False, None))
        if isinstance(foot.get("nmatch"), (int, float)):
            pairs.append((_("匹配星数"), f"{int(foot['nmatch'])}", _("解算的成功判据"),
                          False, "good"))
        rms = foot.get("rms")
        if isinstance(rms, (int, float)) and rms == rms:
            pairs.append((_("解算残差"), f"{float(rms):.2f} px",
                          _("中心区域的拟合残差,不代表全幅畸变"), True, None))
        pairs.append((_("来源"), src_text, src_note, False, "dim"))
        self._fill_kv(self.sub_panel, GLYPH_SUB, _("这张 sub"), pairs)
