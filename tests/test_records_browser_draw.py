"""拍摄记录页/浏览页:批量绘图 + win32more 事件泄漏的离线回归单测。

背景(两件事是配套的,见 docs/DEVELOPMENT.md §7.1 与 win32more 的 `_winrt.py`):

* **事件永久泄漏**:`event.__get__` 把 `event_setter(instance, …)` 存进**类级**
  `_event_setters[id(instance)]`,而 `event_setter._instance` 是强引用;
  `-=` 与 `clear()` 只清 `_callbacks`,那张类级字典的条目**永不删除**。
  于是"每次重画/重铺列表就新建控件并 `+=` 挂事件"= 永久滞留控件与闭包
  (闭包还顺带 pin 住整条数据)。补 `-=` 无效,只能**别重复注册**:
  ① 按键缓存复用控件;② 固定容器上只挂一个事件 + 命中点位反算。
* **逐元素建 XAML 图元极慢**(实测 Rectangle 全套 1.7~2.1ms、Line 1.27ms、
  TextBlock 0.88ms):整批拼成 XAML 文本一次 `XamlReader.Load` 快 40~80 倍。
  这两件事天然配套 —— 批量路径下根本没有"每个图元"可以挂事件。

本文件全部离线:WinRT 控件类换成鸭子替身(win32more 的枚举/结构体不需要
激活,可以直接用真的),`RecordsPage` 用 `__new__` 造壳只装被测方法要用的字段。
"""
from __future__ import annotations

import ast
import inspect
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_guide_quality_card_belongs_to_records_not_sky3d():
    """三证据导星结论归属具体拍摄记录，3D 天球不再承载诊断卡。"""
    root = Path(__file__).parents[1] / "astro_smb_gui"
    records = (root / "records.xaml").read_text(encoding="utf-8")
    sky3d = (root / "sky3d.xaml").read_text(encoding="utf-8")
    for name in ("GuideQualityCard", "GuideQualityHeadline",
                 "GuideQualityConfidence", "GuideQualityFindings",
                 "GuideQualityRing", "GuideQualityBtn"):
        assert f'x:Name="{name}"' in records
    assert 'x:Name="QualityPanel"' not in sky3d
    source = inspect.getsource(R.RecordsPage._on_guide_quality)
    assert "request_guide_quality" in source
    assert "cancel_guide_quality" in source

from astro_smb_gui import _records as R

NS = "{http://schemas.microsoft.com/winfx/2006/xaml/presentation}"
SRC_ROOT = Path(R.__file__).resolve().parent


# ---------------------------------------------------------------- WinRT 替身

class DrawEvent:
    """win32more 事件的替身:只统计 `+=` 次数。

    真实实现里"注册了几次"就等于"永久滞留了几个实例",所以次数就是泄漏量。
    """

    def __init__(self, log: list, kind: str, name: str):
        self._log, self._kind, self._name = log, kind, name

    def __iadd__(self, callback):
        self._log.append((self._kind, self._name))
        return self

    def __isub__(self, callback):        # 真实实现里这条对泄漏毫无作用
        return self


class DrawChildren(list):
    """UIElementCollection 替身(WinRT 集合没有 .add,只有 Append/Clear/Size)。"""

    def Append(self, item):
        self.append(item)

    def Clear(self):
        del self[:]

    @property
    def Size(self):
        return len(self)


class DrawElement:
    """任意 XAML 元素的鸭子替身:属性随便设,事件 `+=` 计数,子元素可追加。"""

    _EVENTS = ("Tapped", "Click", "PointerMoved", "PointerExited",
               "PointerPressed", "SizeChanged", "Loaded")

    def __init__(self, kind: str, log: list):
        self.kind = kind
        self.Children = DrawChildren()
        self.Content = None
        self.Text = None
        self.Left = self.Top = None
        for name in self._EVENTS:
            setattr(self, name, DrawEvent(log, kind, name))


def _factory(kind: str, log: list):
    return lambda *a, **k: DrawElement(kind, log)


class DrawCanvasStatics:
    """Canvas 的附加属性静态方法替身(真 Canvas 需要 WinRT 激活)。"""

    @staticmethod
    def SetLeft(el, v):
        el.Left = v

    @staticmethod
    def SetTop(el, v):
        el.Top = v


