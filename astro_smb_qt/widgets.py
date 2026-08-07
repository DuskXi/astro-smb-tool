"""复用件:每一页都只能用这些拼,不许各页自己画卡片。

和 :mod:`astro_smb_qt.theme` 一起构成"颜值一直维持"的执行机制。规矩:

* 页面**不写 QSS、不写色值**;要什么外观就来这里加一个件,或给已有件加一个变体。
* 这里的件**只管外观与通用交互**,不含任何业务 —— 业务在
  ``astro_smb`` / ``astro_smb_app.views``。

``DataTable`` 的 cell 形状刻意与 ``astro_smb_app.views.browser.row_cells``
同源(text / sub / align / weight / size),这样"哪一列是文件名"这种知识
仍然只存在于视图模型里,换一张表不用碰这里。**唯一的差别是颜色**:视图模型
给的是浅色主题的 ARGB,这边只收语义(``tone``)或色号(夜次),由主题决定实际颜色 ——
两套主题(常规/红光)才可能各自表达同一份语义。
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from typing import Any

from PySide6.QtCore import (QAbstractTableModel, QModelIndex, QPointF, QRect,
                            QRectF, QSize, Qt, QTimer, Signal)
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QImage, QPainter,
                           QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog,
                               QFrame, QHBoxLayout, QHeaderView, QLabel,
                               QLineEdit, QPushButton, QScrollArea, QSizePolicy,
                               QStyle, QStyledItemDelegate, QTableView,
                               QVBoxLayout, QWidget)

from astro_smb_qt import theme
from astro_smb.i18n import N_, gettext as _

# ------------------------------------------------------------------ 基础助手

_GAPS = {"none": 0, "xs": theme.Space.XS, "sm": theme.Space.SM,
         "md": theme.Space.MD, "lg": theme.Space.LG, "xl": theme.Space.XL,
         "card": theme.Space.CARD_GAP, "page": theme.Space.PAGE_PAD}

_PADS = dict(_GAPS, card=theme.Space.CARD_PAD, page=theme.Space.PAGE_PAD,
             nav=theme.Space.NAV_PAD)


def _gap(name: str | int) -> int:
    return name if isinstance(name, int) else _GAPS[name]


def _pad(name: str | int) -> int:
    return name if isinstance(name, int) else _PADS[name]


def vbox(parent: QWidget | None = None, *, gap: str | int = "md",
         pad: str | int = "none") -> QVBoxLayout:
    lay = QVBoxLayout(parent) if parent is not None else QVBoxLayout()
    lay.setSpacing(_gap(gap))
    p = _pad(pad)
    lay.setContentsMargins(p, p, p, p)
    return lay


def hbox(parent: QWidget | None = None, *, gap: str | int = "md",
         pad: str | int = "none") -> QHBoxLayout:
    lay = QHBoxLayout(parent) if parent is not None else QHBoxLayout()
    lay.setSpacing(_gap(gap))
    p = _pad(pad)
    lay.setContentsMargins(p, p, p, p)
    return lay


def restyle(root: QWidget) -> None:
    """切换配色后让整棵树重新取样式。

    Qt 的 QSS 是**在 polish 时算好存起来**的:只换 QApplication 的样式表,
    已经 polish 过的控件不会自动重算带动态属性的规则。切红光模式时不做这一步,
    症状是"大部分变了、少数几个还是原色" —— 不报错,只是看着脏。
    """
    stack = [root]
    while stack:
        w = stack.pop()
        st = w.style()
        st.unpolish(w)
        st.polish(w)
        w.update()
        stack.extend(w.findChildren(QWidget, options=Qt.FindDirectChildrenOnly))


def set_prop(w: QWidget, name: str, value) -> None:
    """改动态属性并立刻重新取样式(不 repolish 的话 QSS 选择器不会重算)。"""
    if w.property(name) == value:
        return
    w.setProperty(name, value)
    w.style().unpolish(w)
    w.style().polish(w)


# ------------------------------------------------------------------ 文字

def label(text: str = "", *, role: str = "body", tone: str | None = None,
          wrap: bool = False, tip: str = "") -> QLabel:
    lb = QLabel(text)
    lb.setProperty("role", role)
    if tone:
        lb.setProperty("tone", theme.tone_name(tone))
    lb.setWordWrap(wrap)
    if tip:
        lb.setToolTip(tip)
    lb.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return lb


#: 允许换行的位置(插零宽空格用)。ASIAIR 的文件名就是靠这几个符号分段的。
_BREAK_AFTER = "_-."


#: `glyph_px` 的结果缓存 —— 每次 paint 都量两遍字体度量太亏(一屏几十行)。
_GLYPH_PX: dict[tuple[str, int, str], int] = {}


def _qt_measure(ch: str, base: QFont, px: int) -> tuple[int, int]:
    """在字号 ``px`` 下量一个字的 ``(墨高, 步进宽)``。"""
    probe = QFont(base)
    probe.setPixelSize(int(px))
    fm = QFontMetrics(probe)
    return fm.tightBoundingRect(ch).height(), fm.horizontalAdvance(ch)


def glyph_px(ch: str, target_ink: int, base: QFont, max_w: int = 0,
             measure=None) -> int:
    """把符号的**墨迹**放到 ``target_ink`` 那么高,返回该用多大的字号。

    独立验收量出来:老 UI 的 `FontIcon` 墨迹 17×16 px,Qt 这边 9×9 —— 差
    约 40% 线性,看起来"明显小于文字的一个小点"。第一版的修法是把字号从
    12 抬到 16,**只修对了一半**:`▢▣▤◉◍` 这些几何符号在正文字体里的墨占比
    只有 em 的一半左右(advance 也只有半个 em),抬字号抬不动那个比例。

    所以不写死字号,**按量到的墨高反算**。这样换字体、换平台、换缩放都不用
    重新调常量 —— 而写死常量在另一台机器上就是另一个大小,还没人会发现。

    ``measure(ch, base, px) -> (墨高, 步进宽)`` 可以换掉。**这不是为了好看,
    是为了测得动**:offscreen 平台的字体库对每个字都返回"墨高 = em",于是
    放大分支永远走不到,几条断言全部空转(反向验证里活了三条才发现)。

    夹在 ``[target, 3×target]``:量不到时退回 target,不至于放成一个巨大方块。
    """
    key = (ch, int(target_ink), f"{base.family()}|{int(max_w)}")
    if measure is None:
        got = _GLYPH_PX.get(key)
        if got is not None:
            return got
        measure = _qt_measure
    target = int(target_ink)
    ink, _adv = measure(ch, base, target)
    if ink <= 0:
        out = target
    else:
        out = max(target, min(target * 3, round(target * target / ink)))
    # **放大之后还得放得下。** 列宽是硬约束:advance 超过可用宽度时
    # `elidedText` 会把整个符号换成一个省略号 —— 那比小一点糟得多。
    if max_w > 0:
        while out > target and measure(ch, base, out)[1] > max_w:
            out -= 1
    _GLYPH_PX[key] = out
    return out


def breakable(text: str) -> str:
    """在 ``_ - .`` 之后插零宽空格,让长串能换行。

    QLabel 的 ``setWordWrap`` **只在空格处断行**。ASIAIR 的文件名
    ``Light_IC 4603_300.0s_Bin1_4C_20260725-202016_276deg_0001.fit`` 只有一个
    空格,后半截 54 个字符是一个"词" —— 于是标签的最小宽度就是那 54 个字符,
    详情栏放不下,右边直接**截掉**(序号和角度正好在被截的那一段,而那是
    区分同一目标几十张 sub 的唯一信息)。老 UI 的 TextBlock 是按字符断的,
    断点恰好落在下划线上,这里用零宽空格复现同一处断法。

    代价:选中复制会带上零宽空格。文件路径请走「复制路径」按钮。
    """
    out = []
    for ch in str(text):
        out.append(ch)
        if ch in _BREAK_AFTER:
            out.append("​")
    return "".join(out)


def show_if(w: QWidget, cond) -> None:
    """按条件显示/隐藏。**绝不给无父控件调 `setVisible(True)`。**

    Qt 里**无父控件就是顶层窗口** —— 对它 `show()` 会让它带着标题栏和
    最小化/关闭按钮在屏幕上闪一帧,然后在被 `addWidget` 收进布局时消失。
    真机症状:某些操作会"迅速弹一堆在一个位置的小框框又秒级消失"
    (用户原话)。构造期就 `setVisible(bool(text))` 的件全中招 ——
    详情面板一次要建七八个徽章/胶囊,于是就闪成一片。

    加进布局之后控件本来就是可见的,压根不需要显式 show;真正需要的只有
    "隐藏",而 `setVisible(False)` 对无父控件是安全的。
    """
    if cond:
        if w.parent() is not None:
            w.setVisible(True)
    else:
        w.setVisible(False)


def wrap(layout) -> QWidget:
    """把一个布局套进一个 `QWidget` —— 这样才能整块 `show_if` 掉。

    布局本身没有可见性:`QVBoxLayout` 上没有 `setVisible`,想整块藏起来
    只能藏它的宿主控件。
    """
    host = QWidget()
    host.setLayout(layout)
    return host


def group_title(text: str) -> QLabel:
    """分组小标题(边栏那种全大写小字灰标题)。"""
    return label(text.upper(), role="group")


def pen(color, width: float = 1.0, *, dash=None) -> QPen:
    """给自绘控件用的画笔。

    **页面不许自己造 QPen/QColor** —— 自己造的颜色会绕过红光模式的映射
    (`test_page_does_not_construct_colors` 盯着这条)。要画线就来这里取。
    """
    p = QPen(QColor(color), float(width))
    if dash:
        p.setDashPattern([float(x) for x in dash])
    return p


class Divider(QFrame):
    def __init__(self, horizontal: bool = True, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Divider")
        if horizontal:
            self.setFixedHeight(1)
            self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        else:
            self.setFixedWidth(1)
            self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)


# ------------------------------------------------------------------ 胶囊

class StatusChip(QLabel):
    """卡右上角那种状态胶囊(``3/3`` / ``不可用`` / ``SMB 3.1.1``)。"""

    def __init__(self, text: str = "", tone: str | None = None,
                 parent: QWidget | None = None):
        super().__init__(text, parent)
        self.setObjectName("Chip")
        self.setAlignment(Qt.AlignCenter)
        # 不给 Fixed 的话它会跟着行高拉伸成一根竖条(真机截图上就是那样);
        # 空文本时直接隐藏,免得留一个没有内容的小圆角块。
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        show_if(self, text)
        self.set_tone(tone)

    def set_tone(self, tone: str | None) -> None:
        set_prop(self, "tone", theme.tone_name(tone))

    def set(self, text: str, tone: str | None = None) -> None:
        self.setText(text)
        self.set_tone(tone)
        show_if(self, text)


# ------------------------------------------------------------------ 按钮

def button(text: str, *, kind: str = "default",
           on_click: Callable[[], Any] | None = None,
           tip: str = "", enabled: bool = True) -> QPushButton:
    b = QPushButton(text)
    if kind != "default":
        b.setProperty("kind", kind)
    b.setCursor(Qt.PointingHandCursor)
    b.setEnabled(enabled)
    if tip:
        b.setToolTip(tip)
    if on_click is not None:
        b.clicked.connect(lambda *_: on_click())
    return b


def line_edit(placeholder: str = "", text: str = "",
              on_return: Callable[[], Any] | None = None) -> QLineEdit:
    e = QLineEdit(text)
    e.setPlaceholderText(placeholder)
    if on_return is not None:
        e.returnPressed.connect(lambda *_: on_return())
    return e


def combo(items: Iterable[str] = (), *, index: int = 0,
          on_change: Callable[[int], Any] | None = None) -> QComboBox:
    cb = QComboBox()
    cb.addItems(list(items))
    if 0 <= index < cb.count():
        cb.setCurrentIndex(index)
    if on_change is not None:
        cb.currentIndexChanged.connect(lambda i: on_change(int(i)))
    return cb


#: 下拉箭头 + 内边距,量文字之外还得留出来
COMBO_CHROME = 34


def _iter_points(points) -> list:
    """把"一堆点"安全地摊成可迭代 —— **不对它做真值判断**。

    ``None`` 与空都给空列表;numpy 数组、列表、元组一视同仁。
    这个函数存在的唯一理由是 `points or ()` 会把 numpy 数组炸掉,
    而那正是真实调用方给的类型。
    """
    if points is None:
        return []
    try:
        return list(points)
    except TypeError:
        return []


def fit_combo(cb: QComboBox, items: Iterable[str] = ()) -> None:
    """按**真实项文字**定下拉的宽度下限。

    QComboBox 默认 ``AdjustToContentsOnFirstShow``:项是构造之后才
    ``addItems()`` 填进去的,尺寸不会跟着重算 —— 于是收起状态把内容截掉。
    夜次项形如 "2026-07-29 · 2 目标 · 59 帧",截掉的正好是帧数,
    而帧数就是加它的理由。

    **两种写死都是错的**:150px 不够;改成写死 210px 在 9pt 下要 288px 才够
    (全量测试里为此挂过一次)。字宽随字体与 DPI 变,只能拿当前字体去量。

    这个函数在共享层而不是某一页里,是因为它已经是**第二**个页面踩同一个坑了
    (3D 天球先踩,拍摄记录同款下拉照样被截)。
    """
    cb.setSizeAdjustPolicy(QComboBox.AdjustToContents)
    texts = [str(s) for s in items]
    if not texts:
        texts = [cb.itemText(i) for i in range(cb.count())]
    fm = cb.fontMetrics()
    need = max((fm.horizontalAdvance(s) for s in texts), default=0)
    cb.setMinimumWidth(need + COMBO_CHROME)


def check(text: str, *, on: bool = False,
          on_change: Callable[[bool], Any] | None = None) -> QCheckBox:
    cb = QCheckBox(text)
    cb.setChecked(on)
    if on_change is not None:
        cb.toggled.connect(lambda v: on_change(bool(v)))
    return cb


# ------------------------------------------------------------------ 卡片

class SectionTitle(QWidget):
    """标题 + 下面一行小字副标题,标题左侧一道 3px 强调色竖条。"""

    def __init__(self, title: str, subtitle: str = "",
                 parent: QWidget | None = None):
        super().__init__(parent)
        row = hbox(self, gap="sm")
        self._bar = QFrame()
        self._bar.setObjectName("CardHeaderBar")
        self._bar.setFixedWidth(theme.ACCENT_BAR_W)
        self._bar.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Expanding)
        row.addWidget(self._bar)
        col = vbox(gap="none")
        self._title = label(title, role="title")
        self._sub = label(subtitle, role="subtitle", wrap=True)
        show_if(self._sub, subtitle)
        col.addWidget(self._title)
        col.addWidget(self._sub)
        # **文字列吃掉剩余宽度**,不是末尾加个 stretch。加 stretch 的话
        # 带换行的副标题只拿到自己的最小宽度,一句短话会被折成两行、
        # 而且断在词中间(真机截图上"选中一个文件查看拍/摄参数")。
        row.addLayout(col, 1)

    def set_title(self, text: str) -> None:
        self._title.setText(text)

    def set_subtitle(self, text: str) -> None:
        self._sub.setText(text)
        show_if(self._sub, text)


class Card(QFrame):
    """卡片 —— 界面的基本单位。

    卡头 = 标题 + 副标题 + 右上角状态胶囊。内容加到 :attr:`body`。
    """

    def __init__(self, title: str = "", subtitle: str = "", *,
                 chip: str = "", chip_tone: str | None = None,
                 flat: bool = False, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Card")
        if flat:
            self.setProperty("flat", "true")
        outer = vbox(self, gap="card", pad="card")
        self._head = None
        self.chip = StatusChip(chip, chip_tone)
        show_if(self.chip, chip)
        if title:
            head = hbox(gap="sm")
            self._head = SectionTitle(title, subtitle)
            head.addWidget(self._head, 1)
            head.addWidget(self.chip, 0, Qt.AlignTop | Qt.AlignRight)
            outer.addLayout(head)
        self.body = vbox(gap="sm")
        outer.addLayout(self.body, 1)

    # -- 便捷 ---------------------------------------------------------
    def add(self, w: QWidget, stretch: int = 0) -> QWidget:
        self.body.addWidget(w, stretch)
        return w

    def add_layout(self, lay, stretch: int = 0):
        self.body.addLayout(lay, stretch)
        return lay

    def add_stretch(self, n: int = 1) -> None:
        self.body.addStretch(n)

    def set_title(self, text: str) -> None:
        if self._head is not None:
            self._head.set_title(text)

    def set_subtitle(self, text: str) -> None:
        if self._head is not None:
            self._head.set_subtitle(text)

    def set_chip(self, text: str, tone: str | None = None) -> None:
        self.chip.set(text, tone)

    def clear_body(self) -> None:
        _clear_layout(self.body)


def _clear_layout(lay) -> None:
    while lay.count():
        item = lay.takeAt(0)
        w = item.widget()
        if w is not None:
            w.setParent(None)
            w.deleteLater()
        elif item.layout() is not None:
            _clear_layout(item.layout())


class Dialog(QDialog):
    """模态层。

    大图/长内容用它而不是塞进页面 —— 页内覆盖层在 Qt 上要自己处理 z 序、
    焦点与 Esc,而这些 QDialog 都是现成的。破坏性操作的确认走
    ``QMessageBox``(真模态,用户没法无视它去点别的)。
    """

    def __init__(self, parent: QWidget | None = None, title: str = ""):
        super().__init__(parent)
        self.setObjectName("Root")
        if title:
            self.setWindowTitle(title)
        self.setModal(True)


class Inset(QFrame):
    """卡内再嵌一层的浅底块(详情分组、代码块那种)。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Inset")
        self.body = vbox(self, gap="xs", pad="sm")


