"""离线镜像后端:拿 `scripts/pull_mirror.py` 抓下来的结构当一台设备用。

**为什么需要它。** ASIAIR 是 DHCP 的、会重启、会被别的实例占着,而界面开发
99% 的时间只需要"有哪些文件、多大、什么时候拍的"这些**元数据**,以及日志原文。
把设备拴在开发循环里,等于每次它掉线就停工;而并行开发第二套界面时,
两边同时连一台设备还会互相拖慢。

**镜像里有什么。** `tree.json` 是三个共享的完整递归元数据(几百 KB),
`logs/` 是 Autorun 与 PHD2 日志原文(拍摄记录页与导星页的**全部**输入),
`thumbs/` 是一批缩略图,`fits/` 是少量原图。**内容只抓了这些** —— 光 EMMC
就 222GB,全量镜像没有意义。

**读不到内容时会说清楚。** 没镜像下来的文件走 `read_bytes` 会抛
`SmbClientError` 并写明"这个文件不在镜像里",而不是返回一片零 ——
返回零会让 FITS 头解析出一堆莫名其妙的结果,排查时根本想不到是数据源的问题。

用法::

    ASTRO_SMB_MIRROR=.tmp/mirror uv run astro-smb-tool-qt
    # 或
    uv run astro-smb-tool-qt --host mirror:.tmp/mirror
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from astro_smb.client import (
    RemoteEntry,
    ShareInfo,
    SmbClientError,
    VolumeInfo,
)
from astro_smb.i18n import gettext as _

SEP = "\\"

#: 环境变量:指向镜像目录。给了它就整个应用离线跑。
ENV_MIRROR = "ASTRO_SMB_MIRROR"


def mirror_root_from_env() -> Path | None:
    """`ASTRO_SMB_MIRROR` 指向的镜像目录;没设或不存在返回 None。"""
    raw = (os.environ.get(ENV_MIRROR) or "").strip()
    if not raw:
        return None
    p = Path(raw).expanduser()
    return p if (p / "tree.json").is_file() else None


class MirrorBackend:
    """只读的离线后端。实现 `StorageBackend` 里界面真正会用到的那部分。

    写操作(上传/删除/改名)一律抛错并说明原因 —— **不要静默成功**:
    一个"看起来删掉了、其实什么都没发生"的界面比直接报错难查得多。
    """

    def __init__(self, root: str | Path, *, timeout: float = 0.0,
                 chunk_size: int = 0) -> None:
        self.root = Path(root).expanduser()
        blob = json.loads((self.root / "tree.json").read_text(encoding="utf-8"))
        self._info: dict = dict(blob.get("info") or {})
        self._shares = [ShareInfo(name=s["name"], type=0,
                                  remark=s.get("remark") or "")
                        for s in blob.get("shares") or ()]
        self._entries: dict[str, list[dict]] = blob.get("entries") or {}
        self._origin = str(blob.get("host") or "")
        try:
            self._vols = json.loads(
                (self.root / "volumes.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._vols = {}

    # ---------------------------------------------------------------- 生命周期

    @property
    def host(self) -> str:
        return _("镜像 {name}").format(name=self.root.name)

    def connect(self) -> None:
        return None

    def close(self) -> None:
        return None

    def reconnect(self) -> None:
        return None

    def clone(self) -> "MirrorBackend":
        # 无连接可言,但**仍然返回新实例** —— 调用方按"每线程一个"的纪律写的,
        # 返回 self 会让"镜像上能跑、真设备上串包"这种差异藏起来。
        return MirrorBackend(self.root)

    def ping_tcp(self, timeout: float = 1.0) -> tuple[bool, float | None]:
        return True, 0.0

    def echo(self) -> float:
        return 0.0

    def server_info(self) -> dict[str, str]:
        out = {"server_name": self._info.get("server_name") or _("镜像"),
               "dialect": self._info.get("dialect") or "-",
               "os": self._info.get("os") or _("离线镜像")}
        out.update({k: str(v) for k, v in self._info.items() if k not in out})
        out["origin"] = self._origin
        return out

    # ---------------------------------------------------------------- 读

    def list_shares(self, include_hidden: bool = False) -> list[ShareInfo]:
        return list(self._shares)

    def _rows(self, share: str) -> list[dict]:
        if share not in self._entries:
            raise SmbClientError(_("镜像里没有共享 {share!r}").format(share=share))
        return self._entries[share]

    @staticmethod
    def _parent(path: str) -> str:
        return path.rsplit(SEP, 1)[0] if SEP in path else ""

    def _make(self, share: str, row: dict) -> RemoteEntry:
        return RemoteEntry(
            share=share, path=row["path"], name=row["name"],
            is_dir=bool(row["is_dir"]), size=int(row["size"] or 0),
            mtime=float(row.get("mtime") or 0.0),
            ctime=float(row.get("ctime") or 0.0),
            atime=float(row.get("atime") or 0.0),
            attributes=int(row.get("attributes") or 0))

    def listdir(self, share: str, path: str = "") -> list[RemoteEntry]:
        want = (path or "").strip(SEP)
        out = [self._make(share, r) for r in self._rows(share)
               if self._parent(r["path"]) == want]
        # 目录在前,再按名字 —— 与真后端同一套排序,免得两边看起来不一样
        out.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return out

    def stat(self, share: str, path: str) -> RemoteEntry:
        want = (path or "").strip(SEP)
        for r in self._rows(share):
            if r["path"] == want:
                return self._make(share, r)
        raise SmbClientError(_("镜像里没有 {share}/{path}").format(share=share, path=path))

    def exists(self, share: str, path: str) -> bool:
        try:
            self.stat(share, path)
            return True
        except SmbClientError:
            return False

    def _local(self, name: str) -> Path | None:
        """这个文件的内容有没有被镜像下来。按**文件名**找,不按路径 ——
        抓的时候是摊平放的(日志/缩略图/原图各一个目录)。"""
        for sub in ("logs", "thumbs", "fits"):
            cand = self.root / sub / name
            if cand.is_file():
                return cand
        return None

    def read_bytes(self, share: str, path: str, offset: int = 0,
                   size: int = 65536) -> bytes:
        """**默认 64KB 部分读取,与协议一致。**

        `StorageBackend.read_bytes` 的默认值就是 65536 —— 它是"低开销预览"
        用的(读几 KB 就能拿到 FITS 头),整文件要走 `download_file`。
        这里原来默认"读全部",和另外两个后端不一样:同一段调用代码在镜像上
        拿到 52MB、在真设备上拿到 64KB,而**两边都不报错** —— 这种只在
        某个后端上成立的差异,正是镜像最容易骗人的地方。
        """
        entry = self.stat(share, path)
        local = self._local(entry.name)
        if local is None:
            raise SmbClientError(
                _("{name} 的内容不在镜像里(镜像只抓了日志/缩略图/少量原图) —— 换真设备,或用 scripts/pull_mirror.py 多抓一点").format(
                    name=entry.name))
        if size <= 0:
            return b""
        with local.open("rb") as fh:
            if offset:
                fh.seek(offset)
            return fh.read(size)

    def download_file(self, share: str, path: str, local_path,
                      resume: bool = False, on_progress=None,
                      cancel=None) -> int:
        entry = self.stat(share, path)
        src = self._local(entry.name)
        if src is None:
            raise SmbClientError(
                _("{name} 的内容不在镜像里 —— 换真设备,或用 scripts/pull_mirror.py 多抓一点").format(
                    name=entry.name))
        dest = Path(local_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest)
        n = dest.stat().st_size
        if on_progress:
            on_progress(n)
        return n

    def volume_info(self, share: str) -> VolumeInfo | None:
        got = self._vols.get(share)
        return VolumeInfo(total=int(got["total"]), free=int(got["free"])) \
            if got else None

    def count_children(self, share: str, path: str) -> tuple[int, int] | None:
        kids = self.listdir(share, path)
        return sum(1 for e in kids if e.is_dir), sum(1 for e in kids
                                                     if not e.is_dir)

    def walk(self, share: str, top: str = "", max_depth: int | None = None,
             on_error=None, depth_first: bool = False):
        stack = [(top or "").strip(SEP)]
        while stack:
            cur = stack.pop()
            kids = self.listdir(share, cur)
            dirs = [e for e in kids if e.is_dir]
            files = [e for e in kids if not e.is_dir]
            yield cur, dirs, files
            stack.extend(d.path for d in dirs)

    # ---------------------------------------------------------------- 写(不支持)

    def _readonly(self, what: str):
        raise SmbClientError(
            _("离线镜像是只读的,不能{what} —— 要写就连真设备").format(what=what))

    def upload_file(self, *a, **k):
        self._readonly(_("上传"))

    def upload_dir(self, *a, **k):
        self._readonly(_("上传"))

    def makedirs(self, *a, **k):
        self._readonly(_("新建目录"))

    def mkdir(self, *a, **k):
        self._readonly(_("新建目录"))

    def remove(self, *a, **k):
        self._readonly(_("删除"))

    def rmdir(self, *a, **k):
        self._readonly(_("删除"))

    def rename(self, *a, **k):
        self._readonly(_("重命名"))
