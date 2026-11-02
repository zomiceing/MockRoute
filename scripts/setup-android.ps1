# ============================================================================
#  setup-android.ps1  --  One-shot Android build environment bootstrap
#
#  ASCII-only on purpose: Windows PowerShell 5.1 reads .ps1 files as ANSI when
#  there is no UTF-8 BOM, which corrupts non-ASCII text and can even break
#  parsing.  Keeping this file pure ASCII makes it work under both
#  powershell.exe 5.1 and pwsh 7 regardless of encoding.
#  Chinese documentation lives in docs/01-environment-setup.md.
#
#  What it does:
#    1. locate JDK 17 (download Temurin 17 into .toolchain/ if missing)
#    2. locate Android SDK (download commandline-tools if missing)
#    3. accept licenses, install platform-tools / platforms;android-34 /
#       build-tools;34.0.0
#    4. write mockroute-android/local.properties and bootstrap gradle wrapper
#
#  Usage:
#    powershell -ExecutionPolicy Bypass -File scripts/setup-android.ps1
#    powershell -ExecutionPolicy Bypass -File scripts/setup-android.ps1 `
#        -SdkRoot .toolchain\android-sdk
#
#  NOTE ON DOWNLOADS: files are fetched by tools/fetch.py (Python + OpenSSL),
#  not by Invoke-WebRequest.  Some Windows hosts have a broken certificate /
#  credential subsystem where every schannel-based HTTPS client fails with
#  SEC_E_NO_CREDENTIALS during the TLS handshake, while DNS and TCP are fine.
#  Python's bundled OpenSSL is not affected, and it also gives us resume.
# ============================================================================

