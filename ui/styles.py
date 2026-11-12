from pathlib import Path
_CHECKED=str(Path(__file__).resolve().parents[1]/'assets'/'ui'/'checked.png').replace('\\','/')
# Compact dark theme. No runtime interpolation of CSS braces.
DARK_THEME_QSS = """
QWidget { background: #171b22; color: #e6e9ef; font-family: 'Segoe UI'; font-size: 13px; }
QMainWindow, QDialog { background: #171b22; }
QLabel, QCheckBox { background: transparent; }
QGroupBox { background: #1d232d; border: 1px solid #354052; border-radius: 6px; margin-top: 18px; padding: 16px 12px 12px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; color: #cbd5e1; }
QPushButton { background: #2b3544; border: 1px solid #46536a; border-radius: 5px; padding: 8px 12px; min-height: 20px; }
QPushButton:hover { background: #38465c; }
QPushButton:disabled { color: #7c8799; background: #202630; border-color: #303846; }
QPushButton#btn_start { background: #176746; border-color: #25825a; }
QPushButton#btn_stop { background: #89343a; border-color: #aa454b; }
QPushButton#btn_pause { background: #725520; border-color: #957333; }
QPushButton#btn_start:hover { background: #228058; }
QPushButton#btn_pause:hover { background: #8d6928; }
QPushButton#btn_stop:hover { background: #a84149; }
QPushButton#btn_start:disabled, QPushButton#btn_pause:disabled, QPushButton#btn_stop:disabled {
    color: #8390a3; background: #222933; border-color: #394453;
}
QPushButton#btn_start[active="true"]:disabled { color: #ecfdf5; background: #167650; border-color: #5ee4ac; }
QPushButton#btn_pause[active="true"] { color: #fffbeb; background: #8a6220; border-color: #fbbf24; }
QPushButton#btn_stop[active="true"]:disabled { color: #fff1f2; background: #9f3545; border-color: #fb7185; }
QPushButton[variant="reset"] {
    background: #1e2634; border: 1px solid #3b4759; color: #94a3b8; border-radius: 4px; padding: 3px 8px; font-size: 11px; min-height: 18px;
}
QPushButton[variant="reset"]:hover {
    background: #2b374a; color: #f1f5f9; border-color: #556680;
}
QLineEdit, QSpinBox, QComboBox { background: #141921; border: 1px solid #46536a; border-radius: 4px; padding: 5px 8px; min-height: 22px; selection-background-color: #36628a; }
QComboBox QAbstractItemView { background: #242d3a; selection-background-color: #36628a; }
QCheckBox { spacing: 8px; min-height: 26px; }
QCheckBox::indicator { width: 18px; height: 18px; }
QCheckBox::indicator:unchecked { background: #141921; border: 1px solid #71809a; border-radius: 3px; }
QCheckBox::indicator:checked { image: url(__CHECKED__); background: #4f9aca; border: 1px solid #8ec6e9; border-radius: 3px; }
QTabWidget::pane { border: 1px solid #354052; }
QTabBar::tab { background: #242c38; padding: 10px 18px; margin-right: 3px; }
QTabBar::tab:selected { background: #35465d; color: white; }
QTabBar::tab:disabled { background: #1d232d; color: #8290a3; }
QScrollArea { border: none; }
QScrollBar:vertical { width: 12px; background: #171b22; }
QScrollBar::handle:vertical { background: #53647c; min-height: 30px; border-radius: 4px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
QPlainTextEdit, QTextBrowser { background: #11161d; border: 1px solid #354052; font-family: Consolas; padding: 6px; }
QStatusBar { color: #abb7ca; }
""".replace('__CHECKED__',_CHECKED)
