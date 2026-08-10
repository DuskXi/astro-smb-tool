"""主题:**所有**颜色、字号、间距、圆角的唯一真源。

这一份是"颜值一直维持"的地基。规矩只有一条,但它是硬的:

> ``astro_smb_qt/pages/*.py`` 里**不许出现任何字面色值**,也不许直接
> ``setStyleSheet`` —— 样式只能来自这里和 :mod:`astro_smb_qt.widgets`。

门禁在 ``tests/test_qt_style.py``。没有它,写到第三页必然散掉:第一页认认真真
调好间距,第二页图快写个 ``#2A2A2A``,第三页开始每个控件各自 setStyleSheet,
最后谁也说不清"卡片的底色到底是哪个"。

**红光模式**是天文软件的刚需(夜间白蓝光会毁掉暗适应,恢复要二三十分钟),
所以它不是"换个配色主题"这种可选项 —— 两套调色板的**键必须完全一致**,
少一个键在红光下就是某个控件颜色取不到、变成透明或黑底黑字。这条也有门禁。
"""
from __future__ import annotations

from dataclasses import dataclass, fields

from PySide6.QtGui import QColor
from astro_smb.i18n import N_, gettext as _

# ------------------------------------------------------------------ 模式

MODE_LIGHT = "light"
MODE_NORMAL = "normal"
MODE_RED = "red"
#: 顺序 = 边栏上从左到右的顺序,也是"由亮到暗"的顺序。
MODES = (MODE_LIGHT, MODE_NORMAL, MODE_RED)

# 表里只标记,取用时才翻(模块级求值一次)
MODE_LABEL = {MODE_LIGHT: N_("白天"), MODE_NORMAL: N_("常规"),
              MODE_RED: N_("红光")}


# ------------------------------------------------------------------ 调色板


@dataclass(frozen=True)
class Palette:
    """一套完整配色。**每个字段在两套调色板里都必须有值。**"""

    # 表面层次:窗口底 → 边栏 → 卡片 → 卡内高亮。层次感来自这几档的**色差**,
    # 不是描边 —— 全用同一个底色再靠 1px 边框撑,界面会又硬又平。
    BG: str
    BG_ALT: str
    SURFACE: str
    SURFACE_HI: str
    BORDER: str
    BORDER_HI: str

    # 单一强调色。选中态、进度条、关键数值、图表主线 —— 只有这一个。
    ACCENT: str
    ACCENT_DIM: str
    ACCENT_SOFT: str
    ON_ACCENT: str
    #: **画在饱和色带上的文字**(treemap 的嵌套目录标题带)。
    #: 那条带子的颜色来自 treemap 调色板、与主题无关,所以这里
    #: 三档都要给一个**浅色** —— 白天档的 `TEXT` 是近黑,
    #: 压在中蓝底上几乎读不出来(老 UI 那里恒为白字)。
    #: 用 `ON_ACCENT` 是不行的:它在深色档里是近黑。
    ON_BAND: str

    # 文字灰度至少三档。全用一个颜色 = 没有层次。
    TEXT: str
    TEXT_DIM: str
    TEXT_FAINT: str

    # 语义色。红光模式下这三档靠**亮度**区分(不能用绿/蓝)。
    OK: str
    WARN: str
    BAD: str

    # 图表。CHART_A/B 是 RA/DEC 两条主线(老 UI 是蓝/橙)。
    CHART_A: str
    CHART_B: str
    CHART_C: str
    CHART_GRID: str
    CHART_AXIS: str
    CHART_BG: str

    # treemap 的分类底色(按扩展名类别取,来自 views.space.palette_index)
    TREEMAP: tuple[str, ...]
    # 夜次徽章:(底色, 字色)
    NIGHT: tuple[tuple[str, str], ...]

    SHADOW: str
    SCRIM: str


NORMAL = Palette(
    BG="#0E1116",
    BG_ALT="#0A0D12",
    SURFACE="#171B22",
    SURFACE_HI="#1E242E",
    BORDER="#262D3A",
    BORDER_HI="#36404F",
    ACCENT="#4FC3D7",
    ACCENT_DIM="#2C7B8B",
    ACCENT_SOFT="#12303A",
    ON_ACCENT="#06171C",
    ON_BAND="#F2F6FA",
    TEXT="#E4E9F0",
    TEXT_DIM="#96A0AE",
    TEXT_FAINT="#5F6875",
    OK="#4FBF87",
    WARN="#E0A44A",
    BAD="#E0655A",
    CHART_A="#4FC3D7",
    CHART_B="#F2A65A",
    CHART_C="#7FD18C",
    CHART_GRID="#232A35",
    CHART_AXIS="#3A4351",
    CHART_BG="#12161C",
    TREEMAP=(
        "#3C6E8F", "#3F7A5E", "#8A6234", "#7A4470",
        "#8A4048", "#3F7078", "#6E6A34", "#4A4E60",
    ),
    NIGHT=(
        ("#1E3A5F", "#9CC8F2"), ("#1D4331", "#94DCAE"),
        ("#4A2F1B", "#F2BE92"), ("#392551", "#C9A9EA"),
        ("#4A2029", "#F2A0A6"), ("#1E4549", "#92DBDF"),
        ("#3E3B19", "#DED792"), ("#2E3040", "#B4B8C6"),
    ),
    SHADOW="#04060A",
    SCRIM="#C0080B10",
)

