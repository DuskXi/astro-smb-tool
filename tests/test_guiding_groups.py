"""导星页「按拍摄目标分组」的离线单测(纯计算,不连设备、不起 XAML)。

覆盖:RMS 语义分级 / 帧数加权合并 / 目标块归组(含 Pause 分裂的块级匹配)/
碎段折叠 / 「其它」组垫底 / loc 反查表 / 逐段 RMS 总览的点击命中反算;
外加 §7.1 的「文本里不许有 emoji」静态扫描(空间页与导星页的用户可见文本
一旦混入星平面字符,win32more 转 HSTRING 会让末尾少一个字符 —— 真机踩过)。
"""

from datetime import datetime, timedelta
from pathlib import Path

from astro_smb.autorunlog import AutorunBlock, Night, TargetRun
from astro_smb.phd2log import (
    CalibrationSection, GuideFrame, GuideSection, Phd2Log, RmsStats,
)
from astro_smb_gui import _guiding as G
from astro_smb_gui.logstore import LogData
from tests.support import tr

D0 = datetime(2026, 7, 23)


def _at(h, m, s=0, day=23):
    return datetime(2026, 7, day, h, m, s)


def _frame(t, ra=0.5, dec=-0.5):
    return GuideFrame(time_s=t, dx=0.0, dy=0.0, ra_raw=ra, dec_raw=dec,
                      ra_guide=0.0, dec_guide=0.0, ra_dur=0, ra_dir="",
                      dec_dur=0, dec_dir="", star_mass=1000.0, snr=20.0, err=0)


def _sec(begins, n, step=2.0, scale=1.0, ra=0.5, dec=-0.5):
    """n 帧、帧间隔 step 秒的导星段(duration_s = n*step)。"""
    sec = GuideSection(begins=begins, pixel_scale=scale)
    sec.frames = [_frame(i * step, ra, dec) for i in range(1, n + 1)]
    sec.ends = begins + timedelta(seconds=n * step)
    return sec


def _block(target, t0, t1):
    return AutorunBlock(target=target, begin_time=t0, end_time=t1,
                        end_mode="Finish")


def _data(sections, cals=(), runs=()):
    night = Night(date="2026-07-23")
    night.runs = list(runs)
    log = Phd2Log(source="PHD2_GuideLog_test.txt", enabled_at=_at(20, 0))
    log.guide_sections = list(sections)
    log.calibrations = list(cals)
    return LogData(nights=[night], phd2_logs=[log])


def _run(target, blocks, plan_no=1):
    r = TargetRun(target=target, plan_no=plan_no)
    r.blocks = list(blocks)
    return r


def _stats(rms_total, n_frames, arcsec=True, lost=0):
    return RmsStats(rms_ra=rms_total / 2 ** 0.5, rms_dec=rms_total / 2 ** 0.5,
                    rms_total=rms_total, peak_ra=0.0, peak_dec=0.0,
                    n_frames=n_frames, n_lost=lost, duration_s=0.0,
                    pixel_scale=1.0 if arcsec else None)


def _src_of(module, dotted: str) -> str:
    """取模块里某个(嵌套)函数/类的源码文本,给"写法必须保持"的静态断言用。"""
    import ast

    src = Path(module.__file__).read_text(encoding="utf-8")
    node = ast.parse(src)
    for part in dotted.split("."):
        found = None
        for child in ast.iter_child_nodes(node):
            if (isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                   ast.ClassDef)) and child.name == part):
                found = child
                break
        assert found is not None, f"{module.__name__} 里找不到 {dotted}"
        node = found
    return ast.get_source_segment(src, node) or ""


