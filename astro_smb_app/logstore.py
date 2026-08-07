"""日志数据层:从设备 ``EMMC Images/log`` 下载/缓存/解析日志,聚合为夜次视图。

供 拍摄记录页 / 导星分析页 / 运行状态 watcher 三方复用。

线程模型:``refresh()`` 是**阻塞调用**,必须在工作线程执行(调用方自备
``client``——通常是 ``shell.client.clone()``);返回的 ``LogData`` 是纯数据,
UI 层经 ``shell.ui(...)`` 编组后使用。实例内部用锁保护解析缓存,可被多个
工作线程(页面刷新 + watcher)安全并用。

缓存(三层,由快到慢):
  1. **内存**:解析结果按 (文件名, 大小, mtime),进程内复用;
  2. **metacache(SQLite,跨进程/重启)**:
     - ``autorunlog/<结构指纹>`` —— Autorun 解析产物**整棵树**(dataclass↔JSON,
       实测往返严格相等);命中即跳过"读盘 + 解析";
     - ``phd2sum`` —— PHD2 **段摘要**(段头元数据 + 每段 RMS/帧数/时长,
       **不含逐帧数组**),供不需要逐帧的消费者(记录页导星列/夜次汇总)秒开;
     - ``guidesum`` —— ``guide_summary_for_run`` 的结果(拍摄区间 × 导星帧的
       跨积计算,随日志累积增长最快的那一项);
  3. **磁盘原文**:%LOCALAPPDATA%/AstroSmbTool/logs/(日志写盘后不再变化,
     按文件名 + 大小判同)。

**注意**:PHD2 逐帧数组不进库(导星页要逐帧,只能现场 parse;而 26k 帧进
SQLite 既大又慢)。所以 ``refresh()`` 仍会全量 parse PHD2;想要"只看摘要、
秒出首屏"的页面请改用 ``LogStore.summaries()``。
"""
from __future__ import annotations

import hashlib
import json
import os
import statistics
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from astro_smb_app import metacache
from astro_smb import astro
from astro_smb import paths
from astro_smb.autorunlog import (
    AutorunLog, Night, TargetRun, aggregate_nights, parse_autorun_log,
)
from astro_smb.client import AstroSmbClient, RemoteEntry, SmbClientError
from astro_smb.phd2log import (
    GuideSection, Phd2Log, RmsStats, compute_rms, frames_for_interval,
    guide_coverage, parse_phd2_log, section_rms,
)

LOG_SHARE = "EMMC Images"
LOG_DIR = "log"

# metacache 数据种类。autorun 那条把 dataclass **结构指纹**拼进 kind ——
# autorunlog.py 的字段一改,旧 payload 自动整体未命中(不会喂进缺字段的对象)。
#
# ⚠ 指纹只认字段,**认不出解析逻辑的改动**:改了 parse_autorun_log /
# parse_phd2_log / _section_summary / guide_summary_for_run 的**行为**
# (字段没变但算出来的值变了)时,必须手动把下面对应的版本号 +1,
# 否则用户机上的旧缓存会继续命中,新逻辑白改。
AUTORUN_CACHE_VER = 1
PHD2SUM_CACHE_VER = 1
GUIDESUM_CACHE_VER = 1

AUTORUN_KIND = (f"autorunlog/{AUTORUN_CACHE_VER}/"
                + metacache.dc_schema_sig(AutorunLog))
PHD2SUM_KIND = f"phd2sum/{PHD2SUM_CACHE_VER}"
GUIDESUM_KIND = f"guidesum/{GUIDESUM_CACHE_VER}"

# 单条 Autorun payload 上限:异常大的日志不进库(库是元数据缓存,不是文件仓库)
AUTORUN_PAYLOAD_MAX = 2 << 20
# 导星摘要是"内容寻址"的(key 里含拍摄区间 + 导星日志指纹),不会读到脏数据;
# TTL 只用来兜底控制库体积,过期重算约 1ms
GUIDESUM_TTL = 60 * 24 * 3600.0


