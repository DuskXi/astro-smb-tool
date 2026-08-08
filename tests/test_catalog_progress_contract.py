"""星表下载的进度契约:签名和单位两边必须对得上。

用户报"新机器上星表没有自动下载"。查下来有两层:

1. Qt 前端从来没查过星表是否就绪(另见 `test_qt_catalog_offer.py`);
2. **更根本的一层在核心库**:`catalog._build_from_upstream` 里的
   `dl_progress` 只收两个参数,而 `catalog_build.download_parts` 调的是
   ``progress(i + 1, len(parts), name, cached)`` —— **四个**。
   于是**第一次回调就 TypeError**,`ensure_catalog` 直接抛出去。
   星表在**任何前端上都下不下来**,老 UI 也一样。

单位也曾对不上:调用方给的是"第几个分片 / 共几个",而两套前端都把
`progress(done, total)` 当**字节**渲染成 MB —— 直接透传的话界面上写的是
"1/20 MB"。

这两件事都属于"不报错就看不出来"的反面:**一报错就整个功能没了**,
而它偏偏只在**没有星表的机器**上才走到 —— 开发机上星表早就有了,
`ensure_catalog` 在第一个 `if dest.is_file()` 就返回,永远走不到这段。
所以这份闸门把那条路**强制**走一遍。
"""
from __future__ import annotations

import inspect

import pytest

from astro_smb import catalog, catalog_build


class TestTheTwoSidesAgree:

    def test_download_parts_still_reports_four_things(self):
        """契约的另一端。它要是改回两个参数,下面那些用例就该跟着改。"""
        src = inspect.getsource(catalog_build.download_parts)
        assert "progress(i + 1, len(parts), name, True)" in src

    def test_the_handler_accepts_that_shape(self):
        """**直接按调用方的形状调一次。**

        只断言"函数签名有四个参数"挡不住把顺序写反;这里走的是真实调用。
        """
        called = []
        fake = _FakeBuild(on_progress=lambda p: p(3, 20, "tyc2.dat.03.gz",
                                                  False))
        _run_ensure(fake, progress=lambda d, t: called.append((d, t)))
        assert called, "一次进度都没有 —— 回调压根没被调到"

    def test_progress_is_in_bytes_not_part_counts(self):
        """前端渲染的是 ``{done/(1<<20):.0f} MB``。透传分片序号的话,
        用户看到的是"3/20 MB" —— 数字荒唐,而且永远不会走到 159。"""
        seen = []
        fake = _FakeBuild(on_progress=lambda p: p(3, 20, "x.gz", False))
        _run_ensure(fake, progress=lambda d, t: seen.append((d, t)))
        done, total = seen[0]
        assert total == catalog.UPSTREAM_BYTES
        # 20 个分片里的第 3 个,下载占 0~85%
        assert 0 < done < total
        assert done == pytest.approx(total * 0.85 * 3 / 20, rel=0.02)

    def test_the_download_phase_ends_at_85_percent(self):
        """下载段收尾也得是**字节**。

        (替身构建出来的不是真星表,收尾校验会先抛 —— 所以这里能看到的
        最后一次回调就是下载段那 85%,再往后的 100% 用下一条盯。)
        """
        seen = []
        fake = _FakeBuild(on_progress=lambda p: p(20, 20, "x.gz", False))
        _run_ensure(fake, progress=lambda d, t: seen.append((d, t)))
        assert seen[-1] == (int(catalog.UPSTREAM_BYTES * 0.85),
                            catalog.UPSTREAM_BYTES)

    def test_completion_does_not_switch_units(self):
        """最后那一下曾经是 `progress(100, 100)` —— 前端按 MB 渲染,
        于是进度条走到 159 MB 之后**跳回 "0/0 MB"**,看着像出错了。

        **验行为,不验源码长什么样。** 上一版查的是源码里有没有那串
        字面量;后来所有回报都收口到 `report()` 里,字面量自然不在了,
        而这条只会红给你看 —— 它盯的从来就不是那行字。
        """
        seen = []
        fake = _FakeBuild(on_progress=lambda p: p(20, 20, "x.gz", False))
        _run_ensure(fake, progress=lambda d, t: seen.append((d, t)))
        assert seen, "一次进度都没有"
        assert all(t == catalog.UPSTREAM_BYTES for _d, t in seen), (
            f"分母中途换了单位: {seen}")
        assert max(d for d, _t in seen) <= catalog.UPSTREAM_BYTES

    def test_cancel_aborts_during_the_download_not_after_it(self):
        """**取消要在回调里就生效。**

        `_build_from_upstream` 在 `download_parts` **返回之后**还有一道
        cancel 检查,所以哪怕回调里那道被删掉,"点了取消最终会抛"这件事
        照样成立 —— 只是要等 20 个分片全下完,好几分钟。
        (第一版就这么写的,变异活了下来。)

        所以这里不看"抛没抛",看的是 **`download_parts` 有没有被打断**:
        回调里生效的话,异常从 `progress(...)` 里直接穿出去,
        它后面那行就永远执行不到。
        """
        import threading

        cancel = threading.Event()
        cancel.set()
        reached_end = []

        def on_progress(p):
            p(1, 20, "x.gz", False)
            reached_end.append(True)      # 取消若在回调里生效,到不了这行

        fake = _FakeBuild(on_progress=on_progress)
        with pytest.raises(catalog.CatalogError):
            _run_ensure(fake, progress=None, cancel=cancel)
        assert not reached_end, "取消没有打断下载,要等 20 个分片全下完"


