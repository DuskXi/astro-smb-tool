"""astro_smb.fitsimage 的离线单测:合成 FITS 字节流,不碰网络也不碰缓存目录。

重点钉死三件真机上最容易错、错了又不报错(只是图看着不对)的事:

1. **BZERO 还原** —— BITPIX 16 是有符号,ASIAIR 写 BZERO=32768 当无符号用;
2. **Bayer 相位** —— XBAYROFF/YBAYROFF 偏移 + ROWORDER 翻转都会改相位,
   且顺序是「先 offset 后翻转」;
3. **MTF/STF 数学** —— 边界、单调、PixInsight 口径的 c0/m2 公式。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np
import pytest

from astro_smb import fitsimage as fi
from astro_smb.fitshdr import parse_fits_header
from tests.support import tr

# ----------------------------------------------------------------- 合成工具


def _card(key: str, value) -> bytes:
    """一张 80 字节 FITS 卡片。"""
    if isinstance(value, bool):
        v = "T" if value else "F"
    elif isinstance(value, str):
        v = "'%s'" % value.ljust(8)
    else:
        v = str(value)
    return f"{key:<8}= {v:>20}".ljust(80)[:80].encode("ascii")


def make_fits(width: int, height: int, *, bitpix: int = 16, planes: int = 1,
              extra=None, data=None, end: bool = True, truncate: int = 0) -> bytes:
    """造一份合成 FITS 字节流。

    ``data`` 是**存储值**(未反 BSCALE/BZERO 的原始整数/浮点),按 bitpix
    对应的 big-endian dtype 写盘。``end=False`` 造"没有 END 卡"的坏头,
    ``truncate=N`` 从尾部砍掉 N 字节造截断数据区。
    """
    cards = [("SIMPLE", True), ("BITPIX", bitpix),
             ("NAXIS", 3 if planes > 1 else 2),
             ("NAXIS1", width), ("NAXIS2", height)]
    if planes > 1:
        cards.append(("NAXIS3", planes))
    items = extra.items() if isinstance(extra, dict) else (extra or [])
    cards.extend(items)
    buf = b"".join(_card(k, v) for k, v in cards)
    if end:
        buf += b"END" + b" " * 77
    buf += b" " * ((-len(buf)) % 2880)
    if data is not None:
        buf += np.asarray(data).astype(fi._DTYPES[bitpix]).tobytes()
    return buf[:-truncate] if truncate else buf


def header_of(blob: bytes):
    return parse_fits_header(blob)


def cfa(pattern: str, h: int, w: int, dtype=np.uint16) -> np.ndarray:
    """按相位造 CFA 阵列:R 位 = 1000+idx,G 位 = 2000+idx,B 位 = 3000+idx,
    便于逐通道断言。"""
    base = {"R": 1000, "G": 2000, "B": 3000}
    p = pattern.upper()
    a = np.zeros((h, w), dtype=dtype)
    idx = 0
    for r in range(h):
        for c in range(w):
            a[r, c] = base[p[(r % 2) * 2 + (c % 2)]] + idx
            idx += 1
    return a


def plane_of(a: np.ndarray, pattern: str, color: str) -> np.ndarray:
    r, c = fi.bayer_positions(pattern)[color][0]
    return a[r::2, c::2]


def R_of(a, pattern):
    return plane_of(a, pattern, "R")


def B_of(a, pattern):
    return plane_of(a, pattern, "B")


def synth_sky(h: int = 256, w: int = 256, bg: float = 0.02,
              sigma: float = 0.002, seed: int = 7) -> np.ndarray:
    """float32 合成天区:高斯背景 + 稀疏亮星(0.9)。给 STF 输出分布做断言。"""
    rng = np.random.default_rng(seed)
    img = rng.normal(bg, sigma, size=(h, w)).astype(np.float32)
    n_star = max(1, h * w // 2000)
    ys = rng.integers(0, h, n_star)
    xs = rng.integers(0, w, n_star)
    img[ys, xs] = 0.9
    return np.clip(img, 0.0, 1.0)


# ----------------------------------------------------------------- 几何


class TestGeometry:
    def test_basic(self):
        blob = make_fits(6248, 4176, extra=[("BZERO", 32768), ("BSCALE", 1),
                                            ("BAYERPAT", "RGGB")])
        g = fi.geometry_from_header(header_of(blob))
        assert (g.width, g.height, g.planes) == (6248, 4176, 1)
        assert g.bitpix == 16
        assert g.bscale == 1.0 and g.bzero == 32768.0
        assert g.data_offset == 2880
        assert g.data_bytes == 6248 * 4176 * 2
        assert not g.is_color_cube

    def test_incomplete_header_raises(self):
        blob = make_fits(4, 4, end=False)
        with pytest.raises(fi.FitsImageError, match="不完整"):
            fi.geometry_from_header(header_of(blob))

    def test_bad_bitpix_raises(self):
        blob = make_fits(4, 4, bitpix=16)
        hdr = header_of(blob)
        hdr.cards["BITPIX"] = "24"
        with pytest.raises(fi.FitsImageError):
            fi.geometry_from_header(hdr)

    def test_naxis3_color(self):
        blob = make_fits(8, 6, planes=3, extra=[("BAYERPAT", "RGGB")])
        g = fi.geometry_from_header(header_of(blob))
        assert g.planes == 3
        assert g.is_color_cube
        assert g.bayer_effective is None      # 已经是 RGB,不能再去马赛克

    def test_naxis3_one_is_mono(self):
        blob = make_fits(8, 6, planes=1)
        g = fi.geometry_from_header(header_of(blob))
        assert g.planes == 1
        assert not g.is_color_cube


# ----------------------------------------------------------------- Bayer


class TestBayerParse:
    def test_normalize(self):
        assert fi.normalize_bayer("rggb  ") == "RGGB"
        assert fi.normalize_bayer("RGGB") == "RGGB"
        assert fi.normalize_bayer(None) is None
        assert fi.normalize_bayer("CYYM") is None


class TestBayerPhase:
    def test_shift_identity(self):
        for p in fi.BAYER_PATTERNS:
            assert fi.bayer_shift(p, 0, 0) == p
            assert fi.bayer_shift(p, 2, 2) == p

    def test_shift_table(self):
        assert fi.bayer_shift("RGGB", 1, 0) == "GRBG"
        assert fi.bayer_shift("RGGB", 0, 1) == "GBRG"
        assert fi.bayer_shift("RGGB", 1, 1) == "BGGR"

    def test_shift_negative(self):
        for p in fi.BAYER_PATTERNS:
            assert fi.bayer_shift(p, -1, -1) == fi.bayer_shift(p, 1, 1)

    def test_vflip_even_table(self):
        h = 4176
        assert fi.bayer_after_vflip("RGGB", h) == "GBRG"
        assert fi.bayer_after_vflip("BGGR", h) == "GRBG"
        assert fi.bayer_after_vflip("GRBG", h) == "BGGR"
        assert fi.bayer_after_vflip("GBRG", h) == "RGGB"

    def test_vflip_odd_is_noop(self):
        for p in fi.BAYER_PATTERNS:
            assert fi.bayer_after_vflip(p, 4175) == p

    def test_vflip_involution(self):
        for p in fi.BAYER_PATTERNS:
            assert fi.bayer_after_vflip(fi.bayer_after_vflip(p, 6), 6) == p

    def test_vflip_consistent_with_data(self):
        """相位换算必须和真的翻转数据一致 —— 这条错了整幅图会红蓝互换。"""
        for p in fi.BAYER_PATTERNS:
            a = cfa(p, 6, 6)
            q = fi.bayer_after_vflip(p, 6)
            assert np.array_equal(R_of(a, p)[::-1], R_of(a[::-1], q))
            assert np.array_equal(B_of(a, p)[::-1], B_of(a[::-1], q))
            # 三通道整体(含绿的两位平均)也要对得上
            assert np.array_equal(fi.debayer_superpixel(a, p)[::-1],
                                  fi.debayer_superpixel(np.ascontiguousarray(a[::-1]), q))

    def test_hflip_table(self):
        assert fi.bayer_after_hflip("RGGB", 6248) == "GRBG"
        assert fi.bayer_after_hflip("BGGR", 6248) == "GBRG"
        for p in fi.BAYER_PATTERNS:
            assert fi.bayer_after_hflip(p, 5) == p


class TestRowOrder:
    def test_topdown_no_flip(self):
        assert fi.roworder_needs_flip("TOP-DOWN") is False

    def test_bottomup_and_missing_flip(self):
        for v in ("BOTTOM-UP", None, "", "bottom_up", "什么鬼"):
            assert fi.roworder_needs_flip(v) is True

    def test_effective_pattern_pipeline(self):
        """先 offset 后翻转 —— 顺序反了结果就不同,这条把顺序钉死。"""
        blob = make_fits(4, 4, extra=[("BAYERPAT", "RGGB"),
                                      ("XBAYROFF", 1), ("YBAYROFF", 0)])
        g = fi.geometry_from_header(header_of(blob))
        assert g.flip_vertical is True            # 缺 ROWORDER = 自底向上
        expect = fi.bayer_after_vflip(fi.bayer_shift("RGGB", 1, 0), 4)
        assert expect == "BGGR"
        assert g.bayer_effective == "BGGR"


# ----------------------------------------------------------------- 解码


class TestDecode:
    def _decode(self, blob):
        return fi.decode_pixels(blob, fi.geometry_from_header(header_of(blob)))

    def test_bzero_roundtrip(self):
        """ASIAIR 主路径:物理值 0..65535 写成 int16 + BZERO=32768。"""
        vals = np.array([[0, 1, 32767], [32768, 65535, 12345]], dtype=np.int64)
        stored = (vals - 32768).astype(np.int16)
        blob = make_fits(3, 2, extra=[("BZERO", 32768), ("BSCALE", 1),
                                      ("ROWORDER", "TOP-DOWN")], data=stored)
        out = self._decode(blob)
        assert out.dtype == np.uint16
        assert np.array_equal(out, vals.astype(np.uint16))

    def test_bzero_extremes(self):
        stored = np.array([[-32768, -1, 0, 1, 32767]], dtype=np.int16)
        blob = make_fits(5, 1, extra=[("BZERO", 32768), ("BSCALE", 1),
                                      ("ROWORDER", "TOP-DOWN")], data=stored)
        out = self._decode(blob)
        assert out.dtype == np.uint16
        assert list(out[0]) == [0, 32767, 32768, 32769, 65535]

    def test_no_bzero_stays_signed_float(self):
        stored = np.array([[-5, 0, 7]], dtype=np.int16)
        blob = make_fits(3, 1, extra=[("ROWORDER", "TOP-DOWN")], data=stored)
        out = self._decode(blob)
        assert out.dtype == np.float32
        assert np.allclose(out[0], [-5.0, 0.0, 7.0])

    def test_float32_plane(self):
        stored = np.array([[0.0, 0.5, 1.0]], dtype=np.float32)
        blob = make_fits(3, 1, bitpix=-32, extra=[("ROWORDER", "TOP-DOWN")],
                         data=stored)
        out = self._decode(blob)
        assert out.dtype == np.float32
        assert np.allclose(out[0], [0.0, 0.5, 1.0])

    def test_bscale_applied(self):
        stored = np.array([[1, 2, 3]], dtype=np.int16)
        blob = make_fits(3, 1, extra=[("BSCALE", 2), ("BZERO", 100),
                                      ("ROWORDER", "TOP-DOWN")], data=stored)
        out = self._decode(blob)
        assert np.allclose(out[0], [102.0, 104.0, 106.0])

    def test_flip_applied(self):
        stored = np.arange(16, dtype=np.int16).reshape(4, 4)
        blob = make_fits(4, 4, data=stored)      # 缺 ROWORDER → 自底向上 → 翻
        out = self._decode(blob)
        assert np.allclose(out, stored[::-1].astype(np.float32))

    def test_truncated_raises(self):
        stored = np.arange(16, dtype=np.int16).reshape(4, 4)
        blob = make_fits(4, 4, data=stored, truncate=10)
        with pytest.raises(fi.FitsImageError):
            self._decode(blob)

    def test_truncated_file_raises(self, tmp_path):
        stored = np.arange(16, dtype=np.int16).reshape(4, 4)
        blob = make_fits(4, 4, data=stored, truncate=10)
        p = tmp_path / "cut.fit"
        p.write_bytes(blob)
        g = fi.geometry_from_header(header_of(blob))
        with pytest.raises(fi.FitsImageError):
            fi.decode_pixels(p, g)

    def test_cube_axis_order(self):
        stored = np.empty((3, 4, 5), dtype=np.int16)
        for k in range(3):
            stored[k] = k + 1
        blob = make_fits(5, 4, planes=3, extra=[("ROWORDER", "TOP-DOWN")],
                         data=stored)
        out = self._decode(blob)
        assert out.shape == (4, 5, 3)
        for k in range(3):
            assert np.all(out[..., k] == k + 1)


# ----------------------------------------------------------------- 去马赛克


class TestDebayer:
    def test_four_patterns(self):
        for p in fi.BAYER_PATTERNS:
            a = cfa(p, 4, 4)
            out = fi.debayer_superpixel(a, p)
            assert out.shape == (2, 2, 3)
            assert np.array_equal(out[..., 0], R_of(a, p))
            assert np.array_equal(out[..., 2], B_of(a, p))
            (r1, c1), (r2, c2) = fi.bayer_positions(p)["G"]
            g_expect = (a[r1::2, c1::2].astype(np.uint32)
                        + a[r2::2, c2::2].astype(np.uint32)) // 2
            assert np.array_equal(out[..., 1], g_expect.astype(a.dtype))

    def test_green_average_no_overflow(self):
        a = np.zeros((2, 2), dtype=np.uint16)
        a[0, 0], a[1, 1] = 100, 200            # R, B
        a[0, 1] = a[1, 0] = 65535              # 两个绿位都顶格
        out = fi.debayer_superpixel(a, "RGGB")
        assert out[0, 0, 1] == 65535           # uint16 直接相加会退化成 65534//2

    def test_odd_size_cropped(self):
        a = cfa("RGGB", 5, 5)
        out = fi.debayer_superpixel(a, "RGGB")
        assert out.shape == (2, 2, 3)

    def test_dtype_preserved(self):
        for dt in (np.uint16, np.float32):
            a = cfa("RGGB", 4, 4, dtype=dt)
            assert fi.debayer_superpixel(a, "RGGB").dtype == dt

    def test_bad_pattern_raises(self):
        with pytest.raises(fi.FitsImageError):
            fi.debayer_superpixel(cfa("RGGB", 4, 4), "XXXX")


# ----------------------------------------------------------------- MTF


_MS = (0.05, 0.25, 0.5, 0.75, 0.95)


class TestMTF:
    def test_boundaries(self):
        for m in _MS:
            assert float(fi.mtf(m, 0.0)) == pytest.approx(0.0, abs=1e-7)
            assert float(fi.mtf(m, 1.0)) == pytest.approx(1.0, abs=1e-6)

    def test_identity_at_half(self):
        x = np.linspace(0.0, 1.0, 11, dtype=np.float32)
        assert np.allclose(fi.mtf(0.5, x), x, atol=1e-6)

    def test_half_point_identity(self):
        for m in _MS:
            assert float(fi.mtf(m, 0.5)) == pytest.approx(1.0 - m, abs=1e-6)

    def test_monotonic(self):
        x = np.linspace(0.0, 1.0, 1001, dtype=np.float32)
        for m in _MS:
            y = fi.mtf(m, x)
            assert np.all(np.diff(y) >= -1e-12)

    def test_degenerate_m(self):
        x = np.linspace(0.0, 1.0, 21, dtype=np.float32)
        for m in (0.0, 1.0, -1.0, 2.0):
            y = fi.mtf(m, x)
            assert np.all(y >= 0.0) and np.all(y <= 1.0)


# ----------------------------------------------------------------- STF


class TestSTF:
    def test_madn_on_normal(self):
        rng = np.random.default_rng(1)
        assert fi.madn(rng.normal(0.0, 1.0, 200000)) == pytest.approx(1.0, rel=0.02)

    def test_madn_constant_is_zero(self):
        assert fi.madn(np.full(1000, 5.0, dtype=np.float32)) == 0.0

    def test_stats_formula(self):
        img = synth_sky()
        cs = fi.stf_stats(img)[0]
        flat = img.reshape(-1).astype(np.float32)
        med = float(np.median(flat))
        mad = fi.madn(flat)
        assert cs.median == pytest.approx(med, rel=1e-6)
        assert cs.madn == pytest.approx(mad, rel=1e-6)
        assert cs.c0 == pytest.approx(float(np.clip(med - 2.80 * mad, 0.0, 1.0)),
                                      rel=1e-6, abs=1e-9)
        assert cs.m2 == pytest.approx(float(fi.mtf(0.25, med - cs.c0)),
                                      rel=1e-6, abs=1e-9)

    def test_output_median_near_target(self):
        rgb = np.stack([synth_sky(seed=s) for s in (1, 2, 3)], axis=-1)
        stats = fi.stf_stats(rgb)
        for c in range(3):
            out = fi.apply_stf(rgb[..., c], stats[c])
            assert float(np.median(out)) == pytest.approx(0.25, abs=0.02)

    def test_unlinked_balances_channels(self):
        base = synth_sky(seed=11)
        rgb = np.stack([base * k for k in (1.0, 0.6, 0.4)], axis=-1).astype(np.float32)
        un = fi.stf_stats(rgb, linked=False)
        meds = [float(np.median(fi.apply_stf(rgb[..., c], un[c]))) for c in range(3)]
        assert max(meds) - min(meds) < 0.01
        li = fi.stf_stats(rgb, linked=True)
        meds_l = [float(np.median(fi.apply_stf(rgb[..., c], li[c]))) for c in range(3)]
        assert max(meds_l) - min(meds_l) > 0.03

    def test_sampling_matches_full(self):
        img = synth_sky(1000, 1000, seed=5)
        a = fi.stf_stats(img)[0]
        b = fi.stf_stats(img, max_samples=None)[0]
        assert a.c0 == pytest.approx(b.c0, rel=0.01, abs=1e-4)
        assert a.m2 == pytest.approx(b.m2, rel=0.01, abs=1e-4)

    def test_compute_stats_keeps_channels_apart(self):
        """(N, C) 抽样矩阵必须按通道分开统计 —— 合并了会让"不链接"静默失效。"""
        base = synth_sky(seed=13).reshape(-1)
        sample = np.stack([base * k for k in (1.0, 0.6, 0.4)], axis=-1).astype(np.float32)
        st = fi.compute_stats(sample, fi.StretchParams(linked=False))
        assert len(st) == 3
        assert st[0].median > st[1].median > st[2].median
        assert st[0].lo >= st[1].lo >= st[2].lo
        lk = fi.compute_stats(sample, fi.StretchParams(linked=True))
        assert lk[0].median == lk[1].median == lk[2].median

    def test_compute_stats_matches_stf_stats(self):
        rgb = np.stack([synth_sky(seed=s) for s in (4, 5, 6)], axis=-1)
        sample = fi.sample_unit(rgb, fi.UnitScale(0.0, 1.0))
        a = fi.compute_stats(sample, fi.StretchParams())
        b = fi.stf_stats(rgb.reshape(-1, 3)[:, None, :], max_samples=None)
        for x, y in zip(a, b):
            assert x.c0 == pytest.approx(y.c0, rel=1e-6, abs=1e-9)
            assert x.m2 == pytest.approx(y.m2, rel=1e-6, abs=1e-9)

    def test_stretch_unlinked_differs_per_channel(self):
        """整条 stretch 通路上"不链接"必须真的按通道各算各的。"""
        rng = np.random.default_rng(21)
        mono = rng.integers(2000, 6000, (64, 64)).astype(np.uint16)
        rgb = np.stack([mono, (mono * 0.6).astype(np.uint16),
                        (mono * 0.4).astype(np.uint16)], axis=-1)
        un, _ = fi.stretch(rgb, fi.StretchParams(linked=False))
        meds = [float(np.median(un[..., c])) for c in range(3)]
        assert max(meds) - min(meds) <= 3          # 各通道被拉到同一背景
        li, _ = fi.stretch(rgb, fi.StretchParams(linked=True))
        meds_l = [float(np.median(li[..., c])) for c in range(3)]
        assert max(meds_l) - min(meds_l) > 10      # 链接则保留通道比例

    def test_output_range(self):
        img = synth_sky(seed=9)
        cs = fi.stf_stats(img)[0]
        out = fi.apply_stf(img, cs)
        assert float(out.min()) >= 0.0 and float(out.max()) <= 1.0


# ----------------------------------------------------------------- asinh / 百分位


class TestAsinh:
    def test_boundaries_and_mono(self):
        x = np.linspace(0.0, 1.0, 501, dtype=np.float32)
        for a in (1.0, 10.0, 100.0, 1000.0):
            y = fi.asinh_stretch(x, a)
            assert float(y[0]) == pytest.approx(0.0, abs=1e-7)
            assert float(y[-1]) == pytest.approx(1.0, abs=1e-6)
            assert np.all(np.diff(y) >= -1e-7)

    def test_a_zero_is_identity(self):
        x = np.linspace(0.0, 1.0, 11, dtype=np.float32)
        assert np.allclose(fi.asinh_stretch(x, 0.0), x)
        assert np.allclose(fi.asinh_stretch(x, -1.0), x)


class TestPercentile:
    def test_matches_manual(self):
        a = np.linspace(0.0, 1.0, 1000, dtype=np.float32)
        out = fi.percentile_stretch(a, 10.0, 90.0)
        lo, hi = np.percentile(a, (10.0, 90.0))
        assert np.allclose(out, np.clip((a - lo) / (hi - lo), 0, 1), atol=1e-6)

    def test_flat_image_no_div_zero(self):
        a = np.full((8, 8), 0.3, dtype=np.float32)
        out = fi.percentile_stretch(a)
        assert np.all(np.isfinite(out))

    def test_bounds_clip(self):
        a = synth_sky(32, 32)
        out = fi.percentile_stretch(a, 99.0, 1.0)      # 上下限写反
        assert float(out.min()) >= 0.0 and float(out.max()) <= 1.0


# ----------------------------------------------------------------- stretch 驱动


class TestStretch:
    def test_uint8_output(self):
        rgb = (np.random.default_rng(3).integers(0, 65535, (8, 6, 3))
               ).astype(np.uint16)
        out, stats = fi.stretch(rgb, fi.StretchParams())
        assert out.dtype == np.uint8 and out.shape == (8, 6, 3)
        assert len(stats) == 3

    def test_mono_broadcast(self):
        mono = (np.random.default_rng(4).integers(0, 65535, (5, 7, 1))
                ).astype(np.uint16)
        out, stats = fi.stretch(mono, fi.StretchParams())
        assert out.shape == (5, 7, 3)
        assert np.array_equal(out[..., 0], out[..., 1])
        assert np.array_equal(out[..., 1], out[..., 2])
        assert len(stats) == 3

    def test_float_path_matches_lut(self):
        """浮点通路与 LUT 通路的结果必须一致(否则换模式会跳色)。"""
        u16 = (np.random.default_rng(6).integers(0, 65535, (16, 16, 3))
               ).astype(np.uint16)
        p = fi.StretchParams()
        lut_out, stats = fi.stretch(u16, p)
        flt = u16.astype(np.float32) / 65535.0
        flt_out, _ = fi.stretch(flt, p, unit=fi.UnitScale(0.0, 1.0), stats=stats)
        assert np.max(np.abs(lut_out.astype(np.int16)
                             - flt_out.astype(np.int16))) <= 1

    def test_all_modes_run(self):
        rgb = (np.random.default_rng(8).integers(0, 65535, (8, 8, 3))
               ).astype(np.uint16)
        for mode in ("stf", "asinh", "percentile"):
            out, stats = fi.stretch(rgb, fi.StretchParams(mode=mode))
            assert out.dtype == np.uint8 and len(stats) == 3

    def test_fingerprint_stability(self):
        a = fi.StretchParams(mode="stf", asinh_a=10.0)
        b = fi.StretchParams(mode="stf", asinh_a=1000.0)
        assert a.fingerprint() == b.fingerprint()   # 只把当前 mode 用到的进指纹
        c = fi.StretchParams(mode="stf", target_background=0.3)
        assert a.fingerprint() != c.fingerprint()
        d = fi.StretchParams(mode="asinh", asinh_a=10.0)
        e = fi.StretchParams(mode="asinh", asinh_a=1000.0)
        assert d.fingerprint() != e.fingerprint()


# ----------------------------------------------------------------- 直方图


class TestHistogram:
    def test_bincount_matches_numpy(self):
        a = (np.random.default_rng(2).integers(0, 65536, 50000)).astype(np.uint16)
        mine = fi.histogram_u16(a, 256)
        ref, _ = np.histogram(a, bins=256, range=(0, 65536))
        assert np.array_equal(mine, ref)

    def test_unit_histogram_total(self):
        x = synth_sky(64, 64)
        h = fi.histogram_unit(x, 128)
        assert int(h.sum()) == x.size and h.shape == (128,)


# ----------------------------------------------------------------- 端到端


class TestPreviewCacheHelpers:
    """preview.py 里被抽成模块级、供 FITS 查看器复用的两个缓存原语。"""

    def _entry(self, size=100, mtime=1700000000.5):
        from astro_smb.client import RemoteEntry
        return RemoteEntry(share="EMMC Images", path="Autorun\\Bias\\a.fit",
                           name="a.fit", is_dir=False, size=size, mtime=mtime,
                           ctime=0.0, atime=0.0, attributes=0x20)

    def test_cache_key_varies(self):
        from astro_smb_gui.preview import cache_key
        e = self._entry()
        k = cache_key("h1", e)
        assert k == cache_key("h1", e)                    # 稳定
        assert k != cache_key("h2", e)                    # 换设备
        assert k != cache_key("h1", e, "fitsview")        # 换用途
        assert k != cache_key("h1", self._entry(size=101))  # 换大小
        assert k != cache_key("h1", self._entry(mtime=1.0))  # 换时间

    def test_download_cached_atomic(self, tmp_path):
        from astro_smb_gui.preview import download_cached

        class FakeClient:
            def __init__(self, boom=False):
                self.boom = boom
                self.calls = []

            def download_file(self, share, path, local, progress=None, cancel=None):
                self.calls.append(Path(local).name)
                Path(local).write_bytes(b"half")
                if self.boom:
                    raise RuntimeError("断线")
                Path(local).write_bytes(b"whole")

        dest = tmp_path / "x.fit"
        bad = FakeClient(boom=True)
        with pytest.raises(RuntimeError):
            download_cached(bad, "s", "p", dest, None, tmp_suffix=".fvpart")
        assert not dest.exists()                          # 半截文件不能留下
        assert not list(tmp_path.glob("x.fit.fvpart*"))
        # 临时后缀可定制,后面再缀一个进程内唯一序号
        assert len(bad.calls) == 1 and bad.calls[0].startswith("x.fit.fvpart")

        ok = FakeClient()
        download_cached(ok, "s", "p", dest, None)
        assert dest.read_bytes() == b"whole"
        assert len(ok.calls) == 1 and ok.calls[0].startswith("x.fit.part")
        download_cached(ok, "s", "p", dest, None)         # 已存在直接跳过
        assert len(ok.calls) == 1


class TestPipeline:
    def _blob(self, roworder: str) -> tuple[bytes, np.ndarray]:
        stored_phys = cfa("RGGB", 8, 8).astype(np.int64)
        stored = (stored_phys - 32768).astype(np.int16)
        blob = make_fits(8, 8, extra=[("BZERO", 32768), ("BSCALE", 1),
                                      ("BAYERPAT", "RGGB"),
                                      ("ROWORDER", roworder)], data=stored)
        return blob, stored_phys.astype(np.uint16)

    def test_end_to_end_rggb(self, tmp_path):
        blob, phys = self._blob("TOP-DOWN")
        p = tmp_path / "m8.fit"
        p.write_bytes(blob)
        img = fi.load_linear(p, header_of(blob))
        assert img.rgb.shape == (4, 4, 3)
        assert img.debayered is True
        assert np.array_equal(img.rgb[..., 0], R_of(phys, "RGGB"))
        assert np.array_equal(img.rgb[..., 2], B_of(phys, "RGGB"))
        (r1, c1), (r2, c2) = fi.bayer_positions("RGGB")["G"]
        g = (phys[r1::2, c1::2].astype(np.uint32)
             + phys[r2::2, c2::2].astype(np.uint32)) // 2
        assert np.array_equal(img.rgb[..., 1], g.astype(np.uint16))
        out, stats = fi.stretch(img.rgb, fi.StretchParams(), unit=img.unit)
        assert out.dtype == np.uint8 and out.shape == (4, 4, 3)
        assert len(stats) == 3

    def test_end_to_end_bottomup_phase(self, tmp_path):
        """整条「翻转 + 相位补偿」链路的最终对账。"""
        top_blob, _ = self._blob("TOP-DOWN")
        bot_blob, _ = self._blob("BOTTOM-UP")
        top = fi.load_linear(top_blob, header_of(top_blob))
        bot = fi.load_linear(bot_blob, header_of(bot_blob))
        assert bot.geom.bayer_effective == "GBRG"
        assert np.array_equal(bot.rgb, top.rgb[::-1])

    def test_raw_at_mapping(self, tmp_path):
        blob, _ = self._blob("TOP-DOWN")
        img = fi.load_linear(blob, header_of(blob))
        assert img.raw_at(2, 3) == (4, 6)
        assert img.width == 4 and img.height == 4 and img.is_color


# ================================================================= 缺陷回归
#
# 下面每个类钉死一条**已在真机/离线复现过**的缺陷,类名后的编号对应
# 2026-07-27 那轮 FITS 查看器专项审查的条目。改这些代码前先看这里。


class TestLinkedStfCaliber:
    """#1 链接模式必须「逐通道算 median/MADN 再平均」,不是把三通道汇成一池。

    汇池后 MADN 量到的是**通道间背景偏移**而不是噪声:R/G/B 背景
    1200/1800/900 ADU、σ≈40 时汇池 MADN 大十几倍,c0 被 clamp 到 0
    (等于完全不裁背景)、m2 大一个数量级,链接模式渲染发灰欠拉伸。
    """

    def _channels(self):
        rng = np.random.default_rng(0)
        bg = (1200.0, 1800.0, 900.0)
        return np.stack([rng.normal(b, 40.0, 60000).astype(np.float32) / 65535.0
                         for b in bg], axis=-1)

    def test_linked_equals_mean_of_per_channel(self):
        flat = self._channels()
        li = fi.stf_stats(flat[:, None, :], linked=True, max_samples=None)
        un = fi.stf_stats(flat[:, None, :], linked=False, max_samples=None)
        assert li[0].median == pytest.approx(
            sum(s.median for s in un) / 3, rel=1e-6)
        assert li[0].madn == pytest.approx(sum(s.madn for s in un) / 3, rel=1e-6)
        # 三通道共用同一组参数(链接模式的定义)
        assert li[0].c0 == li[1].c0 == li[2].c0
        assert li[0].m2 == li[1].m2 == li[2].m2

    def test_linked_madn_is_noise_not_channel_offset(self):
        """汇池 MADN 会比真噪声大一个数量级 —— 这是当年错的根因。"""
        flat = self._channels()
        pooled = fi.madn(flat.reshape(-1))
        li = fi.stf_stats(flat[:, None, :], linked=True, max_samples=None)[0]
        assert pooled > li.madn * 5           # 实测约 11 倍
        assert li.madn == pytest.approx(40.0 / 65535.0, rel=0.05)

    def test_linked_still_clips_background(self):
        """c0 必须真的落在背景上方,不能被 clamp 成 0(=不裁)。"""
        flat = self._channels()
        li = fi.stf_stats(flat[:, None, :], linked=True, max_samples=None)[0]
        assert li.c0 > 0.0
        assert li.c0 == pytest.approx(li.median - 2.80 * li.madn, rel=1e-6)
        assert li.m2 < 0.02                    # 汇池口径这里会是 0.05 量级

    def test_linked_keeps_channel_ratio(self):
        """链接模式的用途没变:通道比例保留(不做自动白平衡)。"""
        base = synth_sky(seed=11)
        rgb = np.stack([base * k for k in (1.0, 0.6, 0.4)], axis=-1).astype(np.float32)
        li = fi.stf_stats(rgb, linked=True)
        meds = [float(np.median(fi.apply_stf(rgb[..., c], li[c]))) for c in range(3)]
        assert max(meds) - min(meds) > 0.03


class TestNaNFloatImage:
    """#2 浮点 FITS(BITPIX -32/-64)里的 NaN 曾让整张图打不开:
    ``(nan*bins).astype(int32)`` → INT_MIN → ``np.bincount`` 抛
    「'list' argument must have no negative elements」,错误信息毫无指向性。
    NaN 在 Siril/DSS/定标叠加的输出里很常见。
    """

    def _img(self):
        a = synth_sky(64, 64, seed=17).copy()
        a[0, 0] = np.nan
        a[1, 1] = np.inf
        a[2, 2] = -np.inf
        return a

    def test_histogram_unit_survives(self):
        h = fi.histogram_unit(self._img(), 256)
        assert h.shape == (256,)
        # NaN 与 ±Inf 一律剔除(真正的流水线里 to_unit 已先把 ±Inf 抹成 0/1,
        # 这里是直接拿原始浮点调用的兜底路径)
        assert int(h.sum()) == 64 * 64 - 3

    def test_madn_and_stats_finite(self):
        a = self._img()
        assert np.isfinite(fi.madn(a))
        cs = fi.stf_stats(a)[0]
        for v in (cs.median, cs.madn, cs.c0, cs.m2, cs.lo, cs.hi):
            assert np.isfinite(v)
        assert cs.madn > 0.0                    # 真的量到了噪声,不是被 NaN 吃掉

    def test_compute_stats_finite(self):
        a = self._img()[:, :, None]
        for mode in ("stf", "asinh", "percentile"):
            st = fi.compute_stats(fi.sample_unit(a), fi.StretchParams(mode=mode))
            for cs in st:
                assert np.isfinite(cs.c0) and np.isfinite(cs.m2)
                assert np.isfinite(cs.lo) and np.isfinite(cs.hi)

    def test_to_unit_scrubs(self):
        u = fi.to_unit(np.array([np.nan, np.inf, -np.inf, 0.5], dtype=np.float32),
                       fi.UnitScale(0.0, 1.0))
        assert list(u) == [0.0, 1.0, 0.0, 0.5]

    def test_stretch_not_black(self):
        """全链路:带 NaN 的浮点图必须能渲染,而且不是一片黑。"""
        out, _ = fi.stretch(self._img(), fi.StretchParams())
        assert out.dtype == np.uint8 and np.all(np.isfinite(out))
        assert int(out.max()) > 40              # 背景被拉到 0.25 附近 ≈ 64

    def test_end_to_end_float_fits_with_nan(self, tmp_path):
        data = np.full((8, 8), 0.02, dtype=np.float32)
        data[0, 0] = np.nan
        blob = make_fits(8, 8, bitpix=-32, extra=[("ROWORDER", "TOP-DOWN")],
                         data=data)
        p = tmp_path / "nan.fit"
        p.write_bytes(blob)
        img = fi.load_linear(p, header_of(blob))
        h = fi.histogram_unit(img.sample[:, 0], 256)    # 以前在这里炸
        assert int(h.sum()) > 0
        out, _ = fi.stretch(img.rgb, fi.StretchParams(), unit=img.unit)
        assert out.shape == (8, 8, 3)


class TestDebayerWideAccumulator:
    """#3 绿位求平均的累加宽度要看输入位宽:uint32 输入(BITPIX 32 +
    BZERO 2147483648)用 uint32 中转会回绕。"""

    def test_uint32_no_overflow(self):
        a = np.zeros((2, 2), dtype=np.uint32)
        a[0, 0], a[1, 1] = 10, 20               # R, B
        a[0, 1] = a[1, 0] = 4000000000          # 两个绿位
        out = fi.debayer_superpixel(a, "RGGB")
        assert int(out[0, 0, 1]) == 4000000000  # uint32 中转会得 1852516352

    def test_uint32_general(self):
        a = np.zeros((2, 2), dtype=np.uint32)
        a[0, 1], a[1, 0] = 4294967295, 4294967293
        assert int(fi.debayer_superpixel(a, "RGGB")[0, 0, 1]) == 4294967294

    def test_int64_signed(self):
        a = np.zeros((2, 2), dtype=np.int64)
        a[0, 1], a[1, 0] = -2000000000, 3000000000
        assert int(fi.debayer_superpixel(a, "RGGB")[0, 0, 1]) == 500000000

    def test_uint16_unchanged(self):
        a = np.zeros((2, 2), dtype=np.uint16)
        a[0, 1] = a[1, 0] = 65535
        assert int(fi.debayer_superpixel(a, "RGGB")[0, 0, 1]) == 65535


class TestMonoStretch:
    """#7 单色/缺 BAYERPAT 的 FITS 不该被强行展开成 3 通道全分辨率:
    同一条 LUT 算三遍、位图大三倍。"""

    def _mono(self):
        return (np.random.default_rng(31).integers(0, 65536, (32, 48))
                ).astype(np.uint16)

    def test_mono_out_single_plane(self):
        out, stats = fi.stretch(self._mono(), fi.StretchParams(), mono_out=True)
        assert out.shape == (32, 48) and out.dtype == np.uint8
        assert len(stats) == 3                   # 统计仍给三份,UI 不用改

    def test_mono_out_matches_broadcast(self):
        a = self._mono()
        p = fi.StretchParams()
        gray, _ = fi.stretch(a, p, mono_out=True)
        rgb, _ = fi.stretch(a, p)
        for c in range(3):
            assert np.array_equal(rgb[:, :, c], gray)

    def test_color_ignores_mono_out(self):
        rgb = (np.random.default_rng(32).integers(0, 65536, (8, 8, 3))
               ).astype(np.uint16)
        out, _ = fi.stretch(rgb, fi.StretchParams(), mono_out=True)
        assert out.shape == (8, 8, 3)            # 三通道不受影响

    def test_lut_computed_once_for_mono(self, monkeypatch):
        calls = []
        real = fi.transfer_lut

        def counting(params, cs, n=65536):
            calls.append(n)
            return real(params, cs, n)

        monkeypatch.setattr(fi, "transfer_lut", counting)
        fi.stretch(self._mono(), fi.StretchParams())
        assert len(calls) == 1                   # 以前同一条 LUT 要算三遍


class TestLutHistogram:
    """#8 拉伸后的直方图由 LUT 推导,不再在全分辨率 uint8 位图上 bincount
    (非连续视图先复制、再提升成 intp,单色 26M 像素瞬时 +209MB)。"""

    def _u16(self):
        return (np.random.default_rng(41).integers(0, 65536, (64, 96))
                ).astype(np.uint16)

    def test_linear_hist_matches_bincount(self):
        a = self._u16()
        assert np.array_equal(fi.linear_hist_u16(a),
                              np.bincount(a.reshape(-1), minlength=65536))

    def test_linear_hist_chunking_is_exact(self):
        a = self._u16()
        assert np.array_equal(fi.linear_hist_u16(a, chunk_rows=1),
                              fi.linear_hist_u16(a, chunk_rows=4096))

    def test_linear_hist_on_noncontiguous_channel(self):
        a = self._u16()
        cube = np.stack([a, a // 2, a // 3], axis=-1)
        assert np.array_equal(fi.linear_hist_u16(cube[:, :, 1]),
                              np.bincount((a // 2).reshape(-1), minlength=65536))

    def test_derived_hist_equals_direct(self):
        """三种模式下都必须与直接在位图上 bincount 逐格相同。"""
        a = self._u16()
        us = fi.UnitScale(0.0, 1.0 / 65535.0)
        lin = fi.linear_hist_u16(a)
        for mode in ("stf", "asinh", "percentile"):
            p = fi.StretchParams(mode=mode)
            stats = fi.compute_stats(fi.sample_unit(a[:, :, None], us), p)
            rgb8, st3 = fi.stretch(a, p, unit=us, stats=stats, mono_out=True)
            derived = fi.hist_after_from_lut(fi.transfer_lut(p, st3[0]), lin)
            assert np.array_equal(derived,
                                  np.bincount(rgb8.reshape(-1), minlength=256))

    def test_length_mismatch_raises(self):
        with pytest.raises(fi.FitsImageError):
            fi.hist_after_from_lut(np.zeros(256, np.uint8), np.zeros(65536))


class TestDecodeNoExtraCopies:
    """#10 主路径(BITPIX 16 + BZERO 32768)零整份复制:就地交换字节序 + view,
    翻转按行块就地交换。**结果必须与老的 astype/ascontiguousarray 逐元素相同**,
    而且**绝不能改坏调用方传进来的 bytearray**。"""

    def _blob(self, h, w, roworder):
        vals = (np.arange(h * w, dtype=np.int64) % 65536).reshape(h, w)
        stored = (vals - 32768).astype(np.int16)
        return make_fits(w, h, extra=[("BZERO", 32768), ("BSCALE", 1),
                                      ("ROWORDER", roworder)],
                         data=stored), vals.astype(np.uint16)

    @pytest.mark.parametrize("h,w", [(6, 4), (7, 4), (1, 4), (64, 8)])
    def test_file_matches_bytes_path(self, tmp_path, h, w):
        blob, phys = self._blob(h, w, "BOTTOM-UP")
        p = tmp_path / f"a{h}x{w}.fit"
        p.write_bytes(blob)
        g = fi.geometry_from_header(header_of(blob))
        from_file = fi.decode_pixels(p, g)
        from_bytes = fi.decode_pixels(blob, g)
        assert np.array_equal(from_file, from_bytes)
        assert np.array_equal(from_file, phys[::-1])        # 自底向上 → 翻转
        assert from_file.dtype == np.uint16
        assert from_file.flags.c_contiguous                 # 后续切片不能是负步长

    def test_topdown_not_flipped(self, tmp_path):
        blob, phys = self._blob(6, 4, "TOP-DOWN")
        p = tmp_path / "t.fit"
        p.write_bytes(blob)
        g = fi.geometry_from_header(header_of(blob))
        assert np.array_equal(fi.decode_pixels(p, g), phys)

    def test_bytearray_source_not_mutated(self):
        """可写的 bytearray 传进来时**不能**就地交换字节序 —— 那是调用方的缓冲区。"""
        blob, phys = self._blob(6, 4, "BOTTOM-UP")
        buf = bytearray(blob)
        snapshot = bytes(buf)
        out = fi.decode_pixels(buf, fi.geometry_from_header(header_of(blob)))
        assert np.array_equal(out, phys[::-1])
        assert bytes(buf) == snapshot

    def test_bytes_source_readonly_ok(self):
        blob, phys = self._blob(6, 4, "BOTTOM-UP")
        out = fi.decode_pixels(bytes(blob),
                               fi.geometry_from_header(header_of(blob)))
        assert np.array_equal(out, phys[::-1])

    def test_flip_helper_odd_and_even(self):
        for h in (1, 2, 3, 64, 65, 130):
            a = np.arange(h * 3, dtype=np.uint16).reshape(h, 3)
            assert np.array_equal(fi._flip_vertical(a.copy()), a[::-1])

    def test_extremes_still_roundtrip(self, tmp_path):
        """就地 byteswap+view 与 astype(uint16) 的等价性(边界值)。"""
        stored = np.array([[-32768, -1, 0, 1, 32767]], dtype=np.int16)
        blob = make_fits(5, 1, extra=[("BZERO", 32768), ("BSCALE", 1),
                                      ("ROWORDER", "TOP-DOWN")], data=stored)
        p = tmp_path / "x.fit"
        p.write_bytes(blob)
        g = fi.geometry_from_header(header_of(blob))
        assert list(fi.decode_pixels(p, g)[0]) == [0, 32767, 32768, 32769, 65535]


# ------------------------------------------------------- GUI 侧(纯逻辑,不建窗口)
#
# 这些只调页面上**不碰 XAML** 的静态/纯函数,或用桩对象调未绑定方法。
# win32more 是延迟绑定,import 不会加载 DLL,所以离线单测里能安全 import。


class TestHistDownsample:
    """#4 曲线下采样:256 格降到约 96 点,**每段取最大**保住尖峰。"""

    def test_keeps_length_when_small(self):
        from astro_smb_gui._fitsview import _downsample_peak
        y = np.arange(50, dtype=np.float64)
        assert _downsample_peak(y, 96) is y

    def test_downsamples_and_keeps_peak(self):
        from astro_smb_gui._fitsview import _downsample_peak
        y = np.zeros(256, dtype=np.float64)
        y[137] = 999.0
        out = _downsample_peak(y, 96)
        assert out.shape[0] <= 96 and out.shape[0] >= 90
        assert float(out.max()) == 999.0            # 尖峰不能被平均掉
        assert float(out.sum()) >= 999.0

    def test_monotone_ramp(self):
        from astro_smb_gui._fitsview import _downsample_peak
        y = np.arange(256, dtype=np.float64)
        out = _downsample_peak(y, 96)
        assert np.all(np.diff(out) > 0)
        assert float(out[-1]) == 255.0


