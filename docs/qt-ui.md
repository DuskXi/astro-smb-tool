# 跨平台前端:PySide6 / Qt(`astro_smb_qt`)

> 这一份既是上手说明,也是**设计规格的可对照清单** —— 每加一页对一遍。
> 规格那部分由 `tests/test_qt_style.py` 与 `tests/test_qt_wiring.py` 守着,
> 对不上就变红。

---

## 0. 跑起来

```bash
# 真设备(SMB)
uv run --with pyside6 python -m astro_smb_qt --host 192.0.2.227

# 本地目录当设备用(ZWO 卡直插电脑,或把卡的内容拷到本机)—— 同一条代码路径
uv run --with pyside6 python -m astro_smb_qt --host "D:\ASIAIR\EMMC Images"

# 不给 --host:ASTRO_SMB_HOST > devices.json 里上次连过的 > 不猜(直接去扫描页)
uv run --with pyside6 python -m astro_smb_qt
```

**PySide6 不在 `pyproject.toml` 里**,靠 `uv run --with pyside6` 临时注入。
这是有意的:另外两套前端各自带着一条重依赖链(Windows App Runtime / .NET),
再往主依赖里塞一个 240MB 的 Qt 不值得,直到这一套被正式采纳为止。

其它参数(`--help`):

| 参数 | 作用 |
|---|---|
| `--page browse\|records\|guiding\|sky\|fits\|space\|devices\|scan\|transfers` | 启动页 |
| `--theme normal\|red` | 配色;`red` 是红光模式 |
| `--seconds N` | N 秒后自动关窗。**探针脚本必须给它** —— 没有自关的探针在迭代验证里几轮就吃掉几个 GB |
| `--shot x.png` | 配合 `--seconds`,退出前把窗口截图 |

环境变量:

| 变量 | 作用 |
|---|---|
| `ASTRO_SMB_HOST` | 覆盖启动地址(与 CLI / 另外两套前端同名) |
| `ASTRO_SMB_QT_TITLE_TAG` | 窗口标题后缀。**自动化只准按标签认自己那一个窗口** —— 用户常开着自己的实例,按进程名匹配一定会误抓(docs/DEVELOPMENT.md §7.10 记着这条真机教训) |
| `QT_QPA_PLATFORM=offscreen` | 无头跑(交互门禁用它) |

### 两个窗口

```bash
# 给人手动体验的那个:不带 --seconds,一直开着
ASTRO_SMB_QT_TITLE_TAG=人类体验窗口 uv run --with pyside6 python -m astro_smb_qt --host "<设备或本地目录>"

# 自己实验/截图的那个:一定带 --seconds
ASTRO_SMB_QT_TITLE_TAG=AGENT uv run --with pyside6 python -m astro_smb_qt --host "<…>" --seconds 12 --shot .tmp/shots/x.png
```

批量截图:`uv run --with pyside6 python -m astro_smb_qt.shots --host "<…>"`。

---

## 1. 分层:只写界面,业务一行都不重写

```
astro_smb            核心库:SMB / 本地 后端、FITS、天文、日志解析、板解算
   ↑
astro_smb_app        共享应用层:设备记录、缓存、传输队列、预览、日志聚合、卷枚举
   ↑                 + 各页的视图模型 views/
   ├── astro_smb_gui        WinUI3 —— 已冻结,是 fallback
   ├── astro_smb_gui        WinUI3(Windows 原生 + 界面原型)
   └── astro_smb_qt         ← 这一套
```

**下面两层一个字节都没改。** 判读(气量用 Pickering 而不是 1/sin(h)、高度角与
采样的阈值、导星 RMS 按帧数平方加权、天球投影北上东左、扫描只认 SMB 协商)
全部只有一份实现。

本套自己的模块:

