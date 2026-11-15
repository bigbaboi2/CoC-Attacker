import unittest
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from bot.treasury import TreasuryCollector


class TreasuryCastleSearchTests(unittest.TestCase):
    def make_collector(self):
        frame = object()
        adb = SimpleNamespace(
            zoom_out_map=Mock(), screencap=Mock(return_value=frame), tap=Mock(),
            _guard_input=Mock(),
        )
        manager = SimpleNamespace(adb=adb, deselect_all=Mock(return_value=True))
        collector = TreasuryCollector.__new__(TreasuryCollector)
        collector.adb = adb
        collector.manager = manager
        collector.vision = SimpleNamespace(is_in_main_village=Mock(return_value=True))
        collector.detector = SimpleNamespace(detect=Mock(return_value=[]))
        collector.words = Mock(return_value=[])
        return collector, frame

    def test_no_evidence_never_taps_a_village_grid(self):
        collector, frame = self.make_collector()
        collector.treasury_button = Mock(return_value=None)
        log=Mock()
        with patch('bot.treasury.time.sleep'):
            result = collector.open_castle(frame,log)

        self.assertEqual(result, (None,None))
        collector.adb.tap.assert_not_called()
        collector.manager.deselect_all.assert_not_called()
        self.assertEqual(collector.detector.detect.call_count,2)
        self.assertTrue(any('không tap dò' in call.args[0] for call in log.call_args_list))

    def test_stops_when_village_screen_disappears(self):
        collector, frame = self.make_collector()
        collector.treasury_button = Mock(return_value=None)
        collector.detector.detect.return_value=[{'x':430,'y':370}]
        collector.vision.is_in_main_village = Mock(side_effect=[True, False])
        with patch('bot.treasury.time.sleep'):
            result = collector.open_castle(frame)

        self.assertEqual(result, (None, None))
        self.assertEqual(collector.adb.tap.call_count, 1)
        collector.manager.deselect_all.assert_not_called()

    def test_image_hit_is_only_a_hint(self):
        collector, frame = self.make_collector()
        collector.detector.detect.return_value = [{'x': 430, 'y': 370}, {'x': 580, 'y': 370}]
        collector.treasury_button = Mock(side_effect=[None, None, (820, 740)])
        with patch('bot.treasury.time.sleep'):
            result = collector.open_castle(frame)

        self.assertEqual(result, (frame, (820, 740)))
        self.assertEqual(collector.adb.tap.call_args_list[0].args, (430, 370))
        self.assertEqual(collector.adb.tap.call_args_list[1].args, (580, 370))
        collector.adb.zoom_out_map.assert_not_called()

    def test_matching_clan_name_gives_one_castle_hint(self):
        collector, frame = self.make_collector()
        collector.words.side_effect = [
            [('the amigos', 816, 292)],
            [('cu', 175, 86), ('the amigos', 210, 116)],
        ]
        collector.treasury_button = Mock(side_effect=[None, (820, 740)])
        with patch('bot.treasury.time.sleep'):
            result = collector.open_castle(frame)

        self.assertEqual(result, (frame, (820, 740)))
        collector.adb.tap.assert_called_once()
        self.assertEqual(collector.adb.tap.call_args.args, (816, 337))
        collector.adb.zoom_out_map.assert_not_called()

    def test_enlarged_map_tile_finds_small_clan_label(self):
        collector, frame = self.make_collector()
        collector.words.return_value=[('the amigos',854,528)]

        hints=collector.tiled_clan_hints(frame,{'the amigos'})

        self.assertEqual(hints,[(854,558,'tên bang trên bản đồ')])
        collector.words.assert_called_once_with(frame,(700,400,220,170),scale=2)

    def test_requires_castle_title_beside_treasury_button(self):
        collector, frame = self.make_collector()
        collector.words.return_value = [('cannon level 1', 800, 600), ('treasury', 850, 740)]
        self.assertIsNone(collector.treasury_button(frame))
        collector.words.return_value = [('clan castle level 4', 800, 600), ('treasury', 850, 740)]
        self.assertEqual(collector.treasury_button(frame), (850, 740))

    def test_old_castle_miss_does_not_keep_thirty_minute_cooldown(self):
        settings = Mock()
        settings.filepath = 'config.json'
        settings.get.return_value = '127.0.0.1:16384'
        manager = SimpleNamespace(settings=settings, vision=SimpleNamespace(asset_dir=Path('assets')),
                                  adb=Mock(), _treasury_next_check=0)
        saved = json.dumps({'device_serial': '127.0.0.1:16384', 'state': {
            'last_result': 'castle_not_found', 'next_check_at': 2800,
        }})
        with patch('bot.treasury.Path.read_text', return_value=saved), patch('bot.treasury.time.time', return_value=1000):
            collector = TreasuryCollector(manager)

        self.assertEqual(manager._treasury_next_check, 0)
        self.assertEqual(collector.memory['castle_search_version'], 3)

    def test_castle_miss_retries_soon_and_cooldown_is_logged_once(self):
        collector, _ = self.make_collector()
        collector.manager.settings = {'auto_collect_treasury': True,
                                      'treasury_star_bonus_enabled': True,
                                      'treasury_war_rewards_enabled': True}
        collector.manager._treasury_next_check = 0
        collector.memory = {}
        collector.save_memory = Mock()
        collector._run = Mock(return_value={'verified': False, 'reason': 'castle_not_found'})
        log = Mock()
        with patch('bot.treasury.time.time', return_value=1000):
            collector.run(log)
        self.assertEqual(collector.manager._treasury_next_check, 1120)
        self.assertEqual(collector.memory['next_check_at'], 1120)
        self.assertTrue(any('2 phút' in call.args[0] for call in log.call_args_list))

        collector._run = TreasuryCollector._run.__get__(collector)
        log.reset_mock()
        with patch('bot.treasury.time.time', return_value=1050):
            self.assertEqual(collector.run(log)['reason'], 'cooldown')
            self.assertEqual(collector.run(log)['reason'], 'cooldown')
        self.assertEqual(log.call_count, 1)
        self.assertIn('lượt trước: castle_not_found', log.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
