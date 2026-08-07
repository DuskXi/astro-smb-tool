"""高度角量尺:刻度 + 天文线稿图标。

**为什么不写度数。** 度数在上面那行(`高度角 35.5°`)已经写过一遍,再沿着
条子重复四个数字只是噪声;而"35° 到底算高还是低"要的是**判读**,不是精度。
四个图标(地平线 / 低空浑浊 / 通透 / 天顶)直接说的是那个高度意味着什么。

这份闸门盯的是三件**坏了也不报错**的事:

1. **刻度和填充不在同一把尺子上。** 填充是 ``f → w·f``;刻度一旦另算一套
   (比如整体内缩半个图标),40° 的刻度线就和"填充刚好到 40°"差半格。
   写这条时真踩过。
2. **越过的刻度不变色。** 那样图标就只是装饰,读者还是得回去看数字。
3. **图标被裁。** 控件高度没算上图标格子的话,下半截直接没了 —— 不报错,
   只是看不见。
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    """**autouse。** 少写一个 `qt_app` 参数就是没有 QApplication,
    而那时候造 QWidget 不是抛异常 —— 是整个解释器**直接死掉**
    (pytest 只留下半行输出,看不出是哪条)。写这份闸门时就这么栽了一次。"""
    from PySide6.QtWidgets import QApplication

    from astro_smb_qt import theme

    inst = QApplication.instance() or QApplication([])
    theme.apply(inst)
    return inst


def _gauge(alt: float, tone: str = "good", width: int = 240):
    from astro_smb_qt import widgets as W

    g = W.Gauge(max(0.0, alt) / 90.0, tone=tone, ticks=W.ALT_TICKS, span=90.0)
    g.resize(width, g.height())
    return g


def _icon_box(idx: int, width: int):
    """按文档里的几何算第 idx 个图标格子(x0, x1, y0, y1)。"""
    from astro_smb_qt import widgets as W

    ic = float(W.Gauge.ICON)
    f = W.ALT_TICKS[idx][0] / 90.0
    cx = f * width
    bx = min(max(0.0, cx - ic / 2.0), max(0.0, width - ic))
    y0 = W.Gauge.H + 4
    return int(bx), int(bx + ic), y0, int(y0 + ic)


def _dpr(img, widget) -> float:
    """抓下来的图是**设备像素**,控件坐标是**逻辑像素**。

    125% 缩放下 240 宽的控件抓出来是 300 宽 —— 拿逻辑坐标直接去索引,
    量到的是别处(zenith 那格就这么变成了"压根没画")。
    这个比例在无头 offscreen 下是 1.0,在真机上不是,所以**必须现算**。
    """
    return (img.width() / widget.width()) if widget.width() else 1.0


def _scale(box, k: float):
    return tuple(int(round(v * k)) for v in box)


def _ink(img, box):
    """格子里"画上去的"像素的平均色。

    **按 alpha 判定,不按颜色距离。** 控件抓下来是**透明底**,没画到的像素是
    ``(0,0,0,0)`` —— 拿它当"背景色"去比 RGB 距离,深色的图标会被一起滤掉
    (写这条时 zenith 那格就这么变成了"压根没画")。alpha 才是"画没画过"
    的确切信号。抗锯齿的半透明边缘按 alpha 加权,免得淡边把均值拖向黑。
    """
    x0, x1, y0, y1 = box
    rs = gs = bs = wsum = 0.0
    for x in range(x0, min(x1, img.width())):
        for y in range(y0, min(y1, img.height())):
            c = img.pixelColor(x, y)
            a = c.alpha()
            if a < 40:
                continue
            w = a / 255.0
            rs += c.red() * w
            gs += c.green() * w
            bs += c.blue() * w
            wsum += w
    if wsum <= 0:
        return None
    return (rs / wsum, gs / wsum, bs / wsum)


def _dist(a, b) -> float:
    return math.dist(a, b)


class TestTicksExist:

    def test_four_ticks_at_the_semantic_thresholds(self):
        """**刻度落在换色的界上,不是均分。** 20° / 40° 正是
        `_alt_tone` 换色的两处 —— 均分成 30/60 好看,但那样刻度解释不了
        颜色为什么变。改了判读阈值就必须同步改这里,所以直接钉在一起。"""
        from astro_smb_app.views import browser as vb
        from astro_smb_qt import widgets as W

        vals = [t[0] for t in W.ALT_TICKS]
        assert vals == [0.0, 20.0, 40.0, 90.0]
        # 与判读函数对账:19.9 与 20.1 必须分属两个语义色,40 同理
        assert vb._alt_tone(19.9) != vb._alt_tone(20.1)
        assert vb._alt_tone(39.9) != vb._alt_tone(40.1)

    def test_no_degree_text(self):
        """用户要的是"图标当标,而不是写度数"。"""
        from astro_smb_qt import widgets as W

        g = W.Gauge(0.5, tone="good", ticks=W.ALT_TICKS, span=90.0)
        assert not g._lo and not g._hi

    def test_icons_are_line_art_not_characters(self):
        """不能拿字符顶替:emoji 是星平面字符(本仓库另有一处坑),
        而 `★` 这类 BMP 符号在 11px 上糊成一团。"""
        src = (ROOT / "astro_smb_qt" / "widgets.py").read_text(encoding="utf-8")
        at = src.index("def alt_icon")
        body = src[at:src.index("\nclass Gauge", at)]
        assert "drawText" not in body, "图标用文字画了"
        assert "drawLine" in body and "drawArc" in body and "drawEllipse" in body

    def test_haze_lines_widen_downward(self, qt_app):
        """三条等长会读成菜单图标 —— 必须**越往下越长**才像"越近地平越浑"。"""
        from PySide6.QtGui import QImage, QPainter

        from astro_smb_qt import theme, widgets as W

        n = 44                                # 放大画,免得取整把差别抹平
        img = QImage(n, n, QImage.Format_ARGB32)
        img.fill(0)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        W.alt_icon(p, "haze", W.QRectF(0, 0, n, n),
                   W.QColor(theme.Q.TEXT_FAINT))
        p.end()
        runs = []
        for y in range(n):
            xs = [x for x in range(n) if img.pixelColor(x, y).alpha() >= 40]
            if xs:
                runs.append((y, max(xs) - min(xs)))
        widths = [w for _y, w in runs]
        assert len(widths) >= 3, runs
        assert widths[0] < widths[-1], f"横纹没有越往下越长: {runs}"

    def test_height_leaves_room_for_icons(self, qt_app):
        """算漏图标格子的话下半截被裁 —— 不报错,只是看不见。"""
        from astro_smb_qt import widgets as W

        g = _gauge(45.0)
        assert g.height() >= W.Gauge.H + W.Gauge.ICON


class TestTickGeometry:

    def test_ticks_share_the_fill_scale(self, qt_app):
        """**刻度线必须落在填充的同一把尺子上。**

        用"填充边缘恰好压住 40° 刻度"来验:把 frac 设成 40/90,填充右缘的
        x 应当和第三个刻度的 cx 重合(±1px)。刻度整体内缩过半个图标时
        这里差 5~6 px,而肉眼只看得出"对不上"。
        """
        from astro_smb_qt import theme, widgets as W

        width = 240
        g = _gauge(40.0)
        img = g.grab().toImage()
        k = _dpr(img, g)
        tone = theme.tone_color("good")
        # 沿着条子中线找填充右缘(设备像素,量完再折回逻辑像素)
        y = int((W.Gauge.H // 2) * k)
        edge = 0
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            if _dist((c.red(), c.green(), c.blue()),
                     (tone.red(), tone.green(), tone.blue())) < 40:
                edge = x
        assert abs(edge / k - (40.0 / 90.0) * width) <= 2, edge / k

    def test_the_icon_itself_sits_on_that_scale(self, qt_app):
        """**量图标的实际位置,不是量填充。**

        上一条验的是填充边缘落在 ``f·w``;可刻度要是另算一套尺子,填充
        一动不动 —— 那条测试照样绿。所以这里直接找刻度图标画在哪:
        取墨迹的横向重心,必须压在 ``f·w`` 上。

        **中间那两个刻度都要量,尤其是 20° 那个。** 内缩半个图标造成的偏移是
        ``(ic/2)·(1-2f)``:两端被夹回边界看不出来,40° 处只差 0.6px ——
        比容差还小。只量 40° 的话这条变异**活得好好的**(第一版就这么放过去了),
        20° 处才差到 3px。
        """
        from astro_smb_qt import widgets as W

        width = 240
        ic = float(W.Gauge.ICON)
        g = _gauge(90.0)                     # 全亮,免得淡色被 alpha 门槛滤掉
        img = g.grab().toImage()
        k = _dpr(img, g)

        # **在整条图标带上找墨迹团,不要在"按正确公式算出来的窗口"里找。**
        # 拿正确公式开窗等于先假设了结论:刻度一旦挪位,窗口把它裁掉一半,
        # 重心被拖回窗口中间,变异就此蒙混过关(第一版正是这么放过去的)。
        y0 = int((W.Gauge.H + 4) * k)
        y1 = int(img.height())
        cols = []
        for x in range(img.width()):
            wsum = sum(img.pixelColor(x, y).alpha() / 255.0
                       for y in range(y0, y1)
                       if img.pixelColor(x, y).alpha() >= 40)
            cols.append(wsum)
        groups, cur = [], []
        for x, wv in enumerate(cols):
            if wv > 0:
                cur.append((x, wv))
            elif cur:
                groups.append(cur)
                cur = []
        if cur:
            groups.append(cur)
        assert len(groups) == len(_ticks()), (
            f"图标带上找到 {len(groups)} 团墨迹,应当是 {len(_ticks())} 个刻度")

        for idx, grp in enumerate(groups):
            got = sum(x * wv for x, wv in grp) / sum(wv for _x, wv in grp) / k
            cx = (_ticks()[idx][0] / 90.0) * width
            # 两端的图标格子会被夹回边界内,重心相应落在半个图标处
            want = min(max(cx, ic / 2.0), width - ic / 2.0)
            assert abs(got - want) <= 1.5, (idx, got, want)

    def test_end_icons_stay_inside(self, qt_app):
        """0° 与 90° 的图标要贴边收进来,否则半个画到控件外。"""
        width = 240
        for idx in (0, len(_ticks()) - 1):
            x0, x1, _y0, _y1 = _icon_box(idx, width)
            assert x0 >= 0 and x1 <= width, (idx, x0, x1)


def _ticks():
    from astro_smb_qt import widgets as W

    return W.ALT_TICKS


class TestPassedColouring:
    """越过的刻度用语义色,没到的留淡色 —— 图标本身就是结论。"""

    def _classify(self, alt: float, idx: int, tone: str = "good"):
        from PySide6.QtGui import QColor

        from astro_smb_qt import theme

        width = 240
        g = _gauge(alt, tone=tone, width=width)
        img = g.grab().toImage()
        ink = _ink(img, _scale(_icon_box(idx, width), _dpr(img, g)))
        assert ink is not None, f"第 {idx} 个图标压根没画"
        live = theme.tone_color(tone)
        faint = QColor(theme.Q.TEXT_FAINT)
        d_live = _dist(ink, (live.red(), live.green(), live.blue()))
        d_faint = _dist(ink, (faint.red(), faint.green(), faint.blue()))
        return "live" if d_live < d_faint else "faint"

    def test_below_a_tick_stays_faint(self, qt_app):
        assert self._classify(35.0, 2) == "faint"

    def test_above_a_tick_lights_up(self, qt_app):
        assert self._classify(45.0, 2) == "live"

    def test_zenith_only_at_the_top(self, qt_app):
        assert self._classify(45.0, 3) == "faint"
        assert self._classify(90.0, 3) == "live"

    def test_horizon_always_lit(self, qt_app):
        """0° 的刻度永远算"已越过" —— 条子从那里起步。"""
        assert self._classify(1.0, 0) == "live"


class TestWiring:

    def test_browser_passes_ticks_and_span(self):
        src = (ROOT / "astro_smb_qt" / "pages" / "browser.py").read_text(
            encoding="utf-8")
        at = src.index('bar[0] == "altbar"')
        body = src[at:at + 700]
        assert "ticks=W.ALT_TICKS" in body
        assert "span=90.0" in body, "少了 span,90° 会被当成 1.0"

    def test_tooltip_explains_the_icons(self, qt_app):
        """图标再直白也得有个说明入口 —— 不能让人猜。"""
        from astro_smb_qt import widgets as W

        g = W.Gauge(0.4, tone="good", ticks=W.ALT_TICKS, span=90.0)
        tip = g.tick_tooltip()
        assert "地平线" in tip and "天顶" in tip

    def test_volume_gauge_still_uses_text(self, qt_app):
        """卷容量那条是两端文字,不该被这次改动波及。"""
        from astro_smb_qt import widgets as W

        g = W.Gauge(0.5, tone="good", lo="0", hi="3.7 TB")
        assert g._lo and not g._ticks
        assert not g.grab().toImage().isNull()
