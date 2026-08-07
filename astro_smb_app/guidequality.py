"""导星质量分析:**从拍摄结果倒推导星到底好不好**。

老 UI 把这块放在拍摄记录页的目标详情里(而不是 3D 工具里),因为它回答的是
"我这一晚拍的这个目标,导星拖了后腿没有" —— 那是看着具体那一夜的记录时
才会问的问题。

链路(全在工作线程):

1. 从 ``Plan/Light/<目标>/`` 里按时刻窗口挑出该夜的 sub,**均匀抽稀**;
2. 每张拿到 WCS 与**主镜星点形状**(FWHM/椭率/方向)——
   先查 metacache,没有就本机提星 + 板解算;
3. 与**同期 PHD2** 的 RMS/覆盖率配对,交给
   :func:`astro_smb.guidecheck.cross_validate` 做三证据交叉判读;
4. 同一夜多个目标的漂移速率**联合**反解极轴误差。

为什么要三条证据而不是直接信 PHD2:PHD2 报的是**导星相机自己**看到的残差,
它对"主镜实际被拖成什么样"是间接的。星点形状是主镜的直接证据,板解算中心
是绝对位置的直接证据 —— **三条链的分歧才是价值所在**,一致时反而没什么可说。

两条不能忘的口径(docs/DEVELOPMENT.md §12 记着):

* **FITS 头里的 RA/DEC 是赤道仪编码器读数**,与板解算中心恒差约 21′。
  凡是需要"实际指向"的一律用板解算中心。
* **场旋的符号不能直接和正演比** —— ASIAIR 的 light 帧恒为镜像,
  镜像把旋向整个翻过来。`cross_validate` 的两链对质因此**只比量级不比符号**。

单目标的极轴反解是**恰定**的(2 个方程 2 个未知数),残差恒为机器零 ——
模型错了也照样"完美拟合"。所以它推翻不了任何东西,只能当量级参考;
夜次级联合反解才第一次让残差有意义。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from astro_smb.i18n import gettext as _

#: metacache 数据种类名。**与 3D 天球页共用同一份缓存** —— 同一张 sub 解算
#: 一次就够,两处都能命中(一次板解算要拉 50MB 原图)。
WCS_KIND = "sky3dwcs"
#: payload 版本。改结构必须 +1,否则会读到旧结构。
#: **这个数字必须和老 UI 的 `FOOT_CACHE_V` 一致**,否则两边互相看不见对方的缓存。
WCS_CACHE_V = 3
#: 每个目标最多取几张(全取既慢又没有额外信息)
MAX_SUBS_PER_TARGET = 12
#: 单轮最多板解算几张 —— 每张要把 50MB 原图拉到本地
MAX_SOLVE_PER_RUN = 8
#: 失败结果的缓存寿命。星表刚下好 / 算法改了就该再试一次,
#: 否则一次失败会**永久**钉死这张 sub(缓存的本意是省时间,不是记仇)。
WCS_FAIL_TTL_S = 7 * 86400
#: 亮场目录
PLAN_LIGHT_DIR = "Plan\\Light"

#: 板解算串行化:一次要拉 50MB 原图 + 吃满 CPU,两个同时跑只会互相拖慢
_solve_lock = threading.Lock()


# ---------------------------------------------------------------- 挑 sub

def _entry_time(entry) -> float:
    """一张 sub 的时刻(unix 秒)。

    优先用**文件名时间戳**(设备本地时间的曝光结束时刻,与日志同源);
    解析不出才退回 mtime。前提是 PC 与设备同时区(§4.6 已有的同一条假设)。
    """
    from astro_smb.naming import parse_image_name

    parsed = parse_image_name(entry.name)
    if parsed is not None and parsed.time is not None:
        try:
            return parsed.time.timestamp()
        except (OverflowError, OSError, ValueError):
            pass
    return float(entry.mtime or 0.0)


def pick_subs(entries, ts0: float, ts1: float,
              limit: int = MAX_SUBS_PER_TARGET) -> list:
    """按时刻窗口过滤,再**均匀抽稀**到 limit 张。

    ``Plan/Light/<目标>/`` 是**跨夜累积**的(同一个目标拍了三晚就三晚的帧都在
    里面),不按窗口过滤会把昨夜的证据混进今夜。

    抽稀取**首尾 + 均匀间隔**而不是"前 N 张":要看的是整夜的漂移,
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


