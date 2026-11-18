import unittest
from types import SimpleNamespace

import numpy as np

from bot.suggested_upgrades import SuggestedUpgrader


class BuilderMenuResourceFilterTests(unittest.TestCase):
    def make_worker(self, enabled, active='elixir'):
        worker = SuggestedUpgrader.__new__(SuggestedUpgrader)
        points = worker.FIXED_FILTER_POINTS
        state = {'active': active, 'popup': True, 'taps': []}

        def tap(x, y, **_):
            state['taps'].append((x, y))
            if (x, y) == points['button']:
                state['popup'] = not state['popup']
            else:
                resource = next((key for key in ('gold', 'elixir', 'dark_elixir')
                                 if (x, y) == points[f'res_{key}']), None)
                if resource:
                    state['active'] = None if state['active'] == resource else resource

        frame = np.zeros((900, 1600, 3), dtype=np.uint8)
        worker.adb = SimpleNamespace(screencap=lambda: frame, tap=tap)
        worker.village = SimpleNamespace(settings={
            'auto_upgrade_buildings': True,
            'building_upgrade_use_menu_filter': True,
            'building_upgrade_resources': enabled,
            'building_upgrade_priority': 'time_min',
        })
        worker.check = lambda: None
        worker.is_filter_popup_open = lambda _: state['popup']
        worker.is_filter_button_active = lambda _, point, sorting=False: (
            False if sorting else state['active'] == next(
                key for key in ('gold', 'elixir', 'dark_elixir')
                if point == points[f'res_{key}'])
        )
        return worker, state

    def test_gold_and_elixir_clear_exclusive_game_filter(self):
        worker, state = self.make_worker({'gold': True, 'elixir': True,
                                          'dark_elixir': False})
        self.assertTrue(worker.apply_menu_filter_if_needed())
        self.assertIsNone(state['active'])
        self.assertNotIn(worker.FIXED_FILTER_POINTS['res_gold'], state['taps'])

    def test_single_gold_replaces_elixir(self):
        worker, state = self.make_worker({'gold': True, 'elixir': False,
                                          'dark_elixir': False})
        self.assertTrue(worker.apply_menu_filter_if_needed())
        self.assertEqual(state['active'], 'gold')

    def test_reset_clears_resource_filter(self):
        worker, state = self.make_worker({'gold': True, 'elixir': True,
                                          'dark_elixir': False}, active='dark_elixir')
        worker.reset_menu_filter()
        self.assertIsNone(state['active'])

    def test_supercharge_toggle_filters_blue_lightning_rows_and_dialog(self):
        frame = np.zeros((900, 1600, 3), dtype=np.uint8)
        # Cyan-blue lightning pixels below Supercharge row (y=210 -> y+6..y+40 = 216..250)
        frame[225:238, 1245:1255] = (255, 190, 30)  # BGR cyan-blue
        self.assertTrue(SuggestedUpgrader.is_supercharge_row(frame, 210))
        self.assertFalse(SuggestedUpgrader.is_supercharge_row(frame, 350))

        worker = SuggestedUpgrader.__new__(SuggestedUpgrader)
        worker.base_regular_names = {'firespitter', 'buildershut'}
        worker.custom_regular_names = {'firespitter', 'buildershut'}
        worker.village = SimpleNamespace(settings={
            'lock_townhall_upgrade': True,
            'building_upgrade_allow_supercharge': False,
        })
        self.assertFalse(worker.is_candidate_allowed({'name': 'Firespitter', 'supercharge': True}, custom_mode=True))
        self.assertTrue(worker.is_candidate_allowed({'name': "Builder's Hut", 'supercharge': False}, custom_mode=True))
        worker.village.settings['building_upgrade_allow_supercharge'] = True
        self.assertTrue(worker.is_candidate_allowed({'name': 'Firespitter', 'supercharge': True}, custom_mode=True))

        worker.village.settings['building_upgrade_allow_supercharge'] = False
        worker.village.settings['auto_upgrade_buildings'] = True
        worker.words = lambda _scr, _roi: [('Supercharge Firespitter to Charge 1?', 800, 50), ('Confirm', 950, 750)]
        self.assertIsNone(worker.confirmation(frame, {'name': 'Firespitter', 'cost': 8500000, 'resource': 'gold'}))


if __name__ == '__main__':
    unittest.main()
