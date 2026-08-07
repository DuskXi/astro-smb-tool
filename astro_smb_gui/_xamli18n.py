"""XAML 里的文案怎么翻。

**属性值上包不了 `_()`。** `<TextBlock Text="刷新"/>` 里那个"刷新"是 XML
属性,不是 Python 表达式 —— 没有地方放函数调用。

好在这些 XAML 是**当成字符串读进来再 `XamlReader.Load` 的**,所以在那之前
把属性值换掉就行,不用去遍历建好的视觉树(那条路要认每种控件的哪个属性是
文案,还要处理模板里的元素,复杂得多)。

只动**白名单里的属性**,而且只动值里含中文的那些 —— `x:Name`、`Style`、
`Grid.Row` 这些一律不碰。

## 实体的坑

XAML 是 XML,属性值里会有 `&#10;`(换行,提示气泡里常用)、`&amp;` 之类。
**msgid 必须是解码之后的样子**,否则译者看到的是一串 `&#10;`;而写回去时
又必须重新转义,否则生成的 XML 直接坏掉。两边用的是同一对函数,
`scripts/i18n_extract.py` 抽 XAML 时也走这里的 `msgids()` —— 一份实现,
不会漂。
"""
from __future__ import annotations

import html
import re
from pathlib import Path

from astro_smb.i18n import gettext as _

#: 会承载用户可见文案的属性。**白名单而不是黑名单** —— 漏翻一个属性只是
#: 少翻一句,而误翻 `x:Name` 会让 `FindName` 全线失灵(那是静默的)。
ATTRS = ("Text", "Content", "PlaceholderText", "ToolTipService.ToolTip",
         "Header", "Description", "Title")

_CJK = re.compile(r"[一-鿿　-〿＀-￯]")
_ATTR_RE = re.compile(
    r'(?P<name>' + "|".join(a.replace(".", r"\.") for a in ATTRS) + r')'
    r'(?P<mid>\s*=\s*")(?P<val>[^"]*)(?P<end>")')


def _decode(raw: str) -> str:
    """属性值原文 → msgid(把 `&#10;`/`&amp;` 之类还原成真字符)。"""
    return html.unescape(raw)


def _encode(text: str) -> str:
    """msgid/译文 → 能塞回属性值的样子。

    顺序要紧:`&` 必须**先**换,否则会把后面几步产出的实体再转义一遍。
    """
    return (text.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;")
            .replace("\n", "&#10;"))


def msgids(xaml: str) -> list[str]:
    """这份 XAML 里所有该翻的字符串(已解码)。抽词表和翻译共用同一套判据。"""
    out: list[str] = []
    for m in _ATTR_RE.finditer(xaml):
        val = _decode(m.group("val"))
        if val and _CJK.search(val):
            out.append(val)
    return out


def localize(xaml: str) -> str:
    """把白名单属性上的中文换成当前语言。中文下**逐字节返回原文**。"""
    def sub(m: re.Match) -> str:
        val = _decode(m.group("val"))
        if not val or not _CJK.search(val):
            return m.group(0)
        return (m.group("name") + m.group("mid") + _encode(_(val))
                + m.group("end"))

    return _ATTR_RE.sub(sub, xaml)


def load_text(path: Path) -> str:
    """读一份页面 XAML 并翻好。**所有 `XamlReader.Load` 都该走这里。**"""
    return localize(Path(path).read_text(encoding="utf-8"))