def _cache_get(kind: str, backend: str, key: str, **kw) -> dict | None:
    """缓存永远是**可选**的:metacache 自身已吞异常,这里再兜一层,保证
    "缓存层整体不可用"(库锁死/被换掉/未来重构出岔)时日志功能照常。"""
    try:
        return metacache.get(kind, backend, key, **kw)
    except Exception:
        return None


def _cache_put(kind: str, backend: str, key: str, payload: dict, **kw) -> None:
    try:
        metacache.put(kind, backend, key, payload, **kw)
    except Exception:
        pass


def _host_slug(host: str) -> str:
    """把设备标识变成安全的目录名(``192.0.2.228`` / ``E:\\`` 都要能用)。"""
    s = "".join(c if (c.isalnum() or c in "._-") else "_" for c in (host or ""))
    return s.strip("._-") or "default"


def logs_cache_dir(host: str = "") -> Path:
    """日志原文缓存目录。**按设备分子目录** —— 缓存只按文件名+大小判同,
    而不同设备上完全可能存在同名日志(尤其 ASIAIR 内置 EMMC 与它导出到
    U 盘/TF 卡的副本),不隔离就会互相串。``host`` 为空时回落到旧的扁平目录。
    """
    base = paths.cache_root() / "logs"
    if host:
        base = base / _host_slug(host)
    base.mkdir(parents=True, exist_ok=True)
    return base


def _site_cfg_path() -> Path:
    base = paths.data_dir()
    base.mkdir(parents=True, exist_ok=True)
    return base / "site.json"


def load_site() -> dict:
    """站点配置 {lat, lon, lon_auto};lat 默认 30.0(用户可改),lon_auto=True
    表示经度采用日志推算值。"""
    try:
        with open(_site_cfg_path(), encoding="utf-8") as fh:
            d = json.load(fh)
        return {"lat": float(d.get("lat", 30.0)),
                "lon": float(d.get("lon", 120.0)),
                "lon_auto": bool(d.get("lon_auto", True))}
    except Exception:
        return {"lat": 30.0, "lon": 120.0, "lon_auto": True}


def save_site(lat: float, lon: float, lon_auto: bool) -> None:
    try:
        with open(_site_cfg_path(), "w", encoding="utf-8") as fh:
            json.dump({"lat": lat, "lon": lon, "lon_auto": lon_auto}, fh)
    except Exception:
        pass


@dataclass
class LogData:
    """一次 refresh 的完整结果(纯数据,跨线程传递安全)。"""
    nights: list[Night] = field(default_factory=list)
    phd2_logs: list[Phd2Log] = field(default_factory=list)
    autorun_logs: list[AutorunLog] = field(default_factory=list)
    log_entries: list[RemoteEntry] = field(default_factory=list)
    lon_estimate: float | None = None       # 日志推算的站点东经(度)
    lon_samples: int = 0
    errors: list[str] = field(default_factory=list)
    # 导星段摘要(不含逐帧;与 phd2_logs 同源,给只要摘要的消费者用)
    phd2_sections: list[dict] = field(default_factory=list)

    def latest_log_mtime(self) -> float:
        return max((e.mtime for e in self.log_entries), default=0.0)


@dataclass
class LogSummary:
    """``summaries()`` 的结果:只有摘要,**没有 PHD2 逐帧**。

    全部命中缓存时不读盘不解析,适合"首屏先出摘要 + 后台再补逐帧"的懒加载。
    """
    nights: list[Night] = field(default_factory=list)
    phd2_sections: list[dict] = field(default_factory=list)
    log_entries: list[RemoteEntry] = field(default_factory=list)
    lon_estimate: float | None = None
    lon_samples: int = 0
    errors: list[str] = field(default_factory=list)
    complete: bool = True       # False = 有文件缓存未命中且这次没去解析


