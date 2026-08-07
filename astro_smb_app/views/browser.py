"""浏览页的视图模型 —— 天文详情卡片、气量/高度角判读、夜次配色。

从 `astro_smb_gui/_browser.py` 原样搬来,**函数体一个字节没动**。留在原处的是
真正绑 WinUI 的部分:`_brush`/`_corner`、行 Grid 拼装、迷你雷达绘制。

这一层值钱在**判读**而不在取值:`_airmass` 用的是 Pickering (2002) 而不是课本
的 1/sin(h),`_alt_tone`/`_airmass_tone`/`_sampling_verdict` 的阈值都是有前提的
经验值。两套前端各写一份,迟早会在某一次"顺手调阈值"时分叉,而分叉后两边给出
不同判读、谁都不知道哪个对。
"""
from __future__ import annotations

import math
from datetime import datetime, timezone

from astro_smb import astro
from astro_smb.autorunlog import night_key
from astro_smb.i18n import N_, gettext as _
from astro_smb.client import RemoteEntry
from astro_smb.fitshdr import FitsHeader
from astro_smb.naming import parse_image_name
from astro_smb.util import format_mtime, human_size
from astro_smb_app.entries import ext_category, ext_category_id

RENDER_CAP = 2000
RENDER_BATCH = 200

# 子项计数用的缓存新鲜度上限:比这更旧的目录会在后台补一次真 listdir。
# 计数只是装饰性数字,先出旧值再校正远比干等强;但也不能一直不校。
CHILD_COUNT_TTL = 300.0

# **表里放 `N_()`(只标记不翻),取用时才 `_()`。** 模块级的东西在 import 时
# 求值一次,直接写 `_()` 会把翻译冻在那一刻的语言上 —— 之后切语言这几个词
# 永远不跟着变,而且不报错。下面 `_AZ_NAMES` 同理。
_KIND_CN = {"Light": N_("亮场"), "Bias": N_("偏置"), "Dark": N_("暗场"),
            "Flat": N_("平场"), "Preview": N_("预览")}

# 16 向方位名(按 22.5° 就近取整)
_AZ_NAMES = [N_("北"), N_("北北东"), N_("东北"), N_("东北东"), N_("东"),
             N_("东南东"), N_("东南"), N_("南南东"), N_("南"), N_("南南西"),
             N_("西南"), N_("西南西"), N_("西"), N_("西北西"), N_("西北"),
             N_("北北西")]

# 详情组小标题图标(Segoe Fluent Icons;与 _common.glyph_for 同族)
_GRP_TARGET = ""      # FavoriteStarFill
_GRP_OPTICS = ""      # View
_GRP_CAMERA = ""      # Camera
_GRP_TIME = ""        # Calendar
_GRP_FILE = ""        # Document
_GRP_PLACE = ""       # Globe

# 数值语义色(中间调:浅色/深色主题下都可读,不带底色)
_TONE_RGB = {
    "good": (0x3F, 0xA9, 0x55),     # 绿:好
    "warn": (0xD0, 0x8A, 0x00),     # 琥珀:需留意
    "bad":  (0xD9, 0x4A, 0x4A),     # 红:差
    "dim":  (0x8A, 0x8A, 0x8A),     # 灰:无意义/不适用(如地平线下)
}

# 夜次徽章配色(浅底 + 深字,两主题下均可读);按夜次日期排序循环取用
_NIGHT_PALETTE = [
    ((0xD7, 0xE8, 0xFA), (0x0D, 0x47, 0xA1)),   # 蓝
    ((0xDC, 0xEF, 0xDC), (0x1B, 0x5E, 0x20)),   # 绿
    ((0xFB, 0xE7, 0xC6), (0x7A, 0x52, 0x00)),   # 琥珀
    ((0xF3, 0xD9, 0xE4), (0x88, 0x1B, 0x50)),   # 玫红
    ((0xE2, 0xDC, 0xF4), (0x4A, 0x33, 0x82)),   # 紫
    ((0xD2, 0xEE, 0xEC), (0x0F, 0x4C, 0x4A)),   # 青
    ((0xE9, 0xE3, 0xD2), (0x5A, 0x4A, 0x1E)),   # 卡其
    ((0xE4, 0xE4, 0xE4), (0x45, 0x45, 0x45)),   # 中性灰
]