| 模块 | 职责 |
|---|---|
| `theme.py` | **所有**颜色/字号/间距/圆角的唯一真源 + 红光模式 |
| `widgets.py` | `Card` / `SectionTitle` / `StatusChip` / `SideNav` / `MetricRow` / `DataTable` / `OpsCanvas` / `StateStack` … |
| `workers.py` | 后台线程 + 世代计数器 + 信号编组 |
| `models.py` | 页面模型(**纯函数,能脱离 QApplication 单测**) |
| `shell.py` | 外壳:侧边栏 / 连接栏 / 页面区 / 传输条 / 心跳 / watcher |
| `pages/` | 九页,一页一个模块 |
| `shots.py` | 截图脚本(开发用) |

---

## 2. 视觉规格(**每加一页对一遍**)

### 2.1 表面层次

| 层 | 常量 | 常规 | 红光 |
|---|---|---|---|
| 窗口底 | `C.BG` | 近黑 | 近黑偏红 |
| 边栏 | `C.BG_ALT` | 更深一档 | 更深一档 |
| 卡片 | `C.SURFACE` | 亮一档 | 亮一档 |
| 卡内高亮 / hover | `C.SURFACE_HI` | 再亮一档 | 再亮一档 |
| 描边 | `C.BORDER` / `C.BORDER_HI` | 1px | 1px |

**层次感来自色差,不是描边。** 全用同一个底色再靠 1px 边框撑,界面会又硬又平
(另外那套前端的原话:"硬""薄")。

### 2.2 强调色

**只有一个**(`C.ACCENT`,青色系),用在:选中态、进度条、关键数值、图表主线、
卡头竖条。想再加一个颜色之前先问:它表达的是不是"语义"(那就用
`C.OK` / `C.WARN` / `C.BAD`)。

### 2.3 卡片

- 界面的基本单位:每块内容一张 `W.Card`
- 卡头 = **标题 + 下面一行小字副标题**,标题左侧一道 3px 强调色竖条
  (`ACCENT_BAR_W`)
- 右上角是状态胶囊(`W.StatusChip`:`n/d` / `不可用` / `SMB 3.1.1`)
- 圆角 `Radius.CARD`(8px),内边距 `Space.CARD_PAD`(≥12),卡间距
  `Space.CARD_GAP`(≥10)

### 2.4 边栏

应用名 + 图标在顶 → 导航项带图标、**分组并有分组小标题**(全大写小字灰,
`W.group_title`)→ 底部常驻状态区(连接状态 + 红光模式切换)。

分组:数据(浏览/拍摄记录/导星分析)· 影像(3D 天球/影像查看/空间分析)·
设备(设备管理/扫描设备/传输)。顺序与文案与另外两套前端一致 —— 用户的
肌肉记忆在这上面。

### 2.5 排版

| 角色 | 常量 | 用途 |
|---|---|---|
| `pagetitle` | `Font.TITLE` 16 semibold | 页头 |
| `title` | `Font.H1` 15 semibold | 卡头 |
| `body` | `Font.BODY` 12–13 | 正文 |
| `subtitle` | `Font.SMALL` 11,`TEXT_DIM` | 副标题 |
| `faint` | `Font.SMALL` 11,`TEXT_FAINT` | 注解 |
| `group` | `Font.TINY` 10 semibold + letter-spacing | 分组小标题 |
| `metric` | `Font.METRIC` 22 semibold | 大数字 |
| `mono` | 等宽 | 坐标、路径、日志 |

**灰度至少三档**(`TEXT` / `TEXT_DIM` / `TEXT_FAINT`),不要所有文字一个颜色。

### 2.6 三档配色:白天 / 常规 / 红光

边栏底部三个按钮,也可 `--theme light|normal|red` 启动。

**白天档**是浅底深字 —— 深色在暗房里舒服,可白天对着窗户、或者户外看笔记本时
反光严重、对比度全失。它的表面层次是**反过来的**:浅色主题里卡片比窗口底
更白(深色主题里卡片比底更亮的那一档在这里对应"更白")。语义三档不能直接抄
深色那套:`#4FBF87` 放在白底上几乎看不见。门禁
`test_light_palette_is_actually_light` 逐条验亮度与对比。

