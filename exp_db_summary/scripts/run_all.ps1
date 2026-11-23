# Regenerate every offline database summary (stage 1).
#
#   powershell -File scripts\run_all.ps1
#   powershell -File scripts\run_all.ps1 -Python C:\ProgramData\miniconda3\python.exe
#
# BIRD is read directly from its .sqlite files; Spider2.0 local databases are
# extracted once from the shipped zip into outputs/cache (no dataset file is
# modified) and reused on later runs.

param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Push-Location $here
try {
    $configs = @(
        "configs\bird_dev.yaml",
        "configs\bird_train.yaml",
        "configs\bird_minidev.yaml",
        "configs\spider2_lite_local.yaml"
    )
    foreach ($cfg in $configs) {
        Write-Host "[build] $cfg"
        & $Python -u scripts/build_db_summary.py --config $cfg
        if ($LASTEXITCODE -ne 0) { throw "build failed for $cfg" }
    }
    Write-Host "done. artifacts: outputs/db_summary/*.json"
}
finally {
    Pop-Location
}
