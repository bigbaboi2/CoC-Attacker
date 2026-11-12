@echo off
chcp 65001 >nul
cd /d "%~dp0"
python -m pip install -r requirements.txt
if errorlevel 1 echo Cai dat that bai. Kiem tra Python va ket noi mang.
pause
