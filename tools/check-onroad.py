#!/usr/bin/env python3
"""检查轨迹是否真的贴着「真实道路」走。

为什么需要它
------------
`verify-route.py` 只是把轨迹画到底图上让人看，2 米的偏差在图上就几个像素，
看不出来。而更严重的问题是：**如果你的路径文件本身不是路**（比如手写的
折线、或途经点落在园区里），轨迹会忠实沿那条假路走 —— 看起来就是
「只按走势行走，不在路上」。

这个工具用 OSM 的**真实道路数据**（Overpass API）做客观判定：
每个轨迹点到最近真实道路的距离是多少。

用法：
    python tools/check-onroad.py out/demo-walk.json
    python tools/check-onroad.py out/demo-walk.json --limit 5
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.parse
import urllib.request
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))

from mockroute import geo, mappicker  # noqa: E402

UA = {"User-Agent": "mockroute/1.0 (road-adherence check)"}

# 视为「道路」的 OSM highway 类型。
# 故意**排除**人行道/小径/台阶之类 —— 我们要检查的是「在道路上」，
# 而不是「在某个可通行面上」。想放宽就把它加进来。
ROAD_TYPES = [
    "motorway", "trunk", "primary", "secondary", "tertiary",
    "unclassified", "residential", "service", "living_street",
    "motorway_link", "trunk_link", "primary_link", "secondary_link",
    "tertiary_link", "road",
]


def fetch_roads(lat0, lon0, lat1, lon1, timeout=60):
    """从 Overpass 拉取范围内的道路几何，返回 [(lat,lon), ...] 列表的列表。"""
    q = f"""
[out:json][timeout:{int(timeout)}];
way["highway"~"^({'|'.join(ROAD_TYPES)})$"]({lat0},{lon0},{lat1},{lon1});
out geom;
"""
    url = "https://overpass-api.de/api/interpreter"
    data = urllib.parse.urlencode({"data": q}).encode()
    req = urllib.request.Request(url, data=data, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout + 15) as r:
        d = json.loads(r.read().decode("utf-8"))
    ways = []
    for el in d.get("elements", []):
        geom = el.get("geometry") or []
        pts = [(g["lat"], g["lon"]) for g in geom if "lat" in g]
        if len(pts) >= 2:
            ways.append(pts)
    return ways


def dev_to_roads(ways, q):
    """点 q 到最近道路的距离。"""
    best = 1e9
    for w in ways:
        for i in range(1, len(w)):
            d = geo._point_to_segment(q, w[i - 1], w[i])
            if d < best:
                best = d
    return best


def main() -> int:
    ap = argparse.ArgumentParser(description="检查轨迹是否在真实道路上")
    ap.add_argument("route", help="轨迹 JSON")
    ap.add_argument("--limit", type=float, default=5.0,
                    help="超过这个距离算「不在道路上」（米，默认 5）")
    ap.add_argument("--sample", type=int, default=120,
                    help="抽取多少个点检查（Overpass 查询有成本）")
    args = ap.parse_args()

    src = Path(args.route)
    if not src.exists():
        print(f"找不到 {src}")
        return 1
    d = json.loads(src.read_text(encoding="utf-8"))
    pts = [(p["lat"], p["lon"]) for p in d["points"]]
    if len(pts) < 2:
        print("轨迹点太少")
        return 1

    lats = [p[0] for p in pts]
    lons = [p[1] for p in pts]
    # 留 100m 余量，避免边界上的点找不到邻近道路
    m = 0.001
    lat0, lat1 = min(lats) - m, max(lats) + m
    lon0, lon1 = min(lons) - m, max(lons) + m

    print("=" * 70)
    print("道路贴合检查（对比 OSM 真实道路）")
    print("=" * 70)
    print(f"  轨迹: {src.name}  {len(pts)} 点")
    print(f"  范围: lat {lat0:.5f}~{lat1:.5f}  lon {lon0:.5f}~{lon1:.5f}")
    print("  正在查询 OSM 道路数据…")
    try:
        ways = fetch_roads(lat0, lon0, lat1, lon1)
    except Exception as e:                                           # noqa: BLE001
        print(f"  查询失败: {type(e).__name__}: {e}")
        print("  （Overpass 可能繁忙或不可达，稍后重试）")
        return 2
    print(f"  取到 {len(ways)} 条道路")
    if not ways:
        print("  该范围内 OSM 没有道路数据 —— 无法判定")
        return 2

    step = max(1, len(pts) // args.sample)
    sample = pts[::step]
    devs = sorted(dev_to_roads(ways, q) for q in sample)
    n = len(devs)
    med, p90, mx = devs[n // 2], devs[int(n * 0.9)], devs[-1]
    over = [x for x in devs if x > args.limit]

    print()
    print(f"  抽样 {n} 个点，到最近真实道路的距离：")
    print(f"    中位 {med:6.1f} m   90分位 {p90:6.1f} m   最大 {mx:6.1f} m")
    print(f"    超过 {args.limit:.0f}m 的点: {len(over)} / {n} "
          f"({100.0*len(over)/n:.0f}%)")
    print()
    if not over:
        print(f"  [OK] 全部在道路 {args.limit:.0f}m 范围内 —— 确实贴着路走")
    elif p90 <= args.limit:
        print(f"  [注意] 大部分在道路上，但有 {len(over)} 个点偏离")
    else:
        print("  [XX] 大量点不在道路上 —— 这条轨迹很可能不是沿路走的。")
        print()
        print("  常见原因：")
        print("    · 起终点/途经点落在公园、广场、小区内部，没有路")
        print("    · 用了手写的路径文件（形状像路但不是真实道路）")
        print("    · 路网后端返回的几何穿越了不可通行区域")
        print()
        print("  怎么修：")
        print("    · 核对每个途经点是否落在道路上（用 audit-aliases.py --render）")
        print("    · 别用 examples/path-demo.txt 这种示意数据当真实路线")
    return 0 if not over else 1

if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())
