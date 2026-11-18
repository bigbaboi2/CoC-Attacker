"""Entry point for the CoCAttack Isolated Flow Test Tool."""
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _preload_ocr():
    """onnxruntime MUST be imported before PyQt5 on Windows to avoid DLL load conflicts."""
    try:
        import onnxruntime  # noqa: F401
        import rapidocr  # noqa: F401
    except Exception as exc:
        print(f"[TestTool] Warning: OCR preload failed: {exc}")


def main():
    _preload_ocr()
    from PyQt5.QtCore import Qt
    from PyQt5.QtGui import QFontDatabase, QIcon
    from PyQt5.QtWidgets import QApplication
    from ui.styles import APP_ICON_ICO, APP_ICON_PNG, FONT_FILES
    from ui.test_tool_window import TestToolWindow

    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

    app = QApplication(sys.argv)
    app.setApplicationName("CoCAttack Test Tool")
    icon_file = APP_ICON_ICO if APP_ICON_ICO.is_file() else APP_ICON_PNG
    if icon_file.is_file():
        app.setWindowIcon(QIcon(str(icon_file)))
    for font_file in FONT_FILES:
        if font_file.is_file():
            QFontDatabase.addApplicationFont(str(font_file))

    win = TestToolWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
