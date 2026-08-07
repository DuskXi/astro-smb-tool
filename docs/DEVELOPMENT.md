# Astro SMB Tool 开发指南

> 本文件面向**没有历史上下文的开发者**,目标是让你无需追溯提交记录即可
> 全面理解本项目、稳定地继续开发。**务必先通读本文,尤其是「线程模型」
> 「win32more 的坑」「非常规设计(野路子)」三节** —— 这些是这个项目里最容易
> 踩雷、也最反直觉的部分。
>
> 这个项目一半的价值在功能,另一半在**趟过的坑**。§7 那份清单里几乎每一条
> 都对应一次真机上的失败,而且大多数是**不报错的那种** —— 不崩溃、不违反
> 任何契约,只是行为悄悄不对。改动前先确认你没有在重新踩已经解决的雷。
---

## 0. 一句话概述

**Astro SMB Tool** 是面向天文摄影工作流的 SMB 文件管理、FITS 查看与日志分析工具。
当前优先兼容 ZWO **ASIAIR** 的共享目录和日志格式，但 ASIAIR/ZWO 只表示受支持的设备
与数据格式，不是软件品牌；窗口标题、包名、命令和应用数据目录一律使用 Astro SMB Tool。
当前分三层交付:

1. **核心库** `astro_smb`(纯 impacket,不依赖 GUI)—— SMB 2/3 直连、枚举/浏览/搜索/上传下载/
   分块并发/卷容量/占用统计/FITS 头解析,以及**天文日志解析**(Autorun 日志/PHD2 导星日志/
   影像文件名/天文坐标计算)。
2. **CLI** `astro-smb-tool` —— 核心库的命令行入口。
3. **WinUI3 GUI** `astro-smb-tool-gui` —— 基于 `win32more` 的原生 WinUI3 前端(**无需 Windows App
   SDK 工具链**),导航包含浏览 / **拍摄记录** / **导星分析** / 3D 天球 / 影像查看 /
   空间分析 / 设备管理 / 扫描设备 / 传输监控;
   另有常驻**运行状态 watcher**(状态栏显示"正在拍摄 <目标> 第N张")。

**为什么存在**:Windows 资源管理器打不开 `\\192.0.2.225`,是因为系统默认禁止匿名 Guest
访问(`AllowInsecureGuestAuth` 策略),**与 SMB1 无关**。impacket 从协议层直连,绕开该策略。

---

## 1. 环境 / 运行 / 依赖

- **包管理:uv**(不是 pip/poetry)。Python **3.13**(`.python-version` 固定,`uv run` 自动用它)。
- 依赖(`pyproject.toml`):`impacket>=0.13.1`、`win32more>=0.8.1`、`numpy>=2.0`、`pillow>=10.0`;
  dev:`pytest`。
- **系统前提**:Windows 11(或 Win10 21H2+),已装 **Windows App Runtime 2.3**(多数系统自带;
  缺失时启动会弹官方安装提示)。win32more 0.8 的 wheel 自带 Bootstrap DLL,但**运行时框架包
  (MSIX)需系统已安装**。
- 常用命令:
  ```bash
  uv sync                       # 安装/同步依赖(editable 安装本项目)
  uv run astro-smb-tool <子命令>     # CLI
  uv run astro-smb-tool-gui         # GUI
  uv run pytest tests/ -q       # 离线单测(不连设备)
  ```
- **入口点**(`pyproject.toml [project.scripts]`):
  `astro-smb-tool = astro_smb.cli:main`,`astro-smb-tool-gui = astro_smb_gui.app:main`。

---

## 2. 目标设备事实(ASIAIR,实测)

- 地址 **DHCP,会变**(2026-07-31 实测 `192.0.2.227`,更早为 `.225`;见扫描页);端口 445;Samba 4.9.5-Debian;**SMB 3.1.1**;
  匿名 `''/''` 登录成功(`isGuest=0`)。可写(上传/删除/改名实测通过)。
- 共享(名字**带空格**):`EMMC Images`(约 222GB)、`Udisk Images`、`TF Images`(各约 6.83GB);
  另有隐藏 `print$`/`IPC$`。
- 典型目录:`EMMC Images/Autorun/{Bias,Dark,Flat}`,`.fit` 单张约 **49.77MB**
  (ASI2600MC Pro,6248×4176,BITPIX16,BAYERPAT RGGB)。
- **关键**:每个 `.fit` 旁有同名 **`_thn.jpg` 缩略图(约 18KB)** —— 预览优先拉它,不碰原图。
- 吞吐:单流约 6 MiB/s(MaxReadSize 仅 1MiB,受 RTT 限制);4 并发约 9.6 MiB/s,8 并发后饱和。
- **网络坑**:该 LAN 的路由器(RT-BE88U,在 .7)会对**整个网段**的 TCP 445 SYN 秒回 ACK
  (1ms 就"连上"),但只有真机能完成 SMB 协商。**扫描必须以 SMB 协商成功为准**,否则 254 个 IP
  会误报 200+ 台。真实 SMB 设备约 4 台:.225 ASIAIR、.11 DUSK-N100 NAS、.7 路由器、.10 PC。

---

## 3. 目录结构与模块职责

```
astro_smb/                 # 核心库(纯 impacket,可独立于 GUI 使用)
  client.py      (1117)     # AstroSmbClient:连接/枚举/浏览/搜索/上传下载/卷容量/统计/占用树
  parallel.py    (215)      # ParallelDownloader:文件内分块多连接并发下载(.part 原子落盘)
  fitshdr.py     (134)      # 极简 FITS 头解析(不依赖 astropy),供低开销预览
  autorunlog.py  (505)      # Autorun 日志解析:会话/Plan/目标块/帧 + 夜次归并(见 §4.4)
  phd2log.py     (393)      # PHD2 导星日志解析:段/帧/校准 + RMS/区间求交(见 §4.5)
  astro.py       (140)      # RA/DEC 字符串解析、GMST/LST、alt-az、站点经度反推(见 §4.6)
  naming.py      (79)       # ASIAIR 影像文件名解析(时间戳锚点回推,字段全可缺失)
  util.py        (56)       # human_size / parse_size / format_mtime / sanitize_local_name
  cli.py         (518)      # argparse CLI:info/shares/ls/tree/find/get/put/df/du/header/...
  devices.py     (315)      # 设备记录 devices.json(B20 从 astro_smb_app 下沉,CLI 也要用)
  paths.py       (95)       # 跨平台数据/缓存目录 + curl 可执行名(Windows 路径一字未变)
  __main__.py               # python -m astro_smb

astro_smb_app/             # 共享应用层(两套前端共用;**不 import 任何前端**)
  devices.py                # (shim → astro_smb.devices,B20 下沉到核心库)
  dircache/metacache/       # 目录与元数据缓存(sqlite)
  logstore.py               # 日志下载/缓存/聚合 + 站点经度推算
  preview.py                # PreviewWorker:低开销预览(_thn.jpg 优先)
  skymap.py                 # 巡天底图下载 + alt-az 重投影
  transfers.py              # TransferManager:队列/并发/重试/冲突
  volumes.py watcher.py     # 本机卷枚举 / 运行状态 watcher
  entries.py                # 条目排序/扩展名分类/去重名(纯函数)
  web/                      # 天球页静态资产(两套前端共用,B10 从 gui 移来)
  views/                    # **各页视图模型**(纯函数,两套前端消费同一份)
  bundle.py                 # 打包后的资源定位(冻结/onedir 差一层的坑)

astro_smb_qt/              # **跨平台前端:PySide6 / Qt(纯 QWidget,不是 QML)**
  theme.py                  # 颜色/字号/间距的唯一真源 + 白天/常规/红光三档
  widgets.py                # Card/StatusChip/SideNav/MetricRow/DataTable/OpsCanvas/Gauge…
  models.py                 # 页面模型(纯函数,脱离 QApplication 可单测)
  workers.py shell.py       # 后台线程+世代计数器 / 外壳
  pages/                    # 九页,一页一个模块
  shots.py human_window.py  # 截图脚本 / 人类体验窗口
  # 验收清单在 docs/qt-final.md —— 一页没在那里被勾掉就不算做完

astro_smb_gui/                 # WinUI3 前端(win32more)—— **已冻结**,是 fallback
  app.py         (24)       # 入口:appsdk.initialize() 后拉起 _window.App
  _window.py     (741)      # App 外壳:NavigationView + 6 页宿主 + 连接/心跳/watcher/传输条(分组)
  _browser.py    (1645)     # 浏览页:浏览/预览/传输/拖拽/搜索/删除 + 副行懒加载 + rich 详情卡片
  _records.py    (2171)     # 拍摄记录页:夜次/统计卡/甘特/结构化时间线/天球(底图+放大遮罩)
  _guiding.py    (1343)     # 导星分析页:段列表/曲线(窗口+包络)/8 项统计图表/show_range
  _space.py      (701)      # 空间分析页:嵌套 treemap(SpaceSniffer 式)+树形明细双向联动
  skymap.py      (268)      # 巡天底图:下载缓存 + 等距柱状→alt-az 重投影(见 §7.11a)
  skyview.py     (121)      # 共享天球雷达:MiniRadar(浏览页) + radar_xy 几何
  _scan.py       (259)      # 扫描页:局域网找 445/SMB 设备(DHCP 换 IP 时用)
  _monitor.py    (518)      # 传输监控页:分区 + 文件夹分组折叠 + aria2NG 分块方块图 + 统计
  _common.py     (109)      # 各页共享:排序/去重名/glyph/_spin/unbox_str 等
  preview.py     (359)      # PreviewWorker:低开销预览 + read_fits_header(与懒加载共用)
  devices.py               # 设备记录:devices.json 读写(host/名字/协议/共享数/last_ok)
  logstore.py    (225)      # 日志数据层:下载缓存/解析聚合/站点经度推算(见 §7.12)
  watcher.py     (165)      # 运行状态 watcher:帧 mtime 心跳 + 新日志侦测(见 §7.13)
  transfers.py   (408)      # TransferManager + TransferJob:队列/并发/重试/冲突/分块状态
  dragout_ole.py (262)      # 【野路子】原生 OLE 虚拟文件:IDataObject+IStream,下载即拖
  *.xaml                    # main + 六个页面(browser/records/guiding/space/scan/monitor)

tests/
  test_core.py              # 路径解析 / FITS 头 / util(离线)
  test_features.py          # 卷容量 / 扩展名排序 / 网段识别 / 传输冲突 / 并行 .part(离线)
  test_astrolog.py          # Autorun/PHD2 解析 / astro 数学 / 文件名解析(离线+真机样例)
```

