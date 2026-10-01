param(
    [ValidateSet("unsigned-test", "authenticode-required")]
    [string]$SignatureMode = "unsigned-test",
    [switch]$SkipPrivacyVerification
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "common.ps1")
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$python = Resolve-OpenThesisPython
$buildTools = Join-Path $projectRoot ".build-tools"
$resourceRoot = Join-Path $projectRoot "desktop\src-tauri\resources\bin"
$sidecarBundle = Join-Path $resourceRoot "openthesis-sidecar"
$sidecarStageRoot = Join-Path $projectRoot ("build\sidecar-stage\{0}" -f [guid]::NewGuid())
$sidecarDist = Join-Path $sidecarStageRoot "dist"
$sidecarWork = Join-Path $sidecarStageRoot "work"
$sidecarBackup = Join-Path $sidecarStageRoot "previous-sidecar"
$hadSidecarBundle = Test-Path -LiteralPath $sidecarBundle -PathType Container
$sidecarInstalledForBuild = $false
$cargoTarget = if ($env:CARGO_TARGET_DIR) {
    $env:CARGO_TARGET_DIR
} else {
    "D:\OpenThesisToolchain\cargo-target\openthesis"
}
$versionLine = Select-String -LiteralPath (Join-Path $projectRoot "pyproject.toml") -Pattern '^version\s*=\s*"(\d+\.\d+\.\d+)"' | Select-Object -First 1
if (-not $versionLine) {
    throw "Unable to read the canonical version from pyproject.toml."
}
$version = $versionLine.Matches[0].Groups[1].Value
$versionChecks = @(
    @{ Path = "desktop\package.json"; Pattern = '"version"\s*:\s*"([^"]+)"' },
    @{ Path = "desktop\package-lock.json"; Pattern = '"version"\s*:\s*"([^"]+)"' },
    @{ Path = "desktop\src-tauri\Cargo.toml"; Pattern = '(?m)^version\s*=\s*"([^"]+)"' },
    @{ Path = "desktop\src-tauri\tauri.conf.json"; Pattern = '"version"\s*:\s*"([^"]+)"' },
    @{ Path = "src\openthesis\__init__.py"; Pattern = '__version__\s*=\s*"([^"]+)"' },
    @{ Path = "desktop\src-tauri\Cargo.lock"; Pattern = '(?ms)\[\[package\]\]\s*name\s*=\s*"openthesis-desktop"\s*version\s*=\s*"([^"]+)"' }
)
foreach ($check in $versionChecks) {
    $sourcePath = Join-Path $projectRoot $check.Path
    $sourceText = Get-Content -LiteralPath $sourcePath -Raw
    $match = [regex]::Match($sourceText, $check.Pattern)
    if (-not $match.Success -or $match.Groups[1].Value -ne $version) {
        throw "Version mismatch in $($check.Path); pyproject.toml defines $version."
    }
}
foreach ($versionResource in @(
    @{ Path = "build_support\version_info.txt"; Pattern = "StringStruct\(u'FileVersion', u'([^']+)'\)" },
    @{ Path = "build_support\version_info.txt"; Pattern = "StringStruct\(u'ProductVersion', u'([^']+)'\)" },
    @{ Path = "OpenThesisSidecar.version.txt"; Pattern = 'StringStruct\("FileVersion", "([^"]+)"\)' },
    @{ Path = "OpenThesisSidecar.version.txt"; Pattern = 'StringStruct\("ProductVersion", "([^"]+)"\)' },
    @{ Path = "build_support\version_info.txt"; Pattern = 'filevers=\((\d+,\s*\d+,\s*\d+,\s*\d+)\)' },
    @{ Path = "build_support\version_info.txt"; Pattern = 'prodvers=\((\d+,\s*\d+,\s*\d+,\s*\d+)\)' },
    @{ Path = "OpenThesisSidecar.version.txt"; Pattern = 'filevers=\((\d+,\s*\d+,\s*\d+,\s*\d+)\)' },
    @{ Path = "OpenThesisSidecar.version.txt"; Pattern = 'prodvers=\((\d+,\s*\d+,\s*\d+,\s*\d+)\)' }
)) {
    $resourceText = Get-Content -LiteralPath (Join-Path $projectRoot $versionResource.Path) -Raw
    $resourceMatch = [regex]::Match($resourceText, $versionResource.Pattern)
    $expected = if ($versionResource.Pattern -match "FileVersion" -and $versionResource.Path -eq "OpenThesisSidecar.version.txt") {
        "$version.0"
    } elseif ($versionResource.Pattern -match "^(filevers|prodvers)=") {
        (($version.Split('.') + @('0'))[0..3] -join ', ')
    } else {
        $version
    }
    if (-not $resourceMatch.Success -or $resourceMatch.Groups[1].Value -ne $expected) {
        throw "Windows version resource mismatch in $($versionResource.Path); expected $expected."
    }
}
$output = Join-Path $projectRoot "installer-output"
$portableZip = Join-Path $output "OpenThesis-$version-windows-x64-portable.zip"
$portableCandidate = Join-Path $output "OpenThesis-$version-windows-x64-portable.candidate.zip"
$portableChecksum = "$portableZip.sha256"
$packageReady = $false

