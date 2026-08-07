"""App 外壳:NavigationView + 页面宿主 + 共享连接/传输队列。

线程模型见 app.py。三个页面(浏览/空间分析/扫描)各自持有从 shell 引用来的
client、dispatcher、transfers、preview;UI 更新一律经 shell.ui() 编组回 UI 线程。
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path

from win32more import asyncui
from win32more.winui3 import XamlApplication
from win32more.Microsoft.UI.Dispatching import DispatcherQueue
from win32more.Microsoft.UI.Xaml import (
    FrameworkElement,
    GridLength,
    GridUnitType,
    Thickness,
    VerticalAlignment,
    Window,
)
from win32more.Microsoft.UI.Xaml.Controls import (
    Button,
    ColumnDefinition,
    ComboBox,
    ComboBoxItem,
    ComboBoxTextSubmittedEventArgs,
    ContentDialog,
    ContentDialogButton,
    ContentDialogResult,
    Grid,
    InfoBar,
    InfoBarSeverity,
    ListView,
    NavigationView,
    NavigationViewItem,
    Orientation,
    ProgressBar,
    ProgressRing,
    StackPanel,
    TextBlock,
)
from win32more.Microsoft.UI.Xaml.Markup import XamlReader
from win32more.Microsoft.UI.Xaml.Media import MicaBackdrop, SolidColorBrush
from win32more.Windows.Graphics import SizeInt32
from win32more.Windows.UI import Color

from astro_smb.backend import is_local, make_backend
from astro_smb.client import AstroSmbClient, SmbClientError
from astro_smb.util import human_size
from astro_smb.i18n import gettext as _
from astro_smb_gui import devices, metacache, volumes
from astro_smb_gui.logstore import LOG_SHARE as LOG_SHARE_DEFAULT
from astro_smb_gui._browser import BrowserPage
from astro_smb_gui._common import looks_like_local_path, unbox_str
from astro_smb_gui._guiding import GuidingPage
from astro_smb_gui._monitor import MonitorPage
from astro_smb_gui._records import RecordsPage
from astro_smb_gui._devices import DevicesPage
from astro_smb_gui._sky3d import Sky3DPage
from astro_smb_gui._fitsview import FitsViewPage
from astro_smb_gui._scan import ScanPage
from astro_smb_gui._space import SpacePage
from astro_smb_gui.logstore import LogStore
from astro_smb_gui.preview import PreviewWorker, clear_cache
from astro_smb_gui.transfers import (
    CANCELLED,
    DONE_S,
    ERROR,
    SKIPPED,
    TransferJob,
    TransferManager,
)
from astro_smb_gui.watcher import RunWatcher
from astro_smb_gui._xamli18n import load_text as _xaml_text

XAML_PATH = Path(__file__).with_name("main.xaml")

DEV_PROBE_SECONDS = 20.0        # 设备存活探测周期
# 没有任何设备记录时给 client 的占位地址(RFC 5737 文档网段,永不路由):
# 只为让 self.client 始终是个可用对象(transfers/preview 的 client_factory
# 捕获了 self.client.clone()),**绝不 connect**,等用户选/扫到设备再换掉。
PLACEHOLDER_HOST = "192.0.2.1"

_GREEN = (0x4C, 0xAF, 0x50)     # 存活
_GRAY = (0x9E, 0x9E, 0x9E)      # 离线/未探测
_BRUSHES: dict[tuple[int, int, int], SolidColorBrush] = {}


# 判定本地路径的实现已挪到 _common —— 扫描页也要用它(判断"当前设备根本不在
# 局域网上"),而扫描页不能反向 import 本模块(循环)。这里保留同名别名,
# 免得动本文件里既有的调用点。
_looks_like_local_path = looks_like_local_path


def _brush(rgb: tuple[int, int, int]) -> SolidColorBrush:
    """画刷复用:同一颜色只建一次(下拉项每 20s 刷新一次颜色)。"""
    br = _BRUSHES.get(rgb)
    if br is None:
        c = Color()
        c.A, c.R, c.G, c.B = 255, *rgb
        br = SolidColorBrush(c)
        _BRUSHES[rgb] = br
    return br


class App(XamlApplication):
    # ---------- 启动 ----------

    def OnLaunched(self, args) -> None:
        self.win = Window()
        self.win.SystemBackdrop = MicaBackdrop()
        # 钩子(§7.10): 自动化测试实例加标题后缀,截图脚本不会误抓用户实例
        title_tag = os.environ.get("ASTRO_SMB_GUI_TITLE_TAG", "")
        # 无缓存模式要在标题上常驻可见:它会让一切都变慢,不标出来很容易
        # 拿"冷启动的耗时"当成常态性能来判断(信息条会被关掉,标题不会)
        if metacache.bypass_reads():
            title_tag = (title_tag + " · " if title_tag else "") + _("无缓存")
        self.win.Title = "Astro SMB Tool" + (f" [{title_tag}]" if title_tag else "")

        root = XamlReader.Load(_xaml_text(XAML_PATH))
        self.win.Content = root
        self.root = self.win.Content.as_(FrameworkElement)
        self.dispatcher: DispatcherQueue = DispatcherQueue.GetForCurrentThread()

        # 顶栏状态 + 心跳状态。必须早于设备下拉的构建:下拉项的存活徽章
        # (_dev_status)要读 self.hb / self._hb_host 判断"当前这台是否在线"。
        self._status_base = _("未连接")
        self._hb_tail = ""
        self._watch_tail = ""
        self.hb: dict = {}
        self._hb_host: str | None = None
        self._hb_ok = 0
        self._hb_fail = 0
        self._hb_last_ok_str = ""

        # 设备记录/下拉状态(必须早于 _wire_events:重建下拉会触发 SelectionChanged)
        self._host_sync = False                     # True=程序化改动下拉,忽略选中事件
        self._connecting = False                    # 连接中(回车/按钮/下拉防重入)
        self._pending_host: str | None = None       # 在途连接期间用户又选的设备
        self._dev_hosts: list[str] = []             # 下拉项索引 → host(项内容是纯文本)
        self._dev_rtt: dict[str, float | None] = {}  # host → TCP 445 时延(None=不可达)
        self._dev_stop = threading.Event()
        self._dev_wake = threading.Event()

        self._find_controls()
        self._wire_events()

        # 初始设备地址优先级:ASTRO_SMB_HOST 环境变量(§7.10 钩子,与 CLI 同名)
        # > 设备记录里最近一次连接成功的 > 空。
        # **不再硬编码 192.0.2.225**:DHCP 会换 IP,别人的设备也不是这台。
        env_host = (os.environ.get("ASTRO_SMB_HOST") or "").strip()
        start_host = env_host or self._startup_host()
        self._rebuild_device_items()
        self.set_host(start_host)

        # 后端可能是 SMB 也可能是**本地磁盘**(ASIAIR 卡直接插电脑上),
        # 两者方法签名一致(astro_smb.backend.StorageBackend),各页零改动
        self.client = self._make_backend(start_host or PLACEHOLDER_HOST)
        self._shares = []
        self._log_share = LOG_SHARE_DEFAULT   # 日志所在共享(本地盘是卷标)

        self.transfers = TransferManager(
            client_factory=lambda: self.client.clone(),
            on_update=lambda job: self.ui(self._on_transfer_update, job),
        )
        self._transfer_rows: dict[int, dict] = {}
        # 底部精简条:有 group 的任务按组聚合为一行(keyed by 组名),
        # 防止文件夹展开出几百行撑爆底部条
        self._group_transfer_rows: dict[str, dict] = {}
        self.preview = PreviewWorker(
            client_factory=lambda: self.client.clone(),
            on_result=lambda r: self.ui(self._deliver_preview, r),
        )

        # 日志数据层 + 运行状态 watcher(拍摄记录/导星分析/状态栏共用)
        self.logstore = LogStore()
        self._watch_host: str | None = None
        self.watcher = RunWatcher(
            host_getter=lambda: self._watch_host,
            on_state=lambda st: self.ui(self._apply_watch, st),
            client_factory=self._make_backend,   # 本地磁盘设备也要能轮询
        )
        # 3D 实际视场后台产出的“主镜星点 + WCS + PHD2”诊断按拍摄 run
        # 共享给记录页展示；空间页自身不承载导星结论。
        self._guide_quality: dict[tuple, object] = {}
        self._guide_quality_state: dict[tuple, dict] = {}

        # 页面
        self.browser = BrowserPage(self)
        self.records = RecordsPage(self)
        self.guiding = GuidingPage(self)
        self.sky3d = Sky3DPage(self)
        self.fitsview = FitsViewPage(self)
        self.space = SpacePage(self)
        self.devices_page = DevicesPage(self)
        self.scan = ScanPage(self)
        self.monitor = MonitorPage(self)
        self._pages = {"browse": self.browser, "records": self.records,
                       "guiding": self.guiding, "sky3d": self.sky3d,
                       "fitsview": self.fitsview, "space": self.space,
                       "devices": self.devices_page,
                       "scan": self.scan, "monitor": self.monitor}
        self._current_page = None
        self._show_page("browse")

        try:
            clear_cache()
            # 元数据缓存体积维护:磁盘 I/O + 可能 VACUUM,绝不能放 UI 线程
            threading.Thread(target=lambda: metacache.vacuum_if_large(64),
                             daemon=True, name="meta-vacuum").start()
        except Exception:
            pass
        try:
            # AppWindow.Resize 收**物理像素**(§7.1):125% 缩放下 1460 物理只有
            # 1168 逻辑,浏览页/空间页最右侧的固定宽列(330)会被挤出窗口。
            # 按当前 DPI 换算,保证逻辑宽度 ~1400(容得下 左栏+主区+右栏)。
            scale = self._dpi_scale()
            size = SizeInt32()
            size.Width = int(1400 * scale)
            size.Height = int(880 * scale)
            self.win.AppWindow.Resize(size)
        except Exception as ex:
            print(_("窗口调整大小失败: {ex}").format(ex=ex), flush=True)

        self.win.Closed += self._on_closed
        self.win.Activate()

        # 插着的 ZWO 卡自动进设备记录(工作线程:卷枚举要读 disk_usage,
        # 网络盘可能慢;实测 10 个卷约 57ms,但不放 UI 线程更稳)
        threading.Thread(target=self._discover_local_devices,
                         daemon=True, name="vol-discover").start()

        if start_host:
            asyncui.create_task(self._connect())
        else:
            # 从没连过任何设备:不猜地址(硬编码的 IP 对新用户永远是错的),
            # 直接去扫描页开扫,扫到后点「连接」即记录下来
            self.status(_("未记录设备,正在扫描局域网 …"))
            self.select_page("scan")
            self.scan._on_scan(None, None)
        self._start_dev_probe()

        auto_close = os.environ.get("ASTRO_SMB_GUI_AUTOCLOSE")
        if auto_close:
            asyncui.create_task(self._auto_close(float(auto_close)))

    def _dpi_scale(self) -> float:
        """当前窗口的 DPI 缩放(125% → 1.25);取不到按 1.0。"""
        try:
            import ctypes
            hwnd = self.win.AppWindow.Id.Value
            dpi = ctypes.windll.user32.GetDpiForWindow(ctypes.c_void_p(hwnd))
            if dpi:
                return max(1.0, float(dpi) / 96.0)
        except Exception:
            pass
        return 1.0

    async def _auto_close(self, seconds: float) -> None:
        await asyncio.sleep(seconds)
        print("AUTOCLOSE", flush=True)
        self.win.Close()

    def _find_controls(self) -> None:
        f = self.root.FindName
        # HostBox 是可编辑 ComboBox(IsEditable):.Text 手输,下拉选设备记录
        self.host_box = f("HostBox").as_(ComboBox)
        self.connect_btn = f("ConnectBtn").as_(Button)
        self.forget_btn = f("ForgetBtn").as_(Button)
        self.busy_ring = f("BusyRing").as_(ProgressRing)
        self.status_text = f("StatusText").as_(TextBlock)
        self.notice_bar = f("NoticeBar").as_(InfoBar)
        self.nav_view = f("NavView").as_(NavigationView)
        self.page_host = f("PageHost").as_(Grid)
        self.nav_items = {
            "browse": f("NavBrowse").as_(NavigationViewItem),
            "records": f("NavRecords").as_(NavigationViewItem),
            "guiding": f("NavGuiding").as_(NavigationViewItem),
            "sky3d": f("NavSky3D").as_(NavigationViewItem),
            "fitsview": f("NavFitsView").as_(NavigationViewItem),
            "space": f("NavSpace").as_(NavigationViewItem),
            "devices": f("NavDevices").as_(NavigationViewItem),
            "scan": f("NavScan").as_(NavigationViewItem),
            "monitor": f("NavMonitor").as_(NavigationViewItem),
        }
        self.transfer_list = f("TransferList").as_(ListView)
        self.transfer_summary = f("TransferSummary").as_(TextBlock)
        self.workers_box = f("WorkersBox").as_(ComboBox)
        self.cancel_all_btn = f("CancelAllBtn").as_(Button)
        self.clear_done_btn = f("ClearDoneBtn").as_(Button)

    def _wire_events(self) -> None:
        self.connect_btn.Click += self._on_connect_click
        self.forget_btn.Click += self._on_forget_click
        self.host_box.KeyDown += self._on_host_key
        self.host_box.TextSubmitted += self._on_host_submitted
        self.host_box.SelectionChanged += self._on_host_selected
        self.host_box.Loaded += self._on_host_loaded
        self.nav_view.SelectionChanged += self._on_nav_changed
        self.workers_box.SelectionChanged += self._on_workers_changed
        self.cancel_all_btn.Click += lambda s, e: self.transfers.cancel_all()
        self.clear_done_btn.Click += self._on_clear_done

    # ---------- 基础设施(供页面调用) ----------

    def ui(self, fn, *args) -> None:
        self.dispatcher.TryEnqueue(lambda: fn(*args))

    def busy(self, on: bool) -> None:
        self.busy_ring.IsActive = on

    def status(self, text: str) -> None:
        self._status_base = text
        self._refresh_status()

    def _refresh_status(self) -> None:
        self.status_text.Text = self._status_base + self._hb_tail + self._watch_tail

    def error(self, text: str) -> None:
        self._notice(InfoBarSeverity.Error, text)

    def info(self, text: str, action_text: str = "",
             on_action=None) -> None:
        """信息条。给了 ``action_text``/``on_action`` 就在条上挂一个动作按钮
        (例如"打开所在文件夹")。"""
        self._notice(InfoBarSeverity.Informational, text, action_text, on_action)

    def _notice(self, severity, text: str, action_text: str = "",
                on_action=None) -> None:
        # 动作按钮必须每次重建/清掉 —— 否则上一条通知的按钮会留在下一条上,
        # 点下去干的是完全不相干的事。
        try:
            if action_text and on_action is not None:
                btn = Button()
                btn.Content = action_text
                btn.Click += lambda s, e: on_action()
                self.notice_bar.ActionButton = btn
            else:
                self.notice_bar.ActionButton = None
        except Exception:
            pass
        self.notice_bar.Severity = severity
        self.notice_bar.Message = text
        self.notice_bar.IsOpen = True

    def hwnd(self) -> int:
        return self.win.AppWindow.Id.Value

    async def confirm(self, title: str, message: str,
                      ok_text: str | None = None) -> bool:
        """在应用内弹确认对话框(用于删除等破坏性操作的二次确认)。

        **默认值不能写成 `ok_text=_("确定")`** —— 函数默认值是 `def` 执行时
        (import 时)求值的,和模块级常量一样会把翻译冻住。
        """
        ok_text = _("确定") if ok_text is None else ok_text
        dlg = ContentDialog()
        dlg.Title = title
        dlg.Content = message
        dlg.PrimaryButtonText = ok_text
        dlg.CloseButtonText = _("取消")
        dlg.DefaultButton = ContentDialogButton.Close
        dlg.XamlRoot = self.root.XamlRoot
        try:
            result = await dlg.ShowAsync()
        except Exception:
            # 兜底:某些情况下 ContentDialog 不可用,退回 tkinter
            return await asyncio.to_thread(_tk_yesno, title, message)
        return result == ContentDialogResult.Primary

    # ---------- 导航 ----------

    def _on_nav_changed(self, sender, args) -> None:
        item = args.SelectedItem
        if item is None:
            return
        try:
            tag = unbox_str(item.as_(NavigationViewItem).Tag)
        except Exception:
            tag = ""
        if tag in self._pages:
            self._show_page(tag)

    @staticmethod
    def _startup_host() -> str:
        """启动默认设备。

        优先 `devices.last_host()`(真正连接成功过的那台)。**都没有时退回
        自动发现的本地卡** —— 自动发现是靠 ZWO 特征扫出来的,证据比"用户手输
        了一个地址"强得多,但它毕竟没"连接成功"过,所以 last_ok 是 0
        (否则打错的地址会写出假的"刚刚连过"并抢走默认设备)。
        这样既保住了"插上卡就自动连"的便利,又不往 devices.json 里写假数据。
        """
        hit = devices.last_host()
        if hit:
            return hit
        try:
            local = [r for r in devices.load()
                     if str(r.get("kind", "")).lower() == devices.KIND_LOCAL]
        except Exception:
            return ""
        if not local:
            return ""
        local.sort(key=lambda r: float(r.get("first_seen") or 0.0), reverse=True)
        return str(local[0].get("host") or "")

    def _show_page(self, tag: str) -> None:
        page = self._pages[tag]
        old = self._current_page
        if old is not None and old is not page:
            # 切走时通知旧页(设备页据此停掉可见期的定时重采;别的页可以不实现)
            hide = getattr(old, "on_hide", None)
            if callable(hide):
                try:
                    hide()
                except Exception as ex:
                    print(_("页面 on_hide 失败: {ex}").format(ex=ex), flush=True)
        self.page_host.Children.Clear()
        self.page_host.Children.Append(page.root)
        self._current_page = page
        page.on_show()

    def select_page(self, tag: str) -> None:
        """程序化切换页面(会同步高亮左侧导航项)。"""
        item = self.nav_items.get(tag)
        if item is not None and self.nav_view.SelectedItem is not item:
            self.nav_view.SelectedItem = item  # 触发 SelectionChanged→_show_page
        else:
            self._show_page(tag)

    # ---------- 页面间跳转 API(供拍摄记录页等调用) ----------

    def open_guiding(self, t0, t1, label: str) -> None:
        """跳到导星分析页并定位到时间区间 [t0, t1](datetime)。"""
        self.select_page("guiding")
        try:
            self.guiding.show_range(t0, t1, label)
        except Exception as ex:
            self.error(_("导星定位失败: {ex}").format(ex=ex))

    @staticmethod
    def _guide_quality_key(run) -> tuple:
        end = getattr(run, "end_time", None) or getattr(run, "begin_time", None)
        return (getattr(run, "target", ""),
                getattr(run, "begin_time", None), end)

    def set_guide_quality(self, run, quality) -> None:
        """保存某次拍摄的三证据诊断，并刷新当前可见的记录详情。"""
        key = self._guide_quality_key(run)
        self._guide_quality[key] = quality
        self._guide_quality_state[key] = {"busy": False, "text": _("分析完成"),
                                          "error": False}
        if getattr(self, "_current_page", None) is self.records:
            self.records.on_guide_quality_updated(run)

    def guide_quality_for(self, run):
        return self._guide_quality.get(self._guide_quality_key(run))

    def guide_quality_state_for(self, run) -> dict:
        return dict(self._guide_quality_state.get(
            self._guide_quality_key(run), {}))

    def set_guide_quality_state(self, run, busy: bool, text: str,
                                error: bool = False) -> None:
        self._guide_quality_state[self._guide_quality_key(run)] = {
            "busy": bool(busy), "text": str(text), "error": bool(error)}
        if getattr(self, "_current_page", None) is self.records:
            self.records.on_guide_quality_updated(run)

    def request_guide_quality(self, run) -> bool:
        """由拍摄记录页直接启动质量倒推，不要求先打开 3D 天球。"""
        return bool(self.sky3d.request_guide_quality(run))

    def cancel_guide_quality(self, run) -> bool:
        return bool(self.sky3d.cancel_guide_quality(run))

    def open_browser_path(self, share: str, path: str) -> None:
        """跳到浏览页并导航到远程路径。"""
        self.select_page("browse")
        asyncui.create_task(self.browser._navigate(share, path))

    def dev_rtt(self) -> dict:
        """已记录设备的存活探测结果 {host: RTT 毫秒 | None(不可达)}。

        本地设备是 0.0=卡还在 / None=拔了。每 ~20s 一轮(_dev_probe_loop)。
        设备页据此让徽章跟着外壳的探测走,不必自己再探一遍。
        """
        return dict(self._dev_rtt)

    @property
    def data_share(self) -> str:
        """存放 log/ 与 Plan\\Light/ 的那个共享。

        SMB 设备上是 "EMMC Images",**本地磁盘后端是卷标**(LocalBackend 的
        单共享模型)。各页面以前各自硬编码 "EMMC Images",在本地卡上必然取不到
        任何东西 —— 而且是**静默**退化(listdir 抛错被吞成空集合),表现为
        "3D 天球/详情里的实测坐标莫名其妙全没了"(审查实证)。
        """
        return self._log_share or LOG_SHARE_DEFAULT

    def open_fitsview(self, share: str, path: str) -> None:
        """跳到 FITS 查看器并加载该文件(浏览页双击/右键/按钮调用)。"""
        self.select_page("fitsview")
        try:
            self.fitsview.open_path(share, path)
        except Exception as ex:
            self.error(_("打开 FITS 查看器失败: {ex}").format(ex=ex))

    # ---------- 本地 ZWO 卡自动发现 ----------

    def _discover_local_devices(self) -> None:
        """扫一遍本机卷,把有 ZWO 特征的自动加进设备记录(工作线程)。

        判据在 volumes.autodetect_zwo:命中 ≥3 个特征目录且杂项 ≤4 —— 宁可
        漏收也不能把用户的大硬盘误当 ASIAIR 卡。
        """
        try:
            vols = volumes.list_volumes()
            roots = volumes.autodetect_zwo(vols)
        except Exception:
            return
        added = []
        for root in roots:
            vol = next((v for v in vols if str(v.path) == str(root)), None)
            host = str(root)
            try:
                if any(r.get("host") == host for r in devices.load()):
                    continue        # 已记录过,不重复提示
                devices.remember(
                    host, name=(getattr(vol, "label", "") or _("ASIAIR 卡")),
                    os=volumes.describe_zwo(root) or _("本地磁盘"),
                    dialect=_("本地磁盘"), shares=1,
                    kind=devices.KIND_LOCAL, path=host,
                    # **自动发现不等于连接成功**:last_ok 是"最近一次连上"的
                    # 意思,写成当前时间就是假数据,还会让打错的地址抢走默认设备。
                    # 首启动仍会自动连它 —— 见 _startup_host 的本地卡兜底。
                    connected=False)
                added.append(host)
            except Exception:
                continue
        if added:
            self.ui(self._on_local_found, added)

    def _on_local_found(self, hosts: list) -> None:
        self._rebuild_device_items()
        self.info(_("发现本地 ASIAIR 存储卡: ") + _("、").join(hosts)
                  + _(" — 在顶部设备下拉里可直接选用"))

    # ---------- 后端工厂(SMB / 本地磁盘) ----------

    def _make_backend(self, host: str):
        """按设备记录决定建 SMB 还是本地磁盘后端。

        host 对本地设备就是根路径("E:\\\\"),它仍是设备记录的唯一键 ——
        下拉/忘记/探测全部照旧。记录里没有(手输的新地址)一律按 SMB。
        """
        rec = next((r for r in devices.load() if r.get("host") == host), None)
        kind = (rec or {}).get("kind", devices.KIND_SMB)
        if kind != devices.KIND_LOCAL:
            # 本地盘的路径形态(E:\ 或 /media/...)不可能是 SMB 主机名,
            # 兜底识别一下,免得记录丢了 kind 就去连一个盘符
            if not _looks_like_local_path(host):
                return AstroSmbClient(host=host)
            kind = devices.KIND_LOCAL
        return make_backend(kind, host=host,
                            path=devices.local_root(rec or {}) or host,
                            label=(rec or {}).get("name", ""))

    @staticmethod
    def _detect_log_share(backend, shares) -> str:
        """找出哪个共享底下有 log/ 目录(SMB 是 EMMC Images,本地盘是卷标)。"""
        for s in shares:
            try:
                if backend.exists(s.name, "log"):
                    return s.name
            except Exception:
                continue
        return shares[0].name if shares else LOG_SHARE_DEFAULT

    # ---------- 设备记录 / 顶部设备下拉 ----------

    def current_host(self) -> str:
        """顶部设备框里的地址(去空白)。页面统一走这里,别直接摸控件类型。

        选中下拉项时 ``ComboBox.Text`` 是**整条富文本**
        ("192.0.2.228  ·  ASIAIR · SMB 3.1.1  ·  ● 端口可达 5 ms"),
        直接拿去连会失败 —— 选中态一律按索引取回真地址,只有手输时才用文本
        (手输文本也按分隔符截首段兜底)。
        """
        try:
            idx = self.host_box.SelectedIndex
        except Exception:
            idx = -1
        hosts = getattr(self, "_dev_hosts", [])
        if idx is not None and 0 <= idx < len(hosts):
            return hosts[idx]
        text = (self.host_box.Text or "").strip()
        if "·" in text:                 # 用户可能把整条文本粘回去了
            text = text.split("·")[0].strip()
        return text

    def set_host(self, host: str) -> None:
        """程序化填入设备地址(扫描页「连接」、下拉选中都走这里)。

        顺带清掉下拉选中项:否则同一台设备第二次点选不会再发
        SelectionChanged(WinUI 只在选中项**变化**时发),就"点了没反应"。
        实测清选中不会清掉编辑框文本,所以先清选中再写文本。

        **代码设置的 .Text 不会显示在可编辑 ComboBox 的编辑框上**(探针实证,
        见 _rebuild_device_items 的注释),所以命中设备记录时用选中项来显示。
        """
        self._wanted_host = host or ""
        self._host_sync = True
        try:
            self._select_host_item(self._wanted_host)
        finally:
            self._host_sync = False

    def _on_host_loaded(self, sender, e) -> None:
        """控件模板就绪:补一次显示(启动阶段设的值可能还没渲染出来)。"""
        want = getattr(self, "_wanted_host", "")
        if not want:
            return
        self._host_sync = True
        try:
            self._select_host_item(want)
        except Exception:
            pass
        finally:
            self._host_sync = False

    def _rebuild_device_items(self) -> None:
        """按设备记录重建下拉项(启动/连接成功/忘记设备/探测结果变化时)。

        **项内容必须是纯字符串**:探针实测(scratchpad/probe_host2.py)——
        可编辑 ComboBox **不会把代码设置的 .Text 渲染到编辑框**(属性读回正常,
        视觉不同步),只有「选中一个纯文本项」才显示;富排版(StackPanel)内容
        选中后编辑框显示空白。所以状态/时延都拼进项的字符串里,
        并在 host 命中记录时用 SelectedIndex 显示。
        """
        keep = getattr(self, "_wanted_host", "")
        self._host_sync = True
        try:
            self.host_box.Items.Clear()
            self._dev_hosts = []
            for rec in devices.load():
                host = rec["host"]
                item = ComboBoxItem()
                item.Tag = host
                item.Content = self._dev_item_text(rec)
                self.host_box.Items.Append(item)
                self._dev_hosts.append(host)
            self._select_host_item(keep)
        finally:
            self._host_sync = False

    def _dev_item_text(self, rec: dict) -> str:
        """一条下拉项的显示文本:地址 · 服务器名/协议/共享数 · 存活与时延。"""
        parts = [rec["host"]]
        sub = devices.summary(rec)
        if sub:
            parts.append(sub)
        parts.append(self._dev_status(rec["host"])[0])
        return "  ·  ".join(parts)

    def _select_host_item(self, host: str) -> None:
        """让编辑框显示 host:命中记录就选中对应项(唯一可靠的显示方式),
        没命中(手输的新地址)则退回写 Text —— 用户自己输入的文本能正常显示。"""
        idx = -1
        for i, h in enumerate(getattr(self, "_dev_hosts", [])):
            if h == host:
                idx = i
                break
        if idx >= 0:
            self.host_box.SelectedIndex = idx
        else:
            self.host_box.SelectedIndex = -1
            self.host_box.Text = host or ""

    def _dev_status(self, host: str) -> tuple[str, tuple[int, int, int]]:
        """下拉项的存活徽章(文本, 颜色)。UI 线程调用:纯查表,不做任何 I/O。

        当前已连接的设备用心跳结果(真·SMB 会话存活);其余用后台 TCP 445
        探测结果 —— 中间盒/路由器会对整个网段的 445 SYN 秒回 ACK
        (docs/DEVELOPMENT.md §2),所以措辞只能是"端口可达",不能说"在线"。
        """
        # 本地设备(ASIAIR 卡直插电脑)没有"网络"可言:它只有插着和拔了两种状态。
        # 说"在线 0 ms / 端口可达"既没信息量又误导 —— 用户看到"离线"会去查网络,
        # 而实际上是卡被拔了。
        if _looks_like_local_path(host):
            if host in self._dev_rtt:
                return (_("● 已插入"), _GREEN) if self._dev_rtt[host] is not None \
                    else (_("● 已拔出"), _GRAY)
            return _("○ 未检测"), _GRAY
        if host and host == self._hb_host and self.hb.get("host") == host:
            if self.hb.get("alive"):
                rtt = self.hb.get("rtt_ms")
                return (_("● 在线 {rtt:.0f} ms").format(
                    rtt=rtt) if rtt is not None else _("● 在线")), _GREEN
            return _("● 离线"), _GRAY
        if host not in self._dev_rtt:
            return _("○ 未探测"), _GRAY
        rtt = self._dev_rtt[host]
        if rtt is None:
            return _("● 离线"), _GRAY
        return _("● 端口可达 {rtt:.0f} ms").format(rtt=rtt), _GREEN

    def _refresh_dev_row(self, host: str | None) -> None:
        """探测结果变化:原地改对应项的文本(项是纯字符串,不能只改子控件)。"""
        hosts = getattr(self, "_dev_hosts", [])
        if not host or host not in hosts:
            return
        idx = hosts.index(host)
        rec = next((r for r in devices.load() if r["host"] == host), None)
        if rec is None:
            return
        try:
            item = self.host_box.Items.GetAt(idx).as_(ComboBoxItem)
            keep_sel = self.host_box.SelectedIndex == idx
            self._host_sync = True
            try:
                item.Content = self._dev_item_text(rec)
                if keep_sel:        # 改内容会掉选中态,补回去否则编辑框变空
                    self.host_box.SelectedIndex = -1
                    self.host_box.SelectedIndex = idx
            finally:
                self._host_sync = False
        except Exception:
            pass

    def _on_host_selected(self, sender, args) -> None:
        """下拉里选中一台设备 = 填入地址并立即连接(等同点「连接」)。"""
        if self._host_sync:
            return          # 程序化重建/清选中造成的重入,忽略
        item = self.host_box.SelectedItem
        if item is None:
            return
        try:
            host = unbox_str(item.as_(ComboBoxItem).Tag)
        except Exception:
            host = ""
        if not host:
            return
        self.set_host(host)
        asyncui.create_task(self._connect())

    def _on_host_submitted(self, sender, args) -> None:
        """在可编辑框里回车 = 连接手输的地址。"""
        try:
            # 标记已处理,免得 ComboBox 再拿文本去匹配下拉项(匹配不上会清选中)
            args.as_(ComboBoxTextSubmittedEventArgs).Handled = True
        except Exception:
            pass
        asyncui.create_task(self._connect())

    def _on_forget_click(self, sender, e) -> None:
        """把当前地址从设备记录里移除(DHCP 换走的旧 IP 清理用)。

        只对**记录里真有**的地址生效:手输一个不存在的地址点忘记不该提示
        "已移除";忘记当前正连着的设备也没意义(下次成功连接会原样加回来)。
        """
        host = self.current_host()
        if not host:
            self.error(_("请先填入或选中一个设备地址"))
            return
        if host not in getattr(self, "_dev_hosts", []):
            self.info(_("{host} 不在设备记录中").format(host=host))
            return
        if host == self._hb_host:
            self.info(_("{host} 正在连接中,断开或换设备后再忘记").format(host=host))
            return
        devices.forget(host)
        self._dev_rtt.pop(host, None)
        self._wanted_host = ""
        self._rebuild_device_items()
        self.info(_("已从设备记录中移除 {host}").format(host=host))

    # ---------- 设备存活探测(后台线程) ----------

    def _start_dev_probe(self) -> None:
        if getattr(self, "_dev_thread", None) and self._dev_thread.is_alive():
            return
        self._dev_thread = threading.Thread(
            target=self._dev_probe_loop, daemon=True, name="dev-probe")
        self._dev_thread.start()

    def _dev_probe_loop(self) -> None:
        """每 ~20s 对记住的设备测一次 TCP 445 时延,喂给顶部下拉的存活徽章。

        只做 TCP 连通性(便宜、不建 SMB 会话):**能连上只代表端口可达**,
        不代表那是台 SMB 设备(见 docs/DEVELOPMENT.md §2 的网段秒回 ACK 坑)——徽章文案
        照此措辞。当前已连接的设备跳过,直接用心跳的 RTT,不多开一条连接。
        """
        while not self._dev_stop.is_set():
            try:
                results: dict[str, float | None] = {}
                for rec in devices.load():      # 已按 MAX_RECORDS(12)截断
                    if self._dev_stop.is_set():
                        break
                    host = rec["host"]
                    if host == self._hb_host:
                        continue
                    if devices.is_local(rec):
                        # 本地盘不测网络:卡还在就是 0ms,拔了就是不可达
                        root = devices.local_root(rec) or host
                        results[host] = 0.0 if os.path.isdir(root) else None
                        continue
                    results[host] = AstroSmbClient(
                        host=host, timeout=2).ping_tcp(timeout=2.0)
                if results:
                    self.ui(self._apply_dev_probe, results)
            except Exception as ex:     # 防御:不静默、也不许杀掉探测线程
                self.ui(self.error, _("设备探测失败: {ex}").format(ex=ex))
            if self._dev_wake.wait(DEV_PROBE_SECONDS):
                self._dev_wake.clear()      # 被 poke(连接成功)提前唤醒

    def _apply_dev_probe(self, results: dict) -> None:
        """探测结果回 UI:只改已有项的文本/画刷,不重建下拉。"""
        self._dev_rtt.update(results)
        for host in results:
            self._refresh_dev_row(host)
        # 设备页的徽章跟着这一轮探测走(它自己只有 20s 的可见期重采,
        # 这条让插拔立刻反映出来)
        try:
            self.devices_page.refresh_records()
        except Exception as ex:
            print(_("设备页刷新失败: {ex}").format(ex=ex), flush=True)

    # ---------- 连接 ----------

    def _on_host_key(self, sender, e) -> None:
        from win32more.Windows.System import VirtualKey
        try:
            if e.Key == VirtualKey.Enter:
                asyncui.create_task(self._connect())
        except Exception:
            pass

    async def _on_connect_click(self, sender, e) -> None:
        await self._connect()

    async def _connect(self) -> None:
        """连接顶部设备框里的地址(回车/按钮/下拉选中三处入口共用,防重入)。

        重入不再**静默丢弃**:在途连接期间点选另一台设备时记下它,
        当前这轮结束后自动接着连 —— 否则地址框显示 B、实际连着 A(审查实证)。
        """
        host = self.current_host()
        if not host:
            self.error(_("请输入设备地址,或到「扫描设备」页找一台"))
            return
        if self._connecting:
            self._pending_host = host
            self.status(_("正在连接中,稍后切换到 {host} …").format(host=host))
            return      # 回车会同时触发 TextSubmitted 与 KeyDown,别连两次
        self._connecting = True
        try:
            await self._connect_to(host)
            # 期间用户又选了别的设备:接着连,直到没有新的待连地址
            while self._pending_host and self._pending_host != host:
                host = self._pending_host
                self._pending_host = None
                await self._connect_to(host)
        finally:
            self._connecting = False
            self._pending_host = None

    async def _connect_to(self, host: str) -> None:
        """**先验证新连接、成功后才替换旧的**。

        旧写法是先 close 旧连接再连新地址:连不上时旧会话已经被销毁,
        而 _shares/各页列表/心跳/watcher 仍指向旧设备 —— 状态栏会出现
        "连接失败 ● 12ms" 这种自相矛盾的显示,浏览页后续操作全打到坏地址
        (审查实证)。现在失败时旧连接原样可用。
        """
        self.busy(True)
        self.status(_("正在连接 {host} …").format(host=host))
        new = self._make_backend(host)
        try:
            await asyncio.to_thread(new.connect)
            shares = await asyncio.to_thread(new.list_shares)
            info = await asyncio.to_thread(new.server_info)
        except SmbClientError as ex:
            await asyncio.to_thread(new.close)      # 不留悬挂 socket
            keep = self._hb_host                    # 仍然有效的旧设备
            self.status(_("连接 {host} 失败").format(host=host) + (_(",仍连接 {keep}").format(
                keep=keep) if keep else ""))
            self.error(str(ex))
            if keep:                                # 地址框回滚到真正连着的那台
                self.set_host(keep)
            self.busy(False)
            return
        old = self.client
        self.client = new
        await asyncio.to_thread(old.close)
        self.preview.reset()
        self._shares = shares
        # 日志所在共享:SMB 是 "EMMC Images",本地盘是卷标 —— 探一次谁下面有 log/
        self._log_share = await asyncio.to_thread(self._detect_log_share, new, shares)
        # **全程序数据源在这里、且只在这里换设备**。bind() 里 host 变了就把
        # LogData 与两个按文件名做键的内存缓存全清掉;之后 store.data 对新设备
        # 返回 None,拍摄记录/导星/3D 天球三页的 `data is None` 分支自然会去
        # 重新拉取 —— 换设备后它们仍显示上一台设备日志的问题(真机确认)就此消失。
        switched = self.logstore.bind(host, self._log_share)
        self.watcher.share = self._log_share
        if switched:
            self._guide_quality.clear()
            self._guide_quality_state.clear()
            # 各页面已渲染的内容属于上一台设备,标记作废让它们重新走首屏
            for page in self._pages.values():
                inval = getattr(page, "on_source_changed", None)
                if callable(inval):
                    try:
                        inval()
                    except Exception as ex:
                        print(_("页面数据源切换回调失败: {ex}").format(ex=ex), flush=True)
        self.status(
            _("已连接 {0} ({1}) — {2} 个共享").format(
                info.get('server_name', host), info['dialect'], len(shares)))
        self.notice_bar.IsOpen = False

        # 记住这台设备:下次启动默认连它,顶部下拉也会列出
        # (remember 内部已吞掉 IO 异常;这里再兜一层,记录写不进去绝不影响连接)
        try:
            local = is_local(new)
            devices.remember(host, name=info.get("server_name"),
                             os=info.get("server_os"), dialect=info.get("dialect"),
                             shares=len(shares),
                             kind=devices.KIND_LOCAL if local else devices.KIND_SMB,
                             path=(host if local else ""))
            self._rebuild_device_items()
        except Exception as ex:
            print(_("设备记录更新失败: {ex}").format(ex=ex), flush=True)
        self._dev_wake.set()        # 让探测线程立刻跑一轮,徽章别等 20s

        for page in self._pages.values():
            page.on_connected(shares)
        self.busy(False)

        # 启动/切换心跳到当前设备
        self._hb_host = host
        self._hb_ok = self._hb_fail = 0
        self._start_heartbeat()

        # 启动/切换运行状态 watcher
        self._watch_host = host
        self.watcher.start()
        self.watcher.poke()

        # 测试/便捷钩子:启动直达路径与页面
        start = os.environ.get("ASTRO_SMB_GUI_START_PATH")
        if start:
            from astro_smb.client import split_remote_path
            try:
                s, p = split_remote_path(start)
                asyncui.create_task(self.browser._navigate(s, p))
                asyncui.create_task(self.browser._load_volume(s))
            except SmbClientError:
                pass
        # 测试钩子:自动下载某目录前几个大文件(用于展示监控页分块图)
        autodl = os.environ.get("ASTRO_SMB_GUI_AUTODL")
        if autodl:
            from pathlib import Path as _P

            from astro_smb.client import split_remote_path
            try:
                s, p = split_remote_path(autodl)
                entries = await asyncio.to_thread(self.client.listdir, s, p)
                big = [e for e in entries if not e.is_dir and e.size >= (16 << 20)][:5]
                target = _P(os.environ.get("TEMP", ".")) / "asiair_dl_test"
                for e in big:
                    self.transfers.submit_download(
                        e.share, e.path, target / e.name, label=e.name, size=e.size)
            except SmbClientError:
                pass

        # 测试钩子:整个文件夹入队下载(验证按文件展开 + 监控页分组)
        autodldir = os.environ.get("ASTRO_SMB_GUI_AUTODLDIR")
        if autodldir:
            from pathlib import Path as _P

            from astro_smb.client import split_remote_path
            try:
                s, p = split_remote_path(autodldir)
                entry = await asyncio.to_thread(self.client.stat, s, p)
                target = _P(os.environ.get("TEMP", ".")) / "asiair_dl_test_dir"
                self.browser._queue_download([entry], target)
            except SmbClientError:
                pass

        start_page = os.environ.get("ASTRO_SMB_GUI_START_PAGE")
        if start_page in self._pages:
            if start_page == "space" and start:
                from astro_smb.client import split_remote_path
                try:
                    s, p = split_remote_path(start)
                    self.space.load_path(s, p)
                except SmbClientError:
                    pass
            self.select_page(start_page)
            if start_page == "scan":
                self.scan._on_scan(None, None)

    # ---------- 心跳 / 连接状态 ----------

    def _start_heartbeat(self) -> None:
        if getattr(self, "_hb_thread", None) and self._hb_thread.is_alive():
            return
        self._hb_stop = threading.Event()
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="heartbeat")
        self._hb_thread.start()

    def _heartbeat_loop(self) -> None:
        """独立连接每 ~4s 发一次 SMB2 ECHO,测存活与 RTT;掉线自动重试。"""
        hb_client = None
        hb_host = None
        while not self._hb_stop.is_set():
            host = self._hb_host
            if not host:
                self._hb_stop.wait(2)
                continue
            if hb_client is None or hb_host != host:
                if hb_client is not None:
                    try:
                        hb_client.close()
                    except Exception:
                        pass
                # 本地盘也走同一套:LocalBackend.echo() 返回 0ms,
                # 卡被拔掉时 connect() 抛 SmbClientError → 正好显示"离线"
                hb_client = self._make_backend(host)
                hb_host = host
            state: dict = {"host": host}
            try:
                rtt = hb_client.echo()
                info = hb_client.server_info()
                self._hb_ok += 1
                self._hb_last_ok_str = time.strftime("%H:%M:%S")
                state.update(alive=True, rtt_ms=rtt,
                             server_name=info.get("server_name"),
                             server_os=info.get("server_os"),
                             dialect=info.get("dialect"),
                             shares=len(self._shares),
                             last_ok_str=self._hb_last_ok_str)
            except SmbClientError:
                self._hb_fail += 1
                tcp = hb_client.ping_tcp()
                state.update(alive=False, rtt_ms=None, tcp_ok=(tcp is not None),
                             last_ok_str=self._hb_last_ok_str)
                try:
                    hb_client.close()
                except Exception:
                    pass
                hb_client = None
                hb_host = None
            state["checks"] = self._hb_ok + self._hb_fail
            state["fails"] = self._hb_fail
            self.ui(self._apply_heartbeat, dict(state))
            self._hb_stop.wait(4)
        if hb_client is not None:
            try:
                hb_client.close()
            except Exception:
                pass

    def _apply_heartbeat(self, state: dict) -> None:
        self.hb.update(state)
        # 顶栏尾巴
        if self.hb.get("host"):
            if self.hb.get("alive"):
                rtt = self.hb.get("rtt_ms")
                self._hb_tail = f"    ● {rtt:.0f} ms" if rtt is not None else _("    ● 在线")
            else:
                tail = _("断线") if not self.hb.get("tcp_ok") else _("会话断(端口可达)")
                self._hb_tail = f"    ● {tail}"
            self._refresh_status()
        try:
            self.scan.on_heartbeat(self.hb)
        except Exception:
            pass
        try:
            self.devices_page.on_heartbeat(self.hb)
        except Exception:
            pass
        # 顶部下拉里当前设备那一行:用心跳结果(比 TCP 探测更准)
        try:
            self._refresh_dev_row(self.hb.get("host"))
        except Exception:
            pass

    # ---------- 运行状态 watcher ----------

    def _apply_watch(self, state: dict) -> None:
        """watcher 上报(已编组到 UI 线程):更新状态栏尾巴 + 新日志提示。"""
        self.watch_state = dict(state)      # 供页面读取(拍摄记录页实时横幅)
        try:
            self.records.on_watch(state)
        except Exception:
            pass
        if state.get("running"):
            target = state.get("target") or "?"
            seq = state.get("seq")
            exp = state.get("exposure_s")
            parts = [_("正在拍摄 {target}").format(target=target)]
            if seq:
                parts.append(_("第{seq}张").format(seq=seq))
            if exp and exp >= 1.0:
                parts.append(f"{exp:.0f}s")
            self._watch_tail = "    ◉ " + " · ".join(parts)
        elif state.get("age_s") is not None:
            age = state["age_s"]
            if age < 6 * 3600:
                self._watch_tail = _("    ○ 拍摄空闲({0:.0f}分前有帧)").format(age / 60)
            else:
                self._watch_tail = ""
        else:
            self._watch_tail = ""
        self._refresh_status()

        new_logs = state.get("new_logs") or []
        if new_logs:
            # 会话刚结束:失效日志缓存(带代次,在途旧 refresh 不会覆盖失效标记)
            self.logstore.invalidate()
            names = _("、").join(new_logs[:3])
            self.info(_("拍摄会话结束,新日志已生成({names}) — 拍摄记录页可刷新查看").format(names=names))
            try:
                self.records.on_new_logs(new_logs)
            except Exception:
                pass

    # ---------- 传输队列 ----------

    def _on_workers_changed(self, sender, e) -> None:
        try:
            n = int(unbox_str(self.workers_box.SelectedItem.as_(ComboBoxItem).Content))
        except Exception:
            mapping = {0: 1, 1: 2, 2: 3, 3: 4, 4: 6}
            n = mapping.get(self.workers_box.SelectedIndex, 3)
        self.transfers.set_workers(n)

    def _on_transfer_update(self, job: TransferJob) -> None:
        # 底部常驻精简条 + 传输监控页 都要更新;
        # 有 group 的任务在底部条按组聚合为一行,监控页仍逐文件(分组折叠)显示
        if job.group:
            self._update_group_transfer_row(job)
        else:
            self._update_transfer_row(job)
        try:
            self.monitor.update_job(job)
        except Exception:
            pass

    def _update_transfer_row(self, job: TransferJob) -> None:
        row = self._transfer_rows.get(job.job_id)
        if row is None:
            row = self._make_transfer_row(job)
            self._transfer_rows[job.job_id] = row
            self.transfer_list.Items.Append(row["root"])
        arrow = "↓" if job.kind.startswith("download") else "↑"
        row["label"].Text = f"{arrow} {job.label}" + (f" — {job.detail}" if job.detail else "")
        if job.total > 0:
            row["bar"].IsIndeterminate = False
            row["bar"].Value = job.progress_fraction() * 100
        else:
            row["bar"].IsIndeterminate = not job.finished
            if job.finished:
                # **比常量不比字面量** —— 状态串住在 astro_smb_app.transfers,
                # 写死中文的话常量一改(或一翻译)进度条就永远停在 0
                row["bar"].Value = 100 if job.status == DONE_S else 0
        parts = [_(job.status)]   # 显示才翻
        if job.attempt and not job.finished:
            parts.append(_("重试{attempt}").format(attempt=job.attempt))
        if job.total > 0 and not job.finished:
            parts.append(f"{human_size(job.done)}/{human_size(job.total)}")
        elif job.done:
            parts.append(human_size(job.done))
        if job.speed > 0 and not job.finished:
            parts.append(f"{human_size(job.speed)}/s")
        if job.error:
            parts.append(job.error)
        row["status"].Text = "  ".join(parts)
        row["cancel"].IsEnabled = not job.finished
        self._update_transfer_summary()

    def _update_transfer_summary(self) -> None:
        active = self.transfers.active_count()
        self.transfer_summary.Text = _("{active} 个进行中").format(
            active=active) if active else ""

    def _update_group_transfer_row(self, job: TransferJob) -> None:
        """底部精简条:同一文件夹组的所有文件任务聚合为一行。
        聚合计算在 UI 线程线性扫 transfers.jobs(节流 0.1s;任务收尾不节流)。"""
        group = job.group or ""
        row = self._group_transfer_rows.get(group)
        if row is None:
            row = self._make_group_transfer_row(group)
            self._group_transfer_rows[group] = row
            self.transfer_list.Items.Append(row["root"])
        now = time.monotonic()
        if not job.finished and now - row["_last"] < 0.1:
            return
        row["_last"] = now
        self._refresh_group_transfer_row(group)
        self._update_transfer_summary()

    def _refresh_group_transfer_row(self, group: str) -> None:
        row = self._group_transfer_rows.get(group)
        if row is None:
            return
        jobs = [j for j in self.transfers.jobs if j.group == group]
        if not jobs:
            return
        total = len(jobs)
        # 「完成」口径:成功 + 跳过(目标已存在,本地已有该文件)
        completed = sum(1 for j in jobs if j.status in (DONE_S, SKIPPED))
        unfinished = [j for j in jobs if not j.finished]
        row["label"].Text = _("↓ {group} ({completed}/{total} 文件)").format(
            group=group, completed=completed, total=total)
        row["bar"].IsIndeterminate = False
        row["bar"].Value = completed / total * 100 if total else 0
        if unfinished:
            parts = []
            speed = sum(j.speed for j in jobs if j.running)
            if speed > 0:
                parts.append(_("合计 {0}/s").format(human_size(speed)))
            cur = next((j for j in jobs if j.running), None)
            if cur is not None:
                parts.append(cur.label)
            else:
                parts.append(_("排队 {0}").format(len(unfinished)))
            row["status"].Text = "  ".join(parts)
        else:
            ok = sum(1 for j in jobs if j.status == DONE_S)
            err = sum(1 for j in jobs if j.status == ERROR)
            cn = sum(1 for j in jobs if j.status == CANCELLED)
            sk = sum(1 for j in jobs if j.status == SKIPPED)
            parts = [_("完成 {ok}").format(ok=ok)]
            if err:
                parts.append(_("失败 {err}").format(err=err))
            if cn:
                parts.append(_("取消 {cn}").format(cn=cn))
            if sk:
                parts.append(_("跳过 {sk}").format(sk=sk))
            row["status"].Text = " · ".join(parts)
        row["cancel"].IsEnabled = bool(unfinished)

    def _make_transfer_row(self, job: TransferJob) -> dict:
        g = Grid()
        for width, unit in ((1, GridUnitType.Star), (240, GridUnitType.Pixel),
                            (300, GridUnitType.Pixel), (60, GridUnitType.Pixel)):
            c = ColumnDefinition()
            c.Width = GridLength(Value=float(width), GridUnitType=unit)
            g.ColumnDefinitions.Append(c)

        label = TextBlock()
        label.FontSize = 12
        g.Children.Append(label)
        Grid.SetColumn(label, 0)

        bar = ProgressBar()
        bar.Margin = Thickness(Left=8, Top=0, Right=8, Bottom=0)
        g.Children.Append(bar)
        Grid.SetColumn(bar, 1)

        status = TextBlock()
        status.FontSize = 12
        status.Opacity = 0.75
        g.Children.Append(status)
        Grid.SetColumn(status, 2)

        cancel = Button()
        cancel.Content = _("取消")
        cancel.FontSize = 11
        job_id = job.job_id
        cancel.Click += lambda s, ev: self.transfers.cancel_job(job_id)
        g.Children.Append(cancel)
        Grid.SetColumn(cancel, 3)
        return {"root": g, "label": label, "bar": bar, "status": status, "cancel": cancel}

    def _make_group_transfer_row(self, group: str) -> dict:
        """底部精简条的组聚合行:布局同单文件行,取消按钮取消该组全部未完成任务。"""
        g = Grid()
        for width, unit in ((1, GridUnitType.Star), (240, GridUnitType.Pixel),
                            (300, GridUnitType.Pixel), (60, GridUnitType.Pixel)):
            c = ColumnDefinition()
            c.Width = GridLength(Value=float(width), GridUnitType=unit)
            g.ColumnDefinitions.Append(c)

        label = TextBlock()
        label.FontSize = 12
        g.Children.Append(label)
        Grid.SetColumn(label, 0)

        bar = ProgressBar()
        bar.Margin = Thickness(Left=8, Top=0, Right=8, Bottom=0)
        bar.Minimum, bar.Maximum = 0, 100
        g.Children.Append(bar)
        Grid.SetColumn(bar, 1)

        status = TextBlock()
        status.FontSize = 12
        status.Opacity = 0.75
        g.Children.Append(status)
        Grid.SetColumn(status, 2)

        cancel = Button()
        cancel.Content = _("取消")
        cancel.FontSize = 11
        cancel.Click += lambda s, ev, grp=group: self.transfers.cancel_group(grp)
        g.Children.Append(cancel)
        Grid.SetColumn(cancel, 3)
        return {"root": g, "label": label, "bar": bar, "status": status,
                "cancel": cancel, "_last": 0.0}

    def _prune_transfer_rows(self) -> None:
        """按当前 transfers.jobs 重建底部精简条(供两个「清除已完成」按钮共用,
        避免监控页清除后底部条残留 + 行 dict 无限增长)。组行同步:组内任务
        全被清除后组行消失,仍有存活任务的组行保留并刷新聚合文本。"""
        self.transfer_list.Items.Clear()
        alive_ids = {j.job_id for j in self.transfers.jobs}
        alive_groups = {j.group for j in self.transfers.jobs if j.group}
        self._transfer_rows = {
            k: v for k, v in self._transfer_rows.items() if k in alive_ids}
        self._group_transfer_rows = {
            k: v for k, v in self._group_transfer_rows.items() if k in alive_groups}
        seen_groups: set[str] = set()
        for job in self.transfers.jobs:
            if job.group:
                if job.group in seen_groups:
                    continue
                seen_groups.add(job.group)
                row = self._group_transfer_rows.get(job.group)
            else:
                row = self._transfer_rows.get(job.job_id)
            if row is not None:
                self.transfer_list.Items.Append(row["root"])
        for grp in seen_groups:
            self._refresh_group_transfer_row(grp)

    def _on_clear_done(self, sender, e) -> None:
        """底部常驻条的「清除已完成」。

        **必须同时通知监控页**:它自己有一套行/组头回收池(win32more 的事件注册
        撤不掉,控件只能复用不能丢),而这条路径不经过它的按钮处理器。不通知的话,
        那些行会一直挂在 `MonitorPage._rows` 里既不显示也不回收。
        注意行是 `_on_transfer_update` 对**每个** job 无条件建的 —— 用户从没打开过
        「传输」页照样会建,所以这条通知与当前在看哪一页无关。
        """
        self.transfers.clear_finished()
        self._prune_transfer_rows()
        try:
            self.monitor.on_jobs_pruned()
        except Exception as ex:
            print(_("监控页回收失败: {ex}").format(ex=ex), flush=True)

    # ---------- 预览分发(转给浏览页) ----------

    def _deliver_preview(self, result) -> None:
        self.browser.apply_preview(result)

    # ---------- 关闭 ----------

    def _on_closed(self, sender, e) -> None:
        try:
            if getattr(self, "_hb_stop", None):
                self._hb_stop.set()
            if getattr(self, "_dev_stop", None):
                self._dev_stop.set()
                self._dev_wake.set()        # 立刻从 20s 等待里醒来退出
            self.watcher.stop()
            self.browser.on_close()
            self.space.on_close()
            self.sky3d.on_close()
            self.fitsview.on_close()
            self.devices_page.on_close()
            threading.Thread(target=metacache.close, daemon=True).start()
            self.transfers.shutdown()
            self.preview.shutdown()
            threading.Thread(target=self.client.close, daemon=True).start()
        except Exception:
            pass


def _tk_yesno(title: str, message: str) -> bool:
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        return bool(messagebox.askyesno(title, message, parent=root))
    finally:
        root.destroy()
