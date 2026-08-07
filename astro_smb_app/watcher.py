"""运行状态 watcher:轻量轮询设备,判断"正在拍摄"并侦测新日志。

原理(真机实测,见 docs/DEVELOPMENT.md/记忆):
- Autorun 日志是**会话结束时一次性写盘**的 —— 运行中 log 目录看不到任何东西;
  出现新的 Autorun_Log_*.txt = "一段拍摄会话刚刚结束"的信号。
- "正在拍摄"的唯一可靠心跳是**影像目录的最新帧 mtime**:
  Plan/Light/<目标>/ 与 Autorun/{Bias,Dark,Flat}/ 下每完成一帧就落一对新文件
  (_thn.jpg 先落);目录 mtime 同步跳动。判据:
  now - 最新帧 mtime < 曝光时长 + 容差(默认 10 分钟,覆盖换目标/自动对焦间隙)。

线程模型:独立守护线程 + 自建独立 client(绝不与其他线程共享连接);
每轮 3~4 次 listdir(目录都很小)。结果经构造时传入的 on_state 回调上报
(回调在 watcher 线程被调,**由 shell 负责 shell.ui 编组**)。
"""
from __future__ import annotations

import threading
import time

from astro_smb.client import AstroSmbClient, SmbClientError
from astro_smb.naming import parse_image_name
from astro_smb.i18n import gettext as _

SHARE = "EMMC Images"
POLL_SECONDS = 30.0
IDLE_GRACE_S = 600.0        # 曝光结束后仍算"运行中"的容差(换目标+对焦实测 6~7 分钟)


class RunWatcher:
    """on_state(state: dict) 字段:
    running: bool               是否判定正在拍摄
    target: str|None            正在拍的目标(Plan light)或帧类型(校准帧)
    kind: str|None              'light'|'bias'|'dark'|'flat'
    seq: int|None               最新帧序号
    exposure_s: float|None      最新帧曝光
    age_s: float|None           最新帧距今秒数
    new_logs: list[str]         本轮新出现的 Autorun 日志文件名(会话刚结束)
    error: str|None             本轮轮询失败原因(连接类, 下轮自动重试)
    """

    def __init__(self, host_getter, on_state, client_factory=None) -> None:
        self._host_getter = host_getter        # () -> str|None, 取当前设备地址
        self._on_state = on_state
        # (host) -> 后端实例。默认建 SMB;shell 会注入自己的工厂,
        # 这样**本地磁盘设备**(ASIAIR 卡直插电脑)也能被 watcher 轮询
        self._factory = client_factory or (
            lambda host: AstroSmbClient(host=host, timeout=8))
        # 影像/日志所在共享:SMB 是 "EMMC Images",本地磁盘后端是卷标,
        # 由 shell 连接成功后探测注入(见 _window._detect_log_share)
        self.share: str = SHARE
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._known_logs: set[str] | None = None    # None=首轮(不报 new_logs)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="run-watcher")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def poke(self) -> None:
        """立即触发一轮(连接成功后调用,不必等下个周期)。"""
        self._wake.set()

    # ---------- 内部 ----------

    def _loop(self) -> None:
        client: AstroSmbClient | None = None
        cur_host: str | None = None
        while not self._stop.is_set():
            host = self._host_getter()
            if not host:
                self._stop.wait(POLL_SECONDS)
                continue
            if client is None or cur_host != host:
                if cur_host != host:
                    # 仅真正换设备时重置基线;同 host 断线重连沿用旧基线,
                    # 否则停机期间落盘的新日志会被漏报
                    self._known_logs = None
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass
                client = self._factory(host)
                cur_host = host
            state: dict = {"running": False, "target": None, "kind": None,
                           "seq": None, "exposure_s": None, "age_s": None,
                           "new_logs": [], "error": None}
            try:
                self._poll(client, state)
            except SmbClientError as ex:
                state["error"] = str(ex)
                try:
                    client.close()
                except Exception:
                    pass
                client = None       # 下轮重建连接;cur_host 保留以维持日志基线
            except Exception as ex:     # 防御:任何意外不杀线程
                state["error"] = _("watcher 异常: {ex}").format(ex=ex)
            try:
                self._on_state(state)
            except Exception:
                pass
            self._wake.clear()
            # 可被 poke 提前唤醒
            t0 = time.monotonic()
            while (not self._stop.is_set()
                   and time.monotonic() - t0 < POLL_SECONDS):
                if self._wake.wait(0.5):
                    break
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

    def _poll(self, client: AstroSmbClient, state: dict) -> None:
        # 1) 新日志侦测(= 会话刚结束)
        try:
            logs = {e.name for e in client.listdir(self.share, "log")
                    if e.name.startswith("Autorun_Log_")
                    and "_CHN" not in e.name}
            if self._known_logs is not None:
                state["new_logs"] = sorted(logs - self._known_logs)
            self._known_logs = logs
        except SmbClientError:
            pass    # log 目录读不到不影响心跳判定

        # 2) 帧心跳:候选目录 = Plan/Light/<各目标> + Autorun/{Bias,Dark,Flat}
        candidates: list[tuple[str, float]] = []    # (目录路径, 目录 mtime)
        try:
            for e in client.listdir(self.share, "Plan\\Light"):
                if e.is_dir:
                    candidates.append((f"Plan\\Light\\{e.name}", e.mtime))
        except SmbClientError:
            pass
        try:
            for e in client.listdir(self.share, "Autorun"):
                if e.is_dir:
                    candidates.append((f"Autorun\\{e.name}", e.mtime))
        except SmbClientError:
            pass
        if not candidates:
            return
        candidates.sort(key=lambda p: p[1], reverse=True)
        path, dir_mtime = candidates[0]
        age = time.time() - dir_mtime
        if age > IDLE_GRACE_S + 3600:
            state["age_s"] = age    # 回填年龄让状态栏"空闲(N分前)"窗口按设计生效
            return      # 最活跃目录也太老,明显空闲,不必进目录细看

        # 3) 进最活跃目录取最新帧
        entries = [e for e in client.listdir(self.share, path) if not e.is_dir]
        if not entries:
            return
        latest = max(entries, key=lambda e: e.mtime)
        info = parse_image_name(latest.name)
        age = time.time() - latest.mtime
        exposure = info.exposure_s if info else None
        threshold = (exposure or 0.0) + IDLE_GRACE_S
        state.update(
            running=age < threshold,
            target=(info.target if info and info.target
                    else (info.kind if info else None)),
            kind=(info.kind.lower() if info else None),
            seq=(info.seq if info else None),
            exposure_s=exposure,
            age_s=age,
        )
