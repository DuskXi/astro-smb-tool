"""alt-az 天球图的**唯一一份**投影与画法。

约定:仰视,北上、**东左**,r = R·(90-alt)/90 —— 与老 UI 的 `skyview.radar_xy`
以及拍摄记录页的大图完全一致。

docs/DEVELOPMENT.md 里写着"改投影必须三处同步";B11 要给浏览页详情再加一个迷你雷达时,
那就成了第四处。同步三处已经是在赌没人漏改,四处只是把赌注加大 —— 所以这里
把公式收成一份,各页只管给点。
"""
from __future__ import annotations

import math
from astro_smb.i18n import gettext as _

AXIS = "#59808080"          # 地平圈/高度圈
LABEL = "#99AAAAAA"         # 方位标注
DOT_UP = "#FF4CAF50"        # 地平线上:绿
DOT_DOWN = "#FFE6A000"      # 地平线下:琥珀


def radar_xy(alt_deg: float, az_deg: float,
             cx: float, cy: float, radius: float) -> tuple[float, float]:
    """alt/az → 平面坐标。r = R·(90-alt)/90;北上、**东左**(仰视惯例)。

    alt 下限夹到 -5° 而不是 0°:地平线下一点点的目标仍要画出来(那通常意味着
    站点纬度没设对,把点藏掉等于把线索藏掉),但不能让它跑到画布外面去。
    """
    r = radius * (90.0 - max(-5.0, min(90.0, alt_deg))) / 90.0
    az = math.radians(az_deg)
    return cx - r * math.sin(az), cy - r * math.cos(az)


def frame_ops(size: float, *, margin: float = 10.0,
              labels: bool = True) -> list[dict]:
    """地平圈 + 两道高度圈 + **十字方位线** + 四个方位标注。

    十字线一开始漏了。老 UI 两处都画(`skyview.MiniRadar._frame` 与
    `_records._sky_frame`),而且它不是装饰:没有那两条线,"这个点偏东还是
    偏西、过没过子午线"要靠眼睛在圆里估,而过子午线正是判断翻转的依据。
    """
    cx = cy = size / 2
    r = cx - margin
    ops: list[dict] = [
        {"op": "ellipse", "x": cx, "y": cy, "rx": r, "ry": r,
         "stroke": AXIS, "width": 1.0},
    ]
    for frac in (1 / 3, 2 / 3):     # alt=60 / alt=30
        ops.append({"op": "ellipse", "x": cx, "y": cy,
                    "rx": r * frac, "ry": r * frac,
                    "stroke": AXIS, "width": 1.0, "opacity": 0.6})
    for x1, y1, x2, y2 in ((cx - r, cy, cx + r, cy),
                           (cx, cy - r, cx, cy + r)):
        ops.append({"op": "line", "x1": x1, "y1": y1, "x2": x2, "y2": y2,
                    "stroke": AXIS, "width": 1.0, "opacity": 0.5,
                    "dash": [3.0, 3.0]})
    if labels:
        for text, dx, dy in ((_("北"), 0.0, -r - 9), (_("南"), 0.0, r + 1),
                             (_("东"), -r - 12, -5), (_("西"), r + 2, -5)):
            ops.append({"op": "text", "x": cx + dx - 5, "y": cy + dy,
                        "text": text, "size": 9.0, "fill": LABEL})
    return ops


def point_ops(points, size: float, *, margin: float = 10.0,
              radius: float = 4.0, skip_below: bool = True,
              default_fill: str | None = None,
              ring: str | None = None,
              label_fill: str = "#FFDDDDDD") -> list[dict]:
    """一批 {alt, az, label?, fill?} → 画点(+标签)。

    `skip_below` 控制地平线下的点画不画:整夜总览图里它们是噪音(目标还没升起),
    而单个目标的迷你雷达里恰恰要看见 —— 那是"这张片子拍的时候目标在地平线下"
    这一异常的唯一提示。
    """
    cx = cy = size / 2
    r = cx - margin
    ops: list[dict] = []
    for pt in points or ():
        alt, az = float(pt["alt"]), float(pt["az"])
        if skip_below and alt < 0:
            continue
        x, y = radar_xy(alt, az, cx, cy, r)
        ops.append({"op": "ellipse", "x": x, "y": y,
                    "rx": radius, "ry": radius,
                    "fill": (pt.get("fill") or default_fill
                             or (DOT_UP if alt > 0 else DOT_DOWN))})
        if ring:
            # 高亮描边 —— 老 UI 的迷你雷达用它把"就是这一张片子的位置"
            # 从背景圈线里拎出来。一个 5px 的点在灰圈上很容易看丢。
            ops.append({"op": "ellipse", "x": x, "y": y,
                        "rx": radius + 3.0, "ry": radius + 3.0,
                        "stroke": ring, "width": 1.5})
        if pt.get("label"):
            ops.append({"op": "text", "x": x + radius + 2.0, "y": y - radius - 2.0,
                        "text": str(pt["label"]), "size": 9.0, "fill": label_fill})
    return ops
