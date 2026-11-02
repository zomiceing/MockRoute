#!/usr/bin/env python3
"""MockRoute 图形控制台 —— 用按钮操作全部功能，不用记命令行。

启动：
    python tools\\gui.py

设计要点
--------
* 只依赖标准库（tkinter 是 Python 自带的），不需要 pip install 任何东西。
* 命令一律在**后台线程**里跑，界面不会卡死；长任务可以随时中止。
* adb 需要三个可写目录（用户目录/temp/密钥），这台机器的默认位置不可写，
  否则 adb 会直接崩。所以这里在启动时就把环境变量配好 —— 这也是
  scripts/mroute.ps1 做的事，GUI 里复刻了一遍。
* 所有命令都会打印到日志区，方便你对照学习命令行用法。
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mockroute import geo                      # noqa: E402
from mockroute import locations as loc          # noqa: E402
from mockroute import mappicker                  # noqa: E402

# --------------------------------------------------------------------------
# 路径与常量
# --------------------------------------------------------------------------

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent
CLI = TOOLS / "mroute.py"
PLOT = TOOLS / "plot-route.py"
TC = ROOT / ".toolchain"
SCRIPTS = ROOT / "scripts"
OUT = ROOT / "out"

PYTHON = sys.executable or "python"

PRESETS = {
    "custom（自定义）": "custom",
    "walk 步行": "walk",
    "run 跑步": "run",
    "interval 间歇训练": "interval",
}
# 圆滑策略（三选一）。说明文字里直接写清代价，避免选了却不知道意味着什么。
# 实测「真路径切角」：auto 0.75m / round 6m 1.76m / none 0.000m。
CORNER_MODES = {
    "auto（自动：按离路上限的预算挑圆滑半径）": "auto",
    "round（手动指定圆滑半径）": "round",
    "none（不圆滑：真路径 100% 贴中心线，拐弯方向是尖角）": "none",
}
PROFILES = {
    "foot（步行/跑步路网）": "foot",
    "driving（驾车路网）": "driving",
    "bicycling / bike（骑行）": "bicycling",
}
PROVIDERS = {
    "osrm（免费，无需 key）": "osrm",
    "amap 高德（需 key）": "amap",
    "google（需 key）": "google",
    "file 本地文件（不联网）": "file",
}
MODES = {"once 跑一次": "once", "loop 循环": "loop", "pingpong 往返": "pingpong"}


class Runner:
    """在后台线程里跑命令，把输出塞进队列供界面消费。

    为什么用「重定向到文件 + 轮询」而不是 subprocess 的 PIPE
    ------------------------------------------------------
    在某些受限环境里（例如本工具就是在 DSH 的沙箱里被调用，或者被别的
    自动化脚本包着跑），父进程再往下开的孙进程如果通过管道捕获输出，会直接
    被拒绝或永久阻塞 —— 表现为「命令明明启动了，日志区却一行都没有」。

    改成 stdout/stderr 重定向到临时文件、再轮询该文件增量，就完全不依赖管道，
    在任何环境里都能工作，而且顺带得到了完整的执行历史（.toolchain/gui-logs/）。
    """

    def __init__(self, ui_queue: queue.Queue):
        self.q = ui_queue
        self.proc: subprocess.Popen | None = None
        self.thread: threading.Thread | None = None
        self._logfile: Path | None = None

    @property
    def busy(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def base_env(self) -> dict:
        env = dict(os.environ)
        adb_home = TC / "android-home"
        tmp = TC / "tmp"
        adb_home.mkdir(parents=True, exist_ok=True)
        tmp.mkdir(parents=True, exist_ok=True)
        # adb 的用户目录 / 密钥 / temp 必须可写，否则 daemon 起不来
        env["ANDROID_USER_HOME"] = str(adb_home)
        env["ANDROID_SDK_HOME"] = str(adb_home)
        env["ADB_VENDOR_KEYS"] = str(adb_home)
        env["ANDROID_HOME"] = str(TC / "android-sdk")
        env["ANDROID_SDK_ROOT"] = str(TC / "android-sdk")
        env["TMP"] = str(tmp)
        env["TEMP"] = str(tmp)
        jdk = TC / "jdk"
        if (jdk / "bin/java.exe").exists():
            env["JAVA_HOME"] = str(jdk)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        return env

    def start(self, args: list, title: str, cwd: Path | None = None) -> bool:
        if self.busy:
            self.q.put(("log", "\n[!] 上一个任务还在运行，请先等它结束或点【中止】\n"))
            return False

        def work():
            logs_dir = TC / "gui-logs"
            logs_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%m%d-%H%M%S")
            safe = "".join(c if c.isalnum() else "_" for c in title)[:24]
            self._logfile = logs_dir / f"{stamp}-{safe}.log"

            self.q.put(("log", f"\n{'='*70}\n$ {' '.join(str(a) for a in args)}\n"
                               f"（完整输出: {self._logfile}）\n{'='*70}"))
            try:
                with open(self._logfile, "w", encoding="utf-8", errors="replace") as fh:
                    self.proc = subprocess.Popen(
                        [str(a) for a in args],
                        stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                        cwd=str(cwd or ROOT), env=self.base_env(), close_fds=True,
                    )
                    # 轮询文件增量，而不是读管道 —— 见类文档里的说明
                    pos = 0
                    while True:
                        if self._logfile.exists():
                            with open(self._logfile, "r", encoding="utf-8",
                                      errors="replace") as rf:
                                rf.seek(pos)
                                chunk = rf.read()
                                pos = rf.tell()
                            if chunk:
                                for line in chunk.splitlines():
                                    self.q.put(("log", line))
                        rc = self.proc.poll()
                        if rc is not None:
                            # 收尾：把最后残留的输出读干净
                            with open(self._logfile, "r", encoding="utf-8",
                                      errors="replace") as rf:
                                rf.seek(pos)
                                for line in rf.read().splitlines():
                                    self.q.put(("log", line))
                            if rc == 0:
                                self.q.put(("ok", f"[完成] {title}"))
                            else:
                                self.q.put(("err", f"[失败] {title}（退出码 {rc}）"))
                            break
                        time.sleep(0.12)
            except FileNotFoundError as e:
                self.q.put(("err", f"[失败] 找不到命令: {e}"))
            except Exception as e:                                   # noqa: BLE001
                self.q.put(("err", f"[失败] {type(e).__name__}: {e}"))
            finally:
                self.proc = None
                self.q.put(("done", title))

        self.thread = threading.Thread(target=work, daemon=True)
        self.thread.start()
        return True

    def kill(self):
        """中止当前任务。注意：Windows 上杀父进程不一定杀掉 adb 等孙进程。"""
        p = self.proc
        if p and p.poll() is None:
            try:
                p.terminate()
            except Exception:                                       # noqa: BLE001
                pass
            self.q.put(("warn", "[中止] 已请求终止当前任务"))
        else:
            self.q.put(("warn", "[中止] 没有正在运行的任务"))

    def mroute(self, *args, title: str) -> bool:
        return self.start([PYTHON, str(CLI), *args], title)


# --------------------------------------------------------------------------
# 主界面
# --------------------------------------------------------------------------

class Console:
    def __init__(self, root_win: tk.Tk):
        self.win = root_win
        self.win.title("MockRoute 控制台 — Android 行进模拟")
        self.win.geometry("1080x760")
        self.win.minsize(900, 620)

        self.q: queue.Queue = queue.Queue()
        # 本次会话生成过的轨迹名。只允许覆盖自己刚生成的，
        # 其它同名文件一律拒绝，避免「永远是上一次的」那种混淆。
        self._session_names: set = set()
        self._lap_m = 0.0      # 一圈路程估算（绕圈对话框用）
        self._picked_points: list = []   # 地图上真实选的点（估算一圈路程用）
        self.runner = Runner(self.q)
        self._build()
        self.win.after(100, self._drain)
        self._startup_check()

    # ---------------- 界面搭建 ----------------

    def _build(self):
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass

        nb = ttk.Notebook(self.win)
        nb.pack(fill="both", expand=True, padx=8, pady=(8, 4))

        self.tab_home = ttk.Frame(nb)
        self.tab_route = ttk.Frame(nb)
        self.tab_gen = ttk.Frame(nb)
        self.tab_files = ttk.Frame(nb)
        self.tab_ctrl = ttk.Frame(nb)
        nb.add(self.tab_home, text="  快速开始  ")
        nb.add(self.tab_route, text="  多点选路  ")
        nb.add(self.tab_gen, text="  生成轨迹  ")
        nb.add(self.tab_files, text="  轨迹文件  ")
        nb.add(self.tab_ctrl, text="  设备控制  ")

        self._build_home()
        self._build_route()
        self._build_gen()
        self._build_files()
        self._build_ctrl()

        # 底部：状态条 + 日志 + 按钮
        bottom = ttk.Frame(self.win)
        bottom.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        bar = ttk.Frame(bottom)
        bar.pack(fill="x")
        self.var_status = tk.StringVar(value="就绪")
        ttk.Label(bar, textvariable=self.var_status, anchor="w").pack(side="left", fill="x", expand=True)
        self.btn_kill = ttk.Button(bar, text="中止当前任务", command=self.runner.kill)
        self.btn_kill.pack(side="right")
        ttk.Button(bar, text="清空日志", command=self._clear_log).pack(side="right", padx=4)

        box = ttk.Frame(bottom)
        box.pack(fill="both", expand=True, pady=(4, 0))
        self.log = tk.Text(box, wrap="none", height=16, bg="#12141a", fg="#c8cdd8",
                           insertbackground="#c8cdd8", font=("Consolas", 9))
        vs = ttk.Scrollbar(box, orient="vertical", command=self.log.yview)
        hs = ttk.Scrollbar(box, orient="horizontal", command=self.log.xview)
        self.log.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)
        self.log.grid(row=0, column=0, sticky="nsew")
        vs.grid(row=0, column=1, sticky="ns")
        hs.grid(row=1, column=0, sticky="ew")
        box.rowconfigure(0, weight=1)
        box.columnconfigure(0, weight=1)
        self.log.tag_configure("ok", foreground="#6ee7a0")
        self.log.tag_configure("err", foreground="#f87171")
        self.log.tag_configure("warn", foreground="#fbbf24")

    def _build_home(self):
        f = self.tab_home
        ttk.Label(f, text="一次点完：体检 → 生成 → 回放", font=("", 11, "bold")).pack(anchor="w", padx=12, pady=(12, 2))
        ttk.Label(f, text="下面每个按钮都可以单独点；也可以只点「一键跑步」走完整流程。",
                  foreground="#666").pack(anchor="w", padx=12, pady=(0, 10))

        grid = ttk.Frame(f)
        grid.pack(fill="x", padx=12)

        def big(row, col, text, cmd, tip=""):
            b = ttk.Button(grid, text=text, command=cmd, width=28)
            b.grid(row=row, column=col, padx=6, pady=6, sticky="ew")
            if tip:
                ttk.Label(grid, text=tip, foreground="#777", font=("", 8)).grid(
                    row=row + 1, column=col, padx=6, sticky="w")
            return b

        big(0, 0, "① 环境体检（doctor）", self.act_doctor,
            "检查设备/应用/权限，缺什么会告诉你")
        big(0, 1, "② 生成跑步轨迹（5km）", self.act_gen_run,
            "天安门 → 天坛，真实路网")
        big(2, 0, "③ 回放（20 倍速）", self.act_play_20x,
            "推到手机开始跑，20 倍速便于快速验证")
        big(2, 1, "③ 回放（正常速度）", self.act_play_1x,
            "按真实配速跑，5km 约 23 分钟")
        big(4, 0, "停止模拟（恢复真实定位）", lambda: self._mroute("stop", title="停止"),
            "移除虚拟定位，手机回到真实位置")
        big(4, 1, "打开最新图表", self.act_open_latest_png,
            "用系统默认看图程序打开 out/ 下最新的 png")

        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)

        ttk.Separator(f).pack(fill="x", padx=12, pady=14)
        ttk.Label(f, text="其它常用动作", font=("", 10, "bold")).pack(anchor="w", padx=12)
        row2 = ttk.Frame(f)
        row2.pack(fill="x", padx=12, pady=6)

        ttk.Label(row2, text="固定坐标 (纬度,经度):").pack(side="left")
        self.var_point = tk.StringVar(value="39.9087,116.3975")
        ttk.Entry(row2, textvariable=self.var_point, width=24).pack(side="left", padx=6)
        ttk.Button(row2, text="设为该点", command=self.act_point).pack(side="left", padx=4)

        row3 = ttk.Frame(f)
        row3.pack(fill="x", padx=12, pady=6)
        ttk.Label(row3, text="原地绕圈半径(米):").pack(side="left")
        self.var_radius = tk.StringVar(value="30")
        ttk.Entry(row3, textvariable=self.var_radius, width=8).pack(side="left", padx=6)
        ttk.Label(row3, text="一圈秒数:").pack(side="left")
        self.var_period = tk.StringVar(value="150")
        ttk.Entry(row3, textvariable=self.var_period, width=8).pack(side="left", padx=6)
        ttk.Button(row3, text="开始绕圈", command=self.act_circle).pack(side="left", padx=4)

    def _make_scrollable(self, parent):
        """把 parent 变成可滚动容器，返回真正放内容的 Frame。

        为什么需要：多点选路页的内容总高约 765 px，而窗口可视区只有 ~720 px，
        最底下的「保存的路径」被挤出可视区 —— 用户会以为那个功能没写。
        加了滚动条后所有内容都能看到，窗口再小也不丢控件。
        """
        outer = ttk.Frame(parent)
        outer.pack(fill="both", expand=True)

        canvas = tk.Canvas(outer, highlightthickness=0, bg="#f0f0f0")
        vsb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        inner = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")

        def on_inner(_e=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def on_canvas(e):
            # 让内层宽度跟随画布，否则内容会缩成一条
            canvas.itemconfigure(window, width=e.width)

        inner.bind("<Configure>", on_inner)
        canvas.bind("<Configure>", on_canvas)

        # 滚轮滚动（只在鼠标位于本页时生效，避免抢别的页面的滚轮）
        def on_wheel(e):
            canvas.yview_scroll(-1 if e.delta > 0 else 1, "units")

        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", on_wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
        return inner

    def _build_route(self):
        """多点选路：在地图上依次点出起点/途经点/终点，直接生成轨迹。"""
        f = self._make_scrollable(self.tab_route)
        ttk.Label(f, text="多点选路 —— 在地图上点出一条路线", font=("", 11, "bold")).pack(
            anchor="w", padx=12, pady=(12, 2))
        ttk.Label(f, text="地图里单击加点、拖动平移、滚轮缩放；右侧列表可改顺序、"
                          "单独删点、把某个点存成命名地点。确定后下面会填好起终点与途经点。",
                  foreground="#666").pack(anchor="w", padx=12, pady=(0, 10))

        top = ttk.Frame(f)
        top.pack(fill="x", padx=12)

        ttk.Button(top, text="打开地图选路", command=self.act_route_pick,
                   width=18).pack(side="left", padx=(0, 8))
        ttk.Label(top, text="已选：").pack(side="left")
        self.var_route_count = tk.StringVar(value="0 个点")
        ttk.Label(top, textvariable=self.var_route_count,
                  font=("Consolas", 9)).pack(side="left")

        # 选点结果：起点 / 途经点 / 终点
        box = ttk.LabelFrame(f, text=" 选出的路线 ")
        box.pack(fill="x", padx=12, pady=10)

        r1 = ttk.Frame(box)
        r1.pack(fill="x", padx=10, pady=4)
        ttk.Label(r1, text="起点", width=6, anchor="e").pack(side="left")
        self.var_r_from = tk.StringVar()
        ttk.Entry(r1, textvariable=self.var_r_from, width=30).pack(side="left", padx=6)
        ttk.Button(r1, text="地图选", width=8,
                   command=lambda: self._single_pick(self.var_r_from)).pack(side="left")
        ttk.Button(r1, text="存为命名地点", width=13,
                   command=lambda: self._save_alias_var(self.var_r_from)).pack(side="left", padx=6)

        r2 = ttk.Frame(box)
        r2.pack(fill="x", padx=10, pady=4)
        ttk.Label(r2, text="途经点", width=6, anchor="e").pack(side="left")
        self.var_r_via = tk.StringVar()
        ttk.Entry(r2, textvariable=self.var_r_via, width=30).pack(side="left", padx=6)
        ttk.Label(r2, text="分号分隔，可直接手改", foreground="#777",
                  font=("", 8)).pack(side="left")

        r3 = ttk.Frame(box)
        r3.pack(fill="x", padx=10, pady=4)
        ttk.Label(r3, text="终点", width=6, anchor="e").pack(side="left")
        self.var_r_to = tk.StringVar()
        ttk.Entry(r3, textvariable=self.var_r_to, width=30).pack(side="left", padx=6)
        ttk.Button(r3, text="地图选", width=8,
                   command=lambda: self._single_pick(self.var_r_to)).pack(side="left")
        ttk.Button(r3, text="存为命名地点", width=13,
                   command=lambda: self._save_alias_var(self.var_r_to)).pack(side="left", padx=6)

        # 查看 / 更改选点：先看清楚选了什么，确认无误再动手改
        view = ttk.Frame(box)
        view.pack(fill="x", padx=10, pady=(6, 4))
        ttk.Label(view, text="选点清单", width=6, anchor="e").pack(side="left")
        self.var_route_view = tk.StringVar(value="（还没选点）")
        ttk.Label(view, textvariable=self.var_route_view, font=("Consolas", 9),
                  foreground="#9cf", anchor="w").pack(side="left", padx=6, fill="x",
                                                     expand=True)
        ttk.Button(view, text="查看选点", width=10,
                   command=self.act_route_view).pack(side="left", padx=2)
        # 更改必须点这个按钮才生效 —— 避免误改已经调好的选点
        ttk.Button(view, text="更改选点", width=10,
                   command=self.act_route_edit).pack(side="left", padx=2)

        # 生成参数（复用预设 + 少量可调）
        box2 = ttk.LabelFrame(f, text=" 生成参数 ")
        box2.pack(fill="x", padx=12, pady=6)
        r4 = ttk.Frame(box2)
        r4.pack(fill="x", padx=10, pady=(6, 2))
        ttk.Label(r4, text="预设").pack(side="left")
        self.var_r_preset = tk.StringVar(value="walk 步行")
        cb_preset = ttk.Combobox(r4, textvariable=self.var_r_preset,
                                 values=list(PRESETS), width=16, state="readonly")
        cb_preset.pack(side="left", padx=6)
        cb_preset.bind("<<ComboboxSelected>>", lambda e: self._on_r_preset())
        ttk.Label(r4, text="路网").pack(side="left", padx=(10, 0))
        self.var_r_profile = tk.StringVar(value="foot（步行/跑步路网）")
        ttk.Combobox(r4, textvariable=self.var_r_profile, values=list(PROFILES),
                     width=20, state="readonly").pack(side="left", padx=6)

        # 运动参数
        r5 = ttk.Frame(box2)
        r5.pack(fill="x", padx=10, pady=(2, 2))
        ttk.Label(r5, text="速度 m/s").pack(side="left")
        self.var_r_speed = tk.StringVar()
        ttk.Entry(r5, textvariable=self.var_r_speed, width=8).pack(side="left", padx=4)
        self.var_r_speed_hint = tk.StringVar()
        ttk.Label(r5, textvariable=self.var_r_speed_hint, foreground="#2b7",
                  font=("Consolas", 9), width=20).pack(side="left")
        ttk.Label(r5, text="采样 ms").pack(side="left")
        self.var_r_interval = tk.StringVar()
        ttk.Entry(r5, textvariable=self.var_r_interval, width=7).pack(side="left", padx=4)
        ttk.Label(r5, text="GPS 噪声σ m").pack(side="left")
        self.var_r_noise = tk.StringVar()
        ttk.Entry(r5, textvariable=self.var_r_noise, width=6).pack(side="left", padx=4)

        # 离路约束（只管「点离路多远」）
        r6 = ttk.Frame(box2)
        r6.pack(fill="x", padx=10, pady=2)
        ttk.Label(r6, text="离路上限 m").pack(side="left")
        self.var_r_maxoff = tk.StringVar(value="3")
        ttk.Entry(r6, textvariable=self.var_r_maxoff, width=5).pack(side="left", padx=4)
        ttk.Label(r6, text="(上报坐标到道路中心线的最大距离，0=不限)",
                  foreground="#777", font=("", 8)).pack(side="left")

        # 拐弯约束（只管「方向怎么转」）—— 刻意与上面分开，两回事
        #
        # 第一行选**圆滑策略**（三选一）。这不是可有可无的开关：
        # 实测「真路径切角」—— auto 0.75m / round 6m 1.76m / none 0.000m ——
        # 而切角这部分偏差**离路上限修不了**（真路径已经偏出去了）。
        # 所以想要「绝对不切角」就必须选 none，不能靠把上限压小。
        rm = ttk.Frame(box2)
        rm.pack(fill="x", padx=10, pady=(2, 2))
        ttk.Label(rm, text="圆滑策略").pack(side="left")
        self.var_r_cornermode = tk.StringVar(value="auto")
        cb_mode = ttk.Combobox(rm, textvariable=self.var_r_cornermode,
                               values=list(CORNER_MODES), width=34,
                               state="readonly")
        cb_mode.pack(side="left", padx=6)
        cb_mode.bind("<<ComboboxSelected>>", lambda e: self._on_corner_mode())
        self.var_r_cornermode_hint = tk.StringVar()
        ttk.Label(rm, textvariable=self.var_r_cornermode_hint,
                  foreground="#2b7").pack(side="left", padx=6)

        r7 = ttk.Frame(box2)
        r7.pack(fill="x", padx=10, pady=(2, 6))
        self.lbl_r_corner = ttk.Label(r7, text="拐角圆滑 m")
        self.lbl_r_corner.pack(side="left")
        self.var_r_corner = tk.StringVar(value="6")
        self.ent_r_corner = ttk.Entry(r7, textvariable=self.var_r_corner, width=5)
        self.ent_r_corner.pack(side="left", padx=4)
        ttk.Label(r7, text="单步转角°").pack(side="left")
        self.var_r_stepturn = tk.StringVar(value="25")
        ttk.Entry(r7, textvariable=self.var_r_stepturn, width=5).pack(side="left", padx=4)
        ttk.Label(r7, text="弯道采样点").pack(side="left")
        self.var_r_minsamp = tk.StringVar(value="1")
        ttk.Entry(r7, textvariable=self.var_r_minsamp, width=4).pack(side="left", padx=4)
        ttk.Label(r7, text="过弯 a m/s²").pack(side="left")
        self.var_r_alat = tk.StringVar(value="1.5")
        ttk.Entry(r7, textvariable=self.var_r_alat, width=4).pack(side="left", padx=4)
        ttk.Label(r7, text="(按弯的形状限速)", foreground="#777",
                  font=("", 8)).pack(side="left")
        ttk.Button(r7, text="按预设填回", width=11,
                   command=self._on_r_preset).pack(side="left", padx=8)

        ttk.Label(box2, text="离路上限：只管「点离路多远」。GPS 噪声会把上报坐标推离路面"
                             "（不限制时实测最远 7.7m，落到建筑里），超过上限就拉回来。\n"
                             "拐角圆滑 / 单步转角 / 弯道采样点：只管「方向怎么转」。"
                             "OSRM 几何在路口是尖角，圆滑摊成圆弧；单步转角限制一步内的"
                             "净偏转；弯道采样点保证弯里有足够点，方向不会突变成一次跳变。",
                  foreground="#777", font=("", 8), justify="left").pack(
            anchor="w", padx=12, pady=(0, 6))

        quick = ttk.Frame(box2)
        quick.pack(fill="x", padx=10, pady=(0, 6))
        ttk.Label(quick, text="常用速度:").pack(side="left")
        for label, v in (("步行 1.35", "1.35"), ("快走 1.8", "1.8"),
                         ("慢跑 2.5", "2.5"), ("跑步 3.3", "3.3"),
                         ("快跑 4.0", "4.0")):
            ttk.Button(quick, text=label, width=9,
                       command=lambda x=v: self._set_r_speed(x)).pack(side="left", padx=2)

        self.var_r_open = tk.BooleanVar(value=True)
        ttk.Checkbutton(box2, text="生成后画图并打开", variable=self.var_r_open).pack(
            anchor="w", padx=12, pady=(0, 8))

        # 生成
        act = ttk.Frame(f)
        act.pack(fill="x", padx=12, pady=(8, 2))
        ttk.Button(act, text="用这条路线生成轨迹", command=self.act_route_gen,
                   width=24).pack(side="left", padx=(0, 8))
        self.var_r_autoplay = tk.BooleanVar(value=False)
        ttk.Checkbutton(act, text="生成后自动回放", variable=self.var_r_autoplay).pack(side="left", padx=6)
        ttk.Button(act, text="预览命令", command=self.act_route_preview).pack(side="left", padx=4)
        ttk.Button(act, text="清空", command=self.act_route_clear).pack(side="left", padx=4)

        cur = ttk.Frame(f)
        cur.pack(fill="x", padx=12, pady=(4, 0))
        ttk.Label(cur, text="当前轨迹:").pack(side="left")
        self.var_r_showname = tk.StringVar(value="（还没生成）")
        ttk.Label(cur, textvariable=self.var_r_showname, font=("Consolas", 9),
                  foreground="#2b7").pack(side="left", padx=6)
        ttk.Label(cur, text="每次生成都会要求你命名，重名会被拒绝 —— "
                            "这样每条轨迹都是独立文件，不会搞混。",
                  foreground="#777", font=("", 8)).pack(side="left", padx=6)

        # 回放 —— 之前这一页根本没有回放入口，选完点「没办法跑」
        box3 = ttk.LabelFrame(f, text=" 回放（在手机上跑刚生成的这条） ")
        box3.pack(fill="x", padx=12, pady=6)
        r5 = ttk.Frame(box3)
        r5.pack(fill="x", padx=10, pady=8)
        ttk.Button(r5, text="生成并回放", command=self.act_route_play,
                   width=14).pack(side="left", padx=(0, 6))
        ttk.Label(r5, text="倍速").pack(side="left", padx=(10, 2))
        self.var_r_mult = tk.StringVar(value="1")
        ttk.Entry(r5, textvariable=self.var_r_mult, width=5).pack(side="left")
        ttk.Button(r5, text="跑这条（×20）", command=lambda: self.act_route_play(20),
                   width=13).pack(side="left", padx=6)
        ttk.Button(r5, text="状态", width=7,
                   command=lambda: self._mroute("status", title="查询状态")).pack(side="left", padx=(12, 2))
        ttk.Button(r5, text="暂停", width=6,
                   command=lambda: self._mroute("pause", title="暂停")).pack(side="left", padx=2)
        ttk.Button(r5, text="停止", width=6,
                   command=lambda: self._mroute("stop", title="停止")).pack(side="left", padx=2)

        self.var_r_name = tk.StringVar()          # 当前轨迹名
        self.var_r_file = tk.StringVar(value="out/gui-route.json")
        r6 = ttk.Frame(box3)
        r6.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Label(r6, text="轨迹文件").pack(side="left")
        ttk.Entry(r6, textvariable=self.var_r_file, width=34).pack(side="left", padx=6)
        ttk.Button(r6, text="浏览…", command=self.act_route_pick_file).pack(side="left", padx=2)
        ttk.Label(r6, text="（生成后会自动填成刚生成的文件）",
                  foreground="#777", font=("", 8)).pack(side="left", padx=6)

        save = ttk.LabelFrame(f, text=" 保存的路径（可反复使用） ")
        save.pack(fill="x", padx=12, pady=6)
        r7 = ttk.Frame(save)
        r7.pack(fill="x", padx=10, pady=8)
        ttk.Label(r7, text="已存路径:").pack(side="left")
        self.var_r_path = tk.StringVar()
        self.cb_path = ttk.Combobox(r7, textvariable=self.var_r_path, width=20,
                                    state="readonly")
        self.cb_path.pack(side="left", padx=4)
        ttk.Button(r7, text="载入", width=6,
                   command=self.act_path_load).pack(side="left", padx=2)
        ttk.Button(r7, text="保存当前选点", width=13,
                   command=self.act_path_save).pack(side="left", padx=(10, 2))
        ttk.Button(r7, text="删除", width=6,
                   command=self.act_path_delete).pack(side="left", padx=2)
        ttk.Button(r7, text="刷新", width=6,
                   command=self.refresh_paths).pack(side="left", padx=2)
        ttk.Label(save, text="保存的是「起点+途经点+终点」这组选点（WGS-84 坐标），"
                             "跟命名地点存在同一个文件 locations.json 里。",
                  foreground="#777", font=("", 8)).pack(anchor="w", padx=12, pady=(0, 8))

        # 页面建好后立刻按默认预设填好全部参数 ——
        # 避免初始状态有空字段（空的会被当成「不传该参数」而落到别的默认值上）
        self._apply_preset_params()
        self._on_corner_mode()

        ttk.Label(f, text="提示：途经点最多 10 个（OSRM 限制）。选点顺序就是行进顺序，"
                          "中途想改顺序可在弹出的地图窗口里用「上移/下移」。",
                  foreground="#777", justify="left").pack(anchor="w", padx=16, pady=(6, 12))

    def _build_gen(self):
        f = self.tab_gen
        ttk.Label(f, text="生成轨迹（参数与命令行一一对应）", font=("", 11, "bold")).pack(
            anchor="w", padx=12, pady=(12, 8))

        form = ttk.Frame(f)
        form.pack(fill="x", padx=12)

        def row(r, label, widget, hint=""):
            ttk.Label(form, text=label, width=22, anchor="e").grid(row=r, column=0, padx=6, pady=3, sticky="e")
            widget.grid(row=r, column=1, padx=6, pady=3, sticky="w")
            if hint:
                ttk.Label(form, text=hint, foreground="#777", font=("", 8)).grid(
                    row=r, column=2, padx=6, sticky="w")

        self.var_from = tk.StringVar(value="39.9087,116.3975")
        self.var_to = tk.StringVar(value="39.8822,116.4066")
        row(0, "起点", ttk.Entry(form, textvariable=self.var_from, width=26),
            "经纬度 / 命名地点 / 地名")
        row(1, "终点", ttk.Entry(form, textvariable=self.var_to, width=26),
            "同上")

        # 起点终点的其它指定方式：命名地点、地图选点
        pick = ttk.Frame(form)
        ttk.Label(pick, text="命名地点:").pack(side="left")
        self.var_alias = tk.StringVar()
        self.cb_alias = ttk.Combobox(pick, textvariable=self.var_alias, width=14, state="readonly")
        self.cb_alias.pack(side="left", padx=4)
        ttk.Button(pick, text="设为起点", width=9,
                   command=lambda: self._use_alias(self.var_from)).pack(side="left", padx=2)
        ttk.Button(pick, text="设为终点", width=9,
                   command=lambda: self._use_alias(self.var_to)).pack(side="left", padx=2)
        ttk.Button(pick, text="刷新", width=6, command=self.refresh_aliases).pack(side="left", padx=2)
        row(2, "选择", pick, "存过的常用点，免敲坐标")

        pick2 = ttk.Frame(form)
        ttk.Button(pick2, text="地图选起点", width=12,
                   command=lambda: self.act_map_pick("from")).pack(side="left", padx=2)
        ttk.Button(pick2, text="地图选终点", width=12,
                   command=lambda: self.act_map_pick("to")).pack(side="left", padx=2)
        ttk.Button(pick2, text="读设备位置", width=11,
                   command=self.act_device_location).pack(side="left", padx=2)
        ttk.Button(pick2, text="把起点存为命名地点", width=18,
                   command=self.act_save_alias).pack(side="left", padx=8)
        row(3, "其它方式", pick2, "鼠标点地图最直观")

        self.var_preset = tk.StringVar(value="run 跑步")
        row(4, "预设", ttk.Combobox(form, textvariable=self.var_preset, values=list(PRESETS), width=24, state="readonly"),
            "决定速度/采样率/噪声/etc")

        self.var_provider = tk.StringVar(value="osrm（免费，无需 key）")
        row(5, "路径来源", ttk.Combobox(form, textvariable=self.var_provider, values=list(PROVIDERS),
                                    width=24, state="readonly"))

        self.var_profile = tk.StringVar(value="foot（步行/跑步路网）")
        row(6, "路网轮廓", ttk.Combobox(form, textvariable=self.var_profile, values=list(PROFILES),
                                    width=24, state="readonly"))

        self.var_mode = tk.StringVar(value="once 跑一次")
        row(7, "回放模式", ttk.Combobox(form, textvariable=self.var_mode, values=list(MODES),
                                    width=24, state="readonly"))

        self.var_file = tk.StringVar()
        fe = ttk.Frame(form)
        ttk.Entry(fe, textvariable=self.var_file, width=34).pack(side="left")
        ttk.Button(fe, text="浏览…", command=self.act_pick_file).pack(side="left", padx=4)
        row(8, "本地路径文件", fe, "仅「路径来源=file」时需要")

        self.var_speed = tk.StringVar()
        self.var_interval = tk.StringVar()
        self.var_accel = tk.StringVar()
        self.var_noise = tk.StringVar()
        row(9, "速度 m/s（留空=预设）", ttk.Entry(form, textvariable=self.var_speed, width=12))
        row(10, "采样间隔 ms", ttk.Entry(form, textvariable=self.var_interval, width=12))
        row(11, "加速度上限 m/s²", ttk.Entry(form, textvariable=self.var_accel, width=12))
        row(12, "GPS 噪声 米", ttk.Entry(form, textvariable=self.var_noise, width=12))
        self.var_seed = tk.StringVar(value="42")
        row(13, "随机种子（可复现）", ttk.Entry(form, textvariable=self.var_seed, width=12))

        self.var_open_plot = tk.BooleanVar(value=True)
        ttk.Checkbutton(form, text="生成后自动画图并打开", variable=self.var_open_plot).grid(
            row=14, column=1, sticky="w", padx=6, pady=6)

        # 本页生成用的名字（强制命名，见 act_gen_go）
        self.var_gui_name = tk.StringVar()
        self.var_gui_showname = tk.StringVar(value="（还没生成）")
        name_row = ttk.Frame(f)
        name_row.pack(fill="x", padx=12)
        ttk.Label(name_row, text="当前轨迹:").pack(side="left")
        ttk.Label(name_row, textvariable=self.var_gui_showname, font=("Consolas", 9),
                  foreground="#2b7").pack(side="left", padx=6)
        ttk.Label(name_row, text="生成本页时会要求命名，重名会被拒绝。",
                  foreground="#777", font=("", 8)).pack(side="left", padx=6)

        btns = ttk.Frame(f)
        btns.pack(fill="x", padx=12, pady=10)
        ttk.Button(btns, text="预览命令（不执行）", command=self.act_gen_preview).pack(side="left", padx=4)
        ttk.Button(btns, text="生成轨迹", command=self.act_gen_go).pack(side="left", padx=4)

        # 参数说明
        ttk.Label(f, text="参数说明", font=("", 10, "bold")).pack(anchor="w", padx=12, pady=(14, 2))
        tips = (
            "预设：walk 步行 12'20\"/km ｜ run 跑步 5'03\"/km（含配速波动+地形）｜ interval 间歇\n"
            "速度：留空则用预设值。步行 1.35 ｜ 慢跑 2.5 ｜ 跑步 3.3 ｜ 快跑 4.5 ｜ 冲刺 6+\n"
            "采样间隔：越小越平滑、文件越大。步行建议 500，跑步 1000\n"
            "GPS 噪声：真实手机城市里 3~8m，开阔地 1.5~3m。设 0 则完全不加噪声\n"
            "种子：固定后可复现同一份轨迹（每次都一样），用于回归测试"
        )
        ttk.Label(f, text=tips, justify="left", foreground="#555").pack(anchor="w", padx=16)

    def _build_files(self):
        f = self.tab_files
        top = ttk.Frame(f)
        top.pack(fill="x", padx=12, pady=(12, 6))
        ttk.Label(top, text="out/ 目录下的轨迹文件", font=("", 11, "bold")).pack(side="left")
        ttk.Button(top, text="刷新", command=self.refresh_files).pack(side="right", padx=4)
        ttk.Button(top, text="打开 out 目录", command=lambda: self._open_path(OUT)).pack(side="right", padx=4)

        cols = ("name", "points", "size", "time")
        self.tree = ttk.Treeview(f, columns=cols, show="headings", height=16)
        for c, t, w in (("name", "文件名", 320), ("points", "点数", 90),
                        ("size", "大小", 100), ("time", "修改时间", 180)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=w, anchor="w")
        self.tree.pack(fill="both", expand=True, padx=12)
        self.tree.bind("<Double-1>", lambda e: self.act_file_plot())

        btns = ttk.Frame(f)
        btns.pack(fill="x", padx=12, pady=10)
        ttk.Button(btns, text="画图", command=self.act_file_plot).pack(side="left", padx=4)
        ttk.Button(btns, text="打开图片", command=self.act_file_open_png).pack(side="left", padx=4)
        ttk.Button(btns, text="查看摘要（show）", command=self.act_file_show).pack(side="left", padx=4)
        ttk.Button(btns, text="回放这条", command=lambda: self.act_file_play(1)).pack(side="left", padx=12)
        ttk.Button(btns, text="回放 ×20 速", command=lambda: self.act_file_play(20)).pack(side="left", padx=4)

        self.refresh_files()

    def _build_ctrl(self):
        f = self.tab_ctrl
        ttk.Label(f, text="设备控制", font=("", 11, "bold")).pack(anchor="w", padx=12, pady=(12, 8))

        conn = ttk.LabelFrame(f, text=" 连接 ")
        conn.pack(fill="x", padx=12, pady=6)
        self.var_device = tk.StringVar(value="未检测")
        ttk.Label(conn, textvariable=self.var_device, font=("Consolas", 9)).pack(
            anchor="w", padx=10, pady=8)
        r = ttk.Frame(conn)
        r.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Button(r, text="检测设备", command=self.act_check_device).pack(side="left", padx=3)
        ttk.Button(r, text="appops 授权", command=self.act_grant).pack(side="left", padx=3)
        ttk.Button(r, text="安装 APK", command=self.act_install_apk).pack(side="left", padx=3)

        run = ttk.LabelFrame(f, text=" 回放控制 ")
        run.pack(fill="x", padx=12, pady=6)
        r2 = ttk.Frame(run)
        r2.pack(fill="x", padx=10, pady=8)
        for text, cmd in (("状态", lambda: self._mroute("status", title="查询状态")),
                          ("暂停", lambda: self._mroute("pause", title="暂停")),
                          ("恢复", lambda: self._mroute("resume", title="恢复")),
                          ("停止", lambda: self._mroute("stop", title="停止"))):
            ttk.Button(r2, text=text, command=cmd, width=8).pack(side="left", padx=3)

        r3 = ttk.Frame(run)
        r3.pack(fill="x", padx=10, pady=(0, 8))
        ttk.Label(r3, text="倍速:").pack(side="left")
        self.var_mult = tk.StringVar(value="5")
        ttk.Entry(r3, textvariable=self.var_mult, width=6).pack(side="left", padx=4)
        ttk.Button(r3, text="应用倍速", command=self.act_mult).pack(side="left", padx=4)
        ttk.Button(r3, text="实时日志（另开窗口）", command=self.act_tail_logs).pack(side="left", padx=12)

        logs = ttk.LabelFrame(f, text=" 运行日志（持久化，可回溯） ")
        logs.pack(fill="x", padx=12, pady=6)
        r5 = ttk.Frame(logs)
        r5.pack(fill="x", padx=10, pady=8)
        ttk.Button(r5, text="查看设备日志", width=13,
                   command=self.act_show_device_logs).pack(side="left", padx=3)
        ttk.Button(r5, text="导出到 out/", width=12,
                   command=self.act_pull_device_logs).pack(side="left", padx=3)
        ttk.Label(r5, text="过滤标签:").pack(side="left", padx=(10, 2))
        self.var_logtag = tk.StringVar()
        ttk.Combobox(r5, textvariable=self.var_logtag, width=10,
                     values=["", "LIFE", "CMD", "PLAY", "BEAT", "ERR", "STATUS"]
                     ).pack(side="left")
        ttk.Label(logs, text="BEAT 是心跳（每 30 秒一条）。日志里出现远超 30 秒的空洞，"
                             "就说明进程当时被系统冻结或杀掉了 —— 这是判断"
                             "「有没有被拦」最直接的证据。",
                  foreground="#777", font=("", 8), justify="left").pack(
            anchor="w", padx=12, pady=(0, 8))

        maint = ttk.LabelFrame(f, text=" 维护（改了 Kotlin 代码才需要） ")
        maint.pack(fill="x", padx=12, pady=6)
        r4 = ttk.Frame(maint)
        r4.pack(fill="x", padx=10, pady=8)
        ttk.Button(r4, text="重新编译并安装 APK", command=self.act_rebuild).pack(side="left", padx=3)
        ttk.Label(f, text="注：编译会用到 .toolchain 里的 JDK/SDK/Gradle，\n"
                          "且项目路径含非 ASCII 字符，脚本会自动镜像到 ASCII 目录再编译。",
                  foreground="#777", justify="left").pack(anchor="w", padx=16, pady=(4, 12))

    # ---------------- 队列消费 ----------------

    def _drain(self):
        try:
            while True:
                kind, text = self.q.get_nowait()
                if kind == "log":
                    self.log.insert("end", text + "\n")
                    self.log.see("end")
                elif kind == "ok":
                    self._log_tag(text, "ok")
                    self.var_status.set(text)
                elif kind == "err":
                    self._log_tag(text, "err")
                    self.var_status.set(text)
                elif kind == "warn":
                    self._log_tag(text, "warn")
                    self.var_status.set(text)
                elif kind == "done":
                    self.var_status.set(f"就绪（上个任务：{text}）")
                    self.refresh_files()
                elif kind == "dev":
                    self.var_device.set(text)
                elif kind == "loc":
                    lat, lon, why = text
                    self.var_from.set(f"{lat:.6f},{lon:.6f}")
                    self._last_map_center = (lat, lon)
                    self._log_tag(f"起点已设为 {lat:.6f},{lon:.6f}（{why}）", "ok")
        except queue.Empty:
            pass
        self.win.after(120, self._drain)

    def _log_tag(self, text, tag):
        self.log.insert("end", text + "\n", tag)
        self.log.see("end")

    def _clear_log(self):
        self.log.delete("1.0", "end")

    # ---------------- 动作 ----------------

    def _startup_check(self):
        self._log_tag("MockRoute 控制台已启动。", "ok")
        for line in (
            f"项目根目录: {ROOT}",
            f"Python: {PYTHON}",
            "",
            "建议第一步点【① 环境体检】。",
            "如果手机没连上，先确认：USB 调试已开、手机上点过「允许 USB 调试」。",
        ):
            self._log_tag(line, "warn" if line.startswith("如果") else "ok")
        self.act_check_device()
        self.refresh_aliases()
        self.refresh_paths()

    def _mroute(self, *args, title: str) -> None:
        self.runner.mroute(*args, title=title)

    # --- 快速开始页 ---

    def act_doctor(self):
        self.runner.mroute("doctor", "--fix", title="环境体检")

    def act_gen_run(self):
        self.runner.mroute(
            "gen", "--preset", "run",
            "--from", "39.90870,116.39750",
            "--to", "39.88220,116.40660",
            "--provider", "osrm", "--profile", "foot",
            "--seed", "42", "--out", "out/run5k.json",
            title="生成跑步轨迹 5km")

    def _play(self, route: str, mult: int | None):
        args = ["play", "--route", route, "--no-wait"]
        if mult and mult != 1:
            args += ["--mult", str(mult)]
        self._mroute(*args, title=f"回放 {Path(route).name}")

    def act_play_20x(self):
        self._play("out/run5k.json", 20)

    def act_play_1x(self):
        self._play("out/run5k.json", None)

    def act_point(self):
        at = self.var_point.get().strip()
        if not at:
            messagebox.showwarning("提示", "请填坐标，格式：纬度,经度")
            return
        self._mroute("point", "--at", at, title="设置固定坐标")

    def act_circle(self):
        at = self.var_point.get().strip()
        try:
            radius = float(self.var_radius.get())
            period = float(self.var_period.get())
        except ValueError:
            messagebox.showwarning("提示", "半径和周期要填数字")
            return
        self._mroute("circle", "--at", at, "--radius", str(radius),
                     "--period", str(period), title="原地绕圈")

    def act_open_latest_png(self):
        pngs = sorted(OUT.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not pngs:
            messagebox.showinfo("提示", "out/ 目录下还没有图片，先生成一条轨迹")
            return
        self._open_path(pngs[0])

    # --- 多点选路页 ---

    def _route_center(self):
        """地图初始视野：优先用已选起点，其次上次的视野，否则北京。"""
        c = loc.parse_coord_literal(self.var_r_from.get())
        if c:
            return c
        return getattr(self, "_last_map_center", (39.9087, 116.3975))

    def act_route_pick(self):
        """打开多点选路地图（新建）。返回的点自动分流到起点/途经点/终点。"""
        lat, lon = self._route_center()

        def done(points):
            if len(points) < 2:
                messagebox.showwarning("点太少", "至少要有起点和终点。")
                return
            # 保存**真实选点**。绕圈对话框要用它估算一圈路程 ——
            # from/via/to 只有三个点，算不出真实一圈有多长（路网是曲线）。
            self._picked_points = [(float(a), float(b)) for a, b in points]
            self.var_r_from.set(f"{points[0][0]:.6f},{points[0][1]:.6f}")
            self.var_r_to.set(f"{points[-1][0]:.6f},{points[-1][1]:.6f}")
            vias = [f"{a:.6f},{b:.6f}" for a, b in points[1:-1]]
            self.var_r_via.set(";".join(vias))
            self._render_route_view(points)
            self._last_map_center = points[0]
            self._log_tag(f"已选出 {len(points)} 个点：起点 {self.var_r_from.get()}，"
                          f"途经 {len(vias)} 个，终点 {self.var_r_to.get()}", "ok")

        try:
            mappicker.RoutePicker(self.win, lat, lon, done,
                                  self._save_alias_from_picker,
                                  aliases=loc.load_aliases(ROOT),
                                  amap_key=self._amap_key(),
                                  city=self._amap_city())
        except Exception as e:                                       # noqa: BLE001
            self._log_tag(f"[失败] 打不开选路地图: {type(e).__name__}: {e}", "err")
            return
        self._log_tag("多点选路地图已打开：单击加点、可检索地名、拖动平移、滚轮缩放；"
                      "右侧列表可改顺序/删除，双击列表项可定位。"
                      "选完点【确定（生成路线）】。", "ok")

    def _amap_key(self) -> str:
        """高德 key（有就用，没有检索会自动退回 Photon）。"""
        return (self.var_amap_key.get().strip() if hasattr(self, "var_amap_key")
                else "") or os.environ.get("AMAP_KEY", "")

    def _amap_city(self) -> str:
        return (self.var_city.get().strip() if hasattr(self, "var_city")
                else "") or os.environ.get("AMAP_CITY", "")

    def _single_pick(self, var):
        """给某个输入框单独做一次地图选点，并支持一步存成命名地点。"""
        c = loc.parse_coord_literal(var.get())
        init = c if c else getattr(self, "_last_map_center", (39.9087, 116.3975))

        def done(lat, lon):
            var.set(f"{lat:.6f},{lon:.6f}")
            self._last_map_center = (lat, lon)
            self._log_tag(f"已选: {lat:.6f},{lon:.6f}", "ok")
            if messagebox.askyesno("存为命名地点？",
                                   f"要把它（{lat:.6f},{lon:.6f}）存成命名地点吗？\n"
                                   "存了以后就能直接用名字引用。"):
                self._save_alias_var(var)

        mappicker.MapPicker(self.win, init[0], init[1], done,
                            amap_key=self._amap_key(), city=self._amap_city())

    def _save_alias_var(self, var):
        c = loc.parse_coord_literal(var.get())
        if not c:
            messagebox.showinfo("提示", "这里还不是坐标。\n先点「地图选」，或直接填 纬度,经度。")
            return
        name = simpledialog.askstring("存为命名地点", "给这个位置起个名字：", parent=self.win)
        if not name or not name.strip():
            return
        try:
            p = loc.save_alias(ROOT, name.strip(), c[0], c[1], note="控制台保存")
        except Exception as e:                                       # noqa: BLE001
            messagebox.showerror("失败", f"保存失败：{e}")
            return
        self.refresh_aliases()
        self._log_tag(f"已保存命名地点 {name!r} = {c[0]:.6f},{c[1]:.6f} -> {p}", "ok")

    # 预设的**全量**参数表。
    #
    # 为什么每个预设都要把**所有**参数写全：用户反馈「选择模拟类型的时候，
    # 填进去一些默认值，以确保不发生错乱」。如果预设只填速度、不填噪声和
    # 离路上限，那么先选「步行」（噪声 1.5）再切「跑步」时，噪声会残留成
    # 1.5 —— 两个预设的参数就混在一起了。所以这里一律填全，切换即整体覆盖。
    #
    # 数值来源：docs/08-噪声与离路配置表.md（实测 7 个场景 × 6 个离路上限）
    #   · 噪声 σ：开阔 0.5 / 公园 1.0 / 普通街道 1.5 / 城市跑步 2.0 / 峡谷 3.0
    #   · 离路上限：按「上限 ≥ 3×σ」选，保证被夹占比 <3%（分布不退化）
    #   · 采样：步行 500ms；跑步 1000ms；高精度 250ms
    #   · 单步转角 25°：与 CLI 默认一致；步行/慢跑时本就低于阈值，不触发
    #   · 弯道采样点 5：保证弯道里至少 5 个点，方向不会突变成一次跳变
    PRESET_PARAMS = {
        "walk": dict(speed="1.35", interval="500", noise="1.5",
                     corner="6", maxoff="3", stepturn="75", minsamp="1",
                     alat="1.5", cornermode="auto",
                     desc="普通街道步行（两侧有楼）"),
        "run": dict(speed="3.3", interval="1000", noise="2.0",
                    corner="8", maxoff="4", stepturn="75", minsamp="1",
                    alat="3.0", cornermode="auto",
                    desc="城市街跑"),
        "interval": dict(speed="3.6", interval="500", noise="2.0",
                         corner="10", maxoff="5", stepturn="75", minsamp="1",
                         alat="3.0", cornermode="auto",
                         desc="间歇跑（急加速急减速）"),
        "custom": dict(speed="1.4", interval="1000", noise="1.5",
                       corner="6", maxoff="3", stepturn="75", minsamp="1",
                       alat="1.5", cornermode="auto", desc="自定义"),
    }

    def _apply_preset_params(self):
        """把当前预设的**全部**参数写进输入框（切换预设即整体覆盖，不会残留）。"""
        p = PRESETS.get(self.var_r_preset.get(), "walk")
        cfg = self.PRESET_PARAMS.get(p, self.PRESET_PARAMS["walk"])
        self.var_r_speed.set(cfg["speed"])
        self.var_r_interval.set(cfg["interval"])
        self.var_r_noise.set(cfg["noise"])
        self.var_r_corner.set(cfg["corner"])
        self.var_r_maxoff.set(cfg["maxoff"])
        self.var_r_stepturn.set(cfg["stepturn"])
        self.var_r_minsamp.set(cfg["minsamp"])
        self.var_r_alat.set(cfg["alat"])
        # 圆滑模式是按显示名存的，反查回去
        _want = cfg.get("cornermode", "auto")
        for disp, key in CORNER_MODES.items():
            if key == _want:
                self.var_r_cornermode.set(disp)
                break
        self._on_corner_mode()
        self._update_r_speed_hint()

    def _on_r_preset(self):
        """切换预设 / 点「按预设填回」时，整体覆盖所有参数。"""
        self._apply_preset_params()
        p = PRESETS.get(self.var_r_preset.get(), "walk")
        desc = self.PRESET_PARAMS.get(p, {}).get("desc", "")
        if desc:
            self._log_tag(f"预设「{self.var_r_preset.get()}」已填入全部参数（{desc}）", "ok")

    def _on_corner_mode(self):
        """切换圆滑策略：更新说明文字，并在 none 模式禁用半径输入框。

        为什么 none 要禁用半径：none 的含义就是「绝不切角」，
        此时半径无意义。禁用它比留着一个不起作用的输入框更不容易误解。
        """
        m = CORNER_MODES.get(self.var_r_cornermode.get(), "auto")
        if m == "none":
            self.ent_r_corner.state(["disabled"])
            self.lbl_r_corner.state(["disabled"])
            self.var_r_cornermode_hint.set(
                "真路径切角 0.000m，100% 贴中心线；拐弯方向是尖角")
        elif m == "round":
            self.ent_r_corner.state(["!disabled"])
            self.lbl_r_corner.state(["!disabled"])
            self.var_r_cornermode_hint.set(
                "用下面填的半径；切角不超预算时才安全")
        else:
            self.ent_r_corner.state(["!disabled"])
            self.lbl_r_corner.state(["!disabled"])
            self.var_r_cornermode_hint.set(
                "切角预算 = 离路上限 x 0.25，预算内取最大半径")

    def _set_r_speed(self, v: str):
        self.var_r_speed.set(v)
        self._update_r_speed_hint()

    def _validate_route_params(self) -> bool:
        """生成前校验参数，避免「错乱」的参数被静默传下去。

        返回 False 表示用户需要先改参数（已弹窗提示）。
        """
        checks = [
            (self.var_r_speed, "速度", 0.05, 20.0),
            (self.var_r_interval, "采样间隔", 50, 10000),
            (self.var_r_noise, "GPS 噪声", 0.0, 20.0),
            (self.var_r_corner, "拐角圆滑半径", 0.0, 50.0),
            (self.var_r_maxoff, "离路上限", 0.0, 50.0),
            (self.var_r_stepturn, "单步转角", 0.0, 180.0),
            (self.var_r_minsamp, "弯道采样点", 0.0, 50.0),
            (self.var_r_alat, "过弯横向加速度", 0.0, 12.0),
        ]
        for var, name, lo, hi in checks:
            s = var.get().strip()
            if not s:
                continue
            try:
                v = float(s)
            except ValueError:
                messagebox.showwarning("参数有误", f"「{name}」不是数字：{s!r}")
                return False
            if not (lo <= v <= hi):
                messagebox.showwarning(
                    "参数有误",
                    f"「{name}」= {v} 超出合理范围 [{lo}, {hi}]。")
                return False
        # 圆滑策略必须是已知项（下拉框是 readonly，但不排除变量被别处改坏）
        if CORNER_MODES.get(self.var_r_cornermode.get()) is None:
            messagebox.showwarning("参数有误",
                                   f"圆滑策略无法识别：{self.var_r_cornermode.get()!r}")
            return False

        # 离路上限与噪声的搭配提醒：上限 < 3×σ 时分布会退化
        try:
            sig = float(self.var_r_noise.get() or 0)
            lim = float(self.var_r_maxoff.get() or 0)
        except ValueError:
            return True
        if lim > 0 and sig > 0 and lim < sig * 1.5:
            if not messagebox.askyesno(
                    "参数搭配提醒",
                    f"离路上限 {lim:g}m 相对噪声 σ={sig:g}m 偏小。\n\n"
                    f"实测「被夹住」的点会明显增多（误差分布退化成一个圆），"
                    f"那在统计上不自然。建议上限 ≥ 3×σ = {3*sig:g}m，"
                    f"或者把噪声降到 {lim/3:.1f}m 以下。\n\n仍要继续生成吗？"):
                return False
        return True

    def _update_r_speed_hint(self):
        """把速度换算成配速显示 —— 比 m/s 直观得多。"""
        s = self.var_r_speed.get().strip()
        if not s:
            self.var_r_speed_hint.set("")
            return
        try:
            v = float(s)
        except ValueError:
            self.var_r_speed_hint.set("不是数字")
            return
        if v <= 0.05:
            self.var_r_speed_hint.set("")
            return
        pace = 1000.0 / v
        kmh = v * 3.6
        m, sec = divmod(int(round(pace)), 60)
        self.var_r_speed_hint.set(f"= {m}'{sec:02d}\"/km  ({kmh:.1f} km/h)")

    def _route_args(self, out_path: str, extra=None) -> list | None:
        frm = self.var_r_from.get().strip()
        to = self.var_r_to.get().strip()
        if not frm or not to:
            messagebox.showwarning("提示", "至少要填起点和终点。\n点「打开地图选路」最快。")
            return None
        args = ["gen", "--preset", PRESETS[self.var_r_preset.get()],
                "--provider", "osrm",
                "--profile", PROFILES[self.var_r_profile.get()],
                "--from", frm, "--to", to]
        vias = [v.strip() for v in self.var_r_via.get().split(";") if v.strip()]
        for v in vias:
            args += ["--via", v]
        # 速度/采样/噪声/拐角圆滑：填了就以显式参数传给 gen（覆盖预设）
        for var, flag in ((self.var_r_speed, "--speed"),
                          (self.var_r_interval, "--interval"),
                          (self.var_r_noise, "--noise"),
                          (self.var_r_corner, "--corner-radius"),
                          (self.var_r_maxoff, "--max-offset"),
                          (self.var_r_stepturn, "--corner-step-turn"),
                          (self.var_r_minsamp, "--corner-min-samples"),
                          (self.var_r_alat, "--a-lat")):
            v = var.get().strip()
            if v:
                args += [flag, v]
        # 圆滑策略是枚举（不是数字），单独追加
        _cm = CORNER_MODES.get(self.var_r_cornermode.get(), "auto")
        args += ["--corner-mode", _cm]
        args += ["--out", out_path]
        if extra:
            args += extra
        return args

    # 名字合法性：中文、字母、数字、下划线、短横线
    _NAME_OK = re.compile(r"^[\w\u4e00-\u9fff-]{1,40}$")

    def _ask_route_name(self):
        """让用户给这条轨迹命名，**重名直接拒绝**。

        为什么要强制命名：以前所有生成都覆盖同一个 out/gui-route.json，
        导致「生成的永远是上一次的」—— 分不清哪次是哪次，图片查看器还会
        缓存旧图。强制命名 + 拒绝重名从根上消除这两个问题。
        """
        name = simpledialog.askstring(
            "给这条轨迹命名",
            "这条轨迹叫什么名字？\n"
            "会保存为 out/<名字>.json；同名文件已存在时会被拒绝。",
            parent=self.win)
        if name is None:
            return None                      # 用户取消
        name = name.strip()
        if not name:
            messagebox.showwarning("名字不能为空", "请输入一个名字。")
            return None
        if not self._NAME_OK.match(name):
            messagebox.showwarning(
                "名字不合法",
                "名字只能用中文、字母、数字、下划线、短横线，且不超过 40 字。\n"
                f"你输入的是：{name}")
            return None

        target = OUT / f"{name}.json"
        if target.exists() and name not in self._session_names:
            # 只允许覆盖**本次会话自己刚生成的**，其它一律拒绝
            messagebox.showwarning(
                "名字已被占用",
                f"out/{name}.json 已经存在。\n\n"
                "换个名字，或者先去「轨迹文件」页把它删掉。\n"
                "（重名会让「这条轨迹是哪次生成的」变得分不清，所以这里拒绝覆盖）")
            return None
        return name

    def act_route_preview(self):
        # 预览不需要真名字，用占位路径即可
        name = self.var_r_name.get().strip() or "preview"
        args = self._route_args(f"out/{name}.json")
        if args:
            self.runner.mroute(*args, "--dry-run", title="预览路线命令")

    # 起终点近到这个距离（米）就认为「可能是想绕圈」
    LOOP_HINT_M = 250.0

    def _route_endpoint_gap(self):
        """返回 (起点, 终点, 直线距离米)。解析不了就返回 None。"""
        try:
            a = loc.parse_coord_literal(self.var_r_from.get().strip())
            b = loc.parse_coord_literal(self.var_r_to.get().strip())
        except Exception:                                            # noqa: BLE001
            return None
        if not a or not b:
            return None
        try:
            d = geo.haversine(a[0], a[1], b[0], b[1])
        except Exception:                                            # noqa: BLE001
            return None
        return a, b, d

    def _ask_laps(self):
        """起终点很近时问用户是否绕圈，是则返回圈数（1 = 不绕）。

        用自定义对话框而不是两个连续的 messagebox —— 用户明确要求
        「弹出一个窗口告知一圈的路程，并在该窗口下让用户输入圈数」。
        """
        g = self._route_endpoint_gap()
        if not g:
            return 1
        _, _, gap = g
        if gap > self.LOOP_HINT_M:
            return 1
        # 起终点几乎重合 -> 明显是环线，直接问圈数
        answer = messagebox.askyesnocancel(
            "起终点很近",
            f"起点和终点直线距离只有 {gap:.0f} 米。\n\n"
            f"是不是想沿这条路线**绕圈**？\n\n"
            f"「是」  = 绕圈（下一步填圈数）\n"
            f"「否」  = 只跑一趟\n"
            f"「取消」= 回去改起终点")
        if answer is None:
            return None            # 取消 -> 中止生成
        if not answer:
            return 1

        # ---- 绕圈对话框：显示一圈路程 + 圈数输入 ----
        return self._laps_dialog(gap)

    def _laps_dialog(self, gap_m):
        """绕圈设置窗口：告知一圈路程，让用户输入圈数。返回圈数或 None。"""
        dlg = tk.Toplevel(self.win)
        dlg.title("绕圈设置")
        dlg.transient(self.win)
        dlg.resizable(False, False)
        dlg.grab_set()

        f = ttk.Frame(dlg, padding=16)
        f.pack(fill="both", expand=True)

        ttk.Label(f, text="按这条路线绕圈", font=("", 11, "bold")).pack(anchor="w")
        ttk.Label(
            f,
            text=f"起终点直线距离 {gap_m:.0f} 米。\n"
                 f"下面填要跑几圈 —— 每圈沿同一路线跑一次，时间轴连续，\n"
                 f"不会每圈重新起步。",
            foreground="#666", justify="left").pack(anchor="w", pady=(4, 12))

        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Label(row, text="圈数").pack(side="left")
        var_laps = tk.StringVar(value="5")
        ent = ttk.Entry(row, textvariable=var_laps, width=8)
        ent.pack(side="left", padx=6)

        # 一圈路程（尽量给准；算不出来就显示「生成后确认」）
        lap_txt = tk.StringVar(value="一圈路程：正在估算…")
        ttk.Label(f, textvariable=lap_txt, foreground="#2b7").pack(
            anchor="w", pady=(10, 0))
        tot_txt = tk.StringVar(value="")
        ttk.Label(f, textvariable=tot_txt, foreground="#2b7").pack(anchor="w")

        def refresh(*_a):
            try:
                n = int(var_laps.get())
            except ValueError:
                tot_txt.set("")
                return
            if n < 1:
                tot_txt.set("")
                return
            if self._lap_m:
                tot_txt.set(f"合计约 {self._lap_m * n / 1000:.2f} km"
                            f"（{self._lap_m * n:.0f} m）")
            else:
                tot_txt.set(f"合计 {n} 圈")

        var_laps.trace_add("write", refresh)

        # 估一圈路程。
        #
        # 优先用**地图上真实选的点**（self._picked_points）——它就是这一圈的
        # 折线形状，长度接近真实路网里程。
        #
        # 原来这里写成 `getattr(self, "_route_points", None)`，而
        # `_route_points` 是个**方法**不是属性，取到的是方法对象，
        # 下一行 len(pts) 直接 TypeError 被 except 吞掉，于是永远停在
        # 「正在估算…」，用户看到的就是「卡在计算路程这一步」。
        # 就算写对成 _route_points()，它也只返回 from/via/to 三个点，
        # 三个点连成的直线算不出一圈真实路程，所以必须用选点。
        self._lap_m = 0.0
        pts = list(getattr(self, "_picked_points", None) or [])
        if len(pts) < 2:
            # 没有地图选点时退化为 from/via/to（至少给个下界）
            pts = self._route_points()
        if len(pts) >= 2:
            try:
                self._lap_m = geo.total_length([(float(p[0]), float(p[1]))
                                                for p in pts])
            except Exception:                                        # noqa: BLE001
                self._lap_m = 0.0
        if self._lap_m > 1:
            lap_txt.set(f"一圈路程：约 {self._lap_m:.0f} 米"
                        f"（{self._lap_m / 1000:.2f} km，按你选的点估算）")
        else:
            lap_txt.set("一圈路程：按路网实际里程计算，生成后会显示")
        refresh()

        result = {"laps": None}

        def on_ok():
            try:
                n = int(var_laps.get())
            except ValueError:
                messagebox.showwarning("圈数有误", "圈数要填整数。", parent=dlg)
                return
            if not (1 <= n <= 200):
                messagebox.showwarning("圈数有误", "圈数请在 1~200 之间。",
                                       parent=dlg)
                return
            result["laps"] = n
            dlg.destroy()

        def on_cancel():
            dlg.destroy()

        # 底部按钮栏。
        #
        # 用 grid 而不是 pack(side="right")：后者在窗口被 WM 重新布局、
        # 或高 DPI 缩放下可能把按钮挤到可视区外（用户反馈「忘记写确定按钮」）。
        # grid 让按钮栏固定占满一行，且两列权重固定，按钮不会被挤掉。
        btns = ttk.Frame(f)
        btns.pack(fill="x", side="bottom", pady=(18, 0))
        btns.columnconfigure(0, weight=1)      # 左侧弹性空隙
        btns.columnconfigure(1, weight=0)      # 取消
        btns.columnconfigure(2, weight=0)      # 确定
        btn_ok = ttk.Button(btns, text="  确定  ", command=on_ok)
        btn_ok.grid(row=0, column=2, padx=(8, 0), sticky="e")
        ttk.Button(btns, text="  取消  ", command=on_cancel).grid(
            row=0, column=1, sticky="e")
        # 设为默认按钮：窗口获得焦点时按 Enter 直接确认（不依赖焦点在输入框上）
        try:
            dlg.bind("<Return>", lambda e: (on_ok(), "break")[1])
            dlg.bind("<KP_Enter>", lambda e: (on_ok(), "break")[1])
            btn_ok.bind("<Return>", lambda e: (on_ok(), "break")[1])
            btn_ok.focus_set()
        except Exception:                                            # noqa: BLE001
            pass
        # 点窗口右上角关闭 = 取消（否则会返回 None 但语义不清）
        dlg.protocol("WM_DELETE_WINDOW", on_cancel)
        dlg.bind("<Escape>", lambda e: on_cancel())

        ent.focus_set()
        ent.select_range(0, "end")

        # 居中到主窗口（并确保完整落在屏幕内）
        dlg.update_idletasks()
        try:
            sw, sh = dlg.winfo_screenwidth(), dlg.winfo_screenheight()
            dw, dh = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
            x = self.win.winfo_rootx() + (self.win.winfo_width() - dw) // 2
            y = self.win.winfo_rooty() + (self.win.winfo_height() - dh) // 3
            x = max(0, min(x, sw - dw))
            y = max(0, min(y, sh - dh))
            dlg.geometry(f"{dw}x{dh}+{x}+{y}")
        except Exception:                                            # noqa: BLE001
            pass

        self.win.wait_window(dlg)
        return result["laps"]

    def act_route_gen(self):
        # 生成前先校验参数 —— 避免「错乱」的参数被静默传下去
        if not self._validate_route_params():
            return
        # 起终点很近 -> 问是否绕圈并填圈数
        laps = self._ask_laps()
        if laps is None:                 # 用户取消
            return
        # 生成前先要名字 —— 每条轨迹都有独立文件，不会互相覆盖
        name = self._ask_route_name()
        if not name:
            return
        self.var_r_name.set(name)
        extra = ["--laps", str(laps)] if laps and laps > 1 else None
        args = self._route_args(f"out/{name}.json", extra)
        if not args:
            return
        if not self.runner.mroute(*args, title=f"生成轨迹 {name}"):
            return
        self._session_names.add(name)        # 本次会话生成的，允许后续覆盖
        # 生成是毫秒级的，但仍要等它真的结束再动 —— 否则会和画图/回放抢 runner
        self.win.after(800, self._route_after_gen, name)

    def _route_after_gen(self, name=None):
        if self.runner.busy:
            self.win.after(400, self._route_after_gen, name)
            return
        key = name or self.var_r_name.get().strip()
        if not key:
            self._log_tag("[!] 没有轨迹名，无法定位生成结果", "warn")
            return
        target = OUT / f"{key}.json"
        if not target.exists():
            self._log_tag(f"[!] 没有生成出 out/{key}.json —— 看看上面的报错", "warn")
            return

        mt = time.strftime("%H:%M:%S", time.localtime(target.stat().st_mtime))
        self.var_r_file.set(str(target.relative_to(ROOT)).replace("\\", "/"))
        if hasattr(self, "var_r_showname"):
            self.var_r_showname.set(f"{target.name}  ({target.stat().st_size/1024:.0f} KB, {mt})")
        self._log_tag(
            f"轨迹已就绪: {target.name}  {target.stat().st_size/1024:.0f} KB  "
            f"生成于 {mt}  —— 回放/画图都用这一份", "ok")
        self._report_snap(target)

        if self.var_r_open.get():
            self._route_plot_and_open(target)
        if self.var_r_autoplay.get():
            self.win.after(1500, lambda: self.act_route_play())

    def _report_snap(self, route_file: Path):
        """报告「选的点」与「实际起点/终点」差了多少。

        为什么需要：路网规划只能沿道路走，所以选在建筑/公园内部的点会被
        **吸附到最近的道路**上，实测能挪 50~500 米。这是链路里最大的一环
        误差，而且它**不是 bug** —— 但你必须知道它发生了，否则会以为
        「定位不准」。这里把距离摊开讲，并给出减小办法。
        """
        try:
            data = json.loads(route_file.read_text(encoding="utf-8"))
            pts = data.get("points") or []
            if len(pts) < 2:
                return
            mine = self._route_points()
            if len(mine) < 2:
                return
            from mockroute.geo import haversine
            def d2(a, b):
                return haversine(a[0], a[1], b[0], b[1])
            d_from = d2(mine[0], (pts[0]["lat"], pts[0]["lon"]))
            d_to = d2(mine[-1], (pts[-1]["lat"], pts[-1]["lon"]))
            worst = max(d_from, d_to)
            if worst < 15:
                self._log_tag(
                    f"选点与轨迹吻合：起点差 {d_from:.1f} m，终点差 {d_to:.1f} m", "ok")
                return
            msg = (f"⚠ 道路吸附：起点差 {d_from:.0f} m，终点差 {d_to:.0f} m。\n"
                   f"    这是 OSRM 把点吸附到最近道路造成的，不是定位错误 ——\n"
                   f"    路网规划只能沿道路走。想让它更贴合：\n"
                   f"      · 把点选在道路上（放大地图能看到路），别选在建筑/公园内部\n"
                   f"      · 或用命名地点/直接填坐标，喂精确的路边坐标")
            self._log_tag(msg, "warn")
            if hasattr(self, "var_r_showname"):
                self.var_r_showname.set(
                    f"{route_file.name}  —  道路吸附偏差：起 {d_from:.0f} m / "
                    f"终 {d_to:.0f} m")
        except Exception as e:                                       # noqa: BLE001
            self._log_tag(f"[!] 吸附核对失败: {type(e).__name__}: {e}", "warn")

    def act_route_play(self, mult=None):
        """回放选路页指定的轨迹文件。mult 给了就用它，否则读输入框。"""
        path = (self.var_r_file.get() or "out/gui-route.json").strip()
        p = Path(path)
        if not p.is_absolute():
            p = ROOT / path
        if not p.exists():
            messagebox.showwarning(
                "还没有轨迹",
                f"找不到 {path}\n\n先点【用这条路线生成轨迹】，"
                "或者用下面的「浏览…」选一个已有的轨迹文件。")
            return
        if mult is None:
            try:
                mult = float(self.var_r_mult.get())
            except ValueError:
                mult = 1.0
        args = ["play", "--route", path, "--no-wait"]
        if mult and float(mult) != 1:
            args += ["--mult", str(mult)]
        self._log_tag(f"开始回放 {Path(path).name}"
                      f"（倍速 {mult:g}）—— 手机上应该开始移动了", "ok")
        self._mroute(*args, title=f"回放 {Path(path).name}")

    def act_route_pick_file(self):
        p = filedialog.askopenfilename(
            title="选择轨迹文件", initialdir=str(OUT),
            filetypes=[("轨迹文件", "*.json"), ("所有文件", "*.*")])
        if not p:
            return
        try:
            rel = Path(p).relative_to(ROOT)
            self.var_r_file.set(str(rel).replace("\\", "/"))
        except ValueError:
            self.var_r_file.set(p)

    def _route_plot_and_open(self, route_path=None):
        if self.runner.busy:
            self.win.after(500, self._route_plot_and_open, route_path)
            return
        src = Path(route_path) if route_path else (ROOT / (self.var_r_file.get()
                                                          or "out/gui-route.json"))
        # 图也带唯一名 —— 否则图片查看器缓存住旧图，你会以为「还是上一次的」
        png = src.with_suffix(".png")
        self.runner.start([PYTHON, str(PLOT), str(src), "-o", str(png)], "画图")
        self._log_tag(f"正在画图: {png.name}", "ok")
        self.win.after(1800, lambda: self._open_path(png))

    def act_route_clear(self):
        self.var_r_from.set("")
        self.var_r_to.set("")
        self.var_r_via.set("")
        self.var_route_count.set("0 个点")
        self._log_tag("已清空选路结果", "ok")

    # --- 保存的路径 ---

    def refresh_paths(self):
        try:
            paths = loc.load_paths(ROOT)
        except loc.ResolveError as e:
            self._log_tag(f"[!] 路径读取失败: {e}", "warn")
            paths = {}
        names = list(paths)
        self.cb_path["values"] = names
        if names and not self.var_r_path.get():
            self.var_r_path.set(names[0])
        if names:
            self._log_tag(f"已载入 {len(names)} 条保存的路径（{loc.alias_path(ROOT)}）", "ok")
        return paths

    def act_path_save(self):
        """把当前选点（起点+途经点+终点）存成一条命名路径。"""
        pts = []
        c = loc.parse_coord_literal(self.var_r_from.get())
        if c:
            pts.append(c)
        for v in self.var_r_via.get().split(";"):
            if not v.strip():
                continue
            cv = loc.parse_coord_literal(v)
            if cv:
                pts.append(cv)
        c = loc.parse_coord_literal(self.var_r_to.get())
        if c:
            pts.append(c)

        if len(pts) < 2:
            messagebox.showinfo(
                "还存不了",
                "至少要有起点和终点两个坐标。\n\n"
                "先用「打开地图选路」点出路线，或者在各栏里填 纬度,经度。\n"
                "（如果是命名地点或地名，先解析成坐标再存 —— 路径存的是坐标。）")
            return

        name = simpledialog.askstring("保存选点路径", "给这条路径起个名字：",
                                      parent=self.win)
        if not name or not name.strip():
            return
        try:
            p = loc.save_path(ROOT, name.strip(), pts,
                              note=f"控制台保存（{len(pts)} 个点）",
                              mode=MODES.get("once 跑一次", "once"),
                              preset=PRESETS.get(self.var_r_preset.get(), ""))
        except Exception as e:                                       # noqa: BLE001
            messagebox.showerror("失败", f"保存失败：{e}")
            return
        self.refresh_paths()
        self.var_r_path.set(name.strip())
        self._log_tag(f"已保存路径 {name!r}（{len(pts)} 个点）-> {p}", "ok")

    def act_path_load(self):
        """把保存的路径填进选路页的各个栏位。"""
        name = self.var_r_path.get()
        if not name:
            messagebox.showinfo("提示", "还没有保存的路径。\n先点「保存当前选点」。")
            return
        try:
            paths = loc.load_paths(ROOT)
        except loc.ResolveError as e:
            messagebox.showerror("失败", str(e))
            return
        if name not in paths:
            return
        cli = loc.path_to_cli(paths[name])
        self.var_r_from.set(cli["from"])
        self.var_r_to.set(cli["to"])
        self.var_r_via.set(";".join(cli["via"]))
        self.var_route_count.set(f"{len(paths[name]['points'])} 个点"
                                 f"（途经 {len(cli['via'])} 个）")
        # 顺带恢复当时用的预设
        preset = cli.get("preset")
        if preset:
            for label, val in PRESETS.items():
                if val == preset:
                    self.var_r_preset.set(label)
                    break
        self._last_map_center = (float(paths[name]["points"][0][0]),
                                 float(paths[name]["points"][0][1]))
        self._log_tag(f"已载入路径 {name!r}：起点 {cli['from']}，"
                      f"途经 {len(cli['via'])} 个，终点 {cli['to']}", "ok")

    def act_path_delete(self):
        name = self.var_r_path.get()
        if not name:
            return
        if not messagebox.askyesno("确认", f"删除保存的路径 {name!r}？"):
            return
        if loc.delete_path(ROOT, name):
            self._log_tag(f"已删除路径 {name!r}", "ok")
        self.cb_path["values"] = list(loc.load_paths(ROOT))
        self.var_r_path.set("")
        self.refresh_paths()

    # --- 生成页 ---

    def _gen_args(self, out_path: str = "out/gui-out.json") -> list | None:
        provider = PROVIDERS[self.var_provider.get()]
        preset = PRESETS[self.var_preset.get()]
        args = ["gen", "--preset", preset, "--provider", provider,
                "--mode", MODES[self.var_mode.get()]]

        if provider == "file":
            path = self.var_file.get().strip()
            if not path:
                messagebox.showwarning("提示", "「路径来源=file」需要选一个本地文件")
                return None
            args += ["--file", path]
        else:
            frm, to = self.var_from.get().strip(), self.var_to.get().strip()
            if not frm or not to:
                messagebox.showwarning("提示", "请填起点和终点")
                return None
            args += ["--from", frm, "--to", to]
            if provider == "osrm":
                args += ["--profile", PROFILES[self.var_profile.get()]]

        for var, flag in ((self.var_speed, "--speed"), (self.var_interval, "--interval"),
                          (self.var_accel, "--accel"), (self.var_noise, "--noise")):
            v = var.get().strip()
            if v:
                args += [flag, v]
        seed = self.var_seed.get().strip()
        if seed:
            args += ["--seed", seed]
        args += ["--out", out_path]
        return args

    def act_gen_preview(self):
        name = self.var_gui_name.get().strip() or "preview"
        args = self._gen_args(f"out/{name}.json")
        if args:
            self.runner.mroute(*args, "--dry-run", title="预览生成命令")

    def act_gen_go(self):
        """生成轨迹：与「多点选路」页同样的命名规则（强制命名 + 拒绝重名）。"""
        name = simpledialog.askstring(
            "给这条轨迹命名",
            "这条轨迹叫什么名字？\n"
            "会保存为 out/<名字>.json；同名文件已存在时会被拒绝。",
            parent=self.win)
        if name is None:
            return
        name = name.strip()
        if not name:
            messagebox.showwarning("名字不能为空", "请输入一个名字。")
            return
        if not self._NAME_OK.match(name):
            messagebox.showwarning(
                "名字不合法",
                "名字只能用中文、字母、数字、下划线、短横线，且不超过 40 字。\n"
                f"你输入的是：{name}")
            return
        target = OUT / f"{name}.json"
        if target.exists() and name not in self._session_names:
            messagebox.showwarning(
                "名字已被占用",
                f"out/{name}.json 已经存在。\n\n"
                "换个名字，或者先去「轨迹文件」页把它删掉。")
            return

        self.var_gui_name.set(name)
        args = self._gen_args(f"out/{name}.json")
        if not args:
            return
        if not self.runner.mroute(*args, title=f"生成轨迹 {name}"):
            return
        self._session_names.add(name)
        self.var_r_name.set(name)
        if self.var_open_plot.get():
            self.win.after(1000, self._plot_and_open, name)

    def _plot_and_open(self, name=None):
        if self.runner.busy:
            self.win.after(500, self._plot_and_open, name)
            return
        key = name or self.var_gui_name.get().strip()
        src = OUT / f"{key}.json"
        if not src.exists():
            self._log_tag(f"[!] 没有生成出 {src.name} —— 看看上面的报错", "warn")
            return
        png = src.with_suffix(".png")
        mt = time.strftime("%H:%M:%S", time.localtime(src.stat().st_mtime))
        self._log_tag(f"轨迹已就绪: {src.name}  {src.stat().st_size/1024:.0f} KB  "
                      f"生成于 {mt}", "ok")
        # 顺便把它设成回放页的目标，省得手动挑
        self.var_r_file.set(str(src.relative_to(ROOT)).replace("\\", "/"))
        if hasattr(self, "var_r_showname"):
            self.var_r_showname.set(f"{src.name}  ({src.stat().st_size/1024:.0f} KB, {mt})")
        if hasattr(self, "var_gui_showname"):
            self.var_gui_showname.set(f"{src.name}  ({src.stat().st_size/1024:.0f} KB, {mt})")
        self.runner.start([PYTHON, str(PLOT), str(src), "-o", str(png)], "画图")
        self.win.after(1800, lambda: self._open_path(png))

    def act_pick_file(self):
        ex = ROOT / "examples"
        p = filedialog.askopenfilename(
            title="选择路径文件",
            initialdir=str(ex if ex.exists() else ROOT),
            filetypes=[("路径文件", "*.txt *.geojson *.json *.gpx"), ("所有文件", "*.*")])
        if p:
            self.var_file.set(p)

    # --- 文件页 ---

    def refresh_files(self):
        if not hasattr(self, "tree"):
            return
        sel = self.tree.selection()
        keep = self.tree.item(sel[0], "values")[0] if sel else None
        self.tree.delete(*self.tree.get_children())
        if not OUT.exists():
            return
        import datetime
        import json as _json
        rows = []
        for p in sorted(OUT.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
            n = ""
            try:
                data = _json.loads(p.read_text(encoding="utf-8"))
                n = len(data.get("points", []))
            except Exception:                                        # noqa: BLE001
                n = "?"
            mt = datetime.datetime.fromtimestamp(p.stat().st_mtime).strftime("%m-%d %H:%M:%S")
            rows.append((p.name, n, f"{p.stat().st_size/1024:.1f} KB", mt, p))
        for name, n, size, mt, p in rows:
            self.tree.insert("", "end", values=(name, n, size, mt), iid=str(p))
        if keep and self.tree.exists(str(OUT / keep)):
            self.tree.selection_set(str(OUT / keep))

    def _selected_file(self) -> Path | None:
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "先在上面的列表里选一条轨迹")
            return None
        return Path(sel[0])

    def act_file_plot(self):
        p = self._selected_file()
        if not p:
            return
        png = p.with_suffix(".png")
        self.runner.start([PYTHON, str(PLOT), str(p), "-o", str(png)], "画图")
        self.win.after(1500, lambda: self._open_path(png))

    def act_file_open_png(self):
        p = self._selected_file()
        if not p:
            return
        png = p.with_suffix(".png")
        if not png.exists():
            messagebox.showinfo("提示", "还没有图片，先点【画图】")
            return
        self._open_path(png)

    def act_file_show(self):
        p = self._selected_file()
        if p:
            self._mroute("show", "--route", str(p), "--head", "2", title="查看摘要")

    def act_file_play(self, mult: int):
        p = self._selected_file()
        if p:
            self._play(str(p), mult)

    # --- 设备页 ---

    def act_check_device(self):
        """用 adb devices 直接在界面里显示设备，不走 CLI（更快）。"""
        adb = TC / "android-sdk" / "platform-tools" / "adb.exe"
        if not adb.exists():
            self.var_device.set(f"未找到 adb（{adb}）\n先跑 scripts/setup-android.ps1")
            return
        self.var_device.set("检测中…")

        def work():
            try:
                r = subprocess.run([str(adb), "devices", "-l"], capture_output=True,
                                   text=True, encoding="utf-8", errors="replace",
                                   env=self.runner.base_env(), timeout=25)
                lines = [l for l in r.stdout.splitlines()[1:] if l.strip()]
                if not lines:
                    self.q.put(("dev", "没有检测到设备。\n"
                                       "检查：USB 调试已开 / 手机上点过「允许 USB 调试」/ 数据线能传数据"))
                else:
                    txt = "\n".join(lines)
                    if "unauthorized" in txt:
                        txt += ("\n\n状态是 unauthorized：看手机屏幕点「允许 USB 调试」。\n"
                                "若从不弹窗：开发者选项 → 撤销 USB 调试授权 → 重启手机 → 重插")
                    self.q.put(("dev", txt))
            except Exception as e:                                   # noqa: BLE001
                self.q.put(("dev", f"检测失败: {type(e).__name__}: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def act_grant(self):
        adb = TC / "android-sdk" / "platform-tools" / "adb.exe"
        self.runner.start([str(adb), "shell", "appops", "set", "com.mockroute",
                           "android:mock_location", "allow"], "授予模拟位置权限")

    def act_install_apk(self):
        apk = ROOT / "dist" / "MockRoute-debug.apk"
        if not apk.exists():
            messagebox.showwarning("提示", f"找不到 {apk}\n先点【重新编译并安装 APK】")
            return
        adb = TC / "android-sdk" / "platform-tools" / "adb.exe"
        self.runner.start([str(adb), "install", "-r", str(apk)], "安装 APK")

    def act_mult(self):
        v = self.var_mult.get().strip()
        if not v:
            return
        self._mroute("mult", v, title=f"设置倍速 {v}")

    # --- 起点终点的其它指定方式 ---

    def refresh_aliases(self):
        try:
            aliases = loc.load_aliases(ROOT)
        except loc.ResolveError as e:
            self._log_tag(f"[!] 命名地点读取失败: {e}", "warn")
            aliases = {}
        names = list(aliases)
        self.cb_alias["values"] = names
        if names and not self.var_alias.get():
            self.var_alias.set(names[0])
        if names:
            self._log_tag(f"已载入 {len(names)} 个命名地点（{loc.alias_path(ROOT)}）", "ok")
        else:
            self._log_tag("还没有命名地点。可先「地图选起点」，再点「把起点存为命名地点」。", "warn")
        return aliases

    def _use_alias(self, target_var):
        name = self.var_alias.get()
        if not name:
            messagebox.showinfo("提示", "还没有命名地点。\n先用「地图选起点」定位，"
                                       "再点「把起点存为命名地点」。")
            return
        try:
            e = loc.load_aliases(ROOT).get(name)
        except loc.ResolveError as exc:
            messagebox.showerror("失败", str(exc))
            return
        if not e:
            return
        target_var.set(name)
        self._log_tag(f"已填入 {name!r}（{e['lat']:.6f},{e['lon']:.6f}）", "ok")

    def act_map_pick(self, which: str):
        """打开地图选点窗口。用高德瓦片（实测这台机器上 OSM 瓦片不可达）。"""
        var = self.var_from if which == "from" else self.var_to
        c = loc.parse_coord_literal(var.get())
        init = c if c else getattr(self, "_last_map_center", (39.9087, 116.3975))
        label = "起点" if which == "from" else "终点"

        def done(lat, lon):
            var.set(f"{lat:.6f},{lon:.6f}")
            self._last_map_center = (lat, lon)
            self._log_tag(f"{label}已选定: {lat:.6f},{lon:.6f}", "ok")

        try:
            mappicker.MapPicker(self.win, init[0], init[1], done,
                            amap_key=self._amap_key(), city=self._amap_city())
        except Exception as e:                                       # noqa: BLE001
            self._log_tag(f"[失败] 打不开地图: {type(e).__name__}: {e}", "err")
            return
        self._log_tag(f"地图选点窗口已打开（选{label}）："
                      "单击定位、双击确认、拖动平移、滚轮缩放。", "ok")

    def act_device_location(self):
        """读设备位置填进起点。Android 10+ 会打码，读不到时给出替代方案。"""
        adb = TC / "android-sdk" / "platform-tools" / "adb.exe"
        if not adb.exists():
            messagebox.showwarning("提示", "找不到 adb，先跑 scripts/setup-android.ps1")
            return
        self._log_tag("正在读设备位置…", "ok")

        def work():
            try:
                r = subprocess.run([str(adb), "shell", "dumpsys", "location"],
                                   capture_output=True, text=True, encoding="utf-8",
                                   errors="replace", env=self.runner.base_env(),
                                   timeout=40)
                info = loc.parse_dumpsys_location(r.stdout)
                if info["real"]:
                    self.q.put(("loc", (info["real"].lat, info["real"].lon,
                                        f"设备真实位置（{info['real'].provider}）")))
                elif info["mock"]:
                    self.q.put(("loc", (info["mock"].lat, info["mock"].lon,
                                        "设备当前是 MockRoute 的虚拟位置")))
                elif info["redacted"]:
                    self.q.put(("warn", "设备对 dumpsys location 做了隐私打码，读不到明文坐标。"
                                        "改用「地图选起点」，或手机地图长按取坐标。"))
                else:
                    self.q.put(("warn", "没有解析到设备位置。"))
            except Exception as e:                                   # noqa: BLE001
                self.q.put(("warn", f"读取设备位置失败: {type(e).__name__}: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def act_save_alias(self):
        """把当前起点存成命名地点。"""
        c = loc.parse_coord_literal(self.var_from.get())
        if not c:
            messagebox.showinfo("提示", "起点当前不是坐标。\n先「地图选起点」或填 纬度,经度，再保存。")
            return
        name = simpledialog.askstring("存为命名地点", "给这个位置起个名字：", parent=self.win)
        if not name or not name.strip():
            return
        try:
            p = loc.save_alias(ROOT, name.strip(), c[0], c[1], note="控制台保存")
        except Exception as e:                                       # noqa: BLE001
            messagebox.showerror("失败", f"保存失败：{e}")
            return
        self.refresh_aliases()
        self.var_alias.set(name.strip())
        self._log_tag(f"已保存命名地点 {name!r} = {c[0]:.6f},{c[1]:.6f} -> {p}", "ok")

    def act_show_device_logs(self):
        """读设备上的持久化运行日志（App 自己写的，不是 logcat）。"""
        args = ["logs", "show", "--tail", "120"]
        tag = (self.var_logtag.get() or "").strip()
        if tag:
            args += ["--tag", tag]
        self._mroute(*args, title="查看设备日志")

    def act_pull_device_logs(self):
        self._mroute("logs", "pull", "--out", "out/device-mockroute.log",
                     title="导出设备日志")

    # --- 查看 / 更改选点 ---

    def _route_points(self) -> list:
        """把当前选路页的 from/via/to 读成 [(lat, lon), ...]（只认坐标字面量）。"""
        pts = []
        c = loc.parse_coord_literal(self.var_r_from.get())
        if c:
            pts.append(c)
        for v in self.var_r_via.get().split(";"):
            if v.strip():
                cv = loc.parse_coord_literal(v)
                if cv:
                    pts.append(cv)
        c = loc.parse_coord_literal(self.var_r_to.get())
        if c:
            pts.append(c)
        return pts

    def _render_route_view(self, pts=None):
        """把选点清单渲染成「起点 / 途经 / 终点」的多行文本。"""
        pts = pts if pts is not None else self._route_points()
        if not pts:
            self.var_route_view.set("（还没选点）")
            return
        lines = []
        for i, (lat, lon) in enumerate(pts):
            if i == 0:
                tag = "起点  "
            elif i == len(pts) - 1:
                tag = "终点  "
            else:
                tag = f"途经{i}"
            lines.append(f"{tag} {lat:.6f},{lon:.6f}")
        self.var_route_view.set("\n".join(lines))
        self.var_route_count.set(f"{len(pts)} 个点（途经 {max(0, len(pts) - 2)} 个）")

    def act_route_view(self):
        """只查看当前选点，不改动任何东西。"""
        pts = self._route_points()
        self._render_route_view(pts)
        if not pts:
            messagebox.showinfo(
                "还没有选点",
                "点「打开地图选路」在地图上点出起点/途经点/终点，\n"
                "或者直接在下面各栏填 纬度,经度。")
            return
        self._log_tag(f"当前选点 {len(pts)} 个（未做改动）", "ok")

    def act_route_edit(self):
        """打开地图更改选点 —— **必须点这个按钮**才会进入编辑。

        为什么要单独一个按钮：选点花时间调好的东西，不该因为手滑点到
        地图就变了。所以查看和更改分开，更改走这个显式入口。
        """
        pts = self._route_points()
        if len(pts) < 1:
            messagebox.showinfo(
                "还没有选点可改",
                "先用「打开地图选路」点出路线，\n"
                "或者直接在下面各栏填 纬度,经度，再来更改。")
            return
        if not messagebox.askyesno(
                "确认更改选点",
                f"当前有 {len(pts)} 个点。\n\n"
                "接下来会打开地图，你可以在里面\n"
                "  · 拖动点、删除点、加新点\n"
                "  · 用「上移/下移」调整顺序\n"
                "确定后才会替换现在的选点。\n\n要开始更改吗？"):
            return

        lats = [p[0] for p in pts]
        lons = [p[1] for p in pts]
        center = (sum(lats) / len(lats), sum(lons) / len(lons))

        def done(new_pts):
            if len(new_pts) < 2:
                messagebox.showwarning("点太少", "至少要有起点和终点。")
                return
            # 同 act_route_pick：更改后的选点也要存下来，绕圈对话框靠它估路程
            self._picked_points = [(float(a), float(b)) for a, b in new_pts]
            self.var_r_from.set(f"{new_pts[0][0]:.6f},{new_pts[0][1]:.6f}")
            self.var_r_to.set(f"{new_pts[-1][0]:.6f},{new_pts[-1][1]:.6f}")
            vias = [f"{a:.6f},{b:.6f}" for a, b in new_pts[1:-1]]
            self.var_r_via.set(";".join(vias))
            self._render_route_view(new_pts)
            self._last_map_center = new_pts[0]
            self._log_tag(f"选点已更改：{len(new_pts)} 个点"
                          f"（途经 {len(vias)} 个）", "ok")

        try:
            mappicker.RoutePicker(self.win, center[0], center[1], done,
                                  self._save_alias_from_picker,
                                  aliases=loc.load_aliases(ROOT),
                                  amap_key=self._amap_key(),
                                  city=self._amap_city(),
                                  initial_points=pts)
        except Exception as e:                                       # noqa: BLE001
            self._log_tag(f"[失败] 打不开编辑地图: {type(e).__name__}: {e}", "err")
            return
        self._log_tag(f"编辑模式：已载入 {len(pts)} 个点。"
                      "单击加点、选中列表项可删除、拖动平移、滚轮缩放。", "ok")

    def _save_alias_from_picker(self, name, lat, lon):
        loc.save_alias(ROOT, name, lat, lon, note="多点选路保存")
        self.refresh_aliases()

    def act_tail_logs(self):
        """adb logcat 会一直输出，所以单独开一个控制台窗口跑，不占用日志区。"""
        adb = TC / "android-sdk" / "platform-tools" / "adb.exe"
        try:
            subprocess.Popen(
                ["cmd", "/c", "start", "MockRoute 实时日志",
                 str(adb), "logcat", "-s", "MockRoute:I"],
                cwd=str(ROOT), env=self.runner.base_env())
            self._log_tag("已另开窗口显示实时日志（关闭那个窗口即停止）。", "ok")
        except Exception as e:                                       # noqa: BLE001
            self._log_tag(f"[失败] 无法打开日志窗口: {e}", "err")

    def act_rebuild(self):
        if not messagebox.askyesno("确认", "重新编译 APK 并安装？\n大约需要 1~2 分钟。"):
            return
        self.runner.start(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
                           "-File", str(SCRIPTS / "build-android.ps1"),
                           "-Install", "-Launch"], "编译并安装 APK")

    # --- 杂项 ---

    def _open_path(self, p: Path):
        if not p.exists():
            self._log_tag(f"[!] 文件不存在: {p}", "warn")
            return
        try:
            os.startfile(str(p))                                     # noqa: S606
            self._log_tag(f"已用系统默认程序打开 {p.name}", "ok")
        except Exception as e:                                       # noqa: BLE001
            self._log_tag(f"[失败] 打开失败: {e}\n  可以手动打开: {p}", "err")


def main() -> int:
    root = tk.Tk()
    Console(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    raise SystemExit(main())
