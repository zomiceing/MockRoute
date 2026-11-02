package com.mockroute

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.location.Criteria
import android.location.LocationManager
import android.os.Build
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import android.os.SystemClock
import androidx.core.app.NotificationCompat
import java.io.File
import kotlin.math.max
import kotlin.math.roundToLong

/**
 * 真正写入 mock 定位的服务。
 *
 * 支持四种工作方式：
 *  - 单点：一直重复同一个坐标（原地模拟）
 *  - 轨迹：按 route.json 里的时间戳回放，支持 once / loop / pingpong
 *  - 圆圈：绕一个中心点画圆（半径/周期可调）
 *  - 前往：从某点直线走到目标点（可与轨迹互相切换）
 */
class MockLocationService : Service() {

    companion object {
        const val NOTIF_ID = 1001
        const val CHANNEL_ID = "mockroute"

        /** 心跳间隔。日志里出现远超它的空洞 = 进程当时没被调度（被冻结/杀掉）。 */
        const val HEARTBEAT_MS = 30_000L

        fun start(ctx: Context, action: String, extras: Map<String, Any?>) {
            val i = Intent(ctx, MockLocationService::class.java).setAction(action)
            extras.forEach { (k, v) ->
                when (v) {
                    null -> Unit
                    is Int -> i.putExtra(k, v)
                    is Long -> i.putExtra(k, v)
                    is Float -> i.putExtra(k, v)
                    is Double -> i.putExtra(k, v)
                    is Boolean -> i.putExtra(k, v)
                    else -> i.putExtra(k, v.toString())
                }
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) ctx.startForegroundService(i)
            else ctx.startService(i)
        }
    }

    private lateinit var lm: LocationManager
    private val main = Handler(Looper.getMainLooper())
    private var providers: List<String> = emptyList()
    private var lastLocation: android.location.Location? = null
    private var elapsedNanosBase = 0L
    private var lastBeat = 0L
    private var lastProvCheck = 0L   // provider 健康检查的上次时间

    // 播放状态
    private var route: Route? = null
    private var mode: RouteMode = RouteMode.ONCE
    private var idx = 0
    private var dir = 1
    private var mult = 1.0f
    private var paused = false
    private var playing = false

    // 圆圈 / 前往 用
    private var circle: DoubleArray? = null // [lat, lon, radius, periodMs]
    private var circleT0 = 0L
    private var leg: Leg? = null

    private data class Leg(
        val fromLat: Double, val fromLon: Double,
        val toLat: Double, val toLon: Double,
        val startAt: Long, val durationMs: Long, val speed: Float
    )

    // ---------------- extra 读取（容忍 float / double 两种类型） ----------------
    //
    // Android 的 TypedValue 把 FLOAT(4) 和 DOUBLE(5) 当作不同类型：
    // Bundle.getDoubleExtra() 读一个 float extra 会直接返回默认值，反之亦然。
    // 而 `am broadcast --ef/--ed` 决定了发的是哪种，广播本身却总是"成功"，
    // 所以类型不匹配时表现为设备端报「缺少 lat/lon」而 PC 端毫无察觉。
    // 这里两种都读，谁在就读谁 —— 无论调用方用 --ef 还是 --ed 都能工作。

    private fun dbl(intent: Intent, key: String, def: Double): Double {
        val v = intent.extras?.get(key) ?: return def
        return when (v) {
            is Double -> v
            is Float -> v.toDouble()
            is Number -> v.toDouble()
            is String -> v.toDoubleOrNull() ?: def
            else -> def
        }
    }

    private fun flt(intent: Intent, key: String, def: Float): Float {
        val v = intent.extras?.get(key) ?: return def
        return when (v) {
            is Float -> v
            is Double -> v.toFloat()
            is Number -> v.toFloat()
            is String -> v.toFloatOrNull() ?: def
            else -> def
        }
    }

    private fun isMissing(v: Double) = v.isNaN()

