"""页面模型:``LogData`` / ``GuideSection`` → 纯 dict。

**为什么这一层在这里而不是在 ``astro_smb_app.views``。** 共享层给的是
"一个夜次怎么算"、"一段导星的 RMS 是多少"这一级的东西;而"这一页现在要显示
哪一夜、选中哪个目标、曲线窗口切到哪儿"那一层住在另一套前端的
Uno 前端里(2026-08-03 已删)—— 那是个跟协议/子进程缠在一起的模块,
不能 import。所以这些结构代码在这边重写了一遍;**判读一律仍走
``views.records`` / ``views.guiding``**,阈值和公式一个都没有复制过来。
(这是抽取层的一个缺口,已在报告里记一笔。)

放在独立模块而不是页面里,是为了**能脱离 QApplication 单测** ——
``tests/test_qt_models.py`` 直接喂 ``.tmp/`` 的真日志跑这些函数。
"""
from __future__ import annotations

import numpy as np

from astro_smb import astro
from astro_smb.i18n import gettext as _
from astro_smb_app.views import guiding as gv
from astro_smb_app.views import records as rv

#: 主曲线画布宽度。包络判据要按"每像素几帧"算,所以它是模型的输入之一。
CURVE_W = 900.0
#: 甘特条画布宽度
TIMELINE_W = 900.0

TONE_MAP = {"good": "ok", "warn": "warn", "bad": "bad", "err": "bad",
            "ok": "ok", "error": "bad"}


# ==================================================================== 拍摄记录

def night_list(data) -> list:
    """夜次列表,**倒序**(最近一夜在最上面)。"""
    if data is None or not getattr(data, "nights", None):
        return []
    return sorted(data.nights, key=lambda n: n.date, reverse=True)


def _night_rows(night, guide_map, *, merge: bool) -> list[dict]:
    """目标列表的行。``merge`` 决定平铺还是**按 Plan 分组**。

    两套布局都由 ``rv._night_layouts`` 算好(组头的统计、间隙行、缩进全在里面),
    这里只做形状转换。同一个目标被拆进几个 Plan 是常态(中途暂停、改参数),
    不合并的话一夜看起来像拍了五六个目标。

    行的键有前缀:``g:`` 组头、``x:`` 间隙 —— 它们**不是目标**,点了不该换详情。
    """
    layout = rv._night_layouts(night)
    items = layout["grouped" if merge else "flat"]
    order = {id(r): i for i, r in enumerate(night.runs)}
    rows: list[dict] = []
    for i, it in enumerate(items):
        kind = it.get("kind")
        if kind == "group":
            rows.append({"key": f"g:{it.get('key', i)}", "kind": "group",
                         "mark": "▾", "title": str(it.get("title", "")),
                         "sub": str(it.get("sub", "")), "tone": None,
                         "indent": 0.0})
        elif kind == "gap":
            rows.append({"key": f"x:{i}", "kind": "gap", "mark": "",
                         "title": str(it.get("text", "")), "sub": "",
                         "tone": None, "indent": float(it.get("indent", 0.0))})
        else:
            run = it.get("run")
            row = rv._run_row_data(run, guide_map)
            rows.append({
                "key": str(order.get(id(run), 0)), "kind": "run",
                "mark": row["mark"],
                "title": f"{row['time']} · {row['name']} · {row['plan']}",
                "sub": row["sub"],
                "tone": TONE_MAP.get(row["level"] or ""),
                "indent": float(it.get("indent", 0.0)),
            })
    return rows


def night_labels(nights) -> list[str]:
    """夜次下拉项。**带目标数与帧数** —— 老 UI 就是这样,而光有日期时
    "哪一夜拍得多"要一夜一夜点过去才知道。"""
    out = []
    for n in nights:
        runs = [r for r in n.runs]
        frames = sum(getattr(r, "total_frames", 0) or 0 for r in runs)
        out.append(_("{date} · {0} 目标 · {frames} 帧").format(
            len(runs), date=n.date, frames=frames))
    return out


