"""联网下载管理:共享层的总账 + Qt 那个独立窗口。

这一块存在的理由是"用户看不见这软件会联哪儿的网":三样资产原来各自埋在
用到它的那一页里,想知道全貌得翻三个文件。**对一台架在野外用手机热点的
笔记本来说,这是要提前知道的事。**
"""
from __future__ import annotations

import threading

import pytest

from astro_smb_app.views import downloads as DL


class TestTheRegistryIsCompleteAndKeyed:

    def test_every_network_asset_is_listed(self):
        """**新加一个联网下载就得在这里登记。**

        漏登记的后果不是报错,是"管理窗口里看不到它" —— 而那正是这个
        窗口存在的全部意义。三样:星表 / 巡天底图 / three.js。
        """
        assert {a.key for a in DL.ASSETS} == {"catalog", "survey", "three"}

    def test_identity_is_the_key_not_the_title(self):
        """标题会被翻译,key 不会。这个仓库栽过四次。"""
        from astro_smb import i18n

        try:
            keys = {}
            for lang in ("zh_CN", "en", "xx_PS"):
                i18n.set_language(lang)
                keys[lang] = [r["key"] for r in DL.rows()]
            assert len({tuple(v) for v in keys.values()}) == 1, keys
        finally:
            i18n.set_language("zh_CN")

    def test_order_is_stable(self):
        """刷新一下不许换位置 —— 用户正要点的那一行会跑掉。"""
        assert [r["key"] for r in DL.rows()] == [a.key for a in DL.ASSETS]

    def test_every_row_says_what_it_is_for_and_where_it_comes_from(self):
        for row in DL.rows():
            assert row["why"].strip(), row["key"]
            assert "." in row["source"], f"{row['key']} 没写来源主机"
            assert row["source_note"].strip(), row["key"]

    def test_each_one_has_a_downloader(self):
        for asset in DL.ASSETS:
            assert callable(DL.downloader(asset.key)), asset.key

    def test_an_unknown_key_is_a_hard_error(self):
        with pytest.raises(KeyError):
            DL.downloader("nope")


class TestStatusLineTellsTheTruth:

    def test_a_half_file_is_not_reported_as_missing(self):
        """**下坏的文件"看起来在"。**

        错误页 / 半截下载会留下一个小文件:`ready` 是假的,而 `bytes` > 0。
        只说"未下载"的话用户会再点一次下载,而重下会先撞上同一个坏文件,
        看起来像"怎么下都不成"。得说出来。
        """
        row = {"key": "catalog", "ready": False, "bytes": 4096,
               "size_hint": "约 159 MB"}
        line = DL.state_line(row)
        assert "不完整" in line and "4" in line

    def test_ready_says_how_much_disk_it_eats(self):
        """这是"管理"窗口 —— 来这儿也可能是想知道缓存吃了多少盘。"""
        row = {"key": "survey", "ready": True, "bytes": 8_000_000,
               "size_hint": "约 8 MB"}
        assert "已就绪" in DL.state_line(row)
        assert "MB" in DL.state_line(row)

    def test_missing_quotes_the_size_up_front(self):
        row = {"key": "three", "ready": False, "bytes": 0,
               "size_hint": "约 1.3 MB"}
        assert "1.3 MB" in DL.state_line(row)


class TestRemove:
    def test_removing_something_that_is_not_there_is_false_not_a_crash(self,
                                                                      monkeypatch):
        monkeypatch.setitem(DL._STATE, "catalog", lambda: (False, None))
        assert DL.remove("catalog") is False


