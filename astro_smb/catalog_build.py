"""从 CDS 原始 Tycho-2 数据构建 :mod:`astro_smb.catalog` 的打包星表。

用法::

    # 一步到位:下载 20 个分片(约 159 MB)并构建整表(约 35.6 MB)
    uv run python -m astro_smb.catalog_build --download --out tycho2_v1.bin

    # 已有原始数据时(目录里放 tyc2.dat.00.gz ... tyc2.dat.19.gz,或解压后的 .dat)
    uv run python -m astro_smb.catalog_build --src <目录或文件> --out tycho2_v1.bin

    # 只要亮星子集(体积小很多,适合冒烟验证)
    uv run python -m astro_smb.catalog_build --src <目录> --max-mag 8 --out bright.bin

    # 查看已构建文件的统计
    uv run python -m astro_smb.catalog_build --check tycho2_v1.bin

数据源:CDS **I/259**(Hog+ 2000, A&A 355, L27)。直链下载,支持断点续传;
**不要爬目录列表** —— CDS 挂了 Anubis 反爬。

解析全部**向量化**:记录是**定长 207 字节**(206 字符 + ``\\n``),按 ReadMe 的
1-based 字节区间切列,整列 ``bytes → float64`` 一次转换。2.5M 条整表约几秒,
不做逐行 Python 循环。

两个必须照顾到的真实数据特性(改解析器前必读):

- ``pflag = 'X'`` 的条目**没有均值位置、没有自行**(``RAmdeg``/``DEmdeg`` 为空白)。
  这些星退回**观测位置** ``RAdeg``/``DEdeg``(ReadMe 保证一定有),自行记 0。
- ``BTmag``/``VTmag`` **只保证至少有一个**。Johnson V 按 ReadMe Note(7)
  ``V = VT - 0.090*(BT-VT)``;缺 BT 时退回 VT,缺 VT 时退回 BT。
"""

from __future__ import annotations

from astro_smb import paths
import argparse
import gzip
import re
import sys
import time
from pathlib import Path

import numpy as np

from astro_smb.catalog import (
    CatalogError, RECORD_DTYPE, catalog_path, encode_records,
    validate_catalog_file, write_records,
)
from astro_smb.i18n import gettext as _

__all__ = [
    "TYC2_BASE_URL", "TYC2_PARTS", "TYC2_LINE_BYTES", "TYC2_COLUMNS",
    "johnson_v", "parse_tyc2_bytes", "read_source", "iter_sources",
    "build", "download_parts", "main",
]

#: CDS I/259 主表分片直链前缀
TYC2_BASE_URL = "https://cdsarc.cds.unistra.fr/ftp/I/259"
#: 主表被切成 20 个分片 tyc2.dat.00.gz ... tyc2.dat.19.gz
TYC2_PARTS = tuple(f"tyc2.dat.{i:02d}.gz" for i in range(20))

#: 每条记录定长字节数(206 字符正文 + 1 个换行)
TYC2_LINE_BYTES = 207

#: ReadMe I/259 的 1-based 闭区间字节位置(改这里前先核对 ReadMe)
TYC2_COLUMNS = {
    "pflag": (14, 14),      # ' '正常 / 'P'光心 / 'X' 无均值位置
    "RAmdeg": (16, 27),     # 均值位置, ICRS, 历元 J2000
    "DEmdeg": (29, 40),
    "pmRA": (42, 48),       # mas/yr, 已含 cos(dec)
    "pmDE": (50, 56),
    "BTmag": (111, 116),
    "VTmag": (124, 129),
    "RAdeg": (153, 164),    # 观测位置(历元约 1991.5), 一定有值
    "DEdeg": (166, 177),
}

_SPACE = 32
_ZERO = 48


