package com.mockroute

import android.app.AppOpsManager
import android.content.Context
import android.content.SharedPreferences
import android.graphics.Color
import android.os.Build
import android.os.Bundle
import android.os.Process
import androidx.appcompat.app.AppCompatActivity
import com.mockroute.databinding.ActivityMainBinding

/**
 * 面板：看状态、自检、开关虚拟定位。
 *
 * 正常流程完全不需要碰这个界面 —— PC 端脚本一条 adb 命令就能驱动全部功能。
 * 这里存在的意义是：出问题时你能在手机上一眼看到服务到底有没有收到广播，
 * 以及**在手机上直接关掉虚拟定位**（不用连电脑）。
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var prefs: SharedPreferences

    private val watcher = SharedPreferences.OnSharedPreferenceChangeListener { _, key ->
        if (key == Protocol.KEY_STATUS || key == Protocol.KEY_STATE) render()
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        prefs = getSharedPreferences(Protocol.PREF, Context.MODE_PRIVATE)

        // 关闭虚拟定位：主操作
        binding.btnMockOff.setOnClickListener { turnMockOff() }
        // 重新开启：把上次推送的轨迹再放一遍
        binding.btnMockOn.setOnClickListener { restart() }
        binding.btnCheck.setOnClickListener { selfCheck() }
    }

    override fun onResume() {
        super.onResume()
        prefs.registerOnSharedPreferenceChangeListener(watcher)
        render()
    }

    override fun onPause() {
        prefs.unregisterOnSharedPreferenceChangeListener(watcher)
        super.onPause()
    }

    // ---------------- 开关 ----------------

    /**
     * 关闭虚拟定位。
     *
     * 会让服务停止推点并移除 mock provider，之后系统恢复真实定位。
     *
     * ⚠️ 已知限制：`dumpsys location` 里的 `[mock]` 标记在未 root 的设备上
     * **仍然会显示**。那是 raw provider 内部的状态位，公开 SDK 没有 API 能清除
     * （只有 `isFromMockProvider` 是公开的）。位置本身已经恢复正常，
     * 但如果有 App 直接读 `[mock]` 标记，仍可能识别出来。
     */
    private fun turnMockOff() {
        MockLocationService.start(this, Protocol.ACTION_STOP, emptyMap())
        binding.tvSwitchState.text = "虚拟定位：已关闭"
        binding.tvSwitchState.setBackgroundColor(Color.parseColor("#2A2A2A"))
        binding.tvSwitchState.setTextColor(Color.parseColor("#AAAAAA"))
        binding.tvStatus.text = """
            已发送「关闭虚拟定位」。

            现在：
              · 服务已停止推点，mock provider 已移除
              · 系统会恢复使用真实 GPS
              · 手机地图 App 应很快回到你的实际位置

            注意：dumpsys 里的 [mock] 标记在未 root 设备上仍会保留，
            那是系统内部状态位，公开 API 清不掉。位置本身已恢复正常。
        """.trimIndent()
    }

    /** 重新开启：复用上次推送的轨迹文件，按默认模式再跑一遍。 */
    private fun restart() {
        val f = Protocol.defaultRouteFile(this)
        if (!f.exists()) {
            binding.tvStatus.text = """
                没有可用的轨迹文件。

                ${f.absolutePath} 不存在。
                先用 PC 端推一条：
                  tools/mroute.py play --route out/xxx.json
                或用面板下方的「自检」看目录内容。
            """.trimIndent()
            return
        }
        MockLocationService.start(this, Protocol.ACTION_START_ROUTE, mapOf(
            Protocol.EXTRA_FILE to f.absolutePath,
            Protocol.EXTRA_MODE to "once",
        ))
        binding.tvStatus.text = "已请求重新开启，正在读取轨迹…\n${f.absolutePath}"
    }

    // ---------------- 状态渲染 ----------------

    private fun render() {
        val status = prefs.getString(Protocol.KEY_STATUS, "等待指令…") ?: "等待指令…"
        val ts = prefs.getLong(Protocol.KEY_UPDATED, 0L)
        val when_ = if (ts == 0L) "" else java.text.SimpleDateFormat(
            "HH:mm:ss", java.util.Locale.US).format(java.util.Date(ts))

        // 读服务写的明确状态键，**不要**去解析 KEY_STATUS 的文案 ——
        // 文案只有查询状态时才含 "playing=true"，单点/转圈/前往写的都是别的
        // 措辞，靠文案判断会让开关永远显示「已关闭」。
        when (Status.getState(this)) {
            Protocol.STATE_ON -> {
                binding.tvSwitchState.text = "虚拟定位：运行中"
                binding.tvSwitchState.setBackgroundColor(Color.parseColor("#10391F"))
                binding.tvSwitchState.setTextColor(Color.parseColor("#4ADE80"))
            }
            Protocol.STATE_PAUSED -> {
                binding.tvSwitchState.text = "虚拟定位：已暂停"
                binding.tvSwitchState.setBackgroundColor(Color.parseColor("#4A3C10"))
                binding.tvSwitchState.setTextColor(Color.parseColor("#FFD24A"))
            }
            else -> {
                binding.tvSwitchState.text = "虚拟定位：已关闭"
                binding.tvSwitchState.setBackgroundColor(Color.parseColor("#2A2A2A"))
                binding.tvSwitchState.setTextColor(Color.parseColor("#AAAAAA"))
            }
        }

        binding.tvStatus.text = "[$when_] $status"
    }

    // ---------------- 自检 ----------------

    private fun selfCheck() {
        val sb = StringBuilder()
        val mockAllowed = isMockLocationAllowed()
        sb.appendLine("包名: ${packageName}")
        sb.appendLine("目标 SDK: ${applicationInfo.targetSdkVersion}  设备 SDK: ${Build.VERSION.SDK_INT}")
        sb.appendLine("API 级别: ${Build.VERSION.RELEASE}")
        sb.appendLine()
        sb.appendLine("模拟位置应用授权: " + if (mockAllowed) "✓ 已授权" else "✗ 未授权")
        if (!mockAllowed) {
            sb.appendLine("  修复: adb shell appops set $packageName android:mock_location allow")
            sb.appendLine("  或 开发者选项 → 选择模拟位置信息应用 → MockRoute")
        }
        sb.appendLine()
        val f = Protocol.defaultRouteFile(this)
        sb.appendLine("默认轨迹: ${f.absolutePath}")
        sb.appendLine("  存在: ${f.exists()}" + if (f.exists()) "  ${f.length()} 字节" else "")
        sb.appendLine("  推送: adb push route.json ${f.absolutePath}")
        sb.appendLine()
        sb.appendLine("目录内容:")
        Protocol.appDir(this).listFiles()?.take(20)?.forEach { sb.appendLine("  ${it.name}  ${it.length()}B") }
        sb.appendLine()
        sb.appendLine("当前状态:")
        sb.appendLine(prefs.getString(Protocol.KEY_STATUS, "（无）"))
        sb.appendLine()
        sb.appendLine("电池优化建议: 设置 → 应用 → MockRoute → 耗电管理 → 无限制")
        sb.appendLine("  （否则长时间回放可能被系统冻结）")

        binding.tvStatus.text = sb.toString()
    }

    /** 判断本应用是否被设为模拟位置信息应用，不依赖任何隐藏 API。 */
    private fun isMockLocationAllowed(): Boolean {
        val appOps = getSystemService(Context.APP_OPS_SERVICE) as AppOpsManager
        val mode = try {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                appOps.unsafeCheckOpNoThrow(
                    AppOpsManager.OPSTR_MOCK_LOCATION, Process.myUid(), packageName
                )
            } else {
                @Suppress("DEPRECATION")
                appOps.checkOpNoThrow(
                    AppOpsManager.OPSTR_MOCK_LOCATION, Process.myUid(), packageName
                )
            }
        } catch (t: Throwable) {
            return false
        }
        return mode == AppOpsManager.MODE_ALLOWED
    }
}
