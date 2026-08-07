"""影像查看:单张 FITS 的拉伸预览、直方图、判读卡与板解算。

这一页的形状由**一条设备事实**决定:一张 light 帧是 49.77 MB,而 SMB 单流
约 6 MiB/s —— 打开一张要等十来秒。所以:

* 下载**必须有确定式进度条并写出 MB 数**。只给一个转圈的话,用户分不清
  是在下载、卡住了、还是快好了。
* 下载 → 解码 → 拉伸整条链路都在工作线程上,只把结果 `QPixmap` 交给 UI。
  一张 6248×4176 的解码 + 百分位拉伸在 GUI 线程上跑就是几秒的白屏。

判读与格式化一律走共享层(`views.fitsview` / `views.browser`):气量用的是
Pickering (2002)、高度角与采样的阈值都是有前提的经验值。**这一页只负责摆。**

两条判读上的坑照抄进注释,免得后来的人"顺手修正":

* **FITS 头里的 RA/DEC 是赤道仪编码器读数**,不是实际指向 —— 实测与板解算
  中心恒差 21′。要"实际指向"就得用板解算中心。
* **场旋的符号不能直接和正演比**:ASIAIR 的 light 帧恒为镜像,镜像把旋向
  整个翻过来了。
"""
from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QProgressBar, QSplitter, QWidget

from astro_smb_app.views import browser as bv
from astro_smb_app.views import fitsview as fv
from astro_smb_qt import theme, widgets as W
from astro_smb_qt.pages.base import Page, PageHeader, page_layout
from astro_smb_qt.workers import with_client
from astro_smb.i18n import N_, gettext as _

log = logging.getLogger(__name__)

#: 拉伸模式。文案与共享层的 `_MODES` 一一对应,顺序不能乱。
MODES = [("stf", N_("自动拉伸(STF)")), ("asinh", "asinh"),
         ("percentile", N_("百分位 0.2%–99.8%"))]

VIEW_W, VIEW_H = 820, 560

#: 滑杆整数刻度 → 实值。Qt 的 QSlider 只有整数,而这几个参数都是小数。
_SLIDERS = {
    # 键: (标签, 最小, 最大, 每格, 单位后缀)
    "shadows_clipping": (N_("阴影裁切"), -600, 0, 100.0, " σ"),
    "target_background": (N_("目标背景"), 1, 90, 100.0, ""),
    "asinh_a": (N_("asinh 强度"), 1, 1000, 1.0, ""),
    "lo_pct": (N_("下限"), 0, 200, 100.0, "%"),
    "hi_pct": (N_("上限"), 9000, 10000, 100.0, "%"),
}
#: 每档拉伸各自可调的参数(与 `StretchParams.fingerprint` 的分档一致)
_MODE_KNOBS = {"stf": ("shadows_clipping", "target_background"),
               "asinh": ("asinh_a", "shadows_clipping"),
               "percentile": ("lo_pct", "hi_pct")}


def _default_params():
    from astro_smb.fitsimage import StretchParams

    return StretchParams()
HIST_W, HIST_H = 300, 130