def _az_name(az_deg: float) -> str:
    return _(_AZ_NAMES[int(((az_deg % 360.0) + 11.25) / 22.5) % 16])


def _airmass(alt_deg: float) -> float | None:
    """气量(大气质量),Pickering (2002) 经验式:

        X = 1 / sin(h + 244 / (165 + 47·h^1.1))      (h = 高度角, 单位度)

    出处:Pickering, K. A. (2002), "The Southern Limits of the Ancient Star
    Catalog", DIO 12:1, 20。相对真实(积分)气量在整个 0~90° 范围误差 <0.1%,
    地平处收敛到约 38 而不发散。
    不用课本式 1/sin(h):它只在高空成立,低空显著低估消光(h=5° 已差 2% 以上),
    h→0 时发散,以前只能用 ">19" 这种回避写法,现在低空可以直接给真实数值。
    高度角 ≤0(地平线下)时气量无意义,返回 None。
    """
    if alt_deg <= 0.0:
        return None
    return 1.0 / math.sin(math.radians(
        alt_deg + 244.0 / (165.0 + 47.0 * alt_deg ** 1.1)))


def _airmass_text(alt_deg: float) -> str:
    am = _airmass(alt_deg)
    return "—" if am is None else f"{am:.2f}"


def _alt_tone(alt_deg: float) -> str:
    """高度角语义:≤0° 地平线下(灰) / ≥40° 好 / 20~40° 尚可 / <20° 低空
    (消光与湍流都重)。"""
    if alt_deg <= 0.0:
        return "dim"
    if alt_deg >= 40.0:
        return "good"
    return "warn" if alt_deg >= 20.0 else "bad"


#: 高度角判读的**语义键**。测试断言这些,不断言中文 ——
#: 文案会随语言变,判读不会。
ALT_BELOW_HORIZON = "alt.below_horizon"
ALT_LOW = "alt.low"
ALT_SOMEWHAT_LOW = "alt.somewhat_low"
ALT_OK = "alt.ok"


def alt_verdict(alt_deg: float) -> str:
    """高度角判读 → **语义键**(与语言无关)。

    阈值与 `_alt_tone` 是同一套:≤0° 地平线下 / <20° 低空 / <40° 偏低 / 其余可以。
    显示文本走 `_alt_hint`。**判读和文案分开**,是为了让测试断言前者:
    这个仓库以前把断言绑在中文串上,改一句文案红一片,于是没人敢改文案。
    """
    if alt_deg <= 0.0:
        return ALT_BELOW_HORIZON
    if alt_deg < 20.0:
        return ALT_LOW
    return ALT_SOMEWHAT_LOW if alt_deg < 40.0 else ALT_OK


#: 判读键 → 显示文本。**`_()` 要在取用时调用,不能在模块加载时**
#: —— 模块只加载一次,而语言是可以中途切的。
_ALT_HINTS = {
    ALT_BELOW_HORIZON: lambda: _("(地平线下 · 检查站点纬度设置)"),
    ALT_LOW: lambda: _("(低空 · 消光重)"),
    ALT_SOMEWHAT_LOW: lambda: _("(偏低)"),
    ALT_OK: lambda: "",
}


def _alt_hint(alt_deg: float) -> str:
    # 目标在地平线下多半是站点纬度没设对 —— 那句提示指的就是这个
    return _ALT_HINTS[alt_verdict(alt_deg)]()


def _airmass_note(alt_deg: float) -> str:
    return _("(地平线下)") if alt_deg <= 0.0 else _("(大气质量)")


def _airmass_tone(alt_deg: float) -> str:
    am = _airmass(alt_deg)
    if am is None:
        return "dim"        # 地平线下不是"差",是没有意义
    if am < 1.5:
        return "good"
    return "warn" if am < 2.5 else "bad"


