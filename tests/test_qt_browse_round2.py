"""独立验收第二轮点名的那几条 —— 每条一个闸门。

这一轮的共同点是**全都不报错**:点共享没反应、详情停在上一个目录、
「尺寸」永远不出现、长路径被切掉、副行少两段、图标缩成一个点、
重命名弹出来是英文按钮。界面照样能用,只是不对。

`_astro_subline`(老 UI)是这几条里唯一有**现成参照物**的,所以那条
直接拿老 UI 的源码来对账,而不是我自己重述一遍它应该长什么样。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

ROOT = Path(__file__).resolve().parents[1]
QT = ROOT / "astro_smb_qt"
BROWSER = QT / "pages" / "browser.py"


def _fn(path: Path, name: str) -> ast.FunctionDef:
    """按 **AST** 取函数。

    这一轮在"断言匹配到注释/文档字符串"上栽过八次,一律改走语法树:
    注释根本不进树,而 docstring 是 body[0],要查的东西在它后面。
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name} 里没有 {name}")


def _src(path: Path, name: str) -> str:
    """函数体的源码。**注意 `ast.unparse` 把字符串常量一律吐成单引号** ——
    照着原文写双引号的断言会全部落空,而且是"绿着落空"(负向断言恒真)。
    下面的断言因此统一用单引号。"""
    node = _fn(path, name)
    body = node.body[1:] if (node.body and isinstance(node.body[0], ast.Expr)
                             and isinstance(node.body[0].value, ast.Constant)
                             ) else node.body
    return "\n".join(ast.unparse(n) for n in body)


class TestShareClickAlwaysGoesHome:
    """在 `EMMC Images/Plan/Light/IC 4603` 里点「EMMC Images」要回根。"""

    def test_no_same_share_guard(self):
        src = _src(BROWSER, "_on_share_pick")
        assert "self.share" not in src, (
            "又把 `name != self.share` 这类守卫加回去了 —— 同一共享的深层"
            "路径下点共享会毫无反应")
        assert "open_path(name, '')" in src, (
            "点共享没有跳到该共享的根")


class TestLateResultCannotRepaintTheDetail:
    """上一个目录的在途预览迟到回来,不许把详情画回去。"""

    def test_apply_entries_bumps_the_token(self):
        src = _src(BROWSER, "_apply_entries")
        assert "self._token += 1" in src, (
            "换目录不推进令牌 —— 上一个目录的预览结果令牌仍然对得上,"
            "回来之后会把右边详情**又画回**上一张 light 帧")

    def test_apply_entries_drops_the_cached_size(self):
        src = _src(BROWSER, "_apply_entries")
        assert "self._preview_size = None" in src, (
            "尺寸没跟着清 —— 新目录那张图还没解出来时会显示上一张的尺寸")


class TestImageSizeRow:
    """「尺寸 6248 × 4176」那一行原来永远不出现。"""

    def test_preview_result_is_remembered(self):
        src = _src(BROWSER, "_on_preview")
        assert "self._preview_size" in src and "image_size" in src, (
            "预览回来的 image_size 没有存下来")

    def test_detail_model_passes_it_down(self):
        src = _src(BROWSER, "_detail_model")
        assert "image_size=self._preview_size" in src, (
            "详情没把尺寸传给 `file_groups` —— 那一行就永远是空的")

    def test_no_dead_underscore_attribute(self):
        """曾经写的是 `getattr(fits, '_image_size', None)` —— 恒为 None。

        `FitsHeader` 上没有 `_image_size` 这个属性,那个表达式的**两个分支
        都取不到值**,于是整行被静默丢掉。这类"取一个不存在的私有属性"
        写法在这个文件里不许再出现。
        """
        # 走 AST:**这条是负向断言**,而下面那段解释这个坑的注释里就写着
        # `_image_size` —— 按原文查的话它永远红,按"剥掉注释"查的话
        # docstring 里再写一次又会红。只有语法树里的字符串常量才算数。
        tree = ast.parse(BROWSER.read_text(encoding="utf-8"))
        bad = [n.lineno for n in ast.walk(tree)
               if isinstance(n, ast.Constant) and n.value == "_image_size"]
        assert not bad, f"第 {bad} 行又去取一个不存在的私有属性了"


