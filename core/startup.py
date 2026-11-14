"""Per-user Windows startup integration for the GUI."""
import os
import subprocess
import sys
import time
from pathlib import Path

import psutil


RUN_KEY = r'Software\Microsoft\Windows\CurrentVersion\Run'
RUN_VALUE = 'CoCAttack'
MUMU_PROCESS_NAMES = {'mumunxdevice.exe', 'mumunxmain.exe', 'nemuplayer.exe', 'mumuplayer.exe'}
STARTUP_MODES = {'off', 'windows', 'mumu'}


def startup_command(mode, executable=None, script=None):
    """Build a quoted command; MuMu mode waits before opening the GUI."""
    if mode not in STARTUP_MODES:
        raise ValueError(f'Unknown startup mode: {mode}')
    if mode == 'off':
        return ''
    executable = Path(executable or sys.executable)
    if getattr(sys, 'frozen', False):
        parts = [str(executable)]
    else:
        pythonw = executable.with_name('pythonw.exe')
        if pythonw.is_file():
            executable = pythonw
        parts = [str(executable), str(script or Path(__file__).resolve().parents[1] / 'main.py')]
    if mode == 'mumu':
        parts.append('--wait-for-mumu')
    return subprocess.list2cmdline(parts)


def configure_startup(mode):
    """Install or remove this user's Run entry without requiring administrator rights."""
    if mode not in STARTUP_MODES:
        raise ValueError(f'Unknown startup mode: {mode}')
    if os.name != 'nt':
        raise RuntimeError('Tự khởi động chỉ hỗ trợ Windows.')
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as key:
        if mode == 'off':
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass
        else:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, startup_command(mode))


def mumu_is_running(processes=None):
    processes = processes if processes is not None else psutil.process_iter(['name'])
    for process in processes:
        try:
            name = process.info.get('name') if hasattr(process, 'info') else process.name()
            if (name or '').casefold() in MUMU_PROCESS_NAMES:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def wait_for_mumu(interval=5):
    while not mumu_is_running():
        time.sleep(interval)