def _sampling_verdict(scale: float) -> tuple[str, str]:
    """像元比例(″/px)→ (采样判定, 语义色)。经验阈值,前提视宁 2~3″。"""
    if scale > 2.0:
        return _("欠采样"), "warn"
    if scale < 0.7:
        return _("过采样"), "warn"
    return _("合适"), "good"


def _night_of_name(name: str) -> str | None:
    """影像文件名 → 观测夜键 'YYYY-MM-DD'(正午分界);无时间戳返回 None。"""
    info = parse_image_name(name)
    if info is None or info.time is None:
        return None
    return night_key(info.time)


def _fmt_exposure(seconds: float | None, raw: str | None = None) -> str:
    if seconds is None:
        return raw or "?"
    if seconds >= 1.0:
        return f"{seconds:g}s"
    return f"{seconds * 1000:g}ms"


def _hdr_suffix(hdr: FitsHeader, info) -> str:
    """FITS 头 → 副行补充摘要(文件名没有的信息:增益/温度,必要时目标/曝光)。"""
    parts: list[str] = []
    if info is None or info.target is None:
        obj = hdr.get("OBJECT")
        if obj:
            parts.append(obj)
    if info is None:
        try:
            exp = float(hdr.get("EXPTIME") or hdr.get("EXPOSURE") or "")
            parts.append(_fmt_exposure(exp))
        except ValueError:
            pass
        filt = hdr.get("FILTER")
        if filt:
            parts.append(filt)
    gain = hdr.get("GAIN")
    if gain:
        parts.append(_("增益{gain}").format(gain=gain))
    temp = hdr.get("CCD-TEMP")
    if temp:
        try:
            parts.append(f"{float(temp):.1f}℃")
        except ValueError:
            pass
    return " · ".join(parts)


