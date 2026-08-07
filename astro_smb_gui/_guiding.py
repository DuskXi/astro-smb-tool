"""导星分析页:PHD2 导星段列表(按拍摄目标分组)+ RA/DEC 偏差曲线 + 统计图表。

数据来自 shell.logstore(LogStore,见 logstore.py)。刷新在工作线程完成
(clone 独立 client → logstore.refresh → 预计算 RMS/numpy 帧数组/滑动 RMS/
统计图表数据/汇总/**分组结构**),UI 线程只负责画;单飞用代次计数器,
过期结果丢弃。绘制用 Polyline/Polygon(win32more 投射已确认存在),
SizeChanged 时重画,做法与 _space.py 的 Canvas 绘制一致。

段列表分组(第五轮反馈「几十条 0.1~1.1 分钟的碎段看着费眼睛」):

* **按拍摄目标分组**——用 Autorun 日志的**目标块区间**(不是 TargetRun 整段:
  Pause 分裂的 run 整段会横跨别的目标,整段匹配会抢错段,logstore 同款教训)
  与导星段求重叠,取重叠最长的目标;匹配不上的(校准前后/空闲期)进「其它」组。
* **组头** = 目标名 + 段数徽章 + 合并 RMS 徽章(**帧数平方加权**,与
  _records 的整夜口径一致;角秒/像素不可混算,优先角秒),副行给起止/总时长/
  丢星/校准次数。点组头 = 折叠切换;组头**右侧的「仪表盘」按钮**把右侧换成
  `_guidedash.GuideDashboard`(本组全部段聚合的分析视图)——
  按钮吃掉 PointerPressed 所以不会冒泡成折叠,`_dash_guard` 再兜一层。

右侧分析区有**两个互斥视图**(同一格,靠 Visibility 切;左侧段列表全程可见):

* **段视图** `SegmentView` —— 选中某个导星段:标题 + 徽章 / 大折线图 /
  统计小图横滚条 / 段统计卡。时间窗下拉与位置滑杆**只服务这张大图**。
* **仪表盘视图** `DashHost` —— 某个目标组全部段的聚合分析(**没有大折线图**),
  由 `_guidedash.GuideDashboard` 把自己的根挂进 `DashHost`。

视图切换全部经 `_set_view`:点段行/`_select_row` → 段视图;点组头「仪表盘」
按钮 → 仪表盘视图;`show_range` 跳转 → 强制段视图(定位结果必须看得见);
数据换代(`_apply_data` → `dash.invalidate()`)→ 退回段视图并作废聚合缓存
(组键里带的是旧一代的行索引,留着只会画出错位的东西)。
* **组内**:主段(≥100 有效记录帧 **或** ≥5 分钟)逐条显示;连续的碎段
  (settle/重选星的短尝试)折叠成一条淡色摘要行,点开看明细。
* 列表项 = 组头 / 段行 / 碎段摘要 三类混排,`_disp` 是「显示项」列表,
  `_rows` 仍是「数据行」列表,两者用 `_ri_disp`/`_loc` 互查 ——
  **对外索引(`_overview` 柱、`show_range` 定位、`_sel_idx`)一律是数据行索引**。
  `show_range` 跳转会自动展开所在组与碎段簇再选中。

时间窗:工具行「窗口」下拉(全段/60/30/10/5 分钟)+ 位置滑杆平移。窗口切换/
滑动在 UI 线程只做 numpy searchsorted 切片(O(log n)+视图),统计全部在工作
线程预计算,不随滑动重算。缩出(窗口内帧数 > 2×像素宽)时改画 min/max 包络带
+ 30 帧滑动 RMS 主线;缩进时保持逐点折线。

统计图表区(横滚,8 项):漂移速率卡 / 散点 / 直方图 / 滚动 RMS / 修正脉冲 /
RA 周期图(均匀重采样 + rFFT 幅值谱,找蜗杆周期误差)/ SNR·星质量曲线 /
逐段 RMS 柱状总览(整夜视角,点击柱切换选中段)。前 7 项随选中段变
(_prep_charts),总览随数据换代(_prepare);校准段选中时整区隐藏。

公开入口:show_range(t0, t1, label) —— 拍摄记录页跳转,选中重叠导星段并在
曲线上高亮 [t0, t1] 区间(UI 线程调用;会重置窗口为全段保证高亮可见)。
"""

from __future__ import annotations

import bisect
import math
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np

from win32more.Microsoft.UI.Text import FontWeights
from win32more.Microsoft.UI.Xaml import (
    CornerRadius,
    FrameworkElement,
    GridLength,
    GridUnitType,
    TextTrimming,
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
    Grid,
    ListView,
    Orientation,
    ProgressRing,
    ScrollViewer,
    Slider,
    StackPanel,
    TextBlock,
    ToolTipService,
)
from win32more.Microsoft.UI.Xaml.Markup import XamlReader
from win32more.Microsoft.UI.Xaml.Media import PointCollection, SolidColorBrush
from win32more.Microsoft.UI.Xaml.Shapes import Ellipse, Line, Polygon, Polyline, Rectangle
from win32more.Windows.Foundation import Point
from win32more.Windows.UI import Color

from astro_smb.client import SmbClientError
from astro_smb.phd2log import (
    CalibrationSection,
    GuideSection,
    compute_rms,
    section_rms,
)
from astro_smb.i18n import gettext as _
# 批量绘图原语统一走 _common。本模块曾自带一份 XAML_NS/rect_fragment/
# poly_fragment/_argb_hex 的副本(与 _common 逐字同源),连"画刷缓存要连画刷
# 一起存、否则 id 被复用会取到别人的颜色"这条教训都各写了一遍 —— 同一个契约
# 两份实现,正是本仓库反复修过的病。已合并到 _common。
from astro_smb_gui._common import XAML_NS, argb_hex, poly_fragment, rect_fragment

XAML_PATH = Path(__file__).with_name("guiding.xaml")

# 图表计算层已下沉到 astro_smb_app.views.guiding —— 新前端画同一份数据。
# (B5 按计划随切片抽取;走的是 docs/architecture/frontend.md 的逃生口。
#  函数体一个字节没动,只搬了位置。)
from astro_smb_app.views.guiding import (  # noqa: F401
    BADGE_RGB,
    BAR_GOOD,
    BAR_M,
    BAR_WARN,
    CAL_NEAR_S,
    CAL_SPAN_S,
    CHART_H,
    CHART_W,
    DRIFT_DEC_WARN,
    ENV_FRAMES_PER_PX,
    FRAG_MIN_CLUSTER,
    HIST_BINS,
    MAIN_MIN_DUR,
    MAIN_MIN_FRAMES,
    MAX_LOST_TICKS,
    MAX_POINTS,
    MB,
    MIN_RANK_FRAMES,
    ML,
    MR,
    MT,
    OTHER_KEY,
    PERIOD_MAX_PTS,
    PERIOD_MAX_S,
    PERIOD_MIN_FRAMES,
    PERIOD_MIN_S,
    RMS30_FRAMES,
    ROLL_SAMPLES,
    ROLL_WIN_S,
    SCATTER_MAX,
    SNR_MAX_PTS,
    VIEW_DASH,
    VIEW_SEGMENT,
    WINDOW_CHOICES,
    _PIER_CN,
    _TICK_STEPS,
    _assign_cal,
    _assign_guide,
    _bucket_peak,
    _build_groups,
    _downsample,
    _fmt,
    _fmt_hours,
    _make_group,
    _merge_rms,
    _overlap_s,
    _p995,
    _prep_cal,
    _prep_charts,
    _prep_guide,
    _prep_period,
    _prepare,
    _rms_level,
    _sliding_rms,
    _summary_text,
    _target_blocks,
    _target_runs,
    overview_hit_bar,
)
from astro_smb_gui._xamli18n import load_text as _xaml_text



def _brush(r: int, g: int, b: int, a: int = 255) -> SolidColorBrush:
    c = Color()
    c.A, c.R, c.G, c.B = a, r, g, b
    return SolidColorBrush(c)


def _corner(r: float) -> CornerRadius:
    cr = CornerRadius()
    cr.TopLeft = cr.TopRight = cr.BottomRight = cr.BottomLeft = r
    return cr


