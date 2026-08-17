$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$version = "1.5.1"

Push-Location $projectRoot
try {
    & (Join-Path $PSScriptRoot "test.ps1")
    if ($LASTEXITCODE -ne 0) {
        throw "Tests failed before packaging"
    }

    & (Join-Path $PSScriptRoot "build.ps1")
    if ($LASTEXITCODE -ne 0) {
        throw "Build failed before packaging"
    }

    $bundle = Join-Path $projectRoot "dist\OpenThesis"
    $executable = Join-Path $bundle "OpenThesis.exe"

    $env:OPENTHESIS_SMOKE_TEST = "1"
    $deterministic = Start-Process -FilePath $executable -PassThru -Wait -WindowStyle Hidden
    Remove-Item Env:\OPENTHESIS_SMOKE_TEST
    if ($deterministic.ExitCode -ne 0) {
        throw "Packaged deterministic smoke test failed"
    }

    foreach ($language in @("zh-CN", "en")) {
        $env:OPENTHESIS_GUI_SMOKE_TEST = "1"
        $env:OPENTHESIS_UI_LANGUAGE = $language
        $env:OPENTHESIS_REPORT_LANGUAGE = $language
        $env:OPENTHESIS_REDUCE_MOTION = "0"
        $env:OPENTHESIS_DATA_DIR = Join-Path $projectRoot ".test-frozen-data-$language"
        $gui = Start-Process -FilePath $executable -PassThru -Wait
        Remove-Item Env:\OPENTHESIS_GUI_SMOKE_TEST
        Remove-Item Env:\OPENTHESIS_UI_LANGUAGE
        Remove-Item Env:\OPENTHESIS_REPORT_LANGUAGE
        Remove-Item Env:\OPENTHESIS_REDUCE_MOTION
        Remove-Item Env:\OPENTHESIS_DATA_DIR
        if ($gui.ExitCode -ne 0) {
            throw "Packaged GUI smoke test failed for $language"
        }
    }

    Copy-Item -LiteralPath (Join-Path $projectRoot "README.md") -Destination $bundle -Force
    Copy-Item -LiteralPath (Join-Path $projectRoot "README.zh-CN.md") -Destination $bundle -Force
    Copy-Item -LiteralPath (Join-Path $projectRoot "LICENSE") -Destination $bundle -Force
    Copy-Item -LiteralPath (Join-Path $projectRoot "docs\PROJECT_SPEC.md") -Destination $bundle -Force
    Set-Content -LiteralPath (Join-Path $bundle "VERSION.txt") -Value $version -Encoding utf8

    $output = Join-Path $projectRoot "installer-output"
    New-Item -ItemType Directory -Path $output -Force | Out-Null
    $archive = Join-Path $output "OpenThesis-$version-windows-x64-portable.zip"
    Compress-Archive -LiteralPath $bundle -DestinationPath $archive -CompressionLevel Optimal -Force
    $hash = Get-FileHash -Algorithm SHA256 -LiteralPath $archive
    Set-Content -LiteralPath "$archive.sha256" -Value "$($hash.Hash)  $([IO.Path]::GetFileName($archive))" -Encoding ascii

    Write-Output "Executable: $executable"
    Write-Output "Portable package: $archive"
    Write-Output "SHA256: $($hash.Hash)"
} finally {
    Remove-Item Env:\OPENTHESIS_SMOKE_TEST -ErrorAction SilentlyContinue
    Remove-Item Env:\OPENTHESIS_GUI_SMOKE_TEST -ErrorAction SilentlyContinue
    Remove-Item Env:\OPENTHESIS_UI_LANGUAGE -ErrorAction SilentlyContinue
    Remove-Item Env:\OPENTHESIS_REPORT_LANGUAGE -ErrorAction SilentlyContinue
    Remove-Item Env:\OPENTHESIS_REDUCE_MOTION -ErrorAction SilentlyContinue
    Remove-Item Env:\OPENTHESIS_DATA_DIR -ErrorAction SilentlyContinue
    Pop-Location
}
