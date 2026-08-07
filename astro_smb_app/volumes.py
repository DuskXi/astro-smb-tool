"""本地卷枚举 + ZWO 存储卡自动识别。

**纯数据层**:只依赖标准库(无 WinRT、无 impacket),离线单测可直接 import。

用途:ASIAIR 的卡常被拔出来直接插在电脑上(实测 ZWO Pro 盒子挂在 ``E:\\``),
连接面板要能"看见"这些盘,并且**自动认出**哪一个是 ASIAIR 卡,不用用户手填路径。

三个入口:

- :func:`list_volumes` —— 枚举当前可用的卷(Windows/Linux/macOS 分派);
- :func:`zwo_signature` —— 一个根目录像不像 ZWO 卡(命中分数 + 命中的特征目录);
- :func:`autodetect_zwo` —— 从卷列表里挑出可以**自动**加为设备的那些。

**全部函数对权限/IO 异常静默容错**(返回空列表/低分):一个坏盘、一个空读卡器、
一个断开的网络驱动器,都绝不能让 GUI 起不来 —— 这一条比"信息完整"重要。

关于阈值(为什么是 3 和 4):实测 Pro 盒子的卡根有 8 个特征目录 + 1 个非特征目录
(``batch_stack_tmp``)+ 3 个系统垃圾目录;Plus 盒子经 SMB 只见 4 个特征目录。
取"≥3 个特征目录 **且** 非特征顶层条目 ≤4",既能认出两代盒子,又不会把
"随便放了几个同名文件夹的大硬盘"误判成 ASIAIR 卡。
"""

from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from astro_smb.i18n import N_, gettext as _

# 卷类型
KIND_FIXED = "fixed"
KIND_REMOVABLE = "removable"
KIND_NETWORK = "network"

#: ZWO 卡的特征顶层目录(实测:Pro 盒子本地卡有全部 8 个;
#: Plus 盒子经 SMB 只有 Autorun/Plan/Preview/log)
ZWO_DIRS: tuple[str, ...] = (
    "Autorun", "Plan", "Preview", "log",
    "Live", "Video", "Stacked", "GuidingDarkLibrary",
)
_ZWO_SET = {d.casefold() for d in ZWO_DIRS}

#: 各平台的系统垃圾目录 —— 判定时必须忽略,否则一张干净的 ZWO 卡也会因为
#: "System Volume Information / .Spotlight-V100 / .fseventsd" 被算成"杂物很多"
JUNK_NAMES = {
    "system volume information", "$recycle.bin", "recycler", "found.000",
    ".spotlight-v100", ".fseventsd", ".trashes", ".temporaryitems",
    "lost+found", "desktop.ini", "autorun.inf", "thumbs.db",
}

#: 自动识别阈值
MIN_HITS = 3
MAX_OTHERS = 4


@dataclass(frozen=True)
class VolumeInfo:
    """一个本地卷。

    注意与 :class:`astro_smb.client.VolumeInfo`(只有 total/free 的容量数据类)
    同名但不同物 —— 那个描述"某个共享所在卷的容量",这个描述"电脑上的一个盘"。
    需要同时 import 时请用 ``from astro_smb_app import volumes`` 加模块名限定。
    """

    path: Path
    label: str
    kind: str           # 'fixed' | 'removable' | 'network'
    total: int = 0
    free: int = 0
    fs: str = ""        # 文件系统名(NTFS/exFAT/FAT32/…);取不到为空串

    @property
    def used(self) -> int:
        return max(0, self.total - self.free)

    @property
    def percent(self) -> float:
        return (self.used / self.total * 100) if self.total else 0.0

    @property
    def drive(self) -> str:
        """盘符(``E:``)或挂载点名;没有则退回完整路径。"""
        d = os.path.splitdrive(str(self.path))[0].rstrip("\\/")
        return d or self.path.name or str(self.path)

    @property
    def display(self) -> str:
        """给下拉/卡片用的一行标题:``ASIAIR (E:)`` / ``E:``。"""
        return f"{self.label} ({self.drive})" if self.label else self.drive

    @property
    def kind_text(self) -> str:
        return {KIND_REMOVABLE: _("可移动磁盘"), KIND_NETWORK: _("网络驱动器")}.get(
            self.kind, _("本地磁盘"))

    @property
    def fs_text(self) -> str:
        """文件系统名;取不到显示为破折号(设备页 KV 行不留空值)。"""
        return self.fs or "—"


# ---------------------------------------------------------------- 枚举