#: 红光模式。**没有蓝色,没有绿色** —— 550nm 以上的长波才不破坏暗适应。
#: 三档语义色靠亮度区分:OK 最暗、BAD 最亮(要跳出来的那个)。
RED = Palette(
    BG="#0A0504",
    BG_ALT="#070302",
    SURFACE="#150B09",
    SURFACE_HI="#1F110E",
    BORDER="#3A1B15",
    BORDER_HI="#4E2620",
    ACCENT="#FF5B42",
    ACCENT_DIM="#8C3025",
    ACCENT_SOFT="#2A0E09",
    ON_ACCENT="#180402",
    ON_BAND="#F3D6CD",
    TEXT="#EBAA9B",
    TEXT_DIM="#AC756A",
    TEXT_FAINT="#734B44",
    OK="#B5604C",
    WARN="#E08663",
    # **BAD 要和 ACCENT 拉开。** 红光档整档只有红,`ACCENT=#FF5B42` 与
    # 原来的 `BAD=#FF4A34` 只差十几个 RGB 单位 —— 事件时间线上"某步开始"
    # (info,用 ACCENT)和"AutoCenter 失败"(err,用 BAD)两个点肉眼分不开,
    # 而那条修复的初衷正是"哪一步出了事一眼可辨"。常规/白天两档本来就差得远,
    # 只有这一档撞上了。
    #
    # 不能改成别的色相(红光档的意义就是不破坏暗适应),所以往**深**里压:
    # 保持红,靠明度分开。
    BAD="#D62B14",
    CHART_A="#FF7A5E",
    CHART_B="#AE5340",
    CHART_C="#8C4636",
    CHART_GRID="#2A1310",
    CHART_AXIS="#3E1D17",
    CHART_BG="#0F0705",
    TREEMAP=(
        "#5A241A", "#41180F", "#7A3323", "#2E1009",
        "#8A3D2A", "#4E1E13", "#6A2C1E", "#3A150D",
    ),
    NIGHT=(
        ("#3E1811", "#F0A48F"), ("#2C110C", "#CE8878"),
        ("#4E2016", "#FFB59D"), ("#210C07", "#B57565"),
        ("#452019", "#F2A896"), ("#320F09", "#D89482"),
        ("#57281C", "#FFC0A8"), ("#2A1009", "#C08574"),
    ),
    SHADOW="#030100",
    SCRIM="#C00A0504",
)

#: **白天模式。** 深色在暗房里舒服,可白天对着窗户、或者在户外看笔记本时
#: 反光严重、对比度全失。这一档是浅底深字,层次同样靠**色差**(不是靠边框):
#: 底 → 边栏 → 卡片 是**逐级变亮**的反过来(浅色主题里卡片比底更白)。
#:
#: 三个约束和另外两档一样,门禁盯着:
#: ① 字段与 :class:`Palette` 完全一致(少一个键就是某个控件取不到色);
#: ② 强调色只有一个;③ 语义三档要在浅底上都读得出来(不能直接抄深色那套 ——
#: 深色主题的 OK `#4FBF87` 放在白底上几乎看不见)。
LIGHT = Palette(
    BG="#F2F4F7",
    BG_ALT="#E8EBF0",
    SURFACE="#FFFFFF",
    SURFACE_HI="#F0F3F7",
    BORDER="#DCE1E9",
    BORDER_HI="#C2CAD6",
    ACCENT="#0D7286",
    ACCENT_DIM="#6BA9B5",
    ACCENT_SOFT="#DCEEF2",
    ON_ACCENT="#FFFFFF",
    ON_BAND="#FFFFFF",
    TEXT="#1B2028",
    TEXT_DIM="#5A6472",
    TEXT_FAINT="#8A94A2",
    OK="#1E7A4E",
    WARN="#96601A",
    BAD="#C0392B",
    CHART_A="#0F7F94",
    CHART_B="#C06A16",
    CHART_C="#2E8B57",
    CHART_GRID="#E4E8EE",
    CHART_AXIS="#B6BEC9",
    CHART_BG="#FBFCFD",
    TREEMAP=(
        "#8FBBD4", "#93C7AC", "#DEBE8E", "#C7A5D8",
        "#DFA0A6", "#93C4CA", "#CFC894", "#B4B8C6",
    ),
    NIGHT=(
        ("#D6E6F7", "#1B4B78"), ("#D6EFE0", "#1A5637"),
        ("#F7E4CE", "#7A4A18"), ("#E7DAF5", "#4B2C6E"),
        ("#F7D8DC", "#7A2932"), ("#D4EDEF", "#17565B"),
        ("#EFEBCB", "#5E5716"), ("#E1E3EA", "#3C4150"),
    ),
    SHADOW="#20304050",
    SCRIM="#B0FFFFFF",
)

