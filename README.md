# MockRoute —— PC 脚本驱动的 Android 行进模拟

用 PC 上的脚本控制一台**未 root 的 Android 手机**的定位，并让它沿真实路网「走」或「开」出一条看起来自然的轨迹。

```
PC（规划路径 + 生成轨迹 + 下发指令）
      │  adb
      ▼
手机上的 MockRoute（一次性授权，之后长期可用）
      │  LocationManager 的 mock provider
      ▼
目标 App 读到的定位 = 你要的轨迹
```

## 图形控制台（不想敲命令就用它）

```powershell
python tools\gui.py
```

或者直接**双击仓库根目录的 `启动控制台.cmd`**。

五个标签页覆盖全部功能，每次操作都在日志区显示实际执行的命令（顺便学命令行用法）：

| 标签页 | 能做什么 |
| --- | --- |
| **快速开始** | 一键体检 / 生成跑步轨迹 / 回放（1× 或 20×）/ 停止 / 打开图表；固定坐标、原地绕圈 |
| **多点选路** | 地图上依次点出起点/途经点/终点；**检索地名直接定位**；改顺序、单删、存为命名地点；生成后**可在本页直接回放**（含倍速/暂停/停止） |
| **生成轨迹** | 表单填起终点、预设、路网、速度、噪声…；命名地点下拉；地图选点；可先「预览命令」再执行 |
| **轨迹文件** | 列出 `out/` 下所有轨迹（点数/大小/时间），可画图、打开图片、看摘要、直接回放 |
| **设备控制** | 检测设备、appops 授权、安装 APK、状态/暂停/恢复/停止/倍速、实时日志、重编译 APK |

> 共 5 个标签页。地图选点/选路都带**地名检索**：输入「天安门」直接跳到该处，
> 单击预览、双击确认。检索默认用 Photon（免费、无需 key），配了 `AMAP_KEY`
> 会自动优先用高德（国内 POI 覆盖更好）。

只依赖标准库（tkinter 是 Python 自带的），不需要装任何东西。长任务在后台线程跑，
可随时「中止当前任务」；每次执行的完整输出都留存在 `.toolchain\gui-logs\`。

## 检索地点

```powershell
# 列出候选，挑一个
python tools\mroute.py search "天安门" --limit 5
python tools\mroute.py search "西湖 杭州"           # 加城市限定更准
python tools\mroute.py search "公园" --near 39.9087,116.3975   # 偏向该坐标附近

# 直接取第 3 个并存成命名地点
python tools\mroute.py search "天坛" --pick 3 --save 天坛公园
```

检索来源依次尝试：**高德**（有 `AMAP_KEY` 时，国内 POI 最全）→ **Photon**
（免费、无需 key，实测国内环境可达）→ Nominatim（国内常超时）。

> 实测：`search "天坛"` 第一条会返回**广西南宁**的同名村，第三条才是北京的
> **天坛公园**。所以要么加城市限定（`天坛 北京`），要么用 `--pick` 挑。

## 环境准备（只需一次）

```powershell
# ① 装工具链：JDK 17 + Android SDK + Gradle（约 400MB，下载走 Python/OpenSSL）
powershell -ExecutionPolicy Bypass -File scripts/setup-android.ps1 `
    -SdkRoot .toolchain\android-sdk

# ② 构建 + 安装 + 授予模拟位置权限 + 启动（一条命令走完）
powershell -ExecutionPolicy Bypass -File scripts/build-android.ps1 -Install -Launch
```

