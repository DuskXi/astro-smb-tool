"""心跳:连上就要跳第一拍,不能先等 4 秒。

独立验收在空间页截到两张相隔约 2 秒的图,右上角都写着「○ 断开」——
而设备就在那儿、刚刚才连上。查下来不是探测失败,是**顺序**:
心跳循环写的是 `while not self._hb_stop.wait(HEARTBEAT_S)`,
先等再跳 —— 于是连接成功之后的**头 4 秒一次心跳都没有**,
`conn` 里还没有 rtt,状态栏那 4 秒显示的就是「断开」。

空间页尤其容易撞上:`--auto` 一进页面就开扫大目录,人眼正盯着它,
而那几秒恰好落在第一拍之前。

老 UI 的 watcher 是"连上就 poke 一轮"的规矩,这边照它改。
"""
from __future__ import annotations

import ast
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

ROOT = Path(__file__).resolve().parents[1]
SHELL = ROOT / "astro_smb_qt" / "shell.py"
MIRROR = ROOT / ".tmp" / "device" / "EMMC Images"


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    from PySide6.QtWidgets import QApplication

    from astro_smb_qt import theme

    inst = QApplication.instance() or QApplication([])
    theme.apply(inst)
    return inst


def test_the_loop_beats_before_it_waits():
    """**结构上**先跳后等:`wait()` 不能出现在循环体的最前面。"""
    tree = ast.parse(SHELL.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "loop")
    src = "\n".join(ast.unparse(s) for s in fn.body)
    assert "while not self._hb_stop.wait" not in src, (
        "又变回「先等再跳」了 —— 连上头 4 秒会显示断开")
    assert "_hb_stop.wait(HEARTBEAT_S)" in src, "总得有个节流"


@pytest.mark.skipif(not MIRROR.is_dir(), reason="没有离线镜像")
def test_the_first_beat_is_prompt(qt_app):
    """**行为验证。** 只查结构挡不住"把 wait 挪到别处但还是先等" ——
    这里直接量第一拍多久到:必须远小于一个心跳周期。

    **和 `test_it_throttles_between_beats` 一样要 `DirectConnection`**:默认的
    排队连接量到的是**信号送达**时刻,而送达要等主线程回到事件循环 ——
    并行跑测试时主线程被挤住,量出来的是"轮到我了"而不是"心跳跳了"。
    (这条就是这么在 `-n auto` 下红过一次的,产品代码没问题。)
    """
    from PySide6.QtCore import Qt

    from astro_smb_qt.shell import HEARTBEAT_S, Shell

    win = Shell(host=str(MIRROR), page="browse")
    beats: list[float] = []
    win.heartbeat.connect(lambda _st: beats.append(time.time()),
                          Qt.DirectConnection)
    t0 = time.time()
    deadline = t0 + HEARTBEAT_S      # 一个周期之内就得有第一拍
    while time.time() < deadline and not beats:
        qt_app.processEvents()
        time.sleep(0.02)
    assert beats, f"{HEARTBEAT_S}s 之内一拍都没有"
    assert beats[0] - t0 < HEARTBEAT_S * 0.5, (
        f"第一拍等了 {beats[0] - t0:.2f}s,太晚了")


@pytest.mark.skipif(not MIRROR.is_dir(), reason="没有离线镜像")
def test_a_local_device_reads_as_online(qt_app):
    """本地目录不该被说成「断开」—— 它就在那儿。"""
    from astro_smb_qt.shell import HEARTBEAT_S, Shell

    win = Shell(host=str(MIRROR), page="browse")
    got: list[dict] = []
    win.heartbeat.connect(got.append)
    deadline = time.time() + HEARTBEAT_S
    while time.time() < deadline and not got:
        qt_app.processEvents()
        time.sleep(0.02)
    assert got, "没有心跳"
    assert got[0].get("rtt") is not None, f"本地设备被判成断开: {got[0]}"


