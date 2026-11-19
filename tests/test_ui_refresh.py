"""Regression checks for appearance and wheel-safe settings controls."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from PyQt5.QtCore import QPoint, QPointF, Qt
from PyQt5.QtGui import QFont, QFontDatabase, QRawFont, QWheelEvent
from PyQt5.QtWidgets import QApplication, QScrollArea, QSystemTrayIcon, QVBoxLayout, QWidget

from config.settings import BotSettings
from ui.activity import activity_from_log
from ui.styles import FONT_FILES, APP_ICON_ICO, APP_ICON_PNG, theme_qss
from ui.widgets import BubbleTabBar, NoWheelComboBox, NoWheelDateTimeEdit, NoWheelSpinBox
import ui.main_window as ui


class AppearanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_theme_and_text_size_persist_with_config(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = str(Path(directory) / 'config.json')
            settings = BotSettings(path)
            self.assertEqual(settings.get('ui_theme'), 'dark')
            settings.set('ui_theme', 'light')
            settings.set('ui_text_size', 'large')
            restored = BotSettings(path)
            self.assertEqual(restored.get('ui_theme'), 'light')
            self.assertEqual(restored.get('ui_text_size'), 'large')
            self.assertIn('#eff7ff', theme_qss('light', 'large'))
            self.assertIn('font-size: 16px', theme_qss('light', 'large'))
            self.assertIn('QLabel#sectionDetail, QLabel#metricTitle, QLabel#statTitle { color: #254766; font-weight: 600; }',
                          theme_qss('light', 'medium'))
            self.assertIn('QLabel#sectionDetail[statusTone="normal"] { color: #23425f; font-weight: 600; }',
                          theme_qss('light', 'medium'))
            for resource in ('gold', 'elixir', 'dark_elixir', 'gems'):
                self.assertIn(f'resource="{resource}"', theme_qss('dark', 'medium'))
            for tone in ('blue', 'amber', 'gold', 'elixir', 'dark_elixir', 'green'):
                self.assertIn(f'QFrame#statCard[tone="{tone}"]', theme_qss('dark', 'medium'))
                self.assertIn(f'QFrame#statCard[tone="{tone}"]', theme_qss('light', 'medium'))
            self.assertIn('#b7f52b', theme_qss('dark', 'medium'))

    def test_bundled_fonts_cover_vietnamese(self):
        for path in FONT_FILES:
            self.assertTrue(path.is_file())
            font_id = QFontDatabase.addApplicationFont(str(path))
            self.assertGreaterEqual(font_id, 0)
            family = QFontDatabase.applicationFontFamilies(font_id)[0]
            raw = QRawFont.fromFont(QFont(family, 14))
            self.assertTrue(raw.isValid())
            for char in 'Đặng Nguyễn Tiếng Việt nữ tướng':
                self.assertTrue(raw.supportsCharacter(ord(char)), (family, char))

    def test_wheel_scrolls_page_without_editing_value(self):
        scroll = QScrollArea()
        scroll.resize(240, 180)
        page = QWidget()
        layout = QVBoxLayout(page)
        widgets = [NoWheelSpinBox(), NoWheelComboBox(), NoWheelDateTimeEdit()]
        widgets[1].addItems(['Một', 'Hai'])
        for widget in widgets:
            layout.addWidget(widget)
            layout.addSpacing(350)
        scroll.setWidget(page)
        scroll.show()
        self.app.processEvents()
        for widget in widgets:
            bar = scroll.verticalScrollBar()
            bar.setValue(0)
            old = (widget.value() if isinstance(widget, NoWheelSpinBox)
                   else widget.currentIndex() if isinstance(widget, NoWheelComboBox)
                   else widget.dateTime())
            event = QWheelEvent(QPointF(8, 8), QPointF(8, 8), QPoint(0, 0),
                                QPoint(0, -120), Qt.NoButton, Qt.NoModifier,
                                Qt.ScrollUpdate, False)
            QApplication.sendEvent(widget, event)
            new = (widget.value() if isinstance(widget, NoWheelSpinBox)
                   else widget.currentIndex() if isinstance(widget, NoWheelComboBox)
                   else widget.dateTime())
            self.assertEqual(new, old)
            self.assertGreater(bar.value(), 0)
        scroll.close()

    def test_number_fields_show_and_accept_grouped_values(self):
        spin = NoWheelSpinBox()
        spin.setRange(0, 30_000_000)
        spin.setValue(1_000_000)
        self.assertEqual(spin.text(), '1,000,000')
        spin.lineEdit().setText('2,500,000')
        spin.interpretText()
        self.assertEqual(spin.value(), 2_500_000)

    def test_activity_summaries_ignore_diagnostic_log_lines(self):
        samples = (
            ('[Search] Tài nguyên chưa đạt điều kiện -> Bấm Next tìm nhà khác...',
             ('main', 'search', 'Đang tìm đối thủ phù hợp')),
            ('[BH] Đang quét toàn bộ bảng thợ để tìm nhóm tường cấp thấp nhất.',
             ('builder', 'wall', 'Đang tìm tường cấp thấp nhất')),
            ('[Thợ] Có 2 thợ rảnh; đang tìm công trình nâng cấp.',
             ('main', 'building', 'Đang tìm công trình thỏa điều kiện')),
            ('[Deploy] Balloon: 3 điểm đã giảm số quân; còn 2.',
             ('main', 'battle', 'Đã thả quân Balloon')),
            ('[Spell] Rage tại (234, 567), theo cụm quân đã nhận diện; còn 1.',
             ('main', 'battle', 'Đã thả phép Rage')),
        )
        for log, expected in samples:
            with self.subTest(log=log):
                self.assertEqual(activity_from_log(log)[:3], expected)
        self.assertIsNone(activity_from_log(
            '[Wall] Chưa sẵn sàng bấm Upgrade: tiêu đề/nhóm đang chuyển trạng thái (cấp đọc 5, cần 6).'))
        self.assertIsNone(activity_from_log(
            '[Combat] Chiến thuật four_corners; 14 điểm ngoài ranh đỏ.'))

    def test_activity_button_keeps_milestone_during_diagnostic_logs(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            with patch.object(ui, 'BotSettings', side_effect=lambda: BotSettings(str(Path(directory) / 'config.json'))):
                window = ui.MainWindow()
            try:
                window._on_log('[BH] Đang quét toàn bộ bảng thợ để tìm nhóm tường cấp thấp nhất.')
                self.assertEqual(window._activity_text, 'Đang tìm tường cấp thấp nhất')
                self.assertEqual(window._activity_village, 'builder')
                window._on_log('[Wall] Chưa sẵn sàng bấm Upgrade: tiêu đề/nhóm đang chuyển trạng thái.')
                self.assertEqual(window._activity_text, 'Đang tìm tường cấp thấp nhất')
                self.assertIn('Đang quét toàn bộ', window.activity_btn.toolTip())
            finally:
                window.close()

    def test_run_button_clock_blinks_and_freezes_on_pause(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            config_path = str(Path(directory) / 'config.json')
            with patch.object(ui, 'BotSettings', side_effect=lambda: BotSettings(config_path)):
                window = ui.MainWindow()
            try:
                self.assertEqual(window.tabs.count(), 6)
                window.bot.is_running = True
                window._session_started_at = time.monotonic() - 65
                window._control_frame = 0
                window._sync_controls()
                self.assertRegex(window.btn_start.text(), r'^00:01:0[5-6]$')
                window._control_frame = 1
                window._sync_controls()
                self.assertNotIn(':', window.btn_start.text())
                window.bot.is_paused = True
                window._sync_controls()
                paused_text = window.btn_start.text()
                self.assertTrue(window.btn_start.property('paused'))
                window._sync_controls()
                self.assertEqual(window.btn_start.text(), paused_text)
                window.bot.is_paused = False
                window._sync_controls()
                self.assertFalse(window.btn_start.property('paused'))
            finally:
                window.bot.is_running = False
                window.close()

    def test_dependent_options_keep_parallel_choices_independent(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            with patch.object(ui, 'BotSettings', side_effect=lambda: BotSettings(str(Path(directory) / 'config.json'))):
                window = ui.MainWindow()
            try:
                window.chk_walls.setChecked(False)
                self.assertFalse(window.spin_wall_gold.isEnabled())
                self.assertFalse(window.chk_wall_lowest.isEnabled())
                self.assertTrue(window.chk_farm_finish.isEnabled())
                window.chk_auto_buildings.setChecked(False)
                self.assertFalse(window.combo_bldg_priority.isEnabled())
                self.assertTrue(window.chk_buy_new.isEnabled())
                window.chk_upgrade_army.setChecked(False)
                self.assertFalse(window.army_priority.isEnabled())
                window.chk_upgrade_army.setChecked(True)
                self.assertTrue(window.army_priority.isEnabled())
                window.chk_auto_surr.setChecked(False)
                self.assertFalse(window.chk_end_stars.isEnabled())
                self.assertFalse(window.spin_stars.isEnabled())
                window.chk_auto_surr.setChecked(True)
                window.chk_end_stars.setChecked(True)
                self.assertTrue(window.spin_stars.isEnabled())
                window.chk_main_village.setChecked(False)
                self.assertFalse(window.chk_walls.isEnabled())
                window.chk_main_village.setChecked(True)
                self.assertTrue(window.chk_walls.isEnabled())
            finally:
                window.close()

    def test_narrow_and_compact_window_keep_navigation(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            with patch.object(ui, 'BotSettings', side_effect=lambda: BotSettings(str(Path(directory) / 'config.json'))):
                window = ui.MainWindow()
            try:
                window.show()
                self.app.processEvents()
                self.assertIsInstance(window.overview_nav, BubbleTabBar)
                self.assertEqual(window.card_gold.property('tone'), 'gold')
                self.assertEqual(window.card_elixir_hour.property('tone'), 'elixir')
                self.assertIn(window.village_status.lbl_wall_breakdown.property('statusTone'),
                              ('normal', 'muted', 'success'))
                self.assertLessEqual(
                    window.overview_nav.mapTo(window.tabs, QPoint(0, 0)).y()
                    - window.tabs.tabBar().height(), 12)
                self.assertEqual(window.navigation_scroll.horizontalScrollBar().maximum(), 0)
                self.assertEqual(window.device_subtabs.count(), 4)
                self.assertEqual(window.navigation_layout.indexOf(window.btn_to_tray), -1)
                self.assertEqual(window.device_row.y(), 0)
                window.resize(720, 500)
                self.app.processEvents()
                self.assertEqual(window.width(), 1040)
                self.assertEqual(window.height(), 620)
                self.assertTrue(window.navigation_scroll.isVisible())
                self.assertGreater(window.activity_btn.width(), 180)
                self.assertTrue(window.device_row.isVisible())
                window.tabs.setCurrentIndex(window.main_village_tab)
                for index in range(window.village_subtabs.count()):
                    window.village_subtabs.setCurrentIndex(index)
                    self.app.processEvents()
                    self.assertEqual(window.village_subtabs.currentWidget().horizontalScrollBar().maximum(), 0)
                window.village_subtabs.setCurrentIndex(1)
                page_scroll = window.village_subtabs.currentWidget()
                pinned_y = window.village_subtabs.tabBar().mapTo(window.tabs, QPoint(0, 0)).y()
                page_scroll.verticalScrollBar().setValue(page_scroll.verticalScrollBar().maximum())
                self.app.processEvents()
                self.assertEqual(window.village_subtabs.tabBar().mapTo(window.tabs, QPoint(0, 0)).y(), pinned_y)
                for tab_index, subtabs in ((window.main_attack_tab, window.combat_subtabs),
                                           (window.builder_village_tab, window.builder_subtabs),
                                           (window.device_tab, window.device_subtabs),
                                           (5, window.schedule_subtabs)):
                    window.tabs.setCurrentIndex(tab_index)
                    subtabs.setCurrentIndex(0)
                    self.app.processEvents()
                    fixed_y = subtabs.tabBar().mapTo(window.tabs, QPoint(0, 0)).y()
                    content_scroll = subtabs.currentWidget()
                    self.assertIsInstance(content_scroll, QScrollArea)
                    content_scroll.verticalScrollBar().setValue(content_scroll.verticalScrollBar().maximum())
                    self.app.processEvents()
                    self.assertEqual(subtabs.tabBar().mapTo(window.tabs, QPoint(0, 0)).y(), fixed_y)
                window.tabs.setCurrentIndex(window.main_village_tab)
                window.btn_compact.setChecked(True)
                self.app.processEvents()
                self.assertEqual(window.minimumSize(), window.maximumSize())
                self.assertFalse(window.device_row.isVisible())
                self.assertTrue(window.tabs.isVisible())
                self.assertFalse(window.btn_to_tray.isVisible())
                window.btn_compact.setChecked(False)
                self.app.processEvents()
                self.assertTrue(window.device_row.isVisible())
                self.assertTrue(window.overview_title_label.isHidden())
                self.assertTrue(window.overview_hint_label.isHidden())
                window.combo_text_size.setCurrentIndex(2)
                self.app.processEvents()
                self.assertGreaterEqual(window.minimumWidth(), 1040)
                window.resize(720, 500)
                window.village_subtabs.setCurrentIndex(2)
                self.app.processEvents()
                self.assertEqual(window.village_subtabs.currentWidget().horizontalScrollBar().maximum(), 0)
            finally:
                window.close()

    def test_tray_restore_and_subtab_activity_dots(self):
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            with patch.object(ui, 'BotSettings', side_effect=lambda: BotSettings(str(Path(directory) / 'config.json'))):
                window = ui.MainWindow()
            try:
                window.show()
                self.app.processEvents()
                window.chk_walls.setChecked(False)
                window.chk_farm_finish.setChecked(False)
                self.assertNotIn(1, window.village_subtabs.tabBar().active_dots)
                window.chk_farm_finish.setChecked(True)
                self.assertNotIn(1, window.village_subtabs.tabBar().active_dots)
                window.chk_farm_finish.setChecked(False)
                window.chk_walls.setChecked(True)
                self.assertIn(1, window.village_subtabs.tabBar().active_dots)
                window.bot.village.walls_confirmed_maxed = True
                window._update_farm_finish_badge()
                self.assertNotIn(1, window.village_subtabs.tabBar().active_dots)
                window.bot.village.walls_confirmed_maxed = False
                window.chk_farm_finish.setChecked(False)
                window._update_farm_finish_badge()
                self.assertIn(1, window.village_subtabs.tabBar().active_dots)
                window.chk_auto_buildings.setChecked(False)
                window.chk_buy_new.setChecked(True)
                self.assertIn(2, window.village_subtabs.tabBar().active_dots)
                window.chk_main_village.setChecked(False)
                self.assertEqual(window.village_subtabs.tabBar().active_dots, {})
                window.chk_builder_village.setChecked(True)
                window.builder_tasks['builder_base_upgrade_walls'].setChecked(True)
                self.assertIn(1, window.builder_subtabs.tabBar().active_dots)
                with patch.object(QSystemTrayIcon, 'isSystemTrayAvailable', return_value=True):
                    window._minimize_to_tray()
                self.assertTrue(window.isHidden())
                self.assertFalse(self.app.quitOnLastWindowClosed())
                window._restore_from_tray()
                self.assertTrue(window.isVisible())
                with patch.object(ui, 'configure_startup') as configure:
                    index = window.combo_startup_mode.findData('mumu')
                    window.combo_startup_mode.setCurrentIndex(index)
                    configure.assert_called_once_with('mumu')
                    self.assertEqual(window.settings.get('ui_startup_mode'), 'mumu')
                window.combo_close_behavior.setCurrentIndex(window.combo_close_behavior.findData('tray'))
                with patch.object(QSystemTrayIcon, 'isSystemTrayAvailable', return_value=True):
                    window.close()
                self.assertTrue(window.isHidden())
                self.assertFalse(window._closing)
                window._exit_completely()
                self.assertTrue(window._closing)
            finally:
                window._exit_completely()
                self.assertTrue(self.app.quitOnLastWindowClosed())

    def test_app_icon_files_and_window_icon_loaded(self):
        self.assertTrue(APP_ICON_ICO.is_file())
        self.assertTrue(APP_ICON_PNG.is_file())
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            with patch.object(ui, 'BotSettings', side_effect=lambda: BotSettings(str(Path(directory) / 'config.json'))):
                window = ui.MainWindow()
            try:
                self.assertFalse(window.windowIcon().isNull())
            finally:
                window._exit_completely()

    def test_overview_session_deltas_and_rph_peaks(self):
        from core.recent_rph import append_battle, update_peak_rates
        from core.stats_store import StatsStore

        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            config_path = Path(directory) / 'config.json'
            stats_path = Path(directory) / 'stats.json'
            store = StatsStore(str(stats_path))
            seed = {
                'battles_completed': 100,
                'builder_battles_completed': 10,
                'uptime_seconds': 3600,
                'builder_uptime_seconds': 600,
                'gold_looted': 10_000_000,
                'elixir_looted': 8_000_000,
                'dark_looted': 50_000,
                'rph_updated_uptime_seconds': 3000,
                'rph_window_start_uptime_seconds': 2940,
                'rph_window_version': 2,
                'rph_village_start_battle': 99,
                'rph_history': [
                    {'battle': 100, 'uptime_seconds': 3000, 'elapsed_seconds': 60,
                     'gold': 500_000, 'elixir': 400_000, 'dark_elixir': 3_000},
                ],
                'max_gold_rph': 30_000_000,
                'max_elixir_rph': 24_000_000,
                'max_dark_rph': 180_000,
            }
            store.save(seed)

            with patch.object(ui, 'BotSettings', side_effect=lambda: BotSettings(str(config_path))):
                window = ui.MainWindow()
            try:
                # Initially session deltas are 0 and peaks reflect stored records
                self.assertEqual(window.card_total_battles.sub_lbl.text(), 'Phiên: +0')
                self.assertEqual(window.card_main_total.sub_lbl.text(), 'Phiên: +0')
                self.assertEqual(window.card_builder_total.sub_lbl.text(), 'Phiên: +0')
                self.assertEqual(window.card_uptime.sub_lbl.text(), 'Phiên: +00:00')
                self.assertEqual(window.card_gold.sub_lbl.text(), 'Phiên: +0')
                self.assertEqual(window.card_elixir.sub_lbl.text(), 'Phiên: +0')
                self.assertEqual(window.card_dark.sub_lbl.text(), 'Phiên: +0')
                self.assertEqual(window.card_gold_hour.sub_lbl.text(), 'Kỷ lục: 30,000,000')
                self.assertEqual(window.card_elixir_hour.sub_lbl.text(), 'Kỷ lục: 24,000,000')
                self.assertEqual(window.card_dark_hour.sub_lbl.text(), 'Kỷ lục: 180,000')
                self.assertIn('QLabel#statSub', theme_qss('dark', 'medium'))
                self.assertIn('QLabel#statSub', theme_qss('light', 'large'))

                # Simulate progress in this session with a slower battle (lower current RPH, peak preserved)
                updated = window.bot.get_stats()
                updated['battles_completed'] += 1
                updated['builder_battles_completed'] += 2
                updated['uptime_seconds'] += 125
                updated['gold_looted'] += 250_000
                updated['elixir_looted'] += 180_000
                updated['dark_looted'] += 1_500
                append_battle(updated, {'gold': 250_000, 'elixir': 180_000, 'dark_elixir': 1_500}, 3120)
                update_peak_rates(updated)
                window._on_stats(updated)

                self.assertEqual(window.card_total_battles.sub_lbl.text(), 'Phiên: +3')
                self.assertEqual(window.card_main_total.sub_lbl.text(), 'Phiên: +1')
                self.assertEqual(window.card_builder_total.sub_lbl.text(), 'Phiên: +2')
                self.assertEqual(window.card_uptime.sub_lbl.text(), 'Phiên: +02:05')
                self.assertEqual(window.card_gold.sub_lbl.text(), 'Phiên: +250,000')
                self.assertEqual(window.card_elixir.sub_lbl.text(), 'Phiên: +180,000')
                self.assertEqual(window.card_dark.sub_lbl.text(), 'Phiên: +1,500')
                # Peak RPH must not drop below 30,000,000 / 24,000,000 / 180,000
                self.assertEqual(window.card_gold_hour.sub_lbl.text(), 'Kỷ lục: 30,000,000')
                self.assertEqual(window.card_elixir_hour.sub_lbl.text(), 'Kỷ lục: 24,000,000')
                self.assertEqual(window.card_dark_hour.sub_lbl.text(), 'Kỷ lục: 180,000')

                # Simulate a faster battle pushing peak RPH higher and verify persistence
                updated['battles_completed'] += 1
                append_battle(updated, {'gold': 5_000_000, 'elixir': 4_000_000, 'dark_elixir': 30_000}, 3150)
                update_peak_rates(updated)
                self.assertGreater(updated['max_gold_rph'], 30_000_000)
                store.save(updated)
                reloaded = store.load(updated)
                self.assertEqual(reloaded['max_gold_rph'], updated['max_gold_rph'])
                window._on_stats(updated)
                self.assertEqual(window.card_gold_hour.sub_lbl.text(), f"Kỷ lục: {updated['max_gold_rph']:,}")
            finally:
                window._exit_completely()


if __name__ == '__main__':
    unittest.main()
