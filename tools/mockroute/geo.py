"""大地测量与坐标系工具。

只依赖标准库，方便在没有网络/没有第三方包的机器上直接跑。

坐标系说明（重要）：
  WGS-84  国际标准，GPS 芯片原始输出，Google 地图国际版用
  GCJ-02  中国国测局加密坐标，高德 / 腾讯 / 国内 Google 用
  BD-09   百度在 GCJ-02 上再偏移一次

路线规划 API 与设备定位必须用同一套坐标系，否则会出现几十到几百米的
系统性偏移 —— 这是虚拟定位最常见也最难发现的坑。
"""

from __future__ import annotations

import math
import sys
from typing import Iterable, List, Sequence, Tuple

R_EARTH = 6371008.8  # IUGG 平均地球半径（米）

# GCJ-02 偏移算法的判据参数
_A = 6378245.0
_EE = 0.00669342162296594323


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """两点大圆距离，单位米。"""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R_EARTH * math.asin(min(1.0, math.sqrt(a)))


def bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """起点指向终点的方位角，0=正北，顺时针，单位度。"""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def offset(lat: float, lon: float, brg: float, meters: float) -> Tuple[float, float]:
    """从 (lat, lon) 沿方位角 brg 前进 meters 米后的坐标。"""
    d = meters / R_EARTH
    br = math.radians(brg)
    p1, l1 = math.radians(lat), math.radians(lon)
    p2 = math.asin(math.sin(p1) * math.cos(d) + math.cos(p1) * math.sin(d) * math.cos(br))
    l2 = l1 + math.atan2(
        math.sin(br) * math.sin(d) * math.cos(p1),
        math.cos(d) - math.sin(p1) * math.sin(p2),
    )
    return math.degrees(p2), (math.degrees(l2) + 540.0) % 360.0 - 180.0


def interpolate(lat1: float, lon1: float, lat2: float, lon2: float, f: float) -> Tuple[float, float]:
    """在线段上按比例 f∈[0,1] 取点。

    对经度做 cos(lat) 修正后再插值：在几十到几公里的段长上，
    与真实大地线的偏差远小于 GPS 本身噪声，但计算量小得多。
    """
    f = min(1.0, max(0.0, f))
    lat = lat1 + (lat2 - lat1) * f
    coslat = math.cos(math.radians((lat1 + lat2) / 2))
    if abs(coslat) < 1e-6:
        coslat = 1e-6
    lon = lon1 + (lon2 - lon1) * f
    return lat, lon


def cumulative(points: Sequence[Tuple[float, float]]) -> List[float]:
    """[(lat, lon), ...] -> 每个点的累计里程（米）。"""
    out = [0.0]
    for i in range(1, len(points)):
        out.append(out[-1] + haversine(points[i - 1][0], points[i - 1][1], points[i][0], points[i][1]))
    return out


def point_at_distance(
    points: Sequence[Tuple[float, float]], cum: Sequence[float], dist: float
) -> Tuple[float, float, float]:
    """沿折线前进 dist 米后落在哪里。

    返回 (lat, lon, 该处折线方向角)。
    """
    if not points:
        raise ValueError("points 为空")
    if dist <= 0:
        lat, lon = points[0]
        brg = bearing(*points[0], *points[1]) if len(points) > 1 else 0.0
        return lat, lon, brg
    total = cum[-1]
    if dist >= total:
        lat, lon = points[-1]
        brg = bearing(*points[-2], *points[-1]) if len(points) > 1 else 0.0
        return lat, lon, brg
    # bisect 找所在线段
    lo, hi = 0, len(cum) - 1
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if cum[mid] <= dist:
            lo = mid
        else:
            hi = mid
    seg = cum[hi] - cum[lo]
    f = 0.0 if seg <= 0 else (dist - cum[lo]) / seg
    lat, lon = interpolate(points[lo][0], points[lo][1], points[hi][0], points[hi][1], f)
    return lat, lon, bearing(points[lo][0], points[lo][1], points[hi][0], points[hi][1])


def total_length(points: Sequence[Tuple[float, float]]) -> float:
    return cumulative(points)[-1]


