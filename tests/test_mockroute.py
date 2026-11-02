"""mockroute 自检 —— 不连手机、不联网也能跑。

    python -m unittest discover -s tests -v

覆盖的是「生成出来的东西一定合法、协议参数一定没错」这类容易悄悄坏掉的部分：
  * 大地测量与坐标系转换的数值正确性
  * 轨迹生成的时间戳单调性、速度剖面、字段自洽
  * 路径解析（GeoJSON / GPX / 纯文本）
  * 广播参数渲染（PC 侧与 Android 端的契约）
  * CLI 端到端 smoke test
"""

from __future__ import annotations

import json
import math
import random
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from mockroute import geo, protocol, traj  # noqa: E402
from mockroute.player import Player  # noqa: E402
from mockroute.protocol import Adb  # noqa: E402
from mockroute.route import (ResolveOptions, RouteError, from_file, resolve,  # noqa: E402
                             osrm)

EXAMPLES = ROOT / "examples"


# --------------------------------------------------------------------------
# geo
# --------------------------------------------------------------------------

class TestGeo(unittest.TestCase):
    def test_haversine_known_distance(self):
        # 天安门 → 鸟巢，直线约 8.5 km（±50m 容差）
        d = geo.haversine(39.9087, 116.3975, 39.9928, 116.3964)
        self.assertGreater(d, 9200)
        self.assertLess(d, 9400)

    def test_haversine_symmetric_and_zero(self):
        a, b = (39.9, 116.4), (39.95, 116.45)
        self.assertAlmostEqual(geo.haversine(*a, *b), geo.haversine(*b, *a), places=6)
        self.assertAlmostEqual(geo.haversine(*a, *a), 0.0, places=6)

    def test_bearing_cardinal(self):
        self.assertAlmostEqual(geo.bearing(0, 0, 1, 0), 0.0, places=3)      # 正北
        self.assertAlmostEqual(geo.bearing(0, 0, 0, 1), 90.0, places=3)     # 正东
        self.assertAlmostEqual(geo.bearing(1, 0, 0, 0), 180.0, places=3)    # 正南
        self.assertAlmostEqual(geo.bearing(0, 1, 0, 0), 270.0, places=3)    # 正西

    def test_offset_roundtrip(self):
        lat, lon = 39.9087, 116.3975
        for brg in (0, 45, 137, 250, 359):
            dlat, dlon = geo.offset(lat, lon, brg, 500.0)
            self.assertAlmostEqual(geo.haversine(lat, lon, dlat, dlon), 500.0, delta=0.5)
            self.assertAlmostEqual(geo.bearing(lat, lon, dlat, dlon), brg, delta=0.5)

    def test_interpolate_midpoint(self):
        lat, lon = geo.interpolate(39.9, 116.4, 39.91, 116.42, 0.5)
        self.assertAlmostEqual(lat, 39.905, places=6)
        self.assertAlmostEqual(lon, 116.41, places=6)

    def test_cumulative_monotonic(self):
        pts = [(39.9, 116.4), (39.901, 116.401), (39.902, 116.403)]
        cum = geo.cumulative(pts)
        self.assertEqual(cum[0], 0.0)
        self.assertEqual(len(cum), len(pts))
        self.assertTrue(all(cum[i] < cum[i + 1] for i in range(len(cum) - 1)))

    def test_point_at_distance_ends(self):
        pts = [(39.9, 116.4), (39.91, 116.4)]
        cum = geo.cumulative(pts)
        total = cum[-1]
        lat, lon, _ = geo.point_at_distance(pts, cum, 0.0)
        self.assertAlmostEqual(lat, pts[0][0], places=6)
        mid_lat, _, _ = geo.point_at_distance(pts, cum, total / 2)
        self.assertAlmostEqual(mid_lat, (pts[0][0] + pts[1][0]) / 2, places=4)
        end_lat, _, brg = geo.point_at_distance(pts, cum, total * 2)
        self.assertAlmostEqual(end_lat, pts[1][0], places=6)
        self.assertAlmostEqual(brg, 0.0, places=3)

    def test_gcj02_roundtrip(self):
        for lat, lon in [(39.9087, 116.3975), (31.2304, 121.4737), (22.5431, 114.0579)]:
            g = geo.wgs84_to_gcj02(lat, lon)
            # GCJ-02 偏移量在中国境内约 300~700 米
            shift = geo.haversine(lat, lon, *g)
            self.assertGreater(shift, 100, f"偏移过小，算法可能没生效: {shift}")
            self.assertLess(shift, 1000)
            back = geo.gcj02_to_wgs84(*g)
            self.assertAlmostEqual(back[0], lat, places=6)
            self.assertAlmostEqual(back[1], lon, places=6)

    def test_gcj02_no_shift_outside_china(self):
        for lat, lon in [(35.6762, 139.6503), (48.8566, 2.3522), (-33.8688, 151.2093)]:
            self.assertEqual(geo.wgs84_to_gcj02(lat, lon), (lat, lon))

    def test_bd09_roundtrip(self):
        lat, lon = 39.9087, 116.3975
        b = geo.gcj02_to_bd09(lat, lon)
        back = geo.bd09_to_gcj02(*b)
        self.assertAlmostEqual(back[0], lat, places=5)
        self.assertAlmostEqual(back[1], lon, places=5)

    def test_convert_points_matrix(self):
        pt = (39.9087, 116.3975)
        for src in ("wgs84", "gcj02", "bd09"):
            for dst in ("wgs84", "gcj02", "bd09"):
                out = geo.convert_points([pt], src, dst)
                self.assertEqual(len(out), 1)
                if src == dst:
                    self.assertAlmostEqual(out[0][0], pt[0], places=9)
        # 绕一圈回到原地
        round_trip = geo.convert_points(
            geo.convert_points([pt], "wgs84", "bd09"), "bd09", "wgs84"
        )
        self.assertAlmostEqual(round_trip[0][0], pt[0], places=6)
        self.assertAlmostEqual(round_trip[0][1], pt[1], places=6)

    def test_convert_rejects_unknown_crs(self):
        with self.assertRaises(ValueError):
            geo.convert_points([(1.0, 2.0)], "wgs84", "mars")

    def test_decode_polyline_known_value(self):
        # Google 官方文档示例：_p~iF~ps|U_ulLnnqC_mqNvxq`@
        pts = geo.decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@")
        self.assertEqual(len(pts), 3)
        self.assertAlmostEqual(pts[0][0], 38.5, places=5)
        self.assertAlmostEqual(pts[0][1], -120.2, places=5)
        self.assertAlmostEqual(pts[2][0], 43.252, places=5)
        self.assertAlmostEqual(pts[2][1], -126.453, places=5)

    def test_densify_adds_points(self):
        pts = [(39.9, 116.4), (39.99, 116.4)]  # 约 10 km
        dense = geo.densify(pts, max_segment_m=100.0)
        self.assertGreater(len(dense), 90)
        # 首尾必须保持
        self.assertAlmostEqual(dense[0][0], pts[0][0], places=9)
        self.assertAlmostEqual(dense[-1][0], pts[-1][0], places=9)
        # 相邻间距不超过限制
        for i in range(1, len(dense)):
            d = geo.haversine(dense[i - 1][0], dense[i - 1][1], dense[i][0], dense[i][1])
            self.assertLessEqual(d, 100.5)

    def test_simplify_keeps_shape(self):
        pts = [(39.9, 116.4)]
        for i in range(1, 200):
            pts.append((39.9 + i * 0.0001, 116.4 + i * 0.0001))
        simple = geo.simplify(pts, tolerance_m=2.0)
        self.assertLess(len(simple), len(pts))
        self.assertGreaterEqual(len(simple), 2)
        self.assertAlmostEqual(simple[0][0], pts[0][0], places=9)
        self.assertAlmostEqual(simple[-1][0], pts[-1][0], places=9)


# --------------------------------------------------------------------------
# traj
# --------------------------------------------------------------------------

