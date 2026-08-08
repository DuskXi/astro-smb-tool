# 在 macOS 上跑起来 —— 照着抄就行

从 GitHub 克隆下来,把 Qt 界面(`astro_smb_qt`)跑起来,再打一个免安装包。

**不需要 Xcode、不需要 .NET、不需要自己装 Python。**
Intel(x86_64)与 Apple Silicon(arm64)都适用,不同的地方会单独标出来。

> Windows 原生的那套界面(`astro-smb-tool-gui`,WinUI 3)在 Mac 上跑不了 ——
> 它依赖 `win32more`。那不是缺陷,是它本来就只在 Windows 服役;Mac 上跑的
> 就是这一份 Qt。

---

## 一、装 uv(只做一次)

打开「终端」,粘这一行:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

装完**关掉终端窗口再开一个**(让 PATH 生效),验一下:

```bash
uv --version
```

有版本号就行。Python 不用管 —— uv 会照 `.python-version` 自己拉 3.13。

---

## 二、克隆并进目录

```bash
git clone https://github.com/DuskXi/astro-smb-tool.git
cd astro-smb-tool
```

---

## 三、一条命令备好环境

```bash
./scripts/mac-setup.sh
```

它做三件事:查 uv、`uv sync --extra qt`(第一次要下 PySide6,约 200 MB,
几分钟)、跑一遍离线单测。

**预期是全绿**,两千多条、半分钟跑完(默认 `-n auto` 并行)。会有几十条
skip,那是正常的:`astro_smb_gui/` 在 Mac 上导不进来,`tests/conftest.py`
整块跳过而不是让整轮变红;另有几条板解算的用例要真机 FITS 样本。

不想用脚本就手敲:

```bash
uv sync --extra qt
uv run --extra qt pytest -q
```

> **`--extra qt` 是什么。** PySide6 **故意不在必装依赖里** —— 只用命令行的人
> 不该被拖去下两百多兆。代价是不加这个 extra 时图形入口跑不起来,
> 那时它会自己告诉你该敲什么,不会甩一个 `ModuleNotFoundError` 给你。

---

## 四、跑起来

```bash
./scripts/mac-run.sh
```

**不给设备地址时它会自动扫本网段找 ASIAIR** —— 不猜任何默认 IP。
判据是 SMB 协商成功,不是 445 端口开着(有的路由器会对整个网段秒回 ACK,
只看端口的话 254 个地址全都"在线")。

指定设备:

```bash
./scripts/mac-run.sh --host 192.0.2.227          # 局域网里的真设备
./scripts/mac-run.sh --host "/Volumes/ASIAIR"    # 存储卡直插电脑
```

**本地目录是正式支持的设备类型**,不是测试设施 —— 走的是同一条代码路径。
路径要给到含 `Autorun` / `log` 的那一层(通常叫 `EMMC Images`),给它的
上一级会让共享名和日志目录都落错地方,拍摄记录与导星两页读不到东西。

### 常用参数

```bash
./scripts/mac-run.sh --page records      # 直达某页:browse/records/guiding/sky/
                                         #   fits/space/devices/scan/transfers
./scripts/mac-run.sh --theme light       # 配色:light 白天 / normal 深色 / red 红光
./scripts/mac-run.sh --seconds 30 --shot /tmp/shot.png    # 自动关窗 + 截图
./scripts/mac-run.sh --help
```

### 没有设备也想看看界面

造一个空目录当设备,九页都能打开(大部分是空态,那是对的):

```bash
mkdir -p /tmp/dev/"EMMC Images"/{log,Autorun}
./scripts/mac-run.sh --host "/tmp/dev/EMMC Images"
```

要看真数据,把设备或存储卡上的 `EMMC Images` 整个拷过来,`--host` 指过去。
**日志才是那几页的数据来源**(拍摄记录 / 导星 / 3D 天球 / 夜次统计),
只拷 `log/` 也能看个七七八八。

### 板解算的星表

第一次点「板解算」如果没有星表,界面会告诉你要下多大、从哪儿取,给一个
「下载星表」按钮 —— 点了它直接从上游 CDS I/259 取原始数据在本地构建
(35.6 MB 成品),下完自动把刚才那次解算接着跑完。**不需要任何额外配置。**

已经有一份现成的就指过去:

```bash
export ASTRO_SMB_CATALOG_PATH="$HOME/tycho2_v1.bin"
```

---

## 五、打一个免安装包

```bash
uv run --extra qt python scripts/package.py --smoke
```

