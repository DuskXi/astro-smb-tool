"""空间分析页 / 传输监控页的离线单测(不连设备、不起 XAML 消息泵)。

两条主线:

* **#30 事件泄漏**。win32more 的 `event` 描述符(`_winrt.py` 的 `event.__get__`)
  把实例存进**类级** `_event_setters[id(instance)]` 且**永不删除** ——
  `-=` 与 `clear()` 只清 `_callbacks`。所以「每次重画就新建控件并 `+=` 挂事件」
  的地方都在永久泄漏控件与闭包。真 XAML app 探针实测(见任务报告):
  空间页明细行 6 次折叠切换 → Tapped 条目 +24/+36/+60/+72/+96/+108,
  `Items.Clear()`+gc 之后仍有 120 个存活;监控页 3 轮 × 20 个任务 →
  Click 条目 22/44/66。修复后两处都稳定在「一代内容一次注册」。
  控件本身要真 XAML app 才建得出来(纯 pytest 里 `Grid()` 直接 ComError),
  所以这里测**能离线测的那一半**:缓存/回收池的命中路径(命中时一个控件
  都不新建 —— 反过来说,一旦退化成新建就会在这里 ComError 炸出来)、
  闭包里现读身份而不是捕获身份、以及回收池的收口。
* **#34 批量绘图**。整批图元拼成 XAML 文本一次 `XamlReader.Load`,实测
  900 块 878ms → 150ms(冷)/ 151ms → 46ms(热)。这里钉死片段的**文本形状**
  (可解析、元素数、不区分区域设置的数字、转义)与**几何不变式**
  (批量化之后 `_hits` / `_block_map` / 预算裁剪必须和逐元素时代一模一样)。
"""

from __future__ import annotations

import ast
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest

from astro_smb.client import TreeNode
from astro_smb_gui import _common as C
from astro_smb_gui import _monitor as M
from astro_smb_gui import _space as S

XNS = "{http://schemas.microsoft.com/winfx/2006/xaml/presentation}"
BASE = Path(__file__).resolve().parent.parent / "astro_smb_gui"


# ---------------------------------------------------------------- 工具

def _node_of(module, dotted: str):
    """取 `模块.类.方法` 或 `模块.函数` 的 AST 节点。"""
    src = Path(module.__file__).read_text(encoding="utf-8")
    node = ast.parse(src)
    for part in dotted.split("."):
        found = None
        for child in ast.iter_child_nodes(node):
            if (isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.ClassDef))
                    and child.name == part):
                found = child
                break
        assert found is not None, f"{module.__name__} 里找不到 {dotted}"
        node = found
    return node


def _src_of(module, dotted: str) -> str:
    src = Path(module.__file__).read_text(encoding="utf-8")
    return ast.get_source_segment(src, _node_of(module, dotted)) or ""


def _bare_space() -> S.SpacePage:
    """不经 __init__ 的 SpacePage —— 只填纯 Python 状态,不碰任何 XAML。

    批量绘图之后 `_layout`/`_emit_block` 变成了纯计算(一个 WinRT 调用都不发),
    所以整条布局链路可以离线跑,这正是把几何不变式钉死的机会。
    """
    p = object.__new__(S.SpacePage)
    p._reset_blocks()
    p._nodes = {}
    p._row_mark = {}
    p._row_cache = {}
    return p


def _bare_monitor() -> M.MonitorPage:
    p = object.__new__(M.MonitorPage)
    p._rows = {}
    p._free_rows = []
    p._free_headers = []
    p._group_rows = {}
    p._group_open = {}
    p._relayout = lambda: None
    p._update_stats = lambda: None
    return p


def _fake_tb():
    """假 TextBlock:记录 Foreground 赋值与 ClearValue 次数。"""
    tb = SimpleNamespace(Foreground=None, cleared=0, sets=0)

    def clear(_dp, tb=tb):
        tb.cleared += 1
        tb.Foreground = None

    tb.ClearValue = clear
    return tb


def _leaf(name: str, size: int, parent: str = "") -> TreeNode:
    path = f"{parent}\\{name}" if parent else name
    return TreeNode(name=name, path=path, is_dir=False, size=size, file_count=1)


def _dir(name: str, kids: list[TreeNode], parent: str = "") -> TreeNode:
    path = f"{parent}\\{name}" if parent else name
    d = TreeNode(name=name, path=path, is_dir=True, children=kids)
    d.size = sum(c.size for c in kids)
    d.file_count = sum(c.file_count for c in kids)
    return d


def _tree(nd: int = 4, ns: int = 4, nf: int = 6) -> TreeNode:
    tops = []
    for i in range(nd):
        subs = []
        for j in range(ns):
            base = f"Plan {i}\\M {i}-{j}"
            subs.append(_dir(f"M {i}-{j}",
                             [_leaf(f"Light_{i}{j}{k}.fit",
                                    40_000_000 + k * 1_000, base)
                              for k in range(nf)],
                             f"Plan {i}"))
        tops.append(_dir(f"Plan {i}", subs))
    return _dir("ROOT", tops)