[CmdletBinding()]
param(
    [string]$SdkRoot = "$env:LOCALAPPDATA\Android\Sdk",
    [string]$JavaHome = "",
    [string]$ToolchainDir = "",
    [string]$GradleVersion = "8.7",
    [switch]$SkipDownload,
    [switch]$KeepArchives
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
if (-not $ToolchainDir) { $ToolchainDir = Join-Path $RepoRoot '.toolchain' }

$CmdlineToolsBuild = '11076708'
$TemurinUrl = 'https://api.adoptium.net/v3/binary/latest/17/ga/windows/x64/jdk/hotspot/normal/eclipse'

function Write-Step($m) { Write-Host ""; Write-Host "=== $m ===" -ForegroundColor Cyan }
function Write-Ok($m)   { Write-Host "  [OK] $m" -ForegroundColor Green }
function Write-Warn2($m){ Write-Host "  [!!] $m" -ForegroundColor Yellow }
function Write-Err($m)  { Write-Host "  [XX] $m" -ForegroundColor Red }

function Find-Python {
    foreach ($n in @('python', 'python3')) {
        $c = Get-Command $n -ErrorAction SilentlyContinue
        if ($c) { return $c.Source }
    }
    # PATH 里没有时，再试几个常见的安装位置（只用环境变量拼，不写死盘符）
    $candidates = @()
    foreach ($v in @('311', '312', '313')) {
        $candidates += "$env:LOCALAPPDATA\Programs\Python\Python$v\python.exe"
        $candidates += "$env:ProgramFiles\Python$v\python.exe"
    }
    $candidates += 'C:\Python311\python.exe'
    foreach ($p in $candidates) {
        if ($p -and (Test-Path $p)) { return $p }
    }
    return $null
}

$script:PythonExe = Find-Python

function Invoke-Download($Url, $OutFile) {
    if ($SkipDownload) { throw "Need to download $Url but -SkipDownload was given" }
    if (-not $script:PythonExe) {
        throw "python not found. Python 3.8+ is required as the downloader."
    }
    $fetcher = Join-Path $RepoRoot 'tools/fetch.py'
    if (-not (Test-Path $fetcher)) { throw "missing downloader: $fetcher" }
    Write-Host "  downloading $Url"
    & $script:PythonExe $fetcher $Url $OutFile
    if ($LASTEXITCODE -ne 0) { throw "download failed: $Url" }
}

function Expand-ZipSafe($Zip, $Dest) {
    if (Test-Path $Dest) { Remove-Item -Recurse -Force $Dest }
    New-Item -ItemType Directory -Force -Path $Dest | Out-Null
    Expand-Archive -Path $Zip -DestinationPath $Dest -Force
}

# ----------------------------------------------------------------------------
Write-Step "0/6  Preflight"
# ----------------------------------------------------------------------------
Write-Host "  repo root    : $RepoRoot"
Write-Host "  toolchain    : $ToolchainDir"
Write-Host "  android sdk  : $SdkRoot"
New-Item -ItemType Directory -Force -Path $ToolchainDir | Out-Null

if (-not $script:PythonExe) {
    Write-Err "python not found. The download step needs Python's OpenSSL to work around"
    Write-Host "          the broken Windows schannel on this host."
    Write-Host "          Install Python 3.8+ and re-run."
    exit 1
}
Write-Ok "python: $script:PythonExe"
& $script:PythonExe --version

# Quick connectivity probe so we fail fast with a clear message.
Write-Host "  probing network ..."
& $script:PythonExe -c "import urllib.request as u; u.urlopen('https://dl.google.com/android/repository/repository2-3.xml', timeout=20).read(1); print('  network OK')"
if ($LASTEXITCODE -ne 0) {
    Write-Err "cannot reach dl.google.com over HTTPS even from Python."
    Write-Host "          Check your network / proxy, then re-run."
    exit 1
}

# ----------------------------------------------------------------------------
Write-Step "1/6  JDK 17"
# ----------------------------------------------------------------------------
$javaExe = $null
if ($JavaHome) {
    $cand = Join-Path $JavaHome 'bin/java.exe'
    if (Test-Path $cand) { $javaExe = $cand; Write-Ok "using -JavaHome: $JavaHome" }
    else { Write-Warn2 "-JavaHome has no bin/java.exe: $JavaHome" }
}
if (-not $javaExe -and $env:JAVA_HOME) {
    $cand = Join-Path $env:JAVA_HOME 'bin/java.exe'
    if (Test-Path $cand) { $javaExe = $cand; Write-Ok "using JAVA_HOME: $env:JAVA_HOME" }
}
if (-not $javaExe) {
    $bundled = Join-Path $ToolchainDir 'jdk'
    $cand = Join-Path $bundled 'bin/java.exe'
    if (Test-Path $cand) { $javaExe = $cand; Write-Ok "using bundled JDK: $bundled" }
}
if (-not $javaExe) {
    $cmd = Get-Command java -ErrorAction SilentlyContinue
    # Only trust a PATH java if it is a real JDK (javac present) and version 17+
    if ($cmd) {
        try {
            $verLine = (& $cmd.Source -version 2>&1 | Select-Object -First 1).ToString()
            if ($verLine -match 'version "(\d+)') {
                $major = [int]$Matches[1]
                $javac = Join-Path (Split-Path -Parent $cmd.Source) 'javac.exe'
                if ($major -ge 17 -and (Test-Path $javac)) {
                    $javaExe = $cmd.Source
                    Write-Ok "using java from PATH: $($cmd.Source) ($verLine)"
                } else {
                    Write-Warn2 "PATH java is not a usable JDK 17 ($verLine); ignoring"
                }
            }
        } catch {
            Write-Warn2 "could not determine PATH java version; ignoring"
        }
    }
}
if (-not $javaExe) {
    $dest = Join-Path $ToolchainDir 'jdk'
    $zip = Join-Path $ToolchainDir 'jdk17.zip'
    Write-Warn2 "no JDK 17 found; downloading Temurin 17 (~190 MB)"
    Invoke-Download $TemurinUrl $zip
    $tmp = Join-Path $ToolchainDir 'jdk-extract'
    Write-Host "  extracting ..."
    Expand-ZipSafe $zip $tmp
    $inner = Get-ChildItem $tmp -Directory | Select-Object -First 1
    if (-not $inner) { Write-Err "unexpected JDK archive layout"; exit 1 }
    if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
    Move-Item $inner.FullName $dest
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    if (-not $KeepArchives) { Remove-Item -Force $zip -ErrorAction SilentlyContinue }
    $javaExe = Join-Path $dest 'bin/java.exe'
    Write-Ok "JDK installed: $dest"
}

$env:JAVA_HOME = Split-Path -Parent (Split-Path -Parent $javaExe)
$env:PATH = "$env:JAVA_HOME\bin;$env:PATH"
Write-Host "  JAVA_HOME = $env:JAVA_HOME"
# java -version writes its banner to STDERR. With $ErrorActionPreference='Stop'
# PowerShell 5.1 turns a native command's stderr into a terminating error, which
# would abort the whole script right here. Relax it for this call only.
$eap = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    & $javaExe -version 2>&1 | ForEach-Object { Write-Host "  $_" }
} finally {
    $ErrorActionPreference = $eap
}

