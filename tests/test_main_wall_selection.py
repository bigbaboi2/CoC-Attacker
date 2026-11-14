import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import numpy as np

from bot.walls import WallUpgrader
from bot.wall_guard import WallFarmGuard
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

    def test_failed_selection_with_full_resources_keeps_village_hold(self):
        guard = WallFarmGuard()
        settings = {'auto_upgrade_walls': True, 'wall_hold_enabled': True,
                    'wall_resource': 'both', 'min_gold_to_upgrade_wall': 1000000,
                    'min_elixir_to_upgrade_wall': 1000000}
        readings = {'gold': 9000000, 'elixir': 9000000,
                    'valid': {'gold': True, 'elixir': True}}
        self.assertTrue(guard.observe(settings, readings, {'status': 'selection_failed'}))
        self.assertTrue(guard.active)

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
