"""国际化基础设施(纯标准库 `gettext`,不引入任何依赖、不破坏分层)。

## 为什么是 gettext 而不是 Qt 的 `tr()`

判读文案住在**共享层** `astro_smb_app/views/`,而共享层不能 import PySide6
(分层约束,`test_shared_layer_never_imports_a_frontend` 守着)。用 `tr()` 的话
那批文案还得另配一套机制 —— **一个项目两套 i18n**,正是这个仓库一直在防的
"双实现"。`gettext` 是标准库,三处(核心库 / 共享层 / 两套前端)通用。

## msgid 用中文原文,不用符号键

`_("偏低")` 而不是 `_("alt.somewhat_low")`。理由:

* **源码保持可读。** 这个仓库的注释与文案密度很高,把中文全搬进 `.po`
  之后源码里只剩一串符号,读的人要开两个文件才知道界面上写的是什么。
* **中文不需要任何翻译文件。** msgid 就是中文,没有 `.mo` 时 gettext 退化成
  原样返回 —— 也就是现在的行为,一个字都不会变。

## 那测试怎么与语言无关

**不靠 msgid,靠判读函数另外返回的语义键。** 见 `views.browser.alt_verdict`:
判读只回一个稳定的键(`ALT_LOW` 之类),显示文本是另一支函数。测试断言键,
界面显示走 `_()`。

这条是用户 2026-08-03 定的("断言改成测 key/结构")。它顺带解掉这个仓库的
一个老毛病:**文案耦合** —— 断言绑在中文串上,改一句文案红一片,
于是没人敢改文案。
"""
from __future__ import annotations

import gettext as _gettext
import os
import sys
import threading
from pathlib import Path

__all__ = ["DOMAIN", "LOCALE_DIR", "available_languages", "current_language",
           "set_language", "gettext", "ngettext", "N_",
           "gettext_in", "bilingual",
           "detect_language"]

#: 翻译域。核心库与共享层共用一个 —— 它们对用户是同一套说法。
DOMAIN = "astro_smb"

#: `.mo` 的所在:`astro_smb/locale/<lang>/LC_MESSAGES/astro_smb.mo`
LOCALE_DIR = Path(__file__).resolve().parent / "locale"

#: 源语言。msgid 就是它,所以它**永远不需要 `.mo`**。
SOURCE_LANGUAGE = "zh_CN"

_lock = threading.RLock()
_current = SOURCE_LANGUAGE
_trans: _gettext.NullTranslations = _gettext.NullTranslations()


#: 伪语言前缀。`xx_PS` 是 `scripts/i18n_pseudo.py` 生成的验收工具
#: (每条 msgid 加 ⟦⟧ 壳),**不能出现在给用户看的语言列表里**。
#: 仍然能用 `ASTRO_SMB_LANG=xx_PS` 显式切过去 —— `set_language` 不查这张表。
PSEUDO_PREFIX = "xx"


def available_languages() -> list[str]:
    """本机能用的语言(源语言 + 装了 `.mo` 的那些)。**不含伪语言。**"""
    langs = {SOURCE_LANGUAGE}
    if LOCALE_DIR.is_dir():
        for d in LOCALE_DIR.iterdir():
            if d.name.split("_")[0] == PSEUDO_PREFIX:
                continue
            if (d / "LC_MESSAGES" / f"{DOMAIN}.mo").is_file():
                langs.add(d.name)
    return sorted(langs)


def coverage(lang: str) -> float:
    """`lang` 翻了多少(0.0~1.0)。源语言恒为 1.0,读不出来返回 0.0。

    数字是 `scripts/i18n_build.py` 在编 `.mo` 时写进头里的
    (`X-Translated` / `X-Total`)。**运行时数不出来** —— `.mo` 里
    根本没有未翻译的条目,分母无从谈起。

    界面要用它:提供一个只翻了 4% 的语言而不说,用户切过去看见满屏
    中文,只会以为切换坏了。
    """
    if lang == SOURCE_LANGUAGE:
        return 1.0
    try:
        with _lock:
            tr = _others.get(lang)
            if tr is None:
                tr = _gettext.translation(DOMAIN, localedir=str(LOCALE_DIR),
                                          languages=[lang])
                _others[lang] = tr
        info = tr.info()
        done = int(info.get("x-translated", 0))
        total = int(info.get("x-total", 0))
    except (OSError, ValueError, AttributeError, KeyError):
        return 0.0
    return (done / total) if total else 0.0


def current_language() -> str:
    return _current


#: 按 gettext 的惯例依次看这几个环境变量(`ASTRO_SMB_LANG` 是本项目自己的覆盖口)
_LANG_ENV = ("ASTRO_SMB_LANG", "LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG")


