"""与界面无关的应用层:设备记录、缓存、传输队列、预览、日志聚合、卷枚举。

**这些模块此前住在 `astro_smb_gui/` 里**,但它们一个都不 import win32more ——
纯 Python + numpy/PIL,拿来做无头脚本、CLI 或另一套前端都没问题。B2 把它们
移到这里,是为了让新的跨平台前端不必反向依赖一个已冻结的 WinUI 包。

`astro_smb_gui/` 侧留了同名 shim(`sys.modules` 别名),所以老 UI 的
import 行**一个字节都没改**,而且拿到的是**同一个模块对象** ——
这点是硬要求:`metacache` 持有全局 sqlite 连接与一把锁,两份状态会互相踩。

分层:`astro_smb`(核心) ← `astro_smb_app`(本层) ← 各前端。本层**不许**
反向 import 任何前端包。
"""
