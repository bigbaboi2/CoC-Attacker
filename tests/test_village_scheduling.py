import unittest
import tempfile
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
import numpy as np
from bot.builder_base import BuilderBaseManager
from bot.bot_engine import BotEngine
from bot.farm_finish import FarmFinishController
from bot.walls import WallUpgrader
from core.vision import VisionEngine

class VillageSchedulingTests(unittest.TestCase):
    def test_entering_builder_base_verifies_layout_before_battle(self):
        events = []
        frame = object()
        base = BuilderBaseManager.__new__(BuilderBaseManager)
        base.settings = {'builder_base_enabled': True, 'builder_base_battle': True,
                         'main_village_enabled': False}
        base.ready_actions = Mock(return_value=[('builder_base_battle',
                                                  lambda *_: events.append('battle') or True)])
        base.enter = Mock(side_effect=lambda *_: events.append('enter') or True)
        base.is_home = Mock(return_value=True)
        base.dismiss_star_bonus = Mock(return_value=False)
        base.scan_initial_layout_walls = Mock(side_effect=lambda *_: events.append('layout') or True)
        base.scan_resources = Mock()
        base.adb = SimpleNamespace(screencap=Mock(return_value=frame))
        base.village_state = None
        base.night_schedule_checked = True
        base.night_builders_wait_until = 0
        base.next_cart_check = 0
        base.next_resource_scan = 0
        base.main_builders_wait_until = 0
        base.battle = SimpleNamespace(in_progress=False)
        engine = SimpleNamespace(_night_layout_scan_done=False, log=Mock(),
                                 adb=base.adb, stop_bot=Mock())
        engine._verify_builder_walls_once = lambda builder: BotEngine._verify_builder_walls_once(engine, builder)
        base.engine = engine

        base.run_cycle(frame, engine.log)

        self.assertEqual(events, ['enter', 'layout', 'battle'])
        self.assertTrue(engine._night_layout_scan_done)

    def test_failed_builder_layout_verification_prevents_battle(self):
        frame = object()
        base = BuilderBaseManager.__new__(BuilderBaseManager)
        base.settings = {'builder_base_enabled': True}
        battle = Mock()
        base.ready_actions = Mock(return_value=[('builder_base_battle', battle)])
        base.enter = Mock(return_value=True)
        base.is_home = Mock(return_value=True)
        base.scan_initial_layout_walls = Mock(return_value=False)
        base.adb = SimpleNamespace(screencap=Mock(return_value=frame))
        base.next_cart_check = 0
        base.next_resource_scan = 0
        engine = SimpleNamespace(_night_layout_scan_done=False, log=Mock(),
                                 adb=base.adb, stop_bot=Mock())
        engine._verify_builder_walls_once = lambda builder: BotEngine._verify_builder_walls_once(engine, builder)
        base.engine = engine

        base.run_cycle(frame, engine.log)

        battle.assert_not_called()
        self.assertFalse(engine._night_layout_scan_done)

    def test_network_fault_keeps_short_message_for_recovery_log(self):
        vision = VisionEngine.__new__(VisionEngine)
        vision._get_ocr = Mock(return_value=Mock(return_value=SimpleNamespace(
            txts=['Connection lost', 'Please reload game'])))
        frame = np.zeros((900, 1600, 3), dtype=np.uint8)
        self.assertEqual(vision.game_fault(frame), 'network')
        self.assertIn('connection lost', vision.last_game_fault_message)
        fake = SimpleNamespace(vision=vision, _pending_recovery=None,
                               _request_recovery=Mock(), log=Mock())
        with patch('bot.bot_engine.cv2.imwrite', return_value=True):
            self.assertTrue(BotEngine._queue_screen_fault(fake, frame))
        self.assertTrue(any('connection lost' in call.args[0] for call in fake.log.call_args_list))

    def test_recovery_accepts_builder_base_home_after_restart(self):
        frame = np.zeros((900, 1600, 3), dtype=np.uint8)
        builder = SimpleNamespace(is_home=Mock(return_value=True), deselect=Mock())
        fake = SimpleNamespace(
            settings={'auto_restart_game': True}, optimizer=SimpleNamespace(restart_game=Mock()),
            village=SimpleNamespace(_identity_verified_at=1.0,
                                    is_in_main_village=Mock(return_value=False),
                                    close_popups_and_dialogs=Mock()),
            builder=builder, adb=SimpleNamespace(screencap=Mock(return_value=frame)),
            vision=SimpleNamespace(game_fault=Mock(return_value=None)),
            screen_cb=None, STATE_RECOVERY='RECOVERY', STATE_BUILDER='BUILDER_BASE',
            log=Mock(), set_state=Mock(), _checkpoint=Mock(),
            _ensure_game_resolution=Mock(return_value=frame), _reset_restart_timer=Mock())
        with patch('bot.bot_engine.time.sleep'):
            self.assertTrue(BotEngine.restart_game_and_recover(fake))
        fake.set_state.assert_any_call('BUILDER_BASE')
        builder.deselect.assert_called_once()
        fake.village.close_popups_and_dialogs.assert_not_called()

    def test_builder_waits_restore_only_unexpired_verified_account(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            fake_source = root / 'bot' / 'builder_base.py'
            base = self.manager()
            base.village = SimpleNamespace(_session_player_tag='#P0Y2')
            base.main_builders_wait_until = 500
            base.night_builders_wait_until = 900
            fake_time = SimpleNamespace(monotonic=lambda: 100, time=lambda: 1000)
            with patch('bot.builder_base.Path', return_value=fake_source), patch('bot.builder_base.time', fake_time):
                base.remember_builder_waits()
            restored = self.manager()
            restored.village = SimpleNamespace(_session_player_tag='#P0Y2')
            fake_time = SimpleNamespace(monotonic=lambda: 200, time=lambda: 1500)
            with patch('bot.builder_base.Path', return_value=fake_source), patch('bot.builder_base.time', fake_time):
                restored.restore_builder_waits(lambda _: None)
            self.assertEqual(restored.main_builders_wait_until, 0)
            self.assertEqual(restored.night_builders_wait_until, 500)
            self.assertTrue(restored.night_schedule_checked)

    def test_builder_waits_are_saved_for_two_accounts(self):
        with tempfile.TemporaryDirectory() as folder:
            fake_source = Path(folder) / 'bot' / 'builder_base.py'
            clock = SimpleNamespace(monotonic=lambda: 100, time=lambda: 1000)
            base = self.manager()
            base.village = SimpleNamespace(_session_player_tag='#P0Y2')
            base.main_builders_wait_until = 500
            with patch('bot.builder_base.Path', return_value=fake_source), patch('bot.builder_base.time', clock):
                base.remember_builder_waits()
                base.village._session_player_tag = '#QJU2'
                base.main_builders_wait_until = 900
                base.remember_builder_waits()
                saved = json.loads((Path(folder) / 'builder_wait_state.json').read_text())
            self.assertEqual(set(saved['accounts']), {'#P0Y2', '#QJU2'})
            self.assertEqual(saved['accounts']['#P0Y2']['main_until'], 1400)
            self.assertEqual(saved['accounts']['#QJU2']['main_until'], 1800)

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

    def test_first_night_pass_runs_even_when_main_has_priority(self):
        base = self.manager()
        base.night_builders_wait_until = 400
        self.assertEqual(base.ready_actions(100), [])
        self.assertIn('builder_base_battle',
                      [key for key, _ in base.ready_actions(100, first_pass=True)])

    def test_night_does_not_leave_before_main_builder_is_free(self):
        battle = SimpleNamespace(in_progress=False, find_now_failures=0, obstruction_buttons=Mock(return_value=None))
        base = SimpleNamespace(battle=battle, active=True, dismiss_star_bonus=Mock(return_value=False),
                               is_home=Mock(return_value=True), run_cycle=Mock(), leave=Mock(return_value=True),
                               main_builders_wait_until=400)
        fake = SimpleNamespace(builder=base, settings={'main_village_enabled': True, 'builder_base_enabled': True},
                               set_state=Mock(), STATE_BUILDER='BUILDER', STATE_VILLAGE='VILLAGE', log=Mock(),
                               _night_layout_scan_done=True,
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
        base.is_home = Mock(return_value=False)
        base.main_builders_wait_until = 1500  # 500s remaining
        base.night_builders_wait_until = 1300  # 300s remaining
        fake = SimpleNamespace(builder=base, settings={'main_village_enabled': True, 'builder_base_enabled': True},
                               log=Mock(), adb=SimpleNamespace(screencap=Mock(return_value=object())),
                               _sleep_wait_for_builders=Mock(return_value=True))
        with patch('bot.bot_engine.time.monotonic', return_value=1000):
            result = BotEngine._service_cross_village_sleep(fake)
        self.assertTrue(result)
        fake._sleep_wait_for_builders.assert_called_once_with(300, 'builder')
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
        fake._sleep_wait_for_builders.assert_called_once_with(300, 'builder')
        self.assertEqual(base.night_builders_wait_until, 0)

    def test_equal_builder_waits_resume_in_current_night_village(self):
        base = self.manager()
        base.battle = SimpleNamespace(in_progress=False)
        base.main_builders_wait_until = base.night_builders_wait_until = 1400
        base.is_home = Mock(return_value=True)
        fake = SimpleNamespace(builder=base,
            settings={'main_village_enabled': True, 'builder_base_enabled': True},
            adb=SimpleNamespace(screencap=Mock(return_value=object())),
            log=Mock(), _sleep_wait_for_builders=Mock(return_value=True))
        with patch('bot.bot_engine.time.monotonic', return_value=1000):
            self.assertTrue(BotEngine._service_cross_village_sleep(fake))
        fake._sleep_wait_for_builders.assert_called_once_with(400, 'builder')

    def finished_main_engine(self, *, night_wait=0, night_enabled=True):
        base = self.manager()
        base.battle = SimpleNamespace(in_progress=False, start=Mock())
        base.night_builders_wait_until = night_wait
        base.night_schedule_checked = True
        base.remember_builder_waits = Mock()
        worker = SimpleNamespace(get_shortest_ongoing_builder_seconds=Mock(return_value=3600))
        controller = SimpleNamespace(
            completed=True, ready_reads=2, status='Đã hết thợ thường',
            observe_home=Mock(return_value='complete'),
            suggested_upgrader=Mock(return_value=worker),
            assign_builders=Mock(return_value='done'))
        engine = BotEngine.__new__(BotEngine)
        engine.settings = {'farm_finish_enabled': True, 'farm_finish_wait_builders': True,
                           'main_village_enabled': True, 'builder_base_enabled': night_enabled}
        engine.builder = base
        engine.farm_finish = controller
        engine.village = SimpleNamespace(player_tag_failures=0, wall_status='',
                                         is_in_main_village=Mock(return_value=False))
        engine.adb = SimpleNamespace(screencap=Mock(return_value=object()))
        engine.log = Mock()
        engine.set_state = Mock()
        engine._service_optional_upgrades = Mock(return_value={'status': 'idle'})
        engine._sleep_wait_for_builders = Mock(return_value=True)
        return engine, controller, base

    def test_full_main_with_night_builder_keeps_night_running(self):
        engine, controller, base = self.finished_main_engine()
        with patch('bot.bot_engine.time.monotonic', return_value=1000):
            self.assertTrue(engine._farm_finish_gate(object()))
            self.assertEqual(base.main_builders_wait_until, 4615)
            self.assertIn('builder_base_battle', [key for key, _ in base.ready_actions(1000)])
            self.assertTrue(engine._farm_finish_gate(object()))
        engine._sleep_wait_for_builders.assert_not_called()
        self.assertEqual(controller.observe_home.call_count, 1)
        self.assertTrue(engine.settings['main_village_enabled'])
        base.remember_builder_waits.assert_called_once()

    def test_main_builder_wait_routes_next_turn_to_night(self):
        base = self.manager()
        base.main_builders_wait_until = 4615
        base.main_schedule_checked = True
        base.is_home = Mock(return_value=False)
        base.is_main = Mock(return_value=True)
        base.active = False
        base.dismiss_star_bonus = Mock(return_value=False)
        base.battle.obstruction_buttons = Mock(return_value=None)
        base.battle.in_progress = False
        base.battle.find_now_failures = 0
        base.run_cycle = Mock()
        engine = BotEngine.__new__(BotEngine)
        engine.builder = base
        engine.settings = {'main_village_enabled': True, 'builder_base_enabled': True}
        engine.adb = SimpleNamespace(screencap=Mock(return_value=object()))
        engine.set_state = Mock()
        engine._service_cross_village_sleep = Mock(return_value=False)
        engine.log = Mock()
        with patch('bot.bot_engine.time.monotonic', return_value=1000):
            self.assertTrue(engine._handle_builder_base(object()))
        base.run_cycle.assert_called_once()

    def test_full_main_and_night_both_wait_for_earliest_builder(self):
        engine, controller, base = self.finished_main_engine(night_wait=1300)
        base.is_home = Mock(return_value=False)
        with patch('bot.bot_engine.time.monotonic', return_value=1000):
            self.assertTrue(engine._farm_finish_gate(object()))
        engine._sleep_wait_for_builders.assert_called_once_with(300, 'builder')
        self.assertEqual(base.night_builders_wait_until, 0)
        self.assertFalse(controller.completed)

    def test_full_main_only_preserves_wait_behavior(self):
        engine, controller, _ = self.finished_main_engine(night_enabled=False)
        with patch('bot.bot_engine.time.monotonic', return_value=1000):
            self.assertTrue(engine._farm_finish_gate(object()))
        engine._sleep_wait_for_builders.assert_called_once_with(3600)
        self.assertFalse(controller.completed)

if __name__ == '__main__': unittest.main()
