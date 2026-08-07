"""3D 天球:把这一夜的目标放到真实星空里,可拖动旋转、滚轮缩放。

**两条路,优先真 3D。** 有 QtWebEngine 就把老 UI 那份 `web/sky3d.js`
(three.js,GPU 加速)原封不动嵌进来 —— 那是 920 行、已经在真机上跑了很久的
东西,重写一份纯属自找麻烦。没有它才退回下面这个 `QPainter` 正射球。

依赖账要说清楚:**QtWebEngine 已经在 PySide6 里**(完整包 665 MB,其中
208 MB 就是它)—— 用它不多花一分钱。上一版这里写着"几百 MB 额外依赖",
那个判断是**错的**,那笔钱早就付过了。

`QPainter` 那条现在是**降级路径**:相机在球外看向 (ra0, dec0),只画近半球,
有赤经赤纬网格、目标点、**当时的地平圈**、拖动旋转、滚轮缩放、点选。
装的是 `PySide6-Essentials`(不含 Addons)时走这条。

地平圈是这一页真正值钱的东西:它把"这个目标那个时刻在不在地平线上、离地平
多高"变成一眼可判。天顶方向 = (LST, 纬度),地平圈就是与它垂直的那个大圆。

数据全部走 `views.sky3d._build_nights()`:同夜同名目标跨 Plan 合并、坐标
**优先用 FITS 实测**(日志里的 goto 请求值和实际指向差着指向模型误差)、
纯偏置/暗场的坐标是停机位不上天球。
"""
from __future__ import annotations

import logging
import math

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QSlider, QSplitter, QWidget

from astro_smb.astro import lst_deg
from astro_smb.i18n import N_, gettext as _
from astro_smb_app import logstore
from astro_smb_app.views import browser as bv
from astro_smb_app.views import sky3d as sv
from astro_smb_qt import theme, webhost, widgets as W
from astro_smb_qt.pages.base import Page, PageHeader, page_layout
from astro_smb_qt.workers import with_client

#: 网格间隔(度)。再密就成一团网,再疏就看不出球面。
log = logging.getLogger(__name__)

RA_STEP, DEC_STEP = 30, 30
SPHERE_MIN = 320


def _unit(ra_deg: float, dec_deg: float) -> tuple[float, float, float]:
    ra, dec = math.radians(ra_deg), math.radians(dec_deg)
    return (math.cos(dec) * math.cos(ra), math.cos(dec) * math.sin(ra),
            math.sin(dec))


