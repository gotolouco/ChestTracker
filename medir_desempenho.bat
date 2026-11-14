@echo off
rem Teste de desempenho: abre o ChestTracker e, depois de 5 minutos com o painel aberto, grava desempenho.txt nesta pasta.
rem Jogue normalmente enquanto ele mede (de preferencia uma partida inteira).
cd /d "%~dp0"
set SEG=300
if exist ChestTracker_teste.exe (start "" ChestTracker_teste.exe --medir %SEG% & exit /b)
if exist ChestTracker.exe (start "" ChestTracker.exe --medir %SEG% & exit /b)
python contador_baus.py --medir %SEG%
