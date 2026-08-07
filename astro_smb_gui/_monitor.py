"""传输监控页(#2):分区显示 进行中/排队/已结束,每行 aria2NG 式分块方块图,
阶段(元数据/传输)与统计。行对象持久化,进度 tick 只原地更新,分区变化才重排。

两条改动前必须读懂的约定:

* **行控件与组头都进回收池,永不丢弃**(#30)。行上的「取消」按钮、组头上的
  折叠按钮各挂一个 `Click`,而 win32more 的 `event` 描述符把实例存进**类级**
  `_event_setters` 且**永不删除**(`-=`/`clear()` 只清 `_callbacks`)——
  「清除已完成」之后再下一批文件,每个新行就是一次永久泄漏。探针实测:
  3 轮 × 20 个任务 → Click 条目 22/44/66 一路涨。改成回收池后稳定在 22。
  代价是**闭包不能再捕获 job_id / 组键**(回收给别的任务就错位了),
  一律从行/组头字典里现读(`row["job_id"]` / `hdr["key"]`)。
* **分块方块图首次建格走批量 XAML**(#34)。逐个 new 一个带尺寸/填充/定位的
  Rectangle 约 1.7~2.1ms,64~128 格就是 130~270ms 的 UI 线程停顿,而每个
  并行任务(ASIAIR 的 .fit 每张 50MB,全是并行)都要建一次。改成整批一次
  `XamlReader.Load` 之后再把子元素引用取回来 —— **后续每 tick 仍然只原地
  改 Fill**(那本来就快),批量只发生在建格那一次。
"""

from __future__ import annotations

import time
from pathlib import Path

from win32more.Microsoft.UI.Text import FontWeights
from win32more.Microsoft.UI.Xaml import (
    FrameworkElement,
    GridLength,
    GridUnitType,
    TextTrimming,
    Thickness,
    VerticalAlignment,
    Visibility,
)
from win32more.Microsoft.UI.Xaml.Controls import (
    Border,
    Button,
    Canvas,
    ColumnDefinition,
    ComboBox,
    ComboBoxItem,
    Grid,
    ProgressBar,
    RowDefinition,
    StackPanel,
    TextBlock,
)
from win32more.Microsoft.UI.Xaml.Markup import XamlReader
from win32more.Microsoft.UI.Xaml.Media import SolidColorBrush
from win32more.Microsoft.UI.Xaml.Shapes import Rectangle
from win32more.Windows.UI import Color

from astro_smb.util import format_duration, human_size
from astro_smb.i18n import gettext as _
from astro_smb_gui._common import argb_hex, rect_fragment, unbox_str
from astro_smb_gui.transfers import (
    CANCELLED,
    DONE_S,
    ERROR,
    PH_META,
    PH_TRANSFER,
    QUEUED,
    RUNNING,
    SKIPPED,
    TransferJob,
)
from astro_smb_gui._xamli18n import load_text as _xaml_text

XAML_PATH = Path(__file__).with_name("monitor.xaml")

MAX_BLOCKS = 128       # 单行最多显示的方块数(实际块数会下采样到这个)
BLOCK_COLS = 64        # 每行方块列数(宽而矮)
CELL = 9               # 方块格子(含间距)
SQUARE = 7             # 方块边长


def _brush(r, g, b, a=255) -> SolidColorBrush:
    c = Color()
    c.A, c.R, c.G, c.B = a, r, g, b
    return SolidColorBrush(c)


