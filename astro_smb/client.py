"""Astro SMB Tool 核心库(impacket 实现)。

设计要点:
- SMB 2/3(实测 ASIAIR 为 SMB 3.1.1,Samba 4.9.5),匿名空密码登录;
- 共享名可含空格("EMMC Images"),路径全程使用 str(SMB2 线上为 UTF-16,
  中文路径天然支持);
- 大文件用 openFile/readFile/writeFile 分块流式传输,带进度回调、取消、断点续传;
- 连接超时可配,断线自动重连一次后重试;
- 单个实例内部用锁串行化(impacket 连接不是线程安全的),并行传输请用 clone()。
"""

from __future__ import annotations

import fnmatch
import ntpath
import os
import socket
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from impacket import nt_errors, smb
from impacket.nmb import NetBIOSError, NetBIOSTimeout
from impacket.smb3structs import (
    FILE_DIRECTORY_FILE,
    FILE_NON_DIRECTORY_FILE,
    FILE_OPEN,
    FILE_OVERWRITE_IF,
    FILE_READ_DATA,
    FILE_SHARE_READ,
    FILE_SHARE_WRITE,
    FILE_WRITE_DATA,
    SMB2_0_INFO_FILESYSTEM,
    SMB2_FILESYSTEM_FULL_SIZE_INFO,
)
from impacket.smbconnection import SessionError, SMBConnection
from astro_smb.i18n import gettext as _

# 进度回调:(已传输字节, 总字节)
ProgressCallback = Callable[[int, int], None]

_CONN_ERRORS = (
    ConnectionError,
    TimeoutError,
    socket.timeout,
    socket.error,
    NetBIOSError,
    NetBIOSTimeout,
    EOFError,
)

# 会话/树失效类状态码,重连后重试有意义
_RETRYABLE_NTSTATUS = {
    nt_errors.STATUS_NETWORK_NAME_DELETED,
    nt_errors.STATUS_USER_SESSION_DELETED,
    nt_errors.STATUS_CONNECTION_DISCONNECTED,
    nt_errors.STATUS_CONNECTION_RESET,
    nt_errors.STATUS_FILE_CLOSED,
}


class SmbClientError(Exception):
    """对外统一的错误类型,message 已人类可读。

    ``status`` 是底层的 NTSTATUS(拿不到时为 None)。**要判"是哪种错"就判它,
    不要去 message 里找关键词** —— message 是给人看的,会随文案调整、也会被
    i18n 翻掉,而那种判断失效时不报错、只是走错分支。
    ``makedirs`` 就踩过:它靠 ``"已存在" in str(e)`` 来忽略"目录已存在",
    这句话一翻译,建目录就开始报错。
    """

    #: **这次失败值不值得重试。** 传输队列原来是拿
    #: ``any(k in str(e) for k in ("中断", "超时", "连接", …))`` 判的 ——
    #: 在**翻译过的**消息里搜关键词。中文下"下载超时"恰好含"超时",
    #: 换一种语言未必:译文里的那个词不一定是错误消息的子串,
    #: 于是**连接错误不再重试**,不报错,只是下载开始失败。
    #: 核心库本来就知道哪些是连接类错误(`_CONN_ERRORS` / `_RETRYABLE_NTSTATUS`),
    #: 只是抛出去时把这个信息丢了 —— 现在带上。
    retryable: bool = False

    def __init__(self, message: str, *, status: int | None = None,
                 retryable: bool = False):
        super().__init__(message)
        self.status = status
        self.retryable = retryable


class TransferCancelled(SmbClientError):
    """传输被用户取消。"""


@dataclass(frozen=True)
class ShareInfo:
    name: str
    type: int
    remark: str

    @property
    def is_disk(self) -> bool:
        # STYPE_DISKTREE == 0,高位是 special/temporary 标志
        return (self.type & 0x0FFFFFFF) == 0

    @property
    def is_hidden(self) -> bool:
        return self.name.endswith("$")


@dataclass(frozen=True)
class VolumeInfo:
    total: int
    free: int

    @property
    def used(self) -> int:
        return max(0, self.total - self.free)

    @property
    def percent(self) -> float:
        return (self.used / self.total * 100) if self.total else 0.0


@dataclass
class DirStat:
    """一个目录的递归统计结果。"""
    total_size: int = 0
    file_count: int = 0
    dir_count: int = 0
    partial: bool = False  # 是否因取消/出错未扫完


@dataclass
class TreeNode:
    """占用树节点(dir_tree 的结果):目录 size = 递归总大小,children 按大小降序。

    partial/error_count 只在根节点有意义:遍历中有目录枚举失败(如连接中断)
    时置位 —— 调用方**不得把 partial 树当完整结果缓存**。
    """
    name: str
    path: str                   # 共享内路径
    is_dir: bool
    size: int = 0
    file_count: int = 0         # 目录=递归文件数, 文件=1
    children: list["TreeNode"] = field(default_factory=list)
    partial: bool = False
    error_count: int = 0


