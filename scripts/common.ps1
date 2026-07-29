function Resolve-OpenThesisPython {
    if ($env:OPENTHESIS_PYTHON) {
        $configured = $env:OPENTHESIS_PYTHON
        if (-not (Test-Path -LiteralPath $configured -PathType Leaf)) {
            throw "OPENTHESIS_PYTHON does not point to a Python executable."
        }
        return (Resolve-Path -LiteralPath $configured).Path
    }

    $command = Get-Command python -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    throw "Python 3.11+ was not found. Install Python or set OPENTHESIS_PYTHON to python.exe."
}

function Initialize-OpenThesisTk {
    param(
        [Parameter(Mandatory = $true)][string]$Python,
        [Parameter(Mandatory = $true)][string]$ProjectRoot
    )

    $runtimeRoot = Join-Path $ProjectRoot ".runtime\tcl"
    if (-not (Test-Path -LiteralPath $runtimeRoot)) {
        $pythonRoot = Split-Path -Parent $Python
        $source = Join-Path $pythonRoot "tcl"
        if (-not (Test-Path -LiteralPath $source -PathType Container)) {
            throw "The selected Python installation does not contain Tcl/Tk: $source"
        }
        New-Item -ItemType Directory -Path (Split-Path $runtimeRoot) -Force | Out-Null
        Copy-Item -LiteralPath $source -Destination $runtimeRoot -Recurse
    }

    $env:TCL_LIBRARY = Join-Path $runtimeRoot "tcl8.6"
    $env:TK_LIBRARY = Join-Path $runtimeRoot "tk8.6"
}
