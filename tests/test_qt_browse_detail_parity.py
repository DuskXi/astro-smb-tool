"""独立验收的第二批:右键菜单、详情缺的两组、删除确认、分析占用。

这几条的共同点是**老 UI 有、Qt 没有**,而且都不是"少一个好看的东西":

* 右键菜单里的「下载到…」与「复制到剪贴板」在 Qt 里**全前端找不到等价入口**
  —— 所以"工具栏按钮已经覆盖同样的动作"这个理由只成立 5 项。
* 「文件」组里的**尺寸**(6248 × 4176)与精确字节数,是判断"这一批 sub 是不是
  同一设置拍的"最直接的两个数。
* 删除确认给的是英文 Yes/No 且没有安全默认项 —— "Yes" 不告诉你 Yes 什么。
"""
from __future__ import annotations

from pathlib import Path

import pytest
from tests.support import tr

ROOT = Path(__file__).resolve().parents[1]
BROWSER = (ROOT / "astro_smb_qt" / "pages" / "browser.py").read_text(
    encoding="utf-8")
BASE = (ROOT / "astro_smb_qt" / "pages" / "base.py").read_text(encoding="utf-8")


def _body(src: str, name: str) -> str:
    at = src.index(f"def {name}")
    end = src.find("\n    def ", at + 10)
    return src[at:end if end > 0 else len(src)]


class TestFileAndPlaceGroups:
    """详情整整少两组。数据构造放共享层 —— 三套前端一份实现。"""

    def _entry(self):
        from astro_smb.backend import make_backend

        root = ROOT / ".tmp" / "device" / "EMMC Images"
        if not root.is_dir():
            pytest.skip("没有离线镜像")
        be = make_backend(kind="local", host=str(root))
        be.connect()
        share = be.list_shares()[0].name
        return next(e for e in be.listdir(share, "Plan/Light/IC 4603")
                    if e.name.endswith(".fit"))

    def test_shared_layer_builds_them(self):
        from astro_smb_app.views import browser as bv

        groups = bv.file_groups(self._entry(), host="192.0.2.227",
                                image_size=(6248, 4176))
        names = [n for _g, n, _p in groups]
        assert names == [tr("文件"), tr("位置")], names

    def test_size_has_exact_bytes(self):
        from astro_smb_app.views import browser as bv

        pairs = dict(bv.file_groups(self._entry())[0][2])
        assert tr("字节") in pairs[tr("大小")], (
            "大小只有人类可读那个 —— 判断一批 sub 是不是同一设置拍的,"
            "字节数比 49.78 MB 准")

    def test_image_size_is_included(self):
        from astro_smb_app.views import browser as bv

        pairs = dict(bv.file_groups(self._entry(), image_size=(6248, 4176))[0][2])
        assert pairs.get(tr("尺寸")) == "6248 × 4176"

    def test_no_image_size_no_row(self):
        from astro_smb_app.views import browser as bv

        pairs = dict(bv.file_groups(self._entry())[0][2])
        assert "尺寸" not in pairs, "没拿到尺寸就不该凭空写一行"

    def test_place_group_has_the_full_unc(self):
        from astro_smb_app.views import browser as bv

        pairs = dict(bv.file_groups(self._entry(), host="1.2.3.4")[1][2])
        assert pairs[tr("路径")].startswith("\\\\1.2.3.4\\"), pairs

    def test_page_renders_them(self):
        assert '"file_rows"' in BROWSER, "详情模型里没有文件/位置两组"
        body = _body(BROWSER, "_render_detail")
        assert 'model.get("file_rows")' in body, "算出来了却没画"


class TestCopyAllAndRawHeader:

    def test_both_buttons_exist(self):
        body = _body(BROWSER, "_render_detail")
        assert "复制全部信息" in body
        assert "完整 FITS 头" in body

    def test_raw_header_button_only_when_there_is_one(self):
        body = _body(BROWSER, "_render_detail")
        at = body.index("完整 FITS 头")
        assert 'model.get("header_lines")' in body[:at], (
            "没有 FITS 头的文件也摆一个点了没用的按钮")

    def test_copy_text_comes_from_the_shared_layer(self):
        body = _body(BROWSER, "_detail_model")
        assert "bv._detail_text(" in body, (
            "「复制全部信息」的文本自己拼了一份 —— 改一处会漏另一处")

    def test_raw_header_uses_a_dialog(self):
        """摊在面板里会把上面那几组判读整个顶出可视区。"""
        body = _body(BROWSER, "_show_header")
        assert "TextDialog" in body


