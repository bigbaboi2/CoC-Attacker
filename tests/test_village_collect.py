import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

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

    def test_detector_distinguishes_normal_cream_and_red_full_bubbles(self):
        de_icon = cv2.imread(str(Path(__file__).resolve().parents[1] / 'local_assets' / 'collect_dark_elixir.png'))
        h, w = de_icon.shape[:2]
        # Build a synthetic storage-full red-orange bubble from the normal cream bubble
        hsv = cv2.cvtColor(de_icon, cv2.COLOR_BGR2HSV)
        cream_mask = (hsv[:, :, 0] >= 20) & (hsv[:, :, 0] <= 55) & (hsv[:, :, 1] <= 105) & (hsv[:, :, 2] >= 135)
        red_icon = de_icon.copy()
        red_hsv = hsv.copy()
        red_hsv[cream_mask, 0] = 10
        red_hsv[cream_mask, 1] = 210
        red_hsv[cream_mask, 2] = 225
        red_icon = cv2.cvtColor(red_hsv, cv2.COLOR_HSV2BGR)

        frame = np.random.default_rng(7).integers(20, 65, (900, 1600, 3), dtype=np.uint8)
        frame[300:300+h, 450:450+w] = de_icon
        frame[300:300+h, 650:650+w] = red_icon

        village = VillageManager.__new__(VillageManager)
        village.vision = SimpleNamespace(
            load_template=lambda path: de_icon if path.endswith('collect_dark_elixir.png') else None
        )
        normal, full = village.find_collectors(frame, include_full=True)
        self.assertEqual(len(normal), 1)
        self.assertAlmostEqual(normal[0]['x'], 450 + w // 2, delta=2)
        self.assertEqual(len(full), 1)
        self.assertAlmostEqual(full[0]['x'], 650 + w // 2, delta=2)
        self.assertTrue(full[0]['full'])

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
        self.assertIsNotNone(village.last_collect_time)
        self.assertEqual(village.collect_resources(object(), logs.append), 0)
        village.adb.tap.assert_called_once()

    def test_empty_pass_waits_full_configured_interval(self):
        village = self.make_village([[], [], [], []])
        logs = []
        with patch('bot.village.time.monotonic', return_value=1000):
            self.assertEqual(village.collect_resources(object(), logs.append), 0)
        with patch('bot.village.time.monotonic', return_value=1001):
            self.assertEqual(village.collect_resources(object(), logs.append), 0)
        self.assertEqual(village.find_collectors.call_count, 2)
        self.assertTrue(any('Bỏ qua lượt này' in log for log in logs))
        with patch('bot.village.time.monotonic', return_value=4600):
            self.assertEqual(village.collect_resources(object(), logs.append), 0)
        self.assertEqual(village.find_collectors.call_count, 4)
        self.assertTrue(any('quét lại sau 60 phút' in log for log in logs))

    def test_no_bubbles_after_layout_retries_once_at_zoomed_out_view(self):
        bubble = dict(resource='gold', x=500, y=400, score=.91)
        village = self.make_village([[], [bubble], [bubble], []])
        logs = []
        self.assertEqual(village.collect_resources(object(), logs.append), 1)
        village.adb.zoom_out_map.assert_called_once_with(times=2)
        village.adb.tap.assert_called_once_with(500, 400, jitter=0, delay_after=.15)
        self.assertTrue(any('thu nhỏ bản đồ' in log for log in logs))

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

    def test_bubble_turning_red_after_tap_stops_resource_and_is_not_counted_verified(self):
        b1 = dict(resource='dark_elixir', x=814, y=127, score=.85)
        b2 = dict(resource='dark_elixir', x=880, y=140, score=.84)
        full_b1 = dict(resource='dark_elixir', x=814, y=127, score=.68, full=True)
        screen = np.zeros((900, 1600, 3), dtype=np.uint8)
        village = VillageManager.__new__(VillageManager)
        village.settings = {'collect_interval_min': 60}
        village.last_collect_time = None
        village.adb = Mock()
        village.adb.screencap.return_value = screen
        village.is_in_main_village = Mock(return_value=True)

        def fake_find_collectors(_scr, include_full=False):
            # Before tap: b1, b2 are normal; After tap: b1 turns full (red)
            if village.adb.tap.call_count == 0:
                return ([b1, b2], []) if include_full else [b1, b2]
            return ([b2], [full_b1]) if include_full else [b2]

        village.find_collectors = Mock(side_effect=fake_find_collectors)
        logs = []
        self.assertEqual(village.collect_resources(screen, logs.append), 1)
        village.adb.tap.assert_called_once_with(814, 127, jitter=0, delay_after=.15)
        self.assertTrue(any('chuyển sang biểu tượng đầy kho (nền đỏ)' in log for log in logs))


if __name__ == '__main__':
    unittest.main()
