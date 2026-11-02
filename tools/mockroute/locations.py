"""起点/终点的多种指定方式。

除了直接给经纬度，还支持：

  1. 命名地点（alias）   —— 存一次性，之后写名字就行。完全离线，最可靠。
  2. 地名解析（geocode） —— "天安门" / "杭州西湖" 直接转坐标。需要网络。
  3. 设备当前位置        —— 从 adb 读手机的真实位置当起点。
  4. 文件导入            —— GeoJSON / GPX / 纯文本（见 route.from_file）。

解析顺序（resolve）：
    经纬度字面量  →  命名地点  →  地名解析（若开启）
这样同一套参数既能写 "39.9,116.4"，也能写 "天安门"。
"""

from __future__ import annotations

import json
import math
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple

from . import geo

# 存放命名地点。跟项目走，方便把常用点提交进版本库共享。
ALIAS_FILE = "locations.json"

_UA = {"User-Agent": "mockroute/1.0 (location-simulation-for-testing)"}
_COORD_RE = re.compile(
    r"^\s*([-+]?\d+(?:\.\d+)?)\s*[,，/\s]\s*([-+]?\d+(?:\.\d+)?)\s*$")


class ResolveError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# 命名地点
# --------------------------------------------------------------------------

def alias_path(root: Path) -> Path:
    return Path(root) / ALIAS_FILE


def _load_raw(root: Path) -> dict:
    p = alias_path(root)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:                                          # noqa: BLE001
        raise ResolveError(f"{p} 解析失败: {e}") from e
    if not isinstance(data, dict):
        raise ResolveError(f"{p} 顶层应该是对象（名称 -> 位置/路径）")
    return data


def _write_raw(root: Path, data: dict) -> Path:
    p = alias_path(root)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                 encoding="utf-8")
    return p


def load_aliases(root: Path) -> dict:
    """命名地点表。

    同一个文件里还存着路径（见 load_paths）。这里**只返回地点**，
    靠「有没有 lat 字段」区分，所以旧文件不需要迁移。
    """
    return {k: v for k, v in _load_raw(root).items()
            if isinstance(v, dict) and "lat" in v and "points" not in v}


def save_alias(root: Path, name: str, lat: float, lon: float,
               coord: str = "wgs84", note: str = "") -> Path:
    data = _load_raw(root)
    data[name] = {"kind": "point", "lat": round(lat, 7), "lon": round(lon, 7),
                  "coord": coord, "note": note}
    return _write_raw(root, data)


def delete_alias(root: Path, name: str) -> bool:
    data = _load_raw(root)
    if name not in data:
        return False
    del data[name]
    _write_raw(root, data)
    return True


# --------------------------------------------------------------------------
# 保存的路径（起点 + 途经点 + 终点），存下来可反复使用
# --------------------------------------------------------------------------

def load_paths(root: Path) -> dict:
    """已保存的路径。与命名地点共用一个文件，用 kind/points 区分。"""
    out = {}
    for k, v in _load_raw(root).items():
        if isinstance(v, dict) and v.get("kind") == "path" and "points" in v:
            out[k] = v
    return out


def save_path(root: Path, name: str, points: list, note: str = "",
              mode: str = "once", preset: str = "") -> Path:
    """保存一条选点路径。

    points 是 WGS-84 的 [(lat, lon), ...]，至少 2 个（起点+终点）。
    第一个是起点，最后一个是终点，中间都是途经点。
    """
    pts = [[round(float(a), 7), round(float(b), 7)] for a, b in points]
    if len(pts) < 2:
        raise ResolveError("路径至少要 2 个点（起点 + 终点）")
    data = _load_raw(root)
    data[name] = {
        "kind": "path",
        "points": pts,
        "mode": mode,
        "preset": preset,
        "note": note,
    }
    return _write_raw(root, data)


def delete_path(root: Path, name: str) -> bool:
    data = _load_raw(root)
    e = data.get(name)
    if not isinstance(e, dict) or e.get("kind") != "path":
        return False
    del data[name]
    _write_raw(root, data)
    return True


def path_to_cli(path_entry: dict) -> dict:
    """把保存的路径转成 gen 命令要的 --from / --via / --to 字符串。"""
    pts = path_entry.get("points") or []
    if len(pts) < 2:
        raise ResolveError("这条路径的点不足 2 个，无法使用")
    fmt = lambda p: f"{p[0]:.6f},{p[1]:.6f}"           # noqa: E731
    return {
        "from": fmt(pts[0]),
        "via": [fmt(p) for p in pts[1:-1]],
        "to": fmt(pts[-1]),
        "mode": path_entry.get("mode", "once"),
        "preset": path_entry.get("preset", ""),
        "note": path_entry.get("note", ""),
    }