调色板的字段齐全性由 `test_every_palette_defines_every_field` 按 `PALETTES`
**遍历**检查 —— 不写死名字,再加一档也自动被覆盖(第一版写死了
`("NORMAL", "RED")`,白天档加进来时根本没被检查过)。

切换是**循环**(白天 → 常规 → 红光 → 白天)。原来是
`RED if mode == NORMAL else NORMAL` 的两档写法,加第三档之后会把人锁在
常规/红光之间,白天档永远切不到,而且不报错;`test_mode_cycle_visits_every_mode`
盯着这条。

#### 红光模式

天文软件的刚需:蓝绿波段最毁暗适应,恢复要二三十分钟。

- 一键切换在边栏底部(`白天` / `常规` / `红光`),也可 `--theme red` 启动
- **两套调色板的键完全一致**(`Palette` 是 dataclass,门禁盯着)—— 少一个键
  在红光下就是某个控件颜色取不到,变成透明或黑底黑字,不报错
- 红光配色里**每一档都是红占优**(门禁 `test_red_palette_is_actually_red`
  逐个字段验 `r >= g` 且 `r >= b`)
- 三档语义色靠**亮度**区分(OK 最暗、BAD 最亮),不能用绿/蓝
- **共享视图层给的色值**(天球圈、treemap 分类色、甘特调色板)那一层不知道
  也不该知道有红光模式 —— 显示列表统一过一道 `theme.screen_color()`:
  常规模式恒等,红光模式按亮度映射到强调色
- 缩略图/预览图走 `W.ImageView`,红光下会灰度化后乘强调色

---

## 3. 门禁:"一直维持"的执行机制

**没有门禁的约定,三页之内必然失效。** 三条测试都做过回退验证(把改动去掉、
确认真的变红、再还原)。

### `tests/test_qt_style.py` —— 样式

| 断言 | 挡住什么 |
|---|---|
| 页面里没有字面色值(`#RRGGBB`) | 第二页图快写个 `#2A2A2A` |
| 页面不调 `setStyleSheet` | 第三页开始每个控件各自旁路主题 |
| 页面不构造 `QColor`/`QBrush`/`QPalette` | 自己造的颜色躲过红光映射 |
| `setSpacing`/`setContentsMargins` 不收裸数字 | 每页留白各调各的 |
| 页面不 `QFrame()`/`QGroupBox()` 手搓容器 | 三页之后有三种卡片 |
| `theme.C.X` / `theme.Q.X` 的名字必须存在 | `__getattr__` 拼错要跑到那一行才炸 |
| 两套调色板字段完全一致 | 红光下某个控件颜色取不到 |
| 红光配色每一档红占优 | 抄漏的青色强调色在望远镜旁边就是事故 |
| `stylesheet()` 跟着模式走(断言限定到 `#Chip[tone="ok"]` 那一条规则) | `set_mode` 没真的生效 |
| `screen_color` 在红光下把绿点滤成红 | 共享层的颜色漏过滤 |
| 每个 Page 子类都在 `PAGE_CLASSES` 里 | 写完忘了注册 = 永远打不开的死模块 |

### `tests/test_qt_wiring.py` —— 接线

| 断言 | 挡住什么 |
|---|---|
| 文案里的「某某」必须是真按钮的文案 | 叫用户去点一个不存在的东西 |
| 每个 `W.button` 都给了 `on_click` | 没接槽的按钮和接了的长得一模一样 |
| 页面类都继承 `Page` | 少了 `bg`(世代计数器)/ `on_connected` / 主题钩子 |
| 页面里不直接起 `Thread` | 绕过信号编组 → 随机重绘错乱 |
| 有后端慢调用的页面必须用 `with_client` | 慢调用跑在 GUI 线程上 |

