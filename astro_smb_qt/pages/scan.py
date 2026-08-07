"""扫描设备页:在本网段找 SMB 设备(设备是 DHCP,地址会变)。

这一页承载一条真机踩出来的纪律:**判据只认 SMB 协商,不认端口开着**。
用户那台路由器(RT-BE88U)会对**整个网段**的 445 SYN 秒回 ACK —— 只看 TCP
会把 254 个 IP 误报成 200 多台设备。判定在
``astro_smb_app.views.scan._identify``:只有完成 SMB 协商才返回非 None。

所以这一页的进度与空态文案都要说清"扫到几台**真的**",而不是"几个端口开着"。
"""
from __future__ import annotations

import concurrent.futures as cf

from PySide6.QtWidgets import QProgressBar, QWidget

from astro_smb_app.discover import discover
from astro_smb_app.discover import pick_one as discover_pick_one
from astro_smb_app.views import scan as sv
from astro_smb_qt import widgets as W
from astro_smb_qt.pages.base import Page, PageHeader, page_layout
from astro_smb.i18n import N_, gettext as _

HOSTS = 254
POOL = 64


class ScanPage(Page):
    TITLE = N_("扫描设备")
    SUBTITLE = N_("在本网段找 SMB 设备 —— 只列出完成 SMB 协商的那些")

    def __init__(self, shell):
        super().__init__(shell)
        self.rows: list[dict] = []
        #: 上一轮是**被停掉**还是跑完的 —— 措辞不一样
        self._stopped = False
        self._busy = False
        self._build()
        shell.heartbeat.connect(self._on_heartbeat)

    def _build(self) -> None:
        root = page_layout(self)
        self.header = PageHeader(_(self.TITLE), _(self.SUBTITLE))
        self.found_chip = W.StatusChip("", "accent")
        self.header.add_action(self.found_chip)
        root.addWidget(self.header)

        # 「我现在连的是谁」和「网里还有谁」是同一个问题的两半 —— 老 UI
        # 把连接状态卡摆在这一页,这里照做。
        self.conn_card = W.Card(_("当前连接"), _("SMB ECHO 往返才叫在线"))
        self.conn_line = W.label(_("未连接任何设备"), role="body", tone="dim", wrap=True)
        self.conn_card.add(self.conn_line)
        #: 第二行:OS · 心跳次数 · 最近心跳时刻(本地卡时还有一句说明)
        self.conn_note = W.label("", role="subtitle", wrap=True)
        self.conn_card.add(self.conn_note)
        root.addWidget(self.conn_card)

        bar = W.Card()
        row = W.hbox(gap="sm")
        row.addWidget(W.label(_("网段"), role="subtitle"))
        self.subnet = W.line_edit("192.168.1", on_return=self.toggle)
        self.subnet.setMaximumWidth(160)
        row.addWidget(self.subnet)
        row.addWidget(W.label(".1-254", role="faint"))
        self.scan_btn = W.button(_("开始扫描"), kind="primary", on_click=self.toggle)
        row.addWidget(self.scan_btn)
        self.progress = W.label("", role="subtitle", wrap=True)
        row.addWidget(self.progress, 1)
        # 0–254 的进度条(老 UI 有)。只有文字的话"扫到哪儿了"要读数字,
        # 而这一趟要跑十几秒。
        self.bar = QProgressBar()
        self.bar.setRange(0, HOSTS)
        self.bar.setTextVisible(False)
        self.bar.setFixedWidth(180)
        self.bar.setVisible(False)
        row.addWidget(self.bar)
        bar.add_layout(row)
        root.addWidget(bar)

        self.list_card = W.Card(_("发现的设备"), _("★ 是疑似 ASIAIR(共享里有 Images)"))
        self.scroll = W.Scroll(gap="sm")
        self.state = W.StateStack(self.scroll)
        self.list_card.add(self.state, 1)
        root.addWidget(self.list_card, 1)
        self._show_idle()

    # ------------------------------------------------------------ 契约

    #: 自动发现:扫完之后如果**恰好一台**疑似 ASIAIR,就直接连上。
    #: 由 shell 在"一台设备都没记过"时打开(见 `Shell.__init__`)。
    auto_connect = False

    def autoscan(self) -> None:
        """替用户开扫。**设备是 DHCP 的,没有记录时正确的默认是"去找"** ——
        不是猜一个地址,也不是停在这里等他点按钮。"""
        self.auto_connect = True
        self.on_show()                      # 先把网段填好
        if not self._busy:
            self.toggle()

    def on_show(self) -> None:
        if not self.subnet.text().strip():
            host = self.shell.conn.get("host") or ""
            guess = sv._subnet_of(host) if host else ""
            if not guess:
                subnets = sv._local_subnets()
                guess = subnets[0] if subnets else ""
            self.subnet.setText(guess)
        self._render_conn()

    def on_close(self) -> None:
        self._busy = False

    # ------------------------------------------------------------ 扫描

    def toggle(self) -> None:
        if self._busy:
            self._busy = False           # 协作式停止:worker 每台看一眼
            self._stopped = True         # 结果回来时措辞要说"已停止"
            self.scan_btn.setText(_("开始扫描"))
            return
        subnet = sv.valid_subnet(self.subnet.text())
        if not subnet:
            # **不合法要当场说。** 原来只判空 —— 输入 `nope` 点开始扫描
            # 既不开始也不报错,页面停在"还没扫描"。
            self.shell.notice(_("网段格式应为 192.168.1(三段数字)"))
            return
        self.rows = []
        self._stopped = False
        self._busy = True
        self.scan_btn.setText(_("停止"))
        self.state.show_busy(_("正在探测 —— 判据是 SMB 协商成功,不是端口开着"))
        self.bar.setValue(0)
        W.show_if(self.bar, True)
        gen = self.bg.bump()

        def work(report):
            # **扫描循环在共享层**(`astro_smb_app.discover`)—— CLI 的自动发现
            # 走同一份。"判据是 SMB 协商成功不是端口开着"这条真机纪律只能有
            # 一份实现,复制出去的那份迟早有一份被改回只看 TCP。
            return discover(subnet,
                            on_progress=lambda d, t, rows: report((d, rows)),
                            cancel=lambda: not self._busy,
                            hosts=HOSTS, pool=POOL)

        self.bg.run(work, gen=gen, on_progress=self._on_progress,
                    on_done=self._on_done,
                    on_error=lambda e: self._fail(e))

    def _on_progress(self, payload) -> None:
        done, rows = payload
        self.progress.setText(_("正在探测 {done}/{HOSTS} —— 判据是 SMB 协商成功,不是端口开着").format(
            done=done, HOSTS=HOSTS))
        W.show_if(self.bar, True)
        self.bar.setValue(int(done))
        if rows and len(rows) != len(self.rows):
            self.rows = rows
            self._render_rows()

    def _on_done(self, rows) -> None:
        self._busy = False
        self.scan_btn.setText(_("开始扫描"))
        self.bar.setVisible(False)
        self.rows = list(rows)
        # **停下来的不能说"完成"。** 用户按了停止,界面却报"扫描完成,
        # 共发现 1 台" —— 读起来就是"这个网段只有一台",而其实只探了 6/254。
        stopped = self._stopped
        self.progress.setText(
            (_("已停止") if stopped else _("扫描完成"))
            + _(",共发现 {0} 台 SMB 设备").format(len(self.rows))
            + (_("(只探了一部分地址)") if stopped else ""))
        if not self.rows:
            self.state.show_empty(
                _("已停止,还没发现 SMB 设备") if stopped else _("本网段没有 SMB 设备"),
                (_("停在这里的时候还没探完,不能说明网段里没有设备 —— 重新扫一次看看。"))
                if stopped else
                _("扫描已经跑完了 —— {HOSTS} 个地址里没有一台完成 SMB 协商。确认设备开着、和电脑在同一网段,再换个网段试试。").format(
                    HOSTS=HOSTS))
            self.found_chip.set("0", "warn")
            self.auto_connect = False
            return
        self._render_rows()
        if self.auto_connect and not stopped:
            self.auto_connect = False
            pick = discover_pick_one(self.rows)
            if pick is not None:
                # **只有无歧义才自动连。** 两台疑似 ASIAIR 时替用户选一台,
                # 选错了他看到的是别人的片子,而界面上不会说"我替你选了"。
                self.shell.notice(_("自动发现了 {name}({host}),正在连接")
                                  .format(name=pick.get("title") or pick["ip"],
                                          host=pick["ip"]))
                self.shell.connect_device(pick["ip"])
            elif len(self.rows) > 1:
                self.shell.notice(
                    _("发现 {0} 台 SMB 设备 —— 请在下面挑一台连接").format(
                        len(self.rows)))

    def _fail(self, exc: BaseException) -> None:
        self._busy = False
        self.scan_btn.setText(_("开始扫描"))
        self.state.show_content()
        self.report(exc, _("扫描"))

    def _show_idle(self) -> None:
        self.state.show_empty(
            _("还没扫描"),
            _("点「开始扫描」在本网段找 SMB 设备。现在是还没扫过,不是网里没有设备。"))

    def _render_rows(self) -> None:
        self.state.show_content()
        self.scroll.clear()
        for r in self.rows:
            self.scroll.body.addWidget(self._row(r))
        self.scroll.body.addStretch(1)
        self.found_chip.set(str(len(self.rows)), "ok")

    def _row(self, r: dict) -> QWidget:
        card = W.Card(flat=True)
        row = W.hbox(gap="sm")
        head = W.vbox(gap="none")
        line = W.hbox(gap="sm")
        if r.get("asiair"):
            line.addWidget(W.label("★", role="body", tone="ok"))
        # **IP 必须在标题里** —— 要连哪台靠的是地址,主机名只是给人认的。
        # 另外那套前端重排卡片时把 IP 漏掉过一次。
        title = r["ip"]
        if r.get("title") and r["title"] != r["ip"]:
            title += f" · {r['title']}"
        line.addWidget(W.label(title, role="title"))
        line.addWidget(W.StatusChip(r["rtt"], r.get("tone")))
        line.addStretch(1)
        head.addLayout(line)
        head.addWidget(W.label(r["sub"], role="subtitle", wrap=True))
        row.addLayout(head, 1)
        row.addWidget(W.button(_("连接"), kind="primary",
                               on_click=lambda ip=r["ip"]:
                               self.shell.connect_device(ip, "smb")))
        card.add_layout(row)
        return card

    # ------------------------------------------------------------ 连接卡

    def _on_heartbeat(self, _state: dict) -> None:
        self._render_conn()

    def _render_conn(self) -> None:
        from astro_smb_qt.shell import connection_text

        conn = self.shell.conn
        if not conn.get("host"):
            self.conn_line.setText(_("未连接任何设备"))
            W.set_prop(self.conn_line, "tone", "dim")
            self.conn_card.set_chip("")
            self.conn_note.setText("")
            return
        text, tone = connection_text(conn)
        self.conn_line.setText(f"{conn['host']} — {text}")
        W.set_prop(self.conn_line, "tone", tone)
        self.conn_card.set_chip(
            _("在线") if conn.get("rtt") is not None else
            (_("端口可达") if conn.get("port_ok") else _("断开")), tone)
        # **心跳次数与最近时刻**:区分"现在真的在线"与"五分钟前说过在线
        # 然后卡住了"的唯一读数 —— 而这张卡存在的理由就是这个。
        bits = []
        if conn.get("server_os"):
            bits.append(str(conn["server_os"]))
        beats = int(conn.get("beats", 0) or 0)
        if beats:
            bits.append(_("心跳 {beats} 次").format(beats=beats))
        last = float(conn.get("last_beat", 0.0) or 0.0)
        if last:
            import time as _t

            bits.append(_("最近 {0}").format(_t.strftime('%H:%M:%S', _t.localtime(last))))
        if conn.get("kind") == "local":
            # 本地卡不在局域网上,扫描对它没有意义 —— 老 UI 明说这一句
            bits.append(_("当前连的是本地卡(直插),不在局域网上;扫描用于查找网络上的 ASIAIR"))
        self.conn_note.setText(" · ".join(bits))
