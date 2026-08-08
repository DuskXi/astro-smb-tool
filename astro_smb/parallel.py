"""文件内分块并发下载(#1)。

把单个文件切成若干块,用多条独立 SMB 连接(impacket 连接非线程安全,每个
worker 各持一个 client)并行拉取,写到预分配好的 `.part` 临时文件的对应偏移,
全部块完成后原子改名为目标文件(失败/取消时删除临时文件,最终路径上绝不留下
"大小等于全长但内容有空洞"的文件——那种文件会被 download_file(resume=True)
按大小==总长误判为已完成,造成静默数据损坏)。
并发数默认基于 CPU 核数(有上限,因为设备侧很快饱和)。

暴露 per-chunk 状态回调(pending/active/done),供传输监控页画 aria2NG 式方块图。
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from astro_smb.client import AstroSmbClient, SmbClientError, TransferCancelled
from astro_smb.i18n import gettext as _

MiB = 1 << 20

# chunk 状态
PENDING, ACTIVE, DONE = 0, 1, 2


def cpu_workers(cap: int = 8) -> int:
    """基于 CPU 核数的默认并发数(留有上限——SMB 设备侧通常 4~8 连接即饱和)。"""
    return min(cap, max(2, os.cpu_count() or 4))


def plan_chunks(total: int, target_chunks: int = 64,
                min_chunk: int = 1 * MiB, max_chunk: int = 16 * MiB) -> tuple[int, int]:
    """返回 (chunk_size, n_chunks)。让块数量大致落在 target_chunks 附近,
    这样监控页的方块图既有意义又不至于太碎。"""
    if total <= 0:
        return (min_chunk, 0)
    chunk = total // target_chunks if target_chunks else total
    chunk = max(min_chunk, min(max_chunk, chunk or min_chunk))
    n = -(-total // chunk)  # ceil
    return (chunk, n)


@dataclass
class ParallelResult:
    n_chunks: int
    chunk_size: int
    workers: int
    total: int = 0          # 实际下载的字节数(以服务器 stat 为准)


class ParallelDownloader:
    def __init__(self, client_factory: Callable[[], AstroSmbClient],
                 workers: int | None = None, io_chunk: int = 1 * MiB,
                 max_retries: int = 3):
        self._factory = client_factory
        self.workers = workers or cpu_workers()
        self.io_chunk = io_chunk
        self.max_retries = max_retries

    def download(
        self,
        share: str,
        path: str,
        local_path: Path,
        total: int,
        on_plan: Callable[[int, int, int], None] | None = None,   # (n_chunks, chunk_size, workers)
        on_block: Callable[[int, int], None] | None = None,        # (chunk_idx, state)
        on_progress: Callable[[int], None] | None = None,          # (delta_bytes)
        cancel: threading.Event | None = None,
    ) -> ParallelResult:
        local_path = Path(local_path)
        # **权威大小以服务器为准,绝不信调用方传进来的 total。**
        # client.download_file 一直是自己 stat 的,只有这条并行路径信外部值 ——
        # 这个不对称是个静默截断源:调用方拿到的 size 可能来自任意旧的目录缓存
        # (或拍到一半的帧),偏小时我们会 truncate(.part) 到那个大小、只覆盖
        # [0,total)、然后 os.replace 成最终文件 —— 文件名与完整帧一模一样,
        # 不抛任何异常。审查用 LocalBackend 实测复现:真实 5 MiB 文件传入
        # total=2 MiB → 落地 2097152 字节且不报错。
        probe = None
        try:
            probe = self._factory()
            probe.connect()
            real = probe.stat(share, path).size
            if real != total:
                total = int(real)
        except Exception:
            # 校正是**咨询性**的:取不到就用调用方给的值(总比不下强)。
            # 这里绝不能因为 stat 失败而让整个下载失败。
            pass
        finally:
            if probe is not None:
                try:
                    probe.close()
                except Exception:
                    pass
        chunk_size, n_chunks = plan_chunks(total)
        nworkers = max(1, min(self.workers, n_chunks or 1))
        if on_plan:
            on_plan(n_chunks, chunk_size, nworkers)

        local_path.parent.mkdir(parents=True, exist_ok=True)
        # 预分配 .part 临时文件到目标大小,worker 各自 seek+write 不重叠区间;
        # 成功后才 os.replace 到最终路径(与 preview 缓存同一模式)
        part_path = local_path.with_name(local_path.name + ".part")
        try:
            with open(part_path, "wb") as f:
                if total:
                    f.truncate(total)
        except OSError as e:
            raise SmbClientError(_("预分配本地文件 {part_path} 失败: {e}").format(
                part_path=part_path, e=e)) from e
        if total == 0:
            self._finalize(part_path, local_path)
            return ParallelResult(0, chunk_size, nworkers, 0)

        idx_lock = threading.Lock()
        prog_lock = threading.Lock()
        next_idx = [0]
        errors: list[Exception] = []

        def take() -> int | None:
            with idx_lock:
                i = next_idx[0]
                if i >= n_chunks:
                    return None
                next_idx[0] = i + 1
                return i

        def report_bytes(delta: int) -> None:
            if on_progress:
                with prog_lock:
                    on_progress(delta)

        def worker() -> None:
            client = self._factory()
            fh = None
            try:
                client.connect()
                fh = open(part_path, "r+b")
                while True:
                    if cancel is not None and cancel.is_set():
                        return
                    idx = take()
                    if idx is None:
                        return
                    off = idx * chunk_size
                    length = min(chunk_size, total - off)
                    if on_block:
                        on_block(idx, ACTIVE)
                    self._fetch_chunk(client, share, path, off, length, fh,
                                      cancel, report_bytes)
                    if on_block:
                        on_block(idx, DONE)
            except TransferCancelled:
                if cancel is not None:
                    cancel.set()
            except Exception as e:  # noqa: BLE001
                errors.append(e)
                if cancel is not None:
                    cancel.set()
            finally:
                if fh is not None:
                    try:
                        fh.close()
                    except Exception:
                        pass
                try:
                    client.close()
                except Exception:
                    pass

        threads = [threading.Thread(target=worker, name=f"pdl-{i}", daemon=True)
                   for i in range(nworkers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        if errors:
            self._discard(part_path)
            first = errors[0]
            if isinstance(first, SmbClientError):
                raise first
            raise SmbClientError(_("并发下载失败: {__name__}: {first}").format(
                __name__=type(first).__name__, first=first))
        if cancel is not None and cancel.is_set():
            self._discard(part_path)
            raise TransferCancelled(_("已取消: {share}/{path}").format(share=share, path=path))
        self._finalize(part_path, local_path)
        return ParallelResult(n_chunks, chunk_size, nworkers, total)

    @staticmethod
    def _finalize(part_path: Path, local_path: Path) -> None:
        """全部块完成后把 .part 原子改名为目标文件;失败时删除临时文件。"""
        try:
            os.replace(part_path, local_path)
        except OSError as e:
            ParallelDownloader._discard(part_path)
            raise SmbClientError(_("替换目标文件 {local_path} 失败: {e}").format(
                local_path=local_path, e=e)) from e

    @staticmethod
    def _discard(part_path: Path) -> None:
        try:
            part_path.unlink()
        except OSError:
            pass

    def _fetch_chunk(self, client, share, path, off, length, fh, cancel, report_bytes):
        """读一个块,连接类错误重连后重试(弱网自适应)。"""
        attempt = 0
        while True:
            written = [0]

            def on_bytes(delta, _w=written):
                _w[0] += delta
                report_bytes(delta)

            try:
                client.download_range(share, path, off, length, fh,
                                      cancel=cancel, on_bytes=on_bytes)
                return
            except TransferCancelled:
                raise
            except SmbClientError as e:
                # 回退本块已计的进度,重试整块(download_range 会重新 seek)
                if written[0]:
                    report_bytes(-written[0])
                attempt += 1
                if attempt > self.max_retries or not _is_conn_error(e):
                    raise
                try:
                    client.reconnect()
                except Exception:
                    pass


def _is_conn_error(e: SmbClientError) -> bool:
    """断连类错误吗 —— 值不值得 `reconnect()` 之后重试这一块。

    **判结构化标志,不在消息里搜关键词。** 原来是
    ``any(k in str(e) for k in (_("中断"), _("超时"), _("连接"), …))`` ——
    在**翻译过的**消息里找**翻译过的**关键词。中文下"下载超时"恰好含
    "超时",换一种语言未必:译文里那个孤零零的词不一定是整句的子串。
    失效的样子是:分块下载遇到断连不再重连重试,直接把异常抛上去 ——
    不报错,只是大文件的成功率悄悄掉下来,而且只在非中文界面上。

    i18n 那一轮把 `transfers._is_retryable` 与 `client.makedirs` 都改了,
    **漏了这里**。剩下那两个 ASCII 词是兜底:有些异常没经过 `_run`
    (底层库自己抛的 socket 文本),它们与语言无关。
    """
    if getattr(e, "retryable", False):
        return True
    s = str(e)
    return any(k in s for k in ("timeout", "reset", "Broken pipe"))
