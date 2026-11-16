import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from bot.suggested_upgrades import SuggestedUpgrader


class CraftingStationCandidateTests(unittest.TestCase):
    def setUp(self):
        self.worker = SuggestedUpgrader.__new__(SuggestedUpgrader)
        self.worker.base_regular_names = {'townhall'}
        self.worker.custom_regular_names = {'townhall', 'craftingstation'}
        self.worker.village = SimpleNamespace(settings={
            'auto_upgrade_buildings': True,
            'lock_townhall_upgrade': True,
            'building_upgrade_resources': {'gold': True, 'elixir': False, 'dark_elixir': False},
            'building_upgrade_categories': {'defense': True},
            'building_upgrade_whitelist': {'craftingstation': True},
        })
        self.worker.ui = SimpleNamespace(duration_seconds=lambda _: None)
        self.screen = np.zeros((900, 1600, 3), np.uint8)
        self.words = [
            ('Town Hall', 1300, 100), ('16 000 000', 1470, 123),
            ('Crafting Station', 1300, 200), ('5 000 000', 1470, 223),
        ]

    def test_custom_menu_keeps_enabled_crafting_station(self):
        with patch.object(SuggestedUpgrader, 'currency', return_value='gold'):
            rows = self.worker.menu_candidates(self.screen, self.words, custom_mode=True)
        self.assertEqual([(row['name'], row['cost']) for row in rows],
                         [('Crafting Station', 5_000_000)])

    def test_crafting_station_still_obeys_whitelist(self):
        self.worker.village.settings['building_upgrade_whitelist']['craftingstation'] = False
        with patch.object(SuggestedUpgrader, 'currency', return_value='gold'):
            rows = self.worker.menu_candidates(self.screen, self.words, custom_mode=True)
        self.assertEqual(rows, [])

    def test_specific_target_cannot_override_disabled_crafting_station(self):
        settings = self.worker.village.settings
        settings['building_upgrade_priority'] = 'specific'
        settings['building_upgrade_specific_target'] = 'craftingstation'
        settings['building_upgrade_whitelist']['craftingstation'] = False
        candidate = {'name': 'Crafting Station', 'resource': 'gold'}
        self.assertFalse(self.worker.is_candidate_allowed(candidate, custom_mode=True))

    def test_plan_waits_for_gold_instead_of_reporting_no_building(self):
        self.worker.village.settings['building_upgrade_priority'] = 'time_min'
        self.worker.village.settings['building_upgrade_use_menu_filter'] = True
        self.worker.village.walls_confirmed_maxed = True
        self.worker.village.village_state = None
        self.worker.adb = SimpleNamespace(screencap=lambda: self.screen)
        self.worker.check = lambda: None
        self.worker.words = lambda *_: self.words
        self.worker.apply_menu_filter_if_needed = lambda: True
        self.worker.ui.menu_signature = lambda words: tuple(words)
        self.worker.ui.scroll_menu = Mock()
        self.worker.unaffordable = {}
        wallet = {'gold': 2_781_958, 'elixir': 5_144_667,
                  'valid': {'gold': True, 'elixir': True}}
        with patch.object(SuggestedUpgrader, 'currency', return_value='gold'):
            plan, needed, reason = self.worker.plan_multi_builders(
                wallet, needed_builders=2, custom_mode=True)
        self.assertEqual([item['candidate']['name'] for item in plan], ['Crafting Station'])
        self.assertEqual(needed, {'gold'})
        self.assertEqual(reason, '')

    def test_plan_scans_past_excluded_crafting_rows(self):
        self.worker.custom_regular_names.add('cannon')
        self.worker.village.settings['building_upgrade_whitelist'].update(
            {'craftingstation': False, 'cannon': True})
        self.worker.village.settings['building_upgrade_priority'] = 'time_min'
        self.worker.village.walls_confirmed_maxed = True
        self.worker.village.village_state = None
        self.worker.adb = SimpleNamespace(screencap=lambda: self.screen)
        self.worker.check = lambda: None
        self.worker.apply_menu_filter_if_needed = lambda: True
        self.worker.ui.menu_signature = lambda words: tuple(words)
        pages = [
            [('Filtered', 1380, 90), ('Crafting Station', 1300, 200),
             ('5 000 000', 1470, 223)],
            [('Crafting Station', 1300, 200), ('7 500 000', 1470, 223)],
            [('Cannon', 1300, 200), ('6 000 000', 1470, 223)],
        ]
        page = [0]
        self.worker.words = lambda *_: pages[page[0]]
        self.worker.ui.scroll_menu = Mock(side_effect=lambda *_args, **_kwargs:
                                          page.__setitem__(0, min(page[0] + 1, 2)))
        wallet = {'gold': 26_000_000, 'elixir': 26_000_000,
                  'valid': {'gold': True, 'elixir': True}}
        with patch.object(SuggestedUpgrader, 'currency', return_value='gold'):
            plan, _, _ = self.worker.plan_multi_builders(wallet, needed_builders=1,
                                                         custom_mode=True)
        self.assertEqual([item['candidate']['name'] for item in plan], ['Cannon'])


if __name__ == '__main__':
    unittest.main()
