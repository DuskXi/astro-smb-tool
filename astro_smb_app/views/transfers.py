"""传输监控页的**视图模型**:``TransferJob`` → 普通 dict。

老 UI 的监控页把这些计算和 XAML 控件的原地更新缠在一起(行对象持久化、
只改 Fill、分区变化才 relayout),那是为了绕开 win32more 的事件泄漏与
逐元素调用开销。**那些约束只属于那个宿主**,算法本身是纯的,抽在这里。

一条从老 UI 原样继承的取舍:**方块要在生产侧下采样**。分块数会到几百
(50MB 的帧就有 50 块,大文件更多),而一行最多显示 ``MAX_BLOCKS`` 格。
在这里降,不要把几百个整数发过界再让前端丢掉。
"""
from __future__ import annotations

from astro_smb.util import human_size
from astro_smb.i18n import gettext as _
# **状态判据比常量,不比字面量** —— 见 `section_of` 的注释
from astro_smb_app.transfers import QUEUED

# 方块几何。与老 UI 监控页同一套 —— 那是按"宽而矮、一眼看出空洞在哪"调出来的。
MAX_BLOCKS = 128       # 单行最多显示的方块数
BLOCK_COLS = 64        # 每行方块列数
CELL = 9               # 格子(含间距)
SQUARE = 7             # 方块边长

PENDING, ACTIVE, DONE = 0, 1, 2


def downsample_blocks(states, limit: int = MAX_BLOCKS) -> list[int]:
    """把每块状态降到最多 ``limit`` 格,**聚合而不是抽稀**。

    抽稀会让"中间有个洞"这种最该看见的情况直接消失。聚合规则:
    一格里只要有传输中就显示传输中;全完成才显示完成;否则待传 ——
    也就是**宁可显示得保守一点**,不要让一格绿色掩盖里面没传完的块。
    """
    states = list(states or ())
    n = len(states)
    if n <= limit:
        return states
    out: list[int] = []
    for i in range(limit):
        lo = i * n // limit
        hi = max(lo + 1, (i + 1) * n // limit)
        chunk = states[lo:hi]
        if ACTIVE in chunk:
            out.append(ACTIVE)
        elif all(s == DONE for s in chunk):
            out.append(DONE)
        else:
            out.append(PENDING)
    return out


def section_of(job) -> str:
    """任务归到哪个分区。与老 UI 判据一致。

    **比常量,不比字面量。** 这里原来写的是 ``== "排队"``,而常量是
    ``QUEUED = "排队中"`` —— 两个字符串根本不相等,于是**排队中的任务全被
    分到「进行中」,「排队」分区永远是空的**。不报错,只是分区不对。
    验收清单 §9.1 标着"部分没测到(本地拷贝瞬时完成)",正是因为本地拷贝
    太快、没有任务在排队区停留到能被看见 —— 而这条是查 i18n 时顺出来的:
    **拿显示文本当身份**,改一个字就静默失效。
    """
    if getattr(job, "status", "") == QUEUED:
        return "queue"
    return "done" if getattr(job, "finished", False) else "run"


def row_model(job) -> dict:
    """一个任务 → 一行的显示数据。

    ``id`` 用 ``job_id``,**不是列表下标** —— 下标会在插入/完成时整体错位,
    补丁就会打到别的行上。
    """
    total = int(getattr(job, "total", 0) or 0)
    done = int(getattr(job, "done", 0) or 0)
    frac = (done / total) if total > 0 else 0.0
    speed = float(getattr(job, "speed", 0.0) or 0.0)

    bits = [f"{human_size(done)} / {human_size(total)}" if total else human_size(done)]
    if speed > 0:
        bits.append(f"{human_size(int(speed))}/s")
    eta = getattr(job, "eta", None)
    eta_s = eta() if callable(eta) else eta
    if eta_s:
        bits.append(_("剩 {0}").format(_dur(eta_s)))
    if getattr(job, "parallel", 0) and getattr(job, "n_chunks", 0):
        bits.append(_("{n_chunks} 块 / {parallel} 并发").format(
            n_chunks=job.n_chunks, parallel=job.parallel))
    if getattr(job, "error", ""):
        bits.append(str(job.error))

    return {
        "id": str(getattr(job, "job_id", "?")),
        # **字段叫 `label` 不是 `name`。** 读错了不报错 ——
        # `getattr` 的默认值一兜,每一行都变成"(未命名)"。
        # 这一处在共享层,所以两套前端同时中招。
        "name": str(getattr(job, "label", "") or _("(未命名)")),
        "group": str(getattr(job, "group", "") or ""),
        "status": str(getattr(job, "status", "")),
        "phase": str(getattr(job, "phase", "") or getattr(job, "status", "")),
        "fraction": max(0.0, min(1.0, frac)),
        "detail": " · ".join(bits),
        "blocks": downsample_blocks(getattr(job, "blocks", None)),
        # **为什么没有方块图,要说出来。** 本地设备(卡直插)刻意不走分块并发
        # (分块的全部价值是掩盖 SMB 的单流 RTT 瓶颈,本地盘顺序读就有 1.48 GB/s),
        # 于是监控页上一个格子都没有 —— 而界面什么都不说,用户只能怀疑是坏了。
        "no_blocks_why": (_("本地设备 · 顺序拷贝(不分块)")
                          if getattr(job, "local_device", False) else ""),
    }


def _dur(seconds: float) -> str:
    s = int(max(0.0, float(seconds)))
    if s < 60:
        return _("{s} 秒").format(s=s)
    if s < 3600:
        return _("{0} 分 {1} 秒").format(s // 60, s % 60)
    return _("{0} 小时 {1} 分").format(s // 3600, s % 3600 // 60)


def page_model(jobs, *, total_speed: float = 0.0) -> dict:
    """整页的显示数据。``jobs`` 是 ``TransferManager.jobs`` 那样的可迭代。"""
    sections: dict[str, list[dict]] = {"run": [], "queue": [], "done": []}
    for job in jobs or ():
        sections[section_of(job)].append(row_model(job))
    return {
        "sections": sections,
        "stats": {
            "active": len(sections["run"]),
            "queued": len(sections["queue"]),
            "done": len(sections["done"]),
            "speed": f"{human_size(int(total_speed))}/s" if total_speed else "0 B/s",
        },
    }
