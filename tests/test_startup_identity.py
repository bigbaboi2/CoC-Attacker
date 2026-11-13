import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bot.bot_engine import BotEngine
from bot.village import VillageManager
from bot.walls import WallUpgrader
from core.village_state import VillageStateManager


class StartupIdentityTests(unittest.TestCase):
    def test_builder_base_is_not_misidentified_as_main(self):
        village = VillageManager.__new__(VillageManager)
        village.vision = SimpleNamespace(is_in_main_village=Mock(return_value=True))
        village.builder = SimpleNamespace(night_palette=Mock(return_value=True),
                                          is_home=Mock(return_value=True))
        self.assertFalse(village.is_in_main_village(object()))
        village.builder.night_palette.return_value = False
        village.builder.is_home.return_value = True
        self.assertFalse(village.is_in_main_village(object()))
        village.builder.is_home.return_value = False
        self.assertTrue(village.is_in_main_village(object()))

    def village(self):
        village = VillageManager.__new__(VillageManager)
        village.current_account_key = None
        village._session_player_tag = None
        village._session_legacy_key = None
        village._verified_header_key = None
        village.verified_maxed_accounts = {}
        village.account_capacities = {}
        village.account_builder_timers = {}
        village.walls_confirmed_maxed = False
        village.wall_status = ''
        village._wall_builder_retry_at = 0
        village.village_state = None
        village.settings = {}
        village.adb = SimpleNamespace(screencap=Mock(return_value=object()))
        village.vision = SimpleNamespace(read_player_profile_header=Mock(return_value={
            'name': 'Same', 'level': 100, 'account_key': 'Same_100'}))
        village.is_valid_village_screen = Mock(return_value=True)
        village.set_wall_wait = Mock()
        return village

    def test_three_fresh_profile_attempts_then_legacy_fallback(self):
        village = self.village()
        village.resolve_player_tag = Mock(return_value=None)
        logs = []
        self.assertEqual(village.establish_account(object(), logs.append), 'Same_100')
        self.assertEqual(village.resolve_player_tag.call_count, 3)
        self.assertEqual(village.current_account_key, 'Same_100')
        self.assertEqual(village._session_legacy_key, 'Same_100')
        self.assertTrue(any('tên + cấp' in line for line in logs))
        self.assertEqual(village.check_and_sync_account(object()), 'Same_100')
        self.assertEqual(village.resolve_player_tag.call_count, 3)

    def test_verified_identity_is_reused_for_entire_session(self):
        village = self.village()
        village._session_player_tag = '#P0Y2'
        village._identity_verified_at = 0
        village._verified_header_key = 'Old_100'
        village.vision.read_player_profile_header.return_value = {'account_key': 'Different_101'}
        village.establish_account = Mock()
        for _ in range(3):
            self.assertEqual(village.check_and_sync_account(object()), '#P0Y2')
        village.establish_account.assert_not_called()
        village.vision.read_player_profile_header.assert_not_called()

    def test_matching_legacy_data_merges_and_conflicting_data_stays_separate(self):
        village = self.village()
        village.verified_maxed_accounts = {'Same_100': {'th_level': 16, 'min_level': 17, 'verified_at': 1}}
        village.account_capacities = {'Same_100': {'gold': 24_000_000, 'updated_at': 1},
                                      '#P0Y2': {'gold': 24_000_000, 'updated_at': 2}}
        village._merge_legacy_account('Same_100', '#P0Y2')
        self.assertIn('#P0Y2', village.verified_maxed_accounts)
        self.assertNotIn('Same_100', village.account_capacities)

        village.account_capacities['Same_100'] = {'gold': 10}
        village._merge_legacy_account('Same_100', '#P0Y2')
        self.assertIn('Same_100', village.account_capacities)
        self.assertEqual(village.account_capacities['#P0Y2']['gold'], 24_000_000)

    def test_live_wall_and_resource_cache_is_isolated_by_tag(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'live.json'
            state = VillageStateManager(str(path))
            state.set_account('#P0Y2')
            state.replace_wall_inventory('main', {15: 20})
            state.update_resources('main', {'gold': 100, 'valid': {'gold': True}})
            state.set_account('#QJU2')
            self.assertEqual(state.wall_inventory['main'], {})
            self.assertFalse(state.home_resources['valid'].get('gold'))
            state.set_account('#P0Y2')
            self.assertEqual(state.wall_inventory['main'], {15: 20})
            self.assertEqual(state.home_resources['gold'], 100)
            state.set_account('Same_100')
            state.replace_wall_inventory('main', {16: 10})
            self.assertTrue(state.merge_account_cache('Same_100', '#NEW2'))
            self.assertEqual(state.account_key, '#NEW2')
            self.assertNotIn('Same_100', state.account_caches)

    def test_main_scan_waits_for_identity_and_retries_failure(self):
        events = []
        screen = object()
        village = SimpleNamespace(
            is_in_main_village=lambda _: True,
            check_and_sync_account=Mock(side_effect=[None, '#P0Y2', '#P0Y2', '#P0Y2']),
            verify_walls=Mock(side_effect=[None, {'maxed': False}]))
        worker = SimpleNamespace(observe_home_builders=Mock(return_value=1))
        builder = SimpleNamespace(is_home=lambda _: False, restore_builder_waits=Mock(),
                                  remember_builder_waits=Mock(), main_schedule_checked=False)
        fake = SimpleNamespace(builder=builder, village=village,
                               settings={'main_village_enabled': True},
                               farm_finish=SimpleNamespace(suggested_upgrader=lambda: worker),
                               adb=SimpleNamespace(screencap=lambda: screen), log=events.append)
        self.assertFalse(BotEngine._prepare_village_startup(fake, screen))
        village.verify_walls.assert_not_called()
        self.assertFalse(BotEngine._prepare_village_startup(fake, screen))
        self.assertTrue(BotEngine._prepare_village_startup(fake, screen))
        self.assertTrue(BotEngine._prepare_village_startup(fake, screen))
        self.assertEqual(village.verify_walls.call_count, 2)
        self.assertEqual(fake._main_scanned_account, '#P0Y2')

    def test_night_start_does_not_scan_main_walls(self):
        village = SimpleNamespace(is_in_main_village=lambda _: False,
                                  check_and_sync_account=Mock(return_value='#P0Y2'),
                                  verify_walls=Mock())
        builder = SimpleNamespace(is_home=lambda _: True, restore_builder_waits=Mock())
        fake = SimpleNamespace(village=village, builder=builder,
                               settings={'main_village_enabled': True}, log=Mock())
        self.assertTrue(BotEngine._prepare_village_startup(fake, object()))
        self.assertEqual(fake._startup_origin, 'night')
        village.verify_walls.assert_not_called()

    def test_night_only_mode_does_not_hold_at_disabled_main_village(self):
        village = SimpleNamespace(is_in_main_village=lambda _: True,
                                  check_and_sync_account=Mock(return_value='#P0Y2'),
                                  verify_walls=Mock())
        builder = SimpleNamespace(is_home=lambda _: False, restore_builder_waits=Mock())
        fake = SimpleNamespace(village=village, builder=builder,
                               settings={'main_village_enabled': False}, log=Mock())
        self.assertTrue(BotEngine._prepare_village_startup(fake, object()))
        self.assertFalse(fake._startup_main_work_pending)
        village.verify_walls.assert_not_called()

    def test_night_first_pass_allows_one_battle_then_visits_main(self):
        battle = SimpleNamespace(in_progress=False, find_now_failures=0,
                                 obstruction_buttons=Mock(return_value=None))
        builder = SimpleNamespace(
            battle=battle, active=True, is_home=Mock(return_value=True),
            dismiss_star_bonus=Mock(return_value=False),
            restore_builder_waits=Mock(), remember_builder_waits=Mock(),
            run_cycle=Mock(side_effect=lambda *a, **k: setattr(battle, 'in_progress', True)),
            leave=Mock(return_value=True), main_builders_wait_until=0)
        fake = SimpleNamespace(
            builder=builder, village=SimpleNamespace(check_and_sync_account=Mock()),
            settings={'main_village_enabled': True},
            adb=SimpleNamespace(screencap=Mock(return_value=object())),
            log=Mock(), set_state=Mock(), STATE_BUILDER='BUILDER', STATE_VILLAGE='VILLAGE',
            _startup_origin='night', _startup_night_pass_done=False,
            _main_scanned_account=None, _service_cross_village_sleep=Mock(return_value=False))
        with patch('bot.bot_engine.time.sleep'):
            self.assertTrue(BotEngine._handle_builder_base(fake, object()))
            builder.leave.assert_not_called()
            self.assertTrue(BotEngine._handle_builder_base(fake, object()))
        builder.run_cycle.assert_called_once()
        self.assertTrue(builder.run_cycle.call_args.kwargs['first_pass'])
        builder.leave.assert_called_once()

    def test_obstacle_popup_is_dismissed_before_layout_scan(self):
        wall = WallUpgrader.__new__(WallUpgrader)
        wall.adb = SimpleNamespace(screencap=Mock(return_value=object()), tap=Mock())
        wall.texts = Mock(side_effect=[['Obstacles must be moved'], []])
        wall.vision = SimpleNamespace(match_template=Mock(return_value=(800, 550)),
                                      find_okay_button=Mock())
        self.assertTrue(wall._dismiss_layout_obstacle(Mock()))
        wall.adb.tap.assert_called_once_with(800, 550, jitter=0, delay_after=.5)

    def test_failed_editor_exit_never_commits_wall_result(self):
        wall = WallUpgrader.__new__(WallUpgrader)
        wall.ensure_menu_open = Mock(return_value=True)
        wall._dismiss_layout_obstacle = Mock(return_value=True)
        wall.parse_layout_inventory_cards = Mock(return_value={
            'maxed': True, 'min_level': 17, 'available': 325,
            'wall_groups': [{'level': 17, 'count': 325}], 'th_level': 16})
        wall.adb = SimpleNamespace(tap=Mock(), screencap=Mock(return_value=object()))
        manager = SimpleNamespace(current_account_key='#P0Y2',
                                  verified_maxed_accounts={}, walls_confirmed_maxed=False,
                                  is_in_main_village=Mock(return_value=False), settings={})
        wall.manager = manager
        self.assertIsNone(wall.verify_walls_via_layout_editor(Mock()))
        self.assertFalse(manager.walls_confirmed_maxed)
        self.assertEqual(manager.verified_maxed_accounts, {})

    def test_successful_editor_scan_commits_after_discard(self):
        wall = WallUpgrader.__new__(WallUpgrader)
        wall.ensure_menu_open = Mock(return_value=True)
        wall._dismiss_layout_obstacle = Mock(return_value=True)
        wall.parse_layout_inventory_cards = Mock(return_value={
            'maxed': True, 'min_level': 17, 'available': 325,
            'wall_groups': [{'level': 17, 'count': 325}], 'th_level': 16})
        wall.adb = SimpleNamespace(tap=Mock(), screencap=Mock(return_value=object()))
        manager = SimpleNamespace(current_account_key='#P0Y2',
                                  verified_maxed_accounts={}, walls_confirmed_maxed=False,
                                  is_in_main_village=Mock(return_value=True), settings={})
        wall.manager = manager
        self.assertTrue(wall.verify_walls_via_layout_editor(Mock())['maxed'])
        self.assertTrue(manager.walls_confirmed_maxed)
        self.assertIn('#P0Y2', manager.verified_maxed_accounts)
        self.assertEqual(wall.adb.tap.call_args_list[-1].args,
                         WallUpgrader.FIXED_LAYOUT_POINTS['close_editor'])


if __name__ == '__main__':
    unittest.main()