# ---------------------------------------------------------------- WCS

def wcs_to_payload(w, width: int, height: int, **extra) -> dict:
    """TanWcs + 图幅尺寸 → 可 JSON 化的 metacache payload。"""
    cd = w.cd
    d = {"v": WCS_CACHE_V, "ok": True,
         "crval": [float(w.crval[0]), float(w.crval[1])],
         "crpix": [float(w.crpix[0]), float(w.crpix[1])],
         "cd": [float(cd[0][0]), float(cd[0][1]),
                float(cd[1][0]), float(cd[1][1])],
         "w": int(width), "h": int(height)}
    d.update(extra)
    return d


def wcs_from_payload(d: dict):
    """payload → ``(TanWcs, width, height)``;结构不对/版本不符返回 ``None``。"""
    from astro_smb import wcs as _wcs

    if not isinstance(d, dict) or d.get("v") != WCS_CACHE_V or not d.get("ok"):
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
        return (_wcs.TanWcs(crval, crpix,
                            [[cd[0], cd[1]], [cd[2], cd[3]]]), width, height)
    except Exception:                  # noqa: BLE001 - 坏缓存当未命中
        return None


def cached_wcs(host: str, ent) -> dict | None:
    """metacache 命中(源指纹 = size+mtime)。失败结果也缓存,但会过期。"""
    from astro_smb_app import metacache

    try:
        hit = metacache.get(WCS_KIND, host, f"{ent.share}|{ent.path}",
                            src_size=ent.size, src_mtime=ent.mtime)
    except Exception:                  # noqa: BLE001
        return None
    if not isinstance(hit, dict) or hit.get("v") != WCS_CACHE_V:
        return None
    if hit.get("ok"):
        return hit
    try:
        age = time.time() - float(hit.get("ts", 0.0))
    except (TypeError, ValueError):
        return None
    return hit if age < WCS_FAIL_TTL_S else None


def catalog_ok() -> bool:
    """星表可用吗(要读磁盘校验,工作线程调用)。"""
    try:
        from astro_smb import catalog

        return bool(catalog.catalog_available())
    except Exception:                  # noqa: BLE001
        return False


def solve_wcs(client, host: str, ent, cancel=None) -> dict | None:
    """把原图拉到本地缓存后**本机板解算 + 提星**(一张几秒)。

    即使 FITS 头自带 WCS 也要跑这一趟:**主镜 FWHM / 椭率 / 方向只有本机提星
    才拿得到**,而那是"从拍摄结果倒推"的关键证据 —— 头里的 WCS 只给位置。

    缓存文件名与预览/FITS 查看器**同一套 cache_key**,50MB 只下一次、三处共用。
    成功与失败都写 metacache(失败带 TTL)。
    """
    with _solve_lock:
        return _solve_locked(client, host, ent, cancel)