class TestTraj(unittest.TestCase):
    def path(self, n=20, step_m=25.0):
        """一条向东的直线，长 n*step_m 米。"""
        lat = 39.9075
        lon = 116.391
        pts = [(lat, lon)]
        for _ in range(n):
            lat, lon = geo.offset(lat, lon, 90.0, step_m)
            pts.append((lat, lon))
        return pts

    def test_corner_shape_decides_speed(self):
        """按弯的**形状**决定过弯速度：半径越小越慢。

        用户要求「按拐弯的形状来决定拐弯的动作」。判据用横向加速度
        a = v²/r —— 转弯靠向心力，它必须有上限（走路约 1.5、跑步约 3.0）。

        实测（跑步 3.3 m/s、a_lat 上限 3.0）：
          半径 1m -> 0.62 m/s (a=0.38)   半径 3m -> 2.40 (a=1.92)
          半径 8m -> 3.28 m/s (a=1.34)   半径 30m -> 3.30（不减速）
        """
        import math
        from mockroute import geo
        from mockroute.traj import GenOptions, generate

        def arc_corner(radius_m, turn_deg=90.0, leg_m=120.0):
            lat0, lon0 = 39.9, 116.4
            mlat = 111320.0
            mlon = 111320.0 * math.cos(math.radians(lat0))

            def ll(x, y):
                return (lat0 + y / mlat, lon0 + x / mlon)

            pts = [ll(leg_m * i / 40, 0.0) for i in range(41)]
            cx, cy, a0 = leg_m, radius_m, -math.pi / 2
            n = max(8, int(turn_deg / 3))
            for i in range(n + 1):
                a = a0 + math.radians(turn_deg) * i / n
                pts.append(ll(cx + radius_m * math.cos(a),
                              cy + radius_m * math.sin(a)))
            ae = a0 + math.radians(turn_deg)
            dx, dy = -math.sin(ae), math.cos(ae)
            ex, ey = (cx + radius_m * math.cos(ae), cy + radius_m * math.sin(ae))
            for i in range(1, 41):
                pts.append(ll(ex + dx * leg_m * i / 40, ey + dy * leg_m * i / 40))
            return pts

        speed, iv = 3.3, 1000
        step = speed * iv / 1000.0
        results = {}
        for radius in (1.0, 3.0, 8.0, 30.0):
            path = arc_corner(radius)
            p = geo.round_corners(path, radius_m=8.0, min_turn_deg=25.0,
                                  arc_step_m=max(1.0, step / 4.0))
            p = geo.densify(p, max_segment_m=max(2.0, min(25.0, step / 4.0)))
            cum = geo.cumulative(p)
            o = GenOptions(speed=speed, interval_ms=iv, accel=0.9,
                           noise_sigma_m=0.0, pace_variation=0.0,
                           lead_in_points=0, seed=7,
                           corner_step_turn_deg=75.0, corner_min_samples=1.0,
                           corner_a_lat_max=3.0, corner_v_floor=0.6)
            pts = generate(p, o)
            q = [(s["lat"], s["lon"]) for s in pts]
            sp = [s["speed"] for s in pts]

            def along_of(pt):
                bb, ba = 1e9, 0.0
                for k in range(1, len(p)):
                    d = geo._point_to_segment(pt, p[k-1], p[k])
                    if d < bb:
                        bb = d
                        seg = geo.haversine(p[k-1][0], p[k-1][1],
                                            p[k][0], p[k][1])
                        la = geo.haversine(pt[0], pt[1], p[k-1][0], p[k-1][1])
                        lb = geo.haversine(pt[0], pt[1], p[k][0], p[k][1])
                        tt = ((la*la - lb*lb + seg*seg) / (2*seg)) if seg > 1e-9 else 0
                        ba = cum[k-1] + max(0.0, min(seg, tt))
                return ba

            arc = math.radians(90.0) * radius
            A = [along_of(x) for x in q]
            inb = [i for i, a in enumerate(A) if 120.0 <= a <= 120.0 + arc]
            if not inb:
                inb = sorted(range(len(pts)),
                             key=lambda i: abs(A[i] - (120 + arc / 2)))[:2]
            results[radius] = min(sp[i] for i in inb)

        # 半径越大 -> 过弯速度越高（单调）
        for a, b in ((1.0, 3.0), (3.0, 8.0), (8.0, 30.0)):
            self.assertLess(results[a], results[b] + 1e-6,
                            f"半径 {a}m 的过弯速度应低于 {b}m："
                            f"{results[a]:.2f} vs {results[b]:.2f}")
        # 急弯要明显慢下来
        self.assertLess(results[1.0], speed * 0.4,
                        f"半径 1m 的急弯应当明显减速，实测 {results[1.0]:.2f} m/s")
        # 缓弯不该减速
        self.assertGreater(results[30.0], speed * 0.95,
                           f"半径 30m 的缓弯不该减速，实测 {results[30.0]:.2f} m/s")
        # 横向加速度不得超过设定上限（留 30% 余量给离散误差）
        for radius, v in results.items():
            self.assertLessEqual(v * v / radius, 3.0 * 1.3,
                                 f"半径 {radius}m：横向加速度 {v*v/radius:.2f} 超上限")

    def test_auto_round_radius_respects_cut_budget(self):
        """auto 模式：圆滑半径必须让切角落在预算内。

        用户提出「把两种方式都写入，根据情况自动选择」。关键点：
        **切角是离路上限修不了的**（实测半径 6m 时上限设 3m 却量到 3.72m），
        所以必须用上限的预算去约束圆滑半径，而不是靠事后夹取。
        """
        from mockroute import geo

        L = [(39.9000, 116.4000), (39.9000, 116.4020), (39.9020, 116.4020)]
        for budget in (0.2, 0.3, 0.5, 0.67, 1.0):
            r, c = geo.pick_round_radius(L, budget)
            self.assertGreater(r, 0, f"预算 {budget} 应当选出正的半径")
            # 用真实圆滑验证，不能超过预算
            rounded = geo.round_corners(L, radius_m=r)
            worst = max(
                min(geo._point_to_segment(q, L[i-1], L[i])
                    for i in range(1, len(L)))
                for q in rounded)
            self.assertLessEqual(worst, budget * 1.02,
                                 f"预算 {budget}m 但实测切角 {worst:.3f}m")
            self.assertAlmostEqual(c, worst, delta=0.05,
                                   msg="返回的预计切角应与实测一致")
        # 预算 0 -> 不圆滑
        self.assertEqual(geo.pick_round_radius(L, 0.0), (0.0, 0.0))

    def test_corner_vertices_extraction(self):
        """O(n) 拐点提取：只留转角 >= 阈值的顶点。"""
        from mockroute import geo
        # 一条直线：不该有拐点
        line = [(39.9 + i * 0.0001, 116.4) for i in range(50)]
        v = geo.corner_vertices(line, min_turn_deg=25.0)
        self.assertEqual(len(v), 2, f"直线应当只留首尾，实测 {len(v)}")
        # 直角弯：应当留下拐点
        L = [(39.900, 116.400), (39.900, 116.402), (39.902, 116.402)]
        v2 = geo.corner_vertices(L, min_turn_deg=25.0)
        self.assertEqual(len(v2), 3, f"直角弯应当留 3 点，实测 {len(v2)}")

    def test_shape_limits_use_ratio_not_absolute_speed(self):
        """回归：形状限速表必须存**比例**，不能存绝对速度。

        bug 经过：表按基础速度 3.3 构建成绝对值，而调用时传的是「带配速
        波动的目标速度」（2.9~3.7），于是高于 3.3 的波动被削平 ——
        连**直线路径**上的配速波动都被压掉（变异系数 0.054 -> 0.032），
        一个「直线配速波动」的测试因此失败。真人跑直线本来就有快慢。
        """
        from mockroute import geo, traj

        path = [(39.9075, 116.391)]
        lat, lon = path[0]
        for _ in range(80):
            lat, lon = geo.offset(lat, lon, 90.0, 25.0)
            path.append((lat, lon))
        # 限速表在直线上应当全是 1.0（不限制）
        step = 3.3
        p = geo.round_corners(list(path), radius_m=8.0, min_turn_deg=25.0,
                              arc_step_m=max(1.0, step / 4.0))
        p = geo.densify(p, max_segment_m=max(2.0, min(25.0, step / 4.0)))
        cum = geo.cumulative(p)
        lim = traj.build_shape_limits(p, cum, 1.5, 0.6, 3.3, step_m=2.0)
        self.assertTrue(all(abs(x - 1.0) < 1e-9 for x in lim),
                        "直线路径的形状限速应当全是 1.0（比例表）")
        # 配速波动必须仍然可见
        pts = traj.generate(path, traj.GenOptions(
            speed=3.3, interval_ms=1000, accel=0.9, noise_sigma_m=0.0,
            speed_jitter=0.0, pace_variation=0.12, pace_period_m=180.0,
            seed=11))
        lo, hi = int(len(pts) * 0.4), int(len(pts) * 0.8)
        mv = [x["speed"] for x in pts[lo:hi] if x["speed"] > 0.5]
        m = sum(mv) / len(mv)
        sd = (sum((x - m) ** 2 for x in mv) / len(mv)) ** 0.5
        self.assertGreater(sd / m, 0.04,
                           "直线上的配速波动被形状限速削平了")

    def test_generate_basic_invariants(self):
        path = self.path()
        opts = traj.GenOptions(speed=1.4, interval_ms=1000, seed=42)
        pts = traj.generate(path, opts)
        self.assertGreater(len(pts), 10)

        # t 必须单调不减、且步长基本等于 interval
        ts = [p["t"] for p in pts]
        self.assertEqual(ts, sorted(ts))
        self.assertEqual(ts[0], 0)
        steps = {ts[i + 1] - ts[i] for i in range(len(ts) - 1)}
        self.assertEqual(steps, {1000})

        # 覆盖率：走到路径终点附近（容差 = 一个采样间隔的距离）
        last = pts[-1]
        self.assertAlmostEqual(
            geo.haversine(last["lat"], last["lon"], *path[-1]),
            0.0, delta=2.0,
        )

        # 所有字段齐全且类型正确
        for p in pts:
            for k in ("t", "lon", "lat", "speed", "bearing", "acc", "alt"):
                self.assertIn(k, p)
            self.assertLessEqual(abs(p["lat"]), 90)
            self.assertLessEqual(abs(p["lon"]), 180)
            self.assertGreaterEqual(p["speed"], 0.0)
            self.assertGreaterEqual(p["acc"], 0.0)

    def test_speed_profile_respects_accel(self):
        path = self.path(n=40, step_m=25.0)  # 1 km
        opts = traj.GenOptions(speed=5.0, interval_ms=1000, accel=0.5,
                               speed_jitter=0.0, lead_in_points=0, seed=1)
        pts = traj.generate(path, opts)
        speeds = [p["speed"] for p in pts]

        # 1) 每一步的速度变化不超过 accel*dt。最后一步例外：它的 speed 是
        #    「该采样区间的平均速度」，若车正好在区间中途停下，这个值会低于
        #    区间起点的瞬时速度 —— 这是有意为之，因为它必须与坐标位移自洽
        #    （目标 App 就是这么反推速度的）。
        for i in range(1, len(speeds) - 1):
            self.assertLessEqual(abs(speeds[i] - speeds[i - 1]), 0.5 * 1.01 + 1e-6,
                                 f"第 {i} 步速度跳变过大")

        # 2) 从静止起步
        self.assertLessEqual(speeds[0], 0.5)

        # 3) 能加速到目标速度附近
        self.assertGreater(max(speeds), 5.0 * 0.85)

        # 4) 末段必须单调减速，且明显低于巡航速度。
        #
        #    注意 a_max 决定了减速必须提前多久开始：以 0.5 m/s² 从 5 m/s 降到 0
        #    需要 10 秒 / 50 米。若减速判据触发时剩余距离不足，末段就只能以允许
        #    的最大减速度收尾 —— 这是 accel 参数的物理后果，不是实现缺陷。
        tail = speeds[-5:]
        self.assertEqual(tail, sorted(tail, reverse=True), "末段速度出现回升")
        self.assertLess(tail[-1], speeds[-6])

        # 5) 末点必须落在终点
        last = pts[-1]
        self.assertAlmostEqual(
            geo.haversine(last["lat"], last["lon"], *path[-1]), 0.0, delta=0.5)

    def test_no_decel_flag_keeps_speed(self):
        path = self.path(n=10, step_m=25.0)
        opts = traj.GenOptions(speed=3.0, accel=1.0, speed_jitter=0.0,
                               decel_at_end=False, seed=2)
        pts = traj.generate(path, opts)
        self.assertGreater(pts[-1]["speed"], 2.0)

    def test_average_speed_near_target(self):
        path = self.path(n=60, step_m=25.0)  # 1.5 km
        opts = traj.GenOptions(speed=1.4, interval_ms=1000, accel=0.6,
                               speed_jitter=0.0, lead_in_points=0, seed=3)
        pts = traj.generate(path, opts)
        moving = [p["speed"] for p in pts if p["speed"] > 0.1]
        avg = sum(moving) / len(moving)
        self.assertAlmostEqual(avg, 1.4, delta=0.25)

    def test_bearing_matches_displacement(self):
        path = self.path(n=20, step_m=25.0)
        opts = traj.GenOptions(speed=4.0, interval_ms=1000, bearing_jitter=0.0, seed=4)
        pts = traj.generate(path, opts)
        for i in range(6, len(pts) - 2):  # 跳过起步阶段
            a, b = pts[i], pts[i + 1]
            if geo.haversine(a["lat"], a["lon"], b["lat"], b["lon"]) < 1.0:
                continue
            real = geo.bearing(a["lat"], a["lon"], b["lat"], b["lon"])
            diff = abs((real - b["bearing"] + 180) % 360 - 180)
            self.assertLess(diff, 3.0, f"第 {i} 点方位角与实际位移不符: {diff:.1f}°")

    def test_lead_in_points_present(self):
        path = self.path(n=5, step_m=25.0)
        opts = traj.GenOptions(speed=1.0, lead_in_points=4, lead_in_radius_m=10.0, seed=5)
        pts = traj.generate(path, opts)
        lead = pts[:4]
        # 漂移阶段是原地不动：速度必须是 0，位置在起始点一个小半径内
        for p in lead:
            self.assertEqual(p["speed"], 0.0)
            self.assertLess(geo.haversine(p["lat"], p["lon"], *path[0]), 15.0)
        # 时间戳应当正好每 interval 一个，且第 4 个点对应 t=3000
        self.assertEqual([p["t"] for p in lead], [0, 1000, 2000, 3000])
        # 第 5 个点（主运动第一点）从静止起步
        self.assertLess(pts[4]["speed"], 0.5)

    def test_reproducible_with_seed(self):
        path = self.path(n=10, step_m=25.0)
        a = traj.generate(path, traj.GenOptions(seed=7, speed_jitter=0.1))
        b = traj.generate(path, traj.GenOptions(seed=7, speed_jitter=0.1))
        self.assertEqual(a, b)

    def test_rejects_degenerate_path(self):
        with self.assertRaises(ValueError):
            traj.generate([(39.9, 116.4)], traj.GenOptions())
        with self.assertRaises(ValueError):
            traj.generate([(39.9, 116.4), (39.9, 116.4)], traj.GenOptions())
        with self.assertRaises(ValueError):
            traj.generate(self.path(), traj.GenOptions(interval_ms=0))

    def test_start_offset_skips_distance(self):
        path = self.path(n=40, step_m=25.0)
        full = traj.generate(path, traj.GenOptions(speed=2.0, seed=9, lead_in_points=0))
        part = traj.generate(path, traj.GenOptions(speed=2.0, seed=9, lead_in_points=0),
                             start_offset_m=300.0)
        d_full = geo.haversine(full[0]["lat"], full[0]["lon"], *path[0])
        d_part = geo.haversine(part[0]["lat"], part[0]["lon"], *path[0])
        self.assertLess(d_full, 5.0)
        self.assertAlmostEqual(d_part, 300.0, delta=10.0)

    def test_coord_jitter_bounded(self):
        path = self.path(n=20, step_m=25.0)
        clean = traj.generate(path, traj.GenOptions(speed=2.0, seed=11, lead_in_points=0))
        jit = traj.generate(path, traj.GenOptions(speed=2.0, seed=11, lead_in_points=0,
                                                  coord_jitter_m=5.0))
        self.assertEqual(len(clean), len(jit))
        for a, b in zip(clean[1:], jit[1:]):
            self.assertLess(geo.haversine(a["lat"], a["lon"], b["lat"], b["lon"]), 30.0)

    def test_lead_in_acc_higher(self):
        """起步阶段（刚锁星）上报精度应当明显更差。

        注意 accuracy 是随机采样（Weibull 长尾），单个点可能低于均值，
        而且分组均值本身也有抽样波动 —— 所以这里不比较两次独立采样的均值
        （那会随 RNG 序列变化而变脆），而是验证**结构性**关系：
          * 起步段的每个点都被乘上了 lead_in_acc_factor；
          * 用固定 seed 重现时该倍数必须可复现。
        做法：同样的 seed 跑两遍，一遍开启 lead-in 精度惩罚、一遍关闭，
        逐点比较比值 —— 这样把随机采样的影响完全消掉了。
        """
        path = self.path(n=40, step_m=25.0)
        common = dict(speed=1.0, interval_ms=500, lead_in_points=8,
                      acc_mean_m=6.0, seed=13)
        with_penalty = traj.generate(path, traj.GenOptions(**common,
                                                           lead_in_acc_factor=1.6))
        no_penalty = traj.generate(path, traj.GenOptions(**common,
                                                         lead_in_acc_factor=1.0))
        self.assertEqual(len(with_penalty), len(no_penalty))

        # 起步段的精度被放大（同一个随机样本 × 1.6）
        for a, b in zip(with_penalty[:8], no_penalty[:8]):
            self.assertAlmostEqual(a["acc"], b["acc"] * 1.6, delta=0.15,
                                   msg="起步段精度没有按 lead_in_acc_factor 放大")
        # 主运动段不受影响
        for a, b in zip(with_penalty[8:], no_penalty[8:]):
            self.assertAlmostEqual(a["acc"], b["acc"], delta=1e-6)
        # 整体量级合理（Weibull 均值 6m）
        body = [p["acc"] for p in with_penalty[12:]]
        self.assertLess(sum(body) / len(body), 12.0)

    def test_timeline_matches_distance(self):
        """时间轴必须与位移一致：每一步的位移 ≈ 该点上报的速度 × 采样间隔。

        这是**无噪声时**的严格不变量，也是最容易被目标 App 识破的地方 ——
        如果坐标每 1 秒只挪 0.5 米，但 speed 字段写着 5 m/s，就是假的。

        注意：开启 GPS 噪声后（见 test_noise_speed_stays_plausible），逐点严格
        一致在数学上不可能 —— 噪声与每步位移同一量级时，位移的期望会被系统性
        抬高（莱斯分布）。那部分由另一个测试用「中位偏差 + 无离群」把关。
        """
        for speed, interval in ((1.4, 1000), (5.0, 1000), (13.9, 500), (1.0, 2000)):
            with self.subTest(speed=speed, interval=interval):
                path = self.path(n=40, step_m=25.0)
                opts = traj.GenOptions(speed=speed, interval_ms=interval, accel=0.6,
                                       speed_jitter=0.0, lead_in_points=0, seed=21)
                pts = traj.generate(path, opts)
                dt = interval / 1000.0
                for i in range(1, len(pts)):
                    d = geo.haversine(pts[i - 1]["lat"], pts[i - 1]["lon"],
                                      pts[i]["lat"], pts[i]["lon"])
                    v = pts[i]["speed"]
                    # 最后一个点是刹停点：它报的是区间平均速度，位移会小于
                    # v·dt —— 这正是真实 GPS 的行为，所以只校验非刹停步。
                    if v < 0.5:
                        continue
                    self.assertAlmostEqual(d, v * dt, delta=0.2 * v * dt + 1.0,
                                           msg=f"第 {i} 点位移 {d:.2f}m 与速度 {v:.2f}m/s 不符")

    def test_gps_noise_is_time_correlated(self):
        """真实 GPS 误差是慢漂移，不是白噪声：相邻误差必须高度相关。

        这是"更真实的噪声"与"更平滑的轨迹"能同时成立的关键 ——
        如果用独立白噪声，相邻点各自乱跳，轨迹反而呈锯齿状。
        """
        rng = random.Random(7)
        noise = traj.GpsNoise(rng, sigma_m=3.0, alpha=0.95)
        xs = [noise.step(rng)[0] for _ in range(400)]
        ys = [noise.step(rng)[1] for _ in range(400)]

        # 平稳标准差应接近目标值
        def sd(v):
            m = sum(v) / len(v)
            return (sum((x - m) ** 2 for x in v) / len(v)) ** 0.5

        self.assertAlmostEqual(sd(xs), 3.0, delta=1.0)
        self.assertAlmostEqual(sd(ys), 3.0, delta=1.0)

        # 一阶自相关：alpha=0.95 时理论值约 0.95
        def autocorr(v):
            m = sum(v) / len(v)
            num = sum((v[i] - m) * (v[i - 1] - m) for i in range(1, len(v)))
            den = sum((x - m) ** 2 for x in v)
            return num / den

        self.assertGreater(autocorr(xs), 0.85, "误差自相关太低，说明退化成了白噪声")
        self.assertGreater(autocorr(ys), 0.85)

    def test_gps_noise_zero_disables(self):
        rng = random.Random(1)
        noise = traj.GpsNoise(rng, sigma_m=0.0)
        self.assertEqual(noise.step(rng), (0.0, 0.0))

    def test_sample_accuracy_distribution(self):
        rng = random.Random(3)
        vals = [traj.sample_accuracy(rng, 6.0) for _ in range(4000)]
        mean = sum(vals) / len(vals)
        self.assertAlmostEqual(mean, 6.0, delta=0.5)
        self.assertGreater(min(vals), 0.0)
        # Weibull(k=2) 有长尾：最大值应明显大于均值
        self.assertGreater(max(vals), 12.0)

    def test_noise_keeps_position_near_path(self):
        """噪声量级应当受控：所有点都落在路径附近 sigma 量级内。"""
        path = self.path(n=20, step_m=25.0)
        opts = traj.GenOptions(speed=1.35, interval_ms=500, accel=0.22,
                               noise_sigma_m=3.0, seed=11, lead_in_points=0)
        pts = traj.generate(path, opts)
        cum = geo.cumulative(path)
        for p in pts:
            # 到路径的最短距离（用较粗的采样近似）
            dmin = min(geo.haversine(p["lat"], p["lon"], *q) for q in path)
            self.assertLess(dmin, 25.0, "噪声把点甩离路径太远")

    def test_noise_speed_stays_plausible(self):
        """加噪声后，上报速度必须仍然是「人走路」的量级。

        步行 1.35 m/s 而 GPS 误差 2.5 m：相邻两点的位移误差与真实位移同一量级，
        直接差分会把速度系统性抬高（莱斯分布，|v_true + noise| 的期望大于 v_true）。
        默认 alpha=0.99 让误差在几十秒内近似恒定，正是为了把这个偏差压住。
        这里不做「速度必须等于真值」的强断言 —— 真实的 GPS 速度读数本来就有偏，
        强求一致反而会掩盖问题；只保证它落在合理范围内、没有荒谬尖峰。
        """
        path = self.path(n=40, step_m=25.0)
        opts = traj.GenOptions(speed=1.35, interval_ms=500, accel=0.22,
                               noise_sigma_m=2.0, seed=5)
        pts = traj.generate(path, opts)
        speeds = [p["speed"] for p in pts]

        self.assertLess(max(speeds), 5.0, "出现不合理的速度尖峰")
        mean = sum(speeds) / len(speeds)
        self.assertGreater(mean, 0.8)
        self.assertLess(mean, 2.4, f"噪声把平均速度抬得过高: {mean:.2f}")

        # speed 与坐标的偏差不应达到「明显造假」的程度。
        #
        # 这里只断言**中位数**偏差：GPS 噪声与每步位移同一量级时（步行 1.35 m/s
        # 走 0.68m，噪声 2.5m），逐点强求一致在数学上不可能 —— 真实的 GPS 速度
        # 读数同样有散布。我们只要保证没有系统性偏差（例如恒速 8 却只走 4m）。
        skip = 3 + 1     # 默认 lead_in_points=3，再加主运动第一个点
        devs = []
        for i in range(skip, len(pts)):
            d = geo.haversine(pts[i - 1]["lat"], pts[i - 1]["lon"],
                              pts[i]["lat"], pts[i]["lon"])
            dt = (pts[i]["t"] - pts[i - 1]["t"]) / 1000.0
            expect = pts[i]["speed"] * dt
            if expect > 0.2:
                devs.append(abs(d - expect) / expect)
        devs.sort()
        median = devs[len(devs) // 2]
        self.assertLess(median, 0.5, f"speed 与位移的中位偏差过大: {median*100:.0f}%")
        # 也不能出现极端离群（>5 倍）
        self.assertLess(max(devs), 5.0)

    def test_pace_variation_shape(self):
        """配速波动必须是平滑的长周期曲线，不能是每步乱跳的噪声。

        跑步和步行最大的区别之一就是配速在变。用正弦叠加而不是随机游走，
        是为了保证曲线光滑（跑者不会瞬间改变配速）。
        """
        rng = random.Random(3)
        pv = traj.PaceVariation(rng, amplitude=0.12, period_m=180.0)
        vals = [pv.at(s) for s in range(0, 6000, 25)]

        # 均值应当接近 1.0（只波动，不改变平均配速）
        self.assertAlmostEqual(sum(vals) / len(vals), 1.0, delta=0.06)
        # 幅度受 amplitude 约束
        self.assertLessEqual(max(vals), 1.0 + 0.12 * 1.01)
        self.assertGreaterEqual(min(vals), 1.0 - 0.12 * 1.01)

        # 平滑性用**物理判据**而不是拍脑袋的阈值：
        # 配速变化率受加速度上限约束，即 dv/ds ≤ a/v。
        # 以 a=1.2 m/s²、v=3.3 m/s 计约 0.36 /m，再取 1/4 作为余量。
        v_nominal, a_max = 3.3, 1.2
        max_rate = a_max / v_nominal * 0.25
        for i in range(1, len(vals)):
            rate = abs(vals[i] - vals[i - 1]) / 25.0
            self.assertLess(rate, max_rate,
                            f"配速变化率 {rate:.3f}/m 超过物理上限 {max_rate:.3f}/m")

        # 最细谐波波长必须有下限：跑者在几十米内改变不了配速。
        # 此前用 period/5.7（180m 时只有 32m）会生成物理上不可能的快速抖动。
        self.assertGreaterEqual(min(pv.w1, pv.w2, pv.w3), 120.0)
        # 关闭时恒为 1.0
        self.assertEqual(traj.PaceVariation(rng, 0.0).at(123.0), 1.0)

    def test_elevation_is_smooth_and_realistic(self):
        """海拔剖面必须连续，坡度落在真实范围内。"""
        rng = random.Random(5)
        # 用 5 公里路线，对应城市跑步的典型场景
        e = traj.Elevation(rng, 5000.0, base_m=45.0, amplitude_m=8.0)

        alts = [e.at(s) for s in range(0, 5000, 5)]
        self.assertGreater(max(alts) - min(alts), 4.0, "地形起伏太小，看不出坡")

        # 坡度：城市跑步中位应在 5% 以内，极端不超过 20%
        grades = sorted(abs(e.grade(s)) for s in range(0, 5000, 10))
        median = grades[len(grades) // 2]
        self.assertLess(median, 0.05, f"坡度中位过大: {median*100:.1f}%")
        self.assertLess(max(grades), 0.20, f"出现异常陡坡: {max(grades)*100:.1f}%")

        # 累积爬升应当在合理量级（城市跑步 5~40 m/km）
        gain = sum(max(0.0, e.at(s + 10) - e.at(s)) for s in range(0, 4990, 10))
        per_km = gain / 5.0
        self.assertGreater(per_km, 3.0)
        self.assertLess(per_km, 45.0, f"每公里爬升过大: {per_km:.0f}m/km")

        # 关闭地形时恒定为基准海拔
        flat = traj.Elevation(rng, 5000.0, base_m=45.0, amplitude_m=0.0)
        self.assertEqual(flat.at(1234.0), 45.0)
        self.assertEqual(flat.grade(1234.0), 0.0)

    def test_grade_effect_slows_uphill(self):
        """上坡必须变慢、下坡略快，且倍率在合理范围。"""
        rng = random.Random(9)
        e = traj.Elevation(rng, 3000.0, base_m=45.0, amplitude_m=8.0)
        factors = [e.pace_factor(s) for s in range(0, 3000, 10)]
        self.assertLessEqual(max(factors), 1.35)
        self.assertGreaterEqual(min(factors), 0.55)
        # 至少要有明显变慢的地方（否则地形没起作用）
        self.assertLess(min(factors), 0.92, "上坡没有变慢")
        # 上坡的惩罚应当大于下坡的收益（跑者真实体验）
        uphill = [f for f in factors if f < 1.0]
        self.assertGreater(len(uphill), 0)

    def test_run_preset_applies_and_differs_from_walk(self):
        """run 预设必须覆盖 walk 基础层（实测曾因 hasattr 判断写错而失效）。"""
        path = self.path(n=40, step_m=25.0)
        run = traj.generate(path, traj.GenOptions(
            speed=3.3, interval_ms=1000, accel=0.9, noise_sigma_m=1.8,
            pace_variation=0.12, pace_period_m=180.0, grade_effect=True,
            alt_base_m=45.0, alt_amplitude_m=8.0, seed=42))
        walk = traj.generate(path, traj.GenOptions(
            speed=1.35, interval_ms=500, accel=0.22, noise_sigma_m=2.0,
            pace_variation=0.05, pace_period_m=240.0, seed=42))

        run_sp = [p["speed"] for p in run if p["speed"] > 0.5]
        walk_sp = [p["speed"] for p in walk if p["speed"] > 0.5]
        self.assertGreater(sum(run_sp) / len(run_sp), 2.5, "跑步预设速度过低")
        self.assertLess(sum(walk_sp) / len(walk_sp), 2.0, "步行预设速度过高")
        # 跑步的点更少（同样的路走得更快）
        self.assertLess(len(run), len(walk))

        # 跑步应当有地形起伏，步行预设不生成地形（振幅 0）
        # 注意不能直接比 alt 的范围：即使振幅为 0，水平噪声仍会让 alt 为常量
        # 基准值，范围应接近 0。
        self.assertGreater(max(p["alt"] for p in run) - min(p["alt"] for p in run), 3.0)
        self.assertLess(max(p["alt"] for p in walk) - min(p["alt"] for p in walk), 0.5,
                        "步行预设不该有地形起伏")

    def test_run_pace_variation_is_visible(self):
        """跑步的配速波动应当明显大于步行（这是跑步的核心特征）。

        注意要把「起步加速 / 末段减速」排除掉，否则 CV 里混进了加减速的贡献 ——
        只取中间 40%~80% 的路段，那里已经在巡航。
        """
        path = self.path(n=80, step_m=25.0)   # 2 km

        def cruise_cv(pace_amp):
            pts = traj.generate(path, traj.GenOptions(
                speed=3.3, interval_ms=1000, accel=0.9, noise_sigma_m=0.0,
                speed_jitter=0.0, pace_variation=pace_amp, pace_period_m=180.0,
                seed=11))
            lo, hi = int(len(pts) * 0.4), int(len(pts) * 0.8)
            mv = [p["speed"] for p in pts[lo:hi] if p["speed"] > 0.5]
            m = sum(mv) / len(mv)
            sd = (sum((x - m) ** 2 for x in mv) / len(mv)) ** 0.5
            return sd / m

        cv_flat = cruise_cv(0.0)
        cv_run = cruise_cv(0.12)
        cv_interval = cruise_cv(0.38)
        self.assertLess(cv_flat, 0.01, "无配速波动时巡航段应当很平")
        self.assertGreater(cv_run, 0.04, "跑步配速波动不明显")
        self.assertGreater(cv_interval, cv_run, "间歇训练的波动应大于匀速跑")
        self.assertGreater(cv_run, cv_flat * 3)

    def test_validate_detects_problems(self):
        self.assertIn("points 缺失或为空", traj.validate({"points": []})[0])
        bad = {"points": [{"t": 1000, "lat": 39.9, "lon": 116.4},
                          {"t": 500, "lat": 39.9, "lon": 116.4}]}
        self.assertTrue(any("单调" in p for p in traj.validate(bad)))
        bad2 = {"points": [{"t": 0, "lat": 200.0, "lon": 116.4}]}
        self.assertTrue(any("纬度越界" in p for p in traj.validate(bad2)))
        bad3 = {"points": [{"t": 0, "lat": 39.9, "lon": 116.4, "speed": 999}]}
        self.assertTrue(any("速度" in p for p in traj.validate(bad3)))
        good = traj.build_route(
            traj.generate(self.path(n=3, step_m=25.0), traj.GenOptions(seed=1))
        )
        self.assertEqual(traj.validate(good), [])

    def test_write_and_reload_route(self):
        path = self.path(n=10, step_m=25.0)
        route = traj.build_route(traj.generate(path, traj.GenOptions(seed=1)),
                                 mode="loop", interval_ms=1000, source="test")
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "r.json"
            traj.write_route(route, out)
            self.assertTrue(out.exists())
            back = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(back["mode"], "loop")
            self.assertEqual(len(back["points"]), len(route["points"]))

    def test_write_route_rejects_invalid(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                traj.write_route({"points": []}, Path(td) / "x.json")

    def test_summarise_runs(self):
        route = traj.build_route(
            traj.generate(self.path(n=5, step_m=25.0), traj.GenOptions(seed=1)),
            source="unit-test",
        )
        text = traj.summarise(route)
        self.assertIn("点数=", text)
        self.assertIn("unit-test", text)


# --------------------------------------------------------------------------
# route
# --------------------------------------------------------------------------

class TestRoute(unittest.TestCase):
    def test_from_file_plain_text(self):
        res = from_file(EXAMPLES / "path-demo.txt")
        self.assertGreaterEqual(len(res.points), 10)
        self.assertGreater(res.distance_m, 100)
        self.assertIn("plain", res.notes)

    def test_from_file_geojson(self):
        res = from_file(EXAMPLES / "route-loop.geojson")
        self.assertEqual(len(res.points), 5)
        # GeoJSON 是 [lon, lat]，容易搞反，这里专门验证
        self.assertAlmostEqual(res.points[0][0], 39.906, places=4)
        self.assertAlmostEqual(res.points[0][1], 116.391, places=4)

    def test_from_file_gpx(self):
        res = from_file(EXAMPLES / "path-demo.gpx")
        self.assertEqual(len(res.points), 7)
        self.assertAlmostEqual(res.points[0][0], 39.9075, places=5)

    def test_from_file_plain_swapped_columns(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "swapped.txt"
            f.write_text("116.4,39.9\n116.401,39.901\n", encoding="utf-8")
            res = from_file(f)
            self.assertAlmostEqual(res.points[0][0], 39.9, places=6)
            self.assertAlmostEqual(res.points[0][1], 116.4, places=6)

    def test_from_file_missing(self):
        with self.assertRaises(RouteError):
            from_file(EXAMPLES / "does-not-exist.txt")

    def test_from_file_too_short(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "one.txt"
            f.write_text("39.9,116.4\n", encoding="utf-8")
            with self.assertRaises(RouteError):
                from_file(f)

    def test_file_provider_via_resolve(self):
        opts = ResolveOptions(provider="file", extra={"file": str(EXAMPLES / "path-demo.txt")})
        res = resolve((39.9, 116.4), (39.91, 116.41), [], opts)
        self.assertGreaterEqual(len(res.points), 10)

    def test_file_provider_requires_file(self):
        with self.assertRaises(RouteError):
            resolve((39.9, 116.4), (39.91, 116.41), [], ResolveOptions(provider="file"))

    def test_unknown_provider(self):
        with self.assertRaises(RouteError):
            resolve((39.9, 116.4), (39.91, 116.41), [], ResolveOptions(provider="baidu"))

    def test_amap_requires_key(self):
        with self.assertRaises(RouteError):
            resolve((39.9, 116.4), (39.91, 116.41), [], ResolveOptions(provider="amap"))

    def test_google_requires_key(self):
        with self.assertRaises(RouteError):
            resolve((39.9, 116.4), (39.91, 116.41), [], ResolveOptions(provider="google"))

    def test_osrm_parses_response(self):
        """用假 HTTP 响应验证 polyline 解码与字段取值，不联网。"""
        sample = {
            "code": "Ok",
            "routes": [{
                "distance": 1234.5,
                "duration": 300.0,
                "geometry": "_p~iF~ps|U_ulLnnqC_mqNvxq`@",
            }],
        }
        import mockroute.route as R
        original = R._http_get
        R._http_get = lambda url, timeout=20.0: json.dumps(sample).encode()
        try:
            res = osrm("https://example.invalid", [(39.9, 116.4), (39.91, 116.41)])
        finally:
            R._http_get = original
        self.assertEqual(len(res.points), 3)
        self.assertAlmostEqual(res.distance_m, 1234.5)
        self.assertAlmostEqual(res.duration_s, 300.0)
        self.assertIn("osrm", res.provider)

    def test_osrm_rejects_error_code(self):
        import mockroute.route as R
        original = R._http_get
        R._http_get = lambda url, timeout=20.0: json.dumps(
            {"code": "NoRoute", "message": "no route"}
        ).encode()
        try:
            with self.assertRaises(RouteError):
                osrm("https://example.invalid", [(39.9, 116.4), (39.91, 116.41)])
        finally:
            R._http_get = original

    def test_osrm_needs_two_points(self):
        with self.assertRaises(RouteError):
            osrm("https://example.invalid", [(39.9, 116.4)])


# --------------------------------------------------------------------------
# protocol / player
# --------------------------------------------------------------------------

class TestProtocol(unittest.TestCase):
    def test_find_adb_missing_raises_helpfully(self):
        with self.assertRaises(protocol.AdbError) as ctx:
            protocol.find_adb("C:/definitely/not/here/adb.exe")
        self.assertIn("adb", str(ctx.exception))

    def test_broadcast_args_types(self):
        adb = Adb(exe="adb")
        p = Player(adb)
        args = p._broadcast_args(protocol.ACTION_START_ROUTE,
                                 {"file": "/sdcard/x.json", "mode": "loop"})
        self.assertEqual(args[:5], ["shell", "am", "broadcast", "-a",
                                    protocol.ACTION_START_ROUTE])
        self.assertIn("-n", args)
        self.assertEqual(args[args.index("-n") + 1], protocol.RECEIVER)
        self.assertIn("--es", args)
        self.assertEqual(args[args.index("--es") + 1], "file")
        # file / mode 都是字符串 extra
        self.assertEqual(args.count("--es"), 2)

    def test_broadcast_args_float_and_long(self):
        adb = Adb(exe="adb")
        p = Player(adb)
        args = p._broadcast_args(protocol.ACTION_START_CIRCLE,
                                 {"lat": 39.9, "lon": 116.4,
                                  "radius": 150.0, "period": 60000})
        self.assertEqual(args.count("--ef"), 3)
        self.assertEqual(args.count("--el"), 1)
        self.assertEqual(args[args.index("--el") + 1], "period")

    def test_broadcast_cmd_shell_quotes(self):
        adb = Adb(exe="adb")
        p = Player(adb)
        cmd = p._broadcast_cmd(protocol.ACTION_SET, {"lat": 39.9, "lon": 116.4})
        self.assertNotIn("'--ef'", cmd)          # 开关不该被引号包住
        self.assertIn("--ef", cmd)
        self.assertIn("lat", cmd)
        self.assertIn("39.9", cmd)

    def test_serial_is_placed_before_subcommand(self):
        adb = Adb(exe="adb", serial="ABC123")
        self.assertEqual(adb.base(), ["adb", "-s", "ABC123"])

    def test_constants_match_android_source(self):
        """PC 侧常量必须与 Kotlin 源码里的字面量一致。"""
        kt = (ROOT / "mockroute-android/app/src/main/java/com/mockroute/Protocol.kt")
        if not kt.exists():
            self.skipTest("Android 源码不在预期位置")
        text = kt.read_text(encoding="utf-8")
        for action in (protocol.ACTION_SET, protocol.ACTION_START,
                       protocol.ACTION_START_ROUTE, protocol.ACTION_START_CIRCLE,
                       protocol.ACTION_PAUSE, protocol.ACTION_RESUME,
                       protocol.ACTION_STOP, protocol.ACTION_STATUS,
                       protocol.ACTION_GOTO, protocol.ACTION_SPEED):
            self.assertIn(f'"{action}"', text, f"{action} 在 Kotlin 里找不到")
        for extra in ("lat", "lon", "file", "mode", "mult", "radius", "period",
                      "interval", "tag", "acc", "alt", "bearing"):
            self.assertIn(f'"{extra}"', text, f"extra {extra} 在 Kotlin 里找不到")
        # 开关状态的键与取值也必须两边一致（界面靠它显示运行/暂停/关闭）
        for const in (protocol.KEY_STATUS, protocol.KEY_UPDATED,
                      protocol.KEY_STATE, protocol.STATE_OFF,
                      protocol.STATE_ON, protocol.STATE_PAUSED,
                      protocol.PREF):
            self.assertIn(f'"{const}"', text, f"常量 {const} 在 Kotlin 里找不到")

    def test_panel_does_not_parse_status_text(self):
        """回归：面板不能靠解析 KEY_STATUS 的**文案**判断开关状态。

        真实 bug：MainActivity 曾经用 `status.contains("playing=true")` 判断，
        但那个字符串只有 ACTION_STATUS 才会写 —— 单点写「单点: …」、
        转圈写「圆圈: …」、前往写「前往 …」，于是虚拟定位明明开着，
        顶部状态条却一直显示「已关闭」。

        文案是给人看的，程序不该解析它。必须走 KEY_STATE。
        """
        ma = (ROOT / "mockroute-android/app/src/main/java/com/mockroute/MainActivity.kt")
        if not ma.exists():
            self.skipTest("Android 源码不在预期位置")
        text = ma.read_text(encoding="utf-8")
        self.assertNotIn('contains("playing=true")', text,
                         "不该解析状态文案，应读 Protocol.KEY_STATE")
        self.assertNotIn("contains(\"paused=true\")", text)
        self.assertIn("Status.getState", text, "面板必须从 KEY_STATE 读状态")

    def test_service_writes_state_on_every_transition(self):
        """服务的每条状态变化路径都要写 KEY_STATE，否则面板会显示过期状态。"""
        svc = (ROOT / "mockroute-android/app/src/main/java/com/mockroute/"
                      "MockLocationService.kt")
        if not svc.exists():
            self.skipTest("Android 源码不在预期位置")
        text = svc.read_text(encoding="utf-8")
        for state in ("STATE_ON", "STATE_PAUSED", "STATE_OFF"):
            self.assertIn(f"Status.setState(this, Protocol.{state})", text,
                          f"服务从未写入 {state}")

        # 每个会开启模拟的分支后面都必须跟一次 STATE_ON
        for marker in ('Status.set(this, "单点:',
                       'Status.set(this, "圆圈:',
                       'Status.set(this, "前往 %'):
            i = text.find(marker)
            self.assertGreater(i, 0, f"找不到 {marker}")
            seg = text[i:i + 400]
            self.assertIn("Status.setState", seg,
                          f"{marker} 之后没有写 STATE_ON，面板会显示已关闭")

    def test_package_name_matches_android(self):
        gradle = (ROOT / "mockroute-android/app/build.gradle.kts")
        if not gradle.exists():
            self.skipTest("Android 工程不在预期位置")
        text = gradle.read_text(encoding="utf-8")
        self.assertIn(f'applicationId = "{protocol.APP_ID}"', text)


# --------------------------------------------------------------------------
# CLI 端到端
# --------------------------------------------------------------------------

class TestLocations(unittest.TestCase):
    """起点/终点的多种指定方式（命名地点、地名解析、坐标字面量）。"""

    def setUp(self):
        from mockroute import locations
        self.loc = locations
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_coord_literal_forms(self):
        for s, want in (("39.9,116.4", (39.9, 116.4)),
                        ("39.9 116.4", (39.9, 116.4)),
                        ("39.9/116.4", (39.9, 116.4)),
                        ("39.9087, 116.3975", (39.9087, 116.3975)),
                        # 写成「经度,纬度」时自动换序
                        ("116.3975,39.9087", (39.9087, 116.3975))):
            self.assertEqual(self.loc.parse_coord_literal(s), want, s)
        for bad in ("", "天安门", "39.9", "abc,def", "999,999"):
            self.assertIsNone(self.loc.parse_coord_literal(bad), bad)

    def test_alias_roundtrip(self):
        self.assertEqual(self.loc.load_aliases(self.root), {})
        self.loc.save_alias(self.root, "家", 39.9087, 116.3975, note="测试")
        a = self.loc.load_aliases(self.root)
        self.assertIn("家", a)
        self.assertAlmostEqual(a["家"]["lat"], 39.9087)
        self.assertAlmostEqual(a["家"]["lon"], 116.3975)
        self.assertEqual(a["家"]["note"], "测试")
        # 文件是 UTF-8 JSON，中文名不该丢
        raw = self.loc.alias_path(self.root).read_text(encoding="utf-8")
        self.assertIn("家", raw)
        self.assertTrue(self.loc.delete_alias(self.root, "家"))
        self.assertFalse(self.loc.delete_alias(self.root, "家"))
        self.assertEqual(self.loc.load_aliases(self.root), {})

    def test_resolve_prefers_coords_then_alias(self):
        # 名字只是标签，测试不关心它对应哪个真实地点；
        # 但**别用真地名配假坐标** —— 曾经的教训：把 39.9087,116.3975
        # 标成「天安门」，实际是皇史宬（差 552 米），这个坏假设被沿用了很久。
        self.loc.save_alias(self.root, "测试点A", 39.9087, 116.3975)
        opts = self.loc.ResolveOptions(aliases=self.loc.load_aliases(self.root),
                                       allow_geocode=False)
        # 坐标字面量
        lat, lon, p = self.loc.resolve_location("39.9,116.4", opts)
        self.assertAlmostEqual(lat, 39.9)
        self.assertEqual(p.provider, "coords")
        # 命名地点
        lat, lon, p = self.loc.resolve_location("测试点A", opts)
        self.assertAlmostEqual(lat, 39.9087)
        self.assertEqual(p.provider, "alias")
        # 都不是、且禁用了地名解析 -> 报错并给出可操作提示
        with self.assertRaises(self.loc.ResolveError) as ctx:
            self.loc.resolve_location("不存在的地方", opts)
        self.assertIn("命名地点", str(ctx.exception))

    def test_alias_with_gcj02_is_converted(self):
        """命名地点可以存 GCJ-02（从高德复制的坐标），解析时自动转 WGS-84。"""
        self.loc.save_alias(self.root, "高德点", 39.9087, 116.3975, coord="gcj02")
        opts = self.loc.ResolveOptions(aliases=self.loc.load_aliases(self.root),
                                       allow_geocode=False)
        lat, lon, _ = self.loc.resolve_location("高德点", opts)
        # GCJ-02 -> WGS-84 会有几百米偏移，绝不能等于原值
        self.assertNotAlmostEqual(lat, 39.9087, places=4)

    def test_parse_dumpsys_redacted(self):
        """Android 10+ 会把坐标打码，必须识别出来而不是当成有效坐标。"""
        r = self.loc.parse_dumpsys_location(
            "gps: Location[gps 40******,116****** hAcc=3 mock]")
        self.assertTrue(r["redacted"])
        self.assertIsNone(r["real"])
        self.assertIsNone(r["mock"])

    def test_parse_dumpsys_real_and_mock(self):
        text = (
            "gps: Location[gps 39.908712,116.397503 hAcc=3 et=+1h31m41s]\n"
            "network: Location[network 39.908601,116.397600 hAcc=8 mock]\n"
            "last location=Location[gps 39.908712,116.397503 hAcc=3]\n")
        r = self.loc.parse_dumpsys_location(text)
        self.assertFalse(r["redacted"])
        self.assertAlmostEqual(r["real"].lat, 39.908712)
        self.assertAlmostEqual(r["mock"].lon, 116.397600)
        self.assertIn("真实", r["real"].detail)
        self.assertIn("虚拟", r["mock"].detail)

    def test_geocode_requires_key_for_amap(self):
        with self.assertRaises(self.loc.ResolveError) as ctx:
            self.loc.geocode("天安门", provider="amap", amap_key="")
        self.assertIn("key", str(ctx.exception).lower())


class TestGui(unittest.TestCase):
    """图形界面的构造与命令拼接。

    Tkinter 在无显示环境（CI）里可能初始化失败，这时跳过而不是让整套测试挂掉。
    """

    @classmethod
    def setUpClass(cls):
        try:
            import tkinter
            cls.tk = tkinter
            cls.root = tkinter.Tk()
            cls.root.withdraw()
        except Exception as e:                                       # noqa: BLE001
            raise unittest.SkipTest(f"tkinter 不可用: {e}")
        sys.path.insert(0, str(ROOT / "tools"))
        import gui
        cls.gui = gui
        cls.ui = gui.Console(cls.root)
        cls.root.update()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.root.destroy()
        except Exception:                                            # noqa: BLE001
            pass

    def test_gui_module_imports_and_builds(self):
        nb = [w for w in self.root.winfo_children()
              if isinstance(w, self.tk.ttk.Notebook)][0]
        tabs = [nb.tab(t, "text").strip() for t in nb.tabs()]
        self.assertEqual(len(tabs), 5)
        for name in ("快速开始", "多点选路", "生成轨迹", "轨迹文件", "设备控制"):
            self.assertIn(name, tabs)

    def test_gui_env_has_what_adb_needs(self):
        """GUI 必须自己配好 adb 需要的目录，否则 adb 直接崩。"""
        env = self.ui.runner.base_env()
        for key in ("ANDROID_USER_HOME", "ADB_VENDOR_KEYS", "TMP", "TEMP",
                    "ANDROID_HOME", "PYTHONIOENCODING"):
            self.assertIn(key, env, f"缺少 {key}")
            self.assertTrue(env[key], f"{key} 为空")
        self.assertEqual(env["TMP"], env["TEMP"])
        self.assertIn(".toolchain", env["ANDROID_USER_HOME"])

    def test_gui_gen_args_osrm(self):
        ui = self.ui
        ui.var_provider.set("osrm（免费，无需 key）")
        ui.var_preset.set("run 跑步")
        for v in (ui.var_speed, ui.var_interval, ui.var_accel, ui.var_noise):
            v.set("")
        args = ui._gen_args()
        self.assertIn("gen", args)
        self.assertIn("--preset", args)
        self.assertIn("run", args)
        self.assertIn("--from", args)
        self.assertIn("--profile", args)
        # 留空的参数不应出现在命令里（让预设决定）
        self.assertNotIn("--speed", args)
        self.assertNotIn("--noise", args)

    def test_gui_gen_args_explicit_override(self):
        ui = self.ui
        ui.var_provider.set("osrm（免费，无需 key）")
        ui.var_preset.set("run 跑步")
        ui.var_speed.set("4.2")
        ui.var_interval.set("500")
        ui.var_noise.set("1.0")
        args = ui._gen_args()
        self.assertEqual(args[args.index("--speed") + 1], "4.2")
        self.assertEqual(args[args.index("--interval") + 1], "500")
        self.assertEqual(args[args.index("--noise") + 1], "1.0")
        for v in (ui.var_speed, ui.var_interval, ui.var_noise):
            v.set("")

    def test_gui_gen_args_file_provider(self):
        ui = self.ui
        ui.var_provider.set("file 本地文件（不联网）")
        ui.var_file.set("examples/path-demo.txt")
        args = ui._gen_args()
        self.assertIn("--file", args)
        self.assertIn("examples/path-demo.txt", args)
        # file 模式不该带起终点
        self.assertNotIn("--from", args)
        self.assertNotIn("--to", args)
        # 复原，避免影响其它测试
        ui.var_provider.set("osrm（免费，无需 key）")

    def test_gui_preset_and_profile_maps_are_complete(self):
        """下拉框的显示文字必须都能映射到真实的命令行取值。"""
        for label in self.gui.PRESETS:
            self.assertIn(self.gui.PRESETS[label], ("custom", "walk", "run", "interval"))
        for label in self.gui.PROVIDERS:
            self.assertIn(self.gui.PROVIDERS[label], ("osrm", "amap", "google", "file"))
        for label in self.gui.MODES:
            self.assertIn(self.gui.MODES[label], ("once", "loop", "pingpong"))

    def test_gui_runner_uses_file_redirection_not_pipes(self):
        """Runner 必须用文件重定向，不能用 PIPE。

        原因见 gui.Runner 的类文档：在受限环境里，孙进程用管道捕获输出会被
        拒绝或永久阻塞。这个断言防止以后有人"顺手改回 PIPE"。
        """
        import inspect
        src = inspect.getsource(self.gui.Runner.start)
        self.assertNotIn("subprocess.PIPE", src, "改回管道捕获会在此环境下挂死")
        self.assertIn("stdout=fh", src)
        self.assertIn("gui-logs", src)


    def test_gui_route_args_builds_via_points(self):
        """多点选路的结果必须变成一串 --via。"""
        ui = self.ui
        ui.var_r_from.set("39.908700,116.397500")
        ui.var_r_via.set("39.900000,116.400000;39.890000,116.405000")
        ui.var_r_to.set("39.882200,116.406600")
        args = ui._route_args("out/t.json")
        self.assertEqual(args.count("--via"), 2)
        self.assertEqual(args[args.index("--from") + 1], "39.908700,116.397500")
        self.assertEqual(args[args.index("--to") + 1], "39.882200,116.406600")
        self.assertIn("--profile", args)
        # 空途经点不该产生 --via
        ui.var_r_via.set("")
        self.assertEqual(ui._route_args("out/t.json").count("--via"), 0)
        ui.act_route_clear()
        self.assertEqual(ui.var_r_from.get(), "")
        self.assertEqual(ui.var_r_via.get(), "")

    def test_gui_route_args_requires_both_ends(self):
        ui = self.ui
        ui.var_r_from.set("39.9087,116.3975")
        ui.var_r_to.set("")
        # 缺终点时弹窗并返回 None —— 这里把弹窗替换掉以免阻塞
        import tkinter.messagebox as mb
        orig = mb.showwarning
        mb.showwarning = lambda *a, **k: None
        try:
            self.assertIsNone(ui._route_args("out/t.json"))
        finally:
            mb.showwarning = orig
            ui.act_route_clear()

    def test_route_picker_multi_point_logic(self):
        """RoutePicker 的加点 / 排序 / 删除逻辑（不打开窗口）。"""
        from mockroute import mappicker
        got = {}
        # 直接构造会建真实窗口，这里只测纯逻辑：用一个极简替身
        class Fake:
            _sel = mappicker.RoutePicker._sel
            _move = mappicker.RoutePicker._move
            _delete = mappicker.RoutePicker._delete
            _clear = mappicker.RoutePicker._clear

            def __init__(self):
                self.points = [(1.0, 1.0), (2.0, 2.0), (3.0, 3.0)]
                self._list_calls = []

                class L:
                    @staticmethod
                    def delete(*a):
                        pass

                    @staticmethod
                    def insert(*a):
                        pass

                    @staticmethod
                    def curselection():
                        return Fake._cur

                    @staticmethod
                    def selection_clear(*a):
                        pass

                    @staticmethod
                    def selection_set(*a):
                        pass

                self.lst = L()

            def _refresh_list(self):
                pass

            def redraw(self):
                pass

        f = Fake()
        f.lst.curselection = staticmethod(lambda: (1,) if hasattr(Fake, "_cur") else ())
        # 选中索引 1 上移 -> 与索引 0 交换
        f.lst.curselection = staticmethod(lambda: (1,))
        f._move(-1)
        self.assertEqual(f.points, [(2.0, 2.0), (1.0, 1.0), (3.0, 3.0)])
        # 下移回来
        f.lst.curselection = staticmethod(lambda: (0,))
        f._move(1)
        self.assertEqual(f.points, [(1.0, 1.0), (2.0, 2.0), (3.0, 3.0)])
        # 删除索引 1
        f.lst.curselection = staticmethod(lambda: (1,))
        f._delete()
        self.assertEqual(f.points, [(1.0, 1.0), (3.0, 3.0)])
        f._clear()
        self.assertEqual(f.points, [])


    def test_search_requires_key_for_amap_only(self):
        """provider=amap 且没 key 时必须明确报错（而不是静默失败）。"""
        from mockroute import locations
        with self.assertRaises(locations.ResolveError) as ctx:
            locations.search("天安门", provider="amap", amap_key="")
        self.assertIn("key", str(ctx.exception).lower())

    def test_search_auto_falls_through_providers(self):
        """auto 模式下某个来源失败不该整体失败 —— 要记录原因并继续尝试。

        这里把网络调用全部替换成可控的假实现，不依赖真实网络。
        """
        from mockroute import locations

        calls = []

        def fake_photon(q, limit, lat, lon, timeout):
            calls.append("photon")
            return [locations.Place(name="测试点", lat=1.0, lon=2.0, provider="photon")]

        orig = locations.search_photon
        locations.search_photon = fake_photon
        try:
            res = locations.search("x", provider="auto", amap_key="")
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0].provider, "photon")
            self.assertIn("photon", calls)
        finally:
            locations.search_photon = orig

    def test_search_reports_all_failures(self):
        """所有来源都失败时，错误信息要把每个来源的原因列出来。"""
        from mockroute import locations

        def boom(*a, **k):
            raise locations.ResolveError("模拟不可达")

        o1, o2 = locations.search_photon, locations.geocode_nominatim
        locations.search_photon = boom
        locations.geocode_nominatim = boom
        try:
            with self.assertRaises(locations.ResolveError) as ctx:
                locations.search("x", provider="auto", amap_key="")
            msg = str(ctx.exception)
            self.assertIn("photon", msg)
            self.assertIn("nominatim", msg)
        finally:
            locations.search_photon = o1
            locations.geocode_nominatim = o2

    def test_photon_parsing_shape(self):
        """Photon 的返回结构（GeoJSON FeatureCollection）要能正确解析。

        coordinates 是 [lon, lat] 顺序 —— 写反了会得到完全错误的位置。
        """
        from mockroute import locations
        sample = {"features": [{
            "geometry": {"coordinates": [116.391263, 39.907359]},
            "properties": {"name": "天安门", "city": "北京市",
                           "osm_key": "historic", "osm_value": "city_gate"},
        }]}
        orig = locations._get_json
        locations._get_json = lambda url, timeout: sample
        try:
            res = locations.search_photon("天安门", limit=1)
        finally:
            locations._get_json = orig
        self.assertEqual(len(res), 1)
        self.assertAlmostEqual(res[0].lat, 39.907359)   # latitude
        self.assertAlmostEqual(res[0].lon, 116.391263)  # longitude
        self.assertIn("北京市", res[0].detail)

    def test_gui_search_panel_uses_queue_not_after_from_thread(self):
        """检索线程绝不能直接碰 Tk 控件（包括 self.after）。

        这是一个真实踩过的坑：从后台线程调用 self.after() 会抛
        "main thread is not in main loop"。所以必须走队列 + 主线程轮询。
        这条测试防止以后有人改回去。

        注意要先剥掉注释再检查 —— 注释里正当地提到了 self.after。
        """
        import inspect
        import io as _io
        import tokenize
        from mockroute import mappicker

        def code_only(fn):
            """去掉注释与文档字符串，只留可执行代码。"""
            src = inspect.getsource(fn)
            out = []
            for tok in tokenize.generate_tokens(_io.StringIO(src).readline):
                if tok.type == tokenize.COMMENT:
                    continue
                if tok.type == tokenize.STRING and tok.line.strip().startswith('"""'):
                    continue
                out.append(tok.string)
            return " ".join(out)

        body = code_only(mappicker.SearchPanel.do_search)
        # 取出 work() 那一段（线程体）。token 化后 `Thread(` 会变成 `Thread (`。
        if "def work" in body:
            end = body.find("Thread", body.index("def work"))
            worker = body[body.index("def work"):end if end > 0 else len(body)]
        else:
            worker = ""
        self.assertNotIn("self . after", worker)
        self.assertNotIn("self . configure", worker)
        self.assertIn("_q . put", worker)
        # 主线程轮询：消费队列并把结果落到界面
        poll = code_only(mappicker.SearchPanel._poll)
        self.assertIn("_q . get_nowait", poll)
        self.assertIn("_show", poll)
        # 轮询必须由 after 在主线程里启动
        self.assertIn("self . after", body)


    def test_map_picker_enter_does_not_close_window(self):
        """回归：在搜索框里敲回车，窗口**不能**被当成「确认」关掉。

        真实 bug：窗口级绑定了 <Return> -> _confirm()，而 Entry 上的
        <Return> 没返回 "break"，事件冒泡上去就把窗口关了。
        在控制台里更糟：关闭后回调把默认中心坐标填进了起终点。
        """
        from mockroute import mappicker
        got = []
        win = mappicker.MapPicker(self.root, 39.9087, 116.3975,
                                  lambda la, lo: got.append((la, lo)))
        try:
            self.root.update()
            win.update()
            # Entry 的回车处理必须返回 "break"（阻断冒泡）
            self.assertEqual(win.search._on_enter(), "break")
            self.root.update()
            self.assertTrue(win.winfo_exists(), "敲回车后窗口不该消失")
            self.assertEqual(got, [], "敲回车不该触发确认回调")
        finally:
            win._on_close()
            self.root.update()

    def test_map_picker_refuses_confirm_without_pick(self):
        """没选过点就确认，必须被拦住而不是把初始中心当地点返回。"""
        import tkinter.messagebox as mb
        from mockroute import mappicker
        got = []
        win = mappicker.MapPicker(self.root, 39.9087, 116.3975,
                                  lambda la, lo: got.append((la, lo)))
        shown = []
        orig = mb.showinfo
        mb.showinfo = lambda *a, **k: shown.append(a[0] if a else "")
        try:
            win.update()
            win._confirm()
            self.assertEqual(got, [], "没选点不该确认")
            self.assertTrue(shown, "应当有提示弹窗")
            self.assertTrue(win.winfo_exists())
            # 点过地图之后就能确认了
            win._set_from_screen(400, 300)
            win._confirm()
            self.assertEqual(len(got), 1, "选过点后应能确认")
        finally:
            mb.showinfo = orig
            self.root.update()

    def test_pickers_bind_no_toplevel_return_to_confirm(self):
        """回归：窗口级不能再把 <Return> 绑到「确认」。

        这条是上面那个 bug 的根因，必须静态守住 —— 否则以后有人图方便
        再加一行 self.bind("<Return>", ...) 就又坏了。
        """
        import inspect
        import re
        from mockroute import mappicker
        for cls in (mappicker.MapPicker, mappicker.RoutePicker):
            src = inspect.getsource(cls)
            for line in src.splitlines():
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if not stripped.startswith("self.bind("):
                    continue
                # 窗口级只允许这些键
                keys = re.findall(r'"<([^>]+)>"|\'<([^>]+)>\'', stripped)
                flat = [a or b for a, b in keys]
                self.assertNotIn("Return", flat,
                                 f"{cls.__name__} 不该把回车绑成窗口级确认")
                self.assertNotIn("KP_Enter", flat,
                                 f"{cls.__name__} 不该把回车绑成窗口级确认")

    def test_pickers_cancel_pending_redraw(self):
        """回归：关窗必须取消已排队的重绘，否则抛 invalid command name。

        <Destroy> 与 _on_close **两条路**都要覆盖 —— 父窗口被销毁时
        不会经过 _on_close。
        """
        import inspect
        from mockroute import mappicker
        for cls in (mappicker.MapPicker, mappicker.RoutePicker):
            src = inspect.getsource(cls)
            self.assertIn("_schedule_redraw", src,
                          f"{cls.__name__} 应通过 _schedule_redraw 排重绘")
            self.assertIn("after_cancel", src,
                          f"{cls.__name__} 应取消挂起的回调")
            # <Destroy> 必须绑到 _on_destroy 而不是裸 lambda
            self.assertIn("self.bind(\"<Destroy>\", self._on_destroy)", src,
                          f"{cls.__name__} 的 Destroy 应走 _on_destroy")
        # _schedule_redraw 必须先取消旧的，避免出现两个挂起回调
        sched = inspect.getsource(mappicker.MapPicker._schedule_redraw)
        self.assertIn("after_cancel", sched)
        self.assertLess(sched.index("after_cancel"), sched.index("self.after("),
                        "必须先取消旧回调，再排新的")


    def test_route_tab_has_playback_entry(self):
        """回归：多点选路页必须有回放入口。

        真实用户反馈「选了选点但没办法跑」—— 那一页当时只有「生成轨迹」，
        没有回放按钮，生成完就卡住了。
        """
        texts = []

        def walk(w):
            for c in w.winfo_children():
                if c.__class__.__name__ in ("Button", "Checkbutton"):
                    try:
                        t = c.cget("text")
                    except Exception:                                # noqa: BLE001
                        t = ""
                    if t:
                        texts.append(t)
                walk(c)

        walk(self.ui.tab_route)
        joined = " ".join(texts)
        for need in ("生成并回放", "跑这条", "停止", "状态"):
            self.assertIn(need, joined, f"多点选路页缺少「{need}」")
        # 回放目标要能单独指定，且默认指向本页生成的文件
        self.assertEqual(self.ui.var_r_file.get(), "out/gui-route.json")
        # 倍速字段必须能转成数字（测试间可能残留别的值，所以只查可解析性）
        float(self.ui.var_r_mult.get())

    def test_route_play_reports_missing_file(self):
        """回放不存在的文件时应弹提示，而不是静默什么也不做。"""
        import tkinter.messagebox as mb
        ui = self.ui
        ui.var_r_file.set("out/__绝对不存在的文件__.json")
        shown = []
        orig = mb.showwarning
        mb.showwarning = lambda *a, **k: shown.append(a[0] if a else "")
        try:
            ui.act_route_play()
        finally:
            mb.showwarning = orig
        self.assertTrue(shown, "应当弹窗提示找不到文件")
        ui.var_r_file.set("out/gui-route.json")

    def test_route_play_builds_correct_command(self):
        """回放命令要带 --no-wait 与正确的倍速。

        自己造一个真实的轨迹文件，不要依赖 out/gui-route.json 碰巧存在 ——
        那是上一次手工跑出来的产物，属于脆弱的测试前提。
        """
        ui = self.ui
        with tempfile.TemporaryDirectory() as td:
            rf = Path(td) / "r.json"
            rf.write_text(json.dumps({
                "version": 1, "mode": "once", "interval": 1000,
                "points": [{"t": 0, "lat": 39.9, "lon": 116.4, "speed": 1.4,
                            "bearing": 0.0, "acc": 2.0, "alt": 40.0},
                           {"t": 1000, "lat": 39.9001, "lon": 116.4001,
                            "speed": 1.4, "bearing": 90.0, "acc": 2.0, "alt": 40.0}],
            }, ensure_ascii=False), encoding="utf-8")
            ui.var_r_file.set(str(rf))
            ui.var_r_mult.set("20")
            calls = []
            orig = ui.runner.mroute
            ui.runner.mroute = lambda *a, **k: calls.append(a) or True
            try:
                ui.act_route_play()
            finally:
                ui.runner.mroute = orig
                ui.var_r_file.set("out/gui-route.json")

        self.assertEqual(len(calls), 1)
        args = list(calls[0])
        self.assertIn("play", args)
        self.assertEqual(args[args.index("--route") + 1], str(rf))
        self.assertIn("--no-wait", args)
        # 倍速按 float 传（--mult 接受浮点）
        self.assertEqual(float(args[args.index("--mult") + 1]), 20.0)
        ui.var_r_mult.set("1")


    def test_map_picker_converts_gcj02_to_wgs84(self):
        """回归：地图选点必须做 GCJ-02 -> WGS-84 转换。

        真实 bug：高德瓦片是 GCJ-02，代码却把点出来的坐标当 WGS-84 用，
        国内整体偏 500~600 米（实测北京 555m、上海 479m、深圳 606m）。
        现象就是用户报的「电脑上标的路线和手机上地图跑的不一致」。

        这里模拟「在 GCJ-02 底图上点天安门」，输出应当还原成 WGS-84 的天安门。
        """
        from mockroute import geo, mappicker
        wgs = (39.908700, 116.397500)
        got = []
        win = mappicker.MapPicker(self.root, wgs[0], wgs[1],
                                  lambda la, lo: got.append((la, lo)))
        try:
            win.geometry("900x720+40+40")
            self.root.update()
            win.update()
            # 底图坐标系应存 GCJ-02 值（与 WGS-84 相差约 555 米）
            self.assertAlmostEqual(win.lat, 39.910103, places=5)
            self.assertAlmostEqual(win.lon, 116.403744, places=5)
            # 转回 WGS-84 必须回到天安门
            wlat, wlon = win._center_wgs()
            self.assertAlmostEqual(wlat, wgs[0], places=5)
            self.assertAlmostEqual(wlon, wgs[1], places=5)
            # 点屏幕正中 -> 输出应为 WGS-84 的天安门。
            # 容差 5 米：画布中心用整数像素定位，高德 z15 下一像素约 4.8 米，
            # 这属于选点本身的量化误差，与坐标系无关（坐标系差了 555 米）。
            cx = win.canvas.winfo_width() // 2
            cy = win.canvas.winfo_height() // 2
            win._set_from_screen(cx, cy)
            win._confirm()
            self.root.update()
            self.assertEqual(len(got), 1)
            self.assertAlmostEqual(got[0][0], wgs[0], delta=5e-5)
            self.assertAlmostEqual(got[0][1], wgs[1], delta=5e-5)
            # 关键断言：绝不能是未转换的 GCJ-02 值（那会差 555 米 ≈ 0.005°）
            self.assertGreater(abs(got[0][0] - 39.910103), 1e-4,
                               "输出像是没做 GCJ-02 转换")
            self.assertGreater(abs(got[0][1] - 116.403744), 1e-4,
                               "输出像是没做 GCJ-02 转换")
        finally:
            self.root.update()

    def test_map_picker_switching_basemap_keeps_place(self):
        """切换底图（GCJ <-> WGS）不该让视野跳 500 米。"""
        from mockroute import mappicker
        wgs = (39.908700, 116.397500)
        win = mappicker.MapPicker(self.root, wgs[0], wgs[1], lambda a, b: None)
        try:
            win.geometry("900x720+40+40")
            self.root.update()
            win.update()
            # 切到 ArcGIS（WGS-84）
            win.var_src.set(mappicker.TILE_URLS[1][0])
            win._on_src()
            self.root.update()
            # 中心应变成 WGS-84 的天安门，而不是保留 GCJ 值
            self.assertAlmostEqual(win.lat, wgs[0], places=5)
            self.assertAlmostEqual(win.lon, wgs[1], places=5)
        finally:
            self.root.update()

    def test_tile_sources_declare_coordinate_system(self):
        """每个瓦片源都必须声明坐标系，且高德是 GCJ-02。

        少了这个声明，转换就无从判断 —— 这正是当初漏掉的东西。
        """
        from mockroute import mappicker
        for entry in mappicker.TILE_URLS:
            self.assertEqual(len(entry), 4,
                             f"瓦片源应为 4 元组（名称/URL/子域数/坐标系）: {entry}")
            self.assertIn(entry[3], ("gcj02", "wgs84"),
                          f"未知坐标系: {entry[3]}")
        self.assertEqual(mappicker.TILE_URLS[0][3], "gcj02",
                         "高德瓦片是 GCJ-02")

    def test_route_picker_points_stored_as_wgs84(self):
        """RoutePicker 的 points 必须存 WGS-84（要交回给用户的），
        而画到底图上时要转成 GCJ-02。
        """
        from mockroute import mappicker
        wgs = (39.908700, 116.397500)
        win = mappicker.RoutePicker(self.root, wgs[0], wgs[1], lambda p: None)
        try:
            win.geometry("1180x760+40+40")
            self.root.update()
            win.update()
            win.points.append(wgs)
            vlat, vlon = win._to_view(*wgs)
            self.assertAlmostEqual(vlat, 39.910103, places=5)
            self.assertAlmostEqual(vlon, 116.403744, places=5)
        finally:
            self.root.update()


    def test_route_picker_click_converts_to_wgs84(self):
        """回归：RoutePicker **点击加点**必须转成 WGS-84。

        真实 bug：加点时直接存了底图坐标系（高德 GCJ-02）的值，漏了
        `_to_wgs` —— 于是每个点都偏 500~600 米（实测北京 555m）。
        现象就是用户报的「多点选路根本没对齐」。

        这条测试走的是**真实点击路径**（_on_press/_on_release），
        而不是只调 _to_view，所以能抓住这一类漏转换。
        """
        from mockroute import geo, mappicker
        cases = [
            ("天安门", (39.907359, 116.391263)),
            ("奥体", (39.992800, 116.396400)),
            ("天坛公园", (39.879907, 116.402872)),
        ]
        got = []
        win = mappicker.RoutePicker(self.root, 39.96, 116.40, lambda p: got.append(p))
        try:
            win.geometry("1180x760+40+40")
            self.root.update()
            win.update()
            cw = win.canvas.winfo_width()
            ch = win.canvas.winfo_height()

            class E:
                def __init__(self, x, y):
                    self.x, self.y = x, y

            for name, wgs in cases:
                # 把视野中心对准该点 -> 它必然落在画布正中
                win.lat, win.lon = win._to_view(*wgs)
                win.redraw()
                self.root.update()
                pt = E(cw // 2, ch // 2)
                win._on_press(pt)
                win.drag_moved = False
                win._on_release(pt)
                self.root.update()

                out = win.points[-1]
                d = geo.haversine(out[0], out[1], wgs[0], wgs[1])
                # 容差 20 米：画布中心取整 + z15 像素分辨率约 4.8 米
                self.assertLess(d, 20.0,
                                f"{name} 选点偏差 {d:.0f} 米 —— 像是漏了坐标转换")
                # 关键断言：绝不能是未转换的 GCJ 值（那会偏 500+ 米）
                gcj = geo.wgs84_to_gcj02(*wgs)
                self.assertGreater(geo.haversine(out[0], out[1], gcj[0], gcj[1]),
                                   100.0,
                                   f"{name} 输出疑似未转换的 GCJ-02 坐标")
        finally:
            self.root.update()

    def test_route_picker_search_and_alias_need_no_conversion(self):
        """检索结果和命名地点本身已是 WGS-84，加点时**不该**再转一次。

        与上一条互为对照：点击要转，检索/命名地点不能转。搞混了同样偏 555 米。
        """
        from mockroute import geo, mappicker
        wgs = (39.907359, 116.391263)
        win = mappicker.RoutePicker(self.root, wgs[0], wgs[1], lambda p: None,
                                    aliases={"家": {"lat": wgs[0], "lon": wgs[1]}})
        try:
            win.geometry("1180x760+40+40")
            self.root.update()
            win.update()
            # 检索回调（preview=False 才会加点）
            win._on_search_pick(wgs[0], wgs[1], "测试", "", preview=False)
            self.root.update()
            out = win.points[-1]
            d = geo.haversine(out[0], out[1], wgs[0], wgs[1])
            self.assertLess(d, 0.01, f"检索加点被多转了一次，偏 {d:.0f} 米")

            # 命名地点加点
            win.var_alias.set("家")
            win._add_alias()
            self.root.update()
            out2 = win.points[-1]
            d2 = geo.haversine(out2[0], out2[1], wgs[0], wgs[1])
            self.assertLess(d2, 0.01, f"命名地点加点被多转了一次，偏 {d2:.0f} 米")
        finally:
            self.root.update()

    def test_name_regex(self):
        """轨迹名只允许中文/字母/数字/下划线/短横线。"""
        for good in ("晨跑", "晨跑-1", "abc_2", "morningrun", "跑2024"):
            self.assertTrue(self.gui.Console._NAME_OK.match(good), good)
        for bad in ("", "有 空格", "a/b", "a\\b", "x" * 41, "a.b", "a:b", "a*b"):
            self.assertFalse(self.gui.Console._NAME_OK.match(bad), bad)

    def test_duplicate_name_is_rejected(self):
        """重名规则：已存在且不在本次会话里 -> 拒绝。

        这是用户报的「生成的永远是上一次的」的根治办法 ——
        强制命名 + 拒绝重名，每条轨迹一个独立文件。

        注意：把 `gui.OUT` 重定向到临时目录再测。原来的写法直接往仓库的
        `out/` 写文件，会污染工作区，而且在目录不可写时整条测试报
        PermissionError（并在全套跑时连带把下一个测试卡死）。
        """
        import tempfile
        import tkinter.messagebox as mb
        import tkinter.simpledialog as sd
        gui = self.gui
        ui = self.ui
        shown = []
        o_warn, o_sd = mb.showwarning, sd.askstring
        o_out = gui.OUT
        with tempfile.TemporaryDirectory() as td:
            try:
                gui.OUT = Path(td)
                dup = gui.OUT / "占用测试.json"
                dup.write_text("{}", encoding="utf-8")

                sd.askstring = lambda *a, **k: "占用测试"
                mb.showwarning = lambda *a, **k: shown.append(a[0] if a else "")
                self.assertIsNone(ui._ask_route_name())
                self.assertIn("已被占用", " ".join(shown))

                # 加进会话集合后应放行（允许覆盖自己刚生成的）
                ui._session_names.add("占用测试")
                self.assertEqual(ui._ask_route_name(), "占用测试")

                # 非法名字 -> 拒绝
                shown.clear()
                sd.askstring = lambda *a, **k: "有 空格"
                self.assertIsNone(ui._ask_route_name())
                self.assertIn("不合法", " ".join(shown))

                # 取消（None）-> 返回 None 且不弹警告
                shown.clear()
                sd.askstring = lambda *a, **k: None
                self.assertIsNone(ui._ask_route_name())
                self.assertEqual(shown, [], "取消不该弹警告")
            finally:
                mb.showwarning, sd.askstring = o_warn, o_sd
                gui.OUT = o_out
                ui._session_names.discard("占用测试")
            try:
                dup.unlink()
            except OSError:
                pass
            self.root.update()

    def test_route_tab_has_speed_control(self):
        """回归：多点选路页必须能改速度。

        真实反馈：「跑的似乎不像人类，而且还改不了速度」——
        那一页当时只有预设下拉，**没有任何速度输入框**，
        默认预设还是 run（3.3 m/s ≈ 5'03"/km），对想散步的人就是"飞"。
        """
        ui = self.ui
        self.assertTrue(hasattr(ui, "var_r_speed"), "选路页缺少速度输入")
        self.assertTrue(hasattr(ui, "var_r_interval"))
        self.assertTrue(hasattr(ui, "var_r_noise"))
        # 默认预设应当是步行，而不是跑步。
        # 注意 TestGui 共用一个 Console，前面的测试可能改过这个变量，
        # 所以默认值用**源码**确认（不受测试顺序影响），运行时再单独复位。
        src = (ROOT / "tools/gui.py").read_text(encoding="utf-8")
        self.assertIn('self.var_r_preset = tk.StringVar(value="walk 步行")', src,
                      "选路页默认预设应为步行")
        ui.var_r_preset.set("walk 步行")
        texts = []

        def walk(w):
            for c in w.winfo_children():
                if c.__class__.__name__ == "Button":
                    try:
                        texts.append(c.cget("text"))
                    except Exception:                                # noqa: BLE001
                        pass
                walk(c)

        walk(ui.tab_route)
        joined = " ".join(texts)
        for need in ("步行", "慢跑", "跑步", "按预设填回"):
            self.assertIn(need, joined, f"选路页缺少「{need}」")

    def test_route_tab_speed_flows_into_command(self):
        """改速度必须真的进到 gen 命令里，否则界面上改了也没用。"""
        ui = self.ui
        ui.var_r_from.set("39.907359,116.391263")
        ui.var_r_to.set("39.908700,116.397500")
        ui.var_r_via.set("")
        ui._set_r_speed("1.8")
        ui.var_r_interval.set("500")
        ui.var_r_noise.set("2.0")
        args = ui._route_args("out/t.json")
        self.assertEqual(float(args[args.index("--speed") + 1]), 1.8)
        self.assertEqual(args[args.index("--interval") + 1], "500")
        self.assertEqual(float(args[args.index("--noise") + 1]), 2.0)
        # 清空后不应再带这些参数（交回预设决定）
        ui.var_r_speed.set("")
        ui.var_r_interval.set("")
        ui.var_r_noise.set("")
        args2 = ui._route_args("out/t.json")
        for flag in ("--speed", "--interval", "--noise"):
            self.assertNotIn(flag, args2)

    def test_preset_fills_speed_and_hint(self):
        """选预设应把速度/采样/噪声填好，并把速度换算成配速提示。"""
        ui = self.ui
        ui.var_r_preset.set("walk 步行")
        ui._on_r_preset()
        self.assertAlmostEqual(float(ui.var_r_speed.get()), 1.35, places=2)
        self.assertIn("/km", ui.var_r_speed_hint.get())

        ui.var_r_preset.set("run 跑步")
        ui._on_r_preset()
        self.assertAlmostEqual(float(ui.var_r_speed.get()), 3.3, places=2)
        self.assertIn("5'03", ui.var_r_speed_hint.get())   # 3.3 m/s ≈ 5'03"/km

        # 非法速度不该崩，只提示不是数字
        ui.var_r_speed.set("abc")
        ui._update_r_speed_hint()
        self.assertIn("不是数字", ui.var_r_speed_hint.get())
        ui.var_r_speed.set("")
        ui.var_r_interval.set("")
        ui.var_r_noise.set("")
        ui.var_r_speed_hint.set("")

    def test_round_corners_smooths_without_hairpins(self):
        """拐角圆滑：把尖角变成弧线，且**不能制造发夹（折返）**。

        背景：OSRM 几何在路口是尖角，按弧长采样时某一步正好跨过顶点，
        位置只挪几十厘米但方向瞬间转过去 —— 用户反馈的「拐弯瞬移」。
        实测一步内 bearing 跳 180°、位置转角 >90° 有 322 处。

        实现上最容易犯的错：相邻两个拐角各自取「半段长」当切点会**互相
        重叠**，圆滑后生成走两米又原路折返的「发夹」，反而更糟。这条测试
        专门守住这一点。
        """
        import math
        from mockroute import geo
        R = 6371008.8

        def d2(a, b):
            return geo.haversine(a[0], a[1], b[0], b[1])

        # 一条 L 形折线（直角）
        L = [(39.9000, 116.4000), (39.9000, 116.4020), (39.9020, 116.4020)]
        out = geo.round_corners(L, radius_m=10.0)
        self.assertGreater(len(out), len(L), "应当插入了圆弧点")

        # 1) 圆滑必须让路径**变短**（切角是向内切的；向外鼓会变长）。
        # 注意：圆滑只处理拐角，**不会**把原始长直线变短 —— 那是 densify 的职责。
        # 所以这里不检查全程最长段（那会等于原始长直线），只看圆弧附近。
        self.assertLess(geo.total_length(out), geo.total_length(L) - 1.0,
                        "圆滑应当切角缩短路径；变长说明曲线向外鼓了")
        near = [d2(out[i - 1], out[i]) for i in range(2, len(out) - 1)]
        self.assertLess(max(near), 12.0, "圆弧附近的段长应当很短")

        # 2) 关键：不能有折返。检查每个点的投影是否沿原折线单调前进
        cum = geo.cumulative(L)
        proj = []
        for q in out:
            best, bi, bt = 1e9, 0, 0.0
            for k in range(1, len(L)):
                a, b = L[k - 1], L[k]
                dd = geo._point_to_segment(q, a, b)
                if dd < best:
                    best, bi = dd, k
                    seg = d2(a, b)
                    la, lb = d2(q, a), d2(q, b)
                    tt = ((la * la - lb * lb + seg * seg) / (2 * seg)) if seg > 1e-9 else 0
                    bt = max(0.0, min(seg, tt))
            proj.append(cum[bi - 1] + bt)
        backs = [(k, proj[k - 1], proj[k]) for k in range(1, len(proj))
                 if proj[k] < proj[k - 1] - 0.3]
        self.assertEqual(backs, [],
                         f"圆滑造成了折返（发夹），{len(backs)} 处：{backs[:3]}")

        # 3) 圆滑后转角应当明显变小
        def worst_turn(pts):
            m = 0.0
            for i in range(1, len(pts) - 1):
                b1 = geo.bearing(pts[i-1][0], pts[i-1][1], pts[i][0], pts[i][1])
                b2 = geo.bearing(pts[i][0], pts[i][1], pts[i+1][0], pts[i+1][1])
                m = max(m, abs((b2 - b1 + 540) % 360 - 180))
            return m

        self.assertGreater(worst_turn(L), 80.0, "原折线应当有接近直角的尖角")
        self.assertLess(worst_turn(out), 40.0, "圆滑后不该再有尖角")

        # 4) 关闭圆滑（radius=0）应当原样返回
        self.assertEqual(geo.round_corners(L, radius_m=0.0), L)
        # 5) 点太少时原样返回，不能崩
        self.assertEqual(geo.round_corners([L[0], L[1]], radius_m=10.0), [L[0], L[1]])

    def test_corner_rounding_stays_near_original_path(self):
        """圆滑是「切角」，但切掉量必须小（否则轨迹偏出道路）。

        半径 6m 时实测切角约 1.5m，与 GPS 噪声同量级。
        """
        from mockroute import geo
        L = [(39.9000, 116.4000), (39.9000, 116.4020), (39.9020, 116.4020),
             (39.9020, 116.4040)]
        out = geo.round_corners(L, radius_m=6.0)
        worst = 0.0
        for q in out:
            dd = min(geo._point_to_segment(q, L[i - 1], L[i])
                     for i in range(1, len(L)))
            worst = max(worst, dd)
        # 半径 6m 时实测切角约 1.5~2.1m，与 GPS 噪声（2m）同量级，可接受。
        # 上限 2.5m：超过就说明圆滑半径太大，轨迹会明显偏出道路。
        self.assertLess(worst, 2.5, f"切角 {worst:.2f} m 偏大，轨迹会偏离道路")

    def test_corner_radius_option_flows_to_gen(self):
        """CLI 的 --corner-radius 必须真的传进生成器。"""
        import gui
        ui = self.ui
        # 默认值应为 6.0（实测切角 1.5m，与 GPS 噪声同量级）
        src = (ROOT / "tools/mroute.py").read_text(encoding="utf-8")
        self.assertIn('"--corner-radius", type=float, default=6.0', src)

    def test_route_tab_all_controls_are_reachable(self):
        """回归：选路页的所有控件都必须在**可滚动区域**内，不能被挤出可视区。

        真实问题：那一页内容总高 765 px，而窗口可视区约 682 px。
        「保存的路径」那块（含载入/保存/删除按钮）刚好在最底下，
        被 pack 挤出可视区 —— 用户以为那个功能没写。
        加滚动容器后所有内容都能滚到；这条测试守住两件事：
          1. 选路页有滚动容器（内容高于可视区时能滚）
          2. 关键控件确实存在于滚动内容里
        """
        tk = self.tk
        ui = self.ui
        nb = [w for w in ui.win.winfo_children()
              if isinstance(w, tk.ttk.Notebook)][0]
        nb.select(ui.tab_route)
        for _ in range(20):
            self.root.update()

        # 1) 必须有 Canvas + Scrollbar 组成的滚动容器
        canvas = None
        scrollbar = False

        def find(w):
            nonlocal canvas, scrollbar
            for c in w.winfo_children():
                if c.__class__.__name__ == "Canvas" and canvas is None:
                    canvas = c
                if isinstance(c, tk.ttk.Scrollbar):
                    scrollbar = True
                find(c)

        find(ui.tab_route)
        self.assertIsNotNone(canvas, "选路页应当有滚动容器（否则底部控件看不见）")
        self.assertTrue(scrollbar, "滚动容器应当带滚动条")

        # 2) 滚动区域应当覆盖全部内容（scrollregion 高度 >= 内容需求）
        bbox = canvas.bbox("all")
        self.assertIsNotNone(bbox)
        self.assertGreater(bbox[3] - bbox[1], 100,
                           "scrollregion 应当反映全部内容高度")

        # 3) 关键控件都在（存在即可，滚一下就能看到）
        for attr in ("cb_path", "var_r_path", "var_r_speed", "var_r_corner"):
            self.assertTrue(hasattr(ui, attr), f"缺少 {attr}")

        # 4) 载入功能真的能用
        paths = ui.cb_path["values"]
        self.assertTrue(paths, "示例路径应当已载入下拉框")
        ui.var_r_from.set("")
        ui.var_r_via.set("")
        ui.var_r_to.set("")
        ui.var_r_path.set(paths[0])
        ui.act_path_load()
        self.root.update()
        self.assertTrue(ui.var_r_from.get(), "载入后起点应被填充")
        self.assertTrue(ui.var_r_to.get(), "载入后终点应被填充")

    def test_route_tab_scrolls_when_content_taller_than_view(self):
        """窗口很小时，内容仍可滚动到（不能有控件永远看不到）。"""
        tk = self.tk
        ui = self.ui
        orig = ui.win.geometry()
        try:
            ui.win.geometry("900x420")
            for _ in range(20):
                self.root.update()
            canvas = None

            def find(w):
                nonlocal canvas
                for c in w.winfo_children():
                    if c.__class__.__name__ == "Canvas" and canvas is None:
                        canvas = c
                    find(c)

            find(ui.tab_route)
            self.assertIsNotNone(canvas)
            bbox = canvas.bbox("all")
            # 内容高于可视区 -> 必须能滚
            self.assertGreater(bbox[3] - bbox[1], canvas.winfo_height(),
                               "内容高于可视区时应当可滚动")
            canvas.yview_moveto(1.0)     # 滚到底
            self.root.update()
            self.assertGreater(canvas.yview()[1], 0.5, "应当能滚到内容底部")
        finally:
            ui.win.geometry(orig)
            for _ in range(10):
                self.root.update()

    def test_corner_anticipation_limits_step_turn(self):
        """回归：进弯前要**提前**压小步长，把方向变化分摊到多步。

        用户反馈：「拐弯的判断来的太晚了，了弯道才判断拐弯，变向后一定脱离路线」。
        原来只有 `_chord_safe_step`，它只看当前这一步的弦长/弧长比 ——
        等于走到拐点、一步跨过去时才被动削速度。结果位置虽然还在路径上
        （实测离路 0.01m），但方向一瞬间转完：3.3 m/s + 1s 采样时单步 84°。

        修好后：阈值 8° 能把单步转角从 29.2° 压到 17.2°，而点数只增加 5%
        （不会整体变慢）。
        """
        import math
        from mockroute import geo
        from mockroute.traj import GenOptions, generate

        # 一条 200m 直行 + 90° 直角弯 + 200m 直行
        lat0, lon0 = 39.9000, 116.4000
        d_lon = 200.0 / (111320.0 * math.cos(math.radians(lat0)))
        d_lat = 200.0 / 111320.0
        raw = [(lat0, lon0), (lat0, lon0 + d_lon), (lat0 + d_lat, lon0 + d_lon)]

        speed, iv = 3.3, 1000
        step = speed * iv / 1000.0
        p = geo.round_corners(list(raw), radius_m=6.0, min_turn_deg=25.0,
                              arc_step_m=max(1.0, step / 4.0))
        p = geo.densify(p, max_segment_m=max(2.0, min(25.0, step / 4.0)))

        def build(turn_deg, min_samples=5.0):
            # 两个参数是独立的：corner_step_turn_deg 管「单步净偏转」，
            # corner_min_samples 管「弯道内最少采样点」。要测前者的效果，
            # 必须把后者也显式关掉，否则测不出差异。
            o = GenOptions(speed=speed, interval_ms=iv, accel=0.9,
                           noise_sigma_m=0.0, pace_variation=0.0,
                           lead_in_points=0, seed=7, corner_radius_m=6.0,
                           max_offset_m=0.0,
                           corner_step_turn_deg=turn_deg,
                           corner_min_samples=min_samples)
            return generate(p, o)

        def max_turn(pts):
            mt = 0.0
            for i in range(1, len(pts) - 1):
                b1 = geo.bearing(pts[i-1]["lat"], pts[i-1]["lon"],
                                 pts[i]["lat"], pts[i]["lon"])
                b2 = geo.bearing(pts[i]["lat"], pts[i]["lon"],
                                 pts[i+1]["lat"], pts[i+1]["lon"])
                mt = max(mt, abs((b2 - b1 + 540) % 360 - 180))
            return mt

        off = build(0, min_samples=1.0)   # 两条限制全关
        on = build(8, min_samples=1.0)    # 只开净偏转限制
        mt_off, mt_on = max_turn(off), max_turn(on)
        self.assertGreater(mt_off, 25.0, "关闭预判时应当有较大的单步转角")
        self.assertLess(mt_on, mt_off, "开启预判后单步转角必须变小")
        self.assertLessEqual(mt_on, 20.0,
                             f"预判 8° 时单步转角应压到 20° 内，实测 {mt_on:.1f}°")
        # 代价要小：点数（=总时长）不能明显膨胀
        self.assertLess(len(on), len(off) * 1.15,
                        "预判不该让整体明显变慢")
        # 位置始终在路径上
        for pts in (off, on):
            worst = max(min(geo._point_to_segment((s["lat"], s["lon"]),
                                                 p[k-1], p[k])
                            for k in range(1, len(p))) for s in pts)
            self.assertLess(worst, 0.5, f"离路 {worst:.2f}m 偏大")

    def test_corner_min_samples_is_independent(self):
        """corner_min_samples 与 corner_step_turn_deg 必须互相独立。

          · corner_step_turn_deg 管「一步之内的净偏转」
          · corner_min_samples  管「弯道内至少落几个采样点」

        两者解决不同问题，各有关闭方式，不能揉在一起。
        """
        import math
        from mockroute import geo
        from mockroute.traj import GenOptions, generate

        lat0, lon0 = 39.9000, 116.4000
        d_lon = 200.0 / (111320.0 * math.cos(math.radians(lat0)))
        d_lat = 200.0 / 111320.0
        raw = [(lat0, lon0), (lat0, lon0 + d_lon), (lat0 + d_lat, lon0 + d_lon)]
        # 跑步速度：步长 3.3m，弯道内采样点会不够
        speed, iv = 3.3, 1000
        step = speed * iv / 1000.0
        p = geo.round_corners(list(raw), radius_m=6.0, min_turn_deg=25.0,
                              arc_step_m=max(1.0, step / 4.0))
        p = geo.densify(p, max_segment_m=max(2.0, min(25.0, step / 4.0)))

        def build(turn_deg, min_samples):
            o = GenOptions(speed=speed, interval_ms=iv, accel=0.9,
                           noise_sigma_m=0.0, pace_variation=0.0,
                           lead_in_points=0, seed=7, corner_radius_m=6.0,
                           max_offset_m=0.0, corner_step_turn_deg=turn_deg,
                           corner_min_samples=min_samples)
            return generate(p, o)

        def max_turn(pts):
            mt = 0.0
            for i in range(1, len(pts) - 1):
                b1 = geo.bearing(pts[i-1]["lat"], pts[i-1]["lon"],
                                 pts[i]["lat"], pts[i]["lon"])
                b2 = geo.bearing(pts[i]["lat"], pts[i]["lon"],
                                 pts[i+1]["lat"], pts[i+1]["lon"])
                mt = max(mt, abs((b2 - b1 + 540) % 360 - 180))
            return mt

        both_off = max_turn(build(0, 1.0))
        only_turn = max_turn(build(8, 1.0))
        only_samples = max_turn(build(0, 10.0))
        both_on = max_turn(build(8, 10.0))
        # 每条限制单独开启都应当有改善
        self.assertLess(only_turn, both_off,
                        "只开净偏转限制应当降低单步转角")
        self.assertLess(only_samples, both_off,
                        "只开最少采样点应当降低单步转角")
        self.assertLessEqual(both_on, min(only_turn, only_samples) + 1e-6,
                             "两条都开时应当不差于任一单独开启")

    def test_corner_anticipation_only_triggers_when_needed(self):
        """预判只在真正需要时减速 —— 平缓路段不该被拖慢。"""
        from mockroute import geo
        from mockroute.traj import GenOptions, generate

        # 一条接近直线的路径
        p = [(39.9000 + i * 0.0002, 116.4000) for i in range(60)]
        p = geo.densify(p, max_segment_m=5.0)
        n = {}
        for turn in (0, 25):
            o = GenOptions(speed=1.35, interval_ms=500, accel=0.22,
                           noise_sigma_m=0.0, pace_variation=0.0,
                           lead_in_points=0, seed=5, corner_radius_m=6.0,
                           max_offset_m=0.0, corner_step_turn_deg=turn)
            n[turn] = len(generate(p, o))
        # 直线路径上两者点数应当完全一致（没有多余减速）
        self.assertEqual(n[0], n[25],
                         "平缓路径上预判不该改变采样点数")

    def test_local_turn_uses_net_deviation(self):
        """净偏转而非累积转角 —— 否则缓弯会被误判为急弯。

        实测过：用累积转角时，阈值从 25° 调到 8°，点数反而忽多忽少、
        不单调（缓弯上每段几度，累积起来很大）。
        """
        from mockroute import geo
        from mockroute.traj import _local_turn

        # 一段圆弧，整体只转 20°（半径 300m，弧长约 105m）
        import math
        R = 300.0
        total_deg = 20.0
        n = 60
        pts = []
        for i in range(n + 1):
            a = math.radians(total_deg * i / n)
            pts.append((39.9 + R * a / 111320.0,
                        116.4 + R * (1 - math.cos(a))
                        / (111320.0 * math.cos(math.radians(39.9)))))
        pts = geo.densify(pts, max_segment_m=1.0)
        cum = geo.cumulative(pts)
        net = _local_turn(pts, cum, 0.0, cum[-1])
        # 净偏转应当就是这 20°，而不是把每小段夹角累加
        self.assertLess(net, 30.0,
                        f"净偏转 {net:.1f}° 偏大 —— 可能又用回了累积转角")
        self.assertGreater(net, 10.0, f"净偏转 {net:.1f}° 偏小，量错了")

    def test_each_preset_fills_all_params(self):
        """回归：切换预设必须**整体覆盖所有参数**，不能残留上一个预设的值。

        真实反馈：「在用户选择模拟类型的时候，填进去一些默认值，以确保
        不发生错乱」。

        原来的 `_on_r_preset` 只填速度/采样/噪声，不填离路上限和拐弯参数。
        于是先选「步行」（噪声 1.5、离路 3）再切「跑步」时，离路会残留 3 ——
        两套预设的参数混在一起，这就是「错乱」。
        """
        import gui
        ui = self.ui
        names = ("speed", "interval", "noise", "corner",
                 "maxoff", "stepturn", "minsamp")
        vectors = {}
        for preset in gui.PRESETS:
            ui.var_r_preset.set(preset)
            ui._on_r_preset()
            self.root.update()
            vectors[preset] = tuple(
                getattr(ui, f"var_r_{n}").get() for n in names)
            # 每个参数都不能是空的（空的会落到别的默认值上，也是错乱的来源）
            for n, v in zip(names, vectors[preset]):
                self.assertTrue(v.strip(),
                                f"预设「{preset}」的 {n} 是空的")

        # 四个预设应当是四套不同的参数
        vals = list(vectors.values())
        for preset, v in vectors.items():
            self.assertEqual(vals.count(v), 1,
                             f"预设「{preset}」的参数与别的预设重复：{v}")

    def test_preset_switch_does_not_leak_previous_values(self):
        """切换预设后，每个参数都必须来自新预设，不能残留旧值。"""
        import gui
        ui = self.ui
        names = ("speed", "interval", "noise", "corner",
                 "maxoff", "stepturn", "minsamp")
        # 先选步行，再选跑步
        ui.var_r_preset.set("walk 步行")
        ui._on_r_preset()
        walk = tuple(getattr(ui, f"var_r_{n}").get() for n in names)
        ui.var_r_preset.set("run 跑步")
        ui._on_r_preset()
        run = tuple(getattr(ui, f"var_r_{n}").get() for n in names)
        self.assertNotEqual(walk, run, "两个预设的参数不该相同")
        # 逐项确认 run 的值就是 PRESET_PARAMS 里定义的，而不是残留
        cfg = gui.Console.PRESET_PARAMS["run"]
        expect = (cfg["speed"], cfg["interval"], cfg["noise"], cfg["corner"],
                  cfg["maxoff"], cfg["stepturn"], cfg["minsamp"])
        self.assertEqual(run, expect, "切换后参数与预设定义不一致（有残留）")

    def test_preset_params_keep_clamp_ratio_healthy(self):
        """预设的「上限÷σ」比值必须够大，否则误差分布会退化。

        来源：docs/08-噪声与离路配置表.md 实测 ——
        「被夹住」的点占比只取决于 上限÷σ：
          · 比值 4（σ2.0/上限8、σ0.5/上限2）-> 被夹 0.0~0.2%  健康
          · 比值 2（σ1.5/上限3）           -> 被夹 1.4%       健康
          · 比值 1（σ1.5/上限1）           -> 被夹 13.3%      退化
        所以阈值不能卡在 3×（那是我总结的经验说法，实测 2× 也完全可用），
        按实测健康区间取 **比值 >= 2** 作为下限。
        """
        import gui
        for preset, cfg in gui.Console.PRESET_PARAMS.items():
            sigma = float(cfg["noise"])
            limit = float(cfg["maxoff"])
            if limit == 0 or sigma == 0:
                continue      # 0 = 不限制 / 无噪声
            ratio = limit / sigma
            self.assertGreaterEqual(
                ratio, 2.0 - 1e-9,
                f"预设「{preset}」上限÷σ = {ratio:.2f} < 2，"
                f"实测这种搭配被夹占比会到 13%，误差分布退化成一个圆")

    def test_validate_route_params_rejects_garbage(self):
        """参数校验必须拦住非法值（不静默传下去）。

        注意：必须**自己**把 messagebox 换掉，不能依赖别的测试恰好 patch 过。
        `_validate_route_params()` 内部会弹 `showwarning` / `askyesno`，
        没被替换时是真弹窗 —— 会永久阻塞。实测：单独跑这个测试会挂死，
        而全套跑时"碰巧"能过（前一个测试留下的 patch 还没恢复）。
        这种顺序依赖是脆弱测试，必须消除。
        """
        import gui
        ui = self.ui
        names = ("speed", "interval", "noise", "corner",
                 "maxoff", "stepturn", "minsamp", "preset")
        orig = {n: getattr(ui, f"var_r_{n}").get() for n in names}
        shown = []
        o_warn = gui.messagebox.showwarning
        o_ask = gui.messagebox.askyesno
        try:
            # 任何弹窗都只记录，不显示（否则阻塞）
            gui.messagebox.showwarning = (
                lambda *a, **k: shown.append(" ".join(str(x) for x in a)))
            gui.messagebox.askyesno = (
                lambda *a, **k:
                shown.append("[ask] " + " ".join(str(x) for x in a)) or True)

            ui.var_r_preset.set("walk 步行")
            ui._on_r_preset()
            shown.clear()
            self.assertTrue(ui._validate_route_params())
            self.assertEqual(shown, [], f"正常参数不该弹窗：{shown}")

            # 非数字
            ui.var_r_speed.set("abc")
            self.assertFalse(ui._validate_route_params())
            self.assertTrue(any("速度" in m for m in shown),
                            f"应当提示「速度」有问题，实际：{shown}")
            shown.clear()
            ui.var_r_speed.set("1.35")

            # 超出范围
            ui.var_r_interval.set("5")          # 下限是 50
            self.assertFalse(ui._validate_route_params())
            self.assertTrue(any("采样" in m for m in shown),
                            f"应当提示「采样间隔」超范围，实际：{shown}")
            shown.clear()
            ui.var_r_interval.set("500")

            ui.var_r_maxoff.set("-3")           # 负数
            self.assertFalse(ui._validate_route_params())
            self.assertTrue(any("离路上限" in m for m in shown),
                            f"应当提示「离路上限」非法，实际：{shown}")
            shown.clear()
            ui.var_r_maxoff.set("3")

            self.assertTrue(ui._validate_route_params())
        finally:
            gui.messagebox.showwarning = o_warn
            gui.messagebox.askyesno = o_ask
            for n, v in orig.items():
                getattr(ui, f"var_r_{n}").set(v)
            self.root.update()

    def test_offset_and_corner_controls_are_separate(self):
        """回归：离路上限与拐弯参数必须在界面/命令里各自独立。

        真实问题：界面上「离路上限」和「单步转角」的标签、变量、输入框
        顺序错位（标签写了但输入框没建，两个输入框挂到了对方的变量上）。
        """
        import gui
        ui = self.ui
        # 四个参数各有自己的变量
        for attr in ("var_r_maxoff", "var_r_corner",
                     "var_r_stepturn", "var_r_minsamp"):
            self.assertTrue(hasattr(ui, attr), f"缺少 {attr}")

        # 改一个不能影响别的
        ui.var_r_maxoff.set("7")
        others = (ui.var_r_corner.get(), ui.var_r_stepturn.get(),
                  ui.var_r_minsamp.get())
        ui.var_r_corner.set("9")
        self.assertEqual(ui.var_r_maxoff.get(), "7",
                         "改拐角圆滑不该影响离路上限")

        # 命令里各带各的参数
        ui.var_r_from.set("39.907359,116.391263")
        ui.var_r_to.set("39.908700,116.397500")
        ui.var_r_via.set("")
        ui.var_r_maxoff.set("3")
        ui.var_r_stepturn.set("25")
        ui.var_r_minsamp.set("5")
        args = ui._route_args("out/t.json")
        self.assertEqual(float(args[args.index("--max-offset") + 1]), 3.0)
        self.assertEqual(float(args[args.index("--corner-step-turn") + 1]), 25.0)
        self.assertEqual(float(args[args.index("--corner-min-samples") + 1]), 5.0)
        # 收尾复原
        ui.var_r_preset.set("walk 步行")
        ui._on_r_preset()
        ui.var_r_corner.set(others[0])
        ui.var_r_stepturn.set(others[1])
        ui.var_r_minsamp.set(others[2])

    def test_endpoint_gap_detection(self):
        """起点终点距离检测：远的返回大距离，近的返回小距离，解析不了返回 None。"""
        import gui
        ui = self.ui
        ui.var_r_from.set("39.907359,116.391263")     # 天安门
        ui.var_r_to.set("39.879907,116.402872")       # 天坛
        g = ui._route_endpoint_gap()
        self.assertIsNotNone(g)
        self.assertGreater(g[2], 1000, "天安门到天坛应当超过 1km")

        ui.var_r_from.set("39.900000,116.400000")
        ui.var_r_to.set("39.900500,116.400000")       # 相距约 55m
        g2 = ui._route_endpoint_gap()
        self.assertIsNotNone(g2)
        self.assertLess(g2[2], 100)
        self.assertLessEqual(g2[2], gui.Console.LOOP_HINT_M,
                             "这么近应当触发绕圈询问")

        # 解析不了 -> None（不误报）
        ui.var_r_from.set("这不是坐标")
        self.assertIsNone(ui._route_endpoint_gap())
        # 复原
        ui.var_r_from.set("")
        ui.var_r_to.set("")

    def test_laps_dialog_reports_lap_distance_and_accepts_count(self):
        """绕圈对话框：必须告知一圈路程，并能取回用户填的圈数。"""
        import tkinter as tk
        import gui
        ui = self.ui
        # 假装地图上选了一圈（矩形）
        # 注意：_picked_points 是**列表属性**（地图选点），
        # _route_points 是**方法**（读 from/via/to）。两者别搞混 ——
        # 之前正是把方法当属性用，导致一圈路程算不出来。
        ui._picked_points = [
            (39.9000, 116.4000), (39.9000, 116.4020),
            (39.9020, 116.4020), (39.9020, 116.4000), (39.9000, 116.4000),
        ]
        captured = {}
        real = gui.Console._laps_dialog

        def spy(self, gap):
            def auto():
                dlg = None
                for w in self.win.winfo_children():
                    if isinstance(w, tk.Toplevel) and w.title() == "绕圈设置":
                        dlg = w
                        break
                if dlg is None:
                    self.win.after(80, auto)
                    return
                texts = []

                def walk(w):
                    for c in w.winfo_children():
                        try:
                            t = c.cget("text")
                            if t:
                                texts.append(str(t))
                        except Exception:                            # noqa: BLE001
                            pass
                        if c.__class__.__name__ == "Entry":
                            c.delete(0, "end")
                            c.insert(0, "7")
                        walk(c)
                walk(dlg)
                captured["texts"] = texts
                dlg.update()

                def press(w):
                    for c in w.winfo_children():
                        try:
                            # 去首尾空格再比 —— 按钮文本是 "  确定  "（加空格美化）
                            if str(c.cget("text")).strip() == "确定":
                                c.invoke()
                                return True
                        except Exception:                                # noqa: BLE001
                            pass
                        if press(c):
                            return True
                    return False
                self.win.after(300, lambda: (press(dlg), dlg.destroy()))
            self.win.after(150, auto)
            return real(self, gap)

        gui.Console._laps_dialog = spy
        try:
            got = ui._laps_dialog(55.0)
        finally:
            gui.Console._laps_dialog = real
        self.assertEqual(got, 7, "应当取回用户填的圈数")
        joined = " ".join(captured.get("texts", []))
        # 必须告知一圈路程
        self.assertIn("一圈路程", joined, "对话框必须告知一圈路程")
        self.assertTrue(("米" in joined) or ("km" in joined),
                        "一圈路程应当带单位")

    def test_lap_distance_is_actually_computed(self):
        """回归：绕圈对话框必须有**真实**的一圈路程，不能停在「正在估算…」。

        真实 bug：估算时写成 `getattr(self, "_route_points", None)`，而
        `_route_points` 是个**方法**不是属性 —— 取到的是方法对象，下一行
        `len(pts)` 直接 TypeError，被 except 静默吞掉，`_lap_m` 永远是 0，
        于是对话框一直显示「正在估算…」。用户看到的现象是
        「卡在计算路程这一步，所以没出现确定按钮」。

        教训：这种「异常被吞掉、退化成默认值」的写法，必须有一条测试
        直接断言**算出来的数字是对的**，光断言「不崩」是抓不住的。
        """
        from mockroute import geo
        ui = self.ui
        ring = [(39.9000, 116.4000), (39.9000, 116.4020),
                (39.9020, 116.4020), (39.9020, 116.4000), (39.9000, 116.4000)]
        expect = geo.total_length(ring)
        self.assertGreater(expect, 500)

        # 1) _picked_points 必须是列表属性，_route_points 必须是可调用方法
        ui._picked_points = list(ring)
        self.assertIsInstance(ui._picked_points, list)
        self.assertTrue(callable(ui._route_points),
                        "_route_points 是方法，不能被当属性覆盖/读取")

        # 2) 复刻对话框里的估算逻辑，断言算出来的就是真实长度
        pts = list(getattr(ui, "_picked_points", None) or [])
        self.assertGreaterEqual(len(pts), 2)
        lap_m = geo.total_length([(float(p[0]), float(p[1])) for p in pts])
        self.assertAlmostEqual(lap_m, expect, delta=1.0,
                               msg="一圈路程必须等于选点折线的真实长度")

        # 3) 对话框里确实会把这个数字显示出来（不是 0、不是空）
        captured = {}
        import tkinter as tk
        import gui

        # 跑真实对话框，自动抓标签文本后关掉
        real = gui.Console._laps_dialog

        def spy(self2, gap):
            def grab():
                dlg = next((w for w in self2.win.winfo_children()
                            if isinstance(w, tk.Toplevel)
                            and w.title() == "绕圈设置"), None)
                if dlg is None:
                    self2.win.after(80, grab)
                    return
                texts = []

                def walk(w):
                    for c in w.winfo_children():
                        try:
                            t = c.cget("text")
                            if t:
                                texts.append(str(t))
                        except Exception:                            # noqa: BLE001
                            pass
                        walk(c)
                walk(dlg)
                captured["texts"] = texts
                self2.win.after(200, dlg.destroy)
            self2.win.after(150, grab)
            return real(self2, gap)

        gui.Console._laps_dialog = spy
        try:
            ui._laps_dialog(55.0)
        finally:
            gui.Console._laps_dialog = real
        joined = " ".join(captured.get("texts", []))
        self.assertNotIn("正在估算", joined,
                         "不该停在「正在估算…」—— 说明估算那步又出错了")
        self.assertIn("一圈路程", joined)
        # 显示的数字应当与真实长度一致（允许四舍五入）
        self.assertIn(f"{expect:.0f} 米", joined,
                      f"对话框应显示一圈 {expect:.0f} 米，实际文字：{joined}")
        # 清干净
        ui._picked_points = []

    def test_picked_points_is_not_the_route_points_method(self):
        """防止把「方法」当成「属性」用（这是那次 bug 的根因）。"""
        import gui
        ui = self.ui
        # 属性与方法同名会互相覆盖，这里断言两者是不同的东西
        self.assertTrue(callable(ui._route_points))
        self.assertIsInstance(ui._picked_points, list)
        # _picked_points 里应当是 (lat, lon) 元组，不是字符串
        ui._picked_points = [(39.9, 116.4), (39.9, 116.402)]
        for p in ui._picked_points:
            self.assertEqual(len(p), 2)
            float(p[0]); float(p[1])
        ui._picked_points = []

    def test_corner_mode_selector_in_panel(self):
        """面板上必须有圆滑策略选择器，且三档都能选、命令能带上。"""
        import gui
        ui = self.ui
        self.assertTrue(hasattr(ui, "var_r_cornermode"),
                        "面板缺少圆滑策略选择器")
        self.assertEqual(len(gui.CORNER_MODES), 3,
                         "应当是三档：auto / round / none")
        ui.var_r_from.set("39.907359,116.391263")
        ui.var_r_to.set("39.908700,116.397500")
        ui.var_r_via.set("")
        for disp, key in gui.CORNER_MODES.items():
            ui.var_r_cornermode.set(disp)
            ui._on_corner_mode()
            self.root.update()
            args = ui._route_args("out/t.json")
            self.assertIn("--corner-mode", args)
            self.assertEqual(args[args.index("--corner-mode") + 1], key)
        # none 模式应当禁用半径输入框（半径此时无意义）
        for disp, key in gui.CORNER_MODES.items():
            if key != "none":
                continue
            ui.var_r_cornermode.set(disp)
            ui._on_corner_mode()
            self.root.update()
            self.assertIn("disabled", ui.ent_r_corner.state())
            self.assertIn("100%", ui.var_r_cornermode_hint.get())
        # 换回 auto 后应当恢复可编辑
        for disp, key in gui.CORNER_MODES.items():
            if key == "auto":
                ui.var_r_cornermode.set(disp)
                break
        ui._on_corner_mode()
        self.assertNotIn("disabled", ui.ent_r_corner.state())

    def test_corner_mode_survives_preset_switch(self):
        """切换预设后模式必须同步刷新，hint 不能是空的。"""
        import gui
        ui = self.ui
        for preset in gui.PRESETS:
            ui.var_r_preset.set(preset)
            ui._on_r_preset()
            self.root.update()
            self.assertTrue(gui.CORNER_MODES.get(ui.var_r_cornermode.get()),
                            f"预设 {preset} 后模式无法识别")
            self.assertTrue(ui.var_r_cornermode_hint.get().strip(),
                            f"预设 {preset} 后提示为空")











class TestLogs(unittest.TestCase):
    """持久化运行日志：PC 端命令 + Android 端实现的关键约束。"""

    def test_logs_path_action(self):
        import mroute
        args = type("A", (), {})()
        args.adb = None
        args.serial = None
        args.quiet = True
        args.dry_run = True
        args.log_action = "path"
        self.assertEqual(mroute.cmd_logs(args), 0)

    def test_log_file_lives_in_external_private_dir(self):
        """日志必须落在 PC 用 adb 能直接读的位置（不需要 root）。

        App 的外部私有目录 /sdcard/Android/data/<pkg>/files 满足这个要求：
        分区存储下 App 不能随便写 /sdcard/ 根目录，但这里通过 adb 可读。
        """
        import re
        kt = (ROOT / "mockroute-android/app/src/main/java/com/mockroute/Protocol.kt")
        if not kt.exists():
            self.skipTest("Android 源码不在预期位置")
        text = kt.read_text(encoding="utf-8")
        self.assertIn('NAME = "mockroute.log"', text)
        m = re.search(r"fun file\(ctx: Context\): File = (.+)", text)
        self.assertIsNotNone(m, "找不到 LogFile.file 定义")
        self.assertIn("Protocol.appDir", m.group(1),
                      "日志要用外部私有目录，否则 PC 读不到")

    def test_log_line_carries_elapsed_realtime(self):
        """日志行必须带开机秒数。

        只写墙上时钟的话，遇到「设备休眠」「用户改系统时间」就分不清
        是时间跳了还是日志真断了。加上 elapsedRealtime 才能判断。
        """
        kt = (ROOT / "mockroute-android/app/src/main/java/com/mockroute/Protocol.kt")
        if not kt.exists():
            self.skipTest("Android 源码不在预期位置")
        text = kt.read_text(encoding="utf-8")
        self.assertIn("elapsedRealtime", text)
        self.assertIn("appendText", text)

    def test_log_file_rotates(self):
        """日志必须轮转，否则会无限增长。"""
        kt = (ROOT / "mockroute-android/app/src/main/java/com/mockroute/Protocol.kt")
        if not kt.exists():
            self.skipTest("Android 源码不在预期位置")
        text = kt.read_text(encoding="utf-8")
        self.assertIn("MAX_BYTES", text)
        self.assertIn("rotate", text)
        self.assertIn("BAK", text)

    def test_service_has_heartbeat_and_lifecycle_logging(self):
        """心跳与生命周期埋点不能被删掉。

        心跳（BEAT）是判断「进程被冻结/被杀」最直接的证据：
        正常每 30 秒一条，出现远超 30 秒的空洞就说明当时没被调度。
        onTaskRemoved 用来区分「被从最近任务划掉」和「正常退出」。
        """
        svc = (ROOT / "mockroute-android/app/src/main/java/com/mockroute/"
                      "MockLocationService.kt")
        if not svc.exists():
            self.skipTest("Android 源码不在预期位置")
        text = svc.read_text(encoding="utf-8")
        self.assertIn("HEARTBEAT_MS", text)
        self.assertIn('"BEAT"', text, "心跳标签应为 BEAT")
        self.assertIn("onTaskRemoved", text)
        self.assertIn('"LIFE"', text, "生命周期标签应为 LIFE")
        self.assertIn('"CMD"', text, "收到的命令应记入 CMD")
        self.assertIn('"ERR"', text, "异常应记入 ERR")


    def test_known_landmark_coordinates_are_verified(self):
        """对已知地标做一次「转换链正确性」的锁定。

        背景（值得记住的教训）：曾经把 `39.9087,116.3975` 当成「天安门」
        当基准用了很久，实际那是**皇史宬**，偏 552 米。因为没人验证过
        「这个坐标是不是真在那个地方」，错误一直没暴露。

        这里锁定天安门城楼经过验证的坐标，并顺带断言它**不是**那个坏值。
        校验手段见 tools/audit-aliases.py（反查 + 渲染带十字的底图）。
        """
        from mockroute import geo
        # 经 audit-aliases.py --reverse 反查为「天安门」、--render 肉眼确认
        tiananmen_wgs = (39.907359, 116.391263)
        glat, glon = geo.wgs84_to_gcj02(*tiananmen_wgs)
        # 高德底图上应落在这个 GCJ 位置附近
        self.assertAlmostEqual(glat, 39.908760, places=5)
        self.assertAlmostEqual(glon, 116.397504, places=5)

        # 那个曾经被误当成天安门的坏值，必须明确区别于正确值（差约 550 米）
        bad = (39.908700, 116.397500)
        self.assertGreater(geo.haversine(*bad, *tiananmen_wgs), 500,
                           "坏值与正确值应当显著不同 —— 这条断言在提醒："
                           "别再把这个坐标当天安门用")

        # 反向转换必须回到原值（往返稳定）
        back = geo.gcj02_to_wgs84(glat, glon)
        self.assertAlmostEqual(back[0], tiananmen_wgs[0], places=6)
        self.assertAlmostEqual(back[1], tiananmen_wgs[1], places=6)

class TestSavedPaths(unittest.TestCase):
    """保存的选点路径。与命名地点共用 locations.json，靠 kind 区分。"""

    def setUp(self):
        from mockroute import locations
        self.loc = locations
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_save_and_load_path(self):
        pts = [(39.9087, 116.3975), (39.8822, 116.4066), (39.9928, 116.3964)]
        self.loc.save_path(self.root, "上班", pts, note="测试", preset="run")
        paths = self.loc.load_paths(self.root)
        self.assertIn("上班", paths)
        self.assertEqual(paths["上班"]["kind"], "path")
        self.assertEqual(len(paths["上班"]["points"]), 3)
        self.assertEqual(paths["上班"]["preset"], "run")

    def test_path_and_alias_share_file_without_clash(self):
        """同一个文件里存地点和路径，各自只读到自己的那部分。

        这是兼容性关键：老的 locations.json 里只有地点（没有 kind 字段），
        新代码必须照样能读，而且不会把地点当成路径。
        """
        self.loc.save_alias(self.root, "家", 39.9087, 116.3975)
        self.loc.save_path(self.root, "上班路",
                           [(39.9087, 116.3975), (39.9928, 116.3964)])

        aliases = self.loc.load_aliases(self.root)
        paths = self.loc.load_paths(self.root)
        self.assertEqual(list(aliases), ["家"])
        self.assertEqual(list(paths), ["上班路"])
        # 地点不该出现在路径里，反之亦然
        self.assertNotIn("上班路", aliases)
        self.assertNotIn("家", paths)

    def test_legacy_file_without_kind_still_reads(self):
        """旧格式（只有 lat/lon，没有 kind）必须继续被识别为地点。"""
        p = self.loc.alias_path(self.root)
        p.write_text(json.dumps({
            "老地点": {"lat": 39.9087, "lon": 116.3975, "coord": "wgs84", "note": ""},
        }, ensure_ascii=False), encoding="utf-8")
        aliases = self.loc.load_aliases(self.root)
        self.assertIn("老地点", aliases)
        self.assertEqual(self.loc.load_paths(self.root), {})

    def test_path_to_cli_splits_from_via_to(self):
        pts = [(1.0, 1.0), (2.0, 2.0), (3.0, 3.0), (4.0, 4.0)]
        self.loc.save_path(self.root, "四点", pts)
        cli = self.loc.path_to_cli(self.loc.load_paths(self.root)["四点"])
        self.assertEqual(cli["from"], "1.000000,1.000000")
        self.assertEqual(cli["to"], "4.000000,4.000000")
        self.assertEqual(cli["via"], ["2.000000,2.000000", "3.000000,3.000000"])

    def test_path_needs_two_points(self):
        with self.assertRaises(self.loc.ResolveError):
            self.loc.save_path(self.root, "单点", [(39.9, 116.4)])

    def test_delete_path(self):
        self.loc.save_path(self.root, "x", [(1.0, 1.0), (2.0, 2.0)])
        self.assertTrue(self.loc.delete_path(self.root, "x"))
        self.assertFalse(self.loc.delete_path(self.root, "x"))
        self.assertEqual(self.loc.load_paths(self.root), {})

    def test_delete_path_does_not_touch_alias(self):
        """删路径不能误删同名地点，反之亦然。"""
        self.loc.save_alias(self.root, "同名", 39.9, 116.4)
        self.loc.save_path(self.root, "同名路", [(1.0, 1.0), (2.0, 2.0)])
        self.assertFalse(self.loc.delete_path(self.root, "同名"),
                         "同名的是地点，不是路径，不该被删")
        self.assertIn("同名", self.loc.load_aliases(self.root))


class TestCli(unittest.TestCase):
    def run_cli(self, *argv):
        """运行 CLI 并捕获 SystemExit。

        真实 CLI 用 SystemExit(2) 表示参数/输入错误，这是预期行为；
        测试里把它转成退出码，好断言错误码和提示信息。
        """
        import io
        from contextlib import redirect_stdout, redirect_stderr
        import mroute
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = mroute.main(list(argv))
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        return code, out.getvalue(), err.getvalue()

    def test_corner_mode_none_keeps_path_on_centerline(self):
        """`--corner-mode none` 必须做到「真路径 100% 贴道路中心线」。

        用户要求把这句话实现，并作为面板可选项。判据：
        **真路径**（不是上报坐标）到原始道路折线的偏离必须严格为 0。
        只查上报坐标是不够的 —— 那里面混了 GPS 噪声，看不出切角。

        实测对照（真实道路）：
          none  -> 切角 0.000m
          auto  -> 切角 0.749m
          round 6m -> 切角 1.760m
        """
        from mockroute import geo, route as route_mod

        res = route_mod.from_file(EXAMPLES / "road-to-tiantan.txt")
        raw = [(p[0], p[1]) for p in res.points]

        def path_for(mode, radius=6.0):
            if mode == "none":
                return list(raw)
            if mode == "round":
                return geo.round_corners(list(raw), radius_m=radius,
                                         min_turn_deg=25.0)
            return geo.round_corners(list(raw), radius_m=2.0, min_turn_deg=25.0)

        def cut(p):
            if len(p) < 2:
                return 0.0
            return max(
                min(geo._point_to_segment(q, raw[i-1], raw[i])
                    for i in range(1, len(raw)))
                for q in p)

        self.assertEqual(cut(path_for("none")), 0.0,
                         "none 模式的真路径必须与中心线完全重合（切角 0）")
        self.assertGreater(cut(path_for("round", 6.0)), 1.0,
                           "round 6m 应当有可见切角（用于对照）")

    def test_corner_mode_none_via_cli(self):
        """CLI 的 --corner-mode none 也要产生零切角的真路径。

        通过「生成轨迹的里程」间接验证：none 不切角 -> 里程最长。
        """
        import tempfile
        lengths = {}
        with tempfile.TemporaryDirectory() as td:
            for mode in ("none", "round", "auto"):
                out = Path(td) / f"{mode}.json"
                code, msg, _ = self.run_cli(
                    "gen", "--preset", "walk", "--provider", "file",
                    "--file", str(EXAMPLES / "road-to-tiantan.txt"),
                    "--corner-mode", mode, "--seed", "42",
                    "--out", str(out))
                self.assertEqual(code, 0, msg)
                d = json.loads(out.read_text(encoding="utf-8"))
                pts = [(p["lat"], p["lon"]) for p in d["points"]]
                lengths[mode] = geo.total_length(pts)
        # 不切角 -> 沿真实路径走满 -> 里程最长
        self.assertGreater(lengths["none"], lengths["round"],
                           f"none 的里程应长于 round：{lengths}")
        self.assertGreater(lengths["none"], lengths["auto"],
                           f"none 的里程应长于 auto：{lengths}")


    def test_laps_repeats_path_continuously(self):
        """绕圈：路径重复 N 圈，时间轴跨圈接续（每圈不重新起步）。

        用户需求：「如果起点和终点距离较近……询问是否以此路径绕圈，
        如果选择是，弹出窗口告知一圈路程，并让用户输入绕圈圈数」。
        """
        import math
        from mockroute import geo
        from mockroute.traj import GenOptions, generate, build_lap_path

        lat, lon = 39.9000, 116.4000
        side = 100.0
        dlat = side / 111320.0
        dlon = side / (111320.0 * math.cos(math.radians(lat)))
        ring = [(lat, lon), (lat, lon + dlon), (lat + dlat, lon + dlon),
                (lat + dlat, lon), (lat, lon)]
        one_lap = geo.total_length(ring)
        self.assertGreater(one_lap, 300)

        # build_lap_path 的形状
        merged, bounds = build_lap_path(ring, 3)
        self.assertEqual(len(bounds), 3)
        self.assertEqual(bounds[0], 0)
        self.assertEqual(len(merged), len(ring) * 3 - 2)

        lengths = {}
        for laps in (1, 2, 4):
            o = GenOptions(speed=2.5, interval_ms=1000, accel=0.5,
                           noise_sigma_m=0.0, pace_variation=0.0,
                           lead_in_points=0, seed=42, corner_radius_m=6.0,
                           max_offset_m=0.0, laps=laps)
            pts = generate(ring, o)
            q = [(s["lat"], s["lon"]) for s in pts]
            lengths[laps] = (len(pts), pts[-1]["t"] / 1000.0,
                             geo.total_length(q))
        # 点数与时长应当近似成正比（连续绕圈，不重新起步）
        r_n = lengths[4][0] / lengths[1][0]
        r_t = lengths[4][1] / lengths[1][1]
        r_l = lengths[4][2] / lengths[1][2]
        self.assertAlmostEqual(r_n, 4.0, delta=0.6,
                               msg=f"4 圈点数应约为 1 圈的 4 倍，实测 {r_n:.2f}")
        self.assertAlmostEqual(r_t, 4.0, delta=0.6,
                               msg=f"4 圈时长应约为 1 圈的 4 倍，实测 {r_t:.2f}")
        self.assertAlmostEqual(r_l, 4.0, delta=0.6,
                               msg=f"4 圈里程应约为 1 圈的 4 倍，实测 {r_l:.2f}")
        # 只有一次起步漂移（不每圈重来）
        self.assertLess(lengths[4][1], lengths[1][1] * 4.5)

    def test_laps_one_is_noop(self):
        """laps=1 时行为必须与不传完全一致。"""
        from mockroute import geo
        from mockroute.traj import GenOptions, generate

        path = [(39.9000 + i * 0.0005, 116.4000) for i in range(20)]
        path = geo.densify(path, max_segment_m=10.0)
        outs = {}
        for tag, laps in (("none", None), ("one", 1)):
            kw = {} if laps is None else {"laps": laps}
            o = GenOptions(speed=1.35, interval_ms=500, accel=0.22,
                           noise_sigma_m=1.0, acc_mean_m=6.0,
                           pace_variation=0.05, lead_in_points=3, seed=7,
                           corner_radius_m=6.0, max_offset_m=3.0, **kw)
            outs[tag] = generate(path, o)
        self.assertEqual(len(outs["none"]), len(outs["one"]))
        for a, b in zip(outs["none"], outs["one"]):
            self.assertAlmostEqual(a["lat"], b["lat"], places=9)
            self.assertAlmostEqual(a["lon"], b["lon"], places=9)
            self.assertEqual(a["t"], b["t"])

    def test_laps_cli_option_flows_through(self):
        """`--laps` 必须真的影响生成的里程。"""
        import math
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            ring = Path(td) / "ring.txt"
            lat, lon = 39.9000, 116.4000
            side = 100.0
            dlat = side / 111320.0
            dlon = side / (111320.0 * math.cos(math.radians(lat)))
            ring.write_text("\n".join(
                f"{la:.7f},{lo:.7f}" for la, lo in
                [(lat, lon), (lat, lon + dlon), (lat + dlat, lon + dlon),
                 (lat + dlat, lon), (lat, lon)]), encoding="utf-8")

            got = {}
            for laps in (1, 3):
                out = Path(td) / f"r{laps}.json"
                code, msg, _ = self.run_cli(
                    "gen", "--preset", "walk", "--provider", "file",
                    "--file", str(ring), "--laps", str(laps),
                    "--seed", "5", "--out", str(out))
                self.assertEqual(code, 0, msg)
                d = json.loads(out.read_text(encoding="utf-8"))
                got[laps] = (len(d["points"]), d["points"][-1]["t"])
            self.assertGreater(got[3][0], got[1][0] * 2.5,
                               "3 圈点数应明显多于 1 圈")
            self.assertGreater(got[3][1], got[1][1] * 2.5,
                               "3 圈时长应明显多于 1 圈")


    def test_synthetic_examples_are_labeled(self):
        """回归：合成几何的示例文件必须**在文件里写明**它不是真实道路。

        真实教训：`examples/path-demo.txt` 原本写着「一段真实道路形状的折线」，
        容易让人以为它是真路。它其实只是形状像路的合成折线，有一段甚至
        穿过故宫 —— 拿它生成轨迹会得到「只按走势行走、完全不在路上」的结果
        （用 check-onroad.py 实测 100% 的点远离真实道路 138m 以上）。

        这类误导会让用户以为工具坏了，所以必须在文件头和 examples/README.md
        里都明确标出来。
        """
        demo = EXAMPLES / "path-demo.txt"
        self.assertTrue(demo.exists())
        text = demo.read_text(encoding="utf-8")
        self.assertIn("合成", text, "path-demo.txt 必须标明是合成几何")
        self.assertIn("不是真实道路", text, "必须明确说它不是真实道路")
        # 不能再出现「真实道路形状」这种容易误读的表述
        self.assertNotIn("真实道路形状", text)

        rd = EXAMPLES / "README.md"
        self.assertTrue(rd.exists(), "examples/ 应当有 README 说明各文件性质")
        rtext = rd.read_text(encoding="utf-8")
        for need in ("真实道路", "合成", "check-onroad"):
            self.assertIn(need, rtext, f"examples/README.md 缺少「{need}」说明")

    def test_real_example_paths_are_not_synthetic(self):
        """真实示例（road-*.txt）应当由 export 生成、点足够描述道路。"""
        for name in ("road-tiananmen.txt", "road-to-tiantan.txt"):
            p = EXAMPLES / name
            self.assertTrue(p.exists(), f"缺少 {name}")
            pts = [ln for ln in p.read_text(encoding="utf-8").splitlines()
                   if ln.strip() and not ln.strip().startswith("#")]
            self.assertGreaterEqual(len(pts), 3, f"{name} 路径点太少")
            # 每行都要能解析成 纬度,经度
            for ln in pts:
                a, b = ln.split(",")
                float(a); float(b)
            # 文件头要说明来源
            head = p.read_text(encoding="utf-8")
            self.assertIn("WGS-84", head)

    def test_export_command_writes_reusable_path(self):
        """export 写出的文件必须能被 --provider file 直接吃回去。"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "p.txt"
            code, msg, _ = self.run_cli(
                "export", "--provider", "file",
                "--file", str(EXAMPLES / "path-demo.txt"),
                "--out", str(out))
            self.assertEqual(code, 0, msg)
            self.assertTrue(out.exists())
            text = out.read_text(encoding="utf-8")
            self.assertIn("WGS-84", text)
            # 吃回去再生成一条轨迹
            traj = Path(td) / "t.json"
            code, msg, _ = self.run_cli(
                "gen", "--provider", "file", "--file", str(out),
                "--seed", "3", "--out", str(traj))
            self.assertEqual(code, 0, msg)
            self.assertTrue(traj.exists())

    def test_no_point_exceeds_limit_including_leadin(self):
        """回归：**任何阶段**（含起步 lead-in）都不许有超出离路上限的点。

        真实问题：夹取原先写成 `if ... and not is_lead_in` —— 跳过了起步阶段。
        而上限设 1m 时 lead-in 的漂移半径还是默认 4m，于是开头几个点离路
        3.7m。用户实测发现「拐弯后与道路距离超过预设离路上限」。

        修法两条：
          1. lead-in 的漂移半径按上限收敛（min(半径, 上限*0.8)）
          2. 夹取对所有点生效，不再跳过任何阶段
        这条测试直接扫多组「上限 × 噪声」，检查超限点必须为 0。
        """
        from mockroute import geo, route as route_mod
        from mockroute.traj import GenOptions, generate

        res = route_mod.from_file(EXAMPLES / "path-demo.txt")
        raw = [(p[0], p[1]) for p in res.points]
        STEP = 1.35 * 0.5
        p = geo.round_corners(list(raw), radius_m=6.0, min_turn_deg=25.0,
                              arc_step_m=max(1.0, STEP / 4.0))
        path = geo.densify(p, max_segment_m=max(2.0, min(25.0, STEP / 4.0)))

        for noise in (0.5, 1.0, 2.0, 3.0):
            for limit in (1.0, 2.0, 4.0):
                for seed in (1, 42):
                    opts = GenOptions(speed=1.35, interval_ms=500, accel=0.22,
                                      noise_sigma_m=noise, noise_alpha=0.99,
                                      acc_mean_m=6.0, pace_variation=0.1,
                                      lead_in_points=3, seed=seed,
                                      corner_radius_m=6.0,
                                      max_offset_m=limit)
                    pts = generate(path, opts)
                    worst = 0.0
                    for s in pts:
                        d = min(geo._point_to_segment((s["lat"], s["lon"]),
                                                      path[i - 1], path[i])
                                for i in range(1, len(path)))
                        worst = max(worst, d)
                    # 容差 2%：夹是按「到 along 处的中心点」算的，
                    # 与「到折线的垂距」有微小差异
                    self.assertLessEqual(
                        worst, limit * 1.02,
                        f"σ={noise} 上限={limit} seed={seed} 时最远 "
                        f"{worst:.2f}m 超出上限")

    def test_leadin_radius_respects_offset_limit(self):
        """lead-in 的漂移半径必须随离路上限收敛，否则两个参数自相矛盾。"""
        from mockroute import route as route_mod
        from mockroute.traj import GenOptions, generate

        res = route_mod.from_file(EXAMPLES / "path-demo.txt")
        path = [(p[0], p[1]) for p in res.points]
        # 上限 1m、漂移半径仍给 4m —— 修好后实际漂移应被收敛到 0.8m 内
        opts = GenOptions(speed=1.35, interval_ms=500, noise_sigma_m=2.0,
                          acc_mean_m=6.0, lead_in_points=3,
                          lead_in_radius_m=4.0, max_offset_m=1.0, seed=3)
        pts = generate(path, opts)
        head = (path[0][0], path[0][1])
        lead = [(s["lat"], s["lon"]) for s in pts[:3]]
        worst = max(geo.haversine(head[0], head[1], q[0], q[1]) for q in lead)
        self.assertLessEqual(worst, 1.02,
                             f"起步漂移最远 {worst:.2f}m，应当被收敛到 1m 内")

    def test_offset_limit_sweep_no_overlimit(self):
        """多预设扫描：三个预设都不该有超限点。"""
        from mockroute import geo, route as route_mod
        from mockroute.traj import GenOptions, generate

        res = route_mod.from_file(EXAMPLES / "path-demo.txt")
        raw = [(p[0], p[1]) for p in res.points]
        for preset, speed, iv, acc in (("walk", 1.35, 500, 0.22),
                                       ("run", 3.3, 1000, 0.9)):
            step = speed * iv / 1000.0
            p = geo.round_corners(list(raw), radius_m=6.0, min_turn_deg=25.0,
                                  arc_step_m=max(1.0, step / 4.0))
            path = geo.densify(p, max_segment_m=max(2.0, min(25.0, step / 4.0)))
            opts = GenOptions(speed=speed, interval_ms=iv, accel=acc,
                              noise_sigma_m=2.0, acc_mean_m=6.0,
                              pace_variation=0.12, lead_in_points=3,
                              corner_radius_m=6.0, max_offset_m=2.0, seed=9)
            pts = generate(path, opts)
            worst = max(
                min(geo._point_to_segment((s["lat"], s["lon"]),
                                          path[i - 1], path[i])
                    for i in range(1, len(path)))
                for s in pts)
            self.assertLessEqual(worst, 2.0 * 1.02,
                                 f"{preset}: 最远 {worst:.2f}m 超出上限 2m")

    def test_offsets_stay_inside_road_corridor(self):
        """回归：上报坐标不得偏出「路廊」。

        用户要求「虚拟定位写死在道路上」。不加约束时实测最远偏到离路面
        7.71 m（落到建筑/绿化带里）—— 那是 GPS 噪声造成的。
        现在默认 max_offset_m=4：把「坐标到路径的距离」硬夹到 4m 以内。

        这条测试直接检查生成结果：每个点到路径的垂直距离都 <= 上限。
        """
        from mockroute import geo
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            for limit in (4.0, 2.5):
                for preset in ("walk", "run"):
                    out = Path(td) / f"{preset}-{limit}.json"
                    code, msg, _ = self.run_cli(
                        "gen", "--preset", preset, "--provider", "file",
                        "--file", str(EXAMPLES / "path-demo.txt"),
                        "--seed", "11", "--max-offset", str(limit),
                        "--out", str(out))
                    self.assertEqual(code, 0, msg)
                    d = json.loads(out.read_text(encoding="utf-8"))
                    pts = [(p["lat"], p["lon"]) for p in d["points"]]
                    # 用「相邻点连成的折线」近似路径（生成时夹的是它）
                    worst = 0.0
                    for q in pts:
                        dd = min(geo._point_to_segment(q, pts[i - 1], pts[i])
                                 for i in range(1, len(pts)))
                        worst = max(worst, dd)
                    # 相邻点自身到折线的距离为 0，所以这里的上界由
                    # 非相邻点决定；给 2.5 倍余量容忍折线近似误差
                    self.assertLess(worst, limit * 2.5 + 1.0,
                                    f"{preset} 上限{limit}m 时最远偏 {worst:.2f}m")

    def test_max_offset_zero_keeps_pure_noise(self):
        """--max-offset 0 时应当回到「纯噪声」行为（不夹）。"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            outs = {}
            for tag, extra in (("on", ["--max-offset", "2"]),
                               ("off", ["--max-offset", "0"])):
                out = Path(td) / f"{tag}.json"
                code, msg, _ = self.run_cli(
                    "gen", "--preset", "walk", "--provider", "file",
                    "--file", str(EXAMPLES / "path-demo.txt"),
                    "--noise", "3.0", "--seed", "5",
                    *extra, "--out", str(out))
                self.assertEqual(code, 0, msg)
                outs[tag] = json.loads(out.read_text(encoding="utf-8"))
            # 两者点数应当一致（夹只是挪位置，不改变采样节奏）
            self.assertEqual(len(outs["on"]["points"]),
                             len(outs["off"]["points"]))
            # 关闭时应当出现更大的偏离（噪声没被夹）
            from mockroute import geo

            def maxdev(d):
                pts = [(p["lat"], p["lon"]) for p in d["points"]]
                b = pts[0]
                return max(geo.haversine(b[0], b[1], p[0], p[1]) for p in pts)

            self.assertGreaterEqual(maxdev(outs["off"]), maxdev(outs["on"]) * 0.9,
                                    "关闭上限时偏离不应明显更小")

    def test_clamping_keeps_endpoints_exact(self):
        """夹不能破坏起终点 —— 轨迹必须仍然从起点出发、在终点结束。"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "e.json"
            code, msg, _ = self.run_cli(
                "gen", "--preset", "walk", "--provider", "file",
                "--file", str(EXAMPLES / "path-demo.txt"),
                "--noise", "3.0", "--max-offset", "3",
                "--lead-in", "0", "--out", str(out))
            self.assertEqual(code, 0, msg)
            d = json.loads(out.read_text(encoding="utf-8"))
            pts = d["points"]
            # 起终点附近应当仍在路径端点附近（不能因为夹而大幅平移）
            self.assertLess(geo.haversine(pts[0]["lat"], pts[0]["lon"],
                                          pts[-1]["lat"], pts[-1]["lon"]), 5000)
            # t 必须单调不减（夹不能破坏时间轴）
            ts = [p["t"] for p in pts]
            self.assertTrue(all(ts[i] >= ts[i - 1] for i in range(1, len(ts))))

    def test_circle_accepts_acc(self):
        """circle/goto/point 都要能指定上报精度。

        目标 App 报「GPS信号弱」时，把它调小是最直接的缓解手段。
        """
        code, out, _ = self.run_cli("circle", "--at", "39.9,116.4",
                                    "--radius", "30", "--period", "60",
                                    "--acc", "1.5", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("--ef acc 1.5", out)
        code, out, _ = self.run_cli("goto", "--at", "39.92,116.41",
                                    "--speed", "1.4", "--acc", "2.0", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("--ef acc 2.0", out)

    def test_direct_mode_forwards_route_accuracy(self):
        """直接模式必须逐点转发轨迹文件里的 acc，而不是一律用默认值。

        真实缺口：--direct 逐点广播，但 acc 写死，导致设备回放精度正常、
        直推模式精度很差 —— 目标 APP 会因此报「GPS信号弱」。

        直接构造一个假 Player 来抓实际发出的 extras（dry-run 不会逐点广播）。
        """
        from mockroute.player import Player
        sent = []

        class Fake(Player):
            def __init__(self):
                pass

            def shell(self):
                class S:
                    @staticmethod
                    def run(*a, **k):
                        return ""
                return S()

            def send_fast(self, action, extras):
                sent.append(extras)
                return ""

        route = {
            "version": 1, "mode": "once", "interval": 1,
            "points": [
                {"t": 0.0, "lat": 39.9, "lon": 116.4, "speed": 1.4,
                 "bearing": 0.0, "acc": 1.25, "alt": 40.0},
                {"t": 0.01, "lat": 39.9001, "lon": 116.4001, "speed": 1.4,
                 "bearing": 10.0, "acc": 1.75, "alt": 40.0},
            ],
        }
        Fake().play_direct(route, progress_every=0)
        self.assertEqual(len(sent), 2)
        self.assertAlmostEqual(sent[0]["acc"], 1.25)
        self.assertAlmostEqual(sent[1]["acc"], 1.75)

    def test_direct_mode_time_unit_is_milliseconds(self):
        """回归：direct 的 points[].t 是**毫秒**，不能当秒再 ×1000。

        这是个我**自己犯过**的错：误以为 t 是秒，在这里乘了 1000，
        结果整条路线慢 1000 倍（1399 秒的路线要跑 15 天）。
        traj.py 写入时是 `int(round(ti * 1000))`，单位是毫秒。

        检查时先剥掉注释，否则注释里提到的错误写法会误报。
        """
        import inspect
        import io as _io
        import tokenize
        from mockroute import player as player_mod

        src = inspect.getsource(player_mod.Player.play_direct)
        toks = [t for t in tokenize.generate_tokens(_io.StringIO(src).readline)
                if t.type != tokenize.COMMENT]
        # 把相邻 token 直接拼起来（不加空格），这样 p["t"] / mult 会还原成原文
        code = ""
        prev_end = (0, 0)
        for t in toks:
            if t.start[0] == prev_end[0] and t.start[1] > prev_end[1]:
                pass
            code += t.string
            prev_end = t.end
        self.assertIn('p["t"]/mult', code, "t 是毫秒，直接除以倍速即可")
        self.assertNotIn('p["t"]*1000', code, "不能再乘 1000（那是把毫秒当秒）")

    def test_time_unit_matches_schema(self):
        """用真实生成的轨迹验证 t 的单位是**毫秒**。

        这是上面那条 bug 的**行为级**验证 —— 不依赖源码写法。
        点数 × 采样间隔(ms) 应约等于 t 的跨度(ms)。
        """
        import json as _json
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "r.json"
            code, out, _ = self.run_cli(
                "gen", "--provider", "file",
                "--file", str(EXAMPLES / "path-demo.txt"),
                "--speed", "2.0", "--noise", "0", "--pace-variation", "0",
                "--seed", "4", "--out", str(p))
            self.assertEqual(code, 0, out)
            d = _json.loads(p.read_text(encoding="utf-8"))
            span_ms = d["points"][-1]["t"] - d["points"][0]["t"]
            n = len(d["points"])
            interval_ms = d.get("interval", 1000)
            expect_ms = (n - 1) * interval_ms
            self.assertAlmostEqual(span_ms, expect_ms,
                                   delta=max(1000.0, expect_ms * 0.05))
            # 顺带断言：绝不该是「秒」（否则会小 1000 倍）
            self.assertGreater(span_ms, expect_ms / 2.0)

    def test_parse_coord_forms(self):
        """parse_coord 故意宽松：认出坐标就返回元组，认不出就原样返回字符串。

        因为 --from/--to 现在也接受命名地点和地名，而 argparse 的 type 回调
        发生在所有参数就绪之前 —— 在这里就报错的话就没法写 --from 天安门。
        """
        import argparse
        import mroute
        for s in ("39.9,116.4", "39.9 116.4", "39.9/116.4"):
            self.assertEqual(mroute.parse_coord(s), (39.9, 116.4))
        # 认不出的原样放行，交给位置解析链（见 mockroute/locations.py）
        self.assertEqual(mroute.parse_coord("天安门"), "天安门")
        self.assertEqual(mroute.parse_coord("abc,def"), "abc,def")
        self.assertEqual(mroute.parse_coord("39.9"), "39.9")
        # 只有空值才在参数层直接拒绝
        with self.assertRaises(argparse.ArgumentTypeError):
            mroute.parse_coord("")
        with self.assertRaises(argparse.ArgumentTypeError):
            mroute.parse_coord("   ")

    def test_gen_from_file_and_show(self):
        with tempfile.TemporaryDirectory() as td:
            out_file = Path(td) / "r.json"
            code, out, err = self.run_cli(
                "gen", "--provider", "file",
                "--file", str(EXAMPLES / "path-demo.txt"),
                "--speed", "1.4", "--seed", "5", "--out", str(out_file),
            )
            self.assertEqual(code, 0, err)
            self.assertTrue(out_file.exists())
            self.assertIn("已写出", out)

            data = json.loads(out_file.read_text(encoding="utf-8"))
            self.assertEqual(traj.validate(data), [])

            code2, out2, _ = self.run_cli("show", "--route", str(out_file), "--head", "2")
            self.assertEqual(code2, 0)
            self.assertIn("点数=", out2)

    def test_walk_preset(self):
        """--preset walk 应当同时给出细采样、低加速度和 GPS 噪声。"""
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "walk.json"
            code, _, err = self.run_cli(
                "gen", "--preset", "walk", "--provider", "file",
                "--file", str(EXAMPLES / "path-demo.txt"),
                "--seed", "3", "--out", str(out))
            self.assertEqual(code, 0, err)
            data = json.loads(out.read_text(encoding="utf-8"))
            # 预设应当把采样间隔细化到 500ms
            self.assertEqual(data["interval"], 500)
            self.assertEqual(traj.validate(data), [])
            self.assertGreater(len(data["points"]), 100)
            # 步行速度、且带 GPS 噪声（坐标不会完美落在解析路径上）
            speeds = [p["speed"] for p in data["points"]]
            self.assertLess(max(speeds), 6.0)
            self.assertLess(sum(speeds) / len(speeds), 2.5)

    def test_gen_skips_preset_when_explicit(self):
        """显式传入的参数不应被预设覆盖。"""
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "w.json"
            code, _, err = self.run_cli(
                "gen", "--preset", "walk", "--provider", "file",
                "--file", str(EXAMPLES / "path-demo.txt"),
                "--interval", "1000", "--speed", "5.0",
                "--seed", "3", "--out", str(out))
            self.assertEqual(code, 0, err)
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(data["interval"], 1000)

    def test_gen_requires_endpoints_for_osrm(self):
        code, out, err = self.run_cli("gen", "--provider", "osrm",
                                      "--from", "39.9,116.4", "--out", "x.json")
        self.assertEqual(code, 2)
        self.assertIn("--to", err)

    def test_gen_file_provider_requires_file(self):
        code, out, err = self.run_cli("gen", "--provider", "file", "--out", "x.json")
        self.assertEqual(code, 2)
        self.assertIn("--file", err)

    def test_play_missing_route_message(self):
        code, out, err = self.run_cli("play", "--route", "no/such/file.json")
        self.assertEqual(code, 2)
        self.assertIn("不存在", err)
        self.assertIn("mroute.py gen", err)

    def test_play_dry_run_dumps_commands(self):
        with tempfile.TemporaryDirectory() as td:
            out_file = Path(td) / "r.json"
            self.run_cli("gen", "--provider", "file",
                         "--file", str(EXAMPLES / "path-demo.txt"),
                         "--seed", "1", "--out", str(out_file))
            code, out, _ = self.run_cli(
                "play", "--route", str(out_file), "--dry-run", "--quiet",
            )
            self.assertEqual(code, 0, out)
            self.assertIn("am broadcast", out)
            self.assertIn(protocol.ACTION_START_ROUTE, out)
            self.assertIn("adb push", out)

    def test_point_dry_run(self):
        code, out, _ = self.run_cli("point", "--at", "39.9,116.4", "--dry-run",
                                    "--quiet")
        self.assertEqual(code, 0, out)
        self.assertIn(protocol.ACTION_SET, out)
        self.assertIn("--ef", out)

    def test_circle_dry_run_reports_speed(self):
        code, out, _ = self.run_cli("circle", "--at", "39.9,116.4",
                                    "--radius", "100", "--period", "60",
                                    "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn(protocol.ACTION_START_CIRCLE, out)
        self.assertIn("10.47", out)   # 2π·100/60 ≈ 10.47 m/s

    def test_mode_override_in_dry_run(self):
        with tempfile.TemporaryDirectory() as td:
            out_file = Path(td) / "r.json"
            self.run_cli("gen", "--provider", "file",
                         "--file", str(EXAMPLES / "route-loop.geojson"),
                         "--seed", "1", "--out", str(out_file))
            code, out, _ = self.run_cli("play", "--route", str(out_file),
                                        "--mode", "pingpong", "--dry-run",
                                        "--quiet")
            self.assertEqual(code, 0, out)
            self.assertIn("pingpong", out)

    def test_show_rejects_invalid_route(self):
        with tempfile.TemporaryDirectory() as td:
            bad = Path(td) / "bad.json"
            bad.write_text('{"points":[{"t":100,"lat":39.9,"lon":116.4},'
                           '{"t":50,"lat":39.9,"lon":116.4}]}', encoding="utf-8")
            code, out, err = self.run_cli("show", "--route", str(bad))
            self.assertEqual(code, 2)
            self.assertIn("校验失败", err)


if __name__ == "__main__":
    unittest.main(verbosity=2)
