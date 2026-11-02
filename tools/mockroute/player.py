"""ADB 播放控制器：把轨迹变成设备上的定位更新。

两种播放模式，先选对模式再谈参数：

  设备模式（默认，推荐）
      轨迹文件推到手机，手机端自己按时间戳回放。
      只需要 1 次 adb 调用，时间精度取决于手机，抖动在毫秒级。
      长轨迹、高频率、长时间跑，都用这个。

  直推模式（--direct）
      PC 每个点发一条 am broadcast。
      适合在 PC 上临时改坐标、做交互式调试；频率上限大约 2~5 Hz，
      受 adb 往返延迟（几十毫秒）影响，1000 点以上会很慢。

两条路都通过同一个广播协议，随时可以互相切换。
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from . import geo, protocol, traj
from .protocol import (ACTION_GOTO, ACTION_PAUSE, ACTION_RESUME, ACTION_SET,
                       ACTION_SPEED, ACTION_START_CIRCLE, ACTION_START_ROUTE,
                       ACTION_STATUS, ACTION_STOP, DEVICE_BASE, DEVICE_ROUTE,
                       Adb, AdbError)


class ShellError(RuntimeError):
    pass


class PersistentShell:
    """一条常驻的 adb shell，避免每个点都新建进程。

    每次命令后写一个哨兵 echo，读到哨兵就说明命令执行完毕 —— 这是可靠地
    判断「上一条 am broadcast 真的结束了」的唯一办法。
    """

    def __init__(self, adb: Adb):
        self.adb = adb
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._seq = 0

    def start(self) -> None:
        if self._proc and self._proc.poll() is None:
            return
        cmd = self.adb.base() + ["shell"]
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            encoding="utf-8", errors="replace", bufsize=1,
        )
        # 合并 stdout/stderr，后续读一个流就够了
        self.run("exec 2>&1")

    def run(self, command: str, timeout: float = 20.0) -> str:
        with self._lock:
            if not self._proc or self._proc.poll() is not None:
                self.start()
            assert self._proc and self._proc.stdin and self._proc.stdout
            self._seq += 1
            sentinel = f"__MOCKROUTE_DONE_{self._seq}__"
            try:
                self._proc.stdin.write(command + "\n")
                self._proc.stdin.write(f"echo {sentinel}\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                self.close()
                raise ShellError(f"adb shell 管道已断开: {e}") from e

            lines: List[str] = []
            deadline = time.monotonic() + timeout
            while True:
                if time.monotonic() > deadline:
                    raise ShellError(f"adb shell 命令超时: {command}")
                line = self._proc.stdout.readline()
                if not line:
                    self.close()
                    raise ShellError(f"adb shell 意外结束: {command}")
                line = line.rstrip("\r\n")
                if line == sentinel:
                    return "\n".join(lines)
                lines.append(line)

    def close(self) -> None:
        p, self._proc = self._proc, None
        if not p:
            return
        try:
            if p.stdin:
                p.stdin.close()
            p.terminate()
            p.wait(timeout=3)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass


class Player:
    def __init__(self, adb: Adb, verbose: bool = False, dry_run: bool = False):
        self.adb = adb
        self.verbose = verbose
        self.dry_run = dry_run
        self._shell: Optional[PersistentShell] = None

    # ---------- 基础 ----------

    def shell(self) -> PersistentShell:
        if self._shell is None:
            self._shell = PersistentShell(self.adb)
            self._shell.start()
        return self._shell

    def close(self) -> None:
        if self._shell:
            self._shell.close()
            self._shell = None

    def log(self, msg: str) -> None:
        if self.verbose:
            print(f"  · {msg}")

    def send(self, action: str, extras: Dict) -> str:
        if self.dry_run:
            cmd = self._render_broadcast(action, extras)
            print(cmd)
            return "dry-run"
        return self.adb.broadcast(action, extras)

    def send_fast(self, action: str, extras: Dict) -> str:
        """走常驻 shell 的广播，省掉每次 ~50ms 的进程启动开销。"""
        if self.dry_run:
            print(self._render_broadcast(action, extras))
            return "dry-run"
        return self.shell().run(self._broadcast_cmd(action, extras), timeout=15)

    def _render_broadcast(self, action: str, extras: Dict) -> str:
        return f"{self.adb.exe} " + " ".join(self._broadcast_args(action, extras))

    def _broadcast_args(self, action: str, extras: Dict) -> List[str]:
        args = ["shell", "am", "broadcast", "-a", action,
                "-n", protocol.RECEIVER, "--receiver-foreground"]
        for k, v in extras.items():
            if v is None:
                continue
            if k in protocol.STRING_EXTRAS:
                args += ["--es", k, str(v)]
            elif k in protocol.LONG_EXTRAS:
                args += ["--el", k, str(v)]
            elif k in protocol.INT_EXTRAS:
                args += ["--ei", k, str(v)]
            elif k in protocol.FLOAT_EXTRAS:
                args += ["--ef", k, str(v)]
            else:
                args += ["--es", k, str(v)]
        return args

    def _broadcast_cmd(self, action: str, extras: Dict) -> str:
        parts = self._broadcast_args(action, extras)[1:]  # 去掉 'shell'
        out = []
        for p in parts:
            out.append("'" + p.replace("'", "'\\''") + "'" if (" " in p or "'" in p) else p)
        return " ".join(out)

    # ---------- 准备 ----------

    def prepare(self, ensure_mock: bool = True) -> None:
        """检查设备、应用、权限，缺什么报什么。"""
        self.adb.require_device()
        if not self.adb.is_installed():
            raise AdbError(
                f"设备上没装 {protocol.APP_ID}。先安装 APK：\n"
                "  adb install -r mockroute-android/app/build/outputs/apk/debug/app-debug.apk"
            )
        if ensure_mock:
            state = self.adb.mock_location_state()
            if "allow" not in state:
                self.log(f"未授权模拟位置，正在执行 appops: {state.strip() or '(空)'}")
                self.adb.allow_mock_location()
                state = self.adb.mock_location_state()
            if "allow" not in state:
                raise AdbError(
                    "无法把应用设为「模拟位置信息应用」。\n"
                    "手动路径：设置 → 系统 → 开发者选项 → 选择模拟位置信息应用 → MockRoute\n"
                    f"当前 appops 状态: {state.strip() or '(空)'}"
                )
            self.log("模拟位置授权: OK")

    # ---------- 设备模式 ----------

    def push_route(self, route_file: Path, remote: str = DEVICE_ROUTE) -> str:
        base = remote.rsplit("/", 1)[0]
        if self.dry_run:
            print(f"{self.adb.exe} shell mkdir -p {base}")
            print(f"{self.adb.exe} push {route_file} {remote}")
            return remote
        self.adb.shell(f"mkdir -p {base}", check=False)
        self.adb.push(route_file, remote)
        size = self.adb.shell(f"wc -c < {remote}", check=False).strip()
        self.log(f"已推送 {route_file.name} → {remote} ({size} 字节)")
        return remote

    def play_route(self, route: dict, route_file: Path, *, mode: Optional[str] = None,
                   mult: float = 1.0, remote: str = DEVICE_ROUTE) -> None:
        """设备模式：推文件 + 一条 START_ROUTE。"""
        self.push_route(route_file, remote)
        if mult != 1.0:
            self.send(ACTION_SPEED, {"mult": mult})
        extras = {"file": remote}
        if mode:
            extras["mode"] = mode
        self.send(ACTION_START_ROUTE, extras)
        pts = route["points"]
        eff = route.get("mode", "once") if mode is None else mode
        self.log(
            f"已在设备端开始回放: {len(pts)} 点 / {pts[-1]['t'] / 1000:.0f}s / 模式={eff} / 倍速={mult}"
        )

    # ---------- 直推模式 ----------

    def set_point(self, lat: float, lon: float, *, speed: float = 0.0,
                  bearing: float = 0.0, acc: float = 8.0, alt: float = 0.0,
                  fast: bool = True) -> str:
        extras = {
            "lat": lat, "lon": lon, "speed": speed,
            "bearing": bearing, "acc": acc, "alt": alt,
        }
        return self.send_fast(ACTION_SET, extras) if fast else self.send(ACTION_SET, extras)

    def play_direct(self, route: dict, *, base_interval_ms: Optional[int] = None,
                    mult: float = 1.0, speed_scale: float = 1.0,
                    start_offset_ms: float = 0.0, drift: float = 150.0,
                    progress_every: int = 1) -> None:
        """直推模式：PC 逐点广播。

        speed_scale 会同时改写坐标里的 speed 字段和发送节奏，
        所以速度是一致的 —— 只是点变稀疏了，适合快速跑完看效果。
        """
        pts = route["points"]
        if len(pts) < 1:
            raise ValueError("轨迹为空")
        interval = base_interval_ms or route.get("interval", 1000)

        # ⚠️ 单位：ROUTE SCHEMA 里 points[].t 是**毫秒**（traj.py 写入时
        # `int(round(ti * 1000))`）。别被 dumpsys 或摘要里的“秒”误导 ——
        # 5km/1400 点的 t 跨度是 1399000，对应 1399 秒。
        # 曾经误以为 t 是秒而在这里又 ×1000，结果整条路线慢 1000 倍。

        # 预热常驻 shell，避免第一个点被进程启动开销拖慢
        self.shell().run("echo ready", timeout=10)

        t0 = time.monotonic()
        sent = 0
        skip = 1
        for i, p in enumerate(pts):
            rel_ms = p["t"] / mult
            if rel_ms < start_offset_ms:
                continue
            target = t0 + (rel_ms - start_offset_ms) / 1000.0
            now = time.monotonic()
            if now < target:
                time.sleep(target - now)
            elif now - target > drift / 1000.0 * 10:
                # 落后太多说明 PC 或 adb 太慢，跳点追赶
                skip += 1
                continue

            self.send_fast(ACTION_SET, {
                "lat": p["lat"], "lon": p["lon"],
                "speed": p.get("speed", 0.0) * speed_scale,
                "bearing": p.get("bearing", 0.0),
                # 转发轨迹文件里的精度，而不是一律用默认值 ——
                # 否则设备回放精度正常、直推模式精度很差，目标 App 会报「GPS信号弱」
                "acc": p.get("acc", 8.0),
                "alt": p.get("alt", 0.0),
            })
            sent += 1
            if progress_every and sent % progress_every == 0:
                elapsed = time.monotonic() - t0
                print(
                    f"\r  [{sent}/{len(pts)}] t={p['t'] / 1000:6.1f}s "
                    f"lat={p['lat']:.6f} lon={p['lon']:.6f} v={p.get('speed', 0):.2f} "
                    f"实际用时={elapsed:6.1f}s",
                    end="", flush=True,
                )
        print()
        if skip > 1:
            self.log(f"因跟不上节奏跳过了约 {skip - 1} 个点（用设备模式可避免）")

    # ---------- 其它命令 ----------

    def circle(self, lat: float, lon: float, radius: float = 100.0,
               period_s: float = 60.0, acc: float = None) -> str:
        # acc 不给就用 Kotlin 端默认（6f）。想压「GPS信号弱」提示时可以调小。
        extra = {"lat": lat, "lon": lon, "radius": radius,
                 "period": int(period_s * 1000)}
        if acc is not None:
            extra["acc"] = acc
        return self.send(ACTION_START_CIRCLE, extra)

    def goto(self, lat: float, lon: float, speed: float = 1.4,
             acc: float = None) -> str:
        extra = {"lat": lat, "lon": lon, "speed": speed}
        if acc is not None:
            extra["acc"] = acc
        return self.send(ACTION_GOTO, extra)

    def pause(self) -> str:
        return self.send(ACTION_PAUSE, {})

    def resume(self) -> str:
        return self.send(ACTION_RESUME, {})

    def stop(self) -> str:
        return self.send(ACTION_STOP, {})

    def set_mult(self, mult: float) -> str:
        return self.send(ACTION_SPEED, {"mult": mult})

    def ask_status(self) -> str:
        """请求一次状态，从 logcat 里捞出来。"""
        if self.dry_run:
            return self.send(ACTION_STATUS, {})
        self.adb.logcat_clear()
        time.sleep(0.2)
        self.send(ACTION_STATUS, {})
        time.sleep(0.8)
        out = self.adb.run(
            ["logcat", "-d", "-s", f"{protocol.TAG}:I"], check=False, timeout=15
        ).stdout
        relevant = [l for l in out.splitlines() if protocol.TAG in l]
        if not relevant:
            return "(没有拿到状态。确认应用已安装且曾收到过广播；也可直接看手机界面)"
        # 只保留最后一条业务日志
        for line in reversed(relevant):
            if "收到广播" in line:
                continue
            idx = line.find(": ")
            return line[idx + 2:].strip() if idx > 0 else line
        return relevant[-1]

    def follow_logs(self, seconds: float = 0.0) -> None:
        """实时打印应用日志，排障时最有用。"""
        if self.dry_run:
            print(f"{self.adb.exe} logcat -s {protocol.TAG}:I")
            return
        cmd = self.adb.base() + ["logcat", "-s", f"{protocol.TAG}:I"]
        proc = subprocess.Popen(cmd)
        try:
            if seconds > 0:
                time.sleep(seconds)
            else:
                proc.wait()
        except KeyboardInterrupt:
            pass
        finally:
            proc.terminate()


# --------------------------------------------------------------------------
# 便捷构造：从「两个坐标」一路到「设备在走」
# --------------------------------------------------------------------------

def quick_route(
    origin: Tuple[float, float], dest: Tuple[float, float],
    *,
    provider: str = "osrm",
    speed: float = 1.4,
    interval_ms: int = 1000,
    profile: str = "driving",
    coord: str = "wgs84",
    jitter_m: float = 0.0,
    lead_in: int = 3,
    seed: Optional[int] = None,
    osrm_base: str = "https://router.project-osrm.org",
    amap_key: str = "",
    google_key: str = "",
    file: Optional[str] = None,
) -> tuple:
    """规划路径 + 生成轨迹，返回 (route_dict, RouteResult)。"""
    from .route import ResolveOptions, resolve

    opts = ResolveOptions(
        provider=provider, osrm_base=osrm_base, profile=profile,
        amap_key=amap_key, google_key=google_key, input_coord=coord,
        extra={"file": file} if file else {},
    )
    res = resolve(tuple(origin), tuple(dest), [], opts)
    gen = traj.GenOptions(
        speed=speed, interval_ms=interval_ms, coord_jitter_m=jitter_m,
        lead_in_points=lead_in, seed=seed,
    )
    pts = traj.generate(res.points, gen)
    route = traj.build_route(
        pts, mode="once", interval_ms=interval_ms,
        source=f"{res.provider} 路网{geo.total_length(res.points):.0f}m",
        extra={"distance_m": round(res.distance_m, 1), "notes": res.notes},
    )
    return route, res
