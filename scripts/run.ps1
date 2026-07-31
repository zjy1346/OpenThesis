$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "common.ps1")
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Resolve-OpenThesisPython
Initialize-OpenThesisTk -Python $python -ProjectRoot $projectRoot
$pythonPaths = @((Join-Path $projectRoot "src"))
$buildTools = Join-Path $projectRoot ".build-tools"
if (Test-Path -LiteralPath $buildTools) {
    $pythonPaths = @($buildTools) + $pythonPaths
}
$env:PYTHONPATH = $pythonPaths -join [IO.Path]::PathSeparator
& $python -m openthesis
