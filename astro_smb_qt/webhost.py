"""QtWebEngine 宿主:把老 UI 那份 three.js 天球页原封不动搬过来。

**`astro_smb_app/web/sky3d.js` 一个字都不改。** 它是 920 行、已经在真机上跑了
很久的东西(GPU 加速、拖动/缩放/点选/足迹/巡天贴图全在里面),重写一份纯属
自找麻烦 —— 而这套前端的立身之本就是"业务一行不重写"。

两处适配:

1. **桥。** 那份 JS 用的是 WebView2 专有的 `window.chrome.webview`
   (`postMessage` + `addEventListener('message')`)。这里在文档创建期注入一段
   同名 shim,底下接 `QWebChannel`。JS 侧因此完全不知道换了宿主。
2. **来源。** ES module 从 `file://` 加载会被 CORS 挡(老 UI 为此用了 WebView2
   的虚拟主机映射)。这里起一个**只绑 127.0.0.1、端口 0** 的迷你 HTTP 服务
   指向资产目录 —— 绑回环不触发 Windows 防火墙弹窗(仓库里的回环协议同款做法)。

依赖账:`QtWebEngine` **已经在 PySide6 里**(完整包 665 MB,其中 208 MB 就是它)
—— 用它不多花一分钱。之前判断"几百 MB 额外依赖"是错的。
"""
from __future__ import annotations

import functools
import http.server
import json
import logging
import socketserver
import threading
from pathlib import Path
from astro_smb.i18n import gettext as _

log = logging.getLogger(__name__)

#: 注入到每个文档的桥接 shim。**必须在文档创建期注入** —— sky3d.js 在
#: 顶层就读 `window.chrome.webview`,晚一步它就认为"宿主不在"。
_SHIM = """
(function () {
  if (window.chrome && window.chrome.webview) { return; }
  var listeners = [];
  window.chrome = window.chrome || {};
  window.chrome.webview = {
    postMessage: function (obj) {
      if (window.__astroBridge) {
        window.__astroBridge.fromJs(JSON.stringify(obj));
      } else {
        (window.__astroPending = window.__astroPending || []).push(obj);
      }
    },
    addEventListener: function (type, fn) {
      if (type === 'message') { listeners.push(fn); }
    },
    removeEventListener: function (type, fn) {
      var i = listeners.indexOf(fn);
      if (i >= 0) { listeners.splice(i, 1); }
    }
  };
  // 宿主 → 页面。**参数是 JSON 字符串**:QWebChannel 传 dict 会经过一次
  // 它自己的序列化,嵌套结构上不如自己控制得准。
  window.__astroDeliver = function (text) {
    var data;
    try { data = JSON.parse(text); } catch (e) { return; }
    for (var i = 0; i < listeners.length; i++) {
      try { listeners[i]({ data: data }); } catch (e) { /* 单个监听器出错不影响其它 */ }
    }
  };
  new QWebChannel(qt.webChannelTransport, function (channel) {
    window.__astroBridge = channel.objects.bridge;
    var q = window.__astroPending || [];
    window.__astroPending = [];
    for (var i = 0; i < q.length; i++) {
      window.__astroBridge.fromJs(JSON.stringify(q[i]));
    }
  });
})();
"""


class _Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):        # noqa: A003 - 覆写基类
        pass                                   # 别把每个请求刷到控制台


class AssetServer:
    """资产目录的迷你 HTTP 服务。**只绑 127.0.0.1,端口由内核分配。**"""

    def __init__(self, root: Path):
        self.root = Path(root)
        handler = functools.partial(_Handler, directory=str(self.root))
        self._srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
        self._srv.daemon_threads = True
        self.port = self._srv.server_address[1]
        self._thread = threading.Thread(target=self._srv.serve_forever,
                                        daemon=True, name="qt-sky-assets")
        self._thread.start()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def url(self, name: str) -> str:
        return f"{self.origin}/{name}"

    def close(self) -> None:
        try:
            self._srv.shutdown()
            self._srv.server_close()
        except Exception:                      # noqa: BLE001
            pass


def available() -> tuple[bool, str]:
    """QtWebEngine 能不能用。返回 ``(可用?, 不可用的原因)``。"""
    try:
        from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
    except Exception as exc:                   # noqa: BLE001
        return False, _("QtWebEngine 不可用: {exc}").format(exc=exc)
    return True, ""


def make_view(on_message):
    """建一个接好桥的 ``QWebEngineView``。

    ``on_message(dict)`` 在 **GUI 线程**上被调用(QWebChannel 的槽本来就在
    对象所属线程)。返回 ``(view, post)``,``post(dict)`` 把消息发给页面。
    """
    from PySide6.QtCore import QObject, Slot
    from PySide6.QtWebChannel import QWebChannel
    from PySide6.QtWebEngineCore import QWebEngineScript
    from PySide6.QtWebEngineWidgets import QWebEngineView

    class _Bridge(QObject):
        @Slot(str)
        def fromJs(self, text: str) -> None:   # noqa: N802 - JS 侧的名字
            try:
                on_message(json.loads(text))
            except Exception:                  # noqa: BLE001
                log.exception(_("页面消息处理失败"))

    view = QWebEngineView()
    bridge = _Bridge(view)
    channel = QWebChannel(view)
    channel.registerObject("bridge", bridge)
    view.page().setWebChannel(channel)

    script = QWebEngineScript()
    script.setName("astro-bridge")
    # **DocumentCreation** —— sky3d.js 在顶层就读 `window.chrome.webview`
    script.setInjectionPoint(QWebEngineScript.DocumentCreation)
    script.setWorldId(QWebEngineScript.MainWorld)
    script.setRunsOnSubFrames(False)
    script.setSourceCode(_qwebchannel_js() + _SHIM)
    view.page().scripts().insert(script)

    def post(msg: dict) -> None:
        # 两层 JSON 是**有意的**:内层是消息本身(`ensure_ascii=True`,与老 UI
        # 同一条约定,中文在 JS 侧由 \\uXXXX 还原);外层把它变成一个 JS 字符串
        # 字面量,免得自己去拼引号转义。
        payload = json.dumps(json.dumps(msg, ensure_ascii=True))
        view.page().runJavaScript(
            f"window.__astroDeliver && window.__astroDeliver({payload});")

    return view, post


def _qwebchannel_js() -> str:
    """Qt 自带的 `qwebchannel.js`(资源文件,不必外部下载)。"""
    from PySide6.QtCore import QFile, QIODevice

    f = QFile(":/qtwebchannel/qwebchannel.js")
    if not f.open(QIODevice.ReadOnly):
        raise RuntimeError(_("取不到 qwebchannel.js —— 桥接无法建立"))
    try:
        return bytes(f.readAll().data()).decode("utf-8")
    finally:
        f.close()