def _astro_details(entry: RemoteEntry,
                   fits: FitsHeader | None,
                   site: tuple[float, float] | None = None):
    """(标题, 副题, 分组键值, 徽章, 天球, 参数胶囊) —— 天文卡片内容;
    非天文文件返回 (None, "", [], [], None, [])。

    数据来源双轨:文件名解析(即时) + FITS 头(预览结果到达后),头优先。
    分组键值为 (图标, 组名, 键值对) 列表 —— 组内元素
    (标签, 值[, 副注[, 等宽[, 语义色[, 小组件]]]]):副注以淡色小字随主值横排,
    语义色 ∈ _TONE_RGB 只染主值(标签保持淡色),小组件如 ("altbar", 高度角);
    徽章为 (文本, 样式键),样式键对应 BrowserPage._badge_brushes,
    夜次徽章样式键形如 "night:2026-07-23"(按视图配色表取色);
    参数胶囊为 (文本, 提示, 语义色|None),渲染成圆角小 pill;
    天球 = (ra, dec, unix_ts, alt, az) 供迷你雷达绘制,缺坐标/时刻/站点为 None。
    site 为 (纬度, 经度),缺失时跳过地平位置/天球派生。
    """
    info = parse_image_name(entry.name)
    has_fits = fits is not None and bool(fits.cards)
    if info is None and not has_fits:
        return None, "", [], [], None, []
    g = (lambda k: fits.get(k) if has_fits else None)

    def fnum(*keys: str) -> float | None:
        """依次尝试各 header 键,返回第一个可解析为浮点的值。"""
        for k in keys:
            v = g(k)
            if v:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
        return None

    target = g("OBJECT") or (info.target if info else None)
    kind_cn = _(_KIND_CN.get(info.kind, info.kind)) if info else (
        _(_KIND_CN.get((g("IMAGETYP") or "").capitalize(), g("IMAGETYP") or "FITS")))
    title = target or kind_cn

    sub_parts: list[str] = []
    if target:
        sub_parts.append(kind_cn)
    if info and info.seq is not None:
        sub_parts.append(_("第 {seq} 张").format(seq=info.seq))
    if info and info.time is not None:
        sub_parts.append(info.time.strftime("%Y-%m-%d %H:%M:%S"))
    sub = " · ".join(sub_parts)

    # 四个分区各自累积键值:目标 / 光学 / 相机 / 时间与位置
    tgt: list[tuple] = []
    opt: list[tuple] = []
    cam_pairs: list[tuple] = []
    tim: list[tuple] = []
    pills: list[tuple] = []

    # 坐标(FITS 头里是度)—— 等宽字体便于逐位比对
    ra = dec = None
    try:
        ra, dec = float(g("RA") or ""), float(g("DEC") or "")
        tgt.append((_("坐标"), f"{astro.format_ra(ra)}  {astro.format_dec(dec)}",
                    "", True))
    except (ValueError, TypeError):
        ra = dec = None

    # 拍摄时地平位置 + 迷你雷达数据:坐标 + 时刻(DATE-OBS 优先, 兜底文件名)+ 站点
    sky = None
    if ra is not None and dec is not None:
        ts = None
        raw_date = g("DATE-OBS")
        if raw_date:
            try:
                dt = datetime.fromisoformat(raw_date.strip())
                if dt.tzinfo is None:   # FITS 惯例:无时区标注即 UTC
                    dt = dt.replace(tzinfo=timezone.utc)
                ts = dt.timestamp()
            except ValueError:
                ts = None
        if ts is None and info is not None and info.time is not None:
            try:
                ts = astro.unix_from_local(info.time)
            except (ValueError, OSError, OverflowError):
                ts = None
        if ts is not None and site is not None:
            try:
                alt, az = astro.altaz(ra, dec, site[0], site[1], ts)
                sky = (ra, dec, ts, alt, az)
                # 高度角:数值着色 + 迷你弧条;方位/气量各自成行便于分别着色
                tgt.append((_("高度角"), f"{alt:.1f}°", _alt_hint(alt), False,
                            _alt_tone(alt), ("altbar", alt)))
                tgt.append((_("方位"), f"{az:.0f}° ({_az_name(az)})"))
                tgt.append((_("气量"), _airmass_text(alt), _airmass_note(alt), False,
                            _airmass_tone(alt)))
            except (ValueError, OSError, OverflowError):
                sky = None

    # 曝光(≥1 分钟时附分钟提示)—— 与增益/温度一起做成顶部 pill
    exp_s: float | None = None
    exp_txt = None
    try:
        exp_s = float(g("EXPTIME") or g("EXPOSURE") or "")
        exp_txt = _fmt_exposure(exp_s)
    except ValueError:
        if info:
            exp_s = info.exposure_s
            exp_txt = _fmt_exposure(exp_s, info.exposure)
    if exp_txt:
        note = _("{0:g} 分钟").format(exp_s / 60) if exp_s is not None and exp_s >= 60 else ""
        pills.append((_("曝光 {exp_txt}").format(exp_txt=exp_txt), note, None))

    filt = g("FILTER") or (info.filter if info else None)  # 滤镜改由徽章展示
    binning = fnum("XBINNING")
    if binning is None and info is not None:
        binning = float(info.binning)

    gain = g("GAIN")
    if gain:
        pills.append((_("增益 {gain}").format(gain=gain), "", None))
    temp = g("CCD-TEMP")
    if temp:
        try:
            tval = float(temp)
        except ValueError:
            tval = None
        if tval is not None:
            tip, tone = "", None
            set_t = g("SET-TEMP")
            if set_t:
                try:
                    sv = float(set_t)
                    delta = tval - sv
                    tip = _("目标 {sv:.0f}℃ · 偏离 {delta:+.1f}℃").format(sv=sv, delta=delta)
                    # 偏离设定值 >2℃:制冷未到位/失稳,暗场匹配会失准
                    tone = "warn" if abs(delta) > 2.0 else "good"
                except ValueError:
                    pass    # SET-TEMP 异常不连累 CCD-TEMP 展示
            pills.append((f"{tval:.1f}℃", tip, tone))

    offset = g("OFFSET")
    if offset:
        cam_pairs.append(("Offset", offset, _("(黑电平偏移)")))
    date_obs = g("DATE-OBS")
    if date_obs:
        try:
            dt = datetime.fromisoformat(date_obs.strip())
            if dt.tzinfo is None:       # FITS 惯例:无时区标注即 UTC
                dt = dt.replace(tzinfo=timezone.utc)
            local = dt.astimezone()
            tim.append((_("开始曝光"), local.strftime(_("%Y-%m-%d %H:%M:%S (本地)"))))
        except ValueError:
            tim.append((_("开始曝光"), f"{date_obs} (UTC)"))
    cam = g("INSTRUME")
    if cam:
        cam_pairs.append((_("相机"), cam))

    # 观测夜(正午分界):同一目标目录跨夜累积,序号每次运行重置,靠夜次区分
    nkey = _night_of_name(entry.name)
    if nkey:
        tim.append((_("观测夜"), nkey, _("(正午分界)")))

    # 像元比例 / 视场:206.265 × 像元(µm) / 焦距(mm)。
    # 注意 XPIXSZ 按 FITS 惯例已含 binning(Bin2 时写 2×物理值),不能再乘 Bin
    focal = fnum("FOCALLEN")
    try:
        pixsz = fnum("XPIXSZ", "PIXSZ1", "PIXELSIZE")
        if pixsz and focal:
            b = binning or 1.0
            scale = 206.265 * pixsz / focal
            opt.append((_("像元比例"), f"{scale:.2f}″/px", f"(Bin{b:g})"))
            verdict, tone = _sampling_verdict(scale)
            opt.append((_("采样"), verdict, _("(经验值, 视宁 2~3″ 前提)"), False, tone))
            shape = fits.naxis if has_fits else ()
            if len(shape) >= 2 and shape[0] > 0 and shape[1] > 0:
                opt.append((_("视场"),
                            f"{shape[0] * scale / 3600:.2f}° × "
                            f"{shape[1] * scale / 3600:.2f}°"))
    except (ValueError, TypeError, ZeroDivisionError):
        pass

    scope = g("TELESCOP")
    dev = scope or ""
    if focal:
        dev = (dev + f" · {focal:g}mm").strip(" ·")
    if dev:
        opt.insert(0, (_("设备"), dev))
    # 口径 / 焦比(APERTURE/APTDIA 单位 mm)
    try:
        apt = fnum("APERTURE", "APTDIA")
        if apt and apt > 0:
            note = f"(F/{focal / apt:.1f})" if focal else ""
            opt.insert(1 if dev else 0, (_("口径"), f"{apt:g}mm", note))
    except (ValueError, TypeError, ZeroDivisionError):
        pass

    bayer = g("BAYERPAT")
    if bayer:
        cam_pairs.append(("Bayer", bayer))
    if info and info.angle_deg is not None:
        cam_pairs.append((_("旋转角"), _("{angle_deg}° (解析)").format(
            angle_deg=info.angle_deg)))

    # 徽章:帧类型 / 滤镜 / Bin>1 / 序号 / 夜次
    badges: list[tuple[str, str]] = []
    if info is not None:
        kind_word = info.kind
    else:
        raw_kind = (g("IMAGETYP") or "").strip()
        kind_word = raw_kind.split()[0].capitalize() if raw_kind else ""
    if kind_word in _KIND_CN:
        badges.append((_(_KIND_CN[kind_word]), kind_word.lower()))
    if filt:
        badges.append((_("滤镜 {filt}").format(filt=filt), "filter"))
    if binning and binning > 1:
        badges.append((f"Bin{binning:g}", "bin"))
    if info is not None and info.seq is not None:
        badges.append((f"#{info.seq:04d}", "seq"))
    if nkey:
        badges.append((_("夜 {0}").format(nkey[5:]), f"night:{nkey}"))

    groups = [
        (_GRP_TARGET, _("目标"), tgt),
        (_GRP_OPTICS, _("光学"), opt),
        (_GRP_CAMERA, _("相机"), cam_pairs),
        (_GRP_TIME, _("时间与位置"), tim),
    ]
    return title, sub, groups, badges, sky, pills