class TestHistFrameCaching:
    """#4 曲线只在数据真变了才重建;「拉伸前」档整张图期间一次都不该重建。"""

    class _Canvas:
        def __init__(self):
            self.Width, self.Height = 292.0, 130.0

    def _page(self):
        """桩:只带 _draw_hist 用到的字段,记录 frame/marks 各被调用几次。"""
        from astro_smb_gui._fitsview import FitsViewPage

        class Stub:
            def __init__(self):
                self.hist_canvas = TestHistFrameCaching._Canvas()
                self.hist_toggle = type("T", (), {"IsChecked": False,
                                                  "Content": ""})()
                self.hist_hint = type("H", (), {"Text": ""})()
                self._hist_before = [np.arange(256.0)]
                self._hist_after = None
                self._hist_before_ver = 1
                self._hist_after_ver = 1
                self._hist_key = None
                self._hist_marks = []
                self._stats = None
                self.frames = 0
                self.places = 0

            _draw_hist = FitsViewPage._draw_hist

            def _build_hist_frame(self, *a):
                self.frames += 1

            def _place_hist_marks(self, *a):
                self.places += 1

        return Stub()

    def test_before_curve_built_once(self):
        p = self._page()
        for _ in range(5):
            p._draw_hist()
        assert p.frames == 1            # 曲线只建一次
        assert p.places == 5            # 标记每次都重新摆位(便宜)

    def test_after_data_change_rebuilds(self):
        p = self._page()
        p._draw_hist()
        p.hist_toggle.IsChecked = True
        p._hist_after = [np.arange(256.0)] * 3
        p._draw_hist()                  # 换档 → 必须重建
        assert p.frames == 2
        p._hist_after_ver += 1          # 新一轮渲染的数据
        p._draw_hist()
        assert p.frames == 3
        p._draw_hist()                  # 数据没变 → 不重建
        assert p.frames == 3

    def test_switch_back_rebuilds_but_stays_stable(self):
        p = self._page()
        p._draw_hist()
        p.hist_toggle.IsChecked = True
        p._hist_after = [np.arange(256.0)] * 3
        p._draw_hist()
        p.hist_toggle.IsChecked = False
        p._draw_hist()
        n = p.frames
        for _ in range(3):
            p._hist_after_ver += 1      # 后台还在渲染,但当前看的是「拉伸前」
            p._draw_hist()
        assert p.frames == n            # 一次都不该重建

    def test_no_data_clears(self):
        p = self._page()
        p._draw_hist()
        p._hist_before = None
        cleared = []
        p.hist_canvas.Children = type("C", (), {"Clear": lambda s: cleared.append(1)})()
        p._draw_hist()
        assert cleared == [1] and p._hist_key is None


