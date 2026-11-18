"""Simple, focused PyQt5 Test Tool UI for running isolated CoCAttack flows."""
import os
import sys
import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from PyQt5.QtCore import QObject, Qt, pyqtSignal
from PyQt5.QtGui import QIcon, QTextCursor
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from bot.flow_tester import (
    FLOW_BY_ID,
    FLOW_DEFINITIONS,
    FlowTestResult,
    FlowTestRunner,
    MODE_LABELS,
)
from config.settings import (
    BUILDING_CATALOG,
    BUILDING_CATEGORY_LABELS,
    BUILDING_KEY_TO_RESOURCES,
    BotSettings,
    CATEGORY_TO_RESOURCES,
    DEFAULT_BUILDING_WHITELIST,
    DEFAULT_CONFIG,
)
from core.adb_client import ADBSocketClient
from core.vision import VisionEngine
from ui.main_window import BuildingSelectionDialog
from ui.styles import APP_ICON_ICO, APP_ICON_PNG, theme_qss
from ui.widgets import LiveScreenViewer, NoWheelComboBox, NoWheelSpinBox


class _ViewportFitScrollArea(QScrollArea):
    """Keep inner stacked settings constrained to viewport width so right edge never clips."""

    def resizeEvent(self, event):
        super().resizeEvent(event)
        inner = self.widget()
        if inner is not None:
            vw = self.viewport().width()
            if vw > 0:
                inner.setFixedWidth(vw)


class TestToolBridge(QObject):
    sig_log = pyqtSignal(str)
    sig_frame = pyqtSignal(object, str)
    sig_finished = pyqtSignal(object)
    sig_devices = pyqtSignal(object, str)
    sig_connected = pyqtSignal(bool, str)


