"""astro_smb.catalog / catalog_build 的离线单测(不连设备、不联网)。

覆盖:打包格式往返与量化误差上界、版本/损坏文件的报错、按 dec 排序不变量、
锥形查询与**暴力全表**逐条比对(含过极点/过 RA=0/极小极大半径/自行外推)、
下载器的校验逻辑(用 ``file://`` 与本地假文件驱动),以及 Tycho-2 原始格式解析。
"""
from __future__ import annotations

import gzip
import hashlib
import math
import struct
from pathlib import Path

import numpy as np
import pytest

from astro_smb import catalog as C
from astro_smb import catalog_build as B


# ---------------------------------------------------------------- 工具

def synth(n: int = 4000, seed: int = 0):
    """在球面上均匀撒 n 颗星(dec 用 arcsin 采样,避免极区堆积)。"""
    rng = np.random.default_rng(seed)
    ra = rng.uniform(0.0, 360.0, n)
    dec = np.degrees(np.arcsin(rng.uniform(-1.0, 1.0, n)))
    vmag = rng.uniform(1.0, 14.0, n)
    pmra = rng.normal(0.0, 60.0, n)
    pmde = rng.normal(0.0, 60.0, n)
    return ra, dec, vmag, pmra, pmde


def build_file(tmp_path: Path, n: int = 4000, seed: int = 0, name="c.bin"):
    ra, dec, vmag, pmra, pmde = synth(n, seed)
    p = tmp_path / name
    C.write_catalog(p, ra, dec, vmag, pmra, pmde)
    return p


def brute(cat: C.Catalog, ra, dec, r, max_mag=None, epoch=None):
    """暴力全表:锥形查询的参考实现。"""
    ra_a, dec_a = cat.positions_at(epoch)
    keep = C.angular_separation(ra, dec, ra_a, dec_a) <= r
    if max_mag is not None:
        keep &= cat.vmag <= max_mag
    return np.flatnonzero(keep)


# ---------------------------------------------------------------- 格式

class TestFormat:
    def test_record_is_14_bytes(self):
        assert C.RECORD_DTYPE.itemsize == C.RECORD_BYTES == 14

    def test_header_is_64_bytes(self):
        assert C._HEADER_STRUCT.size == C.HEADER_BYTES == 64

    def test_header_roundtrip(self):
        h = C.CatalogHeader(version=C.FORMAT_VERSION, record_bytes=14,
                            count=123456, epoch=2000.0, built_unix=1.75e9,
                            flags=C.FLAG_SORTED_DEC, source="Tycho-2")
        got = C.unpack_header(C.pack_header(h))
        assert got == h
        assert got.sorted_by_dec
        assert got.data_bytes == 123456 * 14
        assert got.file_bytes == 64 + 123456 * 14

    def test_bad_magic_raises(self):
        raw = bytearray(C.pack_header(C.CatalogHeader(
            C.FORMAT_VERSION, 14, 1, 2000.0, 0.0, 1, "x")))
        raw[:8] = b"NOTACAT!"
        with pytest.raises(C.CatalogError, match="magic"):
            C.unpack_header(bytes(raw))

    def test_wrong_version_raises_not_guesses(self):
        """版本不兼容必须**明确报错**,绝不"尽力解析"(错位解析后果更难查)。"""
        raw = bytearray(C.pack_header(C.CatalogHeader(
            C.FORMAT_VERSION, 14, 1, 2000.0, 0.0, 1, "x")))
        struct.pack_into("<H", raw, 8, C.FORMAT_VERSION + 7)
        with pytest.raises(C.CatalogError, match="版本不兼容"):
            C.unpack_header(bytes(raw))

    def test_wrong_record_size_raises(self):
        raw = bytearray(C.pack_header(C.CatalogHeader(
            C.FORMAT_VERSION, 14, 1, 2000.0, 0.0, 1, "x")))
        struct.pack_into("<H", raw, 10, 16)
        with pytest.raises(C.CatalogError, match="记录长度"):
            C.unpack_header(bytes(raw))

    def test_short_header_raises(self):
        with pytest.raises(C.CatalogError, match="文件头不完整"):
            C.unpack_header(b"ASTARCAT" + b"\x00" * 10)

    def test_count_out_of_range_raises(self):
        h = C.CatalogHeader(C.FORMAT_VERSION, 14, 1 << 33, 2000.0, 0.0, 1, "x")
        with pytest.raises(C.CatalogError, match="条目数超出范围"):
            C.pack_header(h)


# ---------------------------------------------------------------- 编解码

