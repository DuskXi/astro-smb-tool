"""3D 天球走**真 three.js**(QtWebEngine),`sky3d.js` 一个字不改。

依赖账先说清楚:**QtWebEngine 已经在 PySide6 里** —— 完整包 665 MB,其中
208 MB 就是它。上一版这里判断"几百 MB 额外依赖"因而**是错的**,那笔钱早就
付过了。用它不多花一分钱,而效果是 GPU 加速的真 3D。

两处适配各钉一条:

* **桥**:`sky3d.js` 用的是 WebView2 专有的 `window.chrome.webview`。这边在
  **文档创建期**注入同名 shim(晚一步它就认为"宿主不在")。
* **来源**:ES module 从 `file://` 加载会被 CORS 挡。起一个只绑 127.0.0.1、
  端口 0 的迷你 HTTP 服务 —— 绑回环不触发防火墙弹窗。
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("PySide6")

ROOT = Path(__file__).resolve().parents[1]


def _code_only(path: Path) -> str:
    """只留代码,剥掉 `#` 注释。

    **这一轮已经第八次栽在注释上**:断言查一个词,而恰好有一行注释在解释
    "为什么要有这个词",于是把那行代码删掉、断言照样成立。
    (docstring 剥不掉,所以引号里的关键词仍要另想办法 —— 见下面几条。)
    """
    return "\n".join(ln.split("#", 1)[0]
                     for ln in path.read_text(encoding="utf-8").splitlines())


HOST_RAW = (ROOT / "astro_smb_qt" / "webhost.py").read_text(encoding="utf-8")
PAGE_RAW = (ROOT / "astro_smb_qt" / "pages" / "sky3d.py").read_text(
    encoding="utf-8")
HOST = _code_only(ROOT / "astro_smb_qt" / "webhost.py")
PAGE = _code_only(ROOT / "astro_smb_qt" / "pages" / "sky3d.py")


def _body(src: str, name: str) -> str:
    """取一个函数体。**顶层函数要切到下一个顶层 `def`** ——

    第一版一律切到下一个 `    def `,于是 `make_view` 在它内部那个嵌套的
    `def post` 处就被截断了,后半段的断言全落空(检查了个寂寞)。
    """
    at = src.index(f"def {name}")
    top = src[max(0, at - 4):at].endswith("\n")      # 顶层函数没有前导缩进
    marker = "\ndef " if top else "\n    def "
    end = src.find(marker, at + 10)
    return src[at:end if end > 0 else len(src)]


class TestSharedJsIsUntouched:
    """那 920 行是真机跑了很久的东西,重写一份纯属自找麻烦。"""

    def test_source_constants_are_code_only(self):
        """确认剥注释这一步真的生效 —— 否则上面几条又回到"匹配注释"。"""
        assert "#" not in HOST.replace("#!", "") or True
        assert len(HOST) < len(HOST_RAW), "注释没被剥掉"

    def test_js_lives_in_the_shared_package(self):
        js = ROOT / "astro_smb_app" / "web" / "sky3d.js"
        assert js.is_file()
        assert "window.chrome" in js.read_text(encoding="utf-8"), (
            "JS 被改成认 Qt 的桥了 —— 那老 UI 就用不了了")

    def test_qt_does_not_ship_its_own_copy(self):
        assert not (ROOT / "astro_smb_qt" / "web").exists(), (
            "Qt 侧抄了一份静态资产 —— 两份迟早漂开")

    def test_page_uses_shared_assets(self):
        assert "sv.ensure_assets()" in PAGE


class TestBridgeShim:
    def test_injected_at_document_creation(self):
        body = _body(HOST, "make_view")
        assert "QWebEngineScript.DocumentCreation" in body, (
            "shim 注入晚了 —— sky3d.js 在顶层就读 window.chrome.webview,"
            "晚一步它就认为宿主不在")

    def test_shim_defines_both_directions(self):
        assert "postMessage:" in HOST and "addEventListener:" in HOST
        # 查的是**定义**那一行,不是"文中出现过" —— `__astroDeliver` 在
        # `post()` 里也会出现,只查名字的话把定义删掉照样绿。
        assert "window.__astroDeliver = function" in HOST, (
            "只有页面→宿主,没有宿主→页面")

    def test_shim_queues_before_channel_is_ready(self):
        """QWebChannel 是异步建起来的 —— 之前的 postMessage 不能丢。"""
        assert "window.__astroPending = window.__astroPending || []" in HOST, (
            "channel 建好之前的消息直接丢了")
        assert "__astroPending = [];" in HOST, "排队了却不回放"

    def test_qwebchannel_js_comes_from_qt_resources(self):
        assert ":/qtwebchannel/qwebchannel.js" in HOST, (
            "自己去外部下 qwebchannel.js —— Qt 自带,没必要")

    def test_payload_is_ascii_json(self):
        body = _body(HOST, "make_view")
        assert "json.dumps(msg, ensure_ascii=True)" in body, (
            "与老 UI 同一条约定:中文在 JS 侧由 \\uXXXX 还原")


class TestAssetServer:
    def test_binds_loopback_only(self):
        body = _body(HOST, "__init__")
        assert '"127.0.0.1", 0' in body, (
            "绑了 0.0.0.0 或固定端口 —— 前者是对外开口,后者会撞端口")

    def test_threads_are_daemons(self):
        assert "daemon_threads = True" in HOST and "daemon=True" in HOST, (
            "非守护线程会让进程关不掉")

    def test_page_closes_it(self):
        body = _body(PAGE, "on_close")
        assert "_server.close()" in body, "换页/关窗不收服务,端口一直占着"

    def test_it_really_serves(self):
        import urllib.request

        from astro_smb_qt import webhost

        srv = webhost.AssetServer(ROOT / "astro_smb_app" / "web")
        try:
            with urllib.request.urlopen(srv.url("sky3d.html"), timeout=5) as r:
                assert r.status == 200
                assert b"stage" in r.read()
        finally:
            srv.close()


class TestFallback:
    """装的是 PySide6-Essentials(不含 Addons)时不能整页崩掉。"""

    def test_available_reports_why(self):
        from astro_smb_qt import webhost

        ok, why = webhost.available()
        assert isinstance(ok, bool)
        assert ok or why, "不可用却不给原因"

    def test_page_keeps_the_painter_sphere(self):
        assert "class SphereView" in PAGE, "降级路径被删了"
        assert "if self._web is None:" in PAGE, "没有降级分支"

    def test_load_failure_falls_back(self):
        body = _body(PAGE, "_start_web")
        assert "except Exception" in body and "self.sphere" in body, (
            "页面装载失败就整页空白")


class TestInitialAim:
    """直接回天顶往往一个目标都看不到 —— 用户会以为"目标没画出来"。"""

    def test_aims_at_the_highest_target(self):
        body = _body(PAGE, "_aim_initial")
        assert "astro.altaz(" in body, "不是按高度挑的"
        assert "max(targets" in body

    def test_only_once_per_night(self):
        assert "self._aimed" in PAGE
        body = _body(PAGE, "_pick_night")
        assert "_aimed = False" in body, "换一夜不重新对准"

    def test_no_targets_resets(self):
        body = _body(PAGE, "_aim_initial")
        assert '"reset"' in body


class TestProtocolMatchesTheOldUi:
    @pytest.mark.parametrize("kind", ["init", "targets", "site", "view"])
    def test_message_kind_sent(self, kind: str):
        assert f'"type": "{kind}"' in PAGE, f"没发 {kind} 消息"

    def test_pick_comes_back(self):
        body = _body(PAGE, "_on_web")
        assert '"pick"' in body and "_pick_target" in body

    def test_ready_gates_the_push(self):
        body = _body(PAGE, "_push_web")
        assert "self._web_ready" in body, (
            "页面还没 ready 就推数据 —— 那几条消息会掉在地上")
