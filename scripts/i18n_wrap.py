"""把中文字符串字面量包进 ``_()``,并把 f-string 改写成 ``_("…").format(…)``。

**这个脚本最重要的部分不是它包了什么,是它拒绝包什么。**

有三类地方包进去就是**新造一个 bug**,脚本一律跳过并单独列出来给人看:

1. **模块级常量** —— ``X = "中文"`` 在 import 时求值一次。包上 ``_()`` 之后
   翻译就冻在"import 那一刻的语言"上,之后 `set_language()` 再也改不动它。
2. **比较操作数** —— ``if x == "中文"``、``"中文" in y``。这正是本仓库那类
   系统性缺陷(拿显示文本当身份)的现场;包上去等于把它焊死。
3. **字典键 / 下标** —— ``{"排队": …}``、``d["中文"]``。包了就查不到了。

文档字符串也跳过(那是给开发者看的,不是界面文案)。

用法::

    uv run python scripts/i18n_wrap.py astro_smb_app/views      # 只报告
    uv run python scripts/i18n_wrap.py astro_smb_app/views -w   # 真改

改完**必须**跑一遍全量测试,以及 ``scripts/i18n_extract.py`` 重新抽 msgid。
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

CJK = re.compile(r"[一-鿿　-〿＀-￯]")

#: 已经是 `_()` 或 `ngettext()` 的调用,别套第二层
_WRAPPERS = {"_", "gettext", "ngettext", "pgettext", "N_"}


def has_cjk(s: str) -> bool:
    return bool(CJK.search(s))


#: 一眼看去像代码的特征。**样式表/脚本不是文案** —— 它们里面有中文注释,
#: 所以会被"含中文"这条判据勾住;包进去之后词表里多出一条几千字的"消息",
#: 译者改一个字整个界面就坏(Qt 的 QSS 就这么被包过一次)。
_CODE_HINTS = (
    "QWidget", "QPushButton", "border-radius", "font-family",    # QSS
    "function(", "function (", "window.", "document.",           # JS
    "Get-CimInstance", "Write-Output",                           # PowerShell
    "SELECT ", "CREATE TABLE",                                   # SQL
)


def _joined_text(node) -> str:
    """f-string 的字面片段拼起来(只为判"像不像代码")。"""
    if not isinstance(node, ast.JoinedStr):
        return ""
    return "".join(v.value for v in node.values
                   if isinstance(v, ast.Constant) and isinstance(v.value, str))


def _looks_like_code(text: object) -> bool:
    if not isinstance(text, str):
        return False
    if len(text) > 2000:            # 再长的"文案"也不会有两千字
        return True
    return sum(h in text for h in _CODE_HINTS) >= 2


class Skip:
    """跳过的原因 —— 这几个字符串会直接进报告,措辞就是给人看的。"""

    MODULE_CONST = "模块级常量(翻译会冻在 import 时的语言上)"
    DEFAULT_ARG = "函数默认值(def 执行时求值 = import 时,翻译同样会冻住)"
    COMPARE = "比较操作数(拿显示文本当身份 —— 这是要修的,不是要包的)"
    KEY = "字典键 / 下标(包了就查不到)"
    DOCSTRING = "文档字符串"
    ALREADY = "已经在 _() 里"
    ANNOTATION = "类型标注"
    CODEY = "看着像代码不像文案(QSS/JS/SQL/脚本)"


class Finder(ast.NodeVisitor):
    """收集要改的位置,以及**故意不改**的位置。"""

    def __init__(self, tree: ast.AST):
        self.hits: list[tuple[ast.AST, str]] = []      # (节点, 新源码)
        self.skips: list[tuple[int, str, str]] = []    # (行号, 原文, 原因)
        self._parent: dict[int, ast.AST] = {}
        self._field: dict[int, str] = {}
        for node in ast.walk(tree):
            for field, value in ast.iter_fields(node):
                for child in (value if isinstance(value, list) else [value]):
                    if isinstance(child, ast.AST):
                        self._parent[id(child)] = node
                        self._field[id(child)] = field
        self._docstrings = {id(d) for d in _docstring_nodes(tree)}
        #: 落在 f-string **内部表达式**里的节点 —— 包起来时要换单引号,
        #: 免得 `f"…{_(" x ")}…"` 这种同引号套三层(3.12+ 合法但极易改坏)
        self._in_fstring: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                for sub in ast.walk(node):
                    self._in_fstring.add(id(sub))
        self._tree = tree

    # -- 判断 ------------------------------------------------------------

    def _reason_to_skip(self, node: ast.AST) -> str | None:
        if id(node) in self._docstrings:
            return Skip.DOCSTRING
        parent = self._parent.get(id(node))
        field = self._field.get(id(node))
        if parent is None:
            return Skip.DOCSTRING
        if isinstance(parent, ast.Compare):
            return Skip.COMPARE
        if isinstance(parent, ast.Dict) and field == "keys":
            return Skip.KEY
        if isinstance(parent, ast.Subscript) and field == "slice":
            return Skip.KEY
        if isinstance(parent, (ast.AnnAssign,)) and field == "annotation":
            return Skip.ANNOTATION
        if isinstance(parent, ast.Call):
            fn = parent.func
            name = (fn.id if isinstance(fn, ast.Name) else
                    fn.attr if isinstance(fn, ast.Attribute) else "")
            if name in _WRAPPERS:
                return Skip.ALREADY
        if _looks_like_code(node.value if isinstance(node, ast.Constant)
                            else _joined_text(node)):
            return Skip.CODEY
        if self._in_default_arg(node):
            return Skip.DEFAULT_ARG
        # 模块级:一路往上看有没有函数/类把它兜住
        if self._at_module_level(node):
            return Skip.MODULE_CONST
        return None

    def _in_default_arg(self, node: ast.AST) -> bool:
        """在**函数默认值**里吗?

        `def f(t=_("确定"))` 的默认值是 `def` 执行时求值的 —— 也就是 import 时,
        和模块级常量一模一样会把翻译冻住。第一版没查这个:`_at_module_level`
        一路往上走,碰到 `FunctionDef` 就返回 False,于是默认值被当成"在函数
        体里"放行了。真机上 6 处(两套前端的确认框按钮、忙态文案)。
        """
        cur = node
        while cur is not None:
            parent = self._parent.get(id(cur))
            if isinstance(parent, ast.arguments) and self._field.get(
                    id(cur)) in ("defaults", "kw_defaults"):
                return True
            cur = parent
        return False

    def _at_module_level(self, node: ast.AST) -> bool:
        cur = self._parent.get(id(node))
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef,
                                ast.Lambda)):
                return False
            if isinstance(cur, ast.ClassDef):
                return True          # 类体也是 import 时求值
            cur = self._parent.get(id(cur))
        return True

    # -- 遍历 ------------------------------------------------------------

    def visit_Constant(self, node: ast.Constant):
        if not isinstance(node.value, str) or not has_cjk(node.value):
            return
        # f-string 内部的片段由 visit_JoinedStr 统一处理
        if isinstance(self._parent.get(id(node)), ast.JoinedStr):
            return
        why = self._reason_to_skip(node)
        if why:
            self.skips.append((node.lineno, _short(node.value), why))
            return
        single = id(node) in self._in_fstring
        self.hits.append(
            (node, f"_({_quote(node.value, prefer_single=single)})"))

    def visit_JoinedStr(self, node: ast.JoinedStr):
        text = "".join(v.value for v in node.values
                       if isinstance(v, ast.Constant) and isinstance(v.value, str))
        if not has_cjk(text):
            self.generic_visit(node)
            return
        why = self._reason_to_skip(node)
        if why:
            self.skips.append((node.lineno, _short(text), why))
            return
        try:
            self.hits.append((node, _fstring_to_format(node)))
        except _Untranslatable as e:
            self.skips.append((node.lineno, _short(text), f"f-string: {e}"))


class _Untranslatable(Exception):
    pass


def _fstring_to_format(node: ast.JoinedStr) -> str:
    """``f"共 {n} 张"`` → ``_("共 {n} 张").format(n=n)``。

    占位符尽量用**变量名**而不是 ``{0}`` —— 译者看到 ``{n}`` 还能猜出是什么,
    看到 ``{0}`` 只能瞎猜。表达式复杂到没有名字时才退回位置参数。
    """
    msgid: list[str] = []
    kwargs: list[str] = []
    args: list[str] = []
    #: 表达式 → 已经给它起好的占位符名。**同一个表达式重复出现时要复用同名**,
    #: 否则第二次会退成位置参数,出来 ``"{u}…{0}".format(u, u=u)`` 这种
    #: 能跑但没法读的东西(`views/guiding.py` 那句"峰值 RA…/DEC…"就是)。
    used: dict[str, str] = {}
    for part in node.values:
        if isinstance(part, ast.Constant):
            # 字面的 { } 在 .format() 里要转义
            msgid.append(str(part.value).replace("{", "{{").replace("}", "}}"))
            continue
        if not isinstance(part, ast.FormattedValue):
            raise _Untranslatable("看不懂的片段")
        expr = ast.unparse(part.value)
        # 占位符尽量有名字:`job.n_chunks` → `{n_chunks}`,译者还能看懂;
        # `{0}` 对译者等于没有信息。
        cand = expr if expr.isidentifier() else (
            part.value.attr if isinstance(part.value, ast.Attribute) else "")
        if expr in used:                      # 同一个表达式,复用同一个占位符
            name = used[expr]
        elif cand and cand.isidentifier() and cand not in used.values():
            name = cand
        else:
            name = ""
        spec = ""
        if part.format_spec is not None:
            if any(not isinstance(v, ast.Constant)
                   for v in part.format_spec.values):
                raise _Untranslatable("嵌套的格式说明符")
            spec = ":" + "".join(str(v.value) for v in part.format_spec.values)
        conv = {-1: "", 115: "!s", 114: "!r", 97: "!a"}[part.conversion]
        if name:
            if expr not in used:              # 复用时不能再加一遍关键字实参
                used[expr] = name
                kwargs.append(f"{name}={expr}")
            msgid.append("{" + name + conv + spec + "}")
        else:
            msgid.append("{" + str(len(args)) + conv + spec + "}")
            args.append(expr)
    text = "".join(msgid)
    call = ", ".join(args + kwargs)
    return f"_({_quote(text)}).format({call})"


def _quote(s: str, *, prefer_single: bool = False) -> str:
    """尽量出双引号(和仓库里其余字符串一致),不安全时退 `repr()`。

    `prefer_single` 用在**要塞进 f-string 内部**的地方:
    ``f"…{_(" (已缓存)") if cached else ''}"`` 在 3.12+(PEP 701)是合法的、
    也确实能跑,但同一种引号套三层,读的人稍一改就断。出单引号就没这问题。

    **判"安全"要用 `isprintable()`,不能只挡 `\\n`。** 原来漏了 `\\r`:
    `cli.py` 里进度条那句 ``f"\\r  {n} 文件 …"`` 一改写就把**真的回车**
    塞进了双引号里,而 Python 的分词器把裸 `\\r` 当行结束 ——
    整个文件从那一行起 `SyntaxError: unterminated string literal`。
    `isprintable()` 对 `\\n`/`\\r`/`\\t` 和一切控制字符都返回 False,
    正好是要挡的那一类。
    """
    if prefer_single and s.isprintable() and "'" not in s and "\\" not in s:
        return f"'{s}'"
    if s.isprintable() and '"' not in s and "\\" not in s:
        return f'"{s}"'
    return repr(s)


def _short(s: str, n: int = 34) -> str:
    s = s.replace("\n", "\\n")
    return s if len(s) <= n else s[:n] + "…"


def _docstring_nodes(tree: ast.AST) -> list[ast.AST]:
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)) and node.body:
            first = node.body[0]
            if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                out.append(first.value)
    return out


#: 超过这个宽度就在 `.format(` 后面折一行。仓库没有 linter,但注释密度很高,
#: 一行 130 字符的 `.format(...)` 会把旁边那段解释顶到看不见。
WIDTH = 92


def _soften_long_lines(src: str) -> str:
    """把改宽了的行在 ``.format(`` 处折断。

    **安全网是 `ast.dump()` 前后必须一模一样。** 光 `ast.parse` 不够 ——
    如果误折的是三引号字符串**内部**的一行,语法照样合法,只是字符串内容
    被我偷偷改了。比较 AST 转储能抓住这个(Constant 的值会不同)。
    """
    before = ast.dump(ast.parse(src))
    out = []
    for line in src.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        eol = line[len(body):]
        if len(body) <= WIDTH or ".format(" not in body or "_(" not in body:
            out.append(line)
            continue
        cut = body.rfind(".format(") + len(".format(")
        indent = " " * (len(body) - len(body.lstrip())) + "    "
        out.append(body[:cut] + eol + indent + body[cut:].lstrip() + eol)
    new = "".join(out)
    try:
        if ast.dump(ast.parse(new)) == before:
            return new
    except SyntaxError:
        pass
    return src                      # 有一处不对就整份回退,不做局部妥协


IMPORT_LINE = "from astro_smb.i18n import gettext as _\n"


def _ensure_import(src: str) -> str:
    """确保 `_` **这个名字**被导入了。

    原来只查"有没有 `from astro_smb.i18n import`" —— 而一个文件完全可能
    只导了 `N_`(手工标记模块级常量时常这样)。那种情况下这里会直接跳过,
    改写出来的 `_()` 全是 `NameError`,而且**只在真正走到那一行时才炸**
    (`astro_smb_app/transfers.py` 的冲突策略分支就是这么红的)。
    """
    if re.search(r"^from astro_smb\.i18n import .*gettext as _", src, re.M):
        return src
    m = re.search(r"^from astro_smb\.i18n import (.+)$", src, re.M)
    if m:                       # 已经导了别的名字(多半是 N_),把 `_` 补上
        return src[:m.start(1)] + m.group(1) + ", gettext as _" + src[m.end(1):]
    lines = src.splitlines(keepends=True)
    tree = ast.parse(src)
    # 优先跟在**同属核心库**的 import 后面(`from astro_smb.xxx import`),
    # 否则跟在最后一个 import 后面;一个 import 都没有就跟在文档串/future 后面
    last = core = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            end = node.end_lineno or node.lineno
            last = max(last, end)
            mod = getattr(node, "module", "") or ""
            if mod == "astro_smb" or mod.startswith("astro_smb."):
                core = max(core, end)
    last = core or last
    if last == 0:
        for node in tree.body:
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                last = node.end_lineno or node.lineno
    return "".join(lines[:last] + [IMPORT_LINE] + lines[last:])


def rewrite(src: str) -> tuple[str, list[tuple[int, str, str]], int]:
    tree = ast.parse(src)
    f = Finder(tree)
    f.visit(tree)
    if not f.hits:
        return src, f.skips, 0

    lines = src.splitlines(keepends=True)
    offsets = [0]
    for ln in lines:
        offsets.append(offsets[-1] + len(ln))

    def pos(lineno: int, col: int) -> int:
        # col_offset 是**字节**偏移
        line = lines[lineno - 1]
        return offsets[lineno - 1] + len(line.encode()[:col].decode("utf-8", "ignore"))

    spans = sorted(((pos(n.lineno, n.col_offset),
                     pos(n.end_lineno, n.end_col_offset), new)
                    for n, new in f.hits), reverse=True)
    out = src
    for start, end, new in spans:
        out = out[:start] + new + out[end:]
    try:
        ast.parse(out)
    except SyntaxError as e:      # 改写本身出错了,别让报错落在后面的折行上
        raise SystemExit(f"改写产生了语法错误(第 {e.lineno} 行):{e.msg}\n"
                         f"  {(out.splitlines() or [''])[(e.lineno or 1) - 1]}"
                         ) from e
    return _ensure_import(_soften_long_lines(out)), f.skips, len(f.hits)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("targets", nargs="+")
    ap.add_argument("-w", "--write", action="store_true", help="真改文件")
    a = ap.parse_args()

    files: list[Path] = []
    for t in a.targets:
        p = Path(t)
        files.extend(sorted(p.rglob("*.py")) if p.is_dir() else [p])

    total_hits = 0
    all_skips: list[tuple[Path, int, str, str]] = []
    for p in files:
        src = p.read_text(encoding="utf-8")
        new, skips, n = rewrite(src)
        total_hits += n
        all_skips.extend((p, ln, s, why) for ln, s, why in skips)
        if n and a.write:
            ast.parse(new)                      # 先自检语法再落盘
            p.write_text(new, encoding="utf-8", newline="")
        if n:
            print(f"{'改了' if a.write else '待改'} {n:4d}  {p}")

    print(f"\n合计 {total_hits} 处" + ("(已写入)" if a.write else "(未写入,加 -w)"))

    interesting = [x for x in all_skips
                   if x[3] in (Skip.MODULE_CONST, Skip.COMPARE, Skip.KEY,
                               Skip.DEFAULT_ARG)
                   or x[3].startswith("f-string")]
    if interesting:
        print(f"\n跳过 {len(interesting)} 处 —— **这些要人看**:")
        by_why: dict[str, list] = {}
        for p, ln, s, why in interesting:
            by_why.setdefault(why, []).append((p, ln, s))
        for why, items in sorted(by_why.items(), key=lambda kv: -len(kv[1])):
            print(f"\n  {why}  ×{len(items)}")
            for p, ln, s in items[:12]:
                print(f"    {p}:{ln}  {s}")
            if len(items) > 12:
                print(f"    … 另外 {len(items) - 12} 处")
    return 0


if __name__ == "__main__":
    sys.exit(main())
