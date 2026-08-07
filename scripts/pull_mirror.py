"""把设备的**结构**抓成一份离线镜像,供无设备时开发/测试用。

设备是 DHCP 的、会重启、会被别人占着 —— 界面开发不该被它拴住。
这个脚本抓的是:

- `tree.json`  每个共享的完整递归元数据(名字/大小/时间/是否目录)。
  **只抓元数据不抓内容** —— 光 EMMC 就 222GB,而界面要的是"有哪些文件、
  多大、什么时候拍的",那些全在元数据里。
- `logs/`      Autorun 与 PHD2 日志原文(几百 KB,是拍摄记录页与导星页的全部输入)
- `thumbs/`    一批 `_thn.jpg` 缩略图(每张约 18KB,浏览页预览用)
- `fits/`      少量 `.fit` 原图(每张约 50MB,影像查看与板解算用)
- `volumes.json` 各共享的容量(空间分析页用)

**并发**:树遍历是**延迟受限**的 —— 一次 `listdir` 就是一个往返,几千个目录
串行走会很久。所以用一池 worker 各自 `clone()` 一条连接去抢队列里的目录。
下载同理。impacket 的连接**不是线程安全的**,所以是"每 worker 一条连接"
而不是"共用一条"(docs/DEVELOPMENT.md §8)。

用法::

    uv run python scripts/pull_mirror.py -H 192.0.2.227
    uv run python scripts/pull_mirror.py -H 192.0.2.227 -j 24 --fits 4

产物在 `.tmp/mirror/`(已在 .gitignore 里)。
"""
from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from astro_smb.client import AstroSmbClient  # noqa: E402

OUT = ROOT / ".tmp" / "mirror"
#: 抓多少张缩略图。够浏览页翻几屏就行,不必全量(几千张 × 18KB 也有上百 MB)。
THUMB_LIMIT = 160


def _entry(e) -> dict:
    return {
        "name": e.name,
        "path": e.path,
        "is_dir": bool(e.is_dir),
        "size": int(e.size or 0),
        "mtime": float(e.mtime) if e.mtime else None,
        "ctime": float(getattr(e, "ctime", 0) or 0) or None,
        "atime": float(getattr(e, "atime", 0) or 0) or None,
        "attributes": int(getattr(e, "attributes", 0) or 0),
    }


class Walker:
    """并发遍历一个共享。

    队列里放待列的目录,每个 worker 自己 `clone()` 一条连接去抢。
    **不共用连接** —— impacket 的连接不是线程安全的,共用会串包。
    """

    def __init__(self, base: AstroSmbClient, share: str, workers: int) -> None:
        self.base, self.share, self.workers = base, share, workers
        self.q: queue.Queue[str | None] = queue.Queue()
        self.out: list[dict] = []
        self.lock = threading.Lock()
        self.pending = 0            # 还没列完的目录数(不是队列长度)
        self.dirs = 0
        self.errors: list[str] = []

    def _push(self, path: str) -> None:
        with self.lock:
            self.pending += 1
        self.q.put(path)

    def _work(self) -> None:
        cli = self.base.clone()
        try:
            cli.connect()
        except Exception as ex:
            with self.lock:
                self.errors.append(f"连接失败: {ex}")
            return
        try:
            while True:
                cur = self.q.get()
                if cur is None:
                    return
                try:
                    entries = cli.listdir(self.share, cur)
                except Exception as ex:
                    with self.lock:
                        self.errors.append(f"{self.share}/{cur}: {ex}")
                    entries = []
                rows = [_entry(e) for e in entries]
                subs = [e.path for e in entries if e.is_dir]
                with self.lock:
                    self.out.extend(rows)
                    self.dirs += 1
                    self.pending -= 1
                    done = self.dirs
                for p in subs:
                    self._push(p)
                if done % 200 == 0:
                    print(f"  … {done} 个目录 / {len(self.out)} 个条目",
                          flush=True)
                with self.lock:
                    finished = self.pending == 0
                if finished:
                    # 没有待列目录了 —— 给所有 worker 发退出信号
                    for _ in range(self.workers):
                        self.q.put(None)
        finally:
            try:
                cli.close()
            except Exception:
                pass

    def run(self) -> list[dict]:
        self._push("")
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for _ in range(self.workers):
                pool.submit(self._work)
        return self.out