class TestEncoding:
    def test_roundtrip_each_field(self):
        ra = np.array([0.0, 12.3456789, 359.999999, 180.0])
        dec = np.array([0.0, -89.5, 89.5, -0.000001])
        vm = np.array([-1.5, 0.0, 11.234, 15.0])
        pa = np.array([0.0, -1234.0, 5678.0, 3.0])
        pd = np.array([1.0, 9999.0, -9999.0, 0.0])
        r = C.encode_records(ra, dec, vm, pa, pd)
        assert np.allclose(C.decode_ra(r["ra"]), ra, atol=1e-6)
        assert np.allclose(C.decode_dec(r["dec"]), dec, atol=1e-6)
        assert np.allclose(C.decode_vmag(r["vmag"]), vm, atol=5e-4)
        assert np.allclose(r["pmra"], pa)
        assert np.allclose(r["pmde"], pd)

    def test_position_quantisation_bound(self):
        """量化误差:每轴 ≤ 0.5 微度 = 1.8 mas,合成 ≤ 2.55 mas。

        这是选 14 字节格式的前提 —— 必须远小于 Tycho-2 自身 60 mas 的位置精度。
        """
        ra, dec, vm, pa, pd = synth(3000, seed=5)
        r = C.encode_records(ra, dec, vm, pa, pd)
        d_ra = np.abs(C.decode_ra(r["ra"]) - ra) * 3.6e6      # mas(赤经角)
        d_dec = np.abs(C.decode_dec(r["dec"]) - dec) * 3.6e6
        assert d_ra.max() <= 1.81
        assert d_dec.max() <= 1.81
        sep = C.angular_separation(ra, dec, C.decode_ra(r["ra"]),
                                   C.decode_dec(r["dec"])) * 3.6e6
        assert sep.max() <= 2.56
        assert sep.max() < 60.0     # 远小于 Tycho-2 自身精度

    def test_ra_wrap_does_not_overflow_to_360(self):
        """359.9999999 四舍五入后是 360e6 微度,必须回绕成 0 而不是越界。"""
        r = C.encode_records([359.9999999], [0.0], [5.0], [0.0], [0.0])
        assert 0 <= int(r["ra"][0]) < 360_000_000
        assert int(r["ra"][0]) == 0

    def test_ra_normalised_from_negative_and_over(self):
        r = C.encode_records([-10.0, 370.0], [0.0, 0.0], [5.0, 5.0],
                             [0, 0], [0, 0])
        assert np.allclose(C.decode_ra(r["ra"]), [350.0, 10.0], atol=1e-6)

    def test_dec_clipped(self):
        r = C.encode_records([0, 0], [95.0, -95.0], [5, 5], [0, 0], [0, 0])
        assert np.allclose(C.decode_dec(r["dec"]), [90.0, -90.0])

    def test_negative_magnitude_survives(self):
        """亮星 V 可以为负(偏移编码就是为了这个)。"""
        r = C.encode_records([0], [0], [-1.46], [0], [0])
        assert C.decode_vmag(r["vmag"])[0] == pytest.approx(-1.46, abs=5e-4)

    def test_magnitude_clipped_at_both_ends(self):
        r = C.encode_records([0, 0], [0, 0], [-99.0, 999.0], [0, 0], [0, 0])
        assert int(r["vmag"][0]) == 0
        assert int(r["vmag"][1]) == 65535

    def test_proper_motion_clipped_to_int16(self):
        r = C.encode_records([0, 0], [0, 0], [5, 5],
                             [99999.0, -99999.0], [-99999.0, 99999.0])
        assert int(r["pmra"][0]) == 32767 and int(r["pmra"][1]) == -32768
        assert int(r["pmde"][0]) == -32768 and int(r["pmde"][1]) == 32767

    def test_nan_pm_becomes_zero_and_nan_mag_becomes_faintest(self):
        r = C.encode_records([1.0], [2.0], [np.nan], [np.nan], [np.nan])
        assert int(r["pmra"][0]) == 0 and int(r["pmde"][0]) == 0
        assert int(r["vmag"][0]) == 65535

    def test_nan_position_raises(self):
        with pytest.raises(C.CatalogError, match="NaN"):
            C.encode_records([np.nan], [0.0], [5.0], [0.0], [0.0])

    def test_length_mismatch_raises(self):
        with pytest.raises(C.CatalogError, match="长度不一致"):
            C.encode_records([1, 2], [1], [5], [0], [0])


# ---------------------------------------------------------------- 文件

class TestFile:
    def test_write_read_and_sorted_invariant(self, tmp_path):
        p = build_file(tmp_path, 2000)
        cat = C.Catalog.open(p)
        assert len(cat) == 2000
        assert cat.header.sorted_by_dec
        assert cat.verify_sorted()
        assert np.all(np.diff(cat.dec) >= 0)

    def test_written_values_match_input_set(self, tmp_path):
        """排序会打乱顺序,但**集合**必须一一对应。"""
        ra, dec, vm, pa, pd = synth(500, seed=9)
        p = tmp_path / "s.bin"
        C.write_catalog(p, ra, dec, vm, pa, pd)
        cat = C.Catalog.open(p)
        order = np.argsort(dec, kind="stable")
        assert np.allclose(cat.dec, dec[order], atol=1e-6)
        assert np.allclose(cat.ra, ra[order], atol=1e-6)
        assert np.allclose(cat.vmag, vm[order], atol=5e-4)

    def test_presorted_true_rejects_unsorted(self, tmp_path):
        rec = C.encode_records([0, 0, 0], [10.0, -10.0, 0.0],
                               [5, 5, 5], [0, 0, 0], [0, 0, 0])
        with pytest.raises(C.CatalogError, match="并非按 dec 升序"):
            C.write_records(tmp_path / "u.bin", rec, presorted=True)

    def test_presorted_true_accepts_sorted(self, tmp_path):
        rec = C.encode_records([0, 0, 0], [-10.0, 0.0, 10.0],
                               [5, 5, 5], [0, 0, 0], [0, 0, 0])
        h = C.write_records(tmp_path / "s2.bin", rec, presorted=True)
        assert h.count == 3

    def test_write_records_rejects_wrong_dtype(self, tmp_path):
        with pytest.raises(C.CatalogError, match="dtype"):
            C.write_records(tmp_path / "x.bin", np.zeros(3, dtype=np.int32))

    def test_no_part_file_left_behind(self, tmp_path):
        build_file(tmp_path, 100)
        assert list(tmp_path.glob("*.part")) == []

    def test_truncated_file_rejected(self, tmp_path):
        """**下载被截断是实测发生过的**,长度对账必须拦住。"""
        p = build_file(tmp_path, 500)
        data = p.read_bytes()
        p.write_bytes(data[:len(data) - 14 * 20])
        with pytest.raises(C.CatalogError, match="长度不符|截断"):
            C.validate_catalog_file(p)

    def test_missing_sorted_flag_rejected(self, tmp_path):
        p = build_file(tmp_path, 50)
        raw = bytearray(p.read_bytes())
        struct.pack_into("<I", raw, 8 + 2 + 2 + 4 + 8 + 8, 0)   # flags = 0
        p.write_bytes(bytes(raw))
        with pytest.raises(C.CatalogError, match="排序"):
            C.validate_catalog_file(p)

    def test_expect_count_mismatch_rejected(self, tmp_path):
        p = build_file(tmp_path, 50)
        with pytest.raises(C.CatalogError, match="条目数不符"):
            C.validate_catalog_file(p, expect_count=51)

    def test_sha256_checked(self, tmp_path):
        p = build_file(tmp_path, 50)
        good = hashlib.sha256(p.read_bytes()).hexdigest()
        assert C.validate_catalog_file(p, expect_sha256=good).count == 50
        with pytest.raises(C.CatalogError, match="sha256"):
            C.validate_catalog_file(p, expect_sha256="00" * 32)

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(C.CatalogError, match="无法读取"):
            C.read_catalog_header(tmp_path / "nope.bin")

    def test_open_memmap_mode(self, tmp_path):
        p = build_file(tmp_path, 300)
        with C.Catalog.open(p, mmap=True) as cat:
            assert len(cat) == 300
            assert cat.cone(cat.ra[10], cat.dec[10], 1e-4).size >= 1

    def test_empty_catalog(self, tmp_path):
        p = tmp_path / "e.bin"
        C.write_catalog(p, [], [], [], [], [])
        cat = C.Catalog.open(p)
        assert len(cat) == 0
        assert cat.verify_sorted()
        assert cat.cone(10.0, 20.0, 5.0).size == 0
        assert cat.max_pm_mas == 0.0

    def test_repr_and_len(self, tmp_path):
        cat = C.Catalog.open(build_file(tmp_path, 10))
        assert "Tycho-2" in repr(cat) and "10" in repr(cat)