class DrawToolTipService:
    @staticmethod
    def SetToolTip(el, tip):
        el.ToolTip = tip


class DrawBrush:
    """SolidColorBrush 替身:`_common.argb_hex` 只读 `.Color` 的 A/R/G/B。"""

    def __init__(self, a, r, g, b):
        self.Color = SimpleNamespace(A=a, R=r, G=g, B=b)


class DrawLoaded:
    """XamlReader.Load 的返回值替身(调用方要 `.as_(Canvas)`)。"""

    def __init__(self, kind: str, log: list):
        self._el = DrawElement(kind, log)

    def as_(self, _type):
        return self._el


@pytest.fixture
def fakeui(monkeypatch):
    """把 `_records` 里需要 WinRT 激活的控件类换成替身;返回事件注册流水。"""
    log: list[tuple[str, str]] = []
    for name in ("Border", "StackPanel", "TextBlock", "ListViewItem",
                 "Rectangle", "Line", "Grid", "Ellipse"):
        monkeypatch.setattr(R, name, _factory(name, log), raising=False)
    monkeypatch.setattr(R, "Canvas", DrawCanvasStatics)
    monkeypatch.setattr(R, "ToolTipService", DrawToolTipService)

    loads: list[str] = []

    class _Reader:
        @staticmethod
        def Load(text):
            loads.append(text)
            ET.fromstring(text)          # 解析不过就抛,与真解析器同口径
            return DrawLoaded("LoadedCanvas", log)

    monkeypatch.setattr(R, "XamlReader", _Reader)
    return SimpleNamespace(events=log, loads=loads,
                           taps=lambda: [e for e in log if e[1] == "Tapped"])


# ---------------------------------------------------------------- 合成数据

def _tl(bars, guides=(), ticks=()):
    """_night_timeline 的产物形状:bars=(f0,f1,色号,alpha,标签,提示,run)。"""
    return {"bars": list(bars), "guides": list(guides), "ticks": list(ticks)}


def _page(fakeui, tl=None, width=1000.0):
    """只装 `_draw_timeline` 会碰到的字段的 RecordsPage 空壳。"""
    p = R.RecordsPage.__new__(R.RecordsPage)
    p.tl_canvas = DrawElement("Canvas", fakeui.events)
    p.tl_canvas.ActualWidth = width
    p._night_date = "2026-07-25"
    p._night_tl = {"2026-07-25": tl}
    p._tl_brushes = [DrawBrush(255, 0x42, 0xA5, 0xF5),
                     DrawBrush(255, 0xEF, 0x6C, 0x00)]
    p._b_grid_dim = DrawBrush(80, 0x80, 0x80, 0x80)
    p._b_label = DrawBrush(255, 0x9E, 0x9E, 0x9E)
    p._tl_guide = DrawBrush(110, 0x4C, 0xAF, 0x50)
    p._b_white = DrawBrush(235, 0xFF, 0xFF, 0xFF)
    p._tl_hit = []
    p._tl_w = 0.0
    p._tl_tip = None
    p._tl_tip_idx = -2
    return p


# ---------------------------------------------------------------- 排版几何