def records_model(data, *, night_index: int = 0, selected: int = 0,
                  merge: bool = False, fits_map: dict | None = None,
                  site: tuple[float, float] | None = None) -> dict:
    """``LogData`` → 拍摄记录页模型。

    ``fits_map`` 是每个目标首张亮场的 FITS 头(``logstore.collect_fits_map``)。
    **原来这里硬写 `{}`**,于是共享层那半边全在、只是没人喂数据 ——
    夜次统计少「设备」整行、详情少「实测坐标(FITS)」一行、徽章少一枚滤镜。
    一个根因,四个看起来毫不相干的症状。
    """
    nights = night_list(data)
    if not nights:
        return {"nights": [], "runs": [], "detail": None, "night_index": 0}
    # **越界夹回,不能硬写 0** —— 日志重载后夜数可能变少,而选中还停在旧下标上;
    # 硬写 0 的症状是夜次下拉"点了没反应",而且不报任何错。
    idx = min(max(0, night_index), len(nights) - 1)
    night = nights[idx]
    guide_map = rv._guide_map_for([night], data.phd2_logs)
    runs = list(night.runs)

    rows = _night_rows(night, guide_map, merge=merge)

    spans, guides, ticks = timeline_spans(night, data.phd2_logs)
    fits_map = fits_map or {}
    detail = None
    if runs:
        detail = run_detail(runs[min(max(0, selected), len(runs) - 1)],
                            guide_map, fits_map)
    left, right = rv._night_summary(night, guide_map, fits_map)
    return {
        "nights": [n.date for n in nights],
        "night_index": idx,
        "night": night.date,
        "runs": rows,
        "merge": bool(merge),
        # **目标个数按 kind=run 数,不是行数** —— 合并计划下混着组头和间隙行。
        "target_count": sum(1 for r in rows if r.get("kind") == "run"),
        "spans": spans,
        "guides": guides,
        "ticks": ticks,
        "detail": detail,
        "summary_left": left,
        "summary_right": right,
        # 站点**从页面传进来** —— 它给巡天底图重投影用的是同一个
        # (日志反推的经度)。这里再去读一次 site.json 就会是两个数。
        "sky": sky_payload(night, site=site,
                           selected=(runs[min(max(0, selected),
                                              len(runs) - 1)]
                                     if runs else None)),
        "meta": records_meta(data, nights),
    }


def records_meta(data, nights) -> str:
    """顶行那段元信息:日志数 / 夜次数 / 站点。

    经度是**从日志反推**的(PHD2 段头时角 + 同时刻目标 RA),纬度只能靠用户设 ——
    天球图与高度角判读全靠这两个数,写在最显眼处才好核对。
    """
    from astro_smb_app.logstore import load_site

    site = load_site()
    lat = float(site.get("lat", 30.0))
    lon = data.lon_estimate if getattr(data, "lon_estimate", None) is not None \
        else float(site.get("lon", 121.0))
    bits = [_("{0} 份 Autorun 日志").format(len(getattr(data, 'autorun_logs', []) or [])),
            _("{0} 个夜次").format(len(nights)),
            _("站点 {0} {1}").format(rv._fmt_lat(lat), rv._fmt_lon(lon))]
    if getattr(data, "lon_samples", 0):
        bits.append(_("经度由 {lon_samples} 个样本反推").format(lon_samples=data.lon_samples))
    return " · ".join(bits)


def timeline_spans(night, phd2_logs) -> tuple[list[dict], list[dict], list[dict]]:
    """夜次 → 甘特条 + **真实导星区间** + 整点刻度(全部归一化到 ``[0,1]``)。

    **整个算式走 ``rv._night_timeline``,这里只做形状转换。** 原来这里自己
    又算了一遍,而且是有损的:

    * 导星只留了每个目标一个**覆盖率**,画出来是一条**从头连到尾的绿条** ——
      而真实情况是断续的(丢星、换目标、重新校准都会断)。用户报的就是这条:
      "底部绿线一直是连续的,老 UI 是不连续的,是实际的数据"。
    * 刻度按 ``span/steps`` 均分,于是标签是 22:24 / 23:11 这种;老 UI 是
      **整点**(23:00 / 00:00),对着日志看时间才对得上。
    * 一个目标的多个块被合成一条,**暂停/截断的半透明**因此丢了。

    返回 ``(bars, guides, ticks)``。
    """
    tl = rv._night_timeline(night, phd2_logs)
    if not tl:
        return [], [], []
    bars = []
    for i, (f0, f1, ci, alpha, lbl, tip, run) in enumerate(tl["bars"]):
        bars.append({"f0": f0, "f1": f1, "key": str(_run_index(night, run)),
                     "label": lbl, "tip": tip, "alpha": alpha,
                     "fill": rv._TL_PALETTE[ci % len(rv._TL_PALETTE)]})
    guides = [{"f0": f0, "f1": f1} for f0, f1 in tl["guides"]]
    ticks = [{"f": f, "label": lbl} for f, lbl in tl["ticks"]]
    return bars, guides, ticks