class TestImageFailedRelay:
    """#5 位图解码失败时不能丢掉排队中的**更新**那一帧,否则画面永久停在旧帧。"""

    def _stub(self):
        from astro_smb_gui._fitsview import FitsViewPage

        class Stub:
            def __init__(self):
                self._back_busy = True
                self._pending_path = "newer.bmp"
                self.status_text = type("T", (), {"Text": ""})()
                self.shown = []

            _on_image_failed = FitsViewPage._on_image_failed
            _on_image_opened = FitsViewPage._on_image_opened

            def _show_image(self, p):
                self.shown.append(p)

        return Stub()

    def test_failure_starts_pending(self):
        s = self._stub()
        s._on_image_failed(None, None)
        assert s.shown == ["newer.bmp"]
        assert s._pending_path is None
        assert s._back_busy is False
        assert s.status_text.Text == tr("位图解码失败")

    def test_failure_without_pending(self):
        s = self._stub()
        s._pending_path = None
        s._on_image_failed(None, None)
        assert s.shown == []

    def test_ignores_when_not_busy(self):
        s = self._stub()
        s._back_busy = False
        s._on_image_failed(None, None)
        assert s.shown == [] and s._pending_path == "newer.bmp"


class TestRenderWorkerReleases:
    """#6 渲染线程空闲 wait() 时**不能**还抓着上一张图(91~130MB)。"""

    def test_job_released_when_idle(self):
        import threading
        import weakref

        from astro_smb_gui._fitsview import FitsViewPage

        class Big:
            pass

        done = threading.Event()

        class Stub:
            def __init__(self):
                self._rlock = threading.Condition()
                self._rpending = None
                self._rstop = False
                self.shell = self

            _render_worker = FitsViewPage._render_worker

            def _render_once(self, job):
                return "result"     # 不引用 job 里的任何东西

            def _apply_render(self, res):
                pass

            def ui(self, fn, *a):
                done.set()

        s = Stub()
        big = Big()
        ref = weakref.ref(big)
        t = threading.Thread(target=s._render_worker, daemon=True)
        t.start()
        with s._rlock:
            s._rpending = (1, 1, big, None, "k", None)
            s._rlock.notify()
        assert done.wait(5.0)
        del big
        import gc
        for _ in range(20):
            gc.collect()
            if ref() is None:
                break
            time.sleep(0.05)
        assert ref() is None, "渲染线程空闲时仍强引用着上一张图"
        with s._rlock:
            s._rstop = True
            s._rlock.notify()
        t.join(3.0)