class TestTimelineGeometry:
    """绘制与命中反算共用的纯几何 —— 一旦走偏就会点错目标,直接钉死。"""

    def test_min_width(self):
        # 极短块也要看得见:宽度有下限
        assert R.timeline_bar_px(0.0, 0.0001, 1000.0) == (0.0, R.TL_BAR_MIN_W)
        assert R.timeline_bar_px(0.25, 0.5, 800.0) == (200.0, 200.0)

    def test_hit_each_bar_center(self):
        spans = [(0.0, 0.2), (0.3, 0.35), (0.5, 1.0)]
        w = 1000.0
        y = R.TL_BAR_Y + R.TL_BAR_H / 2.0
        for i, (f0, f1) in enumerate(spans):
            x0, bw = R.timeline_bar_px(f0, f1, w)
            assert R.timeline_hit_bar(x0 + bw / 2.0, y, spans, w) == i

    def test_miss_outside_vertical_band(self):
        spans = [(0.0, 1.0)]
        # 导星覆盖细条那一行、刻度标签那一行都不该选中目标
        assert R.timeline_hit_bar(500.0, R.TL_GUIDE_Y + 1.0, spans, 1000.0) is None
        assert R.timeline_hit_bar(500.0, R.TL_TICK_Y + 2.0, spans, 1000.0) is None
        assert R.timeline_hit_bar(500.0, -5.0, spans, 1000.0) is None

    def test_miss_between_bars(self):
        spans = [(0.0, 0.1), (0.9, 1.0)]
        y = R.TL_BAR_Y + 1.0
        assert R.timeline_hit_bar(500.0, y, spans, 1000.0) is None

    def test_narrow_bar_is_reachable(self):
        """2px 宽的块靠左右放宽才点得中(逐根挂事件时几乎点不着)。"""
        spans = [(0.5, 0.5001)]
        x0, bw = R.timeline_bar_px(*spans[0], 1000.0)
        y = R.TL_BAR_Y + 2.0
        assert R.timeline_hit_bar(x0 - R.TL_HIT_PAD_X + 0.5, y, spans, 1000.0) == 0
        assert R.timeline_hit_bar(x0 + bw + R.TL_HIT_PAD_X + 5.0, y,
                                  spans, 1000.0) is None

    def test_overlap_prefers_topmost(self):
        """两条重叠时取**后画的**(z 序在上),与真点击的结果一致。"""
        spans = [(0.0, 1.0), (0.4, 0.6)]
        y = R.TL_BAR_Y + 3.0
        assert R.timeline_hit_bar(500.0, y, spans, 1000.0) == 1

    def test_degenerate_inputs(self):
        assert R.timeline_hit_bar(10.0, R.TL_BAR_Y, [], 1000.0) is None
        assert R.timeline_hit_bar(10.0, R.TL_BAR_Y, [(0.0, 1.0)], 0.0) is None


# ---------------------------------------------------------------- 批量片段

class TestBatchFragments:
    def test_text_fragment_empty(self):
        assert R.text_fragment([]) == ""

    def test_text_fragment_escapes(self):
        """目标名带 `&`/`<` 时片段必须仍能解析 —— 否则整图退回慢路径。"""
        frag = R.text_fragment([(0.0, 0.0, 'M8 & "NGC" <x>', 10.0,
                                 "#FF112233", None)])
        root = ET.fromstring(frag)
        tb = root.find(f"{NS}TextBlock")
        assert tb.get("Text") == 'M8 & "NGC" <x>'

    def test_text_fragment_brace_prefix(self):
        """以 `{` 开头的属性值会被 XAML 当标记扩展,必须用 `{}` 转义。"""
        assert R.xaml_attr("{weird}") == "{}{weird}"
        assert R.xaml_attr("normal") == "normal"

    def test_text_fragment_invariant_numbers(self):
        frag = R.text_fragment([(1234.5, 0.25, "x", 10.0, "#FF000000", 40.0)])
        assert 'Canvas.Left="1234.50"' in frag
        assert 'Canvas.Top="0.25"' in frag
        assert "," not in frag.split('Text="x"')[1].split(">")[0]

    def test_text_fragment_width_controls_trimming(self):
        with_w = R.text_fragment([(0.0, 0.0, "x", 10.0, "#FF000000", 40.0)])
        without = R.text_fragment([(0.0, 0.0, "x", 10.0, "#FF000000", None)])
        assert "TextTrimming" in with_w and 'Width="40.00"' in with_w
        assert "TextTrimming" not in without and "Width=" not in without

    def test_text_fragment_not_hit_testable(self):
        """标签必须让点击穿透,否则挡住下面的横条会让选中落空。"""
        frag = R.text_fragment([(0.0, 0.0, "x", 10.0, "#FF000000", None)])
        assert 'IsHitTestVisible="False"' in frag

    def test_batch_canvas(self):
        assert R.batch_canvas("", "") == ""
        out = R.batch_canvas("<A/>", "", "<B/>")
        assert out.index("<A/>") < out.index("<B/>")     # 顺序即 z 序
        root = ET.fromstring(R.batch_canvas(
            R.rect_fragment([(0.0, 0.0, 1.0, 1.0, "#FF000000")]),
            R.text_fragment([(0.0, 0.0, "t", 10.0, "#FF000000", None)])))
        assert len(root.findall(f".//{NS}Rectangle")) == 1
        assert len(root.findall(f".//{NS}TextBlock")) == 1

    def test_scale_alpha(self):
        assert R.scale_alpha("#FF4CAF50", 1.0) == "#FF4CAF50"
        assert R.scale_alpha("#FF4CAF50", 0.6) == "#994CAF50"
        assert R.scale_alpha("#804CAF50", 0.5) == "#404CAF50"
        assert R.scale_alpha("#FF4CAF50", 0.0) == "#004CAF50"
        assert R.scale_alpha("bogus", 0.5) == "bogus"    # 坏输入原样返回