def _solve_locked(client, host: str, ent, cancel) -> dict | None:
    from astro_smb import platesolve
    from astro_smb_app import metacache
    from astro_smb_app.preview import (cache_dir, cache_key, clear_cache,
                                       download_cached)

    dest = cache_dir() / f"{cache_key(host, ent)}.fit"
    key = f"{ent.share}|{ent.path}"
    try:
        # 下载前裁一次缓存,**且只在本文件尚不存在时裁** —— 否则会把马上要读的
        # dest 自己删掉。应用会常开一整夜,只在启动时裁守不住上限。
        if not dest.exists():
            try:
                clear_cache(drop_dragout=False)
            except OSError:
                pass
        download_cached(client, ent.share, ent.path, dest, cancel=cancel)
        res = platesolve.solve_file(str(dest), name=ent.name, cancel=cancel)
    except InterruptedError:
        raise
    except Exception as ex:            # noqa: BLE001
        payload = {"v": WCS_CACHE_V, "ok": False, "ts": time.time(),
                   "err": f"{type(ex).__name__}: {ex}"}
        try:
            metacache.put(WCS_KIND, host, key, payload,
                          src_size=ent.size, src_mtime=ent.mtime)
        except Exception:              # noqa: BLE001
            pass
        return payload
    if res is None or not res.ok or res.wcs is None:
        payload = {"v": WCS_CACHE_V, "ok": False, "ts": time.time(),
                   "reason": str(getattr(res, "message", "")
                                 or getattr(res, "reason", "") or _("解算失败"))}
    else:
        # **图幅尺寸在 `res.hint` 上,不在结果上。** 拿不到就画不出边界,
        # 而且这种 payload 会被 `wcs_from_payload` 当坏数据静默丢掉 ——
        # 于是每次重开都白解算一遍。所以宁可标成失败。
        size = (res.hint.image_size if res.hint is not None else None) or (0, 0)
        if int(size[0]) <= 0 or int(size[1]) <= 0:
            payload = {"v": WCS_CACHE_V, "ok": False, "ts": time.time(),
                       "reason": _("解算成功但拿不到图幅尺寸(NAXIS 缺失)")}
        else:
            payload = wcs_to_payload(
                res.wcs, int(size[0]), int(size[1]),
                src="solve", sip=False, ts=time.time(),
                nmatch=int(res.n_match), rms=float(res.rms_px),
                focal=(res.hint.focal_len_mm if res.hint is not None else None),
                star_fwhm_px=float(res.star_fwhm_px),
                star_fwhm_arcsec=float(res.star_fwhm_arcsec),
                star_ellipticity=float(res.star_ellipticity),
                star_theta_deg=float(res.star_theta_deg),
                star_theta_r=float(res.star_theta_r))
    try:
        metacache.put(WCS_KIND, host, key, payload,
                      src_size=ent.size, src_mtime=ent.mtime)
    except Exception:                  # noqa: BLE001
        pass
    return payload


#: 图幅边界的等分段数(每边)
FOOT_EDGE_STEPS = 12


