#!/usr/bin/env bash
# One-time setup for Linux / macOS (equivalent of setup.bat on Windows).
# Creates .venv, installs packages, downloads the LiveKit server,
# fetches a Whisper model and a Piper voice, writes the config.
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1; then
  echo "[ERROR] python3 was not found in PATH."
  echo "Install Python 3.11 or 3.12 first, e.g.:"
  echo "  Ubuntu/Debian : sudo apt install python3 python3-venv python3-pip"
  echo "  Fedora        : sudo dnf install python3"
  echo "  macOS         : brew install python@3.12"
  exit 1
fi

python3 -u python/setup_wizard.py "$@"