class TestRmsHelpers:
    def test_level_thresholds(self):
        assert G._rms_level(0.5, "″") == "good"
        assert G._rms_level(G.BAR_GOOD, "″") == "warn"
        assert G._rms_level(1.4, "″") == "warn"
        assert G._rms_level(G.BAR_WARN, "″") == "bad"
        assert G._rms_level(None, "″") is None
        # px 口径没有质量阈值语义(阈值是角秒的),一律不分级
        assert G._rms_level(0.1, "px") is None

    def test_merge_is_frame_weighted(self):
        # 合并 RMS = sqrt(Σ rms²·n / Σ n),不是算术平均
        v, unit, n, lost = G._merge_rms([_stats(1.0, 300), _stats(2.0, 100)])
        assert unit == "″" and n == 400 and lost == 0
        assert abs(v - ((1.0 ** 2 * 300 + 2.0 ** 2 * 100) / 400) ** 0.5) < 1e-9
        # 算术平均会是 1.5,加权口径必须更接近帧多的那段
        assert v < 1.5

    def test_merge_prefers_arcsec_and_counts_lost(self):
        v, unit, n, lost = G._merge_rms(
            [_stats(1.0, 100, lost=3), _stats(9.0, 900, arcsec=False, lost=2)])
        assert unit == "″" and n == 100 and abs(v - 1.0) < 1e-9
        assert lost == 5           # 丢星跨口径累计(它与单位无关)
        # 全无角秒段时才退回像素口径
        v2, unit2, n2, _ = G._merge_rms([_stats(3.0, 10, arcsec=False)])
        assert unit2 == "px" and n2 == 10 and abs(v2 - 3.0) < 1e-9

    def test_merge_empty(self):
        assert G._merge_rms([None, _stats(1.0, 0)]) == (None, "", 0, 0)


class TestAssign:
    def test_overlap_picks_longest(self):
        blocks = [(_at(21, 0), _at(22, 0), "k1", "M 8"),
                  (_at(21, 50), _at(23, 0), "k2", "NGC 7293")]
        # 21:55~22:30:与 k1 重叠 5 分钟、与 k2 重叠 35 分钟 → k2
        assert G._assign_guide(_at(21, 55), _at(22, 30), blocks) == "k2"
        assert G._assign_guide(_at(1, 0, day=24), _at(1, 30, day=24),
                               blocks) is None

    def test_pause_split_uses_block_intervals(self):
        """Pause 分裂的 run:块级区间才不会把中间那段抢走(logstore 同款教训)。"""
        runs = [_run("M 8", [_block("M 8", _at(21, 0), _at(21, 30)),
                             _block("M 8", _at(22, 30), _at(23, 0))]),
                _run("NGC 7293", [_block("NGC 7293", _at(21, 40), _at(22, 20))],
                     plan_no=2)]
        blocks = G._target_blocks(_data([], runs=runs))
        assert len(blocks) == 3
        key = G._assign_guide(_at(21, 45), _at(22, 10), blocks)
        assert key is not None and key.endswith("NGC 7293")

    def test_calibration_attaches_to_following_block(self):
        blocks = [(_at(22, 10), _at(23, 0), "k2", "NGC 7293")]
        # 校准在目标块开始前 5 分钟 → 归入该目标
        assert G._assign_cal(_at(22, 5), blocks) == "k2"
        # 早太多(> CAL_NEAR_S)则不归
        assert G._assign_cal(_at(21, 0), blocks) is None


