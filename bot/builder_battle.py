import re
import time
from dataclasses import dataclass

import cv2
import numpy as np

from bot.builder_army import BuilderArmy


@dataclass
class Troop:
    kind: str
    card: tuple
    deployed: float
    used: bool = False
    last_attempt: float = 0
    pending_charge: float = 0.
    attempts: int = 0


class BuilderBattle:
    """Fight both stages without using Home Village retreat rules."""

    CARDS = (10, 705, 1580, 190)
    FIELD = (95, 85, 1410, 635)
    MULTI_UNIT = {"beta_minion", "sneaky_archer", "raged_barbarian"}
    CARD_SCALES = (1., .68, .72, .76, .79, .82, .86, .91, 1.1, 1.18)
    PORTRAITS = {
        kind: f"deploy-troop/New folder/use_builder_{kind}"
        for kind in (
            "bomber", "power_pekka", "boxer_giant", "sneaky_archer", "raged_barbarian",
            "beta_minion", "cannon_cart", "drop_ship", "electrofire_wizard",
            "hog_glider", "night_witch",
        )
    }
    PORTRAITS.update(baby_dragon="use_main_use_baby_dragon_builder",
                     machine="nighthero1", copter="nighthero2")
    ORDER = ("boxer_giant", "power_pekka", "machine", "copter", "raged_barbarian",
             "hog_glider", "bomber", "cannon_cart", "night_witch", "sneaky_archer",
             "electrofire_wizard", "drop_ship", "baby_dragon", "beta_minion")
    FRONTLINE = {"boxer_giant", "power_pekka", "machine", "copter", "raged_barbarian", "hog_glider"}

    def __init__(self, base, record_stat=None):
        self.base = base
        self.adb = base.adb
        self.vision = base.vision
        self.in_progress = False
        self.record_stat = record_stat
        self.match_counted = False
        self.result_counted = False
        self.stage = 0
        self.troops = []
        self.planned = False
        self.approach = None
        self.rejected_entries = []
        self.speed_stage = 0
        self.next_speed_check_at = 0
        self.sweep_pending = False
        self.empty_stacks = set()
        self.stage_dispatched = set()
        self.last_nw_ability_at = 0.0
        self.last_threat_log = None
        self.nw_hold_logged = set()
        self.combat_seen = False
        self.match_requested = 0
        self.zoomed_for_stage = False
        self.last_deploy_notice = 0
        self.revealed_stage_two = False
        self.revealed_for_stage = False
        self.safe_mask = None
        self.last_battlefield_info = None
        self.find_now_failures = 0
        self.last_sweep_at = 0
        self.map_pan_attempts = 0
        self.empty_sweep_observations = 0
        self.empty_sweep_logged = False
        self.unknown_since = 0
        self.army = BuilderArmy(self)

    def phase(self, screen):
        words = self.base.walls.texts(screen, (530, 5, 540, 110), scale=1)
        title = "".join(ch for ch in "".join(words).lower() if ch.isalnum())
        if "battlestarts" in title or "attackstarts" in title or ("stage2" in title and "prepare" in title):
            return "scouting"
        if "battleends" in title or "attackends" in title or "timeleft" in title:
            return "combat"
        if self.combat_control(screen):
            return "combat"
        return None

    def combat_control(self, screen):
        return self.base.match(screen, ("builder_end_battle",), (5, 570, 230, 145), .91)

    def open_menu(self, screen):
        button = self.vision.match_template(screen, "builderbase", .83, roi=self.base.BATTLE)
        if not button:
            return None
        self.base.tap(button, .55)
        return self.base.wait_for(self.find_now, 5)

    def start(self, screen, log):
        if self.dismiss_star_bonus(screen, log):
            time.sleep(0.5)
            screen = self.adb.screencap()
        menu = self.open_menu(screen)
        if menu is None:
            if self.dismiss_star_bonus(screen, log):
                time.sleep(0.5)
                screen = self.adb.screencap()
                menu = self.open_menu(screen)
        if menu is None:
            self.find_now_failures += 1
            log(f"[BH] Chưa xác nhận nút Find Now (lần {self.find_now_failures}).")
            fresh = self.adb.screencap()
            dismissed = self.dismiss_star_bonus(fresh, log)
            if not dismissed:
                close = self.vision.find_close_button(fresh)
                if close:
                    self.base.tap(close, 0.5)
                    log("[BH] Đã bấm nút đóng popup chặn màn hình.")
                else:
                    self.base.close_panel(fresh)
            return False
        self.find_now_failures = 0
        request = self.army.request()
        if not self.army.uses_current_lineup(request):
            menu = self.army.prepare(menu, *request, log)
            if menu is None:
                return False
        self.stage = 1
        self.revealed_stage_two = False
        self.revealed_for_stage = False
        self.safe_mask = None
        self.last_battlefield_info = None
        self.zoomed_for_stage = False
        self.last_deploy_notice = 0
        self.planned = False
        self.approach = None
        self.rejected_entries = []
        self.speed_stage = 0
        self.next_speed_check_at = 0
        self.sweep_pending = False
        self.empty_stacks = set()
        self.stage_dispatched = set()
        self.last_nw_ability_at = 0.0
        self.last_threat_log = None
        self.nw_hold_logged = set()
        self.map_pan_attempts = 0
        self.empty_sweep_observations = 0
        self.empty_sweep_logged = False
        self.unknown_since = 0
        self.troops = []
        self.combat_seen = False
        self.match_counted = False
        self.result_counted = False
        self.in_progress = True
        self.match_requested = time.monotonic()
        self.base.tap(self.find_now(menu), 1)
        log("[BH] Đã mở tìm trận Builder Base.")
        return self.monitor(log)

    def find_now(self, screen):
        hit = self.base.match(screen, ("night_army/find_now",), (140, 315, 1400, 575),
                              .86, (.82, 1., 1.15))
        return hit if hit and self.base.green_button(screen, hit) else None

    def cards(self, screen, roi=None, threshold=.82):
        x, y, w, h = roi or self.CARDS
        crop = screen[y:y+h, x:x+w]
        detected = []
        for kind in self.ORDER:
            for scale in self.CARD_SCALES:
                template = self.base.template(self.PORTRAITS[kind], scale)
                if template is None:
                    break
                if any(a > b for a, b in zip(template.shape[:2], crop.shape[:2])):
                    continue
                scores = cv2.matchTemplate(crop, template, cv2.TM_CCOEFF_NORMED)
                th, tw = template.shape[:2]
                for _ in range(8):
                    _, score, _, (px, py) = cv2.minMaxLoc(scores)
                    if score < threshold:
                        break
                    detected.append((kind, (x+px+tw//2, y+py+th//2), score))
                    scores[:, max(0, px-tw//2):px+tw//2+1] = -1
        unique = []
        for kind, point, score in sorted(detected, key=lambda item: -item[2]):
            if all(abs(point[0]-prior[1][0]) > 45 for prior in unique):
                unique.append((kind, point, score))
        return [(kind, point) for kind, point, _ in sorted(
            unique, key=lambda item: (self.ORDER.index(item[0]), item[1][0]))]

    def analyze_battlefield(self, screen):
        """Segment the playable arena, red deployment boundary, and safe outer ground for both Cave and Grass stages."""
        if screen is None or not isinstance(screen, np.ndarray) or screen.ndim != 3:
            return None
        orig_h, orig_w = screen.shape[:2]
        if (orig_h, orig_w) != (900, 1600):
            if orig_h < 200 or orig_w < 300:
                return None
            work = cv2.resize(screen, (1600, 900), interpolation=cv2.INTER_CUBIC)
            sx, sy = orig_w / 1600.0, orig_h / 900.0
        else:
            work = screen
            sx, sy = 1.0, 1.0
        x, y, w, h = self.FIELD
        scene = work[y:y+h, x:x+w]
        hsv = cv2.cvtColor(scene, cv2.COLOR_BGR2HSV).astype(np.float32)
        lab = cv2.cvtColor(scene, cv2.COLOR_BGR2LAB).astype(np.float32)
        gray = cv2.cvtColor(scene, cv2.COLOR_BGR2GRAY)
        b, g, r = cv2.split(scene.astype(np.float32))
        rg = r - g
        H, S, V = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
        A = lab[:, :, 1]

        cave_seed = (H >= 96) & (H <= 125) & (S >= 45) & (S <= 135) & (V >= 45) & (V <= 145) & (rg <= -6)
        grass_seed = (H >= 60) & (H <= 95) & (S >= 45) & (S <= 130) & (V >= 50) & (V <= 148) & (rg <= -18) & (b >= 45)
        cave_count = int(np.count_nonzero(cave_seed))
        grass_count = int(np.count_nonzero(grass_seed))
        if max(cave_count, grass_count) < 1200:
            generic_seed = (H >= 55) & (H <= 130) & (S >= 28) & (S <= 165) & (V >= 42) & (V <= 168) & (rg <= -4)
            if int(np.count_nonzero(generic_seed)) < 1200:
                return None
            is_cave = bool(np.median(H[generic_seed]) >= 96)
            seed_tiles = generic_seed
        else:
            is_cave = cave_count > grass_count
            seed_tiles = cave_seed if is_cave else grass_seed
        med_a = float(np.median(A[seed_tiles]))
        med_rg = float(np.median(rg[seed_tiles]))

        arena_raw = (
            (H >= 88) & (H <= 160) & (S >= 28) & (S <= 148) & (V >= 42) & (V <= 152)
            if is_cave else
            (H >= 32) & (H <= 98) & (S >= 28) & (S <= 145) & (V >= 45) & (V <= 158) & (b >= 42)
        )
        arena_u8 = cv2.morphologyEx(arena_raw.astype(np.uint8) * 255, cv2.MORPH_OPEN, np.ones((9, 9), np.uint8))
        arena_u8 = cv2.morphologyEx(arena_u8, cv2.MORPH_CLOSE, np.ones((35, 35), np.uint8))
        count, labels, stats, centers = cv2.connectedComponentsWithStats(arena_u8)
        if count < 2:
            return None
        best_l = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        if stats[best_l, cv2.CC_STAT_AREA] < 25000:
            return None
        pts = np.column_stack(np.where(labels == best_l))[:, ::-1]
        hull_arena = cv2.convexHull(pts)
        arena_mask = np.zeros_like(arena_u8)
        cv2.fillConvexPoly(arena_mask, hull_arena, 255)
        arena_inner = cv2.erode(arena_mask, np.ones((21, 21), np.uint8))
        cx, cy = centers[best_l]

        blur_a = cv2.GaussianBlur(A, (31, 31), 0)
        blur_rg = cv2.GaussianBlur(rg, (31, 31), 0)
        diff_a = A - blur_a
        diff_rg = rg - blur_rg
        translucent_red = (
            (diff_a >= 2.5) & (diff_rg >= 6.0) &
            (A >= med_a + 4.2) & (rg >= med_rg + 13.0) &
            (V >= 42) & (V <= 158)
        )
        if not is_cave:
            translucent_red &= (b >= 44)
        vivid_red = (
            (diff_a >= 10.0) & (diff_rg >= 24.0) &
            (A >= med_a + 18.0) & (rg >= 35.0) &
            (((H <= 14) | (H >= 163)) & (S >= 88) & (V >= 85))
        )
        red_line = (translucent_red | vivid_red) & (arena_inner > 0)
        red_u8 = cv2.morphologyEx(red_line.astype(np.uint8) * 255, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
        lines = cv2.HoughLinesP(red_u8, 1, np.pi / 180, 24, minLineLength=38, maxLineGap=16)

        inward_paint = np.zeros_like(red_u8)
        segments = []
        if lines is not None:
            for x1, y1, x2, y2 in lines.reshape(-1, 4):
                dx, dy = x2 - x1, y2 - y1
                length = float(np.hypot(dx, dy))
                slope = abs(dy) / max(1, abs(dx))
                if not (0.38 <= slope <= 0.96):
                    continue
                nx, ny = -dy / length, dx / length
                ts = np.linspace(0.15, 0.85, 7)
                mx, my = x1 + ts * dx, y1 + ts * dy
                p_pos_x = np.clip(np.rint(mx + 14 * nx).astype(int), 0, w - 1)
                p_pos_y = np.clip(np.rint(my + 14 * ny).astype(int), 0, h - 1)
                p_neg_x = np.clip(np.rint(mx - 14 * nx).astype(int), 0, w - 1)
                p_neg_y = np.clip(np.rint(my - 14 * ny).astype(int), 0, h - 1)
                v_pos = float(np.median(V[p_pos_y, p_pos_x]))
                v_neg = float(np.median(V[p_neg_y, p_neg_x]))
                a_pos = float(np.median(A[p_pos_y, p_pos_x]))
                a_neg = float(np.median(A[p_neg_y, p_neg_x]))
                if max(v_pos, v_neg) > 156 and max(a_pos, a_neg) < med_a + 18:
                    continue
                rg_pos = float(np.median(rg[p_pos_y, p_pos_x]))
                rg_neg = float(np.median(rg[p_neg_y, p_neg_x]))
                if abs(rg_pos - rg_neg) >= 1.5:
                    in_nx, in_ny = (nx, ny) if rg_pos > rg_neg else (-nx, -ny)
                else:
                    mid_x, mid_y = 0.5 * (x1 + x2), 0.5 * (y1 + y2)
                    dot = (cx - mid_x) * nx + (cy - mid_y) * ny
                    in_nx, in_ny = (nx, ny) if dot >= 0 else (-nx, -ny)
                out_nx, out_ny = -in_nx, -in_ny
                segments.append((
                    np.array((x1, y1), dtype=float),
                    np.array((x2, y2), dtype=float),
                    length,
                    np.array((out_nx, out_ny), dtype=float),
                    np.array((in_nx, in_ny), dtype=float),
                ))
                quad = np.array([
                    [x1, y1],
                    [x2, y2],
                    [x2 + 52 * in_nx, y2 + 52 * in_ny],
                    [x1 + 52 * in_nx, y1 + 52 * in_ny],
                ], dtype=np.int32)
                cv2.fillConvexPoly(inward_paint, quad, 255)

        edges = cv2.Canny(gray, 55, 130)
        edge_density = cv2.boxFilter((edges > 0).astype(np.float32), -1, (19, 19))
        if is_cave:
            terrain_color = (H >= 92) & (H <= 128) & (S >= 35) & (S <= 138) & (V >= 42) & (V <= 150) & (rg <= -5)
        else:
            terrain_color = (H >= 58) & (H <= 96) & (S >= 35) & (S <= 135) & (V >= 48) & (V <= 152) & (b >= 45) & (rg <= -16)

        clean_ground = (arena_inner > 0) & terrain_color & (edge_density <= 0.085) & (inward_paint == 0) & (red_u8 == 0)
        forbidden = ((arena_inner > 0) & (~clean_ground)).astype(np.uint8) * 255
        forbidden = cv2.morphologyEx(forbidden, cv2.MORPH_CLOSE, np.ones((17, 17), np.uint8))
        valid_space = ((arena_inner > 0) & (forbidden == 0)).astype(np.uint8)
        dist_from_forbidden = cv2.distanceTransform(valid_space, cv2.DIST_L2, 5)
        safe_mask_roi = (dist_from_forbidden >= 12) & (arena_inner > 0)

        full_safe = np.zeros((900, 1600), dtype=bool)
        full_safe[y:y+h, x:x+w] = safe_mask_roi
        full_safe[:115, :] = False
        full_safe[696:, :] = False
        full_safe[:, :160] = False
        full_safe[:, 1441:] = False
        full_safe[580:676, :761] = False
        full_safe[600:, 1280:] = False
        if (orig_h, orig_w) != (900, 1600):
            self.safe_mask = cv2.resize(full_safe.astype(np.uint8), (orig_w, orig_h), interpolation=cv2.INTER_NEAREST).astype(bool)
        else:
            self.safe_mask = full_safe

        self.last_battlefield_info = {
            "is_cave": is_cave,
            "segments": len(segments),
        }

        return {
            "is_cave": is_cave,
            "work": work,
            "sx": sx,
            "sy": sy,
            "arena_inner": arena_inner,
            "center": (float(cx), float(cy)),
            "segments": segments,
            "edges": edges,
            "edge_density": edge_density,
            "terrain_color": terrain_color,
            "V": V,
            "A": A,
            "med_a": med_a,
            "dist_from_forbidden": dist_from_forbidden,
            "safe_mask_roi": safe_mask_roi,
        }

    def entry(self, screen):
        """Rank visible approaches along the red deployment boundary across Cave and Grass stages."""
        info = self.analyze_battlefield(screen)
        if not info:
            return None
        x, y, w, h = self.FIELD
        segments = info["segments"]
        if len(segments) < 2:
            return None
        edges = info["edges"]
        dist_from_forbidden = info["dist_from_forbidden"]
        safe_mask_roi = info["safe_mask_roi"]
        sx, sy = info["sx"], info["sy"]

        walls = []
        base = getattr(self, "base", None)
        vision = getattr(self, "vision", None)
        if base is not None and vision is not None and hasattr(base, "LOCAL") and hasattr(vision, "match_all"):
            walls = [
                (material, np.array((wx - x, wy - y)))
                for material in ("stone_wall", "wood_wall", "pink_wall")
                for wx, wy, *_ in vision.match_all(
                    info["work"], str(base.LOCAL / f"{material}.png"), .88,
                    roi=self.FIELD, min_distance=23, max_matches=20
                )
            ]

        candidates = []
        for start, end, length, outward, inward in segments:
            direction = (end - start) / length
            for share in (.28, .5, .72):
                mid = start + share * (end - start)
                for out_dist in (26, 34, 42):
                    px, py = (int(round(v)) for v in mid + out_dist * outward)
                    if not (30 < px < w - 30 and 28 < py < h - 28):
                        continue
                    if not safe_mask_roi[py, px]:
                        continue
                    full_pt = (int(round((px + x) * sx)), int(round((py + y) * sy)))
                    if (sx, sy) == (1.0, 1.0) and not self.entry_allowed(full_pt):
                        continue
                    lat_ok = sum(
                        0 <= int(round(py + lat * direction[1])) < h and
                        0 <= int(round(px + lat * direction[0])) < w and
                        bool(safe_mask_roi[int(round(py + lat * direction[1])), int(round(px + lat * direction[0]))])
                        for lat in (-25, -15, 0, 15, 25)
                    )
                    if lat_ok < 2:
                        continue
                    tx, ty = (int(round(v)) for v in mid + 65 * inward)
                    if not (25 < tx < w - 25 and 25 < ty < h - 25):
                        continue
                    texture = float(np.mean(edges[ty-24:ty+24, tx-24:tx+24])) / 255.0
                    wall_distance, material, wall = min(
                        ((float(np.linalg.norm(wall - (tx, ty))), material, wall) for material, wall in walls),
                        key=lambda item: item[0], default=(180.0, None, None)
                    )
                    target = (
                        (material, int(round((wall[0] + x) * sx)), int(round((wall[1] + y) * sy)))
                        if wall is not None and wall_distance <= 65 else None
                    )
                    clearance_bonus = min(float(dist_from_forbidden[py, px]), 40.0) / 40.0
                    score = (
                        min(length, 250.0) / 250.0 +
                        2.0 * texture +
                        max(0.0, 1.0 - wall_distance / 140.0) +
                        0.35 * clearance_bonus +
                        0.12 * lat_ok
                    )
                    candidates.append((score, full_pt, direction, inward, target))
        if not candidates:
            return None
        _, point, direction, inward, target = max(candidates, key=lambda item: item[0])
        return point, direction, inward, target

    def terrain_entry(self, screen):
        """When the red outline has faded on either stage, pick safe outer tiles beyond the 1-tile building margin."""
        info = self.analyze_battlefield(screen)
        if not info:
            return None
        x, y, w, h = self.FIELD
        arena_inner = info["arena_inner"]
        cx, cy = info["center"]
        edges = info["edges"]
        edge_density = info["edge_density"]
        terrain_color = info["terrain_color"]
        V = info["V"]
        A = info["A"]
        med_a = info["med_a"]
        sx, sy = info["sx"], info["sy"]

        flat_tile = (arena_inner > 0) & terrain_color & (edge_density <= 0.065)
        raw_struct = (arena_inner > 0) & (~flat_tile)
        structures = cv2.morphologyEx(raw_struct.astype(np.uint8) * 255, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
        n_comp, comp_labels, comp_stats, _ = cv2.connectedComponentsWithStats(structures)
        filtered_struct = np.zeros_like(structures)
        for idx in range(1, n_comp):
            if comp_stats[idx, cv2.CC_STAT_AREA] >= 260:
                filtered_struct[comp_labels == idx] = 255
        structures = cv2.morphologyEx(filtered_struct, cv2.MORPH_CLOSE, np.ones((25, 25), np.uint8))
        arena_clearance = cv2.distanceTransform((arena_inner > 0).astype(np.uint8), cv2.DIST_L2, 5)

        has_structures = int(np.count_nonzero(structures)) >= 900
        if has_structures:
            dist_struct = cv2.distanceTransform((structures == 0).astype(np.uint8), cv2.DIST_L2, 5)
            safe_roi = (dist_struct >= 36) & (arena_clearance >= 14) & (arena_inner > 0)
            if (sx, sy) == (1.0, 1.0) and isinstance(getattr(self, "safe_mask", None), np.ndarray):
                if int(np.count_nonzero(safe_roi)) >= 1200:
                    self.safe_mask[y:y+h, x:x+w] &= safe_roi
        else:
            dist_struct = None

        # Multi-tier search: prefer 40..96px from structures, then 20..125px, then classic arena perimeter
        tiers = ((40.0, 96.0, 18.0), (20.0, 125.0, 15.0), (0.0, 999.0, 20.0)) if has_structures else ((0.0, 999.0, 20.0),)
        for min_struct, max_struct, min_clear in tiers:
            points = []
            for px in range(max(225 - x, 55), min(1370 - x, w - 55), 24):
                for py in range(max(145 - y, 55), min(560 - y, h - 55), 24):
                    distance = float(np.hypot(px - cx, py - cy))
                    if not 195 <= distance <= 430 or arena_clearance[py, px] < min_clear:
                        continue
                    if has_structures and not (min_struct <= dist_struct[py, px] <= max_struct):
                        continue
                    full_pt = (int(round((px + x) * sx)), int(round((py + y) * sy)))
                    if (sx, sy) == (1.0, 1.0) and not self.entry_allowed(full_pt):
                        continue
                    patch = edges[py-20:py+21, px-20:px+21]
                    if np.mean(patch > 0) > .065:
                        continue
                    inward = np.array(((cx - px) / distance, (cy - py) / distance))
                    direction = np.array((-inward[1], inward[0]))
                    tx, ty = np.rint(np.array((px, py)) + 90 * inward).astype(int)
                    if not (26 <= tx < w - 26 and 26 <= ty < h - 26):
                        continue
                    activity = float(np.mean(edges[ty-25:ty+26, tx-25:tx+26] > 0))
                    struct_bonus = min(float(dist_struct[py, px]), 70.0) / 350.0 if has_structures else 0.0
                    score = activity + min(float(arena_clearance[py, px]), 90.0) / 600.0 + struct_bonus - abs(distance - 255.0) / 1000.0
                    points.append((score, full_pt, direction, inward, None))
            if points:
                return max(points, key=lambda item: item[0])[1:]
        return None

    def reveal_boundary(self, screen, cards=None):
        # Pick a colored, high-detail object near the center of the battlefield.
        x, y = 530, 200
        field = screen[y:550, x:1070]
        hsv = cv2.cvtColor(field, cv2.COLOR_BGR2HSV)
        structures = cv2.inRange(hsv, (5, 95, 115), (36, 255, 255))
        detail = cv2.Canny(cv2.cvtColor(field, cv2.COLOR_BGR2GRAY), 55, 135)
        score = cv2.boxFilter((structures > 0).astype(np.float32), -1, (49, 49))
        score *= cv2.boxFilter((detail > 0).astype(np.float32), -1, (49, 49))
        _, peak, _, point = cv2.minMaxLoc(score)
        if peak >= .025:
            self.base.tap((x+point[0], y+point[1]), .35)
            return self.adb.screencap()
        return screen

    def _format_approach_log(self, approach, source):
        point, _, inward, _ = approach
        info = getattr(self, "last_battlefield_info", None) or {}
        terrain = "Hang Đá" if info.get("is_cave") else "Rừng Cỏ"
        segs = info.get("segments", 0)
        inv = np.asarray(inward, dtype=float)
        return f"nguồn={source}, nền={terrain}, đoạn_đỏ={segs}, neo={point}, hướng_vào=({inv[0]:+.2f},{inv[1]:+.2f})"

    def _is_dispatched(self, card_x):
        dispatched = getattr(self, "stage_dispatched", None)
        if not isinstance(dispatched, set):
            return False
        return any(abs(x - card_x) < 30 for x in dispatched)

    def _mark_dispatched(self, troop, dead=False):
        if not isinstance(getattr(self, "stage_dispatched", None), set):
            self.stage_dispatched = set()
        if not isinstance(getattr(self, "empty_stacks", None), set):
            self.empty_stacks = set()
        if not isinstance(getattr(self, "troops", None), list):
            self.troops = []
        self.stage_dispatched.add(troop.card[0])
        self.empty_stacks.add(troop.card[0])
        if not dead and not any(abs(t.card[0] - troop.card[0]) < 25 for t in self.troops):
            self.troops.append(troop)

    def _probe_card_in_dock(self, screen, card):
        """For a card with no remaining ability (charge < 0.15), tap once to test if it is still undeployed in the dock."""
        if screen is None or not isinstance(screen, np.ndarray) or screen.shape[:2] != (900, 1600):
            return False
        cx, cy = card
        y1, y2 = max(0, cy - 96), max(1, cy - 64)
        x1, x2 = max(0, cx - 35), min(1600, cx + 35)
        before = screen[y1:y2, x1:x2]
        if not before.size:
            return False
        self.base.tap(card, .14)
        after_scr = self.adb.screencap()
        if after_scr is None or not isinstance(after_scr, np.ndarray) or after_scr.shape[:2] != (900, 1600):
            return False
        after = after_scr[y1:y2, x1:x2]
        diff = float(np.mean(np.abs(after.astype(np.int16) - before.astype(np.int16))))
        return diff >= 9.0

    def deploy(self, screen, log):
        threshold = .72 if self.stage == 2 else .80
        cards = self.cards(screen, threshold=threshold)
        if not cards:
            self.deploy_notice(log, "chưa nhận diện được thanh quân")
            return False
        approach_source = "giữ_nguyên" if self.approach is not None else None
        approach = self.approach or self.entry(screen)
        if approach and approach_source is None:
            approach_source = "viền_đỏ"
        if not approach:
            revealed = getattr(self, "revealed_for_stage", False) or (
                self.stage == 2 and getattr(self, "revealed_stage_two", False)
            )
            if not revealed:
                self.revealed_for_stage = True
                if self.stage == 2:
                    self.revealed_stage_two = True
                fresh = self.reveal_boundary(screen, cards=cards)
                if fresh is not None and self.phase(fresh) in ("scouting", "combat"):
                    screen = fresh
                    approach = self.entry(screen)
                    if approach:
                        approach_source = "reveal_viền_đỏ"
            if not approach:
                approach = self.terrain_entry(screen)
                if approach:
                    approach_source = "địa_hình"
        if not approach and self.rejected_entries:
            approach = self.terrain_entry(screen)
            if approach:
                approach_source = "địa_hình_sau_từ_chối"
            else:
                self.rejected_entries.clear()
                approach = self.entry(screen) or self.terrain_entry(screen)
                if approach:
                    approach_source = "quét_lại_toàn_bản_đồ"
        if not approach:
            self.deploy_notice(log, "chưa thấy đủ đường biên thả quân")
            if not self.zoomed_for_stage:
                self.zoomed_for_stage = True
                self.adb.zoom_out_map(times=1)
            self.pan_map_for_deployment(log, cards)
            return False
        point, direction, inward, _ = approach
        first_wave = self.approach is None
        self.approach = approach
        if first_wave:
            log(f"[BH] Stage {self.stage}: {self._format_approach_log(approach, approach_source or 'auto')}.")
            log(f"[BH] Stage {self.stage}: quân chịu đòn/hero đi trước, Bomber và hỗ trợ theo sát.")
        complete = True
        accepted = False
        pending = []
        slot_summary = []
        tracked = {troop.card[0] for troop in self.troops}
        p = np.asarray(point, dtype=float)
        d = np.asarray(direction, dtype=float)
        inv = np.asarray(inward, dtype=float)
        # Use one confirmed frame for the wave. Verify the actual result before retrying.
        for index, (kind, card) in enumerate(cards):
            if any(abs(x-card[0]) < 30 for x in tracked | self.empty_stacks) or self._is_dispatched(card[0]):
                slot_summary.append(f"{kind}@{card[0]}=tracked")
                continue
            spacing = (0, -15, 15, -25, 25)[index % 5]
            depth = 9 if kind in self.FRONTLINE else 17
            raw_drop = tuple(int(round(value)) for value in p + spacing*d - depth*inv)
            drop = self.resolve_drop_point(raw_drop, p, d, inv, depth=depth)
            if drop != raw_drop:
                log(f"[BH] Stage {self.stage}: kéo điểm thả {kind}@{card[0]} {raw_drop}->{drop} (tránh vùng cấm).")
            troop = Troop(kind, card, time.monotonic())
            count = self.stack_count(screen, card)
            if kind in ("machine", "copter"):
                count = None
            had_hp = False
            if count is not None:
                slot_summary.append(f"{kind}@{card[0]}={count}x")
                if count == 0:
                    self._mark_dispatched(troop, dead=True)
                    continue
            else:
                state = self.slot_state(screen, troop)
                if state == "dead":
                    slot_summary.append(f"{kind}@{card[0]}=dead")
                    self._mark_dispatched(troop, dead=True)
                    continue
                if state == "deployed":
                    if self.stage == 2 and not self._is_dispatched(card[0]):
                        # Stage 1 survivors enter Stage 2 with their HP bar already visible in the dock.
                        had_hp = True
                        slot_summary.append(f"{kind}@{card[0]}=survivor_waiting")
                    else:
                        slot_summary.append(f"{kind}@{card[0]}=deployed")
                        self._mark_dispatched(troop)
                        continue
                else:
                    slot_summary.append(f"{kind}@{card[0]}={state}")
            pending.append((troop, count, drop, had_hp))

        if slot_summary and (first_wave or pending):
            log(f"[BH] Stage {self.stage}: đọc thanh quân ({len(cards)} ô): " + ", ".join(slot_summary))

        if first_wave and pending:
            # Protect the Bomber before committing the support wave.
            lead = next((item for item in pending if item[0].kind in self.FRONTLINE),
                        next((item for item in pending if item[0].kind != "bomber"), None))
            if lead is not None and any(item[0].kind == "bomber" for item in pending):
                troop, count, drop, had_hp = lead
                if count is None and not had_hp and self.deploy_survivor(troop, drop, direction, inward, log=log):
                    self._mark_dispatched(troop)
                    accepted = True
                    pending.remove(lead)
                else:
                    # Keep the cover unit first even if its first drop was not confirmed.
                    pending.remove(lead)
                    pending.insert(0, lead)

            if lead is not None:
                fresh = self.adb.screencap()
                phase = self.phase(fresh)
                if phase not in ("scouting", "combat") or (phase == "scouting" and self.combat_seen and self.stage == 1):
                    return False
                if phase == "combat":
                    self.observe_combat(fresh)
            sent_notes = []
            for idx_p, (troop, count, drop, _) in enumerate(pending):
                points = self.drop_candidates(drop, direction, inward)
                target = next(points, None)
                if target is None:
                    continue
                sent_notes.append(f"{troop.kind}@{troop.card[0]}->{target}({min(count or 1, 12)})")
                self.send_drop(troop.card, target, min(count or 1, 12), settle=.03, screen=screen if idx_p == 0 else None)
            if sent_notes:
                log(f"[BH] Stage {self.stage}: đã gửi đợt thả đầu ({len(sent_notes)} ô): " + ", ".join(sent_notes))

        if pending:
            screen = self.adb.screencap()
            phase = self.phase(screen)
            if phase not in ("scouting", "combat") or (first_wave and phase == "scouting" and self.combat_seen and self.stage == 1):
                return False
            if phase == "combat":
                self.observe_combat(screen)
        verify_notes = []
        for troop, count, drop, had_hp in pending:
            state = self.slot_state(screen, troop)
            after_count = None if troop.kind in ("machine", "copter") else self.stack_count(screen, troop.card)
            if state == "dead" and after_count in (None, 0):
                self._mark_dispatched(troop, dead=True)
                accepted = True
                verify_notes.append(f"{troop.kind}@{troop.card[0]}=dead")
                continue
            if had_hp:
                if not first_wave:
                    verify_notes.append(f"{troop.kind}@{troop.card[0]}=survivor_retry")
                    if self.deploy_survivor(troop, drop, direction, inward, log=log, allow_initial_hp=True):
                        self._mark_dispatched(troop)
                        accepted = True
                    else:
                        complete = False
                    continue
                # Stage 2 survivor already showed an HP bar before deployment.
                charge, _ = self.ability_hud(screen, troop)
                still_in_dock = False
                if charge < 0.15 and hasattr(self.base, "tap"):
                    still_in_dock = self._probe_card_in_dock(screen, troop.card)
                if still_in_dock:
                    verify_notes.append(f"{troop.kind}@{troop.card[0]}=survivor_retry")
                    if self.deploy_survivor(troop, drop, direction, inward, log=log, allow_initial_hp=True):
                        self._mark_dispatched(troop)
                        accepted = True
                    else:
                        complete = False
                elif phase == "combat" or accepted or not self.is_card_selected(screen, troop.card):
                    self._mark_dispatched(troop)
                    accepted = True
                    verify_notes.append(f"{troop.kind}@{troop.card[0]}=survivor_deployed")
                else:
                    verify_notes.append(f"{troop.kind}@{troop.card[0]}=survivor_unconfirmed")
                    complete = False
                continue
            if state == "deployed" and after_count in (None, 0):
                self._mark_dispatched(troop)
                accepted = True
                verify_notes.append(f"{troop.kind}@{troop.card[0]}=deployed")
                continue
            if count is not None and after_count == 0:
                self._mark_dispatched(troop)
                accepted = True
                verify_notes.append(f"{troop.kind}@{troop.card[0]}=0x")
                continue
            verify_notes.append(f"{troop.kind}@{troop.card[0]}=retry({state}/{after_count})")
            if after_count is None:
                if self.deploy_survivor(troop, drop, direction, inward, log=log):
                    self._mark_dispatched(troop)
                    accepted = True
                else:
                    complete = False
            elif self.deploy_stack(screen, troop.card, drop, direction, inward, log):
                self._mark_dispatched(troop)
                accepted = True
            else:
                complete = False
            screen = self.adb.screencap()
            phase = self.phase(screen)
            if phase not in ("scouting", "combat") or (phase == "scouting" and self.combat_seen and self.stage == 1):
                return False
        if verify_notes:
            log(f"[BH] Stage {self.stage}: xác minh sau thả: " + ", ".join(verify_notes))
        if not complete:
            if pending and not accepted:
                self.rejected_entries.append(point)
                self.rejected_entries = self.rejected_entries[-4:]
                self.approach = None
                self.safe_mask = None
                self.deploy_notice(log, "điểm thả chưa được game chấp nhận, chuyển sang hướng khác")
                alt = self.entry(screen) or self.terrain_entry(screen)
                if alt is not None:
                    self.approach = alt
                    log(f"[BH] Stage {self.stage}: đổi hướng thả dự phòng -> {self._format_approach_log(alt, 'đổi_hướng')}.")
                    return False
            else:
                self.deploy_notice(log, "còn ô quân chưa xác nhận thả hết")
            if not self.pan_map_for_deployment(log, cards):
                self.rejected_entries.append(point)
                self.rejected_entries = self.rejected_entries[-4:]
                self.approach = self.entry(screen) or self.terrain_entry(screen)
                if self.approach is not None:
                    log(f"[BH] Stage {self.stage}: hết lượt dịch bản đồ, xoay sang hướng khác -> {self._format_approach_log(self.approach, 'xoay_hướng')}.")
        return complete

    def pan_map_for_deployment(self, log, cards=None):
        """Expose the other half of the deployment edge after an unaccepted drop."""
        if self.map_pan_attempts >= 2:
            return False
        frame = self.adb.screencap()
        if frame is None:
            return False
        for _, card in (cards if cards is not None else self.cards(frame)):
            if self.is_card_selected(frame, card):
                self.base.tap(card, .15)
                frame = self.adb.screencap()
                if frame is None:
                    return False
                break
        start_y, end_y = ((370, 550) if self.map_pan_attempts == 0 else (550, 370))
        self.adb.swipe(1000, start_y, 1000, end_y, duration_ms=450, jitter=0)
        self.map_pan_attempts += 1
        self.approach = None
        self.safe_mask = None
        self.rejected_entries.clear()
        log(f"[BH] Stage {self.stage}: dịch bản đồ {'xuống' if end_y > start_y else 'lên'} để dò lại vùng thả quân.")
        return True

    def stack_count(self, screen, card):
        if screen is None:
            return None
        x, y = card
        crop = screen[max(0, y-92):y-35, max(0, x+4):min(screen.shape[1], x+60)]
        ocr = self.vision._get_ocr()
        if ocr is None or not crop.size:
            return None
        for image in (crop, cv2.resize(crop, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)):
            result = ocr(image, use_det=False)
            txts = getattr(result, "txts", None)
            if not isinstance(txts, (list, tuple)):
                continue
            badge_text = "".join(str(t) for t in txts).replace(" ", "")
            match = re.search(r"(?:(\d{1,2}|[oO])\s*[xX×]|[xX×]\s*(\d{1,2}|[oO]))", badge_text)
            if match:
                val = match.group(1) or match.group(2)
                return int(val.lower().replace("o", "0"))
        return None

    @staticmethod
    def battlefield_point(point):
        x, y = point
        if not (160 <= x <= 1440 and 115 <= y <= 695):
            return False
        # Leave room for End Battle, Boost Army/Heroes, and the damage panel.
        return not (580 <= y <= 675 and x <= 760 or y >= 600 and x >= 1280)

    def entry_allowed(self, point):
        return self.battlefield_point(point) and all(
            (point[0]-x)**2 + (point[1]-y)**2 >= 70**2 for x, y in getattr(self, "rejected_entries", ())
        )

    def is_safe_drop_point(self, point):
        if not self.battlefield_point(point):
            return False
        safe_mask = getattr(self, "safe_mask", None)
        if isinstance(safe_mask, np.ndarray):
            x, y = int(round(point[0])), int(round(point[1]))
            if 0 <= y < safe_mask.shape[0] and 0 <= x < safe_mask.shape[1]:
                return bool(safe_mask[y, x])
            return False
        return True

    def resolve_drop_point(self, drop, anchor, direction, inward, depth=12):
        if self.is_safe_drop_point(drop):
            return drop
        p = np.asarray(anchor, dtype=float)
        d = np.asarray(direction, dtype=float)
        inv = np.asarray(inward, dtype=float)
        target = np.asarray(drop, dtype=float)
        best = None
        best_dist = float("inf")
        for lat in (0, -10, 10, -20, 20, -30, 30):
            for out_shift in (depth, depth + 8, depth + 16, max(0, depth - 6), 0, 24):
                cand = tuple(int(round(value)) for value in p + lat * d - out_shift * inv)
                if self.is_safe_drop_point(cand):
                    dist = float(np.hypot(cand[0] - target[0], cand[1] - target[1]))
                    if dist < best_dist:
                        best_dist = dist
                        best = cand
        if best is not None:
            return best
        anchor_pt = tuple(int(round(value)) for value in p)
        return anchor_pt if self.is_safe_drop_point(anchor_pt) else drop

    def drop_candidates(self, drop, direction, inward):
        p = np.asarray(drop, dtype=float)
        d = np.asarray(direction, dtype=float)
        inv = np.asarray(inward, dtype=float)
        offsets = (
            (0, 0), (18, 15), (-18, 23), (36, 8), (-36, 8), (0, 35), (20, 45), (-20, 45),
            (0, 14), (12, 6), (-12, 6), (0, 24),
        )
        safe_points = []
        fallback_points = []
        seen = set()
        for lateral, outward in offsets:
            point = tuple(int(round(value)) for value in p + lateral*d - outward*inv)
            if point in seen or not self.battlefield_point(point):
                continue
            seen.add(point)
            fallback_points.append(point)
            if self.is_safe_drop_point(point):
                safe_points.append(point)
        for point in (safe_points if safe_points else fallback_points):
            yield point

    def is_card_selected(self, screen, card):
        """Check if a troop card is already raised/selected in the dock."""
        if screen is None or not isinstance(screen, np.ndarray) or screen.shape[:2] != (900, 1600):
            return False
        x, y = card
        crop = screen[695:708, max(0, x-30):min(1600, x+30)]
        return bool(crop.size and crop.mean() > 115)

    def send_drop(self, card, point, count=1, settle=.22, screen=None):
        # Keep selection and its first field tap together in one ordered ADB chunk.
        # If the card is already selected, avoid tapping it again which deselects it in CoC.
        selected = self.is_card_selected(screen, card) if screen is not None else False
        points = [point] * count if selected else [card] + [point] * count
        self.adb.tap_batch(points, jitter=0, interval_ms=140, delay_after=settle)

    @staticmethod
    def _detect_hp_bar(screen, troop):
        """Detect the horizontal health bar across both Stage 1 and Stage 2 card heights, ignoring the top-left shortcut badge."""
        if screen is None or not isinstance(screen, np.ndarray) or screen.ndim != 3:
            return False, 0.0
        cx, cy = troop.card
        hero = troop.kind in ("machine", "copter")
        y1, y2 = (max(680, cy - 95), min(775, cy - 20)) if hero else (max(738, cy - 75), min(802, cy - 16))
        left = max(0, cx - 42)
        mid = max(left + 1, cx - 6)
        right = min(screen.shape[1], cx + 42)
        if y2 <= y1 or right <= mid:
            return False, 0.0
        hsv = cv2.cvtColor(screen[y1:y2, left:right], cv2.COLOR_BGR2HSV)
        mid_off = mid - left
        hp_full = (((hsv[:, :, 0] <= 88) | (hsv[:, :, 0] >= 166)) & (hsv[:, :, 1] >= 100) & (hsv[:, :, 2] >= 105))
        hp_right = hp_full[:, mid_off:]
        dark_right = hsv[:, mid_off:, 2] <= 75
        row_ok = (np.mean(hp_full, axis=1) >= 0.12) & (np.mean(hp_right | dark_right, axis=1) >= 0.82)
        best_run = cur = 0
        for ok in row_ok:
            cur = cur + 1 if ok else 0
            if cur > best_run:
                best_run = cur
        if best_run < 5:
            return False, 0.0
        hp_rows = hp_full[row_ok]
        health = float(np.mean(np.count_nonzero(hp_rows, axis=0) >= max(3, len(hp_rows) // 2)))
        return True, health

    @staticmethod
    def slot_state(screen, troop):
        x, y = troop.card
        portrait = cv2.cvtColor(screen[max(0, y-13):y+21, max(0, x-25):x+25], cv2.COLOR_BGR2HSV)
        if not portrait.size or np.mean(portrait[:, :, 1] > 55) < .12:
            return "dead"
        has_hp, _ = BuilderBattle._detect_hp_bar(screen, troop)
        if has_hp:
            return "deployed"
        return "waiting"

    def deploy_survivor(self, troop, drop, direction, inward, log=None, allow_initial_hp=False):
        candidates = list(self.drop_candidates(drop, direction, inward))
        for idx_c, point in enumerate(candidates):
            fresh = self.adb.screencap()
            self.send_drop(troop.card, point, screen=fresh)
            fresh = self.adb.screencap()
            phase = self.phase(fresh)
            if phase not in ("scouting", "combat"):
                return False
            state = self.slot_state(fresh, troop)
            if allow_initial_hp:
                if state == "dead":
                    if log and idx_c > 0:
                        log(f"[BH] Stage {self.stage}: thả {troop.kind}@{troop.card[0]} thành công tại điểm dự phòng #{idx_c+1} {point} (dead).")
                    return True
                charge, _ = self.ability_hud(fresh, troop)
                still_in_dock = False
                if charge < 0.15 and hasattr(self.base, "tap"):
                    still_in_dock = self._probe_card_in_dock(fresh, troop.card)
                if not still_in_dock and (phase == "combat" or not self.is_card_selected(fresh, troop.card)):
                    if log and idx_c > 0:
                        log(f"[BH] Stage {self.stage}: thả {troop.kind}@{troop.card[0]} thành công tại điểm dự phòng #{idx_c+1} {point} (survivor).")
                    return True
            elif state != "waiting":
                if log and idx_c > 0:
                    log(f"[BH] Stage {self.stage}: thả {troop.kind}@{troop.card[0]} thành công tại điểm dự phòng #{idx_c+1} {point} ({state}).")
                return True
        if log:
            log(f"[BH] Stage {self.stage}: thả {troop.kind}@{troop.card[0]} thất bại qua {len(candidates)} điểm {candidates[:4]}.")
        return False

    def deploy_stack(self, screen, card, drop, direction, inward, log):
        remaining = self.stack_count(screen, card)
        if remaining is None:
            probe = Troop("", card, 0)
            return self.slot_state(screen, probe) in ("deployed", "dead")
        if remaining == 0:
            return True
        for _ in range(min(remaining, 12)):
            candidates = list(self.drop_candidates(drop, direction, inward))
            for idx_c, point in enumerate(candidates):
                self.send_drop(card, point)
                fresh = self.adb.screencap()
                if fresh is None:
                    return False
                count = self.stack_count(fresh, card)
                if count is None:
                    probe = Troop("", card, 0)
                    st = self.slot_state(fresh, probe)
                    if st in ("deployed", "dead") or remaining <= 1:
                        remaining = 0 if st in ("deployed", "dead") or remaining <= 1 else remaining - 1
                        break
                    remaining -= 1
                    break
                if count > remaining:
                    return False
                if count < remaining:
                    remaining = count
                    if idx_c > 0:
                        log(f"[BH] Stage {self.stage}: ô @{card[0]} giảm còn {remaining}× tại điểm dự phòng #{idx_c+1} {point}.")
                    break
                if self.phase(fresh) not in ("scouting", "combat"):
                    return False
            else:
                log(f"[BH] Stage {self.stage}: ô @{card[0]} còn {remaining}× không giảm qua {len(candidates)} điểm {candidates[:4]}.")
                return False
            if remaining == 0:
                log(f"[BH] Stage {self.stage}: đã xác nhận ô quân tại x={card[0]} còn 0×.")
                return True
        return remaining == 0

    def deploy_notice(self, log, reason):
        now = time.monotonic()
        if not self.last_deploy_notice or now-self.last_deploy_notice >= 7:
            self.last_deploy_notice = now
            log(f"[BH] Stage {self.stage}: {reason}. Đang dò lại.")

    @staticmethod
    def ability_hud(screen, troop):
        """The deployed slot exposes charge in purple and health directly below it."""
        if screen is None or not isinstance(screen, np.ndarray) or screen.ndim != 3:
            return 0., 0.
        cx = troop.card[0]
        hero = troop.kind in ("machine", "copter")
        # Exclude the top-left shortcut badge (cx-48..cx-10) when measuring purple charge
        c_left = max(0, cx - 8)
        c_right = min(screen.shape[1], cx + 46)
        cy1, cy2 = (685, 745) if hero else (712, 778)
        charge_band = screen[cy1:cy2, c_left:c_right]
        fill = 0.0
        if charge_band.size:
            hsv_c = cv2.cvtColor(charge_band, cv2.COLOR_BGR2HSV)
            purple = cv2.inRange(hsv_c, (128, 88, 118), (170, 255, 255)) > 0
            win = 16 if hero else 22
            if purple.shape[0] >= win:
                best_row = max(
                    range(purple.shape[0] - win + 1),
                    key=lambda r: int(np.count_nonzero(purple[r:r + win])),
                )
                sub = purple[best_row:best_row + win]
                fill = float(np.mean(np.count_nonzero(sub, axis=0) >= (4 if hero else 10)))
            else:
                fill = float(np.mean(np.count_nonzero(purple, axis=0) >= (4 if hero else 10)))

        has_hp, health = BuilderBattle._detect_hp_bar(screen, troop)
        if not has_hp:
            health = 0.0
        return float(fill), float(health)

    def detect_anti_bat_threats(self, screen):
        """Detect live Roasters and Air Bombs on screen for Night Witch tactical abilities."""
        threats = {"air_bombs": [], "roasters": []}
        if screen is None or not isinstance(screen, np.ndarray) or screen.shape[:2] != (900, 1600):
            return threats

        rx0, ry0, rx1, ry1 = 260, 130, 1340, 670
        field = screen[ry0:ry1, rx0:rx1]
        hsv = cv2.cvtColor(field, cv2.COLOR_BGR2HSV)
        blue_mask = cv2.inRange(hsv, (110, 115, 125), (126, 255, 255))
        n_lbl, _, stats, centroids = cv2.connectedComponentsWithStats(blue_mask, connectivity=8)
        for lbl in range(1, n_lbl):
            area = int(stats[lbl, cv2.CC_STAT_AREA])
            if not (140 <= area <= 2600):
                continue
            bx, by, bw, bh = (
                int(stats[lbl, cv2.CC_STAT_LEFT]),
                int(stats[lbl, cv2.CC_STAT_TOP]),
                int(stats[lbl, cv2.CC_STAT_WIDTH]),
                int(stats[lbl, cv2.CC_STAT_HEIGHT]),
            )
            if bw > 95 or bh > 95 or bw < 10 or bh < 10:
                continue
            y_sub1 = min(field.shape[0], by + bh)
            y_sub2 = min(field.shape[0], y_sub1 + 45)
            x_sub1 = max(0, bx - 16)
            x_sub2 = min(field.shape[1], bx + bw + 16)
            if y_sub2 <= y_sub1 or x_sub2 <= x_sub1:
                continue
            sub_hsv = hsv[y_sub1:y_sub2, x_sub1:x_sub2]
            purple_launcher = cv2.inRange(sub_hsv, (127, 65, 75), (158, 255, 255))
            if int(np.count_nonzero(purple_launcher)) >= 120:
                cx = rx0 + int(round(centroids[lbl][0]))
                cy = ry0 + int(round(centroids[lbl][1]))
                if all(abs(cx - px) > 45 or abs(cy - py) > 45 for px, py in threats["air_bombs"]):
                    threats["air_bombs"].append((cx, cy))

        excluded = []
        match_fn = getattr(self.base, "match", None)
        if callable(match_fn):
            for _ in range(2):
                hit = match_fn(
                    screen,
                    ("roaster_1", "roaster_2"),
                    (260, 140, 1080, 520),
                    0.72,
                    (0.85, 0.95, 1.0, 1.05, 1.15),
                    excluded=excluded,
                )
                if (
                    isinstance(hit, (tuple, list))
                    and len(hit) >= 2
                    and isinstance(hit[0], (int, float, np.integer, np.floating))
                    and isinstance(hit[1], (int, float, np.integer, np.floating))
                ):
                    pt = (int(hit[0]), int(hit[1]))
                    threats["roasters"].append(pt)
                    excluded.append(pt)
                else:
                    break

        if not threats["roasters"]:
            flame = cv2.inRange(hsv, (10, 155, 205), (29, 255, 255))
            n_f, _, f_stats, f_cent = cv2.connectedComponentsWithStats(flame, connectivity=8)
            for lbl in range(1, n_f):
                area = int(f_stats[lbl, cv2.CC_STAT_AREA])
                if not (12 <= area <= 360):
                    continue
                fx, fy, fw, fh = (
                    int(f_stats[lbl, cv2.CC_STAT_LEFT]),
                    int(f_stats[lbl, cv2.CC_STAT_TOP]),
                    int(f_stats[lbl, cv2.CC_STAT_WIDTH]),
                    int(f_stats[lbl, cv2.CC_STAT_HEIGHT]),
                )
                if fw > 45 or fh > 45:
                    continue
                ny1, ny2 = max(0, fy - 24), min(field.shape[0], fy + fh + 42)
                nx1, nx2 = max(0, fx - 32), min(field.shape[1], fx + fw + 32)
                patch = hsv[ny1:ny2, nx1:nx2]
                drum = cv2.inRange(patch, (122, 45, 35), (168, 255, 130))
                red1 = cv2.inRange(patch, (0, 140, 150), (9, 255, 255))
                red2 = cv2.inRange(patch, (170, 140, 150), (179, 255, 255))
                red_rim = int(np.count_nonzero(red1)) + int(np.count_nonzero(red2))
                if int(np.count_nonzero(drum)) >= 280 and red_rim >= 80:
                    cx = rx0 + int(round(f_cent[lbl][0]))
                    cy = ry0 + int(round(f_cent[lbl][1]))
                    if all(abs(cx - px) > 45 or abs(cy - py) > 45 for px, py in threats["roasters"]):
                        threats["roasters"].append((cx, cy))

        return threats

    def use_abilities(self, screen, log):
        if screen is None:
            return
        now = time.monotonic()
        has_pending_nw = any(not t.used and t.kind == "night_witch" for t in self.troops)
        has_threats = False
        stars = 0
        if has_pending_nw:
            threats = self.detect_anti_bat_threats(screen)
            has_threats = bool(threats["air_bombs"] or threats["roasters"])
            sig = (self.stage, len(threats["air_bombs"]), len(threats["roasters"]))
            if sig != getattr(self, "last_threat_log", None):
                self.last_threat_log = sig
                log(
                    f"[BH] Stage {self.stage}: quét phòng không chống dơi -> "
                    f"Air Bombs={len(threats['air_bombs'])}, Roaster={len(threats['roasters'])}."
                )
            vs = getattr(self.base, "village_state", None)
            live = getattr(vs, "live_battle", None)
            if isinstance(live, dict) and isinstance(live.get("builder"), dict):
                raw_stars = live["builder"].get("stars", 0)
                if isinstance(raw_stars, (int, float)):
                    stars = int(raw_stars)

        for troop in self.troops:
            if troop.used:
                continue
            kind = troop.kind
            repeatable = kind in ("bomber", "machine", "copter")
            charge, health = self.ability_hud(screen, troop)
            if troop.pending_charge and charge < troop.pending_charge*.65:
                troop.pending_charge = 0.
                troop.attempts = 0
                troop.used = not repeatable
                log(f"[BH] Stage {self.stage}: đã xác nhận kỹ năng {kind} đổi trạng thái.")
                continue
            if troop.attempts >= 3 or (troop.attempts and now-troop.last_attempt < 1.7):
                continue
            age = now-troop.deployed
            if kind == "bomber":
                ready = charge >= .50 and age >= .45
            elif kind in ("machine", "copter"):
                ready = charge >= .25 and health >= .10 and age >= 4 and (
                    health < .48 or charge >= .83)
            elif kind in ("power_pekka", "boxer_giant"):
                ready = charge >= .50 and .08 <= health <= .72 and age >= 2
            elif kind == "night_witch":
                last_nw = getattr(self, "last_nw_ability_at", 0.0)
                since_nw = now - last_nw
                if charge < .50 or health < .08:
                    ready = False
                elif self.stage == 1:
                    emergency = health <= .36 and (health <= .25 or since_nw >= 1.2)
                    stall_rescue = (
                        age >= 18
                        and stars < 2
                        and health <= .62
                        and (not has_threats or (age >= 22 and stars == 0))
                        and since_nw >= 2.5
                    )
                    ready = emergency or stall_rescue
                    hold_set = getattr(self, "nw_hold_logged", None)
                    if not ready and age >= 4 and isinstance(hold_set, set) and troop.card[0] not in hold_set:
                        hold_set.add(troop.card[0])
                        log(
                            f"[BH] Stage 1: giữ kỹ năng night_witch@{troop.card[0]} cho Stage 2 "
                            f"(máu {health:.0%}, đe dọa dơi={'có' if has_threats else 'không'})."
                        )
                else:
                    if has_threats:
                        ready = (
                            (health <= .38 or (age >= 5 and (health <= .58 or age >= 11)))
                            and (since_nw >= 2.0 or health <= .25)
                        )
                    else:
                        ready = (
                            age >= 3
                            and (health <= .72 or age >= 6)
                            and (since_nw >= 1.0 or health <= .35)
                        )
            elif kind in ("baby_dragon", "drop_ship", "hog_glider"):
                ready = charge >= .50 and health >= .08 and age >= 3 and (health < .70 or age >= 7)
            elif kind in ("cannon_cart", "electrofire_wizard"):
                ready = charge >= .50 and .08 <= health < .76 and age >= 4
            else:
                ready = False
            if ready:
                self.base.tap(troop.card, .16)
                troop.last_attempt = now
                troop.attempts += 1
                troop.pending_charge = charge
                if kind == "night_witch":
                    self.last_nw_ability_at = now
                if kind in ("cannon_cart", "electrofire_wizard"):
                    troop.used = True
                log(f"[BH] Stage {self.stage}: bấm kỹ năng {kind}, máu ô quân khoảng {health:.0%}.")

    def obstruction_buttons(self, screen):
        title = self.base.match(screen, ("obstructed_title",), (205, 130, 1190, 440),
                                .86, (.45, .55, .65, .75, .85, 1., 1.1))
        if not title:
            # Newer game layouts use a wider warning title and larger buttons.
            title = self.base.match(screen, ("obstructed_title_alt",), (205, 130, 1190, 440),
                                    .82, (.75, .85, .95, 1.05, 1.07, 1.1, 1.2))
            if not title:
                return None
            attack = self.base.match(screen, ("obstructed_attack_alt",), (700, 420, 500, 300),
                                     .82, (.75, .85, .95, 1.05, 1.1, 1.2))
            cancel = self.base.match(screen, ("obstructed_cancel_alt",), (350, 420, 500, 300),
                                     .82, (.75, .85, .95, 1.05, 1.1, 1.2))
            if attack and cancel and cancel[0] < title[0] < attack[0] and self.base.green_button(screen, attack):
                return attack, cancel
            return None
        x, y, w, _ = title[:4]
        left = max(0, x-w//2-20)
        top = y + round(.23*w)
        roi = (left, top, min(1600-left, w+40), max(1, min(900-top, round(.32*w))))
        scale = w / 730
        scales = (scale*.95, scale, scale*1.05)
        attack = self.base.match(screen, ("obstructed_attack",), roi, .88, scales)
        cancel = self.base.match(screen, ("obstructed_cancel",), roi, .88, scales)
        if attack and cancel and cancel[0] < x < attack[0] and self.base.green_button(screen, attack):
            return attack, cancel
        return None

    def dismiss_star_bonus(self, screen, log):
        if screen is None:
            return False
        words = self.base.walls.texts(screen, (330, 115, 940, 465), scale=1)
        title = "".join(ch for ch in "".join(words).lower() if ch.isalnum())
        if "starbonus" not in title and not ("star" in title and "bonus" in title):
            return False
        okay = self.base.match(screen, ("okaystar", "okhome", "okay"), (375, 410, 850, 465),
                               .75, (.8, 1., 1.15))
        if not okay and hasattr(self.vision, 'find_okay_button'):
            okay = self.vision.find_okay_button(screen)
        if not okay:
            ocr_fn = getattr(self.base.walls, 'ocr_positions', None)
            if ocr_fn:
                for item in ocr_fn(screen, (600, 600, 400, 220), scale=1):
                    text = str(item[0]).lower()
                    if any(k in text for k in ('ok', 'claim', 'ciaim', 'nhan', 'nhận')):
                        bx = item[1] if isinstance(item[1], (int, float)) else (item[1][0] if isinstance(item[1], (list, tuple)) else 800)
                        by = item[2] if len(item) > 2 and isinstance(item[2], (int, float)) else 710
                        cand = (int(bx), int(by), 160, 70)
                        if self.base.green_button(screen, cand):
                            okay = cand
                            break
        if not okay:
            cand = (800, 700, 160, 70)
            if self.base.green_button(screen, cand):
                okay = cand
        if not okay or not self.base.green_button(screen, okay):
            return False
        self.base.tap(okay, .65)
        log("[BH] Đã xác nhận popup Star Bonus.")
        return True

    def result_home(self, screen, log):
        if self.dismiss_star_bonus(screen, log):
            return False
        return self.base.is_home(screen)

    def enable_speedup(self, screen, log):
        if self.speed_stage == self.stage:
            return
        now = time.monotonic()
        if now < self.next_speed_check_at:
            return
        self.next_speed_check_at = now + 2
        remaining = self.vision.battle_seconds_remaining(screen)
        if remaining is None or remaining > 60:
            return
        hit = self.base.match(screen, ("4x",), (0, 0, 1600, 900), .88, (.85, 1., 1.15))
        if hit and self.base.green_button(screen, hit):
            self.base.tap(hit, .15)
            self.speed_stage = self.stage
            log(f"[BH] Stage {self.stage}: còn {remaining}s, đã bấm 4×.")

    @staticmethod
    def count_result_stars(screen):
        """Read the three medal slots. Silver replaces bronze left to right."""
        if screen is None or screen.shape[:2] != (900, 1600):
            return None
        medals = []
        for x, y in ((620, 259), (800, 208), (977, 259)):
            patch = cv2.cvtColor(screen[y-8:y+9, x-8:x+9], cv2.COLOR_BGR2HSV)
            hue, saturation, value = np.median(patch, axis=(0, 1))
            if 5 <= hue <= 35 and saturation >= 110 and value >= 140:
                medals.append(1)
            elif 80 <= hue <= 125 and saturation <= 100 and value >= 190:
                medals.append(2)
            elif value < 155:
                medals.append(0)
            else:
                return None
        if medals != sorted(medals, reverse=True):
            return None
        silver = medals.count(2)
        return 3 + silver if silver else medals.count(1)

    def observe_combat(self, screen):
        self.combat_seen = True
        village_state = self.base.village_state
        if village_state is not None:
            if not village_state.live_battle['builder']:
                village_state.start_battle('builder', time.monotonic(), stage=self.stage)
            village_state.update_battle('builder', self.live_stars(screen), stage=self.stage)

    def live_stars(self, screen):
        """Count the earned stars visible in the Builder Battle HUD."""
        template_name = "1starbuilder_brown" if self.stage == 1 else "1starbuilder_white"
        template = self.vision.load_template(template_name)
        roi = screen[550:735, 1200:1595]
        previous_stage = 3 if self.stage == 2 else 0
        if not isinstance(template, np.ndarray) or any(a < b for a, b in zip(roi.shape, template.shape)):
            return previous_stage
        height, width = template.shape[:2]
        response = cv2.matchTemplate(roi, template, cv2.TM_CCOEFF_NORMED)
        hits = []
        for _ in range(3):
            _, score, _, (x, y) = cv2.minMaxLoc(response)
            if score < .84:
                break
            hits.append((x, y))
            response[max(0, y-height//2):y+height//2+1, max(0, x-width//2):x+width//2+1] = -1
        count = max((sum(abs(y-other_y) <= 5 and abs(x-other_x) <= 2.4*width
                         for other_x, other_y in hits) for x, y in hits), default=0)
        return previous_stage + count

    def result_button(self, screen):
        return self.base.match(screen, ("result_return_home", "return_home", "return_home1"),
                               (480, 605, 640, 270), .88, (.85, 1., 1.15))

    def settled_result(self, screen, button):
        stars = self.count_result_stars(screen)
        if stars == 6:
            return button, stars
        # The button can appear while bronze medals are still turning silver.
        time.sleep(1.5)
        final = self.adb.screencap()
        button = self.result_button(final)
        return button, self.count_result_stars(final) if button else None

    def run_backup_sweep(self, screen, log):
        """Rescan cards and deploy any remaining troops that were missed or partially dropped."""
        if not self.approach:
            self.approach = self.entry(screen) or self.terrain_entry(screen)
            if self.approach:
                log(f"[BH] Stage {self.stage}: sweep tìm được hướng thả -> {self._format_approach_log(self.approach, 'sweep')}.")
        if not self.approach:
            cards = self.cards(screen, threshold=.70 if self.stage == 2 else .78)
            if cards:
                log(f"[BH] Stage {self.stage}: sweep chưa có hướng thả ({len(cards)} ô quân), thử dịch bản đồ.")
                self.pan_map_for_deployment(log, cards)
            return
        point, direction, inward, _ = self.approach
        p = np.asarray(point, dtype=float)
        d = np.asarray(direction, dtype=float)
        inv = np.asarray(inward, dtype=float)
        threshold = .70 if self.stage == 2 else .78
        for sweep_round in range(2):
            cards = self.cards(screen, threshold=threshold)
            if not cards:
                break
            leftovers = []
            tracked_x = (
                {t.card[0] for t in getattr(self, "troops", ())}
                | set(getattr(self, "empty_stacks", ()))
                | set(getattr(self, "stage_dispatched", ()))
            )
            for index, (kind, card) in enumerate(cards):
                if any(abs(x - card[0]) < 30 for x in tracked_x) or self._is_dispatched(card[0]):
                    continue
                troop = Troop(kind, card, time.monotonic())
                count = None if kind in ("machine", "copter") else self.stack_count(screen, card)
                state = self.slot_state(screen, troop)
                if state == "dead":
                    if count in (None, 0):
                        self._mark_dispatched(troop, dead=True)
                    continue
                if count is not None and count > 0:
                    leftovers.append((troop, count, False))
                    if isinstance(getattr(self, "empty_stacks", None), set):
                        self.empty_stacks.discard(card[0])
                elif state == "deployed":
                    if self.stage == 2 and not self._is_dispatched(card[0]) and count in (None, 0):
                        leftovers.append((troop, 1, True))
                    else:
                        self._mark_dispatched(troop)
                    continue
                elif state == "waiting":
                    leftovers.append((troop, count or 1, False))
                    if isinstance(getattr(self, "empty_stacks", None), set):
                        self.empty_stacks.discard(card[0])
            if not leftovers:
                self.empty_sweep_observations += 1
                if self.empty_sweep_observations >= 2 and not self.empty_sweep_logged:
                    log(f"[BH] Stage {self.stage}: hai lượt quét liên tiếp chưa thấy lính chờ thả.")
                    self.empty_sweep_logged = True
                break
            self.empty_sweep_observations = 0
            self.empty_sweep_logged = False
            drop_targets = []
            for index, (troop, count, had_hp) in enumerate(leftovers):
                spacing = (0, -15, 15, -25, 25)[index % 5]
                depth = 9 if troop.kind in self.FRONTLINE else 17
                raw_drop = tuple(int(round(value)) for value in p + spacing*d - depth*inv)
                drop = self.resolve_drop_point(raw_drop, p, d, inv, depth=depth)
                candidates = list(self.drop_candidates(drop, direction, inward))
                drop_pt = candidates[min(sweep_round, len(candidates)-1)] if candidates else drop
                drop_targets.append((troop, count, had_hp, drop_pt))
            log(f"[BH] Stage {self.stage}: Vét thêm {len(leftovers)} ô lính còn sót (vòng {sweep_round+1}): " +
                ", ".join(f"{t.kind}@{t.card[0]}({c})->{pt}" for t, c, _, pt in drop_targets))
            for idx_t, (troop, count, had_hp, drop_pt) in enumerate(drop_targets):
                self.send_drop(troop.card, drop_pt, min(count, 8), settle=.05, screen=screen if idx_t == 0 else None)
            time.sleep(0.3)
            screen = self.adb.screencap()
            if screen is None:
                break
            phase_after = self.phase(screen) if any(item[2] for item in leftovers) else None
            unchanged = 0
            after_notes = []
            for troop, before, had_hp in leftovers:
                after = None if troop.kind in ("machine", "copter") else self.stack_count(screen, troop.card)
                state = self.slot_state(screen, troop)
                if had_hp:
                    charge, _ = self.ability_hud(screen, troop)
                    still_in_dock = False
                    if charge < 0.15 and hasattr(self.base, "tap"):
                        still_in_dock = self._probe_card_in_dock(screen, troop.card)
                    if state == "dead" or (
                        not still_in_dock and (phase_after == "combat" or not self.is_card_selected(screen, troop.card))
                    ):
                        self._mark_dispatched(troop, dead=(state == "dead"))
                        after_notes.append(f"{troop.kind}@{troop.card[0]}=survivor_ok")
                    else:
                        unchanged += 1
                        after_notes.append(f"{troop.kind}@{troop.card[0]}=survivor_retry")
                    continue
                deployed_now = state == 'deployed' and after in (None, 0)
                after_notes.append(f"{troop.kind}@{troop.card[0]}={state}/{after}")
                if after == 0 or state == 'dead' or deployed_now:
                    self._mark_dispatched(troop, dead=(state == 'dead'))
                elif after == before or (after is None and state == 'waiting'):
                    unchanged += 1
            log(f"[BH] Stage {self.stage}: kết quả vét vòng {sweep_round+1}: " + ", ".join(after_notes))
            if unchanged == len(leftovers):
                if self.pan_map_for_deployment(log, cards):
                    break
                rejected = getattr(self, "rejected_entries", None)
                if isinstance(rejected, list):
                    rejected.append(point)
                    self.rejected_entries = rejected[-4:]
                self.approach = None
                self.safe_mask = None
                alt = self.entry(screen) or self.terrain_entry(screen)
                if alt is not None:
                    self.approach = alt
                    point, direction, inward, _ = alt
                    p = np.asarray(point, dtype=float)
                    d = np.asarray(direction, dtype=float)
                    inv = np.asarray(inward, dtype=float)
                    log(f"[BH] Stage {self.stage}: vét không đổi, chuyển hướng mới -> {self._format_approach_log(alt, 'sweep_xoay')}.")
                else:
                    break

    def monitor(self, log, seconds=410):
        until = time.monotonic()+seconds
        obstruction_attempts = 0
        obstruction_last_tap = 0
        village_state = self.base.village_state
        while time.monotonic() < until:
            self.adb._guard_input()
            screen = self.adb.screencap()
            if screen is None:
                time.sleep(.6)
                continue
            if not self.combat_seen and not self.planned:
                obstruction = self.obstruction_buttons(screen)
                if obstruction:
                    attack, cancel = obstruction
                    if self.base.settings.get("builder_base_obstacle_attack", False):
                        now = time.monotonic()
                        if obstruction_attempts >= 3 and now - obstruction_last_tap >= 2:
                            self.base.tap(cancel, .55)
                            self.base.close_panel(self.adb.screencap())
                            self.in_progress = False
                            log("[BH] Cảnh báo chướng ngại vẫn còn sau 3 lần bấm Attack; đã hủy tìm trận để bot tiếp tục.")
                            return False
                        if obstruction_attempts < 3 and now - obstruction_last_tap >= 2:
                            self.base.tap(attack, .75)
                            obstruction_attempts += 1
                            obstruction_last_tap = time.monotonic()
                            log(f"[BH] Bố trí phòng thủ bị chướng ngại; bấm Attack lần {obstruction_attempts}/3, chờ game phản hồi.")
                        time.sleep(.6)
                        continue
                    self.base.tap(cancel, .55)
                    self.base.close_panel(self.adb.screencap())
                    self.in_progress = False
                    log("[BH] Đã hủy tìm trận do cảnh báo chướng ngại. Bật tùy chọn vẫn bấm Attack để đi tiếp.")
                    return False
            if self.vision.game_fault(screen):
                return False
            if self.base.is_home(screen):
                if time.monotonic()-self.match_requested < 12:
                    time.sleep(.6)
                    continue
                self.in_progress = False
                if village_state is not None:
                    village_state.clear_battle('builder')
                return True
            if not self.combat_seen and not self.planned and time.monotonic()-self.match_requested >= 45:
                if self.find_now(screen):
                    self.base.close_panel(screen)
                    self.in_progress = False
                    log("[BH] Tìm trận không tiến triển sau 45 giây; đã đóng bảng và sẽ thử lại ở lượt sau.")
                    return False
                if self.vision.has_open_dialog(screen):
                    close = self.vision.find_close_button(screen)
                    if close:
                        self.base.tap(close, .5)
                        log("[BH] Tìm trận bị chặn quá 45 giây; đã đóng hộp thoại chưa nhận diện và kiểm tra lại màn hình.")
                        self.match_requested = time.monotonic() - 40
            result = self.result_button(screen)
            phase = None if result else self.phase(screen)
            if result or phase:
                self.unknown_since = 0
            elif time.monotonic() - self.match_requested >= 45:
                if not self.unknown_since:
                    self.unknown_since = time.monotonic()
                elif time.monotonic() - self.unknown_since >= 60:
                    log('[BH] Không nhận diện được trạng thái trận trong 60 giây; chuyển sang phục hồi game.')
                    engine = getattr(self.base, 'engine', None)
                    if engine is not None:
                        engine._request_recovery('Trận làng đêm mất trạng thái')
                    return False
            if (result or phase) and not self.match_counted:
                self.match_counted = True
                if self.record_stat:
                    self.record_stat("builder_battles_started")
                if phase in ("scouting", "combat"):
                    log("[BH] Đã tìm thấy căn cứ đối thủ. Đợi 3s load intro trận đấu...")
                    time.sleep(3.0)
                    log(f"[BH] Stage {self.stage}: Thu nhỏ bản đồ làng đêm (zoom out) khi vào trận.")
                    self.adb.zoom_out_map(times=2)
                    time.sleep(0.35)
                    screen = self.adb.screencap()
                    self.zoomed_for_stage = True
            if result:
                if village_state is not None:
                    village_state.clear_battle('builder')
                if not self.result_counted:
                    result, stars = self.settled_result(screen, result)
                    if stars is not None and self.stage == 2:
                        stars = max(3, stars)
                    if self.record_stat:
                        self.record_stat("builder_battles_completed", stars=stars)
                    self.result_counted = True
                    log(f"[BH] Kết quả: {stars}/6 sao." if stars is not None
                        else "[BH] Chưa đọc rõ sao trên bảng kết quả.")
                    if not result:
                        continue
                self.base.tap(result, 1.)
                if self.base.wait_for(lambda frame: self.result_home(frame, log), 10) is not None:
                    self.in_progress = False
                    log("[BH] Đã về làng từ bảng kết quả.")
                    return True
                return False
            if phase is None and self.dismiss_star_bonus(screen, log):
                continue
            if phase == "scouting" and self.stage == 1 and self.combat_seen:
                self.stage = 2
                self.next_speed_check_at = 0
                if village_state is not None:
                    village_state.update_battle('builder', 3, stage=2)
                self.troops = []
                self.zoomed_for_stage = False
                self.revealed_for_stage = False
                self.safe_mask = None
                self.last_battlefield_info = None
                self.last_deploy_notice = 0
                self.planned = False
                self.approach = None
                self.rejected_entries.clear()
                self.sweep_pending = False
                self.empty_stacks.clear()
                self.stage_dispatched.clear()
                self.last_nw_ability_at = 0.0
                self.last_threat_log = None
                self.nw_hold_logged.clear()
                self.map_pan_attempts = 0
                self.empty_sweep_observations = 0
                self.empty_sweep_logged = False
                self.unknown_since = 0
                log("[BH] Đã sang Stage 2, dò đội quân sống sót và quân tiếp viện.")
                time.sleep(1.5)
                log(f"[BH] Stage 2: Thu nhỏ bản đồ làng đêm (zoom out).")
                self.adb.zoom_out_map(times=2)
                time.sleep(0.35)
                screen = self.adb.screencap()
                self.zoomed_for_stage = True
            if phase == "combat":
                self.observe_combat(screen)
                self.enable_speedup(screen, log)
                now_combat = time.monotonic()
                if now_combat - self.last_sweep_at >= 3.5:
                    self.last_sweep_at = now_combat
                    self.run_backup_sweep(screen, log)
                self.use_abilities(screen, log)
            if phase in ("scouting", "combat") and not self.planned:
                self.planned = self.deploy(screen, log)
                self.sweep_pending = self.planned
                if self.planned:
                    fresh = self.adb.screencap()
                    self.run_backup_sweep(fresh, log)
            elif phase == "combat" and self.sweep_pending:
                self.sweep_pending = False
                self.planned = self.deploy(screen, log)
                fresh = self.adb.screencap()
                self.run_backup_sweep(fresh, log)
            time.sleep(.65)
        log("[BH] Hết thời gian quan sát. Giữ quyền xử lý màn hình BH để kiểm tra lại.")
        return False
