"""Qt 前端的**样式门禁**。

这条测试就是"颜值一直维持"的执行机制。规矩:

> ``astro_smb_qt/pages/*.py`` 里不许出现任何字面色值,不许直接 ``setStyleSheet``,
> 不许自己构造 ``QColor``,不许给 ``setSpacing``/``setContentsMargins`` 传裸数字。
> 所有外观来自 ``astro_smb_qt/theme.py`` 与 ``astro_smb_qt/widgets.py``。

**为什么必须是测试而不是约定。** 这个仓库反复验证过同一件事:没有门禁的约定,
三页之内必然失效 —— 第一页认真调间距,第二页图快写个 ``#2A2A2A``,第三页开始
每个控件各自 ``setStyleSheet``,最后谁也说不清"卡片的底色到底是哪个"。

**这份文件刻意不 import PySide6**:静态扫描不需要它,而 PySide6 不在
``pyproject.toml`` 里(用 ``uv run --with pyside6`` 临时注入)。需要真的建
调色板对象的那几条用 ``importorskip`` 单独跳。

写断言的纪律:**每一条都要能定位到具体那一段**。整份文件里 grep 一个字符串
那种过松的包含检查,把被测的那一处删掉、字符串在别处还在,测试照样绿。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
QT_DIR = ROOT / "astro_smb_qt"
PAGES_DIR = QT_DIR / "pages"

pytestmark = pytest.mark.skipif(not QT_DIR.is_dir(),
                                reason="没有 astro_smb_qt 包")

#: `#RGB` / `#RGBA` / `#RRGGBB` / `#AARRGGBB` —— 后面不能再跟十六进制位,
#: 免得把 `#FF` + 格式占位符那种拼接误判成整段色值
HEX_COLOR = re.compile(r"#(?:[0-9A-Fa-f]{3,4}|[0-9A-Fa-f]{6}|[0-9A-Fa-f]{8})\Z")


def page_files() -> list[Path]:
    return sorted(p for p in PAGES_DIR.glob("*.py") if p.name != "__init__.py")


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _string_constants(tree: ast.AST):
    """所有字符串字面量,含 f-string 的字面片段。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node, node.value


def _called_attrs(tree: ast.AST):
    """所有 ``obj.method(...)`` 调用 → (节点, 方法名)。"""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            yield node, node.func.attr


# ================================================================ 页面纪律

def test_pages_exist():
    """先确认真的扫到了页面 —— 否则下面每一条都会因为"没有文件"而空过。"""
    files = page_files()
    assert len(files) >= 5, f"只扫到 {len(files)} 个页面模块,门禁形同虚设"


@pytest.mark.parametrize("path", page_files(), ids=lambda p: p.name)
def test_page_has_no_literal_color(path: Path):
    """页面里不许出现字面色值。颜色只能来自 theme。"""
    bad = []
    for node, text in _string_constants(_tree(path)):
        if HEX_COLOR.match(text.strip()):
            bad.append((node.lineno, text))
    assert not bad, (
        f"{path.name} 里有字面色值 {bad} —— 改成 theme.C.<名字> / theme.Q.<名字>;"
        "缺哪一档就去 theme.py 里加,别在页面里就地写一个")


@pytest.mark.parametrize("path", page_files(), ids=lambda p: p.name)
def test_page_never_calls_setstylesheet(path: Path):
    """页面不许自己写样式表。样式只能来自 theme.stylesheet()。"""
    hits = [n.lineno for n, name in _called_attrs(_tree(path))
            if name == "setStyleSheet"]
    assert not hits, (
        f"{path.name}:{hits} 直接调了 setStyleSheet —— 需要新外观就去 widgets.py "
        "加一个复用件或变体,别在页面里旁路主题")


