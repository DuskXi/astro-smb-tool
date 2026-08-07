"""可复用的 alt-az 天球雷达绘制(仰视:北上·东左)。

- ``radar_xy``: 极坐标几何(各页共用,保证投影一致);
- ``MiniRadar``: 小型单目标雷达,浏览页详情面板用 —— 画地平圈/高度圈/方位标注
  + 一个目标点与 alt/az 说明。

只能在 UI 线程使用(持有并操作 XAML Canvas)。
"""
from __future__ import annotations

import math

from win32more.Microsoft.UI.Xaml import Visibility
from win32more.Microsoft.UI.Xaml.Controls import Canvas, TextBlock
from win32more.Microsoft.UI.Xaml.Media import SolidColorBrush
from win32more.Microsoft.UI.Xaml.Shapes import Ellipse, Line
from win32more.Windows.UI import Color

from astro_smb import astro
from astro_smb.i18n import gettext as _


def radar_xy(alt_deg: float, az_deg: float,
             cx: float, cy: float, radius: float) -> tuple[float, float]:
    """alt/az → 雷达平面坐标。r = R·(90-alt)/90;北上、**东左**(仰视惯例)。"""
    r = radius * (90.0 - max(-5.0, min(90.0, alt_deg))) / 90.0
    az = math.radians(az_deg)
    return cx - r * math.sin(az), cy - r * math.cos(az)


def _brush(r: int, g: int, b: int, a: int = 255) -> SolidColorBrush:
    br = SolidColorBrush()
    br.Color = Color(A=a, R=r, G=g, B=b)
    return br


class MiniRadar:
    """单目标迷你雷达。construct 后调 ``show(ra, dec)`` / ``clear()``。"""

    def __init__(self, canvas: Canvas, size: float = 190.0):
        self.canvas = canvas
        self.size = size
        canvas.Width = size
        canvas.Height = size
        self._ring = _brush(128, 128, 128, 150)
        self._ring_dim = _brush(128, 128, 128, 70)
        self._text = _brush(128, 128, 128, 210)
        self._dot_up = _brush(76, 175, 80)       # 地平线上:绿
        self._dot_down = _brush(230, 160, 0)     # 地平线下:琥珀
        self._accent = _brush(0, 120, 215)

    # ---------- 绘制 ----------

    def clear(self) -> None:
        self.canvas.Children.Clear()

    def _circle(self, r: float, brush: SolidColorBrush) -> None:
        c = self.size / 2
        e = Ellipse()
        e.Width = e.Height = r * 2
        e.Stroke = brush
        e.StrokeThickness = 1.0
        self.canvas.Children.Append(e)
        Canvas.SetLeft(e, c - r)
        Canvas.SetTop(e, c - r)

    def _label(self, text: str, x: float, y: float,
               size: float = 10.0, brush: SolidColorBrush | None = None) -> None:
        tb = TextBlock()
        tb.Text = text
        tb.FontSize = size
        tb.Foreground = brush or self._text
        self.canvas.Children.Append(tb)
        Canvas.SetLeft(tb, x)
        Canvas.SetTop(tb, y)

    def _frame(self) -> None:
        c = self.size / 2
        radius = c - 12
        self._circle(radius, self._ring)             # 地平线
        self._circle(radius * 2 / 3, self._ring_dim)  # alt 30
        self._circle(radius / 3, self._ring_dim)      # alt 60
        for x1, y1, x2, y2 in ((c - radius, c, c + radius, c),
                               (c, c - radius, c, c + radius)):
            ln = Line()
            ln.X1, ln.Y1, ln.X2, ln.Y2 = x1, y1, x2, y2
            ln.Stroke = self._ring_dim
            ln.StrokeThickness = 1.0
            self.canvas.Children.Append(ln)
        self._label(_("北"), c - 6, 0)
        self._label(_("南"), c - 6, self.size - 14)
        self._label(_("东"), 0, c - 7)
        self._label(_("西"), self.size - 12, c - 7)

    def show(self, ra_deg: float, dec_deg: float,
             lat_deg: float, lon_deg: float, unix_ts: float,
             caption: str | None = None) -> tuple[float, float]:
        """画框架 + 目标点,返回 (alt, az)。地平线下的点画在圈外沿并降透明度。"""
        self.clear()
        self._frame()
        alt, az = astro.altaz(ra_deg, dec_deg, lat_deg, lon_deg, unix_ts)
        c = self.size / 2
        radius = c - 12
        x, y = radar_xy(max(alt, -2.0), az, c, c, radius)
        dot = Ellipse()
        dot.Width = dot.Height = 9
        dot.Fill = self._dot_up if alt >= 0 else self._dot_down
        if alt < 0:
            dot.Opacity = 0.55
        self.canvas.Children.Append(dot)
        Canvas.SetLeft(dot, x - 4.5)
        Canvas.SetTop(dot, y - 4.5)
        ring = Ellipse()                     # 高亮描边
        ring.Width = ring.Height = 15
        ring.Stroke = self._accent
        ring.StrokeThickness = 1.5
        self.canvas.Children.Append(ring)
        Canvas.SetLeft(ring, x - 7.5)
        Canvas.SetTop(ring, y - 7.5)
        if caption:
            self._label(caption, 2, self.size - 28, 10.0, self._text)
        return alt, az