### `tests/test_qt_models.py` —— 页面模型(**不需要 Qt**)

合成日志(一定跑)+ `.tmp/` 真日志(有就跑)。盯的是"空文本节点"与"判读口径":

- 目标行/段列表/事件时间线的主文本不能是空串
  (根因永远是读了不存在的键,`.get()` 不报错)
- 「看这段导星」的 `t0` 不能是 None(属性叫 `begin_time`/`end_time`)
- 夜次下标**夹回去**,不是硬写 0(硬写 0 = 下拉"点了没反应")
- 天球图整图同一时刻
- 甘特条永远不是零宽
- 段列表键带前缀 `g:`/`x:`/`r:`;组能折叠
- 默认选中**主段**(第 0 行常是校准或几帧的短尝试,都画不出曲线)
- **量程按整段算,不随窗口变**
- **包络判据按窗口内帧数**,不是整段
- 丢星刻度**先裁窗口再均匀抽稀**,不能截前 N 个
- 整体 RMS 按帧数平方加权(真日志上验;简单平均会被碎段拖爆)

### `tests/test_qt_interaction.py` —— 真窗口(offscreen)

需要一台设备(本地目录即可),没有就整份 skip。

- 连上之后**当前页要再 on_show 一次**(用记录页验 —— 浏览页有自己的
  `on_connected`,拿它验等于什么都没验)
- 长目录(几百行):行数对得上、**表自己能滚**、祖先里没有 `QScrollArea`
- 行身份是路径不是下标
- 多选计数跟着选中走(选 3 → 取消 1 → 计数变 2)
- 多选模式下 `select_key` 不会清掉其他选中
- **世代计数器**:慢的先发起、快的后发起,只有后者算数
- 世代计数器**每页各一个**
- 写操作失败要显示出来
- 红光切换后 QSS 真的重算

---

## 4. 线程模型

| 场景 | 做法 |
|---|---|
| 列目录 / 扫描 / 解析日志 / 占用统计 | `self.bg.run(work, gen=gen, on_done=…)`;`work` 里 `with_client(factory, fn)` |
| 每个后台任务的连接 | `with_client` 自己 `factory()` 建、跑完关。**impacket 连接不是线程安全的** |
| 结果回 UI | 只经 signal(`Bg` 内部是队列连接,`on_done` 一定在 GUI 线程) |
| 迟到的结果 | `gen = self.bg.bump()` 开一代,`stale(gen)` 的整份丢弃 |
| 可停的长任务 | `CancelToken` → `threading.Event` 传给核心库的 `cancel=` |
| 传输进度 | TransferManager 的 `on_update` 只置脏,外壳 250ms 一拍统一刷 —— 10 Hz × N 会把事件循环淹掉 |
| 心跳 / watcher | 各自守护线程 + 各自的连接,结果 `emit` 回来 |

---

## 5. 完成度

| 页 | 状态 |
|---|---|
| 浏览 | 完成:共享栏 / 文件表(排序、搜索可停、勾选批量、子项计数)/ 详情(缩略图、FITS 头、天文判读卡、迷你天球雷达)/ 下载、上传、新建目录、重命名、删除(模态确认) |
| 传输 | 完成:统计大数字、三分区、进度条、aria2NG 分块方块图、按 job_id 持久化的行、单个/全部取消、清除已完成、文件内分块并发档位 |
| 设备管理 | 完成:已记录设备 + 本机卷自动识别 + 手动添加(校验在工作线程做) |
| 扫描设备 | 完成:当前连接卡、网段扫描(判据只认 SMB 协商)、协作式停止、★ 疑似 ASIAIR |
| 拍摄记录 | 完成:夜次下拉、整夜概览、甘特时间线(可点)、天球图 + 放大层(时刻滑杆)、目标列表、详情(帧数/导星/事件时间线)、跳导星页/浏览页 |
| 导星分析 | 完成:按目标分组折叠的段列表、主曲线(时间窗 + 位置滑杆 + 包络视图)、七张统计小图、从记录页跳过来的区间高亮 |
| 空间分析 | 完成:嵌套 treemap(画布 + 命中反查)、目录树、双向联动、下钻/上级 |
| 影像查看 | 完成:确定式下载进度(带 MB)、三档拉伸、直方图、天文判读卡、影像结构、板解算、原始 FITS 头对话框 |
| 3D 天球 | 完成:**真 three.js**(QtWebEngine 跑 `astro_smb_app/web/sky3d.js`,一个字没改);装的是 PySide6-Essentials 时降级到 QPainter 正射球面 |

