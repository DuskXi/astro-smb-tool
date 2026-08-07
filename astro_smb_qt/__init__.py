r"""Astro SMB Tool 的 **PySide6/Qt 前端**。

分层(与另外两套前端完全一致,只是换了渲染框架):

```
astro_smb            核心库:SMB/本地/镜像后端、FITS、天文、日志解析、板解算
   ↑
astro_smb_app        共享应用层:设备记录、缓存、传输队列、预览、日志聚合、
   ↑                 卷枚举,以及各页的**视图模型**(views/)
   ├── astro_smb_gui        WinUI3 —— 已冻结,是 fallback
   ├── astro_smb_gui        WinUI3(Windows 原生 + 界面原型)
   └── astro_smb_qt         ← 这里
```

**下面两层一个字节都不改。** 判读(气量、高度角、采样、导星 RMS 合并、
天球投影、扫描判据……)全部只有一份实现,这一层只负责摆和画。

模块:

* ``theme``   —— 颜色/字号/间距/圆角的唯一真源 + 红光模式
* ``widgets`` —— Card / SectionTitle / StatusChip / SideNav / MetricRow / …
* ``workers`` —— 后台线程 + 世代计数器 + 信号编组
* ``models``  —— 页面模型(纯函数,可脱离 QApplication 单测)
* ``shell``   —— 外壳:侧边栏 / 连接栏 / 页面区 / 传输条 / 心跳 / watcher
* ``pages/``  —— 九页,一页一个模块

跑起来::

    uv run --with pyside6 python -m astro_smb_qt --host 192.0.2.227
    # 本地目录当设备用(卡直插 / 把卡的内容拷到本机),同一条代码路径:
    uv run --with pyside6 python -m astro_smb_qt --host "D:\ASIAIR\EMMC Images"
"""

__all__ = ["theme", "widgets", "workers", "models", "shell", "pages"]
