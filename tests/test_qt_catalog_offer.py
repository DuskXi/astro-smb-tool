"""新机器上的星表:要**说清楚 + 给入口**,不是甩一个 errno。

用户在新机器上试板解算,报的是"星表没有自动下载"。查下来 Qt 这边
**一次都没有查过星表是否就绪**:`astro_smb_qt` 里 `ensure_catalog` /
`catalog_available` 一个引用都没有(老 UI 的 `_fitsview.py` 和 Uno 的
`app.py` 都有)。于是解算直接抛到 `fail()`,面板上写的是

    解算失败:无法读取星表文件: [Errno 2] No such file or directory: ...

一句 errno —— 既没说缺的是星表,也没给获取的路。

**修法是"先问再下",不是自动下。** 那是约 159 MB 的网络流量,
与巡天底图同款口径(`_set_sky_bg` 也是先弹确认再下)。所以这份闸门
里有一条反过来的断言:`_solve` **不许**自己去 `ensure_catalog`。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest
from tests.support import tr

pytest.importorskip("PySide6")

ROOT = Path(__file__).resolve().parents[1]
FITS = ROOT / "astro_smb_qt" / "pages" / "fitsview.py"


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    from PySide6.QtWidgets import QApplication

    from astro_smb_qt import theme

    inst = QApplication.instance() or QApplication([])
    theme.apply(inst)
    return inst


def _page(catalog_ready: bool):
    """一个已经"打开了图"的页面,星表就绪与否可控。"""
    from astro_smb_qt.shell import Shell

    page = Shell().page("fits")
    page._local = "x.fit"
    page._hdr = object()
    page.model = {}
    page._catalog_ready = staticmethod(lambda: catalog_ready)
    return page


def _src(name: str) -> str:
    tree = ast.parse(FITS.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            body = node.body[1:] if (node.body
                                     and isinstance(node.body[0], ast.Expr)
                                     and isinstance(node.body[0].value,
                                                    ast.Constant)
                                     ) else node.body
            return "\n".join(ast.unparse(n) for n in body)
    raise AssertionError(f"fitsview.py 里没有 {name}")


class TestItChecksBeforeSolving:

    def test_missing_catalog_does_not_run_the_solver(self):
        """查都不查就去解算 —— 那就是用户报的那个 errno。"""
        page = _page(False)
        ran = []
        page.bg.run = lambda *a, **k: ran.append(1)
        page._solve()
        assert ran == [], "星表不在还是把解算跑起来了"

    def test_it_says_what_is_missing_and_how_big(self):
        page = _page(False)
        page._solve()
        rows = dict(page.model.get("solve_rows") or ())
        assert rows.get(tr("状态")) == tr("星表未就绪")
        assert "Tycho-2" in rows.get(tr("来源"), "")
        note = rows.get(tr("说明"), "")
        assert "MB" in note, "没写要下多大 —— 用户没法判断要不要点"
        assert "CDS" in note

    def test_the_button_appears(self):
        page = _page(False)
        page._solve()
        assert page.model.get("catalog_offer") is True
        page._render_side()
        assert page.catalog_btn is not None
        assert page.catalog_btn.text() == tr("下载星表")

    def test_the_solve_button_stays_clickable(self):
        """不能把人卡死在这儿 —— 下完还要能再点一次解算。"""
        page = _page(False)
        page._solve()
        assert page.solve_btn.isEnabled()

    def test_a_ready_catalog_goes_straight_to_solving(self):
        page = _page(True)
        ran = []
        page.bg.run = lambda *a, **k: ran.append(1)
        page._solve()
        assert ran == [1]
        assert page.model.get("solve") == tr("正在解算…")


class TestItAsksFirst:
    """**不自动下。** 159 MB 得有人点头。"""

    def test_solve_never_fetches_by_itself(self):
        src = _src("_solve")
        assert "ensure_catalog" not in src, "解算自己去下星表了 —— 得先问"

    def test_only_the_button_fetches(self):
        assert "ensure_catalog" in _src("_download_catalog")

    def test_readiness_check_is_validating_not_just_existence(self):
        """`catalog_available()` 是**校验**过的,不是"文件在就算"。
        半截的 `.part` 或损坏的缓存也得算不就绪。"""
        src = _src("_catalog_ready")
        assert "catalog_available" in src


class TestTheDownloadFlow:

    def test_it_continues_the_solve_afterwards(self):
        """下完要接着做用户本来要做的事,不要让他再点一次。"""
        src = _src("_download_catalog")
        at = src.index("def done")
        assert "self._solve()" in src[at:]

    def test_failure_keeps_a_way_to_retry(self):
        src = _src("_download_catalog")
        at = src.index("def fail")
        seg = src[at:]
        assert "catalog_offer" in seg, "失败之后入口没了,只能重开页面"
        assert "星表获取失败" in seg

    def test_progress_is_reported_in_megabytes(self):
        """159 MB 的下载没有进度 = 看起来像卡死。"""
        src = _src("_catalog_progress")
        assert "MB" in src
        page = _page(False)
        page.model = {}
        page._catalog_progress((12_000_000, 159_000_000))
        assert "12" in page.model["solve"] and "159" in page.model["solve"]

    def test_progress_survives_an_unknown_total(self):
        """`ensure_catalog` 的 total 可能是 0(服务端不给长度)——
        那时不能显示 "12 / 0 MB",更不能除零。"""
        page = _page(False)
        page.model = {}
        page._catalog_progress((12_000_000, 0))
        assert "/" not in page.model["solve"]

class TestItScrollsAllTheWayDown:
    """板解算那一段在长侧栏的**最下面**,点了要滚到它。

    第一版只 `setValue(maximum())` 一次:解算结果让内容从 168px 长到 452px,
    而 `QTimer.singleShot(0, …)` 跑的时候布局**还没按新内容重算** ——
    读到的仍是旧的 168,于是只滚到一半,「离先验中心」等十行仍在折叠线以下。
    独立验收把这条抓出来了。
    """

    def test_a_late_range_update_still_reaches_the_bottom(self):
        page = _page(True)
        bar = page.side.verticalScrollBar()
        page._show_solve_area()
        bar.setRange(0, 452)           # 布局晚一步才把范围撑大
        assert bar.value() == 452

    def test_it_does_not_hijack_later_scrolling(self):
        """补滚只补一次。不断开的话,以后换张图、切个拉伸档都会把用户
        拽到底 —— 那是"界面自己乱跑"。"""
        page = _page(True)
        bar = page.side.verticalScrollBar()
        page._show_solve_area()
        bar.setRange(0, 452)
        bar.setValue(10)
        bar.setRange(0, 900)           # 后续的范围变化
        assert bar.value() == 10

    def test_asking_twice_works(self):
        page = _page(True)
        bar = page.side.verticalScrollBar()
        page._show_solve_area()
        bar.setRange(0, 452)
        page._show_solve_area()
        bar.setRange(0, 700)
        assert bar.value() == 700

    def test_disconnect_is_flag_guarded_not_exception_guarded(self):
        """PySide 对"断一个没连上的信号"是发 `RuntimeWarning` 而不是抛 ——
        `try/except` 拦不住,只会刷日志。"""
        import warnings

        page = _page(True)
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            page._disconnect_range()   # 从没连过
            page._show_solve_area()
            page._disconnect_range()

    def _spy(self, page):
        """**盯效果,不盯标志位。** `_render_side()` 会把 `_scroll_to_solve`
        用掉并复位,事后去查那个布尔永远是 False。"""
        from PySide6.QtWidgets import QApplication

        hits = []
        page._show_solve_area = lambda: hits.append(1)
        return hits, QApplication.instance()

    def test_solving_requests_the_scroll(self):
        page = _page(True)
        page.bg.run = lambda *a, **k: None
        hits, app = self._spy(page)
        page._solve()
        app.processEvents()            # singleShot(0) 要跑一次事件循环
        assert hits, "点了板解算却没把那一段滚进视野"

    def test_the_offer_requests_it_too(self):
        page = _page(False)
        hits, app = self._spy(page)
        page._solve()                  # 星表不在 → 走 _offer_catalog
        app.processEvents()
        assert hits, "「星表未就绪」出现在折叠线以下,点了像没反应"


class TestTheDownloadFlowMore:

    def test_the_two_progress_shapes_are_bridged(self):
        """`Bg.run` 的 report 收**一个**参数,`ensure_catalog` 的 progress
        给**两个** —— 中间必须转一道,直接递进去会 TypeError。"""
        src = _src("_download_catalog")
        assert "lambda done_n, total: report(" in src
