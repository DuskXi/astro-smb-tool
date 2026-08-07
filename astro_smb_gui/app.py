"""Astro SMB Tool — WinUI3 前端入口。

win32more 是延迟绑定,导入不触发 DLL 加载;但 Windows App Runtime 的
引导(appsdk.initialize)必须发生在任何 WinRT 激活之前,所以入口先
initialize 再拉起窗口模块。
"""

from __future__ import annotations


def _require_toolkit() -> None:
    """win32more 不在**必装**依赖里 —— 它只在 Windows 上有意义。

    代价是这条入口点在普通安装下直接 `ModuleNotFoundError`,而那句原始
    报错对用户毫无意义(他装的明明是这个包)。翻译成一句能照着做的话。
    Qt 那条入口点同理,见 `astro_smb_qt/__main__.py`。
    """
    import importlib.util
    import sys

    if importlib.util.find_spec("win32more") is not None:
        return
    sys.exit(
        "astro-smb-tool-gui 需要 win32more,而它只在 Windows 上有意义,"
        "所以不在必装依赖里。\n"
        "    pip install 'astro-smb-tool[winui]'\n"
        "从源码跑:uv run --extra winui astro-smb-tool-gui\n"
        "还需要 Windows 10 21H2+ 与 Windows App Runtime 2.3。\n"
        "跨平台的那套界面是 astro-smb-tool-qt(装 [qt])。\n"
        "\n"
        "astro-smb-tool-gui needs win32more, which is Windows-only and\n"
        "therefore not a required dependency:\n"
        "    pip install 'astro-smb-tool[winui]'")


def main() -> None:
    _require_toolkit()

    from win32more import appsdk

    appsdk.initialize()

    from win32more.winui3 import XamlApplication

    from astro_smb_gui._window import App

    XamlApplication.Start(lambda: App())


if __name__ == "__main__":
    main()
