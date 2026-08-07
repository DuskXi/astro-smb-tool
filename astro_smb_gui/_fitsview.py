"""FITS 影像查看器页:读原始像素 → 超像素去马赛克 → 可调拉伸 → 缩放/读数/直方图。

对标 Siril / ZWO FITS viewer 的观感,但只用 numpy + PIL + WinUI3
(数值全部在 :mod:`astro_smb.fitsimage` 里,这里只负责编排与画面)。

线程模型(**改之前先读 docs/DEVELOPMENT.md §6.2**):

* **加载线程**(每次打开一张图起一条):自持 ``client.clone()`` 连接,
  读头 → 下载到缓存(``.fvpart`` 原子落盘,带进度/取消)→ 解码 + 去马赛克,
  结果经 ``shell.ui`` 编组回 UI;本地盘后端直接读原文件,不复制;
* **渲染线程**(常驻一条,只保留最新请求):拉伸 + 写位图 + 算直方图。
  拉伸走 :func:`astro_smb.fitsimage.stretch` 的 LUT 通路,全分辨率约几十毫秒,
  所以滑杆能实时跟手 —— **线性阵列一直留在内存,改参数只重跑拉伸,不重读文件**;
* UI 线程只做:控件读写、双缓冲翻面、Canvas 画直方图、坐标换算。

画面更新用 **双缓冲**(两张 ``Image`` 叠放,背面 ``ImageOpened`` 后才翻面),
否则每次拖滑杆都会闪一下白底(``_records`` 的天球遮罩同款做法)。
"""

from __future__ import annotations

import asyncio
import math
import os
import subprocess
import threading
import time
from pathlib import Path

import numpy as np

from win32more.Microsoft.UI.Text import FontWeights
from win32more.Microsoft.UI.Xaml import (
    FrameworkElement,
    GridLength,
    GridUnitType,
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
    Orientation,
    ProgressBar,
    ProgressRing,
    RowDefinition,
    ScrollViewer,
    Slider,
    StackPanel,
    TextBlock,
    ToggleSwitch,
    ToolTipService,
)
from win32more.Microsoft.UI.Xaml.Controls.Primitives import ToggleButton
from win32more.Microsoft.UI.Xaml.Markup import XamlReader
from win32more.Microsoft.UI.Xaml.Media import FontFamily, PointCollection
from win32more.Microsoft.UI.Xaml.Media.Imaging import BitmapImage
from win32more.Microsoft.UI.Xaml.Shapes import Line, Polyline, Rectangle
from win32more.Windows.Foundation import Point, PropertyValue

from astro_smb import fitsimage as fi
from astro_smb.client import RemoteEntry, SmbClientError
from astro_smb.util import human_size
from astro_smb import astro, catalog, platesolve
from astro_smb.i18n import gettext as _
from astro_smb_gui import _common

from astro_smb_gui._browser import (
    _GRP_CAMERA,
    _NIGHT_PALETTE,
    _TONE_RGB,
    _astro_details,
    _brush,
    _corner,
    _detail_text,
)
from astro_smb_gui._common import file_uri
from astro_smb_gui.logstore import load_site
from astro_smb_gui.preview import (
    cache_dir,
    cache_key,
    clear_cache,
    download_cached,
    read_fits_header,
)

# 视图模型已下沉到 astro_smb_app.views.fitsview —— 新前端渲染同一份 BMP
# 并走 ResourceRef 传引用(二进制永不进 JSON)。
XAML_PATH = Path(__file__).with_name("fitsview.xaml")

from astro_smb_app.views.fitsview import (  # noqa: F401
    MARK_FILL,
    _CH_RGB,
    _HIST_BINS,
    _HIST_POINTS,
    _KEEP_RENDERS,
    _MODES,
    _PROGRESS_TICK,
    _RENDER_BUDGET,
    _RENDER_DEBOUNCE,
    _downsample_peak,
    _matched_to_display,
    _prune_renders,
    _render_dir,
)
from astro_smb_gui._xamli18n import load_text as _xaml_text





class _RenderResult:
    """渲染线程 → UI 线程的一趟结果(纯数据,不含任何 XAML 对象)。"""

    __slots__ = ("gen", "seq", "path", "stats", "hist", "error")

    def __init__(self, gen, seq, path=None, stats=None, hist=None, error=""):
        self.gen, self.seq = gen, seq
        self.path, self.stats, self.hist, self.error = path, stats, hist, error


