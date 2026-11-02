"""轨迹生成：把折线路径变成带时间戳、速度、方位角的可回放轨迹。

生成算法的要点（决定了模拟像不像真的）：

1. 速度剖面用加速度约束做一阶滤波，起步/刹车是渐变的，不是瞬间满速；
2. 速度叠加小幅随机游走，避免输出恒定值这个典型特征；
3. 坐标按「沿路径推进的距离」插值，因此点间距自然随速度变化；
4. bearing 由相邻坐标的真实位移方向得到，与坐标严格自洽；
5. speed 由「位移 / 时间」反推，与坐标严格自洽（目标 App 就是这么校验的）；
6. accuracy 用 Weibull 分布采样，起步阶段（GPS 刚锁星）精度更差；
7. 可选 lead-in：在起点附近先漂几个点，模拟真实设备定位收敛的过程；
8. 可选 GPS 噪声：AR(1) 时间相关误差，而不是白噪声（见 GpsNoise）。

关于「平滑」与「噪声」的取舍：
    真实 GPS 的定位误差是**高度时间相关**的 —— 相邻两次定位的误差几乎一样，
    整体表现为几米范围内的缓慢游走，而不是每个点独立乱跳。
    所以这里用一阶低通（AR(1)）生成误差序列。若改成独立白噪声，相邻点会各自
    随机偏移，轨迹反而呈锯齿状，比不加噪声更假。
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from . import geo

SCHEMA_VERSION = 1

# 单步位移的「弦长 / 弧长」最低比值。
# 目标 App 只能用相邻两点的直线距离反推速度，而我们的位置是按弧长插值的；
# 一步之内拐弯越急，弦长相对弧长越短，两边算出来的速度差得越多。
# 0.97 意味着这一步最多约 28° 的转角（弦/弧 = 2·sin(θ/2)/θ ≈ 0.97 @ θ≈28°），
# 已经把可察觉的差异压掉了，同时不会让过弯速度低得不合理。
MIN_CHORD_RATIO = 0.97


class GpsNoise:
    """时间相关的 GPS 定位误差（AR(1) 慢漂移）。

    模型：对每个轴维护一个一阶低通滤波的随机游走

        n[k] = alpha · n[k-1] + w[k],   w ~ N(0, sigma_w)

    稳态标准差是 sigma_w / √(1-alpha²)，所以按目标误差反解 sigma_w。
    alpha 越大误差越"粘"（相邻点越接近），这正是真实 GPS 的特征：
    接收机在一个时段内有几米的固定偏差，而不是每次定位都重新掷骰子。

    alpha = 0.85 时，误差的自相关长度约 6 个采样点 —— 1 Hz 采样下即 6 秒，
    与民用 GPS 的实际表现接近。
    """

    def __init__(self, rng: random.Random, sigma_m: float, alpha: float = 0.85):
        self.sigma = max(0.0, float(sigma_m))
        self.alpha = min(0.999, max(0.0, float(alpha)))
        self._nx = 0.0
        self._ny = 0.0
        # 让滤波从稳态附近开始，避免开头几米有个不自然的"归零"过程
        self._sigma_w = self.sigma * math.sqrt(max(1e-9, 1.0 - self.alpha ** 2))
        if self.sigma > 0:
            for _ in range(24):
                self.step(rng)

    def step(self, rng: random.Random) -> Tuple[float, float]:
        """推进一步，返回本采样点的误差 (东向米, 北向米)。"""
        if self.sigma <= 0:
            return 0.0, 0.0
        self._nx = self.alpha * self._nx + rng.gauss(0.0, self._sigma_w)
        self._ny = self.alpha * self._ny + rng.gauss(0.0, self._sigma_w)
        return self._nx, self._ny


def sample_accuracy(rng: random.Random, mean_m: float) -> float:
    """采样一个上报精度值（米）。

    真实设备上报的 accuracy 不是均匀分布也不是严格高斯：多数时候不错，
    偶尔明显变差。用 Weibull(k=2) 近似这个形状 —— 均值给 mean_m，
    中位数约为 0.83·mean，长尾能到 2~3 倍。
    """
    mean_m = max(0.5, float(mean_m))
    k = 2.0
    lam = mean_m / math.gamma(1.0 + 1.0 / k)
    return lam * (-math.log(max(1e-12, 1.0 - rng.random()))) ** (1.0 / k)


class Elevation:
    """合成路线海拔剖面。

    OSRM 的演示服务器不返回海拔，而跑步对坡度比走路敏感得多（跑者常说的
    「爬升」直接影响配速），所以这里自己生成一条平滑的虚拟地形。

    模型 = 长期缓变漂移 + 两个不同波长的正弦起伏 + 一点高频抖动。
    这样得到的坡度是连续的，不会出现"瞬间爬升 50 米"这种物理不可能的情况。

    ⚠️ 这是**合成地形**，不是真实海拔。若要真实爬升数据，请用带海拔的
    GPX 文件（手机/手表导出的轨迹都自带 <ele>），或改用返回海拔的规划服务。
    """

    #: 每爬升 1 米大约多花多少秒（跑者经验值约 0.3~0.6 s/m，
    #: 换算到「速度倍率」时用它做线性近似）
    SEC_PER_METER_CLIMB = 0.45

    def __init__(self, rng: random.Random, total_m: float, base_m: float = 45.0,
                 amplitude_m: float = 0.0):
        self.base = base_m
        self.amp = max(0.0, amplitude_m)
        # 波长用**绝对尺度**，不随路线长度缩放 —— 真实地形的起伏尺度是固定的
        # （几百米的小起伏、一两公里的长坡），跑 1km 和跑 20km 遇到的地形特征
        # 是同一类。若按 total/N 缩放，短路线会得到极陡的"假山"。
        # 同时用 min 保证短路线还能看到至少一个完整起伏。
        self.w1 = min(max(300.0, total_m * 0.8), 1600.0)   # 长坡
        self.w2 = min(max(120.0, total_m * 0.3), 420.0)    # 短起伏
        self.p1 = rng.uniform(0, 2 * math.pi)
        self.p2 = rng.uniform(0, 2 * math.pi)
        self.amp1 = 0.68
        self.amp2 = 0.32
        # 低频漂移模拟"整体上/下坡"
        self._drift = 0.0
        self._drift_sigma = 0.0
        self._last_s = None
        self._last_z = self.at(0.0)
        if amplitude_m > 0:
            self._drift_sigma = amplitude_m * 0.10

    def at(self, s: float) -> float:
        """路线里程 s 米处的海拔（米）。"""
        if self.amp <= 0:
            return self.base
        z = (self.base
             + self.amp * self.amp1 * math.sin(2 * math.pi * s / self.w1 + self.p1)
             + self.amp * self.amp2 * math.sin(2 * math.pi * s / self.w2 + self.p2)
             + self._drift)
        return z

    def step(self, rng: random.Random, s: float) -> float:
        """推进到里程 s，返回该处海拔。内部维护漂移项。"""
        if self.amp <= 0:
            self._last_s = s
            return self.base
        if self._last_s is not None and self._drift_sigma > 0:
            self._drift += rng.gauss(0.0, self._drift_sigma)
            # 把漂移约束在 ±amp 内，避免越走越偏
            self._drift = max(-self.amp, min(self.amp, self._drift))
        self._last_s = s
        z = self.at(s)
        self._last_z = z
        return z

    def grade(self, s: float, window_m: float = 30.0) -> float:
        """里程 s 处的坡度（上升米 / 水平米）。

        用前后各 window_m/2 的差分算，避免单点差分被高频起伏放大。
        """
        if self.amp <= 0:
            return 0.0
        lo = max(0.0, s - window_m / 2)
        hi = s + window_m / 2
        dz = self.at(hi) - self.at(lo)
        return dz / max(1.0, hi - lo)

    def pace_factor(self, s: float) -> float:
        """坡度对配速的影响倍率（1.0 = 平路）。

        上坡变慢、下坡略快（但下坡的收益小于上坡的损失，符合跑者的真实体验）。
        """
        g = self.grade(s)
        if g > 0:
            # 上坡：每 1% 坡度约慢 1.6%（近似 0.45 s/m 爬升的经验值）
            factor = 1.0 / (1.0 + 1.6 * g)
        else:
            factor = 1.0 - 0.6 * abs(g)
        return max(0.55, min(1.35, factor))


class PaceVariation:
    """跑步的配速波动。

    跑步和步行最大的区别之一：步行的速度几乎是恒定的，而跑者每一段的配速都在
    变 —— 体能、路况、红绿灯、疲劳都会影响。实测跑者的分段配速标准差约为
    平均配速的 8%~15%。

    用几个不同波长的正弦叠加来生成，好处是：
      * 完全确定性（给定 seed 可复现）；
      * 曲线光滑，不会像随机游走那样出现突兀的加速；
      * 波长可调，能模拟"每公里配速起伏"到"间歇训练"。
    """

    def __init__(self, rng: random.Random, amplitude: float = 0.12,
                 period_m: float = 180.0):
        self.amp = max(0.0, amplitude)
        self.period = max(30.0, period_m)
        self.phase = rng.uniform(0, 2 * math.pi)
        # 第二、三个谐波错开相位，避免看起来像单一正弦
        self.phase2 = rng.uniform(0, 2 * math.pi)
        self.phase3 = rng.uniform(0, 2 * math.pi)
        # 三个谐波的波长。都有下限，因为跑者在很短的距离内改变不了配速：
        # 以 a=1.2 m/s²、v=3.3 m/s 计，dv/ds ≤ a/v ≈ 0.36 /m，
        # 也就是说幅度 12% 的波动至少要 0.12/0.36 ≈ 0.33m 才"物理可能"，
        # 但那样毫无平滑性可言；实测取 120m 下限既真实又平滑。
        # 此前用 period/5.7（180m 时只有 32m）会生成不可能的快速配速抖动。
        self.w1 = max(120.0, self.period)              # 主波
        self.w2 = max(120.0, self.period * 2.3)        # 中波
        self.w3 = max(120.0, self.period * 5.7)        # 慢趋势

    def at(self, s: float) -> float:
        """里程 s 处的速度倍率（1.0 = 按目标速度跑）。"""
        if self.amp <= 0:
            return 1.0
        k1, k2, k3 = (2 * math.pi / self.w1,
                      2 * math.pi / self.w2,
                      2 * math.pi / self.w3)
        v = (0.55 * math.sin(k1 * s + self.phase)
             + 0.30 * math.sin(k2 * s + self.phase2)
             + 0.15 * math.sin(k3 * s + self.phase3))
        return max(0.5, 1.0 + self.amp * v)


def _local_turn(path, cum, start: float, span: float) -> float:
    """从弧长 start 往前走 span 米的**方向净偏转**（度）。

    注意用「净偏转」而不是「累积转角」：缓弯上每个小折线段都有几度夹角，
    累积起来会很吓人，导致平缓的弯也被当成急弯、乱减速
    （实测过：用累积转角时阈值从 25° 调到 8°，点数反而忽多忽少、不单调）。

    净偏转 = 起点切线方向与终点切线方向的夹角，只反映「整体转了多少」。
    """
    total = cum[-1]
    end = min(start + span, total)
    span = end - start
    if span < 0.3:
        return 0.0
    probe = min(0.5, span)
    a0 = geo.point_at_distance(path, cum, start)
    a1 = geo.point_at_distance(path, cum, min(start + probe, total))
    b0 = geo.point_at_distance(path, cum, max(start, end - probe))
    b1 = geo.point_at_distance(path, cum, end)
    if geo.haversine(a0[0], a0[1], a1[0], a1[1]) < 0.05:
        return 0.0
    if geo.haversine(b0[0], b0[1], b1[0], b1[1]) < 0.05:
        return 0.0
    br_in = geo.bearing(a0[0], a0[1], a1[0], a1[1])
    br_out = geo.bearing(b0[0], b0[1], b1[0], b1[1])
    return abs((br_out - br_in + 540) % 360 - 180)


def _corner_extent(path, cum, start: float, span: float,
                   turn_deg: float = 45.0) -> float:
    """从 start 出发，找出**一个显著弯道**占用的弧长。

    逐步往前量「累积净偏转」，达到 turn_deg 时停下，返回用掉的弧长；
    找不到显著弯道返回 0。

    为什么要量「弯道占用多长」而不只是「前方有多弯」：弯道的**弧长**决定
    它需要多少个采样点才能被表达清楚。实测过一个反例 —— 8 m/s、1s 采样、
    半径 6m 的 90° 弯弧长只有 9.4m，而一步 8m，弯道内只落 1~2 个采样点，
    方向只能用一次突变表达；相位对不齐时甚至出现 168° 的「几乎折返」。
    这不是减速能解决的，必须保证弯道里有足够采样点。
    """
    total = cum[-1]
    end = min(start + span, total)
    if end - start < 0.5:
        return 0.0
    x = start
    while x < end:
        if _local_turn(path, cum, start, x - start) >= turn_deg:
            return x - start
        x += 0.5
    return 0.0


def _corner_shape(path, cum, start: float, span: float,
                  turn_deg: float = 30.0) -> Tuple[float, float]:
    """量出前方一个弯的**形状**：返回 (总转角°, 弧长 m)。找不到就返回 (0, 0)。

    为什么要单独量形状：原来决定减速的是「弯道内采样点够不够」这个**代理**
    指标，而不是弯本身的几何。实测半径 1m 的 90° 弯被压到 0.30 m/s
    （行进速度的 9%），一个 1.6m 的弯要走 5 秒 —— 太夸张。
    真正该决定速度的是**曲率半径**：转弯时横向加速度 a = v²/r 有上限。
    """
    total = cum[-1]
    end = min(start + span, total)
    if end - start < 0.5:
        return 0.0, 0.0
    x = start
    while x < end:
        t = _local_turn(path, cum, start, x - start)
        if t >= turn_deg:
            return t, x - start
        x += 0.5
    return 0.0, 0.0


def build_shape_limits(path, cum, a_lat_max: float, v_floor: float,
                       target_speed: float, span_m: float = 15.0,
                       step_m: float = 2.0,
                       turn_deg: float = 30.0) -> List[float]:
    """预计算「按弯的形状限速」沿路径的限速表（纯几何，与速度无关）。

    为什么要有这步（性能）：原来在生成主循环的每一步都做 12 米前瞻扫描，
    每次扫描又反复调 _local_turn -> point_at_distance。实测一条 4700m 的
    真实路线，生成耗时 **34 秒**，其中 33 秒全花在这个扫描上
    （6745 次 _corner_speed_cap -> 33707 次 _corner_shape -> 809224 次
    _local_turn）。

    限速只取决于路径几何，与当前速度无关，所以完全可以预计算一次：
    沿弧长每 step_m 算一格，主循环只做查表 + 少量算术。

    返回与 positions 对应的列表（positions 用 index*step_m 表示）。
    """
    total = cum[-1]
    n = int(total / step_m) + 2
    # 表里存的是**相对基础速度的比例**，不是绝对速度。
    #
    # 为什么必须存比例：调用时传入的是「带配速波动的当前目标速度」，
    # 若表里是绝对值（按基础速度构建），就会把波动高于基础速度的部分
    # 削平 —— 实测直线路径上的配速波动变异系数从 0.054 掉到 0.032，
    # 一个「直线上的配速波动」测试因此失败，而且真人跑直线本来就有快慢。
    if a_lat_max <= 0:
        return [1.0] * n
    out = [1.0] * n
    base = max(0.05, target_speed)
    for i in range(n):
        x = i * step_m
        if x > total:
            break
        turn, arc = _corner_shape(path, cum, x, max(span_m, base * 3.0),
                                  turn_deg=turn_deg)
        if turn <= 0.0 or arc <= 0.5:
            continue
        r = max(0.5, arc / math.radians(turn))
        v_lim = max(v_floor, math.sqrt(max(0.0, a_lat_max * r)))
        out[i] = min(1.0, v_lim / base)
    return out


def _corner_speed_cap(path, cum, dist: float, target_speed: float,
                      a_lat_max: float, v_floor: float,
                      a_max: float = 0.5,
                      shape_limits: Optional[List[float]] = None,
                      step_m: float = 2.0) -> float:
    """按弯的**形状**给出本处允许的最高速度。

    模型：转弯靠向心力，横向加速度 a = v²/r 有上限（人走路约 1.5 m/s²，
    跑步约 3.0）。半径 r 由形状反推：r = 弧长 / 转角(弧度)。

    于是同样是 90°：
      · 半径 1m（原地转身） -> v ≈ √(1.5×1) = 1.2 m/s，慢但不像爬
      · 半径 6m（路口圆角） -> v ≈ √(1.5×6) = 3.0 m/s，几乎不减速
      · 半径 30m（缓弯）    -> v ≈ 6.7 m/s，完全不限速

    返回的是**速度上限**；实际减速交给上面的加速度限制去做，
    所以进出弯是平滑加减速，不是速度突变。

    shape_limits 为预计算的限速表（见 build_shape_limits）。**必须传**，
    否则每一步都要重新扫前方路径 —— 那是 34 秒 vs 1 秒的差别。
    """
    if a_lat_max <= 0 or not shape_limits:
        return target_speed
    total = cum[-1]
    if dist >= total:
        return target_speed
    n = len(shape_limits)

    base = max(0.05, target_speed)

    def limit_at(x: float) -> float:
        """该处的**绝对**限速 = 比例 x 当前目标速度。"""
        i = int(x / step_m)
        if i < 0:
            i = 0
        elif i >= n:
            i = n - 1
        return shape_limits[i] * base

    best = limit_at(dist)

    # 前方弯道的限速 —— 必须**提前**减速，否则冲进弯道才刹车。
    #
    # 这是关键的一步：原来只看当前位置，而形状限速只在弯道内部生效，
    # 车以 3.3 m/s 冲进来时刹车距离要 2a 分之 v² ≈ 6m，根本来不及 ——
    # 实测半径 1m 的弯，限速算出 1.50 m/s，实际最低速却仍是 0.30 m/s。
    #
    # 扫描前方到「刹停距离」为止，取能让车在该处减到限速的当前速度上界：
    #   v_now ≤ √(v_corner² + 2·a·s)      （s = 距弯道的距离）
    # 注意 v_now 不能低于 v_corner + a·dt，否则会在到达前就低于限速、
    # 反而变成龟速爬行。
    scan = min(total, dist + target_speed * target_speed / (2.0 * a_max)
               + target_speed * 2.0)
    x = dist
    while x <= scan:
        v_c = limit_at(x)
        if v_c < best:
            s = max(0.0, x - dist)
            v_allow = math.sqrt(v_c * v_c + 2.0 * a_max * s)
            # 留一点余量，避免刚好卡在边界上来回抖
            v_allow = max(v_c + a_max * 0.2, v_allow * 0.98)
            best = min(best, v_allow)
        x += step_m
    return max(v_floor, min(target_speed, best))


def _corner_cap(path, cum, dist: float, base_step: float,
                max_turn_deg: float = 25.0,
                min_samples: float = 5.0) -> float:
    """提前预判弯道，给出本步允许的最大位移。

    为什么需要它（用户反馈：「拐弯的判断来的太晚了，变向后一定脱离路线」）
    ------------------------------------------------------------------
    原来只有 `_chord_safe_step`，它只看**当前这一步**的弦长/弧长比 —— 等于
    走到拐点、一步跨过去时才被动削速度。结果是位置虽然还在路径上（实测离路
    0.01m），但**方向在一瞬间转完**：3.3 m/s + 1s 采样时实测单步转角 84°。

    真实的人/车进弯前就减速。这里用两道限制：
      (a) 一步之内的净偏转不超过 max_turn_deg（二分步长）
      (b) 弯道内至少落 min_samples 个采样点：步长 ≤ 弯道弧长 / min_samples

    (b) 是关键。只做 (a) 时采样点仍可能太少，方向用一个突变表达，
    相位不对时甚至出现接近折返的假象（实测 8 m/s 时 168°）。
    """
    total = cum[-1]
    if base_step <= 1e-9 or dist >= total:
        return base_step

    cap = base_step

    # (a) 本步净偏转限制
    if max_turn_deg > 0 and _local_turn(path, cum, dist, base_step) > max_turn_deg:
        lo, hi = 0.02, base_step
        for _ in range(6):
            mid = (lo + hi) / 2.0
            if _local_turn(path, cum, dist, mid) > max_turn_deg:
                hi = mid
            else:
                lo = mid
        cap = min(cap, max(0.02, lo * 0.98))

    # (b) 弯道内最少采样点数
    if min_samples > 1.0:
        span = max(base_step * 3.0, 12.0)
        extent = _corner_extent(path, cum, dist, span, turn_deg=45.0)
        if extent > 0.0:
            cap = min(cap, max(0.05, extent / min_samples))

    return max(0.02, min(base_step, cap))


def _chord_safe_step(path, cum, dist: float, want: float) -> float:
    """把这一步的位移限制在「不会在一步内拐过太大的弯」的范围内。

    做法很直接：从当前位置往前走，实时比较「已走的弧长」与「起点到该处的直线
    距离」。一旦弦长/弧长跌破 MIN_CHORD_RATIO，就停在上一个还在阈值内的位置。

    比逐段累加转角更省事，也天然处理了「一步跨过好几个小折线段」的情况。
    """
    if want <= 1e-9:
        return 0.0
    total = cum[-1]
    start = min(dist, total)
    end = min(dist + want, total)
    lat0, lon0, _ = geo.point_at_distance(path, cum, start)

    # 采样若干候选点，从远到近找到一个弦长安全的位移
    steps = 8
    best = want
    for k in range(1, steps + 1):
        cand = want * k / steps
        lat1, lon1, _ = geo.point_at_distance(path, cum, min(start + cand, total))
        chord = geo.haversine(lat0, lon0, lat1, lon1)
        # 弧长取实际可走的长度（接近终点时会短于 cand）
        arc = min(start + cand, total) - start
        if arc <= 1e-9:
            continue
        if chord / arc >= MIN_CHORD_RATIO:
            best = cand
        else:
            break
    return best


@dataclass
class GenOptions:
    speed: float = 1.4                # 目标速度 m/s
    interval_ms: int = 1000           # 采样间隔
    accel: float = 0.4                # 加速度上限 m/s²，越小越像人
    speed_jitter: float = 0.05        # 速度相对波动（每步白噪声）
    pace_variation: float = 0.0       # 长周期配速波动幅度（0.12 ≈ 跑者水平）
    pace_period_m: float = 180.0      # 配速波动的特征波长（米）
    grade_effect: bool = False        # 是否让坡度影响配速（上坡变慢）
    alt_base_m: float = 45.0          # 海拔基准（米）
    alt_amplitude_m: float = 0.0      # 地形起伏幅度（0=平地）
    noise_sigma_m: float = 0.0        # GPS 定位误差标准差（米），0=关闭
    noise_alpha: float = 0.99         # 误差的时间相关性，越大越"粘"
    coord_jitter_m: float = 0.0       # 【旧参数】白噪声抖动，保留兼容，默认关
    acc_mean_m: float = 8.0           # 上报精度的均值（米），Weibull 采样
    acc_min: float = 5.0              # 【旧参数】保留兼容
    acc_max: float = 12.0             # 【旧参数】保留兼容
    lead_in_acc_factor: float = 1.6   # 起步阶段精度放大倍数（刚锁星更差）
    bearing_jitter: float = 0.0       # 方位角噪声（度）；坐标噪声已在方向里体现
    start_speed: float = 0.0          # 起始速度，0 表示从静止起步
    decel_at_end: bool = True         # 到终点是否平滑刹停
    lead_in_points: int = 3           # 起步前原地漂移点数，0=关闭
    lead_in_radius_m: float = 4.0     # 漂移半径
    max_duration_s: float = 7200.0    # 安全上限
    seed: Optional[int] = None
    mode: str = "once"                # once / loop / pingpong
    altitude: float = 0.0
    altitude_jitter: float = 0.0
    # 拐角圆滑：OSRM 的几何在路口是尖角，按弧长采样会让某一步正好跨过顶点，
    # 方向瞬间转过去（实测一步内 bearing 跳 180°）。圆滑后转弯分摊到多步，
    # 人走路本来就是走弧线。0 = 关闭圆滑。
    corner_radius_m: float = 6.0
    corner_min_turn_deg: float = 25.0
    # 提前预判弯道：单步转角超过这个度数就提前压小步长（分摊到多步）。
    # 0 = 关闭净偏转限制（但下面的 corner_min_samples 仍生效）。
    # 一步净偏转上限。默认 75° 是**故意放宽**的：过弯速度已由下面的
    # corner_a_lat_max（按弯的形状）决定，再用小角度卡步长会跟物理模型打架 ——
    # 实测阈值 25° 时半径 1m 的弯被压到 0.30 m/s（横向加速度仅 0.09，
    # 远低于人的舒适值），而放宽到 75° 后是 0.63 m/s、a=0.40，合理得多。
    corner_step_turn_deg: float = 75.0
    # 弯道内至少落多少个采样点。弯道弧长 / 这个数 = 步长上限。
    # 实测 8 m/s 时若弯道内只有 1~2 个点，方向会表现为一次突变甚至伪折返。
    # 1 = 关闭这条限制。
    # 弯道内最少采样点。默认 1（关闭该限制）—— 它同样会跟形状限速打架：
    # 实测设 5 时半径 30m 的缓弯也被迫减速，而形状模型本该让它全速通过。
    # 形状限速自然会让急弯落更多点（v 小 -> 步长小 -> 点密）。
    corner_min_samples: float = 1.0
    # 绕圈：把路径当一圈连续跑多少圈（1 = 不绕圈）。
    # 用于起终点很近的情况（小区环线、操场）。时间轴跨圈接续，
    # 不会每圈重新起步；拐弯预判也能跨圈看到下一圈的路。
    laps: int = 1
    # 按弯的**形状**限速：转弯横向加速度上限 a = v²/r（m/s²）。
    # 走路舒适约 1.5，跑步约 3.0。0 = 关闭形状限速。
    # 圆滑策略：
    #   "auto"  —— 按「切角预算」自动挑半径（默认）。用离路上限的一部分
    #              当预算，在预算内取最大的圆滑半径。
    #   "round" —— 用上面显式的 corner_radius_m。
    #   "none"  —— 完全不圆滑（方向会退回尖角，但真路径 100% 贴中心线）。
    #
    # 为什么默认 auto（用户提出「把两种方式都写入，根据情况自动选择」）：
    # 圆滑改的是**真路径**，切角这部分偏差**离路上限修不了**
    # （实测半径 6m 时离路被锁在 2.00m，上限从 4 压到 1 完全无效）；
    # 而离路上限只压**噪声**。两者是不同手段，正确关系是
    # 「用离路上限的预算去约束圆滑半径」，而不是二选一。
    corner_mode: str = "auto"
    # auto 模式下，切角允许占离路上限的比例。0.25 = 切角不超过上限的四分之一，
    # 剩下的预算留给 GPS 噪声。上限为 0（不限制）时用 corner_cut_default_m。
    corner_cut_ratio: float = 0.25
    # 离路上限为 0（不限制）时，auto 模式的切角预算（米）
    corner_cut_default_m: float = 0.8
    corner_a_lat_max: float = 1.5
    # 形状限速的速度下限 —— 再急的弯也不低于这个速度（否则会像爬）
    corner_v_floor: float = 0.6
    # 上报坐标到路径的最大距离（米）。超过就沿噪声方向拉回路廊内。
    # 为什么要它：GPS 误差是真会存在的（σ=2m 实测最远偏 7.7m，落到建筑里），
    # 而用户要的是「虚拟定位写死在道路上」。0 = 不限制（保留纯噪声行为）。
    # 默认 4m：城市道路 + 人行道的合理路廊宽度。实测把「离中心线
    # 最大距离」压到 4.5m 以内，且没有任何点超过 5m。
    max_offset_m: float = 4.0


def _walk_noise(rng: random.Random, amp: float) -> float:
    """Ornstein-Uhlenbeck 式的噪声：比均匀随机更像真实传感器的慢漂移。"""
    state = {"v": 0.0}

    def step() -> float:
        state["v"] = 0.9 * state["v"] + rng.uniform(-amp, amp)
        return state["v"]

    return step


def build_lap_path(path: Sequence[Tuple[float, float]],
                   laps: int) -> Tuple[List[Tuple[float, float]], List[int]]:
    """把一段路线重复 laps 次（首尾相接），返回 (合并路径, 圈边界索引)。

    用途：起终点很近时「绕圈」。比如小区环线跑 10 圈。

    要点：
      · 相邻两圈的连接处**不接掉头**（第 1 圈终点 == 第 2 圈起点），
        所以合并后是一条连续路径，采样与拐弯预判都能跨圈正常工作。
      · 圈边界索引用来切分采样点，让每圈的时间戳接续而不是从 0 重来。

    laps <= 1 时原样返回。
    """
    pts = list(path)
    if laps <= 1 or len(pts) < 2:
        return pts, [0]

    merged: List[Tuple[float, float]] = list(pts)
    bounds = [0]
    for _ in range(1, laps):
        # 跳过与上一点重合的首点，避免产生零长度段
        for q in pts[1:]:
            merged.append(q)
        bounds.append(len(merged) - 1)
    return merged, bounds


def generate(
    path: Sequence[Tuple[float, float]],
    opts: GenOptions,
    *,
    start_offset_m: float = 0.0,
) -> List[dict]:
    """沿 path 生成采样点列表。

    start_offset_m: 从路径起点往前跳过多少米（用于「从中途开始走」）。
    opts.laps > 1 时把 path 视为一圈，连续跑 laps 圈（时间轴接续，不重新起步）。
    """
    rng = random.Random(opts.seed)
    if len(path) < 2:
        raise ValueError("路径至少需要 2 个点，检查上游规划结果是否为空")
    if opts.interval_ms <= 0:
        raise ValueError("interval_ms 必须为正")

    # 绕圈：把路径重复 laps 次，首尾相接
    lap_bounds: List[int] = [0]
    if opts.laps and opts.laps > 1:
        path, lap_bounds = build_lap_path(path, int(opts.laps))
        # 圈边界对应的弧长，后面切分采样点时用
        path = list(path)

    # 打散长直线段。
    #
    # 分段长度必须与「一步走多远」相称：路径位置是按弧长插值的，而目标 App 只能
    # 用相邻两点的直线距离反推速度。若一步能跨过好几个折线顶点（例如 8 m/s 走一步
    # 跨过 25 m 的分段），拐弯处的直线距离会明显小于弧长 —— 实测一个 110° 的弯
    # 能让「位移/速度」偏差到 50%，看起来就像在造假。
    # 取「一个采样间隔行程」的 1/4，既保证弦长≈弧长，又不至于把点渲得太密。
    step_m = opts.speed * (opts.interval_ms / 1000.0)
    # 记录**未圆滑的原始中心线**：它才是「道路在哪」的权威。
    # 后面把上报坐标夹回路廊时要以它为准 —— 若以圆滑后的路径为准，
    # 圆滑切角本身就有 1~2m，叠加起来会让实际偏离超出上限
    # （实测上限 4m 时，离中心线最大到了 5.92m）。
    centerline = list(path)
    cum_center = geo.cumulative(centerline)

    # 先把尖角圆滑掉，再打散长直线段。
    #
    # 策略由 corner_mode 决定（详见 GenOptions 的注释）：
    #   auto  —— 在「切角预算」内挑最大的圆滑半径。预算是离路上限的一部分，
    #            因为切角这部分偏差是离路上限**修不了**的，必须提前约束。
    #   round —— 用显式半径
    #   none  —— 不圆滑
    _mode = (opts.corner_mode or "auto").lower()
    _radius = opts.corner_radius_m
    if _mode == "none":
        _radius = 0.0
    elif _mode == "auto":
        if opts.max_offset_m > 0:
            _budget = max(0.2, opts.max_offset_m * opts.corner_cut_ratio)
        else:
            _budget = max(0.0, opts.corner_cut_default_m)
        _radius, _cut = geo.pick_round_radius(
            path, _budget, min_turn_deg=opts.corner_min_turn_deg,
            arc_step_m=max(1.0, step_m / 4.0))
        _auto_note = f"拐角圆滑 auto：预算 {_budget:.2f}m -> 半径 {_radius:.1f}m（切角 {_cut:.2f}m）"
    if _radius and _radius > 0:
        path = geo.round_corners(path,
                                 radius_m=_radius,
                                 min_turn_deg=opts.corner_min_turn_deg,
                                 arc_step_m=max(1.0, step_m / 4.0))
    path = geo.densify(path, max_segment_m=max(2.0, min(25.0, step_m / 4.0)))
    cum = geo.cumulative(path)
    total = cum[-1]
    if total < 1.0:
        raise ValueError(f"路径总长只有 {total:.1f} 米，太短无法生成行进轨迹")

    # 预计算「按弯的形状」限速表 —— 只做一次。绝不能放进主循环：
    # 实测算过，那样一条 4700m 路线要 34 秒（占全部耗时的 96%）。
    _shape_step = 2.0
    shape_limits = build_shape_limits(
        path, cum, opts.corner_a_lat_max, opts.corner_v_floor,
        max(0.05, opts.speed), step_m=_shape_step)

    dt = opts.interval_ms / 1000.0
    samples: List[Tuple[float, float, float, float, float, float]] = []  # (t, lat, lon, speed, along, alt)

    speed_noise = _walk_noise(rng, opts.speed * opts.speed_jitter)
    lat_noise = _walk_noise(rng, 1.0)
    lon_noise = _walk_noise(rng, 1.0)

    # 跑步相关的两个模型（必须在 lead-in 之前建好，漂移点也要有海拔）：
    #   pace  —— 长周期的配速波动（跑者每段配速都在变，不像步行那么匀速）
    #   elev  —— 合成海拔剖面；开启 grade_effect 后上坡会自动变慢
    pace = PaceVariation(rng, opts.pace_variation, opts.pace_period_m)
    elev = Elevation(rng, total, opts.alt_base_m, opts.alt_amplitude_m)

    # --- 起步漂移（lead-in）：真实设备刚拿到定位时会跳几下 ---
    head_lat, head_lon, _ = geo.point_at_distance(path, cum, start_offset_m)
    if opts.lead_in_points > 0:
        # 漂移半径不能超过「离路上限」，否则两个参数自相矛盾：
        # 一个说「不许离路超过 N 米」，另一个把点撒到 4 米外。
        # 之前这里有 bug —— lead-in 完全跳过夹取，导致上限设 1m 时
        # 开头几个点仍在 3.7m 外（用户实测发现）。
        lead_r = opts.lead_in_radius_m
        if opts.max_offset_m > 0:
            lead_r = min(lead_r, opts.max_offset_m * 0.8)
        for i in range(opts.lead_in_points):
            a = rng.uniform(0, 360)
            r = rng.uniform(0.3, 1.0) * lead_r
            dlat, dlon = geo.offset(head_lat, head_lon, a, r)
            samples.append((i * dt, dlat, dlon, 0.0, start_offset_m,
                            elev.step(rng, start_offset_m)))

    # 主运动的时间轴紧接在 lead-in 之后：lead-in 覆盖 t=0 … (L-1)·dt
    t = (opts.lead_in_points - 1) * dt if opts.lead_in_points > 0 else 0.0
    dist = start_offset_m
    v = max(0.0, opts.start_speed)
    target = max(0.05, opts.speed)
    a_max = max(1e-3, opts.accel)
    max_dv = a_max * dt      # 单个采样间隔内允许的最大速度变化

    # --- 主运动 ---
    #
    # 每一步都按「本步内的匀加速运动」解析求解，而不是先预估速度再乘时间。
    # 这样位移、速度、时间三者严格满足 s = v₀t + ½at²，也就自动满足
    # 平均速度 = 位移 / 时间 —— 目标 App 用后者反推速度时不会发现矛盾。
    while dist < total - 1e-9 and t <= opts.max_duration_s:
        remaining = total - dist

        # 本步的目标速度 = 基础目标 × 配速波动 × 坡度影响。
        # 这两个乘子都是随里程缓慢变化的，所以不会引入突兀的加速 ——
        # 后面仍然会经过 accel 上限约束。
        eff_target = target * pace.at(dist)
        if opts.grade_effect:
            eff_target *= elev.pace_factor(dist)
        eff_target = max(0.05, eff_target)

        # --- 按弯的**形状**限速 -------------------------------------------
        #
        # 用户要求「按拐弯的形状来决定拐弯的动作」。这里用横向加速度模型：
        # 转弯靠向心力，a = v²/r 有上限（走路约 1.5 m/s²、跑步约 3.0）。
        # 半径 r 由弯的形状反推（弧长 / 转角），于是同样是 90°：
        # 半径 1m 的原地转身要慢到 ~1.2 m/s，半径 30m 的缓弯完全不限速。
        #
        # 注意这是**限速**而不是直接改位移 —— 实际减速量仍由 accel 上限约束，
        # 所以进出弯是平滑加减速，不会出现速度突变。
        eff_target = _corner_speed_cap(
            path, cum, dist, eff_target,
            opts.corner_a_lat_max, opts.corner_v_floor, a_max,
            shape_limits=shape_limits, step_m=_shape_step)

        # 末段刹车：把「以减速度 a 刹停所需距离」v²/(2a) 约束在剩余距离之内，
        # 即 v ≤ √(2·a·s)。约束作用在「本步结束时」的位置上（remaining - v·dt），
        # 这样最后几步速度已经降到很低，不会出现刹不住、被硬停的突变。
        v_t = eff_target
        if opts.decel_at_end:
            # 刹车判据：v ≤ √(2·a·s')，其中 s' 是「扣掉本步行程后」的剩余距离。
            #
            # 为什么要扣：连续公式 v ≤ √(2·a·s) 只保证「以 a 减速能停下」，
            # 不保证停得平缓。实测 dt=1s、a=0.5、目标 5 m/s、1000m 路径时，
            # 最后一个采样点还有 3.63 m/s，只能用 0.5g 在最后 0.48 秒硬刹 ——
            # 虽然位移与速度仍自洽，但末段速度剖面不像真人开车。
            # 预留一步行程后，车会提前一个采样间隔开始减速，末段自然收敛到 0。
            v_t = min(v_t, math.sqrt(max(0.0, 2.0 * a_max * (remaining - v * dt))))
        v_t = max(0.0, v_t + max(0.0, speed_noise()))

        # 本步实际采用的加速度，受加速度上限与「不能倒车」约束
        acc = min(max_dv, v_t - v)
        acc = max(acc, -min(max_dv, v))

        # 本步理想位移（匀加速运动学）
        want = v * dt + 0.5 * acc * dt * dt

        # --- 拐弯保护（两级）------------------------------------------------
        #
        # 第一级：**提前预判**。看前方一段路的总转角，越大的弯越早压小步长。
        #   用户反馈「拐弯的判断来的太晚了，变向后一定脱离路线」——
        #   原来只有下面第二级，等于走到拐点、一步跨过去时才被动削速度。
        #   结果位置虽然在路径上（实测离路 0.00m），但方向一瞬间转完：
        #   3.3 m/s + 1s 采样时单步转角实测达 84°，看起来就是「突然变向」。
        #   真实的人/车进弯前就减速，方向变化分摊到多步。
        #
        # 第二级：**弦长保护**。限制「本步能走多远」，使这一步的弦长/弧长比
        #   不低于 MIN_CHORD_RATIO（跨过极急的弯时弦长会远小于弧长，
        #   导致「位移/速度」对不上，看起来像造假数据）。
        want = _corner_cap(path, cum, dist, want,
                           max_turn_deg=opts.corner_step_turn_deg,
                           min_samples=opts.corner_min_samples)
        want = _chord_safe_step(path, cum, dist, want)

        # 若本步会越过（或正好走到）终点，则解 ½·acc·τ² + v·τ = remaining，取正根。
        # 这里的判定必须包含「正好到达」的浮点边界，否则循环会在 dist 恰好触到
        # total 时直接退出，最后一个点就保留了期望速度而不是停下来的速度。
        if want >= remaining - 1e-9:
            if abs(acc) > 1e-12:
                disc = v * v + 2.0 * acc * remaining
                tau = (-v + math.sqrt(disc)) / acc if disc >= 0.0 else dt
            else:
                tau = remaining / v if v > 1e-12 else dt
            tau = min(dt, max(0.0, tau))
            # 报「区间平均速度」= 位移/时间：这才是 App 从相邻坐标反推出来的值。
            # 若改报末速度，最后一秒刹停时会从巡航速度直接掉到 0，跳变巨大且矛盾。
            v_end = remaining / tau if tau > 1e-9 else 0.0
            end_lat, end_lon, _ = geo.point_at_distance(path, cum, total)
            dist = total
            t += dt
            samples.append((t, end_lat, end_lon, v_end, total, elev.step(rng, total)))
            break

        dist += want
        # 关键：速度用「实际位移 / 时间」反推，而不是用运动学积分出来的期望值。
        # 这样即便这一步被拐弯保护削掉了位移，speed 与坐标也永远自洽。
        v = want / dt if dt > 1e-9 else v
        t += dt
        lat, lon, _ = geo.point_at_distance(path, cum, dist)
        samples.append((t, lat, lon, v, dist, elev.step(rng, dist)))

    if not samples:
        raise ValueError("没有生成任何采样点")

    # 兜底：循环因浮点误差提前收敛时，再补一个「刹车到 0」的采样点。
    # 直接把速度改成 0 会破坏「相邻两步速度变化 ≤ accel·dt」这个约束，
    # 所以这里按 v = 2·s/T 反解出本步该有的减速度，物理上依然自洽。
    end_lat, end_lon, _ = geo.point_at_distance(path, cum, total)
    end_gap = geo.haversine(samples[-1][1], samples[-1][2], end_lat, end_lon)
    if end_gap > 1e-6:
        # 补收尾点。速度必须用「位移/时间」反推（也就是该区间的平均速度），
        # 不能沿用上一点的巡航速度，否则最后一个点的 speed 与它自己那段位移
        # 会对不上 —— 这正是容易被识破的地方。
        last_at = samples[-1][0]
        n_steps = max(1, int(math.ceil(end_gap / max(1e-9, target * dt))))
        v_tail = end_gap / (n_steps * dt)
        for k in range(1, n_steps + 1):
            samples.append((last_at + k * dt, end_lat, end_lon, v_tail, total, elev.step(rng, total)))

    # --- 施加 GPS 噪声 + 组装输出 ---
    #
    # 关键顺序：先给坐标加噪声，再用「噪声后的相邻坐标」反推 speed 和 bearing。
    # 这样目标 App 无论读 speed 字段，还是拿两个坐标自己算速度，得到的都是同一个数。
    #
    # 但直接报原始位移速度是不行的：步行一步只走 0.7m，而 GPS 误差有几米，
    # 位移速度会在 0 和十几 m/s 之间乱跳（实测起步第一个点能报出 6.23 m/s）。
    # 所以这里对速度再做一次一阶滤波，并把每步变化约束在 accel·dt 之内 ——
    # 真实手机上的速度估计也是这么平滑出来的（接收机用多普勒测速，不是差分位置）。
    gps = GpsNoise(rng, opts.noise_sigma_m, opts.noise_alpha)
    # 高度误差用同一个 AR(1) 模型，只是标准差更大（真实 GPS 的高度误差约为
    # 水平误差的 1.5~3 倍）。这一点很重要：如果给高度加白噪声，逐点高度会
    # 完全随机跳动，剖面图糊成锯齿，也不符合 GPS 的真实行为。
    gps_alt = GpsNoise(rng, opts.noise_sigma_m * 1.8, opts.noise_alpha)
    out: List[dict] = []
    prev_meas: Optional[Tuple[float, float]] = None   # 上一点（主运动）
    last_brg = geo.point_at_distance(path, cum, min(start_offset_m, total))[2]
    v_filt: Optional[float] = None                    # 滤波后的上报速度

    for i, (ti, lat, lon, v, along, alt_base) in enumerate(samples):
        is_lead_in = i < opts.lead_in_points
        dt_pt = dt
        if i > 0:
            dt_pt = max(1e-6, ti - samples[i - 1][0])

        # 1) 时间相关的定位误差（东/北向米 -> 经纬度偏移）
        if opts.noise_sigma_m > 0 and not is_lead_in:
            east, north = gps.step(rng)
            lat = lat + north / 111320.0
            lon = lon + east / (111320.0 * max(0.1, math.cos(math.radians(lat))))

        # 1b) 硬约束：把上报坐标拉回「路廊」内
        #
        # 为什么要这一步：GPS 误差是真实存在的（σ=2m 时实测最远能偏到离
        # 路面 7.7 米 —— 那就落到建筑或绿化带里了）。用户要的是「虚拟定位
        # 写死在道路上」，所以这里给「上报坐标到**道路中心线**的距离」设硬上限：
        # 超了就沿噪声方向拉回到上限处。
        #
        # 基准用 centerline（未圆滑的原始几何）而不是 path，因为圆滑本身
        # 就有切角，用它当基准会让实际偏离超出上限。
        #
        # **对所有点生效**（含 lead-in）。之前写成 `not is_lead_in` 跳过起步阶段，
        # 而上限 1m 时 lead-in 的漂移半径还是 4m —— 于是开头几个点离路 3.7m，
        # 表现为「拐弯后坐标与道路距离超过预设上限」（用户实测发现）。
        # 现在 lead-in 的漂移半径也按上限收敛（见上），这里再兜一层。
        if opts.max_offset_m > 0:
            clat, clon, _ = geo.point_at_distance(
                centerline, cum_center, min(along, total))
            d = geo.haversine(clat, clon, lat, lon)
            if d > opts.max_offset_m:
                f = opts.max_offset_m / d
                lat = clat + (lat - clat) * f
                lon = clon + (lon - clon) * f

        # 2) 兼容旧的独立抖动参数（默认关闭）
        if opts.coord_jitter_m > 0 and i > 0:
            lat += lat_noise() * opts.coord_jitter_m / 111320.0
            lon += lon_noise() * opts.coord_jitter_m / (111320.0 * max(0.1, math.cos(math.radians(lat))))

        # 3) 反推 bearing / speed
        #
        # bearing 与 speed 的处理方式**故意不同**：
        #
        #   speed：跟随噪声后的坐标（speed = 位移/时间）。因为目标 App 最常用的
        #          检测手段就是「拿两个坐标反推速度，和 speed 字段比」，这一致性
        #          比读数好看更重要。代价是步行时读数会抖 —— 真实 GPS 也这样。
        #
        #   bearing：用路径方向 + 小抖动，**不**从噪声坐标反推。因为步行一步
        #          只走 0.68m 而噪声有 2.5m，方向会被完全淹没（实测相邻方位角
        #          中位跳变 30°、最大 179°）。真实设备的航向是强平滑量
        #          （接收机由多普勒或卡尔曼滤波给出），不会这样乱跳。
        path_brg = geo.point_at_distance(path, cum, min(along, total))[2]

        if is_lead_in:
            # 起步漂移：还没开始走
            speed = 0.0
            brg = path_brg
        elif prev_meas is None:
            # 主运动第一个点：lead-in 点是随机散布的，拿它当"上一点"会算出
            # 一个不存在的高速（实测 6.23 m/s）。所以这里直接用运动学速度起步。
            speed = v
            brg = path_brg
        else:
            moved = geo.haversine(prev_meas[0], prev_meas[1], lat, lon)
            raw = moved / dt_pt
            # 只用「运动学上限」挡掉极端异常值，不做进一步平滑。
            #
            # 这是一个有意的取舍：
            #   * 真实 GPS 在步行速度下的速度读数本来就是抖的（误差与每步位移
            #     同一量级），把抖动磨平反而"不像真的"；
            #   * 而 speed 与坐标严格自洽才是更重要的不变量 —— 目标 App 最常用的
            #     检测手段就是「拿两个坐标反推速度，和 speed 字段比」。
            # 所以这里让 speed 贴合坐标，仅当原始值超过物理可能时才夹住，
            # 避免出现 15 m/s 这种荒谬尖峰。
            if v_filt is None:
                v_filt = raw
            else:
                limit = min(3.0, max(1.0, opts.accel * 6.0))
                v_filt = v_filt + max(-limit, min(limit, raw - v_filt))
                if abs(raw - v_filt) > limit:
                    v_filt = raw          # 明显跳变时直接跟随坐标
            speed = v_filt
            brg = path_brg

        if not is_lead_in and prev_meas is not None:
            v_filt = speed
        last_brg = brg
        if not is_lead_in:
            prev_meas = (lat, lon)

        if opts.bearing_jitter > 0 and speed > 0.1:
            brg = (brg + rng.uniform(-opts.bearing_jitter, opts.bearing_jitter)) % 360.0

        # 4) 上报精度：Weibull 采样，起步阶段更差
        acc = sample_accuracy(rng, opts.acc_mean_m)
        if is_lead_in:
            acc *= opts.lead_in_acc_factor

        # 海拔：用合成地形剖面（opt.alt_amplitude_m > 0 时才有起伏），
        # 上报的 alt 字段 = 干净地形 + 高度噪声。
        #
        # ⚠️ 注意这里有个坑：地形（alt_base）是**干净**的合成剖面，上面加的
        # 噪声只影响「上报值」，绝不能反过来参与坡度计算。因为相邻两点的水平
        # 距离只有几米，1.8m 的高度噪声会让逐点坡度算出 100%+ 的荒谬数值
        # （实测中位 123%、最大 398%）—— 而坡度是用来调配速的。
        # 坡度走 Elevation.grade()，那里对 30m 窗口做差分，天然滤掉了高频抖动。
        alt = alt_base
        if opts.altitude_jitter > 0:
            alt += rng.uniform(-opts.altitude_jitter, opts.altitude_jitter)
        if opts.noise_sigma_m > 0 and not is_lead_in and opts.alt_amplitude_m > 0:
            # 真实 GPS 的高度误差远大于水平误差（约 1.5~3 倍），且同样是
            # 时间相关的慢漂移 —— 用 AR(1) 而不是白噪声。
            # 只在开了地形时才加：否则平路会凭空出现 ±数米的"海拔"，
            # 与「平地」这个设定自相矛盾（实测振幅 0 时仍报出 22.9m 的范围）。
            alt += gps_alt.step(rng)[1]

        out.append({
            "t": int(round(ti * 1000)),
            "lon": round(lon, 7),
            "lat": round(lat, 7),
            "speed": round(max(0.0, speed), 2),
            "bearing": round(brg, 1),
            "acc": round(acc, 1),
            "alt": round(alt, 1),
        })

    return out


def build_route(
    points: Sequence[dict],
    *,
    mode: str = "once",
    interval_ms: int = 1000,
    source: str = "",
    extra: Optional[dict] = None,
) -> dict:
    route = {
        "version": SCHEMA_VERSION,
        "mode": mode,
        "interval": interval_ms,
        "source": source,
        "generatedBy": "mockroute/1.0",
        "points": list(points),
    }
    if extra:
        route.update(extra)
    return route


def validate(route: dict) -> List[str]:
    """返回问题列表，空列表表示通过。设备端也会校验，这里提前发现。"""
    problems: List[str] = []
    pts = route.get("points")
    if not isinstance(pts, list) or not pts:
        return ["points 缺失或为空"]
    prev_t = -1
    for i, p in enumerate(pts):
        if not isinstance(p, dict):
            problems.append(f"第 {i} 个点不是对象")
            continue
        for key in ("lat", "lon"):
            if key not in p:
                problems.append(f"第 {i} 个点缺少 {key}")
            else:
                try:
                    float(p[key])
                except (TypeError, ValueError):
                    problems.append(f"第 {i} 个点的 {key} 不是数字: {p[key]!r}")
        t = p.get("t")
        if t is None:
            problems.append(f"第 {i} 个点缺少 t")
        else:
            try:
                ti = int(t)
            except (TypeError, ValueError):
                problems.append(f"第 {i} 个点的 t 不是整数: {t!r}")
                continue
            if ti < prev_t:
                problems.append(f"第 {i} 个点的 t={ti} 小于前一个点 t={prev_t}（必须单调不减）")
            prev_t = ti
        lat, lon = p.get("lat"), p.get("lon")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            if not (-90 <= lat <= 90):
                problems.append(f"第 {i} 个点纬度越界: {lat}")
            if not (-180 <= lon <= 180):
                problems.append(f"第 {i} 个点经度越界: {lon}")
        sp = p.get("speed")
        if isinstance(sp, (int, float)) and sp > 120:
            problems.append(f"第 {i} 个点速度 {sp} m/s 异常（>432km/h）")
    return problems


def write_route(route: dict, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    problems = validate(route)
    if problems:
        raise ValueError("轨迹校验失败:\n  - " + "\n  - ".join(problems[:20]))
    out.write_text(json.dumps(route, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return out


def summarise(route: dict) -> str:
    pts = route["points"]
    dur = pts[-1]["t"] / 1000.0
    lat = [p["lat"] for p in pts]
    lon = [p["lon"] for p in pts]
    dist = geo.haversine(pts[0]["lat"], pts[0]["lon"], pts[-1]["lat"], pts[-1]["lon"])
    along = 0.0
    for i in range(1, len(pts)):
        along += geo.haversine(pts[i - 1]["lat"], pts[i - 1]["lon"], pts[i]["lat"], pts[i]["lon"])
    sp = [p.get("speed", 0) for p in pts]
    avg = sum(sp) / len(sp)
    # 用「移动段」的速度标准差衡量配速波动（跳过起步/停步）
    moving = [s for s in sp if s > 0.5]
    if len(moving) > 2:
        m = sum(moving) / len(moving)
        sd = (sum((x - m) ** 2 for x in moving) / len(moving)) ** 0.5
        cv = sd / m * 100 if m > 0 else 0.0
        pace_line = (f"速度 平均={avg:.2f}  移动段均={m:.2f}  波动={cv:.0f}% "
                     f"最大={max(sp):.2f}  最小={min(sp):.2f} m/s")
        pace_min_km = 1000.0 / m / 60.0 if m > 0 else 0.0
        pace_line += f"\n配速 ≈ {int(pace_min_km)}'{int((pace_min_km % 1) * 60):02d}\"/km"
    else:
        pace_line = (f"速度 平均={avg:.2f}  最大={max(sp):.2f}  最小={min(sp):.2f} m/s")

    # 海拔与爬升（仅当生成时开启了地形才有意义）
    alt = [p.get("alt", 0.0) for p in pts]
    # 逐点高度差的量级能告诉我们 alt 是否叠加了高度噪声
    alt_has_noise = False
    if len(alt) > 20:
        diffs = sorted(abs(alt[i] - alt[i - 1]) for i in range(1, len(alt)))
        alt_has_noise = diffs[len(diffs) // 2] > 0.4
    alt_line = ""
    if max(alt) - min(alt) > 1.0:
        # ⚠️ 不要报「逐点累加爬升」：alt 含高度噪声（σ≈1.8×水平噪声），
        # 逐点求和会把噪声的正半部分全部累加。实测 5km / 1400 点的轨迹因此
        # 报出 2552m 的"累计爬升" —— 比路线本身还荒谬。
        # 有意义的是**净爬升**（终点海拔 − 起点海拔）和海拔范围。
        net = alt[-1] - alt[0]
        alt_line = (f"\n海拔 {min(alt):.0f}~{max(alt):.0f}m  "
                    f"净爬升 {net:+.0f}m")
        if alt_has_noise:
            alt_line += "（alt 含 GPS 高度噪声，逐点累加爬升不可用）"

    return (
        f"点数={len(pts)}  时长={dur:.1f}s  沿路径里程={along:.0f}m  "
        f"直线距离={dist:.0f}m\n"
        f"{pace_line}{alt_line}\n"
        f"范围 lat {min(lat):.6f}~{max(lat):.6f}  lon {min(lon):.6f}~{max(lon):.6f}\n"
        f"模式={route.get('mode')}  来源={route.get('source') or '(未标注)'}"
    )