class FitsViewPage(Page):
    TITLE = N_("影像查看")
    SUBTITLE = N_("单张 FITS 的拉伸预览、直方图与星点")

    def __init__(self, shell):
        super().__init__(shell)
        self.share = ""
        self.path = ""
        self.mode = 0
        self.model: dict = {}
        self._local: Path | None = None
        self._hdr = None
        #: 线性图像(重新拉伸不必重下)。一张 6248×4176 约 100 MB,
        #: 但重下一次是十几秒 —— 拖滑杆时那个代价完全不可接受。
        self._img = None
        self._params = _default_params()
        self._show_before = False
        self._cancel = None
        self._build()

    # ------------------------------------------------------------ 界面

    def _build(self) -> None:
        root = page_layout(self)
        self.header = PageHeader(_(self.TITLE), _(self.SUBTITLE))
        self.mode_combo = W.combo([_(t) for _k, t in MODES],
                                  on_change=self._set_mode)
        self.header.add_tool(self.mode_combo)
        self.solve_btn = W.button(_("板解算"), on_click=self._solve, enabled=False)
        self.header.add_tool(self.solve_btn)
        #: 「下载星表」按钮 —— 只在星表缺失时才建出来(见 `_offer_catalog`)
        self.catalog_btn = None
        #: 下一次重渲染要不要把板解算那一段滚进视野
        self._scroll_to_solve = False
        #: 是否已挂上侧栏范围变化的一次性回调(见 `_disconnect_range`)
        self._range_hooked = False
        self.header.add_tool(W.button(_("重新加载"), on_click=self.reload))
        self.header.add_tool(W.button(_("在浏览页中显示"),
                                      on_click=self._show_in_browser))
        self.name_chip = W.StatusChip("", "accent")
        self.header.add_action(self.name_chip)
        root.addWidget(self.header)

        body = QWidget()
        col = W.vbox(body, gap="card")

        # 进度:**确定式**,而且写出 MB —— 一张 50MB 的图要等十来秒
        self.prog_card = W.Card(_("正在打开"), "")
        self.prog = QProgressBar()
        self.prog.setRange(0, 1000)
        self.prog.setTextVisible(False)
        self.prog_card.add(self.prog)
        self.prog_text = W.label("", role="subtitle")
        self.prog_card.add(self.prog_text)
        self.prog_card.setVisible(False)
        col.addWidget(self.prog_card)

        split = QSplitter(Qt.Horizontal)
        split.setChildrenCollapsible(False)
        img_card = W.Card(_("影像"), _("拉伸后的显示图 —— 不是原始像素"))
        self.img_card = img_card
        # **可缩放可平移。** 定尺寸的话 6248×4176 被压到 820px 宽,
        # 星点是圆是扁根本看不出来 —— 而那是判断导星好坏的直接证据。
        self.image = W.ZoomView()
        self.image.hovered.connect(self._on_hover)
        self.image.zoomed.connect(self._on_zoom)
        img_card.add(self.image, 1)

        zrow = W.hbox(gap="sm")
        zrow.addWidget(W.button(_("适应窗口"), on_click=self.image.fit))
        zrow.addWidget(W.button("1:1", on_click=self.image.actual_size))
        zrow.addWidget(W.button("−", on_click=lambda: self.image.set_zoom(
            self.image.zoom / 1.25)))
        zrow.addWidget(W.button("+", on_click=lambda: self.image.set_zoom(
            self.image.zoom * 1.25)))
        self.zoom_label = W.label("—", role="subtitle")
        zrow.addWidget(self.zoom_label)
        self.star_box = W.check(_("星点叠加"), on_change=self.image.show_stars)
        self.star_box.setEnabled(False)
        zrow.addWidget(self.star_box)
        zrow.addStretch(1)
        zrow.addWidget(W.button(_("另存为 PNG"), on_click=self._save_png))
        img_card.add_layout(zrow)
        # **像素读数**:悬停给图像坐标与该点的原始值(不是拉伸后的)
        self.pix_label = W.label(_("把鼠标放到图上看像素值"), role="faint",
                                 wrap=True)
        img_card.add(self.pix_label)
        split.addWidget(img_card)

        side = W.Card(_("判读"), _("与浏览页详情同一份判读"))
        self.side = W.Scroll(gap="sm")
        self.side.setMinimumWidth(340)
        side.add(self.side, 1)
        split.addWidget(side)
        split.setStretchFactor(0, 1)
        split.setSizes([880, 380])
        col.addWidget(split, 1)

        col.addWidget(self._knob_card())

        self.state = W.StateStack(body)
        root.addWidget(self.state, 1)
        self.state.show_empty(
            _("还没有打开影像"),
            _("在浏览页选一张 .fit,点详情里的「在影像查看中打开」。"))

    def _knob_card(self):
        """每档拉伸的参数滑杆(老 UI 有,这边原来一个都没有)。

        **只显示当前档用得到的那几个** —— 在 STF 档下拖 asinh 强度既不会
        改变画面也不会失效缓存(`StretchParams.fingerprint` 按档分字段),
        摆在那儿只会让人以为它坏了。
        """
        from PySide6.QtCore import Qt as _Qt
        from PySide6.QtWidgets import QSlider

        card = W.Card(_("拉伸参数"), _("拖动即时重渲染 —— 不重新下载"))
        self._knob_row = W.hbox(gap="lg")
        self._knobs: dict = {}
        for key, (label, lo, hi, div, suffix) in _SLIDERS.items():
            label = _(label)         # 表里是 msgid,取用时才翻
            box = W.vbox(gap="none")
            cap = W.label(label, role="subtitle")
            sl = QSlider(_Qt.Horizontal)
            sl.setRange(lo, hi)
            sl.setFixedWidth(150)
            val = W.label("", role="faint")
            sl.valueChanged.connect(
                lambda v, k=key: self._set_knob(k, v))
            box.addWidget(cap)
            box.addWidget(sl)
            box.addWidget(val)
            holder = W.wrap(box)
            self._knobs[key] = (holder, sl, val, div, suffix)
            self._knob_row.addWidget(holder)
        self.link_box = W.check(_("通道链接"), on_change=self._set_linked)
        self.link_box.setToolTip(_("关掉 = 每个通道各自拉伸(相当于自动白平衡)"))
        self._knob_row.addWidget(self.link_box)
        self._knob_row.addStretch(1)
        card.add_layout(self._knob_row)
        self._sync_knobs()
        return card

    def _sync_knobs(self) -> None:
        """按当前档显示/隐藏滑杆,并把值刷成 `self._params` 的实值。"""
        live = _MODE_KNOBS.get(MODES[self.mode][0], ())
        for key, (holder, sl, val, div, suffix) in self._knobs.items():
            W.show_if(holder, key in live)
            if key not in live:
                continue
            cur = float(getattr(self._params, key))
            sl.blockSignals(True)
            sl.setValue(int(round(cur * div)))
            sl.blockSignals(False)
            val.setText(f"{cur:g}{suffix}")

    def _set_knob(self, key: str, raw: int) -> None:
        import dataclasses

        _holder, _sl, val, div, suffix = self._knobs[key]
        v = raw / div
        val.setText(f"{v:g}{suffix}")
        self._params = dataclasses.replace(self._params, **{key: v})
        self._restretch()

    def _set_linked(self, on: bool) -> None:
        import dataclasses

        self._params = dataclasses.replace(self._params, linked=bool(on))
        self._restretch()

    def _restretch(self) -> None:
        """**只重拉伸,不重下载。** 图已经在内存里了。"""
        if self._img is None:
            return
        gen = self.bg.bump()
        img, params = self._img, self._params

        def work():
            from astro_smb.fitsimage import stretch
            from PIL import Image as PILImage

            rgb8, _stats = stretch(img.rgb, params, unit=img.unit,
                                   mono_out=True)
            out = fv._render_dir() / f"qt_{params.fingerprint()}.bmp"
            PILImage.fromarray(
                rgb8, mode="L" if rgb8.ndim == 2 else "RGB").save(out)
            return str(out)

        self.bg.run(work, gen=gen,
                    on_done=lambda p: self.image.set_path(p),
                    on_error=lambda e: self.report(e, _("重新拉伸")))

    # -- 缩放 / 读数 --------------------------------------------------

    def _on_zoom(self, z: float) -> None:
        self.zoom_label.setText(f"{z * 100:.0f}%")

    def _on_hover(self, x: int, y: int) -> None:
        """像素读数。**报的是原始线性值**,不是拉伸后的显示值 ——
        拉伸是为了看得见,判读要看原始数。"""
        if x < 0 or self._img is None:
            self.pix_label.setText(_("把鼠标放到图上看像素值"))
            return
        try:
            arr = self._img.rgb
            v = arr[y, x]
        except Exception:                    # noqa: BLE001
            self.pix_label.setText(f"({x}, {y})")
            return
        try:
            txt = " / ".join(f"{float(c):.6g}" for c in v)
        except TypeError:
            txt = f"{float(v):.6g}"
        self.pix_label.setText(
            _("({x}, {y})   原始值 {txt}   单位 {0}").format(
                self._img.unit or '—', x=x, y=y, txt=txt))

    def _save_png(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        src = self.model.get("image")
        if not src:
            self.shell.notice(_("还没有可保存的图"))
            return
        dest, _f = QFileDialog.getSaveFileName(
            self, _("另存为 PNG"), str(Path.home() / "fits.png"),
            "PNG (*.png)")
        if not dest:
            return
        from PIL import Image as PILImage

        try:
            PILImage.open(src).save(dest)
        except Exception as exc:             # noqa: BLE001
            self.report(exc, _("另存为 PNG"))
            return
        self.shell.notice(_("已保存到 {dest}").format(dest=dest))

    def _show_in_browser(self) -> None:
        if not self.share:
            self.shell.notice(_("还没有打开影像"))
            return
        parent = self.path.rsplit("\\", 1)[0] if "\\" in self.path else ""
        self.shell.open_browser_path(self.share, parent)

    # ------------------------------------------------------------ 契约

    def open(self, share: str, path: str) -> None:
        """外壳的 `open_fits` 调这里。"""
        self.share, self.path = share, path
        self.shell.select_page("fits")
        self.reload()

    def on_connected(self, shares) -> None:
        self._local = None

    # ------------------------------------------------------------ 加载

    def reload(self) -> None:
        if not self.path or self.shell.client_factory is None:
            return
        gen = self.bg.bump()
        share, path = self.share, self.path
        factory = self.shell.client_factory
        mode = MODES[self.mode][0]
        self.state.show_content()
        W.show_if(self.prog_card, True)
        self.prog.setRange(0, 1000)
        self.prog.setValue(0)
        self.prog_text.setText(_("正在下载"))
        self.name_chip.set(path.rsplit("\\", 1)[-1], "accent")

        base_params = self._params

        def work(report):
            import dataclasses
            import tempfile

            from astro_smb.fitshdr import parse_fits_header
            from astro_smb.fitsimage import (StretchParams,
                                             geometry_from_header,
                                             load_linear, stretch)

            local = Path(tempfile.gettempdir()) / "astro-qt-fitsview.fit"

            def tick(done: int, total: int) -> None:
                # **`progress(已完成, 总量)` 是累计值不是增量。** 当成增量
                # 累加会让进度条冲到 200% 然后卡住,而且不报错 —— 签名是
                # 两个 int,长得一模一样。
                report(("dl", int(done), int(total)))

            with_client(factory,
                        lambda c: c.download_file(share, path, local,
                                                  progress=tick))
            report(("phase", _("正在解码并拉伸"), 0))
            hdr = parse_fits_header(local.read_bytes()[:65536])
            img = load_linear(local, hdr)
            params = dataclasses.replace(base_params, mode=mode)
            rgb8, _stats = stretch(img.rgb, params, unit=img.unit,
                                   mono_out=True)
            out = fv._render_dir() / f"qt_{params.fingerprint()}.bmp"
            from PIL import Image as PILImage

            PILImage.fromarray(
                rgb8, mode="L" if rgb8.ndim == 2 else "RGB").save(out)
            geom = geometry_from_header(hdr)
            return {
                # **线性图留着** —— 拖拉伸滑杆时重下一次是十几秒
                "img": img,
                "image": str(out), "local": str(local), "hdr": hdr,
                # **经度带上日志反推值**,否则同一张片子浏览页写 182°、
                # 这一页写 180°(只读 site.json 的兜底 120°E)。
                "astro": fv.fits_astro(
                    share, path, hdr,
                    lon=getattr(getattr(self.shell.logstore, "data", None),
                                "lon_estimate", None)),
                "structure": fv.fits_structure(geom, img),
                "hist": _histogram(img),
                # **拉伸前/后两份**:拉伸前看的是"数据本身长什么样"
                # (有没有削顶、背景在哪),拉伸后看的是"显示得对不对"。
                # 老 UI 有个切换,这边原来只有前者。
                "hist_after": _histogram_u8(rgb8),
                "header_lines": [f"{k:<8}= {v}"
                                 for k, v, _c in (hdr.order or ())],
                "badges": fv.fits_badges(hdr),
                "size": f"{geom.width} × {geom.height}",
                # **板解算要用它算视场那一行。** 原来只有格式化好的 `size`
                # 字符串,`_solve()` 里 `self.model.get("geom")` 恒为 None,
                # 于是 `solve_rows(res, 0, 0)` 静默少一行 —— 不报错,
                # 只是十一行变十行。
                "geom": geom,
            }

        self.bg.run(work, gen=gen, on_done=self._apply, on_error=self._fail,
                    on_progress=self._on_progress)

    def _on_progress(self, payload) -> None:
        kind, a, b = payload
        if kind == "phase":
            self.prog.setRange(0, 0)        # 不确定式:解码没有百分比
            self.prog_text.setText(str(a))
            return
        done, total = int(a), int(b)
        if total <= 0:
            self.prog.setRange(0, 0)
            return
        self.prog.setRange(0, 1000)
        self.prog.setValue(int(done / total * 1000))
        self.prog_text.setText(_("正在下载 {0:.1f} / {1:.1f} MB").format(
            done / 1000000.0, total / 1000000.0))

    def _apply(self, m: dict) -> None:
        self.model = m
        self._local = Path(m["local"])
        self._hdr = m["hdr"]
        self._img = m.get("img")
        self.prog_card.setVisible(False)
        self.solve_btn.setEnabled(True)
        self.image.set_path(m["image"])
        self.img_card.set_chip(m.get("size", ""), "accent")
        self._render_side()

    def _fail(self, exc: BaseException) -> None:
        self.prog_card.setVisible(False)
        self.state.show_empty(_("打开失败"), str(exc))
        self.report(exc, _("打开影像"))

    # ------------------------------------------------------------ 右栏

    def _render_side(self) -> None:
        self.side.clear()
        body = self.side.body
        m = self.model

        hist = m.get("hist_after" if self._show_before is False
                     and m.get("hist_after") else "hist")
        if hist:
            hrow = W.hbox(gap="sm")
            hrow.addWidget(W.label(_("直方图"), role="group"))
            hrow.addWidget(W.button(
                _("看拉伸前") if not self._show_before else _("看拉伸后"),
                kind="ghost", on_click=self._toggle_hist))
            hrow.addStretch(1)
            body.addLayout(hrow)
            body.addWidget(W.MultiHistogram(hist, HIST_W, HIST_H))

        astro = m.get("astro") or {}
        if astro.get("title"):
            body.addWidget(W.label(str(astro["title"]), role="title", wrap=True))
            if astro.get("sub"):
                body.addWidget(W.label(str(astro["sub"]), role="subtitle",
                                       wrap=True))
            if astro.get("badges"):
                row = W.hbox(gap="xs")
                for b in astro["badges"]:
                    row.addWidget(W.StatusChip(str(b), "accent"))
                row.addStretch(1)
                body.addLayout(row)
            group = ""
            for name, key, value, _tone in astro.get("rows") or ():
                if name != group:
                    group = name
                    body.addWidget(W.GroupHeader(name))   # 这一路没有 glyph
                body.addWidget(W.MetricRow(key, value))

        if m.get("structure"):
            body.addWidget(W.GroupHeader(_("影像结构"), W.GLYPH_STRUCTURE))
            for k, v in m["structure"]:
                body.addWidget(W.MetricRow(k, v))

        if m.get("solve") or m.get("solve_rows"):
            body.addWidget(W.GroupHeader(_("板解算"), W.GLYPH_SOLVE))
            if m.get("solve"):          # 忙态那一行文字
                body.addWidget(W.label(str(m["solve"]), role="body", wrap=True))
            # **一行一项**(老 UI 11 行)。压成一行会丢掉「离先验中心」——
            # FITS 头里的 RA/DEC 是编码器读数,与解算中心恒差约 21′,
            # 而那个数正是判断"指向模型同步没同步回去"的依据。
            for k, v in m.get("solve_rows") or ():
                body.addWidget(W.MetricRow(str(k), str(v)))
            # 星表不在时给一个明确的动作入口 —— 光说"未就绪"没有用,
            # 用户得知道从哪儿把它弄来
            if m.get("catalog_offer"):
                self.catalog_btn = W.button(_("下载星表"), kind="primary",
                                            on_click=self._download_catalog)
                row = W.hbox(gap="sm")
                row.addWidget(self.catalog_btn)
                row.addStretch(1)
                body.addLayout(row)
            # **把这一段滚进视野。** 板解算在这条长侧栏的最下面,默认是在
            # 折叠线以下的 —— 点了「板解算」屏幕上什么都不变,看起来就是
            # "点了没反应"(用户报的"星表没自动下载"有一半是这个观感)。
            if self._scroll_to_solve:
                self._scroll_to_solve = False
                QTimer.singleShot(0, self._show_solve_area)

        act = W.hbox(gap="sm")
        act.addWidget(W.button(_("复制全部信息"), on_click=self._copy_all))
        act.addStretch(1)
        body.addLayout(act)

        if m.get("header_lines"):
            row = W.hbox(gap="sm")
            row.addWidget(W.button(
                _("完整 FITS 头({0} 卡)").format(len(m['header_lines'])),
                on_click=self._show_header))
            row.addStretch(1)
            body.addLayout(row)
        body.addStretch(1)

    def _toggle_hist(self) -> None:
        self._show_before = not self._show_before
        self._render_side()

    def _copy_all(self) -> None:
        """把这一页能读到的东西整份复制走 —— 贴到笔记里对账用。"""
        from PySide6.QtWidgets import QApplication

        m = self.model
        astro = m.get("astro") or {}
        lines = [f"{self.share}\\{self.path}", ""]
        if astro.get("title"):
            lines += [str(astro["title"]), str(astro.get("sub") or ""), ""]
        for k, v, *_r in (astro.get("rows") or ()):
            lines.append(f"{k}: {v}")
        for k, v in (m.get("structure") or ()):
            lines.append(f"{k}: {v}")
        for k, v in (m.get("solve_rows") or ()):
            lines.append(f"{k}: {v}")
        QApplication.clipboard().setText("\n".join(lines))
        self.shell.notice(_("已复制"))

    def _show_header(self) -> None:
        lines = self.model.get("header_lines") or []
        if lines:
            W.TextDialog(self, _("完整 FITS 头"), "\n".join(lines)).exec()

    # ------------------------------------------------------------ 交互

    def _set_mode(self, idx: int) -> None:
        """换档**只重拉伸**,不重下载 —— 图已经在内存里了。

        原来是 `self.reload()`:每换一档就把 50 MB 重拉一遍,十几秒。
        """
        import dataclasses

        if idx == self.mode:
            return
        self.mode = idx
        self._params = dataclasses.replace(self._params, mode=MODES[idx][0])
        self._sync_knobs()
        if self._img is not None:
            self._restretch()
        elif self.path:
            self.reload()

    def _solve(self) -> None:
        """板解算 —— 在**本地那份**上跑,先验从 FITS 头取。"""
        if self._local is None or self._hdr is None:
            return
        # **新机器上星表还不在。** 老 UI 这里先查一次、查不到就把话说清楚
        # 再由用户点「下载星表」;Qt 这条一直没接,于是新机器上点解算
        # 直接抛到 fail(),面板上写的是
        # 「解算失败:无法读取星表文件: [Errno 2] No such file or directory」——
        # 一句 errno,既没说缺的是星表,也没给获取的路。
        if not self._catalog_ready():
            self._offer_catalog()
            return
        gen = self.bg.bump()
        self.solve_btn.setEnabled(False)
        self._scroll_to_solve = True
        self.model["solve"] = _("正在解算…")
        self.model["solve_rows"] = []
        self._render_side()
        local = self._local

        def work():
            from astro_smb import platesolve

            # `solve_file` 自己读头装配先验(`SolveHint.from_header`)——
            # **只读若干条行带**,不是整幅 52 MB。
            return platesolve.solve_file(local)

        def done(res):
            self.solve_btn.setEnabled(True)
            # **先把结果写回去,再做锦上添花的事。**
            #
            # 原来的顺序反过来:先铺星点叠加、再写 `solve_rows`。星点那步
            # 一抛异常(实测必抛,见 `ZoomView.set_stars`),解算结果就整个
            # 丢了 —— 而按钮已经在上面恢复成可点,于是界面看着像"解算完了",
            # 面板却永远冻在「正在解算…」。**主结果不能被可选装饰拖下水。**
            geom = self.model.get("geom")
            # **一行一项**,不是压成一行 —— 老 UI 是 11 行,
            # 压扁会丢掉「离先验中心」(那 21′ 的判读)、星点形状、视场。
            self.model["solve_rows"] = fv.solve_rows(
                res, getattr(geom, "width", 0) or 0,
                getattr(geom, "height", 0) or 0)
            self.model["solve"] = ""
            self._render_side()
            # **星点叠加**(5.9):把匹配上的星标在图上 —— 要看的就是
            # "这颗星在这张图的这个位置上是什么形状"。
            try:
                xy = getattr(res, "matched_xy", None)
                if xy is not None and len(xy):
                    self.image.set_stars(xy, show=False)
                    self.star_box.setEnabled(True)
            except Exception:              # noqa: BLE001 - 叠加坏了不该拖垮解算
                log.exception(_("星点叠加失败"))

        def fail(exc):
            self.solve_btn.setEnabled(True)
            self.model["solve_rows"] = [(_("结果"), _("解算失败:{exc}").format(exc=exc))]
            self.model["solve"] = ""
            self._render_side()

        self.bg.run(work, gen=gen, on_done=done, on_error=fail)

    # ------------------------------------------------------------ 星表

    def _show_solve_area(self) -> None:
        """把侧栏滚到底 —— 板解算那一段就在最下面。

        **不能只读一次 `maximum()`。** 解算结果让侧栏内容从 168px 长到
        452px,而 `QTimer.singleShot(0, …)` 跑的时候布局**还没按新内容
        重算** —— 读到的仍是上一次那个 168,于是只滚到一半:用户看到
        「板解算」标题加两行,剩下十行(包括「离先验中心」)还在折叠线
        以下。独立验收把这条抓出来了。

        所以先按当前已知范围滚一次(内容没变高时这就够了),再挂一次性的
        `rangeChanged`:等范围真的更新了再补滚到新的底。
        """
        bar = self.side.verticalScrollBar()
        bar.setValue(bar.maximum())
        self._disconnect_range()
        bar.rangeChanged.connect(self._on_side_range)
        self._range_hooked = True

    def _on_side_range(self, _lo: int, hi: int) -> None:
        """侧栏范围更新了 —— 补滚到新的底,然后**立刻断开**。

        不断开的话,以后任何一次内容变化(换图、切拉伸档)都会把用户
        滚到底,那是"界面自己乱跑"。
        """
        self.side.verticalScrollBar().setValue(hi)
        self._disconnect_range()

    def _disconnect_range(self) -> None:
        """**自己记着连没连过。** PySide 对"断一个没连上的信号"是发
        `RuntimeWarning`(不是抛异常),`try/except` 拦不住,只会在日志里
        刷噪声。一个布尔位比异常处理干净。"""
        if not self._range_hooked:
            return
        self._range_hooked = False
        self.side.verticalScrollBar().rangeChanged.disconnect(
            self._on_side_range)

    @staticmethod
    def _catalog_ready() -> bool:
        """本机有没有一份**校验通过**的星表(不只是"文件在")。"""
        try:
            from astro_smb import catalog

            return bool(catalog.catalog_available())
        except Exception:              # noqa: BLE001 - 查不动就当没有
            return False

    def _offer_catalog(self) -> None:
        """星表还没就绪:说清要取什么、多大,**由用户决定**。

        不自动下载 —— 这是一百多兆的网络流量,照本项目对巡天底图的同款口径
        (`_set_sky_bg` 也是先问再下),得有人点头。
        """
        self.solve_btn.setEnabled(True)
        self.model["solve"] = ""
        self.model["solve_rows"] = [
            (_("状态"), _("星表未就绪")),
            (_("来源"), "Tycho-2(CDS / VizieR)"),
            (_("说明"), _("解算需要 Tycho-2 星表。首次使用会从 CDS(星表的权威发布方)取原始数据并在本机构建,约 159 MB、构建几秒;之后常驻本机,解算不再联网。")),
        ]
        self.model["catalog_offer"] = True
        self._scroll_to_solve = True
        self._render_side()

    def _download_catalog(self) -> None:
        """按用户明确动作取星表;**完成后自动接着跑刚才那次解算**。"""
        gen = self.bg.bump()
        if self.catalog_btn is not None:
            self.catalog_btn.setEnabled(False)
        self.solve_btn.setEnabled(False)
        self._scroll_to_solve = True
        self.model["solve"] = _("正在获取星表…")
        self.model["solve_rows"] = []
        self._render_side()

        def work(report):
            # `Bg.run` 给了 `on_progress` 时,`fn` 收的就是这个 `report`
            # (一个参数)。而 `ensure_catalog` 的 progress 是 (done, total)
            # 两个参数 —— 中间要转一道,别直接把 report 递进去。
            from astro_smb import catalog

            return catalog.ensure_catalog(
                progress=lambda done_n, total: report((done_n, total)))

        def done(_path):
            self.model["catalog_offer"] = False
            self.model["solve"] = ""
            self._render_side()
            self.solve_btn.setEnabled(True)
            self._solve()              # 接着做用户本来要做的事

        def fail(exc):
            self.solve_btn.setEnabled(True)
            self.model["solve"] = ""
            self.model["solve_rows"] = [(_("结果"), _("星表获取失败:{exc}").format(exc=exc))]
            # 失败了还要留着入口能重试 —— `_render_side` 会照 `catalog_offer`
            # 把按钮重新建出来
            self.model["catalog_offer"] = True
            self._render_side()

        self.bg.run(work, gen=gen, on_done=done, on_error=fail,
                    on_progress=self._catalog_progress)

    def _catalog_progress(self, payload) -> None:
        done_n, total = payload
        if total > 0:
            self.model["solve"] = (
                _("正在获取星表… {0:.0f} / {1:.0f} MB").format(
                    done_n / 1000000.0, total / 1000000.0))
        else:
            self.model["solve"] = _("正在获取星表… {0:.0f} MB").format(done_n / 1000000.0)
        self._render_side()


def _histogram_u8(rgb8) -> list[list[float]] | None:
    """拉伸**之后**那张 8 位图的直方图。

    与拉伸前那张读法不同:前者看"数据本身长什么样"(有没有削顶、
    背景落在哪),后者看"显示得对不对"(有没有把暗部压死或把亮部烧掉)。
    """
    import numpy as np

    arr = np.asarray(rgb8)
    if arr.size == 0:
        return None
    chans = [arr] if arr.ndim == 2 else [arr[..., i]
                                         for i in range(min(3, arr.shape[-1]))]
    out = []
    for ch in chans:
        h, _edges = np.histogram(ch, bins=128, range=(0, 255))
        top = float(h.max()) or 1.0
        out.append([float(v) / top for v in h])
    return out


def _histogram(img) -> list[list[float]] | None:
    """每通道的直方图(已归一化)。多通道**半透明叠画** —— 合成单条会让
    重叠处不再叠色,而叠色正是这张图的读法(哪个通道饱和了)。"""
    import numpy as np

    rgb = getattr(img, "rgb", None)
    if rgb is None:
        return None
    arr = np.asarray(rgb)
    chans = [arr] if arr.ndim == 2 else [arr[..., i]
                                         for i in range(min(3, arr.shape[-1]))]
    out = []
    for ch in chans:
        h, _edges = np.histogram(ch[np.isfinite(ch)], bins=128)
        top = float(h.max()) or 1.0
        out.append([float(v) / top for v in h])
    return out
