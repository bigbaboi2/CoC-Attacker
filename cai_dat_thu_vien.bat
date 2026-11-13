@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    py -3.12 -m venv .venv
    if errorlevel 1 (
        echo Khong tao duoc .venv. Hay cai Python 3.12 64-bit cung Python Launcher.
        pause
        exit /b 1
    )
)
".venv\Scripts\python.exe" -m pip --version >nul 2>&1
if errorlevel 1 (
    ".venv\Scripts\python.exe" -m ensurepip
    if errorlevel 1 (
        echo Khong khoi tao duoc pip trong .venv.
        pause
        exit /b 1
    )
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo Cai dat that bai. Kiem tra ket noi mang va chay lai file nay.
    pause
    exit /b 1
)
echo Cai dat xong. Mo CoCAttack.vbs de chay.
pause
