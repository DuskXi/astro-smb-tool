"""ASIAIR Autorun 日志解析(纯标准库)。

日志来源: 设备共享 ``EMMC Images/log/Autorun_Log_YYYY-MM-DD_HHMMSS.txt``
(同名 ``_CHN.txt`` 为逐行对应的中文版, 解析时忽略)。

结构模型(真机实测归纳):
    文件 = 会话*        会话以 'Log enabled/disabled at' 为界
    会话 = 可选 Plan N Start/Pause/Finish + Autorun 目标块* + 收尾杂项
    目标块 = [Autorun|Begin] <目标> Start ... [Autorun|End] Finish|Pause
             内含 Shooting 组 + 实拍帧 + AutoCenter/AutoFocus/Guide 事件

关键陷阱(已在样例上验证):
    - 无 'image N#' 的裸 'Exposure 2.0s' 是 AutoCenter 解析用曝光, 不是实拍帧;
    - image 编号在同一目标块内跨 Shooting 组连续(bias 1-30 后 dark 从 31 起);
    - Pause/恢复会把同一 Plan 分裂成多个会话甚至多个物理文件,
      归并靠 (plan_no, 目标名, 时间邻接), 见 aggregate_nights();
    - 存在无 Shooting 行的块(逐帧变曝光), 落入 frame_type=None 的隐式组;
    - 事件可乱序(手动停止瞬间), 必须逐行状态机, 不能假设严格嵌套;
    - 日志是会话结束时一次性写盘 —— 运行中的会话在设备上看不到日志。

时间均为设备本地时间(naive datetime), 与影像文件名时间戳同一时区。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

#: `integration_by_filter()` 里"这一帧没记滤镜"的键。空串不可能与真实滤镜名
#: (`4C`/`Dul`/`1` 这类槽位号)冲突,而且**与语言无关** —— 界面要把这一档
#: 单独剔掉或另行措辞,拿中文当键的话一翻译就筛不掉了。
FILTER_UNKNOWN = ""

TS = r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}"
_RE_LOG_ENABLED = re.compile(rf"^Log enabled at ({TS})")
_RE_LOG_DISABLED = re.compile(rf"^Log disabled at ({TS})")
_RE_TS_LINE = re.compile(rf"^({TS}) (.*)$")

_RE_PLAN_START = re.compile(r"^Plan (\d+) Start$")
_RE_PLAN_FINISH = re.compile(r"^Plan (\d+) Finish$")
_RE_PLAN_PAUSE = re.compile(r"^Pause Plan (\d+)$")

_RE_AUTORUN_BEGIN = re.compile(r"^\[Autorun\|Begin\] (.+) Start$")
_RE_AUTORUN_END = re.compile(r"^\[Autorun\|End\] (.+)$")
_RE_TARGET_COORD = re.compile(r"^Target RA:(\S+) DEC:(\S+)")
_RE_SLEW = re.compile(r"^Mount slews to target position: RA:(\S+) DEC:(\S+)")

# 'Shooting 30 bias frames, exposure 1.0ms Bin1' / 'Shooting 20 flat frames, auto-exposure Bin1'
_RE_SHOOTING = re.compile(
    r"^Shooting (\d+) (light|bias|dark|flat) frames, "
    r"(?:auto-exposure|exposure (\S+)) Bin(\d+)",
    re.IGNORECASE,
)
_RE_EXPOSURE_IMAGE = re.compile(r"^Exposure (\S+) image (\d+)#$")

_RE_AC_BEGIN = re.compile(r"^\[AutoCenter\|Begin\] Auto-Center (\d+)#")
_RE_AC_END = re.compile(r"^\[AutoCenter\|End\] (.+)$")
_RE_SOLVE_OK = re.compile(
    r"^Solve succeeded: RA:(\S+) DEC:(\S+) Angle = ([\d.eE+-]+), Star number = (\d+)")
_RE_AF_BEGIN = re.compile(r"^\[AutoFocus\|Begin\] (.+)$")
_RE_AF_END = re.compile(r"^\[AutoFocus\|End\] (.+)$")
_RE_AF_POS = re.compile(r"^Auto focus succeeded, the focused position is (\d+)")
_RE_AF_TEMP = re.compile(r"temperature ([\d.-]+)℃")
_RE_GUIDE = re.compile(r"^\[Guide\] (.+)$")

_RE_STOP_MANUAL = re.compile(r"^Stop Autorun Manually$")
_RE_SHUTDOWN = re.compile(r"^Shutdown ASIAIR$")
_RE_FILTER = re.compile(r"^Filter change, (\S+) change to (\S+)$")

# 会话级(块外)保留的杂项事件前缀
_MISC_KEEP = (
    "Mount GoTo Home POS",
    "Stop Tracking",
    "Start Tracking",
    "EAF back to zero position",
    "Turn Off Cooling",
    "Skip going to Home POS",
)


def _parse_ts(s: str) -> datetime:
    return datetime.strptime(s, "%Y/%m/%d %H:%M:%S")


def parse_exposure_seconds(s: str | None) -> float | None:
    """'180.0s' / '1.0ms' → 秒; 'auto'/None → None。"""
    if not s or s == "auto":
        return None
    try:
        if s.endswith("ms"):
            return float(s[:-2]) / 1000.0
        if s.endswith("s"):
            return float(s[:-1])
        return float(s)
    except ValueError:
        return None


@dataclass
class FrameShot:
    """一条实拍帧记录('Exposure Xs image N#', 时间为曝光开始时刻)。"""
    time: datetime
    image_no: int
    exposure: str            # 原始字符串 '180.0s' / '1.0ms'
    filter: str | None = None  # 拍摄时滤镜(由 'Filter change' 事件推得;未知为 None)

    @property
    def exposure_s(self) -> float:
        return parse_exposure_seconds(self.exposure) or 0.0

    @property
    def end_time(self) -> datetime:
        return self.time + timedelta(seconds=self.exposure_s)


@dataclass
class ShootingGroup:
    frame_type: str | None          # light|bias|dark|flat; None=隐式组
    planned: int | None
    exposure: str | None            # '180.0s' / '1.0ms' / 'auto' / None
    binning: str | None
    start_time: datetime | None = None
    frames: list[FrameShot] = field(default_factory=list)

    @property
    def actual(self) -> int:
        return len(self.frames)


@dataclass
class AutoCenterAttempt:
    attempt_no: int
    begin_time: datetime
    end_time: datetime | None = None
    result: str | None = None       # 'The target is centered' / 'Mount slews failed' / ...
    solve_ra: str | None = None
    solve_dec: str | None = None
    solve_angle: float | None = None
    solve_stars: int | None = None

    @property
    def ok(self) -> bool:
        return self.result is not None and "centered" in self.result


@dataclass
class AutoFocusRecord:
    begin_time: datetime
    info: str                       # Begin 行全文(含曝光/温度)
    end_time: datetime | None = None
    success: bool | None = None
    focused_position: int | None = None
    manual_cancel: bool = False
    temperature: float | None = None


@dataclass
class LogEvent:
    time: datetime
    event: str


@dataclass
class AutorunBlock:
    target: str
    begin_time: datetime
    end_time: datetime | None = None
    end_mode: str | None = None     # Finish | Pause | None(会话截断)
    manual_stop: bool = False
    ra: str | None = None           # '17h22m35s' 形式原文
    dec: str | None = None
    groups: list[ShootingGroup] = field(default_factory=list)
    autocenter: list[AutoCenterAttempt] = field(default_factory=list)
    autofocus: list[AutoFocusRecord] = field(default_factory=list)
    guide_events: list[LogEvent] = field(default_factory=list)

    @property
    def total_frames(self) -> int:
        return sum(g.actual for g in self.groups)

    @property
    def autocenter_final(self) -> str | None:
        return self.autocenter[-1].result if self.autocenter else None

    def all_frames(self) -> list[FrameShot]:
        return [f for g in self.groups for f in g.frames]


@dataclass
class Session:
    enabled_at: datetime | None = None
    disabled_at: datetime | None = None
    plan_no: int | None = None
    plan_end: str | None = None     # Finish | Pause | None
    blocks: list[AutorunBlock] = field(default_factory=list)
    shutdown: bool = False
    misc_events: list[LogEvent] = field(default_factory=list)
    unparsed_lines: list[str] = field(default_factory=list)


@dataclass
class AutorunLog:
    source: str                     # 文件名(不含路径), 仅作标识
    sessions: list[Session] = field(default_factory=list)


def parse_autorun_log(text: str, source: str = "") -> AutorunLog:
    """解析一个 Autorun 日志文本(整文件内容)。永不抛异常, 未识别行进 unparsed_lines。"""
    sessions: list[Session] = []
    cur: Session | None = None
    block: AutorunBlock | None = None
    group: ShootingGroup | None = None
    ac: AutoCenterAttempt | None = None
    af: AutoFocusRecord | None = None
    cur_filter: str | None = None   # 滤镜轮当前槽位(物理状态跨会话延续,
                                    # 但保守起见只在文件内跟踪;首次 change 前未知)

    def close_block(end_time: datetime | None, end_mode: str | None) -> None:
        nonlocal block, group, ac, af
        if block is None:
            return
        block.end_time = end_time
        block.end_mode = end_mode
        assert cur is not None
        cur.blocks.append(block)
        block = group = ac = af = None

    def close_session(disabled_at: datetime | None) -> None:
        nonlocal cur
        if cur is None:
            return
        close_block(None, None)     # 截断块容错
        cur.disabled_at = disabled_at
        sessions.append(cur)
        cur = None

    for raw in text.splitlines():
        line = raw.strip("﻿").rstrip()
        if not line.strip():
            continue

        m = _RE_LOG_ENABLED.match(line)
        if m:
            close_session(None)     # 上一段缺 disabled 行容错
            cur = Session(enabled_at=_parse_ts(m.group(1)))
            continue
        m = _RE_LOG_DISABLED.match(line)
        if m:
            close_session(_parse_ts(m.group(1)))
            continue

        m = _RE_TS_LINE.match(line)
        if not m:
            if cur is not None:
                cur.unparsed_lines.append(line)
            continue
        ts, msg = _parse_ts(m.group(1)), m.group(2).strip()
        if cur is None:             # 无 'Log enabled' 前导容错
            cur = Session()

        # ---- 会话级 ----
        if (pm := _RE_PLAN_START.match(msg)):
            cur.plan_no = int(pm.group(1))
            continue
        if (pm := _RE_PLAN_FINISH.match(msg)):
            cur.plan_no = cur.plan_no or int(pm.group(1))
            cur.plan_end = "Finish"
            continue
        if (pm := _RE_PLAN_PAUSE.match(msg)):
            cur.plan_no = cur.plan_no or int(pm.group(1))
            cur.plan_end = "Pause"
            continue
        if _RE_SHUTDOWN.match(msg):
            cur.shutdown = True
            cur.misc_events.append(LogEvent(ts, msg))
            continue

        # ---- 块边界 ----
        if (bm := _RE_AUTORUN_BEGIN.match(msg)):
            close_block(None, None)     # 前块未闭合容错
            block = AutorunBlock(target=bm.group(1), begin_time=ts)
            group = None
            continue
        if (em := _RE_AUTORUN_END.match(msg)):
            reason = em.group(1)
            mode = "Finish" if "Finish" in reason else (
                "Pause" if "Pause" in reason else reason)
            close_block(ts, mode)
            continue

        # ---- 块内 ----
        if block is not None:
            if _RE_STOP_MANUAL.match(msg):
                block.manual_stop = True
                continue
            if (cm := _RE_TARGET_COORD.match(msg)):
                block.ra, block.dec = cm.group(1), cm.group(2)
                continue
            if (sm := _RE_SLEW.match(msg)):
                if block.ra is None:
                    block.ra, block.dec = sm.group(1), sm.group(2)
                continue
            if (gm := _RE_SHOOTING.match(msg)):
                group = ShootingGroup(
                    frame_type=gm.group(2).lower(),
                    planned=int(gm.group(1)),
                    exposure=gm.group(3) or "auto",
                    binning="Bin" + gm.group(4),
                    start_time=ts,
                )
                block.groups.append(group)
                continue
            if (fm := _RE_FILTER.match(msg)):
                cur_filter = fm.group(2)
                continue
            if (im := _RE_EXPOSURE_IMAGE.match(msg)):
                if group is None:       # 无 Shooting 行 → 隐式组
                    group = ShootingGroup(
                        frame_type=None, planned=None,
                        exposure=None, binning=None, start_time=ts)
                    block.groups.append(group)
                group.frames.append(
                    FrameShot(time=ts, image_no=int(im.group(2)),
                              exposure=im.group(1), filter=cur_filter))
                continue
            if (am := _RE_AC_BEGIN.match(msg)):
                ac = AutoCenterAttempt(attempt_no=int(am.group(1)), begin_time=ts)
                block.autocenter.append(ac)
                continue
            if (sv := _RE_SOLVE_OK.match(msg)):
                if ac is not None:
                    ac.solve_ra, ac.solve_dec = sv.group(1), sv.group(2)
                    try:
                        ac.solve_angle = float(sv.group(3))
                    except ValueError:
                        pass
                    ac.solve_stars = int(sv.group(4))
                continue
            if (am := _RE_AC_END.match(msg)):
                if ac is not None:
                    ac.end_time, ac.result = ts, am.group(1)
                    ac = None
                continue
            if (fm := _RE_AF_BEGIN.match(msg)):
                af = AutoFocusRecord(begin_time=ts, info=fm.group(1))
                if (tm := _RE_AF_TEMP.search(fm.group(1))):
                    try:
                        af.temperature = float(tm.group(1))
                    except ValueError:
                        pass
                block.autofocus.append(af)
                continue
            if af is not None and (fm := _RE_AF_POS.match(msg)):
                af.focused_position = int(fm.group(1))
                continue
            if af is not None and msg == "Cancel AF Manually":
                af.manual_cancel = True
                continue
            if (fm := _RE_AF_END.match(msg)):
                if af is not None:
                    af.end_time = ts
                    af.success = "succeeded" in fm.group(1)
                    af = None
                continue
            if (gm := _RE_GUIDE.match(msg)):
                block.guide_events.append(LogEvent(ts, gm.group(1)))
                continue
            # 其余块内细节行(Filter change / Plate Solve / Find Focus Star /
            # Calculate V-Curve ...)v1 不建模, 忽略
            continue

        # ---- 块外杂项 ----
        if any(msg.startswith(k) for k in _MISC_KEEP):
            cur.misc_events.append(LogEvent(ts, msg))
            continue
        if (gm := _RE_GUIDE.match(msg)):
            cur.misc_events.append(LogEvent(ts, "[Guide] " + gm.group(1)))
            continue
        cur.unparsed_lines.append(line)

    close_session(None)             # 文件截断容错
    return AutorunLog(source=source, sessions=sessions)


# ---------------------------------------------------------------- 夜次归并

@dataclass
class TargetRun:
    """同一夜内同一 (plan_no, 目标) 的所有块合并(跨会话/跨文件)。"""
    target: str
    plan_no: int | None             # None = 裸 Autorun(非多目标计划)
    blocks: list[AutorunBlock] = field(default_factory=list)

    @property
    def begin_time(self) -> datetime:
        return self.blocks[0].begin_time

    @property
    def end_time(self) -> datetime | None:
        return self.blocks[-1].end_time or self.blocks[-1].begin_time

    @property
    def ra(self) -> str | None:
        return next((b.ra for b in self.blocks if b.ra), None)

    @property
    def dec(self) -> str | None:
        return next((b.dec for b in self.blocks if b.dec), None)

    @property
    def total_frames(self) -> int:
        return sum(b.total_frames for b in self.blocks)

    @property
    def finished(self) -> bool:
        return any(b.end_mode == "Finish" for b in self.blocks)

    @property
    def attempts(self) -> int:
        return len(self.blocks)

    def all_frames(self) -> list[FrameShot]:
        return [f for b in self.blocks for f in b.all_frames()]

    def type_stats(self) -> dict[str, tuple[int, int]]:
        """帧型 → (计划数, 实拍数合计); 隐式组归入 'unknown'。

        计划数**不跨块累加**:Pause/恢复产生的每个块会重新宣告同一份
        'Shooting N ...',对各块内合计取 max 才是用户的实际计划
        (同一块内同型多组是真实的不同拍摄组,块内仍求和)。
        """
        planned: dict[str, int] = {}
        actual: dict[str, int] = {}
        for b in self.blocks:
            per_block: dict[str, int] = {}
            for g in b.groups:
                key = g.frame_type or "unknown"
                actual[key] = actual.get(key, 0) + g.actual
                if g.planned:
                    per_block[key] = per_block.get(key, 0) + g.planned
            for key, v in per_block.items():
                planned[key] = max(planned.get(key, 0), v)
        # **不能用 `planned.keys() | actual.keys()`。** 那是集合并,而 Python
        # 的字符串 `hash()` 每个进程都不一样(哈希随机化)—— 于是返回的 dict
        # 键序每次启动都在变,拍摄记录页那句"已完成 · dark 5/5 · bias 30/30"
        # 会一会儿这个顺序一会儿那个。症状很轻但很扰人,而且不报错。
        # (同一个病这个仓库犯过第二次了,上一次是 treemap 的
        # `hash(ext_category(node))`,见 `views/space.py` 的模块说明。)
        # 按**日志里第一次出现的顺序**排,那是有含义的顺序。
        keys = list(actual) + [k for k in planned if k not in actual]
        return {k: (planned.get(k, 0), actual.get(k, 0)) for k in keys}

    def frame_span(self) -> tuple[datetime, datetime] | None:
        """首帧开始 ~ 末帧结束(用于导星区间求交); 无帧返回 None。"""
        frames = self.all_frames()
        if not frames:
            return None
        return frames[0].time, frames[-1].end_time

    def integration_by_filter(self) -> dict[str, float]:
        """滤镜 → 亮场积分秒数(只计 light 与隐式组)。

        **滤镜缺失归到 `FILTER_UNKNOWN`(空串),不是中文「未知」。**
        这个 dict 的键会一路传到界面上当筛选条件用(拍摄记录页要把"未知"
        那一档从徽章里剔掉),拿中文当键的话一做 i18n 就筛不掉了 ——
        不报错,只是徽章上多出一个空滤镜。
        """
        out: dict[str, float] = {}
        for b in self.blocks:
            for g in b.groups:
                if g.frame_type not in ("light", None):
                    continue
                for f in g.frames:
                    key = f.filter or FILTER_UNKNOWN
                    out[key] = out.get(key, 0.0) + f.exposure_s
        return out


@dataclass
class Night:
    """一个观测夜(以正午为界): date = 当夜起始日。"""
    date: str                       # 'YYYY-MM-DD'
    sessions: list[Session] = field(default_factory=list)
    runs: list[TargetRun] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)

    @property
    def begin_time(self) -> datetime | None:
        for s in self.sessions:
            if s.enabled_at:
                return s.enabled_at
        return None

    @property
    def end_time(self) -> datetime | None:
        for s in reversed(self.sessions):
            if s.disabled_at:
                return s.disabled_at
        return None

    @property
    def shutdown(self) -> bool:
        return any(s.shutdown for s in self.sessions)

    @property
    def total_frames(self) -> int:
        return sum(r.total_frames for r in self.runs)


def night_key(dt: datetime) -> str:
    """观测夜归属: 正午前算前一夜(12:00 分界)。"""
    return (dt - timedelta(hours=12)).strftime("%Y-%m-%d")


def aggregate_nights(logs: list[AutorunLog]) -> list[Night]:
    """把多个日志文件的会话归并成夜次视图(按时间升序)。

    同一夜内 (plan_no, 目标名) 相同的块合并为一个 TargetRun ——
    Pause/恢复导致的分裂在此处收拢。
    """
    all_sessions: list[tuple[Session, str]] = []
    for lg in logs:
        for s in lg.sessions:
            if s.enabled_at is not None or s.blocks:
                all_sessions.append((s, lg.source))
    # 排序回退与夜次锚点保持一致:缺 'Log enabled' 头的会话按其首块时间排序,
    # 否则会被排到最前,归并后 run.blocks 时间倒挂、frame_span 区间反向
    all_sessions.sort(
        key=lambda p: p[0].enabled_at
        or (p[0].blocks[0].begin_time if p[0].blocks else datetime.min))

    nights: dict[str, Night] = {}
    for s, src in all_sessions:
        anchor = s.enabled_at or (s.blocks[0].begin_time if s.blocks else None)
        if anchor is None:
            continue
        key = night_key(anchor)
        night = nights.setdefault(key, Night(date=key))
        night.sessions.append(s)
        if src and src not in night.sources:
            night.sources.append(src)
        for b in s.blocks:
            run = next(
                (r for r in night.runs
                 if r.target == b.target and r.plan_no == s.plan_no),
                None)
            if run is None:
                run = TargetRun(target=b.target, plan_no=s.plan_no)
                night.runs.append(run)
            run.blocks.append(b)

    out = sorted(nights.values(), key=lambda n: n.date)
    for n in out:
        for r in n.runs:
            r.blocks.sort(key=lambda b: b.begin_time)   # 双保险:块内时间有序
        n.runs.sort(key=lambda r: r.begin_time)
    return out