# PyInstaller can finish successfully even when a lazily imported runtime
# dependency is absent from the selected Python environment. Fail before any
# release artifact is created so an unusable sidecar cannot look publishable.
& $python -c "import cryptography, opencc, pdfplumber, pypdf, pypdfium2, truststore"
if ($LASTEXITCODE -ne 0) {
    throw "The selected packaging Python is missing one or more required runtime dependencies. Install the project dependencies or set OPENTHESIS_PYTHON to a complete environment."
}

New-Item -ItemType Directory -Path $output -Force | Out-Null
foreach ($staleArtifact in @($portableZip, $portableChecksum, $portableCandidate)) {
    if (Test-Path -LiteralPath $staleArtifact) {
        Remove-Item -LiteralPath $staleArtifact -Force
    }
}

$commit = (& git -C $projectRoot rev-parse HEAD).Trim()
$buildTime = [DateTime]::UtcNow.ToString("yyyy-MM-ddTHH:mm:ssZ")
$contractVersion = "2.0"
$identityText = "$version|$commit|$buildTime|$contractVersion"
$identityBytes = [Text.Encoding]::UTF8.GetBytes($identityText)
$identityHash = [Security.Cryptography.SHA256]::Create().ComputeHash($identityBytes)
$buildId = -join ($identityHash | ForEach-Object { $_.ToString("x2") })
$buildInfo = [ordered]@{
    version = $version
    commit = $commit
    build_time_utc = $buildTime
    contract_version = $contractVersion
    build_id = $buildId
} | ConvertTo-Json
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
function Write-Utf8NoBom {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Content)
    [IO.File]::WriteAllText($Path, $Content, $utf8NoBom)
}
$pythonBuildInfo = Join-Path $projectRoot "src\openthesis\resources\build-info.json"
$tauriBuildInfo = Join-Path $projectRoot "desktop\src-tauri\resources\build-info.json"
$hadPythonBuildInfo = Test-Path -LiteralPath $pythonBuildInfo -PathType Leaf
$hadTauriBuildInfo = Test-Path -LiteralPath $tauriBuildInfo -PathType Leaf
$previousPythonBuildInfo = if ($hadPythonBuildInfo) { Get-Content -LiteralPath $pythonBuildInfo -Raw } else { $null }
$previousTauriBuildInfo = if ($hadTauriBuildInfo) { Get-Content -LiteralPath $tauriBuildInfo -Raw } else { $null }
New-Item -ItemType Directory -Path (Split-Path -Parent $pythonBuildInfo) -Force | Out-Null
New-Item -ItemType Directory -Path (Split-Path -Parent $tauriBuildInfo) -Force | Out-Null
Write-Utf8NoBom -Path $pythonBuildInfo -Content $buildInfo
Write-Utf8NoBom -Path $tauriBuildInfo -Content $buildInfo

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
        --distpath $sidecarDist `
        --workpath $sidecarWork `
        .\OpenThesisSidecar.spec
    if ($LASTEXITCODE -ne 0) {
        throw "Sidecar build failed with exit code $LASTEXITCODE"
    }
    $newSidecarBundle = Join-Path $sidecarDist "openthesis-sidecar"
    $sidecarExecutable = Join-Path $newSidecarBundle "openthesis-sidecar.exe"
    if (-not (Test-Path -LiteralPath $sidecarExecutable -PathType Leaf)) {
        throw "Expected sidecar executable was not created: $sidecarExecutable"
    }
    $sidecarRuntimeFiles = @(Get-ChildItem -LiteralPath (Join-Path $newSidecarBundle "_internal") -File |
        Where-Object { $_.Name -match '^(VCRUNTIME|MSVCP).*\.dll$' })
    if ($sidecarRuntimeFiles.Count -eq 0) {
        throw "Expected sidecar MSVC runtime files were not created."
    }
    foreach ($sidecarRuntimeFile in $sidecarRuntimeFiles) {
        $sidecarRuntimeStaged = Join-Path $sidecarRuntimeFile.DirectoryName "$($sidecarRuntimeFile.BaseName).bin"
        Move-Item -LiteralPath $sidecarRuntimeFile.FullName -Destination $sidecarRuntimeStaged
    }

    if ($hadSidecarBundle) {
        Move-Item -LiteralPath $sidecarBundle -Destination $sidecarBackup
    }
    try {
        Move-Item -LiteralPath $newSidecarBundle -Destination $sidecarBundle
        $sidecarInstalledForBuild = $true
        $sidecarExecutable = Join-Path $sidecarBundle "openthesis-sidecar.exe"
    } catch {
        if ($hadSidecarBundle -and (Test-Path -LiteralPath $sidecarBackup -PathType Container)) {
            Move-Item -LiteralPath $sidecarBackup -Destination $sidecarBundle
        }
        throw
    }

    & (Join-Path $PSScriptRoot "desktop.ps1") portable
    if ($LASTEXITCODE -ne 0) {
        throw "Tauri desktop build failed with exit code $LASTEXITCODE"
    }

    $portableStage = Join-Path $projectRoot ("build\portable\{0}\{1}" -f $version, [guid]::NewGuid())
    $portableRoot = Join-Path $portableStage "OpenThesis-$version"
    $portableSidecar = Join-Path $portableRoot "bin\openthesis-sidecar"
    New-Item -ItemType Directory -Path $portableSidecar -Force | Out-Null

    $desktopExecutable = Join-Path $cargoTarget "release\openthesis-desktop.exe"
    if (-not (Test-Path -LiteralPath $desktopExecutable -PathType Leaf)) {
        throw "The desktop executable was not created: $desktopExecutable"
    }
    if ($SignatureMode -eq "authenticode-required") {
        foreach ($executable in @($desktopExecutable, $sidecarExecutable)) {
            $signature = Get-AuthenticodeSignature -LiteralPath $executable
            if ($signature.Status -ne "Valid") {
                throw "Authenticode validation failed for ${executable}: $($signature.Status)"
            }
        }
    }
    Copy-Item -LiteralPath $desktopExecutable -Destination (Join-Path $portableRoot "OpenThesis.exe")
    Copy-Item -Path (Join-Path $sidecarBundle "*") -Destination $portableSidecar -Recurse
    $portableRuntimeFiles = @(Get-ChildItem -LiteralPath (Join-Path $portableSidecar "_internal") -File |
        Where-Object { $_.Name -match '^(VCRUNTIME|MSVCP).*\.bin$' })
    if ($portableRuntimeFiles.Count -eq 0) {
        throw "Portable sidecar MSVC runtime staging files are missing."
    }
    foreach ($portableRuntimeFile in $portableRuntimeFiles) {
        $portableRuntime = Join-Path $portableRuntimeFile.DirectoryName "$($portableRuntimeFile.BaseName).dll"
        Move-Item -LiteralPath $portableRuntimeFile.FullName -Destination $portableRuntime
    }
    # PyInstaller hooks may copy third-party package SBOM directories containing
    # public maintainer contact metadata. They are not required at runtime and
    # would weaken the release archive's strict no-email privacy invariant.
    Get-ChildItem -LiteralPath $portableSidecar -Directory -Recurse -Filter "sboms" |
        Where-Object { $_.Parent.Name -like "*.dist-info" } |
        Remove-Item -Recurse -Force

    & (Join-Path $PSScriptRoot "verify-desktop-runtime.ps1") `
        -Executable (Join-Path $portableRoot "OpenThesis.exe")
    if ($LASTEXITCODE -ne 0) {
        throw "Portable runtime verification failed with exit code $LASTEXITCODE"
    }

    Compress-Archive -LiteralPath $portableRoot -DestinationPath $portableCandidate -CompressionLevel Optimal -Force
    if (-not $SkipPrivacyVerification) {
        & (Join-Path $PSScriptRoot "verify-release-privacy.ps1") -Archive $portableCandidate
        if ($LASTEXITCODE -ne 0) {
            throw "Release privacy verification failed with exit code $LASTEXITCODE"
        }
    }
    Move-Item -LiteralPath $portableCandidate -Destination $portableZip
    $portableHash = Get-FileHash -Algorithm SHA256 -LiteralPath $portableZip
    Set-Content -LiteralPath $portableChecksum `
        -Value "$($portableHash.Hash)  $([IO.Path]::GetFileName($portableZip))" `
        -Encoding ascii
    & (Join-Path $PSScriptRoot "verify-desktop-portable.ps1") `
        -Version $version -CargoTarget $cargoTarget -SignatureMode $SignatureMode
    if ($LASTEXITCODE -ne 0) {
        throw "Portable package verification failed with exit code $LASTEXITCODE"
    }
    $packageReady = $true

    Write-Output "Sidecar: $sidecarExecutable"
    Write-Output "Portable: $portableZip"
    Write-Output "Portable SHA256: $($portableHash.Hash)"
} finally {
    if (-not $packageReady) {
        foreach ($failedArtifact in @($portableZip, $portableChecksum, $portableCandidate)) {
            if (Test-Path -LiteralPath $failedArtifact) {
                Remove-Item -LiteralPath $failedArtifact -Force
            }
        }
        if ($sidecarInstalledForBuild -and (Test-Path -LiteralPath $sidecarBundle -PathType Container)) {
            Remove-Item -LiteralPath $sidecarBundle -Recurse -Force
        }
        if ($hadSidecarBundle -and (Test-Path -LiteralPath $sidecarBackup -PathType Container)) {
            Move-Item -LiteralPath $sidecarBackup -Destination $sidecarBundle
        }
    }
    if ($hadPythonBuildInfo) {
        Write-Utf8NoBom -Path $pythonBuildInfo -Content $previousPythonBuildInfo
    } elseif (Test-Path -LiteralPath $pythonBuildInfo) {
        Remove-Item -LiteralPath $pythonBuildInfo -Force
    }
    if ($hadTauriBuildInfo) {
        Write-Utf8NoBom -Path $tauriBuildInfo -Content $previousTauriBuildInfo
    } elseif (Test-Path -LiteralPath $tauriBuildInfo) {
        Remove-Item -LiteralPath $tauriBuildInfo -Force
    }
    Pop-Location
}