class TestPageHistAfter:
    """#8 页面侧:有线性直方图就由 LUT 推导,没有(浮点)才回落抽样。"""

    def test_uses_lut_when_available(self):
        from astro_smb_gui._fitsview import FitsViewPage
        a = (np.random.default_rng(51).integers(0, 65536, (32, 40))).astype(np.uint16)
        us = fi.UnitScale(0.0, 1.0 / 65535.0)
        p = fi.StretchParams()
        stats = fi.compute_stats(fi.sample_unit(a[:, :, None], us), p)
        rgb8, st3 = fi.stretch(a, p, unit=us, stats=stats, mono_out=True)
        lin = [fi.linear_hist_u16(a)]
        got = FitsViewPage._hist_after_for(rgb8, p, st3, lin)
        assert len(got) == 3
        ref = np.bincount(rgb8.reshape(-1), minlength=256)
        for h in got:
            assert np.array_equal(h, ref)

    def test_falls_back_to_subsample(self):
        from astro_smb_gui._fitsview import FitsViewPage
        rgb8 = np.full((40, 40, 3), 7, dtype=np.uint8)
        got = FitsViewPage._hist_after_for(rgb8, fi.StretchParams(), None, None)
        assert len(got) == 3
        for h in got:
            assert h.shape == (256,)
            assert int(h[7]) == 10 * 10          # [::4, ::4] 抽样
            assert int(h.sum()) == 10 * 10

    def test_fallback_mono(self):
        from astro_smb_gui._fitsview import FitsViewPage
        gray = np.full((40, 40), 3, dtype=np.uint8)
        got = FitsViewPage._hist_after_for(gray, fi.StretchParams(), None, [])
        assert len(got) == 3 and int(got[0][3]) == 100

    def test_linear_hists_skips_float(self):
        from astro_smb_gui._fitsview import FitsViewPage
        blob = make_fits(8, 8, bitpix=-32, extra=[("ROWORDER", "TOP-DOWN")],
                         data=np.full((8, 8), 0.5, dtype=np.float32))
        img = fi.load_linear(blob, header_of(blob))
        assert FitsViewPage._linear_hists(img, None) is None

    def test_linear_hists_u16(self):
        from astro_smb_gui._fitsview import FitsViewPage
        blob, _ = _u16_blob()
        img = fi.load_linear(blob, header_of(blob))
        lin = FitsViewPage._linear_hists(img, None)
        assert lin is not None and len(lin) == img.channels
        assert int(lin[0].sum()) == img.width * img.height