def _run_index(night, run) -> int:
    """块回指的 run → 它在这一夜里的下标(选中用的键)。

    **按身份找,不是按相等找。** `TargetRun` 是 dataclass,同一夜里两个
    目标同名同计划时 `==` 会撞上,选中会跳到另一个。
    """
    for i, r in enumerate(night.runs):
        if r is run:
            return i
    return 0


def run_detail(run, guide_map, fits_map: dict | None = None) -> dict:
    """目标详情。**整表直接用 ``rv._run_detail``** —— 手拼一份就是双实现,
    而且会丢掉 RA/DEC 分轴、峰值、丢星、AutoFocus 与好坏分级。"""
    row = rv._run_row_data(run, guide_map)
    det = rv._run_detail(run, guide_map or {}, fits_map or {})
    pairs = []
    for item in det.get("pairs") or ():
        value = str(item.get("v", ""))
        note = str(item.get("note") or "")
        # 副注折进值里 —— RA/DEC 分轴恰恰在副注里,丢掉它等于丢掉
        # "是 RA 还是 DEC 出问题"这条判读。
        # **`bar` 与 `level` 也要带上。** 原来只取 k/v/note:
        # 「帧数 33/30」「覆盖率 97%」在老 UI 各带一条量条(一眼看出够没够),
        # 结束方式/AutoCenter/导星RMS/丢星 各有语义色(一眼看出哪项不对) ——
        # 两样在 Qt 这边全成了同一种正文白。
        pairs.append({
            "k": str(item.get("k", "")),
            "v": f"{value}  {note}".rstrip() if note else value,
            "bar": item.get("bar"),
            "tone": TONE_MAP.get(str(item.get("level") or "")),
        })
    events = []
    # **不截断。** 原来是 `[:40]` —— 这一夜最长的 run 35 条,没触发;
    # 换一夜就会**静默丢事件**,而"丢了"这件事界面上一个字都不会说。
    # 老 UI 不截断,归并后通常也就 10~30 条。
    for item in (rv._timeline_items(run) or ()):
        if not isinstance(item, dict):
            events.append({"kind": "note", "title": str(item), "level": "info"})
            continue
        # **键是 `t0/title/subtitle`**,不是 `time/text/note`;读错了不会报错,
        # 只是拼出一串空字符串(详情下面挂着几十个一个字没有的文本节点)。
        t0i, t1i = item.get("t0"), item.get("t1")
        # **结构留着,不要在这里拼成一行。** 上一版把时刻/标题/副标题用 `·`
        # 串成一个字符串扔给页面 —— 于是 `level`(状态色)、`kind`(块边界是
        # 方旗、间隙是分隔线)、`progress`(实拍/计划的迷你进度条)三样全丢了,
        # 而清单 2.10 要的正是"结构化"。这跟导星页 3.9 是同一个病。
        events.append({
            "kind": str(item.get("kind") or "info"),
            "level": str(item.get("level") or "info"),
            "when": f"{t0i:%H:%M:%S}" if t0i else "",
            "when2": (f"~{t1i:%H:%M:%S}"
                      if (t1i and t0i and t1i != t0i) else ""),
            "title": str(item.get("title") or ""),
            "subtitle": str(item.get("subtitle") or ""),
            "progress": item.get("progress"),
        })
    # **属性叫 `begin_time`/`end_time`。** 写成 `run.start`/`run.t0` 会让
    # "看这段导星"这个按钮永远是灰的,而且不报任何错。
    b, e = getattr(run, "begin_time", None), getattr(run, "end_time", None)
    return {
        "title": row["name"], "sub": row["sub"],
        "coord": det.get("coord") or "",
        # 徽章**带着各自的样式**。老 UI 那五枚是分色的(绿/蓝/绿/紫/灰),
        # 原来 `for x, _style in ...` 把第二项扔了,五枚全变成同一个 accent
        # 描边 —— "这是什么"这条信息被抹平。
        "badges": [(str(x), str(style or "")) for x, style
                   in (det.get("badges") or ())],
        "pairs": pairs, "events": events,
        "target": row.get("name") or "",
        "t0": b.timestamp() if b else None,
        "t1": e.timestamp() if e else None,
    }


