# 参与开发 / 提交指南

这份文档说明**怎么改、怎么测、怎么提交**。第一次参与请通读第 4 节，那一节讲的是
本项目 `.gitignore` 的特殊之处 —— 不知道它会很困惑。

---

## 1. 环境

只需要 Python 3.9+ 就能跑所有命令行工具和图形控制台（只用标准库，
tkinter 是 Python 自带的）：

```powershell
python tools\gui.py                     # 图形控制台
python tools\mroute.py --help           # 命令行
```

要构建 Android APK 才需要 JDK 17 + Android SDK + Gradle（约 400MB）：

```powershell
powershell -ExecutionPolicy Bypass -File scripts/setup-android.ps1 -SdkRoot .toolchain\android-sdk
powershell -ExecutionPolicy Bypass -File scripts/build-android.ps1 -Install -Launch
```

## 2. 首次使用：建 locations.json

仓库里只有 `locations.example.json`（公开地标示例）。真实文件会记录你常去的位置，
所以不进版本库：

```powershell
copy locations.example.json locations.json
```

## 3. 跑测试

```powershell
python -m unittest discover -s tests -t .          # 全部
python -m unittest tests.test_mockroute.TestCli    # 只跑某一类
```

改完代码**务必跑一遍**。测试覆盖了容易悄悄坏掉的契约，比如「PC 侧常量是否与
Kotlin 源码一致」「速度与位移是否自洽」。

## 4. ⚠️ `.gitignore` 是「白名单」，不是黑名单

这是本项目最容易踩的坑，务必读完。

### 为什么这么设计

项目工作区里除了源码，还可能混着 Android SDK（GB 级）、项目副本、私人文件。
黑名单一旦漏写一行，私人文件就会被提交进 git 历史 —— 那是**永久**的，要
rewrite history 才清得掉。白名单的失败方式是「文件没进仓库」（无害），
而不是「私人文件被公开」（灾难）。

### 它怎么工作

```gitignore
/*                      # ① 先忽略一切
!/README.md             # ② 再逐个放行
!/docs/
!/tools/
...
```

### 所以你新增顶层目录时必须做一件事

**在 `.gitignore` 的第 ② 段加一行放行规则**，否则你的新文件不会进仓库，
而且 `git status` 里**看不到它**（会被当成"已忽略"）：

```gitignore
!/my-new-dir/
```

判断方法：

```powershell
git check-ignore -v my-new-dir    # 有输出 = 被忽略了，需要加放行
git status --short                # 没有你的文件 = 被忽略了
```

### 顺手可用的自检工具

```powershell
python tools\preflight.py
```

它会检查将要提交的文件里有没有：隐私信息（用户名/设备序列号/绝对路径）、
硬编码密钥、超大文件、误提交的构建产物。**提交前跑一次**。

## 5. 不要提交的东西

`.gitignore` 已经挡住了绝大部分，但改代码时请记得：

| 别提交 | 原因 |
| --- | --- |
| `locations.json` | 含你真实去过/常去的位置 |
| `out/` `dist/` | 运行产物，随时能重新生成 |
| `.toolchain/` `build-ws/` | SDK 与构建镜像，GB 级 |
| `*.apk` `*.keystore` `*.jks` | 二进制产物与签名密钥 |
| 任何绝对路径（本机用户目录、带盘符的项目路径等） | 别人 clone 下来会直接跑不起来 |

## 6. 提交前检查清单

```powershell
python tools\preflight.py      # ① 自检必须 OK
python -m unittest discover -s tests -t .   # ② 测试必须全绿

git status --short             # ③ 逐行看，确认没有意外文件
git diff                       # ④ 看具体改了什么
```

第 ③ 步**不要跳过**。仓库发布前的最后一道人工防线就是它。

## 7. 提交信息

一条提交做一件事，描述用祈使句、说清"改了什么"和"为什么"：

```
traj: 按弯的形状限速，避免急弯被压到 0.3 m/s

原来过弯速度由「弯道内采样点够不够」这个代理指标决定，实测半径 1m
的 90° 弯被压到 0.30 m/s（横向加速度仅 0.09，远低于人的舒适值）。
改为按曲率半径用 a = v²/r 限速，同样是 90° 弯：
半径 1m -> 0.62 m/s，半径 8m -> 3.28 m/s，半径 30m 不减速。
```

前缀可选：`fix:` `docs:` `test:` `traj:` `gui:` `android:`。

## 8. 提交信息里的实测数据

这个项目历史上很多 bug 是靠**实测数字**才发现和验证的（例如「上限从 4 压到 1
完全无效，因为切角是 2.00m」）。改行为相关的代码时，在提交信息和代码注释里
带上实测数据，能省掉后来人大量时间。

## 9. 许可

提交即表示你同意以 [MIT](LICENSE) 许可发布你的贡献。