def _float_col(arr: np.ndarray, a: int, b: int) -> np.ndarray:
    """按 1-based 闭区间取列并整列转 float64;**全空白 → NaN**。

    ``numpy`` 的 ``bytes → float`` 转换遇到全空白字段会抛
    ``could not convert string to float``,所以先把空白行填成 ``'0'``、
    转完再打回 NaN(实测比逐行 ``float()`` 快两个量级)。
    """
    sub = np.array(arr[:, a - 1:b], dtype=np.uint8)   # 显式拷贝,可写
    blank = np.all(sub == _SPACE, axis=1)
    if blank.any():
        sub[blank, -1] = _ZERO
    width = b - a + 1
    out = np.frombuffer(sub.tobytes(), dtype=f"S{width}").astype(np.float64)
    if blank.any():
        out = out.copy()
        out[blank] = np.nan
    return out


def johnson_v(bt: np.ndarray, vt: np.ndarray) -> np.ndarray:
    """Tycho BT/VT → Johnson V(ReadMe I/259 Note(7))。

    ``V = VT - 0.090*(BT-VT)``;两个星等**只保证有一个**,缺 BT 退回 VT、
    缺 VT 退回 BT。BT-only 的星按 BT 记会把 V 高估 0.3~0.5 等(偏暗),
    对"取视场内最亮 N 颗"这个用途无害。
    """
    bt = np.asarray(bt, dtype=np.float64)
    vt = np.asarray(vt, dtype=np.float64)
    v = vt - 0.090 * (bt - vt)
    v = np.where(np.isnan(v), vt, v)
    v = np.where(np.isnan(v), bt, v)
    return v


def parse_tyc2_bytes(raw: bytes) -> dict[str, np.ndarray]:
    """定长记录块 → ``{ra, dec, vmag, pmra, pmde}``(度 / 星等 / mas/yr)。

    返回的数组已剔除位置缺失的条目(实测 Tycho-2 主表里没有,属防御)。
    """
    if len(raw) % TYC2_LINE_BYTES:
        raise CatalogError(
            _("Tycho-2 记录长度不符: {0} 不是 {TYC2_LINE_BYTES} 的整数倍 — 数据可能被截断或不是 tyc2.dat").format(
                len(raw), TYC2_LINE_BYTES=TYC2_LINE_BYTES))
    arr = np.frombuffer(raw, dtype=np.uint8).reshape(-1, TYC2_LINE_BYTES)
    if arr.size and not bool(np.all(arr[:, TYC2_LINE_BYTES - 1] == 10)):
        raise CatalogError(_("Tycho-2 记录行尾不是换行符 — 定长假设不成立"))

    col = TYC2_COLUMNS
    ra_m = _float_col(arr, *col["RAmdeg"])
    de_m = _float_col(arr, *col["DEmdeg"])
    ra_o = _float_col(arr, *col["RAdeg"])
    de_o = _float_col(arr, *col["DEdeg"])
    pmra = _float_col(arr, *col["pmRA"])
    pmde = _float_col(arr, *col["pmDE"])
    bt = _float_col(arr, *col["BTmag"])
    vt = _float_col(arr, *col["VTmag"])

    # pflag='X': 无均值位置/自行 → 退回观测位置, 自行记 0
    has_mean = ~(np.isnan(ra_m) | np.isnan(de_m))
    ra = np.where(has_mean, ra_m, ra_o)
    dec = np.where(has_mean, de_m, de_o)
    pmra = np.where(np.isnan(pmra), 0.0, pmra)
    pmde = np.where(np.isnan(pmde), 0.0, pmde)
    vmag = johnson_v(bt, vt)

    ok = ~(np.isnan(ra) | np.isnan(dec))
    if not bool(np.all(ok)):
        ra, dec, vmag, pmra, pmde = (
            x[ok] for x in (ra, dec, vmag, pmra, pmde))
    # 星等仍可能为 NaN(BT/VT 都缺): encode_records 会记成可表示的最暗值
    return {"ra": ra, "dec": dec, "vmag": vmag, "pmra": pmra, "pmde": pmde}


def read_source(path) -> bytes:
    """读一个分片(``.gz`` 自动解压)。"""
    p = Path(path)
    try:
        if p.suffix == ".gz":
            with gzip.open(p, "rb") as fh:
                return fh.read()
        return p.read_bytes()
    except OSError as ex:
        raise CatalogError(_("无法读取原始星表分片 {name}: {ex}").format(name=p.name, ex=ex)) from ex


