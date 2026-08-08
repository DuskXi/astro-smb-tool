"""两个**早就记录在案、一直没修**的竞态。

共同点是这个仓库最贵的那一类:**不报错、不崩、只是不对**,而且都要"某一步
比另一步快"才现形 —— 于是在开发机上偶尔看见一次,重开就好了,没人追。

两条都**先写测试再修**,而且都做过回退验证(把修改撤掉,这里必红)。
"""
from __future__ import annotations

import asyncio
import types

import pytest


# ══════════════════════════════════════════════════════════════════════
# 一、老 UI 浏览页:容量条卡在「读取容量…」
# ══════════════════════════════════════════════════════════════════════
class TestVolumeResultIsNotThrownAwayTooEarly:
    r"""点共享时同时发两个任务:`_navigate` 和 `_load_volume`。

    `self.share` 是 **`_navigate` 里**才赋的(它要先查缓存、发 listdir),
    而 `_load_volume` 回来时拿 `self.share != share` 当"用户已经换走了"的
    判据。两件事没有先后保证:

    * 本地磁盘后端(ZWO 卡直插电脑)`volume_info` 是微秒级的;
    * `_navigate` 要走一次目录列举,慢得多。

    于是**先回来的容量结果被自己的守卫丢掉**,容量条永远停在"读取容量…"。
    不报错,刷新一下又可能好 —— 典型的"偶发"。

    判据必须是**用户最后点的是哪个共享**,那是点下去那一刻就确定的事,
    不该等 `_navigate` 走完。
    """

    def _page(self, *, current_share, asked_share, vol):
        from astro_smb_gui._browser import BrowserPage

        page = types.SimpleNamespace()
        page.share = current_share          # `_navigate` 还没来得及改
        page._want_share = asked_share      # 点下去那一刻就定了
        page.vol_text = types.SimpleNamespace(Text="")
        page.vol_bar = types.SimpleNamespace(Value=-1)
        page.shell = types.SimpleNamespace(
            client=types.SimpleNamespace(volume_info=lambda s: vol))
        page._load_volume = BrowserPage._load_volume.__get__(page)
        return page

    @staticmethod
    def _vol(total=1000, used=110, free=890, percent=11.0):
        return types.SimpleNamespace(total=total, used=used, free=free,
                                     percent=percent)

    def test_it_lands_even_when_navigate_has_not_caught_up(self):
        """**这条就是那个 bug。** `self.share` 还是旧的,而用户要的是新的。"""
        page = self._page(current_share=None, asked_share="EMMC Images",
                          vol=self._vol())
        asyncio.run(page._load_volume("EMMC Images"))
        assert page.vol_bar.Value == pytest.approx(11.0), (
            "容量结果被守卫丢了 —— 界面会一直停在「读取容量…」")
        assert "读取容量" not in page.vol_text.Text, page.vol_text.Text

    def test_a_stale_result_is_still_dropped(self):
        """反面保险:用户**真的**换走之后,旧共享的结果不许画上去。

        少了这一半,修法就退化成"把守卫删掉",那是用另一个 bug 换这个。
        """
        page = self._page(current_share="A", asked_share="B", vol=self._vol())
        asyncio.run(page._load_volume("A"))
        assert page.vol_bar.Value == -1, "换共享之后旧结果还是画上去了"

    def test_unsupported_share_says_so(self):
        page = self._page(current_share=None, asked_share="X", vol=None)
        asyncio.run(page._load_volume("X"))
        assert "不支持" in page.vol_text.Text


