# ============================================================================
#  build-android.ps1  --  build mockroute-android (and optionally install it)
#
#  ASCII-only on purpose (see setup-android.ps1).
#
#  Flow:
#    1. run scripts/prep-build.ps1 to mirror the project into an all-ASCII
#       build workspace INSIDE the repo (see that script for why this is
#       unavoidable on Windows with a non-ASCII project path)
#    2. run Gradle assembleDebug there
#    3. copy the APK to dist/MockRoute-debug.apk
#    4. with -Install: adb install -r, grant the mock-location appop,
#       optionally -Launch the app
#
#  Usage:
#    powershell -ExecutionPolicy Bypass -File scripts/build-android.ps1
#    powershell -ExecutionPolicy Bypass -File scripts/build-android.ps1 -Install -Launch
#    powershell -ExecutionPolicy Bypass -File scripts/build-android.ps1 -Clean -Refresh
# ============================================================================

[CmdletBinding()]
param(
    [string]$SdkRoot = "",
    [string]$ToolchainDir = "",
    [switch]$Install,
    [switch]$Launch,
    [switch]$Clean,
    [switch]$Refresh,      # re-mirror the project before building
    [switch]$Release
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$SrcDir = Join-Path $RepoRoot 'mockroute-android'
$BuildDir = Join-Path $RepoRoot 'build-ws'
if (-not $ToolchainDir) { $ToolchainDir = Join-Path $RepoRoot '.toolchain' }

function Write-Step($m) { Write-Host ""; Write-Host "=== $m ===" -ForegroundColor Cyan }
function Write-Ok($m)   { Write-Host "  [OK] $m" -ForegroundColor Green }
function Write-Warn2($m){ Write-Host "  [!!] $m" -ForegroundColor Yellow }
function Write-Err($m)  { Write-Host "  [XX] $m" -ForegroundColor Red }

if (-not (Test-Path $SrcDir)) { Write-Err "missing $SrcDir"; exit 1 }

# ----------------------------------------------------------------------------
Write-Step "1/5  Toolchain"
# ----------------------------------------------------------------------------
$javaHome = ""
$bundledJdk = Join-Path $ToolchainDir 'jdk'
if (Test-Path (Join-Path $bundledJdk 'bin/java.exe')) {
    $javaHome = $bundledJdk
} elseif ($env:JAVA_HOME -and (Test-Path (Join-Path $env:JAVA_HOME 'bin/java.exe'))) {
    $javaHome = $env:JAVA_HOME
} else {
    $cmd = Get-Command java -ErrorAction SilentlyContinue
    if ($cmd) { $javaHome = Split-Path -Parent (Split-Path -Parent $cmd.Source) }
}
if (-not $javaHome) {
    Write-Err "no JDK found. Run: powershell -ExecutionPolicy Bypass -File scripts/setup-android.ps1"
    exit 1
}
$env:JAVA_HOME = $javaHome
$env:PATH = "$javaHome\bin;$env:PATH"
Write-Ok "JDK: $javaHome"

if (-not $SdkRoot) {
    foreach ($c in @((Join-Path $ToolchainDir 'android-sdk'), "$env:LOCALAPPDATA\Android\Sdk")) {
        if (Test-Path (Join-Path $c 'platform-tools')) { $SdkRoot = $c; break }
    }
    if (-not $SdkRoot -and $env:ANDROID_HOME) { $SdkRoot = $env:ANDROID_HOME }
}
if (-not $SdkRoot -or -not (Test-Path $SdkRoot)) {
    Write-Err "Android SDK not found. Run scripts/setup-android.ps1 first."
    exit 1
}
$env:ANDROID_HOME = $SdkRoot
$env:ANDROID_SDK_ROOT = $SdkRoot
Write-Ok "Android SDK: $SdkRoot"

$gradleExe = $null
$gradlew = Join-Path $BuildDir 'gradlew.bat'
if (Test-Path $gradlew) {
    $gradleExe = $gradlew
} else {
    $cmd = Get-Command gradle -ErrorAction SilentlyContinue
    if ($cmd) {
        $gradleExe = $cmd.Source
    } else {
        $gradleExe = Get-ChildItem -Path $ToolchainDir -Directory -Filter 'gradle-*' -ErrorAction SilentlyContinue |
                     Sort-Object Name -Descending |
                     ForEach-Object { Join-Path $_.FullName 'bin/gradle.bat' } |
                     Where-Object { Test-Path $_ } |
                     Select-Object -First 1
    }
}
if (-not $gradleExe) {
    Write-Err "no gradle found. Run scripts/setup-android.ps1 first."
    exit 1
}
Write-Ok "gradle: $gradleExe"

# Keep every Gradle cache inside the project (default ~/.gradle may be
# unwritable for the sandboxed daemon, same class of problem as ~/.android).
$gradleHome = Join-Path $ToolchainDir 'gradle-home'
$tmpDir = Join-Path $ToolchainDir 'tmp'
New-Item -ItemType Directory -Force -Path $gradleHome, $tmpDir | Out-Null
$env:GRADLE_USER_HOME = $gradleHome
$env:TMP = $tmpDir
$env:TEMP = $tmpDir
Write-Ok "GRADLE_USER_HOME: $gradleHome"

# ----------------------------------------------------------------------------
Write-Step "2/5  Mirrored build workspace"
# ----------------------------------------------------------------------------
$marker = Join-Path $BuildDir '.mockroute-buildws'
if ($Refresh -or -not (Test-Path $marker)) {
    Write-Host "  mirroring project into $BuildDir ..."
    & powershell.exe -NoProfile -ExecutionPolicy Bypass -File (Join-Path $PSScriptRoot 'prep-build.ps1')
    if ($LASTEXITCODE -ne 0) { Write-Err "prep-build.ps1 failed"; exit 1 }
} else {
    # Re-sync sources so you do not have to remember -Refresh.
    # IMPORTANT: do NOT copy gradle.properties / local.properties here.
    # Those two are GENERATED by prep-build.ps1 (they carry the
    # android.overridePathCheck override and the \u-escaped sdk.dir); copying
    # the originals over them would silently undo the workaround and the build
    # fails with "Your project path contains non-ASCII characters".
    foreach ($item in @('app', 'build.gradle.kts', 'settings.gradle.kts')) {
        $s = Join-Path $SrcDir $item
        if (Test-Path $s) { Copy-Item $s -Destination $BuildDir -Recurse -Force }
    }
    Write-Ok "reused $BuildDir (sources re-synced)"
}

# ----------------------------------------------------------------------------
Write-Step "3/5  Build"
# ----------------------------------------------------------------------------
$task = if ($Release) { 'assembleRelease' } else { 'assembleDebug' }
$gradleArgs = @()
if ($Clean) { $gradleArgs += 'clean' }
$gradleArgs += $task
$gradleArgs += '--console=plain'

Write-Host "  $gradleExe $($gradleArgs -join ' ')"
$exit = 0
$eap = $ErrorActionPreference
Push-Location $BuildDir
try {
    $ErrorActionPreference = 'Continue'
    & $gradleExe @gradleArgs 2>&1 | ForEach-Object {
        $line = $_.ToString()
        if ($line -match 'BUILD SUCCESSFUL') { Write-Host "  $line" -ForegroundColor Green }
        elseif ($line -match '^e: |FAILURE|What went wrong|Execution failed|^\s*error:') {
            Write-Host "  $line" -ForegroundColor Red
        }
        elseif ($line -match '^w: |warning:|^\s*>\s') { Write-Host "  $line" -ForegroundColor DarkGray }
        else { Write-Host "  $line" }
    }
    $exit = $LASTEXITCODE
    if ($null -eq $exit) { $exit = 0 }
} catch {
    Write-Host "  $($_.Exception.Message)" -ForegroundColor Red
    $exit = 1
} finally {
    $ErrorActionPreference = $eap
    Pop-Location
}

if ($exit -ne 0) {
    Write-Err "build failed (exit $exit)"
    Write-Host @"

Common causes:
  * SDK licenses / packages missing -> re-run scripts/setup-android.ps1
  * wrong JDK -> AGP 8.x needs JDK 17
  * dependencies unreachable -> configure a gradle proxy, or build in Android Studio
  * Kotlin errors are printed above as lines starting with "e: "
"@
    exit $exit
}

$apk = Get-ChildItem -Path (Join-Path $BuildDir 'app/build/outputs/apk') -Filter '*.apk' -Recurse -ErrorAction SilentlyContinue |
       Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $apk) { Write-Err "build succeeded but no APK found"; exit 1 }