class TestBuildGroups:
    def _prep(self):
        runs = [_run("M 8", [_block("M 8", _at(21, 0), _at(22, 0))]),
                _run("NGC 7293",
                     [_block("NGC 7293", _at(22, 10), _at(23, 0))], plan_no=2)]
        secs = [
            _sec(_at(21, 5), 120),                    # 主段(帧数)
            _sec(_at(21, 20), 5), _sec(_at(21, 21), 5), _sec(_at(21, 22), 5),
            _sec(_at(22, 15), 10, step=40.0),         # 主段(时长 400s)
            _sec(_at(2, 0, day=24), 8),               # 无目标块 → 其它
        ]
        cal = CalibrationSection(begins=_at(22, 5), complete=True)
        return G._prepare(_data(secs, cals=[cal], runs=runs))

    def test_group_order_and_membership(self):
        prep = self._prep()
        groups = prep["groups"]
        titles = [g["title"] for g in groups]
        # 最新的目标在前,「其它」永远垫底
        assert titles[0] == "NGC 7293"
        assert titles[1] == "M 8"
        # 组名整条是 `其它(未匹配到拍摄目标)` —— 比开头不比全串,
        # 但开头也得从 msgid 来
        assert titles[-1] == tr("其它(未匹配到拍摄目标)")
        ngc = groups[0]
        assert ngc["n_sec"] == 1
        assert "校准 1 次" in ngc["sub"]
        assert groups[1]["n_sec"] == 4          # 1 主段 + 3 碎段

    def test_fragments_folded_into_one_summary(self):
        prep = self._prep()
        m8 = next(g for g in prep["groups"] if g["title"] == "M 8")
        kinds = [it["type"] for it in m8["items"]]
        assert kinds == ["frag", "row"]         # 倒序:碎段簇在前,主段在后
        frag = m8["items"][0]
        assert len(frag["ris"]) == 3
        # **整条是两个 msgid 拼的**(段数那句 + 「· 平均 RMS …」或
        # 「· 无有效帧」),所以拆成两半各比一次,不比前缀 ——
        # 比前缀在中文下碰巧成立,换语言整条被一起翻掉就不成立了。
        import re as _re
        assert _re.match(
            _re.escape(tr("{0} 段短尝试 · 共 {1:.1f} 分钟", 3, 0.0)
                       ).replace(r"0\.0", r"[\d.]+"), frag["text"]), frag["text"]
        assert tr(" · 平均 RMS {fv:.2f}{funit}", fv=0, funit="")[:8]             in frag["text"] or tr(" · 无有效帧") in frag["text"]
        # loc 反查:碎段行带簇键,主段行不带
        rows = prep["rows"]
        for ri in frag["ris"]:
            assert rows[ri]["main_seg"] is False
            assert prep["loc"][ri] == (m8["key"], frag["key"])
        main_ri = m8["items"][1]["ri"]
        assert rows[main_ri]["main_seg"] is True
        assert prep["loc"][main_ri] == (m8["key"], None)

    def test_isolated_fragment_not_folded(self):
        """孤立碎段不值得折叠成「1 段短尝试」,原样显示。"""
        runs = [_run("M 8", [_block("M 8", _at(21, 0), _at(22, 0))])]
        prep = G._prepare(_data([_sec(_at(21, 5), 120), _sec(_at(21, 30), 4)],
                                runs=runs))
        m8 = next(g for g in prep["groups"] if g["title"] == "M 8")
        assert [it["type"] for it in m8["items"]] == ["row", "row"]

    def test_group_rms_matches_weighted_merge(self):
        prep = self._prep()
        m8 = next(g for g in prep["groups"] if g["title"] == "M 8")
        rows = prep["rows"]
        ris = [it["ri"] for it in m8["items"] if it["type"] == "row"]
        ris += [ri for it in m8["items"] if it["type"] == "frag"
                for ri in it["ris"]]
        want, unit, _n, _lost = G._merge_rms([rows[i]["rms"] for i in ris])
        assert m8["unit"] == unit
        assert abs(m8["rms"] - want) < 1e-9
        assert m8["level"] == G._rms_level(want, unit)

    def test_no_autorun_logs_all_go_to_other(self):
        prep = G._prepare(_data([_sec(_at(21, 5), 120)]))
        assert len(prep["groups"]) == 1
        assert prep["groups"][0]["key"] == G.OTHER_KEY
        assert prep["groups"][0]["n_sec"] == 1

    def test_every_row_is_placed_exactly_once(self):
        prep = self._prep()
        seen = []
        for g in prep["groups"]:
            for it in g["items"]:
                seen += [it["ri"]] if it["type"] == "row" else list(it["ris"])
        assert sorted(seen) == list(range(len(prep["rows"])))
        assert sorted(prep["loc"]) == list(range(len(prep["rows"])))


