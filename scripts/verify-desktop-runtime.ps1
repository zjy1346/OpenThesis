param(
    [Parameter(Mandatory = $true)][string]$Executable,
    [ValidateRange(1, 20)][int]$Attempts = 3
)

$ErrorActionPreference = "Stop"

$resolvedExecutable = (Resolve-Path -LiteralPath $Executable).Path
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$versionLine = Select-String -LiteralPath (Join-Path $projectRoot "pyproject.toml") -Pattern '^version\s*=\s*"(\d+\.\d+\.\d+)"' | Select-Object -First 1
if (-not $versionLine) {
    throw "Unable to read the canonical package version for runtime verification."
}
$expectedRuntimeVersion = $versionLine.Matches[0].Groups[1].Value
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

function Start-PersistentSidecar {
    param(
        [Parameter(Mandatory = $true)][string]$SidecarExecutable,
        [Parameter(Mandatory = $true)][string]$DataDirectory
    )

    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $SidecarExecutable
    $startInfo.WorkingDirectory = Split-Path -Parent $SidecarExecutable
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardInput = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.EnvironmentVariables["OPENTHESIS_DATA_DIR"] = $DataDirectory
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    if (-not $process.Start()) {
        throw "Packaged sidecar did not start for the persistent-report smoke check."
    }
    $stderrTask = $process.StandardError.ReadToEndAsync()
    return [pscustomobject]@{ Process = $process; StderrTask = $stderrTask }
}

function Invoke-PersistentSidecarRpc {
    param(
        [Parameter(Mandatory = $true)]$Sidecar,
        [Parameter(Mandatory = $true)][string]$Method,
        [Parameter(Mandatory = $true)]$Params,
        [Parameter(Mandatory = $true)][string]$RequestId
    )

    $payload = @{
        jsonrpc = "2.0"
        id = $RequestId
        method = $Method
        params = $Params
    } | ConvertTo-Json -Depth 40 -Compress
    $Sidecar.Process.StandardInput.WriteLine($payload)
    $Sidecar.Process.StandardInput.Flush()
    $responseTask = $Sidecar.Process.StandardOutput.ReadLineAsync()
    if (-not $responseTask.Wait(15000)) {
        throw "Packaged sidecar RPC $Method timed out."
    }
    $line = $responseTask.Result
    if (-not $line) {
        $stderr = ""
        if ($Sidecar.StderrTask.IsCompleted) {
            $stderr = (($Sidecar.StderrTask.Result -replace "\s+", " ").Trim())
        }
        throw "Packaged sidecar RPC $Method returned no JSON response. $stderr"
    }
    try {
        $response = $line | ConvertFrom-Json -ErrorAction Stop
    } catch {
        throw "Packaged sidecar RPC $Method returned invalid JSON."
    }
    if ($response.id -ne $RequestId -or $response.jsonrpc -ne "2.0") {
        throw "Packaged sidecar RPC $Method returned a mismatched response identity."
    }
    if ($response.error) {
        throw "Packaged sidecar RPC $Method failed: $($response.error.message)"
    }
    return $response.result
}

function Stop-PersistentSidecar {
    param($Sidecar)

    if (-not $Sidecar -or -not $Sidecar.Process) {
        return
    }
    try {
        if (-not $Sidecar.Process.HasExited) {
            $Sidecar.Process.StandardInput.Close()
            if (-not $Sidecar.Process.WaitForExit(10000)) {
                $Sidecar.Process.Kill()
                $Sidecar.Process.WaitForExit(5000) | Out-Null
            }
        }
    } catch {
        try { $Sidecar.Process.Kill() } catch { }
    }
    $Sidecar.Process.Dispose()
}

