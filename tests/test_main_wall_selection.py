import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import numpy as np

from bot.walls import WallUpgrader
from bot.wall_guard import WallFarmGuard, wall_thresholds
from bot.bot_engine import BotEngine
from config.settings import DEFAULT_CONFIG
from core.vision import VisionEngine


class MainWallSelectionTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            {'cost': 37500, 'upgrade_level': 8, 'available': 1},
            {'cost': 25000, 'upgrade_level': 7, 'available': 9},
            {'cost': 10000, 'upgrade_level': 5, 'available': 289},
        ]

    def test_lowest_first_uses_verified_layout_level(self):
        self.assertIs(WallUpgrader.choose_wall_candidate(self.rows, True, 4, 289), self.rows[2])

    def test_unchecked_uses_first_ocr_wall(self):
        self.assertIs(WallUpgrader.choose_wall_candidate(self.rows, False, 4), self.rows[0])

    def test_unchecked_stops_scan_at_first_wall_without_badge_ocr(self):
        wall = WallUpgrader.__new__(WallUpgrader)
        wall._selection_deadline = None
        wall.manager = SimpleNamespace(_wall_menu_offset=0)
        wall.adb = SimpleNamespace(screencap=Mock(return_value=object()))
        wall.menu_words = Mock(return_value=[('wall', 1200, 350)])
        wall.menu_rows = Mock(return_value=[self.rows[0]])
        wall.scroll_menu = Mock()
        result = wall.scan_wall_rows(first_match=True)
        self.assertEqual(result, [self.rows[0]])
        wall.menu_rows.assert_called_once_with(wall.adb.screencap.return_value,
                                               [('wall', 1200, 350)], read_level=False)
        wall.scroll_menu.assert_not_called()

    def test_verified_count_stops_scan_when_target_is_seen(self):
        wall = WallUpgrader.__new__(WallUpgrader)
        wall._selection_deadline = None
        wall.manager = SimpleNamespace(_wall_menu_offset=0)
        wall.adb = SimpleNamespace(screencap=Mock(return_value=object()))
        wall.menu_words = Mock(side_effect=[[('page one', 0, 0)], [('page two', 0, 0)]])
        wrong = {'cost': 100000, 'available': 58}
        target = {'cost': 50000, 'available': 242}
        wall.menu_rows = Mock(side_effect=[[wrong], [target]])
        wall.scroll_menu = Mock()
        self.assertEqual(wall.scan_wall_rows(target_available=242), [wrong, target])
        wall.scroll_menu.assert_called_once_with('down')

    def test_scan_reads_page_after_sixth_swipe(self):
        wall = WallUpgrader.__new__(WallUpgrader)
        wall._selection_deadline = None
        wall.manager = SimpleNamespace(_wall_menu_offset=0)
        wall.adb = SimpleNamespace(screencap=Mock(return_value=object()))
        wall.menu_words = Mock(side_effect=[[(f'page {i}', 0, 0)] for i in range(7)])
        wrong = {'cost': 100000, 'available': 58}
        target = {'cost': 50000, 'available': 242}
        wall.menu_rows = Mock(side_effect=[[wrong]] * 6 + [[target]])
        wall.scroll_menu = Mock()
        self.assertIs(wall.scan_wall_rows(target_available=242)[-1], target)
        self.assertEqual(wall.scroll_menu.call_count, 6)

    def test_verified_target_scan_crosses_page_without_wall_rows(self):
        wall = WallUpgrader.__new__(WallUpgrader)
        wall._selection_deadline = None
        wall.manager = SimpleNamespace(_wall_menu_offset=0)
        wall.adb = SimpleNamespace(screencap=Mock(return_value=object()))
        wall.menu_words = Mock(side_effect=[[(f'page {i}', 0, 0)] for i in range(3)])
        target = {'cost': 50000, 'available': 242}
        wall.menu_rows = Mock(side_effect=[[{'cost': 100000, 'available': 58}], [], [target]])
        wall.scroll_menu = Mock()
        self.assertIs(wall.scan_wall_rows(target_available=242)[-1], target)
        self.assertEqual(wall.scroll_menu.call_count, 2)

    def test_unreadable_badges_still_require_verified_count(self):
        rows = [dict(row, upgrade_level=None) for row in self.rows]
        self.assertIs(WallUpgrader.choose_wall_candidate(rows, True, 4, 289), rows[2])
        self.assertIsNone(WallUpgrader.choose_wall_candidate(rows, True, 4, 288))

    def test_verified_count_beats_bad_badge_ocr(self):
        rows = [
            {'cost': 100000, 'upgrade_level': 9, 'available': 58},
            {'cost': 50000, 'upgrade_level': 19, 'available': 242},
        ]
        self.assertIs(WallUpgrader.choose_wall_candidate(rows, True, 8, 242), rows[1])

    def test_verified_layout_level_recovers_noisy_selected_wall_title(self):
        wall = WallUpgrader.__new__(WallUpgrader)
        wall._selection_deadline = None
        wall._selection_metrics = None
        wall._selection_log = Mock()
        wall.settings = {'wall_lowest_first': True}
        frames = [object(), object()]
        wall.adb = SimpleNamespace(tap=Mock(), screencap=Mock(side_effect=frames))
        wall.stable_menu_row = Mock(side_effect=lambda row: row)
        wall.record_wall_tap = Mock()
        wall.save_wall_frame = Mock()
        wall.is_menu_open = Mock(return_value=False)
        wall.level = Mock(return_value=None)
        wall.wall_title_present = Mock(return_value=True)
        wall.wall_controls_present = Mock(return_value=True)
        row = {'x': 1295, 'y': 312, 'cost': 500000, 'available': 220}

        with patch('bot.walls.time.sleep'):
            selected, level = wall.choose_row(row, verified_level=11)

        self.assertIs(selected, frames[1])
        self.assertEqual(level, 11)
        self.assertEqual(wall._menu_selection_evidence['available'], 220)
        wall.adb.tap.assert_called_once()

    def test_verified_level_does_not_replace_missing_wall_title(self):
        wall = WallUpgrader.__new__(WallUpgrader)
        wall._selection_deadline = None
        wall._selection_metrics = None
        wall._selection_log = Mock()
        wall.settings = {'wall_lowest_first': True}
        wall.adb = SimpleNamespace(tap=Mock(), screencap=Mock(return_value=object()))
        wall.stable_menu_row = Mock(side_effect=lambda row: row)
        wall.record_wall_tap = Mock()
        wall.save_wall_frame = Mock()
        wall.is_menu_open = Mock(return_value=False)
        wall.level = Mock(return_value=None)
        wall.wall_title_present = Mock(return_value=False)
        wall.wall_controls_present = Mock(return_value=True)
        row = {'x': 1295, 'y': 312, 'cost': 500000, 'available': 220}

        with patch('bot.walls.time.sleep'):
            selected, level = wall.choose_row(row, verified_level=11)

        self.assertIsNone(selected)
        self.assertIsNone(level)

    def test_prepare_upgrade_accepts_truncated_level_only_with_exact_live_price(self):
        wall = WallUpgrader.__new__(WallUpgrader)
        wall.settings = {'wall_quantity_button_mode': 'fixed'}
        wall.adb = SimpleNamespace(screencap=Mock(return_value=object()))
        wall._menu_selection_evidence = {'level': 11, 'cost': 500000, 'available': 206}
        wall.is_menu_open = Mock(return_value=False)
        wall.selection_level = Mock(return_value=1)
        wall.wall_title_present = Mock(return_value=True)
        wall.wall_controls_present = Mock(return_value=True)
        wall.upgrade_price = Mock(return_value=(1000000, False))
        wall.wall_template = Mock(return_value=(1147, 650, 30, 30, .9))
        wall.price_is_red = Mock(return_value=False)
        wall.fixed_upgrade_point = Mock(return_value=(1147, 747))

        self.assertEqual(wall.prepare_upgrade('elixir', 11, 2, 1000000), (1147, 747))
        self.assertTrue(wall._prepare_level_fallback)

        wall.upgrade_price.return_value = (500000, False)
        wall.save_wall_frame = Mock()
        with patch('bot.walls.time.sleep'):
            self.assertIsNone(wall.prepare_upgrade('elixir', 11, 2, 1000000))
        wall.fixed_upgrade_point.assert_called_once()

    def test_failed_selection_with_full_resources_keeps_village_hold(self):
        guard = WallFarmGuard()
        settings = {'auto_upgrade_walls': True, 'wall_hold_enabled': True,
                    'wall_resource': 'both', 'min_gold_to_upgrade_wall': 1000000,
                    'min_elixir_to_upgrade_wall': 1000000}
        readings = {'gold': 9000000, 'elixir': 9000000,
                    'valid': {'gold': True, 'elixir': True}}
        self.assertTrue(guard.observe(settings, readings, {'status': 'selection_failed'}))
        self.assertTrue(guard.active)

    def test_unreachable_wall_threshold_uses_measured_near_full_capacity(self):
        settings = {'wall_resource': 'both', 'min_gold_to_upgrade_wall': 10000000,
                    'min_elixir_to_upgrade_wall': 10000000,
                    'auto_upgrade_walls': True, 'wall_hold_enabled': True}
        capacities = {'gold': 5518000, 'elixir': 5537000}
        details = wall_thresholds(settings, capacities)
        self.assertEqual(details['gold']['effective'], 5490410)
        self.assertEqual(details['elixir']['effective'], 5509315)
        self.assertTrue(details['gold']['capped'])

        guard = WallFarmGuard()
        full = {'gold': 5515800, 'elixir': 5537000,
                'valid': {'gold': True, 'elixir': True}}
        self.assertTrue(guard.observe(settings, full, {'status': 'below_threshold'}, capacities))
        self.assertEqual(guard.resources, {'gold', 'elixir'})
        after_upgrade = {'gold': 4515800, 'elixir': 4537000,
                         'valid': {'gold': True, 'elixir': True}}
        self.assertTrue(guard.observe(settings, after_upgrade, {'status': 'upgraded'}, capacities))
        self.assertFalse(guard.observe(settings, after_upgrade, {'status': 'upgraded'}, capacities))

    def test_wall_run_checks_builder_when_balance_reaches_effective_threshold(self):
        wall = WallUpgrader.__new__(WallUpgrader)
        wall.settings = {'auto_upgrade_walls': True, 'wall_resource': 'both',
                         'min_gold_to_upgrade_wall': 10000000,
                         'min_elixir_to_upgrade_wall': 10000000}
        wall.manager = SimpleNamespace(wall_status='', wall_result={},
            get_account_capacities=lambda names: {'gold': 5518000, 'elixir': 5537000},
            set_wall_wait=Mock())
        wall.builders = Mock(return_value=0)
        wall.wait_for_builder = Mock()
        wall.paid_builder_visible = False
        screen = np.zeros((900, 1600, 3), dtype=np.uint8)
        self.assertFalse(wall.run(screen, 5515800, 5537000))
        wall.builders.assert_called_once_with(screen)
        self.assertEqual(wall.manager.wall_result['status'], 'waiting_builder')

    def test_full_storage_holds_village_and_retries_wall_before_battle(self):
        engine = BotEngine.__new__(BotEngine)
        engine.settings = {'auto_upgrade_walls': True, 'wall_hold_enabled': True,
                           'wall_resource': 'both', 'min_gold_to_upgrade_wall': 10000000,
                           'min_elixir_to_upgrade_wall': 10000000}
        engine._wall_farm_guard = WallFarmGuard()
        engine.log = Mock()
        engine.set_state = Mock()
        screen = object()
        engine.village = SimpleNamespace(
            walls_confirmed_maxed=False,
            wall_result={'status': 'below_threshold', 'reason': 'raw threshold unreachable'},
            wall_status='raw threshold unreachable', _wall_builder_retry_at=0,
            is_in_main_village=lambda frame: True,
            get_account_capacities=lambda names: {'gold': 5518000, 'elixir': 5537000},
            upgrade_walls=Mock())
        engine.vision = SimpleNamespace(read_village_resources=lambda frame: {
            'gold': 5515800, 'elixir': 5537000,
            'valid': {'gold': True, 'elixir': True}})

        self.assertTrue(engine._wall_hold_gate(screen))
        engine.village.upgrade_walls.assert_called_once()
        self.assertEqual(engine.village.upgrade_walls.call_args.args[:3],
                         (screen, 5515800, 5537000))

    def test_unreadable_capacity_retries_at_bounded_interval(self):
        engine = BotEngine.__new__(BotEngine)
        engine.village = SimpleNamespace(get_account_capacities=Mock(return_value=None))
        reader = Mock(return_value=None)
        engine.farm_finish = SimpleNamespace(capacity_reader=SimpleNamespace(read=reader))
        engine.log = Mock()
        with patch('bot.bot_engine.time.monotonic', return_value=100):
            self.assertIsNone(engine._wall_storage_capacities())
            self.assertIsNone(engine._wall_storage_capacities())
        reader.assert_called_once()
        self.assertEqual(engine._wall_capacity_retry_at, 130)

    def test_unbound_capacity_read_does_not_unlock_battle(self):
        engine = BotEngine.__new__(BotEngine)
        engine.village = SimpleNamespace(get_account_capacities=Mock(return_value=None))
        reader = Mock(return_value={'gold': 5518000, 'elixir': 5537000})
        engine.farm_finish = SimpleNamespace(capacity_reader=SimpleNamespace(read=reader))
        engine.log = Mock()
        with patch('bot.bot_engine.time.monotonic', return_value=100):
            self.assertIsNone(engine._wall_storage_capacities())
        self.assertEqual(engine._wall_capacity_retry_at, 130)

    def test_live_replay_without_home_resource_bars_is_not_village(self):
        vision = VisionEngine.__new__(VisionEngine)
        vision.has_open_dialog = Mock(return_value=False)
        vision.match_template = Mock(return_value=(100, 850, 80, 50))
        vision.read_village_resources = Mock(return_value={
            'valid': {'gold': False, 'elixir': False}})
        screen = np.zeros((900, 1600, 3), dtype=np.uint8)
        self.assertFalse(vision.is_in_main_village(screen))
        vision.match_template.assert_not_called()
        vision.read_village_resources.return_value = {
            'valid': {'gold': True, 'elixir': True}}
        self.assertTrue(vision.is_in_main_village(screen))

    def test_maintenance_does_not_press_back_to_deselect_buildings(self):
        engine = BotEngine.__new__(BotEngine)
        engine.settings = {'main_village_enabled': True, 'auto_train_army': False,
                           'auto_collect': False, 'auto_collect_treasury': False,
                           'auto_request_troops': False, 'auto_upgrade_walls': False}
        engine.last_village_maintenance = 0
        engine._wall_farm_guard = WallFarmGuard()
        engine.log = Mock()
        screen = object()
        engine.adb = SimpleNamespace(screencap=Mock(return_value=screen))
        engine.village = SimpleNamespace(
            wall_result={'status': 'not_checked'},
            is_in_main_village=Mock(return_value=True),
            check_and_sync_account=Mock(), deselect_all=Mock())
        engine._service_optional_upgrades = Mock(return_value={'status': 'idle'})
        engine._maintain_free_builders = Mock(return_value=False)
        engine._wall_hold_gate = Mock(return_value=False)
        engine._run_village_maintenance()
        engine.village.deselect_all.assert_not_called()
        self.assertTrue(any('Hoàn tất bảo trì' in call.args[0]
                            for call in engine.log.call_args_list))

    def test_layout_reverification_is_not_cut_off_by_row_deadline(self):
        wall = WallUpgrader.__new__(WallUpgrader)
        wall._selection_deadline = 1
        wall.verify_walls_via_layout_editor = Mock(return_value={'wall_groups': []})
        self.assertEqual(wall.reverify_selection_inventory(Mock()), {'wall_groups': []})
        self.assertIsNone(wall._selection_deadline)

    def test_removed_quantity_limit_is_not_a_setting(self):
        self.assertTrue(DEFAULT_CONFIG['wall_lowest_first'])
        self.assertNotIn('wall_max_upgrades_per_cycle', DEFAULT_CONFIG)
        self.assertNotIn('wall_upgrade_max_by_money', DEFAULT_CONFIG)

    def test_debit_mismatch_verifies_layout_immediately(self):
        wall = WallUpgrader.__new__(WallUpgrader)
        wall.available_walls = 174
        wall.level = Mock(return_value=6)
        wall.vision = SimpleNamespace(read_village_resources=Mock(return_value={
            'gold': 1, 'valid': {'gold': True}}))
        wall.adb = SimpleNamespace(screencap=Mock(return_value=object()))
        wall.inventory_count = Mock(return_value=None)
        wall.verify_walls_via_layout_editor = Mock(return_value={
            'wall_groups': [{'level': 6, 'count': 174}]})
        wall.save_verification = Mock()
        logs = []
        with patch('bot.walls.time.sleep'):
            self.assertTrue(wall.verify_upgrade(object(), {'gold': 2616900}, 'gold',
                                                2610000, 5, 174, 15000, logs.append))
        wall.verify_walls_via_layout_editor.assert_called_once()
        self.assertTrue(wall._layout_inventory_verified)
        self.assertTrue(wall.last_upgrade_verification['success'])
        self.assertTrue(any('Layout Editor xác nhận' in line for line in logs))

    def test_pending_transaction_blocks_duplicate_upgrade_without_cooldown(self):
        wall = WallUpgrader.__new__(WallUpgrader)
        wall.settings = {'auto_upgrade_walls': True}
        wall.manager = SimpleNamespace(_pending_wall_transaction={
            'level': 5, 'before_count': 174, 'quantity': 174,
            'resource': 'gold', 'balance_before': 2616900},
            wall_status='', wall_result={'status': 'selection_failed'})
        wall.verify_walls_via_layout_editor = Mock(return_value=None)
        wall.run = Mock()
        self.assertFalse(wall._run_cycle(object(), 1, 5537000))
        self.assertEqual(wall.manager.wall_result['status'], 'transaction_pending')
        wall.run.assert_not_called()

    def test_single_white_stroke_is_not_assumed_to_be_one_gold(self):
        vision = VisionEngine.__new__(VisionEngine)
        crop = np.zeros((45, 80, 3), dtype=np.uint8)
        crop[10:32, 42:47] = 255
        vision._get_ocr = Mock(return_value=Mock(return_value=SimpleNamespace(txts=[])))
        self.assertIsNone(vision.read_white_number(crop))


if __name__ == '__main__':
    unittest.main()
