"""Astro SMB Tool 命令行客户端。

示例::

    astro-smb-tool shares
    astro-smb-tool ls "EMMC Images/Autorun" -l
    astro-smb-tool find "EMMC Images" "*.fit" --min-size 1M --limit 50
    astro-smb-tool get "EMMC Images/Autorun/Light/M31.fit" D:/astro/
    astro-smb-tool put D:/astro/plan.txt "EMMC Images/Plan"
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from astro_smb.client import (
    AstroSmbClient,
    SmbClientError,
    TransferCancelled,
    split_remote_path,
)
from astro_smb.fitshdr import parse_fits_header, header_read_hint, BLOCK
from astro_smb.util import format_duration, format_mtime, human_size, parse_size
from astro_smb.i18n import gettext as _

def default_host() -> str:
    """默认设备地址:`ASTRO_SMB_HOST` > 上次连成过的那台 > 空。

    **不再硬编码。** 这里曾经写死 `192.0.2.225` —— 那个地址早就失效了
    (设备是 DHCP,会变),于是不带 `-H` 的每一条命令都超时 15 秒才报错。
    GUI 那侧从一开始就是这条规则(见 docs/DEVELOPMENT.md §7.14:"硬编码的 IP 对新用户
    永远是错的");CLI 没跟上,是因为设备记录当时住在 GUI 包里、核心库不能
    反向依赖 —— B2 把它移到共享包、B19 又下沉到 `astro_smb.devices` 之后,
    那个理由不成立了。

    返回空串表示"不知道连哪台",由调用方给出人话提示而不是去连一个猜的地址。
    """
    env = (os.environ.get("ASTRO_SMB_HOST") or "").strip()
    if env:
        return env
    try:
        from astro_smb.devices import last_host

        return last_host() or ""
    except Exception:
        return ""


class _Progress:
    """stderr 单行进度条:百分比 / 速度 / ETA;非 TTY 时静默。"""

    def __init__(self, label: str, enabled: bool = True):
        self.label = label
        self.enabled = enabled and sys.stderr.isatty()
        self.start = time.monotonic()
        self.last_render = 0.0
        self._done = False

    def __call__(self, done: int, total: int) -> None:
        if not self.enabled or self._done:
            return
        now = time.monotonic()
        if done < total and now - self.last_render < 0.1:
            return
        self.last_render = now
        elapsed = max(now - self.start, 1e-6)
        speed = done / elapsed
        pct = (done / total * 100) if total else 100.0
        eta = (total - done) / speed if speed > 0 and total else 0
        bar_w = 24
        filled = int(bar_w * pct / 100)
        bar = "#" * filled + "-" * (bar_w - filled)
        label = self.label if len(self.label) <= 40 else "…" + self.label[-39:]
        sys.stderr.write(
            f"\r{label} [{bar}] {pct:5.1f}%  {human_size(done)}/{human_size(total)}"
            f"  {human_size(speed)}/s  ETA {format_duration(eta)}   "
        )
        sys.stderr.flush()
        if done >= total:
            self._done = True
            sys.stderr.write("\n")


def _client(args: argparse.Namespace) -> AstroSmbClient:
    return AstroSmbClient(
        host=args.host, port=args.port,
        username=args.user, password=args.password,
        timeout=args.timeout,
    )


# ---------- 子命令 ----------

def cmd_info(args) -> int:
    with _client(args) as c:
        info = c.server_info()
        print(_("主机       : {0}").format(info['host']))
        print(_("协议       : {0}").format(info['dialect']))
        print(_("服务器名   : {0}").format(info.get('server_name', '?')))
        print(_("系统       : {0}").format(info.get('server_os', '?')))
        print(_("共享:"))
        for s in c.list_shares(include_hidden=args.all):
            kind = _("磁盘") if s.is_disk else _("其他")
            print(f"  {s.name:<20} [{kind}] {s.remark}")
    return 0


def cmd_shares(args) -> int:
    with _client(args) as c:
        for s in c.list_shares(include_hidden=args.all):
            if args.all:
                print(f"{s.name:<20} type={s.type:#010x} {s.remark}")
            else:
                print(s.name)
    return 0


def _print_entry(e, long: bool) -> None:
    if long:
        size = "<DIR>" if e.is_dir else human_size(e.size)
        print(f"{e.attr_text()}  {size:>12}  {format_mtime(e.mtime)}  {e.name}")
    else:
        print(e.name + ("/" if e.is_dir else ""))


def cmd_ls(args) -> int:
    share, path = split_remote_path(args.remote)
    with _client(args) as c:
        entry = c.stat(share, path)
        if not entry.is_dir:
            _print_entry(entry, True)
            return 0
        entries = c.listdir(share, path)
        for e in entries:
            _print_entry(e, args.long)
        if args.long:
            ndir = sum(1 for e in entries if e.is_dir)
            total = sum(e.size for e in entries if not e.is_dir)
            print(_("共 {0} 个文件 ({1}), {ndir} 个目录").format(
                len(entries) - ndir, human_size(total), ndir=ndir))
    return 0


def cmd_tree(args) -> int:
    share, path = split_remote_path(args.remote)
    counts = [0, 0]  # 文件数, 目录数
    with _client(args) as c:
        print(f"{share}/{path.replace(chr(92), '/')}" if path else share)

        def rec(p: str, depth: int) -> None:
            indent = "  " * (depth + 1)
            try:
                entries = c.listdir(share, p)
            except SmbClientError as e:
                print(_("{indent}[跳过: {e}]").format(indent=indent, e=e), file=sys.stderr)
                return
            for e in entries:
                if e.is_dir:
                    print(f"{indent}{e.name}/")
                    counts[1] += 1
                    if args.depth is None or depth + 1 < args.depth:
                        rec(e.path, depth + 1)
                else:
                    suffix = f"  ({human_size(e.size)})" if args.size else ""
                    print(f"{indent}{e.name}{suffix}")
                    counts[0] += 1

        rec(path, 0)
        print(_('\n共 {0} 个文件, {1} 个目录').format(counts[0], counts[1]))
    return 0


def cmd_find(args) -> int:
    share, path = split_remote_path(args.remote)
    min_size = parse_size(args.min_size) if args.min_size else None
    max_size = parse_size(args.max_size) if args.max_size else None
    newer = None
    if args.newer_than:
        newer = datetime.fromisoformat(args.newer_than).timestamp()
    count = 0
    with _client(args) as c:
        for e in c.find(
            share, path, args.pattern,
            include_dirs=args.dirs, min_size=min_size, max_size=max_size,
            newer_than=newer, max_depth=args.depth, limit=args.limit,
            on_error=lambda p, err: print(_("[跳过 {p}: {err}]").format(
                p=p, err=err), file=sys.stderr),
        ):
            if args.long:
                size = "<DIR>" if e.is_dir else human_size(e.size)
                print(f"{size:>12}  {format_mtime(e.mtime)}  {e.display_path}")
            else:
                print(e.display_path)
            count += 1
    print(_('\n匹配 {count} 项').format(count=count), file=sys.stderr)
    return 0


def cmd_get(args) -> int:
    share, path = split_remote_path(args.remote)
    local = Path(args.local) if args.local else Path.cwd()
    with _client(args) as c:
        entry = c.stat(share, path)
        if entry.is_dir:
            print(_("递归下载目录 {display_path} -> {local}").format(
                display_path=entry.display_path, local=local), file=sys.stderr)
            bars: dict[str, _Progress] = {}

            def prog(name: str, done: int, total: int):
                bar = bars.get(name)
                if bar is None:
                    bar = bars[name] = _Progress(name)
                bar(done, total)

            n = c.download_dir(share, path, local, progress=prog, resume=args.resume)
            print(_("完成,共 {n} 个文件").format(n=n))
            return 0
        # 单文件:local 是目录则拼上文件名
        from astro_smb.util import sanitize_local_name
        if local.is_dir() or str(args.local or "").endswith(("/", "\\")) or args.local is None:
            target = local / sanitize_local_name(entry.name)
        else:
            target = local
        bar = _Progress(entry.name)
        jobs = getattr(args, "jobs", 1) or 1
        if jobs > 1 and entry.size >= (8 << 20) and not args.resume:
            # 文件内分块并发下载
            from astro_smb.parallel import ParallelDownloader
            import threading as _th
            done = [0]
            lock = _th.Lock()

            def on_delta(d):
                with lock:
                    done[0] += d
                bar(done[0], entry.size)

            pd = ParallelDownloader(
                lambda: AstroSmbClient(host=args.host, port=args.port,
                                        username=args.user, password=args.password,
                                        timeout=args.timeout),
                workers=jobs)
            res = pd.download(share, path, target, entry.size, on_progress=on_delta)
            print(_("已下载: {target} ({0}) [{n_chunks} 块 / {workers} 并发]").format(
                human_size(entry.size), target=target, n_chunks=res.n_chunks, workers=res.workers))
        else:
            c.download_file(share, path, target, progress=bar, resume=args.resume)
            print(_("已下载: {target} ({0})").format(human_size(entry.size), target=target))
    return 0


def cmd_put(args) -> int:
    share, rdir = split_remote_path(args.remote)
    with _client(args) as c:
        total_files = 0
        for local in args.local:
            lp = Path(local)
            if lp.is_dir():
                print(_("递归上传目录 {lp} -> {share}/{rdir}").format(
                    lp=lp, share=share, rdir=rdir), file=sys.stderr)
                bars: dict[str, _Progress] = {}

                def prog(name: str, done: int, total: int):
                    bar = bars.get(name)
                    if bar is None:
                        bar = bars[name] = _Progress(Path(name).name)
                    bar(done, total)

                total_files += c.upload_dir(lp, share, rdir, progress=prog)
            elif lp.is_file():
                name = args.rename if (args.rename and len(args.local) == 1) else lp.name
                rpath = f"{rdir}\\{name}" if rdir else name
                bar = _Progress(lp.name)
                c.upload_file(lp, share, rpath, progress=bar)
                total_files += 1
            else:
                raise SmbClientError(_("本地路径不存在: {lp}").format(lp=lp))
        print(_("完成,共上传 {total_files} 个文件").format(total_files=total_files))
    return 0


def cmd_cat(args) -> int:
    share, path = split_remote_path(args.remote)
    with _client(args) as c:
        data = c.read_bytes(share, path, offset=args.offset, size=args.bytes)
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
    return 0


def cmd_header(args) -> int:
    share, path = split_remote_path(args.remote)
    with _client(args) as c:
        probe = c.read_bytes(share, path, 0, BLOCK * 2)
        need = header_read_hint(probe)
        while need:
            more = c.read_bytes(share, path, len(probe), need)
            if not more:
                break  # 文件读尽仍无 END 卡,防止死循环
            probe += more
            need = header_read_hint(probe)
        hdr = parse_fits_header(probe)
        if not hdr.cards:
            print(_("不是 FITS 文件(头部无 SIMPLE 标记)"), file=sys.stderr)
            return 1
        if args.full:
            for key, value, comment in hdr.order:
                c_txt = f"  / {comment}" if comment else ""
                print(f"{key:<8} = {value}{c_txt}")
        else:
            for key, value in hdr.summary():
                print(f"{key:<8} = {value}")
        shape = hdr.naxis
        if shape:
            print(_("[图像 {0}, BITPIX={bitpix}, 数据区 {1}]").format(
                ' x '.join(map(str, shape)), human_size(hdr.data_size()), bitpix=hdr.bitpix), file=sys.stderr)
    return 0


def cmd_df(args) -> int:
    with _client(args) as c:
        shares = c.list_shares() if args.share is None else [
            s for s in c.list_shares() if s.name == args.share]
        if not shares:
            print(_("共享不存在: {share}").format(share=args.share), file=sys.stderr)
            return 2
        print(_("{0:<16} {1:>10} {2:>10} {3:>10}  占用").format(
            _("共享"), _("总量"), _("已用"), _("可用")))
        for s in shares:
            vi = c.volume_info(s.name)
            if vi is None:
                print(f"{s.name:<16} {_("不支持容量查询"):>10}")
                continue
            bar_w = 20
            filled = int(bar_w * vi.percent / 100)
            bar = "#" * filled + "-" * (bar_w - filled)
            print(f"{s.name:<16} {human_size(vi.total):>10} {human_size(vi.used):>10} "
                  f"{human_size(vi.free):>10}  [{bar}] {vi.percent:.1f}%")
    return 0


def cmd_du(args) -> int:
    share, path = split_remote_path(args.remote)
    with _client(args) as c:
        if args.children:
            print(_("分析 {share}/{0} 直接子级占用…").format(
                path.replace(chr(92), '/') if path else '', share=share),
                  file=sys.stderr)
            results = c.scan_children(share, path)
            grand = sum(sz for _, sz in results)
            for entry, size in results:
                pct = (size / grand * 100) if grand else 0
                mark = "/" if entry.is_dir else " "
                print(f"{human_size(size):>12} {pct:5.1f}%  {entry.name}{mark}")
            print(_('\n合计: {0}').format(human_size(grand)), file=sys.stderr)
        else:
            print(_("递归统计 {share}/{0}…").format(
                path.replace(chr(92), '/') if path else '', share=share),
                  file=sys.stderr)
            last = [0.0]

            def prog(nfiles, nbytes):
                import time as _t
                if _t.monotonic() - last[0] > 0.3:
                    last[0] = _t.monotonic()
                    print(_('\r  {nfiles} 文件 {0} …').format(
                        human_size(nbytes), nfiles=nfiles),
                          end="", file=sys.stderr)

            st = c.dir_stat(share, path, on_progress=prog)
            if sys.stderr.isatty():
                print("", file=sys.stderr)
            print(_("总大小: {0} ({total_size:,} 字节)").format(
                human_size(st.total_size), total_size=st.total_size))
            print(_("文件数: {file_count}").format(file_count=st.file_count))
            print(_("目录数: {dir_count}").format(dir_count=st.dir_count))
    return 0


def cmd_mkdir(args) -> int:
    share, path = split_remote_path(args.remote)
    with _client(args) as c:
        c.makedirs(share, path)
    print(_("已创建: {share}/{0}").format(path.replace(chr(92), '/'), share=share))
    return 0


def cmd_rm(args) -> int:
    share, path = split_remote_path(args.remote)
    with _client(args) as c:
        entry = c.stat(share, path)
        if entry.is_dir:
            if not args.recursive:
                c.rmdir(share, path)
            else:
                c.rmdir(share, path, recursive=True)
        else:
            c.remove(share, path)
    print(_("已删除: {display_path}").format(display_path=entry.display_path))
    return 0


def cmd_mv(args) -> int:
    import ntpath

    share, old = split_remote_path(args.remote)
    if "/" in args.target or "\\" in args.target:
        share2, new = split_remote_path(args.target)
        if share2 != share:
            raise SmbClientError(_("不支持跨共享移动"))
    else:
        # 只给了新名字:保持在原目录
        parent = ntpath.dirname(old)
        new = f"{parent}\\{args.target}" if parent else args.target
    with _client(args) as c:
        c.rename(share, old, new)
    print(_("已重命名: {share}/{0} -> {share}/{1}").format(
        old.replace(chr(92), '/'), new.replace(chr(92), '/'), share=share))
    return 0


# ---------- 入口 ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="astro-smb-tool",
        description=_("Astro SMB Tool(匿名访问,SMB 3.1.1)"),
        epilog=_('远程路径格式: "共享名/目录/文件",共享名可含空格(记得加引号)。'),
    )
    known = default_host()
    p.add_argument("-H", "--host", default=known,
                   help=(_("设备地址 (默认") + (f" {known}" if known else _("无:还没连过任何设备")) + _(",可用环境变量 ASTRO_SMB_HOST)")))
    p.add_argument("--port", type=int, default=445)
    p.add_argument("-u", "--user", default="", help=_("用户名(默认匿名)"))
    p.add_argument("-p", "--password", default="", help=_("密码(默认空)"))
    p.add_argument("--timeout", type=float, default=15.0, help=_("网络超时秒数 (默认 15)"))

    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("info", help=_("服务器信息与共享列表"))
    sp.add_argument("--all", action="store_true", help=_("包含隐藏/IPC 共享"))
    sp.set_defaults(func=cmd_info)

    sp = sub.add_parser("shares", help=_("枚举共享"))
    sp.add_argument("--all", action="store_true", help=_("包含隐藏/IPC 共享"))
    sp.set_defaults(func=cmd_shares)

    sp = sub.add_parser("ls", help=_("列目录"))
    sp.add_argument("remote", help=_('如 "EMMC Images/Autorun"'))
    sp.add_argument("-l", "--long", action="store_true", help=_("详细模式"))
    sp.set_defaults(func=cmd_ls)

    sp = sub.add_parser("tree", help=_("递归树形列出"))
    sp.add_argument("remote")
    sp.add_argument("--depth", type=int, default=None, help=_("最大深度"))
    sp.add_argument("--size", action="store_true", help=_("显示文件大小"))
    sp.set_defaults(func=cmd_tree)

    sp = sub.add_parser("find", help=_("递归搜索"))
    sp.add_argument("remote", help=_('起始位置,如 "EMMC Images"'))
    sp.add_argument("pattern", help=_("通配符,如 '*.fit'、'*M31*'(不区分大小写)"))
    sp.add_argument("--dirs", action="store_true", help=_("也匹配目录"))
    sp.add_argument("--min-size", help=_("最小大小,如 10M"))
    sp.add_argument("--max-size", help=_("最大大小,如 1G"))
    sp.add_argument("--newer-than", help=_("修改时间晚于,如 2026-07-01"))
    sp.add_argument("--depth", type=int, default=None)
    sp.add_argument("--limit", type=int, default=None)
    sp.add_argument("-l", "--long", action="store_true")
    sp.set_defaults(func=cmd_find)

    sp = sub.add_parser("get", help=_("下载文件/目录(目录自动递归)"))
    sp.add_argument("remote")
    sp.add_argument("local", nargs="?", help=_("本地目标(默认当前目录)"))
    sp.add_argument("--resume", action="store_true", help=_("断点续传"))
    sp.add_argument("--jobs", "-j", type=int, default=1,
                    help=_("单文件分块并发数(>1 且文件≥8MB 时启用)"))
    sp.set_defaults(func=cmd_get)

    sp = sub.add_parser("put", help=_("上传文件/目录到远程目录"))
    sp.add_argument("local", nargs="+", help=_("本地文件或目录(可多个)"))
    sp.add_argument("remote", help=_('远程目标目录,如 "EMMC Images/Plan"'))
    sp.add_argument("--rename", help=_("上传后的文件名(仅单文件时有效)"))
    sp.set_defaults(func=cmd_put)

    sp = sub.add_parser("cat", help=_("输出文件内容到 stdout(可部分读取)"))
    sp.add_argument("remote")
    sp.add_argument("--bytes", type=int, default=1 << 20, help=_("读取字节数(默认 1MB)"))
    sp.add_argument("--offset", type=int, default=0)
    sp.set_defaults(func=cmd_cat)

    sp = sub.add_parser("header", help=_("读取 FITS 头(只拉取头部几 KB)"))
    sp.add_argument("remote")
    sp.add_argument("--full", action="store_true", help=_("显示全部卡片"))
    sp.set_defaults(func=cmd_header)

    sp = sub.add_parser("df", help=_("显示共享卷容量/占用"))
    sp.add_argument("share", nargs="?", help=_("只看某个共享(默认全部)"))
    sp.set_defaults(func=cmd_df)

    sp = sub.add_parser("du", help=_("统计目录占用(递归)"))
    sp.add_argument("remote")
    sp.add_argument("--children", "-c", action="store_true",
                    help=_("按直接子级分组显示占用(占用分析)"))
    sp.set_defaults(func=cmd_du)

    sp = sub.add_parser("mkdir", help=_("创建远程目录(递归)"))
    sp.add_argument("remote")
    sp.set_defaults(func=cmd_mkdir)

    sp = sub.add_parser("rm", help=_("删除远程文件/目录"))
    sp.add_argument("remote")
    sp.add_argument("-r", "--recursive", action="store_true", help=_("递归删除目录"))
    sp.set_defaults(func=cmd_rm)

    sp = sub.add_parser("mv", help=_("重命名/移动(同共享内)"))
    sp.add_argument("remote", help=_('源,如 "EMMC Images/a.txt"'))
    sp.add_argument("target", help=_('新名字或 "共享/新/路径"'))
    sp.set_defaults(func=cmd_mv)

    return p


def _autodiscover() -> str:
    """没给 `-H` 也没有记录时,**去局域网找**,而不是报错让人自己填。

    **不猜地址。** 硬编码一个默认 IP 对新用户永远是错的 —— 设备是 DHCP 的,
    写死的地址只会让每条命令等 15 秒超时才知道错在哪。(早期版本里
    `DEFAULT_HOST` 就写死了开发机上那台设备的地址,换台机器条条命令都超时。)

    判据走共享层的 `discover`:**SMB 协商成功才算,端口开着不算** ——
    有路由器会对整个网段的 445 秒回 ACK,按 TCP 判会报出两百多台。

    只有**恰好一台**疑似 ASIAIR 才自动用它;多台就列出来让人选 ——
    替人在两台设备之间做决定,选错了他操作的是别人的片子。
    """
    from astro_smb.netscan import discover_all, pick_one

    print(_("没指定设备,正在扫描本网段找 ASIAIR(判据是 SMB 协商成功)…"),
          file=sys.stderr)
    rows = discover_all()
    if not rows:
        print(_("本网段没找到 SMB 设备。确认设备开着、和电脑在同一网段;"
                "也可以用 -H 直接指定,或设 ASTRO_SMB_HOST。"), file=sys.stderr)
        return ""
    pick = pick_one(rows)
    if pick is not None:
        print(_("找到 {name}({host}),用它。").format(
            name=pick.label, host=pick.ip), file=sys.stderr)
        return pick.ip
    print(_("找到 {0} 台 SMB 设备,不替你选 —— 用 -H 指定一台:").format(len(rows)),
          file=sys.stderr)
    for d in rows:
        star = "*" if d.is_asiair else " "
        print(f"  {star} {d.ip:<16} {d.label}", file=sys.stderr)
    print(_("(* = 疑似 ASIAIR)"), file=sys.stderr)
    return ""


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台可能是 GBK,强制 UTF-8 输出避免中文乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    if not (args.host or "").strip():
        host = _autodiscover()
        if not host:
            return 2
        args.host = host
    try:
        return args.func(args)
    except TransferCancelled as e:
        print(f"\n{e}", file=sys.stderr)
        return 130
    except SmbClientError as e:
        print(_("错误: {e}").format(e=e), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(_('\n已中断'), file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
