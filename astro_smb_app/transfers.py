"""传输队列:上传/下载任务在工作线程池执行。

线程模型:每个任务(或其分块 worker)持有独立的 AstroSmbClient(impacket 连接
非线程安全)。UI 更新经 on_update 回调(app 层负责 DispatcherQueue 编组)。

能力:
- 文件间并发(线程池 max_workers);
- 文件内分块并发(大文件走 ParallelDownloader,多连接分块,#1);
- 重传 + 弱网自适应(连接类失败指数退避重试,下载断点续传/分块重试);
- 冲突处理(下载目标已存在时 改名/跳过/覆盖);
- 每任务阶段(phase)与 per-chunk 方块状态,供监控页(#2)。
"""

from __future__ import annotations

import itertools
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from astro_smb.client import AstroSmbClient, SmbClientError, TransferCancelled
from astro_smb.parallel import ACTIVE, ParallelDownloader, cpu_workers
from astro_smb.i18n import N_, gettext as _

_id_counter = itertools.count(1)

# 任务状态
#: **这几个既是身份也是文案。** 到处拿它们做比较(分区归属、取色、
#: 进度条满不满),所以**存的必须是 msgid**;只有真正画到屏幕上的那一处
#: 才 `_()`。翻在这里的话每一处比较都会静默失效。
QUEUED, RUNNING, DONE_S, ERROR, CANCELLED, SKIPPED = (
    N_("排队中"), N_("进行中"), N_("完成"), N_("失败"),
    N_("已取消"), N_("已跳过"))

# 阶段(phase)
#: 阶段同理:`TONE_FOR_PHASE` 拿它查色,而胶囊上又要显示它。
PH_QUEUE, PH_CONNECT, PH_META, PH_TRANSFER, PH_DONE = (
    N_("排队"), N_("连接中"), N_("元数据"), N_("传输"), N_("完成"))

# 冲突策略
CONFLICT_RENAME, CONFLICT_SKIP, CONFLICT_OVERWRITE = "rename", "skip", "overwrite"

# 大于此大小的单文件下载启用分块并发
PARALLEL_THRESHOLD = 16 << 20


@dataclass
class TransferJob:
    kind: str  # "download" | "upload" | "download_dir" | "upload_dir"
    label: str
    detail: str = ""
    group: str | None = None  # 所属文件夹显示名(文件夹展开的逐文件任务);None=散文件
    total: int = 0
    done: int = 0
    status: str = QUEUED
    phase: str = PH_QUEUE
    error: str = ""
    attempt: int = 0
    parallel: bool = False
    blocks: list[int] = field(default_factory=list)  # per-chunk 状态 0/1/2
    n_chunks: int = 0
    chunk_size: int = 0
    workers: int = 1
    host: str = ""      # 提交时绑定的设备地址(换设备后旧任务仍走旧设备)
    local_device: bool = False   # 提交时那台是本地磁盘(卡直插)⇒ 不走分块并发
    job_id: int = field(default_factory=lambda: next(_id_counter))
    cancel: threading.Event = field(default_factory=threading.Event)
    started_at: float = 0.0
    finished_at: float = 0.0
    _last_done: int = 0
    _last_time: float = 0.0
    speed: float = 0.0

    @property
    def is_dir(self) -> bool:
        return self.kind.endswith("_dir")

    @property
    def is_download(self) -> bool:
        return self.kind.startswith("download")

    @property
    def finished(self) -> bool:
        return self.status in (DONE_S, ERROR, CANCELLED, SKIPPED)

    @property
    def running(self) -> bool:
        return self.status == RUNNING

    def progress_fraction(self) -> float:
        if self.total <= 0:
            return 0.0
        return min(1.0, self.done / self.total)

    def eta(self) -> float:
        if self.speed <= 0 or self.total <= 0:
            return -1.0
        return max(0.0, (self.total - self.done) / self.speed)

    def _tick_speed(self) -> None:
        now = time.monotonic()
        if self._last_time == 0.0:
            self._last_time, self._last_done = now, self.done
            return
        dt = now - self._last_time
        if dt >= 0.5:
            inst = (self.done - self._last_done) / dt
            self.speed = inst if self.speed == 0 else (0.6 * self.speed + 0.4 * inst)
            self._last_time, self._last_done = now, self.done


def _is_local_backend(backend) -> bool:
    """这个后端是不是本地磁盘。**不 import astro_smb.backend** —— 传输层要能被
    测试桩驱动,鸭子判定即可(backend.is_local 是 StorageBackend Protocol 的一部分)。
    """
    return bool(getattr(backend, "is_local", False))


