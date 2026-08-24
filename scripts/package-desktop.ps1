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
$version = "2.0.1"

$pythonPaths = @((Join-Path $projectRoot "src"))
if (Test-Path -LiteralPath (Join-Path $buildTools "PyInstaller")) {
    $pythonPaths = @($buildTools) + $pythonPaths
}
$env:PYTHONPATH = $pythonPaths -join [IO.Path]::PathSeparator
$env:CARGO_TARGET_DIR = $cargoTarget
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

    & (Join-Path $PSScriptRoot "desktop.ps1") portable
    if ($LASTEXITCODE -ne 0) {
        throw "Tauri desktop build failed with exit code $LASTEXITCODE"
    }

    $output = Join-Path $projectRoot "installer-output"
    New-Item -ItemType Directory -Path $output -Force | Out-Null

    $portableStage = Join-Path $projectRoot ("build\portable\{0}\{1}" -f $version, [guid]::NewGuid())
    $portableRoot = Join-Path $portableStage "OpenThesis"
    $portableSidecar = Join-Path $portableRoot "bin\openthesis-sidecar"
    New-Item -ItemType Directory -Path $portableSidecar -Force | Out-Null

    $desktopExecutable = Join-Path $cargoTarget "release\openthesis-desktop.exe"
    if (-not (Test-Path -LiteralPath $desktopExecutable -PathType Leaf)) {
        throw "The desktop executable was not created: $desktopExecutable"
    }
    Copy-Item -LiteralPath $desktopExecutable -Destination (Join-Path $portableRoot "OpenThesis.exe")
    Copy-Item -Path (Join-Path $sidecarBundle "*") -Destination $portableSidecar -Recurse
    # PyInstaller hooks may copy third-party package SBOM directories containing
    # public maintainer contact metadata. They are not required at runtime and
    # would weaken the release archive's strict no-email privacy invariant.
    Get-ChildItem -LiteralPath $portableSidecar -Directory -Recurse -Filter "sboms" |
        Where-Object { $_.Parent.Name -like "*.dist-info" } |
        Remove-Item -Recurse -Force

    $portableZip = Join-Path $output "OpenThesis-$version-windows-x64-portable.zip"
    Compress-Archive -LiteralPath $portableRoot -DestinationPath $portableZip -CompressionLevel Optimal -Force
    $portableHash = Get-FileHash -Algorithm SHA256 -LiteralPath $portableZip
    Set-Content -LiteralPath "$portableZip.sha256" `
        -Value "$($portableHash.Hash)  $([IO.Path]::GetFileName($portableZip))" `
        -Encoding ascii

    & (Join-Path $PSScriptRoot "verify-release-privacy.ps1") -Archive $portableZip
    if ($LASTEXITCODE -ne 0) {
        throw "Release privacy verification failed with exit code $LASTEXITCODE"
    }
    & (Join-Path $PSScriptRoot "verify-desktop-portable.ps1") -Version $version -CargoTarget $cargoTarget
    if ($LASTEXITCODE -ne 0) {
        throw "Portable package verification failed with exit code $LASTEXITCODE"
    }
    & (Join-Path $PSScriptRoot "verify-desktop-runtime.ps1") `
        -Executable (Join-Path $portableRoot "OpenThesis.exe")
    if ($LASTEXITCODE -ne 0) {
        throw "Portable runtime verification failed with exit code $LASTEXITCODE"
    }

    Write-Output "Sidecar: $sidecarExecutable"
    Write-Output "Portable: $portableZip"
    Write-Output "Portable SHA256: $($portableHash.Hash)"
} finally {
    Pop-Location
}

