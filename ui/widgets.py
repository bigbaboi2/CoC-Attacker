import cv2
import numpy as np
import time
from datetime import datetime

from bot.builder_army import COMBOS, REINFORCEMENT_CHOICES, REINFORCEMENT_KEYS
from core.village_state import STRATEGY_NAMES
from PyQt5.QtWidgets import (
    QWidget, QLabel, QVBoxLayout, QHBoxLayout, QFrame,
    QPlainTextEdit, QPushButton, QSizePolicy, QTabBar, QLayout,
    QSpinBox, QComboBox, QDateTimeEdit, QApplication, QScrollArea
)
from PyQt5.QtGui import QImage, QPixmap, QColor, QPainter, QWheelEvent, QLinearGradient, QFont
from PyQt5.QtCore import Qt, pyqtSignal, QTimer, QPointF, QSize, QLocale
from ui.styles import PALETTES


def _scroll_parent(widget, event):
    """Pass wheel input to the containing page without changing a field."""
    parent = widget.parentWidget()
    while parent is not None and not isinstance(parent, QScrollArea):
        parent = parent.parentWidget()
    if parent is None:
        event.ignore()
        return
    viewport = parent.viewport()
    position = widget.mapTo(viewport, event.pos())
    forwarded = QWheelEvent(QPointF(position), event.globalPosF(), event.pixelDelta(),
                            event.angleDelta(), event.buttons(), event.modifiers(),
                            event.phase(), event.inverted())
    QApplication.sendEvent(viewport, forwarded)
    event.setAccepted(forwarded.isAccepted())


class NoWheelSpinBox(QSpinBox):
    """Keep page scrolling independent of numeric editing."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setLocale(QLocale(QLocale.English, QLocale.UnitedStates))
        self.setGroupSeparatorShown(True)

    def wheelEvent(self, event):
        _scroll_parent(self, event)


class NoWheelComboBox(QComboBox):
    def wheelEvent(self, event):
        _scroll_parent(self, event)


class NoWheelDateTimeEdit(QDateTimeEdit):
    def wheelEvent(self, event):
        _scroll_parent(self, event)


class BubbleTabBar(QTabBar):
    """Game-like rounded tabs, painted directly to avoid Qt5 style clipping."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.theme_name = 'dark'
        self.active_dots = {}
        self.setDrawBase(False)
        self.setExpanding(False)
        self.setUsesScrollButtons(True)

    def set_theme(self, theme):
        self.theme_name = theme
        self.update()

    def set_active_dots(self, dots):
        if self.active_dots != dots:
            self.active_dots = dict(dots)
            self.update()

    def tabSizeHint(self, index):
        hint = super().tabSizeHint(index)
        return QSize(max(88, hint.width() + 22), max(43, hint.height() + 12))

    def paintEvent(self, event):
        p = PALETTES.get(self.theme_name, PALETTES['dark'])
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        font = QFont('Baloo 2', max(10, self.font().pointSize()))
        font.setWeight(QFont.Bold)
        painter.setFont(font)
        for index in range(self.count()):
            bounds = self.tabRect(index).adjusted(3, 3, -3, -6)
            if not bounds.intersects(event.rect()):
                continue
            selected = index == self.currentIndex()
            enabled = self.isTabEnabled(index)
            shadow = bounds.translated(0, 3)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor('#0b1827' if self.theme_name == 'dark' else '#8dafc8'))
            painter.drawRoundedRect(shadow, 11, 11)
            top = QColor('#b6efff' if selected else p['selected'])
            bottom = QColor(p['accent'] if selected else p['raised'])
            if not enabled:
                top.setAlpha(115)
                bottom.setAlpha(115)
            fill = QLinearGradient(bounds.topLeft(), bounds.bottomLeft())
            fill.setColorAt(0, top)
            fill.setColorAt(1, bottom)
            painter.setBrush(fill)
            painter.setPen(QColor(p['accent'] if selected else p['border']))
            painter.drawRoundedRect(bounds, 11, 11)
            painter.setPen(QColor('#17324b' if selected else p['text'] if enabled else p['disabled']))
            painter.drawText(bounds.adjusted(10, 0, -10, -1), Qt.AlignCenter, self.tabText(index))
            if index in self.active_dots and enabled:
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(self.active_dots[index]))
                painter.drawEllipse(bounds.right() - 10, bounds.top() + 6, 7, 7)
        painter.end()


