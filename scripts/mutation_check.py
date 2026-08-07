"""变异测试:改坏代码,看有没有测试发现。

    uv run python scripts/mutation_check.py

**这不是常规 CI 的一部分**(跑一轮要几分钟),是改动下面那些"判读"逻辑时
自己跑一遍的工具。首轮实测 8 条里活了 5 条 —— 高度角/气量/采样的三个阈值、
夜次配色的排序、天球投影的东西方向,**全都没有任何测试发现**。
而那几处正是注释里反复写着"这一层值钱在判读不在取值"的地方。

加新的判读逻辑时,往 `MUTATIONS` 里加一条,确认它会被抓。

静态扫只能抓"形状可疑"的断言;真正的问题是**没有任何测试覆盖到某条判断**。
这里挑一批**语义上真的会出错**的改动(不是随机改字符),逐个应用、跑整轮测试、
记下哪些"活下来了"(= 没有测试发现)。改动结束后**一定会还原**(try/finally)。

活下来的不一定都要补测试。**先判断它是不是"等价变异"** —— 改了代码但行为
一模一样的那种。实测踩过一次:把 `_RE_IMAGE.match` 换成 `.search` 活了下来,
查下去发现那个正则本身带 `^...$`,两者行为完全相同。那不是覆盖空洞,是坏变异,
该改的是变异而不是补测试。把等价变异当成空洞去补,只会写出一堆假装在测的测试。

排除等价变异之后,活下来的每一条都该被看一眼,而不是假装不存在。
"""
import io
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path.cwd()

