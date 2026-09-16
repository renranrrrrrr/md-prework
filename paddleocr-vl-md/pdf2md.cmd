@echo off
rem pdf2md wrapper - avoids typing the venv interpreter path.
rem Usage: pdf2md "D:\folder"   or   pdf2md "D:\a\1.pdf" "D:\b\2.pdf"
"%~dp0.venv\Scripts\python.exe" "%~dp0pdf2md.py" %*
exit /b %errorlevel%
