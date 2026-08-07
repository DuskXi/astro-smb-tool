"""设备管理页:把 devices.json 与本机卷信息摊开成卡片,可连接/忘记/手动添加。

**为什么有这一页**:顶部设备下拉只能挤下一行纯字符串(可编辑 ComboBox 的坑,
见 docs/DEVELOPMENT.md §7.1),而 :mod:`astro_smb_gui.volumes` 采集到的卷标/文件系统/容量/
ZWO 特征命中项、:mod:`astro_smb_gui.devices` 记录里的 ``os``/``first_seen``
全都没有 UI 出口。这一页用浏览页详情卡那套排版语言(卡片 + 分组小标题 +
两列 KV 行 + 徽章 + 语义色)把它们完整显示出来。

页面分三块:

1. **已记录的设备** —— 每台一张卡。SMB 设备与本地卡(直插)字段不同,
   分别由 :func:`smb_card` / :func:`local_card` 组装(纯函数,可离线单测)。
2. **本机磁盘(未加入设备记录)** —— 刷新时枚举到、但还不在记录里的卷,
   带 ZWO 特征命中徽章与「添加为设备」按钮(自动发现只收特征 ≥3 的卷,
   这里让用户对剩下的自己拍板)。
3. **手动添加** —— 手输 SMB 地址或本地文件夹路径(卡被拷到别处/移动硬盘)。

**措辞铁律**(docs/DEVELOPMENT.md §2):局域网里的路由器会对整个网段的 TCP 445 SYN 秒回
ACK,所以非当前连接的 SMB 设备只能说"端口可达",绝不能说"在线";只有当前
连接那台(有真 SMB 心跳)才配说"在线"。本地卡没有"网络"可言,只有"已插入"
与"已拔出"两种状态 —— 说"离线"会让用户去查网络,而实际上是卡被拔了。

**线程**:卷枚举(disk_usage 可能碰到慢的网络盘)与 TCP 探测一律在
``dev-page-refresh`` 工作线程里做,结果经 ``shell.ui`` 编组回 UI(§6.2)。
**手动添加也一样**:``os.path.isdir`` 对不可达的 UNC 路径会阻塞四十多秒
(实测),放 UI 线程会把 XAML 消息泵和手摇 asyncio 循环一起冻住 ——
所以 :meth:`DevicesPage._add` 只做纯字符串判断,连解析带落盘全在
``dev-page-add`` 线程里。

**事件泄漏**(项目铁律):win32more 的 ``event.__get__`` 把实例存进类级
``_event_setters`` 且永不删除,``-=`` 无效 —— 所以卡片按 host 缓存整只控件树,
重建列表时只改文字,**绝不给新建控件重新挂事件**。

**心跳每 4s 会走一遍渲染**,所以卡片模型里"每次心跳都变的字段"必须与
"结构性字段"分开:心跳次数塞进 ``groups`` 会让 :meth:`_apply_card` 每 4s
推倒重建整张 KV 表(实测 67 ms/次,且与当前在哪一页无关)。高频字段一律
走 ``live``/``age`` 这种**只改一个 TextBlock.Text** 的通道。

**"我不知道"优先于过期的确定性数字**:容量/ZWO 命中/已插入这些都来自
上一次刷新的快照,卡片右上角会标"采集于 N 分钟前";可见时每
:data:`VISIBLE_REFRESH_AGE` 秒自动重采一次,插拔状态还会顺带取用外壳
每 20s 一轮的探测结果(:meth:`DevicesPage._shell_rtt`)。
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from win32more import asyncui
from win32more.Microsoft.UI.Text import FontWeights
from win32more.Microsoft.UI.Xaml import (
    CornerRadius,
    FrameworkElement,
    GridLength,
    GridUnitType,
    TextWrapping,
    Thickness,
    VerticalAlignment,
    Visibility,
)
from win32more.Microsoft.UI.Xaml.Controls import (
    Border,
    Button,
    Canvas,
    ColumnDefinition,
    FontIcon,
    Grid,
    Orientation,
    ProgressRing,
    RowDefinition,
    StackPanel,
    TextBlock,
    TextBox,
    ToolTipService,
)
from win32more.Microsoft.UI.Xaml.Markup import XamlReader
from win32more.Microsoft.UI.Xaml.Media import FontFamily, SolidColorBrush
from win32more.Microsoft.UI.Xaml.Shapes import Rectangle
from win32more.Windows.UI import Color

from astro_smb.client import AstroSmbClient
from astro_smb.util import format_mtime, human_size
from astro_smb.i18n import N_, gettext as _
from astro_smb_gui import devices, volumes
from astro_smb_gui._common import looks_like_local_path

XAML_PATH = Path(__file__).with_name("devicespage.xaml")

XAML_NS = "http://schemas.microsoft.com/winfx/2006/xaml/presentation"
XAML_X_NS = "http://schemas.microsoft.com/winfx/2006/xaml"

PROBE_TIMEOUT = 2.0         # 单台 SMB 设备的 TCP 445 探测超时(秒)
PROBE_WORKERS = 8           # 探测并发(12 台记录 × 2s 串行太久)
AUTO_REFRESH_AGE = 15.0     # 切到本页时:上次刷新超过这么久就自动再刷一次
VISIBLE_REFRESH_AGE = 20.0  # 本页可见时:快照超过这么久就自动重采(与外壳探测同频)
TICK_SECONDS = 5.0          # 页面自己的心跳(更新"采集于 …"、触发可见时重采)
RECS_CACHE_TTL = 3.0        # devices.json 的内存缓存寿命(别每次心跳都读盘)
SNAPSHOT_STALE_AFTER = 60.0  # 快照超过这么久就在卡上标注采集时刻

#: 手动添加区的常驻说明(操作结束后要恢复它,别把"正在检查 …"留在那)
ADD_HINT_DEFAULT = N_("添加只写入本机的设备记录,不会改动设备上的任何文件")

# 视图模型已下沉到 astro_smb_app.views.devices —— 新前端消费同一份,
# 两边的"12 分钟前"/"占 87%"/"● 端口可达 5 ms" 因此永远一致。
# (B4 按计划随切片抽取;走的是 docs/architecture/frontend.md 里的逃生口。)
from astro_smb_app.views.devices import (  # noqa: F401
    SNAPSHOT_STALE_AFTER,
    _BAR_W,
    _BADGE_RGB,
    _DOT_RGB,
    _GRP_CAPACITY,
    _GRP_DISK,
    _GRP_NET,
    _GRP_RECORD,
    _GRP_ZWO,
    _TONE_RGB,
    _bad,
    _find_existing,
    _flat_pairs,
    _groups_shape,
    _pair_spec,
    abs_time,
    bmp_safe,
    capacity_pairs,
    heartbeat_line,
    local_card,
    local_facts,
    local_status,
    parse_manual_input,
    rel_time,
    should_offer_volume,
    smb_card,
    smb_status,
    snapshot_note,
    sorted_records,
    usage_percent,
    usage_tone,
    volume_card,
    volume_facts,
    zwo_pairs,
)
from astro_smb_gui._xamli18n import load_text as _xaml_text

_CARD_XAML = f'''<Border xmlns="{XAML_NS}" xmlns:x="{XAML_X_NS}"
        CornerRadius="6" Padding="12,10,12,10"
        Background="{{ThemeResource CardBackgroundFillColorDefaultBrush}}">
  <StackPanel Spacing="6">
    <Grid ColumnSpacing="10">
      <Grid.ColumnDefinitions>
        <ColumnDefinition Width="Auto"/>
        <ColumnDefinition Width="*"/>
        <ColumnDefinition Width="Auto"/>
      </Grid.ColumnDefinitions>
      <TextBlock x:Name="Dot" Grid.Column="0" Text="&#x25CF;" FontSize="18"
                 VerticalAlignment="Center"/>
      <StackPanel Grid.Column="1" Spacing="1">
        <StackPanel Orientation="Horizontal" Spacing="8">
          <TextBlock x:Name="Title" FontSize="15" FontWeight="SemiBold"
                     VerticalAlignment="Center" TextTrimming="CharacterEllipsis"/>
          <StackPanel x:Name="Badges" Orientation="Horizontal" Spacing="6"
                      VerticalAlignment="Center"/>
        </StackPanel>
        <TextBlock x:Name="Sub" FontSize="12" Opacity="0.65" TextWrapping="Wrap"/>
      </StackPanel>
      <StackPanel Grid.Column="2" VerticalAlignment="Center" Spacing="0">
        <TextBlock x:Name="Status" FontSize="13" FontWeight="SemiBold"
                   HorizontalAlignment="Right"/>
        <TextBlock x:Name="Age" FontSize="11" Opacity="0.55"
                   HorizontalAlignment="Right" Visibility="Collapsed"/>
      </StackPanel>
    </Grid>
    <Grid x:Name="KV" ColumnSpacing="12" RowSpacing="3" Margin="0,2,0,0">
      <Grid.ColumnDefinitions>
        <ColumnDefinition Width="Auto"/>
        <ColumnDefinition Width="*"/>
      </Grid.ColumnDefinitions>
    </Grid>
    <TextBlock x:Name="Live" FontSize="11" Opacity="0.6" TextWrapping="Wrap"
               Visibility="Collapsed"/>
    <StackPanel x:Name="Actions" Orientation="Horizontal" Spacing="6"
                Margin="0,4,0,0"/>
  </StackPanel>
</Border>'''

# ================================================================ UI 层

def _brush(r: int, g: int, b: int, a: int = 255) -> SolidColorBrush:
    c = Color()
    c.A, c.R, c.G, c.B = a, r, g, b
    return SolidColorBrush(c)


def _corner(r: float) -> CornerRadius:
    cr = CornerRadius()
    cr.TopLeft = cr.TopRight = cr.BottomLeft = cr.BottomRight = r
    return cr


class DevicesPage:
    """设备管理页。页面接口见 docs/DEVELOPMENT.md §6.4(on_show / on_connected / on_close)。"""

    def __init__(self, shell):
        self.shell = shell
        self.root = XamlReader.Load(_xaml_text(XAML_PATH)).as_(FrameworkElement)

        # 画刷一次建好复用(每次刷新都会用到,别在渲染循环里 new)
        self._tone = {k: _brush(*rgb) for k, rgb in _TONE_RGB.items()}
        self._dot = {k: _brush(*rgb) for k, rgb in _DOT_RGB.items()}
        self._badge = {k: (_brush(*bg), _brush(*fg))
                       for k, (bg, fg) in _BADGE_RGB.items()}
        self._divider = _brush(0x80, 0x80, 0x80, 0x3C)
        self._track_bg = _brush(0x80, 0x80, 0x80, 0x38)
        self._mono = FontFamily("Consolas")

        # host → 已建好的卡片控件(**含已挂事件的按钮**),重建列表时复用
        self._cards: dict[str, dict] = {}
        self._laid: dict[str, list] = {}    # 容器 → 上次铺进去的 key 顺序
        # 刷新线程采到的事实:{"rtt": {host: ms|None}, "local": {root: facts},
        #                      "vols": [facts], "ts": float}
        self._state: dict = {"rtt": {}, "local": {}, "vols": [], "ts": 0.0}
        self._gen = 0                       # 刷新代号:过期结果直接丢弃
        self._refreshing = False
        self._last_try = 0.0                # 上次发起刷新(monotonic;失败也算)
        self._stop = threading.Event()

        # 懒渲染:DevicesPage 在 win.Activate() **之前**构造,这里建卡片等于
        # 白白给冷启动加时间(12 台记录实测 +823 ms),而设备页并不是启动页。
        # 首次 on_show 才真正铺卡。
        self._shown = False                 # 至少显示过一次
        self._visible = False               # 当前是不是正显示着
        self._parent_ok = False             # root.Parent 这条情报可不可用
        self._tick_thread: threading.Thread | None = None
        self._recs_cache: list[dict] | None = None      # devices.json 内存缓存
        self._recs_at = 0.0
        self._status_summary = ""           # 状态栏主文本("N 台已记录设备 …")
        self._status_shown = ""             # 上次真正写进控件的整句(免重复赋值)
        self._adding = False                # 手动添加进行中(防重入)

        self._find_controls()
        self._wire()

    # ---------- 初始化 ----------

    def _find_controls(self) -> None:
        f = self.root.FindName
        self.refresh_btn = f("RefreshBtn").as_(Button)
        self.refresh_ring = f("RefreshRing").as_(ProgressRing)
        self.status_text = f("StatusText").as_(TextBlock)
        self.add_box = f("AddBox").as_(TextBox)
        self.add_btn = f("AddBtn").as_(Button)
        self.add_hint = f("AddHint").as_(TextBlock)
        self.record_head = f("RecordHead").as_(TextBlock)
        self.empty_card = f("EmptyCard").as_(Border)
        self.go_scan_btn = f("GoScanBtn").as_(Button)
        self.card_host = f("CardHost").as_(StackPanel)
        self.vol_head = f("VolHead").as_(TextBlock)
        self.vol_host = f("VolHost").as_(StackPanel)

    def _wire(self) -> None:
        # 页面级固定控件的事件只在这里挂一次(卡片里的按钮随卡缓存,同理只挂一次)
        self.refresh_btn.Click += self._on_refresh_click
        self.add_btn.Click += self._on_add_click
        self.add_box.KeyDown += self._on_add_key
        self.go_scan_btn.Click += self._on_go_scan

    # ---------- 页面接口 ----------

    def on_show(self) -> None:
        self._shown = True
        self._visible = True
        self.refresh_records()              # 首次进来才真正建卡(懒渲染)
        if time.time() - self._state["ts"] > AUTO_REFRESH_AGE:
            self._start_refresh()
        self._start_ticker()

    def on_hide(self) -> None:
        """切走了(集成契约:主控在 ``_show_page`` 里对旧页调用,没接线也不影响 ——
        :meth:`_is_visible` 还会用 ``root.Parent`` 兜底)。"""
        self._visible = False

    def on_connected(self, shares) -> None:
        # 连接成功后 shell 已经 devices.remember 过,记录里多了名字/协议/共享数
        self._recs_cache = None             # 记录变了:内存缓存作废
        self.refresh_records()

    def on_close(self) -> None:
        self._stop.set()

    def on_heartbeat(self, hb: dict) -> None:
        """心跳到达(每 ~4s,**与当前在哪一页无关**):只有徽章/心跳计数会变。

        重画一次卡片 —— 内容没变的字段一个 WinRT 调用都不会发(见
        :meth:`_apply_card`);心跳计数走 ``live`` 单个 TextBlock,
        绝不会再触发整张 KV 表重建。
        """
        self.refresh_records()
        self._update_status_line()

    # ---------- 可见性 / 页面自己的心跳 ----------

    def _is_visible(self) -> bool:
        """本页当前是不是显示着。

        外壳切页是 ``PageHost.Children.Clear()`` + ``Append(page.root)``,
        所以"还挂在树上"= 正显示着。``Parent`` 万一在某些版本上不可用
        (一次都没读到过非空),退回 on_show/on_hide 的标记。
        """
        if not self._shown or self._stop.is_set():
            return False
        try:
            parent = self.root.Parent
        except Exception:       # noqa: BLE001
            parent = None
        if parent is not None:
            self._parent_ok = True
            return True
        return False if self._parent_ok else self._visible

    def _start_ticker(self) -> None:
        """页面自己的 5s 心跳线程:更新"采集于 …",可见且过期时自动重采。

        **不依赖外壳把 on_heartbeat 接过来**(接了更好,不接也照常工作)。
        线程只做 sleep + 一次 ``shell.ui`` 编组,活儿全在 UI 线程那一小步。
        """
        if self._tick_thread is not None and self._tick_thread.is_alive():
            return
        self._tick_thread = threading.Thread(target=self._tick_loop, daemon=True,
                                             name="dev-page-tick")
        self._tick_thread.start()

    def _tick_loop(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            try:
                self.shell.ui(self._tick)
            except Exception:       # noqa: BLE001 —— 关窗竞态,不该杀掉线程
                return

    def _tick(self) -> None:
        if self._stop.is_set() or not self._shown:
            return
        self._update_status_line()
        if not self._is_visible():
            return
        self.refresh_records()      # 让"采集于 N 分钟前"跟着走
        stale = time.time() - self._state["ts"] > VISIBLE_REFRESH_AGE
        tried = time.monotonic() - self._last_try > VISIBLE_REFRESH_AGE
        if stale and tried:
            self._start_refresh()   # 可见时定期真刷新,而不是拿旧快照装实时

    # ---------- 设备记录(UI 线程读盘要节制) ----------

    def _load_records(self) -> list[dict]:
        """设备记录(带 :data:`RECS_CACHE_TTL` 秒的内存缓存)。

        心跳每 4s 会走一遍渲染,每次都去读 devices.json 是白白的 UI 线程 I/O;
        记录变化的几个入口(添加/忘记/连接成功)会显式把缓存置空。
        """
        now = time.monotonic()
        if self._recs_cache is None or now - self._recs_at > RECS_CACHE_TTL:
            self._recs_cache = devices.load()
            self._recs_at = now
        return self._recs_cache

    def _update_status_line(self) -> None:
        """状态栏:统计 + **这份数据是什么时候采的**(整页唯一的时间出处)。"""
        if self._refreshing:
            return              # 正在采集:别把"正在枚举 …"覆盖掉
        ts = self._state["ts"]
        if not ts:
            text = _("尚未采集本机磁盘与设备状态 — 点「刷新」")
        else:
            note = snapshot_note(ts) or _("刚刚采集")
            text = (self._status_summary + "  ·  " if self._status_summary else "")
            text += note
            if snapshot_note(ts):
                text += _(",点「刷新」重新采集")
        if text == self._status_shown:
            return                  # 一个字都没变:别碰控件
        self._status_shown = text
        try:
            self.status_text.Text = text
        except Exception:       # noqa: BLE001
            pass

    # ---------- 刷新(工作线程) ----------

    def _on_refresh_click(self, sender, e) -> None:
        self._start_refresh()

    def _start_refresh(self) -> None:
        if self._refreshing or self._stop.is_set():
            return
        self._refreshing = True
        self._last_try = time.monotonic()
        self._gen += 1
        gen = self._gen
        self.refresh_ring.IsActive = True
        self._status_shown = _("正在枚举本机磁盘并探测已记录设备 …")
        self.status_text.Text = self._status_shown
        threading.Thread(target=self._refresh_worker, args=(gen,), daemon=True,
                         name="dev-page-refresh").start()

    def _refresh_worker(self, gen: int) -> None:
        """枚举本机卷 + 探测已记录设备。**全程工作线程,不碰任何 XAML。**"""
        t0 = time.monotonic()
        state: dict = {"rtt": {}, "local": {}, "vols": [], "ts": time.time()}
        try:
            recs = devices.load()
            try:
                vols = volumes.list_volumes()
            except Exception:       # noqa: BLE001 —— 枚举失败不该让整页瘫掉
                vols = []
            sigs: dict[str, tuple] = {}
            for v in vols:
                sigs[str(v.path)] = volumes.scan_root(v.path)

            # 已记录的本地设备:卡还在不在、卷标/容量/特征各是什么
            recorded_local: set[str] = set()
            for rec in recs:
                if not devices.is_local(rec):
                    continue
                root = devices.local_root(rec) or rec["host"]
                recorded_local.add(root)
                present = os.path.isdir(root)
                # 不在了就别再挂"所在卷"的卷标/文件系统/容量 —— 那是另一个盘的
                # 事实,显示出来只会让人以为卡还在(探针实证)
                vol = _volume_for(vols, root) if present else None
                hits = None
                if present:
                    # 卷根已在 sigs 里扫过;子目录(如 D:\ASIAIR 备份)单独扫一层,
                    # 容量仍按所在卷统计(local_card 会标明"所在卷")
                    sig = sigs.get(root)
                    hits = (sig if sig is not None else volumes.scan_root(root))[0]
                state["local"][root] = local_facts(root, vol, present=present,
                                                   hits=hits)
            # 枚举到但还没加进记录的卷 —— 让用户自己决定要不要加
            for v in vols:
                key = str(v.path)
                if key in recorded_local:
                    continue
                hits, others = sigs.get(key, ([], []))
                state["vols"].append(volume_facts(v, hits, others))

            # SMB 设备:只做 TCP 445 连通性(便宜、不建会话)。当前连接那台跳过,
            # 它有真心跳(措辞才配叫"在线")。
            connected = getattr(self.shell, "_hb_host", "") or ""
            targets = [r["host"] for r in recs
                       if not devices.is_local(r) and r["host"] != connected]
            if targets and not self._stop.is_set():
                with ThreadPoolExecutor(
                        max_workers=min(PROBE_WORKERS, len(targets))) as ex:
                    for host, ms in zip(targets, ex.map(_ping, targets)):
                        state["rtt"][host] = ms
        except Exception as ex:     # noqa: BLE001 —— 异常必须落到 InfoBar,不许静默
            self.shell.ui(self.shell.error, _("设备刷新失败: {ex}").format(ex=ex))
            self.shell.ui(self._refresh_done, gen, None, 0.0)
            return
        self.shell.ui(self._refresh_done, gen, state,
                      (time.monotonic() - t0) * 1000.0)

    def _refresh_done(self, gen: int, state: dict | None, ms: float) -> None:
        if self._stop.is_set():
            return              # 窗口已关:工作线程的收尾编组不该再碰控件
        if gen != self._gen:
            return              # 过期结果(用户连点刷新):丢弃
        self._refreshing = False
        self.refresh_ring.IsActive = False
        if state is None:
            self._status_shown = _("刷新失败")
            self.status_text.Text = self._status_shown
            return
        self._state = state
        n_dev = len(self._load_records())
        n_vol = sum(1 for f in state["vols"] if should_offer_volume(f))
        self._status_summary = (
            _("{n_dev} 台已记录设备,枚举到 {0} 个未加入的本机磁盘(列出 {n_vol} 个)  ·  用时 {ms:.0f} ms").format(
                len(state['vols']), n_dev=n_dev, n_vol=n_vol, ms=ms))
        self._update_status_line()
        self.refresh_records()

    # ---------- 渲染 ----------

    def _shell_rtt(self) -> dict:
        """外壳后台探测(每 20s 一轮)的结果:``{host: 毫秒|None}``。

        本地设备在那份表里是 ``0.0=卡还在 / None=拔了`` —— 比本页的手动刷新
        快照新得多(实测拔卡后顶栏 20s 内翻成"已拔出",本页纹丝不动)。

        **集成契约**:主控暴露公开访问器 ``shell.dev_rtt()`` 后走它;
        还没接线时退回私有字段,页面照样能用。
        """
        getter = getattr(self.shell, "dev_rtt", None)
        if callable(getter):
            try:
                return dict(getter() or {})
            except Exception:       # noqa: BLE001
                return {}
        return dict(getattr(self.shell, "_dev_rtt", None) or {})

    def refresh_records(self) -> None:
        """按当前设备记录 + 最近一次采集结果重画卡片列表(不做任何网络/磁盘扫描)。"""
        if not self._shown:
            return      # 懒渲染:首次 on_show 之前不建任何卡片(冷启动 +823 ms)
        try:
            recs = sorted_records(self._load_records())
        except Exception as ex:     # noqa: BLE001
            self.shell.error(_("读取设备记录失败: {ex}").format(ex=ex))
            return
        hb = getattr(self.shell, "hb", None) or {}
        connected = getattr(self.shell, "_hb_host", "") or ""
        now = time.time()
        snap_ts = self._state["ts"]
        # 外壳的周期探测覆盖本页快照:同一个 host 两边都有时以外壳的为准(更新)
        shell_rtt = self._shell_rtt()
        rtt = {**self._state["rtt"], **shell_rtt}

        models: list[dict] = []
        for rec in recs:
            try:
                host = rec["host"]
                is_conn = devices.same_host(host, connected)
                if devices.is_local(rec):
                    root = devices.local_root(rec) or host
                    live = shell_rtt.get(host)
                    models.append(local_card(
                        rec, facts=self._state["local"].get(root),
                        connected=is_conn, snap_ts=snap_ts,
                        present_live=(None if host not in shell_rtt
                                      else live is not None),
                        now=now))
                else:
                    models.append(smb_card(
                        rec, connected=is_conn, hb=hb, rtt=rtt,
                        fresh=shell_rtt.keys(), snap_ts=snap_ts, now=now))
            except Exception as ex:     # noqa: BLE001 —— 单张卡坏掉不该拖垮整页
                self.shell.error(_("设备卡片组装失败({0}): {ex}").format(rec.get('host'), ex=ex))
        self._lay_out("dev", self.card_host, models)

        known = {devices.host_key(r["host"], r["kind"]) for r in recs}
        free = [f for f in self._state["vols"]
                if devices.host_key(f.get("root", ""),
                                    devices.KIND_LOCAL) not in known]
        offered = [f for f in free if should_offer_volume(f)]
        vol_models = [volume_card(f, snap_ts=snap_ts, now=now) for f in offered]
        self._lay_out("vol", self.vol_host, vol_models)

        hidden = len(free) - len(offered)
        self.vol_head.Text = (_("本机磁盘(未加入设备记录)")
                              + (_("  ·  另有 {hidden} 个普通固定盘未列出,要加请用上方「手动添加」").format(
                                  hidden=hidden) if hidden else ""))
        self.empty_card.Visibility = (
            Visibility.Collapsed if models else Visibility.Visible)
        self.record_head.Visibility = (
            Visibility.Visible if models else Visibility.Collapsed)
        self.vol_head.Visibility = (
            Visibility.Visible if vol_models else Visibility.Collapsed)

    def _lay_out(self, slot: str, host: StackPanel, models: list[dict]) -> None:
        """把模型铺进容器:命中缓存的卡只改内容,**不新建、不重挂事件**。

        卡片顺序没变时连 ``Children`` 都不动 —— 心跳每 4s 会走一遍这里,
        每次清空重排是白白的 WinRT 往返。
        """
        keys = [m["key"] for m in models]
        relay = self._laid.get(slot) != keys
        if relay:
            host.Children.Clear()
        for model in models:
            card = self._cards.get(model["key"])
            if card is None:
                card = self._build_card(model)
                self._cards[model["key"]] = card
            self._apply_card(card, model)
            if relay:
                host.Children.Append(card["root"])
        self._laid[slot] = keys

    def _build_card(self, model: dict) -> dict:
        """建一张空卡(整只 XamlReader.Load 一次成型)并挂好按钮事件 —— 每 key 一次。"""
        root = XamlReader.Load(_CARD_XAML).as_(Border)
        f = root.FindName
        card = {
            "root": root,
            "dot": f("Dot").as_(TextBlock),
            "title": f("Title").as_(TextBlock),
            "sub": f("Sub").as_(TextBlock),
            "status": f("Status").as_(TextBlock),
            "age": f("Age").as_(TextBlock),
            "live": f("Live").as_(TextBlock),
            "badges": f("Badges").as_(StackPanel),
            "kv": f("KV").as_(Grid),
            "actions": f("Actions").as_(StackPanel),
            "model": None,
            "buttons": {},
            "rows": [],         # KV 表的行控件(形状没变时原地改文字,见 _fill_groups)
            "shape": None,      # 上次 KV 表的结构签名
        }
        host = model["host"]
        path = model.get("path", "")
        specs = [
            ("add", lambda s, e, h=host, p=path: self._on_add_volume(h, p)),
            ("connect", lambda s, e, h=host: self._on_connect(h)),
            ("open", lambda s, e, p=path: self._on_open_folder(p)),
            ("forget", lambda s, e, h=host: asyncui.create_task(self._forget(h))),
        ]
        for name, handler in specs:
            if name not in model:
                continue
            btn = Button()
            btn.FontSize = 12
            btn.Padding = Thickness(Left=10, Top=3, Right=10, Bottom=4)
            btn.Click += handler          # **只在这里挂一次**(事件永久泄漏,§铁律)
            card["buttons"][name] = btn
            card["actions"].Children.Append(btn)
        return card

    def _apply_card(self, card: dict, model: dict) -> None:
        """把模型刷到卡上。**逐字段比较,只发生了变化的那几个才碰控件**。

        心跳每 4s 就会走一遍这里(而且与当前在哪一页无关):以前整张卡只要
        有一个字节不同就全刷,心跳计数又恰好在 ``groups`` 里 —— 于是每 4s
        推倒重建一次 KV 表(``RowDefinitions.Clear`` + 逐个 new 约 44 个元素,
        实测 67 ms)。现在心跳只落在 ``live`` 一个 TextBlock 上。
        """
        old = card["model"]
        if old == model:
            return
        old = old or {}
        if old.get("title") != model["title"]:
            card["title"].Text = model["title"]
        if old.get("sub") != model["sub"]:
            card["sub"].Text = model["sub"]
        if old.get("status") != model["status"]:
            text, tone = model["status"]
            card["status"].Text = text
            card["status"].Foreground = self._tone.get(tone, self._tone["dim"])
            card["dot"].Foreground = self._dot.get(tone, self._dot["dim"])
        if old.get("age") != model.get("age"):
            self._set_line(card["age"], model.get("age", ""))
        if old.get("live") != model.get("live"):
            self._set_line(card["live"], model.get("live", ""))
        if old.get("badges") != model["badges"]:
            self._render_badges(card["badges"], model["badges"])
        if old.get("groups") != model["groups"]:
            self._fill_groups(card["kv"], model["groups"], card)
        for name, btn in card["buttons"].items():
            spec = model.get(name, (False, "", ""))
            if old.get(name) == spec:
                continue
            enabled, label, tip = spec
            btn.Content = label
            btn.IsEnabled = bool(enabled)
            btn.Visibility = Visibility.Visible if label else Visibility.Collapsed
            ToolTipService.SetToolTip(btn, tip or label)
        card["model"] = model

    @staticmethod
    def _set_line(tb: TextBlock, text: str) -> None:
        """一行淡色副注:空串就收起来(留个空行会把卡片撑松)。"""
        tb.Text = text
        tb.Visibility = Visibility.Visible if text else Visibility.Collapsed

    def _render_badges(self, panel: StackPanel, badges: list[tuple]) -> None:
        """徽章行:圆角小胶囊(浅底深字),与浏览页详情卡同款。"""
        panel.Children.Clear()
        for text, style in badges:
            bg, fg = self._badge.get(style, self._badge["plain"])
            chip = Border()
            chip.CornerRadius = _corner(9.0)
            chip.Background = bg
            chip.Padding = Thickness(Left=8, Top=1, Right=8, Bottom=2)
            tb = TextBlock()
            tb.Text = text
            tb.FontSize = 11
            tb.FontWeight = FontWeights.SemiBold
            tb.Foreground = fg
            chip.Child = tb
            panel.Children.Append(chip)

    # --- 两列 KV 表(与 _browser 详情卡同一套排版规格:标签 12/0.55,值 12 可选中,
    #     副注 11/0.55,语义色只染主值,等宽用 Consolas)。这里是刻意的重复实现 ——
    #     那边是 BrowserPage 的方法,跨页共用得动 _browser.py(不在本轨道的所有权内)。

    def _add_row(self, grid: Grid) -> None:
        rd = RowDefinition()
        rd.Height = GridLength(Value=1.0, GridUnitType=GridUnitType.Auto)
        grid.RowDefinitions.Append(rd)

    def _fill_groups(self, grid: Grid, groups: list[tuple],
                     card: dict | None = None) -> None:
        """按 (图标, 组名, 键值对) 分区填 KV 表;空组自动跳过。

        **结构没变就只改文字**:整表重建要 ``RowDefinitions.Clear`` + 逐个 new
        约 44 个元素(Rectangle 全套 1.7~2.1ms、TextBlock 0.88ms),而定期刷新
        改的通常只是"已用 32GB → 33GB"这种值。行标签/副注有无/等宽/小组件形态
        任何一项对不上就退回整表重建 —— 宁可慢一次,不要错一次。
        """
        shape = _groups_shape(groups)
        if card is not None and card.get("shape") == shape and card.get("rows"):
            self._update_rows(card["rows"], _flat_pairs(groups))
            return
        grid.RowDefinitions.Clear()
        grid.Children.Clear()
        row = 0
        rows: list[dict] = []
        for glyph, name, pairs in groups:
            if not pairs:
                continue
            row = self._add_group_header(grid, row, glyph, name, first=(row == 0))
            row = self._add_pairs(grid, row, pairs, rows)
        if card is not None:
            card["shape"] = shape
            card["rows"] = rows

    def _update_rows(self, rows: list[dict], pairs: list[tuple]) -> None:
        """结构相同的 KV 表:逐行原地改值/副注/占用条(变了才发 WinRT 调用)。"""
        for rec, item in zip(rows, pairs):
            k, v, note, _mono, tone, widget = _pair_spec(item)
            last = rec["last"]
            if last[1] != v:
                rec["val"].Text = v
            if last[4] != tone and tone is not None:
                brush = self._tone.get(tone)
                if brush is not None:
                    rec["val"].Foreground = brush
            if rec["aux"] is not None and last[2] != note:
                rec["aux"].Text = note
            fill = rec["fill"]
            if fill is not None and last[5] != widget:
                try:
                    pct = max(0.0, min(100.0, float(widget[1])))
                except (TypeError, ValueError, IndexError):
                    pct = 0.0
                fill.Width = max(2.0, _BAR_W * pct / 100.0)
                if tone is not None:
                    fill.Fill = self._tone.get(tone, self._tone["good"])
            rec["last"] = (k, v, note, _mono, tone, widget)

    def _add_group_header(self, grid: Grid, row: int, glyph: str, name: str,
                          first: bool = False) -> int:
        self._add_row(grid)
        head = Grid()
        for width, unit in ((1.0, GridUnitType.Auto), (1.0, GridUnitType.Auto),
                            (1.0, GridUnitType.Star)):
            c = ColumnDefinition()
            c.Width = GridLength(Value=width, GridUnitType=unit)
            head.ColumnDefinitions.Append(c)
        head.Margin = Thickness(Left=0, Top=(1 if first else 9), Right=0, Bottom=1)

        icon = FontIcon()
        icon.Glyph = glyph
        icon.FontSize = 11
        icon.Opacity = 0.55
        icon.VerticalAlignment = VerticalAlignment.Center
        head.Children.Append(icon)
        Grid.SetColumn(icon, 0)

        lab = TextBlock()
        lab.Text = name
        lab.FontSize = 11
        lab.Opacity = 0.55
        lab.Margin = Thickness(Left=6, Top=0, Right=0, Bottom=0)
        lab.VerticalAlignment = VerticalAlignment.Center
        head.Children.Append(lab)
        Grid.SetColumn(lab, 1)

        line = Border()
        line.Height = 1
        line.Background = self._divider
        line.VerticalAlignment = VerticalAlignment.Center
        line.Margin = Thickness(Left=8, Top=0, Right=0, Bottom=0)
        head.Children.Append(line)
        Grid.SetColumn(line, 2)

        grid.Children.Append(head)
        Grid.SetRow(head, row)
        Grid.SetColumn(head, 0)
        Grid.SetColumnSpan(head, 2)
        return row + 1

    def _add_pairs(self, grid: Grid, row: int, pairs: list[tuple],
                   rows: list[dict] | None = None) -> int:
        """(标签, 值[, 副注[, 等宽[, 语义色[, 小组件]]]]) → 两列行。

        ``rows`` 给了就把每行可原地改的控件记下来(供 :meth:`_update_rows`)。
        """
        for item in pairs:
            k, v, note, mono, tone, widget = _pair_spec(item)
            self._add_row(grid)
            lab = TextBlock()
            lab.Text = k
            lab.FontSize = 12
            lab.Opacity = 0.55
            grid.Children.Append(lab)
            Grid.SetRow(lab, row)
            Grid.SetColumn(lab, 0)

            val = TextBlock()
            val.Text = v
            val.FontSize = 12
            val.TextWrapping = TextWrapping.Wrap
            val.IsTextSelectionEnabled = True
            val.VerticalAlignment = VerticalAlignment.Center
            if mono:
                val.FontFamily = self._mono
            if tone is not None:
                brush = self._tone.get(tone)
                if brush is not None:
                    val.Foreground = brush
                    val.FontWeight = FontWeights.SemiBold
            aux = None
            fill = None
            if note or widget is not None:
                panel = StackPanel()
                panel.Orientation = Orientation.Horizontal
                panel.Spacing = 6
                panel.VerticalAlignment = VerticalAlignment.Center
                gadget, fill = self._make_gadget(widget, tone)
                if gadget is not None:
                    panel.Children.Append(gadget)
                panel.Children.Append(val)
                if note:
                    aux = TextBlock()
                    aux.Text = note
                    aux.FontSize = 11
                    aux.Opacity = 0.55
                    aux.TextWrapping = TextWrapping.Wrap
                    aux.VerticalAlignment = VerticalAlignment.Center
                    panel.Children.Append(aux)
                grid.Children.Append(panel)
                Grid.SetRow(panel, row)
                Grid.SetColumn(panel, 1)
            else:
                grid.Children.Append(val)
                Grid.SetRow(val, row)
                Grid.SetColumn(val, 1)
            if rows is not None:
                rows.append({"val": val, "aux": aux, "fill": fill,
                             "last": (k, v, note, mono, tone, widget)})
            row += 1
        return row

    def _make_gadget(self, spec, tone: str | None):
        """行内小组件 → (控件, 可原地改宽度的填充块);没有就是 ``(None, None)``。

        目前只有 ``("usagebar", 占用百分比)``。
        """
        if not spec:
            return (None, None)
        try:
            if spec[0] == "usagebar":
                return self._usage_bar(float(spec[1]), tone)
        except (TypeError, ValueError, IndexError):
            return (None, None)
        return (None, None)

    def _usage_bar(self, percent: float, tone: str | None) -> tuple:
        """占用条(140×10):底槽 + 语义色填充。与详情卡的高度角条同族。"""
        w, h = _BAR_W, 8.0
        canvas = Canvas()
        canvas.Width, canvas.Height = w, h + 4.0
        canvas.VerticalAlignment = VerticalAlignment.Center
        track = Rectangle()
        track.Width, track.Height = w, h
        track.RadiusX = track.RadiusY = 4.0
        track.Fill = self._track_bg
        canvas.Children.Append(track)
        Canvas.SetLeft(track, 0.0)
        Canvas.SetTop(track, 2.0)
        frac = max(0.0, min(1.0, percent / 100.0))
        fill = None
        if frac > 0.0:
            fill = Rectangle()
            fill.Width, fill.Height = max(2.0, w * frac), h
            fill.RadiusX = fill.RadiusY = 4.0
            fill.Fill = self._tone.get(tone or "good", self._tone["good"])
            canvas.Children.Append(fill)
            Canvas.SetLeft(fill, 0.0)
            Canvas.SetTop(fill, 2.0)
        return (canvas, fill)

    # ---------- 动作 ----------

    def _on_connect(self, host: str) -> None:
        """连接此设备:填入顶部设备框并走 shell 的统一连接流程。"""
        self.shell.set_host(host)
        asyncui.create_task(self.shell._connect())

    def _on_open_folder(self, path: str) -> None:
        """在资源管理器里打开本地设备的根目录。"""
        if not path:
            return
        if os.name != "nt":
            self.shell.info(_("本地路径:{path}").format(path=path))
            return
        try:
            subprocess.Popen(["explorer.exe", os.path.normpath(path)])
        except Exception as ex:     # noqa: BLE001
            self.shell.error(_("打开资源管理器失败: {ex}").format(ex=ex))

    async def _forget(self, host: str) -> None:
        """忘记设备(破坏性:二次确认)。只删本机记录,不动设备上的文件。"""
        rec = next((r for r in devices.load() if devices.same_host(r["host"], host)),
                   None)
        if rec is None:
            self._recs_cache = None
            self.shell.info(_("{host} 不在设备记录中").format(host=host))
            self.refresh_records()
            return
        if devices.same_host(host, getattr(self.shell, "_hb_host", "") or ""):
            self.shell.info(_("{host} 正在连接中,断开或换设备后再忘记").format(host=host))
            return
        ok = await self.shell.confirm(
            _("忘记设备"),
            _('把 {host} 从设备记录里移除?\n只删本机的这条记录,设备/存储卡上的文件一个都不动。').format(host=host),
            _("忘记"))
        if not ok:
            return
        devices.forget(host)
        self._recs_cache = None         # 记录变了:内存缓存作废
        self._state["rtt"].pop(host, None)
        self._cards.pop(host, None)
        self._notify_shell()
        self.refresh_records()
        self.shell.info(_("已从设备记录中移除 {host}").format(host=host))

    def _on_go_scan(self, sender, e) -> None:
        self.shell.select_page("scan")

    # ---------- 添加 ----------

    def _on_add_key(self, sender, e) -> None:
        from win32more.Windows.System import VirtualKey
        try:
            if e.Key == VirtualKey.Enter:
                self._add(self.add_box.Text)
        except Exception:       # noqa: BLE001
            pass

    def _on_add_click(self, sender, e) -> None:
        self._add(self.add_box.Text)

    def _on_add_volume(self, root: str, path: str) -> None:
        """「添加为设备」:把枚举到的本机卷加进记录(路径已知,直接走同一条路)。"""
        self._add(path or root)

    def _add(self, text: str) -> None:
        """手动添加的**UI 线程那一半:只做纯字符串判断,一次文件系统都不碰**。

        ``parse_manual_input`` 里有 ``os.path.isdir``,对不可达的 UNC 路径实测
        阻塞 42 秒(粘一条 ``//192.0.2.225/EMMC Images`` 或一个断开的映射盘
        就会命中)—— 在这里调用等于把 XAML 消息泵和手摇 asyncio 循环一起冻住。
        解析、去重、探 ZWO 特征、落盘全部丢给 ``dev-page-add`` 线程。
        """
        if self._adding:
            return                          # 防重入:上一次还在检查
        raw = (text or "").strip()
        if not raw:                         # 纯字符串判断,零 I/O
            self.shell.error(_('请输入 SMB 地址(如 192.0.2.225)或本地文件夹(如 E:\\)'))
            return
        self._adding = True
        try:
            self.add_btn.IsEnabled = False
            shown = bmp_safe(raw, 48)       # 用户粘进来的文本可能带 emoji(§7.1)
            self.add_hint.Text = (_("正在检查 {shown} …(路径不可达时可能要等十几秒)").format(shown=shown))
        except Exception:       # noqa: BLE001 —— 控件出问题也要让添加走下去
            pass
        threading.Thread(target=self._add_worker, args=(raw,), daemon=True,
                         name="dev-page-add").start()

    def _add_worker(self, raw: str) -> None:
        """解析 + 落盘,**全程工作线程**(这里才允许碰文件系统)。"""
        try:
            existing = [r["host"] for r in devices.load()]
            res = parse_manual_input(raw, existing)
            if not res["ok"]:
                self.shell.ui(self._add_failed, res["error"],
                              bool(res.get("dup")))
                return
            if res["kind"] == devices.KIND_LOCAL:
                path = res["path"]
                hits = volumes.zwo_signature(path)[1]
                vol = _volume_for(volumes.list_volumes(), path)
                label = (getattr(vol, "label", "") or "") if vol is not None else ""
                name = (label or os.path.basename(path.rstrip("\\/")) or path)
                devices.remember(
                    res["host"], name=name,
                    os=volumes.describe_zwo(path) or _("本地磁盘"),
                    dialect=_("本地磁盘"), shares=1, kind=devices.KIND_LOCAL,
                    path=path, connected=False)
                note = (_("命中 {0} 项 ZWO 特征目录").format(len(hits)) if hits
                        else _("该文件夹没有 ZWO 特征目录,仍可当普通存储浏览"))
            else:
                # SMB:名字/协议/共享数要连上才知道,先只记地址。
                # **connected=False**:还没连过,不能把 last_ok 刷成"刚刚" ——
                # 那会让一个打错的地址显示"最近连接 刚刚",还抢走下次启动的默认设备。
                devices.remember(res["host"], kind=devices.KIND_SMB,
                                 connected=False)
                note = _("连接成功后会补全服务器名与协议")
        except Exception as ex:     # noqa: BLE001
            self.shell.ui(self._add_failed, _("添加设备失败: {ex}").format(ex=ex), False)
            return
        self.shell.ui(self._added, res["host"], note)

    def _add_failed(self, message: str, dup: bool) -> None:
        """添加没成功(输入不合法/重复/异常):恢复输入区,原文留着让人改。"""
        self._adding = False
        if self._stop.is_set():
            return
        try:
            self.add_btn.IsEnabled = True
            self.add_hint.Text = _(ADD_HINT_DEFAULT)
        except Exception:       # noqa: BLE001
            pass
        if dup:
            self._recs_cache = None
            self.shell.info(message)
            self.refresh_records()
        else:
            self.shell.error(message)

    def _added(self, host: str, note: str) -> None:
        self._adding = False
        if self._stop.is_set():
            return
        self.add_box.Text = ""          # 成功了才清输入框(失败时原文留着好改)
        self.add_btn.IsEnabled = True
        self.add_hint.Text = _(ADD_HINT_DEFAULT)
        self._recs_cache = None         # 记录变了:内存缓存作废
        self._notify_shell()
        self.refresh_records()
        self._start_refresh()
        self.shell.info(_("已添加 {host} — {note}").format(host=host, note=note), _("立即连接"),
                        lambda h=host: self._on_connect(h))

    def _notify_shell(self) -> None:
        """设备记录变了 → 让外壳重建顶部下拉(集成契约:shell.on_devices_changed)。

        主控还没接线时退回直接调 ``_rebuild_device_items``,页面照样能用。
        """
        cb = getattr(self.shell, "on_devices_changed", None)
        if not callable(cb):
            cb = getattr(self.shell, "_rebuild_device_items", None)
        if not callable(cb):
            return
        try:
            cb()
        except Exception as ex:     # noqa: BLE001
            self.shell.error(_("刷新顶部设备下拉失败: {ex}").format(ex=ex))


# ---------------------------------------------------------------- 模块级小工具

def _ping(host: str) -> float | None:
    """TCP 445 连通性 → RTT 毫秒;连不上返回 None。**能连上只代表端口可达。**"""
    try:
        return AstroSmbClient(host=host, timeout=PROBE_TIMEOUT).ping_tcp(
            timeout=PROBE_TIMEOUT)
    except Exception:       # noqa: BLE001 —— 一台探测失败不该影响其余
        return None


def _volume_for(vols, path: str):
    """找出 ``path`` 所在的卷(精确匹配优先,其次最长前缀);找不到返回 None。"""
    target = os.path.normcase(os.path.abspath(str(path)))
    best = None
    best_len = -1
    for v in vols or ():
        root = os.path.normcase(os.path.abspath(str(v.path)))
        if target == root:
            return v
        prefix = root if root.endswith(os.sep) else root + os.sep
        if target.startswith(prefix) and len(root) > best_len:
            best, best_len = v, len(root)
    return best