# ------------------------------------------------------------------ 指标

class MetricRow(QWidget):
    """一行键值:左标签(灰)右值。详情面板里成片地用。"""

    def __init__(self, key: str, value: str = "", *, tone: str | None = None,
                 note: str = "", mono: bool = False,
                 parent: QWidget | None = None):
        super().__init__(parent)
        row = hbox(self, gap="sm")
        self._k = label(key, role="subtitle")
        self._k.setMinimumWidth(84)
        self._v = label(value, role="mono" if mono else "body", tone=tone,
                        wrap=True)
        self._n = label(note, role="faint")
        show_if(self._n, note)
        row.addWidget(self._k, 0, Qt.AlignTop)
        row.addWidget(self._v, 1)
        row.addWidget(self._n, 0, Qt.AlignTop)

    def set_value(self, text: str, tone: str | None = None) -> None:
        self._v.setText(text)
        set_prop(self._v, "tone", theme.tone_name(tone))


class TextDialog(QDialog):
    """一段可滚动、可全选复制的等宽长文本(完整 FITS 头就是它)。

    **不做成详情面板里的一段**:一张 light 帧有几十张卡片,摊在面板里会把
    上面那几组判读整个顶出可视区(老 UI 同款,那边是折叠的)。
    """

    def __init__(self, parent, title: str, text: str):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(720, 560)
        col = vbox(self, gap="sm", pad="card")
        scroll = Scroll(gap="none")
        lb = label(text, role="mono", wrap=False)
        lb.setTextInteractionFlags(Qt.TextSelectableByMouse
                                   | Qt.TextSelectableByKeyboard)
        scroll.body.addWidget(lb)
        scroll.body.addStretch(1)
        col.addWidget(scroll, 1)
        row = hbox(gap="sm")
        row.addStretch(1)
        row.addWidget(button(_("关闭"), on_click=self.accept))
        col.addLayout(row)


