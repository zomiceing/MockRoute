"""地图选点对话框 —— 用鼠标在地图上点，直接得到经纬度。

为什么用高德瓦片：实测这台机器上 OSM / Carto 瓦片全部超时，只有高德通
（`webrd0X.is.autonavi.com`，无需 key，国内速度快）。ArcGIS 也可用。

交互：
    左键拖动 = 平移     滚轮 = 缩放     双击 = 选中该点
    单击 = 移动准星（再点【确定】）
"""

from __future__ import annotations

import io
import math
import queue
import threading
import tkinter as tk
import urllib.request
import warnings
from tkinter import messagebox, simpledialog, ttk

from PIL import Image, ImageTk

# 高德瓦片是带透明通道的调色板 PNG，转 RGB 时 PIL 会发这个警告。
# 对我们没有影响（透明像素会被合成到画布的底色上），这里直接静音。
warnings.filterwarnings("ignore", message="Palette images with Transparency")

TILE = 256
MAX_LAT = 85.05112878

# 瓦片源。第 4 个元素是**坐标系**，这一点非常关键：
#   高德瓦片是 GCJ-02（火星坐标），而 OSRM 路网、安卓定位、本工具内部
#   全部用 WGS-84。把高德瓦片上点出来的坐标直接当 WGS-84 用，在国内会
#   整体偏 500~600 米（实测北京 555m、上海 479m、深圳 606m）——
#   表现就是「电脑上标的路线和手机上地图跑的路线不一致」。
#   ArcGIS 影像是 WGS-84，不需要转换。
TILE_URLS = [
    # 高德矢量瓦片（style=8 是标准路网底图）
    ("高德路网", "https://webrd0{s}.is.autonavi.com/appmaptile"
                 "?lang=zh_cn&size=1&scale=1&style=8&x={x}&y={y}&z={z}", 4, "gcj02"),
    # ArcGIS 世界影像（备用，中文标注少）
    ("ArcGIS 影像", "https://server.arcgisonline.com/ArcGIS/rest/services/"
                    "World_Imagery/MapServer/tile/{z}/{y}/{x}", 1, "wgs84"),
]
UA = {"User-Agent": "Mozilla/5.0 (mockroute map picker)"}


def lonlat_to_world(lon: float, lat: float, z: int) -> tuple:
    """经纬度 -> 该缩放级别下的全局像素坐标（Web Mercator）。

    对 GCJ-02 瓦片，传进来的 lon/lat 必须也是 GCJ-02，这样底图才对得上。
    """
    n = TILE * (2 ** z)
    lat = max(-MAX_LAT, min(MAX_LAT, lat))
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


def world_to_lonlat(x: float, y: float, z: int) -> tuple:
    """全局像素坐标 -> 经纬度（与瓦片同一坐标系）。"""
    n = TILE * (2 ** z)
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / n))))
    return lon, lat


def to_wgs(lon: float, lat: float, coord_sys: str) -> tuple:
    """瓦片坐标系 -> WGS-84（本工具内部统一用它）。"""
    if coord_sys == "gcj02":
        from . import geo
        return geo.gcj02_to_wgs84(lat, lon)
    return lat, lon


def from_wgs(lon: float, lat: float, coord_sys: str) -> tuple:
    """WGS-84 -> 瓦片坐标系（定位/画标记时要用它）。"""
    if coord_sys == "gcj02":
        from . import geo
        return geo.wgs84_to_gcj02(lat, lon)
    return lat, lon


