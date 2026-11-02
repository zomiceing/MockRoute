#!/usr/bin/env python3
"""把轨迹文件画到**真实高德底图**上，核对选点与实际位置是否对齐。

为什么需要它
------------
`plot-route.py` 画的是纯色背景，只能看形状对不对，**看不出位置对不对**。
要验证「选点和实际位置对齐」，必须把轨迹叠在真实底图上，肉眼看它是否
贴着道路、起终点是否落在你点的地方。

这一步曾经救过场：发现 `39.9087,116.3975` 被当成天安门用了很久，
实际是皇史宬（差 552 米）；也发现过 RoutePicker 点击加点漏了
GCJ-02 -> WGS-84 转换，每个点都偏 555 米。

用法：
    python tools/verify-route.py out/demo-walk.json
    python tools/verify-route.py out/demo-walk.json --zoom 15
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import urllib.request
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))

from mockroute import geo, mappicker  # noqa: E402

TILE = mappicker.TILE


def fetch(z: int, x: int, y: int):
    from PIL import Image
    url = mappicker.TILE_URLS[0][1].format(s=(x + y) % 4 + 1, x=x, y=y, z=z)
    req = urllib.request.Request(url, headers=mappicker.UA)
    with urllib.request.urlopen(req, timeout=25) as r:
        return Image.open(io.BytesIO(r.read())).convert("RGB")


def render(route_path: Path, out: Path, zoom: int = 15, pad: int = 60) -> Path:
    from PIL import Image, ImageDraw

    d = json.loads(route_path.read_text(encoding="utf-8"))
    pts = [(p["lat"], p["lon"]) for p in d["points"]]
    if not pts:
        raise SystemExit("轨迹为空")

    # 关键：轨迹存的是 WGS-84，而瓦片是 GCJ-02，必须转换后再算像素，
    # 否则整条线会偏 500~600 米。
    view = [mappicker.from_wgs(lon, lat, mappicker.TILE_URLS[0][3])
            for lat, lon in pts]

    lats = [v[0] for v in view]
    lons = [v[1] for v in view]
    # 算包围盒对应的瓦片范围
    xs, ys = [], []
    for lat, lon in view:
        x, y = mappicker.lonlat_to_world(lon, lat, zoom)
        xs.append(x); ys.append(y)
    x0, x1 = min(xs) - pad, max(xs) + pad
    y0, y1 = min(ys) - pad, max(ys) + pad

    tx0, ty0 = int(x0 // TILE), int(y0 // TILE)
    tx1, ty1 = int(x1 // TILE), int(y1 // TILE)
    n = 2 ** zoom
    W = (tx1 - tx0 + 1) * TILE
    H = (ty1 - ty0 + 1) * TILE
    img = Image.new("RGB", (W, H), (20, 22, 28))
    ok_tiles = 0
    for j in range(ty0, ty1 + 1):
        for i in range(tx0, tx1 + 1):
            if 0 <= i < n and 0 <= j < n:
                try:
                    img.paste(fetch(zoom, i, j), ((i - tx0) * TILE, (j - ty0) * TILE))
                    ok_tiles += 1
                except Exception:                                    # noqa: BLE001
                    pass

    dr = ImageDraw.Draw(img)
    scr = []
    for lat, lon in view:
        x, y = mappicker.lonlat_to_world(lon, lat, zoom)
        scr.append((x - tx0 * TILE, y - ty0 * TILE))

    # 轨迹线
    if len(scr) >= 2:
        dr.line(scr, fill=(60, 150, 255), width=4)
    # 起终点
    def mark(p, colour, label):
        x, y = p
        dr.ellipse([x - 12, y - 12, x + 12, y + 12], fill=colour,
                   outline=(255, 255, 255), width=3)
        dr.text((x + 15, y - 10), label, fill=colour)

    mark(scr[0], (60, 220, 100), "起点")
    if len(scr) > 1:
        mark(scr[-1], (240, 70, 60), "终点")

    # 途经点：用轨迹里明显转折处近似不了，这里标出中间采样点便于看走向
    for k in range(1, len(scr) - 1, max(1, len(scr) // 6)):
        x, y = scr[k]
        dr.ellipse([x - 4, y - 4, x + 4, y + 4], fill=(250, 200, 60))

    img.save(out)
    print(f"  轨迹点   : {len(pts)}")
    print(f"  起点 WGS : {pts[0][0]:.6f},{pts[0][1]:.6f}")
    print(f"  终点 WGS : {pts[-1][0]:.6f},{pts[-1][1]:.6f}")
    print(f"  起点 高德: {view[0][0]:.6f},{view[0][1]:.6f}   (用于叠图)")
    print(f"  底图瓦片 : {ok_tiles} 块  缩放 z{zoom}")
    print(f"  已写出   : {out}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="把轨迹烧到真实底图上核对位置")
    ap.add_argument("route", help="轨迹 JSON")
    ap.add_argument("-o", "--out", help="输出 PNG（默认同名 .verify.png）")
    ap.add_argument("--zoom", type=int, default=15)
    args = ap.parse_args()

    src = Path(args.route)
    if not src.exists():
        print(f"找不到 {src}")
        return 1
    out = Path(args.out) if args.out else src.with_suffix(".verify.png")
    render(src, out, zoom=args.zoom)
    print()
    print("请打开这张图核对：")
    print("  · 蓝色轨迹应当**贴着道路**，穿过街区就是坐标系有问题")
    print("  · 绿色起点、红色终点应当落在你选的地方")
    print("  · 若整体偏移约 500 米，说明漏了 GCJ-02 <-> WGS-84 转换")
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())