def densify(points: Sequence[Tuple[float, float]], max_segment_m: float = 20.0) -> List[Tuple[float, float]]:
    """把长直线段打散，避免折线插值时出现「瞬移」的假速度。

    注意：这只打散**直线段**，不会圆滑拐角。拐角仍是尖角 —— 要处理
    尖角请用 round_corners()。
    """
    if not points:
        return []
    out = [points[0]]
    for i in range(1, len(points)):
        a, b = points[i - 1], points[i]
        d = haversine(a[0], a[1], b[0], b[1])
        if d <= max_segment_m:
            out.append(b)
            continue
        n = int(math.ceil(d / max_segment_m))
        for k in range(1, n + 1):
            out.append(interpolate(a[0], a[1], b[0], b[1], k / n))
    return out


def round_corners(points: Sequence[Tuple[float, float]],
                  radius_m: float = 8.0,
                  min_turn_deg: float = 25.0,
                  arc_step_m: float = 2.0) -> List[Tuple[float, float]]:
    """把折线的尖角替换成圆弧，使转弯变成「走一段弧线」。

    为什么需要
    ----------
    OSRM 给的几何在路口就是**尖角折线**。按弧长采样时，某一步会正好跨过
    顶点 —— 位置只挪了几十厘米，但方向瞬间转过去。实测数据里出现了一步
    内 bearing 跳 **180°**、位置转角 >90° 有 322 处。目标 App 看起来就是
    「拐弯时莫名瞬移」。

    真实的人/车转弯走的是弧线，不会在一个采样间隔内转 120°。这个函数把
    每个转角用 **二次贝塞尔（近似圆弧）** 替换，转弯分摊到多个采样点上。

    参数
    ----
    radius_m    : 圆角半径（转弯开始提前多少米）。行人路口约 5~10 m，
                  车辆路口约 10~20 m。太大会明显切掉拐角、偏离真实道路。
    min_turn_deg: 小于这个角度的拐点不动（几乎是直线，没必要圆滑）。
    arc_step_m  : 圆弧上每隔多少米取一个点。

    注意：圆角会**略微切掉**拐角内侧（这是圆滑的代价，真实车辆也这么走）。
    半径 8 m 时切掉量不到 1 米，远小于 GPS 噪声。
    """
    pts = list(points)
    if len(pts) < 3 or radius_m <= 0:
        return pts

    n_seg = len(pts) - 1
    # 每段两端已被相邻圆角占用的长度。
    # 必须记录，否则相邻两个拐角各自按「半段长」取切点时**会重叠** ——
    # 圆角互相吃掉，会生成走两米又原路折返的「发夹」，反而制造瞬移。
    # （实测过：不记录的版本在 idx 139 出现 180° 折返）
    used_start = [0.0] * n_seg
    used_end = [0.0] * n_seg

    out: List[Tuple[float, float]] = [pts[0]]
    for i in range(1, len(pts) - 1):
        a, b, c = pts[i - 1], pts[i], pts[i + 1]
        seg_in, seg_out = i - 1, i
        d_in = haversine(a[0], a[1], b[0], b[1])
        d_out = haversine(b[0], b[1], c[0], c[1])
        if d_in < 1e-6 or d_out < 1e-6:
            out.append(b)
            continue

        br_in = bearing(a[0], a[1], b[0], b[1])
        br_out = bearing(b[0], b[1], c[0], c[1])
        turn = abs((br_out - br_in + 540.0) % 360.0 - 180.0)
        if turn < min_turn_deg:
            out.append(b)
            continue

        # 可用长度 = 本段总长 - 已被邻角占用的部分，再留 10% 余量
        avail_in = max(0.0, d_in - used_end[seg_in]) * 0.9
        avail_out = max(0.0, d_out - used_start[seg_out]) * 0.9
        t = min(radius_m, avail_in, avail_out)
        if t < 0.5:
            out.append(b)
            continue
        used_end[seg_in] += t
        used_start[seg_out] += t

        p0 = point_at_distance([a, b], cumulative([a, b]), max(0.0, d_in - t))
        p2 = point_at_distance([b, c], cumulative([b, c]), min(d_out, t))

        # 控制点不能直接用拐点 B —— 对接近 180° 的急折返，那样曲线会向**外侧**
        # 鼓出去（实测长度不降反增：4116.6 -> 4089.3 是切角，但半径 20m 时
        # 反而变成 4176.3）。这里把控制点沿「内角平分线」向路径内侧拉，
        # 保证曲线是切角（更短、更贴原路），而不是外扩。
        mid = ((p0[0] + p2[0]) / 2.0, (p0[1] + p2[1]) / 2.0)
        # 内侧点 = B 向 P0P2 中点方向收一点：B 与 mid 之间取 70%，
        # 既保留转角形状，又把控制点拉离外侧
        ctrl = (b[0] + (mid[0] - b[0]) * 0.35,
                b[1] + (mid[1] - b[1]) * 0.35)

        arc_len = haversine(p0[0], p0[1], ctrl[0], ctrl[1]) + \
            haversine(ctrl[0], ctrl[1], p2[0], p2[1])
        n = max(3, int(math.ceil(arc_len / max(0.5, arc_step_m))))
        for k in range(n + 1):
            f = k / n
            x = (1 - f) ** 2 * p0[0] + 2 * (1 - f) * f * ctrl[0] + f ** 2 * p2[0]
            y = (1 - f) ** 2 * p0[1] + 2 * (1 - f) * f * ctrl[1] + f ** 2 * p2[1]
            out.append((x, y))

    out.append(pts[-1])
    return out


