import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from bot.suggested_upgrades import SuggestedUpgrader


class PendingBuilderReconcileTests(unittest.TestCase):
    def worker(self, readings, builders):
        worker = SuggestedUpgrader.__new__(SuggestedUpgrader)
        worker.pending = dict(name='Cannon', resource='gold', cost=500,
                              before=2000, builders_before=2, account='#MAIN')
        worker.assignments = []
        worker.target_candidate = {'name': 'Cannon'}
        worker.village = SimpleNamespace(current_account_key='#MAIN',
                                         is_in_main_village=Mock(return_value=True))
        worker.adb = SimpleNamespace(screencap=Mock(return_value=object()))
        worker.vision = SimpleNamespace(read_village_resources=Mock(side_effect=readings))
        worker.observe_home_builders = Mock(side_effect=builders)
        worker.publish_builders = Mock()
        return worker

    def test_two_home_observations_clear_verified_purchase(self):
        reading = {'valid': {'gold': True}, 'gold': 1500}
        worker = self.worker([reading, reading], [1, 1])
        result = worker.reconcile_pending()
        self.assertEqual(result['status'], 'progress')
        self.assertIsNone(worker.pending)
        self.assertEqual(len(worker.assignments), 1)

    def test_unverified_purchase_stays_pending_without_tap(self):
        worker = self.worker([{'valid': {'gold': True}, 'gold': 2000}], [2])
        result = worker.reconcile_pending()
        self.assertEqual(result['status'], 'uncertain')
        self.assertIsNotNone(worker.pending)
        worker.adb.screencap.assert_called_once()

    def test_timed_step_preserves_verified_count_when_later_builder_times_out(self):
        worker = SuggestedUpgrader.__new__(SuggestedUpgrader)
        worker.pending = None
        worker.opened_menu = False
        worker._step_verified = 1
        worker.last_status = ''
        worker._step = Mock(side_effect=TimeoutError('Hết thời gian kiểm tra nâng công trình'))
        result = worker._timed_step(None, maintain_reserve=True, custom_mode=True, preopened=None)
        self.assertEqual(result['status'], 'progress')
        self.assertEqual(result['count'], 1)


if __name__ == '__main__':
    unittest.main()
