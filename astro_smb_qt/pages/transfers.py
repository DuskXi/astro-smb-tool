"""传输页:队列、进度、分块方块图、取消。

这一页最容易写错的地方是**刷新策略**。进度以 10 Hz × N 个任务的频率跳,
每次都重建整棵行树的话:选中丢失、滚动位置跳回顶部、界面闪。所以

* 行对象按 ``job_id`` **持久化**,进度 tick 只原地改字段;
* 只有**分区归属**(进行中/排队/完成)变了才重排版。

这条是从老 UI 原样继承的,当时是被 win32more 的逐元素调用开销逼出来的;
在 Qt 上它同样成立,理由换成了"别让用户正在看的那一行跑掉"。
"""
from __future__ import annotations

from PySide6.QtWidgets import QWidget

from astro_smb_app import transfers as xf
from astro_smb_app.views import transfers as tv
from astro_smb_qt import widgets as W
from astro_smb_qt.pages.base import Page, PageHeader, page_layout
from astro_smb.i18n import N_, gettext as _

#: 文件**内**分块并发档位(与文件间并发是两回事,后者在底部队列条)
CHUNK_CHOICES = [1, 2, 3, 4, 6, 8]
DEFAULT_CHUNK_INDEX = 5     # → 8

SECTIONS = [("run", N_("进行中"), N_("正在传输的任务,方块图显示每一块的状态")),
            ("queue", N_("排队中"), N_("等待空闲工作线程")),
            ("done", N_("已完成"), N_("含失败与已取消"))]

#: 状态 → 语义色。**键是常量不是字面量**:状态字符串住在
#: `astro_smb_app.transfers`,写死中文的话常量一改(或一翻译)这里就查不到,
#: 于是每一行的颜色都退回默认 —— 不报错。传输页的「排队」分区就这么空过。
TONE_FOR_STATUS = {xf.DONE_S: "ok", xf.ERROR: "bad", xf.CANCELLED: "warn"}

#: 阶段 → 语义色。**元数据与传输要分色**(老 UI `_monitor.py` 同款):
#: 一个任务卡在"元数据"和卡在"传输"要采取的行动完全不同 ——
#: 前者是在等设备回 stat,后者是真的在拉数据。同色等于把这条信息抹掉。
TONE_FOR_PHASE = {xf.PH_META: "warn", xf.PH_TRANSFER: "accent"}