def list_volumes() -> list[VolumeInfo]:
    """枚举当前可用的卷。任何平台上都不会抛异常,最差返回空列表。"""
    try:
        if os.name == "nt":
            return _windows_volumes()
        if sys.platform == "darwin":
            return _macos_volumes()
        return _linux_volumes()
    except Exception:       # noqa: BLE001 —— 枚举失败绝不能拖垮 GUI
        return []


def _usage(path: Path) -> tuple[int, int] | None:
    """(total, free);读不到返回 None(空读卡器/断开的网络盘)。"""
    try:
        u = shutil.disk_usage(str(path))
        return (int(u.total), int(u.free))
    except (OSError, ValueError):
        return None


def _windows_volumes() -> list[VolumeInfo]:
    """Windows:GetLogicalDrives + GetDriveTypeW + GetVolumeInformationW。

    探测空读卡器会弹"请插入磁盘"对话框 —— 所以整段用
    ``SetThreadErrorMode(SEM_FAILCRITICALERRORS)`` 包住(仅影响本线程)。
    """
    import ctypes
    from ctypes import wintypes

    SEM_FAILCRITICALERRORS = 0x0001
    SEM_NOOPENFILEERRORBOX = 0x8000
    DRIVE_REMOVABLE, DRIVE_FIXED, DRIVE_REMOTE, DRIVE_RAMDISK = 2, 3, 4, 6
    kind_map = {DRIVE_REMOVABLE: KIND_REMOVABLE, DRIVE_FIXED: KIND_FIXED,
                DRIVE_REMOTE: KIND_NETWORK, DRIVE_RAMDISK: KIND_FIXED}

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetLogicalDrives.restype = wintypes.DWORD
    k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    k32.GetDriveTypeW.restype = wintypes.UINT

    old_mode = wintypes.DWORD(0)
    guarded = False
    try:
        guarded = bool(k32.SetThreadErrorMode(
            SEM_FAILCRITICALERRORS | SEM_NOOPENFILEERRORBOX,
            ctypes.byref(old_mode)))
    except Exception:       # noqa: BLE001 —— 老系统没有这个 API,忽略
        guarded = False

    out: list[VolumeInfo] = []
    try:
        mask = int(k32.GetLogicalDrives())
        for i in range(26):
            if not (mask >> i) & 1:
                continue
            root = f"{chr(65 + i)}:\\"
            try:
                dtype = int(k32.GetDriveTypeW(root))
            except Exception:   # noqa: BLE001
                continue
            kind = kind_map.get(dtype)
            if kind is None:        # 光驱/未知/无根目录:与本项目无关,跳过
                continue
            usage = _usage(Path(root))
            if usage is None:       # 读卡器里没卡
                continue
            label, fs = _win_volume_info(k32, root)
            out.append(VolumeInfo(path=Path(root), label=label, kind=kind,
                                  total=usage[0], free=usage[1], fs=fs))
    finally:
        if guarded:
            try:
                k32.SetThreadErrorMode(old_mode, None)
            except Exception:       # noqa: BLE001
                pass
    return out


