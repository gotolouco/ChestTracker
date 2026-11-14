@echo off
cd /d "%~dp0"
python -m pip install --quiet -r requirements-rota.txt
python contador_baus.py --marcar-minimapa
pause
