@echo off
cd /d "%~dp0replays"
rem conta baus e itens pelos replays do Fortnite (precisa do Node.js: https://nodejs.org)
where node >nul 2>nul || (echo Instale o Node.js em https://nodejs.org e rode de novo. & pause & exit /b 1)
if not exist node_modules call npm install --silent --no-audit --no-fund
node ler_replays.js %*
pause
