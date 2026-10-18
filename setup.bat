@echo off
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python was not found in PATH.
  echo Install Python 3.11 or 3.12 from https://www.python.org/downloads/
  echo and check "Add python.exe to PATH" during install, then run this again.
  pause
  exit /b 1
)

python -u "python\setup_wizard.py" %*
pause
