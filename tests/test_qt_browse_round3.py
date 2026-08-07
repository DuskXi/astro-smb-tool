"""独立验收第三轮判"不过"的三条 + 顺带记在案的几处差异。

三条里有两条是这份清单立起来的**原型**:界面照常、不报错,只是内容/数字
悄悄是错的 ——

* 「完整 FITS 头」对话框照弹,里面是一列光秃秃的关键字,值全没了;
* 方位角显示 180°,而日志明明能推出 121.37°E、该显示 182°。

第三条(行首符号只有老 UI 的 ~60%)是纯视觉的,但它前一轮"修过一次"——
把字号从 12 抬到 16 —— 只修对了一半,这种"看起来动了"的半吊子修复最容易
被当成已完成。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

ROOT = Path(__file__).resolve().parents[1]
QT = ROOT / "astro_smb_qt"
BROWSER = QT / "pages" / "browser.py"


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


class TestFitsHeaderDialog:
    """1.d14:`FitsHeader.cards` 是 dict,遍历它拿到的是**键**。"""

    def _hdr(self):
        from astro_smb.fitshdr import FitsHeader

        h = FitsHeader()
        for k, v in (("SIMPLE", "T"), ("BITPIX", "16"), ("NAXIS", "2"),
                     ("NAXIS1", "6248"), ("EXPTIME", "300.0")):
            h.cards[k] = v
            h.order.append((k, v, ""))
        return h

    def test_values_are_present(self):
        """**这一条是行为断言**:查源码写没写 `order` 挡不住"写了但没用上"。"""
        from astro_smb_qt.pages.browser import _header_lines

        lines = _header_lines(self._hdr())
        assert lines, "一行都没有"
        assert all("=" in ln for ln in lines), (
            f"有行没有等号 —— 那就是只有键名: {lines[:3]}")
        joined = "\n".join(lines)
        for want in ("6248", "300.0", "16"):
            assert want in joined, f"值 {want} 没出现:{lines}"

    def test_keys_are_padded_like_the_old_ui(self):
        from astro_smb_qt.pages.browser import _header_lines

        assert _header_lines(self._hdr())[0].startswith("SIMPLE  = ")

    def test_order_is_the_file_order(self):
        from astro_smb_qt.pages.browser import _header_lines

        lines = _header_lines(self._hdr())
        assert lines[0].startswith("SIMPLE") and lines[1].startswith("BITPIX")

    def test_no_header_means_no_lines(self):
        """没头就是空表 —— 按钮跟着不出现,而不是弹一个空对话框。"""
        from astro_smb.fitshdr import FitsHeader
        from astro_smb_qt.pages.browser import _header_lines

        assert _header_lines(None) == []
        assert _header_lines(FitsHeader()) == []

    def test_it_does_not_iterate_cards(self):
        """`cards` 是 dict —— 一旦有人改回去,上面的行为断言会红,
        但这条能直接指出**为什么**。"""
        src = _src(BROWSER, "_header_lines")
        assert "getattr(fits, 'order'" in src, "没读 order"
        assert "cards" not in src.replace("`cards`", ""), (
            "又去遍历 cards 了 —— 那是 dict,遍历出来是键")


class TestLogStoreHostIdentity:
    """1.d15:同一台设备两种拼法,`LogStore.data` 的守卫每次都命中。"""

    def test_slashes_do_not_change_identity(self):
        from astro_smb_app.logstore import host_key

        assert host_key("C:/Users/x/.tmp/device/EMMC Images") == \
            host_key("C:\\Users\\x\\.tmp\\device\\EMMC Images")

    def test_trailing_separator_does_not_either(self):
        from astro_smb_app.logstore import host_key

        assert host_key("E:\\ASIAIR\\") == host_key("E:\\ASIAIR")

    def test_different_devices_stay_different(self):
        """归一化不能把两台设备并成一台 —— 那是**更糟**的 bug
        (跨设备 serve 陈旧日志,这个守卫本来就是为它设的)。"""
        from astro_smb_app.logstore import host_key

        assert host_key("192.0.2.227") != host_key("192.0.2.228")
        assert host_key("E:\\ASIAIR") != host_key("F:\\ASIAIR")
        assert host_key("") == ""

    def test_data_survives_a_respelled_host(self):
        """**行为验证**:refresh 用规范串打标、bind 用原始串,数据仍取得到。"""
        from astro_smb_app.logstore import LogData, LogStore

        store = LogStore()
        store.bind("C:/Users/x/dev/EMMC Images")
        store._data = LogData()
        store._data_host = "C:\\Users\\x\\dev\\EMMC Images"
        assert store.data is not None, (
            "同一台设备换个拼法就被守卫挡掉 —— 页面永远看到 None,"
            "经度退回兜底,方位角悄悄错 2°")

    def test_a_real_device_change_still_invalidates(self):
        from astro_smb_app.logstore import LogData, LogStore

        store = LogStore()
        store.bind("192.0.2.227")
        store._data = LogData()
        store._data_host = "192.0.2.228"
        assert store.data is None, "换了设备还把上一台的日志交出去"

    def test_rebinding_the_same_device_keeps_the_cache(self):
        """重连同一台设备(换个拼法)不该把已解析的日志全丢掉重来。"""
        from astro_smb_app.logstore import LogData, LogStore

        store = LogStore()
        store.bind("C:/dev/EMMC Images")
        store._data = LogData()
        store._data_host = "C:\\dev\\EMMC Images"
        assert store.bind("C:\\dev\\EMMC Images\\") is False, "被当成换设备了"
        assert store.data is not None, "缓存被清了"

    def test_a_real_switch_reports_true(self):
        from astro_smb_app.logstore import LogStore

        store = LogStore()
        store.bind("192.0.2.227")
        assert store.bind("192.0.2.228") is True


class TestLogShareDetection:
    """D2:共享名写死常量,插卡设备(共享名是卷标)找不到 log/。"""

    class _Backend:
        def __init__(self, has_log: str):
            self.has_log = has_log

        def exists(self, share: str, path: str) -> bool:
            return share == self.has_log and path == "log"

    def test_picks_the_share_that_has_log(self):
        from astro_smb_app.logstore import detect_log_share

        got = detect_log_share(self._Backend("ASIAIR_TF"),
                               ["Preview", "ASIAIR_TF", "Other"])
        assert got == "ASIAIR_TF"

    def test_accepts_objects_with_a_name(self):
        """两套前端拿到的形状不同(字符串 / 带 .name 的对象)。"""
        from astro_smb_app.logstore import detect_log_share

        class _S:
            def __init__(self, name):
                self.name = name

        assert detect_log_share(self._Backend("VOL"),
                                [_S("A"), _S("VOL")]) == "VOL"

    def test_falls_back_to_the_first_share(self):
        from astro_smb_app.logstore import detect_log_share

        assert detect_log_share(self._Backend("none"), ["A", "B"]) == "A"

    def test_survives_a_backend_that_raises(self):
        from astro_smb_app.logstore import detect_log_share

        class _Boom:
            def exists(self, *_a):
                raise OSError("设备掉线")

        assert detect_log_share(_Boom(), ["A", "B"]) == "A"

    def test_shell_actually_calls_it(self):
        src = (QT / "shell.py").read_text(encoding="utf-8")
        assert "detect_log_share(client, shares)" in src, (
            "shell 没探测日志共享 —— LogStore.share 会停在常量")

    def test_shell_binds_the_backend_host(self):
        """绑后端给的规范 host,不是用户原样输入的那串。"""
        src = _src(QT / "shell.py", "_on_connected")
        assert "logstore.bind(real_host" in src, (
            "又去绑 conn['host'] 了 —— 那是用户原样输入的串")

    def test_watcher_gets_the_share_too(self):
        """watcher 也照着 `<share>/Plan/Light` 找帧,共享名不对就永远找不到。"""
        src = _src(QT / "shell.py", "_start_watcher")
        assert "watcher_share" in src and ".share = share" in src


class TestRowGlyphInk:
    """复验点 6:抬字号抬不动墨占比。

    **量度是注入的。** offscreen 平台的字体库对每个字都返回"墨高 = em",
    真机上 `▣` 的墨高只有 em 的一半 —— 拿平台字体做断言的话,放大分支
    永远走不到,几条断言全部空转(反向验证里活了三条才发现这件事)。
    这里用一个模拟"墨占比 50%、advance 也是半个 em"的量度,把逻辑本身钉死。
    """

    @staticmethod
    def _half_ink(_ch, _base, px):
        """真机上 `▢▣▤◉◍` 的形状:墨和步进都只有半个 em。"""
        return px // 2, px // 2

    @staticmethod
    def _full_ink(_ch, _base, px):
        """填满 em 的图标字体(老 UI 的 FontIcon 就是这种)。"""
        return px, px

    def test_it_scales_up_when_ink_is_half_the_em(self, qt_app):
        from PySide6.QtGui import QFont

        from astro_smb_qt import widgets as W

        got = W.glyph_px("▣", 16, QFont(), measure=self._half_ink)
        assert got == 32, (
            f"墨占比 50% 时该把字号翻倍才能得到 16px 的墨迹,实际 {got}")

    def test_it_leaves_a_full_ink_font_alone(self, qt_app):
        from PySide6.QtGui import QFont

        from astro_smb_qt import widgets as W

        assert W.glyph_px("▣", 16, QFont(), measure=self._full_ink) == 16, (
            "本来就填满 em 的字体还去放大 —— 那会撑破行高")

    def test_it_never_shrinks_below_the_target(self, qt_app):
        from PySide6.QtGui import QFont

        from astro_smb_qt import widgets as W

        big = lambda _c, _b, px: (px * 2, px)      # noqa: E731
        assert W.glyph_px("▣", 16, QFont(), measure=big) == 16

    def test_it_does_not_run_away(self, qt_app):
        """墨高量成 1px 时不能放成 16 倍 —— 夹在 3× 以内。"""
        from PySide6.QtGui import QFont

        from astro_smb_qt import widgets as W

        tiny = lambda _c, _b, px: (1, 1)           # noqa: E731
        assert W.glyph_px("▣", 16, QFont(), measure=tiny) == 48

    def test_unmeasurable_falls_back_to_the_target(self, qt_app):
        from PySide6.QtGui import QFont

        from astro_smb_qt import widgets as W

        zero = lambda _c, _b, px: (0, 0)           # noqa: E731
        assert W.glyph_px("▣", 16, QFont(), measure=zero) == 16

    def test_width_clamp_is_respected(self, qt_app):
        """列宽是硬约束 —— 超了会被 `elidedText` 换成一个省略号。"""
        from PySide6.QtGui import QFont

        from astro_smb_qt import widgets as W

        # 不夹宽度会给 32(见上一条)。这里可用宽度只有 10,而
        # advance = px // 2 —— 从 32 一路降到 21 时 advance 才 ≤10(21//2=10)。
        got = W.glyph_px("▣", 16, QFont(), max_w=10, measure=self._half_ink)
        assert got == 21, f"没有按可用宽度夹紧:{got}(advance 会是 {got // 2})"
        assert got // 2 <= 10

    def test_clamp_never_goes_below_the_target(self, qt_app):
        """列窄到放不下 target 时也不能再缩 —— 缩下去就什么都看不清了。"""
        from PySide6.QtGui import QFont

        from astro_smb_qt import widgets as W

        assert W.glyph_px("▣", 16, QFont(), max_w=2,
                          measure=self._half_ink) == 16

    def test_the_symbol_column_is_wide_enough(self):
        """22px 减两边内边距只剩 10px —— 老 UI 的墨迹就有 17px 宽。"""
        from astro_smb_app.views import browser as bv

        from astro_smb_qt.widgets import _CellDelegate

        inner = int(bv.ROW_COLS[0]) - _CellDelegate.PAD_X * 2
        assert inner >= 16, (
            f"符号列可用宽度只有 {inner}px,放不下一个和文字等高的图标")

    def test_glyph_cells_are_not_elided(self):
        """一个被省略的符号就是一个 `…`,比画小了糟得多。

        **判据走 AST**:按子串查会撞上 `f.setPixelSize(...)` 里那个换行处的
        `if cell.get("glyph")`(反向验证里就是这么活下来的)。这里直接找
        `shown` 那次赋值,要求它的右边是个"按 glyph 分支"的条件表达式。
        """
        import ast

        tree = ast.parse((QT / "widgets.py").read_text(encoding="utf-8"))
        found = []
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "shown"):
                found.append(node)
        assert found, "delegate 里没有 `shown` 这次赋值了"
        src = ast.unparse(found[0].value)
        assert src.startswith("text if"), f"符号仍然走省略:{src}"
        assert "glyph" in src

    def test_delegate_asks_for_the_scaled_size(self):
        src = (QT / "widgets.py").read_text(encoding="utf-8")
        at = src.index("class _CellDelegate")
        body = src[at:src.index("\n\nclass ", at + 10)]
        assert "glyph_px(" in body, "delegate 没有按墨迹反算字号"
        assert "max_w=" in body, "没把列宽交给它 —— 符号会被省略号顶掉"

    def test_results_are_cached(self, qt_app):
        """一屏几十行,每次 paint 都量字体度量太亏。

        **要数的是"默认路径量了几次"。** 前两版都测歪了:
        数字典大小 —— 把缓存查询短路掉之后写入照做,大小纹丝不动;
        数注入量度的调用次数 —— 注入了就根本不走缓存那条分支。
        只有把 `_qt_measure` 本身换成计数器,才量得到默认路径的行为。
        """
        from PySide6.QtGui import QFont

        from astro_smb_qt import widgets as W

        calls = []
        orig = W._qt_measure

        def counting(ch, base, px):
            calls.append(px)
            return px // 2, px // 2

        W._qt_measure = counting
        try:
            W._GLYPH_PX.clear()
            W.glyph_px("▤", 16, QFont(), max_w=18)
            first = len(calls)
            assert first, "根本没量"
            W.glyph_px("▤", 16, QFont(), max_w=18)
            assert len(calls) == first, (
                f"同一个键又量了 {len(calls) - first} 次 —— 缓存没生效")
        finally:
            W._qt_measure = orig


class TestSmallerFindings:

    def test_every_detail_group_has_an_icon(self):
        """缺键时静默退化成 `·` —— 七组里有两组是个点,不报错。

        **这条自己被 i18n 打过一次脸。** 原来扫的是 ``(_GRP_X, "组名"`` 这个
        正则,拿组名去 `GROUP_GLYPHS` 里查 —— 组名一包上 `_()`,正则一条都
        扫不到,而它开头那句 ``assert names`` 正好把这事儿抓住了。
        (查表本身也一样错:组名会被翻译。现在按**码位**查。)
        """
        import ast

        from astro_smb_qt import widgets as W

        src = (ROOT / "astro_smb_app" / "views"
               / "browser.py").read_text(encoding="utf-8")
        glyphs = {n.value.value for n in ast.parse(src).body
                  if isinstance(n, ast.Assign)
                  and isinstance(n.targets[0], ast.Name)
                  and n.targets[0].id.startswith("_GRP_")
                  and isinstance(n.value, ast.Constant)}
        assert glyphs, "一个分组都没扫到 —— 这条断言没在测东西"
        missing = sorted(f"U+{ord(g):04X}" for g in glyphs
                         if g not in W.SEGOE_GLYPHS)
        assert not missing, f"这些分组图标没有跨平台替身: {missing}"

    def test_sky_group_has_one_too(self):
        """天球那一组是页面自己写死的标题(共享层不产出它),所以有专门的常量。

        **图标要真的传进去** —— 常量存在但调用点忘了传,`GroupHeader` 会
        退回一个点,而那正是这一组当初被漏掉时的样子。
        """
        from astro_smb_qt import widgets as W

        assert W.GLYPH_SKY and all(ord(c) <= 0xFFFF for c in W.GLYPH_SKY)
        src = (ROOT / "astro_smb_qt" / "pages"
               / "browser.py").read_text(encoding="utf-8")
        assert "W.GLYPH_SKY" in src, "常量有了,页面没传"

    def test_group_glyphs_stay_in_the_bmp(self):
        """星平面字符会让 win32more 那侧少画一个字(docs/DEVELOPMENT.md §7.1),
        这套词表两个前端共用,规矩一起守。"""
        from astro_smb_qt import widgets as W

        vals = list(W.SEGOE_GLYPHS.values()) + [
            W.GLYPH_SKY, W.GLYPH_STRUCTURE, W.GLYPH_SOLVE, W.GLYPH_STATS]
        bad = [g for g in vals if any(ord(c) > 0xFFFF for c in g)]
        assert not bad, f"这些图标不在 BMP: {bad}"

    def test_volume_shows_a_bar_and_a_percentage(self):
        src = _src(BROWSER, "_load_volume")
        assert "volume_bar.set_frac" in src, "卷容量没有量条"
        assert "pct:.0f" in src, "没有百分比 —— 绝对数字要心算才知道快满没"

    def test_volume_uses_the_shared_properties(self):
        """`used`/`percent` 在 `VolumeInfo` 上,别在页面里重算一遍。"""
        src = _src(BROWSER, "_load_volume")
        assert "vol.used" in src and "vol.percent" in src

    def test_volume_bar_warns_when_nearly_full(self):
        src = _src(BROWSER, "_load_volume")
        assert "'bad'" in src and "'warn'" in src, "卡快满了没有变色"

    def test_dialogs_have_their_own_background(self, qt_app):
        """`QWidget {background: transparent}` 把对话框也刷成透明了 ——
        浅色字落在浅色系统底上,正文几乎看不见。"""
        from astro_smb_qt import theme

        for mode in theme.MODES:
            before = theme.C.mode
            try:
                theme.set_mode(mode)
                qss = theme.stylesheet()
                assert "QDialog, QMessageBox {" in qss, f"{mode} 档对话框没底色"
                assert "QMessageBox QLabel" in qss, f"{mode} 档对话框正文没字色"
            finally:
                theme.set_mode(before)
