@echo off
cd /d "%~dp0"
set /p NOME=Nome da rota (Enter = rota1): 
if "%NOME%"=="" set NOME=rota1
python contador_baus.py --editar-rota "%NOME%"
pause