---

## 4. 核心库 `astro_smb`

### 4.1 `AstroSmbClient`(client.py)—— 最重要的类

- **构造**:`AstroSmbClient(host="192.0.2.225", port=445, username="", password="", timeout=15, chunk_size=4MiB)`。
- **连接非线程安全**:内部用 `self._lock`(RLock)串行化。**多线程/并行传输必须各自
  `client.clone()` 出独立实例**,绝不能共享一个 client 跨线程。
- **统一错误类型**:对外只抛 `SmbClientError`(和它的子类 `TransferCancelled`)。impacket 的
  `SessionError`、socket 错误一律在内部转成 `SmbClientError`,message 已人类可读(中文)。
  **调用方只需 catch `SmbClientError`**。
- **`_run(op, *, idempotent=True)`**:所有只读/幂等操作的执行壳。连接类错误会**重连一次**再重试,
  并保证异常统一包装。`idempotent=False`(rename/remove/rmdir)时**不自动重放**——第一次可能已
  在服务器生效,盲目重试会误报失败,改为报"操作可能已生效"。
- 关键方法:
  - `list_shares(include_hidden=False)` → `list[ShareInfo]`
  - `listdir(share, path="")` → `list[RemoteEntry]`(目录在前,已排序)
  - `stat / exists`
  - `walk(share, top, max_depth, on_error, depth_first)` —— 类 os.walk,BFS 默认
  - `find(...)` —— 递归搜索(fnmatch,不区分大小写,支持大小/时间过滤,支持 cancel)
  - `read_bytes(share, path, offset, size)` —— **部分读取**(预览低开销的关键)
  - `download_file(..., resume=False)` / `download_dir(...)` —— 顺序下载,支持进度/取消/续传
  - `download_range(share, path, offset, length, fh, cancel, on_bytes)` —— **把某区间写到已打开的
    本地文件句柄的对应偏移**;供 `ParallelDownloader` 分块调用
  - `upload_file / upload_dir / makedirs / mkdir / remove / rmdir(recursive) / rename`
  - `volume_info(share)` → `VolumeInfo|None` —— **卷容量**(见野路子)
  - `count_children(share, path)` → `(ndir, nfile)|None`(失败返回 None,区别于真空目录)
  - `dir_stat(share, path, cancel, on_progress)` → `DirStat`(递归总大小/文件数/目录数)
  - `scan_children(share, path, cancel, on_item)` → `list[(entry, size)]`(直接子级占用,目录取
    递归大小;单层)
  - `dir_tree(share, path, cancel, on_progress)` → `TreeNode`(一次 BFS 建整棵占用树,
    所有嵌套目录递归大小一次算全;空间页嵌套 treemap 用;cancel 抛 TransferCancelled)
  - `reconnect()` —— 公开强制重连(供分块 worker 弱网重试)
- **数据类**:`ShareInfo`、`RemoteEntry`(share/path/name/is_dir/size/mtime/ctime/atime/attributes,
  含 `display_path`、`attr_text()`)、`VolumeInfo(total, free)`、`DirStat`。
- **路径约定**:内部一律用反斜杠分隔、共享内相对路径(根为 `""`)。用户输入经
  `split_remote_path("EMMC Images/a/b")` → `("EMMC Images", "a\\b")`,支持 `/`、`\`、
  `\\host\share\...`、`smb://host/share/...`。`normalize_remote_path` 处理 `.`/`..`/多斜杠。
  **中文路径与含空格共享名全程 OK**(SMB2 线上是 UTF-16)。

### 4.2 `ParallelDownloader`(parallel.py)

- 单文件切成 N 块,`cpu_workers()`(=`min(8, max(2, cpu))`)条**独立连接**并行拉取。
- `plan_chunks(total)` 让块数落在约 64 附近(块 1–16MiB),这样监控页方块图既有意义又不碎。
- 每 worker 各持 client + 各开一个 `r+b` 句柄;**先把 `.part` 临时文件 truncate 预分配**,
  各 worker `seek(offset)+write` **不重叠区间**(Windows 上安全);**全部块完成后才
  `os.replace` 到最终路径,失败/取消删 `.part`** —— 最终路径上绝不能出现"大小==全长但有
  空洞"的文件,否则重试走 `download_file(resume=True)` 会按大小==总长误判已完成(数据损坏,
  已修复的 bug)。
- 回调:`on_plan(n_chunks, chunk_size, workers)`、`on_block(idx, state)`(state:0待/1传输中/2完成)、
  `on_progress(delta_bytes)`。单块级重传(连接错误重连后重读整块,`download_range` 会重新 seek)。
- 状态常量:`PENDING=0, ACTIVE=1, DONE=2`。

### 4.3 `fitshdr.py`

- 手写 FITS 头解析(2880 字节块、80 字节卡片、遇 `END` 结束)。`header_read_hint(probe)` 估计
  还需读多少字节才能拿全头。`FitsHeader.summary()` 挑常用拍摄参数,`.naxis/.bitpix/.data_size()`。
- **只需从 SMB 部分读取头部几 KB** 即可拿到曝光/增益/温度等,不下载几十 MB 原图。

---

### 4.4 `autorunlog.py` —— Autorun 日志解析(纯标准库)

- 日志在 `EMMC Images/log/Autorun_Log_YYYY-MM-DD_HHMMSS.txt`(同名 `_CHN.txt` 中文版忽略)。
- **层级**: 文件 ⊃ 会话(`Log enabled/disabled`) ⊃ 可选 `Plan N` ⊃ `[Autorun|Begin] <目标>` 块
  ⊃ Shooting 组 + `Exposure Xs image N#` 帧。`parse_autorun_log(text, source)` → `AutorunLog`。