def detect_log_share(backend, shares) -> str:
    """哪个共享底下有 ``log/``。

    SMB 设备上是 ``EMMC Images``,**本地磁盘后端是卷标**(单共享模型),
    插卡时共享名就是那张卡的卷标 —— 写死常量的话 `listdir` 每次都失败,
    而失败只在状态栏一闪而过:经度退回兜底、"正在拍摄"横幅永远不出现,
    两样都不报错。

    ``shares`` 可以是共享名字符串,也可以是带 ``.name`` 的对象
    (两套前端各自拿到的形状不同)。
    """
    names = [s if isinstance(s, str) else getattr(s, "name", "")
             for s in (shares or ())]
    names = [n for n in names if n]
    for name in names:
        try:
            if backend.exists(name, LOG_DIR):
                return name
        except Exception:                  # noqa: BLE001 - 探测失败就试下一个
            continue
    return names[0] if names else LOG_SHARE


def host_key(host: str) -> str:
    """设备标识的**比较用形式**。

    存在的理由是一条真机症状:本地磁盘设备上,拍摄记录/导星/详情方位角
    全都用兜底经度,而日志明明解析成功了。探针打出来是这样 ——

        LogStore.host       = 'C:/Users/…/.tmp/device/EMMC Images'   ← 前端传的原始连接串
        LogStore._data_host = 'C:\\Users\\…\\.tmp\\device\\EMMC Images' ← LocalBackend.host(已规范化)

    同一台设备、两种拼法,于是 :attr:`LogStore.data` 的守卫**每次都命中**,
    refresh 出来的数据被自己挡在门外,页面永远看到 ``None``。
    没有任何报错,只是方位角悄悄从 182° 变成 180°(兜底经度 120°E)。

    守卫本身是对的(见 :attr:`LogStore.data`),错的是**拿字符串当身份**。
    这里把两边归一:本地路径统一分隔符、去尾斜杠,Windows 上再折大小写;
    SMB 主机名/IP 只做去空白与小写(DNS 名不区分大小写)。
    """
    h = (host or "").strip()
    if not h:
        return ""
    if "/" in h or "\\" in h:               # 本地路径(含 UNC)
        h = h.replace("/", "\\").rstrip("\\")
        return h.casefold() if os.name == "nt" else h
    return h.casefold()


