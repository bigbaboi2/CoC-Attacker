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
        self.sweep_pending = False
        self.empty_stacks = set()
        self.combat_seen = False
        self.match_requested = 0
        self.zoomed_for_stage = False
        self.last_deploy_notice = 0
        self.revealed_stage_two = False
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
        self.zoomed_for_stage = False
        self.last_deploy_notice = 0
        self.planned = False
        self.approach = None
        self.rejected_entries = []
        self.speed_stage = 0
        self.sweep_pending = False
        self.empty_stacks = set()
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

    def entry(self, screen):
        """Rank visible approaches along the red deployment boundary."""
        x, y, w, h = self.FIELD
        scene = screen[y:y+h, x:x+w]
        hsv = cv2.cvtColor(scene, cv2.COLOR_BGR2HSV)
        red = (cv2.inRange(hsv, (0, 88, 90), (11, 255, 255)) |
               cv2.inRange(hsv, (169, 88, 90), (179, 255, 255)))
        lines = cv2.HoughLinesP(red, 1, np.pi/180, 33, minLineLength=53, maxLineGap=15)
        if lines is None:
            return None
        segments = []
        for x1, y1, x2, y2 in lines.reshape(-1, 4):
            dx, dy = x2-x1, y2-y1
            length = np.hypot(dx, dy)
            if .30 < abs(dy)/max(1, abs(dx)) < .85:
                segments.append((np.array((x1, y1)), np.array((x2, y2)), length))
        if len(segments) < 2:
            return None
        hull = cv2.convexHull(np.int32([p for start, end, _ in segments for p in (start, end)]))
        if cv2.contourArea(hull) < 16000:
            return None
        edges = cv2.Canny(cv2.cvtColor(scene, cv2.COLOR_BGR2GRAY), 60, 130)
        walls = [
            (material, np.array((wx-x, wy-y)))
            for material in ("stone_wall", "wood_wall", "pink_wall")
            for wx, wy, *_ in self.vision.match_all(
                screen, str(self.base.LOCAL / f"{material}.png"), .88,
                roi=self.FIELD, min_distance=23, max_matches=20
            )
        ]
        candidates = []
        for start, end, length in segments:
            direction = (end-start)/length
            normal = np.array((-direction[1], direction[0]))
            for share in (.28, .5, .72):
                mid = start + share*(end-start)
                for outward in (normal, -normal):
                    point = tuple(int(v) for v in mid+23*outward)
                    px, py = point
                    if not (30 < px < w-30 and 28 < py < h-28):
                        continue
                    if not self.entry_allowed((px+x, py+y)):
                        continue
                    if cv2.pointPolygonTest(hull, point, False) >= 0:
                        continue
                    tx, ty = (int(v) for v in mid-65*outward)
                    if not (25 < tx < w-25 and 25 < ty < h-25):
                        continue
                    if cv2.pointPolygonTest(hull, (tx, ty), False) < 0:
                        continue
                    if np.median(hsv[py-7:py+8, px-7:px+8, 2]) < 52:
                        continue
                    texture = np.mean(edges[ty-24:ty+24, tx-24:tx+24])/255
                    wall_distance, material, wall = min(
                        ((np.linalg.norm(wall-(tx, ty)), material, wall) for material, wall in walls),
                        key=lambda item: item[0], default=(180, None, None))
                    target = (material, int(wall[0])+x, int(wall[1])+y) if wall_distance <= 65 else None
                    score = min(length, 250)/250 + 2*texture + max(0, 1-wall_distance/140)
                    candidates.append((score, (px+x, py+y), direction, -outward, target))
        if not candidates:
            return None
        _, point, direction, inward, target = max(candidates, key=lambda item: item[0])
        return point, direction, inward, target

    def terrain_entry(self, screen):
        """Stage 2 can show no red outline. Use clear tiles around the visible arena."""
        x, y, w, h = self.FIELD
        scene = screen[y:y+h, x:x+w]
        hsv = cv2.cvtColor(scene, cv2.COLOR_BGR2HSV)
        tiles = cv2.inRange(hsv, (72, 34, 45), (111, 150, 160))
        tiles = cv2.morphologyEx(tiles, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
        count, labels, stats, centers = cv2.connectedComponentsWithStats(tiles)
        if count < 2:
            return None
        label = 1 + np.argmax(stats[1:, cv2.CC_STAT_AREA])
        if stats[label, cv2.CC_STAT_AREA] < 65000:
            return None
        arena = (labels == label).astype(np.uint8)
        clearance = cv2.distanceTransform(arena, cv2.DIST_L2, 5)
        cx, cy = centers[label]
        gray = cv2.cvtColor(scene, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 65, 145)
        points = []
        for px in range(max(225-x, 55), min(1370-x, w-55), 24):
            for py in range(max(145-y, 55), min(560-y, h-55), 24):
                distance = np.hypot(px-cx, py-cy)
                if not 195 <= distance <= 405 or clearance[py, px] < 33:
                    continue
                if not self.entry_allowed((px+x, py+y)):
                    continue
                patch = edges[py-20:py+21, px-20:px+21]
                if np.mean(patch > 0) > .06:
                    continue
                inward = np.array(((cx-px)/distance, (cy-py)/distance))
                tx, ty = np.rint(np.array((px, py))+90*inward).astype(int)
                activity = np.mean(edges[ty-25:ty+26, tx-25:tx+26] > 0)
                score = activity + min(clearance[py, px], 90)/600 - abs(distance-245)/1000
                points.append((score, (px+x, py+y), np.array((-inward[1], inward[0])), inward, None))
        if not points:
            return None
        return max(points, key=lambda item: item[0])[1:]

    def reveal_boundary(self, screen):
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

    def deploy(self, screen, log):
        threshold = .72 if self.stage == 2 else .80
        cards = self.cards(screen, threshold=threshold)
        if not cards:
            self.deploy_notice(log, "chưa nhận diện được thanh quân")
            return False
        approach = self.approach or self.entry(screen)
        if not approach and self.stage == 2:
            if not self.revealed_stage_two:
                self.revealed_stage_two = True
                fresh = self.reveal_boundary(screen)
                if fresh is not None and self.phase(fresh) in ("scouting", "combat"):
                    screen = fresh
                    approach = self.entry(screen)
            if not approach:
                approach = self.terrain_entry(screen)
            if approach:
                log("[BH] Stage 2: đã xác định hướng thả quân.")
        if not approach and self.rejected_entries:
            approach = self.terrain_entry(screen)
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
            log(f"[BH] Stage {self.stage}: quân chịu đòn/hero đi trước, Bomber và hỗ trợ theo sát.")
        complete = True
        accepted = False
        pending = []
        tracked = {troop.card[0] for troop in self.troops}
        p = np.asarray(point, dtype=float)
        d = np.asarray(direction, dtype=float)
        inv = np.asarray(inward, dtype=float)
        # Use one confirmed frame for the wave. Verify the actual result before retrying.
        for index, (kind, card) in enumerate(cards):
            if any(abs(x-card[0]) < 30 for x in tracked | self.empty_stacks):
                continue
            spacing = (0, -15, 15, -25, 25)[index % 5]
            depth = 9 if kind in self.FRONTLINE else 17
            drop = tuple(int(round(value)) for value in p + spacing*d - depth*inv)
            troop = Troop(kind, card, time.monotonic())
            count = self.stack_count(screen, card)
            if kind in ("machine", "copter"):
                count = None
            if count is not None:
                if count == 0:
                    self.empty_stacks.add(card[0])
                    continue
            else:
                state = self.slot_state(screen, troop)
                if state == "dead":
                    continue
                if state == "deployed":
                    self.troops.append(troop)
                    continue
            pending.append((troop, count, drop))

        if first_wave and pending:
            # Protect the Bomber before committing the support wave.
            lead = next((item for item in pending if item[0].kind in self.FRONTLINE),
                        next((item for item in pending if item[0].kind != "bomber"), None))
            if lead is not None and any(item[0].kind == "bomber" for item in pending):
                troop, count, drop = lead
                if count is None and self.deploy_survivor(troop, drop, direction, inward):
                    self.troops.append(troop)
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
            for troop, count, drop in pending:
                points = self.drop_candidates(drop, direction, inward)
                target = next(points, None)
                if target is None:
                    continue
                self.send_drop(troop.card, target, min(count or 1, 12), settle=.03, screen=screen)

        if pending:
            screen = self.adb.screencap()
            phase = self.phase(screen)
            if phase not in ("scouting", "combat") or (first_wave and phase == "scouting" and self.combat_seen and self.stage == 1):
                return False
            if phase == "combat":
                self.observe_combat(screen)
        for troop, count, drop in pending:
            if count is None:
                state = self.slot_state(screen, troop)
                if state == "deployed":
                    self.troops.append(troop)
                    accepted = True
                    continue
                if state == "dead":
                    continue
                if self.deploy_survivor(troop, drop, direction, inward):
                    self.troops.append(troop)
                    accepted = True
                else:
                    complete = False
            elif self.deploy_stack(screen, troop.card, drop, direction, inward, log):
                self.empty_stacks.add(troop.card[0])
                accepted = True
            else:
                complete = False
            screen = self.adb.screencap()
            phase = self.phase(screen)
            if phase not in ("scouting", "combat") or (phase == "scouting" and self.combat_seen and self.stage == 1):
                return False
        if not complete:
            if pending and not accepted:
                self.rejected_entries.append(point)
                self.rejected_entries = self.rejected_entries[-4:]
                self.approach = None
                self.deploy_notice(log, "điểm thả chưa được game chấp nhận, chuyển sang hướng khác")
            else:
                self.deploy_notice(log, "còn ô quân chưa xác nhận thả hết")
            self.pan_map_for_deployment(log, cards)
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
                if frame is None or self.is_card_selected(frame, card):
                    return False
                break
        start_y, end_y = ((370, 550) if self.map_pan_attempts == 0 else (550, 370))
        self.adb.swipe(1000, start_y, 1000, end_y, duration_ms=450, jitter=0)
        self.map_pan_attempts += 1
        self.approach = None
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
            (point[0]-x)**2 + (point[1]-y)**2 >= 70**2 for x, y in self.rejected_entries
        )

    def drop_candidates(self, drop, direction, inward):
        p = np.asarray(drop, dtype=float)
        d = np.asarray(direction, dtype=float)
        inv = np.asarray(inward, dtype=float)
        for lateral, outward in ((0, 0), (18, 15), (-18, 23), (36, 8), (-36, 8), (0, 35), (20, 45), (-20, 45)):
            point = tuple(int(round(value)) for value in p + lateral*d - outward*inv)
            if self.battlefield_point(point):
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
    def slot_state(screen, troop):
        x, y = troop.card
        portrait = cv2.cvtColor(screen[y-13:y+21, x-25:x+25], cv2.COLOR_BGR2HSV)
        if np.mean(portrait[:, :, 1] > 55) < .12:
            return "dead"
        hero = troop.kind in ("machine", "copter")
        band = screen[714:726, x-43:x+43] if hero else screen[750:767, x-43:x+43]
        hsv = cv2.cvtColor(band, cv2.COLOR_BGR2HSV)
        life = cv2.inRange(hsv, (0, 110, 115), (81, 255, 255))
        if np.count_nonzero(np.count_nonzero(life, axis=0) >= 7) >= 8:
            return "deployed"
        return "waiting"

    def deploy_survivor(self, troop, drop, direction, inward):
        for point in self.drop_candidates(drop, direction, inward):
            fresh = self.adb.screencap()
            self.send_drop(troop.card, point, screen=fresh)
            fresh = self.adb.screencap()
            if self.phase(fresh) not in ("scouting", "combat"):
                return False
            if self.slot_state(fresh, troop) != "waiting":
                return True
        return False

    def deploy_stack(self, screen, card, drop, direction, inward, log):
        remaining = self.stack_count(screen, card)
        if remaining is None:
            return False
        for _ in range(min(remaining, 12)):
            for point in self.drop_candidates(drop, direction, inward):
                self.send_drop(card, point)
                fresh = self.adb.screencap()
                if fresh is None:
                    return False
                count = self.stack_count(fresh, card)
                if count is None:
                    remaining = 0 if remaining <= 1 else remaining - 1
                    break
                if count > remaining:
                    return False
                if count < remaining:
                    remaining = count
                    break
                if self.phase(fresh) not in ("scouting", "combat"):
                    return False
            else:
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
        cx = troop.card[0]
        left, right = max(0, cx-48), min(screen.shape[1], cx+48)
        hero = troop.kind in ("machine", "copter")
        charge = screen[685:711, left:right] if hero else screen[712:745, left:right]
        hp = screen[715:727, left:right] if hero else screen[749:769, left:right]
        if not charge.size or not hp.size:
            return 0., 0.
        hsv = cv2.cvtColor(charge, cv2.COLOR_BGR2HSV)
        purple = cv2.inRange(hsv, (128, 88, 118), (170, 255, 255))
        fill = np.mean(np.count_nonzero(purple, axis=0) >= (5 if hero else 12))
        hsv = cv2.cvtColor(hp, cv2.COLOR_BGR2HSV)
        green = cv2.inRange(hsv, (23, 95, 105), (94, 255, 255))
        orange = cv2.inRange(hsv, (1, 130, 140), (23, 255, 255))
        health = np.mean(np.count_nonzero(green | orange, axis=0) >= 5)
        return float(fill), float(health)

    def use_abilities(self, screen, log):
        if screen is None:
            return
        now = time.monotonic()
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
            elif kind in ("night_witch", "baby_dragon", "drop_ship", "hog_glider"):
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
        if not okay:
            ocr_fn = getattr(self.base.walls, 'ocr_positions', None)
            if ocr_fn:
                for item in ocr_fn(screen, (600, 600, 400, 200), scale=1):
                    text = item[0]
                    box = item[1]
                    if 'ok' in text.lower():
                        bx = box[0] if isinstance(box, (list, tuple)) else box
                        by = box[1] if isinstance(box, (list, tuple)) and len(box) > 1 else 700
                        cand = (bx, by, 160, 70)
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
        hit = self.base.match(screen, ("4x",), (0, 0, 1600, 900), .88, (.85, 1., 1.15))
        if hit and self.base.green_button(screen, hit):
            self.base.tap(hit, .15)
            self.speed_stage = self.stage
            log(f"[BH] Stage {self.stage}: đã bấm 4×.")

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
            self.approach = self.entry(screen) or (self.terrain_entry(screen) if self.stage == 2 else None)
        if not self.approach:
            cards = self.cards(screen, threshold=.70 if self.stage == 2 else .78)
            if cards:
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
            for index, (kind, card) in enumerate(cards):
                troop = Troop(kind, card, time.monotonic())
                count = self.stack_count(screen, card)
                state = self.slot_state(screen, troop)
                if state in ("dead", "deployed"):
                    continue
                if count is not None and count > 0:
                    leftovers.append((troop, count))
                    self.empty_stacks.discard(card[0])
                elif state == "waiting":
                    leftovers.append((troop, count or 1))
                    self.empty_stacks.discard(card[0])
            if not leftovers:
                self.empty_sweep_observations += 1
                if self.empty_sweep_observations >= 2 and not self.empty_sweep_logged:
                    log(f"[BH] Stage {self.stage}: hai lượt quét liên tiếp chưa thấy lính chờ thả.")
                    self.empty_sweep_logged = True
                break
            self.empty_sweep_observations = 0
            self.empty_sweep_logged = False
            log(f"[BH] Stage {self.stage}: Vét thêm {len(leftovers)} ô lính còn sót: " +
                ", ".join(f"{t.kind} ({c} con)" for t, c in leftovers))
            for index, (troop, count) in enumerate(leftovers):
                spacing = (0, -15, 15, -25, 25)[index % 5]
                depth = 9 if troop.kind in self.FRONTLINE else 17
                drop = tuple(int(round(value)) for value in p + spacing*d - depth*inv)
                candidates = list(self.drop_candidates(drop, direction, inward))
                drop_pt = candidates[min(sweep_round, len(candidates)-1)] if candidates else drop
                self.send_drop(troop.card, drop_pt, min(count, 8), settle=.05, screen=screen)
            time.sleep(0.3)
            screen = self.adb.screencap()
            if screen is None:
                break
            unchanged = 0
            for troop, before in leftovers:
                after = self.stack_count(screen, troop.card)
                state = self.slot_state(screen, troop)
                hero_deployed = troop.kind in ('machine', 'copter') and state == 'deployed'
                if after == 0 or state == 'dead' or hero_deployed:
                    self.empty_stacks.add(troop.card[0])
                    if state != 'dead' and not any(abs(t.card[0] - troop.card[0]) < 25 for t in self.troops):
                        self.troops.append(troop)
                elif after == before or (after is None and state == 'waiting'):
                    unchanged += 1
            if unchanged == len(leftovers) and self.pan_map_for_deployment(log, cards):
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
                if village_state is not None:
                    village_state.update_battle('builder', 3, stage=2)
                self.troops = []
                self.zoomed_for_stage = False
                self.last_deploy_notice = 0
                self.planned = False
                self.approach = None
                self.rejected_entries.clear()
                self.sweep_pending = False
                self.empty_stacks.clear()
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