PALETTES = {MODE_LIGHT: LIGHT, MODE_NORMAL: NORMAL, MODE_RED: RED}


# ------------------------------------------------------------------ 度量

class Space:
    """间距。**卡内边距 ≥12,卡间距 ≥10** —— 留白舍得给。"""

    XS = 4
    SM = 6
    MD = 10
    LG = 16
    XL = 24
    CARD_PAD = 12
    CARD_GAP = 10
    PAGE_PAD = 16
    NAV_PAD = 12


class Radius:
    CARD = 8
    CTL = 6
    CHIP = 9
    BAR = 3


class Font:
    """排版层次:标题 15~16 semibold、副标题 11 灰、正文 12~13。"""

    TITLE = 16
    H1 = 15
    H2 = 13
    BODY = 12
    SMALL = 11
    TINY = 10
    METRIC = 22
    #: 列表行首那个类型符号。**比正文大一号**是有原因的:老 UI 那侧是
    #: `FontIcon`(16px),跟正文同号会缩成一个看不清是什么的小点
    #: (独立验收量出来 Qt 约 7px、老 UI 约 16px)。
    ICON = 16
    #: 各平台的 CJK 兜底。**排在系统界面字体后面** —— 中文字形在
    #: `SF Pro` / `Cantarell` 这些拉丁字体里是没有的,得让它们接住。
    CJK_FALLBACK = ("Microsoft YaHei UI", "PingFang SC", "Hiragino Sans GB",
                    "Noto Sans CJK SC", "Source Han Sans SC", "WenQuanYi Micro Hei")
    #: 等宽字体名单搬到模块级的 `_PLATFORM_MONO` 了 —— 它要按平台排序,
    #: 而这个类只放尺寸。


#: 各平台**公开的**界面字体名。`QFontDatabase` 在 mac 上给的是
#: `.AppleSystemUIFont` —— 那是私有族名,QFont 认得,而 QSS 里按名字匹配
#: 未必认。所以查到的那个排第一,后面再垫一个这台机器上**一定存在**的
#: 公开名字:两条都不中的话又会触发那次全库扫描。
_PLATFORM_UI = {
    "darwin": ("SF Pro Text", "Helvetica Neue", "Lucida Grande"),
    "win32": ("Segoe UI",),
}
_PLATFORM_UI_DEFAULT = ("Cantarell", "Ubuntu", "DejaVu Sans")


#: 等宽字体,**按平台排序**。
#:
#: 这里不问 `QFontDatabase.systemFont(FixedFont)` —— 它在 Windows 上答
#: "Courier New",而我们要的是 Cascadia Mono。界面字体那一支问 Qt 是对的
#: (系统界面字体就该跟系统走),等宽这一支是**审美选择**,该由我们定。
#:
#: 排序的目的和 `ui_family` 一样:让第一顺位在本平台**存在**。原来第一位
#: 写死 `Cascadia Mono`,mac 上实测 `Populating font family aliases took
#: 164 ms` —— 和 `Segoe UI` 一模一样的毛病,上一轮只改了界面字体、漏了这个。
_PLATFORM_MONO = {
    "darwin": ("SF Mono", "Menlo", "Monaco"),
    "win32": ("Cascadia Mono", "Consolas"),
}
_PLATFORM_MONO_DEFAULT = ("DejaVu Sans Mono", "Liberation Mono", "Ubuntu Mono")


def mono_family() -> str:
    """等宽字体族。数字对齐要靠它(RMS、字节数、坐标那些列)。"""
    import sys as _sys

    names = list(_PLATFORM_MONO.get(_sys.platform, _PLATFORM_MONO_DEFAULT))
    # 别的平台的也留在后面当兜底 —— 有人装了 Cascadia 到 mac 上也认
    for group in (*_PLATFORM_MONO.values(), _PLATFORM_MONO_DEFAULT):
        names += [n for n in group if n not in names]
    names.append("monospace")
    return ", ".join(f'"{n}"' if " " in n else n for n in names)


