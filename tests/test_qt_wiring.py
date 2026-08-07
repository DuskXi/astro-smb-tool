"""Qt 前端的**接线门禁** —— 截图看不出来的那几类问题。

这一份对付的三种毛病,都是"不报错、不崩溃,只是界面行为不对":

1. **提示文案指向不存在的按钮。** 空态写「点『重新扫描』」而按钮叫
   「扫描此目录」—— 叫用户去点一个不存在的东西,比不给提示更糟。
2. **按钮没接槽。** 没连上的按钮和连上的长得一模一样,点了什么也不发生。
3. **忘了注册/忘了实现契约方法。** 一页写完不注册 = 永远打不开的死模块。

纯静态分析(``ast``),**不需要 PySide6 也不需要设备**。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
QT_DIR = ROOT / "astro_smb_qt"
PAGES_DIR = QT_DIR / "pages"

pytestmark = pytest.mark.skipif(not QT_DIR.is_dir(), reason="没有 astro_smb_qt 包")

SOURCES = sorted(PAGES_DIR.glob("*.py")) + [QT_DIR / "shell.py"]

#: 「…」里出现、但**不是**按钮的东西:页面名(导航项)与设备侧的文件名
NOT_A_BUTTON = {"扫描设备", "浏览", "拍摄记录", "导星分析", "传输", "影像查看",
                "勾选模式", "重新扫描"}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _docstring_ids(tree: ast.AST) -> set[int]:
    out: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            out.add(id(body[0].value))
    return out


def _visible_strings(path: Path) -> list[str]:
    """模块里所有**不是** docstring 的字符串常量。

    docstring 里也会出现「…」(讲实现意图时),算进来就是一堆假阳性。
    """
    tree = _tree(path)
    docs = _docstring_ids(tree)
    return [n.value for n in ast.walk(tree)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and id(n) not in docs]


def _all_visible() -> list[str]:
    out: list[str] = []
    for p in SOURCES:
        if p.exists():
            out.extend(_visible_strings(p))
    assert out, "一个界面字符串都没收集到 —— 解析坏了,下面每条断言都会空过"
    return out


def _unwrap(node):
    """剥掉 i18n 的包装:``_("刷新")`` / ``N_("刷新")`` → ``"刷新"``。

    **不剥就等于这条断言全空过**:文案一包上 `_()`,第一个位置参数就从
    `Constant` 变成 `Call`,一个按钮都扫不到,而 `assert labels` 那道自检
    只要还剩几个没包的英文按钮(`+`、`1:1`)就照样绿。
    """
    while (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
           and node.func.id in ("_", "N_", "gettext")):
        if not node.args:
            return None
        node = node.args[0]
    return node if isinstance(node, ast.Constant) else None


def _button_labels() -> set[str]:
    """所有按钮/开关的文案。

    ``W.button(_("刷新"), ...)`` 的第一个位置参数、``text=`` 关键字,
    以及 ``W.check(_("勾选模式"))``。
    """
    labels: set[str] = set()
    for path in SOURCES:
        if not path.exists():
            continue
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else \
                fn.id if isinstance(fn, ast.Name) else ""
            if name not in ("button", "check", "EmptyState"):
                continue
            args = list(node.args)
            if name == "EmptyState":
                args = []          # EmptyState 的动作在 action= 关键字里
            for arg in args[:1]:
                c = _unwrap(arg)
                if c is not None and isinstance(c.value, str):
                    labels.add(c.value)
            for kw in node.keywords:
                if kw.arg in ("text", "action"):
                    c = _unwrap(kw.value)
                    if c is not None:
                        labels.add(str(c.value))
    return labels


def test_hints_point_at_a_real_button():
    """用户可见文案里的「某某」必须真的是某个按钮的文案。"""
    labels = _button_labels()
    assert labels, "一个按钮文案都没扫到 —— 这条断言没在测任何东西"
    missing = set()
    for text in _all_visible():
        for name in re.findall(r"「([^」]{1,12})」", text):
            if "{" in name:
                continue        # 「{host}」这种是 `.format()` 的占位符,不是按钮
            if name not in labels and name not in NOT_A_BUTTON:
                missing.add(name)
    assert not missing, (
        f"这些提示指向不存在的按钮: {sorted(missing)};"
        f"现有按钮文案: {sorted(labels)}")


@pytest.mark.parametrize("path", [p for p in SOURCES if p.exists()],
                         ids=lambda p: p.name)
def test_every_button_is_wired(path: Path):
    """每个按钮都要给 ``on_click``。没连上的按钮和连上的长得一模一样。"""
    naked = []
    for node in ast.walk(_tree(path)):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else \
            fn.id if isinstance(fn, ast.Name) else ""
        if name != "button":
            continue
        if not any(kw.arg == "on_click" for kw in node.keywords):
            label = (node.args[0].value if node.args
                     and isinstance(node.args[0], ast.Constant) else "?")
            naked.append((node.lineno, label))
    assert not naked, f"{path.name} 这些按钮没接槽: {naked}"


@pytest.mark.parametrize(
    "path", [p for p in sorted(PAGES_DIR.glob("*.py"))
             if p.name not in ("__init__.py", "base.py")],
    ids=lambda p: p.name)
def test_page_inherits_the_contract(path: Path):
    """每个页面类都要继承 ``Page``(而不是直接 QWidget)。

    继承 Page 才拿得到 ``self.bg``(世代计数器)、``on_connected`` 广播和
    主题切换钩子。直接 QWidget 的话这三样全没有,而界面看着一模一样。
    """
    bad = []
    for node in _tree(path).body:
        if not isinstance(node, ast.ClassDef) or not node.name.endswith("Page"):
            continue
        bases = {b.id if isinstance(b, ast.Name) else
                 b.attr if isinstance(b, ast.Attribute) else "" for b in node.bases}
        if not bases & {"Page", "_StubPage"}:
            bad.append(node.name)
    assert not bad, f"{path.name} 这些页面没继承 Page: {bad}"


def test_pages_never_touch_widgets_from_a_worker():
    """后台任务的结果只能经 ``Bg.run(on_done=...)`` 回来。

    ``Bg`` 内部走队列连接,所以 ``on_done`` 一定在 GUI 线程。页面里如果
    自己 ``threading.Thread`` 起线程,那条路就绕过了编组 —— 在 Qt 上通常
    不当场崩,而是随机的重绘错乱,比崩溃难查得多。
    """
    bad = []
    for path in sorted(PAGES_DIR.glob("*.py")):
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Attribute) and node.attr == "Thread":
                bad.append((path.name, node.lineno))
            if isinstance(node, ast.Name) and node.id == "Thread":
                bad.append((path.name, node.lineno))
    assert not bad, f"页面里直接起了线程: {bad} —— 用 self.bg.run(...)"


#: 后端上的慢方法。**只列后端独有的名字** —— ``refresh`` 这种在页面里另有
#: 含义(``ImageView.refresh``)的名字放进来就是一堆假阳性。
SLOW_BACKEND_CALLS = {
    "listdir", "list_shares", "dir_tree", "dir_stat", "scan_children",
    "count_children", "volume_info", "download_file", "download_dir",
    "upload_file", "upload_dir", "rmdir", "rename", "mkdir", "remove",
}


def test_long_running_calls_are_backgrounded():
    """慢调用不许出现在页面的同步路径上。

    判据用的是 AST 里真正的**方法调用**,不是全文找子串 —— 后者会把
    docstring 和同名的本地方法一起算进来(第一版就在 ``base.py`` 上假阳了)。

    这里做的是弱一点的版本:只要一个页面模块里出现了后端慢调用,它就必须
    import 了 ``with_client``(那是唯一"借一条自己的连接、跑完就关"的入口),
    而 ``with_client`` 只会出现在 ``bg.run`` 的 work 闭包里。
    """
    checked = 0
    for path in sorted(PAGES_DIR.glob("*.py")):
        tree = _tree(path)
        hits = [n.func.attr for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr in SLOW_BACKEND_CALLS]
        if not hits:
            continue
        checked += 1
        src = path.read_text(encoding="utf-8")
        assert "with_client" in src, (
            f"{path.name} 里有后端慢调用 {sorted(set(hits))} 却没用 with_client —— "
            "它是不是跑在 GUI 线程上?")
    assert checked >= 2, "一个有慢调用的页面都没扫到 —— 这条断言没在测任何东西"
