"""Conservative purchase flow for unbuilt entries in the home builder menu."""
import re
import time
from pathlib import Path

import cv2
import numpy as np


class NewBuildingBuyer:
    def __init__(self, worker):
        self.worker = worker
        self.village = worker.village
        self.adb = worker.adb
        self.pending = False
        self.target = None

    @staticmethod
    def has_build_clock(screen, y):
        """The white clock face distinguishes an unread timer from an instant build."""
        if not isinstance(screen, np.ndarray) or screen.shape[:2] != (900, 1600):
            return True
        icon = screen[max(0, y-12):min(900, y+12), 1565:1592]
        if icon.shape[:2] != (24, 27):
            return True
        hsv = cv2.cvtColor(icon, cv2.COLOR_BGR2HSV)
        return int(((hsv[:, :, 1] < 85) & (hsv[:, :, 2] > 185)).sum()) >= 15

    def rows(self, screen, words, free, reserve):
        result = []
        for text, x, y in words:
            if not (1230 <= x <= 1460 and 60 < y < 770):
                continue
            if not any(re.fullmatch('new', t.strip(), re.I) and 1230 <= nx <= 1460
                       and y + 4 <= ny <= y + 40 for t, nx, ny in words):
                continue
            name = text.strip()
            if not re.search('[a-zA-Z]', name) or name.lower() == 'new':
                continue
            duration_text = ''.join(t for t, dx, dy in sorted(words, key=lambda v: v[1])
                                    if dx > 1410 and abs(dy-y) <= 18)
            duration = self.worker.parse_duration(duration_text)
            if duration is None and not self.has_build_clock(screen, y):
                duration = 0
            if free <= reserve and (duration is None or duration >= 60):
                continue
            tokens = []
            for token, px, py in words:
                if px <= 1350 or not y + 10 <= py <= y + 36:
                    continue
                normalized = token.translate(str.maketrans('OoIl', '0011'))
                if re.fullmatch(r'[\d\s,.]+', normalized):
                    digits = re.sub(r'\D', '', normalized)
                    if digits:
                        tokens.append((px, py, digits))
            if not tokens:
                continue
            cost = int(''.join(t[2] for t in sorted(tokens)))
            icon = screen[max(0, tokens[0][1]-16):tokens[0][1]+18, 1560:1598]
            if not icon.size:
                continue
            hsv = cv2.cvtColor(icon, cv2.COLOR_BGR2HSV)
            # Green gem icons are never an eligible purchase, even if a bright
            # yellow highlight also resembles the gold classifier.
            if int((cv2.inRange(hsv, (40, 100, 100), (90, 255, 255)) > 0).sum()) >= 12:
                continue
            resource = self.worker.currency(screen, (1560, tokens[0][1]-16, 38, 34))
            if cost and resource in ('gold', 'elixir', 'dark_elixir'):
                result.append(dict(name=name, y=y, cost=cost, resource=resource, duration_sec=duration))
        return sorted(result, key=lambda row: row['y'])

    @staticmethod
    def _cancel_hit(screen):
        if not isinstance(screen, np.ndarray) or screen.shape[:2] != (900, 1600):
            return None
        root = Path(__file__).resolve().parents[1] / 'local_assets'
        area = screen[50:750, 100:1450]
        best = None
        for name in ('new_building_cancel_live.png', 'new_building_cancel.png'):
            template = cv2.imread(str(root / name))
            if template is None:
                continue
            for scale in (.75, 1., 1.25, 1.5, 2., 2.3, 2.5, 2.7, 3.):
                needle = cv2.resize(template, None, fx=scale, fy=scale)
                _, score, _, point = cv2.minMaxLoc(cv2.matchTemplate(area, needle, cv2.TM_CCOEFF_NORMED))
                if score >= .88:
                    hit = (score, 100+point[0]+needle.shape[1]//2,
                           50+point[1]+needle.shape[0]//2, needle.shape[1])
                    if best is None or hit[0] > best[0]:
                        best = hit
                if best is not None and best[0] >= .96:
                    return best
        return best

    @staticmethod
    def placement_buttons(screen):
        hit = NewBuildingBuyer._cancel_hit(screen)
        if hit is None:
            return None
        _, cx, cy, width = hit
        left = max(0, cx + width//2)
        right = min(1600, cx + int(width*2.6))
        top = max(0, cy - width)
        bottom = min(900, cy + width)
        area = screen[top:bottom, left:right]
        root = Path(__file__).resolve().parents[1] / 'local_assets'
        best = None
        for name in ('new_building_confirm_live.png', 'new_building_confirm.png'):
            template = cv2.imread(str(root / name))
            if template is None:
                continue
            base_scale = width / template.shape[1]
            for scale in (base_scale*.9, base_scale, base_scale*1.1):
                needle = cv2.resize(template, None, fx=scale, fy=scale)
                if needle.shape[0] > area.shape[0] or needle.shape[1] > area.shape[1]:
                    continue
                _, score, _, point = cv2.minMaxLoc(cv2.matchTemplate(area, needle, cv2.TM_CCOEFF_NORMED))
                if score < .8:
                    continue
                crop = area[point[1]:point[1]+needle.shape[0],
                            point[0]:point[0]+needle.shape[1]]
                green = cv2.inRange(cv2.cvtColor(crop, cv2.COLOR_BGR2HSV),
                                    (35, 110, 90), (90, 255, 255))
                if np.mean(green > 0) < .15:
                    continue
                x, y = left+point[0]+needle.shape[1]//2, top+point[1]+needle.shape[0]//2
                if abs(y-cy) <= width*.35 and .7*width < x-cx < 2.3*width:
                    if best is None or score > best[0]:
                        best = (score, x, y)
        return best[1:] if best else None

    @staticmethod
    def cancel_button(screen):
        """Find the red X even when the adjacent confirm button is disabled."""
        hit = NewBuildingBuyer._cancel_hit(screen)
        return hit[1:3] if hit else None

    @staticmethod
    def has_new(words):
        return any(re.fullmatch(r'new', text.strip(), re.I)
                   and 1230 <= x <= 1460 and 50 < y < 795 for text, x, y in words)

    def visible_new(self, free, reserve):
        screen = self.adb.screencap()
        words = self.worker.words(screen, (1170, 50, 430, 755))
        return screen, words, self.rows(screen, words, free, reserve)

    @staticmethod
    def same_row(left, right):
        return (re.sub(r'\s+', ' ', left['name']).strip().casefold() ==
                re.sub(r'\s+', ' ', right['name']).strip().casefold()
                and left['cost'] == right['cost']
                and left['resource'] == right['resource'])

    def reread_row(self, candidate, free, reserve):
        """OCR can miss one frame while the builder list is settling."""
        for retry in range(3):
            _, _, rows = self.visible_new(free, reserve)
            match = next((row for row in rows if self.same_row(row, candidate)), None)
            if match is not None:
                return match
            if retry < 2:
                time.sleep(.2)
        return None

    def seek_new(self, free, reserve, *, target=None, wallet=None):
        """Scan down in place; never scroll away from a row before selecting it."""
        previous = None
        seen_new = False
        first_unaffordable = None
        pages_without_new = 0
        for page in range(20):
            screen, words, rows = self.visible_new(free, reserve)
            has_new = self.has_new(words)
            seen_new |= has_new
            pages_without_new = 0 if has_new else pages_without_new + 1
            for row in rows:
                if target is not None and self.same_row(row, target):
                    return row, seen_new
                if target is None:
                    if wallet is None or wallet.get(row['resource'], -1) >= row['cost']:
                        return row, seen_new
                    if first_unaffordable is None:
                        first_unaffordable = row
            signature = self.worker.ui.menu_signature(words) if hasattr(self.worker.ui, 'menu_signature') else tuple(words)
            if signature == previous or pages_without_new >= 2:
                break
            previous = signature
            self.worker.ui.scroll_menu('down', fast=True)
        return first_unaffordable if target is None else None, seen_new

    def move_empty_ground_to_center(self):
        """Pan a clear grass patch toward the center before asking the game to place again."""
        self.adb.zoom_out_map(times=2)
        time.sleep(.4)
        screen = self.adb.screencap()
        if not isinstance(screen, np.ndarray) or screen.shape[:2] != (900, 1600):
            return False
        hsv = cv2.cvtColor(screen, cv2.COLOR_BGR2HSV)
        grass = cv2.inRange(hsv, (30, 40, 35), (95, 255, 255))
        # A large, mostly green rectangle avoids buildings and the HUD.
        density = cv2.boxFilter((grass > 0).astype(np.float32), -1, (210, 150), normalize=True)
        choices = [(float(density[y, x]), x, y) for y in range(260, 641, 55)
                   for x in range(280, 1151, 55)]
        score, x, y = max(choices, key=lambda item: (item[0], -abs(item[1]-800)-abs(item[2]-450)))
        if score < .72:
            return False
        if abs(x-800) + abs(y-450) < 120:
            # Start on confirmed grass; a small pan changes the suggested placement.
            end_x = min(1120, x + 145) if x < 800 else max(320, x - 145)
            self.adb.swipe(x, y, end_x, y, duration_ms=500, jitter=0)
        else:
            self.adb.swipe(x, y, 800, 450, duration_ms=500, jitter=0)
        time.sleep(.5)
        return True

    def select_for_placement(self, candidate, free, reserve, log):
        """Confirm a green check or retry a grey one after repositioning the map."""
        for attempt in range(4):
            if attempt:
                if not self.move_empty_ground_to_center():
                    return None
                if not self.worker.ui.ensure_menu_open():
                    return None
                candidate, _ = self.seek_new(free, reserve, target=candidate)
                if candidate is None:
                    return None
            match = self.reread_row(candidate, free, reserve)
            if match is None:
                return None
            self.adb.tap(1380, match['y'] + 8, jitter=0, delay_after=.7)
            first = self.adb.screencap()
            point = self.placement_buttons(first)
            second = self.adb.screencap()
            if point and self.placement_buttons(second) == point:
                return point
            cancel = self.cancel_button(second)
            if not cancel:
                return None
            self.adb.tap(*cancel, jitter=0, delay_after=.4)
            log(f'[Công trình mới] Vị trí đặt chưa hợp lệ; đổi vùng đất trống ({attempt+1}/4).')
        return None

    def step(self, log, free, reserve, *, keep_menu_open=False):
        if self.pending:
            return dict(status='uncertain', reason='Giao dịch công trình mới chưa xác minh; dừng mua tiếp.')
        w = self.worker
        w.deadline = time.monotonic() + 60
        if free <= 0 or free < reserve:
            return dict(status='idle', reason='Chưa đủ thợ rảnh để xét công trình mới.')
        screen = self.adb.screencap()
        if not self.village.is_in_main_village(screen):
            return dict(status='blocked', reason='Chưa xác nhận làng chính.')
        wallet = w.vision.read_village_resources(screen)
        before = w.vision.read_village_resources(self.adb.screencap())
        if not w.ui.ensure_menu_open():
            return dict(status='blocked', reason='Không mở được menu thợ.')
        handoff = False
        try:
            candidate, has_new = self.seek_new(free, reserve, wallet=wallet)
            if candidate:
                resource, cost = candidate['resource'], candidate['cost']
                self.target = candidate
                if wallet.get('valid', {}).get(resource) is not True or before.get('valid', {}).get(resource) is not True or wallet[resource] != before[resource]:
                    return dict(status='blocked', reason='Chưa đọc chắc số dư mua công trình mới.')
                if wallet[resource] < cost:
                    return dict(status='needs_resources', resources=[resource], costs={resource: cost}, target=candidate,
                                reason=f'Chờ farm {cost:,} {resource} cho {candidate["name"]}.')
                point = self.select_for_placement(candidate, free, reserve, log)
                if not point:
                    return dict(status='blocked', reason='Chưa xác minh được nút tích xanh sau tối đa 3 lần đổi vị trí.')
                self.pending = True
                self.adb.tap(*point, jitter=0, delay_after=1)
                for _ in range(6):
                    after_screen = self.adb.screencap()
                    if self.village.is_in_main_village(after_screen):
                        after = w.vision.read_village_resources(after_screen)
                        if after.get('valid', {}).get(resource) is True and before[resource]-after[resource] == cost:
                            self.pending = False
                            self.target = None
                            return dict(status='progress', reason=f'Đã mua {candidate["name"]}: {cost:,} {resource}.')
                    time.sleep(.3)
                return dict(status='uncertain', reason='Đã bấm đặt nhưng chưa xác minh tiền đã trừ; dừng mua tiếp.')
            self.target = None
            stable = all(wallet.get('valid', {}).get(r) is True and before.get('valid', {}).get(r) is True
                         and wallet.get(r) == before.get(r) for r in ('gold', 'elixir', 'dark_elixir'))
            handoff = bool(keep_menu_open and stable)
            return dict(status='idle', no_new=not has_new, no_eligible=True,
                        preopened=(wallet, before) if handoff else None,
                        reason='Không thấy công trình New phù hợp; tiếp tục việc khác.')
        finally:
            if not self.pending and not handoff:
                w.close_menu(self.adb.screencap())
