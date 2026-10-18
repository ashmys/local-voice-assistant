@echo off
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] Setup has not run yet.
  echo Run setup.bat first - it creates .venv, downloads the LiveKit server,
  echo and fetches your Whisper model and Piper voice.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -u "python\server.py"
pause