def test_stopping_still_works():
    """先跳后等之后,停止标志仍要能把循环收掉 —— 否则关窗留一个线程。"""
    tree = ast.parse(SHELL.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "loop")
    src = "\n".join(ast.unparse(s) for s in fn.body)
    assert src.count("break") >= 2, (
        "至少要有「没有 factory 时」和「正常一拍之后」两处退出")


def test_the_emit_is_guarded_against_a_dead_window():
    """**窗口先没、心跳后发** —— 不能刷 traceback。

    心跳是 daemon 线程,窗口先于它消失是正常的;这时 `emit` 抛
    `Signal source has been deleted`。以前几乎撞不上(第一拍要等 4 秒,
    窗口早关完了),改成"连上就跳第一拍"之后 0.18s 就发,**立刻在全量
    测试末尾刷出一段 traceback** —— 是这次改动把这个潜伏的洞照出来的。
    `workers.py` 的 `_Job.run` 早就是这么处理的。

    这条**只能靠结构断言**:要造出"C++ 对象已销毁但线程还在发"的确定
    场景,得 `shiboken6.delete()` 强拆窗口,而那会连带触发一堆与心跳无关
    的 teardown 报错(实测 `refresh_devices` 先炸)。靠 GC + excepthook
    的写法则根本不确定 —— 变异实测能活下来,那种测试比没有更糟。
    """
    tree = ast.parse(SHELL.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "loop")
    guarded = False
    for node in ast.walk(fn):
        if not isinstance(node, ast.Try):
            continue
        body = "\n".join(ast.unparse(b) for b in node.body)
        if "heartbeat.emit" not in body:
            continue
        for h in node.handlers:
            if h.type is not None and "RuntimeError" in ast.unparse(h.type):
                guarded = True
    assert guarded, "heartbeat.emit 没有被 except RuntimeError 兜住"


@pytest.mark.skipif(not MIRROR.is_dir(), reason="没有离线镜像")
def test_it_throttles_between_beats(qt_app, monkeypatch):
    """**跳完要等。** 少了节流那一句,心跳线程会全速空转 ——
    不报错、不崩,只是一个后台线程把一个核吃满。

    把周期调小再量间隔,免得这条测试自己要跑 4 秒。

    **必须用 `DirectConnection`。** 默认的排队连接量到的是**信号送达**时刻,
    不是**发出**时刻 —— 主线程一忙(并行跑测试时很常见),两拍会在同一次
    `processEvents()` 里一起投递,间隔看起来是 0,而心跳其实老老实实等过了。
    这条测试因此在 `-n auto` 下红过一次,而产品代码没有任何问题。
    """
    from PySide6.QtCore import Qt

    import astro_smb_qt.shell as sh

    monkeypatch.setattr(sh, "HEARTBEAT_S", 0.25)
    win = sh.Shell(host=str(MIRROR), page="browse")
    stamps: list[float] = []
    win.heartbeat.connect(lambda _st: stamps.append(time.time()),
                          Qt.DirectConnection)
    # 期望 0.25s 一拍,正常情况下两拍 0.5s 就够。给到 5s 是**留给机器忙的时候**
    # —— 循环拿到两拍就提前出来,所以放宽不会让这条测试变慢。
    deadline = time.time() + 5.0
    while time.time() < deadline and len(stamps) < 3:
        qt_app.processEvents()
        time.sleep(0.01)
    win._hb_stop.set()

    # **"一拍都没有"和"只来得及一拍"是两回事。**
    # 前者是真 bug(心跳根本没跑);后者只说明这台机器当时很忙 —— 而这条
    # 测试要证的是"两拍之间有间隔",一拍证不了也证不伪。原来两者都算失败,
    # 于是 `-n auto` 满载跑全量时会偶发变红,而产品代码没有任何问题。
    assert stamps, "5s 内一拍都没有 —— 心跳根本没跑起来"
    if len(stamps) < 2:
        pytest.skip(f"5s 内只跳了 {len(stamps)} 拍(机器忙),节流这件事这轮量不了")

    gap = stamps[1] - stamps[0]
    assert gap > 0.25 * 0.5, f"两拍只隔 {gap:.3f}s —— 节流没了,线程在空转"