def _detail_text(entry: RemoteEntry, title: str | None, sub: str,
                 badges: list[tuple[str, str]], pills: list[tuple],
                 groups: list[tuple]) -> str:
    """详情面板 → 可粘贴的多行纯文本(纯字符串拼接,数据均已算好)。"""
    out: list[str] = [entry.name]
    if title:
        out.append(f"{title} — {sub}" if sub else title)
    if badges:
        out.append(" ".join(f"[{t}]" for t, _s in badges))
    if pills:
        out.append(" · ".join(p[0] for p in pills))
    for _glyph, name, pairs in groups:
        if not pairs:
            continue
        out.append(_("【{name}】").format(name=name))
        for item in pairs:
            note = item[2] if len(item) > 2 else ""
            out.append(f"  {item[0]}: {item[1]}" + (f" {note}" if note else ""))
    return "\n".join(out)




def file_groups(entry, host: str = "", image_size=None,
                extra=()) -> list[tuple[str, str, list[tuple]]]:
    """详情面板的「文件」「位置」两组 —— 与天文卡片并列的那半边。

    形状与 ``_astro_details`` 的 groups 一致 ``(glyph, 组名, [(键, 值), ...])``,
    所以两边可以用同一个渲染循环、也能一起丢给 :func:`_detail_text`。

    这一份原来只存在于老 UI 的 ``_update_detail`` 里(那是**冻结**的模块),
    新前端因此整整少了两组:类型/大小/**尺寸**/修改/创建/属性,以及完整路径。
    大小要给**精确字节数**(``49.78 MB (52,194,240 字节)``)—— 判断一批 sub
    是不是同一设置拍的,字节数比人类可读那个准。
    """
    kind = _("目录") if entry.is_dir else ext_category(entry)
    fpairs: list[tuple] = [(_("类型"), kind)]
    if not entry.is_dir:
        fpairs.append((_("大小"), _("{0} ({size:,} 字节)").format(
            human_size(entry.size), size=entry.size)))
    if image_size:
        fpairs.append((_("尺寸"), f"{image_size[0]} × {image_size[1]}"))
    fpairs += [
        (_("修改"), format_mtime(entry.mtime)),
        (_("创建"), format_mtime(entry.ctime)),
        (_("属性"), f"{entry.attr_text()} (0x{entry.attributes:02X})"),
    ]
    unc = f"\\\\{host}\\{entry.share}" if host else entry.share
    lpairs: list[tuple] = [
        (_("路径"), unc + (f"\\{entry.path}" if entry.path else "")),
    ]
    lpairs += list(extra or ())
    return [(_GRP_FILE, _("文件"), fpairs), (_GRP_PLACE, _("位置"), lpairs)]