def sky_payload(night, frac: float | None = None, *,
                site: tuple[float, float] | None = None,
                selected=None) -> dict | None:
    """夜次中某个时刻各目标的地平位置。

    **整图必须同一时刻** —— 各点用各自拍摄时刻会与底图错位(老 UI 真机踩过
    "M 8 不在银心")。

    ``site=(lat, lon)`` **必须由调用方给**,而且要和巡天底图重投影用的是
    同一个。原来这里自己去读 `load_site()`,而页面给底图的是**日志反推的
    经度** —— 于是点和底图各用各的经度(实测 120.0 vs 121.366)。
    在 260px 的图上差不到 1px,看不出来,但那正是"M 8 不在银心"的同一形态。

    ``frac`` 是整夜窗口内的位置(0 = 入夜,1 = 收工);**给 None 时按老 UI 的
    口径**:选中目标的帧中点 > 夜次中点。换选中目标天球要跟着动 ——
    不动的话"这个目标当时在天上哪儿"这个问题它根本没在回答。
    """
    win = rv._night_window(night)
    if not win:
        return None
    if frac is None:
        mid = _sky_moment(night, win, selected)
        span = (win[1] - win[0]).total_seconds()
        frac = ((mid - win[0]).total_seconds() / span) if span > 0 else 0.5
        frac = min(1.0, max(0.0, frac))
    else:
        frac = min(1.0, max(0.0, float(frac)))
        mid = win[0] + (win[1] - win[0]) * frac
    if site is None:
        from astro_smb_app.logstore import load_site
        cfg = load_site()
        site = (float(cfg.get("lat", 30.0)), float(cfg.get("lon", 121.0)))
    lat, lon = float(site[0]), float(site[1])
    pts = []
    for run in night.runs:
        if not rv._sky_relevant(run):
            continue        # bias/dark-only 的坐标是**停机位**,不上天球
        ra = dec = None
        for b in run.blocks:
            if b.ra and b.dec:
                ra = astro.ra_str_to_deg(b.ra)
                dec = astro.dec_str_to_deg(b.dec)
                break
        if ra is None or dec is None:
            continue
        alt, az = astro.altaz(ra, dec, lat, lon, mid.timestamp())
        # 选中的那颗要**看得出来**:换个填充色 + 描边环。老 UI 是青色高亮,
        # 而这边两颗一模一样 —— 点了列表天球上毫无反应。
        pts.append({"alt": alt, "az": az, "label": run.target,
                    "selected": run is selected})
    if not pts:
        return None
    #  是**给巡天底图重投影用的同一个时刻**。点和底图各用各的时刻会错位,
    # 而错位在一张星图上几乎看不出来(老 UI 真机踩过"M 8 不在银心")。
    return {"at": f"{mid:%m-%d %H:%M}", "points": pts, "frac": frac,
            "ts": mid.timestamp()}


def _sky_moment(night, win, selected):
    """天球渲染时刻:**选中目标的帧中点 > 夜次中点**(老 UI `_sky_ts` 同款)。"""
    if selected is not None:
        try:
            span = selected.frame_span()
        except Exception:                  # noqa: BLE001
            span = None
        if span:
            return span[0] + (span[1] - span[0]) / 2
        begin = getattr(selected, "begin_time", None)
        if begin is not None:
            return begin
    return win[0] + (win[1] - win[0]) / 2


# ==================================================================== 导星