之后所有 mroute 命令都用包装脚本（它负责配好 adb/JDK/Gradle 需要的环境变量）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/mroute.ps1 doctor
powershell -ExecutionPolicy Bypass -File scripts/mroute.ps1 play --route out\demo.json
```

> **路径限制**：Windows 上 AGP/Gradle **无法**在含非 ASCII 字符的路径下构建
> （比如路径里有 `—` em dash、中文、空格以外的特殊符号；目录联接也没用，
> JVM 会解析回真实路径）。如果你把仓库 clone 到这种路径，下面这套会自动绕行：
> `scripts/prep-build.ps1` 会把工程镜像到工作区内的纯 ASCII 目录 `build-ws/`
> 再构建，APK 产物拷到 `dist/MockRoute-debug.apk`。`build-android.ps1` 自动完成，
> 无需手动干预。**别去动 `build-ws/gradle.properties`** —— 里面的
> `android.overridePathCheck=true` 是这个绕行的关键。
>
> `-ExecutionPolicy Bypass` 是因为 Windows 默认禁止运行 `.ps1`，它只对这一次调用
> 生效。脚本刻意写成纯 ASCII，避免 Windows PowerShell 5.1 按 ANSI 读文件把中文
> 注释读成乱码。下载用 `tools/fetch.py`（Python + OpenSSL）而非
> `Invoke-WebRequest`，以绕开部分机器上损坏的 schannel —— 详见
> [docs/04-排障.md](docs/04-排障.md) 的 E5b 与 G 节。

完整步骤见 [docs/01-环境准备.md](docs/01-环境准备.md)。

## 30 秒上手

```powershell
# 1) 环境体检（会告出所有「为什么不动」的原因）
python tools/mroute.py doctor --fix

# 2) 生成一条轨迹：用 OSRM 免费路网规划，按 1.4 m/s（步行）采样
python tools/mroute.py gen --from 39.9075,116.3910 --to 39.9102,116.3950 `
    --provider osrm --speed 1.4 --out out/demo.json

# 3) 推到手机开始走
python tools/mroute.py play --route out/demo.json
```

**步行 / 跑步模拟**（推荐用预设，一次设好采样率、加速度、GPS 噪声、配速波动）：

```powershell
# 步行
python tools/mroute.py gen --preset walk `
    --from 39.9087,116.3975 --to 39.8822,116.4066 `
    --provider osrm --profile foot --out out/walk.json

# 跑步（带配速波动 + 合成地形 + 坡度影响配速）
python tools/mroute.py gen --preset run `
    --from 39.9087,116.3975 --to 39.8822,116.4066 `
    --provider osrm --profile foot --out out/run5k.json

python tools/plot-route.py out/run5k.json -o out/run5k.png   # 看图：路线 + 海拔 + 配速
powershell -ExecutionPolicy Bypass -File scripts/mroute.ps1 play --route out/run5k.json
```

三个预设：`walk`（步行）/ `run`（跑步，约 5'03"/km）/ `interval`（间歇训练，配速大幅波动）。
详见 **[docs/06-步行模拟.md](docs/06-步行模拟.md)** 与 **[docs/07-跑步模拟.md](docs/07-跑步模拟.md)**。

不想联网也能跑：

```powershell
python tools/mroute.py gen --provider file --file examples/road-to-tiantan.txt `
    --speed 1.4 --out out/demo.json
python tools/mroute.py play --route out/demo.json
```

## 起点终点怎么写（四种方式，可混用）

```powershell
# 1) 经纬度
--from 39.9087,116.3975 --to 39.8822,116.4066

# 2) 命名地点（存一次，之后写名字就行）—— 完全离线，最省事
python tools/mroute.py alias add "家" 39.9087,116.3975
--from 家 --to 公司

# 3) 地名解析（国内建议配高德 key）
$env:AMAP_KEY = "你的key"
--from 天安门 --to 天坛

# 4) 本地文件（GeoJSON / GPX / 纯文本）
--provider file --file my-route.gpx
```

```powershell
python tools/mroute.py alias list                 # 看已存的命名地点
python tools/mroute.py resolve "杭州西湖"          # 只解析坐标，不生成
python tools/mroute.py current                    # 读设备当前位置
```

### 保存选点路径（存一次，反复用）

把「起点 + 途经点 + 终点」整组存下来，以后一条命令复用：

```powershell
# 保存（坐标、命名地点、地名可以混用）
python tools/mroute.py path add "上班" --from 家 --via 天坛 --to 公司 --preset run

python tools/mroute.py path list                  # 看已存的路径
python tools/mroute.py path show "上班"            # 看它展开成什么命令

