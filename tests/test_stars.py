"""astro_smb.stars 的离线单测:全部用合成星场,不碰网络、不碰真机文件。

重点钉死几件在真机上翻过车、或者错了也不报错(只是星表悄悄变糟)的事:

1. **背景插值必须外推**到最外一圈块心之外 —— 钳死会在边缘一圈刷出成片假星;
2. **噪声估计必须对梯度免疫** —— 块内 MAD 量到的是梯度不是噪声,阈值会虚高十倍;
3. **二阶矩要减掉像素积分的 1/12**,而且孔径细化后才谈得上 FWHM 还原;
4. **拉长门限必须自适应** —— 整场拖线时固定 0.8 会把星全吃光(真机 M 16)。
"""

from __future__ import annotations

import math
import time

import numpy as np
import pytest

from astro_smb import stars as st

# ----------------------------------------------------------------- 合成工具


def _erf(x: np.ndarray) -> np.ndarray:
    """向量化的 erf(标准库 math.erf 逐点算;只用在少量边界上)。"""
    x = np.asarray(x, dtype=np.float64)
    return np.array([math.erf(float(v)) for v in x.ravel()]).reshape(x.shape)


def add_star(img: np.ndarray, x0: float, y0: float, flux: float,
             sigma: float, rad: int | None = None) -> None:
    """往图里叠一颗**像素积分**的圆高斯星(可分离,用 erf 精确积分)。

    探测器测到的是 PSF 在像素上的积分,不是像素中心处的采样值 —— 前者的二阶矩
    比后者多 1/12(见 :data:`astro_smb.stars.PIXEL_VARIANCE`)。合成数据必须
    按积分来,不然"减 1/12"这一步会被测成错的。
    """
    h, w = img.shape
    rad = int(math.ceil(6 * sigma)) if rad is None else int(rad)
    xi, yi = int(round(x0)), int(round(y0))
    x0i, x1i = max(0, xi - rad), min(w, xi + rad + 1)
    y0i, y1i = max(0, yi - rad), min(h, yi + rad + 1)
    if x0i >= x1i or y0i >= y1i:
        return
    s2 = sigma * math.sqrt(2.0)
    ex = _erf((np.arange(x0i, x1i + 1) - 0.5 - x0) / s2)
    ey = _erf((np.arange(y0i, y1i + 1) - 0.5 - y0) / s2)
    img[y0i:y1i, x0i:x1i] += (flux * (0.5 * np.diff(ey))[:, None]
                              * (0.5 * np.diff(ex))[None, :])


def add_ellipse(img: np.ndarray, x0: float, y0: float, flux: float,
                sa: float, sb: float, theta_deg: float, sub: int = 5) -> None:
    """椭圆高斯星(长轴 σ=``sa``,方位角从 +x 转向 +y);超采样做像素积分。"""
    h, w = img.shape
    rad = int(math.ceil(6 * max(sa, sb)))
    xi, yi = int(round(x0)), int(round(y0))
    xs = np.arange(max(0, xi - rad), min(w, xi + rad + 1))
    ys = np.arange(max(0, yi - rad), min(h, yi + rad + 1))
    if xs.size == 0 or ys.size == 0:
        return
    t = math.radians(theta_deg)
    ct, sn = math.cos(t), math.sin(t)
    off = (np.arange(sub) + 0.5) / sub - 0.5
    acc = np.zeros((ys.size, xs.size))
    for oy in off:
        for ox in off:
            dx = (xs[None, :] + ox) - x0
            dy = (ys[:, None] + oy) - y0
            u = dx * ct + dy * sn
            v = -dx * sn + dy * ct
            acc += np.exp(-0.5 * (u * u / (sa * sa) + v * v / (sb * sb)))
    acc *= flux / (2 * math.pi * sa * sb) / (sub * sub)
    img[ys[0]:ys[-1] + 1, xs[0]:xs[-1] + 1] += acc


def star_field(h=400, w=500, n=60, sigma=1.6, flux=(2e4, 2e5), back=1000.0,
               noise=10.0, seed=1, margin=25):
    """一片随机圆星场,返回 ``(图, 注入的 x, 注入的 y)``。"""
    rng = np.random.default_rng(seed)
    img = np.zeros((h, w), dtype=np.float32)
    xs = rng.uniform(margin, w - margin, n)
    ys = rng.uniform(margin, h - margin, n)
    for x, y, f in zip(xs, ys, rng.uniform(*flux, n)):
        add_star(img, x, y, f, sigma)
    img += back + rng.normal(0, noise, img.shape)
    return img, xs, ys


class _Cancel:
    def __init__(self, flag=True):
        self.flag = flag

    def is_set(self):
        return self.flag


# ================================================================= 背景估计


