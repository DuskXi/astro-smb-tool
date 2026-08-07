"""一份极小的用户设置(JSON,放在应用数据目录)。

**这个仓库原来一样设置都不记。** 配色档、语言,每次启动都回到默认 ——
对配色档还能忍(一次点两下),对**语言**不行:一个只会英文的用户每次开都得
先摸到那个下拉才能看懂界面。

刻意做得很小:

* 一个扁平的 dict,`get`/`set` 两个函数,写盘走 `.tmp` + `os.replace`
  (这个仓库为"半截文件被当成完整的"付过两次代价:预览缓存、分块下载);
* **读不出来就当默认**,绝不因为设置文件坏了让程序起不来 —— 设置是锦上添花,
  不是运行前提;
* 不做 schema、不做迁移。要加字段就加,老版本读到不认识的键会原样保留。

放在 `astro_smb_app` 而不是 `astro_smb_qt`:两套前端都要用
(老 UI 后面接语言切换时是同一份设置,不能各存各的)。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from astro_smb.paths import data_dir

_lock = threading.RLock()
_cache: dict[str, Any] | None = None

#: 语言。空串 = 跟系统走(`i18n.detect_language()`)。
KEY_LANGUAGE = "language"


def settings_path() -> Path:
    return data_dir() / "settings.json"


def _load() -> dict[str, Any]:
    global _cache

    with _lock:
        if _cache is not None:
            return _cache
        try:
            raw = settings_path().read_text(encoding="utf-8")
            data = json.loads(raw)
            _cache = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            # 文件不在 / 坏了 / 不是 dict —— 一律当空。**不抛**:
            # 设置读不出来不该让程序起不来。
            _cache = {}
        return _cache


def get(key: str, default: Any = None) -> Any:
    return _load().get(key, default)


def set(key: str, value: Any) -> None:      # noqa: A001 - 就叫 set 最顺手
    """写一个键并落盘。落盘失败不抛 —— 顶多是这次没记住。"""
    with _lock:
        data = dict(_load())
        data[key] = value
        _cache.update(data)                 # type: ignore[union-attr]
        p = settings_path()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, p)              # 原子改名,别留半截文件
        except OSError:
            pass


def reset_cache() -> None:
    """只给测试用:下次 `get` 重新读盘。"""
    global _cache

    with _lock:
        _cache = None


def apply_saved_language() -> str:
    """把记住的语言应用上;没记过就按系统猜。返回真正生效的那个。

    **必须在建界面之前调用。** 界面上的文案是在建控件那一刻翻好烤进去的,
    之后再切语言,已经建好的那些不会自己变(所以切语言要重启,
    见 `astro_smb_qt.shell.Shell._set_language`)。

    **`ASTRO_SMB_LANG` 压过存下来的设置。** 它是自动化/伪语言审计的显式
    覆盖口(`ASTRO_SMB_TEST_LANG=xx_PS`、截图脚本),让用户设置盖过它的话,
    一台设过语言的机器上那些工具就全失效了 —— 而且不报错。
    """
    import os

    from astro_smb import i18n

    if os.environ.get("ASTRO_SMB_LANG"):
        return i18n.set_language(None)      # 让 detect_language 去读环境
    return i18n.set_language(get(KEY_LANGUAGE) or None)
