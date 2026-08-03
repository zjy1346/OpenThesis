$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "common.ps1")
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$desktopRoot = Join-Path $projectRoot "desktop"
$localNodeRoot = "D:\OpenThesisToolchain\node-v24.15.0-win-x64"
$nodeRoot = if ($env:OPENTHESIS_NODE_ROOT) {
    $env:OPENTHESIS_NODE_ROOT
} elseif (Test-Path -LiteralPath (Join-Path $localNodeRoot "node.exe")) {
    $localNodeRoot
} else {
    Split-Path -Parent (Get-Command node -ErrorAction Stop).Source
}
$node = Join-Path $nodeRoot "node.exe"
$npm = Join-Path $nodeRoot "npm.cmd"
$localCargoBin = "D:\OpenThesisToolchain\cargo\bin"
$cargoBin = if (Test-Path -LiteralPath (Join-Path $localCargoBin "cargo.exe")) {
    $localCargoBin
} else {
    Split-Path -Parent (Get-Command cargo -ErrorAction Stop).Source
}
$localVsDevCmd = "D:\OpenThesisToolchain\vs\Common7\Tools\VsDevCmd.bat"
$vsDevCmd = if ($env:OPENTHESIS_VSDEVCMD) {
    $env:OPENTHESIS_VSDEVCMD
} elseif (Test-Path -LiteralPath $localVsDevCmd) {
    $localVsDevCmd
} else {
    ""
}
$python = Resolve-OpenThesisPython
$cargoHome = if ($env:CARGO_HOME) { $env:CARGO_HOME } elseif ($cargoBin -eq $localCargoBin) { "D:\OpenThesisToolchain\cargo" } else { "" }
$rustupHome = if ($env:RUSTUP_HOME) { $env:RUSTUP_HOME } elseif ($cargoBin -eq $localCargoBin) { "D:\OpenThesisToolchain\rustup" } else { "" }
$cargoTarget = if ($env:CARGO_TARGET_DIR) { $env:CARGO_TARGET_DIR } elseif ($cargoBin -eq $localCargoBin) { "D:\OpenThesisToolchain\cargo-target\openthesis" } else { Join-Path $projectRoot "build\cargo-target" }

foreach ($required in @($node, $npm, (Join-Path $cargoBin "cargo.exe"), $python)) {
    if (-not (Test-Path -LiteralPath $required)) {
        throw "Required desktop tool is missing: $required"
    }
}

$action = if ($args.Count -gt 0) { $args[0] } else { "dev" }
if ($action -notin @("dev", "build", "portable")) {
    throw "Usage: scripts\desktop.ps1 [dev|build|portable]"
}

$npmCommand = switch ($action) {
    "dev" { "run tauri dev" }
    "portable" { "run tauri build -- --no-bundle" }
    default { "run tauri build" }
}
$dataDir = if ($env:OPENTHESIS_DATA_DIR) {
    $env:OPENTHESIS_DATA_DIR
} else {
    Join-Path $projectRoot ".test-tauri-data"
}
$command = @(
    "set `"CARGO_TARGET_DIR=$cargoTarget`"",
    "set `"OPENTHESIS_PYTHON=$python`"",
    "set `"OPENTHESIS_DATA_DIR=$dataDir`"",
    "set `"PATH=$nodeRoot;$cargoBin;%PATH%`"",
    "cd /d `"$desktopRoot`"",
    "call `"$npm`" $npmCommand"
)
if ($rustupHome) {
    $command = @("set `"RUSTUP_HOME=$rustupHome`"") + $command
}
if ($cargoHome) {
    $command = @("set `"CARGO_HOME=$cargoHome`"") + $command
}
if ($vsDevCmd) {
    $command = @("call `"$vsDevCmd`" -arch=x64 -host_arch=x64 >nul") + $command
}
$command = $command -join " && "

& cmd.exe /d /c $command
if ($LASTEXITCODE -ne 0) {
    throw "Desktop $action failed with exit code $LASTEXITCODE"
}