class _FakeBuild:
    """替身 `catalog_build`:不联网,但**按真实形状**回调进度。

    两个回调都要有。只留 `progress` 的话,"下载过程中每 0.25 秒报一次
    字节数"那条路一次都走不到,而它正是界面上数字动不动的原因。
    """

    def __init__(self, on_progress=None, on_bytes=None):
        self._on_progress = on_progress
        self._on_bytes = on_bytes

    def download_parts(self, work, progress=None, on_bytes=None):
        if on_bytes is not None and self._on_bytes is not None:
            self._on_bytes(on_bytes)
        if progress is not None and self._on_progress is not None:
            self._on_progress(progress)
        return []

    def build(self, work, tmp):
        tmp.write_bytes(b"x")
        return _Hdr()


class _Hdr:
    count = 0


def _run_ensure(fake, *, progress, cancel=None):
    """把 `_build_from_upstream` 那条路**强制**走一遍。

    开发机上星表早就有了,`ensure_catalog` 在第一个 `is_file()` 就返回 ——
    这段代码平时根本走不到,所以直接调内部函数。

    **替身要装在包属性上,不是 `sys.modules` 上。** 被测函数里写的是
    ``from astro_smb import catalog_build as CB`` —— 那读的是 `astro_smb`
    这个**包对象的属性**。只往 `sys.modules` 里塞一个同名模块的话替身
    根本不生效,测试会**真的去 CDS 下 159 MB**(第一版就这样,跑了 121 秒
    才发现数字对不上)。
    """
    import tempfile
    import types
    from pathlib import Path

    import astro_smb

    mod = types.ModuleType("astro_smb.catalog_build")
    mod.download_parts = fake.download_parts
    mod.build = fake.build
    old = getattr(astro_smb, "catalog_build", None)
    astro_smb.catalog_build = mod
    try:
        with tempfile.TemporaryDirectory() as d:
            dest = Path(d) / "tycho2_test.bin"
            try:
                catalog._build_from_upstream(dest, progress=progress,
                                             cancel=cancel)
            except catalog.CatalogError:
                # 替身写出来的不是真星表,收尾校验必然抛 —— 那不关这条测试
                # 的事。**只有取消**那种 CatalogError 要放出去。
                if cancel is not None and cancel.is_set():
                    raise
            except Exception:              # noqa: BLE001 - 同上
                pass
    finally:
        if old is not None:
            astro_smb.catalog_build = old
        else:
            delattr(astro_smb, "catalog_build")


