"""目录索引缓存层(建在 metacache 之上,纯标准库,无 GUI 依赖)。

**为什么要有这一层**:``listdir`` 每次都走网络。SMB 上一次 ``listPath`` ≈ 5 个
往返(connectTree/create/queryDirectory/close/disconnectTree),条目多时还要多
几次 ``queryDirectory``;在 ASIAIR 这种 RTT 不低的设备上,一个几百项的目录动辄
好几秒。用户来回切目录就是反复等。FITS 头(``preview.read_fits_header``)与日志
解析(``logstore``)早就吃上 metacache 了,目录索引是漏掉的一项。

两种数据
--------
1. **目录列表**(``DIR_KIND``)—— 一层目录的 ``RemoteEntry`` 列表。
   键 = ``(设备 host, share, 规范化 path)``。
2. **占用树**(``TREE_KIND``)—— ``client.dir_tree`` 的整棵 ``TreeNode``。
   空间页原本只有进程内内存缓存,退出即失;落盘后重开应用也能秒出。

失效策略(**本模块最需要想清楚的地方**)
--------------------------------------
目录没有可用的"源指纹":

* 目录条目的 ``size`` 在 LocalBackend 上恒为 0,SMB 上也不反映内容;
* 目录自身的 mtime 要么额外一次 ``stat``(~5 个往返,省不了多少),要么依赖
  "父目录 listing 里那条目录记录的 mtime" —— 而**共享根(path=="")的 stat 返回
  mtime=0**,且两个后端的 mtime 语义不同(SMB 是 ChangeTime,Local 是 WriteTime)。
  侦察阶段只在真机上确认了"目录里新增文件时该目录 mtime 会前进",**删除/改名
  是否同样前进没有实测**。⇒ **本层不依赖目录 mtime 做判定**。

所以采用「**乐观显示 + 后台对账**」:

* 命中缓存**立即出列表**(秒开),同时后台发一次真 ``listdir`` 校验,不一致就
  原地更新并给出可见但不打扰的提示 —— 陈旧列表比慢更糟,但"先给个大概、几百毫秒后
  自动纠正"比"干等几秒"更好;
* 用户点刷新 / 新建目录 / 改名 / 删除 / 入队上传之后,调用方**必须主动**
  ``invalidate(...)``;
* 占用树因为祖先的统计数字会被后代的任何变化改写,失效时**一律按共享整体清**。

``ASTRO_SMB_GUI_NOCACHE=1`` 时读缓存整体旁路(走 ``metacache.bypass_reads()``,
写照旧)—— 复现"冷启动"的真实耗时,不必删用户的缓存库。

线程模型:本模块只做 sqlite I/O,**必须在工作线程调用**(UI 线程零磁盘 I/O)。
"""

from __future__ import annotations

import threading
import time
from dataclasses import fields

from astro_smb_app import metacache
from astro_smb.client import RemoteEntry, TreeNode, normalize_remote_path
from astro_smb.i18n import gettext as _

# 数据结构版本。**RemoteEntry / TreeNode 字段变了会自动改指纹**(dc_schema_sig),
# 但"字段没变、含义/编码方式变了"的改动指纹认不出来 —— 那种情况**必须手动 +1**。
DIR_CACHE_VER = 1
TREE_CACHE_VER = 1

DIR_KIND = f"dirlist/{DIR_CACHE_VER}/" + metacache.dc_schema_sig(RemoteEntry)
TREE_KIND = f"dirtree/{TREE_CACHE_VER}/" + metacache.dc_schema_sig(TreeNode)

# 占用树落盘上限(节点数)。真机上 EMMC 222GB / 50MB 一张也就几千个节点;
# 设个上限只是防止有人拿本层去缓存一块几十万文件的盘,把 meta.db 撑爆。
TREE_MAX_NODES = 40000
# 占用树默认 TTL:超过一周的占用统计不再默认展示(界面另有"N 分钟前"标注)
TREE_TTL_S = 7 * 86400.0

