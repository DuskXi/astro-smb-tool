"""空间分析页:SpaceSniffer 式嵌套 treemap + 树形明细,逐层下钻(#5 第三轮)。

不会自动全盘扫描:必须点「扫描此目录」。扫描走 client.dir_tree 一次 BFS
构建整棵占用树(path→节点/父级索引在工作线程建好),结果按 (host, share,
path) 缓存;此后下钻/返回/展开/双向联动全部在内存树上完成,UI 线程只画,
不再有 SMB I/O 或重计算。

渲染开销约定(第四轮「空间分析卡」反馈后重写,改动前务必读懂):

* **treemap 零 per-block 委托、零 per-block ToolTip**。每块只有一个
  Rectangle;点击/悬停一律走 **canvas 级单一处理器 + 命中测试**
  (`self._hits` 按绘制顺序存 (x1,y1,x2,y2,node),**逆序**遍历取最上层块)。
  以前每块挂 `Tapped` lambda + `ToolTipService.SetToolTip`(每次调用都会
  新建一个 ToolTip 对象),400 块 = 400 委托 + 400 ToolTip,是主要卡顿源。
* **treemap 一次 `XamlReader.Load` 铺完**(#34):逐个 new 图元每个约 1~2ms
  (win32more 每次 Python→WinRT 调用约 0.2ms,一个带描边的 Rectangle 要
  7~9 次),900 块的**首帧**实测 878ms。改成把整批图元拼成 XAML 文本交给
  C++ 解析器后,同一批 900 块只要十几毫秒(见 `_flush_treemap` 与
  批量 XAML 片段。产物仍是**各自独立的 Rectangle**,z 序与
  半透明叠加完全不变;片段解析失败有逐元素回退(慢,但一定画得出来)。
  正因为批量路径每帧重建图元、拿不到稳定的单块引用,**联动高亮改成一个
  常驻的描边矩形**(xaml 里的 `HiliteRect`)按块几何移动 —— 不再回头改
  某个块自己的描边,`_block_map` 也随之从「块控件」变成「块几何」。
* **树形明细行按内容指纹复用**(#30):win32more 的 `event` 描述符把实例存进
  **类级** `_event_setters` 且**永不删除**(`-=`/`clear()` 只清 `_callbacks`),
  每行一个 `Tapped` 就意味着每次重建行都永久泄漏一个 Grid。探针实测:
  6 次折叠切换 → 条目 +24/+36/+60/+72/+96/+108,`Items.Clear()`+gc 之后
  仍有 120 个存活。复用后同一份内容只注册一次(见 `_row_cache`)。
* **悬停信息只有一个共享 ToolTip**(在 `_wire` 里**一次性**挂到 canvas 上,
  PlacementMode.Mouse;此后只改 `Content`,**绝不再调 SetToolTip** ——
  指针已进入 canvas 之后再挂,气泡不会弹),外加状态栏右侧常驻 `HoverText`
  (ToolTip 万一不可用时的兜底读数)。悬停命中测试节流 80ms,**前沿+尾沿**
  (尾沿定时器补做被节流丢弃的最后一次移动,否则鼠标停下时读数会停在错的块上),
  且**只在命中节点变化时**才改属性。
* **窗口缩放期间零重建**(第五轮反馈「缩放窗口很卡」后重做):以前是
  「8px 阈值 + 120ms 尾沿节流」,但每次尾沿仍然全量重建几百个控件,拖动
  过程中每 120ms 卡一下。现在改成 **ScaleTransform 即时缩放 + 停手后重画**:
  SizeChanged 只把 canvas 的 `RenderTransform`(一个常驻 ScaleTransform)
  改成 `新尺寸/上次渲染尺寸`——两次属性赋值,零控件创建,视觉连续;
  停止变化 250ms 后才复位 transform 并按新尺寸真正重画一次。
  缩放态期间(`_scale_xy != (1,1)`)命中测试整体停用:`_hits` 里的坐标
  属于旧尺寸,此时悬停/点击的读数必然是错的,宁可不响应。
  **树面板(ListView)在 resize 时完全不重建**:`_resize_flush` 只调
  `_render_treemap`,行控件原地保留,只刷新「未画出」圆点的可见性。
* **明细行 4~5 个控件**(Grid + 3 个 TextBlock,当前视图根的直接子目录多一个
  「未画出」圆点):折叠钮不是独立控件,而是名称前缀 "▶ "/"▼ ",整行一个
  Tapped 处理器按命中 x 坐标区分「点前缀=折叠」与「点其余=联动高亮」。
* **「未画出」标记只打在当前视图根的直接子目录上**:更深层的节点本来就要
  下钻才看得见,给它们打标只是噪音(第五轮反馈「不少行显示(未在图中)」)。
  视觉也从一句中文尾注降级为一个淡色小圆点 ○(图例在树面板标题上,
  标题带 XAML 声明的 ToolTip,不为每行新建 ToolTip 对象)。
"""

from __future__ import annotations

import ntpath
import threading
import time

from pathlib import Path

from win32more import asyncui
from win32more.Microsoft.UI.Text import FontWeights
from win32more.Microsoft.UI.Xaml import (
    FrameworkElement,
    GridLength,
    GridUnitType,
    HorizontalAlignment,
    TextTrimming,
    Thickness,
    VerticalAlignment,
    Visibility,
)
from win32more.Microsoft.UI.Xaml.Controls import (
    Button,
    Canvas,
    ColumnDefinition,
    ComboBox,
    ComboBoxItem,
    Grid,
    ListView,
    ProgressRing,
    TextBlock,
    ToolTip,
    ToolTipService,
)
from win32more.Microsoft.UI.Xaml.Controls.Primitives import PlacementMode
from win32more.Microsoft.UI.Xaml.Markup import XamlReader
from win32more.Microsoft.UI.Xaml.Media import ScaleTransform, SolidColorBrush
from win32more.Microsoft.UI.Xaml.Shapes import Rectangle
from win32more.Windows.Foundation import TimeSpan
from win32more.Windows.UI import Color

from astro_smb.client import SmbClientError, TransferCancelled, TreeNode
from astro_smb.util import human_size
from astro_smb.i18n import gettext as _
from astro_smb_gui import dircache
from astro_smb_gui._common import XAML_NS, ext_category

XAML_PATH = Path(__file__).with_name("space.xaml")

# 类别 → RGB(treemap 上色,文件按扩展名类别取色;TreeNode 鸭子兼容 ext_category)
_PALETTE = [
    (0x4F, 0x8A, 0xC7), (0x6A, 0xB0, 0x4F), (0xC7, 0x8A, 0x4F),
    (0xB0, 0x4F, 0x8A), (0x4F, 0xC7, 0xB0), (0x8A, 0x6A, 0xC7),
    (0xC7, 0x5F, 0x5F), (0x9A, 0x9A, 0x5F),
]
_DIR_COLOR = (0x55, 0x63, 0x74)
# 双向联动的高亮描边色。高亮框现在是 space.xaml 里的常驻 `HiliteRect`,
# 这里留作口径来源:改颜色要连 xaml 的 Stroke 一起改(单测钉死两处一致)。
_HL_RGB = (255, 200, 64)
_STROKE_ARGB = (90, 0, 0, 0)     # 普通块描边
_WHITE_ARGB = (255, 255, 255, 255)