    private val tick = object : Runnable {
        override fun run() {
            // 心跳：每 HEARTBEAT_MS 记一条当前位置/速度。
            // 这是判断「模拟是不是被系统冻结/打断了」的关键 ——
            // 日志里出现远超心跳间隔的时间空洞，就说明进程当时没被调度。
            val nowUp = SystemClock.elapsedRealtime()
            if (playing && nowUp - lastBeat >= HEARTBEAT_MS) {
                lastBeat = nowUp
                val cur = lastLocation
                LogFile.append(
                    this@MockLocationService, "BEAT",
                    "playing=$playing paused=$paused idx=$idx/" +
                            "${route?.points?.size ?: 0} " +
                            (cur?.let { "pos=%.6f,%.6f v=%.2f".format(it.latitude, it.longitude, it.speed) }
                                ?: "pos=无") +
                            " circle=${circle != null} goto=${leg != null}"
                )
            }
            // provider 健康检查放在 ACTION_STATUS 分支里（面板会定时查状态，
            // 等于随查随巡检）。这里不再重复，避免两处逻辑分叉。
            if (circle != null) {
                stepCircle()
                main.postDelayed(this, 500L)
                return
            }
            val lg = leg
            if (lg != null) {
                if (stepLeg(lg)) {
                    leg = null
                    Status.set(this@MockLocationService, "前往完成，已停在目标点")
                Status.setState(this@MockLocationService, Protocol.STATE_OFF)
                    return
                }
                main.postDelayed(this, 500L)
                return
            }
            val r = route
            if (r == null || !playing) return
            if (paused) {
                main.postDelayed(this, 250L)
                return
            }
            stepRoute(r)
        }
    }