class TestOverviewHitBar:
    """逐段 RMS 总览:整块画布只挂**一个** Tapped(逐根柱挂事件会被 win32more
    的 `event` 描述符永久 pin 住 —— `_winrt.py` 的 `event.__get__` 把实例存进
    **类级** `_event_setters` 且从不移除,`-=`/`clear()` 只清 `_callbacks`),
    而这张图**每选一次段就重画一次**:选 N 个段就永久滞留 N×柱数 个 Rectangle
    及其闭包。命中柱因此靠几何反算,几何一旦和绘制走偏就会点错段。
    """

    def test_bars_map_to_index(self):
        n = 4
        slot = (G.CHART_W - 2 * G.BAR_M) / n
        for k in range(n):
            x = G.BAR_M + (k + 0.5) * slot          # 槽中心
            assert G.overview_hit_bar(x, n, G.CHART_W) == k

    def test_slot_boundaries(self):
        n = 4
        slot = (G.CHART_W - 2 * G.BAR_M) / n
        assert G.overview_hit_bar(G.BAR_M, n, G.CHART_W) == 0
        assert G.overview_hit_bar(G.BAR_M + slot - 0.01, n, G.CHART_W) == 0
        assert G.overview_hit_bar(G.BAR_M + slot, n, G.CHART_W) == 1

    def test_hit_area_is_the_whole_slot_not_the_bar(self):
        """柱最窄只有 2px,只按柱宽判定几乎点不中 —— 槽内任意 x 都要命中。"""
        n = 60                                       # 槽宽 ≈3.4px,柱宽 2px
        slot = (G.CHART_W - 2 * G.BAR_M) / n
        for k in (0, 17, n - 1):
            lo = G.BAR_M + k * slot
            assert G.overview_hit_bar(lo + 0.01, n, G.CHART_W) == k
            assert G.overview_hit_bar(lo + slot - 0.01, n, G.CHART_W) == k

    def test_outside_margins_miss(self):
        n = 4
        assert G.overview_hit_bar(G.BAR_M - 0.01, n, G.CHART_W) is None
        assert G.overview_hit_bar(G.CHART_W - G.BAR_M + 0.01, n, G.CHART_W) is None
        assert G.overview_hit_bar(-5.0, n, G.CHART_W) is None

    def test_right_edge_maps_to_last_bar(self):
        """右边界恰好落在第 n 槽的起点上,必须夹回最后一柱而不是越界。"""
        n = 7
        k = G.overview_hit_bar(G.CHART_W - G.BAR_M, n, G.CHART_W)
        assert k == n - 1

    def test_single_bar_covers_the_whole_track(self):
        for x in (G.BAR_M, G.CHART_W / 2, G.CHART_W - G.BAR_M):
            assert G.overview_hit_bar(x, 1, G.CHART_W) == 0

    def test_no_bars_never_hits(self):
        assert G.overview_hit_bar(G.CHART_W / 2, 0, G.CHART_W) is None
        assert G.overview_hit_bar(G.CHART_W / 2, -1, G.CHART_W) is None

    def test_degenerate_width_never_hits(self):
        """画布窄到只剩边距(理论上不会,但反算不能因此算出负槽宽)。"""
        assert G.overview_hit_bar(G.BAR_M, 4, 2 * G.BAR_M) is None

    def test_index_is_always_in_range(self):
        n = 5
        for i in range(0, 2210):
            k = G.overview_hit_bar(i / 10.0, n, G.CHART_W)
            assert k is None or 0 <= k < n

    def test_geometry_matches_drawing_constants(self):
        """反算用的几何必须就是绘制用的那组常量/公式(改一处必须两处一起改)。"""
        draw = _src_of(G, "GuidingPage._draw_overview")
        assert "m = BAR_M" in draw
        assert "slot = (w - 2 * m) / n" in draw
        assert "x = m + k * slot + (slot - bw) / 2.0" in draw
        # 绘制时用的画布宽与画出的柱序必须存下来给命中反算用
        assert "self._ov_w = w" in draw
        assert "self._ov_bars = [ri for ri, _ in bars]" in draw
        tapped = _src_of(G, "GuidingPage._on_overview_tapped")
        assert "self._ov_w" in tapped and "self._ov_bars" in tapped
        # 坐标必须在处理器里同步取出(事件参数不能跨帧持有)
        assert "e.GetPosition(self.segrms_canvas)" in tapped

    def test_overview_no_longer_wires_per_bar_events(self):
        draw = _src_of(G, "GuidingPage._draw_overview")
        assert ".Tapped +=" not in draw          # 逐根柱不再挂事件
        wire = _src_of(G, "GuidingPage._wire")
        assert wire.count(".Tapped +=") == 1
        assert "self.segrms_canvas.Tapped +=" in wire

    def test_bars_go_through_the_batch_path(self):
        """柱是一批同款矩形,走 `_append_rects` 的一次 XamlReader.Load;
        只有选中柱的描边(批量片段不带 Stroke)才单独建元素。"""
        draw = _src_of(G, "GuidingPage._draw_overview")
        assert "self._append_rects(cv, rects)" in draw
        assert draw.count("Rectangle()") == 1
        assert "mark.Stroke = self._b_sel" in draw

    def test_hit_table_is_cleared_when_stale(self):
        """图被清空/数据换代后,残留的命中表不能再把点击映射到旧行索引。"""
        draw = _src_of(G, "GuidingPage._draw_overview")
        clear = draw.index("cv.Children.Clear()")
        assert clear < draw.index("self._ov_bars = []") < draw.index("ov = self._overview")
        assert "self._ov_bars = []" in _src_of(G, "GuidingPage._apply_data")
        assert "self._ov_bars" in _src_of(G, "GuidingPage.__init__")


