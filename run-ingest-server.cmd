@echo off
chcp 65001 >nul
setlocal

rem  Double-click this file to start the local page reader receiver.
rem  The Edge extension pushes pages to http://localhost:8787/ingest

set "HERE=%~dp0"
set "SCRIPT=%HERE%tools\page_ingest_server.py"

if not exist "%SCRIPT%" (
  echo [ERROR] Cannot find "%SCRIPT%"
  pause
  exit /b 1
)

echo Starting page reader receiver ...
echo.
python "%SCRIPT%" %*

echo.
pause