class TestContextMenu:
    """老 UI 7 项,Qt 原来一项都没有。"""

    def test_menu_is_wired(self):
        assert "customContextMenuRequested.connect" in BROWSER
        assert "setContextMenuPolicy(Qt.CustomContextMenu)" in BROWSER

    def test_acts_on_the_row_under_the_pointer(self):
        body = _body(BROWSER, "_context_menu")
        assert "indexAt(pos)" in body, (
            "菜单作用于当前选中那一行 —— 右键第 5 行而第 1 行是选中的,"
            "「删除」就删错文件了")

    @pytest.mark.parametrize("item", [
        "下载", "下载到…", "复制到剪贴板", "重命名…", "删除", "复制 UNC 路径",
    ])
    def test_item_present(self, item: str):
        # **查的是 `addAction("…")` 而不是"这段文字里出现过"** ——
        # 第一版栽在这:文档字符串里恰好也写着「下载到…」,把那一行
        # `menu.addAction` 删掉之后断言照样成立。
        body = _body(BROWSER, "_context_menu")
        # 文案外面裹着 `_()`(i18n),所以查 `addAction(_("…` —— 但**仍然**
        # 是查这一个调用,不是"整段里出现过这几个字"
        assert f'addAction(_("{item}' in body, f"菜单里没有「{item}」"

    def test_the_two_exclusive_actions_have_handlers(self):
        """这两项在别处没有入口,不能只有菜单项没有实现。"""
        for name in ("_download_to", "_copy_to_clipboard"):
            assert f"def {name}" in BROWSER, f"{name} 没实现"

    def test_download_to_asks_for_a_folder(self):
        body = _body(BROWSER, "_download_to")
        assert "getExistingDirectory" in body

    def test_clipboard_puts_a_real_file(self):
        """资源管理器只认真实文件路径。"""
        body = _body(BROWSER, "_copy_to_clipboard")
        assert "setUrls" in body and "fromLocalFile" in body
        assert "download_file" in body, "没先落盘就往剪贴板塞路径"

    def test_clipboard_download_is_off_the_gui_thread(self):
        body = _body(BROWSER, "_copy_to_clipboard")
        assert "self.bg.run(" in body


class TestDeleteConfirm:

    def test_uses_the_chinese_confirm(self):
        body = _body(BROWSER, "_delete")
        # 要的是那个**控制流形状**:`if not self.confirm(...)` 之后 return。
        # 只查 `"self.confirm("` 的话,`if False and self.confirm(...)` 照样过。
        assert "if not self.confirm(" in body, (
            "还在用 QMessageBox.question —— 那是英文 Yes/No,"
            "而且一路回车就删了")
        assert 'ok_text=_("删除")' in body, (
            "确认按钮写的是「确定」——「确定」不告诉你确定什么")

    def test_message_says_it_is_permanent(self):
        body = _body(BROWSER, "_delete")
        assert "不可恢复" in body

    def test_confirm_is_one_implementation(self):
        """`Page.confirm` 与 `Shell.confirm` 必须转调同一份。

        原来只有 `Page` 有它,而 `Shell._set_language` 里就写着
        `self.confirm(...)` —— **语言切换点一下直接 AttributeError**。
        复制第二份是更糟的修法:两边的默认按钮迟早不一样。

        默认按钮与角色那两条判据由 `test_qt_skymap.py` 的
        `test_confirm_defaults_to_cancel` 按行为验。
        """
        shell = (ROOT / "astro_smb_qt" / "shell.py").read_text(encoding="utf-8")
        for name, src in (("base.py", BASE), ("shell.py", shell)):
            body = _body(src, "confirm")
            assert "W.confirm(" in body, f"{name} 里的 confirm 不是转调"


