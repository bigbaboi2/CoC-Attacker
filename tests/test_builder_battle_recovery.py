import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from bot.builder_battle import BuilderBattle
from bot.combat import CombatExecutor


class BuilderBattleRecoveryTests(unittest.TestCase):
    def test_failed_deployment_pans_down_then_up_only_once(self):
        battle = BuilderBattle.__new__(BuilderBattle)
        frame = np.zeros((900, 1600, 3), dtype=np.uint8)
        battle.adb = SimpleNamespace(swipe=Mock(), screencap=Mock(return_value=frame))
        battle.base = SimpleNamespace(tap=Mock())
        battle.is_card_selected = Mock(return_value=False)
        battle.stage = 1
        battle.approach = object()
        battle.rejected_entries = [(700, 400)]
        battle.map_pan_attempts = 0
        logs = []
        cards = [('bomber', (300, 800))]
        self.assertTrue(battle.pan_map_for_deployment(logs.append, cards))
        self.assertIsNone(battle.approach)
        self.assertEqual(battle.rejected_entries, [])
        self.assertTrue(battle.pan_map_for_deployment(logs.append, cards))
        self.assertFalse(battle.pan_map_for_deployment(logs.append, cards))
        self.assertEqual([call.args[2:] for call in battle.adb.swipe.call_args_list],
                         [(1000, 550), (1000, 370)])

    def test_empty_sweep_needs_two_observations_and_logs_once(self):
        battle = BuilderBattle.__new__(BuilderBattle)
        battle.stage = 1
        battle.approach = ((800, 400), (1, 0), (0, 1), None)
        battle.cards = Mock(return_value=[('bomber', (300, 800))])
        battle.stack_count = Mock(return_value=0)
        battle.slot_state = Mock(return_value='dead')
        battle.empty_sweep_observations = 0
        battle.empty_sweep_logged = False
        battle.empty_stacks = set()
        logs = []
        frame = np.zeros((900, 1600, 3), dtype=np.uint8)
        battle.run_backup_sweep(frame, logs.append)
        self.assertEqual(logs, [])
        battle.run_backup_sweep(frame, logs.append)
        battle.run_backup_sweep(frame, logs.append)
        self.assertEqual(len(logs), 1)
        self.assertIn('hai lượt quét', logs[0])

    def test_unaccepted_backup_drop_remains_eligible_for_retry(self):
        battle = BuilderBattle.__new__(BuilderBattle)
        battle.stage = 1
        battle.approach = ((800, 400), (1, 0), (0, 1), None)
        battle.cards = Mock(return_value=[('bomber', (300, 800))])
        battle.stack_count = Mock(return_value=1)
        battle.slot_state = Mock(return_value='waiting')
        battle.drop_candidates = Mock(return_value=iter([(800, 400)]))
        battle.send_drop = Mock()
        battle.pan_map_for_deployment = Mock(return_value=True)
        battle.empty_sweep_observations = 0
        battle.empty_sweep_logged = False
        battle.empty_stacks = set()
        battle.troops = []
        frame = np.zeros((900, 1600, 3), dtype=np.uint8)
        battle.adb = SimpleNamespace(screencap=Mock(return_value=frame))
        with patch('bot.builder_battle.time.sleep'):
            battle.run_backup_sweep(frame, lambda _: None)
        self.assertEqual(battle.empty_stacks, set())
        self.assertEqual(battle.troops, [])
        battle.pan_map_for_deployment.assert_called_once()

    def test_unknown_battle_state_requests_recovery(self):
        battle = BuilderBattle.__new__(BuilderBattle)
        frame = np.zeros((900, 1600, 3), dtype=np.uint8)
        engine = SimpleNamespace(_request_recovery=Mock())
        battle.base = SimpleNamespace(village_state=None, is_home=Mock(return_value=False), engine=engine)
        battle.adb = SimpleNamespace(_guard_input=Mock(), screencap=Mock(return_value=frame))
        battle.vision = SimpleNamespace(game_fault=Mock(return_value=None))
        battle.result_button = Mock(return_value=None)
        battle.phase = Mock(return_value=None)
        battle.combat_seen = True
        battle.planned = True
        battle.match_requested = 0
        battle.unknown_since = 1
        with patch('bot.builder_battle.time.monotonic', return_value=100):
            self.assertFalse(battle.monitor(lambda _: None, seconds=1))
        engine._request_recovery.assert_called_once_with('Trận làng đêm mất trạng thái')

    def test_main_battle_pans_and_rescans_before_first_drop(self):
        combat = CombatExecutor.__new__(CombatExecutor)
        frame = np.zeros((900, 1600, 3), dtype=np.uint8)
        combat.adb = SimpleNamespace(swipe=Mock(), screencap=Mock(return_value=frame))
        combat.vision = SimpleNamespace(battle_phase=Mock(return_value='scouting'))
        combat.planner = SimpleNamespace(points=Mock(side_effect=[[], [(600, 500)]]))
        combat._log = Mock()
        with patch('bot.combat.time.sleep'):
            self.assertEqual(combat._find_drop_points_after_pan(16), [(600, 500)])
        self.assertEqual(combat.adb.swipe.call_count, 2)


if __name__ == '__main__':
    unittest.main()
