package com.mockroute

import android.location.Location

/**
 * 轨迹中的一个采样点。
 *
 * t 是相对轨迹起点的时间戳（毫秒），设备端按 t 的差值决定每个点之间等待多久。
 * speed / bearing 由 PC 端按相邻点位移算好，保证与自己算出来的值自洽。
 */
data class Point(
    val t: Long,
    val lon: Double,
    val lat: Double,
    val speed: Float,
    val bearing: Float,
    val accuracy: Float,
    val altitude: Double,
)

/** 回放模式 */
enum class RouteMode {
    /** 播完即停 */
    ONCE,

    /** 播完从头再来 */
    LOOP,

    /** 走到终点后原路折返 */
    PINGPONG;

    companion object {
        fun parse(s: String?): RouteMode = when (s?.trim()?.lowercase()) {
            "loop", "cycle" -> LOOP
            "pingpong", "ping-pong", "back", "bounce" -> PINGPONG
            else -> ONCE
        }
    }
}

/** 一条完整轨迹 */
data class Route(
    val points: List<Point>,
    val mode: RouteMode = RouteMode.ONCE,
    val source: String = "",
) {
    val durationMs: Long get() = if (points.isEmpty()) 0L else points.last().t
}

/**
 * 把 [Point] 转成系统认识的 [Location]。
 *
 * elapsedRealtimeNanos 用 SystemClock.elapsedRealtimeNanos() 补正，避免部分 App 因该字段
 * 与开机时间不符而丢弃定位。synthetic 字段是 framework 内部数据；不使用反射，
 * 因为 Android 14 起对 hidden API 的反射限制会让它直接崩溃。
 */
fun Point.toLocation(provider: String): Location {
    val loc = Location(provider)
    loc.latitude = lat
    loc.longitude = lon
    loc.accuracy = accuracy
    loc.altitude = altitude
    loc.speed = speed
    loc.bearing = bearing
    loc.time = System.currentTimeMillis()
    @Suppress("DEPRECATION")
    loc.elapsedRealtimeNanos = android.os.SystemClock.elapsedRealtimeNanos()
    if (android.os.Build.VERSION.SDK_INT >= android.os.Build.VERSION_CODES.O) {
        loc.verticalAccuracyMeters = accuracy
        loc.speedAccuracyMetersPerSecond = 0.5f
        loc.bearingAccuracyDegrees = 5f
    }
    return loc
}
