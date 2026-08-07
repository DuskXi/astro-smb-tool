"""拍摄记录页:夜次 → 目标 → 详情,外加整夜甘特与天球图。

两张图都走自绘画布:

* **甘特**:每个目标一条彩条 + 导星覆盖带 + 时间刻度。命中测试是**纯几何反算**
  (``views.records.timeline_hit_bar`` 那套约定),不给每条挂事件 —— 一屏
  两百多个图元挂两百多个事件在任何框架下都不该做。
* **天球**:alt-az 极坐标仰视图,北上**东左**,``r = R·(90-alt)/90``。投影走
  ``views.skychart``,和浏览页详情那个迷你雷达是同一份公式。

一条设备事实决定了这一页的形态:**Autorun 日志是会话结束时一次性写盘的**,
运行中设备上根本看不到 —— 所以目标列表永远是历史,"正在拍摄"只能来自外壳
横幅(watcher 的帧 mtime 心跳)。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QSlider, QSplitter, QWidget

from astro_smb_app import logstore
from astro_smb_app.views import records as rv
from astro_smb_app.views import skychart
from astro_smb_qt import models, theme, widgets as W
from astro_smb_qt.pages.base import Page, PageHeader, page_layout
from astro_smb_qt.workers import with_client
from astro_smb.i18n import N_, gettext as _

TL_W, TL_H = models.TIMELINE_W, 58.0
SKY = 260.0
#: 方位标注画在圆外 12px 处 —— margin 给小了「东」会被左边界静默切掉
SKY_MARGIN = 18.0
ZOOM_SKY = 520.0


class RecordsPage(Page):
    TITLE = N_("拍摄记录")
    SUBTITLE = N_("按夜次归并的 Autorun 日志 —— 拍了什么、拍成了没有")

    def __init__(self, shell):
        super().__init__(shell)
        self.data = None
        self.night_index = 0
        # **默认开**,与老 UI 一致(`records.xaml` 的 `IsOn="True"`)。
        # 同一个目标被拆进几个 Plan 是常态,不合并的话一夜看起来像拍了五六个。
        self.merge = True
        self.sky_bg = False
        self.selected = 0
        self.model: dict = {}
        self._zoom_frac = 0.5
        self._loading = False
        #: 目标 → 首张亮场的 FITS 头。共享层的统计/详情一直**收**它,
        #: 原来没人喂 —— 少了「设备」「实测坐标」「滤镜徽章」三处。
        self.fits_map: dict = {}
        #: 头按 (share, path, size, mtime) 缓存,刷新不重读
        self._fits_cache: dict = {}
        #: 导星质量判读:``目标名 → GuideCheck``,以及一份 busy/错误状态。
        #: 按**目标名**做键而不是 `id(run)` —— 刷新一次日志对象全换,
        #: 用对象身份做键等于每次刷新都白算(老 UI 用 id,是它的一个坑)。
        self._quality: dict = {}
        self._quality_state: dict = {}
        self._quality_cancel = None
        self._build()
        shell.watch.connect(self._on_watch)

    # ------------------------------------------------------------ 界面

    def _build(self) -> None:
        root = page_layout(self)
        self.header = PageHeader(_(self.TITLE), _(self.SUBTITLE))
        # 顺序照老 UI:[刷新] 夜次[下拉] [合并计划],**全部紧跟标题**。
        self.night_combo = W.combo(on_change=self._pick_night)
        # 宽度按真实项文字算(填完 items 之后还要再算一次)—— 写死会把
        # "· 59 帧"截在框外,而帧数正是加它的理由。3D 天球页先踩过同一个坑。
        W.fit_combo(self.night_combo, [_("2026-07-29 · 2 目标 · 59 帧")])
        self.header.add_tool(W.button(_("刷新"), on_click=self.reload))
        self.header.add_tool(W.label(_("夜次"), role="subtitle"))
        self.header.add_tool(self.night_combo)
        # 「合并计划」原来整个没有。同一个目标被拆进几个 Plan 是常态
        # (中途暂停、改参数),不合并的话一夜看起来像拍了五六个目标。
        self.merge_box = W.check(_("合并计划"), on=True,
                                 on_change=self._set_merge)
        self.header.add_tool(self.merge_box)
        self.watch_chip = W.StatusChip("", "ok")
        self.watch_chip.setVisible(False)
        self.header.add_action(self.watch_chip)
        root.addWidget(self.header)

        # 概览/时间轴/列表**整体**归在忙态-空态之下:分开摆的话空态出现时
        # 上面还挂着两张永远空的卡,看着像"数据来了但没画出来"。
        content = QWidget()
        col = W.vbox(content, gap="card")
        self.summary_card = W.Card(_("整夜概览"), "")
        srow = W.hbox(gap="xl")
        self.sum_left = W.label("", role="body", wrap=True)
        self.sum_right = W.label("", role="body", wrap=True)
        srow.addWidget(self.sum_left, 1)
        srow.addWidget(self.sum_right, 1)
        self.summary_card.add_layout(srow)
        col.addWidget(self.summary_card)

        self.tl_card = W.Card(
            _("整夜时间轴"),
            _("彩条 = 目标拍摄块(点击选中) · 绿条 = 导星覆盖 · 空隙 = 损失时间"))
        # 最小宽度给小一点(不是 TL_W):它要能**跟着窗口伸缩**,而 900px 的
        # 下限会让窄窗口横向溢出。几何按实际宽度算,所以 resize 要重画。
        self.timeline = W.OpsCanvas(320, int(TL_H))
        self.timeline.hit.connect(self._pick_span)
        self.timeline.resized.connect(self._retime)
        self.tl_card.add(self.timeline)
        col.addWidget(self.tl_card)

        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        split.addWidget(self._left_card())
        split.addWidget(self._detail_card())
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setSizes([420, 760])
        col.addWidget(split, 1)

        self.state = W.StateStack(content)
        root.addWidget(self.state, 1)
        self.state.show_empty(_("还没有读取日志"), _("连接设备后这一页会自动下载并解析。"))

    def _left_card(self) -> QWidget:
        card = W.Card(_("目标列表"), _("这一夜拍了哪几个、它们在天上哪儿"))
        self.sky_row = W.hbox(gap="sm")
        self.sky_at = W.label("", role="faint", wrap=True)
        self.sky_row.addWidget(self.sky_at, 1)
        # 巡天底图开关。**默认关** —— 底图约 8 MB,要用户点头才下载。
        self.bg_box = W.check(_("巡天底图"), on_change=self._set_sky_bg)
        self.sky_row.addWidget(self.bg_box)
        self.sky_row.addWidget(W.button(_("放大"), on_click=self._open_zoom))
        card.add_layout(self.sky_row)
        # **站点设置整块原来没有。** 经度能从日志反推(PHD2 段头时角 + 同时刻
        # 目标 RA),**纬度推不出来,只能用户设** —— 没有入口就永远吃 30.0°N
        # 那个默认值,而天球图和高度角/气量判读全靠这两个数。
        site_row = W.hbox(gap="sm")
        site_row.addWidget(W.label(_("纬度"), role="subtitle"))
        self.lat_box = W.line_edit("30.0", on_return=self._apply_site)
        self.lat_box.setFixedWidth(74)
        site_row.addWidget(self.lat_box)
        site_row.addWidget(W.label(_("经度"), role="subtitle"))
        self.lon_label = W.label("—", role="dim")
        site_row.addWidget(self.lon_label)
        site_row.addWidget(W.button(_("应用"), on_click=self._apply_site))
        site_row.addStretch(1)
        card.add_layout(site_row)
        # **目标列表在上、天球在下** —— 老 UI 就是这个顺序。反过来的话
        # 一进页面先看到一张空天球,目标列表被推到折叠线以下。
        self.runs = W.DataTable(["*"])
        self.runs.key_selected.connect(self._pick_run)
        card.add(self.runs, 1)
        self.sky = W.OpsCanvas(int(SKY), int(SKY))
        card.add(self.sky)
        # 署名是 CC BY 4.0 的**要求**,不是装饰 —— 底图开着就必须显示
        self.sky_credit = W.label("", role="faint", wrap=True)
        self.sky_credit.setVisible(False)
        card.add(self.sky_credit)
        return card

    def _detail_card(self) -> QWidget:
        card = W.Card(_("目标详情"), _("帧数、导星、事件时间线"))
        self.detail = W.Scroll(gap="sm")
        card.add(self.detail, 1)
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
                _("拍摄记录来自设备上的 Autorun_Log_*.txt —— 先连一台设备。"))
            return
        self._loading = True
        gen = self.bg.bump()
        self.state.show_busy(_("正在下载并解析 Autorun 日志"))
        factory = self.shell.client_factory
        store = self.shell.logstore

        def work():
            # LogStore 内部有 _refresh_lock 全程串行 —— 记录页与导星页会并发
            # 触发,不串行会同时下载同一份日志、os.replace 同一个 .part
            # (真机撞过 WinError 32)。
            def run(client):
                data = store.refresh(client)
                # 顺带取每个目标首张亮场的 FITS 头。**失败不算失败** ——
                # 它只补充"设备/实测坐标/滤镜"那几行,拿不到就少几行,
                # 不该把整页日志一起拖垮。
                try:
                    fits = logstore.collect_fits_map(
                        client, data.nights, store.share, self._fits_cache)
                except Exception:            # noqa: BLE001
                    fits = {}
                return data, fits

            return with_client(factory, run)

        self.bg.run(work, gen=gen, on_done=self._apply,
                    on_error=self._fail)

    def _apply(self, payload) -> None:
        self._loading = False
        data, self.fits_map = payload
        self.data = data
        nights = models.night_list(data)
        if not nights:
            self.state.show_empty(
                _("没有拍摄记录"),
                _("设备上要有 Autorun_Log_*.txt。注意日志是在会话结束时才一次性写盘的 —— 正在跑的那一夜要等它结束才看得到。"))
            return
        self.night_combo.blockSignals(True)
        self.night_combo.clear()
        labels = models.night_labels(nights)
        self.night_combo.addItems(labels)
        W.fit_combo(self.night_combo, labels)
        self.night_combo.setCurrentIndex(min(self.night_index, len(nights) - 1))
        self.night_combo.blockSignals(False)
        self.state.show_content()
        self._update_site_ui()
        self._render()

    def _fail(self, exc: BaseException) -> None:
        self._loading = False
        self.state.show_empty(_("日志读取失败"), str(exc))
        self.report(exc, _("解析日志"))

    # ------------------------------------------------------------ 渲染

    def _render(self) -> None:
        self.model = models.records_model(
            self.data, night_index=self.night_index, selected=self.selected,
            merge=self.merge, fits_map=self.fits_map, site=self._site())
        m = self.model
        self.night_index = m.get("night_index", 0)
        self.header.set_subtitle(m.get("meta") or _(self.SUBTITLE))
        self.sum_left.setText(m.get("summary_left") or "")
        self.sum_right.setText(m.get("summary_right") or "")
        self.summary_card.set_chip(_("{0} 个目标").format(m.get('target_count', 0)), "accent")

        rows = []
        for r in m.get("runs") or []:
            kind = r.get("kind", "run")
            # 组头加粗、间隙灰;缩进用**前导空格**而不是给 cell 加 padding ——
            # cell 的几何是共享的,为一页改它会波及所有表。
            pad = " " * int(r.get("indent", 0.0) / 6)
            head = f"{pad}{r['mark']} {r['title']}".rstrip()
            rows.append({"key": r["key"], "cells": [W.cell(
                head, sub=r.get("sub"),
                weight="semibold" if kind == "group" else None,
                dim=kind == "gap",
                sub_color=(theme.tone_color(r["tone"]) if r.get("tone")
                           else None))]})
        self.runs.set_rows(rows)
        if m.get("runs"):
            self.runs.select_key(str(self.selected))
        self._render_timeline(m)
        self._render_sky(m.get("sky"))
        self._refresh_sky_bg()
        self._render_detail(m.get("detail"))

    # ------------------------------------------------------------ 巡天底图

    def _set_sky_bg(self, on: bool) -> None:
        """开关巡天底图。第一次要先下载(约 8 MB),**必须先问**。"""
        from astro_smb_app import skymap

        if not on:
            self.sky_bg = False
            self.sky.set_background("")
            self.sky_credit.setVisible(False)
            return
        if not skymap.survey_available():
            if not self.confirm(
                    _("下载巡天底图?"),
                    _('要从 ESO 下载一张全天全景({SURVEY_SIZE_HINT}),之后一直缓存在本机。\n\n{SURVEY_CREDIT}').format(
                        SURVEY_SIZE_HINT=_(skymap.SURVEY_SIZE_HINT),
                        SURVEY_CREDIT=_(skymap.SURVEY_CREDIT))):
                self.bg_box.setChecked(False)
                return
            self._download_survey()
            return
        self.sky_bg = True
        self._refresh_sky_bg()

    def _download_survey(self) -> None:
        from astro_smb_app import skymap

        gen = self.bg.bump()
        self.sky_credit.setText(_("正在下载巡天底图…"))
        W.show_if(self.sky_credit, True)

        def done(_):
            self.sky_bg = True
            self._refresh_sky_bg()

        def fail(exc):
            self.bg_box.setChecked(False)
            self.sky_credit.setVisible(False)
            self.report(exc, _("下载巡天底图"))

        self.bg.run(skymap.download_survey, gen=gen, on_done=done,
                    on_error=fail)

    def _refresh_sky_bg(self) -> None:
        """按当前站点与**天球图那个时刻**重投影底图。

        时刻必须与点用的是同一个(`sky["ts"]`)—— 各用各的会让星点与银河
        错位,而错位在一张星图上几乎看不出来(老 UI 真机踩过"M 8 不在银心")。
        """
        from astro_smb_app import skymap

        sky = (self.model or {}).get("sky") or {}
        ts = sky.get("ts")
        if not self.sky_bg or ts is None:
            self.sky.set_background("")
            self.sky_credit.setVisible(False)
            return
        lat, lon = self._site()
        gen = self.bg.bump()

        def work():
            return skymap.render_altaz(lat, lon, float(ts), size=int(SKY))

        def done(path):
            r = (SKY / 2.0) - SKY_MARGIN
            c = SKY / 2.0
            # 直径 = 2×地平线半径,与圈精确对齐(拉伸到整张画布会错位)
            self.sky.set_background(str(path), (c - r, c - r, 2 * r, 2 * r))
            self.sky_credit.setText(_(skymap.SURVEY_CREDIT))
            W.show_if(self.sky_credit, True)

        self.bg.run(work, gen=gen, on_done=done,
                    on_error=lambda e: self.report(e, _("重投影底图")))

    # -------------------------------------------------- 导星质量分析

    def _render_quality(self, body, d) -> None:
        """「导星质量分析」卡:从**拍摄结果**倒推导星好不好。

        它回答的是"我这一晚拍的这个目标,导星拖了后腿没有" —— 所以它
        长在具体那一夜的记录下面,而不是某个 3D 工具里(老 UI 的位置)。
        """
        name = str(d.get("target") or "")
        if not name:
            return
        quality = self._quality.get(name)
        state = self._quality_state.get(name) or {}
        busy = bool(state.get("busy"))

        body.addWidget(W.group_title(_("导星质量分析")))
        if quality is None:
            body.addWidget(W.label(
                str(state.get("text") or _("尚未分析拍摄结果")),
                role="body", tone="bad" if state.get("error") else None,
                wrap=True))
            body.addWidget(W.label(
                _("将抽样原始 FITS,提取主镜 FWHM/椭率/方向,再与同期 PHD2 导星数据交叉判读"), role="subtitle", wrap=True))
        else:
            # 结论色:漂移=红、过冲=琥珀、好=绿。**判读本身在共享层**,
            # 这里只把 verdict 映射到本主题的三档。
            tone = {"good": "ok", "drift": "bad",
                    "overguide": "warn"}.get(
                        getattr(quality, "verdict", ""), None)
            body.addWidget(W.label(str(getattr(quality, "headline", _("证据不足"))),
                                   role="strong", tone=tone, wrap=True))
            conf = {"high": _("高"), "medium": _("中"), "low": _("低")}.get(
                getattr(quality, "confidence", "low"), _("低"))
            body.addWidget(W.label(
                str(state.get("text")) if (busy or state.get("error")) else
                _("可信度 {conf} · 成功板解算帧、主镜星点形状与同期 PHD2 交叉判读").format(conf=conf),
                role="subtitle", wrap=True))
            self._render_polar(body, quality)
            for line in list(getattr(quality, "findings", ()) or ()):
                body.addWidget(W.label(f"· {line}", role="subtitle", wrap=True))

        row = W.hbox(gap="sm")
        row.addWidget(W.button(
            _("停止分析") if busy else
            (_("重新分析") if quality is not None else _("开始分析")),
            on_click=(self._stop_quality if busy else self._start_quality),
            enabled=self.shell.client_factory is not None))
        row.addStretch(1)
        body.addLayout(row)

    def _render_polar(self, body, quality) -> None:
        """极轴误差示意图 + 一句"该往哪拧"。

        **没有极轴结论就整块不画** —— 空着一个画好的靶环比不画更容易被
        误读成"极轴没问题"。
        """
        polar = getattr(quality, "polar", None)
        if polar is None:
            return
        cond = float(getattr(quality, "polar_cond", float("inf")))
        # 单目标恰定 ⇒ 残差恒为 0,推翻不了;夜次级联合反解跨多个目标才有
        # 残差可看。**读结构化字段,不去 findings 里搜「恰定」两个字** ——
        # 那是拿会被翻译的显示文本当判据。
        falsifiable = bool(getattr(quality, "polar_falsifiable", False))
        geo = rv.polar_plot_geometry(polar, 132.0)
        canvas = W.OpsCanvas(132, 132)
        canvas.set_ops(_polar_ops(geo))
        line = W.hbox(gap="md")
        line.addWidget(canvas)
        col = W.vbox(gap="xs")
        col.addWidget(W.label(_("极轴误差"), role="subtitle"))
        col.addWidget(W.label(f"{polar.total_arcmin:.2f}′", role="metric"))
        col.addWidget(W.label(rv.polar_advice(polar, cond=cond,
                                              falsifiable=falsifiable),
                              role="subtitle", wrap=True))
        line.addLayout(col, 1)
        body.addLayout(line)

    def _start_quality(self) -> None:
        from astro_smb_app import guidequality
        from astro_smb_qt.workers import CancelToken, with_client

        run = self._selected_run()
        if run is None or self.shell.client_factory is None:
            return
        name = str(getattr(run, "target", "") or "")
        token = CancelToken()
        self._quality_cancel = token
        self._quality_state[name] = {"busy": True, "text": _("正在准备…")}
        self._render()
        factory = self.shell.client_factory
        phd2 = list(getattr(self.data, "phd2_logs", []) or [])
        lat, lon = self._site()
        share = self.shell.logstore.share

        def work(report):
            def go(client):
                return guidequality.analyze(
                    client, run, phd2, lat, lon, share=share,
                    on_progress=report, cancel=token.event)

            return with_client(factory, go)

        def done(quality):
            self._quality[name] = quality
            self._quality_state[name] = {"busy": False, "text": ""}
            self._render()

        def fail(exc):
            self._quality_state[name] = {
                "busy": False, "text": str(exc), "error": True}
            self._render()

        # **不给 gen** —— 这一趟动辄几十秒,期间任何一次重画都会 bump 世代,
        # 带 gen 的话结果回来必被当成"迟到的"整份丢掉(表现:转了半天没反应)。
        self.bg.run(work, on_done=done, on_error=fail,
                    on_progress=lambda t: self._quality_progress(name, t))

    def _quality_progress(self, name: str, text: str) -> None:
        st = self._quality_state.get(name) or {}
        st["text"] = text
        self._quality_state[name] = st
        self._render()

    def _stop_quality(self) -> None:
        if self._quality_cancel is not None:
            self._quality_cancel.cancel()

    def _selected_run(self):
        """当前选中的 ``TargetRun``。**按夜次下标 + 选中下标现取**,
        不缓存对象 —— 刷新一次日志,所有 run 对象都换了。"""
        nights = models.night_list(self.data) if self.data is not None else []
        if not nights:
            return None
        night = nights[min(max(0, self.night_index), len(nights) - 1)]
        runs = list(night.runs)
        if not runs:
            return None
        return runs[min(max(0, self.selected), len(runs) - 1)]

    def _apply_site(self) -> None:
        """保存纬度。经度**恒用日志推算值**(`lon_auto` 永远是 True)——
        它比人肉输入准得多,而纬度日志里根本没有。"""
        from astro_smb_app.logstore import save_site

        try:
            lat = float(self.lat_box.text().strip())
        except (ValueError, AttributeError):
            self.shell.notice(_("纬度格式无效,应为数字(北纬为正,如 30.0)"))
            return
        lat = max(-90.0, min(90.0, lat))
        lon = self._site()[1]
        save_site(lat, lon, True)
        self.lat_box.setText(f"{lat:g}")
        self._update_site_ui()
        self.shell.notice(_("站点已保存:{0} {1}").format(rv._fmt_lat(lat), rv._fmt_lon(lon)))
        # 站点变了 → 天球点、底图重投影全要重来
        if self.model:
            self._render()

    def _update_site_ui(self) -> None:
        lat, lon = self._site()
        self.lat_box.setText(f"{lat:g}")
        est = getattr(self.data, "lon_estimate", None) is not None
        self.lon_label.setText(rv._fmt_lon(lon) + (_("(推算)") if est else _("(默认)")))

    def _apply_zoom_bg(self, zoom, ts) -> None:
        """把巡天底图铺到放大层。底图没开就什么都不做。"""
        if not self.sky_bg or ts is None:
            return
        from astro_smb_app import skymap

        lat, lon = self._site()
        gen = self.bg.generation

        def work():
            return skymap.render_altaz(lat, lon, float(ts), size=int(ZOOM_SKY))

        def done(path):
            r = (ZOOM_SKY / 2.0) - 18.0
            c = ZOOM_SKY / 2.0
            zoom.canvas.set_background(str(path), (c - r, c - r, 2 * r, 2 * r))
            zoom.credit.setText(_(skymap.SURVEY_CREDIT))
            W.show_if(zoom.credit, True)

        self.bg.run(work, gen=gen, on_done=done, on_error=lambda _e: None)

    def _site(self) -> tuple[float, float]:
        from astro_smb_app.logstore import load_site

        site = load_site()
        lat = float(site.get("lat", 30.0))
        lon = getattr(self.data, "lon_estimate", None)
        return lat, float(lon if lon is not None else site.get("lon", 121.0))

    def _retime(self) -> None:
        """画布宽度变了 —— 用**上一次的模型**按新宽度重画一遍。"""
        if getattr(self, "model", None):
            self._render_timeline(self.model)

    def _render_timeline(self, m: dict) -> None:
        """整夜甘特。

        **宽度按画布实际宽度算,不是写死的 `TL_W`。** 时间轴是一条"整夜"的
        轴,它必须顶满可用宽度 —— 固定 900px 的话窗口一拉宽右边就空一块,
        而那块空白看起来像"这一夜后半段没拍东西"。画布宽度会随窗口变,
        所以每次 resize 都要重画(见 `_build` 里接的 `resized`)。
        """
        spans = m.get("spans") or []
        if not spans:
            self.timeline.set_ops([], [])
            self.tl_card.setVisible(False)
            return
        W.show_if(self.tl_card, True)
        w = float(max(240, self.timeline.width()))
        ops: list[dict] = [{"op": "line", "x1": 0.0, "y1": rv.TL_TICK_Y,
                            "x2": w, "y2": rv.TL_TICK_Y,
                            "stroke": theme.C.CHART_AXIS, "width": 1.0}]
        hits = []
        for sp in spans:
            x, bw = rv.timeline_bar_px(float(sp["f0"]), float(sp["f1"]), w)
            ops.append({"op": "rect", "x": x, "y": rv.TL_BAR_Y, "w": bw,
                        "h": rv.TL_BAR_H, "fill": _argb(sp.get("fill")),
                        # 半透明 = 暂停 / 被截断。老 UI 靠它一眼看出
                        # "这一段没拍完",丢掉的话所有块看起来都一样正常。
                        "opacity": float(sp.get("alpha", 1.0)),
                        "radius": 2.0})
            if sp.get("label") and bw >= rv.TL_LABEL_MIN_W:
                # **按条宽截断**(老 UI 是 `bw - 8.0`)。不截的话
                # `NGC 7293` 会顶出条外和后一条的标签挤在一起。
                ops.append({"op": "text", "x": x + 4.0, "y": rv.TL_BAR_Y + 2.0,
                            "text": sp["label"], "size": 10.0,
                            "maxw": max(8.0, bw - 8.0),
                            "fill": theme.C.TEXT})
            # 命中区一律 [x1, y1, x2, y2, key],细条上下左右各放宽一点
            hits.append([x - rv.TL_HIT_PAD_X, rv.TL_BAR_Y - rv.TL_HIT_PAD_Y,
                         x + bw + rv.TL_HIT_PAD_X,
                         rv.TL_BAR_Y + rv.TL_BAR_H + rv.TL_HIT_PAD_Y,
                         sp.get("key", "")])
        # **导星覆盖画的是真实区间,一段一条。** 原来是每个目标一条从头连到尾
        # 的绿条(按覆盖率上色),于是"哪一段丢了导星"这条信息整个没了。
        for g in m.get("guides") or ():
            gx = float(g["f0"]) * w
            gw = max(1.0, (float(g["f1"]) - float(g["f0"])) * w)
            ops.append({"op": "rect", "x": gx, "y": rv.TL_GUIDE_Y, "w": gw,
                        "h": rv.TL_GUIDE_H, "fill": theme.C.OK,
                        "opacity": 0.55})
        for tick in m.get("ticks") or ():
            x = float(tick["f"]) * w
            # 整点是**贯穿彩条的竖线**(老 UI 那样),不是轴下 4px 的小刻度 ——
            # 短刻度对不到彩条上,"这一段是几点到几点"要靠目测平移。
            ops.append({"op": "line", "x1": x, "y1": rv.TL_BAR_Y, "x2": x,
                        "y2": rv.TL_TICK_Y + 4.0,
                        "stroke": theme.C.CHART_AXIS, "width": 1.0})
            # 标签不出画布边界(最后一个整点在最右边时会被切掉半个)
            lx = max(0.0, min(x + 2.0, w - 32.0))
            ops.append({"op": "text", "x": lx, "y": rv.TL_TICK_Y + 5.0,
                        "text": tick["label"], "size": 9.0,
                        "fill": theme.C.TEXT_FAINT})
        self.timeline.set_ops(ops, hits)

    def _render_sky(self, sky, *, canvas=None, size: float = SKY) -> None:
        canvas = canvas or self.sky
        if not sky:
            canvas.setVisible(False)
            self.sky_at.setText(_("这一夜没有可上天球的目标(纯偏置/暗场的坐标是停机位)"))
            return
        W.show_if(canvas, True)
        # **整图同一时刻** —— 时刻写在标题里,免得被当成"各点各自时刻"
        self.sky_at.setText(_("全天位置(仰视:北上·东左) · {0}").format(sky.get('at', '')))
        canvas.set_ops(_sky_ops(sky, size))

    def _render_detail(self, d) -> None:
        self.detail.clear()
        body = self.detail.body
        if not d:
            body.addWidget(W.label(_("选一个目标查看详情"), role="dim"))
            body.addStretch(1)
            return
        body.addWidget(W.label(d.get("title", ""), role="title", wrap=True))
        if d.get("coord"):
            # 坐标单独一行、等宽 —— 逐位比对 RA/DEC 是常做的事
            body.addWidget(W.label(str(d["coord"]), role="mono", wrap=True))
        if d.get("sub"):
            body.addWidget(W.label(d["sub"], role="subtitle", wrap=True))
        if d.get("badges"):
            row = W.hbox(gap="xs")
            for b in d["badges"]:
                # 徽章**各有各的色**(老 UI 五枚分色)。原来一律 accent,
                # "这是什么"那条信息被抹平。
                text, style = b if isinstance(b, (tuple, list)) else (b, "")
                row.addWidget(W.StatusChip(str(text),
                                           models.TONE_MAP.get(str(style))
                                           or "accent"))
            row.addStretch(1)
            body.addLayout(row)
        for item in d.get("pairs") or ():
            if not isinstance(item, dict):      # 兼容旧形状
                body.addWidget(W.MetricRow(str(item[0]), str(item[1])))
                continue
            body.addWidget(W.MetricRow(str(item["k"]), str(item["v"]),
                                       tone=item.get("tone")))
            bar = item.get("bar")
            if bar:
                # 「帧数 33/30」「覆盖率 97%」在老 UI 各带一条量条 ——
                # 够没够一眼可判,光看数字要心算。
                frac, level = (bar if isinstance(bar, (tuple, list))
                               else (bar, None))
                body.addWidget(W.Gauge(float(frac),
                                       tone=models.TONE_MAP.get(str(level))))
        self._render_quality(body, d)
        # 两颗跳转按钮在**事件时间线之上**(老 UI 的位置)。放在下面的话,
        # 一条 30 项的时间线要滚到底才够得着。
        row = W.hbox(gap="sm")
        row.addWidget(W.button(
            _("看这段导星"), kind="primary", on_click=self._open_guiding,
            enabled=d.get("t0") is not None,
            tip=_("看这段曝光期间导星的样子,而不是整晚的平均")))
        row.addWidget(W.button(_("在浏览页打开"), on_click=self._open_browser,
                               enabled=bool(d.get("target"))))
        row.addStretch(1)
        body.addLayout(row)
        evs = d.get("events") or []
        if evs:
            body.addWidget(W.group_title(_("事件时间线")))
            # **结构化,不是一串 `·` 拼起来的文本行。** 时刻列 / 状态色标记 /
            # 卡片 / 迷你进度条 —— 少了它们就只剩一堆等长的灰字,
            # "哪一步出了事"要逐行读才知道(清单 2.10)。
            for i, ev in enumerate(evs):
                if ev.get("kind") == "gap":
                    body.addWidget(W.TimelineGap(str(ev.get("title") or "")))
                    continue
                body.addWidget(W.TimelineRow(
                    ev, first=(i == 0), last=(i == len(evs) - 1)))
        body.addStretch(1)

    # ------------------------------------------------------------ 交互

    def on_theme(self) -> None:
        """甘特/天球的颜色是烤进 op 里的 —— 切档必须整份重生成。"""
        if self.model:
            self._render()

    def _pick_night(self, idx: int) -> None:
        self.night_index = idx
        self.selected = 0        # 换夜次要清选中,否则指向不存在的 run
        self._render()

    def _set_merge(self, on: bool) -> None:
        self.merge = bool(on)
        self._render()

    def _pick_run(self, key: str) -> None:
        """点**左侧目标列表**。"""
        self._select(key)

    def _pick_span(self, key: str) -> None:
        """点**整夜时间轴上的彩条**。"""
        self._select(key)

    def _select(self, key: str) -> None:
        """选中一个目标 —— **两条路必须走同一段代码**。

        这两个入口曾经各写各的:点时间轴走 `_render()`(带上 ``fits_map``
        与 ``site``),点列表却自己拼了一份**不带这两个字段**的模型、
        而且只重画详情。结果是同一个目标,从列表点进去比从时间轴点进去
        少了「实测坐标」「设备」两行、少一个滤镜徽章,天球上的高亮环与
        时刻**干脆不动**(还停在上一次全量渲染选中的那个目标上)。
        不报错,只是从最常用的那条路进去看到的东西是残的。

        这正是本项目反复栽的那一类:**共享层支持的字段,前端没传** ——
        修过一次之后又从一条没堵上的路径冒出来。所以这里不是"把漏掉的
        参数补上",而是把两个入口合并成一个,让它**没有地方**再分叉。
        """
        # `g:`/`x:` 是组头与间隙 —— **不是目标**,点了不该换详情
        try:
            self.selected = int(key)
        except ValueError:
            return
        self.runs.select_key(key)
        self._render()

    def _open_guiding(self) -> None:
        d = (self.model or {}).get("detail") or {}
        if d.get("t0") is None:
            return
        self.shell.open_guiding(d["t0"], d["t1"], d.get("title") or _("这段拍摄"))

    def _open_browser(self) -> None:
        d = (self.model or {}).get("detail") or {}
        target = d.get("target")
        if not target:
            return
        share = self.shell.shares[0] if self.shell.shares else "EMMC Images"
        self.shell.open_browser_path(share, f"Plan\\Light\\{target}")

    def _on_watch(self, state: dict) -> None:
        from astro_smb_qt.shell import watch_text

        text = watch_text(state)
        self.watch_chip.set(text or "", "ok")

    # ------------------------------------------------------------ 放大层

    def _open_zoom(self) -> None:
        nights = models.night_list(self.data)
        if not nights:
            return
        dlg = _SkyZoom(self, nights[self.night_index])
        dlg.exec()


class _SkyZoom(W.Dialog):
    """天球放大层 + 时刻滑杆。

    **整图必须同一时刻** —— 滑杆走整夜任意时刻,拖动时点和标注一起动;
    各点用各自拍摄时刻会让图与真实天区错位。
    """

    def __init__(self, page: RecordsPage, night):
        super().__init__(page, _("全天位置"))
        self.page = page
        self._night = night
        lay = W.vbox(self, gap="sm", pad="card")
        self.title = W.label("", role="title")
        lay.addWidget(self.title)
        self.canvas = W.OpsCanvas(int(ZOOM_SKY), int(ZOOM_SKY))
        lay.addWidget(self.canvas)
        # 署名跟着底图走 —— CC BY 4.0 的**要求**,放大层也一样。
        self.credit = W.label("", role="faint", wrap=True)
        self.credit.setVisible(False)
        lay.addWidget(self.credit)
        row = W.hbox(gap="sm")
        row.addWidget(W.label(_("时刻"), role="subtitle"))
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 200)
        self.slider.setValue(100)
        self.slider.valueChanged.connect(self._on_slide)
        row.addWidget(self.slider, 1)
        lay.addLayout(row)
        self._on_slide(100)

    def _on_slide(self, value: int) -> None:
        # **选中的那个也要在放大层里高亮。** 不传 `selected` 的话所有点
        # 长得一模一样 —— 页面上明明有个青环,点「放大」反而找不到
        # "我看的是哪一个",而放大正是为了看清它。
        sky = models.sky_payload(self._night, value / 200.0,
                                 site=self.page._site(),
                                 selected=self.page.selected)
        if not sky:
            self.title.setText(_("这一夜没有可上天球的目标"))
            self.canvas.set_ops([])
            return
        self.title.setText(_("全天位置 · {0}(整图同一时刻)").format(sky['at']))
        self.canvas.set_ops(_sky_ops(sky, ZOOM_SKY, radius=5.0))
        # **放大层原来没有底图**:页面上底图开着,点「放大」出来是纯黑一张。
        # 放大恰恰是"这个目标落在银河哪一段"看得最清楚的时候。
        self.page._apply_zoom_bg(self, sky.get("ts"))


def _polar_ops(geo: dict) -> list[dict]:
    """极轴误差靶图的显示列表。**几何全部来自共享层**
    (`views.records.polar_plot_geometry`)—— 方位约定(北上东左)只存在于那一处,
    在这里重算一遍等于埋一个"图是镜像的"的雷。
    """
    cx, cy = geo["center"]
    ops: list[dict] = []
    for i, r in enumerate(geo["rings"] or ()):
        ops.append({"op": "ellipse", "x": cx, "y": cy, "rx": r, "ry": r,
                    "stroke": theme.C.CHART_AXIS,
                    "width": 1.0 if i == len(geo["rings"]) - 1 else 0.6,
                    "dash": None if i == len(geo["rings"]) - 1 else [3, 3]})
    r = float(geo["radius"])
    ops.append({"op": "line", "x1": cx - r, "y1": cy, "x2": cx + r, "y2": cy,
                "stroke": theme.C.CHART_AXIS, "width": 0.6, "dash": [3, 3]})
    ops.append({"op": "line", "x1": cx, "y1": cy - r, "x2": cx, "y2": cy + r,
                "stroke": theme.C.CHART_AXIS, "width": 0.6, "dash": [3, 3]})
    for text, lx, ly in geo["labels"] or ():
        ops.append({"op": "text", "x": lx, "y": ly, "text": text,
                    "size": 9.0, "fill": theme.C.TEXT_FAINT})
    ops.append({"op": "text", "x": cx + 3.0, "y": cy - r - 1.0,
                "text": _("满量程 {0:g}′").format(geo['full']), "size": 8.0,
                "fill": theme.C.TEXT_FAINT})
    marker = geo.get("marker")
    if marker is not None:
        ops.append({"op": "ellipse", "x": marker[0], "y": marker[1],
                    "rx": 4.0, "ry": 4.0, "fill": theme.C.BAD})
        ops.append({"op": "line", "x1": cx, "y1": cy,
                    "x2": marker[0], "y2": marker[1],
                    "stroke": theme.C.BAD, "width": 1.2})
    return ops


def _sky_ops(sky: dict, size: float, *, radius: float = 4.0) -> list[dict]:
    """天球显示列表。**选中的那颗换色 + 加描边环。**

    老 UI 里选中目标是青色高亮 + 环,这边两颗长得一模一样 —— 点了列表
    天球上毫无反应,而"这个目标在天上哪儿"正是这张图存在的理由。
    `skychart.point_ops` 一直支持 per-point 的 `fill`,只是没人传。
    """
    pts = []
    sel = None
    for p in sky.get("points") or ():
        if p.get("selected"):
            sel = dict(p, fill=theme.C.ACCENT)
        else:
            pts.append(p)
    ops = skychart.frame_ops(size, margin=SKY_MARGIN)
    ops += skychart.point_ops(pts, size, margin=SKY_MARGIN, radius=radius,
                              default_fill=theme.C.WARN,
                              label_fill=theme.C.TEXT)
    if sel is not None:
        # 单独一批画,`ring` 才只套在选中那颗上
        ops += skychart.point_ops([sel], size, margin=SKY_MARGIN,
                                  radius=radius + 1.0,
                                  default_fill=theme.C.ACCENT,
                                  ring=theme.C.ACCENT,
                                  label_fill=theme.C.TEXT)
    return ops


def _argb(rgb) -> str:
    """``(r, g, b)`` → ``#AARRGGBB``。

    视图模型的甘特调色板是 RGB 元组(老 UI 直接拿去建画刷)。原样塞进显示列表
    的话颜色解析不出来,**静默画成透明** —— 症状是"甘特彩条整排看不见"。
    """
    if isinstance(rgb, str):
        return rgb
    try:
        r, g, b = (int(v) for v in tuple(rgb)[-3:])
    except (TypeError, ValueError):
        return theme.C.ACCENT
    return f"#FF{r:02X}{g:02X}{b:02X}"
