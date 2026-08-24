param(
    [string]$Version = "2.0.1",
    [string]$CargoTarget = "D:\OpenThesisToolchain\cargo-target\openthesis"
)

$ErrorActionPreference = "Stop"

function Get-PeSubsystem([string]$Path) {
    $bytes = [IO.File]::ReadAllBytes($Path)
    if ($bytes.Length -lt 256 -or $bytes[0] -ne 0x4d -or $bytes[1] -ne 0x5a) {
        throw "Not a valid PE executable: $Path"
    }
    $peOffset = [BitConverter]::ToInt32($bytes, 0x3c)
    $optionalHeader = $peOffset + 24
    if ($optionalHeader + 70 -gt $bytes.Length) {
        throw "PE optional header is truncated: $Path"
    }
    return [BitConverter]::ToUInt16($bytes, $optionalHeader + 68)
}

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$output = Join-Path $projectRoot "installer-output"
$zip = Join-Path $output "OpenThesis-$Version-windows-x64-portable.zip"
$checksum = "$zip.sha256"
$releaseExe = Join-Path $CargoTarget "release\openthesis-desktop.exe"

if (-not (Test-Path -LiteralPath $zip -PathType Leaf)) {
    throw "Portable ZIP is missing: $zip"
}
if (-not (Test-Path -LiteralPath $checksum -PathType Leaf)) {
    throw "Portable checksum is missing: $checksum"
}
if (-not (Test-Path -LiteralPath $releaseExe -PathType Leaf)) {
    throw "Release executable is missing: $releaseExe"
}
if ((Get-PeSubsystem $releaseExe) -ne 2) {
    throw "OpenThesis release executable is not a Windows GUI application."
}

$expectedHash = ((Get-Content -LiteralPath $checksum -Raw) -split "\s+")[0]
$actualHash = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash
if (-not $expectedHash -or $expectedHash -ne $actualHash) {
    throw "Portable ZIP SHA-256 does not match its checksum file."
}

Add-Type -AssemblyName System.IO.Compression.FileSystem
$archive = [IO.Compression.ZipFile]::OpenRead($zip)
try {
    $entries = @($archive.Entries | ForEach-Object { $_.FullName.Replace("\", "/") })
    foreach ($required in @(
        "OpenThesis/OpenThesis.exe",
        "OpenThesis/bin/openthesis-sidecar/openthesis-sidecar.exe"
    )) {
        if ($required -notin $entries) {
            throw "Portable ZIP is missing required entry: $required"
        }
    }
    if (-not ($entries | Where-Object {
        $_ -like "OpenThesis/bin/openthesis-sidecar/_internal/python*.dll"
    })) {
        throw "Portable ZIP is missing the sidecar Python runtime."
    }
} finally {
    $archive.Dispose()
}

[pscustomobject]@{
    PortableZip = $zip
    Sha256 = $actualHash
    MainSubsystem = "Windows GUI"
    RequiredEntries = "present"
} | ConvertTo-Json -Compress

