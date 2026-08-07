"""设备管理页的**视图模型** —— 输入 devices.json / 卷枚举 / 心跳,输出普通 dict。

这一层不碰任何 XAML、不碰网络:给它一条设备记录和一份心跳,它算出卡片上要显示
的徽章、分组、色调、相对时间。**老 UI 与新前端消费的是同一份**,所以两边的
"12 分钟前"、"占 87%"、"● 端口可达 5 ms"永远一致 —— 各写一份是本仓库反复
修过的病(A5 覆盖统计、A6 drift、_guiding 原语、搬出来的那九个模块)。

原本住在 `astro_smb_gui/_devices.py` 的「纯数据层」一节里(那里本来就有明确的
分节注释),B4 按计划随切片抽出来。老 UI 侧 re-export,函数体一个字节没动。

措辞纪律:所有文案一律 BMP 字符。老 UI 那边是因为 win32more 会按码点数算
HSTRING 长度、代理对会吃掉末尾一个字;新前端虽无此限,但两边共用这份文案,
就得按更严的那边来。
"""
from __future__ import annotations

import os
import time

from astro_smb.util import format_mtime, human_size
from astro_smb.i18n import gettext as _
from astro_smb_app import devices, volumes
from astro_smb_app.entries import looks_like_local_path


#: 快照超过这么久就在卡上标注采集时刻(纯判读阈值,归视图模型)
SNAPSHOT_STALE_AFTER = 60.0

#: 占用条宽度 —— 视图模型算的是**逻辑宽度**,渲染器照着画
_BAR_W = 140.0

# 分组小标题图标(Segoe Fluent Icons 私用区码位 —— BMP,不是 emoji,§7.1)
_GRP_NET = "\ue774"         # Globe:网络设备
_GRP_DISK = "\ue8b7"        # Folder:磁盘 / 路径
_GRP_CAPACITY = "\ue9d9"    # Diagnostic:容量
_GRP_ZWO = "\ue735"         # FavoriteStarFill:ZWO 特征
_GRP_RECORD = "\ue787"      # Calendar:设备记录

# 数值语义色(与浏览页详情卡同一套中间调,浅/深主题下都可读)
_TONE_RGB = {
    "good": (0x3F, 0xA9, 0x55),
    "warn": (0xD0, 0x8A, 0x00),
    "bad": (0xD9, 0x4A, 0x4A),
    "dim": (0x8A, 0x8A, 0x8A),
}

# 徽章配色:浅底 + 深字(两主题下均可读),与浏览页徽章同风格
_BADGE_RGB = {
    "conn": ((0xDD, 0xEF, 0xDD), (0x1B, 0x5E, 0x20)),   # 当前连接:绿
    "smb": ((0xD9, 0xE7, 0xF8), (0x0D, 0x47, 0xA1)),    # SMB:蓝
    "local": ((0xE2, 0xDC, 0xF4), (0x4A, 0x33, 0x82)),  # 本地卡:紫
    "zwo": ((0xFB, 0xEA, 0xC5), (0x7A, 0x52, 0x00)),    # ZWO 卡:琥珀
    "gone": ((0xE9, 0xE9, 0xE9), (0x45, 0x45, 0x45)),   # 已拔出:灰
    "plain": ((0xE6, 0xE6, 0xE6), (0x50, 0x50, 0x50)),  # 中性
}

# 状态点颜色
_DOT_RGB = {"good": (0x4C, 0xAF, 0x50), "warn": (0xFF, 0xB3, 0x00),
            "bad": (0xE5, 0x73, 0x73), "dim": (0x9E, 0x9E, 0x9E)}

