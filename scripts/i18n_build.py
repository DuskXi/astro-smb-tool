"""把 `.po` 编成 `.mo`(纯标准库,不依赖 gettext 工具链)。

    uv run python scripts/i18n_build.py            # 编全部
    uv run python scripts/i18n_build.py en         # 只编一个

**为什么要自己写。** GNU 的 `msgfmt` 不在标准库里,CPython 只在源码树的
`Tools/i18n/` 放了一份,而 uv 的独立构建**不带 Tools**。要么让每个开发者
装一套 gettext 工具链(mac/Windows 上都不是默认有的),要么这五十行自己写。

格式很简单(GNU MO):幻数 + 两张 (长度, 偏移) 表 + 两段字符串区。
**唯一容易漏的是那条空 msgid 的元数据头** —— 少了它 gettext 按 ASCII 解码,
遇到中文 msgid 直接 `UnicodeDecodeError`。写这个脚本时就是这么栽的一次。
"""
from __future__ import annotations

import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LOCALE_DIR = ROOT / "astro_smb" / "locale"
DOMAIN = "astro_smb"

#: 没有自带头时用这个。**`Plural-Forms` 不能省** —— 少了它,gettext 会退回
#: 一条默认的日耳曼语规则(`n != 1`)。英语碰巧对,所以本地怎么试都正常;
#: 俄语(三式)、阿拉伯语(六式)、日语(一式)全都会静默取错形式。
DEFAULT_PLURAL = "nplurals=2; plural=(n != 1);"

#: `.mo` 里复数条目的分隔符。键是「单数 + 这个 + 复数」,值是各个形式
#: 用它接起来 —— 那是 GNU MO 格式本身的约定,`scripts/i18n_extract.py`
#: 生成 `.pot` 时用的也是它,一份规矩。
NUL = chr(0)

#: 每份 .mo 都要有的元数据头(msgid 为空的那一条)
HEADER = ("Project-Id-Version: astro-smb-tool\\n"
          "MIME-Version: 1.0\\n"
          "Content-Type: text/plain; charset=UTF-8\\n"
          "Content-Transfer-Encoding: 8bit\\n"
          f"Plural-Forms: {DEFAULT_PLURAL}\\n").replace("\\n", "\n")


def _unquote(s: str) -> str:
    """`"…"` → 原文。只处理 po 里真正会出现的转义。"""
    s = s.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1]
    return (s.replace('\\"', '"').replace("\\n", "\n")
             .replace("\\t", "\t").replace("\\\\", "\\"))


def parse_po(text: str) -> dict[str, str]:
    """极简 .po 解析。返回 ``{msgid: msgstr}``。

    **复数条目的键是 ``单\x00复``、值是 ``译0\x00译1…``** —— 那正是 `.mo`
    二进制格式本身的约定,所以 `build_mo` 不用为复数做任何特殊处理。
    (`scripts/i18n_extract.py` 生成 `.pot` 时用的也是这个约定,一份规矩。)

    原来这里**读不了复数条目**:`msgid_plural` 与 `msgstr[N]` 都当成未知行
    跳过,于是那条目整个丢失 —— 不报错,只是 `ngettext` 拿不到翻译、
    永远退回原文。

    **`#,fuzzy` 的条目要跳过** —— 那是"机器猜的,没人确认过"。
    把它们编进去等于把半成品翻译发给用户,而且不会有任何提示。
    """
    out: dict[str, str] = {}
    buf_id: list[str] = []
    buf_plural: list[str] = []
    buf_str: list[list[str]] = []
    mode: str | None = None
    idx = 0
    fuzzy = False
    pending_fuzzy = False

    def flush() -> None:
        nonlocal buf_id, buf_plural, buf_str, mode, fuzzy, idx
        if mode is not None and not fuzzy:
            key = "".join(buf_id)
            if buf_plural:
                key += NUL + "".join(buf_plural)
                val = NUL.join("".join(parts) for parts in buf_str)
            else:
                val = "".join(buf_str[0]) if buf_str else ""
            out[key] = val
        buf_id, buf_plural, buf_str = [], [], []
        mode, fuzzy, idx = None, False, 0

    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#,") and "fuzzy" in line:
            pending_fuzzy = True
            continue
        if line.startswith("#") or not line:
            continue
        if line.startswith("msgid_plural "):
            mode = "plural"
            buf_plural = [_unquote(line[13:])]
        elif line.startswith("msgid "):
            flush()
            fuzzy = pending_fuzzy
            pending_fuzzy = False
            mode = "id"
            buf_id = [_unquote(line[6:])]
        elif line.startswith("msgstr["):
            close = line.index("]")
            idx = int(line[7:close])
            while len(buf_str) <= idx:
                buf_str.append([])
            mode = "str"
            buf_str[idx] = [_unquote(line[close + 2:])]
        elif line.startswith("msgstr "):
            mode = "str"
            idx = 0
            buf_str = [[_unquote(line[7:])]]
        elif line.startswith('"'):
            if mode == "id":
                buf_id.append(_unquote(line))
            elif mode == "plural":
                buf_plural.append(_unquote(line))
            elif mode == "str":
                while len(buf_str) <= idx:
                    buf_str.append([])
                buf_str[idx].append(_unquote(line))
    flush()
    return out


