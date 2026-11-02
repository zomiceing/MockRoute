# ============================================================================
#  mroute.ps1  --  run the mroute CLI with a correct adb/toolchain environment
#
#  ASCII-only on purpose (see setup-android.ps1 for why).
#
#  Why this wrapper exists
#  -----------------------
#  adb needs three writable locations, and on this host the defaults are not
#  usable from the sandboxed process that starts the adb daemon:
#
#    * ANDROID_USER_HOME / ADB_VENDOR_KEYS -> where adbkey lives.
#      C:\Users\<user>\.android cannot be created by the daemon, and adb will
#      not generate its key pair when $ADB_VENDOR_KEYS is unset.
#    * TMP / TEMP -> adb writes %TEMP%\adb.log at startup. The daemon is a
#      detached process and does NOT inherit the interactive session's file
#      grants, so it fails with "cannot open ...\adb.log: Permission denied".
#    * JAVA_HOME -> needed by gradle.
#
#  Usage:
#    powershell -ExecutionPolicy Bypass -File scripts/mroute.ps1 doctor
#    powershell -ExecutionPolicy Bypass -File scripts/mroute.ps1 point --at 39.9075,116.3910
#    powershell -ExecutionPolicy Bypass -File scripts/mroute.ps1 play --route out/demo.json
#
#  Any arguments are forwarded verbatim to tools/mroute.py.
# ============================================================================

[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Tc = Join-Path $RepoRoot '.toolchain'

# --- adb / SDK locations -----------------------------------------------------
$SdkRoot = Join-Path $Tc 'android-sdk'
$AdbHome = Join-Path $Tc 'android-home'
$TmpDir  = Join-Path $Tc 'tmp'
$Jdk     = Join-Path $Tc 'jdk'

New-Item -ItemType Directory -Force -Path $AdbHome, $TmpDir | Out-Null

if (-not (Test-Path (Join-Path $AdbHome 'adbkey'))) {
    Write-Host "[!] adb key pair missing; generating ..." -ForegroundColor Yellow
    $adbExe = Join-Path $SdkRoot 'platform-tools/adb.exe'
    if (Test-Path $adbExe) {
        $env:ANDROID_USER_HOME = $AdbHome
        $env:TMP = $TmpDir; $env:TEMP = $TmpDir
        & $adbExe kill-server 2>&1 | Out-Null
        & $adbExe keygen (Join-Path $AdbHome 'adbkey') 2>&1 | Out-Null
    }
}

$env:ANDROID_USER_HOME = $AdbHome
$env:ANDROID_SDK_HOME  = $AdbHome
$env:ADB_VENDOR_KEYS   = $AdbHome
$env:ANDROID_HOME      = $SdkRoot
$env:ANDROID_SDK_ROOT  = $SdkRoot
$env:TMP = $TmpDir
$env:TEMP = $TmpDir
if (Test-Path (Join-Path $Jdk 'bin/java.exe')) {
    $env:JAVA_HOME = $Jdk
    $env:PATH = "$Jdk\bin;$SdkRoot\platform-tools;$env:PATH"
} else {
    $env:PATH = "$SdkRoot\platform-tools;$env:PATH"
}

# --- python ------------------------------------------------------------------
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { $py = (Get-Command python3 -ErrorAction SilentlyContinue).Source }
if (-not $py) {
    # PATH 里没有时，再试几个常见安装位置（不写死具体盘符）
    foreach ($v in @('311', '312', '313')) {
        $p = "$env:LOCALAPPDATA\Programs\Python\Python$v\python.exe"
        if (Test-Path $p) { $py = $p; break }
    }
}
if (-not $py) {
    Write-Host "[XX] python not found." -ForegroundColor Red
    Write-Host "     Install Python 3 from https://www.python.org/downloads/" -ForegroundColor Yellow
    Write-Host "     and tick 'Add python.exe to PATH'." -ForegroundColor Yellow
    exit 1
}

$cli = Join-Path $RepoRoot 'tools/mroute.py'
if (-not (Test-Path $cli)) { Write-Host "[XX] missing $cli" -ForegroundColor Red; exit 1 }

$env:PYTHONIOENCODING = 'utf-8'
& $py $cli @Args
exit $LASTEXITCODE
