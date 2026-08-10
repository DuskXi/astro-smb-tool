"""用户设置(语言)与 Qt 的语言选择器。

**为什么要有设置文件**:这个仓库原来一样设置都不记。配色档每次回默认还能忍,
语言不行 —— 一个只会英文的用户每次开都得先摸到那个下拉才看得懂界面。

**为什么切语言要重启**:控件上的文案是**建控件那一刻**翻好烤进去的。运行时
热切要重建整棵控件树,而那一刻可能有好几个后台 worker 正拿着旧控件的引用
(传输、预览、日志解析)—— 它们的 `on_done` 会打到已析构的 C++ 对象上,
是**随机时机**的崩。切语言是低频动作,重启更诚实。
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from astro_smb import i18n
from astro_smb_app import settings

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "astro_smb_qt" / "shell.py"


@pytest.fixture(autouse=True)
def _fresh():
    settings.reset_cache()
    yield
    settings.reset_cache()


class TestTheStore:

    def test_round_trip(self):
        settings.set("k", "v")
        settings.reset_cache()
        assert settings.get("k") == "v"

    def test_missing_file_is_not_an_error(self):
        assert settings.get("没设过的键", "兜底") == "兜底"

    def test_a_corrupt_file_does_not_break_startup(self):
        """**设置读不出来不该让程序起不来。** 设置是锦上添花,不是运行前提。"""
        settings.settings_path().parent.mkdir(parents=True, exist_ok=True)
        settings.settings_path().write_text("{这不是 JSON", encoding="utf-8")
        settings.reset_cache()
        assert settings.get(settings.KEY_LANGUAGE, "") == ""

    def test_a_json_scalar_is_not_a_dict(self):
        """`json.loads("3")` 成功但不是 dict —— 直接 `.get` 会 AttributeError。"""
        settings.settings_path().parent.mkdir(parents=True, exist_ok=True)
        settings.settings_path().write_text("3", encoding="utf-8")
        settings.reset_cache()
        assert settings.get("x", "兜底") == "兜底"

    def test_write_is_atomic(self):
        """半截文件会被当成完整的 —— 这个仓库为它付过两次代价(预览缓存、分块下载)。"""
        src = ast.unparse(next(
            n for n in ast.walk(ast.parse(
                (ROOT / "astro_smb_app" / "settings.py").read_text(encoding="utf-8")))
            if isinstance(n, ast.FunctionDef) and n.name == "set"))
        assert "os.replace" in src, "没走 .tmp + os.replace"

    def test_unknown_keys_survive(self):
        """老版本读到不认识的键要原样保留,别把新版本写的设置抹掉。"""
        settings.set("未来的键", 1)
        settings.set(settings.KEY_LANGUAGE, "en")
        settings.reset_cache()
        data = json.loads(settings.settings_path().read_text(encoding="utf-8"))
        assert data["未来的键"] == 1 and data[settings.KEY_LANGUAGE] == "en"


class TestApplySavedLanguage:

    def test_saved_wins_over_the_system(self, monkeypatch):
        monkeypatch.delenv("ASTRO_SMB_LANG", raising=False)
        if "en" not in i18n.available_languages():
            pytest.skip("没有编译好的 en 目录")
        settings.set(settings.KEY_LANGUAGE, "en")
        try:
            assert settings.apply_saved_language() == "en"
        finally:
            i18n.set_language("zh_CN")

    def test_env_wins_over_the_saved_setting(self, monkeypatch):
        """**环境变量必须压过设置。**

        它是自动化/伪语言审计的显式覆盖口(截图脚本、`ASTRO_SMB_TEST_LANG`)。
        让用户设置盖过它的话,一台设过语言的机器上那些工具会全失效 ——
        而且不报错,只是审计跑出来"一条都没漂"。
        """
        settings.set(settings.KEY_LANGUAGE, "en")
        monkeypatch.setenv("ASTRO_SMB_LANG", "zh_CN")
        try:
            assert settings.apply_saved_language() == "zh_CN"
        finally:
            i18n.set_language("zh_CN")

    def test_entry_point_applies_it_before_building_the_ui(self):
        """顺序错了就没用:控件建完再切语言,已经建好的那些不会变。"""
        src = (ROOT / "astro_smb_qt" / "__main__.py").read_text(encoding="utf-8")
        assert "apply_saved_language()" in src, "入口没应用存下来的语言"
        assert src.index("apply_saved_language()") < src.index("Shell("), (
            "在建 Shell **之后**才切语言 —— 那时候文案已经烤进控件了")


class TestPseudoLocaleIsNotOfferedToUsers:

    def test_it_is_hidden_from_the_list(self):
        assert not [k for k in i18n.available_languages()
                    if k.split("_")[0] == i18n.PSEUDO_PREFIX], (
            "伪语言出现在给用户看的语言列表里了")

    def test_but_it_still_works_when_asked_for_explicitly(self):
        """审计要靠它 —— 藏起来不等于不能用。"""
        if not (i18n.LOCALE_DIR / "xx_PS" / "LC_MESSAGES"
                / f"{i18n.DOMAIN}.mo").is_file():
            pytest.skip("没生成伪语言(先跑 scripts/i18n_pseudo.py)")
        try:
            assert i18n.set_language("xx_PS") == "xx_PS"
        finally:
            i18n.set_language("zh_CN")


class TestThePicker:
    """选择器本身。**只在装了不止一种语言时才出现** —— 只有中文可选时
    摆一个一项的下拉是纯噪声,而那正是绝大多数用户看到的情况。"""

    def _fn(self, name: str) -> str:
        tree = ast.parse(SHELL.read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == name)
        return "\n".join(ast.unparse(b) for b in fn.body)

    def test_it_hides_itself_when_there_is_only_one_language(self):
        src = self._fn("_build_language_picker")
        assert "len(langs) < 2" in src and "return" in src

    def test_switching_persists_then_restarts(self):
        src = self._fn("_set_language")
        assert "settings.set(settings.KEY_LANGUAGE" in src, "没存下来"
        assert "self._restart()" in src, "存了但没重启 —— 界面不会变"
        assert src.index("settings.set") < src.index("self._restart()"), (
            "先重启后存 —— 新进程读到的还是旧语言")

    def test_declining_puts_the_combo_back(self):
        """用户改主意时下拉必须拨回去,否则它显示的语言和实际的对不上。"""
        src = self._fn("_set_language")
        assert "blockSignals(True)" in src and "setCurrentIndex" in src

    def test_it_warns_about_transfers_in_flight(self):
        src = self._fn("_set_language")
        assert "active_count()" in src, "重启会中断传输,得先说一声"

    def test_restart_is_detached(self):
        """非 detached 的子进程会跟着父进程一起死 —— 点完"重启"直接没了。"""
        src = self._fn("_restart")
        assert "startDetached" in src
        assert "sys.argv" in src, "没原样重放参数 —— 重启后会回到首页"


class TestTheSwitchDialogIsBilingual:
    """**切语言那个框必须两种语言都写。**

    想切语言的人多半是**看不懂当前语言**的那个人 —— 用当前语言拦住他,
    他连哪个按钮是"确定"都不知道。这一处最需要双语,也最容易被忽略:
    开发的人看得懂中文,自己点一遍一切正常。

    标题、正文、两个按钮都要。
    """

    def _src(self) -> str:
        import ast

        tree = ast.parse(SHELL.read_text(encoding="utf-8"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "_set_language")
        return "\n".join(ast.unparse(b) for b in fn.body)

    def test_it_translates_into_the_target_language(self):
        src = self._src()
        assert "gettext_in(want" in src, (
            "只用了当前语言 —— 看不懂当前语言的人正是要切的那个人")

    def test_title_body_and_both_buttons(self):
        src = self._src()
        for part in ("title=", "ok_text=", "cancel_text="):
            assert part in src or part.rstrip("=") in src, part
        # 四处都得走同一个双语助手,漏一处那一处就只剩一种语言
        assert src.count("two(") >= 4, (
            f"只有 {src.count('two(')} 处走了双语助手,标题/正文/两个按钮共四处")

    def test_gettext_in_does_not_disturb_the_current_language(self):
        """**不能靠 `set_language` 切过去再切回来** —— 那期间别的线程会读到错的。"""
        before = i18n.current_language()
        i18n.gettext_in("en", "取消")
        assert i18n.current_language() == before

    def test_bilingual_collapses_when_both_sides_are_the_same(self):
        """中文 → 中文时不该显示两遍一模一样的字。"""
        i18n.set_language("zh_CN")
        assert i18n.bilingual("zh_CN", "取消") == "取消"

    def test_it_really_reads_the_other_catalog(self):
        if "en" not in i18n.available_languages():
            pytest.skip("没有编译好的 en 目录")
        i18n.set_language("zh_CN")
        got = i18n.gettext_in("en", "取消")
        assert got != "取消", "拿不到英文 —— 双语框会退化成一种语言"


class TestTheSwitcherActuallyRuns:
    """**上面那些断言的是那个框里写什么字,没有一条真去点它。**

    于是 `Shell._set_language` 里那句 `self.confirm(...)` 一直是
    `AttributeError` —— `confirm` 只长在 `Page` 上,`Shell` 上根本没有。
    语言切换是**点一下就崩**,而 i18n 那一整轮的测试全是绿的。

    是用户在 Mac 上点了一下才发现的。这几条走真调用。
    """

    @pytest.fixture(scope="module")
    def qt_app(self):
        pytest.importorskip("PySide6")
        from PySide6.QtWidgets import QApplication

        from astro_smb_qt import theme

        inst = QApplication.instance() or QApplication([])
        theme.apply(inst)
        return inst

    def test_shell_can_confirm(self, qt_app):
        """`Shell` 得真有这个方法 —— 不是 `Page` 有就行。"""
        from astro_smb_qt.shell import Shell

        assert callable(getattr(Shell, "confirm", None)), (
            "Shell 没有 confirm,而 _set_language 里就在调它")

    def test_declining_the_restart_puts_the_dropdown_back(self, qt_app,
                                                          monkeypatch):
        """**真走一遍 `_set_language`。**

        用户点「取消」时下拉要拨回当前语言,否则它显示的语言和实际的对不上。
        这条同时也是"这个函数根本跑不跑得起来"的守卫。
        """
        from astro_smb import i18n
        from astro_smb_qt.shell import Shell

        langs = i18n.available_languages()
        if len(langs) < 2:
            pytest.skip("只装了一种语言,没有下拉可拨")

        shell = Shell.__new__(Shell)         # 不建整个窗口
        shell._langs = list(langs)
        asked = {}

        class _Combo:
            def __init__(self):
                self.idx = 0

            def blockSignals(self, _b):
                pass

            def setCurrentIndex(self, i):
                self.idx = i

        shell.lang_combo = _Combo()
        shell.transfers = None
        monkeypatch.setattr(Shell, "confirm",
                            lambda self, *a, **k: asked.setdefault("hit", True) and False)
        restarted = []
        monkeypatch.setattr(Shell, "_restart", lambda self: restarted.append(1))

        target = next(i for i, k in enumerate(langs)
                      if k != i18n.current_language())
        Shell._set_language(shell, target)

        assert asked.get("hit"), "根本没弹确认框"
        assert not restarted, "用户点了取消,却还是重启了"
        cur = i18n.current_language()
        want = langs.index(cur) if cur in langs else 0
        assert shell.lang_combo.idx == want, (
            "取消之后下拉没拨回来 —— 它显示的语言和实际的对不上")
