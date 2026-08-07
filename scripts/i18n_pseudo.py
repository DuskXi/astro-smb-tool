"""生成**伪语言**词表(`xx_PS`),用来验 i18n 而不用先写出一份真翻译。

伪语言 = 每条 msgid 机械加壳:``你好`` → ``⟦你好⟧``。占位符 `{name}`、
`{0:.2f}` **原样保留**(动了就 `.format()` 直接崩,那不是我们要验的东西)。

跑起来之后屏幕上只会有两种字:

* **加了壳的** —— 走了 `_()`,对的;
* **没加壳的** —— **漏包了**。这是伪语言唯一的用途,而它比人眼扫源码可靠得多:
  源码里 500 处包没包,人是看不过来的;界面上哪块字没壳,一眼就看见。

外加一件事:**行为**在伪语言下必须一模一样。分区少了一格、徽章空了、
treemap 换了颜色、某个筛选筛不掉了 —— 那都是"拿显示文本当身份"的现场。
这个仓库已经因此栽过三次(传输页「排队」分区永远空、丢星那段不标警告、
天球上的点悄悄退回 goto 请求值)。

用法::

    uv run python scripts/i18n_pseudo.py                 # 生成 xx_PS
    ASTRO_SMB_LANG=xx_PS uv run --with pyside6 python -m astro_smb_qt

**别把它发给用户。** `available_languages()` 会列出它,但它不是给人读的。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POT = ROOT / "astro_smb" / "locale" / "astro_smb.pot"
LANG = "xx_PS"

#: 占位符:`{}`、`{0}`、`{name}`、`{name:.2f}`、`{x!r}` 全算
_PLACEHOLDER = re.compile(r"\{[^{}]*\}")

OPEN, CLOSE = "⟦", "⟧"        # ⟦ ⟧ —— BMP 内,不会踩 win32more 那个坑


def pseudo(msgid: str) -> str:
    """加壳。**占位符原样不动。**

    只在两端加,不逐字替换字形:替换字形(a→á)会让中文原文变得没法读,
    而我们要的是"这块字有没有经过 `_()`",不是"这块字长得像不像外文"。
    """
    return f"{OPEN}{msgid}{CLOSE}"


def main() -> int:
    sys.path.insert(0, str(ROOT))
    import scripts.i18n_build as b

    if not POT.is_file():
        print(f"没有 {POT} —— 先跑 scripts/i18n_extract.py")
        return 1
    ids = [k for k in b.parse_po(POT.read_text(encoding="utf-8")) if k]
    pairs = {m: pseudo(m) for m in ids}

    # 自检:加壳前后占位符必须**逐个相同**。这条不是客套 —— 加壳逻辑一旦
    # 碰了占位符,界面上不是显示错而是**直接抛 KeyError/IndexError**,
    # 而那时人会以为是 i18n 本身有问题。
    for src, dst in pairs.items():
        assert _PLACEHOLDER.findall(src) == _PLACEHOLDER.findall(dst), src

    out = ROOT / "astro_smb" / "locale" / LANG / "LC_MESSAGES"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{b.DOMAIN if hasattr(b, 'DOMAIN') else 'astro_smb'}.mo").write_bytes(
        b.build_mo(pairs))
    print(f"{out}\n{len(pairs)} 条伪翻译\n\n"
          f"跑:  ASTRO_SMB_LANG={LANG} uv run --with pyside6 python -m astro_smb_qt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