# ---------------------------------------------------------------- 锥形查询

class TestCone:
    @pytest.fixture(scope="class")
    @staticmethod
    def cat(tmp_path_factory):
        p = build_file(tmp_path_factory.mktemp("cat"), 6000, seed=42)
        return C.Catalog.open(p)

    def test_matches_brute_force_random_centres(self, cat):
        rng = np.random.default_rng(1)
        for _ in range(40):
            ra = rng.uniform(0, 360)
            dec = math.degrees(math.asin(rng.uniform(-1, 1)))
            r = float(rng.choice([0.5, 2.0, 7.0, 20.0]))
            assert np.array_equal(cat.cone(ra, dec, r), brute(cat, ra, dec, r))

    def test_matches_brute_force_at_catalog_stars(self, cat):
        """以真实星为中心 —— 边界上恰好有星,最容易暴露阈值问题。"""
        rng = np.random.default_rng(2)
        for k in rng.integers(0, len(cat), 25):
            ra, dec = float(cat.ra[k]), float(cat.dec[k])
            for r in (1e-5, 1e-3, 0.1, 3.0):
                assert np.array_equal(cat.cone(ra, dec, r),
                                      brute(cat, ra, dec, r)), (k, r)

    def test_tiny_radius_regression(self, cat):
        """回归:小半径下 cos(r) 在 float32 里舍入成 1.0,曾**静默返回空集**。

        修复前 r ≲ 0.03° 的查询会漏掉中心那颗星本身(实测真表 r=2e-5° 返回 0 条)。
        """
        for k in (0, 137, 4321, len(cat) - 1):
            ra, dec = float(cat.ra[k]), float(cat.dec[k])
            for r in (1e-7, 1e-6, 1e-5, 1e-4, 1e-3, 1e-2):
                got = cat.cone(ra, dec, r)
                assert got.size >= 1, (k, r)
                assert k in got, (k, r)

    def test_crosses_ra_zero(self, cat):
        for ra in (0.0, 0.3, 359.7, 360.0):
            assert np.array_equal(cat.cone(ra, 0.0, 4.0),
                                  brute(cat, ra, 0.0, 4.0))

    def test_over_north_pole(self, cat):
        for dec in (88.0, 89.9, 90.0):
            assert np.array_equal(cat.cone(123.0, dec, 5.0),
                                  brute(cat, 123.0, dec, 5.0))

    def test_over_south_pole(self, cat):
        for dec in (-88.0, -90.0):
            assert np.array_equal(cat.cone(200.0, dec, 6.0),
                                  brute(cat, 200.0, dec, 6.0))

    def test_radius_zero(self, cat):
        got = cat.cone(cat.ra[5] + 0.5, cat.dec[5] + 0.5, 0.0)
        assert got.size == 0
        # 中心正好落在星上时半径 0 也应命中
        assert cat.cone(float(cat.ra[5]), float(cat.dec[5]), 0.0).size >= 1

    def test_radius_180_returns_all(self, cat):
        assert cat.cone(10.0, 20.0, 180.0).size == len(cat)
        assert cat.cone(10.0, 20.0, 200.0).size == len(cat)

    def test_negative_radius_returns_empty(self, cat):
        assert cat.cone(10.0, 20.0, -1.0).size == 0

    def test_empty_region(self, cat):
        got = cat.cone(float(cat.ra[0]), float(cat.dec[0]), 1e-9)
        assert got.size <= 1
        far = cat.cone(0.0, 90.0, 1e-6)
        assert far.size == brute(cat, 0.0, 90.0, 1e-6).size

    def test_indices_ascending_and_int64(self, cat):
        got = cat.cone(50.0, 10.0, 8.0)
        assert got.dtype == np.int64
        assert np.all(np.diff(got) > 0)

    def test_max_mag_filter(self, cat):
        got = cat.cone(50.0, 10.0, 15.0, max_mag=6.0)
        assert np.all(cat.vmag[got] <= 6.0)
        assert np.array_equal(got, brute(cat, 50.0, 10.0, 15.0, max_mag=6.0))

    def test_limit_returns_brightest(self, cat):
        full = cat.cone(50.0, 10.0, 15.0)
        k = 12
        got = cat.cone(50.0, 10.0, 15.0, limit=k)
        assert got.size == k
        assert np.all(np.diff(got) > 0)          # 仍是升序索引
        dropped = np.setdiff1d(full, got)
        assert cat.vmag[got].max() <= cat.vmag[dropped].min() + 1e-9

    def test_limit_larger_than_result_is_noop(self, cat):
        full = cat.cone(50.0, 10.0, 3.0)
        assert np.array_equal(cat.cone(50.0, 10.0, 3.0, limit=full.size + 99),
                              full)

    def test_limit_zero_returns_empty(self, cat):
        assert cat.cone(50.0, 10.0, 15.0, limit=0).size == 0

    def test_dec_band_covers_everything_within_radius(self, cat):
        i0, i1 = cat.dec_band(-10.0, 10.0)
        inside = np.flatnonzero((cat.dec >= -10.0) & (cat.dec <= 10.0))
        assert i0 <= inside.min() and i1 > inside.max()

    def test_dec_key_is_contiguous(self, cat):
        assert cat.dec_key.flags["C_CONTIGUOUS"]

    def test_xyz_t_rows_contiguous_and_unit(self, cat):
        xt = cat.xyz_t
        assert xt.shape == (3, len(cat))
        assert xt[0, 10:200].flags["C_CONTIGUOUS"]
        norm = np.sqrt((xt.astype(np.float64) ** 2).sum(axis=0))
        assert np.allclose(norm, 1.0, atol=1e-5)

    def test_xyz_t_builds_directly_without_full_table_helper(self, tmp_path,
                                                             monkeypatch):
        """整表向量必须分块落进最终 SoA，不能恢复成大角度数组 + (N,3) 副本。"""
        cat = C.Catalog.open(build_file(tmp_path, 1000, seed=81))
        monkeypatch.setattr(
            C, "unit_vectors",
            lambda *a, **k: pytest.fail("xyz_t 不应调用整表 unit_vectors"))
        assert cat.xyz_t.shape == (3, 1000)
        assert np.all(np.isfinite(cat.xyz_t))


