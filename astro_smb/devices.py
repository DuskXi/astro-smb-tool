"""设备记录:记住连接成功过的设备,供顶部设备下拉框与启动流程使用。

**纯数据层**:只依赖标准库(无 WinRT、无 impacket),离线单测可直接 import。

落盘 ``%LOCALAPPDATA%/AstroSmbTool/devices.json``::

    [{"host": "192.0.2.225", "kind": "smb", "path": "",
      "name": "ASIAIR", "os": "...", "dialect": "SMB 3.1.1",
      "shares": 3, "first_seen": 1753500000.0, "last_ok": 1753600000.0},
     {"host": "E:\\\\", "kind": "local", "path": "E:\\\\",
      "name": "ASIAIR", "dialect": "本地磁盘", "shares": 1, ...}]

设备有两种 :data:`kind`:``"smb"``(网络上的 ASIAIR 盒子)与 ``"local"``
(卡直接插在电脑上,见 :mod:`astro_smb_app.volumes`)。``host`` 始终是**唯一键**:
SMB 用 IP/主机名,local 用根路径字符串(与 ``LocalBackend.host`` 一致),
``path`` 则冗余保存 local 的根路径便于直接构造后端。

**向后兼容**:老 devices.json 没有 ``kind``/``path`` 字段,一律按 ``"smb"`` 处理
(:func:`_normalize` 兜底),不需要迁移。

时间统一用 unix 时间戳(float)。

**``last_ok`` 只在真的连上时才更新**(:func:`remember` 的 ``connected`` 参数):
手动添加/自动发现只是"记下这台设备",没有连过就是 ``last_ok=0``,卡片显示
"从未",也不会夺走下次启动的默认设备(:func:`last_host` 只认 ``last_ok>0``)。
—— 曾经无条件刷 ``last_ok``:把 IP 打错添加一次,下次启动就去连那个错地址。

列表按"最近见过"(``max(last_ok, first_seen)``)倒序,只保留最近
:data:`MAX_RECORDS` 台 —— 下拉框条目数与后台存活探测的上限都靠它兜住。
用 ``first_seen`` 兜底排序是为了让刚添加、还没连过的设备不被立刻挤掉。

**host 是唯一键,但比较要规范化**(:func:`host_key`):本地路径
``E:\\`` 与 ``e:/`` 是同一张卡(Windows 盘符不分大小写),SMB 主机名
不区分大小写(DNS)。按字节精确比较会让同一台设备占两个名额、卡片出现两次。

**所有 IO / JSON 异常一律吞掉**(返回空列表或静默失败):配置文件被写坏、
磁盘只读、LOCALAPPDATA 不可写,都绝不能让 GUI 起不来。写盘走 ``.part`` +
``os.replace`` 原子落盘(与 preview/logstore 的缓存写法一致),避免半截 JSON
永久毒化设备记录。
"""

from __future__ import annotations

import json
import os
import time
from astro_smb import paths
from astro_smb.i18n import gettext as _
from pathlib import Path

MAX_RECORDS = 12        # 最多记住几台设备(下拉展示与后台探测共用这个上限)

# 设备种类
KIND_SMB = "smb"        # 网络上的 ASIAIR(impacket / AstroSmbClient)
KIND_LOCAL = "local"    # 卡插在本机(astro_smb.backend.LocalBackend)
KINDS = (KIND_SMB, KIND_LOCAL)


def devices_path() -> Path:
    """设备记录文件路径(顺带确保目录存在;建目录失败不抛,交给读写兜底)。"""
    base = paths.data_dir()
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return base / "devices.json"


def host_key(host: str, kind: str | None = None) -> str:
    """设备记录 ``host`` 的**比较用键**(大小写/分隔符差异不算两台设备)。

    - 本地路径(``kind == "local"``,或形如 ``E:\\`` / ``/media/card``):
      ``normpath`` 统一分隔符与尾斜杠 + ``normcase``(Windows 上顺带转小写,
      POSIX 上是恒等 —— 那里路径本来就区分大小写);
    - SMB 主机名:``casefold``(DNS 不区分大小写)。

    自动发现写的是大写 ``E:\\``,用户手输 ``e:\\`` —— 不规范化就成了两台设备。
    """
    h = (host or "").strip()
    if not h:
        return ""
    is_local_path = (str(kind or "").strip().lower() == KIND_LOCAL
                     or _looks_local(h))
    if is_local_path:
        return os.path.normcase(os.path.normpath(h))
    return h.casefold()