@dataclass(frozen=True)
class RemoteEntry:
    share: str
    path: str  # 共享内路径,反斜杠分隔,根为 ""
    name: str
    is_dir: bool
    size: int
    mtime: float
    ctime: float
    atime: float
    attributes: int

    @property
    def display_path(self) -> str:
        """用于显示/CLI 的 'SHARE/a/b' 形式。"""
        if not self.path:
            return self.share
        return f"{self.share}/{self.path.replace(chr(92), '/')}"

    @property
    def unc_path(self) -> str:
        p = f"\\{self.path}" if self.path else ""
        return f"\\\\{{host}}\\{self.share}{p}"

    def attr_text(self) -> str:
        flags = [
            ("D", 0x10), ("R", 0x01), ("H", 0x02), ("S", 0x04), ("A", 0x20),
        ]
        return "".join(ch if self.attributes & bit else "-" for ch, bit in flags)


def normalize_remote_path(path: str) -> str:
    """把用户输入的 'a/b\\c/' 规范成 impacket 需要的 'a\\b\\c'(根为 "")。"""
    parts = [p for p in path.replace("/", "\\").split("\\") if p not in ("", ".")]
    out: list[str] = []
    for p in parts:
        if p == "..":
            if out:
                out.pop()
        else:
            out.append(p)
    return "\\".join(out)


def split_remote_path(remote: str) -> tuple[str, str]:
    """'EMMC Images/Autorun/Light' -> ('EMMC Images', 'Autorun\\Light')。

    第一段是共享名(可含空格),其余是共享内路径。也接受反斜杠与
    'smb://host/share/path'、'\\\\host\\share\\path' 形式(host 部分被忽略)。
    """
    s = remote.strip()
    if s.startswith("smb://"):
        s = s[len("smb://"):]
        s = s.split("/", 1)[1] if "/" in s else ""
    elif s.startswith("\\\\"):
        s = s[2:].replace("\\", "/")
        s = s.split("/", 1)[1] if "/" in s else ""
    s = s.replace("\\", "/").strip("/")
    if not s:
        raise SmbClientError(_("远程路径不能为空,格式: '共享名/目录/文件'"))
    share, _sep, rest = s.partition("/")
    return share, normalize_remote_path(rest)


def _friendly_session_error(e: SessionError) -> str:
    code = e.getErrorCode()
    mapping = {
        nt_errors.STATUS_OBJECT_NAME_NOT_FOUND: _("路径不存在"),
        nt_errors.STATUS_OBJECT_PATH_NOT_FOUND: _("路径不存在"),
        nt_errors.STATUS_NO_SUCH_FILE: _("文件不存在"),
        nt_errors.STATUS_ACCESS_DENIED: _("访问被拒绝(共享可能只读)"),
        nt_errors.STATUS_BAD_NETWORK_NAME: _("共享名不存在"),
        nt_errors.STATUS_OBJECT_NAME_COLLISION: _("目标已存在"),
        nt_errors.STATUS_SHARING_VIOLATION: _("文件被占用"),
        nt_errors.STATUS_FILE_IS_A_DIRECTORY: _("目标是目录"),
        nt_errors.STATUS_DIRECTORY_NOT_EMPTY: _("目录非空"),
        nt_errors.STATUS_LOGON_FAILURE: _("登录失败(账号/密码被拒绝)"),
    }
    friendly = mapping.get(code)
    detail = str(e).split("(", 1)[0].strip()
    if friendly:
        return f"{friendly} [{detail}]"
    return detail