def night_colors(entries) -> dict[str, int]:
    """全视图统一分配 夜次 → 色号:按夜次日期排序取色,同夜同色、邻夜不同色。

    纯文件名解析(µs 级),渲染前一次算完,逐行只查表。
    """
    keys = set()
    for e in entries:
        if e.is_dir:
            continue
        k = _night_of_name(e.name)
        if k:
            keys.add(k)
    return {k: i for i, k in enumerate(sorted(keys))}


def night_chip(entry, colors: dict[str, int]) -> tuple[str, int] | None:
    """行内夜次徽章 → (「月-日」文本, 色号);目录/无时间戳文件返回 None。

    色号取不到就按出现顺序补 —— 搜索结果是流式追加的,`night_colors` 那一次
    预分配看不到后来的项。
    """
    if entry.is_dir:
        return None
    key = _night_of_name(entry.name)
    if not key:
        return None
    idx = colors.get(key)
    if idx is None:
        idx = len(colors)
        colors[key] = idx
    return key[5:], idx % len(_NIGHT_PALETTE)


def night_palette_argb(idx: int) -> tuple[str, str]:
    """色号 → (底色, 字色),`#AARRGGBB`。"""
    bg, fg = _NIGHT_PALETTE[idx % len(_NIGHT_PALETTE)]
    return ("#FF%02X%02X%02X" % bg, "#FF%02X%02X%02X" % fg)


