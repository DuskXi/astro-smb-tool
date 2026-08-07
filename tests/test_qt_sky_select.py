"""3D 天球:球上要看得出选了谁(清单 4.2)。

独立验收员逐像素比对了"选中前 / 选中后"的同一片天区,结论是**两张裁图
完全一样** —— 标记不变粗、不变色、没有任何选中态的视觉元素,只有相机
飞过去了。而相机一旦再动到别处,就再也找不到"我选的是哪一个"。
降级的 QPainter 正射球一直有选中环(半径 6 而不是 4 + 一圈描边),
web 这条漏了。

**这份 JS 是两套前端共用的**(`astro_smb_app/web/sky3d.js`,老 UI 也在跑),
所以只能做**加法**:宿主不发 `targetSelect` 时 `state.selName` 恒为 null,
所有标记维持原样,老 UI 一个像素都不变。那条 null 判断因此也在闸门里 ——
去掉它老 UI 就会跟着变,而那是不允许的。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

ROOT = Path(__file__).resolve().parents[1]
JS = ROOT / "astro_smb_app" / "web" / "sky3d.js"
SKY = ROOT / "astro_smb_qt" / "pages" / "sky3d.py"


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    from PySide6.QtWidgets import QApplication

    from astro_smb_qt import theme

    inst = QApplication.instance() or QApplication([])
    theme.apply(inst)
    return inst


def _page_with_stub_web():
    """页面 + 一个假的 `_web_post`,把发出去的消息收下来。"""
    from astro_smb_qt.shell import Shell

    page = Shell().page("sky")
    sent: list[dict] = []
    page._web_post = sent.append
    page._web_ready = True
    return page, sent


def _js_fn(name: str) -> str:
    src = JS.read_text(encoding="utf-8")
    at = src.index(f"function {name}(")
    end = src.index("\nfunction ", at + 10)
    return src[at:end]


class TestHostSendsTheSelection:

    def test_picking_pushes_target_select(self):
        page, sent = _page_with_stub_web()
        page.nights = [{"date": "d", "ts0": 0.0, "ts1": 1.0, "targets": []}]
        page.night_index = 0
        page._render_detail = lambda *_a, **_k: None
        page._pick_target("NGC 253")
        kinds = [m.get("type") for m in sent]
        assert "targetSelect" in kinds, kinds
        msg = next(m for m in sent if m["type"] == "targetSelect")
        assert msg["name"] == "NGC 253"

    def test_it_is_pushed_even_when_not_flying(self):
        """球上点过来那条路 `fly=False`(镜头已经在那儿了),
        但**高亮照样要更新** —— 否则从球上点选的目标反而没有标记。"""
        page, sent = _page_with_stub_web()
        page.nights = [{"date": "d", "ts0": 0.0, "ts1": 1.0, "targets": []}]
        page.night_index = 0
        page._render_detail = lambda *_a, **_k: None
        page._pick_target("NGC 7293", fly=False)
        assert any(m.get("type") == "targetSelect" for m in sent)

    def test_empty_name_clears_it(self):
        page, sent = _page_with_stub_web()
        page.selected = ""
        page._push_select()
        assert sent[-1] == {"type": "targetSelect", "name": ""}

    def test_pushed_again_after_targets_are_rebuilt(self):
        """重推 `targets` 会把标记全部重建 —— 选中态必须跟着补回去,
        否则拖一下时刻滑杆、换一个夜次,球上的高亮就悄悄没了。"""
        src = SKY.read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_push_web")
        body = "\n".join(ast.unparse(n) for n in fn.body)
        at = body.index("'targets'")
        assert "_push_select()" in body[at:], "推完 targets 没有补选中态"

    def test_nothing_is_sent_before_the_page_is_ready(self):
        page, sent = _page_with_stub_web()
        page._web_ready = False
        page.selected = "X"
        page._push_select()
        assert sent == []


class TestTheRendererHonoursIt:

    def test_the_message_is_handled(self):
        src = JS.read_text(encoding="utf-8")
        assert "case 'targetSelect':" in src
        at = src.index("case 'targetSelect':")
        assert "applyTargetStyle()" in src[at:at + 200]

    def test_selection_survives_a_targets_rebuild(self):
        """`setTargets` 之后要重贴一次样式 —— 标记是新建的,没有记忆。"""
        assert "applyTargetStyle()" in _js_fn("setTargets")

    def test_selected_marker_differs_in_both_size_and_colour(self):
        body = _js_fn("applyTargetStyle")
        assert "MARK_SEL_PX" in body and "MARK_PX" in body
        assert "MARK_SEL_COLOR" in body
        src = JS.read_text(encoding="utf-8")
        at = src.index("const MARK_PX")
        line = src[at:src.index("\n", at)]
        # 选中要**明显**更大,不是大一两个像素
        assert "20" in line and "32" in line, line

    def test_it_restores_the_targets_own_colour(self):
        """取消选中要还原成目标**自己的**颜色(夜次配色是有含义的),
        不能一律刷成默认色。"""
        assert "baseColor" in _js_fn("applyTargetStyle")
        assert "baseColor = color" in _js_fn("setTargets")

    def test_it_does_not_fight_hover(self):
        """鼠标正悬着的那个有自己的 1.55 倍,别互相覆盖。"""
        assert "hoverName" in _js_fn("applyTargetStyle")


class TestOldUiIsUnaffected:
    """**共享资产的加法必须对老 UI 无感。**"""

    def test_default_is_no_selection(self):
        src = JS.read_text(encoding="utf-8")
        at = src.index("selName:")
        assert "null" in src[at:at + 40]

    def test_style_is_a_no_op_without_a_selection(self):
        """老 UI 从不发 `targetSelect` ⇒ `selName` 恒为 null ⇒ 每个标记
        都走 else 分支,尺寸与颜色都是原来的值。

        这条守的是那个 `state.selName !== null &&`:去掉它,`undefined ===
        undefined` 之类的比较会让**某个**标记莫名其妙被高亮 —— 而受害的
        是冻结的老 UI。
        """
        body = _js_fn("applyTargetStyle")
        assert "state.selName !== null &&" in body

    def test_the_frozen_ui_never_sends_it(self):
        gui = ROOT / "astro_smb_gui"
        if not gui.exists():
            pytest.skip("没有老 UI 目录")
        hits = [p.name for p in gui.rglob("*.py")
                if "targetSelect" in p.read_text(encoding="utf-8",
                                                 errors="replace")]
        assert hits == [], f"老 UI 里出现了 targetSelect:{hits}(它是冻结的)"