class LogStore:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        # 整个 refresh 串行化:多个页面 on_connected 同时触发时,后来者等待,
        # 然后命中前者刚写好的磁盘/解析缓存(否则两个线程会同时下载同一日志,
        # os.replace 同一 .part 路径撞 WinError 32,真机已踩过)
        self._refresh_lock = threading.Lock()
        # 文件名 → (size, mtime, 解析结果)
        self._autorun_cache: dict[str, tuple[int, float, AutorunLog]] = {}
        self._phd2_cache: dict[str, tuple[int, float, Phd2Log]] = {}
        self._data: LogData | None = None   # 最近一次成功 refresh 的结果
        self._data_host = ""                # 那份结果**属于哪台设备**
        # 日志所在共享:SMB 设备是 "EMMC Images",**本地磁盘后端是卷标**
        # (LocalBackend 的单共享模型),由 shell 连接成功后探测并注入
        self.share: str = LOG_SHARE
        self.host: str = ""                 # 当前绑定的设备(bind() 维护)
        self._epoch = 0                     # 失效代次:invalidate() 后在途 refresh
                                            # 的旧结果不再写回共享缓存

    # ---------- 设备绑定(全程序唯一的"当前数据源"切换点)----------

    @property
    def data(self) -> LogData | None:
        """最近一次 refresh 的结果 —— **但只在它属于当前设备时才交出去**。

        这是结构性保证,不是补丁:曾经换设备后拍摄记录/导星/3D 天球三页都在
        显示上一台设备的日志(真机确认),根因是 shell 只换了 client 却没动
        数据层,而三页都按 ``store.data is None`` 分支决定要不要重新拉取。
        把宿主写进数据、取用时比对,"静默 serve 跨设备陈旧数据"就不可能发生
        —— 哪怕将来有人忘了调 bind()。
        """
        if (self._data is not None
                and host_key(self._data_host) != host_key(self.host)):
            return None
        return self._data

    @data.setter
    def data(self, value: LogData | None) -> None:
        self._data = value
        self._data_host = self.host

    def bind(self, host: str, share: str = "") -> bool:
        """切到某台设备。host 变了就把**所有设备相关状态**清干净。

        返回是否真的换了设备(没换时只更新 share,不清缓存 —— 重连同一台
        设备没必要把已解析的日志全丢掉重来)。
        """
        with self._lock:
            if share:
                self.share = share
            # 同一台设备换个拼法(正斜杠/反斜杠/大小写)不算换设备 ——
            # 当成换设备会把已解析的日志全丢掉重来。
            if host_key(host) == host_key(self.host):
                self.host = host or self.host
                return False
            self.host = host
            self._epoch += 1
            self._data = None
            self._data_host = host
            # 这两个内存缓存**只按文件名做键**,没有设备维度 —— 换设备必须清空,
            # 否则两台设备上同名的 Autorun_Log_xxx.txt 会互相串
            self._autorun_cache.clear()
            self._phd2_cache.clear()
            return True

    def invalidate(self) -> None:
        """标记缓存失效(如 watcher 发现新日志):清 data 并递增代次,
        使正在进行的旧 refresh 结果不会覆盖失效标记。"""
        with self._lock:
            self._epoch += 1
            self._data = None

    # ---------- 主入口 ----------

    def refresh(self, client: AstroSmbClient,
                cancel: threading.Event | None = None) -> LogData:
        """列出 log 目录 → 下载缺失文件 → 解析 → 聚合。阻塞,在工作线程调用。

        目录列举失败会抛 SmbClientError;单个文件的下载/解析失败进 errors,
        不影响其余文件。
        """
        with self._refresh_lock:
            return self._refresh_locked(client, cancel)

    def _refresh_locked(self, client: AstroSmbClient,
                        cancel: threading.Event | None) -> LogData:
        with self._lock:
            epoch0 = self._epoch
        files, autorun_files, phd2_files = self._list_logs(client)
        data = LogData(log_entries=files)

        for e in autorun_files:
            if cancel is not None and cancel.is_set():
                break
            try:
                log = self._autorun(client, e, cancel)
                if log is not None:
                    data.autorun_logs.append(log)
            except SmbClientError as ex:
                data.errors.append(f"{e.name}: {ex}")

        for e in phd2_files:
            if cancel is not None and cancel.is_set():
                break
            try:
                parsed = self._phd2(client, e, cancel)
                data.phd2_logs.append(parsed)
                data.phd2_sections.extend(
                    self._phd2_sections(client, e, cancel, parsed))
            except SmbClientError as ex:
                data.errors.append(f"{e.name}: {ex}")

        data.phd2_logs.sort(key=lambda l: l.enabled_at or datetime.min)
        data.phd2_sections.sort(key=lambda s: s.get("begins") or "")
        data.nights = aggregate_nights(data.autorun_logs)
        data.lon_estimate, data.lon_samples = _estimate_longitude(
            data.nights, data.phd2_sections)
        with self._lock:
            if self._epoch == epoch0:   # 期间未被 invalidate 才写回共享缓存
                self._data = data
                # 按**产出这份数据的 client** 打标,而不是"当前绑定的设备":
                # 在途 refresh 完成时绑定可能已经换了,盖当前 host 等于给旧设备
                # 的数据发了张新设备的通行证,property 的守卫就白设了。
                self._data_host = getattr(client, "host", "") or self.host
        return data

    # ---------- 只要摘要的轻量入口(懒加载首屏用) ----------

    def summaries(self, client: AstroSmbClient,
                  cancel: threading.Event | None = None,
                  *, parse_missing: bool = True) -> LogSummary:
        """夜次 + 导星**段摘要**(不含逐帧)。阻塞,在工作线程调用。

        全部命中 metacache 时既不读盘也不解析 —— 用于"首屏秒出摘要,逐帧
        等用户真进导星页再补"。``parse_missing=False`` 则严格只用缓存,
        没命中的文件跳过并把 ``complete`` 置 False(可先渲染再后台补齐)。
        """
        with self._refresh_lock:
            files, autorun_files, phd2_files = self._list_logs(client)
            out = LogSummary(log_entries=files)
            logs: list[AutorunLog] = []
            for e in autorun_files:
                if cancel is not None and cancel.is_set():
                    break
                try:
                    log = self._autorun(client, e, cancel,
                                        parse_missing=parse_missing)
                    if log is None:
                        out.complete = False
                    else:
                        logs.append(log)
                except SmbClientError as ex:
                    out.errors.append(f"{e.name}: {ex}")
            for e in phd2_files:
                if cancel is not None and cancel.is_set():
                    break
                try:
                    secs = self._phd2_sections(client, e, cancel,
                                               parse_missing=parse_missing)
                    if secs is None:
                        out.complete = False
                    else:
                        out.phd2_sections.extend(secs)
                except SmbClientError as ex:
                    out.errors.append(f"{e.name}: {ex}")
            out.phd2_sections.sort(key=lambda s: s.get("begins") or "")
            out.nights = aggregate_nights(logs)
            out.lon_estimate, out.lon_samples = _estimate_longitude(
                out.nights, out.phd2_sections)
            return out

    # ---------- 逐文件:内存缓存 → metacache → 读盘解析 ----------

    def _list_logs(self, client: AstroSmbClient
                   ) -> tuple[list[RemoteEntry], list[RemoteEntry], list[RemoteEntry]]:
        entries = client.listdir(self.share or LOG_SHARE, LOG_DIR)
        files = [e for e in entries if not e.is_dir]
        autorun = [e for e in files
                   if e.name.startswith("Autorun_Log_")
                   and e.name.lower().endswith(".txt")
                   and "_CHN" not in e.name]
        phd2 = [e for e in files
                if e.name.startswith("PHD2_GuideLog_")
                and e.name.lower().endswith(".txt")]
        return files, autorun, phd2

    @staticmethod
    def _backend(client: AstroSmbClient) -> str:
        return getattr(client, "host", "") or ""

    def _autorun(self, client: AstroSmbClient, e: RemoteEntry,
                 cancel: threading.Event | None,
                 parse_missing: bool = True) -> AutorunLog | None:
        """Autorun 日志解析产物。**先查缓存再读盘** —— 老代码是先无条件
        `_fetch_text` 再查缓存,等于每次 refresh 都白读几 MB 日志原文。"""
        with self._lock:
            hit = self._autorun_cache.get(e.name)
            if hit and hit[0] == e.size and hit[1] == e.mtime:
                return hit[2]
        payload = _cache_get(AUTORUN_KIND, self._backend(client), e.name,
                             src_size=e.size, src_mtime=e.mtime)
        if payload is not None:
            try:
                log = metacache.dc_decode(AutorunLog, payload)
            except Exception:
                log = None      # payload 坏了就当没缓存,重新解析覆盖掉
            if isinstance(log, AutorunLog):
                with self._lock:
                    self._autorun_cache[e.name] = (e.size, e.mtime, log)
                return log
        if not parse_missing:
            return None
        text = self._fetch_text(client, e, cancel)
        log = parse_autorun_log(text, source=e.name)
        with self._lock:
            self._autorun_cache[e.name] = (e.size, e.mtime, log)
        try:
            enc = metacache.dc_encode(log)
            if len(json.dumps(enc, ensure_ascii=False)) <= AUTORUN_PAYLOAD_MAX:
                _cache_put(AUTORUN_KIND, self._backend(client), e.name, enc,
                           src_size=e.size, src_mtime=e.mtime)
        except Exception:
            pass                # 缓存只是加速,写不进去不影响功能
        return log

    def _phd2(self, client: AstroSmbClient, e: RemoteEntry,
              cancel: threading.Event | None) -> Phd2Log:
        """PHD2 全量解析(含逐帧)。**逐帧不进 metacache**,只有内存缓存。"""
        with self._lock:
            hit = self._phd2_cache.get(e.name)
            if hit and hit[0] == e.size and hit[1] == e.mtime:
                return hit[2]
        text = self._fetch_text(client, e, cancel)
        parsed = parse_phd2_log(text, source=e.name)
        with self._lock:
            self._phd2_cache[e.name] = (e.size, e.mtime, parsed)
        return parsed

    def _phd2_sections(self, client: AstroSmbClient, e: RemoteEntry,
                       cancel: threading.Event | None,
                       parsed: Phd2Log | None = None,
                       parse_missing: bool = True) -> list[dict] | None:
        """某个 PHD2 文件的段摘要(不含逐帧)。

        **先查缓存再看 parsed**:refresh 每次都传 parsed,若不先查就会每次
        重算 + 重写 125 段摘要(纯浪费)。缓存没有时才用 parsed / 现场解析。
        """
        backend = self._backend(client)
        payload = _cache_get(PHD2SUM_KIND, backend, e.name,
                             src_size=e.size, src_mtime=e.mtime)
        if payload is not None:
            secs = payload.get("sections")
            if isinstance(secs, list):
                return [s for s in secs if isinstance(s, dict)]
        if parsed is None:
            if not parse_missing:
                return None
            parsed = self._phd2(client, e, cancel)
        secs = [_section_summary(sec, parsed.source or e.name)
                for sec in parsed.guide_sections]
        _cache_put(PHD2SUM_KIND, backend, e.name, {"sections": secs},
                   src_size=e.size, src_mtime=e.mtime)
        return secs

    def _fetch_text(self, client: AstroSmbClient, e: RemoteEntry,
                    cancel: threading.Event | None) -> str:
        """带本地磁盘缓存的日志原文获取(日志写盘后不再变化,按大小校验)。"""
        local = logs_cache_dir(self.host) / e.name
        try:
            if (not metacache.bypass_reads()          # ASTRO_SMB_GUI_NOCACHE=1
                    and local.is_file() and local.stat().st_size == e.size):
                return local.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            pass
        part = local.with_suffix(local.suffix + ".part")
        try:
            client.download_file(e.share, e.path, part, cancel=cancel)
            os.replace(part, local)
        except BaseException:
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return local.read_text(encoding="utf-8-sig", errors="replace")