$dist = Join-Path $RepoRoot 'dist'
New-Item -ItemType Directory -Force -Path $dist | Out-Null
$distApk = Join-Path $dist 'MockRoute-debug.apk'
Copy-Item $apk.FullName $distApk -Force
Write-Ok ("APK: {0}  ({1:N1} KB)  ->  dist/MockRoute-debug.apk" -f $apk.FullName, ($apk.Length / 1KB))

if (-not ($Install -or $Launch)) {
    Write-Host ""
    Write-Host "  Install with: powershell -ExecutionPolicy Bypass -File scripts/build-android.ps1 -Install"
    exit 0
}

# ----------------------------------------------------------------------------
Write-Step "4/5  Install"
# ----------------------------------------------------------------------------
$adb = Join-Path $SdkRoot 'platform-tools/adb.exe'
if (-not (Test-Path $adb)) {
    $c = Get-Command adb -ErrorAction SilentlyContinue
    if ($c) { $adb = $c.Source } else { Write-Err "adb not found"; exit 1 }
}
# adb needs writable user-home and temp dirs; the sandbox blocks the defaults.
$adbHome = Join-Path $ToolchainDir 'android-home'
New-Item -ItemType Directory -Force -Path $adbHome | Out-Null
$env:ANDROID_USER_HOME = $adbHome
$env:ADB_VENDOR_KEYS = $adbHome
if (-not (Test-Path (Join-Path $adbHome 'adbkey'))) {
    Write-Warn2 "adb key pair missing; generating"
    & $adb kill-server 2>&1 | Out-Null
    & $adb keygen (Join-Path $adbHome 'adbkey') 2>&1 | Out-Null
}
Write-Ok "adb: $adb"

