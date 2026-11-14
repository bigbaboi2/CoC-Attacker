import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from bot.combat import CombatExecutor
from core.targeting import SpellTargeter


class CombatDeploymentTests(unittest.TestCase):
    def make_executor(self):
        combat = CombatExecutor.__new__(CombatExecutor)
        combat.settings = {'troop_drop_delay_ms': 80}
        combat.adb = SimpleNamespace(screencap=Mock(return_value=object()))
        combat.vision = SimpleNamespace(battle_phase=Mock(return_value='combat'))
        combat.recognizer = SimpleNamespace(read_count=Mock())
        combat.targeter = SpellTargeter()
        combat.targeter.friendly_positions = Mock(return_value=[])
        combat._verified_drop_points = [(300, 300), (1200, 300)]
        combat._prepared_spell_targets = {(300, 300): (340, 310),
                                          (1200, 300): (1160, 310)}
        combat._troop_points_by_name = {}
        combat._ultra_active = False
        combat.battle_start_time = 0
        combat.last_spell_at = 0
        combat.pending_spells = []
        combat._log = Mock()
        combat._home_button = Mock(return_value=None)
        combat._abilities = Mock()
        combat.select_slot_pos = Mock()
        combat._drops = Mock()
        return combat

    def test_spread_sends_all_remaining_before_one_count_check(self):
        combat = self.make_executor()
        combat.recognizer.read_count.return_value = 0
        combat._cast_pending_spells = Mock()
        card = {'name': 'dragon', 'x': 200, 'y': 810, 'count': 8}
        legal, left = combat._spread_verified_card(card, [(300, 300), (500, 200),
                                                          (800, 180), (1200, 300)],
                                                   [(300, 300)], 7)
        self.assertEqual(left, 0)
        self.assertEqual(len(combat._drops.call_args.args[0]), 7)
        self.assertEqual(combat.recognizer.read_count.call_count, 1)
        self.assertIn((500, 200), legal)

    def test_spell_uses_verified_entry_when_health_bars_are_unreadable(self):
        combat = self.make_executor()
        combat.pending_spells = [{'name': 'no', 'x': 400, 'y': 810, 'count': 2}]
        combat._cast_pending_spells(force=True)
        self.assertEqual(combat.pending_spells[0]['count'], 0)
        self.assertEqual([call.args[0][0] for call in combat._drops.call_args_list],
                         [(340, 310), (1160, 310)])

    def test_backup_sweep_handles_spell_when_no_troops_remain(self):
        combat = self.make_executor()
        combat.scan_deck_cards = Mock(side_effect=[
            {'troops': [], 'spells': [{'name': 'no', 'x': 400, 'y': 810, 'count': 1}]},
            {'troops': [], 'spells': []},
        ])
        combat.run_backup_sweep()
        combat._drops.assert_called_once_with([(340, 310)], spell=True)

    def test_probe_checks_later_points_after_first_four_fail(self):
        combat = self.make_executor()
        combat._wait = Mock()
        combat.recognizer.read_count.side_effect = [8]*8+[7]
        points = [(300+i*100, 300) for i in range(5)]
        legal, left = combat._verify_drop_points(
            {'name': 'dragon', 'x': 200, 'y': 810, 'count': 8}, points)
        self.assertEqual(legal, [points[4]])
        self.assertEqual(left, 7)
        self.assertEqual(combat._drops.call_count, 5)

    def test_probe_uses_latest_count_when_badge_updates_late(self):
        combat = self.make_executor()
        combat._wait = Mock()
        combat.recognizer.read_count.side_effect = [8, 8, 6, 6, 5]
        points = [(300+i*100, 300) for i in range(3)]
        legal, left = combat._verify_drop_points(
            {'name': 'dragon', 'x': 200, 'y': 810, 'count': 8}, points)
        self.assertEqual(left, 5)
        self.assertEqual(legal, points[1:])

    def test_backup_does_not_use_unverified_boundary_points(self):
        combat = self.make_executor()
        combat._verified_drop_points = []
        combat.scan_deck_cards = Mock(side_effect=[
            {'troops': [{'name': 'dragon', 'x': 200, 'y': 810, 'count': 8}], 'spells': []},
            {'troops': [], 'spells': []},
        ])
        combat.run_backup_sweep(points=[(500, 200)])
        combat._drops.assert_not_called()


if __name__ == '__main__':
    unittest.main()
