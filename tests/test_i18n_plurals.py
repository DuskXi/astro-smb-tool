"""复数形式:机制这一层。

中文没有复数变化,所以**这条路在中文下永远看不出问题** —— 而英语两式、
俄语三式、阿拉伯语六式都得靠它。两个具体的坑:

1. **`.po` 解析器原来读不了复数条目**(`msgid_plural` / `msgstr[N]` 都被
   当成未知行跳过),于是那条目整个丢失。不报错,只是 `ngettext` 拿不到
   翻译、永远退回原文。
2. **`Plural-Forms` 头缺了不报错**:gettext 会退回一条默认的日耳曼语规则
   (`n != 1`)。英语碰巧对,所以本地怎么试都正常;俄语会静默取错形式。

所以这份测试**用俄语**验 —— 英语两式下对不对根本看不出来。
"""
from __future__ import annotations

import gettext as g
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import i18n_build as b            # noqa: E402

RU_RULE = ("nplurals=3; plural=(n%10==1 && n%100!=11 ? 0 : "
           "n%10>=2 && n%10<=4 && (n%100<10 || n%100>=20) ? 1 : 2);")

#: 一份**手写的** .po 文本。`\n` 在 .po 里是**两个字符**(反斜杠 + n),
#: 所以这里必须写成 `\\n` —— 写成真换行的话行尾就没有收尾引号,
#: 解析出来的值会连引号一起留着(调试这条花了不少时间)。
_HEAD = ('msgid ""\n'
         'msgstr "Content-Type: text/plain; charset=UTF-8\\n"\n')


def _po(rule: str) -> str:
    return (_HEAD
            + f'"Plural-Forms: {rule}\\n"\n'
            + "\n"
            + 'msgid "{n} 帧"\n'
              'msgid_plural "{n} 帧"\n'
              'msgstr[0] "{n} кадр"\n'
              'msgstr[1] "{n} кадра"\n'
              'msgstr[2] "{n} кадров"\n')


def _trans(rule: str = RU_RULE) -> g.GNUTranslations:
    return g.GNUTranslations(io.BytesIO(b.build_mo(b.parse_po(_po(rule)))))


class TestThePoTextItself:
    """先验这份夹具本身 —— 它错了下面每一条都会跟着错。"""

    def test_the_header_line_is_one_line_with_a_closing_quote(self):
        lines = _po(RU_RULE).splitlines()
        assert lines[1].startswith('msgstr "') and lines[1].endswith('"'), lines[1]


class TestThePipelineHandlesPlurals:

    def test_the_parser_keeps_the_entry(self):
        pairs = b.parse_po(_po(RU_RULE))
        plural = [k for k in pairs if b.NUL in k]
        assert plural, "复数条目被整个丢掉了 —— ngettext 会永远退回原文"
        assert pairs[plural[0]].split(b.NUL) == ["{n} кадр", "{n} кадра",
                                                 "{n} кадров"]

    def test_the_header_survives_into_the_mo(self):
        assert _trans().info().get("plural-forms"), (
            "`.mo` 里没有 Plural-Forms —— gettext 会退回日耳曼语默认规则,"
            "英语碰巧对,俄语全错")

    def test_russian_picks_three_different_forms(self):
        """**这条是重点。** 英语两式下"对不对"看不出来,俄语才看得出来。"""
        t = _trans()
        got = {n: t.ngettext("{n} 帧", "{n} 帧", n) for n in (1, 3, 11)}
        assert got[1].endswith("кадр"), got
        assert got[3].endswith("кадра"), got
        assert got[11].endswith("кадров"), got
        assert len(set(got.values())) == 3

    def test_a_missing_header_is_filled_in_not_left_empty(self):
        """没写头的 `.po` 也要能编 —— 补一条默认规则,而不是让 .mo 没有头。"""
        mo = b.build_mo({"甲": "A"})
        assert "plural-forms" in g.GNUTranslations(io.BytesIO(mo)).info()

    def test_a_supplied_header_is_not_overwritten(self):
        """**别拿写死的头盖掉 `.po` 自带的** —— 各语言规则不一样。"""
        assert "nplurals=3" in _trans().info()["plural-forms"]


class TestTheRuntimeExposesIt:

    def test_ngettext_is_public(self):
        from astro_smb import i18n

        assert callable(i18n.ngettext)
        # 中文下退化成原样返回(源语言不装 .mo)
        i18n.set_language("zh_CN")
        assert i18n.ngettext("{n} 帧", "{n} 帧", 5) == "{n} 帧"