    override fun onCreate() {
        super.onCreate()
        lm = getSystemService(Context.LOCATION_SERVICE) as LocationManager
        startForegroundCompat()
        LogFile.append(this, "LIFE", "onCreate 服务创建")
        // 进程刚起来时注册一次。providers 是内存字段，新进程必然是空的，
        // 但显式调用能把注册结果写进日志，方便事后判断。
        ensureReady()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        LogFile.append(this, "CMD", "onStartCommand action=${intent?.action} startId=$startId")
        // 每条命令前都确认 provider 可用。
        //
        // 为什么必须做：测试 provider 会被框架移除（进程被回收、被别的应用抢注），
        // 而内存里的 providers 仍非空 —— 于是 ensureReady 的
        // `if (providers.isNotEmpty()) return` 永远拦住重建，setTestProviderLocation
        // 就会一直报 Provider "gps"/"network" unknown。
        // 实测过一次：876 对失败把日志刷爆，一直播到用户手动 stop。
        verifyProviders()
        try {
            handle(intent)
        } catch (t: Throwable) {
            LogFile.append(this, "ERR", "命令失败: ${t.javaClass.simpleName}: ${t.message}")
            Status.set(this, "命令失败: ${t.javaClass.simpleName}: ${t.message}")
        }
        // 用 START_STICKY 而不是 START_REDELIVER_INTENT：服务被系统回收后重启时
        // 不应该重放上一条 START_ROUTE 命令，否则轨迹会莫名从头再来一遍。
        return START_STICKY
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onTaskRemoved(rootIntent: Intent?) {
        // 从最近任务里划掉应用会走到这里。记录下来，方便事后判断
        // 「模拟是正常关的，还是被划掉/被系统清的」。
        LogFile.append(this, "LIFE", "onTaskRemoved（应用被从最近任务划掉）")
        super.onTaskRemoved(rootIntent)
    }

    override fun onDestroy() {
        LogFile.append(this, "LIFE", "onDestroy 服务销毁，开始清理")
        main.removeCallbacksAndMessages(null)
        teardownProviders()
        Status.set(this, "服务已停止，mock 已移除")
        Status.setState(this, Protocol.STATE_OFF)
        LogFile.append(this, "LIFE", "onDestroy 清理完成")
        super.onDestroy()
    }

    // ---------------- 命令分发 ----------------

    private fun handle(intent: Intent?) {
        val a = intent?.action ?: return
        when (a) {
            Protocol.ACTION_SET -> {
                stopPlayback()
                val lat = dbl(intent, Protocol.EXTRA_LAT, Double.NaN)
                val lon = dbl(intent, Protocol.EXTRA_LON, Double.NaN)
                if (lat.isNaN() || lon.isNaN()) {
                    Status.set(this, "SET 缺少 lat/lon")
                    return
                }
                val p = Point(
                    t = 0, lat = lat, lon = lon,
                    speed = flt(intent, Protocol.EXTRA_SPEED, 0f),
                    bearing = flt(intent, Protocol.EXTRA_BEARING, 0f),
                    accuracy = flt(intent, Protocol.EXTRA_ACC, 8f),
                    altitude = dbl(intent, Protocol.EXTRA_ALT, 0.0),
                )
                ensureReady()
                push(p)
                // 持续重发同一点：很多 App 在 5s 无更新后会判定定位失效
                route = Route(listOf(p, p.copy(t = 1L shl 40)), RouteMode.LOOP, "single")
                mode = RouteMode.LOOP
                idx = 0
                playing = true
                paused = false
                Status.set(this, "单点: %.6f,%.6f".format(lat, lon))
                Status.setState(this, Protocol.STATE_ON)
                main.removeCallbacks(tick)
                main.postDelayed(tick, 1000L)
            }

            Protocol.ACTION_START_ROUTE -> {
                stopPlayback()
                val f = resolveFile(intent.getStringExtra(Protocol.EXTRA_FILE))
                val overrideMode = intent.getStringExtra(Protocol.EXTRA_MODE)?.let { RouteMode.parse(it) }
                val r = RouteParser.parse(f, overrideMode)
                ensureReady()
                route = r
                mode = r.mode
                idx = 0
                dir = 1
                playing = true
                paused = false
                Status.set(
                    this,
                    "轨迹: ${f.name} 共 ${r.points.size} 点, ${r.durationMs / 1000}s, 模式=$mode, 倍速=$mult"
                )
                Status.setState(this, Protocol.STATE_ON)
                main.removeCallbacks(tick)
                // 起点立刻生效，避免脚本按下后还有 1s 空白
                val first = r.points.first()
                lastLocation = push(first)
                main.postDelayed(tick, waitFor(r, 0, 1))
            }

            Protocol.ACTION_START_CIRCLE -> {
                stopPlayback()
                ensureReady()
                val lat = dbl(intent, Protocol.EXTRA_LAT, Double.NaN)
                val lon = dbl(intent, Protocol.EXTRA_LON, Double.NaN)
                if (lat.isNaN() || lon.isNaN()) {
                    Status.set(this, "START_CIRCLE 缺少 lat/lon")
                    return
                }
                val radius = dbl(intent, Protocol.EXTRA_RADIUS, 100.0)
                val period = intent.getLongExtra(Protocol.EXTRA_PERIOD, 60_000L).coerceAtLeast(2000L)
                circle = doubleArrayOf(lat, lon, radius, period.toDouble())
                circleT0 = SystemClock.elapsedRealtime()
                playing = true
                Status.set(this, "圆圈: 中心 %.6f,%.6f 半径 %.0fm 周期 %ds".format(lat, lon, radius, period / 1000))
                Status.setState(this, Protocol.STATE_ON)
                main.removeCallbacks(tick)
                main.post(tick)
            }

            Protocol.ACTION_GOTO -> {
                // 从当前位置直线前往目标，不打断轨迹定义
                ensureReady()
                val lat = dbl(intent, Protocol.EXTRA_LAT, Double.NaN)
                val lon = dbl(intent, Protocol.EXTRA_LON, Double.NaN)
                if (lat.isNaN() || lon.isNaN()) {
                    Status.set(this, "GOTO 缺少 lat/lon")
                    return
                }
                val speed = flt(intent, Protocol.EXTRA_SPEED, 1.4f).coerceAtLeast(0.1f)
                val cur = lastLocation
                val fromLat = cur?.latitude ?: lat
                val fromLon = cur?.longitude ?: lon
                val dist = Geo.distanceMeters(fromLat, fromLon, lat, lon)
                val dur = max(1000L, ((dist / speed) * 1000.0).toLong())
                circle = null
                route = null
                leg = Leg(fromLat, fromLon, lat, lon, SystemClock.elapsedRealtime(), dur, speed)
                playing = true
                paused = false
                Status.set(this, "前往 %.6f,%.6f (%.0fm @ %.1fm/s ≈ %ds)".format(lat, lon, dist, speed, dur / 1000))
                Status.setState(this, Protocol.STATE_ON)
                main.removeCallbacks(tick)
                main.post(tick)
            }

            Protocol.ACTION_PAUSE -> {
                paused = true
                main.removeCallbacks(tick)
                Status.set(this, "已暂停")
                Status.setState(this, Protocol.STATE_PAUSED)
            }

            Protocol.ACTION_RESUME -> {
                if (playing && paused) {
                    paused = false
                    Status.set(this, "已恢复")
                    Status.setState(this, Protocol.STATE_ON)
                    main.removeCallbacks(tick)
                    main.post(tick)
                }
            }

            Protocol.ACTION_SPEED -> {
                mult = flt(intent, Protocol.EXTRA_MULT, 1f).coerceIn(0.05f, 50f)
                Status.set(this, "倍速=$mult")
                // 倍速只影响播放节奏，不修改坐标里的 speed 字段；
                // 需要「坐标速度值也一致」时用 PC 端生成不同速度的轨迹。
            }

            Protocol.ACTION_STOP -> {
                stopPlayback()
                Status.set(this, "已停止")
                Status.setState(this, Protocol.STATE_OFF)
            }

            Protocol.ACTION_STATUS -> {
                // 顺手做一次 provider 健康检查。
                //
                // 面板会定时查状态，所以放在这里等于「随查随巡检」——
                // 不必额外起定时器，也不会在待机时漏掉 provider 失效。
                val nowUp = SystemClock.elapsedRealtime()
                if (nowUp - lastProvCheck >= 10_000L) {
                    lastProvCheck = nowUp
                    var dead = providers.isEmpty()
                    for (p in providers) {
                        try {
                            lm.getTestProviderEnabled(p)
                        } catch (_: Throwable) {
                            dead = true
                            break
                        }
                    }
                    if (dead) {
                        LogFile.append(this, "ERR", "状态查询发现 provider 失效，重建注册")
                        providers = emptyList()
                        ensureReady()
                    }
                }
                val r = route
                val cur = lastLocation
                Status.set(
                    this,
                    "状态: playing=$playing paused=$paused mode=$mode idx=$idx/${r?.points?.size ?: 0} " +
                            "mult=$mult circle=${circle != null} goto=${leg != null} " +
                            "last=" + (cur?.let { "%.6f,%.6f v=%.2f".format(it.latitude, it.longitude, it.speed) } ?: "无")
                )
            }
        }
    }

    private fun resolveFile(name: String?): File {
        if (name.isNullOrBlank()) return Protocol.defaultRouteFile(this)
        val f = File(name)
        return if (f.isAbsolute) f else File(Protocol.appDir(this), name)
    }

    // ---------------- 播放 ----------------

    private fun stepRoute(r: Route) {
        if (r.points.size < 2) {
            playing = false
            return
        }
        var nextIdx = idx + dir
        if (nextIdx !in r.points.indices) {
            when (mode) {
                RouteMode.ONCE -> {
                    playing = false
                    push(r.points[idx])
                    Status.set(this, "轨迹播放完成（共 ${r.points.size} 点）")
                    Status.setState(this, Protocol.STATE_OFF)
                    return
                }
                RouteMode.LOOP -> {
                    idx = 0
                    push(r.points[0])
                    main.postDelayed(tick, waitFor(r, 0, 1))
                    return
                }
                RouteMode.PINGPONG -> {
                    dir = -dir
                    nextIdx = idx + dir
                }
            }
        }
        val p = r.points[nextIdx]
        val prev = r.points[idx]
        val deltaMs = kotlin.math.abs(p.t - prev.t)
        idx = nextIdx
        push(p)
        main.postDelayed(tick, (deltaMs / mult).roundToLong().coerceAtLeast(1L))
    }

    /** 第一个点与后续点之间的等待时间 */
    private fun waitFor(r: Route, from: Int, to: Int): Long {
        if (to !in r.points.indices) return 1000L
        val d = kotlin.math.abs(r.points[to].t - r.points[from].t)
        return (d / mult).roundToLong().coerceAtLeast(16L)
    }

    private fun stepCircle() {
        val c = circle ?: return
        val phase = ((SystemClock.elapsedRealtime() - circleT0) % c[3].toLong()).toDouble() / c[3]
        val bearing = phase * 360.0
        val o = Geo.offset(c[0], c[1], bearing, c[2])
        // 圆周线速度 = 2πr / T
        val v = (2 * Math.PI * c[2] / (c[3] / 1000.0)).toFloat()
        val tangential = (bearing + 90.0) % 360.0
        push(
            Point(
                t = 0, lat = o[0], lon = o[1], speed = v,
                bearing = tangential.toFloat(), accuracy = 6f, altitude = 0.0
            )
        )
    }

    /** @return true 表示已到达 */
    private fun stepLeg(lg: Leg): Boolean {
        val elapsed = SystemClock.elapsedRealtime() - lg.startAt
        val f = (elapsed.toDouble() / lg.durationMs).coerceIn(0.0, 1.0)
        val lat = lg.fromLat + (lg.toLat - lg.fromLat) * f
        val lon = lg.fromLon + (lg.toLon - lg.fromLon) * f
        val bearing = Geo.bearingDeg(lg.fromLat, lg.fromLon, lg.toLat, lg.toLon)
        push(
            Point(
                t = 0, lat = lat, lon = lon, speed = lg.speed,
                bearing = bearing.toFloat(), accuracy = 8f, altitude = 0.0
            )
        )
        return f >= 1.0
    }

    private fun stopPlayback() {
        LogFile.append(this, "PLAY", "stopPlayback（idx=$idx 时停止）")
        playing = false
        paused = false
        circle = null
        leg = null
        route = null
        idx = 0
        dir = 1
        main.removeCallbacks(tick)
    }

    // ---------------- mock provider ----------------

    /**
     * 检查内存里记录的 provider 在框架里是否**真的还在**，不在就重建。
     *
     * 为什么需要：`providers` 只是本进程的内存字段，而测试 provider 的注册状态
     * 归框架管。框架移除 provider 的路径不止 `teardownProviders()` 一条
     * （进程被回收后框架清掉、被其他应用抢注等）。一旦框架侧没了而内存里还有，
     * `ensureReady()` 开头的 `if (providers.isNotEmpty()) return` 就会永远拦住
     * 重建，之后每次写入都报 `Provider "gps"/"network" unknown` ——
     * 实测过一次 876 对失败把日志刷爆，一直播到用户手动 stop。
     *
     * 探测手段：`getTestProviderEnabled()` 对不存在的 provider 会抛异常。
     */
    private fun verifyProviders() {
        if (providers.isEmpty()) {
            ensureReady()
            return
        }
        var dead = false
        for (p in providers) {
            try {
                lm.getTestProviderEnabled(p)
            } catch (_: Throwable) {
                dead = true
                break
            }
        }
        if (dead) {
            LogFile.append(this, "ERR", "provider 已失效（${providers}），重新注册")
            providers = emptyList()
            ensureReady()
        }
    }

    private fun ensureReady() {
        if (providers.isNotEmpty()) return
        val want = buildList {
            add(LocationManager.GPS_PROVIDER)
            add(LocationManager.NETWORK_PROVIDER)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
                add(LocationManager.FUSED_PROVIDER)
            }
        }
        val ok = ArrayList<String>()
        val bad = ArrayList<String>()
        for (p in want) {
            try {
                addProvider(p)
                ok.add(p)
            } catch (t: SecurityException) {
                // 未被设为「模拟位置信息应用」时会走到这里
                bad.add("$p(SecurityException)")
            } catch (t: Throwable) {
                // addProvider 内部先 removeTestProvider，所以“已存在”不该是问题；
                // 真到这里说明成因不明，记下类名即可。
                bad.add("$p(${t.javaClass.simpleName})")
            }
        }
        providers = ok
        Status.set(
            this,
            "mock provider 就绪: $ok" + if (bad.isEmpty()) "" else "  失败: $bad"
        )
        if (ok.isEmpty()) {
            playing = false
            Status.set(
                this,
                "无法注册任何 mock provider。请在开发者选项中把 MockRoute 设为「模拟位置信息应用」，" +
                        "或执行: adb shell appops set com.mockroute android:mock_location allow"
            )
        }
    }

