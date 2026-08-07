"""横幅永不可见 + 设备页那几条。

**横幅那条是这一整轮里影响最广的。** `_Banner._sync()` 拿
`self._notice.isVisible()` 决定整条横幅显不显示,而横幅自己初始是隐藏的 ——
**Qt 里父控件隐藏时子控件的 `isVisible()` 恒为 False**,于是
`show_notice()` 设完文字再 `_sync()`,算出来是 `False or False`,
横幅永远不出现。

代价是全局的:九个页面 `Page.report()` 报的后台异常、所有 `shell.notice()`
的提示、以及 watcher 的「正在拍摄 X 第 N 张」(shell 里只有这一个出口)
**一条都显示不出来**。设备页那几个"点了没反应"全是它的产物;真机上
每一次 SMB 失败都会表现成"界面卡住不动"。

同一个文件里 `notice_text()` 的注释一字不差地写着这个坑,而它下面两行就
踩了进去 —— **知道一条规则和在每一处都守住它是两回事**。

判据一律用 `isHidden()`:它反映的是**显式隐藏标志**,与父控件显不显示无关。
用 `isVisible()` 写断言等于把被测的那个 bug 又犯一遍(我第一次就是这么写的,
四个用例全"通过"了)。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from tests.support import tr

pytest.importorskip("PySide6")

ROOT = Path(__file__).resolve().parents[1]
QT = ROOT / "astro_smb_qt"
SHELL = QT / "shell.py"
DEVICES = QT / "pages" / "devices.py"


@pytest.fixture(scope="module")
def qt_app():
    from PySide6.QtWidgets import QApplication

    from astro_smb_qt import theme

    inst = QApplication.instance() or QApplication([])
    theme.apply(inst)
    return inst


def _fn(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name} 里没有 {name}")


def _src(path: Path, name: str) -> str:
    node = _fn(path, name)
    body = node.body[1:] if (node.body and isinstance(node.body[0], ast.Expr)
                             and isinstance(node.body[0].value, ast.Constant)
                             ) else node.body
    return "\n".join(ast.unparse(n) for n in body)


class TestBannerBecomesVisible:

    def test_startup_notice_is_visible(self, qt_app):
        """**修好之后才看得见的第一条**:干净的配置目录下(还没记过设备),
        扫描页会在启动时发一句"还没有记录任何设备"。

        这句话一直在发,只是横幅永远不出现 —— 修好之前它是隐形的。
        """
        from astro_smb_qt.shell import Shell

        sh = Shell()
        if sh.banner.notice_text():
            assert not sh.banner.isHidden(), (
                "有提示文本却还是隐藏的 —— 就是那条 bug")

    def test_notice_shows_it(self, qt_app):
        from astro_smb_qt.shell import Shell

        sh = Shell()
        # **不要断言"初始必然隐藏"** —— 干净配置下启动时本来就有一条提示
        # (见上一条)。这里测的是**转换**,先清干净再看。
        sh.banner.clear_notice()
        sh.banner.show_watch("")
        assert sh.banner.isHidden(), "清干净之后还占着一条"
        sh.notice("SMB 连接失败: 超时")
        assert not sh.banner.isHidden(), (
            "设了提示横幅还是隐藏的 —— 九个页面的错误提示都出不来")
        assert sh.banner.notice_text() == "SMB 连接失败: 超时"

    def test_clearing_hides_it_again(self, qt_app):
        from astro_smb_qt.shell import Shell

        sh = Shell()
        sh.banner.clear_notice()
        sh.notice("x")
        sh.banner.clear_notice()
        assert sh.banner.isHidden(), "清掉提示之后横幅还占着一条"

    def test_watch_shows_it(self, qt_app):
        """watcher 的「正在拍摄」在 shell 里**只有这一个出口**。"""
        from astro_smb_qt.shell import Shell

        sh = Shell()
        sh.banner.clear_notice()
        sh.banner.show_watch("正在拍摄 NGC 7293 第 12 张")
        assert not sh.banner.isHidden()

    def test_watch_clearing_hides_it(self, qt_app):
        from astro_smb_qt.shell import Shell

        sh = Shell()
        sh.banner.clear_notice()
        sh.banner.show_watch("拍着")
        sh.banner.show_watch("")
        assert sh.banner.isHidden()

    def test_either_one_keeps_it_up(self, qt_app):
        """两样只要有一样在,横幅就不能收。"""
        from astro_smb_qt.shell import Shell

        sh = Shell()
        sh.banner.clear_notice()
        sh.banner.show_watch("拍着")
        sh.notice("顺带一条提示")
        sh.banner.clear_notice()
        assert not sh.banner.isHidden(), "清提示把还在跑的拍摄状态一起收了"

    def test_sync_does_not_read_isvisible(self):
        """**根因钉死**:`isVisible()` 在父控件隐藏时恒为 False。"""
        src = _src(SHELL, "_sync")
        assert "isVisible()" not in src, (
            "又拿 `isVisible()` 当判据了 —— 父控件隐藏时它恒为 False,"
            "横幅永远不出现")
        assert ".text()" in src, "判据要以**文本**为准"

    def test_page_report_reaches_the_banner(self, qt_app):
        """页面报的后台异常要真的到得了横幅。"""
        from astro_smb_qt.shell import Shell

        sh = Shell()
        sh.page("browse").report(RuntimeError("枚举失败"), "列目录")
        assert not sh.banner.isHidden(), "页面报的异常没有出现在横幅上"
        assert "列目录" in sh.banner.notice_text()


class TestManualAdd:
    """`parse_manual_input` 要的是 host 字符串列表,不是记录 dict 列表。"""

    def test_page_passes_host_strings(self):
        src = _src(DEVICES, "_add")
        assert "r.get('host', '')" in src, (
            "又把整条记录 dict 传进去了 —— `_find_existing` 里 "
            "`host_key(item)` 会 AttributeError,任何合法输入都抛,"
            "而异常又被横幅那条 bug 吞掉,界面上是零反馈")

    def test_dicts_really_blow_up(self):
        """先把"传 dict 会炸"这件事本身钉死 —— 否则上面那条可能在测
        一个不存在的问题。"""
        from astro_smb_app.views import devices as dv

        with pytest.raises(AttributeError):
            dv.parse_manual_input("192.0.2.99", [{"host": "192.0.2.227"}])

    def test_strings_work(self):
        from astro_smb_app.views import devices as dv

        got = dv.parse_manual_input("192.0.2.99", ["192.0.2.227"])
        assert got["ok"] and got["host"] == "192.0.2.99"

    def test_duplicates_are_caught(self):
        from astro_smb_app.views import devices as dv

        got = dv.parse_manual_input("192.0.2.227", ["192.0.2.227"])
        assert not got["ok"] and got.get("dup")


class TestLocalCardsAreFed:
    """7.1:`local_card(rec)` 光秃秃地调 —— 容量/ZWO 特征/插拔状态全没有。"""

    def test_facts_are_passed(self):
        src = _src(DEVICES, "_cards")
        assert "facts=self._facts.get(root)" in src, (
            "本地卡没喂 facts —— 容量整组、ZWO 特征、「所在卷」全不出现,"
            "状态恒「○ 未检测」")
        assert "connected=" in src, "当前连着的那台也不标「当前连接」"
        assert "present_live=" in src, "插拔状态用的是快照,不是最新探测"

    def test_there_is_a_probe(self):
        src = _src(DEVICES, "_probe_local")
        assert "volumes_mod.scan_root" in src, "ZWO 特征命中没有采"
        assert "dv.local_facts(" in src

    def test_probe_runs_on_a_worker(self):
        """碰文件系统 —— 卡拔掉之后 `os.path.isdir` 会慢。"""
        src = _src(DEVICES, "_probe_local")
        assert "self.bg.run(" in src

    def test_absent_card_keeps_no_stale_hits(self):
        """卡不在时不要挂上一次的命中 —— 那会让人以为卡还在。"""
        src = _src(DEVICES, "_probe_local")
        assert "if ok else None" in src

    def test_it_is_called_on_show(self):
        assert "_probe_local()" in _src(DEVICES, "on_show")

    def test_local_card_really_needs_them(self):
        """把"不传 facts 就没有容量组"钉死。"""
        from astro_smb_app.views import devices as dv

        rec = {"host": "E:/ASIAIR", "kind": "local", "name": "卡"}
        bare = dv.local_card(rec)
        fed = dv.local_card(rec, facts={"root": "E:/ASIAIR", "present": True,
                                        "total": 64 << 30, "free": 12 << 30,
                                        "label": "ASIAIR", "drive": "E:",
                                        "hits": ["Autorun", "Plan"]})
        names = lambda c: [g[1] for g in (c.get("groups") or ())]  # noqa: E731
        assert names(fed) != names(bare), "喂不喂 facts 结果一样?那这条白测"


class TestBadgesAndButtons:

    def test_all_badges_are_drawn(self, qt_app):
        """`[:1]` 只画第一个 —— SMB 卡的「ASIAIR」判读、本地卡的
        「当前连接 / 本地卡 / ZWO 特征 / 已拔出」全被砍掉。

        **行为验证。** 只查源码不行:把 `if len(badges) > 1:` 换成
        `if False:` 之后,`badges[1:]` 这行文本还在,断言照样绿
        (反向验证里这条活了)。
        """
        from astro_smb_qt import widgets as W
        from astro_smb_qt.shell import Shell

        page = Shell().page("devices")
        card = page._card({"title": "T", "sub": "s",
                           "badges": [("SMB", "info"), ("ASIAIR", "good"),
                                      ("第三枚", "warn")]})
        texts = [c.text() for c in card.findChildren(W.StatusChip)]
        for want in ("ASIAIR", "第三枚"):
            assert any(want in t for t in texts), (
                f"第二枚之后的徽章没画:{texts}")

    def test_empty_action_text_draws_nothing(self):
        """`smb_card` 的 `open` 给的是 `(False, "", "")` —— 拿动作键名兜底
        会让按钮上直接印着英文 `open`。"""
        src = _src(DEVICES, "_actions")
        assert "text or name" not in src, "又拿动作键名当按钮文案了"
        assert "if not text:" in src

    def test_smb_card_really_has_an_empty_open(self):
        from astro_smb_app.views import devices as dv

        card = dv.smb_card({"host": "192.0.2.227", "kind": "smb"})
        assert card.get("open") == (False, "", ""), (
            "共享层改了 open 的形状,这条断言要跟着改")


class TestForgetIsConfirmed:
    """**行为验证。** 只查 `"self.confirm(" in src` 挡不住
    `if False and self.confirm(...)` —— 文本还在,分支已经死了
    (反向验证里这条活了)。"""

    def test_cancelling_does_not_forget(self, qt_app, monkeypatch):
        from astro_smb_qt.pages import devices as page_mod
        from astro_smb_qt.shell import Shell

        page = Shell().page("devices")
        gone = []
        monkeypatch.setattr(page_mod.devices_store, "forget",
                            lambda h: gone.append(h))
        page.confirm = lambda *a, **k: False
        page.reload = lambda: None
        page._forget("192.0.2.227")
        assert not gone, "点了取消还是把设备记录删了"

    def test_confirming_forgets(self, qt_app, monkeypatch):
        from astro_smb_qt.pages import devices as page_mod
        from astro_smb_qt.shell import Shell

        page = Shell().page("devices")
        gone = []
        monkeypatch.setattr(page_mod.devices_store, "forget",
                            lambda h: gone.append(h))
        page.confirm = lambda *a, **k: True
        page.reload = lambda: None
        page._forget("192.0.2.227")
        assert gone == ["192.0.2.227"]

    def test_it_asks_at_all(self, qt_app):
        """确认框真的弹了 —— 而不是根本没调。"""
        from astro_smb_qt.shell import Shell

        page = Shell().page("devices")
        asked = []
        page.confirm = lambda *a, **k: asked.append((a, k)) or False
        page.reload = lambda: None
        page._forget("192.0.2.227")
        assert asked, "破坏性动作没有二次确认"
        assert asked[0][1].get("ok_text") == tr("忘记"), asked[0]