def _render(page, tree, w=1000.0, h=700.0):
    items = [c for c in tree.children if c.size > 0]
    page._layout(items, 0, len(items), 2.0, 2.0, w - 4.0, h - 4.0, 0,
                 sum(c.size for c in items))


# ================================================================ #34 批量绘图

class TestFragmentText:
    """片段的文本形状 —— 解析失败 = 整页画白,所以每条都要钉死。"""

    def test_empty_inputs_yield_empty_strings(self):
        assert S._block_fragment([], []) == ""
        assert S._label_fragment([]) == ""
        assert C.rect_fragment([]) == ""

    def test_hex_is_aarrggbb(self):
        assert S._hex((255, 0x4F, 0x8A, 0xC7)) == "#FF4F8AC7"
        assert S._hex((90, 0, 0, 0)) == "#5A000000"

    def test_block_fragment_keeps_fill_stroke_and_radius_on_one_rect(self):
        fill = (1.0, 2.0, 30.0, 40.0, (255, 10, 20, 30))
        root = ET.fromstring(S._block_fragment([fill], [fill[:4]]))
        (r,) = list(root)
        assert r.tag == XNS + "Rectangle"
        assert r.get("Fill") == "#FF0A141E"
        assert r.get("Stroke") == S._hex(S._STROKE_ARGB)
        assert r.get("StrokeThickness") == "1"
        assert r.get("RadiusX") == "2" and r.get("RadiusY") == "2"
        # 附加属性写的是无前缀的 "Canvas.Left",不落在默认命名空间里
        assert r.get("Canvas.Left") == "1.00" and r.get("Canvas.Top") == "2.00"

    def test_numbers_are_invariant_culture(self):
        """XAML 属性按 invariant culture 解析:小数点绝不能变成逗号。"""
        frag = S._block_fragment(
            [(1234.5, 0.25, 2.0, 3.0, (255, 1, 2, 3))], [])
        assert "1234.50" in frag and "," not in frag.split("Canvas.Left")[1][:12]
        lab = S._label_fragment([(1234.5, 0.25, "x", 11.0, "Normal", 8.0)])
        assert 'Canvas.Left="1234.50"' in lab

    def test_label_fragment_escapes_markup_and_newline(self):
        """真机文件名里 & < > " ' 都可能出现;叶级标签本身是两行。"""
        name = 'A&B <c> "d" \'e\'\nrest'
        root = ET.fromstring(
            S._label_fragment([(0.0, 0.0, name, 11.0, "Normal", 40.0)]))
        (t,) = list(root)
        assert t.tag == XNS + "TextBlock"
        assert t.get("Text") == name          # 转义后原样还原
        assert t.get("FontWeight") == "Normal"
        assert t.get("Foreground") == S._hex(S._WHITE_ARGB)
        assert t.get("TextTrimming") == "CharacterEllipsis"

    def test_xml_text_drops_cr_and_maps_lf(self):
        assert S._xml_text("a\r\nb") == "a&#10;b"
        assert S._xml_text("<&>") == "&lt;&amp;&gt;"

    def test_xml_text_keeps_astral_names_without_putting_astral_in_hstring(self):
        escaped = S._xml_text("M 8 🌌.fit")
        assert not any(ord(c) > 0xFFFF for c in escaped)
        root = ET.fromstring(
            S._label_fragment([(0.0, 0.0, "M 8 🌌.fit", 11.0,
                                "Normal", 80.0)]))
        assert list(root)[0].get("Text") == "M 8 🌌.fit"

    def test_xml_text_replaces_xml10_control_characters(self):
        escaped = S._xml_text("a\x00\x0bb")
        assert "\x00" not in escaped and "\x0b" not in escaped
        assert "\uFFFD" in escaped

    def test_label_weight_passthrough(self):
        root = ET.fromstring(
            S._label_fragment([(0.0, 0.0, "n", 10.0, "SemiBold", 20.0)]))
        assert list(root)[0].get("FontWeight") == "SemiBold"


