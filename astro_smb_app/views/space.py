"""空间分析页的**视图模型**:占用树 → treemap 显示列表。

布局递归本来就是纯 Python(老 UI 那边的注释写得很清楚:"一个 WinRT 调用都不发,
只往批量缓冲里塞元组"),这里把它从页面类里抽出来变成真正的纯函数,
输入 ``TreeNode`` + 画布尺寸,输出可直接进画布显示列表的数据。

**顺手修了一个真 bug。** 原来按文件类型取色是
``_PALETTE[hash(ext_category(node)) % len(_PALETTE)]`` —— 而 Python 的字符串
``hash()`` **每个进程都不一样**(哈希随机化)。实测同一个 "图像" 在三次进程里
分别落到 1、2、2 号色:**同一种文件类型的颜色每次启动都在变**。
症状很轻但很扰人("昨天 .fit 是蓝的今天怎么绿了"),而且会让黄金载荷无法确定。
改用 ``zlib.crc32``(跨进程、跨版本稳定)。
"""
from __future__ import annotations

import zlib

from astro_smb.client import TreeNode
from astro_smb.util import human_size
from astro_smb.i18n import gettext as _
from astro_smb_app.entries import ext_category, ext_category_id

# 与老 UI 同一套参数 —— 那是按"一屏能看清、又不至于把控件预算撑爆"调出来的。
BLOCK_BUDGET = 900          # 图元总数预算,超出即停止更深递归
MIN_NEST_AREA = 1600.0      # 目录块内继续嵌套的最小面积(px^2)
MAX_NEST_DEPTH = 4          # 嵌套最大深度
TITLE_H = 14.0              # 嵌套目录块的标题带高度
PAD = 4.0
STROKE_MIN = 6.0            # 小于这个边长不画描边(省一个图元,视觉上也看不出)
NEST_LABEL_W = 44.0
LEAF_LABEL_W = 72.0
LEAF_LABEL_H = 32.0

DIR_COLOR = (0x5A, 0x7D, 0x9A)
PALETTE = [
    (0x4E, 0x79, 0xA7), (0xF2, 0x8E, 0x2B), (0xE1, 0x57, 0x59),
    (0x76, 0xB7, 0xB2), (0x59, 0xA1, 0x4F), (0xED, 0xC9, 0x48),
    (0xB0, 0x7A, 0xA1), (0xFF, 0x9D, 0xA7),
]


def palette_index(category: str) -> int:
    """文件类别 → 调色板下标。**必须跨进程稳定** —— 见模块头。"""
    return zlib.crc32(category.encode("utf-8")) % len(PALETTE)


