import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from bot.builder_battle import BuilderBattle
from bot.combat import CombatExecutor
from core.vision import VisionEngine


class BattleSpeedupTests(unittest.TestCase):
    def test_battle_countdown_formats(self):
        parse = VisionEngine.parse_battle_seconds
        self.assertEqual(parse(['Battle ends in', '1m 00s']), 60)
        self.assertEqual(parse(['Attack ends in', '59s']), 59)
        self.assertEqual(parse(['Battle ends in', '0:45']), 45)
        self.assertEqual(parse(['Battle ends in', '2m 10s']), 130)
        self.assertIsNone(parse(['Battle ends in']))

    def test_builder_speedup_waits_for_final_minute_and_resets_per_stage(self):
        battle = BuilderBattle.__new__(BuilderBattle)
        battle.stage = 1
        battle.speed_stage = 0
        battle.next_speed_check_at = 0
        battle.vision = SimpleNamespace(battle_seconds_remaining=Mock(return_value=61))
        battle.base = SimpleNamespace(match=Mock(return_value=(800, 400)),
                                      green_button=Mock(return_value=True), tap=Mock())
        battle.enable_speedup(object(), Mock())
        battle.base.match.assert_not_called()
        battle.next_speed_check_at = 0
        battle.vision.battle_seconds_remaining.return_value = 60
        battle.enable_speedup(object(), Mock())
        battle.base.tap.assert_called_once()
        battle.stage = 2
        battle.next_speed_check_at = 0
        battle.enable_speedup(object(), Mock())
        self.assertEqual(battle.base.tap.call_count, 2)

    def test_main_speedup_only_once_after_countdown_reaches_60(self):
        combat = CombatExecutor.__new__(CombatExecutor)
        combat._speedup_used = False
        combat._next_speed_check_at = 0
        combat.vision = SimpleNamespace(battle_seconds_remaining=Mock(return_value=61),
                                        match_template=Mock(return_value=(800, 400, 90, 93, .95)))
        combat.adb = SimpleNamespace(tap=Mock())
        combat._enable_final_minute_speedup(object(), Mock())
        combat.vision.match_template.assert_not_called()
        combat._next_speed_check_at = 0
        combat.vision.battle_seconds_remaining.return_value = 59
        combat._enable_final_minute_speedup(object(), Mock())
        combat._enable_final_minute_speedup(object(), Mock())
        combat.adb.tap.assert_called_once_with(800, 400, jitter=0, delay_after=.15)


if __name__ == '__main__':
    unittest.main()