class AstroSmbClient:
    """面向 ASIAIR 的 SMB 客户端。

    用法::

        with AstroSmbClient("192.0.2.225") as c:
            for s in c.list_shares():
                print(s.name)
            for e in c.listdir("EMMC Images", "Autorun"):
                print(e.name, e.size)
    """

    def __init__(
        self,
        host: str = "192.0.2.225",
        port: int = 445,
        username: str = "",
        password: str = "",
        timeout: float = 15.0,
        chunk_size: int = 1 << 22,  # 4 MiB,实际受协商 MaxRead/WriteSize 限制
    ):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.timeout = timeout
        self.chunk_size = chunk_size
        self._conn: SMBConnection | None = None
        self._trees: dict[str, int] = {}
        self._lock = threading.RLock()
        self._max_read = chunk_size
        self._max_write = chunk_size

    # ---------- 连接管理 ----------

    def connect(self) -> None:
        with self._lock:
            if self._conn is not None:
                return
            try:
                conn = SMBConnection(
                    remoteName=self.host,
                    remoteHost=self.host,
                    sess_port=self.port,
                    timeout=self.timeout,
                )
                conn.login(self.username, self.password)
            except SessionError as e:
                raise SmbClientError(
                    _("登录 {host} 失败: {0}").format(
                        _friendly_session_error(e), host=self.host)
                ) from e
            except _CONN_ERRORS as e:
                raise SmbClientError(_("无法连接 {host}:{port}: {e}").format(
                    host=self.host, port=self.port, e=e)) from e
            except UnicodeError as e:
                # **不是 OSError,所以 `_CONN_ERRORS` 接不住。** 地址一旦不是
                # 合法主机名(比如误把本地目录 `.tmp/device/…` 当地址传进来),
                # socket 会在 IDNA 编码那一步抛 UnicodeError,原样漏出去就是
                # 一句 `'idna' codec can't encode character '\x2e'` —— 用户看了
                # 不知道自己错在哪。核心库对外只抛 SmbClientError(docs/DEVELOPMENT.md §11)。
                raise SmbClientError(
                    _("{host} 不是合法的主机名或 IP —— 本地目录请当作「本地磁盘」设备添加,而不是填在地址栏").format(
                        host=self.host)
                ) from e
            self._conn = conn
            self._trees.clear()
            try:
                caps = conn.getIOCapabilities()
                self._max_read = min(self.chunk_size, caps["MaxReadSize"])
                self._max_write = min(self.chunk_size, caps["MaxWriteSize"])
            except Exception:
                self._max_read = self._max_write = min(self.chunk_size, 1 << 16)

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
                self._trees.clear()

    def clone(self) -> "AstroSmbClient":
        """新建一个同配置、独立连接的客户端(用于并行传输线程)。"""
        return AstroSmbClient(
            host=self.host, port=self.port, username=self.username,
            password=self.password, timeout=self.timeout, chunk_size=self.chunk_size,
        )

    @property
    def connected(self) -> bool:
        return self._conn is not None

    @property
    def dialect_name(self) -> str:
        with self._lock:
            if self._conn is None:
                return _("未连接")
            d = self._conn.getDialect()
            return {
                0x0202: "SMB 2.0.2", 0x0210: "SMB 2.1",
                0x0300: "SMB 3.0", 0x0302: "SMB 3.0.2", 0x0311: "SMB 3.1.1",
            }.get(d, hex(d))

    def server_info(self) -> dict[str, str]:
        with self._lock:
            self._ensure()
            conn = self._conn
            assert conn is not None
            info = {"host": self.host, "dialect": self.dialect_name}
            for key, getter in (
                ("server_name", conn.getServerName),
                ("server_os", conn.getServerOS),
                ("server_domain", conn.getServerDomain),
            ):
                try:
                    info[key] = str(getter())
                except Exception:
                    info[key] = "?"
            return info

    def __enter__(self) -> "AstroSmbClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _ensure(self) -> SMBConnection:
        if self._conn is None:
            self.connect()
        assert self._conn is not None
        return self._conn

    def _reconnect(self) -> None:
        self.close()
        self.connect()

    def reconnect(self) -> None:
        """公开的强制重连(关闭再连);供分块下载 worker 弱网重试用。"""
        with self._lock:
            self._reconnect()

    def echo(self) -> float:
        """SMB2 ECHO 心跳:一次轻量往返,返回 RTT(毫秒)。失败抛 SmbClientError。"""
        def op(conn: SMBConnection):
            t0 = time.monotonic()
            conn._SMBConnection.echo()
            return (time.monotonic() - t0) * 1000.0

        return self._run(op)  # type: ignore[return-value]

    def ping_tcp(self, timeout: float = 1.5) -> float | None:
        """测到设备 445 端口的 TCP 连接 RTT(毫秒);不通返回 None。

        这只反映网络可达性/时延,不代表 SMB 会话存活(用 echo 判存活)。
        """
        t0 = time.monotonic()
        try:
            with socket.create_connection((self.host, self.port), timeout=timeout):
                return (time.monotonic() - t0) * 1000.0
        except OSError:
            return None

    def _run(self, op: Callable[[SMBConnection], object], *, idempotent: bool = True):
        """执行一个操作;连接类错误重连一次后重试。

        无论首次还是重试,所有 impacket/socket 异常都统一转成 SmbClientError,
        保证对外只暴露一种错误类型。

        idempotent=False(rename/delete 等有副作用的操作):连接中断时不自动
        重放——第一次可能已在服务器生效,盲目重试会误报失败或产生副作用;
        改为直接报"可能已生效"让调用方核对。
        """
        with self._lock:
            try:
                return op(self._ensure())
            except SessionError as e:
                if e.getErrorCode() not in _RETRYABLE_NTSTATUS:
                    raise SmbClientError(
                        _friendly_session_error(e),
                        status=e.getErrorCode()) from e
                self._reconnect()
            except _CONN_ERRORS:
                self._reconnect()
            # 走到这里说明发生了可重连的错误,连接已重建
            if not idempotent:
                raise SmbClientError(_("连接中断,操作可能已在服务器生效,请刷新后确认"))
            try:
                return op(self._ensure())
            except SessionError as e:
                raise SmbClientError(
                    _friendly_session_error(e), status=e.getErrorCode(),
                    retryable=e.getErrorCode() in _RETRYABLE_NTSTATUS) from e
            except _CONN_ERRORS as e:
                # 重连之后还是连接类错误 —— 上层(传输队列)可以退避后再试
                raise SmbClientError(_("连接 {host} 中断: {e}").format(
                    host=self.host, e=e), retryable=True) from e

    def _tree(self, conn: SMBConnection, share: str) -> int:
        tid = self._trees.get(share)
        if tid is None:
            tid = conn.connectTree(share)
            self._trees[share] = tid
        return tid

    def _drop_tree(self, share: str) -> None:
        self._trees.pop(share, None)

    # ---------- 枚举 ----------

    def list_shares(self, include_hidden: bool = False) -> list[ShareInfo]:
        def op(conn: SMBConnection):
            out = []
            for s in conn.listShares():
                name = s["shi1_netname"][:-1]
                remark = s["shi1_remark"][:-1] if s["shi1_remark"] else ""
                out.append(ShareInfo(name=name, type=s["shi1_type"], remark=remark))
            return out

        shares: list[ShareInfo] = self._run(op)  # type: ignore[assignment]
        if not include_hidden:
            shares = [s for s in shares if s.is_disk and not s.is_hidden]
        return shares

    def listdir(self, share: str, path: str = "") -> list[RemoteEntry]:
        path = normalize_remote_path(path)
        pattern = f"{path}\\*" if path else "*"

        def op(conn: SMBConnection):
            return conn.listPath(share, pattern)

        try:
            raw = self._run(op)
        except SmbClientError as e:
            raise SmbClientError(_("列目录 {share}/{0} 失败: {e}").format(
                path or '', share=share, e=e)) from e
        entries = []
        for f in raw:  # type: ignore[union-attr]
            name = f.get_longname()
            if name in (".", ".."):
                continue
            entries.append(RemoteEntry(
                share=share,
                path=f"{path}\\{name}" if path else name,
                name=name,
                is_dir=bool(f.is_directory()),
                size=f.get_filesize(),
                mtime=f.get_mtime_epoch(),
                ctime=f.get_ctime_epoch(),
                atime=f.get_atime_epoch(),
                attributes=f.get_attributes(),
            ))
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return entries

    def stat(self, share: str, path: str) -> RemoteEntry:
        path = normalize_remote_path(path)
        if not path:
            return RemoteEntry(share=share, path="", name=share, is_dir=True,
                               size=0, mtime=0, ctime=0, atime=0, attributes=0x10)

        def op(conn: SMBConnection):
            return conn.listPath(share, path)

        raw = self._run(op)
        if not raw:
            raise SmbClientError(_("路径不存在: {share}/{path}").format(share=share, path=path))
        f = raw[0]  # type: ignore[index]
        parent = ntpath.dirname(path)
        name = f.get_longname()
        return RemoteEntry(
            share=share,
            path=f"{parent}\\{name}" if parent else name,
            name=name,
            is_dir=bool(f.is_directory()),
            size=f.get_filesize(),
            mtime=f.get_mtime_epoch(),
            ctime=f.get_ctime_epoch(),
            atime=f.get_atime_epoch(),
            attributes=f.get_attributes(),
        )

    def exists(self, share: str, path: str) -> bool:
        try:
            self.stat(share, path)
            return True
        except SmbClientError:
            return False

    # ---------- 卷容量 ----------

    def volume_info(self, share: str) -> VolumeInfo | None:
        """读取共享所在卷的总量/可用量;设备不支持则返回 None。"""

        def op(conn: SMBConnection):
            tid = self._tree(conn, share)
            fid = conn.openFile(
                tid, "",
                desiredAccess=FILE_READ_DATA,
                shareMode=FILE_SHARE_READ | FILE_SHARE_WRITE,
                creationOption=FILE_DIRECTORY_FILE,
                creationDisposition=FILE_OPEN,
            )
            try:
                raw = conn._SMBConnection.queryInfo(
                    tid, fid,
                    infoType=SMB2_0_INFO_FILESYSTEM,
                    fileInfoClass=SMB2_FILESYSTEM_FULL_SIZE_INFO,
                )
            finally:
                conn.closeFile(tid, fid)
            info = smb.SMBFileFsFullSizeInformation(raw)
            unit = info["SectorsPerAllocationUnit"] * info["BytesPerSector"]
            total = info["TotalAllocationUnits"] * unit
            free = info["ActualAvailableAllocationUnits"] * unit
            return VolumeInfo(total=total, free=free)

        try:
            return self._run(op)  # type: ignore[return-value]
        except SmbClientError:
            self._drop_tree(share)
            return None
        except Exception:
            return None

    # ---------- 统计 ----------

    def count_children(self, share: str, path: str) -> tuple[int, int] | None:
        """返回 (子目录数, 子文件数);仅统计直接子级。

        枚举失败(无权限/瞬时错误)返回 None,以便与"真空目录"区分。
        """
        try:
            entries = self.listdir(share, path)
        except SmbClientError:
            return None
        ndir = sum(1 for e in entries if e.is_dir)
        return (ndir, len(entries) - ndir)

    def dir_stat(
        self,
        share: str,
        path: str = "",
        cancel: threading.Event | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> DirStat:
        """递归统计一个目录:总大小、文件数、目录数。on_progress(文件数, 字节数)。"""
        st = DirStat()
        for _sub, dirs, files in self.walk(share, path, on_error=lambda p, e: None):
            if cancel is not None and cancel.is_set():
                st.partial = True
                return st
            st.dir_count += len(dirs)
            for f in files:
                st.file_count += 1
                st.total_size += f.size
            if on_progress:
                on_progress(st.file_count, st.total_size)
        return st

    def scan_children(
        self,
        share: str,
        path: str = "",
        cancel: threading.Event | None = None,
        on_item: Callable[[RemoteEntry, int, bool], None] | None = None,
    ) -> list[tuple[RemoteEntry, int]]:
        """统计一个目录直接子级的"占用":文件用自身大小,目录用递归大小。

        返回按大小降序的 (entry, size) 列表;用于占用分析与 treemap 逐层下钻。
        on_item(entry, size, is_final) 在每个子项算完时回调(增量刷新 UI)。
        """
        try:
            entries = self.listdir(share, path)
        except SmbClientError as e:
            raise SmbClientError(_("扫描 {share}/{0} 失败: {e}").format(
                path or '', share=share, e=e)) from e
        results: list[tuple[RemoteEntry, int]] = []
        for e in entries:
            if cancel is not None and cancel.is_set():
                break
            if e.is_dir:
                size = self.dir_stat(share, e.path, cancel=cancel).total_size
            else:
                size = e.size
            results.append((e, size))
            if on_item:
                on_item(e, size, False)
        results.sort(key=lambda t: t[1], reverse=True)
        return results

    def dir_tree(
        self,
        share: str,
        path: str = "",
        cancel: threading.Event | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> TreeNode:
        """一次 BFS 遍历构建整棵占用树:所有嵌套目录的递归大小一次算全
        (供 SpaceSniffer 式嵌套 treemap;scan_children 只有单层)。

        on_progress(累计文件数, 累计字节) 每目录一次;cancel 置位抛
        TransferCancelled。单目录枚举失败跳过该子树并计入
        root.error_count/partial(**调用方不得把 partial 树当完整结果缓存**);
        根目录本身枚举失败(一个条目都没拿到)则抛 SmbClientError。
        """
        path = normalize_remote_path(path)
        root = TreeNode(name=ntpath.basename(path) or share,
                        path=path, is_dir=True)
        nodes: dict[str, TreeNode] = {path: root}
        nfiles = nbytes = 0
        errors = [0]
        yielded = [False]

        def on_err(_p: str, _e: Exception) -> None:
            errors[0] += 1

        for dirpath, dirs, files in self.walk(share, path, on_error=on_err,
                                              cancel=cancel):
            yielded[0] = True
            parent = nodes.get(dirpath)
            if parent is None:
                continue
            for d in dirs:
                node = TreeNode(name=d.name, path=d.path, is_dir=True)
                parent.children.append(node)
                nodes[d.path] = node
            for f in files:
                parent.children.append(TreeNode(
                    name=f.name, path=f.path, is_dir=False,
                    size=f.size, file_count=1))
                nfiles += 1
                nbytes += f.size
            if on_progress is not None:
                try:
                    on_progress(nfiles, nbytes)
                except Exception:
                    pass
        if not yielded[0] and errors[0]:
            # 根目录都没枚举成功:是失败不是空目录, 不能返回一棵空树
            raise SmbClientError(_("扫描 {share}/{0} 失败(根目录枚举失败)").format(
                path or '', share=share))
        # 自底向上聚合(按深度从深到浅), 并按大小降序排每层子节点
        def depth(p: str) -> int:
            return 0 if not p else p.count("\\") + 1
        for p in sorted(nodes, key=depth, reverse=True):
            node = nodes[p]
            node.size = sum(c.size for c in node.children)
            node.file_count = sum(c.file_count for c in node.children)
            node.children.sort(key=lambda c: c.size, reverse=True)
        root.error_count = errors[0]
        root.partial = errors[0] > 0
        return root

    # ---------- 遍历 / 搜索 ----------

    def walk(
        self,
        share: str,
        top: str = "",
        max_depth: int | None = None,
        on_error: Callable[[str, Exception], None] | None = None,
        depth_first: bool = False,
        cancel: threading.Event | None = None,
    ) -> Iterator[tuple[str, list[RemoteEntry], list[RemoteEntry]]]:
        """类似 os.walk,yield (当前路径, 子目录, 文件)。

        默认广度优先;depth_first=True 时深度优先(树形显示用)。
        cancel 在**每个目录处理前**检查(含枚举失败的目录)——连接死亡时
        队列里每个目录都要吃一次超时,只在成功 yield 后检查会让「停止」
        长时间无效(审查实证)。
        """
        top = normalize_remote_path(top)
        queue: deque[tuple[str, int]] = deque([(top, 0)])
        while queue:
            if cancel is not None and cancel.is_set():
                raise TransferCancelled(_("遍历已取消"))
            path, depth = queue.popleft()
            try:
                entries = self.listdir(share, path)
            except SmbClientError as e:
                if on_error:
                    on_error(f"{share}/{path}", e)
                continue
            dirs = [e for e in entries if e.is_dir]
            files = [e for e in entries if not e.is_dir]
            yield path, dirs, files
            if max_depth is None or depth < max_depth:
                children = [(d.path, depth + 1) for d in dirs]
                if depth_first:
                    queue.extendleft(reversed(children))
                else:
                    queue.extend(children)

    def find(
        self,
        share: str,
        top: str = "",
        pattern: str = "*",
        include_dirs: bool = False,
        min_size: int | None = None,
        max_size: int | None = None,
        newer_than: float | None = None,
        max_depth: int | None = None,
        limit: int | None = None,
        cancel: threading.Event | None = None,
        on_error: Callable[[str, Exception], None] | None = None,
    ) -> Iterator[RemoteEntry]:
        """递归搜索。pattern 为 fnmatch 通配(不区分大小写),支持中文文件名。"""
        pat = pattern.lower()
        count = 0
        for _path, dirs, files in self.walk(share, top, max_depth, on_error):
            if cancel is not None and cancel.is_set():
                return
            candidates = files + dirs if include_dirs else files
            for e in candidates:
                if cancel is not None and cancel.is_set():
                    return
                if not fnmatch.fnmatch(e.name.lower(), pat):
                    continue
                if not e.is_dir:
                    if min_size is not None and e.size < min_size:
                        continue
                    if max_size is not None and e.size > max_size:
                        continue
                if newer_than is not None and e.mtime < newer_than:
                    continue
                yield e
                count += 1
                if limit is not None and count >= limit:
                    return

    # ---------- 读取(预览用) ----------

    def read_bytes(self, share: str, path: str, offset: int = 0, size: int = 65536) -> bytes:
        """读取文件的一段字节(部分读取,预览低开销的关键)。"""
        path = normalize_remote_path(path)

        def op(conn: SMBConnection):
            tid = self._tree(conn, share)
            fid = conn.openFile(
                tid, path,
                desiredAccess=FILE_READ_DATA,
                shareMode=FILE_SHARE_READ,
                creationOption=FILE_NON_DIRECTORY_FILE,
                creationDisposition=FILE_OPEN,
            )
            try:
                return conn.readFile(tid, fid, offset, size, singleCall=False)
            finally:
                conn.closeFile(tid, fid)

        try:
            return self._run(op)  # type: ignore[return-value]
        except SmbClientError:
            self._drop_tree(share)
            raise

    def download_range(
        self,
        share: str,
        path: str,
        offset: int,
        length: int,
        fh,
        cancel: threading.Event | None = None,
        on_bytes: Callable[[int], None] | None = None,
    ) -> None:
        """把远程文件的 [offset, offset+length) 写到已打开的本地文件句柄 fh 的
        对应偏移处(供分块并发下载调用;fh 需以 'r+b' 打开、文件已预分配大小)。

        本方法只用本实例这一条连接(每个 worker 各持一个 client 实例);多 worker
        写同一文件的不重叠区间在 Windows 上是安全的。
        """
        path = normalize_remote_path(path)
        end = offset + length
        with self._lock:
            conn = self._ensure()
            try:
                tid = self._tree(conn, share)
                fid = conn.openFile(
                    tid, path,
                    desiredAccess=FILE_READ_DATA,
                    shareMode=FILE_SHARE_READ,
                    creationOption=FILE_NON_DIRECTORY_FILE,
                    creationDisposition=FILE_OPEN,
                )
            except SessionError as e:
                self._drop_tree(share)
                raise SmbClientError(
                    _("打开 {share}/{path} 失败: {0}").format(
                        _friendly_session_error(e), share=share, path=path)) from e
            except _CONN_ERRORS as e:
                self._drop_tree(share)
                self.close()
                raise SmbClientError(_("下载 {share}/{path} 连接中断: {e}").format(
                    share=share, path=path, e=e)) from e
            try:
                fh.seek(offset)
                o = offset
                while o < end:
                    if cancel is not None and cancel.is_set():
                        raise TransferCancelled(_("已取消: {share}/{path}").format(
                            share=share, path=path))
                    data = conn.readFile(tid, fid, o, min(self._max_read, end - o),
                                         singleCall=True)
                    if not data:
                        raise SmbClientError(
                            _("下载 {share}/{path} 区块不完整(远端文件可能已变)").format(
                                share=share, path=path))
                    fh.write(data)
                    o += len(data)
                    if on_bytes:
                        on_bytes(len(data))
            except SessionError as e:
                self._drop_tree(share)
                raise SmbClientError(
                    _("下载 {share}/{path} 失败: {0}").format(
                        _friendly_session_error(e), share=share, path=path)) from e
            except _CONN_ERRORS as e:
                self._drop_tree(share)
                self.close()
                raise SmbClientError(_("下载 {share}/{path} 连接中断: {e}").format(
                    share=share, path=path, e=e)) from e
            finally:
                try:
                    conn.closeFile(tid, fid)
                except Exception:
                    pass

    # ---------- 下载 ----------

    def download_file(
        self,
        share: str,
        path: str,
        local_path: str | Path,
        progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
        resume: bool = False,
    ) -> Path:
        """下载单个文件,分块流式,支持进度/取消/断点续传。"""
        path = normalize_remote_path(path)
        local_path = Path(local_path)
        entry = self.stat(share, path)
        if entry.is_dir:
            raise SmbClientError(_("{share}/{path} 是目录,请用 download_dir").format(
                share=share, path=path))
        total = entry.size

        start = 0
        if resume and local_path.exists():
            start = local_path.stat().st_size
            if start > total:
                start = 0
            elif start == total:
                if progress:
                    progress(total, total)
                return local_path

        with self._lock:
            conn = self._ensure()
            # 先打开远程文件——失败时不触碰本地文件(避免把已下好的旧文件清零)
            try:
                tid = self._tree(conn, share)
                fid = conn.openFile(
                    tid, path,
                    desiredAccess=FILE_READ_DATA,
                    shareMode=FILE_SHARE_READ,
                    creationOption=FILE_NON_DIRECTORY_FILE,
                    creationDisposition=FILE_OPEN,
                )
            except SessionError as e:
                self._drop_tree(share)
                raise SmbClientError(
                    _("打开远程文件 {share}/{path} 失败: {0}").format(
                        _friendly_session_error(e), share=share, path=path)
                ) from e
            except _CONN_ERRORS as e:
                self._drop_tree(share)
                self.close()
                raise SmbClientError(_("下载 {share}/{path} 连接中断: {e}").format(
                    share=share, path=path, e=e)) from e

            local_path.parent.mkdir(parents=True, exist_ok=True)
            mode = "ab" if start else "wb"
            try:
                with open(local_path, mode) as fh:
                    offset = start
                    if progress:
                        progress(offset, total)
                    while offset < total:
                        if cancel is not None and cancel.is_set():
                            raise TransferCancelled(_("已取消: {share}/{path}").format(
                                share=share, path=path))
                        data = conn.readFile(
                            tid, fid, offset,
                            min(self._max_read, total - offset),
                            singleCall=True,
                        )
                        if not data:
                            # 未读满却提前 EOF:远端文件在下载期间被截断/替换
                            raise SmbClientError(
                                _("下载 {share}/{path} 不完整: 预期 {total} 字节,实际收到 {offset} 字节(远端文件可能已被修改)").format(
                                    share=share, path=path, total=total, offset=offset)
                            )
                        fh.write(data)
                        offset += len(data)
                        if progress:
                            progress(offset, total)
            except SessionError as e:
                self._drop_tree(share)
                raise SmbClientError(
                    _("下载 {share}/{path} 失败: {0}").format(
                        _friendly_session_error(e), share=share, path=path)
                ) from e
            except _CONN_ERRORS as e:
                self._drop_tree(share)
                self.close()
                raise SmbClientError(_("下载 {share}/{path} 连接中断: {e}").format(
                    share=share, path=path, e=e)) from e
            finally:
                try:
                    conn.closeFile(tid, fid)
                except Exception:
                    pass
        return local_path

    def download_dir(
        self,
        share: str,
        path: str,
        local_dir: str | Path,
        progress: Callable[[str, int, int], None] | None = None,
        cancel: threading.Event | None = None,
        resume: bool = False,
    ) -> int:
        """递归下载目录到 local_dir/<目录名>,返回文件数。

        progress(当前文件显示路径, 已传, 总量) 针对单个文件。
        """
        from astro_smb.util import sanitize_local_name

        path = normalize_remote_path(path)
        local_dir = Path(local_dir)
        base_name = sanitize_local_name(ntpath.basename(path) or share)
        root = local_dir / base_name
        count = 0
        for sub, _dirs, files in self.walk(share, path):
            rel = sub[len(path):].lstrip("\\") if path else sub
            target_dir = root
            if rel:
                for part in rel.split("\\"):
                    target_dir = target_dir / sanitize_local_name(part)
            target_dir.mkdir(parents=True, exist_ok=True)
            for f in files:
                if cancel is not None and cancel.is_set():
                    raise TransferCancelled(_("已取消"))
                cb = None
                if progress:
                    display = f.display_path
                    cb = (lambda done, total, _d=display: progress(_d, done, total))
                self.download_file(
                    share, f.path, target_dir / sanitize_local_name(f.name),
                    progress=cb, cancel=cancel, resume=resume,
                )
                count += 1
        return count

    # ---------- 上传 ----------

    def makedirs(self, share: str, path: str) -> None:
        """递归创建远程目录(已存在则忽略)。"""
        path = normalize_remote_path(path)
        if not path:
            return
        parts = path.split("\\")
        cur = ""
        for p in parts:
            cur = f"{cur}\\{p}" if cur else p
            if self.exists(share, cur):
                continue

            def op(conn: SMBConnection, target=cur):
                conn.createDirectory(share, target)

            try:
                self._run(op)
            except SmbClientError as e:
                # **判状态码,不判消息文本。** 原来是 `"已存在" in str(e)` ——
                # 那句话一改文案(或者一做 i18n)就永远匹配不上,于是
                # "目录已存在"变成建目录失败。
                if e.status == nt_errors.STATUS_OBJECT_NAME_COLLISION:
                    continue
                raise SmbClientError(_("创建目录 {share}/{cur} 失败: {e}").format(
                    share=share, cur=cur, e=e)) from e

    def upload_file(
        self,
        local_path: str | Path,
        share: str,
        path: str,
        progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> str:
        """上传单个文件到 share 内 path(目标为文件路径)。覆盖已存在文件。"""
        local_path = Path(local_path)
        if not local_path.is_file():
            raise SmbClientError(_("本地文件不存在: {local_path}").format(local_path=local_path))
        path = normalize_remote_path(path)
        if not path:
            raise SmbClientError(_("上传目标路径不能为空"))
        total = local_path.stat().st_size

        parent = ntpath.dirname(path)
        if parent:
            self.makedirs(share, parent)

        with open(local_path, "rb") as fh:
            with self._lock:
                conn = self._ensure()
                try:
                    tid = self._tree(conn, share)
                    fid = conn.openFile(
                        tid, path,
                        desiredAccess=FILE_WRITE_DATA,
                        shareMode=FILE_SHARE_READ,
                        creationOption=FILE_NON_DIRECTORY_FILE,
                        creationDisposition=FILE_OVERWRITE_IF,
                    )
                except SessionError as e:
                    self._drop_tree(share)
                    raise SmbClientError(
                        _("打开远程文件 {share}/{path} 失败: {0}").format(
                            _friendly_session_error(e), share=share, path=path)
                    ) from e
                except _CONN_ERRORS as e:
                    self._drop_tree(share)
                    self.close()
                    raise SmbClientError(_("上传 {share}/{path} 连接中断: {e}").format(
                        share=share, path=path, e=e)) from e
                try:
                    offset = 0
                    if progress:
                        progress(0, total)
                    while True:
                        if cancel is not None and cancel.is_set():
                            raise TransferCancelled(_("已取消: {name}").format(
                                name=local_path.name))
                        chunk = fh.read(self._max_write)
                        if not chunk:
                            break
                        written = 0
                        while written < len(chunk):
                            n = conn.writeFile(tid, fid, chunk[written:], offset + written)
                            if not n:
                                raise SmbClientError(
                                    _("上传 {share}/{path} 失败: 服务器写入返回 0").format(
                                        share=share, path=path)
                                )
                            written += n
                        offset += len(chunk)
                        if progress:
                            progress(offset, total)
                except SessionError as e:
                    self._drop_tree(share)
                    raise SmbClientError(
                        _("上传 {share}/{path} 失败: {0}").format(
                            _friendly_session_error(e), share=share, path=path)
                    ) from e
                except _CONN_ERRORS as e:
                    self._drop_tree(share)
                    self.close()
                    raise SmbClientError(_("上传 {share}/{path} 连接中断: {e}").format(
                        share=share, path=path, e=e)) from e
                finally:
                    try:
                        conn.closeFile(tid, fid)
                    except Exception:
                        pass
        return path

    def upload_dir(
        self,
        local_dir: str | Path,
        share: str,
        path: str,
        progress: Callable[[str, int, int], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> int:
        """递归上传本地目录到 share 内 path/<目录名>,返回文件数。"""
        local_dir = Path(local_dir)
        if not local_dir.is_dir():
            raise SmbClientError(_("本地目录不存在: {local_dir}").format(local_dir=local_dir))
        path = normalize_remote_path(path)
        base = f"{path}\\{local_dir.name}" if path else local_dir.name
        count = 0
        for dirpath, _dirnames, filenames in os.walk(local_dir):
            rel = Path(dirpath).relative_to(local_dir)
            remote_dir = base if str(rel) == "." else f"{base}\\{str(rel).replace(os.sep, chr(92))}"
            self.makedirs(share, remote_dir)
            for fn in filenames:
                if cancel is not None and cancel.is_set():
                    raise TransferCancelled(_("已取消"))
                lp = Path(dirpath) / fn
                rp = f"{remote_dir}\\{fn}"
                cb = None
                if progress:
                    display = str(lp)
                    cb = (lambda done, total, _d=display: progress(_d, done, total))
                self.upload_file(lp, share, rp, progress=cb, cancel=cancel)
                count += 1
        return count

    # ---------- 修改类操作 ----------

    def mkdir(self, share: str, path: str) -> None:
        self.makedirs(share, path)

    def remove(self, share: str, path: str) -> None:
        path = normalize_remote_path(path)

        def op(conn: SMBConnection):
            conn.deleteFile(share, path)

        try:
            self._run(op, idempotent=False)
        except SmbClientError as e:
            raise SmbClientError(_("删除 {share}/{path} 失败: {e}").format(
                share=share, path=path, e=e)) from e

    def rmdir(self, share: str, path: str, recursive: bool = False) -> None:
        path = normalize_remote_path(path)
        if recursive:
            # 后序遍历删除
            stack: list[tuple[str, list[RemoteEntry], list[RemoteEntry]]] = list(
                self.walk(share, path)
            )
            for sub, _dirs, files in reversed(stack):
                for f in files:
                    self.remove(share, f.path)

                def op(conn: SMBConnection, target=sub):
                    conn.deleteDirectory(share, target)

                try:
                    self._run(op, idempotent=False)
                except SmbClientError as e:
                    raise SmbClientError(_("删除目录 {share}/{sub} 失败: {e}").format(
                        share=share, sub=sub, e=e)) from e
            return

        def op(conn: SMBConnection):
            conn.deleteDirectory(share, path)

        try:
            self._run(op, idempotent=False)
        except SmbClientError as e:
            raise SmbClientError(_("删除目录 {share}/{path} 失败: {e}").format(
                share=share, path=path, e=e)) from e

    def rename(self, share: str, old: str, new: str) -> None:
        old = normalize_remote_path(old)
        new = normalize_remote_path(new)

        def op(conn: SMBConnection):
            conn.rename(share, old, new)

        try:
            self._run(op, idempotent=False)
        except SmbClientError as e:
            raise SmbClientError(_("重命名 {share}/{old} -> {new} 失败: {e}").format(
                share=share, old=old, new=new, e=e)) from e
