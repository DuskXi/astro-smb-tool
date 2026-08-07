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
        于是进度条走到 159 MB 之后**跳回 "0/0 MB"**,看着像出错了。"""
        src = inspect.getsource(catalog._build_from_upstream)
        assert "progress(100, 100)" not in src
        assert "progress(UPSTREAM_BYTES, UPSTREAM_BYTES)" in src

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
    """替身 `catalog_build`:不联网,但**按真实形状**回调进度。"""

    def __init__(self, on_progress):
        self._on_progress = on_progress

    def download_parts(self, work, progress=None):
        if progress is not None:
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
