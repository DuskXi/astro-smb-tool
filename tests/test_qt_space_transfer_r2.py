"""空间分析的取消竞态 + 传输的「元数据」阶段。两条都由独立验收挖出来。

**空间页的取消是假的。** `_stop_scan()` 只 `cancel()` 了 token,没像
`rescan()` 那样 `bg.bump()` 作废世代。本地磁盘扫一个目录只要几十毫秒 ——
取消信号还没被工作线程看见,结果就已经算完了,`on_done` 按世代校验时
**不算过期**,照常被接受。验收员做了 5ms 一次的采样:点停止后
「扫描已停止」只显示约 76ms 就被完整占用图盖回去,跟没点过一样。

**传输的「元数据」阶段是死代码。** `PH_META` 定义了、老 UI 的监控页
也给它配了蓝色分支,但**全仓库没有一处给 `job.phase` 赋过它**;
实测 40+ 个任务的阶段流转只有 排队/连接中/传输/完成 四种。
建连接、开文件、算分块这些"还没搬字节"的时间被算进了「传输」。
"""
from __future__ import annotations

import ast
import tempfile
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPACE = ROOT / "astro_smb_qt" / "pages" / "space.py"
MIRROR = ROOT / ".tmp" / "device" / "EMMC Images"


def _fn_src(path: Path, name: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            body = node.body[1:] if (node.body
                                     and isinstance(node.body[0], ast.Expr)
                                     and isinstance(node.body[0].value,
                                                    ast.Constant)
                                     ) else node.body
            return "\n".join(ast.unparse(n) for n in body)
    raise AssertionError(f"{path.name} 里没有 {name}")


class TestStopScanReallyStops:

    def test_it_invalidates_the_generation(self):
        """**光 cancel 不够。** 世代不作废的话,已经算完的那份结果照样会
        被 `on_done` 收下,把「扫描已停止」盖回成完整占用图。"""
        assert "self.bg.bump()" in _fn_src(SPACE, "_stop_scan")

    def test_rescan_does_the_same(self):
        """两条路都要 bump —— 这条是防止有人"顺手"把 rescan 那句删了。"""
        assert "self.bg.bump()" in _fn_src(SPACE, "rescan")

    def test_a_late_result_is_dropped(self):
        """**行为验证。** 模拟"取消之后结果才回来":`stale()` 必须说它过期。"""
        pytest.importorskip("PySide6")
        from PySide6.QtWidgets import QApplication

        from astro_smb_qt import theme
        from astro_smb_qt.shell import Shell

        app = QApplication.instance() or QApplication([])
        theme.apply(app)
        page = Shell().page("space")
        gen = page.bg.bump()          # 假装扫描开始时拿到的世代
        assert not page.bg.stale(gen)
        page._cancel = None
        page._stop_scan()
        assert page.bg.stale(gen), "取消之后旧世代还被当成新鲜的"


class TestMetadataPhaseIsReachable:
    """「元数据」不能是死代码 —— 监控页为它配了色。"""

    def test_the_constant_is_actually_assigned(self):
        src = (ROOT / "astro_smb_app" / "transfers.py").read_text(
            encoding="utf-8")
        assert "job.phase = PH_META" in src, "PH_META 还是没人赋值"

    @pytest.mark.skipif(not MIRROR.is_dir(), reason="没有离线镜像")
    def test_a_real_download_passes_through_it(self):
        """**真跑一次下载**,看阶段序列。

        只断言"源码里有 `= PH_META`"挡不住把它写在一条走不到的分支里 ——
        这条 bug 本来就是"定义了没人用",再来一次"赋值了走不到"是同一种病。
        """
        from astro_smb.backend import guess_kind, make_backend
        from astro_smb_app.transfers import TransferManager

        src = None
        for p in (MIRROR / "Plan" / "Light").rglob("*.fit"):
            src = p
            break
        if src is None:
            pytest.skip("镜像里没有 .fit")
        rel = str(src.relative_to(MIRROR)).replace("/", "\\")
        host = str(MIRROR)
        seen: list[str] = []

        with tempfile.TemporaryDirectory() as d:
            tm = TransferManager(
                lambda: make_backend(guess_kind(host), host=host),
                lambda job: seen.append(job.phase), max_workers=1)
            job = tm.submit_download("EMMC Images", rel, Path(d) / "a.fit",
                                     "a.fit", src.stat().st_size)
            for _ in range(1200):
                if job.status in ("完成", "失败", "已取消"):
                    break
                time.sleep(0.05)
            assert job.status == "完成", job.status

        order: list[str] = []
        for p in seen:
            if not order or order[-1] != p:
                order.append(p)
        assert "元数据" in order, f"阶段序列里没有元数据: {order}"
        assert order.index("元数据") < order.index("传输"), order

    def test_connect_happens_inside_the_metadata_phase(self):
        """建连接是"还没搬字节"的活 —— 它要是仍在「传输」里,
        速度会看着莫名其妙地低,而阶段标签也就没了意义。"""
        src = (ROOT / "astro_smb_app" / "transfers.py").read_text(
            encoding="utf-8")
        at = src.index("client.connect()\n                    # 连上了")
        assert "job.phase = PH_TRANSFER" in src[at:at + 200]

    def test_parallel_switches_when_the_plan_is_known(self):
        """分块并发要先各开一条连接、算分块方案 —— 那一段仍是元数据。"""
        src = (ROOT / "astro_smb_app" / "transfers.py").read_text(
            encoding="utf-8")
        at = src.index("def _on_plan")
        body = src[at:src.index("\n    def _on_block", at)]
        assert "job.phase = PH_TRANSFER" in body