# ---------------------------------------------------------------- 自行

class TestProperMotion:
    def test_cone_with_epoch_matches_brute(self, tmp_path):
        cat = C.Catalog.open(build_file(tmp_path, 4000, seed=8))
        for ra, dec, r in [(30.0, 10.0, 6.0), (0.2, -0.5, 5.0),
                           (77.0, 89.0, 8.0)]:
            for ep in (2026.5, 1950.0):
                assert np.array_equal(cat.cone(ra, dec, r, epoch=ep),
                                      brute(cat, ra, dec, r, epoch=ep)), ep

    def test_positions_shift_with_epoch(self, tmp_path):
        cat = C.Catalog.open(build_file(tmp_path, 200, seed=3))
        r0, d0 = cat.positions_at(None)
        r1, d1 = cat.positions_at(2100.0)
        assert not np.allclose(d0, d1)
        assert np.median(C.angular_separation(r0, d0, r1, d1)) > 0

    def test_epoch_none_equals_stored(self, tmp_path):
        cat = C.Catalog.open(build_file(tmp_path, 50))
        r0, d0 = cat.positions_at(None)
        assert np.allclose(r0, cat.ra) and np.allclose(d0, cat.dec)

    def test_positions_at_subset(self, tmp_path):
        cat = C.Catalog.open(build_file(tmp_path, 100))
        idx = np.array([3, 17, 42])
        r, d = cat.positions_at(2020.0, idx)
        rf, df = cat.positions_at(2020.0)
        assert np.allclose(r, rf[idx]) and np.allclose(d, df[idx])

    def test_apply_pm_known_value(self):
        """1000 mas/yr × 10 yr = 10000 mas = 2.777... 角秒。"""
        ra, dec = C.apply_proper_motion([100.0], [0.0], [0.0], [1000.0], 10.0)
        assert dec[0] == pytest.approx(10000.0 / 3.6e6, rel=1e-9)
        assert ra[0] == pytest.approx(100.0)

    def test_apply_pm_ra_uses_cos_dec(self):
        """pmra 已含 cos(dec) 因子,还原成赤经增量要除回去。"""
        ra, _ = C.apply_proper_motion([10.0], [60.0], [3600.0], [0.0], 1.0)
        assert ra[0] - 10.0 == pytest.approx(3600.0 / 3.6e6 / math.cos(
            math.radians(60.0)), rel=1e-6)

    def test_apply_pm_at_pole_is_finite(self):
        """极点 cos(dec)→0 不能产生 inf/NaN 污染后续单位向量。"""
        ra, dec = C.apply_proper_motion([0.0, 0.0], [90.0, -90.0],
                                        [9999.0, 9999.0], [9999.0, -9999.0],
                                        100.0)
        assert np.all(np.isfinite(ra)) and np.all(np.isfinite(dec))
        assert np.all(np.abs(dec) <= 90.0)

    def test_apply_pm_zero_dt_is_identity(self):
        ra, dec = C.apply_proper_motion([1.0], [2.0], [500.0], [500.0], 0.0)
        assert ra[0] == 1.0 and dec[0] == 2.0

    def test_max_pm_mas(self, tmp_path):
        p = tmp_path / "pm.bin"
        C.write_catalog(p, [0, 1, 2], [0, 1, 2], [5, 5, 5],
                        [10, -900, 3], [4, 5, 120])
        assert C.Catalog.open(p).max_pm_mas == 900.0


# ---------------------------------------------------------------- 球面数学

class TestSpherical:
    def test_separation_known_values(self):
        assert C.angular_separation(0, 0, 0, 0) == pytest.approx(0.0)
        assert C.angular_separation(0, 0, 90, 0) == pytest.approx(90.0)
        assert C.angular_separation(0, -90, 0, 90) == pytest.approx(180.0)
        assert C.angular_separation(0, 0, 1, 0) == pytest.approx(1.0)

    def test_separation_shrinks_with_cos_dec(self):
        """同样的 ΔRA 在高赤纬上对应更小的角距。"""
        assert C.angular_separation(0, 60, 1, 60) == pytest.approx(
            math.degrees(2 * math.asin(math.sin(math.radians(0.5))
                                       * math.cos(math.radians(60)))), rel=1e-9)

    def test_separation_symmetric_and_broadcasts(self):
        ra, dec, *_ = synth(50, seed=4)
        a = C.angular_separation(10.0, 20.0, ra, dec)
        b = C.angular_separation(ra, dec, 10.0, 20.0)
        assert np.allclose(a, b)
        assert a.shape == (50,)

    def test_separation_small_angle_precision(self):
        """haversine 在毫角秒尺度上不能掉精度(acos 版会)。"""
        d = C.angular_separation(0.0, 0.0, 1e-7, 0.0)
        assert float(d) == pytest.approx(1e-7, rel=1e-6)

    def test_unit_vectors_are_unit(self):
        ra, dec, *_ = synth(200, seed=6)
        v = C.unit_vectors(ra, dec)
        assert np.allclose(np.linalg.norm(v, axis=1), 1.0)

    def test_unit_vector_axes(self):
        assert np.allclose(C.unit_vectors(0.0, 0.0), [1, 0, 0], atol=1e-12)
        assert np.allclose(C.unit_vectors(90.0, 0.0), [0, 1, 0], atol=1e-12)
        assert np.allclose(C.unit_vectors(0.0, 90.0), [0, 0, 1], atol=1e-12)

    def test_jyear_j2000(self):
        # J2000.0 = 2000-01-01T12:00:00 TT ≈ unix 946728000
        assert C.jyear_from_unix(946728000.0) == pytest.approx(2000.0, abs=1e-3)

    def test_jyear_advances_one_per_julian_year(self):
        t = 1.0e9
        assert C.jyear_from_unix(t + 365.25 * 86400.0) - C.jyear_from_unix(t) \
            == pytest.approx(1.0, rel=1e-9)


