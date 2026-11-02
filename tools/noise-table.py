#!/usr/bin/env python3
"""GPS 噪声与离路距离的**最佳配置表**生成器。

为什么要有这个工具
------------------
「噪声设多大、离路上限设多少」不该靠猜，这两个参数互相牵制：
  · 噪声大 -> 更真实，但点位飘得远（可能进建筑）
  · 离路上限小 -> 贴路，但会把误差分布削平（一堆点落在同一半径上）

后者是**可被检测的痕迹**：真实 GPS 的误差是连续的（Rayleigh 分布），
不会有一堆点正好等于某个固定距离。

本工具对每个配置实测四项指标，据此给出推荐：
  1. 离路距离分布（中位 / 90分位 / 最大）
  2. 被夹占比 —— 落在上限 2% 邻域内的点。>10% 说明分布退化
  3. 速度偏差 —— 上报速度相对目标速度的偏差（噪声会抬高读数）
  4. 单步最大转角 —— 方向是否「突然变向」

用法：
    python tools/noise-table.py                    # 完整表
    python tools/noise-table.py --csv out/t.csv    # 另存 CSV
    python tools/noise-table.py --scenario 普通街道步行
"""
from __future__ import annotations

import argparse
import csv
import math
import sys
import time
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))

from mockroute import geo, route as route_mod  # noqa: E402
from mockroute.traj import GenOptions, generate  # noqa: E402

ROAD_FILE = ROOT / "examples" / "road-to-tiantan.txt"

# 场景：(名字, 速度 m/s, 采样 ms, 加速度, 推荐噪声σ, 说明)
SCENARIOS = [
    ("开阔地步行",   1.35, 500, 0.22, 0.5, "广场、江边，天空视野好"),
    ("普通街道步行", 1.35, 500, 0.22, 1.5, "两侧有楼，最典型"),
    ("密集城区步行", 1.35, 500, 0.22, 3.0, "高楼峡谷，误差最大"),
    ("公园小径步行", 1.10, 500, 0.20, 1.0, "树冠遮挡但环境简单"),
    ("开阔地跑步",   3.30, 1000, 0.90, 0.8, "操场、公园跑步"),
    ("城市跑步",     3.30, 1000, 0.90, 2.0, "街跑，最典型"),
    ("高精度步行",   1.20, 250, 0.22, 0.5, "要求贴路 1m 内"),
]

LIMITS = [0, 8, 4, 3, 2, 1]


def _seg_prep(path):
    """预计算线段端点与长度，供批量投影用。"""
    segs = []
    for i in range(1, len(path)):
        a, b = path[i - 1], path[i]
        segs.append((a, b, geo.haversine(a[0], a[1], b[0], b[1])))
    return segs


