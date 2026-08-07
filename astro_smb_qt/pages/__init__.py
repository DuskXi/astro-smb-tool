"""九页。**加一页 = 加一个模块 + 在这里注册**,外壳零改动。

顺序与 tag 和另外两套前端一致(用户的肌肉记忆在导航那一列上)。
"""
from __future__ import annotations

from astro_smb_qt.pages.browser import BrowserPage
from astro_smb_qt.pages.devices import DevicesPage
from astro_smb_qt.pages.guiding import GuidingPage
from astro_smb_qt.pages.records import RecordsPage
from astro_smb_qt.pages.scan import ScanPage
from astro_smb_qt.pages.space import SpacePage
from astro_smb_qt.pages.fitsview import FitsViewPage
from astro_smb_qt.pages.sky3d import Sky3DPage
from astro_smb_qt.pages.transfers import TransfersPage

PAGE_CLASSES = {
    "browse": BrowserPage,
    "records": RecordsPage,
    "guiding": GuidingPage,
    "sky": Sky3DPage,
    "fits": FitsViewPage,
    "space": SpacePage,
    "devices": DevicesPage,
    "scan": ScanPage,
    "transfers": TransfersPage,
}


def build_pages(shell) -> dict:
    return {tag: cls(shell) for tag, cls in PAGE_CLASSES.items()}


__all__ = ["PAGE_CLASSES", "build_pages"]