产物在 `dist/astro-smb-tool/`,目标机不用装 Python、不用装 Qt。
Intel 上打出来的是 **osx-x64**,Apple Silicon 上是 **osx-arm64** ——
**没有交叉编译这回事**:PyInstaller 把当前正在跑的那个解释器连同它的原生
扩展一起塞进包里。两个架构要两台机器(CI 里就是 `macos-13` + `macos-14`)。

`--smoke` 做两件事,缺一不可:

1. **`--selftest`** —— 翻译词表、3D 天球的静态资产、QtWebEngine 找不找得到。
   这三样**缺了都不影响启动**,只是界面永远中文、天球页空白 ——
   打包坏掉的典型症状恰好都不报错;
2. 真开一次窗口,几秒后自退。

单独再跑自检:

```bash
./dist/astro-smb-tool/astro-smb-tool --selftest
```

### 关于 `.app` 与 Gatekeeper

现在产出的是**一个文件夹加一个 Unix 可执行文件**,不是 `.app` ——
双击会开终端。命令行启动是正常用法:

```bash
./dist/astro-smb-tool/astro-smb-tool --host "/Volumes/ASIAIR"
```

**包没有签名也没有公证。** 但这不等于"下下来就一定被拦" ——
Gatekeeper 拦的是带 `com.apple.quarantine` 这条扩展属性的文件,而**那条
属性是下载它的那个程序打上去的**,不是系统自动加在所有文件上的。

于是分三种情况:

| 怎么拿到的 | 会怎样 |
|---|---|
| 本机自己打的 | 什么都不会发生 |
| `curl` / `wget` 下载,或终端里 `tar -xzf` 解开 | **也什么都不会发生** —— `tar` 不会把归档自己身上的隔离属性传给解出来的文件 |
| 浏览器下载后在访达里双击解压 | 会被拦。解压出来的东西继承了隔离属性 |

所以发布包用 `.tar.gz` 而不是 `.dmg`/`.zip` 是有意的:终端里解开就能直接跑。
万一你是在访达里解的:

```bash
xattr -dr com.apple.quarantine dist/astro-smb-tool
```

签名与公证要 Apple 开发者账号(99 美元/年),还没做 —— 权衡写在
[`docs/DEVELOPMENT.md`](docs/DEVELOPMENT.md) §14。

---

## 六、出问题时

| 症状 | 多半是 |
|---|---|
| `uv: command not found` | 第一步装完没重开终端 |
| 图形入口说"需要 PySide6" | 少了 `--extra qt`。照它给的命令敲一遍就行 |
| 窗口起不来、报 `qt.qpa.plugin` | 用的是 ssh / 无桌面会话;要在本机图形界面下跑 |
| 拍摄记录 / 导星是空的 | `--host` 给到含 `log/` 的那一层了吗?见第四节 |
| 3D 天球是黑的 | 显卡/驱动没给 WebGL。虚拟机与远程桌面里常见,实体 Mac 上不该发生 —— **遇到请报** |
| 界面是英文 / 中文变方块 | 界面按系统语言选,左下角可手动切。方块是缺字体,请截图报 |

`astro_smb_gui/`(Windows 那套)在 Mac 上导不进来是**预期的**,测试会自动
跳过,不会让整轮变红。

---

## 七、目录里有什么

```
astro_smb/          核心库(SMB / FITS / 天文 / 日志 / 板解算),不依赖任何界面
astro_smb_app/      共享应用层(设备记录、缓存、传输队列、视图模型)
astro_smb_qt/       Qt 界面 ← Mac 上跑的就是这一份
astro_smb_gui/      WinUI 3 界面(只在 Windows 上能跑),它是界面**原型**
packaging/          PyInstaller 规格
tests/              两千多条离线单测(默认并行,半分钟跑完)
docs/DEVELOPMENT.md 技术总览:分层、线程模型、真机上踩过的坑
docs/qt-final.md    逐页验收清单 = 「完工」的定义
```

---

## 八、手边有真设备的话,最值得帮忙验的

验收清单里有 5 条写的是「**画面已验 / 触发未验**」—— 界面拿到那种状态时
画得对(假数据喂出来截过图),但**真机上会不会触发**只有连上设备才算数:

- 设备正在曝光时的顶部横幅;
- 慢链路下的下载进度条;
- 在线设备的探测与连接;
- 网里真有 ASIAIR 时扫描结果的 ★ 置顶。

这四条在 Mac 上验一遍最有价值。