_ENTRY_FIELDS = tuple(f.name for f in fields(RemoteEntry))

# 逐字段收敛类型:JSON 回来的 int 要提成 float、bool 要还原成 bool。
# 新增字段务必同步登记(没登记的字段按原样塞回去)。
_ENTRY_COERCE = {
    "share": str, "path": str, "name": str, "is_dir": bool,
    "size": int, "mtime": float, "ctime": float, "atime": float,
    "attributes": int,
}


# ---------------------------------------------------------------- 键 / 设备维度

def backend_of(client_or_host) -> str:
    """取 metacache 的 ``backend`` 维度值 = 设备 host(换设备天然隔离)。

    既接受 ``AstroSmbClient``/``LocalBackend``(读 ``.host``),也接受裸字符串
    —— 工作线程里手上常常只有 host 而没有 client。
    """
    if client_or_host is None:
        return ""
    if isinstance(client_or_host, str):
        return client_or_host
    return str(getattr(client_or_host, "host", "") or "")


def dir_key(share: str, path: str) -> str:
    """缓存主键的 key 维度。**path 必须先规范化** —— 否则
    ``"Autorun/Light"`` 与 ``"Autorun\\Light"`` 会各存一条。"""
    return f"{share}|{normalize_remote_path(path or '')}"


tree_key = dir_key      # 占用树与目录列表同一套 key(kind 已经分开了)


# ---------------------------------------------------------------- 目录列表编解码

def encode_entries(entries) -> dict:
    """``list[RemoteEntry]`` → 可 JSON 化 payload。

    行式(字段名单独存一份 + 每条一个数组)比"每条一个 dict"省约 3 倍体积,
    一个 500 项的目录大约 40KB。
    """
    return {
        "v": DIR_CACHE_VER,
        "ts": time.time(),
        "fields": list(_ENTRY_FIELDS),
        "rows": [[getattr(e, f) for f in _ENTRY_FIELDS] for e in entries],
    }


def decode_entries(payload) -> list[RemoteEntry] | None:
    """payload → ``list[RemoteEntry]``;结构不对一律返回 None(当未命中重读)。"""
    try:
        if not isinstance(payload, dict):
            return None
        names = payload.get("fields")
        rows = payload.get("rows")
        if not isinstance(names, list) or not isinstance(rows, list):
            return None
        if tuple(str(n) for n in names) != _ENTRY_FIELDS:
            return None     # 字段集/顺序变了:宁可不命中,也不能拼出半截对象
        out: list[RemoteEntry] = []
        for row in rows:
            if not isinstance(row, list) or len(row) != len(_ENTRY_FIELDS):
                return None
            kw = {}
            for n, v in zip(_ENTRY_FIELDS, row):
                fn = _ENTRY_COERCE.get(n)
                kw[n] = fn(v) if fn is not None else v
            out.append(RemoteEntry(**kw))
        return out
    except Exception:
        return None


def payload_age(payload) -> float:
    """payload 写入至今的秒数(拿不到就当 0)。"""
    try:
        ts = float(payload.get("ts") or 0.0)
    except Exception:
        return 0.0
    if ts <= 0.0:
        return 0.0
    return max(0.0, time.time() - ts)


# ---------------------------------------------------------------- 目录列表 API

def get_with_age(client_or_host, share: str, path: str, *,
                 ttl: float | None = None,
                 use_cache: bool = True) -> tuple[list[RemoteEntry], float] | None:
    """命中返回 ``(条目列表, 缓存年龄秒)``,未命中返回 None。**工作线程调用**。"""
    if not use_cache:
        return None
    try:
        hit = metacache.get(DIR_KIND, backend_of(client_or_host),
                            dir_key(share, path), ttl=ttl)
    except Exception:
        return None         # 缓存永远是可选的,坏了就当没有
    if hit is None:
        return None
    entries = decode_entries(hit)
    if entries is None:
        return None
    return entries, payload_age(hit)