def tone_argb(tone: str | None) -> str | None:
    """语义色名 → `#AARRGGBB`;认不出返回 None(表示"用默认前景色")。"""
    rgb = _TONE_RGB.get(tone or "")
    return None if rgb is None else "#FF%02X%02X%02X" % rgb


# ---------------------------------------------------------------- 行模型(新前端)

#: 类别 → 行首符号。**必须是 BMP 且字体无关。**
#: 老 UI 用的是 Segoe Fluent 私用区码位(`glyph_for`),那在 macOS/Linux 上是
#: 一串豆腐块;而 emoji 是星平面字符,win32more 那侧会按码点数给 HSTRING 长度、
#: 让字符串末尾少一个字 —— 两条都躲开,只用几何符号。
_CATEGORY_SYMBOL = {
    "文件夹": "▣",
    "图像": "◉",
    "缩略图/图片": "◍",
    "文本/日志": "▤",
}
_SYMBOL_FALLBACK = "▢"


def entry_symbol(entry) -> str:
    # **用身份不用显示文本** —— 翻译之后 `ext_category` 与表里的键对不上,
    # 每一行都会退回兜底方块,而且不报错
    return _CATEGORY_SYMBOL.get(ext_category_id(entry), _SYMBOL_FALLBACK)


def entry_key(entry) -> str:
    """行的稳定身份。

    **不能用下标,更不能用 `id(对象)`。** 下标随增删行漂移,而对象一旦序列化
    过界身份就没了 —— 老 UI 里 `id(run)`/`run is self._sel_run` 那套在这边
    一条都用不了。共享内相对路径在一次列目录里唯一,正好当键。
    """
    return entry.path or entry.name


def row_cells(entry, colors: dict[str, int], sub: str | None = None) -> list[dict]:
    """一行五个 cell:符号 · 夜次徽章 · 名字(+副行) · 大小 · 时间。

    **列的含义只存在于这里。** C# 那侧只看见 cols 宽度和一串排版属性,
    它不知道第三列是文件名 —— 换一张表不需要碰渲染器。
    """
    chip = night_chip(entry, colors)
    chip_cell: dict = {"text": ""}
    if chip is not None:
        text, idx = chip
        bg, fg = night_palette_argb(idx)
        chip_cell = {"text": text, "bg": bg, "color": fg, "size": 10.0}

    name_cell: dict = {"text": entry.name}
    if entry.is_dir:
        name_cell["weight"] = "semibold"     # 文件夹加粗以区分
    if len(entry.name) > 30:
        name_cell["tip"] = entry.name
    if sub:
        name_cell["sub"] = sub

    size_text = _("… 项") if entry.is_dir else human_size(entry.size)
    return [
        {"text": entry_symbol(entry), "opacity": 0.7},
        chip_cell,
        name_cell,
        {"text": size_text, "size": 12.0, "opacity": 0.7, "align": "right"},
        {"text": format_mtime(entry.mtime), "size": 12.0, "opacity": 0.7},
    ]


#: 列宽,与 `row_cells` 一一对应。`"*"` = 剩余空间。
#:
#: **固定列的总宽决定名字列还剩多少。** 老 UI 的窗口是 1750 宽,那套
#: (26/52/*/110/140) 在 1280 上会把名字列挤到只剩几十像素 —— 名字才是这张表
#: 最该看的一列,固定列得让位。
#: 列宽。第一列是类型符号 —— **30 不是 22**:老 UI 的 `FontIcon` 墨迹量出来
#: 17×16 px,而 22px 的列减去两边内边距只剩 10px,符号要么被挤扁要么被省略号
#: 顶掉(独立验收量到 Qt 只有 9×9,"明显小于文字的一个小点")。
ROW_COLS: list = [30, 52, "*", 84, 132]
#: 名字列的下标 —— 副行懒加载要就地改它。**不要写成裸的 2**:
#: 列一调整,补上来的增益/温度就会去改夜次徽章那一列(不报错,只是显示错)。
NAME_COL = 2