& $adb start-server 2>&1 | Out-Null
$devices = & $adb devices | Select-Object -Skip 1 | Where-Object { $_ -match '\sdevice$' }
if (-not $devices) {
    Write-Err "no authorized device"
    & $adb devices
    Write-Host "  If it says 'unauthorized', accept the USB debugging prompt on the phone."
    exit 1
}
Write-Ok "device: $(($devices | ForEach-Object { ($_ -split '\s+')[0] }) -join ', ')"

& $adb install -r $apk.FullName
if ($LASTEXITCODE -ne 0) {
    Write-Err "install failed"
    Write-Host "  If INSTALL_FAILED_UPDATE_INCOMPATIBLE: adb uninstall com.mockroute"
    exit $LASTEXITCODE
}
Write-Ok "installed"

# ----------------------------------------------------------------------------
Write-Step "5/5  Mock location permission"
# ----------------------------------------------------------------------------
& $adb shell appops set com.mockroute android:mock_location allow
$state = (& $adb shell appops get com.mockroute android:mock_location) -join ' '
Write-Host "  appops: $state"
if ($state -match 'allow') { Write-Ok "mock location granted" }
else {
    Write-Warn2 "appops did not report allow"
    Write-Host "          Also try: Developer options -> Select mock location app -> MockRoute"
}

if ($Launch) {
    & $adb shell am start -n com.mockroute/.MainActivity | Out-Null
    Write-Ok "launched"
}

Write-Host ""
Write-Host "  Next:" -ForegroundColor Cyan
Write-Host "    powershell -ExecutionPolicy Bypass -File scripts/mroute.ps1 doctor"
Write-Host "    powershell -ExecutionPolicy Bypass -File scripts/mroute.ps1 point --at 39.9087,116.3975"
