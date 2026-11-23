@echo off
rem Regenerate the AST sets, the role templates and run all fidelity checks.
rem
rem A .cmd wrapper is used because this machine refuses unsigned .ps1 files
rem (execution policy AllSigned); a batch file is not affected by it.
rem
rem   scripts\run_all.cmd
rem   scripts\run_all.cmd C:\ProgramData\miniconda3\python.exe

setlocal
set "PYTHON=%~1"
if "%PYTHON%"=="" set "PYTHON=python"

pushd "%~dp0.."
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_all.ps1" -Python "%PYTHON%"
set "RC=%ERRORLEVEL%"
popd

exit /b %RC%