class VillageTabBar(BubbleTabBar):
    disabled_clicked = pyqtSignal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.village_colors = {}
        self.main_village = -1
        self.flash_steps = 0
        self.flash_timer = QTimer(self)
        self.flash_timer.setInterval(230)
        self.flash_timer.timeout.connect(self._advance_flash)

    def set_village_colors(self, colors):
        if self.village_colors != colors:
            self.village_colors = colors
            self.update()

    def flash_main_village(self):
        self.flash_steps = 6
        self.update()
        self.flash_timer.start()

    def _advance_flash(self):
        self.flash_steps -= 1
        self.update()
        if not self.flash_steps:
            self.flash_timer.stop()

    def mousePressEvent(self, event):
        index = self.tabAt(event.pos())
        if event.button() == Qt.LeftButton and index >= 0 and not self.isTabEnabled(index):
            self.disabled_clicked.emit(index)
            event.accept()
            return
        super().mousePressEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        colors = dict(self.village_colors)
        if self.flash_steps > 0 and self.flash_steps % 2 == 0:
            colors.setdefault(self.main_village, '#fb923c')
        for index, color in colors.items():
            rect = self.tabRect(index)
            painter.setBrush(QColor(color))
            painter.drawEllipse(rect.right() - 13, rect.top() + 6, 7, 7)


class StatCard(QFrame):
    """Sleek metric card showing an icon, value, and description."""

    def __init__(self, icon: str, title: str, default_val: str = "0", color: str = "#38BDF8",
                 tone: str = "blue"):
        super().__init__()
        self.setObjectName('statCard')
        self.setProperty('tone', tone)
        self.setMinimumHeight(92)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(7)

        header_layout = QHBoxLayout()
        self.icon_lbl = QLabel(icon)
        self.title_lbl = QLabel(title)
        self.title_lbl.setObjectName('statTitle')
        self.icon_lbl.hide()
        header_layout.addWidget(self.title_lbl)
        header_layout.addStretch()

        self.value_lbl = QLabel(default_val)
        self.value_lbl.setObjectName('statValue')

        layout.addLayout(header_layout)
        layout.addWidget(self.value_lbl)

    def set_value(self, val: str):
        if self.value_lbl.text() != val:
            self.value_lbl.setText(val)

class LiveScreenViewer(QLabel):
    """
    Renders live 1600x900 emulator frames smoothly.
    Maintains 16:9 aspect ratio and handles high-resolution scaling.
    """

    def __init__(self):
        super().__init__()
        self.setAlignment(Qt.AlignCenter)
        self.setMinimumSize(480, 270)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setText("Đang chờ kết nối giả lập...")
        self.setObjectName('liveViewer')

    def update_frame(self, cv_img: np.ndarray):
        """Converts OpenCV BGR image to QPixmap and renders with fast C++ SIMD scaling."""
        if cv_img is None:
            return
        try:
            target_size = self.size()
            tw, th = target_size.width(), target_size.height()
            if tw > 20 and th > 20:
                h, w = cv_img.shape[:2]
                scale = min(tw / w, th / h)
                nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
                # OpenCV C++ SIMD resize is ~15x faster than Qt software scaling
                resized = cv2.resize(cv_img, (nw, nh), interpolation=cv2.INTER_NEAREST)
                rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB)
                q_img = QImage(rgb.data, nw, nh, 3 * nw, QImage.Format_RGB888)
                self.setPixmap(QPixmap.fromImage(q_img))
            else:
                rgb = cv2.cvtColor(cv_img, cv2.COLOR_BGR2RGB)
                h, w, ch = cv_img.shape
                q_img = QImage(rgb.data, w, h, ch * w, QImage.Format_RGB888)
                self.setPixmap(QPixmap.fromImage(q_img))
        except Exception:
            pass

