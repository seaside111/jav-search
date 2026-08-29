param(
    [string]$FfmpegAsset = "ffmpeg-n8.1-latest-win64-lgpl-8.1.zip"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$toolsDir = Join-Path $repoRoot "tools"
$downloadDir = Join-Path $env:TEMP "jav-search-ffmpeg"
$releaseBase = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest"
$archive = Join-Path $downloadDir $FfmpegAsset
$checksums = Join-Path $downloadDir "checksums.sha256"
$extractDir = Join-Path $downloadDir "extract"

New-Item -ItemType Directory -Force -Path $downloadDir, $toolsDir | Out-Null
$curl = Get-Command curl.exe -ErrorAction Stop
& $curl.Source -L --fail --retry 3 --output $checksums "$releaseBase/checksums.sha256"
if ($LASTEXITCODE -ne 0) { throw "Failed to download checksums.sha256." }
& $curl.Source -L --fail --retry 3 --continue-at - --output $archive "$releaseBase/$FfmpegAsset"
if ($LASTEXITCODE -ne 0) { throw "Failed to download $FfmpegAsset." }

$checksumLine = Get-Content $checksums | Where-Object { $_ -match "\s$([regex]::Escape($FfmpegAsset))$" } | Select-Object -First 1
if (-not $checksumLine) { throw "Asset not found in checksums file: $FfmpegAsset" }
$expected = ($checksumLine -split '\s+')[0].ToLowerInvariant()
$actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash.ToLowerInvariant()
if ($actual -ne $expected) { throw "FFmpeg archive checksum verification failed." }

if (Test-Path $extractDir) { Remove-Item -LiteralPath $extractDir -Recurse -Force }
Expand-Archive -LiteralPath $archive -DestinationPath $extractDir
$probe = Get-ChildItem -LiteralPath $extractDir -Filter ffprobe.exe -Recurse | Select-Object -First 1
if (-not $probe) { throw "ffprobe.exe was not found in the archive." }
Copy-Item -LiteralPath $probe.FullName -Destination (Join-Path $toolsDir "ffprobe.exe") -Force

$license = Get-ChildItem -LiteralPath $extractDir -File -Recurse | Where-Object { $_.Name -match '^(LICENSE|COPYING)' } | Select-Object -First 1
if ($license) { Copy-Item -LiteralPath $license.FullName -Destination (Join-Path $toolsDir "FFmpeg-LICENSE.txt") -Force }

& (Join-Path $toolsDir "ffprobe.exe") -version | Select-Object -First 1