# 直接用保存的路径生成轨迹
python tools/mroute.py path use "上班" --out out/commute.json
```

控制台「多点选路」页也有：**保存当前选点 / 载入 / 删除**。
保存的路径与命名地点共用 `locations.json`（按 `kind` 区分），**旧文件无需迁移**。

> **首次使用需要先建 `locations.json`。** 仓库里只带 `locations.example.json`
> （几个公开地标作示例），因为真实文件会记录你常去的位置。复制一份即可开始：
>
> ```powershell
> copy locations.example.json locations.json
> ```
>
> `locations.json` 已在 `.gitignore` 里，不会被误提交。

图形控制台里还有**地图选点**：鼠标在真实地图上点一下就是起点/终点（单击定位、
双击确认、拖动平移、滚轮缩放）。实测这台机器上 OSM 瓦片不可达，所以用的是高德瓦片
（无需 key）。

> 「读设备位置」在部分机型上读不到明文坐标 —— Android 10+ 会对 `dumpsys location`
> 做隐私打码（显示成 `39.9******`）。本机荣耀 9X 即是如此，这时用地图选点或命名地点。

详见 [docs/02-快速开始.md](docs/02-快速开始.md)。

## 它能做什么

| 能力 | 命令 | 说明 |
| --- | --- | --- |
| 单点定位 | `point --at 39.9,116.4` | 固定在某个坐标，会按 1 Hz 持续重发 |
| 沿路网行进 | `gen` + `play` | 真实道路折线 + 速度剖面 + 噪声 |
| 原地转圈 | `circle --at ... --radius 100 --period 60` | 持续移动，用于防「静止判定」 |
| 直线前往 | `goto --at 39.9,116.4 --speed 1.4` | 从当前位置平滑走过去 |
| 循环/往返 | `play --mode loop\|pingpong` | 长跑场景 |
| 运行时调速 | `mult 2.5` | 2.5 倍速回放，轨迹内容不变 |
| 暂停/恢复/停止 | `pause` / `resume` / `stop` | `stop` 会移除 mock provider |
| 看状态 | `status` / `logs` | `logs` 实时打印手机端日志 |

## 目录结构

```
mockroute-android/          Android 侧：Mock 定位服务 + 广播控制入口（Kotlin）
tools/mroute.py             PC 侧 CLI 入口（所有命令都从这里进）
tools/gui.py                图形控制台（按钮操作，tkinter 自带）
tools/plot-route.py         轨迹可视化（路线 + 海拔 + 配速剖面）
tools/fetch.py              下载器（Python + OpenSSL，绕开损坏的 schannel，支持续传）
tools/sdk-direct.py         直接下载安装 Android SDK 组件（绕开会卡死的 sdkmanager）
tools/probe-sdk-zips.py     探查官方 SDK 压缩包的内部目录结构（开发用）
tools/mockroute/
    geo.py                  大地测量 + WGS-84 / GCJ-02 / BD-09 转换
    route.py                路网规划（OSRM / 高德 / Google / 本地文件）
    traj.py                 轨迹生成（速度剖面、GPS 噪声、配速波动、地形）
    protocol.py             广播协议常量 + adb 封装（两边共用的契约）
    player.py               播放控制（设备回放 / 直推两种模式）
