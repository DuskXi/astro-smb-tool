"""设备管理页 —— 用户列的第 12 条(设备不在线,**只做代码对照**)。

对照下来只有一条真缺口,但它是静默的:`views.devices.smb_card` 一直收
`rtt`/`fresh`/`snap_ts`,而 Qt 一个都没传 —— 于是除了当前连接那一台,
**每张卡的存活行永远是空的**。老 UI 对已记录的每一台都周期性探 TCP 445。

另外钉死一条措辞铁律:只能说「端口可达」,不能说「在线」。这个网段的路由器
会对整个网段的 445 SYN 秒回 ACK(docs/DEVELOPMENT.md §2),1ms 就"连上"的那 200 多台
全是假的;只有拿到过 SMB ECHO 往返的那一台才配说在线。
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "astro_smb_qt" / "pages" / "devices.py").read_text(
    encoding="utf-8")


def _body(name: str) -> str:
    at = SRC.index(f"def {name}")
    end = SRC.find("\n    def ", at + 10)
    return SRC[at:end if end > 0 else len(SRC)]


class TestAliveProbe:

    def test_page_probes_every_record(self):
        # 带上签名 —— `_probe_alive` 是 `_probe_alive_disabled` 的**前缀**,
        # 只查函数名的话改个名字就绕过去了(这个仓库为前缀撞车栽过一次)。
        assert "def _probe_alive(self)" in SRC, (
            "只有当前连接那台有存活行,其它卡永远是空的")

    def test_probe_runs_on_show(self):
        assert "_probe_alive()" in _body("on_show"), "进这一页不探活"

    def test_probe_is_off_the_gui_thread(self):
        body = _body("_probe_alive")
        assert "self.bg.run(" in body, (
            "十二台 × 3 秒超时跑在 GUI 线程上就是最长 36 秒的冻结")

    def test_probe_is_capped(self):
        body = _body("_probe_alive")
        assert "[:12]" in body, "不设上限就变成一次小型扫描了"

    def test_probe_has_a_timeout(self):
        body = _body("_probe_alive")
        assert "timeout=3" in body, "没超时的话一台不可达的设备能挂住整轮"

    def test_local_devices_are_not_probed(self):
        """本地磁盘没有 445 端口,探它纯属浪费三秒。"""
        body = _body("_probe_alive")
        assert "KIND_LOCAL" in body

    def test_results_reach_the_card_model(self):
        body = _body("_cards")
        for kw in ("rtt=", "fresh=", "snap_ts="):
            assert kw in body, f"探到的结果没喂给 smb_card 的 {kw}"

    def test_a_failed_probe_does_not_kill_the_page(self):
        body = _body("_probe_alive")
        assert "except Exception" in body, "一台探失败就整轮炸掉"


class TestWordingIsPortReachable:
    """路由器会对整网段假 ACK —— 说「在线」就是在骗人。"""

    def test_shared_layer_says_port_reachable(self):
        """非当前连接的设备只拿到 TCP 探测结果 —— 措辞只能是「端口可达」。"""
        from astro_smb_app.views import devices as dv

        text, tone = dv.smb_status("1.2.3.4", connected=False, hb=None,
                                   rtt={"1.2.3.4": 5.0})
        assert "端口可达" in text, text
        assert "在线" not in text, f"非当前连接的设备被说成「在线」: {text}"
        assert tone

    def test_unreachable_is_not_dressed_up(self):
        from astro_smb_app.views import devices as dv

        text, _tone = dv.smb_status("1.2.3.4", connected=False, hb=None,
                                    rtt={"1.2.3.4": None})
        assert "可达" not in text, text

    def test_page_does_not_hand_roll_the_wording(self):
        code = "\n".join(ln.split("#", 1)[0] for ln in SRC.splitlines())
        assert "在线" not in code, (
            "页面自己写了「在线」—— 措辞只能来自共享层")


class TestActionSetMatchesTheOldUi:
    """连接 / 打开 / 添加 / 忘记,四个都要在。"""

    @pytest.mark.parametrize("name", ["connect", "open", "add", "forget"])
    def test_action_wired(self, name: str):
        body = _body("_actions")
        assert f'"{name}"' in body, f"少了动作 {name}"

    def test_disabled_actions_keep_their_reason(self):
        """三元组是 `(可用?, 文案, 禁用原因)` —— 灰按钮不说原因等于耍人。"""
        body = _body("_actions")
        assert "tip=hint" in body


class TestManualAddIsOffTheGuiThread:
    def test_parse_runs_in_a_worker(self):
        body = _body("_add")
        assert "self.bg.run(" in body, (
            "`parse_manual_input` 会碰文件系统,对不可达的 UNC 路径 "
            "`os.path.isdir` 实测阻塞四十多秒")
