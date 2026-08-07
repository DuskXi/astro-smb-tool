"""浏览页:共享/目录浏览、选择、预览、上传下载、拖拽、搜索、删除。"""

from __future__ import annotations

import asyncio
import math
import ntpath
import os
import shutil
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from win32more import asyncui
from win32more._collections import Vector
from win32more.Microsoft.UI.Text import FontWeights
from win32more.Microsoft.UI.Xaml import (
    CornerRadius,
    FrameworkElement,
    GridLength,
    GridUnitType,
    HorizontalAlignment,
    TextTrimming,
    TextWrapping,
    Thickness,
    VerticalAlignment,
    Visibility,
)
from win32more.Microsoft.UI.Xaml.Controls import (
    Border,
    Button,
    Canvas,
    ColumnDefinition,
    ComboBox,
    FontIcon,
    Grid,
    Image,
    ListView,
    MenuFlyout,
    MenuFlyoutItem,
    Orientation,
    ProgressBar,
    ProgressRing,
    RowDefinition,
    StackPanel,
    TextBlock,
    TextBox,
    ToggleSwitch,
    ToolTipService,
)
from win32more.Microsoft.UI.Xaml.Controls.Primitives import ToggleButton
from win32more.Microsoft.UI.Xaml.Media import FontFamily, SolidColorBrush
from win32more.Microsoft.UI.Xaml.Media.Imaging import BitmapImage
from win32more.Microsoft.UI.Xaml.Shapes import Line, Rectangle
from win32more.Windows.UI import Color
from win32more.Windows.ApplicationModel.DataTransfer import (
    Clipboard,
    DataPackage,
    DataPackageOperation,
    StandardDataFormats,
)
from win32more.Windows.Storage import IStorageItem, StorageFile, StorageFolder
from win32more.Windows.System import VirtualKey

from astro_smb import astro
from astro_smb.autorunlog import night_key
from astro_smb.client import RemoteEntry, SmbClientError, normalize_remote_path
from astro_smb.fitshdr import FitsHeader
from astro_smb.naming import parse_image_name
from astro_smb.util import format_mtime, human_size, sanitize_local_name
from astro_smb.i18n import gettext as _
from astro_smb_gui._common import (
    FITS_EXTS,
    ext_category,
    file_uri,
    glyph_for,
    sorted_entries,
    unique_local,
    _spin,
)
from astro_smb_gui import dircache
from astro_smb_gui.logstore import load_site
from astro_smb_gui.preview import PreviewResult, cache_dir, read_fits_header
from astro_smb_gui.skyview import MiniRadar

XAML_PATH = Path(__file__).with_name("browser.xaml")

# 视图模型住在共享包 —— 新旧两套前端消费同一份判读。
# 这一层值钱在**判读**而不在取值:气量用的是 Pickering (2002) 而不是课本的
# 1/sin(h),高度角/气量/采样的语义色阈值都是有前提的经验值。写两份迟早在某次
# "顺手调阈值"时分叉,而分叉后两边给出不同判读、谁都不知道哪个对。(B11 逃生口)
from astro_smb_app.views.browser import (          # noqa: E402
    CHILD_COUNT_TTL,
    RENDER_BATCH,
    RENDER_CAP,
    _airmass,
    _airmass_note,
    _airmass_text,
    _airmass_tone,
    _alt_hint,
    _alt_tone,
    _astro_details,
    _AZ_NAMES,
    _az_name,
    _detail_text,
    _fmt_exposure,
    _GRP_CAMERA,
    _GRP_FILE,
    _GRP_OPTICS,
    _GRP_PLACE,
    _GRP_TARGET,
    _GRP_TIME,
    _hdr_suffix,
    _KIND_CN,
    _night_of_name,
    _NIGHT_PALETTE,
    _sampling_verdict,
    _TONE_RGB,
)
from astro_smb_gui._xamli18n import load_text as _xaml_text


def _brush(r: int, g: int, b: int, a: int = 255) -> SolidColorBrush:
    c = Color()
    c.A, c.R, c.G, c.B = a, r, g, b
    return SolidColorBrush(c)


def _corner(r: float) -> CornerRadius:
    cr = CornerRadius()
    cr.TopLeft = cr.TopRight = cr.BottomLeft = cr.BottomRight = r
    return cr


class DragOutSession:
    """一次拖出:立即后台下载所选项到暂存目录,provider 延迟交付。"""

    def __init__(self, client_factory, entries: list[RemoteEntry], on_status, sig=None):
        self.entries = entries
        self.sig = sig  # 选择签名,用于判断暂存是否对应当前选择
        self.staging = cache_dir() / "dragout" / uuid.uuid4().hex[:12]
        self.staging.mkdir(parents=True, exist_ok=True)
        self.paths: list[tuple[Path, bool]] = []
        self.done = threading.Event()
        self.cancel = threading.Event()
        self.error: str | None = None
        self._on_status = on_status
        self._factory = client_factory
        threading.Thread(target=self._download_all, daemon=True, name="dragout").start()

    def _download_all(self) -> None:
        client = self._factory()
        used: set[str] = set()
        try:
            client.connect()
            total = len(self.entries)
            for i, e in enumerate(self.entries, 1):
                if self.cancel.is_set():
                    return
                self._on_status(_("拖出准备中 {i}/{total}: {name}").format(
                    i=i, total=total, name=e.name))
                if e.is_dir:
                    parent = self.staging / f"d{i}"
                    parent.mkdir(parents=True, exist_ok=True)
                    client.download_dir(e.share, e.path, parent, cancel=self.cancel)
                    self.paths.append((parent / sanitize_local_name(e.name), True))
                else:
                    local = unique_local(self.staging, e.name, used)
                    client.download_file(e.share, e.path, local, cancel=self.cancel)
                    self.paths.append((local, False))
            self._on_status(_("拖出就绪:{total} 项已暂存").format(total=total))
        except Exception as ex:
            self.error = str(ex)
            self._on_status(_("拖出准备失败: {ex}").format(ex=ex))
        finally:
            if self.cancel.is_set():
                # 取消(如关窗)后这批暂存没人认领,直接清掉;先清再置 done,
                # 免得等待方拿到已被删除的路径
                self.paths = []
                shutil.rmtree(self.staging, ignore_errors=True)
            self.done.set()
            try:
                client.close()
            except Exception:
                pass

    def provide(self, request) -> None:
        deferral = request.GetDeferral()
        try:
            if not self.done.wait(timeout=1800) or self.error or not self.paths:
                return
            items = []
            for p, is_dir in self.paths:
                if is_dir:
                    it = _spin(StorageFolder.GetFolderFromPathAsync(str(p)))
                else:
                    it = _spin(StorageFile.GetFileFromPathAsync(str(p)))
                items.append(it.as_(IStorageItem))
            request.SetData(Vector[IStorageItem](items))
        except Exception as ex:
            self.error = str(ex)
        finally:
            deferral.Complete()


