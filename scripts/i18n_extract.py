"""从源码抽出所有 msgid,生成 `.pot` 模板(纯标准库,不装 babel/xgettext)。

抽的是这几种调用:``_("…")``、``gettext("…")``、``N_("…")``(只标记不翻的
查表常量)、``ngettext("单", "复", n)``。**参数必须是字面量** —— 变量拼出来的
字符串抽取器看不见,那种地方本来就不该直接进词表(见 `i18n_wrap.py` 的说明)。

用法::

    uv run python scripts/i18n_extract.py                 # 写 locale/astro_smb.pot
    uv run python scripts/i18n_extract.py --check         # 只报告差异,不写

`--check` 用来回答一个具体问题:**新加的界面文案有没有漏进词表。** 加了一句
`_("…")` 却没重新抽,译者那边永远看不到它,而界面在英文下会退回中文 ——
不报错,只是混着两种语言。
"""
from __future__ import annotations

import argparse
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "astro_smb" / "locale" / "astro_smb.pot"

#: 抽哪些包。前端也抽 —— 用户可见文案不分层。
PACKAGES = ("astro_smb", "astro_smb_app", "astro_smb_qt", "astro_smb_gui")

#: 单参数的翻译/标记函数
SINGLE = {"_", "gettext", "N_", "pgettext"}

HEADER = '''# 翻译模板(由 scripts/i18n_extract.py 生成,不要手改)。
#
# **翻译天文判读文案的人得懂天文。** 几条容易翻错的:
#   大气质量 = airmass(不是 air quality)
#   西垂/东垂 = pier side(赤道仪在中天哪一侧,不是"往西挂")
#   欠采样/过采样 = under/oversampled(像元比例 vs 视宁)
#   恰定 = exactly determined(方程数 = 未知数,残差恒为零)
#
# 占位符 `{name}` 不许翻也不许改名 —— 它们是 `.format()` 的关键字。
msgid ""
msgstr ""
"Content-Type: text/plain; charset=UTF-8\\n"
'''


def _lit(node) -> str | None:
    return (node.value if isinstance(node, ast.Constant)
            and isinstance(node.value, str) else None)


#: 解析失败的文件。抽完要检查,不能当没发生。
BROKEN: list[Path] = []


def collect(paths) -> dict[str, list[str]]:
    """msgid → 出现位置列表(`文件:行`)。复数条目的键是 ``单\\x00复``。"""
    found: dict[str, list[str]] = {}
    for p in paths:
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError) as e:
            # **不许静默跳过。** 一个文件解析不了 = 它的 msgid 全部消失,
            # 而抽取器照样"成功"输出一份少了几百条的词表。这次就是这么发生的:
            # 六个老 UI 文件被插错的 import 弄坏,词表从 1845 掉到 1618,
            # 全靠人盯着那个数才发现。
            print(f"!! 解析不了 {p.relative_to(ROOT).as_posix()}: {e}",
                  file=sys.stderr)
            BROKEN.append(p)
            continue
        rel = p.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = (fn.id if isinstance(fn, ast.Name) else
                    fn.attr if isinstance(fn, ast.Attribute) else "")
            if name in SINGLE and node.args:
                s = _lit(node.args[0])
                if s:
                    found.setdefault(s, []).append(f"{rel}:{node.lineno}")
            elif name == "ngettext" and len(node.args) >= 2:
                a, b = _lit(node.args[0]), _lit(node.args[1])
                if a and b:
                    found.setdefault(f"{a}\x00{b}", []).append(
                        f"{rel}:{node.lineno}")
    return found


def _esc(s: str) -> str:
    return (s.replace("\\", "\\\\").replace('"', '\\"')
            .replace("\n", "\\n").replace("\t", "\\t"))


def render(found: dict[str, list[str]]) -> str:
    out = [HEADER]
    for msgid in sorted(found):
        where = found[msgid]
        # 出处最多列三处 —— 全列的话热门短语能占几十行,`.po` 变得没法读
        out.append("\n#: " + " ".join(where[:3])
                   + (f"  (+{len(where) - 3})" if len(where) > 3 else ""))
        if "\x00" in msgid:
            one, many = msgid.split("\x00", 1)
            out.append(f'msgid "{_esc(one)}"')
            out.append(f'msgid_plural "{_esc(many)}"')
            out.append('msgstr[0] ""')
            out.append('msgstr[1] ""')
        else:
            out.append(f'msgid "{_esc(msgid)}"')
            out.append('msgstr ""')
    return "\n".join(out) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="只比对,不写文件;有差异时退出码 1")
    a = ap.parse_args()

    files: list[Path] = []
    for pkg in PACKAGES:
        files.extend(sorted((ROOT / pkg).rglob("*.py")))
    found = collect(files)

    # ---- XAML:属性值上包不了 `_()`,所以单独抽。**判据与运行时同一份**
    # (`astro_smb_gui._xamli18n.msgids`)—— 各写一套迟早漂,而漂了的结果是
    # 词表里有的界面上没有、界面上有的词表里没有,两边都不报错。
    sys.path.insert(0, str(ROOT))
    from astro_smb_gui._xamli18n import msgids as _xaml_msgids

    for x in sorted((ROOT / "astro_smb_gui").rglob("*.xaml")):
        rel = x.relative_to(ROOT).as_posix()
        for mid in _xaml_msgids(x.read_text(encoding="utf-8")):
            found.setdefault(mid, []).append(rel)
    text = render(found)

    if a.check:
        old = OUT.read_text(encoding="utf-8") if OUT.is_file() else ""
        if old == text:
            print(f"词表是新的({len(found)} 条)")
            return 0
        oldids = set(collect_ids(old))
        newids = set(found)
        for m in sorted(newids - oldids)[:20]:
            print("  + " + m.replace("\x00", " / ")[:70])
        for m in sorted(oldids - newids)[:20]:
            print("  - " + m.replace("\x00", " / ")[:70])
        print(f"\n词表过期:现在 {len(newids)} 条,模板里 {len(oldids)} 条。"
              f"跑 `uv run python scripts/i18n_extract.py` 重新生成。")
        return 1

    if BROKEN:
        print(f"\n{len(BROKEN)} 个文件解析不了,词表**不完整** —— 先修语法再抽。",
              file=sys.stderr)
        return 2
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8", newline="\n")
    print(f"{OUT}\n{len(found)} 条")
    return 0


def collect_ids(po_text: str) -> list[str]:
    """从已有 `.pot`/`.po` 里读回 msgid(只为 `--check` 报差异用)。

    复用 `i18n_build.parse_po`,免得同一套语法写两个解析器。它不认
    `msgid_plural`,所以复数条目在这里只会显示单数那一半 —— 对"哪条漏了"
    这个用途够了,别拿它当完整解析。
    """
    import scripts.i18n_build as b

    return [k for k in b.parse_po(po_text) if k]


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))
    sys.exit(main())
