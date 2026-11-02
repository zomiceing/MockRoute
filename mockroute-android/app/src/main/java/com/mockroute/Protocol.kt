package com.mockroute

import android.content.Context
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/** 与 PC 端脚本共享的常量。改这里就必须同步改 tools/mockroute/protocol.py。 */
object Protocol {
    const val TAG = "MockRoute"

    const val ACTION_SET = "com.mockroute.action.SET"
    const val ACTION_START = "com.mockroute.action.START"
    const val ACTION_START_ROUTE = "com.mockroute.action.START_ROUTE"
    const val ACTION_START_CIRCLE = "com.mockroute.action.START_CIRCLE"
    const val ACTION_PAUSE = "com.mockroute.action.PAUSE"
    const val ACTION_RESUME = "com.mockroute.action.RESUME"
    const val ACTION_STOP = "com.mockroute.action.STOP"
    const val ACTION_STATUS = "com.mockroute.action.STATUS"
    const val ACTION_GOTO = "com.mockroute.action.GOTO"
    const val ACTION_SPEED = "com.mockroute.action.SPEED"

    const val EXTRA_LAT = "lat"
    const val EXTRA_LON = "lon"
    const val EXTRA_SPEED = "speed"
    const val EXTRA_BEARING = "bearing"
    const val EXTRA_ACC = "acc"
    const val EXTRA_ALT = "alt"
    const val EXTRA_FILE = "file"
    const val EXTRA_MODE = "mode"
    const val EXTRA_INTERVAL = "interval"
    const val EXTRA_MULT = "mult"
    const val EXTRA_RADIUS = "radius"
    const val EXTRA_PERIOD = "period"
    const val EXTRA_TAG = "tag"

    const val PREF = "mockroute_ui"
    const val KEY_STATUS = "status"
    const val KEY_UPDATED = "updated"
    /** 明确的开关状态。界面靠它判断，不要去猜 KEY_STATUS 的文案。 */
    const val KEY_STATE = "state"

    /** 开关状态取值。与 tools/mockroute/protocol.py 里的 STATE_* 一一对应。 */
    const val STATE_OFF = "off"
    const val STATE_ON = "on"
    const val STATE_PAUSED = "paused"

    /** 应用私有外部目录：/sdcard/Android/data/com.mockroute/files/，PC 推送无需任何权限。 */
    fun appDir(ctx: Context): File = ctx.getExternalFilesDir(null) ?: ctx.filesDir

    /** 默认轨迹路径 */
    fun defaultRouteFile(ctx: Context): File = File(appDir(ctx), "route.json")
}

/** 解析 route.json / route.csv，返回 [Route]。 */
object RouteParser {

    fun parse(file: File, overrideMode: RouteMode? = null): Route {
        if (!file.exists()) throw IllegalArgumentException("轨迹文件不存在: ${file.absolutePath}")
        val text = file.readText(Charsets.UTF_8)
        val route = if (file.name.endsWith(".csv", ignoreCase = true)) parseCsv(text, file.name)
        else parseJson(text, file.name)
        // 广播里的 mode 优先于文件里写的 mode
        return if (overrideMode != null) route.copy(mode = overrideMode) else route
    }

    private fun parseJson(text: String, name: String): Route {
        val root: JSONObject = if (text.trimStart().startsWith("[")) {
            JSONObject().put("points", JSONArray(text))
        } else {
            JSONObject(text)
        }
        val arr = root.optJSONArray("points")
            ?: throw IllegalArgumentException("$name 缺少 points 数组")
        if (arr.length() == 0) throw IllegalArgumentException("$name 的 points 为空")

        val points = ArrayList<Point>(arr.length())
        var prevT = -1L
        for (i in 0 until arr.length()) {
            val o = arr.optJSONObject(i)
                ?: throw IllegalArgumentException("$name 第 $i 个点不是对象")
            val lat = o.getDouble("lat")
            val lon = o.getDouble("lon")
            var t = o.optLong("t", -1L)
            if (t < 0) {
                // 没给 t 就按 interval 顺推，容忍手工编辑的极简文件
                val interval = root.optLong("interval", 1000L)
                t = if (prevT < 0) 0L else prevT + interval
            }
            if (t < prevT) throw IllegalArgumentException("$name 第 $i 个点的 t 回退了（必须单调不减）")
            prevT = t
            points.add(
                Point(
                    t = t,
                    lon = lon,
                    lat = lat,
                    speed = o.optDouble("speed", 0.0).toFloat(),
                    bearing = o.optDouble("bearing", 0.0).toFloat(),
                    accuracy = o.optDouble("acc", 8.0).toFloat(),
                    altitude = o.optDouble("alt", 0.0),
                )
            )
        }
        fillMissing(points)
        return Route(points, RouteMode.parse(root.optString("mode", "once")), name)
    }

