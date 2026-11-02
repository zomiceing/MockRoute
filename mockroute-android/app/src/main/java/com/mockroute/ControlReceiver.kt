package com.mockroute

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent

/**
 * 唯一的外部入口。PC 端用：
 *
 *   adb shell am broadcast -a com.mockroute.action.START_ROUTE \
 *       -n com.mockroute/.ControlReceiver --es file /sdcard/mockroute/route.json
 *
 * 这里只做参数搬运，真正的定位写入在 [MockLocationService] 里，
 * 因为广播接收器是短生命周期的，不能持有定时器。
 */
class ControlReceiver : BroadcastReceiver() {

    override fun onReceive(context: Context, intent: Intent) {
        val action = intent.action ?: return
        val extras = intent.extras ?: android.os.Bundle.EMPTY

        val map = HashMap<String, Any?>()
        for (key in extras.keySet()) {
            map[key] = extras.get(key)
        }

        if (action == Protocol.ACTION_STATUS) {
            // 直接查服务状态没有意义（服务可能没起），先把请求转给服务，
            // 服务不在时给出明确提示。
            map["_from"] = "receiver"
        }

        try {
            MockLocationService.start(context, action, map)
        } catch (t: Throwable) {
            Status.set(context, "无法启动服务: ${t.javaClass.simpleName}: ${t.message}")
        }

        // STATUS 且服务未运行时，服务会在 onStartCommand 里输出状态后常驻，
        // 这里额外记录一次接收日志，方便 adb logcat 排查丢广播的问题。
        android.util.Log.i(Protocol.TAG, "收到广播 $action extras=${extras.keySet()}")
    }
}
