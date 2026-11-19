param([switch]$Debug)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$toolRoot = Join-Path $projectRoot '.android-tools'
$env:JAVA_HOME = Join-Path $toolRoot 'jdk'
$env:ANDROID_SDK_ROOT = Join-Path $toolRoot 'sdk'
$env:ANDROID_USER_HOME = Join-Path $toolRoot 'android-user'
$env:GRADLE_USER_HOME = Join-Path $toolRoot 'gradle-cache'
if (-not (Test-Path -LiteralPath (Join-Path $env:JAVA_HOME 'bin\java.exe'))) { throw 'Run setup_android_tools.ps1 or configure the local build tools first.' }
$nodePath = (Get-Command node.exe -ErrorAction Stop).Source
$webSource = Join-Path $projectRoot 'web\dist'
$webTarget = Join-Path $projectRoot 'mobile\www'
foreach ($asset in Get-ChildItem -LiteralPath $webSource -File -Recurse) {
    $relativeAsset = $asset.FullName.Substring($webSource.Length + 1)
    if ($relativeAsset.StartsWith('downloads\') -or $asset.Extension -eq '.apk') { continue }
    $targetAsset = Join-Path $webTarget $relativeAsset
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $targetAsset) | Out-Null
    Copy-Item -LiteralPath $asset.FullName -Destination $targetAsset -Force
}
Push-Location -LiteralPath (Join-Path $projectRoot 'mobile')
try { & $nodePath 'node_modules\@capacitor\cli\bin\capacitor' telemetry off | Out-Null; & $nodePath 'node_modules\@capacitor\cli\bin\capacitor' sync android; if ($LASTEXITCODE -ne 0) { throw 'Capacitor sync failed' } } finally { Pop-Location }
$taskName = if ($Debug) { 'assembleDebug' } else { 'assembleRelease' }
if (-not $Debug) {
    $signingPath = Join-Path $toolRoot 'signing'
    New-Item -ItemType Directory -Force -Path $signingPath | Out-Null
    $secretsPath = Join-Path $signingPath 'signing-secrets.json'
    if (Test-Path -LiteralPath $secretsPath) { $signing = Get-Content -LiteralPath $secretsPath -Raw | ConvertFrom-Json }
    else {
        $randomBytes = New-Object byte[] 32
        $generator = [Security.Cryptography.RandomNumberGenerator]::Create()
        try { $generator.GetBytes($randomBytes) } finally { $generator.Dispose() }
        $signing = @{password=[Convert]::ToBase64String($randomBytes)}
        $signing | ConvertTo-Json | Set-Content -LiteralPath $secretsPath -Encoding utf8
    }
    $env:PAPERLOOM_ANDROID_STORE_FILE = Join-Path $signingPath 'paperloom-release.p12'
    $env:PAPERLOOM_ANDROID_STORE_PASSWORD = $signing.password
    if (-not (Test-Path -LiteralPath $env:PAPERLOOM_ANDROID_STORE_FILE)) {
        & (Join-Path $env:JAVA_HOME 'bin\keytool.exe') -genkeypair -noprompt -storetype PKCS12 -keystore $env:PAPERLOOM_ANDROID_STORE_FILE -alias paperloom -keyalg RSA -keysize 3072 -validity 3650 -dname 'CN=Paperloom, OU=Reader, O=Paperloom' -storepass:env PAPERLOOM_ANDROID_STORE_PASSWORD
        if ($LASTEXITCODE -ne 0) { throw 'APK signing key generation failed' }
    }
}
Push-Location -LiteralPath (Join-Path $projectRoot 'mobile\android')
try { & '.\gradlew.bat' $taskName '--no-daemon' '--console=plain'; if ($LASTEXITCODE -ne 0) { throw 'Android build failed' } } finally { Pop-Location }
$out = Join-Path $projectRoot 'output'
New-Item -ItemType Directory -Force -Path $out | Out-Null
$apk = if ($Debug) { 'mobile\android\app\build\outputs\apk\debug\app-debug.apk' } else { 'mobile\android\app\build\outputs\apk\release\app-release.apk' }
$name = if ($Debug) { 'Paperloom-0.3.4-debug.apk' } else { 'Paperloom-0.3.4.apk' }
Copy-Item -LiteralPath (Join-Path $projectRoot $apk) -Destination (Join-Path $out $name) -Force
Write-Output "APK ready: $name"
