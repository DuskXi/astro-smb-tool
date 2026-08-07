"""通用小工具:大小/时间格式化、Windows 文件名清洗。"""

from __future__ import annotations

import re
from datetime import datetime
from astro_smb.i18n import gettext as _

_SIZE_UNITS = {"": 1, "B": 1, "K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}

# Windows 文件名里不允许的字符(远程 Samba 名字可能包含)
_INVALID_WIN_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def human_size(n: int | float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(n)} B"
            return f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def parse_size(text: str) -> int:
    """'10M' / '1.5G' / '2048' -> 字节数。"""
    m = re.fullmatch(r"\s*([\d.]+)\s*([KMGTB]?)I?B?\s*", text, re.IGNORECASE)
    if not m:
        raise ValueError(_("无法解析大小: {text!r}").format(text=text))
    return int(float(m.group(1)) * _SIZE_UNITS[m.group(2).upper()])


def format_mtime(epoch: float) -> str:
    if epoch <= 0:
        return "-"
    try:
        return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")
    except (OSError, OverflowError, ValueError):
        return "-"


def format_duration(seconds: float) -> str:
    if seconds < 0 or seconds != seconds:  # NaN
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def sanitize_local_name(name: str) -> str:
    """把远程文件名清洗成 Windows 本地合法文件名。"""
    cleaned = _INVALID_WIN_CHARS.sub("_", name).rstrip(" .")
    return cleaned or "_"