def guiding_rows(prep: dict, expanded: set[str], frag_open: set[str]) -> list[dict]:
    """分组段列表 → 一张平表。**键带前缀**:``g:`` 组头 / ``x:`` 碎段簇 /
    ``r:`` 数据行下标。

    折叠必须存在:真机 123 段里 103 段是几帧就结束的短尝试,真正想看的那几段
    会被埋在里面。
    """
    data_rows = prep.get("rows") or []
    out: list[dict] = []
    for g in prep.get("groups") or []:
        gkey = g["key"]
        open_ = gkey in expanded
        # 组头上的**两个数**:段数与合并 RMS。共享层一直在给
        # (`n_sec`/`rms`/`unit`),原来只取了 title+sub —— 于是老 UI 那两枚
        # 徽章「[2 段] [RMS 0.87″]」整个没了,只剩副行的颜色暗示。
        # **合并 RMS 是组头最值钱的那个数**:一眼看出这个目标整体导得稳不稳。
        head = f"{'▼' if open_ else '▶'} {g['title']}"
        n_sec = int(g.get("n_sec") or 0)
        if n_sec:
            head += _("  [{n_sec} 段]").format(n_sec=n_sec)
        rms_v = g.get("rms")
        if rms_v is not None:
            head += f"  [RMS {float(rms_v):.2f}{g.get('unit') or '″'}]"
        out.append({
            "key": f"g:{gkey}",
            "title": head,
            "sub": g.get("sub") or "",
            "tone": TONE_MAP.get(g.get("level") or ""),
            "group": True,
        })
        # **仪表盘入口单独一行**(老 UI 是组头右侧一颗按钮)。
        # 这一列是 `DataTable`,一行只有一个可点区域 —— 与其在组头里挤一个
        # 按不准的小热区,不如给它自己一行:点组头=展开,点这行=看聚合。
        # 只在**展开时**出现,收起来的组不该多一行。
        if open_:
            out.append({
                "key": f"d:{gkey}",
                "title": _("    ▤ 仪表盘 —— 这一组合起来看"),
                "sub": _("RMS 椭圆 · 周期误差 · 脉冲配比 · 逐段对比 · 每张 sub 的导星"),
                "tone": None, "group": False,
            })
        if not open_:
            continue
        for item in g.get("items") or ():
            if item["type"] == "row":
                ri = item["ri"]
                row = data_rows[ri]
                out.append({
                    "key": f"r:{ri}",
                    "title": "   " + str(row.get("main") or ""),
                    "sub": "   " + str(row.get("sub") or ""),
                    "tone": TONE_MAP.get(row.get("level") or ""),
                })
                continue
            fkey = item["key"]
            fopen = fkey in frag_open
            out.append({
                "key": f"x:{fkey}",
                # **文本在 `item["text"]`**,不是 title —— 读错了是一行空白
                "title": f"   {'▼' if fopen else '▶'} {item.get('text') or ''}",
                "sub": "",
                "tone": TONE_MAP.get(item.get("level") or ""),
            })
            if not fopen:
                continue
            for ri in item.get("ris") or ():
                row = data_rows[ri]
                out.append({
                    "key": f"r:{ri}",
                    "title": "      " + str(row.get("main") or ""),
                    "sub": "      " + str(row.get("sub") or ""),
                    "tone": TONE_MAP.get(row.get("level") or ""),
                })
    return out


def default_guide_row(data_rows: list[dict]) -> int:
    """默认选中哪一行。**优先主段** —— 第 0 行常是校准或几帧的短尝试,
    两者都画不出曲线,默认选中它等于打开就是一片空白。"""
    for i, r in enumerate(data_rows):
        if r.get("kind") == "guide" and r.get("main_seg"):
            return i
    for i, r in enumerate(data_rows):
        if r.get("kind") == "guide":
            return i
    return 0


