param(
    [Parameter(Mandatory = $true)][string]$Executable,
    [ValidateRange(1, 20)][int]$Attempts = 3
)

$ErrorActionPreference = "Stop"

$resolvedExecutable = (Resolve-Path -LiteralPath $Executable).Path
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
if (-not $resolvedExecutable.StartsWith($projectRoot + [IO.Path]::DirectorySeparatorChar)) {
    throw "Runtime smoke executable must be inside the OpenThesis workspace."
}

function Get-ProcessDescendants {
    param([Parameter(Mandatory = $true)][int]$RootProcessId)

    $all = @(Get-CimInstance Win32_Process -ErrorAction Stop)
    $knownParents = @([uint32]$RootProcessId)
    $descendants = @()
    do {
        $next = @($all | Where-Object {
            $_.ParentProcessId -in $knownParents -and
            $_.ProcessId -notin @($descendants.ProcessId)
        })
        if ($next.Count -eq 0) {
            break
        }
        $descendants += $next
        $knownParents = @($next | ForEach-Object { [uint32]$_.ProcessId })
    } while ($knownParents.Count -gt 0)
    return @($descendants)
}

function Stop-ProcessTree {
    param([Parameter(Mandatory = $true)][int]$RootProcessId)

    $descendants = @(Get-ProcessDescendants -RootProcessId $RootProcessId)
    foreach ($item in ($descendants | Sort-Object @{Expression = { $_.ParentProcessId }; Descending = $true})) {
        Stop-Process -Id ([int]$item.ProcessId) -Force -ErrorAction SilentlyContinue
    }
    Stop-Process -Id $RootProcessId -Force -ErrorAction SilentlyContinue
}

function Get-BoundedStderrSummary {
    param([string[]]$Paths)

    $parts = @()
    foreach ($path in $Paths) {
        if (-not $path -or -not (Test-Path -LiteralPath $path -PathType Leaf)) {
            continue
        }
        $text = (Get-Content -LiteralPath $path -Raw -ErrorAction SilentlyContinue)
        if ($null -eq $text) {
            continue
        }
        $text = (($text -replace "\s+", " ").Trim())
        if ($text.Length -gt 800) {
            $text = $text.Substring(0, 800)
        }
        if ($text) {
            $parts += $text
        }
    }
    if ($parts.Count -eq 0) {
        return "(no probe stderr)"
    }
    return ($parts -join " | ")
}

function Invoke-JsonProbe {
    param(
        [Parameter(Mandatory = $true)][string]$ProbeExecutable,
        [Parameter(Mandatory = $true)][string]$Payload,
        [Parameter(Mandatory = $true)][string]$Label,
        [Parameter(Mandatory = $true)][hashtable]$ProbeFiles,
        [string[]]$ArgumentList = @()
    )

    Set-Content -LiteralPath $ProbeFiles.Input -Value $Payload -NoNewline -Encoding ascii
    $probeProcess = $null
    try {
        $startParameters = @{
            FilePath = $ProbeExecutable
            RedirectStandardInput = $ProbeFiles.Input
            RedirectStandardOutput = $ProbeFiles.Output
            RedirectStandardError = $ProbeFiles.Error
            WindowStyle = "Hidden"
            PassThru = $true
        }
        if ($ArgumentList.Count -gt 0) {
            $startParameters.ArgumentList = $ArgumentList
        }
        $probeProcess = Start-Process @startParameters
        if (-not $probeProcess.WaitForExit(10000)) {
            throw "$Label probe timed out."
        }
        # A timed WaitForExit can report completion before the redirected
        # streams/process state have been synchronized.  Drain the completed
        # process once more before reading ExitCode; otherwise a successful
        # zero exit may be observed as null and misclassified as a failure.
        $probeProcess.WaitForExit()
        $probeProcess.Refresh()
        $exitCode = $null
        try {
            $exitCode = $probeProcess.ExitCode
        } catch {
            $exitCode = $null
        }
        if ($null -ne $exitCode -and $exitCode -ne 0) {
            throw "$Label probe exited with code $exitCode."
        }
        $exitStatus = if ($null -eq $exitCode) { "unavailable" } else { [string]$exitCode }
        if (-not (Test-Path -LiteralPath $ProbeFiles.Output -PathType Leaf)) {
            throw "$Label probe returned no response (exit_code=$exitStatus)."
        }
        try {
            return (Get-Content -LiteralPath $ProbeFiles.Output -Raw | ConvertFrom-Json -ErrorAction Stop)
        } catch {
            throw "$Label probe returned invalid JSON (exit_code=$exitStatus)."
        }
    } finally {
        if ($probeProcess -and -not $probeProcess.HasExited) {
            Stop-Process -Id $probeProcess.Id -Force -ErrorAction SilentlyContinue
            $probeProcess.WaitForExit(5000) | Out-Null
        }
    }
}

