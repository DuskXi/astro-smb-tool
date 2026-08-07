"""导星分析页:段列表(按拍摄目标分组)+ 主曲线 + 七张统计小图。

两条判读上的硬要求(照抄另外两套前端,改坏了不报错、只是结论变了):

* **整体 RMS 按帧数平方加权合并**(``views.guiding._merge_rms``)。简单平均会被
  一段几帧的碎段拖爆 —— 真机上是 1.89″ vs 0.92″,结论从"导星很差"变成"正常"。
* **最佳/最差段有帧数门槛**(``MIN_RANK_FRAMES``),否则三帧的 settle 碎段会
  以离谱的 RMS 霸榜"最差"。

段列表**必须分组折叠**:真机 123 段里 103 段是几帧就结束的短尝试,平铺的话
真正想看的那几段会被埋掉。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QGridLayout, QSlider, QSplitter, QWidget

from astro_smb.i18n import N_, gettext as _
from astro_smb_app.views import guidedash as gd
from astro_smb_app.views import guiding as gv
from astro_smb_qt import models, theme, widgets as W
from astro_smb_qt.pages.base import Page, PageHeader, page_layout
from astro_smb_qt.workers import with_client

CURVE_W, CURVE_H = int(models.CURVE_W), 300
#: 小图一行摆几张。**算的是卡片外沿不是画布宽** —— 每张图外面还有卡片的
#: 内边距与描边,按画布宽算列数会让最右那张顶到边界被**静默**切掉。
CHART_COLS = 3
#: X 轴候选步长(秒):挑第一个能让刻度数落在 ~8 条以内的
#: 曲线左侧刻度栏宽度 —— y 刻度写在这里,不压曲线
Y_AXIS_W = 34.0

_TIME_STEPS = (30, 60, 120, 300, 600, 900, 1800, 3600, 7200)


class GuidingPage(Page):
    TITLE = N_("导星分析")
    SUBTITLE = N_("PHD2 导星日志 —— 这一段拍得稳不稳,是 RA 还是 DEC 出问题")

    def __init__(self, shell):
        super().__init__(shell)
        self.data = None
        self.prep: dict = {}
        self.selected: int | None = None
        self.expanded: set[str] = set()
        self.frag_open: set[str] = set()
        self.window_index = 0
        self.pos = 0.0
        self.highlight: tuple[float, float, str] | None = None
        #: 右栏当前视图:None = 段视图,组 key = 仪表盘视图
        #: (老 UI 是组头右侧那颗「仪表盘」按钮)
        self.dash_key: str | None = None
        self._dash_cache: dict = {}
        self._loading = False
        self._build()

    # ------------------------------------------------------------ 界面

    def _build(self) -> None:
        root = page_layout(self)
        self.header = PageHeader(_(self.TITLE), _(self.SUBTITLE))
        self.header.add_action(W.label(_("窗口"), role="subtitle"))
        self.win_combo = W.combo([_(c[0]) for c in gv.WINDOW_CHOICES],
                                 on_change=self._set_window)
        self.header.add_action(self.win_combo)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 100)
        self.slider.setFixedWidth(180)
        self.slider.valueChanged.connect(self._set_pos)
        self.slider.setEnabled(False)
        self.header.add_action(self.slider)
        self.header.add_action(W.button(_("刷新"), on_click=self.reload))
        root.addWidget(self.header)

        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        split.addWidget(self._left())
        split.addWidget(self._right())
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([380, 860])
        self.state = W.StateStack(split)
        root.addWidget(self.state, 1)
        self.state.show_empty(_("还没有读取导星日志"), _("连接设备后点「刷新」。"))

    def _collapse_all(self) -> None:
        self.expanded.clear()
        self.frag_open.clear()
        self._render()

    def _expand_all(self) -> None:
        for g in (self.prep or {}).get("groups") or ():
            self.expanded.add(g["key"])
        self._render()

    def _left(self) -> QWidget:
        card = W.Card(_("导星段"), _("按拍摄目标分组 · 点组头展开"))
        self.overview = W.label("", role="subtitle", wrap=True)
        card.add(self.overview)
        # 真机 123 段里 103 段是几帧就结束的短尝试 —— 11 组全展开时这一列
        # 长得没边。老 UI 有这两颗按钮 + 一句"几组、折叠了几段"的说明。
        tools = W.hbox(gap="sm")
        tools.addWidget(W.button(_("全部折叠"), on_click=self._collapse_all))
        tools.addWidget(W.button(_("全部展开"), on_click=self._expand_all))
        self.group_note = W.label("", role="faint", wrap=True)
        tools.addWidget(self.group_note, 1)
        card.add_layout(tools)
        card.add(W.Divider())
        self.sections = W.DataTable(["*"])
        self.sections.key_selected.connect(self._pick)
        card.add(self.sections, 1)
        return card

    def _right(self) -> QWidget:
        card = W.Card(_("曲线与统计"), "")
        self.chart_card = card
        self.scroll = W.Scroll(gap="sm")
        card.add(self.scroll, 1)
        return card

    # ------------------------------------------------------------ 契约

    def on_show(self) -> None:
        if self.data is None and not self._loading:
            self.reload()

    def on_connected(self, shares) -> None:
        self.data = None

    # ------------------------------------------------------------ 加载

    def reload(self) -> None:
        if self.shell.client_factory is None:
            self.state.show_empty(
                _("还没有连接设备"),
                _("导星曲线来自设备上的 PHD2_GuideLog_*.txt —— 先连一台设备。"))
            return
        self._loading = True
        gen = self.bg.bump()
        self.state.show_busy(_("正在下载并解析导星日志"))
        factory, store = self.shell.client_factory, self.shell.logstore

        def work():
            # 走 logstore 而不是自己列日志目录:分组要按**拍摄目标**对齐,
            # 那需要 Autorun 侧的块;两条路径各自下载同一批文件还会撞 .part。
            data = with_client(factory, store.refresh)
            return data, gv._prepare(data)

        self.bg.run(work, gen=gen, on_done=self._apply, on_error=self._fail)

    def _apply(self, payload) -> None:
        self._loading = False
        self.data, self.prep = payload
        rows = self.prep.get("rows") or []
        if not rows:
            self.state.show_empty(
                _("没有导星数据"),
                _("设备上要有 PHD2_GuideLog_*.txt —— 解析已经跑完了,这批日志里没有导星段。"))
            return
        self.state.show_content()
        if self.selected is None:
            self.selected = models.default_guide_row(rows)
        # 默认把选中那一段所在的组展开,否则打开是一排折叠的组头
        loc = self.prep.get("loc") or {}
        gkey, fkey = loc.get(self.selected, (None, None))
        if gkey:
            self.expanded.add(gkey)
        if fkey:
            self.frag_open.add(fkey)
        if self.highlight is not None:
            self._locate(*self.highlight[:2])
        self._render()

    def _fail(self, exc: BaseException) -> None:
        self._loading = False
        self.state.show_empty(_("导星日志读取失败"), str(exc))
        self.report(exc, _("解析导星日志"))

    # ------------------------------------------------------------ 页间跳转

    def show_range(self, t0: float, t1: float, label: str) -> None:
        """从拍摄记录页跳过来:定位到与这段曝光重叠最多的导星段并高亮。"""
        self.highlight = (t0, t1, label)
        # **回到「全段」窗口**(老 UI 同款)。不复位的话,上一次留下的
        # 5 分钟窗很可能整个落在高亮区间外面 —— 跳过来看到的是一段
        # 跟那次曝光毫无关系的曲线,而且没有任何提示。
        self.window_index = 0
        self.pos = 0
        if getattr(self, "win_combo", None) is not None:
            self.win_combo.blockSignals(True)
            self.win_combo.setCurrentIndex(0)
            self.win_combo.blockSignals(False)
        if self.prep:
            self._locate(t0, t1)
            self._render()
        else:
            self.reload()

    def _locate(self, t0: float, t1: float) -> None:
        best = models.locate_range(self.prep, t0, t1)
        if best is None:
            # **找不到要明说**,不能默默停在原地
            self.shell.notice(
                _("没找到与「{0}」重叠的导星段 —— 那段时间可能没在导星").format(
                    self.highlight[2] if self.highlight else ''))
            return
        self.selected = best
        loc = (self.prep.get("loc") or {}).get(best, (None, None))
        if loc[0]:
            self.expanded.add(loc[0])
        if loc[1]:
            self.frag_open.add(loc[1])

    # ------------------------------------------------------------ 渲染

    def _render(self) -> None:
        self.header.set_subtitle(self.prep.get("status") or _(self.SUBTITLE))
        self.overview.setText(self.prep.get("summary") or "")
        groups = self.prep.get("groups") or []
        n_frag = sum(1 for g in groups for it in (g.get("items") or ())
                     if it.get("type") == "frag")
        self.group_note.setText(
            _("{0} 组").format(len(groups)) + (_(" · {n_frag} 簇短尝试已折叠").format(
                n_frag=n_frag) if n_frag else ""))
        rows = models.guiding_rows(self.prep, self.expanded, self.frag_open)
        self.sections.set_rows([
            {"key": r["key"], "cells": [W.cell(
                r["title"], sub=r.get("sub") or None,
                weight="semibold" if r.get("group") else None,
                sub_color=theme.tone_color(r["tone"]) if r.get("tone") else None)]}
            for r in rows])
        if self.selected is not None:
            self.sections.select_key(f"r:{self.selected}")
        self._render_charts()

    def _render_charts(self) -> None:
        if self.dash_key is not None:
            self._render_dash()
            return
        self._render_segment()

    def _render_dash(self) -> None:
        """仪表盘视图:**一组段合起来**看是什么样。

        逐段看得到"这一段抖了",看不到"这一晚这个目标整体如何" —— 而后者
        才是"要不要重拍/要不要调极轴"的依据。聚合是重活(几万帧 numpy),
        放工作线程,结果按组缓存。
        """
        self.scroll.clear()
        body = self.scroll.body
        key = self.dash_key
        group = next((g for g in (self.prep.get("groups") or [])
                      if g["key"] == key), None)
        if group is None:
            self.dash_key = None
            self._render_segment()
            return
        self.chart_card.set_title(_("仪表盘 · {0}").format(group.get('title') or ''))
        self.chart_card.set_chip(_("组聚合"), "accent")
        self.win_combo.setEnabled(False)
        self.slider.setEnabled(False)

        row = W.hbox(gap="sm")
        row.addWidget(W.button(_("← 回到段视图"), on_click=self._leave_dash))
        row.addStretch(1)
        body.addLayout(row)

        agg = self._dash_cache.get(key)
        if agg is None:
            body.addWidget(W.label(_("正在聚合这一组…"), role="dim"))
            body.addStretch(1)
            self._start_dash(key, group)
            return

        m = gd.summary_model(agg)
        chips = W.hbox(gap="xs")
        for text, tone in m["badges"]:
            chips.addWidget(W.StatusChip(
                str(text), models.TONE_MAP.get(str(tone)) or "accent"))
        chips.addStretch(1)
        body.addLayout(chips)
        body.addWidget(W.label(m["title"], role="title", wrap=True))
        body.addWidget(W.label(m["sub"], role="subtitle", wrap=True))
        if m["pills"]:
            pr = W.hbox(gap="xs")
            for text, tip in m["pills"]:
                chip = W.StatusChip(str(text), "accent")
                # tooltip 是老 UI 那颗胶囊的一半价值:「2.00″/px」本身
                # 看不出是导星的还是主镜的
                chip.setToolTip(str(tip))
                pr.addWidget(chip)
            pr.addStretch(1)
            body.addLayout(pr)
        for _glyph, name, rows in m["groups"]:
            if not rows:
                continue      # 空分区连标题都不留 —— 空标题看着像"没读到"
            body.addWidget(W.GroupHeader(name, _glyph))
            for label, value, note, mono, tone in rows:
                body.addWidget(W.MetricRow(
                    str(label),
                    f"{value}  {note}".rstrip() if note else str(value),
                    mono=bool(mono),
                    tone=models.TONE_MAP.get(str(tone or ""))))
        act = W.hbox(gap="sm")
        act.addWidget(W.button(_("复制全部信息"),
                               on_click=lambda: self._copy_dash(agg)))
        act.addStretch(1)
        body.addLayout(act)
        small = _small_charts(agg.get("ch") or {}, agg.get("unit") or "″")
        if small:
            grid = QGridLayout()
            grid.setSpacing(theme.Space.CARD_GAP)
            for i, wdg in enumerate(small):
                grid.addWidget(wdg, i // CHART_COLS, i % CHART_COLS)
            body.addLayout(grid)
        body.addStretch(1)

    def _leave_dash(self) -> None:
        self.dash_key = None
        self._render()

    def _copy_dash(self, agg) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(gd.dashboard_text(agg))
        self.shell.notice(_("仪表盘信息已复制"))

    def _start_dash(self, key: str, group: dict) -> None:
        rows = self.prep.get("rows") or []
        data = self.data

        def work():
            return key, gd.aggregate_group(group, rows, data)

        def done(payload):
            k, agg = payload
            self._dash_cache[k] = agg
            if self.dash_key == k:
                self._render()

        # **不给 gen**:聚合几万帧要好几秒,期间任何重画都会 bump 世代,
        # 带 gen 的话结果回来必被当成"迟到的"整份丢掉。
        self.bg.run(work, on_done=done,
                    on_error=lambda e: self.report(e, _("聚合导星组")))

    def _render_segment(self) -> None:
        self.scroll.clear()
        body = self.scroll.body
        rows = self.prep.get("rows") or []
        if self.selected is None or self.selected >= len(rows):
            body.addWidget(W.label(_("选一段查看曲线"), role="dim"))
            body.addStretch(1)
            return
        row = rows[self.selected]
        if row.get("kind") == "cal":
            # **卡头也要跟着换。** 原来这一支直接 return,`set_title`/`set_chip`
            # 只在下面的导星段分支里调 —— 于是选中「校准 · 失败」时,卡片
            # 标题还是上一段的「导星段 …125.8 分钟」、右上胶囊还是「0.87″」。
            # 一行"校准失败"旁边挂着一个跟它毫无关系的 RMS。
            # **字段叫 `cal_fail`**(共享层 `_cal_row` 给的),不是 `cal_ok`。
            # 读错名字不会报错,只会让每一行校准都显示成"成功"。
            ok = not bool(row.get("cal_fail"))
            self.chart_card.set_title(row.get("title") or _("校准"))
            self.chart_card.set_chip(_("校准失败") if not ok else _("校准段"),
                                     "bad" if not ok else "accent")
            # 时间窗与滑杆在校准行下**要置灰** —— 不然滑一下什么也不动,像坏了
            self.win_combo.setEnabled(False)
            self.slider.setEnabled(False)
            body.addWidget(W.label(row.get("cal_text") or row.get("main") or "",
                                   role="body", wrap=True))
            body.addStretch(1)
            return
        self.win_combo.setEnabled(True)

        ch = models.chart_payload(row, window_index=self.window_index,
                                  pos=self.pos, width=float(CURVE_W))
        if not ch:
            # 同一条路径的另一半:空段也不能留着上一段的标题+胶囊
            self.chart_card.set_title(row.get("title") or _("曲线与统计"))
            self.chart_card.set_chip("", None)
            body.addWidget(W.EmptyState(
                _("这一段画不出曲线"),
                _("有效帧不足两帧 —— 多半是 settle 期间的短尝试。")))
            return
        self.slider.setEnabled(bool(ch.get("can_pan")))
        self.chart_card.set_title(row.get("title") or _("曲线与统计"))
        self.chart_card.set_chip(row.get("unit") or "″", "accent")
        if row.get("level"):
            # **胶囊文字取共享层给的专用字段,不 split 拼好的串、
            # 也不按显示标签去 stat_rows 里找。** 前者(`row["sub"].split("·")[0]`)
            # 会随分隔符变化静默失效;后者(`r[0] == "RMS 合计"`)会随翻译失效。
            self.chart_card.set_chip(ch.get("rms_chip") or "",
                                     models.TONE_MAP.get(row["level"]))

        curve = _Curve(ch, self.highlight)
        body.addWidget(curve)

        charts = ch.get("charts") or {}
        small = _small_charts(charts, ch.get("unit") or "″")
        ovw = self._overview_chart()
        if ovw is not None:
            small.append(ovw)
        if small:
            grid = QGridLayout()
            grid.setSpacing(theme.Space.CARD_GAP)
            for i, w in enumerate(small):
                grid.addWidget(w, i // CHART_COLS, i % CHART_COLS)
            body.addLayout(grid)
        rows = ch.get("stat_rows") or []
        if rows:
            body.addWidget(W.GroupHeader(_("段统计"), W.GLYPH_STATS))
            for k, v, tone in rows:
                body.addWidget(W.MetricRow(k, v, tone=tone))
        else:
            body.addWidget(W.label(ch.get("stat") or "", role="subtitle",
                                   wrap=True))
        body.addStretch(1)

    # ------------------------------------------------------------ 交互

    def on_theme(self) -> None:
        """段列表的语义色是**烤进 cell 字典**的(`theme.tone_color()` 的结果),
        曲线与七张小图的颜色同理 —— 切档只重刷 QSS 不会动它们。

        实测:运行时切到红光档,段列表**还是绿色字**。红光档存在的唯一
        理由就是不破坏暗适应,一列绿字直接把这个理由作废。
        """
        if self.prep:
            self._render()

    def _overview_chart(self):
        """整夜**逐段 RMS 总览**(第八张图)。柱可点,点了跳到那一段。

        共享层 `_prepare` 一直在算 `overview`,`overview_hit_bar()` 也一直在,
        只是 Qt 里**没有任何调用方** —— 于是"这一夜哪几段特别差"这个总览
        整个没有,要一段段点过去才知道。
        """
        ov = (self.prep or {}).get("overview")
        if not ov or not ov.get("bars"):
            return None
        canvas = _Overview(ov, self.selected)
        n = len(ov["bars"])
        canvas.clicked.connect(lambda x, _y: self._pick_overview(x, n))
        return _titled(
            _("逐段 RMS 总览({n} 段 · {0})").format(ov.get('unit', '″'), n=n)
            + (_(" · 只画角秒段") if ov.get("mixed") else ""),
            canvas)

    def _pick_overview(self, x: float, n: int) -> None:
        """点柱跳段。**命中靠几何反算**,不给每根柱挂事件 ——
        柱最窄只有 2px,逐根挂几乎点不中(共享层那条注释记的就是这个)。"""
        ov = (self.prep or {}).get("overview") or {}
        k = gv.overview_hit_bar(float(x), n, w=float(gv.CHART_W))
        if k is None:
            return
        bars = ov.get("bars") or []
        if 0 <= k < len(bars):
            self.selected = int(bars[k][0])
            loc = (self.prep.get("loc") or {}).get(self.selected, (None, None))
            if loc[0]:
                self.expanded.add(loc[0])
            if loc[1]:
                self.frag_open.add(loc[1])
            self._render()

    def _pick(self, key: str) -> None:
        kind, _sep, tag = key.partition(":")
        if kind == "d":
            # 仪表盘:**一组段合起来**看是什么样。逐段看得到"这一段抖了",
            # 看不到"这一晚这个目标整体如何"。
            self.dash_key = tag
            self._render()
            return
        self.dash_key = None
        if kind == "g":
            self.expanded.symmetric_difference_update({tag})
            self._render()
        elif kind == "x":
            self.frag_open.symmetric_difference_update({tag})
            self._render()
        elif kind == "r":
            try:
                self.selected = int(tag)
            except ValueError:
                return
            self.pos = 0.0
            self.slider.blockSignals(True)
            self.slider.setValue(0)
            self.slider.blockSignals(False)
            self._render_charts()

    def _set_window(self, idx: int) -> None:
        self.window_index = idx
        self._render_charts()

    def _set_pos(self, value: int) -> None:
        self.pos = value / 100.0
        self._render_charts()


# ---------------------------------------------------------------- 主曲线

class _Curve(W.Canvas):
    """RA/DEC 主曲线。一条折线**一个路径**,不是一千二百个图元。"""

    def __init__(self, ch: dict, highlight=None):
        super().__init__(CURVE_W, CURVE_H)
        self._ch = ch
        self._hl = highlight

    def paint(self, p, w, h) -> None:
        from PySide6.QtCore import QPointF, QRectF
        from PySide6.QtGui import QPainterPath, QPen

        ch = self._ch
        rng = max(1e-6, float(ch.get("range", 2.0)))
        self.fill_bg(p, w, h)

        # **左边留一条刻度栏。** 原来 y 刻度画在 x=2、正压着曲线,
        # 跳转高亮那种整幅铺满的情况下几乎认不出数字。
        def x_of(t: float) -> float:
            t0, t1 = ch.get("t0", 0.0), ch.get("t1", 1.0)
            return Y_AXIS_W + (t - t0) / max(1e-9, t1 - t0) * (w - Y_AXIS_W)

        def y_of(v: float) -> float:
            return h / 2 - (v / rng) * (h / 2)

        # **高亮区间画在最底下** —— 要看的是"这段 sub 曝光期间"导星什么样,
        # 画在曲线之下才不会盖住尖峰。
        if self._hl is not None and ch.get("epoch"):
            r0 = self._hl[0] - ch["epoch"]
            r1 = self._hl[1] - ch["epoch"]
            hx0, hx1 = max(0.0, x_of(r0)), min(w, x_of(r1))
            if hx1 > hx0:
                p.fillRect(QRectF(hx0, 0, hx1 - hx0, h),
                           theme.alpha(theme.Q.WARN, 0.16))

        # **网格线落在整数角秒上**,不是画布的四分位 —— 刻度写着 +1″/-1″
        # 才能一眼读出幅度。
        step = next((s for s in (1, 2, 5, 10, 20, 50, 100) if rng / s <= 4), 100)
        p.setPen(QPen(theme.Q.CHART_GRID, 1.0))
        v = float(step)
        while v <= rng + 1e-9:
            for sign in (1, -1):
                y = y_of(sign * v)
                p.drawLine(QPointF(Y_AXIS_W, y), QPointF(w, y))
                self.text_at(p, Y_AXIS_W - 3.0, y - 4.0, f"{int(sign * v):+d}″",
                             color=theme.Q.TEXT_FAINT, size=theme.Font.TINY,
                             align_right=True)
            v += step
        p.setPen(QPen(theme.Q.CHART_AXIS, 1.0))
        p.drawLine(QPointF(Y_AXIS_W, h / 2), QPointF(w, h / 2))

        self._time_ticks(p, w, h, x_of)

        self.text_at(p, 44.0, 2.0, "RA", color=theme.Q.CHART_A,
                     size=theme.Font.TINY, bold=True)
        self.text_at(p, 70.0, 2.0, "DEC", color=theme.Q.CHART_B,
                     size=theme.Font.TINY, bold=True)
        if ch.get("dense"):
            # **切了包络就要说出来**,不然"波动范围带"会被读成"抖动有这么大"
            self.text_at(p, w - 330.0, 2.0,
                         _("包络视图:带 = 波动范围 · 实线 = 滑动 RMS(30 帧)"),
                         color=theme.Q.TEXT_FAINT, size=theme.Font.TINY)

        for t in ch.get("lost") or ():
            xx = x_of(t)
            p.setPen(QPen(theme.alpha(theme.Q.BAD, 0.45), 1.0))
            p.drawLine(QPointF(xx, h - 6), QPointF(xx, h))

        env = ch.get("env")
        if env:
            for key, col in (("ra", theme.Q.CHART_A), ("dec", theme.Q.CHART_B)):
                band = env.get(key) or {}
                if len(band.get("t") or ()) < 2:
                    continue
                path = QPainterPath()
                path.moveTo(x_of(band["t"][0]), y_of(band["hi"][0]))
                for tt, vv in zip(band["t"][1:], band["hi"][1:]):
                    path.lineTo(x_of(tt), y_of(vv))
                for tt, vv in zip(reversed(band["t"]), reversed(band["lo"])):
                    path.lineTo(x_of(tt), y_of(vv))
                path.closeSubpath()
                p.fillPath(path, theme.alpha(col, 0.20))
            for key, col in (("ra", theme.Q.CHART_A), ("dec", theme.Q.CHART_B)):
                band = env.get(key) or {}
                if len(band.get("t") or ()) < 2:
                    continue
                _polyline(p, col, 1.4,
                          [(x_of(tt), y_of(vv))
                           for tt, vv in zip(band["t"], band["mid"])])
            return

        for key, col in (("ra", theme.Q.CHART_A), ("dec", theme.Q.CHART_B)):
            pts = ch.get(key) or ()
            if len(pts) >= 2:
                _polyline(p, col, 1.2, [(x_of(t), y_of(v)) for t, v in pts])

    def _time_ticks(self, p, w, h, x_of) -> None:
        """X 轴时间刻度(HH:MM)。

        曲线的 x 是**段内相对秒**,而标签要的是钟点 —— ``epoch`` 是段起点的
        绝对时刻。没有它只能看形状,说不出"哪个时刻开始变差",而那恰恰是
        看这张图的目的。
        """
        import datetime as dt

        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QPen

        ch = self._ch
        t0, t1 = float(ch.get("t0", 0.0)), float(ch.get("t1", 1.0))
        span = max(1e-9, t1 - t0)
        step = next((s for s in _TIME_STEPS if span / s <= 8.0), _TIME_STEPS[-1])
        epoch = ch.get("epoch")
        # **刻度落在钟点上**(01:30 / 02:00 …),不是从段起点按步长往后推。
        # 后者会给出 01:58 / 02:28 / 02:58 这种读不出规律的标签 ——
        # 而这张图的用处正是"几点开始变差"。
        if epoch:
            first_abs = (int(epoch + t0) // step + 1) * step
            t = float(first_abs - epoch)
        else:
            t = float((int(t0) // step + 1) * step)
        p.setPen(QPen(theme.Q.CHART_GRID, 1.0))
        while t < t1:
            x = x_of(t)
            p.drawLine(QPointF(x, 0), QPointF(x, h))
            label = (dt.datetime.fromtimestamp(epoch + t).strftime("%H:%M")
                     if epoch else f"{t / 60.0:.0f}m")
            self.text_at(p, x + 2.0, h - 14.0, label,
                         color=theme.Q.TEXT_FAINT, size=theme.Font.TINY)
            p.setPen(QPen(theme.Q.CHART_GRID, 1.0))
            t += step


def _polyline(p, color, width: float, pts) -> None:
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QPainterPath, QPen

    if len(pts) < 2:
        return
    path = QPainterPath(QPointF(*pts[0]))
    for x, y in pts[1:]:
        path.lineTo(QPointF(x, y))
    p.setPen(QPen(color, width))
    p.drawPath(path)
    p.setPen(QPen(theme.Q.CHART_GRID, 1.0))


# ---------------------------------------------------------------- 统计小图

def _small_charts(ch: dict, unit: str) -> list[QWidget]:
    """七张小图。**每张都要有标题** —— 几张小方块摆一起,不写标题谁也认不出
    哪张是直方图哪张是滚动 RMS。"""
    out: list[QWidget] = []
    # **键是平铺的 `sc_pts` / `sc_rng` / `rms_total`,不是一个叫 `scatter`
    # 的子字典。** 原来写的是 `ch.get("scatter")` —— 那个键从来不存在,
    # 条件永远 False,`_Scatter` 这个类**一次都没画过**:不报错、不违反
    # 任何契约,只是八张图变成七张(独立验收 3.8 抓到的)。
    if ch.get("sc_pts"):
        # 一眼看出是各向同性抖动还是**单轴**在跑
        out.append(_titled(
            _("偏差散点 RMS {0:.2f}{unit} · 圆=1×/2×RMS").format(
                ch.get('rms_total', 0.0), unit=unit),
            _Scatter({"pts": ch["sc_pts"], "rng": ch.get("sc_rng"),
                      "rms": ch.get("rms_total")})))
    if ch.get("hist"):
        # 量程要写出来 —— 没有它读不出这堆柱子是宽是窄
        rng = float((ch["hist"] or {}).get("rng") or 0.0)
        out.append(_titled(_("偏差直方图(±{rng:.1f}{unit})").format(
            rng=rng, unit=unit), _Hist(ch["hist"])))
    if ch.get("roll"):
        out.append(_titled(
            _("滚动 RMS({ROLL_WIN_S:g} 秒窗 · 峰值 {0:.2f}{unit})").format(
                float(ch.get('roll_max') or 0.0), ROLL_WIN_S=gv.ROLL_WIN_S, unit=unit),
            _Roll(ch["roll"])))
    if ch.get("drift"):
        # **唯一能指向极轴误差的读数** —— 那句解释是常驻的,不是超阈值才出现:
        # 数值为 0 时这张图就是两条竖线加两个 +0.00,没有解释根本看不懂。
        out.append(_titled(
            _("漂移速率({unit}/min · DEC 漂移大常提示极轴误差)").format(unit=unit),
            _Drift(ch["drift"], unit)))
    if ch.get("pulse"):
        # 单向堆修正 = 平衡或极轴有问题
        out.append(_titled(_("修正脉冲"), _Pulse(ch["pulse"])))
    period = ch.get("period")
    if period and period.get("pts"):
        out.append(_titled(_("RA 周期图(峰值 {0:.0f}s)").format(period['peak_p']),
                           _Period(period)))
    snr = ch.get("snr")
    if snr and (snr.get("snr") or snr.get("mass")):
        # 视宁/透明度代理 —— 区分"导星差"和"天气差"
        out.append(_titled(_("星点 SNR / 质量(均值 {0:.1f})").format(snr['mean']), _Snr(snr)))
    return out


def _titled(title: str, canvas: QWidget) -> QWidget:
    card = W.Card(flat=True)
    card.add(W.label(title, role="subtitle"))
    card.add(canvas)
    return card


class _Chart(W.Canvas):
    def __init__(self):
        super().__init__(int(gv.CHART_W), int(gv.CHART_H))
        self.setFixedWidth(int(gv.CHART_W))


class _Overview(_Chart):
    """整夜逐段 RMS 柱状图。好=绿、预警=琥珀、差=红,选中的那根描边。

    **分档阈值走共享层 `_rms_level`** —— 在这里再写一遍阈值,等于让同一个
    数字在两处显示成两种好坏。
    """

    def __init__(self, ov: dict, selected):
        super().__init__()
        self._ov = ov
        self._sel = selected

    def paint(self, p, w, h) -> None:
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QPen

        self.fill_bg(p, w, h)
        bars = self._ov.get("bars") or []
        if not bars:
            return
        unit = self._ov.get("unit") or "″"
        top = max((v for _i, v in bars), default=1.0) or 1.0
        span = w - 2 * gv.BAR_M
        slot = span / len(bars)
        bw = max(2.0, slot - 1.0)
        base = h - 14.0
        for k, (idx, val) in enumerate(bars):
            bh = max(1.0, min(1.0, float(val) / top) * (base - 4.0))
            x = gv.BAR_M + k * slot
            rect = QRectF(x, base - bh, bw, bh)
            p.setPen(Qt.NoPen)
            p.setBrush(theme.tone_color(
                models.TONE_MAP.get(gv._rms_level(float(val), unit))))
            p.drawRect(rect)
            if idx == self._sel:
                # 选中的那根**描边**,不是换色 —— 换色会和好坏分档撞车
                p.setPen(QPen(theme.Q.ACCENT, 1.6))
                p.setBrush(Qt.NoBrush)
                p.drawRect(rect.adjusted(-1.0, -1.0, 1.0, 1.0))
        self.text_at(p, 4.0, h - 12.0, _("最高 {top:.2f}{unit}").format(top=top, unit=unit),
                     color=theme.Q.TEXT_FAINT)


class _Scatter(_Chart):
    """RA-DEC 偏差散点 + 1×/2×RMS 参考圆。"""

    def __init__(self, sc: dict):
        super().__init__()
        self._sc = sc

    def paint(self, p, w, h) -> None:
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QPen

        self.fill_bg(p, w, h)
        cx, cy = w / 2, h / 2
        rng = max(1e-9, float(self._sc.get("rng") or 1.0))
        r_px = min(w, h) / 2 - 4
        rms = float(self._sc.get("rms") or 0.0)
        p.setPen(QPen(theme.Q.CHART_AXIS, 1.0))
        for k in (1.0, 2.0):
            rr = rms * k / rng * r_px
            if 1.0 < rr <= r_px:
                p.drawEllipse(QPointF(cx, cy), rr, rr)
        p.setPen(Qt.NoPen)
        p.setBrush(theme.alpha(theme.Q.CHART_A, 0.55))
        for ra, dec in self._sc.get("pts") or ():
            x = cx + max(-1.0, min(1.0, ra / rng)) * r_px
            y = cy - max(-1.0, min(1.0, dec / rng)) * r_px
            p.drawEllipse(QPointF(x, y), 1.4, 1.4)
        p.setBrush(Qt.NoBrush)


class _Hist(_Chart):
    """偏差直方图。RA/DEC **半透明叠画** —— 叠色正是这张图的读法,
    合成一个图元会让重叠处不再叠色。"""

    def __init__(self, hist: dict):
        super().__init__()
        self._h = hist

    def paint(self, p, w, h) -> None:
        from PySide6.QtCore import QRectF

        self.fill_bg(p, w, h)
        bins = max(1, len(self._h.get("ra") or ()))
        bw = w / bins
        for key, col in (("ra", theme.Q.CHART_A), ("dec", theme.Q.CHART_B)):
            brush = theme.alpha(col, 0.5)
            for i, v in enumerate(self._h.get(key) or ()):
                bh = max(0.0, float(v)) * (h - 4)
                p.fillRect(QRectF(i * bw, h - bh, max(1.0, bw - 1), bh), brush)


class _Roll(_Chart):
    def __init__(self, roll):
        super().__init__()
        self._roll = list(roll or ())

    def paint(self, p, w, h) -> None:
        self.fill_bg(p, w, h)
        if len(self._roll) < 2:
            return
        vals = [v for _t, v in self._roll]
        top = max(1e-6, max(vals))
        t0 = self._roll[0][0]
        span = max(1e-9, self._roll[-1][0] - t0)
        _polyline(p, theme.Q.CHART_C, 1.2,
                  [((t - t0) / span * w, h - (v / top) * (h - 4))
                   for t, v in self._roll])


class _Drift(_Chart):
    """RA/DEC 漂移速率:两根左右对称的条 + 零线。"""

    def __init__(self, drift: dict, unit: str):
        super().__init__()
        self._d = drift
        self._unit = unit

    def paint(self, p, w, h) -> None:
        from PySide6.QtCore import QPointF, QRectF
        from PySide6.QtGui import QPen

        self.fill_bg(p, w, h)
        ra = float(self._d.get("ra") or 0.0)
        dec = float(self._d.get("dec") or 0.0)
        top = max(abs(ra), abs(dec), gv.DRIFT_DEC_WARN, 1e-6)
        p.setPen(QPen(theme.Q.CHART_AXIS, 1.0))
        p.drawLine(QPointF(w / 2, 0), QPointF(w / 2, h))
        for i, (v, col) in enumerate(((ra, theme.Q.CHART_A),
                                      (dec, theme.Q.CHART_B))):
            y = 24.0 + i * 46.0
            length = abs(v) / top * (w / 2 - 6)
            x = w / 2 if v >= 0 else w / 2 - length
            p.fillRect(QRectF(x, y, max(1.0, length), 16.0), col)
        self.text_at(p, 4.0, 6.0, f"RA {ra:+.2f}", color=theme.Q.TEXT_DIM)
        self.text_at(p, 4.0, 52.0, f"DEC {dec:+.2f}", color=theme.Q.TEXT_DIM)
        # **判读结论直接写出来** —— 阈值是有前提的经验值,让用户盯着数字猜没有意义
        if self._unit == "″" and abs(dec) > gv.DRIFT_DEC_WARN:
            self.text_at(p, 4.0, h - 14.0, _("DEC 漂移偏大,建议检查极轴"),
                         color=theme.Q.WARN)


class _Pulse(_Chart):
    """修正脉冲:四行(RA E/W、DEC N/S)次数条 + 累计毫秒。"""

    def __init__(self, pulse):
        super().__init__()
        self._rows = list(pulse or ())

    def paint(self, p, w, h) -> None:
        from PySide6.QtCore import QRectF

        self.fill_bg(p, w, h)
        top = max([c for _l, c, _t, _k in self._rows] or [1])
        # **字在上、条在下**(老 UI 的排法)。原来是"条从 x=48 起、数字右对齐
        # 到 w-4",满格条的右端正好压住数字 —— 实测 "RA W" 显示成
        # 「…5·156950ms」。字条分行之后不可能压。
        # 数字也换成人话:`1629 次 · 48.1s`,原始毫秒读起来要自己除。
        band = (h - 8.0) / max(1, len(self._rows))
        for i, (label, cnt, total_ms, kind) in enumerate(self._rows):
            y = 4.0 + i * band
            secs = float(total_ms) / 1000.0
            self.text_at(p, 2.0, y, _("{label}   {cnt} 次 · {secs:.1f}s").format(
                label=label, cnt=cnt, secs=secs),
                         color=theme.Q.TEXT_DIM)
            by = y + band - 9.0
            length = (cnt / max(1, top)) * (w - 8.0)
            p.fillRect(QRectF(4.0, by, max(1.0, length), 5.0),
                       theme.Q.CHART_A if kind == "ra" else theme.Q.CHART_B)


class _Period(_Chart):
    """RA 周期图 —— 整页最"值钱在判读"的一张(蜗杆周期误差)。"""

    def __init__(self, period: dict):
        super().__init__()
        self._pd = period

    def paint(self, p, w, h) -> None:
        from PySide6.QtCore import QPointF
        from PySide6.QtGui import QPen

        self.fill_bg(p, w, h)
        p.setPen(QPen(theme.Q.CHART_GRID, 1.0))
        for xn, label in self._pd.get("ticks") or ():
            x = float(xn) * w
            p.drawLine(QPointF(x, 0), QPointF(x, h))
            self.text_at(p, x + 2.0, h - 12.0, label, color=theme.Q.TEXT_DIM)
        _polyline(p, theme.Q.CHART_A, 1.2,
                  [(float(xn) * w, h - float(a) * (h - 6.0))
                   for xn, a in (self._pd.get("pts") or ())])
        px = float(self._pd.get("peak_x") or 0.0) * w
        p.setPen(QPen(theme.Q.WARN, 1.0))
        p.drawLine(QPointF(px, 0), QPointF(px, h))


class _Snr(_Chart):
    """SNR 与星质量(各自归一到自身最大值,只看形状不看绝对值)。"""

    def __init__(self, snr: dict):
        super().__init__()
        self._s = snr

    def paint(self, p, w, h) -> None:
        self.fill_bg(p, w, h)
        for key, col in (("snr", theme.Q.CHART_C), ("mass", theme.Q.CHART_B)):
            _polyline(p, col, 1.2,
                      [(float(tn) * w, h - float(v) * (h - 6.0))
                       for tn, v in (self._s.get(key) or ())])
