"""兼容 shim:本模块已移到 :mod:`astro_smb_app.metacache`。

**用 `sys.modules` 别名而不是 `from ... import *`** —— 后者取不到下划线私有名
(本包内实测有 0 处从这些模块 import 私有名),而且会造出**第二个模块对象**:
`metacache` 持有全局 sqlite 连接与一把锁,两份状态会互相踩。别名之后
`astro_smb_gui.metacache is astro_smb_app.metacache`,老 UI 的 import 行一个字节没改。

新代码请直接 import `astro_smb_app.metacache`。
"""
import sys

from astro_smb_app import metacache as _module

sys.modules[__name__] = _module
