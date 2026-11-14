import unittest
from pathlib import Path

import cv2
import numpy as np
from types import SimpleNamespace
from unittest.mock import Mock

from core.vision import VisionEngine
from bot.combat import CombatExecutor


class DimmedLootTests(unittest.TestCase):
    def test_combat_reads_dimmed_loot_while_dialog_is_armed(self):
        combat = CombatExecutor.__new__(CombatExecutor)
        combat._surrender_armed = True
        combat._loot_initial = {'gold': 100, 'known': {'gold': True}}
        combat._loot_gained = {}
        combat.vision = SimpleNamespace(read_loot=Mock(return_value={
            'gold': 50, 'known': {'gold': True}}))
        combat._update_loot(object())
        self.assertTrue(combat.vision.read_loot.call_args.kwargs['dimmed'])

    def test_surrender_overlay_loot_is_read_after_brightening(self):
        crop = cv2.imread(str(Path(__file__).resolve().parent / 'fixtures' / 'surrender_loot_dimmed.png'))
        frame = np.zeros((900, 1600, 3), dtype=np.uint8)
        frame[105:242, 80:305] = crop
        vision = VisionEngine()
        self.assertFalse(vision.read_loot(frame)['valid'])
        loot = vision.read_loot(frame, dimmed=True)
        self.assertEqual(loot['gold'], 1016431)
        self.assertEqual(loot['elixir'], 506634)
        self.assertTrue(loot['known']['gold'] and loot['known']['elixir'])


if __name__ == '__main__':
    unittest.main()
