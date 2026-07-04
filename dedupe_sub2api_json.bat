@echo off
setlocal

set "ROOT=%~dp0"
set "PYTHON=%ROOT%.venv\Scripts\python.exe"
set "SCRIPT=%ROOT%scripts\dedupe_sub2api_json.py"

if not exist "%PYTHON%" (
  set "PYTHON=python"
)

if not exist "%SCRIPT%" (
  echo Script not found: %SCRIPT%
  pause
  exit /b 1
)

set "INPUT=%~1"
if "%INPUT%"=="" (
  echo Drag a SUB2API JSON file here and press Enter, or type the file path:
  set /p "INPUT=> "
)

set "INPUT=%INPUT:"=%"
if "%INPUT%"=="" (
  echo No input file.
  pause
  exit /b 1
)

if not exist "%INPUT%" (
  echo Input file not found: %INPUT%
  pause
  exit /b 1
)

for %%F in ("%INPUT%") do set "SUMMARY=%%~dpnF.dedupe-summary.json"

"%PYTHON%" "%SCRIPT%" "%INPUT%" --summary "%SUMMARY%"
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if not "%EXIT_CODE%"=="0" (
  echo Failed, exit code: %EXIT_CODE%
) else (
  echo Summary: %SUMMARY%
)
pause
exit /b %EXIT_CODE%