def chart_payload(row: dict, *, window_index: int = 0, pos: float = 0.0,
                  width: float = CURVE_W) -> dict:
    """一段导星 → 主曲线数据。

    三条判读("改了不报错,只是悄悄退化"的那类):

    * **量程按整段算,不随窗口变**(``row["rng"]``)。缩到 5 分钟就重标定纵轴,
      两个窗口之间没法比。
    * **包络判据按窗口内帧数**,不是整段帧数 —— 用整段判会让 5 分钟窗口
      仍然显示包络带。
    * **丢星刻度先按窗口裁再均匀抽稀**,不能截前 N 个(那看着像"前半段一直丢星")。
    """
    if row.get("kind") != "guide" or row.get("npt") is None:
        return {}
    t = row["npt"]
    ra, dec = row["npra"], row["npdec"]
    if t is None or len(t) < 2:
        return {}
    dur = float(t[-1] - t[0])
    window_s = gv.WINDOW_CHOICES[min(max(0, window_index),
                                     len(gv.WINDOW_CHOICES) - 1)][1]
    t0, t1 = float(t[0]), float(t[-1])
    can_pan = False
    i0, i1 = 0, len(t)
    if window_s and window_s < dur:
        can_pan = True
        w0 = t0 + max(0.0, min(1.0, pos)) * (dur - window_s)
        i0 = int(np.searchsorted(t, w0))
        i1 = int(np.searchsorted(t, w0 + window_s))
        if i1 - i0 < 2:
            i0, i1 = 0, len(t)
            can_pan = False
        else:
            t0, t1 = float(t[i0]), float(t[i1 - 1])

    wt, wra, wdec = t[i0:i1], ra[i0:i1], dec[i0:i1]
    rng = float(row.get("rng") or 1.0)
    dense = len(wt) > gv.ENV_FRAMES_PER_PX * width

    out: dict = {
        "t0": t0, "t1": t1, "range": rng, "can_pan": can_pan,
        "dense": dense, "unit": row.get("unit") or "″",
        "title": row.get("title") or "", "stat": row.get("stats") or "",
        # 结构化版本 —— 右栏比老 UI 窄,一行 `·` 串在这里就是一堵墙,
        # 而这几个数是要**逐个对比**的(RA 大还是 DEC 大?峰值离均值多远?)
        "stat_rows": row.get("stat_rows") or [],
        "rms_chip": row.get("rms_chip") or "",
        "epoch": row["sec"].begins.timestamp() if row.get("sec") else None,
        "charts": row.get("charts"),
    }
    if dense:
        out["env"] = {
            "ra": _envelope(wt, wra, row.get("rms30ra"), i0, i1),
            "dec": _envelope(wt, wdec, row.get("rms30dec"), i0, i1),
        }
    else:
        out["ra"] = gv._downsample(list(zip(wt.tolist(), wra.tolist())))
        out["dec"] = gv._downsample(list(zip(wt.tolist(), wdec.tolist())))

    lost = [x for x in (row.get("lost") or ()) if t0 <= x <= t1]
    stride = len(lost) // gv.MAX_LOST_TICKS + 1
    out["lost"] = lost[::stride]
    return out


def _envelope(t, vals, rms30, i0: int, i1: int, buckets: int = 450) -> dict:
    """min/max 包络带 + 30 帧滑动 RMS 主线。

    缩出到"每像素两帧以上"时逐帧折线只是一团噪声 —— 老 UI 在这个密度上切成
    包络视图,这里照做(阈值 ``gv.ENV_FRAMES_PER_PX``)。
    """
    n = len(t)
    if n < 2:
        return {"t": [], "hi": [], "lo": [], "mid": []}
    k = min(buckets, n)
    edges = np.linspace(0, n, k + 1).astype(int)
    mid_src = rms30[i0:i1] if rms30 is not None and len(rms30) >= i1 else None
    ts, hi, lo, mid = [], [], [], []
    for a, b in zip(edges[:-1], edges[1:]):
        if b <= a:
            continue
        chunk = vals[a:b]
        ts.append(float(t[a]))
        hi.append(float(np.max(chunk)))
        lo.append(float(np.min(chunk)))
        mid.append(float(np.mean(mid_src[a:b])) if mid_src is not None else 0.0)
    return {"t": ts, "hi": hi, "lo": lo, "mid": mid}


def locate_range(prep: dict, t0: float, t1: float) -> int | None:
    """找与 ``[t0, t1]`` **重叠最多**的导星段(不是第一个碰上的)。

    从拍摄记录页跳过来时要看的是"这段 sub 曝光期间"导星什么样。找不到要
    明说,不能默默停在原地。

    ``t0/t1`` 是 unix 时间戳(记录页那边给的),而 ``gv._overlap_s`` 收的是
    **datetime** —— 直接把浮点喂进去会在 ``(hi - lo).total_seconds()`` 上
    AttributeError,而这条路径只有"从记录页点『看这段导星』"才走到。
    测试抓住了它。
    """
    from datetime import datetime

    lo = datetime.fromtimestamp(t0)
    hi = datetime.fromtimestamp(t1)
    best, best_ov = None, 0.0
    for i, row in enumerate(prep.get("rows") or []):
        if row.get("kind") != "guide":
            continue
        b = row["begins"]
        e = row.get("end") or b
        ov = gv._overlap_s(b, e, lo, hi)
        if ov > best_ov:
            best, best_ov = i, ov
    return best