    /** CSV：t,lon,lat,speed,bearing,acc,alt —— 首行可以是表头。 */
    private fun parseCsv(text: String, name: String): Route {
        val points = ArrayList<Point>()
        var prevT = -1L
        text.lineSequence().forEachIndexed { idx, raw ->
            val line = raw.trim()
            if (line.isEmpty() || line.startsWith("#")) return@forEachIndexed
            val cols = line.split(',', ';', '\t').map { it.trim() }
            if (cols.size < 3) {
                if (idx == 0) return@forEachIndexed else throw IllegalArgumentException("$name 第 $idx 行字段不足")
            }
            if (idx == 0 && cols[0].toLongOrNull() == null) return@forEachIndexed // 表头
            val t = cols[0].toLongOrNull() ?: (if (prevT < 0) 0L else prevT + 1000L)
            val lon = cols[1].toDoubleOrNull() ?: throw IllegalArgumentException("$name 第 $idx 行经度无法解析")
            val lat = cols[2].toDoubleOrNull() ?: throw IllegalArgumentException("$name 第 $idx 行纬度无法解析")
            prevT = t
            points.add(
                Point(
                    t = t, lon = lon, lat = lat,
                    speed = cols.getOrNull(3)?.toFloatOrNull() ?: 0f,
                    bearing = cols.getOrNull(4)?.toFloatOrNull() ?: 0f,
                    accuracy = cols.getOrNull(5)?.toFloatOrNull() ?: 8f,
                    altitude = cols.getOrNull(6)?.toDoubleOrNull() ?: 0.0,
                )
            )
        }
        if (points.isEmpty()) throw IllegalArgumentException("$name 没有解析出任何点")
        fillMissing(points)
        return Route(points, RouteMode.ONCE, name)
    }

    /** speed/bearing 缺失（为 0 且相邻点确实在动）时按几何补算，避免输出恒为 0 的假值。 */
    private fun fillMissing(pts: MutableList<Point>) {
        for (i in 0 until pts.size) {
            val p = pts[i]
            if (p.speed > 0f && p.bearing != 0f) continue
            val a = if (i == 0) p else pts[i - 1]
            val b = if (i == pts.size - 1) p else pts[i + 1]
            val dtMs = (b.t - a.t)
            if (dtMs <= 0) continue
            val dist = Geo.distanceMeters(a.lat, a.lon, b.lat, b.lon)
            val speed = if (p.speed > 0f) p.speed else (dist / (dtMs / 1000.0)).toFloat()
            val bearing = if (p.bearing != 0f) p.bearing else Geo.bearingDeg(a.lat, a.lon, b.lat, b.lon).toFloat()
            pts[i] = p.copy(speed = speed, bearing = bearing)
        }
    }
}

/** 极简大地测量工具，避免为几个公式引入依赖。 */
object Geo {
    private const val R = 6371008.8 // 平均地球半径（米），IUGG 平均值

    fun distanceMeters(lat1: Double, lon1: Double, lat2: Double, lon2: Double): Double {
        val p1 = Math.toRadians(lat1)
        val p2 = Math.toRadians(lat2)
        val dp = p2 - p1
        val dl = Math.toRadians(lon2 - lon1)
        val a = Math.sin(dp / 2).let { it * it } +
                Math.cos(p1) * Math.cos(p2) * Math.sin(dl / 2).let { it * it }
        return 2 * R * Math.asin(Math.min(1.0, Math.sqrt(a)))
    }

    fun bearingDeg(lat1: Double, lon1: Double, lat2: Double, lon2: Double): Double {
        val p1 = Math.toRadians(lat1)
        val p2 = Math.toRadians(lat2)
        val dl = Math.toRadians(lon2 - lon1)
        val y = Math.sin(dl) * Math.cos(p2)
        val x = Math.cos(p1) * Math.sin(p2) - Math.sin(p1) * Math.cos(p2) * Math.cos(dl)
        return (Math.toDegrees(Math.atan2(y, x)) + 360.0) % 360.0
    }

    /** 在 [lat],[lon] 处按半径与方位角求偏移点，用于画圆与原地漂移。 */
    fun offset(lat: Double, lon: Double, bearingDeg: Double, meters: Double): DoubleArray {
        val br = Math.toRadians(bearingDeg)
        val d = meters / R
        val p1 = Math.toRadians(lat)
        val l1 = Math.toRadians(lon)
        val p2 = Math.asin(Math.sin(p1) * Math.cos(d) + Math.cos(p1) * Math.sin(d) * Math.cos(br))
        val l2 = l1 + Math.atan2(
            Math.sin(br) * Math.sin(d) * Math.cos(p1),
            Math.cos(d) - Math.sin(p1) * Math.sin(p2)
        )
        return doubleArrayOf(Math.toDegrees(p2), ((Math.toDegrees(l2) + 540.0) % 360.0) - 180.0)
    }
}

