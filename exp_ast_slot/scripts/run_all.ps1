# Regenerate every artifact for this experiment: AST sets, role-slot templates,
# and the fidelity check.  Run from anywhere; the script cds into the
# experiment directory first.
#
#   pwsh -File scripts\run_all.ps1
#   pwsh -File scripts\run_all.ps1 -Python C:\ProgramData\miniconda3\python.exe

param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Push-Location $here
try {
    Write-Host "[1/5] parse  -> outputs/ast/ast_<split>.{json,jsonl}"
    & $Python -m sqlast.cli parse --config configs/datasets.yaml
    if ($LASTEXITCODE -ne 0) { throw "parse failed" }

    Write-Host "[2/5] roles  -> outputs/roles/role_ast_<split>.{json,jsonl}"
    & $Python -m sqlast.cli roles --config configs/datasets.yaml --show-example
    if ($LASTEXITCODE -ne 0) { throw "roles failed" }

    Write-Host "[3/5] verify AST -> should end with '0 problems'"
    & $Python tools/verify_ast.py
    if ($LASTEXITCODE -ne 0) { throw "AST fidelity check failed" }

    Write-Host "[4/5] verify roles -> template + slots must rebuild the AST"
    & $Python tools/verify_roles.py
    if ($LASTEXITCODE -ne 0) { throw "role round-trip check failed" }

    Write-Host "[5/5] verify template -> no schema/value residue, rename-invariant"
    & $Python tools/verify_template.py
    if ($LASTEXITCODE -ne 0) { throw "template decoupling check failed" }

    Write-Host "done. reports: outputs/reports/{parse_report,role_report}.md"
}
finally {
    Pop-Location
}
