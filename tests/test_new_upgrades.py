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

    def test_missing_new_checks_one_lower_page_without_changing_filters(self):
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
        result = buyer.step(lambda _: None, 1, 0)
        self.assertEqual(result['status'], 'idle')
        self.assertTrue(result['no_eligible'])
        self.assertTrue(result['no_new'])
        worker.reset_menu_filter.assert_not_called()
        ui.scroll_menu.assert_called_once_with('down', fast=True)
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
        result = buyer.step(lambda _: None, 1, 0)
        self.assertEqual(result['status'], 'idle')
        self.assertTrue(result['no_eligible'])
        self.assertFalse(result['no_new'])
        ui.scroll_menu.assert_called_once_with('down', fast=True)
        adb.tap.assert_not_called()

    def test_scan_finds_lower_new_without_scrolling_away_from_it(self):
        ui = SimpleNamespace(scroll_menu=Mock(), menu_signature=lambda words: tuple(words))
        self.buyer.worker.ui = ui
        row = dict(name='Cannon', cost=150, resource='gold', y=220)
        self.buyer.visible_new = Mock(side_effect=[
            (self.frame, [('Other', 1300, 200)], []),
            (self.frame, [('New', 1280, 245)], [row])])
        self.assertEqual(self.buyer.seek_new(1, 0), (row, True))
        ui.scroll_menu.assert_called_once_with('down', fast=True)

    def test_scan_skips_unaffordable_new_and_selects_affordable_one(self):
        ui = SimpleNamespace(scroll_menu=Mock(), menu_signature=lambda words: tuple(words))
        self.buyer.worker.ui = ui
        costly = dict(name='Workshop', cost=1200000, resource='elixir', y=477)
        affordable = dict(name='Inferno Tower', cost=500000, resource='gold', y=590)
        self.buyer.visible_new = Mock(return_value=(self.frame, [('New', 1261, 503)],
                                                   [costly, affordable]))
        wallet = {'gold': 892056, 'elixir': 1115955}
        self.assertEqual(self.buyer.seek_new(1, 0, wallet=wallet), (affordable, True))
        ui.scroll_menu.assert_not_called()

    def test_scan_continues_until_new_entries_end(self):
        ui = SimpleNamespace(scroll_menu=Mock(), menu_signature=lambda words: tuple(words))
        self.buyer.worker.ui = ui
        self.buyer.visible_new = Mock(side_effect=[
            (self.frame, [('New', 1280, 245)], []),
            (self.frame, [('New', 1280, 345)], []),
            (self.frame, [('Other', 1280, 345)], []),
            (self.frame, [('Other again', 1280, 345)], [])])
        self.assertEqual(self.buyer.seek_new(1, 0), (None, True))
        self.assertEqual(ui.scroll_menu.call_count, 3)

    def test_transient_ocr_miss_does_not_discard_visible_row(self):
        row = dict(name='Cannon', cost=150, resource='gold', y=220)
        moved = dict(row, name=' cannon ', y=226)
        self.buyer.visible_new = Mock(side_effect=[
            (self.frame, [], []), (self.frame, [], [moved])])
        self.assertEqual(self.buyer.reread_row(row, 1, 0), moved)

    def test_map_pan_starts_in_detected_grass(self):
        self.frame[:] = (0, 120, 0)
        self.buyer.adb.screencap.return_value = self.frame
        self.buyer.adb.zoom_out_map = Mock()
        self.buyer.adb.swipe = Mock()
        self.assertTrue(self.buyer.move_empty_ground_to_center())
        x, y, _, _, = self.buyer.adb.swipe.call_args.args[:4]
        self.assertGreaterEqual(x, 280)
        self.assertLessEqual(x, 1150)
        self.assertGreaterEqual(y, 260)
        self.assertLessEqual(y, 640)

    def test_grey_check_is_cancelled_then_placement_retried(self):
        row = dict(name='Cannon', cost=150, resource='gold', y=220)
        self.buyer.worker.ui = SimpleNamespace(ensure_menu_open=Mock(return_value=True))
        self.buyer.visible_new = Mock(return_value=(self.frame, [], [row]))
        self.buyer.placement_buttons = Mock(side_effect=[None, (500, 200), (500, 200)])
        self.buyer.cancel_button = Mock(return_value=(300, 200))
        self.buyer.move_empty_ground_to_center = Mock(return_value=True)
        self.buyer.seek_new = Mock(return_value=(row, True))
        self.buyer.adb.tap = Mock()
        self.buyer.adb.screencap = Mock(return_value=self.frame)
        self.assertEqual(self.buyer.select_for_placement(row, 1, 0, lambda _: None), (500, 200))
        self.buyer.adb.tap.assert_any_call(300, 200, jitter=0, delay_after=.4)
        self.buyer.move_empty_ground_to_center.assert_called_once()

    def test_grey_check_stops_after_three_map_moves(self):
        row = dict(name='Cannon', cost=150, resource='gold', y=220)
        self.buyer.worker.ui = SimpleNamespace(ensure_menu_open=Mock(return_value=True))
        self.buyer.reread_row = Mock(return_value=row)
        self.buyer.placement_buttons = Mock(return_value=None)
        self.buyer.cancel_button = Mock(return_value=(300, 200))
        self.buyer.move_empty_ground_to_center = Mock(return_value=True)
        self.buyer.seek_new = Mock(return_value=(row, True))
        self.buyer.adb.screencap = Mock(return_value=self.frame)
        self.assertIsNone(self.buyer.select_for_placement(row, 1, 0, lambda _: None))
        self.assertEqual(self.buyer.move_empty_ground_to_center.call_count, 3)
        self.assertEqual(self.buyer.adb.tap.call_count, 8)

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
        self.frame[188:212, 1565:1592] = (220, 220, 220)
        self.assertEqual(self.buyer.rows(self.frame, self.words('unknown'), 1, 1), [])
        self.assertEqual(len(self.buyer.rows(self.frame, self.words('1h'), 2, 1)), 1)

    def test_no_clock_is_instant_with_only_reserve_builder_free(self):
        words = [('Giant Bomb', 1300, 200), ('New', 1280, 224), ('6250', 1490, 224)]
        rows = self.buyer.rows(self.frame, words, 1, 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['duration_sec'], 0)

    def test_unread_clock_is_not_treated_as_instant(self):
        words = [('Giant Bomb', 1300, 200), ('New', 1280, 224), ('6250', 1490, 224)]
        self.frame[188:212, 1565:1592] = (220, 220, 220)
        self.assertEqual(self.buyer.rows(self.frame, words, 1, 1), [])

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

    def test_only_new_work_pauses_after_no_eligible_entry(self):
        from bot.bot_engine import BotEngine
        wallet = {'gold': 800000, 'elixir': 1000000, 'dark_elixir': 35000,
                  'valid': {'gold': True, 'elixir': True, 'dark_elixir': True}}
        buyer = SimpleNamespace(step=Mock(return_value=dict(
            status='idle', no_eligible=True, no_new=False, reason='none')))
        worker = SimpleNamespace(observe_home_builders=Mock(return_value=1), pending=None)
        fake = SimpleNamespace(settings={'auto_buy_new_buildings': True, 'reserve_builders': 1},
            village=SimpleNamespace(current_account_key='#QJU', walls_confirmed_maxed=False,
                                    is_in_main_village=lambda _: True),
            farm_finish=SimpleNamespace(suggested_upgrader=lambda: worker),
            village_state=SimpleNamespace(update_resources=Mock()), _new_building_buyer=buyer,
            vision=SimpleNamespace(read_village_resources=lambda _: wallet),
            battle=SimpleNamespace(), adb=SimpleNamespace(screencap=lambda: object()),
            log=Mock(), pause_bot=Mock())
        self.assertEqual(BotEngine._service_optional_upgrades(fake, object())['status'], 'paused')
        fake.pause_bot.assert_called_once_with()

    def test_other_work_continues_and_new_scan_retries_after_resources_change(self):
        from bot.bot_engine import BotEngine
        wallet = {'gold': 800000, 'elixir': 1000000, 'dark_elixir': 35000,
                  'valid': {'gold': True, 'elixir': True, 'dark_elixir': True}}
        buyer = SimpleNamespace(step=Mock(return_value=dict(
            status='idle', no_eligible=True, no_new=False, reason='none')))
        worker = SimpleNamespace(observe_home_builders=Mock(return_value=1), pending=None)
        fake = SimpleNamespace(settings={'auto_buy_new_buildings': True, 'auto_upgrade_walls': True,
                                         'reserve_builders': 1},
            village=SimpleNamespace(current_account_key='#QJU', walls_confirmed_maxed=False,
                                    is_in_main_village=lambda _: True),
            farm_finish=SimpleNamespace(suggested_upgrader=lambda: worker),
            village_state=SimpleNamespace(update_resources=Mock()), _new_building_buyer=buyer,
            vision=SimpleNamespace(read_village_resources=lambda _: wallet),
            battle=SimpleNamespace(), adb=SimpleNamespace(screencap=lambda: object()),
            log=Mock(), pause_bot=Mock())
        self.assertEqual(BotEngine._service_optional_upgrades(fake, object())['status'], 'idle')
        self.assertEqual(BotEngine._service_optional_upgrades(fake, object())['status'], 'idle')
        self.assertEqual(buyer.step.call_count, 1)
        self.assertGreater(fake.last_village_maintenance, 0)
        fake.pause_bot.assert_not_called()
        wallet['gold'] += 10000
        self.assertEqual(BotEngine._service_optional_upgrades(fake, object())['status'], 'idle')
        self.assertEqual(buyer.step.call_count, 2)

    def test_placement_requires_matching_pair(self):
        assets = Path(__file__).resolve().parents[1] / 'local_assets'
        yes = cv2.imread(str(assets / 'new_building_confirm.png'))
        no = cv2.imread(str(assets / 'new_building_cancel.png'))
        self.frame[200:237, 300:337] = yes
        self.assertIsNone(self.buyer.placement_buttons(self.frame))
        self.frame[200:237, 244:281] = no
        self.assertEqual(self.buyer.placement_buttons(self.frame), (318, 218))

    def test_grey_confirm_cannot_be_purchased_but_cancel_is_found(self):
        assets = Path(__file__).resolve().parents[1] / 'local_assets'
        yes = cv2.imread(str(assets / 'new_building_confirm.png'))
        no = cv2.imread(str(assets / 'new_building_cancel.png'))
        grey = cv2.cvtColor(cv2.cvtColor(yes, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
        self.frame[200:237, 300:337] = grey
        self.frame[200:237, 244:281] = no
        self.assertIsNone(self.buyer.placement_buttons(self.frame))
        self.assertEqual(self.buyer.cancel_button(self.frame), (262, 218))

    def test_current_game_buttons_are_detected_as_a_pair(self):
        assets = Path(__file__).resolve().parents[1] / 'local_assets'
        yes = cv2.imread(str(assets / 'new_building_confirm_live.png'))
        no = cv2.imread(str(assets / 'new_building_cancel_live.png'))
        self.frame[381:414, 800:835] = no
        self.frame[381:414, 846:881] = yes
        self.assertEqual(self.buyer.placement_buttons(self.frame), (863, 397))
        self.assertEqual(self.buyer.cancel_button(self.frame), (817, 397))

    def test_army_rows_only_accept_research_resources(self):
        self.worker.currency.return_value = 'elixir'
        army = ArmyUpgrader(self.worker)
        self.assertEqual(army.rows(self.frame, [('Baby Dragon', 1300, 200), ('750000', 1480, 224)])[0]['cost'], 750000)
        self.worker.currency.return_value = 'gold'
        self.assertEqual(army.rows(self.frame, [('Baby Dragon', 1300, 200), ('750000', 1480, 224)]), [])

if __name__ == '__main__':
    unittest.main()
