"""包边界与发行配置的护栏。

原本这里有 9 条,其中 4 条是围绕**那套已删除的 sidecar 前端**(astro_smb_app /
astro_smb_winui / astro_smb_tauri / contracts)写的。B1 阶段那套已整体删除,
那 4 条随之失去对象 —— 注意其中两条用 `Path.rglob` 走一个**不存在**的目录,
rglob 对缺失目录返回空而不报错,所以它们会**静默地空转通过**,是最坏的一种
"测试还在、其实什么都没测"。故整条删除,并补一条守卫防止那套东西被误合并回来。
"""
from __future__ import annotations

import ast
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def imported_modules(package: Path) -> set[str]:
    modules: set[str] = set()
    for path in package.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
    return modules


def test_core_library_never_imports_the_gui():
    """核心库不许反向依赖任何 UI 包。

    这是分层的地基:`astro_smb` 要能在无 GUI、无 win32more 的环境里被 CLI、
    测试与将来的新前端直接使用。这条在 B1 之前没人守,靠的是自律。
    """
    modules = imported_modules(ROOT / "astro_smb")
    bad = [m for m in modules
           if m == "astro_smb_gui" or m.startswith("astro_smb_gui.")
           or m.startswith("astro_smb_app")]
    assert not bad, f"astro_smb 反向依赖了 UI 层: {bad}"


def test_the_deleted_sidecar_frontend_stays_deleted():
    """B1 删掉的那套双前端不许回来(误合并/误 revert 会拖回 2.9 GB)。

    **契约已变(B2)**:`astro_smb_app` 这个**名字**被 B2 重新启用了 —— 它现在
    装的是从 `astro_smb_gui/` 移出来的 9 个纯模块(devices/dircache/logstore/
    metacache/preview/skymap/transfers/volumes/watcher),与那套毫无关系。
    所以这里改为盯**内容**:sidecar 那套特有的服务/契约/桥接层不许出现在里面。
    盯名字会把一次合理的复用误报成回归。

    **`packaging/` 同理(B19)**:那个名字现在装的是新前端的 PyInstaller 规格
    与入口,和那套(打的是已废弃的 winui 副本 + Tauri sidecar)没关系。
    所以这里也改成盯里面**具体是什么** —— 出现 `winui.spec` 或 sidecar
    才是真的回归。
    """
    gone = ("astro_smb_winui", "astro_smb_tauri", "contracts")
    back = [name for name in gone if (ROOT / name).exists()]
    assert not back, f"这些目录本该在 B1 删除: {back}"

    pack = ROOT / "packaging"
    if pack.is_dir():
        sidecar_pack = [f.name for f in pack.iterdir()
                        if f.name in ("winui.spec", "sidecar.spec")
                        or "sidecar" in f.name.lower()
                        or "winui" in f.name.lower()]
        assert not sidecar_pack, (
            f"packaging/ 里出现了那套已删除前端的打包产物: {sidecar_pack}")

    contract_only = ("services.py", "session.py", "models.py", "schema.py",
                     "sidecar.py", "domain.py", "design_tokens.py", "bridges")
    resurrected = [n for n in contract_only
                   if (ROOT / "astro_smb_app" / n).exists()]
    assert not resurrected, (
        f"astro_smb_app 里出现了那套契约层的文件: {resurrected}")

    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    deps = config["project"]["dependencies"]
    assert not [d for d in deps if d.startswith("pydantic")], (
        "pydantic 是那套契约层的运行时依赖,随它一起删了")


def test_shared_layer_never_imports_a_frontend():
    """`astro_smb_app` 不许反向依赖任何前端包 —— 包括老 UI。

    搬家时它有 5 个模块还写着 `from astro_smb_gui import metacache`,
    没改的话会形成 gui → app → gui 的环,而且 `astro_smb_gui` 那侧只是 shim,
    绕一圈还回到自己。
    """
    modules = imported_modules(ROOT / "astro_smb_app")
    bad = [m for m in modules
           if m == "astro_smb_gui" or m.startswith("astro_smb_gui.")]
    assert not bad, f"共享层反向依赖了前端: {bad}"


