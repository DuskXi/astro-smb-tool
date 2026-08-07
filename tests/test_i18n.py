"""i18n 的机制,以及**判读不许经过显示文本**。

这个仓库有一类系统性缺陷:**拿显示文本当身份**。平时只是脆(改一句文案
就静默失效),而 i18n 是"把每个字都改一遍" —— 于是它把这些雷一次性引爆。

已经炸出来的三个真 bug(都不报错,只是行为悄悄不对):

1. `views/transfers.section_of` 比 ``"排队"``,而常量是 ``"排队中"`` ——
   **「排队」分区永远是空的**;
2. `views/records._guide_card` 在**中文显示短语**里找"丢星"/"失败"来标警告 ——
   翻译之后丢星那段不再标警告;
3. `views/sky3d._build_nights` 比 ``"FITS 实测"`` 来决定 FITS 坐标是否覆盖
   日志坐标 —— 失效时天球上的点静默退回 goto 请求值(与实测恒差约 21′)。

所以这份文件里的断言都**不看中文**,只看:键、以及"换语言之后行为不变"。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

from astro_smb import i18n

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _restore_language():
    """每条用例跑完把语言还原 —— 否则一条测试会污染后面所有的。"""
    before = i18n.current_language()
    yield
    i18n.set_language(before)


class TestTheMechanism:

    def test_chinese_needs_no_catalog(self):
        """**源语言就是 msgid**,所以中文永远不需要 `.mo`。

        这条保证"没装任何翻译文件"时行为与做 i18n 之前**一个字不差**。
        """
        i18n.set_language("zh_CN")
        assert i18n.gettext("(偏低)") == "(偏低)"

    def test_an_unknown_language_falls_back_quietly(self):
        """少一个翻译文件不该让程序起不来。"""
        assert i18n.set_language("xx_YY") == i18n.SOURCE_LANGUAGE
        assert i18n.gettext("(偏低)") == "(偏低)"

    def test_english_actually_translates(self):
        if "en" not in i18n.available_languages():
            pytest.skip("没有编译好的 en 目录(先跑 scripts/i18n_build.py)")
        i18n.set_language("en")
        assert i18n.gettext("(偏低)") != "(偏低)"

    def test_source_language_is_listed_even_without_catalogs(self):
        assert i18n.SOURCE_LANGUAGE in i18n.available_languages()


class TestJudgementDoesNotGoThroughDisplayText:
    """**换语言之后判读必须一模一样。**"""

    def _nights(self):
        from astro_smb.autorunlog import aggregate_nights, parse_autorun_log

        d = ROOT / ".tmp" / "device" / "EMMC Images" / "log"
        if not d.is_dir():
            pytest.skip("没有离线镜像的日志")
        logs = [parse_autorun_log(p.read_text(encoding="utf-8", errors="replace"),
                                  p.name)
                for p in sorted(d.glob("Autorun_Log_*.txt"))
                if not p.name.endswith("_CHN.txt")]
        nights = aggregate_nights(logs)
        if not nights:
            pytest.skip("日志里没有夜次")

        class _D:
            pass

        obj = _D()
        obj.nights = nights
        return obj

    def test_fits_still_overrides_the_log_coordinates_in_english(self):
        """**这条是那个 21′ 的雷。**

        原来判"这条是不是 FITS 实测"比的是**显示文本**。把那句话真的翻成
        英文之后,比较就永远为假 —— FITS 坐标不再覆盖 goto 请求值,
        而界面上什么都不会说。
        """
        from astro_smb_app.views import sky3d as sv

        data = self._nights()
        run = next(r for n in data.nights for r in n.runs)
        coords = {id(run): {"ra_deg": 338.27, "dec_deg": -20.7}}

        seen = {}
        for lang in ("zh_CN", "en"):
            i18n.set_language(lang)
            t = next(t for n in sv._build_nights(data, coords)
                     for t in n["targets"] if t["name"] == run.target)
            seen[lang] = (t["source_key"], round(t["ra"], 6))
            assert t["source_key"] == sv.SRC_FITS, (lang, t)
        assert seen["zh_CN"] == seen["en"], seen

    def test_the_guide_card_severity_is_language_independent(self):
        """丢星要标警告 —— 而"丢星"这两个字是会被翻译的。"""
        from datetime import datetime

        from astro_smb_app.views import records as rv

        class _E:
            def __init__(self, ev):
                self.event = ev
                self.time = datetime(2026, 7, 30, 1, 0)

        for lang in ("zh_CN", "en"):
            i18n.set_language(lang)
            assert rv._guide_card([_E("Star lost")])["level"] == "warn", lang
            assert rv._guide_card([_E("Settle done")])["level"] == "info", lang

    def test_the_altitude_verdict_is_a_key_not_a_sentence(self):
        from astro_smb_app.views import browser as bv

        for lang in ("zh_CN", "en"):
            i18n.set_language(lang)
            assert bv.alt_verdict(35.5) == bv.ALT_SOMEWHAT_LOW, lang
            assert bv.alt_verdict(12.0) == bv.ALT_LOW, lang
            assert bv.alt_verdict(-3.0) == bv.ALT_BELOW_HORIZON, lang
            assert bv.alt_verdict(60.0) == bv.ALT_OK, lang

    def test_the_altitude_hint_does_change(self):
        """反面保险:文案**应该**随语言变,否则说明 `_()` 根本没接上。"""
        if "en" not in i18n.available_languages():
            pytest.skip("没有编译好的 en 目录")
        from astro_smb_app.views import browser as bv

        i18n.set_language("zh_CN")
        zh = bv._alt_hint(35.5)
        i18n.set_language("en")
        assert bv._alt_hint(35.5) != zh


class TestTheCatalogCompiler:
    """`.po → .mo` 那个脚本自己也得有人守。"""

    def test_fuzzy_entries_are_skipped(self):
        """`#,fuzzy` 是"机器猜的,没人确认过" —— 编进去等于把半成品发给用户。"""
        import scripts.i18n_build as b

        po = ('msgid ""\nmsgstr "Content-Type: text/plain; charset=UTF-8\\n"\n'
              '\n#, fuzzy\nmsgid "甲"\nmsgstr "A"\n'
              '\nmsgid "乙"\nmsgstr "B"\n')
        pairs = b.parse_po(po)
        assert "乙" in pairs and pairs["乙"] == "B"
        assert "甲" not in pairs, "fuzzy 条目被编进去了"

    def test_empty_translations_are_dropped(self):
        """空 msgstr 会被 gettext 当成"翻译成空串" —— 界面上直接一片空白。"""
        import scripts.i18n_build as b

        mo = b.build_mo({"甲": "", "乙": "B"})
        assert b"\xe4\xb9\x99" in mo          # 乙 在
        # 甲 不该作为**有翻译的条目**进去
        assert mo.count("甲".encode()) == 0

    def test_the_header_is_present(self):
        """少了那条空 msgid 的头,gettext 按 ASCII 解码,中文 msgid 直接崩。"""
        import gettext as g
        import io as _io

        import scripts.i18n_build as b

        mo = b.build_mo({"甲": "A"})
        t = g.GNUTranslations(_io.BytesIO(mo))
        assert t.gettext("甲") == "A"


class TestCoreErrorsAreJudgedByCodeNotText:
    """核心库不许拿**自己的中文错误消息**做判断。

    `makedirs` 原来靠 ``"已存在" in str(e)`` 忽略"目录已存在"。核心库的异常
    消息是会显示给用户的(项目约定:对外只抛 `SmbClientError`,message 已
    人类可读),所以它一定会进翻译范围 —— 一翻,建目录就开始报错。
    """

    def test_the_error_carries_a_status_code(self):
        from impacket import nt_errors

        from astro_smb.client import SmbClientError

        e = SmbClientError("目标已存在 [x]",
                           status=nt_errors.STATUS_OBJECT_NAME_COLLISION)
        assert e.status == nt_errors.STATUS_OBJECT_NAME_COLLISION

    def test_status_is_optional(self):
        """老调用点 ``SmbClientError("…")`` 一个字都不用改。"""
        from astro_smb.client import SmbClientError

        assert SmbClientError("普通错误").status is None

    def test_makedirs_matches_on_the_code(self):
        """**查反模式,不查词。**

        第一版断言"函数体里不许出现『已存在』" —— 结果命中的是它自己的
        文档字符串「递归创建远程目录(已存在则忽略)」,也就是**描述正确
        行为的那句话**。要查的是 ``in str(e)`` 这个动作。
        """
        import ast
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1] / "astro_smb"
               / "client.py").read_text(encoding="utf-8")
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "makedirs")
        body = "\n".join(ast.unparse(b) for b in fn.body[1:])   # 跳过文档串
        assert "STATUS_OBJECT_NAME_COLLISION" in body, "没有按状态码判"
        assert "in str(e)" not in body, "还在拿消息文本做判断"

    def test_the_wrapper_passes_the_code_through(self):
        """`_run` 包装 SessionError 时要把状态码带上,否则上面那条判不了。

        **要找对地方。** `_friendly_session_error` 在这个文件里出现十来次,
        第一处在 `connect()` 里(那处不需要状态码)—— 按第一处查会查错。
        """
        import ast
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1] / "astro_smb"
               / "client.py").read_text(encoding="utf-8")
        fn = next(n for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.FunctionDef) and n.name == "_run")
        body = "\n".join(ast.unparse(b) for b in fn.body)
        assert "status=e.getErrorCode()" in body, (
            "_run 没把状态码带上,调用方只能回去比消息文本")


class TestUnderscoreIsNotAThrowawayName:
    """**`_` 归 gettext,不许再拿它当丢弃变量。**

    这条是补给一次真事故的:`views/guidedash.aggregate_group` 里有一句
    ``h_ra, _ = np.histogram(...)``。那是**函数级赋值**,于是 `_` 在整个函数里
    都成了局部名 —— 而函数开头几行的 `_("…")` 在它被赋值之前就执行了,
    直接 `UnboundLocalError`。

    最坏的地方在于它**只在走到那条路径的那一组上炸**(真机 11 个导星分组里
    只有第 10 组有直方图数据),所以整轮单测是绿的,差分验证才把它捞出来。

    推导式/生成式里的 `for _, x in …` 有自己的作用域,不受影响,所以这条
    只查函数体里的真赋值。
    """

    def _offenders(self):
        import ast

        out = []
        for pkg in ("astro_smb", "astro_smb_app", "astro_smb_qt", "astro_smb_gui"):
            for p in sorted((ROOT / pkg).rglob("*.py")):
                src = p.read_text(encoding="utf-8")
                if "i18n import" not in src:
                    continue          # 没引进 `_` 的模块随便用
                tree = ast.parse(src)
                for fn in ast.walk(tree):
                    if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    for node in ast.walk(fn):
                        targets = []
                        if isinstance(node, ast.For):
                            targets = [node.target]
                        elif isinstance(node, ast.Assign):
                            targets = node.targets
                        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                            targets = [node.target]
                        for tgt in targets:
                            for sub in ast.walk(tgt):
                                if isinstance(sub, ast.Name) and sub.id == "_":
                                    out.append(
                                        f"{p.relative_to(ROOT).as_posix()}"
                                        f":{node.lineno} ({fn.name})")
        return sorted(set(out))

    def test_nobody_rebinds_underscore(self):
        bad = self._offenders()
        assert bad == [], (
            "这些地方把 `_` 当丢弃变量用了,而同一个函数里还在调 `_()` 翻译 ——\n"
            "  " + "\n  ".join(bad)
            + "\n改成 `_edges` / `_unused` 之类的具名丢弃变量。")


class TestSystemLanguageDetection:
    """**系统语言识别**:Windows 上 `locale.getlocale()` 是靠不住的。

    它给的是 ``'Chinese (Simplified)_China'`` / ``'English_United States'``
    这种 **Windows 风格**的名字,而 `locale.normalize()` 对这两个都不认
    (实测原样返回)。原来的判据是 ``want.startswith("zh")`` ——

    * 中文机器:``'Chinese (Simplified)_China'.startswith('zh')`` 是 **False**,
      于是被判成"不是中文",拿这串去找 `.mo`、找不到、兜底回中文 ——
      **结果碰巧对,过程全错**;
    * 英文机器:同样兜底回中文,也就是**装了 `en` 词表界面也永远是中文**。

    这条不报错、不崩,只是"换了系统语言没反应"。
    """

    def test_env_wins(self, monkeypatch):
        monkeypatch.setenv("ASTRO_SMB_LANG", "en_US")
        assert i18n.detect_language() == "en_US"

    def test_posix_env_is_honoured(self, monkeypatch):
        """gettext 的老规矩:`LANGUAGE` / `LC_ALL` / `LANG` 也要看。"""
        monkeypatch.delenv("ASTRO_SMB_LANG", raising=False)
        monkeypatch.setenv("LANGUAGE", "de_DE:en_US")
        assert i18n.detect_language() == "de_DE", "LANGUAGE 的冒号列表没取第一个"

    def test_windows_ui_language_is_posix_style(self, monkeypatch):
        """Windows 上走 `GetUserDefaultUILanguage`,拿到的必须是 `zh_CN` 那种。"""
        import sys

        if sys.platform != "win32":
            pytest.skip("只在 Windows 上有意义")
        for var in i18n._LANG_ENV:
            monkeypatch.delenv(var, raising=False)
        got = i18n.detect_language()
        assert "_" in got and " " not in got, (
            f"识别出来的是 {got!r} —— 那是 Windows 风格的名字,gettext 不认")
        assert got.split("_")[0].isalpha() and len(got.split("_")[0]) in (2, 3)

    def test_a_windows_style_name_does_not_silently_mean_chinese(self):
        """**反面保险。** 直接喂 Windows 风格的英文名字,不许被当成中文。

        这条是那个 bug 的本体:它当时会走到"找不到 .mo → 兜底回 zh_CN",
        结果与"识别成中文"**在行为上一模一样** —— 所以要看的是
        `set_language` 有没有走那条"这是中文,不用装 .mo"的短路。
        """
        i18n.set_language("English_United States")
        # 走兜底是可以接受的(那串确实没有对应词表);不可接受的是把它
        # **当成中文**而根本不去找。这里用一个真实存在的词表来区分两者:
        assert i18n.set_language("en") == "en", "en 词表都装不上,说明短路判错了"

    def test_regional_chinese_still_counts_as_chinese(self):
        for name in ("zh", "zh_CN", "zh_TW", "zh-Hans", "zh_CN.UTF-8"):
            assert i18n.set_language(name) == i18n.SOURCE_LANGUAGE, name


class TestNoFrozenTranslations:
    """**翻译不许在 import 时定型。**

    两种写法会踩这个,而且症状一模一样(界面上有一块永远是旧语言,不报错):

    1. 模块级常量 ``X = _("中文")``;
    2. **函数默认值** ``def f(t=_("确定"))`` —— `def` 执行时求值,也就是 import 时。

    第二种是后来才发现的:`scripts/i18n_wrap.py` 原来只查第一种,
    `_at_module_level` 一路往上走碰到 `FunctionDef` 就放行,于是默认值被当成
    "在函数体里"。真机上有 6 处(两套前端确认框的「确定」/「取消」、忙态文案)。
    """

    def _offenders(self):
        import ast

        out = []
        for pkg in ("astro_smb", "astro_smb_app", "astro_smb_qt", "astro_smb_gui"):
            for p in sorted((ROOT / pkg).rglob("*.py")):
                tree = ast.parse(p.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        vals = list(node.args.defaults) + [
                            d for d in node.args.kw_defaults if d]
                        where = f"{p.relative_to(ROOT).as_posix()}:{node.lineno}"
                    elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                        continue          # 模块级常量由 i18n_wrap 的报告盯着
                    else:
                        continue
                    for v in vals:
                        for sub in ast.walk(v):
                            if (isinstance(sub, ast.Call)
                                    and isinstance(sub.func, ast.Name)
                                    and sub.func.id in ("_", "gettext")):
                                out.append(f"{where} ({node.name})")
        return sorted(set(out))

    def test_no_translation_in_a_function_default(self):
        bad = self._offenders()
        assert bad == [], (
            "这些函数把 `_()` 写在默认值里,翻译会冻在 import 时的语言上:\n  "
            + "\n  ".join(bad)
            + "\n改成默认 None,进函数体再 `_()`。")

    def test_the_wrapper_refuses_to_create_them(self):
        """转换器自己也得知道这条,否则下一轮又会包出来。"""
        import sys

        sys.path.insert(0, str(ROOT / "scripts"))
        import i18n_wrap as W

        src = 'def f(t=_UNSET):\n    pass\n'.replace("_UNSET", '"确定"')
        import ast as _ast

        tree = _ast.parse(src)
        f = W.Finder(tree)
        f.visit(tree)
        assert f.hits == [], "转换器把函数默认值包了 —— 那会冻住翻译"
        assert any(why == W.Skip.DEFAULT_ARG for _ln, _s, why in f.skips)


class TestIdentityFunctionsStayLanguageIndependent:
    """**身份函数的返回值不许随语言变。**

    `entries.ext_category_id` 的返回值同时是三处的键:行首符号表、treemap
    调色板(`crc32(类名) % 8`)、明细里的"种类"。一翻译:符号全变兜底方块、
    每种文件的颜色整体洗牌 —— 都不报错。

    这条是补给一次真事故的:2026-08-05 最后一轮**机械清扫**把
    `ext_category_id` 里那个 `" 文件"` 也包成了 `_()`,于是身份变成
    `XYZ⟦ 文件⟧`。全量测试在中文下照样全绿,是伪语言跑出来才发现的。
    """

    def _entry(self, name: str, is_dir: bool = False):
        from astro_smb.client import RemoteEntry

        return RemoteEntry(share="s", path=name, name=name, is_dir=is_dir,
                           size=1, mtime=0.0, ctime=0.0, atime=0.0,
                           attributes=0)

    def test_the_id_is_the_same_in_every_language(self):
        from astro_smb_app.entries import ext_category_id

        names = ["a.fit", "a.jpg", "log.txt", "x.xyz", "noext"]
        seen = {}
        for lang in ("zh_CN", "en", "xx_PS"):
            i18n.set_language(lang)
            seen[lang] = [ext_category_id(self._entry(n)) for n in names]
            seen[lang].append(ext_category_id(self._entry("d", is_dir=True)))
        assert len(set(map(tuple, seen.values()))) == 1, seen

    def test_the_palette_index_therefore_does_not_move(self):
        """**颜色跟着语言变**是这条真正的症状 —— 直接量它。"""
        from astro_smb_app.entries import ext_category_id
        from astro_smb_app.views.space import palette_index

        got = set()
        for lang in ("zh_CN", "en", "xx_PS"):
            i18n.set_language(lang)
            got.add(palette_index(ext_category_id(self._entry("a.fit"))))
        assert len(got) == 1, f"同一种文件在不同语言下落到了不同色号: {got}"

    def test_the_display_side_does_change(self):
        """反面保险:**显示**那一支该变 —— 否则说明 `_()` 根本没接上。"""
        from astro_smb_app.entries import ext_category

        i18n.set_language("zh_CN")
        zh = ext_category(self._entry("a.fit"))
        i18n.set_language("xx_PS")
        if i18n.current_language() != "xx_PS":
            pytest.skip("没生成伪语言(先跑 scripts/i18n_pseudo.py)")
        assert ext_category(self._entry("a.fit")) != zh


class TestLiteralsReachTheExtractor:
    """**只有 `_` / `gettext` / `N_` 这三个名字会被抽取器看见。**

    真事:语言切换那个确认框自己包了个 `two(msg)`(当前语言 + 目标语言各印
    一遍),字面量直接写在 `two("切换到 {name}?")` 里。抽取器不认识 `two`,
    于是这四条**从来没进过词表** —— 框照弹,只是两边印的都是当前语言,
    而这个框存在的全部意义就是给**看不懂当前语言的人**看。

    不报错、不缺字、界面一切正常。只有拿 `.pot` 数条数才看得出来。
    """

    #: 自己包了一层的翻译辅助。它们收的是 **msgid**,不是成品文本。
    WRAPPERS = {"two", "bilingual"}

    def _bare_literals(self):
        bad = []
        for path in sorted(ROOT.glob("astro_smb*/**/*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                name = (fn.id if isinstance(fn, ast.Name) else
                        fn.attr if isinstance(fn, ast.Attribute) else "")
                if name not in self.WRAPPERS or not node.args:
                    continue
                a = node.args[0]
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    rel = path.relative_to(ROOT).as_posix()
                    bad.append(f"{rel}:{node.lineno}: {name}({a.value[:30]!r})")
        return bad

    def test_wrapped_literals_are_marked(self):
        assert not self._bare_literals(), (
            "这些字面量传给了抽取器不认识的辅助函数,包一层 N_() 就行:\n  "
            + "\n  ".join(self._bare_literals()))


class TestTheCompiledCatalogComesFromThePo:
    """`.mo` 是**编出来的**,却跟着源码一起进版本控制。

    真事:`.mo` 里有四条译文,而 `.po` 里根本没有这几个 msgid ——
    它们是在某次 `.po` 被重新合并时掉的,而 `.mo` 没人重编,于是继续
    生效。症状是**评审看不见的译文在跑**;而下一次谁重编一次 `.mo`,
    它们就无声消失了。两个方向都不报错。
    """

    def test_every_mo_matches_its_po(self):
        sys.path.insert(0, str(ROOT / "scripts"))
        import i18n_build as B

        stale = []
        for po in sorted((ROOT / "astro_smb" / "locale").rglob("*.po")):
            mo = po.with_suffix(".mo")
            if not mo.exists():
                stale.append(f"{po.parent.parent.name}: 没有 .mo")
                continue
            entries = B.parse_po(po.read_text(encoding="utf-8"))
            if B.build_mo(entries) != mo.read_bytes():
                stale.append(f"{po.parent.parent.name}: .mo 与 .po 对不上")
        assert not stale, (
            "重新编一遍:uv run python scripts/i18n_build.py —— " + "; ".join(stale))
