"""存储后端抽象 + 本地磁盘后端。

**为什么存在**:ASIAIR 的存储卡不只在盒子里 —— 也可能被拔出来**直接插在电脑上**
(实测 ZWO Pro 盒子挂在 ``E:\\``)。各页面(浏览/记录/导星/空间/传输)全部是围绕
``AstroSmbClient`` 的公开方法写的,所以只要提供一个**方法签名完全一致**的本地实现,
上层就能零改动地把本地盘当成一台"设备"用。

本模块提供两样东西:

1. :class:`StorageBackend` —— 契约(``typing.Protocol``)。它把 ``AstroSmbClient``
   已有的公开方法**原样抄了一遍**,只作文档化 + ``isinstance`` 检查用。
   **不要为它改 client.py**,``AstroSmbClient`` 天然满足该协议。
2. :class:`LocalBackend` —— 用 ``pathlib``/``os.scandir`` 实现同一套契约的本地磁盘后端。

设计约定(与 SMB 侧保持一一对应,上层不必分支):

- **单共享模型**:一个 ``LocalBackend`` 只暴露**一个**共享,该共享的根就是 ``root``。
  于是各页 ``(share, path)`` 的既有逻辑原封不动 —— ``share`` 是卷标/盘符,
  ``path`` 是共享内相对路径、**反斜杠**分隔、根为 ``""``。
- **对外只抛** :class:`~astro_smb.client.SmbClientError`
  (取消抛其子类 :class:`~astro_smb.client.TransferCancelled`),与全局约定一致。
- **路径安全**:任何越出 ``root`` 的路径(``..`` 逃逸、盘符、符号链接指出去)一律拒绝。
- **无连接状态**:``connect``/``close``/``reconnect`` 基本是空操作;``clone()`` 返回新实例
  (本地虽然天然线程安全,但要保持接口一致,供 ParallelDownloader / 各页工作线程使用)。

``download_file`` 与 SMB 侧的差别只有一处、且是**更安全**的方向:本地拷贝写到
``<目标>.part`` 再 ``os.replace`` 原子落盘,最终路径上永远不会出现"大小对但内容不全"
的半成品(docs/DEVELOPMENT.md §7.3 记录过这个坑)。``resume=True`` 相应地**从 ``.part`` 续传**,
所以取消后重试仍然只补差量。
"""

from __future__ import annotations

import fnmatch
import ntpath
import os
import platform
import shutil
import threading
from collections import deque
from pathlib import Path
from typing import Callable, Iterator, Protocol, runtime_checkable

from astro_smb.client import (
    DirStat,
    ProgressCallback,
    RemoteEntry,
    ShareInfo,
    SmbClientError,
    TransferCancelled,
    TreeNode,
    VolumeInfo,
)
from astro_smb.util import sanitize_local_name
from astro_smb.i18n import gettext as _

# Windows FILE_ATTRIBUTE_*(RemoteEntry.attr_text 按这套位来解释)
ATTR_READONLY = 0x01
ATTR_HIDDEN = 0x02
ATTR_SYSTEM = 0x04
ATTR_DIRECTORY = 0x10
ATTR_ARCHIVE = 0x20
# 重解析点(目录联接 / 符号链接):walk 绝不下降进去,否则指回祖先就是无限递归
ATTR_REPARSE_POINT = 0x400

# STYPE_DISKTREE:ShareInfo.is_disk 靠它判定
SHARE_TYPE_DISK = 0


def _windows_volume_label(root: str) -> str:
    """读盘符根的卷标(Windows);非 Windows 或失败一律返回空串。

    只用 GetVolumeInformationW 查这一个卷(微秒级),不做全盘枚举 ——
    构造后端时会调用,不能有可感知开销。
    """
    if os.name != "nt":
        return ""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(261)
        drive = os.path.splitdrive(root)[0]
        if not drive:
            return ""
        # 探测不可读的卷(空读卡器)时抑制系统的"请插入磁盘"对话框
        old = ctypes.windll.kernel32.SetErrorMode(0x0001)   # SEM_FAILCRITICALERRORS
        try:
            ok = ctypes.windll.kernel32.GetVolumeInformationW(
                ctypes.c_wchar_p(drive + "\\"), buf, ctypes.sizeof(buf),
                None, None, None, None, 0)
        finally:
            ctypes.windll.kernel32.SetErrorMode(old)
        return buf.value.strip() if ok else ""
    except Exception:
        return ""

#: 上层真正用到的后端方法(比 Protocol 更精确的诊断用清单)
REQUIRED_METHODS: tuple[str, ...] = (
    "connect", "close", "clone", "reconnect",
    "list_shares", "listdir", "stat", "exists",
    "read_bytes", "download_range",
    "download_file", "download_dir", "upload_file", "upload_dir",
    "makedirs", "mkdir", "remove", "rmdir", "rename",
    "walk", "find", "count_children", "dir_stat", "scan_children", "dir_tree",
    "volume_info", "server_info", "echo", "ping_tcp",
)