class SearchPanel(ttk.Frame):
    """地图选点里的检索框：输入关键词 -> 列出候选 -> 点选后跳到该地。

    数据来源按可用性依次尝试（见 locations.search）：
      高德（有 key 时，国内 POI 最全）→ Photon（免费、无需 key，实测可达）
    检索在后台线程跑，界面不卡；带当前视野坐标做「偏向附近」。
    """

    def __init__(self, master, on_pick, get_center=None, amap_key="", city="",
                 width=30):
        super().__init__(master)
        self.on_pick = on_pick                 # 回调 (lat, lon, name, detail)
        self.get_center = get_center or (lambda: (None, None))
        self.amap_key = amap_key
        self.city = city
        self.results: list = []
        self._busy = False
        self._q: queue.Queue = queue.Queue()
        self._polling = False

        row = ttk.Frame(self)
        row.pack(fill="x")
        self.var_q = tk.StringVar()
        ent = ttk.Entry(row, textvariable=self.var_q, width=width)
        ent.pack(side="left", fill="x", expand=True)
        # 必须返回 "break" 阻断冒泡！
        # 否则回车会继续传给窗口级的 <Return>（那个是「确认」），
        # 结果「在搜索框里敲回车」= 直接确认并关闭窗口。这是个真实踩过的 bug。
        ent.bind("<Return>", self._on_enter)
        ent.bind("<KP_Enter>", self._on_enter)
        self.btn = ttk.Button(row, text="检索", width=6, command=self.do_search)
        self.btn.pack(side="left", padx=(4, 0))

        self.var_hint = tk.StringVar(value="输入地名/建筑名/路名后回车")
        ttk.Label(self, textvariable=self.var_hint, foreground="#777",
                  font=("", 8)).pack(anchor="w", pady=(2, 2))

        self.lst = tk.Listbox(self, height=7, font=("", 9))
        self.lst.pack(fill="both", expand=True)
        self.lst.bind("<Double-Button-1>", lambda e: self._pick())
        # 列表里的回车 = 选中这一条，同样要 break，别冒泡去确认
        self.lst.bind("<Return>", self._on_enter)
        self.lst.bind("<<ListboxSelect>>", lambda e: self._preview())

        b = ttk.Frame(self)
        b.pack(fill="x", pady=(2, 0))
        ttk.Button(b, text="选这个", width=8, command=self._pick).pack(side="left")
        ttk.Button(b, text="清空", width=6,
                   command=self._clear).pack(side="left", padx=4)

    # ---------- 检索 ----------

    def _on_enter(self, event=None):
        """回车：有结果就选中当前项，否则发起检索。**必须返回 "break"**。

        返回 "break" 会终止 Tk 的事件传播链，阻止它继续冒泡到窗口级的
        <Return> 绑定。否则「在搜索框里敲回车」会被父窗口当成「确认」，
        直接把窗口关掉 —— 这正是用户报的那个 bug。
        """
        if self.lst.curselection() or self.results:
            self._pick()
        else:
            self.do_search()
        return "break"

    def do_search(self):
        q = self.var_q.get().strip()
        if not q or self._busy:
            return
        self._busy = True
        self.btn.configure(state="disabled")
        self.var_hint.set(f"检索「{q}」中…")
        self.lst.delete(0, "end")
        lat, lon = self.get_center()

        def work():
            # ⚠️ 后台线程里**绝对不能**碰任何 Tk 控件，包括 self.after()
            # （Tkinter 不是线程安全的，会抛 "main thread is not in main loop"）。
            # 所以这里只把结果丢进队列，由主线程轮询消费。
            try:
                from . import locations as locm
                res = locm.search(q, provider="auto", amap_key=self.amap_key,
                                  city=self.city, limit=12,
                                  lat=lat, lon=lon)
                self._q.put(("ok", res, q))
            except Exception as e:                                   # noqa: BLE001
                self._q.put(("err", e, q))

        threading.Thread(target=work, daemon=True).start()
        if not self._polling:
            self._polling = True
            self.after(120, self._poll)

    def _poll(self):
        """主线程轮询：把后台线程放进队列的结果落到界面上。"""
        try:
            while True:
                kind, payload, q = self._q.get_nowait()
                if kind == "ok":
                    self._show(payload, q)
                else:
                    self._fail(payload)
        except queue.Empty:
            pass
        # 还有在飞的检索就继续轮询；注意控件可能已被销毁
        if self._busy:
            try:
                self.after(120, self._poll)
            except tk.TclError:
                self._polling = False
        else:
            self._polling = False

    def _show(self, res, q):
        self.results = res
        self._busy = False
        self.btn.configure(state="normal")
        for p in res:
            self.lst.insert("end", f"{p.name}   {p.lat:.5f},{p.lon:.5f}")
        src = res[0].provider if res else "?"
        self.var_hint.set(f"「{q}」{len(res)} 条结果（来源 {src}）"
                          "；双击或点「选这个」定位")

    def _fail(self, e):
        self._busy = False
        self.btn.configure(state="normal")
        self.results = []
        msg = str(e)
        if "所有检索来源" in msg or "网络" in msg:
            self.var_hint.set("检索失败：网络不可达。可用命名地点或直接点地图。")
        elif "key" in msg.lower():
            self.var_hint.set("检索失败：需要高德 key（也可不配，会用 Photon）")
        else:
            self.var_hint.set(f"检索失败：{msg[:70]}")

    def _sel(self):
        s = self.lst.curselection()
        return s[0] if s else None

    def _preview(self):
        """单击列表项时，只把地图中心移过去（不确认选点）。"""
        i = self._sel()
        if i is None or i >= len(self.results):
            return
        p = self.results[i]
        self.on_pick(p.lat, p.lon, p.name, p.detail, preview=True)

    def _pick(self):
        i = self._sel()
        if i is None:
            # 没选就默认第一个
            if not self.results:
                return
            i = 0
        p = self.results[i]
        self.on_pick(p.lat, p.lon, p.name, p.detail, preview=False)

    def _clear(self):
        self.lst.delete(0, "end")
        self.results = []
        self.var_hint.set("已清空")