# ---------------------------------------------------------------- 甘特图绘制

class TestDrawTimelineBatch:
    def _bars(self):
        r1, r2, r3 = object(), object(), object()
        return [
            (0.00, 0.30, 0, 1.0, "M 8", "M 8\n21:00 ~ 22:00 · 30 帧", r1),
            (0.32, 0.33, 1, 0.6, "NGC 7293", "NGC 7293\n暂停", r2),
            (0.40, 0.95, 0, 1.0, "M 31", "M 31\n22:20 ~ 01:00 · 90 帧", r3),
        ]

    def test_single_load_and_element_counts(self, fakeui):
        bars = self._bars()
        p = _page(fakeui, _tl(bars, guides=[(0.1, 0.2), (0.5, 0.9)],
                              ticks=[(0.0, "21:00"), (0.5, "22:00")]))
        p._draw_timeline()
        # 整批一次 Load,画布上只多一个"分组容器"
        assert len(fakeui.loads) == 1
        assert p.tl_canvas.Children.Size == 1
        root = ET.fromstring(fakeui.loads[0])
        assert len(root.findall(f".//{NS}Line")) == 2               # 刻度线
        assert len(root.findall(f".//{NS}Rectangle")) == 2 + 3      # 导星 + 横条
        # 刻度标签 2 + 够宽的条内标签(0.01 那条太窄不写字)
        assert len(root.findall(f".//{NS}TextBlock")) == 2 + 2
        rects = root.findall(f".//{NS}Rectangle")
        assert all(r.get("RadiusX") == "1.00" for r in rects[:2])
        assert all(r.get("RadiusX") == "2.00" for r in rects[2:])

    def test_no_per_element_events(self, fakeui):
        """批量路径下没有"每个图元"可挂事件 —— 重画多少次都不会注册事件。"""
        p = _page(fakeui, _tl(self._bars()))
        for _ in range(4):
            p._draw_timeline()
        assert fakeui.taps() == []

    def test_hit_data_matches_drawing(self, fakeui):
        """画出来的几何与 `timeline_hit_bar` 反算必须逐条对上。"""
        bars = self._bars()
        p = _page(fakeui, _tl(bars), width=1234.0)
        p._draw_timeline()
        assert p._tl_w == 1234.0
        assert [h[2] for h in p._tl_hit] == [b[6] for b in bars]
        assert [h[3] for h in p._tl_hit] == [b[5] for b in bars]

        root = ET.fromstring(fakeui.loads[0])
        rects = root.findall(f".//{NS}Rectangle")
        spans = [(h[0], h[1]) for h in p._tl_hit]
        y = R.TL_BAR_Y + R.TL_BAR_H / 2.0
        for i, (f0, f1, *_rest) in enumerate(bars):
            x0, bw = R.timeline_bar_px(f0, f1, 1234.0)
            el = rects[i]           # guides 为空,横条就是前几个矩形
            assert float(el.get("Canvas.Left")) == pytest.approx(x0, abs=0.01)
            assert float(el.get("Width")) == pytest.approx(bw, abs=0.01)
            assert R.timeline_hit_bar(x0 + bw / 2.0, y, spans, 1234.0) == i

    def test_opacity_folded_into_fill_alpha(self, fakeui):
        """`rect_fragment` 只吃颜色,半透明(暂停/截断)要折进 A 通道。"""
        p = _page(fakeui, _tl(self._bars()))
        p._draw_timeline()
        root = ET.fromstring(fakeui.loads[0])
        fills = [r.get("Fill") for r in root.findall(f".//{NS}Rectangle")]
        assert fills[0].upper().startswith("#FF")        # alpha=1.0 原样
        assert fills[1].upper().startswith("#99")        # alpha=0.6 → 0.6*255

    def test_stale_hit_data_cleared(self, fakeui):
        """换到没有时间轴的夜次后,旧命中数据必须清掉(否则点了会选中上一夜)。"""
        p = _page(fakeui, _tl(self._bars()))
        p._draw_timeline()
        assert p._tl_hit
        p._night_tl["2026-07-25"] = None
        p._draw_timeline()
        assert p._tl_hit == [] and p._tl_w == 0.0
        assert p.tl_canvas.Children.Size == 0

    def test_too_narrow_canvas_draws_nothing(self, fakeui):
        p = _page(fakeui, _tl(self._bars()), width=50.0)
        p._draw_timeline()
        assert p.tl_canvas.Children.Size == 0 and p._tl_hit == []
        assert fakeui.loads == []

    def test_fallback_when_fragment_fails(self, fakeui, monkeypatch):
        """片段解析失败必须退回逐元素 —— 慢,但一定画得出来,且不留半截。"""
        class _Boom:
            @staticmethod
            def Load(text):
                raise RuntimeError("解析失败")

        monkeypatch.setattr(R, "XamlReader", _Boom)
        bars = self._bars()
        p = _page(fakeui, _tl(bars, guides=[(0.1, 0.2)],
                              ticks=[(0.0, "21:00")]))
        p._draw_timeline()
        kinds = [c.kind for c in p.tl_canvas.Children]
        assert kinds.count("Line") == 1
        assert kinds.count("Rectangle") == 1 + 3
        assert kinds.count("TextBlock") == 1 + 2
        assert p._tl_hit and p._tl_w == 1000.0          # 命中数据照样可用
        assert fakeui.taps() == []                      # 兜底路径也不挂事件

    def test_tooltip_uses_is_open_not_is_enabled(self):
        moved = inspect.getsource(R.RecordsPage._on_timeline_moved)
        exited = inspect.getsource(R.RecordsPage._on_timeline_exited)
        assert "IsOpen" in moved and "IsEnabled" not in moved
        assert "IsOpen" in exited and "IsEnabled" not in exited


