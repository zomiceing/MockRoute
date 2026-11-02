"""真实路网路径规划：给起点终点，返回沿道路的折线。

为什么要接真实路网：两点之间的直线在真实 GPS 轨迹里几乎不存在。
用路网折线再按速度采样，轨迹才会贴着马路走。

支持的后端（按推荐顺序）：
  osrm-demo  免费、无需 key，适合测试；有频率限制，别用来跑批量任务
  osrm       自建 OSRM，最稳（docker run -p 5000:5000 osrm/osrm-backend）
  amap       高德，需要 key，国内路网最准，返回 GCJ-02
  google     Google Directions，需要 key
  file       直接读本地文件（GeoJSON / 一行一个 lat,lon / GPX 轨迹）

坐标系：统一转成 WGS-84 输出，因为 Android 定位给应用的是 WGS-84。
用 amap 时会在内部做 GCJ-02 → WGS-84 反解。
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from . import geo

Point = Tuple[float, float]  # (lat, lon)

USER_AGENT = "mockroute/1.0 (location-simulation-for-testing)"


class RouteError(RuntimeError):
    pass


@dataclass
class RouteResult:
    points: List[Point]
    provider: str
    distance_m: float = 0.0
    duration_s: float = 0.0
    notes: str = ""
    profile: str = ""

    def wgs84(self) -> List[Point]:
        return self.points


def _http_get(url: str, timeout: float = 20.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:400]
        except Exception:
            pass
        raise RouteError(f"HTTP {e.code} 来自 {urllib.parse.urlsplit(url).netloc}\n{body}") from e
    except urllib.error.URLError as e:
        raise RouteError(
            f"无法连接 {urllib.parse.urlsplit(url).netloc}: {e.reason}\n"
            "检查网络、代理设置，或者换一个 provider（--provider osrm）"
        ) from e


# --------------------------------------------------------------------------
# OSRM
# --------------------------------------------------------------------------

OSRM_PROFILES = {"driving", "walking", "cycling", "foot", "bike", "car"}


def osrm(base: str, waypoints: Sequence[Point], profile: str = "driving",
         timeout: float = 25.0) -> RouteResult:
    """OSRM route 服务。waypoints 顺序即行进顺序，必须是 WGS-84。"""
    if len(waypoints) < 2:
        raise RouteError("OSRM 至少需要 2 个点")
    coord_str = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in waypoints)
    # demo 服务器只提供 driving 轮廓；自建实例可换成 foot/bike
    prof = profile if profile in ("driving", "foot", "bike") else "driving"
    url = (
        f"{base.rstrip('/')}/route/v1/{prof}/{coord_str}"
        "?overview=full&geometries=polyline&steps=false&annotations=false"
    )
    raw = _http_get(url, timeout=timeout)
    data = json.loads(raw)
    if data.get("code") != "Ok" or not data.get("routes"):
        raise RouteError(f"OSRM 返回异常: {data.get('code')} {data.get('message', '')}")
    route = data["routes"][0]
    pts = geo.decode_polyline(route["geometry"], precision=5)
    return RouteResult(
        points=pts,
        provider=f"osrm({base})",
        distance_m=route.get("distance", 0.0),
        duration_s=route.get("duration", 0.0),
        profile=prof,
        notes=f"{len(waypoints)} 个途经点",
    )


# --------------------------------------------------------------------------
# 高德
# --------------------------------------------------------------------------

_AMAP_BASE = "https://restapi.amap.com/v3/direction"


def amap(key: str, origin: Point, dest: Point, waypoints: Sequence[Point] = (),
         mode: str = "driving", strategy: int = 0, city: str = "",
         timeout: float = 25.0) -> RouteResult:
    """高德路径规划。

    注意：高德接受和返回的都是 GCJ-02。这里传进去的 origin/dest 视为 GCJ-02，
    返回的折线也按 GCJ-02 解码，最后统一反解成 WGS-84。
    """
    if mode not in ("driving", "walking", "bicycling", "electrobike", "truck", "integrated"):
        raise RouteError(f"不支持的高德出行方式: {mode}")
    params = {
        "key": key,
        "origin": f"{origin[1]:.6f},{origin[0]:.6f}",
        "destination": f"{dest[1]:.6f},{dest[0]:.6f}",
        "extensions": "base",
        "strategy": str(strategy),
        "output": "json",
    }
    if waypoints:
        if mode in ("walking", "bicycling"):
            raise RouteError(f"高德 {mode} 不支持途经点")
        params["waypoints"] = ";".join(f"{lon:.6f},{lat:.6f}" for lat, lon in waypoints)
    if mode == "walking":
        url = f"{_AMAP_BASE}/walking?"
        params.pop("strategy", None)
    elif mode in ("bicycling", "electrobike"):
        url = f"{_AMAP_BASE}/bicycling?"
        params.pop("strategy", None)
        params["city"] = city or ""
    else:
        url = f"{_AMAP_BASE}/{mode}?"

    raw = _http_get(url + urllib.parse.urlencode(params), timeout=timeout)
    data = json.loads(raw)
    if str(data.get("status")) != "1":
        raise RouteError(f"高德返回错误: infocode={data.get('infocode')} info={data.get('info')}")
    paths = data.get("route", {}).get("paths") or []
    if not paths:
        raise RouteError("高德返回的路径为空，检查起终点是否在路网附近")
    path = paths[0]

    gcj: List[Point] = []
    for step in path.get("steps", []):
        poly = step.get("polyline") or ""
        for token in poly.split(";"):
            token = token.strip()
            if not token:
                continue
            try:
                lon_s, lat_s = token.split(",")
                gcj.append((float(lat_s), float(lon_s)))
            except ValueError:
                continue
    if not gcj:
        raise RouteError("高德返回的 polyline 无法解析")

    wgs = geo.convert_points(gcj, "gcj02", "wgs84")
    return RouteResult(
        points=wgs,
        provider=f"amap/{mode}",
        distance_m=float(path.get("distance", 0) or 0),
        duration_s=float(path.get("duration", 0) or 0),
        profile=mode,
        notes=f"原始 {len(gcj)} 点，GCJ-02 → WGS-84 已转换",
    )


# --------------------------------------------------------------------------
# Google Directions
# --------------------------------------------------------------------------

def google(key: str, origin: Point, dest: Point, waypoints: Sequence[Point] = (),
           mode: str = "driving", timeout: float = 25.0) -> RouteResult:
    if mode not in ("driving", "walking", "bicycling", "transit"):
        raise RouteError(f"不支持的 Google 出行方式: {mode}")
    params = {
        "key": key,
        "origin": f"{origin[0]:.6f},{origin[1]:.6f}",
        "destination": f"{dest[0]:.6f},{dest[1]:.6f}",
        "mode": mode,
    }
    if waypoints:
        params["waypoints"] = "|".join(f"{lat:.6f},{lon:.6f}" for lat, lon in waypoints)
    raw = _http_get(
        "https://maps.googleapis.com/maps/api/directions/json?" + urllib.parse.urlencode(params),
        timeout=timeout,
    )
    data = json.loads(raw)
    status = data.get("status")
    if status != "OK":
        raise RouteError(f"Google Directions 返回 {status}: {data.get('error_message', '')}")
    route = data["routes"][0]
    pts = geo.decode_polyline(route["overview_polyline"]["points"], precision=5)
    leg = route["legs"][0]
    return RouteResult(
        points=pts,
        provider=f"google/{mode}",
        distance_m=float(leg["distance"]["value"]),
        duration_s=float(leg["duration"]["value"]),
        profile=mode,
    )


# --------------------------------------------------------------------------
# 本地文件
# --------------------------------------------------------------------------

def from_file(path: Path) -> RouteResult:
    """读取本地路径文件，支持三种格式，自动识别：

      GeoJSON        Feature/FeatureCollection 里的 LineString
      纯坐标文本     每行 "lat,lon" 或 "lat lon"，可用 # 注释
      GPX            <trkpt lat="" lon=""> 轨迹点
    """
    if not path.exists():
        raise RouteError(f"文件不存在: {path}")
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if not text:
        raise RouteError(f"文件是空的: {path}")

    pts: List[Point] = []
    kind = ""

    if text[0] in "[{" and ('"type"' in text[:2000] or text[0] == "["):
        try:
            pts, kind = _parse_geojson(json.loads(text)), "geojson"
        except RouteError:
            raise
        except Exception as e:  # 不是合法 GeoJSON，往下走纯文本
            pts, kind = [], ""
    if not pts and ("<gpx" in text[:2000].lower()):
        pts, kind = _parse_gpx(text), "gpx"
    if not pts:
        pts, kind = _parse_plain(text), "plain"

    if len(pts) < 2:
        raise RouteError(f"从 {path} 只解析出 {len(pts)} 个点，至少需要 2 个")
    return RouteResult(
        points=pts, provider=f"file({path.name})",
        distance_m=geo.total_length(pts), profile="",
        notes=f"格式={kind}",
    )


def _parse_geojson(obj) -> List[Point]:
    def line_from(coords):
        # GeoJSON 是 [lon, lat] 顺序
        return [(float(c[1]), float(c[0])) for c in coords]

    if isinstance(obj, list):
        return line_from(obj)
    t = obj.get("type")
    if t == "FeatureCollection":
        out: List[Point] = []
        for f in obj.get("features", []):
            out.extend(_parse_geojson(f))
        return out
    if t == "Feature":
        return _parse_geojson(obj.get("geometry") or {})
    if t == "LineString":
        return line_from(obj["coordinates"])
    if t == "MultiLineString":
        out = []
        for part in obj["coordinates"]:
            out.extend(line_from(part))
        return out
    raise RouteError(f"不支持的 GeoJSON 类型: {t}")


def _parse_gpx(text: str) -> List[Point]:
    root = ET.fromstring(text)
    out: List[Point] = []
    for el in root.iter():
        if el.tag.endswith("trkpt") or el.tag.endswith("rtept") or el.tag.endswith("wpt"):
            try:
                out.append((float(el.attrib["lat"]), float(el.attrib["lon"])))
            except (KeyError, ValueError):
                continue
    return out


_NUM = re.compile(r"[-+]?\d+(?:\.\d+)?")


def _parse_plain(text: str) -> List[Point]:
    out: List[Point] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        nums = _NUM.findall(line)
        if len(nums) < 2:
            continue
        a, b = float(nums[0]), float(nums[1])
        # 自动判断顺序：纬度绝对值不可能超过 90
        if abs(a) > 90 and abs(b) <= 90:
            out.append((b, a))
        else:
            out.append((a, b))
    return out


# --------------------------------------------------------------------------
# 统一入口
# --------------------------------------------------------------------------

@dataclass
class ResolveOptions:
    provider: str = "osrm"
    osrm_base: str = "https://router.project-osrm.org"
    profile: str = "driving"
    amap_key: str = ""
    google_key: str = ""
    amap_mode: str = "driving"
    amap_strategy: int = 0
    city: str = ""
    input_coord: str = "wgs84"   # 输入起终点用的坐标系
    timeout: float = 25.0
    extra: dict = field(default_factory=dict)


def resolve(
    origin: Point, dest: Point, waypoints: Sequence[Point], opts: ResolveOptions
) -> RouteResult:
    """统一入口：不管哪个后端，返回的都是 WGS-84 折线。"""
    p = opts.provider.lower()

    if p in ("file", "path"):
        f = opts.extra.get("file")
        if not f:
            raise RouteError("provider=file 需要 --file 指定路径文件")
        return from_file(Path(f))

    # 先把输入统一成 WGS-84，因为各后端的坐标习惯不同
    if opts.input_coord.lower() != "wgs84" and p.startswith("osrm"):
        origin, dest = geo.convert_points([origin, dest], opts.input_coord, "wgs84")
        waypoints = geo.convert_points(waypoints, opts.input_coord, "wgs84")

    if p.startswith("osrm"):
        res = osrm(opts.osrm_base, [origin, *waypoints, dest], opts.profile, opts.timeout)
    elif p == "amap":
        if not opts.amap_key:
            raise RouteError("高德需要 key。用 --amap-key 传入，或设置环境变量 AMAP_KEY")
        # 高德要 GCJ-02
        o, d = geo.convert_points([origin, dest], "wgs84", "gcj02")
        wps = geo.convert_points(waypoints, "wgs84", "gcj02")
        res = amap(opts.amap_key, o, d, wps, opts.amap_mode, opts.amap_strategy, opts.city, opts.timeout)
    elif p == "google":
        if not opts.google_key:
            raise RouteError("Google 需要 key。用 --google-key 传入，或设置环境变量 GOOGLE_MAPS_KEY")
        res = google(opts.google_key, origin, dest, waypoints, opts.profile, opts.timeout)
    else:
        raise RouteError(f"未知 provider: {opts.provider}（可用 osrm / amap / google / file）")

    # 抽稀：路网数据常有上千个点，抽到 2 米容差对结果没有可见影响
    before = len(res.points)
    res.points = geo.simplify(res.points, tolerance_m=2.0)
    if before != len(res.points):
        res.notes = (res.notes + f"；抽稀 {before}→{len(res.points)} 点").strip("；")
    return res
