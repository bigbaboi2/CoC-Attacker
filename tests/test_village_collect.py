import unittest
from types import SimpleNamespace
from unittest.mock import Mock

import cv2
import numpy as np
from pathlib import Path

from bot.village import VillageManager


class VillageCollectTests(unittest.TestCase):
    def test_detector_accepts_real_color_template(self):
        icon = cv2.imread(str(Path(__file__).resolve().parents[1] / 'local_assets' / 'collect_gold.png'))
        frame = np.random.default_rng(4).integers(15, 70, (900, 1600, 3), dtype=np.uint8)
        h, w = icon.shape[:2]
        frame[380:380+h, 500:500+w] = icon
        village = VillageManager.__new__(VillageManager)
        village.vision = SimpleNamespace(load_template=lambda path: icon if path.endswith('collect_gold.png') else None)
        matches = village.find_collectors(frame)
        self.assertTrue(any(m['resource'] == 'gold' and abs(m['x']-(500+w//2)) <= 2
                            and abs(m['y']-(380+h//2)) <= 2 for m in matches))

    def make_village(self, detections):
        village = VillageManager.__new__(VillageManager)
        village.settings = {'collect_interval_min': 60}
        village.last_collect_time = None
        village.adb = Mock()
        village.adb.screencap.return_value = object()
        village.is_in_main_village = Mock(return_value=True)
        village.find_collectors = Mock(side_effect=detections)
        return village

    def test_unstable_match_is_not_tapped(self):
        bubble = dict(resource='gold', x=500, y=400, score=.91)
        village = self.make_village([[bubble], [], []])
        self.assertEqual(village.collect_resources(object()), 0)
        village.adb.tap.assert_not_called()

    def test_persistent_match_stops_after_one_tap(self):
        bubble = dict(resource='gold', x=500, y=400, score=.91)
        village = self.make_village([[bubble], [bubble], [bubble]])
        logs = []
        self.assertEqual(village.collect_resources(object(), logs.append), 0)
        village.adb.tap.assert_called_once()
        self.assertTrue(any('không biến mất' in log for log in logs))
        self.assertIsNone(village.last_collect_time)

    def test_disappearing_match_is_verified(self):
        bubble = dict(resource='gold', x=500, y=400, score=.91)
        village = self.make_village([[bubble], [bubble], [], []])
        logs = []
        self.assertEqual(village.collect_resources(object(), logs.append), 1)
        village.adb.tap.assert_called_once_with(500, 400, jitter=0, delay_after=.15)
        self.assertIsNotNone(village.last_collect_time)
        self.assertTrue(any('Tap vàng tại (500, 400)' in log for log in logs))
        self.assertTrue(any('Xác minh vàng tại (500, 400)' in log for log in logs))
        self.assertTrue(any('còn 0 biểu tượng' in log for log in logs))


if __name__ == '__main__':
    unittest.main()