def footprint_ring(w, width: int, height: int,
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


def build_row(ent, payload: dict) -> dict | None:
    """(目录项, WCS payload) → 一条证据行。

    这里**不算足迹环** —— 那是 3D 天球页画视场框要的,质量判读用不上,
    而每张算一圈边界采样是白花的时间。
    """
    res = wcs_from_payload(payload)
    if res is None:
        return None
    w, width, height = res
    try:
        from astro_smb import wcs as _wcs

        ra, dec = _wcs.pixel_to_world(w, (float(width) + 1.0) / 2.0,
                                      (float(height) + 1.0) / 2.0)
        rot = float(w.rotation_deg())
        scale = float(w.pixel_scale())
    except Exception:                  # noqa: BLE001 - 退化的 CD:宁可少一张
        return None
    return {"file": ent.name, "share": ent.share, "path": ent.path,
            "ts": _entry_time(ent),
            "ra": float(ra) % 360.0, "dec": float(dec),
            "rot": rot, "scale": scale,
            "src": payload.get("src", ""),
            "nmatch": payload.get("nmatch"), "rms": payload.get("rms"),
            "focal": payload.get("focal"),
            "star_fwhm_px": payload.get("star_fwhm_px"),
            "star_fwhm_arcsec": payload.get("star_fwhm_arcsec"),
            "star_ellipticity": payload.get("star_ellipticity"),
            "star_theta_deg": payload.get("star_theta_deg"),
            "star_theta_r": payload.get("star_theta_r")}


def collect_footprints(client, targets, *, share: str = "", host: str = "",
                       cache: dict | None = None, on_progress=None,
                       cancel=None) -> list[dict]:
    """一夜的**实际视场框**。``targets`` 是 `views.sky3d` 给的目标列表。

    只用**已经算过**的 WCS(`cached_wcs`):足迹是"顺带看看",不值得为它
    去解算几十张 50MB 的图 —— 真要解算,用导星质量分析那条路,
    两边**共用同一份缓存**,解过的这里立刻就有。

    返回给 JS 的字段只留可序列化的那几个(`TanWcs` 留在 Python 侧)。
    """
    share = share or "EMMC Images"
    host = host or getattr(client, "host", "") or ""
    cache = cache if cache is not None else {}
    out: list[dict] = []
    for t in targets or ():
        name = str(t.get("name") or "")
        if not name:
            continue
        if cancel is not None and cancel.is_set():
            break
        if on_progress is not None:
            on_progress(_("正在读取「{name}」的实际视场").format(name=name))
        try:
            entries = client.listdir(share, PLAN_LIGHT_DIR + "\\" + name)
        except Exception:                  # noqa: BLE001
            continue
        subs = pick_subs(entries, float(t.get("ts0") or 0.0),
                         float(t.get("ts1") or 0.0))
        for ent in subs:
            payload = cache.get(ent.path) or cached_wcs(host, ent)
            if not (isinstance(payload, dict) and payload.get("ok")):
                continue
            cache[ent.path] = payload
            res = wcs_from_payload(payload)
            if res is None:
                continue
            w, width, height = res
            try:
                ring = footprint_ring(w, width, height)
            except Exception:              # noqa: BLE001 - 退化的 CD
                continue
            out.append({"id": f"{ent.share}|{ent.path}", "target": name,
                        "color": t.get("color", ""), "label": ent.name,
                        "ring": ring})
    return out


def dither_events(host: str, phd2_logs) -> list:
    """从**日志磁盘缓存**读 dither 指令。

    dither 是人为的抖动,不剔掉的话它会被算进"导星不稳" —— 那是最容易
    把一晚好数据误判成坏数据的一条。
    """
    from astro_smb.guidecheck import dither_from_log_text
    from astro_smb_app.logstore import logs_cache_dir

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


def quality_for(rows: list[dict], target: dict, phd2_logs, dither,
                lat_deg: float, lon_deg: float):
    """证据行 + 同期 PHD2 + 星点形状 → `guidecheck` 三证据交叉判读。"""
    import math

    import numpy as np

    from astro_smb import astro
    from astro_smb.guidecheck import FrameEvidence, cross_validate
    from astro_smb.phd2log import guide_coverage, rms_for_interval

    if len(rows) < 2:
        return None
    ordered = sorted(rows, key=lambda f: f["ts"])
    shots = [shot for run in target.get("runs", []) or []
             for shot in run.all_frames()]
    evidences = []
    for row in ordered:
        end = datetime.fromtimestamp(float(row["ts"]))
        shot = min(shots, key=lambda s: abs((s.end_time - end).total_seconds()),
                   default=None)
        if shot is not None:
            # 配不上就当配不上 —— 硬配一个远处的曝光会把两段无关的时间凑一起
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
        # **只收角秒口径**:像素口径混进来等于把两把不同的尺子加在一起
        guide_rms = (stats.rms_total if stats is not None and stats.in_arcsec
                     else None)

        def finite(name, _row=row):
            value = _row.get(name)
            return (float(value) if isinstance(value, (int, float))
                    and math.isfinite(float(value)) else None)

        evidences.append(FrameEvidence(
            t0=t0, t1=t1,
            fwhm_px=finite("star_fwhm_px"),
            fwhm_arcsec=finite("star_fwhm_arcsec"),
            ellipticity=finite("star_ellipticity"),
            theta_deg=finite("star_theta_deg"),
            theta_r=finite("star_theta_r"),
            n_stars=int(row.get("nmatch") or 0),
            center_ra=float(row["ra"]), center_dec=float(row["dec"]),
            pa_deg=float(row["rot"]),
            guide_rms_arcsec=guide_rms,
            guide_coverage=guide_coverage(phd2_logs, t0, t1)))
    scales = [float(f["scale"]) for f in rows
              if isinstance(f.get("scale"), (int, float))
              and float(f["scale"]) > 0]
    main_scale = float(np.median(scales)) if scales else 0.0
    main_focal = [float(f["focal"]) for f in rows
                  if isinstance(f.get("focal"), (int, float))
                  and float(f["focal"]) > 0]
    guide_focal = [
        float(sec.focal_len) for log in phd2_logs for sec in log.guide_sections
        if sec.focal_len is not None and sec.focal_len > 0
        and sec.end_time_effective >= evidences[0].t0
        and sec.begins <= evidences[-1].t1
    ]
    # 主镜与导星焦距几乎相同 ⇒ 同轴(OAG)。判读要分开:OAG 看不见的误差
    # 和导星镜看不见的误差是两码事(挠曲只在后者出现)。
    is_oag = None
    if main_focal and guide_focal:
        mf = float(np.median(main_focal))
        gf = float(np.median(guide_focal))
        is_oag = abs(mf - gf) / mf <= 0.05
    mid_ts = 0.5 * (ordered[0]["ts"] + ordered[-1]["ts"])
    mid_ra = float(np.median([f["ra"] for f in rows]))
    ha = ((astro.lst_deg(mid_ts, lon_deg) - mid_ra + 180.0) % 360.0) - 180.0
    return cross_validate(
        evidences, pixel_scale_main=main_scale, dither=dither,
        is_oag=is_oag, lat_deg=lat_deg, ha_deg=ha)


def night_polar(by_target: dict[str, list[dict]], lat_deg: float,
                lon_deg: float):
    """**当夜所有目标联合**反解极轴误差。

    单个目标只给 2 个方程解 2 个未知数 —— 残差恒为机器零,模型错了也照样
    "完美拟合"(掺入非极轴分量实测反解错 65%,残差仍是 4e-16)。所以按目标
    分别算出来的那个极轴数字**推翻不了任何东西**,只能当量级参考。

    把同一夜各目标的漂移速率凑成多组样本联合反解,残差才第一次有意义:
    残差小 = 单一极轴误差解释得通;残差大 = 现场还有别的机制。

    返回 ``(PolarCheck | None, 参与的目标名列表)``。
    """
    import numpy as np

    from astro_smb import astro
    from astro_smb.guidecheck import fit_center_drift, polar_from_runs

    samples, names = [], []
    for name, items in sorted(by_target.items()):
        rows = sorted(items, key=lambda f: f["ts"])
        if len(rows) < 3:              # 少于 3 张拟合不出可信的漂移速率
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
    except ValueError:                 # 反解出的量超出小角近似,不给数
        return None, []


def apply_night_polar(quality, check, names: list[str]) -> None:
    """把夜次级的联合反解结果盖到单目标判读上。

    联合结果**严格优于**单目标那个恰定解,所以直接替换,并把"恰定、
    看不出对错"那条告白换成真正有信息量的结论。
    """
    if check is None or quality is None:
        return
    note = (_("本夜 {0} 个目标({1})联合反解极轴偏差 {total_arcmin:.2f}′,残差 {rms:.3f}″/分").format(
        len(names), '、'.join(names), total_arcmin=check.polar.total_arcmin, rms=check.rms))
    if check.degenerate:
        note += _("(条件数 {cond:.0f},方位/高度分不开,只能看总量)").format(cond=check.cond)
    elif check.explained:
        note += _(" —— 残差很小,单一极轴误差解释得通这几个目标的漂移")
    else:
        note += (_(" —— **残差偏大**,单一极轴误差解释不了这几个目标的漂移,现场还有别的机制(挠曲、组件转动等)"))
    quality.polar, quality.polar_cond = check.polar, check.cond
    # 联合反解跨了多个目标 ⇒ 有残差可看 ⇒ 这个数字终于推翻得了
    quality.polar_falsifiable = True
    # **按相等剔除,不搜关键词** —— findings 会被翻译,搜「恰定」一翻就搜不到,
    # 那句误导性的告白会和新结论并排留着。
    # **别叫 note** —— 这个函数里 `note` 装的是上面刚拼好的"新结论",
    # 同名会把它盖掉,于是剔除了告白又把告白原样加回来。
    stale = getattr(quality, "polar_exact_note", "")
    quality.findings = [f for f in quality.findings
                        if not stale or f != stale]
    quality.findings.append(note)


# ---------------------------------------------------------------- 编排

def analyze(client, run, phd2_logs, lat_deg: float, lon_deg: float, *,
            share: str = "", host: str = "", on_progress=None, cancel=None):
    """一个目标的完整分析。**阻塞,必须在工作线程调用。**

    ``on_progress(text)`` 用来把"正在分析 3/8: xxx.fit"报回界面 ——
    这一步动辄几十秒,没有进度用户会以为卡死了。

    失败一律抛 ``ValueError`` 并带**人话**原因(缺星表 / 帧数不够 / 证据不足),
    因为这几种"没结论"用户要采取的行动完全不同。
    """
    share = share or "EMMC Images"
    host = host or getattr(client, "host", "") or ""
    target = _target_of(run)
    entries = client.listdir(share, PLAN_LIGHT_DIR + "\\" + target["name"])
    subs = pick_subs(entries, target["ts0"], target["ts1"])
    if len(subs) < 2:
        raise ValueError(_("至少需要两张该时段的原始 FITS 才能倒推质量"))
    cat_ok = catalog_ok()
    rows: list[dict] = []
    total = min(len(subs), MAX_SOLVE_PER_RUN)
    for i, ent in enumerate(subs[:total]):
        if cancel is not None and cancel.is_set():
            raise InterruptedError(_("导星质量分析已取消"))
        if on_progress is not None:
            on_progress(_("正在分析拍摄结果 {0}/{total}: {name}").format(
                i + 1, total=total, name=ent.name))
        payload = cached_wcs(host, ent)
        has_shape = (isinstance(payload, dict) and payload.get("ok")
                     and payload.get("star_fwhm_px") is not None)
        if not has_shape:
            if not cat_ok:
                continue
            # **即使 FITS 头自带 WCS 也要本机跑一次** —— 主镜 FWHM/椭率/方向
            # 只有提星才有,而那正是"从拍摄结果倒推"的关键证据。
            payload = solve_wcs(client, host, ent, cancel)
        if isinstance(payload, dict) and payload.get("ok"):
            row = build_row(ent, payload)
            if row is not None:
                rows.append(row)
    if cancel is not None and cancel.is_set():
        raise InterruptedError(_("导星质量分析已取消"))
    if len(rows) < 2:
        if not cat_ok:
            raise ValueError(_("缺少可用星表,无法从原始 FITS 提取主镜星点证据"))
        raise ValueError(_("成功分析的 FITS 少于两张,证据不足"))
    quality = quality_for(rows, target, phd2_logs,
                          dither_events(host, phd2_logs), lat_deg, lon_deg)
    if quality is None:
        raise ValueError(_("可用拍摄结果不足,无法形成导星质量结论"))
    return quality


def _target_of(run) -> dict:
    """``TargetRun`` → `quality_for` 要的那个 target 字典。"""
    span = None
    try:
        span = run.frame_span()
    except Exception:                  # noqa: BLE001
        span = None
    if span:
        ts0, ts1 = span[0].timestamp(), span[1].timestamp()
    else:
        b = getattr(run, "begin_time", None)
        e = getattr(run, "end_time", None) or b
        ts0 = b.timestamp() if b else 0.0
        ts1 = e.timestamp() if e else ts0
    # 积分总时长走 `integration_by_filter()` —— `TargetRun` 上**没有**
    # `total_exposure_s` 这个属性,写它的话 `getattr` 默认值一兜,
    # 平均曝光恒为 0,配不上曝光的那些帧会退化成 0.1 秒的窗口(不报错)。
    try:
        exposure = float(sum(run.integration_by_filter().values()))
    except Exception:                  # noqa: BLE001
        exposure = 0.0
    return {"name": getattr(run, "target", "") or "",
            "ts0": ts0, "ts1": ts1, "runs": [run],
            "exposure": exposure,
            "frames": getattr(run, "total_frames", 0) or 0}