@runtime_checkable
class StorageBackend(Protocol):
    """存储后端契约:``AstroSmbClient``(SMB)与 :class:`LocalBackend`(本地盘)共同实现。

    方法签名与 :class:`~astro_smb.client.AstroSmbClient` 的公开方法**逐字一致**;
    这里只是把契约写下来,便于新写页面时知道能依赖什么。

    ``isinstance(obj, StorageBackend)`` 可用(只按属性名检查,不校验签名);
    ``issubclass`` **不可用** —— 协议里有 ``host`` 这样的非方法成员,
    CPython 会直接抛 ``TypeError``。要更精确的诊断请用 :func:`missing_methods`。
    """

    @property
    def host(self) -> str:
        """设备标识:SMB 是 IP/主机名,本地盘是根路径(如 ``E:\\``)。"""
        ...

    def connect(self) -> None: ...
    def close(self) -> None: ...
    def clone(self) -> "StorageBackend": ...
    def reconnect(self) -> None: ...

    def list_shares(self, include_hidden: bool = False) -> list[ShareInfo]: ...
    def listdir(self, share: str, path: str = "") -> list[RemoteEntry]: ...
    def stat(self, share: str, path: str) -> RemoteEntry: ...
    def exists(self, share: str, path: str) -> bool: ...

    def read_bytes(self, share: str, path: str, offset: int = 0,
                   size: int = 65536) -> bytes: ...

    def download_range(self, share: str, path: str, offset: int, length: int, fh,
                       cancel: threading.Event | None = None,
                       on_bytes: Callable[[int], None] | None = None) -> None: ...

    def download_file(self, share: str, path: str, local_path: str | Path,
                      progress: ProgressCallback | None = None,
                      cancel: threading.Event | None = None,
                      resume: bool = False) -> Path: ...

    def download_dir(self, share: str, path: str, local_dir: str | Path,
                     progress: Callable[[str, int, int], None] | None = None,
                     cancel: threading.Event | None = None,
                     resume: bool = False) -> int: ...

    def upload_file(self, local_path: str | Path, share: str, path: str,
                    progress: ProgressCallback | None = None,
                    cancel: threading.Event | None = None) -> str: ...

    def upload_dir(self, local_dir: str | Path, share: str, path: str,
                   progress: Callable[[str, int, int], None] | None = None,
                   cancel: threading.Event | None = None) -> int: ...

    def makedirs(self, share: str, path: str) -> None: ...
    def mkdir(self, share: str, path: str) -> None: ...
    def remove(self, share: str, path: str) -> None: ...
    def rmdir(self, share: str, path: str, recursive: bool = False) -> None: ...
    def rename(self, share: str, old: str, new: str) -> None: ...

    def walk(self, share: str, top: str = "", max_depth: int | None = None,
             on_error: Callable[[str, Exception], None] | None = None,
             depth_first: bool = False,
             cancel: threading.Event | None = None
             ) -> Iterator[tuple[str, list[RemoteEntry], list[RemoteEntry]]]: ...

    def find(self, share: str, top: str = "", pattern: str = "*",
             include_dirs: bool = False, min_size: int | None = None,
             max_size: int | None = None, newer_than: float | None = None,
             max_depth: int | None = None, limit: int | None = None,
             cancel: threading.Event | None = None,
             on_error: Callable[[str, Exception], None] | None = None
             ) -> Iterator[RemoteEntry]: ...

    def count_children(self, share: str, path: str) -> tuple[int, int] | None: ...

    def dir_stat(self, share: str, path: str = "",
                 cancel: threading.Event | None = None,
                 on_progress: Callable[[int, int], None] | None = None) -> DirStat: ...

    def scan_children(self, share: str, path: str = "",
                      cancel: threading.Event | None = None,
                      on_item: Callable[[RemoteEntry, int, bool], None] | None = None
                      ) -> list[tuple[RemoteEntry, int]]: ...

    def dir_tree(self, share: str, path: str = "",
                 cancel: threading.Event | None = None,
                 on_progress: Callable[[int, int], None] | None = None) -> TreeNode: ...

    def volume_info(self, share: str) -> VolumeInfo | None: ...
    def server_info(self) -> dict[str, str]: ...
    def echo(self) -> float: ...
    def ping_tcp(self, timeout: float = 1.5) -> float | None: ...


def missing_methods(obj) -> list[str]:
    """列出 obj 相对 :data:`REQUIRED_METHODS` 缺少的方法(空列表 = 满足契约)。

    比 ``isinstance(obj, StorageBackend)`` 更适合报错时给出人话原因。
    """
    return [m for m in REQUIRED_METHODS if not callable(getattr(obj, m, None))]


def is_local(backend) -> bool:
    """这个后端是不是本地磁盘(上层用来决定文案/是否显示心跳等)。"""
    return bool(getattr(backend, "is_local", False))


