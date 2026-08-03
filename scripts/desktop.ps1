$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "common.ps1")
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$desktopRoot = Join-Path $projectRoot "desktop"
$nodeRoot = "D:\OpenThesisToolchain\node-v24.15.0-win-x64"
$node = Join-Path $nodeRoot "node.exe"
$npm = Join-Path $nodeRoot "npm.cmd"
$cargoBin = "D:\OpenThesisToolchain\cargo\bin"
$vsDevCmd = "D:\OpenThesisToolchain\vs\Common7\Tools\VsDevCmd.bat"
$python = Resolve-OpenThesisPython

foreach ($required in @($node, $npm, $vsDevCmd, $python)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required desktop tool is missing: $required"
    }
}

$action = if ($args.Count -gt 0) { $args[0] } else { "dev" }
if ($action -notin @("dev", "build")) {
    throw "Usage: scripts\desktop.ps1 [dev|build]"
}

$npmCommand = if ($action -eq "dev") { "run tauri dev" } else { "run tauri build" }
$dataDir = if ($env:OPENTHESIS_DATA_DIR) {
    $env:OPENTHESIS_DATA_DIR
} else {
    Join-Path $projectRoot ".test-tauri-data"
}
$command = @(
    "call `"$vsDevCmd`" -arch=x64 -host_arch=x64 >nul",
    "set `"CARGO_HOME=D:\OpenThesisToolchain\cargo`"",
    "set `"RUSTUP_HOME=D:\OpenThesisToolchain\rustup`"",
    "set `"CARGO_TARGET_DIR=D:\OpenThesisToolchain\cargo-target\openthesis`"",
    "set `"OPENTHESIS_PYTHON=$python`"",
    "set `"OPENTHESIS_DATA_DIR=$dataDir`"",
    "set `"PATH=$nodeRoot;$cargoBin;%PATH%`"",
    "cd /d `"$desktopRoot`"",
    "call `"$npm`" $npmCommand"
) -join " && "

& cmd.exe /d /c $command
if ($LASTEXITCODE -ne 0) {
    throw "Desktop $action failed with exit code $LASTEXITCODE"
}
