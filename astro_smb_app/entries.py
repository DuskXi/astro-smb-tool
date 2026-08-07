"""与界面无关的条目工具:扩展名分类、排序、本地文件名去重、本地路径判定。

这些**原本住在 `astro_smb_gui/_common.py`**,与那里的 WinRT 相关工具混在一起。
新前端同样要按扩展名分类、按同一套规则排序 —— 而它不该为了这几个纯函数去
import 一个已冻结的 WinUI 包。

留在 `_common.py` 的是真正与 WinUI 绑定的:`unbox_str`(WinRT 装箱串)、
`_spin`(等 IAsyncOperation)、`file_uri`(WinRT Uri)、`glyph_for`(Segoe MDL2
私用区码位)、以及 `rect/line/poly_fragment` 那套 **XAML 序列化器** ——
新前端要的是它们**输入的元组**,不是 XAML,所以那几个不搬。
"""
from __future__ import annotations

import os
from pathlib import Path

from astro_smb.client import RemoteEntry
from astro_smb.i18n import N_, gettext as _
from astro_smb.util import sanitize_local_name

FITS_EXTS = {".fit", ".fits", ".fts"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff", ".webp"}
TEXT_EXTS = {".txt", ".log", ".json", ".ini", ".cfg", ".md", ".csv"}

# 扩展名 → 大类。**表里是 msgid(只标记不翻)**,取色/查表用它,显示才 `_()`。
_EXT_CATEGORY = {
    **{e: N_("图像") for e in FITS_EXTS},
    **{e: N_("缩略图/图片") for e in IMAGE_EXTS},
    **{e: N_("文本/日志") for e in TEXT_EXTS},
}


def ext_category_id(entry: RemoteEntry) -> str:
    """分类的**稳定身份**(msgid,与显示语言无关)。

    **查表和取色一律用这个,不要用 `ext_category`。** 那一支会被翻译,
    而它的返回值现在同时是三处的键:行首符号表、treemap 调色板
    (`crc32(类名) % 8`)、以及明细里的"种类"。一翻译:符号全变兜底方块、
    每种文件的颜色整体洗牌 —— 都不报错,只是看起来不对。

    身份用中文拼写不是将就:本项目的 msgid 本来就是中文原文(见
    `astro_smb.i18n` 的说明),所以"msgid 即身份"是一致的,
    而且**保住了现有配色**(换成 ASCII 键会让 crc32 落到别的色号上,
    §6 那批验收截图就对不上了)。
    """
    if entry.is_dir:
        return N_("文件夹")
    ext = os.path.splitext(entry.name)[1].lower()
    if not ext:
        return N_("无扩展名")
    # **这里绝不能 `_()`。** 这一支是**身份**:它的返回值同时是行首符号的键
    # 与 treemap 的色号(`crc32(类名) % 8`)。一翻译,符号全变兜底方块、
    # 每种文件的颜色整体洗牌 —— 而且不报错。
    # (2026-08-05 最后一轮机械清扫真的把它包上了,伪语言跑出来
    #  `⟦XYZ⟦ 文件⟧` 才发现。)
    return _EXT_CATEGORY.get(ext) or ext[1:].upper() + N_(" 文件")


def ext_category(entry: RemoteEntry) -> str:
    """分类的**显示文本**(会随语言变)。只用来显示,不要拿去当键。"""
    cid = ext_category_id(entry)
    if cid in _EXT_CATEGORY.values() or cid in (N_("文件夹"), N_("无扩展名")):
        return _(cid)
    # "XYZ 文件" 是按扩展名现拼的,进不了词表 —— 只翻可翻的那半截
    return _("{ext} 文件").format(ext=cid[:-3].strip())


def sort_key(idx: int):
    """排序下拉索引 → (key 函数, 是否倒序)。见 browser.xaml 的 SortBox。"""
    keys = [
        (lambda e: e.name.lower(), False),
        (lambda e: e.name.lower(), True),
        (lambda e: e.size, False),
        (lambda e: e.size, True),
        (lambda e: e.mtime, False),
        (lambda e: e.mtime, True),
        # 按扩展名:先类别再名字
        (lambda e: (os.path.splitext(e.name)[1].lower(), e.name.lower()), False),
    ]
    return keys[idx] if 0 <= idx < len(keys) else keys[0]


def sorted_entries(entries: list[RemoteEntry], idx: int) -> list[RemoteEntry]:
    """目录永远排在文件前面,组内按所选规则排序。"""
    key, rev = sort_key(idx)
    dirs = sorted([e for e in entries if e.is_dir], key=key, reverse=rev)
    files = sorted([e for e in entries if not e.is_dir], key=key, reverse=rev)
    return dirs + files


def unique_local(target: Path, name: str, used: set[str]) -> Path:
    """在 target 目录内为 name 找不冲突的文件名(避开 used 与磁盘已有)。"""
    name = sanitize_local_name(name)
    base, ext = os.path.splitext(name)
    cand, n = name, 1
    while cand.casefold() in used or (target / cand).exists():
        cand = f"{base} ({n}){ext}"
        n += 1
    used.add(cand.casefold())
    return target / cand


def looks_like_local_path(host: str) -> bool:
    """'E:\' / 'E:/' / '/media/xxx' 这类是本地路径,不是 SMB 主机名。

    放在共享模块里:shell 用它决定建哪种后端与状态文案,扫描页用它判断
    "当前设备根本不在局域网上"。扫描页不能反向 import _window(循环)。
    """
    h = (host or "").strip()
    if not h:
        return False
    if len(h) >= 2 and h[1] == ":":         # 盘符
        return True
    return h.startswith("/") or h.startswith("\\?\\")