# ---------------------------------------------------------------- 段摘要序列化

def _rms_to_dict(st: RmsStats | None) -> dict | None:
    if st is None:
        return None
    return {"rms_ra": st.rms_ra, "rms_dec": st.rms_dec,
            "rms_total": st.rms_total, "peak_ra": st.peak_ra,
            "peak_dec": st.peak_dec, "n_frames": st.n_frames,
            "n_lost": st.n_lost, "duration_s": st.duration_s,
            "pixel_scale": st.pixel_scale}


def _rms_from_dict(d) -> RmsStats | None:
    if not isinstance(d, dict):
        return None
    try:
        return RmsStats(
            rms_ra=float(d["rms_ra"]), rms_dec=float(d["rms_dec"]),
            rms_total=float(d["rms_total"]), peak_ra=float(d["peak_ra"]),
            peak_dec=float(d["peak_dec"]), n_frames=int(d["n_frames"]),
            n_lost=int(d["n_lost"]), duration_s=float(d["duration_s"]),
            pixel_scale=(None if d.get("pixel_scale") is None
                         else float(d["pixel_scale"])))
    except Exception:
        return None


def _section_summary(sec: GuideSection, source: str) -> dict:
    """单个导星段的摘要(**不含逐帧**),可 JSON 化。"""
    kinds = [s.kind for s in sec.settles]
    return {
        "source": source,
        "begins": sec.begins.isoformat(),
        "ends": sec.ends.isoformat() if sec.ends else None,
        "end_eff": sec.end_time_effective.isoformat(),
        "pixel_scale": sec.pixel_scale,
        "binning": sec.binning,
        "focal_len": sec.focal_len,
        "exposure_ms": sec.exposure_ms,
        "camera": sec.camera,
        "mount": sec.mount,
        "dec_deg": sec.dec_deg,
        "hour_angle_hr": sec.hour_angle_hr,
        "pier_side": sec.pier_side,
        "n_frames": len(sec.frames),
        "n_lost": sum(1 for f in sec.frames if f.lost),
        "duration_s": sec.duration_s,
        "settle_failed": kinds.count("failed"),
        "settle_complete": kinds.count("complete"),
        "rms": _rms_to_dict(section_rms(sec)),
    }


