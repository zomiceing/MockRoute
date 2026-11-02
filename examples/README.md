# 示例路径文件

## ⚠️ 先看这里：两种文件性质完全不同

| 文件 | 性质 | 能用吗 |
| --- | --- | --- |
| `road-tiananmen.txt`、`road-to-tiantan.txt` | **真实道路**（从 OSRM 路网导出） | ✅ 可以当真实路线用 |
| `path-demo.txt`、`route-loop.geojson`、`path-demo.gpx` | **合成几何**（形状像路，但不是路） | ❌ 只给测试用 |
| `road-changan.txt` | 真实道路（长安街段） | ✅ |

合成几何**不代表任何真实道路** —— `path-demo.txt` 有一段甚至穿过故宫。
拿它生成轨迹会得到「只按走势行走、完全不在路上」的结果。

## 怎么自己造真实路径

```powershell
# 1) 从真实路网导出一段路径为纯文本
python tools\mroute.py export --from 天安门城楼 --to 天坛公园 `
    --out examples\my-path.txt

# 2) 离线复用它（不需要网络）
python tools\mroute.py gen --preset walk --provider file `
    --file examples\my-path.txt --seed 42 --out out\my-run.json
```

## 怎么确认轨迹真的在路上

`verify-route.py` 只是把轨迹画到地图上看，2 米的偏差在图上就几个像素，
看不出来。要**客观判定**得对比真实道路数据：

```powershell
python tools\check-onroad.py out\my-run.json
```

它用 OSM 的 Overpass API 拉取范围内的真实道路，逐点算「到最近道路的距离」：

```
抽样 121 个点，到最近真实道路的距离：
  中位 1.1 m   90分位 2.6 m   最大 4.6 m
  超过 5m 的点: 0 / 121 (0%)
[OK] 全部在道路 5m 范围内 —— 确实贴着路走
```

对照一下合成几何的结果：

```
中位 138.5 m   90分位 215.6 m   最大 238.5 m
超过 5m 的点: 138 / 138 (100%)
[XX] 大量点不在道路上
```

## 各文件用途

| 文件 | 说明 |
| --- | --- |
| `road-tiananmen.txt` | 天安门 → 天安门东（长安街段），7 个路径点 / 1685 m |
| `road-to-tiantan.txt` | 天安门 → 天坛公园，16 个路径点 / 4698 m |
| `road-changan.txt` | 长安街东段 |
| `path-demo.txt` | **合成**折线，测试夹具（不联网也能跑通流程） |
| `route-loop.geojson` | **合成**闭环，测 `loop` / `pingpong` 模式 |
| `path-demo.gpx` | **合成** GPX，测 GPX 解析 |