#: 目录扫描只认这个形状: tyc2.dat[.NN][.gz]。**不能只用 startswith** ——
#: 下载器/工具常在同目录留 ``tyc2.dat.00.gz.done`` 之类的旁路文件,
#: 它们是 0 字节、恰好也能被"长度是 207 倍数"的检查放行,于是静默贡献 0 条记录。
_SRC_RE = re.compile(r"^tyc2\.dat(\.\d+)?(\.gz)?$", re.IGNORECASE)


def iter_sources(src) -> list[Path]:
    """``src`` 是目录时收集 ``tyc2.dat[.NN][.gz]``;是文件时就它自己。"""
    p = Path(src)
    if p.is_dir():
        files = sorted(q for q in p.iterdir()
                       if q.is_file() and _SRC_RE.match(q.name))
        if not files:
            raise CatalogError(_("{p} 下没有 tyc2.dat* 分片").format(p=p))
        return files
    if p.is_file():
        return [p]
    raise CatalogError(_("原始星表路径不存在: {p}").format(p=p))


def build(src, out, *, max_mag: float | None = None,
          source_name: str = "Tycho-2", progress=None):
    """从原始分片构建打包星表。返回写出的 :class:`~astro_smb.catalog.CatalogHeader`。

    分片逐个解析并**立即量化成 14 字节记录**再累积 —— 整表记录只占 35.6 MB,
    不会把 5 亿字节的原始文本同时留在内存里。
    """
    files = iter_sources(src)
    chunks: list[np.ndarray] = []
    total = 0
    for i, f in enumerate(files):
        d = parse_tyc2_bytes(read_source(f))
        if max_mag is not None:
            keep = np.nan_to_num(d["vmag"], nan=99.0) <= float(max_mag)
            d = {k: v[keep] for k, v in d.items()}
        rec = encode_records(d["ra"], d["dec"], d["vmag"],
                             d["pmra"], d["pmde"])
        total += int(rec.size)
        chunks.append(rec)
        if progress is not None:
            progress(i + 1, len(files), total)
    rec = (np.concatenate(chunks) if chunks
           else np.zeros(0, dtype=RECORD_DTYPE))
    return write_records(out, rec, source=source_name)


# ---------------------------------------------------------------- 下载原始数据