# ---------------------------------------------------------------- 下载器

class TestDownloader:
    @pytest.fixture(autouse=True)
    @staticmethod
    def _isolate_env(tmp_path, monkeypatch):
        """把这组测试与**开发机上真实存在的**星表环境变量隔离开。

        没有这层隔离时,开发者一旦设了 ASTRO_SMB_CATALOG_PATH 指向自己的真表,
        catalog_path() 就会绕过 tmp_path,整组下载器测试凭空变红。
        """
        monkeypatch.delenv("ASTRO_SMB_CATALOG_PATH", raising=False)
        monkeypatch.delenv("ASTRO_SMB_CATALOG_URL", raising=False)
        monkeypatch.setenv("ASTRO_SMB_CATALOG_DIR", str(tmp_path / "_iso"))

    def test_catalog_dir_env_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ASTRO_SMB_CATALOG_DIR", str(tmp_path / "cd"))
        assert C.catalog_dir() == tmp_path / "cd"
        assert C.catalog_path().name == C.CATALOG_FILENAME

    def test_catalog_path_env_override(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ASTRO_SMB_CATALOG_PATH", str(tmp_path / "my.bin"))
        assert C.catalog_path() == tmp_path / "my.bin"

    def test_ensure_uses_existing_valid_file(self, tmp_path, monkeypatch):
        p = build_file(tmp_path, 100, name="tycho2_v1.bin")
        monkeypatch.setenv("ASTRO_SMB_CATALOG_DIR", str(tmp_path))
        assert C.ensure_catalog(url="http://unused.invalid/x") == p

    def test_ensure_with_explicit_path_validates_only(self, tmp_path,
                                                      monkeypatch):
        p = build_file(tmp_path, 20, name="mine.bin")
        monkeypatch.setenv("ASTRO_SMB_CATALOG_PATH", str(p))
        assert C.ensure_catalog() == p

    def test_explicit_path_corrupt_is_not_deleted(self, tmp_path, monkeypatch):
        """用户显式指定的文件坏了要报错,**不能悄悄删掉重下**。"""
        p = build_file(tmp_path, 20, name="mine.bin")
        p.write_bytes(p.read_bytes()[:100])
        monkeypatch.setenv("ASTRO_SMB_CATALOG_PATH", str(p))
        with pytest.raises(C.CatalogError):
            C.ensure_catalog()
        assert p.exists()

    def test_explicit_path_missing_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ASTRO_SMB_CATALOG_PATH", str(tmp_path / "no.bin"))
        with pytest.raises(C.CatalogError, match="不存在"):
            C.ensure_catalog()

    def test_no_url_falls_back_to_upstream_build(self, tmp_path, monkeypatch):
        """**契约已变**:原来这里断言"没配 URL 就抛错" —— 那把死路当成了正确行为。
        打包镜像从没发布过,于是 GUI 的「下载星表」永远失败,板解算永远用不了。
        现在没有镜像就从 CDS I/259 取原始数据自己构建(权威上游,无再分发顾虑)。
        """
        monkeypatch.setenv("ASTRO_SMB_CATALOG_DIR", str(tmp_path))
        monkeypatch.delenv("ASTRO_SMB_CATALOG_PATH", raising=False)
        called = {}
        monkeypatch.setattr(
            C, "_build_from_upstream",
            lambda dest, **k: (called.setdefault("hit", True),
                               dest.write_bytes(b"x"), dest)[-1])
        assert C.ensure_catalog(url="") is not None
        assert called.get("hit") is True

    def test_download_from_file_url(self, tmp_path, monkeypatch):
        src = build_file(tmp_path, 300, name="src.bin")
        dest_dir = tmp_path / "cache"
        monkeypatch.setenv("ASTRO_SMB_CATALOG_DIR", str(dest_dir))
        monkeypatch.delenv("ASTRO_SMB_CATALOG_PATH", raising=False)
        seen = []
        got = C.ensure_catalog(progress=lambda d, t: seen.append(d),
                               url=src.as_uri())
        assert got == dest_dir / C.CATALOG_FILENAME
        assert C.Catalog.open(got).header.count == 300
        assert seen and seen[-1] == src.stat().st_size
        assert list(dest_dir.glob("*.part")) == []

    def test_download_rejects_truncated_and_leaves_no_part(self, tmp_path,
                                                           monkeypatch):
        """**绝不信 HTTP 200**:内容对不上账就丢弃,且不留 .part 孤儿。"""
        src = build_file(tmp_path, 300, name="src.bin")
        bad = tmp_path / "bad.bin"
        bad.write_bytes(src.read_bytes()[:64 + 14 * 100])   # 头说 300, 只有 100
        dest_dir = tmp_path / "cache"
        monkeypatch.setenv("ASTRO_SMB_CATALOG_DIR", str(dest_dir))
        monkeypatch.delenv("ASTRO_SMB_CATALOG_PATH", raising=False)
        with pytest.raises(C.CatalogError, match="长度不符|截断"):
            C.ensure_catalog(url=bad.as_uri())
        assert list(dest_dir.glob("*.part")) == []
        assert not (dest_dir / C.CATALOG_FILENAME).exists()

    def test_download_rejects_sha_mismatch(self, tmp_path, monkeypatch):
        src = build_file(tmp_path, 100, name="src.bin")
        dest_dir = tmp_path / "cache"
        monkeypatch.setenv("ASTRO_SMB_CATALOG_DIR", str(dest_dir))
        monkeypatch.delenv("ASTRO_SMB_CATALOG_PATH", raising=False)
        with pytest.raises(C.CatalogError, match="sha256"):
            C.ensure_catalog(url=src.as_uri(), expect_sha256="ab" * 32)
        assert not (dest_dir / C.CATALOG_FILENAME).exists()

    def test_download_rejects_count_mismatch(self, tmp_path, monkeypatch):
        src = build_file(tmp_path, 100, name="src.bin")
        dest_dir = tmp_path / "cache"
        monkeypatch.setenv("ASTRO_SMB_CATALOG_DIR", str(dest_dir))
        monkeypatch.delenv("ASTRO_SMB_CATALOG_PATH", raising=False)
        with pytest.raises(C.CatalogError, match="条目数不符"):
            C.ensure_catalog(url=src.as_uri(), expect_count=999)

    def test_corrupt_cache_is_replaced(self, tmp_path, monkeypatch):
        """缓存损坏时要能自愈重下,否则用户永远卡住(§7.5 的教训)。"""
        dest_dir = tmp_path / "cache"
        dest_dir.mkdir()
        (dest_dir / C.CATALOG_FILENAME).write_bytes(b"garbage" * 20)
        src = build_file(tmp_path, 77, name="src.bin")
        monkeypatch.setenv("ASTRO_SMB_CATALOG_DIR", str(dest_dir))
        monkeypatch.delenv("ASTRO_SMB_CATALOG_PATH", raising=False)
        got = C.ensure_catalog(url=src.as_uri())
        assert C.Catalog.open(got).header.count == 77

    def test_bad_url_raises_catalog_error(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ASTRO_SMB_CATALOG_DIR", str(tmp_path / "cache"))
        monkeypatch.delenv("ASTRO_SMB_CATALOG_PATH", raising=False)
        with pytest.raises(C.CatalogError):
            C.ensure_catalog(url=(tmp_path / "missing.bin").as_uri())

    def test_catalog_available(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ASTRO_SMB_CATALOG_DIR", str(tmp_path))
        monkeypatch.delenv("ASTRO_SMB_CATALOG_PATH", raising=False)
        assert C.catalog_available() is False
        build_file(tmp_path, 10, name=C.CATALOG_FILENAME)
        assert C.catalog_available() is True

    def test_curl_fallback_reports_progress(self, tmp_path, monkeypatch):
        import subprocess

        target = tmp_path / "curl.part"

        class Proc:
            returncode = None

            def __init__(self):
                self.polls = 0
                target.write_bytes(b"x" * 123)

            def poll(self):
                self.polls += 1
                if self.polls >= 2:
                    self.returncode = 0
                return self.returncode

            def communicate(self):
                return b"", b""

            def kill(self):
                self.returncode = -9

        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: Proc())
        monkeypatch.setattr(C.time, "sleep", lambda _seconds: None)
        seen = []
        C._download_curl("https://example.invalid/catalog", target,
                         lambda done, total: seen.append((done, total)), None)
        assert seen == [(123, 0)]

    def test_curl_fallback_can_cancel_while_running(self, tmp_path,
                                                    monkeypatch):
        import subprocess

        class Proc:
            returncode = None
            killed = False

            def poll(self):
                return self.returncode

            def communicate(self):
                return b"", b""

            def kill(self):
                self.killed = True
                self.returncode = -9

        class Cancel:
            def __init__(self):
                self.calls = 0

            def is_set(self):
                self.calls += 1
                return self.calls >= 2

        proc = Proc()
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: proc)
        with pytest.raises(InterruptedError, match="已取消"):
            C._download_curl("https://example.invalid/catalog",
                             tmp_path / "curl.part", None, Cancel())
        assert proc.killed


# ---------------------------------------------------------------- 构建器

def _tyc2_line(tyc1=1, tyc2=8, tyc3=1, pflag=" ", ram=2.31750494,
               dem=2.23184345, pmra=-16.3, pmde=-9.0, bt=12.146, vt=12.146,
               rao=2.31754222, deo=2.23186444) -> bytes:
    """按 ReadMe I/259 的字节位置拼一条合法记录(206 字符 + 换行)。"""
    buf = bytearray(b" " * 206)

    def put(a, b, s):
        s = s.rjust(b - a + 1)
        assert len(s) == b - a + 1, (a, b, s)
        buf[a - 1:b] = s.encode("ascii")

    put(1, 4, f"{tyc1:04d}")
    put(6, 10, f"{tyc2:05d}")
    put(12, 12, str(tyc3))
    put(14, 14, pflag)
    if ram is not None:
        put(16, 27, f"{ram:12.8f}")
        put(29, 40, f"{dem:12.8f}")
    if pmra is not None:
        put(42, 48, f"{pmra:7.1f}")
        put(50, 56, f"{pmde:7.1f}")
    if bt is not None:
        put(111, 116, f"{bt:6.3f}")
    if vt is not None:
        put(124, 129, f"{vt:6.3f}"[:6])
    put(153, 164, f"{rao:12.8f}")
    put(166, 177, f"{deo:12.8f}")
    return bytes(buf) + b"\n"


class TestBuilder:
    def test_parses_normal_record(self):
        d = B.parse_tyc2_bytes(_tyc2_line())
        assert d["ra"][0] == pytest.approx(2.31750494)
        assert d["dec"][0] == pytest.approx(2.23184345)
        assert d["pmra"][0] == pytest.approx(-16.3)
        assert d["pmde"][0] == pytest.approx(-9.0)

    def test_pflag_x_falls_back_to_observed_position(self):
        """'X' 条目没有均值位置/自行,必须退回观测位置(否则整条被丢掉)。"""
        d = B.parse_tyc2_bytes(_tyc2_line(pflag="X", ram=None, pmra=None,
                                          rao=123.5, deo=-45.25))
        assert d["ra"][0] == pytest.approx(123.5)
        assert d["dec"][0] == pytest.approx(-45.25)
        assert d["pmra"][0] == 0.0 and d["pmde"][0] == 0.0

    def test_johnson_v_from_bt_vt(self):
        v = B.johnson_v(np.array([12.0]), np.array([11.0]))
        assert v[0] == pytest.approx(11.0 - 0.090 * 1.0)

    def test_johnson_v_falls_back(self):
        assert B.johnson_v(np.array([np.nan]), np.array([9.5]))[0] == \
            pytest.approx(9.5)
        assert B.johnson_v(np.array([8.25]), np.array([np.nan]))[0] == \
            pytest.approx(8.25)

    def test_missing_both_magnitudes_is_nan(self):
        d = B.parse_tyc2_bytes(_tyc2_line(bt=None, vt=None))
        assert np.isnan(d["vmag"][0])

    def test_wrong_length_input_rejected(self):
        with pytest.raises(C.CatalogError, match="整数倍"):
            B.parse_tyc2_bytes(b"too short\n")

    def test_missing_newline_rejected(self):
        bad = bytearray(_tyc2_line())
        bad[-1] = ord("X")
        with pytest.raises(C.CatalogError, match="换行"):
            B.parse_tyc2_bytes(bytes(bad))

    def test_multi_record_block(self):
        raw = b"".join(_tyc2_line(tyc2=i, ram=float(i), dem=float(i % 80) - 40)
                       for i in range(1, 51))
        d = B.parse_tyc2_bytes(raw)
        assert d["ra"].size == 50

    def test_iter_sources_ignores_sidecar_files(self, tmp_path):
        """回归:``startswith('tyc2.dat')`` 会把 ``*.gz.done`` 这类 0 字节旁路
        文件也当成分片 —— 它们长度是 207 的整数倍(0),会静默贡献 0 条记录。"""
        (tmp_path / "tyc2.dat.00.gz").write_bytes(b"")
        (tmp_path / "tyc2.dat.01.gz").write_bytes(b"")
        (tmp_path / "tyc2.dat.00.gz.done").write_bytes(b"")
        (tmp_path / "tyc2.dat.bak").write_bytes(b"")
        (tmp_path / "README").write_bytes(b"")
        names = [p.name for p in B.iter_sources(tmp_path)]
        assert names == ["tyc2.dat.00.gz", "tyc2.dat.01.gz"]

    def test_iter_sources_accepts_plain_dat(self, tmp_path):
        (tmp_path / "tyc2.dat").write_bytes(b"")
        assert [p.name for p in B.iter_sources(tmp_path)] == ["tyc2.dat"]

    def test_iter_sources_errors(self, tmp_path):
        with pytest.raises(C.CatalogError, match="没有 tyc2.dat"):
            B.iter_sources(tmp_path)
        with pytest.raises(C.CatalogError, match="不存在"):
            B.iter_sources(tmp_path / "nope")

    def test_read_source_handles_gzip(self, tmp_path):
        p = tmp_path / "tyc2.dat.00.gz"
        with gzip.open(p, "wb") as fh:
            fh.write(_tyc2_line())
        assert B.read_source(p) == _tyc2_line()

    def test_build_end_to_end(self, tmp_path):
        src = tmp_path / "raw"
        src.mkdir()
        raw = b"".join(_tyc2_line(tyc2=i, ram=float(i * 3 % 360),
                                  dem=float(i % 100) - 50, vt=float(6 + i % 9))
                       for i in range(1, 121))
        (src / "tyc2.dat.00").write_bytes(raw)
        out = tmp_path / "built.bin"
        hdr = B.build(src, out)
        assert hdr.count == 120
        cat = C.Catalog.open(out)
        assert cat.verify_sorted()
        assert len(cat) == 120

    def test_build_max_mag_subset(self, tmp_path):
        src = tmp_path / "raw"
        src.mkdir()
        raw = b"".join(_tyc2_line(tyc2=i, ram=float(i), dem=float(i % 60) - 30,
                                  bt=float(5 + i % 10), vt=float(5 + i % 10))
                       for i in range(1, 101))
        (src / "tyc2.dat.00").write_bytes(raw)
        out = tmp_path / "bright.bin"
        hdr = B.build(src, out, max_mag=8.0)
        assert 0 < hdr.count < 100
        assert C.Catalog.open(out).vmag.max() <= 8.0

    def test_column_map_matches_readme(self):
        """列宽必须与 ReadMe 的字节区间一致(改这里前先核对 ReadMe)。"""
        assert B.TYC2_COLUMNS["RAmdeg"] == (16, 27)
        assert B.TYC2_COLUMNS["DEmdeg"] == (29, 40)
        assert B.TYC2_COLUMNS["RAdeg"] == (153, 164)
        assert B.TYC2_LINE_BYTES == 207
        assert len(B.TYC2_PARTS) == 20


# ---------------------------------------------------------------- 真表(可选)

_REAL = None
try:
    _p = C.catalog_path()
    if _p.is_file():
        C.validate_catalog_file(_p)
        _REAL = _p
except (C.CatalogError, OSError):
    _REAL = None


@pytest.mark.skipif(_REAL is None,
                    reason="本机未安装打包星表(设 ASTRO_SMB_CATALOG_PATH 后启用)")
class TestRealCatalog:
    """本机存在真表时才跑 —— 对 Tycho-2 的规模与密度做一次现实性检查。"""

    @pytest.fixture(scope="class")
    @staticmethod
    def cat():
        return C.Catalog.open(_REAL)

    def test_scale_and_invariants(self, cat):
        assert len(cat) > 2_000_000
        assert cat.verify_sorted()
        assert cat.header.epoch == 2000.0

    def test_field_density_supports_solving(self, cat):
        """ASI2600MC @ 400mm 视场(半径约 2°)内必须有足够多的星。"""
        for ra, dec in [(98.0017, 5.4006), (10.457, 40.613),
                        (275.271, -14.041)]:
            n = cat.cone(ra, dec, 2.0).size
            assert n > 200, (ra, dec, n)

    def test_cone_matches_brute_force(self, cat):
        ra, dec, r = 98.0017, 5.4006, 1.0
        assert np.array_equal(cat.cone(ra, dec, r), brute(cat, ra, dec, r))

    def test_tiny_cone_finds_the_star_itself(self, cat):
        for k in (0, 1_000_000, len(cat) - 1):
            got = cat.cone(float(cat.ra[k]), float(cat.dec[k]), 1e-5)
            assert k in got


# ------------------------------- 收尾:没有打包镜像时从上游 CDS 构建(不是死路)

class TestUpstreamBuildFallback:
    """`DEFAULT_CATALOG_URL` 一直是空的(打包镜像从没发布过),原来 ensure_catalog
    直接抛错 ⇒ GUI 上的「下载星表」是条死路,用户永远用不了板解算。
    这跟解算算法无关,纯粹是分发资产没发布造成的接线缺口。
    正确的默认路径是**从 CDS I/259 取原始数据自己构建** —— 那是权威上游,
    没有再分发的许可顾虑;打包镜像只是"省流量"的优化。
    """

    def test_no_url_falls_back_to_build_not_error(self, tmp_path, monkeypatch):
        called = {}
        def fake_build(dest, *, progress=None, cancel=None, expect_count=None):
            called["dest"] = dest
            dest.write_bytes(b"x")
            return dest
        monkeypatch.setattr(C, "CATALOG_URL", "")
        monkeypatch.setattr(C, "_build_from_upstream", fake_build)
        monkeypatch.setattr(C, "catalog_path", lambda: tmp_path / "c.bin")
        monkeypatch.delenv("ASTRO_SMB_CATALOG_PATH", raising=False)
        out = C.ensure_catalog()
        assert out == tmp_path / "c.bin" and called["dest"] == out

    def test_explicit_url_still_wins(self, tmp_path, monkeypatch):
        """配了镜像就走镜像 —— 回退不能把它抢掉。"""
        hit = {}
        monkeypatch.setattr(C, "_build_from_upstream",
                            lambda *a, **k: hit.setdefault("build", True))
        monkeypatch.setattr(C, "catalog_path", lambda: tmp_path / "c.bin")
        monkeypatch.delenv("ASTRO_SMB_CATALOG_PATH", raising=False)
        def fake_dl(src, tmp, progress, cancel):
            hit["url"] = src
            tmp.write_bytes(b"y")
        monkeypatch.setattr(C, "_download_urllib", fake_dl)
        monkeypatch.setattr(C, "validate_catalog_file",
                            lambda *a, **k: None)
        C.ensure_catalog(url="https://example.invalid/t.bin")
        assert hit.get("url") == "https://example.invalid/t.bin"
        assert "build" not in hit

    def test_explicit_path_missing_still_raises(self, tmp_path, monkeypatch):
        """用户显式指定了 ASTRO_SMB_CATALOG_PATH 但文件不在 —— 不该悄悄去下,
        那会把他指定的位置和我们的缓存位置搞混。"""
        monkeypatch.setenv("ASTRO_SMB_CATALOG_PATH", str(tmp_path / "nope.bin"))
        monkeypatch.setattr(C, "catalog_path", lambda: tmp_path / "nope.bin")
        with pytest.raises(C.CatalogError):
            C.ensure_catalog()

    def test_build_cleans_up_on_failure(self, tmp_path, monkeypatch):
        """构建失败不留半截 .part(§7.5 的教训)。"""
        monkeypatch.setattr(B, "download_parts",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("断网")))
        dest = tmp_path / "c.bin"
        with pytest.raises(OSError):
            C._build_from_upstream(dest)
        assert not dest.exists()
        assert not dest.with_name(dest.name + ".part").exists()

    def test_build_reports_progress_in_two_phases(self, tmp_path, monkeypatch):
        """下载占 0~85%,构建占 85~100%,**单位是字节**。

        这条测试原来是**假绿**的,值得留个记号:它的替身调的是
        ``progress(0, 100)`` / ``progress(100, 100)`` —— 两个参数、百分比,
        而真正的 `download_parts` 调的是
        ``progress(i + 1, len(parts), name, cached)`` —— **四个参数、分片序号**。

        于是:测试一直绿,而 `ensure_catalog` 在真机上**第一次回调就
        TypeError**,星表在任何前端上都下不下来(用户在新机器上撞见的
        就是它)。**替身的形状不是真实生产者的形状,等于没测。**

        现在替身按真实形状调,断言也跟着改成字节 —— 两套前端渲染的是
        ``{done/(1<<20):.0f} MB``,分片序号透传过去会写成"1/20 MB"。
        """
        seen = []

        def fake_dl(dest_dir, parts=None, progress=None, on_bytes=None):
            # **照 `download_parts` 的真实调用形状。** 两个回调都要有:
            # `progress` 一个分片响一次,`on_bytes` 是下载过程中
            # 每 0.25 秒一次的真字节数(界面上数字动不动靠它)。
            on_bytes(1_000_000)
            progress(0, 20, "tyc2.dat.00.gz", False)
            on_bytes(80_000_000)
            progress(20, 20, "tyc2.dat.19.gz", False)
            return []

        class _H:
            count = 7
        monkeypatch.setattr(B, "download_parts", fake_dl)
        monkeypatch.setattr(B, "build",
                            lambda src, out: (out.write_bytes(b"z"), _H())[1])
        monkeypatch.setattr(C, "validate_catalog_file", lambda *a, **k: None)
        C._build_from_upstream(tmp_path / "c.bin",
                                     progress=lambda d, t: seen.append((d, t)))
        total = C.UPSTREAM_BYTES
        # 第一下不再是恰好 0 —— 现在下载**过程中**就按真字节数报,
        # 而那一刻盘上已经有东西了。要的是"从很小开始、单调不倒退"。
        assert 0 <= seen[0][0] < total * 0.1, seen[0]
        assert seen[-1] == (total, total)
        assert seen == sorted(seen), f"进度倒退了: {seen}"
        assert any(d == int(total * 0.85) for d, _t in seen), (
            "要有下载→构建的分段切换")
        assert all(t == total for _d, t in seen), "分母中途换了单位"

    def test_build_count_mismatch_is_refused(self, tmp_path, monkeypatch):
        class _H:
            count = 42
        monkeypatch.setattr(B, "download_parts", lambda *a, **k: [])
        monkeypatch.setattr(B, "build",
                            lambda src, out: (out.write_bytes(b"z"), _H())[1])
        with pytest.raises(C.CatalogError):
            C._build_from_upstream(tmp_path / "c.bin",
                                         expect_count=2_539_913)
        assert not (tmp_path / "c.bin").exists()
