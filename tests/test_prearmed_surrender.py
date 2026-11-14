import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bot.combat import CombatExecutor
from core.combat_policy import BattleEndPolicy


class PrearmedSurrenderTests(unittest.TestCase):
    def manager(self):
        combat = CombatExecutor.__new__(CombatExecutor)
        combat._surrender_armed = False
        combat._surrender_empty_checks = 0
        combat._next_surrender_arm_check_at = 0
        combat._deployment_pending = False
        combat.vision = SimpleNamespace(
            battle_phase=Mock(return_value='combat'),
            match_template=Mock(side_effect=lambda _, name, *_a, **_k:
                                (1000, 570) if name == 'okay' else (100, 650)))
        combat.adb = SimpleNamespace(tap=Mock(), screencap=Mock(return_value=object()))
        combat.scan_deck_cards = Mock()
        return combat

    def test_waits_for_troops_and_usable_spells_but_ignores_invisibility(self):
        combat = self.manager()
        combat.scan_deck_cards.side_effect = [
            {'troops': [{'name': 'dragon', 'count': 1}], 'spells': []},
            {'troops': [], 'spells': [{'name': 'haste_spell', 'count': 1}]},
            {'troops': [], 'spells': [{'name': 'invisibility_spell', 'count': 1}]},
            {'troops': [], 'spells': [{'name': 'invisibility_spell', 'count': 1}]},
        ]
        policy = BattleEndPolicy()
        for _ in range(4):
            combat._next_surrender_arm_check_at = 0
            combat._arm_surrender_if_ready(object(), policy, Mock())
        self.assertTrue(combat._surrender_armed)
        combat.adb.tap.assert_called_once_with(100, 650, jitter=0, delay_after=.15)

    def test_disabled_auto_surrender_never_opens_dialog(self):
        combat = self.manager()
        combat._arm_surrender_if_ready(object(), BattleEndPolicy(enabled=False), Mock())
        combat.scan_deck_cards.assert_not_called()
        combat.adb.tap.assert_not_called()

    def test_loot_goal_keeps_prearmed_dialog(self):
        combat = self.manager()
        combat.scan_deck_cards.return_value = {'troops': [], 'spells': []}
        policy = BattleEndPolicy(loot_enabled=True)
        combat._arm_surrender_if_ready(object(), policy, Mock())
        combat._next_surrender_arm_check_at = 0
        combat._arm_surrender_if_ready(object(), policy, Mock())
        self.assertTrue(combat._surrender_armed)

    def test_armed_exit_confirms_existing_dialog_without_reopening_it(self):
        combat = self.manager()
        combat._surrender_armed = True
        combat._home_button = Mock(return_value=None)
        combat._wait_for_battle_result = Mock(return_value=True)
        log = Mock()
        self.assertTrue(combat._surrender_and_exit(log))
        combat.adb.tap.assert_called_once_with(1000, 570, jitter=0, delay_after=.3)
        combat._wait_for_battle_result.assert_called_once_with(log, confirmed=True)
        self.assertFalse(combat._surrender_armed)

    def test_final_minute_releases_dialog_for_four_times_speed(self):
        combat = self.manager()
        combat._surrender_armed = True
        combat._speedup_used = False
        combat._next_speed_check_at = 0
        combat.vision.battle_seconds_remaining = Mock(return_value=60)
        combat.vision.match_template = Mock(return_value=None)
        combat.adb.keyevent = Mock()
        combat._enable_final_minute_speedup = Mock()
        with patch('bot.combat.time.sleep'):
            self.assertTrue(combat._speedup_after_prearming(object(), Mock()))
        combat.adb.keyevent.assert_called_once_with(4)
        combat._enable_final_minute_speedup.assert_called_once()
        self.assertFalse(combat._surrender_armed)


if __name__ == '__main__':
    unittest.main()
