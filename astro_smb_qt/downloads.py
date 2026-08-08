"""**联网下载管理**窗口 —— 这软件会去网上取什么,在这里一次看全、一次备齐。

## 为什么是一个独立窗口,不是第十页

它跟九个页面不是一回事:那九个是"看设备上的东西",这一个是"管本机的
缓存"。而且用它的时机也不一样 —— 典型场景是**出发前在有网的地方把东西
先下好**,那时候还没有设备可连,九个页面基本都是空的。

做成独立窗口还有一个实际好处:**下载归下载,不挡着你继续用**。原来星表
只能在影像查看页点「板解算」的时候顺带下,下的过程中那一页就被占住了。

## 每一行都要说清三件事

**是什么用的、从哪儿取、现在什么状态。** 第二件尤其要紧:这软件可能被
架在野外用手机热点跑,"它到底联不联网、联哪儿"是个要提前知道的事,
而原来这信息散在三个页面的代码里,界面上根本看不到。

判断哪一行是哪一样一律用 `key`(``catalog``/``survey``/``three``),
不用标题 —— 标题是要翻译的。
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget

from astro_smb.i18n import gettext as _
from astro_smb.util import human_size
from astro_smb_app.views import downloads as DL
from astro_smb_qt import widgets as W
from astro_smb_qt.pages.base import PageHeader
from astro_smb_qt.workers import Bg, CancelToken


class _Row(QWidget):
    """一样资产的一张卡:标题 / 用途 / 来源 / 状态 / 进度条 / 按钮。"""

    def __init__(self, key: str, on_start, on_stop, on_remove):
        super().__init__()
        self.key = key
        self._on_start, self._on_stop = on_start, on_stop
        self._on_remove = on_remove
        self.busy = False

        # **往 `card.body` 里加,不许 `card.setLayout()`。**
        # `W.Card` 构造时已经装了自己的布局;再 setLayout 一次 Qt 只在
        # stderr 上嘟囔一句然后**忽略** —— 控件全落在一个没人显示的
        # 孤儿布局里,卡片渲染成一条空黑杠。测试全绿,因为 `.text()`
        # 照样读得到。是截图看出来的。
        card = W.Card()
        col = card.body

        head = W.hbox(gap="sm")
        self.title = W.label("", role="strong")
        head.addWidget(self.title)
        head.addStretch(1)
        self.state = W.label("", role="subtitle")
        head.addWidget(self.state)
        col.addLayout(head)

        self.why = W.label("", role="faint", wrap=True)
        col.addWidget(self.why)
        self.source = W.label("", role="faint", wrap=True)
        col.addWidget(self.source)

        # **进度条常驻,不是下载时才建。** 建出来再塞进布局会让整张卡在
        # 点下去那一刻跳一下高度,而那正是用户盯着看的时刻。
        self.bar = W.Gauge(0.0)
        self.bar.setVisible(False)
        col.addWidget(self.bar)

        btns = W.hbox(gap="sm")
        btns.addStretch(1)
        self.rm_btn = W.button(_("删除本地副本"),
                               on_click=lambda: self._on_remove(self.key))
        btns.addWidget(self.rm_btn)
        self.go_btn = W.button(_("下载"), kind="primary", on_click=self._click)
        btns.addWidget(self.go_btn)
        col.addLayout(btns)

        outer = W.vbox(self, gap="none")
        outer.addWidget(card)

    def _click(self) -> None:
        (self._on_stop if self.busy else self._on_start)(self.key)

    def apply(self, row: dict) -> None:
        """把共享层给的状态画上去。**文案全部来自那一层**,这里不判读。"""
        self.title.setText(row["title"])
        self.why.setText(row["why"])
        self.source.setText(
            _("来源:{host} · {note}").format(host=row["source"],
                                             note=row["source_note"]))
        self.state.setText(DL.state_line(row))
        self.rm_btn.setEnabled(bool(row["bytes"]) and not self.busy)
        if not self.busy:
            self.bar.setVisible(False)
            self.go_btn.setText(_("重新下载") if row["ready"] else _("下载"))

    def start(self) -> None:
        self.busy = True
        self.bar.set_frac(0.0)
        W.show_if(self.bar, True)      # 不能裸 setVisible(True),见 W.show_if
        self.go_btn.setText(_("停止"))
        self.rm_btn.setEnabled(False)
        self.state.setText(_("正在连接…"))

    def tick(self, done: int, total: int) -> None:
        if total > 0:
            self.bar.set_frac(min(1.0, done / total))
            self.state.setText(_("{0} / {1}").format(human_size(done),
                                                     human_size(total)))
        else:
            # 总量未知(有的服务器不给 Content-Length)—— **别画一根假的条**,
            # 只报已下多少。画个乱动的条比不画更误导。
            self.state.setText(human_size(done))

    def finish(self, note: str = "") -> None:
        self.busy = False
        self.bar.setVisible(False)
        self.go_btn.setText(_("下载"))
        if note:
            self.state.setText(note)


class DownloadsWindow(QWidget):
    """联网下载管理。**独立顶层窗口**,不进九页的导航。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, Qt.Window)
        self.setWindowTitle(_("联网下载 — Astro SMB Tool"))
        self.resize(720, 640)
        self.bg = Bg(self)
        self._cancel: dict[str, CancelToken] = {}
        self._rows: dict[str, _Row] = {}
        self._build()
        self.refresh()

    def _build(self) -> None:
        root = W.vbox(self, gap="md")
        root.setContentsMargins(16, 16, 16, 16)
        root.addWidget(PageHeader(
            _("联网下载"),
            _("这软件会联网取的全部东西都在这里。出发前在有网的地方先备齐,"
              "野外就不用再联了。")))

        for asset in DL.ASSETS:
            row = _Row(asset.key, self._start, self._stop, self._remove)
            self._rows[asset.key] = row
            root.addWidget(row)

        root.addStretch(1)
        self.foot = W.label("", role="faint", wrap=True)
        root.addWidget(self.foot)

    # -- 状态 ---------------------------------------------------------
    def refresh(self) -> None:
        for row in DL.rows():
            self._rows[row["key"]].apply(row)
        got, total = DL.summary()
        self.foot.setText(
            _("{got} / {total} 已就绪。缺哪一样都不会让软件用不了 —— "
              "只是对应的那块功能降级。").format(got=got, total=total))

    def on_theme(self) -> None:
        """配色切了。样式表是应用级的,Qt 自己会重刷;这里只要把
        文案与状态重新算一遍(`state_line` 里有翻译)。"""
        self.refresh()

    # -- 下载 ---------------------------------------------------------
    def _start(self, key: str) -> None:
        token = CancelToken()
        self._cancel[key] = token
        widget = self._rows[key]
        widget.start()

        def work(report):
            return DL.run(key, progress=lambda d, t: report((d, t)),
                          cancel=token.event)

        def done(_path):
            widget.finish()
            self._cancel.pop(key, None)
            self.refresh()
            self._tell_shell()

        def fail(exc):
            # **取消不是失败。** 用户自己点的停止,报一句红字会让他以为出事了。
            if token.cancelled:
                widget.finish(_("已停止"))
            else:
                widget.finish(_("失败:{exc}").format(exc=exc))
            self._cancel.pop(key, None)
            self.refresh()

        # **不传 `gen`。** 世代是"新的一次把旧的作废"用的,而这一页三样东西
        # 是**各下各的**:传了的话点第二样时 `bump()` 会把第一样的回调整份
        # 丢掉 —— 它还在下,而进度条从此不动、下完也不刷新状态。
        # 每样自己的重复点击由 `_Row.busy` 挡住(按钮那时是「停止」)。
        self.bg.run(work, on_done=done, on_error=fail,
                    on_progress=lambda p: widget.tick(*p))

    def _stop(self, key: str) -> None:
        token = self._cancel.get(key)
        if token is not None:
            token.cancel()
        self._rows[key].state.setText(_("正在停止…"))

    def _tell_shell(self) -> None:
        """边栏那个「已就绪 N/M」角标要跟着变 —— 否则下完了它还写着旧数,
        用户以为没成功。父窗口不是 Shell 时(测试里就不是)静默跳过。"""
        fn = getattr(self.parent(), "refresh_downloads_badge", None)
        if callable(fn):
            fn()

    def _remove(self, key: str) -> None:
        if not DL.remove(key):
            self._rows[key].state.setText(_("删不掉 —— 文件可能正被占用"))
        self.refresh()
        self._tell_shell()

    def closeEvent(self, event) -> None:      # noqa: N802 - Qt 接口
        """关窗要把在途下载取消掉。

        **不然它继续在后台跑**,而进度条已经没了 —— 用户既看不到它、
        也停不了它,只有退出整个程序才能了结。
        """
        for token in self._cancel.values():
            token.cancel()
        super().closeEvent(event)