**没做的功能**(明说,不是"没做完"):拖出到资源管理器(没有跨平台等价物)、
影像查看页的星点叠加、3D 天球的足迹框(要每张 sub 的 WCS,是另一条链路)。

> **"需要 QtWebEngine 所以做不了"这句话是我编的。** 它已经在 PySide6 里 ——
> 完整包 665 MB,其中 208 MB 就是 QtWebEngine。这笔钱在 `uv --with pyside6`
> 那一刻就付过了,`pyproject.toml` 一个字都不用改。我当时没量就下了结论,
> 还照着那个结论把 3D 天球砍成占位、写进文档当"已知限制"。
> 量一下花了两分钟。**下次先量。**

---

## 6. 已知差距 / 抽取层的缺口

- **页面模型那一层在共享层里没有。** `views/records.py` 给的是"一个夜次怎么算",
  而"这一页现在显示哪一夜、选中哪个目标、曲线窗口切到哪儿"住在
  Uno 前端里(已删;当时跟协议与子进程缠在一起,不能 import)。
  本套在 `astro_smb_qt/models.py` 里重写了这层**结构**代码,**阈值与公式
  一个都没有复制**。要收口的话,那些函数应该下沉到 `views/`。
- **`views.browser.detail_rows` 把语义色烤成了 `#AARRGGBB`**(为浅色主题调的
  三档),换主题就没法用。本套用 `_flat_rows` 保留 `tone` 名。同一个问题也在
  `views.records._TL_PALETTE`(RGB 元组)与 `views.skychart` 的常量色上,
  本套统一过 `theme.screen_color()` 兜住。
- **本地目录当设备时,`volume_info` 报的是 PC 的盘**,不是设备的。空间分析页
  与浏览页的容量数字因此对不上真机 —— 这是诚实的(它确实就是个本地目录),
  但看数字时要心里有数。
- ~~`tests/test_docs_are_honest.py` 会红~~ **已解**:docs/DEVELOPMENT.md §3 的目录树里
  已经有 `astro_smb_qt/` 那一段。
- ~~`tests/test_legacy_ui_freeze.py` 也红~~ **已解**:基线已按"没碰过老 UI"
  重算并记进变更表。

## 7. 截图

`.tmp/shots/`(gitignore,只在 worktree 磁盘上):

| 文件 | 内容 |
|---|---|
| `01-browse.png` | 浏览页 + 详情(缩略图/判读卡) |
| `02-transfers.png` | 传输队列(3 个真任务) |
| `03-devices.png` | 设备管理 |
| `04-scan.png` | 扫描设备(本网段 4 台真 SMB) |
| `05-records.png` | 拍摄记录(甘特 + 天球 + 详情) |
| `06-guiding.png` | 导星分析(包络曲线 + 六张小图) |
| `07-space.png` | 空间分析(treemap) |
| `08-fits.png` / `09-sky.png` | 影像查看(拉伸 + 直方图 + 判读卡) / 3D 天球(three.js) |
| `10-browse-red.png` | 红光模式 |

重截:`uv run --with pyside6 python -m astro_smb_qt.shots --host "<…>"`。