def section_begins(sec: dict) -> datetime | None:
    """段摘要里的开始时刻(解析失败返回 None)。"""
    try:
        return datetime.fromisoformat(sec["begins"])
    except Exception:
        return None


def section_rms_stats(sec: dict) -> RmsStats | None:
    return _rms_from_dict(sec.get("rms"))


# ---------------------------------------------------------------- 站点/导星摘要

def _estimate_longitude(nights: list[Night],
                        sections: list[dict]) -> tuple[float | None, int]:
    """经度推算:PHD2 段头时角 + 同时刻拍摄目标 RA → LST → 经度,取中位数。

    只用**段摘要**(begins/hour_angle_hr/n_frames),不需要逐帧 —— 这样
    ``summaries()`` 走纯缓存路径也能给出经度。
    """
    samples: list[float] = []
    runs = [r for n in nights for r in n.runs]
    for sec in sections:
        ha = sec.get("hour_angle_hr")
        if ha is None or not sec.get("n_frames"):
            continue
        begins = section_begins(sec)
        if begins is None:
            continue
        run = _run_covering(runs, begins)
        if run is None:
            continue
        ra = astro.ra_str_to_deg(run.ra)
        if ra is None:
            continue
        samples.append(astro.estimate_longitude(ra, float(ha), begins))
    if not samples:
        return None, 0
    return statistics.median(samples), len(samples)


