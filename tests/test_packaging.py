"""打包面:**入口点指向的包必须真的在 wheel 里。**

这条是补给一次真事故的:`astro-smb-tool-qt` 这个入口点一直有,而
`astro_smb_qt` **不在** `[tool.hatch.build.targets.wheel].packages` 里 ——
`pip install` 之后跑那条命令直接 ImportError。

而它在本地**完全看不出来**:wheel 能构建、单测全绿、`uv run` 也正常 ——
因为本地跑的是**源码树**,根本不经过 wheel。只有真正装一次才会暴露。
"""
from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"


def _cfg() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


class TestEntryPointsResolve:

    def test_every_script_target_is_a_packaged_module(self):
        cfg = _cfg()
        packaged = set(cfg["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"])
        missing = []
        for name, target in (cfg["project"].get("scripts") or {}).items():
            top = target.split(":")[0].split(".")[0]
            if top not in packaged:
                missing.append(f"{name} → {target}(顶层包 {top} 不在 wheel 里)")
        assert not missing, (
            "这些入口点指向的包不会被打进 wheel —— `pip install` 之后一跑就 "
            "ImportError,而本地(源码树)完全正常:\n  " + "\n  ".join(missing))

    def test_every_script_target_actually_exists(self):
        """顺带查一下目标模块/函数是不是真的在。"""
        cfg = _cfg()
        bad = []
        for name, target in (cfg["project"].get("scripts") or {}).items():
            mod, _, fn = target.partition(":")
            path = ROOT.joinpath(*mod.split("."))
            src = path.with_suffix(".py")
            if not src.is_file():
                src = path / "__init__.py"
            if not src.is_file():
                bad.append(f"{name}: 找不到模块 {mod}")
                continue
            if fn and not re.search(rf"^(async )?def {re.escape(fn)}\b",
                                    src.read_text(encoding="utf-8"), re.M):
                bad.append(f"{name}: {mod} 里没有 {fn}()")
        assert not bad, bad


class TestDataFilesShip:
    """**非 .py 的东西也得进去。** 少一样就是一整块功能在装出来的包里失灵。"""

    def test_the_pieces_that_are_not_python(self):
        packaged = set(_cfg()["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"])
        want = {
            # 老 UI 的界面本体 —— 少了它 XamlReader.Load 直接失败
            "astro_smb_gui": ["*.xaml"],
            # 3D 天球页的静态资产(两套前端共用)
            "astro_smb_app": ["web/*.js", "web/*.css", "web/*.html"],
            # 翻译:少了 .mo 就永远是中文,而且不报错
            "astro_smb": ["locale/*/LC_MESSAGES/*.mo"],
        }
        missing = []
        for pkg, pats in want.items():
            assert pkg in packaged, pkg
            for pat in pats:
                if not list((ROOT / pkg).glob(pat)):
                    missing.append(f"{pkg}/{pat}")
        assert not missing, (
            f"这些随包数据在源码树里就找不到: {missing} —— "
            "要么是路径变了,要么是构建步骤没跑(`.mo` 要 scripts/i18n_build.py 生成)")