class TestToolWindow(QMainWindow):
    """Standalone Test Tool UI for running and verifying individual CoCAttack flows."""

    def __init__(
        self,
        settings_path: Optional[str] = None,
        main_config_path: Optional[str] = None,
    ):
        super().__init__()
        root = Path(__file__).resolve().parents[1]
        self.main_config_path = Path(main_config_path) if main_config_path else (root / "config.json")
        if settings_path is None:
            runtime_dir = root / "supervision" / "runtime"
            runtime_dir.mkdir(parents=True, exist_ok=True)
            settings_path = str(runtime_dir / "test_tool_config.json")
        else:
            Path(settings_path).parent.mkdir(parents=True, exist_ok=True)

        self.settings = BotSettings(filepath=settings_path)
        if not Path(settings_path).exists() and self.main_config_path.exists():
            self._copy_from_main_config(save=True, log_msg=False)

        self.adb = ADBSocketClient()
        self.vision: Optional[VisionEngine] = None
        self._vision_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self._is_running = False
        self._closing = False
        self._connected_serial: Optional[str] = None

        self.bridge = TestToolBridge(self)
        self.bridge.sig_log.connect(self._append_log)
        self.bridge.sig_frame.connect(self._on_frame)
        self.bridge.sig_finished.connect(self._on_test_finished)
        self.bridge.sig_devices.connect(self._on_devices_scanned)
        self.bridge.sig_connected.connect(self._on_connected)

        self.setWindowTitle("CoCAttack — Công cụ Test Luồng Riêng")
        icon_file = APP_ICON_ICO if APP_ICON_ICO.is_file() else APP_ICON_PNG
        if icon_file.is_file():
            self.setWindowIcon(QIcon(str(icon_file)))
        self.resize(1280, 800)
        self.setMinimumSize(1020, 640)

        self._build_ui()
        self._apply_theme()
        self._sync_ui_from_settings()
        self._on_flow_changed(0)
        self._scan_devices()

    def _get_vision(self) -> VisionEngine:
        with self._vision_lock:
            if self.vision is None:
                self.vision = VisionEngine()
            return self.vision

    def _copy_from_main_config(self, save: bool = True, log_msg: bool = True):
        if not self.main_config_path.exists():
            if log_msg:
                self._append_log("[Cấu hình] Không tìm thấy config.json để nạp.")
            return
        try:
            data = json.loads(self.main_config_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                self.settings.data.update(data)
                if save:
                    self.settings.save()
                if log_msg:
                    self._append_log("[Cấu hình] ✅ Đã nạp toàn bộ cài đặt từ config.json vào công cụ test (tách biệt, không ghi đè config chính).")
        except Exception as exc:
            if log_msg:
                self._append_log(f"[Cấu hình] ⚠️ Không đọc được config.json: {exc}")

    # ------------------------------------------------------------------
    # UI Construction
    # ------------------------------------------------------------------
    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(10, 10, 10, 10)
        root_layout.setSpacing(8)

        # 1. Top Toolbar: Device connection + Config sync + Theme
        top_bar = QFrame()
        top_bar.setObjectName("statCard")
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(10, 8, 10, 8)
        top_layout.setSpacing(8)

        lbl_dev = QLabel("Thiết bị ADB:")
        lbl_dev.setStyleSheet("font-weight: 700;")
        self.combo_devices = NoWheelComboBox()
        self.combo_devices.setEditable(True)
        self.combo_devices.setMinimumWidth(230)
        cur_serial = self.settings.get("device_serial", "127.0.0.1:5555")
        self.combo_devices.setCurrentText(cur_serial)
        self.combo_devices.currentIndexChanged.connect(
            lambda _i: self.settings.set("device_serial", self._selected_serial())
        )
        self.combo_devices.editTextChanged.connect(
            lambda _txt: self.settings.set("device_serial", self._selected_serial())
        )

        self.btn_scan_dev = QPushButton("Quét")
        self.btn_scan_dev.clicked.connect(self._scan_devices)
        self.btn_connect = QPushButton("Kết nối")
        self.btn_connect.setObjectName("btn_primary")
        self.btn_connect.clicked.connect(self._connect_device)
        self.btn_snap = QPushButton("📸 Chụp Frame")
        self.btn_snap.clicked.connect(self._capture_preview_once)

        self.lbl_conn = QLabel("Chưa kết nối")
        self.lbl_conn.setObjectName("stateBadge")

        self.btn_load_main_cfg = QPushButton("Nạp config.json")
        self.btn_load_main_cfg.setToolTip("Nạp bản sao cài đặt từ config.json chính vào Test Tool (sửa trên Test Tool không ảnh hưởng config.json chính).")
        self.btn_load_main_cfg.clicked.connect(self._reload_from_main_config)

        self.btn_theme = QPushButton("Theme: Tối")
        self.btn_theme.clicked.connect(self._toggle_theme)

        top_layout.addWidget(lbl_dev)
        top_layout.addWidget(self.combo_devices)
        top_layout.addWidget(self.btn_scan_dev)
        top_layout.addWidget(self.btn_connect)
        top_layout.addWidget(self.btn_snap)
        top_layout.addWidget(self.lbl_conn)
        top_layout.addStretch(1)
        top_layout.addWidget(self.btn_load_main_cfg)
        top_layout.addWidget(self.btn_theme)
        root_layout.addWidget(top_bar)

        # 2. Main Split: Left (Flow & Settings) | Right (Result + Preview + Log)
        main_split = QSplitter(Qt.Horizontal)
        main_split.setChildrenCollapsible(False)

        # --- LEFT COLUMN ---
        left_panel = QWidget()
        left_panel.setMinimumWidth(520)
        left_layout = QVBoxLayout(left_panel)
        left_layout.setContentsMargins(0, 0, 4, 0)
        left_layout.setSpacing(8)

        grp_flow = QGroupBox("1. Chọn luồng test và Chế độ giả lập")
        flow_layout = QVBoxLayout(grp_flow)
        flow_layout.setSpacing(8)

        form_flow = QFormLayout()
        form_flow.setSpacing(6)
        form_flow.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)

        self.combo_flow = NoWheelComboBox()
        self.combo_flow.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        for item in FLOW_DEFINITIONS:
            self.combo_flow.addItem(item["name"], item["id"])
        self.combo_flow.currentIndexChanged.connect(self._on_flow_changed)
        form_flow.addRow("Luồng test:", self.combo_flow)

        self.combo_mode = NoWheelComboBox()
        self.combo_mode.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.combo_mode.currentIndexChanged.connect(self._on_mode_changed)
        form_flow.addRow("Chế độ:", self.combo_mode)

        flow_layout.addLayout(form_flow)

        self.lbl_flow_desc = QLabel("")
        self.lbl_flow_desc.setWordWrap(True)
        self.lbl_flow_desc.setObjectName("sectionDetail")
        flow_layout.addWidget(self.lbl_flow_desc)

        action_row = QHBoxLayout()
        action_row.setSpacing(8)
        self.btn_run = QPushButton("▶ Chạy Test")
        self.btn_run.setObjectName("btn_start")
        self.btn_run.clicked.connect(self._start_test)

        self.btn_stop = QPushButton("⏹ Dừng")
        self.btn_stop.setObjectName("btn_stop")
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self._stop_test)

        action_row.addWidget(self.btn_run, 2)
        action_row.addWidget(self.btn_stop, 1)
        flow_layout.addLayout(action_row)
        left_layout.addWidget(grp_flow)

        # Scrollable Settings Stack
        grp_settings = QGroupBox("2. Cài đặt riêng cho luồng đang chọn")
        grp_settings_layout = QVBoxLayout(grp_settings)
        grp_settings_layout.setContentsMargins(6, 8, 6, 6)

        scroll = _ViewportFitScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        self.settings_stack = QStackedWidget()
        self._page_index_by_flow: Dict[str, int] = {}
        self._build_flow_setting_pages()

        scroll.setWidget(self.settings_stack)
        grp_settings_layout.addWidget(scroll)
        left_layout.addWidget(grp_settings, 1)

        # --- RIGHT COLUMN ---
        right_panel = QWidget()
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(4, 0, 0, 0)
        right_layout.setSpacing(8)

        # Result Banner Card
        self.result_card = QFrame()
        self.result_card.setObjectName("statCard")
        res_layout = QVBoxLayout(self.result_card)
        res_layout.setContentsMargins(12, 10, 12, 10)
        res_layout.setSpacing(4)

        res_top = QHBoxLayout()
        self.lbl_res_title = QLabel("Kết quả Test:")
        self.lbl_res_title.setStyleSheet("font-weight: 700;")
        self.lbl_res_badge = QLabel("CHƯA CHẠY")
        self.lbl_res_badge.setObjectName("stateBadge")
        self.lbl_res_elapsed = QLabel("")
        self.lbl_res_elapsed.setObjectName("sectionDetail")
        res_top.addWidget(self.lbl_res_title)
        res_top.addWidget(self.lbl_res_badge)
        res_top.addStretch(1)
        res_top.addWidget(self.lbl_res_elapsed)
        res_layout.addLayout(res_top)

        self.lbl_res_summary = QLabel("Chọn luồng test, điều chỉnh setting bên trái rồi bấm '▶ Chạy Test'.")
        self.lbl_res_summary.setWordWrap(True)
        res_layout.addWidget(self.lbl_res_summary)
        right_layout.addWidget(self.result_card)

        # Preview + Detailed Log Splitter
        v_split = QSplitter(Qt.Vertical)
        v_split.setChildrenCollapsible(False)

        preview_wrap = QWidget()
        pv_layout = QVBoxLayout(preview_wrap)
        pv_layout.setContentsMargins(0, 0, 0, 0)
        pv_layout.setSpacing(4)
        self.lbl_frame_caption = QLabel("Xem trước màn hình giả lập")
        self.lbl_frame_caption.setObjectName("sectionDetail")
        self.viewer = LiveScreenViewer()
        self.viewer.setMinimumSize(360, 200)
        pv_layout.addWidget(self.lbl_frame_caption)
        pv_layout.addWidget(self.viewer, 1)
        v_split.addWidget(preview_wrap)

        log_wrap = QWidget()
        log_layout = QVBoxLayout(log_wrap)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(4)

        log_header = QHBoxLayout()
        lbl_log = QLabel("Nhật ký chi tiết từng bước (Log)")
        lbl_log.setObjectName("sectionDetail")
        self.chk_autoscroll = QCheckBox("Tự cuộn")
        self.chk_autoscroll.setChecked(True)
        btn_copy_log = QPushButton("Copy Log")
        btn_copy_log.setProperty("variant", "reset")
        btn_copy_log.clicked.connect(self._copy_logs)
        btn_save_log = QPushButton("Lưu Log")
        btn_save_log.setProperty("variant", "reset")
        btn_save_log.clicked.connect(self._save_logs)
        btn_clear_log = QPushButton("Xóa Log")
        btn_clear_log.setProperty("variant", "reset")
        btn_clear_log.clicked.connect(lambda: self.txt_log.clear())

        log_header.addWidget(lbl_log)
        log_header.addStretch(1)
        log_header.addWidget(self.chk_autoscroll)
        log_header.addWidget(btn_copy_log)
        log_header.addWidget(btn_save_log)
        log_header.addWidget(btn_clear_log)
        log_layout.addLayout(log_header)

        self.txt_log = QPlainTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setMaximumBlockCount(4000)
        log_layout.addWidget(self.txt_log, 1)
        v_split.addWidget(log_wrap)
        v_split.setSizes([270, 360])

        right_layout.addWidget(v_split, 1)

        main_split.addWidget(left_panel)
        main_split.addWidget(right_panel)
        main_split.setSizes([550, 710])
        root_layout.addWidget(main_split, 1)

    def _build_flow_setting_pages(self):
        # 1. collect_resources
        p_col = QWidget()
        l_col = QVBoxLayout(p_col)
        self.chk_col_popups = QCheckBox("Tự đóng popup cản trở trước khi thu hoạch")
        self.chk_col_popups.setChecked(True)
        self.chk_col_zoom = QCheckBox("Tự zoom out bản đồ nếu chưa thấy mỏ (chế độ chỉ quét)")
        self.chk_col_zoom.setChecked(True)
        self.chk_col_annotate = QCheckBox("Vẽ vòng tròn nhận diện mỏ lên màn hình xem trước")
        self.chk_col_annotate.setChecked(True)
        note_col = QLabel("Chạy riêng phần thu hoạch mỏ Vàng / Dầu / Dầu đen từ đầu đến khi xác minh xong (bỏ qua cooldown).")
        note_col.setWordWrap(True)
        note_col.setObjectName("sectionDetail")
        l_col.addWidget(self.chk_col_popups)
        l_col.addWidget(self.chk_col_zoom)
        l_col.addWidget(self.chk_col_annotate)
        l_col.addWidget(note_col)
        l_col.addStretch(1)
        self._add_setting_page("collect_resources", p_col)

        # 2. check_max_storage
        p_cap = QWidget()
        f_cap = QFormLayout(p_cap)
        self.combo_cap_res = NoWheelComboBox()
        self.combo_cap_res.addItem("Vàng và Dầu hồng", "gold_elixir")
        self.combo_cap_res.addItem("Chỉ Vàng", "gold")
        self.combo_cap_res.addItem("Chỉ Dầu hồng", "elixir")
        self.combo_cap_res.addItem("Cả 3 (Vàng, Dầu hồng, Dầu đen)", "all_three")
        f_cap.addRow("Tài nguyên kiểm tra:", self.combo_cap_res)

        self.spin_cap_pct = NoWheelSpinBox()
        self.spin_cap_pct.setRange(90, 100)
        self.spin_cap_pct.setSuffix(" %")
        self.spin_cap_pct.setValue(int(self.settings.get("farm_finish_target_percent", 100)))
        f_cap.addRow("Mục tiêu kho thật:", self.spin_cap_pct)

        self.chk_cap_bypass_cache = QCheckBox("Bỏ qua cache (đọc OCR popup mới)")
        self.chk_cap_bypass_cache.setToolTip("Ép bấm thanh tài nguyên trên HUD để đọc OCR popup mới thay vì dùng giá trị nhớ tạm.")
        self.chk_cap_bypass_cache.setChecked(True)
        self.chk_cap_verify_acc = QCheckBox("Xác minh mã người chơi (#tag) qua Hồ sơ")
        self.chk_cap_verify_acc.setChecked(True)
        self.chk_cap_verify_twice = QCheckBox("Chạy kiểm tra FarmFinish 2 lần đọc liên tiếp")
        self.chk_cap_verify_twice.setChecked(True)
        f_cap.addRow(self.chk_cap_bypass_cache)
        f_cap.addRow(self.chk_cap_verify_acc)
        f_cap.addRow(self.chk_cap_verify_twice)
        self._add_setting_page("check_max_storage", p_cap)

        # 3. upgrade_buildings (FULL SETTINGS!)
        p_bldg = QWidget()
        v_bldg = QVBoxLayout(p_bldg)
        v_bldg.setContentsMargins(2, 2, 2, 2)
        v_bldg.setSpacing(8)
        f_bldg = QFormLayout()
        f_bldg.setSpacing(6)

        self.chk_auto_buildings = QCheckBox("Bật nâng công trình theo tùy chọn")
        self.chk_auto_buildings.setToolTip("auto_upgrade_buildings")
        self.chk_auto_buildings.toggled.connect(lambda v: self.settings.set("auto_upgrade_buildings", v))
        f_bldg.addRow(self.chk_auto_buildings)

        self.chk_lock_th = QCheckBox("🛡️ Cấm nâng Nhà chính (Town Hall)")
        self.chk_lock_th.setToolTip("lock_townhall_upgrade: Tuyệt đối không nâng Town Hall.")
        self.chk_lock_th.toggled.connect(lambda v: self.settings.set("lock_townhall_upgrade", v))
        f_bldg.addRow(self.chk_lock_th)

        self.spin_reserve_builders = NoWheelSpinBox()
        self.spin_reserve_builders.setRange(0, 6)
        self.spin_reserve_builders.valueChanged.connect(lambda v: self.settings.set("reserve_builders", v))
        f_bldg.addRow("Số thợ rảnh cần duy trì:", self.spin_reserve_builders)

        self.chk_bldg_walls_maxed = QCheckBox("Giả lập Tường đã Max (dùng hết thợ)")
        self.chk_bldg_walls_maxed.setToolTip("Giả lập trạng thái Tường đã Max (duy trì 0 thợ).")
        self.chk_bldg_walls_maxed.setChecked(False)
        f_bldg.addRow(self.chk_bldg_walls_maxed)

        self.combo_bldg_priority = NoWheelComboBox()
        for key, label in (
            ("time_min", "Ít thời gian nhất trước"),
            ("cost_min", "Chi phí nhỏ nhất (rẻ nhất) trước"),
            ("cost_max", "Chi phí đắt nhất trước"),
            ("time_max", "Nhiều thời gian nhất trước"),
            ("suggested", "Theo gợi ý mặc định (Suggested)"),
            ("specific", "🎯 Cụ thể: Chỉ nâng 1 công trình chỉ định"),
        ):
            self.combo_bldg_priority.addItem(label, key)
        self.combo_bldg_priority.currentIndexChanged.connect(self._on_bldg_priority_changed)
        f_bldg.addRow("Thứ tự ưu tiên:", self.combo_bldg_priority)

        self.lbl_bldg_spec = QLabel("Công trình cụ thể:")
        self.combo_bldg_specific = NoWheelComboBox()
        self.combo_bldg_specific.addItem("-- Chọn công trình mục tiêu --", "")
        cat_icons = {"traps": "💣", "resource": "🌾", "defense": "🛡️", "army": "⚔️", "heroes": "👑"}
        for cat_k, _ in BUILDING_CATEGORY_LABELS:
            c_icon = cat_icons.get(cat_k, "🔹")
            for b_k, b_en, b_vi in BUILDING_CATALOG.get(cat_k, ()):
                if b_k == "townhall":
                    continue
                self.combo_bldg_specific.addItem(f"{c_icon} {b_vi} ({b_en})", b_k)
        self.combo_bldg_specific.currentIndexChanged.connect(
            lambda _: self.settings.set("building_upgrade_specific_target", self.combo_bldg_specific.currentData())
        )
        f_bldg.addRow(self.lbl_bldg_spec, self.combo_bldg_specific)

        self.combo_bldg_wait = NoWheelComboBox()
        self.combo_bldg_wait.addItem("Đủ tiền mục tiêu (target_cost)", "target_cost")
        self.combo_bldg_wait.addItem("Đợi đầy kho (full_storage)", "full_storage")
        self.combo_bldg_wait.currentIndexChanged.connect(self._on_bldg_wait_changed)
        f_bldg.addRow("Khi thiếu tài nguyên:", self.combo_bldg_wait)

        self.spin_bldg_full_pct = NoWheelSpinBox()
        self.spin_bldg_full_pct.setRange(90, 100)
        self.spin_bldg_full_pct.setSuffix(" %")
        self.spin_bldg_full_pct.valueChanged.connect(lambda v: self.settings.set("farm_finish_target_percent", v))
        f_bldg.addRow("Mục tiêu kho (full_storage):", self.spin_bldg_full_pct)

        self.chk_bldg_menu_filter = QCheckBox("Dùng bộ lọc nhanh của Builder Menu (Resource / Cost)")
        self.chk_bldg_menu_filter.toggled.connect(lambda v: self.settings.set("building_upgrade_use_menu_filter", v))
        f_bldg.addRow(self.chk_bldg_menu_filter)

        self.chk_bldg_supercharge = QCheckBox("Cho phép nâng cấp Supercharge (tia sét xanh)")
        self.chk_bldg_supercharge.toggled.connect(lambda v: self.settings.set("building_upgrade_allow_supercharge", v))
        f_bldg.addRow(self.chk_bldg_supercharge)

        res_row = QHBoxLayout()
        self.chk_bldg_res: Dict[str, QCheckBox] = {}
        for r_key, r_lbl in (("gold", "Vàng"), ("elixir", "Dầu hồng"), ("dark_elixir", "Dầu đen")):
            chk = QCheckBox(r_lbl)
            chk.toggled.connect(self._save_bldg_resources)
            self.chk_bldg_res[r_key] = chk
            res_row.addWidget(chk)
        res_row.addStretch(1)
        f_bldg.addRow("Loại tài nguyên:", res_row)

        cat_grid = QGridLayout()
        self.chk_bldg_cats: Dict[str, QCheckBox] = {}
        for idx, (c_key, c_lbl) in enumerate(BUILDING_CATEGORY_LABELS):
            chk = QCheckBox(c_lbl.replace("&", "&&"))
            chk.toggled.connect(self._save_bldg_categories)
            self.chk_bldg_cats[c_key] = chk
            cat_grid.addWidget(chk, idx // 2, idx % 2)
        f_bldg.addRow("Nhóm công trình:", cat_grid)

        self.combo_bldg_preset = NoWheelComboBox()
        self.combo_bldg_preset.addItem("-- Áp dụng mẫu chọn nhanh --", None)
        self.combo_bldg_preset.addItem("⚡ Tất cả công trình", "all")
        self.combo_bldg_preset.addItem("🛡️ Chỉ Phòng thủ & Bẫy", "defense_traps")
        self.combo_bldg_preset.addItem("🌾 Chỉ Kinh tế (Mỏ & Kho)", "resource")
        self.combo_bldg_preset.addItem("⚔️ Chỉ Binh lính & Tướng", "army_heroes")
        self.combo_bldg_preset.addItem("🟣 Chỉ công trình Dầu hồng", "elixir_focus")
        self.combo_bldg_preset.addItem("🟡 Chỉ công trình Vàng", "gold_focus")
        self.combo_bldg_preset.currentIndexChanged.connect(self._on_bldg_preset)
        f_bldg.addRow("Mẫu chọn nhanh:", self.combo_bldg_preset)

        wl_row = QHBoxLayout()
        wl_row.setSpacing(6)
        self.btn_bldg_whitelist = QPushButton("🛠️ Chọn chi tiết...")
        self.btn_bldg_whitelist.clicked.connect(self._open_bldg_whitelist_dialog)
        self.btn_bldg_enable_cats = QPushButton("✅ Tích trong nhóm")
        self.btn_bldg_enable_cats.setToolTip("Bật tất cả công trình trong danh sách chi tiết thuộc các nhóm và tài nguyên đang tích ở trên.")
        self.btn_bldg_enable_cats.clicked.connect(self._enable_whitelist_for_active_categories)
        self.btn_bldg_reset = QPushButton("Mặc định")
        self.btn_bldg_reset.setProperty("variant", "reset")
        self.btn_bldg_reset.clicked.connect(self._reset_bldg_defaults)
        wl_row.addWidget(self.btn_bldg_whitelist, 1)
        wl_row.addWidget(self.btn_bldg_enable_cats)
        wl_row.addWidget(self.btn_bldg_reset)
        f_bldg.addRow("Danh sách chi tiết:", wl_row)

        self.lbl_bldg_summary = QLabel("")
        self.lbl_bldg_summary.setWordWrap(True)
        self.lbl_bldg_summary.setObjectName("sectionDetail")
        f_bldg.addRow("Đang chọn:", self.lbl_bldg_summary)

        v_bldg.addLayout(f_bldg)

        grp_sim = QGroupBox("Tùy chọn Test Giả lập (không sửa config chính)")
        v_sim = QVBoxLayout(grp_sim)
        self.chk_bldg_virtual_rich = QCheckBox("Giả lập ví vô hạn 99M (chỉ quét)")
        self.chk_bldg_virtual_rich.setToolTip("Test logic lọc/xếp hạng ngay cả khi đang thiếu tiền (chỉ áp dụng chế độ Chỉ quét).")
        self.chk_bldg_force_scan = QCheckBox("Vẫn quét menu khi hết thợ rảnh (chỉ quét)")
        self.chk_bldg_ignore_wait = QCheckBox("Bỏ qua đợi đầy kho ở full_storage (chỉ quét)")
        v_sim.addWidget(self.chk_bldg_virtual_rich)
        v_sim.addWidget(self.chk_bldg_force_scan)
        v_sim.addWidget(self.chk_bldg_ignore_wait)
        v_bldg.addWidget(grp_sim)
        v_bldg.addStretch(1)
        self._add_setting_page("upgrade_buildings", p_bldg)

        # 4. upgrade_walls
        p_wall = QWidget()
        f_wall = QFormLayout(p_wall)
        self.combo_wall_res = NoWheelComboBox()
        self.combo_wall_res.addItem("Vàng hoặc dầu", "both")
        self.combo_wall_res.addItem("Chỉ Vàng", "gold")
        self.combo_wall_res.addItem("Chỉ Dầu hồng", "elixir")
        self.combo_wall_res.currentIndexChanged.connect(
            lambda _: self.settings.set("wall_resource", self.combo_wall_res.currentData())
        )
        f_wall.addRow("Tài nguyên nâng tường:", self.combo_wall_res)

        self.spin_wall_gold = NoWheelSpinBox()
        self.spin_wall_gold.setRange(1_000_000, 35_000_000)
        self.spin_wall_gold.setSingleStep(500_000)
        self.spin_wall_gold.valueChanged.connect(lambda v: self.settings.set("min_gold_to_upgrade_wall", v))
        f_wall.addRow("Ngưỡng vàng:", self.spin_wall_gold)

        self.spin_wall_elixir = NoWheelSpinBox()
        self.spin_wall_elixir.setRange(0, 35_000_000)
        self.spin_wall_elixir.setSingleStep(500_000)
        self.spin_wall_elixir.valueChanged.connect(lambda v: self.settings.set("min_elixir_to_upgrade_wall", v))
        f_wall.addRow("Ngưỡng dầu:", self.spin_wall_elixir)

        self.chk_wall_lowest = QCheckBox("Ưu tiên nâng tường cấp thấp nhất (wall_lowest_first)")
        self.chk_wall_lowest.toggled.connect(lambda v: self.settings.set("wall_lowest_first", v))
        f_wall.addRow(self.chk_wall_lowest)

        self.combo_wall_qty = NoWheelComboBox()
        self.combo_wall_qty.addItem("Nhận diện OCR/ảnh (ocr)", "ocr")
        self.combo_wall_qty.addItem("Tọa độ cố định +10/+1/-1 (fixed)", "fixed")
        self.combo_wall_qty.currentIndexChanged.connect(
            lambda _: self.settings.set("wall_quantity_button_mode", self.combo_wall_qty.currentData())
        )
        f_wall.addRow("Cách bấm số lượng:", self.combo_wall_qty)
        self._add_setting_page("upgrade_walls", p_wall)

        # 5. collect_treasury
        p_treas = QWidget()
        v_treas = QVBoxLayout(p_treas)
        self.chk_treas_bypass = QCheckBox("Bỏ qua cooldown & cache số dư để đọc lại từ HUD")
        self.chk_treas_bypass.setChecked(True)
        note_treas = QLabel("Chế độ Chỉ quét sẽ chỉ đọc số dư Treasury từ popup thanh tài nguyên. Chế độ Thực thi thật sẽ tìm Clan Castle và bấm thu Treasury.")
        note_treas.setWordWrap(True)
        note_treas.setObjectName("sectionDetail")
        v_treas.addWidget(self.chk_treas_bypass)
        v_treas.addWidget(note_treas)
        v_treas.addStretch(1)
        self._add_setting_page("collect_treasury", p_treas)

        # 6. upgrade_army
        p_army = QWidget()
        f_army = QFormLayout(p_army)
        self.chk_army_res: Dict[str, QCheckBox] = {}
        r_army_row = QHBoxLayout()
        for k, lbl in (("elixir", "Dầu hồng"), ("dark_elixir", "Dầu đen")):
            c = QCheckBox(lbl)
            c.toggled.connect(self._save_army_settings)
            self.chk_army_res[k] = c
            r_army_row.addWidget(c)
        r_army_row.addStretch(1)
        f_army.addRow("Tài nguyên:", r_army_row)

        self.chk_army_types: Dict[str, QCheckBox] = {}
        t_army_row = QHBoxLayout()
        for k, lbl in (("troops", "Quân"), ("spells", "Phép"), ("siege", "Máy công thành")):
            c = QCheckBox(lbl)
            c.toggled.connect(self._save_army_settings)
            self.chk_army_types[k] = c
            t_army_row.addWidget(c)
        t_army_row.addStretch(1)
        f_army.addRow("Loại nâng:", t_army_row)

        self.combo_army_prio = NoWheelComboBox()
        for k, lbl in (("suggested", "Theo gợi ý"), ("cost_min", "Giá thấp trước"), ("cost_max", "Giá cao trước")):
            self.combo_army_prio.addItem(lbl, k)
        self.combo_army_prio.currentIndexChanged.connect(
            lambda _: self.settings.set("army_upgrade_priority", self.combo_army_prio.currentData())
        )
        f_army.addRow("Ưu tiên:", self.combo_army_prio)
        self._add_setting_page("upgrade_army", p_army)

        # 7. buy_new_buildings
        p_new = QWidget()
        v_new = QVBoxLayout(p_new)
        note_new = QLabel(
            "Dùng chung mức 'Số thợ rảnh cần duy trì' (cài đặt ở mục Nâng công trình). "
            "Chế độ Giả lập tới Confirm sẽ mở vùng đặt công trình mới để kiểm tra nút tích xanh, sau đó bấm Hủy (X đỏ) an toàn."
        )
        note_new.setWordWrap(True)
        note_new.setObjectName("sectionDetail")
        v_new.addWidget(note_new)
        v_new.addStretch(1)
        self._add_setting_page("buy_new_buildings", p_new)

        # 8. assign_apprentice
        p_app = QWidget()
        v_app = QVBoxLayout(p_app)
        note_app = QLabel(
            "Mở menu Thợ xây để kiểm tra Phụ tá (Builder's Apprentice). "
            "Chế độ Giả lập tới Confirm sẽ mở tới hộp thoại xác nhận và kiểm tra ô checkbox giữ phân công, KHÔNG bấm Confirm."
        )
        note_app.setWordWrap(True)
        note_app.setObjectName("sectionDetail")
        v_app.addWidget(note_app)
        v_app.addStretch(1)
        self._add_setting_page("assign_apprentice", p_app)

        # 9. main_combat
        p_cbt = QWidget()
        v_cbt = QVBoxLayout(p_cbt)
        v_cbt.setContentsMargins(2, 2, 2, 2)
        v_cbt.setSpacing(8)
        f_cbt = QFormLayout()
        f_cbt.setSpacing(6)

        self.combo_cbt_strategy = NoWheelComboBox()
        for k, lbl in (
            ("perimeter", "Rải quanh ranh căn cứ"),
            ("four_corners", "Chia 4 hướng"),
            ("smart_wave", "Tập trung từng cánh"),
            ("hybrid", "Hỗn hợp (4 hướng + rải quanh)"),
            ("resource_dragons", "Săn tài nguyên (Rồng)"),
            ("ultra_fast", "Siêu nhanh (Ultra Fast)"),
            ("random_mix", "Ngẫu nhiên mỗi trận"),
            ("fixed_positions", "Rải quân cố định"),
        ):
            self.combo_cbt_strategy.addItem(lbl, k)
        self.combo_cbt_strategy.currentIndexChanged.connect(
            lambda _: self.settings.set("attack_strategy", self.combo_cbt_strategy.currentData())
        )
        f_cbt.addRow("Chiến thuật thả:", self.combo_cbt_strategy)

        self.spin_cbt_spread = NoWheelSpinBox()
        self.spin_cbt_spread.setRange(4, 32)
        self.spin_cbt_spread.valueChanged.connect(lambda v: self.settings.set("attack_spread_points", v))
        f_cbt.addRow("Số điểm rải:", self.spin_cbt_spread)

        self.spin_cbt_drop_ms = NoWheelSpinBox()
        self.spin_cbt_drop_ms.setRange(20, 500)
        self.spin_cbt_drop_ms.setSingleStep(10)
        self.spin_cbt_drop_ms.setSuffix(" ms")
        self.spin_cbt_drop_ms.valueChanged.connect(lambda v: self.settings.set("troop_drop_delay_ms", v))
        f_cbt.addRow("Trễ thả quân:", self.spin_cbt_drop_ms)

        self.chk_cbt_spell_early = QCheckBox("Thả phép cùng đợt quân mở đầu")
        self.chk_cbt_spell_early.toggled.connect(lambda v: self.settings.set("spell_early_with_troops", v))
        f_cbt.addRow(self.chk_cbt_spell_early)

        self.combo_cbt_loot_mode = NoWheelComboBox()
        for k, lbl in (("legacy", "Vàng/Dầu & Hắc dầu (mặc định)"), ("all", "Đạt tất cả ngưỡng"), ("any", "Đạt ít nhất 1 ngưỡng")):
            self.combo_cbt_loot_mode.addItem(lbl, k)
        self.combo_cbt_loot_mode.currentIndexChanged.connect(
            lambda _: self.settings.set("loot_filter_mode", self.combo_cbt_loot_mode.currentData())
        )
        f_cbt.addRow("Kiểu lọc Loot:", self.combo_cbt_loot_mode)

        self.spin_cbt_gold = NoWheelSpinBox()
        self.spin_cbt_gold.setRange(0, 5_000_000)
        self.spin_cbt_gold.setSingleStep(50_000)
        self.spin_cbt_gold.valueChanged.connect(lambda v: self.settings.set("min_gold", v))
        f_cbt.addRow("Vàng tối thiểu:", self.spin_cbt_gold)

        self.spin_cbt_elixir = NoWheelSpinBox()
        self.spin_cbt_elixir.setRange(0, 5_000_000)
        self.spin_cbt_elixir.setSingleStep(50_000)
        self.spin_cbt_elixir.valueChanged.connect(lambda v: self.settings.set("min_elixir", v))
        f_cbt.addRow("Dầu tối thiểu:", self.spin_cbt_elixir)

        self.spin_cbt_dark = NoWheelSpinBox()
        self.spin_cbt_dark.setRange(0, 100_000)
        self.spin_cbt_dark.setSingleStep(500)
        self.spin_cbt_dark.valueChanged.connect(lambda v: self.settings.set("min_dark_elixir", v))
        f_cbt.addRow("Hắc dầu tối thiểu:", self.spin_cbt_dark)

        self.spin_cbt_skips = NoWheelSpinBox()
        self.spin_cbt_skips.setRange(1, 50)
        self.spin_cbt_skips.valueChanged.connect(lambda v: self.settings.set("max_search_skips", v))
        f_cbt.addRow("Số lần Next tối đa:", self.spin_cbt_skips)

        hero_grid = QGridLayout()
        self.chk_cbt_heroes: Dict[str, QCheckBox] = {}
        for idx, (hk, hlbl) in enumerate((
            ("use_king", "King"),
            ("use_queen", "Queen"),
            ("use_warden", "Warden"),
            ("use_champion", "Champion"),
            ("use_prince", "Prince"),
            ("use_luke", "Duke"),
        )):
            chk = QCheckBox(hlbl)
            chk.toggled.connect(lambda v, key=hk: self.settings.set(key, v))
            self.chk_cbt_heroes[hk] = chk
            hero_grid.addWidget(chk, idx // 3, idx % 3)
        f_cbt.addRow("Tướng sử dụng:", hero_grid)

        self.chk_cbt_hero_skill = QCheckBox("Tự kích hoạt kỹ năng Tướng")
        self.chk_cbt_hero_skill.toggled.connect(lambda v: self.settings.set("trigger_hero_abilities", v))
        f_cbt.addRow(self.chk_cbt_hero_skill)

        self.spin_cbt_hero_delay = NoWheelSpinBox()
        self.spin_cbt_hero_delay.setRange(0, 30)
        self.spin_cbt_hero_delay.setSuffix(" s")
        self.spin_cbt_hero_delay.valueChanged.connect(lambda v: self.settings.set("hero_ability_delay_sec", v))
        f_cbt.addRow("Trễ dùng skill Tướng:", self.spin_cbt_hero_delay)

        self.chk_cbt_siege = QCheckBox("Thả Máy công thành (Siege)")
        self.chk_cbt_siege.toggled.connect(lambda v: self.settings.set("use_siege", v))
        f_cbt.addRow(self.chk_cbt_siege)

        self.combo_cbt_siege_mode = NoWheelComboBox()
        self.combo_cbt_siege_mode.addItem("Luôn thả", "always")
        self.combo_cbt_siege_mode.addItem("Chỉ thả khi có lính Clan", "require_cc")
        self.combo_cbt_siege_mode.currentIndexChanged.connect(
            lambda _: self.settings.set("siege_deploy_mode", self.combo_cbt_siege_mode.currentData())
        )
        f_cbt.addRow("Điều kiện Siege:", self.combo_cbt_siege_mode)

        self.spin_cbt_timeout = NoWheelSpinBox()
        self.spin_cbt_timeout.setRange(15, 240)
        self.spin_cbt_timeout.setSuffix(" s")
        self.spin_cbt_timeout.valueChanged.connect(lambda v: self.settings.set("battle_timeout_sec", v))
        f_cbt.addRow("Hết giờ kết thúc:", self.spin_cbt_timeout)

        self.chk_cbt_surrender = QCheckBox("Tự đầu hàng khi đạt điều kiện dừng (auto_surrender)")
        self.chk_cbt_surrender.toggled.connect(lambda v: self.settings.set("auto_surrender", v))
        f_cbt.addRow(self.chk_cbt_surrender)

        self.chk_cbt_end_stars = QCheckBox("Dừng khi đạt số sao:")
        self.chk_cbt_end_stars.toggled.connect(lambda v: self.settings.set("end_on_stars", v))
        self.spin_cbt_stars = NoWheelSpinBox()
        self.spin_cbt_stars.setRange(1, 3)
        self.spin_cbt_stars.valueChanged.connect(lambda v: self.settings.set("star_target", v))
        star_row = QHBoxLayout()
        star_row.addWidget(self.chk_cbt_end_stars)
        star_row.addWidget(self.spin_cbt_stars)
        star_row.addStretch(1)
        f_cbt.addRow(star_row)

        self.chk_cbt_end_time = QCheckBox("Dừng theo thời gian trận (end_on_time)")
        self.chk_cbt_end_time.toggled.connect(lambda v: self.settings.set("end_on_time", v))
        f_cbt.addRow(self.chk_cbt_end_time)

        v_cbt.addLayout(f_cbt)

        grp_cbt_sim = QGroupBox("Tùy chọn Test Combat")
        v_cbt_sim = QVBoxLayout(grp_cbt_sim)
        self.chk_cbt_ignore_loot = QCheckBox("Bỏ qua lọc loot (chọn ngay nhà đầu)")
        self.chk_cbt_ignore_loot.setToolTip("Bỏ qua ngưỡng Vàng/Dầu/Hắc dầu để chọn luôn nhà đầu tiên khi test thả quân.")
        self.chk_cbt_return_home = QCheckBox("Tự rút về làng sau khi giả lập soi")
        self.chk_cbt_return_home.setToolTip("Bấm End Battle rút về làng an toàn sau khi giả lập soi nhà và vẽ điểm thả (chưa thả quân nên không mất quân).")
        self.chk_cbt_return_home.setChecked(True)
        v_cbt_sim.addWidget(self.chk_cbt_ignore_loot)
        v_cbt_sim.addWidget(self.chk_cbt_return_home)
        v_cbt.addWidget(grp_cbt_sim)
        v_cbt.addStretch(1)
        self._add_setting_page("main_combat", p_cbt)

    def _add_setting_page(self, flow_id: str, widget: QWidget):
        if widget.layout() is not None:
            widget.layout().setContentsMargins(4, 4, 16, 8)
        for form in widget.findChildren(QFormLayout):
            form.setFieldGrowthPolicy(QFormLayout.ExpandingFieldsGrow)
        for combo in widget.findChildren(NoWheelComboBox):
            combo.setSizeAdjustPolicy(NoWheelComboBox.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(10)
            combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        idx = self.settings_stack.addWidget(widget)
        self._page_index_by_flow[flow_id] = idx

    # ------------------------------------------------------------------
    # Settings Sync & Controls
    # ------------------------------------------------------------------
    def _sync_ui_from_settings(self):
        s = self.settings
        self.chk_auto_buildings.blockSignals(True)
        self.chk_auto_buildings.setChecked(bool(s.get("auto_upgrade_buildings", False)))
        self.chk_auto_buildings.blockSignals(False)

        self.chk_lock_th.blockSignals(True)
        self.chk_lock_th.setChecked(bool(s.get("lock_townhall_upgrade", True)))
        self.chk_lock_th.blockSignals(False)

        self.spin_reserve_builders.blockSignals(True)
        self.spin_reserve_builders.setValue(int(s.get("reserve_builders", 0)))
        self.spin_reserve_builders.blockSignals(False)

        idx = max(0, self.combo_bldg_priority.findData(s.get("building_upgrade_priority", "time_min")))
        self.combo_bldg_priority.blockSignals(True)
        self.combo_bldg_priority.setCurrentIndex(idx)
        self.combo_bldg_priority.blockSignals(False)

        spec_idx = max(0, self.combo_bldg_specific.findData(s.get("building_upgrade_specific_target", "")))
        self.combo_bldg_specific.blockSignals(True)
        self.combo_bldg_specific.setCurrentIndex(spec_idx)
        self.combo_bldg_specific.blockSignals(False)

        wait_idx = max(0, self.combo_bldg_wait.findData(s.get("building_upgrade_wait_mode", "target_cost")))
        self.combo_bldg_wait.blockSignals(True)
        self.combo_bldg_wait.setCurrentIndex(wait_idx)
        self.combo_bldg_wait.blockSignals(False)

        self.spin_bldg_full_pct.blockSignals(True)
        self.spin_bldg_full_pct.setValue(int(s.get("farm_finish_target_percent", 100)))
        self.spin_bldg_full_pct.blockSignals(False)

        self.spin_cap_pct.blockSignals(True)
        self.spin_cap_pct.setValue(int(s.get("farm_finish_target_percent", 100)))
        self.spin_cap_pct.blockSignals(False)

        cap_idx = self.combo_cap_res.findData(s.get("farm_finish_resources", "gold_elixir"))
        if cap_idx >= 0:
            self.combo_cap_res.setCurrentIndex(cap_idx)

        self.chk_bldg_menu_filter.blockSignals(True)
        self.chk_bldg_menu_filter.setChecked(bool(s.get("building_upgrade_use_menu_filter", True)))
        self.chk_bldg_menu_filter.blockSignals(False)

        self.chk_bldg_supercharge.blockSignals(True)
        self.chk_bldg_supercharge.setChecked(bool(s.get("building_upgrade_allow_supercharge", True)))
        self.chk_bldg_supercharge.blockSignals(False)

        res_cfg = dict(DEFAULT_CONFIG["building_upgrade_resources"])
        if isinstance(s.get("building_upgrade_resources"), dict):
            res_cfg.update(s.get("building_upgrade_resources"))
        for k, chk in self.chk_bldg_res.items():
            chk.blockSignals(True)
            chk.setChecked(bool(res_cfg.get(k, True)))
            chk.blockSignals(False)

        cat_cfg = dict(DEFAULT_CONFIG["building_upgrade_categories"])
        if isinstance(s.get("building_upgrade_categories"), dict):
            cat_cfg.update(s.get("building_upgrade_categories"))
        for k, chk in self.chk_bldg_cats.items():
            chk.blockSignals(True)
            chk.setChecked(bool(cat_cfg.get(k, True)))
            chk.blockSignals(False)

        self._apply_bldg_resource_constraints()
        self._update_bldg_visibility()
        self._update_bldg_summary()

        # Wall settings
        w_idx = max(0, self.combo_wall_res.findData(s.get("wall_resource", "both")))
        self.combo_wall_res.blockSignals(True)
        self.combo_wall_res.setCurrentIndex(w_idx)
        self.combo_wall_res.blockSignals(False)
        self.spin_wall_gold.blockSignals(True)
        self.spin_wall_gold.setValue(int(s.get("min_gold_to_upgrade_wall", 3_000_000)))
        self.spin_wall_gold.blockSignals(False)
        self.spin_wall_elixir.blockSignals(True)
        self.spin_wall_elixir.setValue(int(s.get("min_elixir_to_upgrade_wall", 3_000_000)))
        self.spin_wall_elixir.blockSignals(False)
        self.chk_wall_lowest.blockSignals(True)
        self.chk_wall_lowest.setChecked(bool(s.get("wall_lowest_first", True)))
        self.chk_wall_lowest.blockSignals(False)
        wq_idx = max(0, self.combo_wall_qty.findData(s.get("wall_quantity_button_mode", "ocr")))
        self.combo_wall_qty.blockSignals(True)
        self.combo_wall_qty.setCurrentIndex(wq_idx)
        self.combo_wall_qty.blockSignals(False)

        # Army settings
        a_res = s.get("army_upgrade_resources") or {}
        for k, c in self.chk_army_res.items():
            c.blockSignals(True)
            c.setChecked(bool(a_res.get(k, True)))
            c.blockSignals(False)
        a_types = s.get("army_upgrade_types") or {}
        for k, c in self.chk_army_types.items():
            c.blockSignals(True)
            c.setChecked(bool(a_types.get(k, True)))
            c.blockSignals(False)
        ap_idx = max(0, self.combo_army_prio.findData(s.get("army_upgrade_priority", "suggested")))
        self.combo_army_prio.blockSignals(True)
        self.combo_army_prio.setCurrentIndex(ap_idx)
        self.combo_army_prio.blockSignals(False)

        # Main combat settings
        cs_idx = max(0, self.combo_cbt_strategy.findData(s.get("attack_strategy", "perimeter")))
        self.combo_cbt_strategy.blockSignals(True)
        self.combo_cbt_strategy.setCurrentIndex(cs_idx)
        self.combo_cbt_strategy.blockSignals(False)
        self.spin_cbt_spread.blockSignals(True)
        self.spin_cbt_spread.setValue(int(s.get("attack_spread_points", 16)))
        self.spin_cbt_spread.blockSignals(False)
        self.spin_cbt_drop_ms.blockSignals(True)
        self.spin_cbt_drop_ms.setValue(int(s.get("troop_drop_delay_ms", 80)))
        self.spin_cbt_drop_ms.blockSignals(False)
        self.chk_cbt_spell_early.blockSignals(True)
        self.chk_cbt_spell_early.setChecked(bool(s.get("spell_early_with_troops", True)))
        self.chk_cbt_spell_early.blockSignals(False)
        lm_idx = max(0, self.combo_cbt_loot_mode.findData(s.get("loot_filter_mode", "legacy")))
        self.combo_cbt_loot_mode.blockSignals(True)
        self.combo_cbt_loot_mode.setCurrentIndex(lm_idx)
        self.combo_cbt_loot_mode.blockSignals(False)
        self.spin_cbt_gold.blockSignals(True)
        self.spin_cbt_gold.setValue(int(s.get("min_gold", 600000)))
        self.spin_cbt_gold.blockSignals(False)
        self.spin_cbt_elixir.blockSignals(True)
        self.spin_cbt_elixir.setValue(int(s.get("min_elixir", 600000)))
        self.spin_cbt_elixir.blockSignals(False)
        self.spin_cbt_dark.blockSignals(True)
        self.spin_cbt_dark.setValue(int(s.get("min_dark_elixir", 4000)))
        self.spin_cbt_dark.blockSignals(False)
        self.spin_cbt_skips.blockSignals(True)
        self.spin_cbt_skips.setValue(int(s.get("max_search_skips", 50)))
        self.spin_cbt_skips.blockSignals(False)
        for hk, chk in self.chk_cbt_heroes.items():
            chk.blockSignals(True)
            chk.setChecked(bool(s.get(hk, True)))
            chk.blockSignals(False)
        self.chk_cbt_hero_skill.blockSignals(True)
        self.chk_cbt_hero_skill.setChecked(bool(s.get("trigger_hero_abilities", True)))
        self.chk_cbt_hero_skill.blockSignals(False)
        self.spin_cbt_hero_delay.blockSignals(True)
        self.spin_cbt_hero_delay.setValue(int(s.get("hero_ability_delay_sec", 5)))
        self.spin_cbt_hero_delay.blockSignals(False)
        self.chk_cbt_siege.blockSignals(True)
        self.chk_cbt_siege.setChecked(bool(s.get("use_siege", True)))
        self.chk_cbt_siege.blockSignals(False)
        sm_idx = max(0, self.combo_cbt_siege_mode.findData(s.get("siege_deploy_mode", "always")))
        self.combo_cbt_siege_mode.blockSignals(True)
        self.combo_cbt_siege_mode.setCurrentIndex(sm_idx)
        self.combo_cbt_siege_mode.blockSignals(False)
        self.spin_cbt_timeout.blockSignals(True)
        self.spin_cbt_timeout.setValue(int(s.get("battle_timeout_sec", 120)))
        self.spin_cbt_timeout.blockSignals(False)
        self.chk_cbt_surrender.blockSignals(True)
        self.chk_cbt_surrender.setChecked(bool(s.get("auto_surrender", True)))
        self.chk_cbt_surrender.blockSignals(False)
        self.chk_cbt_end_stars.blockSignals(True)
        self.chk_cbt_end_stars.setChecked(bool(s.get("end_on_stars", True)))
        self.chk_cbt_end_stars.blockSignals(False)
        self.spin_cbt_stars.blockSignals(True)
        self.spin_cbt_stars.setValue(int(s.get("star_target", 1)))
        self.spin_cbt_stars.blockSignals(False)
        self.chk_cbt_end_time.blockSignals(True)
        self.chk_cbt_end_time.setChecked(bool(s.get("end_on_time", True)))
        self.chk_cbt_end_time.blockSignals(False)

    def _on_flow_changed(self, _index: int):
        flow_id = self.combo_flow.currentData()
        meta = FLOW_BY_ID.get(flow_id)
        if not meta:
            return
        self.lbl_flow_desc.setText(meta["description"])
        page_idx = self._page_index_by_flow.get(flow_id, 0)
        self.settings_stack.setCurrentIndex(page_idx)

        self.combo_mode.blockSignals(True)
        self.combo_mode.clear()
        combat_labels = {
            "scan_only": "Giả lập: Chỉ mở Menu / soi nhà hiện tại",
            "until_confirm": "Giả lập: Tìm nhà, đọc Deck & vẽ điểm thả (KHÔNG thả quân)",
            "real": "Thực thi thật: Đánh hoàn chỉnh 1 trận",
        }
        for m in meta["modes"]:
            lbl = combat_labels.get(m, MODE_LABELS.get(m, m)) if flow_id == "main_combat" else MODE_LABELS.get(m, m)
            self.combo_mode.addItem(lbl, m)
        def_idx = max(0, self.combo_mode.findData(meta["default_mode"]))
        self.combo_mode.setCurrentIndex(def_idx)
        self.combo_mode.blockSignals(False)
        self._on_mode_changed(def_idx)

    def _on_mode_changed(self, _index: int):
        mode = self.combo_mode.currentData()
        is_scan = (mode == "scan_only")
        for chk in (self.chk_bldg_virtual_rich, self.chk_bldg_force_scan, self.chk_bldg_ignore_wait):
            chk.setEnabled(is_scan)

    def _on_bldg_priority_changed(self, _index: int):
        self.settings.set("building_upgrade_priority", self.combo_bldg_priority.currentData())
        self._update_bldg_visibility()

    def _on_bldg_wait_changed(self, _index: int):
        self.settings.set("building_upgrade_wait_mode", self.combo_bldg_wait.currentData())
        self._update_bldg_visibility()

    def _update_bldg_visibility(self):
        is_spec = self.combo_bldg_priority.currentData() == "specific"
        self.lbl_bldg_spec.setVisible(is_spec)
        self.combo_bldg_specific.setVisible(is_spec)
        is_full = self.combo_bldg_wait.currentData() == "full_storage"
        self.spin_bldg_full_pct.setEnabled(is_full)

    def _save_bldg_resources(self):
        res = {k: chk.isChecked() for k, chk in self.chk_bldg_res.items()}
        self.settings.set("building_upgrade_resources", res)
        self._apply_bldg_resource_constraints()
        self._update_bldg_summary()

    def _apply_bldg_resource_constraints(self):
        selected_res = {k for k, chk in self.chk_bldg_res.items() if chk.isChecked()}
        cats = {}
        for c_key, chk in self.chk_bldg_cats.items():
            supported = CATEGORY_TO_RESOURCES.get(c_key, ())
            has_match = any(r in selected_res for r in supported) if selected_res else True
            chk.setEnabled(has_match)
            if not has_match:
                chk.blockSignals(True)
                chk.setChecked(False)
                chk.blockSignals(False)
            cats[c_key] = chk.isChecked()
        self.settings.set("building_upgrade_categories", cats)

    def _save_bldg_categories(self):
        cats = {k: chk.isChecked() for k, chk in self.chk_bldg_cats.items()}
        self.settings.set("building_upgrade_categories", cats)
        self._update_bldg_summary()

    def _on_bldg_preset(self, index: int):
        pn = self.combo_bldg_preset.itemData(index)
        if not pn:
            return
        if pn == "all":
            for c in self.chk_bldg_res.values(): c.setChecked(True)
            for c in self.chk_bldg_cats.values(): c.setChecked(True)
            self.settings.set("building_upgrade_whitelist", dict(DEFAULT_BUILDING_WHITELIST))
        elif pn == "defense_traps":
            for k in ("gold", "elixir", "dark_elixir"): self.chk_bldg_res[k].setChecked(True)
            for k, c in self.chk_bldg_cats.items(): c.setChecked(k in ("traps", "defense"))
        elif pn == "resource":
            self.chk_bldg_res["gold"].setChecked(True)
            self.chk_bldg_res["elixir"].setChecked(True)
            self.chk_bldg_res["dark_elixir"].setChecked(False)
            for k, c in self.chk_bldg_cats.items(): c.setChecked(k == "resource")
        elif pn == "army_heroes":
            for k in ("gold", "elixir", "dark_elixir"): self.chk_bldg_res[k].setChecked(True)
            for k, c in self.chk_bldg_cats.items(): c.setChecked(k in ("army", "heroes"))
        elif pn == "elixir_focus":
            self.chk_bldg_res["gold"].setChecked(False)
            self.chk_bldg_res["elixir"].setChecked(True)
            self.chk_bldg_res["dark_elixir"].setChecked(False)
            for k, c in self.chk_bldg_cats.items(): c.setChecked(k in ("defense", "resource", "army", "heroes"))
        elif pn == "gold_focus":
            self.chk_bldg_res["gold"].setChecked(True)
            self.chk_bldg_res["elixir"].setChecked(False)
            self.chk_bldg_res["dark_elixir"].setChecked(False)
            for k, c in self.chk_bldg_cats.items(): c.setChecked(k in ("traps", "defense", "resource", "army"))
        self.combo_bldg_preset.blockSignals(True)
        self.combo_bldg_preset.setCurrentIndex(0)
        self.combo_bldg_preset.blockSignals(False)
        self._update_bldg_summary()

    def _open_bldg_whitelist_dialog(self):
        dlg = BuildingSelectionDialog(self.settings, self)
        dlg.exec_()
        self._update_bldg_summary()

    def _reset_bldg_defaults(self):
        self.settings.set("auto_upgrade_buildings", DEFAULT_CONFIG["auto_upgrade_buildings"])
        self.settings.set("lock_townhall_upgrade", DEFAULT_CONFIG["lock_townhall_upgrade"])
        self.settings.set("reserve_builders", DEFAULT_CONFIG["reserve_builders"])
        self.settings.set("building_upgrade_priority", DEFAULT_CONFIG["building_upgrade_priority"])
        self.settings.set("building_upgrade_specific_target", DEFAULT_CONFIG["building_upgrade_specific_target"])
        self.settings.set("building_upgrade_wait_mode", DEFAULT_CONFIG["building_upgrade_wait_mode"])
        self.settings.set("building_upgrade_use_menu_filter", DEFAULT_CONFIG["building_upgrade_use_menu_filter"])
        self.settings.set("building_upgrade_allow_supercharge", DEFAULT_CONFIG["building_upgrade_allow_supercharge"])
        self.settings.set("building_upgrade_resources", dict(DEFAULT_CONFIG["building_upgrade_resources"]))
        self.settings.set("building_upgrade_categories", dict(DEFAULT_CONFIG["building_upgrade_categories"]))
        self.settings.set("building_upgrade_whitelist", dict(DEFAULT_BUILDING_WHITELIST))
        self._sync_ui_from_settings()
        self._append_log("[Cấu hình] Đã đặt lại cấu hình Nâng công trình về mặc định.")

    def _enable_whitelist_for_active_categories(self):
        wl = dict(DEFAULT_BUILDING_WHITELIST)
        if isinstance(self.settings.get("building_upgrade_whitelist"), dict):
            wl.update(self.settings.get("building_upgrade_whitelist"))
        cats = self.settings.get("building_upgrade_categories") or {}
        res_cfg = self.settings.get("building_upgrade_resources") or {}
        enabled_now = 0
        for c_key, items in BUILDING_CATALOG.items():
            if not cats.get(c_key, True):
                continue
            for b_key, _, _ in items:
                if any(res_cfg.get(r, True) for r in BUILDING_KEY_TO_RESOURCES.get(b_key, ())):
                    if not wl.get(b_key, True):
                        enabled_now += 1
                    wl[b_key] = True
        self.settings.set("building_upgrade_whitelist", wl)
        self._update_bldg_summary()
        self._append_log(f"[Cấu hình] ✅ Đã tích thêm {enabled_now} công trình trong danh sách chi tiết theo nhóm & tài nguyên đang bật.")

    def _update_bldg_summary(self):
        wl = dict(DEFAULT_BUILDING_WHITELIST)
        if isinstance(self.settings.get("building_upgrade_whitelist"), dict):
            wl.update(self.settings.get("building_upgrade_whitelist"))
        cats = dict(DEFAULT_CONFIG["building_upgrade_categories"])
        if isinstance(self.settings.get("building_upgrade_categories"), dict):
            cats.update(self.settings.get("building_upgrade_categories"))
        res_cfg = dict(DEFAULT_CONFIG["building_upgrade_resources"])
        if isinstance(self.settings.get("building_upgrade_resources"), dict):
            res_cfg.update(self.settings.get("building_upgrade_resources"))
        total = sum(len(items) for items in BUILDING_CATALOG.values())
        active = 0
        empty_cats = []
        cat_labels = dict(BUILDING_CATEGORY_LABELS)
        for c_key, items in BUILDING_CATALOG.items():
            if not cats.get(c_key, True):
                continue
            cat_active = 0
            for b_key, _, _ in items:
                if not wl.get(b_key, True):
                    continue
                if any(res_cfg.get(r, True) for r in BUILDING_KEY_TO_RESOURCES.get(b_key, ())):
                    active += 1
                    cat_active += 1
            if cat_active == 0:
                empty_cats.append(cat_labels.get(c_key, c_key))
        msg = f"{active}/{total} công trình hợp lệ theo bộ lọc hiện tại"
        if empty_cats:
            msg += f"\n⚠️ Nhóm {', '.join(empty_cats)} đang bật nhưng chưa tích công trình nào trong Danh sách chi tiết!"
            self.lbl_bldg_summary.setStyleSheet("color: #f59e0b; font-weight: 700;")
        else:
            self.lbl_bldg_summary.setStyleSheet("")
        self.lbl_bldg_summary.setText(msg)

    def _save_army_settings(self):
        self.settings.set("army_upgrade_resources", {k: c.isChecked() for k, c in self.chk_army_res.items()})
        self.settings.set("army_upgrade_types", {k: c.isChecked() for k, c in self.chk_army_types.items()})

    def _reload_from_main_config(self):
        self._copy_from_main_config(save=True, log_msg=True)
        self._sync_ui_from_settings()

    # ------------------------------------------------------------------
    # Theme & Visual Helpers
    # ------------------------------------------------------------------
    def _apply_theme(self):
        theme = self.settings.get("ui_theme", "dark")
        size = self.settings.get("ui_text_size", "medium")
        self.setStyleSheet(theme_qss(theme, size))
        self.btn_theme.setText("Theme: Sáng" if theme == "light" else "Theme: Tối")

    def _toggle_theme(self):
        cur = self.settings.get("ui_theme", "dark")
        nxt = "light" if cur == "dark" else "dark"
        self.settings.set("ui_theme", nxt)
        self._apply_theme()

    def _set_result_badge(self, status: str, summary: str, elapsed_text: str = ""):
        is_light = self.settings.get("ui_theme", "dark") == "light"
        tone_map = {
            "IDLE": ("CHƯA CHẠY", "slate", "#475569" if is_light else "#94a3b8", "rgba(148,163,184,0.18)"),
            "RUNNING": ("ĐANG CHẠY...", "blue", "#0f679d" if is_light else "#38bdf8", "rgba(56,189,248,0.22)"),
            "PASS": ("PASS ✅", "green", "#126b43" if is_light else "#34d399", "rgba(52,211,153,0.22)"),
            "WARN": ("WARN ⚠️", "amber", "#925200" if is_light else "#fbbf24", "rgba(251,191,36,0.22)"),
            "FAIL": ("FAIL ❌", "slate", "#a82e3e" if is_light else "#f87171", "rgba(248,113,113,0.22)"),
            "STOPPED": ("ĐÃ DỪNG ⏹", "slate", "#b45309" if is_light else "#fb923c", "rgba(251,146,60,0.22)"),
        }
        label, card_tone, fg, bg = tone_map.get(status, (status, "blue", "#38bdf8", "rgba(56,189,248,0.22)"))
        self.lbl_res_badge.setText(label)
        self.lbl_res_badge.setStyleSheet(
            f"background: {bg}; color: {fg}; border: 1px solid {fg}; border-radius: 7px; padding: 4px 10px; font-weight: 700;"
        )
        self.result_card.setProperty("tone", card_tone)
        self.result_card.style().unpolish(self.result_card)
        self.result_card.style().polish(self.result_card)
        self.lbl_res_summary.setText(summary)
        self.lbl_res_elapsed.setText(elapsed_text)

    # ------------------------------------------------------------------
    # ADB Operations & Test Execution
    # ------------------------------------------------------------------
    def _emit_safe(self, signal_name: str, *args):
        if getattr(self, "_closing", False):
            return
        try:
            bridge = getattr(self, "bridge", None)
            if bridge is not None:
                getattr(bridge, signal_name).emit(*args)
        except RuntimeError:
            pass

    def _scan_devices(self):
        self.btn_scan_dev.setEnabled(False)

        def job():
            try:
                devices = self.adb.list_devices()
                self._emit_safe("sig_devices", devices, "")
            except Exception as exc:
                self._emit_safe("sig_devices", [], str(exc))

        threading.Thread(target=job, daemon=True, name="TestToolScanADB").start()

    def _selected_serial(self) -> str:
        idx = self.combo_devices.currentIndex()
        txt = self.combo_devices.currentText().strip()
        if idx >= 0 and self.combo_devices.itemText(idx).strip() == txt:
            data = self.combo_devices.itemData(idx)
            if isinstance(data, str) and data.strip():
                return data.strip()
        return txt.split()[0] if txt else ""

    def _on_devices_scanned(self, devices: List[Any], err: str):
        self.btn_scan_dev.setEnabled(True)
        current = self._selected_serial() or self.settings.get("device_serial", "127.0.0.1:5555")
        self.combo_devices.blockSignals(True)
        self.combo_devices.clear()
        serials: List[str] = []
        seen = set()
        for d in devices or []:
            if isinstance(d, dict):
                serial = str(d.get("serial", "")).strip()
                state = str(d.get("state", "")).strip()
                model = str(d.get("model", "")).strip()
                if not serial or serial in seen:
                    continue
                seen.add(serial)
                parts = [serial]
                if model:
                    parts.append(model)
                if state and state != "device":
                    parts.append(f"[{state}]")
                label = " · ".join(parts)
            else:
                serial = str(d).strip()
                if not serial or serial in seen:
                    continue
                seen.add(serial)
                label = serial
            serials.append(serial)
            self.combo_devices.addItem(label, serial)

        for fallback in ("127.0.0.1:16384", "127.0.0.1:5555"):
            if fallback not in seen:
                self.combo_devices.addItem(fallback, fallback)
                seen.add(fallback)

        if current and current not in seen:
            self.combo_devices.addItem(current, current)

        idx = self.combo_devices.findData(current)
        if idx >= 0:
            self.combo_devices.setCurrentIndex(idx)
        elif self.combo_devices.count() > 0:
            self.combo_devices.setCurrentIndex(0)
        self.combo_devices.blockSignals(False)

        if err:
            self._append_log(f"[ADB] Lỗi quét thiết bị: {err}")
        elif serials:
            self._append_log(f"[ADB] Tìm thấy thiết bị: {', '.join(serials)}")
        else:
            self._append_log("[ADB] Chưa phát hiện thiết bị ADB nào (kiểm tra MuMu đang mở).")

    def _connect_device(self):
        serial = self._selected_serial()
        if not serial:
            return
        self.settings.set("device_serial", serial)
        self.btn_connect.setEnabled(False)
        self._append_log(f"[ADB] Đang kết nối tới {serial}...")

        def job():
            ok = False
            try:
                self.adb.device_unavailable = False
                ok = self.adb.connect_device(serial, start_server=True)
                if ok:
                    frame = self.adb.screencap()
                    if frame is not None:
                        self._emit_safe("sig_frame", frame, f"Đã kết nối {serial}")
            except Exception as exc:
                self._emit_safe("sig_log", f"[ADB] Lỗi kết nối {serial}: {exc}")
                ok = False
            self._emit_safe("sig_connected", ok, serial)

        threading.Thread(target=job, daemon=True, name="TestToolConnectADB").start()

    def _on_connected(self, ok: bool, serial: str):
        self.btn_connect.setEnabled(True)
        is_light = self.settings.get("ui_theme", "dark") == "light"
        if ok:
            self._connected_serial = serial
            fg = "#126b43" if is_light else "#34d399"
            self.lbl_conn.setText(f"Đã kết nối: {serial}")
            self.lbl_conn.setStyleSheet(
                f"background: rgba(52,211,153,0.18); color: {fg}; border: 1px solid {fg}; border-radius: 7px; padding: 5px 10px; font-weight: 700;"
            )
            self._append_log(f"[ADB] ✅ Kết nối thành công tới {serial}.")
        else:
            self._connected_serial = None
            fg = "#a82e3e" if is_light else "#f87171"
            self.lbl_conn.setText("Mất kết nối")
            self.lbl_conn.setStyleSheet(
                f"background: rgba(248,113,113,0.18); color: {fg}; border: 1px solid {fg}; border-radius: 7px; padding: 5px 10px; font-weight: 700;"
            )
            self._append_log(f"[ADB] ❌ Không kết nối được tới {serial}.")

    def _capture_preview_once(self):
        serial = self._selected_serial()
        if not serial:
            return

        def job():
            if self.adb.device_serial != serial:
                self.adb.device_unavailable = False
                if not self.adb.connect_device(serial, start_server=True):
                    self._emit_safe("sig_log", f"[Preview] ❌ Chưa kết nối được tới {serial}.")
                    return
            frame = self.adb.screencap()
            if frame is not None:
                self._emit_safe("sig_frame", frame, f"Chụp nhanh từ {serial} ({frame.shape[1]}x{frame.shape[0]})")
            else:
                self._emit_safe("sig_log", "[Preview] ⚠️ Không lấy được frame từ giả lập.")

        threading.Thread(target=job, daemon=True, name="TestToolSnap").start()

    def _collect_options_for_flow(self, flow_id: str) -> Dict[str, Any]:
        if flow_id == "collect_resources":
            return {
                "auto_close_popups": self.chk_col_popups.isChecked(),
                "auto_zoom": self.chk_col_zoom.isChecked(),
                "annotate": self.chk_col_annotate.isChecked(),
            }
        if flow_id == "check_max_storage":
            return {
                "farm_finish_resources": self.combo_cap_res.currentData(),
                "farm_finish_target_percent": self.spin_cap_pct.value(),
                "bypass_cache": self.chk_cap_bypass_cache.isChecked(),
                "verify_account": self.chk_cap_verify_acc.isChecked(),
                "verify_twice": self.chk_cap_verify_twice.isChecked(),
            }
        if flow_id == "upgrade_buildings":
            return {
                "walls_confirmed_maxed": self.chk_bldg_walls_maxed.isChecked(),
                "virtual_rich_wallet": self.chk_bldg_virtual_rich.isChecked(),
                "force_scan_when_no_free": self.chk_bldg_force_scan.isChecked(),
                "ignore_wait_mode_block": self.chk_bldg_ignore_wait.isChecked(),
            }
        if flow_id == "collect_treasury":
            return {"bypass_cache": self.chk_treas_bypass.isChecked()}
        if flow_id == "main_combat":
            return {
                "ignore_loot_filter": self.chk_cbt_ignore_loot.isChecked(),
                "return_home_after_scan": self.chk_cbt_return_home.isChecked(),
            }
        return {}

    def _start_test(self):
        if self._is_running:
            return
        flow_id = self.combo_flow.currentData()
        mode = self.combo_mode.currentData()
        serial = self._selected_serial()
        if not serial:
            QMessageBox.warning(self, "Thiếu thiết bị", "Hãy nhập hoặc quét chọn thiết bị ADB trước khi chạy test.")
            return

        if mode == "real" and flow_id in ("upgrade_buildings", "upgrade_walls", "upgrade_army", "buy_new_buildings", "assign_apprentice", "main_combat"):
            reply = QMessageBox.question(
                self,
                "Xác nhận chạy Thực thi thật",
                "Bạn đang chọn chế độ THỰC THI THẬT (sẽ bấm nút nâng cấp/mua thật và tiêu tài nguyên trong game).\n"
                "Nếu chỉ muốn kiểm tra luồng mà không tiêu tài nguyên, hãy chọn chế độ Giả lập.\n\n"
                "Tiếp tục chạy thật?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return

        self.settings.set("device_serial", serial)
        options = self._collect_options_for_flow(flow_id)
        self._is_running = True
        self._stop_event.clear()
        self.btn_run.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.combo_flow.setEnabled(False)
        self.combo_mode.setEnabled(False)
        self._set_result_badge("RUNNING", f"Đang chạy '{FLOW_BY_ID[flow_id]['name']}' ({MODE_LABELS.get(mode, mode)})...")

        def worker():
            try:
                self.adb.device_unavailable = False
                if self.adb.device_serial != serial or not self._connected_serial:
                    self._emit_safe("sig_log", f"[ADB] Đang kết nối tới {serial}...")
                    ok = self.adb.connect_device(serial, start_server=True)
                    self._emit_safe("sig_connected", ok, serial)
                    if not ok:
                        self._emit_safe(
                            "sig_finished",
                            FlowTestResult(flow_id, FLOW_BY_ID[flow_id]["name"], "FAIL", f"Không kết nối được tới {serial}.", mode=mode),
                        )
                        return

                vision = self._get_vision()
                runner = FlowTestRunner(
                    adb=self.adb,
                    vision=vision,
                    settings=self.settings,
                    log_cb=lambda m: self._emit_safe("sig_log", m),
                    frame_cb=lambda f, c: self._emit_safe("sig_frame", f, c),
                    stop_event=self._stop_event,
                )
                result = runner.run_flow(flow_id, mode=mode, options=options)
                self._emit_safe("sig_finished", result)
            except Exception as exc:
                self._emit_safe("sig_log", f"[TestTool] ❌ Lỗi không mong muốn: {exc}")
                self._emit_safe(
                    "sig_finished",
                    FlowTestResult(flow_id, FLOW_BY_ID[flow_id]["name"], "FAIL", f"Lỗi chạy worker: {exc}", mode=mode),
                )

        self._worker_thread = threading.Thread(target=worker, daemon=True, name="CoCAttackTestWorker")
        self._worker_thread.start()

    def _stop_test(self):
        if self._is_running:
            self._stop_event.set()
            self._append_log("[TestTool] ⏹ Đang gửi tín hiệu dừng bài test...")
            self.btn_stop.setEnabled(False)

    def _on_test_finished(self, result: FlowTestResult):
        self._is_running = False
        self.btn_run.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.combo_flow.setEnabled(True)
        self.combo_mode.setEnabled(True)
        self._set_result_badge(result.status, result.summary, f"Thời gian: {result.elapsed_sec:.2f}s")

    # ------------------------------------------------------------------
    # Frame & Log Handlers
    # ------------------------------------------------------------------
    def _on_frame(self, frame, caption: str):
        if frame is not None:
            self.viewer.update_frame(frame)
        if caption:
            self.lbl_frame_caption.setText(caption)

    def _append_log(self, msg: str):
        self.txt_log.appendPlainText(msg)
        if self.chk_autoscroll.isChecked():
            cursor = self.txt_log.textCursor()
            cursor.movePosition(QTextCursor.End)
            self.txt_log.setTextCursor(cursor)

    def _copy_logs(self):
        QApplication.clipboard().setText(self.txt_log.toPlainText())
        self._append_log("[TestTool] Đã copy toàn bộ log vào clipboard.")

    def _save_logs(self):
        path, _ = QFileDialog.getSaveFileName(self, "Lưu log test", "coc_test_log.txt", "Text Files (*.txt)")
        if path:
            Path(path).write_text(self.txt_log.toPlainText(), encoding="utf-8")
            self._append_log(f"[TestTool] Đã lưu log ra {path}")

    def closeEvent(self, event):
        self._closing = True
        self._stop_event.set()
        if self._worker_thread is not None and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=1.0)
        event.accept()
