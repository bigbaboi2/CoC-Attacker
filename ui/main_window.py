import os
import sys
import json
import time
import threading
from typing import Dict, Tuple, List, Optional
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
import numpy as np
from PyQt5.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QTabWidget,
    QLabel, QPushButton, QCheckBox, QGroupBox,
    QFormLayout, QGridLayout, QMessageBox, QFrame, QSplitter, QScrollArea, QStyle,
    QBoxLayout, QInputDialog, QDialog, QLineEdit, QFileDialog, QSizePolicy, QLayout,
    QSystemTrayIcon, QMenu, QAction, QApplication
)
from PyQt5.QtCore import Qt, QTimer, QDateTime, pyqtSignal, QObject
from PyQt5.QtGui import QIcon

from ui.styles import theme_qss, TEXT_SIZES
from ui.activity import activity_from_log
from ui.widgets import StatCard, LiveScreenViewer, LogConsole, VillageTabBar, VillageLiveStatusWidget, BubbleTabBar
from ui.widgets import NoWheelSpinBox as QSpinBox, NoWheelComboBox as QComboBox, NoWheelDateTimeEdit as QDateTimeEdit
from config.settings import (
    BotSettings, DEFAULT_CONFIG, BUILDING_CATEGORY_LABELS,
    BUILDING_CATALOG, DEFAULT_BUILDING_WHITELIST,
    BUILDING_KEY_TO_RESOURCES, CATEGORY_TO_RESOURCES
)
from core.adb_client import ADBSocketClient
from core.mumu_optimizer import MuMuOptimizer
from core.performance import PerformanceMonitor
from core.startup import configure_startup, STARTUP_MODES
from core.action_timer import ToolActionTimer
from core.fixed_attack import FixedAttackPlan
from ui.fixed_calibration import FixedCalibrationDialog
from bot.bot_engine import BotEngine
from bot.builder_base import BuilderBaseManager
from bot.builder_army import COMBOS, REINFORCEMENT_CHOICES, REINFORCEMENT_KEYS

class BuildingItemWidget(QWidget):
    """Visual row for a building with name and colored resource badges."""
    def __init__(self, b_key, b_name_en, b_label, b_resources, initial_checked, on_toggle, parent=None):
        super().__init__(parent)
        self.b_key = b_key
        self.b_name_en = b_name_en
        self.b_label = b_label
        self.b_resources = b_resources

        layout = QHBoxLayout(self)
        layout.setContentsMargins(5, 2, 5, 2)
        layout.setSpacing(4)

        self.chk = QCheckBox(b_label)
        self.chk.setToolTip(f"{b_label} ({b_name_en})")
        self.chk.setChecked(initial_checked)
        self.chk.toggled.connect(on_toggle)
        layout.addWidget(self.chk, 1)

        res_meta = {
            "gold": ("Vàng", "#F59E0B", "rgba(245, 158, 11, 0.15)"),
            "elixir": ("Dầu", "#EC4899", "rgba(236, 72, 153, 0.15)"),
            "dark_elixir": ("Đen", "#A855F7", "rgba(168, 85, 247, 0.15)"),
        }
        for r in b_resources:
            badge_text, fg, bg = res_meta.get(r, (r.upper(), "#94A3B8", "rgba(148, 163, 184, 0.1)"))
            badge = QLabel(badge_text)
            badge.setStyleSheet(f"""
                QLabel {{
                    color: {fg};
                    background-color: {bg};
                    border: 1px solid {fg}40;
                    border-radius: 3px;
                    padding: 1px 4px;
                    font-weight: bold;
                }}
            """)
            layout.addWidget(badge)



