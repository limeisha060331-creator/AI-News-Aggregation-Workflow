@echo off
chcp 65001 >nul
setlocal

rem  Double-click this file to run 01-enable-wsl.ps1 as administrator.
rem  It will pop a UAC prompt; click "Yes".

set "HERE=%~dp0"
set "PS1=%HERE%01-enable-wsl.ps1"

if not exist "%PS1%" (
  echo [ERROR] Cannot find "%PS1%"
  pause
  exit /b 1
)

net session >nul 2>&1
if %errorlevel% neq 0 (
  echo Requesting administrator privileges...
  powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0'"
  exit /b
)

echo Running 01-enable-wsl.ps1 ...
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%"

echo.
echo ==== Done. Reboot the PC, then run run-02-install-docker-and-dify.cmd ====
pause
