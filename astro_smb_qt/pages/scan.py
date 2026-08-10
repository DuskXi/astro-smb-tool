"""扫描设备页:在本网段找 SMB 设备(设备是 DHCP,地址会变)。

这一页承载一条真机踩出来的纪律:**判据只认 SMB 协商,不认端口开着**。
用户那台路由器(RT-BE88U)会对**整个网段**的 445 SYN 秒回 ACK —— 只看 TCP
会把 254 个 IP 误报成 200 多台设备。判定在
``astro_smb_app.views.scan._identify``:只有完成 SMB 协商才返回非 None。

所以这一页的进度与空态文案都要说清"扫到几台**真的**",而不是"几个端口开着"。
"""
from __future__ import annotations

import concurrent.futures as cf

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QComboBox, QProgressBar, QWidget

from astro_smb_app.discover import discover
from astro_smb_app.discover import pick_one as discover_pick_one
from astro_smb_app.views import scan as sv
from astro_smb_qt import widgets as W
from astro_smb_qt.pages.base import Page, PageHeader, page_layout
from astro_smb.i18n import N_, gettext as _

HOSTS = 254
POOL = 64
#: 隔多久看一眼网卡有没有变(插网线 / 连 Wi-Fi / 开关 VPN)
NET_POLL_MS = 4000


class _Cancel:
    """一趟扫描的取消标志。**每趟一个,不共享。**

    共享一个布尔会出这种事:停掉第一趟、换个网段再点开始,那个布尔又变回
    "没取消",于是第一趟的 worker 继续扫完整整 254 个地址 —— 两趟一起占着
    线程池,界面看起来就是卡住了。
    """

    __slots__ = ("_set",)

    def __init__(self) -> None:
        self._set = False

    def cancel(self) -> None:
        self._set = True

    def is_set(self) -> bool:
        return self._set