# ══════════════════════════════════════════════════════════════════════
# 二、Qt 空间页:扫描途中换共享
# ══════════════════════════════════════════════════════════════════════
class TestChangingShareMidScanDoesNotLeakTheOldResult:
    """扫描跑着的时候在下拉里换一个共享,原来什么都不做。

    三样东西同时不对,而且都不报错:

    1. 在途扫描**没被取消**,继续在后台跑;
    2. 世代**没作废** —— 于是它算完之后 `_apply` 照常接受,
       **把旧共享的占用图画在新共享的标题下面**;
    3. `_busy` 还是 True、按钮还写着「停止」,而画面已经是"还没有扫描结果"。

    第 2 条最贵:屏幕上是一张看起来完全正常的图,只是属于另一个共享。
    """

    @pytest.fixture
    def page(self, qt_app):
        from PySide6.QtCore import QObject, Signal

        from astro_smb_qt.pages.space import SpacePage

        class _Shell(QObject):
            connected = Signal(list)
            theme_changed = Signal()

            def __init__(self):
                super().__init__()
                self.client_factory = None

        p = SpacePage(_Shell())
        p.on_connected(["A", "B"])
        return p

    @pytest.fixture(scope="module")
    def qt_app(self):
        from PySide6.QtWidgets import QApplication

        from astro_smb_qt import theme

        inst = QApplication.instance() or QApplication([])
        theme.apply(inst)
        return inst

    def _arm(self, page):
        """假装正在扫 A:忙态 + 一个能问"被取消了没"的令牌 + 记下 bump。"""
        from astro_smb_qt.workers import CancelToken

        page.share = "A"
        page._busy = True
        page._cancel = CancelToken()
        page.scan_btn.setText("停止")
        bumped: list[int] = []
        real_bump = page.bg.bump
        page.bg.bump = lambda: (bumped.append(1), real_bump())[1]
        return bumped

    def test_it_cancels_the_scan_in_flight(self, page):
        bumped = self._arm(page)
        page._pick_share(1)
        assert page._cancel is None or page._cancel.event.is_set(), (
            "换了共享,旧扫描还在跑")
        assert bumped, (
            "世代没作废 —— 旧共享扫完的结果会被当成新共享的画上去")

    def test_the_button_and_busy_flag_come_back(self, page):
        self._arm(page)
        page._pick_share(1)
        assert page._busy is False, "还卡在忙态,再点一下是取消一个已经没了的扫描"
        assert page.scan_btn.text() != "停止", page.scan_btn.text()

    def test_the_new_share_starts_from_a_clean_slate(self, page):
        self._arm(page)
        page._pick_share(1)
        assert page.share == "B"
        assert page.crumbs == [] and page.path == ""

    def test_changing_share_while_idle_is_still_fine(self, page):
        """没在扫的时候换共享不该被这条修改弄出别的动静。"""
        page._busy = False
        page._cancel = None
        page._pick_share(1)
        assert page.share == "B" and page._busy is False


