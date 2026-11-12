import unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import Mock

import cv2
import numpy as np

from bot.new_buildings import NewBuildingBuyer
from bot.army_upgrades import ArmyUpgrader
from bot.suggested_upgrades import SuggestedUpgrader


class UpgradeTests(unittest.TestCase):
    def setUp(self):
        self.worker = SimpleNamespace(
            adb=Mock(), village=SimpleNamespace(adb=Mock(), settings={}),
            parse_duration=SuggestedUpgrader.parse_duration,
            currency=Mock(return_value='gold'))
        self.buyer = NewBuildingBuyer(self.worker)
        self.frame = np.zeros((900, 1600, 3), np.uint8)

    def words(self, duration='8s'):
        return [('Cannon', 1300, 200), (duration, 1500, 200),
                ('New', 1280, 224), ('150', 1490, 224)]

    def test_missing_new_does_not_scroll_or_change_filters(self):
        screen = self.frame
        adb = SimpleNamespace(screencap=lambda: screen, tap=Mock())
        wallet = {'gold': 1000, 'valid': {'gold': True}}
        village = SimpleNamespace(adb=adb, is_in_main_village=lambda _: True)
        ui = SimpleNamespace(ensure_menu_open=lambda: True, scroll_menu=Mock())
        worker = SimpleNamespace(adb=adb, village=village, ui=ui,
                                 vision=SimpleNamespace(read_village_resources=lambda _: wallet),
                                 words=lambda *args: [('Cannon', 1300, 200)],
                                 reset_menu_filter=Mock(), close_menu=Mock())
        buyer = NewBuildingBuyer(worker)
        self.assertEqual(buyer.step(lambda _: None, 1, 0)['status'], 'idle')
        worker.reset_menu_filter.assert_not_called()
        ui.scroll_menu.assert_not_called()
        adb.tap.assert_not_called()

    def test_new_label_triggers_one_scroll_even_if_first_entry_not_eligible(self):
        screen = self.frame
        adb = SimpleNamespace(screencap=lambda: screen, tap=Mock())
        village = SimpleNamespace(adb=adb, is_in_main_village=lambda _: True)
        ui = SimpleNamespace(ensure_menu_open=lambda: True, scroll_menu=Mock())
        wallet = {'gold': 1000, 'valid': {'gold': True}}
        worker = SimpleNamespace(adb=adb, village=village, ui=ui,
                                 vision=SimpleNamespace(read_village_resources=lambda _: wallet),
                                 words=lambda *args: [('New', 1280, 224)], close_menu=Mock())
        buyer = NewBuildingBuyer(worker)
        self.assertEqual(buyer.step(lambda _: None, 1, 0)['status'], 'idle')
        ui.scroll_menu.assert_called_once_with('down', fast=True)
        adb.tap.assert_not_called()

    def test_new_scan_can_handoff_open_menu_with_verified_wallet(self):
        screen = self.frame
        adb = SimpleNamespace(screencap=lambda: screen, tap=Mock())
        village = SimpleNamespace(adb=adb, is_in_main_village=lambda _: True)
        wallet = {'gold': 1000, 'elixir': 1000, 'dark_elixir': 100,
                  'valid': {'gold': True, 'elixir': True, 'dark_elixir': True}}
        worker = SimpleNamespace(adb=adb, village=village,
              ui=SimpleNamespace(ensure_menu_open=lambda: True, scroll_menu=Mock()),
              vision=SimpleNamespace(read_village_resources=lambda _: wallet),
              words=lambda *args: [('Cannon', 1300, 200)], close_menu=Mock())
        result = NewBuildingBuyer(worker).step(lambda _: None, 2, 0, keep_menu_open=True)
        self.assertEqual(result['status'], 'idle')
        self.assertIsNotNone(result['preopened'])
        worker.close_menu.assert_not_called()
        adb.tap.assert_not_called()

    def test_hud_lab_uses_direct_ocr_fallback(self):
        frame = self.frame
        ocr = Mock(return_value=SimpleNamespace(txts=['1/2']))
        worker = SimpleNamespace(adb=Mock(), village=SimpleNamespace(),
                ui=SimpleNamespace(texts=lambda *args: []), vision=SimpleNamespace(_get_ocr=lambda: ocr))
        self.assertEqual(ArmyUpgrader(worker).home_lab_status(frame), 'goblin')
        self.assertFalse(ocr.call_args.kwargs['use_det'])

    def test_no_free_builder_never_opens_menu(self):
        self.worker.ui = SimpleNamespace(ensure_menu_open=Mock())
        self.assertEqual(self.buyer.step(lambda _: None, 0, 0)['status'], 'idle')
        self.worker.ui.ensure_menu_open.assert_not_called()

    def test_reserve_requires_short_verified_build(self):
        self.assertEqual(len(self.buyer.rows(self.frame, self.words(), 1, 1)), 1)
        self.assertEqual(self.buyer.rows(self.frame, self.words('1m'), 1, 1), [])
        self.assertEqual(self.buyer.rows(self.frame, self.words('unknown'), 1, 1), [])
        self.assertEqual(len(self.buyer.rows(self.frame, self.words('1h'), 2, 1)), 1)

    def test_gems_and_missing_new_are_excluded(self):
        self.worker.currency.return_value = None
        self.assertEqual(self.buyer.rows(self.frame, self.words(), 2, 1), [])
        self.worker.currency.return_value = 'gold'
        self.assertEqual(self.buyer.rows(self.frame, self.words()[:-2] + [self.words()[-1]], 2, 1), [])

    def test_green_gem_rejected_even_when_gold_classifier_matches(self):
        self.frame[208:242, 1560:1598] = (0, 220, 0)
        self.assertEqual(self.buyer.rows(self.frame, self.words(), 2, 1), [])

    def test_research_claim_precedes_new_building(self):
        from bot.bot_engine import BotEngine
        fake = SimpleNamespace(
            _upgrade_resource_hold=False, village=self.worker.village,
            settings={'auto_upgrade_army': True, 'auto_buy_new_buildings': True},
            farm_finish=SimpleNamespace(suggested_upgrader=lambda: self.worker),
            _army_upgrader=SimpleNamespace(step=lambda: dict(status='needs_resources', resources=['elixir'], reason='wait')),
            battle=SimpleNamespace(), log=lambda message: None)
        result = BotEngine._service_optional_upgrades(fake, self.frame)
        self.assertEqual(result['status'], 'needs_resources')
        self.assertEqual(fake.battle.farm_needed_resources, {'elixir'})
        self.assertTrue(fake._upgrade_resource_hold)
        self.assertFalse(hasattr(fake, '_new_building_buyer'))

    def test_placement_requires_matching_pair(self):
        assets = Path(__file__).resolve().parents[1] / 'local_assets'
        yes = cv2.imread(str(assets / 'new_building_confirm.png'))
        no = cv2.imread(str(assets / 'new_building_cancel.png'))
        self.frame[200:237, 300:337] = yes
        self.assertIsNone(self.buyer.placement_buttons(self.frame))
        self.frame[200:237, 244:281] = no
        self.assertEqual(self.buyer.placement_buttons(self.frame), (318, 218))

    def test_army_rows_only_accept_research_resources(self):
        self.worker.currency.return_value = 'elixir'
        army = ArmyUpgrader(self.worker)
        self.assertEqual(army.rows(self.frame, [('Baby Dragon', 1300, 200), ('750000', 1480, 224)])[0]['cost'], 750000)
        self.worker.currency.return_value = 'gold'
        self.assertEqual(army.rows(self.frame, [('Baby Dragon', 1300, 200), ('750000', 1480, 224)]), [])

if __name__ == '__main__':
    unittest.main()