def ui_family() -> str:
    """界面字体族,**问 Qt 要当前系统的那个**,不写死。

    原来第一顺位写死 `Segoe UI` —— 那是 Windows 的界面字体,mac 与 Linux 上
    根本不存在。后果有两层,而且都不报错:

    * **每次启动多花约 200 毫秒。** Qt 找不到就去遍历整个字体库建别名表,
      x86 Mac 上实测 ``Populating font family aliases took 198 ms``,
      而它自己在日志里就写着"换一个存在的字体来避免这个开销";
    * **最终用的是哪个字体我们说了不算** —— 落到 Qt 的兜底上,而整套排版
      尺寸(15/13/12/11 那几档)是照着一个具体字体调出来的。

    **要在 QGuiApplication 建好之后调**,所以这里是函数不是常量,由
    :func:`stylesheet` 现取。拿不到就退回平台默认名单 —— 那也比写死
    另一个平台的名字强。
    """
    import sys as _sys

    names: list[str] = []
    try:
        from PySide6.QtGui import QFontDatabase

        sys_family = QFontDatabase.systemFont(
            QFontDatabase.SystemFont.GeneralFont).family()
    except (ImportError, AttributeError, RuntimeError):
        # RuntimeError = 还没有 QGuiApplication。**只吞这三种** ——
        # 早先这里是裸 `except Exception`,把"枚举名写错了"也一起吞了,
        # 于是系统字体压根没进列表而一切看起来正常。
        sys_family = ""
    if sys_family:
        names.append(sys_family)
    names += [n for n in _PLATFORM_UI.get(_sys.platform, _PLATFORM_UI_DEFAULT)
              if n not in names]
    names += [f for f in Font.CJK_FALLBACK if f not in names]
    names.append("sans-serif")
    # QSS 里带空格的族名要引号,否则 `PingFang SC` 会被当成两个族
    return ", ".join(f'"{n}"' if " " in n else n for n in names)


#: 卡头左侧那道竖条的宽度(强调色)
ACCENT_BAR_W = 3
#: 侧边栏宽度
NAV_W = 188
#: 图表小图的标准尺寸(与 views.guiding.CHART_W/H 对齐由页面负责)
CHART_TILE_W = 236
CHART_TILE_H = 118


# ------------------------------------------------------------------ 当前调色板

class _Current:
    """当前生效的配色。

    **属性访问而不是重新绑定模块变量** —— 页面里 ``theme.C.ACCENT`` 与
    ``from ... import C`` 两种写法都要在切换红光模式后立刻生效。
    """

    def __init__(self) -> None:
        self._pal = NORMAL
        self._mode = MODE_NORMAL

    @property
    def mode(self) -> str:
        return self._mode

    def _set(self, mode: str) -> None:
        self._mode = mode
        self._pal = PALETTES[mode]

    def __getattr__(self, name: str):
        try:
            return getattr(object.__getattribute__(self, "_pal"), name)
        except AttributeError:
            raise AttributeError(_("主题里没有这个颜色: {name}").format(name=name)) from None


C = _Current()


class _QCache:
    """``theme.Q.ACCENT`` → :class:`QColor`。切模式时整体失效。"""

    def __init__(self) -> None:
        self._cache: dict[str, QColor] = {}

    def clear(self) -> None:
        self._cache.clear()

    def __getattr__(self, name: str) -> QColor:
        cache = object.__getattribute__(self, "_cache")
        hit = cache.get(name)
        if hit is None:
            hit = QColor(getattr(C, name))
            cache[name] = hit
        return hit


Q = _QCache()


def current_mode() -> str:
    return C.mode


def set_mode(mode: str) -> None:
    """切换配色。调用方负责重新 ``apply()`` 并让自绘控件 ``update()``。"""
    if mode not in PALETTES:
        raise ValueError(_("未知配色模式: {mode}").format(mode=mode))
    C._set(mode)
    Q.clear()


def toggle_mode() -> str:
    """切到**下一档**(白天 → 常规 → 红光 → 白天)。

    原来是 `RED if mode == NORMAL else NORMAL` 的两档写法 —— 加了白天档之后
    它会把人锁在常规/红光两档之间,白天档永远切不到,而且不报错。
    """
    at = MODES.index(C.mode) if C.mode in MODES else 0
    set_mode(MODES[(at + 1) % len(MODES)])
    return C.mode


# ------------------------------------------------------------------ 取色助手

def qcolor(hex_str: str) -> QColor:
    return QColor(hex_str)