class TestTreemapBatch:
    """布局阶段现在是纯 Python;批量化不得改动任何几何/预算语义。"""

    def test_layout_fills_hits_and_map_stay_in_sync(self):
        p = _bare_space()
        _render(p, _tree())
        assert len(p._fills) == len(p._hits)
        assert len(p._block_map) == len(p._fills)
        # _hits 是绘制顺序(逆序遍历取最上层块),必须与 _fills 一一对应
        for (fx, fy, fw, fh, _c), (hx1, hy1, hx2, hy2, node) in zip(
                p._fills, p._hits):
            assert (fx, fy) == (hx1, hy1)
            assert p._block_map[node.path] == (fx, fy, fw, fh)
            assert fw <= hx2 - hx1 and fh <= hy2 - hy1

    def test_block_geometry_is_cell_minus_two(self):
        """块画在格子内、四周留 2px 间隙 —— 描边层叠在上面才不会压到邻块。"""
        p = _bare_space()
        p._emit_block(_leaf("a.fit", 100), 10.0, 20.0, 50.0, 40.0, 0)
        assert p._fills[0][:4] == (10.0, 20.0, 48.0, 38.0)
        assert p._block_map["a.fit"] == (10.0, 20.0, 48.0, 38.0)

    def test_small_blocks_get_no_outline(self):
        p = _bare_space()
        p._emit_block(_leaf("small.fit", 1), 0.0, 0.0,
                      S._STROKE_MIN - 0.5, 100.0, 0)
        p._emit_block(_leaf("big.fit", 1), 0.0, 0.0,
                      S._STROKE_MIN, S._STROKE_MIN, 0)
        assert len(p._fills) == 2 and len(p._outlines) == 1

    def test_budget_still_caps_the_element_count(self):
        p = _bare_space()
        _render(p, _tree(nd=8, ns=8, nf=12))
        assert p._blocks <= S._BLOCK_BUDGET
        assert p._omitted > 0                      # 超预算的确实被裁掉了
        assert len(p._fills) + len(p._labels) == p._blocks

    def test_tiny_boxes_are_omitted_not_drawn(self):
        p = _bare_space()
        p._emit_block(_leaf("x.fit", 1), 0.0, 0.0, 1.0, 100.0, 0)
        assert p._fills == [] and p._omitted == 1

    def test_whole_batch_parses_as_one_canvas_per_layer(self):
        """端到端:真实规模的一轮布局,三层片段都要是合法 XAML。"""
        p = _bare_space()
        _render(p, _tree(nd=6, ns=6, nf=8))
        for frag, n in ((S._block_fragment(p._fills, p._outlines),
                         len(p._fills)),
                        (S._label_fragment(p._labels), len(p._labels))):
            root = ET.fromstring(frag)
            assert root.tag == XNS + "Canvas"
            assert len(list(root)) == n
        assert len(p._fills) > 200 and len(p._labels) > 0

    def test_nested_titles_and_leaf_labels_differ_in_weight(self):
        p = _bare_space()
        _render(p, _tree(nd=2, ns=2, nf=2), w=1200.0, h=800.0)
        weights = {lab[4] for lab in p._labels}
        assert weights == {"SemiBold", "Normal"}


class TestBatchUsesSharedPrimitives:
    """每条批量路径都必须有明确片段源与逐元素兜底。"""

    def test_space_uses_single_rectangle_for_fill_and_stroke(self):
        src = _src_of(S, "SpacePage._flush_treemap")
        assert "_block_fragment(" in src
        frag = S._block_fragment(
            [(0.0, 0.0, 20.0, 10.0, (255, 1, 2, 3))],
            [(0.0, 0.0, 20.0, 10.0)])
        assert frag.count("<Rectangle ") == 1

    def test_monitor_imports_rect_fragment_from_common(self):
        src = Path(M.__file__).read_text(encoding="utf-8")
        assert "from astro_smb_gui._common import argb_hex, rect_fragment" in src
        assert "rect_fragment(" in _src_of(M, "MonitorPage._batch_blocks")

    def test_emit_block_makes_no_winrt_call(self):
        """`_emit_block` 递归几百次;里面一旦冒出一次 WinRT 调用就前功尽弃。"""
        src = _src_of(S, "SpacePage._emit_block")
        for bad in ("Rectangle(", "TextBlock(", "Canvas.Set", "self._brush(",
                    ".Children."):
            assert bad not in src, f"_emit_block 里不该有 {bad}"

    def test_every_batch_path_has_a_per_element_fallback(self):
        flush = _src_of(S, "SpacePage._load_layer")
        assert "except Exception" in flush and "slow(canvas)" in flush
        assert _src_of(S, "SpacePage._draw_blocks_slow")
        assert _src_of(S, "SpacePage._draw_labels_slow")
        ens = _src_of(M, "MonitorPage._ensure_blocks")
        assert "if rects is None:" in ens and "Rectangle()" in ens
        assert "return None" in _src_of(M, "MonitorPage._batch_blocks")


