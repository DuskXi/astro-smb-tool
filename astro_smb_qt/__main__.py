r"""入口::

    uv run --with pyside6 python -m astro_smb_qt --host 192.0.2.227

不给 ``--host`` 时按这个顺序找设备:``ASTRO_SMB_HOST`` > ``devices.json`` 里
上次连过的 > **不猜**(直接去扫描页)。设备是 DHCP 的,写死的 IP 对新用户
永远是错的。

``--host`` 既收 SMB 地址,也收**本地目录**(ZWO 卡直插电脑,或把卡的内容拷到
本机)—— 后者是正式支持的设备类型,种类由 ``devices._looks_local`` 自动认::

    python -m astro_smb_qt --host 192.0.2.227
    python -m astro_smb_qt --host "D:\ASIAIR\EMMC Images"
"""
from __future__ import annotations

import argparse
import logging
import sys


def _require_toolkit() -> None:
    """PySide6 不在**必装**依赖里 —— 只用 CLI 的人不该被拖去下一百多兆。

    代价是这条入口点在普通安装下会直接 `ModuleNotFoundError`,而那句
    原始报错对用户毫无意义(他装的明明是这个包)。所以在这里把它翻译成
    一句能照着做的话。

    **不用 try/except 包住模块顶层的 import** —— 那会把别处真正的
    `ImportError`(比如 Linux 上缺 libGL)也一并说成"你没装 PySide6",
    而那是另一回事,解法也完全不同。
    """
    import importlib.util
    import sys

    if importlib.util.find_spec("PySide6") is not None:
        return
    sys.exit(
        "astro-smb-tool-qt 需要 PySide6,而它不在必装依赖里"
        "(只用命令行的人不必下它)。\n"
        "    pip install 'astro-smb-tool[qt]'\n"
        "    uv pip install 'astro-smb-tool[qt]'\n"
        "从源码跑:uv run --extra qt astro-smb-tool-qt\n"
        "\n"
        "astro-smb-tool-qt needs PySide6, which is not a required dependency\n"
        "(command-line users should not have to download it):\n"
        "    pip install 'astro-smb-tool[qt]'")


_require_toolkit()


from astro_smb_qt import theme
from astro_smb_qt.pages import PAGE_CLASSES
from astro_smb.i18n import gettext as _


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="python -m astro_smb_qt",
        description=_("Astro SMB Tool — PySide6 界面"))
    ap.add_argument("--host", default="",
                    help=_('SMB 地址(192.0.2.227)或本地目录(D:\\ASIAIR)'))
    ap.add_argument("--page", default="browse", choices=sorted(PAGE_CLASSES),
                    help=_("启动页"))
    ap.add_argument("--theme", default=theme.MODE_NORMAL, choices=list(theme.MODES),
                    help=_("配色:normal 常规 / red 红光(夜间不破坏暗适应)"))
    ap.add_argument("--seconds", type=float, default=0.0,
                    help=_("N 秒后自动关窗 —— 截图脚本必须给它,不然每跑一次就泄漏一个进程"))
    ap.add_argument("--browse", default="",
                    help=_('连上后直达这个目录("共享名/相对路径")—— 验收/截图用'))
    ap.add_argument("--select", default="", metavar="N|.fit",
                    help=_("列完目录自动选中:整数=第 N 行,`fit`=第一张 .fit"))
    ap.add_argument("--auto", action="store_true",
                    help=_("连上后自动触发本页的主动作(扫描/开扫)—— 截图用"))
    ap.add_argument("--shot", default="",
                    help=_("配合 --seconds:退出前把窗口截到这个 png"))
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s")

    from PySide6.QtWidgets import QApplication

    # **语言要在建界面之前定下来。** 控件上的文案是建控件那一刻翻好烤进去的,
    # 之后再 `set_language()` 已经建好的那些不会自己变。
    # (`ASTRO_SMB_LANG` 仍然优先 —— 见 `i18n.detect_language`。)
    from astro_smb_app import settings

    settings.apply_saved_language()

    app = QApplication(sys.argv[:1])
    app.setApplicationName("Astro SMB Tool")
    theme.set_mode(args.theme)
    theme.apply(app)

    from astro_smb_qt.shell import Shell

    win = Shell(host=args.host, page=args.page)
    win.show()

    if args.browse or args.select:
        _arm_browse(win, args.browse, args.select)
    if args.auto:
        _arm_auto(win, args.page)
    if args.seconds > 0:
        _arm_autoclose(app, win, args.seconds, args.shot)
    return app.exec()


#: 页 → 主动作。空间/扫描两页要用户点一下才有内容,截图时得替他点。
AUTO_ACTIONS = {"space": "rescan", "scan": "toggle",
                "transfers": "demo_queue", "browse": "demo_select",
                # 足迹得等日志读完、夜次选好才有意义,所以页面自己排一拍
                "sky": "demo_footprints"}


def _arm_browse(win, target: str, select: str) -> None:
    """连上后直达某目录并选中一行 —— 只为验收与截图。

    没有这个钩子的话,"详情面板里高度角显示对不对"这种问题每验一次都要
    人手点四五下,而截图脚本根本点不了。另外两套前端各有一个同名的。
    """
    from PySide6.QtCore import QTimer

    from astro_smb.client import split_remote_path

    def fire():
        page = win.page("browse")
        if target:
            share, path = split_remote_path(target)
            win.select_page("browse")
            page.open_path(share, path)
        if select:
            QTimer.singleShot(1800, lambda: _pick(page, select))

    def _pick(page, select: str) -> None:
        keys = page.table.keys()
        if not keys:
            return
        if select.strip().lower() in ("fit", ".fit", "fits"):
            hit = next((k for k in keys if k.lower().endswith(".fit")), keys[0])
        else:
            try:
                hit = keys[min(max(0, int(select)), len(keys) - 1)]
            except ValueError:
                hit = keys[0]
        page.table.select_key(hit)
        page._on_pick(hit)

    QTimer.singleShot(2500, fire)


def _arm_auto(win, page: str) -> None:
    from PySide6.QtCore import QTimer

    name = AUTO_ACTIONS.get(page)
    if not name:
        return

    def fire():
        target = win.page(page)
        action = getattr(target, name, None)
        if action is not None:
            action()

    # 等连接那一轮走完 —— 连接是异步的,立刻点会撞上"还没连接设备"
    QTimer.singleShot(2500, fire)


def _arm_autoclose(app, win, seconds: float, shot: str) -> None:
    """定时自关(+ 可选截图)。

    **探针脚本必须自带超时自关。** 这个仓库真机踩过:一个没有自关的天球探针,
    代理迭代验证时每 3 分钟泄漏一个进程,几轮就吃掉几个 GB。
    """
    from PySide6.QtCore import QTimer

    def fire():
        if shot:
            try:
                win.grab().save(shot)
                logging.getLogger(__name__).info(_("截图已保存: %s"), shot)
            except Exception:                # noqa: BLE001
                logging.getLogger(__name__).exception(_("截图失败"))
        win.close()
        app.quit()

    QTimer.singleShot(int(seconds * 1000), fire)


if __name__ == "__main__":
    raise SystemExit(main())
