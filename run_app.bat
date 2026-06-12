@echo off
chcp 65001 >nul
setlocal

set "ROOT=%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%scripts\run_app.ps1" %*
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
  echo.
  echo Run failed. Exit code: %EXIT_CODE%
  pause
  exit /b %EXIT_CODE%
)
