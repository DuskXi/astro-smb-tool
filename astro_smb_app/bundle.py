"""打包后的资源定位。

**`Path(__file__)` 在冻结的包里不再指向源码树。** PyInstaller 会把 Python 模块
塞进一个归档、把数据文件解到 `sys._MEIPASS`(onefile)或放在可执行文件旁边
(onedir);`parents[2]` 那种往上数的写法会直接走到包外面去。

这一层把三样东西的定位收口:

===============  ==========================================================
东西              开发时                       打包后
===============  ==========================================================
`web/sky3d.js`    `astro_smb_app/web/` 下      随包数据,同一相对路径
`web/`(天球资产)  `astro_smb_app/web/`        同上
===============  ==========================================================

**开发路径永远排在前面。** 打包后的目录在开发机上根本不存在,而反过来
(在包里找源码树)会拿到一个恰好同名却是别人的目录 —— 宁可找不到也不要找错。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def frozen() -> bool:
    """是不是跑在 PyInstaller 冻结的包里。"""
    return bool(getattr(sys, "frozen", False))


def bundle_root() -> Path | None:
    """**随包数据**的根;没冻结返回 None。

    onefile 解到一个临时目录,onedir(PyInstaller 6)放在 `_internal/` ——
    两种情况 `sys._MEIPASS` 都会被设上,所以直接用它。
    """
    if not frozen():
        return None
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass)
    return Path(sys.executable).resolve().parent


def install_root() -> Path | None:
    """**可执行文件**所在的目录;没冻结返回 None。

    和 `bundle_root` 不是一回事:PyInstaller 6 的 onedir 把随包数据放进
    `_internal/`,而 `_MEIPASS` 指的正是那个子目录 —— 与可执行文件**差一层**。
    当年就是把这两者当成一回事,打出来的包一启动就说"找不到资源",
    **只有真的运行一次才发现**。(那时找的是 Uno 的 C# 渲染器;Uno 已删,
    但这个"差一层"的坑对任何随包资源都成立,所以这个函数留着。)
    """
    if not frozen():
        return None
    return Path(sys.executable).resolve().parent


def data_file(*parts: str, package_relative: Path | None = None) -> Path | None:
    """找一个随包数据文件。找不到返回 None。

    `package_relative` 是开发时的路径(通常由调用方用 `Path(__file__)` 算出);
    它**排在前面** —— 打包后的目录在开发机上不存在,而反过来在包里找源码树
    可能拿到一个同名却无关的目录。
    """
    if package_relative is not None and package_relative.exists():
        return package_relative
    root = bundle_root()
    if root is not None:
        cand = root.joinpath(*parts)
        if cand.exists():
            return cand
    return None


