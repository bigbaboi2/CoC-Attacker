import re
import time
import json
import re
from pathlib import Path

import cv2
import numpy as np

from bot.builder_battle import BuilderBattle
from bot.walls import WallUpgrader
from core.adb_client import BotStoppedError


class BuilderBaseManager:
    """Own Builder Base actions and keep them separate from Home Village state."""

    OPTIONS = (
        ("builder_base_collect_mine", "Thu vàng và dầu từ mỏ"),
        ("builder_base_collect_gem", "Thu gem từ Gem Mine"),
        ("builder_base_collect_cart", "Thu dầu tím từ Elixir Cart"),
        ("builder_base_upgrade_walls", "Nâng tường bằng Builder Gold / Elixir"),
        ("builder_base_upgrade_buildings", "Tự động nâng cấp công trình làng đêm"),
    )
    BATTLE = (15, 700, 175, 195)
    CENTER = (150, 120, 1300, 695)
    CARTS = ("ready_elixir_cart", "claim_elixir_n2", "claim_elixir_n3",
             "claim_elixir_n5", "claim_elixir_n6", "claim_elixir_n8")
    LOCAL = Path(__file__).resolve().parents[1] / "local_assets" / "builder_base"

    def __init__(self, adb, vision, settings, village, battle_stat=None, village_state=None, pause_callback=None):
        self.village_state = village_state
        self.adb = adb
        self.vision = vision
        self.settings = settings
        self.village = village
        self.pause_callback = pause_callback
        self.walls = WallUpgrader(self)
        self.battle = BuilderBattle(self, battle_stat)
        self.next_maintenance = 0
        self.next_resource_scan = 0
        self.next_wall_check = 0
        self.next_building_check = 0
        self.next_cart_check = 0
        self.retry_after = 0
        self.main_turn_until = 0
        self.main_builders_wait_until = 0
        self.night_builders_wait_until = 0
        self.all_upgrades_complete = False
        self.main_schedule_checked = False
        self.night_schedule_checked = False
        self.active = False
        self._scaled = {}
        self.consecutive_no_wall_count = 0
        self.max_no_wall_retries = 3
        self.walls_confirmed_maxed = False
        self._wall_verified_groups = {}
        self._wall_verified_signature = None
        self._wall_verified_at = 0
        self._elixir_wall_unavailable_at = 0
        self._building_transaction_pending_until = 0

    def pause_tool(self, log, reason):
        log(f"[BH] {reason}. Tạm dừng tool theo thiết lập.")
        self.retry_after = time.monotonic() + 300
        if callable(self.pause_callback):
            self.pause_callback()
        elif hasattr(self, "engine") and hasattr(self.engine, "pause_bot"):
            self.engine.pause_bot()

    def remember_builder_waits(self):
        """Persist builder waits under the current tag or temporary legacy key."""
        village = getattr(self, 'village', None)
        tag = getattr(village, '_session_player_tag', None) or getattr(village, '_session_legacy_key', None)
        if not isinstance(tag, str) or not tag:
            return
        from core.stats_store import StatsStore
        path = Path(__file__).resolve().parents[1] / 'builder_wait_state.json'
        now, wall = time.monotonic(), time.time()
        entry = {'saved_at': wall,
                 'main_until': wall + max(0, self.main_builders_wait_until - now),
                 'night_until': wall + max(0, self.night_builders_wait_until - now),
                 'night_walls_maxed': bool(self.walls_confirmed_maxed)}
        try:
            old = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
            accounts = old.get('accounts', {}) if isinstance(old.get('accounts'), dict) else {}
            if old.get('account') and old.get('account') not in accounts:
                accounts[old['account']] = {key: old.get(key) for key in ('saved_at', 'main_until', 'night_until')}
            accounts[tag] = entry
            data = {'accounts': accounts}
            StatsStore._atomic_write(path, json.dumps(data, ensure_ascii=False).encode('utf-8'))
        except (OSError, ValueError, TypeError):
            pass

    def restore_builder_waits(self, log):
        village = getattr(self, 'village', None)
        tag = getattr(village, '_session_player_tag', None) or getattr(village, '_session_legacy_key', None)
        if not isinstance(tag, str) or not tag:
            return
        if getattr(self, '_restored_wait_account', None) == tag:
            return
        if getattr(self, '_restored_wait_account', None) is not None:
            self.main_builders_wait_until = self.night_builders_wait_until = 0
            self.main_schedule_checked = self.night_schedule_checked = False
            self.walls_confirmed_maxed = False
        self._restored_wait_account = tag
        path = Path(__file__).resolve().parents[1] / 'builder_wait_state.json'
        try:
            data = json.loads(path.read_text(encoding='utf-8'))
            now = time.time()
            accounts = data.get('accounts', {})
            if isinstance(accounts, dict) and tag.startswith('#'):
                legacy = getattr(village, '_verified_header_key', None)
                old = accounts.get(legacy)
                current = accounts.get(tag)
                if isinstance(old, dict):
                    comparable = ('main_until', 'night_until', 'night_walls_maxed')
                    matches = current is None or all(
                        old.get(field) == current.get(field) for field in comparable
                        if field in old and field in current)
                    if matches:
                        accounts.setdefault(tag, old)
                        del accounts[legacy]
                        from core.stats_store import StatsStore
                        StatsStore._atomic_write(path, json.dumps({'accounts': accounts}, ensure_ascii=False).encode('utf-8'))
                    else:
                        log(f'[Thợ] Lịch {legacy} khác {tag}; giữ riêng lịch cũ.')
            entry = accounts.get(tag) if isinstance(accounts, dict) else None
            if entry is None and data.get('account') == tag:
                entry = data
            if entry is None or not 0 <= now - float(entry['saved_at']) <= 86400:
                return
            self.walls_confirmed_maxed = bool(entry.get('night_walls_maxed', False))
            for village_name in ('main', 'night'):
                remaining = float(entry.get(village_name + '_until', 0)) - now
                if 0 < remaining <= 14 * 86400:
                    setattr(self, village_name + '_builders_wait_until', time.monotonic() + remaining)
                    setattr(self, village_name + '_schedule_checked', True)
                    log(f'[Thợ] Dùng lịch đã lưu cho {"làng chính" if village_name == "main" else "làng đêm"}: còn khoảng {max(1, int(remaining)//60)} phút.')
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return

    def get_free_builders(self, screen):
        try:
            val = self.walls.builders(screen, builder_base=True)
            if isinstance(val, int):
                return val
        except BotStoppedError:
            raise
        except Exception:
            pass
        return None

    def confirmed_free_builders(self, screen):
        first = self.get_free_builders(screen)
        if first is None:
            return None
        second = self.get_free_builders(self.adb.screencap())
        return first if second == first else None

    @staticmethod
    def night_palette(screen):
        if not isinstance(screen, np.ndarray) or screen.shape[:2] != (900, 1600):
            return False
        sample = screen[120:635:5, 200:1400:5].astype(np.int16)
        b, g, r = (sample[:, :, index] for index in range(3))
        return np.mean((b > g + 7) & (b > r + 20) & (b < 190)) > .32

    def is_home(self, screen):
        if not isinstance(screen, np.ndarray) or screen.shape[:2] != (900, 1600):
            return False
        # The cave stage has green terrain. Its Builder Battle axes remain the same.
        threshold = .83 if self.night_palette(screen) else .89
        return bool(self.vision.match_template(screen, "builderbase", threshold, roi=self.BATTLE)
                    and self.vision.match_template(screen, "build_btn", .83, roi=(1385, 710, 210, 180))
                    and not self.vision.has_open_dialog(screen))

    def match(self, screen, names, roi, threshold=.84, scales=(1.,), excluded=()):
        if screen is None:
            return None
        x, y, w, h = roi
        crop = screen[y:y+h, x:x+w]
        best = None
        for name in names:
            for scale in scales:
                template = self.template(name, scale)
                if template is None:
                    break
                if not hasattr(template, "shape") or not isinstance(template.shape, (list, tuple)):
                    continue
                th, tw = template.shape[:2]
                if th > crop.shape[0] or tw > crop.shape[1]:
                    continue
                scores = cv2.matchTemplate(crop, template, cv2.TM_CCOEFF_NORMED)
                for cx, cy in excluded:
                    px, py = cx-x-tw//2, cy-y-th//2
                    x1, x2 = max(0, px-23), min(scores.shape[1], px+24)
                    y1, y2 = max(0, py-23), min(scores.shape[0], py+24)
                    if x1 < x2 and y1 < y2:
                        scores[y1:y2, x1:x2] = -1
                _, score, _, (px, py) = cv2.minMaxLoc(scores)
                if score >= threshold and (best is None or score > best[4]):
                    best = (x+px+tw//2, y+py+th//2, tw, th, score)
        return best

    def template(self, name, scale):
        key = (name, scale)
        if key not in self._scaled:
            local = self.LOCAL / f"{name}.png"
            original = self.vision.load_template(str(local) if local.is_file() else name)
            if isinstance(original, np.ndarray):
                self._scaled[key] = cv2.resize(original, None, fx=scale, fy=scale) if scale != 1.0 else original
            else:
                self._scaled[key] = original
        return self._scaled[key]

    def is_main(self, screen):
        return not self.is_home(screen) and self.village.is_in_main_village(screen)

    def deselect(self):
        """Tap on neutral outer area to clear building/wall selection."""
        self.tap((1588, 320), 0.35)

    def tap(self, point, delay=.4):
        self.adb.tap(*point[:2], jitter=0, delay_after=delay)

    def wait_for(self, predicate, seconds=10):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.adb._guard_input()
            screen = self.adb.screencap()
            if screen is not None and predicate(screen):
                return screen
            time.sleep(.25)
        return None

    @staticmethod
    def boat_tap_is_clear(hit):
        if not hit:
            return False
        x, y = hit[:2]
        # Resource bars, their plus buttons, and the bottom HUD cover the map.
        return 150 <= x < 1530 and 210 <= y < 705

    def enter(self, screen):
        if self.is_home(screen):
            return True
        for attempt in range(3):
            if not self.is_main(screen):
                return False
            boat = self.match(screen, ("builder_boat_home", "thuyensun", "builder_boat_badge"),
                              (150, 215, 900, 615), .83, (.7, .85, 1., 1.15, 1.3))
            if self.boat_tap_is_clear(boat):
                self.tap(boat)
                return self.wait_for(self.is_home, 15) is not None
            if attempt < 2:
                # Move the southwest shore away from the bottom-left HUD.
                self.adb.swipe(520, 570, 870, 320, duration_ms=510, jitter=0)
                time.sleep(.55)
                screen = self.adb.screencap()
        return False

    def locate_shore(self, screen, names, roi, threshold):
        for attempt in range(6):
            if not self.is_home(screen):
                break
            hit = self.match(screen, names, roi, threshold, (.7, .85, 1., 1.15, 1.3))
            if self.boat_tap_is_clear(hit):
                return hit
            if attempt == 5:
                break
            # A selection from the builder panel may have zoomed into the green area.
            if attempt == 0 and not hit:
                self.adb.zoom_out_map(times=2)
            elif not self.night_palette(screen):
                self.adb.swipe(880, 465, 535, 240, duration_ms=540, jitter=0)
            else:
                self.adb.swipe(940, 430, 670, 565, duration_ms=490, jitter=0)
            time.sleep(.6)
            screen = self.adb.screencap()
        return None

    def leave(self, screen):
        if not self.is_home(screen):
            return self.is_main(screen)
        boat = self.locate_shore(screen, ("thuyennight", "thuyensun", "builder_return_boat"),
                                 (690, 60, 910, 595), .84)
        if not boat:
            return False
        self.tap(boat)
        return self.wait_for(self.is_main, 15) is not None

    def scan_initial_layout_walls(self, log):
        """Read both Builder Base stages and discard the temporary layout edit."""
        def contains(frame, roi, *words):
            text = ' '.join(self.walls.texts(frame, roi, scale=1)).lower()
            return all(word in text for word in words)

        def stage_number(frame):
            text = ' '.join(self.walls.texts(frame, (0, 60, 300, 120), scale=1))
            match = re.search(r'stage\s*([12])\b', text, re.I)
            return int(match.group(1)) if match else None

        if not self.walls.ensure_menu_open():
            log('[BH] Không mở được bảng thợ để kiểm tra Layout Editor.')
            return False
        editor_open = False
        edit_open = False
        results = {}
        try:
            self.tap((1090, 715), .8)
            frame = self.wait_for(lambda f: contains(f, (300, 0, 1000, 160), 'layout', 'editor'), 5)
            if frame is None:
                return False
            editor_open = True
            self.tap((300, 460), .4)
            frame = self.wait_for(lambda f: contains(f, (200, 750, 400, 150), 'edit', 'layout'), 5)
            if frame is None:
                return False
            self.tap((410, 825), .8)
            frame = self.wait_for(lambda f: contains(f, (0, 0, 650, 150), 'builder', 'edit'), 5)
            if frame is None:
                return False
            edit_open = True
            current_stage = stage_number(frame)
            if current_stage is None:
                return False
            for stage in (current_stage, 3 - current_stage):
                if stage != current_stage:
                    # Move the map until its Stage label confirms the transition.
                    start_y, end_y = (300, 680) if stage == 2 else (680, 300)
                    for _ in range(8):
                        self.adb.swipe(800, start_y, 800, end_y, duration_ms=650, jitter=0)
                        time.sleep(.5)
                        frame = self.adb.screencap()
                        if frame is not None and stage_number(frame) == stage:
                            break
                    else:
                        log(f'[BH] Chưa chuyển được sang Stage {stage}; chưa tổng hợp đủ tường.')
                        break
                if not contains(frame, (1290, 70, 300, 130), 'remove', 'all'):
                    break
                self.tap((1445, 130), .5)
                frame = self.wait_for(lambda f: contains(f, (350, 200, 900, 400), 'collect', 'all'), 5)
                if frame is None:
                    break
                self.tap((980, 590), .8)
                frame = self.wait_for(lambda f: stage_number(f) == stage and
                                      contains(f, (0, 60, 300, 120), 'wall'), 5)
                if frame is None:
                    break
                header = ' '.join(self.walls.texts(frame, (0, 60, 300, 120), scale=1))
                if not re.search(r'0\s*/\s*\d+\s*wall', header, re.I):
                    break
                result = self.walls.parse_layout_inventory_cards(frame, builder_base=True)
                fresh = self.adb.screencap()
                confirmed = (self.walls.parse_layout_inventory_cards(fresh, builder_base=True)
                             if fresh is not None and stage_number(fresh) == stage else None)
                if result is None or confirmed is None or result['wall_groups'] != confirmed['wall_groups']:
                    log(f'[BH] Chưa đọc ổn định tường Stage {stage}; hoãn tổng hợp.')
                    break
                counts = {}
                for group in result['wall_groups']:
                    counts[group['level']] = counts.get(group['level'], 0) + group['count']
                if results:
                    previous = next(iter(results.values()))
                    if any(counts.get(level, 0) < count for level, count in previous.items()):
                        log('[BH] Khay tường thay đổi bất thường giữa hai Stage; chưa tổng hợp.')
                        break
                    counts = {level: count - previous.get(level, 0)
                              for level, count in counts.items()
                              if count > previous.get(level, 0)}
                results[stage] = counts
        finally:
            if edit_open:
                self.tap((1445, 635), .5)
                frame = self.wait_for(lambda f: contains(f, (350, 200, 900, 400), 'cancel', 'editing')
                                      or contains(f, (300, 0, 1000, 160), 'layout', 'editor'), 5)
                if frame is not None and contains(frame, (350, 200, 900, 400), 'cancel', 'editing'):
                    self.tap((975, 580), .7)
                elif frame is None:
                    results.clear()
            if editor_open:
                frame = self.wait_for(lambda f: contains(f, (300, 0, 1000, 160), 'layout', 'editor'), 5)
                if frame is not None:
                    self.tap((1525, 70), .7)
                else:
                    results.clear()
        if not self.is_home(self.adb.screencap()):
            log('[BH] Chưa xác nhận về làng sau Layout Editor.')
            return False
        if len(results) != 2:
            log('[BH] Chưa đọc đủ tường cả hai Stage trong Layout Editor.')
            return False
        totals = {}
        for stage, counts in sorted(results.items()):
            summary = ', '.join(f'cấp {level} x{count}' for level, count in sorted(counts.items()))
            log(f'[BH] Layout Editor Stage {stage}: {summary}.')
            for level, count in counts.items():
                totals[level] = totals.get(level, 0) + count
        if self.village_state is not None:
            self.village_state.replace_wall_inventory('builder', totals)
        self._elixir_wall_unavailable_at = 0
        log('[BH] Tổng tường hai Stage: ' +
            ', '.join(f'cấp {level} x{count}' for level, count in sorted(totals.items())) + '.')
        return True

    def collect_mines(self, screen, log, *, gem_only=False):
        resources = (
            (("builder_mine_gem", (33, 65, 115), (89, 255, 255)),)
            if gem_only else (
                ("builder_mine_gold", (15, 100, 140), (43, 255, 255)),
                ("builder_mine_elixir", (120, 90, 115), (178, 255, 255)),
            )
        )
        attempted = []
        for _ in range(12):
            if not self.is_home(screen):
                break
            candidates = []
            for name, low, high in resources:
                hit = self.match(screen, (name,), (170, 95, 1245, 670), .86,
                                 (.6, .7, .8, .9, 1., 1.1, 1.25), attempted)
                if not hit:
                    continue
                x, y, w, h = hit[:4]
                center = screen[y-h//4:y+h//4, x-w//4:x+w//4]
                color = cv2.inRange(cv2.cvtColor(center, cv2.COLOR_BGR2HSV), low, high)
                if np.mean(color > 0) >= .28:
                    candidates.append(hit)
            if not candidates:
                break
            hit = max(candidates, key=lambda candidate: candidate[4])
            self.tap(hit, .45)
            attempted.append(hit[:2])
            screen = self.adb.screencap()
        label = "Gem Mine" if gem_only else "mỏ vàng/dầu"
        log(f"[BH] Đã bấm {len(attempted)} biểu tượng {label} ở khu đang mở.")

    def collect_gems(self, screen, log):
        self.collect_mines(screen, log, gem_only=True)

    def ready_cart(self, screen):
        # Look for a purple reward badge, then verify the cart immediately below it.
        world = screen[:650, 145:1450]
        hsv = cv2.cvtColor(world, cv2.COLOR_BGR2HSV)
        purple = cv2.inRange(hsv, (120, 100, 105), (173, 255, 255))
        _, _, blobs, centers = cv2.connectedComponentsWithStats(purple)
        for (_, _, w, h, area), (cx, cy) in zip(blobs[1:], centers[1:]):
            if not (9 <= w <= 40 and 7 <= h <= 40 and 65 <= area <= 1150):
                continue
            x, y = int(cx)+145, int(cy)
            if x >= 1240 and y < 190:
                continue
            hit = self.match(screen, self.CARTS, (x-47, y+8, 94, 96),
                             .86, (.65, .78, .9, 1., 1.15, 1.3))
            if hit and abs(hit[0]-x) <= 26:
                return hit
        return None

    def collect_cart(self, screen, log):
        cart = self.ready_cart(screen)
        if not cart:
            return
        self.tap(cart, .65)
        screen = self.adb.screencap()
        collect = self.match(screen, ("collectnight",), self.CENTER, .87, (.85, 1., 1.15))
        title = " ".join(self.walls.texts(screen, (325, 45, 950, 85), scale=1)).lower()
        if collect and "elixir" in title and "cart" in title and self.green_button(screen, collect):
            self.tap(collect, .7)
            log("[BH] Đã bấm Collect trong bảng Elixir Cart.")
        elif "elixir" in title and "cart" in title:
            log("[BH] Xe dầu chưa có nút Collect khả dụng.")
        self.close_panel(self.adb.screencap())

    @staticmethod
    def green_button(screen, hit):
        x, y = hit[0], hit[1]
        w = hit[2] if len(hit) > 2 else 160
        h = hit[3] if len(hit) > 3 else 70
        rx = max(55, w // 2)
        ry = max(20, h // 2)
        crop = screen[max(0, y-ry):min(screen.shape[0], y+ry), max(0, x-rx):min(screen.shape[1], x+rx)]
        if not crop.size:
            return False
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        return np.mean(cv2.inRange(hsv, (30, 60, 60), (95, 255, 255)) > 0) > .25

    def dismiss_star_bonus(self, screen, log=None):
        return self.battle.dismiss_star_bonus(screen, log or (lambda msg: None))

    def close_panel(self, screen):
        close = self.match(screen, ("cart_close", "wall_close", "battle_menu_close"), (1240, 0, 240, 205), .89,
                           (.8, 1., 1.15)) or self.vision.find_close_button(screen)
        if close:
            self.tap(close)

    def wall_balances(self, screen, eligible=None):
        balances = self.vision.read_village_resources(screen, builder=True)
        self._last_wall_balance_read = balances
        policy = self.settings.get("builder_base_wall_resource", "both")
        return {name: balances[name] for name in ("gold", "elixir")
                if policy in (name, "both") and (eligible is None or name in eligible)
                and balances.get("valid", {}).get(name) and balances[name] > 0}

    def wall_opening_resources(self, balances, *, continuing=False):
        """Return only eligible currencies when the configured opening condition is met."""
        mode = self.settings.get('builder_base_wall_trigger', 'either')
        if mode not in ('either', 'both', 'gold', 'elixir'):
            mode = 'either'
        allowed = {'gold', 'elixir'} if mode in ('either', 'both') else {mode}
        reached = {name for name in allowed if type(balances.get(name)) is int
                   and balances[name] > 0
                   and balances[name] >= self.settings.get(f'builder_base_wall_min_{name}', 0)}
        return reached if (mode != 'both' or continuing or reached == allowed) else set()

    def upgrade_wall_cycle(self, screen, log):
        if self.walls_confirmed_maxed:
            log("[BH] Đã xác nhận Max toàn bộ tường làng đêm.")
            if self.settings.get("builder_base_pause_on_no_upgrades", False):
                self.pause_tool(log, "Đã Max toàn bộ tường làng đêm")
                return None
            return False

        balances = self.wall_balances(screen)
        eligible = self.wall_opening_resources(balances)
        mode = self.settings.get('builder_base_wall_trigger', 'either')
        allowed = {'gold', 'elixir'} if mode in ('either', 'both') else {mode}
        for name, label in (("gold", "Builder Gold"), ("elixir", "Builder Elixir")):
            if name not in allowed:
                continue
            threshold = self.settings.get(f"builder_base_wall_min_{name}", 0)
            amount = balances.get(name)
            last_read = getattr(self, '_last_wall_balance_read', {})
            if amount is None and last_read.get('valid', {}).get(name) and last_read.get(name) == 0:
                amount = 0
            if type(amount) is int:
                state = 'đạt ngưỡng' if amount >= threshold and amount > 0 else 'chưa đạt ngưỡng'
                log(f"[BH] {label}: {amount:,}/{threshold:,} ({state}).")
            else:
                log(f"[BH] {label}: chưa đọc được số dư hợp lệ (ngưỡng {threshold:,}).")
        if not eligible:
            detail = {'both': 'cần cả vàng và dầu đạt ngưỡng', 'gold': 'chỉ xét vàng',
                      'elixir': 'chỉ xét dầu', 'either': 'cần ít nhất một loại đạt ngưỡng'}.get(mode, 'chưa đủ tài nguyên')
            log(f"[BH] Bỏ lượt nâng tường trước khi mở bảng thợ: {detail}.")
            return None
        if eligible == {'elixir'}:
            log("[BH] Chỉ Builder Elixir đạt điều kiện: sẽ tìm tường cấp 6 trở lên trước khi chọn nâng.")
            unavailable_at = getattr(self, '_elixir_wall_unavailable_at', 0)
            if unavailable_at and time.monotonic() - unavailable_at < 900:
                log("[BH] Bảng thợ vừa xác nhận không có tường cấp 6 trở lên để nâng bằng dầu; bỏ lượt quét, tiếp tục kiếm vàng.")
                return None
            gold = balances.get('gold')
            gold_threshold = self.settings.get('builder_base_wall_min_gold', 0)
            if type(gold) is int and 0 < gold < gold_threshold:
                log(f"[BH] Có {gold:,} Builder Gold nhưng chưa đạt ngưỡng {gold_threshold:,} trong cài đặt; chưa dùng vàng cho tường cấp thấp.")
        elif eligible == {'gold'}:
            log("[BH] Chỉ Builder Gold đạt điều kiện: tìm tường theo tùy chọn ưu tiên hiện tại.")
        else:
            log("[BH] Vàng và dầu đều đủ điều kiện; tìm tường theo tùy chọn ưu tiên hiện tại.")

        free_builders = self.confirmed_free_builders(screen)
        if free_builders == 0:
            log("[BH] Đã dùng hết thợ xây làng đêm (0 thợ rảnh).")
            if self.settings.get("builder_base_pause_on_no_upgrades", False):
                self.pause_tool(log, "Đã dùng hết thợ xây làng đêm (0 thợ rảnh)")
                return None
            return False

        upgraded = False
        for _ in range(10):
            self.adb._guard_input()
            if not (self.settings.get("builder_base_enabled", False)
                    and self.settings.get("builder_base_upgrade_walls", False)):
                break
            result = self.upgrade_wall(screen, log, eligible_resources=eligible)
            if result is None:
                self.deselect()
                return None
            if not result:
                break
            upgraded = True
            self._wall_verified_groups = {}
            self._wall_verified_signature = None
            self._elixir_wall_unavailable_at = 0
            self.consecutive_no_wall_count = 0
            screen = self.adb.screencap()
            if not self.is_home(screen):
                break

            # Re-read balances and re-verify threshold conditions
            balances = self.wall_balances(screen)
            eligible = self.wall_opening_resources(balances, continuing=True)
            for name, label in (("gold", "Builder Gold"), ("elixir", "Builder Elixir")):
                if name not in allowed:
                    continue
                threshold = self.settings.get(f"builder_base_wall_min_{name}", 0)
                amount = balances.get(name)
                last_read = getattr(self, '_last_wall_balance_read', {})
                if amount is None and last_read.get('valid', {}).get(name) and last_read.get(name) == 0:
                    amount = 0
                if type(amount) is int:
                    state = 'đạt ngưỡng' if amount >= threshold and amount > 0 else 'chưa đạt ngưỡng'
                    log(f"[BH] {label}: {amount:,}/{threshold:,} ({state}).")
                else:
                    log(f"[BH] {label}: chưa đọc được số dư hợp lệ (ngưỡng {threshold:,}).")

            if not eligible:
                detail = {'both': 'cần cả vàng và dầu đạt ngưỡng', 'gold': 'chỉ xét vàng',
                          'elixir': 'chỉ xét dầu', 'either': 'cần ít nhất một loại đạt ngưỡng'}.get(mode, 'chưa đủ tài nguyên')
                log(f"[BH] Tài nguyên không còn đạt điều kiện ({detail}); kết thúc chu trình nâng tường để chuyển sang tác vụ tiếp theo.")
                break

            # Re-check free builders
            free_builders = self.get_free_builders(screen)
            if free_builders == 0:
                log("[BH] Đã dùng hết thợ xây làng đêm (0 thợ rảnh); dừng nâng tường.")
                break

            log(f"[BH] Tài nguyên vẫn đạt điều kiện ({', '.join(sorted(eligible))}); tiếp tục tìm nhóm tường tiếp theo.")
        self.deselect()

        if not upgraded:
            self.consecutive_no_wall_count += 1
            log(f"[BH] Không tìm thấy tường để nâng (lần {self.consecutive_no_wall_count}/{self.max_no_wall_retries}).")
            if self.consecutive_no_wall_count >= self.max_no_wall_retries:
                log("[BH] Chưa thể kết luận tường đã Max: có thể nhận diện hoặc chọn tường thất bại; sẽ kiểm tra lại ở lượt sau.")
                if self.settings.get("builder_base_pause_on_no_upgrades", False):
                    self.pause_tool(
                        log,
                        f"Đã thử {self.consecutive_no_wall_count} lần liên tiếp không tìm thấy tường nào có thể nâng (có thể đã Max hoặc hết thợ)"
                    )
                self.consecutive_no_wall_count = 0
        else:
            self.consecutive_no_wall_count = 0
        return upgraded

    def upgrade_building_cycle(self, screen, log):
        if time.monotonic() < getattr(self, '_building_transaction_pending_until', 0):
            log('[BH] Giao dịch công trình trước chưa rõ; tạm hoãn để tránh nâng lặp.')
            return None
        if self.all_upgrades_complete:
            log("[BH] Đã xác nhận Max toàn bộ công trình làng đêm.")
            if self.settings.get("builder_base_pause_on_no_upgrades", False):
                self.pause_tool(log, "Đã Max toàn bộ công trình làng đêm")
                return None
            return False

        free = self.confirmed_free_builders(screen)
        reserve = 0 if self.walls_confirmed_maxed else int(self.settings.get("builder_base_reserve_builders", 0))

        if free is None:
            log('[BH] Chưa đọc chắc số thợ làng đêm; hoãn nâng công trình.')
            return None
        if free <= reserve:
            if free == 0:
                seconds = self.night_builder_wait_seconds(screen)
                self.night_builders_wait_until = time.monotonic() + seconds
                self.remember_builder_waits()
                log(f"[BH] Đã dùng hết thợ xây làng đêm (0 thợ rảnh). Thợ gần nhất xong sau {max(1, seconds//60)} phút.")
                if self.settings.get("builder_base_pause_on_no_upgrades", False):
                    self.pause_tool(log, "Đã dùng hết thợ xây làng đêm (0 thợ rảnh)")
                    return None
            else:
                log(f"[BH] Số thợ rảnh ({free}) không vượt mức duy trì ({reserve}); giữ thợ cho nâng tường.")
            return False

        to_assign = free - reserve
        wallet = self.vision.read_village_resources(screen, builder=True)
        gold_val = wallet.get('gold', 0) if wallet.get('valid', {}).get('gold') else 0
        elixir_val = wallet.get('elixir', 0) if wallet.get('valid', {}).get('elixir') else 0
        log(f"[BH] Kiểm tra nâng công trình: thợ rảnh {free if free is not None else '?'}, mức duy trì {reserve}. Số dư: Vàng {gold_val:,} | Dầu {elixir_val:,}.")

        if not self.walls.ensure_menu_open():
            log("[BH] Không mở được menu thợ làng đêm.")
            return False

        from bot.suggested_upgrades import SuggestedUpgrader
        upgrader = getattr(self, '_suggested_upgrader', None)
        if upgrader is None:
            upgrader = self._suggested_upgrader = SuggestedUpgrader(self.village)

        menu_screen = self.adb.screencap()
        words = upgrader.words(menu_screen, (1170, 50, 430, 755))
        if not words:
            log('[BH] Chưa đọc được menu thợ; không kết luận công trình đã Max.')
            self.walls.close_dialog()
            return None
        if any('allupgradescomplete' in re.sub(r'[^a-z0-9]', '', str(w[0]).lower()) for w in words):
            self.all_upgrades_complete = True
            log("[BH] Không còn đề xuất nào trong menu thợ; đã Max công trình làng đêm.")
            self.walls.close_dialog()
            if self.settings.get("builder_base_pause_on_no_upgrades", False):
                self.pause_tool(log, "Đã Max toàn bộ công trình làng đêm")
                return None
            return False

        header_ys = [y for text, _, y in words if 'suggest' in text.lower()]
        header_y = min(header_ys) if header_ys else 50
        raw_candidates = upgrader.menu_candidates(menu_screen, words, start=header_y, end=795, custom_mode=True)

        allowed_res = self.settings.get('builder_base_building_resources', {'gold': True, 'elixir': True})
        candidates = []
        for c in raw_candidates:
            res = c.get('resource')
            if res not in ('gold', 'elixir') or not allowed_res.get(res, True):
                continue
            if c['cost'] > wallet.get(res, 0):
                continue
            candidates.append(c)

        if not candidates:
            log("[BH] Không có công trình đề xuất nào đủ tài nguyên để nâng.")
            self.walls.close_dialog()
            return False

        priority = self.settings.get('builder_base_building_priority', 'suggested')
        sorted_candidates = upgrader.sort_candidates(candidates, priority=priority)

        assigned_any = False
        for cand in sorted_candidates:
            if to_assign <= 0:
                break
            res = cand['resource']
            if cand['cost'] > wallet.get(res, 0):
                continue

            tap_x = cand.get('x', 1350)
            tap_y = cand.get('y')
            if tap_y is None:
                continue

            log(f"[BH] Chọn nâng {cand['name']}: {cand['cost']:,} {res}.")
            self.adb.tap(tap_x, tap_y, jitter=0, delay_after=0.5)

            selected_screen = self.adb.screencap()
            confirm_pt = self.builder_building_confirmation(selected_screen, cand, upgrader)
            if confirm_pt is None and not upgrader.upgrade_close_button(selected_screen):
                if upgrader.close_menu(selected_screen):
                    point = upgrader.upgrade_button(self.adb.screencap(), cand)
                    if point is not None:
                        self.adb.tap(*point, jitter=0, delay_after=0.5)
                        confirm_pt = self.builder_building_confirmation(self.adb.screencap(), cand, upgrader)

            if confirm_pt is not None:
                second = self.builder_building_confirmation(self.adb.screencap(), cand, upgrader)
                if second != confirm_pt:
                    log('[BH] Hộp xác nhận công trình chưa ổn định; hủy lượt.')
                    self.walls.close_dialog()
                    return None
                log(f"[BH] Xác nhận nâng cấp {cand['name']} (chi phí {cand['cost']:,} {res}).")
                self._building_transaction_pending_until = time.monotonic() + 300
                self.adb.tap(*confirm_pt, jitter=0, delay_after=1.0)
                verified = 0
                for _ in range(6):
                    after_screen = self.adb.screencap()
                    after = self.vision.read_village_resources(after_screen, builder=True)
                    after_free = self.get_free_builders(after_screen) if self.is_home(after_screen) else None
                    matches = (after.get('valid', {}).get(res) is True and wallet.get('valid', {}).get(res) is True
                               and wallet[res] - after[res] == cand['cost'] and after_free == free - 1)
                    verified = verified + 1 if matches else 0
                    if verified >= 2:
                        self._building_transaction_pending_until = 0
                        self.night_schedule_checked = False
                        log(f"[BH] Đã xác minh nâng {cand['name']}: trừ đúng {cand['cost']:,} {res}, thợ {free} → {after_free}.")
                        self.deselect()
                        return True
                    time.sleep(.3)
                log('[BH] Đã gửi xác nhận công trình nhưng chưa đối chiếu được tiền và thợ; hoãn 5 phút, không bấm lại.')
                return None
            else:
                log(f"[BH] Không xác định được nút xác nhận cho {cand['name']}.")
                self.walls.close_dialog()
                self.deselect()

        self.deselect()
        return assigned_any

    def builder_building_confirmation(self, screen, candidate, upgrader):
        """Require the exact building, price and allowed currency in an enabled button."""
        if not isinstance(screen, np.ndarray) or screen.shape[:2] != (900, 1600):
            return None
        words = upgrader.words(screen, (200, 0, 1200, 880))
        text = ' '.join(str(t).lower() for t, _, _ in words)
        if re.search(r'\b(?:gems?|goblin|finish now|need more|buy resources)\b', text):
            return None
        title = re.sub(r'[^a-z0-9]', '', ' '.join(str(t).lower() for t, _, y in words if y < 300))
        name = re.sub(r'[^a-z0-9]', '', candidate['name'].lower())
        if not name or name not in title or 'upgrade' not in title:
            return None
        hsv = cv2.cvtColor(screen, cv2.COLOR_BGR2HSV)
        green = cv2.inRange(hsv[550:870, 400:1300], (35, 80, 70), (95, 255, 255))
        contours, _ = cv2.findContours(green, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for contour in contours:
            x, y, w, h = cv2.boundingRect(contour)
            x, y = x + 400, y + 550
            if w < 100 or h < 35:
                continue
            roi = (x, y, w, h)
            if upgrader.currency(screen, roi) != candidate['resource']:
                continue
            digits = []
            for token, px, py in sorted(words, key=lambda item: item[1]):
                if x <= px <= x+w and y-8 <= py <= y+h+8:
                    cleaned = str(token).translate(str.maketrans({'O':'0', 'o':'0', 'I':'1', 'l':'1', 'i':'1'}))
                    if re.fullmatch(r'[\d\s,.]+', cleaned):
                        digits.append(re.sub(r'\D', '', cleaned))
            if digits and int(''.join(digits)) == candidate['cost']:
                return (x+w//2, y+h//2)
        return None

    def upgrade_wall(self, screen, log, eligible_resources=None):
        """Try the suggested wall, then a bounded single-wall fallback before payment."""
        elixir_only = eligible_resources is not None and set(eligible_resources) == {'elixir'}
        budget = max(self.wall_balances(screen, eligible_resources).values(), default=0) if eligible_resources is not None else None
        if not self.walls.is_menu_open(screen):
            tab = self.match(screen, ("upgrade2/ui/night_builder_tab",), (1390, 640, 140, 90),
                             .80, (.85, 1., 1.15))
            # The Builder Base builder badge is on the right, above Build.
            if not self.is_home(screen):
                return False
            builder_button = tab or (1455, 690)
            self.tap(builder_button)
            screen = self.wait_for(self.walls.is_menu_open, 2)
            if screen is None and self.is_home(self.adb.screencap()):
                # The first tap can clear a selected wall instead of opening the menu.
                self.tap(builder_button)
                screen = self.wait_for(self.walls.is_menu_open, 2)

        row = None
        priority = self.settings.get("builder_base_wall_lowest_first", False) or elixir_only
        if screen is not None and priority and self.walls.is_menu_open(screen):
            row, screen = self.lowest_wall_row(screen, budget, log, min_level=6 if elixir_only else 1)
            if row is None:
                log("[BH] Không chọn được nhóm tường phù hợp; hoãn lượt nâng.")
                if screen is not None and self.walls.is_menu_open(screen):
                    self.tap((1570, 30), .35)
                return None
            if not row.get('_already_selected'):
                self.tap((row["x"], row["y"] + 12), .55)
                screen = self.adb.screencap()
        if screen is not None and not priority:
            previous = None
            for step in range(8):
                if not self.walls.is_menu_open(screen):
                    break
                words = self.walls.ocr_positions(screen, (1180, 65, 415, 730))
                rows = self.walls.menu_rows(screen, words)
                if step == 0 and not rows:
                    all_done = False
                    try:
                        all_done = bool(self.match(screen, ("upgrade2/ui/all_done", "upgrade2/ui/all_done_panel"),
                                                  (1180, 65, 415, 730), .80))
                    except Exception:
                        all_done = False
                    if not all_done and isinstance(words, (list, tuple)):
                        try:
                            import unicodedata
                            raw_txt = "".join(str(w[0] if isinstance(w, (tuple, list)) else w) for w in words).lower()
                            norm = "".join(c for c in unicodedata.normalize("NFD", raw_txt) if unicodedata.category(c) != "Mn")
                            clean = re.sub(r"[^a-z0-9]", "", norm)
                            if any(k in clean for k in ("allupgradescomplete", "tatcanangcaphoanthanh")):
                                all_done = True
                        except Exception:
                            pass
                    if all_done:
                        self.walls_confirmed_maxed = True
                        self.all_upgrades_complete = True
                        log("[BH] Đã hoàn thành toàn bộ nâng cấp làng đêm (All Upgrades Complete).")
                        if self.settings.get("builder_base_pause_on_no_upgrades", False):
                            self.pause_tool(log, "Hết thứ để nâng (All Upgrades Complete)")
                        break
                if budget is not None:
                    rows = [row for row in rows if row["cost"] <= budget]
                if rows:
                    candidate = min(rows, key=lambda item: item["cost"])
                    stable = self.walls.stable_menu_row(candidate)
                    if stable is not None and 65 <= stable["y"] <= 775:
                        row = stable
                        # OCR reports the title near the top of the card.
                        # Tap the card interior after the scrolling list settles.
                        self.tap((row["x"], row["y"] + 12), .55)
                        screen = self.adb.screencap()
                    else:
                        log("[BH] Hàng tường còn dịch chuyển hoặc bị khuất; bỏ chọn từ bảng gợi ý.")
                        screen = self.adb.screencap()
                    break
                signature = self.walls.menu_signature(words)
                if (signature and signature == previous) or step == 7:
                    break
                previous = signature
                self.adb.swipe(1445, 746, 1445, 290, duration_ms=510, jitter=0)
                time.sleep(.4)
                screen = self.adb.screencap()

        if screen is None:
            screen = self.adb.screencap()
        if self.walls.is_menu_open(screen):
            close = self.vision.find_close_button(screen)
            if close and isinstance(close, (tuple, list)) and len(close) >= 2 and close[0] > 1530 and close[1] < 55:
                self.tap(close, .35)
            else:
                self.tap((1570, 30), .35)
        level = None
        if row:
            screen, level = self.wait_for_wall_selection()
        if row and level is None:
            log("[BH] Chưa đọc được cấp tường ổn định sau khi chọn; chuyển sang chọn trên bản đồ.")
        if priority and (level is None or level + 1 != row.get('upgrade_level')):
            log("[BH] Cấp tường đã chọn chưa khớp nhóm ưu tiên; hoãn lượt nâng.")
            return None
        if level is not None:
            log(f"[BH] Đã chọn tường cấp {level} từ bảng gợi ý; kiểm tra số lượng và giá.")
            if row and eligible_resources is not None:
                balances = self.wall_balances(screen, eligible_resources)
                if level < 6:
                    balances.pop('elixir', None)
                price = row.get('cost')
                if type(price) is int and not any(amount >= price for amount in balances.values()):
                    if level < 6 and 'elixir' in eligible_resources:
                        log(f"[BH] Vàng không đủ nâng tường cấp {level}; tìm tường cấp 6 trở lên bằng dầu.")
                        self.deselect()
                        fresh = self.adb.screencap()
                        if self.is_home(fresh):
                            return self.upgrade_wall(fresh, log, eligible_resources={'elixir'})
                        return None
                    log(f"[BH] Tường cấp {level} cần {price:,}/đoạn; chưa đủ tài nguyên phù hợp để nâng.")
                    return None
            screen, quantity = self.prepare_wall_quantity(screen, row, log, eligible_resources)
            if screen is None and row is not None:
                screen = self.single_wall_after_quantity_failure(level, row, log)
                quantity = 1
            if screen is not None:
                result = self.confirm_wall_upgrade(screen, level, log, row=row, quantity=quantity,
                                                   eligible_resources=eligible_resources)
                if result is not False:
                    if result:
                        self.deselect()
                    return result
        if priority:
            log("[BH] Chưa nâng được nhóm cấp thấp đã chọn; hoãn thay vì chọn nhóm khác.")
            return None
        log("[BH] Thử lại bằng một đoạn tường trên bản đồ.")
        screen = self.adb.screencap()
        if self.is_home(screen):
            return self.select_visible_wall(screen, log, eligible_resources)
        return False

    def lowest_wall_row(self, screen, budget, log, precise=False, _deadline=None, min_level=1):
        """Scan the whole builder list before choosing a verified lowest level."""
        log("[BH] Đang quét toàn bộ bảng thợ để tìm nhóm tường cấp thấp nhất.")
        deadline = _deadline if _deadline is not None else time.monotonic() + 100
        def expired():
            self.adb._guard_input()
            if time.monotonic() >= deadline:
                log("[BH] Đã hết 100 giây tìm tường; kết thúc lượt này và tiếp tục tác vụ khác.")
                return True
            return False
        def menu_words(frame):
            if precise:
                return self.walls.ocr_positions(frame, (1180, 65, 415, 730))
            return self.walls.menu_words(frame)
        def read(frame):
            words = menu_words(frame)
            rows = [item for item in self.walls.menu_rows(frame, words, read_level=False) if 80 <= item['y'] <= 735]
            if not precise:
                for item in rows:
                    item['badge_level'] = self.builder_wall_badge_template(frame, item['y'])
            return words, rows, self.walls.menu_signature(words)

        def find_group(item, frame, directions=('up', 'down')):
            for direction in directions:
                previous = None
                for _ in range(16):
                    if expired():
                        return None, frame
                    words = menu_words(frame)
                    rows = self.walls.menu_rows(frame, words, read_level=False)
                    found = next((r for r in rows if r['cost'] == item['cost']
                                  and r['available'] == item['available'] and 80 <= r['y'] <= 735), None)
                    if found:
                        return found, frame
                    signature = self.walls.menu_signature(words)
                    if not signature or signature == previous:
                        break
                    previous = signature
                    start, end = (650, 470) if direction == 'down' else (300, 480)
                    self.adb.swipe(1410, start, 1410, end, duration_ms=500, jitter=0)
                    time.sleep(.9)
                    frame = self.adb.screencap()
            return None, frame

        if not precise:
            result = self.capture_wall_pages(screen, deadline, log)
            if result is None:
                return None, self.adb.screencap()
            pages, screen = result
            log(f"[BH] Đã chụp {len(pages)} trang bảng thợ; đang đọc từng trang.")
            groups = {}
            for page_index, frame in enumerate(pages):
                if expired():
                    return None, screen
                _, rows, _ = read(frame)
                details = ', '.join(f"x{r['available']} giá {r['cost']:,} huy hiệu {r.get('badge_level') or '?'}" for r in rows)
                log(f"[BH] Trang {page_index+1}/{len(pages)}: {details or 'không thấy tường'}.")
                for item in rows:
                    key = (item['cost'], item['available'])
                    item['page'] = page_index
                    y = item['y']
                    if y - 20 >= 75 and y + 53 <= 790:
                        item['_row_image'] = cv2.cvtColor(frame[y-20:y+53, 1230:1585], cv2.COLOR_BGR2GRAY)
                    old = groups.get(key)
                    if old is None or old.get('badge_level') is None:
                        if old is not None and '_row_image' not in item and '_row_image' in old:
                            item['_row_image'] = old['_row_image']
                            item['page'] = old['page']
                        groups[key] = item
                    elif item.get('badge_level') is not None and item['badge_level'] != old['badge_level']:
                        old['badge_conflict'] = True
                    elif item.get('badge_level') == old['badge_level']:
                        if item.get('_row_image') is not None:
                            old['page'] = page_index
                            old['_row_image'] = item['_row_image']
            unknown = [item for item in groups.values()
                       if type(item.get('badge_level')) is not int or item.get('badge_conflict')]
            known = [item for item in groups.values()
                     if type(item.get('badge_level')) is int and not item.get('badge_conflict')
                     and item['badge_level'] >= min_level + 1]
            if min_level > 1:
                skipped = sum(1 for item in groups.values() if type(item.get('badge_level')) is int
                              and not item.get('badge_conflict') and item['badge_level'] < min_level + 1)
                log(f"[BH] Chỉ dùng dầu: bỏ {skipped} nhóm đã nhận diện dưới cấp {min_level}.")
            if not groups and self.night_upgrades_complete(pages[0]):
                self.all_upgrades_complete = True
                self.walls_confirmed_maxed = True
                log('[BH] Đã xác nhận bảng thợ báo hoàn tất toàn bộ nâng cấp; ưu tiên làng chính.')
                return None, screen
            if not unknown and not known and groups:
                if min_level == 6:
                    self._elixir_wall_unavailable_at = time.monotonic()
                log(f"[BH] Bảng thợ không có nhóm tường từ cấp {min_level} có thể nâng. "
                    "Tường đã đạt cấp tối đa không xuất hiện trong danh sách nâng cấp.")
                return None, screen
            if groups and len(unknown) <= 1:
                candidate = unknown[0] if unknown else min(known, key=lambda item: (item['badge_level'], item['cost']))
                if not unknown and budget is not None and candidate['cost'] > budget:
                    log(f"[BH] Tường cấp thấp nhất cần {candidate['cost']:,}; chưa đủ tài nguyên hợp lệ.")
                    return None, screen
                log(f"[BH] Wall x{candidate['available']} giá {candidate['cost']:,} xuất hiện ở trang {candidate['page']+1}; tìm lại bằng ảnh hàng đã chụp.")
                sample = candidate.get('_row_image')
                if sample is None:
                    log("[BH] Hàng tường trong ảnh bị khuất; hoãn chọn để tránh bấm nhầm.")
                    return None, screen
                found = None
                for step in range(25):
                    if expired():
                        return None, screen
                    roi = cv2.cvtColor(screen[75:790, 1230:1585], cv2.COLOR_BGR2GRAY)
                    _, score, _, point = cv2.minMaxLoc(cv2.matchTemplate(roi, sample, cv2.TM_CCOEFF_NORMED))
                    log(f"[BH] Dò ảnh hàng, lần {step+1}: độ khớp {score:.3f}.")
                    if score >= .90:
                        y = 75 + point[1] + 20
                        log(f"[BH] Thấy ảnh hàng sau {step} lần vuốt về, điểm khớp {score:.3f}, y={y}; đối chiếu số lượng và giá.")
                        words = self.walls.ocr_positions(screen, (1235, max(50, y-25), 365, 105))
                        found = next((r for r in self.walls.menu_rows(screen, words, read_level=False)
                                      if r['cost'] == candidate['cost'] and r['available'] == candidate['available']
                                      and abs(r['y']-y) <= 25), None)
                        if found:
                            break
                        log("[BH] Ảnh hàng khớp nhưng chữ hoặc giá chưa khớp; tiếp tục tìm.")
                    self.adb.swipe(1410, 300, 1410, 600, duration_ms=420, jitter=0)
                    time.sleep(.55)
                    fresh = self.adb.screencap()
                    if fresh is None:
                        break
                    before = cv2.cvtColor(screen[75:790, 1220:1580], cv2.COLOR_BGR2GRAY)
                    after = cv2.cvtColor(fresh[75:790, 1220:1580], cv2.COLOR_BGR2GRAY)
                    screen = fresh
                    if np.mean(cv2.absdiff(before, after)) < 2.0:
                        log("[BH] Đã tới đầu bảng nhưng chưa thấy lại hàng tường.")
                        break
                stable = self.walls.stable_menu_row(found) if found else None
                if stable is None or stable['available'] != candidate['available']:
                    log("[BH] Không xác minh được hàng tường tại trang đã lưu; hoãn lượt này.")
                    return None, self.adb.screencap()
                self.tap((stable['x'], stable['y'] + 12), .55)
                menu = self.adb.screencap()
                if self.walls.is_menu_open(menu):
                    self.tap((1570, 30), .4)
                selected, level = self.wait_for_wall_selection()
                if level is None or (not unknown and level + 1 != candidate['badge_level']):
                    log("[BH] Cấp tường thực tế chưa khớp huy hiệu; hoãn nâng.")
                    return None, self.adb.screencap()
                if unknown and (level < min_level or (known and level + 1 > min(item['badge_level'] for item in known))):
                    if not known:
                        log(f"[BH] Nhóm chưa rõ là cấp {level}; kiểm tra lại toàn bảng bằng OCR để tìm tường từ cấp {min_level}.")
                        self.tap((1455, 690))
                        screen = self.wait_for(self.walls.is_menu_open, 3)
                        if screen is None:
                            self.tap((1455, 690))
                            screen = self.wait_for(self.walls.is_menu_open, 3)
                        if screen is None:
                            log("[BH] Không mở lại được bảng thợ để kiểm tra tường bằng dầu.")
                            return None, self.adb.screencap()
                        return self.lowest_wall_row(screen, budget, log, precise=True,
                                                    _deadline=deadline, min_level=min_level)
                    lowest = min(known, key=lambda item: (item['badge_level'], item['cost']))
                    log(f"[BH] Nhóm chưa rõ là cấp {level}; tiếp tục tìm nhóm phù hợp Wall x{lowest['available']} giá {lowest['cost']:,} trong cùng lượt.")
                    if budget is not None and lowest['cost'] > budget:
                        log(f"[BH] Tường cấp thấp nhất cần {lowest['cost']:,}; chưa đủ tài nguyên hợp lệ.")
                        return None, selected
                    self.tap((1455, 690))
                    screen = self.wait_for(self.walls.is_menu_open, 3)
                    if screen is None:
                        self.tap((1455, 690))
                        screen = self.wait_for(self.walls.is_menu_open, 3)
                    if screen is None:
                        log("[BH] Không mở lại được bảng thợ để chọn nhóm thấp hơn.")
                        return None, self.adb.screencap()
                    found, screen = find_group(lowest, screen, directions=('down', 'up'))
                    stable = self.walls.stable_menu_row(found) if found else None
                    if stable is None or stable['available'] != lowest['available']:
                        log("[BH] Chưa tìm lại được nhóm cấp thấp hơn ở bảng thợ; hoãn nâng.")
                        return None, self.adb.screencap()
                    self.tap((stable['x'], stable['y'] + 12), .55)
                    menu = self.adb.screencap()
                    if self.walls.is_menu_open(menu):
                        self.tap((1570, 30), .4)
                    selected, level = self.wait_for_wall_selection()
                    if level is None or level + 1 != lowest['badge_level']:
                        log("[BH] Cấp thực tế của nhóm thấp hơn chưa khớp huy hiệu; hoãn nâng.")
                        return None, self.adb.screencap()
                    candidate = lowest
                    log(f"[BH] Đã xác minh nhóm thấp hơn: Wall x{candidate['available']}, cấp {level}, giá {candidate['cost']:,}.")
                if budget is not None and candidate['cost'] > budget:
                    log(f"[BH] Tường cấp thấp nhất cần {candidate['cost']:,}; chưa đủ tài nguyên hợp lệ.")
                    return None, selected
                candidate['upgrade_level'] = level + 1
                candidate['_already_selected'] = True
                candidate.pop('_row_image', None)
                log(f"[BH] Đã xác minh Wall x{candidate['available']} cấp {level}; giữ lựa chọn.")
                return candidate, selected
            missing = [f"x{r['available']} giá {r['cost']:,}" for r in groups.values()
                       if type(r.get('badge_level')) is not int or r.get('badge_conflict')]
            log("[BH] Chưa đọc chắc huy hiệu: " + (', '.join(missing) or 'không thấy hàng tường') + '; chuyển sang kiểm tra dự phòng.')
            return self.lowest_wall_row(screen, budget, log, precise=True, _deadline=deadline, min_level=min_level)

        previous = None
        for _ in range(16):
            if expired():
                return None, screen
            words = menu_words(screen)
            signature = self.walls.menu_signature(words)
            if not signature:
                return None, screen
            if signature and signature == previous:
                break
            previous = signature
            self.walls.scroll_menu('up')
            time.sleep(.6)
            screen = self.adb.screencap()
        else:
            return None, screen
        groups = {}
        previous = None
        for _ in range(16):
            if expired():
                return None, screen
            words, rows, signature = read(screen)
            if not signature:
                return None, screen
            for item in rows:
                key = (item['cost'], item['available'])
                if key in groups and groups[key].get('badge_level') is not None:
                    badge = item.get('badge_level')
                    if badge is not None and badge != groups[key]['badge_level']:
                        groups[key]['badge_conflict'] = True
                else:
                    groups[key] = item
            if signature and signature == previous:
                break
            previous = signature
            self.walls.scroll_menu('down')
            time.sleep(.6)
            screen = self.adb.screencap()
        else:
            return None, screen
        if not groups:
            if not precise:
                log("[BH] Quét nhanh chưa thấy nhóm tường; kiểm tra lại bằng OCR toàn bảng.")
                return self.lowest_wall_row(screen, budget, log, precise=True, _deadline=deadline, min_level=min_level)
            log("[BH] Không thấy nhóm tường trong bảng thợ.")
            return None, screen
        if not precise and all(type(item.get('badge_level')) is int and not item.get('badge_conflict')
                               for item in groups.values()):
            candidate = min(groups.values(), key=lambda item: (item['badge_level'], item['cost']))
            if budget is not None and candidate['cost'] > budget:
                log(f"[BH] Nhóm tường thấp nhất cần {candidate['cost']:,}; chưa đủ tài nguyên hợp lệ.")
                return None, screen
            log(f"[BH] Nhận diện nhanh Wall x{candidate['available']}, cấp dự kiến {candidate['badge_level']-1}; đối chiếu cấp thực tế.")
            found, screen = find_group(candidate, screen)
            stable = self.walls.stable_menu_row(found) if found else None
            if stable is None or stable['available'] != candidate['available']:
                log("[BH] Hàng tường chưa ổn định; hoãn lượt này.")
                return None, self.adb.screencap()
            self.tap((stable['x'], stable['y'] + 12), .55)
            menu = self.adb.screencap()
            if self.walls.is_menu_open(menu):
                self.tap((1570, 30), .4)
            selected, level = self.wait_for_wall_selection()
            if level is None or level + 1 != candidate['badge_level']:
                log("[BH] Cấp thực tế chưa khớp huy hiệu; hoãn nâng để tránh chọn sai.")
                return None, self.adb.screencap()
            candidate['upgrade_level'] = level + 1
            candidate['_already_selected'] = True
            log(f"[BH] Đã xác minh Wall x{candidate['available']} là cấp {level}; giữ lựa chọn.")
            return candidate, selected
        if not precise:
            log("[BH] Có huy hiệu chưa đọc chắc; kiểm tra bằng OCR toàn bảng.")
            return self.lowest_wall_row(screen, budget, log, precise=True, _deadline=deadline, min_level=min_level)
        signature = frozenset(groups)
        if (getattr(self, '_wall_verified_signature', None) != signature
                or time.monotonic() - getattr(self, '_wall_verified_at', 0) > 900):
            self._wall_verified_groups = {}
            self._wall_verified_signature = signature
        self._wall_verified_at = time.monotonic()
        cache = self._wall_verified_groups
        verified = []
        for item in sorted(groups.values(), key=lambda row: row['cost']):
            if expired():
                return None, screen
            key = (item['cost'], item['available'])
            if key in cache:
                item['upgrade_level'] = cache[key] + 1
                if cache[key] >= min_level:
                    verified.append(item)
                log(f"[BH] Dùng kết quả đã kiểm tra: Wall x{item['available']}, cấp {cache[key]}.")
                continue
            log(f"[BH] Kiểm tra cấp thực tế Wall x{item['available']}, giá {item['cost']:,}.")
            found, screen = find_group(item, screen)
            stable = self.walls.stable_menu_row(found) if found else None
            if not stable or stable['available'] != item['available']:
                log("[BH] Không tìm lại được hàng tường ổn định trong bảng thợ; hoãn lượt này.")
                return None, self.adb.screencap()
            self.tap((stable['x'], stable['y'] + 12), .55)
            menu = self.adb.screencap()
            if self.walls.is_menu_open(menu):
                close = self.vision.find_close_button(menu)
                self.tap(close if close and close[0] > 1530 and close[1] < 55 else (1570, 30), .4)
            selected, level = self.wait_for_wall_selection()
            if level is None:
                return None, self.adb.screencap()
            item['upgrade_level'] = level + 1
            cache[key] = level
            if level >= min_level:
                verified.append(item)
            log(f"[BH] Đã kiểm tra nhóm này là tường cấp {level}.")
            if level == min_level:
                if budget is not None and item['cost'] > budget:
                    log(f"[BH] Tường cấp thấp nhất cần {item['cost']:,}, số dư hợp lệ cao nhất {budget:,}.")
                    return None, selected
                item['_already_selected'] = True
                log(f"[BH] Ưu tiên Wall x{item['available']}, cấp {level}; giữ lựa chọn đã xác minh.")
                return item, selected
            self.tap((1455, 690))
            screen = self.wait_for(self.walls.is_menu_open, 3)
            if screen is None:
                self.tap((1455, 690))
                screen = self.wait_for(self.walls.is_menu_open, 3)
            if screen is None:
                return None, self.adb.screencap()
        if not verified:
            log(f"[BH] Không xác nhận được tường từ cấp {min_level} phù hợp tài nguyên; kết thúc lượt.")
            return None, screen
        candidate = min(verified, key=lambda item: (item['upgrade_level'], item['cost']))
        if budget is not None and candidate['cost'] > budget:
            log(f"[BH] Tường cấp thấp nhất cần {candidate['cost']:,}, số dư hợp lệ cao nhất {budget:,}.")
            return None, screen
        log(f"[BH] Ưu tiên nhóm Wall x{candidate['available']}, cấp {candidate['upgrade_level'] - 1}, giá {candidate['cost']:,}/đoạn.")
        found, screen = find_group(candidate, screen)
        if found:
            stable = self.walls.stable_menu_row(found)
            if stable is not None:
                stable['upgrade_level'] = candidate['upgrade_level']
            return stable, self.adb.screencap()
        return None, screen

    def capture_wall_pages(self, screen, deadline, log):
        """Move to the top and capture overlapping pages without running OCR while scrolling."""
        def region(frame):
            if not isinstance(frame, np.ndarray) or frame.shape[:2] != (900, 1600):
                return None
            return cv2.resize(cv2.cvtColor(frame[75:790, 1220:1580], cv2.COLOR_BGR2GRAY), (180, 358))

        def move(direction):
            self.adb._guard_input()
            a, b = (300, 600) if direction == 'up' else (700, 400)
            self.adb.swipe(1410, a, 1410, b, duration_ms=420, jitter=0)
            time.sleep(.55)
            return self.adb.screencap()

        for _ in range(24):
            if time.monotonic() >= deadline or region(screen) is None:
                log("[BH] Không đưa được bảng thợ về đầu trong thời gian cho phép.")
                return None
            fresh = move('up')
            if region(fresh) is None:
                return None
            if np.mean(cv2.absdiff(region(screen), region(fresh))) < 2.0:
                screen = fresh
                break
            screen = fresh
        else:
            log("[BH] Chưa xác nhận được đầu danh sách thợ; hoãn lượt nâng.")
            return None
        pages = [screen]
        for _ in range(24):
            if time.monotonic() >= deadline:
                log("[BH] Hết thời gian chụp bảng thợ; hoãn lượt nâng.")
                return None
            fresh = move('down')
            if region(fresh) is None:
                return None
            if np.mean(cv2.absdiff(region(screen), region(fresh))) < 2.0:
                return pages, fresh
            pages.append(fresh)
            screen = fresh
        log("[BH] Chưa xác nhận được cuối danh sách thợ; hoãn lượt nâng.")
        return None

    def builder_wall_badge_level(self, screen, title_y):
        """Read the badge without its white border, which OCR can mistake for 1."""
        top = max(0, int(title_y) + 10)
        crop = screen[top + 16:top + 34, 1187:1208]
        if not crop.size:
            return None
        ocr = self.vision._get_ocr()
        if ocr is None:
            return None
        try:
            result = ocr(cv2.resize(crop, None, fx=6, fy=6), use_det=False)
        except Exception:
            return None
        value = ''.join(getattr(result, 'txts', None) or ()).strip().translate(
            str.maketrans({'G': '6', '|': '1', 'I': '1', 'l': '1'}))
        return int(value) if value.isascii() and value.isdigit() and 2 <= int(value) <= 25 else None

    def night_upgrades_complete(self, screen):
        """Only explicit completion text counts; an empty list is not proof of max."""
        words = self.walls.ocr_positions(screen, (1180, 65, 415, 730), scale=1)
        text = re.sub(r'[^a-z0-9]', '', ''.join(str(word[0]).lower() for word in words))
        return 'allupgradescomplete' in text

    def builder_wall_badge_template(self, screen, title_y):
        """Use a badge only when a known glyph matches clearly and uniquely."""
        if not isinstance(screen, np.ndarray) or screen.shape[:2] != (900, 1600):
            return None
        top = max(0, int(title_y) + 10)
        region = cv2.cvtColor(screen[top:min(900, top+38), 1179:1217], cv2.COLOR_BGR2GRAY)
        scores = []
        for level in (2, 3, 4, 5, 6, 7, 8, 9):
            paths = list((self.LOCAL / 'wall_badges').glob(f'{level}*.png'))
            if not paths:
                return None
            best = -1.0
            for path in paths:
                template = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
                if template is None or region.shape[0] < template.shape[0]:
                    continue
                _, score, _, _ = cv2.minMaxLoc(cv2.matchTemplate(region, template, cv2.TM_CCOEFF_NORMED))
                best = max(best, score)
            scores.append((best, level))
        scores.sort(reverse=True)
        if scores[0][0] >= .95 and scores[0][0] - scores[1][0] >= .06:
            return scores[0][1]
        return None

    def wait_for_wall_selection(self):
        previous = None

        def read_selection(frame):
            nonlocal previous
            # Extra contours above the two-currency toolbar can cut off the
            # title in the fast crop. Use the full reader's alternate crops.
            level = self.walls.level(frame) if self.is_home(frame) else None
            stable = level is not None and level == previous
            previous = level
            return stable

        # Allow a second observation even if OCR initialization took several
        # seconds on the first capture.
        for _ in range(4):
            self.adb._guard_input()
            screen = self.adb.screencap()
            if read_selection(screen):
                return screen, previous
            time.sleep(.2)
        return None, None

    def wall_offer(self, screen, resource):
        icon = "Vangbuilderwall" if resource == "gold" else "elixerbuilderwall"
        alternate = "goldwall" if resource == "gold" else "elixerdwall"
        hit = self.match(screen, (icon, alternate), (230, 610, 1100, 190), .84)
        if not hit or self.walls.price_is_red(screen, hit):
            return None
        x, y = hit[:2]
        button = self.walls.upgrade_button(screen, hit)
        if button is None:
            button = self.match(screen, ("wall_upgrade_action",), (x-145, y+12, 145, 120),
                                .88, (.85, 1., 1.15))
            if not button:
                return None
            button = button[:2]
        price = self.walls.amount(screen, (max(0, x-160), y-24, 148, 48))
        return (button, price) if type(price) is int and price > 0 else None

    def prepare_wall_quantity(self, screen, row, log, eligible_resources=None):
        balances = self.wall_balances(screen, eligible_resources)
        level = self.walls.level(screen)
        if level is None:
            log("[BH] Không đọc rõ cấp tường trước khi tính tài nguyên; hoãn lượt nâng.")
            return None, 1
        if level < 6:
            balances.pop('elixir', None)
            if not balances:
                last = getattr(self, '_last_wall_balance_read', {})
                gold = last.get('gold')
                valid = last.get('valid', {}).get('gold', False)
                threshold = self.settings.get('builder_base_wall_min_gold', 0)
                policy = self.settings.get('builder_base_wall_resource', 'both')
                trigger = self.settings.get('builder_base_wall_trigger', 'either')
                if policy not in ('gold', 'both') or trigger == 'elixir':
                    reason = 'cấu hình không cho dùng vàng'
                elif not valid or type(gold) is not int:
                    reason = 'không đọc chắc số vàng làng đêm'
                elif eligible_resources is not None and 'gold' not in eligible_resources:
                    reason = f'vàng {gold:,} chưa đạt ngưỡng mở lượt {threshold:,}'
                else:
                    reason = f'số vàng đọc được {gold:,} không khả dụng'
                log(f"[BH] Tường cấp {level} chỉ nâng bằng Builder Gold; {reason}. Bỏ lượt.")
                return None, 1
        cap = row.get("available", 1)
        affordable = []
        for resource, balance in balances.items():
            offer = self.wall_offer(screen, resource)
            if offer and offer[1] == row["cost"]:
                affordable.append(balance // offer[1])
        target = min(cap, max(affordable, default=1))
        if target <= 1:
            return screen, 1
        log(f"[BH] Dự kiến chọn tối đa {target} đoạn; kiểm tra nút chọn và số tiền trước khi nâng.")
        more = self.walls.enter_more(screen)
        if more is None:
            log("[BH] Không mở được bộ chọn nhiều đoạn; thử nâng một đoạn đã chọn.")
            return screen, 1
        self.walls.available_walls = cap
        return self.select_wall_quantity_fast(more, target, cap, log)

    def wall_quantity_buttons(self, screen, available):
        """Find the green +1/+10 glyphs in either Builder Base toolbar layout."""
        if not isinstance(screen, np.ndarray) or screen.shape[:2] != (900, 1600):
            return {}
        crop = screen[580:880, :1350]
        buttons = {}
        excluded = None
        for step in (10, 1):
            if step == 10 and available < 10:
                continue
            path = Path(__file__).resolve().parents[1] / 'local_assets' / f'wall_add_{step}.png'
            template = cv2.imread(str(path))
            if template is None:
                continue
            best = None
            for scale in (.75, .85, .9, 1., 1.1, 1.2):
                sample = cv2.resize(template, None, fx=scale, fy=scale)
                scores = cv2.matchTemplate(crop, sample, cv2.TM_CCOEFF_NORMED)
                if excluded is not None:
                    x1, y1, x2, y2 = excluded
                    scores[max(0, y1-30):min(scores.shape[0], y2+30),
                           max(0, x1-30):min(scores.shape[1], x2+30)] = -1
                _, score, _, (x, y) = cv2.minMaxLoc(scores)
                if score >= .84 and (best is None or score > best[0]):
                    best = (score, x, y, sample.shape[1], sample.shape[0])
            if best:
                _, x, y, w, h = best
                buttons[step] = (x+w//2, y+580+h//2)
                if step == 10:
                    excluded = (x, y, x+w, y+h)
        if 10 in buttons and 1 in buttons:
            dx = buttons[1][0] - buttons[10][0]
            if not (120 <= dx <= 230 and abs(buttons[1][1] - buttons[10][1]) <= 20):
                return {}
        return buttons

    def select_wall_quantity_fast(self, screen, target, available, log):
        current = self.builder_wall_quantity(screen)
        buttons = self.wall_quantity_buttons(screen, available)
        if type(current) is not int or not 1 <= current <= target or 1 not in buttons:
            save = getattr(getattr(self, 'walls', None), 'save_wall_frame', None)
            if callable(save):
                save('builder-wall-quantity-uncertain.png', screen)
            log("[BH] Chưa nhận diện chắc số đoạn hoặc nút +1; hoãn nâng.")
            return None, 1
        remaining = target - current
        tens = remaining // 10 if 10 in buttons else 0
        ones = remaining - tens * 10
        points = ([buttons[10]] * tens if tens else []) + [buttons[1]] * ones
        if points:
            gap = max(100, min(1000, int(self.settings.get('wall_quantity_fixed_gap_ms', 120))))
            log(f"[BH] Wall x{available}: nhận diện +1" + (" và +10" if 10 in buttons else " (không có +10 khả dụng)")
                + f"; gửi {tens} lần +10, {ones} lần +1 để chọn {target} đoạn.")
            self.adb.tap_batch(points, jitter=0, interval_ms=gap, delay_after=.5)
        previous = None
        for _ in range(4):
            frame = self.adb.screencap()
            actual = self.builder_wall_quantity(frame)
            if actual == previous and type(actual) is int and 1 <= actual <= target:
                log(f"[BH] Đã đối chiếu số đoạn sau chọn: {actual}/{target}.")
                return frame, actual
            previous = actual
            time.sleep(.2)
        log("[BH] Không đọc được số đoạn ổn định sau chọn nhanh; bỏ giao dịch.")
        save = getattr(getattr(self, 'walls', None), 'save_wall_frame', None)
        if callable(save):
            save('builder-wall-quantity-uncertain.png', frame)
        return None, 1

    def builder_wall_quantity(self, screen):
        # Builder Base's yellow header often turns 6 into B and 12 into 1
        # in the filtered OCR. Read the unfiltered header first, without the
        # builder menu on the right, and only accept actual digits.
        for text in self.walls.texts(screen, (200, 500, 950, 135), scale=2):
            match = re.search(r'wa[li1][li1].*?[x×]\s*(\d+)', text, re.I)
            if match:
                value = int(match.group(1))
                if 1 <= value <= 1000:
                    return value
        return self.walls.quantity(screen)

    def single_wall_after_quantity_failure(self, level, row, log):
        """Return to a single selected wall only when its level and price still agree."""
        self.adb.keyevent(4)
        screen = self.adb.screencap()
        if screen is None or not self.is_home(screen) or self.walls.level(screen) != level:
            return None
        price = row.get('cost')
        if type(price) is not int or price <= 0:
            return None
        if not any(offer and offer[1] == price
                   for offer in (self.wall_offer(screen, 'gold'), self.wall_offer(screen, 'elixir'))):
            return None
        log('[BH] Không xác minh được chọn nhiều đoạn; đã trở lại một đoạn với cấp và giá khớp.')
        return screen

    def select_visible_wall(self, screen, log, eligible_resources=None):
        tried = []
        for material in ("wood_wall", "stone_wall", "pink_wall"):
            if not self.is_home(screen):
                return False
            point = self.match(screen, (material,), (180, 130, 1220, 480), .86,
                               (.75, .87, 1., 1.13), tried)
            if not point:
                continue
            tried.append(point[:2])
            self.tap(point, .45)
            screen, level = self.wait_for_wall_selection()
            if level is not None:
                if not self.walls.wall_controls_present(screen):
                    self.deselect()
                    continue
                result = self.confirm_wall_upgrade(screen, level, log, eligible_resources=eligible_resources)
                if result is None:
                    return None
                if result:
                    self.deselect()
                    self.consecutive_no_wall_count = 0
                    return True
            screen = self.adb.screencap()
            if self.is_home(screen):
                self.deselect()
                screen = self.adb.screencap()
        log("[BH] Chưa xác nhận được đoạn tường có thể nâng.")
        return False

    def wall_payment_button(self, screen, level, resource, price, quantity):
        if screen is None:
            return None
        if quantity > 1:
            if not self.vision.wall_confirmation(screen):
                return None
            import unicodedata
            raw_body = "".join(self.walls.texts(screen, (420, 330, 760, 200))).lower()
            norm_body = "".join(c for c in unicodedata.normalize("NFD", raw_body) if unicodedata.category(c) != "Mn").replace("đ", "d")
            body = re.sub(r"[^a-z0-9]", "", norm_body)
            match_en = re.search(r"(?:(\d+))?wallsfor(\d+)(?:builder)?(gold|elixir)", body)
            res_vn = r"vang" if resource == "gold" else r"(?:dau|elixir)"
            match_vn = re.search(r"(?:(\d+))?(?:doan)?tuong.*?(?:voi)?(\d+).*?" + res_vn, body)
            button = self.vision.match_template(screen, "okay", .80, roi=(750, 450, 450, 250))
            if match_en and (int(match_en[2]), match_en[3]) == (price, resource) and (match_en[1] is None or int(match_en[1]) == quantity):
                return button
            if match_vn and int(match_vn[2]) == price and (match_vn[1] is None or int(match_vn[1]) == quantity):
                return button
            if str(price) in body and button and (resource in body or ("vang" if resource == "gold" else "dau") in body):
                return button
            return None

        icon = "okaywallbase2" if resource == "gold" else "okaywallbase"
        hit = self.match(screen, (icon, "confirmwall", "okay"), (580, 680, 480, 200), .80, (.82, 1., 1.15))
        if not hit:
            return None
        title = " ".join(self.walls.texts(screen, (230, 10, 1100, 175), scale=1)).lower()
        target = re.search(r"(?:level|c[aấâ]p)\s*(\d+)", title)
        if not target or int(target[1]) != level + 1 or not re.search(r"\b(?:wall|tường|tuong)\b", title):
            return None
        x, y = hit[:2]
        final_price = self.walls.amount(screen, (x-225, y-26, 196, 52))
        button = (x-100, y)
        crop = screen[y-24:y+24, x-130:x-70]
        green = cv2.inRange(cv2.cvtColor(crop, cv2.COLOR_BGR2HSV), (27, 70, 100), (95, 255, 255))
        return button if final_price == price and (green > 0).mean() >= .18 else None

    def confirm_wall_upgrade(self, screen, level, log, row=None, quantity=1, eligible_resources=None):
        """Return None if payment may have been sent and must not be retried."""
        if level is None or not self.is_home(screen):
            return False
        balances = self.wall_balances(screen, eligible_resources)
        if level < 6:
            balances.pop('elixir', None)
        for resource in balances:
            offer = self.wall_offer(screen, resource)
            if not offer:
                continue
            button, price = offer
            if (price > balances[resource]
                    or (row is not None and price != row["cost"] * quantity)):
                log(f"[BH] Bỏ {resource}: giá {price:,}, số dư {balances[resource]:,}, số đoạn {quantity}; giá hoặc số dư chưa khớp.")
                continue
            log(f"[BH] Đang mở xác nhận nâng {quantity} đoạn cấp {level} → {level + 1}, giá {price:,} {resource}.")
            self.tap(button, .55)

            payment = None
            def read_popup(frame):
                nonlocal payment
                payment = self.wall_payment_button(frame, level, resource, price, quantity)
                return bool(payment)

            popup = self.wait_for(read_popup, 3)
            if popup is None:
                fresh = self.adb.screencap()
                if fresh is not None and self.vision.has_open_dialog(fresh):
                    self.adb.keyevent(4)
                log("[BH] Hộp thoại nâng tường chưa khớp cấp, giá hoặc loại tài nguyên.")
                return False
            self.tap(payment, .8)
            log("[BH] Đã gửi xác nhận nâng; đang đối chiếu số dư, không gửi lại khi kết quả chưa rõ.")

            verified = 0
            def read_debit(frame):
                nonlocal verified
                if not self.is_home(frame):
                    verified = 0
                    return False
                remaining = self.vision.read_village_resources(frame, builder=True)
                matched = (remaining.get("valid", {}).get(resource)
                           and balances[resource] - remaining[resource] == price)
                verified = verified + 1 if matched else 0
                return verified >= 2

            if self.wait_for(read_debit, 4) is None:
                fresh = self.adb.screencap()
                if fresh is not None and self.vision.has_open_dialog(fresh):
                    self.adb.keyevent(4)
                log("[BH] Đã bấm nâng tường nhưng chưa xác minh tiền trừ; dừng lượt để tránh bấm lặp.")
                return None
            log(f"[BH] Đã xác minh nâng {quantity} đoạn tường, chi {price:,} {resource}.")
            return True
        log("[BH] Không tìm thấy giá nâng khả dụng khớp với số lượng và tài nguyên đã chọn.")
        return False

    def ready_actions(self, now, first_pass=False):
        if not self.settings.get("builder_base_enabled", False) or now < self.retry_after:
            return []
        main_enabled = self.settings.get("main_village_enabled", True)
        if (not first_pass and main_enabled and now >= self.main_builders_wait_until
                and (self.all_upgrades_complete or now < self.night_builders_wait_until)):
            return []
        actions = []
        if now >= self.next_maintenance:
            actions.extend((key, action) for key, action in (
                ("builder_base_collect_mine", self.collect_mines),
                ("builder_base_collect_gem", self.collect_gems),
            ) if self.settings.get(key, False))
        if (now >= self.next_wall_check
                and self.settings.get("builder_base_upgrade_walls", False)
                and not self.walls_confirmed_maxed):
            actions.append(("builder_base_upgrade_walls", self.upgrade_wall_cycle))
        if (now >= getattr(self, 'next_building_check', 0)
                and self.settings.get("builder_base_upgrade_buildings", False)
                and not self.all_upgrades_complete):
            actions.append(("builder_base_upgrade_buildings", self.upgrade_building_cycle))
        if first_pass or not main_enabled or now >= self.main_turn_until or now < self.main_builders_wait_until:
            actions.append(("builder_base_battle", self.battle.start))
        return actions

    def due(self):
        now = time.monotonic()
        if (self.settings.get('main_village_enabled', True) and now >= self.main_builders_wait_until
                and (self.all_upgrades_complete or now < self.night_builders_wait_until)):
            return False
        return bool(self.ready_actions(now)) or (
            self.settings.get("builder_base_enabled", False)
            and self.settings.get("builder_base_collect_cart", False)
            and not self.settings.get("main_village_enabled", True)
            and now >= self.retry_after
        )

    def scan_resources(self, screen):
        if self.village_state is None or not self.is_home(screen):
            return False
        self.next_resource_scan = time.monotonic() + 30
        return self.village_state.update_resources(
            'builder', self.vision.read_village_resources(screen, builder=True)
        )

    def run_cycle(self, screen, log, first_pass=False):
        now = time.monotonic()
        actions = self.ready_actions(now, first_pass=first_pass)
        cart_due = bool(actions) or now >= getattr(self, "next_cart_check", 0)
        check_cart = (self.settings.get("builder_base_enabled", False)
                      and self.settings.get("builder_base_collect_cart", False)
                      and cart_due
                      and (actions or self.is_home(screen) or self.due()))
        if not actions and not check_cart:
            if now >= self.next_resource_scan:
                self.scan_resources(screen)
            return
        if not self.enter(screen):
            self.retry_after = time.monotonic() + 60
            log("[BH] Chưa xác nhận sang làng đêm. Sẽ thử lại sau 60 giây.")
            return
        self.active = True
        engine = getattr(self, 'engine', None)
        if engine is not None and not engine._verify_builder_walls_once(self):
            self.retry_after = time.monotonic() + 60
            return
        if self.village_state is not None:
            self.village_state.set_active_village('builder')
            self.village_state.update_attack_strategy('builder', {
                'combo': self.settings.get('builder_base_combo', 'current')
            })
        if self.dismiss_star_bonus(self.adb.screencap(), log):
            time.sleep(0.5)
        if first_pass or not self.night_schedule_checked or (self.night_builders_wait_until > 0
                                                and time.monotonic() >= self.night_builders_wait_until):
            fresh = self.adb.screencap()
            free = self.confirmed_free_builders(fresh) if self.is_home(fresh) else None
            if type(free) is int:
                self.night_schedule_checked = True
            if type(free) is int and free == 0:
                seconds = self.night_builder_wait_seconds(fresh)
                self.night_builders_wait_until = time.monotonic() + seconds
                self.remember_builder_waits()
                log(f"[BH] Không có thợ rảnh; kiểm tra lại sau khoảng {max(1, seconds//60)} phút.")
                if not first_pass:
                    return
            if type(free) is int and free > 0:
                self.night_builders_wait_until = 0
                self.remember_builder_waits()
        has_maintenance = any(key in ("builder_base_collect_mine", "builder_base_collect_gem") for key, _ in actions)
        if has_maintenance:
            log("[BH] Thu nhỏ bản đồ làng đêm (zoom out) để tìm mỏ thu hoạch chuẩn xác.")
            self.adb.zoom_out_map(times=2)
            time.sleep(0.4)
        if check_cart:
            self.next_cart_check = now + 60
            fresh = self.adb.screencap()
            if self.is_home(fresh):
                self.collect_cart(fresh, log)
        for key, action in actions:
            if not self.settings.get("builder_base_enabled", False):
                return
            if key != "builder_base_battle" and not self.settings.get(key, False):
                continue
            fresh = self.adb.screencap()
            if not self.is_home(fresh):
                dismissed = self.dismiss_star_bonus(fresh, log)
                if dismissed:
                    time.sleep(0.5)
                    fresh = self.adb.screencap()
                if not (dismissed and self.is_home(fresh)):
                    self.close_panel(fresh)
                    self.deselect()
                    time.sleep(0.4)
                    fresh = self.adb.screencap()
            if not self.is_home(fresh):
                self.retry_after = time.monotonic() + 15
                log("[BH] Màn hình chưa sẵn sàng (có thể bị che bởi popup hoặc đang bận). Tạm hoãn 15 giây rồi thử lại.")
                return
            if key == "builder_base_battle":
                self.scan_resources(fresh)
                completed = action(fresh, log)
                now = time.monotonic()
                if (self.settings.get("main_village_enabled", True)
                        and time.monotonic() >= self.main_builders_wait_until):
                    # Give TH a turn. A stalled TH should not block BH indefinitely.
                    self.main_turn_until = now + 120
                if not completed and not self.battle.in_progress:
                    self.retry_after = now + 60
            else:
                result = action(fresh, log)
                if (self.all_upgrades_complete and self.settings.get('main_village_enabled', True)
                        and time.monotonic() >= self.main_builders_wait_until):
                    log('[BH] Làng đêm đã hoàn tất nâng cấp; chuyển sang làng chính.')
                    return
                minutes = max(1, self.settings.get("builder_base_interval_mins", 30))
                if key == "builder_base_upgrade_walls":
                    if self.walls_confirmed_maxed:
                        self.next_wall_check = time.monotonic() + 60 * minutes
                    elif result is True:
                        self.next_wall_check = time.monotonic() + 15
                    else:
                        self.next_wall_check = time.monotonic() + 120
                        log("[BH] Hoãn kiểm tra tường 120 giây; các tác vụ khác vẫn tiếp tục.")
                elif key == "builder_base_upgrade_buildings":
                    if self.all_upgrades_complete:
                        self.next_building_check = time.monotonic() + 60 * minutes
                    elif result is True:
                        self.next_building_check = time.monotonic() + 15
                    else:
                        self.next_building_check = time.monotonic() + 120
                        log("[BH] Hoãn kiểm tra nâng công trình 120 giây; các tác vụ khác vẫn tiếp tục.")
                else:
                    self.next_maintenance = time.monotonic() + 60 * minutes
        if (self.village_state is not None and not self.battle.in_progress
                and (actions or time.monotonic() >= self.next_resource_scan)):
            self.scan_resources(self.adb.screencap())

    def night_builder_wait_seconds(self, screen):
        """Read only active jobs above the Builder Base suggestions header."""
        self.tap((1455, 690), .4)
        menu = self.wait_for(self.walls.is_menu_open, 3)
        if menu is None and self.is_home(self.adb.screencap()):
            self.tap((1455, 690), .4)
            menu = self.wait_for(self.walls.is_menu_open, 3)
        if menu is None:
            return 300
        words = self.walls.ocr_positions(menu, (1180, 55, 415, 740))
        headers = [y for text, _, y in words if 'suggest' in text.lower()]
        seconds = None
        if headers:
            end = min(headers)
            grouped = {}
            for text, x, y in words:
                if x < 1400 or not 55 < y < end:
                    continue
                key = next((old for old in grouped if abs(old-y) <= 12), y)
                grouped.setdefault(key, []).append((x, text))
            durations = [self.walls.duration_seconds(''.join(text for _, text in sorted(row)))
                         for row in grouped.values()]
            valid = [value for value in durations if type(value) is int and value > 0]
            if valid:
                seconds = min(valid) + 10
        self.tap((1570, 30), .35)
        return seconds if seconds is not None else 300
