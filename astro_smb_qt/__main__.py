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
import os
import sys


def _utf8_when_redirected() -> None:
    """输出被重定向时钉成 UTF-8。**交互式控制台不碰。**

    打包后的 exe 走 PyInstaller 的引导,**`PYTHONIOENCODING` 不生效**;实测
    Windows 上它按机器的 ANSI 代码页写(这台是 GBK)。重定向到文件或被 CI
    抓走时:中文在中文 Windows 上只是"编码不对",在**英文 Windows 上直接
    变成一串问号** —— 那是丢字,不是显示错,而丢的正是报错信息。

    真控制台那一支不动:Python 对着控制台走宽字符 API,本来就是对的,
    强行改 encoding 反而可能把对的改坏。
    """
    for stream in (sys.stdout, sys.stderr):
        if stream is None or not hasattr(stream, "reconfigure"):
            continue
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):          # 流已关闭 / 不支持
            pass


def _chromium_cannot_sandbox_as_root() -> None:
    """以 root 跑时给 QtWebEngine 关掉沙箱。**只在 root 下,只在 Linux 上。**

    Chromium 的 zygote 检查到 uid 0 会直接 `LOG(FATAL)` —— 不是抛异常,是
    **把整个进程带走**,Python 这边一个字都拦不住。而九个页面是开机就建的
    (天球页里有 `QWebEngineView`),所以这不是"天球页打不开",是
    **整个程序起不来,退出码 1,只留一行 Chromium 的英文日志**。

    WSL / docker / CI 默认就是 root,实测打好的 Linux 包在里面就是这个下场。
    普通桌面用户不是 root,沙箱照常开着 —— 这一支碰不到他们。

    必须在 QtWebEngine 初始化**之前**设,所以放在模块级。
    """
    if not sys.platform.startswith("linux"):
        return
    if getattr(os, "geteuid", None) is None or os.geteuid() != 0:
        return
    flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    if "--no-sandbox" not in flags:
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
            f"{flags} --no-sandbox".strip())


_utf8_when_redirected()
_chromium_cannot_sandbox_as_root()


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
    ap.add_argument(
        "--selftest", action="store_true",
        help=_("检查随包资源找不找得到(打完包必跑),不开界面"))
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


def _selftest() -> int:
    """检查**随包资源**在这台机器上真的找得到。

    只查那些"用路径打开、不是 import 进来"的东西 —— 它们缺了都不会让程序
    起不来,只会让某一页空白、或者界面永远是中文。也就是说:**打包坏了的
    典型症状,恰好都不报错。**

    每一条都打印查到的实际路径,不只是打勾 —— 排查时要的是"它去哪儿找了"。
    """
    import sys as _sys

    from astro_smb import i18n

    rows: list[tuple[bool, str, str]] = []

    langs = [x for x in i18n.available_languages() if x != i18n.SOURCE_LANGUAGE]
    rows.append((bool(langs), "翻译词表", f"{len(langs)} 种: {langs}"))

    # 真翻一句 —— 词表文件在不代表 gettext 找得到它(目录层级差一层就静默
    # 回退到原文)。用一条**一定有译文**的:界面语言名。
    if "en" in langs:
        got = i18n.gettext_in("en", "语言")
        rows.append((got != "语言", "英文能翻出来", f"语言 -> {got!r}"))

    from astro_smb_app.views import sky3d

    web = sky3d.PKG_WEB_DIR
    need = {"sky3d.js", "sky3d.css", "sky3d.html"}
    have = {p.name for p in web.iterdir()} if web.is_dir() else set()
    rows.append((need <= have, "天球静态资产", f"{web} -> {sorted(have)}"))

    from astro_smb_qt import webhost

    ok, why = webhost.available()
    rows.append((ok, "QtWebEngine", why or "可用"))

    from astro_smb_app.icons import icon_dir, icon_files

    files = icon_files()
    rows.append((len(files) >= 3, "窗口图标",
                 f"{icon_dir()} -> {len(files)} 档"))

    from astro_smb_app import bundle

    rows.append((True, "运行方式",
                 f"frozen={bundle.frozen()} root={bundle.bundle_root()}"))

    bad = 0
    for good, name, detail in rows:
        if not good:
            bad += 1
        print(f"  {'ok ' if good else 'FAIL'}  {name:16} {detail}")
    if bad:
        print(f"\n{bad} 项不通过 —— 这个包缺东西,而它照样起得来。",
              file=_sys.stderr)
    else:
        print("\n自检通过:随包资源都找得到。")
    return 1 if bad else 0


def _set_app_icon(app) -> None:
    """窗口 / 任务栏 / alt-tab 的图标。

    **多档一起塞进同一个 `QIcon`。** Qt 会按需要挑最接近的那一档;
    只给一张 256 的话,任务栏那个 16px 是它缩出来的,糊得很明显。

    找不到就算了 —— 没图标是难看,不是坏掉。
    """
    from PySide6.QtGui import QIcon, QPixmap

    from astro_smb_app.icons import icon_files

    files = icon_files()
    if not files:
        return
    icon = QIcon()
    for f in files:
        icon.addPixmap(QPixmap(str(f)))
    app.setWindowIcon(icon)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s")

    if args.selftest:
        return _selftest()

    from PySide6.QtWidgets import QApplication

    # **语言要在建界面之前定下来。** 控件上的文案是建控件那一刻翻好烤进去的,
    # 之后再 `set_language()` 已经建好的那些不会自己变。
    # (`ASTRO_SMB_LANG` 仍然优先 —— 见 `i18n.detect_language`。)
    from astro_smb_app import settings

    settings.apply_saved_language()

    app = QApplication(sys.argv[:1])
    app.setApplicationName("Astro SMB Tool")
    _set_app_icon(app)
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

    **带界面的脚本必须自带超时自关。** 真机踩过:一个没有自关的天球探针,
    反复跑验证时每 3 分钟泄漏一个进程,几轮就吃掉几个 GB —— 而它还拖着
    一整棵 WebView2 子进程树。
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