def get(client_or_host, share: str, path: str, *,
        ttl: float | None = None,
        use_cache: bool = True) -> list[RemoteEntry] | None:
    """命中返回条目列表,未命中返回 None。**工作线程调用**。"""
    got = get_with_age(client_or_host, share, path, ttl=ttl, use_cache=use_cache)
    return None if got is None else got[0]


def put(client_or_host, share: str, path: str, entries) -> None:
    """写入/覆盖一层目录的索引。失败静默。**工作线程调用**。"""
    try:
        metacache.put(DIR_KIND, backend_of(client_or_host),
                      dir_key(share, path), encode_entries(entries))
    except Exception:
        pass


def put_async(client_or_host, share: str, path: str,
              entries) -> threading.Thread | None:
    """把 ``put`` 甩到守护线程(UI 线程绝不做 sqlite I/O)。返回线程供测试 join。"""
    host = backend_of(client_or_host)
    rows = list(entries)

    def work() -> None:
        put(host, share, path, rows)

    try:
        t = threading.Thread(target=work, daemon=True, name="dircache-put")
        t.start()
        return t
    except Exception:
        return None


def invalidate(host: str | None = None, share: str | None = None,
               path: str | None = None, *, subtree: bool = True,
               trees: bool = True) -> int:
    """按三种粒度失效目录索引,返回删除行数(失败返回 0)。

    * ``invalidate()`` —— 全部设备的全部目录索引;
    * ``invalidate(host)`` —— 某设备全部;
    * ``invalidate(host, share)`` —— 某设备某共享全部;
    * ``invalidate(host, share, path)`` —— 某个目录(``subtree=True`` 时连同其
      所有子目录 —— 删掉一棵目录树后,子目录的缓存也全废了)。

    ``host=None`` 表示"不限设备"(与 metacache 的 backend=None 同义)。

    **占用树一律按共享整体清**:树里祖先节点的大小/文件数是后代聚合出来的,
    任何一处变化都会让所有祖先的数字失真,按路径精确失效反而会留下错的祖先。
    """
    n = 0
    try:
        if share is None:
            n += metacache.invalidate(DIR_KIND, host)
        elif path is None:
            n += metacache.invalidate_prefix(DIR_KIND, host, f"{share}|")
        else:
            n += metacache.invalidate(DIR_KIND, host, dir_key(share, path))
            if subtree:
                p = normalize_remote_path(path or "")
                pref = f"{share}|{p}\\" if p else f"{share}|"
                n += metacache.invalidate_prefix(DIR_KIND, host, pref)
        if trees:
            if share is None:
                n += metacache.invalidate(TREE_KIND, host)
            else:
                n += metacache.invalidate_prefix(TREE_KIND, host, f"{share}|")
    except Exception:
        pass
    return n


# ---------------------------------------------------------------- 对账

def same(old, new) -> bool:
    """两份目录列表是否**逐字段**完全一致(RemoteEntry 是 frozen dataclass,
    ``==`` 已经比全部 9 个字段;listdir 的排序是确定的,顺序可比)。"""
    if old is None or new is None:
        return False
    return list(old) == list(new)


def diff_summary(old, new) -> tuple[int, int, int]:
    """``(新增, 消失, 变化)`` —— 供"缓存和设备对不上"的提示文案用。"""
    o = {e.path: e for e in (old or [])}
    m = {e.path: e for e in (new or [])}
    added = sum(1 for p in m if p not in o)
    removed = sum(1 for p in o if p not in m)
    changed = sum(1 for p, e in m.items() if p in o and o[p] != e)
    return added, removed, changed