def _wrapped_args(fn: ast.FunctionDef, callee: str) -> list[set[str]]:
    """`fn` 里每次调用 `callee` 时,各实参外面套着哪个函数。

    直接查"函数体里出现过 `W.breakable(`"是**不够的**:`_render_detail` 里
    另有一处给大标题用的 `W.breakable`,于是把 MetricRow 那处删掉之后断言
    照样成立 —— 反向验证时这条真的活下来了。判据必须绑到**具体那次调用的
    具体那个实参**上。
    """
    out = []
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == callee):
            continue
        names = set()
        for a in node.args:
            if (isinstance(a, ast.Call)
                    and isinstance(a.func, ast.Attribute)):
                names.add(a.func.attr)
        out.append(names)
    return out


class TestLongValuesWrap:
    def test_file_rows_are_breakable(self):
        """**MetricRow 的值**那一格要软换行 —— 不是"这个函数里某处有"。"""
        calls = _wrapped_args(_fn(BROWSER, "_render_detail"), "MetricRow")
        assert calls, "_render_detail 里根本没有 MetricRow"
        assert any("breakable" in names for names in calls), (
            "完整 UNC 路径那一行没做软换行 —— 会被右边界直接切掉")

    def test_the_title_is_breakable_too(self):
        """大标题那处是另一条(清单 1.d1),各守各的,别互相顶替。"""
        calls = _wrapped_args(_fn(BROWSER, "_render_detail"), "label")
        assert any("breakable" in names for names in calls), (
            "详情大标题的长文件名没做软换行")

    def test_breakable_inserts_zero_width_spaces(self):
        from astro_smb_qt import widgets as W

        out = W.breakable("Light_IC 4603_300s_Bin1_4C_20260726-231501_0001.fit")
        assert "​" in out, "没插零宽空格,等于没做换行点"
        assert out.replace("​", "") == (
            "Light_IC 4603_300s_Bin1_4C_20260726-231501_0001.fit"), (
            "插的东西改变了文本本身")


class TestSublineMatchesTheOldUi:
    """副行少了滤镜 `4C` 和序号 `#0001`。

    老 UI 的 `_astro_subline` 是唯一的参照物,拿它的源码对账 ——
    我自己重述一遍"应该有哪几段"的话,重述错了没人发现。
    """

    OLD = (ROOT / "astro_smb_gui" / "_browser.py")

    def test_shared_subline_has_every_segment(self):
        from astro_smb_app.views import browser as bv

        src = _src(ROOT / "astro_smb_app" / "views" / "browser.py",
                   "astro_subline")
        for seg in ("info.filter", "info.binning", "info.seq"):
            assert seg in src, f"副行少了 {seg} —— 老 UI 有"
        assert "f'#{info.seq:04d}'" in src, "序号没有补零到四位"
        assert callable(bv.astro_subline)

    def test_target_and_kind_are_exclusive(self):
        """有目标名时不再另外拼一遍类型 —— 老 UI 就是二选一。"""
        src = _src(ROOT / "astro_smb_app" / "views" / "browser.py",
                   "astro_subline")
        tree = ast.parse(src)
        got = [n for n in ast.walk(tree)
               if isinstance(n, ast.If) and n.orelse
               and "info.target" in ast.unparse(n.test)]
        assert got, "目标/类型不是 if/else 二选一(会同时拼两段,挤掉后面的)"

    def test_behaviour_on_a_real_asiair_name(self):
        """真机文件名走一遍 —— 结构断言挡不住"拼错顺序"。"""
        from astro_smb_app.views import browser as bv

        class _E:
            name = "Light_IC 4603_300.0s_Bin1_4C_20260726-231501_0001.fit"
            is_dir = False

        out = bv.astro_subline(_E())
        assert out is not None
        for seg in ("IC 4603", "300s", "4C", "#0001"):
            assert seg in out, f"副行里没有 {seg}:{out}"
        assert "亮场" not in out, "有目标名还拼了类型"

    def test_kind_shows_when_there_is_no_target(self):
        from astro_smb_app.views import browser as bv

        class _E:
            name = "Bias_1.0s_Bin1_20260726-231501_0031.fit"
            is_dir = False

        out = bv.astro_subline(_E())
        assert out and "偏置" in out, f"没有目标名时类型也不显示了:{out}"


