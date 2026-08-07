# 术语表 —— 翻译这个项目之前必读

> 这份表**同时是给人看的和给机器看的**:批量翻译时把它整份塞进提示词,
> 单条术语翻错的概率会显著下降。`scripts/i18n_check_po.py` 会按它抽查。

## 为什么需要它

这个项目的文案**一多半是天文判读结论**,不是界面装饰。翻错不是"读着别扭",
是**给出错误的结论**:

- `大气质量` 直译成 *air quality* → 变成空气污染指数;
- `西垂` 直译成 *west hanging* → 赤道仪在中天哪一侧这件事整个没了;
- `恰定` 漏掉 → 用户会以为那个极轴数字是可信的,而它恰恰推翻不了。

## 核心术语

| 中文 | 英文 | 说明(翻译时的判断依据) |
|---|---|---|
| 大气质量 / 气量 | **airmass** | 天体到观测者的大气路径长度,天顶为 1。**不是** air quality |
| 高度角 | altitude | 地平坐标的仰角。缩写 alt |
| 方位角 | azimuth | 地平坐标,**北 0° 东 90°**(本项目约定) |
| 赤经 / 赤纬 | right ascension (RA) / declination (Dec) | 直接用缩写即可 |
| 时角 | hour angle | |
| 中天 | meridian / transit | 「中天翻转」= meridian flip |
| 西垂 / 东垂 | **pier side west / east** | 赤道仪镜筒在中天哪一侧。**不是**"往西挂" |
| 极轴 | polar axis | 「极轴误差」= polar alignment error |
| 恰定 | **exactly determined** | 方程数 = 未知数,残差恒为零 —— **模型错了也看不出来**。这个词丢了,结论的可信度就丢了 |
| 可证伪 | falsifiable | 同上,成对出现 |
| 简并 | degenerate | 两个分量在观测上分不开 |
| 板解算 | **plate solving** | 天文摄影通用叫法,不是 "board solving" |
| 盲解 | blind solve | 没有指向先验的板解算 |
| 内点 | inliers | 拟合时留下来的匹配点 |
| 星点 | stars / detected sources | 图上提取出来的光源 |
| 视场 | field of view (FOV) | |
| 足迹 | footprint | 视场在天球上覆盖的四边形 |
| 场旋 | field rotation | |
| 位置角 | position angle (PA) | |
| 导星 | **guiding** | 「导星镜」= guide scope,「导星相机」= guide camera |
| 丢星 | star lost | 导星丢失目标星 |
| 抖动 | dither | 每张之间的小幅偏移,**不是** "shake" |
| 校准 | calibration | 导星校准 |
| 稳定 / 落定 | settle | PHD2 的 settling |
| 欠采样 / 过采样 | **undersampled / oversampled** | 像元比例 vs 视宁度 |
| 像元比例 | pixel scale | ″/px |
| 视宁度 | seeing | |
| 亮场 | **light frame** | 正片 |
| 暗场 | dark frame | |
| 偏置 | bias frame | |
| 平场 | flat frame | |
| 积分(时间) | integration (time) | 总曝光时长,不是数学积分 |
| 夜次 | night / session | 按正午分界归的一夜 |
| 拉伸 | stretch | 「自动拉伸」= auto-stretch / STF |
| 去马赛克 | demosaic | Bayer 阵列还原 |
| 缩略图 | thumbnail | |
| 共享(SMB) | share | 名词。SMB 共享目录 |
| 端口可达 | **port reachable** | ⚠ 与「在线」**必须区分**:路由器会对整个网段的 445 秒回 ACK,端口通不代表那是台 SMB 设备 |
| 在线 | online | 只用于真正拿到过 SMB ECHO 往返的那台 |
| 分块并发 | chunked / parallel download | 单文件切块多连接 |

## 硬规则

1. **占位符原样保留,不许翻也不许改名**:`{name}`、`{0}`、`{rms_px:.2f}`、
   `%d`、`&#10;`。改一个字就是运行时 `KeyError`/`IndexError`。
2. **格式说明符不许动**:`{x:.1f}` 的 `.1f`、`{n:,}` 的逗号。
3. **两端的空格要保留**:很多 msgid 是拼接用的片段(` · 平均 RMS {fv:.2f}`),
   前导空格丢了会粘在一起。
4. **不要"顺手补充"**:msgid 短是因为它是按钮或标签,译文也要短。
   一个 `刷新` 翻成 *Refresh the current view* 会把按钮撑破。
5. **`⟦⟧` 出现说明你拿到的是伪语言词表**,那是验收工具,不要翻。

## 复数

中文没有复数变化,所以源语言看不出问题。**目标语言有复数的,
译者要自己判断哪些条目需要 `msgid_plural`** —— 词表里
「数字 + 量词」形态的约有一百多条(`{n} 帧`、`{shares} 个`、`{0} 张`)。

注意有些条目**含两个计数**(`共 {0} 个文件, {1} 个目录`),gettext 的复数
机制只按一个数选形式 —— 那种情况下选一个**对所有 n 都通顺**的说法,
不要硬套复数。

## 校验

翻完跑:

```bash
uv run python scripts/i18n_check_po.py <语言>
```

它查占位符是否对齐、有没有空翻译、有没有留着 `#,fuzzy`、复数形式数量
对不对得上头里的 `nplurals`。**这几样错了都不报错,只是界面上悄悄不对。**
