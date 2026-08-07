"""文档不许说谎。

docs/DEVELOPMENT.md 存在的理由是"让没有历史上下文的人不必追溯对话就能接手"。
一份**写着假话**的上手文档比没有更糟 —— 读的人会照着它做决定。

这里盯的不是文风,是几条**会随代码漂移、而漂了就会误导人**的事实。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "DEVELOPMENT.md"


def _text() -> str:
    return DOC.read_text(encoding="utf-8")


class TestNoStaleNumbers:
    def test_no_hardcoded_test_count(self):
        """**别写死单测数量。**

        写死的数字必然漂:文档说 1600,实际已经 1890 —— 而漂了的数字比没有
        更糟,读的人会以为它是准的。要给数字就给命令。
        """
        bad = []
        for i, line in enumerate(_text().splitlines(), 1):
            if re.search(r"单测\s*[0-9,]{3,}\s*个", line):
                bad.append(f"{i}: {line.strip()}")
            if re.search(r"当前\s*[0-9,]{3,}\s*个[,,]?\s*\d*\s*skipped", line):
                bad.append(f"{i}: {line.strip()}")
        assert not bad, f"docs/DEVELOPMENT.md 里写死了单测数量: {bad}"


class TestTheDirectoryMapMatchesReality:
    """§3 的目录树要真的对得上 —— 那是新人找路的第一张图。"""

    def _tree(self) -> str:
        """§3 那个围栏代码块的内容。

        **必须限定到那个块。** 第一版查的是"整个文件里出现过这个名字" ——
        而包名在别的章节里也会出现,把它从目录树里删掉照样绿。
        (这是本轮第三次踩同一个坑:断言的东西必须是它声称在测的那个。)
        """
        text = _text()
        head = text.index("## 3. 目录结构与模块职责")
        block = text.index("```", head)
        return text[block:text.index("```", block + 3)]

    def test_every_top_level_package_is_listed(self):
        tree = self._tree()
        packages = [p.name for p in ROOT.iterdir()
                    if p.is_dir() and (p / "__init__.py").exists()
                    and not p.name.startswith(".")]
        assert packages, "一个包都没找到 —— 检查这条测试本身"
        missing = [p for p in packages if p + "/" not in tree]
        assert not missing, f"§3 的目录树里没有这些包: {missing}"

    def test_the_frontends_are_on_the_map(self):
        """Uno 删除后只剩两套:WinUI3(原型)与 Qt(跨平台交付)。"""
        tree = self._tree()
        for token in ("astro_smb_app/", "astro_smb_qt/", "astro_smb_gui/"):
            assert token in tree, f"§3 的目录树里找不到 {token}"

    def test_the_deleted_frontend_is_not_still_advertised(self):
        """Uno 已删。目录树里还留着它,读的人会去找一个不存在的包。"""
        tree = self._tree()
        for token in ("frontend/", "vocabulary.json", "astro_smb_app/ui",
                      "astro_smb_app/proto"):
            assert token not in tree, f"§3 的目录树里还写着已删除的 {token}"


class TestClosedLimitationsAreNotStillOpen:
    """§12 的"已知限制"里,已经解掉的不许还写成待办。

    一条"暂时用 -H 绕过"会让接手的人绕过一个其实已经修好的东西。
    """

    def test_the_cli_default_host_limitation_is_marked_closed(self):
        text = _text()
        # 那条限制的原文里有"暂时用 `-H` 绕过";解掉之后不该还在
        assert "暂时用 `-H` 绕过" not in text, \
            "CLI 默认地址那条已经在 B20 解掉了,§12 还写着待办"

    def test_no_limitation_claims_devices_lives_in_the_gui_package(self):
        """设备记录 B20 下沉到核心库了 —— 还说它在 GUI 包里会让人绕远路。"""
        text = _text()
        assert "`devices.py` 在 `astro_smb_gui/` 下" not in text