def age_text(seconds: float) -> str:
    """缓存年龄的中文短文案(界面用;只用 BMP 字符)。"""
    try:
        s = max(0.0, float(seconds))
    except Exception:
        return _("刚刚")
    if s < 45:
        return _("刚刚")
    if s < 3600:
        return _("{0} 分钟前").format(int(s // 60))
    if s < 86400:
        return _("{0} 小时前").format(int(s // 3600))
    return _("{0} 天前").format(int(s // 86400))


# ---------------------------------------------------------------- 占用树编解码
#
# 紧凑编码:节点的 path 完全可以由父路径 + name 推出(client.listdir 就是
# `f"{path}\\{name}" if path else name`),所以只存根路径。
#   目录 → [name, 1, size, file_count, [子节点…]]
#   文件 → [name, 0, size]           (文件的 file_count 恒为 1)

def _count_nodes(root: TreeNode) -> int:
    total = 0
    stack = [root]
    while stack:
        n = stack.pop()
        total += 1
        stack.extend(n.children)
    return total


def _enc_node(n: TreeNode):
    if n.is_dir:
        return [n.name, 1, int(n.size), int(n.file_count),
                [_enc_node(c) for c in n.children]]
    return [n.name, 0, int(n.size)]


def _dec_node(raw, parent_path: str) -> TreeNode:
    name = str(raw[0])
    path = f"{parent_path}\\{name}" if parent_path else name
    if raw[1]:
        node = TreeNode(name=name, path=path, is_dir=True,
                        size=int(raw[2]), file_count=int(raw[3]))
        node.children = [_dec_node(c, path) for c in raw[4]]
        return node
    return TreeNode(name=name, path=path, is_dir=False,
                    size=int(raw[2]), file_count=1)


def encode_tree(tree: TreeNode) -> dict | None:
    """整棵占用树 → payload;``partial`` 树或节点数超上限返回 None(不缓存)。"""
    if tree is None or not tree.is_dir:
        return None
    if getattr(tree, "partial", False):
        return None     # 截断树绝不能当完整结果落盘(同 _space 的内存缓存约定)
    if _count_nodes(tree) > TREE_MAX_NODES:
        return None
    return {
        "v": TREE_CACHE_VER,
        "ts": time.time(),
        "root_path": tree.path,
        "root": _enc_node(tree),
    }


def decode_tree(payload) -> TreeNode | None:
    """payload → ``TreeNode``;结构不对一律返回 None(当未命中重扫)。"""
    try:
        if not isinstance(payload, dict):
            return None
        raw = payload["root"]
        if not isinstance(raw, list) or len(raw) < 5 or not raw[1]:
            return None
        rp = str(payload.get("root_path") or "")
        root = TreeNode(name=str(raw[0]), path=rp, is_dir=True,
                        size=int(raw[2]), file_count=int(raw[3]))
        root.children = [_dec_node(c, rp) for c in raw[4]]
        return root
    except Exception:
        return None


# ---------------------------------------------------------------- 占用树 API

def get_tree(client_or_host, share: str, path: str, *,
             ttl: float | None = TREE_TTL_S,
             use_cache: bool = True) -> tuple[TreeNode, float] | None:
    """命中返回 ``(树, 缓存年龄秒)``,未命中返回 None。**工作线程调用**。"""
    if not use_cache:
        return None
    try:
        hit = metacache.get(TREE_KIND, backend_of(client_or_host),
                            tree_key(share, path), ttl=ttl)
    except Exception:
        return None
    if hit is None:
        return None
    tree = decode_tree(hit)
    if tree is None:
        return None
    return tree, payload_age(hit)


def put_tree(client_or_host, share: str, path: str, tree: TreeNode) -> bool:
    """写入整棵占用树;partial/过大/失败一律返回 False。**工作线程调用**。"""
    try:
        payload = encode_tree(tree)
        if payload is None:
            return False
        metacache.put(TREE_KIND, backend_of(client_or_host),
                      tree_key(share, path), payload)
        return True
    except Exception:
        return False