def alpha(color: QColor, a: float) -> QColor:
    """同色不同透明度。``a`` 是 0~1。"""
    out = QColor(color)
    out.setAlphaF(max(0.0, min(1.0, a)))
    return out


def mix(a: QColor, b: QColor, t: float) -> QColor:
    """两色线性插值,``t=0`` 取 a。图表里做渐变/淡出用。"""
    t = max(0.0, min(1.0, t))
    return QColor(
        int(a.red() + (b.red() - a.red()) * t),
        int(a.green() + (b.green() - a.green()) * t),
        int(a.blue() + (b.blue() - a.blue()) * t),
    )


def screen_color(value) -> QColor:
    """任何来源的颜色 → 当前模式下真正该画的颜色。

    共享视图层(``views.skychart`` 的地平圈、``views.space`` 的 treemap 配色)
    产出的是**与主题无关的**色值 —— 那一层不知道也不该知道有红光模式。
    显示列表统一从这里过一道:常规模式是恒等,红光模式按亮度映射到强调色,
    于是"页面不写色值"和"红光模式真的红"这两条同时成立,而且只有一处实现。
    """
    col = value if isinstance(value, QColor) else QColor(value)
    if C.mode != MODE_RED:
        return col
    lum = (0.299 * col.redF() + 0.587 * col.greenF() + 0.114 * col.blueF())
    base = Q.ACCENT
    out = QColor(int(base.red() * lum), int(base.green() * lum),
                 int(base.blue() * lum))
    out.setAlpha(col.alpha())
    return out


#: 各种叫法 → 规范语义名。``views.*`` 那层用 ``good/warn/bad/dim``(设备页)
#: 与 ``ok/warn/error``(图元词表)两套,两套都认,免得每个页面自己去转。
#: 语义名 → 本主题的三档色。**共享层实际发出来的名字只有四个:
#: `ok` / `warn` / `err` / `info`**(`views/` 里 grep 得到),别的都是本地别名。
#:
#: 这里曾经有 `error` 却没有 `err` —— 于是 `tone_color("err")` 认不出、
#: 落回正文色,事件时间线上「自动居中·失败」那个标记是**近白色**,
#: 而绿的成功、琥珀的暂停都正常。**"哪一步出了事"那个色恰恰是唯一丢掉的。**
#: `tests/test_qt_tone_coverage.py` 现在按共享层的真实取值反查这张表。
_TONE_ALIAS = {
    "ok": "ok", "good": "ok", "success": "ok",
    "warn": "warn", "warning": "warn",
    "bad": "bad", "err": "bad", "error": "bad", "danger": "bad",
    "accent": "accent", "info": "accent",
    "dim": "dim", "muted": "dim", "faint": "faint",
}


def tone_name(tone: str | None) -> str:
    """规范化语义名;认不出返回空串(= 用默认前景色)。"""
    return _TONE_ALIAS.get((tone or "").lower(), "")


def tone_color(tone: str | None) -> QColor:
    """语义色名 → QColor。认不出返回正文色。"""
    return {
        "ok": Q.OK, "warn": Q.WARN, "bad": Q.BAD, "accent": Q.ACCENT,
        "dim": Q.TEXT_DIM, "faint": Q.TEXT_FAINT,
    }.get(tone_name(tone), Q.TEXT)


def night_pair(idx: int) -> tuple[str, str]:
    """夜次色号 → (底色, 字色)。

    ``views.browser.night_chip`` 只给色号,配色由主题决定 —— 那边的
    ``night_palette_argb`` 是给浅色主题调的(淡底深字),贴到近黑卡片上会
    炸眼。同一份**语义**(同夜同色、邻夜不同色),两套主题各自表达。
    """
    pal = C.NIGHT
    return pal[idx % len(pal)]


def treemap_color(idx: int) -> QColor:
    pal = C.TREEMAP
    return QColor(pal[idx % len(pal)])


def shade(color: QColor, depth: int) -> QColor:
    """treemap 逐层加深。深度每加一层压暗一档,层级才看得出来。"""
    k = max(0.35, 1.0 - 0.16 * max(0, depth))
    return QColor(int(color.red() * k), int(color.green() * k),
                  int(color.blue() * k))


# ------------------------------------------------------------------ QSS