class TransferManager:
    def __init__(
        self,
        client_factory: Callable[[], AstroSmbClient],
        on_update: Callable[[TransferJob], None],
        max_workers: int = 3,
        max_retries: int = 4,
        conflict: str = CONFLICT_RENAME,
        chunk_workers: int | None = None,
    ):
        self._factory = client_factory
        self._on_update = on_update
        self._max_retries = max_retries
        self.conflict = conflict
        self.chunk_workers = chunk_workers or cpu_workers()
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="smb-xfer")
        self.jobs: list[TransferJob] = []
        self._lock = threading.Lock()

    def set_workers(self, n: int) -> None:
        old = self._pool
        self._pool = ThreadPoolExecutor(max_workers=max(1, n), thread_name_prefix="smb-xfer")
        old.shutdown(wait=False)

    def set_chunk_workers(self, n: int) -> None:
        self.chunk_workers = max(1, n)

    # ---------- 设备绑定 ----------

    def _bind_factory(self, job: TransferJob) -> Callable[[], AstroSmbClient]:
        """提交时绑定设备:此刻就向外部 factory 要一个 client 当模板,任务执行与
        每次重试都从它 clone 派生连接。

        否则 work 闭包是在执行时才调 self._factory(),而该 factory 捕获的是 shell
        的**当前** client —— 换设备后仍在排队/重试的任务会连到新设备去取同名路径,
        可能取回错误设备的文件。任务属于提交时那台设备,与"当前设备"不一致也照做。
        """
        binder = self._factory()
        if binder is None or not hasattr(binder, "clone"):
            return self._factory        # 非 client 工厂(测试桩等):保持原行为
        job.host = getattr(binder, "host", "") or ""
        job.local_device = _is_local_backend(binder)
        return lambda: binder.clone()

    # ---------- 提交 ----------

    def submit_download(self, share: str, rpath: str, local: Path,
                        label: str, size: int, resume: bool = True,
                        group: str | None = None) -> TransferJob:
        job = TransferJob(kind="download", label=label, total=size, group=group)
        local = self._resolve_conflict(local, job)
        if local is None:
            return job

        make_client = self._bind_factory(job)
        # 本地设备(ASIAIR 卡直插)不走分块并发:分块的全部价值是掩盖 SMB 的
        # 单流 RTT 瓶颈(实测单流 6 MiB/s、8 并发才 9.6),而本地盘顺序读就有
        # 1.48 GB/s。对本地盘开 8 个句柄同时 seek+write 只会打乱磁盘调度、
        # 白占句柄,还让监控页画一堆瞬间就绿的方块。
        use_parallel = (size >= PARALLEL_THRESHOLD and self.chunk_workers > 1
                        and not job.local_device)

        def work(resume_ok: bool) -> None:
            # **「元数据」这一段以前根本走不到。** `PH_META` 定义了、监控页
            # 也给它配了色(老 UI `_monitor.py` 那条蓝色分支),但**全仓库
            # 没有一处给 job.phase 赋过它** —— 建连接、开文件、算分块这些
            # "还没开始搬字节"的时间被算进了「传输」,于是速度看着莫名其妙
            # 地低,而阶段标签永远只有两种。
            #
            # 这里把标签放回它该在的地方:搬字节之前都算元数据。
            job.phase = PH_META
            self._notify(job, throttled=False)
            if use_parallel:
                # 重试也重走并行:失败的并行下载只留 .part 或什么都不留,最终路径上
                # 不会有可续传的半成品;若目标处有旧文件(覆盖模式),对它 resume 会
                # 因大小==总长被 download_file 误判为已完成
                job.parallel = True
                job.done = 0
                # 分块并发要先各开一条连接、算分块方案(`on_plan` 回调)——
                # 那一段仍是元数据,`_on_plan` 拿到方案时才切「传输」
                pd = ParallelDownloader(make_client, workers=self.chunk_workers)
                res = pd.download(
                    share, rpath, local, size,
                    on_plan=self._on_plan(job),
                    on_block=self._on_block(job),
                    on_progress=self._on_delta(job),
                    cancel=job.cancel)
                # 下载器以服务器 stat 为准;真实大小与提交时的不一致(目录缓存
                # 陈旧、或帧当时还在写)就把进度基准改回来,免得显示 140%/60%
                if res.total and res.total != job.total:
                    job.total = res.total
            else:
                client = make_client()
                try:
                    client.connect()
                    # 连上了、要开始搬字节了,这时候才是「传输」
                    job.phase = PH_TRANSFER
                    client.download_file(
                        share, rpath, local,
                        progress=self._file_progress(job), cancel=job.cancel,
                        resume=resume_ok)
                finally:
                    client.close()

        self._start(job, work)
        return job

    def submit_download_dir(self, share: str, rpath: str, local: Path, label: str) -> TransferJob:
        # 注:浏览页的文件夹下载已改为「展开为逐文件任务」(见 _browser._queue_download),
        # 不再走这里;保留此入口供其他调用方/兼容使用。
        job = TransferJob(kind="download_dir", label=label)
        make_client = self._bind_factory(job)

        def work(resume_ok: bool) -> None:
            job.phase = PH_TRANSFER
            client = make_client()
            try:
                client.connect()
                client.download_dir(share, rpath, local,
                                    progress=self._dir_progress(job),
                                    cancel=job.cancel, resume=True)
            finally:
                client.close()

        self._start(job, work)
        return job

    def submit_upload(self, local: Path, share: str, rpath: str, label: str) -> TransferJob:
        job = TransferJob(kind="upload", label=label, total=local.stat().st_size)
        make_client = self._bind_factory(job)

        def work(resume_ok: bool) -> None:
            job.phase = PH_TRANSFER
            client = make_client()
            try:
                client.connect()
                client.upload_file(local, share, rpath,
                                   progress=self._file_progress(job), cancel=job.cancel)
            finally:
                client.close()

        self._start(job, work)
        return job

    def submit_upload_dir(self, local: Path, share: str, rpath: str, label: str) -> TransferJob:
        job = TransferJob(kind="upload_dir", label=label)
        make_client = self._bind_factory(job)

        def work(resume_ok: bool) -> None:
            job.phase = PH_TRANSFER
            client = make_client()
            try:
                client.connect()
                client.upload_dir(local, share, rpath,
                                  progress=self._dir_progress(job), cancel=job.cancel)
            finally:
                client.close()

        self._start(job, work)
        return job

    # ---------- 冲突处理 ----------

    def _resolve_conflict(self, local: Path, job: TransferJob) -> Path | None:
        if not local.exists():
            return local
        if self.conflict == CONFLICT_OVERWRITE:
            return local
        if self.conflict == CONFLICT_SKIP:
            job.status = SKIPPED
            job.phase = PH_DONE
            job.detail = _("目标已存在")
            with self._lock:
                self.jobs.append(job)
            job.finished_at = time.time()
            self._notify(job, throttled=False)
            return None
        base, ext = local.stem, local.suffix
        n, cand = 1, local
        while cand.exists():
            cand = local.with_name(f"{base} ({n}){ext}")
            n += 1
        return cand

    # ---------- 进度回调 ----------

    def _file_progress(self, job: TransferJob):
        def cb(done: int, total: int) -> None:
            job.done, job.total = done, total
            job._tick_speed()
            self._notify(job, throttled=done < total)
        return cb

    def _dir_progress(self, job: TransferJob):
        state = {"base": 0, "cur_total": 0, "cur_file": ""}

        def cb(name: str, done: int, total: int) -> None:
            if name != state["cur_file"]:
                state["base"] += state["cur_total"]
                state["cur_file"] = name
                state["cur_total"] = total
                job.detail = name.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
            job.done = state["base"] + done
            job.total = 0
            job._tick_speed()
            self._notify(job, throttled=True)
        return cb

    def _on_plan(self, job: TransferJob):
        def cb(n_chunks: int, chunk_size: int, workers: int) -> None:
            job.n_chunks = n_chunks
            job.chunk_size = chunk_size
            job.workers = workers
            job.blocks = [0] * n_chunks
            # 分块方案已定 = 各条连接就位、马上开始搬字节
            job.phase = PH_TRANSFER
            self._notify(job, throttled=False)
        return cb

    def _on_block(self, job: TransferJob):
        def cb(idx: int, state: int) -> None:
            if 0 <= idx < len(job.blocks):
                job.blocks[idx] = state
            self._notify(job, throttled=(state == ACTIVE))
        return cb

    def _on_delta(self, job: TransferJob):
        lock = threading.Lock()

        def cb(delta: int) -> None:
            with lock:
                job.done += delta
            job._tick_speed()
            self._notify(job, throttled=True)
        return cb

    # ---------- 执行 + 重试 ----------

    def _start(self, job: TransferJob, work: Callable[[bool], None]) -> None:
        with self._lock:
            self.jobs.append(job)
        self._notify(job, throttled=False)

        def run() -> None:
            if job.cancel.is_set():
                job.status, job.phase = CANCELLED, PH_DONE
                job.finished_at = time.time()
                self._notify(job, throttled=False)
                return
            job.status = RUNNING
            job.phase = PH_CONNECT
            job.started_at = time.time()
            self._notify(job, throttled=False)

            last_err = ""
            for attempt in range(self._max_retries + 1):
                if job.cancel.is_set():
                    job.status = CANCELLED
                    break
                job.attempt = attempt
                if attempt:
                    backoff = min(2 ** attempt, 20)
                    job.detail = _("连接中断,{backoff}s 后重试 ({attempt}/{_max_retries})").format(
                        backoff=backoff, attempt=attempt, _max_retries=self._max_retries)
                    self._notify(job, throttled=False)
                    if job.cancel.wait(timeout=backoff):
                        job.status = CANCELLED
                        break
                try:
                    work(attempt > 0)
                    job.status = DONE_S
                    job.phase = PH_DONE
                    if job.total:
                        job.done = job.total
                    break
                except TransferCancelled:
                    job.status = CANCELLED
                    break
                except SmbClientError as e:
                    last_err = str(e)
                    if self._is_retryable(e) and attempt < self._max_retries:
                        continue
                    job.status, job.error = ERROR, last_err
                    break
                except Exception as e:  # noqa: BLE001
                    job.status, job.error = ERROR, f"{type(e).__name__}: {e}"
                    break

            if job.status not in (DONE_S, ERROR):
                job.phase = PH_DONE
            job.finished_at = time.time()
            self._notify(job, throttled=False)

        self._pool.submit(run)

    @staticmethod
    def _is_retryable(e: SmbClientError) -> bool:
        """值不值得退避重试。

        **判结构化标志,不在消息里搜关键词。** 原来是
        ``any(k in str(e) for k in (_("中断"), _("超时"), …))`` —— 在**翻译过的**
        消息里找**翻译过的**关键词。中文下"下载超时"恰好含"超时",换一种语言
        未必(译文里的那个词不一定是错误消息的子串),于是连接错误不再重试:
        不报错,只是下载开始失败。核心库现在直接告诉我们(`SmbClientError.retryable`)。

        剩下那两个 ASCII 关键词是兜底:有些路径的异常没经过 `_run`
        (比如底层库自己抛的 socket 超时文本),它们与语言无关,留着不亏。
        """
        if getattr(e, "retryable", False):
            return True
        s = str(e)
        return any(k in s for k in ("timeout", "reset", "Broken pipe"))

    _NOTIFY_INTERVAL = 0.1

    def _notify(self, job: TransferJob, throttled: bool) -> None:
        if throttled:
            now = time.monotonic()
            last = getattr(job, "_last_notify", 0.0)
            if now - last < self._NOTIFY_INTERVAL:
                return
            job._last_notify = now  # type: ignore[attr-defined]
        try:
            self._on_update(job)
        except Exception:
            pass

    # ---------- 操作 / 统计 ----------

    def cancel_job(self, job_id: int) -> None:
        for job in self.jobs:
            if job.job_id == job_id:
                job.cancel.set()
                return

    def cancel_all(self) -> None:
        for job in self.jobs:
            if not job.finished:
                job.cancel.set()

    def cancel_group(self, group: str) -> None:
        """取消某文件夹组内全部未完成任务(底部精简条组行的取消按钮用)。"""
        for job in self.jobs:
            if job.group == group and not job.finished:
                job.cancel.set()

    def clear_finished(self) -> list[TransferJob]:
        with self._lock:
            gone = [j for j in self.jobs if j.finished]
            self.jobs = [j for j in self.jobs if not j.finished]
        return gone

    def active_count(self) -> int:
        return sum(1 for j in self.jobs if not j.finished)

    def stats(self) -> dict:
        running = [j for j in self.jobs if j.running]
        queued = [j for j in self.jobs if j.status == QUEUED]
        done = [j for j in self.jobs if j.status == DONE_S]
        failed = [j for j in self.jobs if j.status in (ERROR, CANCELLED, SKIPPED)]
        speed = sum(j.speed for j in running)
        done_bytes = sum(j.done for j in self.jobs)
        return {"running": running, "queued": queued, "done": done, "failed": failed,
                "speed": speed, "done_bytes": done_bytes}

    def shutdown(self) -> None:
        for j in self.jobs:
            j.cancel.set()
        self._pool.shutdown(wait=False, cancel_futures=True)