class TileCache:
    """瓦片内存缓存 + 后台下载，避免界面卡顿。"""

    def __init__(self):
        self.mem: dict = {}
        self.pending: set = set()
        self.lock = threading.Lock()
        self.src_idx = 0
        self.errors: dict = {}

    def url(self, z: int, x: int, y: int) -> str:
        _, tpl, nsub, _ = TILE_URLS[self.src_idx]
        sub = (x + y) % nsub + 1
        try:
            return tpl.format(s=sub, x=x, y=y, z=z)
        except (KeyError, IndexError):
            return tpl.format(x=x, y=y, z=z)

    @property
    def coord_sys(self) -> str:
        """当前底图的坐标系：'gcj02'（高德）或 'wgs84'（ArcGIS）。"""
        return TILE_URLS[self.src_idx][3]

    def get(self, z: int, x: int, y: int):
        key = (z, x, y)
        with self.lock:
            if key in self.mem:
                return self.mem[key]
            if key in self.pending:
                return None
            self.pending.add(key)
        threading.Thread(target=self._fetch, args=(key,), daemon=True).start()
        return None

    def _fetch(self, key):
        z, x, y = key
        try:
            req = urllib.request.Request(self.url(z, x, y), headers=UA)
            with urllib.request.urlopen(req, timeout=20) as r:
                img = Image.open(io.BytesIO(r.read())).convert("RGB")
            with self.lock:
                self.mem[key] = img
                self.errors.pop(key, None)
        except Exception as e:                                       # noqa: BLE001
            with self.lock:
                self.errors[key] = f"{type(e).__name__}: {e}"
        finally:
            with self.lock:
                self.pending.discard(key)

    def use_source(self, idx: int):
        with self.lock:
            self.src_idx = idx
            self.mem.clear()
            self.errors.clear()

    def missing(self, keys) -> list:
        with self.lock:
            return [k for k in keys if k not in self.mem]


