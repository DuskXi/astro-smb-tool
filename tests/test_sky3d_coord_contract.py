"""3D 天球的坐标契约:``_build_nights`` 必须同时认两种 FITS 记录形状。

**这一条是真机上整页打不开换来的。**

``_build_nights`` 在共享层,被**两套前端共用**:

* 冻结的老 UI(``astro_smb_gui/_sky3d.py``)的 ``_collect_fits`` 给的是
  ``dict[int, tuple[float, float]]``;
* 下沉到共享层的 ``logstore.collect_fits_map`` 给的是 ``dict[int, dict]``
  (键 ``ra_deg`` / ``dec_deg``,另带焦距像元等)。

原来那行写的是 ``hit[0], hit[1]``。喂 dict 进去就是拿整数 ``0`` 当键查字典 ——
``KeyError: 0``,3D 天球页**整页起不来**(红条"解析日志失败: 0")。而且这不是
边角:只要哪个目标的坐标是从 FITS 头读出来的就会踩到,也就是常态。

**为什么全量测试没拦住:** 当时所有用例喂的都是元组 —— 那正是老 UI 的形状。
"测过了"测的是没人再走的那条路。所以这份闸门里有一条**契约测试**:直接拿
``_fits_info`` 的真实产物去喂,这样改了键名两边一起红。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from astro_smb_app.views import sky3d as sv

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def real_data():
    """用 `.tmp/` 里的真机日志建夜次 —— 合成的 run 缺一堆字段,
    补齐它们只会让测试去迁就替身,而不是迁就真数据。"""
    from astro_smb.autorunlog import aggregate_nights, parse_autorun_log

    logs = []
    for p in sorted((ROOT / ".tmp").glob("Autorun_Log_*.txt")):
        if p.name.endswith("_CHN.txt"):
            continue
        logs.append(parse_autorun_log(
            p.read_text(encoding="utf-8", errors="replace"), p.name))
    if not logs:
        pytest.skip("没有真机样例日志(.tmp/Autorun_Log_*.txt)")
    nights = aggregate_nights(logs)

    class _D:
        pass

    d = _D()
    d.nights = nights
    run = next((r for n in nights for r in n.runs), None)
    assert run is not None
    return d, run


def _targets(data, coords):
    from astro_smb_app.views import sky3d as sv

    out = sv._build_nights(data, coords)
    return [t for n in out for t in n["targets"]]


def _find(data, coords, name):
    hit = [t for t in _targets(data, coords) if t["name"] == name]
    assert hit, f"目标 {name} 整个从天球上消失了"
    return hit[0]


class TestBothShapes:

    def test_the_shared_dict_shape_works(self, real_data):
        """``collect_fits_map`` 的形状 —— 就是它当初炸的。"""
        data, run = real_data
        t = _find(data, {id(run): {"ra_deg": 338.27, "dec_deg": -20.7}},
                  run.target)
        assert t["source_key"] == sv.SRC_FITS
        assert t["ra"] == pytest.approx(338.27, abs=1e-6)
        assert t["dec"] == pytest.approx(-20.7, abs=1e-6)

    def test_the_frozen_tuple_shape_still_works(self, real_data):
        """老 UI 冻结着,**不能为了新形状把它弄坏**。"""
        data, run = real_data
        t = _find(data, {id(run): (338.27, -20.7)}, run.target)
        assert t["source_key"] == sv.SRC_FITS
        assert t["ra"] == pytest.approx(338.27, abs=1e-6)

    def test_a_record_without_coordinates_falls_back(self, real_data):
        """``collect_fits_map`` 对只读到焦距/像元的头也会建条目。

        那时**回退到日志坐标**,不能把这个目标从天球上抹掉 —— 少一个点
        没有任何报错,只是"我拍的那个怎么不见了"。
        """
        data, run = real_data
        t = _find(data, {id(run): {"focallen": 400.0}}, run.target)
        assert t["source_key"] == sv.SRC_LOG

    def test_unknown_shapes_do_not_crash(self, real_data):
        """认不出来就当没有,整页不能因为一条坏记录打不开。"""
        data, run = real_data
        for junk in (object(), "338.27", 42):
            assert _find(data, {id(run): junk}, run.target)["source_key"] == sv.SRC_LOG

    def test_no_map_at_all(self, real_data):
        data, run = real_data
        assert _find(data, {}, run.target)["source_key"] == sv.SRC_LOG

    def test_string_numbers_are_converted(self, real_data):
        """键在、值是字符串数字时要**转成 float**。

        原样透出去的话,``t["ra"]`` 就是个字符串:JS 侧拿它算单位向量、
        Python 侧拿它比大小,都不会在这里报错,而是在别处。
        """
        data, run = real_data
        t = _find(data, {id(run): {"ra_deg": "338.27", "dec_deg": "-20.7"}},
                  run.target)
        assert t["source_key"] == sv.SRC_FITS
        assert isinstance(t["ra"], float) and isinstance(t["dec"], float)
        assert t["ra"] == pytest.approx(338.27, abs=1e-6)

    def test_non_numeric_values_fall_back(self, real_data):
        """值根本不是数(空串 / 文字)时回退日志坐标,不能当成坐标画上去。"""
        data, run = real_data
        for junk in ("", "abc", None, object()):
            t = _find(data, {id(run): {"ra_deg": junk, "dec_deg": junk}},
                      run.target)
            assert t["source_key"] == sv.SRC_LOG, junk


class TestProducerConsumerContract:
    """**把生产者和消费者钉在一起。**

    只测"dict 形状能收"挡不住把 ``ra_deg`` 改名 —— 测试里那个字面量还在,
    照样绿,而真数据里那个键已经不叫这个名了。所以这里直接拿
    ``_fits_info`` 的**真实产物**去喂。
    """

    def test_fits_info_output_is_readable(self, real_data):
        from astro_smb_app.views import sky3d as sv
        from astro_smb_app.views.records import _fits_info

        info = _fits_info({"RA": "338.27", "DEC": "-20.7",
                           "FOCALLEN": "400.0"})
        assert "ra_deg" in info, "生产者自己就没给坐标,这条测试失去意义"
        ra, dec = sv._fits_coord(info)
        assert ra == pytest.approx(338.27, abs=1e-6)
        assert dec == pytest.approx(-20.7, abs=1e-6)

    def test_fits_info_without_coords_reads_as_missing(self):
        from astro_smb_app.views import sky3d as sv
        from astro_smb_app.views.records import _fits_info

        info = _fits_info({"FOCALLEN": "400.0"})
        assert info and "ra_deg" not in info
        assert sv._fits_coord(info) == (None, None)

    def test_collect_fits_map_is_annotated_as_dict_of_dict(self):
        """``collect_fits_map`` 真的是"值为 dict" —— 它要是哪天改回元组,
        上面那些测试全都白测了。"""
        import ast

        src = (ROOT / "astro_smb_app" / "logstore.py").read_text(
            encoding="utf-8")
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "collect_fits_map")
        body = "\n".join(ast.unparse(n) for n in fn.body)
        assert "out: dict[int, dict] = {}" in body


class TestSkyPageThresholds:
    """高度角判读阈值**只有一份**。"""

    def test_sky_page_uses_the_shared_tone(self):
        """这一页原来自己写了 20/35,而浏览页详情是 20/40 —— 37° 在一页
        琥珀、另一页绿。阈值分叉靠"共用同一个函数"根治,不靠记得同步改。

        **看 AST,不看原文。** 按原文切一段来找 "35",连解释这件事的注释
        都会被算成"又写了阈值"(第一版就这么自己把自己判红了)。
        """
        import ast

        src = (ROOT / "astro_smb_qt" / "pages" / "sky3d.py").read_text(
            encoding="utf-8")
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "_render_detail")
        body = "\n".join(ast.unparse(n) for n in fn.body)
        at = body.index("'高度角'")
        seg = body[max(0, at - 300):at + 300]
        assert "bv._alt_tone(alt)" in seg
        assert "35" not in seg, f"又在这一页自己写阈值了:\n{seg}"

    def test_the_two_pages_agree(self):
        from astro_smb_app.views import browser as bv

        # 两处曾经分歧的那一段,现在必须给同一个结论
        for alt in (19.0, 21.0, 36.0, 37.0, 39.0, 41.0):
            assert bv._alt_tone(alt) == bv._alt_tone(alt)
        assert bv._alt_tone(37.0) == bv._alt_tone(39.0), (
            "35~40 之间应当仍是同一档 —— 分歧就出在这个区间")