class TestNoAstralChars:
    """§7.1:win32more 把 str 转 HSTRING 时按码点数给长度,而 HSTRING 是
    UTF-16 —— 任何星平面字符(emoji)都会让字符串末尾少一个字符
    (真机现象:目录名 Plan→Pla)。这两页**能进 HSTRING 的东西**
    (Python 字符串字面量 / XAML 文本与属性值)里不许有 BMP 之外的字符;
    `#` 注释不受限(§7.1 的注释里就举了 emoji 当反例)。
    """

    @staticmethod
    def _astral(s: str) -> list[str]:
        return sorted({c for c in s if ord(c) > 0xFFFF})

    def test_python_string_literals_are_bmp_only(self):
        import ast

        base = Path(__file__).resolve().parent.parent / "astro_smb_gui"
        for name in ("_space.py", "_guiding.py"):
            tree = ast.parse((base / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    bad = self._astral(node.value)
                    assert not bad, (f"{name}:{node.lineno} 字符串含星平面字符: "
                                     f"{[hex(ord(c)) for c in bad]}")

    def test_xaml_text_is_bmp_only(self):
        from xml.dom import minidom

        base = Path(__file__).resolve().parent.parent / "astro_smb_gui"
        for name in ("space.xaml", "guiding.xaml"):
            doc = minidom.parse(str(base / name))
            chunks: list[str] = []
            stack = [doc.documentElement]
            while stack:
                el = stack.pop()
                if el.attributes is not None:
                    chunks += [a.value for a in el.attributes.values()]
                for ch in el.childNodes:
                    if ch.nodeType == ch.TEXT_NODE:
                        chunks.append(ch.data)
                    elif ch.nodeType == ch.ELEMENT_NODE:
                        stack.append(ch)
            bad = self._astral("".join(chunks))
            assert not bad, f"{name} 含星平面字符: {[hex(ord(c)) for c in bad]}"