# --------------------------------------------------------------------------
# 经纬度字面量
# --------------------------------------------------------------------------

def parse_coord_literal(s: str) -> Optional[Tuple[float, float]]:
    """能认出来就返回 (lat, lon)，否则 None。

    兼容：39.9,116.4 ／ 39.9 116.4 ／ 39.9/116.4 ／ 116.4,39.9（自动换序）。
    注意「自动换序」只在明显反了的时候生效（纬度绝对值 > 90）。
    """
    m = _COORD_RE.match(s or "")
    if not m:
        return None
    try:
        a, b = float(m.group(1)), float(m.group(2))
    except ValueError:
        return None
    # 纬度绝对值不可能超过 90：超了说明写成了「经度,纬度」
    if abs(a) > 90 and abs(b) <= 90:
        a, b = b, a
    if not (-90 <= a <= 90) or not (-180 <= b <= 180):
        return None
    return a, b


# --------------------------------------------------------------------------
# 地名解析
# --------------------------------------------------------------------------

@dataclass
class Place:
    name: str
    lat: float          # WGS-84
    lon: float
    provider: str = ""
    detail: str = ""
    coord_src: str = "wgs84"


def geocode_amap(key: str, query: str, city: str = "",
                 timeout: float = 20.0) -> Place:
    """高德地理编码。国内 POI 最准（店名、小区名都能查）。

    高德返回 GCJ-02，这里统一反解成 WGS-84 —— 与设备定位一致。
    """
    if not key:
        raise ResolveError("高德需要 key：--amap-key 或环境变量 AMAP_KEY")
    params = {"key": key, "address": query, "output": "json"}
    if city:
        params["city"] = city
    url = "https://restapi.amap.com/v3/geocode/geo?" + urllib.parse.urlencode(params)
    data = _get_json(url, timeout)
    if str(data.get("status")) != "1":
        raise ResolveError(f"高德返回错误: infocode={data.get('infocode')} "
                           f"info={data.get('info')}")
    geos = data.get("geocodes") or []
    if not geos:
        raise ResolveError(f"高德查不到这个地名: {query}（可加 --city 限定城市）")
    g = geos[0]
    try:
        glon, glat = (float(v) for v in g["location"].split(","))
    except Exception as e:                                          # noqa: BLE001
        raise ResolveError(f"高德返回的坐标无法解析: {g.get('location')!r}") from e
    wlat, wlon = geo.gcj02_to_wgs84(glat, glon)
    return Place(name=query, lat=wlat, lon=wlon, provider="amap",
                 detail=f"{g.get('formatted_address', '')} "
                        f"({g.get('province', '')}{g.get('city', '')}{g.get('district', '')})",
                 coord_src="gcj02")


def geocode_nominatim(query: str, timeout: float = 25.0) -> Place:
    """OSM Nominatim。免费无需 key，但国内常被墙，且中文 POI 覆盖差。"""
    url = ("https://nominatim.openstreetmap.org/search?format=json&limit=1&q="
           + urllib.parse.quote(query))
    data = _get_json(url, timeout)
    if not isinstance(data, list) or not data:
        raise ResolveError(f"Nominatim 查不到: {query}")
    it = data[0]
    return Place(name=query, lat=float(it["lat"]), lon=float(it["lon"]),
                 provider="nominatim", detail=it.get("display_name", ""),
                 coord_src="wgs84")


def geocode_photon(query: str, limit: int = 1, lat: float = None,
                   lon: float = None, timeout: float = 20.0) -> Place:
    """Photon（Komoot 的 OSM 检索服务）。免费、无需 key。

    实测在这台机器上 **可达**，而 Nominatim 超时 —— 所以国内环境优先用它。
    中文支持不错（「天安门」「天坛」都能查到），但 OSM 的国内 POI 密度不如高德：
    小店铺、小区名往往查不到，这时要用高德或命名地点。

    带 `lat/lon` 时会偏向该坐标附近的结果，用来做「视野内搜索」。
    """
    results = search_photon(query, limit=limit, lat=lat, lon=lon, timeout=timeout)
    if not results:
        raise ResolveError(f"Photon 查不到: {query}")
    return results[0]