class TransfersPage(Page):
    TITLE = N_("传输")
    SUBTITLE = N_("下载与上传队列 · 文件间并发 × 文件内分块并发")

    def __init__(self, shell):
        super().__init__(shell)
        self._rows: dict[str, _JobRow] = {}
        self._layout_key: tuple = ()
        #: (分区, 组名) → 展开?**进行中默认展开,排队/已完成默认折叠**
        #: (老 UI 同款:一夹几十个文件,全展开那一列就没边了)
        self._open: dict = {}
        self._build()
        shell.transfers_changed.connect(self.refresh)

    def _build(self) -> None:
        root = page_layout(self)
        self.header = PageHeader(_(self.TITLE), _(self.SUBTITLE))
        root.addWidget(self.header)
        root.addWidget(self._stats_card())

        self.scroll = W.Scroll(gap="card")
        self._cards: dict[str, W.Card] = {}
        for key, title, note in SECTIONS:
            card = W.Card(_(title), _(note))
            self._cards[key] = card
            self.scroll.body.addWidget(card)
        self.empty = W.EmptyState(
            _("暂无传输任务"),
            _("到「浏览」页选中文件点「下载」,或用勾选模式批量下载 —— 任务会在这里实时显示进度与分块状态。"))
        self.scroll.body.addWidget(self.empty)
        self.scroll.body.addStretch(1)
        root.addWidget(self.scroll, 1)
        self.refresh()

    def _stats_card(self) -> QWidget:
        card = W.Card()
        row = W.hbox(gap="xl")
        self.t_speed = W.MetricTile("0 B/s", _("总速度"), accent=True)
        self.t_active = W.MetricTile("0", _("进行中"))
        self.t_queue = W.MetricTile("0", _("排队中"))
        self.t_done = W.MetricTile("0", _("已完成"))
        for tile in (self.t_speed, self.t_active, self.t_queue, self.t_done):
            row.addWidget(tile)
        row.addStretch(1)
        col = W.vbox(gap="none")
        col.addWidget(W.label(_("分块并发"), role="subtitle"))
        # **默认值读回真值,不写死。** `TransferManager` 的默认是
        # `cpu_workers()`(核数少的机器上不是 8)—— 写死 index 5 会让下拉
        # 说着 8 而实际在跑别的数。
        cur = int(getattr(self.shell.transfers, "chunk_workers", 0) or 0)
        idx = (CHUNK_CHOICES.index(cur) if cur in CHUNK_CHOICES
               else DEFAULT_CHUNK_INDEX)
        col.addWidget(W.combo([str(n) for n in CHUNK_CHOICES], index=idx,
                              on_change=self._set_chunks))
        row.addLayout(col)
        row.addWidget(W.button(_("全部取消"), on_click=self._cancel_all))
        row.addWidget(W.button(_("清除已完成"), on_click=self._clear_done))
        card.add_layout(row)
        return card

    # ------------------------------------------------------------ 契约

    def on_show(self) -> None:
        self.refresh()

    # ------------------------------------------------------------ 刷新

    def refresh(self) -> None:
        mgr = self.shell.transfers
        jobs = list(getattr(mgr, "jobs", ()) or ())
        speed = sum(float(getattr(j, "speed", 0.0) or 0.0)
                    for j in jobs if not j.finished)
        model = tv.page_model(jobs, total_speed=speed)
        _patch_names(model, jobs)
        stats = model["stats"]
        self.t_speed.set_value(stats["speed"])
        self.t_active.set_value(str(stats["active"]))
        self.t_queue.set_value(str(stats["queued"]))
        self.t_done.set_value(str(stats["done"]))

        any_rows = any(model["sections"].values())
        W.show_if(self.empty, not any_rows)
        for key, _title, _note in SECTIONS:
            W.show_if(self._cards[key], model["sections"].get(key))

        # 分区归属变了才重排版 —— 否则每 tick 重建会让界面闪、选中丢
        layout_key = tuple((k, tuple(r["id"] for r in model["sections"].get(k) or ()))
                           for k, _t, _n in SECTIONS)
        if layout_key != self._layout_key:
            self._layout_key = layout_key
            self._relayout(model)
        for key, _t, _n in SECTIONS:
            for r in model["sections"].get(key) or ():
                row = self._rows.get(r["id"])
                if row is not None:
                    row.apply(r)

    def _relayout(self, model: dict) -> None:
        """分区内**按文件夹分组**(老 UI 同款)。

        整夹下载会一次入队几十上百个文件;不分组的话那一列就是一片文件名,
        "这个文件夹下完了没有"要自己数。老 UI 是每组一个组头
        `▶ ▣ Dark 10 个文件 · 完成 10 · 失败 0`,进行中默认展开、
        排队/已完成默认折叠。
        """
        # **`self._rows` 整份重建,不是"补差"。** `clear_body()` 已经把上一轮
        # 的行控件都销毁了,而折叠起来的组这一轮**根本不建行** —— 沿用旧字典
        # 的话里面留着一批 C++ 对象已经没了的行,下一次 `refresh()` 去 `apply`
        # 就是 "Internal C++ object already deleted"(折叠一个组就能复现)。
        built: dict = {}
        for key, _t, _n in SECTIONS:
            card = self._cards[key]
            card.clear_body()
            rows = model["sections"].get(key) or []
            card.set_chip(str(len(rows)), "accent" if rows else None)
            for gname, grows in _by_group(rows):
                if gname:
                    open_ = self._open.get((key, gname), key == "run")
                    card.add(_GroupHead(self, key, gname, grows, open_))
                    if not open_:
                        continue
                for r in grows:
                    row = _JobRow(self, r)
                    built[r["id"]] = row
                    card.add(row)
        self._rows = built

    # ------------------------------------------------------------ 动作

    def toggle_group(self, key: str, name: str) -> None:
        self._open[(key, name)] = not self._open.get((key, name),
                                                     key == "run")
        self._layout_key = None       # 强制重排
        self.refresh()

    def cancel_group(self, name: str) -> None:
        """取消整组。整夹下载一次入队几十个,逐个点取消不现实。"""
        self.shell.transfers.cancel_group(name)
        self.refresh()

    def _set_chunks(self, idx: int) -> None:
        if 0 <= idx < len(CHUNK_CHOICES):
            self.shell.transfers.set_chunk_workers(CHUNK_CHOICES[idx])

    def demo_queue(self, n: int = 3) -> None:
        """排几个真任务进来(``--auto`` 用)。

        不是纯装饰:它把**整条传输链路**跑一遍(队列 → 分块并发 → 进度回调 →
        250ms 节流刷新 → 方块图),而这条链路在另外那套前端里因为
        ``TransferManager`` 压根没被构造过,从来没跑起来过。
        落盘到系统临时目录,不碰用户的下载文件夹。
        """
        import tempfile
        from pathlib import Path

        from astro_smb_qt.workers import with_client

        if self.shell.client_factory is None or not self.shell.shares:
            return
        share = self.shell.shares[0]
        factory = self.shell.transfers_demo_factory()
        out = Path(tempfile.gettempdir()) / "astro-smb-qt-demo"
        out.mkdir(parents=True, exist_ok=True)

        def work():
            def run(client):
                picked = []
                for entry in client.listdir(share, "Autorun\\Bias"):
                    if not entry.is_dir and entry.name.lower().endswith(".fit"):
                        picked.append((entry.path, entry.name, entry.size))
                    if len(picked) >= n:
                        break
                return picked
            return with_client(factory, run)

        def queue(picked):
            for path, name, size in picked:
                self.shell.transfers.submit_download(
                    share, path, out / name, name, size, group="Autorun/Bias")
            self.refresh()

        self.bg.run(work, on_done=queue,
                    on_error=lambda e: self.report(e, _("排演示任务")))

    def _cancel_all(self) -> None:
        self.shell.transfers.cancel_all()
        self.refresh()

    def _clear_done(self) -> None:
        self.shell.transfers.clear_finished()
        self._layout_key = ()
        self.refresh()