class TestAnalyzeShortcut:

    def test_button_exists(self):
        assert "分析占用" in BROWSER

    def test_it_carries_the_current_directory(self):
        body = _body(BROWSER, "_analyze")
        assert "page.share, page.path" in body, (
            "只是跳过去而不带当前目录 —— 用户还要再选一遍共享、再点一次扫描")
        assert "rescan()" in body, "跳过去了却不开扫"


class TestSublineLazyMeta:
    """列表副行只有「目标 · 类型 · 曝光」,老 UI 还有滤镜/序号/增益/温度。

    少掉的正是**区分同一目标不同批次**要看的那几个。判读与拼串全在共享层
    (`views.browser._hdr_suffix`),这边缺的只是"去读"这一步。
    """

    def test_the_page_starts_it(self):
        body = _body(BROWSER, "_apply_entries")
        assert "_start_fits_meta()" in body, "列完目录不去补副行"

    def test_it_uses_the_shared_formatter(self):
        body = _body(BROWSER, "_start_fits_meta")
        assert "bv._hdr_suffix(" in body, (
            "副行自己拼了一份 —— 判读口径会和另外两套前端漂开")

    def test_generation_guard(self):
        """视图一变这一轮必须作废,否则 A 目录的增益会填到 B 目录的行上。"""
        body = _body(BROWSER, "_start_fits_meta")
        assert "self.bg.generation" in body
        assert body.count("self.bg.generation") >= 2, (
            "只取了一次世代号却不比对 —— 等于没有守卫")

    def test_stops_after_repeated_failures(self):
        """连续失败多半是连接断了,继续读只会堆一串超时。"""
        body = _body(BROWSER, "_start_fits_meta")
        assert "fails" in body and "return out" in body

    def test_results_are_cached(self):
        body = _body(BROWSER, "_start_fits_meta")
        assert "cache" in body, "回到同一个目录会重读一遍几百个头"
        assert "e.mtime" in body, (
            "缓存键不含 mtime —— 文件被改过之后拿的还是旧头")

    def test_writes_into_the_name_column(self):
        body = _body(BROWSER, "_apply_fits_meta")
        assert "bv.NAME_COL" in body, (
            "列下标写死了 —— 列一调整,增益就会去改夜次徽章那一列")

    def test_name_col_points_at_the_name(self):
        from astro_smb_app.views import browser as bv

        assert bv.ROW_COLS[bv.NAME_COL] == "*", (
            "NAME_COL 指的不是那根伸缩的名字列")

    def test_it_keeps_the_base_subline(self):
        """补上来的部分要接在原副行**后面**,不能把目标名冲掉。"""
        body = _body(BROWSER, "_apply_fits_meta")
        assert "astro_subline" in body and "base" in body


class TestCheckModeDoesNotSquashTheIcon:
    """勾选框画在第一列左边距里,而那一列只有 22px。"""

    def test_column_widens(self):
        pytest.importorskip("PySide6")
        from PySide6.QtWidgets import QApplication

        from astro_smb_app.views import browser as bv
        from astro_smb_qt import theme, widgets as W

        app = QApplication.instance() or QApplication([])
        theme.apply(app)
        t = W.DataTable(bv.ROW_COLS, multi=True)
        t.set_rows([{"key": "a", "cells": [{"text": "◉"}, {"text": "07-25"},
                                           {"text": "x"}, {"text": "1"},
                                           {"text": "2"}]}])
        narrow = t.columnWidth(0)
        t.set_check_mode(True)
        wide = t.columnWidth(0)
        assert wide - narrow >= W._CellDelegate.BOX, (
            f"勾选模式下第一列没加宽({narrow}→{wide}) —— 类型图标会被"
            f"挤成一条两像素的竖线")

    def test_restores_when_off(self):
        pytest.importorskip("PySide6")
        from PySide6.QtWidgets import QApplication

        from astro_smb_app.views import browser as bv
        from astro_smb_qt import theme, widgets as W

        app = QApplication.instance() or QApplication([])
        theme.apply(app)
        t = W.DataTable(bv.ROW_COLS, multi=True)
        t.set_rows([{"key": "a", "cells": [{"text": "◉"}] * 5}])
        base = t.columnWidth(0)
        t.set_check_mode(True)
        t.set_check_mode(False)
        assert t.columnWidth(0) == base, "关掉勾选模式后第一列没缩回去"