def _run_covering(runs: list[TargetRun], when: datetime) -> TargetRun | None:
    """找导星段开始时刻正处于哪个目标的拍摄时段内。

    先按**块级**区间匹配(Pause 分裂的 run 整段区间可能横跨其他目标的块,
    块级才不会配错 RA);找不到再退回 run 整段区间兜底(导星可先于首帧启动)。
    """
    for r in runs:
        for b in r.blocks:
            if b.begin_time <= when <= (b.end_time or b.begin_time):
                return r
    for r in runs:
        end = r.end_time
        if end is not None and r.begin_time <= when <= end:
            return r
    return None


def _phd2_fingerprint(phd2_logs: list[Phd2Log]) -> str:
    """导星日志集合的内容指纹(文件名 + 每段起点/帧数)。O(段数),不碰逐帧。"""
    parts = []
    for log in phd2_logs:
        parts.append(log.source or "")
        for sec in log.guide_sections:
            parts.append(f"{sec.begins.isoformat()}:{len(sec.frames)}"
                         f":{sec.pixel_scale}")
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def guide_summary_for_run(run: TargetRun, phd2_logs: list[Phd2Log], *,
                          use_cache: bool = True) -> tuple[RmsStats | None, float]:
    """目标 run 的拍摄区间与导星日志求交 → (RMS 统计, 覆盖率 0~1)。

    **按块区间的并集**统计而非 run 整段 frame_span:Pause 分裂的 run 整段
    区间会横跨其他目标的块,整段求交会把别的目标时段的导星帧灌进来
    (夜次汇总随之重复计数,审查实证)。块与块之间设备只做一件事,
    并集天然无重叠。

    结果进 metacache(kind=``guidesum``):这是随日志累积增长最快的一项
    ——每个 run 都要扫全部导星段并把落在区间内的帧拉出来求 RMS。
    key **内容寻址**(拍摄区间 + 导星日志指纹),日志一变 key 就变,
    不存在读到脏数据的可能。**在工作线程调用**(含 sqlite I/O)。
    """
    spans = []
    for b in run.blocks:
        frames = b.all_frames()
        if frames:
            spans.append((frames[0].time, frames[-1].end_time))
    if not spans:
        return None, 0.0

    key = None
    if use_cache:
        try:
            raw = "|".join(f"{t0.isoformat()}~{t1.isoformat()}" for t0, t1 in spans)
            key = (hashlib.sha1(raw.encode("utf-8")).hexdigest()
                   + "@" + _phd2_fingerprint(phd2_logs))
            hit = _cache_get(GUIDESUM_KIND, "", key, ttl=GUIDESUM_TTL)
            if hit is not None and "cov" in hit:
                rms = _rms_from_dict(hit.get("rms"))
                if rms is not None or hit.get("rms") is None:
                    return rms, float(hit.get("cov") or 0.0)
        except Exception:
            key = None

    pairs = []
    cov_num = cov_den = 0.0
    for t0, t1 in spans:
        pairs.extend(frames_for_interval(phd2_logs, t0, t1))
        d = (t1 - t0).total_seconds()
        if d > 0:
            cov_num += guide_coverage(phd2_logs, t0, t1) * d
            cov_den += d
    stats = compute_rms(pairs)
    cov = cov_num / cov_den if cov_den > 0 else 0.0
    if key is not None:
        _cache_put(GUIDESUM_KIND, "", key,
                   {"rms": _rms_to_dict(stats), "cov": cov})
    return stats, cov