def guess_kind(host: str) -> str:
    """只给了一串地址、没给 ``kind`` 时,猜该建哪种后端(``"local"``/``"smb"``)。

    ``devices._looks_local`` 是**纯字符串**判据(盘符 / 以 ``/`` 开头),因为它
    要给 ``host_key`` 做记录归一,必须廉价且离线可测。可**相对目录**
    (``.tmp/device/EMMC Images``)光看字符串判不出来 —— 它既没有盘符也不以
    ``/`` 开头,于是会被当成主机名送进 SMB,在 IDNA 编码那步炸掉。

    这里补的是那一格,判据刻意收得很紧:**既要含路径分隔符,又要磁盘上确实
    是个目录**。形状像主机名的一概走 SMB —— 否则当前目录里恰好有个叫
    ``192.0.2.227`` 的文件夹就会悄悄改掉语义。
    """
    h = (host or "").strip()
    if not h:
        return "smb"
    # UNC(`\\host\share` / `//host/share`)是**网络**路径,而且这一条必须排在
    # `_looks_local` 前面 —— 正斜杠 UNC 以 `/` 开头,会被它当成本地绝对路径
    # (`views.devices._host_from_text` 在另一层踩过同一个坑)。也别拿去
    # `is_dir()` 探盘:那会真的发一次网络请求,拖住一个本该瞬时的判断。
    if h.startswith(("\\\\", "//")):
        return "smb"
    from astro_smb import devices as _dv
    if _dv._looks_local(h):
        return "local"
    if ("/" in h or "\\" in h) and Path(h).is_dir():
        return "local"
    return "smb"


def make_backend(kind: str = "smb", host: str = "", path: str = "",
                 label: str = "", **kwargs) -> StorageBackend:
    """按设备种类建后端 —— **上层唯一需要分支的地方**。

    - ``kind="local"``:建 :class:`LocalBackend`,根路径取 ``path`` 或 ``host``
      (设备记录里两者相同);
    - 其余(含未知值):建 ``AstroSmbClient(host=host, **kwargs)``。

    ``kwargs`` 原样透给对应构造函数(如 SMB 的 ``timeout=3``);本地后端只认
    ``chunk_size``/``timeout``,其余键会被忽略,免得调用方为两种设备写两套参数。
    """
    kind_l = (kind or "").strip().lower()
    # 离线镜像:`kind="mirror"`,或地址写成 `mirror:<目录>`。
    # 设备是 DHCP 的、会重启、会被别的实例占着 —— 界面开发不该被它拴住。
    if kind_l == "mirror" or (host or "").startswith("mirror:"):
        from astro_smb.mirror import MirrorBackend
        root = (path or "").strip() or (host or "").split(":", 1)[-1]
        if not root:
            raise SmbClientError(_("镜像后端缺少目录"))
        return MirrorBackend(root)
    if kind_l == "local":
        root = (path or host or "").strip()
        if not root:
            raise SmbClientError(_("本地设备缺少根路径"))
        keep = {k: v for k, v in kwargs.items() if k in ("chunk_size", "timeout")}
        return LocalBackend(root, label=label, **keep)
    if not (host or "").strip():
        raise SmbClientError(_("设备地址不能为空"))
    from astro_smb.client import AstroSmbClient
    return AstroSmbClient(host=host, **kwargs)


# ---------------------------------------------------------------- 内部工具

def _safe_resolve(p: Path) -> Path:
    """尽力把路径规范化;设备不在/权限不足时退回 abspath(绝不抛)。"""
    try:
        return p.resolve()
    except (OSError, ValueError):
        try:
            return Path(os.path.abspath(str(p)))
        except (OSError, ValueError):
            return p


def _normcase(p: Path) -> str:
    return os.path.normcase(str(p))


def _within(child: Path, parent: Path) -> bool:
    """child 是否在 parent 之内(含相等)。Windows 上大小写不敏感。"""
    try:
        if child == parent or child.is_relative_to(parent):
            return True
    except (OSError, ValueError):
        pass
    cs, ps = _normcase(child), _normcase(parent)
    if cs == ps:
        return True
    sep = os.sep
    return cs.startswith(ps if ps.endswith(sep) else ps + sep)


def _attrs_of(st, name: str, is_dir: bool, target: Path | None = None) -> int:
    """把 os.stat_result 翻成 Windows 风格的属性位(RemoteEntry.attr_text 用)。"""
    raw = getattr(st, "st_file_attributes", None)
    if isinstance(raw, int) and raw:
        return raw                  # Windows:已含 REPARSE_POINT(0x400)
    attrs = ATTR_DIRECTORY if is_dir else ATTR_ARCHIVE
    if name.startswith("."):        # POSIX 惯例的隐藏项
        attrs |= ATTR_HIDDEN
    # POSIX 没有 st_file_attributes:符号链接自己补上重解析位,
    # 否则 walk 会跟着符号链接下降,指回祖先就是无限递归(与 Windows 联接同理)
    if target is not None:
        try:
            if target.is_symlink():
                attrs |= ATTR_REPARSE_POINT
        except OSError:
            pass
    return attrs


def _same_path(a: Path, b: Path) -> bool:
    try:
        if a.exists() and b.exists():
            return os.path.samefile(a, b)
    except OSError:
        pass
    return _normcase(_safe_resolve(a)) == _normcase(_safe_resolve(b))


