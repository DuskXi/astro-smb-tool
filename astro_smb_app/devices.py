"""兼容 shim:本模块已下沉到 :mod:`astro_smb.devices`。

**用 `sys.modules` 别名而不是 `from ... import *`** —— 后者取不到下划线私有名
(本包内有从它 import 私有名的地方),而且会造出**第二个模块对象**。
别名之后 `astro_smb_app.devices is astro_smb.devices`,调用点一个字节没改。

为什么下沉:CLI 在核心库里,而它需要"上次连过哪台" —— 这条一直被
`DEFAULT_HOST` 硬编码顶着(docs/DEVELOPMENT.md §12 记着这个已知限制,理由是"设备记录在
GUI 包里,核心库不能反向依赖")。B2 把它移到 `astro_smb_app`、B17 又把数据目录
收口到 `astro_smb.paths` 之后,那个理由就不成立了:这个模块只用 json/pathlib,
放核心库没有任何反向依赖。
"""
import sys

from astro_smb import devices as _module

sys.modules[__name__] = _module
