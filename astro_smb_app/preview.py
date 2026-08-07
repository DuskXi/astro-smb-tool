"""快速低开销预览服务。

策略(按开销从低到高):
1. 目录列表元数据 —— 零额外 I/O;
2. FITS 头 —— 只从 SMB 部分读取头部几 KB;
3. ASIAIR 缩略图 —— 设备会在 .fit 旁生成 `<名字>_thn.jpg`(几十 KB),
   优先拉它当预览图,完全不碰几十 MB 的原图;
4. 小图片/文本 —— 下载到本地缓存后生成 ≤1024px 缩略图;
5. 大 FITS 拉伸预览 —— 仅在用户明确点击"生成预览"时才下载全图。

工作线程只保留最新请求(浏览时快速换选不会排队),结果带 token,
UI 侧丢弃过期结果。
"""

from __future__ import annotations

import hashlib
import itertools
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

from astro_smb_app import metacache
from astro_smb import paths
from astro_smb.client import AstroSmbClient, RemoteEntry, SmbClientError
from astro_smb.fitshdr import BLOCK, FitsHeader, header_read_hint, parse_fits_header
from astro_smb.util import human_size
from astro_smb.i18n import N_, gettext as _

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff", ".webp"}
FITS_EXTS = {".fit", ".fits", ".fts"}
TEXT_EXTS = {".txt", ".log", ".json", ".ini", ".cfg", ".csv", ".md", ".py", ".sh", ".yaml", ".yml"}

IMAGE_MAX = 25 << 20      # 图片超过 25MB 不自动下载
TEXT_MAX = 128 << 10      # 文本最多读 128KB
FITS_AUTO_MAX = 0         # FITS 一律不自动下载全图(有 _thn.jpg 兜底)
THUMB_SIZE = 1024


def cache_dir() -> Path:
    base = paths.cache_root() / "cache"
    base.mkdir(parents=True, exist_ok=True)
    return base


FITS_HDR_KIND = "fitshdr"       # metacache 的数据种类名


def cache_key(host: str, entry: RemoteEntry, tag: str = "") -> str:
    """缓存文件名:设备 + 共享 + 路径 + 大小 + mtime + 用途标签 的 sha1。

    模块级函数(不是 PreviewWorker 的方法)—— FITS 查看器等其它组件要用**同一套**
    键才能命中同一份原图缓存,不然每个组件各下一遍 50MB。
    """
    raw = f"{host}|{entry.share}|{entry.path}|{entry.size}|{entry.mtime}|{tag}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


_dl_locks: dict[str, threading.Lock] = {}
_dl_locks_guard = threading.Lock()
_tmp_seq = itertools.count(1)


def _dest_lock(dest: Path) -> threading.Lock:
    """按目标路径取一把进程内互斥锁(同一个 dest 全局唯一)。"""
    key = os.path.normcase(os.path.abspath(str(dest)))
    with _dl_locks_guard:
        lk = _dl_locks.get(key)
        if lk is None:
            lk = _dl_locks[key] = threading.Lock()
        return lk


_NOCACHE: bool | None = None


def _nocache() -> bool:
    """``ASTRO_SMB_GUI_NOCACHE=1``:忽略已存在的缓存文件,强制重新下载。

    进程内只读一次环境变量并记住 —— 同一次运行里语义必须稳定,否则同一批
    下载有的复用有的重下,耗时就没有可比性了。
    """
    global _NOCACHE
    if _NOCACHE is None:
        _NOCACHE = os.environ.get("ASTRO_SMB_GUI_NOCACHE", "").strip() not in ("", "0")
    return _NOCACHE