class GroupHeader(QWidget):
    """分组标题:图标 + 名字 + 右侧一道横线。

    老 UI 的详情面板就是这么分组的(★ 目标 / ◔ 光学 / ▣ 相机 …),那道横线
    很重要 —— 没有它,四组几十行键值就是一片没有边界的文字,眼睛找不到落点。
    Qt 这边原来只有一行小灰字。

    **图标不用 Segoe MDL2 的私用区码位。** 共享层给的 glyph 是
    ``\\ue735`` 那种,Windows 上有字体、macOS/Linux 上是一个方框。
    这里把它换成 BMP 字符(:data:`SEGOE_GLYPHS`),三个平台都画得出来。

    **按 glyph 换,不按组名查。** 原来是 ``GROUP_GLYPHS[组名]`` —— 而组名是
    共享层给的**显示文本**,会被翻译;一翻,每一组的图标都退回一个点,
    不报错、也不违反任何契约。glyph 是私用区码位,与语言无关。
    页面自己写死的标题(「影像结构」这类)直接传 BMP 字符,原样用。
    """

    def __init__(self, name: str, glyph: str = "",
                 parent: QWidget | None = None):
        super().__init__(parent)
        row = hbox(self, gap="sm")
        glyph = SEGOE_GLYPHS.get(glyph, glyph or "·")
        self._g = label(glyph, role="group")
        self._t = label(name, role="group")
        row.addWidget(self._g)
        row.addWidget(self._t)
        row.addWidget(Divider(True), 1)


#: Segoe MDL2 私用区码位 → BMP 图标。共享层给的是前者,跨平台画不出来。
#:
#: **键是码位不是组名。** 组名会被翻译,码位不会 —— 见 `GroupHeader` 的说明。
#: 多个组共用一个码位是正常的(`_GRP_PLACE` 与 `_GRP_NET` 都是 Globe),
#: 那本来就是"同一个图标"的意思。
SEGOE_GLYPHS = {
    "": "★",   # FavoriteStarFill —— 目标 / ZWO 特征
    "": "◎",   # View —— 光学
    "": "▣",   # Camera —— 相机
    "": "◷",   # Calendar —— 时间与位置 / 记录
    "": "▤",   # Document —— 文件
    "": "▸",   # Globe —— 位置 / 网络
    "": "▥",   # 磁盘
    "": "▩",   # 容量
}

#: 页面自己写死的组标题用的 BMP 图标(共享层不产出这几组)
GLYPH_SKY = "◍"        # 全天位置
GLYPH_STRUCTURE = "▦"  # 影像结构
GLYPH_SOLVE = "✧"      # 板解算
GLYPH_STATS = "▤"      # 段统计


