$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path

Push-Location $projectRoot
try {
    & (Join-Path $PSScriptRoot "test.ps1")
    if ($LASTEXITCODE -ne 0) {
        throw "Tests failed before packaging"
    }

    & (Join-Path $PSScriptRoot "package-desktop.ps1")
    if ($LASTEXITCODE -ne 0) {
        throw "Desktop packaging failed"
    }
} finally {
    Pop-Location
}
