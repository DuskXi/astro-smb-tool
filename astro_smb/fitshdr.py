"""极简 FITS 头解析器(不依赖 astropy)。

FITS 头由 2880 字节块组成,每块 36 张 80 字节卡片,遇到 'END' 卡片结束。
用于低开销预览:只需从 SMB 读取头部若干 KB 即可拿到曝光/增益/温度等元数据。
"""

from __future__ import annotations

from dataclasses import dataclass, field

BLOCK = 2880
CARD = 80
MAX_HEADER_BLOCKS = 64  # 防御:最多解析 64 块(184 KB)

# ASIAIR 拍摄参数里最值得优先展示的键
INTERESTING_KEYS = [
    "IMAGETYP", "EXPTIME", "EXPOSURE", "GAIN", "CCD-TEMP", "SET-TEMP",
    "XBINNING", "YBINNING", "FILTER", "DATE-OBS", "INSTRUME", "TELESCOP",
    "FOCALLEN", "OBJECT", "RA", "DEC", "BAYERPAT", "NAXIS1", "NAXIS2",
]


@dataclass
class FitsHeader:
    cards: dict[str, str] = field(default_factory=dict)  # 原始值字符串(已去引号)
    order: list[tuple[str, str, str]] = field(default_factory=list)  # (key, value, comment)
    complete: bool = False  # 是否读到了 END
    header_bytes: int = 0  # 头部占用字节数(含补齐)

    def get(self, key: str, default: str | None = None) -> str | None:
        return self.cards.get(key, default)

    @property
    def naxis(self) -> tuple[int, ...]:
        try:
            n = int(self.cards.get("NAXIS", "0"))
            return tuple(int(self.cards[f"NAXIS{i}"]) for i in range(1, n + 1))
        except (KeyError, ValueError):
            return ()

    @property
    def bitpix(self) -> int:
        try:
            return int(self.cards.get("BITPIX", "0"))
        except ValueError:
            return 0

    def data_size(self) -> int:
        """主 HDU 数据区字节数(不含补齐)。"""
        shape = self.naxis
        if not shape or not self.bitpix:
            return 0
        n = abs(self.bitpix) // 8
        for dim in shape:
            n *= dim
        return n

    def summary(self) -> list[tuple[str, str]]:
        """挑出常用拍摄参数,按 INTERESTING_KEYS 顺序。"""
        out = []
        for k in INTERESTING_KEYS:
            if k in self.cards:
                out.append((k, self.cards[k]))
        return out


def _parse_card(raw: bytes) -> tuple[str, str, str] | None:
    """返回 (key, value, comment);注释卡/空卡返回 None。"""
    try:
        text = raw.decode("ascii", errors="replace")
    except Exception:
        return None
    key = text[:8].strip()
    if not key or key in ("COMMENT", "HISTORY"):
        return None
    if text[8:10] != "= ":
        return None
    body = text[10:]
    value, comment = body, ""
    if body.lstrip().startswith("'"):
        # 字符串值:找到闭合引号('' 转义)
        s = body.lstrip()
        i, buf = 1, []
        while i < len(s):
            if s[i] == "'":
                if i + 1 < len(s) and s[i + 1] == "'":
                    buf.append("'")
                    i += 2
                    continue
                break
            buf.append(s[i])
            i += 1
        value = "".join(buf).rstrip()
        rest = s[i + 1:]
        if "/" in rest:
            comment = rest.split("/", 1)[1].strip()
    else:
        if "/" in body:
            value, comment = body.split("/", 1)
            comment = comment.strip()
        value = value.strip()
    return key, value, comment


def parse_fits_header(data: bytes) -> FitsHeader:
    """从字节流解析主 HDU 头。data 可以只是文件开头的一部分。"""
    hdr = FitsHeader()
    if len(data) < CARD or not data.startswith(b"SIMPLE"):
        return hdr
    nblocks = min(len(data) // BLOCK, MAX_HEADER_BLOCKS)
    # 不足一块也尽量解析(部分读取场景)
    limit = nblocks * BLOCK if nblocks else (len(data) // CARD) * CARD
    for off in range(0, limit, CARD):
        raw = data[off:off + CARD]
        if raw[:3] == b"END" and raw[3:4] in (b" ", b""):
            hdr.complete = True
            hdr.header_bytes = ((off // BLOCK) + 1) * BLOCK
            break
        parsed = _parse_card(raw)
        if parsed:
            key, value, comment = parsed
            hdr.cards[key] = value
            hdr.order.append((key, value, comment))
    return hdr


def header_read_hint(probe: bytes) -> int:
    """给定已读的开头字节,估计还需要读多少才能拿全头部;0 表示已够。"""
    hdr = parse_fits_header(probe)
    if hdr.complete or not probe.startswith(b"SIMPLE"):
        return 0
    # 每次翻倍,最多 MAX_HEADER_BLOCKS 块
    want = min(max(len(probe) * 2, BLOCK * 2), BLOCK * MAX_HEADER_BLOCKS)
    return max(0, want - len(probe))