def shade(rgb: tuple, depth: int) -> tuple:
    """按嵌套深度调明度:奇数层变浅、偶数层加深,幅度随深度略增。"""
    if depth <= 0:
        return rgb
    k = 0.16 + 0.05 * (depth // 2)
    if depth % 2 == 1:
        return tuple(min(255, int(c + (255 - c) * k)) for c in rgb)
    return tuple(max(0, int(c * (1.0 - k))) for c in rgb)


def node_rgb(node: TreeNode, depth: int) -> tuple:
    # 取色走**身份**:换语言不该让整张 treemap 换一套颜色
    base = DIR_COLOR if node.is_dir else PALETTE[palette_index(ext_category_id(node))]
    return shade(base, depth)


class Treemap:
    """一次布局的产物。``hits`` 与图元**分开** —— 命中测试不需要图元,
    图元也不需要知道自己对应哪个节点。老 UI 早就是这么分的(逐元素挂事件在
    win32more 下会永久泄漏),新前端沿用:容器级一个事件 + 几何反查。"""

    def __init__(self) -> None:
        self.fills: list[tuple] = []      # (x, y, w, h, (r,g,b))
        self.outlines: list[tuple] = []   # (x, y, w, h)
        self.labels: list[tuple] = []     # (x, y, text, size, weight, maxw)
        self.hits: list[tuple] = []       # (x1, y1, x2, y2, path)
        self.blocks = 0
        self.omitted = 0

    @property
    def truncated(self) -> bool:
        return self.omitted > 0


def treemap(root: TreeNode | None, width: float, height: float) -> Treemap:
    """占用树 → treemap 几何。``root`` 为空或画布过小时返回空结果。"""
    out = Treemap()
    if root is None or width < 20 or height < 20:
        return out
    items = [c for c in (root.children or ()) if c.size > 0]
    if not items:
        return out
    _layout(out, items, 0, len(items), 2.0, 2.0, width - 4.0, height - 4.0,
            0, sum(c.size for c in items))
    return out


def _layout(out: Treemap, items, lo: int, hi: int, x, y, w, h,
            depth: int, total: int) -> None:
    """squarify:按面积切一刀分两半递归;超预算即整体停手。

    用 ``[lo, hi)`` 下标区间代替列表切片,并把区间和顺着递归传下去 ——
    每层都 ``items[:i]`` 复制再重新 sum,在深树上是纯浪费。
    """
    if lo >= hi:
        return
    if w <= 1 or h <= 1 or out.blocks >= BLOCK_BUDGET:
        out.omitted += hi - lo
        return
    if hi - lo == 1:
        _emit(out, items[lo], x, y, w, h, depth)
        return
    acc, i = 0, lo
    half = total / 2 if total else 0
    while i < hi - 1 and acc < half:
        acc += items[i].size
        i += 1
    frac = (acc / total) if total else 0.5
    if w >= h:
        wa = w * frac
        _layout(out, items, lo, i, x, y, wa, h, depth, acc)
        _layout(out, items, i, hi, x + wa, y, w - wa, h, depth, total - acc)
    else:
        ha = h * frac
        _layout(out, items, lo, i, x, y, w, ha, depth, acc)
        _layout(out, items, i, hi, x, y + ha, w, h - ha, depth, total - acc)


def _emit(out: Treemap, node: TreeNode, x, y, w, h, depth: int) -> None:
    """记一个块;目录块面积够大时在内部(标题带下)继续嵌套。"""
    if out.blocks >= BLOCK_BUDGET or w < 2 or h < 2:
        out.omitted += 1
        return
    bw, bh = max(1.0, w - 2), max(1.0, h - 2)
    out.fills.append((x, y, bw, bh, node_rgb(node, depth)))
    if w >= STROKE_MIN and h >= STROKE_MIN:
        out.outlines.append((x, y, bw, bh))
    out.blocks += 1
    out.hits.append((x, y, x + w, y + h, node.path))

    nest = (node.is_dir and node.children and depth < MAX_NEST_DEPTH
            and w * h >= MIN_NEST_AREA and out.blocks < BLOCK_BUDGET)
    if nest:
        if w > NEST_LABEL_W:
            out.labels.append((x + 5.0, y + 1.0, node.name, 10.0,
                               "semibold", max(8.0, w - 10.0)))
            out.blocks += 1
        ix, iy = x + PAD, y + TITLE_H + 2.0
        iw, ih = w - 2.0 * PAD, h - TITLE_H - 2.0 - PAD
        kids = [c for c in node.children if c.size > 0]
        if iw > 8 and ih > 8 and kids:
            _layout(out, kids, 0, len(kids), ix, iy, iw, ih, depth + 1,
                    sum(c.size for c in kids))
    elif w > LEAF_LABEL_W and h > LEAF_LABEL_H:
        out.labels.append((x + 4.0, y + 3.0,
                           f"{node.name}\n{human_size(node.size)}",
                           11.0, "normal", max(8.0, w - 8.0)))
        out.blocks += 1


def hit_test(hits, px: float, py: float) -> str | None:
    """逆序遍历绘制表:后画的子块在上层,先命中者胜。返回节点路径。"""
    for x1, y1, x2, y2, path in reversed(hits):
        if x1 <= px <= x2 and y1 <= py <= y2:
            return path
    return None


def node_tip(node: TreeNode) -> str:
    kind = _("目录") if node.is_dir else ext_category(node)
    bits = [node.name, human_size(node.size), kind]
    if node.is_dir and node.file_count:
        bits.append(_("{file_count} 文件").format(file_count=node.file_count))
    return " · ".join(bits)