def _fetch_many(base: AstroSmbClient, jobs: list[tuple[str, str, Path]],
                workers: int, label: str) -> int:
    """并发下小文件。`jobs` 是 (share, 远端路径, 本地目标)。"""
    if not jobs:
        return 0
    done = [0]
    lock = threading.Lock()
    local = threading.local()

    def one(job) -> None:
        share, path, dest = job
        cli = getattr(local, "cli", None)
        if cli is None:
            cli = base.clone()
            cli.connect()
            local.cli = cli
        try:
            cli.download_file(share, path, dest)
            with lock:
                done[0] += 1
                if done[0] % 40 == 0:
                    print(f"  … {label} {done[0]}/{len(jobs)}", flush=True)
        except Exception as ex:
            print(f"  ! {dest.name}: {ex}", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, jobs))
    return done[0]


def pull_full(base: AstroSmbClient, tree: dict[str, list[dict]],
              dest_root: Path, workers: int) -> None:
    """**全量落地**:把整棵树按原结构拷到本地,让它能被当成一台"本地卡"用。

    为什么值得这么做:`LocalBackend`(本地卡直插)是**已有的正式设备类型**,
    一个根目录 = 一个共享、共享名取目录名。把 `EMMC Images` 原样拷下来,
    `make_backend(kind="local", path=<那个目录>)` 就是一台完整的设备 ——
    **走的是用户真在用的代码路径**,而不是一个只在开发时存在的特制后端。
    读写、部分读取、分块下载、占用扫描全都能跑。

    体量也不吓人:卷是 238GB,但**已用只有 16.4GB**(那个大数字是容量不是内容)。

    并发按文件铺开;每 worker 各自 `clone()` 一条连接(impacket 不是线程安全的)。
    已存在且大小一致的跳过 —— 断了重跑不必从头来。
    """
    jobs: list[tuple[str, dict, Path]] = []
    for share, entries in tree.items():
        for e in entries:
            if e["is_dir"]:
                continue
            dest = dest_root / share / e["path"].replace("\\", "/")
            if dest.exists() and dest.stat().st_size == e["size"]:
                continue
            jobs.append((share, e, dest))
    if not jobs:
        print("[全量] 已是最新", flush=True)
        return
    total = sum(e["size"] for _s, e, _d in jobs)
    print(f"[全量] {len(jobs)} 个文件 · {total / 1e9:.2f} GB · 并发 {workers}",
          flush=True)

    done = [0, 0]           # 文件数, 字节数
    lock = threading.Lock()
    local = threading.local()
    t0 = time.time()

    def one(job) -> None:
        share, e, dest = job
        cli = getattr(local, "cli", None)
        if cli is None:
            cli = base.clone()
            cli.connect()
            local.cli = cli
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            cli.download_file(share, e["path"], dest)
        except Exception as ex:
            print(f"  ! {e['name']}: {ex}", flush=True)
            return
        with lock:
            done[0] += 1
            done[1] += e["size"]
            if done[0] % 20 == 0 or done[0] == len(jobs):
                el = max(1e-9, time.time() - t0)
                print(f"  … {done[0]}/{len(jobs)} · "
                      f"{done[1] / 1e9:.2f}/{total / 1e9:.2f} GB · "
                      f"{done[1] / el / 1e6:.0f} MB/s", flush=True)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(one, jobs))
    print(f"[全量] 完成 {done[0]}/{len(jobs)} 个文件 · "
          f"{time.time() - t0:.0f}s", flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-H", "--host", required=True)
    ap.add_argument("-j", "--jobs", type=int, default=16,
                    help="并发连接数(树遍历是延迟受限的,给大点)")
    ap.add_argument("--full", metavar="目录", default="",
                    help="把整棵树原样拷到这个目录 —— 之后可以当本地卡用:"
                         "make_backend(kind='local', path='<目录>/EMMC Images')")
    ap.add_argument("--fits", type=int, default=2, help="抓几张 .fit 原图")
    ap.add_argument("--thumbs", type=int, default=THUMB_LIMIT)
    args = ap.parse_args(argv)

    OUT.mkdir(parents=True, exist_ok=True)
    base = AstroSmbClient(host=args.host, timeout=25)
    base.connect()
    t0 = time.time()

    info = base.server_info()
    shares = [s for s in base.list_shares() if not s.name.endswith("$")]
    print(f"设备 {args.host}: {info.get('server_name')} · "
          f"{len(shares)} 个共享 · 并发 {args.jobs}", flush=True)

    tree: dict[str, list[dict]] = {}
    for s in shares:
        print(f"[树] {s.name}", flush=True)
        w = Walker(base, s.name, args.jobs)
        tree[s.name] = w.run()
        print(f"  = {len(w.out)} 个条目 / {w.dirs} 个目录"
              + (f" · {len(w.errors)} 处出错" if w.errors else ""), flush=True)

    (OUT / "tree.json").write_text(
        json.dumps({"host": args.host, "info": info,
                    "shares": [{"name": s.name, "remark": s.remark}
                               for s in shares],
                    "entries": tree}, ensure_ascii=False),
        encoding="utf-8")
    print(f"[树] 写出 tree.json · 耗时 {time.time() - t0:.0f}s", flush=True)

    if args.full:
        pull_full(base, tree, Path(args.full).expanduser(), args.jobs)

    vols = {}
    for s in shares:
        try:
            v = base.volume_info(s.name)
            if v:
                vols[s.name] = {"total": v.total, "free": v.free}
        except Exception as ex:
            print(f"  ! 容量 {s.name}: {ex}", flush=True)
    (OUT / "volumes.json").write_text(json.dumps(vols), encoding="utf-8")

    # ---- 日志:拍摄记录页与导星页的**全部**输入,必抓 ----
    logs = OUT / "logs"
    logs.mkdir(exist_ok=True)
    jobs = []
    for share, entries in tree.items():
        for e in entries:
            nm = e["name"]
            if (e["is_dir"] or not nm.endswith(".txt")
                    or not nm.startswith(("Autorun_Log_", "PHD2_GuideLog_"))):
                continue
            dest = logs / nm
            if dest.exists() and dest.stat().st_size == e["size"]:
                continue
            jobs.append((share, e["path"], dest))
    got = _fetch_many(base, jobs, args.jobs, "日志")
    print(f"[日志] 新抓 {got} 份,共 {len(list(logs.glob('*.txt')))} 份",
          flush=True)

    # ---- 缩略图:浏览页预览。均匀抽稀,别只抓同一个目录的前 N 张 ----
    thumbs = OUT / "thumbs"
    thumbs.mkdir(exist_ok=True)
    cand = [(sh, e) for sh, es in tree.items() for e in es
            if not e["is_dir"] and e["name"].endswith("_thn.jpg")]
    step = max(1, len(cand) // max(1, args.thumbs))
    jobs = [(sh, e["path"], thumbs / e["name"])
            for sh, e in cand[::step][:args.thumbs]
            if not (thumbs / e["name"]).exists()]
    got = _fetch_many(base, jobs, args.jobs, "缩略图")
    print(f"[缩略图] 新抓 {got} 张(候选 {len(cand)})", flush=True)

    # ---- 原图:影像查看与板解算要真数据 ----
    fits_dir = OUT / "fits"
    fits_dir.mkdir(exist_ok=True)
    lights = [(sh, e) for sh, es in tree.items() for e in es
              if not e["is_dir"] and e["name"].lower().endswith(".fit")
              and e["name"].startswith("Light")]
    anyfit = [(sh, e) for sh, es in tree.items() for e in es
              if not e["is_dir"] and e["name"].lower().endswith(".fit")]
    picks = (lights or anyfit)[:args.fits]
    for share, e in picks:
        dest = fits_dir / e["name"]
        if dest.exists() and dest.stat().st_size == e["size"]:
            continue
        print(f"[原图] {e['name']} ({e['size'] / 1e6:.0f} MB)", flush=True)
        try:
            # 大文件走分块并发 —— 单流约 6 MiB/s,4~8 并发能快一倍多
            from astro_smb.parallel import ParallelDownloader

            def _mk():
                c = base.clone()
                c.connect()
                return c

            ParallelDownloader(_mk, workers=min(8, args.jobs)).download(
                share, e["path"], dest, e["size"])
        except Exception as ex:
            print(f"  ! 分块下载失败,退回顺序: {ex}", flush=True)
            try:
                base.download_file(share, e["path"], dest)
            except Exception as ex2:
                print(f"  ! {e['name']}: {ex2}", flush=True)

    base.close()
    total = sum(len(v) for v in tree.values())
    size = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    print(f"\n完成:{total} 个条目 · 镜像 {size / 1e6:.0f} MB · "
          f"耗时 {time.time() - t0:.0f}s", flush=True)
    print(f"镜像在 {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
