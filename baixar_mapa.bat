@echo off
cd /d "%~dp0"
rem Mapa e baus do chestroute.com (versao 43.10 do jogo). Quando o mapa mudar, troque o 43.10 pelo numero novo.
python contador_baus.py --baixar-mapa "https://chestroute.com/tiles/43.10/real/{z}/{x}/{y}.webp" --zoom 4 --escala 1.8 --baus-de https://chestroute.com
pause