def download_cached(client, share: str, path: str, dest: Path,
                    cancel: threading.Event | None = None, *,
                    progress=None, tmp_suffix: str = ".part") -> None:
    """下载到 ``dest``,但先写临时文件再 ``os.replace`` 原子改名。

    取消/出错不会留下会被误判为"已完整"的半截缓存文件(曾是 bug)。
    ``dest`` 已存在则直接跳过。

    **按 dest 加锁**:预览线程和 FITS 查看器用的是**同一个** ``cache/<sha1>.fit``
    (cache_key 相同,本来就是为了共用一份 50MB 原图)。浏览页点"下载原图并生成
    拉伸预览"后立刻双击同一文件,两条线程会各下一份、再各自 ``os.replace``;
    只要一方的 replace 落在另一方 ``np.fromfile`` 打开该文件期间,Windows 上就是
    共享冲突。加锁后**第二个下载者拿到锁先重判 dest 是否已存在**,直接复用
    第一个的结果(顺带省掉一次 50MB 传输)。

    ``tmp_suffix`` 仍然保留给调用方做语义区分,并**再附一个进程内唯一序号** ——
    同一组件两次并发加载(不同 dest)也不会撞到同一个临时名。
    """
    dest = Path(dest)
    # ASTRO_SMB_GUI_NOCACHE=1: 强制重下, 用来复现"第一次打开"的真实耗时
    # (不必删掉用户的缓存目录)。**进程内只判一次**: 同一次运行里语义要稳定,
    # 否则同一批下载有的复用有的重下, 计时没有可比性。
    fresh = _nocache()
    if dest.exists() and not fresh:
        return
    with _dest_lock(dest):
        if dest.exists() and not fresh:
            return          # 别人刚下完:复用,不重复拉一遍
        tmp = dest.with_name(f"{dest.name}{tmp_suffix}{next(_tmp_seq)}")
        try:
            client.download_file(share, path, tmp, progress=progress, cancel=cancel)
            os.replace(tmp, dest)
        except BaseException:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise


def _hdr_to_payload(hdr: FitsHeader) -> dict:
    return {
        "cards": hdr.cards,
        "order": [[k, v, c] for k, v, c in hdr.order],
        "complete": bool(hdr.complete),
        "header_bytes": int(hdr.header_bytes),
    }


def _hdr_from_payload(d: dict) -> FitsHeader | None:
    """反序列化;结构不对(旧版本 payload/被改坏)一律返回 None 走重读。"""
    try:
        cards = d["cards"]
        order = d["order"]
        if not isinstance(cards, dict) or not isinstance(order, list):
            return None
        return FitsHeader(
            cards={str(k): str(v) for k, v in cards.items()},
            order=[(str(t[0]), str(t[1]), str(t[2])) for t in order],
            complete=bool(d.get("complete")),
            header_bytes=int(d.get("header_bytes") or 0),
        )
    except Exception:
        return None


def read_fits_header(client: AstroSmbClient, entry: RemoteEntry, *,
                     backend_id: str | None = None,
                     use_cache: bool = True) -> FitsHeader:
    """从 SMB 部分读取拿全 FITS 头(几 KB,不下载原图)。供预览与列表懒加载共用。

    ``use_cache=True`` 时先查 **metacache 磁盘元数据缓存**(kind=``fitshdr``,
    key=``share|path``,以文件 size+mtime 为源指纹)—— 命中即零 SMB 往返返回,
    重启应用/换目录再回来都不用重读头(一张头 = 1~2 次 SMB 往返 × RTT,
    一个目录几百张时差别是"秒级 vs 十几秒")。``backend_id`` 缺省取
    ``client.host``,不同设备天然隔离。

    **必须在工作线程调用**(含 SMB 与 sqlite I/O)。
    """
    backend = backend_id if backend_id is not None else (
        getattr(client, "host", "") or "")
    key = f"{entry.share}|{entry.path}"
    if use_cache:
        try:
            hit = metacache.get(FITS_HDR_KIND, backend, key,
                                src_size=entry.size, src_mtime=entry.mtime)
        except Exception:
            hit = None          # 缓存永远是可选的,坏了就当没有
        if hit is not None:
            hdr = _hdr_from_payload(hit)
            if hdr is not None:
                return hdr

    probe = client.read_bytes(entry.share, entry.path, 0, BLOCK * 2)
    need = header_read_hint(probe)
    while need:
        more = client.read_bytes(entry.share, entry.path, len(probe), need)
        if not more:
            break  # 文件读尽仍无 END 卡,防止死循环
        probe += more
        need = header_read_hint(probe)
    hdr = parse_fits_header(probe)
    # 只缓存**读全了**的头(没读到 END 的是截断/非 FITS,缓存下来会以讹传讹)
    if use_cache and hdr.complete and hdr.cards:
        try:
            metacache.put(FITS_HDR_KIND, backend, key, _hdr_to_payload(hdr),
                          src_size=entry.size, src_mtime=entry.mtime)
        except Exception:
            pass
    return hdr


