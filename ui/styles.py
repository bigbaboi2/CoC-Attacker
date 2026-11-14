"""Shared Clash-inspired palettes and scalable Qt styles."""
from pathlib import Path

FONT_DIR = Path(__file__).resolve().parents[1] / 'assets' / 'fonts'
FONT_FILES = (FONT_DIR / 'Baloo2.ttf', FONT_DIR / 'NunitoSans.ttf')
TEXT_SIZES = {'small': 12, 'medium': 14, 'large': 16}

PALETTES = {
    'dark': dict(window='#151e2c', panel='#213047', raised='#2b3e58', input='#172438',
                 text='#f5f8ff', muted='#b8c9de', border='#516b89', accent='#58bded', accent_hover='#83d3f6',
                 selected='#345c81', log='#111b2a', disabled='#8195ae'),
    'light': dict(window='#eff7ff', panel='#ffffff', raised='#e5f2ff', input='#ffffff',
                  text='#18324d', muted='#49637e', border='#a7c5de', accent='#187cb8', accent_hover='#0f679d',
                  selected='#c9e9fb', log='#f8fbff', disabled='#74899e'),
}


def theme_qss(theme='dark', text_size='medium'):
    p = PALETTES.get(theme, PALETTES['dark'])
    base = TEXT_SIZES.get(text_size, TEXT_SIZES['medium'])
    checked = str(Path(__file__).resolve().parents[1] / 'assets' / 'ui' / 'checked.png').replace('\\', '/')
    resources = ({
        'gold': ('#4a3b1b', '#d9a82b', '#ffe278'),
        'elixir': ('#40284f', '#b967d7', '#f2b4ff'),
        'dark_elixir': ('#302b43', '#8171ad', '#c2b1f2'),
        'gems': ('#1f4339', '#4abf8b', '#92f5bd'),
    } if theme == 'dark' else {
        'gold': ('#fff6d9', '#d99b18', '#795400'),
        'elixir': ('#fbeafc', '#bd66d1', '#793089'),
        'dark_elixir': ('#efebf8', '#8171ad', '#4c3b6d'),
        'gems': ('#e4f9ed', '#47b879', '#176b40'),
    })
    stat_tones = ({
        'blue': ('#1c3c58', '#4e9fd0', '#e2f5ff'),
        'purple': ('#332d58', '#8878c8', '#ece7ff'),
        'amber': ('#51401f', '#d4a94a', '#fff0b8'),
        'gold': ('#4b3a1c', '#d1a02e', '#ffe8a3'),
        'elixir': ('#442b50', '#bd74ce', '#f8dbff'),
        'dark_elixir': ('#302a46', '#8977b5', '#e9dcff'),
        'green': ('#234738', '#59ae82', '#ddffeb'),
        'slate': ('#263b4b', '#6c99b0', '#e3f3ff'),
    } if theme == 'dark' else {
        'blue': ('#e2f2ff', '#4d9dca', '#17486b'),
        'purple': ('#eee9ff', '#927dca', '#49357b'),
        'amber': ('#fff4d7', '#d1a447', '#704b08'),
        'gold': ('#fff2ce', '#d49b2a', '#765000'),
        'elixir': ('#f8e7fa', '#bc73cb', '#71317f'),
        'dark_elixir': ('#eee9f7', '#8d78b3', '#4b396e'),
        'green': ('#e2f8e9', '#62ae7e', '#1d6240'),
        'slate': ('#e7f1f7', '#739ab0', '#284b60'),
    })
    qss = """
    QWidget { background: __WINDOW__; color: __TEXT__; font-family: 'Nunito Sans'; font-size: __BASE__px; }
    QMainWindow, QDialog { background: __WINDOW__; }
    QLabel, QCheckBox { background: transparent; }
    QLabel[role="pageTitle"] { color: __TEXT__; font-family: 'Baloo 2'; font-size: __TITLE__px; font-weight: 700; }
    QLabel[role="pageHint"] { color: __MUTED__; font-size: __HINT__px; }
    QGroupBox { background: __PANEL__; border: 1px solid __BORDER__; border-radius: 10px; margin-top: 22px; padding: 18px 14px 14px; font-size: __SECTION__px; font-weight: 700; }
    QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 0 6px; color: __ACCENT__; font-family: 'Baloo 2'; }
    QPushButton { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 __SELECTED__,stop:0.48 __RAISED__,stop:1 __PANEL__); border: 2px solid __BORDER__; border-bottom: 4px solid __BORDER__; border-radius: 10px; padding: 5px 11px; min-height: __CONTROL__px; font-family: 'Baloo 2'; font-size: __BUTTON__px; font-weight: 700; }
    QPushButton[role="headerControl"] { font-family: 'Nunito Sans'; font-size: 11px; padding: 4px 5px; min-height: 24px; }
    QPushButton:hover { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 __ACCENT_HOVER__,stop:0.3 __SELECTED__,stop:1 __RAISED__); border-color: __ACCENT__; }
    QPushButton:pressed { padding-top: 7px; padding-bottom: 3px; border-bottom-width: 2px; }
    QPushButton:checked { background: __SELECTED__; border-color: __ACCENT__; color: __TEXT__; }
    QPushButton#btn_primary { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #80d8ff,stop:0.45 #269bd6,stop:1 #126caa); color: #ffffff; border-color: #1476b7; border-bottom-color: #0b4d82; }
    QPushButton#btn_primary:hover { background: #2aa9e6; }
    QPushButton:disabled { color: __DISABLED__; background: __PANEL__; border-color: __BORDER__; }
    QPushButton:focus, QLineEdit:focus, QSpinBox:focus, QDateTimeEdit:focus, QComboBox:focus { border: 2px solid __ACCENT__; }
    QPushButton#btn_start { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #e9ff79,stop:0.24 #b7f52b,stop:0.62 #84db00,stop:1 #49a900); border-color: #d7ff54; border-bottom-color: #277900; color: #203400; }
    QPushButton#btn_start:hover { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #f1ff9e,stop:0.5 #c4fa32,stop:1 #66c900); }
    QPushButton#btn_pause { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #fff19a,stop:0.42 #ffc652,stop:1 #d98a21); border-color: #ffdc71; border-bottom-color: #9e5d13; color: #59320b; }
    QPushButton#btn_pause:hover { background: #ffda77; }
    QPushButton#btn_stop { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #ffada4,stop:0.42 #ed6e70,stop:1 #bf3f4b); border-color: #f99895; border-bottom-color: #862933; color: #4d1520; }
    QPushButton#btn_stop:hover { background: #fa8987; }
    QPushButton#recordReset { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #ffe8a2,stop:0.5 #e9b751,stop:1 #bd7f27); color: #4f320e; border-color: #e4b75d; border-bottom-color: #8e5618; }
    QPushButton#btn_start:disabled, QPushButton#btn_pause:disabled, QPushButton#btn_stop:disabled { background: __RAISED__; color: __DISABLED__; border-color: __BORDER__; }
    QPushButton#btn_start[active="true"]:disabled { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #e9ff79,stop:0.24 #b7f52b,stop:0.62 #84db00,stop:1 #49a900); border-color: #d7ff54; border-bottom-color: #277900; color: #203400; }
    QPushButton#btn_start[paused="true"]:disabled { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #fff19a,stop:0.42 #ffc652,stop:1 #d98a21); border-color: #ffdc71; border-bottom-color: #9e5d13; color: #59320b; }
    QPushButton#btn_start[paused="true"] { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #fff19a,stop:0.42 #ffc652,stop:1 #d98a21); border-color: #ffdc71; border-bottom-color: #9e5d13; color: #59320b; }
    QPushButton#btn_stop[active="true"]:disabled { background: #bd4350; color: #ffffff; }
    QPushButton[variant="reset"] { padding: 3px 8px; min-height: 18px; }
    QLineEdit, QSpinBox, QDateTimeEdit, QComboBox { background: __INPUT__; border: 1px solid __BORDER__; border-radius: 6px; padding: 5px 8px; min-height: __CONTROL__px; selection-background-color: __SELECTED__; }
    QComboBox QAbstractItemView { background: __PANEL__; color: __TEXT__; selection-background-color: __SELECTED__; }
    QCheckBox { spacing: 8px; min-height: __CHECK__px; }
    QCheckBox::indicator { width: 18px; height: 18px; }
    QCheckBox::indicator:unchecked { background: __INPUT__; border: 1px solid __BORDER__; border-radius: 4px; }
    QCheckBox::indicator:checked { image: url(__CHECKED__); background: __ACCENT__; border: 1px solid __ACCENT__; border-radius: 4px; }
    QTabWidget::pane { background: __PANEL__; border: 1px solid __BORDER__; border-radius: 8px; }
    QTabBar::tab { background: __RAISED__; color: __MUTED__; }
    QTabBar::tab:selected { background: __PANEL__; color: __ACCENT__; }
    QTabBar::tab:disabled { color: __DISABLED__; }
    QScrollArea { border: none; }
    QScrollBar:vertical { width: 12px; background: __WINDOW__; }
    QScrollBar::handle:vertical { background: __BORDER__; min-height: 30px; border-radius: 5px; }
    QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0px; }
    QPlainTextEdit, QTextBrowser { background: __LOG__; color: __TEXT__; border: 1px solid __BORDER__; border-radius: 6px; font-family: Consolas; padding: 8px; }
    QFrame#statCard, QFrame#villageMetric, QFrame#villageSection { background: __PANEL__; border: 1px solid __BORDER__; border-radius: 10px; }
    BuildingItemWidget { background: __RAISED__; border: 1px solid __BORDER__; border-radius: 6px; }
    BuildingItemWidget:hover { background: __SELECTED__; border-color: __ACCENT__; }
    QLabel#liveViewer { background: __LOG__; color: __MUTED__; border: 1px solid __BORDER__; border-radius: 8px; }
    QLabel#metricTitle, QLabel#statTitle, QLabel#sectionDetail { color: __MUTED__; }
    QLabel#metricValue, QLabel#sectionTitle { color: __TEXT__; font-size: __METRIC__px; font-weight: 700; }
    __RESOURCE_STYLES__
    QLabel#statValue { color: __TEXT__; font-size: __STAT__px; font-weight: 700; }
    __STAT_TONE_STYLES__
    QLabel#stateBadge { background: __RAISED__; color: __TEXT__; border: 1px solid __BORDER__; border-radius: 7px; padding: 6px 12px; font-weight: 700; }
    QLabel#stateBadge[village="main"] { background: #1e6948; color: #ffffff; border-color: #51c88b; }
    QLabel#stateBadge[village="builder"] { background: #493976; color: #ffffff; border-color: #a88de5; }
    QPushButton#activityButton { text-align: left; padding-left: 12px; }
    QPushButton#activityButton[village="main"] { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #54c987,stop:1 #176943); color: #ffffff; border-color: #68dca0; border-bottom-color: #0b4d2d; }
    QPushButton#activityButton[village="builder"] { background: qlineargradient(x1:0,y1:0,x2:0,y2:1,stop:0 #9d83df,stop:1 #4a367a); color: #ffffff; border-color: #b29bea; border-bottom-color: #322158; }
    QStatusBar { color: __MUTED__; }
    """
    values = {**p, 'base': base, 'title': base + 10, 'hint': base - 1,
              'metric': base + 2, 'stat': base + 7,
              'section': base + 2, 'button': base + 1, 'control': base + 14, 'check': base + 13,
              'checked': checked}
    resource_rules = []
    for key, (background, border, text) in resources.items():
        resource_rules.append(
            f'QFrame#villageMetric[resource="{key}"] {{ background: {background}; border: 1px solid {border}; }} '
            f'QFrame#villageMetric[resource="{key}"] QLabel#metricTitle, '
            f'QFrame#villageMetric[resource="{key}"] QLabel#metricValue {{ color: {text}; }}'
        )
    qss = qss.replace('__RESOURCE_STYLES__', '\n'.join(resource_rules))
    stat_rules = []
    for tone, (background, border, foreground) in stat_tones.items():
        stat_rules.append(
            f'QFrame#statCard[tone="{tone}"] {{ background: {background}; border: 1px solid {border}; '
            f'border-bottom: 3px solid {border}; }} '
            f'QFrame#statCard[tone="{tone}"] QLabel#statTitle, '
            f'QFrame#statCard[tone="{tone}"] QLabel#statValue {{ color: {foreground}; }}'
        )
    qss = qss.replace('__STAT_TONE_STYLES__', '\n'.join(stat_rules))
    for key, value in values.items():
        qss = qss.replace('__' + key.upper() + '__', str(value))
    return qss


DARK_THEME_QSS = theme_qss('dark', 'medium')
