"""ASIAIR 影像文件名解析(真机实测归纳的语法)。

    <类型>[_<目标>]_<曝光ms|s>_Bin<n>[_<滤镜>]_<YYYYMMDD-HHMMSS>[_<角度>deg][_<序号4位>][_thn].fit|.jpg

实测要点:
    - 目标名可含空格('M 8'/'IC 4603'), 仅 Light/部分 Preview 有; 校准帧无目标字段;
    - 滤镜是滤镜轮槽位名(如 '4C'/'Dul'/'1'), **不是温度**, 可整体缺失;
    - 时间戳 = 设备本地时间的曝光结束/保存时刻(= 日志曝光开始 + 曝光时长 + ~1s);
    - '<N>deg' 是最近一次 plate solve 的图像旋转角, 可缺失;
    - 序号 4 位, 每次计划运行从 0001 重置(同目标目录跨夜累积), 可缺失(Preview);
    - 每个 .fit 旁有同名 '_thn.jpg' 缩略图。

解析以 ``\\d{8}-\\d{6}`` 时间戳为锚点回推, 目标/滤镜/角度/序号全部可缺失。
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from .autorunlog import parse_exposure_seconds

_RE_IMAGE = re.compile(
    r"^(?P<kind>Light|Bias|Dark|Flat|Preview)"
    r"(?:_(?P<target>.+?))?"
    r"_(?P<exposure>[\d.]+(?:ms|s))"
    r"_Bin(?P<bin>\d+)"
    r"(?:_(?P<filter>[^_]+?))?"
    r"_(?P<ts>\d{8}-\d{6})"
    r"(?:_(?P<angle>-?\d+)deg)?"
    r"(?:_(?P<seq>\d{4}))?"
    r"(?P<thn>_thn)?"
    r"\.(?P<ext>fit|fits|jpg|jpeg|png|tif|tiff)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ImageName:
    kind: str                   # Light/Bias/Dark/Flat/Preview(首字母大写规范形)
    target: str | None
    exposure: str               # 原始 '180.0s' / '1.0ms'
    binning: int
    filter: str | None
    time: datetime | None       # 文件名时间戳(本地时间, 曝光结束/保存时刻)
    angle_deg: int | None
    seq: int | None
    thumb: bool
    ext: str

    @property
    def exposure_s(self) -> float | None:
        return parse_exposure_seconds(self.exposure)


def parse_image_name(name: str) -> ImageName | None:
    """解析 ASIAIR 影像文件名; 不匹配返回 None。"""
    m = _RE_IMAGE.match(name)
    if not m:
        return None
    ts: datetime | None = None
    try:
        ts = datetime.strptime(m.group("ts"), "%Y%m%d-%H%M%S")
    except ValueError:
        pass
    angle = m.group("angle")
    seq = m.group("seq")
    return ImageName(
        kind=m.group("kind").capitalize(),
        target=m.group("target"),
        exposure=m.group("exposure"),
        binning=int(m.group("bin")),
        filter=m.group("filter"),
        time=ts,
        angle_deg=int(angle) if angle is not None else None,
        seq=int(seq) if seq is not None else None,
        thumb=m.group("thn") is not None,
        ext=m.group("ext").lower(),
    )
