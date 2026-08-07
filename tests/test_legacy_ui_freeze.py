"""老 UI 冻结门禁。

`astro_smb_gui/` 是**功能完整、正在服役**的那套界面,在新的跨平台前端逐页
追平并验收之前,它是唯一可用的正式版本,也是 fallback。冻结的目的不是禁止
一切改动,而是让改动**必须是有意识的**:改了它,这条测试就红,提交前得显式
重算基线并在 `docs/architecture/frontend.md` 里记一笔。

**为什么要门禁而不是只写文档**:上一轮就是只写文档 ——
`docs/architecture/frontend-migration.md` 白纸黑字写着"astro_smb_gui 不再修改,
也不会进入 wheel、sidecar、安装器或新前端的导入图",而实际上它既在 wheel 里、
又被改了 8 次,原来那份清单对 7 个文件早已对不上,且**没有任何代码在读它** ——
纯孤儿。文档挡不住漂移,红色的测试才挡得住。
"""
from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUI = ROOT / "astro_smb_gui"
MANIFEST = ROOT / "docs" / "architecture" / "legacy-ui.sha256"

# 纳入冻结的文件类型。资产(web/*.js 等)一并纳入 —— 3D 天球页的 JS 是界面的
# 一部分,改了同样要走逃生口。
SUFFIXES = {".py", ".xaml", ".js", ".css", ".html"}


def _tracked() -> list[Path]:
    return sorted(p for p in GUI.rglob("*")
                  if p.is_file() and p.suffix in SUFFIXES
                  and "__pycache__" not in p.parts)


#: 参与冻结校验的文本后缀 —— 这些要做换行归一化
_TEXTUAL = {".py", ".xaml", ".js", ".css", ".html"}


def _digest(path: Path) -> str:
    """内容哈希。**文本先把换行归一到 LF 再算。**

    原来是按裸字节算的,注释里写着"工作区是 CRLF、git 里是 LF,两边一致即可"
    —— **那个假设只在某一个检出里成立**。仓库的 `.gitattributes` 是
    `* text=auto eol=lf`,所以新检出(CI、git worktree、别人 clone)拿到的是 LF;
    而我这台机器的工作区是 CRLF(早于 .gitattributes 的遗留)。

    结果:基线一旦在 CRLF 工作区重算,37 个冻结文件里有 24 个会在**所有**
    LF 检出上对不上 —— CI 会红,并行开发的 worktree 也会红,而**谁都没改过
    老 UI 一个字节**。那种红比不红更糟:它训练人忽略这道门禁。

    冻结要挡的是"内容变了",不是"你的检出用什么换行"。
    """
    raw = path.read_bytes()
    if path.suffix in _TEXTUAL:
        raw = raw.replace(b"\r\n", b"\n")
    return hashlib.sha256(raw).hexdigest()


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def build_manifest() -> str:
    """重算基线。**逃生口**:改了老 UI 之后跑

        uv run python -c "import tests.test_legacy_ui_freeze as t; \
print(t.build_manifest(), end='')" > docs/architecture/legacy-ui.sha256

    并在 `docs/architecture/frontend.md` 的变更表里记一笔(改了什么、为什么)。
    """
    return "".join(f"{_digest(p)}  {_rel(p)}\n" for p in _tracked())


def _load() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        digest, _, name = line.partition("  ")
        out[name.strip()] = digest.strip()
    return out


def test_manifest_covers_every_frozen_file():
    """基线必须**覆盖全部**文件 —— 漏掉的文件等于没冻结。

    这条单独存在是因为"少了一项"和"某项变了"是两种不同的事故:
    前者是基线本身失修(比如新增了页面却没重算),后者才是改动。
    """
    recorded = set(_load())
    actual = {_rel(p) for p in _tracked()}
    missing = sorted(actual - recorded)
    stale = sorted(recorded - actual)
    assert not missing, f"这些文件不在冻结基线里(新增后忘了重算?): {missing}"
    assert not stale, f"基线里有已不存在的文件: {stale}"


def test_frozen_ui_matches_the_recorded_baseline():
    """老 UI 的内容必须与基线逐字节一致。

    红了不代表你做错了 —— 代表你改了老 UI,而这需要是一个**有意识的**决定。
    确认要改就重算基线(见 `build_manifest` 的文档串)并在 frontend.md 记一笔。
    """
    recorded = _load()
    changed = [name for p in _tracked()
               if (name := _rel(p)) in recorded and _digest(p) != recorded[name]]
    assert not changed, (
        "老 UI 已冻结,这些文件与基线不符:\n  "
        + "\n  ".join(changed)
        + "\n\n确实要改就重算基线并在 docs/architecture/frontend.md 记一笔:\n"
          '  uv run python -c "import tests.test_legacy_ui_freeze as t; '
          "print(t.build_manifest(), end='')\" > docs/architecture/legacy-ui.sha256")
