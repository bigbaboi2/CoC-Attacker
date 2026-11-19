import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np

from bot.builder_battle import BuilderBattle
from bot.combat import CombatExecutor


def _make_stage_frame(is_cave=True, draw_red_boundary=True):
    frame = np.zeros((900, 1600, 3), dtype=np.uint8)
    tile_bgr = (110, 82, 66) if is_cave else (105, 128, 84)
    tint_bgr = (114, 89, 89) if is_cave else (108, 130, 104)
    line_bgr = (100, 74, 100) if is_cave else (94, 110, 115)

    arena_diamond = np.array([[800, 105], [1360, 395], [800, 685], [240, 395]], dtype=np.int32)
    cv2.fillConvexPoly(frame, arena_diamond, tile_bgr)

    base_diamond = np.array([[800, 210], [1085, 395], [800, 580], [515, 395]], dtype=np.int32)
    cv2.fillConvexPoly(frame, base_diamond, tint_bgr)

    # Inner stone wall core and high-contrast buildings inside the base
    wall_diamond = np.array([[800, 275], [985, 395], [800, 515], [615, 395]], dtype=np.int32)
    cv2.polylines(frame, [wall_diamond], True, (185, 185, 190), 5)
    for bx in range(690, 910, 45):
        for by in range(320, 470, 45):
            cv2.rectangle(frame, (bx, by), (bx + 24, by + 24), (45, 145, 220), 2)

    if draw_red_boundary:
        cv2.polylines(frame, [base_diamond], True, line_bgr, 3)

    return frame, base_diamond