class TestEveryGuiEntryPointCanGetItsToolkit:
    """**入口点在 wheel 里,不等于它跑得起来。**

    `astro-smb-tool-qt` 一直在 `[project.scripts]` 里,而 `pyside6` 只在
    `[dependency-groups].dev` —— 那个组**不进发行元数据**。于是
    `pip install astro-smb-tool` 之后跑它:

        ModuleNotFoundError: No module named 'PySide6'

    本地一次都看不出来:`uv sync` 装了 dev 组,`uv run` 跑的是源码树。
    是把 wheel 装进一个干净 venv 真的敲了一遍那三条命令才发现的。

    工具包**故意不进必装依赖**(只用 CLI 的人不该被拖去下一百多兆),
    所以这里要的不是"必装",而是**有一条明说的路**:声明成 extra。
    """

    #: 入口点 → (它需要的第三方包, 该由哪个 extra 提供)
    TOOLKITS = {
        "astro-smb-tool-qt": ("pyside6", "qt"),
        "astro-smb-tool-gui": ("win32more", "winui"),
    }

    def test_each_one_is_declared_in_an_extra(self):
        cfg = _cfg()
        extras = cfg["project"].get("optional-dependencies") or {}
        scripts = cfg["project"].get("scripts") or {}
        bad = []
        for entry, (dist, extra) in self.TOOLKITS.items():
            if entry not in scripts:
                continue                      # 入口点没了就不用管它的 extra
            got = extras.get(extra) or []
            if not any(d.lower().startswith(dist) for d in got):
                bad.append(f"{entry} 要 {dist},而 [{extra}] 里没有: {got}")
        assert not bad, "\n  ".join(["图形入口点拿不到它的工具包:", *bad])

    def test_the_toolkits_stay_out_of_the_required_deps(self):
        """反面:别为了"省事"把 Qt 塞进必装。

        CLI 是这个包的一等入口,`pip install astro-smb-tool` 之后应该
        几秒装完。真塞进去了,这条会红,而不是等用户抱怨下载慢。
        """
        deps = _cfg()["project"]["dependencies"]
        heavy = [d for d in deps
                 if d.lower().startswith(("pyside6", "pyqt", "win32more"))]
        assert not heavy, f"图形工具包不该在必装依赖里: {heavy}"

    def test_the_message_actually_tells_you_what_to_type(self, monkeypatch):
        """**声明了 extra ≠ 用户知道要装它。**

        没有这一层,`pip install astro-smb-tool` 之后跑图形入口拿到的是
        `ModuleNotFoundError: No module named 'PySide6'` —— 他装的明明是
        这个包,那句话对他毫无意义。所以两条入口点都要自己说清楚。

        **行为验证**:把 `find_spec` 打成"找不到",看它说了什么。
        只查源码里有没有那串字面量的话,改个函数名就静默失效了。
        """
        import importlib.util

        from astro_smb_gui import app as winui_app
        from astro_smb_qt import __main__ as qt_main

        monkeypatch.setattr(importlib.util, "find_spec", lambda name: None)
        for guard, extra in ((qt_main._require_toolkit, "[qt]"),
                             (winui_app._require_toolkit, "[winui]")):
            with pytest.raises(SystemExit) as e:
                guard()
            msg = str(e.value)
            assert extra in msg, f"没告诉用户装哪个 extra: {msg[:120]}"
            assert "pip install" in msg, f"没给可以照抄的命令: {msg[:120]}"


class TestTheFrozenBundleSpec:
    """**PyInstaller 规格里写死的路径也会漂。**

    规格是构建时才读的:里面某个目录改了名,一直到打包那一刻才炸,而打包
    只在打 tag 时跑。和 `.github/` 一样,是这个仓库里没人替它变红的地方。
    """

    SPEC = ROOT / "packaging" / "astro-smb-tool.spec"

    def test_the_spec_exists(self):
        assert self.SPEC.is_file(), (
            "release.yml 要打这份规格,它不在 —— 整条发布链是断的")

    def test_the_data_directories_it_bundles_are_real(self):
        """规格里点名要打进去的那几样东西,源码树里得真有。

        少一样的表现不是构建失败,是**打出来的包缺一块功能而照样启动** ——
        翻译没了界面永远中文,天球资产没了那一页空白。
        """
        src = self.SPEC.read_text(encoding="utf-8")
        want = {
            "astro_smb_app/web": ROOT / "astro_smb_app" / "web",
            "astro_smb/locale": ROOT / "astro_smb" / "locale",
        }
        for name, path in want.items():
            assert name in src, f"规格里没提 {name} —— 它不会被打进包"
            assert path.is_dir(), f"规格要打 {name},而它不在源码树里"

    def test_it_targets_the_qt_frontend(self):
        """打的是跨平台那一套。**WinUI3 打不了** —— 它要 Windows App
        Runtime 单独安装,不是解开就能跑的东西。"""
        src = self.SPEC.read_text(encoding="utf-8")
        assert "astro_smb_qt" in src, "规格没指向 Qt 入口"
        assert '"win32more"' in src, (
            "win32more 没被 exclude —— 它会把另一套前端的绑定拖进包里")

    def test_mac_targets_the_running_architecture(self):
        """mac 的目标架构要跟着运行架构走,不能写死。

        **注意这条不保证包会变小。** 当初加 `target_arch` 是以为能把
        universal2 的胖二进制 `lipo -thin` 掉一半 —— 实测前后都是 1023 MB,
        那条推断是错的(PyInstaller 本来就按当前架构走)。留着是为了
        "换台 arm64 mac 打包时不会打错架构"。
        """
        src = self.SPEC.read_text(encoding="utf-8")
        assert "target_arch=_TARGET_ARCH" in src, "没给 target_arch,胖二进制原样打进去"
        assert "platform.machine()" in src, (
            "target_arch 写死了某一个架构 —— 换台 mac 就打错了")