def astro_subline(entry, extra: str = "") -> str | None:
    """名字下那一行:文件名解析即时可得,FITS 头信息稍后补。非天文文件返回 None。"""
    info = parse_image_name(entry.name)
    if info is None:
        return extra or None
    parts: list[str] = []
    # **目标与类型二选一**(老 UI 就是这么排的):有目标名时类型是冗余的,
    # 而副行那一行寸土寸金 —— 挤掉的正是下面滤镜/序号那几段。
    if info.target:
        parts.append(info.target)
    else:
        parts.append(_(_KIND_CN.get(info.kind, info.kind)))
    if info.exposure_s is not None or info.exposure:
        parts.append(_fmt_exposure(info.exposure_s, info.exposure))
    # 滤镜槽位 / Bin / 序号 —— 这三段原来整个没有,而它们正是**区分同一目标
    # 不同批次**要看的东西(独立验收点名的那条)。
    # 注意滤镜字段(4C/Dul/1)是**滤镜轮槽位不是温度**(docs/DEVELOPMENT.md §4.6)。
    if info.filter:
        parts.append(info.filter)
    if info.binning != 1:
        parts.append(f"Bin{info.binning}")
    if info.seq is not None:
        parts.append(f"#{info.seq:04d}")
    if extra:
        parts.append(extra)
    return " · ".join(p for p in parts if p) or None


def detail_rows(groups) -> list[tuple[str, str, str, str | None]]:
    """分组键值 → 扁平的 (组名, 标签, 值, 语义色) 列表,供页面直接铺。

    组内元素长度不定(标签, 值[, 副注[, 等宽[, 语义色[, 小组件]]]]),
    这里把它规整成定长四元组 —— 页面就不必再解一次可变元组。
    """
    out: list[tuple[str, str, str, str | None]] = []
    for _glyph, name, pairs in groups:
        for item in pairs:
            note = item[2] if len(item) > 2 else ""
            tone = item[4] if len(item) > 4 else None
            value = f"{item[1]} {note}".strip()
            out.append((name, str(item[0]), value, tone_argb(tone)))
    return out


def child_count_text(ndir: int, nfile: int) -> str:
    """目录行的"… 项" → 真数字。

    分开写目录数与文件数,而不是笼统一个总数:浏览 ASIAIR 时"这个目标下有几个
    夜次子目录"和"这个夜次下有几张片子"是两件不同的事。
    """
    if ndir and nfile:
        return _("{ndir} 夹 {nfile} 文件").format(ndir=ndir, nfile=nfile)
    if ndir:
        return _("{ndir} 个目录").format(ndir=ndir)
    return _("{nfile} 个文件").format(nfile=nfile) if nfile else _("空")


def site_latlon(site: dict | None, lon_estimate: float | None = None):
    """站点配置 → `(纬度, 经度)`;拿不到返回 None。

    **`load_site()` 返回的是 dict,而 `_astro_details` 要的是元组。** 中间这层
    转换在老 UI 里叫 `_site_latlon`,抽取视图模型时漏了 —— 于是详情面板里
    `site[0]` 直接 KeyError,而调用点外正好包着 except,症状是**天文卡片
    整块静默不出现**(坐标/高度角/方位/气量/光学四组全没了),不报任何错。
    和 B8 那次漏 import 是同一种病。

    经度**优先用日志推算值**:纬度没法从日志推(由用户设),而经度可以由
    PHD2 段头时角 + 同时刻目标 RA 反推出来,那个值比用户随手填的准。
    """
    try:
        lat = float((site or {}).get("lat", 30.0))
        lon = float((site or {}).get("lon", 120.0))
    except (TypeError, ValueError, AttributeError):
        return None
    if lon_estimate is not None:
        try:
            lon = float(lon_estimate)
        except (TypeError, ValueError):
            pass
    return lat, lon
