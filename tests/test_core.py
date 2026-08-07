"""离线单元测试(不依赖设备):路径解析、FITS 头、工具函数。"""

import pytest

from astro_smb.client import normalize_remote_path, split_remote_path, SmbClientError
from astro_smb.fitshdr import BLOCK, CARD, parse_fits_header, header_read_hint
from astro_smb.util import human_size, parse_size, sanitize_local_name


class TestPaths:
    def test_split_basic(self):
        assert split_remote_path("EMMC Images/Autorun/Light") == ("EMMC Images", "Autorun\\Light")

    def test_split_share_only(self):
        assert split_remote_path("EMMC Images") == ("EMMC Images", "")
        assert split_remote_path("EMMC Images/") == ("EMMC Images", "")

    def test_split_backslash(self):
        assert split_remote_path("EMMC Images\\Autorun") == ("EMMC Images", "Autorun")

    def test_split_unc(self):
        assert split_remote_path(r"\\192.0.2.225\EMMC Images\Plan") == ("EMMC Images", "Plan")

    def test_split_smb_url(self):
        assert split_remote_path("smb://192.0.2.225/EMMC Images/Plan") == ("EMMC Images", "Plan")

    def test_split_chinese(self):
        assert split_remote_path("EMMC Images/中文目录/文件.fit") == ("EMMC Images", "中文目录\\文件.fit")

    def test_split_empty_raises(self):
        with pytest.raises(SmbClientError):
            split_remote_path("")

    def test_normalize(self):
        assert normalize_remote_path("a/b/../c/./d/") == "a\\c\\d"
        assert normalize_remote_path("") == ""
        assert normalize_remote_path("/") == ""
        assert normalize_remote_path("..\\..") == ""


class TestFits:
    @staticmethod
    def _make_header(cards: list[str], blocks: int = 1) -> bytes:
        raw = b"".join(c.encode("ascii").ljust(CARD) for c in cards)
        raw += b"END".ljust(CARD)
        raw = raw.ljust(BLOCK * blocks, b" ")
        return raw

    def test_parse_basic(self):
        data = self._make_header([
            "SIMPLE  =                    T / conforms",
            "BITPIX  =                   16",
            "NAXIS   =                    2",
            "NAXIS1  =                 6248",
            "NAXIS2  =                 4176",
            "EXPTIME =                 30.0 / seconds",
            "INSTRUME= 'ZWO ASI2600MC Pro'",
            "BZERO   =                32768",
        ])
        hdr = parse_fits_header(data)
        assert hdr.complete
        assert hdr.naxis == (6248, 4176)
        assert hdr.bitpix == 16
        assert hdr.get("INSTRUME") == "ZWO ASI2600MC Pro"
        assert hdr.get("EXPTIME") == "30.0"
        assert hdr.data_size() == 6248 * 4176 * 2
        assert hdr.header_bytes == BLOCK

    def test_quoted_with_slash(self):
        data = self._make_header(["SIMPLE  =                    T",
                                  "OBJECT  = 'M31 / andromeda' / target name"])
        hdr = parse_fits_header(data)
        assert hdr.complete
        # 引号内的斜杠属于值本身,引号外的才是注释
        assert hdr.get("OBJECT") == "M31 / andromeda"

    def test_not_fits(self):
        hdr = parse_fits_header(b"not a fits file" * 200)
        assert not hdr.cards
        assert not hdr.complete

    def test_read_hint(self):
        # 40 张卡片把 END 挤到第二个 2880 块,只给第一块时应要求继续读
        many = ["SIMPLE  =                    T"] + [
            f"KEY{i:04d} = {i:>20}" for i in range(40)
        ]
        full = self._make_header(many, blocks=2)
        partial = full[:BLOCK]
        assert header_read_hint(partial) > 0
        assert header_read_hint(full) == 0
        assert header_read_hint(b"JUNK" * 720) == 0


class TestUtil:
    def test_human_size(self):
        assert human_size(0) == "0 B"
        assert human_size(1023) == "1023 B"
        assert human_size(52191360) == "49.77 MB"

    def test_parse_size(self):
        assert parse_size("10M") == 10 << 20
        assert parse_size("1.5G") == int(1.5 * (1 << 30))
        assert parse_size("2048") == 2048
        assert parse_size("100k") == 100 << 10
        with pytest.raises(ValueError):
            parse_size("abc")

    def test_sanitize(self):
        assert sanitize_local_name('a<b>:c"d|e?f*g.txt') == "a_b__c_d_e_f_g.txt"
        assert sanitize_local_name("正常文件.fit") == "正常文件.fit"
        assert sanitize_local_name("trailing. ") == "trailing"