class LogConsole(QWidget):
    """Rich colored log viewer with auto-scroll and clear actions."""

    def __init__(self):
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        header = QHBoxLayout()
        title = QLabel("Hoạt động gần đây")
        title.setObjectName('sectionDetail')
        self.clear_btn = QPushButton("Xóa Nhật Ký")
        self.clear_btn.clicked.connect(self.clear_logs)

        header.addWidget(title)
        header.addStretch()
        self.new_logs_btn = QPushButton("↓ Log mới")
        self.new_logs_btn.clicked.connect(self.scroll_to_latest)
        self.new_logs_btn.hide()
        header.addWidget(self.new_logs_btn)
        header.addWidget(self.clear_btn)

        self.text_edit = QPlainTextEdit()
        self.text_edit.setReadOnly(True)
        self.text_edit.setMaximumBlockCount(1000)
        self.text_edit.verticalScrollBar().valueChanged.connect(self._log_scroll_changed)

        layout.addLayout(header)
        layout.addWidget(self.text_edit)

    def append_log(self, text: str):
        scroll = self.text_edit.verticalScrollBar()
        at_bottom = scroll.value() >= scroll.maximum() - 2
        old_value = scroll.value()
        now_str = datetime.now().strftime("%H:%M:%S")
        self.text_edit.appendPlainText(f"[{now_str}] {text}")
        if at_bottom:
            scroll.setValue(scroll.maximum())
            self.new_logs_btn.hide()
        else:
            scroll.setValue(old_value)
            self.new_logs_btn.show()

    def _log_scroll_changed(self, value):
        scroll = self.text_edit.verticalScrollBar()
        if value >= scroll.maximum() - 2:
            self.new_logs_btn.hide()

    def scroll_to_latest(self):
        scroll = self.text_edit.verticalScrollBar()
        scroll.setValue(scroll.maximum())
        self.new_logs_btn.hide()

    def clear_logs(self):
        self.text_edit.clear()
        self.new_logs_btn.hide()


class ResourceItemCard(QFrame):
    """Compact resource metric tile."""
    def __init__(self, label: str, default_val: str, color: str, bg_color: str, border_color: str):
        super().__init__()
        self.setObjectName("villageMetric")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setMinimumHeight(56)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(2)

        self.lbl_title = QLabel(label)
        self.lbl_title.setObjectName('metricTitle')
        self.lbl_val = QLabel(default_val)
        self.lbl_val.setObjectName('metricValue')
        layout.addWidget(self.lbl_title)
        layout.addWidget(self.lbl_val)

    def set_value(self, val: str):
        if self.lbl_val.text() != val:
            self.lbl_val.setText(val)