class TestProgressMovesWhileTheDownloadIsStillRunning:
    """**只按分片报进度 = 界面上只动 20 下。**

    每个分片约 8 MB;`download_parts` 原来只在**一个分片下完之后**才回调
    一次。于是慢链路上:点了「下载星表」之后好几十秒里屏幕上一个数字都
    没有(第一下要等第一个分片整个下完),之后每次跳 8 MB。
    用户看到的就是"没有进度条"。

    现在下载**过程中**每 0.25 秒按已落盘字节数报一次。
    """

    def test_bytes_arrive_before_any_part_finishes(self):
        seen = []
        fake = _FakeBuild(on_bytes=lambda cb: cb(3_000_000))
        _run_ensure(fake, progress=lambda d, t: seen.append((d, t)))
        assert seen, "下载过程中一次进度都没有 —— 界面只能干等"
        done, total = seen[0]
        assert total == catalog.UPSTREAM_BYTES
        assert 0 < done < total

    def test_it_is_proportional_to_the_bytes(self):
        """3 MB 和 30 MB 报出来的进度必须差十倍,不能是同一格。"""
        got = {}
        for n in (3_000_000, 30_000_000):
            seen = []
            fake = _FakeBuild(on_bytes=lambda cb, n=n: cb(n))
            _run_ensure(fake, progress=lambda d, t: seen.append(d))
            got[n] = seen[0]
        assert got[30_000_000] == pytest.approx(got[3_000_000] * 10, rel=0.02)

    def test_it_never_overshoots_the_download_band(self):
        """`UPSTREAM_BYTES` 只是估计值。真实总量偏大时不能冲过下载段,
        否则进度条会先到 85% 再退回来。"""
        seen = []
        huge = catalog.UPSTREAM_BYTES * 3
        fake = _FakeBuild(on_bytes=lambda cb: cb(huge))
        _run_ensure(fake, progress=lambda d, t: seen.append(d))
        assert seen[0] <= int(catalog.UPSTREAM_BYTES * 0.85)

    def test_cancel_works_from_the_byte_callback_too(self):
        """取消要在**这条**回调里也生效 —— 它才是下载过程中一直在跑的那个。
        只在分片回调里判的话,点了取消要等当前这个 8 MB 分片下完。"""
        import threading

        cancel = threading.Event()
        cancel.set()
        reached_end = []

        def on_bytes(cb):
            cb(1_000_000)
            reached_end.append(True)

        fake = _FakeBuild(on_bytes=on_bytes)
        with pytest.raises(catalog.CatalogError):
            _run_ensure(fake, progress=None, cancel=cancel)
        assert not reached_end, "取消没有打断下载"


class TestDownloadedBytesCountsWhatIsOnDisk:
    """字节数是**量文件**来的,不是解析 curl 的输出。

    curl 的进度写在 stderr 上、还被 `capture_output` 吞掉,拿不到;
    而文件大小随时能看,还天然把断点续传和已缓存的分片都算进去了。
    """

    def test_it_sums_the_parts(self, tmp_path):
        (tmp_path / "tyc2.dat.00.gz").write_bytes(b"a" * 100)
        (tmp_path / "tyc2.dat.01.gz").write_bytes(b"b" * 250)
        assert catalog_build.downloaded_bytes(tmp_path) == 350

    def test_it_ignores_everything_else(self, tmp_path):
        """构建产物、`.part`、别人的文件都不算 —— 算进去的话进度会超过 100%。"""
        (tmp_path / "tyc2.dat.00.gz").write_bytes(b"a" * 100)
        (tmp_path / "catalog.bin").write_bytes(b"x" * 9999)
        (tmp_path / "notes.txt").write_bytes(b"y" * 9999)
        assert catalog_build.downloaded_bytes(tmp_path) == 100

    def test_empty_dir_is_zero(self, tmp_path):
        assert catalog_build.downloaded_bytes(tmp_path) == 0