class TestTheUiFontIsNotHardcodedToOnePlatform:
    """`Segoe UI` 是 Windows 的界面字体。写死它的后果在别的平台上有两层,
    **都不报错**:

    * 每次启动多花约 200 毫秒 —— Qt 找不到就遍历整个字体库建别名表。
      x86 Mac 上实测 ``Populating font family aliases took 198 ms``,
      Qt 自己在日志里就写着"换一个存在的字体来避免这个开销";
    * 最终用哪个字体我们说了不算,而整套排版尺寸是照着一个具体字体调的。
    """

    def test_the_family_list_is_computed_not_frozen(self):
        import ast

        src = (ROOT / "astro_smb_qt" / "theme.py").read_text(encoding="utf-8")
        assert "def ui_family" in src
        # 常量里不许再有它 —— 函数里作为**平台名单的一项**是可以的
        for node in ast.walk(ast.parse(src)):
            if (isinstance(node, ast.Assign) and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "FAMILY"):
                raise AssertionError("FAMILY 又变回写死的常量了")

    def test_each_platform_gets_a_font_that_exists_there(self, monkeypatch):
        pytest.importorskip("PySide6")
        import sys as _sys

        from astro_smb_qt import theme

        want = {"darwin": ("SF Pro Text", "Helvetica Neue"),
                "win32": ("Segoe UI",),
                "linux": ("Cantarell", "Ubuntu", "DejaVu Sans")}
        for plat, expect in want.items():
            monkeypatch.setattr(_sys, "platform", plat)
            got = theme.ui_family()
            assert any(e in got for e in expect), f"{plat}: {got}"

    def test_cjk_fallbacks_are_always_there(self, monkeypatch):
        """界面字体是拉丁字体时中文字形要有人接 —— 否则满屏方框。"""
        pytest.importorskip("PySide6")
        import sys as _sys

        from astro_smb_qt import theme

        for plat in ("darwin", "win32", "linux"):
            monkeypatch.setattr(_sys, "platform", plat)
            got = theme.ui_family()
            assert "PingFang SC" in got and "Noto Sans CJK SC" in got, got

    def test_families_with_spaces_are_quoted(self, monkeypatch):
        """`PingFang SC` 不加引号会被 QSS 当成两个族名。"""
        pytest.importorskip("PySide6")
        import sys as _sys

        from astro_smb_qt import theme

        monkeypatch.setattr(_sys, "platform", "darwin")
        assert '"PingFang SC"' in theme.ui_family()


class TestTheBundleIsNotFullOfThingsNobodyUses:
    """打包体积:量出来再删,而且删完要真开一次。"""

    SPEC = ROOT / "packaging" / "astro-smb-tool.spec"

    def test_it_trims_the_chromium_devtools_resources(self):
        """**72 MB 的 Chromium 开发者工具资源不该进包。**

        实测(win-x64)裁剪前 505 MB,裁掉 DevTools 资源与用不到的语言包
        之后 372 MB —— 四分之一。没人会在天球页上开 Chrome DevTools,
        而我们只发中文和英文两种界面语言。

        真正的护栏是 `package.py --smoke` **开在天球页**:裁过头的表现是
        那一页空白、控制台一个字不说,只有真开一次才看得出来。
        这里钉的是"裁剪逻辑还在,而且没把该留的一起裁了"。
        """
        src = self.SPEC.read_text(encoding="utf-8")
        assert "qtwebengine_devtools_resources" in src, "DevTools 资源又打进去了"
        assert "KEEP_LOCALES" in src, "语言包裁剪没了"
        for keep in ("zh-CN", "en-US"):
            assert keep in src, f"{keep} 不在保留名单里 —— WebEngine 会缺语言"

    def test_the_smoke_test_opens_the_page_that_uses_webengine(self):
        """裁剪只影响 QtWebEngine,而九页里只有天球用它。
        冒烟开在别的页 = 这条裁剪一次都没被验过。"""
        src = (ROOT / "scripts" / "package.py").read_text(encoding="utf-8")
        assert '"--page", "sky"' in src, (
            "冒烟没开天球页 —— Chromium 资源裁过头的话没人会发现")

    def test_the_size_report_does_not_follow_symlinks(self):
        """**报体积不能用 `stat()`** —— 它跟随符号链接,而 macOS 的
        `.framework` 全靠符号链接搭起来,每个 Qt 库会被数两遍。

        实测虚报到 1023 MB,`du -sh` 说 439 MB。我拿着那个虚高的数字追了
        三轮「包怎么这么大」,还据此推断出一条错误的 universal2 结论。
        """
        src = (ROOT / "scripts" / "package.py").read_text(encoding="utf-8")
        assert "def bundle_size" in src
        assert "is_symlink()" in src, "又会把符号链接算进去了"
        assert "lstat()" in src, "用了会跟随链接的 stat()"
