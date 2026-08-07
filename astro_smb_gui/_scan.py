"""扫描页:找局域网内开放 445 的设备(DHCP 下 ASIAIR 地址会变)。#8"""

from __future__ import annotations

import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from win32more import asyncui
from win32more.Microsoft.UI.Xaml import (
    FrameworkElement,
    GridLength,
    GridUnitType,
    VerticalAlignment,
)
from win32more.Microsoft.UI.Xaml.Controls import (
    Button,
    ColumnDefinition,
    Grid,
    ListView,
    Orientation,
    ProgressBar,
    ProgressRing,
    StackPanel,
    TextBlock,
    TextBox,
)
from win32more.Microsoft.UI.Xaml.Markup import XamlReader
from win32more.Microsoft.UI.Xaml.Media import SolidColorBrush
from win32more.Windows.UI import Color

from astro_smb_gui._common import looks_like_local_path

XAML_PATH = Path(__file__).with_name("scan.xaml")


def _color(r: int, g: int, b: int) -> SolidColorBrush:
    c = Color()
    c.A, c.R, c.G, c.B = 255, r, g, b
    return SolidColorBrush(c)


def _latency_color(rtt: float | None) -> SolidColorBrush:
    if rtt is None:
        return _color(0x9E, 0x9E, 0x9E)
    if rtt < 30:
        return _color(0x4C, 0xAF, 0x50)   # 绿:好
    if rtt < 100:
        return _color(0xFF, 0xB3, 0x00)   # 琥珀:一般
    return _color(0xE5, 0x73, 0x73)       # 红:差


# 探测与判读已下沉到 astro_smb_app.views.scan —— 新前端扫同一个网段、
# 用同一套判据(**只认 SMB 协商,不认 TCP**:路由器会对整网段假 ACK)。
from astro_smb_app.views.scan import (  # noqa: F401
    _identify,
    _local_subnets,
    _probe,
    _resolve_hostname,
    _subnet_of,
)
from astro_smb.i18n import gettext as _
from astro_smb_gui._xamli18n import load_text as _xaml_text