    private fun addProvider(name: String) {
        try {
            lm.removeTestProvider(name)
        } catch (_: Throwable) {
            // 之前没加过，忽略
        }
        @Suppress("DEPRECATION")
        lm.addTestProvider(
            name, false, true, false, false, true, true, true,
            Criteria.POWER_LOW, Criteria.ACCURACY_FINE
        )
        lm.setTestProviderEnabled(name, true)
    }

    private fun teardownProviders() {
        for (p in providers) {
            // 两步各自容错。原来两步放在同一个 try 里 —— 如果
            // setTestProviderEnabled 抛异常，removeTestProvider 就被跳过，
            // provider 实际没被移除，下次重建时 addTestProvider 会失败。
            try {
                lm.setTestProviderEnabled(p, false)
            } catch (_: Throwable) {
            }
            try {
                lm.removeTestProvider(p)
            } catch (_: Throwable) {
            }
        }
        providers = emptyList()
    }

    private fun push(p: Point): android.location.Location {
        if (providers.isEmpty()) ensureReady()
        if (providers.isEmpty()) return p.toLocation(LocationManager.GPS_PROVIDER)
        var last: android.location.Location? = null
        var unknown = false
        for (name in providers) {
            val loc = p.toLocation(name)
            // elapsedRealtimeNanos 必须单调递增，否则部分 App 会丢弃该次更新。
            // 注意不要再去写 loc.elapsedRealtime：它在公开 SDK 里没有可见的 setter
            // （会编译报 Unresolved reference），而且框架本来就会从 nanos 推导出它。
            elapsedNanosBase += 1_000_000L
            @Suppress("DEPRECATION")
            loc.elapsedRealtimeNanos = SystemClock.elapsedRealtimeNanos() + elapsedNanosBase
            try {
                lm.setTestProviderLocation(name, loc)
                last = loc
            } catch (t: Throwable) {
                val msg = t.message ?: ""
                // 「Provider unknown」= 测试 provider 已经被框架移除（进程重启、
                // 系统回收、或被别的应用抢注），而我们内存里的 providers 还以为是好的。
                // 这种情况**必须重建 provider**，光记日志没用 —— 之前就是这样：
                // 每 30ms 一对失败、刷出上千行日志，一直播到用户手动 stop 为止。
                if (msg.contains("unknown", ignoreCase = true)) {
                    unknown = true
                } else {
                    logThrottled("写入 $name 失败: $msg")
                }
            }
        }
        if (unknown) {
            // 重建一次，并在本次重试写入，避免白白丢掉这个点
            Status.set(this, "测试 provider 失效，正在重建…")
            providers = emptyList()
            ensureReady()
            for (name in providers) {
                try {
                    lm.setTestProviderLocation(name, p.toLocation(name))
                    last = p.toLocation(name)
                } catch (_: Throwable) {
                }
            }
            if (providers.isNotEmpty() && last == null) {
                logThrottled("重建后仍写入失败")
            }
        }
        lastLocation = last ?: p.toLocation(LocationManager.GPS_PROVIDER)
        return lastLocation!!
    }

    /** 限流日志：同一类消息每 10 秒最多记一条，避免把日志刷爆（会触发轮转）。 */
    private var lastThrottleAt = 0L
    private var lastThrottleMsg = ""

    private fun logThrottled(msg: String) {
        val now = SystemClock.elapsedRealtime()
        if (msg == lastThrottleMsg && now - lastThrottleAt < 10_000L) return
        lastThrottleAt = now
        lastThrottleMsg = msg
        Status.set(this, msg)
    }

    // ---------------- 前台通知 ----------------

    private fun startForegroundCompat() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val nm = getSystemService(NotificationManager::class.java)
            if (nm.getNotificationChannel(CHANNEL_ID) == null) {
                nm.createNotificationChannel(
                    NotificationChannel(CHANNEL_ID, "MockRoute", NotificationManager.IMPORTANCE_MIN)
                )
            }
        }
        val pi = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val n: Notification = NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("MockRoute 正在模拟定位")
            .setContentText("由 PC 脚本控制，点按打开面板")
            .setSmallIcon(android.R.drawable.ic_menu_mylocation)
            .setOngoing(true)
            .setContentIntent(pi)
            .build()
        @Suppress("DEPRECATION")
        startForeground(NOTIF_ID, n)
    }
}
