param([string]$ExistingSdk = (Join-Path $env:USERPROFILE 'AppData\Local\Android\Sdk'))
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$projectRoot = Split-Path -Parent $PSScriptRoot
$toolRoot = Join-Path $projectRoot '.android-tools'
New-Item -ItemType Directory -Force -Path $toolRoot | Out-Null
$jdkPath = Join-Path $toolRoot 'jdk'
if (-not (Test-Path -LiteralPath (Join-Path $jdkPath 'bin\java.exe'))) {
    $metadata = Invoke-RestMethod -Uri 'https://api.adoptium.net/v3/assets/latest/21/hotspot?architecture=x64&image_type=jdk&os=windows&vendor=eclipse' -TimeoutSec 60
    $package = $metadata[0].binary.package
    $jdkZip = Join-Path $toolRoot 'jdk21.zip'
    Write-Output 'Downloading official Temurin Java 21...'
    Invoke-WebRequest -Uri $package.link -OutFile $jdkZip -TimeoutSec 900
    if ((Get-FileHash -LiteralPath $jdkZip -Algorithm SHA256).Hash.ToLowerInvariant() -ne $package.checksum) { throw 'Java archive checksum mismatch' }
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $jdkArchive = [IO.Compression.ZipFile]::OpenRead($jdkZip)
    try {
        foreach ($entry in $jdkArchive.Entries) {
            $resolvedEntry = [IO.Path]::GetFullPath((Join-Path $toolRoot $entry.FullName))
            if (-not $resolvedEntry.StartsWith($toolRoot + [IO.Path]::DirectorySeparatorChar, [StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe Java archive path' }
        }
        $jdkFolder = $jdkArchive.Entries[0].FullName.Split('/')[0]
    } finally { $jdkArchive.Dispose() }
    Expand-Archive -LiteralPath $jdkZip -DestinationPath $toolRoot -Force
    $extractedJdk = [IO.Path]::GetFullPath((Join-Path $toolRoot $jdkFolder))
    if (-not $extractedJdk.StartsWith($toolRoot + [IO.Path]::DirectorySeparatorChar,[StringComparison]::OrdinalIgnoreCase)) { throw 'Unsafe Java destination' }
    Move-Item -LiteralPath $extractedJdk -Destination $jdkPath
}
$sdkPath = Join-Path $toolRoot 'sdk'
New-Item -ItemType Directory -Force -Path $sdkPath | Out-Null
$existingLicenses = Join-Path $ExistingSdk 'licenses'
if (-not (Test-Path -LiteralPath $existingLicenses)) { throw 'Android SDK license acceptance is required; install the SDK with Android Studio first.' }
Copy-Item -LiteralPath $existingLicenses -Destination $sdkPath -Recurse -Force
$sdkManager = Join-Path $ExistingSdk 'cmdline-tools\latest\bin\sdkmanager.bat'
if (-not (Test-Path -LiteralPath $sdkManager)) { throw 'Android command-line tools are not installed' }
$env:JAVA_HOME = $jdkPath
$env:ANDROID_USER_HOME = Join-Path $toolRoot 'android-user'
Write-Output 'Installing Android API 36 using the already accepted local SDK licenses...'
& $sdkManager "--sdk_root=$sdkPath" 'platforms;android-36' 'build-tools;36.0.0' 'platform-tools'
if ($LASTEXITCODE -ne 0) { throw 'Android SDK installation failed; no license agreement was auto-accepted.' }
$propertiesPath = Join-Path $projectRoot 'mobile\android\local.properties'
('sdk.dir=' + $sdkPath.Replace('\','/')) | Set-Content -LiteralPath $propertiesPath -Encoding utf8
Write-Output "Android tools ready: $toolRoot"
