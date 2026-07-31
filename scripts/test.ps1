$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "common.ps1")
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Resolve-OpenThesisPython
$pythonPaths = @((Join-Path $projectRoot "src"))
$buildTools = Join-Path $projectRoot ".build-tools"
if (Test-Path -LiteralPath $buildTools) {
    $pythonPaths = @($buildTools) + $pythonPaths
}
$env:PYTHONPATH = $pythonPaths -join [IO.Path]::PathSeparator

Push-Location $projectRoot
try {
    & $python -m compileall -q src tests
    if ($LASTEXITCODE -ne 0) {
        throw "compileall failed with exit code $LASTEXITCODE"
    }
    & $python -m unittest discover -s tests -v
    if ($LASTEXITCODE -ne 0) {
        throw "Unit tests failed with exit code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}
