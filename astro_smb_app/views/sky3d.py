"""3D 天球页的**资产引导**:静态资源落盘、three.js 下载、巡天底图复制。

**页面本身一行都不用改。** `astro_smb_app/web/sky3d.js` 那 920 行在老 UI 里
已经跑了很久,消息协议(``init``/``targets``/``site``/``footprints``/``footSelect``/
``options``/``view``/``reset`` 出,``ready``/``hover``/``pick``/``footprint``/
``view``/``survey``/``error`` 回)也早就稳定 —— 新前端把它原样嵌进 `web` 图元,
连同一套 JSON 消息一起复用。这正是"Python 算、发 JSON、声明式渲染器画"
这个模式**在本仓库已经被验证过一轮**的证据。

两条从老 UI 继承的硬约束:

- **ES module 必须走 http(s) 源**,``file://`` 会被 CORS 挡掉 —— 所以要虚拟主机
  映射(WebView2 是 SetVirtualHostNameToFolderMapping,别的宿主各有各的做法)。
- **Python → JS 的 JSON 一律 ensure_ascii**。老 UI 是因为 win32more 按码点数
  给 HSTRING 长度;新前端没这个限制,但两边共用同一份 JS 与同一套消息,
  按更严的那边来。
"""
from __future__ import annotations

import json
import os
import shutil
import ssl
import subprocess
import threading
from collections.abc import Mapping
import urllib.error
import urllib.request
from astro_smb import paths
from pathlib import Path

# 虚拟主机名:WebView2 把它映射到本地资产目录(https 源,ES module 可用)
ASSET_HOST = "astro-smb-tool.local"
ASSET_ORIGIN = f"https://{ASSET_HOST}"
# three.js(ES module 单文件,r160)。任务已核实该 URL 200 OK,约 1.27MB。
THREE_URL = "https://unpkg.com/three@0.160.0/build/three.module.js"
THREE_NAME = "three.module.js"
THREE_MIN_BYTES = 400 * 1024        # 合理下限:小于此判为下载残缺/错误页
THREE_CREDIT = "three.js r160 (MIT)"
SURVEY_ASSET = "survey.jpg"         # 资产目录内的巡天底图文件名
#: 静态资产住在共享包里 —— **不复制一份**。那 920 行 sky3d.js 两套前端共用,
#: 复制就是又一处双实现。B10 把它从 `astro_smb_gui/web/` 移了过来:共享层
#: 伸手去读一个已冻结包的目录,和 import 它是同一种反向依赖,只是绕过了门禁。
def _pkg_web_dir() -> Path:
    from astro_smb_app import bundle

    dev = Path(__file__).resolve().parents[1] / "web"
    return bundle.data_file("astro_smb_app", "web",
                            package_relative=dev) or dev


PKG_WEB_DIR = _pkg_web_dir()


def web_cache_dir() -> Path:
    base = paths.cache_root() / "web"
    base.mkdir(parents=True, exist_ok=True)
    return base


def three_path() -> Path:
    return web_cache_dir() / THREE_NAME


def three_ready() -> bool:
    """three.js 是否已在本地缓存(首次进入页面据此决定要不要显示下载进度)。"""
    try:
        p = three_path()
        return p.is_file() and p.stat().st_size >= THREE_MIN_BYTES
    except OSError:
        return False
CORE_DLL = "Microsoft.Web.WebView2.Core.dll"
LOADER_NAME = "WebView2Loader.dll"
LOADER_ENV = "ASTRO_SMB_WEBVIEW2_LOADER"
_preload_state: tuple[bool, str] | None = None


