@echo off
cd /d "%~dp0.."

if not exist "bin\livekit-server.exe" (
  echo [ERROR] LiveKit server not downloaded yet - run setup.bat.
  pause
  exit /b 1
)

"bin\livekit-server.exe" --config "user_data\livekit.yaml"
pause