@pytest.mark.parametrize("path", page_files(), ids=lambda p: p.name)
def test_page_does_not_construct_colors(path: Path):
    """页面不许 ``QColor(...)``。红光模式靠 theme 统一映射,自己造的颜色躲不过去。"""
    bad = []
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Call):
            fn = node.func
            name = (fn.id if isinstance(fn, ast.Name) else
                    fn.attr if isinstance(fn, ast.Attribute) else "")
            if name in ("QColor", "QBrush", "QPalette"):
                bad.append((node.lineno, name))
    assert not bad, (
        f"{path.name} 里自己造了颜色 {bad} —— 用 theme.Q.<名字> / "
        "theme.tone_color(tone) / theme.alpha(...)")


@pytest.mark.parametrize("path", page_files(), ids=lambda p: p.name)
def test_page_layout_numbers_come_from_theme(path: Path):
    """间距不许是魔法数字。

    ``setSpacing(8)`` 这种一旦散开,每页的留白就各调各的 —— 而"留白舍得给"
    恰恰是这套界面看起来贵不贵的关键。用 ``W.vbox(gap=...)`` 或
    ``theme.Space.*``。
    """
    bad = []
    for node, name in _called_attrs(_tree(path)):
        if name not in ("setSpacing", "setContentsMargins"):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, (int, float)):
                bad.append((node.lineno, name, arg.value))
    assert not bad, (
        f"{path.name} 里有魔法数字间距 {bad} —— 用 W.vbox(gap=...)/W.hbox(gap=...) "
        "或 theme.Space.<名字>")


@pytest.mark.parametrize("path", page_files(), ids=lambda p: p.name)
def test_page_does_not_hand_roll_cards(path: Path):
    """卡片只能来自 ``widgets.Card``。

    各页自己 ``QFrame`` 加边框,就是三页之后有三种卡片的开始。
    """
    bad = []
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in ("QFrame", "QGroupBox"):
            bad.append((node.lineno, node.func.id))
    assert not bad, (
        f"{path.name} 里手搓了容器 {bad} —— 用 W.Card / W.Inset")


@pytest.mark.parametrize("path", page_files(), ids=lambda p: p.name)
def test_page_theme_names_exist(path: Path):
    """页面引用的每一个 ``theme.C.X`` / ``theme.Q.X`` 都必须真的存在。

    ``theme.C`` 走 ``__getattr__``,拼错名字要到**运行到那一行**才炸 ——
    而那一行可能藏在某个只有真机才走到的分支里。
    """
    known = set(_palette_fields())
    bad = []
    for node in ast.walk(_tree(path)):
        if not isinstance(node, ast.Attribute):
            continue
        owner = node.value
        if not (isinstance(owner, ast.Attribute) and owner.attr in ("C", "Q")
                and isinstance(owner.value, ast.Name)
                and owner.value.id == "theme"):
            continue
        if node.attr not in known and node.attr != "mode":
            bad.append((node.lineno, node.attr))
    assert not bad, (
        f"{path.name} 引用了主题里没有的颜色 {bad} —— "
        f"可选:{sorted(known)}")


# ================================================================ 主题自身

def _palette_fields() -> list[str]:
    """静态解析 ``theme.Palette`` 的字段名(不 import PySide6)。"""
    tree = _tree(QT_DIR / "theme.py")
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "Palette":
            return [b.target.id for b in node.body
                    if isinstance(b, ast.AnnAssign) and isinstance(b.target, ast.Name)]
    raise AssertionError("theme.py 里找不到 Palette 类")


def _palette_literal(name: str) -> dict[str, str]:
    """静态取出 ``NORMAL = Palette(...)`` / ``RED = Palette(...)`` 的关键字实参。"""
    tree = _tree(QT_DIR / "theme.py")
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        target = node.targets[0]
        if not (isinstance(target, ast.Name) and target.id == name):
            continue
        assert isinstance(node.value, ast.Call), f"{name} 不是 Palette(...) 调用"
        return {kw.arg: kw.value for kw in node.value.keywords if kw.arg}
    raise AssertionError(f"theme.py 里找不到调色板 {name}")


