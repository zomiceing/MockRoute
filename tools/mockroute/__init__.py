"""mockroute —— PC 侧工具包。

模块职责：
  geo.py       大地测量与坐标系转换（WGS-84 / GCJ-02 / BD-09）
  traj.py      轨迹生成（速度剖面、噪声、参数自洽）
  route.py     路网路径规划（OSRM / 高德 / Google / 本地文件）
  protocol.py  广播协议常量与 adb 封装（与 Android 端共用同一份契约）
  player.py    播放控制（设备回放 / 直推两种模式）
"""

__version__ = "1.0.0"
