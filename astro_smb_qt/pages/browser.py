"""浏览页 —— 最核心也最难的一页。

难在它不是"把数据画出来"而是**一套交互**:选共享、进出目录、排序、搜索、
选中、预览、下载、写操作。判读与格式化全部走 ``astro_smb_app.views.browser``
(气量用的是 Pickering 而不是课本那个 1/sin(h),几个语义色阈值都是有前提的
经验值)—— 这一页只负责摆。

三条换框架也不会消失的坑:

* **行身份是共享内相对路径**(``views.browser.entry_key``),不是下标。
* **世代计数器**:用户点得比网络快是常态,迟到的结果整份丢弃,
  否则"进了 B 目录却显示 A 目录的内容"。
* **表自己滚**:``QTableView`` 不能再套进 ``QScrollArea`` —— 那样它拿到的是
  无限高度,永远不滚。
"""
from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QFileDialog, QListWidget, QSplitter,
                               QWidget)

from astro_smb.util import human_size
from astro_smb.i18n import N_, gettext as _
from astro_smb_app.entries import FITS_EXTS, sorted_entries
from astro_smb_app.views import browser as bv
from astro_smb_app.views import skychart
from astro_smb_qt import theme, widgets as W
from astro_smb_qt.pages.base import Page, PageHeader, page_layout
from astro_smb_qt.workers import CancelToken, with_client

# 表里只标记,取用时才翻(模块级求值一次)
SORTS = [N_("名字 ↑"), N_("名字 ↓"), N_("大小 ↑"), N_("大小 ↓"),
         N_("时间 ↑"), N_("时间 ↓"), N_("扩展名")]

#: 目录行"… 项"变成真数字的上限。整页都算太慢,而这只是装饰。
CHILD_COUNT_MAX = 200
RADAR_SIZE = 190
#: 方位标注画在圆外,margin 给小了「东」会被左边界静默切掉
RADAR_MARGIN = 18.0
#: 大小列在 ``views.browser.ROW_COLS`` 里的下标 —— 子项计数改的就是它。
#: **不要写成裸的 3**:列一调整,子项计数就会去改时间列(不报错,只是显示错)。
SIZE_COL = 3


def _flat_rows(groups) -> list[tuple[str, str, str, str | None]]:
    """分组键值 → ``(组名, 标签, 值, 语义色名)``。

    ``views.browser.detail_rows`` 做的是同一件事,但它把语义色**烤成了**
    ``#AARRGGBB``(为浅色主题调的三档),换主题就没法用了 —— 这里保留
    ``tone`` 名,颜色交给 :mod:`astro_smb_qt.theme`。
    **判读本身仍在共享层**:tone 是 ``_astro_details`` 按阈值给的,这里只做结构展平。
    """
    out: list[dict] = []
    for _glyph, name, pairs in groups:
        for item in pairs:
            note = item[2] if len(item) > 2 else ""
            mono = bool(item[3]) if len(item) > 3 else False
            tone = item[4] if len(item) > 4 else None
            # 第 6 项是**量条**(`('altbar', 35.4)`)。原来这里只取到第 5 项,
            # 于是高度角那条 0°–90° 的横条被静默丢掉 —— 老 UI 有,而它正是
            # "35° 到底算高还是低"一眼可判的那个东西。
            bar = item[5] if len(item) > 5 else None
            # glyph 一并带上:图标按**码位**换,不按组名查(组名会被翻译)
            out.append({"group": name, "glyph": _glyph, "key": str(item[0]),
                        "value": f"{item[1]} {note}".strip(),
                        "tone": tone, "mono": mono, "bar": bar})
    return out


#: 徽章种类 → 语义色。老 UI 那一排是**分色**的(亮场绿、滤镜琥珀、序号蓝、
#: 夜次按夜配色),全用强调色等于把"这是什么帧"这条信息抹掉。
#: `night:<日期>` 走夜次调色板,那是同一夜的所有东西共用的颜色。
_BADGE_TONES = {"light": "good", "dark": None, "bias": None, "flat": "warn",
                "filter": "warn", "seq": "accent", "type": None}


def _header_lines(fits) -> list[str]:
    """原始 FITS 头卡片(每行一张卡)。没有头就是空表 —— 按钮跟着不出现。

    **读的是 `order`,不是 `cards`。** `FitsHeader.cards` 是
    ``dict[str, str]`` —— 遍历 dict 拿到的是**键**,于是整个对话框只剩
    ``SIMPLE / BITPIX / NAXIS / …`` 一列光秃秃的关键字,值全没了。
    而 `isinstance(c, (tuple, list))` 那一支永远走不到,是死代码。
    对话框照弹、不报错 —— 独立验收 1.d14 抓到的就是这个。
    `order` 是 ``[(key, value, comment)]``,顺序也是文件里的原始顺序。
    """
    order = getattr(fits, "order", None) if fits is not None else None
    if not order:
        return []
    return [f"{str(k):<8}= {v}" for k, v, _comment in order]


def _badge_tone(kind: str) -> str | None:
    kind = str(kind or "")
    if kind.startswith("night:"):
        return "accent"
    return _BADGE_TONES.get(kind)