def _palette_names() -> list[str]:
    """`PALETTES` 里登记的调色板变量名。

    **不能写死一串名字。** 第一版写的是 `("NORMAL", "RED")`,加白天档之后
    那一档根本没被检查过 —— 少一个键在它里面就是某个控件取不到色,而门禁
    照样绿。改成从 `PALETTES = {...}` 那行的值里读。
    """
    import ast
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "astro_smb_qt"
           / "theme.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(src)):
        if (isinstance(node, ast.Assign) and node.targets
                and getattr(node.targets[0], "id", "") == "PALETTES"):
            return [v.id for v in node.value.values
                    if isinstance(v, ast.Name)]
    raise AssertionError("theme.py 里找不到 PALETTES")


def test_every_palette_defines_every_field():
    """每一套调色板的**键都必须完全一致**。

    少一个键在那个模式下就是某个控件颜色取不到 —— 表现为透明或黑底黑字,
    不报错,只是那一块看不见了。
    """
    fields = set(_palette_fields())
    names = _palette_names()
    assert len(names) >= 3, f"只登记了 {names} —— 白天档是不是没进 PALETTES"
    for name in names:
        got = set(_palette_literal(name))
        assert got == fields, (
            f"调色板 {name} 与 Palette 字段对不上:缺 {sorted(fields - got)},"
            f"多 {sorted(got - fields)}")


def test_light_palette_is_actually_light():
    """白天档必须是**浅底深字**,而且层次是"卡片比底更亮"。

    直接把深色那套的语义色抄过来是最容易犯的:`#4FBF87` 在白底上几乎看不见。
    """
    pytest.importorskip("PySide6")
    from astro_smb_qt import theme

    def lum(hex_str: str) -> float:
        r, g, b = _rgb(hex_str)
        return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0

    p = theme.LIGHT
    assert lum(p.BG) > 0.7, f"白天档的底不够亮: {p.BG}"
    assert lum(p.TEXT) < 0.35, f"白天档的正文不够深: {p.TEXT}"
    # 浅色主题里卡片比窗口底**更白**(深色主题是反过来的)
    assert lum(p.SURFACE) > lum(p.BG) > lum(p.BG_ALT), (
        "白天档的表面层次反了 —— 卡片应当比底更亮")
    # 语义三档在白底上都要读得出来。**判据用 WCAG 对比度,不是朴素亮度差** ——
    # 第一版用 `lum(BG) - lum(v) > 0.25`,而深色主题的 `#4FBF87` 在白底上
    # 亮度差有 0.36(过关)、对比度只有 2.09:1(实际几乎看不见),变异因此活了。
    # 这些是 12px 的数值文本 = 正文,取 AA 的 4.5:1。
    for field in ("OK", "WARN", "BAD", "ACCENT"):
        v = getattr(p, field)
        got = _contrast(v, p.BG)
        assert got >= 4.5, (
            f"白天档的 {field}={v} 对底色只有 {got:.2f}:1,低于 AA 正文的 4.5:1 "
            f"—— 深色主题那套语义色直接抄过来就是这个下场")


def test_mode_cycle_visits_every_mode():
    """切换按钮要能走遍每一档 —— 两档时代的 `A if x==B else B` 会漏掉新的那档。"""
    pytest.importorskip("PySide6")
    from astro_smb_qt import theme

    start = theme.C.mode
    try:
        seen = {theme.C.mode}
        for _ in range(len(theme.MODES) + 1):
            seen.add(theme.toggle_mode())
        assert seen == set(theme.MODES), (
            f"循环只走到 {sorted(seen)},应当是 {sorted(theme.MODES)}")
    finally:
        theme.set_mode(start)


def test_red_palette_is_actually_red():
    """红光模式里每一档颜色都必须是**红占优**。

    这不是审美问题:蓝绿波段最毁暗适应,恢复要二三十分钟。一个抄漏没改的
    青色强调色在常规模式下看着挺好,在望远镜旁边就是事故。
    """
    pytest.importorskip("PySide6")
    from astro_smb_qt import theme

    offenders = []
    for field in _palette_fields():
        value = getattr(theme.RED, field)
        for hex_str in _hex_values(value):
            r, g, b = _rgb(hex_str)
            if g > r or b > r:
                offenders.append((field, hex_str))
    assert not offenders, f"红光配色里有非红占优的颜色: {offenders}"