def test_moved_modules_are_the_same_object_through_the_shim():
    """老 UI 的 `astro_smb_gui.X` 必须与 `astro_smb_app.X` 是**同一个模块对象**。

    不是洁癖:`metacache` 持有全局 sqlite 连接与一把 `threading.Lock`,
    `preview`/`transfers` 各自有线程与缓存。两份模块状态会互相踩,
    而且症状是随机的缓存不命中与锁失效 —— 最难查的那类。

    也因此 shim 用 `sys.modules` 别名而**不是** `from ... import *`:
    后者既造第二个对象,又取不到下划线私有名(本包内实测有 18 处从这些模块
    import 私有名)。
    """
    import importlib

    for name in ("devices", "dircache", "logstore", "metacache", "preview",
                 "skymap", "transfers", "volumes", "watcher"):
        old = importlib.import_module(f"astro_smb_gui.{name}")
        new = importlib.import_module(f"astro_smb_app.{name}")
        assert old is new, name


def test_distribution_includes_every_shipped_package():
    """**这条断言几经反转,值得记一笔。**

    最早它断言 `astro_smb_gui` 要被**排除**出发行版(前提是新 UI 会取代它);
    用户审查判定新 UI 不合格后改为必须**包含**;后来删掉两个包收窄到两项;
    再把 9 个纯模块移进**重建的** `astro_smb_app`,于是回到三项。

    **2026-08-05:加上 `astro_smb_qt`。而这条测试本身正是那个 bug 能活下来
    的原因** —— 跨平台前端做完好几轮、入口点 `astro-smb-tool-qt` 一直在,
    而它不在打包列表里;这条断言**自信地把错误的列表钉死了**,于是每次
    全量测试都在替那个 bug 背书。本地看不出来:wheel 能构建、单测全绿、
    `uv run` 也正常(跑的是源码树),只有真装一次才炸。

    写死清单的测试有这个固有风险,所以另加了 `tests/test_packaging.py` ——
    那一条**从入口点推**出该有哪些包,不需要人记得更新。
    """
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    packages = config["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    assert sorted(packages) == [
        "astro_smb", "astro_smb_app", "astro_smb_gui", "astro_smb_qt"], packages


def test_default_gui_entry_point_is_the_working_one():
    """`astro-smb-tool-gui` 必须指向功能完整的那套。曾被改指到 astro_smb_winui,
    于是用户敲惯用命令启动的根本不是他在用的界面。"""
    config = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = config["project"]["scripts"]
    assert scripts["astro-smb-tool-gui"] == "astro_smb_gui.app:main"
    assert "astro-smb-tool-winui" not in scripts
    assert "astro-smb-tool-sidecar" not in scripts