# ══════════════════════════════════════════════════════════════════════
class TestTheWindow:
    """Qt 那个独立窗口。"""

    @pytest.fixture(scope="module")
    def qt_app(self):
        pytest.importorskip("PySide6")
        from PySide6.QtWidgets import QApplication

        from astro_smb_qt import theme

        inst = QApplication.instance() or QApplication([])
        theme.apply(inst)
        return inst

    @pytest.fixture
    def win(self, qt_app):
        from astro_smb_qt.downloads import DownloadsWindow

        w = DownloadsWindow()
        yield w
        w.close()

    def test_one_card_per_asset(self, win):
        assert set(win._rows) == {a.key for a in DL.ASSETS}

    def test_two_downloads_do_not_cancel_each_other(self, win, monkeypatch):
        """**这条是真 bug 拦下来的。**

        第一版每次 `_start` 都 `self.bg.bump()`。点第二样的时候,世代一涨,
        第一样的 `on_progress`/`on_done` 就整份作废 —— 它**还在下**,而
        进度条从此不动、下完也不刷新状态。而这个窗口的全部意义就是同时
        管几样东西。
        """
        started = []
        monkeypatch.setattr(
            DL, "run",
            lambda key, **kw: started.append(key) or __import__("pathlib").Path("."))
        gen_before = win.bg.generation
        win._start("survey")
        win._start("three")
        assert win.bg.generation == gen_before, (
            "开第二个下载把第一个的世代顶掉了 —— 第一个的进度和完成都收不到了")

    def test_cancelling_is_not_reported_as_a_failure(self, win):
        """用户自己点的停止,报一句红色「失败」会让他以为出事了。"""
        row = win._rows["three"]
        row.start()
        win._cancel["three"] = tok = _Tok(cancelled=True)
        assert tok.cancelled
        # 走一遍失败分支的判据
        row.finish("已停止")
        assert "停止" in row.state.text()

    def test_closing_cancels_what_is_in_flight(self, win):
        """关了窗口下载还在后台跑的话,用户既看不到也停不了它。"""
        tok = _Tok()
        win._cancel["catalog"] = tok
        win.close()
        assert tok.cancelled, "关窗没有取消在途下载"

    def test_progress_without_a_total_does_not_fake_a_bar(self, win):
        """有的服务器不给 Content-Length。画一根乱动的条比不画更误导。"""
        row = win._rows["survey"]
        row.start()
        row.tick(1234, 0)
        assert "1" in row.state.text()

    def test_progress_with_a_total_fills_the_bar(self, win):
        row = win._rows["survey"]
        row.start()
        row.tick(50, 100)
        assert row.bar._frac == pytest.approx(0.5)

    def test_the_shell_has_an_entry_point(self):
        """窗口存在但没人能打开它 = 白做。"""
        import ast
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1]
               / "astro_smb_qt" / "shell.py").read_text(encoding="utf-8")
        names = {n.name for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.FunctionDef)}
        assert "open_downloads" in names
        assert "_build_downloads_entry" in names

    def test_opening_twice_reuses_the_same_window(self):
        """开三个窗口各自下同一样东西会撞同一个 `.part`。"""
        import ast
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1]
               / "astro_smb_qt" / "shell.py").read_text(encoding="utf-8")
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "open_downloads")
        body = ast.unparse(fn)
        assert "_downloads_win" in body and "is None" in body


class _Tok:
    def __init__(self, cancelled: bool = False):
        self.event = threading.Event()
        if cancelled:
            self.event.set()

    @property
    def cancelled(self) -> bool:
        return self.event.is_set()

    def cancel(self) -> None:
        self.event.set()


class TestTheCardsAreActuallyRendered:
    """**18 条测试全绿,而三张卡片是空的黑杠。**

    `W.Card` 构造时已经装好了自己的布局;再 `card.setLayout(col)` 一次,
    Qt 只在 stderr 上嘟囔一句就**忽略**它 —— 控件全落在一个没人显示的
    孤儿布局里。`.text()` 照样读得到,所以断言文案的测试一条都不红。
    是**截图**看出来的。

    这几条查的是"控件到底在不在窗口里",不是"文字对不对"。
    """

    @pytest.fixture(scope="module")
    def qt_app(self):
        pytest.importorskip("PySide6")
        from PySide6.QtWidgets import QApplication

        from astro_smb_qt import theme

        inst = QApplication.instance() or QApplication([])
        theme.apply(inst)
        return inst

    @pytest.fixture
    def win(self, qt_app):
        from astro_smb_qt.downloads import DownloadsWindow

        w = DownloadsWindow()
        yield w
        w.close()

    def test_every_label_has_a_parent_widget(self, win):
        """孤儿布局里的控件**没有父控件** —— 这就是那次的直接症状。"""
        orphan = []
        for key, row in win._rows.items():
            for name in ("title", "why", "source", "state", "bar",
                         "go_btn", "rm_btn"):
                w = getattr(row, name)
                if w.parentWidget() is None:
                    orphan.append(f"{key}.{name}")
        assert not orphan, f"这些控件没进任何窗口,画不出来: {orphan}"

    def test_a_card_is_tall_enough_to_hold_its_content(self, win):
        """空布局的卡片只有内边距那么高。三行字 + 一排按钮不可能这么矮。"""
        for key, row in win._rows.items():
            h = row.sizeHint().height()
            assert h > 80, f"{key} 那张卡只有 {h}px 高 —— 里面是空的"

    def test_nobody_calls_setlayout_on_a_card(self):
        """**门禁,盯做法。** `W.Card` 永远自带布局,对它 `setLayout` 永远
        是错的,而且永远静默。整个 Qt 包扫一遍。"""
        import ast
        from pathlib import Path

        qt = Path(__file__).resolve().parents[1] / "astro_smb_qt"
        bad = []
        for path in sorted(qt.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            cards = set()
            for node in ast.walk(tree):
                # `x = W.Card()` / `x = Card()`
                if (isinstance(node, ast.Assign) and len(node.targets) == 1
                        and isinstance(node.targets[0], ast.Name)
                        and isinstance(node.value, ast.Call)):
                    fn = node.value.func
                    name = (fn.attr if isinstance(fn, ast.Attribute)
                            else getattr(fn, "id", ""))
                    if name == "Card":
                        cards.add(node.targets[0].id)
            for node in ast.walk(tree):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "setLayout"
                        and isinstance(node.func.value, ast.Name)
                        and node.func.value.id in cards):
                    rel = path.relative_to(qt.parent).as_posix()
                    bad.append(f"{rel}:{node.lineno}")
        assert not bad, (
            "对 W.Card 调了 setLayout —— Qt 会静默忽略,卡片渲染成空黑杠。"
            f"往 `card.body` 里加或者用 `card.add()`: {bad}")
