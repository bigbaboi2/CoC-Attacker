import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from bot.village import VillageManager
from core.vision import VisionEngine


class TreasureChestTests(unittest.TestCase):
    def test_chest_recognition_requires_tap_text_and_wood_center(self):
        vision = VisionEngine.__new__(VisionEngine)
        vision._get_ocr = lambda: lambda _: SimpleNamespace(txts=['Tap!'])
        frame = np.zeros((900, 1600, 3), np.uint8)
        self.assertFalse(vision.is_treasure_chest_tap_screen(frame))
        frame[400:560, 710:890] = (30, 100, 180)
        self.assertTrue(vision.is_treasure_chest_tap_screen(frame))
        vision._get_ocr = lambda: lambda _: SimpleNamespace(txts=['Battle'])
        self.assertFalse(vision.is_treasure_chest_tap_screen(frame))

    def test_five_taps_then_continue(self):
        frame = np.zeros((900, 1600, 3), np.uint8)
        vision = SimpleNamespace(is_treasure_chest_tap_screen=Mock(return_value=True),
                                 is_treasure_chest_reward=Mock(return_value=True))
        adb = SimpleNamespace(tap=Mock(), screencap=Mock(return_value=frame))
        village = VillageManager.__new__(VillageManager)
        village.vision, village.adb = vision, adb
        self.assertTrue(village.open_treasure_chest_if_present(frame))
        self.assertEqual(adb.tap.call_count, 6)
        self.assertEqual(adb.tap.call_args_list[:5],
                         [unittest.mock.call(800, 450, jitter=0, delay_after=0.08)] * 5)
        self.assertEqual(adb.tap.call_args_list[5],
                         unittest.mock.call(790, 775, jitter=0, delay_after=0.6))
        self.assertTrue(village._treasure_chest_claimed)
        self.assertFalse(village._treasure_chest_pending)

    def test_pending_reward_can_be_claimed_on_next_cycle(self):
        frame = np.zeros((900, 1600, 3), np.uint8)
        vision = SimpleNamespace(is_treasure_chest_tap_screen=Mock(return_value=False),
                                 is_treasure_chest_reward=Mock(return_value=True))
        adb = SimpleNamespace(tap=Mock())
        village = VillageManager.__new__(VillageManager)
        village.vision, village.adb = vision, adb
        village._treasure_chest_pending = True
        self.assertTrue(village.open_treasure_chest_if_present(frame))
        adb.tap.assert_called_once_with(790, 775, jitter=0, delay_after=0.6)
        self.assertTrue(village._treasure_chest_claimed)

    def test_mid_open_chest_recognized_by_rolling_pin_without_tap_text(self):
        vision = VisionEngine.__new__(VisionEngine)
        vision._get_ocr = lambda: lambda _: SimpleNamespace(txts=[])
        frame = np.zeros((900, 1600, 3), np.uint8)
        frame[400:560, 710:890] = (30, 100, 180)
        self.assertFalse(vision.is_treasure_chest_tap_screen(frame))
        import cv2
        from pathlib import Path
        pin = cv2.imread(str(Path(__file__).resolve().parents[1] / 'local_assets' / 'treasure_chest_pin_gold.png'))
        frame[726:726 + pin.shape[0], 874:874 + pin.shape[1]] = pin
        self.assertTrue(vision.is_treasure_chest_tap_screen(frame))

    def test_reward_uses_green_button_region(self):
        vision = VisionEngine.__new__(VisionEngine)
        frame = np.zeros((900, 1600, 3), np.uint8)
        self.assertFalse(vision.is_treasure_chest_reward(frame))
        frame[785:850, 705:895] = (40, 205, 95)
        self.assertTrue(vision.is_treasure_chest_reward(frame))


if __name__ == '__main__':
    unittest.main()
