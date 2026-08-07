"""pytest 全局配置。

**为什么需要这个文件**:GUI 层的测试要 ``import astro_smb_gui.<页面>``,而页面模块
在**模块顶层**就 ``from win32more...`` —— win32more 是 Windows 专属、且原来只挂在
``[project.optional-dependencies].winui`` 下,``uv sync`` 不装。结果是本机跑
``pytest tests/`` 时 **7 个模块直接 collection error,整轮被中断**,另有 122 个失败;
真实回归完全被噪音淹没(实测 1017 passed / 122 failed / 7 errors)。

``pytest.importorskip`` 放在测试文件里解决不了这个问题:它必须写在 ``import
astro_smb_gui`` **之前**才有用,而那些文件的 import 块本身就是失败点。所以在这里
用收集期钩子统一拦截 —— 缺 win32more 时把整个模块标成 skip,而不是 error。

win32more 现在也已进 ``[dependency-groups].dev``(带 ``sys_platform == 'win32'``
标记),所以 Windows 上正常开发时这条路径不会触发;它兜的是 Linux/macOS CI
与没装 extra 的干净环境。
"""
from __future__ import annotations

import importlib.util
import os

import pytest

#: 需要 win32more(直接或经 astro_smb_gui 页面模块传递)的测试模块。
#: 判据是"模块顶层 import 链上有 win32more",不是"文件里出现过这个词"。
_NEEDS_WIN32MORE = {
    "test_devicespage.py",
    "test_features.py",
    "test_fitsimage.py",
    "test_fitsview_solve_ui.py",
    "test_guidedash.py",
    "test_guiding_groups.py",
    "test_records_browser_draw.py",
    "test_records_lazy.py",
    "test_sky3d.py", "test_night_polar.py", "test_polar_plot.py",
    "test_sky3d_footprint.py",
    "test_space_monitor_draw.py",
}

_HAVE_WIN32MORE = importlib.util.find_spec("win32more") is not None


def pytest_ignore_collect(collection_path, config):  # noqa: ARG001
    """缺 win32more 时不去收集 GUI 相关模块(避免 import 期 error)。"""
    if _HAVE_WIN32MORE:
        return None
    if collection_path.name in _NEEDS_WIN32MORE:
        return True
    return None


#: 缺 PySide6 时**整轮变红**,而不是安静地少跑一半。
#:
#: 2026-08-05 才发现:`pyside6` 从来没进过任何依赖组,于是 `uv sync` 之后
#: 跑测试是 1353 条,带上它是 2669 条 —— **少跑的一半正是跨平台交付的那套
#: 前端**。每个 Qt 测试文件各自 `importorskip`,单看每一条都合理,合起来
#: 就是"CI 一直没测过交付物",而日志尾巴上的 `1353 passed` 和 `2669 passed`
#: 长得一模一样。
#:
#: win32more 那条仍然是 skip —— 它是 Windows 专属,别的平台装不上,跳过是
#: 事实。PySide6 三个平台都装得上,缺它就是环境坏了,不是平台差异。
#: **要真的 import 到 QtWidgets,不能只看包在不在。** Linux 上 PySide6 装得上
#: 而 `libEGL.so.1` / `libxkbcommon` 缺一个,`import PySide6` 照样成功(它只是
#: 个包),炸的是 `from PySide6.QtWidgets import …` —— 于是 `importorskip
#: ("PySide6")` **过**,而每个 Qt 测试各自 ImportError。那种红看起来像
#: "测试坏了",不像"CI 机器少装了两个 so"。
def _qt_is_usable() -> bool:
    if importlib.util.find_spec("PySide6") is None:
        return False
    try:
        import PySide6.QtWidgets       # noqa: F401
    except ImportError:
        return False
    return True


_HAVE_PYSIDE6 = _qt_is_usable()