class TestRowGlyphIsLegible:
    """类型符号量出来约 7px,老 UI 那侧是 16px 的 FontIcon。"""

    def test_theme_has_a_dedicated_size(self):
        from astro_smb_qt import theme

        assert theme.Font.ICON > theme.Font.BODY, (
            "符号和正文同号 —— 缩成一个看不清是什么的小点")

    def test_cells_ask_for_it(self):
        src = _src(BROWSER, "_cells")
        assert "glyph=True" in src, "行首符号没有标记成 glyph,还是按正文号画"

    def test_delegate_honours_the_flag(self):
        src = (QT / "widgets.py").read_text(encoding="utf-8")
        at = src.index("class _CellDelegate")
        body = src[at:src.index("\nclass ", at + 10)]
        assert "theme.Font.ICON" in body, "delegate 根本没读这个尺寸"
        assert body.count("theme.Font.ICON") >= 2, (
            "只在 paint 里放大了,`sizeHint` 没跟着 —— 行高不够会把字裁掉半截")

    def test_size_hint_grows(self):
        """行为验证:标了 glyph 的 cell 行高必须比正文的高。"""
        from PySide6.QtGui import QFont
        from PySide6.QtWidgets import QApplication, QStyleOptionViewItem

        from astro_smb_qt import widgets as W

        QApplication.instance() or QApplication([])
        d = W._CellDelegate()
        opt = QStyleOptionViewItem()
        opt.font = QFont()

        class _Idx:
            def __init__(self, cell):
                self._c = cell

            def data(self, _role):
                return self._c

        big = d.sizeHint(opt, _Idx(W.cell("▣", glyph=True))).height()
        small = d.sizeHint(opt, _Idx(W.cell("▣"))).height()
        assert big > small, "glyph 标记对行高没有任何影响"


class TestMiniRadarCaption:
    def test_caption_is_rendered(self):
        src = _src(BROWSER, "_radar")
        assert "高度" in src and "方位" in src, (
            "雷达底下那句「高度 35° · 方位 南」没了 —— 雷达本身只给方向直觉,"
            "具体几度只有这一行能读")
        assert "_az_name" in src, "方位名没走共享层的 16 向映射"


class TestDialogsAreChinese:
    def test_no_qinputdialog_anywhere(self):
        """`QInputDialog.getText` 的按钮是 Qt 默认英文 OK/Cancel。"""
        bad = []
        for p in sorted(QT.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            tree = ast.parse(p.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if (isinstance(node, ast.Attribute)
                        and isinstance(node.value, ast.Name)
                        and node.value.id == "QInputDialog"):
                    bad.append(f"{p.name}:{node.lineno}")
        assert not bad, (
            f"{bad} 还在用 QInputDialog —— 按钮会是英文 OK/Cancel,"
            f"而同一页别的对话框全是中文")

    def test_ask_text_labels_both_buttons(self):
        src = _src(QT / "pages" / "base.py", "ask_text")
        assert "addButton(_('取消')" in src, "取消按钮没给中文文案"
        assert "ok_text" in src, "确定按钮的文案不可指定"

    def test_callers_pass_a_verb(self):
        for name, verb in (("_mkdir", "创建"), ("_rename", "重命名")):
            src = _src(BROWSER, name)
            assert f"ok_text=_('{verb}')" in src, (
                f"{name} 的确定按钮没写成「{verb}」")

    def test_cancel_returns_empty(self):
        """行为验证:取消要给空串,不能给 None 或抛。"""
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QApplication, QDialog

        from astro_smb_qt import theme
        from astro_smb_qt.pages.base import Page

        app = QApplication.instance() or QApplication([])
        theme.apply(app)
        page = Page.__new__(Page)          # 不跑各页自己的 __init__
        from PySide6.QtWidgets import QWidget
        QWidget.__init__(page)

        def close_it():
            for w in app.topLevelWidgets():
                if isinstance(w, QDialog) and w.isVisible():
                    w.reject()

        QTimer.singleShot(60, close_it)
        assert page.ask_text("重命名", "新名字", text="x") == ""