class LocalBackend:
    """把一个本地目录/盘符当成"一台设备的一个共享"来访问。

    用法::

        b = LocalBackend(Path("E:/"), label="ASIAIR")
        b.connect()
        share = b.list_shares()[0].name
        for e in b.listdir(share, "Autorun"):
            print(e.name, e.size)

    线程安全:本类无连接状态、不持有跨调用的句柄,天然可多线程使用;
    但为了与 SMB 侧接口一致(以及未来可能的状态),并行传输仍请各线程 ``clone()``。
    """

    #: 上层可用 ``getattr(backend, "is_local", False)`` 廉价分支
    is_local = True

    #: 与 AstroSmbClient.port 对齐的占位(本地无端口)
    port = 0
    username = ""
    password = ""

    def __init__(
        self,
        root: str | Path,
        label: str = "",
        chunk_size: int = 1 << 22,      # 4 MiB,本地拷贝的读写块
        timeout: float = 0.0,           # 仅为接口一致,本地不使用
    ):
        self.root = Path(root)
        self.label = (label or self._default_label(self.root)).strip() or _("本地磁盘")
        self.chunk_size = max(1 << 16, int(chunk_size))
        self.timeout = timeout
        self._real_root = _safe_resolve(self.root)
        self._connected = False
        self._lock = threading.RLock()      # 仅保护 _connected/_real_root

    # ---------- 标识 ----------

    @staticmethod
    def _default_label(root: Path) -> str:
        """没给卷标时的兜底名:普通目录用目录名;**盘符根优先读真实卷标**。

        读卷标是为了让同一张卡**无论从哪条路径打开**(设备记录里带 name、
        或 ASTRO_SMB_HOST 直接指盘符)都得到同一个共享名 —— 共享名会进
        metacache 的键与用户可见路径,不稳定会造成缓存穿透与路径失配
        (真机踩过:同一张卡一会儿叫 'asiair' 一会儿叫 'E:')。
        """
        name = root.name
        if name:
            return name
        label = _windows_volume_label(str(root))
        if label:
            return label
        drive = os.path.splitdrive(str(root))[0]
        return drive.rstrip("\\/") or str(root)

    @property
    def host(self) -> str:
        """设备标识 = 根路径字符串(设备记录/传输任务用它区分设备)。"""
        return str(self.root)

    @property
    def share_name(self) -> str:
        """唯一共享的名字(卷标或盘符)。"""
        return self.label

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def dialect_name(self) -> str:
        return _("本地磁盘")

    def __repr__(self) -> str:      # pragma: no cover - 调试用
        return f"<LocalBackend root={self.root!s} label={self.label!r}>"

    # ---------- 连接管理(本地基本是空操作) ----------

    def connect(self) -> None:
        """校验根目录当前可用(卡被拔掉/盘符失效时在这里报错)。"""
        with self._lock:
            real = _safe_resolve(self.root)
            try:
                ok = real.is_dir()
            except OSError as e:
                raise SmbClientError(_("无法访问本地路径 {root}: {e}").format(
                    root=self.root, e=e)) from e
            if not ok:
                raise SmbClientError(_("本地路径不存在或不是目录: {root}").format(root=self.root))
            self._real_root = real
            self._connected = True

    def close(self) -> None:
        with self._lock:
            self._connected = False

    def reconnect(self) -> None:
        self.close()
        self.connect()

    def clone(self) -> "LocalBackend":
        """新建同配置实例(本地无连接状态,但保持与 SMB 侧一致的用法)。"""
        return LocalBackend(self.root, label=self.label,
                            chunk_size=self.chunk_size, timeout=self.timeout)

    def __enter__(self) -> "LocalBackend":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def echo(self) -> float:
        """心跳:确认根目录仍可访问,RTT 记 0 毫秒(本地无网络往返)。

        **不能无条件返回 0.0** —— 卡被拔掉后心跳会一直报"在线 0 ms",
        而此时 listdir 早就抛错了(审查实测)。shell 的心跳循环只调 echo(),
        从不调 connect(),所以这里必须自己做存活判定。
        """
        root = getattr(self, "_real_root", None)
        if root is None or not os.path.isdir(root):
            raise SmbClientError(_("本地设备不可用(已拔出?): {host}").format(host=self.host))
        return 0.0

    def ping_tcp(self, timeout: float = 1.5) -> float | None:
        """本地无网络往返:介质在返回 0 毫秒,拔掉返回 None(= 不可达)。"""
        root = getattr(self, "_real_root", None)
        return 0.0 if (root is not None and os.path.isdir(root)) else None

    def server_info(self) -> dict[str, str]:
        return {
            "host": self.host,
            "dialect": self.dialect_name,
            "server_name": self.label,
            "server_os": f"{platform.system()} {platform.release()}".strip(),
            "server_domain": _("本地"),
        }

    # ---------- 路径解析(安全边界) ----------

    def _check_share(self, share: str) -> None:
        """本地只有一个共享;传空串视为该共享(容错)。"""
        s = (share or "").strip()
        if not s or s.casefold() == self.share_name.casefold():
            return
        raise SmbClientError(_("共享不存在: {share}(本地设备只有 {share_name!r})").format(
            share=share, share_name=self.share_name))

    def _rel(self, share: str, path: str) -> str:
        """校验并规范化共享内相对路径(反斜杠分隔,根为 ``""``)。

        ``..``、盘符(``C:\\...``)、UNC 前缀在这里被拒绝 —— **必须先于**
        ``normalize_remote_path``:后者会把 ``..\\..\\x`` 无声折叠成合法的 ``x``,
        逃逸企图就再也看不见了。

        **本类内一切接受 path 的方法都必须先过这一关**(而不是直接调
        normalize_remote_path),否则 upload/rename/rmdir 这些写操作会被折叠后放行。
        """
        self._check_share(share)
        raw = (path or "").replace("/", "\\")
        parts = [p for p in raw.split("\\") if p not in ("", ".")]
        for p in parts:
            if p == "..":
                raise SmbClientError(_("路径越界(不允许 '..'): {share}/{path}").format(
                    share=share, path=path))
            if ":" in p:
                raise SmbClientError(_("非法路径片段 {p!r}: {share}/{path}").format(
                    p=p, share=share, path=path))
        return "\\".join(parts)

    def _resolve(self, share: str, path: str) -> Path:
        """共享内路径 → 本地绝对路径,并拒绝一切越界。

        先过 :meth:`_rel` 的语法关,再用 resolve() 兜住符号链接/联接点指到根之外
        的情况(卡上一般没有,但 root 指向普通目录时可能有)。
        """
        rel = self._rel(share, path)
        target = self._real_root.joinpath(*rel.split("\\")) if rel else self._real_root
        real = _safe_resolve(target)
        if not _within(real, self._real_root):
            raise SmbClientError(_("路径越界(超出 {root}): {share}/{path}").format(
                root=self.root, share=share, path=path))
        return real

    def _entry(self, share: str, rel: str, name: str, target: Path,
               st=None, is_dir: bool | None = None) -> RemoteEntry:
        """构造 RemoteEntry;stat 失败时退化成"零元数据"条目而不是整目录失败。"""
        try:
            st = st if st is not None else target.stat()
        except OSError:
            st = None
        if is_dir is None:
            try:
                is_dir = target.is_dir()
            except OSError:
                is_dir = False
        return RemoteEntry(
            share=share,
            path=rel,
            name=name,
            is_dir=bool(is_dir),
            size=0 if (is_dir or st is None) else int(st.st_size),
            mtime=float(getattr(st, "st_mtime", 0.0) or 0.0),
            ctime=float(getattr(st, "st_ctime", 0.0) or 0.0),
            atime=float(getattr(st, "st_atime", 0.0) or 0.0),
            attributes=_attrs_of(st, name, bool(is_dir), target),
        )

    # ---------- 枚举 ----------

    def list_shares(self, include_hidden: bool = False) -> list[ShareInfo]:
        """本地设备只有一个"共享" —— 它的根就是 ``root``。"""
        return [ShareInfo(name=self.share_name, type=SHARE_TYPE_DISK,
                          remark=str(self.root))]

    def listdir(self, share: str, path: str = "") -> list[RemoteEntry]:
        rel = self._rel(share, path)
        base = self._resolve(share, rel)
        entries: list[RemoteEntry] = []
        try:
            with os.scandir(base) as it:
                for de in it:
                    try:
                        is_dir = de.is_dir()
                    except OSError:
                        is_dir = False
                    try:
                        st = de.stat()
                    except OSError:
                        st = None
                    entries.append(self._entry(
                        share, f"{rel}\\{de.name}" if rel else de.name,
                        de.name, Path(de.path), st=st, is_dir=is_dir))
        except NotADirectoryError as e:
            raise SmbClientError(_("列目录 {share}/{rel} 失败: 目标不是目录").format(
                share=share, rel=rel)) from e
        except FileNotFoundError as e:
            raise SmbClientError(_("列目录 {share}/{rel} 失败: 路径不存在").format(
                share=share, rel=rel)) from e
        except PermissionError as e:
            raise SmbClientError(_("列目录 {share}/{rel} 失败: 访问被拒绝").format(
                share=share, rel=rel)) from e
        except OSError as e:
            raise SmbClientError(_("列目录 {share}/{rel} 失败: {e}").format(
                share=share, rel=rel, e=e)) from e
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return entries

    def stat(self, share: str, path: str) -> RemoteEntry:
        rel = self._rel(share, path)
        if not rel:
            # 与 SMB 侧一致:共享根返回一个伪条目
            return RemoteEntry(share=share, path="", name=self.share_name,
                               is_dir=True, size=0, mtime=0, ctime=0, atime=0,
                               attributes=ATTR_DIRECTORY)
        target = self._resolve(share, rel)
        try:
            st = target.stat()
        except FileNotFoundError as e:
            raise SmbClientError(_("路径不存在: {share}/{rel}").format(
                share=share, rel=rel)) from e
        except PermissionError as e:
            raise SmbClientError(_("访问被拒绝: {share}/{rel}").format(
                share=share, rel=rel)) from e
        except OSError as e:
            raise SmbClientError(_("读取 {share}/{rel} 失败: {e}").format(
                share=share, rel=rel, e=e)) from e
        return self._entry(share, rel, ntpath.basename(rel), target, st=st,
                           is_dir=target.is_dir())

    def exists(self, share: str, path: str) -> bool:
        try:
            self.stat(share, path)
            return True
        except SmbClientError:
            return False

    # ---------- 卷容量 ----------

    def volume_info(self, share: str) -> VolumeInfo | None:
        """本卷的总量/可用量;取不到返回 None(与 SMB 侧一致)。"""
        try:
            self._check_share(share)
            usage = shutil.disk_usage(self._real_root)
        except (SmbClientError, OSError, ValueError):
            return None
        return VolumeInfo(total=int(usage.total), free=int(usage.free))

    # ---------- 统计 ----------

    def count_children(self, share: str, path: str) -> tuple[int, int] | None:
        """(子目录数, 子文件数);枚举失败返回 None(与"真空目录"区分)。"""
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
        """一次遍历构建整棵占用树(与 SMB 侧同一套语义,含 partial/error_count)。"""
        rel = self._rel(share, path)
        root = TreeNode(name=ntpath.basename(rel) or self.share_name,
                        path=rel, is_dir=True)
        nodes: dict[str, TreeNode] = {rel: root}
        nfiles = nbytes = 0
        errors = [0]
        yielded = [False]

        def on_err(_p: str, _e: Exception) -> None:
            errors[0] += 1

        for dirpath, dirs, files in self.walk(share, rel, on_error=on_err,
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
            raise SmbClientError(_("扫描 {share}/{0} 失败(根目录枚举失败)").format(
                rel or '', share=share))

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
        """类 os.walk,yield (共享内路径, 子目录, 文件)。默认广度优先。

        与 SMB 侧一致:cancel 在**每个目录处理前**检查,置位抛 TransferCancelled。
        """
        top = self._rel(share, top)
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
                # **绝不下降到重解析点(目录联接/符号链接)**:它可能指回祖先,
                # 于是 walk 无限递归。审查实测:用 mklink /J 建一个指回根的联接后,
                # 一个"1 文件 10 字节"的目录被 dir_stat 报成 65 文件 / 650 字节 /
                # 130 目录,而且只有靠 max_depth 或 cancel 才停得下来 ——
                # download_dir 会一直复制到填满磁盘。
                # os.walk 的 followlinks=False 正是为此;listdir 早就把
                # FILE_ATTRIBUTE_REPARSE_POINT 带出来了,这里用上。
                children = [(d.path, depth + 1) for d in dirs
                            if not (d.attributes & ATTR_REPARSE_POINT)]
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

    def read_bytes(self, share: str, path: str, offset: int = 0,
                   size: int = 65536) -> bytes:
        """读取文件的一段字节(FITS 头部分读取靠它,必须是 seek+read 而非全读)。"""
        target = self._resolve(share, path)
        if size <= 0:
            return b""
        try:
            with open(target, "rb") as fh:
                if offset:
                    fh.seek(offset)
                return fh.read(size)
        except IsADirectoryError as e:
            raise SmbClientError(_("读取 {share}/{path} 失败: 目标是目录").format(
                share=share, path=path)) from e
        except FileNotFoundError as e:
            raise SmbClientError(_("路径不存在: {share}/{path}").format(
                share=share, path=path)) from e
        except PermissionError as e:
            raise SmbClientError(_("读取 {share}/{path} 失败: 访问被拒绝").format(
                share=share, path=path)) from e
        except OSError as e:
            raise SmbClientError(_("读取 {share}/{path} 失败: {e}").format(
                share=share, path=path, e=e)) from e

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
        """把 [offset, offset+length) 拷到已打开句柄 fh 的对应偏移处。

        供 ParallelDownloader 用(本地并发拷贝收益有限,但保持接口一致,
        免得上层为"是不是本地"分支)。
        """
        src = self._resolve(share, path)
        end = offset + length
        try:
            with open(src, "rb") as rf:
                rf.seek(offset)
                fh.seek(offset)
                o = offset
                while o < end:
                    if cancel is not None and cancel.is_set():
                        raise TransferCancelled(_("已取消: {share}/{path}").format(
                            share=share, path=path))
                    data = rf.read(min(self.chunk_size, end - o))
                    if not data:
                        raise SmbClientError(
                            _("读取 {share}/{path} 区块不完整(文件可能已变)").format(
                                share=share, path=path))
                    fh.write(data)
                    o += len(data)
                    if on_bytes:
                        on_bytes(len(data))
        except (SmbClientError, TransferCancelled):
            raise
        except OSError as e:
            raise SmbClientError(_("读取 {share}/{path} 失败: {e}").format(
                share=share, path=path, e=e)) from e

    # ---------- 下载(本地拷贝) ----------

    def download_file(
        self,
        share: str,
        path: str,
        local_path: str | Path,
        progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
        resume: bool = False,
    ) -> Path:
        """把共享内文件拷到本地路径:``.part`` 暂存 + ``os.replace`` 原子落盘。

        ``resume=True`` 时:目标已完整则直接返回;否则**从 ``.part`` 续**(取消/失败
        保留 ``.part``,下次只补差量)。最终路径上永远不会出现半成品。
        """
        src = self._resolve(share, path)
        entry = self.stat(share, path)
        if entry.is_dir:
            raise SmbClientError(_("{share}/{path} 是目录,请用 download_dir").format(
                share=share, path=path))
        total = entry.size
        local_path = Path(local_path)
        if _same_path(src, local_path):
            raise SmbClientError(_("源和目标是同一个文件: {local_path}").format(
                local_path=local_path))
        part = local_path.with_name(local_path.name + ".part")

        start = 0
        if resume:
            try:
                if local_path.exists() and local_path.stat().st_size == total:
                    if progress:
                        progress(total, total)
                    return local_path
                if part.exists():
                    # **比源还大的 .part 只能是废物**(源被换小了,或残留的是
                    # 别的文件)。原来的 min() 会把它原样留着当"已完成",
                    # 于是最终文件大小对不上、内容还是旧的,函数却正常返回。
                    # SMB 侧一直是 `if start > total: start = 0`,这里对齐。
                    psize = part.stat().st_size
                    start = psize if psize <= total else 0
            except OSError:
                start = 0
        else:
            self._discard(part)     # 别把上一次的残留续到这次里

        try:
            local_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise SmbClientError(_("创建本地目录 {parent} 失败: {e}").format(
                parent=local_path.parent, e=e)) from e

        try:
            with open(src, "rb") as rf, open(part, "ab" if start else "wb") as wf:
                if start:
                    rf.seek(start)
                offset = start
                if progress:
                    progress(offset, total)
                while offset < total:
                    if cancel is not None and cancel.is_set():
                        raise TransferCancelled(_("已取消: {share}/{path}").format(
                            share=share, path=path))
                    data = rf.read(min(self.chunk_size, total - offset))
                    if not data:
                        raise SmbClientError(
                            _("下载 {share}/{path} 不完整: 预期 {total} 字节,实际读到 {offset} 字节(源文件可能已被修改)").format(
                                share=share, path=path, total=total, offset=offset))
                    wf.write(data)
                    offset += len(data)
                    if progress:
                        progress(offset, total)
        except (SmbClientError, TransferCancelled):
            raise                   # 保留 .part 供续传
        except OSError as e:
            raise SmbClientError(_("下载 {share}/{path} 失败: {e}").format(
                share=share, path=path, e=e)) from e

        try:
            os.replace(part, local_path)
        except OSError as e:
            raise SmbClientError(_("替换目标文件 {local_path} 失败: {e}").format(
                local_path=local_path, e=e)) from e
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
        """递归下载目录到 local_dir/<目录名>,返回文件数。"""
        rel = self._rel(share, path)
        local_dir = Path(local_dir)
        base_name = sanitize_local_name(ntpath.basename(rel) or self.share_name)
        root = local_dir / base_name
        count = 0
        for sub, _dirs, files in self.walk(share, rel):
            sub_rel = sub[len(rel):].lstrip("\\") if rel else sub
            target_dir = root
            if sub_rel:
                for part in sub_rel.split("\\"):
                    target_dir = target_dir / sanitize_local_name(part)
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise SmbClientError(_("创建本地目录 {target_dir} 失败: {e}").format(
                    target_dir=target_dir, e=e)) from e
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

    # ---------- 上传(拷进共享) ----------

    def makedirs(self, share: str, path: str) -> None:
        """递归创建目录(已存在则忽略)。"""
        rel = self._rel(share, path)
        if not rel:
            return
        target = self._resolve(share, rel)
        try:
            target.mkdir(parents=True, exist_ok=True)
        except FileExistsError:
            if target.is_dir():
                return
            raise SmbClientError(_("创建目录 {share}/{rel} 失败: 同名文件已存在").format(
                share=share, rel=rel))
        except OSError as e:
            raise SmbClientError(_("创建目录 {share}/{rel} 失败: {e}").format(
                share=share, rel=rel, e=e)) from e

    def mkdir(self, share: str, path: str) -> None:
        self.makedirs(share, path)

    def upload_file(
        self,
        local_path: str | Path,
        share: str,
        path: str,
        progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> str:
        """把本地文件拷进共享内 path(目标为文件路径)。覆盖已存在文件。

        同样走 ``.part`` + ``os.replace``:取消/失败不会在卡上留下截断的半个文件
        (上传没有续传语义,所以失败时**删掉** ``.part``)。
        """
        local_path = Path(local_path)
        if not local_path.is_file():
            raise SmbClientError(_("本地文件不存在: {local_path}").format(local_path=local_path))
        rel = self._rel(share, path)
        if not rel:
            raise SmbClientError(_("上传目标路径不能为空"))
        dest = self._resolve(share, rel)
        if _same_path(local_path, dest):
            raise SmbClientError(_("源和目标是同一个文件: {dest}").format(dest=dest))
        parent = ntpath.dirname(rel)
        if parent:
            self.makedirs(share, parent)
        total = local_path.stat().st_size
        part = dest.with_name(dest.name + ".part")
        try:
            with open(local_path, "rb") as rf, open(part, "wb") as wf:
                offset = 0
                if progress:
                    progress(0, total)
                while True:
                    if cancel is not None and cancel.is_set():
                        raise TransferCancelled(_("已取消: {name}").format(
                            name=local_path.name))
                    chunk = rf.read(self.chunk_size)
                    if not chunk:
                        break
                    wf.write(chunk)
                    offset += len(chunk)
                    if progress:
                        progress(offset, total)
        except TransferCancelled:
            self._discard(part)
            raise
        except OSError as e:
            self._discard(part)
            raise SmbClientError(_("上传 {share}/{rel} 失败: {e}").format(
                share=share, rel=rel, e=e)) from e
        try:
            os.replace(part, dest)
        except OSError as e:
            self._discard(part)
            raise SmbClientError(_("上传 {share}/{rel} 失败(替换目标): {e}").format(
                share=share, rel=rel, e=e)) from e
        return rel

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
        rel = self._rel(share, path)
        base = f"{rel}\\{local_dir.name}" if rel else local_dir.name
        count = 0
        for dirpath, _dirnames, filenames in os.walk(local_dir):
            sub = Path(dirpath).relative_to(local_dir)
            remote_dir = base if str(sub) == "." else \
                f"{base}\\{str(sub).replace(os.sep, chr(92))}"
            self.makedirs(share, remote_dir)
            for fn in filenames:
                if cancel is not None and cancel.is_set():
                    raise TransferCancelled(_("已取消"))
                lp = Path(dirpath) / fn
                cb = None
                if progress:
                    display = str(lp)
                    cb = (lambda done, total, _d=display: progress(_d, done, total))
                self.upload_file(lp, share, f"{remote_dir}\\{fn}",
                                 progress=cb, cancel=cancel)
                count += 1
        return count

    # ---------- 修改类操作 ----------

    def remove(self, share: str, path: str) -> None:
        rel = self._rel(share, path)
        if not rel:
            raise SmbClientError(_("不能删除共享根目录"))
        target = self._resolve(share, rel)
        try:
            if target.is_dir():
                raise SmbClientError(_("删除 {share}/{rel} 失败: 目标是目录").format(
                    share=share, rel=rel))
            target.unlink()
        except SmbClientError:
            raise
        except FileNotFoundError as e:
            raise SmbClientError(_("删除 {share}/{rel} 失败: 路径不存在").format(
                share=share, rel=rel)) from e
        except PermissionError as e:
            raise SmbClientError(_("删除 {share}/{rel} 失败: 访问被拒绝").format(
                share=share, rel=rel)) from e
        except OSError as e:
            raise SmbClientError(_("删除 {share}/{rel} 失败: {e}").format(
                share=share, rel=rel, e=e)) from e

    def rmdir(self, share: str, path: str, recursive: bool = False) -> None:
        rel = self._rel(share, path)
        if not rel:
            raise SmbClientError(_("不能删除共享根目录"))
        if recursive:
            # 后序:先删文件再自底向上删目录(与 SMB 侧同一套顺序)
            levels = list(self.walk(share, rel))
            for sub, _dirs, files in reversed(levels):
                for f in files:
                    self.remove(share, f.path)
                self._rmdir_one(share, sub)
            return
        self._rmdir_one(share, rel)

    def _rmdir_one(self, share: str, rel: str) -> None:
        target = self._resolve(share, rel)
        try:
            target.rmdir()
        except FileNotFoundError as e:
            raise SmbClientError(_("删除目录 {share}/{rel} 失败: 路径不存在").format(
                share=share, rel=rel)) from e
        except NotADirectoryError as e:
            raise SmbClientError(_("删除目录 {share}/{rel} 失败: 目标不是目录").format(
                share=share, rel=rel)) from e
        except PermissionError as e:
            raise SmbClientError(_("删除目录 {share}/{rel} 失败: 访问被拒绝").format(
                share=share, rel=rel)) from e
        except OSError as e:
            if getattr(e, "errno", None) in (39, 41, 66):   # ENOTEMPTY 各平台
                raise SmbClientError(_("删除目录 {share}/{rel} 失败: 目录非空").format(
                    share=share, rel=rel)) from e
            raise SmbClientError(_("删除目录 {share}/{rel} 失败: {e}").format(
                share=share, rel=rel, e=e)) from e

    def rename(self, share: str, old: str, new: str) -> None:
        old_rel = self._rel(share, old)
        new_rel = self._rel(share, new)
        if not old_rel or not new_rel:
            raise SmbClientError(_("重命名的源/目标路径不能为空"))
        src = self._resolve(share, old_rel)
        dst = self._resolve(share, new_rel)
        if dst.exists():
            # 与 SMB 侧的 STATUS_OBJECT_NAME_COLLISION 对齐(不静默覆盖)
            raise SmbClientError(
                _("重命名 {share}/{old_rel} -> {new_rel} 失败: 目标已存在").format(
                    share=share, old_rel=old_rel, new_rel=new_rel))
        try:
            os.rename(src, dst)
        except FileNotFoundError as e:
            raise SmbClientError(
                _("重命名 {share}/{old_rel} -> {new_rel} 失败: 路径不存在").format(
                    share=share, old_rel=old_rel, new_rel=new_rel)) from e
        except PermissionError as e:
            raise SmbClientError(
                _("重命名 {share}/{old_rel} -> {new_rel} 失败: 访问被拒绝(文件可能被占用)").format(
                    share=share, old_rel=old_rel, new_rel=new_rel)
            ) from e
        except OSError as e:
            raise SmbClientError(
                _("重命名 {share}/{old_rel} -> {new_rel} 失败: {e}").format(
                    share=share, old_rel=old_rel, new_rel=new_rel, e=e)) from e

    # ---------- 杂项 ----------

    @staticmethod
    def _discard(part: Path) -> None:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass
