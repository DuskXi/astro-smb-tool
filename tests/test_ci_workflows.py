"""CI 里提到的文件必须真的存在。

**`.github/` 是这个仓库里唯一没人替它变红的地方。** 删一个目录,引用它的
测试会红、import 它的模块会红 —— 只有 workflow 不会:它要等下一次推送、
在 GitHub 上、事后才红,而那时红的原因看起来是"CI 坏了",不是"你删了
它要的东西"。

真事:Uno 那套前端 2026-08-04 整体删除,`ci.yml` 里两个 C# job 又活了三天,
每次推送都失败;`release.yml` 到现在还在 `dotnet publish` 一个不存在的
csproj —— 而它只在打 tag 时触发,所以要等到真的发版才知道。

这里查的是**路径**,不查语义:workflow 到底该干什么是人的判断,但它提到的
文件在不在,机器一眼就能看出来。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WF = sorted((ROOT / ".github" / "workflows").glob("*.yml"))

#: 形如 `scripts/x.py`、`frontend/src/…/y.csproj` 的仓库内路径。
#: 只认带目录分隔符且带后缀的 —— 裸词太容易撞上 job 名和 shell 关键字。
PATH_RE = re.compile(r"(?<![\w./-])([A-Za-z_][\w.-]*(?:/[\w.${}-]+)+\.[A-Za-z0-9]+)")

#: 跑起来才产生的东西,不是仓库里的文件。
RUNTIME = ("dist/", "build/", ".venv/", "node_modules/", "~/")


def _referenced(text: str) -> set[str]:
    out = set()
    for line in text.splitlines():
        code = line.split("#", 1)[0]
        for m in PATH_RE.finditer(code):
            rel = m.group(1)
            if rel.startswith(RUNTIME) or "${{" in rel:
                continue
            out.add(rel)
    return out


@pytest.mark.parametrize("wf", WF, ids=lambda p: p.name)
def test_every_path_it_mentions_exists(wf: Path):
    missing = sorted(r for r in _referenced(wf.read_text(encoding="utf-8"))
                     if not (ROOT / r).exists())
    assert not missing, (
        f"{wf.name} 引用了仓库里不存在的路径 —— 这个 workflow 一跑就失败:\n  "
        + "\n  ".join(missing))


def test_there_is_at_least_one_workflow():
    """自检:glob 空了的话上面那条**一条都不会跑**,而报告依然全绿。"""
    assert WF, ".github/workflows/*.yml 一个都没找到,门禁在空转"
