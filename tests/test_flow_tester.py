import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

# Ensure onnxruntime is loaded before PyQt5 if GUI test runs in the same process.
try:
    import onnxruntime  # noqa: F401
except Exception:
    pass

from PyQt5.QtWidgets import QApplication

from bot.flow_tester import FlowTestRunner
from config.settings import BotSettings, DEFAULT_BUILDING_WHITELIST
from ui.test_tool_window import TestToolWindow


class _FakeADB:
    def __init__(self, frame_fn=None):
        self.device_serial = "127.0.0.1:5555"
        self.device_unavailable = False
        self.control_callback = None
        self.taps = []
        self.zooms = 0
        self._frame_fn = frame_fn or (lambda taps: np.zeros((900, 1600, 3), dtype=np.uint8))

    def _guard_input(self):
        if self.control_callback:
            self.control_callback()

    def screencap(self):
        self._guard_input()
        return self._frame_fn(self.taps)

    def tap(self, x, y, jitter=0, delay_after=0.0):
        self._guard_input()
        self.taps.append((int(x), int(y)))

    def swipe(self, x1, y1, x2, y2, duration_ms=300, jitter=0):
        self._guard_input()

    def zoom_out_map(self, times=2):
        self._guard_input()
        self.zooms += times

    def list_devices(self):
        return ["127.0.0.1:5555"]

    def connect_device(self, serial, start_server=True):
        self.device_serial = serial
        return True


class _FakeVision:
    asset_dir = Path(__file__).resolve().parents[1] / "assets"

    def __init__(self):
        self.resources = {
            "gold": 4_500_000,
            "elixir": 4_000_000,
            "dark_elixir": 80_000,
            "valid": {"gold": True, "elixir": True, "dark_elixir": True},
        }
        self.capacities = {"gold": 5_000_000, "elixir": 5_000_000, "dark_elixir": 100_000}
        self.treasury = {"gold": 300_000, "elixir": 250_000, "dark_elixir": 1_500}
        self._last_hud = None

    def is_in_main_village(self, screen):
        return True

    def has_open_dialog(self, screen):
        return False

    def find_close_button(self, screen):
        return None

    def read_village_resources(self, screen):
        return {
            "gold": self.resources["gold"],
            "elixir": self.resources["elixir"],
            "dark_elixir": self.resources["dark_elixir"],
            "valid": dict(self.resources["valid"]),
        }

    def read_single_resource(self, screen, resource):
        return self.read_village_resources(screen)

    def read_player_profile_header(self, screen):
        return {"account_key": "TesterTH12"}

    def read_resource_popup(self, frame):
        if self._last_hud is None:
            return None
        return {
            "capacity": self.capacities[self._last_hud],
            "treasury": self.treasury[self._last_hud],
        }

    def match_template(self, *args, **kwargs):
        return None

    def load_template(self, *args, **kwargs):
        return None

    def _get_ocr(self):
        return None

    def battle_phase(self, screen):
        return "home"

    def is_attack_menu_open(self, screen):
        return False, (275, 650)

    def is_enemy_base(self, screen):
        return False

    def read_loot(self, screen, dimmed=False):
        return {
            "gold": 800_000,
            "elixir": 750_000,
            "dark_elixir": 6_000,
            "known": {"gold": True, "elixir": True, "dark_elixir": True},
            "valid": True,
            "valid_dark": True,
        }


class FlowTesterTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.cfg_path = str(Path(self.tmpdir.name) / "test_cfg.json")
        self.settings = BotSettings(filepath=self.cfg_path)
        self.logs = []

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_collect_resources_scan_only_never_taps(self):
        adb = _FakeADB()
        vision = _FakeVision()
        runner = FlowTestRunner(adb, vision, self.settings, log_cb=self.logs.append)
        bubbles = [{"resource": "gold", "x": 400, "y": 350, "score": 0.91}]
        full_bubbles = [{"resource": "elixir", "x": 600, "y": 420, "score": 0.85, "full": True}]

        with patch("bot.village.VillageManager.find_collectors", return_value=(bubbles, full_bubbles)):
            res = runner.run_flow("collect_resources", mode="scan_only", options={"auto_zoom": False})

        self.assertEqual(res.status, "PASS")
        self.assertEqual(adb.taps, [])
        self.assertEqual(res.details["ready"]["gold"], 1)
        self.assertEqual(res.details["full_red"]["elixir"], 1)

    def test_collect_resources_real_runs_from_start_to_finish(self):
        adb = _FakeADB()
        vision = _FakeVision()
        runner = FlowTestRunner(adb, vision, self.settings, log_cb=self.logs.append)

        with patch("bot.village.VillageManager.collect_resources", return_value=3) as mock_collect, \
             patch("bot.village.VillageManager.find_collectors", side_effect=[
                 ([{"resource": "gold", "x": 300, "y": 300, "score": 0.9}], []),
                 ([], []),
             ]):
            res = runner.run_flow("collect_resources", mode="real")

        mock_collect.assert_called_once()
        self.assertEqual(res.status, "PASS")
        self.assertEqual(res.details["verified_count"], 3)

    def test_check_max_storage_reads_popup_and_computes_percentage(self):
        adb = _FakeADB()
        vision = _FakeVision()

        original_tap = adb.tap

        def tap_with_hud(x, y, jitter=0, delay_after=0.0):
            original_tap(x, y, jitter=jitter, delay_after=delay_after)
            if (x, y) == (1430, 45):
                vision._last_hud = "gold"
            elif (x, y) == (1430, 105):
                vision._last_hud = "elixir"
            elif (x, y) == (1430, 165):
                vision._last_hud = "dark_elixir"
            elif (x, y) == (1000, 250):
                vision._last_hud = None

        adb.tap = tap_with_hud
        runner = FlowTestRunner(adb, vision, self.settings, log_cb=self.logs.append)

        res = runner.run_flow(
            "check_max_storage",
            mode="real",
            options={
                "farm_finish_resources": "gold_elixir",
                "farm_finish_target_percent": 90,
                "bypass_cache": True,
                "verify_account": False,
                "verify_twice": True,
            },
        )

        self.assertEqual(res.status, "PASS")
        self.assertEqual(res.details["capacities"]["gold"], 5_000_000)
        self.assertEqual(res.details["percentages"]["gold"], 90.0)
        self.assertEqual(res.details["percentages"]["elixir"], 80.0)
        self.assertFalse(res.details["reached"])
        self.assertEqual(res.details["missing"], ["elixir"])

    def test_upgrade_buildings_scan_only_diagnoses_all_settings(self):
        adb = _FakeADB()
        vision = _FakeVision()
        self.settings.set("auto_upgrade_buildings", True)
        self.settings.set("lock_townhall_upgrade", True)
        self.settings.set("building_upgrade_allow_supercharge", False)
        self.settings.set("building_upgrade_priority", "cost_max")
        self.settings.set("building_upgrade_use_menu_filter", False)
        self.settings.set("building_upgrade_resources", {"gold": True, "elixir": False, "dark_elixir": True})
        self.settings.set("building_upgrade_categories", {"traps": True, "resource": True, "defense": True, "army": True, "heroes": True})
        wl = dict(DEFAULT_BUILDING_WHITELIST)
        wl["cannon"] = False
        self.settings.set("building_upgrade_whitelist", wl)

        runner = FlowTestRunner(adb, vision, self.settings, log_cb=self.logs.append)

        fake_words = [
            ("Suggested upgrades", 1250, 70),
            ("Town Hall", 1280, 130), ("4000000", 1450, 152), ("5d", 1460, 130),
            ("Cannon", 1280, 200), ("800000", 1450, 222), ("6h", 1460, 200),
            ("Archer Tower", 1280, 270), ("1500000", 1450, 292), ("12h", 1460, 270),
            ("Army Camp", 1280, 340), ("2000000", 1450, 362), ("1d", 1460, 340),
            ("X-Bow", 1280, 410), ("3200000", 1450, 432), ("2d", 1460, 410),
        ]

        def fake_currency(screen, roi):
            y = roi[1]
            # Army Camp uses elixir (row y=340 -> price_y=362), others gold
            if 335 <= y <= 375:
                return "elixir"
            return "gold"

        with patch("bot.walls.WallUpgrader.builders", return_value=3), \
             patch("bot.walls.WallUpgrader.ensure_menu_open", return_value=True), \
             patch("bot.walls.WallUpgrader.is_menu_open", side_effect=[True, True, True, False]), \
             patch("bot.walls.WallUpgrader.close_dialog", return_value=True), \
             patch("bot.suggested_upgrades.SuggestedUpgrader.regular_builders", return_value=3), \
             patch("bot.suggested_upgrades.SuggestedUpgrader.words", return_value=fake_words), \
             patch("bot.suggested_upgrades.SuggestedUpgrader.currency", side_effect=fake_currency), \
             patch("bot.suggested_upgrades.SuggestedUpgrader.is_supercharge_row", side_effect=lambda s, y: y == 410), \
             patch("bot.suggested_upgrades.SuggestedUpgrader.close_menu", return_value=True), \
             patch("bot.walls.WallUpgrader.scroll_menu"):
            res = runner.run_flow("upgrade_buildings", mode="scan_only")

        self.assertEqual(res.status, "PASS")
        ranked_names = [r["name"] for r in res.details["ranked"]]
        # Town Hall locked, Cannon whitelisted out, Army Camp elixir disabled, X-Bow supercharge disabled!
        self.assertEqual(ranked_names, ["Archer Tower"])
        joined_logs = "\n".join(res.logs)
        self.assertIn("Khóa an toàn Town Hall", joined_logs)
        self.assertIn("Bị bỏ chọn trong danh sách chi tiết", joined_logs)
        self.assertIn("Tài nguyên elixir đang tắt", joined_logs)
        self.assertIn("Tắt nâng Supercharge", joined_logs)
        # Ensure no row click or confirm tap happened
        self.assertNotIn((1380, 285), adb.taps)

    def test_upgrade_buildings_until_confirm_never_clicks_confirm(self):
        adb = _FakeADB()
        vision = _FakeVision()
        self.settings.set("auto_upgrade_buildings", True)
        self.settings.set("building_upgrade_priority", "cost_min")
        self.settings.set("building_upgrade_use_menu_filter", False)

        runner = FlowTestRunner(adb, vision, self.settings, log_cb=self.logs.append)
        cand = {"name": "Archer Tower", "x": 1280, "y": 270, "cost": 1_500_000, "resource": "gold", "duration_sec": 43200, "page": 0}
        confirm_pt = (960, 740)

        with patch("bot.walls.WallUpgrader.builders", return_value=2), \
             patch("bot.walls.WallUpgrader.ensure_menu_open", return_value=True), \
             patch("bot.walls.WallUpgrader.is_menu_open", return_value=False), \
             patch("bot.suggested_upgrades.SuggestedUpgrader.close_menu", return_value=True), \
             patch("bot.suggested_upgrades.SuggestedUpgrader.regular_builders", return_value=2), \
             patch("bot.suggested_upgrades.SuggestedUpgrader.plan_multi_builders", return_value=(
                 [{"builder_idx": 1, "candidate": cand, "is_ready": True, "cost": 1_500_000, "resource": "gold", "balance_after": 3_000_000}],
                 set(),
                 "",
             )), \
             patch("bot.suggested_upgrades.SuggestedUpgrader._seek_candidate_row", return_value=cand), \
             patch("bot.suggested_upgrades.SuggestedUpgrader.words", return_value=[]), \
             patch("bot.suggested_upgrades.SuggestedUpgrader.menu_candidates", return_value=[cand]), \
             patch("bot.suggested_upgrades.SuggestedUpgrader.confirmation", return_value=confirm_pt), \
             patch("bot.suggested_upgrades.SuggestedUpgrader._confirm_stable_on_second_frame", return_value=True), \
             patch("bot.suggested_upgrades.SuggestedUpgrader.upgrade_close_button", return_value=None):
            res = runner.run_flow("upgrade_buildings", mode="until_confirm")

        self.assertEqual(res.status, "PASS")
        self.assertEqual(res.details["confirm_point"], confirm_pt)
        # Critical safety invariant: Confirm button was NEVER tapped on real ADB!
        self.assertNotIn(confirm_pt, adb.taps)
        # Row tap DID occur to open the Confirm dialog
        self.assertIn((1380, 285), adb.taps)

    def test_upgrade_buildings_full_storage_blocks_when_under_target(self):
        adb = _FakeADB()
        vision = _FakeVision()
        self.settings.set("auto_upgrade_buildings", True)
        self.settings.set("building_upgrade_wait_mode", "full_storage")
        self.settings.set("farm_finish_target_percent", 100)
        runner = FlowTestRunner(adb, vision, self.settings, log_cb=self.logs.append)

        with patch("bot.walls.WallUpgrader.builders", return_value=2), \
             patch("bot.village.VillageManager.get_account_capacities", return_value={"gold": 5_000_000, "elixir": 5_000_000}):
            res = runner.run_flow("upgrade_buildings", mode="scan_only", options={"ignore_wait_mode_block": False})

        self.assertEqual(res.status, "WARN")
        self.assertIn("100%", res.summary)

    def test_upgrade_walls_until_confirm_never_clicks_confirm(self):
        adb = _FakeADB()
        vision = _FakeVision()
        runner = FlowTestRunner(adb, vision, self.settings, log_cb=self.logs.append)

        def fake_run_cycle(wall_self, screen, g, e, log_callback=None):
            wall_self._transaction_submitted = True
            wall_self.adb.tap(1111, 777)
            return True

        with patch("bot.walls.WallUpgrader.builders", return_value=2), \
             patch("bot.walls.WallUpgrader.run_cycle", fake_run_cycle), \
             patch("bot.walls.WallUpgrader.close_dialog", return_value=True):
            res = runner.run_flow("upgrade_walls", mode="until_confirm")

        self.assertEqual(res.status, "PASS")
        self.assertEqual(res.details["confirm_point"], (1111, 777))
        self.assertNotIn((1111, 777), adb.taps)

    def test_upgrade_army_until_confirm_never_clicks_confirm(self):
        adb = _FakeADB()
        vision = _FakeVision()
        runner = FlowTestRunner(adb, vision, self.settings, log_cb=self.logs.append)
        cand = {"name": "Barbarian", "y": 210, "cost": 500_000, "resource": "elixir"}

        with patch("bot.walls.WallUpgrader.ensure_menu_open", return_value=True), \
             patch("bot.walls.WallUpgrader.is_menu_open", return_value=False), \
             patch("bot.walls.WallUpgrader.texts", return_value=["Army Upgrades"]), \
             patch("bot.army_upgrades.ArmyUpgrader.read_research_slot", return_value=("available", "1/1")), \
             patch("bot.army_upgrades.ArmyUpgrader.configure_filter", return_value=True), \
             patch("bot.army_upgrades.ArmyUpgrader.rows", return_value=[cand]), \
             patch("bot.suggested_upgrades.SuggestedUpgrader.words", return_value=[]), \
             patch("bot.army_upgrades.ArmyUpgrader.confirm", return_value=(920, 720)), \
             patch("bot.suggested_upgrades.SuggestedUpgrader.upgrade_close_button", return_value=None), \
             patch("bot.suggested_upgrades.SuggestedUpgrader.close_menu", return_value=True):
            res = runner.run_flow("upgrade_army", mode="until_confirm")

        self.assertEqual(res.status, "PASS")
        self.assertEqual(res.details["confirm_point"], (920, 720))
        self.assertNotIn((920, 720), adb.taps)

    def test_buy_new_buildings_until_confirm_never_clicks_green_check(self):
        adb = _FakeADB()
        vision = _FakeVision()
        runner = FlowTestRunner(adb, vision, self.settings, log_cb=self.logs.append)
        cand = {"name": "Cannon", "y": 210, "cost": 250, "resource": "gold", "duration_sec": 10}

        with patch("bot.walls.WallUpgrader.builders", return_value=2), \
             patch("bot.walls.WallUpgrader.ensure_menu_open", return_value=True), \
             patch("bot.walls.WallUpgrader.is_menu_open", return_value=False), \
             patch("bot.new_buildings.NewBuildingBuyer.seek_new", return_value=(cand, True)), \
             patch("bot.new_buildings.NewBuildingBuyer.select_for_placement", return_value=(888, 666)), \
             patch("bot.new_buildings.NewBuildingBuyer.cancel_button", return_value=(777, 666)), \
             patch("bot.suggested_upgrades.SuggestedUpgrader.close_menu", return_value=True):
            res = runner.run_flow("buy_new_buildings", mode="until_confirm")

        self.assertEqual(res.status, "PASS")
        self.assertEqual(res.details["confirm_point"], (888, 666))
        self.assertNotIn((888, 666), adb.taps)
        self.assertIn((777, 666), adb.taps)

    def test_collect_treasury_scan_only_reads_hud(self):
        adb = _FakeADB()
        vision = _FakeVision()

        def tap_with_hud(x, y, jitter=0, delay_after=0.0):
            adb.taps.append((int(x), int(y)))
            if (x, y) == (1430, 45):
                vision._last_hud = "gold"
            elif (x, y) == (1430, 105):
                vision._last_hud = "elixir"
            elif (x, y) == (1430, 165):
                vision._last_hud = "dark_elixir"
            elif (x, y) == (1000, 250):
                vision._last_hud = None

        adb.tap = tap_with_hud
        runner = FlowTestRunner(adb, vision, self.settings, log_cb=self.logs.append)
        res = runner.run_flow("collect_treasury", mode="scan_only")

        self.assertEqual(res.status, "PASS")
        self.assertEqual(res.details["balances"]["gold"], 300_000)
        self.assertEqual(res.details["balances"]["elixir"], 250_000)
        self.assertEqual(res.details["balances"]["dark_elixir"], 1_500)

    def test_plan_multi_builders_with_ongoing_jobs_does_not_scroll_up_and_logs_ocr(self):
        adb = _FakeADB()
        vision = _FakeVision()
        self.settings.set("auto_upgrade_buildings", True)
        self.settings.set("building_upgrade_use_menu_filter", False)
        self.settings.set("building_upgrade_resources", {"gold": False, "elixir": False, "dark_elixir": True})
        self.settings.set("building_upgrade_categories", {"traps": False, "resource": False, "defense": False, "army": False, "heroes": True})
        wl = dict(DEFAULT_BUILDING_WHITELIST)
        for h in ("barbarianking", "archerqueen", "minionprince", "grandwarden", "royalchampion", "dragonduke"):
            wl[h] = False
        self.settings.set("building_upgrade_whitelist", wl)

        runner = FlowTestRunner(adb, vision, self.settings, log_cb=self.logs.append)

        # Header at y=220 because 3 builders are busy above it (y=75, 125, 175)
        fake_words = [
            ("Cannon", 1280, 75), ("17h 42m", 1500, 75),
            ("Mortar", 1280, 125), ("4d 11h", 1500, 125),
            ("Air Defense", 1280, 175), ("4d 23h", 1500, 175),
            ("Filtered upgrades", 1280, 220),
            ("Barbarian King", 1280, 270), ("180000", 1450, 292), ("7d", 1460, 270),
        ]

        scroll_calls = []

        with patch("bot.walls.WallUpgrader.builders", return_value=3), \
             patch("bot.walls.WallUpgrader.ensure_menu_open", return_value=True), \
             patch("bot.walls.WallUpgrader.is_menu_open", return_value=False), \
             patch("bot.suggested_upgrades.SuggestedUpgrader.regular_builders", return_value=3), \
             patch("bot.suggested_upgrades.SuggestedUpgrader.words", return_value=fake_words), \
             patch("bot.suggested_upgrades.SuggestedUpgrader.currency", return_value="dark_elixir"), \
             patch("bot.suggested_upgrades.SuggestedUpgrader.close_menu", return_value=True), \
             patch("bot.walls.WallUpgrader.scroll_menu", side_effect=lambda d, **kw: scroll_calls.append(d)):
            res = runner.run_flow("upgrade_buildings", mode="until_confirm")

        self.assertEqual(res.status, "FAIL")
        self.assertNotIn("up", scroll_calls)
        joined = "\n".join(res.logs)
        self.assertIn("CẢNH BÁO: Nhóm 'heroes' đang BẬT", joined)
        self.assertIn("Đã ở đầu danh sách (tiêu đề tại y=220)", joined)
        self.assertIn("Barbarian King", joined)
        self.assertIn("Bị bỏ chọn trong danh sách chi tiết (barbarianking)", joined)
        self.assertIn("Cuộn xuống trang 1/3", joined)

    def test_main_combat_scan_only_stops_at_attack_menu(self):
        adb = _FakeADB()
        vision = _FakeVision()
        runner = FlowTestRunner(adb, vision, self.settings, log_cb=self.logs.append)

        with patch("bot.battle.BattleSearcher.open_attack_menu", return_value=True) as mock_open, \
             patch.object(vision, "is_attack_menu_open", return_value=(True, (275, 650))):
            res = runner.run_flow("main_combat", mode="scan_only")

        mock_open.assert_called_once()
        self.assertEqual(res.status, "PASS")
        self.assertEqual(res.details["find_match_button"], (275, 650))
        self.assertEqual(adb.taps, [])

    def test_main_combat_until_confirm_scans_deck_and_plans_without_dropping_troops(self):
        adb = _FakeADB()
        vision = _FakeVision()
        vision.battle_phase = lambda _scr: "scouting"
        vision.is_enemy_base = lambda _scr: True
        runner = FlowTestRunner(adb, vision, self.settings, log_cb=self.logs.append)

        fake_cards = {
            "troops": [{"name": "dragon", "x": 200, "y": 820, "count": 10}],
            "spells": [{"name": "angry_spell", "x": 350, "y": 820, "count": 4}],
            "heroes": [{"name": "king", "x": 450, "y": 820}],
            "siege": [],
        }
        fake_pts = [(300, 200), (450, 180), (1100, 180), (1250, 200)]

        with patch("bot.combat.CombatExecutor.scan_deck_cards", return_value=fake_cards), \
             patch("core.deployment.DeploymentPlanner.points", return_value=fake_pts), \
             patch("bot.flow_tester.FlowTestRunner._safe_exit_scouting_home", return_value=True) as mock_exit:
            res = runner.run_flow(
                "main_combat",
                mode="until_confirm",
                options={"ignore_loot_filter": False, "return_home_after_scan": True},
            )

        self.assertEqual(res.status, "PASS")
        self.assertEqual(res.details["troop_points"], 4)
        mock_exit.assert_called_once()
        # Only card slot selection tap (200, 820) happened — zero battlefield drops!
        self.assertEqual(adb.taps, [(200, 820)])

    def test_main_combat_real_runs_execute_full_attack(self):
        adb = _FakeADB()
        vision = _FakeVision()
        vision.battle_phase = lambda _scr: "scouting"
        vision.is_enemy_base = lambda _scr: True
        runner = FlowTestRunner(adb, vision, self.settings, log_cb=self.logs.append)

        fake_result = {
            "stars": 2,
            "duration": 42.5,
            "completed": True,
            "loot_gained": {"gold": 700_000, "elixir": 650_000, "dark_elixir": 5_000},
            "reason": "stars_reached",
        }
        with patch("bot.combat.CombatExecutor.execute_full_attack", return_value=fake_result) as mock_exec, \
             patch("bot.flow_tester.FlowTestRunner._wait_return_home_after_battle", return_value=True):
            res = runner.run_flow("main_combat", mode="real")

        mock_exec.assert_called_once()
        self.assertEqual(res.status, "PASS")
        self.assertIn("2 sao", res.summary)

    def test_ui_test_tool_window_smoke(self):
        app = QApplication.instance() or QApplication(sys.argv)
        win = TestToolWindow(
            settings_path=self.cfg_path,
            main_config_path=str(Path(self.tmpdir.name) / "nonexistent_main.json"),
        )
        try:
            self.assertEqual(win.combo_flow.count(), 9)
            for idx in range(win.combo_flow.count()):
                win.combo_flow.setCurrentIndex(idx)
                app.processEvents()
            win._on_devices_scanned([
                {"serial": "127.0.0.1:16384", "state": "device", "model": "MuMu12"},
                {"serial": "127.0.0.1:5555", "state": "offline", "model": ""},
            ], "")
            self.assertEqual(win._selected_serial(), "127.0.0.1:5555")
            win.combo_devices.setCurrentIndex(0)
            self.assertEqual(win._selected_serial(), "127.0.0.1:16384")
            win._toggle_theme()
            win._toggle_theme()
            self.assertFalse(Path(self.tmpdir.name, "nonexistent_main.json").exists())
        finally:
            win.close()


if __name__ == "__main__":
    unittest.main()