# ---------------------------------------------------------------- 组头行复用

class TestGroupRowReuse:
    def _page(self, fakeui):
        p = R.RecordsPage.__new__(R.RecordsPage)
        p._group_cards = {}
        p._collapsed = set()
        p._night_date = "2026-07-25"
        p._b_card_bg = DrawBrush(28, 0x80, 0x80, 0x80)
        return p

    def _item(self, key="p1", title="计划 1", sub="21:00 ~ 23:00 · 3 目标"):
        return {"kind": "group", "key": key, "title": title, "sub": sub}

    def test_reused_and_registers_once(self, fakeui):
        """重铺 4 次列表只注册 1 次事件(泄漏从"重铺次数×组数"降到"组数")。"""
        p = self._page(fakeui)
        first = p._group_row(self._item())
        for _ in range(3):
            assert p._group_row(self._item()) is first
        assert len(fakeui.taps()) == 1

    def test_distinct_keys_get_distinct_rows(self, fakeui):
        p = self._page(fakeui)
        a = p._group_row(self._item(key="p1"))
        b = p._group_row(self._item(key="solo", title="单目标拍摄"))
        assert a is not b
        assert len(fakeui.taps()) == 2

    def test_cache_hit_refreshes_text(self, fakeui):
        """组键只在夜次内唯一,跨夜重名 —— 复用时必须换掉标题/副行文字。"""
        p = self._page(fakeui)
        p._group_row(self._item())
        p._group_row(self._item(title="计划 1", sub="02:00 ~ 04:30 · 1 目标"))
        card = p._group_cards["p1"]
        assert card["sub"].Text == "02:00 ~ 04:30 · 1 目标"
        assert len(fakeui.taps()) == 1

    def test_chevron_follows_collapsed_state(self, fakeui):
        p = self._page(fakeui)
        p._group_row(self._item())
        assert p._group_cards["p1"]["chev"].Text == "▾"
        p._collapsed.add(("2026-07-25", "p1"))
        p._group_row(self._item())
        assert p._group_cards["p1"]["chev"].Text == "▸"


# ------------------------------------------------------ 事件注册点的静态护栏