def test_background_constant():
    """常量图:背景 == 常量,噪声 == 0。"""
    img = np.full((200, 240), 1234.0, dtype=np.float32)
    bg = st.estimate_background(img, 60)
    assert bg.grid_shape == (3, 4)
    assert np.allclose(bg.back, 1234.0)
    assert np.allclose(bg.rms, 0.0)
    assert bg.rms_floor == 0.0


def test_background_noise_level():
    """纯高斯噪声:估出来的 σ 与真值差 < 5%。"""
    rng = np.random.default_rng(11)
    img = (500 + rng.normal(0, 37.0, (512, 512))).astype(np.float32)
    bg = st.estimate_background(img, 64)
    assert abs(bg.global_back - 500) < 3.0
    assert abs(bg.global_rms - 37.0) / 37.0 < 0.05


def test_background_gradient_tracks_everywhere():
    """强梯度下,插值背景在**整幅图(含四角)**上都贴着真值。

    回归:块心之外若钳成端点值而不外推,最外半个块的残差能到十几 σ。
    """
    h, w = 600, 800
    truth = (500 + 4000 * (np.arange(w)[None, :] / w)
             + 2500 * (np.arange(h)[:, None] / h)).astype(np.float32)
    rng = np.random.default_rng(3)
    img = (truth + rng.normal(0, 12.0, (h, w))).astype(np.float32)
    bg = st.estimate_background(img, 64)
    res = bg.plane() - truth
    assert abs(float(np.median(res))) < 3.0
    bh, bw = bg.box
    for name, patch in (("上", res[:bh]), ("下", res[-bh:]),
                        ("左", res[:, :bw]), ("右", res[:, -bw:]),
                        ("角", res[:bh, :bw])):
        assert abs(float(np.median(patch))) < 8.0, name
    assert float(np.abs(res).max()) < 60.0


def test_background_gradient_no_false_stars():
    """纯梯度 + 噪声(一颗星都没有)不能检出任何东西。"""
    h, w = 600, 800
    rng = np.random.default_rng(3)
    img = (500 + 4000 * (np.arange(w)[None, :] / w)
           + 2500 * (np.arange(h)[:, None] / h)
           + rng.normal(0, 12.0, (h, w))).astype(np.float32)
    s = st.detect_stars(img, box=64, threshold=5.0, apply_filters=False)
    assert s.n_blobs <= 3


@pytest.mark.parametrize("slope", [0.0, 5.0, 10.0, 30.0])
def test_background_rms_immune_to_gradient(slope):
    """任意线性斜率下 'diff'(二阶差分)都给出真噪声;'mad' 被梯度带飞。

    斜率 30 ADU/px、真值 σ=15 时实测:块内 MAD 给 711,二阶差分给 15.0。
    """
    h, w = 400, 600
    rng = np.random.default_rng(5)
    img = (1000 + slope * np.arange(w)[None, :]
           + rng.normal(0, 15.0, (h, w))).astype(np.float32)
    diff = st.estimate_background(img, 64, rms_method="diff").global_rms
    assert abs(diff - 15.0) / 15.0 < 0.05, f"斜率 {slope}: σ={diff}"
    if slope > 0:
        mad = st.estimate_background(img, 64, rms_method="mad").global_rms
        assert mad > 3 * diff


def test_background_box_larger_than_image():
    """box 比图还大 → 退化成单块,插值全常量。"""
    img = np.full((40, 50), 7.0, dtype=np.float32)
    bg = st.estimate_background(img, 200)
    assert bg.grid_shape == (1, 1)
    assert bg.plane().shape == (40, 50)
    assert np.allclose(bg.plane(), 7.0)


def test_background_rows_matches_plane():
    img, _, _ = star_field(h=300, w=320, n=20, seed=9)
    bg = st.estimate_background(img, 64)
    full_b, full_r = bg.plane(), bg.rms_plane()
    b, r = bg.rows(100, 150)
    assert np.allclose(b, full_b[100:150])
    assert np.allclose(r, full_r[100:150])


def test_background_at_matches_plane():
    """``back_at``/``rms_at`` 与整面插值逐点一致,且支持广播。"""
    img, _, _ = star_field(h=260, w=300, n=15, seed=4)
    bg = st.estimate_background(img, 64)
    full_b, full_r = bg.plane(), bg.rms_plane()
    ys = np.array([0, 1, 77, 130, 259])
    xs = np.array([0, 5, 100, 299])
    assert np.allclose(bg.back_at(ys[:, None], xs[None, :]),
                       full_b[np.ix_(ys, xs)], atol=1e-4)
    assert np.allclose(bg.rms_at(ys[:, None], xs[None, :]),
                       full_r[np.ix_(ys, xs)], atol=1e-4)