class SphereView(W.Canvas):
    """正射投影的天球。拖动转、滚轮缩放、点选目标。"""

    picked = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        # **高度给 0**:`Canvas` 的 height 参数是 `setFixedHeight`,给了值
        # 这个球就永远长不大 —— 卡片再高,球上面也只会多一块空白。
        # 天球是这一页的主角,它必须吃掉可用高度。
        super().__init__(SPHERE_MIN, 0, parent)
        self.setMinimumHeight(SPHERE_MIN)
        self.ra0 = 0.0          # 相机看向的赤经
        self.dec0 = 20.0        # 赤纬
        self.zoom = 1.0
        self.targets: list[dict] = []
        self.selected = ""
        self.horizon: list[tuple[float, float]] = []   # (ra, dec) 采样点
        self.zenith: tuple[float, float] | None = None
        self._drag: tuple[float, float] | None = None
        self._hits: list[tuple[float, float, str]] = []
        self.setMouseTracking(True)

    # -- 投影 ---------------------------------------------------------
    def _basis(self):
        f = _unit(self.ra0, self.dec0)
        # 右向量 = 天极叉视线(单位化)。视线正对天极时退化,夹住 dec0 避开。
        rx, ry = -f[1], f[0]
        n = math.hypot(rx, ry) or 1e-9
        r = (rx / n, ry / n, 0.0)
        u = (f[1] * r[2] - f[2] * r[1],
             f[2] * r[0] - f[0] * r[2],
             f[0] * r[1] - f[1] * r[0])
        return f, r, u

    def project(self, ra: float, dec: float):
        """→ ``(x, y, 可见)``;背面返回 ``可见=False``。"""
        f, r, u = self._basis()
        p = _unit(ra, dec)
        depth = sum(a * b for a, b in zip(p, f))
        cx, cy = self.width() / 2.0, self.height() / 2.0
        radius = min(cx, cy) * 0.92 * self.zoom
        x = cx + sum(a * b for a, b in zip(p, r)) * radius
        y = cy - sum(a * b for a, b in zip(p, u)) * radius
        return x, y, depth > 0.0

    # -- 交互 ---------------------------------------------------------
    def mousePressEvent(self, ev):  # noqa: N802
        pos = ev.position()
        self._drag = (pos.x(), pos.y())
        hit = self._hit(pos.x(), pos.y())
        if hit:
            self.picked.emit(hit)
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):  # noqa: N802
        pos = ev.position()
        if self._drag is not None and (ev.buttons() & Qt.LeftButton):
            dx, dy = pos.x() - self._drag[0], pos.y() - self._drag[1]
            self._drag = (pos.x(), pos.y())
            # 拖一屏转 180°;缩放越大转得越慢(否则放大后完全没法瞄准)
            span = max(1.0, min(self.width(), self.height()))
            self.ra0 = (self.ra0 - dx / span * 180.0 / self.zoom) % 360.0
            # **夹住赤纬**:到极点时右向量退化,画面会突然翻转
            self.dec0 = max(-85.0, min(85.0, self.dec0 + dy / span * 180.0
                                       / self.zoom))
            self.update()
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):  # noqa: N802
        self._drag = None
        super().mouseReleaseEvent(ev)

    def wheelEvent(self, ev):  # noqa: N802
        step = 1.0 + (0.0015 * ev.angleDelta().y())
        self.zoom = max(0.6, min(6.0, self.zoom * step))
        self.update()

    def _hit(self, x: float, y: float) -> str:
        best, bestd = "", 14.0
        for hx, hy, name in self._hits:
            d = math.hypot(hx - x, hy - y)
            if d < bestd:
                best, bestd = name, d
        return best

    # -- 绘制 ---------------------------------------------------------
    def paint(self, p, w: float, h: float) -> None:
        self.fill_bg(p, w, h)
        self._draw_limb(p, w, h)
        self._draw_grid(p)
        self._draw_horizon(p)
        self._draw_targets(p)

    def _draw_limb(self, p, w: float, h: float) -> None:
        from PySide6.QtCore import QRectF

        cx, cy = w / 2.0, h / 2.0
        r = min(cx, cy) * 0.92 * self.zoom
        p.setPen(W.pen(theme.Q.CHART_AXIS, 1.0))
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(QRectF(cx - r, cy - r, 2 * r, 2 * r))

    def _draw_grid(self, p) -> None:
        pen = W.pen(theme.Q.CHART_GRID, 1.0)
        p.setPen(pen)
        for ra in range(0, 360, RA_STEP):          # 赤经圈(经线)
            self._polyline(p, [(ra, d) for d in range(-80, 81, 4)])
        for dec in range(-60, 61, DEC_STEP):       # 赤纬圈(纬线)
            self._polyline(p, [(ra, dec) for ra in range(0, 361, 4)])

    def _draw_horizon(self, p) -> None:
        if not self.horizon:
            return
        # 地平圈画粗一点:它是这一页最该一眼看见的东西
        p.setPen(W.pen(theme.Q.WARN, 1.6))
        self._polyline(p, self.horizon, close=True)
        if self.zenith is not None:
            x, y, vis = self.project(*self.zenith)
            if vis:
                self.text_at(p, x + 4, y - 6, _("天顶"), color=theme.Q.WARN)

    def _draw_targets(self, p) -> None:
        from PySide6.QtCore import QRectF

        self._hits = []
        for t in self.targets:
            x, y, vis = self.project(float(t["ra"]), float(t["dec"]))
            if not vis:
                continue
            self._hits.append((x, y, t["name"]))
            sel = t["name"] == self.selected
            rad = 6.0 if sel else 4.0
            p.setPen(W.pen(theme.Q.TEXT, 1.5) if sel else Qt.NoPen)
            p.setBrush(theme.screen_color(t.get("color") or theme.C.ACCENT))
            p.drawEllipse(QRectF(x - rad, y - rad, 2 * rad, 2 * rad))
            p.setBrush(Qt.NoBrush)
            self.text_at(p, x + rad + 3, y - rad - 2, str(t["name"]),
                         color=theme.Q.TEXT if sel else theme.Q.TEXT_DIM,
                         bold=sel)

    def _polyline(self, p, pts, *, close: bool = False) -> None:
        """只连**都在近半球**的相邻点 —— 跨过边缘直接连过去会画出一道横穿
        整个球的假线(投影上看不出它其实绕到背面去了)。"""
        from PySide6.QtCore import QPointF

        seq = list(pts) + ([pts[0]] if close and pts else [])
        prev = None
        for ra, dec in seq:
            x, y, vis = self.project(float(ra), float(dec))
            if vis and prev is not None:
                p.drawLine(QPointF(prev[0], prev[1]), QPointF(x, y))
            prev = (x, y) if vis else None


