"""禁用态必须**看得出来**。

独立验收员在导星页发现:选中校准行时「窗口」下拉与位置滑杆确实
`setEnabled(False)` 了 —— 输入真的被挡住 —— 但屏幕上**零提示**。
他把启用/禁用两张截图做逐像素比对,`getbbox()` 返回 `None`:
**一个像素都没变**。

原因不是忘了调 `setEnabled`,而是 QSS:**一旦给控件写了颜色,
Qt 就不再对它自动做禁用态变暗**。`QPushButton:disabled` 早就写了,
`QComboBox`/`QSlider`/`QLineEdit`/`QCheckBox` 漏了。

这份闸门**照验收员的办法测** —— 真渲染、真比像素。只断言样式表里
有没有 `:disabled` 字样是不够的:写了一条颜色和启用态相同的规则,
字符串在、像素还是不变。
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6")


@pytest.fixture(scope="module", autouse=True)
def qt_app():
    from PySide6.QtWidgets import QApplication

    from astro_smb_qt import theme

    inst = QApplication.instance() or QApplication([])
    theme.apply(inst)
    return inst


def _differs(make) -> bool:
    """同一个控件启用/禁用各画一遍,像素有没有变。"""
    w = make()
    w.resize(160, 28)
    w.setEnabled(True)
    on = w.grab().toImage()
    w.setEnabled(False)
    off = w.grab().toImage()
    if on.size() != off.size():
        return True
    for x in range(0, on.width(), 2):
        for y in range(0, on.height(), 2):
            if on.pixelColor(x, y) != off.pixelColor(x, y):
                return True
    return False


def _combo():
    from PySide6.QtWidgets import QComboBox

    cb = QComboBox()
    cb.addItems(["2026-07-29 · 2 目标 · 59 帧"])
    return cb


def _slider():
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QSlider

    s = QSlider(Qt.Horizontal)
    s.setRange(0, 100)
    s.setValue(45)
    return s


def _line():
    from PySide6.QtWidgets import QLineEdit

    e = QLineEdit()
    e.setText("192.0.2.227")
    return e


def _check():
    from PySide6.QtWidgets import QCheckBox

    c = QCheckBox("足迹")
    c.setChecked(True)
    return c


def _button():
    from PySide6.QtWidgets import QPushButton

    return QPushButton("刷新")


CONTROLS = [("下拉", _combo), ("滑杆", _slider), ("输入框", _line),
            ("勾选框", _check), ("按钮", _button)]


@pytest.mark.parametrize("name,make", CONTROLS, ids=[c[0] for c in CONTROLS])
def test_disabled_looks_different(name, make):
    """**这条是像素级的。** 只查样式表里有 `:disabled` 挡不住
    "写了一条和启用态同色的规则"。"""
    assert _differs(make), f"{name} 禁用之后一个像素都没变 —— 用户看不出它失效了"


@pytest.mark.parametrize("name,make", CONTROLS, ids=[c[0] for c in CONTROLS])
def test_it_holds_in_every_theme(name, make):
    """三档配色各自都要有禁用态 —— 配色是三份,规则却只有一份,
    某一档的 `TEXT_FAINT` 恰好和 `TEXT` 撞上就白写了。"""
    from astro_smb_qt import theme

    before = theme.C.mode
    try:
        for mode in theme.MODES:
            theme.set_mode(mode)
            assert _differs(make), f"{mode} 档的{name}禁用后没有变化"
    finally:
        theme.set_mode(before)


def test_faint_is_actually_fainter_than_text():
    """禁用态借的是 `TEXT_FAINT`。它要真的比正文淡,否则上面那些
    像素比对能过、人眼还是看不出来。"""
    from PySide6.QtGui import QColor

    from astro_smb_qt import theme

    before = theme.C.mode
    try:
        for mode in theme.MODES:
            theme.set_mode(mode)
            bg = QColor(theme.Q.SURFACE)
            t = QColor(theme.Q.TEXT)
            f = QColor(theme.Q.TEXT_FAINT)

            def contrast(c):
                return abs((0.299 * c.red() + 0.587 * c.green()
                            + 0.114 * c.blue())
                           - (0.299 * bg.red() + 0.587 * bg.green()
                              + 0.114 * bg.blue()))

            assert contrast(f) < contrast(t), (
                f"{mode} 档 TEXT_FAINT 并不比 TEXT 淡")
    finally:
        theme.set_mode(before)