def test_background_rms_floor():
    """整块死区(σ=0)会被下限兜住,不会让阈值退化成 0。"""
    rng = np.random.default_rng(7)
    img = (800 + rng.normal(0, 20.0, (256, 256))).astype(np.float32)
    img[:64, :64] = 800.0                      # 一整块常量
    bg = st.estimate_background(img, 64, rms_floor_frac=0.1)
    assert bg.rms_floor > 0
    assert float(bg.rms.min()) >= bg.rms_floor
    assert float(bg.rms_plane().min()) >= bg.rms_floor


def test_background_bad_input():
    with pytest.raises(ValueError):
        st.estimate_background(np.zeros((2, 2, 2, 2)), 8)
    with pytest.raises(ValueError):
        st.estimate_background(np.zeros((2, 2)), 8)
    with pytest.raises(ValueError):
        st.estimate_background(np.zeros((50, 50)), 1)
    with pytest.raises(ValueError):
        st.estimate_background(np.zeros((50, 50)), 16, rms_method="magic")


def test_background_cancel():
    with pytest.raises(InterruptedError):
        st.estimate_background(np.zeros((128, 128), np.float32), 32,
                               cancel=_Cancel())


# ================================================================= 行程 / 连通域


def test_extract_runs():
    mask = np.zeros((3, 8), dtype=bool)
    mask[0, 1:4] = True
    mask[0, 6] = True
    mask[2, 0:2] = True
    rows, ca, cb = st._extract_runs(mask, 10, 8)
    assert rows.tolist() == [10, 10, 12]
    assert ca.tolist() == [1, 6, 0]
    assert cb.tolist() == [4, 7, 2]


def test_label_two_blobs():
    mask = np.zeros((6, 10), dtype=bool)
    mask[1:3, 1:3] = True
    mask[4:6, 6:9] = True
    rows, ca, cb = st._extract_runs(mask, 0, 10)
    lab, k = st._label_runs(rows, ca, cb, 10)
    assert k == 2
    assert len(set(lab.tolist())) == 2


def test_label_diagonal_is_connected():
    """8-连通:只在对角相接的两块算同一个团。"""
    mask = np.zeros((4, 6), dtype=bool)
    mask[1, 1] = True
    mask[2, 2] = True
    mask[3, 3] = True
    rows, ca, cb = st._extract_runs(mask, 0, 6)
    lab, k = st._label_runs(rows, ca, cb, 6)
    assert k == 1


def test_label_gap_is_not_connected():
    """隔了一个空像素就不算连通。"""
    mask = np.zeros((4, 8), dtype=bool)
    mask[1, 1] = True
    mask[3, 3] = True
    rows, ca, cb = st._extract_runs(mask, 0, 8)
    _, k = st._label_runs(rows, ca, cb, 8)
    assert k == 2


def test_label_u_shape():
    """跨多行的 U 形是一个团(并查集要合并两条腿)。"""
    mask = np.zeros((6, 7), dtype=bool)
    mask[1:5, 1] = True
    mask[1:5, 5] = True
    mask[4, 1:6] = True
    rows, ca, cb = st._extract_runs(mask, 0, 7)
    _, k = st._label_runs(rows, ca, cb, 7)
    assert k == 1


def test_label_empty():
    e = np.empty(0, dtype=np.int32)
    lab, k = st._label_runs(e, e, e, 10)
    assert k == 0 and lab.size == 0


# ================================================================= 检测:位置


def test_single_star_centroid():
    img = np.zeros((200, 200), dtype=np.float32)
    add_star(img, 100.37, 99.62, 5e4, 1.5)
    rng = np.random.default_rng(2)
    img += 1000.0 + rng.normal(0, 10.0, img.shape)
    s = st.detect_stars(img, box=50, threshold=5.0)
    assert len(s) == 1
    assert abs(float(s.x[0]) - 100.37) < 0.05
    assert abs(float(s.y[0]) - 99.62) < 0.05
    assert bool(s.refined[0])


@pytest.mark.parametrize("dx", [0.0, 0.17, 0.33, 0.5, 0.71, 0.94])
def test_centroid_subpixel(dx):
    """亚像素质心精度 < 0.1 px(整个相位都要覆盖)。"""
    img = np.zeros((160, 160), dtype=np.float32)
    x0, y0 = 80.0 + dx, 79.0 + (0.5 - dx)
    add_star(img, x0, y0, 8e4, 1.6)
    rng = np.random.default_rng(int(dx * 1000) + 1)
    img += 500.0 + rng.normal(0, 8.0, img.shape)
    s = st.detect_stars(img, box=40, threshold=5.0)
    assert len(s) == 1
    assert math.hypot(float(s.x[0]) - x0, float(s.y[0]) - y0) < 0.1