class VillageLiveStatusWidget(QWidget):
    """Resource readings, live battle progress and enabled village actions."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.viewing_village = 'main'
        self.bot_active_village = 'main'
        self._snapshot = {}
        self._settings = {}
        root = QVBoxLayout(self)
        root.setSizeConstraint(QLayout.SetMinimumSize)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(10)

        header = QHBoxLayout()
        self.village_buttons = {}
        for key, name in (('main', 'Làng chính'), ('builder', 'Làng đêm')):
            button = QPushButton(name)
            button.setCheckable(True)
            button.clicked.connect(lambda checked, village=key: self._select_village(village))
            self.village_buttons[key] = button
            header.addWidget(button)
        header.addStretch()
        self.lbl_bot_state_badge = self._label('', '#94a3b8')
        header.addWidget(self.lbl_bot_state_badge)
        root.addLayout(header)

        resources, res_header = self._section(root, 'Tài nguyên')
        self.lbl_res_time = self._label('Chưa quét', '#94a3b8')
        res_header.addWidget(self.lbl_res_time)
        self.scan_button = QPushButton('Quét')
        self.scan_button.setToolTip('Quét tài nguyên trên màn hình làng đang mở trong MuMu.')
        self.scan_button.setStyleSheet('padding: 3px 10px;')
        res_header.addWidget(self.scan_button)
        row = QHBoxLayout()
        self.resource_cards = {}
        for key, name, color in (
            ('gold', 'Vàng', '#fbbf24'), ('elixir', 'Dầu tím', '#f9a8d4'),
            ('dark_elixir', 'Hắc dầu', '#c4b5fd'), ('gems', 'Gem', '#6ee7b7'),
        ):
            card = ResourceItemCard(name, '—', color, '#202832', '#303b4b')
            card.setProperty('resource', key)
            self.resource_cards[key] = card
            row.addWidget(card, 1)
        self.resource_cards['gems'].setToolTip('Gem dùng chung cho hai làng.')
        resources.addLayout(row)

        battle, battle_header = self._section(root, 'Trận đấu')
        self.lbl_battle_status = self._label('', '#94a3b8')
        battle_header.addWidget(self.lbl_battle_status)
        metrics = QHBoxLayout()
        self.card_live_stars = ResourceItemCard('Sao trận này', '—', '#fbbf24', '#202832', '#303b4b')
        self.card_elapsed = ResourceItemCard('Đã trôi qua', '—', '#e2e8f0', '#202832', '#303b4b')
        metrics.addWidget(self.card_live_stars, 1)
        metrics.addWidget(self.card_elapsed, 1)
        battle.addLayout(metrics)
        self.lbl_atk_strategy = self._label('', '#e2e8f0', wrap=True)
        self.lbl_atk_detail = self._label('', '#94a3b8', wrap=True)
        battle.addWidget(self.lbl_atk_strategy)
        battle.addWidget(self.lbl_atk_detail)

        # Builders & Upgrades section
        builders_sec, b_header = self._section(root, 'Thợ xây & Nâng cấp công trình')
        self.frame_builders = builders_sec.parentWidget()
        self.lbl_bldg_mode_badge = self._label('', '#94a3b8')
        b_header.addWidget(self.lbl_bldg_mode_badge)

        row_bldg = QHBoxLayout()
        self.card_builder_free = ResourceItemCard('Thợ rảnh', '—', '#38bdf8', '#202832', '#303b4b')
        self.card_builder_busy = ResourceItemCard('Đang nâng', '—', '#fbbf24', '#202832', '#303b4b')
        self.card_builder_reserve = ResourceItemCard('Mức duy trì', '—', '#94a3b8', '#202832', '#303b4b')
        self.card_builder_priority = ResourceItemCard('Ưu tiên nâng', '—', '#c084fc', '#202832', '#303b4b')
        row_bldg.addWidget(self.card_builder_free, 1)
        row_bldg.addWidget(self.card_builder_busy, 1)
        row_bldg.addWidget(self.card_builder_reserve, 1)
        row_bldg.addWidget(self.card_builder_priority, 1)
        builders_sec.addLayout(row_bldg)

        self.lbl_builder_active_jobs = self._label('', '#cbd5e1', wrap=True)
        self.lbl_builder_plan = self._label('', '#38bdf8', wrap=True)
        self.lbl_builder_lock = self._label('', '#aeb8c6', wrap=True)
        self.lbl_builders = self.lbl_builder_active_jobs
        builders_sec.addWidget(self.lbl_builder_active_jobs)
        builders_sec.addWidget(self.lbl_builder_plan)
        builders_sec.addWidget(self.lbl_builder_lock)

        activity, _ = self._section(root, 'Tường và tự động hóa')
        self.lbl_wall_breakdown = self._label('', '#cbd5e1', wrap=True)
        self.lbl_features = self._label('', '#94a3b8', wrap=True)
        activity.addWidget(self.lbl_wall_breakdown)
        activity.addWidget(self.lbl_features)
        root.addStretch()
        self._render()

    @staticmethod
    def _label(text, color, *, wrap=False):
        label = QLabel(text)
        label.setTextFormat(Qt.PlainText)
        label.setWordWrap(wrap)
        label.setObjectName('sectionDetail')
        return label

    @staticmethod
    def _set_status_tone(label, tone):
        if label.property('statusTone') != tone:
            label.setProperty('statusTone', tone)
            label.style().unpolish(label)
            label.style().polish(label)

    @classmethod
    def _section(cls, root, title):
        frame = QFrame()
        frame.setObjectName('villageSection')
        layout = QVBoxLayout(frame)
        layout.setSizeConstraint(QLayout.SetMinimumSize)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        header = QHBoxLayout()
        label = cls._label(title, '#e2e8f0')
        label.setObjectName('sectionTitle')
        header.addWidget(label)
        header.addStretch()
        layout.addLayout(header)
        root.addWidget(frame)
        return layout, header

    def _select_village(self, village):
        self.viewing_village = village
        self._render()

    def update_state(self, state_snapshot, settings):
        self._snapshot = state_snapshot
        self._settings = settings
        active_village = state_snapshot.get('active_village', 'main')
        if active_village != self.bot_active_village:
            self.bot_active_village = active_village
            self.viewing_village = active_village
        self._render()

    def _render(self):
        state = self._snapshot
        cfg = self._settings.get
        village = self.viewing_village
        is_main = village == 'main'
        for key, button in self.village_buttons.items():
            button.setChecked(key == village)
        current = 'Làng chính' if self.bot_active_village == 'main' else 'Làng đêm'
        self.lbl_bot_state_badge.setText(f'Làng gần nhất · {current}')
        resources = state.get('home_resources' if is_main else 'builder_resources', {})
        valid = resources.get('valid', {})
        self.resource_cards['gold'].lbl_title.setText('Vàng' if is_main else 'Vàng đêm')
        self.resource_cards['elixir'].lbl_title.setText('Dầu tím' if is_main else 'Dầu đêm')
        self.resource_cards['dark_elixir'].setVisible(is_main)
        for key, card in self.resource_cards.items():
            card.set_value(f"{resources[key]:,}" if valid.get(key) else '—')
        updated_at = resources.get('updated_at', 0)
        age = max(0, int(time.time() - updated_at))
        if not updated_at:
            freshness = 'Đã lưu' if valid else 'Chưa quét'
        elif age < 10:
            freshness = 'Vừa quét'
        elif age < 60:
            freshness = f'{age} giây trước'
        elif age < 3600:
            freshness = f'{age // 60} phút trước'
        else:
            freshness = f'{age // 3600} giờ trước'
        self.lbl_res_time.setText(freshness)

        battle = state.get('live_battle', {}).get(village)
        if battle:
            stage = battle['stage']
            self.lbl_battle_status.setText(f'Đang đánh · Chặng {stage}' if stage else 'Đang đánh')
            self.card_live_stars.set_value(f"{battle['stars']} / {3 if is_main else 6}")
            elapsed = max(0, int(time.monotonic() - battle['started_at']))
            minutes, seconds = divmod(elapsed, 60)
            prefix = '~' if battle.get('recovered') else ''
            self.card_elapsed.set_value(f'{prefix}{minutes:02d}:{seconds:02d}')
        else:
            self.lbl_battle_status.setText('Chưa vào trận')
            self.card_live_stars.set_value('—')
            self.card_elapsed.set_value('—')
        self.card_elapsed.setToolTip(
            'Trận đã diễn ra khi bot tiếp quản. Đồng hồ tính từ lúc tiếp quản.'
            if battle and battle.get('recovered') else 'Thời gian từ lúc bắt đầu trận.'
        )

        attack = state.get('current_attack', {}).get(village, {})
        if is_main:
            strategy = cfg('attack_strategy', 'perimeter')
            label = STRATEGY_NAMES.get(strategy, strategy)
            if strategy == 'random_mix' and battle:
                label = attack.get('strategy_label', label)
            detail = (f"Mục tiêu {cfg('star_target', 1)} sao"
                      if cfg('auto_surrender', True) and cfg('end_on_stars', True)
                      else 'Mục tiêu sao đã tắt')
            detail += f" · {cfg('attack_spread_points', 17)} điểm rải quân"
            enabled = cfg('main_village_enabled', True)
            actions = (
                ('Nâng tường', 'auto_upgrade_walls', True),
                ('Thu mỏ', 'auto_collect', True),
                ('Luyện quân', 'auto_train_army', True),
                ('Xin quân hội', 'auto_request_troops', False),
                ('Giao Phụ tá', 'auto_assign_apprentice', False),
                ('Thu kho bạc', 'auto_collect_treasury', False),
                ('Giữ thợ tường', 'wall_hold_enabled', True),
                ('Hạ cúp', 'trophy_drop_mode', False),
            )
        else:
            combo = attack.get('combo') if battle else cfg('builder_base_combo', 'current')
            label = COMBOS.get(combo, COMBOS['current'])[0]
            reinforcements = [REINFORCEMENT_CHOICES.get(cfg(key, 'current'), REINFORCEMENT_CHOICES['current'])
                              for key in REINFORCEMENT_KEYS]
            detail = 'Tiếp viện chặng 2 · ' + ' / '.join(reinforcements)
            enabled = cfg('builder_base_enabled', False)
            actions = (
                ('Nâng tường', 'builder_base_upgrade_walls', False),
                ('Thu xe dầu', 'builder_base_collect_cart', False),
                ('Thu mỏ Gem', 'builder_base_collect_gem', False),
                ('Thu mỏ vàng và dầu', 'builder_base_collect_mine', False),
                ('Đánh khi có chướng ngại', 'builder_base_obstacle_attack', False),
            )
        self.lbl_atk_strategy.setText(label)
        self.lbl_atk_detail.setText(detail)
        # Render Builders & Upgrades
        self.frame_builders.setVisible(is_main)
        is_maxed = is_main and bool(state.get('walls_confirmed_maxed', False))
        if is_main:
            free = state.get('free_builders')
            total = state.get('total_builders') or 6
            cfg_reserve = int(cfg('reserve_builders', 0) or 0)
            reserve = 0 if is_maxed else cfg_reserve
            auto_bldg = cfg('auto_upgrade_buildings', False)
            lock_th = cfg('lock_townhall_upgrade', True)

            if auto_bldg:
                self.lbl_bldg_mode_badge.setText("● Tự động nâng: Đang BẬT")
                self._set_status_tone(self.lbl_bldg_mode_badge, 'success')
            else:
                self.lbl_bldg_mode_badge.setText("○ Tự động nâng: Đã TẮT")
                self._set_status_tone(self.lbl_bldg_mode_badge, 'muted')

            if free is None:
                self.card_builder_free.set_value("—")
                self.card_builder_busy.set_value("—")
            else:
                self.card_builder_free.set_value(f"{free} / {total}")
                busy = max(0, total - free)
                self.card_builder_busy.set_value(f"{busy} thợ")

            if is_maxed:
                if cfg_reserve > 0:
                    self.card_builder_reserve.set_value("0 thợ (Max tường)")
                    self.card_builder_reserve.setToolTip(f"Cài đặt gốc là {cfg_reserve} thợ, nhưng tường đã Max hoàn toàn nên tự động coi như 0 để sử dụng hết toàn bộ thợ xây khả dụng.")
                else:
                    self.card_builder_reserve.set_value("0 thợ")
                    self.card_builder_reserve.setToolTip("Duy trì 0 thợ dự phòng, sử dụng hết toàn bộ thợ xây khả dụng.")
            else:
                self.card_builder_reserve.set_value(f"{reserve} thợ")

            prio = cfg('building_upgrade_priority', 'time_min')
            prio_labels = {
                'time_min': 'Nhanh nhất',
                'cost_min': 'Rẻ nhất',
                'cost_max': 'Đắt nhất',
                'time_max': 'Lâu nhất',
                'suggested': 'Theo game',
                'specific': 'Cụ thể',
            }
            if prio == 'specific':
                spec_k = cfg('building_upgrade_specific_target', '')
                spec_lbl = spec_k.capitalize() if spec_k else 'Chưa chọn'
                self.card_builder_priority.set_value(f"Cụ thể: {spec_lbl}")
            else:
                self.card_builder_priority.set_value(prio_labels.get(prio, prio))

            ongoing = state.get('ongoing_upgrades', [])
            if ongoing:
                jobs_str = " · ".join(f"{j.get('name')} ({j.get('duration')})" if j.get('duration') else j.get('name') for j in ongoing)
                busy = max(0, total - (free or 0))
                rem_busy = busy - len(ongoing)
                if rem_busy > 0:
                    jobs_str += f" · và {rem_busy} thợ khác"
                self.lbl_builder_active_jobs.setText(f"🔨 Đang nâng: {jobs_str}")
            elif free is not None:
                busy = max(0, total - free)
                if busy > 0:
                    self.lbl_builder_active_jobs.setText(f"🔨 Đang nâng: {busy} thợ đang bận làm việc tại làng.")
                else:
                    self.lbl_builder_active_jobs.setText("✨ Toàn bộ thợ đang rảnh rỗi.")
            else:
                self.lbl_builder_active_jobs.setText("🔨 Trạng thái thợ: Chưa quét menu thợ.")

            b_plan = state.get('builder_plan_summary', '')
            if b_plan:
                self.lbl_builder_plan.setText(f"📋 {b_plan}")
            elif is_maxed:
                self.lbl_builder_plan.setText(
                    "📋 Kế hoạch: Tường đã Max hoàn toàn → Sử dụng hết toàn bộ số thợ xây khả dụng (duy trì 0 thợ dự phòng)."
                )
            elif free is not None and free <= reserve:
                self.lbl_builder_plan.setText(
                    f"📋 Kế hoạch: Đang giữ đủ {reserve} thợ dự phòng (thợ sẵn sàng nâng tường khi dư tài nguyên)."
                )
            elif not auto_bldg:
                self.lbl_builder_plan.setText(
                    "ℹ️ Tự động nâng công trình đang TẮT (vào Cài đặt > Làng chính để bật)."
                )
            else:
                self.lbl_builder_plan.setText("📋 Kế hoạch: Sẵn sàng phân công khi thợ rảnh.")

            if lock_th:
                self.lbl_builder_lock.setText("🛡️ Chốt an toàn: Cấm nâng Nhà chính (Town Hall) [Đang BẬT]")
                self._set_status_tone(self.lbl_builder_lock, 'success')
            else:
                self.lbl_builder_lock.setText("⚠️ Chốt an toàn: Cấm nâng Nhà chính đã TẮT (có thể nâng TH)")
                self._set_status_tone(self.lbl_builder_lock, 'danger')
        walls = state.get('wall_inventory', {}).get(village, {})
        th_level = state.get('th_level')
        max_allowed = (th_level if th_level <= 8 else th_level + 1) if (isinstance(th_level, int) and th_level >= 2) else None
        int_walls = {}
        for k, v in (walls.items() if isinstance(walls, dict) else ()):
            try:
                lvl = int(k)
                cnt = int(v)
                if max_allowed is not None and lvl > max_allowed:
                    continue
                int_walls[lvl] = cnt
            except (ValueError, TypeError):
                pass
        walls = int_walls

        if is_maxed:
            target_lvl = max_allowed or (max(walls.keys()) if walls else 17)
            total_walls = walls.get(target_lvl, sum(walls.values()) if walls else 325)
            if total_walls > 325:
                total_walls = 325
            th_str = f" (TH{th_level})" if th_level else ""
            self.lbl_wall_breakdown.setText(
                f"🏰 Tường: Toàn bộ {total_walls} đoạn đã đạt Cấp {target_lvl}{th_str} · ĐÃ MAX HOÀN TOÀN ✅\n"
                f"⚡ Trạng thái thợ: Sử dụng hết toàn bộ số thợ xây khả dụng (duy trì 0 thợ dự phòng)"
            )
            self._set_status_tone(self.lbl_wall_breakdown, 'success')
        elif walls:
            levels = ' · '.join(f'Cấp {level} · {walls[level]:,} đoạn' for level in sorted(walls))
            self.lbl_wall_breakdown.setText(f'Tường đã thấy · {levels}')
            self._set_status_tone(self.lbl_wall_breakdown, 'normal')
        else:
            self.lbl_wall_breakdown.setText('Tường · Chưa có dữ liệu')
            self._set_status_tone(self.lbl_wall_breakdown, 'muted')

        if is_maxed:
            actions = tuple(
                (('Nâng tường (Đã Max)', key, default) if key == 'auto_upgrade_walls' else (name, key, default))
                for name, key, default in actions
                if key != 'wall_hold_enabled'
            )

        active = ' · '.join(name for name, key, default in actions if cfg(key, default))
        self.lbl_features.setText((active or 'Chưa bật tác vụ phụ') if enabled else 'Tự động hóa đã tắt')
