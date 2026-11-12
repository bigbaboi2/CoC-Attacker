import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
import time
import cv2
import numpy as np
from PyQt5.QtWidgets import QApplication

from ui.widgets import LogConsole
from bot.village import VillageManager
from bot.farm_finish import ResourceCapacityReader
from bot.army_upgrades import ArmyUpgrader


class FixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_log_stays_on_old_lines_until_arrow_clicked(self):
        w = LogConsole()
        w.resize(400, 180)
        w.show()
        for n in range(100):
            w.append_log(str(n))
        self.app.processEvents()
        bar = w.text_edit.verticalScrollBar()
        bar.setValue(10)
        w.append_log('new')
        self.app.processEvents()
        self.assertEqual(bar.value(), 10)
        self.assertFalse(w.new_logs_btn.isHidden())
        w.scroll_to_latest()
        self.assertEqual(bar.value(), bar.maximum())
        self.assertTrue(w.new_logs_btn.isHidden())
        w.close()

    def test_notification_prompt_requires_both_labels(self):
        frame = np.full((900, 1600, 3), 230, np.uint8)
        boxes = [np.array([[700, 100], [800, 120]]), np.array([[500, 610], [600, 630]])]
        adb = Mock()
        ocr = Mock(return_value=SimpleNamespace(txts=['Notifications', 'Not now'], boxes=boxes))
        village = SimpleNamespace(adb=adb, vision=SimpleNamespace(_get_ocr=lambda: ocr))
        self.assertTrue(VillageManager.dismiss_notification_prompt(village, frame))
        adb.tap.assert_called_once_with(550, 620, jitter=0, delay_after=.5)
        ocr.return_value = SimpleNamespace(txts=['Notifications', 'Yes'], boxes=boxes)
        self.assertFalse(VillageManager.dismiss_notification_prompt(village, frame))

    def test_player_tag_requires_unambiguous_valid_tag(self):
        self.assertEqual(VillageManager._player_tag_from_texts(['My Profile', '#P0Y2P0Y2P']), '#P0Y2P0Y2P')
        self.assertIsNone(VillageManager._player_tag_from_texts(['#P0Y2P0Y2P', '#P0Y2']))
        self.assertIsNone(VillageManager._player_tag_from_texts(['#P0Y2P0Y2PX']))
        self.assertEqual(VillageManager._player_tag_from_texts(['#LLLVVOGP8']), '#LLLVV0GP8')

    def test_capacity_cache_isolated_by_tag(self):
        accounts = {'#P0Y2': {'gold': 100, 'source': 'popup_ocr', 'updated_at': time.time()},
                    '#QJU2': {'gold': 200, 'source': 'popup_ocr', 'updated_at': time.time()}}
        village = SimpleNamespace(settings={'device_serial': 'same'}, account_capacities=accounts,
                                  adb=SimpleNamespace(screencap=lambda: object()), vision=Mock())
        village.resolve_player_tag = Mock(side_effect=['#P0Y2', '#QJU2'])
        village.get_account_capacities = lambda resources, acc_key: {r: accounts[acc_key][r] for r in resources}
        village.get_account_th_level = lambda key: None
        reader = ResourceCapacityReader(village)
        self.assertEqual(reader.read(('gold',), lambda x: None), {'gold': 100})
        self.assertEqual(reader.read(('gold',), lambda x: None), {'gold': 200})
        self.assertIn(('same', '#P0Y2', 'gold'), reader.cache)
        self.assertIn(('same', '#QJU2', 'gold'), reader.cache)

    def test_capacity_refuses_identity_failure(self):
        village = SimpleNamespace(settings={'device_serial': 'same'}, account_capacities={},
                                  adb=SimpleNamespace(screencap=lambda: object()), vision=Mock(),
                                  resolve_player_tag=lambda screen, log: None)
        self.assertIsNone(ResourceCapacityReader(village).read(('gold',), lambda x: None))

    def test_research_slot_handles_regular_and_goblin_counters(self):
        frame = np.zeros((900, 1600, 3), np.uint8)
        self.assertEqual(ArmyUpgrader.research_slot(frame, '1/2'), 'goblin')
        self.assertEqual(ArmyUpgrader.research_slot(frame, '0/2'), 'goblin')
        self.assertEqual(ArmyUpgrader.research_slot(frame, '1/1'), 'available')
        self.assertEqual(ArmyUpgrader.research_slot(frame, '0/1'), 'busy')
        self.assertIsNone(ArmyUpgrader.research_slot(frame, '2/2'))

    def test_verified_tag_reused_for_entire_session(self):
        screen = np.zeros((900, 1600, 3), np.uint8)
        vision = SimpleNamespace(read_player_profile_header=Mock(), _get_ocr=Mock())
        village = SimpleNamespace(is_in_main_village=lambda _: True, vision=vision,
                                  _session_player_tag='#P0Y2P0Y2P', player_tag_failure=None, player_tag_failures=0)
        self.assertEqual(VillageManager.resolve_player_tag(village, screen), '#P0Y2P0Y2P')
        vision.read_player_profile_header.assert_not_called()
        vision._get_ocr.assert_not_called()

    def test_goblin_slot_is_idle_and_does_not_open_filter(self):
        screen = np.zeros((900, 1600, 3), np.uint8)
        adb = SimpleNamespace(tap=Mock(), screencap=lambda: screen)
        village = SimpleNamespace(settings={}, is_in_main_village=lambda _: True)
        ui = SimpleNamespace(ensure_menu_open=lambda: True,
                             texts=lambda frame, roi: ['Army Upgrades'] if roi[1] == 0 else ['1/2'])
        vision = SimpleNamespace(read_village_resources=lambda _: {})
        worker = SimpleNamespace(adb=adb, village=village, ui=ui, vision=vision, close_menu=Mock())
        army = ArmyUpgrader(worker)
        army.configure_filter = Mock()
        result = army.step()
        self.assertEqual(result['status'], 'idle')
        self.assertIn('yêu tinh', result['reason'])
        army.configure_filter.assert_not_called()
        self.assertEqual(adb.tap.call_count, 1)

    def test_lab_template_fallback_when_ocr_is_empty(self):
        from pathlib import Path
        frame = np.zeros((900, 1600, 3), np.uint8)
        assets = Path(__file__).resolve().parents[1] / 'local_assets'
        worker = SimpleNamespace(adb=Mock(), village=SimpleNamespace(),
                                 ui=SimpleNamespace(texts=lambda *args: []))
        army = ArmyUpgrader(worker)
        for filename, expected in (('lab_slot_available.png', 'available'), ('lab_slot_goblin.png', 'goblin')):
            frame[:] = 0
            template = cv2.imread(str(assets / filename))
            frame[854:854 + template.shape[0], 1316:1316 + template.shape[1]] = template
            self.assertEqual(army.read_research_slot(frame)[0], expected)
        frame[:] = 0
        self.assertIsNone(army.read_research_slot(frame)[0])

    def test_profile_tab_selected_before_accepting_tag(self):
        frame = np.zeros((900, 1600, 3), np.uint8)
        box = np.array([[180, 30], [280, 70]])
        results = [SimpleNamespace(txts=['My Profile', 'Settings', '#P0Y2'], boxes=[box, box, box]),
                   SimpleNamespace(txts=['My Profile', 'Home Village'], boxes=[box, box]),
                   SimpleNamespace(txts=['#P0Y2P0Y2P']),
                   SimpleNamespace(txts=['My Profile', 'Home Village'], boxes=[box, box]),
                   SimpleNamespace(txts=['#P0Y2P0Y2P'])]
        ocr = Mock(side_effect=results)
        adb = SimpleNamespace(tap=Mock(), screencap=lambda: frame)
        village = VillageManager.__new__(VillageManager)
        village.adb = adb
        village.vision = SimpleNamespace(read_player_profile_header=lambda _: {'account_key': 'Cu_37'},
                                        _get_ocr=lambda: ocr, find_close_button=lambda _: None)
        village.is_in_main_village = lambda _: True
        village._session_player_tag = None
        village.current_account_key = None
        village.verified_maxed_accounts = {}
        village.player_tag_failures = 0
        self.assertEqual(village.resolve_player_tag(frame), '#P0Y2P0Y2P')
        self.assertEqual(adb.tap.call_args_list[1].args, (230, 50))
        self.assertEqual(ocr.call_count, 5)

    def test_player_id_failure_pauses_without_restart(self):
        from bot.bot_engine import BotEngine
        village = SimpleNamespace(player_tag_failures=3, player_tag_failure='OCR không đọc chắc mã người chơi')
        controller = SimpleNamespace(observe_home=lambda *args: 'wait')
        fake = SimpleNamespace(settings={'farm_finish_enabled': True}, farm_finish=controller,
              village=village, set_state=Mock(), log=Mock(), _request_recovery=Mock(), pause_bot=Mock())
        self.assertTrue(BotEngine._farm_finish_gate(fake, object()))
        fake._request_recovery.assert_not_called()
        fake.pause_bot.assert_called_once()

    def test_tag_ocr_confusions_and_clipboard_link(self):
        self.assertEqual(VillageManager._player_tag_from_texts(['#1iLOP8']), '#LLL0P8')
        self.assertEqual(VillageManager._clipboard_player_tag('https://link.clashofclans.com/en?action=OpenPlayerProfile&tag=%23P0Y2P0Y2P'), '#P0Y2P0Y2P')
        self.assertIsNone(VillageManager._clipboard_player_tag('https://example.com/?tag=%23P0Y2P0Y2P'))
        self.assertIsNone(VillageManager._clipboard_player_tag('#P0Y2P0Y2P1'))

    def test_copy_fallback_accepts_only_changed_clipboard(self):
        from pathlib import Path
        frame = np.zeros((900, 1600, 3), np.uint8)
        icon = cv2.imread(str(Path(__file__).resolve().parents[1] / 'local_assets' / 'profile_share_icon.png'))
        frame[250:250+icon.shape[0], 600:600+icon.shape[1]] = icon
        box = np.array([[680, 250], [730, 280]])
        ocr = lambda frame, **kwargs: SimpleNamespace(txts=['Copy', 'Share'], boxes=[box, box])
        village = VillageManager.__new__(VillageManager)
        village.adb = SimpleNamespace(tap=Mock(), screencap=lambda: frame)
        village._windows_clipboard_text = Mock(side_effect=['old', '#P0Y2P0Y2P'])
        self.assertEqual(village._copy_profile_tag(ocr), '#P0Y2P0Y2P')
        village._windows_clipboard_text = Mock(return_value='#P0Y2P0Y2P')
        self.assertIsNone(village._copy_profile_tag(ocr))

    def test_empty_new_scan_is_not_repeated_for_same_account(self):
        from bot.bot_engine import BotEngine
        buyer = SimpleNamespace(step=Mock(return_value=dict(status='idle', no_new=True, reason='none')))
        worker = SimpleNamespace(observe_home_builders=Mock(return_value=2))
        village = SimpleNamespace(current_account_key='#P0Y2', walls_confirmed_maxed=False,
                                  is_in_main_village=lambda _: True)
        fake = SimpleNamespace(settings={'auto_buy_new_buildings': True, 'reserve_builders': 0},
              village=village, farm_finish=SimpleNamespace(suggested_upgrader=lambda: worker),
              village_state=SimpleNamespace(update_resources=Mock()), _new_building_buyer=buyer, vision=SimpleNamespace(read_village_resources=lambda _: {'valid': {}}),
              battle=SimpleNamespace(), adb=SimpleNamespace(screencap=lambda: object()), log=lambda _: None)
        self.assertEqual(BotEngine._service_optional_upgrades(fake, object())['status'], 'idle')
        self.assertEqual(BotEngine._service_optional_upgrades(fake, object())['status'], 'idle')
        self.assertEqual(buyer.step.call_count, 1)
        fake.village_state.update_resources.assert_called_once_with('main', {'valid': {}})
        village.current_account_key = '#QJU2'
        self.assertEqual(BotEngine._service_optional_upgrades(fake, object())['status'], 'idle')
        self.assertEqual(buyer.step.call_count, 2)

    def test_pending_builder_defers_battle(self):
        from bot.bot_engine import BotEngine
        from bot.farm_finish import FarmFinishController
        worker = SimpleNamespace(observe_home_builders=lambda _: 2, pending={'name': 'Cannon'})
        controller = FarmFinishController.__new__(FarmFinishController)
        controller.builder = worker
        fake = SimpleNamespace(farm_finish=controller, village=SimpleNamespace(walls_confirmed_maxed=False,
                               current_account_key='#P0Y2'), settings={'auto_upgrade_buildings': True,
                               'reserve_builders': 0}, log=Mock())
        BotEngine._maintain_free_builders(fake, object())
        self.assertTrue(fake._defer_battle_after_maintenance)
        self.assertIn('chưa được xác minh', fake.log.call_args.args[0])

    def test_reconnect_uses_only_selected_port_and_retries(self):
        from bot.bot_engine import BotEngine
        import threading
        connect = Mock(side_effect=[False, True])
        cancelled = threading.Event()
        cancelled.wait = Mock(return_value=False)
        fake = SimpleNamespace(settings={'auto_reconnect': True}, _start_cancelled=cancelled,
                               adb=SimpleNamespace(connect_device=connect), log=Mock())
        self.assertTrue(BotEngine._connect_mumu_with_retries(fake, '127.0.0.1:16384', startup=True))
        self.assertEqual([c.args[0] for c in connect.call_args_list], ['127.0.0.1:16384'] * 2)

    def test_adb_reconnect_clears_disconnected_flag(self):
        from core.adb_client import ADBSocketClient
        client = ADBSocketClient(adb_bin='adb')
        client.device_unavailable = True
        client.connect_remote = Mock(return_value=True)
        client.shell = Mock(return_value='Physical size: 1600x900')
        self.assertTrue(client.connect_device('127.0.0.1:16384', start_server=False))
        self.assertFalse(client.device_unavailable)

    def test_reconnect_retries_if_device_drops_during_probe(self):
        from bot.bot_engine import BotEngine
        from core.adb_client import BotStoppedError
        import threading
        adb = SimpleNamespace(device_unavailable=False)
        def connect(serial, **kwargs):
            if adb.connect_device.call_count == 1:
                adb.device_unavailable = True
                raise BotStoppedError()
            adb.device_unavailable = False
            return True
        adb.connect_device = Mock(side_effect=connect)
        fake = SimpleNamespace(settings={'auto_reconnect': True}, _start_cancelled=threading.Event(),
                               adb=adb, log=Mock(), _is_current_worker=lambda: True, stop_requested=False)
        with patch('bot.bot_engine.time.sleep'):
            self.assertTrue(BotEngine._connect_mumu_with_retries(fake, '127.0.0.1:16384'))
        self.assertEqual(adb.connect_device.call_count, 2)

    def test_reconnect_stops_without_retry_after_user_stop(self):
        from bot.bot_engine import BotEngine
        from core.adb_client import BotStoppedError
        import threading
        fake = SimpleNamespace(settings={'auto_reconnect': True}, _start_cancelled=threading.Event(),
                               adb=SimpleNamespace(device_unavailable=True), log=Mock(),
                               _is_current_worker=lambda: True, stop_requested=False)
        def stop_during_probe(*args, **kwargs):
            fake.stop_requested = True
            raise BotStoppedError()
        fake.adb.connect_device = Mock(side_effect=stop_during_probe)
        self.assertFalse(BotEngine._connect_mumu_with_retries(fake, '127.0.0.1:16384'))
        self.assertEqual(fake.adb.connect_device.call_count, 1)

    def test_busy_lab_is_not_reopened_during_next_actions(self):
        from bot.bot_engine import BotEngine
        army = SimpleNamespace(step=Mock(return_value=dict(status='idle', lab_busy=True, reason='busy')))
        village = SimpleNamespace(current_account_key='#P0Y2', is_in_main_village=lambda _: True)
        fake = SimpleNamespace(settings={'auto_upgrade_army': True}, village=village,
              farm_finish=SimpleNamespace(suggested_upgrader=lambda: object()), _army_upgrader=army,
              battle=SimpleNamespace(), adb=SimpleNamespace(screencap=lambda: object()), log=lambda _: None)
        self.assertEqual(BotEngine._service_optional_upgrades(fake, object())['status'], 'idle')
        self.assertEqual(BotEngine._service_optional_upgrades(fake, object())['status'], 'idle')
        self.assertEqual(army.step.call_count, 1)

    def test_pending_army_closes_menu_and_verifies_without_second_purchase(self):
        screen = np.zeros((900, 1600, 3), np.uint8)
        adb = SimpleNamespace(tap=Mock(), screencap=lambda: screen)
        is_open = Mock(side_effect=[True, False])
        village = SimpleNamespace(settings={}, current_account_key='#P0Y2',
                                  is_in_main_village=lambda _: True)
        worker = SimpleNamespace(adb=adb, village=village,
             ui=SimpleNamespace(is_menu_open=is_open), close_menu=Mock(),
             vision=SimpleNamespace(read_village_resources=lambda _: {'elixir': 750, 'valid': {'elixir': True}}))
        army = ArmyUpgrader(worker)
        army.pending = dict(name='Dragon', resource='elixir', cost=250, before=1000, account='#P0Y2')
        result = army.reconcile_pending()
        self.assertEqual(result['status'], 'progress')
        self.assertIsNone(army.pending)
        worker.close_menu.assert_called_once()
        adb.tap.assert_not_called()

    def test_pending_army_never_retries_after_account_change(self):
        village = SimpleNamespace(settings={}, current_account_key='#QJU2')
        worker = SimpleNamespace(adb=SimpleNamespace(tap=Mock()), village=village)
        army = ArmyUpgrader(worker)
        army.pending = dict(name='Dragon', resource='elixir', cost=250, before=1000, account='#P0Y2')
        self.assertEqual(army.reconcile_pending()['status'], 'uncertain')
        worker.adb.tap.assert_not_called()

    def test_all_army_filters_use_default_unfiltered_state(self):
        frame = np.zeros((900, 1600, 3), np.uint8)
        adb = SimpleNamespace(tap=Mock(), screencap=lambda: frame)
        state = [True, False]
        worker = SimpleNamespace(adb=adb, village=SimpleNamespace(settings={}),
              is_filter_popup_open=lambda screen: state.pop(0),
              words=lambda screen, roi: [('Troops Spells Siege', 700, 350)],
              is_filter_button_active=lambda screen, pt, sorting=False: False)
        self.assertTrue(ArmyUpgrader(worker).configure_filter())
        self.assertEqual(adb.tap.call_count, 2)

if __name__ == '__main__':
    unittest.main()
