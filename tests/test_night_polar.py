"""夜次级极轴联合反解的接线测试(_sky3d._night_polar / _apply_night_polar)。

夹具是真机足迹:2026-07-30 同夜 NGC 253 + NGC 7293,各 7 张板解算成功。
单目标反解恒为恰定(残差机器零、推翻不了),两目标联合才第一次有残差可看。
"""
from __future__ import annotations

from datetime import datetime

import pytest

from astro_smb import guidecheck as G
from astro_smb_gui._sky3d import _apply_night_polar, _night_polar

LAT, LON = 31.0, 121.071

# (目标, 起始本地时刻, 每帧间隔秒, 帧数, 起始 ra/dec, 每帧 ra/dec 增量)
# 数值取自真机解算:NGC 253 漂移 RA+0.156 DEC-0.051 "/分;NGC 7293 +0.126/-0.012
_RUNS = {
    "NGC 253": (datetime(2026, 7, 30, 1, 30, 49), 910.0, 7,
                11.88800, -25.28800, 0.156, -0.051),
    "NGC 7293": (datetime(2026, 7, 29, 23, 59, 41), 600.0, 7,
                 337.41200, -20.84000, 0.126, -0.012),
}


def _by_target():
    import math
    out = {}
    for name, (t0, step, n, ra0, dec0, dra, ddec) in _RUNS.items():
        rows = []
        for i in range(n):
            mins = i * step / 60.0
            rows.append({
                "ts": (t0.timestamp() + i * step),
                "ra": ra0 + dra * mins / 3600.0 / math.cos(math.radians(dec0)),
                "dec": dec0 + ddec * mins / 3600.0,
            })
        out[name] = rows
    return out


class TestNightPolarWiring:
    def test_two_targets_give_a_falsifiable_answer(self):
        check, names = _night_polar(_by_target(), LAT, LON)
        assert check is not None
        assert sorted(names) == ["NGC 253", "NGC 7293"]
        assert check.n_samples == 2 and check.falsifiable
        assert check.polar.total_arcmin == pytest.approx(1.45, abs=0.25)
        assert check.explained is True

    def test_single_target_is_refused(self):
        """只剩一个目标时不给夜次级结论 —— 那就退回恰定解,毫无增益。"""
        one = {"NGC 253": _by_target()["NGC 253"]}
        check, names = _night_polar(one, LAT, LON)
        assert check is None and names == []

    def test_runs_with_too_few_frames_are_skipped(self):
        few = {k: v[:2] for k, v in _by_target().items()}
        assert _night_polar(few, LAT, LON) == (None, [])

    def test_applies_over_the_unfalsifiable_note(self):
        """替换掉"恰定"那条告白,换成真正有信息量的联合结论。"""
        check, names = _night_polar(_by_target(), LAT, LON)
        # **照真实产出方的形状构造。** `cross_validate` 追加那句告白时会把
        # 原文同时记进 `polar_exact_note`,而剔除是**按相等**做的(不搜关键词
        # —— findings 会被翻译)。手搓一个不带 `polar_exact_note` 的对象,
        # 测的就是一个不存在的调用方。
        note = "注意:只有一个目标时段,极轴反解是**恰定**的 —— ..."
        q = G.CrossCheck(verdict="drift", headline="x",
                         findings=[note, "别的结论"],
                         polar_exact_note=note)
        _apply_night_polar({"NGC 253": q}, check, names)
        assert not any("恰定" in f for f in q.findings)
        assert any("联合反解极轴偏差" in f for f in q.findings)
        assert "别的结论" in q.findings
        assert q.polar is check.polar

    def test_no_check_leaves_everything_alone(self):
        q = G.CrossCheck(verdict="good", headline="x", findings=["原样"])
        _apply_night_polar({"a": q}, None, [])
        assert q.findings == ["原样"]

    def test_none_quality_is_tolerated(self):
        check, names = _night_polar(_by_target(), LAT, LON)
        _apply_night_polar({"a": None}, check, names)      # 不许炸

    def test_no_astral_chars(self):
        """§7.1:这些字符串会直接进 UI,星平面字符会被吞掉末尾一个字。"""
        check, names = _night_polar(_by_target(), LAT, LON)
        q = G.CrossCheck(verdict="drift", headline="x")
        _apply_night_polar({"a": q}, check, names)
        for s in q.findings:
            assert not [c for c in s if ord(c) > 0xFFFF], s
