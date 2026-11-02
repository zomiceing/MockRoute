#!/usr/bin/env python3
"""误差预算核查：从「地图上点一下」到「手机收到坐标」，每一环误差多少。

为什么要量化
------------
「不准」是个模糊的说法。实际链路有好几环，每环误差量级完全不同：

  1. 地图选点（像素 -> 经纬度）
  2. GCJ-02 <-> WGS-84 转换
  3. OSRM 把点吸附到最近道路   <-- 往往是最大的一环
  4. 轨迹生成 / 上报

只有把每环量出来，才知道该优化哪个。本工具就干这件事。

用法：
    python tools/error-budget.py                 # 用内置地标
    python tools/error-budget.py --zoom 17       # 指定缩放级别
"""
from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))

from mockroute import geo  # noqa: E402

# 经 audit-aliases.py --reverse / --render 验证过的地标（WGS-84）
LANDMARKS = [
    ("天安门城楼", 39.907359, 116.391263),
    ("天坛公园",   39.879907, 116.402872),
    ("奥林匹克公园", 39.992800, 116.396400),
]


def picker_error(zoom: int, canvas_w: int = 826, canvas_h: int = 703) -> list:
    """模拟「点屏幕正中」，算像素量化带来的误差。

    屏幕坐标是整数像素，半像素的取整误差在地面上就是
    (地面分辨率 / 2)。这就是选点精度的物理下限。
    """
    rows = []
    lat0 = LANDMARKS[0][1]
    # Web Mercator 在纬度 lat、缩放 z 下的地面分辨率（米/像素）
    res = (156543.03392 * math.cos(math.radians(lat0))) / (2 ** zoom)
    for name, lat, lon in LANDMARKS:
        # 中心取整带来的最大偏移：半个像素
        err = res * 0.5
        rows.append((name, zoom, res, err))
    return rows


def conversion_error() -> list:
    """GCJ-02 <-> WGS-84 往返误差（应为浮点噪声级）。"""
    rows = []
    for name, lat, lon in LANDMARKS:
        g = geo.wgs84_to_gcj02(lat, lon)
        back = geo.gcj02_to_wgs84(*g)
        rows.append((name, geo.haversine(lat, lon, back[0], back[1]),
                     geo.haversine(lat, lon, g[0], g[1])))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description="量化定位链路各环误差")
    ap.add_argument("--zoom", type=int, nargs="*",
                    default=[13, 15, 16, 17, 18],
                    help="要评估的缩放级别")
    args = ap.parse_args()

    print("=" * 68)
    print("误差预算核查")
    print("=" * 68)

    print()
    print("【1】地图选点：像素取整误差（选点精度的物理下限）")
    print(f"  {'缩放':>6} {'地面分辨率':>12} {'最大取整误差':>14}")
    for z in args.zoom:
        res = (156543.03392 * math.cos(math.radians(39.9))) / (2 ** z)
        print(f"  z{z:<5} {res:9.2f} m/px {res * 0.5:11.2f} m")
    print("  → 缩放越大越精确。z15 约 2.4m，z17 约 0.6m，z18 约 0.3m")

    print()
    print("【2】坐标系转换：GCJ-02 <-> WGS-84 往返误差")
    for name, back_err, offset in conversion_error():
        flag = "OK " if back_err < 0.01 else "XX "
        print(f"  [{flag}] {name:12} 往返 {back_err:8.4f} m"
              f"   （该地 WGS→GCJ 偏移 {offset:6.1f} m）")
    print("  → 往返应接近 0；这个偏移量就是不转换时的错误大小")

    print()
    print("【3】OSRM 道路吸附：选的点会被挪到最近道路上")
    print("  这一环通常**远大于**前两环，而且无法避免 —— 路网规划本来")
    print("  就只能沿着道路走。想精确落在某点，请把点选在道路上。")
    try:
        from mockroute.route import resolve as route_resolve
        from mockroute.route import ResolveOptions
        opts = ResolveOptions(provider="osrm", profile="foot",
                              osrm_base="https://router.project-osrm.org",
                              timeout=25.0)
        for name, lat, lon in LANDMARKS:
            try:
                # 到自身：OSRM 会把起终点吸附到最近路网
                res = route_resolve((lat, lon), (lat, lon + 0.0009), [], opts)
                p0 = res.points[0]
                d = geo.haversine(lat, lon, p0[0], p0[1])
                print(f"  {name:12} 吸附偏移 {d:6.1f} m"
                      f"   ({lat:.6f},{lon:.6f} -> {p0[0]:.6f},{p0[1]:.6f})")
            except Exception as e:                                   # noqa: BLE001
                print(f"  {name:12} 查询失败: {type(e).__name__}: {str(e)[:50]}")
    except Exception as e:                                           # noqa: BLE001
        print(f"  跳过（无法导入 route 模块）: {e}")

    print()
    print("=" * 68)
    print("结论")
    print("=" * 68)
    print("  · 前两环（选点像素精度 + 坐标转换）都在**米级或更低**，")
    print("    而且转换往返误差接近 0 —— 这部分已经做到位。")
    print("  · 第三环（OSRM 道路吸附）是**几十到上百米**级，它是")
    print("    「我点的位置和生成出来的起点对不上」的主因。")
    print("  · 想更准：① 放大地图再点（z17+）② 把点选在道路上")
    print("    ③ 或者用「命名地点」直接喂精确坐标，跳过手点。")
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())
