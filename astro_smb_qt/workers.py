"""后台执行:线程 + 世代计数器 + 信号编组。

三条硬约束(违反了都不报错,只是行为不对):

1. **impacket 连接不是线程安全的。** 每个后台任务自己 ``client_factory()``
   建一条连接、用完就关,绝不跨线程共用。
2. **Qt 控件只能在 GUI 线程碰。** 后台只算数据,结果一律经 signal 回主线程。
   worker 里 ``setText`` 在 Qt 上通常不当场崩,而是随机的重绘错乱/偶发段错误 ——
   比崩溃更难查。
3. **迟到的结果要按世代丢弃。** 用户点得比网络快是常态;没有代次就会
   "进了 B 目录却显示 A 目录的内容"(这个仓库的另一套前端栽过)。

取消的语义是**结果作废**,不是中断线程:线程照样跑完(SMB 读到一半没法安全
掐断),只是没人要它的结果。真正要停的长任务(搜索、扫描、占用统计)另外走
``threading.Event``,核心库那几个 API 都收 ``cancel``。
"""
from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from astro_smb.i18n import gettext as _

log = logging.getLogger(__name__)


class _Emitter(QObject):
    """一次任务的信号载体。

    ``QRunnable`` 不是 ``QObject``,发不了信号 —— 所以每个任务配一个这个,
    并且**由 :class:`Bg` 持有引用直到回调跑完**。不持有的话它会在 ``run()``
    还在跑的时候被 GC,信号发到一个已经死掉的对象上(表现为"任务跑了但界面
    永远不更新")。
    """

    done = Signal(object)
    failed = Signal(object)
    progress = Signal(object)


class _Job(QRunnable):
    def __init__(self, fn: Callable[..., Any], em: _Emitter, wants_report: bool):
        super().__init__()
        self._fn = fn
        self._em = em
        self._wants = wants_report
        self.setAutoDelete(True)

    def run(self) -> None:  # pragma: no cover - 线程体
        try:
            value = self._fn(self._em.progress.emit) if self._wants else self._fn()
        except BaseException as exc:      # noqa: BLE001 - 要把一切都送回 UI
            log.debug(_("后台任务失败"), exc_info=True)
            try:
                self._em.failed.emit(exc)
            except RuntimeError:
                pass                      # 窗口已关,信号对象没了
        else:
            try:
                self._em.done.emit(value)
            except RuntimeError:
                pass


class Bg(QObject):
    """页面级的后台执行器。

    典型用法::

        gen = self.bg.bump()                       # 作废之前所有在途结果
        self.bg.run(lambda: work(...),
                    on_done=self._apply, gen=gen)

    ``on_done`` / ``on_error`` **一定在 GUI 线程上被调用**(信号默认是
    队列连接,因为发信号的是另一个线程)。
    """

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._gen = 0
        self._alive: set[_Emitter] = set()
        self._pool = QThreadPool.globalInstance()

    # -- 世代 ---------------------------------------------------------
    @property
    def generation(self) -> int:
        return self._gen

    def bump(self) -> int:
        """开一代新的:之前所有在途任务的结果从此作废。"""
        self._gen += 1
        return self._gen

    def stale(self, gen: int) -> bool:
        return gen != self._gen

    # -- 执行 ---------------------------------------------------------
    def run(self, fn: Callable[..., Any], *,
            on_done: Callable[[Any], None] | None = None,
            on_error: Callable[[BaseException], None] | None = None,
            on_progress: Callable[[Any], None] | None = None,
            gen: int | None = None) -> None:
        """把 ``fn`` 丢到线程池。给了 ``on_progress`` 时 ``fn`` 收一个 ``report`` 可调用。"""
        em = _Emitter()
        self._alive.add(em)

        def _guard(cb):
            def _inner(payload):
                self._alive.discard(em)
                if gen is not None and self.stale(gen):
                    return                      # 迟到的结果,整份丢弃
                if cb is not None:
                    cb(payload)
            return _inner

        em.done.connect(_guard(on_done), Qt.QueuedConnection)
        em.failed.connect(_guard(on_error or self._default_error),
                          Qt.QueuedConnection)
        if on_progress is not None:
            def _prog(payload):
                if gen is None or not self.stale(gen):
                    on_progress(payload)
            em.progress.connect(_prog, Qt.QueuedConnection)
        self._pool.start(_Job(fn, em, on_progress is not None))

    @staticmethod
    def _default_error(exc: BaseException) -> None:
        log.warning(_("后台任务出错(无人处理): %s"), exc)


class CancelToken:
    """协作式取消。核心库那几个长任务 API 都收 ``threading.Event``。"""

    def __init__(self) -> None:
        self.event = threading.Event()

    def cancel(self) -> None:
        self.event.set()

    @property
    def cancelled(self) -> bool:
        return self.event.is_set()


def with_client(factory: Callable[[], Any], fn: Callable[[Any], Any]) -> Any:
    """借一条**自己的**连接跑 ``fn``,跑完一定关掉。

    ``factory`` 每次返回一条新连接(已 connect)。共用一条跨线程用会在
    impacket 内部把两个请求的响应串起来 —— 症状是随机的解析错误。
    """
    client = factory()
    try:
        return fn(client)
    finally:
        try:
            client.close()
        except Exception:                 # noqa: BLE001 - 关连接失败不该盖住正事
            log.debug(_("关闭连接失败"), exc_info=True)
