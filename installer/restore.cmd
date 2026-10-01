@echo off
setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0patch.ps1" -Action restore
set "patch_exit=%ERRORLEVEL%"
if /I not "%~1"=="/quiet" pause
exit /b %patch_exit%