def search_photon(query: str, limit: int = 8, lat: float = None,
                  lon: float = None, timeout: float = 20.0) -> list:
    """Photon 检索，返回 Place 列表（可能为空）。

    加了 lat/lon 之后结果会向该点聚集，适合「在当前地图视野附近找」。
    """
    params = {"q": query, "limit": max(1, min(30, limit))}
    if lat is not None and lon is not None:
        params["lat"] = f"{lat:.6f}"
        params["lon"] = f"{lon:.6f}"
    url = "https://photon.komoot.io/api/?" + urllib.parse.urlencode(params)
    data = _get_json(url, timeout)
    out = []
    for f in (data.get("features") or []):
        try:
            lon_f, lat_f = f["geometry"]["coordinates"][:2]
        except (KeyError, IndexError, TypeError):
            continue
        p = f.get("properties") or {}
        name = p.get("name") or p.get("street") or p.get("city") or "?"
        bits = []
        for k in ("street", "housenumber", "district", "city", "county",
                  "state", "postcode", "country"):
            v = p.get(k)
            if v and str(v) not in name and str(v) not in bits:
                bits.append(str(v))
        kind = "/".join(x for x in (p.get("osm_key"), p.get("osm_value")) if x)
        out.append(Place(
            name=name, lat=float(lat_f), lon=float(lon_f), provider="photon",
            detail=" · ".join(bits) + (f"  [{kind}]" if kind else ""),
            coord_src="wgs84"))
    return out


def search_amap(key: str, query: str, city: str = "", limit: int = 8,
                lat: float = None, lon: float = None,
                timeout: float = 20.0) -> list:
    """高德输入提示（POI 检索）。国内覆盖最好，但需要 key。

    同时返回行政区划与 POI。「天安门」这类地标、店名、小区名都查得到。
    """
    if not key:
        raise ResolveError("高德检索需要 key")
    params = {"key": key, "keywords": query, "output": "json",
              "offset": str(max(1, min(20, limit)))}
    if city:
        params["city"] = city
    if lat is not None and lon is not None:
        # 高德要求「经度,纬度」
        params["location"] = f"{lon:.6f},{lat:.6f}"
    url = "https://restapi.amap.com/v3/assistant/inputtips?" + urllib.parse.urlencode(params)
    data = _get_json(url, timeout)
    if str(data.get("status")) != "1":
        raise ResolveError(f"高德检索失败: infocode={data.get('infocode')} "
                           f"info={data.get('info')}")
    out = []
    for t in (data.get("tips") or []):
        loc = t.get("location")
        if not loc or "," not in loc:
            continue
        try:
            glon, glat = (float(v) for v in loc.split(","))
        except ValueError:
            continue
        wlat, wlon = geo.gcj02_to_wgs84(glat, glon)
        bits = [x for x in (t.get("district"), t.get("address")) if x]
        out.append(Place(name=t.get("name") or query, lat=wlat, lon=wlon,
                         provider="amap", detail=" · ".join(bits),
                         coord_src="gcj02"))
    return out


def search(query: str, provider: str = "auto", amap_key: str = "",
           city: str = "", limit: int = 8, lat: float = None, lon: float = None,
           timeout: float = 20.0) -> list:
    """按关键词检索地点，返回 Place 列表。

    多来源依次尝试，谁先给出结果就用谁：
      1. 高德（有 key 时）—— 国内 POI 覆盖最好
      2. Photon —— 免费无需 key，实测国内环境可达
      3. Nominatim —— 免费，但国内常超时

    每个来源失败只记下来，不影响后面的尝试；全都失败才报错。
    """
    errors = []
    order = []
    if provider in ("auto", "amap") and amap_key:
        order.append("amap")
    if provider in ("auto", "photon"):
        order.append("photon")
    if provider in ("auto", "nominatim"):
        order.append("nominatim")
    if provider == "amap" and not amap_key:
        raise ResolveError("provider=amap 需要 key（--amap-key 或环境变量 AMAP_KEY）")

    for name in order:
        try:
            if name == "amap":
                r = search_amap(amap_key, query, city, limit, lat, lon, timeout)
            elif name == "photon":
                r = search_photon(query, limit, lat, lon, timeout)
            else:
                r = [geocode_nominatim(query, timeout)]
            if r:
                return r
            errors.append(f"{name}: 无结果")
        except ResolveError as e:
            errors.append(f"{name}: {e}")
        except Exception as e:                                       # noqa: BLE001
            errors.append(f"{name}: {type(e).__name__}: {e}")

    raise ResolveError(
        "所有检索来源都没拿到结果：\n  " + "\n  ".join(errors) +
        "\n国内建议配 AMAP_KEY 用高德检索。")


