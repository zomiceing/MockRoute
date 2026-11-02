#!/usr/bin/env python3
"""校验命名地点：列出坐标，反查它到底在地图上的哪儿。

为什么需要这个工具
------------------
曾经踩过一个坑：把 `39.9087,116.3975` 当成「天安门」一直用，实际那是
**皇史宬**，偏了 552 米。因为当时没有任何手段验证「这个坐标是不是真的
在那个地方」，错误就一直没暴露 —— 直到把标记画到真实底图上才看出来。

用法：
    python tools/audit-aliases.py               # 列出所有命名地点
    python tools/audit-aliases.py --reverse     # 联网反查每个点是什么地方
    python tools/audit-aliases.py --render      # 把每个点渲染成带十字的底图
    python tools/audit-aliases.py 天安门         # 只查一个

自己验证一个新坐标的推荐流程：
    1) 地图选点里点一下，记下坐标
    2) python tools/audit-aliases.py --render   （肉眼看十字落点）
    3) 或 --reverse 让地图服务告诉你那是哪儿
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
sys.path.insert(0, str(TOOLS))

from mockroute import geo, locations as loc, mappicker  # noqa: E402

TILE = mappicker.TILE
UA = {"User-Agent": "mockroute/1.0 (alias audit)"}


def fmt_coord(lat: float, lon: float) -> str:
    return f"{lat:.6f},{lon:.6f}"


def reverse_photon(lat: float, lon: float) -> str:
    """反向地理编码：这个坐标对应什么地点。"""
    url = f"https://photon.komoot.io/reverse?lon={lon:.6f}&lat={lat:.6f}&limit=1"
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=20) as r:
            d = json.loads(r.read().decode("utf-8"))
        f = (d.get("features") or [{}])[0]
        p = f.get("properties") or {}
        name = p.get("name") or p.get("street") or "?"
        bits = [x for x in (p.get("district"), p.get("city")) if x]
        return f"{name} / {' · '.join(bits)}" if bits else name
    except Exception as e:                                           # noqa: BLE001
        return f"<反查失败 {type(e).__name__}>"


def fetch_tile(z: int, x: int, y: int):
    from PIL import Image
    url = mappicker.TILE_URLS[0][1].format(s=(x + y) % 4 + 1, x=x, y=y, z=z)
    req = urllib.request.Request(url, headers=mappicker.UA)
    with urllib.request.urlopen(req, timeout=25) as r:
        return Image.open(io.BytesIO(r.read())).convert("RGB")


def render_one(name: str, lat: float, lon: float, zoom: int = 16,
               half: int = 1, out_dir: Path | None = None) -> Path:
    """把该点画在高德底图上，十字落在正确位置才算通过。"""
    from PIL import Image, ImageDraw

    out_dir = out_dir or (ROOT / "out" / "alias-audit")
    out_dir.mkdir(parents=True, exist_ok=True)

    # 关键：先转成底图坐标系（高德=GCJ-02），否则十字会偏 500 米
    vlat, vlon = mappicker.from_wgs(lon, lat, mappicker.TILE_URLS[0][3])
    cx, cy = mappicker.lonlat_to_world(vlon, vlat, zoom)
    tx0, ty0 = int(cx // TILE) - half, int(cy // TILE) - half
    n = 2 ** zoom
    size = TILE * (2 * half + 1)
    img = Image.new("RGB", (size, size), (25, 25, 30))
    for j in range(2 * half + 1):
        for i in range(2 * half + 1):
            tx, ty = tx0 + i, ty0 + j
            if 0 <= tx < n and 0 <= ty < n:
                try:
                    img.paste(fetch_tile(zoom, tx, ty), (i * TILE, j * TILE))
                except Exception:                                    # noqa: BLE001
                    pass

    px = cx - tx0 * TILE
    py = cy - ty0 * TILE
    d = ImageDraw.Draw(img)
    for t in range(-24, 25):
        for w in (-1, 0, 1):
            d.point((px + t, py + w), fill=(255, 45, 45))
            d.point((px + w, py + t), fill=(255, 45, 45))
    d.ellipse([px - 9, py - 9, px + 9, py + 9], outline=(255, 45, 45), width=3)
    d.text((px + 12, py - 22), name, fill=(255, 80, 80))

    safe = "".join(c if c.isalnum() else "_" for c in name)[:20] or "point"
    p = out_dir / f"{safe}.png"
    img.save(p)
    return p


def main() -> int:
    ap = argparse.ArgumentParser(description="校验命名地点的实际位置")
    ap.add_argument("name", nargs="?", help="只查这一个（留空=全部）")
    ap.add_argument("--reverse", action="store_true",
                    help="联网反查每个坐标是什么地方（Photon，国内可达）")
    ap.add_argument("--render", action="store_true",
                    help="渲染带十字的底图到 out/alias-audit/，肉眼核对落点")
    ap.add_argument("--zoom", type=int, default=16)
    ap.add_argument("--sleep", type=float, default=1.2,
                    help="反查间隔秒数（公共实例建议 >=1s）")
    args = ap.parse_args()

    aliases = loc.load_aliases(ROOT)
    if not aliases:
        print(f"没有命名地点（{loc.alias_path(ROOT)}）")
        return 1

    if args.name:
        if args.name not in aliases:
            print(f"没有名为 {args.name!r} 的命名地点")
            return 1
        aliases = {args.name: aliases[args.name]}

    print(f"命名地点表: {loc.alias_path(ROOT)}")
    print(f"共 {len(aliases)} 个\n")

    for name, e in aliases.items():
        lat, lon = float(e["lat"]), float(e["lon"])
        coord = e.get("coord", "wgs84")
        note = f"  ({e['note']})" if e.get("note") else ""
        # 存的是 GCJ-02 的话先转成 WGS-84 统一展示
        wlat, wlon = (geo.gcj02_to_wgs84(lat, lon) if coord == "gcj02"
                      else (lat, lon))
        print(f"  {name}{note}")
        print(f"    WGS-84 : {fmt_coord(wlat, wlon)}")
        if coord != "wgs84":
            print(f"    原始值 : {fmt_coord(lat, lon)}  [{coord}]")
        glat, glon = geo.wgs84_to_gcj02(wlat, wlon)
        print(f"    高德显示: {fmt_coord(glat, glon)}  (WGS->GCJ 偏移 "
              f"{geo.haversine(wlat, wlon, glat, glon):.0f} 米)")

        if args.reverse:
            time.sleep(args.sleep)
            print(f"    反查   : {reverse_photon(wlat, wlon)}")
        if args.render:
            p = render_one(name, wlat, wlon, zoom=args.zoom)
            print(f"    底图   : {p}")
        print()

    if args.render:
        print("请打开上面的底图，确认红色十字落在你期望的地点。")
        print("十字如果明显偏移（几百米），说明坐标或转换有问题。")
    elif not args.reverse:
        print("加 --reverse 联网反查地名，或加 --render 渲染底图肉眼核对。")
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())
