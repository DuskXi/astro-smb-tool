"""三个后端的签名/默认值必须一致 —— 不一致时**两边都不报错**。

`StorageBackend` 是个 Protocol,Python 不会替你检查实现有没有对齐。
所以"同一段调用代码在 A 后端上是一个行为、在 B 后端上是另一个行为"这种事
完全可能悄悄成立,而且只在换后端时才发作。

真栽过一次:`MirrorBackend.read_bytes` 的 `size` 默认值写成"读全部",
而协议(以及另外两个后端)是 **64KB 部分读取**。同一句
`be.read_bytes(share, path)` 在镜像上拿到 52MB、在真设备上拿到 64KB ——
**两边都不报错**。FITS 预览那条路正好只读头部几 KB,所以在真机上是对的、
在镜像上白读了整张图,而界面看起来一模一样。

离线镜像/本地卡这类"开发用数据源"最大的风险就是这个:**它让你以为验过了。**
"""
from __future__ import annotations

import inspect

import pytest

from astro_smb.backend import LocalBackend
from astro_smb.client import AstroSmbClient
from astro_smb.mirror import MirrorBackend

BACKENDS = [LocalBackend, AstroSmbClient, MirrorBackend]

#: 这些方法的这些参数,默认值必须三家一致。
#: 挑的是"默认值不同会导致静默行为差异"的那些 —— 不是全部签名。
SHARED_DEFAULTS = {
    "read_bytes": ["offset", "size"],
    "listdir": ["path"],
    "list_shares": ["include_hidden"],
}


@pytest.mark.parametrize("method,params", SHARED_DEFAULTS.items())
def test_defaults_agree(method, params):
    seen: dict[str, dict] = {}
    for cls in BACKENDS:
        fn = getattr(cls, method, None)
        assert fn is not None, f"{cls.__name__} 没有 {method}"
        sig = inspect.signature(fn)
        seen[cls.__name__] = {
            p: sig.parameters[p].default
            for p in params if p in sig.parameters}
    first = next(iter(seen.values()))
    bad = {k: v for k, v in seen.items() if v != first}
    assert not bad or len(seen) == 1, (
        f"{method} 的默认值三家不一致:{seen}\n"
        "同一段调用代码在不同后端上会有不同行为,而且**两边都不报错** —— "
        "这正是离线数据源最容易骗人的地方")


def test_every_backend_has_the_read_path():
    """界面真正用得到的读接口,三家都得有 —— 缺一个就是换后端时炸。"""
    need = ["list_shares", "listdir", "stat", "exists", "read_bytes",
            "download_file", "volume_info", "server_info", "clone",
            "connect", "close"]
    for cls in BACKENDS:
        missing = [m for m in need if not hasattr(cls, m)]
        assert not missing, f"{cls.__name__} 缺:{missing}"


def test_clone_returns_a_new_object():
    """**每线程一个连接**是这个项目的硬纪律(impacket 不是线程安全的)。

    返回 self 的话,离线后端上跑得好好的、换真设备就串包 ——
    而这种差异恰恰会被"我在镜像上验过了"掩盖掉。
    """
    src = inspect.getsource(MirrorBackend.clone)
    assert "return self" not in src, (
        "clone 返回了自己 —— 会把「镜像上能跑、真设备上串包」藏起来")