def _get_json(url: str, timeout: float):
    req = urllib.request.Request(url, headers=_UA)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except Exception as e:                                          # noqa: BLE001
        raise ResolveError(
            f"网络请求失败（{type(e).__name__}）。国内环境 OSM 系服务常不可达，"
            f"建议配 AMAP_KEY 用高德，或改用命名地点/坐标。") from e
    try:
        return json.loads(raw)
    except Exception as e:                                          # noqa: BLE001
        raise ResolveError(f"返回内容不是 JSON: {raw[:120]!r}") from e


def geocode(query: str, provider: str = "auto", amap_key: str = "",
            city: str = "", timeout: float = 20.0) -> Place:
    """地名 -> 坐标。内部走 search()，拿到第一个结果。

    来源优先级：高德（有 key）→ Photon（免费可达）→ Nominatim。
    """
    results = search(query, provider=provider, amap_key=amap_key, city=city,
                     limit=1, timeout=timeout)
    return results[0]


# --------------------------------------------------------------------------
# 设备当前位置
# --------------------------------------------------------------------------

_LOC_RE = re.compile(
    r"Location\[(?P<prov>\w+)\s+(?P<lat>[-+]?\d+(?:\.\d+)?)\s*,\s*"
    r"(?P<lon>[-+]?\d+(?:\.\d+)?)(?P<rest>[^\]]*)\]")


def parse_dumpsys_location(text: str) -> dict:
    """从 `dumpsys location` 的输出里抽位置。

    返回 {"real": Place|None, "mock": Place|None, "redacted": bool}

    ⚠️ Android 10+ 会对这个输出做隐私打码（`40******,116******`），
    这时只能报 redacted=True，让上层给出替代方案。
    """
    out = {"real": None, "mock": None, "redacted": False}
    if re.search(r"\d\*{3,}", text):
        out["redacted"] = True
    for m in _LOC_RE.finditer(text):
        try:
            lat, lon = float(m.group("lat")), float(m.group("lon"))
        except ValueError:
            continue
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        is_mock = "mock" in m.group("rest")
        p = Place(name="device", lat=lat, lon=lon,
                  provider=f"device/{m.group('prov')}",
                  detail=("MockRoute 虚拟位置" if is_mock else "设备真实位置"))
        if is_mock:
            out["mock"] = out["mock"] or p
        else:
            out["real"] = out["real"] or p
    return out


# --------------------------------------------------------------------------
# 统一解析
# --------------------------------------------------------------------------

@dataclass
class ResolveOptions:
    aliases: dict = None                # 命名地点表
    amap_key: str = ""
    city: str = ""
    geocode_provider: str = "auto"
    allow_geocode: bool = True
    timeout: float = 20.0


def resolve_location(text: str, opts: ResolveOptions) -> Tuple[float, float, Place]:
    """把用户写的一个字符串解析成 (lat, lon, 来源说明)。

    顺序：经纬度 → 命名地点 → 地名解析。
    """
    s = (text or "").strip()
    if not s:
        raise ResolveError("位置为空")

    c = parse_coord_literal(s)
    if c:
        return c[0], c[1], Place(name=s, lat=c[0], lon=c[1],
                                 provider="coords", detail="直接给了经纬度")

    aliases = opts.aliases or {}
    if s in aliases:
        e = aliases[s]
        lat, lon = float(e["lat"]), float(e["lon"])
        coord = e.get("coord", "wgs84")
        if coord != "wgs84":
            lat, lon = geo.convert_points([(lat, lon)], coord, "wgs84")[0]
        return lat, lon, Place(name=s, lat=lat, lon=lon, provider="alias",
                               detail=e.get("note", "") or "命名地点")

    if opts.allow_geocode:
        p = geocode(s, opts.geocode_provider, opts.amap_key, opts.city, opts.timeout)
        return p.lat, p.lon, p

    known = ", ".join(list(aliases)[:8]) or "(暂无)"
    raise ResolveError(
        f"无法解析位置 {s!r}。\n"
        f"  可以用：经纬度（39.9,116.4）／ 命名地点 ／ 地名解析。\n"
        f"  当前已有的命名地点：{known}\n"
        f"  要新增：python tools/mroute.py alias add \"家\" 39.9087,116.3975")


def format_place(p: Place) -> str:
    bits = [f"{p.lat:.6f},{p.lon:.6f}", f"来源={p.provider}"]
    if p.detail:
        bits.append(p.detail.strip())
    return "  ".join(bits)