def preload_webview2() -> tuple[bool, str]:
    """把 WebView2 的 WinRT 实现 DLL 预加载进本进程,返回 (是否成功, 说明)。

    **本任务最大的坑,排查了很久,勿删勿改顺序**:不做这一步,
    ``EnsureCoreWebView2Async`` 直接抛 ``0x8007007E 找不到指定的模块``
    (真机实证:探针 1 失败 → 预加载后探针成功)。

    原因:``Microsoft.Web.WebView2.Core.dll`` 由 win32more 的 wheel 放在
    ``site-packages/win32more/dll/x64``,而该目录是 ``os.add_dll_directory``
    (= AddDllDirectory)挂上去的;App SDK 里的 XAML ``WebView2`` 控件用
    cppwinrt 的**裸 LoadLibraryW** 去找这个 DLL,裸 LoadLibraryW **不搜**
    AddDllDirectory 挂的目录(只搜 exe 目录/System32/PATH)。而 ctypes 用
    ``LOAD_LIBRARY_SEARCH_DEFAULT_DIRS`` 能找到 —— 先由我们加载一次,模块就按
    基名常驻进程,控件那次 LoadLibraryW 便直接命中。

    ``WebView2Loader.dll``(WebView2 SDK 里由应用自行分发的加载器)在本机实测
    **不需要**:预加载 Core.dll 后即使全机没有它也能起来。留一个兜底:若
    ``ASTRO_SMB_WEBVIEW2_LOADER`` 指向的目录/资产目录里有它,一并加载
    (某些 Core.dll 版本可能仍要);**绝不扫描全盘去借别的软件的副本**。
    """
    global _preload_state
    if _preload_state is not None:
        return _preload_state
    if not paths.is_windows():
        # WebView2 是 Windows 专属的东西。别的平台上 Uno 用各自的原生 WebView,
        # 没有这个 DLL 也没有 ctypes.WinDLL —— 不早退这里直接 AttributeError,
        # 而它在 ensure_assets() 里,会把整个资产引导带走(天球页永远白屏)。
        _preload_state = (True, _("非 Windows,无需预加载 WebView2"))
        return _preload_state
    import ctypes
    # 兜底加载器(有就加载,没有不报错)
    dirs: list[Path] = []
    env = (os.environ.get(LOADER_ENV) or "").strip().strip('"')
    if env:
        p = Path(env)
        dirs.append(p if p.is_dir() else p.parent)
    dirs.append(web_cache_dir())
    for d in dirs:
        f = d / LOADER_NAME
        try:
            if not f.is_file():
                continue
            os.add_dll_directory(str(d))
            ctypes.WinDLL(str(f))
            break
        except OSError:
            continue
    try:
        ctypes.WinDLL(CORE_DLL)
        _preload_state = (True, _("{CORE_DLL} 已预加载").format(CORE_DLL=CORE_DLL))
    except OSError as ex:
        _preload_state = (
            False,
            _("找不到 {CORE_DLL}({ex})—— win32more 的 WebView2 组件缺失,可用 `uv sync` 重装依赖恢复").format(
                CORE_DLL=CORE_DLL, ex=ex))
    return _preload_state


def webview2_runtime_version() -> str:
    """已安装的 WebView2 Runtime 版本(取不到返回空串)。

    只用于降级提示措辞;真正能不能用以 ``EnsureCoreWebView2Async`` 为准。
    """
    if not paths.is_windows():
        return ""
    import winreg
    key = r"SOFTWARE\Microsoft\EdgeUpdate\Clients" \
          r"\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    for root, flags in ((winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
                        (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
                        (winreg.HKEY_CURRENT_USER, 0)):
        try:
            with winreg.OpenKey(root, key, 0,
                                winreg.KEY_READ | flags) as h:
                v, _kind = winreg.QueryValueEx(h, "pv")
                if v:
                    return str(v)
        except OSError:
            continue
    return ""


def prepare_user_data_folder() -> None:
    """把 WebView2 的用户数据目录固定到 %LOCALAPPDATA%/AstroSmbTool/webview2。

    非包身份进程默认把 ``<exe名>.WebView2`` 建在**解释器所在目录**
    (uv 的 .venv\\Scripts),那儿可能只读或被清理。必须在创建 CoreWebView2
    之前设置环境变量,之后再改无效。
    """
    if os.environ.get("WEBVIEW2_USER_DATA_FOLDER"):
        return
    try:
        d = paths.cache_root() / "webview2"
        d.mkdir(parents=True, exist_ok=True)
        os.environ["WEBVIEW2_USER_DATA_FOLDER"] = str(d)
    except OSError:
        pass


def _ssl_context() -> ssl.SSLContext:
    """从 Windows 证书库补根证书。

    与 skymap.py 同款兜底:uv 独立构建的 Python 在 Windows 上不自动挂接系统
    证书库,OpenSSL 也不做 AIA 补链,失败时再退 curl.exe(Schannel)。
    """
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


def _download_urllib(url: str, tmp: Path, progress, cancel) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "astro-smb-tool/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=30, context=_ssl_context()) as resp:
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
                raise OSError(_("下载不完整: {done}/{total} 字节").format(done=done, total=total))
    except urllib.error.URLError as ex:
        if isinstance(ex.reason, ssl.SSLCertVerificationError):
            raise ex.reason from ex
        raise


