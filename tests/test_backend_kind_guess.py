"""地址是本地目录还是主机名 —— 猜错的代价是一句谁也看不懂的 IDNA 报错。

真机上 `--host ".tmp/device/EMMC Images"` 走进了 SMB 分支,socket 在 IDNA
编码那一步抛 `UnicodeError`,界面上显示的是:

    连接 .tmp/device/EMMC Images 失败: 'idna' codec can't encode
    character '\\x2e' in position 0: label empty

两层都有问题:判据认不出**相对目录**,而错误又是生的 Python 异常。
这两条各自钉死。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from astro_smb.backend import guess_kind
from astro_smb.client import AstroSmbClient, SmbClientError


class TestGuessKind:
    """`guess_kind` 的判据 —— 收紧的那一格与不该动的那些格。"""

    def test_relative_directory_is_local(self, tmp_path: Path, monkeypatch):
        """**这条就是真机上炸掉的那一格。**"""
        (tmp_path / "device" / "EMMC Images").mkdir(parents=True)
        monkeypatch.chdir(tmp_path)
        assert guess_kind("device/EMMC Images") == "local"

    def test_relative_directory_with_leading_dot(self, tmp_path: Path,
                                                 monkeypatch):
        """真机传的正是 `.tmp/...` —— 开头那个点是 IDNA 报错里的 `\\x2e`。"""
        (tmp_path / ".tmp" / "device").mkdir(parents=True)
        monkeypatch.chdir(tmp_path)
        assert guess_kind(".tmp/device") == "local"

    def test_backslash_relative_directory_is_local(self, tmp_path: Path,
                                                   monkeypatch):
        r"""反斜杠**是不是分隔符,本来就看平台**。

        Windows 上 `dev\img` 是两级目录;POSIX 上它是一个**文件名里带反斜杠**
        的东西,`Path("dev\\img").is_dir()` 为假,于是判为 smb —— 两边都对。

        原来这里只写死了 Windows 那一半,于是 CI 的 ubuntu / macOS 两个 job
        一直是红的(那三个平台的 job 早就没人看了,见 ci.yml 里删掉的两个
        C# job)。断言跟着平台走,比跳过更有用:它把"两边行为不同"这件事
        本身钉住了。
        """
        (tmp_path / "dev" / "img").mkdir(parents=True)
        monkeypatch.chdir(tmp_path)
        want = "local" if os.name == "nt" else "smb"
        assert guess_kind("dev\\img") == want

    @pytest.mark.parametrize("host", [
        "192.0.2.227",
        "asiair.local",
        "ASIAIR",
        "192.0.2.1",
    ])
    def test_hostnames_stay_smb(self, host: str):
        assert guess_kind(host) == "smb"

    def test_hostname_wins_over_a_same_named_folder(self, tmp_path: Path,
                                                    monkeypatch):
        """当前目录里正好有个叫 `192.0.2.227` 的文件夹,**不能**改语义。

        判据要求"含路径分隔符"正是为了这个 —— 否则一个无关的文件夹就能
        把用户的设备地址悄悄劫持成本地磁盘。
        """
        (tmp_path / "192.0.2.227").mkdir()
        monkeypatch.chdir(tmp_path)
        assert guess_kind("192.0.2.227") == "smb"

    def test_missing_relative_path_stays_smb(self, tmp_path: Path, monkeypatch):
        """磁盘上不存在就不认 —— 只含斜杠不够。"""
        monkeypatch.chdir(tmp_path)
        assert guess_kind("nope/does-not-exist") == "smb"

    def test_absolute_and_drive_letters_still_local(self):
        """原来那条纯字符串判据不能被削弱。"""
        assert guess_kind("E:\\") == "local"
        assert guess_kind("/media/card") == "local"

    def test_unc_is_smb_and_does_not_touch_the_network(self, monkeypatch):
        """UNC 是**网络**路径,而且不能拿去 is_dir() 探盘(会真发请求)。"""
        called: list[str] = []

        real = Path.is_dir

        def spy(self):
            called.append(str(self))
            return real(self)

        monkeypatch.setattr(Path, "is_dir", spy)
        assert guess_kind("\\\\nas\\share") == "smb"
        assert guess_kind("//nas/share") == "smb"
        assert called == [], f"UNC 被拿去探盘了: {called}"

    def test_empty_is_smb(self):
        assert guess_kind("") == "smb"
        assert guess_kind("   ") == "smb"


class TestBadHostGivesAHumanError:
    """核心库对外只抛 `SmbClientError`(docs/DEVELOPMENT.md §11)—— IDNA 也不例外。

    `UnicodeError` 不是 `OSError` 的子类,所以 `_CONN_ERRORS` 接不住它。
    """

    def test_directory_as_host_is_wrapped(self):
        client = AstroSmbClient(host=".tmp/device/EMMC Images", timeout=1)
        with pytest.raises(SmbClientError) as ei:
            client.connect()
        msg = str(ei.value)
        assert "idna" not in msg.lower(), f"生的 Python 异常漏出去了: {msg}"
        assert "不是合法的主机名" in msg, msg

    def test_the_error_points_at_the_fix(self):
        """光说"错了"没用,要告诉用户该怎么办。"""
        client = AstroSmbClient(host="./some dir", timeout=1)
        with pytest.raises(SmbClientError) as ei:
            client.connect()
        assert "本地磁盘" in str(ei.value), str(ei.value)


class TestUiUsesTheSharedGuess:
    """界面里**任何地方**都不能再自己写一份判据。

    第一版只盯了 `connect_device` 内部,于是漏掉了它的**调用方** —— 启动那
    一处自己写了 ``"local" if ":" in host[:3] else "smb"``,显式传了 kind,
    `guess_kind` 压根没机会跑,真机上照样炸。所以这里扫**整个文件**。

    **这几条原来盯的是已删的 Uno 前端。转指 Qt 之后当场发现 Qt 有同一个
    缺陷** —— `_backend_spec` 写的是 ``"local" if _looks_local(host) else "smb"``,
    而 `_looks_local` 只认盘符与绝对路径,于是 ``.tmp/device/EMMC Images``
    这种相对路径被判成 smb,又一次被当成主机名。
    删掉的测试反倒暴露了活着那套的 bug。
    """

    @staticmethod
    def _src() -> str:
        return (Path(__file__).resolve().parents[1] / "astro_smb_qt"
                / "shell.py").read_text(encoding="utf-8")

    def test_the_spec_calls_guess_kind(self):
        src = self._src()
        at = src.index("def _backend_spec")
        end = src.index("\ndef ", at + 10)
        assert "guess_kind(host)" in src[at:end], (
            "界面没走共享判据 —— 相对目录会再一次被当成主机名")

    def test_nobody_in_the_ui_hand_rolls_a_kind(self):
        """整个文件里不许再出现"自己算出一个 kind"的三元表达式。

        判据要**精确到"条件表达式产出 local/smb"**,不能只看"这行里有 local
        又有 if" —— 那样会误伤 ``path=host if kind == "local" else ""``
        (那是**读** kind,不是造 kind)。
        """
        import re

        bad = re.compile(r'["\'](local|smb)["\']\s+if'
                         r'|else\s+["\'](local|smb)["\']')
        for n, line in enumerate(self._src().splitlines(), 1):
            code = line.split("#", 1)[0]
            assert not bad.search(code), (
                f"又手搓了一份 kind 判据,应当调 backend.guess_kind: "
                f"shell.py:{n}: {line.strip()}")

    def test_it_agrees_with_the_shared_judgement(self, tmp_path, monkeypatch):
        """**行为验证。** 只查源码挡不住"调了 guess_kind 但又覆盖掉结果"。"""
        pytest.importorskip("PySide6")
        from astro_smb.backend import guess_kind
        from astro_smb_qt.shell import _backend_spec

        # 第一个是**存在的**相对目录 —— 两边一致这件事在"目录不存在"时是
        # 白给的(都答 smb)。现造一个,才真的走到 local 那一支。
        (tmp_path / "device" / "EMMC Images").mkdir(parents=True)
        monkeypatch.chdir(tmp_path)
        for host in ("device/EMMC Images", "./cards/x", "C:/a/b",
                     r"D:\ASIAIR", "192.0.2.227", "asiair.local"):
            assert _backend_spec(host)[0] == guess_kind(host), host

    def test_a_relative_directory_is_local(self, tmp_path, monkeypatch):
        """这条是那次真机故障本身:相对路径被当成主机名,
        socket 在 IDNA 编码时炸。

        **目录得当场造一个。** 原来写死 `.tmp/device/EMMC Images` —— 那是
        作者机器上的离线镜像,`.tmp/` 进不了版本控制。判据本身要求"磁盘上
        确实是个目录"(见 `backend.guess_kind`),于是**换台机器 clone 下来
        这条就是红的**,而在这里它一直绿着。测试依赖的东西必须跟着测试走。
        """
        pytest.importorskip("PySide6")
        from astro_smb_qt.shell import _backend_spec

        (tmp_path / "device" / "EMMC Images").mkdir(parents=True)
        monkeypatch.chdir(tmp_path)
        assert _backend_spec("device/EMMC Images")[0] == "local"
