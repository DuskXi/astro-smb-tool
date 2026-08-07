"""跨平台的应用数据目录与几个平台差异的收口处。

**Windows 上的路径一个字节都没变** —— 仍然是 ``%LOCALAPPDATA%/AstroSmbTool``。
这点是硬要求:那底下躺着用户的设备记录、日志缓存、巡天底图(8 MB)、
星表(35.6 MB)和 meta.db。换个位置等于让老用户的东西全部凭空消失一次。

其余平台按各自惯例:

===========  ==========================================  ==========================
平台          数据(要备份的)                                缓存(丢了能重下的)
===========  ==========================================  ==========================
Windows      ``%LOCALAPPDATA%/AstroSmbTool``             同左
macOS        ``~/Library/Application Support/AstroSmbTool``  ``~/Library/Caches/AstroSmbTool``
Linux        ``$XDG_DATA_HOME/AstroSmbTool``             ``$XDG_CACHE_HOME/AstroSmbTool``
===========  ==========================================  ==========================

Windows 不分数据与缓存,是因为它本来就没分过 —— 分了反而会把现有的
``cache/`` 挪走。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

APP_NAME = "AstroSmbTool"


def _home() -> Path:
    try:
        return Path.home()
    except (RuntimeError, OSError):      # 无 HOME 的怪环境
        return Path(os.getcwd())


def data_dir() -> Path:
    """要保留的东西(设备记录、站点配置、meta.db)。"""
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or _home())
    elif sys.platform == "darwin":
        base = _home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME")
                    or (_home() / ".local" / "share"))
    return base / APP_NAME


def cache_root() -> Path:
    """丢了能重新下载的东西(预览缓存、日志原文、巡天底图、星表)。

    Windows 上**故意与 data_dir 相同** —— 它一直就是一个目录,拆开会把现有的
    ``cache/`` 挪到别处,等于让用户重下一遍几十兆。
    """
    if sys.platform == "win32":
        return data_dir()
    if sys.platform == "darwin":
        return _home() / "Library" / "Caches" / APP_NAME
    return Path(os.environ.get("XDG_CACHE_HOME") or (_home() / ".cache")) / APP_NAME


def sub(name: str, *, cache: bool = True) -> Path:
    """取一个子目录并建好。``cache=False`` 的放数据目录。"""
    d = (cache_root() if cache else data_dir()) / name
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass          # 只读盘不该让程序起不来 —— 调用方各自兜底
    return d


#: 子进程创建标志:Windows 上不弹黑窗,其余平台没有这回事
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def curl_argv(*args: str) -> list[str]:
    """curl 命令行。**可执行名按平台取** —— Windows 上是 ``curl.exe``
    (Win10+ 自带,走 Schannel,证书链完整),其余平台是 ``curl``。

    这条兜底链存在的原因见 `skymap` / `catalog`:uv 的独立构建 Python 在
    Windows 上不挂系统证书库,OpenSSL 也不做 AIA 补链,urllib 会因缺链而失败。
    macOS/Linux 的 Python 本来就用系统或 certifi 的根证书,这条路基本用不上,
    但留着不亏。
    """
    exe = "curl.exe" if sys.platform == "win32" else "curl"
    return [exe, *args]


def is_windows() -> bool:
    return sys.platform == "win32"
