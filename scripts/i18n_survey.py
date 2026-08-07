"""i18n 普查:用户可见文案散落在哪几层、各多少条。

结论与取舍见 `docs/architecture/i18n.md`。**文档里不写死数字** ——
这个仓库的规矩是"写死的数字必然漂,而漂了的数字比没有更糟"(docs/DEVELOPMENT.md §9),
所以要看当前值就跑这个脚本:

    uv run python scripts/i18n_survey.py

它统计的是**非 docstring 的、含中文的字符串常量**,按层分组。注释不算
(注释不面向用户),docstring 也不算。
"""
import ast, re, pathlib, collections

ROOT = pathlib.Path(".")
HAN = re.compile(r'[\u4e00-\u9fff]')

def visible_strings(path):
    """非 docstring 的字符串常量。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    docs = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef,
                          ast.AsyncFunctionDef)):
            b = getattr(n, "body", None)
            if (b and isinstance(b[0], ast.Expr)
                    and isinstance(b[0].value, ast.Constant)
                    and isinstance(b[0].value.value, str)):
                docs.add(id(b[0].value))
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docs and HAN.search(n.value)]

GROUPS = {
    "核心库 astro_smb/": ["astro_smb"],
    "共享视图模型 views/": ["astro_smb_app/views"],
    "共享应用层(其余)": ["astro_smb_app"],
    "老 UI astro_smb_gui/": ["astro_smb_gui"],
    "Qt astro_smb_qt/": ["astro_smb_qt"],
}
seen_files = set()
rows = []
for label, dirs in GROUPS.items():
    n_str = n_file = 0
    for d in dirs:
        p = ROOT / d
        if not p.exists():
            continue
        for f in sorted(p.rglob("*.py")):
            if "__pycache__" in f.parts:
                continue
            key = str(f)
            # ui/ 是 astro_smb_app 的子目录 —— 别重复计
            if label == "共享应用层(其余)" and "/ui/" in key.replace("\\", "/"):
                continue
            if label == "共享应用层(其余)" and "views" in f.parts:
                continue
            if key in seen_files and label != "Uno 前端 ui/":
                continue
            s = visible_strings(f)
            if s:
                n_str += len(s); n_file += 1
            seen_files.add(key)
    rows.append((label, n_file, n_str))

print(f"{'层':26s} {'文件':>5s} {'含中文的字符串':>14s}")
print("-" * 50)
tot = 0
for label, nf, ns in rows:
    print(f"{label:26s} {nf:5d} {ns:14d}")
    tot += ns
print("-" * 50)
print(f"{'合计':26s} {'':5s} {tot:14d}")

# XAML 里的文案(老 UI 独有)
xaml = list((ROOT / "astro_smb_gui").glob("*.xaml"))
n_xaml = sum(len(HAN.findall(f.read_text(encoding="utf-8"))) and
             len(re.findall(r'"[^"]*[\u4e00-\u9fff][^"]*"',
                            f.read_text(encoding="utf-8")))
             for f in xaml)
print(f"\n老 UI 的 XAML: {len(xaml)} 个文件 · 约 {n_xaml} 处含中文的属性值")