def _u16_blob():
    phys = cfa("RGGB", 8, 8).astype(np.int64)
    stored = (phys - 32768).astype(np.int16)
    return make_fits(8, 8, extra=[("BZERO", 32768), ("BSCALE", 1),
                                  ("BAYERPAT", "RGGB"),
                                  ("ROWORDER", "TOP-DOWN")], data=stored), phys


class TestPruneRenders:
    """#7 渲染位图目录要**同时**受数量和字节数两道闸门(一张全分辨率彩色位图
    就有 78MB,只按「留 8 张」算能占 626MB)。"""

    def _fill(self, d, sizes):
        import time as _t
        out = []
        for i, n in enumerate(sizes):
            f = d / f"r{i}.bmp"
            f.write_bytes(b"\0" * n)
            os.utime(f, (1700000000 + i, 1700000000 + i))    # i 越大越新
            out.append(f)
        return out

    def test_byte_budget_wins(self, tmp_path, monkeypatch):
        # **monkeypatch 要打在函数真正住的地方。** _prune_renders/_render_dir 已随
        # B9 移到 views.fitsview;_fitsview 那边只是 `from ... import` 的名字,
        # 改它不影响源模块内部调用 —— 测试会**静默失效**(真踩了:两条预算
        # 测试仍然全绿,其实什么都没测)。与 sys.modules 别名的那九个模块不同,
        # 那些两边是同一对象。
        from astro_smb_app.views import fitsview as fv
        monkeypatch.setattr(fv, "_render_dir", lambda: tmp_path)
        files = self._fill(tmp_path, [1000] * 6)
        fv._prune_renders(keep=8, budget=2500)
        alive = sorted(f.name for f in tmp_path.iterdir())
        assert alive == ["r4.bmp", "r5.bmp"]        # 数量够 8 张,但字节数只装得下 2 张
        assert not files[0].exists()

    def test_count_budget_wins(self, tmp_path, monkeypatch):
        from astro_smb_app.views import fitsview as fv
        monkeypatch.setattr(fv, "_render_dir", lambda: tmp_path)
        self._fill(tmp_path, [10] * 6)
        fv._prune_renders(keep=2, budget=1 << 30)
        assert sorted(f.name for f in tmp_path.iterdir()) == ["r4.bmp", "r5.bmp"]

    def test_defaults_are_bounded(self):
        from astro_smb_app.views import fitsview as fv
        assert fv._RENDER_BUDGET <= 256 << 20

    def test_save_png_from_mono_bmp(self, tmp_path):
        """#7 的连带面:单色位图改存 mode="L" 后,「另存为 PNG」必须照旧能用。"""
        from PIL import Image

        from astro_smb_gui._fitsview import _save_png
        g = (np.linspace(0, 255, 64, dtype=np.uint8)[None, :]
             * np.ones((32, 1), dtype=np.uint8))
        bmp = tmp_path / "a.bmp"
        Image.fromarray(g, mode="L").save(bmp, format="BMP")
        out = _save_png(str(bmp), tmp_path / "png", "mono.fit")
        with Image.open(out) as im:
            assert np.array_equal(np.array(im.convert("L")), g)


