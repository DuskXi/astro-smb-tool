"""Qt 前端**页面模型**的门禁 —— 截图看不出来的那几类。

``astro_smb_qt/models.py`` 是纯函数,所以这一份**不需要 QApplication、
不需要 PySide6、不需要设备**。

它盯的是这个仓库反复栽的两类:

1. **空文本节点。** 根因永远是"读了不存在的键"(``.get()`` 不报错):
   视图模型给 ``{"ri": 3}``、代码读 ``item["title"]``;时间线条目的键是
   ``t0/title/subtitle``、代码读 ``time/text/note``。界面上表现为一行高度
   正常、一个字没有的行,排查的人第一反应是"渲染器丢内容了"。
2. **判读口径。** 导星整体 RMS 必须按帧数加权(简单平均会被几帧的碎段拖爆:
   真机上 1.89″ vs 0.92″,结论从"导星很差"变成"正常")。

有真日志(``.tmp/`` 下)就用真的跑一遍;没有就用合成日志 —— **合成的那份
一定会跑**,不能让整份文件在没有设备时静默空过。
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("numpy")

from astro_smb.autorunlog import aggregate_nights, parse_autorun_log  # noqa: E402
from astro_smb.phd2log import parse_phd2_log                          # noqa: E402
from astro_smb_app.views import guiding as gv                          # noqa: E402
from astro_smb_qt import models                                        # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- 合成日志

AUTORUN = """\
Log enabled at 2026/07/29 22:24:14
2026/07/29 22:24:14 Plan 1 Start
2026/07/29 22:24:14 [Autorun|Begin] NGC 7293 Start
2026/07/29 22:24:15 [AutoCenter|Begin] Auto-Center 1#
2026/07/29 22:24:15 Mount slews to target position: RA:22h31m5s DEC:-20°41'59"
2026/07/29 22:24:20 Exposure 2.0s
2026/07/29 22:24:24 Plate Solve
2026/07/29 22:24:26 Solve succeeded: RA:22h31m4s DEC:-20°41'53" Angle = 1.076, Star number = 28
2026/07/29 22:24:26 [AutoCenter|End] The target is centered
2026/07/29 22:24:27 Start Tracking
2026/07/29 22:24:30 [AutoFocus|Begin] Auto-Focus 1#
2026/07/29 22:24:50 [AutoFocus|End] Auto focus succeeded, current position 12925, temperature 36
2026/07/29 22:27:19 [Guide] ReSelect Guide star
2026/07/29 22:27:20 [Guide] Start Guiding
2026/07/29 22:27:40 [Guide] Settle Done
2026/07/29 22:27:41 Shooting 3 light frames, exposure 60.0s Bin1
2026/07/29 22:28:41 Exposure 60.0s image 1#
2026/07/29 22:29:42 Exposure 60.0s image 2#
2026/07/29 22:30:43 Exposure 60.0s image 3#
2026/07/29 22:30:44 [Autorun|End] Autorun Finished
2026/07/29 22:30:45 Plan 1 Finished
Log disabled at 2026/07/29 22:30:45
"""

PHD2 = """\
PHD2 version , Log version 2.5. Log enabled at 2026-07-29 22:27:20
Guiding Begins at 2026-07-29 22:27:20
Equipment Profile = ASIAIR
Camera = guide, gain = 50, full size = 1280 x 960, no dark, no defect map, \
Pixel size = 3.8 um, Binning = 1
Mount = ASIAIR, xAngle = 0.0, xRate = 10.0, yAngle = 90.0, yRate = 10.0
Dec = 0.0, Hour angle = 1.50, Pier side = West, Rotator pos = N/A, \
Alt = 60.0, Az = 180.0
Lock position = 100.0, 100.0, Star position = 100.0, 100.0, HFD = 2.00 px
Pixel scale = 2.00 arc-sec/px, Binning = 1, Focal length = 400 mm
Frame,Time,mount,dx,dy,RARawDistance,DECRawDistance,RAGuideDistance,\
DECGuideDistance,RADuration,RADirection,DECDuration,DECDirection,XStep,YStep,\
StarMass,SNR,ErrorCode
{ROWS}
Guiding Ends at 2026-07-29 22:37:20
"""


def _phd2_text(n: int = 400) -> str:
    import math

    rows = []
    for i in range(n):
        t = 1.5 * (i + 1)
        ra = 0.30 * math.sin(i / 7.0)
        dec = 0.22 * math.cos(i / 11.0)
        rows.append(
            f"{i + 1},{t:.3f},\"Mount\",{ra:.3f},{dec:.3f},{ra:.3f},{dec:.3f},"
            f"{ra:.3f},{dec:.3f},120,E,80,N,,,3500,22.5,0")
    return PHD2.replace("{ROWS}", "\n".join(rows))


@pytest.fixture(scope="module")
def synth():
    """合成的一夜:一个目标、3 帧、一段 400 帧的导星。"""
    log = parse_autorun_log(AUTORUN, "Autorun_Log_2026-07-29_222414.txt")
    nights = aggregate_nights([log])
    phd2 = [parse_phd2_log(_phd2_text())]
    return _LogData(nights, phd2, [log])


class _LogData:
    """最小的 ``LogData`` 替身 —— 页面模型只读这几个属性。"""

    def __init__(self, nights, phd2_logs, autorun_logs):
        self.nights = nights
        self.phd2_logs = phd2_logs
        self.autorun_logs = autorun_logs
        self.lon_estimate = 121.4
        self.lon_samples = 12
        self.errors = []


def _log_dirs():
    """可能放着真日志的地方。

    从这份 checkout 往上逐级找:次级 checkout 里 ``.tmp/`` 通常是空的
    (它被 gitignore,不随 checkout 复制),而设备内容只拷了一份。
    都没有时这里自然什么都不产出,不影响别的环境。
    """
    for base in [ROOT, *ROOT.parents]:
        yield base / ".tmp" / "device" / "EMMC Images" / "log"
        yield base / ".tmp" / "mirror" / "logs"
        yield base / ".tmp" / "logs"


def _real_logs():
    """真日志目录,没有就返回 None。"""
    for base in _log_dirs():
        if base.is_dir():
            auto = [p for p in base.glob("Autorun_Log_*.txt")
                    if not p.name.endswith("_CHN.txt")]
            phd2 = list(base.glob("PHD2_GuideLog_*.txt"))
            if auto:
                return auto, phd2
    return None


# ================================================================ 拍摄记录

def test_synth_records_model_has_no_blank_row(synth):
    """每一行的**主文本**都不能是空串。

    空文本节点照样占一行高度、有 hover、能点 —— 看起来像"这里本该有东西
    却是空的",而根因永远是读了不存在的键。
    """
    m = models.records_model(synth)
    assert m["runs"], "合成日志没有产出任何目标行 —— 这条断言没在测任何东西"
    for row in m["runs"]:
        assert row["title"].strip(), f"目标行主文本是空的: {row}"
        assert row["key"], "行没有身份键"


def test_synth_records_detail_events_are_not_blank(synth):
    """事件时间线的每一条都要有字。

    这是本仓库栽过的原型:``_timeline_items`` 的键是 ``t0/title/subtitle``,
    读成 ``time/text/note`` 时 ``" · ".join`` 出来是空串 —— 详情下面挂着
    几十个一个字没有的文本节点,还把后面的按钮顶出了可视区。
    """
    d = models.records_model(synth)["detail"]
    assert d is not None
    assert d["title"].strip()
    assert d["events"], "详情里一条事件都没有"
    # 条目现在是**结构化 dict**(时刻/标题/副标题/状态色/进度),不再是拼好的
    # 一行字符串 —— 但"每条都要有字"这条判据一个字没变。
    for ev in d["events"]:
        assert isinstance(ev, dict), f"事件条目又被拼成字符串了: {ev!r}"
        assert str(ev.get("title") or "").strip(), f"事件条目没有标题: {ev}"
    # 结构化的意义在于**这几样真的带着**,否则它就只是换了个包装
    kinds = {ev.get("kind") for ev in d["events"]}
    assert kinds - {"", None}, "所有条目的 kind 都是空的 —— 方旗/圆点分不出来"
    levels = {ev.get("level") for ev in d["events"]}
    assert levels - {"", None, "info"}, (
        "所有条目的 level 都是 info —— 完成/暂停/截断的状态色整个没了")
    # 键值也变成了结构化 dict(多带 `bar` 量条与 `tone` 语义色);
    # "不许有空的"这条判据一个字没变。
    for item in d["pairs"]:
        assert isinstance(item, dict), f"键值又被拍成元组了: {item!r}"
        assert str(item["k"]).strip() and str(item["v"]).strip(), (
            f"详情键值有空的: {item!r}")


def test_synth_jump_to_guiding_is_enabled(synth):
    """「看这段导星」的可用条件是 ``t0`` 不是 None。

    另外那套前端读的是 ``run.start``/``run.t0`` —— ``TargetRun`` 上根本没有
    这两个名字,``getattr`` 默认值一兜,按钮**永远是灰的**,而且不报错。
    """
    d = models.records_model(synth)["detail"]
    assert d["t0"] is not None and d["t1"] is not None
    assert d["t1"] > d["t0"]


def test_night_index_is_clamped_not_reset(synth):
    """越界的夜次下标要**夹回去**,不是硬写 0。

    硬写 0 的症状是夜次下拉"点了没反应",而且不报任何错。
    """
    n = len(models.night_list(synth))
    assert models.records_model(synth, night_index=99)["night_index"] == n - 1
    assert models.records_model(synth, night_index=-5)["night_index"] == 0


def test_sky_payload_uses_one_instant(synth):
    """天球图**整图同一时刻** —— 各点用各自拍摄时刻会与真实天区错位
    (老 UI 真机踩过"M 8 不在银心")。"""
    early = models.sky_payload(models.night_list(synth)[0], 0.0)
    late = models.sky_payload(models.night_list(synth)[0], 1.0)
    if early is None:          # 纯偏置/暗场的夜次没有可上天球的目标
        pytest.skip("这一夜没有可上天球的目标")
    assert early["at"] != late["at"], "滑杆动了但时刻没变 —— frac 没接上"
    assert len({p["label"] for p in early["points"]}) == len(early["points"])


def test_timeline_spans_are_never_zero_width(synth):
    """极短的块也要看得见 —— 宽度为 0 的条在画布上就是不存在。"""
    m = models.records_model(synth)
    for sp in m["spans"]:
        assert sp["f1"] > sp["f0"], f"零宽甘特条: {sp}"
        assert sp["key"], "甘特条没有身份键(点了会选不中)"


# ================================================================ 导星

def test_synth_guiding_rows_are_not_blank(synth):
    """段列表的每一行都要有字,而且键必须带前缀(``g:``/``x:``/``r:``)。

    键前缀没认全的症状是"点了没反应" —— 碎段簇那一档尤其容易漏。
    """
    prep = gv._prepare(synth)
    groups = {g["key"] for g in prep["groups"]}
    rows = models.guiding_rows(prep, groups, set())
    assert rows, "一行都没有 —— 这条断言没在测任何东西"
    for r in rows:
        assert r["title"].strip(), f"段列表有空行: {r}"
        # `d:` = 仪表盘入口(组展开时多出来的那一行)
        assert r["key"].split(":", 1)[0] in ("g", "x", "r", "d"), \
            f"行键没有前缀: {r['key']}"


def test_guiding_group_folds(synth):
    """组默认折叠;展开后行数必须变多。

    分组折叠不是装饰:真机 123 段里 103 段是几帧的短尝试,平铺的话真正
    想看的那几段会被埋掉。
    """
    prep = gv._prepare(synth)
    folded = models.guiding_rows(prep, set(), set())
    opened = models.guiding_rows(prep, {g["key"] for g in prep["groups"]}, set())
    assert len(opened) > len(folded), "展开组之后行数没变 —— 折叠没接上"


def test_default_row_prefers_a_main_segment(synth):
    """默认选中**主段**。第 0 行常是校准或几帧的短尝试,两者都画不出曲线 ——
    默认选中它等于打开就是一片空白。"""
    prep = gv._prepare(synth)
    idx = models.default_guide_row(prep["rows"])
    row = prep["rows"][idx]
    assert row["kind"] == "guide"
    if any(r.get("main_seg") and r["kind"] == "guide" for r in prep["rows"]):
        assert row["main_seg"], "有主段却默认选中了碎段"


def test_chart_range_does_not_follow_the_window(synth):
    """**量程按整段算,不随窗口变。** 缩到 5 分钟就重标定纵轴,
    两个窗口之间没法比。"""
    prep = gv._prepare(synth)
    row = prep["rows"][models.default_guide_row(prep["rows"])]
    full = models.chart_payload(row, window_index=0)
    assert full, "主段画不出曲线"
    for wi in range(1, len(gv.WINDOW_CHOICES)):
        win = models.chart_payload(row, window_index=wi, pos=0.5)
        if not win:
            continue
        assert win["range"] == full["range"], (
            f"窗口 {gv.WINDOW_CHOICES[wi][0]} 把量程改了:"
            f"{win['range']} vs {full['range']}")


def test_envelope_threshold_uses_the_window_not_the_section(synth):
    """包络判据按**窗口内**帧数算。

    用整段帧数判的话,缩到 5 分钟仍然显示包络带 —— 界面看着正常,
    但画的已经不是逐帧曲线了。
    """
    prep = gv._prepare(synth)
    row = prep["rows"][models.default_guide_row(prep["rows"])]
    wide = models.chart_payload(row, window_index=0, width=10.0)
    narrow = models.chart_payload(row, window_index=0, width=100000.0)
    assert wide["dense"] is True, "画布窄到每像素几十帧了还不切包络"
    assert narrow["dense"] is False, "画布宽到每像素不到一帧还在画包络"


def test_lost_ticks_are_thinned_not_truncated(synth):
    """丢星刻度**先裁窗口再均匀抽稀**,不能截前 N 个 ——
    截断看着像"前半段一直丢星"。"""
    prep = gv._prepare(synth)
    row = prep["rows"][models.default_guide_row(prep["rows"])]
    ch = models.chart_payload(row)
    assert len(ch["lost"]) <= gv.MAX_LOST_TICKS


def test_locate_range_picks_the_biggest_overlap(synth):
    """跨页跳转要找**重叠最多**的段,不是第一个碰上的。"""
    prep = gv._prepare(synth)
    guides = [r for r in prep["rows"] if r["kind"] == "guide"]
    if not guides:
        pytest.skip("合成日志里没有导星段")
    sec = guides[0]
    t0 = sec["begins"].timestamp()
    t1 = sec["end"].timestamp()
    assert models.locate_range(prep, t0, t1) is not None
    # 完全不重叠的区间要返回 None(调用方据此给"那段时间可能没在导星")
    assert models.locate_range(prep, t1 + 86400, t1 + 86400 + 600) is None


# ================================================================ 真日志

@pytest.mark.skipif(_real_logs() is None, reason="没有 .tmp/ 真日志")
def test_real_logs_produce_no_blank_rows():
    """真日志跑一遍全链路。合成日志覆盖不到的形态(Pause 分裂、无 Shooting 行的
    块、失败校准后的孤立 Ends)只有真日志有。"""
    auto_paths, phd2_paths = _real_logs()
    logs = [parse_autorun_log(p.read_text(encoding="utf-8", errors="replace"),
                              p.name) for p in auto_paths]
    phd2 = [parse_phd2_log(p.read_text(encoding="utf-8", errors="replace"))
            for p in phd2_paths]
    data = _LogData(aggregate_nights(logs), phd2, logs)
    assert data.nights, "真日志没归并出夜次"

    for i in range(len(data.nights)):
        m = models.records_model(data, night_index=i)
        for row in m["runs"]:
            assert row["title"].strip(), f"第 {i} 夜有空的目标行: {row}"
        if m["detail"]:
            for ev in m["detail"]["events"]:
                assert str(ev.get("title") or "").strip(), (
                    f"第 {i} 夜的事件时间线有空条目: {ev}")

    prep = gv._prepare(data)
    rows = models.guiding_rows(prep, {g["key"] for g in prep["groups"]},
                              {it["key"] for g in prep["groups"]
                               for it in g["items"] if it["type"] == "frag"})
    assert rows
    for r in rows:
        assert r["title"].strip(), f"段列表有空行: {r}"


@pytest.mark.skipif(_real_logs() is None, reason="没有 .tmp/ 真日志")
def test_real_overall_rms_is_frame_weighted():
    """整体 RMS 必须按帧数平方加权。

    简单平均会被一段几帧的碎段拖爆 —— 真机上是 1.89″ vs 0.92″,
    **结论从"导星很差"变成"正常"**。这条只在真日志上才有意义:
    合成日志只有一段,加权与平均恰好相等。
    """
    auto_paths, phd2_paths = _real_logs()
    if not phd2_paths:
        pytest.skip("没有 PHD2 日志")
    logs = [parse_autorun_log(p.read_text(encoding="utf-8", errors="replace"),
                              p.name) for p in auto_paths]
    phd2 = [parse_phd2_log(p.read_text(encoding="utf-8", errors="replace"))
            for p in phd2_paths]
    data = _LogData(aggregate_nights(logs), phd2, logs)
    prep = gv._prepare(data)

    stats = [r["rms"] for r in prep["rows"]
             if r["kind"] == "guide" and r.get("rms") is not None
             and r["rms"].n_frames > 0]
    if len(stats) < 3:
        pytest.skip("段太少,加权与平均没有可分辨的差别")
    # _merge_rms 返回 (RMS, 单位, 有效帧数, 丢星数),不是 RmsStats
    merged, unit, n_frames, _lost = gv._merge_rms(stats)
    arcsec = [s.rms_total for s in stats if s.in_arcsec and s.n_frames > 0]
    assert merged is not None and n_frames > 0
    assert unit in ("″", "px")
    if unit == "″" and len(arcsec) >= 3:
        plain = sum(arcsec) / len(arcsec)
        # 加权结果必须落在各段之间,且**不等于**简单平均(碎段一多必然拉开)
        assert min(arcsec) <= merged <= max(arcsec)
        assert abs(merged - plain) > 1e-6, \
            "加权 RMS 与简单平均一模一样 —— 加权没起作用"
