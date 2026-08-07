"""外壳:侧边栏 + 顶部连接栏 + 页面区 + 底部传输条。

这层不是"又一页",而是**包住每一页的那个框**,同时也是共享服务的持有者:
连接工厂、传输队列、预览、日志聚合、心跳、运行状态 watcher。

从另外两套前端继承、**不能改**的判据:

- **"端口可达" ≠ "在线"。** 这个网段的路由器会对整网段的 445 SYN 秒回 ACK,
  只有拿到过 SMB ECHO 往返的那台才配说"在线"。
- **不硬编码默认地址。** 顺序 ``ASTRO_SMB_HOST`` > ``devices.last_host()`` > 空;
  一台都没记过就不连,直接去扫描页。设备是 DHCP,写死的 IP 对新用户永远是错的。
- **"正在拍摄"不能回读日志。** Autorun 日志是会话结束时一次性写盘的,运行中
  设备上根本看不到;唯一可靠心跳是影像目录的最新帧 mtime(``watcher.py``)。

另外这里修了新前端的一个真缺陷:它**从不构造 TransferManager**(``run()`` 的
``transfers=`` 参数没人传),于是传输页永远空、浏览页「下载」静默 return。
这边照老 UI 的做法在启动时就建好。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QMainWindow, QStackedWidget,
                               QWidget)

from astro_smb import devices as devices_store
from astro_smb.backend import guess_kind, make_backend
from astro_smb import i18n
from astro_smb.i18n import N_, gettext as _
from astro_smb_app import settings
from astro_smb_app import transfers as xfer
from astro_smb_app.logstore import LogStore, detect_log_share
from astro_smb_app.preview import PreviewWorker
from astro_smb_app.views import devices as devices_view
from astro_smb_app.watcher import RunWatcher
from astro_smb_qt import theme, widgets as W
from astro_smb_qt.workers import Bg, with_client

log = logging.getLogger(__name__)

#: 导航:分组 → [(tag, 符号, 文案)]。顺序与文案照抄另外两套前端 ——
#: 用户的肌肉记忆在这上面。符号一律 BMP 几何字符(emoji 是星平面字符,
#: 跨平台字体覆盖差,老 UI 那边还会让 HSTRING 末尾少一个字)。
# 表里 `N_()` 只标记不翻,取用时才 `_()` —— 模块级求值一次,
# 直接 `_()` 会把整棵导航冻在 import 那一刻的语言上
NAV_GROUPS: list[tuple[str, list[tuple[str, str, str]]]] = [
    (N_("数据"), [
        ("browse", "▣", N_("浏览")),
        ("records", "▤", N_("拍摄记录")),
        ("guiding", "◈", N_("导星分析")),
    ]),
    (N_("影像"), [
        ("sky", "◉", N_("3D 天球")),
        ("fits", "◍", N_("影像查看")),
        ("space", "▦", N_("空间分析")),
    ]),
    (N_("设备"), [
        ("devices", "◫", N_("设备管理")),
        ("scan", "⌕", N_("扫描设备")),
        ("transfers", "⇅", N_("传输")),
    ]),
]

#: 文件**间**并发档位(与文件内分块并发是两回事,后者在传输页)
WORKER_CHOICES = [1, 2, 3, 4, 6, 8]
DEFAULT_WORKER_INDEX = 2      # → 3

HEARTBEAT_S = 4.0
TICK_MS = 250

WINDOW_TITLE = "Astro SMB Tool (Qt)"
#: 窗口标题后缀的环境变量。与老 UI 的 ``ASTRO_SMB_GUI_TITLE_TAG``、
#: Uno 前端的 ``ASTRO_SMB_UI_TITLE_TAG`` 是同一套约定。
TITLE_TAG_ENV = "ASTRO_SMB_QT_TITLE_TAG"


class Shell(QMainWindow):
    """主窗口。页面通过 ``page.shell`` 拿到这里的一切。"""

    #: 连接成功,携带共享名列表
    connected = Signal(list)
    #: 传输队列有变化(250ms 节流,页面自己去读 ``shell.transfers.jobs``)
    transfers_changed = Signal()
    #: 心跳:``{host, rtt, port_ok, server_name, dialect, shares}``
    heartbeat = Signal(dict)
    #: 运行状态 watcher:``{running, target, kind, seq, exposure_s, age_s, ...}``
    watch = Signal(dict)
    #: 日志读完了(站点经度反推出来了)。**浏览页要接它** —— 详情里的方位角
    #: 与迷你雷达用的是反推经度,读日志之前只能用 site.json 的兜底值,
    #: 差 1.4° 就是方位差 2°,而它安静地显示成一个错数字。
    logs_ready = Signal()
    #: 主题切换(自绘控件收到后 ``update()``)
    theme_changed = Signal(str)

    def __init__(self, *, host: str = "", page: str = "browse"):
        super().__init__()
        # **标题标签**:用户常开着自己的实例,截图/自动化按进程名匹配一定会误抓
        # (老 UI 与 Uno 前端各有一个同名钩子,这是第三个)。自动化只准按
        # `[AGENT]` 这类标签认自己那一个,绝不碰用户那个。
        tag = os.environ.get(TITLE_TAG_ENV, "")
        self.setWindowTitle(WINDOW_TITLE + (f" [{tag}]" if tag else ""))
        self.resize(1480, 940)

        self.bg = Bg(self)
        self.client_factory: Callable[[], Any] | None = None
        self.logstore = LogStore()          # **一个实例**,不是每次刷新新建
        # 日志预热用的执行器 —— 和页面各自的 Bg 分开:换页/换目录 bump 世代
        # 不该把这一趟预热作废掉。
        self._logbg = Bg(self)
        self.preview: PreviewWorker | None = None
        self.watcher: RunWatcher | None = None

        self._current_tag = ""
        self.conn: dict = {"host": "", "kind": "", "rtt": None,
                           "port_ok": False, "server_name": "",
                           "server_os": "", "dialect": "", "shares": 0,
                           "connecting": False,
                           # **心跳次数与最近时刻**:区分"现在真的在线"和
                           # "五分钟前说过在线然后卡住了"的唯一读数 ——
                           # 而连接状态卡存在的理由就是这个。
                           "beats": 0, "last_beat": 0.0}
        self.shares: list[str] = []
        self._records: list[dict] = []
        self._device_hosts: list[tuple[str, str]] = []
        self._jobs_dirty = threading.Event()
        self._hb_stop = threading.Event()
        self._hb_thread: threading.Thread | None = None
        self._closing = False

        self.transfers = xfer.TransferManager(
            client_factory=self._new_client,
            on_update=self._on_job_update,
            max_workers=WORKER_CHOICES[DEFAULT_WORKER_INDEX])

        self._build()
        self._pages: dict[str, Any] = {}
        self._install_pages()
        self.refresh_devices()

        want = host or _preferred_host()
        self.select_page(page if page in self._pages else "browse")
        if want:
            self.connect_device(want)
        else:
            # 一台都没记过 → **不猜地址,去找**。设备是 DHCP 的,写死一个 IP
            # 对新用户永远是错的;而停在扫描页等他点"开始扫描"同样不对 ——
            # 他还不知道要找什么。恰好一台疑似 ASIAIR 时直接连上。
            self.select_page("scan")
            self.notice(_("还没有记录任何设备 —— 正在自动扫描本网段找 ASIAIR"))
            page = self._pages.get("scan")
            if page is not None and hasattr(page, "autoscan"):
                QTimer.singleShot(0, page.autoscan)

        self._tick = QTimer(self)
        self._tick.timeout.connect(self._on_tick)
        self._tick.start(TICK_MS)

    # ------------------------------------------------------------ 界面

    def _build(self) -> None:
        root = QWidget()
        root.setObjectName("Root")
        row = QHBoxLayout(root)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)

        self.nav = W.SideNav(NAV_GROUPS)
        self.nav.navigate.connect(self.select_page)
        row.addWidget(self.nav)
        self._build_nav_footer()

        right = QWidget()
        right.setObjectName("PageArea")
        col = W.vbox(right, gap="none")
        col.addWidget(self._build_topbar())
        self.banner = _Banner(self)
        col.addWidget(self.banner)
        self.stack = QStackedWidget()
        col.addWidget(self.stack, 1)          # **主体吃掉剩余高度**
        col.addWidget(self._build_queue())
        row.addWidget(right, 1)

        self.setCentralWidget(root)

    def _build_topbar(self) -> QWidget:
        bar = QFrame()
        lay = W.hbox(bar, gap="sm", pad="card")
        lay.addWidget(W.label(_("设备"), role="subtitle"))
        self.device_combo = W.combo()
        self.device_combo.setMinimumWidth(320)
        lay.addWidget(self.device_combo)
        lay.addWidget(W.button(_("连接"), kind="primary",
                               on_click=lambda: self.connect_device()))
        self.forget_btn = W.button(_("忘记"), on_click=self._forget_device)
        lay.addWidget(self.forget_btn)
        lay.addStretch(1)
        self.conn_label = W.label(_("未选择设备"), role="body", tone="dim")
        lay.addWidget(self.conn_label)
        return bar

    def _build_nav_footer(self) -> None:
        """侧边栏底部常驻状态区:连接状态 + 红光模式开关。"""
        self.nav_state = W.label(_("未连接"), role="subtitle", wrap=True)
        self.nav.status.addWidget(self.nav_state)
        self.nav.status.addWidget(W.group_title(_("显示模式")))
        row = W.hbox(gap="xs")
        self._mode_btns: dict[str, Any] = {}
        for mode in theme.MODES:
            b = W.button(_(theme.MODE_LABEL[mode]), kind="ghost",
                         on_click=lambda m=mode: self.set_theme_mode(m))
            self._mode_btns[mode] = b
            row.addWidget(b)
        self.nav.status.addLayout(row)
        self._sync_mode_buttons()
        self._build_language_picker()

    #: 语言代码 → 给人看的名字。**用各语言自己的写法**(endonym):
    #: 一个只会英文的用户在中文界面里要找的是 "English",不是"英语"。
    LANG_NAMES = {"zh_CN": "简体中文", "zh_TW": "繁體中文", "en": "English",
                  "en_US": "English", "ja_JP": "日本語", "de_DE": "Deutsch",
                  "fr_FR": "Français", "es_ES": "Español", "ru_RU": "Русский"}

    def _build_language_picker(self) -> None:
        """语言下拉。**只在装了不止一种语言时才出现。**

        只有中文可选时摆一个一项的下拉是纯噪声 —— 而这恰恰是绝大多数用户
        看到的情况(源语言就是中文,不需要任何 `.mo`)。
        """
        langs = i18n.available_languages()
        if len(langs) < 2:
            return
        self.nav.status.addWidget(W.group_title(_("语言")))
        self._langs = langs
        cur = i18n.current_language()
        idx = langs.index(cur) if cur in langs else 0
        self.lang_combo = W.combo([self.LANG_NAMES.get(k, k) for k in langs],
                                  index=idx, on_change=self._set_language)
        self.nav.status.addWidget(self.lang_combo)

    def _set_language(self, idx: int) -> None:
        """记住选择,然后**替用户重启** —— 界面文案是建控件时烤进去的。

        为什么不做运行时热切:整棵控件树都要重建,而此刻可能有好几个后台
        worker 正拿着旧控件的引用(传输、预览、日志解析)。它们的 `on_done`
        会打到已经析构的 C++ 对象上 —— `RuntimeError: wrapped C++ object
        deleted`,而且是**随机时机**的崩,比"重启一下"糟糕得多。

        切语言是低频动作,重启是诚实且可靠的做法。**但要真的替他重启**,
        不是弹一句"请重新启动"就完事。
        """
        langs = getattr(self, "_langs", None)
        if not langs or not (0 <= idx < len(langs)):
            return
        want = langs[idx]
        if want == i18n.current_language():
            return
        name = self.LANG_NAMES.get(want, want)
        busy = self.transfers.active_count() if self.transfers else 0

        # **这个框必须是双语的。** 想切语言的人多半是**看不懂当前语言**的人 ——
        # 用当前语言拦住他,他连哪个按钮是"确定"都不知道。所以标题、正文、
        # 两个按钮都写两遍:当前语言 + 目标语言。
        # 传进 `two()` 的字面量要用 `N_()` 包一下 —— 抽取器只认 `_`/`gettext`/
        # `N_` 这几个名字,`two(...)` 它看不见。少了这一步,这四条永远进不了
        # 词表:框还是会弹,只是**两边都是当前语言**,而这个框存在的全部意义
        # 就是给看不懂当前语言的人看。
        def two(msg: str, sep: str = "\n", **kw) -> str:
            here = _(msg).format(**kw) if kw else _(msg)
            there = i18n.gettext_in(want, msg)
            there = there.format(**kw) if kw else there
            return here if here == there else here + sep + there

        title = two(N_("切换到 {name}?"), sep="   /   ", name=name)
        body = two(N_("界面文案在启动时生成,切换语言需要重启。现在就重启吗?"),
                   sep="\n\n")
        if busy:
            body += "\n\n" + two(
                N_("注意:还有 {busy} 个传输在进行,重启会中断它们。"),
                sep="\n", busy=busy)
        if not self.confirm(title, body,
                            ok_text=two(N_("重启"), sep=" / "),
                            cancel_text=two(N_("取消"), sep=" / ")):
            # 用户改主意了 —— 把下拉拨回去,否则它显示的语言和实际的对不上
            cur = i18n.current_language()
            self.lang_combo.blockSignals(True)
            self.lang_combo.setCurrentIndex(
                langs.index(cur) if cur in langs else 0)
            self.lang_combo.blockSignals(False)
            return
        settings.set(settings.KEY_LANGUAGE, want)
        self._restart()

    def _restart(self) -> None:
        """原样重开一个自己,然后退出。

        **必须 detached**:非 detached 的子进程会跟着父进程一起死。
        用 `sys.argv` 原样重放,连 `--host`/`--page` 都保留 —— 重启之后
        落在同一个地方,而不是回到首页。
        """
        import sys

        from PySide6.QtCore import QProcess

        QProcess.startDetached(sys.executable, sys.argv)
        self.close()

    def _build_queue(self) -> QWidget:
        """底部传输队列条 —— **常驻**,不随页面消失。"""
        bar = QFrame()
        lay = W.hbox(bar, gap="sm", pad="card")
        lay.addWidget(W.label(_("传输队列"), role="title"))
        lay.addWidget(W.label(_("并发"), role="subtitle"))
        self.worker_combo = W.combo([str(n) for n in WORKER_CHOICES],
                                    index=DEFAULT_WORKER_INDEX,
                                    on_change=self._set_workers)
        lay.addWidget(self.worker_combo)
        self.queue_label = W.label(_("空闲"), role="subtitle")
        lay.addWidget(self.queue_label)
        self.queue_chip = W.StatusChip("")
        lay.addWidget(self.queue_chip)
        lay.addStretch(1)
        lay.addWidget(W.button(_("全部取消"), on_click=self._cancel_all))
        lay.addWidget(W.button(_("清除已完成"), on_click=self._clear_done))
        return bar

    def _install_pages(self) -> None:
        from astro_smb_qt.pages import build_pages

        for tag, page in build_pages(self).items():
            self._pages[tag] = page
            self.stack.addWidget(page)

    # ------------------------------------------------------------ 主题

    def set_theme_mode(self, mode: str) -> None:
        if mode == theme.current_mode():
            return
        theme.set_mode(mode)
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None:
            theme.apply(app)
        W.restyle(self)
        self._sync_mode_buttons()
        self.theme_changed.emit(mode)

    def _sync_mode_buttons(self) -> None:
        for mode, btn in self._mode_btns.items():
            btn.setProperty("kind", "primary" if mode == theme.current_mode()
                            else "ghost")
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    # ------------------------------------------------------------ 页面

    def select_page(self, tag: str) -> None:
        page = self._pages.get(tag)
        if page is None:
            return
        self.stack.setCurrentWidget(page)
        self.nav.set_active(tag)
        self._current_tag = tag
        hook = getattr(page, "on_show", None)
        if hook is not None:
            try:
                hook()
            except Exception:            # noqa: BLE001
                log.exception(_("页面 on_show 失败: %s"), tag)
                self.notice(_("{tag} 页初始化失败,详情见日志").format(tag=tag))

    def page(self, tag: str):
        return self._pages.get(tag)

    # -- 页间跳转 ------------------------------------------------------
    def open_browser_path(self, share: str, path: str = "") -> None:
        page = self._pages.get("browse")
        if page is None:
            return
        self.select_page("browse")
        page.open_path(share, path)

    def open_guiding(self, t0, t1, label: str) -> None:
        page = self._pages.get("guiding")
        if page is None:
            return
        self.select_page("guiding")
        page.show_range(t0, t1, label)

    def open_fits(self, share: str, path: str) -> None:
        page = self._pages.get("fits")
        if page is None:
            return
        self.select_page("fits")
        page.open(share, path)

    # ------------------------------------------------------------ 提示

    def notice(self, text: str, tone: str = "warn") -> None:
        self.banner.show_notice(text, tone)

    def clear_notice(self) -> None:
        self.banner.clear_notice()

    def notice_text(self) -> str:
        """当前提示条的文字(空串 = 没有提示)。

        提示条是错误的**持久**通道:页面里的状态行会被下一次列目录的结果盖掉,
        而横幅要用户自己点 ✕ 才消失。
        """
        return self.banner.notice_text()

    # ------------------------------------------------------------ 设备

    def refresh_devices(self) -> None:
        self._records = devices_view.sorted_records(devices_store.load())
        self._device_hosts = [(r.get("host", ""), r.get("kind", ""))
                              for r in self._records]
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        for rec in self._records:
            host = rec.get("host") or ""
            shown = host if len(host) <= 34 else "…" + host[-33:]
            self.device_combo.addItem(f"{shown}  ·  {devices_store.summary(rec)}")
        want = (os.environ.get("ASTRO_SMB_HOST", "") or self.conn["host"]
                or devices_store.last_host())
        for i, (host, _kind) in enumerate(self._device_hosts):
            if want and devices_store.same_host(host, want):
                self.device_combo.setCurrentIndex(i)
                break
        self.device_combo.blockSignals(False)
        self.forget_btn.setEnabled(bool(self._records))

    def _forget_device(self) -> None:
        idx = self.device_combo.currentIndex()
        if not (0 <= idx < len(self._device_hosts)):
            return
        devices_store.forget(self._device_hosts[idx][0])
        self.refresh_devices()
        dev = self._pages.get("devices")
        if dev is not None:
            dev.reload()

    def connect_device(self, host: str = "", kind: str = "") -> None:
        if not host:
            idx = self.device_combo.currentIndex()
            if 0 <= idx < len(self._device_hosts):
                host, kind = self._device_hosts[idx]
        if not host:
            self.notice(_("没有可连接的设备 —— 先去「扫描设备」找一台"))
            self.select_page("scan")
            return
        kind, path = _backend_spec(host, kind)

        def factory():
            be = make_backend(kind=kind, host=host, path=path)
            be.connect()
            return be

        self.client_factory = factory
        self.conn.update(host=host, kind=kind, connecting=True, rtt=None)
        self._render_conn()
        gen = self.bg.bump()

        def work():
            client = factory()
            try:
                shares = [s.name for s in client.list_shares()]
                info = {}
                try:
                    info = client.server_info() or {}
                except Exception:        # noqa: BLE001 - 有的后端没有
                    pass
                # 日志在哪个共享底下:SMB 是 `EMMC Images`,**本地磁盘后端是
                # 卷标**(单共享模型)。原来这里不探,`LogStore.share` 就停在
                # 常量 —— 插卡设备(共享名是卷标)找不到 log/,经度退回兜底,
                # 而失败只在状态栏一闪而过。老 UI 一直有这一步。
                return (shares, info, detect_log_share(client, shares),
                        getattr(client, "host", "") or host)
            finally:
                client.close()

        self.bg.run(work, gen=gen, on_done=self._on_connected,
                    on_error=self._on_connect_failed)

    def _on_connected(self, payload) -> None:
        shares, info, log_share, real_host = payload
        self.conn.update(connecting=False, shares=len(shares),
                         server_name=info.get("server_name", ""),
                         server_os=info.get("server_os", ""),
                         dialect=info.get("dialect", ""),
                         beats=0, last_beat=0.0)
        self.shares = list(shares)
        devices_store.remember(self.conn["host"], info.get("server_name"),
                               info.get("server_os"), info.get("dialect"),
                               len(shares), kind=self.conn["kind"])
        self.refresh_devices()
        # **绑后端给的规范 host,不是用户原样输入的那串。** 本地目录设备上
        # 用户可能敲 `C:/…/device`(正斜杠),而 `LocalBackend.host` 是
        # `str(self.root)`(反斜杠)—— 两串不等,`LogStore.data` 的守卫每次
        # 都命中,解析好的日志被自己挡在门外(方位角悄悄退回兜底经度)。
        # 共享层的 `host_key` 已经兜住了这条,这里再从源头对齐,顺带让详情
        # 里的完整路径不再是 `\\C:/Users/…\…` 这种混着两种分隔符的样子。
        self.logstore.bind(real_host or self.conn["host"], log_share)
        self.watcher_share = log_share
        self._warm_logs()
        self._start_preview()
        self._start_heartbeat()
        self._start_watcher()
        self.clear_notice()
        self._render_conn()
        self.connected.emit(list(shares))
        # **连上之后要把当前页再 on_show 一次。** 启动顺序是"先选页、后连设备"
        # (连接是异步的),所以首屏那次 on_show 看到的是"还没连接" —— 不补这一下,
        # 界面会停在空态,而顶栏明明写着已连接。真机截图上就是这样。
        current = self._pages.get(getattr(self, "_current_tag", ""))
        if current is not None and hasattr(current, "on_show"):
            current.on_show()

    def _on_connect_failed(self, exc: BaseException) -> None:
        self.conn.update(connecting=False, rtt=None)
        self._render_conn()
        self.notice(_("连接 {0} 失败: {exc}").format(self.conn['host'], exc=exc), "bad")

    def _new_client(self):
        """给 TransferManager / PreviewWorker 用的连接工厂。"""
        if self.client_factory is None:
            raise RuntimeError(_("还没有连接任何设备"))
        return self.client_factory()

    def transfers_demo_factory(self):
        """给传输页的演示任务借一条连接(``--auto`` 用)。"""
        return self._new_client

    # ------------------------------------------------------------ 心跳

    def _start_heartbeat(self) -> None:
        if self._hb_thread is not None and self._hb_thread.is_alive():
            return
        self._hb_stop.clear()

        def loop():
            client = None
            # **先跳一拍再等,不是先等再跳。**
            #
            # 原来是 `while not wait(HEARTBEAT_S)`:连接成功之后的**头 4 秒
            # 一次心跳都没有**,而 `conn` 里此时还没有 rtt —— 状态栏那 4 秒
            # 显示的是「○ 断开」,尽管设备就在那儿、刚刚才连上。
            # 空间页尤其明显:`--auto` 一进页面就开扫,人眼正盯着它,
            # 而那几秒正好落在第一拍之前(独立验收就是在这儿抓到的)。
            #
            # 老 UI 的 watcher 也是这个规矩:连上就 `poke()` 一轮,不等。
            while True:
                factory = self.client_factory
                if factory is None:
                    if self._hb_stop.wait(HEARTBEAT_S):
                        break
                    continue
                try:
                    if client is None:
                        client = factory()          # **独立克隆连接**
                    echo = getattr(client, "echo", None)
                    rtt = float(echo()) if echo is not None else 0.0
                    state = dict(self.conn, rtt=rtt, port_ok=True)
                except Exception:                   # noqa: BLE001
                    try:
                        if client is not None:
                            client.close()
                    except Exception:               # noqa: BLE001
                        pass
                    client = None
                    port_ok = False
                    try:
                        probe = factory()
                        port_ok = probe.ping_tcp() is not None
                        probe.close()
                    except Exception:               # noqa: BLE001
                        pass
                    # **只敢说"端口可达"** —— 路由器会对整网段 445 假 ACK
                    state = dict(self.conn, rtt=None, port_ok=port_ok)
                if state["rtt"] is not None:
                    self.conn["beats"] = int(self.conn.get("beats", 0)) + 1
                    self.conn["last_beat"] = time.time()
                state["beats"] = self.conn.get("beats", 0)
                state["last_beat"] = self.conn.get("last_beat", 0.0)
                self.conn.update(rtt=state["rtt"], port_ok=state["port_ok"])
                try:
                    self.heartbeat.emit(dict(state))
                except RuntimeError:
                    # **窗口已关、信号对象没了。** 心跳是 daemon 线程,
                    # 窗口先于它消失是正常的;这时候 emit 会抛
                    # `Signal source has been deleted`,不接住就是解释器
                    # 退出时刷一段没人看得懂的 traceback。
                    # `workers.py` 的 `_Job.run` 早就是这么处理的。
                    #
                    # 这条以前几乎撞不上:第一拍要等 4 秒,窗口早关完了。
                    # 改成"连上就跳第一拍"之后 0.18s 就发,才露出来。
                    break
                if self._hb_stop.wait(HEARTBEAT_S):
                    break
            if client is not None:
                try:
                    client.close()
                except Exception:                   # noqa: BLE001
                    pass

        self._hb_thread = threading.Thread(target=loop, daemon=True,
                                           name="qt-heartbeat")
        self._hb_thread.start()
        self.heartbeat.connect(self._on_heartbeat)

    def _on_heartbeat(self, state: dict) -> None:
        self._render_conn()

    def _render_conn(self) -> None:
        text, tone = connection_text(self.conn)
        self.conn_label.setText(text)
        W.set_prop(self.conn_label, "tone", tone)
        # **本地设备的 host 是一条完整路径**,原样塞进边栏会占掉三四行。
        # 掐头留尾 —— 尾巴才是能认出是哪台的部分(IP 从不需要截断)。
        host = self.conn.get("host") or _("未连接")
        if len(host) > 26:
            host = "…" + host[-25:]
        rtt = self.conn.get("rtt")
        line = host if rtt is None else _('{host}\n● 在线 {rtt:.0f} ms').format(
            host=host, rtt=rtt)
        self.nav_state.setText(line)
        W.set_prop(self.nav_state, "tone", "ok" if rtt is not None else "")

    # ------------------------------------------------------------ watcher

    def _warm_logs(self) -> None:
        """连上之后**后台读一次日志**,只为把站点经度反推出来。

        经度不是可选项:浏览页详情里的方位角与迷你雷达都用它。没有它就退回
        `site.json` 的兜底 120°,而真值是 121.44° —— 方位差 2°,而且**不报错**,
        界面上就是一个看起来很正常的错数字(独立验收抓到的正是这条:
        冷启动 180°,切一趟拍摄记录页再回来变成 182°)。

        日志下过之后有磁盘缓存,`LogStore` 内部也全程串行,所以这一下很便宜;
        失败就算了(没有日志的设备照样能用浏览页)。
        """
        factory = self.client_factory
        if factory is None:
            return

        def work():
            return with_client(factory, self.logstore.refresh)

        self._logbg.run(work, gen=self._logbg.bump(),
                        on_done=lambda _d: self.logs_ready.emit(),
                        on_error=lambda _e: None)

    def _start_watcher(self) -> None:
        # **共享名要跟着探测结果走。** watcher 是照着
        # `<share>/Plan/Light` 与 `<share>/Autorun` 找最新帧的,共享名不对
        # 就每一轮 listdir 都失败 —— 表现是"正在拍摄"横幅永远不出现,
        # 而且不报错(watcher 把异常当"读不到"处理)。
        share = getattr(self, "watcher_share", "") or ""
        if self.watcher is not None:
            if share:
                self.watcher.share = share
            self.watcher.poke()
            return
        self.watcher = RunWatcher(
            host_getter=lambda: self.conn.get("host") or "",
            on_state=lambda st: self.watch.emit(dict(st)),
            client_factory=lambda _host: self._new_client())
        if share:
            self.watcher.share = share
        self.watch.connect(self._on_watch)
        self.watcher.start()

    def _on_watch(self, state: dict) -> None:
        self.banner.show_watch(watch_text(state))
        if state.get("new_logs"):
            # 新日志 = 一段会话刚结束 → 缓存作废
            self.logstore.invalidate()
            self.notice(_("发现 {0} 份新日志,拍摄记录/导星分析下次刷新会重新解析").format(
                len(state['new_logs'])), "accent")

    # ------------------------------------------------------------ 预览

    def _start_preview(self) -> None:
        if self.preview is not None:
            return
        self.preview = PreviewWorker(self._new_client, self._on_preview)

    def _on_preview(self, result) -> None:
        """**在 worker 线程上被调用** —— 只能转发给页面的信号。"""
        page = self._pages.get("browse")
        if page is not None:
            page.preview_ready.emit(result)

    # ------------------------------------------------------------ 传输

    def _on_job_update(self, job) -> None:
        """TransferManager 在**工作线程**上调这里。只置脏,不碰控件。"""
        self._jobs_dirty.set()

    def _set_workers(self, idx: int) -> None:
        if 0 <= idx < len(WORKER_CHOICES):
            self.transfers.set_workers(WORKER_CHOICES[idx])

    def _cancel_all(self) -> None:
        self.transfers.cancel_all()
        self._jobs_dirty.set()

    def _clear_done(self) -> None:
        self.transfers.clear_finished()
        self._jobs_dirty.set()

    def _on_tick(self) -> None:
        """250ms 一拍。**进度靠它动**,不靠每个回调各发一次信号 ——
        10 Hz × N 个任务会把事件循环淹掉。

        整拍包一层兜底:这里抛异常会**把整条刷新链一起带走**(传输页从此不再
        更新,而界面上什么都不说)。真机上就栽过一次 —— ``stats()`` 的形状读错,
        ``int()`` 抛 TypeError,于是底部队列条永远写着"空闲"、传输页永远停在
        "0 B / 49.77 MB 进行中",两个症状一个根因。
        """
        try:
            if not self._jobs_dirty.is_set():
                jobs = list(getattr(self.transfers, "jobs", ()) or ())
                if not any(j.running for j in jobs):
                    return
            self._jobs_dirty.clear()
            self._render_queue()
        except Exception:                    # noqa: BLE001
            log.exception(_("刷新传输队列失败"))
        finally:
            self.transfers_changed.emit()

    def _render_queue(self) -> None:
        """底部队列条。

        **``TransferManager.stats()`` 返回的是任务列表,不是计数**
        (``{"running": [...], "queued": [...], "done": [...], "failed": [...],
        "speed": …, "done_bytes": …}``)。当成计数用会 TypeError。
        """
        from astro_smb.util import human_size

        stats = self.transfers.stats()
        active = len(stats.get("running") or ())
        queued = len(stats.get("queued") or ())
        failed = len(stats.get("failed") or ())
        speed = float(stats.get("speed", 0.0) or 0.0)
        if active or queued:
            self.queue_label.setText(
                _("{active} 个进行中 · {queued} 个排队 · {0}/s").format(
                    human_size(int(speed)), active=active, queued=queued))
            self.queue_chip.set(f"{active}/{active + queued}", "accent")
            return
        done = len(stats.get("done") or ())
        bits = []
        if done:
            bits.append(_("已完成 {done} 个").format(done=done))
        if failed:
            bits.append(_("失败/取消 {failed} 个").format(failed=failed))
        self.queue_label.setText(" · ".join(bits) or _("空闲"))
        self.queue_chip.set(str(failed) if failed else "",
                            "bad" if failed else None)

    # ------------------------------------------------------------ 关闭

    def closeEvent(self, ev):  # noqa: N802
        self._closing = True
        self._tick.stop()
        self._hb_stop.set()
        if self.watcher is not None:
            self.watcher.stop()
        if self.preview is not None:
            try:
                self.preview.shutdown()
            except Exception:            # noqa: BLE001
                pass
        try:
            self.transfers.shutdown()
        except Exception:                # noqa: BLE001
            pass
        for page in self._pages.values():
            hook = getattr(page, "on_close", None)
            if hook is not None:
                try:
                    hook()
                except Exception:        # noqa: BLE001
                    log.debug(_("页面关闭钩子失败"), exc_info=True)
        super().closeEvent(ev)


# ---------------------------------------------------------------- 纯函数

def _preferred_host() -> str:
    """启动地址。**不猜** —— 一台都没记过就返回空,由调用方去扫描页。"""
    return os.environ.get("ASTRO_SMB_HOST", "") or devices_store.last_host()


def _backend_spec(host: str, kind: str = "") -> tuple[str, str]:
    """``host`` → ``(kind, path)``,交给 ``make_backend``。

    **只有两种设备**:SMB 网络设备,和本地目录(ZWO 卡直插电脑、或把卡的内容
    拷到本机)。后者是正式支持的设备类型,不是测试设施 —— 所以离线开发也走
    这一条,不必再造第三种后端。

    **种类判定必须走共享层的 `backend.guess_kind`,不许在这里自己写一份。**
    原来这里是 ``"local" if devices_store._looks_local(host) else "smb"`` ——
    而 `_looks_local` 只认盘符与绝对路径,**相对路径会被判成 smb**:
    ``--host ".tmp/device/EMMC Images"`` 于是被当成主机名,socket 在 IDNA
    编码时炸。这条真机故障当初是有测试守着的,只不过那些测试盯的是已删的
    Uno,Qt 这边一直漏着(删 Uno 时把它们转指过来才发现)。
    """
    if kind:
        return kind, host
    return guess_kind(host), host


RTT_GOOD, RTT_WARN = 30.0, 100.0


def rtt_tone(ms: float | None) -> str:
    if ms is None:
        return "bad"
    if ms < RTT_GOOD:
        return "ok"
    return "warn" if ms < RTT_WARN else "bad"


def connection_text(state: dict) -> tuple[str, str]:
    """(文本, 语义色)。**措辞分三档**,因为这三件事真的不一样。"""
    if not state.get("host"):
        return _("未选择设备"), "dim"
    if state.get("connecting") and state.get("rtt") is None:
        return _("正在连接 {0} …").format(state['host']), "dim"
    if state.get("rtt") is not None:
        bits = []
        if state.get("server_name"):
            bits.append(str(state["server_name"]))
        if state.get("dialect"):
            bits.append(f"({state['dialect']})")
        head = (_("已连接 ") + " ".join(bits)) if bits else _("已连接")
        if state.get("shares"):
            head += _(" — {0} 个共享").format(state['shares'])
        return f"{head}  ● {state['rtt']:.0f} ms", rtt_tone(state["rtt"])
    if state.get("port_ok"):
        # 路由器会假 ACK 整个网段,所以端口通不等于设备在
        return _("○ 端口可达(未握手)"), "warn"
    return _("○ 断开"), "bad"


def watch_text(watch: dict | None) -> str | None:
    """运行状态横幅。不在拍就返回 None —— 没事的时候不占地方。"""
    if not watch or not watch.get("running"):
        return None
    parts = [_("正在拍摄 {0}").format(watch.get('target') or _("未知目标"))]
    if watch.get("seq") is not None:
        parts.append(_("第 {0} 张").format(watch['seq']))
    if watch.get("exposure_s"):
        parts.append(f"{watch['exposure_s']:g}s")
    if watch.get("age_s") is not None:
        parts.append(_("{0:.0f} 秒前落盘").format(watch['age_s']))
    return " · ".join(parts)


class _Banner(QFrame):
    """运行状态横幅 + 提示条。两者都没有时整条隐藏。"""

    def __init__(self, shell: Shell):
        super().__init__(shell)
        self._shell = shell
        lay = W.hbox(self, gap="sm", pad="sm")
        self._watch = W.StatusChip("", "ok")
        self._watch.setVisible(False)
        self._notice = W.label("", role="body", wrap=True)
        self._notice.setVisible(False)
        self._close = W.button("✕", kind="ghost", on_click=self.clear_notice)
        self._close.setVisible(False)
        lay.addWidget(self._watch)
        lay.addWidget(self._notice, 1)
        lay.addWidget(self._close)
        self.setVisible(False)

    def show_watch(self, text: str | None) -> None:
        self._watch.set(text or "", "ok")
        self._sync()

    def show_notice(self, text: str, tone: str = "warn") -> None:
        self._notice.setText(text)
        W.set_prop(self._notice, "tone", tone)
        W.show_if(self._notice, text)
        W.show_if(self._close, text)
        self._sync()

    def clear_notice(self) -> None:
        self._notice.setText("")
        self._notice.setVisible(False)
        self._close.setVisible(False)
        self._sync()

    def notice_text(self) -> str:
        # **不能用 isVisible() 判**:顶层窗口没 show() 时子控件一律报不可见
        # (无头跑就是这种情况),于是"提示明明设上了却读不到"。以文本为准。
        return self._notice.text()

    def _sync(self) -> None:
        """**以文本为准,不能用 `isVisible()`。**

        整条横幅初始是隐藏的,而 Qt 里**父控件隐藏时子控件的 `isVisible()`
        恒为 False** —— 于是 `show_notice()` 设完文字调到这里,算出的是
        `False or False`,横幅**永远不出现**。

        代价是全局的:九个页面 `Page.report()` 报的后台异常、所有
        `shell.notice()` 的提示、以及 watcher 的「正在拍摄 X 第 N 张」
        (shell 里只有这一个出口)一条都显示不出来。设备页那几个
        "点了没反应"全是它的产物;真机上每一次 SMB 失败都会表现成
        "界面卡住不动"。

        上面 `notice_text()` 的注释一字不差地写着这个坑,而它下面两行就
        踩了进去 —— 知道一条规则和在每一处都守住它是两回事。
        """
        W.show_if(self, self._watch.text() or self._notice.text())
