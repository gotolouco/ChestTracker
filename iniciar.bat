@echo off
cd /d "%~dp0"
python -m pip install --quiet pynput
python contador_baus.py %*
pause