function Invoke-RuntimeAttempt {
    param(
        [Parameter(Mandatory = $true)][int]$Attempt,
        [Parameter(Mandatory = $true)][string]$DataDirectory
    )

    $startedAt = [DateTime]::UtcNow
    $process = $null
    $helloFiles = @{
        Input = Join-Path $env:TEMP ("openthesis-sidecar-hello-$([guid]::NewGuid().ToString('N')).json")
        Output = Join-Path $env:TEMP ("openthesis-sidecar-hello-$([guid]::NewGuid().ToString('N')).out")
        Error = Join-Path $env:TEMP ("openthesis-sidecar-hello-$([guid]::NewGuid().ToString('N')).err")
    }
    $gatewayFiles = @{
        Input = Join-Path $env:TEMP ("openthesis-model-gateway-$([guid]::NewGuid().ToString('N')).json")
        Output = Join-Path $env:TEMP ("openthesis-model-gateway-$([guid]::NewGuid().ToString('N')).out")
        Error = Join-Path $env:TEMP ("openthesis-model-gateway-$([guid]::NewGuid().ToString('N')).err")
    }
    try {
        New-Item -ItemType Directory -Force -Path $DataDirectory | Out-Null
        $process = Start-Process -FilePath $resolvedExecutable -WindowStyle Hidden -PassThru
        $deadline = [DateTime]::UtcNow.AddSeconds(20)
        $descendants = @()
        do {
            Start-Sleep -Milliseconds 250
            $process.Refresh()
            if ($process.HasExited) {
                throw "Portable OpenThesis exited during startup."
            }
            $descendants = @(Get-ProcessDescendants -RootProcessId $process.Id)
            $sidecar = @($descendants | Where-Object Name -eq "openthesis-sidecar.exe")
        } while ($sidecar.Count -eq 0 -and [DateTime]::UtcNow -lt $deadline)

        if ($sidecar.Count -ne 1) {
            throw "Portable OpenThesis did not start exactly one research sidecar."
        }
        $helloSidecar = Join-Path (Split-Path $resolvedExecutable -Parent) "bin\openthesis-sidecar\openthesis-sidecar.exe"
        if (-not (Test-Path -LiteralPath $helloSidecar -PathType Leaf)) {
            throw "Portable OpenThesis sidecar executable is missing."
        }
        $helloResponse = Invoke-JsonProbe `
            -ProbeExecutable $helloSidecar `
            -Payload '{"jsonrpc":"2.0","id":"runtime-hello","method":"system.hello","params":{}}' `
            -Label "system.hello" -ProbeFiles $helloFiles
        if ($helloResponse.jsonrpc -ne "2.0" -or
            $helloResponse.id -ne "runtime-hello" -or
            $helloResponse.result.contract_version -ne "2.0") {
            throw "Portable OpenThesis sidecar returned an invalid system.hello protocol response."
        }

        $gatewayResponse = Invoke-JsonProbe `
            -ProbeExecutable $resolvedExecutable `
            -Payload '{"operation":"unknown","configured_model_id":"none","system_prompt":"","user_prompt":"","json_mode":true}' `
            -Label "model-gateway" -ProbeFiles $gatewayFiles `
            -ArgumentList @("--model-gateway")
        if ($gatewayResponse.ok -ne $false -or
            $gatewayResponse.error.code -ne "MODEL_GATEWAY_PROTOCOL_ERROR") {
            throw "Portable OpenThesis model-gateway command returned an invalid protocol response."
        }

        $process.Refresh()
        if ($process.HasExited) {
            throw "Portable OpenThesis exited before protocol probes completed."
        }
        $descendants = @(Get-ProcessDescendants -RootProcessId $process.Id)
        $sidecar = @($descendants | Where-Object Name -eq "openthesis-sidecar.exe")
        if ($sidecar.Count -ne 1) {
            throw "Portable OpenThesis did not keep exactly one research sidecar alive."
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
            Attempt = $Attempt
            Status = "PASS"
            MainProcess = $process.Id
            SidecarProcess = $sidecar[0].ProcessId
            ElapsedSeconds = [Math]::Round(([DateTime]::UtcNow - $startedAt).TotalSeconds, 3)
            DataDirectory = $DataDirectory
        }
    } catch {
        $mainExitCode = "not-exited"
        if ($process) {
            try {
                $process.Refresh()
                if ($process.HasExited) {
                    $mainExitCode = [string]$process.ExitCode
                }
            } catch {
                $mainExitCode = "unavailable"
            }
        }
        $stderr = Get-BoundedStderrSummary -Paths @($helloFiles.Error, $gatewayFiles.Error)
        throw "attempt=$Attempt main_exit_code=$mainExitCode probe_stderr=$stderr error=$($_.Exception.Message)"
    } finally {
        if ($process) {
            try {
                # Also reap an orphaned sidecar when the main process has
                # already exited; a failed round must not leave descendants.
                Stop-ProcessTree -RootProcessId $process.Id
            } catch {
                Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
            }
        }
        foreach ($probeFile in @($helloFiles.Input, $helloFiles.Output, $helloFiles.Error, $gatewayFiles.Input, $gatewayFiles.Output, $gatewayFiles.Error)) {
            if ($probeFile -and (Test-Path -LiteralPath $probeFile)) {
                Remove-Item -LiteralPath $probeFile -Force -ErrorAction SilentlyContinue
            }
        }
    }
}

$previousDataDir = $env:OPENTHESIS_DATA_DIR
$previousDataDirPresent = Test-Path Env:OPENTHESIS_DATA_DIR
$results = @()
$failures = @()
try {
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        $attemptDataDir = Join-Path $projectRoot ("build\portable-runtime-data\attempt-{0}-{1}" -f $attempt, [guid]::NewGuid().ToString("N"))
        $env:OPENTHESIS_DATA_DIR = $attemptDataDir
        try {
            $result = Invoke-RuntimeAttempt -Attempt $attempt -DataDirectory $attemptDataDir
            $results += $result
            Write-Output ($result | ConvertTo-Json -Compress)
        } catch {
            $failures += $_.Exception.Message
            # Keep all configured attempts running so the final aggregate
            # reports every cold-start failure instead of stopping at attempt 1.
            Write-Warning $_.Exception.Message
        } finally {
            if ($previousDataDirPresent) {
                $env:OPENTHESIS_DATA_DIR = $previousDataDir
            } else {
                Remove-Item Env:OPENTHESIS_DATA_DIR -ErrorAction SilentlyContinue
            }
        }
    }
    if ($failures.Count -gt 0) {
        throw ("Desktop runtime verification failed: " + ($failures -join " || "))
    }
    [pscustomobject]@{
        Attempts = $Attempts
        Passed = $results.Count
        Results = $results
    } | ConvertTo-Json -Compress
} finally {
    if ($previousDataDirPresent) {
        $env:OPENTHESIS_DATA_DIR = $previousDataDir
    } else {
        Remove-Item Env:OPENTHESIS_DATA_DIR -ErrorAction SilentlyContinue
    }
}
