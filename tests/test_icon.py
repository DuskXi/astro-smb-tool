"""应用图标:一个源、三处消费、必须在 16px 上还认得出来。

图标缺了不会让程序起不来 —— 它只是**难看**,而且难看得没人会写 issue。
所以这里盯的是那几条"少了也不报错"的接线。
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SVG = ROOT / "packaging" / "icon.svg"
ICO = ROOT / "packaging" / "icon.ico"
ICNS = ROOT / "packaging" / "icon.icns"


class TestTheSourceIsTheSvg:

    def test_the_svg_exists(self):
        assert SVG.is_file(), "图标的唯一定稿没了,别的尺寸全从它渲"

    def test_it_uses_the_theme_accent(self):
        """**图标和界面必须是一套颜色。** 对不上比没有图标更糟 ——
        那看起来像装错了软件。"""
        from astro_smb_qt import theme

        src = SVG.read_text(encoding="utf-8")
        accent = theme.NORMAL.ACCENT.upper()
        assert accent in src.upper(), (
            f"SVG 里没有主题强调色 {accent} —— 改配色时图标漏改了?")

    def test_it_has_no_text(self):
        """**图标里不许有字。** 16px 上一个字都看不清,而且字要翻译。"""
        src = SVG.read_text(encoding="utf-8")
        assert "<text" not in src, "图标里有文字元素"


class TestTheGeneratedFilesAreThere:
    """产物是提交进仓库的 —— 打包时不能依赖一个要 QtSvg 的生成步骤
    (那会把 QtSvg 拖进发行包,而界面本身用不到它)。"""

    def test_windows_ico(self):
        assert ICO.is_file(), "缺 icon.ico —— Windows 的 exe 会用回默认图标"

    def test_macos_icns(self):
        assert ICNS.is_file(), "缺 icon.icns"

    def test_the_ico_really_contains_the_small_sizes(self):
        """**16 那一档必须真的在里面。** 少了它资源管理器会拿 32 的缩,
        而那一档恰好是任务栏天天在用的。
        """
        from PIL import Image

        with Image.open(ICO) as im:
            got = {s[0] for s in im.info.get("sizes", ())}
        for need in (16, 32, 256):
            assert need in got, f".ico 里没有 {need}px 那一档,只有 {sorted(got)}"

    def test_runtime_pngs_cover_the_small_end(self):
        from astro_smb_app.icons import icon_files

        sizes = {int(p.stem.split("-")[1]) for p in icon_files()}
        assert {16, 32} <= sizes, f"运行时图标缺小尺寸: {sorted(sizes)}"


class TestItIsWiredIntoAllThreePlaces:
    """一个源,三处消费。**少接一处的表现都是"某个地方图标是默认的"** ——
    不报错,而且要在那个地方看一眼才发现。
    """

    def test_pyinstaller_gets_it(self):
        src = (ROOT / "packaging" / "astro-smb-tool.spec").read_text(
            encoding="utf-8")
        assert "icon=_ICON" in src, "spec 没设 exe 图标"
        assert "icon.ico" in src and "icon.icns" in src, "两个平台没分开给"

    def test_the_runtime_pngs_are_bundled(self):
        """exe 自己的图标和**窗口**图标是两回事 —— PNG 不打进包的话,
        冻结之后任务栏有图标而窗口没有。"""
        src = (ROOT / "packaging" / "astro-smb-tool.spec").read_text(
            encoding="utf-8")
        assert "astro_smb_app/icons" in src, "随包数据里没有运行时图标"

    def test_qt_sets_the_window_icon(self):
        import ast

        src = (ROOT / "astro_smb_qt" / "__main__.py").read_text(
            encoding="utf-8")
        tree = ast.parse(src)
        fn = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef) and n.name == "_set_app_icon"),
                  None)
        assert fn is not None, "没有设置窗口图标的函数"
        body = ast.unparse(fn)
        assert "setWindowIcon" in body
        # **多档,不是一张大的** —— 只给 256 的话 16px 是 Qt 缩出来的
        assert "for f in files" in body, "只塞了一张图,小尺寸会糊"

    def test_main_actually_calls_it(self):
        """函数写了没人调 = 白写。"""
        import ast

        src = (ROOT / "astro_smb_qt" / "__main__.py").read_text(
            encoding="utf-8")
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "main")
        assert "_set_app_icon(app)" in ast.unparse(fn)

    def test_the_selftest_checks_it(self):
        """图标是"用路径打开、不是 import 进来"的那一类 —— 打包漏了不报错。"""
        src = (ROOT / "astro_smb_qt" / "__main__.py").read_text(
            encoding="utf-8")
        assert "窗口图标" in src, "--selftest 不查图标"


class TestItSurvivesSixteenPixels:
    """**图标九成的时间活在 16×16 上。**

    同一轮画过两个信息更多的候选(叠传输箭头、装星野),16px 上一个糊成
    噪点、一个只剩个圆圈。这条量的是"缩到 16 之后还剩多少结构"。
    """

    def test_the_smallest_png_is_not_a_flat_blob(self):
        """16px 那张里得有**至少三种明显不同的颜色**:底、环、核。
        只剩一两种就说明结构塌了。"""
        pytest.importorskip("PIL")
        from PIL import Image

        from astro_smb_app.icons import icon_dir

        p = icon_dir() / "app-16.png"
        assert p.is_file()
        with Image.open(p) as im:
            px = list(im.convert("RGB").getdata())
        # 量化到 32 级,避免抗锯齿产生的一堆相近色被当成"结构"
        buckets = {tuple(v // 32 for v in c) for c in px}
        assert len(buckets) >= 3, (
            f"16px 上只剩 {len(buckets)} 种色块 —— 环和核糊成一团了")

    def test_the_centre_is_the_accent_colour(self):
        """核在正中间,而且是强调色 —— 缩到 16px 之后它还得在。"""
        pytest.importorskip("PIL")
        from PIL import Image

        from astro_smb_app.icons import icon_dir
        from astro_smb_qt import theme

        want = theme.NORMAL.ACCENT.lstrip("#")
        wr, wg, wb = (int(want[i:i + 2], 16) for i in (0, 2, 4))
        with Image.open(icon_dir() / "app-16.png") as im:
            r, g, b = im.convert("RGB").getpixel((8, 8))
        assert abs(r - wr) < 40 and abs(g - wg) < 40 and abs(b - wb) < 40, (
            f"16px 正中间是 ({r},{g},{b}),不是强调色 ({wr},{wg},{wb})")
