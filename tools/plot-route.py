#!/usr/bin/env python3
"""把轨迹文件画成 PNG，用于人工检查形状与速度剖面。

用途：
  * 确认路径确实沿着道路走（而不是两点直线）
  * 确认速度剖面合理（起步加速 → 巡航 → 末端刹车）
  * 快速对比不同 --speed / --interval / --jitter 参数的效果

用法：
    python tools/plot-route.py out/tianTan.json
    python tools/plot-route.py out/tianTan.json -o out/tianTan.png
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("需要 Pillow：本仓库的运行环境通常已自带。", file=sys.stderr)
    print("若没有：pip install Pillow", file=sys.stderr)
    raise SystemExit(1)

W, H = 1400, 900
PAD = 70
BG = (18, 20, 26)
GRID = (38, 42, 52)
TEXT = (215, 220, 230)
DIM = (130, 138, 150)


def speed_colour(f: float) -> tuple:
    """0..1 -> 蓝→青→绿→黄→红（类似 turbo）。"""
    f = min(1.0, max(0.0, f))
    stops = [
        (0.00, (58, 76, 192)),
        (0.25, (46, 168, 190)),
        (0.50, (86, 190, 96)),
        (0.75, (222, 178, 54)),
        (1.00, (214, 76, 62)),
    ]
    for i in range(1, len(stops)):
        f0, c0 = stops[i - 1]
        f1, c1 = stops[i]
        if f <= f1:
            t = 0.0 if f1 == f0 else (f - f0) / (f1 - f0)
            return tuple(int(c0[k] + (c1[k] - c0[k]) * t) for k in range(3))
    return stops[-1][1]


def load_font(size: int):
    for name in ("msyh.ttc", "msyhbd.ttc", "simhei.ttf", "arial.ttf", "DejaVuSans.ttf"):
        for root in (r"C:\Windows\Fonts", "/usr/share/fonts/truetype/dejavu",
                     "/System/Library/Fonts"):
            p = Path(root) / name
            if p.exists():
                try:
                    return ImageFont.truetype(str(p), size)
                except Exception:
                    pass
    return ImageFont.load_default()


def render(route: dict, out: Path) -> None:
    pts = route["points"]
    if len(pts) < 2:
        raise SystemExit("轨迹点太少，无法绘制")

    lats = [p["lat"] for p in pts]
    lons = [p["lon"] for p in pts]
    lat0, lat1 = min(lats), max(lats)
    lon0, lon1 = min(lons), max(lons)

    # 等距圆柱投影 + 按 cos(lat) 修正经度，保证长宽比不失真
    coslat = math.cos(math.radians((lat0 + lat1) / 2)) or 1.0
    span_x = (lon1 - lon0) * coslat
    span_y = lat1 - lat0
    if span_x <= 0 and span_y <= 0:
        span_x = span_y = 1e-6

    avail_w, avail_h = W - 2 * PAD, H - 2 * PAD - 120
    scale = min(avail_w / span_x if span_x else avail_w,
                avail_h / span_y if span_y else avail_h)
    draw_w, draw_h = span_x * scale, span_y * scale
    off_x = PAD + (avail_w - draw_w) / 2
    off_y = PAD + (avail_h - draw_h) / 2

    def xy(p):
        x = off_x + (p["lon"] - lon0) * coslat * scale
        y = off_y + (lat1 - p["lat"]) * scale
        return (x, y)

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    f_title = load_font(30)
    f_body = load_font(19)
    f_small = load_font(16)

    # 网格
    for i in range(1, 10):
        x = PAD + (W - 2 * PAD) * i / 10
        y = PAD + (H - 2 * PAD) * i / 10
        d.line([(x, PAD), (x, H - PAD)], fill=GRID, width=1)
        d.line([(PAD, y), (W - PAD, y)], fill=GRID, width=1)

    speeds = [p.get("speed", 0.0) for p in pts]
    vmax = max(speeds) or 1.0

    # 轨迹：按速度着色，逐段绘制
    for i in range(1, len(pts)):
        a, b = xy(pts[i - 1]), xy(pts[i])
        d.line([a, b], fill=speed_colour(speeds[i] / vmax), width=5)

    # 每个采样点（点多时只画每 N 个）
    step = max(1, len(pts) // 220)
    for i in range(0, len(pts), step):
        x, y = xy(pts[i])
        d.ellipse([x - 2.5, y - 2.5, x + 2.5, y + 2.5],
                  fill=speed_colour(speeds[i] / vmax))

    # 起终点
    sx, sy = xy(pts[0])
    ex, ey = xy(pts[-1])
    d.ellipse([sx - 11, sy - 11, sx + 11, sy + 11], fill=(70, 210, 120), outline=(240, 255, 245), width=3)
    d.ellipse([ex - 11, ey - 11, ex + 11, ey + 11], fill=(225, 70, 60), outline=(255, 240, 238), width=3)
    d.text((sx + 16, sy - 10), "起点", font=f_body, fill=(150, 240, 180))
    d.text((ex + 16, ey - 10), "终点", font=f_body, fill=(250, 150, 140))

    # 标题与统计
    total = 0.0
    for i in range(1, len(pts)):
        a, b = pts[i - 1], pts[i]
        total += _haversine(a["lat"], a["lon"], b["lat"], b["lon"])
    dur = pts[-1]["t"] / 1000.0
    stats = [f"{len(pts)} 点 · {dur:.0f}s · {total:.0f}m"]
    moving = [s for s in speeds if s > 0.5]
    if moving and total > 10:
        m = sum(moving) / len(moving)
        pk = 1000.0 / m / 60.0
        stats.append(f"平均 {m:.2f} m/s ≈ {int(pk)}'{int((pk % 1) * 60):02d}\"/km")
    d.text((PAD, 24), "轨迹形状 · 速度与海拔剖面   " + "   ".join(stats),
           font=f_title, fill=TEXT)

    # 底部剖面：海拔 + 配速（有海拔起伏时才画）
    alts = [p.get("alt", 0.0) for p in pts]
    if max(alts) - min(alts) > 1.5:
        _draw_profile(d, pts, speeds, alts, f_small)

    # 底部两张信息带：上面一行是统计文字，下面一行是速度色标。
    # 之前把色标放在同一行的右侧，结果和统计文字叠在一起。
    d.text((PAD, H - 86),
           f"速度 平均 {sum(speeds)/len(speeds):.2f} / 最大 {vmax:.2f} m/s    "
           f"模式 {route.get('mode', '-')}    "
           f"来源 {route.get('source', '-')[:46]}",
           font=f_small, fill=DIM)

    bx, by, bw, bh = PAD, H - 52, 300, 12
    for i in range(bw):
        c = speed_colour(i / bw)
        d.line([(bx + i, by), (bx + i, by + bh)], fill=c)
    d.rectangle([bx, by, bx + bw, by + bh], outline=(90, 96, 108))
    d.text((bx + bw + 12, by - 3), f"速度 0 → {vmax:.1f} m/s", font=f_small, fill=DIM)

    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    print(f"已写出 {out}  ({out.stat().st_size/1024:.0f} KB)")


def _draw_profile(d, pts, speeds, alts, font):
    """在图内右下角画一个「沿程海拔 + 配速」剖面小图。

    跑步模拟最需要看的就是这个：路线形状对了不代表配速剖面合理，
    而上坡是否变慢、配速波动是否自然，只有画出来才一眼看得出。
    """
    # 沿程距离
    cum = [0.0]
    for i in range(1, len(pts)):
        cum.append(cum[-1] + _haversine(pts[i - 1]["lat"], pts[i - 1]["lon"],
                                        pts[i]["lat"], pts[i]["lon"]))
    total = cum[-1] or 1.0

    pw, ph = 420, 150
    px0, py0 = W - PAD - pw, H - 250
    d.rectangle([px0 - 12, py0 - 26, px0 + pw + 12, py0 + ph + 30],
                fill=(24, 27, 34), outline=(70, 76, 88))
    d.text((px0 - 4, py0 - 22), "沿程剖面：海拔（灰填）与配速（黄线）",
           font=font, fill=DIM)

    def sx(i):
        return px0 + pw * (cum[i] / total)

    def sy_alt(v):
        lo, hi = min(alts), max(alts)
        span = max(1.0, hi - lo)
        return py0 + ph - ph * (v - lo) / span

    # 海拔填充
    poly = [(sx(i), sy_alt(alts[i])) for i in range(len(pts))]
    d.polygon(poly + [(px0 + pw, py0 + ph), (px0, py0 + ph)],
              fill=(52, 58, 70))
    d.line(poly, fill=(120, 132, 150), width=2)

    # 配速曲线（同图叠加）。注意这里是**显示用**的 5 点滑动平均：
    # 真实 GPS 在跑步速度下的速度读数会有明显抖动（见 docs/07 说明），
    # 原样画出来会糊成锯齿。数据文件里的 speed 字段保持原始值不变，
    # 只有这张图为了可读性做了平滑。
    k = 5
    sm = []
    for i in range(len(speeds)):
        lo = max(0, i - k // 2)
        hi = min(len(speeds), i + k // 2 + 1)
        sm.append(sum(speeds[lo:hi]) / (hi - lo))
    vmaxp = max(0.5, max(sm))
    ppoly = [(sx(i), py0 + ph - ph * 0.85 * (sm[i] / vmaxp))
             for i in range(len(pts))]
    d.line(ppoly, fill=(236, 196, 92), width=2)

    # 刻度（间距按最窄的字体留足，避免文字互相挤压）
    d.text((px0, py0 + ph + 4),
           f"海拔 {min(alts):.0f}~{max(alts):.0f}m", font=font, fill=DIM)
    d.text((px0 + 120, py0 + ph + 4),
           f"速度 0~{vmaxp:.1f}m/s 平滑", font=font, fill=(200, 170, 90))
    d.text((px0 + 300, py0 + ph + 4),
           f"总 {total/1000:.1f}km", font=font, fill=DIM)


def _haversine(a1, o1, a2, o2):
    R = 6371008.8
    p1, p2 = math.radians(a1), math.radians(a2)
    dp, dl = p2 - p1, math.radians(o2 - o1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(h)))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="plot-route", description="把轨迹画成 PNG")
    ap.add_argument("route", help="轨迹 json 文件")
    ap.add_argument("-o", "--out", help="输出 png（默认与输入同名）")
    a = ap.parse_args(argv)
    src = Path(a.route)
    if not src.exists():
        print(f"文件不存在: {src}", file=sys.stderr)
        return 2
    route = json.loads(src.read_text(encoding="utf-8"))
    out = Path(a.out) if a.out else src.with_suffix(".png")
    render(route, out)
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())