#: 故意只跑核心库时用:`ASTRO_SMB_NO_QT=1 pytest tests/`
_QT_OPTIONAL = os.environ.get("ASTRO_SMB_NO_QT") == "1"


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_app_data: 这条测试要读用户真实的应用数据目录(如已下载的星表)")

    if not _HAVE_PYSIDE6 and not _QT_OPTIONAL:
        raise pytest.UsageError(
            "PySide6 用不了 —— 跨平台交付的那套前端(约 1300 条测试)会被整块"
            "跳过,而报告看起来一切正常。\n"
            "    uv sync --all-groups\n"
            "Linux 上装了还报这个:缺系统库,见 .github/workflows/ci.yml 里那步 apt。\n"
            "确实只想跑核心库:ASTRO_SMB_NO_QT=1 pytest tests/")

    # Linux CI 上没有 X11/Wayland,建 QWidget 会直接 abort(不是异常,是
    # 进程没了)。离屏平台插件是 Qt 自带的,不需要额外装东西。
    if not _HAVE_PYSIDE6 or os.name == "nt":
        return
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


#: 跑测试时用哪种语言。默认钉死源语言 —— **不跟开发机的环境走**。
#: 想做"换语言之后哪些断言会漂"的审计:`ASTRO_SMB_TEST_LANG=xx_PS pytest`
#: (先 `uv run python scripts/i18n_pseudo.py` 生成伪语言词表)。
_TEST_LANG = os.environ.get("ASTRO_SMB_TEST_LANG") or "zh_CN"


@pytest.fixture(autouse=True)
def _pin_language():
    """**测试语言钉死,不许跟着开发机的 locale 走。**

    `astro_smb.i18n` 在 import 时会按环境挑语言(`ASTRO_SMB_LANG`、`LANG`、
    Windows 的界面语言……)。也就是说**这套测试在一台 `LANG=en_US` 的机器上
    会红一片**,而代码一个字都没错 —— 那种红比不红更糟,它训练人忽略结果。

    这不是把语言无关性藏起来:
    * **判读**的语言无关性由 `TestJudgementDoesNotGoThroughDisplayText` 守着
      (它自己在用例里切语言);
    * **界面**漏没漏包由伪语言跑一遍看(`docs/architecture/i18n.md` §0.4)。
    这里钉死的只是"断言中文文案内容"的那一批 —— 它们本来就该在中文下跑。
    """
    from astro_smb import i18n

    before = i18n.current_language()
    i18n.set_language(_TEST_LANG)
    yield
    i18n.set_language(before)


@pytest.fixture(autouse=True)
def _isolate_app_data(request, tmp_path_factory, monkeypatch):
    """**测试一律不许碰用户真实的应用数据目录。**

    这条是补给一次真事故的:一条新测试想隔离 `devices.json`,patch 的却是
    `devices._path` / `devices.DEVICES_FILE` —— 两个都不存在的名字(真入口是
    `devices.devices_path`),而 `raising=False` 把这事儿咽了。测试全绿,
    实际写进去的是 `%LOCALAPPDATA%/AstroSmbTool/devices.json`,真给用户的
    设备列表塞了两条垃圾记录。

    单条测试自己 patch 是防不住这个的 —— patch 错名字不会报错。所以在这里
    从**环境变量**这一层拧掉:`paths.data_dir()`/`cache_root()` 三个平台各自
    读的就是这几个变量(外加 `Path.home()`)。

    需要真实数据目录的测试(比如要读已下载的星表)显式加 `real_app_data` 标记。
    """
    if request.node.get_closest_marker("real_app_data") is not None:
        return
    base = tmp_path_factory.mktemp("appdata")
    monkeypatch.setenv("LOCALAPPDATA", str(base))       # Windows
    monkeypatch.setenv("XDG_DATA_HOME", str(base / "data"))    # Linux
    monkeypatch.setenv("XDG_CACHE_HOME", str(base / "cache"))  # Linux
    monkeypatch.setenv("HOME", str(base))               # macOS 的 ~/Library
    monkeypatch.setenv("USERPROFILE", str(base))


def pytest_report_header(config):  # noqa: ARG001
    if not _HAVE_WIN32MORE:
        return (f"win32more 未安装 —— 跳过 {len(_NEEDS_WIN32MORE)} 个 GUI 测试模块"
                f"(Windows 上 `uv sync` 会自动装它)")
    return None
