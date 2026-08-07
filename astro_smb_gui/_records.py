"""拍摄记录页:按夜次浏览 Autorun 日志聚合出的目标拍摄记录。

左栏为当前夜次的目标列表(行对象持久化 dict 模式;每行自带时间轨道:
开始时刻 + 状态色圆点 + 上下连接竖线,长间隔处插"间隙"细行)+ alt-az
天球图(可叠加巡天底图:底层 Image 显示工作线程重投影出的 ESO 银河全景);
「合并计划」开关可把列表切换成按 Plan 分组的顺序视图(组头行可点击折叠)。
右栏为选中目标的详情(徽章行 + 两列 KV 表 + 结构化事件时间线);
顶部工具栏下方是夜次统计汇总卡与整夜时间轴甘特图。
数据来自共享的 ``shell.logstore``(LogStore),刷新在工作线程执行,
统计/时间轴/列表布局/详情渲染数据/FITS 头读取/时间线归并/底图重投影
也全部在工作线程算好,UI 更新一律经 ``shell.ui(...)`` 编组回 UI 线程;
单飞用代次计数器,过期结果丢弃(日志刷新与底图渲染各一套代次)。

**两段式懒加载**(同一个 ``records-load`` 工作线程,两次编组):

  第一段「秒出」—— 只用**已缓存**的摘要(``store.data`` 直接复用,或
  ``LogStore.summaries(parse_missing=False)`` 严格只读 metacache,不下载
  不解析),把仅凭 ``nights`` 就能画的部分先渲染出来:夜次下拉、目标列表
  (含时间轨道/分组布局)、事件时间线、统计卡左列、整夜时间轴、天球图
  (先用日志 slew 坐标)。尚未就绪的字段(导星 RMS / FITS 实测坐标与设备)
  一律显示 **"读取中…"** 占位 —— 绝不能与"确实没有数据"混淆。

  第二段「补全」—— 同一线程继续走原有全量路径(``refresh()`` + 逐 run 导星
  摘要 + ``Plan\\Light`` 目录集合 + 逐目标 FITS 头),完成后整屏重渲染替换
  占位,并按**稳定键**(``_run_key``)找回用户选中的目标 + 滚回可视区。

  缓存全空(首次使用/换设备)时第一段拿不到夜次,直接跳过,行为与两段式
  引入前完全一致(不会先闪一下空列表)。两段共用同一代次 ``_gen``,
  加载指示(ring/按钮/状态文案)只在第二段结束时收起。
"""

from __future__ import annotations

import math
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

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
    ComboBoxItem,
    ContentDialog,
    Grid,
    Image,
    ListView,
    ListViewItem,
    Orientation,
    ProgressRing,
    RowDefinition,
    Slider,
    StackPanel,
    TextBlock,
    TextBox,
    ToggleSwitch,
    ToolTip,
    ToolTipService,
)
from win32more.Microsoft.UI.Xaml.Markup import XamlReader
from win32more.Microsoft.UI.Xaml.Media import (
    DoubleCollection,
    FontFamily,
    SolidColorBrush,
)
from win32more.Microsoft.UI.Xaml.Media.Imaging import BitmapImage
from win32more.Microsoft.UI.Xaml.Shapes import Ellipse, Line, Rectangle
from win32more.Windows.UI import Color

from astro_smb import astro
from astro_smb.autorunlog import (
    AutorunBlock, Night, TargetRun, parse_exposure_seconds,
)
from astro_smb.client import SmbClientError
from astro_smb_gui import skymap
from astro_smb_gui._common import (
    XAML_NS, argb_hex, file_uri, line_fragment, rect_fragment,
)
from astro_smb_gui.logstore import (
    guide_summary_for_run, load_site, save_site, section_begins,
)
from astro_smb.guidecheck import POLAR_COND_DEGENERATE
from astro_smb.i18n import gettext as _
from astro_smb_gui.preview import read_fits_header

XAML_PATH = Path(__file__).with_name("records.xaml")


# ------------------------------------------------------------------ 极轴示意图

# 环的候选满量程(角分)。取"刚好装得下"的那一档,免得 1′ 的误差和 30′ 的
# 误差画出来一模一样。
POLAR_FULL_SCALES = (1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0)


# 视图模型已下沉到 astro_smb_app.views.records —— 新前端消费同一份,
# "第 N 张"/"已完成·已暂停·被截断"/"间隔 6 分钟" 这些措辞两边永远一致。
# (B8 按计划随切片抽取,走 docs/architecture/frontend.md 的逃生口;
#  函数体一个字节没动。留在本文件的是真正绑 WinUI 的:_brush/_corner
#  与那几个 XAML 序列化器。)
from astro_smb_app.views.records import (  # noqa: F401
    GROUP_INDENT,
    PLAN_LIGHT_DIR,
    PLAN_SHARE,
    POLAR_FULL_SCALES,
    SKY_RENDER_PX,
    TARGET_GAP_S,
    TIMELINE_GAP_S,
    TL_BAR_H,
    TL_BAR_MIN_W,
    TL_BAR_Y,
    TL_GUIDE_H,
    TL_GUIDE_Y,
    TL_HIT_PAD_X,
    TL_HIT_PAD_Y,
    TL_LABEL_MIN_W,
    TL_TICK_Y,
    TRACK_GAP,
    TRACK_RAIL_W,
    TRACK_W,
    _BADGE_COLORS,
    _DOT_BOT,
    _DOT_TOP,
    _FRAME_TYPE_CN,
    _FRAME_TYPE_LABEL,
    _FRAME_TYPE_ORDER,
    _GUIDE_PHRASES,
    _LEVEL_MARK,
    _TL_MIN_BLOCK_S,
    _TL_PALETTE,
    _XML_ESCAPE,
    _ac_card,
    _ac_short,
    _af_card,
    _block_end,
    _derive_maps,
    _end_state,
    _fits_info,
    _fits_num,
    _fmt_exp_compact,
    _fmt_gap,
    _fmt_integration,
    _fmt_lat,
    _fmt_lon,
    _fmt_range,
    _group_card,
    _group_header,
    _guide_card,
    _guide_map_for,
    _guide_short,
    _night_layouts,
    _night_summary,
    _night_timeline,
    _night_window,
    _pixel_scale,
    _preview_status_line,
    _run_detail,
    _run_key,
    _run_level,
    _run_row_data,
    _run_subline,
    _runs_with_gaps,
    _sky_relevant,
    _spans_from_sections,
    _timeline_items,
    polar_advice,
    polar_plot_geometry,
    polar_plot_scale,
    scale_alpha,
    timeline_bar_px,
    timeline_hit_bar,
)
from astro_smb_gui._xamli18n import load_text as _xaml_text


def _brush(r: int, g: int, b: int, a: int = 255) -> SolidColorBrush:
    c = Color()
    c.A, c.R, c.G, c.B = a, r, g, b
    return SolidColorBrush(c)


def _corner(r: float) -> CornerRadius:
    """CornerRadius 结构体(先建后赋, §7.1 结构体惯用法)。"""
    cr = CornerRadius()
    cr.TopLeft = cr.TopRight = cr.BottomLeft = cr.BottomRight = r
    return cr


def polar_plot_fragment(polar, size: float = 132.0, *,
                        ink: str = "#FF8A8A8A", accent: str = "#FFE05A5A",
                        label: str = "#FF9A9A9A") -> str:
    """极轴示意图 → 一次 ``XamlReader.Load`` 就能成型的片段。

    元素只有十来个,批量拼串主要是为了**一次 Load**(见 _common.rect_fragment
    里记的那笔账),顺便省掉逐元素设属性的往返。
    """
    g = polar_plot_geometry(polar, size)
    cx, cy = g["center"]
    r = g["radius"]
    body = []
    for rr in g["rings"]:
        body.append(
            f'<Ellipse Width="{rr * 2:.2f}" Height="{rr * 2:.2f}"'
            f' Stroke="{ink}" StrokeThickness="1" Opacity="0.45"'
            f' Canvas.Left="{cx - rr:.2f}" Canvas.Top="{cy - rr:.2f}"/>')
    body.append(
        f'<Line X1="{cx - r:.2f}" Y1="{cy:.2f}" X2="{cx + r:.2f}" Y2="{cy:.2f}"'
        f' Stroke="{ink}" StrokeThickness="1" Opacity="0.5"/>')
    body.append(
        f'<Line X1="{cx:.2f}" Y1="{cy - r:.2f}" X2="{cx:.2f}" Y2="{cy + r:.2f}"'
        f' Stroke="{ink}" StrokeThickness="1" Opacity="0.5"/>')
    # 天极本身:实心小点,和"实际极轴"区分开
    body.append(
        f'<Ellipse Width="5" Height="5" Fill="{ink}"'
        f' Canvas.Left="{cx - 2.5:.2f}" Canvas.Top="{cy - 2.5:.2f}"/>')
    for text, tx, ty in g["labels"]:
        body.append(
            f'<TextBlock Text="{text}" FontSize="9" Foreground="{label}"'
            f' Opacity="0.75" Canvas.Left="{tx - 9:.2f}" Canvas.Top="{ty:.2f}"/>')
    body.append(
        _('<TextBlock Text="满量程 {0:g}′" FontSize="9" Foreground="{label}" Opacity="0.6" Canvas.Left="2" Canvas.Top="{1:.2f}"/>').format(
            
            g['full'], size - 12, label=label))
    mk = g["marker"]
    if mk is not None:
        mx, my = mk
        body.append(
            f'<Line X1="{cx:.2f}" Y1="{cy:.2f}" X2="{mx:.2f}" Y2="{my:.2f}"'
            f' Stroke="{accent}" StrokeThickness="1.4" Opacity="0.8"/>')
        body.append(
            f'<Ellipse Width="9" Height="9" Fill="{accent}"'
            f' Canvas.Left="{mx - 4.5:.2f}" Canvas.Top="{my - 4.5:.2f}"/>')
    return (f'<Canvas xmlns="{XAML_NS}" Width="{size:.2f}" Height="{size:.2f}">'
            + "".join(body) + "</Canvas>")


def xaml_attr(text) -> str:
    """任意文本 → 可安全放进 XAML 属性值的串。

    两件事: ① XML 实体转义(目标名来自日志, 出现 ``&`` 就会让整段片段解析失败,
    那时甘特图会整个退回逐元素慢路径); ② 以 ``{`` 开头的属性值会被 XAML 当成
    **标记扩展**解析, 用 XAML 自己的 ``{}`` 前缀转义掉。
    """
    s = "".join(_XML_ESCAPE.get(ch, ch) for ch in str(text))
    return "{}" + s if s.startswith("{") else s


def text_fragment(texts) -> str:
    """一批文本 (x, y, 文本, 字号, #AARRGGBB, 宽度|None) → 批量片段。

    `_common` 的共享原语覆盖了矩形/直线/折线, **没有**文本 —— 而甘特图的小时
    刻度标签与条内目标名逐个建 TextBlock 约 0.88ms/个, 和矩形一样值得整批交给
    XAML 解析器。除元素类型外语义与共享原语完全一致: 产物是各自独立的
    TextBlock、数字按 invariant culture 定点格式化、空列表返回空串。

    宽度非 None 时附 ``TextTrimming=CharacterEllipsis``(条内标签超宽要省略号);
    全部 ``IsHitTestVisible=False`` —— 标签挡住下面的横条会让点击落空。
    """
    if not texts:
        return ""
    out = []
    for x, y, text, size, color, width in texts:
        wattr = ("" if width is None else
                 f' Width="{max(0.0, width):.2f}"'
                 f' TextTrimming="CharacterEllipsis"')
        out.append(
            f'<TextBlock Text="{xaml_attr(text)}" FontSize="{size:.2f}"'
            f' Foreground="{color}" IsHitTestVisible="False"{wattr}'
            f' Canvas.Left="{x:.2f}" Canvas.Top="{y:.2f}"/>')
    return f'<Canvas xmlns="{XAML_NS}">{"".join(out)}</Canvas>'


def batch_canvas(*fragments: str) -> str:
    """把若干子片段合成**一次** XamlReader.Load 的根画布(参数顺序即 z 序)。

    共享原语各自返回一个 ``<Canvas>`` 根, 嵌套进同一个根画布是合法 XAML,
    于是整张图只解析一次(子画布无尺寸、不裁剪, 只当分组容器用)。
    全空返回空串, 调用方据此跳过 Load。
    """
    body = "".join(f for f in fragments if f)
    return f'<Canvas xmlns="{XAML_NS}">{body}</Canvas>' if body else ""


