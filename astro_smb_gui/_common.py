"""GUI 各页共享的小工具与常量。"""

from __future__ import annotations

import os
import time
from pathlib import Path

from win32more.Windows.Foundation import AsyncStatus, IAsyncInfo, Uri

from astro_smb.client import RemoteEntry
from astro_smb.util import sanitize_local_name
from astro_smb.i18n import gettext as _

# 与界面无关的那几个已下沉到 astro_smb_app.entries —— 新前端也要用,
# 它不该为几个纯函数去 import 一个已冻结的 WinUI 包。这里 re-export 保持兼容。
from astro_smb_app.entries import (  # noqa: F401
    FITS_EXTS,
    IMAGE_EXTS,
    TEXT_EXTS,
    ext_category,
    looks_like_local_path,
    sort_key,
    sorted_entries,
    unique_local,
)

def unbox_str(obj) -> str:
    """把 WinRT 装箱的字符串(如 XAML 里的 Tag/Content)取回 Python str。"""
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    try:
        from win32more.Windows.Foundation import IPropertyValue
        return obj.as_(IPropertyValue).GetString()
    except Exception:
        try:
            return str(obj)
        except Exception:
            return ""


def _spin(op, timeout: float = 30.0):
    """在非 UI 线程同步等待 WinRT 异步操作。"""
    info = op.as_(IAsyncInfo)
    t0 = time.monotonic()
    while info.Status == AsyncStatus.Started:
        if time.monotonic() - t0 > timeout:
            raise TimeoutError(_("WinRT 异步操作超时"))
        time.sleep(0.005)
    return op.GetResults()


def file_uri(path: str | Path) -> Uri:
    return Uri(Path(path).resolve().as_uri())


def glyph_for(entry: RemoteEntry) -> str:
    if entry.is_dir:
        return ""  # Folder
    ext = os.path.splitext(entry.name)[1].lower()
    if ext in FITS_EXTS:
        return ""  # Camera
    if ext in IMAGE_EXTS:
        return ""  # Photo2
    if ext in TEXT_EXTS:
        return ""  # Document
    return ""  # Page


# ---------------------------------------------------------------- 批量绘图

XAML_NS = "http://schemas.microsoft.com/winfx/2006/xaml/presentation"

# 画刷 → #AARRGGBB 的缓存。按 id(brush) 做键,但**把画刷本身一起存**:
# 否则对象被回收后 id 可能被复用,拿到的就是别人的颜色。
_HEX_CACHE: dict[int, tuple[object, str]] = {}


def argb_hex(brush, default: str = "#FF808080") -> str:
    """SolidColorBrush → XAML 颜色串 #AARRGGBB(取不到就用兜底串)。"""
    if brush is None:
        return default
    hit = _HEX_CACHE.get(id(brush))
    if hit is not None and hit[0] is brush:
        return hit[1]
    try:
        c = brush.Color
        out = f"#{int(c.A):02X}{int(c.R):02X}{int(c.G):02X}{int(c.B):02X}"
    except Exception:
        return default
    _HEX_CACHE[id(brush)] = (brush, out)
    return out


def rect_fragment(rects, *, radius: float = 0.0) -> str:
    """一批矩形 (x, y, w, h, #AARRGGBB) → 可直接 ``XamlReader.Load`` 的子画布片段。

    win32more 每次 Python→WinRT 调用约 0.2ms,逐个建矩形(Rectangle + 2 尺寸
    + Fill + Canvas.Left/Top + Append ≈ 7 次调用)在 400 个上要 600ms 以上;
    把**同样一批元素**交给 C++ 的 XAML 解析器只要 16ms(实测快 40~80 倍)。
    产物仍是**各自独立的 Rectangle**,重叠处的半透明叠加与 z 序都不变 ——
    这点很关键,换成单个 Path 就会毁掉混色。

    数字用不区分区域设置的定点格式(XAML 属性按 invariant culture 解析,
    某些区域设置下小数点会变成逗号,那样片段直接解析失败)。
    空列表返回空串(调用方据此跳过 Load)。
    """
    if not rects:
        return ""
    corner = (f' RadiusX="{float(radius):.2f}" RadiusY="{float(radius):.2f}"'
              if radius > 0 else "")
    body = "".join(
        f'<Rectangle Width="{w:.2f}" Height="{h:.2f}" Fill="{c}"{corner}'
        f' Canvas.Left="{x:.2f}" Canvas.Top="{y:.2f}"/>'
        for x, y, w, h, c in rects)
    return f'<Canvas xmlns="{XAML_NS}">{body}</Canvas>'


def line_fragment(lines) -> str:
    """一批直线 (x1, y1, x2, y2, #AARRGGBB, 粗细) 的批量片段。"""
    if not lines:
        return ""
    body = "".join(
        f'<Line X1="{x1:.2f}" Y1="{y1:.2f}" X2="{x2:.2f}" Y2="{y2:.2f}"'
        f' Stroke="{c}" StrokeThickness="{t:.2f}"/>'
        for x1, y1, x2, y2, c, t in lines)
    return f'<Canvas xmlns="{XAML_NS}">{body}</Canvas>'


def poly_fragment(pts, *, stroke: str | None = None,
                  fill: str | None = None, thickness: float = 1.5) -> str:
    """折线/多边形的批量片段(``XamlReader.Load`` 一次成型)。

    ``PointCollection.Append`` 是**逐点**的 Python→WinRT 调用(约 0.2ms/点),
    一屏上千个点就是几百毫秒。拼成 ``Points="x,y x,y …"`` 交给解析器只要一次调用。
    点数 <2 返回空串。
    """
    pts = list(pts)
    if len(pts) < 2:
        return ""
    body = " ".join(f"{x:.2f},{y:.2f}" for x, y in pts)
    if fill is not None:
        el = f'<Polygon Fill="{fill}" Points="{body}"/>'
    else:
        el = (f'<Polyline Stroke="{stroke}" StrokeThickness="{thickness:.2f}"'
              f' Points="{body}"/>')
    return f'<Canvas xmlns="{XAML_NS}">{el}</Canvas>'
