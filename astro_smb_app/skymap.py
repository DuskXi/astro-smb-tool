"""巡天底图:下载缓存 ESO 银河全景,并按站点+时刻重投影为 alt-az 极坐标图。

底图: ESO GigaGalaxy Zoom 全天全景 eso0932a(6000×3000, 等距柱状投影,
**银道坐标**,银心在图像中心,银经向左增,银纬向上)。CC BY 4.0,
使用须署名 —— UI 里常显 SURVEY_CREDIT。

重投影链(全部 numpy 向量化,公式与 astro_smb.astro 标量版一致,单测互查):
    输出极坐标像素 (x,y) → (alt, az)[北上东左] → (ha, dec) → (ra, dec)
    → 银道 (l, b) → 底图像素采样。地平线以下透明。

线程模型:``download_survey`` / ``render_altaz`` 都是阻塞调用,必须在工作
线程执行;结果是磁盘 PNG 路径,UI 线程用 BitmapImage 加载。
"""
from __future__ import annotations

import hashlib
import math
import os
import ssl
import threading
import urllib.request
from pathlib import Path

from astro_smb import paths
from astro_smb.i18n import N_, gettext as _

SURVEY_URL = "https://cdn.eso.org/images/large/eso0932a.jpg"
SURVEY_CREDIT = N_("底图: ESO/S. Brunier — GigaGalaxy Zoom (CC BY 4.0)")
SURVEY_SIZE_HINT = N_("约 8 MB")

# 底图方向约定(经自动化校验,见 tests 与 scripts 的 LMC 亮度校验):
# 银经 l 向左增(天图惯例,从地球内部看天球),银心 l=0 在图像中心
_L_INCREASES_LEFT = True


def skymap_dir() -> Path:
    base = paths.cache_root() / "skymap"
    base.mkdir(parents=True, exist_ok=True)
    return base


def survey_path() -> Path:
    return skymap_dir() / "eso0932a.jpg"


def survey_available() -> bool:
    try:
        return survey_path().is_file() and survey_path().stat().st_size > (1 << 20)
    except OSError:
        return False


def _ssl_context() -> ssl.SSLContext:
    """uv 独立构建的 Python 在 Windows 上不自动挂接系统证书库,
    这里从 ROOT/CA 存储补充根证书(纯标准库,不引入 certifi 依赖)。"""
    ctx = ssl.create_default_context()
    try:
        for store in ("ROOT", "CA"):
            for cert, enc, _trust in ssl.enum_certificates(store):
                if enc == "x509_asn":
                    try:
                        ctx.load_verify_locations(cadata=cert)
                    except ssl.SSLError:
                        pass
    except (AttributeError, OSError, PermissionError):
        pass
    return ctx


def download_survey(progress=None,
                    cancel: threading.Event | None = None) -> Path:
    """下载底图(约 8MB)到本地缓存,.part 原子落盘。progress(done, total)。

    urllib 证书验证失败时(uv 独立 Python 的 Windows 证书库缺链,实测发生)
    退回系统自带 curl.exe(Schannel,证书链完整)。
    """
    dest = survey_path()
    if survey_available():
        return dest
    tmp = dest.with_suffix(".part")
    try:
        try:
            _download_urllib(tmp, progress, cancel)
        except ssl.SSLCertVerificationError:
            _download_curl(tmp, cancel)
        _verify_jpeg(tmp)   # 截断下载(服务器提前 FIN 时 urllib 静默短读)不落盘
        os.replace(tmp, dest)
        return dest
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _verify_jpeg(path: Path) -> None:
    """完整性校验:截断/损坏的 JPEG 会让之后每次渲染都失败且无 UI 恢复入口。"""
    try:
        from PIL import Image
        with Image.open(path) as im:
            im.verify()
    except Exception as ex:
        raise OSError(_("底图文件不完整或已损坏: {ex}").format(ex=ex)) from ex