def _win_volume_info(k32, root: str) -> tuple[str, str]:
    """(卷标, 文件系统名);取不到的那一项返回空串。

    一次 ``GetVolumeInformationW`` 同时拿两样 —— 文件系统名(NTFS/exFAT/FAT32)
    对 ASIAIR 卡有实际意义:exFAT 的卡是原厂格式,被重新格成 NTFS 的卡在
    盒子里可能读不出来,设备页把它显示出来供用户自查。
    """
    import ctypes

    try:
        buf = ctypes.create_unicode_buffer(261)
        fsbuf = ctypes.create_unicode_buffer(261)
        ok = k32.GetVolumeInformationW(
            ctypes.c_wchar_p(root), buf, ctypes.sizeof(buf) // 2,
            None, None, None, fsbuf, ctypes.sizeof(fsbuf) // 2)
        if not ok:
            return ("", "")
        return (buf.value.strip(), fsbuf.value.strip())
    except Exception:       # noqa: BLE001
        return ("", "")


def _win_label(k32, root: str) -> str:
    """卷标;取不到返回空串(``_win_volume_info`` 的兼容包装)。"""
    return _win_volume_info(k32, root)[0]


def _linux_volumes() -> list[VolumeInfo]:
    """Linux:/proc/mounts 里挂在 /media、/run/media、/mnt 下的文件系统。"""
    prefixes = ("/media/", "/run/media/", "/mnt/")
    net_fs = {"cifs", "smb3", "nfs", "nfs4", "sshfs", "fuse.sshfs"}
    out: list[VolumeInfo] = []
    seen: set[str] = set()
    try:
        with open("/proc/mounts", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError:
        return []
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        # /proc/mounts 用八进制转义空格等字符
        mp = parts[1].replace("\\040", " ").replace("\\011", "\t")
        fstype = parts[2]
        if not mp.startswith(prefixes) or mp in seen:
            continue
        seen.add(mp)
        usage = _usage(Path(mp))
        if usage is None:
            continue
        kind = KIND_NETWORK if fstype in net_fs else (
            KIND_FIXED if mp.startswith("/mnt/") else KIND_REMOVABLE)
        out.append(VolumeInfo(path=Path(mp), label=Path(mp).name, kind=kind,
                              total=usage[0], free=usage[1], fs=fstype))
    return out


def _macos_volumes() -> list[VolumeInfo]:
    """macOS:/Volumes 下的挂载点。"""
    out: list[VolumeInfo] = []
    base = Path("/Volumes")
    try:
        children = sorted(base.iterdir())
    except OSError:
        return []
    for p in children:
        try:
            if not p.is_dir():
                continue
        except OSError:
            continue
        usage = _usage(p)
        if usage is None:
            continue
        out.append(VolumeInfo(path=p, label=p.name, kind=KIND_REMOVABLE,
                              total=usage[0], free=usage[1]))
    return out


# ---------------------------------------------------------------- ZWO 识别

def is_junk(name: str) -> bool:
    """系统垃圾/隐藏项(判定时一律忽略)。"""
    return name.startswith(".") or name.casefold() in JUNK_NAMES


def scan_root(root: str | Path) -> tuple[list[str], list[str]]:
    """扫一层根目录 → (命中的特征目录名, 其余非垃圾条目名)。

    只扫一层、只用 scandir,几毫秒级;任何 IO/权限错误返回 ``([], [])``。
    """
    hits: list[str] = []
    others: list[str] = []
    try:
        with os.scandir(str(root)) as it:
            for de in it:
                name = de.name
                if is_junk(name):
                    continue
                try:
                    is_dir = de.is_dir()
                except OSError:
                    is_dir = False
                if is_dir and name.casefold() in _ZWO_SET:
                    hits.append(name)
                else:
                    others.append(name)
    except (OSError, ValueError):
        return ([], [])
    return (hits, others)


def zwo_signature(root: str | Path) -> tuple[int, list[str]]:
    """一个根目录像不像 ZWO 卡 → (命中分数, 命中的特征目录名)。

    分数就是命中的特征目录个数(0~8)。不看非特征条目 —— 那个由
    :func:`autodetect_zwo` 单独把关(见模块 docstring 的阈值说明)。
    """
    hits, _others = scan_root(root)
    return (len(hits), hits)


def autodetect_zwo(volumes, min_hits: int = MIN_HITS,
                   max_others: int = MAX_OTHERS) -> list[Path]:
    """从卷列表里挑出可以**自动**加为设备的 ZWO 卡根路径。

    判据:命中 ≥ ``min_hits`` 个特征目录 **且** 非特征顶层条目 ≤ ``max_others``。
    不满足就不自动加(用户仍可手动指定路径)—— 宁可漏,不可把用户的大硬盘
    当成 ASIAIR 卡挂上去。

    ``volumes`` 可以是 :class:`VolumeInfo` 列表,也可以直接是路径列表。
    """
    found: list[Path] = []
    for vol in volumes or ():
        path = getattr(vol, "path", vol)
        try:
            path = Path(path)
        except (TypeError, ValueError):
            continue
        hits, others = scan_root(path)
        if len(hits) >= min_hits and len(others) <= max_others:
            found.append(path)
    return found


def describe_zwo(root: str | Path) -> str:
    """给 UI 用的一行说明:``ZWO 卡 · 命中 8 项(Autorun/Plan/Preview…)``。

    没命中返回空串,调用方据此决定是否显示徽章。
    """
    hits, _others = scan_root(root)
    if not hits:
        return ""
    order = {d.casefold(): i for i, d in enumerate(ZWO_DIRS)}
    shown = sorted(hits, key=lambda h: order.get(h.casefold(), 99))[:3]
    tail = "…" if len(hits) > len(shown) else ""
    return _("ZWO 卡 · 命中 {0} 项({1}{tail})").format(len(hits), '/'.join(shown), tail=tail)