def download_parts(dest_dir, parts=TYC2_PARTS, progress=None) -> list[Path]:
    """把 CDS 的 20 个分片下到 ``dest_dir``(已存在且 gzip 完好的跳过)。

    用 ``curl.exe`` 断点续传(``-C -``),下完**必须 ``gzip`` 完整性自检** ——
    截断的分片会让构建出来的星表少几万颗星却毫无征兆(与 §7.5「半截缓存被
    当成完整文件」同源)。
    """
    import subprocess

    out_dir = Path(dest_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    got: list[Path] = []
    for i, name in enumerate(parts):
        dst = out_dir / name
        if dst.is_file() and _gzip_ok(dst):
            got.append(dst)
            if progress is not None:
                progress(i + 1, len(parts), name, True)
            continue
        try:
            proc = subprocess.run(
                paths.curl_argv("-sSL", "--max-time", "900", "--retry", "3",
                 "-C", "-", "-o", str(dst), f"{TYC2_BASE_URL}/{name}"),
                capture_output=True, timeout=1000,
                creationflags=paths.NO_WINDOW)
        except (OSError, subprocess.SubprocessError) as ex:
            raise CatalogError(_("下载 {name} 失败: {ex}").format(name=name, ex=ex)) from ex
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", errors="replace").strip()
            raise CatalogError(_("下载 {name} 失败: {0}").format(
                err or proc.returncode, name=name))
        if not _gzip_ok(dst):
            try:
                dst.unlink(missing_ok=True)     # 截断分片不留下
            except OSError:
                pass
            raise CatalogError(_("{name} 下载后 gzip 校验失败 — 疑似被截断").format(name=name))
        got.append(dst)
        if progress is not None:
            progress(i + 1, len(parts), name, False)
    return got


def _gzip_ok(path: Path) -> bool:
    try:
        with gzip.open(path, "rb") as fh:
            while fh.read(1 << 20):
                pass
        return True
    except (OSError, EOFError):
        return False


# ---------------------------------------------------------------- CLI

def _describe(path) -> str:
    hdr = validate_catalog_file(path)
    from astro_smb.catalog import Catalog
    with Catalog.open(path) as cat:
        dec = cat.dec
        vm = cat.vmag
        lines = [
            _("文件      : {path}").format(path=path),
            _("来源      : {source}  格式 v{version}  记录 {record_bytes} 字节").format(
                source=hdr.source, version=hdr.version, record_bytes=hdr.record_bytes),
            _("条目数    : {count:,}   文件大小 {0:.1f} MB").format(
                hdr.file_bytes / 1000000.0, count=hdr.count),
            _("历元      : J{epoch:.1f}   生成于 {0}").format(
                time.strftime('%Y-%m-%d %H:%M', time.localtime(hdr.built_unix)), epoch=hdr.epoch),
            _("dec 升序  : {0}   范围 {1:+.4f} ~ {2:+.4f}").format(
                cat.verify_sorted(), dec.min(), dec.max()),
            _("V 星等    : {0:.3f} ~ {1:.3f}   中位 {2:.2f}").format(
                vm.min(), vm.max(), float(np.median(vm))),
            _("最大自行  : {max_pm_mas:.0f} mas/yr").format(max_pm_mas=cat.max_pm_mas),
        ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m astro_smb.catalog_build",
        description=_("构建 Tycho-2 打包星表(14 字节/星)"))
    ap.add_argument("--src", help=_("原始 tyc2.dat 分片所在目录(或单个文件)"))
    ap.add_argument("--download", action="store_true",
                    help=_("先从 CDS 下载 20 个分片(约 159 MB)到 --raw-dir"))
    ap.add_argument("--raw-dir", default=None,
                    help=_("原始分片存放目录(配合 --download,默认 <out目录>/tyc2raw)"))
    ap.add_argument("--out", default=None,
                    help=_("输出打包文件(默认 {0})").format(catalog_path()))
    ap.add_argument("--max-mag", type=float, default=None,
                    help=_("只保留 V ≤ 该值的星(构建亮星子集)"))
    ap.add_argument("--name", default="Tycho-2", help=_("写入文件头的来源短名"))
    ap.add_argument("--check", default=None, help=_("只打印已构建文件的统计"))
    args = ap.parse_args(argv)

    # Windows 控制台可能是 GBK, 强制 UTF-8 输出避免中文乱码(与 cli.py 一致)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass

    if args.check:
        try:
            print(_describe(args.check))
        except CatalogError as ex:
            print(_("星表校验失败: {ex}").format(ex=ex), file=sys.stderr)
            return 2
        return 0

    out = Path(args.out) if args.out else catalog_path()
    src = args.src
    try:
        if args.download:
            raw_dir = Path(args.raw_dir) if args.raw_dir else out.parent / "tyc2raw"
            print(_("下载 Tycho-2 原始分片 → {raw_dir}").format(raw_dir=raw_dir))
            download_parts(
                raw_dir,
                progress=lambda i, n, name, cached: print(
                    f"  [{i}/{n}] {name}{_(' (已缓存)') if cached else ''}"))
            src = raw_dir
        if not src:
            ap.error(_("需要 --src 或 --download"))
        t0 = time.time()
        hdr = build(src, out, max_mag=args.max_mag, source_name=args.name,
                    progress=lambda i, n, tot: print(
                        _("  解析 [{i}/{n}] 累计 {tot:,} 条").format(i=i, n=n, tot=tot)))
        print(_("完成: {count:,} 条, {out} ({0:.1f} MB, {1:.1f}s)").format(
            out.stat().st_size / 1000000.0, time.time() - t0, count=hdr.count, out=out))
        print(_describe(out))
    except CatalogError as ex:
        print(_("构建失败: {ex}").format(ex=ex), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(_("已中断"), file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":     # pragma: no cover
    raise SystemExit(main())
