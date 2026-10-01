param(
    [Parameter(Mandatory = $true)][string]$Archive,
    [Parameter(Mandatory = $true)][string]$DestinationRoot
)

$ErrorActionPreference = "Stop"
$archivePath = (Resolve-Path -LiteralPath $Archive).Path
$destination = [IO.Path]::GetFullPath($DestinationRoot)
if (-not (Test-Path -LiteralPath $archivePath -PathType Leaf)) {
    throw "Update archive is missing."
}
$running = @(Get-Process -Name "OpenThesis", "openthesis-sidecar" -ErrorAction SilentlyContinue)
if ($running.Count -gt 0) {
    $details = ($running | ForEach-Object { "$($_.ProcessName) (PID $($_.Id))" }) -join ", "
    throw "OpenThesis is still running: $details. Close it normally before installing the update."
}
$parent = Split-Path -Parent $destination
if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
    New-Item -ItemType Directory -Path $parent | Out-Null
}
$staging = Join-Path $parent (".openthesis-update-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $staging | Out-Null
try {
    Expand-Archive -LiteralPath $archivePath -DestinationPath $staging
    $roots = @(Get-ChildItem -LiteralPath $staging -Directory)
    if ($roots.Count -ne 1 -or $roots[0].Name -notmatch '^OpenThesis-\d+\.\d+\.\d+$') {
        throw "The update archive does not contain one versioned OpenThesis directory."
    }
    if (Test-Path -LiteralPath $destination) {
        throw "The destination version already exists; it was not overwritten."
    }
    Move-Item -LiteralPath $roots[0].FullName -Destination $destination
} finally {
    if (Test-Path -LiteralPath $staging) {
        Remove-Item -LiteralPath $staging -Recurse -Force
    }
}