def stylesheet() -> str:
    """整个应用的 QSS。**只有这里能写选择器。**

    **这一份不进翻译。** 它里面有中文**注释**,所以 i18n 的机械清扫把整份
    包进了 `_()` —— 后果有两层:词表里多出一条 9000 多字的"消息"(译者
    改一个字整个界面就坏),而且原来的多行 f-string 被压成了一行,
    没法读也没法改。样式表是代码不是文案。
    """
    c = C
    return f"""
* {{
    font-family: {ui_family()};
    font-size: {Font.BODY}px;
    outline: none;
}}
QWidget {{
    background: transparent;
    color: {c.TEXT};
}}
QMainWindow, #Root {{
    background: {c.BG};
}}
/* **对话框要自己的底色。** 上面那条 `QWidget {{ background: transparent }}`
   把它们也刷成透明了 —— 于是 `QMessageBox` 的正文是浅色字落在浅色系统底
   上,几乎看不见(独立验收 D7:"正文几乎与背景同色,只有按钮清楚")。
   顺带给正文一个明确的字色,别指望调色板兜底。 */
QDialog, QMessageBox {{
    background: {c.SURFACE};
}}
QMessageBox QLabel, QDialog QLabel {{
    color: {c.TEXT};
}}
#Sidebar {{
    background: {c.BG_ALT};
    border-right: 1px solid {c.BORDER};
}}
#PageArea {{
    background: {c.BG};
}}

/* ---------------------------------------------------------- 卡片 */
#Card {{
    background: {c.SURFACE};
    border: 1px solid {c.BORDER};
    border-radius: {Radius.CARD}px;
}}
#Card[flat="true"] {{
    background: {c.BG_ALT};
}}
#CardHeaderBar {{
    background: {c.ACCENT};
    border-radius: 1px;
}}
#Inset {{
    background: {c.SURFACE_HI};
    border: 1px solid {c.BORDER};
    border-radius: {Radius.CTL}px;
}}
/* 事件时间线的卡片:比 #Inset 更轻(没有边框),因为一屏会并排十几张,
   每张都描边的话整列看起来像一堆输入框。 */
#TimelineCard {{
    background: {c.SURFACE_HI};
    border-radius: {Radius.CTL}px;
}}

/* ---------------------------------------------------------- 文字分级 */
QLabel[role="title"] {{
    font-size: {Font.H1}px;
    font-weight: 600;
    color: {c.TEXT};
}}
QLabel[role="pagetitle"] {{
    font-size: {Font.TITLE}px;
    font-weight: 600;
    color: {c.TEXT};
}}
QLabel[role="subtitle"] {{
    font-size: {Font.SMALL}px;
    color: {c.TEXT_DIM};
}}
QLabel[role="body"] {{
    font-size: {Font.BODY}px;
    color: {c.TEXT};
}}
/* 正文加重。事件时间线的条目标题、以及任何"这一行是主句"的地方用它 ——
   `title` 会大一号,在密排的卡片列里显得太吵。 */
QLabel[role="strong"] {{
    font-size: {Font.BODY}px;
    font-weight: 600;
    color: {c.TEXT};
}}
QLabel[role="dim"] {{
    font-size: {Font.BODY}px;
    color: {c.TEXT_DIM};
}}
QLabel[role="faint"] {{
    font-size: {Font.SMALL}px;
    color: {c.TEXT_FAINT};
}}
QLabel[role="metric"] {{
    font-size: {Font.METRIC}px;
    font-weight: 600;
    color: {c.TEXT};
}}
QLabel[role="metric_accent"] {{
    font-size: {Font.METRIC}px;
    font-weight: 600;
    color: {c.ACCENT};
}}
QLabel[role="mono"] {{
    font-family: {mono_family()};
    font-size: {Font.SMALL}px;
    color: {c.TEXT};
}}
QLabel[role="group"] {{
    font-size: {Font.TINY}px;
    font-weight: 600;
    color: {c.TEXT_FAINT};
    letter-spacing: 1px;
}}
QLabel[tone="ok"]    {{ color: {c.OK}; }}
QLabel[tone="warn"]  {{ color: {c.WARN}; }}
QLabel[tone="bad"]   {{ color: {c.BAD}; }}
QLabel[tone="accent"]{{ color: {c.ACCENT}; }}

/* ---------------------------------------------------------- 状态胶囊 */
#Chip {{
    background: {c.SURFACE_HI};
    color: {c.TEXT_DIM};
    border: 1px solid {c.BORDER};
    border-radius: {Radius.CHIP}px;
    padding: 1px 8px;
    font-size: {Font.TINY}px;
}}
#Chip[tone="ok"]     {{ color: {c.OK};     border-color: {c.OK}; }}
#Chip[tone="warn"]   {{ color: {c.WARN};   border-color: {c.WARN}; }}
#Chip[tone="bad"]    {{ color: {c.BAD};    border-color: {c.BAD}; }}
#Chip[tone="accent"] {{ color: {c.ACCENT}; border-color: {c.ACCENT_DIM}; }}

/* ---------------------------------------------------------- 按钮 */
QPushButton {{
    background: {c.SURFACE_HI};
    color: {c.TEXT};
    border: 1px solid {c.BORDER_HI};
    border-radius: {Radius.CTL}px;
    padding: 5px 12px;
    font-size: {Font.BODY}px;
}}
QPushButton:hover {{
    background: {c.BORDER};
    border-color: {c.ACCENT_DIM};
}}
QPushButton:pressed {{
    background: {c.ACCENT_SOFT};
}}
QPushButton:disabled {{
    color: {c.TEXT_FAINT};
    border-color: {c.BORDER};
    background: {c.SURFACE};
}}
QPushButton[kind="primary"] {{
    background: {c.ACCENT};
    color: {c.ON_ACCENT};
    border-color: {c.ACCENT};
    font-weight: 600;
}}
QPushButton[kind="primary"]:hover {{
    background: {c.ACCENT_DIM};
    color: {c.TEXT};
}}
QPushButton[kind="danger"] {{
    color: {c.BAD};
    border-color: {c.BAD};
}}
QPushButton[kind="ghost"] {{
    background: transparent;
    border-color: transparent;
    color: {c.TEXT_DIM};
}}
QPushButton[kind="ghost"]:hover {{
    color: {c.TEXT};
    background: {c.SURFACE_HI};
}}

/* ---------------------------------------------------------- 导航 */
#NavItem {{
    background: transparent;
    border: none;
    border-radius: {Radius.CTL}px;
    color: {c.TEXT_DIM};
    padding: 6px 10px;
    text-align: left;
    font-size: {Font.H2}px;
}}
#NavItem:hover {{
    background: {c.SURFACE};
    color: {c.TEXT};
}}
#NavItem[active="true"] {{
    background: {c.ACCENT_SOFT};
    color: {c.ACCENT};
    font-weight: 600;
}}
#NavGroup {{
    color: {c.TEXT_FAINT};
    font-size: {Font.TINY}px;
    font-weight: 600;
    letter-spacing: 1px;
}}
#AppName {{
    color: {c.TEXT};
    font-size: {Font.H1}px;
    font-weight: 600;
}}
#AppMark {{
    color: {c.ACCENT};
    font-size: {Font.TITLE}px;
}}

/* ---------------------------------------------------------- 输入 */
QLineEdit {{
    background: {c.BG_ALT};
    border: 1px solid {c.BORDER_HI};
    border-radius: {Radius.CTL}px;
    padding: 5px 8px;
    color: {c.TEXT};
    selection-background-color: {c.ACCENT_DIM};
}}
QLineEdit:focus {{
    border-color: {c.ACCENT};
}}
QComboBox {{
    background: {c.SURFACE_HI};
    border: 1px solid {c.BORDER_HI};
    border-radius: {Radius.CTL}px;
    padding: 4px 8px;
    color: {c.TEXT};
    min-height: 18px;
}}
QComboBox:hover {{ border-color: {c.ACCENT_DIM}; }}
/* **不给 `::drop-down` / `::down-arrow` 写样式。**
   一旦写了,Qt 就不再画原生箭头 —— 而原来这里恰好只写了
   `border: none; width: 18px`,补都没补一个。结果是**全项目每一个下拉都
   长得跟普通输入框一模一样**,用户根本不知道它能点开(扫描页那个可编辑的
   尤其致命:它既能选也能填,而看上去只能填)。

   试过纯 QSS 画三角(宽高 0 + 三条边框)—— Qt 不吃这一套,画出来是个
   实心小方块,比没有更糟。要自绘就得带一个图片资产,而为一个箭头往包里
   塞文件不值。让 Qt 按平台画它自己的箭头,是这里最省事也最合规矩的解。 */
/* **一旦给控件写了颜色,Qt 就不再自动做置灰。**
   于是 `setEnabled(False)` 只挡住输入,看上去和能用的一模一样 ——
   导星页选中校准行时"窗口"下拉与位置滑杆确实失效了,而屏幕上
   零提示(独立验收逐像素比对:两张截图差异为 0)。
   `QPushButton` 早就有 `:disabled`,这几个漏了,一起补上。 */
QComboBox:disabled {{
    color: {c.TEXT_FAINT};
    background: {c.SURFACE};
    border-color: {c.BORDER};
}}
QComboBox QAbstractItemView {{
    background: {c.SURFACE};
    border: 1px solid {c.BORDER_HI};
    color: {c.TEXT};
    selection-background-color: {c.ACCENT_SOFT};
    selection-color: {c.ACCENT};
    outline: none;
}}
QLineEdit:disabled {{
    color: {c.TEXT_FAINT};
    background: {c.SURFACE};
    border-color: {c.BORDER};
}}
QCheckBox {{ color: {c.TEXT_DIM}; spacing: 6px; }}
QCheckBox:disabled {{ color: {c.TEXT_FAINT}; }}
QCheckBox::indicator:disabled {{ border-color: {c.BORDER}; }}
QCheckBox::indicator {{
    width: 13px; height: 13px;
    border: 1px solid {c.BORDER_HI};
    border-radius: 3px;
    background: {c.BG_ALT};
}}
QCheckBox::indicator:checked {{
    background: {c.ACCENT};
    border-color: {c.ACCENT};
}}

/* ---------------------------------------------------------- 表 */
QTableView, QTreeView {{
    background: {c.SURFACE};
    alternate-background-color: {c.SURFACE};
    border: 1px solid {c.BORDER};
    border-radius: {Radius.CARD}px;
    gridline-color: transparent;
    color: {c.TEXT};
    selection-background-color: {c.ACCENT_SOFT};
    selection-color: {c.TEXT};
}}
QTableView::item {{ padding: 2px 6px; border: none; }}
QTableView::item:selected {{
    background: {c.ACCENT_SOFT};
    color: {c.TEXT};
}}
QHeaderView::section {{
    background: {c.BG_ALT};
    color: {c.TEXT_FAINT};
    border: none;
    border-bottom: 1px solid {c.BORDER};
    padding: 5px 6px;
    font-size: {Font.TINY}px;
    font-weight: 600;
}}
QTableCornerButton::section {{ background: {c.BG_ALT}; border: none; }}
QListWidget {{
    background: {c.SURFACE};
    border: 1px solid {c.BORDER};
    border-radius: {Radius.CARD}px;
    color: {c.TEXT};
    outline: none;
}}
QListWidget::item {{ padding: 5px 8px; border-radius: {Radius.CTL}px; }}
QListWidget::item:selected {{
    background: {c.ACCENT_SOFT};
    color: {c.ACCENT};
}}
QListWidget::item:hover {{ background: {c.SURFACE_HI}; }}

/* ---------------------------------------------------------- 滚动条 */
QScrollBar:vertical {{
    background: transparent; width: 9px; margin: 0px;
}}
QScrollBar::handle:vertical {{
    background: {c.BORDER_HI}; border-radius: 4px; min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{ background: {c.ACCENT_DIM}; }}
QScrollBar:horizontal {{
    background: transparent; height: 9px; margin: 0px;
}}
QScrollBar::handle:horizontal {{
    background: {c.BORDER_HI}; border-radius: 4px; min-width: 28px;
}}
QScrollBar::handle:horizontal:hover {{ background: {c.ACCENT_DIM}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QScrollArea {{ border: none; background: transparent; }}

/* ---------------------------------------------------------- 进度 */
QProgressBar {{
    background: {c.BG_ALT};
    border: none;
    border-radius: {Radius.BAR}px;
    height: 6px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{
    background: {c.ACCENT};
    border-radius: {Radius.BAR}px;
}}

/* ---------------------------------------------------------- 其它 */
QSplitter::handle {{ background: {c.BORDER}; }}
QSplitter::handle:horizontal {{ width: 1px; }}
QSplitter::handle:vertical {{ height: 1px; }}
QToolTip {{
    background: {c.SURFACE_HI};
    color: {c.TEXT};
    border: 1px solid {c.BORDER_HI};
    padding: 4px 6px;
}}
QSlider::groove:horizontal {{
    height: 3px; background: {c.BORDER_HI}; border-radius: 2px;
}}
QSlider::sub-page:horizontal {{ background: {c.ACCENT}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {c.ACCENT}; width: 11px; margin: -5px 0;
    border-radius: 6px;
}}
QSlider::sub-page:horizontal:disabled {{ background: {c.BORDER_HI}; }}
QSlider::handle:horizontal:disabled {{ background: {c.TEXT_FAINT}; }}
QSlider::groove:horizontal:disabled {{ background: {c.BORDER}; }}
#Divider {{
    background: {c.BORDER};
}}
QMenu {{
    background: {c.SURFACE};
    border: 1px solid {c.BORDER_HI};
    color: {c.TEXT};
}}
QMenu::item:selected {{ background: {c.ACCENT_SOFT}; color: {c.ACCENT}; }}
"""


def apply(app) -> None:
    """把当前配色刷到 QApplication 上。"""
    app.setStyleSheet(stylesheet())


def palette_keys() -> list[str]:
    """两套调色板都必须有的字段名 —— 给门禁用。"""
    return [f.name for f in fields(Palette)]