class TimelineRow(QWidget):
    """事件时间线的一行:``[时刻列 | 轨道列(标记+连接竖线) | 卡片列]``。

    老 UI 的详情面板就是这个形状。Qt 这边原来把 时刻/标题/副标题 用 ``·``
    串成**一行字符串**扔进一个普通 label —— 于是三样东西一起没了:

    * ``level`` 的状态色(完成绿 / 暂停琥珀 / 截断红),看不出哪一步出了事;
    * ``kind``:目标块边界在老 UI 是**方旗**、其余是圆点,分不出层级;
    * ``progress``:Shooting 组的"实拍/计划"迷你进度条,那是"这组拍够了没有"
      的唯一直观读法。

    连接竖线首行不画上半、末行不画下半 —— 否则时间线两端各挂一截断头。
    """

    #: 时刻列宽。等宽字体下 ``23:59:59`` 正好放得下,再窄就换行。
    T_COL = 62
    #: 轨道列宽(标记 9px + 两边留白)
    RAIL_COL = 14
    MARK = 9

    def __init__(self, item: dict, *, first: bool = False, last: bool = False,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._level = str(item.get("level") or "info")
        self._kind = str(item.get("kind") or "info")
        self._first, self._last = bool(first), bool(last)

        row = hbox(self, gap="sm")
        when = str(item.get("when") or "")
        when2 = str(item.get("when2") or "")
        t = label(f"{when}\n{when2}" if when2 else when, role="mono")
        t.setFixedWidth(self.T_COL)
        t.setAlignment(Qt.AlignRight | Qt.AlignTop)
        row.addWidget(t)

        # 轨道列是**自绘**的:标记要跟着卡片顶部对齐,而竖线要贯穿整行高度 ——
        # 用控件拼的话行高一变就对不齐。
        self._rail = _RailStrip(self)
        self._rail.setFixedWidth(self.RAIL_COL)
        row.addWidget(self._rail)

        card = QWidget()
        card.setObjectName("TimelineCard")
        col = vbox(card, gap="xs", pad="sm")
        col.addWidget(label(str(item.get("title") or ""), role="strong",
                            wrap=True))
        if item.get("subtitle"):
            col.addWidget(label(str(item["subtitle"]), role="subtitle",
                                wrap=True))
        prog = item.get("progress")
        if prog:
            actual, planned = prog
            frac = (min(1.0, actual / planned) if planned else 0.0)
            bar = hbox(gap="sm")
            # 拍够了绿、没拍够琥珀 —— 与老 UI 同一条判读
            g = Gauge(frac, tone="ok" if actual >= planned else "warn")
            g.setFixedWidth(120)
            bar.addWidget(g)
            bar.addWidget(label(f"{actual}/{planned}", role="faint"))
            bar.addStretch(1)
            col.addLayout(bar)
        row.addWidget(card, 1)

    def rail_geometry(self) -> tuple[str, str, bool, bool]:
        """给 :class:`_RailStrip` 用的四元组,顺带方便测试读取。"""
        return self._level, self._kind, self._first, self._last


class _RailStrip(QWidget):
    """时间线左侧那条轨道:上连接线 + 状态色标记 + 下连接线。"""

    def paintEvent(self, ev) -> None:  # noqa: N802
        row = self.parent()
        if not isinstance(row, TimelineRow):
            return
        level, kind, first, last = row.rail_geometry()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        cx = self.width() / 2.0
        top = 12.0                      # 标记中心离顶部的距离,与卡片首行齐平
        m = float(TimelineRow.MARK)
        p.setPen(pen(theme.Q.BORDER, 2.0))
        if not first:
            p.drawLine(QPointF(cx, 0.0), QPointF(cx, top - m / 2.0))
        if not last:
            p.drawLine(QPointF(cx, top + m / 2.0),
                       QPointF(cx, float(self.height())))
        col = QColor(theme.tone_color(level) or theme.Q.TEXT_DIM)
        box = QRectF(cx - m / 2.0, top - m / 2.0, m, m)
        path = QPainterPath()
        if kind == "block":
            path.addRoundedRect(box, 2.0, 2.0)   # 块边界 = 方旗
        else:
            path.addEllipse(box)                 # 其余 = 圆点
        p.fillPath(path, col)


class TimelineGap(QWidget):
    """两条事件之间的"空了 N 分钟"分隔 —— 居中小字,没有卡片。

    间隙本身就是这一页要回答的问题之一(损失了多少时间),画成普通条目会
    让它看起来像"又发生了一件事"。
    """

    def __init__(self, text: str, parent: QWidget | None = None):
        super().__init__(parent)
        row = hbox(self, gap="sm")
        row.addStretch(1)
        row.addWidget(label(f"—  {text}  —", role="faint"))
        row.addStretch(1)


#: 高度角量尺的刻度:(高度角, 图标, 说明)。
#:
#: **刻度落在语义阈值上,不是均分。** 20° 与 40° 正是
#: :func:`astro_smb_app.views.browser._alt_tone` 换色的两个界:填充停在雾号
#: 之前就是"低空",越过星号才是"通透"。均分成 30/60 好看,但那样刻度
#: 只是尺子,解释不了颜色为什么变。
ALT_TICKS: tuple[tuple[float, str, str], ...] = (
    (0.0, "horizon", N_("地平线")),
    (20.0, "haze", N_("20° 以下:大气量大,低空")),
    (40.0, "star", N_("40° 以上:通透")),
    (90.0, "zenith", N_("天顶")),
)


def alt_icon(p: QPainter, kind: str, box: QRectF, color: QColor) -> None:
    """在 ``box`` 里画一个天文线稿小图标。

    **线稿而不是字符。** 这里不能用 emoji(那是星平面字符,本仓库另有一处
    坑),而 `☀`/`★` 这类 BMP 符号在 10px 上糊成一团黑;自己画线条才在
    小尺寸下立得住,也才跟得上三档配色。
    """
    pen = QPen(color)
    pen.setWidthF(1.2)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    x, y, w, h = box.x(), box.y(), box.width(), box.height()
    cx, cy = x + w / 2.0, y + h / 2.0
    if kind == "horizon":
        # 地平线:一条基线 + 压在线上的半轮(天体正在升起/落下)
        p.drawLine(QPointF(x, y + h - 1), QPointF(x + w, y + h - 1))
        p.drawArc(QRectF(cx - w * 0.3, y + h - 1 - w * 0.3, w * 0.6, w * 0.6),
                  0, 180 * 16)
    elif kind == "haze":
        # 大气/低空:三条横纹**越往下越长** —— 贴着地平线越厚的那层浑浊。
        # 三条等长会读成菜单图标,长度必须单调变化才像"越近地平越浑"。
        for i, frac in enumerate((0.45, 0.7, 0.95)):
            yy = y + h * (0.3 + 0.24 * i)
            p.drawLine(QPointF(cx - w * frac / 2.0, yy),
                       QPointF(cx + w * frac / 2.0, yy))
    elif kind == "star":
        # 通透:四芒星(两条交叉线 + 中心点),小尺寸下比五角星干净
        p.drawLine(QPointF(cx, y + h * 0.1), QPointF(cx, y + h * 0.9))
        p.drawLine(QPointF(x + w * 0.1, cy), QPointF(x + w * 0.9, cy))
        p.drawLine(QPointF(cx - w * 0.22, cy - h * 0.22),
                   QPointF(cx + w * 0.22, cy + h * 0.22))
        p.drawLine(QPointF(cx - w * 0.22, cy + h * 0.22),
                   QPointF(cx + w * 0.22, cy - h * 0.22))
    elif kind == "zenith":
        # 天顶:圆圈 + 圆心点 + 头顶一道竖(正上方)。竖线要够长才读得出
        # "从头顶直下"这层意思 —— 太短就只剩一个准星。
        p.drawEllipse(QRectF(cx - w * 0.26, cy - w * 0.26, w * 0.52, w * 0.52))
        p.drawLine(QPointF(cx, y), QPointF(cx, cy - w * 0.34))
        p.setBrush(color)
        p.drawEllipse(QRectF(cx - 1.1, cy - 1.1, 2.2, 2.2))
        p.setBrush(Qt.NoBrush)


class Gauge(QWidget):
    """一根 0–1 的横条 + 刻度标注。高度角那条就是它。

    老 UI 里高度角是"数字 + 一条量条",量条让"35° 到底算高还是低"一眼可判;
    Qt 这边只把数字搬过来了,共享层元组里那个 ``('altbar', 35.4)`` 被丢掉。

    两种标注二选一:``lo``/``hi`` 是两端的文字(卷容量那条用),``ticks``
    是刻度 + 线稿图标(高度角那条用,见 :data:`ALT_TICKS`)。
    """

    H = 6
    #: 图标格子边长
    ICON = 11

    def __init__(self, frac: float, *, tone: str | None = None,
                 lo: str = "", hi: str = "",
                 ticks: Sequence[tuple[float, str, str]] | None = None,
                 span: float = 1.0, parent: QWidget | None = None):
        super().__init__(parent)
        self._frac = max(0.0, min(1.0, float(frac)))
        self._tone = tone
        self._ticks = tuple(ticks or ())
        self._span = float(span) or 1.0
        foot = (self.ICON + 4) if self._ticks else (12 if (lo or hi) else 0)
        self.setFixedHeight(self.H + foot)
        self.setMinimumWidth(90)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._lo, self._hi = lo, hi

    def set_frac(self, frac: float, *, tone: str | None = None) -> None:
        """就地改值。**别重建控件** —— 卷容量那条每换一次共享就要更新,
        重建会让它在布局里跳一下。"""
        self._frac = max(0.0, min(1.0, float(frac)))
        self._tone = tone
        self.update()

    def paintEvent(self, ev) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        track = QRectF(0, 0, w, self.H)
        path = QPainterPath()
        path.addRoundedRect(track, self.H / 2.0, self.H / 2.0)
        p.fillPath(path, QColor(theme.Q.SURFACE_HI))
        if self._frac > 0:
            fill = QPainterPath()
            fill.addRoundedRect(QRectF(0, 0, max(self.H, w * self._frac),
                                       self.H),
                                self.H / 2.0, self.H / 2.0)
            p.fillPath(fill, theme.tone_color(self._tone))
        if self._ticks:
            self._paint_ticks(p, w)
        elif self._lo or self._hi:
            f = QFont(self.font())
            f.setPixelSize(theme.Font.TINY)
            p.setFont(f)
            p.setPen(QColor(theme.Q.TEXT_FAINT))
            r = QRect(0, self.H + 1, w, 12)
            p.drawText(r, Qt.AlignLeft | Qt.AlignVCenter, self._lo)
            p.drawText(r, Qt.AlignRight | Qt.AlignVCenter, self._hi)
        p.end()

    def _paint_ticks(self, p: QPainter, w: float) -> None:
        """刻度线 + 图标。

        **已越过的刻度用语义色,未到的留淡色** —— 这样"填充停在雾号之前"
        本身就是结论,不用再去读数字。
        """
        ic = float(self.ICON)
        live = theme.tone_color(self._tone)
        faint = QColor(theme.Q.TEXT_FAINT)
        for val, kind, _tip in self._ticks:
            f = max(0.0, min(1.0, float(val) / self._span))
            # **刻度线必须落在填充的同一把尺子上**(填充是 f→w·f)。
            # 先前把刻度整体内缩了 ICON/2,于是 40° 的线和"填充刚好到 40°"
            # 差半个图标 —— 看着就是量条与刻度对不上。内缩只许作用在
            # 图标格子上,免得两端半个图标画到控件外面。
            cx = f * w
            bx = min(max(0.0, cx - ic / 2.0), max(0.0, w - ic))
            col = live if self._frac + 1e-9 >= f else faint
            # **两端不画刻度线。** 0° 和 90° 的线只能落在控件最边上,而那里
            # 放不下一个居中的图标(图标必须内缩才不被裁),于是线和图标
            # 差半格,看着就是没对齐。条子的两端本来就是 0° 和 90°,
            # 再画一道线也不多给任何信息。
            if 0.0 < f < 1.0:
                pen = QPen(faint)
                pen.setWidthF(1.0)
                p.setPen(pen)
                p.drawLine(QPointF(cx, self.H + 1), QPointF(cx, self.H + 3))
            alt_icon(p, kind, QRectF(bx, self.H + 4, ic, ic), col)

    def tick_tooltip(self) -> str:
        return " · ".join(_(t[2]) for t in self._ticks)


class MetricTile(QWidget):
    """大数字 + 下面一行小标签。传输页那四个统计就是这个。"""

    def __init__(self, value: str, caption: str, *, accent: bool = False,
                 parent: QWidget | None = None):
        super().__init__(parent)
        col = vbox(self, gap="none")
        self._v = label(value, role="metric_accent" if accent else "metric")
        self._c = label(caption, role="subtitle")
        col.addWidget(self._v)
        col.addWidget(self._c)

    def set_value(self, text: str) -> None:
        self._v.setText(text)


# ------------------------------------------------------------------ 忙态 / 空态

class Spinner(QWidget):
    """转圈。Qt 没有内置的,自己画一段弧。"""

    def __init__(self, size: int = 18, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._angle = 0
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    def _tick(self) -> None:
        self._angle = (self._angle + 30) % 360
        self.update()

    def showEvent(self, ev):  # noqa: N802 - Qt 命名
        super().showEvent(ev)
        self._timer.start(90)

    def hideEvent(self, ev):  # noqa: N802
        super().hideEvent(ev)
        self._timer.stop()

    def paintEvent(self, ev):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect().adjusted(2, 2, -2, -2)
        pen = QPen(theme.Q.ACCENT, 2.0)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        p.drawArc(r, -self._angle * 16, 110 * 16)
        p.end()


class BusyState(QWidget):
    """忙态。**每一页都要有** —— 没有它,慢操作期间界面看着像坏了。"""

    def __init__(self, text: str | None = None,
                 parent: QWidget | None = None):
        # 默认值不能直接 `_()` —— 那是 import 时求值,翻译会被冻住
        text = _("正在读取 …") if text is None else text
        super().__init__(parent)
        row = hbox(self, gap="sm", pad="card")
        row.addStretch(1)
        self._sp = Spinner()
        self._lb = label(text, role="dim")
        row.addWidget(self._sp)
        row.addWidget(self._lb)
        row.addStretch(1)

    def set_text(self, text: str) -> None:
        self._lb.setText(text)


class EmptyState(QWidget):
    """空态。

    **文案必须说清是"没有"还是"还没读"** —— 两者混在一起会让人以为设备上
    真的没数据(这是 ui-protocol.md 里点名的那条)。所以构造函数强制要
    ``note``:那一句就是用来说明"为什么空"和"接下来做什么"的。
    """

    def __init__(self, title: str, note: str, *, action: str = "",
                 on_action: Callable[[], Any] | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        col = vbox(self, gap="sm", pad="card")
        col.addStretch(1)
        t = label(title, role="title")
        t.setAlignment(Qt.AlignCenter)
        n = label(note, role="subtitle", wrap=True)
        n.setAlignment(Qt.AlignCenter)
        col.addWidget(t)
        col.addWidget(n)
        if action and on_action is not None:
            row = hbox(gap="sm")
            row.addStretch(1)
            row.addWidget(button(action, kind="primary", on_click=on_action))
            row.addStretch(1)
            col.addLayout(row)
        col.addStretch(1)


class StateStack(QWidget):
    """忙态 / 空态 / 内容 三选一的容器。

    每一页都要这三态,写三遍必然有一页忘了其中一种(真机上表现为
    "转了一下就变白板")。
    """

    def __init__(self, content: QWidget, parent: QWidget | None = None):
        super().__init__(parent)
        self._lay = vbox(self, gap="none")
        self._content = content
        self._busy = BusyState()
        self._empty: QWidget | None = None
        self._lay.addWidget(self._busy)
        self._lay.addWidget(self._content)
        self._busy.setVisible(False)

    def show_busy(self, text: str | None = None) -> None:
        self._busy.set_text(_("正在读取 …") if text is None else text)
        show_if(self._busy, True)
        self._content.setVisible(False)
        if self._empty is not None:
            self._empty.setVisible(False)

    def show_empty(self, title: str, note: str, *, action: str = "",
                   on_action: Callable[[], Any] | None = None) -> None:
        if self._empty is not None:
            self._lay.removeWidget(self._empty)
            self._empty.deleteLater()
        self._empty = EmptyState(title, note, action=action,
                                 on_action=on_action)
        self._lay.addWidget(self._empty)
        self._busy.setVisible(False)
        self._content.setVisible(False)

    def show_content(self) -> None:
        self._busy.setVisible(False)
        if self._empty is not None:
            self._empty.setVisible(False)
        show_if(self._content, True)


# ------------------------------------------------------------------ 滚动区

class Scroll(QScrollArea):
    """竖向滚动区。``.body`` 是里面那个 VBox。

    **横向必须关掉** —— 允许横滚等于用无限宽度量内容,里面的伸缩列会塌成
    0 像素(ui-protocol.md §3.2 那条,换个框架照样成立)。
    """

    def __init__(self, *, gap: str | int = "card", pad: str | int = "none",
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.NoFrame)
        inner = QWidget()
        inner.setObjectName("ScrollBody")
        self.body = vbox(inner, gap=gap, pad=pad)
        self.setWidget(inner)
        self._inner = inner

    def clear(self) -> None:
        _clear_layout(self.body)


# ------------------------------------------------------------------ 画布

class Canvas(QWidget):
    """自绘控件基类。子类覆写 :meth:`paint`。

    图表/天球/treemap 全走这里 —— 一屏几百个图元不该是几百个控件。
    颜色一律从 ``theme.Q`` 取,所以红光模式切过去自动跟着变(只要
    ``update()`` 一下)。
    """

    clicked = Signal(float, float)
    #: 尺寸变了。**几何按实际宽度算的图必须接它** —— 显示列表是一次性算好的
    #: 像素坐标,窗口一拉宽,图还停在原来的宽度上(右边空一块)。
    resized = Signal()

    def __init__(self, width: int = 0, height: int = 0,
                 parent: QWidget | None = None):
        super().__init__(parent)
        if width:
            self.setMinimumWidth(width)
        if height:
            self.setFixedHeight(height)
        self.setAttribute(Qt.WA_OpaquePaintEvent, False)

    def resizeEvent(self, ev):  # noqa: N802
        super().resizeEvent(ev)
        self.resized.emit()

    def paint(self, p: QPainter, w: float, h: float) -> None:  # pragma: no cover
        raise NotImplementedError

    def paintEvent(self, ev):  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        try:
            self.paint(p, float(self.width()), float(self.height()))
        finally:
            p.end()

    def mousePressEvent(self, ev):  # noqa: N802
        pos = ev.position()
        self.clicked.emit(float(pos.x()), float(pos.y()))
        super().mousePressEvent(ev)

    # -- 画图常用 ------------------------------------------------------
    def fill_bg(self, p: QPainter, w: float, h: float,
                radius: float = float(theme.Radius.CTL)) -> None:
        path = QPainterPath()
        path.addRoundedRect(QRectF(0, 0, w, h), radius, radius)
        p.fillPath(path, theme.Q.CHART_BG)

    def text_at(self, p: QPainter, x: float, y: float, text: str, *,
                color: QColor | None = None, size: int = theme.Font.TINY,
                bold: bool = False, align_right: bool = False,
                maxw: float = 0.0) -> None:
        """``maxw`` = 可用宽度,超了截成省略号。

        甘特条上的目标名必须**按条宽截断**:老 UI 是 `NGC 72...`,而这边
        整个 `NGC 7293` 顶出条外、和后一条的标签挤在一起(看起来像两条重叠)。
        """
        f = QFont(p.font())
        f.setPixelSize(size)
        f.setBold(bold)
        p.setFont(f)
        p.setPen(color or theme.Q.TEXT_DIM)
        fm = QFontMetrics(f)
        # **换行符要真的换行。** `drawText(QPointF, str)` 不处理它 ——
        # treemap 的叶级标签是「名字 + 换行 + 大小」两行,于是渲染成
        # `Bias1.46 GB`、`…_0022.fit49.77 MB` 这种连在一起的怪串。
        # 老 UI 在 XAML 里写的是 `&#10;`,是真的两行。
        for i, line in enumerate(str(text).splitlines() or [""]):
            if maxw > 0:
                line = fm.elidedText(line, Qt.ElideRight, int(maxw))
            lx = x - fm.horizontalAdvance(line) if align_right else x
            p.drawText(QPointF(lx, y + fm.ascent() + i * fm.height()), line)


class OpsCanvas(Canvas):
    """渲染**显示列表**(``views.skychart`` / ``views.space`` 那种 op 字典)。

    这是复用共享视图层的关键一件:天球投影、treemap 布局、甘特几何在
    ``astro_smb_app.views`` 里已经算好了,这边只负责画。**一份投影三处消费**
    (老 UI / Uno 前端 / 这里),改公式只改那一处。

    ``hits`` 是一张独立的矩形表 ``[x1, y1, x2, y2, key]``,点击时 Python 反查 ——
    900 个图元挂 900 个事件在任何框架下都不是好主意。
    """

    hit = Signal(str)
    #: 双击命中区(treemap 下钻)
    hit_activated = Signal(str)
    #: 指针进入/离开某个命中区(空串 = 离开)。**只在换了区时发** ——
    #: 每个鼠标移动事件都发一次会让上层每帧重画一整张 treemap。
    hit_hovered = Signal(str)

    def __init__(self, width: int = 0, height: int = 0,
                 parent: QWidget | None = None):
        super().__init__(width, height, parent)
        self._ops: list[dict] = []
        self._hits: list[list] = []
        self._hover = ""
        self._bg = None
        self._bg_rect = None
        self.clicked.connect(self._on_click)
        self.setMouseTracking(True)

    def set_ops(self, ops: Iterable[dict], hits: Iterable[list] = ()) -> None:
        self._ops = list(ops or ())
        self._hits = [list(h) for h in (hits or ())]
        self.update()

    def set_background(self, path: str = "",
                       rect: tuple[float, float, float, float] | None = None
                       ) -> None:
        """画在**所有图元之下**的一张位图(天球的巡天底图)。

        `rect` 是它该占的矩形。天球那张必须给 `(cx-r, cy-r, 2r, 2r)` ——
        底图是一张"盘外透明"的圆盘,直径就是地平圈直径;拉伸到整个画布的话
        星点会与圈错位,而错位在一张星图上几乎看不出来(真机踩过"M 8 不在
        银心",那次是时刻错了,同一类症状)。
        """
        self._bg = QPixmap(path) if path else None
        self._bg_rect = rect
        self.update()

    def key_at(self, x: float, y: float) -> str:
        """命中反查。**后画的在上面,所以倒着找** —— 嵌套 treemap 里子块
        画在父块之上,正着找永远命中最外层那个大块。"""
        for x1, y1, x2, y2, key in reversed(self._hits):
            if x1 <= x <= x2 and y1 <= y <= y2:
                return str(key)
        return ""

    def _on_click(self, x: float, y: float) -> None:
        key = self.key_at(x, y)
        if key:
            self.hit.emit(key)

    def mouseDoubleClickEvent(self, ev):  # noqa: N802
        pos = ev.position()
        key = self.key_at(float(pos.x()), float(pos.y()))
        if key:
            self.hit_activated.emit(key)
        super().mouseDoubleClickEvent(ev)

    def mouseMoveEvent(self, ev):  # noqa: N802
        pos = ev.position()
        key = self.key_at(float(pos.x()), float(pos.y()))
        if key != self._hover:
            self._hover = key
            self.hit_hovered.emit(key)
        super().mouseMoveEvent(ev)

    def leaveEvent(self, ev):  # noqa: N802
        if self._hover:
            self._hover = ""
            self.hit_hovered.emit("")
        super().leaveEvent(ev)

    def paint(self, p: QPainter, w: float, h: float) -> None:
        if self._bg is not None and not self._bg.isNull():
            x, y, bw, bh = self._bg_rect or (0.0, 0.0, w, h)
            p.drawPixmap(QRectF(x, y, bw, bh), self._bg,
                         QRectF(0, 0, self._bg.width(), self._bg.height()))
        for op in self._ops:
            kind = op.get("op")
            opacity = float(op.get("opacity", 1.0))
            p.setOpacity(opacity)
            fill = op.get("fill")
            stroke = op.get("stroke")
            pen = (QPen(theme.screen_color(stroke), float(op.get("width", 1.0)))
                   if stroke else Qt.NoPen)
            if kind == "rect":
                rect = QRectF(float(op["x"]), float(op["y"]),
                              float(op["w"]), float(op["h"]))
                radius = float(op.get("radius", 0.0))
                path = QPainterPath()
                if radius:
                    path.addRoundedRect(rect, radius, radius)
                else:
                    path.addRect(rect)
                if fill:
                    p.fillPath(path, theme.screen_color(fill))
                if stroke:
                    p.setPen(pen)
                    p.drawPath(path)
            elif kind == "ellipse":
                rect = QRectF(float(op["x"]) - float(op["rx"]),
                              float(op["y"]) - float(op["ry"]),
                              float(op["rx"]) * 2, float(op["ry"]) * 2)
                p.setPen(pen)
                p.setBrush(theme.screen_color(fill) if fill else Qt.NoBrush)
                p.drawEllipse(rect)
                p.setBrush(Qt.NoBrush)
            elif kind == "line":
                lp = pen if stroke else QPen(theme.Q.CHART_AXIS, 1.0)
                dash = op.get("dash")
                if dash:
                    # 虚线是**语义**不是装饰:天球上实线=地平圈、虚线=高度圈与
                    # 十字方位线。忽略 dash 会让三种线看起来一样。
                    lp.setDashPattern([float(x) for x in dash])
                p.setPen(lp)
                p.drawLine(QPointF(float(op["x1"]), float(op["y1"])),
                           QPointF(float(op["x2"]), float(op["y2"])))
            elif kind == "text":
                self.text_at(p, float(op["x"]), float(op["y"]),
                             str(op.get("text", "")),
                             color=theme.screen_color(fill) if fill
                             else theme.Q.TEXT_DIM,
                             size=int(float(op.get("size", theme.Font.TINY))),
                             bold=op.get("weight") == "semibold",
                             maxw=float(op.get("maxw", 0.0)))
            elif kind == "poly":
                pts = list(op.get("points") or ())
                if len(pts) < 4:
                    continue
                path = QPainterPath(QPointF(pts[0], pts[1]))
                for i in range(2, len(pts) - 1, 2):
                    path.lineTo(QPointF(pts[i], pts[i + 1]))
                if op.get("closed"):
                    path.closeSubpath()
                if fill:
                    p.fillPath(path, theme.screen_color(fill))
                if stroke:
                    p.setPen(pen)
                    p.drawPath(path)
        p.setOpacity(1.0)


class UsageBar(Canvas):
    """占用条。两个圆角矩形,颜色按阈值取语义色。"""

    def __init__(self, percent: float = 0.0, width: int = 140,
                 parent: QWidget | None = None):
        super().__init__(width, 6, parent)
        self.setMinimumWidth(width)
        self._pct = percent

    def set_percent(self, pct: float) -> None:
        self._pct = max(0.0, min(100.0, float(pct)))
        self.update()

    def paint(self, p: QPainter, w: float, h: float) -> None:
        r = h / 2
        track = QPainterPath()
        track.addRoundedRect(QRectF(0, 0, w, h), r, r)
        p.fillPath(track, theme.alpha(theme.Q.TEXT_FAINT, 0.30))
        pct = max(0.0, min(100.0, self._pct))
        col = (theme.Q.OK if pct < 80 else
               theme.Q.WARN if pct < 92 else theme.Q.BAD)
        fill = QPainterPath()
        fill.addRoundedRect(QRectF(0, 0, max(2.0, w * pct / 100.0), h), r, r)
        p.fillPath(fill, col)


class MultiHistogram(Canvas):
    """多通道**半透明叠画**的直方图。

    合成单条会让重叠处不再叠色,而叠色正是这张图的读法(哪个通道饱和了)。

    **住在这里而不是页面里**:它要构造 QColor 调透明度,而页面一律不许造颜色 ——
    自己造的颜色会绕过红光模式的映射(`test_page_does_not_construct_colors`
    盯着这条,它刚刚就抓了我一次)。
    """

    _FIELDS = ("CHART_A", "CHART_B", "CHART_C")

    def __init__(self, hist, width: int = 300, height: int = 130,
                 parent: QWidget | None = None):
        super().__init__(width, height, parent)
        self._hist = list(hist or ())

    def paint(self, p: QPainter, w: float, h: float) -> None:
        self.fill_bg(p, w, h)
        for i, chan in enumerate(self._hist[:3]):
            if not chan:
                continue
            path = QPainterPath()
            n = max(1, len(chan) - 1)
            for x, v in enumerate(chan):
                px, py = x / n * w, h - float(v) * (h - 4)
                if x == 0:
                    path.moveTo(px, py)
                else:
                    path.lineTo(px, py)
            col = QColor(getattr(theme.Q, self._FIELDS[i % 3]))
            col.setAlpha(200)
            p.setPen(QPen(col, 1.2))
            p.drawPath(path)


class BlockMap(Canvas):
    """aria2NG 式分块方块图。绿=完成 / 琥珀=传输中 / 灰=待传。

    几何(格距/边长/列数)沿用 ``views.transfers`` 的常量 —— 那套参数是按
    "宽而矮、一眼看出空洞在哪"调出来的,换框架没有理由重调。
    """

    def __init__(self, parent: QWidget | None = None):
        from astro_smb_app.views.transfers import BLOCK_COLS, CELL

        super().__init__(0, 0, parent)
        self._states: list[int] = []
        self._cols = BLOCK_COLS
        self._cell = CELL
        self.setFixedHeight(self._cell)

    def set_states(self, states: Sequence[int]) -> None:
        from astro_smb_app.views.transfers import CELL

        self._states = list(states or ())
        cols = min(self._cols, len(self._states)) or 1
        rows = -(-len(self._states) // cols)
        self.setFixedHeight(max(CELL, rows * CELL))
        self.update()

    def paint(self, p: QPainter, w: float, h: float) -> None:
        from astro_smb_app.views.transfers import ACTIVE, CELL, DONE, SQUARE

        if not self._states:
            return
        cols = min(self._cols, len(self._states)) or 1
        for i, st in enumerate(self._states):
            col = (theme.Q.OK if st == DONE else
                   theme.Q.WARN if st == ACTIVE else
                   theme.alpha(theme.Q.TEXT_FAINT, 0.45))
            p.fillRect(QRectF((i % cols) * CELL, (i // cols) * CELL,
                              SQUARE, SQUARE), col)


class ImageView(QLabel):
    """图片。**红光模式下会整体转成红调** —— 一张全彩缩略图足够毁掉暗适应。"""

    def __init__(self, w: int, h: int, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedSize(w, h)
        self.setAlignment(Qt.AlignCenter)
        self._src: QPixmap | None = None

    def set_path(self, path: str | None) -> bool:
        if not path:
            self._src = None
            self.clear()
            return False
        pix = QPixmap(str(path))
        if pix.isNull():
            self._src = None
            self.clear()
            return False
        self._src = pix
        self.refresh()
        return True

    def refresh(self) -> None:
        if self._src is None:
            return
        pix = self._src
        if theme.current_mode() == theme.MODE_RED:
            pix = QPixmap.fromImage(_red_tint(pix.toImage()))
        self.setPixmap(pix.scaled(self.size(), Qt.KeepAspectRatio,
                                  Qt.SmoothTransformation))


class ZoomView(QWidget):
    """可缩放、可拖动平移的图片视图,带**像素读数**与星点叠加。

    影像查看页原来只有一个定死尺寸的 `ImageView` —— 一张 6248×4176 的片子
    被缩到 820px 宽,**看不出星点是圆是扁**,而那正是判断导星好坏的直接证据。
    老 UI 那边有一整条:适应窗口 / 1:1 / ± / 百分比、滚轮缩放、拖动平移、
    底部像素读数。

    坐标约定:``self._zoom`` 是**显示像素 / 图像像素**,``self._ox/_oy`` 是
    图像左上角在控件里的位置。两者一起决定 `img→控件` 的仿射,
    **反查(控件→图像)必须用同一对参数** —— 各写一份迟早在缩放时对不上,
    而对不上的表现是"读数指向别的像素",看不出来。
    """

    #: 鼠标位置对应的**图像坐标**(x, y);离开图像时发 (-1, -1)
    hovered = Signal(int, int)
    #: 缩放变了(百分比)
    zoomed = Signal(float)

    MIN_ZOOM, MAX_ZOOM = 0.02, 32.0

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._src: QPixmap | None = None
        self._zoom = 1.0
        self._ox = self._oy = 0.0
        self._fit = True
        self._drag: tuple[float, float] | None = None
        self._stars: list[tuple[float, float]] = []
        self._show_stars = False
        self.setMouseTracking(True)
        self.setMinimumSize(320, 240)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    # -- 内容 ---------------------------------------------------------
    def set_path(self, path: str | None) -> bool:
        if not path:
            self._src = None
            self.update()
            return False
        pix = QPixmap(str(path))
        if pix.isNull():
            self._src = None
            self.update()
            return False
        self._src = pix
        self.fit()
        return True

    def set_stars(self, points, show: bool = True) -> None:
        """匹配上的星点(图像坐标)。**画在图上而不是另开一张** ——
        要看的就是"这颗星在这张图的这个位置上是什么形状"。

        **不许写 `points or ()`。** 真实调用方给的是板解算返回的
        `matched_xy` —— 一个 **numpy 数组**,而对元素数 >1 的数组做真值判断
        直接抛 `ValueError: truth value ... is ambiguous`。
        独立验收实测:解算成功几乎总是匹配到几十颗星,所以这不是边角
        而是**必现** —— 而且异常抛在"按钮恢复可点"之后、"写回结果"之前,
        于是界面看着像解算完了,面板却永远冻在「正在解算…」。

        写这个坑的原因也记一下:当时的闸门喂的是 **Python 列表**,
        而真实调用方从来不给列表。**测试喂的形状不是调用方的形状,
        等于没测。**
        """
        self._stars = [(float(x), float(y)) for x, y in _iter_points(points)]
        self._show_stars = bool(show and self._stars)
        self.update()

    def show_stars(self, on: bool) -> None:
        self._show_stars = bool(on and self._stars)
        self.update()

    # -- 缩放 ---------------------------------------------------------
    @property
    def zoom(self) -> float:
        return self._zoom

    def fit(self) -> None:
        """适应窗口。**这是默认** —— 一打开就该看到整张图。"""
        self._fit = True
        self._apply_fit()
        self.update()
        self.zoomed.emit(self._zoom)

    def actual_size(self) -> None:
        self.set_zoom(1.0)

    def set_zoom(self, z: float, anchor=None) -> None:
        """定点缩放。``anchor`` 是控件坐标,给了就让那一点**保持不动** ——
        不然滚轮放大时图像会往左上角跑,想看的地方越缩越远。"""
        if self._src is None:
            return
        z = max(self.MIN_ZOOM, min(self.MAX_ZOOM, float(z)))
        if anchor is None:
            anchor = (self.width() / 2.0, self.height() / 2.0)
        ix = (anchor[0] - self._ox) / max(1e-9, self._zoom)
        iy = (anchor[1] - self._oy) / max(1e-9, self._zoom)
        self._zoom = z
        self._fit = False
        self._ox = anchor[0] - ix * z
        self._oy = anchor[1] - iy * z
        self.update()
        self.zoomed.emit(self._zoom)

    def _apply_fit(self) -> None:
        if self._src is None:
            return
        w, h = self._src.width(), self._src.height()
        if w <= 0 or h <= 0:
            return
        self._zoom = min(self.width() / w, self.height() / h)
        self._ox = (self.width() - w * self._zoom) / 2.0
        self._oy = (self.height() - h * self._zoom) / 2.0

    # -- 坐标 ---------------------------------------------------------
    def to_image(self, x: float, y: float) -> tuple[int, int]:
        """控件坐标 → 图像坐标。**与 `paintEvent` 用同一对 zoom/offset。**"""
        if self._src is None or self._zoom <= 0:
            return -1, -1
        ix = int((x - self._ox) / self._zoom)
        iy = int((y - self._oy) / self._zoom)
        if 0 <= ix < self._src.width() and 0 <= iy < self._src.height():
            return ix, iy
        return -1, -1

    # -- 事件 ---------------------------------------------------------
    def resizeEvent(self, ev) -> None:      # noqa: N802
        if self._fit:
            self._apply_fit()
        super().resizeEvent(ev)

    def wheelEvent(self, ev) -> None:       # noqa: N802
        if self._src is None:
            return
        step = 1.25 if ev.angleDelta().y() > 0 else 1 / 1.25
        pos = ev.position()
        self.set_zoom(self._zoom * step, (pos.x(), pos.y()))

    def mousePressEvent(self, ev) -> None:  # noqa: N802
        pos = ev.position()
        self._drag = (pos.x() - self._ox, pos.y() - self._oy)

    def mouseReleaseEvent(self, ev) -> None:  # noqa: N802
        self._drag = None

    def mouseMoveEvent(self, ev) -> None:   # noqa: N802
        pos = ev.position()
        if self._drag is not None and (ev.buttons() & Qt.LeftButton):
            self._fit = False
            self._ox = pos.x() - self._drag[0]
            self._oy = pos.y() - self._drag[1]
            self.update()
            return
        ix, iy = self.to_image(pos.x(), pos.y())
        self.hovered.emit(ix, iy)

    def leaveEvent(self, ev) -> None:       # noqa: N802
        self.hovered.emit(-1, -1)

    def paintEvent(self, ev) -> None:       # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), theme.Q.BG)
        if self._src is None:
            return
        pix = self._src
        if theme.current_mode() == theme.MODE_RED:
            pix = QPixmap.fromImage(_red_tint(pix.toImage()))
        w = pix.width() * self._zoom
        h = pix.height() * self._zoom
        # 放大到 1:1 以上时用**近邻**:平滑插值会把单个像素糊成一团,
        # 而放这么大就是为了看单个星点的形状。
        p.setRenderHint(QPainter.SmoothPixmapTransform, self._zoom < 1.0)
        p.drawPixmap(QRectF(self._ox, self._oy, w, h), pix,
                     QRectF(0, 0, pix.width(), pix.height()))
        if self._show_stars:
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(pen(theme.Q.OK, 1.2))
            p.setBrush(Qt.NoBrush)
            r = max(3.0, 6.0 * self._zoom)
            for sx, sy in self._stars:
                p.drawEllipse(QPointF(self._ox + sx * self._zoom,
                                      self._oy + sy * self._zoom), r, r)


def _red_tint(img: QImage) -> QImage:
    """灰度化后乘上强调色 —— 保留结构,去掉短波。"""
    gray = img.convertToFormat(QImage.Format_Grayscale8)
    out = gray.convertToFormat(QImage.Format_ARGB32)
    tint = theme.Q.ACCENT
    painter = QPainter(out)
    painter.setCompositionMode(QPainter.CompositionMode_Multiply)
    painter.fillRect(out.rect(), QColor(tint.red(), tint.green(), tint.blue()))
    painter.end()
    return out


# ------------------------------------------------------------------ 表

#: 一列的宽度:整数 = 固定像素,``"*"`` = 吃掉剩余
ColSpec = int | str


class _TableModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.rows: list[dict] = []
        self.ncols = 1

    def rowCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else self.ncols

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self.rows[index.row()]
        cells = row.get("cells") or ()
        cell = cells[index.column()] if index.column() < len(cells) else {}
        if role == Qt.UserRole:
            return cell
        if role == Qt.DisplayRole:
            return str(cell.get("text", ""))
        if role == Qt.ToolTipRole:
            return cell.get("tip") or None
        return None

    def set_rows(self, rows: list[dict], ncols: int) -> None:
        self.beginResetModel()
        self.rows = rows
        self.ncols = max(1, ncols)
        self.endResetModel()


class _CellDelegate(QStyledItemDelegate):
    """自己画 cell:主行 + 可选副行 + 可选彩色胶囊。

    副行不做成"第二列"是有原因的:导星页那句
    ``RMS 0.74″ (RA 0.62 / DEC 0.41)`` 挤进右边一列固定宽度必被截断,
    而"是 RA 还是 DEC 出问题"恰恰是那一页要回答的。
    """

    PAD_X = 6
    PAD_Y = 3
    #: 勾选模式下行首那个方框的边长
    BOX = 13

    def paint(self, p: QPainter, option, index) -> None:
        cell = index.data(Qt.UserRole) or {}
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        rect: QRect = option.rect
        text = str(cell.get("text", ""))
        chip = cell.get("chip")
        align = cell.get("align", "left")
        sub = cell.get("sub") or ""

        f = QFont(option.font)
        f.setPixelSize(int(cell.get("size", theme.Font.ICON
                                    if cell.get("glyph")
                                    else theme.Font.BODY)))
        if cell.get("glyph") and not cell.get("size") and text:
            f.setPixelSize(glyph_px(
                text, theme.Font.ICON, f,
                max_w=max(0, rect.width() - self.PAD_X * 2)))
        f.setBold(cell.get("weight") == "semibold")
        if cell.get("mono"):
            f.setFamily(theme.Font.MONO.split(",")[0])
        p.setFont(f)
        fm = QFontMetrics(f)

        color = cell.get("color")
        if color is None:
            color = theme.tone_color(cell.get("tone"))
        if cell.get("dim") and cell.get("tone") is None:
            color = theme.Q.TEXT_DIM

        inner = rect.adjusted(self.PAD_X, self.PAD_Y, -self.PAD_X, -self.PAD_Y)
        line_h = fm.height()
        y = inner.top()
        # 类型符号那一列**跨整行居中**。别的列有副行、要顶行对齐,而符号列
        # 永远只有一行 —— 顶行对齐会让它吊在两行行高的上半截。
        if cell.get("glyph"):
            y = inner.top() + (inner.height() - line_h) / 2.0

        # **勾选模式要看得见框。** 老 UI 那边是 WinUI ListView 的 `Multiple`,
        # 它自带每行一个复选框;Qt 的 MultiSelection 只改选中语义、不画任何
        # 东西 —— 于是"勾选模式"打开之后界面毫无变化,用户报的就是这条。
        # 画在第一列的左边距里,不新增一列(新增列会把所有调用方的列下标挪位)。
        table = self.parent()
        if (index.column() == 0
                and getattr(table, "_check_mode", False)):
            box = QRectF(inner.left(), inner.top() + (line_h - self.BOX) / 2.0,
                         self.BOX, self.BOX)
            on = bool(option.state & QStyle.State_Selected)
            path = QPainterPath()
            path.addRoundedRect(box, 3.0, 3.0)
            p.fillPath(path, QColor(theme.Q.ACCENT if on else theme.Q.BG_ALT))
            p.setPen(QPen(QColor(theme.Q.ACCENT if on else theme.Q.BORDER_HI),
                          1.0))
            p.drawPath(path)
            if on:
                # 实心方块和"选中"长得太像,勾必须画出来
                tick = QPainterPath()
                tick.moveTo(box.left() + 3.0, box.center().y())
                tick.lineTo(box.center().x() - 0.5, box.bottom() - 3.5)
                tick.lineTo(box.right() - 2.5, box.top() + 3.5)
                p.setPen(QPen(QColor(theme.Q.ON_ACCENT), 1.8))
                p.drawPath(tick)
            inner.setLeft(inner.left() + self.BOX + 6)

        if chip and text:
            bg, fg = chip
            wpx = fm.horizontalAdvance(text) + 10
            path = QPainterPath()
            path.addRoundedRect(QRectF(inner.left(), y, wpx, line_h), 6.0, 6.0)
            p.fillPath(path, QColor(bg))
            p.setPen(QColor(fg))
            p.drawText(QRectF(inner.left(), y, wpx, line_h),
                       Qt.AlignCenter, text)
        elif text:
            flag = (Qt.AlignRight | Qt.AlignVCenter if align == "right" else
                    Qt.AlignHCenter | Qt.AlignVCenter if align == "center" else
                    Qt.AlignLeft | Qt.AlignVCenter)
            p.setPen(color)
            # **符号不省略。** 一个被省略的符号就是一个 `…` —— 那比画小了
            # 糟得多(至少小的还认得出是文件还是目录)。宽度已经在
            # `glyph_px` 里按列宽夹过了,这里让它整个画出来。
            shown = (text if cell.get("glyph")
                     else fm.elidedText(text, Qt.ElideRight, inner.width()))
            p.drawText(QRect(inner.left(), y, inner.width(), line_h),
                       flag, shown)

        if sub:
            sf = QFont(f)
            sf.setPixelSize(theme.Font.SMALL)
            sf.setBold(False)
            p.setFont(sf)
            sfm = QFontMetrics(sf)
            p.setPen(cell.get("sub_color") or theme.Q.TEXT_DIM)
            p.drawText(QRect(inner.left(), y + line_h, inner.width(),
                             sfm.height()),
                       Qt.AlignLeft | Qt.AlignVCenter,
                       sfm.elidedText(sub, Qt.ElideRight, inner.width()))
        p.restore()

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        cell = index.data(Qt.UserRole) or {}
        f = QFont(option.font)
        f.setPixelSize(int(cell.get("size", theme.Font.ICON
                                    if cell.get("glyph")
                                    else theme.Font.BODY)))
        fm = QFontMetrics(f)
        h = fm.height() + self.PAD_Y * 2
        if cell.get("sub"):
            sf = QFont(f)
            sf.setPixelSize(theme.Font.SMALL)
            h += QFontMetrics(sf).height()
        return QSize(40, h)


class DataTable(QTableView):
    """数据驱动的表。

    **行的身份是 ``key``,永远不是下标。** 下标随增删行漂移,选中就跟着漂 ——
    这是本仓库点名的那条,换框架照样成立(浏览页用共享内相对路径当键)。
    """

    key_selected = Signal(str)
    key_activated = Signal(str)
    keys_checked = Signal(list)

    def __init__(self, cols: Sequence[ColSpec] = ("*",), *,
                 multi: bool = False, header: Sequence[str] | None = None,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._model = _TableModel(self)
        self.setModel(self._model)
        self.setItemDelegate(_CellDelegate(self))
        self.setShowGrid(False)
        self.setWordWrap(False)
        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        # multi=True 的表**默认就是 Extended**(ctrl 加选 / shift 连选) ——
        # 老 UI 不开勾选模式时就是这个。原来这里给 Extended、而浏览页
        # 建表时又没传 multi,于是启动后 ctrl/shift 一直不管用。
        self.setSelectionMode(QAbstractItemView.ExtendedSelection if multi
                              else QAbstractItemView.SingleSelection)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self._cols = list(cols)
        self._header = list(header) if header else []
        show_if(self.horizontalHeader(), header)
        self.horizontalHeader().setHighlightSections(False)
        self._keys: list[str] = []
        self._quiet = 0
        self._check_mode = False
        self.selectionModel().selectionChanged.connect(self._on_sel)
        self.doubleClicked.connect(self._on_dbl)

    # -- 选中语义 ------------------------------------------------------
    def set_check_mode(self, on: bool) -> None:
        """勾选模式开关 —— 对齐老 UI 的 `Multiple` / `Extended` 两档。

        **关掉勾选模式不等于只能选一个。** 原来这里给的是
        `MultiSelection`↔`SingleSelection`,于是不开勾选模式时 ctrl / shift
        全都失效(用户报的第 2 条),而开了勾选模式界面又毫无变化(第 1 条,
        Qt 的 MultiSelection 只改语义、不画框 —— 框由 `_CellDelegate` 画)。

        - **开**:`MultiSelection` —— 单击即勾选/取消,不用按住 ctrl;行首画框。
        - **关**:`ExtendedSelection` —— 单击换选中,ctrl 加选,shift 连选。
        """
        self._check_mode = bool(on)
        self.setSelectionMode(QAbstractItemView.MultiSelection if on
                              else QAbstractItemView.ExtendedSelection)
        self.clearSelection()
        self._apply_cols()          # 第一列宽度跟着变(见 `_apply_cols`)
        self.viewport().update()

    # -- 数据 ---------------------------------------------------------
    def set_rows(self, rows: list[dict]) -> None:
        """``rows`` 形如 ``[{"key": ..., "cells": [{...}, ...]}, ...]``。"""
        self._keys = [str(r.get("key", i)) for i, r in enumerate(rows)]
        self._model.set_rows(rows, len(self._cols))
        self._apply_cols()
        self.resizeRowsToContents()

    def update_cell(self, key: str, col: int, **kw) -> bool:
        """就地改一个 cell(不重建整张表)。

        目录行的"… 项 → 真数字"、进度类的数字都走这里 —— 每来一个数就
        ``set_rows`` 一次会让选中丢失、滚动位置跳回顶部。
        """
        if key not in self._keys:
            return False
        row = self._model.rows[self._keys.index(key)]
        cells = row.get("cells") or ()
        if col >= len(cells):
            return False
        cells[col].update(kw)
        self.viewport().update()
        return True

    def _apply_cols(self) -> None:
        hh = self.horizontalHeader()
        for i, spec in enumerate(self._cols):
            if spec == "*":
                hh.setSectionResizeMode(i, QHeaderView.Stretch)
            else:
                hh.setSectionResizeMode(i, QHeaderView.Fixed)
                # **勾选模式要给第一列加宽。** 框画在第一列的左边距里,而
                # 浏览页那一列只有 22px(就一个类型图标)—— 不加宽的话
                # 图标被挤成一条 2 像素的竖线,看着像渲染坏了。
                extra = (_CellDelegate.BOX + 6
                         if (i == 0 and self._check_mode) else 0)
                self.setColumnWidth(i, int(spec) + extra)
        if self._header:
            self._model.setHeaderData  # 表头文本走下面的 headerData 兜底
            for i, name in enumerate(self._header):
                self._model.setHeaderData(i, Qt.Horizontal, name)

    # -- 选中 ---------------------------------------------------------
    def keys(self) -> list[str]:
        return list(self._keys)

    def selected_key(self) -> str:
        idx = self.currentIndex()
        if not idx.isValid() or idx.row() >= len(self._keys):
            return ""
        return self._keys[idx.row()]

    def checked_keys(self) -> list[str]:
        rows = sorted({i.row() for i in self.selectionModel().selectedRows()})
        return [self._keys[r] for r in rows if r < len(self._keys)]

    def select_key(self, key: str) -> None:
        """程序化选中。**要闭麦** —— 否则 Qt 也会发 selectionChanged,
        调用方收到就当成用户点了,于是又列一次目录、又填一次表(这是
        新前端在 Uno 上踩过的那条,``Quiet()`` 计数器同一个道理)。"""
        if key not in self._keys:
            return
        row = self._keys.index(key)
        self._quiet += 1
        try:
            self.selectRow(row)
        finally:
            self._quiet -= 1

    def _on_sel(self, *_):
        if self._quiet:
            return
        # 单选之外的两种模式都要报"当前选了哪些" —— 「下载所选(n)」那个
        # 计数在 ctrl 多选下也得跟着动,不能只在勾选模式里才更新。
        if self.selectionMode() != QAbstractItemView.SingleSelection:
            self.keys_checked.emit(self.checked_keys())
        key = self.selected_key()
        if key:
            self.key_selected.emit(key)

    def _on_dbl(self, index):
        if index.isValid() and index.row() < len(self._keys):
            self.key_activated.emit(self._keys[index.row()])


def cell(text: str = "", **kw) -> dict:
    """构造一个 cell。把散落的 dict 字面量收成一个口子,免得 key 拼错。"""
    out: dict = {"text": text}
    out.update({k: v for k, v in kw.items() if v is not None})
    return out


# ------------------------------------------------------------------ 侧边栏

class SideNav(QWidget):
    """左侧边栏:应用名 + 图标在顶,导航项**分组并有分组小标题**,底部常驻状态区。"""

    navigate = Signal(str)

    def __init__(self, groups: Sequence[tuple[str, Sequence[tuple[str, str, str]]]],
                 parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("Sidebar")
        self.setFixedWidth(theme.NAV_W)
        col = vbox(self, gap="sm", pad="nav")

        head = hbox(gap="sm")
        mark = label("◉")
        mark.setObjectName("AppMark")
        name = label("Astro SMB")
        name.setObjectName("AppName")
        head.addWidget(mark)
        head.addWidget(name)
        head.addStretch(1)
        col.addLayout(head)
        col.addWidget(label(_("天文摄影 SMB 工具箱"), role="subtitle"))
        col.addSpacing(theme.Space.SM)

        self._buttons: dict[str, QPushButton] = {}
        # 组名与项名进来时是 **msgid**(`shell.NAV_GROUPS` 用 `N_()` 标记),
        # 到这一步才翻 —— 表在模块级,提前翻会冻在 import 时的语言上
        for gname, items in groups:
            col.addWidget(group_title(_(gname)))
            for tag, sym, text in items:
                b = QPushButton(f"  {sym}   {_(text)}")
                b.setObjectName("NavItem")
                b.setCursor(Qt.PointingHandCursor)
                b.setProperty("active", "false")
                b.clicked.connect(lambda _=False, t=tag: self.navigate.emit(t))
                col.addWidget(b)
                self._buttons[tag] = b
            col.addSpacing(theme.Space.SM)

        col.addStretch(1)
        col.addWidget(Divider())
        self.status = vbox(gap="xs")
        col.addLayout(self.status)

    def set_active(self, tag: str) -> None:
        for key, b in self._buttons.items():
            set_prop(b, "active", "true" if key == tag else "false")
