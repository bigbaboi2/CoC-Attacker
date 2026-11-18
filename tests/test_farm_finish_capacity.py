import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from bot.farm_finish import FarmFinishController
from core.vision import VisionEngine


class FarmFinishCapacityTests(unittest.TestCase):
    def test_separate_max_label_and_number_boxes_are_read(self):
        frame = np.zeros((900, 1600, 3), np.uint8)
        boxes = (
            np.array([[371, 160], [491, 160], [491, 211], [371, 211]]),
            np.array([[770, 151], [1102, 151], [1102, 216], [770, 216]]),
        )
        vision = VisionEngine.__new__(VisionEngine)
        vision._get_ocr = lambda: lambda *args, **kwargs: SimpleNamespace(
            txts=('Max:', '31 000 000'), boxes=boxes)
        vision.read_white_number = lambda row: None
        self.assertEqual(vision.read_resource_capacity_popup(frame), 31_000_000)

    def test_failed_capacity_read_waits_before_retry(self):
        village = SimpleNamespace(adb=Mock(), vision=Mock(), settings=Mock())
        controller = FarmFinishController(village)
        controller.capacity_reader.read = Mock(return_value=None)
        settings = SimpleNamespace(get=lambda key, default=None: {
            'farm_finish_enabled': True,
            'farm_finish_resources': 'gold',
            'device_serial': '127.0.0.1:16384',
        }.get(key, default))
        with patch('bot.farm_finish.time.monotonic', return_value=100):
            self.assertEqual(controller.observe_home(settings, None, Mock()), 'wait')
            self.assertEqual(controller.next_capacity_retry, 130)
            self.assertEqual(controller.observe_home(settings, None, Mock()), 'wait')
        controller.capacity_reader.read.assert_called_once()

    def test_isolated_max_row_reocr_recovers_skipped_capacity_number(self):
        frame = np.zeros((900, 1600, 3), np.uint8)
        max_box = np.array([[368, 282], [492, 282], [492, 338], [368, 338]])
        row_boxes = (
            np.array([[407, 0], [937, 0], [937, 70], [407, 70]]),
            np.array([[6, 7], [131, 7], [131, 65], [6, 65]]),
        )
        vision = VisionEngine.__new__(VisionEngine)
        vision._get_ocr = lambda: Mock(side_effect=[
            SimpleNamespace(txts=('Max:',), boxes=(max_box,)),
            SimpleNamespace(txts=('31 000 000', 'Max:'), boxes=row_boxes),
        ])
        vision.read_white_number = lambda row: None
        self.assertEqual(vision.read_resource_capacity_popup(frame), 31_000_000)

    def test_unified_popup_reads_capacity_and_treasury_in_single_ocr(self):
        frame = np.zeros((900, 1600, 3), np.uint8)
        boxes = (
            np.array([[371, 160], [491, 160], [491, 211], [371, 211]]),
            np.array([[770, 151], [1102, 151], [1102, 216], [770, 216]]),
            np.array([[360, 310], [600, 310], [600, 360], [360, 360]]),
            np.array([[790, 310], [1050, 310], [1050, 360], [790, 360]]),
        )
        ocr_mock = Mock(return_value=SimpleNamespace(
            txts=('Max:', '31 000 000', 'In treasury:', '1 250 000'), boxes=boxes))
        vision = VisionEngine.__new__(VisionEngine)
        vision._get_ocr = lambda: ocr_mock
        vision.read_white_number = lambda row: None
        popup = vision.read_resource_popup(frame)
        self.assertEqual(popup, {'capacity': 31_000_000, 'treasury': 1_250_000})
        ocr_mock.assert_called_once()

    def test_merged_zeros_do_not_truncate_31_million_to_31000(self):
        frame = np.zeros((900, 1600, 3), np.uint8)
        # Draw colon dots on the Max row inside crop (x=950..1600) so colon path triggers
        frame[84:88, 950 + 250:950 + 254] = 255
        frame[92:96, 950 + 250:950 + 254] = 255
        boxes = (
            np.array([[371, 160], [491, 160], [491, 211], [371, 211]]),
            np.array([[770, 151], [1102, 151], [1102, 216], [770, 216]]),
        )
        vision = VisionEngine.__new__(VisionEngine)
        vision._get_ocr = lambda: lambda *args, **kwargs: SimpleNamespace(
            txts=('Max:', '31 000 000'), boxes=boxes)
        vision.read_white_number = lambda row: 31_000
        popup = vision.read_resource_popup(frame)
        self.assertEqual(popup['capacity'], 31_000_000)

    def test_read_village_resources_caches_by_hud_roi_bytes(self):
        frame = np.zeros((900, 1600, 3), np.uint8)
        vision = VisionEngine.__new__(VisionEngine)
        vision._village_resources_cache = None
        vision.read_white_number = Mock(side_effect=[100, 200, 300, 400])
        first = vision.read_village_resources(frame)
        second = vision.read_village_resources(frame)
        self.assertEqual(first, second)
        self.assertEqual(vision.read_white_number.call_count, 4)

    def test_save_account_capacities_rejects_corrupted_31k_against_31m_sibling(self):
        from bot.village import VillageManager
        village = VillageManager.__new__(VillageManager)
        village.current_account_key = '#TEST'
        village._session_player_tag = '#TEST'
        village._session_legacy_key = None
        village.account_capacities = {}
        village.village_state = None
        village._wall_builder_retry_at = 0.0
        village.wall_status = ''
        village.set_wall_wait = Mock()
        village.save_account_capacities({'gold': 31_000_000, 'elixir': 31_000}, acc_key='#TEST')
        self.assertEqual(village.account_capacities['#TEST']['gold'], 31_000_000)
        self.assertNotIn('elixir', village.account_capacities['#TEST'])


if __name__ == '__main__':
    unittest.main()