def invalidate_fits_headers(backend_id: str | None = None) -> int:
    """清掉 FITS 头缓存(整体或某设备)。正常无需调用——源指纹会自动失效。"""
    try:
        return metacache.invalidate(FITS_HDR_KIND, backend_id)
    except Exception:
        return 0


@dataclass
class PreviewResult:
    token: int
    entry: RemoteEntry
    kind: str = "meta"          # meta | image | text | error
    thumb_path: str | None = None
    image_size: tuple[int, int] | None = None   # 原图尺寸
    text: str = ""              # kind == "text" 时的内容
    fits: FitsHeader | None = None
    thumb_source: str = ""      # 预览图来源说明(如 "ASIAIR 缩略图")
    can_load_full: bool = False  # 提供"生成预览"按钮(大 FITS)
    error: str = ""
    extra: list[tuple[str, str]] = field(default_factory=list)


class PreviewWorker:
    """单工作线程,仅处理最新请求。结果通过 on_result(PreviewResult) 上报
    (在工作线程调用,UI 层负责编组)。"""

    def __init__(self, client_factory, on_result):
        self._factory = client_factory
        self._on_result = on_result
        self._cond = threading.Condition()
        self._pending: tuple[int, RemoteEntry, bool] | None = None
        self._stop = False
        self._reset = False
        self._client: AstroSmbClient | None = None
        self._cancel_current = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="preview")
        self._thread.start()

    def request(self, token: int, entry: RemoteEntry, want_full: bool = False) -> None:
        with self._cond:
            self._pending = (token, entry, want_full)
            self._cancel_current.set()  # 让正在进行的大下载尽快让路
            self._cond.notify()

    def reset(self) -> None:
        """主机变更后调用:丢弃缓存连接,下次请求重新克隆。"""
        with self._cond:
            self._reset = True
            self._cancel_current.set()
            self._cond.notify()

    def shutdown(self) -> None:
        with self._cond:
            self._stop = True
            self._cancel_current.set()
            self._cond.notify()

    # ---------- 工作线程 ----------

    def _loop(self) -> None:
        while True:
            with self._cond:
                while self._pending is None and not self._stop:
                    self._cond.wait()
                if self._stop:
                    break
                token, entry, want_full = self._pending
                self._pending = None
                self._cancel_current = threading.Event()
                if self._reset:
                    self._reset = False
                    if self._client is not None:
                        try:
                            self._client.close()
                        except Exception:
                            pass
                        self._client = None
            try:
                result = self._build(token, entry, want_full, self._cancel_current)
            except SmbClientError as e:
                result = PreviewResult(token=token, entry=entry, kind="error", error=str(e))
            except Exception as e:
                result = PreviewResult(token=token, entry=entry, kind="error",
                                       error=f"{type(e).__name__}: {e}")
            if result is not None and not self._cancel_current.is_set():
                try:
                    self._on_result(result)
                except Exception:
                    pass
        if self._client is not None:
            self._client.close()

    def _cli(self) -> AstroSmbClient:
        if self._client is None:
            self._client = self._factory()
            self._client.connect()
        return self._client

    def _download_cached(self, share: str, path: str, dest: Path,
                         cancel: threading.Event) -> None:
        download_cached(self._cli(), share, path, dest, cancel)

    def _key(self, entry: RemoteEntry, tag: str = "") -> str:
        return cache_key(getattr(self._cli(), "host", ""), entry, tag)

    def _build(self, token: int, entry: RemoteEntry, want_full: bool,
               cancel: threading.Event) -> PreviewResult | None:
        if entry.is_dir:
            return PreviewResult(token=token, entry=entry, kind="meta")
        ext = os.path.splitext(entry.name)[1].lower()
        if ext in FITS_EXTS:
            return self._build_fits(token, entry, want_full, cancel)
        if ext in IMAGE_EXTS:
            return self._build_image(token, entry, cancel)
        if ext in TEXT_EXTS:
            return self._build_text(token, entry)
        # 未知类型:嗅探开头字节,可能是 FITS/文本
        head = self._cli().read_bytes(entry.share, entry.path, 0, 2880)
        if head.startswith(b"SIMPLE"):
            return self._build_fits(token, entry, want_full, cancel)
        if head and all(b in (9, 10, 13) or 32 <= b < 0xFF for b in head[:512]):
            return self._build_text(token, entry)
        return PreviewResult(token=token, entry=entry, kind="meta")

    # ----- 各类型 -----

    def _read_fits_header(self, entry: RemoteEntry) -> FitsHeader:
        return read_fits_header(self._cli(), entry)

    def _build_fits(self, token: int, entry: RemoteEntry, want_full: bool,
                    cancel: threading.Event) -> PreviewResult:
        hdr = self._read_fits_header(entry)
        result = PreviewResult(token=token, entry=entry, kind="meta", fits=hdr)
        shape = hdr.naxis
        if len(shape) >= 2:
            result.image_size = (shape[0], shape[1])

        if want_full:
            thumb, source = self._render_fits_thumb(entry, hdr, cancel)
            if thumb:
                result.kind = "image"
                result.thumb_path = thumb
                result.thumb_source = source
                return result

        # 优先 ASIAIR 生成的 _thn.jpg
        stem, _ext = os.path.splitext(entry.path)
        thn_path = f"{stem}_thn.jpg"
        c = self._cli()
        try:
            thn = c.stat(entry.share, thn_path)
            if not thn.is_dir and 0 < thn.size <= (5 << 20):
                local = cache_dir() / f"{self._key(thn)}.jpg"
                self._download_cached(entry.share, thn_path, local, cancel)
                thumb = self._make_thumb(local, self._key(thn, "thumb"))
                if thumb:
                    result.kind = "image"
                    result.thumb_path = thumb
                    result.thumb_source = _("ASIAIR 缩略图 (_thn.jpg)")
                    return result
        except SmbClientError:
            pass

        result.can_load_full = True
        result.thumb_source = _("原图 {0},点击按钮生成拉伸预览").format(human_size(entry.size))
        return result

    _FULL_SRC = N_("FITS 全图预览(超像素去马赛克 + STF 拉伸)")
    _FULL_SRC_CUBE = N_("FITS 全图预览(RGB 立方体 + STF 拉伸)")
    _FULL_SRC_MONO = N_("FITS 全图预览(单色 + STF 拉伸)")
    _FULL_SRC_LEGACY = N_("FITS 全图预览(灰度百分位拉伸)")

    @classmethod
    def _full_src(cls, debayered: bool, channels: int) -> str:
        """按**实际**做了什么选来源说明。

        单色相机(ASI1600MM/2600MM/6200MM)不写 BAYERPAT,``debayered=False``、
        通道数 1 —— 这时再说"超像素去马赛克"就是睁眼说瞎话
        (本函数存在的全部理由就是"界面上别说去马赛克其实是灰度")。
        """
        if debayered:
            return cls._FULL_SRC
        return cls._FULL_SRC_CUBE if channels >= 3 else cls._FULL_SRC_MONO

    def _render_fits_thumb(self, entry: RemoteEntry, hdr: FitsHeader,
                           cancel: threading.Event) -> tuple[str | None, str]:
        """下载整个 FITS 并生成拉伸预览图,返回 (图片路径, 来源说明)。

        主路径走 :mod:`astro_smb.fitsimage`:Bayer 超像素去马赛克 + STF 自动拉伸
        → **彩色**预览(以前恒是灰度,ASIAIR 的 OSC 原图看着像黑白)。
        遇到不认识的结构(奇怪的 BITPIX/维度)回落到老的灰度百分位实现,
        来源说明也跟着换,免得界面上说"去马赛克"其实是灰度。
        """
        from PIL import Image

        from astro_smb import fitsimage as fi

        try:
            geom = fi.geometry_from_header(hdr)
        except fi.FitsImageError:
            return (self._render_fits_thumb_legacy(entry, hdr, cancel),
                    self._FULL_SRC_LEGACY)
        # 缓存命中时没有 LinearImage 可问,按几何推同样的结论
        # (load_linear 的 debayered 判据就是 bayer_effective 非空)
        src_text = self._full_src(geom.bayer_effective is not None, geom.planes)
        thumb_file = cache_dir() / f"{self._key(entry, 'fitsthumb2')}.png"
        if thumb_file.exists():
            return str(thumb_file), src_text
        raw_file = cache_dir() / f"{self._key(entry)}.fit"
        self._download_cached(entry.share, entry.path, raw_file, cancel)
        if cancel.is_set():
            return None, ""
        try:
            img = fi.load_linear(raw_file, hdr, cancel=cancel)
            if cancel.is_set():
                return None, ""
            src_text = self._full_src(img.debayered, img.channels)
            step = max(1, max(img.height, img.width) // THUMB_SIZE)
            small = img.rgb[::step, ::step]
            # 统计取自全图抽样(不是抽稀后的小图),缩略图与查看器口径一致
            stats = fi.compute_stats(img.sample, fi.StretchParams())
            rgb8, _st = fi.stretch(small, fi.StretchParams(), unit=img.unit,
                                 stats=stats, mono_out=True)
            pil = Image.fromarray(rgb8, mode="L" if rgb8.ndim == 2 else "RGB")
        except fi.FitsImageError:
            return (self._render_fits_thumb_legacy(entry, hdr, cancel),
                    self._FULL_SRC_LEGACY)
        tmp = thumb_file.with_name(thumb_file.name + ".part")
        try:
            pil.save(tmp, format="PNG")
            os.replace(tmp, thumb_file)
        except BaseException:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise
        return str(thumb_file), src_text

    def _render_fits_thumb_legacy(self, entry: RemoteEntry, hdr: FitsHeader,
                                  cancel: threading.Event) -> str | None:
        """兜底:老的灰度百分位拉伸(fitsimage 认不出结构时用)。"""
        import numpy as np
        from PIL import Image

        shape = hdr.naxis
        if (len(shape) < 2 or hdr.bitpix not in (8, 16, 32, -32)
                or not hdr.complete or hdr.header_bytes <= 0):
            return None
        thumb_file = cache_dir() / f"{self._key(entry, 'fitsthumb')}.png"
        if thumb_file.exists():
            return str(thumb_file)
        raw_file = cache_dir() / f"{self._key(entry)}.fit"
        self._download_cached(entry.share, entry.path, raw_file, cancel)
        if cancel.is_set():
            return None

        w, h = shape[0], shape[1]
        dtype = {8: ">u1", 16: ">i2", 32: ">i4", -32: ">f4"}[hdr.bitpix]
        count = w * h
        data = np.fromfile(raw_file, dtype=dtype, count=count, offset=hdr.header_bytes)
        if data.size < count:
            return None
        img = data.reshape(h, w).astype(np.float32)
        try:
            bzero = float(hdr.get("BZERO", "0") or 0)
            bscale = float(hdr.get("BSCALE", "1") or 1)
        except ValueError:
            bzero, bscale = 0.0, 1.0
        img = img * bscale + bzero

        step = max(1, max(w, h) // THUMB_SIZE)
        img = img[::step, ::step]
        lo, hi = np.percentile(img, (0.2, 99.8))
        if hi <= lo:
            hi = lo + 1
        img8 = np.clip((img - lo) / (hi - lo) * 255, 0, 255).astype(np.uint8)
        tmp = thumb_file.with_name(thumb_file.name + ".part")
        Image.fromarray(img8, mode="L").save(tmp, format="PNG")
        os.replace(tmp, thumb_file)
        return str(thumb_file)

    def _build_image(self, token: int, entry: RemoteEntry,
                     cancel: threading.Event) -> PreviewResult:
        result = PreviewResult(token=token, entry=entry, kind="meta")
        if entry.size > IMAGE_MAX:
            result.extra.append((_("提示"), _("图片超过 {0},不自动预览").format(
                human_size(IMAGE_MAX))))
            return result
        ext = os.path.splitext(entry.name)[1].lower()
        local = cache_dir() / f"{self._key(entry)}{ext}"
        self._download_cached(entry.share, entry.path, local, cancel)
        if cancel.is_set():
            return result
        try:
            from PIL import Image
            with Image.open(local) as im:
                result.image_size = im.size
        except Exception:
            pass
        thumb = self._make_thumb(local, self._key(entry, "thumb"))
        if thumb:
            result.kind = "image"
            result.thumb_path = thumb
            result.thumb_source = _("原图缩略")
        return result

    def _make_thumb(self, local: Path, key: str) -> str | None:
        from PIL import Image

        out = cache_dir() / f"{key}.png"
        if out.exists():
            return str(out)
        tmp = out.with_name(out.name + ".part")
        try:
            with Image.open(local) as im:
                im.thumbnail((THUMB_SIZE, THUMB_SIZE))
                if im.mode not in ("RGB", "L", "RGBA"):
                    im = im.convert("RGB")
                im.save(tmp, format="PNG")
            os.replace(tmp, out)
            return str(out)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return None

    def _build_text(self, token: int, entry: RemoteEntry) -> PreviewResult:
        size = min(entry.size, TEXT_MAX)
        data = self._cli().read_bytes(entry.share, entry.path, 0, size) if size else b""
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = data.decode("gbk")
            except UnicodeDecodeError:
                text = data.decode("utf-8", errors="replace")
        lines = text.splitlines()
        clipped = len(lines) > 400 or entry.size > TEXT_MAX
        text = "\n".join(lines[:400])
        if clipped:
            text += _('\n… (仅显示前 400 行 / {0})').format(human_size(TEXT_MAX))
        return PreviewResult(token=token, entry=entry, kind="text", text=text)


def clear_cache(max_bytes: int = 500 << 20, *, drop_dragout: bool = True) -> None:
    """缓存超过上限时按最旧访问时间清理顶层文件到一半;幂等,可重复调用。

    启动时调用一次(``drop_dragout=True``,顺便清空拖出暂存目录 —— 启动时不可能
    有拖拽在进行)。**运行中也要调**:FITS 查看器每打开一张 SMB 上的图就在这里
    留一份 49.8MB 原图,一晚翻 60 张就是 3GB,而本项目的 GUI 是常开设计
    (watcher/心跳都常驻),只在启动时裁等于永远不裁。运行中调必须
    ``drop_dragout=False`` —— 那个目录里可能正有一次拖拽在读文件。
    """
    import shutil

    d = cache_dir()
    if drop_dragout:
        # 拖出暂存的整份文件副本不计入 500MB 账,只能在这里整棵删掉
        shutil.rmtree(d / "dragout", ignore_errors=True)

    files = []
    for f in d.iterdir():
        try:
            st = f.stat()
        except OSError:
            continue            # 并发下被别人删掉/占用,跳过
        if f.is_file():
            files.append((st.st_atime, st.st_size, f))
    total = sum(s for _at, s, _f in files)
    if total <= max_bytes:
        return
    files.sort(key=lambda t: (t[0], str(t[2])))
    for _at, s, f in files:
        try:
            f.unlink()
            total -= s
        except OSError:
            pass        # 正被别的线程 np.fromfile 打开的文件删不掉,下次再说
        if total <= max_bytes // 2:
            break