class BuildingSelectionDialog(QDialog):
    """Modern intuitive compact dialog for granular building selection."""

    def __init__(self, settings: BotSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.setWindowTitle("Thiết lập chi tiết từng công trình nâng cấp")
        self.resize(660, 560)
        self.setMinimumWidth(600)
        self.item_widgets = {}

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(10, 10, 10, 10)
        main_layout.setSpacing(8)

        # 1. Top bar: Search box and compact Resource filter dropdown
        top_bar = QHBoxLayout()
        top_bar.setSpacing(8)

        self.txt_search = QLineEdit()
        self.txt_search.setPlaceholderText("Tìm nhanh công trình (pháo, bom, mỏ, trạm, cannon, xbow...)")
        self.txt_search.textChanged.connect(self._apply_search_and_filter)
        top_bar.addWidget(self.txt_search, 1)

        self.active_resource_filter = "all"
        self.combo_res_filter = QComboBox()
        self.combo_res_filter.addItem("Tất cả tài nguyên", "all")
        self.combo_res_filter.addItem("Chỉ Vàng", "gold")
        self.combo_res_filter.addItem("Chỉ Dầu hồng", "elixir")
        self.combo_res_filter.addItem("Chỉ Dầu đen", "dark_elixir")
        self.combo_res_filter.currentIndexChanged.connect(
            lambda idx: self._set_resource_filter(self.combo_res_filter.itemData(idx))
        )
        self.combo_res_filter.setMinimumWidth(150)
        top_bar.addWidget(self.combo_res_filter)

        main_layout.addLayout(top_bar)

        # 2. Tab Widget with Category Pages (Concise tab titles)
        category_tab_icons = {
            "traps": "Bẫy",
            "resource": "Mỏ & Kho",
            "defense": "Phòng thủ",
            "army": "Binh lính",
            "heroes": "Tướng",
        }

        self.tabs = QTabWidget()
        whitelist = dict(DEFAULT_BUILDING_WHITELIST)
        saved_wl = self.settings.get("building_upgrade_whitelist")
        if isinstance(saved_wl, dict):
            whitelist.update(saved_wl)

        for cat_key, cat_label in BUILDING_CATEGORY_LABELS:
            page = QWidget()
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(4, 4, 4, 4)
            page_layout.setSpacing(6)

            # Quick action row for this category
            btn_row = QHBoxLayout()
            btn_row.setSpacing(6)
            btn_all = QPushButton("Chọn tất cả")
            btn_none = QPushButton("Bỏ chọn")
            btn_gold_only = QPushButton("Chỉ Vàng")
            btn_elixir_only = QPushButton("Chỉ Dầu")

            btn_all.clicked.connect(lambda _, c=cat_key: self._set_category_all(c, True))
            btn_none.clicked.connect(lambda _, c=cat_key: self._set_category_all(c, False))
            btn_gold_only.clicked.connect(lambda _, c=cat_key: self._set_category_by_res(c, "gold"))
            btn_elixir_only.clicked.connect(lambda _, c=cat_key: self._set_category_by_res(c, "elixir"))

            btn_row.addWidget(btn_all)
            btn_row.addWidget(btn_none)
            btn_row.addWidget(btn_gold_only)
            btn_row.addWidget(btn_elixir_only)

            has_dark = any("dark_elixir" in BUILDING_KEY_TO_RESOURCES.get(k, ()) for k, _, _ in BUILDING_CATALOG.get(cat_key, ()))
            if has_dark:
                btn_dark_only = QPushButton("Chỉ Đen")
                btn_dark_only.clicked.connect(lambda _, c=cat_key: self._set_category_by_res(c, "dark_elixir"))
                btn_row.addWidget(btn_dark_only)

            btn_row.addStretch()
            page_layout.addLayout(btn_row)

            # Scroll list of items (NO horizontal scrollbar)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            grid_host = QWidget()
            grid = QGridLayout(grid_host)
            grid.setContentsMargins(2, 2, 2, 2)
            grid.setSpacing(4)
            items = BUILDING_CATALOG.get(cat_key, ())
            for idx, (b_key, b_name_en, b_label) in enumerate(items):
                b_res = BUILDING_KEY_TO_RESOURCES.get(b_key, ("gold",))
                init_val = bool(whitelist.get(b_key, True))
                w = BuildingItemWidget(
                    b_key=b_key,
                    b_name_en=b_name_en,
                    b_label=b_label,
                    b_resources=b_res,
                    initial_checked=init_val,
                    on_toggle=self._save_whitelist,
                )
                self.item_widgets[b_key] = (cat_key, w)
                grid.addWidget(w, idx // 2, idx % 2)
            grid.setRowStretch((len(items) + 1) // 2, 1)
            scroll.setWidget(grid_host)
            page_layout.addWidget(scroll)

            tab_title = category_tab_icons.get(cat_key, cat_label)
            self.tabs.addTab(page, tab_title)

        main_layout.addWidget(self.tabs, 1)

        # 3. Footer Bar
        footer = QHBoxLayout()
        footer.setSpacing(8)
        btn_all_global = QPushButton("Chọn tất cả")
        btn_all_global.clicked.connect(lambda: self._set_all(True))
        btn_none_global = QPushButton("Bỏ chọn tất cả")
        btn_none_global.clicked.connect(lambda: self._set_all(False))

        self.lbl_stats = QLabel("")
        self.lbl_stats.setStyleSheet("color: #38BDF8; font-weight: bold; ")
        self._update_dialog_stats()

        btn_close = QPushButton("Đóng & Lưu")
        btn_close.setObjectName('btn_primary')
        btn_close.clicked.connect(self.accept)

        footer.addWidget(btn_all_global)
        footer.addWidget(btn_none_global)
        footer.addSpacing(10)
        footer.addWidget(self.lbl_stats)
        footer.addStretch()
        footer.addWidget(btn_close)
        main_layout.addLayout(footer)
        self.checks = {k: w.chk for k, (_, w) in self.item_widgets.items()}

    def _set_resource_filter(self, res_filter: str):
        self.active_resource_filter = res_filter or "all"
        self._apply_search_and_filter()

    def _apply_search_and_filter(self):
        query = self.txt_search.text().strip().lower()
        rf = self.active_resource_filter
        for _, (_, w) in self.item_widgets.items():
            match_search = (
                (not query)
                or (query in w.b_label.lower())
                or (query in w.b_key.lower())
                or (query in getattr(w, 'b_name_en', '').lower())
            )
            match_res = (rf == "all") or (rf in w.b_resources)
            w.setVisible(match_search and match_res)

    def _set_category_all(self, cat_key: str, checked: bool):
        for _, (c_key, w) in self.item_widgets.items():
            if c_key == cat_key and w.isVisible():
                w.chk.blockSignals(True)
                w.chk.setChecked(checked)
                w.chk.blockSignals(False)
        self._save_whitelist()

    def _set_category_by_res(self, cat_key: str, res_key: str):
        for _, (c_key, w) in self.item_widgets.items():
            if c_key == cat_key:
                matches = res_key in w.b_resources
                w.chk.blockSignals(True)
                w.chk.setChecked(matches)
                w.chk.blockSignals(False)
        self._save_whitelist()

    def _set_all(self, checked: bool):
        for _, (_, w) in self.item_widgets.items():
            w.chk.blockSignals(True)
            w.chk.setChecked(checked)
            w.chk.blockSignals(False)
        self._save_whitelist()

    def _update_dialog_stats(self):
        active = sum(1 for _, (_, w) in self.item_widgets.items() if w.chk.isChecked())
        total = len(self.item_widgets)
        self.lbl_stats.setText(f"Đang chọn: {active}/{total} công trình")

    def _save_whitelist(self):
        wl = {b_key: w.chk.isChecked() for b_key, (_, w) in self.item_widgets.items()}
        self.settings.set("building_upgrade_whitelist", wl)
        self._update_dialog_stats()

class GuiBridge(QObject):
    """Bridge for thread-safe UI updates from the background BotEngine."""
    sig_log = pyqtSignal(str)
    sig_screen = pyqtSignal(object)
    sig_stats = pyqtSignal(object)
    sig_state = pyqtSignal(str)
    sig_job_done = pyqtSignal(object)
    sig_performance = pyqtSignal(object)
    sig_farm_completed = pyqtSignal()

class MainWindow(QMainWindow):
    """
    Master Application Window for CoCAttack - MuMu & Emulator Farm Bot.
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("CoCAttack — Điều khiển MuMu")
        self.resize(1180, 780)
        self.setMinimumSize(1040, 620)
        self.settings = BotSettings()
        self._apply_appearance()
        self._pending_logs = []
        self.bridge = GuiBridge()
        self._io_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='CoCAttackIO')
        self._operation_pending = False
        self._control_operation = None
        self._preview_pending = False
        self._closing = False
        self._force_quit = False
        self._session_elapsed = 0.0
        self._session_started_at = None
        self._subtabs = []
        self._frame_lock = threading.Lock()
        self._latest_frame = None

        # Connect bridge signals
        self.bridge.sig_log.connect(self._on_log)
        self.bridge.sig_screen.connect(self._on_screen)
        self.bridge.sig_stats.connect(self._on_stats)
        self.bridge.sig_state.connect(self._on_state)
        self.bridge.sig_job_done.connect(self._finish_job)
        self.bridge.sig_performance.connect(self._on_performance)
        self.bridge.sig_farm_completed.connect(self._close_after_farm)

        # Initialize Bot Engine
        self.bot = BotEngine(
            settings=self.settings,
            log_callback=lambda m: self.bridge.sig_log.emit(m),
            screen_callback=self._queue_frame,
            stats_callback=lambda d: self.bridge.sig_stats.emit(dict(d)),
            state_callback=lambda st: self.bridge.sig_state.emit(st)
        )
        self.performance = PerformanceMonitor(self.bot.adb)
        self.bot.completion_callback = self.bridge.sig_farm_completed.emit
        self._performance_pending = False
        self.action_timer = ToolActionTimer()
        self._timer_wait_logged = False

        self._init_ui()
        for message in self._pending_logs:
            self.log_console.append_log(message)
        self._pending_logs.clear()
        initial_stats = self.bot.get_stats()
        if isinstance(initial_stats, dict):
            self._on_stats(initial_stats)
        self._refresh_devices()
        self._update_farm_finish_badge()

        self._idle_preview_wait = 0
        # Keep active frames responsive while limiting extra ADB captures when idle.
        self.preview_timer = QTimer(self)
        self.preview_timer.timeout.connect(self._poll_preview)
        self.preview_timer.start(1000)
        self.performance_timer = QTimer(self)
        self.performance_timer.timeout.connect(self._poll_performance)
        self.performance_timer.start(5000)
        self.action_timer_tick = QTimer(self)
        self.action_timer_tick.timeout.connect(self._check_action_timer)
        self.action_timer_tick.start(1000)

    def _poll_performance(self):
        if (self._closing or self._performance_pending or self._operation_pending
                or self.bot.adb.device_unavailable is True
                or self.isMinimized() or not self.card_fps.isVisible()):
            return
        self._performance_pending = True
        def sample():
            try:
                values = self.performance.sample()
            except Exception:
                values = dict(tool_cpu=None, mumu_cpu=None, fps=None)
            if not self._closing:
                self.bridge.sig_performance.emit(values)
        self._io_pool.submit(sample)

    def _on_performance(self, values):
        self._performance_pending = False
        if self._closing:
            return
        def percent(value):
            return '—' if value is None else f'{value:.1f}%'
        tool = percent(values.get('tool_cpu'))
        mumu = percent(values.get('mumu_cpu'))
        fps = '—' if values.get('fps') is None else f"{values['fps']:.1f}"
        self.card_cpu.set_value(f'{tool} / {mumu}')
        self.card_fps.set_value(fps)

    def _queue_frame(self, frame):
        # One replaceable frame, so the Qt event queue never accumulates images.
        with self._frame_lock:
            self._latest_frame = frame

    def _run_job(self, operation, finished, preview=False, control=None):
        if self._closing or (not preview and self._operation_pending):
            return
        if preview:
            self._preview_pending = True
        else:
            self._operation_pending = True
            self._control_operation = control
            self._sync_controls()
        def run():
            if self._closing:
                return
            try:
                result, error = operation(), None
            except Exception as exc:
                result, error = None, str(exc)
            if not self._closing:
                self.bridge.sig_job_done.emit((finished, result, error, preview))
        self._io_pool.submit(run)

    def _finish_job(self, payload):
        finished, result, error, preview = payload
        if preview:
            self._preview_pending = False
        else:
            self._operation_pending = False
            self._control_operation = None
        if self._closing:
            return
        if error:
            self._on_log(f'Lỗi thao tác: {error}')
        else:
            finished(result)
        self._sync_controls()

    def _sync_controls(self):
        active = self.bot.is_alive()
        running = self.bot.is_running
        paused = running and self.bot.is_paused
        starting = self._control_operation == 'start'
        stopping = self._control_operation == 'stop' or (active and not running and not starting)
        idle = not active and not running and not self._operation_pending

        # The button clock is for this run only; saved uptime remains in the dashboard.
        clock_active = (starting or running) and not stopping
        if clock_active and not paused and self._session_started_at is None:
            self._session_started_at = time.monotonic()
        elif (paused or not clock_active) and self._session_started_at is not None:
            self._session_elapsed += max(0.0, time.monotonic() - self._session_started_at)
            self._session_started_at = None
        for control in (self.device_combo, self.btn_connect, self.btn_refresh, self.btn_quick_opt):
            control.setEnabled(idle)
        self.btn_start.setEnabled((idle or running) and not self._operation_pending)
        self.btn_stop.setEnabled(running and not self._operation_pending)
        dots = '·' * (1 + self._control_frame % 3)
        if clock_active:
            seconds = int(self._session_elapsed + (max(0.0, time.monotonic() - self._session_started_at)
                                                   if self._session_started_at is not None else 0.0))
            hours, rem = divmod(seconds, 3600)
            minutes, secs = divmod(rem, 60)
            clock_text = f'{hours:02d}:{minutes:02d}:{secs:02d}'
            if not paused and self._control_frame % 2:
                clock_text = clock_text.replace(':', ' ')
            self.btn_start.setText(clock_text)
            self.btn_start.setAccessibleName(f'Thời gian phiên chạy {hours:02d}:{minutes:02d}:{secs:02d}')
            self.btn_start.setToolTip(('Đang mở tool · ' if starting else
                                       'Đã tạm dừng · ' if paused else 'Đang chạy · ') +
                                      f'{hours:02d}:{minutes:02d}:{secs:02d}')
        else:
            self.btn_start.setText('Bắt đầu')
            self.btn_start.setAccessibleName('Bắt đầu')
            self.btn_start.setToolTip('Bắt đầu chu trình tự động')
        self.btn_stop.setText(f'Đang dừng {dots}' if stopping else 'Dừng')
        self.btn_start.setIcon(self.style().standardIcon(
            QStyle.SP_MediaPlay if paused or not running else QStyle.SP_MediaPause))
        for button, highlighted in (
            (self.btn_start, (starting or running) and not paused and not stopping),
            (self.btn_stop, stopping),
        ):
            if button.property('active') != highlighted:
                button.setProperty('active', highlighted)
                button.style().unpolish(button)
                button.style().polish(button)
        if self.btn_start.property('paused') != paused:
            self.btn_start.setProperty('paused', paused)
            self.btn_start.style().unpolish(self.btn_start)
            self.btn_start.style().polish(self.btn_start)
        if starting or (running and not paused) or stopping:
            if not self._control_timer.isActive():
                self._control_timer.start()
        else:
            self._control_timer.stop()

    def _animate_controls(self):
        self._control_frame = (self._control_frame + 1) % 3
        self._sync_controls()

    def _init_ui(self):
        main_widget = QWidget()
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)
        main_layout.setSizeConstraint(QLayout.SetNoConstraint)
        main_layout.setContentsMargins(12, 12, 12, 12)
        main_layout.setSpacing(10)
        self._compact_mode = False

        self.navigation_widget = QWidget()
        navigation_root = QHBoxLayout(self.navigation_widget)
        navigation_root.setContentsMargins(0, 0, 0, 0)
        navigation_root.setSpacing(8)
        self.navigation_root = navigation_root
        navigation_controls = QWidget()
        navigation = QHBoxLayout(navigation_controls)
        navigation.setContentsMargins(0, 0, 0, 0)
        self.navigation_layout = navigation
        navigation_root.addWidget(navigation_controls)
        self.tab_selector = QComboBox()
        self.tab_selector.setAccessibleName('Chuyển tab')
        self.tab_selector.setToolTip('Chọn tab muốn hiển thị')
        self.tab_selector.setMinimumWidth(160)
        self.tab_selector.hide()
        navigation.addWidget(self.tab_selector)
        self.btn_compact = QPushButton('Thu gọn')
        self.btn_compact.setProperty('role', 'headerControl')
        self.btn_compact.setCheckable(True)
        self.btn_compact.setToolTip('Thu nhỏ cửa sổ và chuyển tab bằng ô chọn')
        self.btn_compact.toggled.connect(self._set_compact_mode)
        navigation.addWidget(self.btn_compact)
        self.btn_to_tray = QPushButton('Ẩn xuống khay ngay')
        self.btn_to_tray.setProperty('role', 'headerControl')
        self.btn_to_tray.setIcon(self.style().standardIcon(QStyle.SP_TitleBarMinButton))
        self.btn_to_tray.setToolTip('Thu gọn xuống khay hệ thống')
        self.btn_to_tray.setAccessibleName('Thu gọn xuống khay hệ thống')
        self.btn_to_tray.clicked.connect(self._minimize_to_tray)
        self.btn_pin = QPushButton('Luôn trên cùng')
        self.btn_pin.setProperty('role', 'headerControl')
        self.btn_pin.setCheckable(True)
        self.btn_pin.setToolTip('Giữ cửa sổ CoCAttack trên các cửa sổ khác')
        self.btn_pin.toggled.connect(self._set_always_on_top)
        navigation.addWidget(self.btn_pin)
        self.btn_theme = QPushButton()
        self.btn_theme.setProperty('role', 'headerControl')
        self.btn_theme.setAccessibleName('Chuyển theme sáng tối')
        self.btn_theme.clicked.connect(self._toggle_theme)
        navigation.addWidget(self.btn_theme)
        size_label = QLabel('Cỡ chữ')
        navigation.addWidget(size_label)
        self.combo_text_size = QComboBox()
        self.combo_text_size.setAccessibleName('Cỡ chữ giao diện')
        for label, value in (('Nhỏ', 'small'), ('Vừa', 'medium'), ('Lớn', 'large')):
            self.combo_text_size.addItem(label, value)
        self.combo_text_size.setCurrentIndex(max(0, self.combo_text_size.findData(
            self.settings.get('ui_text_size', 'medium'))))
        self.combo_text_size.currentIndexChanged.connect(self._change_text_size)
        navigation.addWidget(self.combo_text_size)
        self._update_theme_button()
        self.navigation_scroll = QScrollArea()
        self.navigation_scroll.setFrameShape(QFrame.NoFrame)
        self.navigation_scroll.setWidgetResizable(True)
        self.navigation_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.navigation_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.navigation_scroll.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.navigation_scroll.setFixedHeight(58)
        self.navigation_scroll.setWidget(self.navigation_widget)
        main_layout.addWidget(self.navigation_scroll)

        # 1. Top Control Bar
        top_bar = self._create_top_bar()
        navigation_root.insertWidget(0, self.device_row)
        navigation_root.insertStretch(1)
        main_layout.addLayout(top_bar)

        # 2. Main Tab Widget
        self.tabs = QTabWidget()
        self.tabs.setMinimumWidth(0)
        self.tabs.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.village_tab_bar = VillageTabBar(self.tabs)
        self.village_tab_bar.set_theme(self.settings.get('ui_theme', 'dark'))
        self.tabs.setTabBar(self.village_tab_bar)
        self.tabs.addTab(self._scrollable_tab(self._create_dashboard_tab(), 'Tổng quan',
                                          'Theo dõi bot, tài nguyên, trận đấu và nhật ký trong một nơi.'), "Tổng quan")
        self.main_village_tab = self.tabs.addTab(self._scrollable_tab(self._create_village_tab(), 'Làng chính',
                                          'Chọn mục tiêu và các việc tự động trong làng chính.'), "Làng chính")
        self.village_tab_bar.main_village = self.main_village_tab
        self.main_attack_tab = self.tabs.addTab(
            self._scrollable_tab(self._create_combat_tab(), 'Tấn công làng chính',
                                 'Thiết lập chiến thuật, cách thả quân và điều kiện kết thúc trận.'), "Tấn công"
        )
        self.builder_village_tab = self.tabs.addTab(self._scrollable_tab(self._create_builder_tab(), 'Làng đêm',
                                               'Quản lý farm, quân đội và nâng cấp ở làng đêm.'), "Làng đêm")
        self.device_tab = self.tabs.addTab(self._scrollable_tab(self._create_optimizer_tab(), 'Thiết bị',
                                             'Kết nối, tối ưu MuMu và quản lý cấu hình tool.'), "Thiết bị")
        self.tabs.addTab(self._scrollable_tab(self._create_schedule_tab(), 'Hẹn giờ',
                                             'Đặt thời điểm tạm dừng hoặc đóng tool.'), 'Hẹn giờ')
        for index in range(self.tabs.count()):
            self.tab_selector.addItem(self.tabs.tabText(index))
        self.tab_selector.activated[int].connect(self._select_tab)
        self.tabs.currentChanged.connect(self.tab_selector.setCurrentIndex)
        self.village_tab_bar.disabled_clicked.connect(self._select_tab)

        def update_main_attack_tab(enabled):
            self.tabs.setTabEnabled(self.main_attack_tab, enabled)
            self.tab_selector.setItemText(self.main_attack_tab, self.tabs.tabText(self.main_attack_tab)
                                          + ('' if enabled else ' (đã tắt)'))
            self._highlight_active_village()

        update_main_attack_tab(self.chk_main_village.isChecked())
        self.chk_main_village.toggled.connect(update_main_attack_tab)
        self.chk_builder_village.toggled.connect(self._highlight_active_village)
        subtab_switches = (
            self.chk_main_village, self.chk_walls, self.chk_farm_finish,
            self.chk_auto_buildings, self.chk_buy_new, self.chk_upgrade_army,
            self.chk_auto_surr, self.chk_builder_village,
            self.chk_restart, self.chk_reconnect, self.chk_periodic_restart,
            *self.builder_tasks.values(),
        )
        for switch in subtab_switches:
            switch.toggled.connect(self._update_active_subtabs)
        self._update_active_subtabs()

        main_layout.addWidget(self.tabs)

        # Status Bar
        self.statusBar().showMessage("Sẵn sàng. Hãy chọn thiết bị và bấm 'Bắt Đầu'")
        QTimer.singleShot(0, self._update_expanded_minimum)

    def _select_tab(self, index):
        if self.tabs.isTabEnabled(index):
            self.tabs.setCurrentIndex(index)
            return
        if index == self.main_attack_tab:
            if self._compact_mode:
                self.tabs.setCurrentIndex(self.main_village_tab)
            self.village_tab_bar.flash_main_village()
            self.statusBar().showMessage('Bật farm trong tab Làng chính để mở cài đặt tấn công.', 5000)
        self.tab_selector.setCurrentIndex(self.tabs.currentIndex())

    def _highlight_active_village(self):
        battles = self.bot.village_state.live_battle
        colors = {}
        for village, tab, enabled in (
            ('main', self.main_village_tab, self.chk_main_village.isChecked()),
            ('builder', self.builder_village_tab, self.chk_builder_village.isChecked()),
        ):
            fighting = self.bot.is_running and not self.bot.is_paused and bool(battles[village])
            status = 'Đang đánh' if fighting else 'Đã bật' if enabled else 'Đã tắt'
            if fighting or enabled:
                colors[tab] = '#34d399' if fighting else '#fb923c'
            self.tabs.setTabToolTip(tab, status)
            label = self.tabs.tabText(tab)
            self.tab_selector.setItemText(tab, f'{label} · {status}')
        self.village_tab_bar.set_village_colors(colors)

    def _update_active_subtabs(self, *_):
        main = self.chk_main_village.isChecked()
        builder = self.chk_builder_village.isChecked()
        main_color, builder_color = '#fb923c', '#aa8ce6'
        self.village_subtabs.tabBar().set_active_dots({
            index: main_color for index, active in enumerate((
                main,
                main and (self.chk_walls.isChecked() or self.chk_farm_finish.isChecked()),
                main and (self.chk_auto_buildings.isChecked() or self.chk_buy_new.isChecked()),
                main and self.chk_upgrade_army.isChecked(),
            )) if active
        })
        self.combat_subtabs.tabBar().set_active_dots({
            index: main_color for index, active in enumerate((
                main, main, main, main and self.chk_auto_surr.isChecked(),
            )) if active
        })
        tasks = self.builder_tasks
        self.builder_subtabs.tabBar().set_active_dots({
            index: builder_color for index, active in enumerate((
                builder,
                builder and tasks['builder_base_upgrade_walls'].isChecked(),
                builder and tasks['builder_base_upgrade_buildings'].isChecked(),
                builder,
            )) if active
        })
        self.device_subtabs.tabBar().set_active_dots({
            1: '#5ec6ef'} if any(check.isChecked() for check in
                                   (self.chk_restart, self.chk_reconnect, self.chk_periodic_restart)) else {})
        self.schedule_subtabs.tabBar().set_active_dots(
            {1: '#fbbf24'} if self.action_timer.remaining() is not None else {})

    def _set_compact_mode(self, compact):
        if compact == self._compact_mode:
            return
        self._compact_mode = compact
        if compact:
            self._expanded_geometry = self.saveGeometry()
            self.navigation_layout.removeWidget(self.btn_compact)
            self.navigation_layout.removeWidget(self.btn_pin)
            self.actions_layout.addWidget(self.btn_compact)
            self.actions_layout.addWidget(self.btn_pin)
            self.navigation_scroll.hide()
            self.overview_nav.hide()
            self.device_row.hide()
            self.state_badge.hide()
            self.dashboard_right_layout.removeWidget(self.log_console)
            self.compact_log_layout.addWidget(self.log_console)
            self.dashboard_content.hide()
            self.compact_log_host.show()
            self.compact_log_host.setMinimumHeight(220)
            self.overview_title_label.hide()
            self.overview_hint_label.hide()
        else:
            self.actions_layout.removeWidget(self.btn_compact)
            self.actions_layout.removeWidget(self.btn_pin)
            self.navigation_layout.insertWidget(1, self.btn_compact)
            self.navigation_layout.insertWidget(2, self.btn_pin)
            self.navigation_scroll.show()
            self.overview_nav.show()
            self.device_row.show()
            self.state_badge.show()
            self.compact_log_layout.removeWidget(self.log_console)
            self.dashboard_right_layout.addWidget(self.log_console)
            self.compact_log_host.hide()
            self.dashboard_content.show()
        self.tabs.tabBar().show()
        self.tab_selector.hide()
        self.btn_compact.setText('Mở rộng' if compact else 'Thu gọn')
        self.btn_compact.setToolTip('Khôi phục giao diện đầy đủ' if compact else 'Chỉ hiện điều khiển, tab và nhật ký')
        self.top_bar.setDirection(QBoxLayout.LeftToRight if compact or self.width() >= 1100
                                  else QBoxLayout.TopToBottom)
        self.statusBar().setVisible(not compact)
        self.centralWidget().layout().activate()
        if compact:
            self.showNormal()
            self.setMinimumSize(0, 0)
            self.setMaximumSize(16777215, 16777215)
            self.resize(760, 430)
            self.setFixedSize(760, 430)
        else:
            self.setMaximumSize(16777215, 16777215)
            self._update_expanded_minimum()
            self.restoreGeometry(self._expanded_geometry)
            self._fit_overview()

    def _update_expanded_minimum(self):
        if getattr(self, '_compact_mode', False) or not hasattr(self, 'village_subtabs'):
            return
        # Keep the widest village form visible at the selected text size.
        content_width = max(self.village_subtabs.widget(i).sizeHint().width()
                            for i in range(self.village_subtabs.count()))
        self.setMinimumSize(max(1040, content_width + 70), 620)

    def _set_always_on_top(self, enabled):
        geometry = self.saveGeometry()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, enabled)
        self.show()
        self.restoreGeometry(geometry)

    def _minimize_to_tray(self):
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.showMinimized()
            return
        if not hasattr(self, '_tray_icon'):
            tray = QSystemTrayIcon(self.style().standardIcon(QStyle.SP_ComputerIcon), self)
            tray.setToolTip('CoCAttack · Nhấp để mở cửa sổ')
            menu = QMenu(self)
            restore_action = QAction('Mở CoCAttack', menu)
            restore_action.triggered.connect(self._restore_from_tray)
            menu.addAction(restore_action)
            exit_action = QAction('Thoát CoCAttack', menu)
            exit_action.triggered.connect(self._exit_completely)
            menu.addAction(exit_action)
            tray.setContextMenu(menu)
            tray.activated.connect(lambda reason: self._restore_from_tray()
                                   if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) else None)
            self._tray_icon = tray
        QApplication.instance().setQuitOnLastWindowClosed(False)
        self._tray_icon.show()
        self.hide()

    def _restore_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def _exit_completely(self):
        self._force_quit = True
        self.close()
        QApplication.instance().quit()

    def _change_startup_mode(self, _index):
        mode = self.combo_startup_mode.currentData()
        if mode not in STARTUP_MODES:
            return
        previous = self.settings.get('ui_startup_mode', 'off')
        if mode == previous:
            return
        try:
            configure_startup(mode)
        except Exception as exc:
            self.combo_startup_mode.blockSignals(True)
            self.combo_startup_mode.setCurrentIndex(max(0, self.combo_startup_mode.findData(previous)))
            self.combo_startup_mode.blockSignals(False)
            QMessageBox.warning(self, 'Tự khởi động', f'Không cập nhật được chế độ khởi động: {exc}')
            return
        self.settings.set('ui_startup_mode', mode)
        self.statusBar().showMessage('Đã cập nhật chế độ tự khởi động.', 4000)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'top_bar') and not getattr(self, '_compact_mode', False):
            direction = QBoxLayout.LeftToRight if self.width() >= 1100 else QBoxLayout.TopToBottom
            if self.top_bar.direction() != direction:
                self.top_bar.setDirection(direction)
        if hasattr(self, 'activity_btn'):
            self._refresh_activity_button()

    def _scrollable_tab(self, content, title, hint):
        if title != 'Tổng quan':
            content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
            return content
        scroll = QScrollArea()
        scroll.setMinimumWidth(0)
        scroll.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        page = QWidget()
        page.setMinimumWidth(0)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 4 if title == 'Tổng quan' else 12, 14, 14)
        layout.setSpacing(10)
        title_label = QLabel(title)
        title_label.setProperty('role', 'pageTitle')
        hint_label = QLabel(hint)
        hint_label.setProperty('role', 'pageHint')
        hint_label.setWordWrap(True)
        title_label.hide()
        hint_label.hide()
        if title == 'Tổng quan':
            self.overview_title_label = title_label
            self.overview_hint_label = hint_label
        layout.addWidget(title_label)
        layout.addWidget(hint_label)
        content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout.addWidget(content)
        if title == 'Tổng quan':
            self.compact_log_host = QGroupBox('Nhật ký hoạt động')
            self.compact_log_layout = QVBoxLayout(self.compact_log_host)
            self.compact_log_host.hide()
            layout.addWidget(self.compact_log_host)
        layout.addStretch()
        scroll.setWidget(page)
        container = QWidget()
        container_layout = QVBoxLayout(container)
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(0)
        container_layout.addWidget(self.overview_nav)
        container_layout.addWidget(scroll, 1)
        return container

    def _apply_appearance(self):
        theme = self.settings.get('ui_theme', 'dark')
        size = self.settings.get('ui_text_size', 'medium')
        self.setStyleSheet(theme_qss(theme, size))
        if hasattr(self, 'village_tab_bar'):
            self.village_tab_bar.set_theme(theme)
        if hasattr(self, 'overview_nav'):
            self.overview_nav.set_theme(theme)
        for tabs in getattr(self, '_subtabs', ()):
            tabs.tabBar().set_theme(theme)
            QTimer.singleShot(0, lambda box=tabs: self._fit_subtabs(box))
        if hasattr(self, 'btn_theme'):
            self._update_theme_button()
        if hasattr(self, 'overview'):
            QTimer.singleShot(0, self._fit_overview)
        if hasattr(self, 'village_subtabs'):
            QTimer.singleShot(0, self._update_expanded_minimum)

    def _update_theme_button(self):
        theme = self.settings.get('ui_theme', 'dark')
        self.btn_theme.setText('Sáng' if theme == 'dark' else 'Tối')
        self.btn_theme.setToolTip('Đổi màu giao diện sáng / tối')

    def _toggle_theme(self):
        theme = 'light' if self.settings.get('ui_theme', 'dark') == 'dark' else 'dark'
        self.settings.set('ui_theme', theme)
        self._apply_appearance()

    def _change_text_size(self, index):
        size = self.combo_text_size.itemData(index)
        if size in TEXT_SIZES and size != self.settings.get('ui_text_size', 'medium'):
            self.settings.set('ui_text_size', size)
            self._apply_appearance()

    def _create_top_bar(self) -> QHBoxLayout:
        self._control_frame = 0
        self._control_timer = QTimer(self)
        self._control_timer.setInterval(500)
        self._control_timer.timeout.connect(self._animate_controls)
        bar = QBoxLayout(QBoxLayout.LeftToRight)
        self.top_bar = bar
        bar.setSpacing(10)
        self.device_row = QWidget()
        devices = QHBoxLayout(self.device_row)
        devices.setContentsMargins(0, 0, 0, 0)
        actions = QHBoxLayout()
        self.actions_layout = actions

        # Device selector
        dev_lbl = QLabel("Thiết Bị (ADB):")
        dev_lbl.setObjectName('sectionDetail')
        self.device_combo = QComboBox()
        self.device_combo.setMinimumWidth(240)
        self.device_combo.setMaximumWidth(290)

        self.btn_refresh = QPushButton("Quét")
        self.btn_refresh.setProperty('role', 'headerControl')
        self.btn_refresh.setFixedWidth(78)
        self.btn_refresh.clicked.connect(self._refresh_devices)

        self.btn_connect = QPushButton("Kết Nối")
        self.btn_connect.setProperty('role', 'headerControl')
        self.btn_connect.setFixedWidth(102)
        self.btn_connect.clicked.connect(self._connect_device)

        # State badge
        self.state_badge = QLabel("ĐANG CHỜ")
        self.state_badge.setObjectName('stateBadge')

        # Action Buttons
        self.btn_start = QPushButton("Bắt đầu")
        self.btn_start.setObjectName("btn_start")
        self.btn_start.clicked.connect(self._toggle_run_pause)

        self.btn_stop = QPushButton("Dừng")
        self.btn_stop.setObjectName("btn_stop")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop_bot)
        for button, icon, tooltip in (
            (self.btn_start, QStyle.SP_MediaPlay, 'Bắt đầu chu trình tự động'),
            (self.btn_stop, QStyle.SP_MediaStop, 'Kết thúc chu trình và lưu thống kê'),
        ):
            button.setIcon(self.style().standardIcon(icon))
            button.setMinimumWidth(134)
            button.setToolTip(tooltip)

        self.btn_quick_opt = QPushButton("Tối ưu MuMu")
        self.btn_quick_opt.setObjectName("btn_optimize")
        self.btn_quick_opt.clicked.connect(self._run_optimization)
        self.btn_quick_opt.hide()

        devices.addWidget(dev_lbl)
        devices.addWidget(self.device_combo)
        devices.addWidget(self.btn_refresh)
        devices.addWidget(self.btn_connect)
        actions.addWidget(self.state_badge)
        self.activity_btn = QPushButton('Sẵn sàng · Chọn thiết bị và bắt đầu')
        self.activity_btn.setObjectName('activityButton')
        self.activity_btn.setToolTip('Bấm để mở mục liên quan đến hoạt động hiện tại')
        self.activity_btn.clicked.connect(self._open_activity_tab)
        self.activity_btn.setMinimumWidth(180)
        self.activity_btn.setMaximumWidth(16777215)
        self.activity_btn.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self._activity_text = self.activity_btn.text()
        self._activity_section = 'overview'
        self._activity_village = 'idle'
        actions.addWidget(self.activity_btn, 1)
        actions.addWidget(self.btn_quick_opt)
        actions.addWidget(self.btn_start)
        actions.addWidget(self.btn_stop)
        bar.addLayout(actions)

        return bar

    def _create_dashboard_tab(self) -> QWidget:
        widget = QWidget()
        self.dashboard_content = widget
        layout = QVBoxLayout(widget)
        layout.setAlignment(Qt.AlignTop)
        layout.setContentsMargins(0, 0, 0, 8)
        layout.setSpacing(10)

        overview = QTabWidget()
        self.overview = overview
        overview_bar = BubbleTabBar(overview)
        overview_bar.set_theme(self.settings.get('ui_theme', 'dark'))
        overview.setTabBar(overview_bar)
        overview.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout.addWidget(overview)
        common_page, main_page, builder_page = QWidget(), QWidget(), QWidget()
        overview.addTab(common_page, 'Chung')
        overview.addTab(main_page, 'Làng chính')
        overview.addTab(builder_page, 'Làng đêm')
        self.overview_nav = BubbleTabBar()
        self.overview_nav.set_theme(self.settings.get('ui_theme', 'dark'))
        for index in range(overview.count()):
            self.overview_nav.addTab(overview.tabText(index))
        self.overview_nav.currentChanged.connect(overview.setCurrentIndex)
        overview.currentChanged.connect(self.overview_nav.setCurrentIndex)
        overview.tabBar().hide()
        common_layout = QVBoxLayout(common_page)
        main_layout = QVBoxLayout(main_page)
        builder_layout = QVBoxLayout(builder_page)

        common_grid = QGridLayout()
        self.card_total_battles = StatCard('', 'Tổng trận hoàn tất', tone='blue')
        self.card_main_total = StatCard('', 'Trận làng chính', tone='blue')
        self.card_builder_total = StatCard('', 'Trận làng đêm', tone='purple')
        self.card_uptime = StatCard('', 'Tổng thời gian bot chạy', '00:00', '#10B981', 'green')
        self.card_cpu = StatCard('', 'CPU tool / MuMu', '— / —', tone='slate')
        self.card_fps = StatCard('', 'FPS game MuMu', '—', tone='slate')
        common_cards = (self.card_total_battles, self.card_main_total, self.card_builder_total,
                        self.card_uptime, self.card_cpu, self.card_fps)
        for index, card in enumerate(common_cards):
            common_grid.addWidget(card, 0, index)
        common_layout.addLayout(common_grid)
        common_note = QLabel('Tổng trận cộng hai làng. Thời gian bot chạy tính chung và dừng đếm khi tạm dừng.')
        common_note.setWordWrap(True)
        common_layout.addWidget(common_note)
        common_layout.addStretch()

        cards_layout = QGridLayout()
        cards_layout.setSpacing(8)
        self.card_battles = StatCard("", "Trận đã đánh", "0", "#38BDF8", "blue")
        self.card_stars = StatCard("⭐", "Sao", "0", "#FBBF24", "amber")
        self.card_gold = StatCard("", "Vàng thu được", "0", "#F59E0B", "gold")
        self.card_elixir = StatCard("", "Dầu thu được", "0", "#EC4899", "elixir")
        self.card_dark = StatCard("", "Hắc dầu", "0", "#A855F7", "dark_elixir")
        main_cards = (self.card_battles, self.card_stars, self.card_gold,
                      self.card_elixir, self.card_dark)
        for index, card in enumerate(main_cards):
            cards_layout.addWidget(card, 0, index)
        main_layout.addLayout(cards_layout)

        rates_layout = QGridLayout()
        rates_layout.setSpacing(8)
        self.card_gold_hour = StatCard('', 'Vàng RPH', '—', tone='gold')
        self.card_elixir_hour = StatCard('', 'Tiên dược RPH', '—', tone='elixir')
        self.card_dark_hour = StatCard('', 'Hắc tiên dược RPH', '—', tone='dark_elixir')
        self.card_attack_scan = StatCard('', 'Lượt đánh / đã quét', '0 / 0', tone='blue')
        for card in (self.card_gold_hour, self.card_elixir_hour, self.card_dark_hour):
            card.setToolTip('RPH lấy tài nguyên tối đa 50 trận làng chính đã xác nhận, chia theo thời gian chu kỳ làng chính. Chỉ dùng mẫu ghi từ khi tool tách thời gian hai làng.')
        self.card_attack_scan.setToolTip('Số lượt triển khai tấn công mới / tổng căn cứ đã đánh giá, gồm cả đánh và bỏ qua. Trận đã đánh ở hàng trên là số trận xác nhận kết thúc.')
        self.card_cpu.setToolTip('CPU tiến trình tool / tổng CPU các tiến trình MuMu. Chuẩn hóa theo tổng CPU máy (0–100%); lấy mẫu mỗi 2 giây.')
        self.card_fps.setToolTip('FPS từ thời điểm trình bày khung hình SurfaceView của game qua SurfaceFlinger; không phải FPS preview. — khi không có mẫu hợp lệ hoặc khung hình không cập nhật.')
        rate_cards = (self.card_gold_hour, self.card_elixir_hour, self.card_dark_hour, self.card_attack_scan)
        for index, card in enumerate(rate_cards):
            rates_layout.addWidget(card, 0, index)
        self._dashboard_card_groups = ((common_grid, common_cards), (cards_layout, main_cards),
                                       (rates_layout, rate_cards))
        main_layout.addLayout(rates_layout)
        self.lbl_rph_window=QLabel('RPH · 0/50 trận gần nhất có dữ liệu')
        main_layout.addWidget(self.lbl_rph_window)
        self.lbl_avg_battle=QLabel('TB thời gian mỗi trận: —')
        self.lbl_avg_battle.setToolTip('Thời gian đánh trung bình của các trận đã kết thúc và có ghi thời lượng. Không gồm tìm nhà/bảo trì; các trận cũ chưa có thời lượng không được dùng làm mẫu.')
        main_layout.addWidget(self.lbl_avg_battle)
        self.lbl_fastest_50=QLabel('Kỷ lục 50%: chưa có số đo')
        self.lbl_fastest_50.setToolTip('Thời gian nhanh nhất tool đã xác minh từ lúc bắt đầu thả quân đến 50%. Không ghi số đo dưới 5 giây hoặc trận bắt đầu theo dõi giữa chừng.')
        main_layout.addWidget(self.lbl_fastest_50)
        self.btn_reset_fastest=QPushButton('Đặt lại số đo kỷ lục 50%')
        self.btn_reset_fastest.setObjectName('recordReset')
        self.btn_reset_fastest.setToolTip('Xóa số đo kỷ lục đã lưu, giữ nguyên loot, lượt đánh và thời gian.')
        self.btn_reset_fastest.clicked.connect(self._reset_fastest_record)
        main_layout.addWidget(self.btn_reset_fastest, alignment=Qt.AlignLeft)

        builder_grid = QGridLayout()
        self.card_builder_started = StatCard('', 'Trận đã vào', tone='purple')
        self.card_builder_stars = StatCard('', 'Sao đạt được', tone='amber')
        self.card_builder_uptime = StatCard('', 'Thời gian làng đêm', '00:00', tone='green')
        for index, card in enumerate((self.card_builder_started, self.card_builder_stars, self.card_builder_uptime)):
            builder_grid.addWidget(card, 0, index)
        builder_layout.addLayout(builder_grid)
        builder_note = QLabel('Ghi nhận thông số làng đêm (tối đa 6 sao mỗi trận gồm 2 stage).')
        builder_note.setWordWrap(True)
        builder_layout.addWidget(builder_note)
        builder_layout.addStretch()

        overview.currentChanged.connect(self._fit_overview)
        QTimer.singleShot(0, self._fit_overview)

        splitter = QSplitter(Qt.Horizontal)
        self.dashboard_splitter = splitter

        left_box = QGroupBox("Thông số hoạt động tại làng")
        left_layout = QVBoxLayout(left_box)
        self.village_status = VillageLiveStatusWidget()
        self.village_status.scan_button.clicked.connect(self._manual_resource_scan)
        left_layout.addWidget(self.village_status)

        right_box = QGroupBox("Nhật ký")
        right_layout = QVBoxLayout(right_box)
        self.dashboard_right_layout = right_layout
        self.log_console = LogConsole()
        right_layout.addWidget(self.log_console)

        splitter.addWidget(left_box)
        splitter.addWidget(right_box)
        splitter.setStretchFactor(0, 5)
        splitter.setStretchFactor(1, 4)

        layout.addWidget(splitter, 1)
        return widget

    def _fit_overview(self):
        page_height = self.overview.currentWidget().sizeHint().height()
        self.overview.setFixedHeight(page_height + 6)
        if hasattr(self, 'dashboard_content'):
            self.dashboard_content.setMaximumHeight(16777215)
            self.dashboard_content.setFixedHeight(self.dashboard_content.sizeHint().height())

    def _make_subtabs(self, sections):
        tabs = QTabWidget()
        bar = BubbleTabBar(tabs)
        bar.set_theme(self.settings.get('ui_theme', 'dark'))
        tabs.setTabBar(bar)
        tabs.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        for name, widgets in sections:
            page = QWidget()
            column = QVBoxLayout(page)
            column.setContentsMargins(8, 8, 8, 8)
            column.setSpacing(8)
            for item in widgets:
                column.addWidget(item)
            column.addStretch()
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
            scroll.setWidget(page)
            tabs.addTab(scroll, name)
        self._subtabs.append(tabs)
        return tabs

    @staticmethod
    def _fit_subtabs(tabs):
        page = tabs.currentWidget()
        if isinstance(page, QScrollArea):
            return
        if page is not None:
            tabs.setFixedHeight(page.sizeHint().height() + tabs.tabBar().sizeHint().height() + 14)

    def _create_village_tab(self) -> QWidget:
        widget = QWidget()
        layout = QGridLayout(widget)

        self.chk_main_village = QCheckBox("Bật farm làng chính")
        self.chk_main_village.setChecked(self.settings.get("main_village_enabled", True))
        def save_main_farm(enabled):
            if enabled != self.settings.get("main_village_enabled", True):
                self.settings.set("main_village_enabled", enabled)

        self.chk_main_village.toggled.connect(save_main_farm)
        layout.addWidget(self.chk_main_village, 0, 0, 1, 2)

        # Group 1: Resource Search Thresholds
        grp_loot = QGroupBox("Điều kiện chọn đối thủ")
        form_loot = QFormLayout(grp_loot)

        self.spin_gold = QSpinBox()
        self.spin_gold.setRange(0, 3000000)
        self.spin_gold.setSingleStep(50000)
        self.spin_gold.setValue(self.settings.get("min_gold", 600000))
        self.spin_gold.valueChanged.connect(lambda v: self.settings.set("min_gold", v))

        self.spin_elixir = QSpinBox()
        self.spin_elixir.setRange(0, 3000000)
        self.spin_elixir.setSingleStep(50000)
        self.spin_elixir.setValue(self.settings.get("min_elixir", 600000))
        self.spin_elixir.valueChanged.connect(lambda v: self.settings.set("min_elixir", v))

        self.spin_dark = QSpinBox()
        self.spin_dark.setRange(0, 50000)
        self.spin_dark.setSingleStep(500)
        self.spin_dark.setValue(self.settings.get("min_dark_elixir", 4000))
        self.spin_dark.valueChanged.connect(lambda v: self.settings.set("min_dark_elixir", v))

        self.spin_skips = QSpinBox()
        self.spin_skips.setRange(5, 200)
        self.spin_skips.setValue(self.settings.get("max_search_skips", 50))
        self.spin_skips.valueChanged.connect(lambda v: self.settings.set("max_search_skips", v))

        self.combo_loot_filter=QComboBox()
        self.combo_loot_filter.addItems(['(Vàng hoặc dầu) và hắc dầu — như hiện tại','Đạt tất cả loại đã tích (AND)','Đạt ít nhất một loại đã tích (OR)'])
        filter_modes=['legacy','all','any']
        selected_mode=self.settings.get('loot_filter_mode','legacy')
        self.combo_loot_filter.setCurrentIndex(filter_modes.index(selected_mode) if selected_mode in filter_modes else 0)
        self.combo_loot_filter.currentIndexChanged.connect(lambda i:self.settings.set('loot_filter_mode',filter_modes[i]))
        form_loot.addRow('Ghép điều kiện tài nguyên:',self.combo_loot_filter)
        self.loot_filter_checks={}
        for key,label,control in (('gold','Vàng tối thiểu:',self.spin_gold),('elixir','Dầu tối thiểu:',self.spin_elixir),('dark_elixir','Hắc dầu tối thiểu:',self.spin_dark)):
            check=QCheckBox(label)
            check.setChecked(self.settings.get('loot_filter_'+key,True))
            check.toggled.connect(lambda checked,name=key:self.settings.set('loot_filter_'+name,checked))
            self.loot_filter_checks[key]=check
            form_loot.addRow(check,control)
            control.setEnabled(check.isChecked())
            check.toggled.connect(control.setEnabled)
        form_loot.addRow("Số lần bỏ qua tối đa:", self.spin_skips)
        reset_loot_row = QHBoxLayout()
        reset_loot_row.addStretch()
        reset_loot_row.addWidget(self._make_reset_button(self._reset_loot_defaults))
        form_loot.addRow(reset_loot_row)
        self.grp_loot = grp_loot

        # Group 2: Automation Toggles
        grp_auto = QGroupBox("Chăm sóc làng")
        vbox_auto = QVBoxLayout(grp_auto)

        self.chk_collect = QCheckBox("Thu hoạch vàng và dầu")
        self.chk_collect.setChecked(self.settings.get("auto_collect", True))
        self.chk_collect.stateChanged.connect(lambda s: self.settings.set("auto_collect", s == Qt.Checked))

        self.chk_train = QCheckBox("Dùng đội quân nhanh")
        self.chk_train.setChecked(self.settings.get("auto_train_army", True))
        self.chk_train.stateChanged.connect(lambda s: self.settings.set("auto_train_army", s == Qt.Checked))

        self.chk_request = QCheckBox("Xin lính hội")
        self.chk_request.setChecked(self.settings.get("auto_request_troops", False))
        self.chk_request.stateChanged.connect(lambda s: self.settings.set("auto_request_troops", s == Qt.Checked))

        self.chk_assign_apprentice = QCheckBox('Tự giao Phụ tá thợ xây')
        self.chk_assign_apprentice.setChecked(self.settings.get('auto_assign_apprentice', False))
        self.chk_assign_apprentice.setToolTip(
            'Khi Phụ tá sẵn sàng, giao cho công trình đầu tiên trong danh sách trong một lượt.'
        )
        self.chk_assign_apprentice.toggled.connect(
            lambda enabled: self.settings.set('auto_assign_apprentice', enabled)
        )
        vbox_auto.addWidget(self.chk_assign_apprentice)
        vbox_auto.addWidget(self.chk_collect)
        grp_treasury=QGroupBox('Treasury · kho bạc Clan Castle')
        vbox_treasury=QVBoxLayout(grp_treasury)
        self.chk_treasury=QCheckBox('Thu tài nguyên Treasury của Clan Castle')
        self.chk_treasury.setChecked(self.settings.get('auto_collect_treasury',False))
        self.chk_treasury.stateChanged.connect(lambda state:self.settings.set('auto_collect_treasury',state==Qt.Checked))
        vbox_treasury.addWidget(self.chk_treasury)
        self.chk_treasury_star=QCheckBox('  Theo dõi và nhận Star Bonus khi đủ sao, đến giờ')
        self.chk_treasury_war=QCheckBox('  Theo dõi giờ thưởng hội chiến')
        for key,check in (('treasury_star_bonus_enabled',self.chk_treasury_star),('treasury_war_rewards_enabled',self.chk_treasury_war)):
            check.setChecked(self.settings.get(key,True))
            check.setEnabled(self.chk_treasury.isChecked())
            check.stateChanged.connect(lambda state,name=key:self.settings.set(name,state==Qt.Checked))
            self.chk_treasury.toggled.connect(check.setEnabled)
            vbox_treasury.addWidget(check)
        vbox_auto.addWidget(self.chk_train)
        vbox_auto.addWidget(self.chk_request)
        form_auto = QFormLayout()
        self.spin_collect_interval = QSpinBox()
        self.spin_collect_interval.setRange(1, 1440)
        self.spin_collect_interval.setSuffix(' phút')
        self.spin_collect_interval.setValue(self.settings.get('collect_interval_min', 60))
        self.spin_collect_interval.setEnabled(self.chk_collect.isChecked())
        self.spin_collect_interval.setToolTip(
            'Khoảng cách giữa hai lượt thu mỏ làng chính, kể cả khi vừa về sau trận.'
        )
        self.spin_collect_interval.valueChanged.connect(
            lambda value: self.settings.set('collect_interval_min', value)
        )
        self.chk_collect.toggled.connect(self.spin_collect_interval.setEnabled)
        form_auto.addRow('Chu kỳ thu hoạch:', self.spin_collect_interval)
        self.spin_treasury_interval=QSpinBox()
        self.spin_treasury_interval.setRange(1,1440)
        self.spin_treasury_interval.setValue(self.settings.get('treasury_check_interval_min',30))
        self.spin_treasury_interval.valueChanged.connect(lambda value:self.settings.set('treasury_check_interval_min',value))
        form_treasury=QFormLayout()
        form_treasury.addRow('Kiểm tra dự phòng khi chưa rõ giờ (phút):',self.spin_treasury_interval)
        vbox_treasury.addLayout(form_treasury)
        self.spin_treasury_interval.setEnabled(self.chk_treasury.isChecked())
        self.chk_treasury.toggled.connect(self.spin_treasury_interval.setEnabled)
        treasury_note=QLabel('Lưu riêng giờ Star Bonus và hội chiến, cùng trạng thái đã thu/kho trống. Hai ô chọn thời điểm kiểm tra; Treasury thu chung tài nguyên đang có trong kho.')
        treasury_note.setWordWrap(True)
        vbox_treasury.addWidget(treasury_note)
        reset_treasury_row = QHBoxLayout()
        reset_treasury_row.addStretch()
        reset_treasury_row.addWidget(self._make_reset_button(self._reset_treasury_defaults))
        vbox_treasury.addLayout(reset_treasury_row)
        grp_treasury.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        self.spin_train_slot = QSpinBox()
        self.spin_train_slot.setRange(1, 3)
        self.spin_train_slot.setValue(self.settings.get('quick_train_slot', 1))
        self.spin_train_slot.valueChanged.connect(lambda value: self.settings.set('quick_train_slot', value))
        form_auto.addRow('Đội quân nhanh:', self.spin_train_slot)
        self.spin_train_slot.setEnabled(self.chk_train.isChecked())
        self.chk_train.toggled.connect(self.spin_train_slot.setEnabled)
        self.spin_maintenance = QSpinBox()
        self.spin_maintenance.setRange(10, 600)
        self.spin_maintenance.setValue(self.settings.get('maintenance_interval_sec', 45))
        self.spin_maintenance.valueChanged.connect(lambda value: self.settings.set('maintenance_interval_sec', value))
        form_auto.addRow('Kiểm tra làng mỗi (giây):', self.spin_maintenance)
        self.spin_reserve_builders = QSpinBox()
        self.spin_reserve_builders.setRange(0, 6)
        self.spin_reserve_builders.setValue(self.settings.get('reserve_builders', 0))
        self.spin_reserve_builders.valueChanged.connect(lambda v: self.settings.set('reserve_builders', v))
        self.spin_reserve_builders.setToolTip(
            'Nếu có nhiều thợ thường rảnh hơn mức này, bot sẽ nâng công trình theo gợi ý khi đủ tài nguyên. '
            'Đặt 0 để dùng hết thợ thường rảnh cho công trình. Nâng tường vẫn dùng được khi có 1 thợ rảnh.'
        )
        form_auto.addRow('Số thợ rảnh cần duy trì:', self.spin_reserve_builders)
        vbox_auto.addLayout(form_auto)
        reset_auto_row = QHBoxLayout()
        reset_auto_row.addStretch()
        reset_auto_row.addWidget(self._make_reset_button(self._reset_village_care_defaults))
        vbox_auto.addLayout(reset_auto_row)
        layout.addWidget(grp_auto, 2, 0)

        # Group 3: Wall Upgrades
        grp_wall = QGroupBox("Nâng tường")
        form_wall = QFormLayout(grp_wall)

        self.chk_walls = QCheckBox("Nâng tường khi đạt ngưỡng")
        self.chk_walls.setChecked(self.settings.get("auto_upgrade_walls", True))
        self.chk_walls.stateChanged.connect(lambda s: self.settings.set("auto_upgrade_walls", s == Qt.Checked))

        self.combo_wall_res = QComboBox()
        self.combo_wall_res.addItems(["Vàng hoặc dầu", "Vàng", "Dầu"])
        wall_keys = ['both', 'gold', 'elixir']
        self.combo_wall_res.setCurrentIndex(wall_keys.index(self.settings.get('wall_resource', 'both')) if self.settings.get('wall_resource', 'both') in wall_keys else 0)
        self.combo_wall_res.currentIndexChanged.connect(
            lambda i: self.settings.set("wall_resource", ["both", "gold", "elixir"][i])
        )

        self.spin_wall_gold = QSpinBox()
        self.spin_wall_gold.setRange(1000000, 20000000)
        self.spin_wall_gold.setSingleStep(500000)
        self.spin_wall_gold.setValue(self.settings.get("min_gold_to_upgrade_wall", 3000000))
        self.spin_wall_gold.valueChanged.connect(lambda v: self.settings.set("min_gold_to_upgrade_wall", v))

        self.spin_wall_elixir = QSpinBox()
        self.spin_wall_elixir.setRange(0, 30000000)
        self.spin_wall_elixir.setSingleStep(500000)
        self.spin_wall_elixir.setValue(self.settings.get('min_elixir_to_upgrade_wall', 3000000))
        self.spin_wall_elixir.valueChanged.connect(lambda v: self.settings.set('min_elixir_to_upgrade_wall', v))
        for control in (self.spin_wall_gold, self.spin_wall_elixir):
            control.setToolTip('Ngưỡng để mở một lượt nâng. Tài nguyên đã đạt ngưỡng được dùng đến khi không đủ nâng thêm; vàng và dầu được xét riêng.')
        self.chk_wall_lowest = QCheckBox('Ưu tiên nâng tường cấp thấp nhất')
        self.chk_wall_lowest.setChecked(self.settings.get('wall_lowest_first', True))
        self.chk_wall_lowest.toggled.connect(lambda checked: self.settings.set('wall_lowest_first', checked))
        self.chk_wall_lowest.setToolTip('Bật: dùng cấp thấp nhất đã quét qua Layout Editor. Tắt: chọn nhóm tường đầu tiên OCR thấy trong Menu thợ.')

        form_wall.addRow(self.chk_walls)
        form_wall.addRow(self.chk_wall_lowest)
        form_wall.addRow("Tài nguyên dùng để nâng:", self.combo_wall_res)
        form_wall.addRow("Ngưỡng vàng để bắt đầu nâng:", self.spin_wall_gold)
        form_wall.addRow('Ngưỡng dầu để bắt đầu nâng:', self.spin_wall_elixir)
        self.combo_wall_quantity_mode=QComboBox()
        self.combo_wall_quantity_mode.addItems(['Nhận diện nút như hiện tại (OCR/ảnh)','Tọa độ cố định +10 / +1 / -1'])
        self.combo_wall_quantity_mode.setCurrentIndex(1 if self.settings.get('wall_quantity_button_mode','ocr')=='fixed' else 0)
        self.combo_wall_quantity_mode.currentIndexChanged.connect(lambda i:self.settings.set('wall_quantity_button_mode','fixed' if i else 'ocr'))
        self.combo_wall_quantity_mode.setToolTip('Cố định: bấm liền chuỗi +10/+1/-1, không đọc số lượng sau từng lần bấm; đối chiếu tiền sau nâng. Hỗ trợ bố cục có/hết nhẫn trên ảnh 1600×900. Nhận diện: kiểm tra số lượng sau từng bước như trước. Áp dụng từ lần chọn số lượng tiếp theo.')
        form_wall.addRow('Cách bấm số lượng tường:',self.combo_wall_quantity_mode)
        self.spin_wall_fixed_gap=QSpinBox()
        self.spin_wall_fixed_gap.setRange(50,1000)
        self.spin_wall_fixed_gap.setSuffix(' ms')
        self.spin_wall_fixed_gap.setValue(self.settings.get('wall_quantity_fixed_gap_ms',120))
        self.spin_wall_fixed_gap.setEnabled(self.combo_wall_quantity_mode.currentIndex()==1)
        self.spin_wall_fixed_gap.valueChanged.connect(lambda value:self.settings.set('wall_quantity_fixed_gap_ms',value))
        self.combo_wall_quantity_mode.currentIndexChanged.connect(lambda i:self.spin_wall_fixed_gap.setEnabled(i==1))
        form_wall.addRow('Khoảng cách bấm cố định:',self.spin_wall_fixed_gap)
        self.lbl_wall_status = QLabel('Chưa kiểm tra nâng tường.')
        self.chk_wall_hold = QCheckBox('Giữ làng khi lỗi nâng tường và tài nguyên cao')
        self.chk_wall_hold.setChecked(self.settings.get('wall_hold_enabled', True))
        self.chk_wall_hold.toggled.connect(lambda enabled: self.settings.set('wall_hold_enabled', enabled))
        self.chk_wall_hold.setToolTip('Khi nâng lỗi và tài nguyên đạt 125% ngưỡng nâng: ngừng tìm trận, thử lại mỗi 30 giây. Chạy tiếp sau 2 lần đọc dưới 110%. Đây không phải phần trăm dung lượng kho.')
        form_wall.addRow(self.chk_wall_hold)
        self.chk_farm_finish = QCheckBox('Tường đã tối đa: cày đầy kho rồi dừng')
        self.chk_farm_finish.setChecked(self.settings.get('farm_finish_enabled', False))
        self.chk_farm_finish.setToolTip('Bật khi tường đã tối đa: tự động dùng hết số thợ rảnh để nâng công trình (duy trì 0 thợ), sau đó farm cày full 100% kho tài nguyên (vàng/dầu) rồi mới dừng tool.')
        self.lbl_farm_finish_max_badge = QLabel('')
        self.lbl_farm_finish_max_badge.setStyleSheet('color: #34d399; font-weight: bold; ')
        row_farm_finish = QHBoxLayout()
        row_farm_finish.addWidget(self.chk_farm_finish)
        row_farm_finish.addWidget(self.lbl_farm_finish_max_badge)
        row_farm_finish.addStretch(1)
        form_wall.addRow(row_farm_finish)
        self.chk_farm_finish.toggled.connect(self._on_farm_finish_toggled)
        self.chk_farm_finish_close = QCheckBox('Đóng ứng dụng khi hoàn tất')
        self.chk_farm_finish_close.setChecked(self.settings.get('farm_finish_close_app', False))
        self.chk_farm_finish_close.toggled.connect(lambda enabled: self.settings.set('farm_finish_close_app', enabled))
        self.chk_farm_finish_close.setToolTip('Nếu bật: đóng toàn bộ tool khi đạt full kho và hết thợ. Nếu tắt: chỉ tạm dừng bot và giữ nguyên tool.')
        form_wall.addRow(self.chk_farm_finish_close)
        self.chk_farm_finish_wait_builders = QCheckBox('Hết thợ và đầy kho: chờ thợ rồi chạy tiếp')
        self.chk_farm_finish_wait_builders.setChecked(self.settings.get('farm_finish_wait_builders', True))
        self.chk_farm_finish_wait_builders.toggled.connect(lambda enabled: self.settings.set('farm_finish_wait_builders', enabled))
        self.chk_farm_finish_wait_builders.setToolTip('Khi full kho tài nguyên và không còn thợ rảnh: đóng game Clash of Clans, tạm dừng bot đúng thời gian công trình đang nâng gần nhất hoàn thành, sau đó tự mở lại game để kiểm tra và tiếp tục nâng cấp. Chỉ dừng bot hẳn khi không còn công trình nào phù hợp sau lọc.')
        form_wall.addRow(self.chk_farm_finish_wait_builders)
        self.spin_farm_finish_percent = QSpinBox()
        self.spin_farm_finish_percent.setRange(90, 100)
        self.spin_farm_finish_percent.setSuffix(' %')
        self.spin_farm_finish_percent.setValue(self.settings.get('farm_finish_target_percent', 100))
        self.spin_farm_finish_percent.valueChanged.connect(lambda value: self.settings.set('farm_finish_target_percent', value))
        form_wall.addRow('Mục tiêu kho thật:', self.spin_farm_finish_percent)
        self.combo_farm_finish_resources = QComboBox()
        self.combo_farm_finish_resources.addItems(['Vàng và dầu', 'Vàng', 'Dầu'])
        finish_modes = ['gold_elixir', 'gold', 'elixir']
        current_finish = self.settings.get('farm_finish_resources', 'gold_elixir')
        self.combo_farm_finish_resources.setCurrentIndex(finish_modes.index(current_finish) if current_finish in finish_modes else 0)
        self.combo_farm_finish_resources.currentIndexChanged.connect(lambda i: self.settings.set('farm_finish_resources', finish_modes[i]))
        form_wall.addRow('Kho cần đạt:', self.combo_farm_finish_resources)
        self.lbl_wall_status.setWordWrap(True)
        self.lbl_wall_status.setStyleSheet('color: #aeb8c6; ')
        form_wall.addRow('Trạng thái:', self.lbl_wall_status)
        self.btn_wall_recheck = QPushButton('Kiểm tra thợ lượt tới')
        self.btn_wall_recheck.clicked.connect(self._recheck_wall_builders)
        self.btn_verify_layout_now = QPushButton('🔍 Quét Max tường ngay')
        self.btn_verify_layout_now.setToolTip('Mở Layout Editor kiểm tra chính xác toàn bộ tường của tài khoản đang mở trên màn hình xem đã Max hay chưa.')
        self.btn_verify_layout_now.clicked.connect(self._manual_verify_layout_editor)
        wall_btn_row = QVBoxLayout()
        wall_btn_row.addWidget(self.btn_verify_layout_now)
        wall_btn_row.addWidget(self.btn_wall_recheck)
        form_wall.addRow('', wall_btn_row)
        reset_wall_row = QHBoxLayout()
        reset_wall_row.addStretch()
        reset_wall_row.addWidget(self._make_reset_button(self._reset_wall_defaults))
        form_wall.addRow(reset_wall_row)
        layout.addWidget(grp_wall, 1, 1, 2, 1)
        wall_options = (self.chk_wall_lowest, self.combo_wall_res, self.spin_wall_gold,
                        self.spin_wall_elixir, self.combo_wall_quantity_mode,
                        self.spin_wall_fixed_gap, self.chk_wall_hold,
                        self.btn_wall_recheck, self.btn_verify_layout_now)
        def update_wall_options():
            for control in wall_options:
                control.setEnabled(self.chk_walls.isChecked())
            self.spin_wall_fixed_gap.setEnabled(
                self.chk_walls.isChecked() and self.combo_wall_quantity_mode.currentIndex() == 1)
        self.chk_walls.toggled.connect(update_wall_options)
        self.combo_wall_quantity_mode.currentIndexChanged.connect(update_wall_options)
        update_wall_options()

        farm_finish_options = (self.chk_farm_finish_close, self.chk_farm_finish_wait_builders,
                               self.spin_farm_finish_percent, self.combo_farm_finish_resources)
        def update_farm_finish_options():
            for control in farm_finish_options:
                control.setEnabled(self.chk_farm_finish.isChecked())
        self._update_farm_finish_options = update_farm_finish_options
        self.chk_farm_finish.toggled.connect(update_farm_finish_options)
        update_farm_finish_options()

        # Group 4: Custom Building Upgrades
        grp_bldg = QGroupBox("Nâng cấp công trình tùy chọn · phân công thợ xây")
        form_bldg = QFormLayout(grp_bldg)

        self.chk_auto_buildings = QCheckBox("Bật tự động nâng cấp công trình theo tùy chọn")
        self.chk_auto_buildings.setChecked(self.settings.get("auto_upgrade_buildings", False))
        self.chk_auto_buildings.setToolTip(
            "Khi số thợ thường rảnh nhiều hơn 'Số thợ rảnh cần duy trì', bot sẽ tạm dừng nâng tường (cả vàng & dầu) "
            "để tích tài nguyên và phân công đủ số thợ vào các công trình đã chọn."
        )
        self.chk_auto_buildings.toggled.connect(lambda checked: self.settings.set("auto_upgrade_buildings", checked))
        form_bldg.addRow(self.chk_auto_buildings)

        self.chk_buy_new = QCheckBox("Tự mua công trình mới (không dùng đá quý)")
        self.chk_buy_new.setChecked(self.settings.get("auto_buy_new_buildings", False))
        self.chk_buy_new.toggled.connect(lambda checked: self.settings.set("auto_buy_new_buildings", checked))
        self.chk_buy_new.setToolTip("Ưu tiên công trình mới; chỉ dùng thợ dự phòng nếu thời gian xây dưới 1 phút.")
        form_bldg.addRow(self.chk_buy_new)

        self.chk_lock_townhall = QCheckBox("🛡️ Chốt an toàn: Tuyệt đối cấm nâng cấp Nhà chính (Town Hall)")
        self.chk_lock_townhall.setChecked(self.settings.get("lock_townhall_upgrade", True))
        self.chk_lock_townhall.setToolTip(
            "Chốt chặn bảo vệ: Tuyệt đối không bao giờ bấm nâng Nhà chính dưới mọi tình huống để tránh bị ép lên cấp ngoài ý muốn."
        )
        self.chk_lock_townhall.toggled.connect(lambda checked: self.settings.set("lock_townhall_upgrade", checked))
        form_bldg.addRow(self.chk_lock_townhall)

        self.combo_bldg_priority = QComboBox()
        self.bldg_priority_choices = [
            ("time_min", "Ít thời gian nhất trước"),
            ("cost_min", "Chi phí nhỏ nhất (rẻ nhất) trước"),
            ("cost_max", "Chi phí đắt nhất trước"),
            ("time_max", "Nhiều thời gian nhất trước"),
            ("suggested", "Theo gợi ý mặc định (Suggested)"),
            ("specific", "🎯 Cụ thể: Chỉ nâng 1 công trình chỉ định"),
        ]
        for key, label in self.bldg_priority_choices:
            self.combo_bldg_priority.addItem(label, key)
        cur_prio = self.settings.get("building_upgrade_priority", "time_min")
        self.combo_bldg_priority.setCurrentIndex(max(0, self.combo_bldg_priority.findData(cur_prio)))
        self.combo_bldg_priority.currentIndexChanged.connect(
            lambda _: [
                self.settings.set("building_upgrade_priority", self.combo_bldg_priority.currentData()),
                self._update_specific_target_visibility(),
            ]
        )
        form_bldg.addRow("Thứ tự ưu tiên nâng:", self.combo_bldg_priority)

        self.combo_specific_target = QComboBox()
        self.combo_specific_target.addItem("-- Chọn công trình mục tiêu --", "")
        cat_icons = {"traps": "💣", "resource": "🌾", "defense": "🛡️", "army": "⚔️", "heroes": "👑"}
        for cat_k, _ in BUILDING_CATEGORY_LABELS:
            c_icon = cat_icons.get(cat_k, "🔹")
            for b_k, b_en, b_vi in BUILDING_CATALOG.get(cat_k, ()):
                if b_k == "townhall":
                    continue
                self.combo_specific_target.addItem(f"{c_icon} {b_vi} ({b_en})", b_k)
        cur_target = self.settings.get("building_upgrade_specific_target", "")
        self.combo_specific_target.setCurrentIndex(max(0, self.combo_specific_target.findData(cur_target)))
        self.combo_specific_target.currentIndexChanged.connect(
            lambda _: self.settings.set("building_upgrade_specific_target", self.combo_specific_target.currentData())
        )
        self.lbl_specific_target_title = QLabel("Công trình cụ thể:")
        form_bldg.addRow(self.lbl_specific_target_title, self.combo_specific_target)
        self._update_specific_target_visibility()

        self.combo_bldg_wait_mode = QComboBox()
        self.bldg_wait_choices = [
            ("target_cost", "Nâng ngay khi farm đủ tiền công trình mục tiêu"),
            ("full_storage", "Đợi farm đầy kho mới triển khai nâng cấp"),
        ]
        for key, label in self.bldg_wait_choices:
            self.combo_bldg_wait_mode.addItem(label, key)
        cur_wait = self.settings.get("building_upgrade_wait_mode", "target_cost")
        self.combo_bldg_wait_mode.setCurrentIndex(max(0, self.combo_bldg_wait_mode.findData(cur_wait)))
        self.combo_bldg_wait_mode.currentIndexChanged.connect(
            lambda _: self.settings.set("building_upgrade_wait_mode", self.combo_bldg_wait_mode.currentData())
        )
        form_bldg.addRow("Khi thiếu tài nguyên:", self.combo_bldg_wait_mode)

        self.chk_bldg_menu_filter = QCheckBox("Dùng bộ lọc nhanh của Builder Menu (Resource / Cost)")
        self.chk_bldg_menu_filter.setChecked(self.settings.get("building_upgrade_use_menu_filter", True))
        self.chk_bldg_menu_filter.toggled.connect(
            lambda checked: self.settings.set("building_upgrade_use_menu_filter", checked)
        )
        form_bldg.addRow(self.chk_bldg_menu_filter)

        res_row = QHBoxLayout()
        self.chk_bldg_resources = {}
        saved_res = dict(DEFAULT_CONFIG["building_upgrade_resources"])
        if isinstance(self.settings.get("building_upgrade_resources"), dict):
            saved_res.update(self.settings.get("building_upgrade_resources"))
        for r_key, r_label in (("gold", "Vàng"), ("elixir", "Dầu hồng"), ("dark_elixir", "Dầu đen")):
            chk = QCheckBox(r_label)
            chk.setChecked(bool(saved_res.get(r_key, True)))
            chk.toggled.connect(self._save_building_resource_toggles)
            self.chk_bldg_resources[r_key] = chk
            res_row.addWidget(chk)
        res_row.addStretch()
        form_bldg.addRow("Loại tài nguyên:", res_row)

        cat_grid = QGridLayout()
        self.chk_bldg_categories = {}
        saved_cats = dict(DEFAULT_CONFIG["building_upgrade_categories"])
        if isinstance(self.settings.get("building_upgrade_categories"), dict):
            saved_cats.update(self.settings.get("building_upgrade_categories"))
        for idx, (c_key, c_label) in enumerate(BUILDING_CATEGORY_LABELS):
            chk = QCheckBox(c_label)
            chk.setChecked(bool(saved_cats.get(c_key, True)))
            chk.toggled.connect(self._save_building_category_toggles)
            self.chk_bldg_categories[c_key] = chk
            cat_grid.addWidget(chk, idx // 2, idx % 2)
        form_bldg.addRow("Nhóm công trình:", cat_grid)
        self._apply_resource_constraints_to_categories()

        self.combo_bldg_preset = QComboBox()
        self.combo_bldg_preset.addItem("-- Chọn mẫu cấu hình nhanh --", None)
        self.combo_bldg_preset.addItem("⚡ Tất cả công trình (Thợ luôn bận)", "all")
        self.combo_bldg_preset.addItem("🛡️ Chỉ Phòng thủ & Bẫy", "defense_traps")
        self.combo_bldg_preset.addItem("🌾 Chỉ Kinh tế (Mỏ & Kho)", "resource")
        self.combo_bldg_preset.addItem("⚔️ Chỉ Binh lính & Tướng", "army_heroes")
        self.combo_bldg_preset.addItem("🟣 Chỉ công trình Dầu hồng", "elixir_focus")
        self.combo_bldg_preset.addItem("🟡 Chỉ công trình Vàng", "gold_focus")
        self.combo_bldg_preset.currentIndexChanged.connect(self._on_building_preset_selected)
        form_bldg.addRow("Mẫu chọn nhanh:", self.combo_bldg_preset)

        self.btn_bldg_details = QPushButton("🛠️ Mở bảng chọn chi tiết công trình...")
        self.btn_bldg_details.clicked.connect(self._open_building_selection_dialog)
        form_bldg.addRow("Danh sách chi tiết:", self.btn_bldg_details)

        self.lbl_bldg_count = QLabel("")
        self.lbl_bldg_count.setWordWrap(True)
        self.lbl_bldg_count.setStyleSheet("color: #94A3B8; ")
        self._update_building_selection_summary()
        form_bldg.addRow("Đang chọn:", self.lbl_bldg_count)

        self.lbl_bldg_status = QLabel("Chưa kiểm tra nâng công trình.")
        self.lbl_bldg_status.setWordWrap(True)
        self.lbl_bldg_status.setStyleSheet("color: #aeb8c6; ")
        form_bldg.addRow("Trạng thái thợ:", self.lbl_bldg_status)

        reset_bldg_row = QHBoxLayout()
        reset_bldg_row.addStretch()
        reset_bldg_row.addWidget(self._make_reset_button(self._reset_building_upgrade_defaults))
        form_bldg.addRow(reset_bldg_row)
        layout.addWidget(grp_bldg, 3, 1)
        building_options = (self.combo_bldg_priority, self.combo_specific_target,
                            self.combo_bldg_wait_mode, self.chk_bldg_menu_filter,
                            self.combo_bldg_preset, self.btn_bldg_details,
                            *self.chk_bldg_resources.values(), *self.chk_bldg_categories.values())
        def update_building_options():
            for control in building_options:
                control.setEnabled(self.chk_auto_buildings.isChecked())
        self._update_building_options = update_building_options
        self.chk_auto_buildings.toggled.connect(update_building_options)
        update_building_options()

        # Army research has its own settings, independent of builder upgrades.
        grp_army = QGroupBox("Nâng cấp quân đội · phòng nghiên cứu")
        form_army = QFormLayout(grp_army)
        self.chk_upgrade_army = QCheckBox("Tự nâng quân, phép và máy công thành")
        self.chk_upgrade_army.setChecked(self.settings.get("auto_upgrade_army", False))
        self.chk_upgrade_army.toggled.connect(lambda checked: self.settings.set("auto_upgrade_army", checked))
        form_army.addRow(self.chk_upgrade_army)
        self.army_resource_checks = {}
        army_resource_row = QHBoxLayout()
        for key, label in (("elixir", "Dầu hồng"), ("dark_elixir", "Dầu đen")):
            check = QCheckBox(label)
            check.setChecked(self.settings.get("army_upgrade_resources", {}).get(key, True))
            self.army_resource_checks[key] = check
            army_resource_row.addWidget(check)
            check.toggled.connect(lambda _checked: self.settings.set("army_upgrade_resources", {k: c.isChecked() for k, c in self.army_resource_checks.items()}))
        form_army.addRow("Tài nguyên nâng quân:", army_resource_row)
        self.army_type_checks = {}
        army_type_row = QHBoxLayout()
        for key, label in (("troops", "Quân"), ("spells", "Phép"), ("siege", "Máy công thành")):
            check = QCheckBox(label)
            check.setChecked(self.settings.get("army_upgrade_types", {}).get(key, True))
            self.army_type_checks[key] = check
            army_type_row.addWidget(check)
            check.toggled.connect(lambda _checked: self.settings.set("army_upgrade_types", {k: c.isChecked() for k, c in self.army_type_checks.items()}))
        form_army.addRow("Loại nâng quân:", army_type_row)
        self.army_priority = QComboBox()
        for key, label in (("suggested", "Theo gợi ý"), ("cost_min", "Giá thấp trước"), ("cost_max", "Giá cao trước")):
            self.army_priority.addItem(label, key)
        self.army_priority.setCurrentIndex(max(0, self.army_priority.findData(self.settings.get("army_upgrade_priority", "suggested"))))
        self.army_priority.currentIndexChanged.connect(lambda _: self.settings.set("army_upgrade_priority", self.army_priority.currentData()))
        form_army.addRow("Ưu tiên nâng quân:", self.army_priority)
        army_options = (*self.army_resource_checks.values(), *self.army_type_checks.values(), self.army_priority)
        def update_army_options():
            for control in army_options:
                control.setEnabled(self.chk_upgrade_army.isChecked())
        self.chk_upgrade_army.toggled.connect(update_army_options)
        update_army_options()

        grp_army.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        left_upgrade_column = QVBoxLayout()
        left_upgrade_column.setContentsMargins(0, 0, 0, 0)
        left_upgrade_column.addWidget(grp_army)
        left_upgrade_column.addWidget(grp_treasury)
        main_village_groups = (grp_auto, grp_treasury, grp_wall, grp_bldg, grp_army)
        self._main_village_groups = main_village_groups
        def update_main_village_options(enabled):
            for group in main_village_groups:
                group.setEnabled(enabled)
        self.chk_main_village.toggled.connect(update_main_village_options)
        update_main_village_options(self.chk_main_village.isChecked())
        left_upgrade_column.addStretch()
        layout.addLayout(left_upgrade_column, 3, 0)

        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(0, 0, 0, 0)
        self.village_subtabs = self._make_subtabs((
            ('Chăm sóc làng', (self.chk_main_village, grp_auto, grp_treasury)),
            ('Nâng tường', (grp_wall,)),
            ('Nâng công trình', (grp_bldg,)),
            ('Nâng quân', (grp_army,)),
        ))
        root.addWidget(self.village_subtabs)
        return page

    def _create_combat_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        # Strategy
        grp_strat = QGroupBox("Cách thả quân")
        form_strat = QFormLayout(grp_strat)

        self.combo_strat = QComboBox()
        self.combo_strat.addItems([
            "Rải quanh ranh căn cứ",
            "Chia 4 hướng",
            "Tập trung từng cánh",
            "Hỗn hợp: quân chính 4 hướng, quân khác rải quanh",
            "Săn tài nguyên: rồng theo mỏ/kho, rồi đánh bổ sung",
            "Siêu nhanh: thả liên tục, ưu tiên đạt 50% sớm nhất",
            "Ngẫu nhiên: đổi chiến thuật mỗi trận",
            "Rải quân cố định"
        ])
        strat_keys = ["perimeter", "four_corners", "smart_wave", "hybrid", "resource_dragons", "ultra_fast", "random_mix", FixedAttackPlan.KEY]
        cur_strat = self.settings.get("attack_strategy", "perimeter")
        if cur_strat in strat_keys:
            self.combo_strat.setCurrentIndex(strat_keys.index(cur_strat))
        self.combo_strat.currentIndexChanged.connect(
            lambda i: self.settings.set("attack_strategy", strat_keys[i])
        )

        self.spin_drop_delay = QSpinBox()
        self.spin_drop_delay.setRange(50, 500)
        self.spin_drop_delay.setSingleStep(20)
        self.spin_drop_delay.setValue(self.settings.get("troop_drop_delay_ms", 80))
        self.spin_drop_delay.valueChanged.connect(lambda v: self.settings.set("troop_drop_delay_ms", v))

        form_strat.addRow("Kiểu thả quân:", self.combo_strat)
        calibrate = QPushButton('Hiệu chuẩn rải quân cố định')
        calibrate.clicked.connect(self._calibrate_fixed_attack)
        calibrate.setVisible(cur_strat == FixedAttackPlan.KEY)
        self.combo_strat.currentIndexChanged.connect(
            lambda index: calibrate.setVisible(strat_keys[index] == FixedAttackPlan.KEY)
        )
        form_strat.addRow(calibrate)
        random_note=QLabel('Ngẫu nhiên chọn một trong sáu kiểu cho mỗi trận và tránh lặp liên tiếp. Mọi kiểu đều áp dụng điều kiện kết thúc đã tích.')
        random_note.setWordWrap(True);form_strat.addRow(random_note)
        form_strat.addRow("Độ trễ giữa mỗi lần thả (ms):", self.spin_drop_delay)
        self.combo_resource_fallback=QComboBox()
        self.combo_resource_fallback.addItems(['Rải quanh ranh','Chia 4 hướng','Tập trung một cánh','Hỗn hợp'])
        fallback_keys=['perimeter','four_corners','smart_wave','hybrid']
        fallback=self.settings.get('resource_attack_fallback','perimeter')
        self.combo_resource_fallback.setCurrentIndex(fallback_keys.index(fallback) if fallback in fallback_keys else 0)
        self.combo_resource_fallback.currentIndexChanged.connect(lambda i:self.settings.set('resource_attack_fallback',fallback_keys[i]))
        form_strat.addRow('Đánh bổ sung sau khi thả rồng theo tài nguyên:',self.combo_resource_fallback)
        self.combo_resource_fallback.setEnabled(cur_strat=='resource_dragons')
        self.combo_strat.currentIndexChanged.connect(lambda i:self.combo_resource_fallback.setEnabled(strat_keys[i]=='resource_dragons'))
        self.spin_attack_controls = {}
        for key, label, minimum, maximum, default in [
            ('attack_spread_points', 'Số điểm rải quân:', 4, 32, 16),
            ('spell_first_delay_sec', 'Trễ tối thiểu của phép tự dò (giây):', 0, 30, 1),
            ('spell_wave_gap_sec', 'Khoảng cách hai đợt phép (giây):', 0, 30, 2),
        ]:
            control = QSpinBox()
            control.setRange(minimum, maximum)
            control.setValue(round(self.settings.get(key, default)))
            control.valueChanged.connect(lambda value, name=key: self.settings.set(name, value))
            form_strat.addRow(label, control)
            self.spin_attack_controls[key] = control
        reset_strat_row = QHBoxLayout()
        reset_strat_row.addStretch()
        reset_strat_row.addWidget(self._make_reset_button(self._reset_attack_strat_defaults))
        form_strat.addRow(reset_strat_row)
        layout.addWidget(grp_strat)
        grp_ultra=QGroupBox('Siêu nhanh · thả liên tục')
        form_ultra=QFormLayout(grp_ultra)
        self.chk_ultra_fixed=QCheckBox('Ưu tiên hướng cố định trong vùng thả đã quét')
        self.chk_ultra_fixed.setChecked(self.settings.get('ultra_fixed_positions',False))
        self.chk_ultra_fixed.toggled.connect(lambda checked:self.settings.set('ultra_fixed_positions',checked))
        form_ultra.addRow(self.chk_ultra_fixed)
        self.spin_ultra_controls = {}
        for key,label,minimum,maximum,default in (
            ('ultra_drop_gap_ms','Nhịp thả nhanh (ms):',5,100,20),
            ('ultra_card_select_ms','Chờ chọn thẻ quân/tướng (ms):',40,200,80),
            ('ultra_spell_inward_pct','Thiên về hướng tâm khi dẫn phép (%):',5,50,20),
            ('ultra_spell_max_lead_px','Dẫn phép tối đa khỏi cụm lính (px):',15,60,40)):
            spin=QSpinBox();spin.setRange(minimum,maximum);spin.setValue(self.settings.get(key,default))
            spin.valueChanged.connect(lambda value,name=key:self.settings.set(name,value))
            form_ultra.addRow(label,spin)
            self.spin_ultra_controls[key] = spin
        ultra_note=QLabel('Chuẩn bị: zoom, đọc đội hình, quét ranh và tính sẵn điểm phép gần đường tiến quân. Xen phép ngay sau nhóm lính mở đầu và giữa các đợt lính, trước khi thả hết tướng/quân; không chờ thanh máu để thả phép mở đầu. Phép còn lại bám cụm lính quan sát được, được đặt trong ranh đỏ. Sau chuỗi chính kiểm tra quân/phép/tướng còn sót, thử lại tối đa hai lượt. Kỹ năng chỉ dùng khi xác minh tướng xuất trận; kết thúc theo các điều kiện đã tích bên dưới.')
        ultra_note.setWordWrap(True);form_ultra.addRow(ultra_note)
        reset_ultra_row = QHBoxLayout()
        reset_ultra_row.addStretch()
        reset_ultra_row.addWidget(self._make_reset_button(self._reset_ultra_strat_defaults))
        form_ultra.addRow(reset_ultra_row)
        grp_ultra.setEnabled(cur_strat=='ultra_fast')
        self.combo_strat.currentIndexChanged.connect(lambda i:grp_ultra.setEnabled(strat_keys[i]=='ultra_fast'))
        layout.addWidget(grp_ultra)

        # Heroes
        grp_heroes = QGroupBox("Tướng sử dụng")
        grid_heroes = QGridLayout(grp_heroes)

        self.chk_king = QCheckBox("Barbarian King")
        self.chk_king.setChecked(self.settings.get("use_king", True))
        self.chk_king.stateChanged.connect(lambda s: self.settings.set("use_king", s == Qt.Checked))

        self.chk_queen = QCheckBox("Archer Queen")
        self.chk_queen.setChecked(self.settings.get("use_queen", True))
        self.chk_queen.stateChanged.connect(lambda s: self.settings.set("use_queen", s == Qt.Checked))

        self.chk_warden = QCheckBox("Grand Warden")
        self.chk_warden.setChecked(self.settings.get("use_warden", True))
        self.chk_warden.stateChanged.connect(lambda s: self.settings.set("use_warden", s == Qt.Checked))

        self.chk_champ = QCheckBox("Royal Champion")
        self.chk_champ.setChecked(self.settings.get("use_champion", True))
        self.chk_champ.stateChanged.connect(lambda s: self.settings.set("use_champion", s == Qt.Checked))

        self.chk_abilities = QCheckBox("Dùng kỹ năng tướng")
        self.chk_abilities.setChecked(self.settings.get("trigger_hero_abilities", True))
        self.chk_abilities.stateChanged.connect(lambda s: self.settings.set("trigger_hero_abilities", s == Qt.Checked))

        grid_heroes.addWidget(self.chk_king, 0, 0)
        grid_heroes.addWidget(self.chk_queen, 0, 1)
        grid_heroes.addWidget(self.chk_warden, 1, 0)
        grid_heroes.addWidget(self.chk_champ, 1, 1)
        self.chk_hero_extras = {}
        for column, (name, label) in enumerate([('prince', 'Minion Prince'), ('luke', 'Dragon Duke')]):
            checkbox = QCheckBox(label)
            checkbox.setChecked(self.settings.get('use_' + name, True))
            checkbox.stateChanged.connect(lambda state, key='use_' + name: self.settings.set(key, state == Qt.Checked))
            grid_heroes.addWidget(checkbox, 2, column)
            self.chk_hero_extras[name] = checkbox
        self.spin_ability_delay = QSpinBox()
        self.spin_ability_delay.setRange(0, 120)
        self.spin_ability_delay.setValue(self.settings.get('hero_ability_delay_sec', 5))
        self.spin_ability_delay.valueChanged.connect(lambda v: self.settings.set('hero_ability_delay_sec', v))
        grid_heroes.addWidget(self.chk_abilities, 3, 0, 1, 2)
        grid_heroes.addWidget(QLabel('Dùng kỹ năng sau khi thả tướng (giây):'), 4, 0)
        grid_heroes.addWidget(self.spin_ability_delay, 4, 1)
        self.spin_ability_delay.setEnabled(self.chk_abilities.isChecked())
        self.chk_abilities.toggled.connect(self.spin_ability_delay.setEnabled)
        reset_heroes_row = QHBoxLayout()
        reset_heroes_row.addStretch()
        reset_heroes_row.addWidget(self._make_reset_button(self._reset_heroes_defaults))
        grid_heroes.addLayout(reset_heroes_row, 5, 0, 1, 2)
        layout.addWidget(grp_heroes)

        # Siege Machines
        grp_siege = QGroupBox("Cỗ máy công thành (Siege Machine)")
        form_siege = QFormLayout(grp_siege)

        self.chk_use_siege = QCheckBox("Sử dụng Cỗ máy công thành khi tấn công")
        self.chk_use_siege.setChecked(self.settings.get("use_siege", True))
        self.chk_use_siege.stateChanged.connect(lambda s: self.settings.set("use_siege", s == Qt.Checked))
        form_siege.addRow(self.chk_use_siege)

        self.combo_siege_mode = QComboBox()
        self.combo_siege_mode.addItem("Thả luôn (kể cả không có lính Clan)", "always")
        self.combo_siege_mode.addItem("Chỉ thả khi có lính Clan", "require_cc")
        current_siege_mode = self.settings.get("siege_deploy_mode", "always")
        mode_idx = 1 if current_siege_mode == "require_cc" else 0
        self.combo_siege_mode.setCurrentIndex(mode_idx)
        self.combo_siege_mode.currentIndexChanged.connect(
            lambda i: self.settings.set("siege_deploy_mode", self.combo_siege_mode.itemData(i))
        )
        form_siege.addRow(QLabel("Chế độ thả cỗ máy:"), self.combo_siege_mode)

        reset_siege_row = QHBoxLayout()
        reset_siege_row.addStretch()
        reset_siege_row.addWidget(self._make_reset_button(self._reset_siege_defaults))
        form_siege.addRow(reset_siege_row)
        layout.addWidget(grp_siege)

        # Retreat conditions
        grp_surr = QGroupBox("Kết thúc trận")
        form_surr = QFormLayout(grp_surr)

        self.spin_stars = QSpinBox()
        self.spin_stars.setRange(1, 3)
        self.spin_stars.setValue(self.settings.get("star_target", 1))
        self.spin_stars.valueChanged.connect(lambda v: self.settings.set("star_target", v))

        self.spin_timeout = QSpinBox()
        self.spin_timeout.setRange(30, 180)
        self.spin_timeout.setValue(self.settings.get("battle_timeout_sec", 120))
        self.spin_timeout.valueChanged.connect(lambda v: self.settings.set("battle_timeout_sec", v))

        self.chk_auto_surr = QCheckBox("Bật tự kết thúc theo các điều kiện đã tích")
        self.chk_auto_surr.setChecked(self.settings.get("auto_surrender", True))
        self.chk_auto_surr.stateChanged.connect(lambda s: self.settings.set("auto_surrender", s == Qt.Checked))

        form_surr.addRow(self.chk_auto_surr)
        self.chk_end_stars=QCheckBox('Đạt số sao (1, 2 hoặc 3):')
        self.chk_end_time=QCheckBox('Đạt thời gian tối đa (giây):')
        self.chk_end_loot=QCheckBox('Farm đủ tài nguyên địch (%):')
        self.chk_end_destruction=QCheckBox('Đạt tỷ lệ phá hủy (%):')
        self.spin_loot_percent=QSpinBox();self.spin_loot_percent.setRange(1,100)
        self.spin_loot_percent.setValue(self.settings.get('loot_target_percent',70))
        self.spin_loot_percent.valueChanged.connect(lambda v:self.settings.set('loot_target_percent',v))
        self.spin_destruction=QSpinBox();self.spin_destruction.setRange(1,100)
        self.spin_destruction.setValue(self.settings.get('destruction_target_percent',50))
        self.spin_destruction.valueChanged.connect(lambda v:self.settings.set('destruction_target_percent',v))
        for key,check,control,default in (
            ('end_on_stars',self.chk_end_stars,self.spin_stars,True),
            ('end_on_time',self.chk_end_time,self.spin_timeout,True),
            ('end_on_loot',self.chk_end_loot,self.spin_loot_percent,False),
            ('end_on_destruction',self.chk_end_destruction,self.spin_destruction,False)):
            check.setChecked(self.settings.get(key,default))
            check.stateChanged.connect(lambda state,name=key:self.settings.set(name,state==Qt.Checked))
            form_surr.addRow(check,control)
        self.combo_star_source=QComboBox()
        star_sources=['all','town_hall','destruction']
        self.combo_star_source.addItems(['Tính mọi sao','Phải có sao nhà chính','Không tính sao nhà chính'])
        source=self.settings.get('end_star_source','all')
        self.combo_star_source.setCurrentIndex(star_sources.index(source) if source in star_sources else 0)
        def change_star_source(index):
            self.settings.set('end_star_source',star_sources[index])
            self.spin_stars.setMaximum(2 if index==2 else 3)
        self.combo_star_source.currentIndexChanged.connect(change_star_source)
        self.spin_stars.setMaximum(2 if source=='destruction' else 3)
        form_surr.addRow('Sao dùng xét điều kiện dừng:',self.combo_star_source)
        star_note=QLabel('Nhà chính: 1 sao; 50%: 1 sao; 100%: 1 sao. Muốn nhà chính + 50%: chọn 2 sao, Phải có sao nhà chính. Không tính nhà chính: 1 sao tương ứng 50%, 2 sao tương ứng 100%. Siêu tốc cũng áp dụng các điều kiện đã tích.')
        star_note.setWordWrap(True);form_surr.addRow(star_note)
        self.combo_loot_resource=QComboBox()
        loot_keys=['gold_elixir','gold','elixir','dark_elixir','all']
        self.combo_loot_resource.addItems(['Tổng vàng + tiên dược','Chỉ vàng','Chỉ tiên dược','Chỉ hắc tiên dược','Tổng cả ba tài nguyên'])
        selected=self.settings.get('loot_target_resource','gold_elixir')
        self.combo_loot_resource.setCurrentIndex(loot_keys.index(selected) if selected in loot_keys else 0)
        self.combo_loot_resource.currentIndexChanged.connect(lambda i:self.settings.set('loot_target_resource',loot_keys[i]))
        form_surr.addRow('Tài nguyên dùng tính tỷ lệ farm:',self.combo_loot_resource)
        self.combo_end_mode=QComboBox()
        self.combo_end_mode.addItems(['Đạt bất kỳ mục tiêu đã tích','Đạt tất cả mục tiêu đã tích'])
        self.combo_end_mode.setCurrentIndex(1 if self.settings.get('end_condition_mode','any')=='all' else 0)
        self.combo_end_mode.currentIndexChanged.connect(lambda i:self.settings.set('end_condition_mode','all' if i else 'any'))
        form_surr.addRow('Kết hợp điều kiện:',self.combo_end_mode)
        end_conditions = (self.chk_end_stars, self.chk_end_time,
                          self.chk_end_loot, self.chk_end_destruction)
        def update_end_conditions():
            enabled = self.chk_auto_surr.isChecked()
            for check in end_conditions:
                check.setEnabled(enabled)
            for check, control in ((self.chk_end_stars, self.spin_stars),
                                   (self.chk_end_time, self.spin_timeout),
                                   (self.chk_end_loot, self.spin_loot_percent),
                                   (self.chk_end_destruction, self.spin_destruction),
                                   (self.chk_end_stars, self.combo_star_source),
                                   (self.chk_end_loot, self.combo_loot_resource)):
                control.setEnabled(enabled and check.isChecked())
            self.combo_end_mode.setEnabled(enabled)
        self._update_end_conditions = update_end_conditions
        self.chk_auto_surr.toggled.connect(update_end_conditions)
        for check in end_conditions:
            check.toggled.connect(update_end_conditions)
        update_end_conditions()
        note=QLabel('Thời gian đã tích luôn là giới hạn trên. Tỷ lệ farm tính trên tài nguyên có thể cướp lúc bắt đầu trận; thay đổi áp dụng từ trận tiếp theo.')
        note.setWordWrap(True);form_surr.addRow(note)
        reset_surr_row = QHBoxLayout()
        reset_surr_row.addStretch()
        reset_surr_row.addWidget(self._make_reset_button(self._reset_surrender_defaults))
        form_surr.addRow(reset_surr_row)
        layout.insertWidget(1, grp_surr)

        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(0, 0, 0, 0)
        self.combat_subtabs = self._make_subtabs((
            ('Đối thủ', (self.grp_loot,)),
            ('Chiến thuật', (grp_strat, grp_ultra)),
            ('Lực lượng', (grp_heroes, grp_siege)),
            ('Kết thúc trận', (grp_surr,)),
        ))
        root.addWidget(self.combat_subtabs)
        return page

    def _create_builder_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        grp_builder = QGroupBox("Tự động hóa làng đêm")
        form_builder = QFormLayout(grp_builder)
        form_general = form_builder

        enabled = QCheckBox("Bật tự động farm làng đêm")
        self.chk_builder_village = enabled
        enabled.setChecked(self.settings.get("builder_base_enabled", False))
        form_builder.addRow(enabled)

        tasks = {}
        self.builder_tasks = tasks
        for key, label in BuilderBaseManager.OPTIONS:
            checkbox = QCheckBox(label)
            checkbox.setChecked(self.settings.get(key, False))
            checkbox.toggled.connect(lambda checked, option=key: self.settings.set(option, checked))
            form_builder.addRow(checkbox)
            tasks[key] = checkbox
        self._builder_task_controls = tuple(tasks.values())

        grp_builder_wall = QGroupBox("Nâng tường làng đêm")
        form_builder = QFormLayout(grp_builder_wall)
        self.builder_wall_thresholds = {}
        self.chk_builder_wall_lowest = QCheckBox("Ưu tiên nâng tường cấp thấp nhất")
        self.chk_builder_wall_lowest.setChecked(self.settings.get("builder_base_wall_lowest_first", False))
        self.chk_builder_wall_lowest.toggled.connect(lambda checked: self.settings.set("builder_base_wall_lowest_first", checked))
        self.chk_builder_wall_lowest.setEnabled(tasks["builder_base_upgrade_walls"].isChecked())
        tasks["builder_base_upgrade_walls"].toggled.connect(self.chk_builder_wall_lowest.setEnabled)
        form_builder.addRow(self.chk_builder_wall_lowest)
        self.combo_builder_wall_trigger = QComboBox()
        for key, label in (('either', 'Vàng hoặc dầu đạt ngưỡng'),
                           ('both', 'Cả vàng và dầu đều đạt ngưỡng'),
                           ('gold', 'Chỉ vàng đạt ngưỡng'),
                           ('elixir', 'Chỉ dầu đạt ngưỡng')):
            self.combo_builder_wall_trigger.addItem(label, key)
        self.combo_builder_wall_trigger.setCurrentIndex(max(0, self.combo_builder_wall_trigger.findData(
            self.settings.get('builder_base_wall_trigger', 'either'))))
        self.combo_builder_wall_trigger.setToolTip(
            'Điều kiện được kiểm tra trước khi mở bảng thợ. Chế độ chỉ vàng hoặc chỉ dầu cũng chỉ dùng loại đó. '
            'Sau khi mở lượt, tiếp tục dùng loại còn đạt ngưỡng. '
            'Tường dưới cấp 6 chỉ nâng bằng vàng. Nếu chỉ dầu đạt điều kiện, tool tìm tường cấp 6 trở lên.')
        self.combo_builder_wall_trigger.currentIndexChanged.connect(
            lambda _: self.settings.set('builder_base_wall_trigger', self.combo_builder_wall_trigger.currentData()))
        self.combo_builder_wall_trigger.setEnabled(tasks['builder_base_upgrade_walls'].isChecked())
        tasks['builder_base_upgrade_walls'].toggled.connect(self.combo_builder_wall_trigger.setEnabled)
        form_builder.addRow('Điều kiện mở lượt nâng tường:', self.combo_builder_wall_trigger)
        for resource, label in (("gold", "Ngưỡng Builder Gold"), ("elixir", "Ngưỡng Builder Elixir")):
            key = f"builder_base_wall_min_{resource}"
            threshold = QSpinBox()
            threshold.setRange(0, 30000000)
            threshold.setSingleStep(500000)
            threshold.setGroupSeparatorShown(True)
            threshold.setValue(self.settings.get(key, 0))
            threshold.setToolTip("Xét ngưỡng khi bắt đầu và sau mỗi lần nâng. Tài nguyên còn đạt ngưỡng có thể tiếp tục được sử dụng.")
            threshold.valueChanged.connect(lambda value, option=key: self.settings.set(option, value))
            wall_enabled = tasks["builder_base_upgrade_walls"]
            threshold.setEnabled(wall_enabled.isChecked())
            wall_enabled.toggled.connect(threshold.setEnabled)
            form_builder.addRow(label, threshold)
            self.builder_wall_thresholds[key] = threshold

        # Tùy chọn nâng công trình làng đêm
        grp_builder_buildings = QGroupBox("Nâng công trình làng đêm")
        form_builder = QFormLayout(grp_builder_buildings)
        self.spin_builder_reserve = QSpinBox()
        self.spin_builder_reserve.setRange(0, 4)
        self.spin_builder_reserve.setValue(self.settings.get("builder_base_reserve_builders", 0))
        self.spin_builder_reserve.setToolTip("Số thợ rảnh luôn giữ lại ở Làng đêm để dùng nâng tường. Khi tường Max, toàn bộ thợ sẽ được dùng cho công trình.")
        self.spin_builder_reserve.valueChanged.connect(lambda v: self.settings.set("builder_base_reserve_builders", v))
        form_builder.addRow("Duy trì số thợ rảnh làng đêm (để nâng tường):", self.spin_builder_reserve)

        self.combo_builder_priority = QComboBox()
        for key, label in (('suggested', 'Gợi ý của game (Suggested)'),
                           ('time_min', 'Thời gian ngắn nhất'),
                           ('cost_min', 'Chi phí thấp nhất'),
                           ('cost_max', 'Chi phí cao nhất')):
            self.combo_builder_priority.addItem(label, key)
        self.combo_builder_priority.setCurrentIndex(max(0, self.combo_builder_priority.findData(
            self.settings.get('builder_base_building_priority', 'suggested'))))
        self.combo_builder_priority.currentIndexChanged.connect(
            lambda _: self.settings.set('builder_base_building_priority', self.combo_builder_priority.currentData()))
        form_builder.addRow("Ưu tiên nâng công trình làng đêm:", self.combo_builder_priority)

        bld_enabled = tasks.get("builder_base_upgrade_buildings")
        if bld_enabled:
            self.spin_builder_reserve.setEnabled(bld_enabled.isChecked())
            self.combo_builder_priority.setEnabled(bld_enabled.isChecked())
            bld_enabled.toggled.connect(self.spin_builder_reserve.setEnabled)
            bld_enabled.toggled.connect(self.combo_builder_priority.setEnabled)

        def toggle_farm(checked):
            if checked:
                tasks["builder_base_collect_mine"].setChecked(True)
            self.settings.set("builder_base_enabled", checked)

        enabled.toggled.connect(toggle_farm)

        grp_builder_battle = QGroupBox("Đánh trận làng đêm")
        form_builder = QFormLayout(grp_builder_battle)
        combo = QComboBox()
        self.combo_builder_combo = combo
        for key, (label, _) in COMBOS.items():
            combo.addItem(label, key)
        combo.setCurrentIndex(max(0, combo.findData(self.settings.get("builder_base_combo", "current"))))
        combo.currentIndexChanged.connect(lambda _: self.settings.set("builder_base_combo", combo.currentData()))
        form_builder.addRow("Combo đánh làng đêm", combo)

        self.builder_reinforcement_combos = {}
        for index, key in enumerate(REINFORCEMENT_KEYS, 1):
            selector = QComboBox()
            for role, label in REINFORCEMENT_CHOICES.items():
                selector.addItem(label, role)
            selector.setCurrentIndex(max(0, selector.findData(self.settings.get(key, "current"))))
            selector.currentIndexChanged.connect(
                lambda _, option=key, box=selector: self.settings.set(option, box.currentData())
            )
            form_builder.addRow(f"Quân tiếp viện ô {index}", selector)
            self.builder_reinforcement_combos[key] = selector

        obstacle = QCheckBox("Vẫn bấm Attack khi bố trí phòng thủ bị chướng ngại")
        self.chk_builder_obstacle = obstacle
        obstacle.setChecked(self.settings.get("builder_base_obstacle_attack", False))
        obstacle.toggled.connect(lambda checked: self.settings.set("builder_base_obstacle_attack", checked))
        form_builder.addRow(obstacle)
        form_builder = form_general
        clear_trees = QCheckBox("Dọn cây và chướng ngại vật (chưa khả dụng)")
        clear_trees.setEnabled(False)
        form_builder.addRow(clear_trees)

        chk_builder_pause = QCheckBox("Tạm dừng tool khi max tường, hết thợ xây hoặc không còn gì để nâng")
        self.chk_builder_pause = chk_builder_pause
        chk_builder_pause.setChecked(self.settings.get("builder_base_pause_on_no_upgrades", False))
        chk_builder_pause.setToolTip(
            "Tự động tạm dừng tool khi phát hiện đã max toàn bộ tường làng đêm, "
            "dùng hết thợ xây hoặc sau nhiều lần liên tiếp không tìm thấy đối tượng nâng cấp."
        )
        chk_builder_pause.toggled.connect(lambda checked: self.settings.set("builder_base_pause_on_no_upgrades", checked))
        form_builder.addRow(chk_builder_pause)

        self.spin_builder_int = QSpinBox()
        self.spin_builder_int.setRange(1, 120)
        self.spin_builder_int.setValue(self.settings.get("builder_base_interval_mins", 30))
        self.spin_builder_int.valueChanged.connect(lambda v: self.settings.set("builder_base_interval_mins", v))
        self.spin_builder_int.setToolTip("Chỉ áp dụng cho thu mỏ vàng, dầu, gem và nâng tường. "
                                         "Xe dầu được mở khi thấy biểu tượng dầu tím.")
        form_builder.addRow("Kiểm tra mỏ và tường mỗi (phút)", self.spin_builder_int)

        note = QLabel("Khi bot chạy và farm làng đêm được bật, bot tự tìm trận theo combo đã chọn. Các loại lính trong combo cần được mở khóa. "
                      "Bot thu mỏ ở khu đang mở và đánh tiếp Stage 2 khi game chuyển sang.")
        note.setWordWrap(True)
        form_builder.addRow(note)
        reset_builder_row = QHBoxLayout()
        reset_builder_row.addStretch()
        reset_builder_row.addWidget(self._make_reset_button(self._reset_builder_base_defaults))
        form_builder.addRow(reset_builder_row)
        builder_groups = (grp_builder_wall, grp_builder_buildings, grp_builder_battle)
        self._builder_groups = builder_groups
        builder_general_options = (*self._builder_task_controls, self.chk_builder_pause,
                                   self.spin_builder_int, self.chk_builder_obstacle)
        def update_builder_options(checked):
            for control in (*builder_general_options, *builder_groups):
                control.setEnabled(checked)
        self._update_builder_options = update_builder_options
        enabled.toggled.connect(update_builder_options)
        update_builder_options(enabled.isChecked())

        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(0, 0, 0, 0)
        self.builder_subtabs = self._make_subtabs((
            ('Tác vụ', (grp_builder,)),
            ('Nâng tường', (grp_builder_wall,)),
            ('Công trình', (grp_builder_buildings,)),
            ('Trận đấu', (grp_builder_battle,)),
        ))
        root.addWidget(self.builder_subtabs)
        return page

    def _create_optimizer_tab(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)

        grp_opt = QGroupBox("Tối Ưu MuMu Player & Giả Lập")
        vbox_opt = QVBoxLayout(grp_opt)

        info_lbl = QLabel(
            'Kết nối bằng ADB. Bot hiện dùng bố cục 1600×900.\n'
            'Các nút bên dưới chỉnh độ phân giải và hiệu ứng Android.\n'
            'Bắt đầu sẽ mở game nếu MuMu đang ở màn hình chính. Tùy chọn bên dưới dùng khi game bị kẹt.'
        )
        info_lbl.setWordWrap(True)
        info_lbl.setStyleSheet("color: #94A3B8; line-height: 1.6;")
        vbox_opt.addWidget(info_lbl)

        btn_run_opt = QPushButton(" Chạy Tối Ưu Hóa MuMu Ngay")
        btn_run_opt.setObjectName("btn_optimize")
        btn_run_opt.clicked.connect(self._run_optimization)
        vbox_opt.addWidget(btn_run_opt)

        btn_fix_res = QPushButton(" Thiết Lập Độ Phân Giải Chuẩn 1600x900 (240 DPI)")
        btn_fix_res.clicked.connect(self._fix_resolution)
        vbox_opt.addWidget(btn_fix_res)

        layout.addWidget(grp_opt)

        grp_antiban = QGroupBox("Thao tác & khôi phục")
        form_anti = QFormLayout(grp_antiban)

        self.chk_restart = QCheckBox("Tự động mở lại Clash of Clans khi bị crash/văng game")
        self.chk_restart.setChecked(self.settings.get("auto_restart_game", True))
        self.chk_restart.stateChanged.connect(lambda s: self.settings.set("auto_restart_game", s == Qt.Checked))

        self.chk_reconnect = QCheckBox('Thử nối lại khi mất ADB')
        self.chk_reconnect.setChecked(self.settings.get('auto_reconnect', True))
        self.chk_reconnect.stateChanged.connect(lambda state: self.settings.set('auto_reconnect', state == Qt.Checked))
        form_anti.addRow(self.chk_reconnect)
        form_anti.addRow(self.chk_restart)
        self.chk_periodic_restart = QCheckBox('Khởi động lại game định kỳ khi đã về làng')
        self.chk_periodic_restart.setChecked(self.settings.get('periodic_restart_enabled', True))
        self.chk_periodic_restart.toggled.connect(lambda checked: self.settings.set('periodic_restart_enabled', checked))
        self.spin_restart_interval = QSpinBox()
        self.spin_restart_interval.setRange(15, 240)
        self.spin_restart_interval.setSingleStep(15)
        self.spin_restart_interval.setSuffix(' phút')
        self.spin_restart_interval.setValue(self.settings.get('periodic_restart_interval_mins', 60))
        self.spin_restart_interval.valueChanged.connect(lambda value: self.settings.set('periodic_restart_interval_mins', value))
        form_anti.addRow(self.chk_periodic_restart)
        form_anti.addRow('Chu kỳ restart game:', self.spin_restart_interval)
        self.spin_restart_interval.setEnabled(self.chk_periodic_restart.isChecked())
        self.chk_periodic_restart.toggled.connect(self.spin_restart_interval.setEnabled)
        reset_antiban_row = QHBoxLayout()
        reset_antiban_row.addStretch()
        reset_antiban_row.addWidget(self._make_reset_button(self._reset_antiban_defaults))
        form_anti.addRow(reset_antiban_row)

        layout.addWidget(grp_antiban)

        grp_config = QGroupBox("Quản lý cấu hình Tool (Config Management)")
        vbox_config = QVBoxLayout(grp_config)

        cfg_info = QLabel(
            "Lưu nhanh cấu hình hiện tại đang dùng vào config.json, hoặc xuất ra tệp dự phòng / nhập cấu hình từ máy tính để áp dụng ngay vào tool."
        )
        cfg_info.setWordWrap(True)
        cfg_info.setStyleSheet("color: #94A3B8; line-height: 1.5; margin-bottom: 6px;")
        vbox_config.addWidget(cfg_info)

        cfg_btn_row = QHBoxLayout()
        cfg_btn_row.setSpacing(10)

        btn_save_cfg = QPushButton("💾 Lưu cấu hình hiện tại")
        btn_save_cfg.setToolTip("Lưu toàn bộ thông số đang hiển thị trên giao diện vào config.json")
        btn_save_cfg.clicked.connect(self._save_current_config)

        btn_export_cfg = QPushButton("📤 Xuất cấu hình (Export)")
        btn_export_cfg.setToolTip("Xuất toàn bộ cấu hình ra tệp JSON để lưu trữ hoặc chuyển sang máy khác")
        btn_export_cfg.clicked.connect(self._export_config)

        btn_import_cfg = QPushButton("📥 Nhập cấu hình (Import)")
        btn_import_cfg.setToolTip("Nạp cấu hình từ tệp JSON vào tool và tự động cập nhật lại toàn bộ giao diện")
        btn_import_cfg.clicked.connect(self._import_config)

        cfg_btn_row.addWidget(btn_save_cfg)
        cfg_btn_row.addWidget(btn_export_cfg)
        cfg_btn_row.addWidget(btn_import_cfg)

        vbox_config.addLayout(cfg_btn_row)
        layout.addWidget(grp_config)

        grp_window = QGroupBox('Cửa sổ & khởi động')
        form_window = QFormLayout(grp_window)
        form_window.addRow(self.btn_to_tray)
        self.combo_startup_mode = QComboBox()
        for title, mode in (('Không tự khởi động', 'off'),
                            ('Khởi động cùng Windows', 'windows'),
                            ('Mở khi MuMu chạy', 'mumu')):
            self.combo_startup_mode.addItem(title, mode)
        self.combo_startup_mode.setCurrentIndex(max(0, self.combo_startup_mode.findData(
            self.settings.get('ui_startup_mode', 'off'))))
        self.combo_startup_mode.currentIndexChanged.connect(self._change_startup_mode)
        form_window.addRow('Tự khởi động:', self.combo_startup_mode)
        self.combo_close_behavior = QComboBox()
        self.combo_close_behavior.addItem('Thoát hẳn', 'quit')
        self.combo_close_behavior.addItem('Ẩn xuống khay hệ thống', 'tray')
        self.combo_close_behavior.setCurrentIndex(max(0, self.combo_close_behavior.findData(
            self.settings.get('ui_close_behavior', 'quit'))))
        self.combo_close_behavior.currentIndexChanged.connect(
            lambda _: self.settings.set('ui_close_behavior', self.combo_close_behavior.currentData()))
        form_window.addRow('Khi bấm X:', self.combo_close_behavior)
        startup_note = QLabel('Chế độ MuMu chạy nền từ lúc đăng nhập Windows và chỉ mở giao diện khi phát hiện MuMu. '
                              'Bot vẫn cần bấm Bắt đầu. Có thể chọn Thoát hẳn trong menu khay.')
        startup_note.setWordWrap(True)
        form_window.addRow(startup_note)
        layout.addWidget(grp_window)

        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(0, 0, 0, 0)
        self.device_subtabs = self._make_subtabs((
            ('Tối ưu MuMu', (grp_opt,)),
            ('Khôi phục', (grp_antiban,)),
            ('Cấu hình', (grp_config,)),
            ('Cửa sổ', (grp_window,)),
        ))
        root.addWidget(self.device_subtabs)
        return page

    def _create_schedule_tab(self):
        widget = QWidget()
        layout = QVBoxLayout(widget)
        group = QGroupBox('Hẹn giờ tạm dừng hoặc đóng tool')
        form = QFormLayout(group)
        self.combo_timer_mode = QComboBox()
        self.combo_timer_mode.addItems(['Sau khoảng thời gian', 'Vào lúc (giờ Việt Nam)'])
        self.spin_timer_minutes = QSpinBox()
        self.spin_timer_minutes.setRange(1, 10080)
        self.spin_timer_minutes.setSuffix(' phút')
        self.spin_timer_minutes.setValue(self.settings.get('timer_minutes', 60))
        self.edit_timer_at = QDateTimeEdit()
        self.edit_timer_at.setDisplayFormat('dd/MM/yyyy HH:mm')
        self.edit_timer_at.setCalendarPopup(True)
        local = datetime.now(timezone(timedelta(hours=7)))+timedelta(hours=1)
        self.edit_timer_at.setDateTime(QDateTime(local.replace(tzinfo=None)))
        self.edit_timer_at.setEnabled(False)
        self.combo_timer_mode.currentIndexChanged.connect(lambda index: (
            self.spin_timer_minutes.setEnabled(index == 0), self.edit_timer_at.setEnabled(index == 1)))
        self.combo_timer_action = QComboBox()
        self.combo_timer_action.addItems(['Tạm dừng bot', 'Đóng tool'])
        self.chk_timer_finish_battle = QCheckBox('Chờ kết thúc trận đang đánh trước khi thực hiện')
        self.chk_timer_finish_battle.setChecked(True)
        self.lbl_timer_state = QLabel('Chưa có lịch hẹn.')
        self.lbl_timer_state.setWordWrap(True)
        self.btn_timer_arm = QPushButton('Đặt lịch')
        self.btn_timer_arm.clicked.connect(self._arm_action_timer)
        self.btn_timer_cancel = QPushButton('Hủy lịch')
        self.btn_timer_cancel.setEnabled(False)
        self.btn_timer_cancel.clicked.connect(self._cancel_action_timer)
        buttons = QHBoxLayout()
        buttons.addWidget(self.btn_timer_arm)
        form.addRow('Kiểu hẹn:', self.combo_timer_mode)
        form.addRow('Thời gian chờ:', self.spin_timer_minutes)
        form.addRow('Ngày và giờ:', self.edit_timer_at)
        form.addRow('Hành động:', self.combo_timer_action)
        form.addRow(self.chk_timer_finish_battle)
        form.addRow(buttons)
        note = QLabel('Lịch hẹn áp dụng cho lần mở tool này. Đóng tool sẽ lưu thống kê; MuMu và game vẫn mở.')
        note.setWordWrap(True)
        status_group = QGroupBox('Lịch đang áp dụng')
        status_layout = QVBoxLayout(status_group)
        status_layout.addWidget(self.lbl_timer_state)
        status_layout.addWidget(self.btn_timer_cancel, alignment=Qt.AlignLeft)
        status_layout.addWidget(note)
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(0, 0, 0, 0)
        self.schedule_subtabs = self._make_subtabs((
            ('Thiết lập', (group,)),
            ('Trạng thái', (status_group,)),
        ))
        root.addWidget(self.schedule_subtabs)
        return page

    def _arm_action_timer(self):
        now = time.time()
        if self.combo_timer_mode.currentIndex() == 0:
            delay = self.spin_timer_minutes.value()*60
        else:
            chosen = self.edit_timer_at.dateTime().toPyDateTime().replace(tzinfo=timezone(timedelta(hours=7)))
            delay = chosen.timestamp()-now
        if delay <= 0:
            self.lbl_timer_state.setText('Chọn một thời điểm trong tương lai.')
            return
        action = 'pause' if self.combo_timer_action.currentIndex() == 0 else 'close'
        self.action_timer.arm(delay, action, self.chk_timer_finish_battle.isChecked())
        self._timer_wait_logged = False
        self._timer_finish_label = datetime.fromtimestamp(now+delay, timezone(timedelta(hours=7))).strftime('%H:%M %d/%m')
        self._timer_action_label = self.combo_timer_action.currentText()
        self.settings.set('timer_minutes', self.spin_timer_minutes.value())
        self.btn_timer_cancel.setEnabled(True)
        self._update_active_subtabs()
        self._on_log(f'[Hẹn giờ] {self._timer_action_label} lúc {self._timer_finish_label} (giờ Việt Nam).')
        self._check_action_timer()

    def _cancel_action_timer(self):
        self.action_timer.cancel()
        self.lbl_timer_state.setText('Đã hủy lịch hẹn.')
        self.btn_timer_cancel.setEnabled(False)
        self._update_active_subtabs()
        self._on_log('[Hẹn giờ] Đã hủy lịch.')

    def _check_action_timer(self):
        remaining = self.action_timer.remaining()
        if remaining is None:
            return
        waiting = (self.bot.is_running and not self.bot.is_paused
                   and self.bot.current_state in ('COMBAT', 'RECOVERY'))
        action = self.action_timer.take_due_action(in_battle=waiting)
        if action:
            self.btn_timer_cancel.setEnabled(False)
            self._update_active_subtabs()
            self.lbl_timer_state.setText('Đã thực hiện lịch hẹn.')
            self._on_log(f'[Hẹn giờ] Đến giờ: {self._timer_action_label}.')
            if action == 'close':
                self._exit_completely()
            elif self.bot.is_running and not self.bot.is_paused:
                self._pause_bot()
                self.bot.save_stats(force=True)
            return
        if remaining == 0:
            self.lbl_timer_state.setText('Đã đến giờ; đang chờ kết thúc trận hoặc khôi phục game.')
            if not self._timer_wait_logged:
                self._on_log('[Hẹn giờ] Đã đến giờ; chờ kết thúc trận trước khi thực hiện.')
                self._timer_wait_logged = True
        else:
            seconds = int(remaining)
            hours, rest = divmod(seconds, 3600)
            minutes, seconds = divmod(rest, 60)
            self.lbl_timer_state.setText(f'{self._timer_action_label} lúc {self._timer_finish_label}; còn {hours:02d}:{minutes:02d}:{seconds:02d}.')

    @staticmethod
    def _format_device_item(device: Dict[str, str]) -> Tuple[str, str]:
        serial = device.get('serial', '')
        state = device.get('state', 'device')
        model = device.get('model', '').strip()
        prod = device.get('product', '').strip()
        desc = model or prod or state

        is_mumu12 = any(f":{p}" in serial for p in range(16384, 16450))
        if is_mumu12 or any(p in serial for p in ('16384', '16416', '16448')):
            label = f"{serial} [MuMu Player 12] ({desc})"
        elif '7555' in serial:
            label = f"{serial} [MuMu Player 6] ({desc})"
        elif '5555' in serial or '5554' in serial:
            label = f"{serial} [Giả lập Android] ({desc})"
        elif 'mumu' in model.lower() or 'nemu' in model.lower() or 'mumu' in prod.lower():
            label = f"{serial} [MuMu Player] ({desc})"
        elif ':' not in serial:
            label = f"{serial} [Điện thoại / Scrcpy] ({desc})"
        else:
            label = f"{serial} ({desc})"
        return label, serial

    def _refresh_devices(self):
        selected = self.device_combo.currentData() or self.settings.get('device_serial')
        def done(devices):
            self.device_combo.clear()

            # Lọc bỏ duplicate alias 5555 / 7555 nếu đã có instance MuMu 12 (16384+) cùng model
            mumu_12_models = {
                (d.get('product'), d.get('model'))
                for d in devices
                if any(f":{p}" in d.get('serial', '') for p in range(16384, 16450))
            }
            cleaned_devices = []
            for d in devices:
                s = d.get('serial', '')
                key = (d.get('product'), d.get('model'))
                if key in mumu_12_models and any(s.endswith(p) for p in (':5555', ':7555')):
                    continue
                cleaned_devices.append(d)

            mumu_serials = []
            seen_serials = set()

            for device in cleaned_devices:
                label, serial = self._format_device_item(device)
                self.device_combo.addItem(label, serial)
                seen_serials.add(serial)
                if any(f":{p}" in serial for p in range(16384, 16450)) or '7555' in serial or 'mumu' in label.lower():
                    mumu_serials.append(serial)

            # Dự phòng các cổng MuMu mặc định nếu chưa phát hiện
            fallbacks = [
                ('127.0.0.1:16384', '127.0.0.1:16384 [MuMu Player 12]'),
                ('127.0.0.1:7555', '127.0.0.1:7555 [MuMu Player 6]'),
                ('127.0.0.1:5555', '127.0.0.1:5555 [Giả lập Android]'),
            ]
            for port, title in fallbacks:
                if port not in seen_serials and not mumu_serials:
                    self.device_combo.addItem(title, port)

            # Lựa chọn thiết bị thông minh:
            # 1. Nếu đang chọn MuMu hợp lệ -> giữ nguyên
            # 2. Nếu đang chọn thiết bị USB/Scrcpy nhưng có MuMu -> ưu tiên MuMu!
            target = None
            if selected and selected in mumu_serials:
                target = selected
            elif mumu_serials:
                target = mumu_serials[0]
            elif selected:
                target = selected
            else:
                target = '127.0.0.1:16384'

            index = self.device_combo.findData(target)
            if index >= 0:
                self.device_combo.setCurrentIndex(index)
            elif self.device_combo.count() > 0:
                self.device_combo.setCurrentIndex(0)

            chosen = self.device_combo.currentData()
            if chosen and chosen != selected and ':' in chosen:
                self.settings.set('device_serial', chosen)

        self._run_job(self.bot.adb.list_devices, done)

    def _connect_device(self):
        if self.bot.is_alive():
            self._on_log('Dừng bot trước khi đổi kết nối.')
            return
        serial = self.device_combo.currentData() or '127.0.0.1:5555'
        def done(ok):
            if ok:
                self.settings.set('device_serial', serial)
                self._on_log(f'Đã kết nối {serial}')
                self.statusBar().showMessage(f'Đã kết nối: {serial}')
                self._manual_capture()
            else:
                self._on_log(f'Không kết nối được {serial}. Kiểm tra MuMu và cổng ADB.')
        self._run_job(lambda: self.bot.adb.connect_device(serial), done)

    def _manual_resource_scan(self):
        if self.bot.is_alive():
            self.bot._manual_village_scan_requested = True
            self._on_log('[Làng] Đã yêu cầu quét; bot sẽ cập nhật khi về làng chính.')
            return
        if not self._preview_pending and not self._operation_pending:
            self._on_log('[Làng] Đang quét tài nguyên từ màn hình game...')
            def scan():
                frame = self.bot.adb.screencap()
                if frame is None:
                    return 'Không lấy được ảnh MuMu.'
                state = self.bot.village_state
                if self.bot.builder.is_home(frame):
                    state.set_active_village('builder')
                    read = self.bot.builder.scan_resources(frame)
                elif self.bot.village.is_in_main_village(frame):
                    state.set_active_village('main')
                    self.bot.farm_finish.suggested_upgrader().observe_home_builders(frame)
                    read = state.update_resources('main', self.bot.vision.read_village_resources(frame))
                else:
                    return 'Hãy mở màn hình làng chính hoặc làng đêm để quét.'
                return 'Đã quét tài nguyên.' if read else 'Chưa đọc rõ tài nguyên.'
            def done(message):
                self._on_log(f'[Làng] {message}')
                self.village_status.update_state(self.bot.village_state.get_snapshot(), self.settings.data)
            self._run_job(scan, done, preview=True)

    def _manual_capture(self):
        self._manual_resource_scan()

    def _poll_preview(self):
        self._sync_controls()
        self._highlight_active_village()
        stats = self.bot.get_stats()
        if isinstance(stats, dict):
            self._on_stats(stats)
        wall_status = getattr(self.bot.village, 'wall_status', None)
        if isinstance(wall_status, str) and self.lbl_wall_status.text() != wall_status:
            self.lbl_wall_status.setText(wall_status)
            if getattr(self.bot.village, 'walls_confirmed_maxed', False):
                self.lbl_wall_status.setStyleSheet('color: #34d399; font-weight: bold; ')
            else:
                self.lbl_wall_status.setStyleSheet('color: #aeb8c6; ')
        if hasattr(self, 'lbl_bldg_status') and hasattr(self.bot, 'farm_finish') and self.bot.farm_finish is not None:
            bldg_worker = getattr(self.bot.farm_finish, 'builder', None)
            if bldg_worker is not None:
                bldg_status = getattr(bldg_worker, 'last_status', None)
                if isinstance(bldg_status, str) and self.lbl_bldg_status.text() != bldg_status:
                    self.lbl_bldg_status.setText(bldg_status)
        if hasattr(self, 'village_status') and hasattr(self.bot, 'village_state'):
            snap = self.bot.village_state.get_snapshot()
            if getattr(self.bot.village, 'walls_confirmed_maxed', False):
                snap['walls_confirmed_maxed'] = True
            self.village_status.update_state(snap, self.settings.data)
            if self.dashboard_content.sizeHint().height() != self.dashboard_content.height():
                self._fit_overview()
        self._update_farm_finish_badge()
        if self.tabs.currentIndex() != 0 or self.isMinimized():
            self._idle_preview_wait = 0
            with self._frame_lock:
                self._latest_frame = None
            return

    def _is_walls_maxed(self) -> bool:
        v = getattr(self.bot, 'village', None)
        if v is not None:
            return bool(getattr(v, 'walls_confirmed_maxed', False))
        return bool(getattr(getattr(self.bot, 'village_state', None), 'walls_confirmed_maxed', False))

    def _update_farm_finish_badge(self):
        if not hasattr(self, 'chk_farm_finish') or not hasattr(self, 'lbl_farm_finish_max_badge'):
            return
        if self._is_walls_maxed():
            self.lbl_farm_finish_max_badge.setText('(Tường đã Max ✅)')
            self.lbl_farm_finish_max_badge.setStyleSheet('color: #34d399; font-weight: bold; ')
            if not self.chk_farm_finish.isChecked():
                self.chk_farm_finish.blockSignals(True)
                self.chk_farm_finish.setChecked(True)
                self.chk_farm_finish.blockSignals(False)
                self.settings.set('farm_finish_enabled', True)
                if hasattr(self, '_update_farm_finish_options'):
                    self._update_farm_finish_options()
                if hasattr(self, 'village_subtabs'):
                    self._update_active_subtabs()
        else:
            self.lbl_farm_finish_max_badge.setText('')

    def _on_farm_finish_toggled(self, enabled: bool):
        if not enabled and self._is_walls_maxed():
            self.lbl_farm_finish_max_badge.setText('(Tường đã Max — Tự động bật lại ✅)')
            self.lbl_farm_finish_max_badge.setStyleSheet('color: #34d399; font-weight: bold; ')
            self.chk_farm_finish.blockSignals(True)
            self.chk_farm_finish.setChecked(True)
            self.chk_farm_finish.blockSignals(False)
            self.settings.set('farm_finish_enabled', True)
            if hasattr(self, '_update_farm_finish_options'):
                self._update_farm_finish_options()
            if hasattr(self, 'village_subtabs'):
                self._update_active_subtabs()
            self._on_log('[Wall] Tường đã Max hoàn toàn: tự động duy trì bật chế độ cày full kho & dừng tool.')
            QTimer.singleShot(3500, self._update_farm_finish_badge)
            return
        self.settings.set('farm_finish_enabled', enabled)
        self._update_farm_finish_badge()

    def _manual_verify_layout_editor(self):
        if self.bot.is_alive() or self.bot.is_running or self._operation_pending or self._preview_pending:
            self.statusBar().showMessage('Dừng bot trước khi quét thủ công Layout Editor.', 4000)
            return
        def scan():
            self._on_log('[Wall] Đang mở Layout Editor để quét xác minh tường cho tài khoản hiện tại...')
            scr = self.bot.adb.screencap()
            if scr is None or not self.bot.village.is_in_main_village(scr):
                return 'Hãy mở màn hình làng chính trước khi quét.'
            self.bot.village.check_and_sync_account(scr, self._on_log)
            res = self.bot.village.verify_walls(self._on_log)
            if res is None:
                return 'Không thể quét Layout Editor (kiểm tra giao diện game).'
            if res.get('maxed'):
                th_str = f" (TH{res.get('th_level')})" if res.get('th_level') else ""
                return f"ĐÃ XÁC MINH: Toàn bộ {res.get('available', 325)} đoạn tường ĐÃ MAX HOÀN TOÀN Cấp {res.get('min_level')}{th_str} ✅."
            return f"ĐÃ XÁC MINH: Tường CHƯA MAX (còn {res.get('available')} đoạn Cấp {res.get('min_level')})."
        def done(msg):
            self._on_log(f'[Wall] {msg}')
            self._update_farm_finish_badge()
            snap = self.bot.village_state.get_snapshot()
            if getattr(self.bot.village, 'walls_confirmed_maxed', False):
                snap['walls_confirmed_maxed'] = True
            self.village_status.update_state(snap, self.settings.data)
        self._run_job(scan, done, preview=True)

    def _recheck_wall_builders(self):
        self.bot.village.clear_wall_wait()
        self.lbl_wall_status.setText('Sẽ kiểm tra lại thợ ở lượt về làng kế tiếp.')
        self._on_log('[Wall] Đã yêu cầu kiểm tra lại thợ; không chờ lịch cũ.')

    def _calibrate_fixed_attack(self):
        if self.bot.is_alive() or self.bot.is_running or self._operation_pending or self._preview_pending:
            self.statusBar().showMessage('Dừng bot và chờ thao tác đang chạy trước khi hiệu chuẩn.', 5000)
            return
        serial = self.device_combo.currentData() or self.settings.get('device_serial', '127.0.0.1:5555')
        previous = FixedAttackPlan.load(self.settings, serial)
        zoom, ok = QInputDialog.getInt(self, 'Thu nhỏ bản đồ',
                                      'Mở màn hình soi nhà hoặc trận giao hữu trong MuMu. Chọn số lần cần để thu nhỏ tối đa:',
                                      previous['zoom'] if previous else 4, 1, 8)
        if not ok:
            return

        def capture():
            adb = self.bot.adb
            if adb.device_serial != serial and not adb.connect_device(serial):
                return None
            frame = adb.screencap()
            if getattr(frame, 'shape', ())[:2] != (900, 1600):
                return None
            if self.bot.vision.battle_phase(frame) not in ('scouting', 'combat'):
                return None
            self.bot.combat.zoom_out_map(zoom)
            return adb.screencap()

        def edit(frame):
            if getattr(frame, 'shape', ())[:2] != (900, 1600):
                QMessageBox.information(self, 'Hiệu chuẩn', 'Cần màn hình soi nhà hoặc trận giao hữu ở 1600×900.')
                return
            dialog = FixedCalibrationDialog(frame, zoom, previous if previous and previous['zoom'] == zoom else None, self)
            if dialog.exec_() == dialog.Accepted:
                profiles = self.settings.get(FixedAttackPlan.SETTINGS_KEY, {})
                profiles = dict(profiles) if isinstance(profiles, dict) else {}
                profiles[serial] = dialog.profile()
                self.settings.set(FixedAttackPlan.SETTINGS_KEY, profiles)
                self.statusBar().showMessage(f'Đã lưu hiệu chuẩn rải quân cho {serial}.', 5000)

        self._run_job(capture, edit)

    def _start_bot(self):
        serial = self.device_combo.currentData() or '127.0.0.1:5555'
        self._session_elapsed = 0.0
        self._session_started_at = time.monotonic()
        self.statusBar().showMessage('Đang kết nối và chuẩn bị game…')
        def done(ok):
            if not ok:
                self._session_elapsed = 0.0
                self._session_started_at = None
            self.statusBar().showMessage('Bot đang chạy' if ok else 'Chưa khởi động được. Xem nhật ký.')
        self._run_job(lambda: self.bot.start_bot(device_serial=serial), done, control='start')

    def _toggle_run_pause(self):
        if self.bot.is_running:
            self._pause_bot()
        else:
            self._start_bot()

    def _pause_bot(self):
        if self.bot.is_paused:
            self.bot.resume_bot()
        else:
            self.bot.pause_bot()
        self._sync_controls()

    def _stop_bot(self):
        self.statusBar().showMessage('Đang dừng bot và lưu thống kê…')
        self._run_job(self.bot.stop_bot, self._show_stopped, control='stop')

    def _show_stopped(self, _):
        self.state_badge.setText('ĐÃ DỪNG')
        self.statusBar().showMessage('Bot đã dừng.')

    def _run_optimization(self):
        if self.bot.is_alive():
            self._on_log('Dừng bot trước khi thay thiết lập giả lập.')
            return
        serial = self.device_combo.currentData() or '127.0.0.1:5555'
        def operation():
            if not self.bot.adb.connect_device(serial):
                raise RuntimeError('Không kết nối được MuMu.')
            opt = MuMuOptimizer(self.bot.adb)
            return list(opt.optimize_windows_priority().values()) + opt.apply_android_optimizations()
        def done(messages):
            for message in messages:
                self._on_log(message)
            self.statusBar().showMessage('Đã áp dụng thiết lập MuMu. Chi tiết trong nhật ký.')
        self._run_job(operation, done)

    def _fix_resolution(self):
        if self.bot.is_alive():
            self._on_log('Dừng bot trước khi đổi độ phân giải.')
            return
        serial = self.device_combo.currentData() or '127.0.0.1:5555'
        def operation():
            if not self.bot.adb.connect_device(serial):
                raise RuntimeError('Không kết nối được MuMu.')
            MuMuOptimizer(self.bot.adb).set_optimal_resolution()
        self._run_job(operation, lambda _: self._on_log('Đã đặt màn hình 1600×900, 240 DPI.'))

    def _close_after_farm(self):
        if self.bot.is_running:
            return
        if self.bot.is_alive():
            QTimer.singleShot(200, self._close_after_farm)
            return
        self._exit_completely()

    def closeEvent(self, event):
        if (not self._force_quit and self.settings.get('ui_close_behavior', 'quit') == 'tray'
                and QSystemTrayIcon.isSystemTrayAvailable()):
            event.ignore()
            self._minimize_to_tray()
            return
        self._closing = True
        if hasattr(self, '_tray_icon'):
            self._tray_icon.hide()
            QApplication.instance().setQuitOnLastWindowClosed(True)
        self.preview_timer.stop()
        self.performance_timer.stop()
        self.action_timer_tick.stop()
        self.action_timer.cancel()
        self.bot.close()
        self._io_pool.shutdown(wait=False, cancel_futures=True)
        event.accept()

    def _on_log(self, text: str):
        if not hasattr(self, 'log_console'):
            self._pending_logs.append(text)
            return
        self.log_console.append_log(text)
        village = self.bot.village_state.get_snapshot().get('active_village', 'main')
        activity = activity_from_log(text, village)
        if activity:
            self._set_activity(*activity)

    def _set_activity(self, village, section, summary, detail=None):
        self._activity_village = village
        self._activity_section = section
        self._activity_text = summary
        self.activity_btn.setToolTip((detail or summary) + '\nBấm để mở mục liên quan')
        self.activity_btn.setAccessibleName(f'Hoạt động {"làng đêm" if village == "builder" else "làng chính"}: {summary}')
        for item in (self.activity_btn, self.state_badge):
            if item.property('village') != village:
                item.setProperty('village', village)
                item.style().unpolish(item)
                item.style().polish(item)
        self._refresh_activity_button()

    def _refresh_activity_button(self):
        if not hasattr(self, 'activity_btn'):
            return
        width = max(120, self.activity_btn.width() - 24)
        self.activity_btn.setText(self.activity_btn.fontMetrics().elidedText(
            getattr(self, '_activity_text', 'Sẵn sàng'), Qt.ElideRight, width))

    def _open_activity_tab(self):
        section = self._activity_section
        village = self._activity_village
        if village == 'builder':
            self._select_tab(self.builder_village_tab)
            index = {'wall': 1, 'building': 2, 'battle': 3, 'search': 3}.get(section, 0)
            self.builder_subtabs.setCurrentIndex(index)
        elif section in {'search', 'battle'}:
            self._select_tab(self.main_attack_tab)
            self.combat_subtabs.setCurrentIndex(0 if section == 'search' else 1)
        elif section == 'device':
            self._select_tab(self.device_tab)
        elif section == 'overview':
            self._select_tab(0)
        else:
            self._select_tab(self.main_village_tab)
            self.village_subtabs.setCurrentIndex({'wall': 1, 'building': 2, 'army': 3}.get(section, 0))

    def _on_screen(self, screen_img: np.ndarray):
        self._queue_frame(screen_img)

    def _reset_fastest_record(self):
        try:
            self.bot.reset_fastest_record()
        except OSError as exc:
            self.log_console.append_log(f'[Kỷ lục] Chưa lưu được lệnh đặt lại: {exc}')

    def _make_reset_button(self, callback, label: str = "Đặt lại mặc định") -> QPushButton:
        btn = QPushButton(f"🔄 {label}")
        btn.setProperty("variant", "reset")
        btn.setToolTip("Khôi phục các thông số trong phần này về giá trị mặc định ban đầu")
        btn.clicked.connect(callback)
        return btn

    def _reset_loot_defaults(self):
        self.spin_gold.setValue(DEFAULT_CONFIG["min_gold"])
        self.spin_elixir.setValue(DEFAULT_CONFIG["min_elixir"])
        self.spin_dark.setValue(DEFAULT_CONFIG["min_dark_elixir"])
        self.spin_skips.setValue(DEFAULT_CONFIG["max_search_skips"])
        filter_modes = ['legacy', 'all', 'any']
        def_mode = DEFAULT_CONFIG["loot_filter_mode"]
        if def_mode in filter_modes:
            self.combo_loot_filter.setCurrentIndex(filter_modes.index(def_mode))
        for key in ('gold', 'elixir', 'dark_elixir'):
            if key in self.loot_filter_checks:
                self.loot_filter_checks[key].setChecked(DEFAULT_CONFIG[f'loot_filter_{key}'])
        for k in ("min_gold", "min_elixir", "min_dark_elixir", "max_search_skips",
                  "loot_filter_mode", "loot_filter_gold", "loot_filter_elixir", "loot_filter_dark_elixir"):
            self.settings.set(k, DEFAULT_CONFIG[k])
        self.statusBar().showMessage("Đã đặt lại mặc định: Điều kiện chọn đối thủ", 3000)

    def _reset_village_care_defaults(self):
        self.chk_assign_apprentice.setChecked(DEFAULT_CONFIG["auto_assign_apprentice"])
        self.chk_collect.setChecked(DEFAULT_CONFIG["auto_collect"])
        self.spin_collect_interval.setValue(DEFAULT_CONFIG["collect_interval_min"])
        self.chk_train.setChecked(DEFAULT_CONFIG["auto_train_army"])
        self.chk_request.setChecked(DEFAULT_CONFIG["auto_request_troops"])
        self.spin_train_slot.setValue(DEFAULT_CONFIG["quick_train_slot"])
        self.spin_maintenance.setValue(DEFAULT_CONFIG["maintenance_interval_sec"])
        self.spin_reserve_builders.setValue(DEFAULT_CONFIG["reserve_builders"])
        for k in ("auto_assign_apprentice", "auto_collect", "auto_train_army", "auto_request_troops",
                  "collect_interval_min", "quick_train_slot", "maintenance_interval_sec", "reserve_builders"):
            self.settings.set(k, DEFAULT_CONFIG[k])
        self.statusBar().showMessage("Đã đặt lại mặc định: Chăm sóc làng", 3000)

    def _reset_treasury_defaults(self):
        self.chk_treasury.setChecked(DEFAULT_CONFIG["auto_collect_treasury"])
        self.chk_treasury_star.setChecked(DEFAULT_CONFIG["treasury_star_bonus_enabled"])
        self.chk_treasury_war.setChecked(DEFAULT_CONFIG["treasury_war_rewards_enabled"])
        self.spin_treasury_interval.setValue(DEFAULT_CONFIG["treasury_check_interval_min"])
        for k in ("auto_collect_treasury", "treasury_star_bonus_enabled",
                  "treasury_war_rewards_enabled", "treasury_check_interval_min"):
            self.settings.set(k, DEFAULT_CONFIG[k])
        self.statusBar().showMessage("Đã đặt lại mặc định: Kho bạc Clan Castle", 3000)

    def _reset_wall_defaults(self):
        self.chk_walls.setChecked(DEFAULT_CONFIG["auto_upgrade_walls"])
        wall_keys = ['both', 'gold', 'elixir']
        def_res = DEFAULT_CONFIG["wall_resource"]
        if def_res in wall_keys:
            self.combo_wall_res.setCurrentIndex(wall_keys.index(def_res))
        self.spin_wall_gold.setValue(DEFAULT_CONFIG["min_gold_to_upgrade_wall"])
        self.spin_wall_elixir.setValue(DEFAULT_CONFIG["min_elixir_to_upgrade_wall"])
        self.chk_wall_lowest.setChecked(DEFAULT_CONFIG["wall_lowest_first"])
        self.combo_wall_quantity_mode.setCurrentIndex(1 if DEFAULT_CONFIG["wall_quantity_button_mode"] == 'fixed' else 0)
        self.spin_wall_fixed_gap.setValue(DEFAULT_CONFIG["wall_quantity_fixed_gap_ms"])
        self.chk_wall_hold.setChecked(DEFAULT_CONFIG["wall_hold_enabled"])
        self.chk_farm_finish.setChecked(DEFAULT_CONFIG["farm_finish_enabled"])
        if hasattr(self, 'chk_farm_finish_close'):
            self.chk_farm_finish_close.setChecked(DEFAULT_CONFIG.get("farm_finish_close_app", False))
        self.spin_farm_finish_percent.setValue(DEFAULT_CONFIG["farm_finish_target_percent"])
        finish_modes = ['gold_elixir', 'gold', 'elixir']
        def_finish = DEFAULT_CONFIG["farm_finish_resources"]
        if def_finish in finish_modes:
            self.combo_farm_finish_resources.setCurrentIndex(finish_modes.index(def_finish))
        for k in ("auto_upgrade_walls", "wall_resource", "min_gold_to_upgrade_wall",
                  "min_elixir_to_upgrade_wall", "wall_lowest_first",
                  "wall_quantity_button_mode", "wall_quantity_fixed_gap_ms", "wall_hold_enabled",
                  "farm_finish_enabled", "farm_finish_target_percent", "farm_finish_resources"):
            self.settings.set(k, DEFAULT_CONFIG[k])
        self.statusBar().showMessage("Đã đặt lại mặc định: Nâng tường", 3000)

    def _save_building_resource_toggles(self):
        res = {k: chk.isChecked() for k, chk in self.chk_bldg_resources.items()}
        self.settings.set("building_upgrade_resources", res)
        self._apply_resource_constraints_to_categories()
        self._update_building_selection_summary()

    def _apply_resource_constraints_to_categories(self):
        res_names = {"gold": "Vàng", "elixir": "Dầu hồng", "dark_elixir": "Dầu đen"}
        selected_res = {k for k, chk in self.chk_bldg_resources.items() if chk.isChecked()}
        cats_to_save = {}
        for c_key, chk in self.chk_bldg_categories.items():
            supported = CATEGORY_TO_RESOURCES.get(c_key, ())
            has_match = any(r in selected_res for r in supported) if selected_res else True
            chk.setEnabled(has_match)
            if not has_match:
                chk.blockSignals(True)
                chk.setChecked(False)
                chk.blockSignals(False)
                supp_str = ", ".join(res_names.get(r, r) for r in supported)
                chk.setToolTip(f"Nhóm này không nâng bằng tài nguyên đang chọn (chỉ nâng bằng: {supp_str})")
            else:
                chk.setToolTip("")
            cats_to_save[c_key] = chk.isChecked()
        self.settings.set("building_upgrade_categories", cats_to_save)

    def _save_building_category_toggles(self):
        cats = {k: chk.isChecked() for k, chk in self.chk_bldg_categories.items()}
        self.settings.set("building_upgrade_categories", cats)
        self._update_building_selection_summary()

    def _on_building_preset_selected(self, index: int):
        pn = self.combo_bldg_preset.itemData(index)
        if pn:
            self._apply_building_preset(pn)
            self.combo_bldg_preset.blockSignals(True)
            self.combo_bldg_preset.setCurrentIndex(0)
            self.combo_bldg_preset.blockSignals(False)

    def _apply_building_preset(self, preset_name: str):
        if preset_name == "all":
            for chk in self.chk_bldg_resources.values(): chk.setChecked(True)
            for chk in self.chk_bldg_categories.values(): chk.setChecked(True)
            self.settings.set("building_upgrade_whitelist", dict(DEFAULT_BUILDING_WHITELIST))
        elif preset_name == "defense_traps":
            self.chk_bldg_resources["gold"].setChecked(True)
            self.chk_bldg_resources["elixir"].setChecked(True)
            self.chk_bldg_resources["dark_elixir"].setChecked(True)
            for k, chk in self.chk_bldg_categories.items():
                chk.setChecked(k in ("traps", "defense"))
        elif preset_name == "resource":
            self.chk_bldg_resources["gold"].setChecked(True)
            self.chk_bldg_resources["elixir"].setChecked(True)
            self.chk_bldg_resources["dark_elixir"].setChecked(False)
            for k, chk in self.chk_bldg_categories.items():
                chk.setChecked(k == "resource")
        elif preset_name == "army_heroes":
            self.chk_bldg_resources["gold"].setChecked(True)
            self.chk_bldg_resources["elixir"].setChecked(True)
            self.chk_bldg_resources["dark_elixir"].setChecked(True)
            for k, chk in self.chk_bldg_categories.items():
                chk.setChecked(k in ("army", "heroes"))
        elif preset_name == "elixir_focus":
            self.chk_bldg_resources["gold"].setChecked(False)
            self.chk_bldg_resources["elixir"].setChecked(True)
            self.chk_bldg_resources["dark_elixir"].setChecked(False)
            for k, chk in self.chk_bldg_categories.items():
                chk.setChecked(k in ("defense", "resource", "army", "heroes"))
        elif preset_name == "gold_focus":
            self.chk_bldg_resources["gold"].setChecked(True)
            self.chk_bldg_resources["elixir"].setChecked(False)
            self.chk_bldg_resources["dark_elixir"].setChecked(False)
            for k, chk in self.chk_bldg_categories.items():
                chk.setChecked(k in ("traps", "defense", "resource", "army"))
        self._update_building_selection_summary()
        self.statusBar().showMessage(f"Đã áp dụng mẫu cấu hình công trình: {preset_name}", 3000)

    def _update_building_selection_summary(self):
        if not hasattr(self, "lbl_bldg_count"):
            return
        wl = dict(DEFAULT_BUILDING_WHITELIST)
        saved_wl = self.settings.get("building_upgrade_whitelist")
        if isinstance(saved_wl, dict):
            wl.update(saved_wl)
        cats = dict(DEFAULT_CONFIG["building_upgrade_categories"])
        if isinstance(self.settings.get("building_upgrade_categories"), dict):
            cats.update(self.settings.get("building_upgrade_categories"))
        res_cfg = dict(DEFAULT_CONFIG["building_upgrade_resources"])
        if isinstance(self.settings.get("building_upgrade_resources"), dict):
            res_cfg.update(self.settings.get("building_upgrade_resources"))

        total = sum(len(items) for items in BUILDING_CATALOG.values())
        active_count = 0
        gold_count = 0
        elixir_count = 0
        dark_count = 0

        for cat_key, items in BUILDING_CATALOG.items():
            if not cats.get(cat_key, True):
                continue
            for b_key, _, _ in items:
                if not wl.get(b_key, True):
                    continue
                b_resources = BUILDING_KEY_TO_RESOURCES.get(b_key, ())
                if any(res_cfg.get(r, True) for r in b_resources):
                    active_count += 1
                    if "gold" in b_resources: gold_count += 1
                    if "elixir" in b_resources: elixir_count += 1
                    if "dark_elixir" in b_resources: dark_count += 1

        self.lbl_bldg_count.setText(
            f"Đang bật: {active_count}/{total} công trình (🟡 {gold_count} Vàng | 🟣 {elixir_count} Dầu hồng | ⚫ {dark_count} Dầu đen)"
        )

    def _open_building_selection_dialog(self):
        dlg = BuildingSelectionDialog(self.settings, self)
        dlg.exec_()
        self._update_building_selection_summary()

    def _update_specific_target_visibility(self):
        is_spec = hasattr(self, 'combo_bldg_priority') and self.combo_bldg_priority.currentData() == "specific"
        if hasattr(self, 'lbl_specific_target_title'):
            self.lbl_specific_target_title.setVisible(is_spec)
        if hasattr(self, 'combo_specific_target'):
            self.combo_specific_target.setVisible(is_spec)

    def _reset_building_upgrade_defaults(self):
        self.chk_auto_buildings.setChecked(DEFAULT_CONFIG["auto_upgrade_buildings"])
        if hasattr(self, 'chk_lock_townhall'):
            self.chk_lock_townhall.setChecked(DEFAULT_CONFIG["lock_townhall_upgrade"])
        self.settings.set("lock_townhall_upgrade", DEFAULT_CONFIG["lock_townhall_upgrade"])
        if hasattr(self, 'combo_specific_target'):
            self.combo_specific_target.setCurrentIndex(
                max(0, self.combo_specific_target.findData(DEFAULT_CONFIG["building_upgrade_specific_target"]))
            )
        self.settings.set("building_upgrade_specific_target", DEFAULT_CONFIG["building_upgrade_specific_target"])
        self._update_specific_target_visibility()
        self.combo_bldg_priority.setCurrentIndex(
            max(0, self.combo_bldg_priority.findData(DEFAULT_CONFIG["building_upgrade_priority"]))
        )
        self.combo_bldg_wait_mode.setCurrentIndex(
            max(0, self.combo_bldg_wait_mode.findData(DEFAULT_CONFIG["building_upgrade_wait_mode"]))
        )
        self.chk_bldg_menu_filter.setChecked(DEFAULT_CONFIG["building_upgrade_use_menu_filter"])
        for k, chk in self.chk_bldg_resources.items():
            chk.blockSignals(True)
            chk.setChecked(DEFAULT_CONFIG["building_upgrade_resources"][k])
            chk.blockSignals(False)
        for k, chk in self.chk_bldg_categories.items():
            chk.blockSignals(True)
            chk.setChecked(DEFAULT_CONFIG["building_upgrade_categories"][k])
            chk.blockSignals(False)
        self.settings.set("auto_upgrade_buildings", DEFAULT_CONFIG["auto_upgrade_buildings"])
        self.settings.set("building_upgrade_priority", DEFAULT_CONFIG["building_upgrade_priority"])
        self.settings.set("building_upgrade_wait_mode", DEFAULT_CONFIG["building_upgrade_wait_mode"])
        self.settings.set("building_upgrade_use_menu_filter", DEFAULT_CONFIG["building_upgrade_use_menu_filter"])
        self.settings.set("building_upgrade_resources", dict(DEFAULT_CONFIG["building_upgrade_resources"]))
        self.settings.set("building_upgrade_categories", dict(DEFAULT_CONFIG["building_upgrade_categories"]))
        self.settings.set("building_upgrade_whitelist", dict(DEFAULT_BUILDING_WHITELIST))
        self._update_building_selection_summary()
        self.statusBar().showMessage("Đã đặt lại mặc định: Nâng cấp công trình tùy chọn", 3000)

    def _reset_attack_strat_defaults(self):
        strat_keys = ["perimeter", "four_corners", "smart_wave", "hybrid", "resource_dragons", "ultra_fast", "random_mix", FixedAttackPlan.KEY]
        def_strat = DEFAULT_CONFIG["attack_strategy"]
        if def_strat in strat_keys:
            self.combo_strat.setCurrentIndex(strat_keys.index(def_strat))
        self.spin_drop_delay.setValue(DEFAULT_CONFIG["troop_drop_delay_ms"])
        fallback_keys = ['perimeter', 'four_corners', 'smart_wave', 'hybrid']
        def_fb = DEFAULT_CONFIG["resource_attack_fallback"]
        if def_fb in fallback_keys:
            self.combo_resource_fallback.setCurrentIndex(fallback_keys.index(def_fb))
        for key in ('attack_spread_points', 'spell_first_delay_sec', 'spell_wave_gap_sec'):
            if hasattr(self, 'spin_attack_controls') and key in self.spin_attack_controls:
                self.spin_attack_controls[key].setValue(round(DEFAULT_CONFIG[key]))
        for k in ("attack_strategy", "troop_drop_delay_ms", "resource_attack_fallback",
                  "attack_spread_points", "spell_first_delay_sec", "spell_wave_gap_sec"):
            self.settings.set(k, DEFAULT_CONFIG[k])
        self.statusBar().showMessage("Đã đặt lại mặc định: Cách thả quân", 3000)

    def _reset_ultra_strat_defaults(self):
        self.chk_ultra_fixed.setChecked(DEFAULT_CONFIG["ultra_fixed_positions"])
        for key in ('ultra_drop_gap_ms', 'ultra_card_select_ms', 'ultra_spell_inward_pct', 'ultra_spell_max_lead_px'):
            if hasattr(self, 'spin_ultra_controls') and key in self.spin_ultra_controls:
                self.spin_ultra_controls[key].setValue(DEFAULT_CONFIG[key])
        for k in ("ultra_fixed_positions", "ultra_drop_gap_ms", "ultra_card_select_ms",
                  "ultra_spell_inward_pct", "ultra_spell_max_lead_px"):
            self.settings.set(k, DEFAULT_CONFIG[k])
        self.statusBar().showMessage("Đã đặt lại mặc định: Siêu nhanh", 3000)

    def _reset_heroes_defaults(self):
        self.chk_king.setChecked(DEFAULT_CONFIG["use_king"])
        self.chk_queen.setChecked(DEFAULT_CONFIG["use_queen"])
        self.chk_warden.setChecked(DEFAULT_CONFIG["use_warden"])
        self.chk_champ.setChecked(DEFAULT_CONFIG["use_champion"])
        for name, key in (('prince', 'use_prince'), ('luke', 'use_luke')):
            if hasattr(self, 'chk_hero_extras') and name in self.chk_hero_extras:
                self.chk_hero_extras[name].setChecked(DEFAULT_CONFIG[key])
        self.chk_abilities.setChecked(DEFAULT_CONFIG["trigger_hero_abilities"])
        self.spin_ability_delay.setValue(DEFAULT_CONFIG["hero_ability_delay_sec"])
        for k in ("use_king", "use_queen", "use_warden", "use_champion",
                  "use_prince", "use_luke", "trigger_hero_abilities", "hero_ability_delay_sec"):
            self.settings.set(k, DEFAULT_CONFIG[k])
        self.statusBar().showMessage("Đã đặt lại mặc định: Tướng sử dụng", 3000)

    def _reset_siege_defaults(self):
        self.chk_use_siege.setChecked(DEFAULT_CONFIG["use_siege"])
        mode = DEFAULT_CONFIG["siege_deploy_mode"]
        self.combo_siege_mode.setCurrentIndex(1 if mode == "require_cc" else 0)
        self.settings.set("use_siege", DEFAULT_CONFIG["use_siege"])
        self.settings.set("siege_deploy_mode", mode)
        self.statusBar().showMessage("Đã đặt lại mặc định: Cỗ máy công thành", 3000)

    def _reset_surrender_defaults(self):
        self.chk_auto_surr.setChecked(DEFAULT_CONFIG["auto_surrender"])
        self.chk_end_stars.setChecked(DEFAULT_CONFIG["end_on_stars"])
        star_sources = ['all', 'town_hall', 'destruction']
        def_source = DEFAULT_CONFIG["end_star_source"]
        if def_source in star_sources:
            self.combo_star_source.setCurrentIndex(star_sources.index(def_source))
        self.spin_stars.setValue(DEFAULT_CONFIG["star_target"])
        self.chk_end_time.setChecked(DEFAULT_CONFIG["end_on_time"])
        self.spin_timeout.setValue(DEFAULT_CONFIG["battle_timeout_sec"])
        self.chk_end_loot.setChecked(DEFAULT_CONFIG["end_on_loot"])
        self.spin_loot_percent.setValue(DEFAULT_CONFIG["loot_target_percent"])
        loot_keys = ['gold_elixir', 'gold', 'elixir', 'dark_elixir', 'all']
        def_loot_res = DEFAULT_CONFIG["loot_target_resource"]
        if def_loot_res in loot_keys:
            self.combo_loot_resource.setCurrentIndex(loot_keys.index(def_loot_res))
        self.chk_end_destruction.setChecked(DEFAULT_CONFIG["end_on_destruction"])
        self.spin_destruction.setValue(DEFAULT_CONFIG["destruction_target_percent"])
        self.combo_end_mode.setCurrentIndex(1 if DEFAULT_CONFIG["end_condition_mode"] == 'all' else 0)
        for k in ("auto_surrender", "end_on_stars", "end_star_source", "star_target",
                  "end_on_time", "battle_timeout_sec", "end_on_loot", "loot_target_percent",
                  "loot_target_resource", "end_on_destruction", "destruction_target_percent", "end_condition_mode"):
            self.settings.set(k, DEFAULT_CONFIG[k])
        self.statusBar().showMessage("Đã đặt lại mặc định: Kết thúc trận", 3000)

    def _reset_builder_base_defaults(self):
        self.chk_builder_village.setChecked(DEFAULT_CONFIG["builder_base_enabled"])
        for key, _ in BuilderBaseManager.OPTIONS:
            if hasattr(self, 'builder_tasks') and key in self.builder_tasks:
                self.builder_tasks[key].setChecked(DEFAULT_CONFIG[key])
        for resource in ("gold", "elixir"):
            key = f"builder_base_wall_min_{resource}"
            if hasattr(self, 'builder_wall_thresholds') and key in self.builder_wall_thresholds:
                self.builder_wall_thresholds[key].setValue(DEFAULT_CONFIG[key])
        if hasattr(self, 'chk_builder_wall_lowest'):
            self.chk_builder_wall_lowest.setChecked(DEFAULT_CONFIG['builder_base_wall_lowest_first'])
        if hasattr(self, 'combo_builder_wall_trigger'):
            self.combo_builder_wall_trigger.setCurrentIndex(max(0, self.combo_builder_wall_trigger.findData(
                DEFAULT_CONFIG['builder_base_wall_trigger'])))
        if hasattr(self, 'combo_builder_combo'):
            idx = max(0, self.combo_builder_combo.findData(DEFAULT_CONFIG["builder_base_combo"]))
            self.combo_builder_combo.setCurrentIndex(idx)
        for key in REINFORCEMENT_KEYS:
            if hasattr(self, 'builder_reinforcement_combos') and key in self.builder_reinforcement_combos:
                selector = self.builder_reinforcement_combos[key]
                idx = max(0, selector.findData(DEFAULT_CONFIG[key]))
                selector.setCurrentIndex(idx)
        if hasattr(self, 'chk_builder_obstacle'):
            self.chk_builder_obstacle.setChecked(DEFAULT_CONFIG["builder_base_obstacle_attack"])
        if hasattr(self, 'chk_builder_pause'):
            self.chk_builder_pause.setChecked(DEFAULT_CONFIG.get("builder_base_pause_on_no_upgrades", False))
        if hasattr(self, 'spin_builder_reserve'):
            self.spin_builder_reserve.setValue(DEFAULT_CONFIG.get("builder_base_reserve_builders", 0))
        if hasattr(self, 'combo_builder_priority'):
            idx = max(0, self.combo_builder_priority.findData(DEFAULT_CONFIG.get("builder_base_building_priority", "suggested")))
            self.combo_builder_priority.setCurrentIndex(idx)
        self.spin_builder_int.setValue(DEFAULT_CONFIG["builder_base_interval_mins"])
        builder_keys = ["builder_base_enabled", "builder_base_wall_min_gold", "builder_base_wall_min_elixir",
                        "builder_base_combo", "builder_base_obstacle_attack", "builder_base_interval_mins",
                        "builder_base_pause_on_no_upgrades", "builder_base_wall_lowest_first", "builder_base_wall_trigger",
                        "builder_base_reserve_builders", "builder_base_building_priority", "builder_base_building_resources"]
        builder_keys.extend([k for k, _ in BuilderBaseManager.OPTIONS])
        builder_keys.extend(REINFORCEMENT_KEYS)
        for k in builder_keys:
            self.settings.set(k, DEFAULT_CONFIG[k])
        self.statusBar().showMessage("Đã đặt lại mặc định: Làng đêm", 3000)

    def _reset_antiban_defaults(self):
        self.chk_reconnect.setChecked(DEFAULT_CONFIG["auto_reconnect"])
        self.chk_restart.setChecked(DEFAULT_CONFIG["auto_restart_game"])
        self.chk_periodic_restart.setChecked(DEFAULT_CONFIG["periodic_restart_enabled"])
        self.spin_restart_interval.setValue(DEFAULT_CONFIG["periodic_restart_interval_mins"])
        for k in ("auto_reconnect", "auto_restart_game", "periodic_restart_enabled", "periodic_restart_interval_mins"):
            self.settings.set(k, DEFAULT_CONFIG[k])
        self.statusBar().showMessage("Đã đặt lại mặc định: Thao tác & khôi phục", 3000)

    def _save_current_config(self):
        """Save current in-memory settings to config.json with visual feedback."""
        try:
            self.settings.save()
            msg = "Đã lưu cấu hình hiện tại vào config.json thành công!"
            self.statusBar().showMessage(msg, 4000)
            self._on_log(f"[Cấu hình] {msg}")
            QMessageBox.information(self, "Lưu cấu hình", msg)
        except Exception as e:
            err = f"Lỗi khi lưu cấu hình: {e}"
            self.statusBar().showMessage(err, 4000)
            self._on_log(f"[Cấu hình] {err}")
            QMessageBox.critical(self, "Lỗi lưu cấu hình", err)

    def _export_config(self):
        """Export current settings to a user-chosen JSON file."""
        try:
            self.settings.save()
            default_name = f"cocattack_config_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            path, _ = QFileDialog.getSaveFileName(
                self,
                "Xuất cấu hình Tool CoCAttack",
                default_name,
                "JSON Files (*.json);;All Files (*)"
            )
            if not path:
                return
            with open(path, "w", encoding="utf-8") as f:
                from core.stats_store import StatsStore
                json.dump(self.settings.data, f, indent=4, ensure_ascii=False, default=StatsStore.json_default)
            msg = f"Đã xuất cấu hình thành công ra:\n{path}"
            self.statusBar().showMessage(f"Đã xuất cấu hình ra {os.path.basename(path)}.", 4000)
            self._on_log(f"[Cấu hình] Đã xuất cấu hình ra tệp: {path}")
            QMessageBox.information(self, "Xuất cấu hình", msg)
        except Exception as e:
            err = f"Lỗi khi xuất cấu hình: {e}"
            self.statusBar().showMessage(err, 4000)
            self._on_log(f"[Cấu hình] {err}")
            QMessageBox.critical(self, "Lỗi xuất cấu hình", err)

    def _import_config(self):
        """Import settings from a user-selected JSON file and update all UI controls."""
        try:
            path, _ = QFileDialog.getOpenFileName(
                self,
                "Nhập cấu hình Tool CoCAttack",
                "",
                "JSON Files (*.json);;All Files (*)"
            )
            if not path:
                return
            with open(path, "r", encoding="utf-8") as f:
                imported = json.load(f)
            if not isinstance(imported, dict):
                QMessageBox.warning(self, "Nhập cấu hình", "Tệp đã chọn không đúng định dạng JSON cấu hình hợp lệ.")
                return

            old_startup = self.settings.get('ui_startup_mode', 'off')
            requested_startup = imported.get('ui_startup_mode', old_startup)
            if requested_startup not in STARTUP_MODES:
                requested_startup = old_startup
            if requested_startup != old_startup:
                try:
                    configure_startup(requested_startup)
                except Exception as startup_error:
                    requested_startup = old_startup
                    self._on_log(f'[Cấu hình] Chưa áp dụng tự khởi động Windows: {startup_error}')
            imported['ui_startup_mode'] = requested_startup
            self.settings.data.update(imported)
            self.settings.save()
            self._apply_settings_to_ui()

            msg = f"Đã nạp thành công cấu hình từ:\n{path}\n\nToàn bộ thông số trên giao diện đã được cập nhật!"
            self.statusBar().showMessage(f"Đã nhập cấu hình từ {os.path.basename(path)}.", 4000)
            self._on_log(f"[Cấu hình] Đã nhập cấu hình từ tệp: {path}")
            QMessageBox.information(self, "Nhập cấu hình", msg)
        except Exception as e:
            err = f"Lỗi khi nhập cấu hình: {e}"
            self.statusBar().showMessage(err, 4000)
            self._on_log(f"[Cấu hình] {err}")
            QMessageBox.critical(self, "Lỗi nhập cấu hình", err)

    def _apply_settings_to_ui(self):
        """Update all UI controls across all tabs to reflect the current self.settings."""
        if hasattr(self, 'combo_text_size'):
            self.combo_text_size.blockSignals(True)
            self.combo_text_size.setCurrentIndex(max(0, self.combo_text_size.findData(
                self.settings.get('ui_text_size', 'medium'))))
            self.combo_text_size.blockSignals(False)
            self._apply_appearance()
        if hasattr(self, 'combo_startup_mode'):
            self.combo_startup_mode.blockSignals(True)
            self.combo_startup_mode.setCurrentIndex(max(0, self.combo_startup_mode.findData(
                self.settings.get('ui_startup_mode', 'off'))))
            self.combo_startup_mode.blockSignals(False)
        if hasattr(self, 'combo_close_behavior'):
            self.combo_close_behavior.blockSignals(True)
            self.combo_close_behavior.setCurrentIndex(max(0, self.combo_close_behavior.findData(
                self.settings.get('ui_close_behavior', 'quit'))))
            self.combo_close_behavior.blockSignals(False)
        # 1. Top bar & general
        if hasattr(self, 'chk_main_village'):
            self.chk_main_village.blockSignals(True)
            self.chk_main_village.setChecked(self.settings.get("main_village_enabled", True))
            self.chk_main_village.blockSignals(False)
            for group in getattr(self, '_main_village_groups', ()):
                group.setEnabled(self.chk_main_village.isChecked())

        # 2. Loot & Scouting
        if hasattr(self, 'spin_gold'):
            self.spin_gold.setValue(self.settings.get("min_gold", 600000))
        if hasattr(self, 'spin_elixir'):
            self.spin_elixir.setValue(self.settings.get("min_elixir", 600000))
        if hasattr(self, 'spin_dark'):
            self.spin_dark.setValue(self.settings.get("min_dark_elixir", 4000))
        if hasattr(self, 'spin_skips'):
            self.spin_skips.setValue(self.settings.get("max_search_skips", 50))
        if hasattr(self, 'combo_loot_filter'):
            filter_modes = ['legacy', 'all', 'any']
            mode = self.settings.get("loot_filter_mode", "legacy")
            if mode in filter_modes:
                self.combo_loot_filter.setCurrentIndex(filter_modes.index(mode))
        if hasattr(self, 'loot_filter_checks'):
            for key in ('gold', 'elixir', 'dark_elixir'):
                if key in self.loot_filter_checks:
                    self.loot_filter_checks[key].setChecked(self.settings.get(f'loot_filter_{key}', True if key != 'dark_elixir' else False))
        if hasattr(self, 'chk_trophy'):
            self.chk_trophy.setChecked(self.settings.get("trophy_drop_mode", False))
        if hasattr(self, 'spin_max_trophies'):
            self.spin_max_trophies.setValue(self.settings.get("max_trophies", 3200))

        # 3. Village Care
        if hasattr(self, 'chk_assign_apprentice'):
            self.chk_assign_apprentice.setChecked(self.settings.get("auto_assign_apprentice", False))
        if hasattr(self, 'chk_collect'):
            self.chk_collect.setChecked(self.settings.get("auto_collect", True))
        if hasattr(self, 'spin_collect_interval'):
            self.spin_collect_interval.setValue(self.settings.get("collect_interval_min", 60))
        if hasattr(self, 'chk_train'):
            self.chk_train.setChecked(self.settings.get("auto_train_army", True))
        if hasattr(self, 'chk_request'):
            self.chk_request.setChecked(self.settings.get("auto_request_troops", False))
        if hasattr(self, 'spin_train_slot'):
            self.spin_train_slot.setValue(self.settings.get("quick_train_slot", 1))
        if hasattr(self, 'spin_maintenance'):
            self.spin_maintenance.setValue(self.settings.get("maintenance_interval_sec", 45))
        if hasattr(self, 'spin_reserve_builders'):
            self.spin_reserve_builders.setValue(self.settings.get("reserve_builders", 1))

        # 4. Treasury
        if hasattr(self, 'chk_treasury'):
            self.chk_treasury.setChecked(self.settings.get("auto_collect_treasury", False))
        if hasattr(self, 'chk_treasury_star'):
            self.chk_treasury_star.setChecked(self.settings.get("treasury_star_bonus_enabled", True))
        if hasattr(self, 'chk_treasury_war'):
            self.chk_treasury_war.setChecked(self.settings.get("treasury_war_rewards_enabled", True))
        if hasattr(self, 'spin_treasury_interval'):
            self.spin_treasury_interval.setValue(self.settings.get("treasury_check_interval_min", 600))

        # 5. Walls & Farm Finish
        if hasattr(self, 'chk_walls'):
            self.chk_walls.setChecked(self.settings.get("auto_upgrade_walls", True))
        if hasattr(self, 'combo_wall_res'):
            wall_keys = ['both', 'gold', 'elixir']
            w_res = self.settings.get("wall_resource", "both")
            if w_res in wall_keys:
                self.combo_wall_res.setCurrentIndex(wall_keys.index(w_res))
        if hasattr(self, 'spin_wall_gold'):
            self.spin_wall_gold.setValue(self.settings.get("min_gold_to_upgrade_wall", 4000000))
        if hasattr(self, 'spin_wall_elixir'):
            self.spin_wall_elixir.setValue(self.settings.get("min_elixir_to_upgrade_wall", 4000000))
        if hasattr(self, 'chk_wall_lowest'):
            self.chk_wall_lowest.setChecked(self.settings.get("wall_lowest_first", True))
        if hasattr(self, 'combo_wall_quantity_mode'):
            self.combo_wall_quantity_mode.setCurrentIndex(1 if self.settings.get("wall_quantity_button_mode") == 'fixed' else 0)
        if hasattr(self, 'spin_wall_fixed_gap'):
            self.spin_wall_fixed_gap.setValue(self.settings.get("wall_quantity_fixed_gap_ms", 120))
        if hasattr(self, 'chk_wall_hold'):
            self.chk_wall_hold.setChecked(self.settings.get("wall_hold_enabled", True))
        if hasattr(self, 'chk_farm_finish'):
            self.chk_farm_finish.setChecked(self.settings.get("farm_finish_enabled", False))
        if hasattr(self, 'chk_farm_finish_close'):
            self.chk_farm_finish_close.setChecked(self.settings.get("farm_finish_close_app", False))
        if hasattr(self, 'chk_farm_finish_wait_builders'):
            self.chk_farm_finish_wait_builders.setChecked(self.settings.get("farm_finish_wait_builders", True))
        self._update_farm_finish_badge()
        if hasattr(self, 'spin_farm_finish_percent'):
            self.spin_farm_finish_percent.setValue(self.settings.get("farm_finish_target_percent", 95))
        if hasattr(self, 'combo_farm_finish_resources'):
            finish_modes = ['gold_elixir', 'gold', 'elixir']
            f_res = self.settings.get("farm_finish_resources", "gold_elixir")
            if f_res in finish_modes:
                self.combo_farm_finish_resources.setCurrentIndex(finish_modes.index(f_res))

        # Refresh account-specific controls without overwriting settings while
        # the individual checkboxes are being synchronized.
        if hasattr(self, 'chk_buy_new'):
            self.chk_buy_new.setChecked(self.settings.get('auto_buy_new_buildings', False))
            self.chk_upgrade_army.setChecked(self.settings.get('auto_upgrade_army', False))
            for key, check in self.army_resource_checks.items():
                check.blockSignals(True)
                check.setChecked(self.settings.get('army_upgrade_resources', {}).get(key, True))
                check.blockSignals(False)
            for key, check in self.army_type_checks.items():
                check.blockSignals(True)
                check.setChecked(self.settings.get('army_upgrade_types', {}).get(key, True))
                check.blockSignals(False)
            index = self.army_priority.findData(self.settings.get('army_upgrade_priority', 'suggested'))
            if index >= 0:
                self.army_priority.setCurrentIndex(index)

        # 6. Buildings
        if hasattr(self, 'chk_auto_buildings'):
            self.chk_auto_buildings.setChecked(self.settings.get('auto_upgrade_buildings', False))
        if hasattr(self, 'chk_lock_townhall'):
            self.chk_lock_townhall.setChecked(self.settings.get('lock_townhall_upgrade', True))
        if hasattr(self, 'chk_buildings'):
            self.chk_buildings.setChecked(self.settings.get("auto_upgrade_buildings", False))
        if hasattr(self, 'chk_lock_th'):
            self.chk_lock_th.setChecked(self.settings.get("lock_townhall_upgrade", True))
        if hasattr(self, 'combo_building_priority'):
            idx = self.combo_building_priority.findData(self.settings.get("building_upgrade_priority", "time_min"))
            if idx >= 0:
                self.combo_building_priority.setCurrentIndex(idx)
        if hasattr(self, 'combo_building_wait_mode'):
            idx = self.combo_building_wait_mode.findData(self.settings.get("building_upgrade_wait_mode", "target_cost"))
            if idx >= 0:
                self.combo_building_wait_mode.setCurrentIndex(idx)
        if hasattr(self, 'chk_building_menu_filter'):
            self.chk_building_menu_filter.setChecked(self.settings.get("building_upgrade_use_menu_filter", True))
        if hasattr(self, 'chk_bldg_resources'):
            res_dict = self.settings.get("building_upgrade_resources", {})
            for k, chk in self.chk_bldg_resources.items():
                chk.setChecked(res_dict.get(k, True))
        if hasattr(self, 'chk_bldg_categories'):
            cat_dict = self.settings.get("building_upgrade_categories", {})
            for k, chk in self.chk_bldg_categories.items():
                chk.setChecked(cat_dict.get(k, True))
        if hasattr(self, 'building_checks'):
            wl_dict = self.settings.get("building_upgrade_whitelist", {})
            for k, chk in self.building_checks.items():
                chk.setChecked(wl_dict.get(k, True))
        if hasattr(self, '_update_building_selection_summary'):
            self._update_building_selection_summary()
        if hasattr(self, '_update_building_options'):
            self._update_building_options()

        # 7. Combat & Strategy
        if hasattr(self, 'combo_strategy'):
            idx = self.combo_strategy.findData(self.settings.get("attack_strategy", "random_mix"))
            if idx >= 0:
                self.combo_strategy.setCurrentIndex(idx)
        if hasattr(self, 'spin_spread'):
            self.spin_spread.setValue(self.settings.get("attack_spread_points", 12))
        if hasattr(self, 'chk_ultra_fixed'):
            self.chk_ultra_fixed.setChecked(self.settings.get("ultra_fixed_positions", False))
        if hasattr(self, 'spin_ultra_controls'):
            for key in ('ultra_drop_gap_ms', 'ultra_card_select_ms', 'ultra_spell_inward_pct', 'ultra_spell_max_lead_px'):
                if key in self.spin_ultra_controls:
                    self.spin_ultra_controls[key].setValue(self.settings.get(key, DEFAULT_CONFIG.get(key, 20)))
        if hasattr(self, 'chk_king'):
            self.chk_king.setChecked(self.settings.get("use_king", True))
        if hasattr(self, 'chk_queen'):
            self.chk_queen.setChecked(self.settings.get("use_queen", True))
        if hasattr(self, 'chk_warden'):
            self.chk_warden.setChecked(self.settings.get("use_warden", True))
        if hasattr(self, 'chk_champ'):
            self.chk_champ.setChecked(self.settings.get("use_champion", True))
        if hasattr(self, 'chk_hero_extras'):
            for name, key in (('prince', 'use_prince'), ('luke', 'use_luke')):
                if name in self.chk_hero_extras:
                    self.chk_hero_extras[name].setChecked(self.settings.get(key, True))
        if hasattr(self, 'chk_abilities'):
            self.chk_abilities.setChecked(self.settings.get("trigger_hero_abilities", True))
        if hasattr(self, 'spin_ability_delay'):
            self.spin_ability_delay.setValue(self.settings.get("hero_ability_delay_sec", 5))

        # 8. Surrender & End conditions
        if hasattr(self, 'chk_auto_surr'):
            self.chk_auto_surr.setChecked(self.settings.get("auto_surrender", True))
        if hasattr(self, 'chk_end_stars'):
            self.chk_end_stars.setChecked(self.settings.get("end_on_stars", True))
        if hasattr(self, 'combo_star_source'):
            sources = ['all', 'town_hall', 'destruction']
            src = self.settings.get("end_star_source", "all")
            if src in sources:
                self.combo_star_source.setCurrentIndex(sources.index(src))
        if hasattr(self, 'spin_stars'):
            self.spin_stars.setValue(self.settings.get("star_target", 1))
        if hasattr(self, 'chk_end_time'):
            self.chk_end_time.setChecked(self.settings.get("end_on_time", False))
        if hasattr(self, 'spin_timeout'):
            self.spin_timeout.setValue(self.settings.get("battle_timeout_sec", 180))
        if hasattr(self, 'chk_end_loot'):
            self.chk_end_loot.setChecked(self.settings.get("end_on_loot", False))
        if hasattr(self, 'combo_end_loot_res'):
            modes = ['gold_elixir', 'gold', 'elixir']
            lr = self.settings.get("loot_target_resource", "gold_elixir")
            if lr in modes:
                self.combo_end_loot_res.setCurrentIndex(modes.index(lr))
        if hasattr(self, 'spin_end_loot_percent'):
            self.spin_end_loot_percent.setValue(self.settings.get("loot_target_percent", 50))
        if hasattr(self, 'chk_end_destr'):
            self.chk_end_destr.setChecked(self.settings.get("end_on_destruction", False))
        if hasattr(self, 'spin_end_destr_percent'):
            self.spin_end_destr_percent.setValue(self.settings.get("destruction_target_percent", 50))
        if hasattr(self, 'combo_end_condition_mode'):
            self.combo_end_condition_mode.setCurrentIndex(1 if self.settings.get("end_condition_mode") == 'all' else 0)
        if hasattr(self, 'combo_resource_fallback'):
            idx = self.combo_resource_fallback.findData(self.settings.get("resource_attack_fallback", "perimeter"))
            if idx >= 0:
                self.combo_resource_fallback.setCurrentIndex(idx)

        # 9. Builder Base
        if hasattr(self, 'chk_builder_village'):
            self.chk_builder_village.setChecked(self.settings.get("builder_base_enabled", False))
            self._update_builder_options(self.chk_builder_village.isChecked())
        if hasattr(self, 'spin_builder_interval'):
            self.spin_builder_interval.setValue(self.settings.get("builder_base_interval_mins", 30))
        if hasattr(self, 'builder_tasks'):
            for key, _ in BuilderBaseManager.OPTIONS:
                if key in self.builder_tasks:
                    self.builder_tasks[key].setChecked(self.settings.get(key, False))
        if hasattr(self, 'builder_wall_thresholds'):
            for resource in ("gold", "elixir"):
                key = f"builder_base_wall_min_{resource}"
                if key in self.builder_wall_thresholds:
                    self.builder_wall_thresholds[key].setValue(self.settings.get(key, 0))
        if hasattr(self, 'combo_builder_wall_trigger'):
            self.combo_builder_wall_trigger.setCurrentIndex(max(0, self.combo_builder_wall_trigger.findData(
                self.settings.get('builder_base_wall_trigger', 'either'))))
        if hasattr(self, 'combo_builder_combo'):
            idx = max(0, self.combo_builder_combo.findData(self.settings.get("builder_base_combo", "current")))
            self.combo_builder_combo.setCurrentIndex(idx)
        if hasattr(self, 'combo_builder_reinf1'):
            idx = max(0, self.combo_builder_reinf1.findData(self.settings.get("builder_base_reinforcement_1", "current")))
            self.combo_builder_reinf1.setCurrentIndex(idx)
        if hasattr(self, 'combo_builder_reinf2'):
            idx = max(0, self.combo_builder_reinf2.findData(self.settings.get("builder_base_reinforcement_2", "current")))
            self.combo_builder_reinf2.setCurrentIndex(idx)

        # 10. Devices & Antiban
        if hasattr(self, 'chk_restart'):
            self.chk_restart.setChecked(self.settings.get("auto_restart_game", True))
        if hasattr(self, 'chk_reconnect'):
            self.chk_reconnect.setChecked(self.settings.get("auto_reconnect", True))
        if hasattr(self, 'chk_periodic_restart'):
            self.chk_periodic_restart.setChecked(self.settings.get("periodic_restart_enabled", True))
        if hasattr(self, 'spin_restart_interval'):
            self.spin_restart_interval.setValue(self.settings.get("periodic_restart_interval_mins", 60))
        if hasattr(self, 'device_combo'):
            serial = self.settings.get("device_serial")
            if serial:
                idx = self.device_combo.findData(serial)
                if idx >= 0:
                    self.device_combo.setCurrentIndex(idx)

        # 11. Schedule
        if hasattr(self, 'spin_timer_minutes'):
            self.spin_timer_minutes.setValue(self.settings.get("timer_minutes", 60))
        self._update_active_subtabs()

    def _on_stats(self, stats: dict):
        main_battles = stats.get('battles_completed', 0)
        builder_battles = stats.get('builder_battles_completed', 0)
        builder_stars = stats.get('builder_stars_earned', 0)
        self.card_total_battles.set_value(f'{main_battles + builder_battles:,}')
        self.card_main_total.set_value(f'{main_battles:,}')
        self.card_builder_total.set_value(f'{builder_battles:,}')
        self.card_builder_stars.set_value(f'{builder_stars:,}')
        self.card_builder_started.set_value(f"{stats.get('builder_battles_started', 0):,}")
        self.card_battles.set_value(f'{main_battles:,}')
        self.card_stars.set_value(f"{stats.get('stars_earned', 0):,}")
        self.card_gold.set_value(f"{stats.get('gold_looted', 0):,}")
        self.card_elixir.set_value(f"{stats.get('elixir_looted', 0):,}")
        self.card_dark.set_value(f"{stats.get('dark_looted', 0):,}")
        samples=stats.get('battle_duration_samples',0)
        average=stats.get('battle_duration_ms',0)/1000/samples if samples else None
        self.lbl_avg_battle.setText('TB thời gian mỗi trận: '+(f'{average:.1f} giây · {samples:,} trận có dữ liệu' if average is not None else '—'))
        measured=stats.get('fastest_50_ms',0)
        if measured:
            self.lbl_fastest_50.setText(f'Kỷ lục 50%: {measured/1000:.2f} giây')
        else:
            self.lbl_fastest_50.setText('Kỷ lục 50%: chưa có số đo')

        from core.recent_rph import rates, village_history
        recent_rates,samples=rates(village_history(stats))
        self.lbl_rph_window.setText(f'RPH · {samples}/50 trận gần nhất có dữ liệu')
        for card,key in ((self.card_gold_hour,'gold'),(self.card_elixir_hour,'elixir'),(self.card_dark_hour,'dark_elixir')):
            value=recent_rates[key]
            card.set_value(f'{value:,}' if value is not None else '—')
        self.card_attack_scan.set_value(f"{stats.get('battles_started', 0):,} / {stats.get('bases_scanned', 0):,}")
        self.card_uptime.set_value(self._format_uptime(stats.get('uptime_seconds', 0)))
        self.card_builder_uptime.set_value(self._format_uptime(stats.get('builder_uptime_seconds', 0)))

    @staticmethod
    def _format_uptime(seconds):
        mins, secs = divmod(seconds, 60)
        hrs, mins = divmod(mins, 60)
        return f"{hrs:02d}:{mins:02d}:{secs:02d}" if hrs else f"{mins:02d}:{secs:02d}"

    def _on_state(self, state: str):
        self._highlight_active_village()
        self._sync_controls()
        if state == 'ADB_DISCONNECTED':
            self.statusBar().showMessage('Mất kết nối MuMu. Mở MuMu rồi bấm Kết nối hoặc Bắt đầu.')
        if hasattr(self, 'chk_main_village'):
            self.chk_main_village.setChecked(self.settings.get("main_village_enabled", True))
        if hasattr(self, 'chk_farm_finish'):
            self.chk_farm_finish.blockSignals(True)
            self.chk_farm_finish.setChecked(self.settings.get('farm_finish_enabled', False))
            self.chk_farm_finish.blockSignals(False)
        if hasattr(self, 'chk_farm_finish_close'):
            self.chk_farm_finish_close.blockSignals(True)
            self.chk_farm_finish_close.setChecked(self.settings.get('farm_finish_close_app', False))
            self.chk_farm_finish_close.blockSignals(False)
        if hasattr(self, 'chk_farm_finish_wait_builders'):
            self.chk_farm_finish_wait_builders.blockSignals(True)
            self.chk_farm_finish_wait_builders.setChecked(self.settings.get('farm_finish_wait_builders', True))
            self.chk_farm_finish_wait_builders.blockSignals(False)
        self._update_farm_finish_badge()
        if self.bot.is_running and self.bot.is_paused:
            state = 'PAUSED'
        state_map = {
            "IDLE": ("ĐANG CHỜ", "#1E293B", "#94A3B8"),
            "VILLAGE": ("LÀNG CHÍNH", "#064E3B", "#34D399"),
            "TRAINING": ("LUYỆN QUÂN", "#1E3A8A", "#60A5FA"),
            "SEARCHING": ("ĐANG TÌM ĐỐI THỦ", "#78350F", "#FBBF24"),
            "COMBAT": ("ĐANG TẤN CÔNG", "#831843", "#F472B6"),
            "BUILDER_BASE": ("LÀNG ĐÊM", "#4C1D95", "#A78BFA"),
            "RECOVERY": ("KHÔI PHỤC GAME", "#7F1D1D", "#F87171"),
            "ADB_DISCONNECTED": ("MẤT KẾT NỐI MUMU", "#7F1D1D", "#F87171"),
            "CHỜ THỢ RẢNH": ("CHỜ THỢ RẢNH", "#1E3A8A", "#60A5FA"),
            "PAUSED": ("TẠM DỪNG", "#78350F", "#FBBF24")
        }
        text, bg, fg = state_map.get(state, (state, "#1E293B", "#94A3B8"))
        self.state_badge.setText(text)
        village = ('builder' if state == 'BUILDER_BASE' else
                   self.bot.village_state.get_snapshot().get('active_village', 'main'))
        if state == 'IDLE':
            village = 'idle'
        summary = {
            'IDLE': ('overview', 'Sẵn sàng · Chọn thiết bị và bắt đầu'),
            'VILLAGE': ('care', 'Đang kiểm tra làng chính'),
            'TRAINING': ('army', 'Đang chuẩn bị đội quân'),
            'SEARCHING': ('search', 'Đang tìm đối thủ'),
            'COMBAT': ('battle', 'Đang triển khai quân'),
            'BUILDER_BASE': ('care', 'Đang xử lý làng đêm'),
            'RECOVERY': ('device', 'Đang phục hồi game'),
            'ADB_DISCONNECTED': ('device', 'Mất kết nối MuMu'),
            'PAUSED': (self._activity_section, 'Tạm dừng · ' + self._activity_text),
        }.get(state, (self._activity_section, text))
        self._set_activity(village, *summary)
