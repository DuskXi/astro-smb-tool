"""XAML 属性值的翻译。

**属性值上包不了 `_()`** —— `<TextBlock Text="刷新"/>` 里那个"刷新"是 XML
属性,没有地方放函数调用。做法是在 `XamlReader.Load` **之前**处理字符串
(那些 XAML 本来就是当字符串读进来的),而不是去遍历建好的视觉树。

这份测试守三件事:

1. 只动白名单属性 —— 误翻 `x:Name` 会让 `FindName` 全线失灵,而那是静默的;
2. XML 实体进出对称 —— `&#10;` 要还原成真换行(否则译者看到一串实体),
   写回去又要重新转义(否则生成的 XML 直接坏掉);
3. **抽词表与运行时用同一份判据** —— 各写一套迟早漂,而漂了的结果是
   词表里有的界面上没有、界面上有的词表里没有,两边都不报错。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from astro_smb import i18n

pytest.importorskip("win32more")

from astro_smb_gui import _xamli18n as X       # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
GUI = ROOT / "astro_smb_gui"


class TestWhatItTouches:

    def test_it_translates_the_whitelisted_attributes(self):
        for attr in ("Text", "Content", "PlaceholderText"):
            src = f'<T {attr}="刷新"/>'
            assert X.msgids(src) == ["刷新"], attr

    def test_it_leaves_names_and_layout_alone(self):
        """**误翻 `x:Name` 会让 `FindName` 全线失灵,而且不报错。**"""
        src = '<T x:Name="刷新" Grid.Row="1" Style="{StaticResource 中文}"/>'
        assert X.msgids(src) == []
        assert X.localize(src) == src

    def test_ascii_values_are_left_alone(self):
        """纯 ASCII 的值不进词表 —— `Text="RA"` 不需要翻。"""
        assert X.msgids('<T Text="RA"/>') == []


class TestEntities:
    """真机 XAML 里有 `&#10;`(提示气泡的换行)。"""

    def test_numeric_refs_become_real_characters_in_the_msgid(self):
        got = X.msgids('<T Text="第一行&#10;第二行"/>')
        assert got == ["第一行\n第二行"], "msgid 里还留着实体,译者看到的是一串 &#10;"

    def test_round_trip_keeps_the_xml_valid(self):
        """中文下 `localize` 必须**逐字节返回原文** —— 它同时也是最好的往返检查。"""
        i18n.set_language("zh_CN")
        for p in sorted(GUI.glob("*.xaml")):
            src = p.read_text(encoding="utf-8")
            assert X.localize(src) == src, f"{p.name} 在中文下被改动了"

    def test_the_result_is_still_parseable_xml(self):
        """翻完还得是合法 XML —— 转义漏一个 `&` 就整份加载失败。"""
        import xml.etree.ElementTree as ET

        i18n.set_language("zh_CN")
        for p in sorted(GUI.glob("*.xaml")):
            ET.fromstring(X.localize(p.read_text(encoding="utf-8")))


class TestOneSourceOfTruth:

    def test_the_extractor_uses_the_runtime_judgement(self):
        """抽词表不许自己再写一套"哪些属性算文案"。"""
        src = (ROOT / "scripts" / "i18n_extract.py").read_text(encoding="utf-8")
        assert "_xamli18n import msgids" in src, (
            "抽取器另写了一套判据 —— 两边迟早漂,而漂了不报错")

    def test_every_xaml_load_goes_through_it(self):
        """**漏一个加载点,那一页就整页不翻。** 而它只是"看起来没翻",不报错。"""
        missed = []
        for p in sorted(GUI.glob("*.py")):
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if "XamlReader.Load(" not in line:
                    continue
                if "read_text(" in line:          # 直接读文件 = 绕过了本地化
                    missed.append(f"{p.name}:{i}")
        assert missed == [], f"这些加载点没走 _xamli18n: {missed}"

    def test_the_xaml_strings_are_in_the_catalog(self):
        """抽出来的 msgid 必须真的进了 `.pot`,否则译者根本看不到它们。"""
        import sys

        sys.path.insert(0, str(ROOT / "scripts"))
        import i18n_build as b

        pot = ROOT / "astro_smb" / "locale" / "astro_smb.pot"
        if not pot.is_file():
            pytest.skip("还没抽词表")
        have = set(b.parse_po(pot.read_text(encoding="utf-8")))
        want: set[str] = set()
        for p in sorted(GUI.glob("*.xaml")):
            want |= set(X.msgids(p.read_text(encoding="utf-8")))
        missing = sorted(want - have)
        assert not missing, f"这些 XAML 文案没进词表: {missing[:6]}"
