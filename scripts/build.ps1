$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "common.ps1")
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Resolve-OpenThesisPython
$buildTools = Join-Path $projectRoot ".build-tools"

if (-not (Test-Path -LiteralPath (Join-Path $buildTools "PyInstaller"))) {
    & $python -c "import PyInstaller" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller is missing. Install it with: python -m pip install pyinstaller"
    }
}

Initialize-OpenThesisTk -Python $python -ProjectRoot $projectRoot
$pythonPaths = @((Join-Path $projectRoot "src"))
if (Test-Path -LiteralPath $buildTools) {
    $pythonPaths = @($buildTools) + $pythonPaths
}
$env:PYTHONPATH = $pythonPaths -join [IO.Path]::PathSeparator

Push-Location $projectRoot
try {
    & $python -m PyInstaller --noconfirm --clean .\OpenThesis.spec
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed with exit code $LASTEXITCODE"
    }
    $executable = Join-Path $projectRoot "dist\OpenThesis\OpenThesis.exe"
    if (-not (Test-Path -LiteralPath $executable)) {
        throw "Expected executable was not created: $executable"
    }
    Write-Output $executable
} finally {
    Pop-Location
}