# ----------------------------------------------------------------------------
Write-Step "2/6  Android SDK"
# ----------------------------------------------------------------------------
$sdkManager = Get-ChildItem -Path $SdkRoot -Filter 'sdkmanager.bat' -Recurse -ErrorAction SilentlyContinue |
              Select-Object -First 1 -ExpandProperty FullName

if (-not $sdkManager) {
    if ($SkipDownload) {
        Write-Err "no sdkmanager under $SdkRoot and -SkipDownload was given"
        exit 1
    }
    Write-Warn2 "no commandline-tools found; downloading"
    $zip = Join-Path $ToolchainDir 'cmdline-tools.zip'
    $url = "https://dl.google.com/android/repository/commandlinetools-win-${CmdlineToolsBuild}_latest.zip"
    Invoke-Download $url $zip
    $tmp = Join-Path $ToolchainDir 'cmdline-tools-extract'
    Write-Host "  extracting ..."
    Expand-ZipSafe $zip $tmp
    # sdkmanager requires the exact layout cmdline-tools/latest/bin/...
    $latest = Join-Path $SdkRoot 'cmdline-tools/latest'
    if (Test-Path $latest) { Remove-Item -Recurse -Force $latest }
    New-Item -ItemType Directory -Force -Path (Join-Path $SdkRoot 'cmdline-tools') | Out-Null
    Move-Item (Join-Path $tmp 'cmdline-tools') $latest
    Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
    if (-not $KeepArchives) { Remove-Item -Force $zip -ErrorAction SilentlyContinue }
    $sdkManager = Join-Path $latest 'bin/sdkmanager.bat'
    Write-Ok "commandline-tools installed"
    if (-not (Test-Path $sdkManager)) { Write-Err "sdkmanager.bat missing after extract"; exit 1 }
} else {
    Write-Ok "sdkmanager: $sdkManager"
}

# ----------------------------------------------------------------------------
Write-Step "3/6  SDK packages and licenses"
# ----------------------------------------------------------------------------
$env:ANDROID_HOME = $SdkRoot
$env:ANDROID_SDK_ROOT = $SdkRoot

Write-Host "  accepting licenses ..."
# Same stderr-as-error trap as with java -version: sdkmanager writes progress to
# stderr and the text can contain sequences that PowerShell flags.
$eap = $ErrorActionPreference
$ErrorActionPreference = 'Continue'
try {
    $yes = ("y`n" * 80)
    $yes | & $sdkManager --sdk_root="$SdkRoot" --licenses 2>&1 | Out-Null
    Write-Ok "licenses handled"

    $packages = @('platform-tools', 'platforms;android-34', 'build-tools;34.0.0')
    foreach ($pkg in $packages) {
        $probe = Join-Path $SdkRoot ($pkg -replace ';', '/')
        if (Test-Path $probe) { Write-Ok "already installed: $pkg"; continue }
        Write-Host "  installing $pkg ..."
        & $sdkManager --sdk_root="$SdkRoot" $pkg 2>&1 | Select-Object -Last 2 | ForEach-Object { Write-Host "    $_" }
        if (Test-Path $probe) { Write-Ok "$pkg done" } else { Write-Warn2 "$pkg may have failed" }
    }
} finally {
    $ErrorActionPreference = $eap
}