scripts/setup-android.ps1   装 JDK + Android SDK + Gradle
scripts/prep-build.ps1      把工程镜像到 ASCII 目录（Windows 非 ASCII 路径必需）
scripts/build-android.ps1   构建 + 可选安装/授权/启动
scripts/mroute.ps1          包装 mroute CLI，配好 adb/JDK/Gradle 环境变量
启动控制台.cmd              双击打开图形控制台
tests/                      81 项自检，不连手机不联网
docs/                       详细文档（含步行/跑步模拟专章）
examples/                   示例路径（纯文本 / GeoJSON / GPX）
build-ws/                   自动生成的 ASCII 构建目录（可安全删除）
dist/MockRoute-debug.apk    构建产物
.toolchain/                 JDK / SDK / Gradle / 缓存 / GUI 日志（约 840MB）
```

## 两条关键设计

**1. 定位由手机端自己回放，而不是 PC 逐点推送。**
`play` 默认把轨迹文件推到手机，手机按时间戳自己插值写点。PC 只发 1 条广播，因此：
- 时间精度由手机保证（毫秒级），不受 adb 往返延迟影响；
- 长时间、高频率、上千个点都不掉链子。

只有在要「在 PC 上临时改坐标做交互调试」时才用 `--direct` 逐点广播（上限约 2~5 Hz）。

**2. 轨迹里的 `speed`/`bearing` 一定和自己算出来的位移自洽。**
坐标按 `s = v₀t + ½at²` 解析积分，末端用 `v ≤ √(2·a·s)` 的刹车剖面；
`tests/test_mockroute.py::test_timeline_matches_distance` 专门守这条不变量。
这是目标 App 判断「定位是否被伪造」最常用的方法。

## 适用范围与边界

- 未 root 设备的定位只能由设备上的「模拟位置提供程序」改写，**没有 adb 直接写坐标的接口**。`adb emu geo fix` 只对 emulator 控制台有效，真机不认。
- 纯 mock provider 会留下 `Location.isMock()` / `isFromMockProvider()` 为 `true` 的痕迹，主流风控还会交叉检查 Wi-Fi/基站/陀螺仪。**这套工具适合自研测试、自动化回归、演示、竞品对比**；用来伪造考勤、刷单、绕过地域限制属于违规甚至违法，后果自负。

详见 [docs/05-合规与风险.md](docs/05-合规与风险.md)。

## 文档

| 文档 | 内容 |
| --- | --- |
| [docs/06-步行模拟.md](docs/06-步行模拟.md) | **步行模拟完整指南**：`--preset walk`、GPS 噪声原理与调参、平滑技巧 |
| [docs/07-跑步模拟.md](docs/07-跑步模拟.md) | **跑步模拟完整指南**：配速波动、合成地形与坡度效应、间歇训练 |
| [docs/01-环境准备.md](docs/01-环境准备.md) | JDK / Android SDK / 构建 APK / 手机设置 |
| [docs/02-快速开始.md](docs/02-快速开始.md) | 从零到手机开始移动的完整流程 |
| [docs/03-广播协议.md](docs/03-广播协议.md) | action / extra 全表，便于自己写客户端 |
| [docs/04-排障.md](docs/04-排障.md) | 症状 → 原因 → 处置 |
| [docs/05-合规与风险.md](docs/05-合规与风险.md) | 检测手段、法律边界 |

## 自检

```powershell
python -m unittest discover -s tests -t . -v
```

覆盖大地测量与坐标系转换、轨迹生成的全部不变量、路径解析、广播参数渲染，以及
「PC 侧常量是否与 Kotlin 源码一致」这类容易悄悄坏掉的契约。

## 外部服务与使用限制

这个工具会用到几个**公开的免费服务**。它们大多有明确的用量约束，**请别用它跑批量任务**：

| 用途 | 默认端点 | 约束 |
| --- | --- | --- |
| 路网规划 | `router.project-osrm.org` | OSRM 官方把这台机器**仅供 demo 试用**。长期/批量请自建 OSRM 实例（`--osrm-base` 可换） |
| 地名检索 | Photon / Nominatim | Nominatim 要求**≤1 次/秒**且必须带可识别的 UA；批量会被封 |
| 地图瓦片 | 高德 / ArcGIS 公开瓦片 | 仅供个人调试；生产环境请申请正式 key 或自建瓦片服务 |

需要正式使用时，把它们换成你自己申请的服务，或自建实例。

## 参与开发

见 [CONTRIBUTING.md](CONTRIBUTING.md)。**特别注意**：本项目的 `.gitignore` 是
「白名单」式 —— 新增顶层目录时必须在 `.gitignore` 里加放行规则，否则文件不会
进仓库。提交前跑 `python tools\preflight.py` 自检。

## 许可

[MIT](LICENSE)。可自由使用、修改、商用、再分发，保留版权声明即可。

## 免责

本项目是**定位功能的测试工具**，用于自研 App 的回归测试、自动化演示、教学与研究。
它依赖 Android 的 mock provider，会留下 `Location.isMock()` / `isFromMockProvider()`
为 `true` 的痕迹，**不是隐蔽方案**。

用于伪造考勤、刷单、骗取补贴/学分、绕过平台地域限制等，属于违规甚至违法，
与本项目作者无关。使用前请确认：**你有权改动这台设备上该 App 的行为**。

详见 [docs/05-合规与风险.md](docs/05-合规与风险.md)。