class TestClearCacheRuntime:
    """#9 查看器每打开一张 SMB 上的 FITS 就在 cache 顶层留一份 49.8MB 原图,
    而 clear_cache 以前只在启动时跑一次 —— 常开的 GUI 等于永远不裁。
    运行中调必须 **不动 dragout 目录**(那里可能正有一次拖拽在读文件)。"""

    def test_keeps_dragout_when_asked(self, tmp_path, monkeypatch):
        from astro_smb_gui import preview
        monkeypatch.setattr(preview, "cache_dir", lambda: tmp_path)
        drag = tmp_path / "dragout"
        drag.mkdir()
        (drag / "in-flight.fit").write_bytes(b"x" * 10)
        (tmp_path / "a.fit").write_bytes(b"y" * 100)
        preview.clear_cache(max_bytes=10, drop_dragout=False)
        assert (drag / "in-flight.fit").exists()

    def test_startup_drops_dragout(self, tmp_path, monkeypatch):
        from astro_smb_gui import preview
        monkeypatch.setattr(preview, "cache_dir", lambda: tmp_path)
        drag = tmp_path / "dragout"
        drag.mkdir()
        (drag / "stale.fit").write_bytes(b"x" * 10)
        preview.clear_cache(max_bytes=1 << 30)
        assert not drag.exists()

    def test_evicts_oldest_by_atime(self, tmp_path, monkeypatch):
        from astro_smb_gui import preview
        monkeypatch.setattr(preview, "cache_dir", lambda: tmp_path)
        for i in range(6):
            f = tmp_path / f"{i}.fit"
            f.write_bytes(b"z" * 100)
            os.utime(f, (1700000000 + i, 1700000000 + i))
        preview.clear_cache(max_bytes=300, drop_dragout=False)
        left = sorted(int(f.stem) for f in tmp_path.iterdir() if f.is_file())
        assert left == [5]              # 按 atime 从旧到新删,裁到上限的一半(150B)
        preview.clear_cache(max_bytes=300, drop_dragout=False)   # 幂等:已达标不再删
        assert sorted(int(f.stem) for f in tmp_path.iterdir() if f.is_file()) == [5]

    def test_under_budget_keeps_everything(self, tmp_path, monkeypatch):
        from astro_smb_gui import preview
        monkeypatch.setattr(preview, "cache_dir", lambda: tmp_path)
        (tmp_path / "a.fit").write_bytes(b"z" * 100)
        preview.clear_cache(max_bytes=1 << 20, drop_dragout=False)
        assert (tmp_path / "a.fit").exists()