$adb = Join-Path $SdkRoot 'platform-tools/adb.exe'
if (Test-Path $adb) { Write-Ok "adb: $adb" } else { Write-Warn2 "adb.exe not found under platform-tools" }

# ----------------------------------------------------------------------------
Write-Step "4/6  local.properties"
# ----------------------------------------------------------------------------
$AppDir = Join-Path $RepoRoot 'mockroute-android'
if (-not (Test-Path $AppDir)) { Write-Err "missing $AppDir"; exit 1 }
$localProps = Join-Path $AppDir 'local.properties'
Set-Content -Path $localProps -Value ("sdk.dir=" + ($SdkRoot -replace '\\', '\\')) -Encoding ASCII
Write-Ok "wrote $localProps"

# ----------------------------------------------------------------------------
Write-Step "5/6  gradle wrapper"
# ----------------------------------------------------------------------------
if (Test-Path (Join-Path $AppDir 'gradlew.bat')) {
    Write-Ok "gradle wrapper already present"
} else {
    $gradleExe = $null
    $cmd = Get-Command gradle -ErrorAction SilentlyContinue
    if ($cmd) { $gradleExe = $cmd.Source }

    $bundled = Join-Path $ToolchainDir "gradle-$GradleVersion"
    if (-not $gradleExe -and (Test-Path (Join-Path $bundled 'bin/gradle.bat'))) {
        $gradleExe = Join-Path $bundled 'bin/gradle.bat'
    }
    if (-not $gradleExe) {
        if ($SkipDownload) {
            Write-Warn2 "no gradle and -SkipDownload given; skipping wrapper generation"
        } else {
            $zip = Join-Path $ToolchainDir "gradle-$GradleVersion-bin.zip"
            Write-Warn2 "downloading gradle $GradleVersion"
            Invoke-Download "https://services.gradle.org/distributions/gradle-$GradleVersion-bin.zip" $zip
            $tmp = Join-Path $ToolchainDir 'gradle-extract'
            Write-Host "  extracting ..."
            Expand-ZipSafe $zip $tmp
            $inner = Get-ChildItem $tmp -Directory | Select-Object -First 1
            if (Test-Path $bundled) { Remove-Item -Recurse -Force $bundled }
            Move-Item $inner.FullName $bundled
            Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue
            if (-not $KeepArchives) { Remove-Item -Force $zip -ErrorAction SilentlyContinue }
            $gradleExe = Join-Path $bundled 'bin/gradle.bat'
            Write-Ok "gradle installed: $bundled"
        }
    }

    if ($gradleExe) {
        Write-Host "  generating wrapper with $gradleExe ..."
        Push-Location $AppDir
        # gradle also writes progress to stderr; keep it from aborting the script.
        $eap = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try {
            & $gradleExe wrapper --gradle-version $GradleVersion 2>&1 |
                Select-Object -Last 4 | ForEach-Object { Write-Host "    $_" }
        } catch {
            Write-Warn2 "wrapper generation failed: $($_.Exception.Message)"
        } finally {
            $ErrorActionPreference = $eap
            Pop-Location
        }
        if (Test-Path (Join-Path $AppDir 'gradlew.bat')) { Write-Ok "gradle wrapper ready" }
        else { Write-Warn2 "wrapper still missing; build-android.ps1 will fall back to the bundled gradle" }
    }
}

# ----------------------------------------------------------------------------
Write-Step "6/6  Summary"
# ----------------------------------------------------------------------------
Write-Host "  JAVA_HOME    = $env:JAVA_HOME"
Write-Host "  ANDROID_SDK  = $SdkRoot"
if (Test-Path $adb) { Write-Host "  adb          = $adb" }
Write-Host ""
Write-Host "  Next:" -ForegroundColor Cyan
Write-Host "    powershell -ExecutionPolicy Bypass -File scripts/build-android.ps1 -Install"
Write-Host ""
Write-Host "  Add these to PATH later if you want plain 'adb' / 'java' commands:"
Write-Host "    $SdkRoot\platform-tools"
Write-Host "    $env:JAVA_HOME\bin"