def _patch_names(model: dict, jobs) -> None:
    """补上任务名。

    **``views.transfers.row_model`` 读的是 ``job.name``,而 ``TransferJob``
    上那个字段叫 ``label``** —— ``getattr(job, "name", "")`` 一兜,每一行都变成
    ``(未命名)``,不报错。这是共享视图模型的缺陷(另外那套前端同样中招,
    只是它那边 ``TransferManager`` 压根没被构造过,所以从来没显示出来)。
    这一层不归本轮改,所以在这里按 ``job_id`` 把名字补回去,并在报告里记一笔。
    """
    by_id = {str(getattr(j, "job_id", "")): j for j in jobs}
    for rows in model["sections"].values():
        for r in rows:
            if r["name"] in ("", _("(未命名)")):
                job = by_id.get(r["id"])
                label = str(getattr(job, "label", "") or "") if job else ""
                if label:
                    r["name"] = label


def _by_group(rows) -> list[tuple[str, list]]:
    """行 → ``[(组名, 行)]``,**保持原顺序**。

    没有组名的行归到 `""` 一组、不加组头 —— 单个文件下载不该多一层。
    """
    out: list[tuple[str, list]] = []
    for r in rows:
        name = str(r.get("group") or "")
        if out and out[-1][0] == name:
            out[-1][1].append(r)
        else:
            out.append((name, [r]))
    return out


class _GroupHead(QWidget):
    """一个文件夹的组头:折叠箭头 + 名字 + 计数 + 「取消整组」。"""

    def __init__(self, page, key: str, name: str, rows: list, open_: bool):
        super().__init__()
        row = W.hbox(self, gap="sm")
        # **比常量,不比字面量。** 这两行原来写死 "完成"/"失败",而状态常量
        # 住在 `astro_smb_app.transfers` —— 常量一改(或一翻译)组头的计数
        # 就永远是 0,不报错。传输页的「排队」分区就是这么空了很久的。
        done = sum(1 for r in rows if r.get("status") == xf.DONE_S)
        fail = sum(1 for r in rows if r.get("status") == xf.ERROR)
        head = W.button(
            _("{0}  {name}   {1} 个文件 · 完成 {done}").format('▼' if open_ else '▶', len(rows), name=name, done=done) + (_(" · 失败 {fail}").format(
                fail=fail) if fail else ""),
            kind="ghost", on_click=lambda: page.toggle_group(key, name))
        row.addWidget(head, 1)
        if key != "done":
            row.addWidget(W.button(_("取消整组"), kind="danger",
                                   on_click=lambda: page.cancel_group(name)))
        return


class _JobRow(QWidget):
    """一个任务一行:名字 + 阶段胶囊 + 进度条 + 明细 + 分块方块图。"""

    def __init__(self, page: TransfersPage, model: dict):
        super().__init__()
        self._page = page
        self._id = model["id"]
        col = W.vbox(self, gap="xs")
        head = W.hbox(gap="sm")
        self._name = W.label(model["name"], role="body")
        self._phase = W.StatusChip(model["phase"])
        head.addWidget(self._name, 1)
        head.addWidget(self._phase)
        head.addWidget(W.button(_("取消"), kind="ghost", on_click=self._cancel))
        col.addLayout(head)

        from PySide6.QtWidgets import QProgressBar

        self._bar = QProgressBar()
        self._bar.setRange(0, 1000)
        self._bar.setTextVisible(False)
        col.addWidget(self._bar)
        self._detail = W.label(model["detail"], role="subtitle", wrap=True)
        col.addWidget(self._detail)
        self._blocks = W.BlockMap()
        col.addWidget(self._blocks)
        # 没有方块图时**说明为什么**(本地设备不分块)。什么都不说的话
        # 用户只能怀疑是坏了 —— 用户就是这么报上来的。
        self._noblocks = W.label("", role="faint")
        col.addWidget(self._noblocks)
        self.apply(model)

    def apply(self, m: dict) -> None:
        self._name.setText(m["name"] + (f"   ({m['group']})" if m.get("group") else ""))
        # 状态色优先(完成/失败/已取消),否则按**阶段**分色
        # **翻的是显示,查表用的还是原值** —— 两者混起来的话胶囊颜色会没
        self._phase.set(_(m["phase"]),
                        TONE_FOR_STATUS.get(m["status"])
                        or TONE_FOR_PHASE.get(m["phase"]) or "dim")
        self._bar.setValue(int(max(0.0, min(1.0, m["fraction"])) * 1000))
        self._detail.setText(m["detail"])
        blocks = m.get("blocks") or []
        W.show_if(self._blocks, blocks)
        if blocks:
            self._blocks.set_states(blocks)
        why = "" if blocks else str(m.get("no_blocks_why") or "")
        self._noblocks.setText(why)
        W.show_if(self._noblocks, why)

    def _cancel(self) -> None:
        try:
            self._page.shell.transfers.cancel_job(int(self._id))
        except (TypeError, ValueError):
            pass
        self._page.refresh()