- **已验证的格式陷阱**(改解析器前必读): 裸 `Exposure 2.0s`(无 image#)是 AutoCenter 曝光不算帧;
  帧号同目标内跨组连续(bias 1-30 后 dark 从 31 起); Pause/恢复把同一 Plan 分裂成多会话/多文件;
  存在无 Shooting 行的块(逐帧变曝光); 事件可乱序 —— 必须逐行状态机。
- `aggregate_nights(logs)` → `list[Night]`: 按**正午分界**归夜(晨间平场归前一夜),同夜同
  (plan_no, 目标) 的分裂块合并为 `TargetRun`(带 type_stats/frame_span/finished)。
- **关键设备事实**: 日志是**会话结束时一次性写盘** —— 运行中设备上看不到日志;
  新日志出现 = 会话刚结束(watcher 靠这个失效缓存)。

### 4.5 `phd2log.py` —— PHD2 导星日志解析

- `PHD2_GuideLog_*.txt`,Log version 2.5(ASIAIR 定制,"PHD2 version" 后为空)。
- `parse_phd2_log(text)` → `Phd2Log{guide_sections, calibrations}`。Guiding 段帧行 18 列 CSV;
  段头有 **Pixel scale(按段取用,同文件内会变)**/曝光/相机/Dec/时角/Pier side。
- **容错**(真机踩过): Begins/Ends 不配对(失败校准后有孤立 Ends)、文件无收尾截断、
  mount 名不稳定。帧 `ErrorCode!=0 或 SNR==0` = 星丢失,**RMS 统计必须剔除**。
- `section_rms(sec)`、`rms_for_interval(logs, t0, t1)`(与拍摄帧区间求交 → "这张 sub 期间导星
  多稳")、`guide_coverage(logs, t0, t1)`。帧绝对时刻 = 段 begins + Time 列秒。

### 4.6 `astro.py` / `naming.py`

- `astro`: `ra_str_to_deg('17h22m35s')` / `dec_str_to_deg('-36°7'40"')`、`format_ra/format_dec`、
  `gmst_deg/lst_deg/altaz`(方位角北 0 东 90)、**`estimate_longitude(ra, ha_hours, when_local)`**
  —— 用 PHD2 段头时角 + 同时刻拍摄目标 RA 反推站点经度(LST=RA+HA; 真机推得 121.44°E,
  样本中位数,logstore._estimate_longitude 负责配对)。naive 时间按**本机时区**换算 UTC
  (前提: PC 与 ASIAIR 同时区)。纬度无法从日志推,由用户在拍摄记录页设置(logstore.load_site)。
- `naming.parse_image_name` → `ImageName`: 文件名语法
  `<类型>[_<目标>]_<曝光>_Bin<n>[_<滤镜>]_<YYYYMMDD-HHMMSS>[_<角度>deg][_<序号>][_thn].fit/jpg`,
  以时间戳为锚点回推,目标含空格、滤镜/deg/序号全可缺失; **滤镜字段(4C/Dul/1)是滤镜轮槽位不是
  温度**; 文件名时间 = 本地曝光结束时刻(= 日志 image N# 行 + 曝光 + ~1s)。

## 5. CLI(cli.py)

子命令:`info`、`shares`、`ls`、`tree`、`find`、`get`(`--jobs N` 分块并发、`--resume`)、
`put`、`cat`、`header`、`df`(卷容量)、`du`(`-c` 按子级占用)、`mkdir`、`rm`(`-r`)、`mv`。
默认 host 与 GUI 同一条规则:`ASTRO_SMB_HOST` > `devices.last_host()` >
**不猜**(打印"用 -H 指定"后退出 2)。**不再硬编码任何地址** —— 设备是 DHCP,
写死的 IP 只会让每条命令等 15 秒超时。远程路径**含空格要加引号**。
Windows 控制台强制 UTF-8 输出避免中文乱码。进度条在非 TTY 时静默。

---

## 5b. **两套前端**(2026-08-03 起,必读)

```
astro_smb            核心库(SMB/本地后端、FITS、天文、日志、板解算、导星逆推)
   ↑
astro_smb_app        共享应用层 —— 设备记录/缓存/传输队列/预览/日志聚合/卷枚举,
   ↑                 外加各页的**视图模型**(views/)。**不 import 任何前端。**
   ├── astro_smb_gui      WinUI3 / win32more —— Windows 原生,**并且是界面原型**
   └── astro_smb_qt       PySide6 / Qt —— **跨平台交付**(含 Windows)
```

### 方向(用户 2026-08-03 定的)

> **Windows 上双 UI(WinUI3 + Qt),跨平台走 Qt。
> UI 以 WinUI3 为原型开发,再同步实现到 Qt。同步是单向的。**

也就是说:**新界面先在 WinUI3 上成形,Qt 跟着实现;不要反过来。**
这条路径本身被验证过 —— Qt 那套就是照着 WinUI3 逐页复刻做出来的,
`docs/qt-final.md` 走完九页独立验收 103 条过。

**曾经有第三套(Uno:Python 主进程 + C# 通用渲染器),2026-08-03 判为不成功、
已删除**(`astro_smb_app/ui`、`astro_smb_app/proto`、`frontend/`、`packaging/`)。
存档在标签 `archive/before-uno-removal`。它最贵的 bug 全是同一类:
**图元在词表里 ≠ 渲染器实现了它** —— 不报错、不违反契约,只是界面行为不对,
而页面作者会照着词表写完、对着没反应的界面发呆。这是"通用渲染器 + 词表"
这条路线的固有代价,也是它被放弃的原因。

### 老 UI 不再冻结,但**改了要同步**

`tests/test_legacy_ui_freeze.py` + `docs/architecture/legacy-ui.sha256` 仍在,
但含义变了:冻结当初是"新前端没追平之前别再往前跑",现在追平了。
门禁现在的作用是**同步提醒**——

- 改了 `astro_smb_gui/` → 测试变红 → 重算基线,并在
  `docs/architecture/frontend.md` 的变更表里记一笔;
- 那一笔**必须写清楚**:已经同步到 Qt 了,还是本轮不同步、原因是什么。

真正的风险不再是"老 UI 乱动",而是**两套 UI 悄悄分叉**。

### 启动

```bash
uv run astro-smb-tool-gui                    # WinUI3(Windows,界面原型)
uv run astro-smb-tool-qt                     # Qt(跨平台交付)
uv run --with pyside6 python -m astro_smb_qt --help   # 同上,带全部调试参数
```

Qt 侧的验收清单在 `docs/qt-final.md`,上手与设计规格在 `docs/qt-ui.md`。

## 6. GUI 框架:win32more + WinUI3(**必读**)

本项目 GUI **不是 tkinter/PyQt/PySide**,而是**原生 WinUI3**,通过 `win32more`(WinRT 的 Python
投射)驱动。**无需安装 Windows App SDK 的 C++/NuGet 工具链**,纯 pip 依赖即可。

### 6.1 启动流程

```python
# app.py
from win32more import appsdk
appsdk.initialize()                         # 必须最先:引导 Windows App Runtime
from win32more.winui3 import XamlApplication
from astro_smb_gui._window import App
XamlApplication.Start(lambda: App())        # 阻塞,进入 XAML 消息泵
```

- `appsdk.initialize()` **必须在任何 WinRT 激活之前**调用(所以 WinUI 的 import 放在 initialize 之后)。
- `App(XamlApplication)` 覆写 `OnLaunched(args)`:建 `Window`、设 `MicaBackdrop`、
  `XamlReader.Load(xaml文本)` 得到根元素、`self.win.Content = root`、`self.win.Activate()`。

### 6.2 线程模型(**最关键,违反必炸**)

- **UI 线程 = STA + XAML 消息泵 + 手摇 asyncio 循环**(win32more 用 `SetTimer` 每 100ms crank 一次
  asyncio loop)。`OnLaunched` 和所有 XAML 事件回调都在 UI 线程。
- **事件处理器可以是 `async def`**,里面 `await` WinRT 的 `IAsyncOperation` 或 `asyncio.sleep` 都行
  (win32more 自动把 async 处理器调度到 UI 线程的 loop)。
- **XAML 对象有线程亲和性**:**绝不能从工作线程直接碰任何 XAML 控件**,否则 RPC_E_WRONG_THREAD。
- **跨线程编组**:`DispatcherQueue.GetForCurrentThread()`(在 UI 线程捕获)+ `TryEnqueue(callback)`。
  本项目里 shell 暴露 **`shell.ui(fn, *args)`** = `dispatcher.TryEnqueue(lambda: fn(*args))`,
  **所有工作线程回 UI 的更新都必须走它**(见 transfers/preview/scan/space/browser 的 worker)。
- **重 I/O 出线程**:SMB 浏览用 `await asyncio.to_thread(client.xxx, ...)`;传输/预览/扫描各起自己的
  `threading.Thread`,各持独立 client,结果经 `shell.ui(...)` 回编组。
- **关窗**:`_on_closed` 里 `client.close()` 要拿 RLock,而浏览可能正卡在超时里持锁——所以用
  **守护线程**关 client,避免 UI 线程冻结(进程退出 OS 回收 socket)。

### 6.3 XAML 加载与控件访问惯用法

```python
root = XamlReader.Load(Path("x.xaml").read_text(encoding="utf-8")).as_(FrameworkElement)
btn  = root.FindName("MyButton").as_(Button)      # FindName 返回的东西必须 .as_() 强转
btn.Click += self.on_click                        # 事件用 += 挂(处理器签名 (sender, args))
btn.Content = "文本"                               # 属性可直接赋值
# 事件里 sender/args 也常要再强转:sender.as_(ScrollViewer)
```

- **`.as_(T)` 到处都要**:`XamlReader.Load`、`FindName`、事件 `sender`/`args.SelectedItem` 返回的都是
  `IInspectable`,不 `.as_()` 就没有目标类型的属性/方法。
- 集合是 WinRT `IVector`:用 `.Append(x)` / `.Clear()` / `.RemoveAt(i)` / `.GetAt(i)` / `.Size`,
  **没有 `.add()`**。`UIElementCollection` 无"按值 Remove",要移动元素用 `Clear()` 全清后重 `Append`。

### 6.4 GUI 架构:外壳 + 6 页

- **`_window.py` 的 `App`** 是外壳:顶部连接栏、`NavigationView`(6 个菜单项)、`PageHost`(Grid)、
  底部常驻传输精简条。它拥有共享的 `self.client`、`self.transfers`(TransferManager)、
  `self.preview`(PreviewWorker)、`self.logstore`(LogStore)、`self.watcher`(RunWatcher)、
  `self.dispatcher`。
- **页面对象**:`BrowserPage / RecordsPage / GuidingPage / SpacePage / ScanPage / MonitorPage`,
  各自 `XamlReader.Load` 自己的 xaml 建 `self.root`,持有 `shell` 引用。切页时
  `PageHost.Children.Clear()` 再 `Append(page.root)`(页面实例复用,状态保留)。
- **页面接口约定**:每页实现 `on_show()`、`on_connected(shares)`(连接成功后 shell 广播)、可选
  `on_close()`。程序化切页用 `shell.select_page(tag)`;页面间跳转用
  `shell.open_guiding(t0,t1,label)` / `shell.open_browser_path(share,path)`。
- **shell 提供给页面的基础设施**:`shell.ui(fn,*args)`、`shell.busy(bool)`、`shell.status(text)`、
  `shell.error(text)`/`shell.info(text)`(InfoBar)、`shell.hwnd()`、`await shell.confirm(title, msg)`
  (应用内 ContentDialog 二次确认,失败回退 tkinter)。

---

## 7. **非常规设计 / 野路子清单**(本项目灵魂,改动前务必理解)

> 这些都是绕过 win32more/WinUI3/impacket 各种限制的"非常规"手段,很多是实测踩坑后的唯一可行解。
> **改动相关代码前先读懂这里,否则很容易破坏已验证的行为。**

### 7.1 win32more 具体坑(会静默失败或抛异常)

- **委托不能用 `Handler(callback)` 构造**(报 `TypeError: cannot be converted to pointer`)。直接把
  Python 可调用对象当参数传,调用点自动包装。例:`pack.SetDataProvider(fmt, provider_callable)`。
- **`NavigationView.SelectionChanged` 里 `args.SelectedItem` 是 `IInspectable`,没有 `.Tag`**。必须
  `args.SelectedItem.as_(NavigationViewItem).Tag`,再用 `_common.unbox_str()`(内部
  `.as_(IPropertyValue).GetString()`)取回 XAML 里设的字符串 Tag。**直接 `str()` 只得到 repr**。
  —— 这个坑当时导致"点导航不换页",排查了很久。
- **`IAsyncOperation` 没有 `.Status`**:非 UI 线程同步等待要 `op.as_(IAsyncInfo).Status`,见
  `_common._spin(op)`。
- **可编辑 ComboBox 不显示"代码设置的 Text"**(探针实证 `scratchpad/probe_host2.py`):
  `cb.Text = "192.0.2.228"` 属性读回正常,但编辑框视觉上仍是占位符/空白;
  富内容项(StackPanel)被选中时编辑框同样空白。**唯一可靠显示方式 = 选中一个
  `Content` 为纯字符串的项**(`SelectedIndex = i`)。因此设备下拉的状态/时延
  拼进项字符串,取回真地址走 `_dev_hosts[SelectedIndex]`(`Text` 此时是整条富文本,
  直接拿去连会失败 —— 真机踩过)。用户手输的文本能正常显示,不受影响。
- **文本里绝不能用 emoji(星平面字符)**:win32more 把 Python str 转 HSTRING 时按**码点数**
  给长度,而 HSTRING 是 UTF-16 —— 每个代理对(📁/🌌/emoji 等)会让字符串**末尾少一个字符**。
  真机现象:空间页目录名 `Plan`→`Pla`、监控页组名 `M 8`→`M `(排查了很久才定位)。
  一律用 BMP 字符(`▣ ▶ ▼ ★ ● ◉ ✓ ✗` 等)或 `FontIcon`(Segoe MDL2 私用区码位也是 BMP)。
- **`ToggleSwitch` 在 `Microsoft.UI.Xaml.Controls`,但 `ToggleButton` 在 `...Controls.Primitives`**。
- **`FontWeights` 在 `Microsoft.UI.Text`,不在 `Xaml`**(`FontWeights.SemiBold`)。
- **`ComboBoxItem.Content` 是装箱字符串**:取值用 `unbox_str(item.as_(ComboBoxItem).Content)`。
- 结构体构造用**关键字**或先建后赋:`GridLength(Value=1.0, GridUnitType=GridUnitType.Star)`;
  `size = SizeInt32(); size.Width, size.Height = 1460, 920`。`Grid.SetColumn(el, n)` 是静态方法。
- `Window.AppWindow.Id.Value` 直接当 **HWND** 用(给文件选择器 `IInitializeWithWindow.Initialize`
  和 `SHDoDragDrop`)。

### 7.2 卷容量 —— 走 impacket 底层 `queryInfo`(client.volume_info)

impacket 没有公开封装。开共享**根目录**(path=`""`,`FILE_DIRECTORY_FILE`)拿 fid,再
`conn._SMBConnection.queryInfo(tid, fid, infoType=SMB2_0_INFO_FILESYSTEM,
fileInfoClass=SMB2_FILESYSTEM_FULL_SIZE_INFO)`,解析 `smb.SMBFileFsFullSizeInformation`
(单位 = SectorsPerAllocationUnit × BytesPerSector)。ASIAIR 实测可用。

### 7.3 文件内分块并发下载(parallel.py + client.download_range)

见 §4.2。要点:**每 worker 独立 client**;**`.part` 临时文件先 truncate 预分配**再多句柄
seek+write 不重叠区间,**成功才 `os.replace` 到最终路径**(失败/取消删 `.part`);块状态回调
驱动监控页方块图。分块并发数默认按 **CPU 核数**(上限 8)。GUI 侧 `submit_download` 重试时
**始终重走并行**(并行失败不留可续传的半成品,对目标处旧文件 resume 会误判已完成)。

### 7.4 拖出到资源管理器 —— 原生 OLE 虚拟文件"下载即拖"(dragout_ole.py)

**背景(踩坑史)**:非包身份(unpackaged)WinUI3 **无法**用 `DataPackage.SetDataProvider` 延迟渲染把
SMB 文件拖到资源管理器——延迟的 StorageItems 只在进程内可见,不会投射成 shell 能识别的
CF_HDROP/虚拟文件格式;而 `DragItemsStarting` 又没有 deferral,不能在里面等下载。
`CreateStreamedFileAsync` 虚拟文件在无包进程直接 RPC_E_WRONG_THREAD。

**野路子(已真机验证数据面)**:用 win32more **在 Python 里实现 COM 服务** ——
`SmbStream(ComClass, IStream)` + `VirtualFileGroup(ComClass, IDataObject)`,对资源管理器声明
`CFSTR_FILEDESCRIPTORW`(文件名/大小)与 `CFSTR_FILECONTENTS`(TYMED_ISTREAM)。落点松手时资源管理器
**同步调用 `IStream.Read`,我们按需从 SMB 拉对应区间** —— 真正的拖拽即下载,**无需预暂存**。

**Python COM 服务实现模式**(win32more,通用):
- `class X(ComClass, IFace)`,**ComClass 必须在前**;每个 commethod 写**同名** Python 方法,收 self
  之后的全部参数,**返回 HRESULT(int)**;out 参数经指针写回(`pmedium[0].tymed=...`、`pcbRead[0]=n`)。
- `VoidPtr` 参数是 Python **int 地址**,用 `ctypes.memmove(addr, data, n)`;`POINTER(T)` 参数用 `[0]`。
- `STGMEDIUM.u` 是**具名**联合:`med.u.pstm` / `med.u.hGlobal`。
- 返回接口要**自己 `AddRef()`**(GetData 转移所有权)。QI/AddRef/Release 由 ComClass 自带。
- 把实例**直接**传给原生 API:`SHDoDragDrop(hwnd, obj, None, DROPEFFECT_COPY, byref(effect))`
  (`pdsrc=None` 用 shell 默认拖源,免实现 IDropSource)。

**已知取舍**:`SHDoDragDrop` 是**模态**的,资源管理器在**发起 DoDragDrop 的 UI 线程**上同步读流,
故**大文件复制期间窗口会短暂无响应**(资源管理器有自己的进度条);`IDataObjectAsyncCapability`
不在 win32more(不影响,同步读即可)。**真实鼠标手势无法在无人值守环境自动化验证**(数据面
`GetData→IStream→Read` 已验证字节与 SMB 原文件 sha256 一致)。
**兜底**:右键「复制到剪贴板」(暂存真实文件后 `DataPackage.SetStorageItems(...,False)` +
`Clipboard.SetContent/Flush`,资源管理器 Ctrl+V)—— 这条最稳,永远保留。

**手势接线**:`_browser._on_drag_items_starting` 里 `e.Cancel=True` 取消 WinUI 自带拖拽,改调
`_start_ole_drag(files)` 走原生 OLE。仅支持文件(目录虚拟拖出复杂,提示用下载)。

### 7.5 低开销预览(preview.py)

按开销从低到高:① 目录元数据(零 I/O);② FITS 头(只部分读几 KB);③ **ASIAIR `_thn.jpg`
缩略图**(几十 KB,不碰几十 MB 原图);④ 小图片(≤25MB)下载缓存后缩略,文本(≤128KB)直接
部分读取**不落缓存**;
⑤ **大 FITS 拉伸预览仅在用户点按钮时**才下载全图(0.2%–99.8% 百分位拉伸,numpy)。
- **单工作线程只保留最新请求**(浏览快速换选不排队),结果带 token,UI 侧丢弃过期结果。
- **缓存原子写**:下载先写 `.part` 再 `os.replace`,取消/出错删 `.part`,避免半截文件被永久当成完整
  (曾是 bug)。缓存在 `%LOCALAPPDATA%\AstroSmbTool\cache`,超 500MB 按最旧访问时间清理;
  拖出暂存目录 `dragout/` 启动时整棵清。

### 7.6 扫描 + 主机名 + 时延 + 心跳(_scan.py + _window.py)

- **扫描以 SMB 协商为准,而非 TCP**(见 §2 网络坑)。`_identify(ip)` 只在**SMB 协商成功**时返回
  非 None(TCP 被中间盒 ACK 但不说 SMB 的返回 None 被过滤)。匿名登录失败不影响判定。网段默认取
  **当前设备所在 /24**(`on_show` 从 host 框推断)。疑似 ASIAIR(共享含 "Images" 或名字含 ASIAIR)
  **置顶标 ★**。
- **主机名**:`_resolve_hostname(ip)` = 反向 DNS(`socket.gethostbyaddr`),显示优先于 SMB 服务器名;
  只对已确认的 SMB 设备做(避免 254 次慢查询)。
- **时延**:`_probe(ip)` 返回 `(可连, RTT 毫秒)`;每行彩色徽章(`_latency_color`:<30ms 绿、
  <100ms 琥珀、否则红),**内联在主机名右侧**(不放右列,避免窄窗口看不到)。
- **心跳 / 当前连接状态**:shell 有独立心跳线程(`_heartbeat_loop`),用**专用克隆连接**每 ~4s 发一次
  `client.echo()`(SMB2 ECHO,底层 `conn._SMBConnection.echo()`)测存活+RTT;掉线自动重连重试
  (心跳 client `timeout=3`)。状态经 `shell.ui(_apply_heartbeat)` 更新到:① 扫描页顶部**连接状态卡**
  (`scan.on_heartbeat(state)`:绿点在线/红点断线、server_name/OS/dialect/共享数/心跳次数/最近时间);
  ② 顶栏 status 尾巴(`self._hb_tail`,经 `_refresh_status()` 拼在 `_status_base` 后)。
  `AstroSmbClient.ping_tcp()` 在断线时判端口是否可达(区分"网络断"与"会话断")。
  心跳在 `_connect` 成功后 `_start_heartbeat()`,关窗时 `_hb_stop.set()`。

### 7.7 传输监控页(_monitor.py)

- **行对象持久化**(按 job_id 存 dict),**进度 tick 只原地更新字段**;只有分区(进行中/排队/完成)
  变化才 `_relayout()`(清三个 panel 再按 `transfers.jobs` 顺序重 Append)—— 避免每 tick 重建导致闪烁。
- **aria2NG 式方块图**:`Canvas` 上一排小 `Rectangle`,块数下采样到 ≤128,每格聚合若干 chunk,
  绿=全完成、琥珀=有传输中/部分、灰=待传。方块在 `_ensure_blocks` 首次(拿到 n_chunks)建好,
  之后每次只改 `Fill`。
- **阶段标签**区分"元数据/传输"(不同色)。统计条读 `transfers.stats()`。
- shell 的 `_on_transfer_update` 同时喂**底部精简条**和**监控页**(两套行 dict,各自 keyed by job_id)。

### 7.8 传输队列(transfers.py)

- `TransferManager`:线程池(文件间并发 `max_workers`)+ 每 job 重试循环(连接类失败**指数退避**
  重试,下载走续传/分块重传)+ 冲突策略(`rename`/`skip`/`overwrite`,默认改名 `x (1).fit`)。
- 大文件(≥16MB 且分块并发>1)自动走 `ParallelDownloader`,否则顺序 `download_file`。
- `TransferJob` 带 `phase`(排队/连接中/元数据/传输/完成)、`blocks`(per-chunk 状态)、`n_chunks`、
  `parallel`、`speed`、`eta()`、`group`(所属文件夹显示名)。状态常量 `DONE_S="完成"` 等。
- **文件夹下载按文件展开**(2026-07 二轮): 浏览页对目录不再 submit_download_dir,而是后台
  `xfer-expand` 线程 walk 展开为逐文件 `submit_download(group=目录名)` —— 每个大文件自动获得
  分块并行+方块图(整夹下载实测 18 MB/s,3 文件×8 块并发);监控页按 group 分区内分组折叠
  (进行中默认展开,排队/完成默认折叠,同夹文件跨分区时各分区各有组头);底部精简条按组聚合
  一行(取消=cancel_group)。`submit_download_dir` 保留但下载路径不再使用(上传目录不变)。

### 7.9 对话框/选择器用 tkinter 兜底

WinRT 文件选择器(`FileOpenPicker`+`IInitializeWithWindow`)和 `ContentDialog` 都用了,但**多文件
选择、输入框(重命名/新建目录)用 tkinter 兜底**(`_browser._ask_text` / `_tk_pick_files` /
`_tk_pick_folder`),在 `asyncio.to_thread` 里跑(各自建/销毁 `tk.Tk()`,`-topmost` 置顶)。

### 7.10 无人值守测试钩子(环境变量)

GUI 支持这些 env var(仅供开发/自动化截图,生产不用):
- `ASTRO_SMB_GUI_AUTOCLOSE=10` —— 秒后自动关窗;
- `ASTRO_SMB_GUI_START_PATH="EMMC Images/Autorun"` —— 启动直达路径;
- `ASTRO_SMB_GUI_START_PAGE=browse|records|guiding|space|scan|monitor` —— 启动页(space 会自动扫描该路径,scan 会自动开扫);
- `ASTRO_SMB_GUI_AUTODL="EMMC Images/Autorun/Bias"` —— 自动下载该目录前几个大文件(演示监控页分块图);
- `ASTRO_SMB_GUI_AUTODLDIR="EMMC Images/Plan/Light/M 8"` —— 整个文件夹入队(验证按文件展开+分组);
- `ASTRO_SMB_GUI_AUTOSELECT=0` —— 浏览页渲染完成后自动选中第 N 项(验证详情面板/预览);
- `ASTRO_SMB_GUI_SKYBG=1` —— 底图已缓存时自动开启巡天底图;`ASTRO_SMB_GUI_SKYZOOM=1` —— 自动打开天球放大遮罩;
- `ASTRO_SMB_HOST=192.0.2.228` —— 覆盖 GUI 初始设备地址(与 CLI 同名;DHCP 换 IP 后自动化用);
- `ASTRO_SMB_GUI_TITLE_TAG=AUTOTEST` —— 窗口标题加后缀,截图脚本据此区分测试实例与用户实例
  (**用户常开着自己的实例,按标题匹配第一个进程会误抓**,真机踩过);
- `ASTRO_SMB_GUI_MERGEPLAN=1` —— 拍摄记录页自动开启「合并计划」(截图验证用)。

### 7.11a 巡天底图重投影(skymap.py,2026-07 二轮新增)

- **底图**: ESO GigaGalaxy 全天全景 eso0932a(6000×3000 等距柱状,**银道坐标**,
  银心居中,**银经向左增** —— 经 LMC/SMC 亮度不对称自动化校验)。约 8MB,用户
  确认后下载,缓存 `%LOCALAPPDATA%/AstroSmbTool/skymap/`;CC BY 4.0,
  UI 常显 `SURVEY_CREDIT` 署名。
- **重投影**: 输出极坐标像素→alt/az→ha/dec→ra/dec→银道 l,b→底图采样,numpy
  向量化(与 astro.py 标量公式一致,tests 的 TestSkymapReproject 用合成亮块做
  全链路钉死);760px 约 0.36s,按(站点 0.1°,时刻 5 分钟,尺寸)缓存 PNG。
- **下载的证书坑**: uv 独立构建 Python 在 Windows 不挂系统证书库且 OpenSSL 不做
  AIA 补链 —— `ssl.enum_certificates` 装载 91 张根证书仍可能缺链;实测兜底链:
  urllib(补根)失败 → **curl.exe**(Schannel,Win10+ 自带)。
- skyview.py 的 MiniRadar(浏览页详情)与 records 大图共用同一投影约定
  (radar_xy: 北上东左, r=R·(90-alt)/90),改投影必须三处同步。

### 7.12 天文数据链路(拍摄记录/导星分析,2026-07 新增)

- **数据流**: 页面工作线程 `clone()` → `shell.logstore.refresh(clone)`(下载→磁盘缓存→解析→
  聚合,**内部 _refresh_lock 全程串行** —— records/guiding 两页 on_connected 会并发触发,
  不串行会同时下载同一日志、os.replace 同一 .part 撞 WinError 32,真机踩过)→ 纯数据
  `LogData` 经 `shell.ui` 编组回 UI。日志原文缓存 `%LOCALAPPDATA%/AstroSmbTool/logs/`
  (按文件名+大小判同,日志写盘后不再变化);站点配置在同目录 `site.json`。
- **页面间跳转**: `shell.open_guiding(t0, t1, label)`(导星页 `show_range` 定位高亮区间)、
  `shell.open_browser_path(share, path)`。watcher 发现新日志 → `logstore.data = None` +
  `records.on_new_logs`(前台立即重载,后台等下次 on_show)。**导星页不自动重载,需手动刷新**。
- **天球图**(_records.py): alt-az 极坐标仰视图,北上**东左**,r = R·(90-alt)/90;
  x = cx - r·sin(az), y = cy - r·cos(az)。经度用日志推算值,纬度用户输入(默认 30°N)。
  巡天底图开启时叠底层 Image(直径=2×地平线半径,与圈精确对齐),前景换亮色画刷+标签阴影。
  **整图(点+底图)必须同一时刻**(_sky_ts,标注在标题上;各点用各自拍摄时刻会与底图错位,
  真机踩过"M 8 不在银心");bias/dark-only 的 run 坐标是停机位,不上天球(_sky_relevant)。
- **天球放大遮罩层**(_records.py): ContentDialog 内容上限 ~548px 会硬裁大图,故用页面内遮罩
  (根 Grid 最后一个子元素);带**时刻滑杆**(整夜任意时刻)。流畅性铁律:框架 _sky_frame 画一次,
  点/标签持久化只 SetLeft/SetTop 原地移动(绝不 Children.Clear);底图打开时按 15 分钟桶
  整夜预热(单 worker,想要的桶优先向外扩散,磁盘缓存二次打开秒热),拖动只换缓存帧。
- **实时横幅**(_records.py on_watch): 日志事后落盘 ⇒ 目标列表永远是历史;
  "正在拍摄"由 shell._apply_watch 转发 watcher 状态到页顶横幅显示,行副行带
  明确状态词(已完成/已暂停/被截断)。
- 曲线绘制(_guiding.py): Polyline + PointCollection(投射已验证可用);>1200 帧分桶降采样,
  每桶取 |值| 最大帧保尖峰;量程 max(1", 1.2×P99.5)。二轮增强:时间窗(全段~5分钟)+位置
  滑杆,缩出(>2帧/像素)自动切 min/max 包络带+30帧滑动 RMS 主线;统计小图(散点/直方图/
  滚动RMS/脉冲)与汇总全部在工作线程随 _prepare 预计算,UI 线程只做 searchsorted 切片。

### 7.14 设备记录与顶部下拉(devices.py + _window.py,2026-07 四轮新增)

- **不再硬编码默认设备**:启动地址优先级 `ASTRO_SMB_HOST` 环境变量 > `devices.last_host()` > 空;
  一台都没记过时**不连接**,直接跳「扫描设备」页自动开扫(硬编码 IP 对新用户永远是错的)。
- `self.client` 始终是可用对象(transfers/preview 的 `client_factory` 捕获了它):无记录时用
  `PLACEHOLDER_HOST=192.0.2.1`(RFC 5737 文档网段)构造但**绝不 connect**。
- 连接成功后 `devices.remember(host, 名字, OS, dialect, 共享数)` 并重建下拉;「忘记」按钮移除记录。
- 下拉项**必须是纯字符串**(见 §7.1 可编辑 ComboBox 的坑),形如
  `192.0.2.228  ·  ASIAIR · SMB 3.1.1 · 3 共享  ·  ● 端口可达 5 ms`;取回真地址走
  `_dev_hosts[SelectedIndex]`。
- 存活探测:独立守护线程每 20s 对已记录设备(≤12 台)`ping_tcp()`;**措辞只能是"端口可达"**
  (路由器会对整网段 445 假 ACK,§2),当前连接的那台才用心跳 RTT 显示"在线"。

### 7.13 运行状态 watcher(watcher.py)

- **判据来自真机实测**(勿"优化"回读日志): Autorun 日志运行中不可见(会话结束才写盘),
  "正在拍摄"唯一可靠心跳 = `Plan/Light/<目标>/` 与 `Autorun/{Bias,Dark,Flat}/` 的**最新帧
  mtime**,阈值 = 曝光时长 + 10 分钟容差(换目标+自动对焦实测间隙 6~7 分钟)。
- 独立守护线程 + 自建 client(超时 8s),30s 一轮 3~4 次 listdir;先看目录 mtime 挑最活跃目录,
  再进目录取最新帧解析文件名。`poke()` 连接成功后立即触发一轮。
- 新日志侦测: log 目录 Autorun_Log 文件名集合做差(首轮只建基线不报),上报 `new_logs` →
  shell 弹 info + 失效 logstore 缓存。换 host 时基线重置。

### 7.11 截图 GUI 的方式(无浏览器,原生窗口)

> **写探针脚本必须自带超时自关**(照 `ASTRO_SMB_GUI_AUTOCLOSE` 的做法:起一个
> `asyncui.create_task` 睡 N 秒后 `win.Close()`,并在最外层加保险)。真机踩过:
> 一个没有自关的 3D 天球探针,反复跑验证时每 3 分钟泄漏一个进程,
> 每个约 300MB **还拖着一整棵 WebView2 子进程树**,几轮就吃掉几个 GB。
> 同理:PrintWindow 截图脚本要 DPI-aware(见下),窗口标题要带 `ASTRO_SMB_GUI_TITLE_TAG`
> 以免抓到你自己开着的那个实例。

用 PowerShell + `PrintWindow(hwnd, hdc, 3)`(PW_RENDERFULLCONTENT)截被遮挡的 WinUI3 窗口。注意
**PowerShell 默认非 DPI-aware**,`GetWindowRect` 是虚拟化坐标(125% 缩放下 1460 物理 → 1168)。
按进程 `MainWindowTitle -like "*ASIAIR*"` 找窗口句柄。

---

## 8. 线程模型速查(改 GUI 前对照)

| 场景 | 正确做法 |
|------|----------|
| 事件处理器里做 SMB 浏览 | `async def`,`await asyncio.to_thread(self.shell.client.xxx, ...)` |
| 工作线程里更新 UI | `self.shell.ui(self._apply, arg)`(内部 TryEnqueue),**绝不直接碰 XAML** |
| 传输/扫描/预览 | 各起 `threading.Thread`,**各持 `client.clone()`**,结果 `shell.ui(...)` 回编组 |
| 分块并发 | `ParallelDownloader`,每 worker 各持 client,不共享 |
| await WinRT 异步 | 直接 `await op`(UI 线程);非 UI 线程用 `_common._spin(op)` |
| 关窗清理 | cancel 各线程 + 守护线程关 client(别在 UI 线程阻塞在锁上) |

---

## 9. 测试

- **离线单测**(不连设备):`uv run pytest tests/ -q`。
  **默认并行**(`pyproject.toml` 里 `addopts = "-n auto"`)。串行不只是没用上多核 ——
  Qt 那批测试全在一个 QApplication 里建控件,控件越堆越多、后面越跑越慢,
  **总 CPU 也比并行贵 4~5 倍**(实测同一份代码:串行二十多分钟没跑完,并行半分钟跑完)。
  要串行复现顺序依赖:加 `-p no:xdist`。**这里不写具体数字** ——
  写死的数字必然漂,而漂了的数字比没有数字更糟(读的人会以为它是准的)。
  `tests/test_docs_are_honest.py` 盯着这一点。
  缺 `win32more` 时 `tests/conftest.py` 会跳过 GUI 模块而不是中断整轮。覆盖路径解析、FITS 头、
  util、卷容量数据类、扩展名排序、网段识别、传输冲突策略、并行下载 .part 语义,以及
  Autorun/PHD2 日志解析、astro 数学、影像文件名解析(`.tmp/` 有真机样例日志时追加对账测试)。
- **真机测试**(连当前 ASIAIR 地址,`-H` 或 `ASTRO_SMB_HOST`):CLI 直接跑(`info`/`ls`/`get --jobs`/`put`/`df`/`du`);GUI 用
  上面的 env 钩子 + PrintWindow 截图验证。
- **重要经验**:改核心库(尤其 client.py 的 import 块/openFile 参数)后,**务必真机回归上传+下载
  哈希对比**。曾有一次重写 import 加卷容量常量时**漏掉 `FILE_WRITE_DATA` 导致所有上传崩溃**,是
  逐行复核时兜住的——**动 import 块后立刻 `uv run python -c "import astro_smb.client"` 并测一次
  `put`**。

---

## 9b. 变异测试(改判读逻辑前跑一遍)

```bash
uv run python scripts/mutation_check.py
```

**"测试全绿"不等于"这条判断被测过"。** 把变异集扩到 19 条(覆盖天文/日志/
导星/传输/协议/打包)之后,首轮一共活了 7 条:

- 高度角 / 气量 / 采样的三个判读阈值(本文反复强调"值钱在判读"的那几处)
- 夜次配色的排序(去掉 sorted 之后色号跟集合迭代序走)
- **天球投影的东西方向**(整张图镜像之后看起来完全正常)
- 包络视图阈值(抬上去导星曲线又变成一团噪声)
- 分块并发阈值(抬上去等于关掉 +57% 的提速,不报错只是每次慢一半)

后两条尤其值得记:它们改坏了**不报错、不崩溃**,只是悄悄退化。

第二批又扩到 26 条(覆盖 #33 导星逆推、watcher 判据、并行/预览的原子落盘、
treemap 配色、文件名解析),再捞出两条:

- **watcher 的 `IDLE_GRACE_S`**:10 分钟压到 6 秒,每次换目标/自动对焦都会
  误报停机(真机实测那些间隙是 6~7 分钟)。
- **文件名解析的锚定**(见下)。

**别把等价变异当成覆盖空洞。** 文件名那条踩过:先把 `.match` 换成 `.search`,
活了;再把正则的 `^` 去掉,还活着。查下去发现锚定是**双重**的 —— 两处各自都
足够,只动一处行为完全不变,那不是空洞是坏变异。同时动两处才是真缺陷,而那条
一放上去就被抓。把等价变异当空洞去补,只会写出一堆假装在测的测试。

26/26 全被抓。

同样的病还有一种更隐蔽的形态:**断言的东西不是它声称在测的东西**。
本轮栽了三次,每次都是"整份文件里出现过这个字符串"这类过松的包含检查
(把被测的那一处删掉,字符串在别处还在,测试照样绿)。写包含类断言时
一定要限定到具体的那一段。

第三批(2026-08)扩到 **33 条**,新增的七条全是**界面的静默失败**:
网格重建时行列翻倍、页面根的星号落错行、下载任务大小传 0、真实启动路径
不建传输管理器、本地/远程判据去掉分隔符要求、treemap 标签不带 `maxw`、
画布尺寸不量化。它们比阈值那几条更难发现 —— 阈值错了数字至少看着别扭,
而这些只是"点了没反应""看不见""挤在一起"。33/33 全被抓。

**2026-08-03 删掉 Uno 之后减到 23 条** —— 那 10 条打在
`astro_smb_app/ui/`、`astro_smb_app/proto/` 上,文件都没了。
留着它们只会在自检里报"命中 0 次",那是**空转的门禁,比没有更糟**。
被删掉的那几条守的性质(补丁按 id 定位、词表校验、页面根的星号落行……)
本来就是 Uno 那条路线特有的,随它一起走是对的。

顺带给脚本加了一条**自检**:每条变异的原文必须**恰好命中一次**。
命中 0 次说明代码改过、这条已经空转;命中多次说明它打在"第一处",
而那一处是哪一处没人保证 —— 改动一挪位置,测的就是别的东西了,
**且没有任何提示**。实测就有一条(词表校验)因为同一文件里有两处
`if spec is None:` 而变得不唯一,已经带上前一行消歧。

## 10. 开发工作流建议

- **加一个新 GUI 页**:写 `xxx.xaml` → 写 `_xxx.py`(`XamlReader.Load` 建 root、`_find`/`_wire`、
  实现 `on_show`/`on_connected`)→ 在 `main.xaml` 的 `NavigationView.MenuItems` 加 `NavigationViewItem`
  (带唯一 `Tag`)→ 在 `_window.py` 的 `_pages`、`nav_items` 注册。
- **加 SMB 能力**:优先加到 `AstroSmbClient`(核心库),CLI 和 GUI 复用;注意用 `_run` 或手动把
  impacket 异常转 `SmbClientError`。
- **验证 win32more API 是否存在**:直接 grep 投射源
  `\.venv\Lib\site-packages\win32more\Microsoft\UI\Xaml\...`,**不要猜**(命名空间和 .NET 文档常有
  出入,例如 ToggleButton/FontWeights 的位置)。
- **拿不准某个 WinRT 调用**:写个最小 probe 脚本在真 XAML app 里跑(参考对话里 `probe_*.py` 的做法),
  用 `ASTRO_SMB_GUI_AUTOCLOSE` 自动退出。
- **改动后**:`uv run python -c "import ast; ..."` 或直接 `import astro_smb_gui._window`(需先
  `appsdk.initialize()`)做 import 冒烟,再跑 pytest,再真机/截图。

---

## 11. 代码风格约定

- 注释、用户可见文本、错误信息一律**中文**;代码标识符英文。
- 错误处理:核心库对外只抛 `SmbClientError`;GUI 工作线程的异常要落到 `shell.error(...)`,别静默
  (win32more 里 async 处理器的异常会被吞)。
- 防御式:`TryEnqueue`/`stat` 等可能失败的地方加保护;破坏性操作(删除)先 `shell.confirm`。
- 不引入重型依赖(astropy 等);FITS/treemap 都是自己手写的轻量实现。

### 11a. i18n:**判读不许经过显示文本**(2026-08 起,新写代码必读)

用户可见文本走 `gettext`,**msgid 就是中文原文**(所以中文不需要任何 `.mo`,
行为与做 i18n 之前一个字不差)。详见 `docs/architecture/i18n.md`。

```python
from astro_smb.i18n import N_, gettext as _

_KIND = {"Light": N_("亮场"), "Bias": N_("偏置")}   # 表里只标记
...
label = _(_KIND.get(kind, kind))                    # 取用时才翻
```

三条规矩,**违反了不报错,只是行为悄悄不对**:

1. **模块级常量用 `N_()` 不用 `_()`。** 模块只 import 一次,`_()` 会把翻译
   冻在那一刻的语言上,之后 `set_language()` 再也改不动。
2. **判读/查表/取色一律用稳定身份,不用显示文本。** 判读函数返回语义键
   (`alt_verdict()` → `ALT_LOW`),显示是另一支(`_alt_hint()`);查表用
   `ext_category_id()`,显示用 `ext_category()`;状态比 `transfers.DONE_S`
   这类常量,**不比 `"完成"` 字面量**。
3. **测试断言键与结构,不断言中文。** 断言绑在中文串上,改一句文案红一片,
   于是没人敢改文案 —— 这个仓库为此付过代价。

这不是洁癖:光是做 i18n 的头一轮就炸出八处真 bug(「排队」分区永远是空的、
丢星那段不标警告、天球上的点悄悄退回 goto 请求值……),全部**不报错**。
`docs/architecture/i18n.md` §0.3 有完整清单。

工具在 `scripts/`:`i18n_survey`(还剩多少)、`i18n_wrap`(机械包,**会拒绝
包上面第 1、2 类的位置并列出来要人看**)、`i18n_extract`(生成 `.pot`)、
`i18n_build`(`.po`→`.mo`)、`i18n_pseudo`(伪语言 `xx_PS`)。

**验的办法是跑伪语言,不是读源码**:

```bash
uv run python scripts/i18n_pseudo.py
ASTRO_SMB_LANG=xx_PS uv run --with pyside6 python -m astro_smb_qt
```

屏幕上没被 `⟦⟧` 包住的字 = 漏包了;**行为变了 = 又有一处拿显示文本当身份**。

---

## 12. 已知限制 / 陷阱

- **拖出真实手势**只能实机验证;数据面已验证。大文件拖出复制期间 UI 会短暂冻结(模态)。目录不支持
  虚拟拖出。→ 兜底:剪贴板复制 / 下载。
- **分块并发**对设备侧收益有上限(约 4~8 连接饱和);文件间×文件内并发相乘会开较多连接,注意别把
  并发都拉满。
- **扫描**依赖 SMB 协商(1.5s 超时/主机),全网段约 6 秒;别改回"只看 TCP"。
- impacket 连接**非线程安全**——任何跨线程都要 `clone()`。
- win32more 是延迟绑定:import 不触发 DLL 加载,但 `appsdk.initialize()` 必须先行。
- 窗口 `AppWindow.Resize` 用物理像素;截图看到的尺寸受 DPI 缩放影响,不代表逻辑尺寸有误。
- ~~CLI 的 `DEFAULT_HOST` 硬编码~~ **已解(B20)**:设备记录下沉到
  `astro_smb/devices.py`(纯 json/pathlib,没有反向依赖),CLI 现在与 GUI 同一条
  规则 —— `ASTRO_SMB_HOST` > `last_host()` > **不猜**(给人话提示后退出 2)。
  `astro_smb_app/devices.py` 留 `sys.modules` 别名 shim,调用点一个字节没改。
- **盲解算(全天网格)对窄视场不实用**:0.65°x0.43° 视场铺满全天是 137672 格
  (约 2.7 小时),默认 400 格预算只覆盖 0.3%。失败消息会**写出覆盖比例**,
  别把"没搜到"读成"解不出来"。真正的全天盲解要靠四星几何哈希索引,本项目刻意
  没走那条路(正常流程总有指向先验)。真机盲解**至今未验证**。
- **FITS 头里的 RA/DEC 是赤道仪编码器读数**,不是计划目标也不是实际指向:
  实测与板解算中心恒差 21′(指向模型误差没同步回去),而板解算中心与目录位置
  只差 0.2′~0.4′。`SolveResult.hint_offset_deg` 那 21′ 不是故障指标。
  凡需"实际指向"的分析一律用板解算中心。
- **场旋的符号不能直接和正演比**:`TanWcs.rotation_deg` 是图像 +y 的天球位置角,
  而 **ASIAIR light 帧恒为镜像**(`TanWcs.flipped`,det>0),镜像把旋向整个翻过来。
  `guidecheck.cross_validate` 的两链对质因此**只比量级不比符号** —— 比符号会
  凭空造出假分歧(真机上差点这么报)。

---

## 13. 历史与验证状态(截至最后一次开发)

- 功能已实现并**真机验证**:枚举/浏览/搜索/上传下载(大文件、续传哈希一致、中文回环)、
  卷容量、占用分析、treemap、扫描、删除确认、子项计数、扩展名排序、勾选模式、
  **文件内分块并发(哈希一致、+57% 提速)**、**传输监控页(截图验证方块图)**、
  **OLE 虚拟文件拖出(数据面 sha256 一致)**。
- 经过**三轮逐行复核**(每轮多视角并行 + 逐条反证),已修复的关键问题包括:`_run` 重试路径
  泄漏原始异常、download_file 的 openFile 在 try 外、非幂等操作盲目重试、预览取消留半截缓存、
  关窗 UI 线程阻塞、同名文件并发写坏、扫描 TCP 误报、`FILE_WRITE_DATA` 漏导入(上传全崩)、
  空间页并发扫描竞态、子项计数线程不取消等。
- **2026-07-26 天文功能**已实现并真机验证:Autorun/PHD2 日志解析(样例 0 未解析行,导星 RMS 与
  PHDLogView 口径一致)、拍摄记录页(夜次/目标/事件时间线/天球图截图验证)、导星分析页
  (RA/DEC 曲线/RMS/校准行截图验证)、运行状态 watcher(实拍中"正在拍摄 NGC 7293 第N张"实时
  命中)、浏览页副行懒加载 + 详情天文卡片(截图验证);修复 records/guiding 并发 refresh 撞
  .part 的 WinError 32(logstore._refresh_lock)。并行下载空洞文件问题同期修复
  (.part 原子落盘 + 重试不再退化 resume)。
- 单测全绿(数量见 `uv run pytest tests/ -q`)。

> 记住:这个项目的价值一半在功能,一半在**趟过的坑**(§7)。改动前先确认你没有在重新踩已经解决的雷。
