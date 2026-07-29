$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "common.ps1")
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Resolve-OpenThesisPython
Initialize-OpenThesisTk -Python $python -ProjectRoot $projectRoot
$env:PYTHONPATH = Join-Path $projectRoot "src"
& $python -m openthesis
