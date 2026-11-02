"""广播协议常量与 adb 命令构造。

这个文件是 PC 侧与 Android 侧的唯一契约。
Android 端对应文件：mockroute-android/app/src/main/java/com/mockroute/Protocol.kt
改任何 action 名或 extra 名，两边必须同时改。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, List, Optional, Sequence

APP_ID = "com.mockroute"
RECEIVER = f"{APP_ID}/.ControlReceiver"

# --dry-run 时用的占位可执行名：此时不连设备，只是为了把命令渲染出来，
# 所以不需要机器上真的装了 adb。
DRY_RUN_ADB = "adb"

# 应用私有外部目录：PC 可读写，且不需要任何存储权限
DEVICE_BASE = f"/sdcard/Android/data/{APP_ID}/files"
DEVICE_ROUTE = f"{DEVICE_BASE}/route.json"

ACTION_SET = "com.mockroute.action.SET"
ACTION_START = "com.mockroute.action.START"
ACTION_START_ROUTE = "com.mockroute.action.START_ROUTE"
ACTION_START_CIRCLE = "com.mockroute.action.START_CIRCLE"
ACTION_PAUSE = "com.mockroute.action.PAUSE"
ACTION_RESUME = "com.mockroute.action.RESUME"
ACTION_STOP = "com.mockroute.action.STOP"
ACTION_STATUS = "com.mockroute.action.STATUS"
ACTION_GOTO = "com.mockroute.action.GOTO"
ACTION_SPEED = "com.mockroute.action.SPEED"

# extra 名 -> 广播类型后缀
#
# 重要：`am broadcast` 只支持 --ef（float）/--ei（int）/--el（long）/--es（string），
# 并没有 --ed（double）。所以这里一律用 --ef 发浮点值。
#
# 但 Android 的 TypedValue 把 FLOAT(4) 和 DOUBLE(5) 当成不同类型：
# 如果设备端用 Bundle.getDoubleExtra() 去读一个 float extra，会直接拿到默认值 ——
# 表现为设备端报「SET 缺少 lat/lon」，而 am broadcast 依然返回成功，极难排查。
# 因此 MockLocationService 里改用能同时接受 float/double/String 的容错读取函数
# （dbl()/flt()），两边配合才不会踩这个坑。
STRING_EXTRAS = {"file", "mode", "tag"}
INT_EXTRAS = {"interval"}
FLOAT_EXTRAS = {"lat", "lon", "acc", "alt", "speed", "bearing", "mult", "radius"}
LONG_EXTRAS = {"period"}

TAG = "MockRoute"

# ---- 与 Android 端 SharedPreferences 共用的键 ----
# 必须与 Protocol.kt 里的同名常量一致（有测试守着）。
PREF = "mockroute_ui"
KEY_STATUS = "status"
KEY_UPDATED = "updated"
KEY_STATE = "state"

# 开关状态取值。
# 界面靠这个显示「运行中/已暂停/已关闭」，**不要**去解析 KEY_STATUS 的文案：
# 那个文案只有 ACTION_STATUS 时才含 "playing=true"，单点/转圈/前往写的是
# 别的措辞，靠文案判断会让状态条永远显示「已关闭」。
STATE_OFF = "off"
STATE_ON = "on"
STATE_PAUSED = "paused"


class AdbError(RuntimeError):
    pass


def find_adb(explicit: Optional[str] = None) -> str:
    """找 adb：先看显式路径，再看 PATH，然后看常见安装位置。

    包括 scripts/setup-android.ps1 装在项目里的那份 .toolchain/android-sdk，
    这样装完工具链不用手工配 PATH 就能直接用。
    """
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return str(p)
        raise AdbError(f"指定的 adb 不存在: {explicit}")

    found = shutil.which("adb")
    if found:
        return found

    here = Path(__file__).resolve()
    candidates = [
        # 项目内工具链（scripts/setup-android.ps1 的默认落点）
        here.parents[2] / ".toolchain/android-sdk/platform-tools/adb.exe",
        here.parents[2] / ".toolchain/android-sdk/platform-tools/adb",
        # 常见全局位置
        Path.home() / "AppData/Local/Android/Sdk/platform-tools/adb.exe",
        Path("C:/Android/Sdk/platform-tools/adb.exe"),
        Path("/usr/local/bin/adb"),
        Path("/usr/bin/adb"),
        Path("/opt/homebrew/bin/adb"),
    ]
    for c in candidates:
        if c.is_file():
            return str(c)
    raise AdbError(
        "找不到 adb。三选一：\n"
        "  1) 跑 scripts/setup-android.ps1 把工具链装进项目（推荐）\n"
        "  2) 安装 Android SDK Platform-Tools 并加入 PATH\n"
        "  3) 用 --adb <完整路径> 显式指定\n"
        "详见 docs/01-environment-setup.md"
    )


@dataclass
class Adb:
    exe: str
    serial: Optional[str] = None
    timeout: float = 30.0

    def base(self) -> List[str]:
        cmd = [self.exe]
        if self.serial:
            cmd += ["-s", self.serial]
        return cmd

    def run(self, args: Sequence[str], timeout: Optional[float] = None,
            check: bool = True) -> subprocess.CompletedProcess:
        cmd = self.base() + list(args)
        try:
            cp = subprocess.run(
                cmd, capture_output=True, timeout=timeout or self.timeout,
                encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired as e:
            raise AdbError(f"adb 超时: {' '.join(cmd)}") from e
        if check and cp.returncode != 0:
            raise AdbError(
                f"adb 失败({cp.returncode}): {' '.join(cmd)}\n"
                f"stdout: {cp.stdout.strip()}\nstderr: {cp.stderr.strip()}"
            )
        return cp

    def shell(self, script: str, timeout: Optional[float] = None,
              check: bool = True) -> str:
        return self.run(["shell", script], timeout=timeout, check=check).stdout

    # ---- 高阶封装 ----

    def devices(self) -> List[str]:
        out = self.run(["devices"]).stdout
        result = []
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                result.append(parts[0])
        return result

    def require_device(self) -> str:
        devs = self.devices()
        if not devs:
            raise AdbError(
                "没有已连接的设备。检查：\n"
                "  1. USB 线是否支持数据传输（不是纯充电线）\n"
                "  2. 手机是否已开启「USB 调试」\n"
                "  3. 手机上是否点了「允许 USB 调试」弹窗\n"
                "  然后用 adb devices 确认状态是 device 而不是 unauthorized"
            )
        if self.serial:
            if self.serial not in devs:
                raise AdbError(f"指定的设备 {self.serial} 不在已连接列表中: {devs}")
            return self.serial
        self.serial = devs[0]
        return self.serial

    def push(self, local: Path | str, remote: str) -> None:
        self.run(["push", str(local), remote], timeout=120)

    def broadcast(self, action: str, extras: dict, component: str = RECEIVER) -> str:
        args = ["shell", "am", "broadcast", "-a", action, "-n", component,
                "--receiver-foreground"]
        for k, v in extras.items():
            if v is None:
                continue
            if k in STRING_EXTRAS:
                args += ["--es", k, str(v)]
            elif k in LONG_EXTRAS:
                args += ["--el", k, str(v)]
            elif k in INT_EXTRAS:
                args += ["--ei", k, str(v)]
            elif k in FLOAT_EXTRAS:
                args += ["--ef", k, str(v)]
            else:
                args += ["--es", k, str(v)]
        # 广播本身很快，但设备繁忙时可能要等；给 15s 足够
        return self.run(args, timeout=15, check=False).stdout

    def allow_mock_location(self) -> str:
        """把本应用设为「模拟位置信息应用」，等价于开发者选项里手动选择。

        Android 6.0+ 未 root 设备上可用。这是整条链路最关键的一步。
        """
        return self.shell(f"appops set {APP_ID} android:mock_location allow", check=False)

    def mock_location_state(self) -> str:
        out = self.shell(f"appops get {APP_ID} android:mock_location", check=False)
        return out.strip()

    def logcat_clear(self) -> None:
        self.run(["logcat", "-c"], check=False)

    def is_installed(self) -> bool:
        out = self.shell(f"pm list packages {APP_ID}", check=False)
        return APP_ID in out


def shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def dry_run_lines(adb_exe: str, calls: Iterable[Sequence[str]]) -> str:
    """把命令序列渲染成可复制粘贴的文本（--dry-run 用）。"""
    lines = []
    for c in calls:
        parts = [adb_exe] + list(c)
        rendered = " ".join(shell_quote(p) if (" " in p or "\\" in p) else p for p in parts)
        lines.append(rendered)
    return "\n".join(lines)


def eprint(*a: Any, **kw: Any) -> None:
    print(*a, file=sys.stderr, **kw)
