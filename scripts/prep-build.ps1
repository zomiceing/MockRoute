# ============================================================================
#  prep-build.ps1  --  mirror mockroute-android into an ASCII build directory
#
#  ASCII-only on purpose (see setup-android.ps1).
#
#  WHY THIS EXISTS
#  ---------------
#  If the repo path contains non-ASCII characters (e.g. an em dash or CJK
#  directory name), Windows + AGP cannot build there, for two independent
#  reasons:
#
#    1. Gradle/Gradle-daemon cannot create the project's .gradle directory when
#       an ancestor path contains non-ASCII characters. The daemon log shows a
#       mangled path (the em dash comes back as GBK mojibake), and the build
#       dies with "Failed to create parent directory ...\.gradle".
#    2. Even when .gradle creation succeeds, AGP refuses outright:
#       "Your project path contains non-ASCII characters."
#
#  A directory junction does NOT help: the JVM resolves the link back to the
#  real (non-ASCII) path, so the same failure returns.
#
#  Therefore we mirror the Android project into an all-ASCII directory *inside*
#  the workspace (it has to be inside, because the sandbox only permits writes
#  there), build there, and copy the APK back out.
#
#  Usage:
#    powershell -ExecutionPolicy Bypass -File scripts/prep-build.ps1
#    powershell -ExecutionPolicy Bypass -File scripts/prep-build.ps1 -Force
# ============================================================================

[CmdletBinding()]
param(
    [string]$Dest = "",
    [switch]$Force
)

$ErrorActionPreference = 'Stop'

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Src = Join-Path $RepoRoot 'mockroute-android'
if (-not (Test-Path $Src)) { Write-Host "[XX] missing $Src" -ForegroundColor Red; exit 1 }

if (-not $Dest) { $Dest = Join-Path $RepoRoot 'build-ws' }

# Safety: never delete a directory we did not create.
if ((Test-Path $Dest) -and -not $Force) {
    $marker = Join-Path $Dest '.mockroute-buildws'
    if (-not (Test-Path $marker)) {
        Write-Host "[XX] $Dest exists but is not a build workspace (no marker file)." -ForegroundColor Red
        Write-Host "     Refusing to overwrite. Pass -Force if you are sure."
        exit 1
    }
}
if ((Test-Path $Dest) -and $Force) { Remove-Item $Dest -Recurse -Force }

Write-Host "=== mirroring project ===" -ForegroundColor Cyan
Write-Host "  from: $Src"
Write-Host "  to  : $Dest"
New-Item -ItemType Directory -Force -Path $Dest | Out-Null
'created by scripts/prep-build.ps1 -- safe to delete' | Set-Content (Join-Path $Dest '.mockroute-buildws') -Encoding ASCII

foreach ($item in @('app', 'build.gradle.kts', 'settings.gradle.kts', 'gradle.properties', 'proguard-rules.pro')) {
    $s = Join-Path $Src $item
    if (Test-Path $s) {
        Copy-Item $s -Destination $Dest -Recurse -Force
        Write-Host "  copied $item"
    }
}

# --- local.properties: sdk.dir must be escaped ---------------------------------
# Java .properties files are ISO-8859-1, so a non-ASCII character in the SDK
# path (ours contains an em dash) must be written as \uXXXX. Writing it raw as
# ASCII turns it into '?' and Gradle reports "SDK location not found".
$SdkRoot = Join-Path $RepoRoot '.toolchain/android-sdk'
if (-not (Test-Path $SdkRoot)) {
    Write-Host "[XX] SDK not found at $SdkRoot -- run scripts/setup-android.ps1 first" -ForegroundColor Red
    exit 1
}
$sdkSlash = $SdkRoot -replace '\\', '/'
$sdkEscaped = ''
foreach ($ch in $sdkSlash.ToCharArray()) {
    if ([int][char]$ch -gt 127) { $sdkEscaped += ('\u{0:x4}' -f [int][char]$ch) } else { $sdkEscaped += $ch }
}
[System.IO.File]::WriteAllText((Join-Path $Dest 'local.properties'), "sdk.dir=$sdkEscaped`n", [System.Text.Encoding]::ASCII)
Write-Host "  local.properties: sdk.dir=$sdkEscaped"

# --- gradle.properties: disable AGP's non-ASCII path check ---------------------
# Written from scratch rather than patched: simpler to reason about, and it
# guarantees the override is present even if the source file changes.
$gp = Join-Path $Dest 'gradle.properties'
$gpContent = @(
    'org.gradle.jvmargs=-Xmx2048m -Dfile.encoding=UTF-8',
    'android.useAndroidX=true',
    'android.nonTransitiveRClass=true',
    'kotlin.code.style=official',
    '',
    '# The build directory still has a non-ASCII ancestor (the workspace path),',
    '# but the build itself works there -- only AGP''s preflight check trips on it.',
    'android.overridePathCheck=true',
    ''
) -join "`n"
[System.IO.File]::WriteAllText($gp, $gpContent, [System.Text.Encoding]::ASCII)
$check = Get-Content $gp -Raw
if ($check -notmatch 'overridePathCheck') {
    Write-Host "[XX] failed to write android.overridePathCheck into gradle.properties" -ForegroundColor Red
    exit 1
}
Write-Host "  gradle.properties: android.overridePathCheck=true"

Write-Host ""
Write-Host "  Build workspace ready. Next:" -ForegroundColor Green
Write-Host "    powershell -ExecutionPolicy Bypass -File scripts/build-android.ps1 -Install -Launch"
