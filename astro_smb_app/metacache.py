"""元数据缓存层(SQLite,纯标准库,无 GUI 依赖)。

**只缓存"元数据/派生结果",绝不缓存文件 raw 数据** —— 后者归
`preview.cache_dir()`(缩略图/小图)与 `logstore.logs_cache_dir()`(日志原文)。

设计要点
--------
* 库文件 ``%LOCALAPPDATA%/AstroSmbTool/meta.db``,WAL 模式;单连接
  ``check_same_thread=False`` + **一把 ``threading.Lock`` 串行化所有操作** ——
  预览线程/浏览页懒加载线程/记录页线程/watcher 会并发读写同一个库。
* 通用一张表 ``entries(kind, backend, key, src_size, src_mtime, ts, payload)``,
  payload 存 JSON 文本。``kind`` 是数据种类(如 ``fitshdr``),``backend`` 是
  数据源标识(通常是设备 host —— 换设备天然隔离,不会串味)。
* **缓存更新机制**:
  1. *源指纹* —— 写入时记录源文件的 ``src_size``/``src_mtime``,读取时若调用方
     给出的指纹不一致即视为未命中(文件被覆盖/重传自动失效);
  2. *TTL* —— 读取时可给 ``ttl`` 秒,过期行直接删除并返回未命中;
  3. *显式失效* —— ``invalidate(kind/backend/key)``,任意维度批量清;
  4. *容量* —— ``vacuum_if_large()`` 超阈值时按最旧写入时间淘汰一半再 VACUUM。
* **所有异常一律吞掉**:库损坏/磁盘满/被占用时降级为"没有缓存",绝不能让
  功能挂掉。检测到库损坏会自动删库重建;重建再失败则本进程内永久停用缓存。

线程模型:本模块只做本地 sqlite I/O,**必须在工作线程调用**(UI 线程零磁盘 I/O)。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from dataclasses import fields, is_dataclass
from datetime import datetime
from astro_smb import paths
from astro_smb.i18n import gettext as _
from pathlib import Path
from types import UnionType
from typing import Union, get_args, get_origin, get_type_hints

SCHEMA_VERSION = 1          # 表结构版本;变更后旧库自动整体重建

_CREATE = """
CREATE TABLE IF NOT EXISTS entries(
    kind      TEXT    NOT NULL,
    backend   TEXT    NOT NULL,
    key       TEXT    NOT NULL,
    src_size  INTEGER,
    src_mtime REAL,
    ts        REAL    NOT NULL,
    payload   TEXT    NOT NULL,
    PRIMARY KEY(kind, backend, key)
);
CREATE INDEX IF NOT EXISTS idx_entries_ts ON entries(ts);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""

_MTIME_EPS = 1e-6           # mtime 浮点比较容差(SMB 的 FILETIME 换算量级足够安全)


def _like_prefix(prefix: str) -> str:
    """把任意字符串转成 SQL ``LIKE ... ESCAPE '\\'`` 的前缀模式。

    远程路径本身就带反斜杠,共享名/文件名里也完全可能有 ``_``(LIKE 的单字符
    通配),不转义会误删无关的行。
    """
    out = (prefix.replace("\\", "\\\\")
                 .replace("%", "\\%")
                 .replace("_", "\\_"))
    return out + "%"


def default_db_path() -> Path:
    base = paths.data_dir()
    base.mkdir(parents=True, exist_ok=True)
    return base / "meta.db"