class ScanPage:
    def __init__(self, shell):
        self.shell = shell
        self.root = XamlReader.Load(_xaml_text(XAML_PATH)).as_(FrameworkElement)
        self._scanning = False
        self._cancel: threading.Event | None = None
        self._find_controls()
        self._wire()
        self.subnet_box.Text = _local_subnets()[0]

    def _find_controls(self) -> None:
        f = self.root.FindName
        self.subnet_box = f("SubnetBox").as_(TextBox)
        self.scan_btn = f("ScanBtn").as_(Button)
        self.scan_ring = f("ScanRing").as_(ProgressRing)
        self.status_text = f("StatusText").as_(TextBlock)
        self.progress = f("ScanProgress").as_(ProgressBar)
        self.result_list = f("ResultList").as_(ListView)
        # 当前连接状态卡
        self.hb_dot = f("HbDot").as_(TextBlock)
        self.conn_title = f("ConnTitle").as_(TextBlock)
        self.conn_detail = f("ConnDetail").as_(TextBlock)
        self.hb_latency = f("HbLatency").as_(TextBlock)
        self.hb_time = f("HbTime").as_(TextBlock)

    def _wire(self) -> None:
        self.scan_btn.Click += self._on_scan

    def on_show(self) -> None:
        # 默认扫描"当前设备所在网段"——DHCP 换 IP 后 ASIAIR 仍在同一 /24。
        # 顶部设备框是可编辑 ComboBox,统一经 shell.current_host() 取值,
        # 不直接摸控件类型;没有设备记录时它是空串,保留本机网段。
        if not self._scanning:
            cur = self.shell.current_host()
            # 当前连的是本地卡(直插)时,"当前设备所在网段"无从谈起 —— 从盘符
            # 推不出网段,硬扫本机网段又跟这台设备毫无关系。保留上次/本机网段,
            # 并明说扫描找的是网络设备,免得用户以为扫描能找到手里这张卡。
            if looks_like_local_path(cur):
                self.status_text.Text = (
                    _("当前连接的是本地卡(直插),不在局域网上;扫描用于查找网络上的 ASIAIR"))
            else:
                host_pre = _subnet_of(cur)
                if host_pre:
                    self.subnet_box.Text = host_pre
        # 立刻反映一次最近的心跳状态
        if getattr(self.shell, "hb", None):
            self.on_heartbeat(self.shell.hb)

    def on_connected(self, shares) -> None:
        pass

    # ---------- 当前连接状态(心跳驱动) ----------

    def on_heartbeat(self, hb: dict) -> None:
        """由 shell 的心跳线程经 shell.ui 调用,刷新连接状态卡。"""
        if not hb or not hb.get("host"):
            self.hb_dot.Foreground = _color(0x9E, 0x9E, 0x9E)
            self.conn_title.Text = _("当前未连接")
            self.conn_detail.Text = _("连接设备后,这里显示实时状态与心跳")
            self.hb_latency.Text = ""
            self.hb_time.Text = ""
            return
        alive = hb.get("alive")
        name = hb.get("server_name") or hb["host"]
        if alive:
            self.hb_dot.Foreground = _color(0x4C, 0xAF, 0x50)  # 绿:在线
            self.conn_title.Text = _("{name}  ·  在线").format(name=name)
            rtt = hb.get("rtt_ms")
            self.hb_latency.Text = f"{rtt:.0f} ms" if rtt is not None else "—"
            self.hb_latency.Foreground = _latency_color(rtt)
        else:
            self.hb_dot.Foreground = _color(0xE5, 0x73, 0x73)  # 红:断线
            self.conn_title.Text = _("{name}  ·  断线").format(name=name)
            self.hb_latency.Text = _("掉线")
            self.hb_latency.Foreground = _color(0xE5, 0x73, 0x73)
        parts = [hb["host"]]
        if hb.get("dialect"):
            parts.append(hb["dialect"])
        if hb.get("server_os"):
            parts.append(hb["server_os"])
        if hb.get("shares") is not None:
            parts.append(_("{0} 共享").format(hb['shares']))
        if hb.get("checks"):
            parts.append(_("心跳 {0} 次").format(hb['checks']) + (_("/失败 {0}").format(
                hb['fails']) if hb.get("fails") else ""))
        self.conn_detail.Text = "  ·  ".join(parts)
        lt = hb.get("last_ok_str") or ""
        self.hb_time.Text = _("最近 {lt}").format(lt=lt) if lt else ""

    # ---------- 扫描 ----------

    def _on_scan(self, sender, e) -> None:
        if self._scanning:
            if self._cancel:
                self._cancel.set()
            return
        prefix = self.subnet_box.Text.strip().rstrip(".")
        parts = prefix.split(".")
        if len(parts) != 3 or not all(p.isdigit() for p in parts):
            self.shell.error(_("网段格式应为 192.168.1"))
            return
        self._scanning = True
        self.scan_btn.Content = _("停止")
        self.scan_ring.IsActive = True
        self.progress.Value = 0
        self.result_list.Items.Clear()
        self.status_text.Text = _("扫描中…")
        cancel = threading.Event()
        self._cancel = cancel

        def work():
            done = [0]
            found = [0]
            lock = threading.Lock()

            def scan_one(host):
                if cancel.is_set():
                    return
                # 先快速 TCP 预筛(带 RTT),再用 SMB 协商确认(过滤只 ACK 不说 SMB 的中间盒)
                ok, rtt = _probe(host)
                info = _identify(host) if ok else None
                with lock:
                    done[0] += 1
                    if info is not None:
                        found[0] += 1
                    d = done[0]
                self.shell.ui(self._tick, d)
                if info is not None:
                    hostname = _resolve_hostname(host)  # 反向 DNS 主机名
                    self.shell.ui(self._add_result, host, info, rtt, hostname)

            with ThreadPoolExecutor(max_workers=64) as ex:
                for i in range(1, 255):
                    if cancel.is_set():
                        break
                    ex.submit(scan_one, f"{prefix}.{i}")
            self.shell.ui(self._scan_done, found[0], cancel.is_set())

        threading.Thread(target=work, daemon=True, name="netscan").start()

    def _tick(self, done: int) -> None:
        self.progress.Value = done
        n = self.result_list.Items.Size
        self.status_text.Text = _("已探测 {done}/254 …").format(done=done) + (_(" 发现 {n} 台 SMB 设备").format(
            
            n=n) if n else "")

    def _add_result(self, host: str, info, rtt=None, hostname="") -> None:
        name, shares = info
        # 主机名优先用反向 DNS,退回 SMB 服务器名
        display_name = hostname or name
        is_asiair = (any("Images" in s for s in shares)
                     or "ASIAIR" in (display_name or "").upper())
        row = self._result_row(host, display_name, shares, is_asiair, rtt)
        if is_asiair:
            self.result_list.Items.InsertAt(0, row)  # 疑似 ASIAIR 置顶
        else:
            self.result_list.Items.Append(row)
        self.status_text.Text = _("发现 {Size} 台 SMB 设备").format(
            Size=self.result_list.Items.Size)

    def _result_row(self, host: str, name: str, shares, is_asiair: bool, rtt=None) -> Grid:
        g = Grid()
        g.ColumnSpacing = 10
        for w, u in ((1, GridUnitType.Star), (0, GridUnitType.Auto)):
            c = ColumnDefinition()
            c.Width = GridLength(Value=float(w), GridUnitType=u)
            g.ColumnDefinitions.Append(c)

        left = StackPanel()
        # 标题行:名字 + 时延徽章(横排,始终可见,不被推到右边缘)
        title_row = StackPanel()
        title_row.Orientation = Orientation.Horizontal
        title_row.Spacing = 10
        title = TextBlock()
        prefix = "★ " if is_asiair else ""
        title.Text = f"{prefix}{host}" + (f"  ·  {name}" if name and name != host else "")
        title.FontWeight = _semibold()
        title.VerticalAlignment = VerticalAlignment.Center
        title_row.Children.Append(title)
        lat = TextBlock()
        lat.Text = f"{rtt:.0f} ms" if rtt is not None else "—"
        lat.FontSize = 12
        lat.FontWeight = _semibold()
        lat.VerticalAlignment = VerticalAlignment.Center
        lat.Foreground = _latency_color(rtt)
        title_row.Children.Append(lat)
        left.Children.Append(title_row)

        sub = TextBlock()
        sub.FontSize = 12
        sub.Opacity = 0.7
        if shares:
            sub.Text = (_("疑似 ASIAIR — ") if is_asiair else "") + _("共享: ") + _("、").join(shares[:6])
        else:
            sub.Text = _("SMB 设备(匿名无可见共享,可能是 PC/NAS,非 ASIAIR)")
        left.Children.Append(sub)
        g.Children.Append(left)
        Grid.SetColumn(left, 0)

        btn = Button()
        btn.Content = _("连接")
        btn.VerticalAlignment = VerticalAlignment.Center
        btn.Click += (lambda s, e, h=host: self._connect_to(h))
        g.Children.Append(btn)
        Grid.SetColumn(btn, 1)
        return g

    def _connect_to(self, host: str) -> None:
        # 填入顶部设备框并连接;连接成功后由 _window 的成功分支写入设备记录
        # (devices.remember),扫描页自己不碰记录。
        self.shell.set_host(host)
        self.shell.select_page("browse")
        asyncui.create_task(self.shell._connect())

    def _scan_done(self, found: int, cancelled: bool) -> None:
        self._scanning = False
        self.scan_btn.Content = _("开始扫描")
        self.scan_ring.IsActive = False
        self.progress.Value = 254
        self.status_text.Text = (
            _("{0}共发现 {found} 台 SMB 设备").format(
                _("已停止,") if cancelled else _("扫描完成,"), found=found)
            + ("" if found else _(" — 确认设备已开机且与本机同网段")))


def _semibold():
    from win32more.Microsoft.UI.Text import FontWeights
    return FontWeights.SemiBold