class TestMonitorBlockPainting:
    """方块图:建格走批量,着色仍然是原地改 Fill(而且只改变了色的格子)。"""

    def _row(self, n=8):
        rects = [SimpleNamespace(Fill=None) for _ in range(n)]
        return {"rects": rects, "fills": [None] * n}

    def _page(self):
        p = _bare_monitor()
        p._c_done, p._c_active, p._c_pending = "DONE", "ACTIVE", "PEND"
        return p

    def test_colours_follow_block_states(self):
        p, row = self._page(), self._row(4)
        job = SimpleNamespace(blocks=[2, 2, 1, 0])
        p._paint_blocks(row, job)
        assert [r.Fill for r in row["rects"]] == ["DONE", "DONE",
                                                  "ACTIVE", "PEND"]

    def test_downsampling_aggregates_chunks(self):
        """4 块下采样到 2 格:整格全完成才绿,沾一点传输中就琥珀。"""
        p, row = self._page(), self._row(2)
        p._paint_blocks(row, SimpleNamespace(blocks=[2, 2, 0, 0]))
        assert [r.Fill for r in row["rects"]] == ["DONE", "PEND"]
        p, row = self._page(), self._row(2)
        p._paint_blocks(row, SimpleNamespace(blocks=[2, 2, 2, 0]))
        assert [r.Fill for r in row["rects"]] == ["DONE", "ACTIVE"]

    def test_unchanged_cells_are_not_rewritten(self):
        """一次 Fill 赋值约 40us;128 格每 tick 全写就是 5ms 的白烧。"""
        p, row = self._page(), self._row(4)
        job = SimpleNamespace(blocks=[2, 2, 1, 0])
        p._paint_blocks(row, job)
        for r in row["rects"]:
            r.Fill = "SENTINEL"          # 谁被重写谁就露馅
        p._paint_blocks(row, job)
        assert [r.Fill for r in row["rects"]] == ["SENTINEL"] * 4
        job.blocks = [2, 2, 2, 0]        # 只有第 3 格变色
        p._paint_blocks(row, job)
        assert [r.Fill for r in row["rects"]] == ["SENTINEL", "SENTINEL",
                                                  "DONE", "SENTINEL"]

    def test_stale_fill_cache_length_is_repaired(self):
        p, row = self._page(), self._row(4)
        row["fills"] = [None, None]      # 回收来的行:长度对不上
        p._paint_blocks(row, SimpleNamespace(blocks=[2, 2, 1, 0]))
        assert len(row["fills"]) == 4


# ================================================================ #30 事件泄漏

class TestSpaceRowReuse:
    """明细行按内容指纹复用(方案 a,与 `_sky3d._row_cache` 同款)。"""

    def test_key_covers_every_rendered_field(self):
        parent = _dir("P", [_leaf("a.fit", 10, "P"), _leaf("b.fit", 90, "P")])
        a = parent.children[0]
        k = S._row_key(a, parent, 0, False)
        assert S._row_key(a, parent, 0, False) == k          # 同内容同键
        assert S._row_key(a, parent, 1, False) != k          # 缩进变了
        assert S._row_key(a, parent, 0, True) != k           # 箭头变了
        a.size = 11
        assert S._row_key(a, parent, 0, False) != k          # 大小变了
        a.size = 10
        parent.size = 999
        assert S._row_key(a, parent, 0, False) != k          # 百分比变了

    def test_cache_hit_builds_no_control(self):
        """命中缓存时一个控件都不新建 —— 退化成新建的话,纯 pytest 里
        `Grid()` 会直接 ComError(没有 XAML 消息泵),这条就红。"""
        p = _bare_space()
        parent = _dir("P", [_leaf("a.fit", 10, "P")])
        node = parent.children[0]
        key = S._row_key(node, parent, 0, False)
        sentinel, mark = object(), object()
        p._row_cache[key] = {"g": sentinel, "mark": mark}
        assert p._detail_row(node, parent, 0, False) is sentinel
        assert p._row_mark[node.path] is mark

    def test_cache_hit_without_mark_does_not_register_one(self):
        p = _bare_space()
        parent = _dir("P", [_leaf("a.fit", 10, "P")])
        node = parent.children[0]
        key = S._row_key(node, parent, 0, False)
        p._row_cache[key] = {"g": object(), "mark": None}
        p._detail_row(node, parent, 0, False)
        assert p._row_mark == {}

    def test_cache_is_consulted_before_creating_the_grid(self):
        src = _src_of(S, "SpacePage._detail_row")
        hit = src.index("self._row_cache.get(")
        assert hit < src.index("g = Grid()")
        assert src.index('return hit["g"]') < src.index("g = Grid()")
        assert 'self._row_cache[key] = ' in src

    def test_handler_captures_path_not_the_node(self):
        """闭包永久滞留,捕获 TreeNode 就把整棵子树也 pin 住了。"""
        src = _src_of(S, "SpacePage._detail_row")
        assert "p=node.path" in src
        assert "n=node" not in src
        tapped = _src_of(S, "SpacePage._on_row_tapped_at")
        assert "self._nodes.get(path)" in tapped
        assert "if node is None:" in tapped     # 换代后的旧行点了要能安全早退

    def test_cache_survives_data_generation_when_content_matches(self):
        """**契约已变**:这条原来同时断言"`_set_data` 不许清"和
        "`_clear_view` 必须清" —— 把那个自相矛盾的半吊子修复当成了正确行为。

        `_scan()` 开头就调 `_clear_view()`,所以只要它清缓存,"按内容指纹复用"
        在**用户最常走的重扫路径**上就完全失效(实测重扫 10 次累计泄漏 120 个
        Tapped 注册)。而清缓存的原顾虑"旧行大小已作废"在设计上不成立:
        `_row_key` 连 node.size / parent.size 都在键里,大小一变必然是新键。
        两处统一为**都不清**。
        """
        assert "self._row_cache = {}" not in _src_of(S, "SpacePage._set_data")
        assert "self._row_cache = {}" not in _src_of(S, "SpacePage._clear_view")

    def test_view_root_change_keeps_the_cache(self):
        """下钻/返回不换代,行指纹一模一样 —— 清掉就等于白复用。"""
        assert "_row_cache" not in _src_of(S, "SpacePage._set_view_root")


