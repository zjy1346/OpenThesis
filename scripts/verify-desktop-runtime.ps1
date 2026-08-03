param(
    [Parameter(Mandatory = $true)][string]$Executable
)

$ErrorActionPreference = "Stop"

$resolvedExecutable = (Resolve-Path -LiteralPath $Executable).Path
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $resolvedExecutable.StartsWith($projectRoot + [IO.Path]::DirectorySeparatorChar)) {
    throw "Runtime smoke executable must be inside the OpenThesis workspace."
}

$previousDataDir = $env:OPENTHESIS_DATA_DIR
$env:OPENTHESIS_DATA_DIR = Join-Path $projectRoot (
    "build\portable-runtime-data\" + [guid]::NewGuid().ToString("N")
)
$process = $null
try {
    $process = Start-Process -FilePath $resolvedExecutable -WindowStyle Hidden -PassThru
    $deadline = [DateTime]::UtcNow.AddSeconds(20)
    $descendants = @()
    do {
        Start-Sleep -Milliseconds 250
        if ($process.HasExited) {
            throw "Portable OpenThesis exited during startup."
        }
        $all = @(Get-CimInstance Win32_Process)
        $knownParents = @([uint32]$process.Id)
        $descendants = @()
        do {
            $next = @($all | Where-Object {
                $_.ParentProcessId -in $knownParents -and
                $_.ProcessId -notin @($descendants.ProcessId)
            })
            $descendants += $next
            $knownParents = @($next | ForEach-Object { [uint32]$_.ProcessId })
        } while ($knownParents.Count -gt 0)
        $sidecar = @($descendants | Where-Object Name -eq "openthesis-sidecar.exe")
    } while ($sidecar.Count -eq 0 -and [DateTime]::UtcNow -lt $deadline)

    if ($sidecar.Count -ne 1) {
        throw "Portable OpenThesis did not start exactly one research sidecar."
    }
    $visibleConsoleHosts = @(
        $descendants |
            Where-Object Name -eq "conhost.exe" |
            ForEach-Object { Get-Process -Id $_.ProcessId -ErrorAction SilentlyContinue } |
            Where-Object { $_.MainWindowHandle -ne 0 }
    )
    if ($visibleConsoleHosts.Count -gt 0) {
        throw "Portable OpenThesis started a visible console host."
    }
    [pscustomobject]@{
        MainProcess = $process.Id
        SidecarProcess = $sidecar[0].ProcessId
        VisibleConsoleHosts = 0
        IsolatedDataDirectory = $env:OPENTHESIS_DATA_DIR
    } | ConvertTo-Json -Compress
} finally {
    if ($process -and -not $process.HasExited) {
        Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
        $process.WaitForExit(5000) | Out-Null
    }
    $env:OPENTHESIS_DATA_DIR = $previousDataDir
}