#: 一张卡的控件模板。**整只卡一次 XamlReader.Load 建出来**:逐个 new 元素
#: 极慢(Rectangle 全套 1.7~2.1ms、TextBlock 0.88ms,见项目绘图性能铁律),
#: 而且代码里拿不到 ``{ThemeResource ...}`` 的主题画刷。按钮不写进模板 ——
#: 它们要挂事件,由代码建一次后随卡缓存。
def rel_time(ts: float | None, now: float | None = None) -> str:
    """unix 时间戳 → "刚刚 / 12 分钟前 / 3 天前";没有时间返回"从未"。"""
    try:
        ts = float(ts or 0.0)
    except (TypeError, ValueError):
        return _("从未")
    if ts <= 0:
        return _("从未")
    now = time.time() if now is None else float(now)
    d = now - ts
    if d < 60:                      # 含时钟回拨造成的负数
        return _("刚刚")
    if d < 3600:
        return _("{0} 分钟前").format(int(d // 60))
    if d < 86400:
        return _("{0} 小时前").format(int(d // 3600))
    if d < 86400 * 30:
        return _("{0} 天前").format(int(d // 86400))
    if d < 86400 * 365:
        return _("{0} 个月前").format(int(d // (86400 * 30)))
    return _("{0} 年前").format(int(d // (86400 * 365)))


def snapshot_note(ts: float | None, now: float | None = None) -> str:
    """"这些数字是什么时候采的" —— 卡片右上角的淡色副注。

    容量/ZWO 命中/已插入都来自上一次刷新的快照,而页面每 4s 会被心跳带着
    重画一遍,看上去像实时数据。新鲜(< :data:`SNAPSHOT_STALE_AFTER`)时
    返回空串不加噪声;从没采过返回"未采集" —— **宁可说"我不知道",
    也不要把过期的确定性数字摆在那**。
    """
    try:
        ts = float(ts or 0.0)
    except (TypeError, ValueError):
        ts = 0.0
    if ts <= 0:
        return _("未采集")
    now = time.time() if now is None else float(now)
    if now - ts < SNAPSHOT_STALE_AFTER:
        return ""
    return _("采集于 {0}").format(rel_time(ts, now))


def heartbeat_line(hb: dict | None) -> str:
    """心跳计数行(**每 4s 就变**,所以绝不能进 ``groups``,见模块 docstring)。"""
    hb = hb or {}
    checks = hb.get("checks")
    if not checks:
        return ""
    fails = hb.get("fails") or 0
    return _("心跳 {checks} 次  ·  ").format(checks=checks) + (_("失败 {fails} 次").format(
        fails=fails) if fails else _("全部成功"))


def bmp_safe(text: str, limit: int = 0) -> str:
    """把用户输入回显到 UI 前的清洗:去掉星平面字符(§7.1)并可选截断。

    win32more 按**码点数**给 HSTRING 长度,一个代理对会让字符串末尾少一个
    字符 —— 用户粘进来的文本里完全可能有 emoji,不能直接往 TextBlock 上放。
    """
    out = "".join(c for c in str(text or "") if ord(c) <= 0xFFFF)
    if limit and len(out) > limit:
        out = out[:limit] + "…"
    return out


def abs_time(ts: float | None) -> str:
    """绝对时刻(作 KV 行的淡色副注);没有时间返回空串,不显示占位破折号。"""
    try:
        ts = float(ts or 0.0)
    except (TypeError, ValueError):
        return ""
    return format_mtime(ts) if ts > 0 else ""


def usage_tone(percent: float) -> str:
    """占用率 → 语义色:<70% 好,<90% 需留意,再往上是"快满了"。"""
    if percent >= 90.0:
        return "bad"
    return "warn" if percent >= 70.0 else "good"


def usage_percent(total: int, free: int) -> float:
    total = max(0, int(total or 0))
    used = max(0, total - max(0, int(free or 0)))
    return (used / total * 100.0) if total else 0.0


def capacity_pairs(total: int, free: int) -> list[tuple]:
    """容量三行 KV:已用(带占比条小组件)/ 可用 / 总量。

    ``total`` 为 0(读不到容量)时返回一行占位,不留空组。
    """
    total = max(0, int(total or 0))
    free = max(0, int(free or 0))
    if not total:
        return [(_("容量"), "—", _("读不到该卷的容量"))]
    used = max(0, total - free)
    pct = usage_percent(total, free)
    tone = usage_tone(pct)
    return [
        (_("已用"), human_size(used), _("占 {pct:.0f}%").format(
            pct=pct), False, tone, ("usagebar", pct)),
        (_("可用"), human_size(free), _("剩 {0:.0f}%").format(100.0 - pct)),
        (_("总量"), human_size(total)),
    ]


def zwo_pairs(hits) -> list[tuple]:
    """ZWO 特征命中 → KV 行。``hits`` 为 None 表示"还没扫过"。"""
    if hits is None:
        return [(_("特征目录"), _("未检测"), _("点上方「刷新」重新扫描"), False, "dim")]
    hits = list(hits)
    n = len(hits)
    total = len(volumes.ZWO_DIRS)
    if n >= volumes.MIN_HITS:
        note, tone = _("达到自动识别阈值"), "good"
    elif n:
        note, tone = _("未达阈值(需 ≥{MIN_HITS} 项)").format(MIN_HITS=volumes.MIN_HITS), "warn"
    else:
        note, tone = _("没有 ZWO 特征目录"), "dim"
    pairs: list[tuple] = [(_("特征目录"), _("{n} / {total} 项").format(
        n=n, total=total), note, False, tone)]
    if hits:
        order = {d.casefold(): i for i, d in enumerate(volumes.ZWO_DIRS)}
        shown = sorted(hits, key=lambda h: order.get(h.casefold(), 99))
        pairs.append((_("命中"), " / ".join(shown)))
    return pairs


def smb_status(host: str, *, connected: bool, hb: dict | None,
               rtt: dict | None) -> tuple[str, str]:
    """SMB 设备的状态徽章 → (文本, 语义色)。

    只有当前连接、且心跳确认存活的那台才说"在线"(有真 SMB 会话);其余一律
    只能说"端口可达" —— 路由器会对整个网段的 445 秒回 ACK(docs/DEVELOPMENT.md §2)。
    """
    hb = hb or {}
    rtt = rtt or {}
    if connected and hb.get("host") == host:
        if hb.get("alive"):
            ms = hb.get("rtt_ms")
            return ((_("● 在线 {ms:.0f} ms").format(
                ms=ms) if ms is not None else _("● 在线")), "good")
        return (_("● 离线"), "dim")
    if host not in rtt:
        return (_("○ 未探测"), "dim")
    ms = rtt[host]
    if ms is None:
        return (_("● 离线"), "dim")
    return (_("● 端口可达 {ms:.0f} ms").format(ms=ms), "good")


def local_status(present: bool | None) -> tuple[str, str]:
    """本地卡的状态徽章:只有"已插入 / 已拔出",没有"离线"这回事。"""
    if present is None:
        return (_("○ 未检测"), "dim")
    return (_("● 已插入"), "good") if present else (_("● 已拔出"), "dim")


def smb_card(rec: dict, *, connected: bool = False, hb: dict | None = None,
             rtt: dict | None = None, fresh=(), snap_ts: float = 0.0,
             now: float | None = None) -> dict:
    """一台 SMB 设备 → 卡片模型(纯数据)。

    ``fresh`` 是"状态来自实时探测(外壳每 20s 一轮)"的 host 集合 —— 不在
    里面的 host 只有本页快照那一份数据,要按 ``snap_ts`` 标注采集时刻。
    """
    host = rec.get("host", "")
    name = rec.get("name") or ""
    text, tone = smb_status(host, connected=connected, hb=hb, rtt=rtt)

    badges: list[tuple[str, str]] = []
    if connected:
        badges.append((_("当前连接"), "conn"))
    badges.append(("SMB", "smb"))
    if "ASIAIR" in (name or "").upper() or "ASIAIR" in host.upper():
        badges.append(("ASIAIR", "zwo"))

    shares = rec.get("shares")
    dev_pairs: list[tuple] = [
        (_("地址"), host, "", True),
        (_("服务器名"), name or "—"),
        (_("系统"), rec.get("os") or "—"),
        (_("协议"), rec.get("dialect") or "—"),
        (_("共享数"), _("{shares} 个").format(
            shares=shares) if shares is not None else _("未知")),
    ]
    rec_pairs: list[tuple] = [
        (_("最近连接"), rel_time(rec.get("last_ok"), now), abs_time(rec.get("last_ok"))),
        (_("首次记录"), rel_time(rec.get("first_seen"), now),
         abs_time(rec.get("first_seen"))),
    ]
    hb = hb or {}
    live_hb = bool(connected and hb.get("host") == host)
    # **心跳计数每 4s 就变**:它只能进 live(单个 TextBlock 原地改文字),
    # 进 groups 会让每次心跳都推倒重建整张 KV 表(实测 67 ms/次)。
    live = heartbeat_line(hb) if live_hb else ""
    # 状态来源:实时心跳 / 外壳的周期探测 / 只有本页快照(要标采集时刻)
    if live_hb or host in set(fresh) or host not in (rtt or {}):
        age = ""
    else:
        age = snapshot_note(snap_ts, now)
    return {
        "key": host,
        "host": host,
        "kind": devices.KIND_SMB,
        "title": name or host,
        "sub": host if name else _("SMB 设备"),
        "badges": badges,
        "status": (text, tone),
        "age": age,
        "live": live,
        "groups": [(_GRP_NET, _("设备"), dev_pairs), (_GRP_RECORD, _("记录"), rec_pairs)],
        "connect": (True, _("重新连接") if connected else _("连接此设备"), ""),
        "open": (False, "", ""),
        "forget": (not connected, _("忘记"),
                   _("正在连接中的设备不能忘记(断开或换设备后再来)") if connected else ""),
    }


def local_card(rec: dict, *, facts: dict | None = None, connected: bool = False,
               present_live: bool | None = None, snap_ts: float = 0.0,
               now: float | None = None) -> dict:
    """一台本地设备(ZWO 卡直插 / 拷到本机的资料夹)→ 卡片模型。

    ``facts`` 是刷新线程采到的这一份路径的事实(见 :func:`local_facts`);
    没有(还没刷新过)时按"未检测"渲染,不猜。

    ``present_live`` 是**外壳每 20s 一轮的探测**给出的插拔状态(比本页快照新):
    给了就以它为准 —— 卡拔掉后顶栏 20s 内就翻成"已拔出",本页不该还挂着
    上次刷新时的"已插入 + 容量 64GB"(真机复现过)。容量/ZWO 命中仍来自快照,
    所以拔出时它们会按既有逻辑退回"—",并由 ``age`` 标注采集时刻。
    """
    host = rec.get("host", "")
    root = devices.local_root(rec) or host
    facts = facts or {}
    present = facts.get("present") if present_live is None else present_live
    text, tone = local_status(present)

    # 标题「名字 (位置)」:卷根用「卷标 (盘符)」;子目录(卡的资料被拷到
    # D:\ASIAIR 这种)用「文件夹名 (所在盘符)」—— 拿盘符的卷标当子目录的名字
    # 会张冠李戴(探针实证)。卡拔了/没刷新过时没有盘符,退回文件夹名,
    # **不把整条路径塞进标题**(它就在下面的「路径」行里,重复一遍只会挤掉徽章)。
    drive = facts.get("drive") or ""
    vol_root = facts.get("volume_root") or ""
    folder = os.path.basename(root.rstrip("\\/")) or root
    loc = drive or folder
    name = folder if (vol_root and vol_root != root) else (
        facts.get("label") or rec.get("name") or "")
    title = f"{name} ({loc})" if name and name != loc else loc

    hits = facts.get("hits")
    badges: list[tuple[str, str]] = []
    if connected:
        badges.append((_("当前连接"), "conn"))
    badges.append((_("本地卡"), "local"))
    if hits:
        badges.append((_("ZWO 特征 {0} 项").format(len(hits)), "zwo"))
    if present is False:
        badges.append((_("已拔出"), "gone"))

    disk_pairs: list[tuple] = [(_("路径"), root, "", True)]
    if facts.get("label"):
        disk_pairs.append((_("卷标"), facts["label"]))
    disk_pairs.append((_("文件系统"), facts.get("fs") or "—",
                       "" if facts.get("fs") else _("读不到")))
    disk_pairs.append((_("磁盘类型"), facts.get("kind_text") or "—"))
    if facts.get("volume_root") and facts["volume_root"] != root:
        disk_pairs.append((_("所在卷"), facts["volume_root"], _("容量按整卷统计"), True))

    groups: list[tuple] = [(_GRP_DISK, _("磁盘"), disk_pairs)]
    if present is False:
        groups.append((_GRP_CAPACITY, _("容量"),
                       [(_("容量"), "—", _("卡已拔出或路径不存在,插回后点「刷新」"),
                         False, "dim")]))
    elif present:
        groups.append((_GRP_CAPACITY, _("容量"),
                       capacity_pairs(facts.get("total", 0), facts.get("free", 0))))
    groups.append((_GRP_ZWO, _("ZWO 特征"), zwo_pairs(hits if present else None)))
    groups.append((_GRP_RECORD, _("记录"), [
        (_("最近连接"), rel_time(rec.get("last_ok"), now), abs_time(rec.get("last_ok"))),
        (_("首次记录"), rel_time(rec.get("first_seen"), now),
         abs_time(rec.get("first_seen"))),
    ]))

    gone_tip = _("卡已拔出,插回后点「刷新」")
    return {
        "key": host,
        "host": host,
        "kind": devices.KIND_LOCAL,
        "path": root,
        "title": title,
        "sub": (facts.get("kind_text") or _("本地磁盘"))
               + (f"  ·  {facts['fs']}" if facts.get("fs") else ""),
        "badges": badges,
        "status": (text, tone),
        # 容量/文件系统/ZWO 命中全来自快照 —— 标出它是什么时候采的
        "age": snapshot_note(snap_ts, now) if present else "",
        "live": "",
        "groups": groups,
        "connect": (bool(present), _("重新连接") if connected else _("连接此设备"),
                    "" if present else gone_tip),
        "open": (bool(present), _("在资源管理器中打开"), "" if present else gone_tip),
        "forget": (not connected, _("忘记"),
                   _("正在连接中的设备不能忘记(断开或换设备后再来)") if connected else ""),
    }


def volume_card(facts: dict, *, snap_ts: float = 0.0,
                now: float | None = None) -> dict:
    """本机上枚举到、但**还没加进设备记录**的卷 → 卡片模型(带「添加为设备」)。"""
    root = facts.get("root", "")
    label = facts.get("label") or ""
    drive = facts.get("drive") or root
    hits = facts.get("hits") or []
    pct = usage_percent(facts.get("total", 0), facts.get("free", 0))

    badges: list[tuple[str, str]] = [(facts.get("kind_text") or _("本地磁盘"), "plain")]
    if len(hits) >= volumes.MIN_HITS:
        badges.append((_("疑似 ZWO 卡 {0} 项").format(len(hits)), "zwo"))

    pairs: list[tuple] = [
        (_("路径"), root, "", True),
        (_("文件系统"), facts.get("fs") or "—"),
    ]
    pairs += capacity_pairs(facts.get("total", 0), facts.get("free", 0))
    groups: list[tuple] = [(_GRP_DISK, _("磁盘"), pairs),
                           (_GRP_ZWO, _("ZWO 特征"), zwo_pairs(hits))]
    return {
        "key": f"vol:{root}",
        "host": root,
        "kind": devices.KIND_LOCAL,
        "path": root,
        "title": f"{label} ({drive})" if label else drive,
        "sub": _("加进设备记录后可在浏览/拍摄记录/导星各页当成一台设备直接用"),
        "badges": badges,
        "status": (_("已用 {pct:.0f}%").format(pct=pct) if facts.get("total") else "—",
                   usage_tone(pct) if facts.get("total") else "dim"),
        "age": snapshot_note(snap_ts, now),      # 整张卡都是快照
        "live": "",
        "groups": groups,
        "add": (True, _("添加为设备"), ""),
    }


def local_facts(root: str, vol=None, *, present: bool | None = None,
                hits=None) -> dict:
    """把一个本地路径的采集结果整理成 :func:`local_card` 吃的 dict。

    ``vol`` 是 :class:`astro_smb_gui.volumes.VolumeInfo`(该路径所在的卷),
    可能为 None(卷枚举里没有它,例如 ``D:\\ASIAIR备份`` 这种子目录,
    或者卡已经拔了)。
    """
    out: dict = {"root": root, "present": present, "hits": hits}
    if vol is not None:
        out.update(label=vol.label, drive=vol.drive, fs=vol.fs,
                   kind_text=vol.kind_text, total=vol.total, free=vol.free,
                   volume_root=str(vol.path))
    else:
        # 认不出所在卷(卡拔了/路径没了):**不编造盘符与容量**,留空让卡片
        # 自己退回文件夹名显示。
        out.update(label="", drive="", fs="", kind_text="", total=0, free=0,
                   volume_root="")
    return out


def volume_facts(vol, hits=None, others=None) -> dict:
    """:class:`VolumeInfo` → :func:`volume_card` 吃的 dict。"""
    return {"root": str(vol.path), "label": vol.label, "drive": vol.drive,
            "fs": vol.fs, "kind": vol.kind, "kind_text": vol.kind_text,
            "total": vol.total, "free": vol.free, "hits": list(hits or []),
            "others_n": len(others or [])}


def should_offer_volume(facts: dict) -> bool:
    """这个卷值不值得在"未加入设备记录"里列出来。

    列可移动盘/网络盘(卡最可能在这儿),以及**任何有 ZWO 特征命中**的盘;
    一台机器上的普通固定盘(C:/D: 这种)全列出来只是噪声 —— 真要加它们
    (例如把卡的资料拷到了 ``D:\\ASIAIR``)走「手动添加」更准。
    """
    if facts.get("hits"):
        return True
    return facts.get("kind") in (volumes.KIND_REMOVABLE, volumes.KIND_NETWORK)


def _bad(msg: str, **extra) -> dict:
    out = {"ok": False, "kind": "", "host": "", "path": "", "error": msg}
    out.update(extra)
    return out


def parse_manual_input(text: str, existing=(), isdir=None) -> dict:
    """手输的地址/路径 → ``{ok, kind, host, path, error, dup}``。

    支持这些写法:``192.0.2.225`` / ``\\\\192.0.2.225\\EMMC Images`` /
    ``//192.0.2.225/EMMC Images`` / ``smb://192.0.2.225/...``(都取主机名)/
    ``E:\\``、``D:\\ASIAIR``、``/media/card``(本地文件夹,必须真实存在)。

    **本函数会碰文件系统**(判断本地文件夹在不在):对不可达的 UNC 路径
    ``os.path.isdir`` 实测阻塞四十多秒 —— **只许在工作线程里调用**,
    UI 线程调用会冻住整个窗口(见 :meth:`DevicesPage._add`)。

    ``isdir`` 可注入(单测用),默认 :func:`os.path.isdir`。
    """
    isdir = os.path.isdir if isdir is None else isdir
    raw = (text or "").strip().strip('"').strip("'").strip()
    if not raw:
        return _bad(_('请输入 SMB 地址(如 192.0.2.225)或本地文件夹(如 E:\\)'))

    # 先把"明摆着是网络地址"的几种写法剥成主机名。
    # **正斜杠 UNC(//host/share)必须在这里认掉**:_common.looks_like_local_path
    # 对任何以 / 开头的串都返回 True,漏掉这一支就会拿它去 os.path.isdir ——
    # 而那正是"粘贴 ASIAIR 共享路径后窗口冻住四十多秒"的来源。
    host_from_net: str | None = None
    low = raw.lower()
    if low.startswith("smb://"):
        seg = raw[6:].lstrip("/\\").replace("\\", "/").split("/")[0]
        host_from_net = seg
    elif raw.startswith("\\\\") and not raw.startswith(("\\\\?\\", "\\\\.\\")):
        seg = raw[2:].replace("/", "\\").split("\\")[0]
        host_from_net = seg
    elif raw.startswith("//"):
        seg = raw[2:].lstrip("/").split("/")[0]
        host_from_net = seg

    # 本地分支只认"看起来就是本地路径"或"绝对路径且真实存在"的写法 —— 不然
    # 当前目录下恰好有个叫 nas 的文件夹时,手输主机名 nas 会被误当本地路径。
    if host_from_net is None and (looks_like_local_path(raw)
                                  or (os.path.isabs(raw) and isdir(raw))):
        path = raw
        if len(path) == 2 and path[1] == ":":       # "E:" → "E:\"
            path += "\\"
        if len(path) > 3:                           # 去掉结尾多余分隔符(盘符根保留)
            path = path.rstrip("\\/") or path
        if not isdir(path):
            return _bad(_("路径不存在或不是文件夹:{path}").format(path=path))
        # 去重按规范化的键比:自动发现写的是大写 "E:\",用户手输 "e:\" ——
        # 逐字节比较会把同一张卡记成两台设备(各占一个 MAX_RECORDS 名额)
        dupe = _find_existing(path, existing, devices.KIND_LOCAL)
        if dupe is not None:
            return _bad(_("{dupe} 已在设备记录中").format(dupe=dupe), dup=True, host=dupe,
                        kind=devices.KIND_LOCAL, path=dupe)
        return {"ok": True, "kind": devices.KIND_LOCAL, "host": path,
                "path": path, "error": ""}

    host = (host_from_net if host_from_net is not None else raw).strip()
    if not host:
        return _bad(_("没能从这串文本里认出主机名"))
    if ":" in host:
        return _bad(_("暂不支持指定端口:SMB 固定用 445,只填地址即可"))
    if any(c.isspace() for c in host) or any(c in host for c in "\\/|<>?*\"'"):
        return _bad(_("SMB 地址不能包含空格或路径分隔符;本地文件夹请填完整路径"))
    if len(host) > 255:
        return _bad(_("地址过长"))
    if not all(c.isalnum() or c in ".-_" for c in host):
        return _bad(_("看不出这是 IP 还是主机名:{host}").format(host=host))
    dupe = _find_existing(host, existing, devices.KIND_SMB)
    if dupe is not None:        # 主机名不区分大小写(DNS),同上
        return _bad(_("{dupe} 已在设备记录中").format(dupe=dupe), dup=True, host=dupe,
                    kind=devices.KIND_SMB)
    return {"ok": True, "kind": devices.KIND_SMB, "host": host, "path": "",
            "error": ""}


def _find_existing(host: str, existing, kind: str) -> str | None:
    """记录里已经有这台设备吗 → 返回**记录里的原始写法**(没有返回 None)。

    比较走 :func:`devices.host_key`(大小写/分隔符/尾斜杠无关);返回原写法
    是为了让提示语显示用户记录里真实存在的那一条,而不是他刚敲的变体。
    """
    key = devices.host_key(host, kind)
    for item in existing or ():
        if devices.host_key(item) == key:
            return item
    return None


def sorted_records(recs: list[dict]) -> list[dict]:
    """卡片顺序:本地卡在前(手边的东西优先),同类按 last_ok 倒序。

    ``devices.load()`` 已按 last_ok 倒序,这里只再稳定地把 local 提到前面。
    """
    local = [r for r in recs if devices.is_local(r)]
    smb = [r for r in recs if not devices.is_local(r)]
    return local + smb


# ---------------------------------------------------------------- KV 表结构签名

_BAR_W = 140.0          # 占用条宽度(原地改宽度时也要用,故提到模块级)


def _pair_spec(item: tuple) -> tuple:
    """把长度可变的 KV 元组补齐成 (标签, 值, 副注, 等宽, 语义色, 小组件)。"""
    return (item[0], item[1],
            item[2] if len(item) > 2 else "",
            item[3] if len(item) > 3 else False,
            item[4] if len(item) > 4 else None,
            item[5] if len(item) > 5 else None)


def _flat_pairs(groups: list[tuple]) -> list[tuple]:
    """按渲染顺序摊平所有 KV 行(空组跳过,与 ``_fill_groups`` 一致)。"""
    out: list[tuple] = []
    for _glyph, _name, pairs in groups:
        if not pairs:
            continue
        out.extend(pairs)
    return out


def _groups_shape(groups: list[tuple]) -> tuple:
    """KV 表的**结构签名**:只含"会改变控件树"的部分,不含具体数值。

    值/副注文字/语义色的深浅可以原地改;行数、标签、副注有无、等宽与否、
    小组件形态(含占用条要不要那根填充块)变了就必须整表重建。
    """
    out = []
    for glyph, name, pairs in groups:
        if not pairs:
            continue
        rows = []
        for item in pairs:
            k, _v, note, mono, tone, widget = _pair_spec(item)
            gadget = None
            if widget:
                try:
                    if widget[0] == "usagebar":
                        gadget = ("usagebar", float(widget[1]) > 0.0)
                    else:
                        gadget = (widget[0],)
                except (TypeError, ValueError, IndexError):
                    gadget = None
            rows.append((k, bool(note), bool(mono), tone is None, gadget))
        out.append((glyph, name, tuple(rows)))
    return tuple(out)