def build_mo(pairs: dict[str, str]) -> bytes:
    entries = dict(pairs)
    # **覆盖率写进头。** 运行时要能说出「这个语言只翻了 4%」——
    # 不说的话,用户切到英文看见满屏中文,只会以为切换坏了。
    # 写在头里而不是运行时数:`.mo` 里根本没有未翻译的条目,
    # 运行时数不出分母。
    total = len([k for k in pairs if k])
    done = len([k for k, v in pairs.items() if k and v])
    # **头优先用 `.po` 自带的那一份** —— 各语言的 `Plural-Forms` 不一样,
    # 拿一份写死的头盖掉它,俄语/阿拉伯语的复数就全取错形式,而且不报错。
    if not entries.get(""):
        entries[""] = HEADER                  # 元数据头,少了它按 ASCII 解码
    elif "Plural-Forms" not in entries[""]:
        entries[""] = (entries[""].rstrip("\n")
                       + f"\nPlural-Forms: {DEFAULT_PLURAL}\n")
    entries[""] = (entries[""].rstrip("\n")
                   + f"\nX-Translated: {done}\nX-Total: {total}\n")
    # 空翻译不进 .mo —— gettext 会把它当"翻译成空串",界面上直接变成一片空白
    entries = {k: v for k, v in entries.items() if v or k == ""}
    keys = sorted(entries)

    ids = b"\x00".join(k.encode("utf-8") for k in keys) + b"\x00"
    strs = b"\x00".join(entries[k].encode("utf-8") for k in keys) + b"\x00"

    koff, o = [], 0
    for k in keys:
        b = k.encode("utf-8")
        koff.append((len(b), o))
        o += len(b) + 1
    voff, o = [], 0
    for k in keys:
        b = entries[k].encode("utf-8")
        voff.append((len(b), o))
        o += len(b) + 1

    n = len(keys)
    kt = 7 * 4
    vt = kt + n * 8
    ids_at = vt + n * 8
    strs_at = ids_at + len(ids)

    out = struct.pack("<Iiiiiii", 0x950412DE, 0, n, kt, vt, 0, 0)
    for ln, off in koff:
        out += struct.pack("<ii", ln, ids_at + off)
    for ln, off in voff:
        out += struct.pack("<ii", ln, strs_at + off)
    return out + ids + strs


def main(argv: list[str] | None = None) -> int:
    want = (argv or sys.argv[1:])
    if not LOCALE_DIR.is_dir():
        print(f"没有 {LOCALE_DIR}")
        return 1
    built = 0
    for po in sorted(LOCALE_DIR.rglob(f"{DOMAIN}.po")):
        lang = po.parent.parent.name
        if want and lang not in want:
            continue
        pairs = parse_po(po.read_text(encoding="utf-8"))
        mo = po.with_suffix(".mo")
        mo.write_bytes(build_mo(pairs))
        real = len([k for k in pairs if k])
        print(f"{lang:8} {real:4} 条  → {mo.relative_to(ROOT)}")
        built += 1
    if not built:
        print("没有找到 .po")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
