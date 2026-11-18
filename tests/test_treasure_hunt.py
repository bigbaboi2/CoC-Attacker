import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import cv2
import numpy as np

from bot.bot_engine import BotEngine
from bot.combat import CombatExecutor
from core.vision import VisionEngine


class TreasureHuntTests(unittest.TestCase):
    def test_collected_counter_requires_valid_fraction(self):
        frame = np.zeros((900, 1600, 3), np.uint8)
        vision = VisionEngine.__new__(VisionEngine)
        ocr = Mock(return_value=SimpleNamespace(txts=['Collected:', '0/4']))
        vision._get_ocr = lambda: ocr
        self.assertEqual(vision.read_treasure_hunt_collected(frame), (0, 4))
        ocr.return_value = SimpleNamespace(txts=['Collected:', '4/4'])
        self.assertEqual(vision.read_treasure_hunt_collected(frame), (4, 4))
        ocr.return_value = SimpleNamespace(txts=['Collected:', '5/4'])
        self.assertIsNone(vision.read_treasure_hunt_collected(frame))

    def test_hud_icon_matches_reference_only_in_damage_corner(self):
        vision = VisionEngine.__new__(VisionEngine)
        icon = cv2.imread(str(Path(__file__).resolve().parents[1] /
                              'local_assets' / 'treasure_hunt_chest_hud.png'))
        frame = np.zeros((900, 1600, 3), np.uint8)
        self.assertFalse(vision.has_treasure_hunt_chest_icon(frame))
        frame[49:49 + icon.shape[0], 1508:1508 + icon.shape[1]] = icon
        self.assertTrue(vision.has_treasure_hunt_chest_icon(frame))
        frame[49:49 + icon.shape[0], 1508:1508 + icon.shape[1]] = 0
        frame[49:49 + icon.shape[0], 1100:1100 + icon.shape[1]] = icon
        self.assertFalse(vision.has_treasure_hunt_chest_icon(frame))

    def make_engine(self, collected, settings=None):
        config = dict(auto_surrender=True, end_on_treasure_hunt=True,
                      end_on_stars=True, end_on_time=False,
                      end_on_loot=False, end_on_destruction=False)
        config.update(settings or {})
        engine = BotEngine.__new__(BotEngine)
        engine.settings = SimpleNamespace(get=lambda key, default=None: config.get(key, default))
        engine.vision = SimpleNamespace(read_treasure_hunt_collected=Mock(side_effect=collected))
        engine.adb = SimpleNamespace(screencap=Mock(return_value=object()))
        engine.combat = SimpleNamespace(treasure_hunt_active=False)
        engine.village = SimpleNamespace(_treasure_chest_claimed=True)
        engine.log = Mock()
        return engine

    def test_gate_arms_only_for_confirmed_incomplete_quota(self):
        engine = self.make_engine([(0, 4), (0, 4)])
        engine._configure_treasure_hunt(object())
        self.assertTrue(engine.combat.treasure_hunt_active)
        self.assertFalse(engine.village._treasure_chest_claimed)

    def test_full_or_unreadable_quota_uses_normal_end_conditions(self):
        for readings in ([(4, 4), (4, 4)], [(0, 4), None]):
            with self.subTest(readings=readings):
                engine = self.make_engine(readings)
                engine._configure_treasure_hunt(object())
                self.assertFalse(engine.combat.treasure_hunt_active)

    def test_gate_needs_another_selected_end_condition(self):
        engine = self.make_engine([], dict(end_on_stars=False))
        engine._configure_treasure_hunt(object())
        self.assertFalse(engine.combat.treasure_hunt_active)
        engine.vision.read_treasure_hunt_collected.assert_not_called()

    def test_battle_end_waits_for_two_chest_icon_frames(self):
        combat = CombatExecutor.__new__(CombatExecutor)
        combat.treasure_hunt_active = True
        combat._treasure_found = False
        combat._treasure_icon_streak = 0
        combat.vision = SimpleNamespace(has_treasure_hunt_chest_icon=Mock(
            side_effect=[False, True, True]))
        log = Mock()
        self.assertFalse(combat._treasure_end_ready(object(), log))
        self.assertFalse(combat._treasure_end_ready(object(), log))
        self.assertTrue(combat._treasure_end_ready(object(), log))
        self.assertEqual(log.call_count, 1)
        self.assertTrue(combat._treasure_end_ready(object(), log))
        self.assertEqual(combat.vision.has_treasure_hunt_chest_icon.call_count, 3)


if __name__ == '__main__':
    unittest.main()