def _download_curl(url: str, tmp: Path) -> None:
    import subprocess
    proc = subprocess.run(
        # -f:HTTP 错误码要让 curl 以非零退出。没有它时 404 页面会被原样存成
        # "three.module.js",curl 返回 0,我们还以为下载成功了。
        paths.curl_argv("-fsSL", "--max-time", "120", "-o", str(tmp), url),
        capture_output=True, timeout=150,
        creationflags=paths.NO_WINDOW)
    if proc.returncode != 0 or not tmp.is_file():
        err = proc.stderr.decode("utf-8", errors="replace").strip()
        raise OSError(_("curl 下载失败: {0}").format(err or proc.returncode))


def download_three(progress=None, cancel: threading.Event | None = None) -> Path:
    """下载 three.js 到缓存(.part 原子落盘)。已存在且大小合理则直接返回。"""
    dest = three_path()
    if three_ready():
        return dest
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        try:
            _download_urllib(THREE_URL, tmp, progress, cancel)
        except ssl.SSLCertVerificationError:
            _download_curl(THREE_URL, tmp)
        size = tmp.stat().st_size
        if size < THREE_MIN_BYTES:
            raise OSError(_("three.js 下载内容过小({size} 字节),可能是错误页").format(size=size))
        head = tmp.read_bytes()[:4096].decode("utf-8", errors="replace")
        if "REVISION" not in head and "three" not in head.lower():
            raise OSError(_("three.js 下载内容不像 JS 模块,已丢弃"))
        os.replace(tmp, dest)
        return dest
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _copy_static(dest_dir: Path) -> list[str]:
    """把包内 web/ 的静态资产覆盖到缓存目录,返回复制到的文件名列表。"""
    names: list[str] = []
    if not PKG_WEB_DIR.is_dir():
        return names
    for src in sorted(PKG_WEB_DIR.iterdir()):
        if not src.is_file() or src.name.startswith("."):
            continue
        dst = dest_dir / src.name
        # **原子替换**:直接 copy2 覆盖时,若 WebView2 恰好在读同名文件,
        # 导航会拿到 ERR_ACCESS_DENIED(真机复现过一次)。先写临时名再
        # os.replace,读者要么看到旧版要么看到新版,不会撞到"正在写"的中间态。
        tmp = dest_dir / (src.name + ".part")
        try:
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
        except OSError:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            try:
                shutil.copy2(src, dst)      # 退回直接覆盖,总比没有资产强
            except OSError:
                continue
        names.append(src.name)
    return names


def _sync_survey(dest_dir: Path) -> bool:
    """把 skymap 缓存的巡天底图复制成 survey.jpg(大小相同则跳过)。

    返回是否可用。没有底图不是错误 —— 页面退化成"只有网格没有贴图"。
    """
    try:
        from astro_smb_app.skymap import survey_available, survey_path
        if not survey_available():
            return False
        src = survey_path()
        dst = dest_dir / SURVEY_ASSET
        if dst.is_file() and dst.stat().st_size == src.stat().st_size:
            return True
        tmp = dst.with_suffix(dst.suffix + ".part")
        try:
            shutil.copy2(src, tmp)
            os.replace(tmp, dst)
        except BaseException:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return True
    except Exception:
        return False


def ensure_assets(progress=None, cancel: threading.Event | None = None) -> Path:
    """准备资产目录并返回它。**阻塞,工作线程调用。**

    progress(阶段文字, 已下载字节, 总字节);总字节未知时为 0。
    """
    dest = web_cache_dir()
    prepare_user_data_folder()
    if progress is not None:
        progress(_("准备页面资源 …"), 0, 0)
    _copy_static(dest)
    preload_webview2()  # LoadLibrary 有磁盘 I/O,放在工作线程先做掉
    if not three_ready():
        if progress is not None:
            progress(_("正在下载 three.js(约 1.3 MB)…"), 0, 0)
        download_three(
            progress=(None if progress is None
                      else (lambda d, t: progress(_("正在下载 three.js …"), d, t))),
            cancel=cancel)
    if progress is not None:
        progress(_("准备巡天底图 …"), 0, 0)
    _sync_survey(dest)
    return dest