/** 状态同时写 logcat（PC 可读）、SharedPreferences（界面可读）和日志文件（可回溯）。 */
object Status {
    fun set(ctx: Context, msg: String) {
        Log.i(Protocol.TAG, msg)
        LogFile.append(ctx, "STATUS", msg)
        ctx.getSharedPreferences(Protocol.PREF, Context.MODE_PRIVATE)
            .edit()
            .putString(Protocol.KEY_STATUS, msg)
            .putLong(Protocol.KEY_UPDATED, System.currentTimeMillis())
            .apply()
    }

    /**
     * 单独设置开关状态。
     *
     * 为什么需要这个：界面最初是去 `KEY_STATUS` 的文案里找 "playing=true"，
     * 但那个字符串**只有查询状态（ACTION_STATUS）时才会写** —— 单点、转圈、
     * 前往写的分别是「单点: …」「圆圈: …」「前往 …」。结果是虚拟定位明明开着，
     * 顶部状态条却一直显示「已关闭」。
     *
     * 文案是给人看的，不该被程序解析。所以单独存一个明确的状态键。
     */
    fun setState(ctx: Context, state: String) {
        ctx.getSharedPreferences(Protocol.PREF, Context.MODE_PRIVATE)
            .edit()
            .putString(Protocol.KEY_STATE, state)
            .apply()
    }

    fun getState(ctx: Context): String =
        ctx.getSharedPreferences(Protocol.PREF, Context.MODE_PRIVATE)
            .getString(Protocol.KEY_STATE, Protocol.STATE_OFF) ?: Protocol.STATE_OFF
}

/**
 * 持久化运行日志。
 *
 * 为什么需要它：logcat 是**环形缓冲区**，很快会被系统日志冲掉。实测隔了
 * 十几个小时后回去查，`logcat -d` 里只剩两行分隔符 —— 什么都回溯不了。
 * 出问题时最需要的恰恰是「当时到底发生了什么」，所以必须自己落盘。
 *
 * 存放位置：App 的外部私有目录
 *   /sdcard/Android/data/com.mockroute/files/mockroute.log
 * 选这里是因为 **PC 用 adb 就能直接读，不需要 root**。
 * （Android 10 起的分区存储不允许 App 随便写 /sdcard/ 根目录。）
 *
 * 格式：`HH:mm:ss.SSS 开机秒数 [标签] 内容`
 *   带「开机秒数」(elapsedRealtime) 是刻意的：它能区分
 *   「设备休眠/被冻结导致时间跳跃」和「日志真的断了」。
 *   只看墙上时钟做不到这一点。
 *
 * 轮转：超过 MAX_BYTES 就改名成 .1（旧的 .1 覆盖成 .2，最多留 BAK 个）。
 */
object LogFile {
    private const val NAME = "mockroute.log"
    private const val MAX_BYTES = 512 * 1024
    private const val BAK = 2

    private val lock = Any()
    private var inited = false

    fun file(ctx: Context): File = File(Protocol.appDir(ctx), NAME)

    /** 所有备份文件（含主文件），新的在前。PC 端按顺序拼起来看。 */
    fun all(ctx: Context): List<File> {
        val out = mutableListOf<File>()
        val f = file(ctx)
        if (f.exists()) out.add(f)
        for (i in 1..BAK) {
            val b = File(f.parentFile, "$NAME.$i")
            if (b.exists()) out.add(b)
        }
        return out
    }

    private fun init(ctx: Context) {
        if (inited) return
        inited = true
        // 每次进程启动插一条分隔线，方便区分不同的运行会话
        append(ctx, "SESSION", "===== 进程启动 =====")
    }

    fun append(ctx: Context, tag: String, msg: String) {
        synchronized(lock) {
            if (!inited) init(ctx)
            try {
                val f = file(ctx)
                if (f.length() > MAX_BYTES) rotate(ctx)
                val now = System.currentTimeMillis()
                val wall = java.text.SimpleDateFormat("MM-dd HH:mm:ss.SSS",
                    java.util.Locale.US).format(java.util.Date(now))
                val up = android.os.SystemClock.elapsedRealtime() / 1000.0
                f.appendText("$wall ${"%10.3f".format(up)} [$tag] $msg\n")
            } catch (_: Throwable) {
                // 日志写不进去也绝不能让主流程挂掉
            }
        }
    }

    private fun rotate(ctx: Context) {
        val f = file(ctx)
        File(f.parentFile, "$NAME.$BAK").delete()
        for (i in BAK - 1 downTo 1) {
            val src = File(f.parentFile, "$NAME.$i")
            if (src.exists()) src.renameTo(File(f.parentFile, "$NAME.${i + 1}"))
        }
        f.renameTo(File(f.parentFile, "$NAME.1"))
    }
}
