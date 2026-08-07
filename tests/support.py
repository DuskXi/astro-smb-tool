"""测试用的小助手。

## `tr`:断言"选中了哪条消息",而不是"那条消息长什么样"

写死中文的断言有两个毛病,而且都真实发生过:

* **改一句文案红一片** —— 于是没人敢改文案(这个仓库为此付过代价);
* **换语言全线红** —— 而代码一个字都没错。

`tr("从未")` 从**同一个 msgid** 出发算出期望值,两边一起变。不变量
("这个输入必须选中那条消息")还在,耦合没了。

**别拿它去比测试自己喂进去的假数据。** 那种情况下产品只是原样透传,
根本没经过翻译 —— 用 `tr()` 比就变成在测翻译本身了(踩过一次)。
"""
from __future__ import annotations


def tr(msgid: str, *args, **kw) -> str:
    """msgid → 当前语言(需要时套 `.format`)。"""
    from astro_smb.i18n import gettext as _

    s = _(msgid)
    return s.format(*args, **kw) if (args or kw) else s
