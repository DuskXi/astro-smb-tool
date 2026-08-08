"""**联网下载的总账** —— 这个软件会去网上取哪些东西,各自在哪、多大、有没有。

## 为什么要有这一层

三样资产原来各自埋在用到它的那一页里:星表在影像查看页(点「板解算」才
问你要不要下),巡天底图在 3D 天球页,three.js 连问都不问、页面自己悄悄下。
后果是:

* **想知道"这软件联不联网、联哪儿"得翻三个页面**,而这对一台架在野外、
  用手机热点的笔记本来说是要紧事;
* 想**提前**在有网的地方把东西备好,没有地方可以点;
* 某一样下坏了(半截文件、错误页),除了删缓存目录没有别的办法。

这一层只回答"有哪些、什么状态",**不碰界面**:两套前端消费同一份,
免得"星表多大"在两个地方各写一遍然后对不上。

## 判据是 key,不是标题

`Asset.key` 是身份(``"catalog"``/``"survey"``/``"three"``),`title` 是给人
看的、会被翻译。**这个仓库栽过四次**:拿显示文本当身份,一翻译就静默失效。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from astro_smb.i18n import N_, gettext as _

#: 下载函数的统一形状:``fn(progress=(done,total)->None, cancel=Event) -> Path``
#: 三样资产本来就都是这个签名 —— 不是为这一层新造的抽象。
Downloader = Callable[..., Path]


@dataclass(frozen=True)
class Asset:
    """一样要联网取的东西。

    `title` / `why` / `source_note` 用 :func:`N_` 标记而**不翻译** ——
    这是模块级常量,import 时求值一次;在这里翻的话语言切换之后
    它们永远停在旧语言上(`i18n.N_` 的文档里那条纪律)。
    """

    key: str
    title: str
    why: str
    source: str                 # 主机名,不翻译
    source_note: str            # 署名 / 许可,可能要翻
    size_hint: str
    required: bool              # False = 缺了只是降级,不是不能用

    def display_title(self) -> str:
        return _(self.title)

    def display_why(self) -> str:
        return _(self.why)


ASSETS: tuple[Asset, ...] = (
    Asset(
        key="catalog",
        title=N_("Tycho-2 星表"),
        why=N_("板解算要它。没有星表就认不出画面里是哪片天,"
               "星点叠加、足迹、极轴反解都跟着没有。"),
        source="cdsarc.cds.unistra.fr",
        source_note=N_("CDS / VizieR I/259 —— 星表的权威发布方,无再分发顾虑"),
        size_hint=N_("下载约 159 MB,构建后本机占 35.6 MB"),
        required=False,
    ),
    Asset(
        key="survey",
        title=N_("巡天底图"),
        why=N_("3D 天球的背景。没有它天球还是能转,只是背后是纯黑,"
               "看不出目标落在银河的哪一段。"),
        source="cdn.eso.org",
        source_note=N_("ESO/S. Brunier — GigaGalaxy Zoom (CC BY 4.0)"),
        size_hint=N_("约 8 MB"),
        required=False,
    ),
    Asset(
        key="three",
        title=N_("three.js"),
        why=N_("3D 天球的渲染库。没有它天球页退化成 QPainter 画的正射投影 —— "
               "能看,但不能转。"),
        source="unpkg.com",
        source_note=N_("three.js r160 (MIT)"),
        size_hint=N_("约 1.3 MB"),
        required=False,
    ),
)

#: key → Asset。**别拿 title 找** —— 见模块文档。
BY_KEY = {a.key: a for a in ASSETS}


def _catalog_state() -> tuple[bool, Path | None]:
    from astro_smb import catalog

    try:
        return bool(catalog.catalog_available()), catalog.catalog_path()
    except Exception:                   # noqa: BLE001 - 状态查询不许把界面带走
        return False, None


def _survey_state() -> tuple[bool, Path | None]:
    from astro_smb_app import skymap

    try:
        return bool(skymap.survey_available()), skymap.survey_path()
    except Exception:                   # noqa: BLE001
        return False, None


def _three_state() -> tuple[bool, Path | None]:
    from astro_smb_app.views import sky3d

    try:
        return bool(sky3d.three_ready()), sky3d.three_path()
    except Exception:                   # noqa: BLE001
        return False, None


_STATE = {"catalog": _catalog_state, "survey": _survey_state,
          "three": _three_state}


def downloader(key: str) -> Downloader:
    """取某一样的下载函数。**惰性 import** —— 这一层被界面在启动时就 import,
    而 `catalog` 那一支会拖进 numpy 与构建代码。"""
    if key == "catalog":
        from astro_smb import catalog

        return catalog.ensure_catalog
    if key == "survey":
        from astro_smb_app import skymap

        return skymap.download_survey
    if key == "three":
        from astro_smb_app.views import sky3d

        return sky3d.download_three
    raise KeyError(key)


def status(key: str) -> dict:
    """一样资产**现在**什么样。纯查询,不联网、不下载。

    `bytes` 是**磁盘上的实际占用**,不是估计值 —— 半截文件、错误页留下的
    小文件都能一眼看出来(`ready=False` 而 `bytes>0` 就是这种)。
    """
    asset = BY_KEY[key]
    ready, path = _STATE[key]()
    size = 0
    if path is not None:
        try:
            size = path.stat().st_size if path.is_file() else 0
        except OSError:
            size = 0
    return {
        "key": key,
        "ready": ready,
        "path": path,
        "bytes": size,
        "title": asset.display_title(),
        "why": asset.display_why(),
        "source": asset.source,
        "source_note": _(asset.source_note),
        "size_hint": _(asset.size_hint),
    }


def rows() -> list[dict]:
    """全部资产的状态,顺序固定(界面上不许每次刷新换位置)。"""
    return [status(a.key) for a in ASSETS]


def summary() -> tuple[int, int]:
    """``(已就绪, 总数)`` —— 给入口按钮上那个角标用。"""
    got = sum(1 for r in rows() if r["ready"])
    return got, len(ASSETS)


def state_line(row: dict) -> str:
    """一行人话的状态。**已就绪时也要说出占了多少地方** —— 这是"管理"页,
    不是"下载"页;用户来这里也可能是想知道缓存吃了多少盘。"""
    from astro_smb.util import human_size

    if row["ready"]:
        return _("已就绪 · {0}").format(human_size(row["bytes"]))
    if row["bytes"]:
        # 有文件但判定没就绪 = 半截 / 错误页。**要说出来** —— 否则用户看到
        # "未就绪"又下一遍,还是同一个坏文件。
        return _("文件不完整({0})—— 重新下载会覆盖它").format(
            human_size(row["bytes"]))
    return _("未下载 · {0}").format(row["size_hint"])


def remove(key: str) -> bool:
    """删掉本地那一份。删不掉(占用中/权限)返回 False,**不抛**。

    存在的理由是上面那条:下坏了的文件"看起来在",而重下会先撞上它。
    """
    _ready, path = _STATE[key]()
    if path is None or not path.is_file():
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False


def run(key: str, *, progress=None,
        cancel: threading.Event | None = None) -> Path:
    """下载某一样。进度是 ``progress(done, total)``,单位**字节**。

    三样资产的下载函数本来就都收 `progress` 与 `cancel`,这里只是按 key
    分发 —— 界面因此不必知道"星表在核心库、底图在共享层、three.js 在天球
    页的资产模块里"。
    """
    return downloader(key)(progress=progress, cancel=cancel)