class TestSpaceHighlightLayer:
    """联动高亮改成常驻描边框(批量绘图拿不到稳定的单块引用)。"""

    def test_no_more_restore_bookkeeping(self):
        src = Path(S.__file__).read_text(encoding="utf-8")
        assert "_hl_restore" not in src
        apply_ = _src_of(S, "SpacePage._apply_highlight")
        assert "self.hl_rect" in apply_ and "self._block_map.get" in apply_

    def test_xaml_declares_the_persistent_rect(self):
        xaml = (BASE / "space.xaml").read_text(encoding="utf-8")
        assert 'x:Name="HiliteCanvas"' in xaml
        assert 'x:Name="HiliteRect"' in xaml
        # 颜色口径只有一处真源(_HL_RGB),xaml 必须跟它一致
        assert f'Stroke="{S._hex((255,) + S._HL_RGB)}"' in xaml

    def test_highlight_layer_scales_with_the_others(self):
        """拖窗口期间三层必须同步缩放,否则高亮框会和色块错位。"""
        wire = _src_of(S, "SpacePage._wire")
        assert "self.canvas, self.hilite_canvas, self.label_canvas" in wire


class TestMonitorRecycling:
    """行/组头进回收池(方案 a);闭包现读身份,绝不捕获 job_id / 组键。"""

    def test_cleared_rows_go_to_the_free_list(self):
        p = _bare_monitor()
        jobs = [SimpleNamespace(job_id=i, group=None, finished=True)
                for i in range(3)]
        rows = {i: {"job_id": i} for i in range(3)}
        p._rows = dict(rows)
        p.shell = SimpleNamespace(
            transfers=SimpleNamespace(jobs=[],
                                      clear_finished=lambda: jobs))
        p._on_clear_done(None, None)
        assert p._rows == {}
        assert p._free_rows == [rows[0], rows[1], rows[2]]

    def test_dead_group_headers_go_to_the_free_list(self):
        p = _bare_monitor()
        hdr_run = {"key": ("run", "M 8")}
        hdr_alive = {"key": ("run", "M 42")}
        p._group_rows = {("run", "M 8"): hdr_run, ("run", "M 42"): hdr_alive}
        p._group_open = {("run", "M 8"): False, ("run", "M 42"): True}
        alive = SimpleNamespace(job_id=9, group="M 42", finished=False,
                                status="传输")
        p.shell = SimpleNamespace(
            transfers=SimpleNamespace(jobs=[alive], clear_finished=lambda: []))
        p._on_clear_done(None, None)
        assert p._group_rows == {("run", "M 42"): hdr_alive}
        assert p._free_headers == [hdr_run]
        assert p._group_open == {("run", "M 42"): True}

    def test_external_clear_path_is_reaped_on_relayout_boundary(self):
        """底部常驻条直接 clear_finished 后,监控页也必须反向收割孤儿。"""
        p = _bare_monitor()
        row = {"job_id": 1}
        hdr = {"key": ("done", "M 8")}
        p._rows = {1: row}
        p._group_rows = {("done", "M 8"): hdr}
        p._group_open = {("done", "M 8"): False}
        p.shell = SimpleNamespace(transfers=SimpleNamespace(jobs=[]))
        p._reap_orphans()
        assert p._rows == {} and p._free_rows == [row]
        assert p._group_rows == {} and p._free_headers == [hdr]
        assert p._group_open == {}

    def test_reused_row_rebinds_job_id_and_indent(self):
        """复用时 job_id / 分区 / 缩进都要归位,否则「取消」会取消错任务。"""
        p = _bare_monitor()
        old = {"job_id": 7, "section": "done",
               "root": SimpleNamespace(Margin=None)}
        p._free_rows = [old]
        job = SimpleNamespace(job_id=42, group="M 8")
        row = p._take_row_for(job)
        assert row is old
        assert row["job_id"] == 42 and row["section"] is None
        assert row["root"].Margin.Left == 18.0
        assert p._free_rows == []

    def test_reused_row_for_a_loose_file_has_no_indent(self):
        p = _bare_monitor()
        p._free_rows = [{"job_id": 1, "section": "run",
                         "root": SimpleNamespace(Margin=None)}]
        row = p._take_row_for(SimpleNamespace(job_id=2, group=None))
        assert row["root"].Margin.Left == 0.0

    def test_reused_group_header_rebinds_key_and_texts(self):
        p = _bare_monitor()
        old = {"key": ("done", "旧组"), "_last": 123.0,
               "toggle": SimpleNamespace(Content="▶"),
               "name": SimpleNamespace(Text="▣ 旧组"),
               "status": SimpleNamespace(Text="旧文案")}
        p._free_headers = [old]
        hdr = p._build_group_header(("run", "M 8"))
        assert hdr is old and hdr["key"] == ("run", "M 8")
        assert hdr["_last"] == 0.0
        assert hdr["name"].Text == "▣ M 8"
        assert hdr["status"].Text == ""
        assert hdr["toggle"].Content == "▼"     # run 分区默认展开

    def test_reused_group_header_respects_a_remembered_collapse(self):
        p = _bare_monitor()
        p._group_open[("run", "M 8")] = False
        p._free_headers = [{"key": None, "_last": 1.0,
                            "toggle": SimpleNamespace(Content="▼"),
                            "name": SimpleNamespace(Text=""),
                            "status": SimpleNamespace(Text="")}]
        hdr = p._build_group_header(("run", "M 8"))
        assert hdr["toggle"].Content == "▶"

    def test_pool_is_consulted_before_building_controls(self):
        # _build_row 已拆成两半:_take_row_for 负责"池 → 现收割 → 才新建"的顺序,
        # _build_fresh_row 只管建。断言前者在调后者之前先试过池与收割。
        take = _src_of(M, "MonitorPage._take_row_for")
        assert take.index("self._free_rows.pop()") < take.index("_reap_orphans")
        assert take.index("_reap_orphans") < take.index("_build_fresh_row")
        assert "Border()" not in take, "取行入口不该自己建控件"
        hdr = _src_of(M, "MonitorPage._build_group_header")
        assert hdr.index("self._free_headers.pop()") < hdr.index("Border()")

    def test_closures_read_identity_instead_of_capturing_it(self):
        row = _src_of(M, "MonitorPage._build_fresh_row")
        assert 'r["job_id"]' in row
        assert "jid = job.job_id" not in row      # 捕获快照 = 取消错任务
        hdr = _src_of(M, "MonitorPage._build_group_header")
        assert 'k = h["key"]' in hdr
        assert "def on_toggle(s, e, k=key)" not in hdr


