"""设备管理页:已记录的设备 + 本机卷 + 手动添加。

视图模型全部来自 ``astro_smb_app.views.devices`` —— **与另外两套前端消费的是
同一份**,所以三边的"12 分钟前"、"占 87%"、"● 端口可达 5 ms" 永远一致。
这一页只负责把那些 dict 摆成卡片。

卡片的身份用 ``key``(视图模型给的稳定键:SMB 卡是 host、本地卡是路径),
不是列表下标 —— 设备增删时下标会整体错位。
"""
from __future__ import annotations

from PySide6.QtWidgets import QWidget

from astro_smb import devices as devices_store
from astro_smb.i18n import N_, gettext as _
from astro_smb_app import volumes as volumes_mod
from astro_smb_app.views import devices as dv
from astro_smb_qt import models, widgets as W
from astro_smb_qt.pages.base import Page, PageHeader, page_layout


class DevicesPage(Page):
    TITLE = N_("设备管理")
    SUBTITLE = N_("本机记录的设备、插着的存储卡,以及手动添加")

    def __init__(self, shell):
        super().__init__(shell)
        #: 存活探测结果 host → 毫秒(None = 不可达)。**只敢说"端口可达"** ——
        #: 路由器会对整网段的 445 秒回 ACK(docs/DEVELOPMENT.md §2)。
        self._rtt: dict = {}
        self._rtt_ts: float = 0.0
        #: 本地路径 → `local_facts()` 采到的事实(容量 / ZWO 命中 / 所在卷)
        self._facts: dict = {}
        self._present: dict = {}
        self._facts_ts: float = 0.0
        self._build()

    def _build(self) -> None:
        root = page_layout(self)
        self.header = PageHeader(_(self.TITLE), _(self.SUBTITLE))
        self.header.add_action(W.button(_("刷新"), on_click=self.reload))
        root.addWidget(self.header)
        root.addWidget(self._manual_card())
        self.scroll = W.Scroll(gap="card")
        root.addWidget(self.scroll, 1)
        self.reload()

    def _manual_card(self) -> QWidget:
        card = W.Card(_("手动添加"), _("添加只写入本机的设备记录,不会改动设备上的任何文件"))
        row = W.hbox(gap="sm")
        self.manual = W.line_edit(
            _('SMB 地址(192.0.2.227)或本地文件夹(E:\\ 、D:\\ASIAIR)'),
            on_return=self._add)
        row.addWidget(self.manual, 1)
        row.addWidget(W.button(_("添加"), kind="primary", on_click=self._add))
        card.add_layout(row)
        return card

    # ------------------------------------------------------------ 契约

    def _probe_alive(self) -> None:
        """对**已记录的每一台** SMB 设备探一次 TCP 445。

        上限 12 台(老 UI 同一条)—— 再多就变成一次小型扫描,而这一页只是
        "看看它们还在不在"。整轮在工作线程上跑,超时 3 秒。
        """
        recs = [r for r in devices_store.load()
                if r.get("kind") != devices_store.KIND_LOCAL][:12]
        if not recs:
            return
        hosts = [r.get("host", "") for r in recs if r.get("host")]
        gen = self.bg.bump()

        def work():
            import time

            from astro_smb.client import AstroSmbClient

            out = {}
            for h in hosts:
                try:
                    out[h] = AstroSmbClient(host=h, timeout=3).ping_tcp()
                except Exception:            # noqa: BLE001
                    out[h] = None
            return out, time.time()

        def done(payload):
            self._rtt, self._rtt_ts = payload
            self.reload()

        self.bg.run(work, gen=gen, on_done=done, on_error=lambda _e: None)

    def _probe_local(self) -> None:
        """采一轮**本地记录**的事实:所在卷、容量、ZWO 特征命中、插没插。

        原来一次都没采 —— `local_card(rec)` 光秃秃地调,于是容量整组、
        ZWO 特征、"已插入"状态全都不出现,状态恒「○ 未检测」。
        碰文件系统(`os.path.isdir` 对拔掉的卡会慢),放工作线程。
        """
        roots = [r.get("host", "") for r in devices_store.load()
                 if r.get("kind") == devices_store.KIND_LOCAL and r.get("host")]
        if not roots:
            return
        gen = self.bg.bump()

        def work():
            import os
            import time

            vols = {}
            try:
                for v in volumes_mod.list_volumes():
                    vols[str(v.path).rstrip("\\/").lower()] = v
            except Exception:                # noqa: BLE001
                pass
            facts, present = {}, {}
            for root in roots:
                here = None
                low = str(root).rstrip("\\/").lower()
                for key, v in vols.items():
                    if low == key or low.startswith(key + "\\"):
                        here = v
                        break
                ok = False
                try:
                    ok = os.path.isdir(root)
                except OSError:
                    ok = False
                present[root] = ok
                # ZWO 特征命中走 `volumes.scan_root`(老 UI 同一条)——
                # 卡不在时**不要挂上一次的命中**,那会让人以为卡还在
                hits = volumes_mod.scan_root(root)[0] if ok else None
                facts[root] = dv.local_facts(root, here, present=ok, hits=hits)
            return facts, present, time.time()

        def done(payload):
            self._facts, self._present, self._facts_ts = payload
            self.reload()

        self.bg.run(work, gen=gen, on_done=done, on_error=lambda _e: None)

    def on_show(self) -> None:
        self.reload()
        self._probe_alive()      # 每次进这一页探一轮
        self._probe_local()

    def on_connected(self, shares) -> None:
        self.reload()

    # ------------------------------------------------------------ 内容

    def reload(self) -> None:
        self.scroll.clear()
        cards = self._cards()
        if not cards:
            self.scroll.body.addWidget(W.EmptyState(
                _("还没有记录任何设备"),
                _("去「扫描设备」页点「开始扫描」找 ASIAIR,或者把存储卡插到电脑上 —— 这里会自动认出来。"),
                action=_("去扫描"), on_action=lambda: self.shell.select_page("scan")))
            self.scroll.body.addStretch(1)
            return
        for c in cards:
            self.scroll.body.addWidget(self._card(c))
        self.scroll.body.addStretch(1)
        self.header.set_subtitle(_("{0} · 共 {1} 张").format(_(self.SUBTITLE), len(cards)))

    def _cards(self) -> list[dict]:
        out: list[dict] = []
        conn = self.shell.conn
        for rec in dv.sorted_records(devices_store.load()):
            if rec.get("kind") == devices_store.KIND_LOCAL:
                # **本地卡也要喂 facts。** 原来 `local_card(rec)` 光秃秃地调 ——
                # 于是容量整组、ZWO 特征、所在卷、"已插入"状态**全都没有**,
                # 状态恒「○ 未检测」,当前连着的那台也不标「当前连接」。
                # 又是"共享层收着、前端没传"那一类(浏览页 1.d15、
                # 记录页 fits_map、设备页 rtt 之后的第四次)。
                root = rec.get("host", "")
                out.append(dv.local_card(
                    rec, facts=self._facts.get(root),
                    connected=bool(conn.get("host") == root),
                    present_live=self._present.get(root),
                    snap_ts=self._facts_ts))
                continue
            # **每张卡的存活行要有数据。** `smb_card` 一直收 `rtt`/`fresh`,
            # 而这里一个都没传 —— 于是除了当前连接那台,所有卡的
            # 「● 端口可达 5 ms」永远是空的(老 UI 对全部已记录设备探活)。
            host = rec.get("host", "")
            out.append(dv.smb_card(
                rec,
                connected=bool(conn.get("host") == host and conn.get("rtt")),
                hb={"rtt": conn.get("rtt")} if conn.get("host") == host else None,
                rtt=self._rtt, fresh=set(self._rtt),
                snap_ts=self._rtt_ts))
        try:
            # **卷枚举失败只记不抛** —— 不该让整页空掉
            for vol in volumes_mod.list_volumes():
                facts = dv.volume_facts(vol)
                if dv.should_offer_volume(facts):
                    out.append(dv.volume_card(facts))
        except Exception as exc:             # noqa: BLE001
            self.shell.notice(_("本机卷枚举失败: {exc}").format(exc=exc))
        return out

    def _card(self, c: dict) -> QWidget:
        """卡片模型的形状来自 ``views.devices``:``status`` 是 ``(文本, 语义色)``,
        三个动作是 ``(可用?, 文案, 禁用原因)`` —— 不是布尔值。"""
        card = W.Card(c.get("title", ""), c.get("sub", ""))
        # **徽章不止一个。** 原来 `[:1]` 只画第一个 —— SMB 卡的「ASIAIR」
        # (疑似 ASIAIR 的判读就靠它)、本地卡的「当前连接 / 本地卡 /
        # ZWO 特征 4 项 / 已拔出」全被砍掉。卡头放第一个,其余排一行。
        badges = list(c.get("badges") or ())
        if badges:
            card.set_chip(str(badges[0][0]), "accent")
        if len(badges) > 1:
            brow = W.hbox(gap="xs")
            for text, style in badges[1:]:
                brow.addWidget(W.StatusChip(str(text),
                                            models.TONE_MAP.get(str(style))
                                            or "accent"))
            brow.addStretch(1)
            card.add_layout(brow)
        status = c.get("status") or ("", "")
        if status[0]:
            card.add(W.label(str(status[0]), role="body", tone=status[1],
                             wrap=True))
        if c.get("live"):
            card.add(W.label(str(c["live"]), role="subtitle", wrap=True))
        if c.get("age"):
            card.add(W.label(str(c["age"]), role="faint", wrap=True))
        for _glyph, title, pairs in c.get("groups") or ():
            card.add(W.group_title(title))
            for item in pairs:
                note = str(item[2]) if len(item) > 2 and item[2] else ""
                tone = item[4] if len(item) > 4 else None
                gadget = item[5] if len(item) > 5 else None
                card.add(W.MetricRow(str(item[0]), str(item[1]), note=note,
                                     tone=tone))
                if gadget and gadget[0] == "usagebar":
                    card.add(W.UsageBar(float(gadget[1]), int(dv._BAR_W)))
        card.add_layout(self._actions(c))
        return card

    def _actions(self, c: dict):
        row = W.hbox(gap="sm")
        host, kind = c.get("host", c.get("key", "")), c.get("kind", "")
        for name, kw in (("connect", {"kind": "primary"}), ("open", {}),
                         ("add", {}), ("forget", {"kind": "danger"})):
            spec = c.get(name)
            if not spec:
                continue
            enabled, text, hint = spec
            handler = {
                "connect": lambda h=host, k=kind: self.shell.connect_device(h, k),
                "open": lambda h=host: self.shell.open_browser_path(h, ""),
                "add": lambda h=host, k=kind, p=c.get("path", ""):
                    self._remember(h, k, p),
                "forget": lambda h=host: self._forget(h),
            }[name]
            # **文案为空时不要拿动作键名兜底。** `smb_card` 的 `open` 给的是
            # `(False, "", "")`,于是按钮上直接印着英文 `open`;
            # 项目规范是用户可见文本一律中文。没文案就不画这颗按钮。
            if not text:
                continue
            row.addWidget(W.button(text, on_click=handler,
                                   enabled=bool(enabled), tip=hint, **kw))
        row.addStretch(1)
        return row

    # ------------------------------------------------------------ 动作

    def _add(self) -> None:
        """**校验在工作线程里做。**

        ``parse_manual_input`` 会碰文件系统(判断本地文件夹在不在),而对不可达
        的 UNC 路径 ``os.path.isdir`` 实测阻塞四十多秒 —— 在 GUI 线程调用会把
        整个窗口冻住,那正是它的 docstring 点名警告的事。
        """
        text = self.manual.text().strip()
        if not text:
            return
        # **`existing` 要的是 host 字符串列表,不是记录 dict 列表。**
        # 传 dict 进去,`_find_existing` 里 `host_key(item)` 会
        # `AttributeError: 'dict' object has no attribute 'strip'` ——
        # 任何合法输入都抛,而异常又被横幅那条 bug 吞掉,
        # 界面上是**零反馈**(老 UI 传的就是 `[r["host"] for r in …]`)。
        existing = [r.get("host", "") for r in devices_store.load()
                    if r.get("host")]
        self.bg.run(lambda: dv.parse_manual_input(text, existing),
                    on_done=self._apply_parsed,
                    on_error=lambda e: self.report(e, _("解析地址")))

    def _apply_parsed(self, parsed: dict) -> None:
        if not parsed.get("ok"):
            # **手输要校验。** 另外那套前端直接 remember 了原文,
            # 于是一个打错的地址会永久躺在设备列表里。
            self.shell.notice(parsed.get("error") or _("地址无法识别"))
            return
        self._remember(parsed["host"], parsed.get("kind", ""),
                       parsed.get("path", ""))
        self.manual.clear()

    def _remember(self, host: str, kind: str, path: str) -> None:
        devices_store.remember(host, kind=kind or None, path=path or None,
                               connected=False)
        self.shell.refresh_devices()
        self.reload()

    def _forget(self, host: str) -> None:
        # **破坏性动作要二次确认**(老 UI 有)。原来点下去卡直接消失。
        if not self.confirm(
                _("忘记设备"),
                _('从设备记录里移除「{host}」?\n\n设备上的数据不受影响,只是这条记录没了。').format(host=host),
                ok_text=_("忘记")):
            return
        devices_store.forget(host)
        self.shell.refresh_devices()
        self.reload()