class Sky3DPage(Page):
    TITLE = N_("3D 天球")
    SUBTITLE = N_("把这一夜的目标放到真实星空里 —— 拖动旋转,滚轮缩放")

    def __init__(self, shell):
        super().__init__(shell)
        self.data = None
        self.nights: list[dict] = []
        #: `None` = 还没选过 —— 第一次渲染时落到**最近**一夜(老 UI 同款)
        self.night_index = None
        self.selected = ""
        self._frac = 0.5
        self._loading = False
        #: 真 3D 视图(QtWebEngine)。None = 走降级的 QPainter 正射球。
        self._web = None
        self._web_post = None
        self._web_ready = False
        self._aimed = False
        #: FITS 头缓存,按 (share, path, size, mtime) —— 刷新不重读
        self._fits_cache: dict = {}
        #: 夜次 → 足迹;以及 sub 路径 → WCS payload(与质量分析共用口径)
        self._foot_cache: dict = {}
        self._wcs_cache: dict = {}
        self._assets = None
        self._server = None
        self._build()

    # ------------------------------------------------------------ 界面

    def _build(self) -> None:
        root = page_layout(self)
        self.header = PageHeader(_(self.TITLE), _(self.SUBTITLE))
        self.night_combo = W.combo(on_change=self._pick_night)
        # 宽度在 `_fit_night_combo()` 里按**真实项文字**算(见那里)
        self.night_combo.setSizeAdjustPolicy(
            W.QComboBox.AdjustToContents)
        self._fit_night_combo([_("2026-07-29 · 2 目标 · 59 帧")])
        self.header.add_tool(W.button(_("刷新"), on_click=self.reload))
        self.header.add_tool(W.label(_("夜次"), role="subtitle"))
        self.header.add_tool(self.night_combo)
        self.header.add_tool(W.button(_("正视目标"), on_click=self._face_selected))
        # 老 UI 那里是个 ToggleSwitch,Qt 原来把 `showHorizon` 写死成 True
        self.horizon_box = W.check(_("地平线"), on=True,
                                   on_change=lambda _v: self._push_web())
        self.header.add_tool(self.horizon_box)
        # **足迹(实际视场框)**:只用已经解算过的 WCS —— 与导星质量分析
        # 共用同一份缓存,解过的立刻就有,没解过的这里不去解(几十张 50MB)。
        self.foot_box = W.check(_("足迹"), on_change=self._toggle_foot)
        self.header.add_tool(self.foot_box)
        root.addWidget(self.header)

        body = QWidget()
        col = W.vbox(body, gap="card")
        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)

        sky_card = W.Card(_("天球"), _("拖动旋转 · 滚轮缩放 · 点选目标"))
        self.sky_card = sky_card
        self.sphere = SphereView()
        self.sphere.picked.connect(self._pick_target)
        # **优先真 3D。** 有 QtWebEngine 就嵌老 UI 那份 three.js 页面
        # (GPU 加速、920 行、真机跑了很久);没有才用下面这个正射球。
        ok, why = webhost.available()
        if ok:
            try:
                self._web, self._web_post = webhost.make_view(self._on_web)
                sky_card.add(self._web, 1)
                W.show_if(self.sphere, False)
                sky_card.set_chip("three.js", "accent")
            except Exception as exc:          # noqa: BLE001
                log.warning(_("QtWebEngine 起不来,退回正射球: %s"), exc)
                self._web = self._web_post = None
        # **署名是 CC BY 4.0 的要求,不是装饰。** 这一页用的是和拍摄记录页
        # 同一张巡天底图,那边贴了、这边没贴。
        from astro_smb_app import skymap

        self.credit = W.label(skymap.SURVEY_CREDIT + _(" · 渲染: three.js (MIT)"),
                              role="faint", wrap=True)
        sky_card.add(self.credit)
        if self._web is None:
            sky_card.add(self.sphere, 1)
            sky_card.set_chip(_("正射投影"), "warn")
            if not ok:
                self.shell.notice(_("{why} —— 用降级的正射天球").format(why=why), "warn")
        trow = W.hbox(gap="sm")
        self.time_label = W.label("", role="faint")
        trow.addWidget(self.time_label, 1)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 1000)
        self.slider.setValue(500)
        self.slider.valueChanged.connect(self._set_frac)
        trow.addWidget(self.slider, 2)
        sky_card.add_layout(trow)
        split.addWidget(sky_card)

        list_card = W.Card(_("目标"), _("点一个看它在球上的位置"))
        self.table = W.DataTable(["*"])
        self.table.key_selected.connect(self._pick_target)
        list_card.add(self.table, 1)
        self.detail = W.Scroll(gap="sm")
        list_card.add(self.detail, 1)
        split.addWidget(list_card)
        split.setStretchFactor(0, 1)
        split.setSizes([820, 380])
        col.addWidget(split, 1)

        self.state = W.StateStack(body)
        root.addWidget(self.state, 1)
        self.state.show_empty(_("还没有读取日志"), _("连接设备后这一页会自动下载并解析。"))

    # ------------------------------------------------------------ 加载

    def on_show(self) -> None:
        if self.data is None and not self._loading:
            self.reload()

    def on_connected(self, shares) -> None:
        self.data = None

    def reload(self) -> None:
        if self.shell.client_factory is None:
            self.state.show_empty(_("还没有连接设备"), _("先连一台设备。"))
            return
        self._loading = True
        gen = self.bg.bump()
        self.state.show_busy(_("正在下载并解析日志"))
        factory, store = self.shell.client_factory, self.shell.logstore

        def work():
            def run(client):
                data = store.refresh(client)
                # 坐标**优先用 FITS 实测**:日志里的是 goto 请求值,与实际
                # 指向差着指向模型误差(实测恒差 21′)。要读文件头,
                # 所以和 refresh 共用同一个 client、同在工作线程里做。
                #
                # **原来这里传的是 `{}`** —— 于是 `source` 恒为「日志坐标」,
                # FITS 那条分支是死代码,球上的点整体偏着约 21′,
                # 而界面上只写了四个字。
                # 拿不到头**不算失败**:少几行判读,不该把整页拖垮。
                try:
                    fits_map = logstore.collect_fits_map(
                        client, data.nights, store.share, self._fits_cache)
                except Exception:            # noqa: BLE001
                    fits_map = {}
                return data, fits_map

            data, fits_map = with_client(factory, run)
            assets = None
            if self._web is not None:
                # three.js 与巡天底图:**阻塞下载,必须在工作线程**。
                # 已经备好时这一步几乎是零开销(只做一次文件拷贝)。
                assets = sv.ensure_assets()
            return data, sv._build_nights(data, fits_map), assets

        self.bg.run(work, gen=gen, on_done=self._apply, on_error=self._fail)

    def _apply(self, payload) -> None:
        self._loading = False
        self.data, self.nights, assets = payload
        if assets is not None and self._server is None:
            self._assets = assets
            self._start_web(assets)
        if not self.nights:
            self.state.show_empty(
                _("这些日志里没有可上天球的目标"),
                _("纯偏置/暗场的坐标是**停机位**,不上天球;另外日志是会话结束才写盘的,正在跑的那一夜要等它结束。"))
            return
        self.night_combo.blockSignals(True)
        self.night_combo.clear()
        # 老 UI 的夜次项带目标数与帧数 —— 光有日期时"哪一夜拍得多"
        # 要一夜一夜点过去才知道
        items = [_("{0} · {1} 目标 · {2} 帧").format(
            n['date'], len(n['targets']), sum((t['frames'] for t in n['targets'])))
                 for n in self.nights]
        self.night_combo.addItems(items)
        self._fit_night_combo(items)
        # **默认最近一夜**(老 UI `SelectedIndex = len(nights)-1`)——
        # 打开这一页最想看的是昨晚拍的,不是三个月前那一夜。
        if self.night_index is None:
            self.night_index = len(self.nights) - 1
        self.night_combo.setCurrentIndex(
            max(0, min(self.night_index, len(self.nights) - 1)))
        self.night_combo.blockSignals(False)
        self.state.show_content()
        self._render()

    def _fail(self, exc: BaseException) -> None:
        self._loading = False
        self.state.show_empty(_("日志读取失败"), str(exc))
        self.report(exc, _("解析日志"))

    # ------------------------------------------------------------ 渲染

    def _night(self) -> dict:
        at = min(max(0, self.night_index), len(self.nights) - 1)
        return self.nights[at]

    def on_theme(self) -> None:
        """球面是自绘的,颜色在 `_render` 里取好交给 `SphereView`;
        真 three.js 那条路要把新配色推给页面。"""
        if self.data is not None:
            self._render()

    def _render(self) -> None:
        night = self._night()
        targets = night["targets"]
        self.sphere.targets = targets
        self.sphere.selected = self.selected
        self._update_horizon()
        self.sphere.update()
        self.sky_card.set_chip(_("{0} 个目标").format(len(targets)), "accent")
        self.table.set_rows([
            {"key": t["name"], "cells": [W.cell(
                t["name"],
                sub=_("{0} 帧 · {1:.1f} 小时 · {2}").format(
                    t['frames'], t['exposure'] / 3600, t['source']))]}
            for t in targets])
        if self.selected:
            self.table.select_key(self.selected)
        self._render_detail()
        self._push_web()

    # ------------------------------------------------------------ 真 3D

    def _start_web(self, assets) -> None:
        """起资产服务并把页面装进去。**只做一次。**"""
        if self._web is None:
            return
        from PySide6.QtCore import QUrl

        try:
            self._server = webhost.AssetServer(assets)
            self._web.load(QUrl(self._server.url("sky3d.html")))
        except Exception as exc:              # noqa: BLE001
            log.warning(_("天球页面装载失败: %s"), exc)
            self.shell.notice(_("3D 天球装载失败: {exc} —— 退回正射球").format(exc=exc), "warn")
            W.show_if(self._web, False)
            W.show_if(self.sphere, True)
            self._web = self._web_post = None

    def _on_web(self, msg: dict) -> None:
        """页面 → 宿主。**在 GUI 线程上被调用。**"""
        kind = msg.get("type")
        if kind == "ready":
            self._web_ready = True
            self._push_web()
        elif kind == "pick":
            name = str(msg.get("name") or "")
            if name:
                # 球上点过来的:镜头已经在那儿了,再飞一次是自己跟自己打架
                self._pick_target(name, fly=False)
        elif kind == "error":
            log.warning(_("天球页面报错: %s"), msg.get("message"))

    def _push_web(self) -> None:
        """把当前夜次推给页面。协议与老 UI **逐字一致**(init/targets/site)。"""
        if self._web_post is None or not self._web_ready or not self.nights:
            return
        url = sv.survey_asset_url(self._assets)
        if url and self._server is not None:
            # 共享层给的是老 UI 那个虚拟主机的地址,这边换成本地服务的
            url = self._server.url(url.rsplit("/", 1)[-1])
        self._web_post({"type": "init", "survey": url})
        night = self._night()
        self._web_post({"type": "targets", "items": [
            {"name": t["name"], "ra": t["ra"], "dec": t["dec"],
             # **颜色要过一遍主题映射。** 原样推 `t["color"]` 的话,
             # 红光档下这块画布上全是原色标记 —— 而这一页最大的那块面积
             # 正是它。降级的 QPainter 球那条路一直是过的,web 这条漏了。
             "color": theme.screen_color(t["color"]).name()}
            for t in night["targets"]]})
        # 重推 targets 会把标记全部重建 —— 选中态要跟着补回去,
        # 否则换个时刻/夜次之后球上的高亮就悄悄没了
        self._push_select()
        lat, lon = self._site()
        ts = night["ts0"] + (night["ts1"] - night["ts0"]) * self._frac
        self._web_post({"type": "site", "lat": lat,
                        "lst": lst_deg(ts, lon),
                        "showHorizon": self.horizon_box.isChecked()})
        if not self._aimed:
            self._aimed = True
            self._aim_initial(night, lat, lon, ts)

    def _fit_night_combo(self, items) -> None:
        """宽度按真实项文字算 —— 实现在共享层(拍摄记录页同款下拉共用)。"""
        W.fit_combo(self.night_combo, items)

    def demo_footprints(self) -> None:
        """挑一个**有已解算 sub 的夜次**并勾上足迹(``--auto`` 用)。

        不是装饰:足迹这条链路(读夜次 → 找已解算的 WCS → 算图幅环 → 推给
        three.js)以前只能靠人手点,验收员没法截图,上一轮只好自己写一个
        打补丁的驱动进程去点。有了这个钩子,``--page sky --auto`` 就能把
        整条路跑出来。

        **要挑对夜次。** 默认停在最近一夜,而"最近"未必解算过 —— 那时
        勾上只会看到"还没有解算过的 sub",证明不了足迹能画。
        """
        from PySide6.QtCore import QTimer

        if not self.nights:
            return

        def fire():
            # 从最近往前找第一个有缓存 WCS 的夜次
            for i in range(len(self.nights) - 1, -1, -1):
                if self._night_has_wcs(self.nights[i]):
                    if i != self.night_index:
                        self.night_combo.setCurrentIndex(i)
                    break
            self.foot_box.setChecked(True)

        QTimer.singleShot(1200, fire)

    def _night_has_wcs(self, night) -> bool:
        """这一夜有没有**已经解算过**的 sub(只查缓存,不去解算)。"""
        from astro_smb_app import guidequality as gq

        factory = self.shell.client_factory
        if factory is None:
            return False
        try:
            feet = with_client(factory, lambda c: gq.collect_footprints(
                c, list(night["targets"]), share=self.shell.logstore.share,
                cache=self._wcs_cache))
        except Exception:              # noqa: BLE001 - 探测失败当没有
            return False
        return bool(feet)

    def _toggle_foot(self, on: bool) -> None:
        if not on:
            self._push_foot([])
            return
        night = self._night() if self.nights else None
        if not night:
            return
        key = night["date"]
        got = self._foot_cache.get(key)
        if got is not None:
            self._push_foot(got)
            return
        factory = self.shell.client_factory
        if factory is None:
            self.shell.notice(_("先连接设备"))
            return
        share = self.shell.logstore.share
        targets = list(night["targets"])
        self.shell.notice(_("正在读取实际视场(只用已解算过的)…"))

        def work():
            from astro_smb_app import guidequality as gq

            def go(client):
                return gq.collect_footprints(
                    client, targets, share=share, cache=self._wcs_cache)

            return with_client(factory, go)

        def done(feet):
            self._foot_cache[key] = feet
            if not feet:
                self.shell.notice(
                    _("这一夜还没有解算过的 sub —— 足迹要先在影像查看页或导星质量分析里解算过才有"))
            else:
                # **成功也要把横幅收掉。** 原来只有失败分支改横幅,于是读完
                # 之后"正在读取实际视场…"一直挂在那儿,看着像卡住了。
                self.shell.notice(_("实际视场 {0} 张").format(len(feet)))
            self._push_foot(feet)

        self.bg.run(work, on_done=done,
                    on_error=lambda e: self.report(e, _("读取实际视场")))

    def _push_select(self) -> None:
        """把"选中了谁"推给球。空字符串 = 取消选中。"""
        if self._web_post is None or not self._web_ready:
            return
        self._web_post({"type": "targetSelect", "name": self.selected or ""})

    def _push_foot(self, feet) -> None:
        if self._web_post is None or not self._web_ready:
            return
        self._web_post({"type": "footprints", "items": list(feet or ()),
                        "show": bool(feet)})

    def _aim_initial(self, night, lat: float, lon: float, ts: float) -> None:
        """初始视角对准**当时最高的那个目标**。

        直接回天顶往往一个目标都看不到 —— 真机上打开这一页就是一片空白星区,
        用户会以为"目标没画出来"(老 UI 为此专门有 `_push_initial_view`)。
        """
        from astro_smb import astro

        targets = night["targets"]
        if not targets:
            self._web_post({"type": "reset"})
            return
        best = max(targets, key=lambda t: astro.altaz(
            float(t["ra"]), float(t["dec"]), lat, lon, ts)[0])
        self._web_post({"type": "view", "ra": best["ra"], "dec": best["dec"],
                        "fov": 72, "animate": False})

    def _site(self) -> tuple[float, float]:
        from astro_smb_app.logstore import load_site

        site = load_site()
        lat = float(site.get("lat", 30.0))
        lon = getattr(self.data, "lon_estimate", None)
        return lat, float(lon if lon is not None else site.get("lon", 121.0))

    def _update_horizon(self) -> None:
        """地平圈 = 与天顶垂直的大圆。天顶 = (LST, 站点纬度)。"""
        from astro_smb_app.logstore import load_site

        night = self._night()
        ts = night["ts0"] + (night["ts1"] - night["ts0"]) * self._frac
        lat, lon = self._site()
        ra_z = lst_deg(ts, lon) % 360.0
        self.sphere.zenith = (ra_z, lat)

        z = _unit(ra_z, lat)
        # 与 z 垂直的两个单位向量
        ax, ay = -z[1], z[0]
        n = math.hypot(ax, ay) or 1e-9
        a = (ax / n, ay / n, 0.0)
        b = (z[1] * a[2] - z[2] * a[1], z[2] * a[0] - z[0] * a[2],
             z[0] * a[1] - z[1] * a[0])
        pts = []
        for k in range(0, 361, 3):
            t = math.radians(k)
            v = tuple(math.cos(t) * a[i] + math.sin(t) * b[i] for i in range(3))
            dec = math.degrees(math.asin(max(-1.0, min(1.0, v[2]))))
            ra = math.degrees(math.atan2(v[1], v[0])) % 360.0
            pts.append((ra, dec))
        self.sphere.horizon = pts
        import datetime as _dt

        self.time_label.setText(
            _("时刻 {0:%m-%d %H:%M}(拖滑杆看整夜)").format(_dt.datetime.fromtimestamp(ts)))

    def _render_detail(self) -> None:
        self.detail.clear()
        body = self.detail.body
        t = next((x for x in self._night()["targets"]
                  if x["name"] == self.selected), None)
        if t is None:
            body.addWidget(W.label(_("点一个目标查看详情"), role="dim", wrap=True))
            body.addStretch(1)
            return
        body.addWidget(W.label(str(t["name"]), role="title", wrap=True))
        from astro_smb import astro

        body.addWidget(W.MetricRow(_("坐标"),
                                   f"{astro.format_ra(t['ra'])}  "
                                   f"{astro.format_dec(t['dec'])}", mono=True))
        # **坐标来源要写出来。** 日志里的是 goto 请求值,与实际指向差着指向
        # 模型误差(实测恒差 21′);把两者混为一谈会让"走没走"的判断整个偏掉。
        body.addWidget(W.MetricRow(_("坐标来源"), str(t["source"])))
        body.addWidget(W.MetricRow(_("帧数"), f"{t['frames']}"))
        body.addWidget(W.MetricRow(_("积分"), _("{0:.2f} 小时").format(t['exposure'] / 3600)))
        body.addWidget(W.MetricRow(
            _("时段"), f"{t['t0']:%H:%M} ~ {t['t1']:%H:%M}"))
        # **高度角与方位**:"这个目标那个时刻离地平多高"正是这一页的用处,
        # 而原来整张详情里一个字都没有。方位名走共享层的 16 向映射。
        lat, lon = self._site()
        night = self._night()
        ts = night["ts0"] + (night["ts1"] - night["ts0"]) * self._frac
        alt, az = astro.altaz(float(t["ra"]), float(t["dec"]), lat, lon, ts)
        # **判读阈值只有一份。** 这里原来自己写了 20/35,而浏览页详情走的是
        # `_alt_tone` 的 20/40 —— 37° 在一页是琥珀、另一页是绿,同一个值两种
        # 结论。阈值分叉一律靠"共用同一个函数"根治,不靠记得同步改两处。
        tone = bv._alt_tone(alt)
        body.addWidget(W.MetricRow(_("高度角"), f"{alt:.1f}°", tone=tone))
        body.addWidget(W.Gauge(max(0.0, min(1.0, alt / 90.0)), tone=tone,
                               ticks=W.ALT_TICKS, span=90.0))
        body.addWidget(W.MetricRow(_("方位"), f"{az:.0f}° ({bv._az_name(az)})"))
        if t.get("plans"):
            body.addWidget(W.MetricRow(
                _("计划"), " / ".join(str(x) for x in t["plans"])))
        body.addStretch(1)

    # ------------------------------------------------------------ 交互

    def on_close(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None

    def _pick_night(self, idx: int) -> None:
        self.night_index = idx
        self.selected = ""
        self._aimed = False          # 换一夜要重新对准最高的那个目标
        self._render()

    def _pick_target(self, name: str, *, fly: bool = True) -> None:
        """选中一个目标。**要推给球**,不然点了列表球上毫无反应。

        `fly` 在"球上点过来"那条路径上要给 False —— 镜头已经在那儿了,
        再飞一次是自己跟自己打架(老 UI `_select_target(name, fly=…)` 同款)。
        """
        self.selected = name
        self.sphere.selected = name
        self.sphere.update()
        self.table.select_key(name)
        # **球上也要看得出选了谁。** 原来只有相机飞过去,标记本身一点没变 ——
        # 镜头一动别的地方,就再也找不到"我选的是哪一个"了。降级的正射球
        # 一直是有选中环的,web 这条漏了(独立验收 4.2 判的就是这条)。
        self._push_select()
        if fly and self._web_post is not None and self._web_ready:
            t = next((x for x in self._night()["targets"]
                      if x["name"] == name), None)
            if t is not None:
                self._web_post({"type": "view", "ra": t["ra"], "dec": t["dec"],
                                "fov": 34, "animate": True})
        self._render_detail()

    def _set_frac(self, value: int) -> None:
        self._frac = value / 1000.0
        if self.nights:
            self._update_horizon()
            self.sphere.update()
            self._push_web()

    def _face_selected(self) -> None:
        """把相机转到选中目标正对着 —— 拖着找一个点很累。"""
        t = next((x for x in self._night()["targets"]
                  if x["name"] == self.selected), None)
        if t is None:
            self.shell.notice(_("先选一个目标"))
            return
        self.sphere.ra0 = float(t["ra"])
        self.sphere.dec0 = max(-85.0, min(85.0, float(t["dec"])))
        self.sphere.update()
        if self._web_post is not None and self._web_ready:
            self._web_post({"type": "view", "ra": t["ra"], "dec": t["dec"]})
