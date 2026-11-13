import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import numpy as np
import cv2
from bot.builder_base import BuilderBaseManager
from bot.walls import WallUpgrader
from collections import OrderedDict

class BuilderWallQuantityTests(unittest.TestCase):
    def test_uncertain_multi_selection_can_return_to_verified_single_wall(self):
        frame = np.zeros((900, 1600, 3), dtype=np.uint8)
        base = BuilderBaseManager.__new__(BuilderBaseManager)
        base.adb = SimpleNamespace(keyevent=Mock(), screencap=Mock(return_value=frame))
        base.is_home = Mock(return_value=True)
        base.walls = SimpleNamespace(level=Mock(return_value=4))
        base.wall_offer = Mock(side_effect=lambda _, resource:
                               ((600, 700), 150000) if resource == 'gold' else None)
        logs = []
        self.assertIs(base.single_wall_after_quantity_failure(4, {'cost': 150000}, logs.append), frame)
        base.adb.keyevent.assert_called_once_with(4)
        self.assertTrue(any('một đoạn' in line for line in logs))
        base.wall_offer = Mock(return_value=((600, 700), 300000))
        self.assertIsNone(base.single_wall_after_quantity_failure(4, {'cost': 150000}, logs.append))

    def test_builder_layout_counts_small_wall_groups(self):
        wall = WallUpgrader.__new__(WallUpgrader)
        wall.ocr_positions = Mock(return_value=[
            ('x79', 84, 743), ('x14', 252, 744), ('x7', 419, 745),
            ('Level 4', 86, 862), ('Level 5', 254, 862),
            ('Level 8', 423, 862), ('Level 8', 594, 862)])
        result = wall.parse_layout_inventory_cards(np.zeros((900, 1600, 3), dtype=np.uint8), builder_base=True)
        self.assertEqual([(g['level'], g['count']) for g in result['wall_groups']],
                         [(4, 79), (5, 14), (8, 7)])
        self.assertFalse(result['maxed'])

    def test_boat_tap_rejects_overlapping_hud(self):
        self.assertFalse(BuilderBaseManager.boat_tap_is_clear((1350, 165, 40, 40, .9)))
        self.assertFalse(BuilderBaseManager.boat_tap_is_clear((180, 740, 40, 40, .9)))
        self.assertTrue(BuilderBaseManager.boat_tap_is_clear((1000, 350, 40, 40, .9)))

    def test_enter_moves_map_before_tapping_boat_under_hud(self):
        base = BuilderBaseManager.__new__(BuilderBaseManager)
        base.is_home = Mock(return_value=False)
        base.is_main = Mock(return_value=True)
        base.match = Mock(side_effect=[(180, 740, 40, 40, .9), (500, 480, 40, 40, .9)])
        base.adb = SimpleNamespace(swipe=Mock(), screencap=Mock(return_value=object()))
        base.tap = Mock()
        base.wait_for = Mock(return_value=object())
        with patch('bot.builder_base.time.sleep'):
            self.assertTrue(base.enter(object()))
        base.adb.swipe.assert_called_once()
        base.tap.assert_called_once_with((500, 480, 40, 40, .9))

    def test_leave_moves_map_before_tapping_boat_under_resource_bar(self):
        base = BuilderBaseManager.__new__(BuilderBaseManager)
        base.is_home = Mock(return_value=True)
        base.night_palette = Mock(return_value=True)
        base.match = Mock(side_effect=[(1350, 165, 40, 40, .9), (1000, 350, 40, 40, .9)])
        base.adb = SimpleNamespace(swipe=Mock(), screencap=Mock(return_value=object()), zoom_out_map=Mock())
        base.tap = Mock()
        base.is_main = Mock()
        base.wait_for = Mock(return_value=object())
        with patch('bot.builder_base.time.sleep'):
            self.assertTrue(base.leave(object()))
        base.adb.swipe.assert_called_once()
        base.tap.assert_called_once_with((1000, 350, 40, 40, .9))

    def make_elixir_scan(self, unknown_low=False):
        base = BuilderBaseManager.__new__(BuilderBaseManager)
        rng = np.random.default_rng(42)
        frame = rng.integers(0, 256, (900, 1600, 3), dtype=np.uint8)
        rows = [dict(x=1290, y=200, cost=10000, available=20),
                dict(x=1290, y=400, cost=400000, available=5)]
        base.adb = SimpleNamespace(_guard_input=Mock(), screencap=Mock(return_value=frame), swipe=Mock())
        base.walls = SimpleNamespace(menu_words=Mock(return_value=[('page', 0, 0)]),
            menu_rows=Mock(side_effect=lambda *args, **kwargs: [dict(row) for row in rows]),
            menu_signature=Mock(return_value=('page',)), ocr_positions=Mock(return_value=[]),
            stable_menu_row=Mock(side_effect=lambda row: row), is_menu_open=Mock(return_value=False))
        base.capture_wall_pages = Mock(return_value=([frame], frame))
        base.builder_wall_badge_template = Mock(side_effect=lambda img, y: (None if unknown_low else 3) if y == 200 else 7)
        base.wait_for_wall_selection = Mock(side_effect=[(frame, 2), (frame, 6)] if unknown_low else [(frame, 6)])
        base.tap = Mock()
        base.wait_for = Mock(return_value=frame)
        return base, frame

    def test_elixir_scan_skips_known_low_level_wall(self):
        base, frame = self.make_elixir_scan()
        with patch('bot.builder_base.time.sleep'):
            row, _ = base.lowest_wall_row(frame, 500000, lambda _: None, min_level=6)
        self.assertEqual((row['cost'], row['upgrade_level']), (400000, 7))
        self.assertEqual(base.wait_for_wall_selection.call_count, 1)

    def test_elixir_scan_checks_unknown_then_selects_eligible_wall(self):
        base, frame = self.make_elixir_scan(unknown_low=True)
        with patch('bot.builder_base.time.sleep'):
            row, _ = base.lowest_wall_row(frame, 500000, lambda _: None, min_level=6)
        self.assertEqual((row['cost'], row['upgrade_level']), (400000, 7))
        self.assertEqual(base.wait_for_wall_selection.call_count, 2)

    def test_unknown_ineligible_wall_retries_full_scan(self):
        base, frame = self.make_elixir_scan(unknown_low=True)
        base.walls.menu_rows.side_effect = lambda *args, **kwargs: [
            dict(x=1290, y=200, cost=10000, available=20)]
        base.wait_for_wall_selection = Mock(return_value=(frame, 5))
        original = base.lowest_wall_row
        fallback = (dict(cost=400000, upgrade_level=7), frame)
        def scan(screen, budget, log, precise=False, _deadline=None, min_level=1):
            if precise:
                self.assertEqual(min_level, 6)
                return fallback
            return original(screen, budget, log, precise=precise,
                            _deadline=_deadline, min_level=min_level)
        base.lowest_wall_row = scan
        with patch('bot.builder_base.time.sleep'):
            row, _ = base.lowest_wall_row(frame, 500000, lambda _: None, min_level=6)
        self.assertEqual(row['upgrade_level'], 7)

    def test_builder_menu_single_wall_without_x_is_counted(self):
        wall = WallUpgrader.__new__(WallUpgrader)
        wall._selection_deadline = None
        words = [('Wall', 1280, 200), ('400', 1510, 223), ('000', 1550, 223)]
        rows = wall.menu_rows(None, words, read_level=False)
        self.assertEqual([(row['available'], row['cost']) for row in rows], [(1, 400000)])
        malformed = [('Wall x', 1280, 200), ('400000', 1510, 223)]
        self.assertEqual(wall.menu_rows(None, malformed, read_level=False), [])

    def test_opening_resource_modes(self):
        base = BuilderBaseManager.__new__(BuilderBaseManager)
        base.settings = {'builder_base_wall_min_gold': 1000000, 'builder_base_wall_min_elixir': 1000000}
        balances = {'gold': 628498, 'elixir': 2548168}
        for mode, expected in [('either', {'elixir'}), ('both', set()), ('gold', set()), ('elixir', {'elixir'})]:
            base.settings['builder_base_wall_trigger'] = mode
            self.assertEqual(base.wall_opening_resources(balances), expected, mode)
        base.settings['builder_base_wall_trigger'] = 'both'
        self.assertEqual(base.wall_opening_resources({'gold': 1000000, 'elixir': 1000000}), {'gold', 'elixir'})
        self.assertEqual(base.wall_opening_resources({'gold': 1000000}), set())
        self.assertEqual(base.wall_opening_resources({'gold': 999999, 'elixir': 2000000}, continuing=True), {'elixir'})
        self.assertEqual(base.wall_opening_resources({'gold': 2000000, 'elixir': 999999}, continuing=True), {'gold'})

    def test_both_mode_continues_with_elixir_after_gold_falls_below_threshold(self):
        base = BuilderBaseManager.__new__(BuilderBaseManager)
        base.walls_confirmed_maxed = False
        base.settings = {'builder_base_enabled': True, 'builder_base_upgrade_walls': True,
                         'builder_base_wall_trigger': 'both', 'builder_base_wall_min_gold': 100,
                         'builder_base_wall_min_elixir': 100}
        base.wall_balances = Mock(side_effect=[{'gold': 200, 'elixir': 300},
                                               {'gold': 50, 'elixir': 300},
                                               {'gold': 50, 'elixir': 50}])
        base.adb = SimpleNamespace(_guard_input=Mock(), screencap=Mock(return_value=object()))
        base.is_home = Mock(return_value=True)
        base.deselect = Mock()
        base.confirmed_free_builders = Mock(return_value=1)
        base.get_free_builders = Mock(return_value=1)
        base.upgrade_wall = Mock(return_value=True)
        self.assertTrue(base.upgrade_wall_cycle(object(), lambda _: None))
        self.assertEqual([call.kwargs['eligible_resources'] for call in base.upgrade_wall.call_args_list],
                         [{'gold', 'elixir'}, {'elixir'}])

    def test_below_threshold_never_opens_wall_menu(self):
        base = BuilderBaseManager.__new__(BuilderBaseManager)
        base.walls_confirmed_maxed = False
        base.settings = {'builder_base_wall_trigger': 'gold', 'builder_base_wall_min_gold': 1000000}
        base.wall_balances = Mock(return_value={'gold': 628498, 'elixir': 2548168})
        base.upgrade_wall = Mock()
        base.walls = SimpleNamespace(builders=Mock())
        base.get_free_builders = Mock(return_value=1)
        base.adb = SimpleNamespace(screencap=Mock(return_value=object()))
        logs = []
        self.assertIsNone(base.upgrade_wall_cycle(object(), logs.append))
        base.upgrade_wall.assert_not_called()
        base.walls.builders.assert_not_called()
        self.assertTrue(any('trước khi mở bảng thợ' in line for line in logs))

    def make_base(self, readings, buttons):
        base = BuilderBaseManager.__new__(BuilderBaseManager)
        base.adb = SimpleNamespace(tap_batch=Mock(), screencap=Mock(return_value=np.zeros((900,1600,3),dtype=np.uint8)))
        base.settings = {}
        base.builder_wall_quantity = Mock(side_effect=readings)
        base.wall_quantity_buttons = Mock(return_value=buttons)
        return base

    def test_six_walls_use_only_plus_one(self):
        base = self.make_base([1,6,6], {1:(608,704)})
        frame,count = base.select_wall_quantity_fast(object(),6,6,lambda _: None)
        self.assertIsNotNone(frame)
        self.assertEqual(count,6)
        self.assertEqual(base.adb.tap_batch.call_args.args[0],[(608,704)] * 5)

    def test_twenty_one_uses_two_tens_without_intermediate_ocr(self):
        base = self.make_base([1,21,21], {10:(533,704),1:(694,704)})
        frame,count = base.select_wall_quantity_fast(object(),21,30,lambda _: None)
        self.assertEqual(count,21)
        self.assertEqual(base.adb.tap_batch.call_args.args[0],[(533,704)] * 2)
        self.assertEqual(base.builder_wall_quantity.call_count,3)

    def test_uncertain_count_does_not_return_payment_frame(self):
        base = self.make_base([1,None,None,None,None], {1:(608,704)})
        frame,count = base.select_wall_quantity_fast(object(),6,6,lambda _: None)
        self.assertIsNone(frame)
        self.assertEqual(count, 1)

    def test_prepare_wall_quantity_ineligible_skips(self):
        base = BuilderBaseManager.__new__(BuilderBaseManager)
        base.settings = {'wall_upgrade_max_by_money': True}
        base.wall_balances = Mock(return_value={'elixir': 2548168})
        base.walls = SimpleNamespace(level=Mock(return_value=2))
        base.wall_offer = Mock()
        frame, count = base.prepare_wall_quantity(object(), {'available': 20, 'cost': 10000}, lambda _: None, {'elixir'})
        self.assertIsNone(frame)
        base.wall_offer.assert_not_called()

    def test_gold_below_threshold_explains_why_it_is_not_eligible(self):
        base = BuilderBaseManager.__new__(BuilderBaseManager)
        base.settings = {'wall_upgrade_max_by_money': True, 'builder_base_wall_min_gold': 1000000}
        base._last_wall_balance_read = {'gold': 628498, 'valid': {'gold': True}}
        base.wall_balances = Mock(return_value={'elixir': 2548168})
        base.walls = SimpleNamespace(level=Mock(return_value=2))
        logs = []
        frame, count = base.prepare_wall_quantity(object(), {'available': 20, 'cost': 10000}, logs.append, {'elixir'})
        self.assertIsNone(frame)
        self.assertTrue(any('628,498' in line and '1,000,000' in line for line in logs))

    def test_priority_scan_stops_after_deadline(self):
        base = BuilderBaseManager.__new__(BuilderBaseManager)
        base.adb = SimpleNamespace(_guard_input=Mock(), screencap=Mock())
        base.walls = SimpleNamespace()
        log = Mock()
        with patch('bot.builder_base.time.monotonic', side_effect=[0, 101]):
            row, frame = base.lowest_wall_row(object(), None, log)
        self.assertIsNone(row)
        self.assertTrue(any('thời gian' in call.args[0] or '100 giây' in call.args[0] for call in log.call_args_list))

    def test_badge_templates_recognize_known_levels_and_reject_blank(self):
        base = BuilderBaseManager.__new__(BuilderBaseManager)
        frame = np.zeros((900, 1600, 3), dtype=np.uint8)
        self.assertIsNone(base.builder_wall_badge_template(frame, 300))
        for level in (2, 3, 4, 5, 6, 7, 8, 9):
            frame[:] = 0
            path = base.LOCAL / 'wall_badges' / f'{level}.png'
            template = cv2.imread(str(path))
            frame[320:338, 1186:1207] = template
            self.assertEqual(base.builder_wall_badge_template(frame, 300), level)

    def test_capture_pages_stops_at_both_boundaries_without_ocr(self):
        base = BuilderBaseManager.__new__(BuilderBaseManager)
        pages = [np.full((900, 1600, 3), value, dtype=np.uint8) for value in (10, 80, 150)]
        base.adb = SimpleNamespace(_guard_input=Mock(), swipe=Mock(),
                                   screencap=Mock(side_effect=[pages[0], pages[1], pages[2], pages[2]]))
        with patch('bot.builder_base.time.sleep'), patch('bot.builder_base.time.monotonic', return_value=0):
            captured, last = base.capture_wall_pages(pages[0], 100, lambda _: None)
        self.assertEqual(len(captured), 3)
        self.assertIs(last, pages[2])
        self.assertEqual(base.adb.swipe.call_count, 4)

    def test_upgrade_wall_cycle_stops_when_threshold_no_longer_met(self):
        base = BuilderBaseManager.__new__(BuilderBaseManager)
        base.walls_confirmed_maxed = False
        base.settings = {
            'builder_base_enabled': True,
            'builder_base_upgrade_walls': True,
            'builder_base_wall_trigger': 'either',
            'builder_base_wall_min_gold': 1000000,
            'builder_base_wall_min_elixir': 1000000,
        }
        # First check before loop: Elixir is 2.9M (eligible).
        # Second check after upgrade: Elixir drops to 500k (below threshold).
        base.wall_balances = Mock(side_effect=[
            {'gold': 774888, 'elixir': 2911850},
            {'gold': 774888, 'elixir': 511850},
        ])
        base.adb = SimpleNamespace(_guard_input=Mock(), screencap=Mock(return_value=object()))
        base.is_home = Mock(return_value=True)
        base.deselect = Mock()
        base.get_free_builders = Mock(return_value=1)
        base.upgrade_wall = Mock(return_value=True)

        logs = []
        result = base.upgrade_wall_cycle(object(), logs.append)

        self.assertTrue(result)
        self.assertEqual(base.upgrade_wall.call_count, 1)
        self.assertTrue(any('không còn đạt điều kiện' in line for line in logs))
        self.assertTrue(any('511,850/1,000,000 (chưa đạt ngưỡng)' in line for line in logs))

    def test_builder_base_builders_recognition_no_goblin_subtraction(self):
        wall = WallUpgrader.__new__(WallUpgrader)
        wall.vision = SimpleNamespace(read_white_number=Mock(return_value=None), _get_ocr=Mock(return_value=None))
        wall.texts = Mock(return_value=['0/2'])
        self.assertEqual(wall.builders(object(), builder_base=True), 0)
        self.assertEqual(wall.total_builders_read, 2)
        self.assertFalse(wall.paid_builder_visible)

        wall.texts = Mock(return_value=['1/2'])
        self.assertEqual(wall.builders(object(), builder_base=True), 1)

        wall.texts = Mock(return_value=['2/2'])
        self.assertEqual(wall.builders(object(), builder_base=True), 2)

    def test_account_sync_in_builder_base(self):
        from bot.village import VillageManager
        village = VillageManager.__new__(VillageManager)
        village.current_account_key = None
        village._session_player_tag = None
        village._session_legacy_key = None
        village._verified_header_key = None
        village.verified_maxed_accounts = {}
        village.account_capacities = {}
        village.account_builder_timers = {}
        village.TH_MAX_STORAGE_CAPACITIES = {}
        village.settings = {}
        village.set_wall_wait = Mock()
        village.wall_status = ""
        village._wall_builder_retry_at = 0
        village.get_account_capacities = Mock(return_value=None)
        village.get_account_shortest_builder_seconds = Mock(return_value=None)
        village.vision = SimpleNamespace(read_player_profile_header=Mock(return_value={'name': 'NightHero', 'level': 120, 'account_key': 'NightHero_120'}))
        village.resolve_player_tag = Mock(side_effect=lambda *_: (village._activate_account('#P0Y2') or '#P0Y2'))
        village.is_in_main_village = Mock(return_value=False)
        builder = SimpleNamespace(is_home=Mock(return_value=True))
        village.builder = builder

        logs = []
        acc_key = village.check_and_sync_account(object(), logs.append)
        self.assertEqual(acc_key, '#P0Y2')
        self.assertEqual(village.current_account_key, '#P0Y2')
        village.resolve_player_tag.assert_called_once()

    def test_upgrade_building_cycle_respects_reserve(self):
        base = BuilderBaseManager.__new__(BuilderBaseManager)
        base.all_upgrades_complete = False
        base.walls_confirmed_maxed = False
        base.settings = {
            'builder_base_reserve_builders': 1,
            'builder_base_building_resources': {'gold': True, 'elixir': True},
        }
        base.get_free_builders = Mock(return_value=1)
        base.adb = SimpleNamespace(screencap=Mock(return_value=object()))
        base.walls = SimpleNamespace(ensure_menu_open=Mock())
        logs = []
        result = base.upgrade_building_cycle(object(), logs.append)
        self.assertFalse(result)
        base.walls.ensure_menu_open.assert_not_called()
        self.assertTrue(any('mức duy trì' in l for l in logs))

    def test_upgrade_building_cycle_upgrades_candidate(self):
        base = BuilderBaseManager.__new__(BuilderBaseManager)
        base.all_upgrades_complete = False
        base.walls_confirmed_maxed = False
        base.settings = {
            'builder_base_reserve_builders': 1,
            'builder_base_building_priority': 'suggested',
            'builder_base_building_resources': {'gold': True, 'elixir': True},
        }
        base.get_free_builders = Mock(side_effect=[2, 2, 1, 1])
        before = {'gold': 5000000, 'elixir': 3000000, 'valid': {'gold': True, 'elixir': True}}
        after = {'gold': 3800000, 'elixir': 3000000, 'valid': {'gold': True, 'elixir': True}}
        base.vision = SimpleNamespace(read_village_resources=Mock(side_effect=[before, after, after]))
        base.walls = SimpleNamespace(ensure_menu_open=Mock(return_value=True), close_dialog=Mock())
        base.deselect = Mock()
        base.adb = SimpleNamespace(screencap=Mock(return_value=object()), tap=Mock())

        cand = {'name': 'Clock Tower', 'cost': 1200000, 'resource': 'gold', 'x': 1350, 'y': 300, 'duration_sec': 3600}
        upgrader = SimpleNamespace(
            words=Mock(return_value=[('Suggested upgrades', 1300, 100), ('Clock Tower', 1300, 300)]),
            menu_candidates=Mock(return_value=[cand]),
            sort_candidates=Mock(return_value=[cand]),
            confirmation=Mock(return_value=(800, 750)),
            upgrade_close_button=Mock(return_value=None),
        )
        base._suggested_upgrader = upgrader
        base.builder_building_confirmation = Mock(return_value=(800, 750))
        base.is_home = Mock(return_value=True)

        logs = []
        result = base.upgrade_building_cycle(object(), logs.append)
        self.assertTrue(result)
        self.assertEqual(base.adb.tap.call_count, 2)
        self.assertTrue(any('Clock Tower' in l for l in logs))

    def test_unreadable_builder_menu_is_not_classified_as_maxed(self):
        base = BuilderBaseManager.__new__(BuilderBaseManager)
        base.all_upgrades_complete = False
        base.walls_confirmed_maxed = False
        base.settings = {}
        base.get_free_builders = Mock(return_value=2)
        base.adb = SimpleNamespace(screencap=Mock(return_value=object()))
        base.vision = SimpleNamespace(read_village_resources=Mock(return_value={'gold': 10, 'valid': {'gold': True}}))
        base.walls = SimpleNamespace(ensure_menu_open=Mock(return_value=True), close_dialog=Mock())
        base._suggested_upgrader = SimpleNamespace(words=Mock(return_value=[]))
        self.assertIsNone(base.upgrade_building_cycle(object(), lambda _: None))
        self.assertFalse(base.all_upgrades_complete)

    def test_unknown_builder_count_does_not_start_building(self):
        base = BuilderBaseManager.__new__(BuilderBaseManager)
        base.all_upgrades_complete = False
        base.walls_confirmed_maxed = False
        base.settings = {}
        base.get_free_builders = Mock(return_value=None)
        base.walls = SimpleNamespace(ensure_menu_open=Mock())
        self.assertIsNone(base.upgrade_building_cycle(object(), lambda _: None))
        base.walls.ensure_menu_open.assert_not_called()

    def test_building_confirmation_requires_matching_price_name_and_currency(self):
        base = BuilderBaseManager.__new__(BuilderBaseManager)
        frame = np.zeros((900, 1600, 3), np.uint8)
        frame[700:770, 700:1000] = (0, 220, 0)
        candidate = {'name': 'Clock Tower', 'cost': 1200000, 'resource': 'gold'}
        worker = SimpleNamespace(words=Mock(return_value=[('Upgrade to level 5', 750, 60),
            ('Clock Tower', 600, 160), ('1 200 000', 820, 735)]), currency=Mock(return_value='gold'))
        self.assertIsNotNone(base.builder_building_confirmation(frame, candidate, worker))
        worker.currency.return_value = 'elixir'
        self.assertIsNone(base.builder_building_confirmation(frame, candidate, worker))
        worker.currency.return_value = 'gold'
        worker.words.return_value[-1] = ('2 200 000', 820, 735)
        self.assertIsNone(base.builder_building_confirmation(frame, candidate, worker))

if __name__ == '__main__': unittest.main()
