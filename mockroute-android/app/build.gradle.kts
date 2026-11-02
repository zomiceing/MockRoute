plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.mockroute"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.mockroute"
        minSdk = 23
        // targetSdk 33：避免 Android 13+ 的通知运行时权限与 targetSdk 34 的前台服务类型限制，
        // 让这个调试用工具在 6.0 ~ 14 的设备上都能直接跑。需要上架/合规场景请自行提高。
        targetSdk = 33
        versionCode = 1
        versionName = "1.0"
    }

    buildTypes {
        debug {
            isMinifyEnabled = false
        }
        release {
            isMinifyEnabled = false
            // 未配置签名，release 产物无法直接安装；调试请用 debug 或自行配置 signingConfig。
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro"
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        viewBinding = true
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    implementation("org.json:json:20240303")
}
