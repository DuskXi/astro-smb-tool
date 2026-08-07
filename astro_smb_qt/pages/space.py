"""空间分析页:嵌套 treemap + 目录树,双向联动。

整张图就是**一个画布 + 一条显示列表**:上限约 900 个图元,几何与分类全部由
``astro_smb_app.views.space`` 算好(它的配色用 ``zlib.crc32`` 而不是 ``hash()`` ——
Python 的字符串 hash 每进程随机,原来同一种文件类型的颜色每次启动都在变)。

命中测试**不挂在图元上**:``hits`` 是一张独立的矩形表,点击时反查。
900 个图元挂 900 个事件在任何框架下都不是好主意。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QSplitter

from astro_smb.util import human_size
from astro_smb.i18n import N_, gettext as _
from astro_smb_app import dircache
from astro_smb_app.views import space as sv
from astro_smb_qt import theme, widgets as W
from astro_smb_qt.pages.base import Page, PageHeader, page_layout
from astro_smb_qt.workers import CancelToken, with_client

MAP_W, MAP_H = 880, 520
#: 目录树只列**直接子级**,按大小倒序取前这么多 —— 整棵铺开没人看得完
TREE_ROWS = 200


class SpacePage(Page):
    TITLE = N_("空间分析")
    SUBTITLE = N_("谁占了空间 —— 嵌套 treemap,块面积正比于占用")

    def __init__(self, shell):
        super().__init__(shell)
        self.root = None
        self.share = ""
        self.path = ""
        self.crumbs: list[tuple[str, str]] = []
        self.selected = ""
        self._hover = ""
        self._hits: list[list] = []
        self._cancel: CancelToken | None = None
        self._busy = False
        self._cache_age = 0.0
        self._build()

    def _build(self) -> None:
        root = page_layout(self)
        self.header = PageHeader(_(self.TITLE), _(self.SUBTITLE))
        self.share_combo = W.combo(on_change=self._pick_share)
        self.share_combo.setMinimumWidth(160)
        self.header.add_action(self.share_combo)
        self.up_btn = W.button(_("上级"), on_click=self._go_up, enabled=False)
        self.header.add_action(self.up_btn)
        self.scan_btn = W.button(_("扫描此目录"), kind="primary",
                                 on_click=self._toggle_scan)
        self.header.add_action(self.scan_btn)
        root.addWidget(self.header)

        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        map_card = W.Card(_("占用图"), _("点块选中 · 双击下钻 · 悬停看名字与大小"))
        self.map_card = map_card
        self.canvas = W.OpsCanvas(MAP_W, MAP_H)
        # **高度不写死。** `OpsCanvas(w, h)` 内部是 `setFixedHeight` ——
        # treemap 于是永远 520px 高,窗口拉高下面空一大块。老 UI 跟着重排。
        self.canvas.setMinimumHeight(320)
        self.canvas.setMaximumHeight(16777215)
        self.canvas.resized.connect(self._retile)
        self.canvas.hit.connect(self._pick_block)
        # 双击下钻 + 悬停高亮 —— 老 UI 的 treemap 两样都有
        self.canvas.hit_activated.connect(self._drill)
        self.canvas.hit_hovered.connect(self._on_hover)
        map_card.add(self.canvas, 1)
        self.status = W.label("", role="subtitle", wrap=True)
        map_card.add(self.status)
        split.addWidget(map_card)

        tree_card = W.Card(_("目录树"), _("点行联动图块 · 双击下钻"))
        self.tree = W.DataTable(["*", 92, 52])
        self.tree.key_selected.connect(self._pick_row)
        self.tree.key_activated.connect(self._drill)
        tree_card.add(self.tree, 1)
        split.addWidget(tree_card)
        split.setStretchFactor(0, 1)
        split.setSizes([900, 380])

        self.state = W.StateStack(split)
        root.addWidget(self.state, 1)
        self._idle()

    def _retile(self) -> None:
        """画布尺寸变了 —— treemap 的几何是按像素算的,必须重排。"""
        if self.root is not None:
            self._render()

    def _idle(self) -> None:
        self.state.show_empty(
            _("还没有扫描结果"),
            _("选好共享点「扫描此目录」。现在是还没扫过,不是设备上没东西。大目录要走一遍全部子级,几十秒是正常的。"))

    # ------------------------------------------------------------ 契约

    def on_connected(self, shares) -> None:
        self.share_combo.blockSignals(True)
        self.share_combo.clear()
        self.share_combo.addItems(shares)
        self.share_combo.blockSignals(False)
        if shares:
            self.share = shares[0]

    def on_close(self) -> None:
        if self._cancel is not None:
            self._cancel.cancel()

    def on_theme(self) -> None:
        """treemap 的每一块都带着生成那一刻的填充色。"""
        if self.root is not None:
            self._render()

    # ------------------------------------------------------------ 扫描

    def _pick_share(self, idx: int) -> None:
        if 0 <= idx < self.share_combo.count():
            self.share = self.share_combo.itemText(idx)
            self.crumbs = []        # 换共享清面包屑
            self.path = ""
            self._idle()

    def _toggle_scan(self) -> None:
        """一颗按钮两个状态(老 UI 同款)。原来全页**没有任何取消入口** ——
        大目录扫起来几十秒,按钮还纹丝不动,只能干等。"""
        if self._busy:
            self._stop_scan()
        else:
            self.rescan()

    def _stop_scan(self) -> None:
        if self._cancel is not None:
            self._cancel.cancel()
        # **光 cancel 不够,还要作废世代。**
        #
        # 本地磁盘扫一个目录只要几十毫秒:取消信号还没被工作线程看见,
        # 结果就已经算完了。`on_done` 按世代校验时这份结果**不算过期**,
        # 于是照常被接受 —— 屏幕上「扫描已停止」闪一下(实测约 76ms),
        # 又变回完整的占用图,跟没点过一样。
        #
        # `rescan()` 一直是 `bump()` 了的,停止这条漏了。
        self.bg.bump()
        self._busy = False
        self.scan_btn.setText(_("扫描此目录"))
        self.state.show_empty(
            _("扫描已停止"),
            _("扫到一半的结果不完整,没有保留 —— 重新扫一次即可。"))

    def rescan(self) -> None:
        if not self.share or self.shell.client_factory is None:
            self.shell.notice(_("先连接设备并选一个共享"))
            return
        gen = self.bg.bump()
        token = CancelToken()
        self._cancel = token
        self._busy = True
        self.scan_btn.setText(_("停止"))
        share, path, factory = self.share, self.path, self.shell.client_factory
        where = f"{share}\\{path}" if path else share
        self.state.show_busy(_("正在扫描 {where} 的占用").format(where=where))

        def work(report):
            def run(client):
                # **先查磁盘索引。** 老 UI 有两层缓存,进页/下钻/上级都秒出,
                # 并且明确标注"(本地索引 · 3 分钟前的统计)";Qt 原来每次都
                # 真扫 —— 真机 222 GB 每层几百次 listdir。
                # 缓存**只在这里读一次**,读到就直接用;`put_tree` 自己会挡掉
                # partial 树(枚举失败过的子树不能当完整结果缓存)。
                hit = dircache.get_tree(client, share, path)
                if hit is not None:
                    return hit[0], hit[1]
                # **进度要报出来。** 原来不传 `on_progress`,忙态自始至终是
                # 一行不动的字 —— 几十秒里没有任何数字在动,看着就像卡死了。
                tree = client.dir_tree(share, path, cancel=token.event,
                                       on_progress=lambda n, b: report((n, b)))
                dircache.put_tree(client, share, path, tree)
                return tree, 0.0

            return with_client(factory, run)

        def progress(payload):
            n, b = payload
            self.state.show_busy(
                _("正在扫描 {where} 的占用 —— 已扫 {n} 文件 · {0}").format(
                    human_size(b), where=where, n=n))

        self.bg.run(work, gen=gen, on_done=self._apply,
                    on_error=lambda e: self._fail(e), on_progress=progress)

    def _apply(self, payload) -> None:
        self._busy = False
        self.scan_btn.setText(_("扫描此目录"))
        root, age = payload
        # 缓存命中时**必须说出来**:数字可能过期,而一个静默的旧数字比
        # 慢一点糟得多(老 UI 那句"(本地索引 · N 前的统计)"就是干这个的)。
        self._cache_age = float(age or 0.0)
        self.root = root
        self.state.show_content()
        self._render()

    def _fail(self, exc: BaseException) -> None:
        self._busy = False
        self.scan_btn.setText(_("扫描此目录"))
        self.state.show_empty(_("扫描失败"), str(exc))
        self.report(exc, _("扫描占用"))

    # ------------------------------------------------------------ 渲染

    def _render(self) -> None:
        if self.root is None:
            return
        w = float(max(320, self.canvas.width()))
        h = float(max(240, self.canvas.height()))
        tm = sv.treemap(self.root, w, h)
        ops: list[dict] = []
        for x, y, bw, bh, rgb in tm.fills:
            ops.append({"op": "rect", "x": x, "y": y, "w": bw, "h": bh,
                        "fill": _argb(rgb)})
        for x, y, bw, bh in tm.outlines:
            ops.append({"op": "rect", "x": x, "y": y, "w": bw, "h": bh,
                        "stroke": theme.C.BG, "width": 1.0})
        for x, y, text, size, weight, maxw in tm.labels:
            # **`maxw` 必须传下去。** 画布早就实现了它(`fm.elidedText`),
            # 只是没人传 —— 于是文件名整条画出去,压在相邻块上,下钻一层
            # 整屏文字糊成一团。又一次「图元支持 ≠ 页面用了」。
            # **嵌套目录的标题带上恒用「强调色上的文字」色。** 那条带子
            # 是中蓝底,而白天档的 `TEXT` 是近黑 —— 压上去几乎读不出来
            # (老 UI 那里恒为白字)。叶级标签画在浅色块上,用正文色。
            band = weight == "semibold"
            ops.append({"op": "text", "x": x, "y": y, "text": text,
                        "size": size,
                        "fill": theme.C.ON_BAND if band else theme.C.TEXT,
                        "weight": weight, "maxw": maxw})
        # **选中与悬停必须画出来。** 原来点一个块只是把树里那一行选上,
        # treemap 本身毫无变化 —— 于是"点了没反应"(用户报的"有图,但是
        # 没有交互")。高亮画在最后 = 盖在所有块之上。
        self._hits = [[x1, y1, x2, y2, p] for x1, y1, x2, y2, p in tm.hits]
        self.canvas.set_ops(ops + self._marks(), self._hits)

        # 老 UI:`当前根 X · 共 15.29 GB · 649 文件 · 图中已省略 …`
        # **文件数原来整页没有第二处能看到。**
        bits = [_("当前根 {0}").format(self.path or self.share),
                _("共 {0}").format(human_size(int(self.root.size)))]
        n_files = int(getattr(self.root, "file_count", 0) or 0)
        if n_files:
            bits.append(_("{n_files} 文件").format(n_files=n_files))
        bits.append(_("{blocks} 个块").format(blocks=tm.blocks))
        if self._cache_age > 0:
            bits.append(_("本地索引 · {0}的统计").format(dircache.age_text(self._cache_age)))
        if tm.truncated:
            # **省略必须说出来**:默默画少几百个块,用户会以为那些目录不存在
            bits.append(_("图中已省略 {omitted} 个小块(下钻可看清)").format(omitted=tm.omitted))
        self.status.setText(" · ".join(bits))
        self.map_card.set_chip(human_size(int(self.root.size)), "accent")
        self.header.set_subtitle(
            f"{self.share} / {self.path.replace(chr(92), ' / ')}"
            if self.path else self.share)
        self.up_btn.setEnabled(bool(self.crumbs))

        kids = sorted([c for c in self.root.children], key=lambda c: -c.size)
        total = max(1, self.root.size)
        self.tree.set_rows([
            {"key": c.path, "cells": [
                W.cell(("▣ " if c.is_dir else "▢ ") + c.name),
                W.cell(human_size(c.size), align="right", dim=True,
                       size=theme.Font.SMALL),
                # **四舍五入**,不是向下取整 —— 同一份数据老 UI 给 78/21/2,
                # 向下取整给 77/20/1,看起来像两套数据
                W.cell(f"{round(c.size * 100 / total)}%", align="right", dim=True,
                       size=theme.Font.SMALL),
            ]} for c in kids[:TREE_ROWS]])

    # ------------------------------------------------------------ 交互

    def _marks(self) -> list[dict]:
        """选中框 + 悬停框。画在显示列表最后,盖在所有块之上。"""
        out: list[dict] = []
        for path, color, width in ((self._hover, theme.C.TEXT_DIM, 1.0),
                                   (self.selected, theme.C.ACCENT, 2.0)):
            if not path:
                continue
            for x1, y1, x2, y2, p in self._hits:
                if p == path:
                    out.append({"op": "rect", "x": x1 + 1, "y": y1 + 1,
                                "w": max(1.0, x2 - x1 - 2),
                                "h": max(1.0, y2 - y1 - 2),
                                "stroke": color, "width": width})
                    break
        return out

    def _repaint_marks(self) -> None:
        """高亮变了 —— 整份重画。

        **这里以前的注释写的是"只重画高亮,不必重算"** —— 那是句谎话,
        它调的就是 `_render()`,布局照算不误。测了一下:1555 个节点铺成
        900 个块,`views.space.treemap` 一次 1.5 ms,而 hover 只在**换了块**
        时才发(`hit_hovered` 自带去重)。省这一下不值得多养一份缓存状态
        (缓存和真相不同步是这一页更贵的 bug)。所以留着重算,把注释改对。
        """
        if self.root is not None:
            self._render()

    def _on_hover(self, path: str) -> None:
        if path == self._hover:
            return
        self._hover = path
        # tooltip 走共享层的名字与大小,不自己拼
        self.canvas.setToolTip(self._label_for(path) if path else "")
        self._repaint_marks()

    def _label_for(self, path: str) -> str:
        """tooltip **走共享层的 `node_tip`**。

        上面那句注释一直写着"不自己拼",而底下就是自己拼的 —— 拼掉了
        类别与**文件数**(老 UI 是 `Autorun · 3.16 GB · 目录 · 130 文件`)。
        """
        node = self._node_for(path)
        if node is None:
            return path
        return sv.node_tip(node)

    def _node_for(self, path: str):
        if self.root is None:
            return None
        stack = [self.root]
        while stack:
            n = stack.pop()
            if n.path == path:
                return n
            stack.extend(n.children or ())
        return None

    def _pick_block(self, path: str) -> None:
        # **只选中不下钻** —— 每次下钻都要重扫整棵树,误钻一次很贵。
        # 双击才下钻(`hit_activated`),和树那一列一致。
        # 顺序**必须是先重画再选中**:`_repaint_marks()` 会整份 `_render()`,
        # 而那一步 `tree.set_rows(...)` 重建整张表、把刚设的选中当场抹掉
        # (原来的顺序就是这样 —— 点块之后树里一行都没选上)。
        self.selected = path
        self._repaint_marks()
        self.tree.select_key(path)

    def _pick_row(self, path: str) -> None:
        """点树里一行 → **图上也要高亮**。

        原来只写了 `self.selected = path`,没有重画 —— 于是"点了没反应",
        直到鼠标随便晃一下触发 hover 重画,高亮才突然冒出来。
        """
        self.selected = path
        self._repaint_marks()

    def _drill(self, path: str) -> None:
        # **文件不下钻。** 拿文件路径去 `dir_tree` 会"根目录枚举失败",
        # 而失败路径不跑 `_render()` —— 「上级」保持禁用,整页**卡死在
        # 错误页出不来**(只能换共享)。老 UI 双击文件只是高亮。
        node = self._node_for(path)
        if node is not None and not node.is_dir:
            self.shell.notice(_("{name} 是文件,没有下级可看").format(name=node.name))
            return
        # 面包屑记的是**走过来的路**,不从树上反查父节点:每次下钻都重扫,
        # 上一棵树已经不在手里了。
        self.crumbs.append((self.share, self.path))
        self.path = path
        self.rescan()

    def _go_up(self) -> None:
        if not self.crumbs:
            return
        self.share, self.path = self.crumbs.pop()
        self.rescan()


def _argb(rgb) -> str:
    r, g, b = rgb[-3:]
    return f"#FF{int(r):02X}{int(g):02X}{int(b):02X}"
