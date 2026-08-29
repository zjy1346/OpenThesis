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
    $helloInput = Join-Path $env:TEMP ("openthesis-sidecar-hello-" + [guid]::NewGuid().ToString("N") + ".json")
    $helloOutput = Join-Path $env:TEMP ("openthesis-sidecar-hello-" + [guid]::NewGuid().ToString("N") + ".out")
    $helloError = Join-Path $env:TEMP ("openthesis-sidecar-hello-" + [guid]::NewGuid().ToString("N") + ".err")
    $helloProcess = $null
    try {
        $helloSidecar = Join-Path (Split-Path $resolvedExecutable -Parent) "bin\openthesis-sidecar\openthesis-sidecar.exe"
        if (-not (Test-Path -LiteralPath $helloSidecar -PathType Leaf)) {
            throw "Portable OpenThesis sidecar executable is missing: $helloSidecar"
        }
        Set-Content -LiteralPath $helloInput -Value '{"jsonrpc":"2.0","id":"runtime-hello","method":"system.hello","params":{}}' -NoNewline -Encoding ascii
        $helloProcess = Start-Process -FilePath $helloSidecar -RedirectStandardInput $helloInput -RedirectStandardOutput $helloOutput -RedirectStandardError $helloError -WindowStyle Hidden -PassThru
        if (-not $helloProcess.WaitForExit(10000)) {
            throw "Portable OpenThesis sidecar did not complete the system.hello probe."
        }
        $helloProcess.Refresh()
        $helloExitCode = $helloProcess.ExitCode
        if ($null -ne $helloExitCode -and $helloExitCode -ne 0) {
            throw "Portable OpenThesis sidecar exited during the system.hello probe with code $helloExitCode."
        }
        if (-not (Test-Path -LiteralPath $helloOutput -PathType Leaf)) {
            throw "Portable OpenThesis sidecar returned no system.hello response."
        }
        try {
            $helloResponse = Get-Content -LiteralPath $helloOutput -Raw | ConvertFrom-Json -ErrorAction Stop
        } catch {
            throw "Portable OpenThesis sidecar returned invalid system.hello JSON."
        }
        if ($helloResponse.jsonrpc -ne "2.0" -or $helloResponse.id -ne "runtime-hello" -or $helloResponse.result.contract_version -ne "2.0") {
            throw "Portable OpenThesis sidecar returned an invalid system.hello protocol response."
        }
    } finally {
        if ($helloProcess -and -not $helloProcess.HasExited) {
            Stop-Process -Id $helloProcess.Id -Force -ErrorAction SilentlyContinue
            $helloProcess.WaitForExit(5000) | Out-Null
        }
        foreach ($probeFile in @($helloInput, $helloOutput, $helloError)) {
            if ($probeFile -and (Test-Path -LiteralPath $probeFile)) {
                Remove-Item -LiteralPath $probeFile -Force -ErrorAction SilentlyContinue
            }
        }
    }
    $gatewayInput = Join-Path $env:TEMP ("openthesis-model-gateway-" + [guid]::NewGuid().ToString("N") + ".json")
    $gatewayOutput = Join-Path $env:TEMP ("openthesis-model-gateway-" + [guid]::NewGuid().ToString("N") + ".out")
    $gatewayError = Join-Path $env:TEMP ("openthesis-model-gateway-" + [guid]::NewGuid().ToString("N") + ".err")
    $gatewayProcess = $null
    try {
        Set-Content -LiteralPath $gatewayInput -Value '{"operation":"unknown","configured_model_id":"none","system_prompt":"","user_prompt":"","json_mode":true}' -NoNewline -Encoding ascii
        $gatewayProcess = Start-Process -FilePath $resolvedExecutable -ArgumentList "--model-gateway" -RedirectStandardInput $gatewayInput -RedirectStandardOutput $gatewayOutput -RedirectStandardError $gatewayError -WindowStyle Hidden -PassThru
        if (-not $gatewayProcess.WaitForExit(10000)) {
            throw "Portable OpenThesis model-gateway command did not complete."
        }
        $gatewayProcess.Refresh()
        $gatewayExitCode = $gatewayProcess.ExitCode
        if ($null -ne $gatewayExitCode -and $gatewayExitCode -ne 0) {
            throw "Portable OpenThesis model-gateway command exited with code $gatewayExitCode."
        }
        if (-not (Test-Path -LiteralPath $gatewayOutput -PathType Leaf)) {
            throw "Portable OpenThesis model-gateway command returned no response."
        }
        try {
            $gatewayResponse = Get-Content -LiteralPath $gatewayOutput -Raw | ConvertFrom-Json -ErrorAction Stop
        } catch {
            throw "Portable OpenThesis model-gateway command returned invalid JSON."
        }
        if ($gatewayResponse.ok -ne $false -or $gatewayResponse.error.code -ne "MODEL_GATEWAY_PROTOCOL_ERROR") {
            throw "Portable OpenThesis model-gateway command returned an invalid protocol response."
        }
    } finally {
        if ($gatewayProcess -and -not $gatewayProcess.HasExited) {
            Stop-Process -Id $gatewayProcess.Id -Force -ErrorAction SilentlyContinue
            $gatewayProcess.WaitForExit(5000) | Out-Null
        }
        foreach ($probeFile in @($gatewayInput, $gatewayOutput, $gatewayError)) {
            if ($probeFile -and (Test-Path -LiteralPath $probeFile)) {
                Remove-Item -LiteralPath $probeFile -Force -ErrorAction SilentlyContinue
            }
        }
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