class BrowserPage:
    def __init__(self, shell):
        self.shell = shell
        self.root = XamlReader_load(XAML_PATH)
        self.dispatcher = shell.dispatcher

        self.share: str | None = None
        self.path: str = ""
        self.entries: list[RemoteEntry] = []
        self._rendered: list[RemoteEntry] = []
        self._render_gen = 0
        # 导航代次:缓存命中后的后台对账线程靠它判断"用户是不是已经切走了"
        self._nav_gen = 0
        self._nav_busy = 0          # 在飞的 _navigate 数(转圈开关用计数,见 _navigate)
        self._nav_note = ""         # 列表状态栏的前缀提示(缓存/核对/搜索)
        self._status_body = ""      # 列表状态栏的统计正文
        self._preview_token = 0
        self._preview_entry: RemoteEntry | None = None
        self._fits_visible = False
        self._search_cancel: threading.Event | None = None
        self._searching = False
        self._search_hits: list[RemoteEntry] | None = None
        self._search_prefix = ""
        self._drag_session: DragOutSession | None = None
        self._expand_stop = threading.Event()   # 关窗时停止目录展开线程
        self._count_gen = 0
        self._count_cells: dict[str, TextBlock] = {}
        # FITS 元数据懒加载:path → (副行 TextBlock, 文件名解析出的基础文本)
        self._fits_cells: dict[str, tuple[TextBlock, str]] = {}
        # (share, path, size, mtime) → 头部摘要后缀(跨目录切换复用,免重复读头)
        self._hdr_cache: dict[tuple, str] = {}
        # 夜次 → 色号(每次 _render 按当前视图重算,_make_row 查表;见 _assign_night_colors)
        self._night_colors: dict[str, int] = {}
        self._detail_copy_text = ""     # 「复制全部信息」的当前文本
        self.download_dir = Path.home() / "Downloads" / "Astro SMB Tool"

        self._find_controls()
        self._wire_events()

        # 详情卡片复用资源:徽章画刷(浅色底/深色字, 两主题下均可读)、
        # 等宽字体、迷你天球雷达(构造一次复用, draw 前 clear 已内置)
        self._badge_brushes = {
            "light":   (_brush(0xDD, 0xEF, 0xDD), _brush(0x1B, 0x5E, 0x20)),  # 亮场:绿
            "bias":    (_brush(0xE9, 0xE9, 0xE9), _brush(0x45, 0x45, 0x45)),  # 偏置:灰
            "dark":    (_brush(0xD3, 0xD3, 0xDC), _brush(0x2A, 0x2A, 0x38)),  # 暗场:深灰
            "flat":    (_brush(0xD9, 0xE7, 0xF8), _brush(0x0D, 0x47, 0xA1)),  # 平场:蓝
            "preview": (_brush(0xE4, 0xDD, 0xF2), _brush(0x4A, 0x33, 0x82)),  # 预览:紫
            "filter":  (_brush(0xFB, 0xEA, 0xC5), _brush(0x7A, 0x52, 0x00)),  # 滤镜:琥珀
            "bin":     (_brush(0xDF, 0xE9, 0xEC), _brush(0x24, 0x50, 0x60)),  # Bin:青灰
            "seq":     (_brush(0xE6, 0xE6, 0xE6), _brush(0x50, 0x50, 0x50)),  # 序号:中性
        }
        # 夜次徽章画刷(列表徽章列与详情徽章共用,按色号索引;构造一次复用)
        self._night_brushes = [(_brush(*bg), _brush(*fg))
                               for bg, fg in _NIGHT_PALETTE]
        # 语义色画刷(只染数值文本)/ 组分隔线 / pill 底色
        self._tone_brushes = {k: _brush(*rgb) for k, rgb in _TONE_RGB.items()}
        self._divider = _brush(0x80, 0x80, 0x80, 0x3C)
        self._pill_bg = _brush(0x80, 0x80, 0x80, 0x28)
        self._track_bg = _brush(0x80, 0x80, 0x80, 0x38)     # 高度弧条底槽
        self._track_tick = _brush(0x80, 0x80, 0x80, 0x78)   # 高度弧条刻度
        self._mono_font = FontFamily("Consolas")
        self._radar = MiniRadar(self.detail_radar, 190.0)

    # ---------- 页面生命周期 ----------

    def on_show(self) -> None:
        pass

    def on_connected(self, shares) -> None:
        self.shares_list.Items.Clear()
        self._shares = shares
        for s in shares:
            tb = TextBlock()
            tb.Text = s.name
            if s.remark:
                ToolTipService.SetToolTip(tb, s.remark)
            self.shares_list.Items.Append(tb)
        self.share = None
        self.path = ""
        self.entries = []
        self._rendered = []
        self._nav_gen += 1          # 换设备:在途的对账线程一律作废
        self._nav_note = ""
        self._status_body = ""
        self._night_colors = {}     # 换设备/重连:夜次配色随视图一起清空
        self.file_list.Items.Clear()
        self.path_text.Text = _("(未打开共享)")
        self.vol_text.Text = _("选择共享查看容量")
        self.vol_bar.Value = 0

    def on_close(self) -> None:
        if self._search_cancel:
            self._search_cancel.set()
        if self._drag_session is not None:
            self._drag_session.cancel.set()
        self._expand_stop.set()

    # ---------- 控件 ----------

    def _find_controls(self) -> None:
        f = self.root.FindName
        self.shares_list = f("SharesList").as_(ListView)
        self.vol_bar = f("VolBar").as_(ProgressBar)
        self.vol_text = f("VolText").as_(TextBlock)
        self.up_btn = f("UpBtn").as_(Button)
        self.refresh_btn = f("RefreshBtn").as_(Button)
        self.path_text = f("PathText").as_(TextBlock)
        self.download_btn = f("DownloadBtn").as_(Button)
        self.upload_files_btn = f("UploadFilesBtn").as_(Button)
        self.upload_dir_btn = f("UploadDirBtn").as_(Button)
        self.new_dir_btn = f("NewDirBtn").as_(Button)
        self.analyze_btn = f("AnalyzeBtn").as_(Button)
        self.search_box = f("SearchBox").as_(TextBox)
        self.search_btn = f("SearchBtn").as_(Button)
        self.sort_box = f("SortBox").as_(ComboBox)
        self.checkbox_toggle = f("CheckboxToggle").as_(ToggleSwitch)
        self.file_list = f("FileList").as_(ListView)
        self.list_status = f("ListStatusText").as_(TextBlock)
        self.preview_title = f("PreviewTitle").as_(TextBlock)
        self.preview_image = f("PreviewImage").as_(Image)
        self.preview_ring = f("PreviewRing").as_(ProgressRing)
        self.thumb_source_text = f("ThumbSourceText").as_(TextBlock)
        self.load_full_btn = f("LoadFullBtn").as_(Button)
        self.astro_card = f("AstroCard").as_(Border)
        self.astro_badges = f("AstroBadges").as_(StackPanel)
        self.astro_pills = f("AstroPills").as_(StackPanel)
        self.copy_detail_btn = f("CopyDetailBtn").as_(Button)
        self.astro_target = f("AstroTargetText").as_(TextBlock)
        self.astro_sub = f("AstroSubText").as_(TextBlock)
        self.astro_grid = f("AstroGrid").as_(Grid)
        self.detail_radar = f("DetailRadar").as_(Canvas)
        self.file_card = f("FileCard").as_(Border)
        self.file_grid = f("FileGrid").as_(Grid)
        self.preview_text = f("PreviewTextBlock").as_(TextBlock)
        self.fits_toggle = f("FitsToggle").as_(ToggleButton)
        self.fits_text = f("FitsText").as_(TextBlock)

    def _wire_events(self) -> None:
        self.shares_list.SelectionChanged += self._on_share_selected
        self.up_btn.Click += self._on_up
        self.refresh_btn.Click += self._on_refresh
        self.file_list.SelectionChanged += self._on_file_selected
        self.file_list.DoubleTapped += self._on_file_double_tapped
        self.file_list.DragOver += self._on_drag_over
        self.file_list.Drop += self._on_drop
        self.file_list.DragItemsStarting += self._on_drag_items_starting
        self.file_list.DragItemsCompleted += self._on_drag_items_completed
        self.download_btn.Click += self._on_download_selected
        self.upload_files_btn.Click += self._on_upload_files
        self.upload_dir_btn.Click += self._on_upload_dir
        self.new_dir_btn.Click += self._on_new_dir
        self.analyze_btn.Click += self._on_analyze
        self.search_btn.Click += self._on_search_click
        self.search_box.KeyDown += self._on_search_key
        self.sort_box.SelectionChanged += self._on_sort_changed
        self.checkbox_toggle.Toggled += self._on_checkbox_toggled
        self.load_full_btn.Click += self._on_load_full
        self.fits_toggle.Click += self._on_fits_toggle
        self.copy_detail_btn.Click += self._on_copy_detail
        self._install_viewer_button()
        self._build_context_menu()

    def _install_viewer_button(self) -> None:
        """在「下载原图并生成拉伸预览」后面插一颗「在 FITS 查看器中打开」。

        browser.xaml 由主控维护,这里纯代码插入。两个坑:

        * **`XamlReader.Load` 刚建好的树里 `FrameworkElement.Parent` 是 None**
          —— 逻辑父要等元素进入活动树(Loaded)才挂上(真机实测)。所以先试一次,
          失败就挂 `root.Loaded` 等页面第一次显示时再插;
        * 定位插入点只能靠子元素的 `Name`:`UIElementCollection.IndexOf` 要 out
          参数不好用,而 ComPtr 包装之间不能按 `is` 比身份(同一个 COM 对象每次
          拿到的是不同包装)。
        """
        self.open_viewer_btn = None
        self._viewer_btn_pending = None
        try:
            btn = Button()
            btn.Content = _("在 FITS 查看器中打开")
            btn.Visibility = Visibility.Collapsed
            btn.Click += self._on_open_viewer
            self._viewer_btn_pending = btn
        except Exception:
            return                          # 建不出来也不能挡住浏览页
        if not self._try_insert_viewer_button():
            try:
                self.root.Loaded += self._on_root_loaded
            except Exception:
                pass

    def _on_root_loaded(self, sender, e) -> None:
        self._try_insert_viewer_button()

    def _try_insert_viewer_button(self) -> bool:
        """插入成功(或已无待插按钮)返回 True。可重入,Loaded 多次触发也只插一次。"""
        btn = self._viewer_btn_pending
        if btn is None:
            return True
        try:
            panel = self.load_full_btn.Parent.as_(StackPanel)
            kids = panel.Children
            idx = kids.Size
            for i in range(kids.Size):
                try:
                    if kids.GetAt(i).as_(FrameworkElement).Name == "LoadFullBtn":
                        idx = i + 1
                        break
                except Exception:
                    continue
            kids.InsertAt(idx, btn)
        except Exception:
            return False
        self.open_viewer_btn = btn
        self._viewer_btn_pending = None
        return True

    def _build_context_menu(self) -> None:
        menu = MenuFlyout()

        def add(label, handler):
            item = MenuFlyoutItem()
            item.Text = label
            item.Click += handler
            menu.Items.Append(item)
            return item

        self._menu_viewer_item = add(_("在 FITS 查看器中打开"), self._on_open_viewer_menu)
        add(_("下载"), self._on_download_selected)
        add(_("下载到…"), self._on_download_to)
        add(_("复制到剪贴板(可粘贴到资源管理器)"), self._on_copy_files)
        add(_("重命名…"), self._on_rename)
        add(_("删除"), self._on_delete)
        add(_("复制 UNC 路径"), self._on_copy_path)
        # 「在 FITS 查看器中打开」是**第一项**(排在「下载」之前),右键一个
        # _thn.jpg / .txt 极易误点 —— 弹出前按选中项决定显不显示,
        # 免得切页清掉用户正在看的图、几百毫秒后再弹「FITS 头不完整」
        try:
            menu.Opening += self._on_context_opening
        except Exception:
            pass                # 挂不上事件也不能让浏览页起不来(_on_... 里还有一道过滤)
        self.file_list.ContextFlyout = menu

    def _on_context_opening(self, sender, e) -> None:
        item = getattr(self, "_menu_viewer_item", None)
        if item is None:
            return
        try:
            ok = any(self._is_fits(x) for x in self._selected_entries())
            item.Visibility = Visibility.Visible if ok else Visibility.Collapsed
            item.IsEnabled = ok
        except Exception:
            pass

    # ---------- 选择模式(#9) ----------

    def _on_checkbox_toggled(self, sender, e) -> None:
        from win32more.Microsoft.UI.Xaml.Controls import ListViewSelectionMode
        on = self.checkbox_toggle.IsOn
        self.file_list.IsMultiSelectCheckBoxEnabled = on
        self.file_list.SelectionMode = (
            ListViewSelectionMode.Multiple if on else ListViewSelectionMode.Extended)

    # ---------- 导航 ----------

    def _on_share_selected(self, sender, e) -> None:
        idx = self.shares_list.SelectedIndex
        if idx is None or idx < 0 or idx >= len(getattr(self, "_shares", [])):
            return
        share = self._shares[idx].name
        asyncui.create_task(self._navigate(share, ""))
        asyncui.create_task(self._load_volume(share))

    async def _load_volume(self, share: str) -> None:
        self.vol_text.Text = _("读取容量…")
        vi = await asyncio.to_thread(self.shell.client.volume_info, share)
        if self.share != share:
            return
        if vi is None:
            self.vol_text.Text = _("该共享不支持容量查询")
            self.vol_bar.Value = 0
            return
        self.vol_bar.Value = vi.percent
        self.vol_text.Text = (_("{0} / {1} 已用 {percent:.0f}% · 空闲 {2}").format(
            human_size(vi.used), human_size(vi.total), human_size(vi.free), percent=vi.percent))

    async def _navigate(self, share: str, path: str, *, force: bool = False) -> None:
        """进目录:**乐观显示 + 后台对账**。

        本地目录索引缓存命中就立刻出列表(零 SMB 往返,秒开),同时后台发一次
        真 ``listdir`` 校验 —— 一致就把状态栏的"核对中"改成"已核对",不一致就
        原地换成设备上的最新内容并标注差异。``force=True``(刷新按钮/增删改后
        回刷)跳过缓存直接走网络。
        """
        self._cancel_search()
        self._search_hits = None
        self._nav_gen += 1
        gen = self._nav_gen
        host = self.shell.client.host
        # 转圈用**在飞计数**而不是布尔:被更晚一次导航挤掉的这次会中途 return,
        # 直接 busy(False) 会把还在读的那次的转圈关掉;计数归零才关最稳。
        self._nav_busy += 1
        self.shell.busy(True)
        try:
            if not force:
                try:
                    cached = await asyncio.to_thread(
                        dircache.get_with_age, host, share, path)
                except Exception:
                    cached = None       # 缓存永远是可选的,坏了就当没有
                if gen != self._nav_gen:
                    return              # 更晚的一次导航已经接管
                if cached is not None:
                    entries, age = cached
                    self._show_entries(
                        share, path, entries,
                        note=_("▣ 本地索引({0}) · 核对中…").format(dircache.age_text(age)))
                    self._start_reconcile(gen, share, path)
                    return
            self.list_status.Text = _("正在读取目录…")
            try:
                entries = await asyncio.to_thread(
                    self.shell.client.listdir, share, path)
            except SmbClientError as ex:
                if gen == self._nav_gen:
                    self.shell.error(str(ex))
                return
            if gen != self._nav_gen:
                return
            self._show_entries(share, path, entries)
            dircache.put_async(host, share, path, entries)   # 落盘甩到守护线程
        finally:
            self._nav_busy = max(0, self._nav_busy - 1)
            if self._nav_busy == 0:
                self.shell.busy(False)

    def _show_entries(self, share: str, path: str,
                      entries: list[RemoteEntry], note: str = "") -> None:
        """把一份目录列表落到视图上(缓存命中与网络读取共用)。"""
        self.share, self.path = share, path
        self.entries = entries
        display = f"{share}/{path.replace(chr(92), '/')}" if path else share
        self.path_text.Text = display
        self.up_btn.IsEnabled = True
        self.refresh_btn.IsEnabled = True
        self._render(sorted_entries(entries, self.sort_box.SelectedIndex), note=note)

    # ---------- 缓存对账 ----------

    def _start_reconcile(self, gen: int, share: str, path: str) -> None:
        """后台发一次真 listdir 与当前显示的缓存列表对账(独立 clone,不占共享连接)。"""
        host = self.shell.client.host

        def work() -> None:
            client = None
            try:
                client = self.shell.client.clone()
                client.connect()
                fresh = client.listdir(share, path)
            except SmbClientError as ex:
                self.shell.ui(self._reconcile_failed, gen, str(ex), False)
                return
            except Exception as ex:     # 兜底:意外异常不许无声杀线程(§11)
                self.shell.ui(self._reconcile_failed, gen, f"{ex!r}", True)
                return
            finally:
                if client is not None:
                    client.close()
            dircache.put(host, share, path, fresh)
            self.shell.ui(self._reconcile_done, gen, share, path, fresh)

        threading.Thread(target=work, daemon=True, name="dir-reconcile").start()

    def _reconcile_done(self, gen: int, share: str, path: str,
                        fresh: list[RemoteEntry]) -> None:
        if gen != self._nav_gen or share != self.share or path != self.path:
            return                          # 用户已切走
        if self._searching or self._search_hits is not None:
            return                          # 视图已被搜索结果接管,别抢
        if dircache.same(self.entries, fresh):
            self._nav_note = _("▣ 本地索引 · 已核对")
            self._refresh_list_status()
            return
        added, removed, changed = dircache.diff_summary(self.entries, fresh)
        self.entries = fresh
        self._render(sorted_entries(fresh, self.sort_box.SelectedIndex),
                     note=_("● 已同步设备最新内容(新增 {added} · 消失 {removed} · 变化 {changed})").format(
                         added=added, removed=removed, changed=changed))

    def _reconcile_failed(self, gen: int, msg: str, unexpected: bool) -> None:
        if gen == self._nav_gen:
            brief = msg if len(msg) <= 40 else msg[:40] + "…"
            self._nav_note = _("▣ 本地索引 · 未能核对({brief})").format(brief=brief)
            self._refresh_list_status()
        if unexpected:
            self.shell.error(_("目录核对出错: {msg}").format(msg=msg))

    def _refresh_list_status(self) -> None:
        """列表状态栏 = 前缀提示(缓存/核对/搜索) + 统计正文,两者各自独立更新。"""
        if self._nav_note:
            self.list_status.Text = f"{self._nav_note}  {self._status_body}"
        else:
            self.list_status.Text = self._status_body

    def _invalidate_dir(self, share: str, path: str, *,
                        subtree: bool = True, trees: bool = True) -> None:
        """主动作废某目录的索引缓存;sqlite I/O 甩到守护线程(UI 线程不落盘)。"""
        if not share:
            return          # share=None 会被 dircache 当成"整台设备",别误伤
        host = self.shell.client.host

        def work() -> None:
            dircache.invalidate(host, share, path, subtree=subtree, trees=trees)

        threading.Thread(target=work, daemon=True, name="dircache-inv").start()

    def _on_up(self, sender, e) -> None:
        if self.share is None:
            return
        if self.path:
            parent = ntpath.dirname(self.path)
            asyncui.create_task(self._navigate(self.share, parent))

    def _on_refresh(self, sender, e) -> None:
        if self.share is None:
            return
        # 刷新 = 用户明确要设备上的最新内容:先作废本目录索引再强制走网络
        # (占用树不动 —— 刷新一层目录不代表整个共享的统计都作废)
        self._invalidate_dir(self.share, self.path, subtree=False, trees=False)
        asyncui.create_task(self._navigate(self.share, self.path, force=True))

    # ---------- 渲染 ----------

    def _make_row(self, entry: RemoteEntry, name_override: str | None = None) -> Grid:
        g = Grid()
        # 图标 | 夜次徽章 | 名字(+副行) | 大小 | 时间
        for width, unit in ((26, GridUnitType.Pixel), (52, GridUnitType.Pixel),
                            (1, GridUnitType.Star),
                            (110, GridUnitType.Pixel), (140, GridUnitType.Pixel)):
            c = ColumnDefinition()
            c.Width = GridLength(Value=float(width), GridUnitType=unit)
            g.ColumnDefinitions.Append(c)

        icon = FontIcon()
        icon.Glyph = glyph_for(entry)
        icon.FontSize = 14
        if entry.is_dir:
            icon.Opacity = 0.95
        else:
            icon.Opacity = 0.6
        g.Children.Append(icon)
        Grid.SetColumn(icon, 0)

        # 夜次徽章(目录不显示,列位仍占位保持对齐)
        chip = self._night_chip(entry)
        if chip is not None:
            g.Children.Append(chip)
            Grid.SetColumn(chip, 1)

        name = TextBlock()
        text = name_override or entry.name
        name.Text = text
        name.TextTrimming = TextTrimming.CharacterEllipsis
        if entry.is_dir:
            name.FontWeight = FontWeights.SemiBold  # 文件夹加粗以区分
        if len(text) > 30:
            ToolTipService.SetToolTip(name, text)
        sub = None if entry.is_dir else self._astro_subline(entry)
        if sub is not None:
            # 天文文件:名字下加副行(文件名解析即时显示,FITS 头信息稍后懒加载补充)
            panel = StackPanel()
            panel.VerticalAlignment = VerticalAlignment.Center
            panel.Children.Append(name)
            subtb = TextBlock()
            subtb.Text = sub
            subtb.FontSize = 11
            subtb.Opacity = 0.6
            subtb.TextTrimming = TextTrimming.CharacterEllipsis
            panel.Children.Append(subtb)
            g.Children.Append(panel)
            Grid.SetColumn(panel, 2)
            ext = os.path.splitext(entry.name)[1].lower()
            if ext in FITS_EXTS:
                self._fits_cells[entry.path] = (subtb, sub)
        else:
            name.VerticalAlignment = VerticalAlignment.Center
            g.Children.Append(name)
            Grid.SetColumn(name, 2)

        info = TextBlock()
        info.Opacity = 0.7
        info.FontSize = 12
        info.VerticalAlignment = VerticalAlignment.Center
        if entry.is_dir:
            info.Text = _("… 项")
            self._count_cells[entry.path] = info  # 子项数异步填充
        else:
            info.Text = human_size(entry.size)
        g.Children.Append(info)
        Grid.SetColumn(info, 3)

        mtime = TextBlock()
        mtime.Text = format_mtime(entry.mtime)
        mtime.Opacity = 0.7
        mtime.FontSize = 12
        mtime.VerticalAlignment = VerticalAlignment.Center
        g.Children.Append(mtime)
        Grid.SetColumn(mtime, 4)
        return g

    # ---------- 夜次徽章(#2) ----------

    def _assign_night_colors(self, entries: list[RemoteEntry]) -> None:
        """全视图统一分配 夜次→色号:按夜次日期排序取色,同夜同色、邻夜不同色。

        纯文件名解析(µs 级),在 _render 开头一次算完,_make_row 只查表。
        """
        keys: set[str] = set()
        for e in entries:
            if e.is_dir:
                continue
            k = _night_of_name(e.name)
            if k:
                keys.add(k)
        self._night_colors = {k: i for i, k in enumerate(sorted(keys))}

    def _night_index(self, key: str) -> int:
        """夜次 → 色号;表里没有(搜索结果流式追加)则按出现顺序补号。"""
        idx = self._night_colors.get(key)
        if idx is None:
            idx = len(self._night_colors)
            self._night_colors[key] = idx
        return idx

    def _night_chip(self, entry: RemoteEntry) -> Border | None:
        """行内夜次徽章:"月-日" 圆角小胶囊;目录/无时间戳文件返回 None。"""
        if entry.is_dir:
            return None
        key = _night_of_name(entry.name)
        if not key:
            return None
        bg, fg = self._night_brushes[self._night_index(key) % len(self._night_brushes)]
        chip = Border()
        chip.CornerRadius = _corner(8.0)
        chip.Background = bg
        chip.Padding = Thickness(Left=5, Top=0, Right=5, Bottom=1)
        chip.HorizontalAlignment = HorizontalAlignment.Center
        chip.VerticalAlignment = VerticalAlignment.Center
        tb = TextBlock()
        tb.Text = key[5:]                   # 'YYYY-MM-DD' → 'MM-DD'
        tb.FontSize = 10
        tb.FontWeight = FontWeights.SemiBold
        tb.Foreground = fg
        chip.Child = tb
        ToolTipService.SetToolTip(chip, _("观测夜 {key}(正午分界)").format(key=key))
        return chip

    def _render(self, entries: list[RemoteEntry], note: str = "", name_fn=None) -> None:
        self._render_gen += 1
        self._count_gen += 1  # 取消上一视图残留的子项计数/FITS 元数据线程
        gen = self._render_gen
        # 前缀提示随视图换代;对账线程之后只改 _nav_note 再 _refresh_list_status,
        # 不必整表重画(见 _reconcile_done)
        self._nav_note = note
        self.file_list.Items.Clear()
        self._rendered = []
        self._count_cells = {}
        self._fits_cells = {}
        todo = entries[:RENDER_CAP]
        truncated = len(entries) > RENDER_CAP
        self._assign_night_colors(todo)     # 配色需全视图信息,先于建行算好

        def append_batch(start: int) -> None:
            if gen != self._render_gen:
                return
            end = min(start + RENDER_BATCH, len(todo))
            for e in todo[start:end]:
                override = name_fn(e) if name_fn else None
                self.file_list.Items.Append(self._make_row(e, name_override=override))
                self._rendered.append(e)
            if end < len(todo):
                self.list_status.Text = _("加载中 {end}/{0} …").format(len(todo), end=end)
                self.dispatcher.TryEnqueue(lambda: append_batch(end))
            else:
                ndir = sum(1 for e in todo if e.is_dir)
                total = sum(e.size for e in todo if not e.is_dir)
                text = _("{0} 个文件 ({1}), {ndir} 个目录").format(
                    len(todo) - ndir, human_size(total), ndir=ndir)
                if truncated:
                    text += _(" — 仅显示前 {RENDER_CAP} 项(共 {0}),可用搜索过滤").format(
                        len(entries), RENDER_CAP=RENDER_CAP)
                self._status_body = text
                self._refresh_list_status()
                self._start_child_counts(todo)
                self._start_fits_meta(todo)
                # 调试钩子(§7.10):渲染完成后自动选中第 N 项(验证详情面板用)
                auto_sel = os.environ.get("ASTRO_SMB_GUI_AUTOSELECT")
                if auto_sel:
                    try:
                        idx = int(auto_sel)
                        if 0 <= idx < len(self._rendered):
                            self.file_list.SelectedIndex = idx
                    except ValueError:
                        pass

        append_batch(0)

    def _start_child_counts(self, entries: list[RemoteEntry]) -> None:
        """后台统计目录子项数,填回列表(#3)。

        **两段式**:先用本地目录索引缓存把数字立刻填上(sqlite,零 SMB 往返),
        再只对"缓存缺失或已超 CHILD_COUNT_TTL"的目录补一次真 listdir。补的时候
        顺手把结果写回索引缓存 —— 用户点进那个子目录时就是秒开。

        _count_gen 已在 _render / _on_search_click 里随视图切换递增,这里读取
        当时的代次;视图一变,本线程的 gen 即失配而自然退出。
        """
        dirs = [e for e in entries if e.is_dir]
        if not dirs:
            return
        gen = self._count_gen
        host = self.shell.client.host

        def work() -> None:
            stale: list[RemoteEntry] = []
            for d in dirs:                              # 第 1 段:纯本地
                if gen != self._count_gen:
                    return
                got = dircache.get_with_age(host, d.share, d.path)
                if got is None:
                    stale.append(d)
                    continue
                cached, age = got
                nd = sum(1 for x in cached if x.is_dir)
                self.shell.ui(self._apply_count, gen, d.path,
                              nd, len(cached) - nd)
                if age > CHILD_COUNT_TTL:
                    stale.append(d)     # 旧数字先显示着,后台再校一次
            if not stale:
                return
            client = None
            try:                                        # 第 2 段:补网络
                client = self.shell.client.clone()
                client.connect()
                for d in stale:
                    if gen != self._count_gen:
                        return
                    try:
                        children = client.listdir(d.share, d.path)
                    except SmbClientError:
                        # 枚举失败(无权限/瞬时错误)显示 "?",区别于真空目录
                        self.shell.ui(self._apply_count, gen, d.path, -1, -1)
                        continue
                    dircache.put(host, d.share, d.path, children)
                    nd = sum(1 for x in children if x.is_dir)
                    self.shell.ui(self._apply_count, gen, d.path,
                                  nd, len(children) - nd)
            except SmbClientError:
                pass
            except Exception as ex:     # 兜底:意外异常不许无声杀线程(§11)
                self.shell.ui(self.shell.error, _("子项计数出错: {ex!r}").format(ex=ex))
            finally:
                if client is not None:
                    client.close()

        threading.Thread(target=work, daemon=True, name="child-count").start()

    def _apply_count(self, gen: int, path: str, ndir: int, nfile: int) -> None:
        if gen != self._count_gen:
            return
        cell = self._count_cells.get(path)
        if cell is None:
            return
        if ndir < 0:
            cell.Text = "?"  # 枚举失败(无权限/瞬时错误),区别于真空目录
        elif ndir and nfile:
            cell.Text = _("{ndir}目录/{nfile}文件").format(ndir=ndir, nfile=nfile)
        elif ndir:
            cell.Text = _("{ndir} 目录").format(ndir=ndir)
        elif nfile:
            cell.Text = _("{nfile} 文件").format(nfile=nfile)
        else:
            cell.Text = _("空")

    # ---------- FITS 元数据懒加载(#lazyload) ----------

    def _astro_subline(self, entry: RemoteEntry) -> str | None:
        """文件名解析出的即时副行;非天文命名的 FITS 返回 ""(等头部填充),
        其他文件返回 None(不加副行)。"""
        info = parse_image_name(entry.name)
        ext = os.path.splitext(entry.name)[1].lower()
        if info is None:
            return "" if ext in FITS_EXTS else None
        parts: list[str] = []
        if info.target:
            parts.append(info.target)
        else:
            parts.append(_KIND_CN.get(info.kind, info.kind))
        parts.append(_fmt_exposure(info.exposure_s, info.exposure))
        if info.filter:
            parts.append(info.filter)
        if info.binning != 1:
            parts.append(f"Bin{info.binning}")
        if info.seq is not None:
            parts.append(f"#{info.seq:04d}")
        return " · ".join(parts)

    def _start_fits_meta(self, entries: list[RemoteEntry]) -> None:
        """后台逐个部分读取 FITS 头(几 KB/个),把增益/温度等补进副行。

        与子项计数共用 _count_gen 代次:视图一变线程自然退出。
        结果按 (share,path,size,mtime) 进内存缓存,回到同目录不重复读。
        """
        fits = [e for e in entries
                if not e.is_dir
                and os.path.splitext(e.name)[1].lower() in FITS_EXTS][:500]
        if not fits:
            return
        gen = self._count_gen

        def work() -> None:
            client = None
            try:
                client = self.shell.client.clone()
                client.connect()
                fails = 0
                for e in fits:
                    if gen != self._count_gen:
                        return
                    key = (e.share, e.path, e.size, e.mtime)
                    suffix = self._hdr_cache.get(key)
                    if suffix is None:
                        try:
                            hdr = read_fits_header(client, e)
                        except SmbClientError:
                            fails += 1
                            if fails >= 3:      # 连续失败视为连接问题,停止本轮
                                return
                            continue
                        fails = 0
                        suffix = _hdr_suffix(hdr, parse_image_name(e.name))
                        self._hdr_cache[key] = suffix
                    if suffix:
                        self.shell.ui(self._apply_fits_meta, gen, e.path, suffix)
            except SmbClientError:
                pass
            finally:
                if client is not None:
                    client.close()

        threading.Thread(target=work, daemon=True, name="fits-meta").start()

    def _apply_fits_meta(self, gen: int, path: str, suffix: str) -> None:
        if gen != self._count_gen:
            return
        cell = self._fits_cells.get(path)
        if cell is None:
            return
        tb, base = cell
        tb.Text = f"{base} · {suffix}" if base else suffix

    def _search_name(self, entry: RemoteEntry) -> str:
        rel = entry.path
        if self._search_prefix and rel.startswith(self._search_prefix):
            rel = rel[len(self._search_prefix):]
        return rel.replace("\\", "/")

    def _on_sort_changed(self, sender, e) -> None:
        if self._searching:
            return
        if self._search_hits is not None:
            self._render(sorted_entries(self._search_hits, self.sort_box.SelectedIndex),
                         note=_("搜索结果"), name_fn=self._search_name)
        elif self.entries:
            # 保留当前的缓存/核对提示 —— 换个排序不代表数据来源变了
            self._render(sorted_entries(self.entries, self.sort_box.SelectedIndex),
                         note=self._nav_note)

    # ---------- 选中 / 预览 ----------

    def _selected_entries(self) -> list[RemoteEntry]:
        out: list[RemoteEntry] = []
        try:
            ranges = self.file_list.SelectedRanges
            for i in range(ranges.Size):
                r = ranges.GetAt(i)
                for idx in range(r.FirstIndex, r.LastIndex + 1):
                    if 0 <= idx < len(self._rendered):
                        out.append(self._rendered[idx])
        except Exception:
            idx = self.file_list.SelectedIndex
            if idx is not None and 0 <= idx < len(self._rendered):
                out.append(self._rendered[idx])
        return out

    def _reset_preview_panel(self) -> None:
        """换选/多选/清空时统一复位预览区控件(残留内容会被误当当前选择的属性)。"""
        self.preview_image.put_Source(None)
        self.preview_text.Visibility = Visibility.Collapsed
        self.fits_toggle.Visibility = Visibility.Collapsed
        self.fits_text.Visibility = Visibility.Collapsed
        self._fits_visible = False              # 与 ToggleButton 状态保持同步
        try:
            self.fits_toggle.IsChecked = False
        except Exception:
            pass
        self.load_full_btn.Visibility = Visibility.Collapsed
        if self.open_viewer_btn is not None:
            self.open_viewer_btn.Visibility = Visibility.Collapsed
        self.thumb_source_text.Text = ""

    def _is_fits(self, entry: RemoteEntry) -> bool:
        return (not entry.is_dir
                and os.path.splitext(entry.name)[1].lower() in FITS_EXTS)

    def _open_in_viewer(self, entry: RemoteEntry) -> bool:
        """跳到 FITS 查看器页;shell 还没接线时返回 False,调用方回落原行为。

        页面注册/`open_fitsview` 由主控在 _window.py 接线,浏览页**不能假设**
        它一定在(缺了就退回下载,而不是抛异常把双击弄坏)。
        """
        opener = getattr(self.shell, "open_fitsview", None)
        if opener is None:
            return False
        try:
            opener(entry.share, entry.path)
            return True
        except Exception as ex:
            self.shell.error(_("打开 FITS 查看器失败: {ex}").format(ex=ex))
            return False

    def _on_open_viewer(self, sender, e) -> None:
        entry = self._preview_entry
        if entry is None or entry.is_dir:
            return
        if not self._open_in_viewer(entry):
            self.shell.info(_("FITS 查看器尚未接入(shell 缺少 open_fitsview)"))

    def _on_open_viewer_menu(self, sender, e) -> None:
        # 必须按 _is_fits 过滤(按钮和双击都过滤了,这里以前漏了):
        # 拿一个 _thn.jpg / .txt 进查看器会先切页清掉当前画面,
        # 几百毫秒后再弹一句「打开失败: FITS 头不完整」——纯纯的误伤
        sel = [x for x in self._selected_entries() if self._is_fits(x)]
        if not sel:
            self.shell.info(_("先选中一个 FITS 文件(.fit/.fits/.fts)"))
            return
        if not self._open_in_viewer(sel[0]):
            self.shell.info(_("FITS 查看器尚未接入(shell 缺少 open_fitsview)"))

    def _on_file_selected(self, sender, e) -> None:
        sel = self._selected_entries()
        if len(sel) != 1:
            # 使在途预览请求过期,避免过期结果稍后覆盖多选/清空后的面板
            self._preview_token += 1
            self._preview_entry = None
            self.preview_ring.IsActive = False
            self._reset_preview_panel()
            self.astro_card.Visibility = Visibility.Collapsed
            self.file_card.Visibility = Visibility.Collapsed
            self.copy_detail_btn.Visibility = Visibility.Collapsed
            self._detail_copy_text = ""
            if len(sel) > 1:
                total = sum(x.size for x in sel if not x.is_dir)
                self.preview_title.Text = _("已选中 {0} 项 ({1})").format(
                    len(sel), human_size(total))
            else:
                self.preview_title.Text = _("(未选中)")
            return
        entry = sel[0]
        self._preview_entry = entry
        self._preview_token += 1
        token = self._preview_token
        self._update_detail(entry, None)
        self._reset_preview_panel()
        if self.open_viewer_btn is not None and self._is_fits(entry):
            self.open_viewer_btn.Visibility = Visibility.Visible
        if not entry.is_dir:
            self.preview_ring.IsActive = True
            self.shell.preview.request(token, entry)
        else:
            self.preview_ring.IsActive = False

    def _add_row(self, grid: Grid) -> None:
        """给两列 KV Grid 追加一行 Auto 高度的 RowDefinition。"""
        rd = RowDefinition()
        rd.Height = GridLength(Value=1.0, GridUnitType=GridUnitType.Auto)
        grid.RowDefinitions.Append(rd)

    def _add_pairs(self, grid: Grid, row: int, pairs: list[tuple]) -> int:
        """把 (标签, 值[, 副注[, 等宽[, 语义色[, 小组件]]]]) 逐行填进两列 Grid,
        返回下一个可用行号。

        标签淡色,值可选中复制;副注(如 "(3 分钟)")以淡色小字随主值横排;
        等宽=True 时主值用 Consolas(坐标等对齐敏感内容);语义色只染主值
        (标签保持淡色);小组件如 ("altbar", 高度角) 画在主值左侧。
        兼容纯 (k, v) 二元组(文件卡片/预览附加信息)。
        """
        for item in pairs:
            k, v = item[0], item[1]
            note = item[2] if len(item) > 2 else ""
            mono = item[3] if len(item) > 3 else False
            tone = item[4] if len(item) > 4 else None
            widget = item[5] if len(item) > 5 else None
            self._add_row(grid)
            lab = TextBlock()
            lab.Text = k
            lab.FontSize = 12
            lab.Opacity = 0.55
            grid.Children.Append(lab)
            Grid.SetRow(lab, row)
            Grid.SetColumn(lab, 0)
            val = TextBlock()
            val.Text = v
            val.FontSize = 12
            val.TextWrapping = TextWrapping.Wrap
            val.IsTextSelectionEnabled = True
            val.VerticalAlignment = VerticalAlignment.Center
            if mono:
                val.FontFamily = self._mono_font
            if tone is not None:
                brush = self._tone_brushes.get(tone)
                if brush is not None:
                    val.Foreground = brush
                    val.FontWeight = FontWeights.SemiBold
            if note or widget is not None:
                # [小组件] 主值 + 淡色副注横排(副注都很短,不需要换行)
                panel = StackPanel()
                panel.Orientation = Orientation.Horizontal
                panel.Spacing = 6
                panel.VerticalAlignment = VerticalAlignment.Center
                gadget = self._make_gadget(widget, tone)
                if gadget is not None:
                    panel.Children.Append(gadget)
                panel.Children.Append(val)
                if note:
                    aux = TextBlock()
                    aux.Text = note
                    aux.FontSize = 11
                    aux.Opacity = 0.55
                    aux.TextWrapping = TextWrapping.Wrap
                    aux.VerticalAlignment = VerticalAlignment.Center
                    panel.Children.Append(aux)
                grid.Children.Append(panel)
                Grid.SetRow(panel, row)
                Grid.SetColumn(panel, 1)
            else:
                grid.Children.Append(val)
                Grid.SetRow(val, row)
                Grid.SetColumn(val, 1)
            row += 1
        return row

    def _fill_groups(self, grid: Grid, groups: list[tuple]) -> None:
        """清空后按 (图标, 组名, 键值对) 分区填充:每组一条小标题行 + 组内 KV。

        空组自动跳过(该文件没有对应信息时不留空标题)。
        """
        grid.RowDefinitions.Clear()
        grid.Children.Clear()
        row = 0
        for glyph, name, pairs in groups:
            if not pairs:
                continue
            row = self._add_group_header(grid, row, glyph, name, first=(row == 0))
            row = self._add_pairs(grid, row, pairs)

    def _add_group_header(self, grid: Grid, row: int, glyph: str, name: str,
                          first: bool = False) -> int:
        """分区小标题:图标 + 组名(淡色小字)+ 一条细分隔线,横跨两列。"""
        self._add_row(grid)
        head = Grid()
        for width, unit in ((1.0, GridUnitType.Auto), (1.0, GridUnitType.Auto),
                            (1.0, GridUnitType.Star)):
            c = ColumnDefinition()
            c.Width = GridLength(Value=width, GridUnitType=unit)
            head.ColumnDefinitions.Append(c)
        head.Margin = Thickness(Left=0, Top=(1 if first else 9), Right=0, Bottom=1)

        icon = FontIcon()
        icon.Glyph = glyph
        icon.FontSize = 11
        icon.Opacity = 0.55
        icon.VerticalAlignment = VerticalAlignment.Center
        head.Children.Append(icon)
        Grid.SetColumn(icon, 0)

        lab = TextBlock()
        lab.Text = name
        lab.FontSize = 11
        lab.Opacity = 0.55
        lab.Margin = Thickness(Left=6, Top=0, Right=0, Bottom=0)
        lab.VerticalAlignment = VerticalAlignment.Center
        head.Children.Append(lab)
        Grid.SetColumn(lab, 1)

        line = Border()
        line.Height = 1
        line.Background = self._divider
        line.VerticalAlignment = VerticalAlignment.Center
        line.Margin = Thickness(Left=8, Top=0, Right=0, Bottom=0)
        head.Children.Append(line)
        Grid.SetColumn(line, 2)

        grid.Children.Append(head)
        Grid.SetRow(head, row)
        Grid.SetColumn(head, 0)
        Grid.SetColumnSpan(head, 2)
        return row + 1

    def _make_gadget(self, spec, tone: str | None):
        """行内小组件:目前只有 ("altbar", 高度角) —— 0~90° 水平刻度条 + 指针。"""
        if not spec:
            return None
        try:
            kind = spec[0]
            if kind == "altbar":
                return self._alt_bar(float(spec[1]), tone)
        except (TypeError, ValueError, IndexError):
            return None
        return None

    def _alt_bar(self, alt_deg: float, tone: str | None) -> Canvas:
        """高度角迷你条(120×26):底槽 + 语义色填充 + 30°/60° 刻度 + 指针与端标。"""
        w, h = 120.0, 26.0
        track_y, track_h = 8.0, 6.0
        canvas = Canvas()
        canvas.Width, canvas.Height = w, h
        canvas.VerticalAlignment = VerticalAlignment.Center
        if alt_deg <= 0.0:      # 地平线下:整条弱化,指针停在最左端(0°),无填充
            canvas.Opacity = 0.4

        track = Rectangle()
        track.Width, track.Height = w, track_h
        track.RadiusX = track.RadiusY = 3.0
        track.Fill = self._track_bg
        canvas.Children.Append(track)
        Canvas.SetLeft(track, 0.0)
        Canvas.SetTop(track, track_y)

        frac = max(0.0, min(1.0, alt_deg / 90.0))
        if frac > 0.0:
            brush = self._tone_brushes.get(tone) if tone else None
            fill = Rectangle()
            fill.Width, fill.Height = max(2.0, w * frac), track_h
            fill.RadiusX = fill.RadiusY = 3.0
            fill.Fill = brush if brush is not None else self._tone_brushes["good"]
            canvas.Children.Append(fill)
            Canvas.SetLeft(fill, 0.0)
            Canvas.SetTop(fill, track_y)

        for deg in (30.0, 60.0):        # 语义阈值附近的参考刻度
            ln = Line()
            ln.X1 = ln.X2 = w * deg / 90.0
            ln.Y1, ln.Y2 = track_y - 2.0, track_y + track_h + 2.0
            ln.Stroke = self._track_tick
            ln.StrokeThickness = 1.0
            canvas.Children.Append(ln)

        ptr = Rectangle()               # 当前高度指针
        ptr.Width, ptr.Height = 2.0, track_h + 8.0
        ptr.Fill = self._track_tick
        canvas.Children.Append(ptr)
        Canvas.SetLeft(ptr, max(0.0, min(w - 2.0, w * frac - 1.0)))
        Canvas.SetTop(ptr, track_y - 4.0)

        for text, x in (("0°", 0.0), ("90°", w - 18.0)):
            tb = TextBlock()
            tb.Text = text
            tb.FontSize = 9
            tb.Opacity = 0.45
            canvas.Children.Append(tb)
            Canvas.SetLeft(tb, x)
            Canvas.SetTop(tb, track_y + track_h + 2.0)
        return canvas

    def _update_detail(self, entry: RemoteEntry, result: PreviewResult | None) -> None:
        """右栏详情:天文卡片(分区结构化拍摄参数) + 文件卡片。"""
        self.preview_title.Text = entry.name

        # --- 文件卡片(文件 / 位置 两分区) ---
        kind = _("目录") if entry.is_dir else ext_category(entry)
        fpairs: list[tuple] = [(_("类型"), kind)]
        if not entry.is_dir:
            fpairs.append((_("大小"), _("{0} ({size:,} 字节)").format(
                human_size(entry.size), size=entry.size)))
        if result is not None and result.image_size:
            fpairs.append((_("尺寸"), f"{result.image_size[0]} × {result.image_size[1]}"))
        fpairs += [
            (_("修改"), format_mtime(entry.mtime)),
            (_("创建"), format_mtime(entry.ctime)),
            (_("属性"), f"{entry.attr_text()} (0x{entry.attributes:02X})"),
        ]
        lpairs: list[tuple] = [
            (_("路径"), f"\\\\{self.shell.client.host}\\{entry.share}"
             + (f"\\{entry.path}" if entry.path else "")),
        ]
        if result is not None:
            lpairs += list(result.extra)
        fgroups = [(_GRP_FILE, _("文件"), fpairs), (_GRP_PLACE, _("位置"), lpairs)]
        self._fill_groups(self.file_grid, fgroups)
        self.file_card.Visibility = Visibility.Visible

        # --- 天文卡片 ---
        site = self._site_latlon()
        title, sub, groups, badges, sky, pills = _astro_details(
            entry, result.fits if result is not None else None, site)
        if title is not None:
            self.astro_target.Text = title
            self.astro_sub.Text = sub
            self.astro_sub.Visibility = (
                Visibility.Visible if sub else Visibility.Collapsed)
            self._render_badges(badges)
            self._render_pills(pills)
            self._fill_groups(self.astro_grid, groups)
            self._update_radar(sky, site)
            self.astro_card.Visibility = Visibility.Visible
        else:
            # 非天文文件:_astro_details 已保证 groups/badges/pills 为空
            self.astro_card.Visibility = Visibility.Collapsed

        # 「复制全部信息」的文本(纯字符串拼接,数据都已算好)
        self._detail_copy_text = _detail_text(
            entry, title, sub, badges, pills, list(groups) + fgroups)
        self.copy_detail_btn.Visibility = Visibility.Visible

    def _site_latlon(self) -> tuple[float, float] | None:
        """站点 (纬度, 经度):纬度取本地配置;经度优先日志推算值,兜底配置。"""
        try:
            site = load_site()
            lat = float(site.get("lat", 30.0))
            lon = float(site.get("lon", 120.0))
        except Exception:
            return None
        try:
            store = getattr(self.shell, "logstore", None)
            data = store.data if store is not None else None
            if data is not None and data.lon_estimate is not None:
                lon = float(data.lon_estimate)
        except Exception:
            pass
        return lat, lon

    def _render_badges(self, badges: list[tuple[str, str]]) -> None:
        """徽章行:圆角小胶囊(浅色底深色字),帧类型/滤镜/Bin/序号/夜次。

        夜次徽章样式键形如 "night:2026-07-23",取当前视图的夜次配色,
        与列表徽章列同色(同夜同色,一眼对得上)。
        """
        self.astro_badges.Children.Clear()
        for text, style in badges:
            tip = ""
            if style.startswith("night:"):
                key = style.split(":", 1)[1]
                bg, fg = self._night_brushes[
                    self._night_index(key) % len(self._night_brushes)]
                tip = _("观测夜 {key}(正午分界)").format(key=key)
            else:
                bg, fg = self._badge_brushes.get(style, self._badge_brushes["seq"])
            chip = Border()
            chip.CornerRadius = _corner(9.0)
            chip.Background = bg
            chip.Padding = Thickness(Left=8, Top=1, Right=8, Bottom=2)
            tb = TextBlock()
            tb.Text = text
            tb.FontSize = 11
            tb.FontWeight = FontWeights.SemiBold
            tb.Foreground = fg
            chip.Child = tb
            if tip:
                ToolTipService.SetToolTip(chip, tip)
            self.astro_badges.Children.Append(chip)
        self.astro_badges.Visibility = (
            Visibility.Visible if badges else Visibility.Collapsed)

    def _render_pills(self, pills: list[tuple]) -> None:
        """参数胶囊行:曝光/增益/温度做成中性底色小 pill,语义色只染文字。"""
        self.astro_pills.Children.Clear()
        for item in pills:
            text = item[0]
            tip = item[1] if len(item) > 1 else ""
            tone = item[2] if len(item) > 2 else None
            pill = Border()
            pill.CornerRadius = _corner(4.0)
            pill.Background = self._pill_bg
            pill.Padding = Thickness(Left=7, Top=1, Right=7, Bottom=2)
            tb = TextBlock()
            tb.Text = text
            tb.FontSize = 12
            tb.FontWeight = FontWeights.SemiBold
            if tone is not None:
                brush = self._tone_brushes.get(tone)
                if brush is not None:
                    tb.Foreground = brush
            pill.Child = tb
            if tip:
                ToolTipService.SetToolTip(pill, tip)
            self.astro_pills.Children.Append(pill)
        self.astro_pills.Visibility = (
            Visibility.Visible if pills else Visibility.Collapsed)

    def _on_copy_detail(self, sender, e) -> None:
        """把当前详情(徽章/胶囊/各分区键值)组装成多行文本写剪贴板。"""
        text = self._detail_copy_text
        if not text:
            self.shell.info(_("先选中单个文件查看详情"))
            return
        try:
            pack = DataPackage()
            pack.SetText(text)
            Clipboard.SetContent(pack)
            try:
                Clipboard.Flush()   # 让内容在 DataPackage 释放后仍留在剪贴板
            except Exception:
                pass
            self.shell.info(_("已复制详情({0} 行)到剪贴板").format(text.count(chr(10)) + 1))
        except Exception as ex:
            self.shell.error(_("复制详情失败: {ex}").format(ex=ex))

    def _update_radar(self, sky, site) -> None:
        """迷你天球雷达:有坐标 + 时刻 + 站点时绘制,否则整个 Canvas 折叠。

        alt/az 已在 _astro_details 里算好(sky 元组),这里只驱动绘制。
        """
        if sky is None or site is None:
            try:
                self._radar.clear()
            except Exception:
                pass
            self.detail_radar.Visibility = Visibility.Collapsed
            return
        ra, dec, ts, alt, az = sky
        try:
            self._radar.show(ra, dec, site[0], site[1], ts,
                             caption=_("高度 {alt:.0f}° · 方位 {0}").format(
                                 _az_name(az), alt=alt))
            self.detail_radar.Visibility = Visibility.Visible
        except Exception:
            self.detail_radar.Visibility = Visibility.Collapsed

    def apply_preview(self, result: PreviewResult) -> None:
        if result.token != self._preview_token:
            return
        self.preview_ring.IsActive = False
        entry = result.entry
        self._update_detail(entry, result)
        if result.kind == "error":
            self.thumb_source_text.Text = _("预览失败: {error}").format(error=result.error)
            return
        if result.kind == "image" and result.thumb_path:
            try:
                self.preview_image.Source = BitmapImage(file_uri(result.thumb_path))
            except Exception as ex:
                self.thumb_source_text.Text = _("图片加载失败: {ex}").format(ex=ex)
                return
            self.thumb_source_text.Text = result.thumb_source
        elif result.kind == "text":
            self.preview_text.Text = result.text
            self.preview_text.Visibility = Visibility.Visible
        if result.can_load_full:
            self.load_full_btn.Visibility = Visibility.Visible
            self.thumb_source_text.Text = result.thumb_source
        if result.fits is not None and result.fits.order:
            self.fits_toggle.Visibility = Visibility.Visible
            self.fits_text.Text = "\n".join(
                f"{k:<8}= {v}" for k, v, _c in result.fits.order)

    def _on_load_full(self, sender, e) -> None:
        if self._preview_entry is None:
            return
        self.load_full_btn.Visibility = Visibility.Collapsed
        self.preview_ring.IsActive = True
        self.thumb_source_text.Text = _("正在下载原图并拉伸…")
        self._preview_token += 1
        self.shell.preview.request(self._preview_token, self._preview_entry, want_full=True)

    def _on_fits_toggle(self, sender, e) -> None:
        self._fits_visible = not self._fits_visible
        self.fits_text.Visibility = (
            Visibility.Visible if self._fits_visible else Visibility.Collapsed)

    # ---------- 打开 / 下载 ----------

    def _on_file_double_tapped(self, sender, e) -> None:
        idx = self.file_list.SelectedIndex
        if idx is None or not (0 <= idx < len(self._rendered)):
            return
        entry = self._rendered[idx]
        if entry.is_dir:
            asyncui.create_task(self._navigate(entry.share, entry.path))
            return
        # FITS 双击 = 打开查看器(看图比下载更常用);查看器没接线才退回下载
        if self._is_fits(entry) and self._open_in_viewer(entry):
            return
        self._queue_download([entry], self.download_dir)
        self.shell.info(_("已加入下载: {name} → {download_dir}").format(
            name=entry.name, download_dir=self.download_dir))

    def _queue_download(self, entries: list[RemoteEntry], target: Path) -> None:
        """下载一律以文件为排队单元:文件立即入队;目录进后台展开线程,
        递归展开为逐文件任务(每个大文件自动获得分块并行 + 监控页方块图)。"""
        target.mkdir(parents=True, exist_ok=True)
        used: set[str] = set()
        dirs: list[RemoteEntry] = []
        for e in entries:
            if e.is_dir:
                dirs.append(e)
            else:
                local = unique_local(target, e.name, used)
                self.shell.transfers.submit_download(
                    e.share, e.path, local, label=e.name, size=e.size)
        if dirs:
            threading.Thread(
                target=self._expand_dirs, args=(dirs, target),
                daemon=True, name="xfer-expand").start()

    def _expand_dirs(self, dirs: list[RemoteEntry], target: Path) -> None:
        """工作线程:把目录递归展开为逐文件下载任务。

        自持独立 clone 连接(impacket 非线程安全);submit_download 从工作线程
        调用是安全的(jobs.append 持锁,UI 通知经 shell.ui 编组);本线程内
        绝不直接碰任何 XAML,UI 更新一律 shell.ui(...) 回编组。
        """
        clone = self.shell.client.clone()
        try:
            clone.connect()
            for d in dirs:
                if self._expand_stop.is_set():
                    return
                base = target / sanitize_local_name(d.name)
                top = normalize_remote_path(d.path)  # walk 内部同样先规范化,前缀必须一致
                count = 0
                try:
                    for sub, _sub_dirs, files in clone.walk(d.share, top):
                        if self._expand_stop.is_set():
                            return
                        # 相对子路径 = 遍历路径去掉顶层前缀,各段本地名消毒
                        # (与 client.download_dir 的落盘布局保持一致)
                        rel = sub[len(top):].lstrip("\\") if top else sub
                        local_dir = base
                        if rel:
                            for part in rel.split("\\"):
                                local_dir = local_dir / sanitize_local_name(part)
                        local_dir.mkdir(parents=True, exist_ok=True)
                        for f in files:
                            self.shell.transfers.submit_download(
                                f.share, f.path,
                                local_dir / sanitize_local_name(f.name),
                                label=f.name, size=f.size, group=d.name)
                            count += 1
                            if count % 25 == 0:
                                self.shell.ui(self._set_list_status,
                                              _("展开 {name}: 已入队 {count} 个文件…").format(
                                                  name=d.name, count=count))
                    if count:
                        self.shell.ui(self._set_list_status,
                                      _("已展开 {name}: {count} 个文件任务").format(
                                          name=d.name, count=count))
                        self.shell.ui(self.shell.info,
                                      _("文件夹 {name} 已展开为 {count} 个文件任务").format(
                                          name=d.name, count=count))
                    else:
                        self.shell.ui(self.shell.info, _("目录 {name} 为空").format(
                            name=d.name))
                except SmbClientError as ex:
                    self.shell.ui(self.shell.error, _("展开目录 {name} 失败: {ex}").format(
                        name=d.name, ex=ex))
        except SmbClientError as ex:
            self.shell.ui(self.shell.error, _("展开目录失败: {ex}").format(ex=ex))
        finally:
            try:
                clone.close()
            except Exception:
                pass

    def _set_list_status(self, text: str) -> None:
        """UI 线程:更新列表底部状态文本(展开进度用)。"""
        try:
            self.list_status.Text = text
        except Exception:
            pass

    def _on_download_selected(self, sender, e) -> None:
        sel = self._selected_entries()
        if not sel:
            self.shell.info(_("先选中要下载的文件/目录"))
            return
        self._queue_download(sel, self.download_dir)
        self.shell.info(_("已加入 {0} 项下载 → {download_dir}").format(
            len(sel), download_dir=self.download_dir))

    async def _on_download_to(self, sender, e) -> None:
        sel = self._selected_entries()
        if not sel:
            self.shell.info(_("先选中要下载的文件/目录"))
            return
        folder = await self._pick_folder()
        if folder:
            self.download_dir = Path(folder)
            self._queue_download(sel, self.download_dir)
            self.shell.info(_("已加入 {0} 项下载 → {folder}").format(len(sel), folder=folder))

    # ---------- 复制到剪贴板(拖出的可靠替代) ----------

    async def _on_copy_files(self, sender, e) -> None:
        sel = self._selected_entries()
        if not sel:
            self.shell.info(_("先选中文件/目录"))
            return
        self.shell.status(_("正在下载所选项到暂存目录以便复制…"))
        session = DragOutSession(
            client_factory=lambda: self.shell.client.clone(),
            entries=sel,
            on_status=lambda text: self.shell.ui(self.shell.status, text),
        )
        # 挂到页面上,关窗(on_close)才能取消这次暂存下载;结束后置回 None
        self._drag_session = session
        try:
            ok = await asyncio.to_thread(session.done.wait, 1800)
            if session.cancel.is_set():     # 关窗等外部取消:暂存已清,静默收场
                return
            if not ok or session.error or not session.paths:
                self.shell.error(_("复制准备失败: {0}").format(session.error or _("超时")))
                return
            items = []
            for p, is_dir in session.paths:
                if is_dir:
                    it = await StorageFolder.GetFolderFromPathAsync(str(p))
                else:
                    it = await StorageFile.GetFileFromPathAsync(str(p))
                items.append(it.as_(IStorageItem))
            pack = DataPackage()
            pack.RequestedOperation = DataPackageOperation.Copy
            pack.SetStorageItems(Vector[IStorageItem](items), False)
            Clipboard.SetContent(pack)
            try:
                Clipboard.Flush()  # 让内容在 DataPackage/进程释放后仍留在剪贴板
            except Exception:
                pass
            self.shell.info(_("已复制 {0} 项到剪贴板 — 到资源管理器按 Ctrl+V 粘贴").format(len(items)))
        except Exception as ex:
            self.shell.error(_("复制到剪贴板失败: {ex}").format(ex=ex))
        finally:
            if self._drag_session is session:
                self._drag_session = None

    # ---------- 上传 ----------

    def _require_dir(self) -> bool:
        if self.share is None:
            self.shell.error(_("请先打开一个共享目录"))
            return False
        return True

    async def _on_upload_files(self, sender, e) -> None:
        if not self._require_dir():
            return
        paths = await self._pick_files()
        for p in paths:
            self._queue_upload(Path(p))
        if paths:
            self._invalidate_upload_target()
            self.shell.info(_("已加入 {0} 个文件上传").format(len(paths)))

    async def _on_upload_dir(self, sender, e) -> None:
        if not self._require_dir():
            return
        folder = await self._pick_folder()
        if folder:
            self._queue_upload(Path(folder))
            self._invalidate_upload_target()

    def _invalidate_upload_target(self) -> None:
        """上传入队后作废目标目录的索引缓存。

        上传是异步完成的,这里作废**不是**为了立刻看到新文件(那时还没传完),
        而是保证下次进这个目录时不会先闪一版"没有新文件"的旧列表 —— 直接走
        网络读一次。批量上传只调一次(每个文件一次会开出几十条线程)。
        """
        if self.share is None:
            return
        self._invalidate_dir(self.share, self.path, subtree=False)

    def _queue_upload(self, local: Path) -> None:
        if self.share is None:
            return
        if local.is_dir():
            self.shell.transfers.submit_upload_dir(local, self.share, self.path, label=local.name)
        elif local.is_file():
            rpath = f"{self.path}\\{local.name}" if self.path else local.name
            self.shell.transfers.submit_upload(local, self.share, rpath, label=local.name)

    # ---------- 拖拽 ----------

    def _on_drag_over(self, sender, e) -> None:
        try:
            if self.share is not None and e.DataView.Contains(StandardDataFormats.StorageItems):
                e.AcceptedOperation = DataPackageOperation.Copy
                try:
                    e.DragUIOverride.Caption = _("上传到 {share}/{0}").format(
                        self.path.replace(chr(92), '/'), share=self.share)
                except Exception:
                    pass
            else:
                e.AcceptedOperation = DataPackageOperation.None_
        except Exception:
            e.AcceptedOperation = DataPackageOperation.None_

    async def _on_drop(self, sender, e) -> None:
        if not self._require_dir():
            return
        deferral = e.GetDeferral()
        paths: list[str] = []
        try:
            view = e.DataView
            if view.Contains(StandardDataFormats.StorageItems):
                items = await view.GetStorageItemsAsync()
                for i in range(items.Size):
                    paths.append(items.GetAt(i).Path)
        finally:
            deferral.Complete()
        n = 0
        for p in paths:
            if p:
                self._queue_upload(Path(p))
                n += 1
        if n:
            self._invalidate_upload_target()
            self.shell.info(_("已加入 {n} 项上传 → {share}/{0}").format(
                self.path.replace(chr(92), '/'), n=n, share=self.share))

    def _on_drag_items_starting(self, sender, e) -> None:
        # 野路子(#3):取消 WinUI 自带拖拽,改用原生 OLE 虚拟文件——资源管理器落点
        # 时同步读我们的 IStream,按需从 SMB 拉字节,真正"拖拽即下载",无需预暂存。
        # 仅支持文件(目录虚拟拖出复杂,建议用「下载」);含目录则回退提示。
        sel = self._selected_entries()
        if not sel:
            e.Cancel = True
            return
        e.Cancel = True  # 交给原生 OLE
        files = [x for x in sel if not x.is_dir]
        if not files:
            self.shell.info(_("拖出暂只支持文件;目录请用「下载选中」或右键「复制到剪贴板」"))
            return
        self._start_ole_drag(files)

    def _start_ole_drag(self, entries: list[RemoteEntry]) -> None:
        """在 UI 线程发起原生 OLE 拖拽(模态阻塞至落点完成)。"""
        from astro_smb_gui.dragout_ole import build_smb_dataobject, do_drag

        try:
            obj = build_smb_dataobject(entries, lambda: self.shell.client.clone())
        except Exception as ex:
            self.shell.error(_("拖出初始化失败: {ex}(可改用右键「复制到剪贴板」)").format(ex=ex))
            return
        self.shell.status(_("拖出 {0} 个文件到资源管理器松手即开始下载…").format(len(entries)))
        try:
            do_drag(obj, None)  # 阻塞;期间资源管理器读流→按需下载
            self.shell.status(_("拖出完成"))
        except Exception as ex:
            self.shell.error(_("拖出失败: {ex}(可改用右键「复制到剪贴板」)").format(ex=ex))
        finally:
            try:
                obj.close_client()
            except Exception:
                pass

    def _on_drag_items_completed(self, sender, e) -> None:
        pass

    # ---------- 搜索 ----------

    def _on_search_key(self, sender, e) -> None:
        try:
            if e.Key == VirtualKey.Enter:
                self._on_search_click(sender, e)
        except Exception:
            pass

    def _cancel_search(self) -> None:
        if self._search_cancel is not None:
            self._search_cancel.set()
            self._search_cancel = None
        self._searching = False
        self.search_btn.Content = _("搜索")

    def _on_search_click(self, sender, e) -> None:
        if self._searching:
            self._cancel_search()
            self.list_status.Text = _("搜索已停止")
            return
        if self.share is None:
            self.shell.error(_("请先打开一个共享"))
            return
        pattern = self.search_box.Text.strip()
        if not pattern:
            return
        if "*" not in pattern and "?" not in pattern:
            pattern = f"*{pattern}*"
        self._searching = True
        self._nav_note = ""     # 搜索视图与目录索引缓存无关,别留着"已核对"的尾巴
        self.search_btn.Content = _("停止")
        cancel = threading.Event()
        self._search_cancel = cancel
        share, top = self.share, self.path
        self._render_gen += 1
        self._count_gen += 1  # 取消目录视图残留的子项计数线程
        gen = self._render_gen
        self.file_list.Items.Clear()
        self._rendered = []
        self._count_cells = {}
        self._fits_cells = {}   # 与 _render 保持一致,释放旧视图行控件引用
        # 搜索结果是流式追加,拿不到全视图夜次集合 —— 清空后按出现顺序补色号
        self._night_colors = {}
        self._search_hits = []
        self._search_prefix = f"{top}\\" if top else ""
        self.list_status.Text = _("搜索 {pattern} …").format(pattern=pattern)
        prefix = self._search_prefix

        def work() -> None:
            client = self.shell.client.clone()
            found = [0]
            try:
                client.connect()
                batch: list[RemoteEntry] = []

                def flush() -> None:
                    if batch:
                        chunk = list(batch)
                        batch.clear()
                        self.shell.ui(self._append_search_results, gen, chunk, prefix)

                for entry in client.find(
                    share, top, pattern, include_dirs=True,
                    cancel=cancel, limit=RENDER_CAP, on_error=lambda p, err: None,
                ):
                    batch.append(entry)
                    found[0] += 1
                    if len(batch) >= 25:
                        flush()
                flush()
            except SmbClientError as ex:
                self.shell.ui(self.shell.error, _("搜索失败: {ex}").format(ex=ex))
            finally:
                client.close()
                if not cancel.is_set():
                    self.shell.ui(self._finish_search, gen, found[0])

        threading.Thread(target=work, daemon=True, name="search").start()

    def _append_search_results(self, gen: int, chunk: list[RemoteEntry], prefix: str) -> None:
        if gen != self._render_gen:
            return
        for entry in chunk:
            rel = entry.path
            if prefix and rel.startswith(prefix):
                rel = rel[len(prefix):]
            self.file_list.Items.Append(
                self._make_row(entry, name_override=rel.replace("\\", "/")))
            self._rendered.append(entry)
            if self._search_hits is not None:
                self._search_hits.append(entry)
        self.list_status.Text = _("搜索中… 已找到 {0}").format(len(self._rendered))

    def _finish_search(self, gen: int, count: int) -> None:
        if gen != self._render_gen:
            return
        self._searching = False
        self.search_btn.Content = _("搜索")
        cap = _("(达到 {RENDER_CAP} 上限)").format(
            RENDER_CAP=RENDER_CAP) if count >= RENDER_CAP else ""
        self.list_status.Text = _("搜索完成: {count} 项{cap} — 双击目录进入,选中可下载/拖出").format(
            count=count, cap=cap)
        self._start_child_counts([e for e in self._rendered if e.is_dir])
        # 搜索结果同样懒加载 FITS 副行(否则注册了单元格却永不填充)
        self._start_fits_meta([e for e in self._rendered if not e.is_dir])

    # ---------- 占用分析(#5) ----------

    def _on_analyze(self, sender, e) -> None:
        if self.share is None:
            self.shell.error(_("请先打开一个共享"))
            return
        self.shell.space.load_path(self.share, self.path)
        self.shell.select_page("space")

    # ---------- 目录操作 ----------

    async def _on_new_dir(self, sender, e) -> None:
        if not self._require_dir():
            return
        name = await asyncio.to_thread(_ask_text, _("新建目录"), _("目录名:"))
        if not name:
            return
        rpath = f"{self.path}\\{name}" if self.path else name
        try:
            await asyncio.to_thread(self.shell.client.makedirs, self.share, rpath)
        except SmbClientError as ex:
            self.shell.error(str(ex))
            return
        # 目录结构变了:本目录索引 + 该共享的占用树全部作废
        self._invalidate_dir(self.share, self.path, subtree=False)
        await self._navigate(self.share, self.path, force=True)

    async def _on_rename(self, sender, e) -> None:
        sel = self._selected_entries()
        if len(sel) != 1:
            self.shell.info(_("请选中单个文件/目录重命名"))
            return
        entry = sel[0]
        new_name = await asyncio.to_thread(
            _ask_text, _("重命名"), _("{name} 改为:").format(name=entry.name), entry.name)
        if not new_name or new_name == entry.name:
            return
        parent = ntpath.dirname(entry.path)
        new_path = f"{parent}\\{new_name}" if parent else new_name
        try:
            await asyncio.to_thread(self.shell.client.rename, entry.share, entry.path, new_path)
        except SmbClientError as ex:
            self.shell.error(str(ex))
            return
        # 改名后旧路径(及其整棵子树)的索引键全部失效,所在目录也要重读
        self._invalidate_dir(entry.share, entry.path, subtree=True)
        self._invalidate_dir(self.share, self.path, subtree=False)
        await self._navigate(self.share, self.path, force=True)

    async def _on_delete(self, sender, e) -> None:
        sel = self._selected_entries()
        if not sel:
            return
        ndir = sum(1 for x in sel if x.is_dir)
        names = _("、").join(x.name for x in sel[:6]) + ("…" if len(sel) > 6 else "")
        detail = _("共 {0} 项").format(len(sel))
        if ndir:
            detail += _("(含 {ndir} 个目录,将递归删除)").format(ndir=ndir)
        ok = await self.shell.confirm(
            _("确认删除"), _('即将永久删除以下内容,不可恢复:\n\n{names}\n\n{detail}').format(
                names=names, detail=detail),
            ok_text=_("删除"))
        if not ok:
            return
        self.shell.busy(True)
        errors = []
        for entry in sel:
            try:
                if entry.is_dir:
                    await asyncio.to_thread(
                        self.shell.client.rmdir, entry.share, entry.path, True)
                else:
                    await asyncio.to_thread(
                        self.shell.client.remove, entry.share, entry.path)
            except SmbClientError as ex:
                errors.append(str(ex))
            else:
                # 删掉的目录连同其整棵子树的索引全部作废
                self._invalidate_dir(entry.share, entry.path, subtree=entry.is_dir)
        self._invalidate_dir(self.share, self.path, subtree=False)
        self.shell.busy(False)
        if errors:
            self.shell.error(_("删除时出错: {0}").format(errors[0]))
        else:
            self.shell.info(_("已删除 {0} 项").format(len(sel)))
        await self._navigate(self.share, self.path, force=True)

    def _on_copy_path(self, sender, e) -> None:
        sel = self._selected_entries()
        if not sel:
            return
        lines = [
            f"\\\\{self.shell.client.host}\\{x.share}" + (f"\\{x.path}" if x.path else "")
            for x in sel
        ]
        pack = DataPackage()
        pack.SetText("\n".join(lines))
        Clipboard.SetContent(pack)
        self.shell.info(_("已复制 {0} 条路径").format(len(lines)))

    # ---------- 文件选择器 ----------

    async def _pick_files(self) -> list[str]:
        from win32more.Windows.Win32.UI.Shell import IInitializeWithWindow
        try:
            from win32more.Windows.Storage.Pickers import FileOpenPicker

            picker = FileOpenPicker()
            picker.FileTypeFilter.Append("*")
            picker.as_(IInitializeWithWindow).Initialize(self.shell.hwnd())
            files = await picker.PickMultipleFilesAsync()
            return [files.GetAt(i).Path for i in range(files.Size)] if files else []
        except Exception:
            return await asyncio.to_thread(_tk_pick_files)

    async def _pick_folder(self) -> str | None:
        from win32more.Windows.Win32.UI.Shell import IInitializeWithWindow
        try:
            from win32more.Windows.Storage.Pickers import FolderPicker

            picker = FolderPicker()
            picker.FileTypeFilter.Append("*")
            picker.as_(IInitializeWithWindow).Initialize(self.shell.hwnd())
            folder = await picker.PickSingleFolderAsync()
            return folder.Path if folder else None
        except Exception:
            return await asyncio.to_thread(_tk_pick_folder)


# ---------- 模块级工具 ----------

def XamlReader_load(path: Path):
    from win32more.Microsoft.UI.Xaml.Markup import XamlReader
    return XamlReader.Load(_xaml_text(path)).as_(FrameworkElement)


def _ask_text(title: str, prompt: str, initial: str = "") -> str | None:
    import tkinter as tk
    from tkinter import simpledialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        return simpledialog.askstring(title, prompt, initialvalue=initial, parent=root)
    finally:
        root.destroy()


def _tk_pick_files() -> list[str]:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        return list(filedialog.askopenfilenames(title=_("选择要上传的文件")))
    finally:
        root.destroy()


def _tk_pick_folder() -> str | None:
    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        return filedialog.askdirectory(title=_("选择目录")) or None
    finally:
        root.destroy()