class BuilderBattleRecoveryTests(unittest.TestCase):
    def test_failed_deployment_pans_down_then_up_only_once(self):
        battle = BuilderBattle.__new__(BuilderBattle)
        frame = np.zeros((900, 1600, 3), dtype=np.uint8)
        battle.adb = SimpleNamespace(swipe=Mock(), screencap=Mock(return_value=frame))
        battle.base = SimpleNamespace(tap=Mock())
        battle.is_card_selected = Mock(return_value=False)
        battle.stage = 1
        battle.approach = object()
        battle.rejected_entries = [(700, 400)]
        battle.map_pan_attempts = 0
        logs = []
        cards = [('bomber', (300, 800))]
        self.assertTrue(battle.pan_map_for_deployment(logs.append, cards))
        self.assertIsNone(battle.approach)
        self.assertEqual(battle.rejected_entries, [])
        self.assertTrue(battle.pan_map_for_deployment(logs.append, cards))
        self.assertFalse(battle.pan_map_for_deployment(logs.append, cards))
        self.assertEqual([call.args[2:] for call in battle.adb.swipe.call_args_list],
                         [(1000, 550), (1000, 370)])

    def test_empty_sweep_needs_two_observations_and_logs_once(self):
        battle = BuilderBattle.__new__(BuilderBattle)
        battle.stage = 1
        battle.approach = ((800, 400), (1, 0), (0, 1), None)
        battle.cards = Mock(return_value=[('bomber', (300, 800))])
        battle.stack_count = Mock(return_value=0)
        battle.slot_state = Mock(return_value='dead')
        battle.empty_sweep_observations = 0
        battle.empty_sweep_logged = False
        battle.empty_stacks = set()
        logs = []
        frame = np.zeros((900, 1600, 3), dtype=np.uint8)
        battle.run_backup_sweep(frame, logs.append)
        self.assertEqual(logs, [])
        battle.run_backup_sweep(frame, logs.append)
        battle.run_backup_sweep(frame, logs.append)
        self.assertEqual(len(logs), 1)
        self.assertIn('hai lượt quét', logs[0])

    def test_unaccepted_backup_drop_remains_eligible_for_retry(self):
        battle = BuilderBattle.__new__(BuilderBattle)
        battle.stage = 1
        battle.approach = ((800, 400), (1, 0), (0, 1), None)
        battle.cards = Mock(return_value=[('bomber', (300, 800))])
        battle.stack_count = Mock(return_value=1)
        battle.slot_state = Mock(return_value='waiting')
        battle.drop_candidates = Mock(return_value=iter([(800, 400)]))
        battle.send_drop = Mock()
        battle.pan_map_for_deployment = Mock(return_value=True)
        battle.empty_sweep_observations = 0
        battle.empty_sweep_logged = False
        battle.empty_stacks = set()
        battle.troops = []
        frame = np.zeros((900, 1600, 3), dtype=np.uint8)
        battle.adb = SimpleNamespace(screencap=Mock(return_value=frame))
        with patch('bot.builder_battle.time.sleep'):
            battle.run_backup_sweep(frame, lambda _: None)
        self.assertEqual(battle.empty_stacks, set())
        self.assertEqual(battle.troops, [])
        battle.pan_map_for_deployment.assert_called_once()

    def test_unknown_battle_state_requests_recovery(self):
        battle = BuilderBattle.__new__(BuilderBattle)
        frame = np.zeros((900, 1600, 3), dtype=np.uint8)
        engine = SimpleNamespace(_request_recovery=Mock())
        battle.base = SimpleNamespace(village_state=None, is_home=Mock(return_value=False), engine=engine)
        battle.adb = SimpleNamespace(_guard_input=Mock(), screencap=Mock(return_value=frame))
        battle.vision = SimpleNamespace(game_fault=Mock(return_value=None))
        battle.result_button = Mock(return_value=None)
        battle.phase = Mock(return_value=None)
        battle.combat_seen = True
        battle.planned = True
        battle.match_requested = 0
        battle.unknown_since = 1
        with patch('bot.builder_battle.time.monotonic', return_value=100):
            self.assertFalse(battle.monitor(lambda _: None, seconds=1))
        engine._request_recovery.assert_called_once_with('Trận làng đêm mất trạng thái')

    def test_main_battle_pans_and_rescans_before_first_drop(self):
        combat = CombatExecutor.__new__(CombatExecutor)
        frame = np.zeros((900, 1600, 3), dtype=np.uint8)
        combat.adb = SimpleNamespace(swipe=Mock(), screencap=Mock(return_value=frame))
        combat.vision = SimpleNamespace(battle_phase=Mock(return_value='scouting'))
        combat.planner = SimpleNamespace(points=Mock(side_effect=[[], [(600, 500)]]))
        combat._log = Mock()
        with patch('bot.combat.time.sleep'):
            self.assertEqual(combat._find_drop_points_after_pan(16), [(600, 500)])
        self.assertEqual(combat.adb.swipe.call_count, 2)

    def test_entry_detects_translucent_red_boundary_on_cave_and_grass_stages(self):
        for is_cave in (True, False):
            frame, base_diamond = _make_stage_frame(is_cave=is_cave, draw_red_boundary=True)
            battle = BuilderBattle.__new__(BuilderBattle)
            battle.rejected_entries = []
            battle.safe_mask = None
            approach = battle.entry(frame)
            self.assertIsNotNone(approach, f'Failed to find entry for is_cave={is_cave}')
            point, direction, inward, _ = approach
            self.assertTrue(battle.is_safe_drop_point(point))
            # Point must lie strictly outside the red deployment boundary diamond
            self.assertLess(cv2.pointPolygonTest(base_diamond, (float(point[0]), float(point[1])), True), -10.0)
            # Inward vector must point toward the base center (800, 395)
            to_center = np.array([800.0 - point[0], 395.0 - point[1]])
            self.assertGreater(float(np.dot(inward, to_center)), 0.0)
            # Building interior must be marked forbidden in safe_mask
            self.assertFalse(battle.is_safe_drop_point((800, 395)))

    def test_drop_candidates_and_resolve_drop_point_filter_forbidden_zones(self):
        battle = BuilderBattle.__new__(BuilderBattle)
        safe_mask = np.zeros((900, 1600), dtype=bool)
        safe_mask[240:320, 480:560] = True
        battle.safe_mask = safe_mask
        anchor = (520, 280)
        direction = np.array([1.0, 0.0])
        inward = np.array([0.0, 1.0])
        # A lateral offset that lands outside safe_mask (e.g. x=610) is snapped back into safe_mask
        resolved = battle.resolve_drop_point((610, 280), anchor, direction, inward, depth=10)
        self.assertTrue(battle.is_safe_drop_point(resolved))
        candidates = list(battle.drop_candidates(anchor, direction, inward))
        self.assertTrue(candidates)
        self.assertTrue(all(battle.is_safe_drop_point(pt) for pt in candidates))

    def test_stage_one_can_reveal_boundary_and_fallback_to_terrain_entry(self):
        frame, base_diamond = _make_stage_frame(is_cave=True, draw_red_boundary=False)
        battle = BuilderBattle.__new__(BuilderBattle)
        battle.stage = 1
        battle.revealed_for_stage = False
        battle.revealed_stage_two = False
        battle.zoomed_for_stage = True
        battle.approach = None
        battle.rejected_entries = []
        battle.safe_mask = None
        battle.troops = []
        battle.empty_stacks = set()
        battle.combat_seen = False
        battle.cards = Mock(return_value=[('boxer_giant', (300, 800))])
        battle.is_card_selected = Mock(return_value=False)
        battle.stack_count = Mock(return_value=1)
        battle.deploy_stack = Mock(return_value=True)
        battle.send_drop = Mock()
        battle.phase = Mock(return_value='scouting')
        battle.adb = SimpleNamespace(screencap=Mock(return_value=frame))
        battle.base = SimpleNamespace(tap=Mock())
        logs = []
        self.assertTrue(battle.deploy(frame, logs.append))
        self.assertTrue(battle.revealed_for_stage)
        self.assertIsNotNone(battle.approach)
        point = battle.approach[0]
        self.assertLess(cv2.pointPolygonTest(base_diamond, (float(point[0]), float(point[1])), True), -10.0)

    def test_deploy_recognizes_troops_already_deployed_by_initial_batch_wave(self):
        frame = np.zeros((900, 1600, 3), dtype=np.uint8)
        battle = BuilderBattle.__new__(BuilderBattle)
        battle.stage = 1
        battle.approach = ((500, 300), np.array([1.0, 0.0]), np.array([0.0, 1.0]), None)
        battle.rejected_entries = []
        battle.safe_mask = None
        battle.troops = []
        battle.empty_stacks = set()
        battle.combat_seen = False
        battle.cards = Mock(return_value=[('power_pekka', (300, 800)), ('night_witch', (410, 800))])
        # Before batch drop: stack_count reads '1x' (1); after batch drop: badge is gone (None) and slot is 'deployed'
        battle.stack_count = Mock(side_effect=[1, 1, None, None])
        battle.slot_state = Mock(return_value='deployed')
        battle.send_drop = Mock()
        battle.deploy_stack = Mock(return_value=False)
        battle.pan_map_for_deployment = Mock()
        battle.phase = Mock(return_value='combat')
        battle.observe_combat = Mock()
        battle.adb = SimpleNamespace(screencap=Mock(return_value=frame))
        logs = []
        self.assertTrue(battle.deploy(frame, logs.append))
        self.assertEqual(len(battle.troops), 2)
        self.assertEqual(battle.empty_stacks, {300, 410})
        battle.deploy_stack.assert_not_called()
        battle.pan_map_for_deployment.assert_not_called()

    def test_backup_sweep_does_not_redeploy_already_tracked_troop_with_no_badge(self):
        frame = np.zeros((900, 1600, 3), dtype=np.uint8)
        battle = BuilderBattle.__new__(BuilderBattle)
        battle.stage = 1
        battle.approach = ((500, 300), np.array([1.0, 0.0]), np.array([0.0, 1.0]), None)
        battle.cards = Mock(return_value=[('night_witch', (410, 800))])
        battle.stack_count = Mock(return_value=None)
        battle.slot_state = Mock(return_value='waiting')
        battle.send_drop = Mock()
        battle.pan_map_for_deployment = Mock()
        battle.empty_sweep_observations = 0
        battle.empty_sweep_logged = False
        battle.empty_stacks = {410}
        battle.troops = []
        logs = []
        battle.run_backup_sweep(frame, logs.append)
        battle.send_drop.assert_not_called()
        battle.pan_map_for_deployment.assert_not_called()

    def test_pan_map_for_deployment_still_swipes_when_card_stays_selected_in_coc(self):
        battle = BuilderBattle.__new__(BuilderBattle)
        frame = np.zeros((900, 1600, 3), dtype=np.uint8)
        battle.adb = SimpleNamespace(swipe=Mock(), screencap=Mock(return_value=frame))
        battle.base = SimpleNamespace(tap=Mock())
        battle.is_card_selected = Mock(return_value=True)
        battle.stage = 1
        battle.approach = object()
        battle.rejected_entries = [(700, 400)]
        battle.map_pan_attempts = 0
        logs = []
        cards = [('power_pekka', (300, 800))]
        self.assertTrue(battle.pan_map_for_deployment(logs.append, cards))
        battle.adb.swipe.assert_called_once()

    def test_stage_two_slot_state_ignores_shortcut_badges_and_purple_header(self):
        from bot.builder_battle import Troop
        frame = np.zeros((900, 1600, 3), dtype=np.uint8)
        # Draw saturated portrait pixels for 3 troops so none are 'dead'
        for cx in (294, 939, 1067, 1190):
            frame[795:850, cx - 30:cx + 30] = (200, 120, 40)
            # Purple ability header at y=742..774 (present on undeployed & deployed Stage 2 cards)
            frame[742:774, cx - 10:cx + 42] = (230, 60, 190)
        # Colored top-left shortcut badges (cyan, purple, red, orange)
        frame[742:774, 294 - 42:294 - 14] = (210, 210, 40)    # cyan (key 1)
        frame[742:774, 939 - 42:939 - 14] = (220, 70, 170)    # purple (key 6)
        frame[742:774, 1067 - 42:1067 - 14] = (50, 50, 235)   # red (key 7)
        frame[742:774, 1190 - 42:1190 - 14] = (40, 150, 245)  # orange (key 8)
        # Only slot 6 (Bomber@939) is actually deployed: green HP bar at y=784..795
        frame[784:796, 939 - 40:939 + 38] = (45, 220, 70)

        self.assertEqual(BuilderBattle.slot_state(frame, Troop('night_witch', (294, 808), 0)), 'waiting')
        self.assertEqual(BuilderBattle.slot_state(frame, Troop('power_pekka', (1067, 818), 0)), 'waiting')
        self.assertEqual(BuilderBattle.slot_state(frame, Troop('power_pekka', (1190, 818), 0)), 'waiting')
        self.assertEqual(BuilderBattle.slot_state(frame, Troop('bomber', (939, 818), 0)), 'deployed')
        charge, health = BuilderBattle.ability_hud(frame, Troop('bomber', (939, 818), 0))
        self.assertGreater(charge, 0.85)
        self.assertGreater(health, 0.85)

    def test_stage_two_backup_sweep_deploys_waiting_troop_even_after_empty_sweep_log(self):
        frame = np.zeros((900, 1600, 3), dtype=np.uint8)
        battle = BuilderBattle.__new__(BuilderBattle)
        battle.stage = 2
        battle.approach = ((500, 300), np.array([1.0, 0.0]), np.array([0.0, 1.0]), None)
        battle.cards = Mock(return_value=[('power_pekka', (1067, 818))])
        battle.stack_count = Mock(return_value=None)
        battle.slot_state = Mock(side_effect=['waiting', 'deployed', 'deployed'])
        battle.send_drop = Mock()
        battle.pan_map_for_deployment = Mock()
        battle.empty_sweep_observations = 2
        battle.empty_sweep_logged = True
        battle.empty_stacks = set()
        battle.troops = []
        battle.adb = SimpleNamespace(screencap=Mock(return_value=frame))
        logs = []
        with patch('bot.builder_battle.time.sleep'):
            battle.run_backup_sweep(frame, logs.append)
        battle.send_drop.assert_called_once()
        self.assertEqual(len(battle.troops), 1)
        self.assertTrue(any('Vét thêm' in line for line in logs))
        self.assertTrue(any('kết quả vét' in line for line in logs))

    def test_stage_two_deploy_dispatches_survivor_with_existing_hp_bar_and_used_ability(self):
        from bot.builder_battle import Troop
        frame_before = np.zeros((900, 1600, 3), dtype=np.uint8)
        frame_after_drop = np.zeros((900, 1600, 3), dtype=np.uint8)
        # Card 556 is a Stage 1 Night Witch survivor: saturated portrait + green HP bar, NO purple ability charge
        for f in (frame_before, frame_after_drop):
            f[795:850, 556 - 25:556 + 25] = (200, 120, 40)
            f[784:796, 556 - 40:556 + 38] = (45, 220, 70)

        battle = BuilderBattle.__new__(BuilderBattle)
        battle.stage = 2
        battle.approach = ((880, 180), np.array([0.8, 0.6]), np.array([-0.6, 0.8]), None)
        battle.safe_mask = None
        battle.troops = []
        battle.empty_stacks = set()
        battle.stage_dispatched = set()
        battle.rejected_entries = []
        battle.last_deploy_notice = 0
        battle.zoomed_for_stage = True
        battle.revealed_for_stage = False
        battle.combat_seen = False
        battle.map_pan_attempts = 0
        battle.cards = Mock(return_value=[('night_witch', (556, 808))])
        battle.stack_count = Mock(return_value=None)
        battle.phase = Mock(return_value='combat')
        battle.observe_combat = Mock()
        battle.send_drop = Mock()
        battle.base = SimpleNamespace(tap=Mock())
        # frame_before -> frame_after_drop (after wave) -> frame_after_drop (identical after probe tap => already gone from dock)
        battle.adb = SimpleNamespace(screencap=Mock(return_value=frame_after_drop))
        logs = []

        self.assertEqual(BuilderBattle.slot_state(frame_before, Troop('night_witch', (556, 808), 0)), 'deployed')
        self.assertTrue(battle.deploy(frame_before, logs.append))
        battle.send_drop.assert_called_once()
        self.assertIn(556, battle.stage_dispatched)
        self.assertEqual(len(battle.troops), 1)
        self.assertTrue(any('survivor_waiting' in line for line in logs))
        self.assertTrue(any('survivor_retry' in line for line in logs))

    def test_stage_two_backup_sweep_dispatches_survivor_with_existing_hp_bar(self):
        frame = np.zeros((900, 1600, 3), dtype=np.uint8)
        frame[795:850, 683 - 25:683 + 25] = (200, 120, 40)
        frame[784:796, 683 - 40:683 + 38] = (45, 220, 70)

        battle = BuilderBattle.__new__(BuilderBattle)
        battle.stage = 2
        battle.approach = ((880, 180), np.array([0.8, 0.6]), np.array([-0.6, 0.8]), None)
        battle.safe_mask = None
        battle.troops = []
        battle.empty_stacks = set()
        battle.stage_dispatched = set()
        battle.rejected_entries = []
        battle.empty_sweep_observations = 0
        battle.empty_sweep_logged = False
        battle.cards = Mock(return_value=[('night_witch', (683, 808))])
        battle.stack_count = Mock(return_value=None)
        battle.phase = Mock(return_value='combat')
        battle.send_drop = Mock()
        battle.pan_map_for_deployment = Mock()
        battle.base = SimpleNamespace(tap=Mock())
        battle.adb = SimpleNamespace(screencap=Mock(return_value=frame))
        logs = []
        with patch('bot.builder_battle.time.sleep'):
            battle.run_backup_sweep(frame, logs.append)
        battle.send_drop.assert_called_once()
        self.assertIn(683, battle.stage_dispatched)
        self.assertTrue(any('survivor_ok' in line for line in logs))

    def test_detect_anti_bat_threats_air_bombs_and_roaster(self):
        frame = np.zeros((900, 1600, 3), dtype=np.uint8)
        # Cobalt-blue Air Bombs balloons (HSV H~118 => BGR strong blue, moderate green/low red) above purple launcher (H~142)
        cv2.circle(frame, (520, 320), 16, (235, 80, 30), -1)
        cv2.rectangle(frame, (505, 342), (538, 372), (160, 40, 105), -1)
        # Roaster fallback HSV signature: purple drum + red rim + golden flame core
        cv2.rectangle(frame, (760, 340), (805, 385), (105, 45, 75), -1)
        cv2.circle(frame, (782, 355), 12, (20, 30, 230), -1)
        cv2.circle(frame, (782, 352), 7, (30, 180, 245), -1)

        battle = BuilderBattle.__new__(BuilderBattle)
        battle.base = SimpleNamespace(match=Mock(return_value=None))
        threats = battle.detect_anti_bat_threats(frame)
        self.assertEqual(len(threats['air_bombs']), 1)
        self.assertEqual(len(threats['roasters']), 1)

    def test_night_witch_saves_ability_in_stage_one_and_uses_in_stage_two(self):
        from bot.builder_battle import Troop
        frame = np.zeros((900, 1600, 3), dtype=np.uint8)
        battle = BuilderBattle.__new__(BuilderBattle)
        battle.stage = 1
        battle.base = SimpleNamespace(tap=Mock(), village_state=None, match=Mock(return_value=None))
        battle.detect_anti_bat_threats = Mock(return_value={'air_bombs': [(520, 320)], 'roasters': []})
        battle.last_nw_ability_at = 0.0
        battle.last_threat_log = None
        battle.nw_hold_logged = set()
        now = time.monotonic()

        # Stage 1 healthy Night Witch (health=0.85, age=9s): hold ability for Stage 2!
        nw_healthy = Troop('night_witch', (303, 808), now - 9.0)
        battle.troops = [nw_healthy]
        battle.ability_hud = Mock(return_value=(0.95, 0.85))
        logs = []
        battle.use_abilities(frame, logs.append)
        battle.base.tap.assert_not_called()
        self.assertTrue(any('giữ kỹ năng night_witch' in line for line in logs))

        # Stage 1 emergency low HP (health=0.24): save/trigger immediately!
        battle.ability_hud = Mock(return_value=(0.95, 0.24))
        battle.use_abilities(frame, logs.append)
        battle.base.tap.assert_called_once_with((303, 808), .16)

        # Stage 2 with NO threats (health=0.68, age=4s): triggers promptly in Stage 2
        battle.stage = 2
        battle.base.tap.reset_mock()
        battle.detect_anti_bat_threats = Mock(return_value={'air_bombs': [], 'roasters': []})
        battle.last_nw_ability_at = 0.0
        battle.last_threat_log = None
        nw_s2 = Troop('night_witch', (430, 808), now - 4.0)
        battle.troops = [nw_s2]
        battle.ability_hud = Mock(return_value=(0.95, 0.68))
        battle.use_abilities(frame, logs.append)
        battle.base.tap.assert_called_once_with((430, 808), .16)


if __name__ == '__main__':
    unittest.main()
