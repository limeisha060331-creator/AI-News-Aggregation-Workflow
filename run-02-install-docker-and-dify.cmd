@echo off
chcp 65001 >nul
setlocal

rem  Double-click this file to run 02-install-docker-and-dify.ps1 as administrator.
rem  Run this ONLY after 01 finished and the PC has been rebooted.
rem  It will pop a UAC prompt; click "Yes".

set "HERE=%~dp0"
set "PS1=%HERE%02-install-docker-and-dify.ps1"

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

echo Running 02-install-docker-and-dify.ps1 ...
echo This can take 10-30 minutes (download + install + first Docker start).
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%"

echo.
echo ==== Done. Follow the printed steps to configure Docker disk location and start Dify ====
pause