class FitsViewPage:
    """契约与其它页一致:``__init__(shell)`` / ``root`` / ``on_show()`` /
    ``on_connected(shares)`` / ``on_close()``。

    对外入口 :meth:`open_path`(share, path)—— shell 的 ``open_fitsview`` 转发到它。
    """

    def __init__(self, shell):
        self.shell = shell
        self.root = XamlReader.Load(_xaml_text(XAML_PATH)).as_(FrameworkElement)

        # ---- 状态 ----
        self._gen = 0                       # 加载代次(过期结果一律丢弃)
        self._render_seq = 0                # 渲染代次
        self._cancel: threading.Event | None = None
        self._entry: RemoteEntry | None = None
        self._hdr = None
        self._img: fi.LinearImage | None = None
        # 拖拽平移中: (起点 x, 起点 y, 起始 H 偏移, 起始 V 偏移),均为视口坐标
        self._pan_from: tuple[float, float, float, float] | None = None
        self._hist_before: list[np.ndarray] | None = None
        self._hist_after: list[np.ndarray] | None = None
        # 曲线数据的版本号:_draw_hist 靠它判断「要不要重建点集」。
        # 拉伸前的曲线整张图期间根本不变,而每调一格滑杆都会换一次拉伸后的曲线。
        self._hist_before_ver = 0
        self._hist_after_ver = 0
        self._hist_key: tuple | None = None     # 已画好的 (档位, 版本, 画布尺寸)
        self._hist_marks: list[tuple] = []      # 持久化的 3 条标记线 + 标签
        self._lin_hist: list[np.ndarray] | None = None   # 65536 格线性直方图
        self._stats: list[fi.ChannelStats] | None = None
        self._params = fi.StretchParams()
        self._copy_text = ""
        self._want_fit = False
        self._ui_updating = False
        self._night_colors: dict[str, int] = {}
        self._last_progress = 0.0
        self._auto_done = False

        # 双缓冲记账
        self._front = None
        self._back = None
        self._back_busy = False
        self._back_path = ""
        self._shown_path = ""
        self._src_path = ""      # 本地源 FITS(解算用)
        # 板解算状态
        self._solve_gen = 0
        self._solve_cancel: threading.Event | None = None
        self._solve_res = None              # platesolve.SolveResult
        self._solve_stars = None            # stars.StarList(用于叠加标记)
        self._pending_path: str | None = None

        # 渲染线程(常驻,只保留最新请求)
        self._rlock = threading.Condition()
        self._rpending = None
        self._rstop = False
        self._rthread: threading.Thread | None = None

        self._find()
        self._wire()

        # 画刷/字体建一次复用(渲染函数里绝不新建 SolidColorBrush)
        self._badge_brushes = {
            "light":   (_brush(0xDD, 0xEF, 0xDD), _brush(0x1B, 0x5E, 0x20)),
            "bias":    (_brush(0xE9, 0xE9, 0xE9), _brush(0x45, 0x45, 0x45)),
            "dark":    (_brush(0xD3, 0xD3, 0xDC), _brush(0x2A, 0x2A, 0x38)),
            "flat":    (_brush(0xD9, 0xE7, 0xF8), _brush(0x0D, 0x47, 0xA1)),
            "preview": (_brush(0xE4, 0xDD, 0xF2), _brush(0x4A, 0x33, 0x82)),
            "filter":  (_brush(0xFB, 0xEA, 0xC5), _brush(0x7A, 0x52, 0x00)),
            "bin":     (_brush(0xDF, 0xE9, 0xEC), _brush(0x24, 0x50, 0x60)),
            "seq":     (_brush(0xE6, 0xE6, 0xE6), _brush(0x50, 0x50, 0x50)),
        }
        self._night_brushes = [(_brush(*bg), _brush(*fg)) for bg, fg in _NIGHT_PALETTE]
        self._tone_brushes = {k: _brush(*rgb) for k, rgb in _TONE_RGB.items()}
        self._divider = _brush(0x80, 0x80, 0x80, 0x3C)
        self._pill_bg = _brush(0x80, 0x80, 0x80, 0x28)
        self._track_bg = _brush(0x80, 0x80, 0x80, 0x38)
        self._track_tick = _brush(0x80, 0x80, 0x80, 0x78)
        self._mono_font = FontFamily("Consolas")
        self._ch_brushes = [_brush(*c) for c in _CH_RGB]
        self._grid_brush = _brush(0x80, 0x80, 0x80, 0x40)
        self._mark_c0 = _brush(0xD0, 0x8A, 0x00)
        self._mark_m2 = _brush(0x3F, 0xA9, 0x55)
        self._mark_med = _brush(0x90, 0x90, 0x90)

        self._debounce = self._make_timer(_RENDER_DEBOUNCE, self._on_debounce_tick)
        self._sync_mode_panels()

    # ------------------------------------------------------------ 控件

    def _find(self) -> None:
        f = self.root.FindName
        self.title_text = f("TitleText").as_(TextBlock)
        self.fit_btn = f("FitBtn").as_(Button)
        self.one_btn = f("OneToOneBtn").as_(Button)
        self.zoom_out_btn = f("ZoomOutBtn").as_(Button)
        self.zoom_in_btn = f("ZoomInBtn").as_(Button)
        self.zoom_text = f("ZoomText").as_(TextBlock)
        self.reveal_btn = f("RevealBtn").as_(Button)
        self.reload_btn = f("ReloadBtn").as_(Button)

        self.info_card = f("InfoCard").as_(Border)
        self.info_title = f("InfoTitle").as_(TextBlock)
        self.info_sub = f("InfoSub").as_(TextBlock)
        self.info_badges = f("InfoBadges").as_(StackPanel)
        self.info_pills = f("InfoPills").as_(StackPanel)

        self.side_panel = f("SidePanel").as_(ScrollViewer)
        self.image_scroll = f("ImageScroll").as_(ScrollViewer)
        self.image_host = f("ImageHost").as_(Grid)
        self.image_a = f("ImageA").as_(Image)
        self.image_b = f("ImageB").as_(Image)
        self.busy_panel = f("BusyPanel").as_(StackPanel)
        self.busy_ring = f("BusyRing").as_(ProgressRing)
        self.busy_text = f("BusyText").as_(TextBlock)
        self.busy_bar = f("BusyBar").as_(ProgressBar)
        self.cancel_btn = f("CancelBtn").as_(Button)
        self.empty_text = f("EmptyText").as_(TextBlock)

        self.mode_box = f("ModeBox").as_(ComboBox)
        self.linked_toggle = f("LinkedToggle").as_(ToggleSwitch)
        self.stf_panel = f("StfPanel").as_(StackPanel)
        self.shadow_label = f("ShadowLabel").as_(TextBlock)
        self.shadow_slider = f("ShadowSlider").as_(Slider)
        self.target_label = f("TargetLabel").as_(TextBlock)
        self.target_slider = f("TargetSlider").as_(Slider)
        self.asinh_panel = f("AsinhPanel").as_(StackPanel)
        self.asinh_label = f("AsinhLabel").as_(TextBlock)
        self.asinh_slider = f("AsinhSlider").as_(Slider)
        self.pct_panel = f("PctPanel").as_(StackPanel)
        self.lo_label = f("LoLabel").as_(TextBlock)
        self.lo_slider = f("LoSlider").as_(Slider)
        self.hi_label = f("HiLabel").as_(TextBlock)
        self.hi_slider = f("HiSlider").as_(Slider)
        self.reset_btn = f("ResetBtn").as_(Button)
        self.save_btn = f("SaveBtn").as_(Button)
        self.solve_btn = f("SolveBtn").as_(Button)
        self.star_layer = f("StarLayer").as_(Canvas)
        self.solve_card = f("SolveCard").as_(Border)
        self.solve_status = f("SolveStatus").as_(TextBlock)
        self.solve_grid = f("SolveGrid").as_(Grid)
        self.star_toggle = f("StarToggle").as_(ToggleButton)
        self.solve_cancel_btn = f("SolveCancelBtn").as_(Button)
        self.catalog_download_btn = f("CatalogDownloadBtn").as_(Button)

        self.hist_toggle = f("HistToggle").as_(ToggleButton)
        self.hist_canvas = f("HistCanvas").as_(Canvas)
        self.hist_hint = f("HistHint").as_(TextBlock)

        self.astro_card = f("AstroCard").as_(Border)
        self.astro_grid = f("AstroGrid").as_(Grid)
        self.image_card = f("ImageCard").as_(Border)
        self.image_grid = f("ImageGrid").as_(Grid)
        self.copy_btn = f("CopyBtn").as_(Button)
        self.hdr_toggle = f("HdrToggle").as_(ToggleButton)
        self.hdr_text = f("HdrText").as_(TextBlock)

        self.readout = f("ReadoutText").as_(TextBlock)
        self.status_text = f("StatusText").as_(TextBlock)

        self._front, self._back = self.image_a, self.image_b

    def _wire(self) -> None:
        # 缩放上限调到 16×(默认 10)看单像素。
        # **不要动 MinZoomFactor**:WinUI3 有硬下限 0.1,设更小直接抛
        # "The MinZoomFactor property cannot be set to a value smaller than 0.1"
        # (真机实测,XAML 里写 0.02 会让整个 XamlReader.Load 失败)。
        # 0.1 对本项目够用:6248×4176 超像素后 3124×2088,适应窗口约 0.3×。
        try:
            self.image_scroll.MaxZoomFactor = 16.0
        except Exception:
            pass
        self.fit_btn.Click += self._on_fit
        self.one_btn.Click += self._on_one_to_one
        self.zoom_in_btn.Click += lambda s, e: self._zoom_step(1.25)
        self.zoom_out_btn.Click += lambda s, e: self._zoom_step(1.0 / 1.25)
        self.reveal_btn.Click += self._on_reveal
        self.reload_btn.Click += self._on_reload
        self.cancel_btn.Click += self._on_cancel

        self.image_scroll.ViewChanged += self._on_view_changed
        self.image_scroll.SizeChanged += self._on_scroll_size
        self.image_host.PointerMoved += self._on_pointer_moved
        self.image_host.PointerExited += self._on_pointer_exited
        self.image_host.PointerWheelChanged += self._on_wheel
        self.image_host.PointerPressed += self._on_pointer_pressed
        self.image_host.PointerReleased += self._end_pan
        self.image_host.PointerCaptureLost += self._end_pan
        for img in (self.image_a, self.image_b):
            img.ImageOpened += self._on_image_opened
            img.ImageFailed += self._on_image_failed

        self.mode_box.SelectionChanged += self._on_mode_changed
        self.linked_toggle.Toggled += self._on_param_changed
        for sl in (self.shadow_slider, self.target_slider, self.asinh_slider,
                   self.lo_slider, self.hi_slider):
            sl.ValueChanged += self._on_slider_changed
        self.reset_btn.Click += self._on_reset
        self.save_btn.Click += self._on_save
        self.solve_btn.Click += self._on_solve
        self.solve_cancel_btn.Click += self._on_solve_cancel
        self.catalog_download_btn.Click += self._on_catalog_download
        self.star_toggle.Click += self._on_star_toggle
        self.hist_toggle.Click += lambda s, e: self._draw_hist()
        self.copy_btn.Click += self._on_copy
        self.hdr_toggle.Click += self._on_hdr_toggle

    def _make_timer(self, seconds: float, handler):
        """单次 DispatcherQueueTimer;不可用时返回 None(调用处有兜底)。"""
        try:
            from win32more.Windows.Foundation import TimeSpan
            t = self.shell.dispatcher.CreateTimer()
            span = TimeSpan()
            span.Duration = int(seconds * 1e7)      # TimeSpan 单位 = 100ns
            t.Interval = span
            t.IsRepeating = False
            t.Tick += handler
            return t
        except Exception:
            return None

    # ------------------------------------------------------------ 生命周期

    def on_show(self) -> None:
        if self._img is not None and self._want_fit:
            self._fit_to_window()
        # 无人值守截图钩子:ASTRO_SMB_GUI_FITSVIEW="EMMC Images/.../x.fit"
        if not self._auto_done:
            self._auto_done = True
            target = os.environ.get("ASTRO_SMB_GUI_FITSVIEW", "").strip()
            if target:
                try:
                    from astro_smb.client import split_remote_path
                    share, path = split_remote_path(target)
                    self.open_path(share, path)
                except Exception as ex:
                    self.shell.error(_("ASTRO_SMB_GUI_FITSVIEW 无法打开: {ex}").format(ex=ex))

    def on_connected(self, shares) -> None:
        # 换设备/重连:当前图属于旧设备,直接清空(路径可能在新设备上不存在)
        self._gen += 1
        if self._cancel is not None:
            self._cancel.set()
        self._clear()

    def on_close(self) -> None:
        self._gen += 1
        if self._cancel is not None:
            self._cancel.set()
        with self._rlock:
            self._rstop = True
            self._rpending = None
            self._rlock.notify()

    # ------------------------------------------------------------ 对外入口

    def open_path(self, share: str, path: str) -> None:
        """打开共享内的一个 FITS 文件(UI 线程调用)。"""
        if not share or not path:
            self.shell.error(_("FITS 查看器:路径为空"))
            return
        self._start_load(share, path)

    def open_entry(self, entry: RemoteEntry) -> None:
        self.open_path(entry.share, entry.path)

    # ------------------------------------------------------------ 加载

    def _clear(self) -> None:
        self._img = None
        self._entry = None
        self._hdr = None
        self._hist_before = self._hist_after = None
        self._hist_before_ver += 1
        self._hist_after_ver += 1
        self._hist_key = None
        self._hist_marks = []
        self._lin_hist = None
        self._stats = None
        self._shown_path = ""
        self._pending_path = None
        self._back_busy = False
        # 渲染线程的待办里还抓着上一张图(几十 MB),不清就一直不释放
        with self._rlock:
            self._rpending = None
        for img in (self.image_a, self.image_b):
            try:
                img.put_Source(None)
            except Exception:
                pass
        self.image_a.Opacity, self.image_b.Opacity = 1.0, 0.0
        self._front, self._back = self.image_a, self.image_b
        self.title_text.Text = _("(未打开文件)")
        self.info_card.Visibility = Visibility.Collapsed
        self.astro_card.Visibility = Visibility.Collapsed
        self.image_card.Visibility = Visibility.Collapsed
        self.copy_btn.Visibility = Visibility.Collapsed
        self.hdr_toggle.Visibility = Visibility.Collapsed
        self.hdr_text.Visibility = Visibility.Collapsed
        self.empty_text.Visibility = Visibility.Visible
        self.reveal_btn.IsEnabled = False
        self.reload_btn.IsEnabled = False
        self.save_btn.IsEnabled = False
        self.solve_btn.IsEnabled = False
        self._reset_solve()
        self.readout.Text = ""
        self.zoom_text.Text = "—"
        self.hist_canvas.Children.Clear()
        self.hist_hint.Text = ""
        self._set_busy(False, "")

    def _set_busy(self, on: bool, text: str, percent: float | None = None) -> None:
        self.busy_panel.Visibility = Visibility.Visible if on else Visibility.Collapsed
        self.busy_ring.IsActive = on
        self.busy_text.Text = text
        self.cancel_btn.Visibility = (
            Visibility.Visible if on else Visibility.Collapsed)
        if percent is None:
            self.busy_bar.Visibility = Visibility.Collapsed
        else:
            self.busy_bar.Visibility = Visibility.Visible
            self.busy_bar.Value = max(0.0, min(100.0, percent))

    def _start_load(self, share: str, path: str) -> None:
        self._gen += 1
        gen = self._gen
        if self._cancel is not None:
            self._cancel.set()
        cancel = self._cancel = threading.Event()
        self._clear()
        self._share, self._path = share, path
        name = path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        self.title_text.Text = name
        self.empty_text.Visibility = Visibility.Collapsed
        self._set_busy(True, _("正在读取 FITS 头…"))
        self.status_text.Text = f"{share}\\{path}"
        base = self.shell.client
        self._last_progress = 0.0
        threading.Thread(target=self._load_worker,
                         args=(gen, base, share, path, cancel),
                         daemon=True, name="fitsview-load").start()

    def _load_worker(self, gen, base, share, path, cancel) -> None:
        """工作线程:自持 clone 连接读头 + 取像素;绝不碰任何 XAML。"""
        client = None
        try:
            client = base.clone()
            client.connect()
            entry = client.stat(share, path)
            if entry.is_dir:
                raise SmbClientError(_("{path} 是目录").format(path=path))
            hdr = read_fits_header(client, entry)
            geom = fi.geometry_from_header(hdr)     # 早失败早报错,别白下 50MB
            self.shell.ui(self._on_phase, gen,
                          _("正在获取像素数据({0})…").format(human_size(geom.data_bytes)))
            src = self._local_source(client, entry, cancel, gen)
            self.shell.ui(self._set_src_path, gen, str(src))
            if cancel.is_set():
                return
            self.shell.ui(self._on_phase, gen, _("正在解码 / 去马赛克…"))
            img = fi.load_linear(src, hdr, cancel=cancel)
            if cancel.is_set():
                return
            hist = [fi.histogram_unit(img.sample[:, c], _HIST_BINS)
                    for c in range(img.sample.shape[1])]
            lin = self._linear_hists(img, cancel)
            self.shell.ui(self._apply_loaded, gen, entry, hdr, img, hist, lin)
        except fi.FitsImageError as ex:
            self.shell.ui(self._load_failed, gen, str(ex))
        except SmbClientError as ex:
            self.shell.ui(self._load_failed, gen, str(ex))
        except Exception as ex:      # 绝不静默:async/线程里的异常会被吞掉
            self.shell.ui(self._load_failed, gen, f"{type(ex).__name__}: {ex}")
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    @staticmethod
    def _linear_hists(img: fi.LinearImage, cancel) -> list[np.ndarray] | None:
        """装载时算一次 65536 格线性直方图,之后每次调参的「拉伸后直方图」
        就只是 ``bincount(LUT, weights=这张表)``(256 项运算)。

        以前每次调参都在**全分辨率 uint8 位图**上跑三遍 ``np.bincount``:
        ``rgb8[:,:,c]`` 是 stride=3 的非连续视图,reshape 先复制,bincount 再把
        uint8 提升成 intp —— 单色 26M 像素实测 261ms、瞬时 +209MB,是整条流水线
        最大的一次分配,而它只为得到 3×256 个显示用计数。
        只有整数 uint16 通路能这么推(浮点没有有限量化格点),否则返回 None,
        渲染线程回落抽样统计。
        """
        if img.rgb.dtype != np.uint16 or not img.unit.is_u16:
            return None
        out = []
        for c in range(img.channels):
            if cancel is not None and cancel.is_set():
                return None
            out.append(fi.linear_hist_u16(img.rgb[:, :, c]))
        return out

    def _local_source(self, client, entry: RemoteEntry, cancel, gen):
        """拿到可以 ``np.fromfile`` 的本地路径。

        本地盘后端直接用卡上的原文件(50MB 白拷一份纯属浪费);SMB 走缓存
        下载(``.fvpart`` 临时后缀,避开 PreviewWorker 的 ``.part``,两边同时
        拉同一张图不会互相 ``os.replace`` 撞车)。
        """
        if getattr(client, "is_local", False):
            try:
                p = Path(client._resolve(entry.share, entry.path))
                if p.is_file():
                    return p
            except Exception:
                pass        # 私有解析失败就老老实实复制一份
        dest = cache_dir() / f"{cache_key(getattr(client, 'host', ''), entry)}.fit"

        def on_progress(done: int, total: int) -> None:
            now = time.monotonic()
            if now - self._last_progress < _PROGRESS_TICK and done < total:
                return
            self._last_progress = now
            pct = (done * 100.0 / total) if total else 0.0
            self.shell.ui(self._on_phase, gen,
                          _("正在下载 {0} / {1}").format(
                              human_size(done), human_size(total)), pct)

        # 查看器的核心用法就是双击翻图,每张在 cache 顶层留一份 49.8MB 原图;
        # clear_cache 只在应用启动时跑一次,而本项目的 GUI 是常开设计
        # (watcher/心跳都常驻)—— 一晚翻 60 张 = 3GB 永远不裁。
        # 这里按 atime 顺手裁一次(幂等)。两个讲究:
        #  * **裁在下载之前**(且仅当 dest 还不存在):绝不可能把马上要读的那份删掉;
        #  * **drop_dragout=False**:运行中那个目录里可能正有一次拖拽在读文件。
        if not dest.exists():
            try:
                clear_cache(drop_dragout=False)
            except OSError:
                pass
        download_cached(client, entry.share, entry.path, dest, cancel,
                        progress=on_progress, tmp_suffix=".fvpart")
        return dest

    def _on_phase(self, gen: int, text: str, percent: float | None = None) -> None:
        if gen != self._gen:
            return
        self._set_busy(True, text, percent)

    def _load_failed(self, gen: int, msg: str) -> None:
        if gen != self._gen:
            return
        self._set_busy(False, "")
        self.empty_text.Text = _("打开失败: {msg}").format(msg=msg)
        self.empty_text.Visibility = Visibility.Visible
        self.reload_btn.IsEnabled = True
        self.shell.error(_("FITS 查看器: {msg}").format(msg=msg))

    def _set_src_path(self, gen: int, path: str) -> None:
        """记住这张图的本地源文件 —— 解算直接在它上面跑,不重下。"""
        if gen == self._gen:
            self._src_path = path

    def _apply_loaded(self, gen, entry, hdr, img, hist, lin=None) -> None:
        if gen != self._gen:
            return
        self._entry, self._hdr, self._img = entry, hdr, img
        self._hist_before = hist
        self._hist_before_ver += 1
        self._hist_after = None
        self._hist_after_ver += 1
        self._hist_key = None
        self._lin_hist = lin
        self._set_busy(False, "")
        self.empty_text.Visibility = Visibility.Collapsed
        self.reveal_btn.IsEnabled = True
        self.reload_btn.IsEnabled = True
        self.save_btn.IsEnabled = True
        self.solve_btn.IsEnabled = True

        w, h = float(img.width), float(img.height)
        self.image_host.Width, self.image_host.Height = w, h
        for el in (self.image_a, self.image_b):
            el.Width, el.Height = w, h

        self._build_cards(entry, hdr, img)
        self._want_fit = True
        self._fit_to_window()
        self._request_render(immediate=True)

    # ------------------------------------------------------------ 渲染

    def _params_from_ui(self) -> fi.StretchParams:
        idx = self.mode_box.SelectedIndex
        mode = _MODES[idx] if isinstance(idx, int) and 0 <= idx < len(_MODES) else "stf"
        return fi.StretchParams(
            mode=mode,
            shadows_clipping=float(self.shadow_slider.Value),
            target_background=float(self.target_slider.Value),
            linked=bool(self.linked_toggle.IsOn),
            asinh_a=10.0 ** float(self.asinh_slider.Value),
            lo_pct=float(self.lo_slider.Value),
            hi_pct=float(self.hi_slider.Value),
        )

    def _request_render(self, *, immediate: bool = False) -> None:
        if self._img is None:
            return
        if not immediate and self._debounce is not None:
            try:
                self._debounce.Stop()
                self._debounce.Start()
                return
            except Exception:
                pass
        self._submit_render()

    def _on_debounce_tick(self, sender, e) -> None:
        self._submit_render()

    def _submit_render(self) -> None:
        img = self._img
        if img is None:
            return
        self._params = self._params_from_ui()
        self._render_seq += 1
        key = cache_key(getattr(self.shell.client, "host", ""), self._entry,
                        "fitsview") if self._entry is not None else "adhoc"
        with self._rlock:
            self._rpending = (self._gen, self._render_seq, img, self._params, key,
                              self._lin_hist)
            if self._rthread is None:
                self._rstop = False
                self._rthread = threading.Thread(target=self._render_worker,
                                                 daemon=True, name="fitsview-render")
                self._rthread.start()
            self._rlock.notify()

    def _render_worker(self) -> None:
        """常驻渲染线程:只做最新一次请求(中间的参数变化直接丢弃)。

        **轮体单独抽成 :meth:`_render_once`**:循环局部变量(img/rgb8/hist…)
        在空闲 ``wait()`` 期间会一直强引用上一张图 —— 用户按了"清空"、
        甚至换了文件,那 91~130MB 也不会释放,打开第二张时峰值直接翻倍。
        栈帧随函数返回销毁,再把 job/res 显式置 None,空闲时这条线程零持有。
        """
        while True:
            with self._rlock:
                while self._rpending is None and not self._rstop:
                    self._rlock.wait()
                if self._rstop:
                    return
                job = self._rpending
                self._rpending = None
            res = self._render_once(job)
            job = None                      # 先松开对 LinearImage 的引用再回编组
            try:
                self.shell.ui(self._apply_render, res)
            except Exception:
                pass        # 编组失败只丢一帧;让线程死掉会把之后所有渲染一起弄没
            res = None

    def _render_once(self, job) -> _RenderResult:
        """一次拉伸 + 落盘 + 直方图(工作线程;绝不碰 XAML)。"""
        from PIL import Image as PILImage

        gen, seq = (job[0], job[1]) if job else (-1, -1)
        try:
            _g, _s, img, params, key, lin = job
            stats = fi.compute_stats(img.sample, params)
            # mono_out:单色图只算一遍 LUT、位图也只存一个通道
            # (6248×4176 时 78MB → 26MB,落盘和拉伸都降到 1/3)
            rgb8, stats3 = fi.stretch(img.rgb, params, unit=img.unit, stats=stats,
                                      mono_out=True)
            hist = self._hist_after_for(rgb8, params, stats3, lin)
            out = _render_dir() / f"{key}_{params.fingerprint()}.bmp"
            if not out.exists():
                tmp = out.with_name(out.name + ".part")
                try:
                    # BMP 落盘约 0.02s(同尺寸 PNG 要 0.3~0.5s),
                    # 交互式重渲染必须用它,否则拖滑杆明显卡顿。
                    # 单通道存 mode="L"(8bpp 灰度 BMP,WinUI3 的 BitmapImage
                    # 能直接解码 —— 探针实证)
                    PILImage.fromarray(
                        rgb8, mode="L" if rgb8.ndim == 2 else "RGB").save(
                            tmp, format="BMP")
                    os.replace(tmp, out)
                except BaseException:
                    try:
                        tmp.unlink(missing_ok=True)
                    except OSError:
                        pass
                    raise
            _prune_renders()
            return _RenderResult(gen, seq, str(out), stats3, hist)
        except Exception as ex:
            return _RenderResult(gen, seq, error=f"{type(ex).__name__}: {ex}")

    @staticmethod
    def _hist_after_for(rgb8, params, stats3, lin) -> list[np.ndarray]:
        """拉伸后的 256 格直方图。

        有线性直方图(整数通路)就**由 LUT 反推**,一次调参只花几毫秒、几乎不分配;
        没有(浮点 FITS)才回落到位图上抽样 bincount —— 抽样是 ``[::4, ::4]``,
        显示用直方图会按峰值归一化,抽稀不影响观感,但能把 209MB 的瞬时分配
        砍掉 16 倍。
        """
        if lin:
            n = len(lin)
            return [fi.hist_after_from_lut(fi.transfer_lut(params, stats3[c]),
                                           lin[min(c, n - 1)]) for c in range(3)]
        a = np.asarray(rgb8)
        if a.ndim == 2:
            small = np.ascontiguousarray(a[::4, ::4]).reshape(-1)
            return [np.bincount(small, minlength=256)] * 3
        return [np.bincount(np.ascontiguousarray(a[::4, ::4, c]).reshape(-1),
                            minlength=256) for c in range(3)]

    def _apply_render(self, res: _RenderResult) -> None:
        if res.gen != self._gen or res.seq != self._render_seq:
            return          # 过期结果(用户已经又动了参数/换了文件)
        if res.error:
            self.status_text.Text = _("渲染失败: {error}").format(error=res.error)
            self.shell.error(_("FITS 拉伸失败: {error}").format(error=res.error))
            return
        self._stats = res.stats
        self._hist_after = res.hist
        self._hist_after_ver += 1
        self._show_image(res.path)
        self._draw_hist()
        self.status_text.Text = self._stretch_summary()

    def _stretch_summary(self) -> str:
        p, st = self._params, self._stats
        if st is None:
            return ""
        if p.mode == "stf":
            head = (_("STF · 阴影 {shadows_clipping:.2f}σ · 目标背景 {target_background:.3f} · ").format(
                shadows_clipping=p.shadows_clipping, target_background=p.target_background)
                    + (_("链接") if p.linked else _("不链接")))
        elif p.mode == "asinh":
            head = f"asinh · a = {p.asinh_a:.0f}"
        else:
            head = _("百分位 · {0:.2f}% ~ {1:.2f}%").format(
                min(p.lo_pct, p.hi_pct), max(p.lo_pct, p.hi_pct))
        return head

    # ---- 双缓冲翻面 ----

    def _show_image(self, path: str) -> None:
        if not path or path == self._shown_path:
            return          # 已解码位图重赋值是 no-op,永不触发 ImageOpened
        if self._back_busy:
            self._pending_path = path
            return
        self._back_busy = True
        self._back_path = path
        try:
            self._back.Source = BitmapImage(file_uri(path))
        except Exception as ex:
            self._back_busy = False
            self.status_text.Text = _("位图加载失败: {ex}").format(ex=ex)

    def _on_image_opened(self, sender, e) -> None:
        # 只有背面会被赋 Source,所以靠 _back_busy 记账甄别即可
        # (sender 是另一个 ComPtr 包装,和 self._back 比 `is` 永远为假)
        if not self._back_busy:
            return
        self._back.Opacity = 1.0
        self._front.Opacity = 0.0
        self._front, self._back = self._back, self._front
        self._shown_path = self._back_path
        self._back_busy = False
        nxt, self._pending_path = self._pending_path, None
        if nxt:
            self._show_image(nxt)

    def _on_image_failed(self, sender, e) -> None:
        if not self._back_busy:
            return
        self._back_busy = False     # 不清账状态机会永久卡住
        self.status_text.Text = _("位图解码失败")
        # 排队中的那一帧是**更新**的(用户又调了一格),而且通常是好的 ——
        # 直接丢掉会让画面永久停在更早一帧,直到用户再动一次控件。
        # 接力逻辑与 _on_image_opened 完全一致。
        nxt, self._pending_path = self._pending_path, None
        if nxt:
            self._show_image(nxt)

    # ------------------------------------------------------------ 缩放 / 平移

    def _zoom_now(self) -> float:
        try:
            z = float(self.image_scroll.ZoomFactor)
        except Exception:
            z = 1.0
        return z if z > 0 else 1.0

    def _set_zoom(self, z: float, center: tuple[float, float] | None = None) -> None:
        sv = self.image_scroll
        try:
            zmin = float(sv.MinZoomFactor) or 0.02
            zmax = float(sv.MaxZoomFactor) or 24.0
        except Exception:
            zmin, zmax = 0.02, 24.0
        z = max(zmin, min(zmax, float(z)))
        cur = self._zoom_now()
        try:
            vw, vh = float(sv.ViewportWidth), float(sv.ViewportHeight)
            px, py = center if center else (vw / 2.0, vh / 2.0)
            cx = float(sv.HorizontalOffset) + px
            cy = float(sv.VerticalOffset) + py
            ratio = z / cur
            nh, nv = max(0.0, cx * ratio - px), max(0.0, cy * ratio - py)
        except Exception:
            nh = nv = 0.0
        try:
            # ChangeView 的第三个参数是 IReference[Single]:裸 float 会被
            # box 成 Double 再 QI,必炸 E_NOINTERFACE(真机踩过),
            # 必须显式 CreateSingle
            sv.ChangeView(nh, nv, PropertyValue.CreateSingle(float(z)))
        except Exception:
            try:
                sv.ChangeView(nh, nv, None)
            except Exception:
                pass
        self._update_zoom_text()

    def _zoom_step(self, factor: float) -> None:
        if self._img is None:
            return
        self._want_fit = False
        self._set_zoom(self._zoom_now() * factor)

    def _fit_to_window(self) -> None:
        sv, img = self.image_scroll, self._img
        if img is None:
            return
        vw, vh = float(sv.ViewportWidth or 0.0), float(sv.ViewportHeight or 0.0)
        if vw < 20.0 or vh < 20.0:
            self._want_fit = True        # 还没布局,等 SizeChanged 再来
            return
        self._want_fit = False
        self._set_zoom(min(vw / max(1.0, img.width), vh / max(1.0, img.height)))

    def _on_fit(self, sender, e) -> None:
        self._fit_to_window()

    def _on_one_to_one(self, sender, e) -> None:
        self._want_fit = False
        self._set_zoom(1.0)

    def _on_scroll_size(self, sender, e) -> None:
        if self._want_fit:
            self._fit_to_window()

    def _on_view_changed(self, sender, args) -> None:
        self._update_zoom_text()

    def _update_zoom_text(self) -> None:
        if self._img is None:
            self.zoom_text.Text = "—"
            return
        self.zoom_text.Text = f"{self._zoom_now() * 100.0:.0f}%"

    def _on_wheel(self, sender, e) -> None:
        """滚轮 = 以指针为锚点缩放(看图工具的通行手势,不是滚动)。"""
        if self._img is None:
            return
        try:
            pp = e.GetCurrentPoint(self.image_scroll)
            delta = int(pp.Properties.MouseWheelDelta)
            px, py = float(pp.Position.X), float(pp.Position.Y)
        except Exception:
            return
        if not delta:
            return
        self._want_fit = False
        self._set_zoom(self._zoom_now() * (1.25 if delta > 0 else 1.0 / 1.25),
                       center=(px, py))
        try:
            e.Handled = True
        except Exception:
            pass

    # ------------------------------------------------------------ 像素读数

    # ---------- 拖拽平移 ----------
    #
    # WinUI3 的 ScrollViewer 只对触摸/触控笔自带惯性平移, **鼠标拖拽要自己实现**
    # (真机反馈: 试过 Ctrl/Alt/中键/左键都拖不动)。做法: 按下时捕获指针并记下
    # 起点与当时的偏移, 移动时按增量 ChangeView。增量必须在**视口坐标系**里算
    # (GetCurrentPoint(image_scroll)), 不能用 image_host —— 后者会随缩放一起变换,
    # 拖动过程中偏移一改, 同一个屏幕位置换算回去就变了, 会自激抖动。

    def _on_pointer_pressed(self, sender, e) -> None:
        try:
            props = e.GetCurrentPoint(self.image_scroll).Properties
            if not (props.IsLeftButtonPressed or props.IsMiddleButtonPressed):
                return
            pos = e.GetCurrentPoint(self.image_scroll).Position
            self._pan_from = (float(pos.X), float(pos.Y),
                              float(self.image_scroll.HorizontalOffset),
                              float(self.image_scroll.VerticalOffset))
            self.image_host.CapturePointer(e.Pointer)
            e.Handled = True
        except Exception:
            self._pan_from = None

    def _end_pan(self, sender, e) -> None:
        if self._pan_from is not None:
            self._pan_from = None
            try:
                self.image_host.ReleasePointerCapture(e.Pointer)
            except Exception:
                pass

    def _on_pointer_moved(self, sender, e) -> None:
        if self._pan_from is not None:
            try:
                pos = e.GetCurrentPoint(self.image_scroll).Position
                x0, y0, h0, v0 = self._pan_from
                # 反向: 光标往右拖 = 内容往右走 = 偏移变小
                self.image_scroll.ChangeViewWithOptionalAnimation(
                    max(0.0, h0 - (float(pos.X) - x0)),
                    max(0.0, v0 - (float(pos.Y) - y0)), None, True)
            except Exception:
                self._pan_from = None
            return                          # 拖拽中不刷读数(位置在动, 没意义)
        img = self._img
        if img is None:
            return
        try:
            pos = e.GetCurrentPoint(self.image_host).Position
            x, y = int(pos.X), int(pos.Y)
        except Exception:
            return
        if not (0 <= x < img.width and 0 <= y < img.height):
            self.readout.Text = ""
            return
        self.readout.Text = self._readout_text(img, x, y)

    def _on_pointer_exited(self, sender, e) -> None:
        if self._pan_from is None:           # 拖出边界不该中断拖拽(已捕获指针)
            self.readout.Text = ""

    def _readout_text(self, img: fi.LinearImage, x: int, y: int) -> str:
        """(x, y) + 该点**原始 ADU**(线性,不是显示值)+ 去马赛克后的 RGB。"""
        rx, ry = img.raw_at(x, y)
        raw = img.raw
        parts = [f"({x:>5}, {y:>5})"]
        try:
            if raw.ndim == 2:
                adu = f"ADU {self._fmt_val(raw[ry, rx])}"
                pat = img.geom.bayer_effective
                if pat:
                    adu += f" [{pat[(ry % 2) * 2 + (rx % 2)]}]"
                parts.append(adu)
            else:
                parts.append("ADU " + "/".join(
                    self._fmt_val(v) for v in raw[ry, rx]))
            px = img.rgb[y, x]
            if img.channels >= 3:
                parts.append("RGB " + "/".join(self._fmt_val(v) for v in px[:3]))
            else:
                parts.append(_("灰度 ") + self._fmt_val(px[0]))
        except (IndexError, ValueError):
            return f"({x}, {y})"
        return "  ".join(parts)

    @staticmethod
    def _fmt_val(v) -> str:
        f = float(v)
        return f"{int(f):>5}" if float(f).is_integer() else f"{f:.4g}"

    # ------------------------------------------------------------ 拉伸面板

    def _sync_mode_panels(self) -> None:
        idx = self.mode_box.SelectedIndex
        mode = _MODES[idx] if isinstance(idx, int) and 0 <= idx < len(_MODES) else "stf"
        self.stf_panel.Visibility = (
            Visibility.Visible if mode == "stf" else Visibility.Collapsed)
        self.asinh_panel.Visibility = (
            Visibility.Visible if mode == "asinh" else Visibility.Collapsed)
        self.pct_panel.Visibility = (
            Visibility.Visible if mode == "percentile" else Visibility.Collapsed)
        # asinh 是逐点函数,无所谓通道统计,链接开关对它没意义
        self.linked_toggle.IsEnabled = (mode != "asinh")
        self._sync_labels()

    def _sync_labels(self) -> None:
        self.shadow_label.Text = _("阴影裁切 {Value:.2f} σ").format(
            Value=self.shadow_slider.Value)
        self.target_label.Text = _("目标背景 {Value:.3f}").format(
            Value=self.target_slider.Value)
        self.asinh_label.Text = _("asinh 强度 a = {0:.0f}").format(
            10.0 ** self.asinh_slider.Value)
        self.lo_label.Text = _("下限 {Value:.2f}%").format(Value=self.lo_slider.Value)
        self.hi_label.Text = _("上限 {Value:.2f}%").format(Value=self.hi_slider.Value)

    def _on_mode_changed(self, sender, e) -> None:
        if self._ui_updating:
            return
        self._sync_mode_panels()
        self._request_render(immediate=True)

    def _on_param_changed(self, sender, e) -> None:
        if self._ui_updating:
            return
        self._request_render(immediate=True)

    def _on_slider_changed(self, sender, e) -> None:
        if self._ui_updating:
            return
        self._sync_labels()
        self._request_render()

    def _on_reset(self, sender, e) -> None:
        d = fi.StretchParams()
        self._ui_updating = True
        try:
            self.mode_box.SelectedIndex = 0
            self.linked_toggle.IsOn = d.linked
            self.shadow_slider.Value = d.shadows_clipping
            self.target_slider.Value = d.target_background
            self.asinh_slider.Value = math.log10(d.asinh_a)
            self.lo_slider.Value = d.lo_pct
            self.hi_slider.Value = d.hi_pct
        finally:
            self._ui_updating = False
        self._sync_mode_panels()
        self._request_render(immediate=True)

    def _on_cancel(self, sender, e) -> None:
        if self._cancel is not None:
            self._cancel.set()
        self._gen += 1
        self._set_busy(False, "")
        self.empty_text.Text = _("已取消")
        self.empty_text.Visibility = Visibility.Visible
        self.reload_btn.IsEnabled = True

    def _on_reload(self, sender, e) -> None:
        share = getattr(self, "_share", None)
        path = getattr(self, "_path", None)
        if share and path:
            self.open_path(share, path)

    def _on_reveal(self, sender, e) -> None:
        entry = self._entry
        if entry is None:
            return
        parent = entry.path.rsplit("\\", 1)[0] if "\\" in entry.path else ""
        try:
            self.shell.open_browser_path(entry.share, parent)
        except Exception as ex:
            self.shell.error(_("跳转浏览页失败: {ex}").format(ex=ex))

    # ---------------------------------------------------------------- 板解算

    def _reset_solve(self) -> None:
        self._solve_gen += 1
        if self._solve_cancel is not None:
            self._solve_cancel.set()
        self._solve_cancel = None
        self._solve_res = None
        self._solve_stars = None
        self.solve_card.Visibility = Visibility.Collapsed
        self.star_toggle.Visibility = Visibility.Collapsed
        self.star_toggle.IsChecked = False
        self.solve_cancel_btn.Visibility = Visibility.Collapsed
        self.catalog_download_btn.Visibility = Visibility.Collapsed
        self.solve_grid.Children.Clear()
        self.solve_grid.RowDefinitions.Clear()
        self.star_layer.Children.Clear()

    def _on_solve_cancel(self, sender, e) -> None:
        if self._solve_cancel is not None:
            self._solve_cancel.set()
        self.solve_status.Text = _("正在取消 …")

    def _on_solve(self, sender, e) -> None:
        """从当前这张图解出绝对天坐标。**重活全在工作线程**,可取消。"""
        if self._entry is None or not self._src_path:
            return
        if not catalog.catalog_available():
            self._offer_catalog()
            return
        self._reset_solve()
        self._solve_gen += 1
        gen = self._solve_gen
        cancel = self._solve_cancel = threading.Event()
        self.solve_card.Visibility = Visibility.Visible
        self.solve_cancel_btn.Visibility = Visibility.Visible
        self.solve_btn.IsEnabled = False
        self.solve_status.Text = _("正在解算 …")
        src, hdr, name = self._src_path, self._hdr, self._entry.name

        def work() -> None:
            try:
                hint = platesolve.SolveHint.from_header(hdr, name)
                res = platesolve.solve_file(
                    src, hint=hint, name=name, cancel=cancel,
                    progress=lambda stage, frac:
                        self.shell.ui(self._solve_progress, gen, stage, frac))
                self.shell.ui(self._solve_done, gen, res, None)
            except InterruptedError:
                self.shell.ui(self._solve_cancelled, gen)
            except Exception as ex:      # 工作线程异常绝不静默(§11)
                self.shell.ui(self._solve_failed, gen,
                              f"{type(ex).__name__}: {ex}")

        threading.Thread(target=work, daemon=True, name="fitsview-solve").start()

    def _solve_progress(self, gen: int, text: str, frac: float = 0.0) -> None:
        if gen == self._solve_gen:
            pct = max(0.0, min(100.0, float(frac) * 100.0))
            self.solve_status.Text = f"{text} {pct:.0f}%"

    def _solve_cancelled(self, gen: int) -> None:
        if gen != self._solve_gen:
            return
        self._solve_cancel = None
        self.solve_cancel_btn.Visibility = Visibility.Collapsed
        self.solve_btn.IsEnabled = True
        self.solve_status.Text = _("解算已取消")

    def _solve_failed(self, gen: int, msg: str) -> None:
        if gen != self._solve_gen:
            return
        self.solve_cancel_btn.Visibility = Visibility.Collapsed
        self.solve_btn.IsEnabled = True
        self.solve_status.Text = _("解算失败: {msg}").format(msg=msg)

    def _solve_done(self, gen: int, res, star_list) -> None:
        if gen != self._solve_gen:
            return                       # 过期结果(用户又换了图/又点了一次)
        self._solve_cancel = None
        self.solve_cancel_btn.Visibility = Visibility.Collapsed
        self.solve_btn.IsEnabled = True
        self._solve_res, self._solve_stars = res, star_list
        if not getattr(res, "ok", False):
            self.solve_status.Text = (
                _("未能解算: {0}(图上星点 {1} 颗)").format(
                    getattr(res, 'message', '') or getattr(res, 'reason', ''), getattr(res, 'n_stars', 0)))
            return
        self.solve_status.Text = (
            _("解算成功 · 匹配 {n_match} 对 · 用时 {elapsed_s:.1f}s").format(
                n_match=res.n_match, elapsed_s=res.elapsed_s))
        self._fill_solve_rows(res)
        if star_list is not None or res.matched_xy is not None:
            self.star_toggle.Visibility = Visibility.Visible

    def _fill_solve_rows(self, res) -> None:
        self.solve_grid.Children.Clear()
        self.solve_grid.RowDefinitions.Clear()
        rows: list[tuple] = []
        c = res.center
        if c:
            rows.append((_("中心"), f"{astro.format_ra(c[0])}  {astro.format_dec(c[1])}"))
        if res.pixel_scale:
            rows.append((_("像元比例"), f"{res.pixel_scale:.3f}″/px"))
        if res.zwo_angle_deg is not None:
            rows.append((_("旋转角"), _("{zwo_angle_deg:.2f}°(ZWO 约定)").format(
                zwo_angle_deg=res.zwo_angle_deg)))
        if res.wcs is not None and self._img is not None:
            fw, fh = res.wcs.fov_deg(self._img.width, self._img.height)
            rows.append((_("视场"), f"{fw:.2f}° × {fh:.2f}°"))
            rows.append((_("镜像"), _("是") if res.wcs.flipped() else _("否")))
        rows.append((_("匹配 / 星点"), f"{res.n_match} / {res.n_stars}"))
        if res.rms_px == res.rms_px:        # 非 NaN
            # **口径**必须说清:它只统计收紧容差内的内点,是"中心区域拟合得多好",
            # 不是这张图的畸变大小,更不是成功判据(判成功看匹配数与 log_fap)
            rows.append((_("拟合残差"), _("{rms_px:.2f} px(中心区内点)").format(rms_px=res.rms_px)))
        if res.hint_offset_deg == res.hint_offset_deg:
            rows.append((_("离先验中心"), f"{res.hint_offset_deg * 60:.1f}′"))
        if res.star_fwhm_px == res.star_fwhm_px:
            value = f"{res.star_fwhm_px:.2f} px"
            if res.star_fwhm_arcsec == res.star_fwhm_arcsec:
                value += f" / {res.star_fwhm_arcsec:.2f}″"
            rows.append((_("星点 FWHM"), value))
        if res.star_ellipticity == res.star_ellipticity:
            rows.append((_("星点椭圆率"), f"{res.star_ellipticity:.3f}"))
        if res.star_theta_deg == res.star_theta_deg:
            rows.append((_("拉伸方向"), _("{star_theta_deg:.1f}°(集中度 {star_theta_r:.2f})").format(
                star_theta_deg=res.star_theta_deg, star_theta_r=res.star_theta_r)))
        self._add_pairs(self.solve_grid, 0, rows)

    def _on_star_toggle(self, sender, e) -> None:
        if not self.star_toggle.IsChecked:
            self.star_layer.Children.Clear()
            return
        self._draw_stars()

    def _draw_stars(self) -> None:
        """把匹配上的星标在图上。**走 _common 的批量片段** —— 逐个 new
        几百个矩形要好几百毫秒(单价 1.7~2.1ms/个)。"""
        self.star_layer.Children.Clear()
        res, img = self._solve_res, self._img
        if res is None or img is None or res.matched_xy is None:
            return
        # 显示图是超像素后的尺寸,而星点坐标在**原始像素**上 —— 按比例缩放
        # 显示图是超像素后的尺寸(OSC 时是原始的一半),而星点坐标在
        # **原始像素**上 —— 按 raw 与 rgb 的实际比例缩放,别假设是 2
        rh, rw = int(img.raw.shape[0]), int(img.raw.shape[1])
        size = 9.0
        marks = []
        flip = bool(getattr(img.geom, "flip_vertical", True))
        display = _matched_to_display(
            res.matched_xy, rw, rh, img.width, img.height, flip)
        for cx, cy in display:
            marks.append((cx - size / 2, cy - size / 2, size, size, MARK_FILL))
        try:
            frag = _common.rect_fragment(marks)
            if frag:
                el = XamlReader.Load(frag).as_(Canvas)
                self.star_layer.Children.Append(el)
        except Exception:
            return
        self.solve_status.Text = (
            _("解算成功 · 匹配 {n_match} 对 · 已在图上标出").format(n_match=res.n_match))

    def _offer_catalog(self) -> None:
        """星表还没下载:说清楚要下什么、多大,由用户决定。"""
        self.solve_card.Visibility = Visibility.Visible
        self.solve_cancel_btn.Visibility = Visibility.Collapsed
        self.catalog_download_btn.Visibility = Visibility.Visible
        self.solve_status.Text = (
            _("解算需要 Tycho-2 星表。首次使用会从 CDS(星表的权威发布方)取原始数据并在本机构建,约 159 MB、构建几秒;之后常驻本机,不再联网。下载后本机常驻,之后解算不再联网。"))
        self.solve_grid.Children.Clear()
        self.solve_grid.RowDefinitions.Clear()
        self._add_pairs(self.solve_grid, 0,
                        [(_("状态"), _("星表未就绪")), (_("来源"), "Tycho-2(CDS/VizieR)")])

    def _on_catalog_download(self, sender, e) -> None:
        """按用户明确动作下载星表；完成后自动继续刚才的板解算。"""
        if self._entry is None or not self._src_path:
            return
        self._reset_solve()
        self._solve_gen += 1
        gen = self._solve_gen
        cancel = self._solve_cancel = threading.Event()
        self.solve_card.Visibility = Visibility.Visible
        self.solve_cancel_btn.Visibility = Visibility.Visible
        self.catalog_download_btn.Visibility = Visibility.Collapsed
        self.solve_btn.IsEnabled = False
        self.solve_status.Text = _("正在获取星表 …")

        def work() -> None:
            try:
                catalog.ensure_catalog(
                    cancel=cancel,
                    progress=lambda done, total:
                        self.shell.ui(self._catalog_progress, gen, done, total))
                self.shell.ui(self._catalog_done, gen)
            except InterruptedError:
                self.shell.ui(self._solve_cancelled, gen)
            except Exception as ex:
                self.shell.ui(
                    self._solve_failed, gen,
                    _("星表获取失败: {__name__}: {ex}").format(__name__=type(ex).__name__, ex=ex))

        threading.Thread(target=work, daemon=True,
                         name="fitsview-catalog").start()

    def _catalog_progress(self, gen: int, done: int, total: int) -> None:
        if gen != self._solve_gen:
            return
        if total > 0:
            self.solve_status.Text = (
                _("正在获取星表 {0:.0f}/{1:.0f} MB").format(done / (1 << 20), total / (1 << 20)))
        else:
            self.solve_status.Text = _("正在获取星表 {0:.0f} MB").format(done / (1 << 20))

    def _catalog_done(self, gen: int) -> None:
        if gen != self._solve_gen:
            return
        self._solve_cancel = None
        self.solve_cancel_btn.Visibility = Visibility.Collapsed
        self.solve_btn.IsEnabled = True
        self._on_solve(None, None)

    def _on_save(self, sender, e) -> None:
        if not self._shown_path or self._entry is None:
            return
        src, name = self._shown_path, self._entry.name
        target = Path.home() / "Downloads" / "Astro SMB Tool"

        async def run():
            try:
                out = await asyncio.to_thread(_save_png, src, target, name)
                self.shell.info(_("已保存拉伸后的 PNG: {out}").format(out=out),
                                _("打开所在文件夹"), lambda: _reveal(out))
            except Exception as ex:
                self.shell.error(_("保存 PNG 失败: {ex}").format(ex=ex))

        from win32more import asyncui
        asyncui.create_task(run())

    def _on_hdr_toggle(self, sender, e) -> None:
        vis = self.hdr_text.Visibility != Visibility.Visible
        self.hdr_text.Visibility = Visibility.Visible if vis else Visibility.Collapsed

    def _on_copy(self, sender, e) -> None:
        if not self._copy_text:
            return
        try:
            from win32more.Windows.ApplicationModel.DataTransfer import (
                Clipboard, DataPackage)
            pack = DataPackage()
            pack.SetText(self._copy_text)
            Clipboard.SetContent(pack)
            try:
                Clipboard.Flush()
            except Exception:
                pass
            self.shell.info(_("已复制详情({0} 行)").format(self._copy_text.count(chr(10)) + 1))
        except Exception as ex:
            self.shell.error(_("复制详情失败: {ex}").format(ex=ex))

    # ------------------------------------------------------------ 直方图

    def _draw_hist(self) -> None:
        """曲线与标记**分离**:曲线只在数据真变了才重建,标记每次原地移动。

        以前每次 ``_apply_render`` 都无条件全量重画:768 次
        ``PointCollection.Append`` 跨 COM 就占 52ms(约 67µs/次),整趟 50~96ms,
        每调一格滑杆冻结 4 帧以上 —— 而默认的「拉伸前」档曲线整张图期间根本不变,
        这些开销全白花。做法与 ``_records`` 的天球遮罩一致:框架画一次,
        元素持久化只 SetLeft/SetTop 原地移动,绝不 Children.Clear。
        """
        c = self.hist_canvas
        after = bool(self.hist_toggle.IsChecked)
        self.hist_toggle.Content = _("拉伸后") if after else _("拉伸前")
        data = self._hist_after if after else self._hist_before
        ver = self._hist_after_ver if after else self._hist_before_ver
        if not data:
            c.Children.Clear()
            self._hist_key = None
            self._hist_marks = []
            self.hist_hint.Text = ""
            return
        w = float(c.Width or 292.0)
        h = float(c.Height or 130.0)
        ml, mb, mt = 4.0, 12.0, 4.0
        pw, ph = w - ml * 2.0, h - mb - mt
        if pw < 20.0 or ph < 20.0:
            return

        key = (after, ver, round(w, 1), round(h, 1))
        if key != self._hist_key:
            self._build_hist_frame(c, data, after, ml, mt, pw, ph)
            self._hist_key = key
        self._place_hist_marks(after, ml, mt, pw, ph, w)

    def _build_hist_frame(self, c: Canvas, data, after: bool,
                          ml: float, mt: float, pw: float, ph: float) -> None:
        """重建网格线 + 三条曲线 + 三组标记元素(只在曲线数据/尺寸变了时调用)。"""
        c.Children.Clear()
        self._hist_marks = []

        # 拉伸前用对数纵轴(线性数据 99% 的像素挤在最左边,线性纵轴什么都看不见)
        curves = []
        peak = 1.0
        for arr in data:
            y = _downsample_peak(np.asarray(arr, dtype=np.float64), _HIST_POINTS)
            if not after:
                y = np.log1p(y)
            peak = max(peak, float(y.max()) if y.size else 1.0)
            curves.append(y)

        for k in (0.25, 0.5, 0.75):
            self._line(c, ml + pw * k, mt, ml + pw * k, mt + ph, self._grid_brush, 1.0)

        for i, y in enumerate(curves):
            brush = self._ch_brushes[i] if len(curves) >= 3 else self._mark_med
            pl = Polyline()
            pl.Stroke = brush
            pl.StrokeThickness = 1.2
            col = pl.Points
            if col is None:
                col = PointCollection()
                pl.Points = col
            n = len(y)
            # 256 格下采样到约 96 点再 Append:曲线在 292px 宽的画布上看不出差别,
            # 但跨 COM 的调用次数少 2/3
            for j in range(n):
                col.Append(Point(X=ml + pw * j / max(1, n - 1),
                                 Y=mt + ph - ph * float(y[j]) / peak))
            c.Children.Append(pl)

        for _i in range(3):
            ln = Line()
            ln.StrokeThickness = 1.0
            ln.IsHitTestVisible = False
            c.Children.Append(ln)
            tb = TextBlock()
            tb.FontSize = 9
            tb.Opacity = 0.85
            tb.IsHitTestVisible = False
            c.Children.Append(tb)
            self._hist_marks.append((ln, tb))

    def _place_hist_marks(self, after: bool, ml: float, mt: float,
                          pw: float, ph: float, w: float) -> None:
        """把 c0/m2/中位数三条标记线挪到位(不新建任何元素)。"""
        st = self._stats
        marks: list[tuple] = []
        if st and not after:
            med = sum(s.median for s in st) / len(st)
            if self._params.mode == "percentile":
                marks = [(sum(s.lo for s in st) / len(st), self._mark_c0, _("低")),
                         (sum(s.hi for s in st) / len(st), self._mark_m2, _("高")),
                         (med, self._mark_med, _("中"))]
            else:
                c0 = sum(s.c0 for s in st) / len(st)
                m2 = sum(s.m2 for s in st) / len(st)
                marks = [(c0, self._mark_c0, "c0"),
                         (c0 + m2 * (1.0 - c0), self._mark_m2, "m2"),
                         (med, self._mark_med, _("中"))]
            self.hist_hint.Text = (
                _("拉伸前(对数纵轴)· 中位数 {med:.4f}").format(med=med)
                + (f" · c0 {marks[0][0]:.4f} · m2 {st[0].m2:.3f}"
                   if self._params.mode != "percentile" else ""))
        elif after:
            self.hist_hint.Text = _("拉伸后(线性纵轴,0~255)")
        else:
            self.hist_hint.Text = _("拉伸前(对数纵轴)")

        for i, (ln, tb) in enumerate(self._hist_marks):
            if i >= len(marks):
                ln.Visibility = Visibility.Collapsed
                tb.Visibility = Visibility.Collapsed
                continue
            xv, brush, label = marks[i]
            x = ml + pw * max(0.0, min(1.0, float(xv)))
            ln.X1 = ln.X2 = x
            ln.Y1, ln.Y2 = mt, mt + ph
            ln.Stroke = brush
            ln.Visibility = Visibility.Visible
            tb.Text = label
            tb.Foreground = brush
            tb.Visibility = Visibility.Visible
            # 线性天区图里 c0/中位数/m2 全挤在最左边一小段,标签**竖向错开**
            # 才认得出谁是谁(横向再怎么挪都会叠在一起)
            Canvas.SetLeft(tb, min(w - 18.0, x + 2.0))
            Canvas.SetTop(tb, mt + 1.0 + i * 12.0)

    def _line(self, canvas: Canvas, x1, y1, x2, y2, brush, thickness=1.0) -> None:
        ln = Line()
        ln.X1, ln.Y1, ln.X2, ln.Y2 = float(x1), float(y1), float(x2), float(y2)
        ln.Stroke = brush
        ln.StrokeThickness = float(thickness)
        ln.IsHitTestVisible = False
        canvas.Children.Append(ln)

    # ------------------------------------------------------------ 信息卡片

    def _site_latlon(self) -> tuple[float, float] | None:
        """站点 (纬度, 经度):纬度取本地配置,经度优先日志推算值。"""
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

    def _build_cards(self, entry, hdr, img: fi.LinearImage) -> None:
        site = self._site_latlon()
        title, sub, groups, badges, _sky, pills = _astro_details(entry, hdr, site)
        if title is not None:
            self.info_title.Text = title
            self.info_sub.Text = sub
            self.info_sub.Visibility = (
                Visibility.Visible if sub else Visibility.Collapsed)
            self._render_badges(badges)
            self._render_pills(pills)
            self._fill_groups(self.astro_grid, groups)
            self.info_card.Visibility = Visibility.Visible
            self.astro_card.Visibility = Visibility.Visible
        else:
            self.info_card.Visibility = Visibility.Collapsed
            self.astro_card.Visibility = Visibility.Collapsed
            groups, badges, pills = [], [], []

        g = img.geom
        pairs: list[tuple] = [
            (_("原始尺寸"), f"{g.width} × {g.height}"
             + (f" × {g.planes}" if g.planes > 1 else "")),
            (_("位深"), f"BITPIX {g.bitpix}"
             + (_(" (有符号整数)") if g.bitpix == 16 else "")),
            (_("刻度"), f"BSCALE {g.bscale:g} · BZERO {g.bzero:g}",
             _("(还原成 0~65535)") if g.bzero == 32768.0 else ""),
            # 副注要短:右栏只有 330px,长单词(ROWORDER=BOTTOM-UP)断不了行会被裁掉
            (_("行序"), (_("自底向上 (已翻转)") if g.flip_vertical else _("自顶向下 (原样)")),
             _("(缺 ROWORDER 卡)") if not hdr.get("ROWORDER") else ""),
        ]
        if g.bayer_raw:
            note = ""
            if g.bayer_offset != (0, 0):
                note = _("(偏移 {0},{1})").format(g.bayer_offset[0], g.bayer_offset[1])
            pairs.append((_("Bayer 相位"), f"{g.bayer_raw} → {g.bayer_effective}",
                          note or _("(翻转后的实际相位)"), True))
        pairs.append((_("显示尺寸"), f"{img.width} × {img.height}"
                      + (_(" (超像素去马赛克)") if img.debayered else "")))
        pairs.append((_("通道"), _("RGB 彩色") if img.channels >= 3 else _("单色")))
        pairs.append((_("数据区"), _("偏移 {data_offset:,} · {0}").format(
            human_size(g.data_bytes), data_offset=g.data_offset)))
        fgroups = [(_GRP_CAMERA, _("影像结构"), pairs)]
        self._fill_groups(self.image_grid, fgroups)
        self.image_card.Visibility = Visibility.Visible

        self.copy_btn.Visibility = Visibility.Visible
        self._copy_text = _detail_text(entry, title, sub, badges, pills,
                                       list(groups) + fgroups)

        if hdr is not None and hdr.order:
            self.hdr_toggle.Visibility = Visibility.Visible
            self.hdr_text.Text = "\n".join(f"{k:<8}= {v}" for k, v, _c in hdr.order)
        else:
            self.hdr_toggle.Visibility = Visibility.Collapsed

    # ---- 排版原语(与浏览页详情卡同一套设计语言;各页各持一份是本项目惯例) ----

    def _add_row(self, grid: Grid) -> None:
        rd = RowDefinition()
        rd.Height = GridLength(Value=1.0, GridUnitType=GridUnitType.Auto)
        grid.RowDefinitions.Append(rd)

    def _fill_groups(self, grid: Grid, groups: list[tuple]) -> None:
        """(图标, 组名, 键值对) 分区填充;空组自动跳过。"""
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
        self._add_row(grid)
        head = Grid()
        for width, unit in ((1.0, GridUnitType.Auto), (1.0, GridUnitType.Auto),
                            (1.0, GridUnitType.Star)):
            col = ColumnDefinition()
            col.Width = GridLength(Value=width, GridUnitType=unit)
            head.ColumnDefinitions.Append(col)
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

    def _add_pairs(self, grid: Grid, row: int, pairs: list[tuple]) -> int:
        """(标签, 值[, 副注[, 等宽[, 语义色[, 小组件]]]]) 逐行填进两列 Grid。"""
        for item in pairs:
            k, v = item[0], item[1]
            note = item[2] if len(item) > 2 else ""
            mono = item[3] if len(item) > 3 else False
            tone = item[4] if len(item) > 4 else None
            widget = item[5] if len(item) > 5 else None
            self._add_row(grid)
            lab = TextBlock()
            lab.Text = str(k)
            lab.FontSize = 12
            lab.Opacity = 0.55
            grid.Children.Append(lab)
            Grid.SetRow(lab, row)
            Grid.SetColumn(lab, 0)
            val = TextBlock()
            val.Text = str(v)
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
                    aux.Text = str(note)
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

    def _make_gadget(self, spec, tone: str | None):
        """行内小组件:目前只有 ("altbar", 高度角)。"""
        if not spec:
            return None
        try:
            if spec[0] == "altbar":
                return self._alt_bar(float(spec[1]), tone)
        except (TypeError, ValueError, IndexError):
            return None
        return None

    def _alt_bar(self, alt_deg: float, tone: str | None) -> Canvas:
        w, h = 120.0, 26.0
        track_y, track_h = 8.0, 6.0
        canvas = Canvas()
        canvas.Width, canvas.Height = w, h
        canvas.VerticalAlignment = VerticalAlignment.Center
        if alt_deg <= 0.0:
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

        for deg in (30.0, 60.0):
            ln = Line()
            ln.X1 = ln.X2 = w * deg / 90.0
            ln.Y1, ln.Y2 = track_y - 2.0, track_y + track_h + 2.0
            ln.Stroke = self._track_tick
            ln.StrokeThickness = 1.0
            canvas.Children.Append(ln)

        ptr = Rectangle()
        ptr.Width, ptr.Height = 2.0, track_h + 8.0
        ptr.Fill = self._track_tick
        canvas.Children.Append(ptr)
        Canvas.SetLeft(ptr, max(0.0, min(w - 2.0, w * frac - 1.0)))
        Canvas.SetTop(ptr, track_y - 4.0)
        return canvas

    def _night_index(self, key: str) -> int:
        idx = self._night_colors.get(key)
        if idx is None:
            idx = len(self._night_colors)
            self._night_colors[key] = idx
        return idx

    def _render_badges(self, badges: list[tuple[str, str]]) -> None:
        self.info_badges.Children.Clear()
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
            self.info_badges.Children.Append(chip)
        self.info_badges.Visibility = (
            Visibility.Visible if badges else Visibility.Collapsed)

    def _render_pills(self, pills: list[tuple]) -> None:
        self.info_pills.Children.Clear()
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
            self.info_pills.Children.Append(pill)
        self.info_pills.Visibility = (
            Visibility.Visible if pills else Visibility.Collapsed)


def _reveal(path: str) -> None:
    """在资源管理器里定位到该文件(选中它, 而不是只打开目录)。

    用 explorer.exe /select 而不是 Launcher.LaunchFolderAsync —— 后者是
    WinRT 异步 API, 从 InfoBar 的同步 Click 处理器里调要再绕一层编组,
    而且只能打开目录、不能选中文件。
    """
    try:
        subprocess.Popen(["explorer.exe", "/select,", os.path.normpath(path)])
    except Exception:
        pass


def _save_png(src_bmp: str, target_dir: Path, name: str) -> str:
    """把当前显示的位图另存成 PNG(工作线程调用)。"""
    from PIL import Image as PILImage

    target_dir.mkdir(parents=True, exist_ok=True)
    stem = os.path.splitext(name)[0]
    out = target_dir / f"{stem}_stretched.png"
    n = 1
    while out.exists():
        out = target_dir / f"{stem}_stretched ({n}).png"
        n += 1
    with PILImage.open(src_bmp) as im:
        im.save(out, format="PNG")
    return str(out)
