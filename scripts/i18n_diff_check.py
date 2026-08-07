"""**差分验证**:i18n 改写前后,中文下的视图模型输出必须逐字节相同。

`_()` 在源语言(中文)下是恒等函数,所以"包 `_()`"这一步理论上零行为变化。
真正有风险的是另一半:**f-string 改写成 `.format()`** —— 参数顺序、格式说明符
(`{x:.2f}`)、转换符(`!r`)任何一处搞错,出来的都是**一个看着挺像的错字符串**,
不报错、不崩溃。500 处里错一处,人眼是看不出来的。

做法:在 HEAD 的 git worktree 里跑一遍同样的代码,把一批视图模型的输出
序列化成 JSON,再和工作区的比。用真机离线镜像当输入 —— 合成数据走不到
那些分支。

用法::

    uv run python scripts/i18n_diff_check.py

**要求工作区是干净的以外的状态**(就是说:改动还没提交),否则两边一样,
这个脚本什么也证明不了 —— 它会直接说出来。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIRROR = ROOT / ".tmp" / "device" / "EMMC Images"

#: 在被测进程里跑的采样程序。**输入用真机镜像**,不用合成数据。
PROBE = r'''
import json, sys, io, os
sys.path.insert(0, sys.argv[1])
os.chdir(sys.argv[1])
out = {}

def rec(key, value):
    out[key] = value

# ---- 日志链路:records / guiding / sky3d / guidedash 全靠它
from astro_smb.autorunlog import aggregate_nights, parse_autorun_log
from astro_smb.phd2log import parse_phd2_log
from pathlib import Path

logdir = Path(sys.argv[2]) / "log"
autoruns, phd2 = [], []
for p in sorted(logdir.glob("Autorun_Log_*.txt")):
    if p.name.endswith("_CHN.txt"):
        continue
    autoruns.append(parse_autorun_log(p.read_text(encoding="utf-8",
                                                  errors="replace"), p.name))
for p in sorted(logdir.glob("PHD2_GuideLog_*.txt")):
    phd2.append(parse_phd2_log(p.read_text(encoding="utf-8", errors="replace")))
nights = aggregate_nights(autoruns)


from astro_smb_app.logstore import LogData

data = LogData(nights=nights, phd2_logs=phd2, autorun_logs=autoruns)

from astro_smb_app.views import records as rv
from astro_smb_app.views import guidedash as gd
from astro_smb_app.views import guiding as gv
from astro_smb_app.views import sky3d as sv
from astro_smb_app.views import browser as bv
from astro_smb_app.views import transfers as tv
from astro_smb_app.views import scan as scv

# ---- records:夜次汇总 / 时间轴 / 目标详情 / 副行 / 收尾状态
guide_map = rv._guide_map_for(nights, phd2)
for i, n in enumerate(nights):
    for name, args in (("summary", (n, guide_map, {})),
                       ("timeline", (n, phd2)),
                       ("layouts", (n,))):
        try:
            rec(f"records.{name}[{i}]", getattr(rv, f"_night_{name}")(*args))
        except Exception as e:
            rec(f"records.{name}[{i}]", f"!! {type(e).__name__}: {e}")
    for j, r in enumerate(n.runs):
        for name, args in (("_run_detail", (r, guide_map, {})),
                           ("_run_subline", (r, guide_map)),
                           ("_end_state", (r,))):
            try:
                rec(f"records.{name}[{i}][{j}]", getattr(rv, name)(*args))
            except Exception as e:
                rec(f"records.{name}[{i}][{j}]", f"!! {type(e).__name__}: {e}")

# ---- guiding:段准备(rows / groups / stat_rows / 图表)
prep = None
try:
    prep = gv._prepare(data)
    rec("guiding.rows", [ {k: v for k, v in r.items()
                           if k not in ("sec", "rms", "rng", "lost")}
                          for r in (prep.get("rows") or []) ])
    rec("guiding.groups", [ {k: v for k, v in g.items() if k != "items"}
                            for g in (prep.get("groups") or []) ])
    rec("guiding.summary", prep.get("summary"))
    rec("guiding.status", prep.get("status"))
except Exception as e:
    rec("guiding", f"!! {type(e).__name__}: {e}")

# ---- guidedash:组聚合 → 仪表盘文本 / 摘要模型(85 条文案在这里)
for gi, g in enumerate((prep or {}).get("groups") or []):
    try:
        agg = gd.aggregate_group(g, prep["rows"], data)
        rec(f"guidedash.text[{gi}]", gd.dashboard_text(agg))
        rec(f"guidedash.summary[{gi}]", gd.summary_model(agg))
    except Exception as e:
        rec(f"guidedash[{gi}]", f"!! {type(e).__name__}: {e}")

# ---- sky3d:夜次 → 目标
try:
    rec("sky3d.nights", sv._build_nights(data, {}))
except Exception as e:
    rec("sky3d.nights", f"!! {type(e).__name__}: {e}")

# ---- browser:天文详情卡(拿镜像里的真文件名 + 真 FITS 头)
from astro_smb.client import RemoteEntry
from astro_smb.fitshdr import parse_fits_header

mirror = Path(sys.argv[2])
names = []
for p in sorted(mirror.rglob("*_thn.jpg"))[:40]:
    names.append(p)
for p in sorted(mirror.rglob("*.fit"))[:4]:
    names.append(p)
for k, p in enumerate(names):
    e = RemoteEntry(share="EMMC Images", path=str(p), name=p.name,
                    is_dir=False, size=p.stat().st_size,
                    mtime=p.stat().st_mtime, ctime=0.0, atime=0.0,
                    attributes=0)
    hdr = None
    if p.suffix.lower() == ".fit":
        try:
            hdr = parse_fits_header(p.read_bytes()[:200000])
        except Exception:
            hdr = None
    try:
        rec(f"browser.details[{k}]", bv._astro_details(e, hdr, (30.0, 121.44)))
    except Exception as ex:
        rec(f"browser.details[{k}]", f"!! {type(ex).__name__}: {ex}")
    try:
        rec(f"browser.symbol[{k}]", bv.entry_symbol(e))
    except Exception as ex:
        rec(f"browser.symbol[{k}]", f"!! {type(ex).__name__}: {ex}")

# ---- browser:判读文本(阈值边界逐个走一遍)
for a in (-5.0, 0.0, 0.1, 12.0, 19.9, 20.0, 35.5, 39.9, 40.0, 60.0, 89.9):
    rec(f"browser.alt[{a}]", [bv._alt_hint(a), bv._airmass_text(a),
                              bv._airmass_note(a), bv._alt_tone(a),
                              bv._airmass_tone(a)])
for s in (0.3, 0.69, 0.7, 1.0, 2.0, 2.01, 5.0):
    rec(f"browser.sampling[{s}]", list(bv._sampling_verdict(s)))
for az in (0.0, 11.2, 11.3, 22.5, 90.0, 180.0, 349.0, 359.9):
    rec(f"browser.az[{az}]", bv._az_name(az))

# ---- transfers:行模型 / 分区 / 时长
from astro_smb_app import transfers as X
for st, done, blocks in ((X.QUEUED, 0, None), (X.RUNNING, 3_000_000, [2]*20+[1]*8+[0]*36),
                         (X.DONE_S, 9_000_000, None), (X.ERROR, 1, None),
                         (X.CANCELLED, 1, None), (X.SKIPPED, 0, None)):
    j = X.TransferJob(kind="download", label="片子.fit", total=9_000_000, done=done)
    j.status = st
    j.phase = X.PH_TRANSFER if st == X.RUNNING else st
    if blocks:
        j.speed, j.parallel, j.n_chunks, j.blocks = 6.2e6, 8, 64, blocks
    rec(f"transfers.row[{st}]", tv.row_model(j))
    rec(f"transfers.section[{st}]", tv.section_of(j))
for s in (0, 1, 59, 60, 61, 3599, 3600, 7322, 100000):
    rec(f"transfers.dur[{s}]", tv._dur(s))

# ---- 核心库的**长句子**:这些几乎没有测试在断言内容,而它们恰恰是
#      f-string → .format() 最容易参数错位的形状(位置占位符 ≥2)。
from datetime import datetime as _dt, timedelta as _td

from astro_smb import guidecheck as GC
from astro_smb import platesolve as PS

# 板解算:每种失败理由都过一遍 `__str__`
for key in (PS.REASON_OK, PS.REASON_NO_HINT, PS.REASON_FEW_STARS,
            PS.REASON_NO_CATALOG, PS.REASON_NO_MATCH, PS.REASON_BAD_FIT,
            PS.REASON_TIMEOUT):
    try:
        rr = PS.SolveResult(ok=(key == PS.REASON_OK), reason=key,
                            message="详情占位", n_match=258, rms_px=0.63,
                            elapsed_s=0.2, n_stars=400)
        rec(f"platesolve.str[{key or 'ok'}]", str(rr))
    except Exception as e:
        rec(f"platesolve.str[{key or 'ok'}]", f"!! {type(e).__name__}: {e}")

# **成功那一支单独喂一条** —— 它要有 wcs 才走得到,而它是核心库里最复杂的
# 一条格式串(2 个位置 + 4 个关键字占位符)。
try:
    import numpy as _np

    from astro_smb.wcs import TanWcs

    _w = TanWcs(crval=(270.75, -24.38), crpix=(3124.0, 2088.0),
                cd=_np.array([[-5.34e-4, 2.06e-5], [2.06e-5, 5.34e-4]]))
    rec("platesolve.str[success]",
        str(PS.SolveResult(ok=True, wcs=_w, reason=PS.REASON_OK,
                           n_match=258, rms_px=0.63, elapsed_s=0.2,
                           n_stars=400)))
except Exception as e:
    rec("platesolve.str[success]", f"!! {type(e).__name__}: {e}")

# 导星逆推:三通道交叉判读(48 处文案在这个模块,几乎没有测试断言内容)
_T0 = _dt(2026, 7, 30, 22, 0, 0)


def _fev(n, drift=0.0, rms=0.4, pa_rate=0.0):
    out = []
    for i in range(n):
        t0 = _T0 + _td(seconds=i * 5.0 * 60.0)
        out.append(GC.FrameEvidence(
            t0=t0, t1=t0 + _td(minutes=4), center_ra=100.0,
            center_dec=20.0 + (i * 5.0 * drift) / 3600.0,
            pa_deg=30.0 + i * 5.0 / 60.0 * pa_rate,
            guide_rms_arcsec=rms, guide_coverage=1.0, fwhm_px=3.0,
            fwhm_arcsec=3.0, ellipticity=0.10, n_stars=120))
    return out


for label, kw, extra in (("good", {}, {}),
                         ("drift", {"drift": 1.2}, {}),
                         ("drift-oag", {"drift": 1.2}, {"is_oag": True}),
                         ("rotation", {"pa_rate": 0.9}, {}),
                         ("overguide", {"rms": 2.5}, {})):
    try:
        cc = GC.cross_validate(_fev(12, **kw), pixel_scale_main=1.0,
                               lat_deg=30.0, ha_deg=15.0, **extra)
        rec(f"guidecheck.{label}", {"verdict": cc.verdict,
                                    "headline": cc.headline,
                                    "findings": list(cc.findings),
                                    "confidence": cc.confidence})
    except Exception as e:
        rec(f"guidecheck.{label}", f"!! {type(e).__name__}: {e}")

# ---- scan
for shares in ([], ["Public"], ["EMMC Images", "TF Images", "Udisk Images"]):
    rec(f"scan.row[{len(shares)}]",
        scv.device_row("192.0.2.9", "HOST", shares, 4.2))

print(json.dumps(out, ensure_ascii=False, sort_keys=True, default=str))
'''


def _run(cwd: Path, tag: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "-c", PROBE, str(cwd), str(MIRROR)],
        capture_output=True, cwd=str(cwd),
        env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8",
             "ASTRO_SMB_LANG": "zh_CN"})
    if proc.returncode != 0:
        print(f"[{tag}] 采样失败:\n{proc.stderr.decode('utf-8', 'replace')[-3000:]}")
        raise SystemExit(2)
    return json.loads(proc.stdout.decode("utf-8"))


def main() -> int:
    if not MIRROR.is_dir():
        print(f"没有离线镜像 {MIRROR} —— 这个检查靠真机数据,合成数据走不到那些分支")
        return 1
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=ROOT,
                           capture_output=True, text=True).stdout.strip()
    if not dirty:
        print("工作区是干净的 —— 改前改后是同一份代码,这个检查证明不了任何东西")
        return 1

    with tempfile.TemporaryDirectory(prefix="i18n-diff-") as td:
        wt = Path(td) / "head"
        subprocess.run(["git", "worktree", "add", "--detach", str(wt), "HEAD"],
                       cwd=ROOT, capture_output=True, check=True)
        try:
            before = _run(wt, "HEAD")
            after = _run(ROOT, "工作区")
        finally:
            subprocess.run(["git", "worktree", "remove", "--force", str(wt)],
                           cwd=ROOT, capture_output=True)

    keys = sorted(set(before) | set(after))
    diffs = [(k, before.get(k, "<缺>"), after.get(k, "<缺>"))
             for k in keys if before.get(k) != after.get(k)]
    errs = [k for k in keys if str(after.get(k, "")).startswith("!!")
            or "!!" in json.dumps(after.get(k), ensure_ascii=False, default=str)]

    print(f"采样 {len(keys)} 个键(改前 {len(before)} / 改后 {len(after)})")
    if errs:
        print(f"\n**采样过程里有 {len(errs)} 处异常** —— 先看这些,"
              f"它们说明探针写错了或者代码真的坏了:")
        for k in errs[:10]:
            print(f"  {k}: {json.dumps(after[k], ensure_ascii=False, default=str)[:160]}")
    if not diffs:
        print("\n中文下输出**逐字节一致**。")
        return 0 if not errs else 2
    print(f"\n**{len(diffs)} 处不一致** —— 中文下 `_()` 是恒等函数,"
          f"所以这些几乎肯定是 f-string 改写错了:")
    for k, b, a in diffs[:25]:
        print(f"\n  {k}\n    改前 {json.dumps(b, ensure_ascii=False, default=str)[:220]}"
              f"\n    改后 {json.dumps(a, ensure_ascii=False, default=str)[:220]}")
    if len(diffs) > 25:
        print(f"\n  … 另外 {len(diffs) - 25} 处")
    return 1


if __name__ == "__main__":
    sys.exit(main())