class TestMonitorStickyStyling:
    """行会被回收给别的任务:单向着色/淡化必须双向可逆。"""

    def test_error_colour_is_cleared_when_the_next_state_is_fine(self):
        p = _bare_monitor()
        p._c_err = "RED"
        row = {"status": _fake_tb()}
        p._set_fg(row, "status", "RED")
        assert row["status"].Foreground == "RED"
        p._set_fg(row, "status", None)
        assert row["status"].Foreground is None
        assert row["status"].cleared == 1

    def test_unchanged_colour_is_not_rewritten(self):
        p = _bare_monitor()
        row = {"status": _fake_tb()}
        p._set_fg(row, "status", "RED")
        p._set_fg(row, "status", "RED")
        assert row["status"].Foreground == "RED"
        p._set_fg(row, "status", None)
        p._set_fg(row, "status", None)
        assert row["status"].cleared == 1

    def test_a_fresh_row_needs_no_clear(self):
        """新建的行本来就没显式 Foreground,别为它白跑一次 ClearValue。"""
        p = _bare_monitor()
        row = {"status": _fake_tb()}
        p._set_fg(row, "status", None)
        assert row["status"].cleared == 0

    def test_update_row_writes_both_branches(self):
        src = _src_of(M, "MonitorPage._update_row")
        assert src.count("_set_fg(row, \"phase\"") == 3   # 元数据/传输/其它
        assert "_set_fg(row, \"status\"" in src
        assert 'row["phase"].Opacity = 1.0' in src
        assert "row[\"status\"].Foreground = self._c_err" not in src


# ---------------------------------------------------------------- 静态扫描

