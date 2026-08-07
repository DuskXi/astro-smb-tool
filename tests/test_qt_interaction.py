"""Qt 前端的**交互门禁**:真的建窗口、真的点、真的等后台线程回来。

跑在 ``QT_QPA_PLATFORM=offscreen`` 上 —— 不弹窗、不抢焦点、不需要显示器,
但走的是**完整的真代码路径**(信号编组、世代计数器、选中模型、表格滚动)。

它盯的是"截图证明不了"的那几类:

* **世代计数器**:快速连点几个目录,最后停在哪个。没有代次就是
  "进了 B 目录却显示 A 目录的内容"。
* **多选计数**:计数从后端状态来、选中在控件里,两者不一致时截图上看不出来。
* **长目录**:滚动/列宽塌陷只有几百上千行才发作,三行数据全都正常。

需要一台"设备"。本地目录就是正式支持的设备类型(卡直插电脑),所以这里用
``.tmp/device/…`` 那份;没有就整份 skip —— 但 skip 的理由会写清楚。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]


def _checkouts_with_device_data(rel: Path):
    """从这里往上逐级找 ``rel``。

    次级 checkout(worktree、并行副本、CI 的浅克隆)里 ``.tmp/`` 通常是空的
    —— 它被 gitignore,不随 checkout 复制,而几十 GB 的设备镜像只会存一份。
    判据是**内容在不在**,不是目录长什么样:凡是上级目录里存在这份数据的,
    都算数;都不存在时自然一个也不产出,不影响别的环境。
    """
    for base in [ROOT, *ROOT.parents]:
        cand = base / rel
        if cand.is_dir():
            yield cand


def _device_root() -> Path | None:
    """本地设备目录 —— 这份 checkout 里没有就往上找。"""
    return next(_checkouts_with_device_data(
        Path(".tmp") / "device" / "EMMC Images"), None)


DEVICE = _device_root()
pytestmark = pytest.mark.skipif(
    DEVICE is None,
    reason="没有 .tmp/device/EMMC Images —— 交互门禁需要一台设备(本地目录即可)")


# ---------------------------------------------------------------- 夹具

@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    from astro_smb_qt import theme

    inst = QApplication.instance() or QApplication([])
    theme.apply(inst)
    return inst


@pytest.fixture(scope="module")
def shell(app):
    from astro_smb_qt.shell import Shell

    win = Shell(host=str(DEVICE), page="browse")
    _pump_until(app, lambda: bool(win.shares), 30.0)
    assert win.shares, "连不上本地设备目录"
    yield win
    win.close()


def _pump(app, seconds: float = 0.05) -> None:
    import time

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)


def _pump_until(app, cond, timeout: float = 20.0) -> bool:
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------------- 连接

def test_shell_connects_and_broadcasts_shares(shell):
    """连上之后共享列表要广播到各页。

    本地目录**一个根目录 = 一个共享**,所以这里只会有一个 —— 共享栏必须
    吃得下"只有一个共享"(真机上是三个)。
    """
    assert shell.shares
    browse = shell.page("browse")
    assert browse.share_list.count() == len(shell.shares)


def test_current_page_reloads_after_connect(app):
    """启动顺序是"先选页、后连设备"(连接是异步的)。

    不在连上之后补一次 ``on_show``,首屏就会停在"还没有连接设备"的空态,
    而顶栏明明写着已连接 —— 真机截图上出现过。

    **必须用记录页来验。** 浏览页有自己的 ``on_connected``(拿到共享就去列
    根目录),所以它即使没有那一下补丁也会自己加载 —— 拿它验等于什么都没验。
    记录页只在 ``on_show`` 里懒加载,补丁没有它就永远停在空态。
    """
    from astro_smb_qt.shell import Shell

    win = Shell(host=str(DEVICE), page="records")
    try:
        assert _pump_until(app, lambda: win.page("records").data is not None, 40.0), \
            "以 --page records 启动,连上之后这一页仍然没有数据"
    finally:
        win.close()


# ---------------------------------------------------------------- 长目录

def _goto(app, browse, share: str, sub: str, timeout: float = 25.0) -> bool:
    """进一个目录并**等它真的换过来**。

    直接等 ``bool(browse.entries)`` 是不行的:上一个目录的条目还在,条件
    当场就成立,于是读到的是旧内容 —— 这个测试自己第一版就栽在这儿,
    而它恰恰是在测"迟到结果覆盖新状态"。所以先把 entries 清空当哨兵。
    """
    browse.entries = []
    browse.open_path(share, sub)
    return _pump_until(app, lambda: bool(browse.entries), timeout)


@pytest.fixture(scope="module")
def long_dir(app, shell):
    """挑一个条目最多的目录。滚动/列宽塌陷只有几百行才发作。"""
    browse = shell.page("browse")
    best, best_n = None, 0
    for sub in ("Autorun\\Bias", "Autorun\\Dark", "Autorun\\Flat", "log"):
        if not _goto(app, browse, shell.shares[0], sub):
            continue
        if len(browse.entries) > best_n:
            best, best_n = sub, len(browse.entries)
    if best is None or best_n < 50:
        pytest.skip(f"没有足够长的目录(最多 {best_n} 项)")
    _goto(app, browse, shell.shares[0], best)
    return best, best_n


def test_long_directory_renders_every_row(app, shell, long_dir):
    _sub, n = long_dir
    table = shell.page("browse").table
    assert table.model().rowCount() == n, "长目录的行数对不上"
    assert len(table.keys()) == n


def test_long_directory_scrolls(app, shell, long_dir):
    """表必须**自己能滚**,而且不能被套进 ``QScrollArea``。

    套进去它就拿到无限高度、永远不滚(外层那个也没有边界)—— Qt 上和另外
    那套前端的 ScrollViewer 是同一个坑。两条断言各管一半:祖先里没有滚动区
    是**结构**保证,可滚范围 > 0 是**行为**证据。
    """
    from PySide6.QtWidgets import QScrollArea

    table = shell.page("browse").table
    node = table.parentWidget()
    while node is not None:
        assert not isinstance(node, QScrollArea), (
            f"表被套进了 {type(node).__name__} —— 它会拿到无限高度,永远不滚")
        node = node.parentWidget()

    table.resize(900, 300)
    _pump(app, 0.2)
    bar = table.verticalScrollBar()
    assert bar.maximum() > 0, "长目录里竖直滚动条没有可滚范围 —— 表拿到了无限高度?"


def test_row_keys_are_paths_not_indices(app, shell, long_dir):
    """行身份是共享内相对路径。下标会随增删行漂移,选中就跟着漂。"""
    keys = shell.page("browse").table.keys()
    assert len(set(keys)) == len(keys), "行键有重复"
    assert not all(k.isdigit() for k in keys[:5]), "行键看起来是下标"


# ---------------------------------------------------------------- 多选计数

def test_multi_select_count_tracks_the_selection(app, shell, long_dir):
    """选 3 个 → 取消 1 个 → 计数必须跟着变。

    计数从页面状态来、选中在控件里,两者不一致时截图上完全看不出来
    (用户报过"下载所选一直显示 2")。
    """
    from PySide6.QtCore import QItemSelectionModel

    browse = shell.page("browse")
    browse._set_multi(True)
    _pump(app, 0.1)
    table = browse.table
    sm = table.selectionModel()
    rows = [table.model().index(i, 0) for i in range(3)]
    for idx in rows:
        sm.select(idx, QItemSelectionModel.Select | QItemSelectionModel.Rows)
    _pump(app, 0.1)
    assert len(table.checked_keys()) == 3
    assert "3" in browse.dl_sel_btn.text(), \
        f"选了 3 个,按钮却写着 {browse.dl_sel_btn.text()!r}"
    assert browse.dl_sel_btn.isEnabled()

    sm.select(rows[0], QItemSelectionModel.Deselect | QItemSelectionModel.Rows)
    _pump(app, 0.1)
    assert len(table.checked_keys()) == 2
    assert "2" in browse.dl_sel_btn.text(), \
        f"取消一个后按钮还写着 {browse.dl_sel_btn.text()!r}"
    browse._set_multi(False)
    _pump(app, 0.1)
    assert not browse.dl_sel_btn.isEnabled(), "退出勾选模式后按钮还是可用的"


def test_single_select_does_not_clear_siblings_in_multi_mode(app, shell, long_dir):
    """多选模式下**不能**用"单个当前项"去表达选中。

    ``setCurrentIndex``/``selectRow`` 会把其他选中全清掉(勾一个掉一片)——
    所以 ``select_key`` 只在单选模式下调。
    """
    from PySide6.QtCore import QItemSelectionModel

    browse = shell.page("browse")
    browse._set_multi(True)
    _pump(app, 0.1)
    table = browse.table
    sm = table.selectionModel()
    for i in range(2):
        sm.select(table.model().index(i, 0),
                  QItemSelectionModel.Select | QItemSelectionModel.Rows)
    _pump(app, 0.1)
    before = len(table.checked_keys())
    assert before == 2
    # 这一步在旧写法下会把选中清成 1 个
    table.select_key(table.keys()[5])
    _pump(app, 0.1)
    browse._set_multi(False)
    _pump(app, 0.1)
    assert before == 2


# ---------------------------------------------------------------- 世代计数器

def test_bg_drops_stale_results(app):
    """世代计数器的**机制**本身:慢的先发起、快的后发起,只有后者算数。

    这条是确定性的(靠 sleep 强制先后),不依赖真实的目录大小。
    """
    import threading
    import time

    from astro_smb_qt.workers import Bg

    bg = Bg()
    got: list[str] = []
    started = threading.Event()

    gen_a = bg.bump()

    def slow():
        started.set()
        time.sleep(0.4)
        return "A"

    bg.run(slow, gen=gen_a, on_done=got.append)
    assert started.wait(5.0), "慢任务没起来"
    gen_b = bg.bump()                     # 用户又点了一下
    bg.run(lambda: "B", gen=gen_b, on_done=got.append)

    assert _pump_until(app, lambda: "B" in got, 5.0), "新任务的结果没回来"
    _pump(app, 0.8)                       # 等慢的那个也跑完
    assert got == ["B"], f"迟到的结果没有被丢弃: {got}"


def test_late_results_are_discarded(app, shell):
    """快速连点几个目录,最后必须停在**最后点的那个**。

    用户点得比网络快是常态;没有世代计数器就会"进了 B 目录却显示 A 目录的内容"。
    """
    browse = shell.page("browse")
    share = shell.shares[0]
    browse.entries = []
    for sub in ("Autorun", "log", "Plan", "Preview"):
        browse.open_path(share, sub)     # 不等,连着点
    last = "Preview"
    assert _pump_until(app, lambda: bool(browse.entries), 25.0), \
        "快速连点后一个结果都没回来"
    _pump(app, 1.0)                      # 把所有在途的都放完
    assert browse.path == last
    names = {e.name for e in browse.entries}
    listed = {p.name for p in (DEVICE / last).iterdir()}
    assert names == listed, (
        f"显示的是 {browse.path} 的内容,但条目对不上 —— 迟到的结果覆盖了新状态")


def test_generation_is_per_page(shell):
    """世代计数器**每页各一个**。

    共用一个的话,浏览页换目录会把导星页正在解析的日志一并作废。
    """
    a = shell.page("browse").bg
    b = shell.page("guiding").bg
    assert a is not b
    before = b.generation
    a.bump()
    assert b.generation == before


# ---------------------------------------------------------------- 写操作的错误通道

def test_write_failures_surface_to_the_user(app, shell):
    """写操作失败必须**显示出来**。

    静默吞掉就等于用户按了删除、什么都没发生、还以为成功了。这里用一个
    根本不存在的路径去触发失败。
    """
    browse = shell.page("browse")
    # **先等目录列完再触发写操作。** 不等的话在途的那次列目录会在错误之后回来,
    # 把状态行覆盖掉 —— 状态行是"最近一次操作"的位置,本来就会被后来的操作刷。
    assert _goto(app, browse, shell.shares[0], ""), "列共享根目录失败"
    browse.status_label.setText("")
    shell.clear_notice()
    browse._write(lambda c: c.rmdir(shell.shares[0], "根本不存在的目录_xyz"), "删除")
    assert _pump_until(app, lambda: "失败" in browse.status_label.text(), 20.0), \
        "写操作失败了,但状态行一个字都没有"
    # 提示条是**持久**通道:状态行可能被下一次列目录盖掉,横幅不会
    assert "失败" in shell.notice_text(), \
        f"写操作失败了,但顶部提示条是 {shell.notice_text()!r}"


# ---------------------------------------------------------------- 传输

def test_transfers_show_real_names_and_finish(app, shell):
    """排几个真任务:名字要对、要跑完、底部队列条要跟着变。

    两条真机抓到的缺陷都在这里钉住:

    1. **``views.transfers.row_model`` 读的是 ``job.name``,而
       ``TransferJob`` 上那个字段叫 ``label``** —— 每一行都变成"(未命名)"。
    2. **``TransferManager.stats()`` 返回的是任务列表,不是计数** ——
       当成计数用会 TypeError,而那一下会把整条 250ms 刷新链带走:
       底部条永远"空闲"、传输页永远停在"0 B / 进行中"。
    """
    page = shell.page("transfers")
    shell.transfers.clear_finished()
    page.demo_queue(2)
    assert _pump_until(app, lambda: len(list(shell.transfers.jobs)) >= 2, 30.0), \
        "演示任务没排进队列"
    assert _pump_until(app, lambda: all(j.finished for j in shell.transfers.jobs),
                       60.0), "任务没跑完"
    _pump(app, 0.6)
    page.refresh()

    jobs = list(shell.transfers.jobs)
    assert all(j.done == j.total > 0 for j in jobs), "字节数对不上"

    # 传输页现在按**文件夹分组**,而「已完成」那一档默认是**折叠**的
    # (老 UI 同款)—— 折叠的组不建行,所以这里先展开再看。
    for (key, name) in list({(k, str(r.get("group") or ""))
                             for k in ("run", "queue", "done")
                             for r in []} or set()):
        page.toggle_group(key, name)
    for job in jobs:
        g = str(getattr(job, "group", "") or "")
        if g:
            page._open[("done", g)] = True
    page._layout_key = None
    page.refresh()

    # **断言页面真正画出来的那个文本**,不是某个纯函数的返回值 ——
    # 后者即使把页面里的调用整个删掉也照样绿(这条测试第一版就是那样)。
    rendered = {jid: row._name.text() for jid, row in page._rows.items()}
    assert rendered, "传输页一行都没渲染"
    labels = {str(j.job_id): j.label for j in jobs}
    for jid, text in rendered.items():
        assert "未命名" not in text, f"任务 {jid} 渲染成了 {text!r}"
        assert labels.get(jid, "") in text, \
            f"任务 {jid} 渲染的是 {text!r},文件名应该是 {labels.get(jid)!r}"

    # 底部队列条:跑完之后不能还写着"空闲"
    assert "已完成" in shell.queue_label.text(), \
        f"任务跑完了,底部队列条却写着 {shell.queue_label.text()!r}"


def test_queue_bar_survives_a_broken_tick(app, shell, monkeypatch):
    """一拍里抛异常**不能**把整条刷新链带走。

    没有这层兜底,任何一次 ``stats()`` 读错形状都会让传输页从此不再更新,
    而界面上什么都不说。
    """
    def boom():
        raise RuntimeError("故意炸的")

    monkeypatch.setattr(shell.transfers, "stats", boom)
    shell._jobs_dirty.set()
    fired: list[int] = []
    shell.transfers_changed.connect(lambda: fired.append(1))
    shell._on_tick()                 # 不许抛出来
    assert fired, "一拍炸了之后 transfers_changed 没有照常发出"


# ---------------------------------------------------------------- 红光模式

def test_red_mode_switches_everything(app, shell):
    """切红光后所有 QSS 规则要重算。

    Qt 的样式是在 polish 时算好存起来的:只换 QApplication 的样式表,
    已经 polish 过的控件不会自动重算带动态属性的规则 —— 症状是
    "大部分变了、少数几个还是原色"。
    """
    from astro_smb_qt import theme

    shell.set_theme_mode(theme.MODE_RED)
    _pump(app, 0.2)
    try:
        assert theme.current_mode() == theme.MODE_RED
        css = app.styleSheet()
        assert theme.RED.SURFACE.lower() in css.lower()
        assert theme.NORMAL.ACCENT.lower() not in css.lower(), \
            "样式表里还留着常规配色的强调色"
    finally:
        shell.set_theme_mode(theme.MODE_NORMAL)
        _pump(app, 0.2)
