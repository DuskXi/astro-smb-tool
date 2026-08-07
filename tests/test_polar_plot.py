"""极轴误差示意图(_records.polar_plot_* / polar_advice)。

几何和文案都是纯函数,这里离线钉死。重点在**方位约定**和**调整方向** ——
这两样记反了,用户会照着提示往错的方向拧一晚上。
"""
from __future__ import annotations

import math

import pytest

from astro_smb.guidecheck import POLAR_COND_DEGENERATE, PolarError
from astro_smb_gui._records import (
    POLAR_FULL_SCALES, polar_advice, polar_plot_fragment, polar_plot_geometry,
    polar_plot_scale,
)

SIZE = 132.0


class TestScale:
    @pytest.mark.parametrize("total,want", [
        (0.1, 1.0), (0.9, 2.0), (1.45, 2.0), (3.0, 5.0), (9.0, 20.0),
    ])
    def test_picks_the_smallest_that_fits(self, total, want):
        assert polar_plot_scale(total) == want

    def test_never_returns_zero_for_perfect_alignment(self):
        """极轴完美时也得有个量程,否则后面按它归一会除零。"""
        assert polar_plot_scale(0.0) > 0

    def test_huge_error_clamps_to_the_largest_scale(self):
        assert polar_plot_scale(9999.0) == POLAR_FULL_SCALES[-1]


class TestGeometry:
    def test_perfect_alignment_sits_on_the_pole(self):
        g = polar_plot_geometry(PolarError(0.0, 0.0), SIZE)
        assert g["marker"] == pytest.approx(g["center"])

    def test_east_error_goes_left(self):
        """**北上东左** —— 与 skyview.radar_xy 和天球图同一约定。"""
        g = polar_plot_geometry(PolarError(1.0 / 60, 0.0), SIZE)
        cx, cy = g["center"]
        assert g["marker"][0] < cx, "方位偏东,点子必须往左"
        assert g["marker"][1] == pytest.approx(cy)

    def test_high_error_goes_up(self):
        g = polar_plot_geometry(PolarError(0.0, 1.0 / 60), SIZE)
        cx, cy = g["center"]
        assert g["marker"][1] < cy, "极轴偏高,点子必须往上"
        assert g["marker"][0] == pytest.approx(cx)

    def test_west_and_low_go_the_other_way(self):
        g = polar_plot_geometry(PolarError(-1.0 / 60, -1.0 / 60), SIZE)
        cx, cy = g["center"]
        assert g["marker"][0] > cx and g["marker"][1] > cy

    def test_marker_never_escapes_the_dial(self):
        """极端误差被夹进圆内 —— 画到画布外等于没画。"""
        g = polar_plot_geometry(PolarError(500.0, 500.0), SIZE)
        cx, cy = g["center"]
        assert math.hypot(g["marker"][0] - cx,
                          g["marker"][1] - cy) <= g["radius"] + 1e-6

    def test_offset_is_proportional_to_the_error(self):
        # 必须选**同一量程档**内的两个值,否则归一化的分母都不一样(0.5′ 落
        # 1′ 档、1.0′ 落 2′ 档),比出来的当然不成比例 —— 这条测试最初就写错了。
        a = polar_plot_geometry(PolarError(0.0, 0.3 / 60), SIZE)
        b = polar_plot_geometry(PolarError(0.0, 0.6 / 60), SIZE)
        assert a["full"] == b["full"] == 1.0
        cy = a["center"][1]
        assert (cy - b["marker"][1]) == pytest.approx(2 * (cy - a["marker"][1]))

    def test_no_polar_means_no_marker(self):
        assert polar_plot_geometry(None, SIZE)["marker"] is None

    def test_everything_stays_inside_the_canvas(self):
        g = polar_plot_geometry(PolarError(0.4 / 60, -0.9 / 60), SIZE)
        cx, cy = g["center"]
        assert 0 < cx < SIZE and 0 < cy < SIZE
        assert g["radius"] > 0 and cx + g["radius"] <= SIZE


class TestFragment:
    def test_is_loadable_shaped_xaml(self):
        frag = polar_plot_fragment(PolarError(0.4 / 60, -0.9 / 60), SIZE)
        assert frag.startswith("<Canvas xmlns=") and frag.endswith("</Canvas>")
        assert frag.count("<Ellipse") >= 5     # 4 个环 + 天极点 + 落点
        assert "<Line" in frag

    def test_uses_invariant_decimal_point(self):
        """XAML 按 invariant culture 解析,逗号小数点会让整段片段解析失败。"""
        frag = polar_plot_fragment(PolarError(0.4 / 60, -0.9 / 60), SIZE)
        assert ",0" not in frag.replace('Points="', "")

    def test_no_astral_chars(self):
        """§7.1:星平面字符会让 HSTRING 末尾少一个字。"""
        frag = polar_plot_fragment(PolarError(0.4 / 60, -0.9 / 60), SIZE)
        assert not [c for c in frag if ord(c) > 0xFFFF]

    def test_perfect_alignment_still_draws_the_dial(self):
        frag = polar_plot_fragment(PolarError(0.0, 0.0), SIZE)
        assert "<Ellipse" in frag


class TestAdvice:
    def test_direction_is_opposite_to_the_error(self):
        """误差偏东 ⇒ 提示往**西**拧。反了就白折腾一晚上。"""
        east_high = polar_advice(PolarError(2.0 / 60, 1.0 / 60),
                                 cond=2.0, falsifiable=True)
        assert "向西" in east_high and "下调" in east_high
        west_low = polar_advice(PolarError(-2.0 / 60, -1.0 / 60),
                                cond=2.0, falsifiable=True)
        assert "向东" in west_low and "上调" in west_low

    def test_amounts_are_the_component_magnitudes(self):
        text = polar_advice(PolarError(2.0 / 60, 1.0 / 60),
                            cond=2.0, falsifiable=True)
        assert "2.00" in text and "1.00" in text

    def test_degenerate_refuses_to_give_a_direction(self):
        """简并时给方向就是误导 —— 只报总量。"""
        text = polar_advice(PolarError(2.0 / 60, 1.0 / 60),
                            cond=POLAR_COND_DEGENERATE + 1, falsifiable=True)
        assert "简并" in text
        assert "向西" not in text and "向东" not in text

    def test_unfalsifiable_is_flagged_in_the_text(self):
        text = polar_advice(PolarError(2.0 / 60, 1.0 / 60),
                            cond=2.0, falsifiable=False)
        assert "推翻不了" in text

    def test_falsifiable_has_no_caveat(self):
        text = polar_advice(PolarError(2.0 / 60, 1.0 / 60),
                            cond=2.0, falsifiable=True)
        assert "推翻不了" not in text

    def test_no_polar_gives_empty(self):
        assert polar_advice(None) == ""

    def test_no_astral_chars(self):
        for kw in ({"cond": 2.0, "falsifiable": True},
                   {"cond": 999.0, "falsifiable": False}):
            text = polar_advice(PolarError(2.0 / 60, -1.0 / 60), **kw)
            assert not [c for c in text if ord(c) > 0xFFFF], text