def _module_level_imports(path: Path) -> set[str]:
    """这个文件 **import 时**会执行到的 import,拍平成点分名字。

    模块体里的算(包括写在 `try:` / `if:` 里的 —— 那些照样会执行);
    函数体和类体里的**不算** —— 那是调用时才发生的事,而这里关心的是
    "collection 期会不会炸"。

    返回的既有模块名也有 `from a.b import c` 里的 `a.b.c`,这样调用方
    既能问"有没有 import win32more",也能问"有没有 import 某个具体页面"。
    """
    out: set[str] = set()

    def visit(nodes) -> None:
        for n in nodes:
            if isinstance(n, ast.Import):
                out.update(a.name for a in n.names)
            elif isinstance(n, ast.ImportFrom):
                mod = n.module or ""
                out.add(mod)
                out.update(f"{mod}.{a.name}" for a in n.names)
            elif isinstance(n, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                for attr in ("body", "orelse", "finalbody"):
                    visit(getattr(n, attr, []) or [])
                for h in getattr(n, "handlers", []) or []:
                    visit(h.body)
            # FunctionDef / AsyncFunctionDef / ClassDef:**不进去**

    visit(ast.parse(path.read_text(encoding="utf-8")).body)
    return out


def test_gui_test_modules_are_registered_in_conftest():
    """需要 win32more 的测试模块必须登记进 `conftest._NEEDS_WIN32MORE`。

    否则在没装 win32more 的环境(Linux/macOS CI、干净 checkout)里,
    该模块会在 **collection 期**炸成 error 并**中断整轮 pytest** ——
    本机实测过一次:7 个 error 让 1142 个用例一个都没跑成,
    真实回归被完全淹没。
    """
    from conftest import _NEEDS_WIN32MORE

    # 判据不能是"import 了 astro_smb_gui" —— 该包里那 9 个同名模块现在只是
    # 指向 astro_smb_app 的 shim(B2 搬家),零 win32more 引用,干净环境照样能
    # import。真正需要它的是那些 UI 模块。这里先算出后者,再据此判断测试模块。
    #
    # **也不能是"文件里出现过这行"。** `app.py` 的 `from win32more import appsdk`
    # 写在 `main()` 里面,import 那个模块一行 win32more 都不碰;而原来的正则是
    # `^\s*`,`re.M` 下缩进的行照样命中。代价不是多一条假警报 —— 是那个测试
    # 文件被登记进 `_NEEDS_WIN32MORE`,于是**在 Linux/macOS 上整份跳过**,
    # 而它查的可能和平台毫无关系(实测:test_packaging.py 查的是 wheel 打包)。
    gui_dir = ROOT / "astro_smb_gui"
    ui_modules = {p.stem for p in gui_dir.glob("*.py")
                  if "win32more" in _module_level_imports(p)}
    assert ui_modules, "没找到任何需要 win32more 的 GUI 模块 —— 判据失效了"

    missing = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        top = _module_level_imports(path)
        needs = ("win32more" in top
                 or any(m in top for m in
                        (f"astro_smb_gui.{n}" for n in ui_modules)))
        if needs and path.name not in _NEEDS_WIN32MORE:
            missing.append(path.name)
    assert not missing, (
        f"这些测试模块在**模块顶层** import 了需要 win32more 的东西,"
        f"但没登记进 conftest._NEEDS_WIN32MORE: {missing}")


def test_win32more_is_in_dev_group_with_platform_marker():
    """win32more 必须在 dev 组里(否则 `uv sync` 不装,GUI 测试全 error),
    且必须带平台标记(它是 Windows 专属,Linux/macOS CI 装不上)。"""
    config = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev = config["dependency-groups"]["dev"]
    hit = [d for d in dev if d.startswith("win32more")]
    assert hit, "win32more 不在 dev 组里"
    assert "sys_platform" in hit[0], f"缺平台标记: {hit[0]}"


def test_no_undefined_names_anywhere_extraction_touched():
    """抽取过的模块里不许有"import 能过、调用才炸"的漏网名字。

    **两个方向都栽过一次。**

    *新包这侧*:B8 从 `_records.py` 抽出 1088 行时漏了三个 import
    (`guide_summary_for_run` / `parse_exposure_seconds` / `section_begins`)。
    模块 import 完全正常,是调用到那几个函数时才 NameError —— 而调用点外面
    正好包着 `except Exception: summary = None`,于是**症状是首屏静默不出现**,
    没有任何报错。查了好一会儿才定位。

    *老 UI 这侧*:B6 抽 `_guiding.py` 时漏了 `WINDOW_CHOICES`,于是导星页
    **时间窗下拉一动就 NameError**。这条一直躺到 B11 把门禁扩过来才被捞出来 ——
    只扫新包等于只堵了一半,而抽取本来就是两边同时改。

    静态扫一遍名字比事后调试便宜太多,所以做成门禁。
    """
    import ast
    import builtins

    bad: list[str] = []
    targets = sorted((ROOT / "astro_smb_app" / "views").glob("*.py"))
    targets += sorted((ROOT / "astro_smb_gui").glob("*.py"))
    for path in targets:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        # 模块级 dunder 不在 builtins 里,但每个模块都有
        defined = set(dir(builtins)) | {
            "__file__", "__name__", "__doc__", "__package__",
            "__spec__", "__loader__", "__path__"}
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(n.name)
            elif isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                defined.add(n.id)
            elif isinstance(n, (ast.Import, ast.ImportFrom)):
                for a in n.names:
                    defined.add((a.asname or a.name).split(".")[0])
            elif isinstance(n, ast.arg):
                defined.add(n.arg)
            elif isinstance(n, ast.ExceptHandler) and n.name:
                defined.add(n.name)
            elif isinstance(n, (ast.Global, ast.Nonlocal)):
                defined.update(n.names)
        used = {n.id for n in ast.walk(tree)
                if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
        bad += [f"{path.name}:{name}" for name in sorted(used - defined)]
    assert not bad, f"这些名字没定义也没 import(调用时才会炸): {bad}"


def test_no_monkeypatch_on_a_reexported_name():
    """不许把 monkeypatch 打在 **re-export 的名字**上 —— 那样测试会静默失效。

    `astro_smb_gui/_fitsview.py` 等页面模块用 `from astro_smb_app.views.X import …`
    把视图模型的名字导进来。给**导入名**打补丁不会改变源模块内部的调用,
    于是被测代码走的仍是原实现 —— **测试照样全绿,但什么都没测**。

    (B9 真踩了:两条渲染预算测试 monkeypatch `_fitsview._render_dir`,
    抽取之后它们仍然通过,其实完全没生效。)

    注意这与 B2 那九个 `sys.modules` 别名模块**不同** —— 那些两边是同一个
    模块对象,打哪边都一样。所以判据只针对 `views/` 下的名字。
    """
    import ast
    import re

    views_dir = ROOT / "astro_smb_app" / "views"
    view_names: set[str] = set()
    for path in views_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                view_names.add(node.name)
            elif isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name):
                        view_names.add(t.id)

    # 页面模块里从 views 再导出的那些名字
    reexported: set[str] = set()
    for path in (ROOT / "astro_smb_gui").glob("_*.py"):
        text = path.read_text(encoding="utf-8")
        for m in re.finditer(r"from astro_smb_app\.views\.\w+ import \(([^)]*)\)",
                             text):
            reexported |= {n.strip().rstrip(",")
                           for n in m.group(1).split("\n") if n.strip()}
        for m in re.finditer(r"from astro_smb_app\.views\.\w+ import "
                             r"([\w, ]+)$", text, re.M):
            reexported |= {n.strip() for n in m.group(1).split(",") if n.strip()}
    reexported &= view_names

    bad: list[str] = []
    pat = re.compile(r"monkeypatch\.setattr\(\s*(\w+)\s*,\s*[\"'](\w+)[\"']")
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        # 该文件把哪些别名指向了 astro_smb_gui 的页面模块
        gui_aliases = set(re.findall(
            r"from astro_smb_gui import (_\w+) as (\w+)", text))
        alias_map = {alias: mod for mod, alias in gui_aliases}
        alias_map.update({m: m for m in re.findall(
            r"import astro_smb_gui\.(_\w+)", text)})
        for mod_alias, attr in pat.findall(text):
            if mod_alias in alias_map and attr in reexported:
                bad.append(f"{path.name}: monkeypatch {mod_alias}.{attr}")
    assert not bad, (
        "这些 monkeypatch 打在 re-export 的名字上,不会生效(测试会静默通过):\n  "
        + "\n  ".join(bad)
        + "\n改为 patch astro_smb_app.views.<模块> 里的同名对象。")


def test_no_automation_still_builds_the_deleted_uno_frontend():
    """Uno / C# 渲染器 2026-08-04 整体删除。**自动化没跟着删。**

    `.github/workflows/ci.yml` 里两个 C# job 又活了三天,每次推送都失败;
    `release.yml` 与 `scripts/package.py` 更久 —— 它们 `dotnet publish` 一个
    不存在的 csproj、再 PyInstaller 一份不存在的 spec,而**只在打 tag 时
    触发**,所以要等到真的发一次版才知道整条发布链是断的。

    删代码时 import 会红、测试会红,只有这两个地方不会:CI 配置和它调的
    脚本都没人替它们变红。所以这条盯的是**残留的构建动作**,不是文件名。
    """
    # **`.sh` 也要扫。** 第一版只扫了 `.yml` 和 `.py`,于是
    # `scripts/mac-setup.sh` 与 `mac-run.sh` 又活了三天:前者要 .NET SDK 9、
    # 建一个不存在的 csproj,后者 exec 一个早就没有的入口点
    # (`astro-smb-tool-ui`)。而 CI 里那个 mac job 只做 `bash -n` 语法检查,
    # **语法当然是对的** —— 它一路绿着,直到有人真在 Mac 上照着跑。
    watch = [*(ROOT / ".github" / "workflows").glob("*.yml"),
             *(ROOT / "scripts").glob("*.py"),
             *(ROOT / "scripts").glob("*.sh")]
    assert watch, "没有可扫的自动化文件,这条门禁在空转"

    dead = ("dotnet ", "AstroSmbTool.Uno", "AstroSmbTool.Protocol",
            "wasm-tools", "net9.0-desktop")
    bad = []
    for p in watch:
        rel = p.relative_to(ROOT).as_posix()
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]
            for token in dead:
                if token in code:
                    bad.append(f"{rel}:{n}: {line.strip()[:80]}")
    assert not bad, (
        "自动化里还留着建 Uno 那套的动作,而那套已经删了:\n  " + "\n  ".join(bad))
