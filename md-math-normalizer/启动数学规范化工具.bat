@echo off
setlocal
cd /d "%~dp0"
set "LAUNCHER=%~dp0launcher.pyw"

where pythonw.exe >nul 2>nul
if not errorlevel 1 (
    pythonw.exe "%LAUNCHER%"
    exit /b %errorlevel%
)

where python.exe >nul 2>nul
if not errorlevel 1 (
    python.exe "%LAUNCHER%"
    exit /b %errorlevel%
)

echo Python was not found. Install Python 3 and add python.exe to PATH.
pause
exit /b 1