def refresh_survey(assets_dir: Path | None = None) -> str | None:
    """(工作线程)把巡天底图同步进资产目录并返回 URL。

    用户可能在拍摄记录页事后才下载底图,这时不必重启:本页 on_show 时再同步一次。
    """
    d = assets_dir or web_cache_dir()
    _sync_survey(d)
    return survey_asset_url(d)


def survey_asset_url(assets_dir: Path | None = None) -> str | None:
    """页面可直接访问的底图 URL;没有底图返回 None。"""
    d = assets_dir or web_cache_dir()
    try:
        p = d / SURVEY_ASSET
        if p.is_file() and p.stat().st_size > (1 << 20):
            return f"{ASSET_ORIGIN}/{SURVEY_ASSET}"
    except OSError:
        pass
    return None


# ------------------------------------------------------- 夜次 → 目标
#
# **从 `astro_smb_gui/_sky3d.py` 原样搬来,函数体一个字节没动。**
# 3D 天球页要把"这一夜拍了哪些目标、各在天上什么位置"推给前端,而那份
# 归并逻辑(同夜同名目标跨 Plan / 被 Pause 分裂后合并、坐标优先用 FITS
# 实测)只该有一份 —— 两套前端对同一夜给出不同的目标列表,是最难查的
# 那种分叉。

# 目标配色:列表色点与天球标记同源(低饱和亮色,深色背景可读)
TARGET_COLORS = ["#7FD88F", "#FFC457", "#8AB4FF", "#F09BC8",
                 "#6FE0D8", "#C4A6FF", "#FF9E7A", "#B8D96B"]


# `_sky_relevant` 的唯一真源在 `views.records` —— 老 UI 的 `_sky3d.py` 里
# 原本还有一份逐字相同的副本(既存双实现),这次一并收掉。
from astro_smb import astro                                # noqa: E402
from astro_smb.i18n import gettext as _                    # noqa: E402

#: 坐标来源的**语义键**(与显示文本分开 —— 见 `_build_nights` 里的注释)
SRC_FITS = "fits"
SRC_LOG = "log"
from astro_smb_app.views.records import _sky_relevant     # noqa: E402,F401


def _fits_coords(hdr) -> tuple[float, float] | None:
    """FITS 头里的实测指向(度);缺失/非数值返回 None。"""
    try:
        ra = hdr.get("RA")
        dec = hdr.get("DEC")
        if ra is None or dec is None:
            return None
        return float(ra) % 360.0, max(-90.0, min(90.0, float(dec)))
    except (TypeError, ValueError):
        return None


def _as_float(value) -> float | None:
    try:
        out = float(value)
        return out if out == out else None
    except (TypeError, ValueError):
        return None


def _fits_coord(hit) -> tuple[float | None, float | None]:
    """从一条 FITS 记录里取 (ra, dec) —— **两种形状都要收**。

    这个函数存在的原因是一次真实的线上崩溃:

    * 冻结的老 UI 的 ``_collect_fits`` 返回 ``dict[int, tuple[float, float]]``;
    * 下沉到共享层的 ``logstore.collect_fits_map`` 返回 ``dict[int, dict]``
      (键是 ``ra_deg`` / ``dec_deg``,另外还带焦距、像元等)。

    ``_build_nights`` 被**两套前端共用**,老 UI 冻结着不能改,于是它必须同时
    认得两种。原来只写了 ``hit[0], hit[1]``:喂 dict 进来时那是拿整数 0 当键
    去查字典 —— ``KeyError: 0``,3D 天球页**整页打不开**,而且是常态而非边角
    (只要哪个目标的坐标是从 FITS 头读出来的就会踩到)。

    dict 里**没有**坐标是正常情况(``collect_fits_map`` 对只有焦距/像元的头
    也会建条目),这时返回 (None, None) 让调用方回退到日志坐标,
    而不是把这个目标整个丢掉。
    """
    if isinstance(hit, Mapping):
        return _num(hit.get("ra_deg")), _num(hit.get("dec_deg"))
    if isinstance(hit, (str, bytes)):
        # **字符串要先挡掉再下标。** 它是可以下标的,而 ``"338.27"[0]`` 是
        # ``"3"`` —— 转成 float 完全合法,于是一条坏记录变成"实测坐标 3°"
        # 画到天球上:不报错,只是点在错的地方。
        return None, None
    try:
        return _num(hit[0]), _num(hit[1])
    except (TypeError, LookupError):        # noqa: BLE001 - 形状不认识就当没有
        return None, None