def _top_functions(tree: ast.Module):
    """模块级函数 + 各类的方法(**不下钻**嵌套函数,以免把 `add` 之类的
    局部闭包当成独立注册点 —— 它们本来就归属外层那次调用)。"""
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    yield sub


def _event_wiring_functions(path: Path) -> set[str]:
    """源码里所有出现"`x.<大写事件名> += …`"的函数名。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for fn in _top_functions(tree):
        for node in ast.walk(fn):
            if (isinstance(node, ast.AugAssign)
                    and isinstance(node.op, ast.Add)
                    and isinstance(node.target, ast.Attribute)
                    and node.target.attr[:1].isupper()):
                out.add(fn.name)
    return out


class TestNoPerRenderEventWiring:
    """事件只能在"每个控件一生一次"的地方注册。

    win32more 的 `_event_setters` 是类级字典且永不回收(见文件头),所以
    "每次重画/重铺就 `+=`"= 永久泄漏。允许名单里的四个函数各有理由:
    `_wire` / `_wire_events` 每个页面实例只跑一次;`_install_viewer_button`
    与 `_build_context_menu` 建的是一生一个的按钮与右键菜单;`_group_row`
    是**缓存命中就直接返回**的复用路径(由 TestGroupRowReuse 钉死)。

    新增了别的注册点 → 先确认它不是"每次渲染都跑",再加进名单。
    """

    ALLOW = {
        "_records.py": {"_wire", "_group_row"},
        "_browser.py": {"_wire_events", "_install_viewer_button",
                        "_build_context_menu"},
    }

    @pytest.mark.parametrize("name", sorted(ALLOW))
    def test_only_allowed_functions_wire_events(self, name):
        found = _event_wiring_functions(SRC_ROOT / name)
        assert found <= self.ALLOW[name], (
            f"{name} 里这些函数注册了事件但不在允许名单内: "
            f"{sorted(found - self.ALLOW[name])}")

    def test_hot_paths_never_wire(self):
        """点名保护几条**每次渲染都会跑**的路径(名单写错也拦得住)。"""
        hot = {"_records.py": {"_draw_timeline", "_draw_timeline_slow",
                               "_render_list", "_build_target_row",
                               "_build_list_gap_row", "_inert_row",
                               "_draw_sky_onto", "_sky_frame",
                               "_ov_build_scene", "_fill_timeline"},
               "_browser.py": {"_make_row", "_render", "_alt_bar",
                               "_night_chip", "_update_detail"}}
        for name, funcs in hot.items():
            found = _event_wiring_functions(SRC_ROOT / name)
            assert not (found & funcs), f"{name}: {sorted(found & funcs)}"


class TestBrowserContextMenuBuiltOnce:
    """右键菜单是**一生一次**建的(每次弹出重建 = 每次弹出泄漏一批菜单项)。

    审查工单把 `_browser.py` 的 `item.Click += handler` 列为第三处泄漏,实测
    并非如此:`_build_context_menu` 只在 `_wire_events` 里调一次,菜单常驻
    `file_list.ContextFlyout`,弹出时只走 `Opening` 回调改可见性。这条测试把
    "只在 `_wire_events` 里调一次"钉住,防止以后改成"每次弹出重建"。
    """

    def test_built_once_from_wire_events(self):
        tree = ast.parse((SRC_ROOT / "_browser.py").read_text(encoding="utf-8"))
        callers = []
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr == "_build_context_menu"):
                    callers.append(fn.name)
        assert callers == ["_wire_events"]

    def test_wire_events_called_once(self):
        tree = ast.parse((SRC_ROOT / "_browser.py").read_text(encoding="utf-8"))
        callers = [fn.name for fn in ast.walk(tree)
                   if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                   for node in ast.walk(fn)
                   if isinstance(node, ast.Call)
                   and isinstance(node.func, ast.Attribute)
                   and node.func.attr == "_wire_events"]
        assert callers == ["__init__"]


class TestTimelineUiStringsAreBmp:
    """时间轴/组头新加的 UI 字符必须是 BMP —— 星平面字符会吞掉末尾一个字。"""

    def test_no_astral_chars(self):
        text = (SRC_ROOT / "_records.py").read_text(encoding="utf-8")
        bad = sorted({ch for ch in text if ord(ch) > 0xFFFF})
        assert not bad, f"_records.py 出现星平面字符: {bad}"
