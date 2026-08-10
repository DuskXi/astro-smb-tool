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
import os
import platform
import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files

#: mac 的目标架构。
#:
#: **加这个没能把包变小 —— 那条推断是错的,记在这儿免得有人再试一遍。**
#: 原以为 PySide6 的 mac 轮子是 universal2(一份文件里 x86_64 + arm64 都有),
#: 给了 `target_arch` PyInstaller 就会 `lipo -thin` 掉一半。实测:x86 Mac 上
#: 加之前 1023 MB,加之后**还是 1023 MB**。回看构建日志,`EXE target arch:
#: x86_64` 在**两次里都有** —— PyInstaller 本来就按当前架构走了,这个参数
#: 对它是个空操作。真正的大头在哪儿还没查清(QtWebEngine 一个人就两百多兆)。
#:
#: 显式写着仍然有意义:换台 arm64 mac 打包时它是 arm64,不会因为环境里
#: 混进什么而打错架构。某个依赖缺目标架构时 PyInstaller 会当场报错,
#: 不会悄悄打出一个跑不起来的包。
_TARGET_ARCH = None
if sys.platform == "darwin":
    _TARGET_ARCH = "arm64" if platform.machine() == "arm64" else "x86_64"

#: mac 代码签名身份。**现在是空的** —— 没有 Apple 开发者证书,包是 ad-hoc
#: 签的(PyInstaller 自己那一下),经过网络传输后会被 Gatekeeper 拦。
#:
#: 拿到 "Developer ID Application" 证书之后,**只要设这个环境变量**:
#:
#:     export ASTRO_SMB_CODESIGN_ID="Developer ID Application: 名字 (TEAMID)"
#:
#: PyInstaller 会用它把收集到的**每一个** dylib/framework 都签一遍 ——
#: 漏签任何一个,公证都会被退回。签完还要 notarize + staple,那两步在
#: docs/DEVELOPMENT.md §14 里。
#:
#: 硬化运行时(公证的前提)需要 entitlements:Qt/Chromium 要 JIT,
#: PyInstaller 装载的 dylib 不是同一 team 签的、要关掉 library validation。
_CODESIGN_ID = os.environ.get("ASTRO_SMB_CODESIGN_ID") or None
_ENTITLEMENTS = os.environ.get("ASTRO_SMB_ENTITLEMENTS") or None

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
# ------------------------------------------------------------------ 瘦身
#
# **量出来再删,别猜。** 实测(win-x64)PySide6 占 437 MB / 全包 505 MB,
# 其中前三名:
#
#     195 MB  Qt6WebEngineCore.dll          ← 天球页要它,动不了
#      72 MB  qtwebengine_devtools_resources.debug.pak
#      51 MB  translations/                 ← Qt 自己的 ~50 种语言
#
# 后两样是**纯浪费**:没人会在天球页上开 Chrome 开发者工具,而我们只发
# 中文和英文两种界面语言。删掉约 130 MB,占全包四分之一。
#
# **删完必须真开一次天球页** —— 少一个 `.pak`,WebEngine 的表现是那一页
# 空白,控制台一个字都不说(`--smoke` 里那次窗口启动覆盖不到它)。

#: 我们发的界面语言。Qt 自己的 `.qm` 与 WebEngine 的 locale `.pak` 只留这些。
KEEP_LOCALES = ("en", "en-US", "en_US", "zh", "zh-CN", "zh_CN")


def _drop(dest: str) -> bool:
    """这份随包数据要不要扔掉。``dest`` 是它在包里的相对路径。"""
    p = dest.replace("\\", "/")
    name = p.rsplit("/", 1)[-1]

    # Chromium 开发者工具的资源。**`.debug.pak` 一个就 72 MB。**
    if name.startswith("qtwebengine_devtools_resources"):
        return True

    # WebEngine 的界面语言包(`qtwebengine_locales/xx.pak`)
    if "qtwebengine_locales/" in p and name.endswith(".pak"):
        return name[:-4] not in KEEP_LOCALES

    # Qt 自己的翻译(`translations/qtbase_de.qm` 之类)
    if "/translations/" in f"/{p}" and name.endswith(".qm"):
        stem = name[:-3]
        lang = stem.split("_", 1)[1] if "_" in stem else stem
        return lang not in KEEP_LOCALES

    return False


_before = len(a.datas)                                          # noqa: F821
a.datas = [t for t in a.datas if not _drop(t[0])]               # noqa: F821
print(f"[spec] 随包数据裁掉 {_before - len(a.datas)} 项"          # noqa: F821
      f"(DevTools 资源 + 用不到的语言包)")

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
    target_arch=_TARGET_ARCH,  # mac:瘦成本机架构,见文件头
    codesign_identity=_CODESIGN_ID,
    entitlements_file=_ENTITLEMENTS,
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