def detect_language() -> str:
    """猜用户的语言。**返回的是 POSIX 风格的名字**(`zh_CN` / `en_US`)。

    **不能只靠 `locale.getlocale()`。** Windows 上它给的是
    ``'Chinese (Simplified)_China'`` / ``'English_United States'`` 这种
    **Windows 风格**的名字,而 `locale.normalize()` 对这两个都不认(实测原样
    返回;只有 ``German_Germany`` 之类碰巧在别名表里)。

    后果很隐蔽:``'Chinese (Simplified)_China'.startswith('zh')`` 是 **False**,
    于是中文机器被判成"不是中文",拿这串去找 `.mo`、找不到、兜底回中文 ——
    **结果碰巧对,过程全错**。而英文 Windows 上同样兜底回中文,
    也就是说**装了 `en` 词表界面也永远是中文**。

    可靠的那条路是 `GetUserDefaultUILanguage()`(要的正是**界面语言**,
    不是数字/日期的格式区域),再经 `locale.windows_locale` 换成 `zh_CN`。
    """
    import locale

    for var in _LANG_ENV:
        v = (os.environ.get(var) or "").strip()
        if v:
            return v.split(":")[0]          # LANGUAGE 可以是 `de:en` 这种列表

    if sys.platform == "win32":
        try:
            import ctypes

            lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            name = locale.windows_locale.get(lcid)
            if name:
                return name
        except Exception:                   # noqa: BLE001 - 拿不到就往下走
            pass

    # **不要用 `locale.getdefaultlocale()`** —— 3.11 起废弃、3.15 移除,
    # 全量测试里会刷一片 DeprecationWarning。`getlocale()` 是替代品,
    # 但它在没设过 locale 的进程里可能给 (None, None),所以要兜住。
    try:
        raw = locale.getlocale()[0] or ""
    except (TypeError, ValueError):
        raw = ""
    if raw:
        norm = locale.normalize(raw)
        return norm if norm else raw
    return SOURCE_LANGUAGE


def set_language(lang: str | None = None) -> str:
    """切换语言;返回真正生效的那个。

    ``None`` = 按环境挑(见 `detect_language`)。
    **找不到对应的 `.mo` 就退回源语言**,不抛异常 —— 少一个翻译文件
    不该让程序起不来。

    `gettext` 自己会做回退展开(``en_US.UTF-8`` → ``en_US`` → ``en``),
    所以这里只管把名字弄成 POSIX 风格,不用自己拆。
    """
    global _current, _trans

    with _lock:
        want = lang or detect_language()
        # **比语言主码,不比整串。** `zh_CN` / `zh_TW` / `zh_Hans_CN` 都是中文,
        # 而 `startswith("zh")` 会把 `zho`… 之外的写法漏掉又把别的误收。
        if want.split(".")[0].split("_")[0].replace("-", "_").lower() == "zh":
            # 中文:msgid 本身就是中文,不需要也不该去装 .mo
            _trans = _gettext.NullTranslations()
            _current = SOURCE_LANGUAGE
            return _current
        try:
            _trans = _gettext.translation(DOMAIN, localedir=str(LOCALE_DIR),
                                          languages=[want])
            _current = want
        except OSError:
            _trans = _gettext.NullTranslations()
            _current = SOURCE_LANGUAGE
        return _current


def gettext(message: str) -> str:
    """翻一条。没装翻译时原样返回(也就是中文)。"""
    return _trans.gettext(message)


#: `gettext_in` 的翻译器缓存(每种语言只装一次 `.mo`)
_others: dict[str, _gettext.NullTranslations] = {}


def gettext_in(lang: str, message: str) -> str:
    """**用指定语言**翻一句,不动当前语言。

    存在的理由很具体:**语言切换的确认框必须是双语的。** 一个只会英文的用户
    被中文界面挡住,而拦住他的那个框如果也是中文的,他连"确定"是哪个都不知道 ——
    这是最需要双语的一处,而它偏偏最容易被忽略(开发的人看得懂中文,一切正常)。

    所以那个框要同时用**当前语言**和**目标语言**各写一遍,后者就得靠这个函数。
    """
    with _lock:
        if lang.split(".")[0].split("_")[0].replace("-", "_").lower() == "zh":
            return message              # 源语言:msgid 本身就是中文
        t = _others.get(lang)
        if t is None:
            try:
                t = _gettext.translation(DOMAIN, localedir=str(LOCALE_DIR),
                                         languages=[lang])
            except OSError:
                t = _gettext.NullTranslations()
            _others[lang] = t
        return t.gettext(message)


def bilingual(lang: str, message: str, *, sep: str = "\n") -> str:
    """当前语言 + 目标语言各一遍(两边一样时只出一遍)。

    `sep` 默认换行 —— 对话框正文用换行;标题挤在一行时传 `" / "`。
    """
    here = gettext(message)
    there = gettext_in(lang, message)
    return here if here == there else here + sep + there


def N_(message: str) -> str:
    """**只做标记,不翻译。** 用在模块级的查表常量上。

    模块级的东西在 import 时求值一次。``KINDS = {"Light": _("亮场")}`` 会把
    翻译**冻在 import 那一刻的语言**上,之后 `set_language()` 再也改不动它 ——
    不报错,只是界面上有一块永远是旧语言。这个仓库里这类"静默不对"是最贵的
    一类 bug,所以宁可多一个函数也要把它分开:

    * 表里放 ``N_("亮场")`` —— 抽取器能看见,运行时原样返回;
    * **取用的地方**再 ``_(KINDS[k])`` —— 那时候才按当前语言翻。

    `scripts/i18n_wrap.py` 会拒绝自动包模块级常量,并把它们单独列出来
    要人处理,处理办法就是这一条。
    """
    return message


def ngettext(singular: str, plural: str, n: int) -> str:
    """复数形式。

    中文没有复数变化,但英/俄/阿拉伯语有 —— `N 帧` 这类文案必须走它,
    否则那些语言只能得到一个"差不多对"的形式。
    """
    return _trans.ngettext(singular, plural, n)


# 按环境初始化一次;调用方随时可以再 set_language()
set_language()