# treemap / 明细的预算与几何常量(用户要求"不过于极端")
# 预算 320→900 的历史:先靠控件复用池 + 缓存 Canvas 静态接口 + 缓存属性接口
# 指针把**热重画**压到 900 块约 150~276ms(首帧仍要 878ms,因为 900 个控件
# 得逐个 new)。#34 改成批量 XAML 之后连首帧也一起解决了 —— 现在整批图元
# 一次 `XamlReader.Load`,冷热都是十几毫秒,复用池随之删除(见 _flush_treemap)。
# 重画只发生在**扫描完成 / 下钻 / 缩放停手**各一次(拖动期间走 ScaleTransform,
# 零重建)。预算留在 900 是为了让深层小目录也能上图(真机 Autorun/Bias 那种
# 几百个等大文件的目录,裁掉的正是预算),现在它已经不是耗时瓶颈了。
_BLOCK_BUDGET = 900          # canvas 控件总数预算,超出即停止更深递归
_MIN_NEST_AREA = 1600.0      # 目录块内继续嵌套布局的最小面积(px²)
_MAX_NEST_DEPTH = 4          # 嵌套最大深度(当前根的直接子级为第 0 层)
_TITLE_H = 14.0              # 嵌套目录块的标题带高度
_PAD = 4.0                   # 嵌套内边距
_STROKE_MIN = 6.0            # 小于这个边长的块不画描边(省一次几何)
_NEST_LABEL_W = 44.0         # 嵌套目录块画标题文字的最小宽度
_LEAF_LABEL_W = 72.0         # 叶级块画文字的最小宽/高
_LEAF_LABEL_H = 32.0
_MAX_ROWS = 300              # 右侧树形明细行数上限(超出截断+提示)
_ROW_INDENT = 14.0           # 明细行每层缩进
_ARROW_HIT_W = 20.0          # 行内「点这里=折叠」的前缀命中宽度
_ARROW_NONE = "　 "      # 无折叠钮时的占位(全角空格,与 ▶ 等宽)
# 「图中没有对应块」的标记:一个淡色小圆点(BMP 字符,绝不用 emoji,见 §7.1)。
# 只给**当前视图根的直接子目录**打——更深的节点本来就要下钻才看得见。
# 图例写在树面板标题上(XAML 里带 ToolTip),不为每行新建 ToolTip 对象。
_MARK_GLYPH = "○"

_HOVER_THROTTLE = 0.08       # 悬停命中测试节流(秒)
_RESIZE_EPS = 4.0            # 尺寸变化小于此值直接忽略(px)
_RESIZE_SETTLE = 0.25        # 尺寸停止变化多久后才真正重画(秒)


def _hex(argb: tuple) -> str:
    """(A, R, G, B) → XAML 颜色串 #AARRGGBB。

    `_common.argb_hex` 收的是 SolidColorBrush,而本页的块色一路都是元组
    (`_node_rgb` 直接算 RGB,从不建画刷),所以这里只做一次格式化,
    不再为了复用而先造一个用不上的画刷。
    """
    a, r, g, b = argb
    return f"#{a:02X}{r:02X}{g:02X}{b:02X}"


def _xml_text(s: str) -> str:
    """XAML 属性值转义(目录/文件名里 & < > " ' 和换行都可能出现)。

    换行必须写成 `&#10;` —— 叶级块标签是「名字\\n大小」两行,直接把裸换行
    塞进属性值,解析器会当成普通空白折掉。
    """
    out: list[str] = []
    esc = {"&": "&amp;", "<": "&lt;", ">": "&gt;",
           '"': "&quot;", "'": "&apos;"}
    for ch in str(s):
        cp = ord(ch)
        if ch == "\r":
            continue
        if ch == "\n":
            out.append("&#10;")
        elif cp > 0xFFFF:
            # Python→HSTRING 的长度按码点而不是 UTF-16 码元计算;把星平面
            # 字符留在 Python 串里会截掉片段末尾。字符引用全是 BMP/ASCII,
            # 由 XAML 解析器在 WinRT 内部还原,既保住文字也保住批量路径。
            out.append(f"&#x{cp:X};")
        elif cp < 0x20 and ch not in ("\t", "\n", "\r"):
            # XML 1.0 禁止的控制字符会让整层标签解析失败。
            out.append("\uFFFD")
        else:
            out.append(esc.get(ch, ch))
    return "".join(out)


def _block_fragment(fills, outlines) -> str:
    """色块 + 可选描边合成**同一个** Rectangle 的批量片段。

    拆成填充层和描边层会改变 WinUI 的几何:原来的 Stroke 会把 Fill 向内压
    0.5px,拆层后 Fill 外扩且圆角消失。这里恢复原先「单矩形同时 Fill/Stroke」
    的像素语义,图元还比双层少一半。
    """
    if not fills:
        return ""
    outlined = set(outlines)
    stroke = _hex(_STROKE_ARGB)
    body = "".join(
        f'<Rectangle Width="{w:.2f}" Height="{h:.2f}" Fill="{_hex(c)}"'
        + (f' Stroke="{stroke}" StrokeThickness="1"'
           f' RadiusX="2" RadiusY="2"' if (x, y, w, h) in outlined else "")
        + f' Canvas.Left="{x:.2f}" Canvas.Top="{y:.2f}"/>'
        for x, y, w, h, c in fills)
    return f'<Canvas xmlns="{XAML_NS}">{body}</Canvas>'


def _label_fragment(labels) -> str:
    """一批文字 (x, y, text, 字号, 字重, 宽度) → 批量片段。

    共享原语只覆盖 Rectangle/Line/Polyline,TextBlock 没有对应项,这里就地
    拼一份;字段与原先逐个建 TextBlock 时**逐条对齐**(白字 + 省略号裁剪 +
    固定宽度)。`IsHitTestVisible` 不必再逐个设:LabelCanvas 整层已经是
    `IsHitTestVisible="False"`,子元素继承。
    """
    if not labels:
        return ""
    fg = _hex(_WHITE_ARGB)
    body = "".join(
        f'<TextBlock Text="{_xml_text(t)}" FontSize="{fs:.1f}"'
        f' FontWeight="{fw}" Width="{w:.2f}" Foreground="{fg}"'
        f' TextTrimming="CharacterEllipsis"'
        f' Canvas.Left="{x:.2f}" Canvas.Top="{y:.2f}"/>'
        for x, y, t, fs, fw, w in labels)
    return f'<Canvas xmlns="{XAML_NS}">{body}</Canvas>'


def _row_key(node: TreeNode, parent: TreeNode, depth: int,
             expanded: bool) -> tuple:
    """明细行的**内容指纹** —— 行控件按它缓存复用(见 `SpacePage._detail_row`)。

    只按路径复用会在换代后显示上一棵树的大小/百分比(`_sky3d._row_cache`
    踩过同一个坑),所以把这一行真正渲染出来的每个字段都并进键里。
    """
    return (node.path, depth, expanded, node.is_dir, bool(node.children),
            node.name, node.size, parent.size)


# 明度调节与取色已下沉到 astro_smb_app.views.space —— 新前端要画同一张图。
# **顺带修了一个真 bug**:原来按文件类型取色用的是 `hash(ext_category(node))`,
# 而 Python 的字符串 hash **每个进程都不一样**(哈希随机化) —— 实测同一个
# "图像" 在三次进程里分别落到 1、2、2 号色,也就是**同一种文件类型的颜色
# 每次启动都在变**。共享实现改用 zlib.crc32,跨进程稳定。
from astro_smb_app.views.space import node_rgb as _node_rgb  # noqa: E402,F401
from astro_smb_app.views.space import shade as _shade  # noqa: E402,F401
from astro_smb_gui._xamli18n import load_text as _xaml_text


def _node_tip(node: TreeNode) -> str:
    """悬停/提示文本;只在鼠标真的停在某块上时才计算(不再每块预算一份)。"""
    tip = f"{node.name} · {human_size(node.size)}"
    if node.is_dir:
        tip += _(" · {file_count} 文件").format(file_count=node.file_count)
    return tip


def _index_tree(root: TreeNode) -> tuple[dict, dict]:
    """全树索引:path→节点 与 path→父路径。在工作线程建好,UI 线程零重计算。"""
    nodes: dict[str, TreeNode] = {root.path: root}
    parents: dict[str, str] = {}
    stack = [root]
    while stack:
        n = stack.pop()
        for c in n.children:
            nodes[c.path] = c
            parents[c.path] = n.path
            if c.is_dir:
                stack.append(c)
    return nodes, parents


def _load_cached_tree(host: str, share: str, path: str):
    """**工作线程**:读磁盘占用索引 → (树, path→节点, path→父路径, 缓存年龄秒)。

    解码与建索引都在工作线程做完(几万节点的 dict 构建放 UI 线程会顿一下)。
    """
    got = dircache.get_tree(host, share, path)
    if got is None:
        return None
    tree, age = got
    nodes, parents = _index_tree(tree)
    return tree, nodes, parents, age


def _mark_handled(e) -> None:
    """把路由事件标记为已处理(阻止折叠点击再冒泡到行选中)。"""
    try:
        e.Handled = True
    except Exception:
        pass