class RecordsPage:
    def __init__(self, shell):
        self.shell = shell
        self.root = XamlReader.Load(_xaml_text(XAML_PATH)).as_(FrameworkElement)

        self.data = None                    # LogData(最近一次应用到 UI 的)
        self._guide_map: dict[int, tuple] = {}   # id(run) → (RmsStats|None, 覆盖率)
        self._plan_dirs: set[str] | None = None  # Plan\Light 下已存在的目标目录名
        self._nights: list = []             # 倒序夜次(最新在前)
        self._night_idx = -1
        self._night_date: str | None = None
        # 夜次是**第一段缓存摘要**临时钉上的(而非用户选的/全量结果定的)。
        # 缓存里缺最新那一夜时(关着程序拍了一整夜, 早上开 app), 第一段只能钉在
        # 次新夜; 若第二段照旧"优先匹配已选日期"就会停在次新夜, 而改动前冷启动
        # 一定落在最新一夜 —— 所以第二段要忽略这个临时值。
        self._night_from_preview = False
        # 页面上**已经有一份完整渲染**(第二段产物)。用来门控第一段:
        # 已经完整就别再用信息量更少的缓存摘要去覆盖它(退回"读取中…"再填回来)。
        # 不能用 `store.data is self.data` 代替 —— _apply_preview 刻意不写
        # self.data, 且别的页面 force 刷新会把 store.data 换成新对象。
        self._render_complete = False
        self._runs: list[TargetRun] = []    # 当前夜次目标(天球图/遮罩共用)
        self._rows: dict[int, dict] = {}    # id(run) → 行对象(持久化, 数据代内复用)
        self._sel_run: TargetRun | None = None

        # 目标列表布局(反馈#3): 两套布局在工作线程算好, 按夜次日期取用
        self._night_layout: dict[str, dict] = {}
        self._list_items: list[dict] = []   # 当前可见条目(索引 ↔ ListView 行)
        # 「合并计划」开关(页面实例内存):**默认开**,与 records.xaml 的
        # ToggleSwitch IsOn="True" 保持一致(两处必须同步,否则首屏显示与实际布局不符)
        self._merge_plan = True
        self._collapsed: set[tuple] = set()  # 折叠态 (夜次日期, 组 key), 跨夜保持
        # 行显示字段 / 详情渲染数据(工作线程算好): id(run) → dict
        self._row_map: dict[int, dict] = {}
        self._detail_map: dict[int, dict] = {}

        self._connected = False
        self._loading = False
        self._gen = 0                       # 单飞代次: 过期 worker 结果丢弃
        self._ui_updating = False           # 程序化改 Combo/List/Toggle 时抑制事件回调
        self._site = load_site()
        # 懒加载第一段的占位标志: 对应数据还在后台补(UI 显示"读取中…")
        self._guide_pending = False
        self._fits_pending = False
        # 站点经度推算值: 第一段(LogSummary)也有, 故不再直接读 self.data
        # —— 否则首屏会先按默认经度画一遍天球再跳一次(_site_latlon 用它)
        self._lon_est: float | None = None

        # 夜次统计/时间轴(工作线程算好, 按夜次日期取用)
        self._night_stats: dict[str, tuple[str, str]] = {}
        self._night_tl: dict[str, dict | None] = {}
        # 整夜时间轴的命中反算数据(横条不再逐个挂事件, 见 _draw_timeline):
        # _tl_hit = 与绘制同序的 (f0, f1, run, 提示文本), _tl_w = 画这一版时的画布宽
        self._tl_hit: list[tuple] = []
        self._tl_w = 0.0
        self._tl_tip: ToolTip | None = None     # 画布上唯一那个提示框
        self._tl_tip_idx: int | None = -2       # 最近一次悬停命中(-2=未知)
        # Plan 组头行按组键复用的容器(见 _group_row): key → 行控件
        self._group_cards: dict[str, dict] = {}
        # FITS 头信息(反馈#2#3): id(run) → info; 文件级缓存跨刷新复用
        # (_fits_cache 只在 records-load 工作线程读写, dict 单操作 GIL 原子)
        self._fits_map: dict[int, dict] = {}
        self._fits_cache: dict[tuple, dict] = {}   # (share,path,size,mtime) → info
        # 事件时间线条目(反馈#4, 工作线程算好): id(run) → 条目列表
        self._timeline_map: dict[int, list] = {}

        # 巡天底图状态(渲染单飞代次独立于日志刷新代次)
        self._survey_on = False             # ToggleSwitch 逻辑状态
        # 天球放大遮罩层状态
        self._ov_open = False
        self._ov_begin = None               # 滑杆 0 点对应的 datetime
        self._ov_size = 0.0
        self._ov_dots: list[tuple] = []     # 持久化点/标签(tick 只移动位置)
        self._ov_bg_paths: dict[int, str] = {}   # 时间桶 → 预热好的底图 PNG
        self._ov_bg_shown: int | None = None
        self._ov_want_bucket = 0
        self._ov_bg_busy = False            # 预热 worker 单飞
        # 双缓冲状态:BitmapImage 按桶缓存(LRU),ready=已解码过可直接换。
        # 解码**串行化**:back 同一时刻只解一帧(_ov_back_bucket 记账),
        # ImageOpened 事件归属才无歧义(已解码位图重赋值不再触发事件,审查实证)
        self._ov_bmp: dict[int, object] = {}
        self._ov_lru: list[int] = []
        self._ov_ready: set[int] = set()
        self._ov_pending: int | None = None
        self._ov_back_bucket: int | None = None
        self._ov_front: Image | None = None
        self._ov_back: Image | None = None
        self._ov_warm_gen = 0               # 预热代次(关/开遮罩都 +1)
        self._ov_warm_req = None            # busy 期间被顶掉的预热请求
        self._bg_shown = False              # 底图当前已显示(决定前景提亮)
        self._sky_gen = 0                   # 底图渲染单飞代次
        self._sky_key = None                # 最近一次成功应用的渲染 key
        self._sky_inflight = None           # 在途渲染的 key(去重防连点)
        self._sky_lock = threading.Lock()   # 下载/渲染串行化(并发下载会撞同一 .part)

        # 复用画刷(状态色与其它页一致)
        self._b_green = _brush(0x4C, 0xAF, 0x50)
        self._b_amber = _brush(0xFF, 0xB3, 0x00)
        self._b_red = _brush(0xE5, 0x73, 0x73)
        self._b_grid = _brush(0x80, 0x80, 0x80, 150)
        self._b_grid_dim = _brush(0x80, 0x80, 0x80, 80)
        self._b_label = _brush(0x9E, 0x9E, 0x9E)
        self._b_sel = _brush(0x42, 0xA5, 0xF5)
        # 巡天底图上的前景提亮画刷(底图显示时替换普通灰系)
        self._b_grid_bri = _brush(0xFF, 0xFF, 0xFF, 200)
        self._b_grid_dim_bri = _brush(0xFF, 0xFF, 0xFF, 120)
        self._b_label_bri = _brush(0xF5, 0xF5, 0xF5, 235)
        self._b_shadow = _brush(0x00, 0x00, 0x00, 230)   # 标签 1px 阴影
        # 时间轴画刷(6 色循环 + 导星覆盖绿 + 条内标签白)
        self._tl_brushes = [_brush(*rgb) for rgb in _TL_PALETTE]
        self._tl_guide = _brush(0x4C, 0xAF, 0x50, 110)
        self._b_white = _brush(0xFF, 0xFF, 0xFF, 235)
        # 事件时间线画刷(标记按状态色, 轨道/卡片底/进度槽为半透明灰系,
        # 深浅主题都可读; 预建复用, 行构建时绝不新建画刷)
        self._b_infoblue = _brush(0x78, 0x90, 0x9C)
        self._b_rail = _brush(0x80, 0x80, 0x80, 90)
        self._b_card_bg = _brush(0x80, 0x80, 0x80, 28)
        self._b_bar_bg = _brush(0x80, 0x80, 0x80, 70)
        self._tl_level_brush = {"ok": self._b_green, "warn": self._b_amber,
                                "err": self._b_red, "info": self._b_infoblue}
        # 详情徽章画刷(浅底深字), 预建复用 —— 渲染时绝不新建画刷
        self._badge_brushes = {
            k: (_brush(*bg), _brush(*fg)) for k, (bg, fg) in _BADGE_COLORS.items()
        }
        self._mono_font = FontFamily("Consolas")

        self._find()
        self._wire()
        self.lat_box.Text = f"{self._site['lat']:g}"
        self._update_site_ui()
        self._draw_sky()

    # ---------- 控件 ----------

    def _find(self) -> None:
        f = self.root.FindName
        self.refresh_btn = f("RefreshBtn").as_(Button)
        self.refresh_ring = f("RefreshRing").as_(ProgressRing)
        self.night_combo = f("NightCombo").as_(ComboBox)
        self.merge_toggle = f("MergePlanToggle").as_(ToggleSwitch)
        self.status_text = f("StatusText").as_(TextBlock)
        self.stats_card = f("NightStatsCard").as_(Border)
        self.stats_left = f("NightStatsLeft").as_(TextBlock)
        self.stats_right = f("NightStatsRight").as_(TextBlock)
        self.tl_card = f("TimelineCard").as_(Border)
        self.tl_canvas = f("TimelineCanvas").as_(Canvas)
        self.target_list = f("TargetList").as_(ListView)
        self.sky_canvas = f("SkyCanvas").as_(Canvas)
        self.sky_title = f("SkyTitleText").as_(TextBlock)
        self.sky_bg_image = f("SkyBgImage").as_(Image)
        self.sky_credit = f("SkyCreditText").as_(TextBlock)
        self.survey_toggle = f("SurveyToggle").as_(ToggleSwitch)
        self.lat_box = f("LatBox").as_(TextBox)
        self.lon_text = f("LonText").as_(TextBlock)
        self.site_apply_btn = f("SiteApplyBtn").as_(Button)
        self.detail_title = f("DetailTitle").as_(TextBlock)
        self.detail_coord = f("DetailCoord").as_(TextBlock)
        self.detail_badges = f("DetailBadges").as_(StackPanel)
        self.detail_grid = f("DetailGrid").as_(Grid)
        self.guide_quality_card = f("GuideQualityCard").as_(Border)
        self.guide_quality_headline = f("GuideQualityHeadline").as_(TextBlock)
        self.guide_quality_confidence = f("GuideQualityConfidence").as_(TextBlock)
        self.guide_quality_findings = f("GuideQualityFindings").as_(TextBlock)
        self.guide_quality_ring = f("GuideQualityRing").as_(ProgressRing)
        self.guide_quality_btn = f("GuideQualityBtn").as_(Button)
        self.polar_card = f("PolarCard").as_(Border)
        self.polar_plot = f("PolarPlot").as_(Canvas)
        self.polar_total = f("PolarTotal").as_(TextBlock)
        self.polar_advice = f("PolarAdvice").as_(TextBlock)
        self.polar_trust = f("PolarTrust").as_(TextBlock)
        self.open_files_btn = f("OpenFilesBtn").as_(Button)
        self.guiding_btn = f("GuidingBtn").as_(Button)
        self.dir_hint = f("DirHint").as_(TextBlock)
        self.timeline_list = f("TimelineList").as_(ListView)
        self.live_banner = f("LiveBanner").as_(Border)
        self.live_text = f("LiveText").as_(TextBlock)
        self.sky_zoom_btn = f("SkyZoomBtn").as_(Button)
        self.sky_overlay = f("SkyOverlay").as_(Border)
        self.ov_title = f("OvTitleText").as_(TextBlock)
        self.ov_close_btn = f("OvCloseBtn").as_(Button)
        self.ov_holder = f("OvHolder").as_(Grid)
        self.ov_image_a = f("OvImageA").as_(Image)
        self.ov_image_b = f("OvImageB").as_(Image)
        self.ov_canvas = f("OvCanvas").as_(Canvas)
        self.ov_slider = f("OvSlider").as_(Slider)
        self.ov_time = f("OvTimeText").as_(TextBlock)
        self.ov_credit = f("OvCreditText").as_(TextBlock)

    def _wire(self) -> None:
        self.refresh_btn.Click += self._on_refresh
        self.night_combo.SelectionChanged += self._on_night_changed
        self.merge_toggle.Toggled += self._on_merge_toggled
        self.target_list.SelectionChanged += self._on_target_selected
        self.open_files_btn.Click += self._on_open_files
        self.guiding_btn.Click += self._on_guiding
        self.guide_quality_btn.Click += self._on_guide_quality
        self.site_apply_btn.Click += self._on_site_apply
        self.survey_toggle.Toggled += self._on_survey_toggled
        self.sky_zoom_btn.Click += self._open_sky_overlay
        self.ov_close_btn.Click += self._close_sky_overlay
        self.ov_slider.ValueChanged += self._on_ov_slider
        # 双缓冲翻面:背面解码完成才显示(两张都挂,靠 _ov_back_bucket 记账甄别)
        self.ov_image_a.ImageOpened += self._ov_img_opened
        self.ov_image_b.ImageOpened += self._ov_img_opened
        self.ov_image_a.ImageFailed += self._ov_img_failed
        self.ov_image_b.ImageFailed += self._ov_img_failed
        # 时间轴宽随窗口: 尺寸变化只重画(数据已在工作线程算好)。
        # 画布是 records.xaml 里的固定控件, 下面三个事件**只在这里注册一次**:
        # 逐个横条挂 Tapped 会随每次重画永久滞留(见 _draw_timeline 的说明),
        # 命中哪一条改由 timeline_hit_bar 反算, 提示文本走画布上唯一的 ToolTip。
        self.tl_canvas.SizeChanged += lambda s, e: self._draw_timeline()
        self.tl_canvas.Tapped += self._on_timeline_tapped
        self.tl_canvas.PointerMoved += self._on_timeline_moved
        self.tl_canvas.PointerExited += self._on_timeline_exited
        try:
            tip = ToolTip()
            tip.IsEnabled = False               # 没悬在横条上就不弹
            ToolTipService.SetToolTip(self.tl_canvas, tip)
            self._tl_tip = tip
        except Exception:
            self._tl_tip = None                 # 弹不出提示也不能挡住整页

    # ---------- 生命周期 ----------

    def _data_share(self) -> str:
        """存放 log/ 与 Plan/Light/ 的共享名。

        SMB 是 "EMMC Images",**本地卡是卷标** —— 硬编码常量在本地卡上
        必然一无所获,而且是静默退化(listdir 抛错被吞成空集合),
        表现为"实测坐标/设备信息莫名其妙全没了"(审查实证)。
        """
        return getattr(self.shell, "data_share", "") or PLAN_SHARE

    def on_show(self) -> None:
        st = getattr(self.shell, "watch_state", None)
        if st:
            self.on_watch(st)               # 补显实时横幅(watcher 早于切页时)
        store = getattr(self.shell, "logstore", None)
        if store is not None and store.data is not None:
            if store.data is self.data:
                self._render_guide_quality(self._sel_run)
                return                      # 已渲染当前数据
            self._start_load(force=False)   # 有缓存: 线程里只做派生计算
            return
        if not self._connected:
            self.status_text.Text = _("未连接设备, 无法读取日志")
            return
        self._start_load(force=False)

    def on_watch(self, state: dict) -> None:
        """shell 转发的运行状态(已编组到 UI 线程):顶部实时横幅。

        日志是会话结束时一次性落盘的 —— 目标列表永远是历史;正在进行的
        拍摄只有 watcher 的帧心跳知道,在这里明确标出,消除"哪个在跑"的困惑。
        """
        try:
            if state.get("running"):
                t = state.get("target") or "?"
                parts = [_("正在拍摄(实时): {t}").format(t=t)]
                if state.get("seq"):
                    parts.append(_("第{0}张").format(state['seq']))
                exp = state.get("exposure_s")
                if exp and exp >= 1.0:
                    parts.append(f"{exp:.0f}s")
                self.live_text.Text = (
                    "◉ " + " · ".join(parts)
                    + _("  —  下方列表为历史记录;本次会话的日志将在拍摄结束后生成, 届时会提示刷新"))
                self.live_banner.Visibility = Visibility.Visible
            else:
                self.live_banner.Visibility = Visibility.Collapsed
        except Exception:
            pass

    def on_connected(self, shares) -> None:
        self._connected = True
        self._start_load(force=False)

    def on_source_changed(self) -> None:
        """shell 换了设备:屏幕上这份渲染属于上一台设备,不再算"已完整"。

        不清空列表 —— 让旧内容留着直到新数据到位,比先白屏再填好看;
        但必须把 _render_complete 放倒,否则首屏门控会以为页面已完整,
        跳过新设备的缓存秒出路径。
        """
        self._render_complete = False
        self._night_from_preview = False
        self._night_date = None         # 新设备的夜次集合无关,别沿用旧选择

    def on_new_logs(self, names) -> None:
        """watcher 侦测到新日志(shell 已把 logstore.data 置 None):
        页面正在前台则立即重载,否则等下次 on_show 时自然重载。"""
        if getattr(self.shell, "_current_page", None) is self:
            self._start_load(force=True)

    # ---------- 刷新(工作线程) ----------

    def _on_refresh(self, sender, e) -> None:
        # 未连接时不许发起 SMB —— 否则会去拨占位地址(192.0.2.1)并
        # 挂满超时,还把这个用户从没输入过的地址回显进错误提示(审查实证)
        if not self._connected:
            self.status_text.Text = _("未连接设备, 无法读取日志")
            return
        self._start_load(force=True)

    def _start_load(self, force: bool) -> None:
        store = getattr(self.shell, "logstore", None)
        if store is None:
            self.shell.error(_("日志数据层未初始化(shell.logstore 缺失)"))
            return
        if self._loading and not force:
            return
        self._gen += 1
        gen = self._gen
        self._loading = True
        self.refresh_ring.IsActive = True
        self.refresh_btn.IsEnabled = False
        self.status_text.Text = _("正在读取日志…")
        base_client = self.shell.client
        # 页面上已有完整渲染时不做第一段 —— 否则会把已就绪的导星/设备行退回
        # "读取中…"再填回来。**在 UI 线程判定**, 工作线程不去读 self.data。
        has_full = self._render_complete
        threading.Thread(
            target=self._work, args=(gen, force, store, base_client, has_full),
            daemon=True, name="records-load").start()

    def _work(self, gen: int, force: bool, store, base_client,
              has_full: bool = False) -> None:
        """工作线程(两段式, 见模块 docstring)。

        第一段: 只用已缓存的摘要(``store.data`` 或
        ``summaries(parse_missing=False)``)派生"只需 nights"的渲染数据,
        编组给 ``_apply_preview`` 秒出首屏; 拿不到夜次就整段跳过。
        第二段: refresh(或用缓存) + 逐 run 导星摘要 + Plan\\Light 目录集合
        + 逐目标 FITS 头(实测坐标/设备) + 事件时间线条目归并 + 夜次统计汇总
        + 整夜时间轴数据 + 目标行显示字段/详情渲染数据/两套列表布局。
        所有计算在此做完, _apply_data 只做 UI。"""
        clone = None
        try:
            data = store.data
            guide_map: dict[int, tuple] = {}

            # ---- 第一段: 缓存首屏(零解析; 只有 summaries 那条会发 1 次 listdir) ----
            if data is not None and not has_full:
                # 已有完整 LogData: 导星摘要不碰 SMB(全命中缓存约 1ms),
                # 顺手算上, 首屏就只剩 FITS 头是占位
                guide_map = _guide_map_for(data.nights, data.phd2_logs)
                self._emit_preview(gen, data.nights, data.lon_estimate, True,
                                   guide_map, data.phd2_logs, None,
                                   guide_pending=False, fits_pending=True)

            if force or data is None:
                clone = base_client.clone()
                # has_full: 页面上已有完整渲染(典型触发是 watcher 发现新日志 ——
                # shell 先 logstore.invalidate() 把 data 置 None 再回调本页)。
                # 此时屏幕上的旧内容比缓存摘要**信息量更大**, 不能降级覆盖。
                if data is None and not has_full:
                    # 严格只用 metacache: 不下载不解析; 没有夜次就静默跳过
                    # (缓存全空时绝不能先闪一个空列表)
                    summary = None
                    try:
                        summary = store.summaries(clone, parse_missing=False)
                    except SmbClientError:
                        # **连接类失败直接上抛**: 否则紧接着的 refresh() 会拿同一个
                        # 未连接的 clone 再 connect 一次, 用户要等 2× 超时
                        # (默认 15s ⇒ 30s)才看到"读取日志失败"。列目录/解析类
                        # 失败不影响全量路径, 照旧吞掉再试一次。
                        # 判断不了(测试替身等没有该属性)就当**不是**连接失败,
                        # 保持原来的"吞掉再走全量路径"行为
                        if not getattr(clone, "connected", True):
                            raise
                        summary = None
                    except Exception:
                        summary = None
                    if summary is not None and summary.nights:
                        self._emit_preview(
                            gen, summary.nights, summary.lon_estimate,
                            summary.complete, {}, [], summary.phd2_sections,
                            guide_pending=True, fits_pending=True)
                data = store.refresh(clone)
                guide_map = {}
            # ---- 第二段: 全量补全 ----
            if not guide_map:
                guide_map = _guide_map_for(data.nights, data.phd2_logs)

            plan_dirs: set[str] | None = None
            try:
                if clone is None:
                    clone = base_client.clone()
                plan_dirs = {en.name for en in clone.listdir(self._data_share(), PLAN_LIGHT_DIR)
                             if en.is_dir}
            except SmbClientError:
                plan_dirs = None            # 不致命: 详情里不显示该行

            # FITS 头信息(反馈#2#3): 逐目标部分读取首帧头(几 KB/目标)
            fits_map: dict[int, dict] = {}
            if plan_dirs:
                fits_map = self._collect_fits(clone, data, plan_dirs)

            # 事件时间线条目 + 夜次统计卡/时间轴 + 目标行显示字段 + 详情渲染
            # 数据 + 两套列表布局(反馈#3#4): 全部是纯数据, UI 线程只做赋值/
            # 搭控件。与第一段共用 _derive_maps(此处 pending 全 False = 最终态)
            derived = _derive_maps(data.nights, guide_map, fits_map,
                                   data.phd2_logs)

            self.shell.ui(self._apply_data, gen, data, guide_map, plan_dirs,
                          derived["stats"], derived["tl"], fits_map,
                          derived["timelines"],
                          {"rows": derived["rows"],
                           "details": derived["details"],
                           "layouts": derived["layouts"]})
        except SmbClientError as ex:
            self.shell.ui(self._load_failed, gen, str(ex))
        except Exception as ex:             # 防御: 工作线程异常不许静默
            self.shell.ui(self._load_failed, gen, f"{type(ex).__name__}: {ex}")
        finally:
            if clone is not None:
                try:
                    clone.close()
                except Exception:
                    pass

    def _emit_preview(self, gen: int, nights: list, lon_est, complete: bool,
                      guide_map: dict, phd2_logs: list, sections, *,
                      guide_pending: bool, fits_pending: bool) -> bool:
        """工作线程: 派生第一段渲染数据并编组给 UI(纯数据过界)。

        无夜次 / 派生炸了都静默跳过 —— 第一段是纯加速, 绝不能因为它让整次
        加载失败(第二段还会重算一遍)。返回是否真的发出了首屏。
        """
        if not nights:
            return False
        try:
            derived = _derive_maps(
                nights, guide_map, {}, phd2_logs,
                _spans_from_sections(sections) if sections is not None else None,
                guide_pending=guide_pending, fits_pending=fits_pending)
        except Exception:
            return False
        self.shell.ui(self._apply_preview, gen, {
            "nights": list(nights), "lon": lon_est, "complete": complete,
            "guide_map": guide_map, "derived": derived,
            "guide_pending": guide_pending, "fits_pending": fits_pending})
        return True

    def _collect_fits(self, clone, data, plan_dirs: set[str]) -> dict[int, dict]:
        """工作线程: 对有 Plan\\Light\\<目标> 目录的 run 部分读取首帧 FITS 头,
        提取实测坐标与设备信息(反馈#2#3)。

        每个目标目录只 listdir 一次(同目标跨夜复用同一首帧); 头信息按
        (share, path, size, mtime) 缓存在页面实例上, 刷新不重复读; 单个失败
        静默跳过, 连续 3 次失败视为连接问题放弃本轮(浏览页
        _start_fits_meta 同款口径)。"""
        fits_map: dict[int, dict] = {}
        first_fit: dict[str, object] = {}   # 目标名 → 首个 .fit RemoteEntry|None
        fails = 0
        for night in data.nights:
            for run in night.runs:
                if fails >= 3:
                    return fits_map
                if run.target not in plan_dirs:
                    continue
                if run.target not in first_fit:
                    try:
                        entries = clone.listdir(
                            self._data_share(),
                            PLAN_LIGHT_DIR + "\\" + run.target)
                        first_fit[run.target] = next(
                            (e for e in entries if not e.is_dir
                             and e.name.lower().endswith((".fit", ".fits", ".fts"))),
                            None)
                        fails = 0
                    except SmbClientError:
                        first_fit[run.target] = None
                        fails += 1
                        continue
                ent = first_fit[run.target]
                if ent is None:
                    continue
                key = (ent.share, ent.path, ent.size, ent.mtime)
                info = self._fits_cache.get(key)
                if info is None:
                    try:
                        info = _fits_info(read_fits_header(clone, ent))
                    except SmbClientError:
                        fails += 1
                        continue
                    fails = 0
                    self._fits_cache[key] = info
                if info:
                    fits_map[id(run)] = info
        return fits_map

    def _load_failed(self, gen: int, msg: str) -> None:
        if gen != self._gen:
            return                          # 过期 worker
        self._loading = False
        self.refresh_ring.IsActive = False
        self.refresh_btn.IsEnabled = True
        # 第一段已出过缓存首屏时保留内容(比清空有用), 但必须说明补全没做完
        # —— 否则详情里的"读取中…"占位会永远挂着, 变成谎话
        if self._guide_pending or self._fits_pending:
            self.status_text.Text = _("读取日志失败 · 以上为缓存摘要, 详情未补全")
        else:
            self.status_text.Text = _("读取日志失败")
        self.shell.error(_("读取拍摄日志失败: {msg}").format(msg=msg))

    # ---------- 应用数据(仅 UI 线程) ----------

    def _apply_preview(self, gen: int, p: dict) -> None:
        """第一段: 缓存摘要首屏(仅 UI 线程)。

        **不动 self.data** —— on_show 靠 ``store.data is self.data`` 的对象
        身份判断"已渲染当前数据", 把摘要塞进去会让每次切页都重载;
        也**不收起加载指示** —— 第二段结束才算加载完。
        """
        if gen != self._gen:
            return                          # 过期 worker
        nights = p.get("nights") or []
        if not nights:
            return                          # 缓存全空: 不许闪一个空列表
        derived = p["derived"]
        prev_key = self._sel_key()
        self._guide_map = p["guide_map"]
        self._plan_dirs = None
        self._fits_map = {}
        self._guide_pending = bool(p["guide_pending"])
        self._fits_pending = bool(p["fits_pending"])
        self._lon_est = p["lon"]
        self._night_stats = derived["stats"]
        self._night_tl = derived["tl"]
        self._timeline_map = derived["timelines"]
        self._row_map = derived["rows"]
        self._detail_map = derived["details"]
        self._night_layout = derived["layouts"]
        self._rows.clear()                  # 新数据代: 旧行对象全部作废
        self._list_items = []
        self._sel_run = None
        self._nights = list(reversed(nights))        # 倒序, 最新在前

        self.status_text.Text = _preview_status_line(
            len(nights), bool(p["complete"]), self._guide_pending)
        self._update_site_ui()
        idx = self._rebuild_night_combo()
        self._restore_sel_by_key(prev_key, idx)
        self._show_night(idx if self._nights else -1)
        self._scroll_to_selection()
        # 首屏是"部分"渲染: 标记页面尚未完整, 并记下 _night_date 是临时钉的
        # (_show_night 刚写过它), 好让第二段不被缓存里的旧夜次带偏。
        self._render_complete = False
        self._night_from_preview = True

    def _rebuild_night_combo(self) -> int:
        """重建夜次下拉(保持先前选中的夜次日期), 返回选中行索引。"""
        idx = 0
        if self._night_date is not None:
            for i, n in enumerate(self._nights):
                if n.date == self._night_date:
                    idx = i
                    break
        self._ui_updating = True
        try:
            self.night_combo.Items.Clear()
            for n in self._nights:
                it = ComboBoxItem()
                it.Content = _("{date} · {0} 目标 · {total_frames} 帧").format(
                    len(n.runs), date=n.date, total_frames=n.total_frames)
                self.night_combo.Items.Append(it)
            if self._nights:
                self.night_combo.SelectedIndex = idx
        finally:
            self._ui_updating = False
        return idx

    def _sel_key(self) -> tuple | None:
        return None if self._sel_run is None else _run_key(self._sel_run)

    def _restore_sel_by_key(self, key: tuple | None, idx: int) -> None:
        """把选中目标恢复到新一代数据里的**同一个** run。

        两段/两次刷新各跑一遍 aggregate_nights, 对象不同 ⇒ 只能按稳定键找回
        (`_show_night` 之后的一切都按对象身份比较, 所以必须在此换成新对象)。
        """
        if key is None or not (0 <= idx < len(self._nights)):
            return
        for r in self._nights[idx].runs:
            if _run_key(r) == key:
                self._sel_run = r
                return

    def _scroll_to_selection(self) -> None:
        """重建列表会把滚动位置弹回顶部 —— 把选中目标滚回可视区。

        只在整屏重渲染(两段式的两次编组)后调用; 折叠/切开关那些局部重画
        不动滚动位置(那是用户自己的手势, 原地不动才对)。
        """
        idx = self._index_of_run(self._sel_run)
        if idx < 0:
            return
        try:
            items = self.target_list.Items
            if idx < items.Size:
                self.target_list.ScrollIntoView(items.GetAt(idx))
        except Exception:
            pass                            # 滚动只是锦上添花, 失败不影响内容

    def _apply_data(self, gen: int, data, guide_map: dict, plan_dirs,
                    night_stats: dict, night_tl: dict,
                    fits_map: dict, timeline_map: dict, extra: dict) -> None:
        if gen != self._gen:
            return                          # 过期 worker
        self._loading = False
        self.refresh_ring.IsActive = False
        self.refresh_btn.IsEnabled = True

        self.data = data
        self._guide_map = guide_map
        self._plan_dirs = plan_dirs
        self._night_stats = night_stats
        self._night_tl = night_tl
        self._fits_map = fits_map
        self._timeline_map = timeline_map
        self._row_map = extra.get("rows", {})
        self._detail_map = extra.get("details", {})
        self._night_layout = extra.get("layouts", {})
        self._guide_pending = False         # 第二段 = 最终态, 占位全部落实
        self._fits_pending = False
        self._lon_est = data.lon_estimate
        prev_key = self._sel_key()          # 必须在清 _sel_run 之前取
        self._rows.clear()                  # 新数据代: 旧行对象全部作废
        self._list_items = []
        self._sel_run = None
        self._nights = list(reversed(data.nights))   # 倒序, 最新在前

        self.status_text.Text = self._status_line()
        self._update_site_ui()
        # 第一段临时钉的夜次不作数(缓存里可能没有最新那一夜, 见 __init__ 注释),
        # 丢掉它让 _rebuild_night_combo 回落 idx=0 = 最新夜。用户手动选过的
        # (_on_night_changed 已清标记)则照旧尊重。
        if self._night_from_preview:
            self._night_date = None
        self._night_from_preview = False
        idx = self._rebuild_night_combo()
        self._restore_sel_by_key(prev_key, idx)
        self._show_night(idx if self._nights else -1)
        self._scroll_to_selection()
        self._render_complete = True        # 页面现在是完整渲染

        # 调试钩子: ASTRO_SMB_GUI_MERGEPLAN=1 自动开启「合并计划」(截图验证用)
        if (os.environ.get("ASTRO_SMB_GUI_MERGEPLAN")
                and not self._merge_plan
                and not getattr(self, "_merge_auto_done", False)):
            self._merge_auto_done = True
            self._merge_plan = True
            self._ui_updating = True
            try:
                self.merge_toggle.IsOn = True
            finally:
                self._ui_updating = False
            self._render_list()
        # 调试钩子(§7.10): ASTRO_SMB_GUI_SKYBG=1 且底图已缓存时自动开启巡天底图
        if (os.environ.get("ASTRO_SMB_GUI_SKYBG")
                and not self._survey_on
                and not getattr(self, "_skybg_auto_done", False)
                and skymap.survey_available()):
            self._skybg_auto_done = True
            self._survey_on = True
            self._set_toggle(True)
            self._update_sky_bg(force=True)
        # 调试钩子: ASTRO_SMB_GUI_SKYZOOM=1 自动打开天球放大遮罩(截图验证用)
        if (os.environ.get("ASTRO_SMB_GUI_SKYZOOM")
                and not getattr(self, "_skyzoom_auto_done", False)):
            self._skyzoom_auto_done = True
            self._open_sky_overlay(None, None)

    def _status_line(self) -> str:
        d = self.data
        n_logs = len(d.autorun_logs) + len(d.phd2_logs)
        lat, lon = self._site_latlon()
        tag = _("(推算)") if d.lon_estimate is not None else _("(默认)")
        s = (_("{n_logs} 个日志 · {0} 个夜次 · 站点 {1}{tag} / {2}").format(
            len(d.nights), _fmt_lon(lon), _fmt_lat(lat), n_logs=n_logs, tag=tag))
        if d.errors:
            s += _(" · {0} 个文件读取失败").format(len(d.errors))
        return s

    # ---------- 夜次 / 目标列表 ----------

    def _on_night_changed(self, sender, e) -> None:
        if self._ui_updating:
            return
        idx = self.night_combo.SelectedIndex
        if idx is None or not (0 <= idx < len(self._nights)):
            return
        self._sel_run = None                # 切夜次不保留跨夜选择
        self._night_from_preview = False    # 用户亲自选的, 第二段必须尊重
        self._show_night(idx)

    def _show_night(self, idx: int) -> None:
        if 0 <= idx < len(self._nights):
            night = self._nights[idx]
            self._night_idx = idx
            self._night_date = night.date
            self._runs = list(night.runs)
        else:
            self._night_idx = -1
            self._night_date = None
            self._runs = []
        # 跨夜选择不保留(按身份比较: TargetRun 是 dataclass, == 会逐字段比)
        if self._sel_run is not None and not any(r is self._sel_run
                                                 for r in self._runs):
            self._sel_run = None
        self._render_list()
        self._update_summary_ui()
        self._draw_timeline()
        self._show_detail(self._sel_run)
        self._draw_sky()
        self._update_sky_bg()

    # ---------- 目标列表渲染(平铺 / 按计划分组) ----------

    def _on_merge_toggled(self, sender, e) -> None:
        """「合并计划」开关: 只换一套已算好的布局条目重画列表。"""
        if self._ui_updating:
            return
        on = bool(self.merge_toggle.IsOn)
        if on == self._merge_plan:
            return
        self._merge_plan = on
        prev = self._sel_run
        self._render_list()
        if self._sel_run is not prev:
            self._show_detail(self._sel_run)
            self._draw_sky()
            self._update_sky_bg()

    def _render_list(self) -> None:
        """按当前夜次布局 + 「合并计划」开关 + 折叠态重建目标列表。

        条目(run / gap / group)已在工作线程算好, 这里只搭控件、设可见性
        与轨道竖线的连接关系; 行对象按 id(run) 持久化复用。
        """
        layout = (self._night_layout.get(self._night_date)
                  if self._night_date is not None else None)
        items = (layout or {}).get("grouped" if self._merge_plan else "flat") or []
        vis: list[dict] = []
        for it in items:
            gk = it.get("group")
            if (it["kind"] != "group" and gk is not None
                    and (self._night_date, gk) in self._collapsed):
                continue                    # 该 Plan 组已折叠
            vis.append(it)
        self._list_items = vis
        n = len(vis)

        self._ui_updating = True
        try:
            self.target_list.Items.Clear()
            sel_idx = -1
            first_run = -1
            for i, it in enumerate(vis):
                kind = it["kind"]
                if kind == "run":
                    run = it["run"]
                    row = self._rows.get(id(run))
                    if row is None:
                        row = self._build_target_row()
                        self._rows[id(run)] = row
                    # 轨道连接: 上下相邻条目仍属同一段轨道(run/gap)才连线
                    up = i > 0 and vis[i - 1]["kind"] in ("run", "gap")
                    down = i + 1 < n and vis[i + 1]["kind"] in ("run", "gap")
                    self._update_target_row(row, run, it, up, down)
                    el = row["root"]
                    if first_run < 0:
                        first_run = i
                    if run is self._sel_run:
                        sel_idx = i
                elif kind == "gap":
                    # 间隙行内无任何交互 → 连指针命中一起关掉, 彻底选不中
                    el = self._inert_row(self._build_list_gap_row(it),
                                         hit_test=False)
                else:
                    # 组头行要留命中给内部折叠手势, 只去焦点(按组键复用容器)
                    el = self._group_row(it)
                self.target_list.Items.Append(el)
            # 选中项被折叠隐藏时保留选择(SelectedIndex=-1, 详情不变);
            # 无有效选择时落到第一个可见目标
            if sel_idx < 0 and (self._sel_run is None
                                or not any(r is self._sel_run
                                           for r in self._runs)):
                sel_idx = first_run
                self._sel_run = (vis[first_run]["run"] if first_run >= 0
                                 else None)
            self.target_list.SelectedIndex = sel_idx
        finally:
            self._ui_updating = False

    def _index_of_run(self, run: TargetRun | None) -> int:
        """run 在当前可见列表中的行索引; 不可见(折叠/跨夜)返回 -1。"""
        if run is None:
            return -1
        for i, it in enumerate(self._list_items):
            if it["kind"] == "run" and it["run"] is run:
                return i
        return -1

    def _restore_selection(self) -> None:
        """把 ListView 选中恢复到当前选中目标(兜底: 万一选到不可选行)。"""
        idx = self._index_of_run(self._sel_run)
        self._ui_updating = True
        try:
            self.target_list.SelectedIndex = idx
        finally:
            self._ui_updating = False

    def _toggle_group(self, key: str, night: str | None) -> None:
        """折叠/展开一个 Plan 组(折叠态按夜次记忆, 切夜次回来仍保持)。

        night = 发起折叠时的夜次日期; 延后执行期间用户已切夜次则作废。
        """
        if night is None or night != self._night_date:
            return
        k = (self._night_date, key)
        if k in self._collapsed:
            self._collapsed.discard(k)
        else:
            self._collapsed.add(k)
        prev = self._sel_run
        self._render_list()                 # 内部已恢复选中(_ui_updating 保护)
        if self._sel_run is not prev:
            self._show_detail(self._sel_run)
            self._draw_sky()
            self._update_sky_bg()

    # ---------- 行控件 ----------

    def _track_columns(self, g: Grid) -> None:
        """目标列表行的两列: 时间轨道列(定宽) + 内容列(自适应)。"""
        c0 = ColumnDefinition()
        c0.Width = GridLength(Value=TRACK_W, GridUnitType=GridUnitType.Pixel)
        g.ColumnDefinitions.Append(c0)
        c1 = ColumnDefinition()
        c1.Width = GridLength(Value=1.0, GridUnitType=GridUnitType.Star)
        g.ColumnDefinitions.Append(c1)

    def _build_track_grid(self) -> Grid:
        """轨道列内部的两列: 时刻文字(自适应) + 竖线/圆点(定宽)。
        目标行与间隙行共用, 保证竖线 x 位置逐行一致(轨道才连得上)。"""
        track = Grid()
        track.ColumnSpacing = TRACK_GAP
        ct = ColumnDefinition()
        ct.Width = GridLength(Value=1.0, GridUnitType=GridUnitType.Star)
        track.ColumnDefinitions.Append(ct)
        cr = ColumnDefinition()
        cr.Width = GridLength(Value=TRACK_RAIL_W, GridUnitType=GridUnitType.Pixel)
        track.ColumnDefinitions.Append(cr)
        return track

    def _build_target_row(self) -> dict:
        """目标行: Grid[时间轨道列(时刻 + 状态色圆点 + 上下竖线) | 内容列]。

        行对象持久化, 更新时只改文本/画刷/可见性(不新建控件与画刷);
        下连接线不设 Height —— Rectangle 默认纵向拉伸填满行高, 与事件
        时间线的轨道同款做法, 行间零内边距让竖线跨行连续。
        """
        root = Grid()
        self._track_columns(root)

        # --- 轨道列: 左=时刻(等宽小字), 右=圆点与竖线 ---
        track = self._build_track_grid()
        tm = TextBlock()
        tm.FontSize = 11
        tm.FontFamily = self._mono_font
        tm.Opacity = 0.55
        tm.HorizontalAlignment = HorizontalAlignment.Right
        tm.VerticalAlignment = VerticalAlignment.Top
        tm.Margin = Thickness(Left=0, Top=8, Right=0, Bottom=0)
        Grid.SetColumn(tm, 0)
        track.Children.Append(tm)

        rail = Grid()
        up = Rectangle()
        up.Width = 2.0
        up.Height = _DOT_TOP
        up.Fill = self._b_rail
        up.HorizontalAlignment = HorizontalAlignment.Center
        up.VerticalAlignment = VerticalAlignment.Top
        rail.Children.Append(up)
        dot = Ellipse()
        dot.Width = dot.Height = 9.0
        dot.HorizontalAlignment = HorizontalAlignment.Center
        dot.VerticalAlignment = VerticalAlignment.Top
        dot.Margin = Thickness(Left=0, Top=_DOT_TOP, Right=0, Bottom=0)
        rail.Children.Append(dot)
        down = Rectangle()
        down.Width = 2.0
        down.Fill = self._b_rail
        down.HorizontalAlignment = HorizontalAlignment.Center
        down.Margin = Thickness(Left=0, Top=_DOT_BOT, Right=0, Bottom=0)
        rail.Children.Append(down)
        Grid.SetColumn(rail, 1)
        track.Children.Append(rail)
        Grid.SetColumn(track, 0)
        root.Children.Append(track)

        # --- 内容列: 状态字符 + 目标名 + 计划号, 副行统计 ---
        content = StackPanel()
        content.Spacing = 2
        content.Margin = Thickness(Left=0, Top=6, Right=0, Bottom=6)
        head = StackPanel()
        head.Orientation = Orientation.Horizontal
        head.Spacing = 6
        icon = TextBlock()
        icon.FontSize = 14
        icon.VerticalAlignment = VerticalAlignment.Center
        head.Children.Append(icon)
        name = TextBlock()
        name.FontWeight = FontWeights.SemiBold
        name.TextTrimming = TextTrimming.CharacterEllipsis
        name.VerticalAlignment = VerticalAlignment.Center
        head.Children.Append(name)
        plan = TextBlock()
        plan.FontSize = 11
        plan.Opacity = 0.6
        plan.VerticalAlignment = VerticalAlignment.Center
        head.Children.Append(plan)
        content.Children.Append(head)
        sub = TextBlock()
        sub.FontSize = 11
        sub.Opacity = 0.7
        sub.TextWrapping = TextWrapping.Wrap
        content.Children.Append(sub)
        Grid.SetColumn(content, 1)
        root.Children.Append(content)

        return {"root": root, "time": tm, "dot": dot, "up": up, "down": down,
                "icon": icon, "name": name, "plan": plan, "sub": sub}

    def _update_target_row(self, row: dict, run: TargetRun, item: dict,
                           up: bool, down: bool) -> None:
        """把工作线程算好的行字段贴到持久化行控件上(零计算)。"""
        rd = self._row_map.get(id(run))
        if rd is None:                      # 兜底(正常路径已在工作线程算好)
            rd = _run_row_data(run, self._guide_map,
                               guide_pending=self._guide_pending)
        brush = self._tl_level_brush.get(rd["level"], self._b_infoblue)
        row["time"].Text = rd["time"]
        row["dot"].Fill = brush
        row["icon"].Text = rd["mark"]
        row["icon"].Foreground = brush
        row["name"].Text = rd["name"]
        row["plan"].Text = rd["plan"]
        # 分组视图里组头已经写明计划号, 行内不再重复
        row["plan"].Visibility = (Visibility.Collapsed if item.get("group")
                                  else Visibility.Visible)
        row["sub"].Text = rd["sub"]
        row["up"].Visibility = Visibility.Visible if up else Visibility.Collapsed
        row["down"].Visibility = (Visibility.Visible if down
                                  else Visibility.Collapsed)
        row["root"].Margin = Thickness(Left=item.get("indent", 0.0), Top=0,
                                       Right=0, Bottom=0)

    def _inert_row(self, child, hit_test: bool) -> ListViewItem:
        """把组头/间隙行包进**不可聚焦**的 ListViewItem 容器(自带容器条目)。

        以前这两类行是裸元素, ListView 会给它们生成普通容器, 于是键盘上下键
        会反复落在组头/间隙上(还会误折叠计划组), 只能靠选中后再退回。改成
        自带容器并 IsTabStop=False + AllowFocusOnInteraction=False 后,
        ListView 的键盘导航直接跳过它们。

        容器的 Padding/MinHeight/内容对齐显式设成与 records.xaml 里
        ItemContainerStyle 相同的值 —— 自带容器不保证套得上该样式, 而轨道
        竖线跨行连续依赖**零垂直内边距**, 少设一条竖线就断。
        """
        li = ListViewItem()
        li.Content = child
        li.HorizontalContentAlignment = HorizontalAlignment.Stretch
        li.Padding = Thickness(Left=10, Top=0, Right=10, Bottom=0)
        li.MinHeight = 0.0
        li.IsTabStop = False
        li.AllowFocusOnInteraction = False
        if not hit_test:
            li.IsHitTestVisible = False
        return li

    def _build_list_gap_row(self, it: dict) -> Grid:
        """间隙细行: 轨道竖线照旧贯穿, 内容列居中淡字(不可选中)。"""
        g = Grid()
        g.Margin = Thickness(Left=it.get("indent", 0.0), Top=0, Right=0, Bottom=0)
        self._track_columns(g)

        track = self._build_track_grid()
        line = Rectangle()
        line.Width = 2.0
        line.Fill = self._b_rail
        line.HorizontalAlignment = HorizontalAlignment.Center
        Grid.SetColumn(line, 1)
        track.Children.Append(line)
        Grid.SetColumn(track, 0)
        g.Children.Append(track)

        tb = TextBlock()
        tb.Text = f"—  {it['text']}  —"
        tb.FontSize = 11
        tb.Opacity = 0.5
        tb.HorizontalAlignment = HorizontalAlignment.Center
        tb.Margin = Thickness(Left=0, Top=3, Right=0, Bottom=3)
        Grid.SetColumn(tb, 1)
        g.Children.Append(tb)
        return g

    def _group_row(self, it: dict) -> ListViewItem:
        """Plan 组头行(**按组键缓存复用**), 命中缓存时只改箭头与两行文字。

        为什么必须复用: `_render_list` 每次折叠/展开/切夜次/切「合并计划」都会
        重铺整张列表, 而 win32more 的 event 描述符(`_winrt.py` 的
        `event.__get__`)把实例存进**类级** `_event_setters[id(instance)]` 且
        **从不移除**(`-=` 与 `clear()` 只清 `_callbacks`)—— 每次新建组头卡片
        就等于永久滞留一个 Border 和它的 Tapped 闭包。真机实测: 2 个组 × 4 轮
        重铺 → Tapped 条目 2/4/6/8 一路涨, 复用后恒为 2。补 `-=` 是无效的。

        缓存的是**整个 ListViewItem 容器**而不只是里面的 Border: 容器每次新建
        会让同一个 Border 被新旧两个 ContentControl 争当逻辑父, 复用容器就
        完全不存在重挂父的问题(ListView 的 Items 里放的就是它自己)。

        组键(`p3`/`solo`)只在**夜次内**唯一, 跨夜会重名 —— 所以命中缓存时
        必须把标题/副行文字一起更新, 否则会显示上一夜的起止时刻(`_sky3d`
        的行缓存踩过同款坑)。
        """
        key = it["key"]
        cached = self._group_cards.get(key)
        collapsed = (self._night_date, key) in self._collapsed
        if cached is not None:
            cached["chev"].Text = "▸" if collapsed else "▾"
            cached["title"].Text = it["title"]
            cached["sub"].Text = it["sub"]
            return cached["item"]
        card = Border()
        card.Background = self._b_card_bg
        card.CornerRadius = _corner(4.0)
        card.Padding = Thickness(Left=8, Top=5, Right=8, Bottom=5)
        card.Margin = Thickness(Left=0, Top=5, Right=0, Bottom=2)
        sp = StackPanel()
        sp.Orientation = Orientation.Horizontal
        sp.Spacing = 6
        chev = TextBlock()
        chev.Text = "▸" if collapsed else "▾"
        chev.FontSize = 11
        chev.Opacity = 0.7
        chev.VerticalAlignment = VerticalAlignment.Center
        sp.Children.Append(chev)
        title = TextBlock()
        title.Text = it["title"]
        title.FontSize = 13
        title.FontWeight = FontWeights.SemiBold
        title.VerticalAlignment = VerticalAlignment.Center
        sp.Children.Append(title)
        sub = TextBlock()
        sub.Text = it["sub"]
        sub.FontSize = 11
        sub.Opacity = 0.6
        sub.TextTrimming = TextTrimming.CharacterEllipsis
        sub.VerticalAlignment = VerticalAlignment.Center
        sp.Children.Append(sub)
        card.Child = sp
        try:
            ToolTipService.SetToolTip(card, _("点击折叠/展开该计划"))
        except Exception:
            pass
        # 组键在闭包里是**常量**(缓存命中时不重挂), 折叠用的夜次在处理器里现取
        card.Tapped += (lambda s, e, k=key: self._on_group_tapped(k, e))
        item = self._inert_row(card, hit_test=True)
        self._group_cards[key] = {"item": item, "chev": chev,
                                  "title": title, "sub": sub}
        return item

    def _on_group_tapped(self, key: str, e) -> None:
        """组头行点击 → 折叠/展开(标记事件已处理, 尽量别再冒泡成行选中)。

        折叠动作**延后一拍**: 在指针事件里直接 Items.Clear() 重建列表(会连
        自己一起拆掉)风险高, 交给 dispatcher 下一轮做。
        """
        try:
            e.Handled = True
        except Exception:
            pass
        self.shell.ui(self._toggle_group, key, self._night_date)

    def _run_badge(self, run: TargetRun) -> tuple[str, SolidColorBrush]:
        """状态字符 + 状态色画刷(列表行图标与天球点共用)。"""
        level = _run_level(run)
        return (_LEVEL_MARK.get(level, "·"),
                self._tl_level_brush.get(level, self._b_infoblue))

    # ---------- 夜次统计卡 / 整夜时间轴 ----------

    def _update_summary_ui(self) -> None:
        """按当前夜次刷新统计卡与时间轴卡的可见性/文本(数据已在工作线程算好)。"""
        stats = (self._night_stats.get(self._night_date)
                 if self._night_date is not None else None)
        if stats is not None:
            self.stats_left.Text, self.stats_right.Text = stats
            self.stats_card.Visibility = Visibility.Visible
        else:
            self.stats_card.Visibility = Visibility.Collapsed
        tl = (self._night_tl.get(self._night_date)
              if self._night_date is not None else None)
        self.tl_card.Visibility = (Visibility.Visible if tl and tl["bars"]
                                   else Visibility.Collapsed)

    def _draw_timeline(self) -> None:
        """整夜时间轴甘特图: 上=目标块横条, 中=导星覆盖细条, 下=小时刻度。
        只做归一化区间 → 像素的映射, 无任何统计计算。

        **整批一次 XamlReader.Load**: 一夜能画到两百多个图元(真机 2026-07-25
        那夜光导星覆盖条就有 212 条), 逐个建元素实测 410~455ms —— 而画布
        SizeChanged 每次拖窗口都会重画一遍。拼成 XAML 文本交给 C++ 解析器后
        降到个位数毫秒。产物仍是**各自独立的元素**, z 序与半透明叠加不变。
        片段解析失败退回逐元素慢路径(慢, 但一定画得出来)。

        **横条不再逐个挂 Tapped/ToolTip**: win32more 的 event 描述符把实例存进
        类级 `_event_setters` 且从不移除, 每重画一次就永久滞留一批 Rectangle
        与闭包(实测 13 条横条 × 4 轮 → 52 个条目, 闭包还 pin 住 TargetRun)。
        改成画布上只有 `_wire` 注册的那**一个** Tapped, 命中横条靠
        `timeline_hit_bar` 反算; 提示文本走画布上唯一的 ToolTip(悬停时换内容)。
        """
        canvas = self.tl_canvas
        canvas.Children.Clear()
        self._tl_hit = []
        self._tl_w = 0.0
        self._tl_tip_idx = -2
        tl = (self._night_tl.get(self._night_date)
              if self._night_date is not None else None)
        if not tl or not tl["bars"]:
            return
        w = float(canvas.ActualWidth or 0)
        if w < 80:
            return

        # ---- 先算几何(纯 Python), 再一次性铺到画布上 ----
        lines: list[tuple] = []         # (x1, y1, x2, y2, 画刷, 粗细)
        labels: list[tuple] = []        # (x, y, 文本, 字号, 画刷, 宽度|None)
        guides: list[tuple] = []        # (x, y, w, h, 画刷)
        bars: list[tuple] = []          # (x, y, w, h, 画刷, 不透明度)
        bar_labels: list[tuple] = []
        hit: list[tuple] = []           # (f0, f1, run, 提示文本) 与 bars 同序

        # 小时刻度: 竖线 + 'HH:MM' 标签(标签不出画布边界)
        for frac, label in tl["ticks"]:
            x = frac * w
            lines.append((x, 2.0, x, TL_TICK_Y - 2.0, self._b_grid_dim, 1.0))
            labels.append((max(0.0, min(x - 15.0, w - 32.0)), TL_TICK_Y,
                           label, 10.0, self._b_label, None))

        # 导星覆盖细条(半透明绿)
        for f0, f1 in tl["guides"]:
            guides.append((f0 * w, TL_GUIDE_Y, max(1.0, (f1 - f0) * w),
                           TL_GUIDE_H, self._tl_guide))

        # 目标块横条(点击选中目标; 空间够时写目标名)
        for f0, f1, ci, alpha, label, tip, run in tl["bars"]:
            x0, bw = timeline_bar_px(f0, f1, w)
            bars.append((x0, TL_BAR_Y, bw, TL_BAR_H,
                         self._tl_brushes[ci], alpha))
            hit.append((f0, f1, run, tip))
            if bw >= TL_LABEL_MIN_W:
                bar_labels.append((x0 + 4.0, TL_BAR_Y + 0.5, label, 10.0,
                                   self._b_white, bw - 8.0))
        self._tl_hit = hit
        self._tl_w = w                  # 命中反算必须用**这次画的**宽度

        try:
            frag = batch_canvas(
                line_fragment([(x1, y1, x2, y2, argb_hex(b), t)
                               for x1, y1, x2, y2, b, t in lines]),
                self._text_frag(labels),
                rect_fragment([(x, y, ww, hh, argb_hex(b))
                               for x, y, ww, hh, b in guides], radius=1.0),
                rect_fragment([(x, y, ww, hh,
                                scale_alpha(argb_hex(b), a))
                               for x, y, ww, hh, b, a in bars], radius=2.0),
                self._text_frag(bar_labels))
            if frag:
                canvas.Children.Append(XamlReader.Load(frag).as_(Canvas))
            return
        except Exception:
            canvas.Children.Clear()     # 半截片段绝不能留在画布上
        self._draw_timeline_slow(canvas, lines, labels, guides, bars,
                                 bar_labels)

    @staticmethod
    def _text_frag(items) -> str:
        """(x, y, 文本, 字号, **画刷**, 宽度) → 批量文本片段(画刷转 #AARRGGBB)。"""
        return text_fragment([(x, y, t, s, argb_hex(b), ww)
                              for x, y, t, s, b, ww in items])

    def _draw_timeline_slow(self, canvas, lines, labels, guides, bars,
                            bar_labels) -> None:
        """逐元素兜底路径(批量片段解析失败时用): 几何与快路径逐像素一致。

        慢(真机两百多个图元要 400ms 以上), 但保证甘特图一定画得出来。
        """
        for x1, y1, x2, y2, brush, thick in lines:
            ln = Line()
            ln.X1, ln.Y1, ln.X2, ln.Y2 = x1, y1, x2, y2
            ln.Stroke = brush
            ln.StrokeThickness = thick
            canvas.Children.Append(ln)
        for x, y, ww, hh, brush in guides:
            canvas.Children.Append(
                self._slow_rect(x, y, ww, hh, brush, 1.0, radius=1.0))
        for x, y, ww, hh, brush, alpha in bars:
            canvas.Children.Append(
                self._slow_rect(x, y, ww, hh, brush, alpha, radius=2.0))
        for x, y, text, size, brush, width in labels + bar_labels:
            tb = TextBlock()
            tb.Text = text
            tb.FontSize = size
            tb.Foreground = brush
            tb.IsHitTestVisible = False
            if width is not None:
                tb.Width = width
                tb.TextTrimming = TextTrimming.CharacterEllipsis
            Canvas.SetLeft(tb, x)
            Canvas.SetTop(tb, y)
            canvas.Children.Append(tb)

    @staticmethod
    def _slow_rect(x, y, w, h, brush, alpha, radius=0.0) -> Rectangle:
        rect = Rectangle()
        rect.Width, rect.Height = w, h
        rect.Fill = brush
        if alpha < 0.999:
            rect.Opacity = alpha
        if radius > 0:
            rect.RadiusX = rect.RadiusY = radius
        Canvas.SetLeft(rect, x)
        Canvas.SetTop(rect, y)
        return rect

    def _tl_spans(self) -> list[tuple]:
        return [(h[0], h[1]) for h in self._tl_hit]

    def _on_timeline_tapped(self, sender, e) -> None:
        """时间轴画布上**唯一**的 Tapped: 反算命中的横条, 再选中对应目标。

        坐标必须在处理器内**同步**取出(事件参数不能跨帧持有, 与导星仪表盘的
        分段对比条同款做法)。
        """
        if not self._tl_hit:
            return
        try:
            p = e.GetPosition(self.tl_canvas)
            k = timeline_hit_bar(float(p.X), float(p.Y), self._tl_spans(),
                                 self._tl_w)
        except Exception:
            return
        if k is None:
            return
        self._select_run_from_timeline(self._tl_hit[k][2])

    def _on_timeline_moved(self, sender, e) -> None:
        """悬停 → 换画布上唯一那个 ToolTip 的内容(替代逐条挂 ToolTip)。"""
        tip = self._tl_tip
        if tip is None:
            return
        k = None
        if self._tl_hit:
            try:
                p = e.GetCurrentPoint(self.tl_canvas).Position
                k = timeline_hit_bar(float(p.X), float(p.Y), self._tl_spans(),
                                     self._tl_w)
            except Exception:
                k = None
        if k == self._tl_tip_idx:
            return
        self._tl_tip_idx = k
        try:
            if k is None:
                tip.IsOpen = False
            else:
                tip.Content = self._tl_hit[k][3]
                tip.IsOpen = True
        except Exception:
            pass

    def _on_timeline_exited(self, sender, e) -> None:
        """指针离开画布: 关掉提示并把命中记忆清成"未知"(回来必定重算)。"""
        self._tl_tip_idx = -2
        if self._tl_tip is not None:
            try:
                self._tl_tip.IsOpen = False
            except Exception:
                pass

    def _select_run_from_timeline(self, run: TargetRun) -> None:
        """点击时间轴横条 → 选中左侧列表对应目标(按对象身份匹配)。

        目标所在 Plan 组处于折叠态时先展开再选中, 否则点了没反应。
        """
        idx = self._index_of_run(run)
        if idx < 0:
            layout = (self._night_layout.get(self._night_date)
                      if self._night_date is not None else None)
            for it in (layout or {}).get("grouped") or []:
                if (it["kind"] == "run" and it["run"] is run
                        and it.get("group")):
                    self._collapsed.discard((self._night_date, it["group"]))
                    break
            self._render_list()
            idx = self._index_of_run(run)
        if idx < 0:
            return
        if self.target_list.SelectedIndex != idx:
            self.target_list.SelectedIndex = idx     # 触发 _on_target_selected
        elif self._sel_run is not run:
            self._sel_run = run
            self._show_detail(run)
            self._draw_sky()
            self._update_sky_bg()

    # ---------- 详情 ----------

    def _on_target_selected(self, sender, e) -> None:
        """列表选中变化。组头/间隙行已是不可聚焦容器, 这里只做兜底。"""
        if self._ui_updating:
            return
        idx = self.target_list.SelectedIndex
        if idx is None or not (0 <= idx < len(self._list_items)):
            return
        it = self._list_items[idx]
        if it["kind"] != "run":
            # 正常走不到(容器 IsTabStop=False / 间隙行还关了命中);
            # 万一某条路径仍选中了组头/间隙行, 退回原选择。折叠**不在**这里
            # 触发 —— 那是组头 Tapped 的事, 否则键盘一走过就误折叠
            self._restore_selection()
            return
        run = it["run"]
        if run is self._sel_run:
            return
        self._sel_run = run
        self._show_detail(run)
        self._draw_sky()
        self._update_sky_bg()               # 底图时刻跟随选中目标的帧中点

    def _show_detail(self, run: TargetRun | None) -> None:
        if run is None:
            self.detail_title.Text = (_("选择左侧目标查看详情") if self._runs
                                      else _("该夜次无拍摄记录"))
            self.detail_coord.Text = ""
            self._render_badges([])
            self._fill_kv(self.detail_grid, [])
            self._render_guide_quality(None)
            self.dir_hint.Text = ""
            self.open_files_btn.IsEnabled = False
            self.guiding_btn.IsEnabled = False
            self.timeline_list.Items.Clear()
            return

        # 渲染数据已在工作线程算好(徽章 + 两列 KV), 这里只赋值/搭控件
        d = self._detail_map.get(id(run))
        if d is None:                       # 兜底(正常路径已在工作线程算好)
            d = _run_detail(run, self._guide_map, self._fits_map,
                            guide_pending=self._guide_pending,
                            fits_pending=self._fits_pending)
        self.detail_title.Text = run.target
        self.detail_coord.Text = d["coord"]
        self._render_badges(d["badges"])
        self._fill_kv(self.detail_grid, d["pairs"])
        self._render_guide_quality(run)

        if self._plan_dirs is not None:
            has = run.target in self._plan_dirs
            self.dir_hint.Text = _("文件目录: {0}").format(_("有") if has else _("无"))
        elif self._fits_pending:            # 第一段: Plan 目录还没列
            self.dir_hint.Text = _("文件目录: 读取中…")
        else:
            self.dir_hint.Text = ""         # 列目录失败: 不下断言
        self.open_files_btn.IsEnabled = True
        self.guiding_btn.IsEnabled = True

        self._fill_timeline(run)

    def on_guide_quality_updated(self, run: TargetRun) -> None:
        """3D 足迹后台晚于页面切换完成时，就地补上当前目标的诊断卡。"""
        if run is self._sel_run:
            self._render_guide_quality(run)

    def _render_guide_quality(self, run: TargetRun | None) -> None:
        """把三证据导星结论放在具体拍摄记录下，而不是 3D 空间工具里。"""
        if run is None:
            self.guide_quality_card.Visibility = Visibility.Collapsed
            self.polar_card.Visibility = Visibility.Collapsed
            return
        quality = self.shell.guide_quality_for(run)
        state = self.shell.guide_quality_state_for(run)
        busy = bool(state.get("busy"))
        self.guide_quality_ring.IsActive = busy
        self.guide_quality_ring.Visibility = (
            Visibility.Visible if busy else Visibility.Collapsed)
        self.guide_quality_btn.IsEnabled = bool(self._connected)
        self.guide_quality_btn.Content = (
            _("停止分析") if busy else (_("重新分析") if quality is not None
                                    else _("开始分析")))
        self.guide_quality_card.Visibility = Visibility.Visible
        if quality is None:
            self.guide_quality_headline.Text = (
                str(state.get("text") or _("尚未分析拍摄结果")))
            self.guide_quality_headline.Foreground = (
                self._b_red if state.get("error") else self._b_label)
            self.guide_quality_confidence.Text = (
                _("将抽样原始 FITS，提取主镜 FWHM/椭率/方向，再与同期 PHD2 导星数据交叉判读"))
            self.guide_quality_findings.Text = ""
            self.guide_quality_findings.Visibility = Visibility.Collapsed
            self.polar_card.Visibility = Visibility.Collapsed
            return
        verdict = getattr(quality, "verdict", "unknown")
        brush = {
            "good": self._b_green,
            "drift": self._b_red,
            "overguide": self._b_amber,
            "unknown": self._b_label,
        }.get(verdict, self._b_label)
        confidence = {"high": _("高"), "medium": _("中"), "low": _("低")}.get(
            getattr(quality, "confidence", "low"), _("低"))
        self.guide_quality_headline.Text = str(
            getattr(quality, "headline", _("证据不足")))
        self.guide_quality_headline.Foreground = brush
        self.guide_quality_confidence.Text = (
            str(state.get("text")) if (busy or state.get("error")) else
            _("可信度 {confidence} · 成功板解算帧、主镜星点形状与同期 PHD2 交叉判读").format(
                confidence=confidence))
        self._render_polar(quality)
        findings = list(getattr(quality, "findings", ()) or ())
        self.guide_quality_findings.Text = "\n".join(
            f"· {line}" for line in findings)
        self.guide_quality_findings.Visibility = (
            Visibility.Visible if findings else Visibility.Collapsed)

    def _render_polar(self, quality) -> None:
        """极轴误差示意图 + 一句"该往哪拧"。

        没有极轴结论就整块收起 —— 空着一个画好的靶环比不画更容易被误读成
        "极轴没问题"。
        """
        polar = getattr(quality, "polar", None)
        if polar is None:
            self.polar_card.Visibility = Visibility.Collapsed
            return
        cond = float(getattr(quality, "polar_cond", float("inf")))
        # 单目标恰定 ⇒ 残差恒为 0,推翻不了;夜次级联合反解跨多个目标才有
        # 残差可看。**读结构化字段,不去 findings 里搜「恰定」两个字** ——
        # 那是拿会被翻译的显示文本当判据。
        falsifiable = bool(getattr(quality, "polar_falsifiable", False))
        self.polar_card.Visibility = Visibility.Visible
        self.polar_plot.Children.Clear()
        frag = polar_plot_fragment(
            polar, 132.0, ink=argb_hex(self._b_label, "#FF8A8A8A"),
            accent=argb_hex(self._b_red, "#FFE05A5A"),
            label=argb_hex(self._b_label, "#FF9A9A9A"))
        try:
            canvas = XamlReader.Load(frag).as_(Canvas)
        except Exception as ex:         # 片段拼错不许把整页拖垮
            print(_("极轴示意图渲染失败: {ex}").format(ex=ex), flush=True)
            self.polar_card.Visibility = Visibility.Collapsed
            return
        kids = list(canvas.Children)
        canvas.Children.Clear()         # 先摘下来才能挂到我们的 Canvas 上
        for el in kids:
            self.polar_plot.Children.Append(el)
        self.polar_total.Text = f"{polar.total_arcmin:.2f}′"
        self.polar_total.Foreground = (
            self._b_green if polar.total_arcmin <= 2.0 else
            self._b_amber if polar.total_arcmin <= 5.0 else self._b_red)
        self.polar_advice.Text = polar_advice(
            polar, cond=cond, falsifiable=falsifiable)
        consistent = getattr(quality, "polar_consistent", None)
        if consistent is False:
            self.polar_trust.Text = (
                _("注意:实测场旋与这个极轴值对不上,现场还有别的机制 —— 只拧极轴多半解决不了"))
        elif not falsifiable:
            self.polar_trust.Text = (
                _("只有一个目标时段,该结论恰定、推翻不了;同夜多个目标一起分析才能验证"))
        else:
            self.polar_trust.Text = _("同夜多目标联合反解,残差已用于验证")

    def _on_guide_quality(self, sender, e) -> None:
        """记录页直接发起分析；后台会逐步回写状态并最终刷新本卡。"""
        run = self._sel_run
        if run is None:
            return
        state = self.shell.guide_quality_state_for(run)
        if state.get("busy"):
            self.shell.cancel_guide_quality(run)
        else:
            self.shell.request_guide_quality(run)

    def _render_badges(self, badges: list[tuple[str, str]]) -> None:
        """详情徽章行: 圆角小胶囊(浅底深字), 状态/计划/帧型/滤镜/尝试次数。"""
        self.detail_badges.Children.Clear()
        for text, style in badges:
            bg, fg = self._badge_brushes.get(style, self._badge_brushes["info"])
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
            self.detail_badges.Children.Append(chip)
        self.detail_badges.Visibility = (Visibility.Visible if badges
                                         else Visibility.Collapsed)

    def _mini_bar(self, frac: float, level: str, width: float = 90.0) -> Border:
        """迷你占比条(槽 + 填充), 画刷全部复用预建实例。"""
        holder = Border()
        holder.Background = self._b_bar_bg
        holder.CornerRadius = _corner(2.5)
        holder.Width = width
        holder.Height = 5.0
        holder.VerticalAlignment = VerticalAlignment.Center
        holder.HorizontalAlignment = HorizontalAlignment.Left
        f = min(1.0, max(0.0, frac))
        if f > 0.0:
            bar = Rectangle()
            bar.Width = width * f
            bar.Height = 5.0
            bar.RadiusX = bar.RadiusY = 2.5
            bar.Fill = self._tl_level_brush.get(level, self._b_green)
            bar.HorizontalAlignment = HorizontalAlignment.Left
            holder.Child = bar
        return holder

    def _fill_kv(self, grid: Grid, pairs: list[dict]) -> None:
        """把 KV 条目渲染成两列 Grid(与浏览页详情同风格)。

        条目字段: k=标签(淡色), v=数值(可选中复制), note=淡色副注,
        mono=等宽字体, level=数值着色级别, bar=(占比, 级别) 迷你进度条。
        条目由工作线程算好, 这里零计算。
        """
        grid.RowDefinitions.Clear()
        grid.Children.Clear()
        for i, p in enumerate(pairs):
            rd = RowDefinition()
            rd.Height = GridLength(Value=1.0, GridUnitType=GridUnitType.Auto)
            grid.RowDefinitions.Append(rd)

            lab = TextBlock()
            lab.Text = p.get("k", "")
            lab.FontSize = 12
            lab.Opacity = 0.55
            lab.VerticalAlignment = VerticalAlignment.Center
            grid.Children.Append(lab)
            Grid.SetRow(lab, i)
            Grid.SetColumn(lab, 0)

            val = TextBlock()
            val.Text = p.get("v", "")
            val.FontSize = 12
            val.TextWrapping = TextWrapping.Wrap
            val.IsTextSelectionEnabled = True
            val.VerticalAlignment = VerticalAlignment.Center
            if p.get("mono"):
                val.FontFamily = self._mono_font
            lv = p.get("level")
            if lv:
                val.Foreground = self._tl_level_brush.get(lv, self._b_infoblue)
            bar, note = p.get("bar"), p.get("note")
            if bar is None and not note:
                # 无附件时直接放进单元格, 长文本才能正常换行
                grid.Children.Append(val)
                Grid.SetRow(val, i)
                Grid.SetColumn(val, 1)
                continue
            panel = StackPanel()
            panel.Orientation = Orientation.Horizontal
            panel.Spacing = 6
            panel.Children.Append(val)
            if bar is not None:
                panel.Children.Append(self._mini_bar(bar[0], bar[1]))
            if note:
                aux = TextBlock()
                aux.Text = note
                aux.FontSize = 11
                aux.Opacity = 0.55
                aux.VerticalAlignment = VerticalAlignment.Center
                panel.Children.Append(aux)
            grid.Children.Append(panel)
            Grid.SetRow(panel, i)
            Grid.SetColumn(panel, 1)

    # ---------- 事件时间线(反馈#4: 结构化 Timeline UI) ----------

    def _fill_timeline(self, run: TargetRun) -> None:
        """结构化时间线渲染。条目已在工作线程归并到 ~10-30 条
        (self._timeline_map), 此处只搭控件, 无统计计算。"""
        self.timeline_list.Items.Clear()
        items = self._timeline_map.get(id(run))
        if items is None:
            items = _timeline_items(run)    # 兜底(正常路径已在工作线程算好)
        n = len(items)
        for i, it in enumerate(items):
            if it["kind"] == "gap":
                self.timeline_list.Items.Append(self._build_gap_row(it))
            else:
                self.timeline_list.Items.Append(
                    self._build_timeline_row(it, i == 0, i == n - 1))

    def _build_gap_row(self, it: dict) -> TextBlock:
        """间隙分隔条目: 居中小字, 无卡片。"""
        tb = TextBlock()
        tb.Text = f"—  {it['title']}  —"
        tb.FontSize = 11
        tb.Opacity = 0.55
        tb.HorizontalAlignment = HorizontalAlignment.Center
        tb.Margin = Thickness(Left=0, Top=2, Right=0, Bottom=2)
        return tb

    def _build_timeline_row(self, it: dict, first: bool, last: bool) -> Grid:
        """时间线行: Grid[时刻列 | 轨道列(标记+连接竖线) | 卡片列]。
        画刷全部复用预建实例; 首/末行分别不画上/下连接线。"""
        g = Grid()
        g.ColumnSpacing = 6
        for w, u in ((62, GridUnitType.Pixel), (14, GridUnitType.Pixel),
                     (1, GridUnitType.Star)):
            c = ColumnDefinition()
            c.Width = GridLength(Value=float(w), GridUnitType=u)
            g.ColumnDefinitions.Append(c)

        # 时刻列(带跨度的卡片第二行显示 ~结束时刻)
        t_txt = f"{it['t0']:%H:%M:%S}"
        t1 = it.get("t1")
        if t1 is not None and t1 != it["t0"]:
            t_txt += f"\n~{t1:%H:%M:%S}"
        tt = TextBlock()
        tt.Text = t_txt
        tt.FontSize = 11
        tt.FontFamily = self._mono_font
        tt.Opacity = 0.7
        tt.Margin = Thickness(Left=0, Top=8, Right=0, Bottom=0)
        Grid.SetColumn(tt, 0)
        g.Children.Append(tt)

        # 轨道列: 上连接线 + 状态色标记(块边界=方形旗标, 其余=圆点) + 下连接线
        track = Grid()
        if not first:
            top = Rectangle()
            top.Width = 2.0
            top.Height = 12.0
            top.Fill = self._b_rail
            top.HorizontalAlignment = HorizontalAlignment.Center
            top.VerticalAlignment = VerticalAlignment.Top
            track.Children.Append(top)
        brush = self._tl_level_brush.get(it["level"], self._b_infoblue)
        if it["kind"] == "block":
            mark = Rectangle()
            mark.Width = mark.Height = 9.0
            mark.RadiusX = mark.RadiusY = 2.0
            mark.Fill = brush
        else:
            mark = Ellipse()
            mark.Width = mark.Height = 9.0
            mark.Fill = brush
        mark.HorizontalAlignment = HorizontalAlignment.Center
        mark.VerticalAlignment = VerticalAlignment.Top
        mark.Margin = Thickness(Left=0, Top=12, Right=0, Bottom=0)
        track.Children.Append(mark)
        if not last:
            bot = Rectangle()
            bot.Width = 2.0
            bot.Fill = self._b_rail
            bot.HorizontalAlignment = HorizontalAlignment.Center
            bot.Margin = Thickness(Left=0, Top=23, Right=0, Bottom=0)
            track.Children.Append(bot)
        Grid.SetColumn(track, 1)
        g.Children.Append(track)

        # 卡片列: 圆角浅底 Border, 标题 + 副标题 + 可选迷你进度条
        card = Border()
        card.Background = self._b_card_bg
        card.CornerRadius = _corner(6.0)
        card.Padding = Thickness(Left=10, Top=6, Right=10, Bottom=6)
        card.Margin = Thickness(Left=0, Top=1, Right=0, Bottom=3)
        inner = StackPanel()
        inner.Spacing = 2
        title = TextBlock()
        title.Text = it["title"]
        title.FontSize = 13
        title.FontWeight = FontWeights.SemiBold
        title.TextWrapping = TextWrapping.Wrap
        inner.Children.Append(title)
        if it.get("subtitle"):
            sub = TextBlock()
            sub.Text = it["subtitle"]
            sub.FontSize = 11
            sub.Opacity = 0.65
            sub.TextWrapping = TextWrapping.Wrap
            inner.Children.Append(sub)
        prog = it.get("progress")
        if prog:
            actual, planned = prog
            frac = min(1.0, actual / planned) if planned > 0 else 0.0
            rowp = StackPanel()
            rowp.Orientation = Orientation.Horizontal
            rowp.Spacing = 6
            holder = Border()
            holder.Background = self._b_bar_bg
            holder.CornerRadius = _corner(2.5)
            holder.Width = 120.0
            holder.Height = 5.0
            holder.VerticalAlignment = VerticalAlignment.Center
            holder.HorizontalAlignment = HorizontalAlignment.Left
            if frac > 0.0:
                bar = Rectangle()
                bar.Width = 120.0 * frac
                bar.Height = 5.0
                bar.RadiusX = bar.RadiusY = 2.5
                bar.Fill = self._b_green if actual >= planned else self._b_amber
                bar.HorizontalAlignment = HorizontalAlignment.Left
                holder.Child = bar
            rowp.Children.Append(holder)
            ptxt = TextBlock()
            ptxt.Text = f"{actual}/{planned}"
            ptxt.FontSize = 10
            ptxt.Opacity = 0.6
            ptxt.VerticalAlignment = VerticalAlignment.Center
            rowp.Children.Append(ptxt)
            inner.Children.Append(rowp)
        card.Child = inner
        Grid.SetColumn(card, 2)
        g.Children.Append(card)
        return g

    # ---------- 按钮 ----------

    def _on_open_files(self, sender, e) -> None:
        run = self._sel_run
        if run is None:
            return
        if run.plan_no is None:
            self.shell.info(_("该目标非多目标计划拍摄, 无固定文件目录(校准帧在 Autorun/<类型> 下)"))
            return
        try:
            self.shell.open_browser_path(
                self._data_share(), PLAN_LIGHT_DIR + "\\" + run.target)
        except Exception as ex:
            self.shell.error(_("打开浏览页失败: {ex}").format(ex=ex))

    def _on_guiding(self, sender, e) -> None:
        run = self._sel_run
        if run is None:
            return
        span = run.frame_span()
        if span is not None:
            t0, t1 = span
        else:
            t0, t1 = run.begin_time, (run.end_time or run.begin_time)
        try:
            self.shell.open_guiding(t0, t1, run.target)
        except Exception as ex:
            self.shell.error(_("打开导星详情失败: {ex}").format(ex=ex))

    # ---------- 站点设置 ----------

    def _site_latlon(self) -> tuple[float, float]:
        """当前生效的 (纬度, 经度): 经度优先用日志推算值(lon_auto 恒 True)。

        推算值取自 ``self._lon_est`` 而非 ``self.data`` —— 懒加载第一段只有
        ``LogSummary``(不写 self.data)但它同样带经度, 用上才不会首屏先按
        默认经度画一遍天球、第二段再整体跳一次。
        """
        lat = float(self._site.get("lat", 30.0))
        if self._lon_est is not None:
            lon = float(self._lon_est)
        else:
            lon = float(self._site.get("lon", 120.0))
        return lat, lon

    def _update_site_ui(self) -> None:
        _lat, lon = self._site_latlon()
        est = self._lon_est is not None
        self.lon_text.Text = _fmt_lon(lon) + (_("(推算)") if est else _("(默认)"))

    def _on_site_apply(self, sender, e) -> None:
        try:
            lat = float(self.lat_box.Text.strip())
        except (ValueError, AttributeError):
            self.shell.error(_("纬度格式无效, 应为数字(北纬为正, 如 30.0)"))
            return
        lat = max(-90.0, min(90.0, lat))
        self._site["lat"] = lat
        _lat, lon = self._site_latlon()
        self._site["lon"] = lon
        save_site(lat, lon, True)           # lon_auto 恒 True
        self.lat_box.Text = f"{lat:g}"
        self._update_site_ui()
        if self.data is not None:
            self.status_text.Text = self._status_line()
        self._draw_sky()
        self._update_sky_bg()               # 站点变化 → 底图重投影
        self.shell.info(_("站点已保存: {0} / {1}").format(_fmt_lat(lat), _fmt_lon(lon)))

    # ---------- 天球图 ----------

    def _run_coords(self, run: TargetRun) -> tuple[float | None, float | None]:
        """目标坐标(度): 优先 FITS 头实测值(角秒级), 无则回退日志 slew
        坐标(goto 请求值, 精度低)。天球图与放大遮罩共用。"""
        info = self._fits_map.get(id(run))
        if info and "ra_deg" in info:
            return info["ra_deg"], info["dec_deg"]
        return astro.ra_str_to_deg(run.ra), astro.dec_str_to_deg(run.dec)

    def _dash_collection(self):
        """StrokeDashArray 用 DoubleCollection; 投射不可用时返回 None(退化为实线)。"""
        try:
            dc = DoubleCollection()
            dc.Append(3.0)
            dc.Append(4.0)
            return dc
        except Exception:
            return None

    # ---------- 天球放大遮罩层(带时刻滑杆) ----------
    # ContentDialog 内容宽度上限 ~548px 会硬裁大图(真机踩过),故用页面内遮罩层。
    # 流畅性设计(真机踩过"闪/卡/不同步"):
    # ① 场景持久化——框架画一次,滑杆 tick 只对目标点/标签做 SetLeft/SetTop
    #    原地移动(绝不 Children.Clear 重建,监控页同款模式);
    # ② 底图预热——打开时后台按 15 分钟桶把整夜底图批量重投影(磁盘缓存,
    #    二次打开秒热),拖动时直接换最近的已缓存帧,不会"慢一拍弹入"。

    _OV_BUCKET = 900.0      # 底图时间桶(秒):15 分钟一帧(≈3.8° 天球旋转)

    def _ov_night_window(self):
        """滑杆时间范围:当前夜次起止;无数据时以 _sky_ts ±6h 兜底。"""
        begin = end = None
        if self._runs:
            begin = min(r.begin_time for r in self._runs)
            end = max((r.end_time or r.begin_time) for r in self._runs)
        if begin is None or end is None or end <= begin:
            mid = datetime.fromtimestamp(self._sky_ts())
            begin, end = mid - timedelta(hours=6), mid + timedelta(hours=6)
        return begin, end

    def _open_sky_overlay(self, sender, e) -> None:
        w = float(self.root.ActualWidth or 0) or 1200.0
        h = float(self.root.ActualHeight or 0) or 800.0
        size = max(480.0, min(w - 320.0, h - 190.0, 960.0))
        self._ov_size = size
        self.ov_holder.Width = self.ov_holder.Height = size
        self.ov_canvas.Width = self.ov_canvas.Height = size
        for img in (self.ov_image_a, self.ov_image_b):
            img.Width = img.Height = size - 40.0
        # 双缓冲复位(尺寸可能变了,BitmapImage 缓存作废)
        self._ov_front, self._ov_back = self.ov_image_a, self.ov_image_b
        self.ov_image_a.Opacity = 1.0
        self.ov_image_b.Opacity = 0.0
        self._ov_bmp.clear()
        self._ov_lru.clear()
        self._ov_ready.clear()
        self._ov_pending = None
        self.ov_title.Text = _("全天位置(仰视: 北上 · 东左)")

        begin, end = self._ov_night_window()
        self._ov_begin = begin
        span_min = max(1.0, (end - begin).total_seconds() / 60.0)
        self._ui_updating = True
        try:
            self.ov_slider.Minimum = 0.0
            self.ov_slider.Maximum = span_min
            cur = (self._sky_ts() - astro.unix_from_local(begin)) / 60.0
            self.ov_slider.Value = min(max(cur, 0.0), span_min)
        finally:
            self._ui_updating = False

        self._ov_open = True
        self._ov_warm_gen += 1              # 作废任何陈旧预热 worker
        self._ov_bg_paths: dict[int, str] = {}
        self._ov_bg_shown: int | None = None
        self._ov_pending = None
        self._ov_back_bucket = None
        self.sky_overlay.Visibility = Visibility.Visible
        self._ov_build_scene()
        self._ov_tick()
        use_bg = self._survey_on and skymap.survey_available()
        self.ov_credit.Text = _(skymap.SURVEY_CREDIT) if use_bg else ""
        if use_bg:
            self._ov_start_warm(begin, end)
        else:
            for img in (self.ov_image_a, self.ov_image_b):
                try:
                    img.put_Source(None)
                except Exception:
                    pass

    def _close_sky_overlay(self, sender, e) -> None:
        self._ov_open = False               # 预热 worker 见状自然退出
        self._ov_warm_gen += 1              # 双保险:代次失配立即退出
        self.sky_overlay.Visibility = Visibility.Collapsed
        self.ov_canvas.Children.Clear()
        self._ov_dots = []
        self._ov_bmp.clear()
        self._ov_lru.clear()
        self._ov_ready.clear()
        self._ov_pending = None
        self._ov_back_bucket = None
        for img in (self.ov_image_a, self.ov_image_b):
            try:
                img.put_Source(None)
            except Exception:
                pass

    def _ov_ts(self) -> float:
        base = self._ov_begin or datetime.fromtimestamp(self._sky_ts())
        return astro.unix_from_local(base) + float(self.ov_slider.Value) * 60.0

    def _on_ov_slider(self, sender, e) -> None:
        if not self._ov_open or self._ui_updating:
            return
        self._ov_tick()

    # ----- 场景:框架一次 + 点原地移动 -----

    def _ov_build_scene(self) -> None:
        c = self.ov_canvas
        c.Children.Clear()
        size = self._ov_size
        cx = cy = size / 2.0
        radius = cx - 20.0
        self._sky_frame(c, cx, cy, radius, True)
        lat, lon = self._site_latlon()
        self._ov_dots: list[tuple] = []
        for run in self._runs:
            if not _sky_relevant(run):
                continue
            ra, dec = self._run_coords(run)     # FITS 实测优先(反馈#2)
            if ra is None or dec is None:
                continue
            sel = run is self._sel_run
            d = 13.0 if sel else 9.0
            dot = Ellipse()
            dot.Width = dot.Height = d
            _ch, brush = self._run_badge(run)
            dot.Fill = brush
            if sel:
                dot.Stroke = self._b_sel
                dot.StrokeThickness = 2.0
            try:
                ToolTipService.SetToolTip(dot, run.target)
            except Exception:
                pass
            c.Children.Append(dot)
            sh = TextBlock()                # 底图上的 1px 标签阴影
            sh.Text = run.target
            sh.FontSize = 11
            sh.Foreground = self._b_shadow
            sh.IsHitTestVisible = False
            c.Children.Append(sh)
            lbl = TextBlock()
            lbl.Text = run.target
            lbl.FontSize = 11
            lbl.Foreground = self._b_label_bri
            if sel:
                lbl.FontWeight = FontWeights.SemiBold
            c.Children.Append(lbl)
            self._ov_dots.append((ra, dec, d, dot, sh, lbl))

    def _ov_tick(self) -> None:
        """滑杆 tick:时间标签 + 点/标签原地移动 + 底图换最近缓存帧。全部 O(点数)。"""
        ts = self._ov_ts()
        self.ov_time.Text = datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")
        size = self._ov_size
        cx = cy = size / 2.0
        radius = cx - 20.0
        lat, lon = self._site_latlon()
        for ra, dec, d, dot, sh, lbl in self._ov_dots:
            try:
                alt, az = astro.altaz(ra, dec, lat, lon, ts)
            except (ValueError, OSError, OverflowError):
                continue
            below = alt < 0
            r = radius if below else radius * (90.0 - alt) / 90.0
            azr = math.radians(az)
            x = cx - r * math.sin(azr)
            y = cy - r * math.cos(azr)
            Canvas.SetLeft(dot, x - d / 2.0)
            Canvas.SetTop(dot, y - d / 2.0)
            dot.Opacity = 0.4 if below else 1.0
            lx, ly = x + d / 2.0 + 3.0, y - 7.0
            Canvas.SetLeft(sh, lx + 1.0)
            Canvas.SetTop(sh, ly + 1.0)
            Canvas.SetLeft(lbl, lx)
            Canvas.SetTop(lbl, ly)
            op = 0.5 if below else 0.95
            sh.Opacity = op
            lbl.Opacity = op
        # 底图:换到当前时刻所在桶的缓存帧(没有就保持现状,预热到了会自动补)
        if self._survey_on:
            bucket = int(ts // self._OV_BUCKET)
            self._ov_want_bucket = bucket
            path = self._ov_bg_paths.get(bucket)
            if path is not None:
                self._ov_show_bucket(bucket, path)

    # ----- 双缓冲换帧(防解码空窗闪烁) -----

    def _ov_show_bucket(self, bucket: int, path: str) -> None:
        """显示某桶底图:已解码过的直接换正面;新帧派发到背面**串行**解码,
        ImageOpened 后翻面 —— 正面永远有完整旧帧,不闪。

        串行化关键:back 忙时只登记 _ov_pending,不覆盖 back.Source ——
        否则事件归属混乱,且已解码位图重赋值是 no-op 永不触发事件(审查 high)。
        """
        if self._ov_bg_shown == bucket:
            return
        if not os.path.isfile(path):
            # 预热帧可能被磁盘缓存清理删掉:剔除,等预热补渲染
            self._ov_bg_paths.pop(bucket, None)
            self._ov_bmp.pop(bucket, None)
            self._ov_ready.discard(bucket)
            return
        bmp = self._ov_bmp.get(bucket)
        if bmp is None:
            try:
                bmp = BitmapImage(file_uri(path))
            except Exception:
                return
            self._ov_bmp[bucket] = bmp
            self._ov_lru.append(bucket)
            while len(self._ov_lru) > 12:   # LRU 上限,防解码位图占内存
                old = self._ov_lru.pop(0)
                if old in (self._ov_bg_shown, self._ov_pending,
                           self._ov_back_bucket, bucket):
                    self._ov_lru.append(old)
                    if len(self._ov_lru) <= 12:
                        break
                    continue
                self._ov_bmp.pop(old, None)
                self._ov_ready.discard(old)
        try:
            if bucket in self._ov_ready and self._ov_front is not None:
                self._ov_front.Source = bmp     # 已解码,直接换,无空窗
                self._ov_bg_shown = bucket
                self._ov_pending = None
            elif self._ov_back_bucket is None and self._ov_back is not None:
                self._ov_pending = bucket
                self._ov_back_bucket = bucket   # 记账:back 正在解码哪个桶
                self._ov_back.Source = bmp
            else:
                self._ov_pending = bucket       # back 忙:事件链完成后会来追
        except Exception:
            pass

    def _ov_img_opened(self, sender, e) -> None:
        """背面解码完成:先无条件记账 ready(被打断的桶也不丢),
        仍被期待才翻面;随后追用户最新想要的桶。"""
        b = self._ov_back_bucket
        if not self._ov_open or b is None:
            return
        self._ov_ready.add(b)
        self._ov_back_bucket = None
        if self._ov_pending == b:
            front, back = self._ov_front, self._ov_back
            if front is not None and back is not None:
                back.Opacity = 1.0
                front.Opacity = 0.0
                self._ov_front, self._ov_back = back, front
            self._ov_bg_shown = b
            self._ov_pending = None
        want = self._ov_want_bucket
        if want != self._ov_bg_shown:
            p = self._ov_bg_paths.get(want)
            if p is not None:
                self._ov_show_bucket(want, p)

    def _ov_img_failed(self, sender, e) -> None:
        """解码失败(如缓存文件被删):清账并丢弃该桶,状态机可恢复。"""
        b = self._ov_back_bucket
        self._ov_back_bucket = None
        if b is not None:
            self._ov_bmp.pop(b, None)
            self._ov_ready.discard(b)
            self._ov_bg_paths.pop(b, None)
            if self._ov_pending == b:
                self._ov_pending = None

    # ----- 底图预热(单 worker,想要的桶优先,向外扩散) -----

    def _ov_start_warm(self, begin, end) -> None:
        if self._ov_bg_busy:
            # 旧 worker 在跑(可能是上个会话的陈旧参数):登记请求,
            # 旧 worker 因代次失配退出后由 _ov_warm_done 补跑
            self._ov_warm_req = (begin, end)
            return
        self._ov_bg_busy = True
        self._ov_warm_req = None
        gen = self._ov_warm_gen             # 代次快照:关/开遮罩都会 +1
        lat, lon = self._site_latlon()
        size = int(self._ov_size * 1.15)
        b0 = int(astro.unix_from_local(begin) // self._OV_BUCKET)
        b1 = int(astro.unix_from_local(end) // self._OV_BUCKET)
        buckets = list(range(b0, b1 + 1))
        budget = max(160, len(buckets) + 40)    # 防缓存清理删掉本轮预热帧
        self._ov_want_bucket = int(self._ov_ts() // self._OV_BUCKET)

        def work() -> None:
            done: set[int] = set()
            try:
                while (self._ov_open and self._ov_warm_gen == gen
                       and len(done) < len(buckets)):
                    want = self._ov_want_bucket
                    todo = [b for b in buckets if b not in done]
                    b = min(todo, key=lambda x: abs(x - want))
                    try:
                        p = skymap.render_altaz(
                            lat, lon, (b + 0.5) * self._OV_BUCKET,
                            size=size, cache_budget=budget)
                    except Exception as ex:
                        self.shell.ui(self.shell.error,
                                      _("巡天底图渲染失败: {ex}").format(ex=ex))
                        return
                    done.add(b)
                    self.shell.ui(self._ov_bg_ready, gen, b, str(p),
                                  len(done), len(buckets))
            finally:
                self.shell.ui(self._ov_warm_done)

        threading.Thread(target=work, daemon=True, name="sky-ov-warm").start()

    def _ov_bg_ready(self, gen: int, bucket: int, path: str,
                     ndone: int, ntotal: int) -> None:
        if not self._ov_open or gen != self._ov_warm_gen:
            return                          # 陈旧 worker 的帧不进本会话
        self._ov_bg_paths[bucket] = path
        if ndone < ntotal:
            self.ov_credit.Text = _("{SURVEY_CREDIT} · 预热 {ndone}/{ntotal}").format(
                SURVEY_CREDIT=_(skymap.SURVEY_CREDIT), ndone=ndone,
                ntotal=ntotal)
        else:
            self.ov_credit.Text = _(skymap.SURVEY_CREDIT)
        if bucket == self._ov_want_bucket:
            self._ov_show_bucket(bucket, path)

    def _ov_warm_done(self) -> None:
        self._ov_bg_busy = False
        req = self._ov_warm_req
        self._ov_warm_req = None
        if self._ov_open and req is not None:
            self._ov_start_warm(*req)       # busy 期间被顶掉的请求补跑
        elif self._ov_open and self._ov_bg_paths:
            self.ov_credit.Text = _(skymap.SURVEY_CREDIT)

    def _draw_sky(self) -> None:
        try:
            t_lbl = datetime.fromtimestamp(self._sky_ts()).strftime("%H:%M")
            self.sky_title.Text = _("全天位置(仰视: 北上 · 东左) · {t_lbl} 时刻").format(t_lbl=t_lbl)
        except Exception:
            pass
        self._draw_sky_onto(self.sky_canvas, 340.0, self._bg_shown)

    def _sky_frame(self, canvas: Canvas, cx: float, cy: float,
                   radius: float, bright: bool) -> None:
        """静态框架:同心圈/十字线/方位标注(遮罩层只画一次,主图每次重画)。"""
        b_ring = self._b_grid_bri if bright else self._b_grid
        b_ring_dim = self._b_grid_dim_bri if bright else self._b_grid_dim
        b_label = self._b_label_bri if bright else self._b_label

        # 同心圈: 地平线(实线) + alt=30/60(虚线)
        for alt, dashed in ((0, False), (30, True), (60, True)):
            r = radius * (90.0 - alt) / 90.0
            ring = Ellipse()
            ring.Width = ring.Height = 2.0 * r
            ring.Stroke = b_ring if not dashed else b_ring_dim
            ring.StrokeThickness = 1.0
            if dashed:
                dc = self._dash_collection()
                if dc is not None:
                    try:
                        ring.StrokeDashArray = dc
                    except Exception:
                        pass
            Canvas.SetLeft(ring, cx - r)
            Canvas.SetTop(ring, cy - r)
            canvas.Children.Append(ring)

        # 十字方位线
        for x1, y1, x2, y2 in ((cx - radius, cy, cx + radius, cy),
                               (cx, cy - radius, cx, cy + radius)):
            ln = Line()
            ln.X1, ln.Y1, ln.X2, ln.Y2 = x1, y1, x2, y2
            ln.Stroke = b_ring_dim
            ln.StrokeThickness = 1.0
            canvas.Children.Append(ln)

        # 方位标注: 北上 / 东左 / 南下 / 西右
        for text, lx, ly in ((_("北"), cx - 7, cy - radius - 19),
                             (_("南"), cx - 7, cy + radius + 2),
                             (_("东"), cx - radius - 17, cy - 9),
                             (_("西"), cx + radius + 3, cy - 9)):
            lbl = TextBlock()
            lbl.Text = text
            lbl.FontSize = 12
            lbl.Foreground = b_label
            Canvas.SetLeft(lbl, lx)
            Canvas.SetTop(lbl, ly)
            canvas.Children.Append(lbl)

    def _draw_sky_onto(self, canvas: Canvas, fallback: float,
                       bright: bool, ts: float | None = None) -> None:
        """alt-az 极坐标全天图(仰视): 圆心=天顶, 外圈=地平线;
        北=上, 东=左(仰视天球惯例)。巡天底图显示时(bright)前景换更亮画刷
        并给目标标签加 1px 阴影, 保证叠在底图上可读。

        canvas 未经历布局时 ActualWidth 为 0, 用 fallback 尺寸(放大对话框场景)。
        """
        canvas.Children.Clear()
        w = float(canvas.ActualWidth or 0) or fallback
        h = float(canvas.ActualHeight or 0) or fallback
        cx, cy = w / 2.0, h / 2.0
        radius = min(w, h) / 2.0 - 20.0
        if radius < 40:
            return
        self._sky_frame(canvas, cx, cy, radius, bright)

        # 当前夜次每个目标一个点。
        # **整图统一用一个时刻**(与巡天底图同一 _sky_ts):各点若用各自拍摄时刻,
        # 与按单一时刻渲染的底图会错位(M 8 不落在银心上,真机踩过);
        # 时刻标注在图标题上。
        lat, lon = self._site_latlon()
        if ts is None:
            ts = self._sky_ts()
        for run in self._runs:
            if not _sky_relevant(run):
                continue    # 偏置/暗场会话的"坐标"是停机位,上图只会误导
            ra, dec = self._run_coords(run)     # FITS 实测优先(反馈#2)
            if ra is None or dec is None:
                continue
            try:
                alt, az = astro.altaz(ra, dec, lat, lon, ts)
            except (ValueError, OSError, OverflowError):
                continue
            below = alt < 0
            r = radius if below else radius * (90.0 - alt) / 90.0
            azr = math.radians(az)
            x = cx - r * math.sin(azr)      # 东=左
            y = cy - r * math.cos(azr)      # 北=上
            sel = run is self._sel_run

            d = 13.0 if sel else 9.0
            dot = Ellipse()
            dot.Width = dot.Height = d
            _ch, brush = self._run_badge(run)
            dot.Fill = brush
            if sel:
                dot.Stroke = self._b_sel
                dot.StrokeThickness = 2.0
            if below:
                dot.Opacity = 0.4
            Canvas.SetLeft(dot, x - d / 2.0)
            Canvas.SetTop(dot, y - d / 2.0)
            tip = (f"{run.target}\nalt {alt:.1f}°  az {az:.1f}°"
                   + (_('\n(已落至地平线以下)') if below else ""))
            try:
                ToolTipService.SetToolTip(dot, tip)
            except Exception:
                pass
            canvas.Children.Append(dot)

            opacity = 1.0 if sel else (0.5 if below else 0.85)
            if bright:
                # 底图上加 1px 阴影(错位深色 TextBlock 垫底)保证可读
                sh = TextBlock()
                sh.Text = run.target
                sh.FontSize = 10
                sh.Foreground = self._b_shadow
                sh.Opacity = opacity
                if sel:
                    sh.FontWeight = FontWeights.SemiBold
                sh.IsHitTestVisible = False
                Canvas.SetLeft(sh, x + d / 2.0 + 3.0)
                Canvas.SetTop(sh, y - 6.0)
                canvas.Children.Append(sh)
            lbl = TextBlock()
            lbl.Text = run.target
            lbl.FontSize = 10
            lbl.Opacity = opacity
            if bright:
                lbl.Foreground = self._b_label_bri
            if sel:
                lbl.FontWeight = FontWeights.SemiBold
            lbl.IsHitTestVisible = False
            Canvas.SetLeft(lbl, x + d / 2.0 + 2.0)
            Canvas.SetTop(lbl, y - 7.0)
            canvas.Children.Append(lbl)

    # ---------- 巡天底图(反馈#3) ----------

    async def _on_survey_toggled(self, sender, e) -> None:
        """开关巡天底图。首次开启且本地无底图时先问询再下载(约 8MB)。"""
        if self._ui_updating:
            return
        on = bool(self.survey_toggle.IsOn)
        if on == self._survey_on:
            return
        if not on:
            self._survey_off()
            return
        if not skymap.survey_available():
            ok = await self.shell.confirm(
                _("下载巡天底图"),
                _("首次使用需下载 ESO 银河全景底图({SURVEY_SIZE_HINT}),仅下载一次, 之后离线可用。是否继续?").format(
                    SURVEY_SIZE_HINT=_(skymap.SURVEY_SIZE_HINT)),
                _("下载"))
            if not ok:
                self._set_toggle(False)
                return
        self._survey_on = True
        self._update_sky_bg(force=True)

    def _set_toggle(self, on: bool) -> None:
        """程序化改开关状态(不触发 _on_survey_toggled 的业务逻辑)。"""
        self._ui_updating = True
        try:
            self.survey_toggle.IsOn = on
        finally:
            self._ui_updating = False

    def _survey_off(self) -> None:
        """关闭底图: 作废在途渲染, 清空 Image 源, 前景恢复常规亮度。"""
        self._survey_on = False
        self._bg_shown = False
        self._sky_gen += 1                  # 在途渲染结果过期丢弃
        self._sky_inflight = None
        self._sky_key = None
        try:
            self.sky_bg_image.put_Source(None)
        except Exception:
            pass
        self.sky_credit.Visibility = Visibility.Collapsed
        self._draw_sky()

    def _sky_ts(self) -> float:
        """底图渲染时刻: 选中目标的帧中点 > 夜次中点 > 当前时间。"""
        try:
            run = self._sel_run
            if run is not None:
                span = run.frame_span()
                t = (span[0] + (span[1] - span[0]) / 2) if span else run.begin_time
                return astro.unix_from_local(t)
            if 0 <= self._night_idx < len(self._nights):
                n = self._nights[self._night_idx]
                b, e = n.begin_time, n.end_time
                if b is not None and e is not None and e > b:
                    return astro.unix_from_local(b + (e - b) / 2)
                if b is not None:
                    return astro.unix_from_local(b)
        except (ValueError, OSError, OverflowError):
            pass
        return time.time()

    def _update_sky_bg(self, force: bool = False) -> None:
        """夜次切换/选中切换/站点应用后按需重渲染底图。

        渲染在工作线程执行(render_altaz 自带 5 分钟桶磁盘缓存, 重复调用
        便宜), 这里仍按 (站点, 时刻桶) 去重 + 单飞代次防连点。"""
        if not self._survey_on:
            return
        lat, lon = self._site_latlon()
        ts = self._sky_ts()
        key = (f"{lat:.1f}", f"{lon:.1f}", int(ts // 300))   # 与 render_altaz 缓存粒度一致
        if not force:
            if self._bg_shown and key == self._sky_key:
                # 已是最新;但若有一个"异 key"渲染在途(快速来回切换),
                # 必须作废它——否则它完成后会把过期底图应用上去并常驻
                if (self._sky_inflight is not None
                        and self._sky_inflight != key):
                    self._sky_gen += 1
                    self._sky_inflight = None
                return
            if key == self._sky_inflight:
                return                      # 相同渲染在途
        self._sky_gen += 1
        gen = self._sky_gen
        self._sky_inflight = key
        threading.Thread(
            target=self._sky_work, args=(gen, lat, lon, ts, key),
            daemon=True, name="records-skybg").start()

    def _sky_work(self, gen: int, lat: float, lon: float,
                  ts: float, key) -> None:
        """工作线程: 需要时下载底图 → 重投影渲染 PNG → 编组回 UI。

        _sky_lock 串行化: 连点/快速切换会并发进入, 并发 download_survey
        写同一 .part 会撞 WinError 32(logstore 已踩过同类雷)。"""
        try:
            with self._sky_lock:
                if gen != self._sky_gen:
                    return                  # 排队期间已被更新的渲染取代
                if not skymap.survey_available():
                    last = [-1]

                    def prog(done: int, total: int) -> None:
                        if total > 0:
                            p = int(done * 100 / total)
                            if p != last[0]:    # 整百分点才编组, 避免刷屏
                                last[0] = p
                                self.shell.ui(self._sky_status, gen,
                                              _("正在下载巡天底图… {p}%").format(p=p))

                    self.shell.ui(self._sky_status, gen, _("正在下载巡天底图…"))
                    skymap.download_survey(progress=prog)
                self.shell.ui(self._sky_status, gen, _("正在渲染巡天底图…"))
                png = skymap.render_altaz(lat, lon, ts, size=SKY_RENDER_PX,
                                          dim=0.85)
            self.shell.ui(self._apply_sky_bg, gen, key, str(png))
        except Exception as ex:             # 工作线程异常不许静默
            self.shell.ui(self._sky_failed, gen, f"{type(ex).__name__}: {ex}")

    def _sky_status(self, gen: int, text: str) -> None:
        if gen != self._sky_gen:
            return                          # 过期 worker
        self.sky_credit.Text = text
        self.sky_credit.Visibility = Visibility.Visible

    def _apply_sky_bg(self, gen: int, key, png_path: str) -> None:
        if gen != self._sky_gen or not self._survey_on:
            return                          # 过期 worker / 已关闭
        self._sky_inflight = None
        try:
            self.sky_bg_image.Source = BitmapImage(file_uri(png_path))
        except Exception as ex:
            self._sky_failed(gen, _("加载底图失败: {ex}").format(ex=ex))
            return
        self._sky_key = key
        self._bg_shown = True
        # 署名义务: 底图显示期间常显 CC BY 4.0 署名
        self.sky_credit.Text = _(skymap.SURVEY_CREDIT)
        self.sky_credit.Visibility = Visibility.Visible
        self._draw_sky()                    # 前景提亮重画

    def _sky_failed(self, gen: int, msg: str) -> None:
        if gen != self._sky_gen:
            return                          # 过期 worker
        self._sky_inflight = None
        self.shell.error(_("巡天底图不可用: {msg}").format(msg=msg))
        self._set_toggle(False)             # 关回开关, 避免后续联动反复报错
        self._survey_off()