class MetaCache:
    """元数据缓存。实例线程安全;任何异常都不会向外传播。"""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        self._path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._disabled = False      # 重建仍失败 → 本进程内彻底降级为无缓存
        self.hits = 0               # 轻量自检计数(供"缓存命中率"类展示)
        self.misses = 0
        self.writes = 0
        self.errors = 0

    # ------------------------------------------------------------ 连接管理

    def path(self) -> Path:
        if self._path is None:
            self._path = default_db_path()
        return self._path

    def _open(self) -> sqlite3.Connection:
        """真正建连 + 建表;失败会关掉半开的连接并把异常抛出。"""
        p = self.path()
        p.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(p), check_same_thread=False, timeout=5.0)
        try:
            # 自动提交:VACUUM 不能在事务里跑,且我们每条语句本就独立
            conn.isolation_level = None
            conn.execute("PRAGMA journal_mode=WAL")     # 提升并发(读不挡写)
            conn.execute("PRAGMA synchronous=NORMAL")   # 元数据丢一点无所谓
            conn.executescript(_CREATE)
            row = conn.execute("SELECT v FROM meta WHERE k='schema'").fetchone()
            if row is None:
                conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('schema',?)",
                             (str(SCHEMA_VERSION),))
            elif row[0] != str(SCHEMA_VERSION):
                # 结构升级:整体重建(元数据都能重新算出来,不值得写迁移)
                conn.executescript(
                    "DROP TABLE IF EXISTS entries; DROP TABLE IF EXISTS meta;")
                conn.executescript(_CREATE)
                conn.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('schema',?)",
                             (str(SCHEMA_VERSION),))
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            raise
        return conn

    def _wipe(self) -> None:
        """删库文件(含 -wal/-shm)。只在确认损坏时调用。"""
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None
        try:
            p = str(self.path())
        except Exception:
            return
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(p + suffix).unlink(missing_ok=True)
            except OSError:
                pass

    def _connect(self) -> sqlite3.Connection | None:
        """建连(调用方须持锁);检测到库损坏自动删库重建一次,再失败则永久降级。"""
        if self._disabled:
            return None
        if self._conn is not None:
            return self._conn
        try:
            self._conn = self._open()
            return self._conn
        except sqlite3.OperationalError:
            # 锁冲突/权限之类的**暂时性**失败:绝不能因此删掉用户的缓存库
            self.errors += 1
            return None
        except Exception:
            self.errors += 1
        self._wipe()                # 走到这儿基本就是 "file is not a database"
        try:
            self._conn = self._open()
            return self._conn
        except Exception:
            self.errors += 1
            self._disabled = True   # 连重建都失败:老实降级,别再反复试
            return None

    def _rebuild(self) -> sqlite3.Connection | None:
        """操作中途撞上库损坏:删库重建。"""
        self._wipe()
        return self._connect()

    def _exec(self, sql: str, args: tuple = ()) -> list | None:
        """持锁执行一条语句;库损坏自动重建后重试一次。失败返回 None。"""
        with self._lock:
            conn = self._connect()
            if conn is None:
                return None
            try:
                return list(conn.execute(sql, args))
            except sqlite3.OperationalError:
                # 锁冲突/超时/权限之类的**暂时性**失败,绝不是"库坏了"。
                # _connect 早就把它单列了并注释「绝不能因此删掉用户的缓存库」,
                # 四个操作路径却没照做 —— 审查实测:双实例撞锁时 put() 阻塞
                # 10.9s 后确实进了 _rebuild(那次库没被删纯属另一进程还开着
                # 句柄、Windows unlink 失败被吞掉)。降级为"这次没缓存"即可。
                self.errors += 1
                return None
            except sqlite3.DatabaseError:
                self.errors += 1
                conn = self._rebuild()
                if conn is None:
                    return None
                try:
                    return list(conn.execute(sql, args))
                except Exception:
                    self.errors += 1
                    return None
            except Exception:
                self.errors += 1
                return None

    # ------------------------------------------------------------ 公开 API

    def get(self, kind: str, backend: str, key: str, *,
            src_size: int | None = None, src_mtime: float | None = None,
            ttl: float | None = None) -> dict | None:
        """取缓存。源指纹不匹配或超 TTL 一律返回 None(视为未命中)。"""
        try:
            rows = self._exec(
                "SELECT src_size, src_mtime, ts, payload FROM entries "
                "WHERE kind=? AND backend=? AND key=?",
                (kind, backend or "", key))
            if not rows:
                self.misses += 1
                return None
            got_size, got_mtime, ts, payload = rows[0]
            stale = False
            if src_size is not None and got_size != src_size:
                stale = True
            if (src_mtime is not None
                    and (got_mtime is None
                         or abs(float(got_mtime) - float(src_mtime)) > _MTIME_EPS)):
                stale = True
            if ttl is not None and (time.time() - float(ts)) > ttl:
                stale = True
            if stale:
                # 过期/失配的行没有留存价值,顺手删掉,免得库一直长
                self._exec("DELETE FROM entries WHERE kind=? AND backend=? AND key=?",
                           (kind, backend or "", key))
                self.misses += 1
                return None
            data = json.loads(payload)
            if not isinstance(data, dict):
                self.misses += 1
                return None
            self.hits += 1
            return data
        except Exception:
            self.errors += 1
            self.misses += 1
            return None

    def put(self, kind: str, backend: str, key: str, payload: dict, *,
            src_size: int | None = None, src_mtime: float | None = None) -> None:
        """写入/覆盖缓存。payload 必须可 JSON 序列化(dict)。失败静默。"""
        try:
            text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            self.errors += 1
            return
        with self._lock:
            conn = self._connect()
            if conn is None:
                return
            args = (kind, backend or "", key,
                    src_size, src_mtime, time.time(), text)
            sql = ("INSERT OR REPLACE INTO entries"
                   "(kind,backend,key,src_size,src_mtime,ts,payload) "
                   "VALUES(?,?,?,?,?,?,?)")
            try:
                conn.execute(sql, args)
                conn.commit()
                self.writes += 1
            except sqlite3.OperationalError:
                # 锁冲突/超时/权限之类的**暂时性**失败,绝不是"库坏了"。
                # _connect 早就把它单列了并注释「绝不能因此删掉用户的缓存库」,
                # 四个操作路径却没照做 —— 审查实测:双实例撞锁时 put() 阻塞
                # 10.9s 后确实进了 _rebuild(那次库没被删纯属另一进程还开着
                # 句柄、Windows unlink 失败被吞掉)。降级为"这次没缓存"即可。
                self.errors += 1
                return None
            except sqlite3.DatabaseError:
                self.errors += 1
                conn = self._rebuild()
                if conn is None:
                    return
                try:
                    conn.execute(sql, args)
                    conn.commit()
                    self.writes += 1
                except Exception:
                    self.errors += 1
            except Exception:
                self.errors += 1

    def invalidate(self, kind: str | None = None, backend: str | None = None,
                   key: str | None = None) -> int:
        """按任意维度组合失效;三个都不给 = 清空。返回删除行数(失败返回 0)。"""
        where, args = [], []
        if kind is not None:
            where.append("kind=?")
            args.append(kind)
        if backend is not None:
            where.append("backend=?")
            args.append(backend or "")
        if key is not None:
            where.append("key=?")
            args.append(key)
        sql = "DELETE FROM entries"
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self._lock:
            conn = self._connect()
            if conn is None:
                return 0
            try:
                cur = conn.execute(sql, tuple(args))
                conn.commit()
                return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            except sqlite3.OperationalError:
                self.errors += 1      # 暂时性失败:降级,**不删库**(见 _exec 注释)
                return 0
            except sqlite3.DatabaseError:
                self.errors += 1
                self._rebuild()
                return 0
            except Exception:
                self.errors += 1
                return 0

    def invalidate_prefix(self, kind: str, backend: str | None,
                          key_prefix: str) -> int:
        """按 key **前缀**批量失效(同一 kind,可再限定 backend)。返回删除行数。

        给"按共享/按目录子树"整片清缓存用(目录索引层)—— key 是
        ``share|a\\b`` 这种带层级的串,单条 DELETE 覆盖不了一整棵子树。
        ``backend=None`` 表示不限设备。``key_prefix=""`` 等价于该 kind 全清。
        """
        where = ["kind=?"]
        args: list = [kind]
        if backend is not None:
            where.append("backend=?")
            args.append(backend or "")
        where.append("key LIKE ? ESCAPE '\\'")
        args.append(_like_prefix(key_prefix))
        sql = "DELETE FROM entries WHERE " + " AND ".join(where)
        return self._delete(sql, tuple(args))

    def stats(self) -> dict[str, int]:
        """{kind: 条数}(失败返回空表)。"""
        rows = self._exec("SELECT kind, COUNT(*) FROM entries GROUP BY kind")
        if not rows:
            return {}
        try:
            return {str(k): int(n) for k, n in rows}
        except Exception:
            self.errors += 1
            return {}

    def db_size_bytes(self) -> int:
        total = 0
        try:
            p = self.path()
            for suffix in ("", "-wal", "-shm"):
                f = Path(str(p) + suffix)
                if f.is_file():
                    total += f.stat().st_size
        except Exception:
            pass
        return total

    def prune(self, kind: str | None = None, *, max_age_s: float | None = None,
              max_rows: int | None = None) -> int:
        """维护:删掉太旧或超量的行。返回删除行数。"""
        deleted = 0
        try:
            if max_age_s is not None:
                cutoff = time.time() - max_age_s
                sql = "DELETE FROM entries WHERE ts < ?"
                args: tuple = (cutoff,)
                if kind is not None:
                    sql += " AND kind=?"
                    args = (cutoff, kind)
                deleted += self._delete(sql, args)
            if max_rows is not None:
                sub = ("SELECT kind,backend,key FROM entries "
                       + ("WHERE kind=? " if kind is not None else "")
                       + "ORDER BY ts DESC LIMIT -1 OFFSET ?")
                args2 = (kind, max_rows) if kind is not None else (max_rows,)
                sql2 = ("DELETE FROM entries WHERE (kind,backend,key) IN (" + sub + ")")
                deleted += self._delete(sql2, args2)
        except Exception:
            self.errors += 1
        return deleted

    def _delete(self, sql: str, args: tuple) -> int:
        with self._lock:
            conn = self._connect()
            if conn is None:
                return 0
            try:
                cur = conn.execute(sql, args)
                conn.commit()
                return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            except sqlite3.OperationalError:
                self.errors += 1      # 暂时性失败:降级,**不删库**(见 _exec 注释)
                return 0
            except sqlite3.DatabaseError:
                self.errors += 1
                self._rebuild()
                return 0
            except Exception:
                self.errors += 1
                return 0

    def vacuum_if_large(self, max_mb: int = 64) -> None:
        """库超过阈值:按最旧写入时间淘汰一半,再 VACUUM 真正回收空间。"""
        try:
            if self.db_size_bytes() <= max_mb * (1 << 20):
                return
            rows = self._exec("SELECT COUNT(*) FROM entries")
            n = int(rows[0][0]) if rows else 0
            if n > 0:
                self._delete(
                    "DELETE FROM entries WHERE (kind,backend,key) IN ("
                    "SELECT kind,backend,key FROM entries ORDER BY ts ASC LIMIT ?)",
                    (max(1, n // 2),))
            with self._lock:
                conn = self._connect()
                if conn is None:
                    return
                try:
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    conn.execute("VACUUM")
                    conn.commit()
                except Exception:
                    self.errors += 1
        except Exception:
            self.errors += 1

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None


# ------------------------------------------------- dataclass 树 ↔ JSON 通用编解码
#
# 供"把解析产物整棵存进缓存"的场景用(如 Autorun 日志)。**必须配合
# dc_schema_sig() 一起用**:把结构指纹拼进 kind,dataclass 字段一改,
# 旧 payload 自动全部未命中,不会喂给新代码一堆缺字段的对象。

_HINTS: dict[type, dict] = {}
_NONE = type(None)


def _hints(cls: type) -> dict:
    h = _HINTS.get(cls)
    if h is None:
        h = get_type_hints(cls)
        _HINTS[cls] = h
    return h


def dc_encode(obj):
    """dataclass 树 → JSON 可序列化结构(datetime 编成 ISO 字符串)。"""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: dc_encode(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, (list, tuple)):
        return [dc_encode(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): dc_encode(v) for k, v in obj.items()}
    return obj


def dc_decode(tp, val):
    """按类型注解把 dc_encode 的结果还原成 dataclass 树。"""
    origin = get_origin(tp)
    if origin is Union or origin is UnionType:
        if val is None:
            return None
        args = [a for a in get_args(tp) if a is not _NONE]
        return dc_decode(args[0], val) if args else val
    if origin is list or tp is list:
        args = get_args(tp)
        inner = args[0] if args else None
        return [dc_decode(inner, x) if inner is not None else x
                for x in (val or [])]
    if origin is tuple or tp is tuple:
        args = get_args(tp)
        inner = args[0] if args else None
        return tuple(dc_decode(inner, x) if inner is not None else x
                     for x in (val or []))
    if origin is dict or tp is dict:
        args = get_args(tp)
        vt = args[1] if len(args) == 2 else None
        return {k: (dc_decode(vt, v) if vt is not None else v)
                for k, v in (val or {}).items()}
    if tp is datetime:
        return datetime.fromisoformat(val) if isinstance(val, str) else None
    if is_dataclass(tp) and isinstance(tp, type):
        if not isinstance(val, dict):
            raise TypeError(_("{__name__} 期望 dict, 得到 {0}").format(
                type(val).__name__, __name__=tp.__name__))
        hints = _hints(tp)
        kw = {f.name: dc_decode(hints.get(f.name), val[f.name])
              for f in fields(tp) if f.init and f.name in val}
        return tp(**kw)
    if tp is float and isinstance(val, int) and not isinstance(val, bool):
        return float(val)
    return val


def _dc_types(tp, out: set) -> None:
    if is_dataclass(tp) and isinstance(tp, type):
        out.add(tp)
        return
    for a in get_args(tp):
        _dc_types(a, out)


def dc_schema_sig(*roots: type) -> str:
    """dataclass 树的结构指纹(12 位十六进制)。字段名/类型一变指纹就变。"""
    try:
        seen: set = set()
        pending = list(roots)
        while pending:
            tp = pending.pop()
            if not (is_dataclass(tp) and isinstance(tp, type)) or tp in seen:
                continue
            seen.add(tp)
            hints = _hints(tp)
            for f in fields(tp):
                sub: set = set()
                _dc_types(hints.get(f.name), sub)
                pending.extend(sub)
        parts = []
        for tp in sorted(seen, key=lambda t: t.__qualname__):
            hints = _hints(tp)
            parts.append(tp.__qualname__ + "|" + ";".join(
                f"{f.name}={hints.get(f.name)!s}" for f in fields(tp)))
        return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:12]
    except Exception:
        # 拿不到指纹就用一个随进程变化的值 —— 宁可不命中,也不能错命中
        return "nosig%06x" % (int(time.time()) & 0xFFFFFF)


# ---------------------------------------------------------------- 进程级单例

_default: MetaCache | None = None
_default_lock = threading.Lock()


def cache() -> MetaCache:
    """进程级默认实例(懒建,线程安全)。"""
    global _default
    if _default is None:
        with _default_lock:
            if _default is None:
                _default = MetaCache()
    return _default


def bypass_reads() -> bool:
    """``ASTRO_SMB_GUI_NOCACHE=1``:一切读缓存都当未命中(写照旧)。

    用来在**不删除用户缓存**的前提下复现"冷启动/首次使用"的路径 ——
    删缓存目录既麻烦又会破坏真实使用数据(真机调试反馈)。
    每次调用都读环境变量:同一进程里没必要改,但这样测试可以 monkeypatch。
    """
    return os.environ.get("ASTRO_SMB_GUI_NOCACHE", "").strip() not in ("", "0")


def get(kind: str, backend: str, key: str, **kw) -> dict | None:
    if bypass_reads():
        return None
    return cache().get(kind, backend, key, **kw)


def put(kind: str, backend: str, key: str, payload: dict, **kw) -> None:
    cache().put(kind, backend, key, payload, **kw)


def invalidate(kind: str | None = None, backend: str | None = None,
               key: str | None = None) -> int:
    return cache().invalidate(kind, backend, key)


def invalidate_prefix(kind: str, backend: str | None = None,
                      key_prefix: str = "") -> int:
    return cache().invalidate_prefix(kind, backend, key_prefix)


def stats() -> dict[str, int]:
    return cache().stats()


def vacuum_if_large(max_mb: int = 64) -> None:
    cache().vacuum_if_large(max_mb)


def close() -> None:
    global _default
    with _default_lock:
        if _default is not None:
            _default.close()


def use_path(path: str | os.PathLike[str]) -> MetaCache:
    """把默认实例指向别的库文件(测试用;生产不要调)。"""
    global _default
    with _default_lock:
        if _default is not None:
            _default.close()
        _default = MetaCache(path)
        return _default
