@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Setup has not run yet.
  echo Run setup.bat first - it creates .venv, downloads the LiveKit server,
  echo and fetches your Whisper model and Piper voice.
  pause
  exit /b 1
)

if not exist "python\server.py" (
  echo [ERROR] python\server.py is missing - re-extract the kit completely.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -u "python\server.py"

rem Normal exits (you closed the app window, or pressed Ctrl+C) close this
rem console too. Only a real error keeps the window open so you can read it.
if errorlevel 1 pause
