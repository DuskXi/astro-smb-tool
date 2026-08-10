"""页面基类与页头。

页面契约(与另外两套前端一致):

* ``on_show()`` —— 切到这一页时调。懒加载放这里,而且**必须幂等**
  (已在跑或已有数据就别重复拉)。
* ``on_connected(shares)`` —— 连接成功后外壳广播。
* ``on_close()`` —— 关窗清理(可选)。

页面里**不写样式**:所有外观来自 :mod:`astro_smb_qt.theme` 与
:mod:`astro_smb_qt.widgets`,门禁在 ``tests/test_qt_style.py``。
"""
from __future__ import annotations

import logging
from typing import Any

from PySide6.QtWidgets import QWidget

from astro_smb_qt import widgets as W
from astro_smb_qt.workers import Bg
from astro_smb.i18n import N_, gettext as _

log = logging.getLogger(__name__)


class Page(QWidget):
    """一页。``self.shell`` 是外壳,``self.bg`` 是这一页自己的后台执行器。

    每页各有一个 ``Bg`` —— **世代计数器必须是每页独立的**:浏览页换目录不该
    把导星页正在解析的日志作废掉。
    """

    #: 页头文案(子类覆盖)
    TITLE = N_("")
    SUBTITLE = N_("")

    def __init__(self, shell):
        super().__init__()
        self.shell = shell
        self.bg = Bg(self)
        shell.connected.connect(self._on_connected)
        shell.theme_changed.connect(self._on_theme)

    # -- 契约 ---------------------------------------------------------
    def on_show(self) -> None:
        pass

    def on_connected(self, shares: list[str]) -> None:
        pass

    def on_close(self) -> None:
        pass

    def on_theme(self) -> None:
        """配色切了 —— **有显示列表的页面必须在这里重新生成一遍**。

        `paintEvent` 里现取颜色的自绘件不用管(`update()` 就够);麻烦的是
        :class:`~astro_smb_qt.widgets.OpsCanvas` 那种 —— 它吃的是一串
        ``{"fill": "#4FBF87"}``,颜色在**生成 op 的那一刻**就烤进去了。
        切档之后 `update()` 只是把同一串旧颜色再画一遍:天球、甘特、treemap
        全都留在上一档的配色里,**不报错**,只是颜色不对。
        红光档尤其致命 —— 那一档存在的理由就是不破坏暗适应。
        """

    # -- 内部 ---------------------------------------------------------
    def _on_connected(self, shares) -> None:
        self.on_connected(list(shares))

    def _on_theme(self, _mode: str) -> None:
        """切配色后让自绘件与图片重画。

        QSS 那半边由外壳的 ``restyle`` 负责,但 :class:`~astro_smb_qt.widgets.ImageView`
        要重新做红光染色 —— 它画的是 QPixmap,样式表管不着。
        显示列表则要整个重生成,交给各页的 :meth:`on_theme`。
        """
        for view in self.findChildren(W.ImageView):
            view.refresh()
        try:
            self.on_theme()
        except Exception:                # noqa: BLE001
            # 重画失败不该把整个切档动作带崩 —— 那会让人以为"点了没反应"
            log.exception(_("%s 页重画失败"), type(self).__name__)
        self.update()

    def confirm(self, title: str, message: str, *,
                ok_text: str | None = None,
                cancel_text: str | None = None) -> bool:
        """见 :func:`astro_smb_qt.widgets.confirm`。**实现只有那一份** ——
        Shell 也要用它(语言切换),而复制一份的结果就是两边慢慢不一样。"""
        return W.confirm(self, title, message, ok_text=ok_text,
                         cancel_text=cancel_text)

    def ask_text(self, title: str, label: str, *, text: str = "",
                 ok_text: str | None = None) -> str:
        """要一行输入。返回空串 = 用户取消。

        **不用 `QInputDialog.getText`** —— 它的按钮是 Qt 默认的英文 OK/Cancel,
        而这一页别的对话框全是中文(独立验收点名的那条:删除确认改成中文之后,
        重命名弹出来还是 OK/Cancel,看着像两个软件)。
        """
        ok_text = _("确定") if ok_text is None else ok_text   # 见 `confirm`
        from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QLineEdit,
                                       QVBoxLayout)

        dlg = QDialog(self)
        dlg.setWindowTitle(title)
        col = QVBoxLayout(dlg)
        col.addWidget(W.label(label, role="subtitle"))
        edit = QLineEdit(text)
        edit.selectAll()
        col.addWidget(edit)
        box = QDialogButtonBox()
        ok = box.addButton(ok_text, QDialogButtonBox.AcceptRole)
        box.addButton(_("取消"), QDialogButtonBox.RejectRole)
        box.accepted.connect(dlg.accept)
        box.rejected.connect(dlg.reject)
        edit.returnPressed.connect(dlg.accept)
        col.addWidget(box)
        ok.setDefault(True)
        dlg.resize(420, dlg.sizeHint().height())
        return edit.text().strip() if dlg.exec() == QDialog.Accepted else ""

    def report(self, exc: BaseException, what: str = "") -> None:
        """后台任务失败的统一出口。**不要静默** ——
        另外两套前端都栽过"异常被 except 吞掉,页面停在忙态"。"""
        self.shell.notice(_("{what}失败: {exc}").format(
            what=what, exc=exc) if what else str(exc), "bad")


class PageHeader(QWidget):
    """页头:标题 + 副标题 + 右侧动作区。每一页都从它开始。"""

    def __init__(self, title: str, subtitle: str = "",
                 parent: QWidget | None = None):
        super().__init__(parent)
        row = W.hbox(self, gap="md")
        col = W.vbox(gap="none")
        self._t = W.label(title, role="pagetitle")
        # 页头副标题**不换行**:它是一句话,折成两行会把整个页头顶高一档,
        # 而且中文没有词边界,断点会落在词中间。
        self._s = W.label(subtitle, role="subtitle")
        W.show_if(self._s, subtitle)
        col.addWidget(self._t)
        col.addWidget(self._s)
        row.addLayout(col)
        # **标题右边先有一条工具区,再是弹簧,最后才是右侧动作区。**
        # 页面的主控件(拍摄记录的「刷新 / 夜次 / 合并计划」)属于前者 ——
        # 老 UI 就是紧跟标题排的,而全塞进右侧动作区会把它们甩到窗口另一头,
        # 视线要横跨整个屏幕去够(用户报的"夜次选择框跑右边去了")。
        self.tools = W.hbox(gap="sm")
        row.addSpacing(W._gap("lg"))
        row.addLayout(self.tools)
        row.addStretch(1)
        self.actions = W.hbox(gap="sm")
        row.addLayout(self.actions)

    def add_tool(self, w: QWidget) -> QWidget:
        """加到**左侧**工具区(紧跟标题)。页面的主控件用这个。"""
        self.tools.addWidget(w)
        return w

    def set_subtitle(self, text: str) -> None:
        self._s.setText(text)
        W.show_if(self._s, text)

    def add_action(self, w: QWidget) -> QWidget:
        self.actions.addWidget(w)
        return w


def page_layout(page: QWidget) -> Any:
    """页面根布局。统一内边距与卡间距,免得每页各调一套。"""
    return W.vbox(page, gap="card", pad="page")