# ══════════════════════════════════════════════════════════════════════
# 三、并行下载的重连判据还在翻译过的文本里搜关键词
# ══════════════════════════════════════════════════════════════════════
class TestParallelRetryDoesNotGoThroughDisplayText:
    """i18n 那一轮把 `transfers._is_retryable` 和 `client.makedirs` 都改成了
    判结构化标志,**漏了并行下载器**。

    `parallel._is_conn_error` 至今还是
    ``any(k in str(e) for k in (_("中断"), _("超时"), _("连接"), …))`` ——
    在**翻译过的**消息里找**翻译过的**关键词。中文下"下载超时"恰好含"超时",
    换一种语言未必:译文里那个孤零零的词不一定是整句错误消息的子串。

    失效的样子:分块下载遇到断连**不再 reconnect、不再重试**,直接把异常
    抛给上层。不报错误、不崩 —— 只是大文件下载的成功率悄悄掉下来,
    而且只在非中文界面上。
    """

    def _err(self, msg, *, retryable):
        from astro_smb.client import SmbClientError

        return SmbClientError(msg, retryable=retryable)

    def test_a_retryable_error_reconnects_whatever_the_message_says(self):
        """判据是核心库给的布尔字段,不是消息长什么样。"""
        from astro_smb.parallel import _is_conn_error

        assert _is_conn_error(self._err("Соединение прервано", retryable=True))
        assert _is_conn_error(self._err("接続が中断されました", retryable=True))

    def test_it_still_works_in_the_source_language(self):
        from astro_smb.client import SmbClientError
        from astro_smb.parallel import _is_conn_error

        assert _is_conn_error(SmbClientError("下载超时", retryable=True))

    def test_a_plain_failure_is_not_retried(self):
        """反面:不是连接类的错误不该无限重连 —— 那会把"文件不存在"
        变成四次徒劳的往返。"""
        from astro_smb.parallel import _is_conn_error

        assert not _is_conn_error(self._err("路径不存在", retryable=False))

    def test_ascii_fallback_survives(self):
        """有些异常没经过 `_run`(底层库自己抛的 socket 文本),
        那些词与语言无关,兜底留着。"""
        from astro_smb.parallel import _is_conn_error

        assert _is_conn_error(self._err("read timeout", retryable=False))
        assert _is_conn_error(self._err("connection reset by peer",
                                        retryable=False))

    def test_nobody_searches_translated_words_any_more(self):
        """门禁:重试判据里不许再出现 `_( )` 包着的关键词。

        这条盯的是**做法**不是某一处 —— 同样的写法在这个仓库里出现过三次了。
        """
        import ast
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        bad = []
        for rel in ("astro_smb/parallel.py", "astro_smb_app/transfers.py",
                    "astro_smb/client.py"):
            src = (root / rel).read_text(encoding="utf-8")
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if not isinstance(node, ast.FunctionDef):
                    continue
                if "retry" not in node.name and "conn_error" not in node.name:
                    continue
                # **摘掉文档串再看。** 这几个函数的注释里正好在引用
                # 那句错误写法当反例 —— 拿它当命中,等于禁止解释历史。
                stmts = node.body
                if (stmts and isinstance(stmts[0], ast.Expr)
                        and isinstance(stmts[0].value, ast.Constant)
                        and isinstance(stmts[0].value.value, str)):
                    stmts = stmts[1:]
                body = "\n".join(ast.unparse(x) for x in stmts)
                if "_(" in body:
                    bad.append(f"{rel}:{node.lineno} {node.name}")
        assert not bad, (
            "重试判据里又出现了翻译调用 —— 那是在翻译过的文本里搜翻译过的词:\n  "
            + "\n  ".join(bad))


# ══════════════════════════════════════════════════════════════════════
# 四、同一层里两份扩展名表
# ══════════════════════════════════════════════════════════════════════
class TestExtensionTablesAreNotForkedInTheSharedLayer:
    """`astro_smb_app.entries` 与 `astro_smb_app.preview` 各有一份
    `FITS_EXTS` / `IMAGE_EXTS` / `TEXT_EXTS`,**同一个包里**。

    这不是"将来可能分叉",是**已经分叉了**:`TEXT_EXTS` 两边不一样
    (preview 多了 py/sh/yaml/yml)。后果不严重但很别扭 —— 一个 `.yaml`
    在浏览页被归到"其他",点开却按文本预览出来了。

    两份完全相同的(FITS/IMAGE)收成一份;真有差别的那份把差别**写出来**,
    这样下次谁动了哪一边都看得见。
    """

    def _mods(self):
        from astro_smb_app import entries, preview

        return entries, preview

    def test_fits_and_image_are_literally_the_same_object(self):
        entries, preview = self._mods()
        assert preview.FITS_EXTS is entries.FITS_EXTS, (
            "FITS 扩展名还有两份 —— 两边都能改,而改错哪边都不报错")
        assert preview.IMAGE_EXTS is entries.IMAGE_EXTS

    def test_the_text_difference_is_spelled_out(self):
        """预览认得比分类多是可以的,但**必须是显式的加法**,
        不能是两份各写各的然后碰巧不一样。"""
        entries, preview = self._mods()
        assert entries.TEXT_EXTS < preview.TEXT_EXTS, (
            "预览的文本类型不再是分类那份的超集 —— 两边真的分叉了")
        extra = preview.TEXT_EXTS - entries.TEXT_EXTS
        assert extra == {".py", ".sh", ".yaml", ".yml"}, extra