class TestNoAstralChars:
    """§7.1:win32more 把 str 转 HSTRING 时按码点数给长度,而 HSTRING 是
    UTF-16 —— 任何星平面字符都会让末尾少一个字符(真机:组名 'M 8'→'M ')。
    `_space.py` / `space.xaml` 已由 test_guiding_groups 扫过,这里补监控页。"""

    @staticmethod
    def _astral(s: str) -> list:
        return sorted({c for c in s if ord(c) > 0xFFFF})

    def test_monitor_string_literals_are_bmp_only(self):
        tree = ast.parse((BASE / "_monitor.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                bad = self._astral(node.value)
                assert not bad, (f"_monitor.py:{node.lineno} 含星平面字符: "
                                 f"{[hex(ord(c)) for c in bad]}")

    def test_monitor_xaml_text_is_bmp_only(self):
        from xml.dom import minidom

        doc = minidom.parse(str(BASE / "monitor.xaml"))
        chunks = []
        stack = [doc.documentElement]
        while stack:
            el = stack.pop()
            if el.attributes is not None:
                chunks += [a.value for a in el.attributes.values()]
            for ch in el.childNodes:
                if ch.nodeType == ch.TEXT_NODE:
                    chunks.append(ch.data)
                elif ch.nodeType == ch.ELEMENT_NODE:
                    stack.append(ch)
        bad = self._astral("".join(chunks))
        assert not bad, f"monitor.xaml 含星平面字符: {bad}"


@pytest.mark.parametrize("name", ["_space.py", "_monitor.py"])
def test_no_dead_pool_machinery_left(name):
    """复用池/接口视图已被批量绘图取代:留着半套只会误导下一个人。"""
    src = (BASE / name).read_text(encoding="utf-8")
    for dead in ("_take_rect", "_take_label", "_trim_pools", "_RectView"):
        assert dead not in src


class TestBottomBarClearReapsToo:
    """底部常驻条那颗「清除已完成」走的是 `_window._on_clear_done` ——
    `transfers.clear_finished()` + `_prune_transfer_rows()`,**完全不经过监控页**。
    而 `main.xaml` 里它在 Grid.Row=3 全程可见,比监控页那颗更常用。

    更要命的是行是 `_window._on_transfer_update` 对**每个** job 无条件
    `monitor.update_job(job)` 建的 —— 用户从没打开过「传输」页照样建、照样漏。
    """

    @staticmethod
    def _page_with(n: int):
        """n 个已完成任务、行都已建好的监控页。"""
        p = _bare_monitor()
        jobs = [SimpleNamespace(job_id=i, group=None, finished=True,
                                status="完成") for i in range(n)]
        # 行必须是**真实形状**:复用路径要读 row["root"].Margin。
        # 用只有 job_id 的假行会在复用时 KeyError,那是测试桩失真,不是代码缺陷。
        p._rows = {i: {"job_id": i, "section": "done",
                       "root": SimpleNamespace(Margin=None)} for i in range(n)}
        mgr = SimpleNamespace(jobs=list(jobs))

        def clear_finished():
            gone = [j for j in mgr.jobs if j.finished]
            mgr.jobs = [j for j in mgr.jobs if not j.finished]
            return gone

        mgr.clear_finished = clear_finished
        p.shell = SimpleNamespace(transfers=mgr)
        return p, mgr

    def test_reap_after_external_clear_recovers_every_row(self):
        """底部条清除后,监控页必须能把全部孤儿行收回池。"""
        p, mgr = self._page_with(5)
        mgr.clear_finished()                    # 底部条那条路径
        assert p._rows and not p._free_rows, "清除后行还挂在 _rows(前置条件)"
        p._reap_orphans()
        assert p._rows == {}
        assert len(p._free_rows) == 5

    def test_build_row_never_creates_when_pool_is_recoverable(self):
        """**这条盯的是顺序**:底部条清除后回收池是空的(reap 还没跑),
        下一批任务的第一个 job 走 update_job → _build_row 发现池空 → 新建
        整棵控件树 → 之后才在 _relayout 里 reap。每个「清除→再下载」周期
        永久泄漏一行的 Click 注册(实测 14/18/22/26,每轮 +4)。

        正确行为:`_build_row` 拿不到池时应先收割一次,能复用就绝不新建。
        """
        p, mgr = self._page_with(3)
        mgr.clear_finished()                    # 3 行变成孤儿,池仍为空
        built = []

        def fresh(job):
            built.append(job.job_id)
            return {"job_id": job.job_id, "section": None,
                    "root": SimpleNamespace(Margin=None)}

        p._build_fresh_row = fresh

        new_job = SimpleNamespace(job_id=99, group=None, finished=False,
                                  status="传输")
        mgr.jobs.append(new_job)
        row = p._take_row_for(new_job)

        assert built == [], "池里明明有 3 行可复用,不该新建控件树"
        assert row is not None and row["job_id"] == 99
        assert len(p._free_rows) == 2, "复用一行后池里应剩 2 行"

    def test_repeated_clear_download_cycles_do_not_grow(self):
        """连跑 4 个「清除→再下载」周期,建行总数必须恒定 —— 这才是泄漏与否的判据。"""
        p, mgr = self._page_with(0)
        built = []

        def fresh(job):
            built.append(job.job_id)
            return {"job_id": job.job_id, "section": None,
                    "root": SimpleNamespace(Margin=None)}

        p._build_fresh_row = fresh

        nid = 0
        for _cycle in range(4):
            batch = []
            for _ in range(5):
                nid += 1
                j = SimpleNamespace(job_id=nid, group=None, finished=False,
                                    status="传输")
                mgr.jobs.append(j)
                batch.append(j)
                p._rows[j.job_id] = p._take_row_for(j)
            for j in batch:
                j.finished = True
            mgr.clear_finished()                # 底部条清除

        assert len(built) == 5, (
            f"4 个周期只该建 5 行(第一轮),实际建了 {len(built)} 行 —— 每轮都在泄漏")


class TestWindowNotifiesMonitorOnClear:
    """`_window._on_clear_done`(底部常驻条)必须通知监控页回收。"""

    def test_on_clear_done_calls_monitor(self):
        import astro_smb_gui._window as W

        seen = []
        shell = SimpleNamespace(
            transfers=SimpleNamespace(clear_finished=lambda: seen.append("clear")),
            _prune_transfer_rows=lambda: seen.append("prune"),
            monitor=SimpleNamespace(on_jobs_pruned=lambda: seen.append("reap")),
        )
        W.App._on_clear_done(shell, None, None)
        assert seen == ["clear", "prune", "reap"]

    def test_monitor_failure_does_not_break_clearing(self):
        """回收失败不能让「清除已完成」这个用户动作整个失败。"""
        import astro_smb_gui._window as W

        def boom():
            raise RuntimeError("监控页炸了")

        shell = SimpleNamespace(
            transfers=SimpleNamespace(clear_finished=lambda: None),
            _prune_transfer_rows=lambda: None,
            monitor=SimpleNamespace(on_jobs_pruned=boom),
        )
        W.App._on_clear_done(shell, None, None)      # 不许抛

    def test_public_entry_reaps_and_relayouts(self):
        p = _bare_monitor()
        calls = []
        p._reap_orphans = lambda: calls.append("reap")
        p._relayout = lambda: calls.append("relayout")
        p._update_stats = lambda: calls.append("stats")
        p.on_jobs_pruned()
        assert calls == ["reap", "relayout", "stats"]


class TestSpaceRowCacheSurvivesRescan:
    """`_row_cache` 在**重扫**路径上必须活着。

    `_row_key` 是完整内容指纹(连 node.size / parent.size 都在键里),所以
    "旧行大小已作废"这个顾虑在设计上不成立 —— 大小一变必然是新键。
    而清缓存有实害:win32more 的 Tapped 注册撤不掉,清一次就逼着下一代
    重建一批控件、永久泄漏一批注册。

    `_scan()` 开头就调 `_clear_view()`,所以只要它清缓存,
    "按内容指纹复用"这条修复在**用户最常走的重扫路径**上就完全失效。
    """

    def test_clear_view_keeps_the_row_cache(self):
        src = _src_of(S, "SpacePage._clear_view")
        assert "_row_cache = {}" not in src and "_row_cache.clear()" not in src, (
            "_clear_view 不该清 _row_cache —— _scan() 开头就调它,"
            "清了等于让重扫路径完全享受不到复用")

    def test_set_data_keeps_the_row_cache(self):
        src = _src_of(S, "SpacePage._set_data")
        assert "_row_cache = {}" not in src and "_row_cache.clear()" not in src

    def test_row_key_covers_size_so_stale_rows_cannot_be_reused(self):
        """键里必须含 size —— 这才是"不用清缓存"的依据。"""
        a = SimpleNamespace(path="a", is_dir=True, children=[], name="a", size=10)
        parent = SimpleNamespace(path="", is_dir=True, children=[a],
                                 name="", size=100)
        k1 = S._row_key(a, parent, 1, False)
        a2 = SimpleNamespace(path="a", is_dir=True, children=[], name="a", size=20)
        k2 = S._row_key(a2, parent, 1, False)
        assert k1 != k2, "大小变了必须是新键,否则会复用出过期数字"
        parent2 = SimpleNamespace(path="", is_dir=True, children=[a],
                                  name="", size=200)
        assert S._row_key(a, parent2, 1, False) != k1, "父级大小变了(百分比变)也要换键"

    def test_only_one_place_documents_the_policy(self):
        """两处口径必须一致 —— 曾经 :539 说"不能复用"、:718 说"可以复用"。"""
        for fn in ("SpacePage._clear_view", "SpacePage._set_data"):
            assert "不清 `_row_cache`" in _src_of(S, fn) or \
                   "不清 _row_cache" in _src_of(S, fn), fn
