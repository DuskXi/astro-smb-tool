"""WebView2 可视化宿主:把 GPU 加速的 three.js 页面嵌进 WinUI3。

**为什么有这个模块**:纯软件重投影的天球图(skymap.py)拖动时要在 CPU 上重算
整幅位图,拖不动;把渲染交给 WebView2 里的 WebGL(three.js)即可吃显卡。

三件事:
1. ``ensure_assets()`` —— 把包内 ``astro_smb_app/web/`` 的静态资产(html/js/css)
   **每次启动覆盖**到 ``%LOCALAPPDATA%/AstroSmbTool/web/``(便于迭代),
   并保证 ``three.module.js`` 已下载(**下载一次就留着**);顺带把巡天底图
   (skymap 缓存的 eso0932a.jpg)复制成 ``survey.jpg`` 供页面贴图。
   **阻塞调用,必须在工作线程执行。**
2. ``WebHost`` —— 包一个 ``WebView2`` 控件:虚拟主机映射
   (``https://astro-smb-tool.local/`` → 资产目录,ES module 需要真实 http(s) 源,
   file:// 会被 CORS 挡)、导航、双向 JSON 消息。
3. 降级 —— WebView2 Runtime 缺失/初始化失败时不崩:``WebHost.failure`` 给出
   中文原因,页面显示可读提示。

线程模型:``WebHost`` 的所有方法都只在 **UI 线程**调用(WebView2 是 XAML 控件,
有线程亲和性);``on_message`` 回调由 WinRT 事件派发,同样在 UI 线程。
``ensure_assets`` 是唯一的阻塞函数,反过来只能在工作线程调用。

编码约定:Python → JS 的 JSON 一律 ``ensure_ascii=True``。win32more 把 str 转
HSTRING 时按码点数给长度(§7.1),非 BMP 字符会让字符串末尾少字符;全 ASCII
的 JSON 天然免疫,中文在 JS 侧由 ``\\uXXXX`` 还原。
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import ssl
import threading
import urllib.error
import urllib.request
from pathlib import Path
from astro_smb.i18n import gettext as _

# 虚拟主机名:WebView2 把它映射到本地资产目录(https 源,ES module 可用)
ASSET_HOST = "astro-smb-tool.local"
ASSET_ORIGIN = f"https://{ASSET_HOST}"

# three.js(ES module 单文件,r160)。任务已核实该 URL 200 OK,约 1.27MB。
THREE_URL = "https://unpkg.com/three@0.160.0/build/three.module.js"
THREE_NAME = "three.module.js"
THREE_MIN_BYTES = 400 * 1024        # 合理下限:小于此判为下载残缺/错误页
THREE_CREDIT = "three.js r160 (MIT)"

SURVEY_ASSET = "survey.jpg"         # 资产目录内的巡天底图文件名

#: 静态资产住在 **共享包** `astro_smb_app/web/` —— 新旧两套前端共用同一份。
#: 那 920 行 sky3d.js 复制一份就是又一处双实现。(B10 逃生口)
PKG_WEB_DIR = Path(__file__).resolve().parents[1] / "astro_smb_app" / "web"


def web_cache_dir() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "AstroSmbTool" / "web"
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
    try:
        import winreg
    except ImportError:      # 非 Windows(理论上不会走到)
        return ""
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
        d = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "AstroSmbTool" / "webview2"
        d.mkdir(parents=True, exist_ok=True)
        os.environ["WEBVIEW2_USER_DATA_FOLDER"] = str(d)
    except OSError:
        pass


# ---------------------------------------------------------------- 资产准备

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
        ["curl.exe", "-fsSL", "--max-time", "120", "-o", str(tmp), url],
        capture_output=True, timeout=150,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
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
        from astro_smb_gui.skymap import survey_available, survey_path
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


# ---------------------------------------------------------------- 宿主控件

class WebHost:
    """一个 WebView2 控件 + 虚拟主机映射 + JSON 消息通道。

    用法(全部在 UI 线程):
        host = WebHost("sky3d.html", on_message=self._on_web, on_error=...)
        grid.Children.InsertAt(0, host.element)       # element 可能为 None
        ok = await host.ensure_ready(assets_dir)      # 失败看 host.failure
        host.post({"type": "targets", "items": [...]})

    页面就绪握手:JS 侧初始化完成后 ``postMessage({type:"ready"})``,在此之前
    ``post()`` 的消息**排队**,收到 ready 后按序发出 —— 否则页面还没注册
    message 监听器,先发的消息会丢。
    """

    def __init__(self, page: str, on_message=None, on_error=None) -> None:
        self.page = page
        self._on_message = on_message
        self._on_error = on_error
        self.element = None                 # WebView2(创建失败为 None)
        self.core = None
        self.ready = False                  # CoreWebView2 已就绪并已导航
        self.page_ready = False             # 页面 JS 已 ready
        self.failure = ""                   # 降级原因(中文,给用户看)
        self._queue: list[str] = []
        self._booting = False
        try:
            from win32more.Microsoft.UI.Xaml.Controls import WebView2
            prepare_user_data_folder()
            self.element = WebView2()
        except Exception as ex:
            self.failure = _("无法创建 WebView2 控件: {ex}").format(ex=ex)

    # ---------- 生命周期 ----------

    async def ensure_ready(self, assets_dir: Path) -> bool:
        """初始化 CoreWebView2 → 映射虚拟主机 → 导航到页面。失败返回 False。"""
        if self.element is None:
            return False
        if self.ready:
            return True
        if self._booting:
            return False
        self._booting = True
        try:
            from win32more.Microsoft.Web.WebView2.Core import (
                CoreWebView2HostResourceAccessKind,
            )
            preloaded = preload_webview2()   # 已缓存;正常在工作线程做过
            if not preloaded[0]:
                raise RuntimeError(preloaded[1])
            # **控件必须先进可视树并 Loaded**:页面刚建好就调
            # EnsureCoreWebView2Async 会"成功"返回但 CoreWebView2 是 None
            # (真机复现:启动即切到本页时必现)。等它 Loaded 再初始化,
            # 之后再给几次重试兜底。
            for _i in range(100):
                try:
                    if self.element.IsLoaded:
                        break
                except Exception:
                    break
                await asyncio.sleep(0.1)
            core = None
            for _i in range(12):
                await self.element.EnsureCoreWebView2Async()
                core = self.element.CoreWebView2
                if core is not None:
                    break
                await asyncio.sleep(0.5)
            if core is None:
                raise RuntimeError(_("CoreWebView2 初始化后仍为空(控件未加载?)"))
            self.core = core
            try:
                s = core.Settings
                s.AreDefaultContextMenusEnabled = False
                s.IsStatusBarEnabled = False
                s.IsZoomControlEnabled = False
                s.IsWebMessageEnabled = True
            except Exception:
                pass    # 设置项是锦上添花,拿不到不影响渲染
            core.SetVirtualHostNameToFolderMapping(
                ASSET_HOST, str(assets_dir),
                CoreWebView2HostResourceAccessKind.Allow)
            # **只注册一次**:ensure_ready 会被重试路径再走一遍,而它在
            # Navigate/ready=True **之前**,失败重来时就又挂一个处理器 ——
            # 之后每条 JS 消息都会被处理两次(审查实证)。
            # 注意 win32more 的 `-=` 对 _event_setters 无效,只能靠自己不重复挂。
            if not getattr(self, "_msg_hooked", False):
                self.element.WebMessageReceived += self._handle_message
                self._msg_hooked = True
            core.Navigate(f"{ASSET_ORIGIN}/{self.page}")
            self.ready = True
            return True
        except Exception as ex:
            ok_pre, pre_msg = preload_webview2()
            if not ok_pre:
                hint = " — " + pre_msg
            else:
                ver = webview2_runtime_version()
                hint = (_("(已装 Runtime {ver})").format(ver=ver) if ver
                        else _("(未检测到 Microsoft Edge WebView2 Runtime,请到微软官网安装「Evergreen 独立安装程序」后重试)"))
            self.failure = _("WebView2 初始化失败{hint}: {ex}").format(hint=hint, ex=ex)
            if self._on_error is not None:
                self._on_error(self.failure)
            return False
        finally:
            self._booting = False

    def close(self) -> None:
        """释放 WebView2(关窗时调用;失败静默——进程退出 OS 会回收)。"""
        self.page_ready = False
        self.ready = False
        try:
            if self.element is not None:
                self.element.Close()
        except Exception:
            pass

    # ---------- 消息 ----------

    def post(self, payload: dict) -> None:
        """Python → JS。页面未就绪时排队(见类文档)。"""
        try:
            text = json.dumps(payload, ensure_ascii=True)
        except (TypeError, ValueError) as ex:
            if self._on_error is not None:
                self._on_error(_("消息序列化失败: {ex}").format(ex=ex))
            return
        if not (self.ready and self.page_ready) or self.core is None:
            self._queue.append(text)
            if len(self._queue) > 64:       # 页面一直起不来时别无限攒
                del self._queue[:-64]
            return
        self._send(text)

    def _send(self, text: str) -> None:
        try:
            self.core.PostWebMessageAsJson(text)
        except Exception as ex:
            if self._on_error is not None:
                self._on_error(_("发送消息到页面失败: {ex}").format(ex=ex))

    def _flush(self) -> None:
        pending, self._queue = self._queue, []
        for text in pending:
            self._send(text)

    def _handle_message(self, sender, args) -> None:
        """WebMessageReceived(UI 线程)。异常必须自己吞——WinRT 事件里抛出
        会被吞掉且可能杀掉后续事件。"""
        try:
            raw = args.WebMessageAsJson
            msg = json.loads(raw)
            if isinstance(msg, str):        # JS 侧 postMessage 传的是字符串
                try:
                    msg = json.loads(msg)
                except (TypeError, ValueError):
                    msg = {"type": "text", "text": msg}
            if not isinstance(msg, dict):
                return
            if msg.get("type") == "ready":
                self.page_ready = True
                self._flush()
            if self._on_message is not None:
                self._on_message(msg)
        except Exception as ex:
            if self._on_error is not None:
                try:
                    self._on_error(_("处理页面消息失败: {ex}").format(ex=ex))
                except Exception:
                    pass
