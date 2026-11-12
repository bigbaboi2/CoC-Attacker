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
    def placement_buttons(screen):
        if not isinstance(screen, np.ndarray) or screen.shape[:2] != (900, 1600):
            return None
        root = Path(__file__).resolve().parents[1] / 'local_assets'
        hits = []
        for name in ('new_building_confirm.png', 'new_building_cancel.png'):
            template = cv2.imread(str(root / name))
            if template is None:
                return None
            found = []
            for scale in (.65, .75, .85, 1., 1.1, 1.2, 1.35, 1.5):
                needle = cv2.resize(template, None, fx=scale, fy=scale)
                scores = cv2.matchTemplate(screen, needle, cv2.TM_CCOEFF_NORMED)
                _, score, _, point = cv2.minMaxLoc(scores)
                if score >= .88:
                    found.append((score, point[0] + needle.shape[1]//2, point[1] + needle.shape[0]//2, needle.shape[1]))
            if not found:
                return None
            hits.append(max(found))
        _, x, y, width = hits[0]
        _, cx, cy, cwidth = hits[1]
        if abs(y-cy) > max(width, cwidth)*.35 or not .7*width < x-cx < 2.3*width:
            return None
        return (x, y)

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
            # Keep the player's current menu filters untouched and inspect the
            # initially visible list. Accounts may have no New entries at all.
            screen = self.adb.screencap()
            words = w.words(screen, (1170, 50, 430, 755))
            first_rows = self.rows(screen, words, free, reserve)
            has_new = any(re.fullmatch(r'new', text.strip(), re.I)
                          and 1230 <= x <= 1460 and 50 < y < 795
                          for text, x, y in words)
            second_rows = []
            if has_new:
                w.ui.scroll_menu('down', fast=True)
                screen = self.adb.screencap()
                words = w.words(screen, (1170, 50, 430, 755))
                second_rows = self.rows(screen, words, free, reserve)
            rows = first_rows or second_rows
            if rows:
                candidate = rows[0]
                if first_rows and has_new:
                    w.ui.scroll_menu('up', fast=True)
                    screen = self.adb.screencap()
                    current = self.rows(screen, w.words(screen, (1170, 50, 430, 755)), free, reserve)
                    candidate = next((r for r in current
                                      if r['name'] == candidate['name'] and r['cost'] == candidate['cost']
                                      and r['resource'] == candidate['resource']), None)
                    if candidate is None:
                        return dict(status='blocked', reason='Chưa tìm lại được hàng New sau khi cuộn; chưa mua.')
                resource, cost = candidate['resource'], candidate['cost']
                self.target = candidate
                if wallet.get('valid', {}).get(resource) is not True or before.get('valid', {}).get(resource) is not True or wallet[resource] != before[resource]:
                    return dict(status='blocked', reason='Chưa đọc chắc số dư mua công trình mới.')
                if wallet[resource] < cost:
                    return dict(status='needs_resources', resources=[resource], costs={resource: cost}, target=candidate,
                                reason=f'Chờ farm {cost:,} {resource} cho {candidate["name"]}.')
                live = self.adb.screencap()
                live_rows = self.rows(live, w.words(live, (1170, 50, 430, 755)), free, reserve)
                match = next((r for r in live_rows if r == candidate), None)
                if not match:
                    return dict(status='blocked', reason='Hàng công trình mới chưa ổn định.')
                self.adb.tap(1380, candidate['y'] + 8, jitter=0, delay_after=.7)
                point = self.placement_buttons(self.adb.screencap())
                if not point or point != self.placement_buttons(self.adb.screencap()):
                    return dict(status='blocked', reason='Chưa xác minh được nút đặt công trình; không xác nhận.')
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
            return dict(status='idle', no_new=not has_new,
                        preopened=(wallet, before) if handoff else None,
                        reason='Không thấy công trình New phù hợp; tiếp tục việc khác.')
        finally:
            if not self.pending and not handoff:
                w.close_menu(self.adb.screencap())
