@echo off
rem Drag files or folders onto this file to run the whole pipeline:
rem   PDF -> Markdown -> normalized Markdown + suspicious-line report
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" set "PY=python"
"%PY%" "%~dp0run_pipeline.py" %*
set "CODE=%errorlevel%"
echo.
echo Exit code: %CODE%
if not "%~1"=="" if not "%NO_PAUSE%"=="1" pause
exit /b %CODE%