def test_positions_and_container_protocol():
    img, xs, ys = star_field(n=30, seed=6)
    s = st.detect_stars(img, box=64)
    assert bool(s) is True and len(s) == s.positions.shape[0]
    assert s.positions.shape[1] == 2
    half = s.select(np.arange(len(s)) < len(s) // 2)
    assert len(half) == len(s) // 2
    assert half.shape == s.shape and half.threshold == s.threshold
    empty = s.select(np.zeros(len(s), dtype=bool))
    assert len(empty) == 0 and bool(empty) is False


# ================================================================= 检测:形状


@pytest.mark.parametrize("sigma", [1.2, 1.5, 2.0, 3.0])
def test_fwhm_recovery(sigma):
    """孔径细化后 FWHM 还原误差 < 5%。"""
    img = np.zeros((220, 220), dtype=np.float32)
    add_star(img, 110.3, 109.8, 4e5, sigma)
    rng = np.random.default_rng(int(sigma * 10))
    img += 900.0 + rng.normal(0, 9.0, img.shape)
    s = st.detect_stars(img, box=55, threshold=5.0)
    assert len(s) == 1
    truth = st.FWHM_PER_SIGMA * sigma
    assert abs(float(s.fwhm[0]) - truth) / truth < 0.05


def test_refine_fixes_isophotal_bias():
    """不细化的等照度矩会**系统性低估** FWHM;细化必须把它拉回来。"""
    img = np.zeros((220, 220), dtype=np.float32)
    add_star(img, 110.0, 110.0, 3e4, 2.0)
    rng = np.random.default_rng(21)
    img += 900.0 + rng.normal(0, 9.0, img.shape)
    truth = st.FWHM_PER_SIGMA * 2.0
    raw = st.detect_stars(img, box=55, refine=False)
    fine = st.detect_stars(img, box=55, refine=True)
    assert not bool(raw.refined[0]) and bool(fine.refined[0])
    # 峰值/阈值 ≈ 26 ⇒ 理论截断系数 f(ln26)=0.87 ⇒ σ 低估约 6.6%
    assert float(raw.fwhm[0]) < truth * 0.96
    assert abs(float(fine.fwhm[0]) - truth) < abs(float(raw.fwhm[0]) - truth)
    assert abs(float(fine.fwhm[0]) - truth) / truth < 0.06


def test_round_star_is_round():
    img = np.zeros((200, 200), dtype=np.float32)
    add_star(img, 100.0, 100.0, 3e5, 2.2)
    rng = np.random.default_rng(31)
    img += 700.0 + rng.normal(0, 8.0, img.shape)
    s = st.detect_stars(img, box=50)
    assert float(s.eccentricity[0]) < 0.2
    assert float(s.ellipticity[0]) < 0.03
    assert abs(float(s.sigma_major[0]) - float(s.sigma_minor[0])) < 0.1


def test_ellipticity_recovered():
    """注入 3:2 椭圆 → 轴比 / 偏心率还原。"""
    img = np.zeros((200, 200), dtype=np.float32)
    add_ellipse(img, 100.0, 100.0, 3e5, 3.0, 2.0, 0.0)
    rng = np.random.default_rng(41)
    img += 700.0 + rng.normal(0, 8.0, img.shape)
    s = st.detect_stars(img, box=50, max_eccentricity=None)
    a, b = float(s.sigma_major[0]), float(s.sigma_minor[0])
    assert abs(a - 3.0) < 0.15 and abs(b - 2.0) < 0.15
    assert abs(float(s.eccentricity[0]) - math.sqrt(1 - (2 / 3) ** 2)) < 0.05
    assert abs(float(s.ellipticity[0]) - (1 - 2 / 3)) < 0.05


@pytest.mark.parametrize("deg", [0.0, 30.0, 75.0, 120.0, 170.0])
def test_position_angle(deg):
    """长轴方位角还原(含 0/180 环绕)。"""
    img = np.zeros((200, 200), dtype=np.float32)
    add_ellipse(img, 100.0, 100.0, 3e5, 3.2, 1.8, deg)
    rng = np.random.default_rng(int(deg) + 51)
    img += 700.0 + rng.normal(0, 8.0, img.shape)
    s = st.detect_stars(img, box=50, max_eccentricity=None)
    got = float(s.theta[0])
    err = abs((got - deg + 90.0) % 180.0 - 90.0)      # 轴向,mod 180
    assert err < 2.0, f"theta {got} vs {deg}"


def test_theta_concentration_flags_trailing():
    """全场朝同一方向拉长 → ``theta_r`` 接近 1;圆星场 → 接近 0。

    这就是"星点形状是独立证据链"的落点:真机 M 16 那一帧实测
    ecc 中位 0.91、``theta_r`` 0.88,直接把跟踪拖线摆在脸上。
    """
    rng = np.random.default_rng(61)
    trail = np.zeros((400, 500), dtype=np.float32)
    round_ = np.zeros((400, 500), dtype=np.float32)
    xs = rng.uniform(30, 470, 60)
    ys = rng.uniform(30, 370, 60)
    for x, y in zip(xs, ys):
        add_ellipse(trail, x, y, 2e5, 3.0, 1.2, 40.0)
        add_star(round_, x, y, 2e5, 2.0)
    trail += 800 + rng.normal(0, 9.0, trail.shape)
    round_ += 800 + rng.normal(0, 9.0, round_.shape)
    st_trail = st.detect_stars(trail, box=64).stats()
    st_round = st.detect_stars(round_, box=64).stats()
    assert st_trail["theta_r"] > 0.85
    assert abs((st_trail["theta_median"] - 40.0 + 90) % 180 - 90) < 5.0
    assert st_round["theta_r"] < 0.4
    assert st_trail["eccentricity_median"] > 0.85
    assert st_round["eccentricity_median"] < 0.35


# ================================================================= 检测:过滤


def test_detection_rate_under_gradient():
    """强天光梯度下检出率仍 ≥ 90%(且位置准)。"""
    h, w, n = 600, 800, 100
    rng = np.random.default_rng(71)
    img = np.zeros((h, w), dtype=np.float32)
    xs = rng.uniform(40, w - 40, n)
    ys = rng.uniform(40, h - 40, n)
    for x, y, f in zip(xs, ys, rng.uniform(4e4, 3e5, n)):
        add_star(img, x, y, f, 1.7)
    img += (400 + 5000 * (np.arange(w)[None, :] / w)
            + 3000 * (np.arange(h)[:, None] / h))
    img += rng.normal(0, 12.0, img.shape)
    s = st.detect_stars(img, box=64, threshold=5.0)
    d = np.hypot(np.asarray(s.x)[:, None] - xs[None, :],
                 np.asarray(s.y)[:, None] - ys[None, :])
    hit = (d.min(axis=0) < 0.5).sum()
    assert hit >= 0.90 * n, f"只命中 {hit}/{n}"


def test_hot_pixels_rejected():
    """2×2 的热像素团比真星锐得多 → 被 hot 规则剔掉。"""
    rng = np.random.default_rng(81)
    img = np.zeros((400, 400), dtype=np.float32)
    for x, y in zip(rng.uniform(40, 360, 40), rng.uniform(40, 360, 40)):
        add_star(img, x, y, 1e5, 1.8)
    hot_xy = [(60, 300), (120, 320), (200, 60), (330, 200), (250, 350)]
    for x, y in hot_xy:
        img[y:y + 2, x:x + 2] += 3e4
    img += 600 + rng.normal(0, 10.0, img.shape)
    s = st.detect_stars(img, box=64)
    assert s.rejects.get("hot", 0) >= 4
    for x, y in hot_xy:
        d = np.hypot(np.asarray(s.x) - (x + 0.5), np.asarray(s.y) - (y + 0.5))
        assert d.min() > 2.0, f"热像素 ({x},{y}) 没被剔掉"


def test_cosmic_ray_streak_rejected():
    """一条 1 像素宽的细迹(宇宙线/卫星)被 elongated 规则剔掉。"""
    rng = np.random.default_rng(91)
    img = np.zeros((400, 400), dtype=np.float32)
    for x, y in zip(rng.uniform(40, 360, 40), rng.uniform(40, 360, 40)):
        add_star(img, x, y, 1e5, 1.8)
    img[200, 100:118] += 2.5e4
    img += 600 + rng.normal(0, 10.0, img.shape)
    s = st.detect_stars(img, box=64)
    assert s.rejects.get("elongated", 0) >= 1
    d = np.hypot(np.asarray(s.x) - 108.5, np.asarray(s.y) - 200.0)
    assert d.min() > 3.0


def test_elongation_cut_is_adaptive():
    """整场被拖成 2.5:1 时,自适应门限必须留住星;关掉自适应就会全军覆没。

    回归真机 M 16:固定 0.8 会剔掉 95% 的星(含全部亮星)。
    """
    rng = np.random.default_rng(101)
    img = np.zeros((400, 500), dtype=np.float32)
    n = 60
    for x, y in zip(rng.uniform(30, 470, n), rng.uniform(30, 370, n)):
        add_ellipse(img, x, y, 2e5, 3.0, 1.2, 35.0)
    img += 800 + rng.normal(0, 9.0, img.shape)
    adaptive = st.detect_stars(img, box=64)                     # elong_ratio=0.65
    fixed = st.detect_stars(img, box=64, elong_ratio=None)      # 只用绝对 0.8
    assert len(adaptive) >= 0.8 * n
    assert len(fixed) < 0.2 * n
    assert fixed.rejects.get("elongated", 0) > adaptive.rejects.get("elongated", 0)


def test_saturation_flag_and_reject():
    img = np.zeros((200, 200), dtype=np.float32)
    add_star(img, 60.0, 60.0, 5e4, 2.0)
    add_star(img, 140.0, 140.0, 5e6, 2.0)
    rng = np.random.default_rng(111)
    img += 500 + rng.normal(0, 8.0, img.shape)
    u16 = np.clip(img, 0, 65535).astype(np.uint16)
    raw = st.detect_stars(u16, box=50, apply_filters=False)
    assert int(np.asarray(raw.saturated).sum()) == 1
    kept = st.detect_stars(u16, box=50)
    assert kept.rejects.get("saturated", 0) == 1
    assert all(not bool(v) for v in kept.saturated)
    # 显式给满量程也要生效
    f = st.detect_stars(img, box=50, saturation=1e5, sat_frac=0.5,
                        apply_filters=False)
    assert int(np.asarray(f.saturated).sum()) == 1


def test_float_input_has_no_saturation_guess():
    """浮点图猜不出满量程 → 一律不标饱和(而不是把最亮那颗误判成饱和)。"""
    img = np.zeros((160, 160), dtype=np.float32)
    add_star(img, 80.0, 80.0, 1e6, 2.0)
    rng = np.random.default_rng(112)
    img += 400 + rng.normal(0, 6.0, img.shape)
    s = st.detect_stars(img, box=40, apply_filters=False)
    assert not np.asarray(s.saturated).any()


def test_edge_stars_rejected():
    img = np.zeros((200, 200), dtype=np.float32)
    add_star(img, 100.0, 100.0, 1e5, 1.8)
    add_star(img, 1.0, 100.0, 1e5, 1.8)          # 贴左边
    add_star(img, 100.0, 198.5, 1e5, 1.8)        # 贴下边
    rng = np.random.default_rng(121)
    img += 500 + rng.normal(0, 8.0, img.shape)
    raw = st.detect_stars(img, box=50, apply_filters=False)
    assert int(np.asarray(raw.edge).sum()) == 2
    kept = st.detect_stars(img, box=50)
    assert len(kept) == 1 and kept.rejects.get("edge", 0) == 2
    assert abs(float(kept.x[0]) - 100.0) < 0.2


def test_min_and_max_pixels():
    img, _, _ = star_field(n=40, flux=(2e3, 3e5), seed=131)
    raw = st.detect_stars(img, box=64, apply_filters=False)
    npix = np.asarray(raw.npix)
    cut = int(np.median(npix))
    assert npix.min() < cut < npix.max()
    small = st.filter_stars(raw, min_pixels=cut, max_eccentricity=None,
                            reject_hot=False)
    assert np.asarray(small.npix).min() >= cut
    assert small.rejects.get("too_small", 0) > 0
    capped = st.filter_stars(raw, min_pixels=1, max_pixels=cut,
                             max_eccentricity=None, reject_hot=False)
    assert np.asarray(capped.npix).max() <= cut
    assert capped.rejects.get("too_big", 0) > 0


def test_min_snr_filter():
    img, _, _ = star_field(n=50, flux=(3e3, 3e5), seed=141)
    raw = st.detect_stars(img, box=64, apply_filters=False)
    cut = float(np.median(np.asarray(raw.snr)))
    kept = st.filter_stars(raw, min_snr=cut, max_eccentricity=None,
                           reject_hot=False, min_pixels=1)
    assert np.asarray(kept.snr).min() >= cut
    assert kept.rejects.get("low_snr", 0) > 0


def test_reject_counts_add_up():
    """每颗被剔的星只记一次,保留数 + 剔除数 == 团块总数。"""
    img, _, _ = star_field(n=60, flux=(2e3, 3e5), seed=151)
    s = st.detect_stars(img, box=64, max_pixels=400)
    assert len(s) + sum(s.rejects.values()) == s.n_blobs


# ================================================================= 退化输入


def test_empty_image():
    s = st.detect_stars(np.zeros((128, 128), dtype=np.float32), box=32)
    assert len(s) == 0 and s.n_blobs == 0
    assert s.stats() == {"n": 0.0}
    assert s.brightest(5).x.size == 0
    assert s.sorted_by_flux().x.size == 0
    assert s.fwhm_arcsec is None


def test_all_saturated_image():
    img = np.full((160, 160), 65535, dtype=np.uint16)
    s = st.detect_stars(img, box=40)
    assert len(s) == 0


def test_pure_noise_image():
    """纯噪声在 5σ 上几乎不该出东西(26 万像素期望 < 1 个)。"""
    rng = np.random.default_rng(161)
    img = (2000 + rng.normal(0, 25.0, (512, 512))).astype(np.float32)
    s = st.detect_stars(img, box=64, threshold=5.0, apply_filters=False)
    assert s.n_blobs <= 5
    assert len(st.detect_stars(img, box=64, threshold=5.0)) == 0


def test_constant_image_no_detection():
    img = np.full((128, 128), 300.0, dtype=np.float32)
    assert len(st.detect_stars(img, box=32)) == 0


# ================================================================= API 行为


def test_brightest_ordering():
    img = np.zeros((300, 300), dtype=np.float32)
    fluxes = [3e5, 2e5, 1e5, 6e4, 3e4]
    pos = [(50, 50), (150, 50), (250, 50), (50, 200), (200, 200)]
    for (x, y), f in zip(pos, fluxes):
        add_star(img, x, y, f, 1.8)
    rng = np.random.default_rng(171)
    img += 600 + rng.normal(0, 8.0, img.shape)
    s = st.detect_stars(img, box=60)
    assert len(s) == 5
    assert np.all(np.diff(np.asarray(s.flux)) <= 0)     # 默认已按流量降序
    top3 = st.brightest(s, 3)
    assert len(top3) == 3
    assert np.all(np.diff(np.asarray(top3.flux)) <= 0)
    assert abs(float(top3.x[0]) - 50) < 0.3 and abs(float(top3.y[0]) - 50) < 0.3
    assert len(s.brightest(99)) == 5
    assert len(s.brightest(0)) == 0


def test_max_stars_truncates():
    img, _, _ = star_field(n=50, seed=181)
    full = st.detect_stars(img, box=64)
    cut = st.detect_stars(img, box=64, max_stars=12)
    assert len(full) > 12 and len(cut) == 12
    assert np.allclose(np.asarray(cut.flux), np.asarray(full.flux)[:12])


def test_pixel_scale_arcsec():
    img, _, _ = star_field(n=20, seed=191)
    s = st.detect_stars(img, box=64, pixel_scale=3.85)
    assert np.allclose(s.fwhm_arcsec, np.asarray(s.fwhm) * 3.85)
    assert abs(s.stats()["fwhm_arcsec_median"]
               - s.stats()["fwhm_median"] * 3.85) < 1e-3


def test_reuse_background_and_shape_check():
    img, _, _ = star_field(n=25, seed=201)
    bg = st.estimate_background(img, 64)
    a = st.detect_stars(img, background=bg)
    b = st.detect_stars(img, box=64)
    assert len(a) == len(b)
    assert np.allclose(np.asarray(a.x), np.asarray(b.x))
    with pytest.raises(ValueError):
        st.detect_stars(np.zeros((100, 100), np.float32), background=bg)


def test_channel_selection():
    """三通道输入默认取绿(第 1 层)。"""
    rgb = np.zeros((200, 200, 3), dtype=np.float32)
    add_star(rgb[:, :, 1], 100.0, 100.0, 2e5, 1.8)
    add_star(rgb[:, :, 0], 40.0, 40.0, 2e5, 1.8)
    rng = np.random.default_rng(211)
    rgb += 500 + rng.normal(0, 8.0, rgb.shape)
    green = st.detect_stars(rgb, box=50)
    assert len(green) == 1 and abs(float(green.x[0]) - 100.0) < 0.2
    red = st.detect_stars(rgb, box=50, channel=0)
    assert len(red) == 1 and abs(float(red.x[0]) - 40.0) < 0.2
    with pytest.raises(ValueError):
        st.detect_stars(rgb, channel=7)


def test_bad_threshold_and_runaway():
    img, _, _ = star_field(n=40, seed=221)
    with pytest.raises(ValueError):
        st.detect_stars(img, threshold=-1.0)
    with pytest.raises(ValueError):
        st.detect_stars(img, threshold=float("nan"))
    with pytest.raises(ValueError, match="行程数"):
        st.detect_stars(img, box=64, threshold=0.0, max_runs=10)
    with pytest.raises(ValueError, match="阈值以上像素"):
        st.detect_stars(img, box=64, threshold=0.0,
                        max_threshold_pixels=100)
    with pytest.raises(ValueError, match="必须为正数"):
        st.detect_stars(img, max_threshold_pixels=0)


def test_perfectly_smooth_background_has_nonzero_rms_floor():
    """全零二阶差分也必须有数值下限，不能让 threshold×σ 退化为 0。"""
    x = np.linspace(1000.0, 1100.0, 256, dtype=np.float32)
    img = np.broadcast_to(x, (256, 256)).copy()
    bg = st.estimate_background(img, 64)
    assert bg.rms_floor > 0.0
    assert float(bg.rms.min()) >= bg.rms_floor


def test_detect_cancel():
    img, _, _ = star_field(n=20, seed=231)
    with pytest.raises(InterruptedError):
        st.detect_stars(img, box=64, cancel=_Cancel())


def test_stats_fields():
    img, _, _ = star_field(n=45, seed=241)
    s = st.detect_stars(img, box=64, pixel_scale=2.0)
    d = s.stats()
    for key in ("n", "fwhm_median", "fwhm_mad", "ellipticity_median",
                "eccentricity_median", "flux_median", "snr_median",
                "background_median", "noise_median", "theta_median", "theta_r"):
        assert key in d and math.isfinite(d[key])
    assert d["n"] == len(s)
    assert 0.0 <= d["theta_median"] < 180.0
    assert 0.0 <= d["theta_r"] <= 1.0


# ================================================================= 超像素


@pytest.mark.parametrize("pat,cells", [
    ("RGGB", [[10, 20], [30, 40]]),
    ("BGGR", [[10, 20], [30, 40]]),
    ("GRBG", [[10, 20], [30, 40]]),
    ("GBRG", [[10, 20], [30, 40]]),
])
def test_green_superpixel_patterns(pat, cells):
    """绿平面 = 两个绿位的平均;四种相位各取对角的那一对。"""
    cell = np.array(cells, dtype=np.uint16)
    raw = np.tile(cell, (4, 3))
    g = st.green_superpixel(raw, pat)
    assert g.shape == (4, 3) and g.dtype == np.uint16
    want = (20 + 30) // 2 if pat in ("RGGB", "BGGR") else (10 + 40) // 2
    assert np.all(g == want)


def test_green_superpixel_no_overflow():
    """两个 65535 的绿位平均出来还是 65535(整数相加必须先加宽)。"""
    raw = np.full((4, 4), 65535, dtype=np.uint16)
    assert np.all(st.green_superpixel(raw, "RGGB") == 65535)


def test_green_superpixel_odd_size_and_errors():
    raw = np.zeros((7, 9), dtype=np.uint16)
    assert st.green_superpixel(raw, "RGGB").shape == (3, 4)
    with pytest.raises(ValueError):
        st.green_superpixel(raw, "CYGM")
    with pytest.raises(ValueError):
        st.green_superpixel(np.zeros((4, 4, 3), np.uint16), "RGGB")


def test_green_superpixel_keeps_star_centroid():
    """真星在超像素平面上的质心 = 全分辨率质心 / 2(不带半像素偏置)。"""
    full = np.zeros((256, 256), dtype=np.float32)
    add_star(full, 128.5, 100.5, 4e5, 3.0)
    rng = np.random.default_rng(251)
    cfa = np.clip(full * 0.5 + 2000 + rng.normal(0, 20.0, full.shape),
                  0, 65535).astype(np.uint16)
    g = st.green_superpixel(cfa, "RGGB")
    s = st.detect_stars(g, box=32)
    assert len(s) == 1
    assert abs(float(s.x[0]) - (128.5 - 0.5) / 2) < 0.15
    assert abs(float(s.y[0]) - (100.5 - 0.5) / 2) < 0.15


# ================================================================= 性能


def test_performance_full_frame():
    """6248×4176(ASIAIR 全幅)必须在 2 秒左右跑完;这里留足余量防抖。"""
    h, w, n = 4176, 6248, 500
    rng = np.random.default_rng(999)
    img = rng.normal(3350, 95, (h, w)).astype(np.float32)
    for x, y, f in zip(rng.uniform(20, w - 20, n), rng.uniform(20, h - 20, n),
                       10 ** rng.uniform(3.5, 5.5, n)):
        add_star(img, x, y, f, 1.6, rad=7)
    img = np.clip(img, 0, 65535).astype(np.uint16)

    t0 = time.perf_counter()
    s = st.detect_stars(img, box=64, threshold=5.0)
    elapsed = time.perf_counter() - t0
    assert len(s) > 0.6 * n, f"只提到 {len(s)}/{n}"
    assert elapsed < 6.0, f"全幅提星用了 {elapsed:.2f}s(基线约 1.0s)"


def test_performance_background_only():
    rng = np.random.default_rng(998)
    img = rng.normal(3350, 95, (4176, 6248)).astype(np.uint16)
    t0 = time.perf_counter()
    bg = st.estimate_background(img, 64)
    elapsed = time.perf_counter() - t0
    assert bg.grid_shape[0] > 50 and bg.grid_shape[1] > 80
    assert elapsed < 3.0, f"背景估计用了 {elapsed:.2f}s(基线约 0.42s)"
