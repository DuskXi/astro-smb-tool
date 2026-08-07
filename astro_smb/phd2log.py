"""PHD2 引导日志解析(纯标准库, Log version 2.5, ASIAIR 内置 PHD2)。

日志来源: ``EMMC Images/log/PHD2_GuideLog_YYYY-MM-DD_HHMMSS.txt``。

结构(实测):
    1 行文件头 'PHD2 version, Log version 2.5. Log enabled at ...'
    + 若干 Calibration 段 / Guiding 段(帧数据为 18 列 CSV)
    + 可选 'Log closed at ...' 收尾

容错要点(实测踩到):
    - Begins/Ends 不保证配对: 失败校准后有孤立的 'Guiding Ends'; 文件可能
      无收尾截断(最后一段既无 Ends 也无 Log closed);
    - 'PHD2 version' 后为空(ASIAIR 不写版本号), 'Equipment Profile =' 值为空;
    - mount 名不稳定('OnStep Electronics'/'OnStep'), 只按前缀匹配;
    - pixel scale 同一文件内会变(2.06→2.05), 必须按段取用;
    - ErrorCode!=0 或 SNR==0 的星丢失帧必须从 RMS 统计中剔除。

时间: 段锚点 'Guiding Begins at YYYY-MM-DD HH:MM:SS' 为设备本地时间,
帧绝对时刻 = 锚点 + Time 列(相对秒)。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

_TS = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}"
_RE_GUIDE_BEGIN = re.compile(rf"^Guiding Begins at ({_TS})")
_RE_GUIDE_END = re.compile(rf"^Guiding Ends at ({_TS})")
_RE_CAL_BEGIN = re.compile(rf"^Calibration Begins at ({_TS})")
_RE_CAL_DONE = re.compile(r"^Calibration complete")
_RE_LOG_ENABLED = re.compile(rf"Log enabled at ({_TS})")
_RE_LOG_CLOSED = re.compile(rf"^Log closed at ({_TS})")
_RE_INFO = re.compile(r"^INFO: (.+)$")

_RE_PIXEL_SCALE = re.compile(
    r"Pixel scale = ([\d.]+) arc-sec/px, Binning = (\d+), Focal length = ([\d.]+) mm")
_RE_EXPOSURE = re.compile(r"^Exposure = (\d+) ms")
_RE_CAMERA = re.compile(r"^Camera = ([^,]+)")
_RE_MOUNT_META = re.compile(r"^Mount = ([^,]+)")
_RE_DEC_HA = re.compile(
    r"^Dec = ([\d.eE+-]+) deg, Hour angle = ([\d.eE+-]+) hr, Pier side = ([A-Za-z]+)")
_RE_CAL_RESULT = re.compile(
    r"^(West|North) calibration complete\. Angle = ([\d.eE+-]+) deg, "
    r"Rate = ([\d.eE+-]+) px/sec")

# Guiding 帧行 18 列列头(固定)
FRAME_HEADER = ("Frame,Time,mount,dx,dy,RARawDistance,DECRawDistance,"
                "RAGuideDistance,DECGuideDistance,RADuration,RADirection,"
                "DECDuration,DECDirection,XStep,YStep,StarMass,SNR,ErrorCode")
# Calibration 数据列头
CAL_HEADER = "Direction,Step,dx,dy,x,y,Dist"


def _parse_ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def _f(s: str) -> float:
    try:
        return float(s)
    except ValueError:
        return 0.0


@dataclass
class GuideFrame:
    time_s: float               # 相对本段 begins 的秒
    dx: float
    dy: float
    ra_raw: float               # RARawDistance(px)
    dec_raw: float              # DECRawDistance(px)
    ra_guide: float
    dec_guide: float
    ra_dur: int                 # RA 脉冲 ms
    ra_dir: str                 # 'E'/'W'/''
    dec_dur: int
    dec_dir: str                # 'N'/'S'/''
    star_mass: float
    snr: float
    err: int                    # ErrorCode, 0=正常

    @property
    def lost(self) -> bool:
        return self.err != 0 or self.snr <= 0.0


@dataclass
class SettleEvent:
    time: datetime | None       # INFO 行没有自带时间, 记录所属段内最近帧的绝对时刻
    kind: str                   # started | complete | failed


@dataclass
class GuideSection:
    begins: datetime
    ends: datetime | None = None
    pixel_scale: float | None = None    # arc-sec/px
    binning: int | None = None
    focal_len: float | None = None
    exposure_ms: int | None = None
    camera: str | None = None
    mount: str | None = None
    dec_deg: float | None = None        # 段头 'Dec = ... deg'
    hour_angle_hr: float | None = None  # 段头 'Hour angle = ... hr'
    pier_side: str | None = None
    frames: list[GuideFrame] = field(default_factory=list)
    settles: list[SettleEvent] = field(default_factory=list)

    @property
    def duration_s(self) -> float:
        if self.frames:
            return self.frames[-1].time_s
        if self.ends:
            return (self.ends - self.begins).total_seconds()
        return 0.0

    def frame_abs_time(self, fr: GuideFrame) -> datetime:
        return self.begins + timedelta(seconds=fr.time_s)

    @property
    def end_time_effective(self) -> datetime:
        """ends 缺失(截断)时用末帧时间兜底。"""
        if self.ends:
            return self.ends
        return self.begins + timedelta(seconds=self.duration_s)


@dataclass
class CalStep:
    direction: str
    step: int
    dx: float
    dy: float
    x: float
    y: float
    dist: float


@dataclass
class CalibrationSection:
    begins: datetime
    complete: bool = False
    mount: str | None = None
    pixel_scale: float | None = None
    west_angle: float | None = None
    west_rate: float | None = None
    north_angle: float | None = None
    north_rate: float | None = None
    steps: list[CalStep] = field(default_factory=list)
    star_lost: int = 0          # 校准中 STAR LOST 行数(失败校准会刷屏)


@dataclass
class Phd2Log:
    source: str
    enabled_at: datetime | None = None
    closed_at: datetime | None = None
    guide_sections: list[GuideSection] = field(default_factory=list)
    calibrations: list[CalibrationSection] = field(default_factory=list)


def parse_phd2_log(text: str, source: str = "") -> Phd2Log:
    """解析 PHD2 日志全文。容忍截断/不配对/未知行(静默跳过)。"""
    log = Phd2Log(source=source)
    gs: GuideSection | None = None
    last_gs: GuideSection | None = None     # 刚结束的段:PHD2 会把 settle 结果
    cal: CalibrationSection | None = None   # 写在 'Guiding Ends' 之后,需回填
    in_frames = False           # 已看到 Guiding 帧列头
    in_cal_steps = False

    def meta_line(line: str) -> None:
        """Guiding/Calibration 段头元数据(两者字段有交集)。"""
        target = gs or cal
        if target is None:
            return
        if (m := _RE_PIXEL_SCALE.search(line)):
            target.pixel_scale = _f(m.group(1))
            if gs is not None and target is gs:
                gs.binning = int(m.group(2))
                gs.focal_len = _f(m.group(3))
            return
        if gs is not None and target is gs:
            if (m := _RE_EXPOSURE.match(line)):
                gs.exposure_ms = int(m.group(1))
                return
            if (m := _RE_CAMERA.match(line)):
                gs.camera = m.group(1).strip()
                return
            if (m := _RE_MOUNT_META.match(line)):
                gs.mount = m.group(1).strip()
                return
            if (m := _RE_DEC_HA.match(line)):
                gs.dec_deg = _f(m.group(1))
                gs.hour_angle_hr = _f(m.group(2))
                gs.pier_side = m.group(3)
                return

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            continue

        if (m := _RE_GUIDE_BEGIN.search(line)):
            gs = GuideSection(begins=_parse_ts(m.group(1)))
            log.guide_sections.append(gs)
            cal = None
            last_gs = None
            in_frames = in_cal_steps = False
            continue
        if (m := _RE_GUIDE_END.match(line)):
            # 可能是孤立 Ends(失败校准后), 只在有开段时回填
            if gs is not None:
                if gs.ends is None:
                    gs.ends = _parse_ts(m.group(1))
                last_gs = gs
            gs = None
            in_frames = False
            continue
        if (m := _RE_CAL_BEGIN.search(line)):
            cal = CalibrationSection(begins=_parse_ts(m.group(1)))
            log.calibrations.append(cal)
            gs = None
            last_gs = None
            in_frames = in_cal_steps = False
            continue
        if _RE_CAL_DONE.match(line):
            if cal is not None:
                cal.complete = True
                i = line.find("mount = ")
                if i >= 0:
                    cal.mount = line[i + 8:].rstrip(".")
            in_cal_steps = False
            continue
        if (m := _RE_LOG_CLOSED.match(line)):
            log.closed_at = _parse_ts(m.group(1))
            gs = None
            last_gs = None
            continue
        if log.enabled_at is None and (m := _RE_LOG_ENABLED.search(line)):
            log.enabled_at = _parse_ts(m.group(1))
            continue

        if (m := _RE_INFO.match(line)):
            info = m.group(1)
            if "SETTLING STATE CHANGE" in info:
                kind = ("complete" if "complete" in info
                        else "failed" if "failed" in info else "started")
                # 真机实证:settle 结果(尤其 failed)常写在 'Guiding Ends' 之后,
                # 回填到刚结束的段(last_gs);新段/校准/收尾之后不再回填
                target = gs if gs is not None else last_gs
                if target is not None:
                    if gs is not None:
                        t = (gs.frame_abs_time(gs.frames[-1])
                             if gs.frames else gs.begins)
                    else:
                        t = target.end_time_effective
                    target.settles.append(SettleEvent(time=t, kind=kind))
            elif "STAR LOST" in info and cal is not None:
                cal.star_lost += 1
            continue

        if line.startswith(FRAME_HEADER[:20]):     # 'Frame,Time,mount,...'
            in_frames = True
            continue
        if line.startswith(CAL_HEADER):
            in_cal_steps = True
            continue

        if (m := _RE_CAL_RESULT.match(line)):
            if cal is not None:
                angle, rate = _f(m.group(2)), _f(m.group(3))
                if m.group(1) == "West":
                    cal.west_angle, cal.west_rate = angle, rate
                else:
                    cal.north_angle, cal.north_rate = angle, rate
            continue

        # ---- 数据行 ----
        if in_frames and gs is not None and line[:1].isdigit():
            parts = line.split(",")
            if len(parts) == 18:
                try:
                    gs.frames.append(GuideFrame(
                        time_s=_f(parts[1]),
                        dx=_f(parts[3]), dy=_f(parts[4]),
                        ra_raw=_f(parts[5]), dec_raw=_f(parts[6]),
                        ra_guide=_f(parts[7]), dec_guide=_f(parts[8]),
                        ra_dur=int(parts[9] or 0), ra_dir=parts[10],
                        dec_dur=int(parts[11] or 0), dec_dir=parts[12],
                        star_mass=_f(parts[15]), snr=_f(parts[16]),
                        err=int(parts[17] or 0),
                    ))
                except (ValueError, IndexError):
                    pass
            continue
        if in_cal_steps and cal is not None:
            parts = line.split(",")
            if len(parts) == 7 and parts[1].isdigit():
                cal.steps.append(CalStep(
                    direction=parts[0], step=int(parts[1]),
                    dx=_f(parts[2]), dy=_f(parts[3]),
                    x=_f(parts[4]), y=_f(parts[5]), dist=_f(parts[6])))
            continue

        meta_line(line)

    return log


# ---------------------------------------------------------------- RMS 统计

DEFAULT_PIXEL_SCALE = 1.0   # 段头缺 pixel scale 时按 px 输出(scale=1)


@dataclass
class RmsStats:
    rms_ra: float               # 角秒(若无 pixel scale 则为像素)
    rms_dec: float
    rms_total: float
    peak_ra: float
    peak_dec: float
    n_frames: int               # 参与统计的有效帧
    n_lost: int                 # 被剔除的星丢失帧
    duration_s: float
    pixel_scale: float | None   # 实际使用的比例(None=无, 按 px)

    @property
    def in_arcsec(self) -> bool:
        return self.pixel_scale is not None


def compute_rms(pairs: list[tuple[GuideFrame, float]]) -> RmsStats | None:
    """pairs = [(帧, 该帧所属段的 pixel_scale 或 None)], 计算 RMS。

    混合情形(部分帧有 pixel_scale、部分没有)时**剔除无 scale 的帧**:
    像素值与角秒值不可混合平方平均,否则输出错误数值却仍标注角秒。
    """
    if not pairs:
        return None
    n_lost = sum(1 for f, _ in pairs if f.lost)
    valid = [(f, s) for f, s in pairs if not f.lost]
    if not valid:
        return RmsStats(0, 0, 0, 0, 0, 0, n_lost, 0.0, None)
    any_scale = any(s is not None for _, s in valid)
    if any_scale:
        valid = [(f, s) for f, s in valid if s is not None]
    sum_ra2 = sum_dec2 = 0.0
    peak_ra = peak_dec = 0.0
    for f, s in valid:
        k = s if s is not None else DEFAULT_PIXEL_SCALE
        ra = f.ra_raw * k
        dec = f.dec_raw * k
        sum_ra2 += ra * ra
        sum_dec2 += dec * dec
        peak_ra = max(peak_ra, abs(ra))
        peak_dec = max(peak_dec, abs(dec))
    n = len(valid)
    rms_ra = math.sqrt(sum_ra2 / n)
    rms_dec = math.sqrt(sum_dec2 / n)
    t0 = min(f.time_s for f, _ in valid)
    t1 = max(f.time_s for f, _ in valid)
    return RmsStats(
        rms_ra=rms_ra, rms_dec=rms_dec,
        rms_total=math.sqrt(rms_ra ** 2 + rms_dec ** 2),
        peak_ra=peak_ra, peak_dec=peak_dec,
        n_frames=n, n_lost=n_lost, duration_s=max(0.0, t1 - t0),
        pixel_scale=(sum(s for _, s in valid if s is not None)
                     / sum(1 for _, s in valid if s is not None))
                    if any_scale else None,
    )


def section_rms(section: GuideSection) -> RmsStats | None:
    return compute_rms([(f, section.pixel_scale) for f in section.frames])


def frames_for_interval(logs: list[Phd2Log], t0: datetime, t1: datetime,
                        ) -> list[tuple[GuideFrame, float | None]]:
    """绝对时间区间 [t0, t1] 内的全部导星帧(跨段/跨文件),
    返回 [(帧, 所属段 pixel_scale)] —— 供多区间并集后统一 compute_rms。"""
    pairs: list[tuple[GuideFrame, float | None]] = []
    for log in logs:
        for sec in log.guide_sections:
            if sec.end_time_effective < t0 or sec.begins > t1:
                continue
            lo = (t0 - sec.begins).total_seconds()
            hi = (t1 - sec.begins).total_seconds()
            for f in sec.frames:
                if lo <= f.time_s <= hi:
                    pairs.append((f, sec.pixel_scale))
    return pairs


def rms_for_interval(logs: list[Phd2Log],
                     t0: datetime, t1: datetime) -> RmsStats | None:
    """绝对时间区间 [t0, t1] 与所有导星段求交后的 RMS(跨段/跨文件)。

    用于回答"这张(些)曝光期间导星表现如何"。区间无覆盖返回 None。
    """
    return compute_rms(frames_for_interval(logs, t0, t1))


def guide_coverage(logs: list[Phd2Log],
                   t0: datetime, t1: datetime) -> float:
    """区间 [t0,t1] 被导星段覆盖的比例(0~1), 粗略以段起止求交。"""
    total = (t1 - t0).total_seconds()
    if total <= 0:
        return 0.0
    covered = 0.0
    for log in logs:
        for sec in log.guide_sections:
            s = max(t0, sec.begins)
            e = min(t1, sec.end_time_effective)
            if e > s:
                covered += (e - s).total_seconds()
    return min(1.0, covered / total)
