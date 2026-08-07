"""拍摄记录页:时间轴与目标列表 —— 用户报的 6 / 7 两条。

三处都是**把共享层已经算好的东西又算了一遍,而且算漏了**。这套前端的立身
之本就是"业务一行不重写",所以这几条不只是 bug,是走错了路:

* 导星覆盖只留了每个目标一个**覆盖率**,画出来是一条从头连到尾的绿条 ——
  真实情况是断续的(丢星、换目标、重新校准都会断)。用户原话:"底部的绿色
  线一直是连续的,老UI是不连续的,是实际的数据"。
* 刻度按 ``span/steps`` 均分,标签成了 22:24 / 23:11;老 UI 是**整点**。
* 一个目标的多个块被合成一条,**暂停/截断的半透明**因此丢了。
* 「合并计划」整个没有(老 UI 有,而且默认开)。
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("numpy")

from astro_smb.autorunlog import aggregate_nights, parse_autorun_log  # noqa: E402
from astro_smb.phd2log import parse_phd2_log                          # noqa: E402
from astro_smb_qt import models                                       # noqa: E402
from tests.support import tr

ROOT = Path(__file__).resolve().parents[1]
MIRROR = ROOT / ".tmp" / "device" / "EMMC Images" / "log"


def _real_night():
    """真日志里挑一个有导星、有多个块的夜次;没有真日志就跳过。"""
    if not MIRROR.is_dir():
        pytest.skip("没有离线镜像日志")
    autoruns, phd2 = [], []
    for p in sorted(MIRROR.iterdir()):
        if not p.is_file() or p.suffix.lower() != ".txt":
            continue
        if "_CHN" in p.name:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if p.name.startswith("Autorun_Log"):
            autoruns.append(parse_autorun_log(text, p.name))
        elif p.name.startswith("PHD2_GuideLog"):
            phd2.append(parse_phd2_log(text))
    nights = aggregate_nights(autoruns)
    if not nights:
        pytest.skip("镜像里没有可用夜次")
    # 挑导星区间最多的那一夜 —— 才验得到"绿条断开"
    best, best_n = None, -1
    for n in nights:
        _b, guides, _t = models.timeline_spans(n, phd2)
        if len(guides) > best_n:
            best, best_n = n, len(guides)
    return best, phd2


class TestGuideCoverageIsRealIntervals:

    def test_guides_are_a_separate_list(self):
        night, phd2 = _real_night()
        bars, guides, ticks = models.timeline_spans(night, phd2)
        assert isinstance(guides, list)
        for g in guides:
            assert set(g) == {"f0", "f1"}, g

    def test_guides_come_from_the_shared_layer_verbatim(self):
        """**这条就是用户报的那一条,而且判据必须钉死到共享层。**

        第一版写的是 `len(guides) > len(bars) or 有短区间` —— 太松:把 guides
        换成"每个目标一条"之后,条本来就短,`or` 的后半截照样成立,变异活了下来。
        真正的不变式是**这份区间就是 `rv._night_timeline` 算出来的那一份**,
        一个字都不许自己再算。
        """
        night, phd2 = _real_night()
        from astro_smb_app.views import records as rv

        want = rv._night_timeline(night, phd2)["guides"]
        _b, guides, _t = models.timeline_spans(night, phd2)
        assert [(g["f0"], g["f1"]) for g in guides] == list(want), (
            "导星区间不是共享层那一份 —— 又自己算了一遍(而且必然算漏)")

    def test_the_night_really_has_broken_coverage(self):
        """确认这份样本能验到"断开" —— 否则上一条是在一份连续数据上空转。"""
        night, phd2 = _real_night()
        _b, guides, _t = models.timeline_spans(night, phd2)
        assert len(guides) >= 2, (
            f"样本里只有 {len(guides)} 段导星,验不到'绿条应当断开'")

    def test_guides_are_inside_the_night(self):
        night, phd2 = _real_night()
        _b, guides, _t = models.timeline_spans(night, phd2)
        for g in guides:
            assert -0.001 <= g["f0"] <= g["f1"] <= 1.001, g

    def test_no_guide_key_left_on_bars(self):
        """条上不该再挂覆盖率 —— 留着它下一个人又会拿它画满一条。"""
        night, phd2 = _real_night()
        bars, _g, _t = models.timeline_spans(night, phd2)
        for b in bars:
            assert "guide" not in b, b


class TestTicksAreWholeHours:

    def test_labels_are_on_the_hour(self):
        night, phd2 = _real_night()
        _b, _g, ticks = models.timeline_spans(night, phd2)
        assert ticks, "一个刻度都没有"
        for t in ticks:
            assert t["label"].endswith(":00"), (
                f"刻度 {t['label']} 不是整点 —— 对着日志看时间对不上")


class TestBarsKeepAlpha:
    """半透明 = 暂停 / 被截断。丢掉它所有块看起来都一样正常。"""

    def test_alpha_present(self):
        night, phd2 = _real_night()
        bars, _g, _t = models.timeline_spans(night, phd2)
        assert bars
        for b in bars:
            assert "alpha" in b and 0.0 < float(b["alpha"]) <= 1.0, b

    def test_keys_point_at_real_runs(self):
        night, phd2 = _real_night()
        bars, _g, _t = models.timeline_spans(night, phd2)
        n = len(night.runs)
        for b in bars:
            assert 0 <= int(b["key"]) < n, b


class TestMergePlan:
    """「合并计划」在老 UI 里是有的,而且是这一页的主开关之一。"""

    def _rows(self, merge: bool):
        night, phd2 = _real_night()
        from astro_smb_app.views import records as rv
        guide_map = rv._guide_map_for([night], phd2)
        return models._night_rows(night, guide_map, merge=merge)

    def test_flat_has_no_group_rows(self):
        rows = self._rows(False)
        assert rows
        assert not [r for r in rows if r["kind"] == "group"]

    def test_merged_has_group_rows(self):
        rows = self._rows(True)
        assert [r for r in rows if r["kind"] == "group"], (
            "合并计划下没有组头 —— 那就等于没合并")

    def test_non_run_rows_have_prefixed_keys(self):
        """组头/间隙**不是目标**,点了不该换详情。靠键前缀区分。"""
        for r in self._rows(True):
            if r["kind"] == "run":
                assert r["key"].isdigit(), r
            else:
                assert r["key"][:2] in ("g:", "x:"), r

    def test_run_keys_are_night_indices(self):
        night, _phd2 = _real_night()
        for r in self._rows(True):
            if r["kind"] == "run":
                assert 0 <= int(r["key"]) < len(night.runs), r

    def test_target_count_ignores_headers(self):
        """概览那枚「n 个目标」不能把组头和间隙也数进去。

        第一版自己数了一遍 rows 再和 `len(night.runs)` 比 —— **压根没读
        模型里的 `target_count`**,所以把它改成 `len(rows)` 也照样绿。
        """
        night, phd2 = _real_night()

        class _Data:
            nights = [night]
            phd2_logs = phd2
            autorun_logs: list = []
            lon_estimate = None
            lon_samples = 0

        m = models.records_model(_Data(), merge=True)
        rows = m["runs"]
        assert len(rows) > len(night.runs), "这一夜没有组头/间隙,这条验不到"
        assert m["target_count"] == len(night.runs), (
            f"target_count={m['target_count']} 把组头/间隙也数进去了")


class TestHeaderToolsAreOnTheLeft:
    """夜次下拉跑到窗口另一头,视线要横跨整个屏幕去够。"""

    def test_page_header_has_a_left_tool_area(self):
        src = (ROOT / "astro_smb_qt" / "pages" / "base.py").read_text(
            encoding="utf-8")
        assert "def add_tool" in src, "页头没有左侧工具区"
        # **比的是"工具区被 addLayout 进去"与"弹簧"的先后**,不是变量声明的
        # 先后 —— 第一版比了 `self.tools = W.hbox` 的位置,而那一行不管
        # 工具区最后加在弹簧前还是后都在最前面,于是变异照样绿。
        add_tools = src.index("row.addLayout(self.tools)")
        stretch = src.index("row.addStretch(1)")
        assert add_tools < stretch, (
            "工具区加在弹簧**后面** —— 那它一样会被推到窗口右边")

    def test_records_puts_its_controls_on_the_left(self):
        src = (ROOT / "astro_smb_qt" / "pages" / "records.py").read_text(
            encoding="utf-8")
        for name in ("night_combo", "merge_box"):
            assert f"add_tool(self.{name})" in src, (
                f"{name} 没放进左侧工具区")
        assert "add_action(self.night_combo)" not in src


class TestGuidingSectionStats:
    """导星段统计 —— 用户列的第 9 条(「导星段还没完全做完」)。

    老 UI 那边是一张**结构化**的统计卡;Qt 原来把共享层拼好的那一行 `·` 串
    整段丢进一个 wrap 标签。在比老 UI 窄的右栏里那就是一堵墙,而这几个数恰恰
    是要**逐个对比**的:这一段 RA 大还是 DEC 大?峰值离均值多远?

    两份都由共享层产出(`stats` 一行串 + `stat_rows` 结构化),页面按自己的
    宽度挑一份用 —— **不许自己去 split 那个串**。
    """

    def _prep(self, i: int = 0):
        from astro_smb.phd2log import parse_phd2_log
        from astro_smb_app.views import guiding as gv

        d = MIRROR
        if not d.is_dir():
            pytest.skip("没有离线镜像日志")
        secs = []
        for p in sorted(d.glob("PHD2_GuideLog*.txt")):
            secs += parse_phd2_log(
                p.read_text(encoding="utf-8", errors="replace")).guide_sections
        if not secs:
            pytest.skip("镜像里没有导星段")
        return gv._prep_guide(secs[i])

    def test_rows_exist(self):
        assert self._prep()["stat_rows"], "段统计没有结构化版本"

    def test_ra_and_dec_are_separate_rows(self):
        """只给一个 Total 的话,看得出"差",看不出"差在哪一轴"。"""
        keys = [k for k, _v, _t in self._prep()["stat_rows"]]
        assert "RMS RA" in keys and "RMS DEC" in keys, keys

    def test_peaks_are_there(self):
        """均值正常而峰值很大 = 有阵风或机械跳动。"""
        keys = [k for k, _v, _t in self._prep()["stat_rows"]]
        assert tr("峰值 RA") in keys and tr("峰值 DEC") in keys, keys

    def test_lost_frames_are_toned_when_nonzero(self):
        rows = {k: (v, t) for k, v, t in self._prep()["stat_rows"]}
        value, tone = rows[tr("帧数")]
        assert "丢星" in value
        if "丢星 0)" not in value:
            assert tone == "bad", "有丢星却不上色"

    def test_key_is_always_present(self):
        """**有时有有时没有的键**最难查:页面 `.get()` 不报错,那一块静默消失。"""
        from astro_smb.phd2log import GuideSection
        from astro_smb_app.views import guiding as gv

        empty = GuideSection(begins=self._prep()["begins"], frames=[])
        assert "stat_rows" in gv._prep_guide(empty), (
            "无有效帧的段没有 stat_rows —— 那一块会静默消失")

    def test_model_passes_them_through(self):
        """共享层给了不等于页面拿得到 —— 中间那一层也要验。"""
        from astro_smb.phd2log import parse_phd2_log
        from astro_smb_app.views import guiding as gv

        secs = []
        for p in sorted(MIRROR.glob("PHD2_GuideLog*.txt")):
            secs += parse_phd2_log(
                p.read_text(encoding="utf-8", errors="replace")).guide_sections
        if not secs:
            pytest.skip("镜像里没有导星段")
        prep = gv._prepare_sections([secs[0]]) if hasattr(
            gv, "_prepare_sections") else None
        row = gv._prep_guide(secs[0])
        ch = models.chart_payload(row, window_index=0, pos=0.0, width=900.0)
        assert ch and ch.get("stat_rows"), (
            "模型没把 stat_rows 透传出来 —— 页面 `.get()` 拿到空表,"
            "那一块静默消失")

    def test_page_renders_them_as_rows(self):
        src = (ROOT / "astro_smb_qt" / "pages" / "guiding.py").read_text(
            encoding="utf-8")
        # 段视图的渲染在 `_render_segment` 里(`_render_charts` 现在只做
        # 段视图/仪表盘视图的分流)
        at = src.index("def _render_segment")
        body = src[at:src.index("\n    def ", at + 10)]
        assert "W.MetricRow(k, v, tone=tone)" in body, (
            "拿到了结构化行却没画成一行一行 —— 又是一堵墙")
        assert 'W.GroupHeader(_("段统计")' in body      # 后面还跟着图标参数
        # **循环体在**不等于**循环跑得到** —— 把 `rows` 改成 `[]` 的话
        # 上面两条照样成立(那个 for 只是永远不进)。所以数据从哪来也要钉住。
        assert 'rows = ch.get("stat_rows") or []' in body, (
            "统计行不是从模型里取的 —— 那个 for 循环永远不会进")

    def test_page_does_not_split_the_string(self):
        src = (ROOT / "astro_smb_qt" / "pages" / "guiding.py").read_text(
            encoding="utf-8")
        assert 'ch.get("stat_rows")' in src, "页面没用结构化版本"
        # **先把注释剥掉。** 修好之后我在原处留了一句"原来是
        # `row["sub"].split("·")[0]`" 的说明,按整份源码查会一直红 ——
        # 这一轮第六次栽在注释/文档字符串上。
        code = "\n".join(ln.split("#", 1)[0] for ln in src.splitlines())
        assert '.split("·")' not in code and '.split(" · ")' not in code, (
            "页面自己去 split 那个串 —— 共享层改一下分隔符就全散架")