def same_host(a: str, b: str) -> bool:
    """两个 host 串是不是同一台设备(规范化后比较)。"""
    return bool(a) and bool(b) and host_key(a) == host_key(b)


def load() -> list[dict]:
    """读设备记录,按"最近见过"倒序返回(最近连接/最近添加的在最前)。

    文件不存在/损坏/字段类型不对一律当作"没有记录",返回空列表。
    重复 host(含只差大小写/尾斜杠的写法)只保留排序靠前的那条。
    """
    try:
        with open(devices_path(), encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception:
        return []
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        rec = _normalize(item)
        if rec is None:
            continue        # 脏数据直接丢弃
        out.append(rec)
    out.sort(key=_rank, reverse=True)
    deduped: list[dict] = []
    seen: set[str] = set()
    for rec in out:
        key = host_key(rec["host"], rec["kind"])
        if key in seen:
            continue        # 同一台设备的重复写法:只留排序靠前的那条
        seen.add(key)
        deduped.append(rec)
    return deduped[:MAX_RECORDS]


def remember(host: str, name=None, os=None, dialect=None, shares=None,
             kind=None, path=None, connected: bool = True) -> list[dict]:
    """记住一台设备(已存在则更新),返回更新后的记录列表。

    注:形参 ``os`` 是设备操作系统字符串(与 os 模块同名,故本函数体内不直接
    使用 os 模块——落盘统一走 :func:`_write`)。传 None 的字段保留上次已知值,
    不会被抹成空。

    ``kind`` 取 :data:`KIND_SMB`/:data:`KIND_LOCAL`(不认识的值退回 smb);
    ``path`` 只对本地设备有意义(卡的根路径,如 ``E:\\``)。

    ``connected``(默认 True = 真的连上了)决定要不要刷新 ``last_ok``。
    **手动添加/自动发现必须传 ``connected=False``**:没连过的设备写上
    "刚刚连过"既是假信息,还会抢走下次启动的默认设备。已有记录的
    ``last_ok`` 在 ``connected=False`` 时原样保留,不会被抹掉。
    """
    host = (host or "").strip()
    if not host:
        return load()
    recs = load()
    now = time.time()
    key = host_key(host, kind)
    hit = next((r for r in recs if host_key(r["host"], r["kind"]) == key), None)
    if hit is None:
        hit = {"host": host, "kind": KIND_SMB, "path": "",
               "name": "", "os": "", "dialect": "",
               "shares": None, "first_seen": now,
               "last_ok": now if connected else 0.0}
        recs.append(hit)
    if name:
        hit["name"] = str(name)
    if os:
        hit["os"] = str(os)
    if dialect:
        hit["dialect"] = str(dialect)
    if kind:
        k = str(kind).strip().lower()
        hit["kind"] = k if k in KINDS else KIND_SMB
    if path:
        hit["path"] = str(path)
    if shares is not None:
        try:
            hit["shares"] = int(shares)
        except (TypeError, ValueError):
            pass
    if connected:
        hit["last_ok"] = now
    recs.sort(key=_rank, reverse=True)
    recs = recs[:MAX_RECORDS]
    _write(recs)
    return recs


def forget(host: str) -> list[dict]:
    """从记录里移除一台设备,返回剩余记录(host 按 :func:`host_key` 比较)。"""
    host = (host or "").strip()
    key = host_key(host)
    recs = [r for r in load() if host_key(r["host"], r["kind"]) != key]
    _write(recs)
    return recs


def last_host() -> str | None:
    """最近一次**连接成功**的设备地址;从没连成过返回 None。

    只认 ``last_ok > 0``:手动添加但从没连上的设备不该当启动默认值
    (地址很可能就是打错的那个)。
    """
    rec = last_device()
    return rec["host"] if rec else None


def last_device() -> dict | None:
    """最近一次**连接成功**的设备记录(要判 kind 才能决定建哪种后端时用)。"""
    for rec in load():
        if rec["last_ok"] > 0:
            return rec
    return None


def is_local(rec: dict) -> bool:
    """这条记录是不是"卡插在本机"的本地设备。"""
    return (rec or {}).get("kind") == KIND_LOCAL


def local_root(rec: dict) -> str:
    """本地设备的根路径(``path`` 缺失时退回 ``host`` —— 两者本来就相同)。"""
    if not is_local(rec):
        return ""
    return str((rec.get("path") or rec.get("host") or "")).strip()


def summary(rec: dict) -> str:
    """一台设备的副标题,供下拉项显示。

    - SMB:``服务器名 · 协议 · N 共享``
    - 本地:``本地磁盘 · 卷标``(协议/共享数对本地盘没有信息量,不显示)
    """
    if is_local(rec):
        parts = [_("本地磁盘")]
        if rec.get("name"):
            parts.append(str(rec["name"]))
        return "  ·  ".join(parts)
    parts = [p for p in (rec.get("name"), rec.get("dialect")) if p]
    if rec.get("shares") is not None:
        parts.append(_("{0} 共享").format(rec['shares']))
    return "  ·  ".join(parts)


# ---------------------------------------------------------------- 内部

def _looks_local(host: str) -> bool:
    """这串 host 看起来是不是本地路径(``E:\\`` / ``/media/x``)。

    与 :func:`astro_smb_gui._common.looks_like_local_path` 同判据 —— 这里刻意
    重写一份:本模块是**纯标准库数据层**(离线单测直接 import),不能因为
    一个字符串判断就把 win32more 拖进来。两者一致性由单测钉死。
    """
    h = (host or "").strip()
    if not h:
        return False
    if len(h) >= 2 and h[1] == ":":         # 盘符
        return True
    # 与 _common.looks_like_local_path 逐字一致(含它对 \?\ 的写法),
    # 免得两份判据悄悄漂移 —— 单测 test_local_path_shape_matches_common 钉死。
    return h.startswith("/") or h.startswith("\\?\\")


def _rank(rec: dict) -> float:
    """排序用的"最近见过":连过就用 last_ok,没连过的用 first_seen。

    只按 last_ok 排会把刚手动添加、还没连过的设备直接压到列表底部,
    记录满 12 台时甚至当场被 :data:`MAX_RECORDS` 截掉(加了等于没加)。
    """
    try:
        return max(float(rec.get("last_ok") or 0.0),
                   float(rec.get("first_seen") or 0.0))
    except (TypeError, ValueError):
        return 0.0


def _normalize(item) -> dict | None:
    """把磁盘上的一条记录规整成固定字段的 dict;不可用返回 None。

    **向后兼容**:没有 ``kind`` 字段(旧版写的记录)或值不认识,一律当 smb。
    """
    if not isinstance(item, dict):
        return None
    host = str(item.get("host") or "").strip()
    if not host:
        return None

    def _text(key: str) -> str:
        v = item.get(key)
        return "" if v is None else str(v)

    def _ts(key: str) -> float:
        try:
            return float(item.get(key) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    shares = item.get("shares")
    try:
        shares = None if shares is None else int(shares)
    except (TypeError, ValueError):
        shares = None
    kind = _text("kind").strip().lower()
    if kind not in KINDS:
        kind = KIND_SMB
    path = _text("path")
    if kind == KIND_LOCAL and not path:
        path = host        # 老记录/写坏的记录:host 本身就是根路径
    return {"host": host, "kind": kind, "path": path,
            "name": _text("name"), "os": _text("os"),
            "dialect": _text("dialect"), "shares": shares,
            "first_seen": _ts("first_seen"), "last_ok": _ts("last_ok")}


def _write(records: list[dict]) -> None:
    """原子落盘(先 .part 再 os.replace);任何失败静默丢弃本次写入。"""
    path = devices_path()
    part = path.with_suffix(path.suffix + ".part")
    try:
        with open(part, "w", encoding="utf-8") as fh:
            json.dump(records, fh, ensure_ascii=False, indent=1)
        os.replace(part, path)
    except Exception:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
