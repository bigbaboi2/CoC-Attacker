import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from bot.suggested_upgrades import SuggestedUpgrader


class CraftingStationPurchaseTests(unittest.TestCase):
    def setUp(self):
        self.frame = np.zeros((900, 1600, 3), np.uint8)
        self.candidate = dict(name='Crafting Station', cost=5_000_000,
                              resource='gold', x=1300, y=520, duration_sec=8 * 3600)
        self.worker = SuggestedUpgrader.__new__(SuggestedUpgrader)
        self.worker.adb = SimpleNamespace(tap=Mock(), screencap=Mock(return_value=self.frame))
        self.worker.village = SimpleNamespace(current_account_key='#ACCOUNT',
                                             is_in_main_village=lambda _: True,
                                             village_state=None,
                                             record_started_builder_upgrade=Mock())
        self.worker.vision = SimpleNamespace(read_village_resources=Mock())
        self.worker.assignments = []
        self.worker.pending = None
        self.worker.unaffordable = {}
        self.worker.target_candidate = None
        self.worker.check = lambda: None
        self.worker.words = lambda *_: []
        self.worker.menu_candidates = Mock(return_value=[self.candidate])
        self.worker._seek_candidate_row = Mock(return_value=self.candidate)
        self.worker.confirmation = Mock(return_value=(1328, 216))
        self.worker.observe_home_builders = Mock()

    def purchase(self):
        plan = [dict(candidate=self.candidate, builder_idx=1, is_ready=True)]
        before = {'gold': 26_000_000, 'valid': {'gold': True}}
        with patch('bot.suggested_upgrades.time.sleep'):
            return self.worker.execute_builder_plan(
                plan, None, before=before, builders_before=3)

    def test_no_resource_or_builder_change_never_counts_as_upgrade(self):
        self.worker.vision.read_village_resources.return_value = {
            'gold': 26_000_000, 'valid': {'gold': True}}
        self.worker.observe_home_builders.return_value = 3
        result = self.purchase()
        self.assertEqual(result['status'], 'uncertain')
        self.assertEqual(self.worker.assignments, [])
        self.assertIsNotNone(self.worker.pending)
        self.assertEqual(self.worker.adb.tap.call_count, 2)
        self.worker._timed_step(None, maintain_reserve=False, custom_mode=True, preopened=None)
        self.assertEqual(self.worker.adb.tap.call_count, 2)

    def test_verified_deduction_and_builder_decrement_count_once(self):
        self.worker.vision.read_village_resources.return_value = {
            'gold': 21_000_000, 'valid': {'gold': True}}
        self.worker.observe_home_builders.return_value = 2
        result = self.purchase()
        self.assertEqual(result['status'], 'progress')
        self.assertIsNone(self.worker.pending)
        self.assertEqual(len(self.worker.assignments), 1)
        self.worker.village.record_started_builder_upgrade.assert_called_once()

    def test_row_must_be_visible_at_the_verified_price(self):
        self.worker._seek_candidate_row.return_value = None
        result = self.purchase()
        self.assertEqual(result['status'], 'blocked')
        self.worker.adb.tap.assert_not_called()

    def test_second_builder_uses_fresh_home_balance_after_closing_menu(self):
        second = dict(self.candidate, cost=7_500_000)
        plan = [dict(candidate=self.candidate, builder_idx=1, is_ready=True),
                dict(candidate=second, builder_idx=2, is_ready=True)]
        self.worker._execute_builder_candidate = Mock(return_value={'status': 'progress'})
        self.worker.village.is_in_main_village = Mock(side_effect=[False, True])
        self.worker.close_menu = Mock()
        self.worker.ui = SimpleNamespace(ensure_menu_open=Mock(return_value=True))
        self.worker.vision.read_village_resources.return_value = {
            'gold': 21_000_000, 'valid': {'gold': True}}
        self.worker.observe_home_builders.return_value = 2
        result = self.worker.execute_builder_plan(
            plan, None, before={'gold': 26_000_000, 'valid': {'gold': True}},
            builders_before=3)
        self.assertEqual(result['count'], 2)
        self.worker.close_menu.assert_called_once()
        self.assertEqual(self.worker._execute_builder_candidate.call_args_list[1].args[1:3],
                         ({'gold': 21_000_000, 'valid': {'gold': True}}, 2))


if __name__ == '__main__':
    unittest.main()
