"""FITS 查看器的**视图模型**:拉伸参数、直方图降采样、星点叠加坐标。

这一页的大件是**图像本身**,而它本来就不过界:老 UI 把拉伸结果渲染成磁盘上的
BMP,UI 线程只 `BitmapImage(file_uri(path))`。新前端同理 —— 走 ResourceRef
传引用,**二进制永不进 JSON**。

为什么是 BMP 不是 PNG(老 UI 记的账,照搬):0.02s vs 0.3~0.5s,拖动拉伸滑杆
时这个差距决定了能不能实时预览。单通道用 mode="L" 存 8bpp 灰度,
6248x4176 从 78MB 降到 26MB。

文件名里编进了拉伸参数指纹(`params.fingerprint()`),所以**跨界的缓存失效
天然正确** —— 参数变了就是另一个文件,ResourceRef 的 rev 直接用它。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np

from astro_smb.fitsimage import StretchParams
from astro_smb.i18n import gettext as _
from astro_smb_app.preview import cache_dir

# 匹配星标记的填充色:半透明绿(#AARRGGBB)。**不能用全透明** —— 我第一版
# 写成 #00000000,标记画了但一个也看不见。
MARK_FILL = "#6633DD66"
# ComboBox 索引 → StretchParams.mode(顺序与 fitsview.xaml 的 ModeBox 一致)
_MODES = ("stf", "asinh", "percentile")
_RENDER_DEBOUNCE = 0.16     # 秒:滑杆停手多久后才真渲染(全图约 100ms,别每 tick 都跑)
_PROGRESS_TICK = 0.12       # 秒:下载进度回 UI 的最小间隔
_HIST_BINS = 256
_HIST_POINTS = 96           # 曲线下采样后的点数(PointCollection.Append 约 67µs/次)
_KEEP_RENDERS = 8           # cache/fitsview 目录里保留的位图**数量**上限
_RENDER_BUDGET = 200 << 20  # 同上,**字节**上限(单张全分辨率彩色位图就有 19~78MB)
# 直方图三通道配色(浅深两主题下都够亮)
_CH_RGB = ((0xE0, 0x5A, 0x5A), (0x4C, 0xAF, 0x50), (0x54, 0x93, 0xF0))
def _matched_to_display(matched_xy, raw_width: int, raw_height: int,
                        display_width: int, display_height: int,
                        flip_vertical: bool) -> np.ndarray:
    """FITS 1-based/y 向上匹配坐标 → 查看器 0-based/y 向下坐标。"""
    points = np.asarray(matched_xy, dtype=np.float64)
    if points.size == 0:
        return np.empty((0, 2), dtype=np.float64)
    points = points.reshape(-1, 2)
    sx = float(display_width) / max(1, int(raw_width))
    sy = float(display_height) / max(1, int(raw_height))
    out = np.empty_like(points)
    out[:, 0] = (points[:, 0] - 1.0) * sx
    out[:, 1] = ((raw_height - points[:, 1]) if flip_vertical
                 else (points[:, 1] - 1.0)) * sy
    return out
def _render_dir() -> Path:
    d = cache_dir() / "fitsview"
    d.mkdir(parents=True, exist_ok=True)
    return d
def _prune_renders(keep: int = _KEEP_RENDERS,
                   budget: int = _RENDER_BUDGET) -> None:
    """渲染位图不进 preview.clear_cache 的账(它只扫顶层文件),自己清。

    **数量和字节数两个闸门取严者**:一张 6248×4176 的彩色位图 78MB,只按
    "留 8 张"算能占到 626MB —— 比整个预览缓存的预算还大。
    """
    try:
        files = []
        for f in _render_dir().iterdir():
            try:
                st = f.stat()
            except OSError:
                continue
            if f.is_file():
                files.append((st.st_mtime, st.st_size, f))
        files.sort(key=lambda t: t[0], reverse=True)
        total = 0
        for i, (_mt, size, f) in enumerate(files):
            total += size
            if i < keep and total <= budget:
                continue
            try:
                f.unlink()
                total -= size
            except OSError:
                pass        # 正被 BitmapImage 占着的文件删不掉,下次再说
    except OSError:
        pass
def _downsample_peak(y: np.ndarray, npts: int) -> np.ndarray:
    """把曲线降到约 ``npts`` 点,**每段取最大值**(直方图的尖峰不能被抹平)。"""
    n = int(y.shape[0])
    if npts <= 0 or n <= npts:
        return y
    edges = np.linspace(0, n, npts + 1).astype(np.intp)
    edges = np.unique(edges[:-1])
    return np.maximum.reduceat(y, edges)


# ------------------------------------------------ 从 Uno 前端下沉过来的帮手
#
# 这五支原来住在 Uno 前端的私有模块里(该前端 2026-08-03 已删),
# 跟协议/子进程缠在一起,别的前端 import 不了。于是第三套前端要么重写一份
# (判读口径迟早分叉),要么这一页干脆不做。下沉到共享层,三套前端共用。

def fits_astro(share: str, path: str, hdr, lon: float | None = None) -> dict:
    """天文判读卡 —— **和浏览页详情用同一份** `views.browser._astro_details`。

    这一层值钱在**判读**不在取值:气量用的是 Pickering (2002),高度角/采样的
    几个语义色阈值都是有前提的经验值。写第二份迟早在某次"顺手调阈值"时分叉,
    而分叉后两页给出不同判读、谁都不知道哪个对。
    """
    from astro_smb.client import RemoteEntry
    from astro_smb_app import logstore as ls
    from astro_smb_app.views import browser as bv

    entry = RemoteEntry(share=share, path=path,
                        name=path.rsplit(chr(92), 1)[-1], is_dir=False,
                        size=0, mtime=0.0, ctime=0.0, atime=0.0, attributes=0)
    try:
        site = bv.site_latlon(ls.load_site())
        # **经度优先用日志反推值。** 只读 `site.json` 的话,同一台机器、
        # 同一张 IC 4603,浏览页写「方位 182°」而这一页写「180°」——
        # 浏览页与老 UI 的 fitsview 都带 `lon_estimate`,只有这里没带。
        if site is not None and lon is not None:
            site = (site[0], float(lon))
    except Exception:
        site = None
    title, sub, groups, badges, _sky, pills = bv._astro_details(entry, hdr, site)
    if title is None:
        return {}
    return {"title": title, "sub": sub, "badges": [b[0] for b in badges],
            "pills": [p[0] for p in pills],
            "rows": bv.detail_rows(groups)}


def fits_structure(geom, img) -> list[tuple[str, str]]:
    """影像结构 —— 照老 UI 那一组。

    **Bayer 相位要报"头里写的 → 实际的"两个值。** 行序翻转会把相位也翻过来,
    只报一个的话,去马赛克出问题时根本对不上账。
    """
    from astro_smb.util import human_size

    out = [
        (_("原始尺寸"), f"{geom.width} × {geom.height}"
         + (f" × {geom.planes}" if geom.planes > 1 else "")),
        (_("位深"), f"BITPIX {geom.bitpix}"
         + (_("(有符号整数)") if geom.bitpix == 16 else "")),
        (_("刻度"), f"BSCALE {geom.bscale:g} · BZERO {geom.bzero:g}"
         + (_("(还原成 0~65535)") if geom.bzero == 32768.0 else "")),
        (_("行序"), _("自底向上(已翻转)") if geom.flip_vertical else _("自顶向下(原样)")),
    ]
    if geom.bayer_raw:
        note = (_("(偏移 {0},{1})").format(geom.bayer_offset[0], geom.bayer_offset[1])
                if geom.bayer_offset != (0, 0) else _("(翻转后的实际相位)"))
        out.append((_("Bayer 相位"),
                    f"{geom.bayer_raw} → {geom.bayer_effective} {note}"))
    out.append((_("显示尺寸"), f"{img.width} × {img.height}"
                + (_("(超像素去马赛克)") if img.debayered else "")))
    out.append((_("通道"), _("RGB 彩色") if img.channels >= 3 else _("单色")))
    out.append((_("数据区"),
                _("偏移 {data_offset:,} · {0}").format(
                    human_size(geom.data_bytes), data_offset=geom.data_offset)))
    return out


def solve_text(res) -> str:
    """解算结果 → 一段话。

    **失败要说清是哪种失败。** `SolveResult.reason` 分了类,而"没搜到"和
    "解不出来"是两回事 —— 盲解对窄视场本来就不实用(docs/DEVELOPMENT.md §12),
    失败消息里带上覆盖比例,别让人把"预算只覆盖了 0.3%"读成"这张图解不出"。
    """
    if not getattr(res, "ok", False):
        return _("解算失败:{0}").format(getattr(res, 'message', '') or res.reason)
    w = res.wcs
    bits = [_("匹配 {n_match} 星").format(n_match=res.n_match), _("耗时 {elapsed_s:.1f}s").format(
        elapsed_s=res.elapsed_s)]
    if w is not None:
        from astro_smb import astro
        # **`pixel_scale`/`rotation_deg`/`flipped` 都是方法不是属性。**
        # 当成属性用会拿到一个 bound method(真值恒为真、格式化成 <bound...>),
        # 和 `type_stats` 那个坑是同一种 —— 不报错,只是显示成一串垃圾。
        ra_deg, dec_deg = w.crval
        bits.insert(0, _("中心 {0} {1}").format(
            astro.format_ra(ra_deg), astro.format_dec(dec_deg)))
        bits.append(_("像元 {0:.2f}″/px").format(w.pixel_scale()))
        # **场旋的符号不能直接和正演比**:ASIAIR light 帧恒为镜像,
        # 镜像把旋向整个翻过来(docs/DEVELOPMENT.md §12)。所以只报角度,不下结论。
        bits.append(_("场旋 {0:.1f}°").format(w.rotation_deg())
                    + (_("(镜像)") if w.flipped() else ""))
    # RMS 单列并注明口径 —— 它是"中心区域拟合得多好",**不是成功判据**
    if res.rms_px == res.rms_px:      # 非 nan
        bits.append(_("内点 RMS {rms_px:.2f}px").format(rms_px=res.rms_px))
    return " · ".join(bits)


def solve_rows(res, width: int = 0, height: int = 0) -> list[tuple[str, str]]:
    """解算结果 → **一行一项**(老 UI `_fill_solve_rows` 同一组)。

    `solve_text` 那一行串是给"复制全部信息"用的;界面上压成一行会丢掉
    几乎所有可判读的量,其中三个尤其要紧:

    * **离先验中心**:FITS 头里的 RA/DEC 是赤道仪编码器读数,与板解算
      中心恒差约 21′(docs/DEVELOPMENT.md §12)。这个数不是故障指标,但看不到它
      就没法判断"指向模型有没有同步回去"。
    * **旋转角走 ZWO 约定**:`TanWcs.rotation_deg` 是图像 +y 的位置角,
      而 ASIAIR 的 light 帧恒为镜像,镜像把旋向整个翻过来 ——
      报 `rotation_deg` 的话用户拿去和文件名里的 `276deg` 对会以为解错了。
    * **匹配 / 星点** 两个数要一起给:只报匹配数看不出"图上本来有多少"。
    """
    import math

    if not getattr(res, "ok", False):
        return [(_("结果"), _("未能解算: {0}(图上星点 {1} 颗)").format(
            getattr(res, 'message', '') or res.reason, getattr(res, 'n_stars', 0)))]
    from astro_smb import astro

    def finite(v) -> bool:
        return isinstance(v, (int, float)) and math.isfinite(float(v))

    rows: list[tuple[str, str]] = []
    w = res.wcs
    if w is not None:
        ra_deg, dec_deg = w.crval
        rows.append((_("中心"), f"{astro.format_ra(ra_deg)}  "
                             f"{astro.format_dec(dec_deg)}"))
        rows.append((_("像元比例"), f"{w.pixel_scale():.3f}″/px"))
        try:
            from astro_smb.platesolve import zwo_angle_from_cd

            rows.append((_("旋转角"),
                         _("{0:.2f}°(ZWO 约定)").format(float(zwo_angle_from_cd(w.cd)))))
        except Exception:                    # noqa: BLE001
            rows.append((_("场旋"), _("{0:.1f}°(图像 +y 位置角)").format(w.rotation_deg())))
        if width > 0 and height > 0:
            fw, fh = w.fov_deg(width, height)
            rows.append((_("视场"), f"{fw:.2f}° × {fh:.2f}°"))
        rows.append((_("镜像"), _("是") if w.flipped() else _("否")))
    rows.append((_("匹配 / 星点"),
                 f"{res.n_match} / {getattr(res, 'n_stars', 0)}"))
    if finite(res.rms_px):
        # **口径**必须说清:只统计收紧容差内的内点,是"中心区域拟合得多好",
        # 不是畸变大小,更不是成功判据
        rows.append((_("拟合残差"), _("{rms_px:.2f} px(中心区内点)").format(rms_px=res.rms_px)))
    if finite(getattr(res, "hint_offset_deg", float("nan"))):
        rows.append((_("离先验中心"), f"{res.hint_offset_deg * 60:.1f}′"))
    if finite(getattr(res, "star_fwhm_px", float("nan"))):
        value = f"{res.star_fwhm_px:.2f} px"
        if finite(getattr(res, "star_fwhm_arcsec", float("nan"))):
            value += f" / {res.star_fwhm_arcsec:.2f}″"
        rows.append((_("星点 FWHM"), value))
    if finite(getattr(res, "star_ellipticity", float("nan"))):
        rows.append((_("星点椭圆率"), f"{res.star_ellipticity:.3f}"))
    if finite(getattr(res, "star_theta_deg", float("nan"))):
        rows.append((_("拉伸方向"), _("{star_theta_deg:.1f}°(集中度 {star_theta_r:.2f})").format(
            star_theta_deg=res.star_theta_deg, star_theta_r=res.star_theta_r)))
    rows.append((_("用时"), f"{res.elapsed_s:.1f}s"))
    return rows


def fits_badges(hdr) -> dict:
    """FITS 头 → 顶部那几个徽章的文字。

    **抽成纯函数是为了能真的测到它。** 原来这几行埋在 `open_fits` 的工作
    线程里,要真下载一张 50MB 的图才会跑 —— 于是"曝光徽章直接 str 了
    EXPTIME"这种事只能靠肉眼看,真机上它就写着 `0.00100000004749745`。
    抽出来之前,把那一行改回 `str(...)` 是**抓不住的**(变异验过)。
    """
    gain = hdr.get("GAIN")
    return {
        "exposure": fmt_exp(hdr.get("EXPTIME")),
        "gain": _("增益 {gain}").format(gain=gain) if gain else "",
    }


def fmt_exp(raw) -> str:
    """FITS 头里的 EXPTIME → 人话。**与浏览页详情同一支函数**,不另写一份。"""
    from astro_smb_app.views.browser import _fmt_exposure

    if raw in (None, ""):
        return ""
    try:
        return _fmt_exposure(float(raw))
    except (TypeError, ValueError):
        return str(raw)