class MapPicker(tk.Toplevel):
    """地图选点窗口。on_confirm 回调收到 (lat, lon)。

    支持三种定位方式：地图上点、检索地名、拖动平移。
    """

    def __init__(self, master, initial_lat: float, initial_lon: float,
                 on_confirm, title="地图选点 — 单击定位，双击确认",
                 amap_key="", city=""):
        super().__init__(master)
        self.title(title)
        self.geometry("900x720")
        self.transient(master)
        self.on_confirm = on_confirm
        self.cache = TileCache()
        self.amap_key = amap_key
        self.city = city

        # lat/lon 存**底图坐标系**（高德=GCJ-02），不是 WGS-84。
        self.lat, self.lon = self._to_view(initial_lat, initial_lon)
        self.zoom = 16
        self.dragging = False
        self.drag_from = (0, 0)
        self.marks: list = []            # 已选中的点 [(lat, lon)]
        # 是否真的选过点。没有就禁止确认 —— 防止「初始中心」被当成用户选择
        # 悄悄填进起终点（用户报的 bug 就包含这一半）。
        self._picked = False
        self._pick_name = ""
        self._after_id = None
        # 窗口销毁后必须停止重绘：redraw 会用 after() 自我重排，
        # 若不取消，关窗后回调仍会在已销毁的控件上执行并抛
        # `invalid command name ...redraw`。两端都加这个守卫。
        self._alive = True
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # <Destroy> 在**两条路**上都会触发：用户点 X（走 _on_close），
        # 以及父窗口被销毁（不经过 _on_close）。所以清理必须挂在这里，
        # 否则父窗口一关，挂起的 after 回调就会变成孤儿并抛
        # invalid command name "...redraw"。
        self.bind("<Destroy>", self._on_destroy)

        self._build()
        self._schedule_redraw(200)

    # ---------- 坐标系 ----------
    # 底图是 GCJ-02（高德）时，屏幕像素算出来的就是 GCJ-02 坐标；
    # 而用户看到的、安卓系统给的、OSRM 路网用的都是 WGS-84。
    # 不转换会整体偏 500~600 米 —— 表现正是
    # 「电脑上标的路线和手机上地图跑的不一致」。
    # 约定：self.lat/self.lon 存**底图坐标系**，只在边界转换。

    def _to_view(self, lat: float, lon: float) -> tuple:
        """WGS-84 -> 底图坐标系（定位、画标记用）。"""
        return from_wgs(lon, lat, self.cache.coord_sys)

    def _to_wgs(self, lat: float, lon: float) -> tuple:
        """底图坐标系 -> WGS-84（输出给用户用）。"""
        return to_wgs(lon, lat, self.cache.coord_sys)

    def _center_wgs(self) -> tuple:
        """当前视野中心的 WGS-84 坐标（检索做「偏向附近」用）。"""
        return self._to_wgs(self.lat, self.lon)

    def _schedule_redraw(self, ms):
        """安排一次重绘。

        关键点：**先取消已有的挂起回调**。redraw 会被很多地方调用
        （Configure、点击、滚轮、检索…），如果每次都直接 after()，
        _after_id 会被覆盖，先前那个就再也取消不掉 —— 窗口销毁后它照样
        执行，抛 invalid command name "...redraw"。
        这里保证同一时刻最多只有一个挂起的重绘。
        """
        if not getattr(self, "_alive", True):
            return
        old = getattr(self, "_after_id", None)
        if old:
            try:
                self.after_cancel(old)
            except Exception:                                        # noqa: BLE001
                pass
            self._after_id = None
        try:
            self._after_id = self.after(ms, self.redraw)
        except tk.TclError:
            self._alive = False

    def _on_destroy(self, event=None):
        if event is not None and event.widget is not self:
            return
        self._alive = False
        aid = getattr(self, "_after_id", None)
        if aid:
            try:
                self.after_cancel(aid)
            except Exception:                                        # noqa: BLE001
                pass
            self._after_id = None

    def _on_close(self):
        """关窗时**必须取消**已排队的 after 回调。

        只在 redraw 开头查 _alive 是不够的 —— 那个回调已经排进 Tk 的事件队列，
        窗口销毁后它仍会执行并抛 invalid command name "...redraw"。
        """
        self._alive = False
        aid = getattr(self, "_after_id", None)
        if aid:
            try:
                self.after_cancel(aid)
            except Exception:                                        # noqa: BLE001
                pass
            self._after_id = None
        self.destroy()

    def _on_search_pick(self, lat, lon, name, detail, preview=False):
        """检索结果被选中：把地图移过去；非预览时同时把准星定到那里。

        单击列表项走 preview（只移动视野，便于先看一眼）；双击或「选这个」
        走正式选择。
        """
        self.lat, self.lon = self._to_view(lat, lon)   # 检索给的是 WGS-84
        # 视野移动后缩放级别不变，检索到的点若在很远处会一次性跳过去
        self._last_pick = (lat, lon, name)
        self._picked = True
        self._pick_name = name
        self.redraw()
        if not preview:
            self._confirm()
        else:
            self.var_info.set(f"预览：{name}  {lat:.6f},{lon:.6f}"
                              "   —— 双击列表项或点【确定】应用")

    # ---------- 界面 ----------

    def _build(self):
        bar = ttk.Frame(self)
        bar.pack(fill="x", padx=8, pady=(6, 2))

        ttk.Label(bar, text="单击 = 定位准星    双击 = 定位并确认    拖动 = 平移    滚轮 = 缩放").pack(side="left")

        self.var_src = tk.StringVar(value=TILE_URLS[0][0])
        cb = ttk.Combobox(bar, textvariable=self.var_src, width=12, state="readonly",
                          values=[n for n, _, _, _ in TILE_URLS])
        cb.pack(side="right", padx=4)
        cb.bind("<<ComboboxSelected>>", self._on_src)
        ttk.Label(bar, text="底图:").pack(side="right")

        # 检索框：输入地名跳到该处，省得手动找
        srow = ttk.Frame(self)
        srow.pack(fill="x", padx=8, pady=(0, 4))
        self.search = SearchPanel(srow, self._on_search_pick,
                                  get_center=self._center_wgs,
                                  amap_key=self.amap_key, city=self.city, width=44)
        self.search.pack(fill="x")

        self.canvas = tk.Canvas(self, bg="#1a1c22", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True, padx=8)
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Double-Button-1>", self._on_double)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Configure>", lambda e: self.redraw())

        self.bind("<Escape>", lambda e: self._on_close())

        foot = ttk.Frame(self)
        foot.pack(fill="x", padx=8, pady=6)
        self.var_info = tk.StringVar()
        ttk.Label(foot, textvariable=self.var_info, font=("Consolas", 9)).pack(side="left")

        ttk.Button(foot, text="确定", command=self._confirm).pack(side="right", padx=4)
        ttk.Button(foot, text="取消", command=self.destroy).pack(side="right")
        ttk.Button(foot, text="回到起点", command=self._recenter).pack(side="right", padx=8)
        ttk.Button(foot, text="放大", command=lambda: self._zoom(1)).pack(side="right", padx=2)
        ttk.Button(foot, text="缩小", command=lambda: self._zoom(-1)).pack(side="right", padx=2)

    # ---------- 事件 ----------

    def _on_src(self, _e=None):
        names = [n for n, _, _, _ in TILE_URLS]
        idx = names.index(self.var_src.get())
        # 切换底图时坐标系可能变（高德 GCJ-02 <-> ArcGIS WGS-84）。
        # 先把中心转成 WGS-84，换完再转回新底图的坐标系，
        # 否则从 GCJ 切到 WGS 底图时视野会整体跳 500 米。
        wlat, wlon = self._to_wgs(self.lat, self.lon)
        self.cache.use_source(idx)
        self.lat, self.lon = self._to_view(wlat, wlon)
        self.redraw()

    def _on_press(self, e):
        self.dragging = True
        self.drag_from = (e.x, e.y)

    def _on_drag(self, e):
        if not self.dragging:
            return
        dx = e.x - self.drag_from[0]
        dy = e.y - self.drag_from[1]
        if abs(dx) < 3 and abs(dy) < 3:
            return
        # 把屏幕位移换算成经纬度位移
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        wx, wy = lonlat_to_world(self.lon, self.lat, self.zoom)
        nlon, nlat = world_to_lonlat(wx - dx, wy - dy, self.zoom)
        self.lon, self.lat = nlon, nlat
        self.drag_from = (e.x, e.y)
        self.redraw()

    def _on_release(self, e):
        was_drag = abs(e.x - self.drag_from[0]) > 3 or abs(e.y - self.drag_from[1]) > 3
        self.dragging = False
        if not was_drag:
            self._set_from_screen(e.x, e.y)
        self.redraw()

    def _on_double(self, e):
        self._set_from_screen(e.x, e.y)
        self._confirm()

    def _on_wheel(self, e):
        self._zoom(1 if e.delta > 0 else -1, at=(e.x, e.y))

    def _set_from_screen(self, sx, sy):
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        wx, wy = lonlat_to_world(self.lon, self.lat, self.zoom)
        nlon, nlat = world_to_lonlat(wx + (sx - w / 2), wy + (sy - h / 2), self.zoom)
        self.lon, self.lat = nlon, nlat
        self._picked = True

    def _zoom(self, d, at=None):
        nz = max(3, min(19, self.zoom + d))
        if nz == self.zoom:
            return
        self.zoom = nz
        self.redraw()

    def _recenter(self):
        self.zoom = 16
        self.redraw()

    def _confirm(self):
        if not self._picked:
            messagebox.showinfo(
                "还没有选点",
                "请先用下面任一方式选定位置：\n\n"
                "  • 在地图上单击\n"
                "  • 在上面检索地名，然后双击结果\n\n"
                "选好后按【确定】或双击地图。",
                parent=self)
            return
        self._alive = False
        wlat, wlon = self._to_wgs(self.lat, self.lon)   # 对外一律 WGS-84
        self.on_confirm(wlat, wlon)
        self.destroy()

    # ---------- 绘制 ----------

    def redraw(self):
        if not getattr(self, "_alive", True):
            return
        try:
            c = self.canvas
            w, h = c.winfo_width(), c.winfo_height()
        except tk.TclError:
            return
        if w < 10 or h < 10:
            return
        c.delete("all")

        wx, wy = lonlat_to_world(self.lon, self.lat, self.zoom)
        left, top = wx - w / 2, wy - h / 2
        n = 2 ** self.zoom

        tx0, ty0 = int(left // TILE), int(top // TILE)
        tx1, ty1 = int((left + w) // TILE), int((top + h) // TILE)

        self._tk_imgs = []
        missing = []
        for ty in range(ty0, ty1 + 1):
            for tx in range(tx0, tx1 + 1):
                if not (0 <= tx < n and 0 <= ty < n):
                    continue
                img = self.cache.get(self.zoom, tx, ty)
                px = tx * TILE - left
                py = ty * TILE - top
                if img is None:
                    missing.append((tx, ty))
                    c.create_rectangle(px, py, px + TILE, py + TILE,
                                       fill="#23262e", outline="")
                else:
                    tki = ImageTk.PhotoImage(img)
                    self._tk_imgs.append(tki)
                    c.create_image(px, py, image=tki, anchor="nw")

        # 准星
        cx, cy = w / 2, h / 2
        c.create_line(cx - 14, cy, cx - 4, cy, fill="#ff5a5a", width=2)
        c.create_line(cx + 4, cy, cx + 14, cy, fill="#ff5a5a", width=2)
        c.create_line(cx, cy - 14, cx, cy - 4, fill="#ff5a5a", width=2)
        c.create_line(cx, cy + 4, cx, cy + 14, fill="#ff5a5a", width=2)
        c.create_oval(cx - 5, cy - 5, cx + 5, cy + 5, outline="#ff5a5a", width=2)

        src = TILE_URLS[self.cache.src_idx][0]
        info = f"{self.lat:.6f}, {self.lon:.6f}   Zoom {self.zoom}   底图 {src}"
        if missing:
            info += f"   载入中 {len(missing)} 块…"
        errs = list(self.cache.errors.values())
        if errs:
            info += "   [瓦片下载失败]"
        self.var_info.set(info)

        # 还有瓦片在路上就过一会儿重画
        # 重排前**再查一次** _alive：只在 redraw 开头查是不够的 ——
        # 检查通过之后、after() 注册之前，窗口可能已被销毁，那样这个
        # 回调就成了孤儿，执行时会抛 invalid command name "...redraw"。
        if missing or self.cache.pending:
            self._schedule_redraw(400)


class RoutePicker(tk.Toplevel):
    """多点选路：在地图上依次点出起点、途经点、终点，连成一条路线。

    与 MapPicker 的区别：
      * 左键单击 = 追加一个点（不做平移）；拖动 = 平移（位移超过阈值才算）
      * 右侧列表显示已选点，可上下移动、删除
      * 支持把某个点一步存成命名地点（存完可直接用名字引用）
      * 确定后回调 [(lat, lon), ...]
    """

    def __init__(self, master, initial_lat: float, initial_lon: float,
                 on_confirm, save_alias_fn=None, aliases=None,
                 amap_key="", city="", initial_points=None):
        super().__init__(master)
        self.title("多点选路 — 单击加点，拖动平移，滚轮缩放")
        self.geometry("1180x760")
        self.transient(master)
        self.on_confirm = on_confirm
        self.save_alias_fn = save_alias_fn
        self.aliases = aliases or {}
        self.cache = TileCache()
        self.amap_key = amap_key
        self.city = city

        # lat/lon 存**底图坐标系**（高德=GCJ-02），不是 WGS-84。
        self.lat, self.lon = self._to_view(initial_lat, initial_lon)
        self.zoom = 16
        # initial_points 是**编辑模式**：把已有选点载进来接着改。
        # 它们都是 WGS-84（与 points 的约定一致），不用转换。
        self.points: list = [(float(a), float(b)) for a, b in (initial_points or [])]
        self.dragging = False
        self.drag_from = (0, 0)
        self.drag_moved = False
        # 见 MapPicker 里的说明：关窗后必须停止重绘，否则 after() 回调
        # 会在已销毁的控件上执行并抛 invalid command name。
        self._alive = True
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        # <Destroy> 在**两条路**上都会触发：用户点 X（走 _on_close），
        # 以及父窗口被销毁（不经过 _on_close）。所以清理必须挂在这里，
        # 否则父窗口一关，挂起的 after 回调就会变成孤儿并抛
        # invalid command name "...redraw"。
        self.bind("<Destroy>", self._on_destroy)

        self._after_id = None
        self._build()
        # 列表必须在 _build() **之后**刷新：_build 内部 self.lst 还没创建，
        # 那时调用会抛 AttributeError（被 Tk 的回调机制吞掉，表现为列表空白，
        # 而且双击定位、多选删除都会跟着失效）。
        self._refresh_list()

    # ---------- 坐标系 ----------
    # 底图是 GCJ-02（高德）时，屏幕像素算出来的就是 GCJ-02 坐标；
    # 而用户看到的、安卓系统给的、OSRM 路网用的都是 WGS-84。
    # 不转换会整体偏 500~600 米 —— 表现正是
    # 「电脑上标的路线和手机上地图跑的不一致」。
    # 约定：self.lat/self.lon 存**底图坐标系**，只在边界转换。

    def _to_view(self, lat: float, lon: float) -> tuple:
        """WGS-84 -> 底图坐标系（定位、画标记用）。"""
        return from_wgs(lon, lat, self.cache.coord_sys)

    def _to_wgs(self, lat: float, lon: float) -> tuple:
        """底图坐标系 -> WGS-84（输出给用户用）。"""
        return to_wgs(lon, lat, self.cache.coord_sys)

    def _center_wgs(self) -> tuple:
        """当前视野中心的 WGS-84 坐标（检索做「偏向附近」用）。"""
        return self._to_wgs(self.lat, self.lon)

    def _schedule_redraw(self, ms):
        """安排一次重绘。

        关键点：**先取消已有的挂起回调**。redraw 会被很多地方调用
        （Configure、点击、滚轮、检索…），如果每次都直接 after()，
        _after_id 会被覆盖，先前那个就再也取消不掉 —— 窗口销毁后它照样
        执行，抛 invalid command name "...redraw"。
        这里保证同一时刻最多只有一个挂起的重绘。
        """
        if not getattr(self, "_alive", True):
            return
        old = getattr(self, "_after_id", None)
        if old:
            try:
                self.after_cancel(old)
            except Exception:                                        # noqa: BLE001
                pass
            self._after_id = None
        try:
            self._after_id = self.after(ms, self.redraw)
        except tk.TclError:
            self._alive = False

    def _on_destroy(self, event=None):
        if event is not None and event.widget is not self:
            return
        self._alive = False
        aid = getattr(self, "_after_id", None)
        if aid:
            try:
                self.after_cancel(aid)
            except Exception:                                        # noqa: BLE001
                pass
            self._after_id = None

    def _on_close(self):
        """关窗时**必须取消**已排队的 after 回调。

        只在 redraw 开头查 _alive 是不够的 —— 那个回调已经排进 Tk 的事件队列，
        窗口销毁后它仍会执行并抛 invalid command name "...redraw"。
        """
        self._alive = False
        aid = getattr(self, "_after_id", None)
        if aid:
            try:
                self.after_cancel(aid)
            except Exception:                                        # noqa: BLE001
                pass
            self._after_id = None
        self.destroy()

    # ---------- 界面 ----------

    def _build(self):
        top = ttk.Frame(self)
        top.pack(fill="x", padx=8, pady=6)
        ttk.Label(top, text="单击 = 加点    拖动 = 平移    滚轮 = 缩放    "
                            "第 1 个点是起点，最后 1 个是终点，中间都是途经点").pack(side="left")

        body = ttk.Frame(self)
        body.pack(fill="both", expand=True, padx=8)

        # 左：地图
        left = ttk.Frame(body)
        left.pack(side="left", fill="both", expand=True)
        self.canvas = tk.Canvas(left, bg="#1a1c22", highlightthickness=0, cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Configure>", lambda e: self.redraw())

        # 右：检索 + 点列表
        right = ttk.Frame(body, width=330)
        right.pack(side="right", fill="y", padx=(8, 0))
        right.pack_propagate(False)

        # 检索框放最上面：先搜到大概位置，再在地图上微调
        ttk.Label(right, text="检索地点（回车搜索）", font=("", 10, "bold")).pack(
            anchor="w", pady=(0, 2))
        self.search = SearchPanel(right, self._on_search_pick,
                                  get_center=self._center_wgs,
                                  amap_key=self.amap_key, city=self.city, width=30)
        self.search.pack(fill="x")
        ttk.Separator(right).pack(fill="x", pady=6)

        ttk.Label(right, text="已选位置", font=("", 10, "bold")).pack(anchor="w", pady=(0, 4))
        self.lst = tk.Listbox(right, height=12, font=("Consolas", 9))
        self.lst.pack(fill="both", expand=True)
        self.lst.bind("<<ListboxSelect>>", lambda e: self.redraw())
        self.lst.bind("<Double-Button-1>", lambda e: self._goto_selected())

        btns = ttk.Frame(right)
        btns.pack(fill="x", pady=6)
        ttk.Button(btns, text="上移", width=6, command=lambda: self._move(-1)).pack(side="left", padx=1)
        ttk.Button(btns, text="下移", width=6, command=lambda: self._move(1)).pack(side="left", padx=1)
        ttk.Button(btns, text="删除", width=6, command=self._remove_selected).pack(side="left", padx=1)
        ttk.Button(btns, text="清空", width=6, command=self._clear).pack(side="left", padx=1)

        b2 = ttk.Frame(right)
        b2.pack(fill="x", pady=2)
        ttk.Button(b2, text="存为命名地点", command=self._save_alias).pack(side="left", padx=1)
        ttk.Button(b2, text="用命名地点加入", command=self._add_alias).pack(side="left", padx=1)

        self.var_alias = tk.StringVar()
        self.cb = ttk.Combobox(right, textvariable=self.var_alias,
                               values=list(self.aliases), width=28, state="readonly")
        self.cb.pack(fill="x", pady=2)

        ttk.Separator(right).pack(fill="x", pady=6)
        ttk.Button(right, text="回到起点视图", command=self._recenter).pack(fill="x", pady=2)
        ttk.Button(right, text="确定（生成路线）", command=self._confirm).pack(fill="x", pady=6)

        self.var_info = tk.StringVar()
        ttk.Label(self, textvariable=self.var_info, font=("Consolas", 9)).pack(
            anchor="w", padx=10, pady=(0, 6))

        self.bind("<Escape>", lambda e: self._on_close())

    # ---------- 事件 ----------

    def _on_press(self, e):
        self.dragging = True
        self.drag_moved = False
        self.drag_from = (e.x, e.y)

    def _on_drag(self, e):
        if not self.dragging:
            return
        dx = e.x - self.drag_from[0]
        dy = e.y - self.drag_from[1]
        if abs(dx) < 3 and abs(dy) < 3:
            return
        self.drag_moved = True
        wx, wy = lonlat_to_world(self.lon, self.lat, self.zoom)
        nlon, nlat = world_to_lonlat(wx - dx, wy - dy, self.zoom)
        self.lon, self.lat = nlon, nlat
        self.drag_from = (e.x, e.y)
        self.redraw()

    def _on_release(self, e):
        self.dragging = False
        if self.drag_moved:
            return
        if self.drag_from and (abs(e.x - self.drag_from[0]) > 3 or
                               abs(e.y - self.drag_from[1]) > 3):
            return
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        wx, wy = lonlat_to_world(self.lon, self.lat, self.zoom)
        nlon, nlat = world_to_lonlat(wx + (e.x - w / 2), wy + (e.y - h / 2), self.zoom)
        # ⚠️ 这里算出来的是**底图坐标系**（高德=GCJ-02），而 points 的约定是
        # 存 WGS-84（要交回给 gen / 手机）。**必须转换** —— 漏掉这一步，
        # 每个点都会偏 500~600 米（实测北京 555m）。
        self.points.append(self._to_wgs(nlat, nlon))
        self._refresh_list()
        self.redraw()

    def _on_wheel(self, e):
        nz = max(3, min(19, self.zoom + (1 if e.delta > 0 else -1)))
        if nz != self.zoom:
            self.zoom = nz
            self.redraw()

    def _on_search_pick(self, lat, lon, name, detail, preview=False):
        """检索结果被选中。

        单击列表项 = preview：只把地图移过去，方便先看一眼、
        再决定加到路线的哪个位置。
        双击 / 点「选这个」 = 正式：把该点追加进路线。
        """
        self.lat, self.lon = self._to_view(lat, lon)   # 检索给的是 WGS-84
        if preview:
            self.redraw()
            self.var_info.set(f"预览：{name}  {lat:.6f},{lon:.6f}"
                              "（双击列表项可加入路线）")
            return
        self.points.append((lat, lon))
        self._refresh_list()
        self.redraw()

    # ---------- 列表操作 ----------

    def _refresh_list(self):
        self.lst.delete(0, "end")
        for i, (lat, lon) in enumerate(self.points):
            if i == 0:
                tag = "起点"
            elif i == len(self.points) - 1:
                tag = "终点"
            else:
                tag = f"途经{i}"
            self.lst.insert("end", f"{tag:6} {lat:.6f},{lon:.6f}")

    def _goto_selected(self):
        """双击列表项：把地图移到该点，方便就近微调。"""
        i = self._sel()
        if i is None or i >= len(self.points):
            return
        self.lat, self.lon = self._to_view(*self.points[i])
        self.redraw()

    def _remove_selected(self):
        """删除所有选中项（支持多选）。"""
        sel = list(self.lst.curselection())
        if not sel:
            return
        for i in sorted(sel, reverse=True):
            if 0 <= i < len(self.points):
                del self.points[i]
        self._refresh_list()
        self.redraw()

    def _sel(self):
        s = self.lst.curselection()
        return s[0] if s else None

    def _move(self, d):
        i = self._sel()
        if i is None:
            return
        j = i + d
        if 0 <= j < len(self.points):
            self.points[i], self.points[j] = self.points[j], self.points[i]
            self._refresh_list()
            self.lst.selection_clear(0, "end")
            self.lst.selection_set(j)
            self.redraw()

    def _delete(self):
        i = self._sel()
        if i is None:
            return
        del self.points[i]
        self._refresh_list()
        self.redraw()

    def _clear(self):
        self.points.clear()
        self._refresh_list()
        self.redraw()

    def _save_alias(self):
        i = self._sel()
        if i is None or not self.save_alias_fn:
            return
        lat, lon = self.points[i]
        name = simpledialog.askstring("存为命名地点", "名字：", parent=self)
        if not name or not name.strip():
            return
        try:
            self.save_alias_fn(name.strip(), lat, lon)
        except Exception as e:                                       # noqa: BLE001
            messagebox.showerror("失败", f"保存失败：{e}", parent=self)
            return
        names = list(self.aliases)
        if name.strip() not in names:
            names.append(name.strip())
        self.cb["values"] = names
        messagebox.showinfo("已保存", f"{name.strip()} = {lat:.6f},{lon:.6f}", parent=self)

    def _add_alias(self):
        n = self.var_alias.get()
        if not n or n not in self.aliases:
            return
        e = self.aliases[n]
        self.points.append((float(e["lat"]), float(e["lon"])))
        self._refresh_list()
        self.redraw()

    def _recenter(self):
        if self.points:
            self.lat, self.lon = self._to_view(*self.points[0])   # points 存 WGS-84
        self.zoom = 16
        self.redraw()

    def _confirm(self):
        if len(self.points) < 2:
            messagebox.showinfo("提示", "至少要有起点和终点两个点。", parent=self)
            return
        self._alive = False
        self.on_confirm(list(self.points))
        self.destroy()

    # ---------- 绘制 ----------

    def redraw(self):
        if not getattr(self, "_alive", True):
            return
        try:
            c = self.canvas
            w, h = c.winfo_width(), c.winfo_height()
        except tk.TclError:
            return
        if w < 10 or h < 10:
            return
        c.delete("all")
        wx, wy = lonlat_to_world(self.lon, self.lat, self.zoom)
        left, top = wx - w / 2, wy - h / 2
        n = 2 ** self.zoom

        tx0, ty0 = int(left // TILE), int(top // TILE)
        tx1, ty1 = int((left + w) // TILE), int((top + h) // TILE)
        self._tk_imgs = []
        missing = []
        for ty in range(ty0, ty1 + 1):
            for tx in range(tx0, tx1 + 1):
                if not (0 <= tx < n and 0 <= ty < n):
                    continue
                img = self.cache.get(self.zoom, tx, ty)
                px, py = tx * TILE - left, ty * TILE - top
                if img is None:
                    missing.append((tx, ty))
                    c.create_rectangle(px, py, px + TILE, py + TILE, fill="#23262e", outline="")
                else:
                    tki = ImageTk.PhotoImage(img)
                    self._tk_imgs.append(tki)
                    c.create_image(px, py, image=tki, anchor="nw")

        # 画已选点与连线
        sel = self._sel()
        scr = []
        for lat, lon in self.points:
            # points 存 WGS-84，画到高德（GCJ-02）底图上必须先转换，
            # 否则标记会比底图偏 500 米
            vlat, vlon = self._to_view(lat, lon)
            px, py = lonlat_to_world(vlon, vlat, self.zoom)
            scr.append((px - left, py - top))
        if len(scr) >= 2:
            c.create_line(*[v for pt in scr for v in pt], fill="#4da3ff",
                          width=3, dash=(6, 4))
        for i, (x, y) in enumerate(scr):
            colour = "#43d17a" if i == 0 else ("#e6483e" if i == len(scr) - 1 else "#f5b041")
            r = 11 if i == sel else 8
            c.create_oval(x - r, y - r, x + r, y + r, fill=colour,
                          outline="#ffffff" if i == sel else "#1a1c22", width=3 if i == sel else 2)
            c.create_text(x, y, text=str(i + 1), fill="#10131a",
                          font=("", 9, "bold"))

        info = f"{self.lat:.6f}, {self.lon:.6f}   Zoom {self.zoom}   已选 {len(self.points)} 点"
        if len(self.points) >= 2:
            d = 0.0
            for i in range(1, len(self.points)):
                d += _haversine(self.points[i - 1][0], self.points[i - 1][1],
                                self.points[i][0], self.points[i][1])
            info += f"   直线合计 {d/1000:.2f} km"
        if missing:
            info += f"   载入中 {len(missing)} 块…"
        self.var_info.set(info)
        # 重排前**再查一次** _alive：只在 redraw 开头查是不够的 ——
        # 检查通过之后、after() 注册之前，窗口可能已被销毁，那样这个
        # 回调就成了孤儿，执行时会抛 invalid command name "...redraw"。
        if missing or self.cache.pending:
            self._schedule_redraw(400)


def _haversine(a1, o1, a2, o2):
    R = 6371008.8
    p1, p2 = math.radians(a1), math.radians(a2)
    dp, dl = p2 - p1, math.radians(o2 - o1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(h)))


def pick_location(master, initial=(39.9087, 116.3975), on_confirm=None):
    """便捷入口。on_confirm 收不到就返回一个可查询的容器。"""
    holder = {}

    def _cb(lat, lon):
        holder["lat"], holder["lon"] = lat, lon
        if on_confirm:
            on_confirm(lat, lon)

    MapPicker(master, initial[0], initial[1], _cb)
    return holder
