import sys
import os
import argparse
from contextlib import redirect_stderr, redirect_stdout
import threading
import traceback

try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except Exception:
    pass

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

def _preload_ocr():
    """
    onnxruntime MUST be imported before PyQt5, otherwise Windows raises
    'DLL initialization routine failed' (DLL conflict between Qt and ORT).
    """
    try:
        import onnxruntime  # noqa: F401
        import rapidocr  # noqa: F401
    except Exception as e:
        print(f"[Main] Warning: OCR preload failed: {e}")

class GuiOutput:
    encoding = 'utf-8'

    def __init__(self):
        self.pending = []
        self.emit = self.pending.append
        self.lines = threading.local()

    def write(self, text):
        lines = (getattr(self.lines, 'partial', '') + text).split('\n')
        self.lines.partial = lines.pop()
        for line in lines:
            if line.strip():
                self.emit(line.rstrip())
        return len(text)

    def flush(self):
        partial = getattr(self.lines, 'partial', '')
        if partial:
            self.lines.partial = ''
            self.emit(partial)

    def attach(self, emit):
        self.emit = emit
        for line in self.pending:
            emit(line)
        self.pending.clear()


def run_gui(output):
    _preload_ocr()
    from PyQt5.QtWidgets import QApplication
    from PyQt5.QtCore import Qt
    from ui.main_window import MainWindow

    # Enable High DPI scaling
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setApplicationName("CoCAttack")

    window = MainWindow()
    output.attach(window.bridge.sig_log.emit)
    window.show()

    sys.exit(app.exec_())

def run_cli():
    _preload_ocr()
    print("=====================================================")
    print(" CoCAttack CLI - Automated Headless Farming Engine   ")
    print("=====================================================")
    from config.settings import BotSettings
    from bot.bot_engine import BotEngine

    settings = BotSettings()
    bot = BotEngine(settings=settings)

    print(f"[*] Target device: {settings.get('device_serial', '127.0.0.1:5555')}")
    print("[*] Starting bot... Press Ctrl+C to stop.")

    try:
        bot.start_bot()
        while bot.is_running:
            import time
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[*] Stopping bot...")
        bot.stop_bot()
        print("[*] Bot terminated safely.")
    finally:
        bot.close()

def main():
    parser = argparse.ArgumentParser(description="CoCAttack - Clash of Clans MuMu Automation Tool")
    parser.add_argument("--cli", action="store_true", help="Run in headless CLI mode without GUI")
    args = parser.parse_args()

    if args.cli:
        run_cli()
    else:
        output = GuiOutput()
        console_available = sys.stderr is not None
        try:
            with redirect_stdout(output), redirect_stderr(output):
                run_gui(output)
        except Exception:
            if console_available or os.name != 'nt':
                raise
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None, traceback.format_exc(), 'CoCAttack - Startup error', 0x10
            )
            sys.exit(1)

if __name__ == "__main__":
    main()
