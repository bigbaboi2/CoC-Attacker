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

    def test_matching_clan_name_gives_castle_hints(self):
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
        self.assertEqual(collector.adb.tap.call_args.args, (816, 284))
        collector.adb.zoom_out_map.assert_not_called()

    def test_enlarged_map_tile_finds_small_clan_label(self):
        collector, frame = self.make_collector()
        collector.words.return_value=[('the amigos',854,528)]

        hints=collector.tiled_clan_hints(frame,{'the amigos'})

        self.assertEqual(hints,[(854,520,'tên bang trên bản đồ'),
                                (854,503,'tên bang trên bản đồ'),
                                (854,558,'tên bang trên bản đồ')])
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

    def test_storage_space_error_recognizes_game_toast(self):
        collector, frame = self.make_collector()
        collector.words.return_value = [('not enough storage space!', 800, 260)]
        self.assertTrue(collector.storage_space_error(frame))
        collector.words.assert_called_with(frame, (420, 205, 850, 180), scale=2)
        collector.words.return_value = [('storage available', 800, 260)]
        self.assertFalse(collector.storage_space_error(frame))

    def test_confirmed_transfer_reports_storage_full_without_retrying_collect(self):
        collector, home = self.make_collector()
        treasury, confirmation, toast = object(), object(), object()
        collector.manager.settings = {'auto_collect_treasury': True,
                                      'treasury_star_bonus_enabled': True,
                                      'treasury_war_rewards_enabled': True,
                                      'treasury_check_interval_min': 30}
        collector.manager._treasury_next_check = 0
        collector.memory = {}
        collector.save_memory = Mock()
        collector.read_balances = Mock(return_value={'gold': 1000, 'elixir': 1000, 'dark_elixir': 50})
        collector.adb.screencap.side_effect = [home, home, treasury, confirmation, toast]
        collector.vision.is_in_main_village.side_effect = [True, False, True]
        collector.vision.read_village_resources = Mock(return_value={
            'gold': 358950, 'elixir': 442697, 'dark_elixir': 37500,
            'valid': {'gold': True, 'elixir': True, 'dark_elixir': True}})
        collector.open_castle = Mock(return_value=(home, (1100, 740)))
        collector.collect_button = Mock(return_value=(800, 680))
        collector.words.return_value = [('collect resources', 700, 300),
                                        ('treasury', 800, 450), ('okay', 900, 600)]
        collector.storage_space_error = Mock(return_value=True)
        collector.close = Mock()
        log = Mock()

        result = collector.run(log)

        self.assertEqual(result['reason'], 'storage_full')
        self.assertFalse(result['verified'])
        self.assertEqual(collector.adb.tap.call_count, 3)
        self.assertEqual(collector.memory['last_result'], 'storage_full')
        self.assertTrue(any('không đủ chỗ' in call.args[0] for call in log.call_args_list))

    def test_castle_not_found_reuses_balances_once_without_retapping_hud(self):
        collector, home = self.make_collector()
        collector.manager.settings = {'auto_collect_treasury': True, 'treasury_check_interval_min': 30}
        collector.manager._treasury_next_check = 0
        collector.memory = {}
        collector.save_memory = Mock()
        collector.read_balances = Mock(return_value={'gold': 1000, 'elixir': 1000, 'dark_elixir': 50})
        collector.vision.read_village_resources = Mock(return_value={
            'gold': 100, 'elixir': 100, 'dark_elixir': 10,
            'valid': {'gold': True, 'elixir': True, 'dark_elixir': True}})
        collector.open_castle = Mock(return_value=(None, None))
        collector.close = Mock()
        log = Mock()

        with patch('bot.treasury.time.time', return_value=1000):
            first = collector.run(log)
        self.assertEqual(first['reason'], 'castle_not_found')
        self.assertEqual(collector.read_balances.call_count, 1)

        with patch('bot.treasury.time.time', return_value=1200):
            second = collector.run(log)
        self.assertEqual(second['reason'], 'castle_not_found')
        self.assertEqual(collector.read_balances.call_count, 1)

    def test_treasury_read_balances_saves_capacities_so_reader_skips_hud(self):
        from bot.farm_finish import ResourceCapacityReader
        from bot.village import VillageManager
        import numpy as np
        home = np.zeros((900, 1600, 3), np.uint8)
        pop = np.ones((900, 1600, 3), np.uint8)
        adb = SimpleNamespace(
            screencap=Mock(side_effect=[home, pop, pop, home, pop, pop, home, pop, pop]),
            tap=Mock(), _guard_input=Mock(),
        )
        vision = SimpleNamespace(
            is_in_main_village=Mock(return_value=True),
            read_resource_popup=Mock(side_effect=lambda f: None if f is home else (
                {'capacity': 31_000_000, 'treasury': 0} if adb.tap.call_count <= 4
                else {'capacity': 500_000, 'treasury': 0}
            )),
        )
        manager = VillageManager.__new__(VillageManager)
        manager.adb = adb
        manager.vision = vision
        manager.settings = {'device_serial': '127.0.0.1:16384'}
        manager.current_account_key = '#TEST'
        manager._session_player_tag = '#TEST'
        manager._session_legacy_key = None
        manager.account_capacities = {}
        manager.village_state = None
        manager._wall_builder_retry_at = 0.0
        manager.wall_status = ''
        manager._hud_treasury_cache = {}
        manager._hud_tooltip_clean = False
        manager.set_wall_wait = Mock()
        manager.check_and_sync_account = Mock(return_value='#TEST')

        collector = TreasuryCollector.__new__(TreasuryCollector)
        collector.manager = manager
        collector.adb = adb
        collector.vision = vision
        with patch('bot.treasury.time.sleep'):
            balances = collector.read_balances(Mock())
        self.assertEqual(balances, {'gold': 0, 'elixir': 0, 'dark_elixir': 0})
        self.assertEqual(manager.account_capacities['#TEST']['gold'], 31_000_000)
        self.assertEqual(manager.account_capacities['#TEST']['elixir'], 31_000_000)
        self.assertEqual(manager.account_capacities['#TEST']['dark_elixir'], 500_000)

        taps_after_treasury = adb.tap.call_count
        adb.screencap = Mock(return_value=home)
        reader = ResourceCapacityReader(manager)
        caps = reader.read(('gold', 'elixir'), Mock())
        self.assertEqual(caps, {'gold': 31_000_000, 'elixir': 31_000_000})
        self.assertEqual(adb.tap.call_count, taps_after_treasury)
        self.assertFalse(reader.last_tapped_hud)

    def test_capacity_reader_populates_treasury_cache_so_treasury_reuses_it(self):
        from bot.farm_finish import ResourceCapacityReader
        from bot.village import VillageManager
        import numpy as np
        home = np.zeros((900, 1600, 3), np.uint8)
        pop = np.ones((900, 1600, 3), np.uint8)
        adb = SimpleNamespace(
            screencap=Mock(side_effect=[home, pop, pop, home, pop, pop, home, pop, pop]),
            tap=Mock(), _guard_input=Mock(),
        )
        popups = iter([
            {'capacity': 31_000_000, 'treasury': 123_456}, {'capacity': 31_000_000, 'treasury': 123_456},
            {'capacity': 31_000_000, 'treasury': 234_567}, {'capacity': 31_000_000, 'treasury': 234_567},
            {'capacity': 500_000, 'treasury': 3_000}, {'capacity': 500_000, 'treasury': 3_000},
        ])
        vision = SimpleNamespace(
            is_in_main_village=Mock(return_value=True),
            read_resource_popup=Mock(side_effect=lambda f: None if f is home else next(popups)),
        )
        manager = VillageManager.__new__(VillageManager)
        manager.adb = adb
        manager.vision = vision
        manager.settings = {'device_serial': '127.0.0.1:16384'}
        manager.current_account_key = '#TEST'
        manager._session_player_tag = '#TEST'
        manager._session_legacy_key = None
        manager.account_capacities = {}
        manager.village_state = None
        manager._wall_builder_retry_at = 0.0
        manager.wall_status = ''
        manager._hud_treasury_cache = {}
        manager._hud_tooltip_clean = False
        manager.set_wall_wait = Mock()
        manager.check_and_sync_account = Mock(return_value='#TEST')

        reader = ResourceCapacityReader(manager)
        with patch('bot.farm_finish.time.sleep'):
            caps = reader.read(('gold', 'elixir'), Mock())
        self.assertEqual(caps, {'gold': 31_000_000, 'elixir': 31_000_000})
        adb.tap.reset_mock()

        collector = TreasuryCollector.__new__(TreasuryCollector)
        collector.manager = manager
        collector.adb = adb
        collector.vision = vision
        with patch('bot.treasury.time.sleep'):
            balances = collector.read_balances(Mock())
        self.assertEqual(balances, {'gold': 123_456, 'elixir': 234_567, 'dark_elixir': 3_000})
        # Only dark_elixir should be tapped + dismissed (2 taps total)
        self.assertEqual(adb.tap.call_count, 2)


if __name__ == '__main__':
    unittest.main()