class TestDownloadCachedRace:
    """#11 查看器与 PreviewWorker 共用同一个 ``cache/<sha1>.fit``(cache_key 相同),
    以前无互斥:两条线程各下一份 50MB,再各自 os.replace —— 只要一方的 replace
    落在另一方 np.fromfile 打开该文件期间,Windows 上就是共享冲突。"""

    def test_second_caller_reuses_first(self, tmp_path):
        import threading

        from astro_smb_gui.preview import download_cached

        started = threading.Event()
        release = threading.Event()
        calls = []

        class SlowClient:
            def download_file(self, share, path, local, progress=None, cancel=None):
                calls.append(Path(local).name)
                started.set()
                release.wait(5.0)
                Path(local).write_bytes(b"whole")

        dest = tmp_path / "x.fit"
        c = SlowClient()
        t = threading.Thread(target=download_cached,
                             args=(c, "s", "p", dest, None), daemon=True)
        t.start()
        assert started.wait(5.0)
        second = threading.Thread(
            target=download_cached, args=(c, "s", "p", dest, None),
            kwargs={"tmp_suffix": ".fvpart"}, daemon=True)
        second.start()
        time.sleep(0.15)
        assert len(calls) == 1               # 第二个还在锁上等,没有开第二次传输
        release.set()
        t.join(5.0)
        second.join(5.0)
        assert len(calls) == 1               # 拿到锁后重判 dest 已存在,直接复用
        assert dest.read_bytes() == b"whole"

    def test_tmp_names_unique(self, tmp_path):
        from astro_smb_gui.preview import download_cached

        seen = []

        class C:
            def download_file(self, share, path, local, progress=None, cancel=None):
                seen.append(Path(local).name)
                Path(local).write_bytes(b"ok")

        for i in range(3):
            download_cached(C(), "s", "p", tmp_path / f"f{i}.fit", None)
        assert len(set(seen)) == 3
        for i, name in enumerate(seen):
            assert name.startswith(f"f{i}.fit.part")

    def test_different_dests_not_serialized(self, tmp_path):
        """锁是按 dest 分的,不同文件不能互相挡(否则并发下载全废)。"""
        import threading

        from astro_smb_gui.preview import download_cached

        gate = threading.Barrier(2, timeout=5.0)

        class C:
            def download_file(self, share, path, local, progress=None, cancel=None):
                gate.wait()                  # 两条线程必须同时在传输中
                Path(local).write_bytes(b"ok")

        ts = [threading.Thread(target=download_cached,
                               args=(C(), "s", "p", tmp_path / f"g{i}.fit", None))
              for i in range(2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(6.0)
            assert not t.is_alive()


class TestThumbSourceWording:
    """#12 单色 FITS 走新路径时来源说明不能再写「超像素去马赛克」——
    ``_render_fits_thumb`` 的存在意义就是「界面上别说去马赛克其实是灰度」。"""

    def test_mono(self):
        from astro_smb_gui.preview import PreviewWorker
        s = PreviewWorker._full_src(False, 1)
        assert "单色" in s and "去马赛克" not in s

    def test_debayered(self):
        from astro_smb_gui.preview import PreviewWorker
        assert "去马赛克" in PreviewWorker._full_src(True, 3)

    def test_rgb_cube(self):
        from astro_smb_gui.preview import PreviewWorker
        s = PreviewWorker._full_src(False, 3)
        assert "去马赛克" not in s and "立方体" in s

    def test_matches_load_linear(self, tmp_path):
        """几何推出来的结论必须与 load_linear 实际做的一致(缓存命中路径靠它)。"""
        from astro_smb_gui.preview import PreviewWorker
        cases = [
            ([("BZERO", 32768), ("BSCALE", 1), ("BAYERPAT", "RGGB")], 1),
            ([("BZERO", 32768), ("BSCALE", 1)], 1),
        ]
        for extra, planes in cases:
            data = (cfa("RGGB", 8, 8).astype(np.int64) - 32768).astype(np.int16)
            blob = make_fits(8, 8, planes=planes, extra=extra, data=data)
            hdr = header_of(blob)
            geom = fi.geometry_from_header(hdr)
            img = fi.load_linear(blob, hdr)
            assert (PreviewWorker._full_src(geom.bayer_effective is not None,
                                            geom.planes)
                    == PreviewWorker._full_src(img.debayered, img.channels))


class TestBrowserViewerMenu:
    """#13 右键菜单「在 FITS 查看器中打开」是第一项(排在「下载」之前),
    以前不按 _is_fits 过滤 —— 右键一个 _thn.jpg 会切页清掉当前画面,
    几百毫秒后再弹「FITS 头不完整」。"""

    def _stub(self, names):
        from astro_smb.client import RemoteEntry

        from astro_smb_gui._browser import BrowserPage

        entries = [RemoteEntry(share="S", path=n, name=n, is_dir=False, size=1,
                               mtime=0.0, ctime=0.0, atime=0.0, attributes=0x20)
                   for n in names]

        class Item:
            Visibility = None
            IsEnabled = None

        class Shell:
            def __init__(self):
                self.infos = []

            def info(self, t):
                self.infos.append(t)

        class Stub:
            _is_fits = BrowserPage._is_fits
            _on_open_viewer_menu = BrowserPage._on_open_viewer_menu
            _on_context_opening = BrowserPage._on_context_opening

            def __init__(self):
                self.shell = Shell()
                self.opened = []
                self._menu_viewer_item = Item()

            def _selected_entries(self):
                return entries

            def _open_in_viewer(self, entry):
                self.opened.append(entry.name)
                return True

        return Stub()

    def test_menu_ignores_non_fits(self):
        s = self._stub(["a_thn.jpg", "b.txt"])
        s._on_open_viewer_menu(None, None)
        assert s.opened == []
        assert s.shell.infos and "FITS" in s.shell.infos[0]

    def test_menu_picks_fits(self):
        s = self._stub(["a_thn.jpg", "b.fits"])
        s._on_open_viewer_menu(None, None)
        assert s.opened == ["b.fits"]

    def test_opening_hides_for_non_fits(self):
        from win32more.Microsoft.UI.Xaml import Visibility
        s = self._stub(["a_thn.jpg"])
        s._on_context_opening(None, None)
        assert s._menu_viewer_item.Visibility == Visibility.Collapsed
        assert s._menu_viewer_item.IsEnabled is False

    def test_opening_shows_for_fits(self):
        from win32more.Microsoft.UI.Xaml import Visibility
        s = self._stub(["x.fit"])
        s._on_context_opening(None, None)
        assert s._menu_viewer_item.Visibility == Visibility.Visible
        assert s._menu_viewer_item.IsEnabled is True

    def test_opening_survives_broken_selection(self):
        s = self._stub(["x.fit"])
        s._selected_entries = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        s._on_context_opening(None, None)       # 不能把右键菜单弄崩


# ------------------------------------------------ 真机反馈:asinh 需要减黑场

class TestAsinhBlackPoint:
    """裸 asinh 在真实天文数据上基本没用:天光基座把曲线的陡峭段全吃掉,
    画面发灰、目标不突出(用户真机反馈)。先减黑场再上曲线才对得上 STF/Siril。"""

    def test_black_point_lifts_contrast_above_pedestal(self):
        # 背景基座 0.02,目标信号 0.02~0.06 —— 典型光污染下的 OSC 亮场量级
        x = np.array([0.02, 0.03, 0.04, 0.06], dtype=np.float32)
        naked = fi.asinh_stretch(x, 100.0, 0.0)
        fixed = fi.asinh_stretch(x, 100.0, 0.02)
        # 减黑场后背景被压到 0,而信号之间的落差应显著变大
        assert fixed[0] == pytest.approx(0.0, abs=1e-6)
        assert (fixed[-1] - fixed[0]) > (naked[-1] - naked[0])

    def test_black_zero_is_identical_to_old_behaviour(self):
        x = np.linspace(0.0, 1.0, 64, dtype=np.float32)
        old = (np.arcsinh(100.0 * x) / np.arcsinh(100.0)).astype(np.float32)
        assert np.allclose(fi.asinh_stretch(x, 100.0, 0.0), old, atol=1e-6)

    def test_monotonic_and_bounded(self):
        x = np.linspace(0.0, 1.0, 256, dtype=np.float32)
        y = fi.asinh_stretch(x, 50.0, 0.1)
        assert np.all(np.diff(y) >= -1e-6)
        assert y.min() >= 0.0 and y.max() <= 1.0

    def test_black_at_or_above_one_degrades_safely(self):
        x = np.linspace(0.0, 1.0, 16, dtype=np.float32)
        y = fi.asinh_stretch(x, 100.0, 1.0)          # den 被夹到 1e-9
        assert np.all(np.isfinite(y)) and y.max() <= 1.0

    def test_transfer_uses_channel_c0(self):
        """_transfer 必须把该通道的 c0 传给 asinh(不是恒 0)。"""
        cs = fi.ChannelStats(median=0.02, madn=0.001, c0=0.018, m2=0.05)
        p = fi.StretchParams(mode="asinh", asinh_a=100.0)
        f = fi._transfer(p, cs)
        got = f(np.array([0.018], dtype=np.float32))
        assert got[0] == pytest.approx(0.0, abs=1e-6), "c0 处应被压到黑"

    def test_fingerprint_tracks_linked_and_clipping(self):
        """asinh 现在依赖 c0 ⇒ linked / shadows_clipping 必须进指纹,
        否则切「通道链接」时缓存键不变、画面不刷新。"""
        base = fi.StretchParams(mode="asinh")
        assert base.fingerprint() != fi.StretchParams(
            mode="asinh", linked=True).fingerprint()
        assert base.fingerprint() != fi.StretchParams(
            mode="asinh", shadows_clipping=-2.0).fingerprint()
