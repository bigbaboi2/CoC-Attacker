import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from bot.builder_base import BuilderBaseManager
from bot.bot_engine import BotEngine
from bot.farm_finish import FarmFinishController
from bot.walls import WallUpgrader

class VillageSchedulingTests(unittest.TestCase):
    def test_builder_waits_restore_only_unexpired_verified_account(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fake_source = root / 'bot' / 'builder_base.py'
            base = self.manager()
            base.village = SimpleNamespace(_session_player_tag='#P0Y2')
            base.main_builders_wait_until = 500
            base.night_builders_wait_until = 900
            with patch('bot.builder_base.Path', return_value=fake_source), patch('bot.builder_base.time.monotonic', return_value=100), patch('bot.builder_base.time.time', return_value=1000):
                base.remember_builder_waits()
            restored = self.manager()
            restored.village = SimpleNamespace(_session_player_tag='#P0Y2')
            with patch('bot.builder_base.Path', return_value=fake_source), patch('bot.builder_base.time.monotonic', return_value=200), patch('bot.builder_base.time.time', return_value=1500):
                restored.restore_builder_waits(lambda _: None)
            self.assertEqual(restored.main_builders_wait_until, 0)
            self.assertEqual(restored.night_builders_wait_until, 500)
            self.assertTrue(restored.night_schedule_checked)

    def test_unstable_builder_count_does_not_claim_zero(self):
        base = self.manager()
        base.get_free_builders = Mock(side_effect=[0, 1])
        base.adb = SimpleNamespace(screencap=Mock(return_value=object()))
        self.assertIsNone(base.confirmed_free_builders(object()))

    def test_switching_accounts_discards_previous_builder_schedule(self):
        base = self.manager()
        base._restored_wait_account = '#P0Y2'
        base.village = SimpleNamespace(_session_player_tag='#P0Y8')
        base.main_builders_wait_until = 500
        base.night_builders_wait_until = 900
        base.main_schedule_checked = base.night_schedule_checked = True
        with tempfile.TemporaryDirectory() as folder:
            with patch('bot.builder_base.Path', return_value=Path(folder) / 'bot' / 'builder_base.py'):
                base.restore_builder_waits(lambda _: None)
        self.assertEqual(base.main_builders_wait_until, 0)
        self.assertEqual(base.night_builders_wait_until, 0)
        self.assertFalse(base.main_schedule_checked)
        self.assertFalse(base.night_schedule_checked)

    def manager(self):
        base = BuilderBaseManager.__new__(BuilderBaseManager)
        base.settings = {'builder_base_enabled': True, 'main_village_enabled': True}
        base.retry_after = 0
        base.next_maintenance = 1000
        base.next_wall_check = 1000
        base.next_building_check = 1000
        base.main_turn_until = 500
        base.main_builders_wait_until = 0
        base.night_builders_wait_until = 0
        base.all_upgrades_complete = False
        base.walls_confirmed_maxed = False
        base.battle = SimpleNamespace(start=Mock())
        return base

    def test_main_builder_wait_keeps_night_battles_eligible(self):
        base = self.manager()
        base.main_builders_wait_until = 400
        self.assertIn('builder_base_battle', [key for key, _ in base.ready_actions(100)])
        self.assertNotIn('builder_base_battle', [key for key, _ in base.ready_actions(401)])

    def test_night_builder_wait_defers_night_when_main_available(self):
        base = self.manager()
        base.night_builders_wait_until = 400
        self.assertEqual(base.ready_actions(100), [])
        base.main_builders_wait_until = 300
        self.assertIn('builder_base_battle', [key for key, _ in base.ready_actions(100)])

    def test_night_does_not_leave_before_main_builder_is_free(self):
        battle = SimpleNamespace(in_progress=False, find_now_failures=0, obstruction_buttons=Mock(return_value=None))
        base = SimpleNamespace(battle=battle, active=True, dismiss_star_bonus=Mock(return_value=False),
                               is_home=Mock(return_value=True), run_cycle=Mock(), leave=Mock(return_value=True),
                               main_builders_wait_until=400)
        fake = SimpleNamespace(builder=base, settings={'main_village_enabled': True, 'builder_base_enabled': True},
                               set_state=Mock(), STATE_BUILDER='BUILDER', STATE_VILLAGE='VILLAGE', log=Mock(),
                               adb=SimpleNamespace(screencap=Mock(return_value=object())))
        fake._service_cross_village_sleep = Mock(return_value=False)
        with patch('bot.bot_engine.time.monotonic', return_value=100), patch('bot.bot_engine.time.sleep'):
            self.assertTrue(BotEngine._handle_builder_base(fake, object()))
        base.leave.assert_not_called()
        with patch('bot.bot_engine.time.monotonic', return_value=401), patch('bot.bot_engine.time.sleep'):
            self.assertTrue(BotEngine._handle_builder_base(fake, object()))
        base.leave.assert_called_once()

    def test_main_no_builder_schedules_return_at_observed_timer(self):
        base = self.manager()
        worker = SimpleNamespace(observe_home_builders=Mock(return_value=0), pending=None,
                                 get_shortest_ongoing_builder_seconds=Mock(return_value=3600))
        controller = FarmFinishController.__new__(FarmFinishController)
        controller.suggested_upgrader = Mock(return_value=worker)
        fake = SimpleNamespace(farm_finish=controller, village=SimpleNamespace(walls_confirmed_maxed=False, current_account_key='#P0Y2'),
            settings={'builder_base_enabled': True, 'main_village_enabled': True}, builder=base, log=Mock())
        fake._service_cross_village_sleep = Mock(return_value=False)
        with patch('bot.bot_engine.time.monotonic', return_value=100):
            BotEngine._maintain_free_builders(fake, object())
        self.assertEqual(base.main_builders_wait_until, 3710)
        self.assertEqual(base.main_turn_until, 0)

    def test_night_timer_uses_only_active_jobs_above_suggestions(self):
        base = self.manager()
        base.tap = Mock()
        frame = object()
        base.wait_for = Mock(return_value=frame)
        base.walls = SimpleNamespace(is_menu_open=Mock(), duration_seconds=WallUpgrader.duration_seconds,
            ocr_positions=Mock(return_value=[('2h', 1450, 100), ('12m', 1500, 100),
                                             ('Suggested upgrades', 1300, 200), ('1m', 1500, 250)]))
        self.assertEqual(base.night_builder_wait_seconds(frame), 7930)
        self.assertEqual(base.tap.call_args_list[-1].args[0], (1570, 30))

    def test_cross_village_sleep_selects_shortest_builder_time(self):
        base = self.manager()
        base.battle = SimpleNamespace(in_progress=False)
        base.main_builders_wait_until = 1500  # 500s remaining
        base.night_builders_wait_until = 1300  # 300s remaining
        fake = SimpleNamespace(builder=base, settings={'main_village_enabled': True, 'builder_base_enabled': True},
                               log=Mock(), _sleep_wait_for_builders=Mock(return_value=True))
        with patch('bot.bot_engine.time.monotonic', return_value=1000):
            result = BotEngine._service_cross_village_sleep(fake)
        self.assertTrue(result)
        fake._sleep_wait_for_builders.assert_called_once_with(300)
        self.assertEqual(base.night_builders_wait_until, 0)

    def test_cross_village_sleep_single_village(self):
        base = self.manager()
        base.battle = SimpleNamespace(in_progress=False)
        base.night_builders_wait_until = 1300  # 300s remaining
        fake = SimpleNamespace(builder=base, settings={'main_village_enabled': False, 'builder_base_enabled': True},
                               log=Mock(), _sleep_wait_for_builders=Mock(return_value=True))
        with patch('bot.bot_engine.time.monotonic', return_value=1000):
            result = BotEngine._service_cross_village_sleep(fake)
        self.assertTrue(result)
        fake._sleep_wait_for_builders.assert_called_once_with(300)
        self.assertEqual(base.night_builders_wait_until, 0)

if __name__ == '__main__': unittest.main()
