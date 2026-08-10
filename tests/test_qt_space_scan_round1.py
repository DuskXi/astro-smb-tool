"""空间分析 + 扫描设备 第一轮独立验收判"不过"的那批。

四条最要紧的,全是"不报错、只是不对":

* **treemap 标签压在相邻块上**:画布**早就实现**了 `maxw`(`fm.elidedText`),
  只是页面没传;顺带 `\\n` 也没渲染,`Bias` 和 `1.46 GB` 连成 `Bias1.46 GB`。
  又一次「图元支持 ≠ 页面用了」。
* **双击文件把页面锁死**:拿文件路径去 `dir_tree` → "根目录枚举失败",
  而失败路径不跑 `_render()`,「上级」保持禁用 —— 出不来了。
* **双向联动两个方向都没反馈**:点树行不重画;点块之后 `_render()` 重建
  整张表,把刚设的选中当场抹掉。
* **停止扫描之后界面自称"扫描完成"**:只探了 6/254,却报"共发现 1 台",
  读起来就是"这个网段只有一台"。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytest.importorskip("PySide6")

ROOT = Path(__file__).resolve().parents[1]
QT = ROOT / "astro_smb_qt"
SPACE = QT / "pages" / "space.py"
SCAN = QT / "pages" / "scan.py"


@pytest.fixture(scope="module")
def qt_app():
    from PySide6.QtWidgets import QApplication

    from astro_smb_qt import theme

    inst = QApplication.instance() or QApplication([])
    theme.apply(inst)
    return inst


def _fn(path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name} 里没有 {name}")


def _src(path: Path, name: str) -> str:
    node = _fn(path, name)
    body = node.body[1:] if (node.body and isinstance(node.body[0], ast.Expr)
                             and isinstance(node.body[0].value, ast.Constant)
                             ) else node.body
    return "\n".join(ast.unparse(n) for n in body)


class _Node:
    """最小的 TreeNode 替身。"""

    def __init__(self, name, path, size, is_dir=True, children=(), files=0):
        self.name = name
        self.path = path
        self.size = size
        self.is_dir = is_dir
        self.children = list(children)
        self.file_count = files


# ================================================================ 空间分析

class TestTreemapLabels:
    """6.1:标签压块 + 换行符没渲染。"""

    def test_page_passes_maxw(self):
        src = _src(SPACE, "_render")
        assert "'maxw': maxw" in src, (
            "`maxw` 又被丢掉了 —— 文件名会整条画出去压住相邻块")
        assert "_maxw" not in src, "还在用下划线前缀把它标成没用的"

    def test_canvas_elides_at_maxw(self, qt_app):
        src = (QT / "widgets.py").read_text(encoding="utf-8")
        at = src.index("def text_at")
        body = src[at:src.index("\n\nclass ", at)]
        assert "elidedText" in body

    def test_canvas_renders_newlines(self, qt_app):
        """`drawText(QPointF, str)` 不处理换行符 —— 叶级标签是两行。"""
        src = (QT / "widgets.py").read_text(encoding="utf-8")
        at = src.index("def text_at")
        body = src[at:src.index("\n\nclass ", at)]
        assert "splitlines()" in body, "换行符没被处理,名字和大小会连成一串"

    def test_two_lines_really_paint_lower(self, qt_app):
        """**行为验证**:第二行必须画在第一行下面,不是同一个 y。"""
        from PySide6.QtGui import QPainter, QPixmap

        from astro_smb_qt import widgets as W

        canvas = W.Canvas(200, 60)
        pm = QPixmap(200, 60)
        pm.fill()
        p = QPainter(pm)
        drawn = []
        real = p.drawText

        def spy(pt, text):
            drawn.append((pt.y(), text))
            return real(pt, text)

        p.drawText = spy
        canvas.text_at(p, 4.0, 4.0, "Bias\n1.46 GB")
        p.end()
        assert len(drawn) == 2, f"没有画成两行:{drawn}"
        assert drawn[1][0] > drawn[0][0], f"第二行没往下走:{drawn}"


class TestTooltipUsesTheSharedLayer:
    """6.3:注释写着"不自己拼",而底下就是自己拼的。"""

    def test_it_calls_node_tip(self):
        src = _src(SPACE, "_label_for")
        assert "sv.node_tip(" in src, "又自己拼了一遍 —— 会丢掉类别与文件数"

    def test_directory_tip_has_the_file_count(self):
        from astro_smb_app.views import space as sv

        tip = sv.node_tip(_Node("Autorun", "Autorun", 3_400_000_000, files=130))
        assert "130 文件" in tip, tip
        assert "目录" in tip


class TestTwoWayLinkage:
    """6.4:两个方向都是"点了没反应"。"""

    def test_picking_a_row_repaints(self):
        src = _src(SPACE, "_pick_row")
        assert "_repaint_marks()" in src, (
            "点树行不重画 —— 要等鼠标随便晃一下触发 hover 才看得到高亮")

    def test_picking_a_block_selects_after_repaint(self):
        """**顺序要紧**:`_repaint_marks()` 会整份 `_render()`,
        而那一步重建整张表 —— 先 `select_key` 的话选中当场被抹掉。"""
        src = _src(SPACE, "_pick_block")
        assert src.index("_repaint_marks()") < src.index("tree.select_key("), (
            "先选中再重画 —— 重建表格会把选中抹掉,树里一行都不会亮")


class TestFileIsNotADeadEnd:
    """清单外最严重的一条:双击文件 → 错误页,而且出不来。"""

    def test_drill_refuses_files(self):
        src = _src(SPACE, "_drill")
        assert "is_dir" in src, (
            "文件也拿去 dir_tree —— 会报「根目录枚举失败」,而失败路径不跑 "
            "`_render()`,「上级」保持禁用,整页卡死在错误页")

    def test_it_says_why(self, qt_app):
        src = _src(SPACE, "_drill")
        assert "notice(" in src, "默默不动比报错更让人以为是坏了"

    def test_behaviour(self, qt_app):
        from astro_smb_qt.shell import Shell

        page = Shell().page("space")
        page.root = _Node("root", "", 100, children=[
            _Node("a.fit", "a.fit", 50, is_dir=False)])
        page.rescan = lambda: (_ for _ in ()).throw(AssertionError("不该重扫"))
        page._drill("a.fit")
        assert page.crumbs == [], "文件也进了面包屑"


class TestScanCancelAndProgress:
    """6.6:全页没有取消入口,忙态一行不动的字。"""

    def test_button_toggles(self):
        src = _src(SPACE, "_toggle_scan")
        assert "self._busy" in src and "_stop_scan()" in src

    def test_stop_cancels_the_token(self):
        src = _src(SPACE, "_stop_scan")
        assert ".cancel()" in src

    def test_scan_reports_progress(self):
        """**绑到 `dir_tree` 那次调用上。**

        只查 `"on_progress" in src` 是不够的:同一个函数里
        `self.bg.run(..., on_progress=progress)` 也带这个词 —— 把
        `dir_tree` 的那个删掉照样绿(反向验证里这条活了)。
        """
        node = _fn(SPACE, "rescan")
        calls = [n for n in ast.walk(node)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "dir_tree"]
        assert calls, "rescan 里没有 dir_tree"
        kw = {k.arg for k in calls[0].keywords}
        assert "on_progress" in kw, (
            "`dir_tree` 支持 `on_progress`,不传的话忙态几十秒里一个数字都不动")
        assert "cancel" in kw, "连取消都没传"
        assert "已扫" in _src(SPACE, "rescan")

    def test_button_resets_on_every_exit(self):
        """成功 / 失败 / 停止 三条路都要把按钮改回来,
        否则会卡在「停止」上,再点就是取消一个已经结束的扫描。"""
        # `ast.unparse` 把字符串常量统一成单引号 —— 照源码写双引号会落空
        for name in ("_apply", "_fail", "_stop_scan"):
            assert "setText(_('扫描此目录'))" in _src(SPACE, name), (
                f"{name} 没有复位按钮")


class TestStatusLine:
    def test_it_shows_the_file_count(self):
        """**文件数原来整页没有第二处能看到。**"""
        src = _src(SPACE, "_render")
        assert "file_count" in src and "文件" in src

    def test_it_shows_the_current_root(self):
        src = _src(SPACE, "_render")
        assert "当前根" in src

    def test_percent_is_rounded_not_floored(self):
        """向下取整给 77/20/1,老 UI 四舍五入给 78/21/2 —— 看起来像两套数据。"""
        src = _src(SPACE, "_render")
        assert "round(c.size * 100 / total)" in src
        assert "// total" not in src


# ================================================================ 扫描设备

class TestSubnetValidation:
    def test_shared_helper_exists(self):
        from astro_smb_app.views import scan as sv

        assert sv.valid_subnet("192.0.2.") == "192.0.2"
        assert sv.valid_subnet(" 10.0.0 ") == "10.0.0"

    def test_it_rejects_garbage(self):
        from astro_smb_app.views import scan as sv

        for bad in ("nope", "", "1.2", "1.2.3.4", "999.1.1", "a.b.c"):
            assert sv.valid_subnet(bad) == "", bad

    def test_the_page_validates_and_says_so(self):
        """校验换成了 `netscan.parse_target` —— 它连 CIDR 一起认
        (`192.0.2.0/22`),而 `valid_subnet` 只认三段前缀。

        **行为覆盖在 `test_scan_subnet_picker.py`**:那边真建一个页面、
        真填 `nope`、真点开始,然后看有没有那句提示。这里只钉住"还在校验"。
        """
        src = _src(SCAN, "toggle")
        assert "parse_target(" in src, "只判空 —— 输入 `nope` 毫无反馈"
        assert "网段认不出来" in src


class TestStopWording:
    """清单外:按了停止,界面说"扫描完成"。"""

    def test_stop_sets_the_flag(self):
        """停止那几步搬进 `_stop()` 了 —— `toggle` 现在只是转调它。

        搬家的原因是那个共享取消标志的 bug(换网段就卡),
        见 `test_scan_subnet_picker.TestSwitchingSubnetsDoesNotWedge`。
        """
        src = _src(SCAN, "_stop")
        assert "self._stopped = True" in src

    def test_a_new_scan_clears_it(self):
        src = _src(SCAN, "toggle")
        assert "self._stopped = False" in src, (
            "上一轮停过,这一轮跑完了还说「已停止」")

    def test_done_wording_depends_on_it(self):
        src = _src(SCAN, "_on_done")
        assert "已停止" in src and "扫描完成" in src
        assert "stopped" in src

    def test_empty_state_does_not_claim_a_full_sweep(self):
        """停在 6/254 却说「254 个地址里没有一台」—— 那是假的。"""
        src = _src(SCAN, "_on_done")
        at = src.index("已停止")
        assert "还没探完" in src[at:] or "还没探完" in src

    def test_behaviour(self, qt_app):
        from astro_smb_qt.shell import Shell

        page = Shell().page("scan")
        page._stopped = True
        page._on_done([])
        assert "已停止" in page.progress.text(), page.progress.text()
        page._stopped = False
        page._on_done([])
        assert "扫描完成" in page.progress.text(), page.progress.text()


class TestDeviceSubline:
    """清单外:少了"可能是 PC/NAS,非 ASIAIR"那句判读。"""

    def test_non_asiair_says_so(self):
        from astro_smb_app.views import scan as sv

        row = sv.device_row("192.0.2.11", "NAS", ["bt"], 3.0, "DUSK-N100")
        assert "非 ASIAIR" in row["sub"], row["sub"]

    def test_asiair_says_so_too(self):
        from astro_smb_app.views import scan as sv

        row = sv.device_row("192.0.2.227", "ASIAIR",
                            ["EMMC Images"], 5.0, "")
        assert row["asiair"]
        assert "疑似 ASIAIR" in row["sub"], row["sub"]


class TestConnectionCard:
    """8.5:卡在,但信息少了一半。"""

    def test_shell_counts_heartbeats(self):
        """**要查的是"真的加一",不是"字典里有这个键"。**

        只查 `'"beats"' in src` 的话,把自增那一行换成 `pass` 照样绿 ——
        键还在,只是永远停在 0(反向验证里这条活了)。
        """
        src = (QT / "shell.py").read_text(encoding="utf-8")
        assert 'self.conn["beats"] = int(self.conn.get("beats", 0)) + 1' in src, (
            "心跳次数没有真的累加")
        assert 'self.conn["last_beat"] = time.time()' in src, (
            "最近心跳时刻没有更新 —— 那就分不出「现在在线」和「刚才在线过」")

    def test_card_shows_them(self, qt_app):
        import time

        from astro_smb_qt.shell import Shell

        sh = Shell()
        page = sh.page("scan")
        sh.conn.update(host="192.0.2.227", kind="smb", rtt=4.0,
                       server_os="Samba 4.9.5", beats=50,
                       last_beat=time.time(), shares=3)
        page._render_conn()
        note = page.conn_note.text()
        assert "心跳 50 次" in note, note
        assert "最近" in note, note
        assert "Samba 4.9.5" in note, note

    def test_local_card_gets_an_explanation(self, qt_app):
        """本地卡不在局域网上,扫描对它没有意义 —— 老 UI 明说这一句。"""
        from astro_smb_qt.shell import Shell

        sh = Shell()
        page = sh.page("scan")
        sh.conn.update(host="E:/ASIAIR", kind="local", rtt=0.0, shares=1)
        page._render_conn()
        assert "本地卡" in page.conn_note.text(), page.conn_note.text()

    def test_online_wording_still_requires_a_real_echo(self):
        """**「在线」只能由心跳驱动。** 路由器会对整网段 445 假 ACK ——
        端口可达绝不能说成在线(这一页的招牌坑)。"""
        src = _src(SCAN, "_render_conn")
        at = src.index("在线")
        assert "rtt" in src[max(0, at - 120):at + 40], (
            "「在线」不是由 rtt(SMB ECHO 往返)驱动的")
        assert "端口可达" in src


class TestDiskCache:
    """清单外:老 UI 有两层缓存,Qt 每次进页/下钻/上级都真扫。

    真机 222 GB 每层几百次 listdir —— 这不是"慢一点",是每次点上级都
    等几十秒。共享层的 `dircache` 早就有 `get_tree`/`put_tree`。
    """

    def test_it_reads_the_cache(self):
        src = _src(SPACE, "rescan")
        assert "dircache.get_tree(" in src, "每次都真扫"

    def test_it_writes_the_cache(self):
        src = _src(SPACE, "rescan")
        assert "dircache.put_tree(" in src, "扫完不落盘,下次还是真扫"

    def test_a_hit_short_circuits_the_scan(self):
        """命中就直接用,不能"先查一遍再照样扫"。"""
        node = _fn(SPACE, "rescan")
        src = ast.unparse(node)
        assert src.index("dircache.get_tree(") < src.index("dir_tree("), (
            "先扫再查缓存 —— 那查它干什么")
        assert "if hit is not None:" in src

    def test_a_stale_hit_is_labelled(self, qt_app):
        """**必须说出来。** 一个静默的旧数字比慢一点糟得多 ——
        老 UI 那句「(本地索引 · 3 分钟前的统计)」就是干这个的。"""
        from astro_smb_qt.shell import Shell

        page = Shell().page("space")
        page.share = "EMMC Images"
        root = _Node("EMMC Images", "", 15_000_000_000,
                     children=[_Node("Plan", "Plan", 11_000_000_000, files=400)],
                     files=649)
        page._apply((root, 186.0))
        assert "本地索引" in page.status.text(), page.status.text()

    def test_a_fresh_scan_is_not_labelled(self, qt_app):
        from astro_smb_qt.shell import Shell

        page = Shell().page("space")
        page.share = "EMMC Images"
        root = _Node("EMMC Images", "", 15_000_000_000,
                     children=[_Node("Plan", "Plan", 11_000_000_000, files=400)],
                     files=649)
        page._apply((root, 0.0))
        assert "本地索引" not in page.status.text(), (
            "刚扫出来的结果被标成了缓存")


class TestCanvasFollowsTheWindow:
    """清单外:`OpsCanvas(w, h)` 内部是 `setFixedHeight` —— treemap
    永远 520px 高,窗口拉高下面空一大块。老 UI 跟着窗口重排。"""

    def test_height_is_not_fixed(self, qt_app):
        from astro_smb_qt.shell import Shell

        page = Shell().page("space")
        assert page.canvas.maximumHeight() > 1000, "高度还是写死的"

    def test_resize_retiles(self):
        src = _src(SPACE, "_build")
        assert "resized.connect(self._retile)" in src, (
            "尺寸变了不重排 —— 几何是按像素算的,图会停在旧尺寸上")

    def test_geometry_uses_the_real_size(self):
        """`sv.treemap` 必须收**实际**画布尺寸,不是那两个常量。"""
        src = _src(SPACE, "_render")
        assert "self.canvas.width()" in src and "self.canvas.height()" in src
        assert "float(MAP_W), float(MAP_H)" not in src