class ScanPage(Page):
    TITLE = N_("扫描设备")
    SUBTITLE = N_("在本网段找 SMB 设备 —— 只列出完成 SMB 协商的那些")

    def __init__(self, shell):
        super().__init__(shell)
        self.rows: list[dict] = []
        #: 上一轮是**被停掉**还是跑完的 —— 措辞不一样
        self._stopped = False
        self._busy = False
        self._token: _Cancel | None = None
        self._known_nets: list[str] = []
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
        # **可编辑下拉**:选本机网卡的,或者自己敲。
        #
        # 原来只是一个输入框,而"我该填什么"对多网卡机器根本不显然 ——
        # 本机实测五个网段,其中两个是 VPN 隧道端点。下拉里给的是**网卡
        # 报上来的真网络**(带真实掩码),排序把家用段放前面。
        self.subnet = QComboBox()
        self.subnet.setEditable(True)
        self.subnet.setMinimumWidth(200)
        self.subnet.setInsertPolicy(QComboBox.NoInsert)
        # publish-scan: ok(给用户看的例子,要长得像家用网才有用)
        self.subnet.lineEdit().setPlaceholderText("192.168.1.0/24")
        self.subnet.lineEdit().returnPressed.connect(self.toggle)
        self.subnet.currentTextChanged.connect(self._on_target_changed)
        row.addWidget(self.subnet)
        #: 这次要扫多大 —— `/16` 会被截断,得让人看见
        self.target_note = W.label("", role="faint")
        row.addWidget(self.target_note)
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
        # 建完就填一次候选,别等到 `on_show` —— 自动扫描那条路
        # (`autoscan`)在页面还没显示过时就会用到下拉里的值。
        self.refresh_networks()

        # **网络会变**:插网线、连 Wi-Fi、开关 VPN、换热点。只在 `on_show`
        # 取一次的话,换了网之后下拉里还是老几项 —— 用户选中的那个网段
        # 早就不在了,扫出来自然什么都没有,而界面不会说为什么。
        self._net_timer = QTimer(self)
        self._net_timer.timeout.connect(self.refresh_networks)
        self._net_timer.start(NET_POLL_MS)

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

    # -- 网段选择 -----------------------------------------------------

    def target_text(self) -> str:
        return self.subnet.currentText().strip()

    def _set_target(self, text: str) -> None:
        self.subnet.setCurrentText(text)

    def refresh_networks(self) -> None:
        """把下拉里的选项换成**当前**网卡报上来的那些。

        **网络会变**:插网线、连 Wi-Fi、开关 VPN、换热点。原来这一页只在
        第一次显示时取一次,之后换了网还是老几项 —— 用户选中的那个网段
        早就不在了,扫出来自然什么都没有。

        用户正在输入的内容不动:只换候选项,不覆盖他敲的字。
        """
        from astro_smb import netscan as N

        nets = N.preferred_networks()
        if nets == self._known_nets:
            return                          # 没变就别动,免得打断输入
        self._known_nets = list(nets)
        keep = self.target_text()
        self.subnet.blockSignals(True)
        self.subnet.clear()
        self.subnet.addItems(nets)
        self.subnet.setCurrentText(keep or (nets[0] if nets else ""))
        self.subnet.blockSignals(False)
        self._on_target_changed()

    def _on_target_changed(self, *_a) -> None:
        """在输入框旁边如实说这次要扫多少个地址。"""
        from astro_smb import netscan as N

        text = self.target_text()
        if not text:
            self.target_note.setText("")
            return
        note = N.describe_target(text)
        self.target_note.setText(note or _("认不出这个网段"))

    def on_show(self) -> None:
        self.refresh_networks()
        if not self.target_text():
            host = self.shell.conn.get("host") or ""
            guess = sv._subnet_of(host) if host else ""
            if guess:
                self._set_target(f"{guess}.0/24")
        self._render_conn()

    def on_close(self) -> None:
        """离开这一页就把在途扫描收掉 —— 它继续跑着,用户既看不到
        进度也停不了它,而 64 条并发连接一直占着。"""
        self._stop()

    # ------------------------------------------------------------ 扫描

    def _stop(self) -> None:
        """停掉**这一趟**。令牌是这一趟专属的,置了就再也不会被撤销。"""
        if self._token is not None:
            self._token.cancel()
        self._token = None
        self._busy = False
        self._stopped = True         # 结果回来时措辞要说"已停止"
        self.scan_btn.setText(_("开始扫描"))

    def toggle(self) -> None:
        if self._busy:
            self._stop()
            return
        from astro_smb import netscan as N

        subnet = self.target_text()
        if N.parse_target(subnet) is None:
            # **不合法要当场说。** 原来只判空 —— 输入 `nope` 点开始扫描
            # 既不开始也不报错,页面停在"还没扫描"。
            self.shell.notice(
                _("网段认不出来。填三段前缀,或者带掩码的写法 —— "
                  "输入框里那行灰字就是例子。掩码不是 /24 的一定要写出来。"))
            return
        self.rows = []
        self._stopped = False
        self._busy = True
        # **每次扫描一个独立的取消标志。**
        #
        # 原来用的是 `self._busy` 这一个共享变量:停止时置 False,而 worker
        # 里的 `cancel=lambda: not self._busy` 读的就是它。于是**换个网段再点
        # 开始**时 `_busy` 又变回 True —— 上一趟那个 worker 眼里"取消"被撤销
        # 了,它继续扫完整整 254 个地址。两趟扫描同时占着线程池(每趟 64 条
        # 连接),界面就是用户说的"换网段之后卡住"。
        #
        # 世代(`bg.bump()`)只挡回调,挡不住已经在跑的线程 —— 挡不住的那部分
        # 正好是最费资源的那部分。
        token = _Cancel()
        self._token = token
        self.scan_btn.setText(_("停止"))
        self.state.show_busy(_("正在探测 —— 判据是 SMB 协商成功,不是端口开着"))
        self.bar.setRange(0, max(1, len(N.target_hosts(N.parse_target(subnet)))))
        self.bar.setValue(0)
        W.show_if(self.bar, True)
        gen = self.bg.bump()

        def work(report):
            # **扫描循环在共享层**(`astro_smb_app.discover`)—— CLI 的自动发现
            # 走同一份。"判据是 SMB 协商成功不是端口开着"这条真机纪律只能有
            # 一份实现,复制出去的那份迟早有一份被改回只看 TCP。
            return discover(subnet,
                            on_progress=lambda d, t, rows: report((d, rows)),
                            cancel=token.is_set, pool=POOL)

        self.bg.run(work, gen=gen, on_progress=self._on_progress,
                    on_done=self._on_done,
                    on_error=lambda e: self._fail(e))

    def _on_progress(self, payload) -> None:
        done, rows = payload
        total = self.bar.maximum()
        self.progress.setText(
            _("正在探测 {done}/{total} —— 判据是 SMB 协商成功,不是端口开着").format(
                done=done, total=total))
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
                _("扫描已经跑完了 —— {total} 个地址里没有一台完成 SMB 协商。确认设备开着、和电脑在同一网段,再换个网段试试。").format(
                    total=self.bar.maximum()))
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