class GuidingPage:
    def __init__(self, shell):
        self.shell = shell
        self.root = XamlReader.Load(_xaml_text(XAML_PATH)).as_(FrameworkElement)

        self._rows: list[dict] | None = None     # 预计算好的**数据行**(None=未加载)
        # 分组显示层:_disp 是 ListView 里的显示项(组头/段行/碎段摘要三类),
        # _disp_widgets 与之一一对齐;_ri_disp 把数据行索引映射到显示项索引。
        self._groups: list[dict] = []
        self._loc: dict[int, tuple] = {}          # 数据行索引 → (组键, 碎段簇键|None)
        self._disp: list[dict] = []
        self._disp_widgets: list = []
        self._ri_disp: dict[int, int] = {}
        # 组头控件按组键缓存复用(见 _group_widget:组头上的「仪表盘」Button 每挂一次
        # 事件就会被 win32more 的 event 描述符永久 pin 住,重建列表时绝不能新建)
        self._group_widgets: dict[str, dict] = {}
        self._group_open: dict[str, bool] = {}    # 组展开态(默认展开)
        self._frag_open: dict[str, bool] = {}     # 碎段簇展开态(默认折叠)
        self._all_collapsed = False               # 「全部折叠/展开」按钮状态
        self._current: dict | None = None         # 当前选中行
        self._prepared_src = None                 # _rows 对应的 LogData(判断缓存有效)
        self._gen = 0                             # 刷新代次(过期结果丢弃)
        self._refreshing = False                  # 单飞标志
        self._loading_list = False                # 重建列表期间屏蔽选择事件
        self._connected = False
        self._hl: tuple[datetime, datetime, str] | None = None  # 待高亮区间
        self._pending_locate = False              # 数据加载完成后再执行定位
        self._status_base = _("连接设备后点「刷新」加载导星日志")
        self._win_s: float | None = None          # 当前时间窗宽(秒,None=全段)
        self._win_pos = 0.0                       # 窗口位置 0~1
        self._ui_updating = False                 # 程序化改窗口控件时抑制事件
        self._overview: dict | None = None        # 逐段 RMS 总览数据(随数据换代)
        # 总览上**这次画出来**的柱 → 数据行索引,以及画时用的画布宽:
        # 画布级 Tapped 靠这两样反算命中(见 _draw_overview / overview_hit_bar)
        self._ov_bars: list[int] = []
        self._ov_w = CHART_W
        self._sel_idx = -1                        # 当前选中的**数据行**索引(总览描边用)
        self._view = VIEW_SEGMENT                 # 右侧当前视图(段视图 / 仪表盘视图)
        self._dash = None                         # GuideDashboard(懒建)
        self._dash_guard: str | None = None       # 组头「仪表盘」按钮的防冒泡守卫
        self._dash_auto_done = False              # ASTRO_SMB_GUI_DASH 自动开启一次性守卫

        # 画刷全部建一次复用(见 _monitor.py 的做法)
        self._b_ra = _brush(0, 120, 215)           # RA 蓝
        self._b_dec = _brush(240, 140, 0)          # DEC 橙
        self._b_axis = _brush(128, 128, 128, 200)  # 0 轴
        self._b_grid = _brush(128, 128, 128, 60)   # 整数角秒网格
        self._b_lost = _brush(0xE5, 0x73, 0x73)    # 丢星红刻度
        self._b_hl = _brush(80, 160, 255, 40)      # 跳转高亮(淡蓝 A=40)
        self._b_ra_fill = _brush(0, 120, 215, 60)  # 包络带 RA(A=60)
        self._b_dec_fill = _brush(240, 140, 0, 60) # 包络带 DEC(A=60)
        self._b_ra_dim = _brush(0, 120, 215, 110)  # 直方图 RA 半透明
        self._b_dec_dim = _brush(240, 140, 0, 110) # 直方图 DEC 半透明
        self._b_pt = _brush(0, 120, 215, 90)       # 散点
        self._b_roll = _brush(0x4C, 0xAF, 0x50)    # 滚动 RMS 绿(逐段柱"好"复用)
        self._b_amber = _brush(0xFF, 0xB3, 0x00)   # 琥珀:逐段柱预警/漂移警示
        self._b_sel = _brush(0x42, 0xA5, 0xF5)     # 选中柱描边蓝
        self._b_mass = _brush(0x9E, 0x9E, 0x9E, 220)  # 星质量灰
        self._badge_bg = _brush(0x80, 0x80, 0x80, 0x28)  # 徽章底(中性半透,两主题通用)
        # 胶囊徽章配色(浅底 + 深字):段视图标题行与仪表盘汇总卡共用同一份
        self._chip_brushes = {k: (_brush(*bg), _brush(*fg))
                              for k, (bg, fg) in BADGE_RGB.items()}
        # 逐段柱"差"复用 self._b_lost(红)
        # 语义级别 → 画刷(组头徽章/段行副行共用;好=绿 警告=琥珀 差=红)
        self._level_brushes = {"good": self._b_roll, "warn": self._b_amber,
                               "bad": self._b_lost}
        # 批量绘制用:id(画刷) → (画刷, #AARRGGBB)。值里留着画刷的强引用,
        # 免得画刷被回收后 id 被复用、取到别人的颜色。

        self._find()
        self._wire()
        # 漂移卡 DEC 行默认前景(预警时换琥珀,恢复时还原;主题画刷建页时捕获)
        try:
            self._fg_default = self.drift_dec_text.Foreground
        except Exception:
            self._fg_default = None
        self.drift_hint_text.Foreground = self._b_amber

    def _find(self) -> None:
        f = self.root.FindName
        self.refresh_btn = f("RefreshBtn").as_(Button)
        self.busy_ring = f("BusyRing").as_(ProgressRing)
        self.status_text = f("StatusText").as_(TextBlock)
        self.window_combo = f("WindowCombo").as_(ComboBox)
        self.pos_slider = f("PosSlider").as_(Slider)
        self.content_grid = f("ContentGrid").as_(Grid)
        self.summary_text = f("SummaryText").as_(TextBlock)
        self.group_hint = f("GroupHintText").as_(TextBlock)
        self.collapse_btn = f("CollapseBtn").as_(Button)
        self.section_list = f("SectionList").as_(ListView)
        # 右侧两个互斥视图的宿主(段视图整棵子树 / 仪表盘面板的挂载点)
        self.segment_view = f("SegmentView").as_(Grid)
        self.dash_host = f("DashHost").as_(Grid)
        self.detail_title = f("DetailTitle").as_(TextBlock)
        self.detail_badges = f("DetailBadges").as_(StackPanel)
        self.stats_card = f("StatsCard").as_(Border)
        self.canvas = f("CurveCanvas").as_(Canvas)
        self.canvas_hint = f("CanvasHint").as_(TextBlock)
        self.charts_scroll = f("ChartsScroll").as_(ScrollViewer)
        self.scatter_canvas = f("ScatterCanvas").as_(Canvas)
        self.hist_canvas = f("HistCanvas").as_(Canvas)
        self.roll_canvas = f("RollCanvas").as_(Canvas)
        self.pulse_canvas = f("PulseCanvas").as_(Canvas)
        self.period_canvas = f("PeriodCanvas").as_(Canvas)
        self.snr_canvas = f("SnrCanvas").as_(Canvas)
        self.segrms_canvas = f("SegRmsCanvas").as_(Canvas)
        self.drift_ra_text = f("DriftRaText").as_(TextBlock)
        self.drift_dec_text = f("DriftDecText").as_(TextBlock)
        self.drift_hint_text = f("DriftHintText").as_(TextBlock)
        self.stats_text = f("StatsText").as_(TextBlock)
        self.empty_hint = f("EmptyHint").as_(TextBlock)

    def _wire(self) -> None:
        self.refresh_btn.Click += self._on_refresh
        self.collapse_btn.Click += self._on_collapse_all
        self.section_list.SelectionChanged += self._on_select
        self.canvas.SizeChanged += lambda s, e: self._render_curve()
        self.window_combo.SelectionChanged += self._on_window_changed
        self.pos_slider.ValueChanged += self._on_pos_changed
        # 逐段 RMS 总览:**整块画布只挂一个** Tapped。win32more 的 event 描述符
        # 会把实例存进**类级** _event_setters 且从不移除(`-=`/`clear()` 只清
        # _callbacks),而这张图**每选一次段就重画一次** —— 逐根柱挂事件等于每选
        # 一段就永久滞留一批 Rectangle 及其闭包。画布是 xaml 里的固定控件,
        # 这里只注册一次,命中柱靠 overview_hit_bar 反算。
        self.segrms_canvas.Tapped += self._on_overview_tapped

    # ---------- 生命周期 ----------

    def on_show(self) -> None:
        # 仪表盘现在是右侧**面板**而不是遮罩:换页回来不再强制收起它
        # (左侧列表一直可见,面板挡不住任何东西,保留视图 = 保留用户上下文)。
        # 需要强制回段视图的只有 show_range 跳转与数据换代两条路径。
        store = getattr(self.shell, "logstore", None)
        if store is None:
            return
        if store.data is None and not self._connected:
            return
        # 数据换代(如 watcher 失效缓存后)也要重载,不能只看 _rows 是否存在
        if self._rows is None or self._prepared_src is not store.data:
            self._refresh(force=False)

    def on_connected(self, shares) -> None:
        self._connected = True
        self._refresh(force=False)

    # ---------- 刷新(单飞 + 代次) ----------

    def _on_refresh(self, sender, e) -> None:
        # 未连接时不发起 SMB(否则拨占位地址 192.0.2.1 挂超时,见 _records 同处)
        if not self._connected:
            self._set_status(_("未连接设备, 无法读取导星日志"))
            return
        self._refresh(force=True)

    def _refresh(self, force: bool) -> None:
        if self._refreshing:
            return
        store = getattr(self.shell, "logstore", None)
        if store is None:
            self.shell.error(_("日志数据层尚未初始化(shell.logstore 缺失)"))
            return
        data = store.data
        if data is not None and not force:
            if self._prepared_src is data and self._rows is not None:
                self._after_data()      # 已渲染过:只处理待定位
                return
            self._start_worker(refetch=False)   # 有数据但未预计算:只算不下载
            return
        self._start_worker(refetch=True)

    def _start_worker(self, refetch: bool) -> None:
        self._refreshing = True
        self._gen += 1
        gen = self._gen
        self.refresh_btn.IsEnabled = False
        self.busy_ring.IsActive = True
        self._set_status(_("正在刷新导星日志…") if refetch else _("正在解析导星日志…"))
        store = self.shell.logstore
        base_client = self.shell.client

        def work():
            clone = None
            try:
                if refetch:
                    clone = base_client.clone()
                    data = store.refresh(clone)
                else:
                    data = store.data
                    if data is None:            # 竞态兜底:被别处清掉则重新拉
                        clone = base_client.clone()
                        data = store.refresh(clone)
                prep = _prepare(data)
                self.shell.ui(self._apply_data, gen, data, prep)
            except SmbClientError as ex:
                self.shell.ui(self._apply_error, gen, str(ex))
            except Exception as ex:             # 防御:工作线程异常不许静默
                self.shell.ui(self._apply_error, gen, f"{type(ex).__name__}: {ex}")
            finally:
                if clone is not None:
                    try:
                        clone.close()
                    except Exception:
                        pass

        threading.Thread(target=work, daemon=True, name="guiding-refresh").start()

    def _apply_data(self, gen: int, data, prep: dict) -> None:
        if gen != self._gen:
            return                              # 过期结果丢弃
        self._refreshing = False
        self.refresh_btn.IsEnabled = True
        self.busy_ring.IsActive = False
        self._prepared_src = data
        self._rows = prep["rows"]
        self._groups = prep.get("groups") or []
        self._loc = prep.get("loc") or {}
        self._overview = prep.get("overview")
        # 总览要等选中某段才重画,这之前旧一代的命中表必须先作废(行索引已失效)
        self._ov_bars = []
        # 数据换代:展开态记忆全部作废(键里带的是旧一代的行索引)
        self._group_open = {}
        self._frag_open = {}
        # 组头控件缓存整代作废(闭包 pin 着上一代的 group/TargetRun,必须放掉)
        self._group_widgets = {}
        self._all_collapsed = False
        self.collapse_btn.Content = _("全部折叠")
        self._sel_idx = -1
        self._current = None
        self._dash_guard = None
        if self._dash is not None:
            # 数据换代:组键里带的是旧一代的行索引,聚合缓存与在显的面板全部作废
            try:
                self._dash.invalidate()
            except Exception:
                pass
        self._set_status(prep["status"])
        summary = prep.get("summary")
        self.summary_text.Text = summary or ""
        self.summary_text.Visibility = (Visibility.Visible if summary
                                        else Visibility.Collapsed)
        self._rebuild_list()
        self._select_row(-1)        # 清空右侧详情,等 _after_data 选默认段
        self._after_data()

    def _apply_error(self, gen: int, msg: str) -> None:
        if gen != self._gen:
            return
        self._refreshing = False
        self.refresh_btn.IsEnabled = True
        self.busy_ring.IsActive = False
        # 清掉未完成的跳转定位,防止很久之后某次成功刷新突然跳到陈旧区间
        self._pending_locate = False
        self._hl = None
        self._set_status(_("导星日志刷新失败"))
        self.shell.error(_("导星日志刷新失败: {msg}").format(msg=msg))

    def _after_data(self) -> None:
        """数据就绪后的收尾:优先执行待定位,否则默认选中最新导星段。

        默认优先挑**主段** —— 挑到碎段会顺手展开它所在的碎段簇,
        与「碎段默认折叠」的初衷相悖;没有主段时才退回任意导星段。
        """
        if self._pending_locate and self._hl is not None:
            self._pending_locate = False
            self._locate_range()
            self._auto_open_dash()
            return
        rows = self._rows or []
        if rows and self._sel_idx < 0:
            pick = next((i for i, r in enumerate(rows)
                         if r["kind"] == "guide" and r.get("main_seg")), None)
            if pick is None:
                pick = next((i for i, r in enumerate(rows)
                             if r["kind"] == "guide"), None)
            if pick is not None:
                self._set_selection(pick)
        self._auto_open_dash()

    def _auto_open_dash(self) -> None:
        """ASTRO_SMB_GUI_DASH=N:数据就绪后自动打开第 N 组的仪表盘(截图验证用)。"""
        if self._dash_auto_done or not self._groups:
            return
        raw = os.environ.get("ASTRO_SMB_GUI_DASH")
        if raw is None:
            return
        self._dash_auto_done = True
        try:
            idx = int(raw)
        except ValueError:
            return
        if 0 <= idx < len(self._groups):
            self._on_dash_click(self._groups[idx])

    def _set_status(self, text: str) -> None:
        self._status_base = text
        self.status_text.Text = text

    # ---------- 左栏列表(按目标分组:组头 / 段行 / 碎段摘要) ----------

    def _level_brush(self, level):
        return self._level_brushes.get(level) if level else None

    def _badge(self, text: str, level: str | None) -> Border:
        """圆角小胶囊:中性半透底 + 语义色字(浅/深主题下都可读)。"""
        b = Border()
        b.CornerRadius = _corner(8.0)
        b.Background = self._badge_bg
        b.Padding = Thickness(Left=7, Top=0, Right=7, Bottom=1)
        b.VerticalAlignment = VerticalAlignment.Center
        tb = TextBlock()
        tb.Text = text
        tb.FontSize = 11
        tb.FontWeight = FontWeights.SemiBold
        brush = self._level_brush(level)
        if brush is not None:
            tb.Foreground = brush
        else:
            tb.Opacity = 0.75
        b.Child = tb
        return b

    def _append_disp(self, item: dict, widget) -> int:
        self._disp.append(item)
        self._disp_widgets.append(widget)
        self.section_list.Items.Append(widget)
        return len(self._disp) - 1

    def _rebuild_list(self) -> None:
        """按当前展开态铺平成显示项列表。

        重建期间 `_loading_list=True` 屏蔽 SelectionChanged(Items.Clear 与
        末尾恢复 SelectedIndex 都会触发它);数据选中(`_sel_idx`/`_current`)
        **不因重建而改变** —— 折叠一个组不该把右侧曲线也清掉。
        """
        rows = self._rows or []
        self._loading_list = True
        try:
            self.section_list.Items.Clear()
            self._disp = []
            self._disp_widgets = []
            self._ri_disp = {}
            for g in self._groups:
                gopen = self._group_open.get(g["key"], True)
                self._append_disp({"type": "group", "g": g},
                                  self._group_widget(g, gopen))
                if not gopen:
                    continue
                for it in g["items"]:
                    if it["type"] == "row":
                        ri = it["ri"]
                        self._ri_disp[ri] = self._append_disp(
                            {"type": "row", "ri": ri},
                            self._row_widget(rows[ri], indent=12.0))
                        continue
                    fopen = self._frag_open.get(it["key"], False)
                    self._append_disp({"type": "frag", "it": it},
                                      self._frag_widget(it, fopen))
                    if not fopen:
                        continue
                    for ri in it["ris"]:
                        self._ri_disp[ri] = self._append_disp(
                            {"type": "row", "ri": ri},
                            self._row_widget(rows[ri], indent=28.0, dim=True))
            # 恢复列表选中到当前数据行(行被折叠起来时置 -1,详情面板保持不变)
            di = self._ri_disp.get(self._sel_idx)
            try:
                self.section_list.SelectedIndex = di if di is not None else -1
            except Exception:
                pass
        finally:
            self._loading_list = False
        has = bool(rows)
        self.content_grid.Visibility = Visibility.Visible if has else Visibility.Collapsed
        self.empty_hint.Visibility = Visibility.Collapsed if has else Visibility.Visible
        if not has:
            self.empty_hint.Text = _('未发现导星日志\n设备 log 目录暂无 PHD2_GuideLog_*.txt')
        n_frag = sum(len(it["ris"]) for g in self._groups for it in g["items"]
                     if it["type"] == "frag")
        hint = _("按拍摄目标分组 · {0} 组").format(len(self._groups))
        if n_frag:
            hint += _(" · {n_frag} 段短尝试已折叠").format(n_frag=n_frag)
        self.group_hint.Text = hint if has else ""

    def _group_widget(self, g: dict, open_: bool):
        """组头:▼/▶ + 目标名 + 段数徽章 + 合并 RMS 徽章,副行给起止/时长/丢星。

        右侧另有一个「仪表盘」按钮:打开本组全部段聚合的分析面板。
        按钮**不冒泡**成折叠切换 —— Button 会吃掉 PointerPressed,
        ListViewItem 收不到就不会改选中;万一某些输入路径仍触发,
        `_dash_guard` 会在 `_on_select` 里把这一次折叠切换吞掉并还原选中。

        **按组键缓存复用**:`_rebuild_list` 每次折叠/展开都会重铺整张列表,而
        win32more 的 `event` 描述符(_winrt.py 的 `event.__get__`)会把实例存进
        **类级** `_event_setters` 字典且**从不移除**(`-=` 与 `clear()` 只清
        `_callbacks`)—— 每次新建组头就等于永久泄漏 N 个 Button 及其闭包,
        `Click` 闭包里的 `gg=g` 还顺带 pin 住整组的 `TargetRun`。所以命中缓存时
        只改箭头字形,事件注册次数从「每次重建 x N 组」降到「每代 x N 组」。
        """
        cached = self._group_widgets.get(g["key"])
        if cached is not None and cached["g"] is g:
            cached["arrow"].Text = "▼" if open_ else "▶"
            return cached["outer"]
        outer = Grid()
        outer.ColumnSpacing = 6
        for width, unit in ((1.0, GridUnitType.Star), (1.0, GridUnitType.Auto)):
            cd = ColumnDefinition()
            cd.Width = GridLength(Value=width, GridUnitType=unit)
            outer.ColumnDefinitions.Append(cd)
        panel = StackPanel()
        panel.Spacing = 2
        head = StackPanel()
        head.Orientation = Orientation.Horizontal
        head.Spacing = 6
        arrow = TextBlock()
        arrow.Text = "▼" if open_ else "▶"   # BMP 字符,绝不用 emoji(§7.1)
        arrow.FontSize = 11
        arrow.Opacity = 0.7
        arrow.VerticalAlignment = VerticalAlignment.Center
        head.Children.Append(arrow)
        name = TextBlock()
        name.Text = g["title"]
        name.FontSize = 13
        name.FontWeight = FontWeights.SemiBold
        name.TextTrimming = TextTrimming.CharacterEllipsis
        name.VerticalAlignment = VerticalAlignment.Center
        head.Children.Append(name)
        head.Children.Append(self._badge(_("{0} 段").format(g['n_sec']), None))
        if g["rms"] is not None:
            head.Children.Append(
                self._badge(f"RMS {g['rms']:.2f}{g['unit']}", g["level"]))
        panel.Children.Append(head)
        sub = TextBlock()
        sub.Text = g["sub"]
        sub.FontSize = 11
        sub.Opacity = 0.65
        sub.TextTrimming = TextTrimming.CharacterEllipsis
        panel.Children.Append(sub)
        outer.Children.Append(panel)
        Grid.SetColumn(panel, 0)

        btn = Button()
        btn.Content = _("仪表盘")
        btn.FontSize = 11
        btn.Padding = Thickness(Left=8, Top=2, Right=8, Bottom=3)
        btn.VerticalAlignment = VerticalAlignment.Center
        ToolTipService.SetToolTip(
            btn, _("打开「{0}」的导星仪表盘(本组全部段聚合分析)").format(g['title']))
        key = g["key"]
        btn.PointerPressed += (lambda s, e, k=key: self._arm_dash_guard(k))
        btn.Click += (lambda s, e, gg=g: self._on_dash_click(gg))
        outer.Children.Append(btn)
        Grid.SetColumn(btn, 1)
        self._group_widgets[g["key"]] = {"outer": outer, "arrow": arrow, "g": g}
        return outer

    # ---------- 右侧视图切换(段视图 / 仪表盘视图) ----------

    def _set_view(self, view: str) -> None:
        """切换右侧分析区(两个视图叠在同一格,Visibility 互斥)。

        大折线图所在的段视图被折叠时 ActualWidth 归零,`CurveCanvas.SizeChanged`
        会带着 0 尺寸走一遍 `_render_curve`(它自己有 pw<40 的早退门槛,不画);
        重新可见时再触发一次,按新尺寸重画 —— 所以这里不需要手动补重绘。
        """
        if self._view == view:
            return
        self._view = view
        seg = view == VIEW_SEGMENT
        self.segment_view.Visibility = (Visibility.Visible if seg
                                        else Visibility.Collapsed)
        self.dash_host.Visibility = (Visibility.Collapsed if seg
                                     else Visibility.Visible)
        # 时间窗下拉/位置滑杆只服务段视图的大折线图
        self._update_win_controls()

    def show_segment_view(self) -> None:
        """右侧切回段视图(公开:仪表盘面板的「段视图」按钮也走这里)。

        仪表盘同时 `hide()`:停掉在途聚合、清空画布,免得看不见的面板
        还占着一屏 XAML 元素。
        """
        dash = self._dash
        if dash is not None:
            try:
                dash.hide()
            except Exception:
                pass
        self._set_view(VIEW_SEGMENT)

    def right_panel_width(self) -> float:
        """右侧分析区的可用宽度(仪表盘按它排图表)。

        取两个视图里**当前有布局结果**的那个:切到仪表盘的瞬间面板还没量过,
        而段视图的 ActualWidth 仍是上一轮布局的正确值(Visibility 改动到真正
        重新布局之间有一帧),所以调用方要在 `_set_view` **之前**问这个值。
        """
        for el in (self.dash_host, self.segment_view):
            try:
                w = float(el.ActualWidth or 0.0)
            except Exception:
                continue
            if w > 1.0:
                return w
        return 0.0        # 还没布局过:调用方用自己的缺省宽,随后靠 SizeChanged 校正

    # ---------- 仪表盘(右侧面板,_guidedash.GuideDashboard) ----------

    def _arm_dash_guard(self, key: str) -> None:
        """按下「仪表盘」按钮时布防:紧随其后的组头选中不当作折叠切换。"""
        self._dash_guard = key

    def _restore_selection(self) -> None:
        """把 ListView 选中还原到当前数据行(组头被误选中时用)。"""
        di = self._ri_disp.get(self._sel_idx)
        self._loading_list = True
        try:
            self.section_list.SelectedIndex = di if di is not None else -1
        except Exception:
            pass
        finally:
            self._loading_list = False

    def _ensure_dash(self):
        """懒建仪表盘(面板挂进右侧的 DashHost)。失败返回 None 并报错。"""
        if self._dash is None:
            try:
                # 延迟 import:_guidedash 反过来复用本模块的纯计算/绘制,
                # 模块级互相 import 会成环
                from astro_smb_gui._guidedash import GuideDashboard
                self._dash = GuideDashboard(self)
            except Exception as ex:
                self.shell.error(_("仪表盘初始化失败: {__name__}: {ex}").format(
                    __name__=type(ex).__name__, ex=ex))
                self._dash = None
        return self._dash

    def _on_dash_click(self, g: dict) -> None:
        self._dash_guard = None         # 按钮真的点到了,守卫用完即弃
        dash = self._ensure_dash()
        if dash is None:
            return
        try:
            # 宽度必须在切视图**之前**问:切完这一帧面板还没量过(见 right_panel_width)
            width = self.right_panel_width()
            self._set_view(VIEW_DASH)
            dash.show(g, self._rows or [], self._prepared_src, panel_w=width)
        except Exception as ex:         # async/事件处理器里的异常会被吞,必须落地
            # 走 show_segment_view 而不是只切 Visibility:show() 半路抛出时面板
            # 可能已经置了"在显",不 hide 掉的话在途聚合回来还会往看不见的画布上画
            self.show_segment_view()
            self.shell.error(_("打开仪表盘失败: {__name__}: {ex}").format(
                __name__=type(ex).__name__, ex=ex))

    def _frag_widget(self, it: dict, open_: bool):
        """碎段摘要行:淡色小字一行(展开后下面才是逐条明细)。"""
        tb = TextBlock()
        tb.Text = ("▼ " if open_ else "▶ ") + it["text"]
        tb.FontSize = 11
        tb.Opacity = 0.6
        tb.TextTrimming = TextTrimming.CharacterEllipsis
        tb.Margin = Thickness(Left=12, Top=0, Right=0, Bottom=0)
        return tb

    def _row_widget(self, row: dict, indent: float = 0.0, dim: bool = False):
        panel = StackPanel()
        panel.Spacing = 1
        if indent:
            panel.Margin = Thickness(Left=indent, Top=0, Right=0, Bottom=0)
        main = TextBlock()
        main.Text = row["main"]
        main.FontSize = 12 if dim else 13
        main.TextTrimming = TextTrimming.CharacterEllipsis
        if row["kind"] == "cal":
            panel.Opacity = 0.7                 # 校准行低调插入时间线
        else:
            main.FontWeight = FontWeights.SemiBold
        if dim:
            panel.Opacity = 0.75                # 展开出来的碎段明细压低存在感
        panel.Children.Append(main)
        if row.get("sub"):
            sub = TextBlock()
            sub.Text = row["sub"]
            sub.FontSize = 11
            sub.TextTrimming = TextTrimming.CharacterEllipsis
            brush = self._level_brush(row.get("level"))
            if brush is not None:
                sub.Foreground = brush          # RMS 按语义着色(好绿/警告琥珀/差红)
                sub.Opacity = 0.95
            else:
                sub.Opacity = 0.7
            panel.Children.Append(sub)
        return panel

    def _on_collapse_all(self, sender, e) -> None:
        """一键折叠/展开全部组(碎段簇不受影响,折叠碎段本就是默认态)。"""
        if not self._groups:
            return
        self._all_collapsed = not self._all_collapsed
        for g in self._groups:
            self._group_open[g["key"]] = not self._all_collapsed
        self.collapse_btn.Content = _("全部展开") if self._all_collapsed else _("全部折叠")
        self._rebuild_list()

    def _on_select(self, sender, e) -> None:
        if self._loading_list:
            return
        idx = self.section_list.SelectedIndex
        if idx is None or not (0 <= idx < len(self._disp)):
            self._select_row(-1)
            return
        item = self._disp[idx]
        kind = item["type"]
        if kind == "group":
            key = item["g"]["key"]
            if self._dash_guard == key:
                # 这一次选中是「仪表盘」按钮引起的,不当作折叠切换(需求 a)
                self._dash_guard = None
                self._restore_selection()
                return
            # 点组头 = 折叠切换;重建后列表选中会自动回到当前数据行
            self._group_open[key] = not self._group_open.get(key, True)
            self._rebuild_list()
            return
        if kind == "frag":
            key = item["it"]["key"]
            self._frag_open[key] = not self._frag_open.get(key, False)
            self._rebuild_list()
            return
        self._select_row(item["ri"])

    def _ensure_visible(self, ri: int) -> None:
        """展开数据行所在的组与碎段簇(必要时重建列表),供跳转定位使用。"""
        loc = self._loc.get(ri)
        if loc is None:
            return
        gkey, fkey = loc
        changed = False
        if not self._group_open.get(gkey, True):
            self._group_open[gkey] = True
            changed = True
        if fkey is not None and not self._frag_open.get(fkey, False):
            self._frag_open[fkey] = True
            changed = True
        if changed:
            self._all_collapsed = False
            self.collapse_btn.Content = _("全部折叠")
            self._rebuild_list()

    def _set_selection(self, ri: int) -> None:
        """按**数据行索引**程序化选中(总览柱/跳转定位/默认选中都走这里)。

        先把该行所在的组与碎段簇展开,再定位到显示项;SelectedIndex 相同时
        手动触发一次渲染保证幂等。
        """
        rows = self._rows or []
        if ri is None or not (0 <= ri < len(rows)):
            return
        self._ensure_visible(ri)
        di = self._ri_disp.get(ri)
        if di is None:
            self._select_row(ri)    # 兜底:列表定位不到也要把数据选上
            return
        try:
            if self.section_list.SelectedIndex == di:
                self._select_row(ri)
            else:
                self.section_list.SelectedIndex = di   # 触发 SelectionChanged
        except Exception:
            self._select_row(ri)

    def _chip(self, text: str, style: str = "neutral") -> Border:
        """胶囊徽章(浅底 + 深字):段视图标题行与仪表盘汇总卡同一套样式。"""
        bg, fg = self._chip_brushes.get(style, self._chip_brushes["neutral"])
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
        return chip

    def _row_badges(self, row: dict | None) -> list[tuple[str, str]]:
        """选中行 → 标题行徽章 [(文本, 语义)](纯数据,便于单测)。"""
        if row is None:
            return []
        if row["kind"] == "cal":
            cal = row.get("cal")
            ok = bool(cal is not None and cal.complete)
            return [(_("校准段"), "neutral"),
                    (_("校准成功"), "good") if ok else (_("校准失败"), "bad")]
        out: list[tuple[str, str]] = []
        rms = row.get("rms")
        if rms is not None and rms.n_frames > 0:
            u = "″" if rms.in_arcsec else "px"
            out.append((f"RMS {rms.rms_total:.2f}{u}", row.get("level") or "info"))
            lost_pct = (100.0 * rms.n_lost / max(1, rms.n_frames + rms.n_lost))
            out.append((_("丢星 {n_lost}({lost_pct:.1f}%)").format(
                n_lost=rms.n_lost, lost_pct=lost_pct),
                        "good" if lost_pct < 2.0
                        else "warn" if lost_pct < 8.0 else "bad"))
        else:
            out.append((_("无有效帧"), "bad"))
        out.append((_("{0:.1f} 分钟").format(row['duration'] / 60), "neutral"))
        if not row.get("main_seg"):
            out.append((_("短尝试"), "warn"))
        return out

    def _render_badges(self, row: dict | None) -> None:
        badges = self._row_badges(row)
        self.detail_badges.Children.Clear()
        for text, style in badges:
            self.detail_badges.Children.Append(self._chip(text, style))
        self.detail_badges.Visibility = (Visibility.Visible if badges
                                         else Visibility.Collapsed)

    def _set_stats(self, text: str) -> None:
        self.stats_text.Text = text
        self.stats_card.Visibility = (Visibility.Visible if text
                                      else Visibility.Collapsed)

    def _select_row(self, idx) -> None:
        # 选中任何一行 = 用户要看这一段 ⇒ 右侧必须是段视图
        self.show_segment_view()
        rows = self._rows or []
        if idx is None or idx < 0 or idx >= len(rows):
            self._sel_idx = -1
            self._current = None
            self.detail_title.Text = _("从左侧选择一个导星段")
            self._render_badges(None)
            self._set_stats("")
            self.canvas.Children.Clear()
            self.canvas_hint.Text = _("从左侧选择一个导星段查看 RA/DEC 偏差曲线")
            self.canvas_hint.Visibility = Visibility.Visible
            self.charts_scroll.Visibility = Visibility.Collapsed
            self._update_win_controls()
            return
        row = rows[idx]
        self._sel_idx = int(idx)
        self._current = row
        self.detail_title.Text = row["title"]
        self._render_badges(row)
        if row["kind"] == "cal":
            # 校准行:只显示信息文本,不画曲线/图表
            self.canvas.Children.Clear()
            self.canvas_hint.Text = _("校准段无导星曲线")
            self.canvas_hint.Visibility = Visibility.Visible
            self._set_stats(row["cal_text"])
            self.charts_scroll.Visibility = Visibility.Collapsed
            self._update_win_controls()
        else:
            self.canvas_hint.Visibility = Visibility.Collapsed
            self._set_stats(row["stats"])
            self._update_win_controls()
            self._render_curve()
            self._render_charts(row)

    # ---------- 时间窗(缩放/平移,UI 线程只做切片) ----------

    def _on_window_changed(self, sender, e) -> None:
        if self._ui_updating:
            return
        idx = self.window_combo.SelectedIndex
        if idx is None or not (0 <= idx < len(WINDOW_CHOICES)):
            return
        self._win_s = WINDOW_CHOICES[idx][1]
        self._update_win_controls()
        self._render_curve()

    def _on_pos_changed(self, sender, e) -> None:
        if self._ui_updating:
            return
        try:
            v = float(self.pos_slider.Value)
        except Exception:
            return
        self._win_pos = max(0.0, min(1.0, v / 1000.0))
        row = self._current
        if row is not None and row["kind"] == "guide" and self._win_s is not None:
            self._render_curve()

    def _update_win_controls(self) -> None:
        """窗口下拉/位置滑杆的可用状态。

        它们只缩放段视图的那张大折线图:仪表盘视图下没有这张图,必须置灰
        (不然滑一下什么也不动,像坏了)。
        """
        row = self._current
        guide = (self._view == VIEW_SEGMENT and row is not None
                 and row["kind"] == "guide")
        self.window_combo.IsEnabled = guide
        self.pos_slider.IsEnabled = (guide and self._win_s is not None
                                     and row["duration"] > self._win_s)

    def _window_range(self, row: dict) -> tuple[float, float]:
        """当前窗口对应的 [t0, t1](相对段起点的秒)。"""
        dur = max(row["duration"], 1e-9)
        win = self._win_s
        if win is None or win >= dur:
            return 0.0, dur
        t0 = self._win_pos * (dur - win)
        return t0, t0 + win

    # ---------- 跳转定位(拍摄记录页入口,UI 线程调用) ----------

    def show_range(self, t0: datetime, t1: datetime, label: str = "") -> None:
        """高亮 [t0, t1]:选中重叠的导星段并在曲线上画半透明区间。"""
        # 定位结果画在段视图的大曲线上:仪表盘视图占着同一格,不先切回来
        # 就什么也看不见(从拍摄记录页点「导星详情」实测复现)。
        self.show_segment_view()
        self._hl = (t0, t1, label or "")
        store = getattr(self.shell, "logstore", None)
        data = store.data if store is not None else None
        if self._rows is not None and self._prepared_src is data:
            self._locate_range()
            if self._refreshing:
                # 在途刷新完成会重建列表并清空选中——置 pending 让它按新数据重定位
                self._pending_locate = True
        else:
            self._pending_locate = True
            self._refresh(force=False)

    def _locate_range(self) -> None:
        t0, t1, label = self._hl or (None, None, "")
        if t0 is None:
            return
        rows = self._rows or []
        hit = None
        # 列表是倒序(最新在前);从尾往头即按时间升序,取时间上第一个重叠段
        for i in range(len(rows) - 1, -1, -1):
            r = rows[i]
            if r["kind"] != "guide":
                continue
            if r["begins"] <= t1 and r["end"] >= t0:
                hit = i
                break
        if hit is None:
            self.shell.info(_("未找到与该时段重叠的导星段")
                            + (f":{label}" if label else ""))
            return
        # 重置窗口为全段,保证高亮区间一定在视野内
        if self._win_s is not None or self._win_pos:
            self._ui_updating = True
            try:
                self.window_combo.SelectedIndex = 0
                self.pos_slider.Value = 0.0
            finally:
                self._ui_updating = False
            self._win_s = None
            self._win_pos = 0.0
        self._set_selection(hit)   # 会自动展开所在组/碎段簇,选中后带高亮矩形
        try:
            di = self._ri_disp.get(hit)
            if di is not None and 0 <= di < len(self._disp_widgets):
                self.section_list.ScrollIntoView(self._disp_widgets[di])
        except Exception:
            pass
        self.status_text.Text = self._status_base + (
            _(" · 已定位: {label}").format(label=label) if label else _(" · 已定位"))

    # ---------- 曲线渲染(UI 线程,数据已在工作线程算好) ----------

    def _render_curve(self) -> None:
        self.canvas.Children.Clear()
        row = self._current
        if row is None or row["kind"] != "guide":
            return
        w = float(self.canvas.ActualWidth or 0)
        h = float(self.canvas.ActualHeight or 0)
        pw, ph = w - ML - MR, h - MT - MB
        if pw < 40 or ph < 30:
            return
        t0, t1 = self._window_range(row)
        span = max(t1 - t0, 1e-9)
        rng = row["rng"]
        unit = row["unit"]

        def xpos(t: float) -> float:
            return ML + max(0.0, min(1.0, (t - t0) / span)) * pw

        def ypos(v: float) -> float:
            return MT + ph / 2 - (v / rng) * (ph / 2)

        # 跳转高亮区间(最底层;切片视图下按窗口裁剪)
        if self._hl is not None:
            h0, h1, _label = self._hl
            s0 = (h0 - row["begins"]).total_seconds()
            s1 = (h1 - row["begins"]).total_seconds()
            if s1 > t0 and s0 < t1:
                x0, x1 = xpos(max(t0, s0)), xpos(min(t1, s1))
                if x1 - x0 >= 1.0:
                    rect = Rectangle()
                    rect.Width = x1 - x0
                    rect.Height = ph
                    rect.Fill = self._b_hl
                    Canvas.SetLeft(rect, x0)
                    Canvas.SetTop(rect, MT)
                    self.canvas.Children.Append(rect)

        # ±整数角秒网格线 + 左侧刻度文字(步长挑成每侧最多 ~4 条)
        step = next((s for s in (1, 2, 5, 10, 20, 50, 100) if rng / s <= 4), 100)
        v = step
        while v <= rng + 1e-9:
            for sign in (1, -1):
                y = ypos(sign * v)
                self._grid_line(ML, ML + pw, y, y, self._b_grid, 1.0)
                self._tick_label(f"{sign * v:+d}{unit}", y)
            v += step
        # 0 轴(细线,稍亮)
        cy = ypos(0.0)
        self._grid_line(ML, ML + pw, cy, cy, self._b_axis, 1.0)
        self._tick_label("0", cy)

        # X 轴时间刻度(依窗口宽自适应步长,标签 HH:MM)
        self._time_ticks(row, t0, t1, xpos, pw, ph)

        # 丢星帧:底部红色小刻度线(窗口内二分切片,过多时抽稀)
        lost = row["lost"]
        li0 = bisect.bisect_left(lost, t0)
        li1 = bisect.bisect_right(lost, t1)
        seg = lost[li0:li1]
        stride = max(1, len(seg) // MAX_LOST_TICKS)
        for t in seg[::stride]:
            x = xpos(t)
            self._grid_line(x, x, MT + ph - 6, MT + ph, self._b_lost, 1.5)

        # RA / DEC 曲线:密(>2帧/像素)→ 包络带+滑动RMS;稀 → 逐点折线
        envelope = False
        npt = row["npt"]
        if npt is not None:
            i0 = int(np.searchsorted(npt, t0, "left"))
            i1 = int(np.searchsorted(npt, t1, "right"))
            nwin = i1 - i0
            if nwin > ENV_FRAMES_PER_PX * pw and nwin >= 16:
                envelope = True
                self._draw_envelope(row, i0, i1, pw, xpos, ypos, rng)
            elif nwin >= 2:
                ts = npt[i0:i1].tolist()
                ra_pts = _downsample(list(zip(ts, row["npra"][i0:i1].tolist())))
                dec_pts = _downsample(list(zip(ts, row["npdec"][i0:i1].tolist())))
                self._polyline(ra_pts, self._b_ra, xpos, ypos, rng)
                self._polyline(dec_pts, self._b_dec, xpos, ypos, rng)

        # 图例
        self._legend("RA", self._b_ra, ML + 6)
        self._legend("DEC", self._b_dec, ML + 36)
        if envelope:
            note = TextBlock()
            note.Text = _("包络视图:带=波动范围 · 实线=滑动RMS(30帧)")
            note.FontSize = 10
            note.Opacity = 0.6
            note.IsHitTestVisible = False
            Canvas.SetLeft(note, max(ML + 70.0, ML + pw - 230.0))
            Canvas.SetTop(note, MT + 2.0)
            self.canvas.Children.Append(note)

    def _draw_envelope(self, row: dict, i0: int, i1: int, pw: float,
                       xpos, ypos, rng: float) -> None:
        """包络带(每桶 min/max 半透明 Polygon)+ 30 帧滑动 RMS 实线。

        桶划分用 numpy reduceat(C 速度),UI 线程无逐帧 Python 循环。
        """
        npt = row["npt"]
        n = i1 - i0
        nb = max(8, int(pw // 3))
        nb = min(nb, n // 2)            # 保证 starts 严格递增
        starts = (np.arange(nb) * n) // nb
        tm = npt[i0 + starts]
        for vkey, fill in (("npra", self._b_ra_fill), ("npdec", self._b_dec_fill)):
            sub = row[vkey][i0:i1]
            mins = np.clip(np.minimum.reduceat(sub, starts), -rng, rng)
            maxs = np.clip(np.maximum.reduceat(sub, starts), -rng, rng)
            ring = [(xpos(float(tm[k])), ypos(float(maxs[k])))      # 上沿(正向)
                    for k in range(nb)]
            ring += [(xpos(float(tm[k])), ypos(float(mins[k])))     # 下沿(反向闭合)
                     for k in range(nb - 1, -1, -1)]
            self._append_poly(self.canvas, ring, None, fill=fill)
        # 滑动 RMS 主线(画在带之上)
        for rkey, brush in (("rms30ra", self._b_ra), ("rms30dec", self._b_dec)):
            rv = np.clip(row[rkey][i0 + starts], -rng, rng)
            self._polyline(list(zip(tm.tolist(), rv.tolist())),
                           brush, xpos, ypos, rng)

    def _time_ticks(self, row: dict, t0: float, t1: float,
                    xpos, pw: float, ph: float) -> None:
        """X 轴时间刻度:对齐到整步长的墙钟时刻,标签 HH:MM。"""
        span = max(t1 - t0, 1e-9)
        step = next((s for s in _TICK_STEPS if span / s <= 8), _TICK_STEPS[-1])
        begins = row["begins"]
        sod = begins.hour * 3600 + begins.minute * 60 + begins.second
        tt = math.ceil((sod + t0) / step) * step - sod
        while tt <= t1 + 1e-6:
            x = xpos(tt)
            self._grid_line(x, x, MT, MT + ph, self._b_grid, 1.0)
            lbl = TextBlock()
            lbl.Text = (begins + timedelta(seconds=tt)).strftime("%H:%M")
            lbl.FontSize = 10
            lbl.Opacity = 0.6
            lbl.IsHitTestVisible = False
            Canvas.SetLeft(lbl, max(2.0, min(x - 14.0, ML + pw - 30.0)))
            Canvas.SetTop(lbl, MT + ph + 4.0)
            self.canvas.Children.Append(lbl)
            tt += step

    def _grid_line(self, x1, x2, y1, y2, brush, thickness) -> None:
        ln = Line()
        ln.X1, ln.X2, ln.Y1, ln.Y2 = float(x1), float(x2), float(y1), float(y2)
        ln.Stroke = brush
        ln.StrokeThickness = float(thickness)
        self.canvas.Children.Append(ln)

    def _tick_label(self, text: str, y: float) -> None:
        lbl = TextBlock()
        lbl.Text = text
        lbl.FontSize = 10
        lbl.Opacity = 0.6
        lbl.IsHitTestVisible = False
        Canvas.SetLeft(lbl, 2.0)
        Canvas.SetTop(lbl, y - 7.0)
        self.canvas.Children.Append(lbl)

    def _legend(self, text: str, brush, x: float) -> None:
        lbl = TextBlock()
        lbl.Text = text
        lbl.FontSize = 11
        lbl.Foreground = brush
        lbl.IsHitTestVisible = False
        Canvas.SetLeft(lbl, x)
        Canvas.SetTop(lbl, MT + 2.0)
        self.canvas.Children.Append(lbl)

    def _polyline(self, pts, brush, xpos, ypos, rng: float) -> None:
        self._append_poly(self.canvas,
                          [(xpos(t), ypos(max(-rng, min(rng, v)))) for t, v in pts],
                          brush)

    # ---------- 统计图表区(UI 线程只画,数据已在工作线程算好) ----------

    def _render_charts(self, row: dict) -> None:
        ch = row.get("charts")
        if ch is None:
            self.charts_scroll.Visibility = Visibility.Collapsed
            return
        self.charts_scroll.Visibility = Visibility.Visible
        self._update_drift(ch)
        self._draw_scatter(ch)
        self._draw_hist(ch)
        self._draw_roll(ch)
        self._draw_pulse(ch)
        self._draw_period(ch)
        self._draw_snr(ch)
        self._draw_overview()

    def _line_on(self, cv, x1, y1, x2, y2, brush, thickness=1.0) -> None:
        ln = Line()
        ln.X1, ln.Y1, ln.X2, ln.Y2 = float(x1), float(y1), float(x2), float(y2)
        ln.Stroke = brush
        ln.StrokeThickness = float(thickness)
        cv.Children.Append(ln)

    def _text_on(self, cv, text: str, x: float, y: float,
                 size: float = 10.0, brush=None, opacity: float | None = 0.7) -> None:
        tb = TextBlock()
        tb.Text = text
        tb.FontSize = size
        if brush is not None:
            tb.Foreground = brush
        if opacity is not None:
            tb.Opacity = opacity
        tb.IsHitTestVisible = False
        Canvas.SetLeft(tb, x)
        Canvas.SetTop(tb, y)
        cv.Children.Append(tb)

    def _brush_hex(self, brush) -> str:
        """画刷 → #AARRGGBB。缓存在 :func:`_common.argb_hex` 里(那份缓存同样
        连画刷一起存,避免对象回收后 id 被复用取到别人的颜色)。

        兜底色沿用本页的 ``#5A0078D7`` —— 图表取不到颜色时宁可画成半透明蓝,
        也不要变成 ``_common`` 默认的灰,那在深色底上几乎看不见。
        """
        return argb_hex(brush, "#5A0078D7")

    def _append_rects(self, cv, rects: list) -> None:
        """把一批 (x, y, w, h, brush) 矩形铺到画布上 —— 图表绘制最贵的一类操作。

        优先走**一次 XamlReader.Load**的批量路径(见 `rect_fragment`);
        万一片段解析失败就退回逐个建元素 —— 慢,但一定画得出来。
        """
        if not rects:
            return
        try:
            frag = rect_fragment([(x, y, w, h, self._brush_hex(b))
                                  for x, y, w, h, b in rects])
            cv.Children.Append(XamlReader.Load(frag).as_(Canvas))
            return
        except Exception:
            pass
        for x, y, w, h, b in rects:
            r = Rectangle()
            r.Width, r.Height = float(w), float(h)
            r.Fill = b
            Canvas.SetLeft(r, x)
            Canvas.SetTop(r, y)
            cv.Children.Append(r)

    def _append_points(self, cv, xy: list[tuple[float, float]]) -> None:
        """散点云:定尺寸 2px 方点的批量铺设。"""
        self._append_rects(cv, [(x, y, 2.0, 2.0, self._b_pt) for x, y in xy])

    def _append_poly(self, cv, xy: list, brush, thickness: float = 1.5,
                     fill=None) -> None:
        """折线(或填充多边形)的批量铺设 —— 图表绘制第二贵的一类操作。

        `PointCollection.Append` 是逐点的 Python→WinRT 调用;一屏图表上千个点
        就是几百毫秒。优先走**一次 XamlReader.Load**(见 `poly_fragment`),
        片段解析失败再退回逐点追加(慢,但一定画得出来)。
        """
        if len(xy) < 2:
            return
        try:
            if fill is not None:
                frag = poly_fragment(xy, fill=self._brush_hex(fill))
            else:
                frag = poly_fragment(xy, stroke=self._brush_hex(brush),
                                     thickness=thickness)
            cv.Children.Append(XamlReader.Load(frag).as_(Canvas))
            return
        except Exception:
            pass
        shape = Polygon() if fill is not None else Polyline()
        if fill is not None:
            shape.Fill = fill
        else:
            shape.Stroke = brush
            shape.StrokeThickness = float(thickness)
        col = shape.Points
        if col is None:                          # 防御:极端情况下自建集合
            col = PointCollection()
            shape.Points = col
        for x, y in xy:
            col.Append(Point(X=float(x), Y=float(y)))
        cv.Children.Append(shape)

    def _draw_scatter(self, ch: dict, cv=None,
                      w: float = CHART_W, h: float = CHART_H) -> None:
        """星点云:x=RA 偏差 y=DEC 偏差,叠加 1×/2×RMS 圆与十字轴。

        `cv`/`w`/`h` 缺省即导星页自己的小图(与参数化前像素级等价);
        仪表盘(_guidedash)传入自己的画布与尺寸复用同一套画法。
        """
        cv = self.scatter_canvas if cv is None else cv
        cv.Children.Clear()
        cx, cy = w / 2.0, h / 2.0
        rng = max(ch["sc_rng"], 1e-9)
        s = (min(w, h) / 2.0 - 8.0) / rng
        # 十字轴
        self._line_on(cv, 4, cy, w - 4, cy, self._b_grid)
        self._line_on(cv, cx, 4, cx, h - 4, self._b_grid)
        # RMS 圆(1× 亮一些,2× 淡一些)
        for mult, brush in ((1.0, self._b_axis), (2.0, self._b_grid)):
            r = ch["rms_total"] * mult * s
            if 2.0 <= r <= min(w, h) / 2.0 - 2.0:
                el = Ellipse()
                el.Width = el.Height = 2.0 * r
                el.Stroke = brush
                el.StrokeThickness = 1.0
                Canvas.SetLeft(el, cx - r)
                Canvas.SetTop(el, cy - r)
                cv.Children.Append(el)
        # 点(超出量程的离群点直接不画)
        self._append_points(cv, [(cx + ra * s - 1.0, cy - dec * s - 1.0)
                                 for ra, dec in ch["sc_pts"]
                                 if abs(ra) <= rng and abs(dec) <= rng])
        if ch["rms_total"] > 0:
            self._text_on(cv, _("RMS {0:.2f}{1} · 圆=1×/2×RMS").format(
                ch['rms_total'], ch['unit']),
                          4.0, h - 15.0)

    def _draw_hist(self, ch: dict, cv=None,
                   w: float = CHART_W, h: float = CHART_H) -> None:
        """RA/DEC 偏差直方图(叠加半透明,范围 ±3×RMS)。"""
        cv = self.hist_canvas if cv is None else cv
        cv.Children.Clear()
        hist = ch["hist"]
        if hist is None:
            self._text_on(cv, _("无数据"), w / 2 - 20, h / 2 - 8)
            return
        m = 6.0
        base, top = h - 18.0, 8.0
        bw = (w - 2 * m) / HIST_BINS
        # RA/DEC 半透明叠画:必须保持"逐 bin 先 RA 后 DEC"的顺序(z 序 = 叠色顺序)
        bars = []
        for i in range(HIST_BINS):
            x = m + i * bw
            for key, brush in (("ra", self._b_ra_dim), ("dec", self._b_dec_dim)):
                v = hist[key][i]
                if v <= 0:
                    continue
                bh = v * (base - top)
                bars.append((x + 0.5, base - bh, max(1.0, bw - 1.0), bh, brush))
        self._append_rects(cv, bars)
        # 基线 + 中心 0 线
        self._line_on(cv, m, base, w - m, base, self._b_axis)
        self._line_on(cv, w / 2.0, top, w / 2.0, base, self._b_grid)
        self._text_on(cv, "RA", 6.0, h - 15.0, brush=self._b_ra, opacity=None)
        self._text_on(cv, "DEC", 28.0, h - 15.0, brush=self._b_dec, opacity=None)
        self._text_on(cv, f"±{hist['rng']:.1f}{ch['unit']}", w - 52.0, h - 15.0)

    def _draw_roll(self, ch: dict, cv=None,
                   w: float = CHART_W, h: float = CHART_H) -> None:
        """滚动总 RMS 曲线(60 秒窗),一眼看出恶化时段。"""
        cv = self.roll_canvas if cv is None else cv
        cv.Children.Clear()
        roll = ch["roll"]
        if len(roll) < 2:
            self._text_on(cv, _("无数据"), w / 2 - 20, h / 2 - 8)
            return
        m = 6.0
        base, top = h - 18.0, 10.0
        vmax = max(ch["roll_max"], 1e-6)
        rt0, rt1 = roll[0][0], roll[-1][0]
        tspan = max(rt1 - rt0, 1e-9)
        self._line_on(cv, m, base, w - m, base, self._b_axis)
        self._append_poly(
            cv,
            [(m + (t - rt0) / tspan * (w - 2 * m),
              base - min(v / vmax, 1.0) * (base - top)) for t, v in roll],
            self._b_roll)
        self._text_on(cv, _("峰值 {0:.2f}{1}").format(ch['roll_max'], ch['unit']), 6.0, 0.0)

    def _draw_pulse(self, ch: dict, cv=None,
                    w: float = CHART_W, h: float = CHART_H) -> None:
        """修正脉冲统计:RA E/W、DEC N/S 四根横条(次数 + 累计时长)。"""
        cv = self.pulse_canvas if cv is None else cv
        cv.Children.Clear()
        rows = ch["pulse"]
        # 行距:默认尺寸下恰为参数化前的 35.0(150-12)/4=34.5 → max 取 35
        pitch = max(35.0, (h - 12.0) / 4.0)
        max_ms = max((ms for _, _, ms, _ in rows), default=0)
        for k, (lab, cnt, ms, axis) in enumerate(rows):
            y = 6.0 + k * pitch
            self._text_on(cv, _("{lab}  {cnt} 次 · {0:.1f}s").format(
                ms / 1000, lab=lab, cnt=cnt), 6.0, y)
            if ms > 0 and max_ms > 0:
                bar = Rectangle()
                bar.Width = max(1.0, (w - 12.0) * ms / max_ms)
                bar.Height = 6.0
                bar.Fill = self._b_ra if axis == "ra" else self._b_dec
                Canvas.SetLeft(bar, 6.0)
                Canvas.SetTop(bar, y + 17.0)
                cv.Children.Append(bar)
        if max_ms == 0:
            self._text_on(cv, _("本段无修正脉冲"), 6.0, h - 18.0)

    def _poly_on(self, cv, pts: list[tuple[float, float]], brush,
                 thickness: float = 1.5) -> None:
        """在小图 Canvas 上画一条像素坐标折线(坐标已在调用方换算好)。"""
        self._append_poly(cv, pts, brush, thickness)

    def _update_drift(self, ch: dict) -> None:
        """漂移速率卡:RA/DEC 线性拟合斜率(数据已在工作线程算好)。

        DEC 漂移大常提示极轴误差:|DEC 斜率| > 0.5″/min 时文字变琥珀并附提示
        (px 口径段阈值语义不同,不做预警)。
        """
        d = ch.get("drift")
        unit = ch["unit"]
        if d is None:
            self.drift_ra_text.Text = _("数据不足")
            self.drift_dec_text.Text = "—"
            if self._fg_default is not None:
                self.drift_dec_text.Foreground = self._fg_default
            self.drift_hint_text.Text = ""
            return
        self.drift_ra_text.Text = _("RA 漂移 {0:+.2f}{unit}/min").format(d['ra'], unit=unit)
        self.drift_dec_text.Text = _("DEC 漂移 {0:+.2f}{unit}/min").format(
            d['dec'], unit=unit)
        warn = unit == "″" and abs(d["dec"]) > DRIFT_DEC_WARN
        if warn:
            self.drift_dec_text.Foreground = self._b_amber
            self.drift_hint_text.Text = _("(建议检查极轴)")
        else:
            if self._fg_default is not None:
                self.drift_dec_text.Foreground = self._fg_default
            self.drift_hint_text.Text = ""

    def _draw_period(self, ch: dict, cv=None,
                     w: float = CHART_W, h: float = CHART_H) -> None:
        """RA 周期图:重采样 rFFT 幅值谱,对数周期轴 30s~1200s,标最大峰。"""
        cv = self.period_canvas if cv is None else cv
        cv.Children.Clear()
        per = ch.get("period")
        if per is None:
            # 不断言具体原因(帧数/时长/重采样失败皆可能), 给出完整门槛
            self._text_on(cv, _("数据不足以做周期分析"),
                          w / 2 - 66.0, h / 2 - 16.0)
            self._text_on(cv, _("(需 ≥120 帧且时长 ≥3 分钟)"),
                          w / 2 - 74.0, h / 2 + 2.0)
            return
        m = 6.0
        base, top = h - 18.0, 14.0
        # 周期刻度(60/120/300/600s,位置已在工作线程按对数轴归一化)
        for xn, lab in per["ticks"]:
            x = m + xn * (w - 2 * m)
            self._line_on(cv, x, top, x, base, self._b_grid)
            self._text_on(cv, lab, x - 10.0, h - 15.0)
        self._line_on(cv, m, base, w - m, base, self._b_axis)
        self._poly_on(cv, [(m + xn * (w - 2 * m), base - an * (base - top))
                           for xn, an in per["pts"]], self._b_ra)
        # 最大峰:竖线 + 文字(蜗杆周期误差一眼可见)
        xpk = m + per["peak_x"] * (w - 2 * m)
        self._line_on(cv, xpk, top, xpk, base, self._b_dec, 1.0)
        self._text_on(cv, _("峰值 ~{0:.0f}s").format(per['peak_p']),
                      min(max(6.0, xpk + 4.0), w - 74.0), 0.0,
                      brush=self._b_dec, opacity=None)

    def _draw_snr(self, ch: dict, cv=None,
                  w: float = CHART_W, h: float = CHART_H) -> None:
        """SNR / 星质量曲线(视宁/透明度代理):双 Y 各自归一到 max。"""
        cv = self.snr_canvas if cv is None else cv
        cv.Children.Clear()
        sn = ch.get("snr")
        if not sn or (not sn["snr"] and not sn["mass"]):
            self._text_on(cv, _("无数据"), w / 2 - 20.0, h / 2 - 8.0)
            return
        m = 6.0
        base, top = h - 18.0, 14.0

        def to_xy(pts):
            return [(m + t * (w - 2 * m), base - v * (base - top)) for t, v in pts]

        self._line_on(cv, m, base, w - m, base, self._b_axis)
        self._poly_on(cv, to_xy(sn["mass"]), self._b_mass, 1.2)   # 星质量灰在下
        self._poly_on(cv, to_xy(sn["snr"]), self._b_ra)           # SNR 蓝在上
        self._text_on(cv, "SNR", 6.0, 0.0, brush=self._b_ra, opacity=None)
        self._text_on(cv, _("星质量"), 40.0, 0.0, brush=self._b_mass, opacity=None)
        self._text_on(cv, _("SNR {0:.1f}±{1:.1f} · 归一到各自max").format(sn['mean'], sn['std']),
                      6.0, h - 15.0)

    def _draw_overview(self) -> None:
        """逐段 RMS 柱状总览(整夜视角,不随选中段变;点击柱切换选中段)。

        柱色:<0.8″ 绿 / <1.5″ 琥珀 / 其余红;选中段的柱加描边。
        **不给单根柱挂 Tapped**(见 `_wire`:事件在画布上只挂一次)——
        这张图每选一次段就重画一次,逐根挂会被 win32more 的 event 描述符
        永久 pin 住;命中柱由 `overview_hit_bar` 按几何反算。
        """
        cv = self.segrms_canvas
        cv.Children.Clear()
        self._ov_bars = []      # 图已清掉,残留的命中表不能再被 Tapped 用上
        w, h = CHART_W, CHART_H
        self._ov_w = w          # 命中反算必须用**这次画的**那个宽度
        ov = self._overview
        if not ov or not ov["bars"]:
            self._text_on(cv, _("无数据"), w / 2 - 20.0, h / 2 - 8.0)
            return
        bars = ov["bars"]
        vmax = max(max(v for _, v in bars), 1e-6)
        m = BAR_M
        base, top = h - 18.0, 12.0
        # 阈值参考线(角秒口径才有意义)
        if ov["unit"] == "″":
            for tv in (BAR_GOOD, BAR_WARN):
                if tv <= vmax:
                    y = base - tv / vmax * (base - top)
                    self._line_on(cv, m, y, w - m, y, self._b_grid)
        n = len(bars)
        slot = (w - 2 * m) / n
        bw = max(2.0, min(18.0, slot - 2.0))
        rects, sel = [], None
        for k, (ri, v) in enumerate(bars):
            bh = max(1.5, v / vmax * (base - top))
            if ov["unit"] == "″":
                brush = (self._b_roll if v < BAR_GOOD
                         else self._b_amber if v < BAR_WARN else self._b_lost)
            else:
                # px 口径没有质量评级意义(阈值是角秒口径), 统一中性色
                brush = self._b_roll
            x = m + k * slot + (slot - bw) / 2.0
            rects.append((x, base - bh, bw, bh, brush))
            if ri == self._sel_idx:
                sel = (x, base - bh, bh)
        self._append_rects(cv, rects)       # 一次 XamlReader.Load 铺完所有柱
        if sel is not None:
            # 批量片段只带 Fill,选中柱的描边单独补一个元素(几何与柱完全重合)
            mark = Rectangle()
            mark.Width, mark.Height = bw, sel[2]
            mark.Stroke = self._b_sel
            mark.StrokeThickness = 1.5
            Canvas.SetLeft(mark, sel[0])
            Canvas.SetTop(mark, sel[1])
            cv.Children.Append(mark)
        self._ov_bars = [ri for ri, _ in bars]   # 命中反算按下标查这份表(与画出的柱同序)
        self._line_on(cv, m, base, w - m, base, self._b_axis)
        if ov["mixed"]:
            sub = _("{0} 段(仅角秒段) · 点击跳转").format(len(bars))
        else:
            sub = _("{0} 段 · 高=总RMS({1}) · 点击跳转").format(len(bars), ov['unit'])
        self._text_on(cv, sub, 6.0, h - 15.0)

    def _on_overview_tapped(self, sender, e) -> None:
        """逐段 RMS 总览的唯一 Tapped(画布级,`_wire` 里注册一次)。

        柱是画布的子元素(批量片段还多包一层子画布),Tapped 都会冒泡上来;
        坐标必须在处理器内**同步**取出(事件参数不能跨帧持有,与仪表盘的
        分段对比条同款)。
        """
        bars = self._ov_bars
        if not bars:
            return
        try:
            p = e.GetPosition(self.segrms_canvas)
            k = overview_hit_bar(float(p.X), len(bars), self._ov_w)
        except Exception:
            return
        if k is None:
            return
        self._set_selection(bars[k])
