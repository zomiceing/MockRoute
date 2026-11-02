#!/usr/bin/env python3
"""mroute —— 用 PC 脚本驱动 Android 手机的行进模拟。

快速上手：

    python tools/mroute.py doctor
    python tools/mroute.py gen --from 39.9087,116.3975 --to 39.9151,116.4040 `
        --provider osrm --speed 1.4 --out out/demo.json
    python tools/mroute.py play --route out/demo.json

完整说明见 docs/ 目录。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# 允许直接 `python tools/mroute.py` 运行（把 tools/ 加进 sys.path）
TOOLS_DIR = Path(__file__).resolve().parent
ROOT = TOOLS_DIR.parent
sys.path.insert(0, str(TOOLS_DIR))

from mockroute import geo, protocol, traj  # noqa: E402
from mockroute import locations as loc  # noqa: E402
from mockroute.player import Player, quick_route  # noqa: E402
from mockroute.protocol import Adb, AdbError, eprint  # noqa: E402
from mockroute.route import ResolveOptions, RouteError, from_file, resolve  # noqa: E402

VERSION = "1.0.0"


def parse_coord(s: str):
    """解析 --from/--to/--via 的值。

    这里**故意宽松**：能认出经纬度就返回 (lat, lon)，认不出就原样返回字符串，
    交给后面的位置解析链处理（命名地点 / 地名解析，见 mockroute/locations.py）。

    如果在这里就报错，就没法写 `--from 天安门` 了 —— argparse 的 type 回调
    发生在所有参数就绪之前。
    """
    c = loc.parse_coord_literal(s)
    if c:
        return c
    if s and s.strip():
        return s.strip()
    raise argparse.ArgumentTypeError("位置不能为空")


def _coord_or_none(v):
    """给需要「确定是坐标」的场合用。"""
    if isinstance(v, tuple):
        return v
    return None


def usage_error(message: str) -> "NoReturn":
    """输入/用法错误统一以退出码 2 结束，与 argparse 的行为一致。

    这样调用方（CI、批处理）只需要判断「2 = 参数给错了」，
    「1 = 运行中出错」，「0 = 成功」。
    """
    print(f"错误: {message}", file=sys.stderr)
    raise SystemExit(2)


def build_adb(args) -> Adb:
    """构造 adb 封装。

    --dry-run 时不去找真的 adb：这条路径只渲染命令文本，
    因此没装 Android SDK 的机器（或 CI）也能用来预览参数。
    """
    if getattr(args, "dry_run", False):
        return Adb(exe=protocol.DRY_RUN_ADB, serial=getattr(args, "serial", None))
    exe = protocol.find_adb(getattr(args, "adb", None))
    return Adb(exe=exe, serial=getattr(args, "serial", None))


def load_route(path: Path) -> dict:
    if not path.exists():
        # 退出码 2 与 argparse 的参数错误保持一致，便于脚本判断
        usage_error(f"轨迹文件不存在: {path}\n先用 gen 子命令生成，例如：\n"
                         f"  python tools/mroute.py gen --from 39.9087,116.3975 "
                         f"--to 39.9151,116.4040 --out {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    problems = traj.validate(data)
    if problems:
        usage_error("轨迹校验失败:\n  - " + "\n  - ".join(problems[:20]))
    return data


# --------------------------------------------------------------------------
# 子命令
# --------------------------------------------------------------------------

def cmd_doctor(args) -> int:
    """环境体检：把「为什么不动」的所有常见原因一次查完。"""
    ok, warn, bad = [], [], []

    print("=" * 62)
    print("  mockroute doctor —— 环境体检")
    print("=" * 62)

    # 1) adb
    try:
        adb = build_adb(args)
        ver = adb.run(["version"], check=False).stdout.strip().splitlines()
        ok.append(f"adb: {args.adb or protocol.find_adb(None)}")
        if ver:
            print("  " + ver[0])
    except AdbError as e:
        bad.append(str(e))
        print(report(ok, warn, bad))
        return 1

    # 2) 设备
    try:
        devs = adb.devices()
        if not devs:
            bad.append("没有已连接的设备（adb devices 为空）")
        else:
            ok.append(f"已连接设备: {', '.join(devs)}")
            adb.require_device()
    except AdbError as e:
        bad.append(str(e))
        print(report(ok, warn, bad))
        return 1

    # 3) 系统信息
    try:
        sdk = adb.shell("getprop ro.build.version.sdk", check=False).strip()
        rel = adb.shell("getprop ro.build.version.release", check=False).strip()
        abi = adb.shell("getprop ro.product.cpu.abi", check=False).strip()
        ok.append(f"Android {rel} (API {sdk}, {abi})")
    except AdbError:
        warn.append("拿不到系统版本信息")

    # 4) 应用
    if adb.is_installed():
        ok.append(f"{protocol.APP_ID} 已安装")
        path = adb.shell(f"pm path {protocol.APP_ID}", check=False).strip()
        if path:
            print(f"  {path}")
    else:
        bad.append(
            f"{protocol.APP_ID} 未安装。安装：\n"
            "      adb install -r mockroute-android/app/build/outputs/apk/debug/app-debug.apk\n"
            "      （还没有 APK 见 docs/01-环境准备.md 的构建步骤）"
        )

    # 5) 模拟位置授权
    if adb.is_installed():
        state = adb.mock_location_state().strip()
        if "allow" in state:
            ok.append("模拟位置授权: allow")
        else:
            if args.fix:
                print(f"  → 正在执行 appops 授权…")
                adb.allow_mock_location()
                state = adb.mock_location_state().strip()
                if "allow" in state:
                    ok.append("模拟位置授权: allow（本次已自动修复）")
                else:
                    bad.append("appops 授权失败，需手动在开发者选项里选择模拟位置信息应用")
            else:
                warn.append(
                    "模拟位置未授权。执行下面任一条即可：\n"
                    f"      adb shell appops set {protocol.APP_ID} android:mock_location allow\n"
                    "      adb shell am start -a android.settings.APPLICATION_DEVELOPMENT_SETTINGS\n"
                    "      （加 --fix 让 doctor 直接帮你执行 appops）"
                )

    # 6) 后台限制（很多「跑几分钟就停」都是这个原因）
    if adb.is_installed():
        standby = adb.shell(f"am get-standby-bucket {protocol.APP_ID}", check=False).strip()
        if standby:
            if "active" in standby or "working" in standby:
                ok.append(f"后台限制: {standby}")
            else:
                warn.append(
                    f"应用后台状态为 {standby}，长时间回放可能被系统冻结。\n"
                    f"      缓解: adb shell am set-standby-bucket {protocol.APP_ID} active\n"
                    "      另外在手机设置里把 MockRoute 的电池优化设为「无限制」"
                )

    # 7) 存储目录可写性
    if adb.is_installed():
        adb.shell(f"mkdir -p {protocol.DEVICE_BASE}", check=False)
        probe = f"{protocol.DEVICE_BASE}/.probe"
        adb.shell(f"echo ok > {probe}", check=False)
        got = adb.shell(f"cat {probe}", check=False).strip()
        adb.shell(f"rm -f {probe}", check=False)
        if got == "ok":
            ok.append(f"目录可写: {protocol.DEVICE_BASE}")
        else:
            bad.append(
                f"无法写入 {protocol.DEVICE_BASE}。先手动启动一次 MockRoute 应用"
                "（系统才会创建该目录），或改用 /sdcard/mockroute/ 并授予存储权限"
            )

    print()
    print(report(ok, warn, bad))
    return 0 if not bad else 1


def report(ok, warn, bad) -> str:
    lines = [f"✓ {x}" for x in ok]
    for x in warn:
        lines.append(f"! {x}")
    for x in bad:
        lines.append(f"✗ {x}")
    lines.append("-" * 62)
    lines.append(f"结论: {len(ok)} 项正常, {len(warn)} 项警告, {len(bad)} 项阻塞")
    if bad:
        lines.append("阻塞项必须解决，否则定位不会生效。")
    elif warn:
        lines.append("可以继续使用，但警告项会影响长时间运行的稳定性。")
    else:
        lines.append("环境就绪，可以开始模拟。")
    return "\n".join(lines)


def _location_opts(args) -> loc.ResolveOptions:
    """构造位置解析选项（读命名地点表、高德 key 等）。"""
    try:
        aliases = loc.load_aliases(ROOT)
    except loc.ResolveError as e:
        usage_error(str(e))
        aliases = {}
    return loc.ResolveOptions(
        aliases=aliases,
        amap_key=getattr(args, "amap_key", None) or os.environ.get("AMAP_KEY", ""),
        city=getattr(args, "city", ""),
        geocode_provider=getattr(args, "geocode", "auto"),
        allow_geocode=not getattr(args, "no_geocode", False),
        timeout=getattr(args, "timeout", 20.0),
    )


def _resolve_place(text, args) -> tuple:
    """把 --from/--to 的值解析成 (lat, lon)。

    值可能是：parse_coord 已经认出来的 (lat, lon) 元组，
    也可能是认不出的原始字符串（命名地点 / 地名）。
    """
    if isinstance(text, tuple):
        return text
    try:
        lat, lon, place = loc.resolve_location(str(text), _location_opts(args))
    except loc.ResolveError as e:
        usage_error(str(e))
        raise
    if not getattr(args, "quiet", False):
        print(f"  {text!r} -> {loc.format_place(place)}")
    return lat, lon


def _fill_gen_defaults(args) -> None:
    """补齐 gen 的默认值，并套用预设。

    顺序很重要，写反了预设就永远不生效：
      1. 先由预设填充「用户完全没提到」的参数；
      2. 再给剩下的参数补基础默认值。
    被预设管理的参数在 argparse 里用 SUPPRESS（不设置属性），所以 hasattr
    能准确区分「用户传了 1000」和「没传」—— `--interval 1000` 不会被改成 500。

    预设之间是**叠加**关系：run 包含 walk 的全部调整（细采样、低噪声）再加上
    跑步特有的部分。这样只写 --preset run 也能得到一套完整可用的参数。
    """
    preset = getattr(args, "preset", "custom")

    layers = []
    if preset in ("walk", "run", "interval"):
        layers.append({
            "speed": 1.35,             # 步行速度
            "interval": 500,           # 细采样 -> 平滑
            "accel": 0.22,             # 人起步/停步很平缓
            "noise": 2.0,              # 城市 GPS 误差量级
            "acc_mean": 6.0,           # 上报精度均值
            "pace_variation": 0.05,    # 步行也有轻微快慢
            "pace_period_m": 240.0,
            "alt_amplitude": 0.0,      # 步行不开地形
        })
    if preset in ("run", "interval"):
        layers.append({
            "speed": 3.3,              # ≈ 5'03"/km，业余跑者常见配速
            "interval": 1000,          # 与真实运动手表一致
            "accel": 0.9,              # 跑步加减速比走路猛得多
            "noise": 1.8,              # 手表/臂带天空视野好，且多有 A-GPS
            "acc_mean": 4.5,           # 运动手表通常上报 3~6m
            "pace_variation": 0.12,    # 跑者分段配速标准差 8%~15%
            "pace_period_m": 180.0,
            # 合成地形起伏。取 ±8m 对应约 4% 的平均坡度 —— 城市爬坡跑的量级；
            # 给太大会变成山地越野（实测 ±18m 在 467m 路程上产生 331m 累计爬升，
            # 平均坡度 24%，配速被压到 6'08"，不像城市跑步）
            "alt_amplitude": 8.0,
            "grade_effect": True,      # 上坡自动变慢
        })
    if preset == "interval":
        layers.append({
            # 间歇训练：快慢交替，周期约 800m（快 400m + 慢 400m）
            "speed": 3.6,
            "interval": 500,
            "accel": 1.2,
            "pace_variation": 0.38,
            "pace_period_m": 800.0,
        })

    # 关键：用 provided 记录「用户是否显式传过」，而不是用 hasattr 判断。
    # 因为第一层（walk 基础）已经 setattr 过了，若继续用 hasattr，第二层
    # （run 特有）就会被自己上一层挡住 —— 实测 --preset run 的速度变成了
    # 步行的 1.35。正确语义是「后一层覆盖前一层，但都不覆盖用户显式值」。
    provided = set()
    for name in ("speed", "interval", "accel", "noise", "acc_mean",
                 "pace_variation", "pace_period_m", "alt_amplitude"):
        if hasattr(args, name):
            provided.add(name)

    for layer in layers:
        for name, value in layer.items():
            if name == "grade_effect":
                # 开关类：只在用户没显式传 --grade-effect 时由预设打开
                if not getattr(args, "grade_effect", False):
                    args.grade_effect = value
            elif name not in provided:
                setattr(args, name, value)

    # 补齐剩余的基础默认值（同样只在用户没显式传、且预设没给时）
    base = {
        "speed": 1.4,
        "interval": 1000,
        "accel": 0.4,
        "noise": 0.0,
        "acc_mean": 8.0,
        "pace_variation": 0.0,
        "pace_period_m": 180.0,
        "alt_amplitude": 0.0,
    }
    for name, value in base.items():
        if name not in provided and not hasattr(args, name):
            setattr(args, name, value)
        elif name not in provided and not layers:
            setattr(args, name, value)


def _resolve_and_generate(args) -> tuple:
    """按参数规划路径 + 生成轨迹，返回 (route, RouteResult)。"""
    _fill_gen_defaults(args)
    coord = args.coord
    provider = args.provider
    origin, dest = args.from_, args.to

    if provider in ("file", "path"):
        if not args.file:
            usage_error("provider=file 需要 --file 指定路径文件，"
                             "例如 --file examples/path-demo.txt")
        res = from_file(Path(args.file))
        # 文件里的坐标默认按 --coord 解释；file 后端统一当 WGS-84 用
        if coord != "wgs84":
            res.points = geo.convert_points(res.points, coord, "wgs84")
    else:
        if origin is None or dest is None:
            usage_error(f"provider={provider} 需要同时给出 --from 和 --to")
        # --from/--to 支持三种写法：经纬度 / 命名地点 / 地名（见 tools/mockroute/locations.py）
        # 解析出来的一定是 WGS-84，所以这里把 --coord 置为 wgs84 交给下游
        origin = _resolve_place(origin, args)
        dest = _resolve_place(dest, args)
        coord = "wgs84"
        opts = ResolveOptions(
            provider=provider,
            osrm_base=args.osrm_base,
            profile=args.profile,
            amap_key=args.amap_key or os.environ.get("AMAP_KEY", ""),
            google_key=args.google_key or os.environ.get("GOOGLE_MAPS_KEY", ""),
            amap_mode=args.amap_mode,
            amap_strategy=args.amap_strategy,
            city=args.city,
            input_coord=coord,
            timeout=args.timeout,
        )
        vias = [_resolve_place(v, args) for v in (args.via or [])]
        res = resolve(origin, dest, vias, opts)

    gen = traj.GenOptions(
        speed=args.speed,
        interval_ms=args.interval,
        accel=args.accel,
        speed_jitter=args.speed_jitter,
        pace_variation=args.pace_variation,
        pace_period_m=args.pace_period,
        grade_effect=args.grade_effect,
        alt_base_m=args.alt_base,
        alt_amplitude_m=args.alt_amplitude,
        noise_sigma_m=args.noise,
        noise_alpha=args.noise_alpha,
        coord_jitter_m=args.jitter,
        acc_mean_m=args.acc_mean,
        bearing_jitter=args.bearing_jitter,
        start_speed=args.start_speed,
        decel_at_end=not args.no_decel,
        lead_in_points=args.lead_in,
        lead_in_radius_m=args.lead_in_radius,
        seed=args.seed,
        mode=args.mode,
        corner_radius_m=args.corner_radius,
        corner_min_turn_deg=args.corner_min_turn,
        corner_step_turn_deg=args.corner_step_turn,
        corner_min_samples=args.corner_min_samples,
        laps=max(1, args.laps),
        max_offset_m=args.max_offset,
        corner_a_lat_max=args.a_lat,
        corner_mode=args.corner_mode,
        corner_v_floor=args.a_lat_floor,
    )
    pts = traj.generate(res.points, gen, start_offset_m=args.start_offset)
    route = traj.build_route(
        pts,
        mode=args.mode,
        interval_ms=args.interval,
        source=f"{res.provider} 路网{res.distance_m:.0f}m/{res.duration_s:.0f}s {res.notes}".strip(),
        extra={"distance_m": round(res.distance_m, 1)},
    )
    return route, res


def cmd_gen(args) -> int:
    _fill_gen_defaults(args)
    route, res = _resolve_and_generate(args)
    out = Path(args.out or f"out/route-{res.provider.split('(')[0].replace('/', '-')}.json")
    traj.write_route(route, out)
    print(f"规划后端: {res.provider}  {res.notes}")
    if res.distance_m:
        print(f"路网里程: {res.distance_m:.0f} m   预计耗时: {res.duration_s:.0f} s")
    print(traj.summarise(route))
    print(f"\n已写出: {out}  ({out.stat().st_size / 1024:.1f} KB)")
    print(f"\n下一步:\n  python tools/mroute.py play --route {out}")
    return 0


def cmd_play(args) -> int:
    route = load_route(Path(args.route))
    if args.mode:
        route["mode"] = args.mode
    adb = build_adb(args)
    player = Player(adb, verbose=not args.quiet, dry_run=args.dry_run)

    print(f"轨迹: {args.route}")
    print(traj.summarise(route))
    if args.direct:
        print("\n模式: 直推（PC 逐点广播）")
    else:
        print("\n模式: 设备回放（推荐）")

    if args.dry_run:
        player.play_route(route, Path(args.route), mode=route.get("mode"),
                          mult=args.mult, remote=args.remote)
        print("\n--dry-run：以上命令未真正执行", file=sys.stderr)
        return 0

    player.prepare(ensure_mock=not args.no_appops)
    if args.standby_fix:
        adb.shell(f"am set-standby-bucket {protocol.APP_ID} active", check=False)
        player.log("已将应用后台策略设为 active")

    try:
        if args.direct:
            player.play_direct(route, mult=args.mult, speed_scale=args.speed_scale,
                               start_offset_ms=args.skip_ms)
        else:
            player.play_route(route, Path(args.route), mode=route.get("mode"),
                              mult=args.mult, remote=args.remote)
            if not args.no_wait:
                dur = route["points"][-1]["t"] / 1000.0 / args.mult
                print(f"\n回放将在 {dur:.0f} 秒后结束（Ctrl+C 提前停止）")
                try:
                    time_wait(dur)
                except KeyboardInterrupt:
                    print("\n已中断，正在停止模拟…")
                    player.stop()
                    return 130
                print("回放结束。")
                if args.stop_when_done:
                    player.stop()
                    print("已停止模拟。")
    finally:
        player.close()
    return 0


def time_wait(seconds: float) -> None:
    import time
    end = time.monotonic() + seconds
    while True:
        left = end - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(1.0, left))
        print(f"\r  剩余 {left:5.0f}s ", end="", flush=True)


def cmd_point(args) -> int:
    adb = build_adb(args)
    player = Player(adb, verbose=not args.quiet, dry_run=args.dry_run)
    lat, lon = args.at
    if not args.dry_run:
        player.prepare(ensure_mock=not args.no_appops)
    player.set_point(lat, lon, speed=args.speed, bearing=args.bearing, acc=args.acc,
                     alt=args.alt, fast=not args.slow)
    if not args.quiet:
        print(f"已设为 {lat:.6f},{lon:.6f}（speed={args.speed} 会一直被重发）")
    player.close()
    return 0


def cmd_circle(args) -> int:
    adb = build_adb(args)
    player = Player(adb, verbose=not args.quiet, dry_run=args.dry_run)
    if not args.dry_run:
        player.prepare(ensure_mock=not args.no_appops)
    player.circle(args.at[0], args.at[1], radius=args.radius,
                  period_s=args.period, acc=getattr(args, "acc", None))
    if not args.quiet:
        v = 2 * 3.141592653589793 * args.radius / args.period
        print(f"绕 {args.at[0]:.6f},{args.at[1]:.6f} 半径 {args.radius}m 转圈，"
              f"周期 {args.period}s，等效线速度 {v:.2f} m/s ({v * 3.6:.1f} km/h)")
    player.close()
    return 0


def cmd_goto(args) -> int:
    adb = build_adb(args)
    player = Player(adb, verbose=not args.quiet, dry_run=args.dry_run)
    if not args.dry_run:
        player.prepare(ensure_mock=not args.no_appops)
    player.goto(args.at[0], args.at[1], speed=args.speed,
                acc=getattr(args, "acc", None))
    player.close()
    return 0


def cmd_ctl(args) -> int:
    adb = build_adb(args)
    player = Player(adb, verbose=not args.quiet, dry_run=args.dry_run)
    action = args.action
    if action == "stop":
        player.stop()
        msg = "已发送停止"
    elif action == "pause":
        player.pause()
        msg = "已暂停"
    elif action == "resume":
        player.resume()
        msg = "已恢复"
    elif action == "mult":
        player.set_mult(args.value)
        msg = f"倍速设为 {args.value}"
    elif action == "status":
        msg = player.ask_status()
    else:
        usage_error(f"未知动作: {action}")
    if not args.quiet:
        print(msg)
    player.close()
    return 0


def cmd_alias(args) -> int:
    """管理命名地点。存一次，之后到处都能写名字。"""
    action = args.action
    if action == "list":
        aliases = loc.load_aliases(ROOT)
        if not aliases:
            print(f"还没有命名地点。文件位置: {loc.alias_path(ROOT)}")
            print('新增：python tools/mroute.py alias add "家" 39.9087,116.3975')
            return 0
        print(f"命名地点（{loc.alias_path(ROOT)}）：")
        for name, e in aliases.items():
            note = e.get("note", "")
            coord = e.get("coord", "wgs84")
            extra = f"  [{coord}]" if coord != "wgs84" else ""
            extra += f"  {note}" if note else ""
            print(f"  {name:<14} {e['lat']:.6f},{e['lon']:.6f}{extra}")
        return 0

    if action == "add":
        if not args.name or not args.at:
            usage_error('用法：alias add "名称" 纬度,经度 [--note 说明] [--coord gcj02]')
        c = loc.parse_coord_literal(args.at)
        if not c:
            # 也允许「用另一个地点/坐标来定义」，例如把地名解析的结果存下来
            try:
                lat, lon, place = loc.resolve_location(args.at, _location_opts(args))
                if not args.quiet:
                    print(f"  解析 {args.at!r} -> {loc.format_place(place)}")
            except loc.ResolveError as e:
                usage_error(f"坐标无法解析：{args.at!r}\n{e}")
                return 2
            c = (lat, lon)
        p = loc.save_alias(ROOT, args.name, c[0], c[1],
                           coord=getattr(args, "coord", "wgs84") or "wgs84",
                           note=args.note or "")
        print(f"已保存 {args.name!r} = {c[0]:.6f},{c[1]:.6f}   -> {p}")
        return 0

    if action == "del":
        if not args.name:
            usage_error("用法：alias del 名称")
        if loc.delete_alias(ROOT, args.name):
            print(f"已删除 {args.name!r}")
            return 0
        print(f"没有名为 {args.name!r} 的命名地点")
        return 1

    usage_error(f"未知动作: {action}")
    return 2


def cmd_resolve(args) -> int:
    """把「位置写法」解析成坐标，可顺带存成命名地点。"""
    try:
        lat, lon, place = loc.resolve_location(args.text, _location_opts(args))
    except loc.ResolveError as e:
        eprint(f"错误: {e}")
        return 2
    print(f"{args.text!r}")
    print(f"  WGS-84 : {lat:.6f},{lon:.6f}")
    print(f"  来源   : {place.provider}")
    if place.detail:
        print(f"  说明   : {place.detail.strip()}")
    if place.coord_src != "wgs84":
        print(f"  (原始坐标系 {place.coord_src}，已转成 WGS-84)")
    if args.save:
        p = loc.save_alias(ROOT, args.save, lat, lon, note=place.detail[:60])
        print(f"  已存为命名地点 {args.save!r} -> {p}")
    return 0


def cmd_current(args) -> int:
    """读设备当前位置（真实或虚拟）。"""
    adb = build_adb(args)
    adb.require_device()
    raw = adb.shell("dumpsys location", timeout=40, check=False)
    info = loc.parse_dumpsys_location(raw)

    if info["redacted"] and not info["real"] and not info["mock"]:
        print("设备对 dumpsys location 做了隐私打码（坐标显示成 39.9******），读不到明文坐标。")
        print()
        print("可选方案：")
        print("  1) 用命名地点：python tools/mroute.py alias add \"当前位置\" <纬度,经度>")
        print("  2) 用地图选点：启动控制台（python tools/gui.py）→ 生成轨迹页 → 地图选点")
        print("  3) 用手机地图 App 长按取坐标，再填进来")
        return 1

    if info["mock"]:
        m = info["mock"]
        print(f"虚拟位置（MockRoute 正在生效）: {m.lat:.6f},{m.lon:.6f}")
    if info["real"]:
        r = info["real"]
        print(f"设备真实位置: {r.lat:.6f},{r.lon:.6f}   ({r.provider})")
        if args.save:
            p = loc.save_alias(ROOT, args.save, r.lat, r.lon, note="读取自设备")
            print(f"已存为命名地点 {args.save!r} -> {p}")
        if args.out:
            Path(args.out).write_text(json.dumps(
                {"lat": r.lat, "lon": r.lon, "source": r.provider},
                ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"已写出 {args.out}")
        return 0
    if info["mock"]:
        print("（没有读到非 mock 的真实位置；MockRoute 可能仍在运行，先 stop 再试）")
        return 1
    print("没有解析到任何位置。")
    return 1


def cmd_search(args) -> int:
    """按关键词检索地点，列出一批候选供挑选。"""
    opts = _location_opts(args)
    try:
        results = loc.search(
            args.text, provider=opts.geocode_provider, amap_key=opts.amap_key,
            city=opts.city, limit=args.limit,
            lat=args.near[0] if args.near else None,
            lon=args.near[1] if args.near else None,
            timeout=opts.timeout)
    except loc.ResolveError as e:
        eprint(f"错误: {e}")
        return 2

    if not results:
        print("没有找到匹配的地点。")
        return 1

    print(f"{args.text!r} 找到 {len(results)} 个结果（来源 {results[0].provider}）：")
    for i, p in enumerate(results, 1):
        print(f"  [{i}] {p.lat:.6f},{p.lon:.6f}  {p.name}")
        if p.detail:
            print(f"      {p.detail}")

    if args.pick:
        idx = int(args.pick)
        if not (1 <= idx <= len(results)):
            eprint(f"错误: --pick 要在 1~{len(results)} 之间")
            return 2
        p = results[idx - 1]
        if args.save:
            path = loc.save_alias(ROOT, args.save, p.lat, p.lon, note=p.name)
            print(f"\n已存为命名地点 {args.save!r} = {p.lat:.6f},{p.lon:.6f} -> {path}")
        else:
            print(f"\n第 {idx} 个: {p.lat:.6f},{p.lon:.6f}")
    else:
        print("\n挑一个用：--pick N 输出坐标；--pick N --save 名字 存成命名地点")
    return 0


def cmd_path(args) -> int:
    """管理保存的选点路径（起点 + 途经点 + 终点）。

    存在 locations.json 里（与命名地点同文件，靠 kind 区分），
    所以旧文件不用迁移。路径存的是 WGS-84 坐标。
    """
    action = args.action

    if action == "list":
        paths = loc.load_paths(ROOT)
        if not paths:
            print(f"还没有保存的路径。文件位置: {loc.alias_path(ROOT)}")
            print('新增：python tools/mroute.py path add "上班" '
                  '--from 39.9087,116.3975 --to 39.9928,116.3964')
            print("控制台里：多点选路页 → 保存选点路径")
            return 0
        print(f"已保存的路径（{loc.alias_path(ROOT)}）：")
        pad = " " * 16
        for name, e in paths.items():
            cli = loc.path_to_cli(e)
            n_via = len(cli["via"])
            note = f"  {e.get('note')}" if e.get("note") else ""
            print(f"  {name:<14} {len(e['points'])} 个点（途经 {n_via}）"
                  f"  模式={e.get('mode', 'once')}{note}")
            print(f"{pad}起点 {cli['from']}")
            for i, v in enumerate(cli["via"], 1):
                print(f"{pad}途经{i} {v}")
            print(f"{pad}终点 {cli['to']}")
        return 0

    if action == "add":
        # 从 --from/--via/--to 收集点（支持命名地点和地名，走位置解析）
        opts = _location_opts(args)
        pts = []
        for label, raw in ([("起点", args.from_)] +
                           [(f"途经{i}", v) for i, v in enumerate(args.via or [], 1)] +
                           [("终点", args.to)]):
            if raw is None:
                continue
            try:
                lat, lon, place = loc.resolve_location(
                    raw if isinstance(raw, str) else f"{raw[0]},{raw[1]}", opts)
            except loc.ResolveError as e:
                usage_error(f"{label} 无法解析：{e}")
                return 2
            if not args.quiet:
                print(f"  {label}: {raw} -> {lat:.6f},{lon:.6f}  ({place.provider})")
            pts.append((lat, lon))
        if len(pts) < 2:
            usage_error("至少要给起点和终点（--from / --to）")
            return 2
        p = loc.save_path(ROOT, args.name, pts, note=args.note or "",
                          mode=args.mode or "once",
                          preset=getattr(args, "preset", "") or "")
        print(f"已保存路径 {args.name!r}（{len(pts)} 个点）-> {p}")
        return 0

    if action == "del":
        if loc.delete_path(ROOT, args.name):
            print(f"已删除路径 {args.name!r}")
            return 0
        print(f"没有名为 {args.name!r} 的路径")
        return 1

    if action == "show":
        paths = loc.load_paths(ROOT)
        if args.name not in paths:
            eprint(f"错误: 没有名为 {args.name!r} 的路径")
            return 1
        cli = loc.path_to_cli(paths[args.name])
        print(f"路径 {args.name!r}: {len(paths[args.name]['points'])} 个点")
        print(f"  --from {cli['from']}")
        for v in cli["via"]:
            print(f"  --via  {v}")
        print(f"  --to   {cli['to']}")
        return 0

    if action == "use":
        """直接用保存的路径生成轨迹 —— 不用再敲坐标。"""
        paths = loc.load_paths(ROOT)
        if args.name not in paths:
            eprint(f"错误: 没有名为 {args.name!r} 的路径")
            print("  用 `path list` 看有哪些")
            return 1
        cli = loc.path_to_cli(paths[args.name])
        argv = ["gen", "--provider", args.provider,
                "--from", cli["from"], "--to", cli["to"]]
        for v in cli["via"]:
            argv += ["--via", v]
        if args.preset:
            argv += ["--preset", args.preset]
        elif cli.get("preset"):
            argv += ["--preset", cli["preset"]]
        argv += ["--mode", args.mode or cli.get("mode") or "once"]
        argv += ["--out", args.out]
        if args.seed is not None:
            argv += ["--seed", str(args.seed)]
        if not args.quiet:
            print(f"用保存的路径 {args.name!r} 生成轨迹：")
            print(f"  {' '.join(argv)}")
            print()
        return main(argv)

    usage_error(f"未知动作: {action}")
    return 2


def cmd_logs(args) -> int:
    """看 App 的运行日志。

    App 自己会把完整运行记录写到外部私有目录（PC 用 adb 就能读，不需要 root）：
        /sdcard/Android/data/com.mockroute/files/mockroute.log
    选这里是因为 logcat 是环形缓冲区，很快被冲掉 —— 实测隔十几小时回去查
    只剩两行分隔符，什么都回溯不了。
    """
    adb = build_adb(args)
    action = getattr(args, "log_action", "show")

    if action == "stream":
        print(f"实时日志 (tag={protocol.TAG})，Ctrl+C 退出…")
        Player(adb).follow_logs(args.seconds)
        return 0

    remote = "/sdcard/Android/data/com.mockroute/files/mockroute.log"

    if action == "path":
        print(f"设备端日志: {remote}")
        print("  查看: python tools/mroute.py logs show --tail 100")
        print("  导出: python tools/mroute.py logs pull")
        return 0

    if not getattr(args, "dry_run", False):
        adb.require_device()

    raw = adb.shell(f"cat {remote} 2>/dev/null || echo __NOREMOTE__",
                    timeout=40, check=False)

    if "__NOREMOTE__" in raw or not raw.strip():
        print(f"设备上还没有日志文件: {remote}")
        print()
        print("可能原因：")
        print("  · 新版 APK 还没装（日志功能是后加的）")
        print("    → powershell -ExecutionPolicy Bypass -File scripts/build-android.ps1 -Install")
        print("  · 装了但还没发过模拟命令 → 先跑一次 play / point")
        return 1

    all_lines = raw.splitlines()

    if action == "pull":
        out = Path(args.out) if getattr(args, "out", None) else (ROOT / "out" / "device-mockroute.log")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(raw, encoding="utf-8")
        print(f"已导出 {len(all_lines)} 行 -> {out}")
        return 0

    lines = all_lines
    if getattr(args, "tag", None):
        want = {t.strip().upper() for t in args.tag.split(",") if t.strip()}
        lines = [l for l in lines if any(f"[{w}]" in l for w in want)]
    if getattr(args, "grep", None):
        lines = [l for l in lines if args.grep in l]
    if getattr(args, "tail", None):
        lines = lines[-args.tail:]

    if not lines:
        print(f"（{len(all_lines)} 行里没有匹配的）")
        return 1

    print(f"设备端 {remote}")
    print(f"共 {len(all_lines)} 行，显示 {len(lines)} 行")
    print()
    for line in lines:
        print(line)
    return 0


def cmd_export(args) -> int:
    """把路网规划出的路径导出成纯文本（每行「纬度,经度」）。

    用途：不想联网时用 `--provider file --file 这个文件` 复用同一段路。
    和「导出轨迹文件」不同 —— 这里存的是**路径几何**（几十个点），
    不是带时间轴的采样点（几千个点）。
    """
    provider = args.provider
    coord = args.coord
    if provider == "file":
        if not args.file:
            usage_error("provider=file 需要 --file")
        res = from_file(Path(args.file))
    else:
        if args.from_ is None or args.to is None:
            usage_error(f"provider={provider} 需要同时给出 --from 和 --to")
        origin = _resolve_place(args.from_, args)
        dest = _resolve_place(args.to, args)
        coord = "wgs84"          # _resolve_place 出来的已经是 WGS-84
        opts = ResolveOptions(
            provider=provider,
            osrm_base=args.osrm_base,
            profile=args.profile,
            amap_key=args.amap_key or os.environ.get("AMAP_KEY", ""),
            google_key=args.google_key or os.environ.get("GOOGLE_MAPS_KEY", ""),
            amap_mode=args.amap_mode,
            amap_strategy=args.amap_strategy,
            city=args.city,
            input_coord=coord,
            timeout=args.timeout,
        )
        vias = [_resolve_place(v, args) for v in (args.via or [])]
        res = resolve(origin, dest, vias, opts)

    pts = res.points
    if coord != "wgs84":
        pts = geo.convert_points(pts, coord, "wgs84")

    before = len(pts)
    if args.simplify and args.simplify > 0:
        pts = geo.simplify(pts, tolerance_m=args.simplify)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# MockRoute 路径文件（纯文本，每行「纬度,经度」）",
        "# 由 mroute export 从真实路网导出，可离线复用：",
        f"#   python tools/mroute.py gen --provider file --file {out.as_posix()} ...",
        "# 坐标系：WGS-84（与安卓定位、OSRM 一致）",
    ]
    if args.note:
        lines.append(f"# 说明：{args.note}")
    lines.append(f"# 来源：{res.provider}  路网 {res.distance_m:.0f}m  "
                 f"抽稀 {before}→{len(pts)} 点")
    for lat, lon in pts:
        lines.append(f"{lat:.7f},{lon:.7f}")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"已写出 {out}  （{len(pts)} 个路径点，路网 {res.distance_m:.0f} m）")
    print(f"  离线复用: python tools/mroute.py gen --provider file "
          f"--file {out.as_posix()} --seed 42 --out out/x.json")
    return 0


def cmd_show(args) -> int:
    route = load_route(Path(args.route))
    print(traj.summarise(route))
    pts = route["points"]
    if args.head:
        print("\n前几个点:")
        for p in pts[: args.head]:
            print("  " + json.dumps(p, ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------
# 参数解析
# --------------------------------------------------------------------------

def add_device_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("--adb", help="adb 可执行文件路径（默认从 PATH 找）")
    p.add_argument("--serial", help="多设备时指定设备序列号")
    p.add_argument("--quiet", action="store_true", help="少输出")


def add_gen_args(p: argparse.ArgumentParser) -> None:
    # ── 会被预设覆盖的参数 ───────────────────────────────────────────────
    # 这些参数的 default 用 SUPPRESS（"不设置属性"），这样就能区分
    # 「用户显式传了 1000」和「用户没传、默认就是 1000」——
    # 否则 --preset walk 会把用户明确指定的值也改掉。
    # 真正的默认值由 _fill_gen_defaults() 补齐。
    p.add_argument("--preset", default="custom",
                   choices=["custom", "walk", "run", "interval"],
                   help="参数预设。walk=步行 / run=跑步（带配速波动与坡度效应）/ "
                        "interval=间歇训练（配速大幅波动）。显式传的参数不会被覆盖")
    p.add_argument("--speed", type=float, default=argparse.SUPPRESS,
                   help="目标速度 m/s（平均配速）。步行 1.35 / 慢跑 2.5 / "
                        "跑步 3.3 / 快跑 4.5 / 冲刺 6+")
    p.add_argument("--interval", type=int, default=argparse.SUPPRESS,
                   help="采样间隔毫秒，默认 1000。跑步建议 500 或更小")
    p.add_argument("--accel", type=float, default=argparse.SUPPRESS,
                   help="加速度上限 m/s²，越小越平缓。步行 0.22 / 跑步 0.9")
    p.add_argument("--noise", type=float, default=argparse.SUPPRESS, metavar="METERS",
                   help="GPS 定位误差标准差（米），默认 0（关闭）。"
                        "真实手机在城市里通常 3~8m；跑步推荐 1.8")
    p.add_argument("--acc-mean", type=float, default=argparse.SUPPRESS,
                   help="上报 accuracy 的均值（米），Weibull 采样，默认 8")
    p.add_argument("--pace-variation", type=float, default=argparse.SUPPRESS,
                   help="配速波动幅度（比例）。跑者分段配速标准差约 8%%~15%%，"
                        "即 0.08~0.15；间歇训练用 0.35 以上")
    p.add_argument("--alt-amplitude", type=float, default=argparse.SUPPRESS,
                   help="合成地形起伏幅度（米），0=平地。开启后生成平滑海拔剖面，"
                        "配合坡度效应让上坡自动变慢")

    # ── 不受预设影响、固定默认值的参数 ───────────────────────────────────
    p.add_argument("--from", dest="from_", type=parse_coord, default=None,
                   metavar="LAT,LON",
                   help="起点（默认 WGS-84）。provider=file 时无需给出")
    p.add_argument("--to", type=parse_coord, default=None, metavar="LAT,LON",
                   help="终点。provider=file 时无需给出")
    p.add_argument("--via", type=parse_coord, action="append", metavar="LAT,LON",
                   help="途经点，可重复")
    p.add_argument("--provider", default="osrm",
                   choices=["osrm", "amap", "google", "file"],
                   help="路径来源。osrm=免费无需 key 的路网规划；file=读本地路径文件")
    p.add_argument("--file", help="provider=file 时的路径文件（GeoJSON / lat,lon 文本 / GPX）")
    p.add_argument("--coord", default="wgs84", choices=["wgs84", "gcj02", "bd09"],
                   help="起终点坐标所属坐标系，默认 wgs84")
    p.add_argument("--profile", default="driving",
                   help="出行方式: driving / walking / bicycling / foot / bike")
    p.add_argument("--osrm-base", default="https://router.project-osrm.org",
                   help="OSRM 服务地址，可指向自建实例")
    p.add_argument("--amap-key", help="高德 key（也可用环境变量 AMAP_KEY）")
    p.add_argument("--amap-mode", default="driving",
                   choices=["driving", "walking", "bicycling", "electrobike", "truck"],
                   help="高德出行方式")
    p.add_argument("--amap-strategy", type=int, default=0, help="高德策略编号，默认 0（速度优先）")
    p.add_argument("--city", default="", help="高德骑行/电单车必填的城市名或 adcode")
    p.add_argument("--google-key", help="Google key（也可用环境变量 GOOGLE_MAPS_KEY）")
    p.add_argument("--timeout", type=float, default=25.0, help="HTTP 超时秒数")
    p.add_argument("--geocode", default="auto", choices=["auto", "amap", "photon", "nominatim"],
                   help="地名解析用哪个服务。auto=有 AMAP_KEY 就用高德，否则 Nominatim"
                        "（注意：国内 Nominatim 常不可达，建议配 AMAP_KEY）")
    p.add_argument("--no-geocode", action="store_true",
                   help="禁用地名解析：--from/--to 只接受经纬度或命名地点（离线用）")

    p.add_argument("--speed-jitter", type=float, default=0.05, help="速度波动比例，默认 0.05")

    # GPS 噪声相关（--noise / --acc-mean 在上面已用 SUPPRESS 声明）
    p.add_argument("--noise-alpha", type=float, default=0.99,
                   help="误差的时间相关性 0~0.99，越大误差越'粘'（漂移越慢）。"
                        "真实 GPS 高度相关，默认 0.99 对应约 50 个采样点的相关长度。"
                        "调低会让相邻点的噪声独立，速度读数会被系统性抬高")
    p.add_argument("--pace-period", type=float, default=180.0,
                   help="配速波动的特征波长（米）。跑步默认 180（约每公里几段起伏）；"
                        "间歇训练建议 400~800")
    p.add_argument("--grade-effect", action="store_true",
                   help="让坡度影响配速（上坡变慢、下坡略快）。需要 --alt-amplitude>0")
    p.add_argument("--alt-base", type=float, default=45.0,
                   help="海拔基准值（米），默认 45")
    p.add_argument("--jitter", type=float, default=0.0,
                   help="【旧参数】白噪声抖动（米），默认 0。新项目请用 --noise")
    p.add_argument("--bearing-jitter", type=float, default=0.0,
                   help="额外的方位角噪声（度），默认 0；坐标噪声已在方向中体现")
    p.add_argument("--start-speed", type=float, default=0.0, help="起始速度，默认 0（从静止起步）")
    p.add_argument("--no-decel", action="store_true", help="终点不做平滑刹车")
    p.add_argument("--lead-in", type=int, default=3, help="起步前原地漂移点数，0=关闭")
    p.add_argument("--lead-in-radius", type=float, default=4.0, help="漂移半径（米）")
    p.add_argument("--start-offset", type=float, default=0.0, help="从路径起点跳过多少米")
    p.add_argument("--mode", default="once", choices=["once", "loop", "pingpong"],
                   help="回放模式")
    p.add_argument("--seed", type=int, help="随机种子，固定后可复现同一份轨迹")
    p.add_argument("--corner-radius", type=float, default=6.0,
                   help="拐角圆滑半径（米）。OSRM 几何在路口是尖角，圆滑后"
                        "转弯分摊到多步，避免『拐弯瞬移』。0=关闭")
    p.add_argument("--corner-min-turn", type=float, default=25.0,
                   help="小于该角度（度）的拐点不做圆滑")
    p.add_argument("--corner-step-turn", type=float, default=75.0,
                   help="提前预判弯道：单步转角超过该度数就提前压小步长，"
                        "把方向变化分摊到多步（避免「突然变向」）。0=关闭")
    p.add_argument("--laps", type=int, default=1,
                   help="绕圈：把路径当一圈，连续跑多少圈（起终点很近时用）。"
                        "时间轴跨圈接续，不会每圈重新起步")
    p.add_argument("--corner-min-samples", type=float, default=1.0,
                   help="弯道内至少落多少个采样点。弯道弧长/该值=步长上限。"
                        "采样点太少时方向会表现为一次突变。1=关闭")
    p.add_argument("--corner-mode", default="auto",
                   choices=["auto", "round", "none"],
                   help="圆滑策略：auto=按切角预算自动挑半径（默认）；"
                        "round=用 --corner-radius；none=不圆滑")
    p.add_argument("--a-lat", type=float, default=1.5,
                   help="按弯的形状限速：过弯横向加速度上限 m/s²"
                        "（a=v²/r）。走路约 1.5，跑步约 3.0。0=关闭")
    p.add_argument("--a-lat-floor", type=float, default=0.6,
                   help="形状限速的速度下限 m/s（再急的弯也不低于它）")
    p.add_argument("--max-offset", type=float, default=4.0,
                   help="上报坐标到路径的最大距离（米）。超过就拉回路面，"
                        "保证虚拟定位不偏出道路。0=不限制")
    p.add_argument("--out", help="输出文件路径")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        prog="mroute",
        description="PC 脚本驱动 Android 手机的虚拟定位与行进模拟",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("--version", action="version", version=f"mroute {VERSION}")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="<子命令>")

    p = sub.add_parser("alias", help="管理命名地点（存一次，之后用名字就行）")
    add_device_args(p)
    p.add_argument("action", choices=["list", "add", "del"], help="list / add / del")
    p.add_argument("name", nargs="?", help="名称（add/del 用）")
    p.add_argument("at", nargs="?", metavar="LAT,LON",
                   help="坐标；也可以写地名或另一个命名地点（需要联网/key）")
    p.add_argument("--note", help="备注")
    p.add_argument("--coord", default="wgs84", choices=["wgs84", "gcj02", "bd09"],
                   help="`at` 的坐标系，默认 wgs84")
    p.add_argument("--geocode", default="auto", choices=["auto", "amap", "photon", "nominatim"])
    p.add_argument("--no-geocode", action="store_true")
    p.add_argument("--amap-key")
    p.add_argument("--city", default="")
    p.add_argument("--timeout", type=float, default=20.0)
    p.set_defaults(func=cmd_alias)

    p = sub.add_parser("resolve", help="把位置写法解析成坐标（可存成命名地点）")
    p.add_argument("text", help="经纬度 / 命名地点 / 地名")
    p.add_argument("--save", metavar="NAME", help="解析成功后存成这个命名地点")
    p.add_argument("--geocode", default="auto", choices=["auto", "amap", "photon", "nominatim"])
    p.add_argument("--no-geocode", action="store_true")
    p.add_argument("--amap-key")
    p.add_argument("--city", default="")
    p.add_argument("--timeout", type=float, default=20.0)
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("current", help="读取设备当前位置（真实/虚拟）")
    add_device_args(p)
    p.add_argument("--save", metavar="NAME", help="把读到的位置存成命名地点")
    p.add_argument("--out", help="把结果写成 JSON 文件")
    p.set_defaults(func=cmd_current)

    p = sub.add_parser("search", help="按关键词检索地点（Photon 免费 / 高德需 key）")
    p.add_argument("text", help="关键词，如「天安门」「西湖 杭州」")
    p.add_argument("--limit", type=int, default=8, help="返回条数，默认 8")
    p.add_argument("--near", type=parse_coord, metavar="LAT,LON",
                   help="偏向这个坐标附近的结果（视野内搜索）")
    p.add_argument("--pick", metavar="N", help="直接取第 N 个")
    p.add_argument("--save", metavar="NAME", help="把 --pick 的那个存成命名地点")
    p.add_argument("--geocode", default="auto",
                   choices=["auto", "amap", "photon", "nominatim"])
    p.add_argument("--no-geocode", action="store_true")
    p.add_argument("--amap-key")
    p.add_argument("--city", default="")
    p.add_argument("--timeout", type=float, default=20.0)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("doctor", help="环境体检并给出修复命令")
    add_device_args(p)
    p.add_argument("--fix", action="store_true", help="自动执行 appops 授权")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("gen", help="规划路径并生成轨迹文件（不连手机）")
    add_gen_args(p)
    p.set_defaults(func=cmd_gen)

    p = sub.add_parser("play", help="把轨迹文件推到手机并开始回放")
    add_device_args(p)
    p.add_argument("--route", required=True, help="轨迹文件路径")
    p.add_argument("--mode", choices=["once", "loop", "pingpong"], help="覆盖文件里的模式")
    p.add_argument("--mult", type=float, default=1.0, help="回放倍速，2=两倍快")
    p.add_argument("--direct", action="store_true",
                   help="直推模式：PC 逐点广播（慢，仅适合调试）")
    p.add_argument("--speed-scale", type=float, default=1.0,
                   help="直推模式下同时缩放速度字段")
    p.add_argument("--skip-ms", type=float, default=0.0,
                   help="直推模式下跳过前多少毫秒")
    p.add_argument("--remote", default=protocol.DEVICE_ROUTE,
                   help=f"设备上的轨迹路径，默认 {protocol.DEVICE_ROUTE}")
    p.add_argument("--no-wait", action="store_true", help="不等待回放结束就退出")
    p.add_argument("--no-appops", action="store_true", help="跳过自动 appops 授权")
    p.add_argument("--standby-fix", action="store_true", help="顺带把后台策略设为 active")
    p.add_argument("--stop-when-done", action="store_true", help="回放结束后自动停止模拟")
    p.add_argument("--dry-run", action="store_true", help="只打印将要执行的命令")
    p.set_defaults(func=cmd_play)

    p = sub.add_parser("point", help="设置一个固定虚拟位置（会一直重发）")
    add_device_args(p)
    p.add_argument("--at", type=parse_coord, required=True, metavar="LAT,LON")
    p.add_argument("--speed", type=float, default=0.0, help="上报速度 m/s")
    p.add_argument("--bearing", type=float, default=0.0, help="上报方位角")
    p.add_argument("--acc", type=float, default=8.0, help="上报精度米")
    p.add_argument("--alt", type=float, default=0.0, help="上报海拔米")
    p.add_argument("--slow", action="store_true", help="不用常驻 shell（排障用）")
    p.add_argument("--no-appops", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_point)

    p = sub.add_parser("circle", help="绕一个点转圈（原地持续移动）")
    add_device_args(p)
    p.add_argument("--at", type=parse_coord, required=True, metavar="LAT,LON", help="圆心")
    p.add_argument("--radius", type=float, default=100.0, help="半径米")
    p.add_argument("--period", type=float, default=60.0, help="绕一圈的秒数")
    p.add_argument("--acc", type=float, default=None,
                   help="上报精度米。目标 App 报「GPS信号弱」时调小（如 1.5）")
    p.add_argument("--no-appops", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_circle)

    p = sub.add_parser("goto", help="从当前位置直线前往目标点")
    add_device_args(p)
    p.add_argument("--at", type=parse_coord, required=True, metavar="LAT,LON")
    p.add_argument("--speed", type=float, default=1.4, help="行进速度 m/s")
    p.add_argument("--acc", type=float, default=None,
                   help="上报精度米。目标 App 报「GPS信号弱」时调小（如 1.5）")
    p.add_argument("--no-appops", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_goto)

    p = sub.add_parser("stop", help="停止模拟并移除 mock provider")
    add_device_args(p)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_ctl, action="stop", value=0)

    p = sub.add_parser("pause", help="暂停回放")
    add_device_args(p)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_ctl, action="pause", value=0)

    p = sub.add_parser("resume", help="恢复回放")
    add_device_args(p)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_ctl, action="resume", value=0)

    p = sub.add_parser("mult", help="运行时调整回放倍速")
    add_device_args(p)
    p.add_argument("value", type=float, help="倍速，0.05~50")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_ctl, action="mult")

    p = sub.add_parser("status", help="查询当前状态")
    add_device_args(p)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_ctl, action="status", value=0)

    p = sub.add_parser("path", help="管理保存的选点路径（可反复使用）")
    p.add_argument("action", choices=["list", "add", "del", "show", "use"])
    p.add_argument("name", nargs="?", help="路径名")
    p.add_argument("--from", dest="from_", type=parse_coord, metavar="LAT,LON",
                   help="起点（也支持命名地点/地名）")
    p.add_argument("--via", type=parse_coord, action="append", metavar="LAT,LON",
                   help="途经点，可重复")
    p.add_argument("--to", type=parse_coord, metavar="LAT,LON", help="终点")
    p.add_argument("--note", help="备注")
    p.add_argument("--mode", default="", choices=["", "once", "loop", "pingpong"])
    p.add_argument("--preset", default="", choices=["", "custom", "walk", "run", "interval"])
    p.add_argument("--provider", default="osrm",
                   choices=["osrm", "amap", "google", "file"],
                   help="use 时用哪个路网后端")
    p.add_argument("--out", default="out/path.json", help="use 时的输出文件")
    p.add_argument("--seed", type=int)
    p.add_argument("--geocode", default="auto",
                   choices=["auto", "amap", "photon", "nominatim"])
    p.add_argument("--no-geocode", action="store_true")
    p.add_argument("--amap-key")
    p.add_argument("--city", default="")
    p.add_argument("--timeout", type=float, default=20.0)
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_path)

    p = sub.add_parser("logs", help="查看 App 运行日志（持久化，可回溯）")
    add_device_args(p)
    p.add_argument("log_action", nargs="?", default="show",
                   choices=["stream", "show", "pull", "path"],
                   help="stream=实时 logcat；show=读设备上的日志文件（默认）；"
                        "pull=导出到本地；path=只打印设备端路径")
    p.add_argument("--seconds", type=float, default=0.0,
                   help="仅 stream 用：只看这么多秒，0=一直看")
    p.add_argument("--tail", type=int, default=80,
                   help="show 时显示最后 N 行（默认 80）")
    p.add_argument("--tag", help="按标签过滤，逗号分隔，如 LIFE,BEAT,ERR,CMD")
    p.add_argument("--grep", help="按关键字过滤")
    p.add_argument("--out", help="pull 时的本地输出路径")
    p.set_defaults(func=cmd_logs)

    p = sub.add_parser("export", help="把路径导出成纯文本（每行 纬度,经度）")
    # 不用 required=True：provider=file 时不需要起终点（直接从文件读几何），
    # 由 cmd_export 里按 provider 分支校验。
    p.add_argument("--from", dest="from_", type=parse_coord,
                   metavar="LAT,LON", help="起点（也支持命名地点/地名）")
    p.add_argument("--to", type=parse_coord, metavar="LAT,LON")
    p.add_argument("--via", type=parse_coord, action="append", metavar="LAT,LON")
    p.add_argument("--provider", default="osrm",
                   choices=["osrm", "amap", "google", "file"])
    p.add_argument("--profile", default="driving")
    p.add_argument("--file", help="provider=file 时的输入文件")
    p.add_argument("--coord", default="wgs84",
                   choices=["wgs84", "gcj02", "bd09"])
    p.add_argument("--out", required=True, help="输出 .txt 路径")
    p.add_argument("--note", default="", help="写进文件头部的说明")
    p.add_argument("--simplify", type=float, default=15.0,
                   help="抽稀容差（米），默认 15 保持文件精炼")
    p.add_argument("--geocode", default="auto",
                   choices=["auto", "amap", "photon", "nominatim"])
    p.add_argument("--no-geocode", action="store_true")
    p.add_argument("--amap-key")
    p.add_argument("--amap-mode", default="driving")
    p.add_argument("--amap-strategy", type=int, default=0)
    p.add_argument("--city", default="")
    p.add_argument("--osrm-base", default="https://router.project-osrm.org")
    p.add_argument("--google-key")
    p.add_argument("--timeout", type=float, default=25.0)
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("show", help="查看轨迹文件摘要")
    p.add_argument("--route", required=True)
    p.add_argument("--head", type=int, default=0, help="同时打印前 N 个点")
    p.set_defaults(func=cmd_show)

    args = ap.parse_args(argv)

    try:
        return args.func(args)
    except (AdbError, RouteError, ValueError, OSError) as e:
        eprint(f"\n错误: {e}")
        return 2
    except KeyboardInterrupt:
        eprint("\n已中断")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
