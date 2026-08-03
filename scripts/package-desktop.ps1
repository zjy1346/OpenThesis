$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "common.ps1")
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Resolve-OpenThesisPython
$buildTools = Join-Path $projectRoot ".build-tools"
$resourceRoot = Join-Path $projectRoot "desktop\src-tauri\resources\bin"
$sidecarBundle = Join-Path $resourceRoot "openthesis-sidecar"
$cargoTarget = if ($env:CARGO_TARGET_DIR) {
    $env:CARGO_TARGET_DIR
} else {
    "D:\OpenThesisToolchain\cargo-target\openthesis"
}
$version = "1.0.0-alpha.1"

if (-not (Test-Path -LiteralPath (Join-Path $buildTools "PyInstaller"))) {
    throw "The locked PyInstaller build tools are missing from .build-tools."
}

$pythonPaths = @($buildTools, (Join-Path $projectRoot "src"))
$env:PYTHONPATH = $pythonPaths -join [IO.Path]::PathSeparator
New-Item -ItemType Directory -Path $resourceRoot -Force | Out-Null

Push-Location $projectRoot
try {
    & $python -m PyInstaller --noconfirm --clean `
        --distpath $resourceRoot `
        --workpath (Join-Path $projectRoot "build\sidecar") `
        .\OpenThesisSidecar.spec
    if ($LASTEXITCODE -ne 0) {
        throw "Sidecar build failed with exit code $LASTEXITCODE"
    }
    $sidecarExecutable = Join-Path $sidecarBundle "openthesis-sidecar.exe"
    if (-not (Test-Path -LiteralPath $sidecarExecutable -PathType Leaf)) {
        throw "Expected sidecar executable was not created: $sidecarExecutable"
    }

    & (Join-Path $PSScriptRoot "desktop.ps1") build
    if ($LASTEXITCODE -ne 0) {
        throw "Tauri desktop build failed with exit code $LASTEXITCODE"
    }

    $nsisDirectory = Join-Path $cargoTarget "release\bundle\nsis"
    $installer = Get-ChildItem -LiteralPath $nsisDirectory -Filter "*.exe" -File |
        Sort-Object LastWriteTimeUtc -Descending |
        Select-Object -First 1
    if (-not $installer) {
        throw "The NSIS installer was not created."
    }
    $output = Join-Path $projectRoot "installer-output"
    New-Item -ItemType Directory -Path $output -Force | Out-Null
    $publishedInstaller = Join-Path $output "OpenThesis-$version-windows-x64-setup.exe"
    Copy-Item -LiteralPath $installer.FullName -Destination $publishedInstaller -Force
    $hash = Get-FileHash -Algorithm SHA256 -LiteralPath $publishedInstaller
    Set-Content -LiteralPath "$publishedInstaller.sha256" `
        -Value "$($hash.Hash)  $([IO.Path]::GetFileName($publishedInstaller))" `
        -Encoding ascii

    Write-Output "Sidecar: $sidecarExecutable"
    Write-Output "Installer: $publishedInstaller"
    Write-Output "SHA256: $($hash.Hash)"
} finally {
    Pop-Location
}
