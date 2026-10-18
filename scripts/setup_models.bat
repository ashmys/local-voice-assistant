@echo off
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Run setup.bat first.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -u "python\setup_wizard.py" --models-only
pause
