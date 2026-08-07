"""跑测试时,应用数据目录必须是临时的。

**这条门禁看的是 conftest 的自动隔离本身。** 别指望各条测试自己 patch ——
一次真事故就是这么来的:一条新测试想隔离 `devices.json`,patch 的却是两个
根本不存在的名字(`devices._path` / `devices.DEVICES_FILE`,真入口是
`devices.devices_path`),`raising=False` 把这事儿咽了。测试全绿,真正写进去
的是 `%LOCALAPPDATA%/AstroSmbTool/devices.json`,给用户的设备列表塞了两条
垃圾记录。

patch 错名字不会报错,所以防线得架在环境变量那一层,并且由这个文件盯着。
"""
from __future__ import annotations

from pathlib import Path

from astro_smb import paths


def _under(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


class TestAppDataIsRedirected:

    def test_data_dir_is_temporary(self, tmp_path_factory):
        base = tmp_path_factory.getbasetemp()
        got = paths.data_dir()
        assert _under(got, base), (
            f"测试正在往真实数据目录写: {got}\n"
            f"(应当落在 pytest 的临时目录 {base} 底下)")

    def test_cache_root_is_temporary(self, tmp_path_factory):
        base = tmp_path_factory.getbasetemp()
        got = paths.cache_root()
        assert _under(got, base), f"测试正在往真实缓存目录写: {got}"

    def test_devices_file_is_temporary(self, tmp_path_factory):
        from astro_smb import devices as dv
        base = tmp_path_factory.getbasetemp()
        got = dv.devices_path()
        assert _under(got, base), (
            f"设备记录会写到用户真实的文件里: {got}")

    def test_writing_a_device_does_not_touch_the_user(self, tmp_path_factory):
        """真写一条,确认它落在临时目录而不是别处。"""
        from astro_smb import devices as dv
        dv.remember("203.0.113.9", "钉门禁用", connected=False)
        assert any(r["host"] == "203.0.113.9" for r in dv.load())
        assert _under(dv.devices_path(), tmp_path_factory.getbasetemp())


class TestAllThreePlatformsAreCovered:
    """三个平台各读各的变量,而测试只在其中一个平台上跑。

    **这是一条查源码的检查,写明白免得被当成行为测试。** Windows 上
    `cache_root()` 就等于 `data_dir()`(都读 `LOCALAPPDATA`),所以把
    `XDG_CACHE_HOME` 那行删掉,上面几条行为断言全都照绿 —— 那是**等价变异**
    (docs/DEVELOPMENT.md §9b),不是覆盖空洞。可它在 Linux 上一点都不等价:CI 一跑
    就会往真实的 `~/.cache/AstroSmbTool` 里写。所以这一条盯的是"意图齐不齐"。
    """

    def test_fixture_sets_every_platforms_variable(self):
        src = (Path(__file__).resolve().parent
               / "conftest.py").read_text(encoding="utf-8")
        at = src.index("def _isolate_app_data")
        block = src[at:src.index("\ndef ", at + 10)]
        for var in ("LOCALAPPDATA", "XDG_DATA_HOME", "XDG_CACHE_HOME",
                    "HOME", "USERPROFILE"):
            assert f'"{var}"' in block, (
                f"隔离没覆盖 {var} —— 换个平台跑就会写用户真实目录")


class TestTheEscapeHatchExists:
    """要读真实星表那种测试得有个显式出口,而不是把隔离整个关掉。"""

    def test_marker_is_registered(self, pytestconfig):
        markers = pytestconfig.getini("markers")
        assert any(m.startswith("real_app_data") for m in markers), (
            "real_app_data 标记没注册 —— 用它的测试会吃 PytestUnknownMarkWarning")
