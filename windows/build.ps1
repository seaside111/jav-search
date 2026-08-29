param(
    [switch]$SkipTests,
    [switch]$Installer,
    [switch]$SkipFreeze,
    [string]$DistPath = "dist-release",
    [string]$WorkPath = "build-release"
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$pyInstaller = Join-Path $repoRoot ".venv\Scripts\pyinstaller.exe"
$version = (Get-Content (Join-Path $repoRoot "VERSION") -Raw).Trim()
$ffprobe = Join-Path $repoRoot "tools\ffprobe.exe"
$distRoot = Join-Path $repoRoot $DistPath
$workRoot = Join-Path $repoRoot $WorkPath

if (-not (Test-Path $venvPython) -or -not (Test-Path $pyInstaller)) {
    throw "Create .venv and install windows/requirements-build.txt first."
}
if (-not (Test-Path $ffprobe)) {
    throw "tools/ffprobe.exe is required for a release build."
}

Push-Location $repoRoot
try {
    if (-not $SkipTests) {
        & $venvPython -m unittest discover -s backend/tests -p "test_*.py"
        if ($LASTEXITCODE -ne 0) { throw "Tests failed." }
    }

    if (-not $SkipFreeze) {
        & $pyInstaller windows/JAVSearch.spec --noconfirm --clean --distpath $distRoot --workpath $workRoot
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed." }
    } elseif (-not (Test-Path -LiteralPath (Join-Path $distRoot "JAV Search\JAV Search.exe"))) {
        throw "SkipFreeze requires an existing frozen application in $distRoot."
    }

    if ($Installer) {
        $iscc = Get-Command ISCC.exe -ErrorAction SilentlyContinue
        $isccPath = if ($iscc) { $iscc.Source } else {
            @(
                (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 7\ISCC.exe"),
                (Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"),
                (Join-Path $env:ProgramFiles "Inno Setup 7\ISCC.exe"),
                (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe")
            ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -First 1
        }
        if (-not $isccPath) { throw "Inno Setup compiler ISCC.exe was not found." }
        $installerOut = Join-Path $distRoot "installer"
        & $isccPath "/DAppVersion=$version" "/DAppSourceRoot=$distRoot" "/DInstallerOutputDir=$installerOut" "windows\installer.iss"
        if ($LASTEXITCODE -ne 0) { throw "Installer build failed." }

        $installerFile = Join-Path $installerOut "JAV-Search-v$version-Windows-x64-Setup.exe"
        if (-not (Test-Path $installerFile)) { throw "Expected installer output was not created." }
        $digest = (Get-FileHash -Algorithm SHA256 -LiteralPath $installerFile).Hash.ToLowerInvariant()
        $checksumPath = "$installerFile.sha256"
        [IO.File]::WriteAllText($checksumPath, "$digest  $([IO.Path]::GetFileName($installerFile))`n",
                                [Text.UTF8Encoding]::new($false))
        Write-Output "Installer: $installerFile"
        Write-Output "SHA256: $digest"
    }
} finally {
    Pop-Location
}
