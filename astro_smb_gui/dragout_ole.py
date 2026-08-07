"""野路子拖出(#3):win32 OLE 虚拟文件 + 下载即拖。

WinUI(非包身份)无法用延迟渲染把 SMB 文件拖到资源管理器,但可以用原生 OLE:
自己实现 IDataObject + IStream(Python COM 服务,win32more 支持),对外声明
CFSTR_FILEDESCRIPTORW(文件名/大小)与 CFSTR_FILECONTENTS(IStream)。资源管理器
落点时同步调用 IStream.Read,我们的 Read 按需从 SMB 拉取对应区间——真正的
"拖拽即下载",无需预暂存。

数据面(GetData→IStream→Read)已在真机上验证可取回正确字节;真实鼠标手势的
落点只能在有交互桌面时验证。COM 服务实现模式见 win32more ComClass。
"""

from __future__ import annotations

import threading
from ctypes import byref, memmove, sizeof

from win32more import ComClass
from win32more.Windows.Win32.Foundation import E_NOTIMPL, HRESULT, S_FALSE, S_OK
from win32more.Windows.Win32.System.Com import (
    DATADIR_GET,
    DVASPECT_CONTENT,
    FORMATETC,
    IDataObject,
    IStream,
    STATSTG,
    STGMEDIUM,
    STGTY_STREAM,
    TYMED_HGLOBAL,
    TYMED_ISTREAM,
)
from win32more.Windows.Win32.System.DataExchange import RegisterClipboardFormatW
from win32more.Windows.Win32.System.Memory import (
    GMEM_MOVEABLE,
    GlobalAlloc,
    GlobalLock,
    GlobalUnlock,
)
from win32more.Windows.Win32.System.Ole import (
    DROPEFFECT,
    DROPEFFECT_COPY,
)
from win32more.Windows.Win32.UI.Shell import (
    CFSTR_FILECONTENTS,
    CFSTR_FILEDESCRIPTORW,
    FD_FILESIZE,
    FD_PROGRESSUI,
    FILEDESCRIPTORW,
    FILEGROUPDESCRIPTORW,
    SHCreateStdEnumFmtEtc,
    SHDoDragDrop,
)

DV_E_FORMATETC = -2147221404
DATA_S_SAMEFORMATETC = 0x00040130
OLE_E_ADVISENOTSUPPORTED = -2147221501

CF_FILEDESCRIPTORW = RegisterClipboardFormatW(CFSTR_FILEDESCRIPTORW)
CF_FILECONTENTS = RegisterClipboardFormatW(CFSTR_FILECONTENTS)


class SmbStream(ComClass, IStream):
    """按需从 SMB 读取的只读 IStream。reader(offset, length) -> bytes。"""

    def __init__(self, size: int, reader):
        super().__init__()
        self._size = size
        self._reader = reader
        self._pos = 0

    def Read(self, pv, cb, pcbRead) -> HRESULT:
        want = min(int(cb), max(0, self._size - self._pos))
        try:
            data = self._reader(self._pos, want) if want else b""
        except Exception:
            data = b""
        n = len(data)
        if n:
            memmove(pv, data, n)
            self._pos += n
        if pcbRead:
            pcbRead[0] = n
        return S_OK if n == int(cb) else S_FALSE

    def Write(self, pv, cb, pcbWritten) -> HRESULT:
        return E_NOTIMPL

    def Seek(self, dlibMove, dwOrigin, plibNewPosition) -> HRESULT:
        if dwOrigin == 0:
            self._pos = int(dlibMove)
        elif dwOrigin == 1:
            self._pos += int(dlibMove)
        elif dwOrigin == 2:
            self._pos = self._size + int(dlibMove)
        if plibNewPosition:
            plibNewPosition[0] = self._pos
        return S_OK

    def Stat(self, pstatstg, grfStatFlag) -> HRESULT:
        st = pstatstg[0]
        memmove(byref(st), bytes(sizeof(STATSTG)), sizeof(STATSTG))
        st.type = STGTY_STREAM
        st.cbSize = self._size
        return S_OK

    def SetSize(self, n) -> HRESULT:
        return E_NOTIMPL

    def CopyTo(self, s, cb, r, w) -> HRESULT:
        return E_NOTIMPL

    def Commit(self, f) -> HRESULT:
        return S_OK

    def Revert(self) -> HRESULT:
        return S_OK

    def LockRegion(self, o, cb, t) -> HRESULT:
        return E_NOTIMPL

    def UnlockRegion(self, o, cb, t) -> HRESULT:
        return E_NOTIMPL

    def Clone(self, ppstm) -> HRESULT:
        return E_NOTIMPL


def _fgd_hglobal(files: list[tuple[str, int]]):
    """构造 N 个文件的 FILEGROUPDESCRIPTORW HGLOBAL。files=[(名字,大小)]。"""
    n = len(files)
    total = sizeof(FILEGROUPDESCRIPTORW) + n * sizeof(FILEDESCRIPTORW)
    h = GlobalAlloc(GMEM_MOVEABLE, total)
    p = GlobalLock(h)
    addr = p if isinstance(p, int) else p.value
    memmove(addr, bytes(total), total)
    fgd = FILEGROUPDESCRIPTORW.from_address(addr)
    fgd.cItems = n
    for i, (name, size) in enumerate(files):
        fd = fgd.fgd[i]
        fd.dwFlags = FD_FILESIZE | FD_PROGRESSUI
        fd.nFileSizeHigh = (size >> 32) & 0xFFFFFFFF
        fd.nFileSizeLow = size & 0xFFFFFFFF
        fd.cFileName = name
    GlobalUnlock(h)
    return h


