@echo off
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\pythonw.exe" (set "PY=.venv\Scripts\pythonw.exe") else (set "PY=pythonw.exe")
start "" "%PY%" "%~dp0main.py"
