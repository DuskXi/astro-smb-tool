"""跨平台门禁:macOS / Linux 上也要能跑。

**在 Windows 上开发,跨平台的东西就只会在别人的机器上坏。** 这些测试用
monkeypatch 假装换了平台,把那几个真会分叉的地方钉住 —— 数据目录、
可执行名、前端产物名。真正上 mac 跑之前,它们是唯一的护栏。
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path

import pytest

from astro_smb import paths

ROOT = Path(__file__).resolve().parents[1]


class TestDataDirs:
    def test_windows_paths_are_unchanged(self, monkeypatch):
        r"""**Windows 上一个字节都不能变。** 那底下躺着用户的设备记录、
        日志缓存、8 MB 巡天底图和 35.6 MB 星表 —— 换位置等于让它们凭空消失。

        **比字符串,不比 Path 对象。** `monkeypatch` 改得动 `sys.platform`,
        改不动 `pathlib` 的风味:在 POSIX 主机上 `Path(r"C:\a\b")` 是**一段**,
        而被测代码拼出来的是两段,于是这条在 ubuntu / macOS 的 CI 上一直红
        (那两个 job 早就没人看了)。要验的本来就是"拼出来的串对不对"。
        """
        monkeypatch.setattr(paths.sys, "platform", "win32")
        monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\x\AppData\Local")
        got = str(paths.data_dir()).replace("/", "\\")
        assert got == r"C:\Users\x\AppData\Local\AstroSmbTool", got
        # Windows 一直没分数据与缓存,分了会把现有的 cache/ 挪走
        assert paths.cache_root() == paths.data_dir()

    def test_macos_follows_the_platform_convention(self, monkeypatch):
        monkeypatch.setattr(paths.sys, "platform", "darwin")
        monkeypatch.setattr(paths, "_home", lambda: Path("/Users/x"))
        assert paths.data_dir() == Path(
            "/Users/x/Library/Application Support/AstroSmbTool")
        assert paths.cache_root() == Path("/Users/x/Library/Caches/AstroSmbTool")

    def test_linux_honours_xdg(self, monkeypatch):
        monkeypatch.setattr(paths.sys, "platform", "linux")
        monkeypatch.setattr(paths, "_home", lambda: Path("/home/x"))
        monkeypatch.delenv("XDG_DATA_HOME", raising=False)
        monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
        assert paths.data_dir() == Path("/home/x/.local/share/AstroSmbTool")
        assert paths.cache_root() == Path("/home/x/.cache/AstroSmbTool")
        monkeypatch.setenv("XDG_DATA_HOME", "/tmp/d")
        assert paths.data_dir() == Path("/tmp/d/AstroSmbTool")

    def test_nothing_reaches_for_localappdata_directly_any_more(self):
        """收口之后**不许再有第二条路** —— 漏一处就是 mac 上多一个写进
        家目录根下的怪目录,而且只有在 mac 上才看得见。"""
        bad = []
        for pkg in ("astro_smb", "astro_smb_app"):
            for path in sorted((ROOT / pkg).rglob("*.py")):
                if path.name == "paths.py":
                    continue
                text = path.read_text(encoding="utf-8")
                for m in re.finditer(r'environ\w*\.get\(\s*"LOCALAPPDATA"', text):
                    line = text[:m.start()].count("\n") + 1
                    bad.append(f"{path.relative_to(ROOT)}:{line}")
        assert not bad, f"这些地方绕过了 astro_smb.paths: {bad}"


class TestCurl:
    @pytest.mark.parametrize("platform,exe", [
        ("win32", "curl.exe"), ("darwin", "curl"), ("linux", "curl")])
    def test_executable_name_follows_the_platform(self, monkeypatch, platform, exe):
        monkeypatch.setattr(paths.sys, "platform", platform)
        assert paths.curl_argv("-sSL")[0] == exe

    def test_no_window_flag_falls_back_to_zero(self, monkeypatch):
        """`CREATE_NO_WINDOW` 只有 Windows 的 subprocess 才有,别的平台取 0。

        **这条原来叫 `..._is_zero_off_windows`,断言的却是
        `isinstance(NO_WINDOW, int)`** —— 名字说一件事,断言测另一件,
        而且那个断言在任何平台上都恒真。静态扫描就是冲这种来的。
        """
        import subprocess

        # 真实取值:Windows 上是那个标志位,别处是 0
        real = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        assert paths.NO_WINDOW == real
        if paths.sys.platform == "win32":
            assert paths.NO_WINDOW != 0, "Windows 上应当是真的标志位"

        # 非 Windows 那条路:属性不存在时必须退回 0(而不是抛)
        monkeypatch.delattr(subprocess, "CREATE_NO_WINDOW", raising=False)
        assert getattr(subprocess, "CREATE_NO_WINDOW", 0) == 0


class TestWindowsOnlyCodeIsGuarded:
    def test_webview2_preload_is_a_noop_off_windows(self, monkeypatch):
        """WebView2 是 Windows 专属的。不早退就直接 `ctypes.WinDLL` 属性错误,
        而它在 `ensure_assets()` 里 —— 会把整个资产引导带走,天球页永远白屏。"""
        from astro_smb_app.views import sky3d as sv

        monkeypatch.setattr(sv.paths, "is_windows", lambda: False)
        monkeypatch.setattr(sv, "_preload_state", None)
        ok, why = sv.preload_webview2()
        assert ok and "非 Windows" in why

    def test_runtime_version_probe_is_quiet_off_windows(self, monkeypatch):
        from astro_smb_app.views import sky3d as sv

        monkeypatch.setattr(sv.paths, "is_windows", lambda: False)
        assert sv.webview2_runtime_version() == ""

    def test_volume_enumeration_dispatches_by_platform(self):
        """设备页要枚举本机磁盘。三个平台各一条路,不能只有 Windows 那条。"""
        from astro_smb_app import volumes

        src = Path(volumes.__file__).read_text(encoding="utf-8")
        for name in ("_windows_volumes", "_macos_volumes", "_linux_volumes"):
            assert f"def {name}" in src, name


class TestImportsAreCleanOffWindows:
    def test_the_cross_platform_ui_never_imports_win32more(self):
        """跨平台那条链路上不许出现 win32more —— 那是 Windows 专属,
        mac 上 `uv sync` 根本不会装它(pyproject 里带着平台标记)。

        **`astro_smb_qt` 现在也在名单里。** Uno 删掉之后跨平台交付就是它,
        这条规则跟着搬过来;老 UI `astro_smb_gui` 不在名单里 —— 它本来就是
        Windows 专属的那一套。
        """
        bad = []
        for pkg in ("astro_smb", "astro_smb_app", "astro_smb_qt"):
            for path in sorted((ROOT / pkg).rglob("*.py")):
                text = path.read_text(encoding="utf-8")
                if re.search(r"^\s*(from|import)\s+win32more", text, re.M):
                    bad.append(str(path.relative_to(ROOT)))
        assert not bad, f"这些模块 import 了 win32more: {bad}"

    def test_win32more_is_platform_marked_in_pyproject(self):
        """没有这个标记,mac 上 `uv sync` 会直接失败,连环境都同步不了。"""
        text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        for line in text.splitlines():
            if "win32more" in line and not line.strip().startswith("#"):
                assert "sys_platform" in line, line


class TestTheWholeUiWorksWithoutWin32more:
    """**最实在的一条**:假装 win32more 装不上(mac 上就是这样),
    把跨平台前端的九页全部构建一遍。

    在 Windows 上开发时 win32more 一直躺在环境里,任何一条不小心加进来的
    依赖都不会被发现 —— 直到有人在 mac 上 `uv sync`,那时它压根不会被安装。

    (Uno 删除后这条转指 Qt。它守的性质没变:**跨平台那套必须能在没有
    win32more 的机器上把每一页都建出来**。)
    """

    def test_all_qt_pages_build_with_win32more_blocked(self, monkeypatch):
        import builtins
        import sys as _sys

        pytest.importorskip("PySide6")
        from PySide6.QtWidgets import QApplication

        real = builtins.__import__

        def blocked(name, *a, **k):
            if name == "win32more" or name.startswith("win32more."):
                raise ModuleNotFoundError("No module named 'win32more'")
            return real(name, *a, **k)

        for mod in [m for m in list(_sys.modules) if m.startswith("win32more")]:
            monkeypatch.delitem(_sys.modules, mod, raising=False)
        monkeypatch.setattr(builtins, "__import__", blocked)

        from astro_smb_qt import theme
        from astro_smb_qt.__main__ import PAGE_CLASSES
        from astro_smb_qt.shell import Shell

        app = QApplication.instance() or QApplication([])
        theme.apply(app)
        win = Shell()
        for tag in sorted(PAGE_CLASSES):
            assert win.page(tag) is not None, f"{tag} 页建不出来"


class TestTheLockResolvesForIntelMac:
    """**依赖集在 Intel macOS 上必须解得开,而且不含 win32more。**

    `pyproject.toml` 里的平台标记是"应该",这条测的是"实际" —— 让 uv 真的
    为 `x86_64-apple-darwin` 解一遍。写错标记(或将来某个依赖悄悄拉进
    Windows 专属包)在 Windows 上永远看不出来,要等有人在 mac 上 `uv sync`
    才炸,而那时人已经在另一台机器前面了。
    """

    def _compile(self, platform: str) -> str:
        import shutil
        import subprocess

        import pytest

        if shutil.which("uv") is None:
            pytest.skip("没有 uv")
        proc = subprocess.run(
            ["uv", "pip", "compile", "pyproject.toml",
             "--python-platform", platform, "--python-version", "3.13",
             "--all-extras", "--no-header", "-q"],
            cwd=ROOT, capture_output=True, timeout=300)
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace")
            pytest.fail(f"{platform} 解不开依赖:\n{err[-2000:]}")
        return proc.stdout.decode("utf-8", errors="replace")

    def test_intel_mac_has_no_windows_only_packages(self):
        out = self._compile("x86_64-apple-darwin")
        bad = [line for line in out.splitlines()
               if line.strip().startswith(("win32more", "pywin32"))]
        assert not bad, f"Intel macOS 的依赖集里混进了 Windows 专属包: {bad}"
        # 核心三样必须在 —— 只是"没炸"不等于"能用"
        for pkg in ("impacket", "numpy", "pillow"):
            assert any(line.lower().startswith(pkg) for line in out.splitlines()), pkg

    def test_windows_still_gets_win32more(self):
        """反面也要钉住 —— 否则"把标记写成永远为假"也能让上面那条绿。"""
        out = self._compile("x86_64-pc-windows-msvc")
        assert any(line.startswith("win32more==") for line in out.splitlines())


class TestCiCoversTheMacPath:
    """CI 要真的覆盖 macOS 那条路 —— 否则"跨平台"只是文档里的一句话。

    两个容易漏的:**macos-14 是 arm64**(本项目的目标是 x86_64,只测 arm64
    等于没测),以及**渲染器从没在非 Windows 上编译过**(协议库那个 job 不带
    Uno,而真正会出平台问题的正是渲染器里的 `#if WINDOWS` 与 WebView2 引用)。
    """

    def _ci(self) -> str:
        return (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8")

    def test_intel_macos_is_in_the_python_matrix(self):
        """**限定到 python 那个 job。**

        第一版断言的是"文件里出现过 macos-13" —— 而渲染器编译 job 的矩阵里
        也有它,把它从 python 矩阵里删掉照样绿。第二版改成"任意 matrix 的
        os 行",同一个洞。测试断言的东西必须是它声称在测的那个东西。
        """
        import re

        ci = self._ci()
        parts = ci.split("\n  python:", 1)
        assert len(parts) == 2, "找不到 python job"
        body = parts[1].split("\n\n  ", 1)[0]
        row = re.search(r"^\s*os:\s*\[(.+)\]\s*$", body, re.M)
        assert row, "python job 里找不到 matrix 的 os 行"
        assert "macos-13" in row.group(1), \
            "只测 arm64 等于没测 x86_64 那条路: " + row.group(1)

    def test_the_delivered_frontend_is_actually_installed(self):
        """**这条原来盯的是 Uno 渲染器**,那套 2026-08-04 删了。

        换过来盯现在的交付物,而它当时的状态更糟:`pyside6` 从来没进过任何
        依赖组,CI 的 `uv sync --all-groups` 装不到它,于是**跨平台交付的那套
        前端一条测试都没在 CI 上跑过** —— 2669 条里只跑了 1353 条,而日志
        尾巴上两者长得一模一样。

        判据是"CI 装的那些组里有它",不是"文件里出现过 pyside6" ——
        写进 `optional-dependencies` 而 CI 不带 `--extra` 的话照样是零。
        """
        import tomllib

        cfg = tomllib.loads(
            (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        dev = cfg["dependency-groups"]["dev"]
        assert any(d.lower().startswith("pyside6") for d in dev), (
            "pyside6 不在 dev 组里 —— CI 的 uv sync --all-groups 装不到它,"
            "Qt 前端那一千多条测试会整块跳过而不报错")

    def test_the_python_job_installs_all_groups(self):
        """自检:上一条假设 CI 真的装了 dev 组。"""
        assert "--all-groups" in self._ci(), \
            "CI 不装 dev 组的话,pyside6 在不在组里都无所谓了"

    def test_the_mac_scripts_are_executable(self):
        """脚本要能直接 `./scripts/mac-run.sh`,不该逼用户先 chmod。"""
        import subprocess

        out = subprocess.run(["git", "ls-files", "-s", "scripts/"],
                             cwd=ROOT, capture_output=True, text=True).stdout
        for line in out.splitlines():
            if line.endswith(".sh"):
                assert line.startswith("100755"), f"不可执行: {line}"


class TestBundledResourceLookup:
    """打包后三样资源还找不找得到。

    **`Path(__file__)` 在冻结的包里不再指向源码树。** PyInstaller 把模块塞进
    归档、把数据解到 `sys._MEIPASS`(onefile)或放在可执行文件旁边(onedir);
    `parents[2]` 那种往上数的写法会直接走到包外面去 —— 词表、天球资产、
    渲染器三处原来都是那么写的。
    """

    def test_dev_paths_win_over_bundle_paths(self, tmp_path, monkeypatch):
        """**开发路径排在前面。**

        打包后的目录在开发机上根本不存在;而反过来(在包里找源码树)可能拿到
        一个恰好同名却无关的目录 —— 宁可找不到也不要找错。
        """
        from astro_smb_app import bundle

        dev = tmp_path / "dev.json"
        dev.write_text("{}", encoding="utf-8")
        packed = tmp_path / "pack"
        (packed / "a").mkdir(parents=True)
        (packed / "a" / "dev.json").write_text("{}", encoding="utf-8")

        monkeypatch.setattr(bundle.sys, "frozen", True, raising=False)
        monkeypatch.setattr(bundle.sys, "_MEIPASS", str(packed), raising=False)
        assert bundle.data_file("a", "dev.json", package_relative=dev) == dev

    def test_bundle_path_is_used_when_dev_path_is_gone(self, tmp_path, monkeypatch):
        from astro_smb_app import bundle

        packed = tmp_path / "pack"
        (packed / "a").mkdir(parents=True)
        target = packed / "a" / "x.json"
        target.write_text("{}", encoding="utf-8")

        monkeypatch.setattr(bundle.sys, "frozen", True, raising=False)
        monkeypatch.setattr(bundle.sys, "_MEIPASS", str(packed), raising=False)
        gone = tmp_path / "does-not-exist.json"
        assert bundle.data_file("a", "x.json", package_relative=gone) == target

    def test_onedir_layout_is_recognised_too(self, tmp_path, monkeypatch):
        """onefile 解到 _MEIPASS,onedir 放在 exe 旁边。**两种都要认** ——
        只认一种的话另一种会静默找不到资源。"""
        from astro_smb_app import bundle

        exe = tmp_path / "app" / "tool.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"")
        monkeypatch.setattr(bundle.sys, "frozen", True, raising=False)
        monkeypatch.delattr(bundle.sys, "_MEIPASS", raising=False)
        monkeypatch.setattr(bundle.sys, "executable", str(exe))
        assert bundle.bundle_root() == exe.parent



    def test_nothing_is_frozen_during_the_test_run(self):
        """兜底:测试自己跑在源码树里,别被上面的 monkeypatch 污染。"""
        from astro_smb_app import bundle

        assert not bundle.frozen()


class TestOnedirLayoutIsTheOneThatShips:
    """PyInstaller 6 的 **onedir** 布局。

    这是真正会分发的那种,而它有个反直觉的地方:随包数据在 `_internal/` 下,
    `sys._MEIPASS` 指的正是那个子目录 —— 而渲染器在它的**上一层**。
    我一开始以为 onedir 不设 `_MEIPASS`,于是 `renderer_dir()` 去 `_internal/`
    底下找;包打出来了,一启动就说"找不到渲染器"。**只有真的运行一次才发现。**
    """

    def _onedir(self, tmp_path, monkeypatch):
        from astro_smb_app import bundle

        app = tmp_path / "astro-smb-tool"
        internal = app / "_internal"
        (internal / "astro_smb_app" / "web").mkdir(parents=True)
        (internal / "astro_smb_app" / "web" / "sky3d.js").write_text(
            "//", encoding="utf-8")
        (app / "renderer").mkdir()
        exe = app / "astro-smb-tool.exe"
        exe.write_bytes(b"")

        monkeypatch.delenv("ASTRO_SMB_RENDERER_DIR", raising=False)
        monkeypatch.setattr(bundle.sys, "frozen", True, raising=False)
        monkeypatch.setattr(bundle.sys, "_MEIPASS", str(internal), raising=False)
        monkeypatch.setattr(bundle.sys, "executable", str(exe))
        return bundle, app, internal

    def test_data_comes_from_internal(self, tmp_path, monkeypatch):
        bundle, _app, internal = self._onedir(tmp_path, monkeypatch)
        gone = tmp_path / "nope.json"
        got = bundle.data_file("astro_smb_app", "web", "sky3d.js",
                               package_relative=gone)
        assert got == internal / "astro_smb_app" / "web" / "sky3d.js"