# ---------------------------------------------------------------- 首帧 FITS 头
#: 亮场目录:``<share>/Plan/Light/<目标>``
PLAN_LIGHT_DIR = "Plan\\Light"
#: 连续这么多次 SMB 失败就放弃本轮 —— 多半是连接问题,继续试只是浪费时间
_FITS_FAIL_GIVE_UP = 3


def collect_fits_map(client, nights, share: str = "",
                     cache: dict | None = None) -> dict[int, dict]:
    """每个目标取**首张亮场**的 FITS 头 → ``{id(run): info}``。

    这一份原来只长在老 UI 里(``_records._collect_fits``),另外两套前端
    统统传 ``{}`` 进去。后果不是报错而是**四处同时少东西**,而且看不出
    是同一件事:

    * 夜次统计右列少「设备: ASI2600MC Pro · OnStep · 403mm」一整行;
    * 目标详情少「实测坐标(FITS)」一行 —— 那是比日志 goto 值准得多的实际指向;
    * 徽章少一枚「滤镜」;
    * 像元比例/采样判读拿不到 XPIXSZ 与焦距。

    共享层 ``_night_summary`` / ``_run_detail`` 一直**收**这个 map,只是没人喂。

    开销控制照老 UI 的口径:每个目标只 ``listdir`` 一次(同目标跨夜复用首帧),
    头按 ``(share, path, size, mtime)`` 缓存在调用方给的 ``cache`` 里,
    单个失败静默跳过,连续 3 次失败视为连接问题、放弃本轮。

    **必须在工作线程调用。**
    """
    from astro_smb_app.preview import read_fits_header
    from astro_smb_app.views.records import _fits_info

    share = share or LOG_SHARE
    cache = cache if cache is not None else {}
    out: dict[int, dict] = {}
    first_fit: dict[str, object] = {}      # 目标名 → 首个 .fit(None = 找不到)
    fails = 0
    for night in nights or ():
        for run in getattr(night, "runs", ()) or ():
            if fails >= _FITS_FAIL_GIVE_UP:
                return out
            target = getattr(run, "target", "") or ""
            if not target:
                continue
            if target not in first_fit:
                try:
                    entries = client.listdir(share,
                                             PLAN_LIGHT_DIR + "\\" + target)
                    first_fit[target] = next(
                        (e for e in entries if not e.is_dir and e.name.lower()
                         .endswith((".fit", ".fits", ".fts"))), None)
                    fails = 0
                except Exception:          # noqa: BLE001 - 目标目录不在很正常
                    first_fit[target] = None
                    fails += 1
                    continue
            ent = first_fit[target]
            if ent is None:
                continue
            key = (ent.share, ent.path, ent.size, ent.mtime)
            info = cache.get(key)
            if info is None:
                try:
                    info = _fits_info(read_fits_header(client, ent))
                except Exception:          # noqa: BLE001
                    fails += 1
                    continue
                fails = 0
                cache[key] = info
            if info:
                out[id(run)] = info
    return out
