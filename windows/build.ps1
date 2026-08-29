param(
    [switch]$SkipTests,
    [switch]$Installer
)

$ErrorActionPreference = "Stop"
$repoRoot = Split-Path -Parent $PSScriptRoot
$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
$pyInstaller = Join-Path $repoRoot ".venv\Scripts\pyinstaller.exe"
$version = (Get-Content (Join-Path $repoRoot "VERSION") -Raw).Trim()
$ffprobe = Join-Path $repoRoot "tools\ffprobe.exe"

if (-not (Test-Path $venvPython) -or -not (Test-Path $pyInstaller)) {
    throw "请先创建 .venv 并安装 windows/requirements-build.txt。"
}
if (-not (Test-Path $ffprobe)) {
    throw "缺少 tools/ffprobe.exe。正式 Windows 构建必须包含 ffprobe。"
}

Push-Location $repoRoot
try {
    if (-not $SkipTests) {
        & $venvPython -m unittest discover -s backend/tests -p "test_*.py"
        if ($LASTEXITCODE -ne 0) { throw "测试失败。" }
    }

    & $pyInstaller windows/JAVSearch.spec --noconfirm --clean
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 构建失败。" }

    if ($Installer) {
        $iscc = Get-Command ISCC.exe -ErrorAction SilentlyContinue
        if (-not $iscc) { throw "未找到 Inno Setup 编译器 ISCC.exe。" }
        & $iscc.Source "/DAppVersion=$version" "windows\installer.iss"
        if ($LASTEXITCODE -ne 0) { throw "安装包构建失败。" }
    }
} finally {
    Pop-Location
}