class BrowserPage(Page):
    TITLE = N_("浏览")
    SUBTITLE = N_("设备上的共享、目录与影像文件")

    #: 预览结果从 PreviewWorker 的线程上抛过来,靠这个信号回主线程
    preview_ready = Signal(object)

    def __init__(self, shell):
        super().__init__(shell)
        self.share = ""
        self.path = ""
        self.entries: list = []
        self.colors: dict[str, int] = {}
        self.selected = ""
        self.sort_index = 0
        self.multi = False
        self._token = 0
        self._search: CancelToken | None = None
        self._detail_entry = None
        self._last_detail: dict = {}
        # FITS 头副行缓存,键 (share, path, size, mtime) —— 回到同一个
        # 目录不重读。
        self._hdr_cache: dict = {}
        self._preview_size = None
        self._build()
        self.preview_ready.connect(self._on_preview)
        # 日志读完 → 站点经度从兜底的 120° 变成反推的 121.44°,方位角与
        # 迷你雷达都要跟着重算。不接的话已经渲染出来的那张详情会一直显示
        # 那个**看起来很正常的错数字**。
        shell.logs_ready.connect(self._on_logs_ready)

    # ------------------------------------------------------------ 界面

    def _build(self) -> None:
        root = page_layout(self)
        self.header = PageHeader(_(self.TITLE), _(self.SUBTITLE))
        self.count_chip = W.StatusChip("", "accent")
        self.header.add_action(self.count_chip)
        root.addWidget(self.header)

        root.addWidget(self._toolbar())

        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        split.addWidget(self._shares_card())
        split.addWidget(self._list_card())
        split.addWidget(self._detail_card())
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setStretchFactor(2, 0)
        split.setSizes([170, 720, 380])
        root.addWidget(split, 1)

    def _toolbar(self) -> QWidget:
        """工具栏**两行**。十一个控件挤一行放不下,右边那几个会被窗口边界
        直接截掉(另外两套前端真机上"上传文件夹"和"多选"就是这么没的)。"""
        card = W.Card()
        nav = W.hbox(gap="sm")
        self.up_btn = W.button(_("上一级"), on_click=self._go_up)
        nav.addWidget(self.up_btn)
        nav.addWidget(W.button(_("刷新"), on_click=self.reload))
        self.crumb = W.label("", role="dim", wrap=True)
        nav.addWidget(self.crumb, 1)
        card.add_layout(nav)

        act = W.hbox(gap="sm")
        act.addWidget(W.combo([_(s) for s in SORTS],
                              on_change=self._set_sort))
        self.search_box = W.line_edit(_("搜索(支持 * ?)"), on_return=self._do_search)
        self.search_box.setMinimumWidth(200)
        act.addWidget(self.search_box)
        act.addWidget(W.button(_("搜索"), on_click=self._do_search))
        self.stop_btn = W.button(_("停止"), on_click=self._stop_search, enabled=False)
        act.addWidget(self.stop_btn)
        act.addWidget(W.button(_("新建目录"), on_click=self._mkdir))
        # 老 UI 工具栏有这个:把当前目录带到空间分析页。没有它的话
        # 用户要自己去那一页再选一遍共享、再点一次扫描。
        act.addWidget(W.button(_("分析占用"), on_click=self._analyze))
        act.addWidget(W.button(_("上传文件"), on_click=lambda: self._upload(False)))
        act.addWidget(W.button(_("上传文件夹"), on_click=lambda: self._upload(True)))
        act.addWidget(W.check(_("勾选模式"), on_change=self._set_multi))
        self.dl_sel_btn = W.button(_("下载所选"), kind="primary",
                                   on_click=self._download_selection,
                                   enabled=False)
        act.addWidget(self.dl_sel_btn)
        act.addStretch(1)
        card.add_layout(act)
        return card

    def _shares_card(self) -> QWidget:
        card = W.Card(_("共享"), _("设备上的共享目录"))
        self.share_list = QListWidget()
        # **`currentTextChanged` 在"点的就是当前项"时不发。** 老 UI 点共享
        # 一律跳回共享根,而这边点已选中的那个共享**毫无反应**(面包屑纹丝
        # 不动)。补一个 `itemClicked` —— 它每次点击都发。
        self.share_list.currentTextChanged.connect(self._on_share_pick)
        self.share_list.itemClicked.connect(
            lambda it: self._on_share_pick(it.text()))
        card.add(self.share_list, 1)
        # **量条 + 百分比**,不是光一行字。老 UI 这里是
        # `ProgressBar` + 「3.12 TB / 3.72 TB 已用 84% · 空闲 622 GB」——
        # "还剩多少"是这一栏唯一要回答的问题,而一串绝对数字要心算才知道
        # 快满了没有(独立验收 1.13 记在案的差异)。
        self.volume_bar = W.Gauge(0.0)
        card.add(self.volume_bar)
        self.volume_label = W.label("", role="subtitle", wrap=True)
        card.add(self.volume_label)
        return card

    def _list_card(self) -> QWidget:
        card = W.Card(_("文件"), "")
        self._list_card_ref = card
        self.table = W.DataTable(bv.ROW_COLS, multi=True)
        self.table.key_selected.connect(self._on_pick)
        self.table.key_activated.connect(self._on_activate)
        self.table.keys_checked.connect(self._on_checked)
        # 右键菜单。**作用于指针下那一行**,不是当前选中那一行。
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        self.state = W.StateStack(self.table)
        card.add(self.state, 1)
        self.status_label = W.label("", role="subtitle", wrap=True)
        card.add(self.status_label)
        return card

    def _detail_card(self) -> QWidget:
        card = W.Card(_("详情"), _("选中一个文件查看拍摄参数"))
        self.detail_scroll = W.Scroll(gap="sm")
        self.detail_scroll.setMinimumWidth(320)
        card.add(self.detail_scroll, 1)
        self._render_detail(None)
        return card

    # ------------------------------------------------------------ 契约

    def on_connected(self, shares: list[str]) -> None:
        self.share_list.blockSignals(True)
        self.share_list.clear()
        self.share_list.addItems(shares)
        self.share_list.blockSignals(False)
        if shares and not self.share:
            self.open_path(shares[0], "")

    def on_show(self) -> None:
        if not self.entries and self.share:
            self.reload()

    def on_close(self) -> None:
        self._stop_search()

    # ------------------------------------------------------------ 列目录

    def open_path(self, share: str, path: str = "") -> None:
        self.share = share
        self.path = path
        self._sync_share_list()
        self.reload()

    def reload(self) -> None:
        if not self.share or self.shell.client_factory is None:
            self.state.show_empty(
                _("还没有连接设备"),
                _("顶部选一台设备点「连接」;没有记录过的设备去「扫描设备」页找。"))
            return
        gen = self.bg.bump()
        self._stop_search()
        share, path = self.share, self.path
        factory = self.shell.client_factory
        self.state.show_busy(_('正在读取 {share}\\{path}').format(
            share=share, path=path) if path
                             else _("正在读取 {share}").format(share=share))
        self._render_crumb()

        def work():
            return with_client(factory, lambda c: c.listdir(share, path))

        self.bg.run(work, gen=gen, on_done=self._apply_entries,
                    on_error=lambda e: self._fail(e, _("列目录")))
        self._load_volume(share, factory, gen)

    def _load_volume(self, share: str, factory, gen: int) -> None:
        def work():
            return with_client(factory, lambda c: c.volume_info(share))

        def done(vol):
            if vol is None:
                self.volume_label.setText(_("容量未知"))
                self.volume_bar.set_frac(0.0)
                return
            # 判据照老 UI:>90% 红、>75% 琥珀 —— 卡快满了要先看见
            pct = vol.percent
            self.volume_bar.set_frac(
                pct / 100.0,
                tone="bad" if pct >= 90 else "warn" if pct >= 75 else None)
            self.volume_label.setText(
                _("{0} / {1} 已用 {pct:.0f}% · 空闲 {2}").format(
                    human_size(vol.used), human_size(vol.total), human_size(vol.free), pct=pct))

        self.bg.run(work, gen=gen, on_done=done, on_error=lambda _e: None)

    def _apply_entries(self, entries) -> None:
        # **换目录先把上一份选中扔掉。** 不清的话:「下载所选(1)」还亮着但
        # 那个键在新目录里根本不存在,点了 `_download_selection` 找不到条目、
        # 既不提示也不入队 —— 就是"点了没反应";而右边详情面板还停在上一个
        # 目录那张 light 帧的判读卡上,像是"进错了目录"。独立验收抓到的两条。
        self.selected = ""
        self._detail_entry = None
        # **在途预览也要作废。** 只清面板不够:上一个目录那张图的预览结果
        # 迟到几百毫秒回来,令牌还对得上,于是把详情**又画回去**了 ——
        # 左边已经是新目录,右边还是上一个目录那张 light 帧的判读卡。
        self._token += 1
        self._preview_size = None
        self.table.clearSelection()
        self._on_checked([])
        self._render_detail(None)
        self.entries = list(entries)
        self._render_rows()
        self._start_child_counts()
        self._start_fits_meta()

    def _render_rows(self) -> None:
        ordered = sorted_entries(list(self.entries), self.sort_index)
        self.colors = bv.night_colors(ordered)
        self._ordered = ordered
        rows = [{"key": bv.entry_key(e), "cells": self._cells(e)}
                for e in ordered]
        if not rows:
            self.state.show_empty(
                _("这个目录是空的"),
                _("设备上确实没有条目 —— 不是还没读完(读取已经结束)。"))
            self.count_chip.set("0", "accent")
            return
        self.state.show_content()
        self.table.set_rows(rows)
        ndir = sum(1 for e in ordered if e.is_dir)
        nfile = len(ordered) - ndir
        total = sum(e.size for e in ordered if not e.is_dir)
        self.status_label.setText(
            _("{nfile} 个文件({0}) · {ndir} 个目录").format(
                human_size(total), nfile=nfile, ndir=ndir))
        self.count_chip.set(str(len(ordered)), "accent")
        if self.selected and self.selected in self.table.keys() and not self.multi:
            self.table.select_key(self.selected)

    def _cells(self, entry) -> list[dict]:
        """五列:符号 · 夜次徽章 · 名字(+副行) · 大小 · 时间。

        列的**含义**来自 ``views.browser``(``ROW_COLS`` / ``entry_symbol`` /
        ``night_chip`` / ``astro_subline``);颜色由主题决定 —— 视图模型给的
        夜次配色是为浅色主题调的淡底深字,贴到近黑卡片上会炸眼。
        """
        from astro_smb.util import format_mtime

        chip = bv.night_chip(entry, self.colors)
        chip_cell = W.cell("")
        if chip is not None:
            text, idx = chip
            bg, fg = theme.night_pair(idx)
            chip_cell = W.cell(text, chip=(bg, fg), size=theme.Font.TINY)
        sub = None if entry.is_dir else bv.astro_subline(entry)
        return [
            W.cell(bv.entry_symbol(entry), dim=True, glyph=True),
            chip_cell,
            W.cell(entry.name, sub=sub,
                   weight="semibold" if entry.is_dir else None,
                   tip=entry.name if len(entry.name) > 30 else None),
            W.cell(_("… 项") if entry.is_dir else human_size(entry.size),
                   align="right", dim=True, size=theme.Font.SMALL),
            W.cell(format_mtime(entry.mtime), dim=True, size=theme.Font.SMALL),
        ]

    def _start_child_counts(self) -> None:
        """目录行的"… 项" → 真数字。**装饰性的**,失败就保持原样。"""
        dirs = [e for e in self.entries if e.is_dir][:CHILD_COUNT_MAX]
        if not dirs or self.shell.client_factory is None:
            return
        gen = self.bg.generation
        factory = self.shell.client_factory

        def work(report):
            def run(client):
                for entry in dirs:
                    if self.bg.stale(gen):
                        return None      # 换目录了,别白算
                    got = client.count_children(entry.share, entry.path)
                    if got is not None:
                        report((bv.entry_key(entry), got))
                return None
            return with_client(factory, run)

        self.bg.run(work, gen=gen, on_progress=self._apply_count,
                    on_error=lambda _e: None)

    def _apply_count(self, payload) -> None:
        key, (ndir, nfile) = payload
        self.table.update_cell(key, SIZE_COL, text=bv.child_count_text(ndir, nfile))

    # ------------------------------------------------------------ 排序 / 搜索

    def _set_sort(self, idx: int) -> None:
        self.sort_index = idx
        self._render_rows()          # **换排序不走网络**

    def _do_search(self) -> None:
        query = self.search_box.text().strip()
        if not query:
            self.reload()
            return
        if self.shell.client_factory is None:
            return
        gen = self.bg.bump()
        token = CancelToken()
        self._search = token
        self.stop_btn.setEnabled(True)
        pattern = query if ("*" in query or "?" in query) else f"*{query}*"
        share, path, factory = self.share, self.path, self.shell.client_factory
        self.state.show_busy(_("正在搜索 {pattern}").format(pattern=pattern))

        def work():
            def run(client):
                out = []
                for e in client.find(share, path, pattern, include_dirs=True,
                                     cancel=token.event):
                    out.append(e)
                    if len(out) >= bv.RENDER_CAP:
                        break
                return out
            return with_client(factory, run)

        def done(found):
            self.stop_btn.setEnabled(False)
            self._search = None
            self.entries = found
            self._render_rows()
            if not found:
                self.state.show_empty(
                    _("没有匹配 {pattern} 的条目").format(pattern=pattern),
                    _("搜索已经跑完 —— 这个目录树下确实没有。换个关键字或换个起点。"))
            else:
                self.status_label.setText(_("搜索 {pattern}:{0} 项").format(
                    len(found), pattern=pattern)
                                          + (_(" (已达上限)") if len(found) >= bv.RENDER_CAP
                                             else ""))

        self.bg.run(work, gen=gen, on_done=done,
                    on_error=lambda e: self._fail(e, _("搜索")))

    def _stop_search(self) -> None:
        if self._search is not None:
            self._search.cancel()
        self.stop_btn.setEnabled(False)

    # ------------------------------------------------------------ 选中 / 预览

    def _set_multi(self, on: bool) -> None:
        self.multi = on
        # **「下载所选」不随勾选模式隐藏。** 不开勾选模式也能 ctrl / shift 多选
        # (老 UI 的 `Extended`),那时候一样要有地方把选中的一批丢进队列。
        self.table.set_check_mode(on)
        self.dl_sel_btn.setEnabled(False)
        self._on_checked([])

    def _on_checked(self, keys: list[str]) -> None:
        self.dl_sel_btn.setText(_("下载所选({0})").format(len(keys)))
        self.dl_sel_btn.setEnabled(bool(keys))

    def _entry_for(self, key: str):
        for e in self.entries:
            if bv.entry_key(e) == key:
                return e
        return None

    def _on_pick(self, key: str) -> None:
        self.selected = key
        entry = self._entry_for(key)
        self._detail_entry = entry
        if entry is None or entry.is_dir:
            self._render_detail(None)
            return
        self._render_detail(self._detail_model(entry, None))
        if self.shell.preview is not None:
            self._token += 1
            self.shell.preview.request(self._token, entry)

    def _on_activate(self, key: str) -> None:
        entry = self._entry_for(key)
        if entry is None:
            return
        if entry.is_dir:
            self.open_path(entry.share, entry.path)
        elif os.path.splitext(entry.name)[1].lower() in FITS_EXTS:
            self.shell.open_fits(entry.share, entry.path)

    def _on_preview(self, result) -> None:
        """**令牌对不上就丢** —— 浏览时快速换选会排一队几十兆的下载。"""
        if result.token != self._token or self._detail_entry is None:
            return
        # **尺寸的来源是这里。** 原来写的是
        # `getattr(fits, "_image_size", None) or getattr(self, "_preview_size", None)`
        # —— `FitsHeader` 全仓库没有 `_image_size`,`_preview_size` 也从没被赋过,
        # 于是那一行恒为 None,详情里「尺寸 6248 × 4176」整行不存在。
        self._preview_size = getattr(result, "image_size", None)
        model = self._detail_model(self._detail_entry, result.fits)
        model.update(preview=result.thumb_path, preview_text=result.text,
                     preview_note=result.thumb_source or result.error,
                     can_load_full=result.can_load_full)
        self._render_detail(model)

    def _load_full(self) -> None:
        if self._detail_entry is None or self.shell.preview is None:
            return
        self._token += 1
        self.shell.preview.request(self._token, self._detail_entry,
                                   want_full=True)

    # ------------------------------------------------------------ 详情

    def _detail_model(self, entry, fits) -> dict:
        """判读一律走共享层 —— 气量/高度角/采样的阈值只有一份。"""
        from astro_smb_app.logstore import load_site

        site = None
        try:
            data = self.shell.logstore.data
            site = bv.site_latlon(load_site(), getattr(data, "lon_estimate", None))
        except Exception:                    # noqa: BLE001 - 站点拿不到不该毁掉整张卡
            site = None
        title, sub, groups, badges, sky, pills = bv._astro_details(
            entry, fits, site)
        return {
            "name": entry.name, "title": title, "sub": sub,
            # 徽章/胶囊都带第二个元素:徽章是**种类**(light/filter/seq/night:日期),
            # 胶囊是**注解**(老 UI 拿它当 tooltip)。原来两边都只取了 [0],
            # 于是徽章全成一个色、胶囊的注解整条丢掉。
            "badges": list(badges),
            "pills": list(pills),
            "rows": _flat_rows(groups),
            "sky": sky,
            "is_fits": os.path.splitext(entry.name)[1].lower() in FITS_EXTS,
            # 「文件」「位置」两组。这两组原来整个没有 —— 类型/大小/**尺寸**/
            # 修改/创建/属性,以及完整 UNC 路径,全都不显示。
            "file_rows": _flat_rows(bv.file_groups(
                entry, host=self._host(),
                image_size=self._preview_size,
                extra=getattr(self, "_preview_extra", ()))),
            "copy_text": bv._detail_text(
                entry, title, sub, badges, pills,
                list(groups) + bv.file_groups(entry, host=self._host())),
            "header_lines": _header_lines(fits),
        }

    def _host(self) -> str:
        return str(self.shell.conn.get("host") or "")

    def on_theme(self) -> None:
        """详情里那张迷你雷达是显示列表 —— 用**上一次的模型**重铺一遍。

        重新走一次 `_on_pick` 会再发一轮预览请求(白跑一次网络 I/O),
        而详情模型本身跟配色无关,`_last_detail` 就够了。
        """
        if self._last_detail:
            self._render_detail(self._last_detail)

    def _render_detail(self, model: dict | None) -> None:
        self._last_detail = model or {}
        self.detail_scroll.clear()
        body = self.detail_scroll.body
        if model is None:
            body.addWidget(W.label(_("选中一个文件查看详情"), role="dim", wrap=True))
            body.addStretch(1)
            return
        # 文件名**必须整条看得见**。ASIAIR 的名字有六七十个字符,截断之后
        # 序号和角度都没了,而那正是区分同一目标几十张 sub 的唯一信息。
        name_lb = W.label(W.breakable(model["name"]), role="title", wrap=True)
        name_lb.setMinimumWidth(1)      # 不给的话它按最长单词要宽度,反而不换行
        body.addWidget(name_lb)

        # **缩略图放最上面**:ASIAIR 的 _thn.jpg 只有 18 KB 而原图 49.77 MB,
        # 永远优先拉它。大图只有用户点了「生成预览」才下载。
        if model.get("preview"):
            img = W.ImageView(300, 200)
            if img.set_path(model["preview"]):
                body.addWidget(img)
                # 图的来源注在下面那行 `preview_note` 里(`thumb_source`),
                # 这里**不要再写一句** —— 会变成两行一模一样的"ASIAIR 缩略图"。
        if model.get("preview_text"):
            body.addWidget(W.label(model["preview_text"][:2000], role="mono",
                                   wrap=True))
        if model.get("preview_note"):
            body.addWidget(W.label(str(model["preview_note"]), role="faint",
                                   wrap=True))
        if model.get("can_load_full"):
            body.addWidget(W.button(_("生成预览(下载原图)"), on_click=self._load_full))
        # 老 UI 把「在 FITS 查看器中打开」放在缩略图正下方 —— 看完预览图想看
        # 大图是同一个动作的两步,按钮该挨着;塞到最底下那一排要先滚过四组参数。
        if model.get("is_fits"):
            body.addWidget(W.button(_("在影像查看中打开"), on_click=self._open_fits))

        if model.get("badges"):
            row = W.hbox(gap="xs")
            for text, kind in model["badges"]:
                row.addWidget(W.StatusChip(str(text), _badge_tone(kind)))
            row.addStretch(1)
            body.addLayout(row)
        if model.get("title"):
            body.addWidget(W.label(model["title"], role="title", wrap=True))
        if model.get("sub"):
            body.addWidget(W.label(str(model["sub"]), role="subtitle", wrap=True))
        if model.get("pills"):
            # 胶囊而不是一行 `·` 拼串:曝光/增益/温度是三个独立读数,
            # 而且各自带注解(温度那枚是"目标 0℃ · 偏离 +0.0℃")。
            row = W.hbox(gap="xs")
            for item in model["pills"]:
                text = str(item[0])
                note = str(item[1]) if len(item) > 1 else ""
                tone = item[2] if len(item) > 2 else None
                chip = W.StatusChip(text, tone)
                if note:
                    chip.setToolTip(note)
                row.addWidget(chip)
            row.addStretch(1)
            body.addLayout(row)

        group = ""
        for r in model.get("rows") or ():
            if r["group"] != group:
                group = r["group"]
                body.addWidget(W.GroupHeader(group, r.get("glyph", "")))
            body.addWidget(W.MetricRow(r["key"], r["value"], tone=r["tone"],
                                       mono=r["mono"]))
            bar = r.get("bar")
            if bar and bar[0] == "altbar":
                # 高度角量条:0°–90°。地平线下的负值夹到 0,但**数字照显示**
                # —— 那多半是站点纬度没设对,把线索藏掉不如让它刺眼。
                #
                # 刻度不写度数,用线稿图标(地平线/雾/星/天顶)—— 度数在上面
                # 那行已经写了一遍,再沿着条子重复四个数字只是噪声;而图标
                # 直接说的是"这个高度意味着什么"。
                g = W.Gauge(max(0.0, float(bar[1])) / 90.0, tone=r["tone"],
                            ticks=W.ALT_TICKS, span=90.0)
                g.setToolTip(g.tick_tooltip())
                body.addWidget(g)

        if model.get("sky"):
            body.addWidget(W.GroupHeader(_("全天位置"), W.GLYPH_SKY))
            body.addWidget(self._radar(model["sky"]))

        group = ""
        for r in model.get("file_rows") or ():
            if r["group"] != group:
                group = r["group"]
                body.addWidget(W.GroupHeader(group, r.get("glyph", "")))
            # 路径是一整条没有空格的长串 —— 不插换行机会就会被右边界截掉,
            # 而 UNC 路径的价值就在于能整条复制/核对。
            body.addWidget(W.MetricRow(r["key"], W.breakable(r["value"]),
                                       mono=r["mono"]))

        act = W.hbox(gap="sm")
        act.addWidget(W.button(_("下载"), kind="primary", on_click=self._download))
        # 「影像查看」已经在缩略图下面了(老 UI 的位置),这里不再重复一个
        act.addWidget(W.button(_("复制路径"), on_click=self._copy_path))
        act.addStretch(1)
        body.addLayout(act)
        act2 = W.hbox(gap="sm")
        act2.addWidget(W.button(_("复制全部信息"), on_click=self._copy_detail))
        if model.get("header_lines"):
            act2.addWidget(W.button(_("完整 FITS 头"), on_click=self._show_header))
        act2.addStretch(1)
        body.addLayout(act2)
        act3 = W.hbox(gap="sm")
        act3.addWidget(W.button(_("重命名"), on_click=self._rename))
        act3.addWidget(W.button(_("删除"), kind="danger", on_click=self._delete))
        act3.addStretch(1)
        body.addLayout(act3)
        body.addStretch(1)

    def _radar(self, sky) -> QWidget:
        """迷你天球雷达。投影走 ``views.skychart`` —— 和拍摄记录页那张大图
        是同一份公式。**地平线下的点照画不误**:那多半是站点纬度没设对,
        把点藏掉等于把线索藏掉。"""
        _ra, _dec, _ts, alt, az = sky
        canvas = W.OpsCanvas(RADAR_SIZE, RADAR_SIZE)
        canvas.setFixedWidth(RADAR_SIZE)
        # margin 要留够,否则「东」会被左边界**静默切掉**(记录页
        # `SKY_MARGIN` 那条注释记的就是这个坑,浏览页这张漏了)。
        ops = skychart.frame_ops(float(RADAR_SIZE), margin=RADAR_MARGIN)
        ops += skychart.point_ops([{"alt": alt, "az": az}], float(RADAR_SIZE),
                                  margin=RADAR_MARGIN,
                                  radius=5.0, skip_below=False,
                                  default_fill=theme.C.ACCENT,
                                  ring=theme.C.ACCENT)
        canvas.set_ops(ops)
        # 「高度 35° · 方位 南」这一句老 UI 有,这边原来整个没有 ——
        # 而雷达图本身只给"点在哪个方向"的直觉,**具体几度只有这一行能读**。
        # 方位名走共享层的 `_az_name`(16 向,22.5° 就近取整),不自己再排一份。
        wrap = QWidget()
        col = W.vbox(wrap, gap="xs")
        col.addWidget(canvas)
        col.addWidget(W.label(_("高度 {alt:.0f}° · 方位 {0}").format(bv._az_name(az), alt=alt),
                              role="subtitle"))
        wrap.setFixedWidth(RADAR_SIZE)
        return wrap

    # ------------------------------------------------------------ 动作

    def _sync_share_list(self) -> None:
        items = [self.share_list.item(i).text()
                 for i in range(self.share_list.count())]
        if self.share in items:
            self.share_list.blockSignals(True)
            self.share_list.setCurrentRow(items.index(self.share))
            self.share_list.blockSignals(False)

    def _on_logs_ready(self) -> None:
        if self._detail_entry is not None:
            self._on_pick(bv.entry_key(self._detail_entry))

    def _on_share_pick(self, name: str) -> None:
        """点共享**一律回到它的根**,哪怕点的就是当前那个。

        原来的守卫是 `name != self.share` —— 于是在
        `EMMC Images/Plan/Light/IC 4603` 里点「EMMC Images」毫无反应
        (同一共享的深层路径永远被这条挡掉)。老 UI 点共享就是跳根。
        """
        if name:
            self.open_path(name, "")

    def _render_crumb(self) -> None:
        shown = self.path.replace("\\", " / ")
        self.crumb.setText(f"{self.share} / {shown}" if shown else self.share)
        self.up_btn.setEnabled(bool(self.path))
        self._list_card_ref.set_subtitle(shown or _("共享根目录"))

    def _start_fits_meta(self) -> None:
        """后台逐个**部分读取** FITS 头(几 KB/个),把滤镜/序号/增益/温度补进副行。

        这一步原来整个没有,于是列表副行只有「目标 · 类型 · 曝光」——
        老 UI 那边是「IC 4603 · 300s · 4C · #0001 · 增益100 · 0.0℃」。少掉的
        正是**区分同一目标不同批次**要看的那几个:滤镜槽位、序号、增益、温度。

        判读与拼串全在共享层(`views.browser._hdr_suffix`),这里只负责调度:

        * 与列目录**共用世代计数器** —— 视图一变这一轮自然作废,不会把 A 目录
          的增益填到 B 目录的行上;
        * 结果按 `(share, path, size, mtime)` 进内存缓存,回到同一个目录不重读;
        * 连续三次失败就停 —— 那多半是连接断了,继续读只会堆一串超时。
        """
        fits = [e for e in self._ordered
                if not e.is_dir
                and os.path.splitext(e.name)[1].lower() in FITS_EXTS][:500]
        if not fits or self.shell.client_factory is None:
            return
        gen = self.bg.generation
        factory = self.shell.client_factory
        cache = self._hdr_cache

        def work():
            from astro_smb.client import SmbClientError
            from astro_smb.naming import parse_image_name
            from astro_smb_app.preview import read_fits_header

            out: list[tuple[str, str]] = []
            client = factory()
            try:
                fails = 0
                for e in fits:
                    if gen != self.bg.generation:
                        return out
                    key = (e.share, e.path, e.size, e.mtime)
                    suffix = cache.get(key)
                    if suffix is None:
                        try:
                            hdr = read_fits_header(client, e)
                        except SmbClientError:
                            fails += 1
                            if fails >= 3:
                                return out
                            continue
                        fails = 0
                        suffix = bv._hdr_suffix(hdr, parse_image_name(e.name))
                        cache[key] = suffix
                    if suffix:
                        out.append((bv.entry_key(e), suffix))
            finally:
                client.close()
            return out

        self.bg.run(work, gen=gen, on_done=self._apply_fits_meta,
                    on_error=lambda _e: None)

    def _apply_fits_meta(self, pairs) -> None:
        for key, suffix in pairs or ():
            entry = self._entry_for(key)
            if entry is None:
                continue
            base = bv.astro_subline(entry) or ""
            self.table.update_cell(key, bv.NAME_COL,
                                   sub=f"{base} · {suffix}" if base else suffix)

    def _analyze(self) -> None:
        """把当前目录带到空间分析页并直接开扫。"""
        page = self.shell.page("space")
        page.share, page.path, page.crumbs = self.share, self.path, []
        self.shell.select_page("space")
        page.rescan()

    def _go_up(self) -> None:
        if not self.path:
            return
        self.open_path(self.share, self.path.rsplit("\\", 1)[0]
                       if "\\" in self.path else "")

    def _download(self) -> None:
        entry = self._detail_entry
        if entry is None:
            return
        if entry.is_dir:
            self._download_dir(entry)
            return
        self._submit(entry)
        self.shell.select_page("transfers")

    def _download_selection(self) -> None:
        keys = self.table.checked_keys()
        n = 0
        for key in keys:
            entry = self._entry_for(key)
            if entry is not None and not entry.is_dir:
                self._submit(entry, group=self.path or self.share)
                n += 1
        if n:
            self.shell.select_page("transfers")

    def _download_dir(self, entry, dest: Path | None = None) -> None:
        """整夹下载:**后台 walk 展开成逐文件入队**(老 UI §7.8 同款)。

        为什么不直接 `submit_download_dir`:那样整个文件夹是**一个**任务,
        拿不到文件内分块并发,也没有逐文件的进度与方块图 —— 老 UI 实测
        按文件展开之后整夹 18 MB/s(3 文件 × 8 块并发)。

        展开本身要走一遍 `walk`,几百次 listdir,所以放工作线程;
        每个文件带上 `group=<目录名>`,传输页按它分组折叠。
        """
        factory = self.shell.client_factory
        if factory is None:
            self.shell.notice(_("先连接设备"))
            return
        root = dest or Path.home() / "Downloads"
        share, path, name = entry.share, entry.path, entry.name
        self.shell.notice(_("正在展开「{name}」…").format(name=name))

        def work():
            def run(client):
                out = []
                for cur, _dirs, files in client.walk(share, path):
                    for f in files:
                        rel = f.path[len(path):].lstrip("\\")
                        out.append((f, root / name / rel))
                return out

            return with_client(factory, run)

        def done(items):
            if not items:
                self.shell.notice(_("「{name}」里没有文件").format(name=name))
                return
            for f, local in items:
                local.parent.mkdir(parents=True, exist_ok=True)
                self.shell.transfers.submit_download(
                    f.share, f.path, local, f.name, f.size, group=name)
            self.shell.notice(_("已把「{name}」的 {0} 个文件加入队列").format(len(items), name=name))
            self.shell.select_page("transfers")

        self.bg.run(work, on_done=done,
                    on_error=lambda e: self.report(e, _("展开文件夹")))

    def _submit(self, entry, group: str | None = None,
                dest: Path | None = None) -> None:
        local = (dest or Path.home() / "Downloads") / entry.name
        self.shell.transfers.submit_download(
            entry.share, entry.path, local, entry.name, entry.size,
            group=group)

    # ------------------------------------------------------------ 右键菜单

    def _context_menu(self, pos) -> None:
        """行右键菜单 —— 老 UI 有 7 项,这边原来一项都没有。

        **两项在别处根本没有入口**:「下载到…」(固定下到 `~/Downloads` 之外
        的地方)和「复制到剪贴板」(粘贴到资源管理器)。所以"工具栏按钮已经
        覆盖同样的动作"这个理由只成立 5 项。

        菜单作用于**指针下那一行**,不是"当前选中的那一行" —— 右键第 5 行而
        第 1 行是选中的,拿选中那行去删就是**删错文件**(另一套前端为这条
        专门修过一次)。
        """
        from PySide6.QtWidgets import QMenu

        index = self.table.indexAt(pos)
        if index.isValid():
            key = self.table.keys()[index.row()]
            if key != self.selected:
                self.table.select_key(key)
                self._on_pick(key)
        entry = self._detail_entry
        if entry is None:
            return
        menu = QMenu(self)
        is_fits = os.path.splitext(entry.name)[1].lower() in FITS_EXTS
        if is_fits:
            menu.addAction(_("在影像查看中打开"), self._open_fits)
        menu.addAction(_("下载"), self._download)
        menu.addAction(_("下载到…"), self._download_to)
        menu.addAction(_("复制到剪贴板(可粘贴到资源管理器)"), self._copy_to_clipboard)
        menu.addSeparator()
        menu.addAction(_("重命名…"), self._rename)
        menu.addAction(_("删除"), self._delete)
        menu.addSeparator()
        menu.addAction(_("复制 UNC 路径"), self._copy_path)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _download_to(self) -> None:
        entry = self._detail_entry
        if entry is None or entry.is_dir:
            return
        folder = QFileDialog.getExistingDirectory(self, _("下载到哪个目录"))
        if not folder:
            return
        self._submit(entry, dest=Path(folder))
        self.shell.select_page("transfers")

    def _copy_to_clipboard(self) -> None:
        """先下到暂存目录,再把**真实文件**放进剪贴板。

        资源管理器只认真实文件路径 —— 这也是老 UI 的兜底方案(虚拟文件拖出
        是 Windows 专属的 COM 活儿,跨平台没有等价物)。下载是异步的,
        所以放进剪贴板要等它落盘。
        """
        entry = self._detail_entry
        if entry is None or entry.is_dir:
            return
        from astro_smb_app.paths import cache_root

        dest = cache_root() / "clipboard"
        dest.mkdir(parents=True, exist_ok=True)
        local = dest / entry.name
        gen = self.bg.bump()

        def work():
            return with_client(
                self.shell.client_factory,
                lambda c: (c.download_file(entry.share, entry.path, local),
                           local)[1])

        def done(path):
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QGuiApplication
            from PySide6.QtCore import QMimeData

            md = QMimeData()
            md.setUrls([QUrl.fromLocalFile(str(path))])
            QGuiApplication.clipboard().setMimeData(md)
            self.shell.notice(_("{name} 已复制到剪贴板 —— 到资源管理器里 Ctrl+V").format(
                name=entry.name), "ok")

        self.shell.notice(_("正在取 {name}…").format(name=entry.name), "accent")
        self.bg.run(work, gen=gen, on_done=done,
                    on_error=lambda e: self.report(e, _("复制到剪贴板")))

    def _open_fits(self) -> None:
        if self._detail_entry is not None:
            self.shell.open_fits(self._detail_entry.share,
                                 self._detail_entry.path)

    def _copy_path(self) -> None:
        from PySide6.QtWidgets import QApplication

        if self._detail_entry is None:
            return
        QApplication.clipboard().setText(
            f"\\\\{self.share}\\{self._detail_entry.path}")
        self.shell.notice(_("路径已复制到剪贴板"), "accent")

    def _upload(self, folder: bool) -> None:
        if self.shell.client_factory is None:
            return
        if folder:
            picked = QFileDialog.getExistingDirectory(self, _("选择要上传的文件夹"))
            locals_ = [picked] if picked else []
        else:
            locals_, _filter = QFileDialog.getOpenFileNames(self, _("选择要上传的文件"))
        if not locals_:
            return
        share, base, factory = self.share, self.path, self.shell.client_factory

        def work():
            def run(client):
                for src in locals_:
                    p = Path(src)
                    target = f"{base}\\{p.name}" if base else p.name
                    if p.is_dir():
                        client.upload_dir(p, share, target)
                    else:
                        client.upload_file(p, share, target)
                return len(locals_)
            return with_client(factory, run)

        self.bg.run(work, on_done=lambda n: (self.shell.notice(
            _("已上传 {n} 项").format(n=n), "ok"), self.reload()),
            on_error=lambda e: self._fail(e, _("上传")))

    def _mkdir(self) -> None:
        name = self.ask_text(_("新建目录"), _("目录名"), ok_text=_("创建"))
        if not name:
            return
        target = f"{self.path}\\{name}" if self.path else name
        self._write(lambda c: c.mkdir(self.share, target), _("新建目录"))

    def _rename(self) -> None:
        entry = self._detail_entry
        if entry is None:
            return
        name = self.ask_text(_("重命名"), _("新名字"), text=entry.name,
                             ok_text=_("重命名"))
        if not name or name == entry.name:
            return
        parent = entry.path.rsplit("\\", 1)[0] if "\\" in entry.path else ""
        target = f"{parent}\\{name}" if parent else name
        self._write(lambda c: c.rename(self.share, entry.path, target), _("重命名"))

    def _copy_detail(self) -> None:
        from PySide6.QtWidgets import QApplication

        text = str((self._last_detail or {}).get("copy_text") or "")
        if text:
            QApplication.clipboard().setText(text)
            self.shell.notice(_("详情已复制到剪贴板"), "ok")

    def _show_header(self) -> None:
        lines = (self._last_detail or {}).get("header_lines") or []
        if not lines:
            return
        W.TextDialog(self, _("完整 FITS 头"), "\n".join(lines)).exec()

    def _delete(self) -> None:
        entry = self._detail_entry
        if entry is None:
            return
        # **破坏性操作必须是模态确认**,不能是一段可以无视的文字。
        # 措辞与按钮照老 UI:中文「删除 / 取消」,默认落在**取消**上 ——
        # `QMessageBox.question` 给的是英文 Yes/No,而且一路回车就删了。
        if not self.confirm(
                _("确认删除"),
                _('即将永久删除以下内容,不可恢复:\n\n{name}\n\n共 1 项').format(name=entry.name),
                ok_text=_("删除")):
            return
        if entry.is_dir:
            self._write(lambda c: c.rmdir(self.share, entry.path, recursive=True),
                        _("删除"))
        else:
            self._write(lambda c: c.remove(self.share, entry.path), _("删除"))

    def _write(self, op, what: str) -> None:
        if self.shell.client_factory is None:
            return
        factory = self.shell.client_factory

        def work():
            return with_client(factory, op)

        self.bg.run(work, on_done=lambda _r: (
            self.shell.notice(_("{what}成功").format(what=what), "ok"), self.reload()),
            on_error=lambda e: self._fail(e, what))

    def demo_select(self) -> None:
        """进一个有影像的目录并选中第一张 ``.fit``(``--auto`` 用)。

        不是纯装饰:它把**详情那条最贵的链路**跑一遍 —— 列目录 → 选中 →
        PreviewWorker 拉 ``_thn.jpg``(18 KB,不碰 49.77 MB 原图)→ FITS 头 →
        天文判读卡 → 迷你天球雷达。
        """
        from PySide6.QtCore import QTimer

        if not self.shell.shares:
            return
        self.open_path(self.shell.shares[0], "Plan\\Light\\M 8")

        def pick():
            fits = [k for k in self.table.keys() if k.lower().endswith(".fit")]
            if fits:
                self.table.select_key(fits[0])
                self._on_pick(fits[0])
            elif self.entries:
                self.table.select_key(self.table.keys()[0])
                self._on_pick(self.table.keys()[0])

        QTimer.singleShot(4000, pick)

    def _fail(self, exc: BaseException, what: str) -> None:
        """**把错误显示出来。** 离线镜像上写操作一律抛错,静默吞掉就等于
        用户按了删除、什么都没发生、还以为成功了。"""
        self.state.show_content()
        self.stop_btn.setEnabled(False)
        self.status_label.setText(_("{what}失败: {exc}").format(what=what, exc=exc))
        self.report(exc, what)
