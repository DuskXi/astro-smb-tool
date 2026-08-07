# 在 macOS 上跑起来 —— 照着抄就行

这一份只讲**一件事**:把 Qt 界面(`astro_smb_qt`)在 Mac 上跑起来。
不需要 Xcode、不需要 .NET、不需要自己装 Python。

> 曾经还有第三套前端(Uno / C#,要 .NET SDK),**2026-08-03 已删除**。
> 现在只有两套:Windows 原生的 WinUI3(`astro-smb-tool-gui`,mac 上跑不了),
> 和这一份跨平台的 Qt。

---

## 一、装 uv(只做一次)

打开「终端」(Terminal),粘这一行:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

装完**关掉终端窗口再开一个**(让 PATH 生效)。验一下:

```bash
uv --version
```

有版本号就行。Python 不用管 —— `uv` 会照 `.python-version` 自己拉 3.13。

---

## 二、进到项目目录

把这个 zip 解压到任意位置,然后 `cd` 进去。**不确定路径就用这招**:
在终端里敲 `cd ` (注意末尾有个空格),然后把解压出来的文件夹从访达拖进
终端窗口,回车。

验一下你在对的地方:

```bash
ls pyproject.toml
```

打印出 `pyproject.toml` 就对了。**下面所有命令都在这个目录里执行。**

---

## 三、跑起来

```bash
uv run --with pyside6 astro-smb-tool-qt
```

第一次会下载依赖(PySide6 约 200 MB,几分钟),之后就快了。

> 下面为了带参数方便,都写成 `python -m astro_smb_qt` —— 和上面这条**完全
> 等价**,只是模块写法能直接跟调试参数。

窗口起来之后,左边九个页面随便点。**没有连设备时大部分页面是空的**,
这是对的 —— 下一步给它一份数据。

---

## 四、给它一份数据(不用有 ASIAIR)

这个工具能直接把**本地文件夹**当设备使(存储卡直插电脑也是走这条路)。
**zip 里已经带了一份真机样例**,直接:

```bash
uv run --with pyside6 python -m astro_smb_qt --host "$PWD/.tmp/device/EMMC Images"
```

> **注意路径末尾是 `EMMC Images`,不是 `.tmp/device`。**
> 传 `.tmp/device` 的话共享名会变成 `device`、日志目录落错地方,
> 拍摄记录和导星两页会读不到东西(验收时踩过两次)。
>
> `.tmp` 是**隐藏目录**,访达里默认看不见(`⌘⇧.` 可以切换显示)。
> 命令照抄就行,不用去翻。

### 这份样例里有什么、没有什么(**先看这段,免得误判**)

真机镜像原本 **16 GB**(316 张 50 MB 的片子),整个塞进 zip 不现实。
带上来的是:

- **全部日志** —— Autorun 6 份 + PHD2 2 份。拍摄记录、导星分析、3D 天球、
  夜次统计**全都建立在日志上**,所以这几页的数据是完整的、真的。
- **全部 316 张缩略图**(`_thn.jpg`)—— 浏览页的目录结构、预览、时间线都真。
- **一张真片**:`Plan/Light/M 8/Light_M 8_180.0s_…_0001.fit`(52 MB)。
  影像查看、拉伸、直方图、板解算、星点叠加拿它演。

因此有两处**看起来像 bug、其实是样例的限制**,别当问题报:

1. **浏览页大部分目录里只有 `_thn.jpg`,看不到 `.fit`** —— 片子没带上来,
   不是列表漏了。只有 `Plan/Light/M 8/` 下面有一张真的。
2. **空间分析算出来的总量只有几 MB** —— 它量的是磁盘上真实占用,而磁盘上
   现在确实只有缩略图。目录结构、嵌套 treemap、双向联动都是对的,
   只有数字小。

想要完整的量,把整个 `EMMC Images` 从设备或存储卡拷过来,把 `--host`
指过去即可 —— 代码路径完全一样。

### 板解算(可选,但很值得看)

zip 里带了一份 **Tycho-2 星表**(`sample/catalog/`,35 MB),指过去就能用,
省掉第一次从 CDS 取 159 MB 原始分片再构建的那几分钟:

```bash
export ASTRO_SMB_CATALOG_PATH="$PWD/sample/catalog/tycho2_v1.bin"
uv run --with pyside6 python -m astro_smb_qt --host "$PWD/.tmp/device/EMMC Images"
```

**不指也行。** 不设这个变量的话,点「板解算」会先告诉你「星表未就绪」、
要下多大、从哪儿取,再给一个「下载星表」按钮 —— 点了才下,下完自动接着
把刚才那次解算跑完。(这条路在 2026-08-03 之前是**坏的**:核心库里进度
回调的参数个数两边对不上,第一次回调就 `TypeError`,**任何前端都下不下来**。
如果你手上这份 zip 是那天之前导出的,请用新的。)

然后:影像查看页 → 打开那张 M 8 → 「板解算」。解出来之后「星点叠加」
可勾,3D 天球页的「足迹」也才有东西可画(足迹**只用已经解算过的** WCS,
不会自己去解算几十张 50 MB 的图)。

存储卡直插的话,把路径换成卡的挂载点,例如:

```bash
uv run --with pyside6 python -m astro_smb_qt --host "/Volumes/ASIAIR"
```

真设备(同一个局域网):

```bash
uv run --with pyside6 python -m astro_smb_qt --host 192.0.2.227
```

---

## 五、常用参数

```bash
# 直接开到某一页(browse/records/guiding/sky/fits/space/devices/scan/transfers)
uv run --with pyside6 python -m astro_smb_qt --page records

# 配色:白天 / 常规(深色)/ 红光(夜间不破坏暗适应)
uv run --with pyside6 python -m astro_smb_qt --theme light

# 浏览页直达某个目录、并选中第一张 .fit
uv run --with pyside6 python -m astro_smb_qt --browse "EMMC Images/Plan/Light" --select fit

# N 秒后自动关窗 + 退出前截一张图(自动化/截图用)
uv run --with pyside6 python -m astro_smb_qt --seconds 30 --shot /tmp/shot.png
```

全部参数:`uv run --with pyside6 python -m astro_smb_qt --help`

---

## 六、跑一遍测试(可选)

```bash
uv run --with pyside6 pytest tests/ -q
```

**预期是全绿**,两千多条,**跑完大约半分钟**(默认并行,`-n auto`)。
有几条会 skip,那是正常的:`astro_smb_gui/`(老的 WinUI3 界面)在 Mac 上
导不进来,`tests/conftest.py` 会跳过它们而不是让整轮变红。

> 别去掉并行。串行不只是慢:Qt 那批测试全在一个 QApplication 里建控件,
> 越堆越多、后面越跑越慢,**总 CPU 比并行贵 4~5 倍**(Windows 上实测串行
> 二十多分钟还没跑完)。

上一节那份样例日志**同时也是测试数据** —— 有它在,`test_astrolog.py` 里
几条"拿真机日志对账"的用例会真的跑起来,而不是 skip 掉。

想省掉每次解析依赖的那几秒,可以先建一个常驻环境:

```bash
uv venv .venv-qt --python 3.13
uv pip install --python .venv-qt/bin/python pyside6 pytest pytest-xdist -e .
.venv-qt/bin/python -m pytest tests/ -q
```

---

## 七、出问题时

| 症状 | 多半是 |
|---|---|
| `uv: command not found` | 第一步装完没重开终端 |
| 窗口起不来、报 `qt.qpa.plugin` | 用的是远程/无桌面会话;要在本机图形界面下跑 |
| 3D 天球页是「正射投影」不是 three.js | 装的是 `PySide6-Essentials`(不含 QtWebEngine)。`uv pip install pyside6-addons` 补上;不补也能用,只是降级成 QPainter 球 |
| 拍摄记录 / 导星是空的 | `--host` 路径给到 `EMMC Images` 那一层了吗?见第四节 |
| 界面是英文的 / 字体方块 | 不该发生,遇到请截图 —— 项目里所有用户可见文本都是中文 |

---

## 八、这一版里可以重点看的

九页验收清单(`docs/qt-final.md`)已经走完。剩下几条要**真机/真网络**才算数
(设备正在曝光的横幅、慢链路的下载进度条、在线设备的探测与连接、网里真有
ASIAIR 时的 ★ 置顶)—— 那几条的**画面**已经用假数据喂出来截图验过了
(`docs/evidence/qt/fake-*.png`,清单 §11),但**"真机上会不会触发"验不了**,
所以它们写的是「画面已验 / 触发未验」而不是「过」。另有 1 条是有意偏离
(边栏分组比另一套多了三个标题)。

**你在 Mac 上如果手边有真设备,最值得帮我验的就是那几条。**

**这一版(8-04 下午)新修的**,都是"不报错、只是悄悄不对"那一类:

- **拍摄记录里帧型的顺序**(「已完成 · dark 5/5 · bias 30/30」)**每次启动都不一样** ——
  底下用的是集合并,而 Python 字符串 hash 每进程随机。现在按日志里出现的顺序。
- **导星仪表盘有一组会直接报错**(`UnboundLocalError`)—— 一句
  `h_ra, _ = np.histogram(...)` 把 gettext 的 `_` 遮成了局部名。真机 11 组里
  只有第 10 组走到那条路径,所以整轮单测是绿的。
- **极轴误差那个数字可不可信**,原来是去结论文本里搜「恰定」两个字判断的。
- 传输页组头的「完成 N / 失败 N」比的是中文字面量而不是状态常量。
- **测试快了 50 倍**(23 分钟没跑完 → 半分钟跑完,见第六节)。

上一版(8-04 上午)修的:

- **板解算**:12 行结构化结果(含「离先验中心 20.7′」—— FITS 头里的 RA/DEC
  是赤道仪编码器读数,与解算中心恒差约 21′,那不是故障)、258 颗星点叠加。
  之前这一页会**永久冻在「正在解算…」**。
- **星表**:新机器上点板解算会先说清楚(未就绪 / 要下多大 / 从哪儿取)
  再给下载按钮。**这条路在 8-03 之前是坏的** —— 核心库里进度回调参数个数
  两边对不上,任何前端都下不下来。
- **传输页「排队」分区**:之前**永远是空的**(判据比的是 `"排队"`,而常量
  是 `"排队中"`)。
- **高度角量条**:四个刻度换成天文线稿图标(地平线 / 低空浑浊 / 通透 / 天顶),
  已越过的点亮、没到的留灰 —— 填充停在哪个图标之前,本身就是结论。
- **3D 天球**:选中目标现在球上有高亮环(之前只有相机飞过去,标记一点没变);
  「足迹」画出真实视场四边形。
- **心跳**:连上就跳第一拍(之前头 4 秒状态栏一直写「断开」)。

---

## 九、目录里有什么

```
astro_smb/        核心库(SMB / FITS / 天文 / 日志 / 板解算),不依赖任何界面
astro_smb_app/    共享应用层(设备记录、缓存、传输队列、视图模型)
astro_smb_qt/     Qt 界面 ← 就是这一份
astro_smb_gui/    WinUI3 界面(**只在 Windows 上能跑**)—— 它是界面**原型**,
                  Qt 这套照它逐页复刻;两边同步是单向的
tests/            两千多条离线单测(默认并行,半分钟跑完)
docs/qt-final.md  逐页验收清单 = 「完工」的定义
```

Mac 上 `astro_smb_gui/` 跑不了(它依赖 `win32more`),这是预期的 ——
`tests/conftest.py` 会自动跳过那些用例,不会让整轮测试变红。