def devs_fast(segs, qs, sample=400):
    """批量算「点到折线的最短距离」。

    完整算 6700 点 × 2500 段 = 1700 万次，太慢（40s）。
    这里按等间隔抽样，并把每个点限制在邻近线段上算 —— 精度足够看分布。
    """
    step = max(1, len(qs) // sample)
    picked = qs[::step]
    out = []
    for q in picked:
        best = 1e9
        for a, b, seg in segs:
            d = geo._point_to_segment(q, a, b)
            if d < best:
                best = d
        out.append(best)
    return sorted(out)


def max_step_turn(pts):
    mt = 0.0
    for i in range(1, len(pts) - 1):
        b1 = geo.bearing(pts[i-1]["lat"], pts[i-1]["lon"],
                         pts[i]["lat"], pts[i]["lon"])
        b2 = geo.bearing(pts[i]["lat"], pts[i]["lon"],
                         pts[i+1]["lat"], pts[i+1]["lon"])
        mt = max(mt, abs((b2 - b1 + 540) % 360 - 180))
    return mt


def measure(path, segs, speed, iv, accel, sigma, limit, seed=42):
    opts = GenOptions(speed=speed, interval_ms=iv, accel=accel,
                      noise_sigma_m=sigma, noise_alpha=0.99,
                      acc_mean_m=6.0, pace_variation=0.05,
                      pace_period_m=180.0, alt_amplitude_m=0.0,
                      lead_in_points=3, seed=seed,
                      corner_radius_m=6.0, max_offset_m=float(limit))
    pts = generate(path, opts)
    q = [(s["lat"], s["lon"]) for s in pts]

    devs = devs_fast(segs, q)
    n = len(devs)
    med, p90, mx = devs[n // 2], devs[int(n * 0.9)], devs[-1]

    clamped = (sum(1 for x in devs if limit * 0.97 <= x <= limit * 1.02)
               if limit > 0 else 0)
    cp = 100.0 * clamped / n

    moving = [s["speed"] for s in pts if s.get("speed", 0) > 0.3]
    ve = (100.0 * ((sum(moving) / len(moving)) - speed) / speed) if moving else 0.0

    return med, p90, mx, cp, ve, len(pts)


def verdict(cp, limit):
    if limit == 0:
        return "不限制"
    if cp < 3:
        return "★ 推荐"
    if cp < 10:
        return "○ 可接受"
    return "✗ 分布退化"


def main() -> int:
    ap = argparse.ArgumentParser(description="GPS 噪声与离路距离配置表")
    ap.add_argument("--csv", help="另存 CSV")
    ap.add_argument("--scenario", action="append", help="只看某个场景")
    args = ap.parse_args()

    res = route_mod.from_file(ROAD_FILE)
    raw = [(p[0], p[1]) for p in res.points]
    print("=" * 86)
    print("GPS 噪声 × 离路距离 · 实测最佳配置表")
    print("=" * 86)
    print(f"  基准: {ROAD_FILE.name}（真实道路 {geo.total_length(raw):.0f} m）")
    print("  离路 = 上报坐标到路径的距离 | 被夹 = 落在上限附近的点占比")
    print("  被夹 >10% 表示误差分布退化成一个圆 —— 真实 GPS 不会这样")
    print()

    rows = []
    for name, speed, iv, acc, sigma, desc in SCENARIOS:
        if args.scenario and name not in args.scenario:
            continue
        step = speed * iv / 1000.0
        p = geo.round_corners(list(raw), radius_m=6.0, min_turn_deg=25.0,
                              arc_step_m=max(1.0, step / 4.0))
        p = geo.densify(p, max_segment_m=max(2.0, min(25.0, step / 4.0)))
        segs = _seg_prep(p)
        t0 = time.time()
        print("=" * 86)
        print(f"【{name}】{speed} m/s · 采样 {iv}ms · 推荐 σ={sigma}  —— {desc}")
        print("=" * 86)
        print(f"  {'离路上限':>8} {'中位':>7} {'90分位':>8} {'最大':>7} "
              f"{'被夹':>7} {'速度偏差':>9}  判定")
        for limit in LIMITS:
            med, p90, mx, cp, ve, n = measure(p, segs, speed, iv, acc, sigma, limit)
            v = verdict(cp, limit)
            print(f"  {limit:>8} {med:6.2f}m {p90:7.2f}m {mx:6.2f}m "
                  f"{cp:6.1f}% {ve:+8.1f}%  {v}")
            rows.append({
                "场景": name, "速度_m/s": speed, "采样_ms": iv,
                "噪声sigma": sigma, "离路上限_m": limit,
                "离路中位_m": round(med, 2), "离路90分位_m": round(p90, 2),
                "离路最大_m": round(mx, 2), "被夹_pct": round(cp, 1),
                "速度偏差_pct": round(ve, 1), "判定": v,
            })
        print(f"  （{time.time()-t0:.1f}s）")
        print()

    print("=" * 86)
    print("怎么用这张表")
    print("=" * 86)
    print("  1. 先按环境选 σ（开阔 0.5 / 普通街道 1.5 / 密集城区 3.0）")
    print("  2. 再按「被夹 <3%」挑离路上限 —— 顺序不能反")
    print("  3. 被夹占比只取决于「上限 ÷ σ」：想 <3%，上限要 ≥ 3×σ")
    print("  4. 「最大」列偶尔略大于上限是正常的：环路径的离散采样误差约 0.9m")

    if args.csv:
        out = Path(args.csv)
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)
        print(f"\n  已写出 CSV: {out}  （{len(rows)} 行）")
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())