def cut_amount(points: Sequence[Tuple[float, float]],
               radius_m: float,
               min_turn_deg: float = 25.0,
               arc_step_m: float = 2.0) -> float:
    """圆滑 radius_m 会把路径“切掉”多少（**实测**，不是公式估算）。

    对传入的折线调一次 round_corners，再量圆弧点到原始折线的最大距离。

    ⚠ 调用方应当传**抽稀后的拐点**（见 pick_round_radius），不要传完整路径：
    round_corners 是 O(n²)（每个顶点都要 point_at_distance），实测 5000 点
    要 130 秒。切角只发生在拐角处，用拐点就够。

    为什么不用解析式：闭式解在小半径时高估 67%、大半径高估 6%，会让选出的
    半径突破精度预算（实测预算 1m 时实际切了 1.20m）。这个函数是「精度预算」
    的执行者，必须准。
    """
    pts = list(points)
    if len(pts) < 3 or radius_m <= 0:
        return 0.0
    rounded = round_corners(pts, radius_m=radius_m,
                            min_turn_deg=min_turn_deg,
                            arc_step_m=arc_step_m)
    worst = 0.0
    for q in rounded:
        best = 1e9
        for i in range(1, len(pts)):
            d = _point_to_segment(q, pts[i - 1], pts[i])
            if d < best:
                best = d
        if best > worst:
            worst = best
    return worst


def corner_vertices(points: Sequence[Tuple[float, float]],
                    min_turn_deg: float = 25.0) -> List[Tuple[float, float]]:
    """抽出折线里的**拐点**（转角 >= min_turn_deg 的顶点），O(n)。

    切角只发生在拐角处，所以量切角只需要这些点。用它取代 simplify：
    simplify 是 O(n²) 递归（实测 5000 点要 7 秒），而且对小转角舍不得删，
    抽稀得很不够；这个函数一遍扫过，直接留下真正是拐角的顶点。
    """
    pts = list(points)
    if len(pts) < 3:
        return pts
    out = [pts[0]]
    for i in range(1, len(pts) - 1):
        a, b, c = pts[i - 1], pts[i], pts[i + 1]
        d_in = haversine(a[0], a[1], b[0], b[1])
        d_out = haversine(b[0], b[1], c[0], c[1])
        if d_in < 1e-6 or d_out < 1e-6:
            continue
        br_in = bearing(a[0], a[1], b[0], b[1])
        br_out = bearing(b[0], b[1], c[0], c[1])
        turn = abs((br_out - br_in + 540.0) % 360.0 - 180.0)
        if turn >= min_turn_deg:
            out.append(b)
    out.append(pts[-1])
    return out