def _download_urllib(tmp: Path, progress, cancel) -> None:
    req = urllib.request.Request(
        SURVEY_URL, headers={"User-Agent": "astro-smb-tool/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=30,
                                    context=_ssl_context()) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            with open(tmp, "wb") as fh:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise InterruptedError(_("下载已取消"))
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    fh.write(chunk)
                    done += len(chunk)
                    if progress is not None:
                        progress(done, total)
            if total > 0 and done != total:
                # http.client 对提前 EOF 静默返回短读, 必须自查长度
                raise OSError(_("底图下载不完整: {done}/{total} 字节").format(
                    done=done, total=total))
    except urllib.error.URLError as ex:     # 解包 URLError 里的证书错误
        if isinstance(ex.reason, ssl.SSLCertVerificationError):
            raise ex.reason from ex
        raise


def _download_curl(tmp: Path, cancel) -> None:
    import subprocess
    proc = subprocess.run(
        paths.curl_argv("-sSL", "--max-time", "120",
         "-o", str(tmp), SURVEY_URL),
        capture_output=True, timeout=150,
        creationflags=paths.NO_WINDOW)
    if proc.returncode != 0 or not tmp.is_file() or tmp.stat().st_size < (1 << 20):
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise OSError(_("curl 下载底图失败: {0}").format(err or proc.returncode))


# ---------------------------------------------------------------- 重投影

def _altaz_to_lb_grid(size: int, lat_deg: float, lon_deg: float,
                      unix_ts: float):
    """输出网格 → (l, b, 地平线内掩码)。numpy 向量化,公式同 astro 标量版。"""
    import numpy as np

    from astro_smb.astro import lst_deg

    c = (size - 1) / 2.0
    r_px = size / 2.0
    yy, xx = np.mgrid[0:size, 0:size]
    nx = (xx - c) / r_px
    ny = (yy - c) / r_px
    r = np.hypot(nx, ny)
    mask = r <= 1.0

    alt = np.radians(90.0 * (1.0 - r))
    # 前向映射为 x = cx - r·sin(az), y = cy - r·cos(az) → az = atan2(-nx, -ny)
    az = np.arctan2(-nx, -ny)

    lat = math.radians(lat_deg)
    sin_dec = (np.sin(alt) * math.sin(lat)
               + np.cos(alt) * math.cos(lat) * np.cos(az))
    dec = np.arcsin(np.clip(sin_dec, -1.0, 1.0))
    ha = np.arctan2(
        -np.sin(az) * np.cos(alt),
        np.sin(alt) * math.cos(lat) - np.cos(alt) * math.sin(lat) * np.cos(az))
    ra = np.radians(lst_deg(unix_ts, lon_deg)) - ha

    # 赤道 → 银道(J2000 常数与 astro 一致)
    ngp_ra = math.radians(192.85948)
    ngp_dec = math.radians(27.12825)
    lon_ncp = math.radians(122.93192)
    sin_b = (np.sin(dec) * math.sin(ngp_dec)
             + np.cos(dec) * math.cos(ngp_dec) * np.cos(ra - ngp_ra))
    b = np.arcsin(np.clip(sin_b, -1.0, 1.0))
    l = lon_ncp - np.arctan2(
        np.cos(dec) * np.sin(ra - ngp_ra),
        np.sin(dec) * math.cos(ngp_dec)
        - np.cos(dec) * math.sin(ngp_dec) * np.cos(ra - ngp_ra))
    return np.degrees(l) % 360.0, np.degrees(b), mask


def _sample_equirect(src, l_deg, b_deg):
    """等距柱状银道底图采样(最近邻)。src 为 HxWx3 uint8 numpy 数组。"""
    import numpy as np

    h, w = src.shape[0], src.shape[1]
    l_signed = ((l_deg + 180.0) % 360.0) - 180.0
    if _L_INCREASES_LEFT:
        x = (w / 2.0 - l_signed * w / 360.0)
    else:
        x = (w / 2.0 + l_signed * w / 360.0)
    y = (h / 2.0 - b_deg * h / 180.0)
    xi = np.clip(x.astype(np.int64) % w, 0, w - 1)
    yi = np.clip(y.astype(np.int64), 0, h - 1)
    return src[yi, xi]


def render_altaz(lat_deg: float, lon_deg: float, unix_ts: float,
                 size: int = 760, src_path: Path | None = None,
                 dim: float = 0.85, cache_budget: int = 120) -> Path:
    """按站点+时刻把巡天底图重投影为 alt-az 圆盘 PNG(RGBA,盘外透明)。

    结果按 (站点0.1°, 时刻5分钟, 尺寸) 缓存;dim 整体压暗让前景标注可读。
    cache_budget: 磁盘缓存保留帧数上限 —— 放大遮罩整夜预热按桶数+余量传入,
    否则默认清理会删掉本轮预热仍被引用的帧(审查实证)。
    """
    import numpy as np
    from PIL import Image

    src_file = src_path or survey_path()
    key = hashlib.sha1(
        f"{src_file.name}|{lat_deg:.1f}|{lon_deg:.1f}|"
        f"{int(unix_ts // 300)}|{size}|{dim:.2f}".encode()).hexdigest()[:20]
    out = skymap_dir() / f"altaz_{key}.png"
    if out.is_file():
        return out

    try:
        with Image.open(src_file) as im:
            src = np.asarray(im.convert("RGB"))
    except OSError:
        # 历史版本可能缓存过截断底图:清掉损坏文件, 下次开启开关自动重下
        if src_path is None:
            try:
                src_file.unlink(missing_ok=True)
            except OSError:
                pass
            raise OSError(_("巡天底图缓存损坏, 已清除 — 请重新打开「巡天底图」开关下载"))
        raise
    l, b, mask = _altaz_to_lb_grid(size, lat_deg, lon_deg, unix_ts)
    rgb = _sample_equirect(src, l, b).astype(np.float32) * dim
    rgba = np.zeros((size, size, 4), dtype=np.uint8)
    rgba[..., :3] = np.clip(rgb, 0, 255).astype(np.uint8)
    rgba[..., 3] = np.where(mask, 255, 0).astype(np.uint8)

    tmp = out.with_suffix(".part")
    try:
        Image.fromarray(rgba, "RGBA").save(tmp, format="PNG")
        os.replace(tmp, out)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)     # 失败不留 .part 孤儿
        except OSError:
            pass
        raise
    _trim_cache(cache_budget)
    return out


def _trim_cache(max_files: int = 120) -> None:
    """重投影结果按 mtime 只留最近 max_files 个(每个约 1-2MB);
    顺带清理 1 小时前的 .part 孤儿(双保险,不碰可能在写的新 .part)。"""
    import time
    try:
        files = sorted(skymap_dir().glob("altaz_*.png"),
                       key=lambda p: p.stat().st_mtime)
        for p in files[:-max_files]:
            p.unlink(missing_ok=True)
        cutoff = time.time() - 3600
        for p in skymap_dir().glob("altaz_*.part"):
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
    except OSError:
        pass
