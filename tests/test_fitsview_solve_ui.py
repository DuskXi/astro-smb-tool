"""FITS 查看器板解算 UI 的纯逻辑/接线回归。"""
from pathlib import Path

import numpy as np
import pytest

from astro_smb_gui import _fitsview as F


def test_matched_fits_coordinates_flip_to_display():
    got = F._matched_to_display(
        np.array([[1.0, 4.0], [8.0, 1.0]]),
        raw_width=8, raw_height=4,
        display_width=4, display_height=2,
        flip_vertical=True)
    assert got[0] == pytest.approx((0.0, 0.0))
    assert got[1] == pytest.approx((3.5, 1.5))


def test_matched_coordinates_respect_nonflipped_storage():
    got = F._matched_to_display(
        [[1.0, 1.0], [8.0, 4.0]], 8, 4, 8, 4, False)
    assert np.allclose(got, [[0.0, 0.0], [7.0, 3.0]])


def test_xaml_exposes_catalog_download_action():
    xaml = Path(F.XAML_PATH).read_text(encoding="utf-8")
    source = Path(F.__file__).read_text(encoding="utf-8")
    assert 'x:Name="CatalogDownloadBtn"' in xaml
    assert "self.catalog_download_btn.Click += self._on_catalog_download" in source