def _contrast(a: str, b: str) -> float:
    """WCAG 2.x 对比度 ``(L1+0.05)/(L2+0.05)``,sRGB 线性化后算相对亮度。"""
    def lin(c: float) -> float:
        c /= 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    def rel(hex_str: str) -> float:
        r, g, b = _rgb(hex_str)
        return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)

    la, lb = rel(a), rel(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def _hex_values(value):
    """字段值可能是单个色串,也可能是嵌套的调色板元组。"""
    if isinstance(value, str):
        yield value
    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from _hex_values(item)


def _rgb(hex_str: str) -> tuple[int, int, int]:
    s = hex_str.lstrip("#")
    if len(s) == 8:
        s = s[2:]
    return int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)


def test_stylesheet_follows_the_current_mode():
    """样式表必须**跟着模式走**。

    断言限定到具体那一段(``#Chip[tone="ok"]`` 那条规则),不是"整份里出现过
    这个颜色" —— 后者太松:把被测的那处删掉,颜色在别的规则里还在,照样绿。
    """
    pytest.importorskip("PySide6")
    from astro_smb_qt import theme

    def chip_ok_rule(css: str) -> str:
        at = css.index('#Chip[tone="ok"]')
        return css[at:css.index("}", at)]

    theme.set_mode(theme.MODE_NORMAL)
    normal_rule = chip_ok_rule(theme.stylesheet())
    theme.set_mode(theme.MODE_RED)
    red_rule = chip_ok_rule(theme.stylesheet())
    theme.set_mode(theme.MODE_NORMAL)

    assert theme.NORMAL.OK.lower() in normal_rule.lower()
    assert theme.RED.OK.lower() in red_rule.lower()
    assert theme.NORMAL.OK.lower() not in red_rule.lower(), (
        "红光模式的胶囊规则里还留着常规配色 —— set_mode 没有真的生效")


def test_screen_color_filters_in_red_mode():
    """共享视图层给的色值(天球圈、treemap 配色)在红光模式下要被滤成红。

    那一层不知道也不该知道有红光模式,所以过滤只能在**显示这一侧**做,
    而且只有一处实现(``theme.screen_color``)。
    """
    pytest.importorskip("PySide6")
    from astro_smb_qt import theme

    green = "#FF4CAF50"          # views.skychart 的"地平线上"绿点
    theme.set_mode(theme.MODE_NORMAL)
    same = theme.screen_color(green)
    assert (same.red(), same.green(), same.blue()) == (0x4C, 0xAF, 0x50), \
        "常规模式下 screen_color 必须是恒等"
    theme.set_mode(theme.MODE_RED)
    tinted = theme.screen_color(green)
    theme.set_mode(theme.MODE_NORMAL)
    assert tinted.green() <= tinted.red() and tinted.blue() <= tinted.red(), \
        "红光模式下 screen_color 没有把绿点滤成红"


def test_every_page_is_registered():
    """每个页面模块里的 Page 子类都要在 ``PAGE_CLASSES`` 里注册。

    写完一页忘了注册,它就是一个永远打不开的死模块 —— 而且不报错。
    """
    registered = set()
    tree = _tree(PAGES_DIR / "__init__.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) \
                and node.targets[0].id == "PAGE_CLASSES":
            for value in node.value.values:
                if isinstance(value, ast.Name):
                    registered.add(value.id)
    defined = set()
    for path in page_files():
        if path.name == "base.py":
            continue                 # 基类与页头不是"一页"
        for node in _tree(path).body:
            if isinstance(node, ast.ClassDef) and node.name.endswith("Page") \
                    and not node.name.startswith("_"):
                defined.add(node.name)
    assert defined, "一个页面类都没扫到 —— 这条断言没在测任何东西"
    assert defined <= registered, f"这些页面没注册: {sorted(defined - registered)}"
