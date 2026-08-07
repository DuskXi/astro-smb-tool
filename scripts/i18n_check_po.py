"""校验一份 `.po`:占位符、空翻译、fuzzy、复数形式数量。

**这是给机器翻译兜底用的。** 一次批量翻译几千条,人不可能逐条看;而这几类
错误的共同点是**运行时才炸,或者根本不炸只是显示错**:

* 占位符被翻掉(`{name}` → `{名字}`)→ 运行时 `KeyError`,而且只在走到
  那一行时才炸;
* 格式说明符被改(`{x:.2f}` → `{x}`)→ 不炸,数字精度悄悄变了;
* 前导/尾随空格丢了 → 拼接出来的句子粘在一起;
* 复数形式数量与头里的 `nplurals` 对不上 → gettext 取到空串,界面一片空白;
* `#,fuzzy` 留着 → 那是"机器猜的没人确认过",编进去等于把半成品发给用户。

用法::

    uv run python scripts/i18n_check_po.py            # 查全部语言
    uv run python scripts/i18n_check_po.py en ru      # 只查这几个
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import i18n_build as B          # noqa: E402

LOCALE_DIR = ROOT / "astro_smb" / "locale"

#: `{name}` / `{0}` / `{x:.2f}` / `{y!r}` —— 花括号那一套
_BRACE = re.compile(r"\{[^{}]*\}")
#: `%d` / `%s` / `%.1f` —— 老式那一套(`wcsapps` 的盲解进度在用)
_PERCENT = re.compile(r"%[-+ #0-9.]*[difgeoxXsr%]")


def placeholders(s: str) -> tuple[list[str], list[str]]:
    """一条消息里的占位符。花括号与百分号**分开数** —— 混在一起比会误报。"""
    return _BRACE.findall(s), [m for m in _PERCENT.findall(s) if m != "%%"]


def check_pairs(pairs: dict[str, str], *, nplurals: int,
                require_complete: bool = False) -> tuple[list[str], int, int]:
    """返回 ``(问题清单, 已翻条数, 总条数)``。

    **"还没翻"不是错误。** 一份进行中的词表里绝大多数条目是空的,把它们
    全报成错误就等于这个工具在 100% 翻完之前完全不能用 —— 那样它就永远
    不会被跑。空条目只计入覆盖率;发布前要卡完整度就加 `--require-complete`。
    """
    bad: list[str] = []
    done = total = 0
    for msgid, msgstr in pairs.items():
        if not msgid:
            continue
        total += 1
        singular = msgid.split(B.NUL)[0]
        forms = msgstr.split(B.NUL) if B.NUL in msgid else [msgstr]
        if not any(f.strip() for f in forms):
            if require_complete:
                bad.append(f"还没翻: {singular[:52]!r}")
            continue                      # 还没翻 —— 正常状态,不是错误
        done += 1

        if B.NUL in msgid and len(forms) != nplurals:
            bad.append(f"复数形式 {len(forms)} 个,头里写的是 {nplurals}: "
                       f"{singular[:40]!r}")

        want_b, want_p = placeholders(singular)
        for i, form in enumerate(forms):
            if not form.strip():
                # 到这里说明**部分**形式空着(复数只翻了一半)—— 那是真错误:
                # gettext 会给出空串,界面上直接一片空白
                bad.append(f"复数形式[{i}]空着(其余已翻): {singular[:40]!r}")
                continue
            got_b, got_p = placeholders(form)
            # **按多重集比,不按集合** —— 同一个占位符出现两次也要两次
            if sorted(got_b) != sorted(want_b):
                bad.append(f"占位符对不上[{i}]: {singular[:34]!r}\n"
                           f"      原文 {want_b}  译文 {got_b}")
            if sorted(got_p) != sorted(want_p):
                bad.append(f"%-占位符对不上[{i}]: {singular[:34]!r}\n"
                           f"      原文 {want_p}  译文 {got_p}")
            # 拼接片段的两端空格 —— 丢了会把句子粘在一起
            for side, fn in (("前导", str.startswith), ("尾随", str.endswith)):
                if fn(singular, " ") and not fn(form, " "):
                    bad.append(f"{side}空格丢了[{i}]: {singular[:44]!r}")
    return bad, done, total


def _nplurals(header: str) -> int:
    m = re.search(r"nplurals\s*=\s*(\d+)", header or "")
    return int(m.group(1)) if m else 2


def check_file(po: Path, *, require_complete: bool = False):
    text = po.read_text(encoding="utf-8")
    problems: list[str] = []
    # `parse_po` **会跳过 fuzzy**,所以单独扫一遍原文才看得见它们
    n_fuzzy = sum(1 for ln in text.splitlines()
                  if ln.strip().startswith("#,") and "fuzzy" in ln)
    if n_fuzzy:
        problems.append(f"还有 {n_fuzzy} 条 `#,fuzzy` —— 那是没人确认过的,"
                        f"不会被编进 .mo(也就是界面上退回中文)")
    pairs = B.parse_po(text)
    more, done, total = check_pairs(
        pairs, nplurals=_nplurals(pairs.get("", "")),
        require_complete=require_complete)
    return problems + more, done, total


def main() -> int:
    args = sys.argv[1:]
    strict = "--require-complete" in args
    want = [a for a in args if not a.startswith("-")]
    files = sorted(LOCALE_DIR.rglob(f"{B.DOMAIN}.po"))
    if not files:
        print(f"{LOCALE_DIR} 底下没有 .po")
        return 1
    rc = 0
    for po in files:
        lang = po.parent.parent.name
        if want and lang not in want:
            continue
        problems, done, total = check_file(po, require_complete=strict)
        pct = (done / total * 100.0) if total else 0.0
        head = f"{lang:8} {done:5}/{total} 条已翻({pct:5.1f}%)"
        if problems:
            rc = 1
            print(f"\n{head} —— {len(problems)} 个问题:")
            for p in problems[:40]:
                print(f"  ✗ {p}")
            if len(problems) > 40:
                print(f"  … 另外 {len(problems) - 40} 个")
        else:
            print(f"{head}  已翻的部分没问题")
    return rc


if __name__ == "__main__":
    sys.exit(main())