function Invoke-PersistentReportReadSmoke {
    param([Parameter(Mandatory = $true)][string]$SidecarExecutable)

    $smokeRoot = Join-Path $projectRoot "build\portable-report-smoke"
    $dataDirectory = Join-Path $smokeRoot ([guid]::NewGuid().ToString("N"))
    $resolvedSmokeRoot = [IO.Path]::GetFullPath($smokeRoot + [IO.Path]::DirectorySeparatorChar)
    $resolvedDataDirectory = [IO.Path]::GetFullPath($dataDirectory)
    if (-not $resolvedDataDirectory.StartsWith($resolvedSmokeRoot, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Persistent report smoke data directory escaped its isolated build location."
    }

    $sidecar = $null
    $restartedSidecar = $null
    try {
        New-Item -ItemType Directory -Force -Path $dataDirectory | Out-Null
        $sidecar = Start-PersistentSidecar -SidecarExecutable $SidecarExecutable -DataDirectory $dataDirectory
        $started = Invoke-PersistentSidecarRpc -Sidecar $sidecar -Method "research.start" -Params @{ mode = "demo" } -RequestId "report-smoke-start"
        if (-not $started.job_id) {
            throw "Packaged demo research did not return a job id."
        }

        $deadline = [DateTime]::UtcNow.AddSeconds(60)
        do {
            Start-Sleep -Milliseconds 150
            $status = Invoke-PersistentSidecarRpc -Sidecar $sidecar -Method "research.status" -Params @{ job_id = $started.job_id } -RequestId "report-smoke-status"
            if ($status.state -in @("failed", "cancelled")) {
                throw "Packaged demo research ended in state $($status.state)."
            }
        } while ($status.state -notin @("completed", "partial") -and [DateTime]::UtcNow -lt $deadline)
        if ($status.state -notin @("completed", "partial") -or -not $status.run_id) {
            throw "Packaged demo research did not reach a readable terminal state."
        }

        $runId = [string]$status.run_id
        $listed = @(Invoke-PersistentSidecarRpc -Sidecar $sidecar -Method "research.list" -Params @{ limit = 50 } -RequestId "report-smoke-list" | Where-Object { $_.run_id -eq $runId })
        if ($listed.Count -ne 1) {
            throw "Packaged research.list did not return the newly persisted demo run exactly once."
        }
        $firstReport = Invoke-PersistentSidecarRpc -Sidecar $sidecar -Method "research.get_report" -Params @{ run_id = $runId; language = "en" } -RequestId "report-smoke-get-first"
        if ($firstReport.run_id -ne $runId -or
            $firstReport.report_contract_version -ne "1" -or
            $firstReport.report_read_state -notin @("ready", "partial", "diagnostic_only") -or
            -not $firstReport.is_substantive -or
            -not $firstReport.visible_sections -or
            -not $firstReport.markdown -or
            -not $firstReport.html) {
            throw "Packaged research.get_report did not return a substantive versioned report."
        }
        $firstGeneration = [string]$firstReport.report_input_generation
        $firstMarkdown = [string]$firstReport.markdown
        Stop-PersistentSidecar $sidecar
        $sidecar = $null

        $restartedSidecar = Start-PersistentSidecar -SidecarExecutable $SidecarExecutable -DataDirectory $dataDirectory
        $restartedRuns = @(Invoke-PersistentSidecarRpc -Sidecar $restartedSidecar -Method "research.list" -Params @{ limit = 50 } -RequestId "report-smoke-restart-list" | Where-Object { $_.run_id -eq $runId })
        if ($restartedRuns.Count -ne 1) {
            throw "After sidecar restart, research.list did not recover the persisted demo run exactly once."
        }
        $restartedReport = Invoke-PersistentSidecarRpc -Sidecar $restartedSidecar -Method "research.get_report" -Params @{ run_id = $runId; language = "en" } -RequestId "report-smoke-get-restarted"
        if ($restartedReport.run_id -ne $runId -or
            $restartedReport.report_contract_version -ne "1" -or
            -not $restartedReport.is_substantive -or
            $restartedReport.report_input_generation -ne $firstGeneration -or
            $restartedReport.markdown -ne $firstMarkdown) {
            throw "After sidecar restart, the report identity or persisted content changed."
        }
        [pscustomobject]@{
            Status = "PASS"
            RunId = $runId
            ResearchState = $status.state
            ReadState = $restartedReport.report_read_state
            VisibleSections = @($restartedReport.visible_sections | Where-Object is_substantive).Count
            RestartStable = $true
        }
    } finally {
        Stop-PersistentSidecar $sidecar
        Stop-PersistentSidecar $restartedSidecar
        if (Test-Path -LiteralPath $resolvedDataDirectory -PathType Container) {
            Remove-Item -LiteralPath $resolvedDataDirectory -Recurse -Force
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
            $helloResponse.result.contract_version -ne "2.0" -or
            $helloResponse.result.app_version -ne $expectedRuntimeVersion -or
            $helloResponse.result.build_info.version -ne $expectedRuntimeVersion) {
            throw "Portable OpenThesis sidecar returned an invalid system.hello response or displayed build version."
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
    $portableSidecar = Join-Path (Split-Path -Parent $resolvedExecutable) "bin\openthesis-sidecar\openthesis-sidecar.exe"
    if (-not (Test-Path -LiteralPath $portableSidecar -PathType Leaf)) {
        throw "Packaged sidecar is missing for the persistent report read smoke check."
    }
    $persistentReportSmoke = Invoke-PersistentReportReadSmoke -SidecarExecutable $portableSidecar
    [pscustomobject]@{
        Attempts = $Attempts
        Passed = $results.Count
        Results = $results
        PersistentReportRead = $persistentReportSmoke
    } | ConvertTo-Json -Compress
} finally {
    if ($previousDataDirPresent) {
        $env:OPENTHESIS_DATA_DIR = $previousDataDir
    } else {
        Remove-Item Env:OPENTHESIS_DATA_DIR -ErrorAction SilentlyContinue
    }
}