def pick_round_radius(points: Sequence[Tuple[float, float]],
                      max_cut_m: float,
                      candidates: Sequence[float] = (10.0, 6.0, 4.0, 3.0, 2.0,
                                                     1.0, 0.5),
                      min_turn_deg: float = 25.0,
                      arc_step_m: float = 2.0) -> Tuple[float, float]:
    """在「切角不超过 max_cut_m」的前提下，挑**最大**的圆滑半径。

    返回 (选中的半径, 实测切角)。max_cut_m <= 0 时返回 (0, 0)（不圆滑）。

    步骤（每步都有界，不会卡）：
      1. O(n) 抽出拐点（切角只发生在拐角处）
      2. 对候选半径从大到小**实测**切角，取满足预算的最大者       <= 7 次
      3. 在「选中的档」与「下一档」之间再做 6 次二分细化          <= 6 次
    合计最多 13 次实测。实测值最可信（解析式在小半径高估 67%、大半径高估
    6%，会让选出的半径突破预算）。

    ⚠ 内层迭代次数必须固定上限。之前写成 `while 半径 < 40` 并在循环里调
    O(n²) 的 simplify，大路径上直接卡死（实测踩过两次）。

    为什么要自动挑（用户提出「把两种方式都写入，根据情况自动选择」）
    -------------------------------------------------------------
    圆滑与离路上限是**两种不同的手段**：

      · 圆滑     改的是**真路径**（让它像人走弧线），代价是切角。
                 这部分偏差**离路上限修不了** —— 实测半径 6m 时上限设 3m
                 却量到 3.72m，上限从 4 压到 1 也完全无效。
      · 离路上限 只压 **GPS 噪声**造成的偏离，不改变真路径。

    所以正确的关系不是二选一，而是：**用离路上限的预算去约束圆滑半径**。
    实测半径 2m 就能把「单步转角 >30°」的点抹平，而切角只有 0.67m
    （半径 6m 是 1.76m）—— 说明 6m 是过度圆滑。
    """
    if max_cut_m <= 0:
        return 0.0, 0.0
    pts = list(points)
    if len(pts) < 3:
        return 0.0, 0.0
    verts = corner_vertices(pts, min_turn_deg=min_turn_deg)
    if len(verts) < 3:
        return 0.0, 0.0

    def cut_at(r: float) -> float:
        return cut_amount(verts, r, min_turn_deg=min_turn_deg,
                          arc_step_m=arc_step_m)

    order = sorted(candidates, reverse=True)
    lo = None          # 已知满足预算的**最大**半径
    lo_cut = 0.0
    hi = None          # 已知超预算的**最小**半径
    for r in order:
        c = cut_at(r)
        if c <= max_cut_m:
            lo, lo_cut = r, c
            break
        hi = r
    if lo is None:
        # 连最小候选都超标：给最小候选（避免方向退回尖角），并如实报切角。
        # 注意调用方应当保证预算不小于最小候选能给出的切角，否则这里会超预算。
        smallest = order[-1]
        return smallest, cut_at(smallest)
    # 继续往上试更大的候选 —— 不能一碰到「装得下」就停，
    # 否则明明预算 1m 却只选到切角 0.67m 的半径（实测踩过）。
    for r in order:
        if r <= lo:
            continue
        c = cut_at(r)
        if c <= max_cut_m:
            lo, lo_cut = r, c
        else:
            hi = r if (hi is None or r < hi) else hi
            break
    if hi is None:
        return lo, lo_cut
    # 在 (lo, hi) 之间细化 6 次，逼近预算上限
    for _ in range(6):
        mid = (lo + hi) / 2.0
        c = cut_at(mid)
        if c <= max_cut_m:
            lo, lo_cut = mid, c
        else:
            hi = mid
    return round(lo, 2), lo_cut


def simplify(points: Sequence[Tuple[float, float]], tolerance_m: float = 2.0) -> List[Tuple[float, float]]:
    """Douglas-Peucker 抽稀。轨迹点太多时用它把文件压小（服务端还要再插值回来）。"""

    def dp(pts: List[Tuple[float, float]]) -> List[Tuple[float, float]]:
        if len(pts) < 3:
            return pts
        a, b = pts[0], pts[-1]
        dmax, idx = 0.0, 0
        for i in range(1, len(pts) - 1):
            d = _point_to_segment(pts[i], a, b)
            if d > dmax:
                dmax, idx = d, i
        if dmax > tolerance_m:
            left = dp(pts[: idx + 1])
            right = dp(pts[idx:])
            return left[:-1] + right
        return [a, b]

    pts = list(points)
    return dp(pts) if len(pts) > 2 else pts


def _point_to_segment(p, a, b) -> float:
    """点到线段的最短距离（米），用等距圆柱投影近似。"""
    lat0 = math.radians(a[0])
    kx = R_EARTH * math.cos(lat0) * math.pi / 180.0
    ky = R_EARTH * math.pi / 180.0
    ax, ay = a[1] * kx, a[0] * ky
    bx, by = b[1] * kx, b[0] * ky
    px, py = p[1] * kx, p[0] * ky
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


