"""为一种语言建 / 刷新 `.po`(从 `.pot` 合并,保留已有译文)。

    uv run python scripts/i18n_init_lang.py en ja_JP ru_RU

**为什么不直接拿 `.pot` 当 `.po` 用**:代码在动,msgid 会增删。每次重来一遍
等于把已有译文全丢掉;而只追加不删,又会留下一堆早就不存在的条目让译者白翻。
所以要合并:

* 新 msgid → 空的 `msgstr ""`,排在**文件最前面**(译者一打开就看见要干的活);
* 已有译文 → 原样保留;
* 已消失的 msgid → 挪到文件末尾并注释掉(`#~`),不直接删 ——
  代码回滚或改回原文时还能捡回来。

复数:`.pot` 里的复数条目(键是 `单 + NUL + 复`)会按目标语言的 `nplurals`
展开成对应数量的 `msgstr[N]`。语言的复数规则见 `PLURAL_RULES`。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import i18n_build as B          # noqa: E402

POT = ROOT / "astro_smb" / "locale" / f"{B.DOMAIN}.pot"

#: 语言 → 复数规则。**不是猜的**,取自 GNU gettext 手册的语言列表。
#: 没列到的语言用 `DEFAULT_PLURAL`(两式),那对多数印欧语系是对的,
#: 但对斯拉夫/闪含语系是错的 —— 加语言时请查一下再往这里补。
PLURAL_RULES = {
    "en": "nplurals=2; plural=(n != 1);",
    "de": "nplurals=2; plural=(n != 1);",
    "fr": "nplurals=2; plural=(n > 1);",
    "es": "nplurals=2; plural=(n != 1);",
    "it": "nplurals=2; plural=(n != 1);",
    "pt": "nplurals=2; plural=(n != 1);",
    "ja": "nplurals=1; plural=0;",
    "ko": "nplurals=1; plural=0;",
    "zh": "nplurals=1; plural=0;",
    "ru": ("nplurals=3; plural=(n%10==1 && n%100!=11 ? 0 : "
           "n%10>=2 && n%10<=4 && (n%100<10 || n%100>=20) ? 1 : 2);"),
    "pl": ("nplurals=3; plural=(n==1 ? 0 : n%10>=2 && n%10<=4 && "
           "(n%100<10 || n%100>=20) ? 1 : 2);"),
    "ar": ("nplurals=6; plural=(n==0 ? 0 : n==1 ? 1 : n==2 ? 2 : "
           "n%100>=3 && n%100<=10 ? 3 : n%100>=11 ? 4 : 5);"),
}


def plural_rule(lang: str) -> str:
    return PLURAL_RULES.get(lang.split("_")[0].lower(), B.DEFAULT_PLURAL)


def _q(s: str) -> str:
    return '"' + (s.replace("\\", "\\\\").replace('"', '\\"')
                  .replace("\n", "\\n").replace("\t", "\\t")) + '"'


def _refs(pot_text: str) -> dict[str, str]:
    """msgid → `#:` 出处行。**出处对译者很值钱** —— 同一个「完成」在按钮上
    和在状态行里,译法可能不一样。"""
    out: dict[str, str] = {}
    ref = ""
    for line in pot_text.splitlines():
        if line.startswith("#: "):
            ref = line
        elif line.startswith("msgid ") and ref:
            key = B._unquote(line[6:])
            out[key] = ref
            ref = ""
    return out


def render(lang: str, want: dict[str, str], have: dict[str, str],
           refs: dict[str, str]) -> str:
    rule = plural_rule(lang)
    n = int(re.search(r"nplurals\s*=\s*(\d+)", rule).group(1))
    head = (f'# {lang} translation for astro-smb-tool.\n'
            f'#\n'
            f'# **翻之前先读 docs/i18n-glossary.md** —— 这个项目的文案一多半是\n'
            f'# 天文判读结论,不是界面装饰;翻错不是读着别扭,是给出错误的结论。\n'
            f'# 翻完跑 `uv run python scripts/i18n_check_po.py {lang}`。\n'
            f'msgid ""\n'
            f'msgstr "Content-Type: text/plain; charset=UTF-8\\n"\n'
            f'"Plural-Forms: {rule}\\n"\n')

    fresh = [k for k in sorted(want) if k and k not in have]
    kept = [k for k in sorted(want) if k and k in have]
    gone = [k for k in sorted(have) if k and k not in want]

    def block(key: str, value: str) -> str:
        ref = refs.get(key.split(B.NUL)[0], "")
        out = (ref + "\n") if ref else ""
        if B.NUL in key:
            one, many = key.split(B.NUL, 1)
            forms = value.split(B.NUL) if value else []
            out += f"msgid {_q(one)}\nmsgid_plural {_q(many)}\n"
            for i in range(n):
                out += f"msgstr[{i}] {_q(forms[i] if i < len(forms) else '')}\n"
        else:
            out += f"msgid {_q(key)}\nmsgstr {_q(value)}\n"
        return out

    parts = [head]
    if fresh:
        parts.append(f"\n# ==== 待翻译({len(fresh)} 条)"
                     f" —— 新加的排在最前面 ====\n")
        parts += ["\n" + block(k, "") for k in fresh]
    if kept:
        parts.append(f"\n# ==== 已翻译({len(kept)} 条)====\n")
        parts += ["\n" + block(k, have[k]) for k in kept]
    if gone:
        parts.append(f"\n# ==== 代码里已经没有了({len(gone)} 条)。"
                     f"不直接删 —— 改回原文时还能捡回来 ====\n")
        for k in gone:
            for line in block(k, have[k]).splitlines():
                parts.append("\n#~ " + line if line else "")
        parts.append("\n")
    return "".join(parts)


def main() -> int:
    langs = sys.argv[1:]
    if not langs:
        print(__doc__.strip().splitlines()[2].strip())
        return 1
    if not POT.is_file():
        print(f"没有 {POT} —— 先跑 scripts/i18n_extract.py")
        return 1
    pot_text = POT.read_text(encoding="utf-8")
    want = {k: v for k, v in B.parse_po(pot_text).items() if k}
    refs = _refs(pot_text)

    for lang in langs:
        po = POT.parent / lang / "LC_MESSAGES" / f"{B.DOMAIN}.po"
        have = {k: v for k, v in B.parse_po(
            po.read_text(encoding="utf-8")).items() if k and v} if po.is_file() else {}
        po.parent.mkdir(parents=True, exist_ok=True)
        po.write_text(render(lang, want, have, refs), encoding="utf-8",
                      newline="\n")
        fresh = len([k for k in want if k not in have])
        print(f"{lang:8} 共 {len(want)} 条,已翻 {len(have)},"
              f"待翻 {fresh}  → {po.relative_to(ROOT)}")
        if lang.split('_')[0].lower() not in PLURAL_RULES:
            print(f"         ⚠ {lang} 不在 PLURAL_RULES 里,用了两式默认规则 ——"
                  f" 斯拉夫/闪含语系请补一条真规则再翻")
    return 0


if __name__ == "__main__":
    sys.exit(main())