def _num(v) -> float | None:
    """能当数用就返回 float,否则 None。

    **别只看"能不能下标"。** 字符串是可以下标的:``"338.27"[0]`` 给的是
    ``"3"`` —— 于是一条坏记录会被当成"实测坐标 3°"画到天球上,不报错,
    只是点在错的地方。
    """
    if isinstance(v, bool) or v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _build_nights(data, coords: dict[int, object]) -> list[dict]:
    """夜次 → 纯数据(工作线程调用,UI 侧只做渲染)。

    同夜同名目标(跨 Plan / 被 Pause 分裂)合并成一条:帧数与积分累加,
    时段取并集,坐标用第一条拿到的。

    ``coords`` 的值可以是 ``(ra, dec)`` 元组,也可以是带 ``ra_deg``/``dec_deg``
    的映射 —— 见 :func:`_fits_coord`。
    """
    out: list[dict] = []
    for night in data.nights:
        merged: dict[str, dict] = {}
        order: list[str] = []
        for run in night.runs:
            if not _sky_relevant(run):
                continue
            # 日志坐标单独留一份:覆盖卡的"指向误差"量的正是
            # 实测/解算中心 vs 这个 goto 请求值,拿合并后的坐标算会永远是 0
            log_ra = astro.ra_str_to_deg(run.ra)
            log_dec = astro.dec_str_to_deg(run.dec)
            hit = coords.get(id(run))
            ra = dec = None
            if hit is not None:
                ra, dec = _fits_coord(hit)
            if ra is not None and dec is not None:
                src_key, src = SRC_FITS, _("FITS 实测")
            else:
                # 有记录但没坐标(只读到焦距/像元)也走这里 —— 回退到日志
                # 坐标,而不是把这个目标从天球上抹掉
                ra, dec = log_ra, log_dec
                src_key, src = SRC_LOG, _("日志坐标")
            if ra is None or dec is None:
                continue
            frames = run.all_frames()
            exposure = sum(f.exposure_s for f in frames)
            span = run.frame_span()
            t0 = span[0] if span else run.begin_time
            t1 = span[1] if span else (run.end_time or run.begin_time)
            item = merged.get(run.target)
            if item is None:
                item = {"name": run.target, "ra": ra, "dec": dec, "source": src,
                        # **语义键与显示文本分开。** 下面那句合并要判"这条是不是
                        # FITS 实测",原来比的是**显示文本**;一旦文案改动或者
                        # 做了 i18n,比较静默失效 —— FITS 坐标不再覆盖日志坐标,
                        # 天球上的点悄悄退回 goto 请求值(与实测恒差约 21′)。
                        "source_key": src_key,
                        "log_ra": log_ra, "log_dec": log_dec,
                        "frames": 0, "exposure": 0.0, "t0": t0, "t1": t1,
                        "plans": [], "runs": []}
                merged[run.target] = item
                order.append(run.target)
            elif src_key == SRC_FITS and item.get("source_key") != SRC_FITS:
                item["ra"], item["dec"] = ra, dec
                item["source"], item["source_key"] = src, src_key
            item["frames"] += len(frames)
            item["exposure"] += exposure
            item["t0"] = min(item["t0"], t0)
            item["t1"] = max(item["t1"], t1)
            item["runs"].append(run)
            if run.plan_no is not None and run.plan_no not in item["plans"]:
                item["plans"].append(run.plan_no)
        targets = [merged[n] for n in order]
        if not targets:
            continue
        for i, t in enumerate(targets):
            t["color"] = TARGET_COLORS[i % len(TARGET_COLORS)]
            t["ts0"] = t["t0"].timestamp()
            t["ts1"] = t["t1"].timestamp()
        begin = night.begin_time or min(t["t0"] for t in targets)
        end = night.end_time or max(t["t1"] for t in targets)
        ts0 = min(begin.timestamp(), min(t["ts0"] for t in targets))
        ts1 = max(end.timestamp(), max(t["ts1"] for t in targets))
        if ts1 - ts0 < 600:             # 极短夜次给个可拖动的量程
            ts1 = ts0 + 600
        out.append({
            "date": night.date,
            "targets": targets,
            "ts0": ts0,
            "ts1": ts1,
            "frames": sum(t["frames"] for t in targets),
            "exposure": sum(t["exposure"] for t in targets),
        })
    out.sort(key=lambda n: n["date"])
    return out