class VirtualFileGroup(ComClass, IDataObject):
    """N 个虚拟文件的 IDataObject。每个文件的字节由对应 SmbStream 按需拉取。"""

    def __init__(self, files: list[tuple[str, int]], reader_factory):
        """files=[(显示名, 大小)];reader_factory(index) -> reader(offset,length)->bytes。"""
        super().__init__()
        self._files = files
        self._streams = [SmbStream(size, reader_factory(i))
                         for i, (name, size) in enumerate(files)]

    def GetData(self, pformatetcIn, pmedium) -> HRESULT:
        fe = pformatetcIn[0]
        med = pmedium[0]
        memmove(byref(med), bytes(sizeof(STGMEDIUM)), sizeof(STGMEDIUM))
        if fe.cfFormat == CF_FILECONTENTS and (fe.tymed & TYMED_ISTREAM):
            idx = fe.lindex if fe.lindex >= 0 else 0
            if idx >= len(self._streams):
                return DV_E_FORMATETC
            stream = self._streams[idx]
            stream.AddRef()
            med.tymed = TYMED_ISTREAM
            med.u.pstm = stream
            return S_OK
        if fe.cfFormat == CF_FILEDESCRIPTORW and (fe.tymed & TYMED_HGLOBAL):
            med.tymed = TYMED_HGLOBAL
            med.u.hGlobal = _fgd_hglobal(self._files)
            return S_OK
        return DV_E_FORMATETC

    def QueryGetData(self, pformatetc) -> HRESULT:
        fe = pformatetc[0]
        if fe.cfFormat == CF_FILECONTENTS and (fe.tymed & TYMED_ISTREAM):
            return S_OK
        if fe.cfFormat == CF_FILEDESCRIPTORW and (fe.tymed & TYMED_HGLOBAL):
            return S_OK
        return S_FALSE

    def EnumFormatEtc(self, dwDirection, ppenumFormatEtc) -> HRESULT:
        if dwDirection != DATADIR_GET:
            return E_NOTIMPL
        arr = (FORMATETC * 2)()
        for i, (cf, ty) in enumerate([(CF_FILEDESCRIPTORW, TYMED_HGLOBAL),
                                      (CF_FILECONTENTS, TYMED_ISTREAM)]):
            arr[i].cfFormat = cf
            arr[i].ptd = None
            arr[i].dwAspect = DVASPECT_CONTENT
            arr[i].lindex = -1
            arr[i].tymed = ty
        return SHCreateStdEnumFmtEtc(2, arr, ppenumFormatEtc)

    def GetDataHere(self, f, m) -> HRESULT:
        return E_NOTIMPL

    def GetCanonicalFormatEtc(self, i, o) -> HRESULT:
        return DATA_S_SAMEFORMATETC

    def SetData(self, f, m, r) -> HRESULT:
        return E_NOTIMPL

    def DAdvise(self, f, a, s, c) -> HRESULT:
        return OLE_E_ADVISENOTSUPPORTED

    def DUnadvise(self, c) -> HRESULT:
        return OLE_E_ADVISENOTSUPPORTED

    def EnumDAdvise(self, pp) -> HRESULT:
        return OLE_E_ADVISENOTSUPPORTED


def build_smb_dataobject(entries, client_factory) -> VirtualFileGroup:
    """从 RemoteEntry 列表构造虚拟文件 IDataObject。

    所有文件共用一个惰性连接的 SMB client(落点时资源管理器顺序读各流,无并发)。
    用完请调用 .close_client()(挂在返回对象上)。
    """
    # **大小必须现取,不能用列表里那个 size。**
    # 浏览页的目录列表可能来自磁盘索引缓存(为了"秒开"),里面的 size 可以是
    # 任意旧的 —— 上次访问时那一帧还在写、或者干脆是几天前的索引。
    # 而这里的 size 会写进 FILEDESCRIPTORW.nFileSize、并让 SmbStream.Read
    # 按它截断:偏小时资源管理器拿到的就是一个**静默截断**的文件,
    # 文件名与完整帧一模一样。这条路**没有并行下载那 16MiB 的门槛**,
    # 任何大小的文件都会中招(审查实证)。
    # 拖拽是模态的、开始前多几个 stat 往返可以接受;取不到就退回列表里的值。
    files = []
    probe = None
    try:
        probe = client_factory()
        probe.connect()
    except Exception:
        probe = None
    for e in entries:
        size = e.size
        if probe is not None:
            try:
                size = probe.stat(e.share, e.path).size
            except Exception:
                pass
        files.append((e.name, size))
    if probe is not None:
        try:
            probe.close()
        except Exception:
            pass

    lock = threading.Lock()
    holder = {"client": None, "closed": False}

    def get_client():
        with lock:
            if holder["closed"]:
                return None  # 已关闭:落点后迟到的 Read 直接失败,不再复活新连接
            if holder["client"] is None:
                c = client_factory()
                c.connect()
                holder["client"] = c
            return holder["client"]

    def reader_factory(i):
        entry = entries[i]

        def reader(offset, length):
            c = get_client()
            if c is None:
                return b""
            return c.read_bytes(entry.share, entry.path, offset, length)

        return reader

    obj = VirtualFileGroup(files, reader_factory)

    def close_client():
        with lock:
            holder["closed"] = True
            if holder["client"] is not None:
                try:
                    holder["client"].close()
                except Exception:
                    pass
                holder["client"] = None

    obj.close_client = close_client  # type: ignore[attr-defined]
    return obj


def do_drag(data_object, hwnd=None) -> tuple[int, int]:
    """在 STA UI 线程调用;模态阻塞直到落点完成。pdsrc=None 用 shell 默认拖源。"""
    effect = DROPEFFECT()
    hr = SHDoDragDrop(hwnd, data_object, None, DROPEFFECT_COPY, byref(effect))
    return hr, effect.value