# (文件, 原文, 改成, 一句话说明)
MUTATIONS = [
    # ---- 判读阈值:改了会给出不同结论
    ("astro_smb_app/views/browser.py",
     "    if alt_deg >= 40.0:\n        return \"good\"",
     "    if alt_deg >= 4.0:\n        return \"good\"",
     '高度角 good 阈值 40° → 4°'),
    ("astro_smb_app/views/browser.py",
     "    if am < 1.5:\n        return \"good\"",
     "    if am < 15.0:\n        return \"good\"",
     '气量 good 阈值 1.5 → 15'),
    ("astro_smb_app/views/browser.py",
     "    if scale > 2.0:\n        return _(\"欠采样\"), \"warn\"",
     "    if scale > 20.0:\n        return _(\"欠采样\"), \"warn\"",
     '欠采样阈值 2.0 → 20'),
    # ---- 排序/身份
    ("astro_smb_app/views/browser.py",
     "    return {k: i for i, k in enumerate(sorted(keys))}",
     "    return {k: i for i, k in enumerate(keys)}",
     "夜次配色不再按日期排序"),
    # ---- 协议
    # ---- 天文
    ("astro_smb_app/views/skychart.py",
     "    return cx - r * math.sin(az), cy - r * math.cos(az)",
     "    return cx + r * math.sin(az), cy - r * math.cos(az)",
     "天球投影东西翻转(北上东左 → 北上东右)"),
    # ---- 外壳措辞
    # ---- 打包
    # (「渲染器只在 _MEIPASS 底下找」那条已删:它打的
    #  `for root in (install_root(), bundle_root()):` 是 Uno 时代找 C# 渲染器
    #  的循环,`data_file` 里现在没有那一段了 —— 自检报的"命中 0 次"就是它。
    #  这一层现存的判据由下面「资源定位不再优先用开发路径」守着。)
    # ---- 核心:夜次归并
    ("astro_smb/autorunlog.py",
     '    return (dt - timedelta(hours=12)).strftime("%Y-%m-%d")',
     '    return dt.strftime("%Y-%m-%d")',
     "夜次不再按正午分界(凌晨的片子会被归到第二天)"),
    # ---- 核心:导星丢星判定
    ("astro_smb/phd2log.py",
     "        return self.err != 0 or self.snr <= 0.0",
     "        return self.err != 0",
     "丢星判定漏掉 SNR==0(那些帧会污染 RMS)"),
    # ---- 导星:合并 RMS 的加权
    ("astro_smb_app/views/guiding.py",
     "            sq_a += rms.rms_total ** 2 * rms.n_frames",
     "            sq_a += rms.rms_total ** 2",
     "合并 RMS 不再按帧数加权(短段会被放大)"),
    ("astro_smb_app/views/guiding.py",
     "ENV_FRAMES_PER_PX = 2.0",
     "ENV_FRAMES_PER_PX = 2000.0",
     "包络视图阈值抬到 2000(密集段又会画成噪声)"),
    # ---- 传输:并行阈值与冲突策略
    ("astro_smb_app/transfers.py",
     "PARALLEL_THRESHOLD = 16 << 20",
     "PARALLEL_THRESHOLD = 16 << 40",
     "并行下载阈值抬到 16 TiB(等于关掉分块并发)"),
    # ---- 条目排序:目录永远在前
    ("astro_smb_app/entries.py",
     "    return dirs + files",
     "    return files + dirs",
     "目录不再排在文件前面"),
    # ---- 协议:补丁按值 diff
    # ---- 词表:未知图元要拒绝
    # 注:`if spec is None:` 在本文件里有**两处**(节点种类一处、画布算子一处),
    # 所以带上前一行才唯一 —— 会动而不自知的变异是不能信的。
    # ---- 天文:RA/DEC 解析
    ("astro_smb_app/views/skychart.py",
     "    r = radius * (90.0 - max(-5.0, min(90.0, alt_deg))) / 90.0",
     "    r = radius * (90.0 + max(-5.0, min(90.0, alt_deg))) / 90.0",
     "天顶与地平画反(高度越高离圆心越远)"),
    # ---- 外壳:导航顺序
    # ---- 打包:开发路径优先
    ("astro_smb_app/bundle.py",
     "    if package_relative is not None and package_relative.exists():\n        return package_relative",
     "    pass",
     "资源定位不再优先用开发路径"),
    # ---- #33 导星逆推:"走没走"的判据(用户明确定过口径)
    ("astro_smb/guidecheck.py",
     "        if walk_px >= DRIFT_WALK_PX:",
     "        if walk_px >= DRIFT_WALK_PX * 100:",
     "累计位移阈值 x100(真的走了也说没走)"),
    ("astro_smb/guidecheck.py",
     "        walk_px = fit.total_arcsec / pixel_scale",
     "        walk_px = fit.total_arcsec * pixel_scale",
     "位移换算成像素时乘反了(403mm/2000mm 结论互换)"),
    # ---- watcher:"正在拍摄"的心跳容差
    ("astro_smb_app/watcher.py",
     "IDLE_GRACE_S = 600.0",
     "IDLE_GRACE_S = 6.0",
     "运行判据容差 10 分钟 → 6 秒(换目标/对焦时会误报停机)"),
    # ---- 并行下载:.part 原子落盘
    ("astro_smb/parallel.py",
     "            os.replace(part_path, local_path)",
     "            pass",
     "分块下载完不再改名成最终文件"),
    # ---- 预览缓存:原子写
    ("astro_smb_app/preview.py",
     "            os.replace(tmp, dest)",
     "            pass",
     "预览缓存不再原子改名(半截文件会被当成完整的)"),
    # ---- treemap 配色:跨进程稳定
    ("astro_smb_app/views/space.py",
     "    return zlib.crc32(category.encode(\"utf-8\")) % len(PALETTE)",
     "    return hash(category) % len(PALETTE)",
     "文件类型取色退回 hash()(每进程随机)"),
    # ---- 文件名解析:时间戳锚点
    # 文件名的锚定是**双重**的:`^` 与 `.match()` 各自都足够。
    # 只动一处是**等价变异**(实测把 match 换成 search、把 ^ 去掉,两条都
    # 活了下来 —— 因为剩下那一处还在锚着)。要同时动两处才是真缺陷。
    ("astro_smb/naming.py",
     "    m = _RE_IMAGE.match(name)",
     "    m = re.compile(_RE_IMAGE.pattern.lstrip(chr(94)), "
     "_RE_IMAGE.flags).search(name)",
     "文件名解析同时去掉 ^ 与 match(会匹配上路径中间的东西)"),
    # ---- 界面:**静默失败**那一类(2026-08 这一批全是真机上看出来的)
    #
    # 共同点是不报错、不崩溃、契约也没违反,只是界面行为不对 —— 比阈值那几条
    # 更难发现:阈值错了数字至少看着别扭,而这些只是"点了没反应""看不见"。
    # (删 Uno 时漏掉的那条「网格重建不清行列」已经删除 —— 它打在
    #  `frontend/…/NodeRenderer.cs` 上,那个文件 2026-08-03 随 Uno 一起没了。
    #  留着不是空转而是**直接崩**:自检只管"命中几次",不管文件在不在。
    #  现在文件缺失也走同一条 skip 通路了,见下面的 `is_file()`。)
    # ---- i18n:**身份不许经过显示文本**(2026-08 这一批的主题)
    #
    # 这三条守的是同一件事:一旦判读/查表/取色走了会被翻译的那根字符串,
    # 换语言就静默走样 —— 不报错,只是颜色变了、图标没了、顺序乱了。
    ("astro_smb_app/views/space.py",
     "    base = DIR_COLOR if node.is_dir else PALETTE[palette_index(ext_category_id(node))]",
     "    base = DIR_COLOR if node.is_dir else PALETTE[palette_index(ext_category(node))]",
     "treemap 取色退回显示文本(换语言整张图换一套颜色)"),
    ("astro_smb/autorunlog.py",
     "        keys = list(actual) + [k for k in planned if k not in actual]",
     "        keys = planned.keys() | actual.keys()",
     "帧型键序退回集合并(每次启动顺序都不一样)"),
    ("astro_smb_qt/widgets.py",
     "        glyph = SEGOE_GLYPHS.get(glyph, glyph or \"·\")",
     "        glyph = SEGOE_GLYPHS.get(name, glyph or \"·\")",
     "分组图标按组名查(组名会被翻译,图标全退回一个点)"),
    ("astro_smb/backend.py",
     "    if (\"/\" in h or \"\\\\\" in h) and Path(h).is_dir():",
     "    if Path(h).is_dir():",
     "本地/远程判据去掉必须含分隔符(同名文件夹会劫持设备地址)"),
]