# --------------------------------------------------------------------------
# 坐标系转换
# --------------------------------------------------------------------------

def _out_of_china(lat: float, lon: float) -> bool:
    return not (73.66 < lon < 135.05 and 3.86 < lat < 53.55)


def _transform_lat(x: float, y: float) -> float:
    ret = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (160.0 * math.sin(y / 12.0 * math.pi) + 320 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    return ret


def _transform_lon(x: float, y: float) -> float:
    ret = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    ret += (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
    ret += (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
    ret += (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    return ret


def wgs84_to_gcj02(lat: float, lon: float) -> Tuple[float, float]:
    if _out_of_china(lat, lon):
        return lat, lon
    dlat = _transform_lat(lon - 105.0, lat - 35.0)
    dlon = _transform_lon(lon - 105.0, lat - 35.0)
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1 - _EE * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((_A * (1 - _EE)) / (magic * sqrtmagic) * math.pi)
    dlon = (dlon * 180.0) / (_A / sqrtmagic * math.cos(radlat) * math.pi)
    return lat + dlat, lon + dlon


def gcj02_to_wgs84(lat: float, lon: float) -> Tuple[float, float]:
    """迭代反解，精度约 1e-7 度（1cm 量级），比一次近似好得多。"""
    if _out_of_china(lat, lon):
        return lat, lon
    wlat, wlon = lat, lon
    for _ in range(6):
        glat, glon = wgs84_to_gcj02(wlat, wlon)
        wlat += lat - glat
        wlon += lon - glon
    return wlat, wlon


def gcj02_to_bd09(lat: float, lon: float) -> Tuple[float, float]:
    z = math.sqrt(lon * lon + lat * lat) + 0.00002 * math.sin(lat * math.pi * 3000.0 / 180.0)
    theta = math.atan2(lat, lon) + 0.000003 * math.cos(lon * math.pi * 3000.0 / 180.0)
    return z * math.sin(theta) + 0.006, z * math.cos(theta) + 0.0065


def bd09_to_gcj02(lat: float, lon: float) -> Tuple[float, float]:
    x, y = lon - 0.0065, lat - 0.006
    z = math.sqrt(x * x + y * y) - 0.00002 * math.sin(y * math.pi * 3000.0 / 180.0)
    theta = math.atan2(y, x) - 0.000003 * math.cos(x * math.pi * 3000.0 / 180.0)
    return z * math.sin(theta), z * math.cos(theta)


def convert_points(
    points: Iterable[Tuple[float, float]], src: str, dst: str
) -> List[Tuple[float, float]]:
    """坐标系批量转换，src/dst ∈ {wgs84, gcj02, bd09}。"""
    src, dst = src.lower(), dst.lower()
    if src == dst:
        return list(points)
    to_wgs = {"wgs84": lambda a, b: (a, b), "gcj02": gcj02_to_wgs84, "bd09": bd09_to_gcj02}
    from_wgs = {"wgs84": lambda a, b: (a, b), "gcj02": wgs84_to_gcj02, "bd09": gcj02_to_bd09}
    if src not in to_wgs or dst not in from_wgs:
        raise ValueError(f"不支持的坐标系: {src} -> {dst}")
    f1, f2 = to_wgs[src], from_wgs[dst]
    out = []
    for lat, lon in points:
        wlat, wlon = f1(lat, lon)
        dlat, dlon = f2(wlat, wlon)
        out.append((dlat, dlon))
    return out


def decode_polyline(encoded: str, precision: int = 5) -> List[Tuple[float, float]]:
    """解码 Google/OSRM 的 encoded polyline，返回 [(lat, lon), ...]。"""
    coords: List[Tuple[float, float]] = []
    index = lat = lon = 0
    factor = 10 ** precision
    while index < len(encoded):
        for is_lon in (False, True):
            result = shift = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if (result & 1) else (result >> 1)
            if is_lon:
                lon += delta
            else:
                lat += delta
        coords.append((lat / factor, lon / factor))
    return coords


def format_coord(lat: float, lon: float) -> str:
    return f"{lat:.6f},{lon:.6f}"