class TestCliDefaultHost:
    """CLI 的默认设备地址:`ASTRO_SMB_HOST` > 上次连成过的 > 空。

    **不许硬编码。** 这里曾经写死 `192.0.2.225`,而设备是 DHCP、那个地址早
    失效了 —— 于是不带 `-H` 的每一条命令都要等 15 秒超时才报错。GUI 那侧从
    一开始就是这条规则(docs/DEVELOPMENT.md §7.14:"硬编码的 IP 对新用户永远是错的");
    CLI 没跟上,是因为设备记录当时住在 GUI 包里。
    """

    def test_no_hardcoded_address_anywhere_in_the_cli(self):
        import re
        from pathlib import Path

        src = (Path(__file__).resolve().parents[1] / "astro_smb" / "cli.py"
               ).read_text(encoding="utf-8")
        # 注释里可以出现(讲这段历史),代码里不行
        code = "\n".join(line.split("#", 1)[0] for line in src.splitlines())
        found = re.findall(r'"(\d{1,3}(?:\.\d{1,3}){3})"', code)
        assert not found, f"CLI 里还硬编码着地址: {found}"

    def test_env_var_wins(self, monkeypatch):
        from astro_smb import cli

        monkeypatch.setenv("ASTRO_SMB_HOST", "192.0.2.9")
        assert cli.default_host() == "192.0.2.9"

    def test_falls_back_to_the_last_connected_device(self, monkeypatch):
        from astro_smb import cli, devices

        monkeypatch.delenv("ASTRO_SMB_HOST", raising=False)
        monkeypatch.setattr(devices, "last_host", lambda: "192.0.2.42")
        assert cli.default_host() == "192.0.2.42"

    def test_empty_when_nothing_is_known(self, monkeypatch):
        from astro_smb import cli, devices

        monkeypatch.delenv("ASTRO_SMB_HOST", raising=False)
        monkeypatch.setattr(devices, "last_host", lambda: None)
        assert cli.default_host() == ""

    def test_a_broken_device_store_does_not_break_the_cli(self, monkeypatch):
        """设备记录坏了就当没有 —— 不该让每条命令都崩。"""
        from astro_smb import cli, devices

        monkeypatch.delenv("ASTRO_SMB_HOST", raising=False)

        def boom():
            raise OSError("盘挂了")

        monkeypatch.setattr(devices, "last_host", boom)
        assert cli.default_host() == ""

    def test_it_discovers_instead_of_guessing(self, monkeypatch, capsys):
        """不知道连哪台时**去局域网找**,而不是猜一个地址、也不是让人自己填。

        原来这条断言的是"给人话提示然后退出 2"。**那本身就是缺陷**:
        设备是 DHCP 的,而"你自己用 -H 填"对第一次装的人等于没有默认行为。
        现在扫本网段,恰好一台疑似 ASIAIR 就用它。
        """
        from astro_smb import cli, devices, netscan

        monkeypatch.delenv("ASTRO_SMB_HOST", raising=False)
        monkeypatch.setattr(devices, "last_host", lambda: None)
        monkeypatch.setattr(
            netscan, "discover_all",
            lambda *a, **k: [netscan.Device(ip="192.0.2.7", name="ASIAIR",
                                            shares=["EMMC Images"])])
        used: list[str] = []
        monkeypatch.setattr(cli, "cmd_info", lambda args: used.append(args.host) or 0)
        rc = cli.main(["info"])
        assert rc == 0 and used == ["192.0.2.7"], (rc, used)
        assert "192.0.2.7" in capsys.readouterr().err

    def test_it_refuses_to_choose_between_two(self, monkeypatch, capsys):
        """**两台疑似 ASIAIR 时不替人选。** 选错了他操作的是别人的片子。"""
        from astro_smb import cli, devices, netscan

        monkeypatch.delenv("ASTRO_SMB_HOST", raising=False)
        monkeypatch.setattr(devices, "last_host", lambda: None)
        monkeypatch.setattr(
            netscan, "discover_all",
            lambda *a, **k: [
                netscan.Device(ip="192.0.2.7", shares=["EMMC Images"]),
                netscan.Device(ip="192.0.2.8", shares=["TF Images"])])
        assert cli.main(["info"]) == 2
        err = capsys.readouterr().err
        assert "192.0.2.7" in err and "192.0.2.8" in err and "-H" in err

    def test_nothing_found_says_so(self, monkeypatch, capsys):
        from astro_smb import cli, devices, netscan

        monkeypatch.delenv("ASTRO_SMB_HOST", raising=False)
        monkeypatch.setattr(devices, "last_host", lambda: None)
        monkeypatch.setattr(netscan, "discover_all", lambda *a, **k: [])
        assert cli.main(["info"]) == 2
        assert "ASTRO_SMB_HOST" in capsys.readouterr().err