def run_tests() -> bool:
    """全绿返回 True。"""
    # **变异跑不许留字节码。** 被改过的源文件编译出的 `.pyc` 一旦落盘,还原之后
    # 有机会被后续进程当成有效缓存用上 —— 真机撞过一次:源码明明是
    # `except UnicodeError`,报错却说 `_NeverRaised` 未定义(那是上一轮变异的
    # 内容)。这类事故最坏的形态是让一条空转变异显示成"存活",于是有人去补一个
    # 根本不存在的覆盖空洞。
    env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
    p = subprocess.run([sys.executable, "-m", "pytest", "tests/", "-q", "-x",
                        "--no-header", "-p", "no:cacheprovider"],
                       cwd=ROOT, capture_output=True, env=env)
    return p.returncode == 0


survivors, killed, skipped = [], [], []
for path, old, new, why in MUTATIONS:
    if old == new:
        continue
    f = ROOT / path
    if not f.is_file():
        # 文件没了(比如整个前端被删掉)。这和"原文命中 0 次"是同一件事 ——
        # 这条变异已经空转 —— 但原来会直接 FileNotFoundError 把整轮打断,
        # 而且是在跑完十几条之后才崩,前面的结果全白跑。
        skipped.append((path, why, "文件不存在(这条变异已经空转)"))
        continue
    src = f.read_text(encoding="utf-8")
    hits = src.count(old)
    if hits != 1:
        # **命中 0 次**:代码改过,这条变异已经空转;
        # **命中多次**:它会打在"第一处",而那一处是哪一处没人保证 ——
        # 改动一挪位置,这条测的就是别的东西了,且**没有任何提示**。
        skipped.append((path, why, f"原文命中 {hits} 次(要求恰好 1 次)"))
        continue
    f.write_text(src.replace(old, new, 1), encoding="utf-8")
    t0 = time.time()
    try:
        green = run_tests()
    finally:
        f.write_text(src, encoding="utf-8")
    tag = "存活" if green else "被抓"
    (survivors if green else killed).append((path, why))
    print(f"[{tag}] {why}  ({time.time() - t0:.0f}s)", flush=True)

print(f"\n被抓 {len(killed)} / 存活 {len(survivors)} / 跳过 {len(skipped)}")
for path, why in survivors:
    print(f"  存活: {path}\n        {why}")
for path, why, note in skipped:
    print(f"  跳过: {path} — {why}({note})")