class MonitorPage:
    def __init__(self, shell):
        self.shell = shell
        self.root = XamlReader.Load(_xaml_text(XAML_PATH)).as_(FrameworkElement)
        self._rows: dict[int, dict] = {}
        # 「清除已完成」退下来的行/组头进回收池,下一批任务直接复用 ——
        # 它们身上的 Click 一旦注册就再也摘不掉(见模块头 #30)
        self._free_rows: list[dict] = []
        self._free_headers: list[dict] = []
        # 文件夹分组:组头行 widget(keyed (分区, 组名))与折叠态
        # 默认:进行中=展开,排队中/已完成=折叠
        self._group_rows: dict[tuple[str, str], dict] = {}
        self._group_open: dict[tuple[str, str], bool] = {}
        # 复用画刷
        self._c_done = _brush(0x4C, 0xAF, 0x50)      # 绿:已完成块
        self._c_active = _brush(0xFF, 0xB3, 0x00)    # 琥珀:传输中块
        self._c_pending = _brush(0x9E, 0x9E, 0x9E, 60)  # 淡灰:待传块
        self._c_meta = _brush(0x42, 0xA5, 0xF5)      # 蓝:元数据阶段标签
        self._c_xfer = _brush(0x66, 0xBB, 0x6A)      # 绿:传输阶段标签
        self._c_err = _brush(0xE5, 0x73, 0x73)       # 红:错误
        self._find()
        self._wire()
        # 让分块并发下拉反映管理器实际默认值(基于 CPU 核数),取最接近的档位
        options = [1, 2, 4, 6, 8]
        cw = self.shell.transfers.chunk_workers
        self.chunk_box.SelectedIndex = min(
            range(len(options)), key=lambda i: abs(options[i] - cw))

    # ---------- 生命周期 ----------

    def on_show(self) -> None:
        self._refresh_all()

    def on_connected(self, shares) -> None:
        pass

    def _find(self) -> None:
        f = self.root.FindName
        self.stat_speed = f("StatSpeed").as_(TextBlock)
        self.stat_running = f("StatRunning").as_(TextBlock)
        self.stat_queued = f("StatQueued").as_(TextBlock)
        self.stat_done = f("StatDone").as_(TextBlock)
        self.chunk_box = f("ChunkWorkersBox").as_(ComboBox)
        self.cancel_all_btn = f("CancelAllBtn").as_(Button)
        self.clear_done_btn = f("ClearDoneBtn").as_(Button)
        self.empty_hint = f("EmptyHint").as_(TextBlock)
        self.running_header = f("RunningHeader").as_(TextBlock)
        self.running_panel = f("RunningPanel").as_(StackPanel)
        self.queued_header = f("QueuedHeader").as_(TextBlock)
        self.queued_panel = f("QueuedPanel").as_(StackPanel)
        self.done_header = f("DoneHeader").as_(TextBlock)
        self.done_panel = f("DonePanel").as_(StackPanel)

    def _wire(self) -> None:
        self.cancel_all_btn.Click += lambda s, e: self.shell.transfers.cancel_all()
        self.clear_done_btn.Click += self._on_clear_done
        self.chunk_box.SelectionChanged += self._on_chunk_workers

    # ---------- 外部入口 ----------

    def update_job(self, job: TransferJob) -> None:
        row = self._rows.get(job.job_id)
        if row is None:
            row = self._take_row_for(job)
            self._rows[job.job_id] = row
        self._update_row(row, job)
        section = self._section(job)
        if row["section"] != section:
            # 分区归属变化才重排(组成员跨分区移动也随之触发,两边组头自然重建)
            row["section"] = section
            self._relayout()
        if job.group:
            # 组头聚合文本按需刷新(收尾状态不节流,保证终态可见)
            self._refresh_group_headers(job.group, force=job.finished)
        self._update_stats()

    def _refresh_all(self) -> None:
        for job in self.shell.transfers.jobs:
            self.update_job(job)
        self._update_stats()
        self._relayout()

    # ---------- 分区 ----------

    @staticmethod
    def _section(job: TransferJob) -> str:
        if job.status == QUEUED:
            return "queue"
        if job.finished:
            return "done"
        return "run"

    def _relayout(self) -> None:
        """按分区重排。同组(文件夹)任务聚为一组:组头行 + 展开时的成员行;
        同一文件夹部分进行中、部分排队时,两个分区各出现一个该文件夹组头。
        散文件(group=None)渲染方式与原来完全一致。"""
        # 底部常驻条的「清除已完成」直接调用 TransferManager.clear_finished(),
        # 不会经过本页的按钮处理器。反向按 jobs 收割孤儿,才能保证两条入口都把
        # 带永久 Click 注册的行/组头送回池里,而不是让它们永远钉在 _rows。
        self._reap_orphans()
        self.running_panel.Children.Clear()
        self.queued_panel.Children.Clear()
        self.done_panel.Children.Clear()
        panels = {"run": self.running_panel, "queue": self.queued_panel,
                  "done": self.done_panel}
        counts = {"run": 0, "queue": 0, "done": 0}
        # 第一遍:按 (分区, 组) 收集成员行(保持 jobs 顺序)
        order: list[tuple[TransferJob, dict]] = []
        members: dict[tuple[str, str], list[dict]] = {}
        for job in self.shell.transfers.jobs:
            row = self._rows.get(job.job_id)
            if row is None:
                continue
            order.append((job, row))
            counts[row["section"]] += 1
            if job.group:
                members.setdefault((row["section"], job.group), []).append(row)
        # 第二遍:散文件按序直接排;组在其首个成员的位置插入组头
        emitted: set[tuple[str, str]] = set()
        for job, row in order:
            sec = row["section"]
            panel = panels[sec]
            if not job.group:
                panel.Children.Append(row["root"])
                continue
            key = (sec, job.group)
            if key in emitted:
                continue
            emitted.add(key)
            hdr = self._ensure_group_header(key)
            panel.Children.Append(hdr["root"])
            if self._group_open.get(key, sec == "run"):
                for r in members[key]:
                    panel.Children.Append(r["root"])
        # 重排后强制刷新在场组头(折叠箭头/聚合文本)
        for g in {k[1] for k in emitted}:
            self._refresh_group_headers(g, force=True)
        self.running_header.Visibility = _vis(counts["run"])
        self.queued_header.Visibility = _vis(counts["queue"])
        self.done_header.Visibility = _vis(counts["done"])
        self.empty_hint.Visibility = (
            Visibility.Collapsed if any(counts.values()) else Visibility.Visible)

    def on_jobs_pruned(self) -> None:
        """外部(底部常驻条)清理过 `transfers.jobs` 之后调这里。

        公开入口,与本页自己的「清除已完成」共用同一套回收逻辑 —— 两条路径都
        必须把带永久 Click 注册的行/组头送回池,否则它们会永远钉在 `_rows`。
        """
        self._reap_orphans()
        self._relayout()
        self._update_stats()

    def _reap_orphans(self) -> None:
        """把已不在传输管理器里的行/组头送回池。

        win32more 的事件注册不能真正撤销,所以这里只能复用控件,不能丢弃。
        该方法刻意只碰 Python 容器,可由底部条与本页按钮两条清理路径共用。
        """
        jobs = list(self.shell.transfers.jobs)
        alive_ids = {j.job_id for j in jobs}
        for jid in list(self._rows):
            if jid not in alive_ids:
                self._free_rows.append(self._rows.pop(jid))

        alive_keys = {
            (self._section(j), j.group)
            for j in jobs if j.group
        }
        for key in list(self._group_rows):
            if key not in alive_keys:
                self._free_headers.append(self._group_rows.pop(key))
                self._group_open.pop(key, None)

    # ---------- 文件夹分组(组头行) ----------

    def _ensure_group_header(self, key: tuple[str, str]) -> dict:
        hdr = self._group_rows.get(key)
        if hdr is None:
            hdr = self._build_group_header(key)
            self._group_rows[key] = hdr
        return hdr

    def _build_group_header(self, key: tuple[str, str]) -> dict:
        """组头行:折叠按钮 + 组名 + 聚合状态。widget 持久化,文本原地刷新。

        回收池优先:折叠按钮的 `Click` 注册不可撤销,清过一轮已完成之后再来
        一批文件夹就不能再建新组头了(见模块头 #30)。所以复用的关键是
        **闭包不捕获组键**,一律现读 `hdr["key"]`。
        """
        sec, group = key
        hdr = self._free_headers.pop() if self._free_headers else None
        if hdr is not None:
            hdr["key"] = key
            hdr["_last"] = 0.0
            hdr["toggle"].Content = (
                "▼" if self._group_open.get(key, sec == "run") else "▶")
            hdr["name"].Text = f"▣ {group}"
            hdr["status"].Text = ""
            return hdr
        outer = Border()
        outer.CornerRadius = _corner(5)
        outer.Padding = Thickness(Left=4, Top=4, Right=10, Bottom=4)
        head = Grid()
        head.ColumnSpacing = 8
        # 组名列 Auto(完整显示"M 8"这类短名), 聚合文本列 Star + 省略号
        # ——反过来的话长聚合文本会把组名挤到只剩首字符(真机踩过)
        for w, u in ((0, GridUnitType.Auto), (0, GridUnitType.Auto),
                     (1, GridUnitType.Star)):
            c = ColumnDefinition()
            c.Width = GridLength(Value=float(w), GridUnitType=u)
            head.ColumnDefinitions.Append(c)

        hdr = {"root": outer, "key": key, "_last": 0.0}

        toggle = Button()
        toggle.FontSize = 11
        toggle.Padding = Thickness(Left=8, Top=2, Right=8, Bottom=2)
        toggle.Content = "▼" if self._group_open.get(key, sec == "run") else "▶"

        def on_toggle(s, e, h=hdr):
            # 组键现读:同一个组头会被回收给别的 (分区, 组名),捕获 key 会错位
            k = h["key"]
            cur = self._group_open.get(k, k[0] == "run")
            self._group_open[k] = not cur
            h["toggle"].Content = "▶" if cur else "▼"
            self._relayout()

        toggle.Click += on_toggle
        head.Children.Append(toggle)
        Grid.SetColumn(toggle, 0)

        name = TextBlock()
        name.FontSize = 13
        name.FontWeight = FontWeights.SemiBold
        name.VerticalAlignment = VerticalAlignment.Center
        # 用 BMP 字符而非 emoji:星平面字符会让 win32more 的 HSTRING 少一个
        # 码元,组名末尾会被吃掉("M 8"→"M "),见 _space.py 同处注释
        name.Text = f"▣ {group}"
        head.Children.Append(name)
        Grid.SetColumn(name, 1)

        status = TextBlock()
        status.FontSize = 12
        status.Opacity = 0.8
        status.VerticalAlignment = VerticalAlignment.Center
        status.TextTrimming = TextTrimming.CharacterEllipsis
        head.Children.Append(status)
        Grid.SetColumn(status, 2)

        outer.Child = head
        hdr.update({"toggle": toggle, "name": name, "status": status})
        return hdr

    def _refresh_group_headers(self, group: str, force: bool = False) -> None:
        """刷新某组在各分区的组头聚合文本(0.15s 节流;force 跳过节流)。"""
        jobs = [j for j in self.shell.transfers.jobs if j.group == group]
        if not jobs:
            return
        by_sec: dict[str, list[TransferJob]] = {}
        for j in jobs:
            by_sec.setdefault(self._section(j), []).append(j)
        done_all = sum(1 for j in jobs if j.status == DONE_S)
        now = time.monotonic()
        for key, hdr in self._group_rows.items():
            sec, g = key
            if g != group:
                continue
            sec_jobs = by_sec.get(sec)
            if not sec_jobs:
                continue  # 本分区已无成员,组头不在树上
            if not force and now - hdr["_last"] < 0.15:
                continue
            hdr["_last"] = now
            hdr["toggle"].Content = (
                "▼" if self._group_open.get(key, sec == "run") else "▶")
            n = len(sec_jobs)
            if sec == "run":
                speed = sum(j.speed for j in sec_jobs if j.running)
                done_bytes = sum(j.done for j in sec_jobs)
                text = (_("{n} 个文件 · 完成 {done_all}/{0} · 合计 {1}/s · 已传 {2}").format(
                    len(jobs), human_size(speed), human_size(done_bytes), n=n, done_all=done_all))
            elif sec == "queue":
                text = _("{n} 个文件排队 · 完成 {done_all}/{0}").format(
                    len(jobs), n=n, done_all=done_all)
            else:
                ok = sum(1 for j in sec_jobs if j.status == DONE_S)
                bad = sum(1 for j in sec_jobs if j.status == ERROR)
                text = _("{n} 个文件 · 完成 {ok} · 失败 {bad}").format(n=n, ok=ok, bad=bad)
                cn = sum(1 for j in sec_jobs if j.status == CANCELLED)
                sk = sum(1 for j in sec_jobs if j.status == SKIPPED)
                if cn:
                    text += _(" · 取消 {cn}").format(cn=cn)
                if sk:
                    text += _(" · 跳过 {sk}").format(sk=sk)
            hdr["status"].Text = text

    # ---------- 行构建 ----------

    def _take_row_for(self, job: TransferJob) -> dict:
        """取一行给 ``job`` 用:**先回收池,池空再现收割,最后才新建**。

        中间那步"现收割"是关键。底部常驻条的「清除已完成」走
        ``_window._on_clear_done``,只调 ``transfers.clear_finished()``,
        **不经过本页** —— 此刻 ``_rows`` 里全是孤儿而池是空的。下一批任务进来时
        ``_relayout`` 还没跑,于是第一个 job 会**新建**整棵控件树,之后 reap 才把
        那些孤儿送回池。win32more 的事件注册撤不掉,所以每个「清除→再下载」
        周期都永久漏一行的 Click 注册(实测 14/18/22/26,每轮 +4)。
        在这里先收割一次,顺序就正过来了。
        """
        row = self._free_rows.pop() if self._free_rows else None
        if row is None:
            self._reap_orphans()
            row = self._free_rows.pop() if self._free_rows else None
        if row is None:
            return self._build_fresh_row(job)
        # 复用时只归位「`_update_row` 不会管」的两样:分组缩进(它只在建行时
        # 设过一次)和分区归属(置 None 逼出一次 `_relayout`)。
        row["job_id"] = job.job_id
        row["section"] = None
        row["root"].Margin = Thickness(
            Left=(18.0 if job.group else 0.0), Top=0, Right=0, Bottom=0)
        return row

    def _build_fresh_row(self, job: TransferJob) -> dict:
        """真正新建一行的控件树。**只应由 :meth:`_take_row_for` 在池确实取不到时调用。**

        文字/进度/方块本来每次 tick 就全量重写;错误红色与阶段淡化这两处原本是
        **单向**设置(只在进入错误/未知阶段时写),行一旦复用就会粘到下一个任务
        身上,所以改由 `_update_row` 双向写全(见 `_set_fg`)。
        """
        outer = Border()
        outer.CornerRadius = _corner(5)
        outer.Padding = Thickness(Left=10, Top=6, Right=10, Bottom=6)
        # 组内成员行左缩进,视觉上从属于组头
        indent = 18.0 if job.group else 0.0
        outer.Margin = Thickness(Left=indent, Top=0, Right=0, Bottom=0)
        grid = Grid()
        grid.RowSpacing = 4
        grid.RowDefinitions.Append(RowDefinition())
        rd2 = RowDefinition()
        rd2.Height = GridLength(Value=0.0, GridUnitType=GridUnitType.Auto)
        grid.RowDefinitions.Append(rd2)
        outer.Child = grid

        # 第一行:名字 | 阶段 | 状态 | 取消
        head = Grid()
        head.ColumnSpacing = 8
        for w, u in ((1, GridUnitType.Star), (0, GridUnitType.Auto),
                     (0, GridUnitType.Auto), (0, GridUnitType.Auto)):
            c = ColumnDefinition()
            c.Width = GridLength(Value=float(w), GridUnitType=u)
            head.ColumnDefinitions.Append(c)

        name = TextBlock()
        name.FontSize = 13
        name.VerticalAlignment = VerticalAlignment.Center
        head.Children.Append(name)
        Grid.SetColumn(name, 0)

        phase = TextBlock()
        phase.FontSize = 11
        phase.VerticalAlignment = VerticalAlignment.Center
        head.Children.Append(phase)
        Grid.SetColumn(phase, 1)

        status = TextBlock()
        status.FontSize = 12
        status.Opacity = 0.8
        status.VerticalAlignment = VerticalAlignment.Center
        head.Children.Append(status)
        Grid.SetColumn(status, 2)

        row = {"job_id": job.job_id, "rects": [], "fills": [],
               "n_disp": 0, "section": None}

        cancel = Button()
        cancel.Content = _("取消")
        cancel.FontSize = 11
        # job_id 现读:这一行会被回收给后来的任务,捕获 jid 会取消错任务
        cancel.Click += (lambda s, e, r=row:
                         self.shell.transfers.cancel_job(r["job_id"]))
        head.Children.Append(cancel)
        Grid.SetColumn(cancel, 3)

        grid.Children.Append(head)
        Grid.SetRow(head, 0)

        # 第二行:分块方块图 或 进度条
        bar = ProgressBar()
        bar.Minimum, bar.Maximum = 0, 100
        grid.Children.Append(bar)
        Grid.SetRow(bar, 1)

        canvas = Canvas()
        canvas.Visibility = Visibility.Collapsed
        grid.Children.Append(canvas)
        Grid.SetRow(canvas, 1)

        row.update({"root": outer, "name": name, "phase": phase,
                    "status": status, "cancel": cancel, "bar": bar,
                    "canvas": canvas})
        return row

    def _ensure_blocks(self, row: dict, n_chunks: int) -> None:
        """按块数(下采样到 MAX_BLOCKS)建方块;仅当数量变化时重建。

        建格走**一次 `XamlReader.Load`**(见模块头 #34),再把子元素引用取回
        `row["rects"]` —— 后续每 tick 的着色仍然是原地改 Fill,一点没变。
        取引用要 n_disp 次 `GetAt`,但那比 n_disp 次「new Rectangle + 两个
        尺寸 + Fill + Left/Top + Append」便宜得多。片段/取引用任一步出岔子就
        整体退回逐个建(慢,但一定画得出来)。
        """
        n_disp = min(n_chunks, MAX_BLOCKS)
        if row["n_disp"] == n_disp and row["rects"]:
            return
        canvas = row["canvas"]
        canvas.Children.Clear()
        cols = min(BLOCK_COLS, n_disp) or 1
        geo = [(float((i % cols) * CELL), float((i // cols) * CELL),
                float(SQUARE), float(SQUARE)) for i in range(n_disp)]
        rects = self._batch_blocks(canvas, geo)
        if rects is None:
            canvas.Children.Clear()     # 半截批量先扔掉,再走逐元素慢路径
            rects = []
            for x, y, w, h in geo:
                r = Rectangle()
                r.Width = r.Height = w
                r.Fill = self._c_pending
                Canvas.SetLeft(r, x)
                Canvas.SetTop(r, y)
                canvas.Children.Append(r)
                rects.append(r)
        rows = -(-n_disp // cols) if n_disp else 0
        canvas.Width = float(cols * CELL)
        canvas.Height = float(rows * CELL)
        row["rects"] = rects
        # 每格「上次用的画刷」,着色时用来跳过没变的格子(见 _paint_blocks)
        row["fills"] = [None] * n_disp
        row["n_disp"] = n_disp

    def _batch_blocks(self, canvas, geo) -> list | None:
        """整批方块一次成型并取回逐个引用;失败返回 None(调用方走慢路径)。"""
        if not geo:
            return []
        try:
            pend = argb_hex(self._c_pending)
            frag = rect_fragment([(x, y, w, h, pend) for x, y, w, h in geo])
            holder = XamlReader.Load(frag).as_(Canvas)
            kids = holder.Children
            rects = [kids.GetAt(i).as_(Rectangle) for i in range(len(geo))]
            canvas.Children.Append(holder)
            return rects
        except Exception:
            return None

    def _set_fg(self, row: dict, part: str, brush) -> None:
        """给行内某个 TextBlock 上色;`brush=None` = 还原成主题默认色。

        用 `ClearValue` 而不是"存一份原色再写回":代码里新建的 TextBlock 本来
        就没有显式 Foreground(靠主题继承),存不到有意义的原值,而写死一个
        颜色会在浅/深色主题切换时露馅。**只在颜色真的变了时才调**(一次赋值
        约 40us,进度 tick 很密)。
        """
        key = "_fg_" + part
        if row.get(key) is brush:
            return
        row[key] = brush
        tb = row[part]
        try:
            if brush is None:
                tb.ClearValue(TextBlock.ForegroundProperty)
            else:
                tb.Foreground = brush
        except Exception:
            pass

    def _update_row(self, row: dict, job: TransferJob) -> None:
        arrow = "↓" if job.is_download else "↑"
        row["name"].Text = f"{arrow} {job.label}" + (f" — {job.detail}" if job.detail else "")

        # 阶段标签(区分元数据 / 传输)。**两个分支都要写全** —— 行会被回收给
        # 别的任务,而且同一个任务本来也会走 排队→元数据→传输,单向着色会粘住。
        row["phase"].Text = job.phase
        if job.phase == PH_META:
            self._set_fg(row, "phase", self._c_meta)
            row["phase"].Opacity = 1.0
        elif job.phase == PH_TRANSFER:
            self._set_fg(row, "phase", self._c_xfer)
            row["phase"].Opacity = 1.0
        else:
            self._set_fg(row, "phase", None)
            row["phase"].Opacity = 0.6

        # 状态文本
        parts = [_(job.status)]   # 显示才翻
        if job.parallel and job.n_chunks:
            parts.append(_("{workers}并发×{n_chunks}块").format(
                workers=job.workers, n_chunks=job.n_chunks))
        if job.attempt and not job.finished:
            parts.append(_("重试{attempt}").format(attempt=job.attempt))
        if job.total > 0:
            parts.append(f"{human_size(job.done)}/{human_size(job.total)}")
        elif job.done:
            parts.append(human_size(job.done))
        if job.speed > 0 and not job.finished:
            parts.append(f"{human_size(job.speed)}/s")
            eta = job.eta()
            if eta >= 0:
                parts.append(f"ETA {format_duration(eta)}")
        if job.error:
            parts.append(job.error)
        row["status"].Text = "  ".join(parts)
        self._set_fg(row, "status", self._c_err if job.status == ERROR else None)

        row["cancel"].IsEnabled = not job.finished

        # 方块图 vs 进度条
        if job.parallel and job.n_chunks > 0:
            row["bar"].Visibility = Visibility.Collapsed
            row["canvas"].Visibility = Visibility.Visible
            self._ensure_blocks(row, job.n_chunks)
            self._paint_blocks(row, job)
        else:
            row["canvas"].Visibility = Visibility.Collapsed
            row["bar"].Visibility = Visibility.Visible
            if job.total > 0:
                row["bar"].IsIndeterminate = False
                row["bar"].Value = job.progress_fraction() * 100
            else:
                row["bar"].IsIndeterminate = not job.finished
                if job.finished:
                    row["bar"].Value = 100 if job.status == DONE_S else 0

    def _paint_blocks(self, row: dict, job: TransferJob) -> None:
        """按块状态着色。**只写颜色变了的格子** —— 一次 Fill 赋值走
        win32more 的 mixin 路径约 40us,128 格全写就是每 tick 5ms;
        真实进度里一 tick 只有个位数格子会变色。"""
        rects = row["rects"]
        blocks = job.blocks
        n_chunks = len(blocks)
        n_disp = len(rects)
        if not n_disp or not n_chunks:
            return
        last = row.get("fills")
        if last is None or len(last) != n_disp:
            last = row["fills"] = [None] * n_disp
        ratio = n_chunks / n_disp
        for d in range(n_disp):
            lo = int(d * ratio)
            hi = max(lo + 1, int((d + 1) * ratio))
            seg = blocks[lo:hi] or [0]
            if all(s == 2 for s in seg):
                brush = self._c_done
            elif any(s >= 1 for s in seg):
                brush = self._c_active
            else:
                brush = self._c_pending
            if last[d] is not brush:
                rects[d].Fill = brush
                last[d] = brush

    # ---------- 统计 ----------

    def _update_stats(self) -> None:
        st = self.shell.transfers.stats()
        self.stat_speed.Text = human_size(st["speed"]) + "/s"
        self.stat_running.Text = str(len(st["running"]))
        self.stat_queued.Text = str(len(st["queued"]))
        self.stat_done.Text = str(len(st["done"]))

    # ---------- 事件 ----------

    def _on_chunk_workers(self, sender, e) -> None:
        try:
            n = int(unbox_str(self.chunk_box.SelectedItem.as_(ComboBoxItem).Content))
        except Exception:
            n = {0: 1, 1: 2, 2: 4, 3: 6, 4: 8}.get(self.chunk_box.SelectedIndex, 4)
        self.shell.transfers.set_chunk_workers(n)

    def _on_clear_done(self, sender, e) -> None:
        self.shell.transfers.clear_finished()
        self._reap_orphans()
        self._relayout()
        self._update_stats()
        # 同步刷新底部常驻精简条(否则底部会残留已清除的行)
        try:
            self.shell._prune_transfer_rows()
        except Exception:
            pass


def _vis(n: int):
    return Visibility.Visible if n else Visibility.Collapsed


def _corner(r: float):
    from win32more.Microsoft.UI.Xaml import CornerRadius
    cr = CornerRadius()
    cr.TopLeft = cr.TopRight = cr.BottomLeft = cr.BottomRight = r
    return cr