class SpacePage:
    def __init__(self, shell):
        self.shell = shell
        self.root = XamlReader.Load(_xaml_text(XAML_PATH)).as_(FrameworkElement)
        self.share: str | None = None
        self.path: str = ""                        # 扫描根路径(缓存键的一部分)
        self.tree: TreeNode | None = None          # 扫描根节点(整棵树)
        self.view_root: TreeNode | None = None     # 当前显示根(内存下钻,无需重扫)
        self._nodes: dict[str, TreeNode] = {}      # path → 节点(全树)
        self._parents: dict[str, str] = {}         # path → 父路径(全树)
        self._expanded: dict[str, bool] = {}       # 树形明细的手动展开记忆
        self._eff_expanded: dict[str, bool] = {}   # 本次渲染实际生效的展开态
        self._cancel: threading.Event | None = None
        self._scanning = False
        # (host, share, path) → (tree, nodes, parents)。**进程内**内存缓存;
        # 磁盘一层在 dircache(metacache),重开应用也在,见 _start_disk_load
        self._cache: dict[tuple, tuple] = {}
        self._load_token: object | None = None     # 在途磁盘索引读取的判废令牌
        self._shares = []
        # 本轮 treemap 的批量绘图缓冲(布局阶段只往里塞元组,收尾一次成图)
        self._fills: list[tuple] = []              # (x, y, w, h, #AARRGGBB)
        self._outlines: list[tuple] = []           # (x, y, w, h)
        self._labels: list[tuple] = []             # (x, y, 文本, 字号, 字重, 宽)
        # path → 块的**绘制几何** (x, y, w, h);高亮描边照它摆位,
        # 「未画出」圆点照它判在不在图里。每次渲染整体重建。
        self._block_map: dict[str, tuple] = {}
        # 命中测试表:按绘制顺序的 (x1, y1, x2, y2, node),逆序遍历取最上层
        self._hits: list[tuple] = []
        self._row_map: dict[str, Grid] = {}
        self._row_index: dict[str, int] = {}
        # **直接子目录**行 path → 「未画出」圆点 TextBlock(只切 Visibility)
        self._row_mark: dict[str, TextBlock] = {}
        # 明细行控件缓存:内容指纹 → {"g": Grid, "mark": 圆点或 None}。
        # 每行一个 Tapped,而 win32more 的事件注册**永不释放**(见模块头 #30),
        # 不复用就等于每次折叠/联动重建都永久泄漏一批 Grid。
        self._row_cache: dict[tuple, dict] = {}
        self._hl_path: str | None = None           # 联动高亮的节点路径
        self._brushes: dict[tuple, SolidColorBrush] = {}  # ARGB → 画刷(复用)
        self._blocks = 0                           # 本次 treemap 渲染的控件计数
        self._omitted = 0                          # 本次因预算/过小被省略的块数
        self._hover_path: str | None = None        # 当前悬停块(变化才改属性)
        self._hover_t = 0.0                        # 悬停命中测试节流时间戳
        self._hover_pending: tuple | None = None   # 被节流丢弃的最后一次坐标(尾沿补做)
        self._hover_timer = None                   # DispatcherQueueTimer(尾沿单次定时器)
        self._hover_tail_tm: threading.Timer | None = None   # 定时器不可用时的兜底
        self._last_size = (0.0, 0.0)               # 上次实际渲染时的画布尺寸
        self._resize_pending = False               # 已排队的重画(合并连续 resize)
        self._scale_tf: list = []                  # 两层 canvas 的常驻缩放变换
        self._scale_xy = (1.0, 1.0)                # 当前临时缩放(≠1 = 拖动中)
        self._resize_timer = None                  # DispatcherQueueTimer(单次)
        self._resize_tm: threading.Timer | None = None  # 定时器不可用时的兜底
        self._resize_evt_t = 0.0                   # 最近一次 SizeChanged 时刻
        self._tip: ToolTip | None = None           # 全页共享的唯一 ToolTip
        self._find_controls()
        self._wire()

    def _find_controls(self) -> None:
        f = self.root.FindName
        self.share_combo = f("ShareCombo").as_(ComboBox)
        self.up_btn = f("UpBtn").as_(Button)
        self.scan_btn = f("ScanBtn").as_(Button)
        self.scan_ring = f("ScanRing").as_(ProgressRing)
        self.path_text = f("PathText").as_(TextBlock)
        self.cache_hint = f("CacheHint").as_(TextBlock)
        self.status_text = f("StatusText").as_(TextBlock)
        self.hover_text = f("HoverText").as_(TextBlock)
        self.canvas = f("TreeCanvas").as_(Canvas)
        # 高亮层:一个常驻描边矩形(xaml 里就有),联动高亮只把它挪到目标块上。
        # 批量绘图每帧重建色块,拿不到稳定的单块引用去改描边,所以高亮独立成层。
        self.hilite_canvas = f("HiliteCanvas").as_(Canvas)
        self.hl_rect = f("HiliteRect").as_(Rectangle)
        # 文字单独一层(IsHitTestVisible=False):指针穿透,且标签永远压在
        # 色块之上(嵌套布局已避开父块标题带,不会互相遮挡)
        self.label_canvas = f("LabelCanvas").as_(Canvas)
        self.detail_list = f("DetailList").as_(ListView)

    def _wire(self) -> None:
        self.scan_btn.Click += self._on_scan
        self.up_btn.Click += self._on_up
        self.share_combo.SelectionChanged += self._on_share_changed
        # canvas 级单一处理器(点击/双击/悬停),块本身不挂任何委托
        self.canvas.SizeChanged += self._on_canvas_size
        self.canvas.Tapped += self._on_canvas_tapped
        self.canvas.DoubleTapped += self._on_canvas_double
        self.canvas.PointerMoved += self._on_canvas_pointer_moved
        self.canvas.PointerExited += self._on_canvas_pointer_exited
        # 共享 ToolTip:**一次性**挂在 canvas 上并一直保持,之后只改 Content。
        # 以前是等指针进了 canvas 才 SetToolTip、离块又设回 None —— 那样气泡实际
        # 几乎永远不弹(挂载晚于 PointerEntered,ToolTipService 已经错过开场时机)。
        try:
            tip = ToolTip()
            tip.Content = " "   # 空串会让气泡量成 0 尺寸,统一用一个空格占位
            ToolTipService.SetToolTip(self.canvas, tip)
            self._tip = tip     # 挂上了才认;Placement 是锦上添花,单独 try
        except Exception:
            self._tip = None    # 兜底:悬停信息只走状态栏右侧的 HoverText
        try:
            ToolTipService.SetPlacement(self.canvas, PlacementMode.Mouse)
        except Exception:
            pass
        # 悬停节流的尾沿定时器:单次、80ms,重复触发即重置(Stop+Start)
        self._hover_timer = self._make_timer(_HOVER_THROTTLE, self._on_hover_tick)
        # 缩放停手定时器:单次、250ms,每次 SizeChanged 重置 —— 只在停手后重画一次
        self._resize_timer = self._make_timer(_RESIZE_SETTLE, self._on_resize_tick)
        # 三层 canvas 各挂一个常驻 ScaleTransform:拖动期间把已画好的画面整体
        # 缩放(零重建);三层必须同步缩放,否则文字/高亮框会和色块错位
        self._scale_tf = []
        for cv in (self.canvas, self.hilite_canvas, self.label_canvas):
            try:
                tf = ScaleTransform()
                tf.ScaleX = tf.ScaleY = 1.0
                cv.RenderTransform = tf
                self._scale_tf.append(tf)
            except Exception:
                pass    # 兜底:退化为「拖动期间画面不动,停手后重画」

    def _make_timer(self, seconds: float, handler):
        """建一个单次 DispatcherQueueTimer;不可用时返回 None(各处有兜底)。"""
        try:
            t = self.shell.dispatcher.CreateTimer()
            span = TimeSpan()
            span.Duration = int(seconds * 1e7)   # TimeSpan 单位 = 100ns
            t.Interval = span
            t.IsRepeating = False
            t.Tick += handler
            return t
        except Exception:
            return None

    def _brush(self, argb: tuple) -> SolidColorBrush:
        """ARGB 四元组 → 画刷,实例级缓存复用(UI 线程专用)。"""
        b = self._brushes.get(argb)
        if b is None:
            c = Color()
            c.A, c.R, c.G, c.B = argb
            b = SolidColorBrush(c)
            self._brushes[argb] = b
        return b

    # ---------- 生命周期 ----------

    def on_show(self) -> None:
        pass

    def on_connected(self, shares) -> None:
        self._abort_scan()
        self._shares = shares
        self.share_combo.Items.Clear()
        for s in shares:
            it = ComboBoxItem()
            it.Content = s.name
            self.share_combo.Items.Append(it)
        self.share = None
        self.path = ""
        self._cache.clear()        # host 可能变了,旧树全部作废
        self._clear_view()
        self.cache_hint.Text = ""
        self.status_text.Text = ""
        self.path_text.Text = _("选择共享后点「扫描此目录」(不会自动全盘扫描)")
        self.up_btn.IsEnabled = False

    def on_close(self) -> None:
        """关窗清理(可选钩子;定时器都是 daemon,不接也不会挂住进程)。"""
        self._abort_scan()
        self._disarm_resize()

    def load_path(self, share: str, path: str) -> None:
        """从浏览页「分析占用」跳转进来(签名兼容,内部改走 dir_tree)。"""
        self.share, self.path = share, path
        # 同步下拉选择(share 相同时 _on_share_changed 会早退,不清状态)
        for i, s in enumerate(self._shares):
            if s.name == share:
                self.share_combo.SelectedIndex = i
                break
        cached = self._cache.get(self._key())
        if cached is not None:
            self._abort_scan()     # 命中缓存:丢弃在途扫描直接展示
            self.cache_hint.Text = _("(内存索引 · 本次运行扫过)")
            self._set_data(*cached)
        else:
            self.cache_hint.Text = ""
            self._start_disk_load()

    # ---------- 磁盘占用索引(dircache;内存缓存之下的第二层) ----------

    def _start_disk_load(self) -> None:
        """内存里没有就先查**磁盘**占用索引,还没有才真扫。

        本页原本只有进程内内存缓存,重开应用就得把整棵树重扫一遍(几百次
        listdir)。磁盘索引让"上次扫过的目录"下次也能秒出 —— 代价是数字可能
        已经过期,所以**必须**在 cache_hint 上标注"基于 N 分钟前的索引",
        并明确提示可以点「扫描此目录」重新统计。
        """
        self._abort_scan()
        host = getattr(self.shell.client, "host", "")
        share, path = self.share, self.path
        token = object()
        self._load_token = token
        self.status_text.Text = _("读取本地占用索引…")

        def work() -> None:
            try:
                got = _load_cached_tree(host, share, path)
            except Exception:
                got = None      # 缓存永远是可选的,坏了就当没有,老实重扫
            self.shell.ui(self._disk_load_done, token, share, path, got)

        threading.Thread(target=work, daemon=True, name="space-diskcache").start()

    def _disk_load_done(self, token, share, path, got) -> None:
        if token is not self._load_token:
            return                                  # 陈旧 worker
        if share != self.share or path != self.path:
            return
        self._load_token = None
        if got is None:
            self.status_text.Text = ""
            asyncui.create_task(self._scan())
            return
        tree, nodes, parents, age = got
        self._cache[self._key()] = (tree, nodes, parents)
        self.cache_hint.Text = _("(本地索引 · {0}的统计)").format(dircache.age_text(age))
        self.status_text.Text = (
            _("以下数字来自 {0}的索引,可能已过期 — 点「扫描此目录」重新统计").format(dircache.age_text(age)))
        self._set_data(tree, nodes, parents)

    # ---------- 交互 ----------

    def _key(self) -> tuple:
        return (self.shell.client.host, self.share, self.path)

    def _clear_view(self) -> None:
        """清掉整棵树与所有派生映射(数据换代)。"""
        self.tree = None
        self.view_root = None
        self._nodes = {}
        self._parents = {}
        self._expanded = {}
        self._eff_expanded = {}
        self._hl_path = None
        self._row_map = {}
        self._row_index = {}
        self._row_mark = {}
        # **不清 `_row_cache`** —— 与 `_set_data` 同一口径。
        #
        # 曾经这里写着"换代:旧行的大小/百分比已作废,不能再复用"。那个顾虑在
        # 设计上就不成立:`_row_key` 是**完整内容指纹**,连 `node.size` 与
        # `parent.size` 都并进了键 —— 大小/百分比一变必然是新键,取不到旧行。
        #
        # 而清掉它有实实在在的害处:win32more 的 Tapped 注册撤不掉,清缓存等于
        # 逼着下一代重建一批控件、永久泄漏一批注册。更要命的是 `_scan()` 开头
        # 就调本方法 —— **用户点「扫描此目录」重扫时缓存必然是空的**,
        # 于是"按内容指纹复用"这条修复在最主要的路径上被完全抵消。
        self._set_hover(None)
        self._reset_blocks()
        self._flush_treemap()      # 空缓冲 = 把色块层与文字层清空
        self._apply_highlight()    # 无高亮:把常驻描边框收起来
        self.detail_list.Items.Clear()

    def _update_path_text(self) -> None:
        node = self.view_root
        if node is not None:
            disp = (f"{self.share}/{node.path.replace(chr(92), '/')}"
                    if node.path else (self.share or ""))
        else:
            disp = (f"{self.share}/{self.path.replace(chr(92), '/')}"
                    if self.path else (self.share or ""))
        self.path_text.Text = disp or _("(未选择)")
        # 「上级」:内存树内可回退,或扫描根还有上层目录可重扫
        in_mem = (self.tree is not None and self.view_root is not None
                  and self.view_root is not self.tree)
        self.up_btn.IsEnabled = in_mem or bool(self.path)

    def _on_share_changed(self, sender, e) -> None:
        idx = self.share_combo.SelectedIndex
        if idx is None or not (0 <= idx < len(self._shares)):
            return
        share = self._shares[idx].name
        if share == self.share:
            return  # load_path 程序化同步下拉时走到这里
        # 换共享:取消在途扫描并复位扫描按钮/转环(修掉原先卡在「停止」的边角)
        self._abort_scan()
        self.share, self.path = share, ""
        self._clear_view()
        self.cache_hint.Text = ""
        self.status_text.Text = ""
        self._update_path_text()

    def _abort_scan(self) -> None:
        """取消在途扫描并复位扫描 UI;陈旧 worker 的回调经 token 判废。"""
        self._load_token = None     # 在途的磁盘索引读取也一并作废
        if self._cancel is not None:
            self._cancel.set()
            self._cancel = None
        if self._scanning:
            self._scanning = False
            self.scan_btn.Content = _("扫描此目录")
            self.scan_ring.IsActive = False

    def _go_up_memory(self) -> bool:
        """内存树内返回上级;成功返回 True(无需重扫)。"""
        if (self.tree is None or self.view_root is None
                or self.view_root is self.tree):
            return False
        ppath = self._parents.get(self.view_root.path)
        node = self._nodes.get(ppath) if ppath is not None else None
        self._set_view_root(node if node is not None else self.tree)
        return True

    def _on_up(self, sender, e) -> None:
        if self._go_up_memory():
            return
        if self.share and self.path:
            # 已在扫描根:上级目录不在树内,走缓存/重扫
            self.load_path(self.share, ntpath.dirname(self.path))

    def _on_canvas_double(self, sender, e) -> None:
        # 双击 treemap 任意处 = 返回父级(仅在内存树内回退,不触发重扫)
        self._go_up_memory()

    def _on_scan(self, sender, e) -> None:
        if self._scanning:
            if self._cancel is not None:
                self._cancel.set()  # worker 抛 TransferCancelled 后回调复位 UI
            return
        if self.share is None:
            self.shell.error(_("请先选择共享"))
            return
        asyncui.create_task(self._scan())

    # ---------- 扫描(dir_tree,一次拿整棵树) ----------

    async def _scan(self) -> None:
        # 先取消上一次仍在跑的扫描(load_path/_on_up 不经 _on_scan 守卫直达这里),
        # 保证任意时刻只有一个有效扫描线程
        if self._cancel is not None:
            self._cancel.set()
        self._load_token = None     # 用户要真扫:在途的磁盘索引读取作废
        self._scanning = True
        self.scan_btn.Content = _("停止")
        self.scan_ring.IsActive = True
        self.cache_hint.Text = ""
        self.status_text.Text = _("扫描中…")
        self._clear_view()
        self._update_path_text()
        cancel = threading.Event()
        self._cancel = cancel
        share, path = self.share, self.path
        host = getattr(self.shell.client, "host", "")
        last = [0.0]

        def on_progress(nfiles: int, nbytes: int) -> None:
            # 工作线程回调:节流 0.3s 才编组回 UI
            now = time.monotonic()
            if now - last[0] < 0.3:
                return
            last[0] = now
            self.shell.ui(self._progress_ui, cancel, nfiles, nbytes)

        def work() -> None:
            client = None
            try:
                client = self.shell.client.clone()
                client.connect()
                tree = client.dir_tree(share, path, cancel=cancel,
                                       on_progress=on_progress)
                nodes, parents = _index_tree(tree)  # 索引也在工作线程建
                # 落盘一份(partial 树 / 超大树由 put_tree 自己挡掉),
                # 下次(哪怕是重开应用)进同一路径就不用再扫几百次 listdir
                dircache.put_tree(host, share, path, tree)
                self.shell.ui(self._scan_done, share, path,
                              tree, nodes, parents, cancel)
            except TransferCancelled:
                self.shell.ui(self._scan_cancelled, cancel)
            except SmbClientError as ex:
                self.shell.ui(self._scan_failed, cancel, str(ex))
            except Exception as ex:     # 兜底:意外异常不许无声杀线程(§11)
                self.shell.ui(self._scan_failed, cancel, _("意外错误: {ex!r}").format(ex=ex))
            finally:
                if client is not None:
                    client.close()

        threading.Thread(target=work, daemon=True, name="space-scan").start()

    def _progress_ui(self, cancel, nfiles: int, nbytes: int) -> None:
        if cancel is not self._cancel:
            return  # 陈旧 worker
        self.status_text.Text = _("已扫 {nfiles} 文件 · {0}").format(
            human_size(nbytes), nfiles=nfiles)

    def _scan_done(self, share, path, tree, nodes, parents, cancel) -> None:
        # 陈旧 worker(路径已变或已被更晚的扫描取代)不得改动控件状态
        if share != self.share or path != self.path or cancel is not self._cancel:
            return
        self._scanning = False
        self.scan_btn.Content = _("扫描此目录")
        self.scan_ring.IsActive = False
        if getattr(tree, "partial", False):
            # 截断树(连接中断/部分目录枚举失败)不得当完整结果缓存
            self.shell.error(
                _("扫描不完整: {error_count} 个目录枚举失败, 结果仅供参考,建议重新扫描").format(
                    error_count=tree.error_count))
        else:
            self._cache[self._key()] = (tree, nodes, parents)
        self._set_data(tree, nodes, parents)

    def _scan_cancelled(self, cancel) -> None:
        if cancel is not self._cancel:
            return  # 陈旧 worker
        self._scanning = False
        self.scan_btn.Content = _("扫描此目录")
        self.scan_ring.IsActive = False
        self.status_text.Text = _("扫描已停止(无结果,可重新扫描)")

    def _scan_failed(self, cancel, msg) -> None:
        if cancel is not self._cancel:
            return  # 陈旧 worker
        self._scanning = False
        self.scan_btn.Content = _("扫描此目录")
        self.scan_ring.IsActive = False
        self.shell.error(_("扫描失败: {msg}").format(msg=msg))
        self.status_text.Text = _("扫描失败")

    # ---------- 数据换代 / 视图根切换 ----------

    def _set_data(self, tree: TreeNode, nodes: dict, parents: dict) -> None:
        self.tree = tree
        self._nodes = nodes
        self._parents = parents
        self.view_root = tree
        self._expanded = {}
        self._hl_path = None
        # 不清 _row_cache:键是完整内容指纹,同内容跨扫描可以安全复用;
        # 内容变化自然生成新键。清缓存反而会永久泄漏新一代的 Tapped 控件。
        self._render()

    def _set_view_root(self, node: TreeNode) -> None:
        """下钻/返回:换当前显示根并整体重画(树在内存,无 I/O)。

        行缓存**不清**:下钻再返回时同一批行的指纹一模一样,直接复用,
        既省重建也不再新增事件注册(同一棵树内来回下钻是最常见的操作)。
        """
        self.view_root = node
        self._hl_path = None
        self._render()

    def _render(self) -> None:
        self._update_path_text()
        self._render_treemap()      # 内部收尾会刷新状态栏(含「已省略 N 个小块」)
        self._render_detail()

    def _update_status(self) -> None:
        node = self.view_root
        if node is None:
            return
        disp = (f"{self.share}/{node.path.replace(chr(92), '/')}"
                if node.path else (self.share or ""))
        txt = (_("当前根 {disp} · 共 {0} · {file_count} 文件").format(
            human_size(node.size), disp=disp, file_count=node.file_count))
        if self._omitted > 0:
            txt += _(" · 图中已省略 {_omitted} 个小块(下钻可看清)").format(_omitted=self._omitted)
        self.status_text.Text = txt

    # ---------- treemap(嵌套 squarify) ----------

    def _on_canvas_size(self, sender, e) -> None:
        self._request_relayout()

    def _request_relayout(self) -> None:
        """拖动期间**零重建**:只改 ScaleTransform;停手 250ms 后才真重画。"""
        w = float(self.canvas.ActualWidth or 0)
        h = float(self.canvas.ActualHeight or 0)
        lw, lh = self._last_size
        if abs(w - lw) < _RESIZE_EPS and abs(h - lh) < _RESIZE_EPS:
            # 尺寸又回到上次渲染时的样子:撤掉临时缩放,连重画都省了
            self._apply_scale(1.0, 1.0)
            self._resize_pending = False
            self._disarm_resize()
            return
        if lw <= 0 or lh <= 0 or not self._hits:
            # 还没有可缩放的画面(首次布局/数据换代):下一轮消息泵直接画
            if not self._resize_pending:
                self._resize_pending = True
                try:
                    self.shell.ui(self._resize_flush)
                except Exception as ex:
                    # 排队失败必须复位脏标记,否则此后所有 resize 都被挡死
                    self._resize_pending = False
                    self.shell.error(_("空间分析重绘排队失败: {ex!r}").format(ex=ex))
            return
        # 两次属性赋值把整幅画面缩放到新尺寸 —— 视觉连续、完全不卡
        self._apply_scale(w / lw, h / lh)
        self._resize_pending = True
        self._arm_resize()

    def _apply_scale(self, sx: float, sy: float) -> None:
        """临时缩放已渲染画面(拖动期间)。sx=sy=1 表示恢复真实坐标。"""
        if self._scale_xy == (sx, sy):
            return
        self._scale_xy = (sx, sy)
        for tf in self._scale_tf:
            try:
                tf.ScaleX = sx
                tf.ScaleY = sy
            except Exception:
                pass

    def _arm_resize(self) -> None:
        """起/重置「停手」定时器(单次 250ms);拖动中反复调用只是不断续期。"""
        self._resize_evt_t = time.monotonic()
        t = self._resize_timer
        if t is not None:
            try:
                t.Stop()
                t.Start()      # 单次定时器:重复触发即重置 = 纯尾沿
                return
            except Exception:
                pass
        # 兜底:DispatcherQueueTimer 不可用时用 threading.Timer。**不重建线程**
        # (拖动一秒能来几十次 SizeChanged),到点若发现仍在拖就自行续期。
        if self._resize_tm is not None:
            return
        self._schedule_resize_fallback(_RESIZE_SETTLE)

    def _schedule_resize_fallback(self, wait: float) -> None:
        try:
            tm = threading.Timer(max(0.02, wait),
                                 lambda: self.shell.ui(self._resize_fallback_fire))
            tm.daemon = True
            self._resize_tm = tm
            tm.start()
        except Exception:
            self._resize_tm = None
            try:
                self.shell.ui(self._resize_flush)   # 定时器排不上就立刻重画
            except Exception as ex:
                self._resize_pending = False
                self.shell.error(_("空间分析重绘排队失败: {ex!r}").format(ex=ex))

    def _resize_fallback_fire(self) -> None:
        self._resize_tm = None
        left = _RESIZE_SETTLE - (time.monotonic() - self._resize_evt_t)
        if left > 0.01:
            self._schedule_resize_fallback(left)    # 还在拖:续期,别现在重画
            return
        self._resize_flush()

    def _disarm_resize(self) -> None:
        if self._resize_timer is not None:
            try:
                self._resize_timer.Stop()
            except Exception:
                pass
        if self._resize_tm is not None:
            try:
                self._resize_tm.cancel()
            except Exception:
                pass
            self._resize_tm = None

    def _on_resize_tick(self, sender, args) -> None:
        self._resize_flush()

    def _resize_flush(self) -> None:
        """停手后的唯一一次真重画:先复位临时缩放,再按新尺寸重建 treemap。

        **只重画 treemap,不碰右侧 ListView** —— 行控件原地保留,
        `_render_treemap` 收尾的 `_apply_row_marks` 只切圆点的 Visibility。
        """
        self._resize_pending = False
        self._disarm_resize()
        self._apply_scale(1.0, 1.0)
        try:
            self._render_treemap()
        except Exception as ex:   # 回调里的异常会被吞,必须显式落 InfoBar
            self.shell.error(_("空间分析重绘失败: {ex!r}").format(ex=ex))

    # ---------- 批量绘图 ----------

    def _reset_blocks(self) -> None:
        """开始新一轮渲染(或清空视图):清空绘图缓冲与派生映射。"""
        self._fills = []
        self._outlines = []
        self._labels = []
        self._block_map = {}
        self._hits = []
        self._blocks = 0
        self._omitted = 0

    def _flush_treemap(self) -> None:
        """把本轮缓冲一次性铺到画布上 —— treemap 渲染的全部 WinRT 开销都在这。

        两次 `XamlReader.Load`(色块 / 文字)代替 900+ 次逐元素创建:
        实测同一批 900 块首帧 878ms → 十几毫秒。**顺序即 z 序**:填充层在下、
        描边层居中、文字层在最上(文字本来就在独立画布上)。
        片段解析万一失败(理论上只会是文本转义出洞)就整体退回逐元素创建 ——
        慢,但一定画得出来,绝不能因为一个文件名把整页画白。
        """
        self._load_layer(self.canvas, [_block_fragment(
            self._fills, self._outlines)],
                         self._draw_blocks_slow)
        self._load_layer(self.label_canvas, [_label_fragment(self._labels)],
                         self._draw_labels_slow)

    def _load_layer(self, canvas, frags, slow) -> None:
        """把若干片段依次铺到一层画布上;任一片段出问题就整层走 `slow` 回退。

        回退前必须**再清一次** —— 前面的片段可能已经 Append 成功,不清就会
        和慢路径画出来的重叠成双份。
        """
        canvas.Children.Clear()
        try:
            for frag in frags:
                if frag:
                    canvas.Children.Append(XamlReader.Load(frag).as_(Canvas))
            return
        except Exception:
            pass
        try:
            canvas.Children.Clear()      # 半截片段先扔掉再走慢路径
            slow(canvas)
        except Exception as ex:
            self.shell.error(_("空间分析绘图失败: {ex!r}").format(ex=ex))

    def _draw_blocks_slow(self, canvas) -> None:
        """逐元素回退:色块 + 描边(与批量路径同一批几何、同一个 z 序)。"""
        outlined = set(self._outlines)
        for x, y, w, h, c in self._fills:
            r = Rectangle()
            r.Width, r.Height = w, h
            r.Fill = self._brush(c)
            if (x, y, w, h) in outlined:
                r.Stroke = self._brush(_STROKE_ARGB)
                r.StrokeThickness = 1.0
                r.RadiusX = r.RadiusY = 2.0
            Canvas.SetLeft(r, x)
            Canvas.SetTop(r, y)
            canvas.Children.Append(r)

    def _draw_labels_slow(self, canvas) -> None:
        for x, y, text, fs, fw, w in self._labels:
            t = TextBlock()
            t.Text = text
            t.FontSize = fs
            t.FontWeight = (FontWeights.SemiBold if fw == "SemiBold"
                            else FontWeights.Normal)
            t.Width = w
            t.Foreground = self._brush(_WHITE_ARGB)
            t.TextTrimming = TextTrimming.CharacterEllipsis
            Canvas.SetLeft(t, x)
            Canvas.SetTop(t, y)
            canvas.Children.Append(t)

    def _render_treemap(self) -> None:
        self._reset_blocks()
        self._set_hover(None)
        # 换根/换数据也可能在缩放态下发生(拖动中点了块):新画面按真实尺寸画,
        # 必须先把临时缩放撤掉,否则整幅图会被按旧比例二次缩放
        self._apply_scale(1.0, 1.0)
        node = self.view_root
        w = float(self.canvas.ActualWidth or 0)
        h = float(self.canvas.ActualHeight or 0)
        self._last_size = (w, h)
        items = ([c for c in node.children if c.size > 0]
                 if (node is not None and w >= 20 and h >= 20) else [])
        if items:
            total = sum(c.size for c in items)
            self._layout(items, 0, len(items), 2.0, 2.0, w - 4.0, h - 4.0,
                         0, total)
        self._flush_treemap()       # 一次成图(必须在布局之后)
        self._apply_highlight()     # 高亮框跟着新几何走;没块可高亮就收起来
        # 块预算随画布尺寸变化(resize 只重画 treemap 不重建行),圆点要跟着刷新
        self._apply_row_marks()
        self._update_status()

    def _layout(self, items: list, lo: int, hi: int, x, y, w, h,
                depth: int, total: int) -> None:
        """squarify:切一刀分两半递归;控件超预算即整体停手。

        用 [lo, hi) 下标区间代替列表切片,并把区间和顺着递归传下去 ——
        以前每层都 `items[:i]`/`items[i:]` 复制一次再重新 sum,深树上是纯浪费。
        """
        if lo >= hi:
            return
        if w <= 1 or h <= 1 or self._blocks >= _BLOCK_BUDGET:
            self._omitted += hi - lo
            return
        if hi - lo == 1:
            self._emit_block(items[lo], x, y, w, h, depth)
            return
        acc, i = 0, lo
        half = total / 2 if total else 0
        while i < hi - 1 and acc < half:
            acc += items[i].size
            i += 1
        frac = (acc / total) if total else 0.5
        if w >= h:
            wa = w * frac
            self._layout(items, lo, i, x, y, wa, h, depth, acc)
            self._layout(items, i, hi, x + wa, y, w - wa, h, depth, total - acc)
        else:
            ha = h * frac
            self._layout(items, lo, i, x, y, w, ha, depth, acc)
            self._layout(items, i, hi, x, y + ha, w, h - ha, depth, total - acc)

    def _emit_block(self, node: TreeNode, x, y, w, h, depth: int) -> None:
        """记一个块;目录块面积够大时在内部(标题带下)继续嵌套布局。

        块上**不挂任何委托、不挂 ToolTip**,只往 _hits 里记一条命中矩形。
        这里**一个 WinRT 调用都不发** —— 只往批量缓冲里塞元组,收尾由
        `_flush_treemap` 一次成图(#34)。所以布局递归本身现在是纯 Python。
        """
        if self._blocks >= _BLOCK_BUDGET or w < 2 or h < 2:
            self._omitted += 1
            return
        bw, bh = max(1.0, w - 2), max(1.0, h - 2)
        self._fills.append((x, y, bw, bh, (255,) + _node_rgb(node, depth)))
        if w >= _STROKE_MIN and h >= _STROKE_MIN:
            # 小块省掉圆角+描边:少一个图元,视觉上本来也看不出来
            self._outlines.append((x, y, bw, bh))
        self._blocks += 1
        self._block_map[node.path] = (x, y, bw, bh)
        self._hits.append((x, y, x + w, y + h, node))

        nest = (node.is_dir and node.children and depth < _MAX_NEST_DEPTH
                and w * h >= _MIN_NEST_AREA and self._blocks < _BLOCK_BUDGET)
        if nest:
            # 14px 标题带(后画的子块在上层,父块只在标题带/边距处可点)
            if w > _NEST_LABEL_W:
                self._labels.append((x + 5.0, y + 1.0, node.name, 10.0,
                                     "SemiBold", max(8.0, w - 10.0)))
                self._blocks += 1
            ix, iy = x + _PAD, y + _TITLE_H + 2.0
            iw, ih = w - 2.0 * _PAD, h - _TITLE_H - 2.0 - _PAD
            kids = [c for c in node.children if c.size > 0]
            if iw > 8 and ih > 8 and kids:
                self._layout(kids, 0, len(kids), ix, iy, iw, ih, depth + 1,
                             sum(c.size for c in kids))
        elif w > _LEAF_LABEL_W and h > _LEAF_LABEL_H:
            # 叶级大块:名称+大小;小块只有色块(悬停看共享 ToolTip)
            self._labels.append((x + 4.0, y + 3.0,
                                 f"{node.name}\n{human_size(node.size)}",
                                 11.0, "Normal", max(8.0, w - 8.0)))
            self._blocks += 1

    # ---------- canvas 级命中测试(替代每块委托/ToolTip) ----------

    def _hit_test(self, px: float, py: float) -> TreeNode | None:
        """逆序遍历绘制表:后画的子块在上层,先命中者胜。

        缩放态(拖动窗口期间)一律不命中:`_hits` 存的是**上次渲染尺寸**下的
        矩形,此时算出来的块必然是错的,宁可不响应也不给错读数/错下钻。
        """
        if self._scale_xy != (1.0, 1.0):
            return None
        for x1, y1, x2, y2, node in reversed(self._hits):
            if x1 <= px <= x2 and y1 <= py <= y2:
                return node
        return None

    def _on_canvas_tapped(self, sender, e) -> None:
        try:
            p = e.GetPosition(self.canvas)
            node = self._hit_test(float(p.X), float(p.Y))
        except Exception:
            node = None
        if node is None:
            return   # 点在空白处:和以前「块外无反应」一致
        self._on_block_tapped(node, e)

    def _on_canvas_pointer_moved(self, sender, e) -> None:
        # 坐标必须在处理器内**同步**取出:PointerRoutedEventArgs 不能跨帧持有
        try:
            p = e.GetCurrentPoint(self.canvas).Position
            pt = (float(p.X), float(p.Y))
        except Exception:
            return
        now = time.monotonic()
        if now - self._hover_t < _HOVER_THROTTLE:
            # 节流:鼠标划过整张图也只做十几次命中测试。但**不能直接丢弃** ——
            # 鼠标停下时的最后一次 PointerMoved 往往正好落在这里,丢了读数就停在
            # 上一个块上。存下坐标,交给尾沿定时器补做一次。
            self._hover_pending = pt
            self._arm_hover_tail()
            return
        self._hover_t = now
        self._hover_pending = None
        self._set_hover(self._hit_test(pt[0], pt[1]))

    def _arm_hover_tail(self) -> None:
        """起/重置尾沿定时器(单次 80ms),到点补做一次命中测试。"""
        t = self._hover_timer
        if t is not None:
            try:
                t.Stop()
                t.Start()      # 单次定时器:重复触发即重置
                return
            except Exception:
                pass
        # 兜底:DispatcherQueueTimer 不可用时用 threading.Timer 编组回 UI 线程。
        # 这里是「已排队就不重排」而非重置 —— 每个 PointerMoved 都建/杀一条线程
        # 的开销远大于它省下的那点延迟,而 _hover_pending 总是最新坐标,等效。
        if self._hover_tail_tm is not None:
            return
        try:
            tm = threading.Timer(_HOVER_THROTTLE,
                                 lambda: self.shell.ui(self._hover_tail_fire))
            tm.daemon = True
            self._hover_tail_tm = tm
            tm.start()
        except Exception:
            self._hover_tail_tm = None

    def _on_hover_tick(self, sender, args) -> None:
        self._hover_tail_fire()

    def _hover_tail_fire(self) -> None:
        """尾沿补做:用最后一次被丢弃的坐标重做命中测试(UI 线程)。"""
        self._hover_tail_tm = None
        pt = self._hover_pending
        self._hover_pending = None
        if pt is None:
            return   # 期间已被前沿处理或指针已离开
        self._hover_t = time.monotonic()
        try:
            self._set_hover(self._hit_test(pt[0], pt[1]))
        except Exception:
            pass     # WinRT 回调里的异常会被吞,别让它炸穿 COM 边界

    def _on_canvas_pointer_exited(self, sender, e) -> None:
        self._hover_t = 0.0
        self._hover_pending = None   # 已排队的尾沿到点后自然空转
        self._set_hover(None)

    def _set_hover(self, node: TreeNode | None) -> None:
        """更新共享 ToolTip 与状态栏悬停读数;**只在命中节点变化时**动属性。"""
        path = node.path if node is not None else None
        if path == self._hover_path:
            return
        self._hover_path = path
        text = _node_tip(node) if node is not None else ""
        try:
            self.hover_text.Text = text
        except Exception:
            pass
        if self._tip is None:
            return
        try:
            # ToolTip 在 _wire 里已一次性挂好,这里**只改 Content**:再调
            # SetToolTip 会在指针已进入 canvas 之后重挂,气泡就再也不弹了。
            # 没命中时给一个空格(不是 None/空串)保持挂载且不显示实质内容。
            self._tip.Content = text or " "
        except Exception:
            pass

    def _on_block_tapped(self, node: TreeNode, e) -> None:
        _mark_handled(e)
        if node.is_dir and node.children:
            self._set_view_root(node)      # 点目录块 = 下钻为新根
        else:
            self._hl_path = node.path      # 点文件/空目录块 = 高亮 + 联动树行
            self._apply_highlight()
            self._select_tree_row(node.path)

    def _apply_highlight(self) -> None:
        """把常驻高亮描边框摆到 _hl_path 对应的块上(没有就收起来)。

        以前是改那个块自己的 Stroke、再记一份原样用于还原;批量绘图之后每帧
        的块都是新图元,拿不到稳定引用,改成**独立一层的常驻矩形**按块几何
        移动 —— 少一套还原逻辑,换根/重画时也不会有"还原到已经不存在的块"。
        """
        got = self._block_map.get(self._hl_path) if self._hl_path else None
        try:
            if got is None:
                self.hl_rect.Visibility = Visibility.Collapsed
                return
            x, y, w, h = got
            self.hl_rect.Width, self.hl_rect.Height = w, h
            Canvas.SetLeft(self.hl_rect, x)
            Canvas.SetTop(self.hl_rect, y)
            self.hl_rect.Visibility = Visibility.Visible
        except Exception:
            pass

    # ---------- 树形明细(手工可折叠树) ----------

    def _render_detail(self) -> None:
        self.detail_list.Items.Clear()
        self._row_map = {}
        self._row_index = {}
        self._row_mark = {}
        self._eff_expanded = {}
        node = self.view_root
        if node is None:
            return
        rows: list = []
        truncated = self._build_rows(node, 0, rows)
        self._apply_row_marks()
        for g in rows:
            self.detail_list.Items.Append(g)
        if truncated:
            hint = TextBlock()
            hint.Text = _("… 超过 {_MAX_ROWS} 行已截断(下钻可查看更深内容)").format(_MAX_ROWS=_MAX_ROWS)
            hint.FontSize = 11.0
            hint.Opacity = 0.6
            self.detail_list.Items.Append(hint)
        # 行整体重建后恢复联动选中(不滚动)
        idx = self._row_index.get(self._hl_path) if self._hl_path else None
        if idx is not None:
            try:
                self.detail_list.SelectedIndex = idx
            except Exception:
                pass

    def _apply_row_marks(self) -> None:
        """刷新「未画出」圆点:块被预算/尺寸裁掉的**直接子目录**曾是纯死角
        (既不能下钻也不能高亮),标出来让用户知道这一行仍可点(点了会下钻)。

        `_row_mark` 里只登记当前视图根的直接子目录 —— 更深的节点本来就要
        下钻才看得见,给它们打标只是噪音。块预算随画布尺寸变(resize 只重画
        treemap 不重建行),所以每次 treemap 渲染结束都要重新过一遍。
        """
        w, h = self._last_size
        laid = w >= 20.0 and h >= 20.0   # 画布还没布局出来时别把所有目录都误标
        for path, tb in self._row_mark.items():
            off = laid and path not in self._block_map
            try:
                tb.Visibility = Visibility.Visible if off else Visibility.Collapsed
            except Exception:
                pass

    def _build_rows(self, parent: TreeNode, depth: int, rows: list) -> bool:
        """按需建可见行(折叠子树不建行);超 _MAX_ROWS 截断,返回是否截断。"""
        for child in parent.children:
            if len(rows) >= _MAX_ROWS:
                return True
            expanded = self._is_expanded(child, parent, depth)
            self._eff_expanded[child.path] = expanded
            g = self._detail_row(child, parent, depth, expanded)
            self._row_index[child.path] = len(rows)
            self._row_map[child.path] = g
            rows.append(g)
            if expanded and self._build_rows(child, depth + 1, rows):
                return True
        return False

    def _is_expanded(self, node: TreeNode, parent: TreeNode, depth: int) -> bool:
        """默认展开规则:根直接子级中 ≤10 个子项且占父 ≥8% 的目录展开一层;
        children >10 默认折叠;手动切换记忆优先。"""
        if not (node.is_dir and node.children):
            return False
        st = self._expanded.get(node.path)
        if st is not None:
            return st
        if depth > 0:
            return False       # 默认只展开一层(更深的须手动展开)
        if len(node.children) > 10:
            return False
        return node.size >= 0.08 * (parent.size or 1)

    def _detail_row(self, node: TreeNode, parent: TreeNode, depth: int,
                    expanded: bool) -> Grid:
        """一行 = Grid + 3 个 TextBlock + 1 个委托(折叠钮改成名称前缀);
        当前视图根的直接子目录再多一个「未画出」圆点(默认折叠不占位)。

        **按内容指纹缓存复用**(#30):`_render_detail` 在每次折叠切换、每次
        树↔图联动、每次下钻返回时都会整表重建,而整行的那个 `Tapped` 一旦挂上
        就再也摘不掉(win32more 的 `event` 描述符把实例存进类级 `_event_setters`
        且永不删除,`-=`/`clear()` 只清 `_callbacks`)。探针实测:6 次折叠切换
        泄漏 120 个 Grid,`Items.Clear()` + gc 之后一个都不掉。命中缓存后
        注册次数从「重建次数 × 行数」降到「出现过多少种不同的行内容」。
        """
        key = _row_key(node, parent, depth, expanded)
        hit = self._row_cache.get(key)
        if hit is not None:
            mark = hit["mark"]
            if mark is not None:
                self._row_mark[node.path] = mark
            return hit["g"]
        g = Grid()
        g.ColumnSpacing = 6
        for w_, u in ((1, GridUnitType.Star), (12, GridUnitType.Pixel),
                      (78, GridUnitType.Pixel), (40, GridUnitType.Pixel)):
            c = ColumnDefinition()
            c.Width = GridLength(Value=float(w_), GridUnitType=u)
            g.ColumnDefinitions.Append(c)

        name = TextBlock()
        if node.is_dir and node.children:
            arrow = "▼ " if expanded else "▶ "
        else:
            arrow = _ARROW_NONE
        # 目录前缀用 BMP 字符,**绝不能用 emoji**:win32more 把 Python str 转
        # HSTRING 时按码点数给长度,星平面字符(📁 等代理对)会让末尾少一个字符
        # ——真机现象是每个目录名都丢最后一字(Plan→Pla),真机踩过
        name.Text = arrow + ("▣ " if node.is_dir else "") + node.name
        name.TextTrimming = TextTrimming.CharacterEllipsis
        name.VerticalAlignment = VerticalAlignment.Center
        name.Margin = Thickness(Left=_ROW_INDENT * depth, Top=0, Right=0, Bottom=0)
        g.Children.Append(name)
        Grid.SetColumn(name, 0)

        if node.is_dir and depth == 0:
            # 只有直接子目录才可能「本该有块却被裁掉」;深层节点要下钻才看得见
            mark = TextBlock()
            mark.Text = _MARK_GLYPH
            mark.FontSize = 10.0
            mark.Opacity = 0.45
            mark.VerticalAlignment = VerticalAlignment.Center
            mark.HorizontalAlignment = HorizontalAlignment.Center
            mark.Visibility = Visibility.Collapsed   # _apply_row_marks 决定显隐
            g.Children.Append(mark)
            Grid.SetColumn(mark, 1)
            self._row_mark[node.path] = mark
        else:
            mark = None

        sz = TextBlock()
        sz.Text = human_size(node.size)
        sz.FontSize = 12.0
        sz.VerticalAlignment = VerticalAlignment.Center
        sz.HorizontalAlignment = HorizontalAlignment.Right
        g.Children.Append(sz)
        Grid.SetColumn(sz, 2)

        pct = TextBlock()  # 占父百分比小字
        pct.Text = f"{node.size / (parent.size or 1) * 100.0:.0f}%"
        pct.FontSize = 11.0
        pct.Opacity = 0.55
        pct.VerticalAlignment = VerticalAlignment.Center
        pct.HorizontalAlignment = HorizontalAlignment.Right
        g.Children.Append(pct)
        Grid.SetColumn(pct, 3)

        # 闭包只捕获 path/depth 这类不可变小值,**绝不捕获 TreeNode**:
        # 事件注册永不释放,捕获节点就等于把整棵子树也一起 pin 住;
        # 真正的节点每次点击时从 `self._nodes`(当前代)现查。
        g.Tapped += (lambda s, e, p=node.path, d=depth, host=g:
                     self._on_row_tapped_at(p, d, host, e))
        self._row_cache[key] = {"g": g, "mark": mark}
        return g

    def _on_row_tapped_at(self, path: str, depth: int, host, e) -> None:
        """整行唯一处理器:点在 ▶/▼ 前缀区 = 折叠切换,点其余 = 联动高亮。"""
        node = self._nodes.get(path)
        if node is None:
            return      # 复用的行属于已经换掉的那棵树:这一次点击直接忽略
        if node.is_dir and node.children:
            px = None
            try:
                px = float(e.GetPosition(host).X)
            except Exception:
                px = None   # 拿不到坐标就按折叠处理(折叠是更要紧的操作)
            if px is None or px <= _ROW_INDENT * depth + _ARROW_HIT_W:
                _mark_handled(e)
                self._toggle(node)
                return
        self._on_row_tapped(node)

    def _toggle(self, node: TreeNode) -> None:
        self._expanded[node.path] = not self._eff_expanded.get(node.path, False)
        self._render_detail()

    # ---------- 双向联动 ----------

    def _on_row_tapped(self, node: TreeNode) -> None:
        """树行点击 → treemap 高亮该块;不在当前视图内则先下钻到其父。"""
        self._hl_path = node.path
        if node.path in self._block_map:
            self._apply_highlight()
            return
        if not node.is_dir:
            self._apply_highlight()  # 文件块被裁掉:只清旧高亮
            return
        ppath = self._parents.get(node.path)
        pnode = self._nodes.get(ppath) if ppath is not None else None
        if pnode is not None and pnode is not self.view_root:
            # 下钻到其父后该目录成为顶级块;保留 _hl_path,渲染完自动加高亮
            self.view_root = pnode
            self._render()
            self._select_tree_row(node.path)
        elif node.children:
            # 父级就是当前视图根、块却被预算/尺寸裁掉的直接子目录:以前这里只能
            # 「清个旧高亮」,行既不下钻也不高亮 = 纯死角。改成直接下钻为新根
            # (_set_view_root 会清 _hl_path —— 它成了根,本来也不该有高亮块)。
            self._set_view_root(node)
        else:
            self._apply_highlight()  # 空目录且块被裁掉:只清旧高亮

    def _select_tree_row(self, path: str) -> None:
        """treemap → 树:必要时展开祖先,行滚动可见并选中。"""
        idx = self._row_index.get(path)
        if idx is None and self.view_root is not None:
            changed = False
            p = self._parents.get(path)
            while p is not None and p != self.view_root.path:
                if not self._eff_expanded.get(p, False):
                    self._expanded[p] = True
                    changed = True
                p = self._parents.get(p)
            if changed:
                self._render_detail()
                idx = self._row_index.get(path)
        if idx is None:
            return  # 行超出截断上限等,放弃定位
        try:
            self.detail_list.SelectedIndex = idx
            self.detail_list.ScrollIntoView(self._row_map[path])
        except Exception:
            pass
