# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 规格 —— 打 Qt 那套界面(`astro_smb_qt`)。

**打的是 Qt 那一套,不是 WinUI3。** 后者要 Windows App Runtime 单独安装,
不是解开就能跑的东西;它留给从源码跑的人(`uv run --extra winui`)。

## 随包数据

`.py` 之外的三样东西 PyInstaller 自己找不到 —— 它们是被 `Path(...)` 在
运行时打开的,不是 import 的:

* `astro_smb/locale/**/*.mo`   翻译。少了它界面永远是中文,**而且不报错**;
* `astro_smb_app/web/*`        3D 天球页的 three.js 资产;
* `astro_smb_gui/*.xaml`       **不打**。那是另一套前端的界面本体。

放进包的相对路径要和源码树里**一模一样** —— `astro_smb_app.bundle` 与
`views.sky3d._pkg_web_dir()` 是按 `astro_smb_app/web` 这个相对路径找的。

## 为什么 onedir 不是 onefile

onefile 每次启动把两百多兆解到临时目录,冷启要十几秒;而这里面最大的一块
是 QtWebEngine(PySide6 完整包 665 MB,其中 208 MB 是它)。onedir 解压一次
装好就完事。

## `_internal/` 那一层

PyInstaller 6 的 onedir 把随包数据放进 `_internal/`,而 `sys._MEIPASS` 指的
正是那个子目录 —— 和可执行文件**差一层**。`astro_smb_app.bundle` 里
`bundle_root()` / `install_root()` 分开两个函数就是为了这个;当年把两者当成
一回事,打出来的包一启动就说"找不到资源",**只有真的运行一次才发现**。
"""
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

ROOT = Path(SPECPATH).parent          # noqa: F821 - SPECPATH 由 PyInstaller 注入

datas = [
    # 三元组的第二项是**包内的目标目录**,要和源码树里的相对路径一致。
    (str(ROOT / "astro_smb_app" / "web"), "astro_smb_app/web"),
]
# `.mo` 逐个收,连目录结构一起 —— gettext 按 `<lang>/LC_MESSAGES/<domain>.mo`
# 找,少一层就找不到,而找不到的表现是"英文界面显示中文",不是报错。
for mo in sorted((ROOT / "astro_smb" / "locale").rglob("*.mo")):
    datas.append((str(mo), str(mo.parent.relative_to(ROOT)).replace("\\", "/")))

# 天球页要 QtWebEngine 的运行时资产(`icudtl.dat`、`qtwebengine_resources*.pak`、
# 各语言的 `.pak`)。PySide6 的钩子会带上,这里显式再收一遍兜底 —— 少了它
# WebEngine 起不来,而症状是**天球页一片空白**,控制台什么都不说。
datas += collect_data_files("PySide6", subdir="Qt/resources", include_py_files=False)

hiddenimports = [
    # 九个页面在 `pages/__init__.py` 里是静态 import 的,PyInstaller 找得到。
    # 这几个是**运行时才按名字取**的,得手写:
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebChannel",
]

excludes = [
    "tkinter",                 # 没用到,但 CPython 默认带着
    "win32more",               # 另一套前端的绑定,不进这个包
    "pytest", "_pytest", "pytest_xdist", "execnet",
    "PySide6.QtQml", "PySide6.QtQuick", "PySide6.Qt3DCore",
    "PySide6.QtCharts", "PySide6.QtDataVisualization",
    "PySide6.QtMultimedia", "PySide6.QtDesigner", "PySide6.QtHelp",
    "PySide6.QtSql", "PySide6.QtTest", "PySide6.QtBluetooth",
]

a = Analysis(                                                    # noqa: F821
    [str(ROOT / "astro_smb_qt" / "__main__.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)
pyz = PYZ(a.pure)                                                # noqa: F821

exe = EXE(                                                       # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="astro-smb-tool",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                 # UPX 压 Qt 的 .so/.dylib 会让它们加载不了
    # **不是 windowed。** 这个程序有 CLI 参数(`--host`/`--page`/`--seconds`),
    # 而且缺依赖时靠 stderr 说人话;做成无控制台的话那些话没人看得见。
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=sys.platform == "darwin",   # mac 上双击/拖放传参
    target_arch=None,          # 交叉架构见下面的说明
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(                                                  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="astro-smb-tool",
)
