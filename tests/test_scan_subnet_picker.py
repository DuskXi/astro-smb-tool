"""扫描页的网段选择:下拉 + 手填 + 任意掩码,以及「换网段就卡」那个 bug。

四件事,前三件是用户提的需求,第四件是他撞上的现象:

1. 下拉列出**本机网卡的**网段,手填也照收;
2. 网络变了(插网线 / 连 Wi-Fi / 开关 VPN)候选自动跟着变;
3. 掩码不是 /24 也能写 —— 办公室、宿舍常见 /22;
4. **扫完一个网段再换一个会卡。**
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal          # noqa: E402

from astro_smb import netscan as N                  # noqa: E402


class _Shell(QObject):
    connected = Signal(list)
    theme_changed = Signal()
    heartbeat = Signal(dict)
    watch = Signal(dict)

    def __init__(self):
        super().__init__()
        self.conn = {"host": ""}
        self.client_factory = None
        self.notices: list[str] = []

    def notice(self, text, tone="warn"):
        self.notices.append(text)


@pytest.fixture(scope="module")
def qt_app():
    from PySide6.QtWidgets import QApplication

    from astro_smb_qt import theme

    inst = QApplication.instance() or QApplication([])
    theme.apply(inst)
    return inst


@pytest.fixture
def page(qt_app, monkeypatch):
    monkeypatch.setattr(N, "local_networks",
                        lambda: ["192.0.2.0/24", "198.51.100.0/22"])
    from astro_smb_qt.pages.scan import ScanPage

    p = ScanPage(_Shell())
    yield p
    p.on_close()


class TestThePickerOffersTheRealNics:

    def test_it_lists_the_local_networks(self, page):
        got = [page.subnet.itemText(i) for i in range(page.subnet.count())]
        assert got == ["192.0.2.0/24", "198.51.100.0/22"], got

    def test_it_is_still_typable(self, page):
        """下拉不能把手填挤掉 —— 设备在别的网段是常事(跨 VLAN、直连)。"""
        assert page.subnet.isEditable()
        page._set_target("198.51.100.0/23")
        assert page.target_text() == "198.51.100.0/23"

    def test_typing_does_not_get_added_to_the_list(self, page):
        """手填的不进候选列表,否则列表会被历史输入撑满。"""
        before = page.subnet.count()
        page._set_target("198.51.100.0/23")
        assert page.subnet.count() == before

    def test_it_says_how_big_the_scan_will_be(self, page):
        page._set_target("198.51.100.0/22")
        assert "1022" in page.target_note.text()

    def test_it_says_when_it_cannot_parse(self, page):
        page._set_target("nope")
        assert page.target_note.text()

    def test_a_huge_range_says_it_will_be_truncated(self, page):
        """`/16` 是 65534 个地址。**要说出来只扫了一部分** ——
        否则"没找到"会被读成"这段里没有设备"。"""
        page._set_target("198.18.0.0/16")
        note = page.target_note.text()
        assert "65534" in note and str(N.MAX_HOSTS) in note


class TestItFollowsTheNetworkChanging:
    """网络会变。只在第一次显示时取一次的话,换了网之后下拉里还是老几项 ——
    用户选中的那个网段早就不在了,扫出来什么都没有,而界面不说为什么。"""

    def test_new_nics_show_up(self, page, monkeypatch):
        monkeypatch.setattr(N, "local_networks",
                            lambda: ["192.0.2.0/24", "198.51.100.0/22",
                                     "203.0.113.0/24"])
        page.refresh_networks()
        got = [page.subnet.itemText(i) for i in range(page.subnet.count())]
        assert "203.0.113.0/24" in got

    def test_it_does_not_clobber_what_the_user_is_typing(self, page,
                                                         monkeypatch):
        """**刷新不能把人正在敲的字吃掉。**"""
        page._set_target("198.51.100.0/24")
        monkeypatch.setattr(N, "local_networks",
                            lambda: ["192.0.2.0/24", "203.0.113.0/24"])
        page.refresh_networks()
        assert page.target_text() == "198.51.100.0/24"

    def test_nothing_changes_when_the_nics_did_not(self, page):
        """没变就别动列表 —— 每 4 秒重建一次会打断下拉的展开与输入。"""
        page._set_target("198.51.100.0/22")
        page.refresh_networks()
        assert page.target_text() == "198.51.100.0/22"

    def test_there_is_a_timer_driving_it(self, page):
        assert page._net_timer.isActive()


class TestSwitchingSubnetsDoesNotWedge:
    """**用户报的:扫完一个网段再换一个会卡。**

    取消用的是 `self._busy` 这**一个共享变量**:停止时置 False,而 worker 里
    `cancel=lambda: not self._busy` 读的就是它。换个网段再点开始时 `_busy`
    又变回 True —— 上一趟 worker 眼里「取消」被撤销了,它继续扫完整整 254 个
    地址。两趟同时占着线程池(每趟 64 条连接),界面就卡在那儿。

    世代(`bg.bump()`)挡得住回调,**挡不住已经在跑的线程** —— 而挡不住的
    那部分正好是最费资源的那部分。
    """

    def test_each_scan_gets_its_own_token(self, page, monkeypatch):
        seen = []
        monkeypatch.setattr("astro_smb_qt.pages.scan.discover",
                            lambda *a, **k: seen.append(k.get("cancel")) or [])
        page._set_target("192.0.2.0/24")
        page.toggle()
        first = page._token
        page._stop()
        page._set_target("198.51.100.0/22")
        page.toggle()
        assert page._token is not first, "两趟共用一个取消标志"

    def test_stopping_is_permanent(self, page):
        """**这条是那个 bug 本身。** 停掉之后再开一趟,第一趟的标志不许
        被"复活"。"""
        page._set_target("192.0.2.0/24")
        page.toggle()
        first = page._token
        page._stop()
        assert first.is_set(), "停止没有置上标志"

        page._set_target("198.51.100.0/22")
        page.toggle()
        assert first.is_set(), (
            "开新一趟把上一趟的取消撤销了 —— 它会继续扫完,两趟一起占线程池")

    def test_leaving_the_page_stops_the_scan(self, page):
        page._set_target("192.0.2.0/24")
        page.toggle()
        tok = page._token
        page.on_close()
        assert tok.is_set(), "离开页面扫描还在跑,用户既看不到也停不了"

    def test_the_progress_denominator_follows_the_target(self, page):
        """/22 的进度条分母得是 1022,不是写死的 254 —— 否则条子跑到
        四分之一就满了,再也不动。"""
        page._set_target("198.51.100.0/22")
        page.toggle()
        assert page.bar.maximum() == 1022, page.bar.maximum()
        page._stop()


class TestEveryComboLooksLikeACombo:
    """**全项目的下拉都没有箭头,长得和输入框一模一样。**

    `theme.py` 里写着 `QComboBox::drop-down {{ border: none; width: 18px }}`——
    给这个子控件写过样式之后 Qt 就不再画原生箭头了,而那条规则没补一个。
    于是设备下拉、共享下拉、语言下拉、并发档位……用户都不知道能点开。

    扫描页这个可编辑的最致命:它既能选也能填,而看上去只能填。

    试过纯 QSS 画三角(宽高 0 + 三条边框),Qt 不吃 —— 画出来是个实心
    小方块,比没有更糟。结论是**别碰这两个子控件**,让 Qt 按平台画。
    """

    def test_the_stylesheet_does_not_kill_the_arrow(self):
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1] / "astro_smb_qt"
               / "theme.py").read_text(encoding="utf-8")
        code = "\n".join(l for l in src.splitlines()
                         if not l.strip().startswith(("#", "*", "/*")))
        for bad in ("QComboBox::drop-down", "QComboBox::down-arrow"):
            assert bad not in code, (
                f"{bad} 又被写了样式 —— Qt 会停掉原生箭头,"
                "而纯 QSS 补不出一个像样的三角")

    def test_the_arrow_is_actually_painted(self, qt_app):
        """**验像素。** 上一条只管样式表里没写死;这条真画一遍,
        看右侧那一段有没有东西 —— 空的就是又没箭头了。"""
        from PySide6.QtWidgets import QComboBox

        from astro_smb_qt import theme

        cb = QComboBox()
        cb.addItems(["192.0.2.0/24"])
        cb.setStyleSheet(theme.stylesheet())
        cb.resize(200, 28)
        img = cb.grab().toImage()

        # 右边 20px 里应当出现至少两种颜色(背景 + 箭头)
        seen = {img.pixel(x, y)
                for x in range(img.width() - 20, img.width() - 4)
                for y in range(6, img.height() - 6)}
        assert len(seen) > 1, "下拉右侧是一片纯色 —— 箭头没画出来"
